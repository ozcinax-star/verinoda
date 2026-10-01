"""``verinoda tour`` (verinoda/tours.py): a trace path written as a CodeTour file, pinned to a commit, its steps
found again when the code moves."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from verinoda import cli, index, tours, workflow
from verinoda.store import open_store

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")

ORDERS = Path(__file__).resolve().parent.parent / "examples" / "orders_app"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false",
                    "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main", *args], cwd=cwd, check=True,
                   capture_output=True, stdin=subprocess.DEVNULL)


@pytest.fixture()
def orders(tmp_path):
    dst = tmp_path / "tour ğ app"
    shutil.copytree(ORDERS, dst, ignore=shutil.ignore_patterns(".verinoda", "__pycache__", "*.pyc", "*.db"))
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


def test_a_tour_follows_the_trace_and_quotes_each_line(orders):
    tour = tours.build(orders, index.load(orders), "create_order_handler", "save")
    assert tour["$schema"] == tours.SCHEMA and len(tour["ref"]) == 40 and tour["title"] == "create_order_handler -> save"
    steps = tour["steps"]
    assert steps[0]["title"].startswith("Start: ") and steps[-1]["title"].startswith("End: ")
    assert len(steps) >= 3 and all(isinstance(s["line"], int) and s["line"] > 0 for s in steps)
    for s in steps:
        line = (orders / s["file"]).read_text(encoding="utf-8").split("\n")[s["line"] - 1]
        assert s["verinoda"]["text"] == line.strip() and f"`{s['file']}:{s['line']}`" in s["description"]
    assert any("confidence" in s["description"] for s in steps[1:-1])
    p = tours.write(orders, tour)
    assert p == orders / ".tours" / "create_order_handler-save.tour" and json.loads(p.read_text("utf-8"))["steps"]
    with pytest.raises(tours.TourError):
        tours.build(orders, index.load(orders), "create_order_handler", "no_such_thing_anywhere")


def test_moved_steps_are_found_again_and_gone_ones_said(orders):
    tour = tours.build(orders, index.load(orders), "create_order_handler", "save")
    p = tours.write(orders, tour)
    assert tours.check(orders, str(p))["exit"] == 0
    # two lines on top of every file the tour opens: every step moves by two
    files = {s["file"] for s in tour["steps"]}
    for rel in files:
        f = orders / rel
        f.write_text("# a\n# b\n" + f.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
    res = tours.check(orders, str(p))
    assert res["moved"] == len(tour["steps"]) and res["exit"] == 1
    assert all(r["to"] == r["line"] + 2 for r in res["steps"])
    fixed = tours.check(orders, str(p), fix=True)
    assert fixed["fixed"] and tours.check(orders, str(p))["exit"] == 0
    # a step whose line is rewritten is gone, and says so
    first = json.loads(p.read_text(encoding="utf-8"))["steps"][-1]
    f = orders / first["file"]
    lines = f.read_text(encoding="utf-8").split("\n")
    lines[first["line"] - 1] = "    pass  # rewritten"
    f.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    res = tours.check(orders, str(p))
    assert res["gone"] == 1 and res["exit"] == 1 and "any more" in tours.render_check(res)


def test_a_tour_someone_else_wrote_is_not_overwritten(orders, capsys):
    other = orders / ".tours" / "create_order_handler-save.tour"
    other.parent.mkdir(parents=True)
    other.write_text('{"title": "mine", "steps": []}\n', encoding="utf-8")
    assert cli.main(["tour", "create_order_handler", "save", "--repo", str(orders)]) == 2
    assert "not written by verinoda" in capsys.readouterr().err
    assert json.loads(other.read_text(encoding="utf-8"))["title"] == "mine"
    assert cli.main(["tour", "create_order_handler", "save", "--repo", str(orders), "--out", "t/x.tour", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["steps"] >= 3 and (orders / "t" / "x.tour").is_file()
    assert cli.main(["tour", "--check", "t/x.tour", "--repo", str(orders)]) == 0
    assert cli.main(["tour", "--repo", str(orders)]) == 2
