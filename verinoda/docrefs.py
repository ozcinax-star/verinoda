"""Code references in the repository's own documents, checked against the tree (``verinoda docs check``).

A README or a design note says "see ``verinoda/history.py``" or links ``[the loader](src/load.py#L40-L52)``; the
code moves on and the sentence points at nothing, or at other lines. This module finds such references in the
tracked Markdown, reStructuredText, AsciiDoc and text files and checks each against the working tree:

- **References** are Markdown link targets (relative to the document's folder, or to the repository root when
  they start with ``/``) and inline code spans that read as a path (a ``/`` or a known source extension, no
  spaces, no URL, no glob or placeholder), with an optional line or range: ``path:12``, ``path:12-30``,
  ``path#L12-L30``. A code span is tried from the repository root, then from the document's folder.
- **Checked**: the path exists (a file or a folder) and the cited lines are inside the file. A path whose first
  folder is not in the repository (an example from another project, ``orders/api.py`` in a tutorial) and a path
  git ignores (a runtime file such as ``.verinoda/config.json``) are not checked, and counted as such.
- **Drift**: for a reference with lines, the file as it was at the document's last commit is compared with the
  working tree; the lines are carried over with the same text diff the hotspots view uses. Lines that only moved
  are ``moved`` (fixable: the new numbers); lines that changed or went are ``changed`` (flagged, never fixed).
- **Renames**: a missing path that git recorded as renamed (``git log -M --diff-filter=R``, followed to the last
  name) to a path that exists is ``renamed`` (fixable).

``--fix`` rewrites only those two kinds, in place, the reference's own characters and nothing else; everything
else stays for a person. Each finding cites the document line. Git is only read.
"""
from __future__ import annotations

import re
from pathlib import Path

from verinoda.snapshot import git, list_files

DOC_EXT = (".md", ".markdown", ".rst", ".adoc")
CODE_EXT = frozenset(("py", "pyi", "js", "jsx", "ts", "tsx", "mjs", "cjs", "java", "kt", "kts", "go", "rs", "c",
                      "h", "cc", "cpp", "hpp", "cs", "rb", "php", "swift", "scala", "sh", "ps1", "sql", "toml",
                      "json", "yaml", "yml", "cfg", "ini", "xml", "gradle", "md", "rst", "txt", "html", "css",
                      "mcfunction", "glsl", "vsh", "fsh", "lua", "r", "jl", "dart", "vue", "svelte"))
_LINK = re.compile(r"\]\(\s*<?([^()\s<>]+)>?(?:\s+\"[^\"\n]*\")?\s*\)")
_CODE = re.compile(r"(`+)([^`\n]+?)\1(?!`)")
_REF = re.compile(r"^(?P<path>[^\s:#<>{}*?|\"'()\[\]]+?)(?::(?P<a>\d+)(?:-(?P<b>\d+))?)?"
                  r"(?:#L(?P<la>\d+)(?:-L?(?P<lb>\d+))?)?$")
MAX_REFS = 5000
FOREIGN = "not a path of this repository (its first folder is not here)"


def _path_like(p: str, from_link: bool) -> bool:
    """A code span or link target that reads as a repository path (not a URL, a flag, a glob or a placeholder);
    a code span needs a ``/`` and a file extension, or a trailing ``/`` (a folder)."""
    if not p or "://" in p or p.startswith(("-", "$", "~", "#", "mailto:", "@")) or             any(ch in p for ch in "*?<>{}|=,;"):
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


def references(text: str) -> list[dict]:
    """Every reference in one document's text: ``{line, start, end, raw, path, a, b, kind}`` (``start``/``end``
    are the columns of ``raw`` on its line)."""
    out: list[dict] = []
    fence = None
    for n, line in enumerate(text.split("\n"), 1):
        s = line.lstrip()
        if s.startswith(("```", "~~~")):
            fence = None if fence == s[:3] else (fence or s[:3])
            continue
        if fence:
            continue
        spans = [(m.start(1), m.end(1), m.group(1), "link") for m in _LINK.finditer(line)]
        spans += [(m.start(2), m.end(2), m.group(2).strip(), "code") for m in _CODE.finditer(line)]
        for start, end, raw, kind in spans:
            ref = raw.split("#", 1)[0] if kind == "link" and "#L" not in raw else raw
            m = _REF.match(ref.replace("\\", "/"))
            if not m or not _path_like(m.group("path"), kind == "link"):
                continue
            a = m.group("a") or m.group("la")
            b = m.group("b") or m.group("lb")
            out.append({"line": n, "start": start, "end": end, "raw": raw, "path": m.group("path").replace("\\", "/"),
                        "a": int(a) if a else None, "b": int(b) if b else (int(a) if a else None), "kind": kind})
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


class _Tree:
    def __init__(self, repo: Path):
        self.repo = repo
        self.files = set(list_files(repo))
        self.dirs = {"/".join(f.split("/")[:k]) for f in self.files for k in range(1, f.count("/") + 1)}
        self.tops = {f.split("/", 1)[0] for f in self.files}
        self._renames: dict[str, str] | None = None
        self._lines: dict[str, list[str]] = {}
        self._at: dict[tuple[str, str], str | None] = {}
        self._doc_commit: dict[str, str | None] = {}
        self._ignored: set[str] = set()

    def exists(self, rel: str) -> bool:
        return rel in self.files or rel in self.dirs or (self.repo / rel).exists()

    def lines(self, rel: str) -> list[str]:
        if rel not in self._lines:
            try:
                t = (self.repo / rel).read_bytes().decode("utf-8", errors="replace").replace("\r\n", "\n")
            except OSError:
                t = ""
            self._lines[rel] = t.split("\n")[:-1] if t.endswith("\n") else t.split("\n")
        return self._lines[rel]

    def ignored(self, rel: str) -> bool:
        return rel in self._ignored

    def load_ignored(self, rels: set[str]) -> None:
        """Which of ``rels`` git ignores, in one ``git check-ignore --stdin`` call."""
        if not rels:
            self._ignored = set()
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
            out = git(self.repo, "log", "-M", "--diff-filter=R", "--name-status", "--format=", "--reverse",
                      "--relative", timeout=120) or ""
            rn: dict[str, str] = {}
            for ln in out.splitlines():
                parts = ln.split("\t")
                if len(parts) == 3 and parts[0].startswith("R"):
                    rn[parts[1]] = parts[2]
            self._renames = rn
        return self._renames

    def renamed_to(self, rel: str) -> str | None:
        seen, cur = set(), rel
        rn = self.renames()
        while cur in rn and cur not in seen:
            seen.add(cur)
            cur = rn[cur]
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


def _resolve(tree: _Tree, doc: str, ref: dict) -> tuple[str | None, str | None]:
    """(repository path, None) for a reference that names a path of this repository (existing or not), or
    (None, why it is not checked). A link is read from the document's folder (``/``: the root); a code span from
    the root, the document's folder, then each folder above it. A path that exists nowhere belongs to this
    repository only when its first folder exists under the base it was read from."""
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
        return None, "outside the repository"
    for _, c in cands:
        if tree.exists(c):
            return c, None
    if ref["kind"] == "link":   # a link names a file of this repository, relative to its document
        return cands[0][1], None
    first = (_norm(p) or "").split("/", 1)[0]
    for base, c in cands:
        if first and tree.exists(f"{base}/{first}" if base else first):
            return c, None
    return cands[0][1], FOREIGN


def check(repo: Path, docs: list[str] | None = None, *, exclude: list[str] | None = None) -> dict:
    """Check every reference in ``docs`` (default: the tracked documents), leaving out documents that match a
    glob of ``exclude`` (``vendor/**``)."""
    from verinoda.guards import glob_match

    repo = Path(repo).resolve()
    tree = _Tree(repo)
    all_docs = sorted(f for f in tree.files if f.lower().endswith(DOC_EXT))
    if docs:
        want = {d.replace("\\", "/").strip("/") for d in docs}
        all_docs = [d for d in all_docs if d in want or any(d.startswith(w + "/") for w in want)]
    skipped = [d for d in all_docs if any(glob_match(d, g) for g in exclude or ())]
    all_docs = [d for d in all_docs if d not in set(skipped)]
    found: dict[str, list[dict]] = {k: [] for k in ("broken", "renamed", "moved", "changed")}
    unchecked: dict[str, int] = {}

    def skip(why: str) -> None:
        unchecked[why] = unchecked.get(why, 0) + 1

    pending: list[tuple[str, dict, str]] = []
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
            rel, why = _resolve(tree, doc, ref)
            if rel is None:
                skip(why)
            else:
                pending.append((doc, ref, rel, why))
    tree.load_ignored({rel for _, _, rel, _ in pending if not tree.exists(rel)})
    ok = 0
    for doc, ref, rel, why in pending:
        if why == FOREIGN:   # a path git ignores is this repository's runtime file, not another project's
            skip("a path git ignores (a runtime or generated file)" if tree.ignored(rel) else FOREIGN)
            continue
        item = {"doc": doc, "at": f"{doc}:{ref['line']}", "ref": ref["raw"], "path": rel, "kind": ref["kind"],
                "start": ref["start"], "end": ref["end"]}
        if not tree.exists(rel):
            if tree.ignored(rel):
                skip("a path git ignores (a runtime or generated file)")
                continue
            new = tree.renamed_to(rel)
            if new:
                found["renamed"].append({**item, "to": new, "status": "statically_verified",
                                         "why": f"{rel} was renamed to {new} (git log -M)",
                                         "fix": ref["raw"].replace(ref["path"], _relink(ref, doc, new), 1)})
            else:
                found["broken"].append({**item, "status": "statically_verified",
                                        "why": f"{rel} does not exist in the working tree"})
            continue
        if ref["a"] is None or rel not in tree.files:
            ok += 1
            continue
        a, b = ref["a"], max(ref["a"], ref["b"] or ref["a"])
        n = len(tree.lines(rel))
        drift = _drift(tree, doc, rel, a, b)
        if drift is None and b > n:
            found["broken"].append({**item, "status": "statically_verified",
                                    "why": f"{rel} has {n} lines; the reference cites {a}-{b}"})
        elif drift is None:
            ok += 1
        elif drift[0] == "moved":
            na, nb = drift[1]
            found["moved"].append({**item, "status": "statically_verified", "lines": [a, b], "to": [na, nb],
                                   "why": f"{rel}:{a}-{b} moved to {na}-{nb} since {doc} was last committed",
                                   "fix": _renumber(ref, na, nb)})
        else:
            found["changed"].append({**item, "status": "statically_verified", "lines": [a, b], "why": drift[1]})
    failing = sum(len(v) for v in found.values())
    return {"docs": len(all_docs), "references": n_refs, "ok": ok, **found, "unchecked": unchecked,
            **({"excluded_docs": len(skipped)} if skipped else {}), "truncated": n_refs > MAX_REFS,
            "method": "Markdown link targets and inline code spans that read as paths, checked against the working "
                      "tree; lines compared with the file at the document's last commit (text diff); renames from "
                      "git log -M",
            "limits": ["symbols named without a path (`Class.method`) are not checked",
                       "a path whose first folder is not under the root, the document's folder or a folder above it "
                       "is taken for another project's and not checked",
                       "a document written for another location (a template copied elsewhere at install time) is "
                       "read where it is; exclude it with --exclude",
                       "drift needs the document and the file in git history: an uncommitted document's line "
                       "references are only checked against the file's length"],
            "exit": 1 if failing else 0}


def _drift(tree: _Tree, doc: str, rel: str, a: int, b: int):
    """None when the cited lines are as they were at the document's last commit (or that cannot be read);
    ("moved", (a', b')) when they only moved; ("changed", why) when they changed or went."""
    sha = tree.doc_commit(doc)
    if not sha:
        return None
    old = tree.text_at(sha, rel)
    if old is None:
        return None
    cur = "\n".join(tree.lines(rel)) + "\n"
    if old == cur or old.rstrip("\n") == cur.rstrip("\n"):
        return None
    from verinoda.hotspots import _line_map

    old_lines = old.split("\n")
    if b > len(old_lines):
        return None
    m = _line_map(old, cur)
    new = [m[i - 1] for i in range(a, b + 1)]
    cur_lines = tree.lines(rel)
    same = all(x and old_lines[i - 1] == cur_lines[x - 1] for i, x in zip(range(a, b + 1), new))
    if same and new == list(range(new[0], new[0] + len(new))):
        return None if new[0] == a else ("moved", (new[0], new[-1]))
    gone = sum(1 for x in new if not x)
    return ("changed", f"{rel}:{a}-{b} changed since {doc} was last committed ({sha[:10]}): "
                       f"{gone} of {b - a + 1} line(s) gone, the rest edited or moved apart")


def _relink(ref: dict, doc: str, new: str) -> str:
    """``new`` (a repository path) written the way the reference wrote its path."""
    p = ref["path"]
    if ref["kind"] == "link" and not p.startswith("/"):
        folder = doc.rsplit("/", 1)[0] if "/" in doc else ""
        depth = len([s for s in folder.split("/") if s])
        return "../" * depth + new if depth else new
    return ("/" + new) if p.startswith("/") else new


def _renumber(ref: dict, a: int, b: int) -> str:
    raw = ref["raw"]
    if "#L" in raw:
        head = raw.split("#L", 1)[0]
        return f"{head}#L{a}" + (f"-L{b}" if b != a else "")
    head = raw.rsplit(":", 1)[0] if re.search(r":\d+(-\d+)?$", raw) else raw
    return f"{head}:{a}" + (f"-{b}" if b != a else "")


def fix(repo: Path, res: dict) -> list[dict]:
    """Rewrite the ``renamed`` and ``moved`` references in place (their own characters only), last column first
    per line; a document that changed since the check is left alone."""
    repo = Path(repo).resolve()
    by_doc: dict[str, list[dict]] = {}
    for f in [*res.get("renamed", []), *res.get("moved", [])]:
        by_doc.setdefault(f["doc"], []).append(f)
    done = []
    for doc, items in by_doc.items():
        p = repo / doc
        data = p.read_bytes()
        text = data.decode("utf-8")
        crlf = "\r\n" in text
        lines = text.replace("\r\n", "\n").split("\n")
        for f in sorted(items, key=lambda f: (int(f["at"].rsplit(":", 1)[1]), -f["start"])):
            n = int(f["at"].rsplit(":", 1)[1])
            ln = lines[n - 1]
            if ln[f["start"]:f["end"]] != f["ref"]:
                continue
            lines[n - 1] = ln[:f["start"]] + f["fix"] + ln[f["end"]:]
            done.append({"at": f["at"], "from": f["ref"], "to": f["fix"]})
        out = "\n".join(lines)
        p.write_bytes((out.replace("\n", "\r\n") if crlf else out).encode("utf-8"))
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
    return "\n".join(out)
