"""Stat-based hash cache (racy-clean rule) and fewer git processes for freshness checks."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import shutil  # noqa: E402
import sqlite3  # noqa: E402
import subprocess  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import snapshot, workflow  # noqa: E402
from verinoda.paths import db_path  # noqa: E402
from verinoda.store import open_store  # noqa: E402

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args],
                          cwd=cwd, check=True, capture_output=True, text=True).stdout


def _repo(tmp_path: Path, name: str = "r", git: bool = True) -> Path:
    repo = tmp_path / name
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "a.py").write_bytes(b"A = 1\n")
    (repo / "pkg" / "b.py").write_bytes(b"B = 2\n")
    if git:
        _git(repo, "init", "-q")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "init")
    workflow.init(repo)
    return repo


def _age(p: Path, seconds: float = 60) -> None:
    """Set a file's mtime well before now (outside the racy window)."""
    t = time.time_ns() - int(seconds * 1e9)
    os.utime(p, ns=(t, t))


def _count_hashing(monkeypatch) -> list[str]:
    seen: list[str] = []
    real = snapshot.sha256_file

    def counting(p):
        seen.append(Path(p).name)
        return real(p)

    monkeypatch.setattr(snapshot, "sha256_file", counting)
    return seen


def test_unchanged_old_files_are_not_rehashed(tmp_path, monkeypatch):
    repo = _repo(tmp_path, git=False)
    for f in ("a.py", "b.py"):
        _age(repo / "pkg" / f)
    seen = _count_hashing(monkeypatch)
    first = snapshot.hash_files(repo)
    assert {"a.py", "b.py"} <= set(seen)
    conn = sqlite3.connect(db_path(repo))
    rows = dict(conn.execute("SELECT path, sha256 FROM file_stat").fetchall())
    conn.close()
    assert rows["pkg/a.py"] == first["pkg/a.py"]
    seen.clear()
    assert snapshot.hash_files(repo) == first
    assert "a.py" not in seen and "b.py" not in seen
    assert snapshot.hash_files(repo, use_cache=False) == first and "a.py" in seen


def test_a_changed_stat_is_rehashed(tmp_path, monkeypatch):
    repo = _repo(tmp_path, git=False)
    _age(repo / "pkg" / "a.py")
    before = snapshot.hash_files(repo)
    (repo / "pkg" / "a.py").write_bytes(b"A = 12345\n")
    _age(repo / "pkg" / "a.py", 30)
    seen = _count_hashing(monkeypatch)
    after = snapshot.hash_files(repo)
    assert "a.py" in seen and after["pkg/a.py"] != before["pkg/a.py"]


def test_racy_clean_same_size_same_mtime_edit_is_still_detected(tmp_path):
    """A file rewritten within the same timestamp tick as it was hashed keeps its stat;
    it is re-hashed while its mtime is within RACY_MARGIN_NS of the recording time."""
    repo = _repo(tmp_path, git=False)
    p = repo / "pkg" / "a.py"
    st = p.stat()
    first = snapshot.hash_files(repo)["pkg/a.py"]
    p.write_bytes(b"A = 9\n")                         # same size
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))  # same mtime: the stat cannot tell
    assert p.stat().st_size == st.st_size and p.stat().st_mtime_ns == st.st_mtime_ns
    assert snapshot.hash_files(repo)["pkg/a.py"] != first


def test_no_database_means_no_cache_and_nothing_is_created(tmp_path):
    repo = tmp_path / "plain"
    repo.mkdir()
    (repo / "x.py").write_bytes(b"x = 1\n")
    assert set(snapshot.hash_files(repo)) == {"x.py"}
    assert not (repo / ".verinoda").exists()


def test_a_locked_database_only_costs_rehashing(tmp_path):
    repo = _repo(tmp_path, git=False)
    conn = sqlite3.connect(db_path(repo), timeout=0.1)
    conn.execute("BEGIN EXCLUSIVE")
    try:
        t0 = time.monotonic()
        assert set(snapshot.hash_files(repo)) >= {"pkg/a.py", "pkg/b.py"}
        assert time.monotonic() - t0 < 5
    finally:
        conn.rollback()
        conn.close()


@needs_git
def test_git_info_uses_one_status_call_and_matches_the_old_answer(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    old = snapshot._git_info_legacy(repo)
    calls: list[tuple] = []
    real = subprocess.run

    def spy(args, *a, **k):
        if args and args[0] == "git":
            calls.append(tuple(args[3:5]))
        return real(args, *a, **k)

    monkeypatch.setattr(snapshot.subprocess, "run", spy)
    new = snapshot.git_info(repo)
    assert new == old and new["is_git"] and not new["dirty"]
    assert calls == [("status", "--porcelain=v2")]
    calls.clear()
    state = snapshot.current_state(repo)
    assert len(calls) == 2  # status + ls-files (it was rev-parse x2 + status + ls-files)
    assert state["commit"] == old["commit"] and state["branch"] == old["branch"]


@needs_git
def test_git_info_dirty_detached_and_unborn(tmp_path):
    repo = _repo(tmp_path)
    (repo / "pkg" / "a.py").write_bytes(b"A = 2\n")
    assert snapshot.git_info(repo)["dirty"] is True
    (repo / ".verinoda" / "extra.txt").write_bytes(b"ignored\n")
    _git(repo, "checkout", "-q", "--", "pkg/a.py")
    assert snapshot.git_info(repo)["dirty"] is False  # .verinoda never counts
    head = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "checkout", "-q", "--detach")
    info = snapshot.git_info(repo)
    assert info["branch"] == "HEAD" and info["commit"] == head == snapshot._git_info_legacy(repo)["commit"]
    unborn = tmp_path / "unborn"
    unborn.mkdir()
    _git(unborn, "init", "-q")
    assert snapshot.git_info(unborn) == {"commit": None, "branch": None, "dirty": False, "is_git": False}
    assert snapshot.git_info(tmp_path / "unborn" / "nope") == snapshot._git_info_legacy(tmp_path / "unborn" / "nope")


@needs_git
def test_snapshots_reuse_the_cache_through_the_store(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    for f in ("a.py", "b.py"):
        _age(repo / "pkg" / f)
    st = open_store(repo)
    try:
        snap = snapshot.take_snapshot(st, repo)
        seen = _count_hashing(monkeypatch)
        state = snapshot.current_state(repo, store=st)
        assert state["tree_hash"] == snap["tree_hash"]
        assert "a.py" not in seen and "b.py" not in seen
    finally:
        st.close()
