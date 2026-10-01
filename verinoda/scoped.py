"""What the project has said about one file: the context an agent should see when it reads or edits it
(``verinoda context FILE``, MCP ``read_context`` behind ``run_tool``, and a Claude Code Read/Edit hook template).

Three sources, each quoted with where it is written:

- **Notes and rules scoped to globs**: a Markdown file in the notes folder whose header has ``scope: GLOB[,
  GLOB...]``; Cursor's ``.cursor/rules/*.mdc`` with ``globs:`` (not the ``alwaysApply: true`` ones: they are not
  about this file); Kiro's ``.kiro/steering/*.md`` with ``inclusion: fileMatch`` and ``fileMatchPattern:``. Read in
  the project and, when the project is a folder of its repository, at the repository's top too (where those tools
  keep them; their globs are then matched against the repository path). Globs are read the way those tools read
  them: ``**/`` matches no folder or any number, ``{a,b}`` is either.
- **The user's notes anchored in the file** (:mod:`verinoda.usernotes`), with their state (fresh, changed, gone).
- **Decision records** (:mod:`verinoda.decisions`): an enforced record whose accepted guard names the file in a
  glob (``only_in allowed=``, ``no_edge from=``/``to=``, ``layers order=``, ``allow_edges``, ``public``; a
  ``tag:NAME`` read as its committed globs), or that governs a symbol of the file; the guard is quoted as written.

Each note or rule is quoted by its first line of text (a heading only when there is nothing else), with its
``path:line``. Nothing is inferred and files are only read. The sources are read once and kept until one of their
files changes (the Read/Edit hook calls this on every file the agent opens).
"""
from __future__ import annotations

import re
import threading
from pathlib import Path

MAX_TEXT = 700          # characters of the hook's additional context
MAX_QUOTE = 160         # characters quoted from one note or rule

_CACHE: dict[str, tuple[tuple, dict]] = {}
_LOCK = threading.Lock()


# -- reading ------------------------------------------------------------------------------------------------------

def _front(text: str) -> tuple[dict[str, str], str, int]:
    """(front matter keys, body, the body's first line number) of a ``---`` headed file. The closing ``---`` may
    carry trailing spaces or end the file."""
    text = text.replace("\r\n", "\n")
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, text, 1
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return {}, text, 1
    meta: dict[str, str] = {}
    key = None
    for line in lines[1:end]:
        if re.match(r"^\s+-\s+", line) and key:   # a YAML list item under the last key
            meta[key] = (meta[key] + "," if meta[key] else "") + line.strip()[1:].strip()
            continue
        k, colon, v = line.partition(":")
        if colon and not line[:1].isspace():
            key = k.strip()
            meta[key] = v.strip()
    return meta, "\n".join(lines[end + 1:]), end + 2


def _unquote(v: str) -> str:
    v = v.strip()
    return v[1:-1] if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'" else v


def _globs(value: str) -> list[str]:
    """Globs of a front matter value: a YAML flow list, a list, or one comma-separated string; commas inside a
    ``{a,b}`` set do not split."""
    v = value.strip()
    if v.startswith("[") and v.endswith("]"):
        v = v[1:-1]
    out, depth, cur = [], 0, ""
    for ch in v:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    out.append(cur)
    return [g for g in (_unquote(x) for x in out) if g]


def _braces(glob: str) -> list[str]:
    m = re.search(r"\{([^{}]*)\}", glob)
    if not m:
        return [glob]
    return [x for alt in m.group(1).split(",") for x in _braces(glob[:m.start()] + alt + glob[m.end():])]


_RX: dict[str, re.Pattern] = {}


def rule_match(path: str, glob: str) -> bool:
    """``path`` against a glob as Cursor, Kiro and editors read it: ``**/`` is no folder or any, ``**`` at the end
    anything, ``*`` within one folder, ``?`` one character, ``{a,b}`` either."""
    for g in _braces(glob.strip().lstrip("/")):
        rx = _RX.get(g)
        if rx is None:
            out, i = "", 0
            while i < len(g):
                if g.startswith("**/", i):
                    out += "(?:.*/)?"
                    i += 3
                elif g.startswith("**", i):
                    out += ".*"
                    i += 2
                elif g[i] == "*":
                    out += "[^/]*"
                    i += 1
                elif g[i] == "?":
                    out += "[^/]"
                    i += 1
                else:
                    out += re.escape(g[i])
                    i += 1
            rx = _RX[g] = re.compile(out + r"\Z")
        if rx.match(path):
            return True
    return False


def _quote(body: str, start: int) -> tuple[str, int]:
    """The first line of text of a body (a heading only when nothing else is there), and its line number."""
    lines = body.split("\n")
    for k, line in enumerate(lines):
        s = line.strip()
        if s and not s.startswith("#"):
            return s[:MAX_QUOTE], start + k
    for k, line in enumerate(lines):
        s = line.strip().lstrip("#").strip()
        if s:
            return s[:MAX_QUOTE], start + k
    return "(empty)", start


# -- the sources, kept until a file of theirs changes --------------------------------------------------------------

def _source_files(repo: Path, top: Path | None) -> list[tuple[str, Path, str]]:
    from verinoda import usernotes

    cands: list[tuple[str, Path, str]] = []
    for base, prefix in ((repo, ""), *(((top, "top"),) if top is not None and top != repo else ())):
        for d, pat, kind in ((base / ".cursor" / "rules", "**/*.mdc", "cursor rule"),
                             (base / ".kiro" / "steering", "*.md", "kiro steering")):
            if d.is_dir():
                cands += [(kind, p, prefix) for p in sorted(d.glob(pat))]
    nd = usernotes.notes_dir(repo)
    if nd.is_dir():
        cands += [("scoped note", p, "") for p in sorted(nd.glob("*.md"))]
    return cands


def _signature(repo: Path, files: list[Path]) -> tuple:
    from verinoda import decisions as dm

    extra = [repo / "verinoda.toml", repo / "pyproject.toml", repo / ".verinoda" / "config.json"]
    try:
        ddir = dm.decisions_dir_source(repo)[0]
        extra += sorted(ddir.glob("*.md")) if ddir.is_dir() else []
    except Exception:  # noqa: BLE001 - the decision records are read (and their error said) below
        pass
    sig = []
    for p in [*files, *extra]:
        try:
            st = p.stat()
            sig.append((str(p), st.st_mtime_ns, st.st_size))
        except OSError:
            sig.append((str(p), None, None))
    return tuple(sig)


def _load(repo: Path) -> dict:
    """The parsed rules, scoped notes, anchored notes and decision records of ``repo``, from the cache when none of
    their files changed."""
    from verinoda import decisions as dm
    from verinoda import usernotes
    from verinoda.snapshot import git
    from verinoda.treestate import project_prefix

    top_s = (git(repo, "rev-parse", "--show-toplevel") or "").strip()
    top = Path(top_s).resolve() if top_s else None
    cands = _source_files(repo, top)
    sig = _signature(repo, [p for _, p, _ in cands] + [usernotes.notes_dir(repo)])
    key = str(repo)
    with _LOCK:
        hit = _CACHE.get(key)
        if hit is not None and hit[0] == sig:
            return hit[1]
    rules = []
    for kind, p, where_from in cands:
        try:
            meta, body, at = _front(p.read_text(encoding="utf-8-sig", errors="replace"))
        except OSError:
            continue
        if kind == "cursor rule":
            if _unquote(meta.get("alwaysApply", "")).lower() == "true":
                continue
            globs = _globs(meta.get("globs", ""))
        elif kind == "kiro steering":
            globs = _globs(meta.get("fileMatchPattern", "")) if _unquote(meta.get("inclusion", "")) == "fileMatch" \
                else []
        else:
            globs = _globs(meta.get("scope", ""))
        if not globs:
            continue
        quote, line = _quote(body, at)
        base = top if where_from == "top" else repo
        try:
            where = p.relative_to(repo).as_posix()
        except ValueError:
            where = p.relative_to(base).as_posix() if base is not None else p.as_posix()
        rules.append({"kind": kind, "globs": globs, "at": f"{where}:{line}", "text": quote,
                      "prefix": (project_prefix(repo) or "") if where_from == "top" else ""})
    error = None
    try:
        recs = dm.load_all(repo)
        tags = dm.architecture_tags(repo)[0]
    except Exception as exc:  # noqa: BLE001 - an unreadable folder: said, never a crash in a hook
        recs, tags, error = [], {}, f"decision records could not be read: {type(exc).__name__}"
    data = {"rules": rules, "notes": usernotes.load_all(repo), "records": recs, "tags": tags, "error": error}
    with _LOCK:
        _CACHE[key] = (sig, data)
    return data


# -- one file -------------------------------------------------------------------------------------------------------

def _decisions(repo: Path, rel: str, data: dict) -> list[dict]:
    from verinoda.guards import glob_match

    tags = data["tags"]

    def globs(v) -> list[str]:
        out = []
        for g in ([v] if isinstance(v, str) else v or []):
            out += tags.get(g[4:], []) if isinstance(g, str) and g.startswith("tag:") else [g]
        return [g for g in out if isinstance(g, str) and g]

    out: list[dict] = []
    for d in data["records"]:
        if not d.enforced:
            continue
        where = d.path.relative_to(repo).as_posix() if d.path and d.path.is_relative_to(repo) else str(d.path)
        for g in d.guards:
            if g.get("status") != "accepted":
                continue
            fields = {"only_in": ("allowed",), "no_edge": ("from", "to"), "layers": ("order",),
                      "allow_edges": ("from", "allowed"), "public": ("module", "api")}.get(g.get("kind"), ())
            for f in fields:
                if any(glob_match(rel, x) for x in globs(g.get(f))):   # the checks' own matching
                    out.append({"kind": "decision", "decision": d.id, "guard": g.get("id"), "rule": g.get("kind"),
                                "field": f, "spec": g.get("spec") or "", "title": d.title, "at": where})
                    break
        for v in d.governs:
            if v.get("file") == rel:
                out.append({"kind": "decision", "decision": d.id, "guard": v.get("id"), "rule": "governs",
                            "field": "symbol", "spec": f"governs {v.get('symbol')}", "title": d.title, "at": where})
    return out


def for_file(repo: Path, path: str) -> dict:
    """Everything the project says about ``path`` (repository-relative, or absolute inside the repository)."""
    from verinoda import usernotes

    repo = Path(repo).resolve()
    p = Path(path)
    full = (p if p.is_absolute() else repo / p).resolve()
    try:
        rel = full.relative_to(repo).as_posix()
    except ValueError:
        return {"file": path, "outside": True, "items": [], "note": "outside the project"}
    data = _load(repo)
    items: list[dict] = []
    for r in data["rules"]:
        target = r["prefix"] + rel
        hit = next((g for g in r["globs"] if rule_match(target, g)), None)
        if hit is not None:
            items.append({"kind": r["kind"], "glob": hit, "text": r["text"], "at": r["at"]})
    for n in data["notes"]:
        if n.file == rel:
            state = usernotes.check(repo, n).get("status")
            quote, _ = _quote(n.text, 1)
            where = n.path.relative_to(repo).as_posix() if n.path and n.path.is_relative_to(repo) else str(n.path)
            items.append({"kind": "note", "subject": n.subject, "state": state, "text": quote, "at": where,
                          "lines": [n.start, n.end]})
    items += _decisions(repo, rel, data)
    out = {"file": rel, "items": items}
    if data["error"]:
        out["note"] = data["error"]
    return out


def text(res: dict, limit: int | None = MAX_TEXT) -> str:
    """One short block for an agent, one line per item (scoped notes and rules first: they are written for files
    like this one), cut between items at ``limit`` characters (None: all)."""
    lines = []
    for it in res.get("items") or []:
        if it["kind"] == "decision":
            lines.append(f"{it['decision']} {it['guard']} `{it['spec']}`: {it['title']} [{it['at']}]")
        elif it["kind"] == "note":
            lines.append(f"note on {it['subject']} ({it['state']}): {it['text']} [{it['at']}]")
        else:
            lines.append(f"{it['kind']} for {it['glob']}: {it['text']} [{it['at']}]")
    if not lines:
        return ""
    head = f"verinoda: what this project says about {res['file']}:"
    out, used = [head], len(head)
    for ln in lines:
        if limit is not None and used + len(ln) + 3 > limit:
            out.append(f"  ... {len(lines) - (len(out) - 1)} more (`verinoda context {res['file']}`)")
            break
        out.append("  " + ln)
        used += len(ln) + 3
    return "\n".join(out)
