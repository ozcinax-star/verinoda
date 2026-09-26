"""Working-tree state identity: which code a run ran on (docs/DESIGN.md D34, cross-cutting 1).

A *tree* is the file set :mod:`verinoda.experiments` copies (tracked plus
untracked-not-ignored files, ``.verinoda`` excluded; see
:func:`verinoda.snapshot.list_files`) or the regular files of one commit.

* **Content id** of a file: sha256 of its bytes with CRLF turned into LF
  (:func:`content_id`), so a checkout with ``core.autocrlf`` and the committed
  blob have the same id. Two files with the same id have the same text.
* **Tree hash**: sha256 over the sorted ``(path, content id)`` pairs
  (:func:`tree_id`). Equal tree hashes mean the same files with the same
  content, line endings aside.
* **Changes vs a base commit** (:func:`changes_vs_base`): ``{path: content id,
  or None when the file is absent}`` for exactly the paths whose content
  differs from the base. With the base this determines the whole tree, so a
  run stores this small map instead of every file's hash.
* **Blobs**: the (LF-normalised) content of every changed file is kept once,
  by content id, under ``.verinoda/runs/blobs/``; any two recorded trees can
  then be diffed exactly (:func:`diff_trees`), and a unified patch vs the base
  is written next to the run (``runs/<id>/change.patch``).
* **Touched symbols**: each diff hunk is mapped to the definitions it changes
  with :func:`verinoda.anchors.enclosing` (old side for removed lines, new
  side for added ones).

Git is read with plumbing only: ``diff-index`` / ``diff-tree --name-only``
(the porcelain ``git diff`` would refresh and rewrite ``.git/index``),
``ls-files``, ``ls-tree`` and ``cat-file --batch`` (raw blobs: no smudge/clean
filters and no line-ending conversion run), always with
``--no-optional-locks``, so a read never writes ``.git/index``. A ref
given by a user or an agent is resolved with ``rev-parse --verify
--end-of-options`` and refused when it starts with ``-``; only the resulting
full commit id is passed on.

A project may sit in a subdirectory of its git repository: paths are then
relative to the project (``git rev-parse --show-prefix``), commit files are
those under the project's directory, and a commit copy holds only them.

Paths from history that name git's own directory in any spelling a file system
may fold to ``.git`` (``.GIT``, ``.git.``, ``git~1``, ignorable Unicode, also
between backslashes, which Windows reads as separators) or ``.verinoda`` are
never listed or written; on Windows neither are names Windows cannot hold
(``what?.md``, ``a:b``, ``NUL``, any name holding a backslash).
"""

from __future__ import annotations

import ast
import difflib
import hashlib
import os
import re
import subprocess
import threading
from pathlib import Path

from verinoda.snapshot import _GIT_SKIP_DIRS, _SKIP_DIRS, git, hash_files, list_files

# the ledger's test files ("test" on a changed file, the test_edited rule): the one rule of verinoda.testcode
from verinoda.testcode import is_test_file

LF_SCHEME = "lf1"          # file_facts scheme of the raw sha256 -> content id memo
TREE_PREFIX = b"verinoda-tree/1\n"
MAX_DIFF_LINES = 20_000    # per side; bigger files are reported changed without a line diff
EXACT_DIFF_LINES = 2_000   # up to this many lines per side the line diff is exact (no junk heuristic)
MAX_HUNK_LINES = 40        # added/removed lines kept per hunk in the recorded diff summary
MAX_HUNKS = 60             # hunks kept per file in the recorded diff summary
_SHA_RE = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
_REGULAR_MODES = ("100644", "100755")
_GIT_SAFE = ("--no-optional-locks", "-c", "core.fsmonitor=false", "-c", "core.quotepath=off")
# Characters file systems ignore when comparing names (HFS+), which git itself refuses around ".git".
_IGNORABLE = re.compile("[​-‏‪-‮⁠-⁤⁪-⁯﻿]")
_RESERVED_PARTS = {".git", "git~1", ".verinoda"}
_WIN_BAD_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')
# A backslash is part of one name for git, but Windows reads it as a directory separator: the tree entry
# ".\.git\config" is written as .git/config. Reserved names are looked for between either separator.
_ANY_SEP = re.compile(r"[/\\]")
_WIN_DEVICES = re.compile(r"^(con|prn|aux|nul|com[0-9¹²³]|lpt[0-9¹²³]|conin\$|conout\$)(\..*)?$", re.I)


class NotAGitTree(RuntimeError):
    """The project is not a git work tree with a commit to compare against."""


# -- identity -----------------------------------------------------------------------------

def normalise(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n") if b"\r" in data else data


def content_id(data: bytes) -> str:
    return hashlib.sha256(normalise(data)).hexdigest()


def tree_id(files: dict[str, str]) -> str:
    h = hashlib.sha256(TREE_PREFIX)
    for p, d in sorted(files.items()):
        h.update(p.encode("utf-8") + b"\0" + d.encode() + b"\n")
    return h.hexdigest()


def is_binary(data: bytes) -> bool:
    return b"\0" in data[:8192]


def copy_file(src: Path, dst: Path) -> str:
    """Copy ``src`` to ``dst`` (bytes unchanged, stat copied) and return the content id of what was copied.

    Streams in 1 MiB chunks, so hashing costs no second read of the file.
    """
    import shutil

    h = hashlib.sha256()
    carry = b""
    with open(src, "rb") as f, open(dst, "wb") as g:
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                break
            g.write(chunk)
            data = carry + chunk
            if data.endswith(b"\r"):
                carry, data = b"\r", data[:-1]
            else:
                carry = b""
            h.update(data.replace(b"\r\n", b"\n"))
    h.update(carry)
    try:
        shutil.copystat(src, dst)
    except OSError:
        pass
    return h.hexdigest()


def current(repo: Path, *, store=None) -> dict:
    """``{"hash", "files": {path: content id}, "count"}`` of the working tree now.

    Raw hashes come from the snapshot stat cache; the raw -> content id step is
    remembered in ``file_facts`` (scheme ``lf1``), so an unchanged file is not
    read again.
    """
    repo = Path(repo).resolve()
    raw = hash_files(repo, list_files(repo), store=store)
    memo: dict[str, str] = {}
    if store is not None and raw:
        wanted = sorted(set(raw.values()))
        for i in range(0, len(wanted), 500):
            chunk = wanted[i:i + 500]
            for r in store.all(f"SELECT sha256, facts FROM file_facts WHERE scheme = ? AND sha256 IN "
                               f"({','.join('?' * len(chunk))})", (LF_SCHEME, *chunk)):
                lf = (r.get("facts") or {}).get("lf") if isinstance(r.get("facts"), dict) else None
                if lf:
                    memo[r["sha256"]] = lf
    files: dict[str, str] = {}
    fresh: list[tuple[str, str]] = []
    for rel, sha in raw.items():
        if sha in memo:
            files[rel] = memo[sha]
            continue
        try:
            data = (repo / rel).read_bytes()
        except OSError:
            continue
        rsha = hashlib.sha256(data).hexdigest()
        files[rel] = memo[rsha] = content_id(data)
        fresh.append((rsha, files[rel]))
    if store is not None and fresh:
        import json

        from verinoda.store import now

        try:
            with store.tx() as conn:
                conn.executemany("INSERT OR IGNORE INTO file_facts (sha256, scheme, facts, created_at) "
                                 "VALUES (?, ?, ?, ?)",
                                 [(r, LF_SCHEME, json.dumps({"lf": c}), now()) for r, c in dict(fresh).items()])
        except Exception:  # noqa: BLE001 - the memo is an optimisation only
            pass
    return {"hash": tree_id(files), "files": files, "count": len(files)}


# -- git plumbing -----------------------------------------------------------------------------

def _git(repo: Path, *args: str, timeout: float = 120) -> str | None:
    return git(repo, *_GIT_SAFE, *args, timeout=timeout)


_PREFIXES: dict[str, str] = {}


def project_prefix(repo: Path) -> str:
    """The project's directory inside its git repository (``""`` at the top level, else ``"sub/dir/"``)."""
    raw = str(repo) if Path(repo).is_absolute() else None
    if raw is not None and raw in _PREFIXES:   # an absolute path as given: no file-system call to resolve it
        return _PREFIXES[raw]
    key = str(Path(repo).resolve())
    if key in _PREFIXES:
        if raw is not None:
            _PREFIXES[raw] = _PREFIXES[key]
        return _PREFIXES[key]
    out = _git(Path(repo), "rev-parse", "--show-prefix")
    pre = (out or "").strip().replace("\\", "/")
    pre = pre if (not pre or pre.endswith("/")) else pre + "/"
    if out is not None:  # not remembered when git could not answer (not a work tree yet)
        _PREFIXES[key] = pre
    return pre


def blob_spec(repo: Path, commit: str, rel: str) -> str:
    """``<commit>:<path>`` for a project-relative path (the git path carries the project's prefix)."""
    return f"{commit}:{project_prefix(repo)}{rel}"


def reserved_part(part: str) -> bool:
    """A path component a file system may treat as ``.git`` (or that is ``.verinoda``), in any spelling:
    case, trailing dots/spaces, NTFS short names and streams, Unicode that HFS+ ignores."""
    p = _IGNORABLE.sub("", part).split(":", 1)[0].rstrip(" .").lower()
    return p in _RESERVED_PARTS


def unwritable_here(rel: str) -> str | None:
    """Why ``rel`` cannot be written as a file on this operating system, else None."""
    if os.name != "nt":
        return None
    if "\\" in rel:
        return "a backslash in a name (Windows reads it as a directory separator)"
    for part in rel.split("/"):
        if _WIN_BAD_CHARS.search(part):
            return "a character Windows does not allow in file names"
        if _WIN_DEVICES.match(part):
            return "a Windows device name"
        if part != part.rstrip(" ."):
            return "a name ending in a dot or space (Windows drops it)"
    return None


def safe_path(rel: str) -> bool:
    """A repository-relative path Verinoda may list and write: no ``..``, no git/.verinoda directory (also
    not between backslashes, which Windows reads as separators)."""
    parts = rel.split("/")
    return (bool(rel) and not any(p in ("", ".", "..") for p in parts)
            and not any(reserved_part(p) for p in _ANY_SEP.split(rel)))


def index_symlinks(repo: Path) -> set[str]:
    """Project-relative paths the index records as symlinks (mode 120000); with ``core.symlinks=false`` (the
    Git for Windows default) such a path is checked out as a plain file holding the link target."""
    out = _git(Path(repo), "ls-files", "-s", "-z") or ""
    links = set()
    for rec in out.split("\0"):
        if rec.startswith("120000 ") and "\t" in rec:
            links.add(rec.split("\t", 1)[1])
    return links


def check_ref(ref: str) -> str:
    """``ref`` stripped, or ValueError when it could be read as an option or holds control characters."""
    r = (ref or "").strip()
    if not r:
        raise ValueError("an empty ref was given")
    if r.startswith("-"):
        raise ValueError(f"refusing ref {r!r}: a ref may not start with '-'")
    if any(ord(c) < 32 or c.isspace() for c in r):
        raise ValueError(f"refusing ref {r!r}: it contains whitespace or control characters")
    return r


def resolve_commit(repo: Path, ref: str) -> str:
    """The full commit id ``ref`` names in ``repo`` (``rev-parse --verify --end-of-options``)."""
    r = check_ref(ref)
    out = _git(Path(repo), "rev-parse", "--verify", "--quiet", "--end-of-options", f"{r}^{{commit}}")
    sha = (out or "").strip()
    if not _SHA_RE.match(sha):
        raise ValueError(f"{r!r} does not name a commit in {repo}")
    return sha


def head_commit(repo: Path) -> str | None:
    out = _git(Path(repo), "rev-parse", "--verify", "--quiet", "HEAD^{commit}")
    sha = (out or "").strip()
    return sha if _SHA_RE.match(sha) else None


def read_blobs(repo: Path, specs: list[str], *, timeout: float = 300) -> dict[str, bytes | None]:
    """Raw contents of ``<commit>:<path>`` or ``<blob id>`` specs from one ``git cat-file --batch``.

    No filters and no line-ending conversion run; a missing object maps to
    None. A spec holding a newline cannot be asked for and maps to None.
    """
    out: dict[str, bytes | None] = {s: None for s in specs}
    asked = [s for s in dict.fromkeys(specs) if "\n" not in s and "\r" not in s]
    if not asked:
        return out
    try:
        proc = subprocess.Popen(["git", "-C", str(repo), *_GIT_SAFE, "cat-file", "--batch"],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError:
        return out

    def feed() -> None:
        try:
            for s in asked:
                proc.stdin.write(s.encode("utf-8") + b"\n")  # type: ignore[union-attr]
            proc.stdin.close()  # type: ignore[union-attr]
        except (OSError, ValueError):
            pass

    t = threading.Thread(target=feed, daemon=True)
    t.start()
    timer = threading.Timer(timeout, proc.kill)
    timer.start()
    try:
        so = proc.stdout
        for s in asked:
            header = so.readline()  # type: ignore[union-attr]
            if not header:
                break
            parts = header.rstrip(b"\n").split(b" ")
            if len(parts) != 3 or parts[1] == b"missing" or not parts[2].isdigit():
                continue
            size = int(parts[2])
            data = so.read(size)  # type: ignore[union-attr]
            so.read(1)  # type: ignore[union-attr]
            out[s] = data if parts[1] == b"blob" else None
    finally:
        timer.cancel()
        try:
            proc.kill()
        except OSError:
            pass
        proc.wait()
        t.join(timeout=5)
    return out


def commit_entries(repo: Path, commit: str, skipped: list[dict] | None = None) -> list[tuple[str, str, str]]:
    """``(mode, blob id, path)`` of the regular files of ``commit`` under the project's directory, paths
    relative to the project (symlinks and submodules left out). Paths naming git's directory in any
    spelling, or ``.verinoda``, are never listed; names this OS cannot hold are left out and, when
    ``skipped`` is given, appended to it as ``{"path", "why"}``."""
    if not _SHA_RE.match(commit or ""):
        raise ValueError("commit_entries needs a full commit id (resolve it with resolve_commit)")
    out = _git(Path(repo), "ls-tree", "-r", "-z", "--full-tree", commit, timeout=300)
    if out is None:
        raise NotAGitTree(f"cannot list the files of commit {commit[:12]}")
    prefix = project_prefix(repo)
    ents = []
    for rec in out.split("\0"):
        if not rec or "\t" not in rec:
            continue
        meta, path = rec.split("\t", 1)
        mode, typ, oid = (meta.split(" ") + ["", "", ""])[:3]
        if typ != "blob" or mode not in _REGULAR_MODES:
            continue
        if prefix:
            if not path.startswith(prefix):
                continue
            path = path[len(prefix):]
        if not safe_path(path) or set(path.split("/")) & _GIT_SKIP_DIRS:
            continue
        why = unwritable_here(path)
        if why:
            if skipped is not None:
                skipped.append({"path": path, "why": why})
            continue
        ents.append((mode, oid, path))
    return ents


COMMIT_IDS_BATCH = 500   # blobs held in memory at once while computing a commit's content ids


def commit_files(repo: Path, commit: str) -> dict[str, str]:
    """``{path: content id}`` of a commit's regular files (reads every blob once, a batch at a time)."""
    ents = commit_entries(repo, commit)
    out: dict[str, str] = {}
    for i in range(0, len(ents), COMMIT_IDS_BATCH):
        batch = ents[i:i + COMMIT_IDS_BATCH]
        data = read_blobs(repo, [oid for _, oid, _ in batch])
        out.update({p: content_id(data[oid]) for _, oid, p in batch if data.get(oid) is not None})
    return out


# -- blobs ------------------------------------------------------------------------------------

def blobs_dir(repo: Path) -> Path:
    from verinoda.paths import runs_dir

    return runs_dir(repo) / "blobs"


def put_blob(repo: Path, data: bytes) -> str:
    """Store LF-normalised ``data`` under its content id (once) and return the id."""
    norm = normalise(data)
    cid = hashlib.sha256(norm).hexdigest()
    p = blobs_dir(repo) / cid[:2] / cid
    if not p.is_file():
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_bytes(norm)
        tmp.replace(p)
    return cid


def get_blob(repo: Path, cid: str) -> bytes | None:
    if not re.match(r"^[0-9a-f]{64}$", cid or ""):
        return None
    try:
        return (blobs_dir(repo) / cid[:2] / cid).read_bytes()
    except OSError:
        return None


# -- changes vs the base -----------------------------------------------------------------------

def _skipped(path: str, tracked: bool) -> bool:
    return bool(set(Path(path).parts) & (_GIT_SKIP_DIRS if tracked else _SKIP_DIRS))


def changes_vs_base(repo: Path, base: str, files: dict[str, str] | None = None) -> dict:
    """What differs between the working tree and commit ``base``.

    ``files`` is the ``{path: content id}`` map of the tree that ran (from the
    experiment copy); without it the working tree is read now. Returns
    ``{"tree_files": {path: content id | None}, "contents": {path: bytes}
    (LF-normalised new contents), "base": {path: bytes} (LF-normalised base
    contents of the changed paths), "drift": [paths whose file changed after
    the run]}``.
    """
    repo = Path(repo).resolve()
    if not _SHA_RE.match(base or ""):
        raise NotAGitTree("no base commit")
    # Plumbing diff-index, not `git diff`: the porcelain refreshes and rewrites .git/index for stat-dirty files
    # even with --no-optional-locks (git 2.53). A stat-dirty candidate is compared by content below.
    # --relative: paths relative to the project directory, and only the files under it.
    out = _git(repo, "diff-index", "--name-only", "-z", "--relative", "--no-renames", base, "--")
    if out is None:
        raise NotAGitTree(f"git diff against {base[:12]} failed")
    others = _git(repo, "ls-files", "-z", "--others", "--exclude-standard") or ""
    cands = {p for p in out.split("\0") if p and not _skipped(p, True) and safe_path(p)}
    cands |= {p for p in others.split("\0") if p and not _skipped(p, False) and safe_path(p)}
    base_raw = read_blobs(repo, [blob_spec(repo, base, p) for p in sorted(cands)])
    tree_files: dict[str, str | None] = {}
    contents: dict[str, bytes] = {}
    base_contents: dict[str, bytes] = {}
    drift: list[str] = []
    for p in sorted(cands):
        b = base_raw.get(blob_spec(repo, base, p))
        bnorm = normalise(b) if b is not None else None
        try:
            now_data = (repo / p).read_bytes() if (repo / p).is_file() else None
        except OSError:
            now_data = None
        nnorm = normalise(now_data) if now_data is not None else None
        ran = files.get(p) if files is not None else (hashlib.sha256(nnorm).hexdigest() if nnorm is not None
                                                       else None)
        if files is not None and p not in files:
            ran = None
        now_id = hashlib.sha256(nnorm).hexdigest() if nnorm is not None else None
        if ran != now_id:
            drift.append(p)
        base_id = hashlib.sha256(bnorm).hexdigest() if bnorm is not None else None
        if ran == base_id:
            continue
        tree_files[p] = ran
        if bnorm is not None:
            base_contents[p] = bnorm
        if ran is not None and ran == now_id and nnorm is not None:
            contents[p] = nnorm
    return {"tree_files": tree_files, "contents": contents, "base": base_contents, "drift": drift}


def base_ids(repo: Path, base: str) -> dict[str, str]:
    """``{path: content id}`` of commit ``base``, computed once and kept under ``runs/base-ids/``."""
    import json

    from verinoda.paths import runs_dir

    if not _SHA_RE.match(base or ""):
        raise NotAGitTree("no base commit")
    p = runs_dir(repo) / "base-ids" / f"{base}.json"
    prefix = project_prefix(repo)
    try:
        got = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(got, dict) and got.get("commit") == base and got.get("v") == 2 and \
                got.get("prefix") == prefix and isinstance(got.get("files"), dict):
            return got["files"]
    except (OSError, ValueError):
        pass
    files = commit_files(repo, base)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"v": 2, "commit": base, "prefix": prefix, "files": files}, sort_keys=True),
                   encoding="utf-8")
    tmp.replace(p)
    return files


def changes_from_ids(repo: Path, base: str, ids: dict[str, str], base_map: dict[str, str]) -> dict:
    """:func:`changes_vs_base` from content ids alone: no ``git diff``; base contents of the changed
    paths come from the blob store (or one ``git cat-file``, then stored)."""
    repo = Path(repo).resolve()
    # A symlink is copied as its target's content but is not a regular file of the commit: not a change.
    # With core.symlinks=false it is a plain file on disk, so the index mode is what tells.
    links: set[str] | None = None

    def symlink(p: str) -> bool:
        nonlocal links
        if (repo / p).is_symlink():
            return True
        if links is None:
            links = index_symlinks(repo)
        return p in links

    changed = sorted(p for p in set(ids) | set(base_map) if ids.get(p) != base_map.get(p)
                     and not (p not in base_map and symlink(p)))
    tree_files: dict[str, str | None] = {p: ids.get(p) for p in changed}
    base_contents: dict[str, bytes] = {}
    missing = []
    for p in changed:
        if p in base_map:
            data = get_blob(repo, base_map[p])
            if data is None:
                missing.append(p)
            else:
                base_contents[p] = data
    if missing:
        raw = read_blobs(repo, [blob_spec(repo, base, p) for p in missing])
        for p in missing:
            b = raw.get(blob_spec(repo, base, p))
            if b is not None:
                base_contents[p] = normalise(b)
                put_blob(repo, b)
    contents: dict[str, bytes] = {}
    drift: list[str] = []
    for p in changed:
        want = ids.get(p)
        if want is None:
            if (repo / p).is_file():
                drift.append(p)
            continue
        try:
            data = normalise((repo / p).read_bytes())
        except OSError:
            drift.append(p)
            continue
        if hashlib.sha256(data).hexdigest() == want:
            contents[p] = data
        else:
            drift.append(p)
    return {"tree_files": tree_files, "contents": contents, "base": base_contents, "drift": drift}


def changes_between_commits(repo: Path, base: str, commit: str) -> dict:
    """:func:`changes_vs_base` for the tree of another commit (a bisect or differential copy)."""
    repo = Path(repo).resolve()
    if base == commit:
        return {"tree_files": {}, "contents": {}, "base": {}, "drift": []}
    out = _git(repo, "diff-tree", "-r", "--name-only", "-z", "--relative", "--no-renames", base, commit, "--")
    if out is None:
        raise NotAGitTree(f"git diff {base[:12]} {commit[:12]} failed")
    cands = sorted({p for p in out.split("\0") if p and not _skipped(p, True) and safe_path(p)
                    and not unwritable_here(p)})
    raw = read_blobs(repo, [blob_spec(repo, base, p) for p in cands] + [blob_spec(repo, commit, p) for p in cands])
    tree_files: dict[str, str | None] = {}
    contents: dict[str, bytes] = {}
    base_contents: dict[str, bytes] = {}
    for p in cands:
        b, n = raw.get(blob_spec(repo, base, p)), raw.get(blob_spec(repo, commit, p))
        bn = normalise(b) if b is not None else None
        nn = normalise(n) if n is not None else None
        if bn == nn:
            continue
        tree_files[p] = hashlib.sha256(nn).hexdigest() if nn is not None else None
        if nn is not None:
            contents[p] = nn
        if bn is not None:
            base_contents[p] = bn
    return {"tree_files": tree_files, "contents": contents, "base": base_contents, "drift": []}


# -- diffs and touched symbols ---------------------------------------------------------------

def _lines(data: bytes | None) -> list[str]:
    return [] if data is None else data.decode("utf-8", "replace").split("\n")


def _facts(rel: str, data: bytes | None):
    if data is None or is_binary(data):
        return None
    from verinoda import anchors

    try:
        return anchors.compute_facts(rel, data)
    except Exception:  # noqa: BLE001 - symbol mapping is best effort; the diff stands without it
        return None


def symbol_at(facts, line: int) -> str | None:
    """The innermost definition containing ``line`` (``<module>`` for top-level statements), or None."""
    from verinoda import anchors

    enc = anchors.enclosing(facts, line, line) if facts else None
    if enc is None:
        return None
    return enc[1] if enc[0] == "sym" else ("<module>" if enc[0] == "mod" else f"#{enc[1]}")


def _symbols_for(facts, start: int, end: int) -> list[str]:
    out: list[str] = []
    for ln in range(start, end + 1):
        s = symbol_at(facts, ln)
        if s and s not in out:
            out.append(s)
    return out


def _docstring(node: ast.AST) -> ast.Expr | None:
    body = getattr(node, "body", None)
    if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and body and \
            isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) and \
            isinstance(body[0].value.value, str):
        return body[0]
    return None


def _strip_docstrings(tree: ast.AST) -> ast.AST:
    """Docstrings out, except those holding doctest examples (``>>>``): run with ``--doctest-modules`` they are
    tests, so a change to one is not "the same code"."""
    for node in ast.walk(tree):
        doc = _docstring(node)
        if doc is not None and ">>>" not in doc.value.value:
            node.body = node.body[1:]  # type: ignore[attr-defined]
    return tree


def _doctest_ranges(data: bytes | None) -> list[tuple[int, int]]:
    """Line ranges (1-based, inclusive) of the docstrings holding doctest examples (``>>>``)."""
    if data is None or b">>>" not in data or len(data) > 2_000_000:
        return []
    try:
        tree = ast.parse(data.decode("utf-8"))
    except (SyntaxError, ValueError, UnicodeDecodeError, RecursionError, MemoryError):
        return []
    out = []
    for node in ast.walk(tree):
        doc = _docstring(node)
        if doc is not None and ">>>" in doc.value.value:
            out.append((doc.lineno, getattr(doc, "end_lineno", None) or doc.lineno))
    return out


_INI_CONFIG = re.compile(r"(^|/)(pyproject\.toml|pytest\.ini|setup\.cfg|tox\.ini)$")
# a header starts its line (the elements of a multi-line array are indented) and names a key, not a list
_SECTION_LINE = re.compile(r"^\[\[?\s*([\w.\-:\"' ]+?)\s*\]\]?\s*(?:[#;].*)?$")


def _sections(lines: list[str]) -> list[str | None]:
    """The INI / TOML section each line is in (None before the first header)."""
    out: list[str | None] = []
    cur = None
    for ln in lines:
        m = _SECTION_LINE.match(ln)
        if m:
            cur = m.group(1).strip().strip("\"'")
        out.append(cur)
    return out


_FP_CACHE: dict[str, str | None] = {}


def code_fingerprint(rel: str, data: bytes | None) -> str | None:
    """A hash of what a Python file *does*: its syntax tree without comments, docstrings and formatting.
    None for other files, for content that does not parse, and for a missing file."""
    if data is None or not rel.endswith((".py", ".pyi")) or len(data) > 2_000_000:
        return None
    key = hashlib.sha256(data).hexdigest()
    if key in _FP_CACHE:
        return _FP_CACHE[key]
    try:
        tree = ast.parse(data.decode("utf-8"))
        dump = ast.dump(_strip_docstrings(tree), include_attributes=False)
        fp: str | None = hashlib.sha256(dump.encode("utf-8")).hexdigest()
    except (SyntaxError, ValueError, UnicodeDecodeError, RecursionError, MemoryError):
        fp = None
    if len(_FP_CACHE) > 4096:
        _FP_CACHE.clear()
    _FP_CACHE[key] = fp
    return fp


def code_tree_id(ch: dict, repo: Path | None = None) -> str:
    """Like :func:`tree_id`, but a Python file counts by its :func:`code_fingerprint`: two trees that
    differ only in comments, docstrings or formatting of Python files get the same id. ``ch`` is a
    ``changes_*`` record (``tree_files``, ``contents``, ``base``)."""
    entries: dict[str, str] = {}
    for p, cid in ch["tree_files"].items():
        if cid is None:
            entries[p] = "absent"
            continue
        new = ch["contents"].get(p)
        if new is None and repo is not None:
            new = get_blob(repo, cid)
        fp_new = code_fingerprint(p, new)
        base = ch["base"].get(p)
        if fp_new is not None and base is not None and fp_new == code_fingerprint(p, base):
            continue  # the same code as the base
        entries[p] = f"code:{fp_new}" if fp_new is not None else cid
    return tree_id(entries)


def diff_file(rel: str, old: bytes | None, new: bytes | None) -> dict:
    """One file's change: status, hunks (with the definitions each touches) and the symbols in total.

    ``no_code_change``: a Python file whose syntax tree is unchanged (only comments, docstrings or
    formatting differ). Up to ``EXACT_DIFF_LINES`` lines per side the line diff is exact; bigger files
    use difflib's junk heuristic (still a correct diff, possibly with larger hunks) so that a big
    generated file costs milliseconds, not seconds.
    """
    status = "added" if old is None else ("removed" if new is None else "modified")
    rec: dict = {"path": rel, "status": status, "test": is_test_file(rel), "symbols": [], "hunks": []}
    if (old is not None and is_binary(old)) or (new is not None and is_binary(new)):
        rec["binary"] = True
        return rec
    if status == "modified":
        fp_old = code_fingerprint(rel, old)
        if fp_old is not None and fp_old == code_fingerprint(rel, new):
            rec["no_code_change"] = True
    a, b = _lines(old), _lines(new)
    if len(a) > MAX_DIFF_LINES or len(b) > MAX_DIFF_LINES:
        rec["too_large"] = True
        return rec
    fa, fb = _facts(rel, old), _facts(rel, new)
    py = rel.endswith((".py", ".pyi"))
    # the lines of docstrings that hold doctest examples, per side (1-based)
    doc_old = {ln for lo, hi in (_doctest_ranges(old) if py else []) for ln in range(lo, hi + 1)}
    doc_new = {ln for lo, hi in (_doctest_ranges(new) if py else []) for ln in range(lo, hi + 1)}
    sec_new = _sections(b) if _INI_CONFIG.search(rel) else None
    exact = len(a) <= EXACT_DIFF_LINES and len(b) <= EXACT_DIFF_LINES
    sm = difflib.SequenceMatcher(None, a, b, autojunk=not exact)
    for group in sm.get_grouped_opcodes(0):
        i1, i2, j1, j2 = group[0][1], group[-1][2], group[0][3], group[-1][4]
        syms: list[str] = []
        if i2 > i1:
            syms += _symbols_for(fa, i1 + 1, i2)
        if j2 > j1:
            syms += [s for s in _symbols_for(fb, j1 + 1, j2) if s not in syms]
        elif fb is not None:  # a pure deletion: the definition that now holds the gap
            s = symbol_at(fb, max(1, j1))
            if s and s not in syms:
                syms.append(s)
        removed = a[i1:i2]
        added = b[j1:j2]
        if len(rec["hunks"]) < MAX_HUNKS:
            h = {"old": [i1 + 1, i2], "new": [j1 + 1, j2], "symbols": syms,
                 "removed": removed[:MAX_HUNK_LINES], "added": added[:MAX_HUNK_LINES],
                 **({"cut": True} if len(removed) > MAX_HUNK_LINES or len(added) > MAX_HUNK_LINES else {})}
            # removed / added lines inside a docstring that holds doctest examples (0-based within the hunk)
            dr = [k for k in range(i2 - i1) if i1 + 1 + k in doc_old] if doc_old else []
            da = [k for k in range(j2 - j1) if j1 + 1 + k in doc_new] if doc_new else []
            if dr or da:
                h["doctest"] = {"removed": dr[:MAX_HUNK_LINES], "added": da[:MAX_HUNK_LINES]}
            if sec_new is not None:
                h["added_sections"] = sec_new[j1:j2][:MAX_HUNK_LINES]
            rec["hunks"].append(h)
        for s in syms:
            if s not in rec["symbols"]:
                rec["symbols"].append(s)
    kinds = {}
    for s in rec["symbols"]:
        info = ((fb or {}).get("symbols") or {}).get(s) or ((fa or {}).get("symbols") or {}).get(s)
        kinds[s] = "module" if s == "<module>" else ((info or {}).get("kind") or "section")
    rec["kinds"] = kinds
    return rec


def unified_patch(changes: dict[str, tuple[bytes | None, bytes | None]]) -> str:
    """A unified diff (``a/`` = old, ``b/`` = new) of ``{path: (old, new)}``, LF-normalised."""
    parts: list[str] = []
    for rel in sorted(changes):
        old, new = changes[rel]
        if (old is not None and is_binary(old)) or (new is not None and is_binary(new)):
            parts.append(f"Binary files a/{rel} and b/{rel} differ")
            continue
        a, b = _lines(old), _lines(new)
        if len(a) > MAX_DIFF_LINES or len(b) > MAX_DIFF_LINES:
            parts.append(f"# {rel}: changed; too large for a line diff")
            continue
        parts.extend(difflib.unified_diff(a, b, fromfile="/dev/null" if old is None else f"a/{rel}",
                                          tofile="/dev/null" if new is None else f"b/{rel}", lineterm="", n=3))
    return "\n".join(parts) + ("\n" if parts else "")


def content_reader(repo: Path, base: str | None, tree_files: dict[str, str | None]):
    """A function ``path -> LF-normalised bytes | None`` for a recorded tree (blobs, else the base commit)."""
    cache: dict[str, bytes | None] = {}

    def read(rel: str) -> bytes | None:
        if rel in cache:
            return cache[rel]
        if rel in tree_files:
            cid = tree_files[rel]
            data = get_blob(repo, cid) if cid else None
        elif base:
            spec = blob_spec(repo, base, rel)
            raw = read_blobs(repo, [spec]).get(spec)
            data = normalise(raw) if raw is not None else None
        else:
            data = None
        cache[rel] = data
        return data
    return read


def diff_trees(repo: Path, base: str | None, old_files: dict[str, str | None],
               new_files: dict[str, str | None], base_map: dict[str, str] | None = None) -> list[dict]:
    """:func:`diff_file` for every path whose content differs between two recorded trees.

    Both trees are given as changes vs the same ``base`` (``tree_files``); a
    path missing from a map has its base content (from the blob store when
    ``base_map`` names its content id, else from git). A changed file whose
    content is not in the blob store is listed with ``content_unknown``.
    """
    paths = sorted(set(old_files) | set(new_files))
    changed = [p for p in paths if old_files.get(p, "<base>") != new_files.get(p, "<base>")]
    base_needed = [p for p in changed if p not in old_files or p not in new_files]
    base_raw: dict[str, bytes | None] = {}
    for p in list(base_needed):
        cid = (base_map or {}).get(p)
        data = get_blob(repo, cid) if cid else None
        if data is not None:
            base_raw[p] = data
            base_needed.remove(p)
    if base and base_needed:
        got = read_blobs(repo, [blob_spec(repo, base, p) for p in base_needed])
        base_raw.update({p: got.get(blob_spec(repo, base, p)) for p in base_needed})

    def side(files: dict[str, str | None], p: str) -> tuple[bytes | None, bool]:
        if p in files:
            cid = files[p]
            if cid is None:
                return None, True
            data = get_blob(repo, cid)
            return data, data is not None
        raw = base_raw.get(p)
        return (normalise(raw) if raw is not None else None), True

    out = []
    for p in changed:
        old, ok_old = side(old_files, p)
        new, ok_new = side(new_files, p)
        if not (ok_old and ok_new):
            out.append({"path": p, "status": "modified", "test": is_test_file(p), "symbols": [], "hunks": [],
                        "content_unknown": True})
            continue
        out.append(diff_file(p, old, new))
    return out
