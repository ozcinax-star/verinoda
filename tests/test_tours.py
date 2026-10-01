"""``verinoda tour`` (verinoda/tours.py): a trace path written as a CodeTour file, pinned to a commit only when the
files are as that commit has them, its steps found again when the code moves."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from verinoda import cli, freshness, index, tours, workflow
from verinoda.store import open_store

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")

ORDERS = Path(__file__).resolve().parent.parent / "examples" / "orders_app"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false",
                           "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main", *args], cwd=cwd, check=True,
                          capture_output=True, text=True, stdin=subprocess.DEVNULL).stdout.strip()


@pytest.fixture()
def orders(tmp_path):
    dst = tmp_path / "tour ğ app"
    shutil.copytree(ORDERS, dst, ignore=shutil.ignore_patterns(".verinoda", "__pycache__", "*.pyc", "*.db"))
    (dst / ".gitignore").write_text(".verinoda/\n.tours/\nt/\n", encoding="utf-8")
    _git(dst, "init", "-q")
    _git(dst, "add", "-A")
    _git(dst, "commit", "-q", "-m", "init")
    workflow.init(dst)
    st = open_store(dst)
    try:
        workflow.scan(st, dst)
    finally:
        st.close()
    return dst


def _build(repo: Path, **kw) -> dict:
    return tours.build(repo, index.load(repo), "create_order_handler", "save",
                       stale=freshness.check(repo)["files"], **kw)


def _prepend(repo: Path, rels, text: str = "# a\n# b\n") -> None:
    for rel in rels:
        f = repo / rel
        f.write_text(text + f.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")


def test_a_tour_follows_the_trace_and_quotes_each_line(orders):
    tour = _build(orders)
    assert tour["$schema"] == tours.SCHEMA and tour["ref"] == _git(orders, "rev-parse", "HEAD")
    steps = tour["steps"]
    assert steps[0]["title"].startswith("Start: ") and steps[-1]["title"].endswith(tour["steps"][-1]["title"][-10:])
    assert len(steps) >= 3 and all(isinstance(s["line"], int) and s["line"] > 0 for s in steps)
    for s in steps:
        line = (orders / s["file"]).read_text(encoding="utf-8").split("\n")[s["line"] - 1]
        assert s["verinoda"]["text"] == line.strip() and f"`{s['file']}:{s['line']}`" in s["description"]
    assert any("confidence" in s["description"] for s in steps[1:])
    p, notes = tours.write(orders, tour)
    assert p == orders / ".tours" / "create_order_handler-save.tour" and notes == []
    with pytest.raises(tours.TourError):
        tours.build(orders, index.load(orders), "create_order_handler", "no_such_thing_anywhere")


def test_a_file_changed_since_the_index_is_refused_and_a_dirty_tree_is_not_pinned(orders):
    files = {s["file"] for s in _build(orders)["steps"]}
    _prepend(orders, ["orders/api.py"])
    with pytest.raises(tours.TourError, match="changed since the index"):
        _build(orders)
    st = open_store(orders)
    try:
        workflow.update(st, orders)
    finally:
        st.close()
    tour = _build(orders)          # the index is current again, but the tree differs from HEAD
    assert "ref" not in tour and "differs from HEAD" in tour["verinoda"]["unpinned"]
    assert {s["file"] for s in tour["steps"]} == files


def test_moved_steps_are_found_again_fixed_and_re_pinned(orders):
    tour = _build(orders)
    p, _ = tours.write(orders, tour)
    assert tours.check(orders, str(p))["exit"] == 0
    _prepend(orders, {s["file"] for s in tour["steps"]})
    _git(orders, "commit", "-qam", "move")
    res = tours.check(orders, str(p))
    assert res["moved"] == len(tour["steps"]) and res["exit"] == 1
    assert all(r["to"] == r["line"] + 2 for r in res["steps"])
    fixed = tours.check(orders, str(p), fix=True)
    assert fixed["fixed"] and fixed["ref"] == _git(orders, "rev-parse", "HEAD")    # the lines are HEAD's now
    assert tours.check(orders, str(p))["exit"] == 0
    # a step whose line is rewritten is gone, and says so
    last = json.loads(p.read_text(encoding="utf-8"))["steps"][-1]
    f = orders / last["file"]
    lines = f.read_text(encoding="utf-8").split("\n")
    lines[last["line"] - 1] = "    pass  # rewritten"
    f.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    res = tours.check(orders, str(p))
    assert res["gone"] == 1 and res["exit"] == 1 and "any more" in tours.render_check(res)


def test_tours_not_written_by_verinoda_or_edited_since_are_kept(orders, capsys):
    other = orders / ".tours" / "create_order_handler-save.tour"
    other.parent.mkdir(parents=True)
    other.write_text('{"title": "mine", "steps": []}\n', encoding="utf-8")
    assert cli.main(["tour", "create_order_handler", "save", "--repo", str(orders)]) == 2
    assert "not overwritten" in capsys.readouterr().err
    assert json.loads(other.read_text(encoding="utf-8"))["title"] == "mine"
    assert cli.main(["tour", "create_order_handler", "save", "--repo", str(orders), "--out", "t/x.tour", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["steps"] >= 3 and (orders / "t" / "x.tour").is_file() and "CodeTour finds tours" in out["notes"][0]
    # written by verinoda and unchanged: rewritten; edited by hand since: kept unless --force
    assert cli.main(["tour", "create_order_handler", "save", "--repo", str(orders), "--out", "t/x.tour"]) == 0
    t = json.loads((orders / "t" / "x.tour").read_text(encoding="utf-8"))
    t["steps"][0]["description"] += " My own words."
    (orders / "t" / "x.tour").write_text(json.dumps(t), encoding="utf-8")
    assert cli.main(["tour", "create_order_handler", "save", "--repo", str(orders), "--out", "t/x.tour"]) == 2
    assert cli.main(["tour", "create_order_handler", "save", "--repo", str(orders), "--out", "t/x.tour", "--force"]) == 0
    capsys.readouterr()
    assert cli.main(["tour", "create_order_handler", "save", "--repo", str(orders), "--out", "../x.tour"]) == 2
    assert cli.main(["tour", "--check", "t/x.tour", "--repo", str(orders)]) == 0
    capsys.readouterr()
    assert cli.main(["tour", "--repo", str(orders), "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "error"
    assert cli.main(["tour", "--fix", "--repo", str(orders)]) == 2


def test_malformed_tours_are_errors_not_crashes(orders):
    for text in ("[1, 2]", '{"steps": [1]}', "not json"):
        (orders / "bad.tour").write_text(text, encoding="utf-8")
        with pytest.raises(tours.TourError):
            tours.check(orders, "bad.tour")


def test_a_structural_path_says_so(orders):
    tour = tours.build(orders, index.load(orders), "OrderRepository", "save", mode="any")
    assert "Reachability" in tour["description"] or "structural" in tour["description"]
    pairs = [(s["file"], s["line"]) for s in tour["steps"]]
    assert len(pairs) == len(set(pairs))       # one line, one step
