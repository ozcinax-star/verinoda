"""What the project has said about one file: the context an agent should see when it reads or edits it
(``verinoda context FILE``, MCP ``read_context`` behind ``run_tool``, and a Claude Code Read/Edit hook template).

Three sources, each quoted with where it is written:

- **Decision records** (:mod:`verinoda.decisions`): an enforced record whose accepted guard names the file in a
  glob (``only_in allowed=``, ``no_edge from=``/``to=``, ``layers order=``, ``allow_edges``, ``public``; a
  ``tag:NAME`` read as its committed globs), or that governs a symbol of the file. The record's id, the guard and
  the record file are given; whether the code keeps it is ``decide check``'s question, not this one's.
- **Notes**: the user's own notes anchored in the file (:mod:`verinoda.usernotes`), with their state (fresh,
  changed, gone), and notes scoped to a glob: a Markdown file in the notes folder whose header has ``scope:
  GLOB[, GLOB...]`` (and no ``file:``).
- **Rules other tools keep for such files**, when the project has them: Cursor's ``.cursor/rules/*.mdc`` with
  ``globs:`` and Kiro's ``.kiro/steering/*.md`` with ``inclusion: fileMatch`` and ``fileMatchPattern:``. Always-on
  rules are not listed (they are not about this file).

Nothing is inferred: each item is a record, a note or a rule that names the file, with its own words quoted (the
first lines, capped). Files are only read.
"""
from __future__ import annotations

import re
from pathlib import Path

MAX_TEXT = 700          # characters of the hook's additional context
MAX_QUOTE = 160         # characters quoted from one note or rule


def _front(text: str) -> tuple[dict[str, str], str, int]:
    """(front matter keys, body, the body's first line number) of a ``---`` headed file."""
    text = text.replace("\r\n", "\n")
    if not text.startswith("---\n"):
        return {}, text, 1
    head, sep, body = text[4:].partition("\n---\n")
    if not sep:
        return {}, text, 1
    meta: dict[str, str] = {}
    key = None
    for line in head.split("\n"):
        if re.match(r"^\s+-\s+", line) and key:   # a YAML list item under the last key
            meta[key] = (meta[key] + "," if meta[key] else "") + line.strip()[1:].strip().strip("\"'")
            continue
        k, colon, v = line.partition(":")
        if colon:
            key = k.strip()
            meta[key] = v.strip()
    return meta, body, head.count("\n") + 4   # "---", the header lines, "---", then the body


def _globs(value: str) -> list[str]:
    v = value.strip().strip("[]")
    return [g.strip().strip("\"'") for g in v.split(",") if g.strip().strip("\"'")]


def _first(body: str, start: int) -> tuple[str, int]:
    """The first non-empty, non-heading-mark line of a body, and its line number."""
    for k, line in enumerate(body.split("\n")):
        s = line.strip().lstrip("#").strip()
        if s:
            return s[:MAX_QUOTE], start + k
    return "", start


def _rules(repo: Path, rel: str, match) -> list[dict]:
    """Cursor and Kiro rules whose globs match ``rel``, and the user's glob-scoped notes."""
    from verinoda import usernotes

    out: list[dict] = []
    cands: list[tuple[str, Path]] = []
    for d, pat, kind in ((repo / ".cursor" / "rules", "**/*.mdc", "cursor rule"),
                         (repo / ".kiro" / "steering", "*.md", "kiro steering")):
        if d.is_dir():
            cands += [(kind, p) for p in sorted(d.glob(pat))]
    nd = usernotes.notes_dir(repo)
    if nd.is_dir():
        cands += [("scoped note", p) for p in sorted(nd.glob("*.md"))]
    for kind, p in cands:
        try:
            meta, body, at = _front(p.read_text(encoding="utf-8-sig", errors="replace"))
        except OSError:
            continue
        if kind == "cursor rule":
            globs = _globs(meta.get("globs", ""))
        elif kind == "kiro steering":
            globs = _globs(meta.get("fileMatchPattern", "")) if meta.get("inclusion", "") == "fileMatch" else []
        else:
            globs = _globs(meta.get("scope", "")) if not meta.get("file") else []
        hit = next((g for g in globs if match(rel, g)), None)
        if hit is None:
            continue
        quote, line = _first(body, at)
        try:
            where = p.relative_to(repo).as_posix()
        except ValueError:
            where = p.as_posix()
        out.append({"kind": kind, "at": f"{where}:{line}", "glob": hit, "text": quote})
    return out


def _decisions(repo: Path, rel: str, match) -> tuple[list[dict], str | None]:
    from verinoda import decisions as dm

    try:
        recs = dm.load_all(repo)
    except Exception as exc:  # noqa: BLE001 - an unreadable folder: said, never a crash in a hook
        return [], f"decision records could not be read: {type(exc).__name__}"
    try:
        tags = dm.architecture_tags(repo)[0]
    except Exception:  # noqa: BLE001 - tags are optional
        tags = {}

    def globs(v) -> list[str]:
        out = []
        for g in ([v] if isinstance(v, str) else v or []):
            out += tags.get(g[4:], []) if isinstance(g, str) and g.startswith("tag:") else [g]
        return [g for g in out if isinstance(g, str) and g]

    out: list[dict] = []
    for d in recs:
        if not d.enforced:
            continue
        where = d.path.relative_to(repo).as_posix() if d.path and d.path.is_relative_to(repo) else str(d.path)
        for g in d.guards:
            if g.get("status") != "accepted":
                continue
            fields = {"only_in": ("allowed",), "no_edge": ("from", "to"), "layers": ("order",),
                      "allow_edges": ("from", "allowed"), "public": ("module", "api")}.get(g.get("kind"), ())
            for f in fields:
                hit = next((x for x in globs(g.get(f)) if match(rel, x)), None)
                if hit:
                    out.append({"kind": "decision", "decision": d.id, "guard": g.get("id"), "rule": g.get("kind"),
                                "field": f, "glob": hit, "title": d.title, "at": where})
                    break
        for v in d.governs:
            if v.get("file") == rel:
                out.append({"kind": "decision", "decision": d.id, "guard": v.get("id"), "rule": "governs",
                            "field": "symbol", "glob": v.get("symbol"), "title": d.title, "at": where})
    return out, None


def for_file(repo: Path, path: str) -> dict:
    """Everything the project says about ``path`` (repository-relative, or absolute inside the repository)."""
    from verinoda import usernotes
    from verinoda.guards import glob_match

    repo = Path(repo).resolve()
    p = Path(path)
    full = (p if p.is_absolute() else repo / p).resolve()
    try:
        rel = full.relative_to(repo).as_posix()
    except ValueError:
        return {"file": path, "outside": True, "items": [], "note": "outside the project"}
    items, why = _decisions(repo, rel, glob_match)
    for n in usernotes.load_all(repo):
        if n.file == rel:
            state = usernotes.check(repo, n).get("status")
            quote, _ = _first(n.text, 1)
            items.append({"kind": "note", "subject": n.subject, "state": state, "text": quote,
                          "at": f"{n.path.relative_to(repo).as_posix() if n.path and n.path.is_relative_to(repo) else n.path}",
                          "lines": [n.start, n.end]})
    items += _rules(repo, rel, glob_match)
    out = {"file": rel, "items": items}
    if why:
        out["note"] = why
    return out


def text(res: dict, limit: int = MAX_TEXT) -> str:
    """One short block for an agent: one line per item, cut between items at ``limit`` characters."""
    lines = []
    for it in res.get("items") or []:
        if it["kind"] == "decision":
            lines.append(f"{it['decision']} {it['guard']} {it['rule']} ({it['field']} {it['glob']}): {it['title']} "
                         f"[{it['at']}]")
        elif it["kind"] == "note":
            lines.append(f"note on {it['subject']} ({it['state']}): {it['text']} [{it['at']}]")
        else:
            lines.append(f"{it['kind']} for {it['glob']}: {it['text']} [{it['at']}]")
    if not lines:
        return ""
    head = f"verinoda: what this project says about {res['file']}:"
    out, used = [head], len(head)
    for ln in lines:
        if used + len(ln) + 3 > limit:
            out.append(f"  ... {len(lines) - (len(out) - 1)} more (`verinoda context {res['file']}`)")
            break
        out.append("  " + ln)
        used += len(ln) + 3
    return "\n".join(out)
