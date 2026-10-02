"""``update --fast`` (docs/DESIGN.md D44): the changed files are taken in at once (search index, lexicon,
stale claims) and the graph is rebuilt by a background ``verinoda update``; until it ends no snapshot is
recorded, so reading commands keep naming the files the graph is behind on."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import json  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import types  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import buildlock, freshness, index, retrieval, workflow  # noqa: E402
from verinoda.paths import graph_path  # noqa: E402
from verinoda.store import open_store  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "orders_app"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args],
                   cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def proj(tmp_path, monkeypatch):
    repo = tmp_path / "orders_app"
    shutil.copytree(EXAMPLE, repo, ignore=shutil.ignore_patterns(".verinoda", "__pycache__", "*.pyc",
                                                                 ".pytest_cache", "*.db"))
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    workflow.init(repo)
    st = open_store(repo)
    workflow.scan(st, repo)
    started: list[list[str]] = []

    def fake_spawn(argv, **kw):
        started.append(argv)
        return types.SimpleNamespace(pid=4242)

    monkeypatch.setattr(workflow, "_spawn", fake_spawn)
    yield repo, st, started
    st.close()


def _edit(repo: Path) -> None:
    p = repo / "orders" / "pricing.py"
    p.write_text(p.read_text(encoding="utf-8") + "\n\ndef loyalty_bonus(points):\n    return points * 2\n",
                 encoding="utf-8")


def test_fast_update_defers_the_graph_and_takes_the_text_in_now(proj):
    repo, st, started = proj
    before = st.latest_snapshot()["id"]
    graph_before = graph_path(repo).read_bytes()
    _edit(repo)
    res = workflow.update(st, repo, fast=True)
    assert res["index_mode"] == "deferred" and res["graph_behind"] == ["orders/pricing.py"]
    assert res["background"] == {"started": True, "pid": 4242, "log": res["background"]["log"]}
    assert started and started[0][-3:] == ["update", "--repo", str(repo)]
    assert st.latest_snapshot()["id"] == before and graph_path(repo).read_bytes() == graph_before
    # the text is searchable now; the files the graph is behind on are named as changed
    g = index.load(repo)
    text = retrieval.render_text(retrieval.retrieve(g, "loyalty bonus points", retrieval.Budget(5, 4000)), 4000)
    assert "def loyalty_bonus(points):" in text
    assert freshness.check(repo)["files"] == ["orders/pricing.py"]
    # the background build is a plain update: it records the snapshot and the graph has the function
    res2 = workflow.update(st, repo)
    assert res2["index_mode"] == "full" and st.latest_snapshot()["id"] != before
    assert any(d.get("label") == "loyalty_bonus()" for _, d in index.load(repo).G.nodes(data=True))
    assert freshness.check(repo)["files"] == []


def test_a_change_the_graph_does_not_read_is_taken_in_as_before(proj):
    repo, st, started = proj
    (repo / "notes.txt").write_text("a note\n", encoding="utf-8")
    res = workflow.update(st, repo, fast=True)
    assert res["index_mode"] == "none" and "background" not in res and not started


def test_nothing_is_started_while_another_build_holds_the_lock(proj, monkeypatch):
    repo, st, started = proj
    monkeypatch.setattr(buildlock, "is_locked", lambda r: True)
    got = workflow.start_background_update(repo)
    assert got["started"] is False and "another index build" in got["why"] and not started


def test_the_cli_says_what_the_graph_is_behind_on(proj, capsys):
    from verinoda import cli

    repo, st, started = proj
    _edit(repo)
    assert cli.main(["update", "--repo", str(repo), "--fast"]) == 0
    out = capsys.readouterr().out
    assert "index deferred" in out and "graph: behind on 1 file(s) (orders/pricing.py)" in out
    assert "rebuilding in the background (pid 4242" in out


def test_mcp_index_update_is_fast_after_a_slow_build(proj, monkeypatch):
    from verinoda.mcp.server import AtlasTools

    repo, st, started = proj
    _edit(repo)
    monkeypatch.setattr(buildlock, "last_build_seconds", lambda r: 40.0)
    res = AtlasTools(repo).index_update()
    assert res["index_mode"] == "deferred" and res["graph_behind"] == ["orders/pricing.py"] and started
    monkeypatch.setattr(buildlock, "last_build_seconds", lambda r: 2.0)
    res = AtlasTools(repo).index_update()
    assert res["index_mode"] == "full"
    assert json.dumps(res)  # a plain result


def test_a_function_in_a_new_file_ranks_by_its_name_before_the_graph_has_it(proj):
    """After ``update --fast`` a new source file is a data unit until the background build ends; a question that
    names a function it defines must still find that function first, not similarly named symbols the graph has."""
    repo, st, started = proj
    p = repo / "orders" / "pricing.py"
    p.write_text(p.read_text(encoding="utf-8") + "".join(
        f"\n\ndef loyalty_bonus_v{i}(points):\n    return points * {i}\n" for i in range(1, 7)), encoding="utf-8")
    workflow.update(st, repo)  # the graph has the six
    (repo / "orders" / "promo.py").write_text("def loyalty_bonus_v7(points):\n    return points * 7\n", encoding="utf-8")
    res = workflow.update(st, repo, fast=True)
    assert res["index_mode"] == "deferred" and "orders/promo.py" in res["graph_behind"]
    hits = retrieval.retrieve(index.load(repo), "loyalty_bonus_v7", retrieval.Budget(5, 4000))
    files = [it.get("file") for it in hits.get("items", [])]
    assert files and files[0] == "orders/promo.py", files


def test_every_background_start_or_refusal_is_logged(proj, monkeypatch):
    """The background log records completions only; a start, and a start refused, leave a line too, so a build that
    never ran (or never finished) can be told apart afterwards."""
    from verinoda.paths import index_dir

    repo, _st, started = proj
    log = index_dir(repo) / workflow._BG_LOG
    got = workflow.start_background_update(repo)
    assert got["started"] is True
    text = log.read_text(encoding="utf-8")
    assert "background start: pid 4242" in text
    monkeypatch.setattr(buildlock, "is_locked", lambda r: True)
    got = workflow.start_background_update(repo)
    assert got["started"] is False
    assert "background not started: another index build is running" in log.read_text(encoding="utf-8")


def test_a_child_and_its_parent_both_append_to_the_background_log(tmp_path):
    """The log is handed to the detached child as its stdout; lines the parent (or another build) adds afterwards
    must not be overwritten by the child's writes from its stale position (Windows' 'ab' appends only for the
    process that opened it)."""
    import sys

    log = tmp_path / "background_update.log"
    log.write_text("earlier\n", encoding="utf-8")
    child = "import sys, time; time.sleep(0.5); print('child line', flush=True)"
    with workflow._open_append(log) as out:
        p = subprocess.Popen([sys.executable, "-c", child], stdout=out, stderr=out)
    with open(log, "a", encoding="utf-8") as fh:  # the parent's note, written while the child still runs
        fh.write("parent line\n")
    p.wait(timeout=30)
    lines = log.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "earlier" and "parent line" in lines and "child line" in lines, lines
