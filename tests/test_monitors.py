"""Code monitors and pattern trends (verinoda/monitors.py): saved searches that fail on a new match, keep their
baseline across moved lines, list what went away, and count a pattern over the history."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from verinoda import cli, monitors

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false",
                    "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main", *args], cwd=cwd, check=True,
                   capture_output=True, stdin=subprocess.DEVNULL)


def _write(repo: Path, rel: str, text: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture()
def repo(tmp_path):
    r = tmp_path / "mon ğ repo"
    _write(r, "app/a.py", "def f():\n    print('old 1')\n    print('old 2')\n")
    _write(r, "app/b.py", "def g():\n    return 1\n")
    _write(r, "docs/x.md", "print( in a doc\n")
    _git(r, "init", "-q")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "one")
    return r


def test_a_new_match_fails_moved_lines_do_not_and_gone_ones_are_listed(repo):
    out = monitors.add(repo, "no-print", regex=r"print\(", paths=["app"], message="use logging")
    assert out["matches"] == 2 and (repo / monitors.FILE).is_file()
    assert monitors.check(repo)["exit"] == 0
    _write(repo, "app/a.py", "import os\n\n\ndef f():\n    print('old 1')\n    print('old 2')\n")   # moved down
    assert monitors.check(repo)["exit"] == 0
    _write(repo, "app/b.py", "def g():\n    print('new')\n    return 1\n")
    res = monitors.check(repo)
    (r,) = res["monitors"]
    assert res["exit"] == 1 and [n["at"] for n in r["new"]] == ["app/b.py:2"] and r["new"][0]["status"] == \
        "statically_verified"
    assert "use logging" in monitors.render(res)
    _write(repo, "app/b.py", "def g():\n    return 1\n")
    _write(repo, "app/a.py", "def f():\n    print('old 1')\n")
    res = monitors.check(repo)
    assert res["exit"] == 0 and len(res["monitors"][0]["gone"]) == 1     # a migration's progress
    assert monitors.accept(repo, "no-print") == {"id": "no-print", "before": 2, "now": 1}


def test_an_ast_monitor_and_a_bad_regex(repo):
    monitors.add(repo, "no-print-call", ast="print($$$A)", langs=["python"])
    assert monitors.check(repo, "no-print-call")["exit"] == 0
    _write(repo, "app/c.py", "print(\n    'split over lines'\n)\n")
    res = monitors.check(repo, "no-print-call")
    assert res["exit"] == 1 and res["monitors"][0]["new"][0]["at"] == "app/c.py:1"
    data = json.loads((repo / monitors.FILE).read_text(encoding="utf-8"))
    data["monitors"].append({"id": "broken", "regex": "(", "baseline": []})
    (repo / monitors.FILE).write_text(json.dumps(data), encoding="utf-8")
    res = monitors.check(repo, "broken")
    assert res["exit"] == 3 and not res["monitors"][0]["complete"] and "git grep failed" in res["monitors"][0]["why"]


def test_ids_files_and_errors(repo):
    with pytest.raises(monitors.MonitorError):
        monitors.add(repo, "bad id!", regex="x")
    with pytest.raises(monitors.MonitorError):
        monitors.add(repo, "both", regex="x", ast="x")
    monitors.add(repo, "m1", regex="nothing_matches_this")
    with pytest.raises(monitors.MonitorError, match="exists"):
        monitors.add(repo, "m1", regex="y")
    (repo / monitors.FILE).write_text("[1]", encoding="utf-8")
    with pytest.raises(monitors.MonitorError):
        monitors.check(repo)


def test_trend_counts_over_history(repo):
    monitors.add(repo, "no-print", regex=r"print\(", paths=["app"])
    for k in range(3):
        _write(repo, f"app/n{k}.py", "print('x')\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", f"more {k}")
    res = monitors.trend(repo, "no-print", points=4)
    assert [p["count"] for p in res["points"]] == [2, 3, 4, 5]
    monitors.add(repo, "astm", ast="print($$$A)")
    with pytest.raises(monitors.MonitorError, match="regex monitors only"):
        monitors.trend(repo, "astm")


def test_cli(repo, capsys):
    r = str(repo)
    assert cli.main(["monitor", "add", "np", "--regex", r"print\(", "--path", "app", "--repo", r]) == 0
    assert cli.main(["monitor", "--repo", r]) == 0
    _write(repo, "app/b.py", "print('new')\n")
    capsys.readouterr()
    assert cli.main(["monitor", "--repo", r, "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["monitors"][0]["new"][0]["at"] == "app/b.py:1"
    assert cli.main(["monitor", "accept", "np", "--repo", r]) == 0
    assert cli.main(["monitor", "--repo", r]) == 0
    assert cli.main(["monitor", "remove", "np", "--repo", r]) == 0
    assert cli.main(["monitor", "accept", "--repo", r]) == 2


def test_the_monitors_file_is_never_searched(repo):
    monitors.add(repo, "np", regex=r"print\(")                         # no --path: the whole project
    assert monitors.check(repo)["exit"] == 0
    for _ in range(2):                                                 # accept settles
        assert monitors.accept(repo, "np")["now"] == 3
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "monitors")
    assert monitors.check(repo)["exit"] == 0
    assert [p["count"] for p in monitors.trend(repo, "np", points=2)["points"]] == [3, 3]


def test_git_config_does_not_change_the_keys(repo):
    monitors.add(repo, "np", regex=r"print\(", paths=["app"])
    _git(repo, "config", "grep.column", "true")
    _git(repo, "config", "grep.fullName", "true")
    assert monitors.check(repo)["exit"] == 0


def test_an_ast_search_that_skipped_files_is_unknown(repo, monkeypatch):
    monitors.add(repo, "pc", ast="print($$$A)", langs=["python"])
    _write(repo, "app/big.py", "x = 1\n" * 200_000 + "print('new')\n")   # over grep_ast's size cap
    res = monitors.check(repo, "pc")
    assert res["exit"] == 3 and "too large" in res["monitors"][0]["why"] and not res["monitors"][0]["gone"]
    (repo / "app/big.py").unlink()
    from verinoda import grep_ast
    real = grep_ast.run
    monkeypatch.setattr(grep_ast, "run", lambda *a, **k: real(*a, **{**k, "total_seconds": -1}))
    res = monitors.check(repo, "pc")
    assert res["exit"] == 3 and "time budget" in res["monitors"][0]["why"]


def test_a_cut_regex_search_is_unknown(repo, monkeypatch):
    monitors.add(repo, "np", regex=r"print\(", paths=["app"])
    monkeypatch.setattr(monitors, "MAX_MATCHES", 1)
    res = monitors.check(repo)
    assert res["exit"] == 3 and "more than 1" in res["monitors"][0]["why"]


def test_paths_are_checked_and_literal(repo):
    for bad in ("../x", "/abs", ":(top)app", "app\a.py", "./app", "app/"):
        with pytest.raises(monitors.MonitorError):
            monitors.add(repo, "p", regex="x", paths=[bad])
    with pytest.raises(monitors.MonitorError, match="no such path"):
        monitors.add(repo, "p", regex="x", paths=["nowhere"])
    assert monitors.norm_path(repo, "a.py", repo / "app") == "app/a.py"
    with pytest.raises(monitors.MonitorError, match="outside"):
        monitors.norm_path(repo, "..", repo)
    monitors.add(repo, "np", regex=r"print\(", paths=["app"])
    shutil.rmtree(repo / "app")                                        # renamed away: unknown, not green
    res = monitors.check(repo)
    assert res["exit"] == 3 and "no such path" in res["monitors"][0]["why"]


@pytest.mark.parametrize("bad", [{"baseline": "a.py\tx"}, {"baseline": ["no tab"]}, {"baseline": [1]},
                                 {"paths": "app"}, {"paths": ["../x"]}, {"regex": 5}, {"ast": "f($A)"},
                                 {"langs": "python"}, {"id": "bad id"}])
def test_a_malformed_monitor_is_an_error(repo, bad):
    m = {"id": "m", "regex": "x", "baseline": [], **bad}
    (repo / monitors.FILE).write_text(json.dumps({"verinoda-monitors": 1, "monitors": [m]}), encoding="utf-8")
    with pytest.raises(monitors.MonitorError):
        monitors.check(repo)
    assert cli.main(["monitor", "--repo", str(repo)]) == 2


def test_cli_path_is_relative_to_the_current_folder(repo, monkeypatch, capsys):
    monkeypatch.chdir(repo / "app")
    assert cli.main(["monitor", "add", "np", "--regex", r"print\(", "--path", "a.py", "--repo", str(repo)]) == 0
    assert monitors.load(repo)["monitors"][0]["paths"] == ["app/a.py"]
    _write(repo, "app/a.py", "def f():\n    print('old 1')\n")
    capsys.readouterr()
    assert cli.main(["monitor", "--repo", str(repo)]) == 0 and "gone app/a.py: print('old 2')" in \
        capsys.readouterr().out
