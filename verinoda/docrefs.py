"""Code references in the repository's own documents, checked against the tree (``verinoda docs check``).

A README or a design note says "see ``verinoda/history.py``" or links ``[the loader](src/load.py#L40-L52)``; the
code moves on and the sentence points at nothing, or at other lines. This module finds such references in the
tracked Markdown, reStructuredText and AsciiDoc files and checks each against the working tree:

- **References** are Markdown link targets (relative to the document's folder, or to the repository root when
  they start with ``/``; percent-encoding decoded; ``#section`` anchors dropped) and inline code spans that read
  as a path (a ``/`` and a file extension, or a trailing ``/``; no URL, glob, flag or placeholder), with an
  optional line or range: ``path:12``, ``path:12-30``, ``path#L12-L30``. Fenced code and links shown inside inline
  code are not references. A code span is tried from the repository root, the document's folder, then each folder
  above it.
- **Checked**: the path exists (a file or a folder) and the cited lines are inside the file. A code-span path whose
  first folder is under none of those bases is another project's example and not checked, unless git recorded it
  as renamed; one git ignores is a runtime file and not checked either.
- **Drift**: a reference with lines that the document's last commit already had, written the same, is compared
  with the file as it was at that commit (the same text diff the hotspots view uses): lines that only moved are
  ``moved`` (fixable), lines that changed or went are ``changed`` (flagged). A reference the document gained or
  changed since its commit (an edit, or ``--fix`` itself) was written against the tree as it is, and is only
  checked against the file's length; so ``--fix`` twice changes nothing the second time.
- **Renames**: a missing path git recorded as renamed (``git log -M --diff-filter=R``, followed to the last name,
  a chain that comes back to a name included) is ``renamed``, fixable, its lines compared too.

``--fix`` rewrites only ``renamed`` and ``moved`` references, the reference's own characters (its separators and
its base kept) and nothing else; a document without an applicable edit is not written. Git is only read.
"""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote

from verinoda.snapshot import git, list_files

DOC_EXT = (".md", ".markdown", ".rst", ".adoc")
CODE_EXT = frozenset(("py", "pyi", "js", "jsx", "ts", "tsx", "mjs", "cjs", "java", "kt", "kts", "go", "rs", "c",
                      "h", "cc", "cpp", "hpp", "cs", "rb", "php", "swift", "scala", "sh", "ps1", "sql", "toml",
                      "json", "yaml", "yml", "cfg", "ini", "xml", "gradle", "md", "rst", "txt", "html", "css",
                      "mcfunction", "glsl", "vsh", "fsh", "lua", "r", "jl", "dart", "vue", "svelte"))
_LINK = re.compile(r"\]\(\s*<?([^()\s<>]+)>?(?:\s+\"[^\"\n]*\")?\s*\)")
_REF = re.compile(r"^(?P<path>[^\s:#<>{}*?|\"'()\[\]]+?)(?::(?P<a>\d+)(?:-(?P<b>\d+))?)?"
                  r"(?:#L(?P<la>\d+)(?:-L?(?P<lb>\d+))?)?$")
# a link target may hold spaces (decoded %20); a code span may not
_REF_LINK = re.compile(r"^(?P<path>[^:#<>{}*?|\"'()\[\]]+?)(?:#L(?P<la>\d+)(?:-L?(?P<lb>\d+))?)?$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
MAX_REFS = 5000
MAX_LINE = 20_000     # characters of one document line read for references
FOREIGN = "not a path of this repository (its first folder is not here)"
IGNORED = "a path git ignores (a runtime or generated file)"


def _path_like(p: str, from_link: bool) -> bool:
    """A code span or link target that reads as a repository path (not a URL, a flag, a glob or a placeholder);
    a code span needs a ``/`` and a file extension, or a trailing ``/`` (a folder)."""
    if not p or "://" in p or p.startswith(("-", "$", "~", "#", "mailto:", "@")) or \
            any(ch in p for ch in "*?<>{}|=,;"):
        return False
    if from_link:
        return True
    if "/" not in p.strip("/"):
        return False
    if p.endswith("/"):
        return True
    last = p.rsplit("/", 1)[-1]
    ext = last.rsplit(".", 1)[-1].lower() if "." in last[1:] else ""
    return ext in CODE_EXT or (0 < len(ext) <= 5 and ext.isalnum())


def _code_spans(line: str) -> list[tuple[int, int]]:
    """(start, end) of each inline code span's content, backtick runs matched by length in one pass (a long run
    of backticks costs linear time)."""
    runs = [(m.start(), m.end()) for m in re.finditer(r"`+", line)]
    out, i = [], 0
    while i < len(runs):
        s, e = runs[i]
        n = e - s
        j = next((k for k in range(i + 1, len(runs)) if runs[k][1] - runs[k][0] == n), None)
        if j is None:
            i += 1
            continue
        out.append((e, runs[j][0]))
        i = j + 1
    return out


def references(text: str) -> list[dict]:
    """Every reference in one document's text: ``{line, start, end, raw, path, a, b, kind}`` (``start``/``end``
    are the columns of ``raw`` on its line)."""
    out: list[dict] = []
    fence: tuple[str, int] | None = None
    for n, line in enumerate(text.split("\n"), 1):
        line = line[:-1] if line.endswith("\r") else line
        m = _FENCE.match(line)
        if m:
            ch, size = m.group(1)[0], len(m.group(1))
            if fence is None:
                fence = (ch, size)
                continue
            if ch == fence[0] and size >= fence[1] and not m.group(2).strip():
                fence = None
            continue
        if fence or len(line) > MAX_LINE:
            continue
        codes = _code_spans(line)
        masked = list(line)
        for s, e in codes:   # a link written inside inline code is an example, not a link
            masked[s - 1:e + 1] = " " * (e - s + 2)
        masked_line = "".join(masked)
        spans = [(mm.start(1), mm.end(1), mm.group(1), "link") for mm in _LINK.finditer(masked_line)]
        for s, e in codes:
            raw = line[s:e]
            lead = len(raw) - len(raw.lstrip())
            raw = raw.strip()
            spans.append((s + lead, s + lead + len(raw), raw, "code"))
        for start, end, raw, kind in spans:
            ref = raw
            if kind == "link":
                target, _, anchor = raw.partition("#")
                ref = unquote(target) + (f"#{anchor}" if re.fullmatch(r"L\d+(?:-L?\d+)?", anchor) else "")
            mm = (_REF_LINK if kind == "link" else _REF).match(ref)
            if not mm or not _path_like(mm.group("path").replace("\\", "/"), kind == "link"):
                continue
            groups = mm.groupdict()
            a = groups.get("a") or groups.get("la")
            b = groups.get("b") or groups.get("lb")
            out.append({"line": n, "start": start, "end": end, "raw": raw, "written": mm.group("path"),
                        "path": mm.group("path").replace("\\", "/"), "a": int(a) if a else None,
                        "b": int(b) if b else (int(a) if a else None), "kind": kind})
    return out


def _norm(p: str) -> str | None:
    parts: list[str] = []
    for seg in p.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if not parts:
                return None
            parts.pop()
        else:
            parts.append(seg)
    return "/".join(parts)


def _lines_of(text: str) -> list[str]:
    text = text.replace("\r\n", "\n")
    return text.split("\n")[:-1] if text.endswith("\n") else text.split("\n")


class _Tree:
    def __init__(self, repo: Path):
        self.repo = repo
        self.files = set(list_files(repo))
        self.dirs = {"/".join(f.split("/")[:k]) for f in self.files for k in range(1, f.count("/") + 1)}
        self._renames: dict[str, str] | None = None
        self._lines: dict[str, list[str]] = {}
        self._at: dict[tuple[str, str], str | None] = {}
        self._doc_commit: dict[str, str | None] = {}
        self._ignored: set[str] = set()
        self.shallow = (git(repo, "rev-parse", "--is-shallow-repository") or "").strip() == "true"

    def exists(self, rel: str) -> bool:
        return rel in self.files or rel in self.dirs or (self.repo / rel).exists()

    def lines(self, rel: str) -> list[str]:
        if rel not in self._lines:
            try:
                t = (self.repo / rel).read_bytes().decode("utf-8", errors="replace")
            except OSError:
                t = ""
            self._lines[rel] = _lines_of(t)
        return self._lines[rel]

    def ignored(self, rel: str) -> bool:
        return rel in self._ignored

    def load_ignored(self, rels: set[str]) -> None:
        """Which of ``rels`` git ignores, in one ``git check-ignore -z --stdin`` call."""
        if not rels:
            return
        import subprocess

        try:
            # NUL-separated bytes: no quoting of unusual names, no CR added to the input on Windows
            r = subprocess.run(["git", "-C", str(self.repo), "check-ignore", "--no-index", "-z", "--stdin"],
                               input=b"\0".join(x.encode("utf-8") for x in sorted(rels)) + b"\0",
                               capture_output=True, timeout=60)
            self._ignored = {x.decode("utf-8", "replace") for x in r.stdout.split(b"\0") if x}
        except (OSError, subprocess.TimeoutExpired):
            self._ignored = set()

    def renames(self) -> dict[str, str]:
        if self._renames is None:
            # -z: names are not quoted (a non-ASCII name is written as it is)
            out = git(self.repo, "log", "-M", "--diff-filter=R", "--name-status", "-z", "--format=", "--reverse",
                      "--relative", timeout=120) or ""
            parts = [p for p in out.split("\0")]
            rn: dict[str, str] = {}
            i = 0
            while i + 2 < len(parts):
                status = parts[i].strip()
                if status.startswith("R"):
                    rn[parts[i + 1]] = parts[i + 2]
                    i += 3
                else:
                    i += 1
            self._renames = rn
        return self._renames

    def renamed_to(self, rel: str) -> str | None:
        """The name ``rel`` has now, by following git's recorded renames (a chain that comes back to an earlier
        name ends at the name that exists); None when it was not renamed to a file that exists."""
        rn = self.renames()
        seen, cur = {rel}, rel
        while cur in rn and rn[cur] not in seen:
            cur = rn[cur]
            seen.add(cur)
        return cur if cur != rel and cur in self.files else None

    def doc_commit(self, doc: str) -> str | None:
        if doc not in self._doc_commit:
            self._doc_commit[doc] = (git(self.repo, "log", "-1", "--format=%H", "--", doc) or "").strip() or None
        return self._doc_commit[doc]

    def text_at(self, sha: str, rel: str) -> str | None:
        k = (sha, rel)
        if k not in self._at:
            t = git(self.repo, "show", f"{sha}:./{rel}")
            self._at[k] = t.replace("\r\n", "\n") if t is not None else None
        return self._at[k]


def _resolve(tree: _Tree, doc: str, ref: dict) -> tuple[str | None, str | None, str]:
    """(repository path, why it is not checked or None, the base it was read from). A link is read from the
    document's folder (``/``: the root); a code span from the root, the document's folder, then each folder
    above it. A missing code-span path belongs here when its first folder exists under the base it was read
    from, or git recorded it as renamed; otherwise it is ``FOREIGN`` (or ignored: decided later, in one call)."""
    folder = doc.rsplit("/", 1)[0] if "/" in doc else ""
    p = ref["path"]
    if ref["kind"] == "link":
        bases = [""] if p.startswith("/") else [folder]
        p = p.lstrip("/")
    else:
        up = folder.split("/") if folder else []
        bases = ["", *("/".join(up[:k]) for k in range(len(up), 0, -1))]
    cands = []
    for base in dict.fromkeys(bases):
        c = _norm(f"{base}/{p}" if base else p)
        if c:
            cands.append((base, c))
    if not cands:
        return None, "outside the repository", ""
    for base, c in cands:
        if tree.exists(c):
            return c, None, base
    if ref["kind"] == "link":   # a link names a file of this repository, relative to its document
        return cands[0][1], None, cands[0][0]
    for base, c in cands:
        if tree.renamed_to(c):
            return c, None, base
    first = (_norm(p) or "").split("/", 1)[0]
    for base, c in cands:
        if first and tree.exists(f"{base}/{first}" if base else first):
            return c, None, base
    return cands[0][1], FOREIGN, cands[0][0]


def _written(ref: dict, new_rel: str, base: str, doc: str) -> str:
    """``new_rel`` (a repository path) written as the reference wrote its path: from the same base, with the
    same separators and leading ``./`` or ``/``."""
    w = ref["written"]
    if ref["kind"] == "link" and not w.startswith("/"):
        folder = doc.rsplit("/", 1)[0] if "/" in doc else ""
        up = [s for s in folder.split("/") if s]
        new_parts = new_rel.split("/")
        k = 0
        while k < len(up) and k < len(new_parts) - 1 and up[k] == new_parts[k]:
            k += 1
        out = "../" * (len(up) - k) + "/".join(new_parts[k:])
        if w.startswith("./") and not out.startswith("../"):
            out = "./" + out
    elif base and new_rel.startswith(base + "/"):
        out = new_rel[len(base) + 1:]
    else:
        out = ("/" if w.startswith("/") else "") + new_rel
    return out.replace("/", "\\") if "\\" in w and "/" not in w.replace("\\", "") else out


def _with_path(ref: dict, new_written: str) -> str:
    """The reference's raw text with its path part replaced (lines and anchors kept)."""
    raw = ref["raw"]
    w = ref["written"]
    i = raw.find(w)
    if i < 0:   # a percent-encoded link: its path is the raw target up to '#'
        head, sep, tail = raw.partition("#")
        return new_written + (sep + tail if sep else "")
    return raw[:i] + new_written + raw[i + len(w):]


def _renumber(raw: str, a: int, b: int) -> str:
    if "#L" in raw:
        head = raw.split("#L", 1)[0]
        return f"{head}#L{a}" + (f"-L{b}" if b != a else "")
    head = raw.rsplit(":", 1)[0] if re.search(r":\d+(-\d+)?$", raw) else raw
    return f"{head}:{a}" + (f"-{b}" if b != a else "")


def _committed_refs(tree: _Tree, doc: str) -> set[str] | None:
    """The raw references the document's last commit has (None: the document has no commit)."""
    sha = tree.doc_commit(doc)
    if not sha:
        return None
    text = tree.text_at(sha, doc)
    return {r["raw"] for r in references(text)} if text is not None else None


def _drift(tree: _Tree, doc: str, rel: str, a: int, b: int, then_rel: str | None = None):
    """None when the cited lines are as they were at the document's last commit (or that cannot be read);
    ("moved", (a', b')) when they only moved; ("changed", why) when they changed or went. ``then_rel``: the
    file's name at that commit (a renamed file)."""
    sha = tree.doc_commit(doc)
    if not sha:
        return None
    old = tree.text_at(sha, then_rel or rel)
    if old is None:
        return None
    cur = "\n".join(tree.lines(rel)) + "\n"
    if old == cur or old.rstrip("\n") == cur.rstrip("\n"):
        return None
    from verinoda.hotspots import _line_map

    old_lines = _lines_of(old)
    if b > len(old_lines):
        return None
    m = _line_map(old if old.endswith("\n") else old + "\n", cur)
    new = [m[i - 1] if i - 1 < len(m) else 0 for i in range(a, b + 1)]
    cur_lines = tree.lines(rel)
    same = all(x and x <= len(cur_lines) and old_lines[i - 1] == cur_lines[x - 1]
               for i, x in zip(range(a, b + 1), new))
    if same and new == list(range(new[0], new[0] + len(new))):
        return None if new[0] == a else ("moved", (new[0], new[-1]))
    gone = sum(1 for x in new if not x)
    return ("changed", f"{rel}:{a}-{b} changed since {doc} was last committed ({sha[:10]}): "
                       f"{gone} of {b - a + 1} line(s) gone, the rest edited or moved apart")


def docs_in(repo: Path, paths: list[str] | None) -> list[str]:
    """The tracked documents under ``paths`` (repository-relative, ``./``-prefixed or absolute); ValueError for a
    path that is no document or folder of the repository (a gate never passes on nothing checked)."""
    repo = Path(repo).resolve()
    files = sorted(f for f in list_files(repo) if f.lower().endswith(DOC_EXT))
    if not paths:
        return files
    out: list[str] = []
    for raw in paths:
        p = Path(raw)
        full = (p if p.is_absolute() else repo / p).resolve()
        try:
            rel = full.relative_to(repo).as_posix()
        except ValueError:
            raise ValueError(f"{raw} is outside the repository {repo}") from None
        hit = [f for f in files if rel in ("", ".") or f == rel or f.startswith(rel + "/")]
        if not hit:
            raise ValueError(f"{raw}: no tracked document (.md, .markdown, .rst, .adoc) there")
        out += hit
    return sorted(dict.fromkeys(out))


def check(repo: Path, docs: list[str] | None = None, *, exclude: list[str] | None = None) -> dict:
    """Check every reference in ``docs`` (default: the tracked documents), leaving out documents that match a
    glob of ``exclude`` (``vendor/**``)."""
    from verinoda.guards import glob_match

    repo = Path(repo).resolve()
    tree = _Tree(repo)
    all_docs = docs_in(repo, docs)
    skipped = [d for d in all_docs if any(glob_match(d, g) for g in exclude or ())]
    all_docs = [d for d in all_docs if d not in set(skipped)]
    found: dict[str, list[dict]] = {k: [] for k in ("broken", "renamed", "moved", "changed")}
    unchecked: dict[str, int] = {}

    def skip(why: str) -> None:
        unchecked[why] = unchecked.get(why, 0) + 1

    pending: list[tuple[str, dict, str, str | None, str]] = []
    n_refs = 0
    for doc in all_docs:
        try:
            text = (repo / doc).read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            skip("a document that is not UTF-8")
            continue
        for ref in references(text):
            n_refs += 1
            if n_refs > MAX_REFS:
                break
            rel, why, base = _resolve(tree, doc, ref)
            if rel is None:
                skip(why)
            else:
                pending.append((doc, ref, rel, why, base))
    tree.load_ignored({rel for _, _, rel, _, _ in pending if not tree.exists(rel)})
    ok = 0
    committed: dict[str, set[str] | None] = {}
    for doc, ref, rel, why, base in pending:
        if why == FOREIGN:   # a path git ignores is this repository's runtime file, not another project's
            skip(IGNORED if tree.ignored(rel) else FOREIGN)
            continue
        item = {"doc": doc, "at": f"{doc}:{ref['line']}", "ref": ref["raw"], "path": rel, "kind": ref["kind"],
                "start": ref["start"], "end": ref["end"]}
        now = rel
        if not tree.exists(rel):
            if tree.ignored(rel):
                skip(IGNORED)
                continue
            now = tree.renamed_to(rel)
            if not now:
                found["broken"].append({**item, "status": "statically_verified",
                                        "why": f"{rel} does not exist in the working tree"})
                continue
        renamed = now != rel
        fixed_raw = _with_path(ref, _written(ref, now, base, doc)) if renamed else ref["raw"]
        if ref["a"] is None or now not in tree.files:
            if renamed:
                found["renamed"].append({**item, "to": now, "status": "statically_verified",
                                         "why": f"{rel} was renamed to {now} (git log -M)", "fix": fixed_raw})
            else:
                ok += 1
            continue
        a, b = ref["a"], max(ref["a"], ref["b"] or ref["a"])
        n = len(tree.lines(now))
        if doc not in committed:
            committed[doc] = _committed_refs(tree, doc)
        # a reference the document's last commit had as it is: its lines were written against that commit
        as_committed = committed[doc] is not None and ref["raw"] in committed[doc]
        drift = _drift(tree, doc, now, a, b, then_rel=rel if renamed else None) if as_committed else None
        if drift is not None and drift[0] == "changed":
            found["changed"].append({**item, "status": "statically_verified", "lines": [a, b], "why": drift[1]
                                     + (f" (and {rel} was renamed to {now})" if renamed else "")})
        elif drift is None and b > n:
            found["broken"].append({**item, "status": "statically_verified",
                                    "why": f"{now} has {n} lines; the reference cites {a}-{b}"})
        elif drift is not None:   # moved (and maybe renamed)
            na, nb = drift[1]
            found["moved"].append({**item, "status": "statically_verified", "lines": [a, b], "to": [na, nb],
                                   **({"renamed_to": now} if renamed else {}),
                                   "why": f"{rel}:{a}-{b} moved to {now}:{na}-{nb} since {doc} was last committed",
                                   "fix": _renumber(fixed_raw, na, nb)})
        elif renamed:
            found["renamed"].append({**item, "to": now, "status": "statically_verified",
                                     "why": f"{rel} was renamed to {now} (git log -M)", "fix": fixed_raw})
        else:
            ok += 1
    failing = sum(len(v) for v in found.values())
    limits = ["symbols named without a path (`Class.method`) are not checked",
              "a code-span path whose first folder is under neither the root, the document's folder nor a folder "
              "above it (and that git did not record as renamed) is taken for another project's and not checked",
              "a document written for another location (a template copied elsewhere at install time) is read where "
              "it is; exclude it with --exclude",
              "drift is measured only for references the document's last commit had as they are; others are checked "
              "against the file's length"]
    if tree.shallow:
        limits.append("a shallow clone: renames and the files' earlier versions before its boundary are missing, so "
                      "a rename can be reported as broken and drift missed")
    return {"docs": len(all_docs), "references": n_refs, "ok": ok, **found, "unchecked": unchecked,
            **({"excluded_docs": len(skipped)} if skipped else {}), "truncated": n_refs > MAX_REFS,
            "method": "Markdown link targets and inline code spans that read as paths, checked against the working "
                      "tree; lines compared with the file at the document's last commit (text diff); renames from "
                      "git log -M", "limits": limits, "exit": 1 if failing else 0}


def fix(repo: Path, res: dict) -> list[dict]:
    """Rewrite the ``renamed`` and ``moved`` references in place: only the reference's own characters, each line's
    own line end kept; a reference whose text is no longer at its place, or whose fix changes nothing, is left
    alone, and a document with no applicable edit is not written."""
    repo = Path(repo).resolve()
    by_doc: dict[str, list[dict]] = {}
    for f in [*res.get("renamed", []), *res.get("moved", [])]:
        if f.get("fix") and f["fix"] != f["ref"]:
            by_doc.setdefault(f["doc"], []).append(f)
    done = []
    for doc, items in by_doc.items():
        p = repo / doc
        try:
            text = p.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        lines = text.split("\n")
        edits = 0
        for f in sorted(items, key=lambda f: (int(f["at"].rsplit(":", 1)[1]), -f["start"])):
            n = int(f["at"].rsplit(":", 1)[1])
            if n > len(lines):
                continue
            ln = lines[n - 1]
            if ln[f["start"]:f["end"]] != f["ref"]:
                continue
            lines[n - 1] = ln[:f["start"]] + f["fix"] + ln[f["end"]:]
            edits += 1
            done.append({"at": f["at"], "from": f["ref"], "to": f["fix"]})
        if edits:
            p.write_bytes("\n".join(lines).encode("utf-8"))
    return done


def render(res: dict) -> str:
    out = [f"docs check: {res['docs']} document(s), {res['references']} reference(s): {res['ok']} ok, "
           f"{len(res['broken'])} broken, {len(res['changed'])} changed, {len(res['renamed'])} renamed, "
           f"{len(res['moved'])} moved"]
    for key in ("broken", "changed", "renamed", "moved"):
        for f in res[key]:
            out.append(f"  {key.upper()} {f['at']} `{f['ref']}`: {f['why']}"
                       + (f" -> `{f['fix']}`" if f.get("fix") else ""))
    for f in res.get("fixed") or []:
        out.append(f"  fixed {f['at']}: `{f['from']}` -> `{f['to']}`")
    for why, n in sorted((res.get("unchecked") or {}).items()):
        out.append(f"  not checked: {n} ({why})")
    for lim in res.get("limits") or []:
        if lim.startswith("a shallow clone"):
            out.append(f"  limit: {lim}")
    return "\n".join(out)
