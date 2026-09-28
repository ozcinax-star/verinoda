"""A from-scratch search index is built in a build file and renamed into place: same tables as a build in place."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import shutil  # noqa: E402
import sqlite3  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import index, search_index  # noqa: E402

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args],
                   cwd=cwd, check=True, capture_output=True)


@pytest.fixture(scope="module", params=["orders_app", "glow_mod"])
def built(request, tmp_path_factory):
    repo = tmp_path_factory.mktemp("sb") / request.param
    shutil.copytree(EXAMPLES / request.param, repo,
                    ignore=shutil.ignore_patterns(".verinoda", "__pycache__", "*.pyc", ".pytest_cache", "*.db"))
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    assert index.build(repo, force=True)["ok"]
    return repo, index.load(repo, augment=False)


def _dump(db: Path) -> dict:
    """Every row of every table, in a fixed order (the build time is the one value that differs)."""
    conn = sqlite3.connect(str(db))
    try:
        out = {}
        for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
            rows = conn.execute(f"SELECT * FROM {name}").fetchall()
            if name == "meta":
                rows = [r for r in rows if r[0] != "built_at"]
            out[name] = sorted(rows, key=repr)
        out["schema"] = conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()
        return out
    finally:
        conn.close()


def _build(repo: Path, g, db: Path) -> dict:
    res = search_index.update(repo, g, rebuild=True, db=db)
    assert res["mode"] == "full" and res["units"] > 0
    assert not list(db.parent.glob(db.name + search_index.BUILD_SUFFIX + "*"))  # nothing left behind
    return _dump(db)


def test_the_renamed_build_has_the_tables_of_a_build_in_place(built, tmp_path, monkeypatch):
    repo, g = built
    renamed = _build(repo, g, tmp_path / "a" / "search.db")
    # the former way: the ordinary connection (WAL, synchronous NORMAL), written where it is read
    monkeypatch.setattr(search_index, "_connect_build", lambda db: search_index._connect(db))
    plain = _build(repo, g, tmp_path / "b" / "search.db")
    assert renamed == plain
    # built twice next to the graph, where a scan writes it: the same index again
    for _ in range(2):
        assert search_index.build(repo, g)["mode"] == "full"
    assert _dump(search_index.db_path_for(g)) == renamed


@pytest.mark.skipif(os.name != "nt", reason="only Windows refuses to replace a file another process has open; "
                    "POSIX replaces it, so the in-place fallback is not reached there")
def test_a_held_index_is_rebuilt_in_place(built, tmp_path, monkeypatch):
    repo, g = built
    db = tmp_path / "search.db"
    first = _build(repo, g, db)
    reader = sqlite3.connect(str(db))  # a server that has the index open
    try:
        assert reader.execute("SELECT COUNT(*) FROM units").fetchone()[0] > 0

        def held(src, dst):
            raise PermissionError("the file is in use")

        monkeypatch.setattr(search_index.os, "replace", held)  # what Windows says while it is open
        assert _build(repo, g, db) == first
        assert reader.execute("SELECT COUNT(*) FROM units").fetchone()[0] == len(first["units"])
    finally:
        reader.close()


def test_a_failed_build_leaves_the_old_index(built, tmp_path, monkeypatch):
    repo, g = built
    db = tmp_path / "search.db"
    first = _build(repo, g, db)

    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(search_index._Writer, "finish", boom)
    with pytest.raises(RuntimeError):
        search_index.update(repo, g, rebuild=True, db=db)
    assert _dump(db) == first
    assert not list(db.parent.glob(db.name + search_index.BUILD_SUFFIX + "*"))


def test_a_build_file_gone_before_install_does_not_replace_the_index(built, tmp_path, monkeypatch):
    """The sweep of old build files may remove a live one (POSIX unlinks an open file); installing it
    must fail and keep the old index, not install a new empty database (review of D66, finding 7)."""
    repo, g = built
    db = tmp_path / "search.db"
    first = _build(repo, g, db)
    install = search_index._install

    def swept(build, target):
        search_index._unlink(build)
        install(build, target)

    monkeypatch.setattr(search_index, "_install", swept)
    with pytest.raises(sqlite3.Error):
        search_index.update(repo, g, rebuild=True, db=db)
    assert _dump(db) == first
    assert not list(db.parent.glob(db.name + search_index.BUILD_SUFFIX + "*"))
    empty = tmp_path / ("search.db" + search_index.BUILD_SUFFIX + "1")
    sqlite3.connect(str(empty)).close()  # a file without the index's tables
    with pytest.raises(sqlite3.Error):
        install(empty, db)
    assert _dump(db) == first
