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
