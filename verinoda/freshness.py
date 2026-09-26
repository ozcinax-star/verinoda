"""Which files changed since the index was built (cheap enough to run on every read).

The index (graph and search index) describes the project's latest snapshot: the file list and
content hashes recorded by the last ``scan``/``update``. Reading commands (query, trace, the MCP
read tools) answer from it; when the working tree moved on they say so - "N files changed since
the index" with the list - instead of answering silently from an older tree.

The check costs one directory listing per folder that holds indexed files (the listing gives
each file's size and modification time) and the stat-cached hashes of ``atlas.db`` (``file_stat``,
:mod:`verinoda.snapshot`, read in the same query as the snapshot's files): a file whose size and
time match its cached row is not read; one that differs is hashed and compared with the
snapshot, so a file that was only touched is not reported. New files are those a listed folder
holds that the snapshot does not and that the snapshot's own rule (:func:`verinoda.snapshot.list_files`)
would list, so that ``verinoda update`` can always clear what is reported: in a git work tree git
must list the file (tracked, or untracked and not ignored: ``git ls-files`` with the paths as
literal pathspecs) and an untracked file under build/ or dist/ is build output; a folder that is
another repository (a nested clone, a submodule) is not looked into. Git is asked only about a
path not asked about before; the answers are kept in ``fresh_ignored.json`` beside the index, per
snapshot. A new folder git ignores is not walked (``git check-ignore``); one that is walked is
looked into up to :data:`MAX_NEW_FOLDER_FILES` files.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path

from verinoda.snapshot import _GIT_SKIP_DIRS, _SKIP_DIRS, RACY_MARGIN_NS, hash_files

SHOWN = 10                  # files named in a note (the full list stays in ``files``)
MAX_NEW_FOLDER_FILES = 200  # files looked at inside a folder the index does not know
IGNORED_NAME = "fresh_ignored.json"
_SKIP_SUFFIXES = (".pyc", ".pyo", ".swp", ".tmp", "~")


_MEMO: dict[str, tuple[tuple, str | None, dict]] = {}  # atlas.db -> (its stat, snapshot id, rows): a long-lived
                                                       # process (the MCP server) reads them again only after a write


def _db_key(db: Path) -> tuple:
    out = []
    for p in (db, Path(str(db) + "-wal")):
        try:
            st = p.stat()
            out.append((st.st_mtime_ns, st.st_size))
        except OSError:
            out.append(None)
    return tuple(out)


def _latest(db: Path) -> tuple[str | None, dict[str, tuple]]:
    """The latest snapshot id and, per file, ``(snapshot sha, cached size, mtime_ns, sha, recorded_at_ns)``."""
    key = _db_key(db)
    memo = _MEMO.get(str(db))
    if memo is not None and memo[0] == key:
        return memo[1], memo[2]
    snap, rows = _read_latest(db)
    if _db_key(db) == key:  # nothing wrote meanwhile
        if len(_MEMO) > 8:
            _MEMO.clear()
        _MEMO[str(db)] = (key, snap, rows)
    return snap, rows


def _read_latest(db: Path) -> tuple[str | None, dict[str, tuple]]:
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=0.5)
    try:
        row = conn.execute("SELECT id FROM snapshots ORDER BY created_at DESC, rowid DESC LIMIT 1").fetchone()
        if row is None:
            return None, {}
        has_stat = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='file_stat'").fetchone()
        if has_stat:
            rows = conn.execute("SELECT sf.path, sf.sha256, fs.size, fs.mtime_ns, fs.sha256, fs.recorded_at_ns "
                                "FROM snapshot_files sf LEFT JOIN file_stat fs ON fs.path = sf.path "
                                "WHERE sf.snapshot_id = ?", (row[0],))
        else:
            rows = conn.execute("SELECT path, sha256, NULL, NULL, NULL, NULL FROM snapshot_files "
                                "WHERE snapshot_id = ?", (row[0],))
        return row[0], {r[0]: r[1:] for r in rows}
    finally:
        conn.close()


def _git_ignored(repo: str, rels: list[str]) -> set[str] | None:
    """The paths among ``rels`` git ignores; None when git could not tell (not a work tree)."""
    try:
        r = subprocess.run(["git", "-C", repo, "check-ignore", "-z", "--stdin"],
                           input="\0".join(rels) + "\0", capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode not in (0, 1):  # 128: not a git work tree - nothing is known to be ignored
        return None
    return {p for p in r.stdout.split("\0") if p}


_ARGS_CHARS = 16_000  # paths per git call (a Windows command line holds 32,767 characters)
_MEMO_VERSION = 2     # 1 kept a new file git did not ignore, although the snapshot would not list it


def _git_listed(repo: str, rels: list[str]) -> dict[str, bool] | None:
    """Per path of ``rels``, would :func:`verinoda.snapshot.list_files` list it: git lists it (tracked, or
    untracked and not ignored; a file inside a nested repository or a submodule is not listed) and no
    folder of it is skipped (for an untracked path build/ and dist/ too, which are build output).
    None when git could not tell. Paths are literal pathspecs after ``--``, never options."""
    out: dict[str, bool] = {r: False for r in rels}
    i = 0
    while i < len(rels):
        chunk, size = [], 0
        while i < len(rels) and (not chunk or size + len(rels[i]) < _ARGS_CHARS):
            chunk.append(rels[i])
            size += len(rels[i]) + 3
            i += 1
        try:
            r = subprocess.run(["git", "-C", repo, "--literal-pathspecs", "ls-files", "-z", "-t", "--cached",
                                "--others", "--exclude-standard", "--", *chunk],
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
                               stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if r.returncode != 0:
            return None
        for entry in r.stdout.split("\0"):
            tag, _, rel = entry.partition(" ")
            if rel in out:
                out[rel] = not set(rel.split("/")) & (_SKIP_DIRS if tag == "?" else _GIT_SKIP_DIRS)
    return out


def _memo(repo: str, snap: str) -> tuple[Path, set[str], set[str]]:
    """``(file, left out, kept)``: what was asked about new paths since snapshot ``snap``."""
    from verinoda.paths import index_dir

    p = index_dir(Path(repo)) / IGNORED_NAME
    try:
        memo = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        memo = {}
    if not isinstance(memo, dict) or memo.get("snapshot") != snap or memo.get("v") != _MEMO_VERSION:
        memo = {"snapshot": snap, "ignored": [], "kept": []}
    return p, set(memo.get("ignored") or ()), set(memo.get("kept") or ())


def _save_memo(p: Path, snap: str, ignored: set[str], kept: set[str]) -> None:
    try:
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps({"v": _MEMO_VERSION, "snapshot": snap, "ignored": sorted(ignored),
                                   "kept": sorted(kept)}), encoding="utf-8")
        tmp.replace(p)
    except OSError:
        pass


def _ignored(repo: str, snap: str, rels: list[str]) -> set[str]:
    """The folders (``dir/``) among ``rels`` git ignores, remembered per snapshot (asked once per path):
    a new folder git ignores is not walked."""
    if not rels:
        return set()
    p, ignored, kept = _memo(repo, snap)
    ask = [r for r in rels if r not in ignored and r not in kept]
    if ask:
        got = _git_ignored(repo, ask)
        if got is None:
            return ignored & set(rels)
        ignored |= got
        kept |= set(ask) - got
        _save_memo(p, snap, ignored, kept)
    return ignored & set(rels)


def _new_listed(repo: str, snap: str, rels: list[str]) -> list[str]:
    """The new files among ``rels`` that the snapshot's own rule would list (:func:`_git_listed`,
    remembered per snapshot; git is asked even without a ``.git`` here, as ``list_files`` does for a
    project inside a larger repository; when git cannot tell, the folder rule of a tree that is not
    a git work tree): a file an update cannot add is never reported as changed."""
    if not rels:
        return []
    p, ignored, kept = _memo(repo, snap)
    ask = [r for r in rels if r not in ignored and r not in kept]
    if ask:
        got = _git_listed(repo, ask)
        if got is None:
            return sorted(r for r in rels if r in kept or (r not in ignored and not set(r.split("/")) & _SKIP_DIRS))
        kept |= {r for r, ok in got.items() if ok}
        ignored |= {r for r, ok in got.items() if not ok}
        _save_memo(p, snap, ignored, kept)
    return sorted(r for r in rels if r in kept)


def _own_repo(path: str) -> bool:
    """Is the folder at ``path`` another git repository (a nested clone, or a submodule's work tree,
    whose ``.git`` is a file)? The snapshot never lists its files."""
    return os.path.exists(os.path.join(path, ".git"))


def _skip(name: str, skip_dirs=_GIT_SKIP_DIRS) -> bool:
    return name in skip_dirs or name.endswith(_SKIP_SUFFIXES) or name.endswith(".egg-info")


def check(repo: Path, *, with_new: bool = True) -> dict:
    """``{"checked", "snapshot", "count", "files", "modified", "added", "removed", "seconds"}``.

    ``files``: every changed path (modified, then added, then removed), repository-relative.
    ``checked`` is False (with ``why``) when there is no snapshot to compare with; nothing is
    then claimed about freshness.
    """
    from verinoda.paths import db_path

    t0 = time.perf_counter()
    repo = Path(repo).resolve()
    root = str(repo)
    db = db_path(repo)
    if not db.is_file():
        return {"checked": False, "why": "no atlas.db: the project was not indexed", "count": 0, "files": []}
    try:
        snap, known = _latest(db)
    except sqlite3.Error as exc:
        return {"checked": False, "why": f"atlas.db unreadable ({type(exc).__name__})", "count": 0, "files": []}
    if snap is None:
        return {"checked": False, "why": "no snapshot yet", "count": 0, "files": []}
    # outside a git work tree the file list also skipped build output folders (snapshot.list_files)
    git_mode = os.path.exists(os.path.join(root, ".git"))
    skip_dirs = _GIT_SKIP_DIRS if git_mode else _SKIP_DIRS
    by_dir: dict[str, list[str]] = {}
    for rel in known:
        d, _, name = rel.rpartition("/")
        by_dir.setdefault(d, []).append(name)
    known_dirs = set(by_dir)
    for d in list(known_dirs):  # a folder with only sub-folders of files is known too
        while d:
            d = d.rpartition("/")[0]
            known_dirs.add(d)
    suspect: list[str] = []
    removed: list[str] = []
    new: list[str] = []
    new_dirs: list[str] = []
    for d in known_dirs:
        names = by_dir.get(d, ())
        try:
            with os.scandir(os.path.join(root, d) if d else root) as it:
                entries = {e.name: e for e in it}
        except OSError:
            removed += [f"{d}/{n}" if d else n for n in names]
            continue
        for n in names:
            rel = f"{d}/{n}" if d else n
            e = entries.get(n)
            try:
                st = e.stat() if e is not None else None
            except OSError:
                st = None
            if st is None:
                removed.append(rel)
                continue
            snap_sha, size, mtime_ns, sha, recorded = known[rel]
            # the cached hash stands only for an unchanged stat recorded well after the file was written
            if not (size == st.st_size and mtime_ns == st.st_mtime_ns and recorded is not None
                    and st.st_mtime_ns < recorded - RACY_MARGIN_NS and sha == snap_sha):
                suspect.append(rel)
        if not with_new:
            continue
        for n, e in entries.items():
            if _skip(n, skip_dirs) or n in names:
                continue
            rel = f"{d}/{n}" if d else n
            try:
                if e.is_dir():
                    if rel not in known_dirs and not _own_repo(e.path):
                        new_dirs.append(rel)
                elif e.is_file():
                    new.append(rel)
            except OSError:
                continue
    modified = []
    if suspect:
        now = hash_files(repo, sorted(suspect))
        modified = sorted(r for r in suspect if r in now and now[r] != known[r][0])
        removed += [r for r in suspect if r not in now]
    if new_dirs:
        ignored_dirs = _ignored(root, snap, sorted(d + "/" for d in new_dirs))
        for d in sorted(new_dirs):
            if d + "/" in ignored_dirs:
                continue
            n_files = 0
            for dirpath, dirnames, filenames in os.walk(os.path.join(root, d)):
                dirnames[:] = sorted(x for x in dirnames
                                     if not _skip(x, skip_dirs) and not _own_repo(os.path.join(dirpath, x)))
                base = os.path.relpath(dirpath, root).replace(os.sep, "/")
                for f in sorted(filenames):
                    if not _skip(f, skip_dirs):
                        new.append(f"{base}/{f}")
                        n_files += 1
                if n_files >= MAX_NEW_FOLDER_FILES:
                    break
    new = _new_listed(root, snap, sorted(new))
    files = modified + new + sorted(removed)
    return {"checked": True, "snapshot": snap, "count": len(files), "files": files,
            "modified": len(modified), "added": len(new), "removed": len(removed),
            "seconds": round(time.perf_counter() - t0, 4)}


def note(fresh: dict, *, shown: int = SHOWN) -> str | None:
    """One line for a reader: "N file(s) changed since the index (...): a, b, ..." (None when fresh)."""
    if not fresh.get("checked") or not fresh.get("count"):
        return None
    files = fresh["files"]
    more = len(files) - shown
    return (f"{fresh['count']} file(s) changed since the index (run `verinoda update`; what the index says "
            f"about them is the previous version): " + ", ".join(files[:shown])
            + (f", ... (+{more})" if more > 0 else ""))


def summary(fresh: dict, *, shown: int = 20) -> dict:
    """The JSON form for a result: ``{"stale_count", "stale_files"}`` (empty when fresh); a check that
    could not run says why (``index_freshness``) instead of claiming the index is current."""
    if not fresh.get("checked"):
        return {"index_freshness": f"not checked: {fresh.get('why')}"} if fresh.get("why") else {}
    if not fresh.get("count"):
        return {}
    return {"stale_count": fresh["count"], "stale_files": fresh["files"][:shown]}
