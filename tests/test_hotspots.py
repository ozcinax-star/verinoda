"""Hotspots: change frequency times complexity per file and per function (`verinoda map --view hotspots`), and
`verinoda review` ranking its read_first list with it.

The tests run on small generated git repositories (never on examples/)."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import json  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
from array import array  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import hotspots as hs  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false",
                           "-c", "commit.gpgsign=false", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout


def _write(repo: Path, rel: str, text: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


def _commit(repo: Path, msg: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)


def _src(hot: str = "x", cold: str = "1", pad: int = 0, tail: str = "") -> str:
    """``cold`` (one branch) before ``hot`` (two branches); ``pad`` blank-comment lines above both."""
    return ("# pad\n" * pad
            + f"def cold(a):\n    if a:\n        return {cold}\n    return 0\n\n\n"
            + f"def hot(a, b):\n    if a:\n        return '{hot}'\n    if b:\n        return 2\n{tail}    return 3\n")


@pytest.fixture()
def history(tmp_path):
    return _make_history(tmp_path)


def _make_history(tmp_path: Path) -> Path:
    """app/core.py: hot changed four times (one on a merged branch), cold once; lines shifted between versions."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _write(repo, ".gitignore", ".verinoda/\n__pycache__/\n")
    _write(repo, "app/core.py", _src())
    _write(repo, "app/flat.py", "X = 1\n")
    _commit(repo, "init")
    _write(repo, "app/core.py", _src(hot="y"))
    _commit(repo, "hot 1")
    _write(repo, "app/core.py", _src(hot="y", pad=3))   # shifts every function down: no function changed
    _commit(repo, "pad")
    _git(repo, "checkout", "-q", "-b", "side")
    _write(repo, "app/core.py", _src(hot="z", pad=3))
    _commit(repo, "hot on a branch")
    _git(repo, "checkout", "-q", "main")
    _write(repo, "app/core.py", _src(hot="y", cold="2", pad=3))
    _commit(repo, "cold 1")
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge side", "side")
    _write(repo, "app/core.py", _src(hot="z", cold="2", pad=3, tail="    b = b\n"))
    _commit(repo, "hot 3: a line added")
    _write(repo, "app/core.py", _src(hot="z", cold="2", pad=3))
    _commit(repo, "hot 4: the line deleted")
    return repo


def test_parent_map_carries_lines_across_hunks():
    m = array("i", [10, 20, 30, 40, 50])   # the version after the diff: 5 lines
    # old line 2 replaced by new lines 2-3 (it takes the first one's place); old line 4 deleted after new line 4
    hunks = [(2, 1, 2, 2), (4, 1, 4, 0)]
    assert list(hs._parent_map(m, hunks)) == [10, 20, 40, 0, 50]
    # deleted inside one function: the line takes the place of the line before the deletion
    own = [None] * 60
    own[40] = own[50] = "f"
    assert list(hs._parent_map(m, hunks, own)) == [10, 20, 40, 40, 50]
    # two old lines replaced by one: both take its place
    assert list(hs._parent_map(array("i", [1, 2]), [(1, 2, 1, 1)])) == [1, 1, 2]
    # two lines inserted after old line 1
    assert list(hs._parent_map(array("i", [1, 2, 3, 4]), [(1, 0, 2, 2)])) == [1, 4]
    # the file deleted: nothing of the old version maps
    assert list(hs._parent_map(array("i"), [(1, 2, 0, 0)])) == [0, 0]


def test_function_changes_follow_lines_through_shifts_merges_and_deletions(history):
    text = (history / "app/core.py").read_text(encoding="utf-8")
    fc = hs.function_changes(history, {"app/core.py": text}, n_commits=None)
    got = {q: c["changes"] for q, c in fc["files"]["app/core.py"].items()}
    # the initial commit adds both; hot: hot 1, the branch commit, the added line, the deleted line; the merge and
    # the padding commit are not changes of either function
    assert got == {"hot": 5, "cold": 2}
    assert fc["unmapped"] == 0


def test_uncommitted_edits_shift_the_current_spans(history):
    text = "\n" * 5 + (history / "app/core.py").read_text(encoding="utf-8")
    (history / "app/core.py").write_text(text, encoding="utf-8", newline="\n")
    fc = hs.function_changes(history, {"app/core.py": text}, n_commits=None)
    assert {q: c["changes"] for q, c in fc["files"]["app/core.py"].items()} == {"hot": 5, "cold": 2}
    [hot] = hs.rank(history, [("app/core.py", text.split("\n").index("def hot(a, b):") + 1)], {"app/core.py": text})
    assert hot["symbol"] == "app/core.py::hot" and hot["changes"] == 5 and hot["score"] == 5 * 3
    assert hot["status"] == "strong_inference"


def test_no_git_history_is_none(tmp_path):
    assert hs.file_changes(tmp_path) is None
    assert hs.rank(tmp_path, [("a.py", 1)], {"a.py": "def f():\n    return 1\n"}) == [None]


def _scan(repo: Path) -> None:
    from verinoda import workflow
    from verinoda.store import open_store

    workflow.init(repo)
    st = open_store(repo)
    try:
        workflow.scan(st, repo)
    finally:
        st.close()


class _Graph:
    """What the view reads of a graph: the root and the files its nodes come from (every file of the tree)."""

    def __init__(self, repo: Path):
        import networkx as nx

        self.root = repo
        self.G = nx.DiGraph()
        for p in repo.rglob("*"):
            rel = p.relative_to(repo).as_posix()
            if p.is_file() and not rel.startswith((".git/", ".verinoda/")):
                self.G.add_node(rel, source_file=rel)


def _graph(repo: Path) -> _Graph:
    return _Graph(repo)


def test_hotspots_view_ranks_files_and_functions(history):
    from verinoda import architecture_map as am
    from verinoda import map_text

    _write(history, "tests/test_core.py", "def test_x():\n    if 1:\n        assert True\n")
    _commit(history, "a test")
    v = am.VIEWS["hotspots"](_graph(history))
    assert v["view"] == "hotspots" and v["is_git"] and v["coverage"]["limits"]
    assert v["window"]["whole_history"] and v["window"]["commits"] == 8   # non-merge commits
    [row] = v["files"]   # flat.py has no function, the test file is test code
    assert row["file"] == "app/core.py" and row["changes"] == 7 and row["complexity"] == 2 + 3
    assert row["score"] == 7 * 5 and row["status"] == "strong_inference"
    assert row["counts_status"] == "statically_verified" and row["at"].startswith("app/core.py:1-")
    assert row["last_change"]["sha"] == _git(history, "log", "-1", "--format=%H", "--", "app/core.py")[:12]
    assert [(f["symbol"], f["changes"], f["score"]) for f in v["functions"]] == [
        ("app/core.py::hot", 5, 15), ("app/core.py::cold", 2, 4)]
    assert v["functions"][0]["at"] == "app/core.py:10-15"
    assert v["functions"][0]["cyclomatic_status"] == "statically_verified"
    text = map_text.render({"hotspots": v}, 12)
    assert "== hotspots ==" in text and "app/core.py::hot" in text and "limit:" in text
    assert "hotspots" in am.VIEWS and "hotspots" not in am.DEFAULT_VIEWS   # asked for by name


def test_cli_map_view_hotspots_json(history):
    _scan(history)
    out = subprocess.run([sys.executable, "-m", "verinoda", "map", str(history), "--view", "hotspots", "--json"],
                         capture_output=True, text=True, encoding="utf-8", cwd=history)
    assert out.returncode == 0, out.stderr
    v = json.loads(out.stdout)["hotspots"]
    assert v["files"][0]["file"] == "app/core.py" and v["functions"][0]["symbol"] == "app/core.py::hot"


def test_review_reads_the_hotter_changed_function_first(history):
    from verinoda import review as rv
    from verinoda.store import open_store

    _scan(history)
    text = (history / "app/core.py").read_text(encoding="utf-8")
    # both functions change; cold comes first in the file, hot has the higher score
    (history / "app/core.py").write_text(text.replace("return 2\n", "return 9\n")
                                         .replace("return 0\n", "return -1\n"), encoding="utf-8", newline="\n")
    st = open_store(history)
    try:
        res = rv.review(history, store=st, concerns=["health"], record=False)
    finally:
        st.close()
    changed = [r for r in res["read_first"] if r["why"].startswith("changed")]
    assert [r["hotspot"]["symbol"] for r in changed] == ["app/core.py::hot", "app/core.py::cold"]
    assert changed[0]["hotspot"]["score"] > changed[1]["hotspot"]["score"]
    assert any("hotspot" in lim for lim in res["coverage"]["limits"])


def test_the_python_bound_is_never_below_the_complexity():
    """The view skips a Python file whose word count times its changes cannot reach the top: the count must bound
    the sum of its functions' cyclomatic complexity from above (checked on Verinoda's own sources)."""
    from verinoda import health

    root = Path(__file__).resolve().parents[1] / "verinoda"
    checked = 0
    for p in sorted(root.rglob("*.py"))[:40]:
        text = p.read_text(encoding="utf-8")
        fns = health.file_metrics(p.name, text)
        if fns:
            assert len(hs._PY_BOUND_RE.findall(text)) >= sum(m.cyclomatic for m in fns.values()), p
            checked += 1
    assert checked > 25


def test_the_view_skips_files_that_cannot_reach_the_top(tmp_path, monkeypatch):
    """With room for one file, a rarely changed small file is not measured; the ranking is the same."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _write(repo, "big.py", _src())
    _write(repo, "small.py", "def s(a):\n    return a\n")
    _commit(repo, "init")
    for i in range(3):
        _write(repo, "big.py", _src(hot=str(i)))
        _commit(repo, f"big {i}")
    monkeypatch.setattr(hs, "FUNCTION_FILES", 1)
    v = hs.hotspots(_graph(repo), limit=1)
    assert [r["file"] for r in v["files"]] == ["big.py"] and v["below_top_not_measured"] == 1
    assert v["files"][0]["score"] == 4 * 5
