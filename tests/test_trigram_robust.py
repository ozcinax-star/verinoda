"""verinoda search: freshness, concurrency, a damaged index, paths and time limits."""

from __future__ import annotations

import os
import subprocess
import threading

import pytest

from verinoda import trigram
from verinoda.cli import main


def _git(cwd, *a):
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *a], cwd=cwd, check=True,
                   capture_output=True)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "proj ş"
    (r / "src").mkdir(parents=True)
    for i in range(30):
        (r / "src" / f"m{i}.py").write_text(f"def fn_{i}():\n    return 'alpha {i}'\n", encoding="utf-8")
    _git(r, "init", "-q")
    return r


def test_a_rewrite_that_keeps_size_and_time_is_seen_while_racy(repo):
    p = repo / "src" / "m0.py"
    assert trigram.search(repo, "alpha 0")["total"] == 1
    st = p.stat()
    p.write_text(p.read_text(encoding="utf-8").replace("alpha", "gamma"), encoding="utf-8")
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))       # same size, same time
    assert trigram.search(repo, "gamma 0")["total"] == 1      # written within the racy window of the last refresh


def test_an_unreadable_file_is_tried_again(repo, monkeypatch):
    real = trigram._read
    calls = {"n": 0}

    def flaky(r, rel):
        if rel == "src/m1.py" and calls["n"] == 0:
            calls["n"] += 1
            return None, "error"
        return real(r, rel)

    monkeypatch.setattr(trigram, "_read", flaky)
    res = trigram.search(repo, "alpha 1\\b")    # indexed while locked: stored without a time, read by the search
    assert res["total"] == 1 and res["index_update"]["unreadable"] == 1
    again = trigram.search(repo, "alpha 1\\b")
    assert again["total"] == 1 and again["index_update"]["changed"] >= 1


def test_two_searches_at_once_do_not_fail(repo):
    errors = []

    def go():
        try:
            trigram.search(repo, "alpha 2")
        except Exception as exc:  # noqa: BLE001 - the test reports any failure
            errors.append(exc)

    ts = [threading.Thread(target=go) for _ in range(3)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert errors == []
    assert trigram.search(repo, "alpha 2\\b")["files_indexed"] == 30


def test_a_damaged_index_is_rebuilt(repo):
    trigram.search(repo, "alpha")
    db = trigram.db_path(repo)
    for suffix in ("-wal", "-shm"):
        p = db.with_name(db.name + suffix)
        if p.exists():
            p.unlink()
    db.write_bytes(b"this is not a database" * 100)
    assert trigram.search(repo, "alpha 3\\b")["total"] == 1


def test_many_changes_rebuild_instead_of_patching(repo):
    trigram.search(repo, "alpha")
    for i in range(20):
        (repo / "src" / f"m{i}.py").write_text(f"def fn_{i}():\n    return 'beta {i}'\n", encoding="utf-8")
    res = trigram.search(repo, "beta 7")
    assert res["total"] == 1 and res["index_update"]["built"] is True
    assert trigram.search(repo, "alpha 7")["total"] == 0


def test_surrogates_in_a_pattern_and_zero_length_matches(repo):
    assert trigram.search(repo, "\ud800alpha 4")["total"] == 0
    assert trigram.search(repo, "x\udcffalpha", fixed=True)["total"] == 0
    assert trigram.required("ab\ud800cdef") == ("lit", "cdef")
    assert trigram.search(repo, "^", paths=["src/m5.py"])["total"] == 3   # every line start, the last one empty


def test_paths_resolve_like_files(repo, tmp_path):
    src = repo / "src"
    assert trigram.search(repo, "alpha", paths=[str(src / "m6.py")])["total"] == 1      # absolute
    assert trigram.search(repo, "alpha", paths=["m6.py"], cwd=src)["total"] == 1        # relative to cwd
    assert trigram.search(repo, "alpha", paths=["."], cwd=src)["total"] == 30           # "." is the cwd: src
    with pytest.raises(trigram.SearchError):
        trigram.search(repo, "alpha", paths=[str(tmp_path)])                             # outside
    with pytest.raises(trigram.SearchError):
        trigram.search(repo, "alpha", paths=["src/nope.py"])                             # missing
    if os.name == "nt":
        assert trigram.search(repo, "alpha", paths=["SRC/M6.PY"])["total"] == 1


def test_the_time_limit_leaves_an_incomplete_search(repo, capsys):
    trigram.search(repo, "alpha")
    res = trigram.search(repo, "zzz_none|alpha 9\\b", timeout=1e-9)
    assert res["status"] == "incomplete" and res["not_read"]["why"] == "the time limit ran out"
    assert main(["search", "\\w{40}", "--repo", str(repo), "--timeout", "0.000000001"]) == 3
    assert "no proof of absence" in capsys.readouterr().out
    assert main(["search", "x", "--repo", str(repo), "--timeout", "0"]) == 2
