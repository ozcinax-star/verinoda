"""Project snapshots: which commit and which file contents an analysis saw.

A snapshot is (commit, dirty flag, per-file sha256). Claims are pinned to a
snapshot; :func:`changed_files` between two snapshots drives stale
invalidation in :mod:`repoatlas.claims`.

Freshness checks are cheap (docs/DESIGN.md D21):

* File hashes are cached by stat in ``atlas.db`` (table ``file_stat``: size,
  mtime_ns, sha256 and when the row was recorded). A cached hash is reused
  only when size and mtime are unchanged **and** the mtime is older than the
  recording time minus :data:`RACY_MARGIN_NS` - git's racy-clean rule: a file
  written in the same timestamp tick as it was hashed could change again
  without its stat changing, so it is re-hashed until it is old enough.
  ``use_cache=False`` hashes everything.
* Commit, branch and dirty flag come from one ``git status --porcelain=v2
  --branch`` call (it was three git processes), and the file list from one
  ``git ls-files``.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
import time
from pathlib import Path

from repoatlas.store import Store, new_id, now

_SKIP_DIRS = {
    ".git", ".repoatlas", "graphify-out", ".venv", "venv", "node_modules", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", "dist", "build", ".idea", ".vscode",
}
# A cached hash is trusted only for files last modified this long before it was recorded
# (covers coarse mtime granularity: FAT 2 s, network shares, clock ticks).
RACY_MARGIN_NS = 2_000_000_000


def git(repo: Path, *args: str, check: bool = False, timeout: float = 60) -> str | None:
    """Run git and return stdout, or None when git/repo is unavailable.

    stdin is never inherited: under the MCP server RepoAtlas's stdin is the
    protocol pipe, and on Windows a git child holding it can hang.
    """
    try:
        r = subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        if check:
            raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
        return None
    return r.stdout


def _git_info_legacy(repo: Path) -> dict:
    head = git(repo, "rev-parse", "HEAD")
    if head is None:
        return {"commit": None, "branch": None, "dirty": False, "is_git": False}
    branch = (git(repo, "rev-parse", "--abbrev-ref", "HEAD") or "").strip() or None
    status = git(repo, "status", "--porcelain", "--untracked-files=normal") or ""
    dirty = any(line and ".repoatlas" not in line for line in status.splitlines())
    return {"commit": head.strip(), "branch": branch, "dirty": dirty, "is_git": True}


def git_info(repo: Path) -> dict:
    """Commit, branch (``HEAD`` when detached) and dirty flag from one ``git status``.

    A directory that is not a git work tree, or a repository without a commit,
    reports ``is_git: False`` (as before). Changes under ``.repoatlas`` never
    make the tree dirty.
    """
    out = git(repo, "status", "--porcelain=v2", "--branch", "--untracked-files=normal")
    if out is None:
        return {"commit": None, "branch": None, "dirty": False, "is_git": False}
    oid = head = None
    dirty = False
    for line in out.splitlines():
        if line.startswith("# branch.oid "):
            oid = line[len("# branch.oid "):].strip()
        elif line.startswith("# branch.head "):
            head = line[len("# branch.head "):].strip()
        elif line and not line.startswith("#") and ".repoatlas" not in line:
            dirty = True
    if oid is None or head is None:  # a git without porcelain v2 headers
        return _git_info_legacy(repo)
    if oid == "(initial)":
        return {"commit": None, "branch": None, "dirty": False, "is_git": False}
    return {"commit": oid, "branch": "HEAD" if head == "(detached)" else head, "dirty": dirty, "is_git": True}


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def list_files(repo: Path) -> list[str]:
    """Repo-relative POSIX paths of tracked + untracked-not-ignored files."""
    repo = Path(repo).resolve()
    out = git(repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    if out is not None:
        rels = [r for r in out.split("\0") if r]
        return sorted(r for r in rels if not set(Path(r).parts) & _SKIP_DIRS and (repo / r).is_file())
    rels = []
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.endswith(".egg-info")]
        for fn in filenames:
            rels.append((Path(dirpath) / fn).relative_to(repo).as_posix())
    return sorted(rels)


class _StatCache:
    """Rows of ``atlas.db``'s ``file_stat`` table (best effort: any SQLite error means "no cache").

    A separate short-timeout connection is used so a caller's open transaction
    is never committed or blocked on.
    """

    def __init__(self, db: Path):
        self.db = db
        self.rows: dict[str, tuple[int, int, str, int]] = {}
        self.ok = False
        try:
            conn = sqlite3.connect(str(db), timeout=0.5)
            try:
                if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='file_stat'").fetchone():
                    self.rows = {r[0]: tuple(r[1:]) for r in conn.execute(
                        "SELECT path, size, mtime_ns, sha256, recorded_at_ns FROM file_stat")}
                    self.ok = True
            finally:
                conn.close()
        except sqlite3.Error:
            self.ok = False

    @classmethod
    def for_repo(cls, repo: Path, store: Store | None) -> "_StatCache | None":
        path = Path(store.path) if store is not None and store.path != ":memory:" else None
        if path is None:
            from repoatlas.paths import db_path

            path = db_path(repo)
        if not path.is_file():
            return None
        cache = cls(path)
        return cache if cache.ok else None

    def fresh(self, rel: str, st: os.stat_result) -> str | None:
        row = self.rows.get(rel)
        if row is None:
            return None
        size, mtime_ns, sha, recorded_ns = row
        if size == st.st_size and mtime_ns == st.st_mtime_ns and st.st_mtime_ns < recorded_ns - RACY_MARGIN_NS:
            return sha
        return None

    def put(self, rows: list[tuple[str, int, int, str, int]]) -> None:
        try:
            conn = sqlite3.connect(str(self.db), timeout=0.5)
            try:
                conn.executemany(
                    "INSERT INTO file_stat (path, size, mtime_ns, sha256, recorded_at_ns) VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(path) DO UPDATE SET size = excluded.size, mtime_ns = excluded.mtime_ns, "
                    "sha256 = excluded.sha256, recorded_at_ns = excluded.recorded_at_ns", rows)
                conn.commit()
            finally:
                conn.close()
        except sqlite3.Error:
            pass  # a busy or read-only database only costs re-hashing next time


def hash_files(repo: Path, rels: list[str] | None = None, *, store: Store | None = None,
               use_cache: bool = True) -> dict[str, str]:
    """sha256 of each file (all listed files by default), reusing stat-cached hashes.

    The cache lives in the project's ``atlas.db`` (``store`` or the one under
    ``.repoatlas``); without one every file is hashed. See the module docstring
    for when a cached hash is trusted.
    """
    repo = Path(repo).resolve()
    rels = list_files(repo) if rels is None else rels
    cache = _StatCache.for_repo(repo, store) if use_cache else None
    out: dict[str, str] = {}
    fresh_rows: list[tuple[str, int, int, str, int]] = []
    for r in rels:
        p = repo / r
        try:
            st = p.stat()
        except OSError:
            continue
        hit = cache.fresh(r, st) if cache else None
        if hit is not None:
            out[r] = hit
            continue
        try:
            out[r] = sha256_file(p)
        except OSError:
            continue
        fresh_rows.append((r, st.st_size, st.st_mtime_ns, out[r], time.time_ns()))
    if cache and fresh_rows:
        cache.put(fresh_rows)
    return out


def tree_hash(files: dict[str, str]) -> str:
    h = hashlib.sha256()
    for p, d in sorted(files.items()):
        h.update(p.encode("utf-8") + b"\0" + d.encode() + b"\n")
    return h.hexdigest()


def take_snapshot(store: Store, repo: Path, *, graph_stats: dict | None = None) -> dict:
    repo = Path(repo).resolve()
    info = git_info(repo)
    files = hash_files(repo, store=store)
    th = tree_hash(files)
    prev = store.latest_snapshot()
    if prev and prev["tree_hash"] == th and prev["commit_sha"] == info["commit"] and not graph_stats:
        return prev
    snap = {
        "id": new_id("snp"),
        "repo_root": str(repo),
        "project": repo.name,
        "commit_sha": info["commit"],
        "branch": info["branch"],
        "dirty": int(info["dirty"]),
        "tree_hash": th,
        "file_count": len(files),
        "graph_path": (graph_stats or {}).get("graph_path"),
        "graph_nodes": (graph_stats or {}).get("nodes"),
        "graph_edges": (graph_stats or {}).get("edges"),
        "created_at": now(),
    }
    store.add_snapshot(snap, files)
    return snap


def changed_files(old: dict[str, str], new: dict[str, str]) -> dict[str, list[str]]:
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    modified = sorted(p for p in set(old) & set(new) if old[p] != new[p])
    return {"added": added, "removed": removed, "modified": modified}


def current_state(repo: Path, *, store: Store | None = None) -> dict:
    """Working-tree state without writing a snapshot."""
    info = git_info(repo)
    files = hash_files(repo, store=store)
    return {**info, "files": files, "tree_hash": tree_hash(files)}
