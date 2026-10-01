"""Inventories computed from search hits: counts with their sites, a sandboxed condition over the counts."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import json  # noqa: E402
import shutil  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import cli, inventory  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "orders_app"


@pytest.fixture
def proj(tmp_path) -> Path:
    files = {
        "app/db.py": "import sqlite3\n\ndef a():\n    c = sqlite3.connect(':memory:')\n    c.close()\n\n"
                     "def b():\n    c = sqlite3.connect('x')\n    return c\n",
        "app/leak.py": "import sqlite3\nconn = sqlite3.connect('y')\nconn2 = sqlite3.connect('z')\n",
        "tools/ok.py": "import sqlite3\nc = sqlite3.connect('w')\nc.close()\n",
        "README.md": "call sqlite3.connect( in docs\n",
    }
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return tmp_path


SEARCHES = [("conn", r"sqlite3\.connect\("), ("close", r"\.close\(\)")]


def test_files_that_open_and_never_close_are_counted_with_their_lines(proj):
    res = inventory.run(proj, SEARCHES, where="conn and not close", group_by="none")
    assert res["status"] == "observed" and res["exact"]
    assert res["units"] == 2 and res["units_with_hits"] == 4
    (row,) = res["groups"]
    assert row["hits"] == {"conn": 3} and row["sites_total"] == 3
    assert row["sites"][0].startswith("README.md:1 [conn]")
    assert any(s.startswith("app/leak.py:2 [conn]") for s in row["sites"])
    assert "2 file(s) where conn and not close (observed)" in inventory.render(res)


def test_counts_compare_and_group(proj):
    res = inventory.run(proj, SEARCHES, where="conn >= 2", group_by="folder")
    assert [(r["group"], r["units"]) for r in res["groups"]] == [("app", 2)]
    res = inventory.run(proj, SEARCHES[:1], unit="line", group_by="ext")
    assert {r["group"]: r["units"] for r in res["groups"]} == {".py": 5, ".md": 1}
    res = inventory.run(proj, SEARCHES[:1], group_by="folder:1", paths=["app"])
    assert [(r["group"], r["units"]) for r in res["groups"]] == [("app", 2)]


@pytest.mark.parametrize("where", ["__import__('os')", "conn.real", "conn[0]", "lambda: 1", "conn + 1 > 0",
                                   "'x'", "1.5 < conn", "nope and conn", "conn if close else 1",
                                   "not " * 3000 + "conn", "not " * 200 + "conn", "(conn or close) >= 2",
                                   "conn " + "and conn " * 1000])
def test_the_condition_is_data_never_code(proj, where):
    with pytest.raises(inventory.InventoryError):
        inventory.run(proj, SEARCHES, where=where)


def test_bad_arguments_are_refused(proj):
    with pytest.raises(inventory.InventoryError):
        inventory.run(proj, [])
    with pytest.raises(inventory.InventoryError):
        inventory.run(proj, [("and", "x")])
    with pytest.raises(inventory.InventoryError):
        inventory.run(proj, [("a", "x"), ("a", "y")])
    with pytest.raises(inventory.InventoryError):
        inventory.run(proj, SEARCHES, group_by="folder:0")
    with pytest.raises(inventory.InventoryError):
        inventory.run(proj, SEARCHES, unit="symbol")   # no index


def test_hits_beyond_the_cap_make_the_counts_lower_bounds(proj, monkeypatch):
    monkeypatch.setattr(inventory, "MAX_HITS", 1)
    res = inventory.run(proj, SEARCHES[:1])
    assert res["status"] == "incomplete" and not res["exact"] and "lower bounds" in res["note"]
    assert inventory.render(res).startswith("at least 1 file(s)")


def test_a_condition_true_without_hits_says_what_is_not_counted(proj):
    res = inventory.run(proj, SEARCHES, where="not close")
    assert any("no hit of any search" in lim for lim in res["limits"])


def test_symbols_come_from_the_index(tmp_path):
    from verinoda import workflow
    from verinoda.store import open_store

    repo = tmp_path / "orders_app"
    shutil.copytree(EXAMPLE, repo, ignore=shutil.ignore_patterns(".verinoda", "__pycache__", "*.pyc", "*.db"))
    workflow.init(repo)
    st = open_store(repo)
    try:
        workflow.scan(st, repo)
    finally:
        st.close()
    res = inventory.run(repo, [("conn", r"self\.conn\b")], unit="symbol", group_by="symbol", paths=["orders"])
    groups = {r["group"] for r in res["groups"]}
    assert any(g.startswith("orders/repository.py::") for g in groups), groups
    assert res["units"] == len(groups)


def test_the_cli(proj, capsys):
    assert cli.main(["inventory", "--repo", str(proj), "-s", "conn", r"sqlite3\.connect\(", "-s", "close",
                     r"\.close\(\)", "--where", "conn and not close", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["units"] == 2 and {r["group"] for r in out["groups"]} == {"README.md", "app/leak.py"}
    assert cli.main(["inventory", "--repo", str(proj), "no_such_text_anywhere_xyz"]) == 1
    capsys.readouterr()
    assert cli.main(["inventory", "--repo", str(proj), "x", "--where", "os.system"]) == 2
    assert "--where" in capsys.readouterr().err


def test_names_that_cannot_be_used_in_a_condition_are_refused(proj):
    for bad in ("True", "if", "\ufb01le"):
        with pytest.raises(inventory.InventoryError):
            inventory.run(proj, [(bad, "x")])


def test_a_condition_that_a_lost_hit_can_turn_true_is_not_called_a_lower_bound(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("x.close()\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("c = connect()\nc.close()\n", encoding="utf-8")
    monkeypatch.setattr(inventory, "MAX_HITS", 1)
    res = inventory.run(tmp_path, [("conn", r"connect\("), ("close", r"\.close\(")], where="conn and not close")
    assert res["status"] == "incomplete" and not res["lower_bound"] and "not bounded" in res["note"]
    assert inventory.render(res).startswith("incomplete: ")
    res = inventory.run(tmp_path, [("conn", r"connect\("), ("close", r"\.close\(")], where="conn and close >= 1")
    assert res["lower_bound"]


def test_symbol_units_are_not_merged_by_label_and_file_units_are_not_grouped_by_symbol(tmp_path):
    from verinoda import workflow
    from verinoda.store import open_store

    (tmp_path / "m.py").write_text("class A:\n    def run(self):\n        return 1\n\n\nclass B:\n"
                                   "    def run(self):\n        return 2\n", encoding="utf-8")
    workflow.init(tmp_path)
    st = open_store(tmp_path)
    try:
        workflow.scan(st, tmp_path)
    finally:
        st.close()
    res = inventory.run(tmp_path, [("ret", "return")], unit="symbol", group_by="symbol")
    assert res["units"] == 2 and len(res["groups"]) == 2
    with pytest.raises(inventory.InventoryError):
        inventory.run(tmp_path, [("ret", "return")], unit="file", group_by="symbol")
