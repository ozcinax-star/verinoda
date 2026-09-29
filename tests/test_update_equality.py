"""The pure helpers of tools/update_equality.py (the update equality harness): root placeholders, volatile
rules, canonical dumps and the first-difference reports. No checkout is run here."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "tools" / "update_equality.py"
_spec = importlib.util.spec_from_file_location("update_equality", _PATH)
eq = importlib.util.module_from_spec(_spec)
sys.modules["update_equality"] = eq  # dataclasses look the module up by name
_spec.loader.exec_module(eq)

ROOT_B = r"C:\w\orders-s1-warm\B\orders"
ROOT_C = r"C:\w\orders-s1-warm\C\orders"


def test_every_spelling_of_the_root_becomes_the_placeholder():
    text = json.dumps({"a": ROOT_B + r"\x.py", "b": ROOT_B.replace("\\", "/") + "/x.py", "c": ROOT_B})
    out = eq.replace_root(text, ROOT_B)
    assert "orders-s1-warm" not in out
    assert json.loads(out) == {"a": "<ROOT>\\x.py", "b": "<ROOT>/x.py", "c": "<ROOT>"}
    # the same artifact of the other copy reads the same
    assert eq.replace_root(text.replace("\\\\B\\\\", "\\\\C\\\\").replace("/B/", "/C/"), ROOT_C) == out


def test_only_the_root_itself_is_replaced():
    sibling = ROOT_B + "2\\x.py"  # a folder whose name only starts like the root
    assert eq.replace_root(sibling, ROOT_B) == sibling
    assert eq.replace_root(ROOT_B + ".bak", ROOT_B) == ROOT_B + ".bak"
    assert eq.replace_root("orders/x.py", ROOT_B) == "orders/x.py"


def test_the_root_slug_of_path_minted_ids_is_replaced():
    slug = eq.root_slug(ROOT_B)
    assert slug == "c_w_orders_s1_warm_b_orders"
    assert eq.replace_root(f"{slug}_orders_api_create", ROOT_B) == "<ROOT_SLUG>_orders_api_create"
    assert eq.replace_root(f"{slug}2_x", ROOT_B) == f"{slug}2_x"
    assert eq.replace_root_obj({f"{slug}_k": [f"{slug}_v", 3]}, ROOT_B) == {"<ROOT_SLUG>_k": ["<ROOT_SLUG>_v", 3]}


def test_first_json_diff_reports_the_path_and_keeps_order():
    assert eq.first_json_diff({"a": [1, 2]}, {"a": [1, 2]}) is None
    assert eq.first_json_diff({"a": [1, 2]}, {"a": [1, 3]})[0] == "$.a[1]"
    assert eq.first_json_diff({"a": [1, 2]}, {"a": [2, 1]})[0] == "$.a[0]"  # list order counts
    assert eq.first_json_diff({"a": 1, "b": 2}, {"b": 2, "a": 1})[0] == "$ (key order)"
    assert eq.first_json_diff({"a": 1}, {"a": 1, "b": 2}) == ("$.b", "<missing>", "2")
    assert eq.first_json_diff([1], [1, 2])[0] == "$ (length)"
    assert eq.first_json_diff({"a": 1}, {"a": "1"})[0] == "$.a"


def test_first_line_diff():
    assert eq.first_line_diff("a\nb\n", "a\nb\n") is None
    assert eq.first_line_diff("a\nb\n", "a\nc\n") == (2, "b", "c")
    assert eq.first_line_diff("a\n", "a\nb\n")[0] == 2
    assert eq.first_line_diff("a\n", "a\r\n")[0] == 0


def test_canonical_json_is_order_independent_for_keys():
    assert eq.canonical_json({"b": 1, "a": "ş"}) == eq.canonical_json({"a": "ş", "b": 1}) == '{"a":"ş","b":1}'


def test_clock_rules_touch_only_their_fields():
    ctx = {"extraction_stamps": {"s8-abc"}, "code_stamps": set()}
    obj, fired = eq.apply_rules("index/build_stats.json",
                                {"graph_seconds": 1.2, "files": 3, "at": 5.0, "extraction": "s8-abc-vendored"}, ctx)
    assert obj == {"graph_seconds": "<VOLATILE>", "files": 3, "at": "<VOLATILE>",
                   "extraction": "<EXTRACTION_STAMP>-vendored"}
    assert fired == ["build_stats_clock", "extraction_stamp"]
    obj, fired = eq.apply_rules("index/build_stats.json", {"extraction": "s8-other"}, ctx)
    assert obj == {"extraction": "s8-other"} and fired == []  # an unknown stamp is compared as it is
    obj, _ = eq.apply_rules("update_result", {"seconds": 1.0, "derived": {"lexicon": {"seconds": 2, "units": 4}},
                                              "snapshot": {"id": "snp_1", "file_count": 3}}, ctx)
    assert obj == {"seconds": "<VOLATILE>", "derived": {"lexicon": {"seconds": "<VOLATILE>", "units": 4}},
                   "snapshot": {"id": "<VOLATILE>", "file_count": 3}}


def test_report_date_rule():
    text = "# Graph Report - orders  (2026-09-30)\n\nBuilt 2026-09-30\n"
    out, n = eq._rule_report_date(text, {})
    assert n == 1 and out == "# Graph Report - orders  (<DATE>)\n\nBuilt 2026-09-30\n"


def _graph():
    return {"nodes": [{"id": "a_dmf", "community": 0, "source_file": "a.dmf"},
                      {"id": "a_elem_win", "community": 0, "source_file": "a.dmf"},
                      {"id": "foo_unresolved", "community": 0},
                      {"id": "plain", "community": 1, "source_file": "p.py"}]}


@pytest.mark.parametrize("root", [ROOT_B, ROOT_C])
def test_labels_sig_rule_checks_and_replaces_a_root_bearing_signature(root):
    slug = eq.root_slug(root)
    pipeline_ids = ["a_dmf", f"a_elem_{slug}_win", f"{slug}_foo"]  # as the pipeline clustered them
    sig = {"0": eq.member_sig(pipeline_ids), "1": eq.member_sig(["plain"])}
    ctx = {"graph": _graph(), "slug": slug}
    obj, n = eq._rule_labels_sig(dict(sig), ctx)
    assert n == 1
    assert obj["1"] == sig["1"]  # root-independent: kept as it is
    assert obj["0"] == "<SIG " + eq.member_sig(["a_dmf", "a_elem_<ROOT_SLUG>_win", "<ROOT_SLUG>_foo"]) + ">"
    # a value no variant of the members hashes to is left for the comparison to report
    obj, n = eq._rule_labels_sig({"0": "0123456789abcdef"}, ctx)
    assert n == 0 and obj == {"0": "0123456789abcdef"}


def test_root_bearing_members_gives_up_past_the_budget():
    members = [f"n{i}_elem_x" for i in range(20)]
    assert eq.root_bearing_members(members, set(members), "slug_root", "0" * 16, max_tries=100) is None


def test_communities_of():
    g = {"nodes": [{"id": "b", "community": 1, "community_name": "B"}, {"id": "a", "community": 1,
                                                                        "community_name": "B"},
                   {"id": "c", "community": 0, "community_name": "C"}]}
    assert eq.communities_of(g) == {"0": {"names": ["C"], "members": ["c"]},
                                    "1": {"names": ["B"], "members": ["a", "b"]}}


def test_sqlite_dump_is_stable_and_applies_the_db_rules(tmp_path):
    def make(p: Path, root: str, ids: list[str], created: str):
        con = sqlite3.connect(p)
        con.execute("CREATE TABLE snapshots (id TEXT PRIMARY KEY, repo_root TEXT, created_at TEXT, n INTEGER)")
        con.execute("CREATE TABLE snapshot_files (snapshot_id TEXT, path TEXT, sha256 TEXT)")
        for i, sid in enumerate(ids):
            con.execute("INSERT INTO snapshots VALUES (?,?,?,?)", (sid, root, created, i))
        # inserted in another order: the dump sorts rows
        for sid in reversed(ids):
            con.execute("INSERT INTO snapshot_files VALUES (?,?,?)", (sid, "x.py", "h"))
        con.commit()
        con.close()

    rules = eq._RULES_BY_ARTIFACT["atlas.db"]
    make(tmp_path / "b.db", ROOT_B, ["snp_aaa", "snp_bbb"], "2026-01-01")
    make(tmp_path / "c.db", ROOT_C, ["snp_zzz", "snp_yyy"], "2026-01-02")
    db, fb = eq.sqlite_dump(tmp_path / "b.db", ROOT_B, rules, first=("snapshots",))
    dc, _ = eq.sqlite_dump(tmp_path / "c.db", ROOT_C, rules, first=("snapshots",))
    assert db == dc
    assert set(fb) == {"atlas_clock_columns", "atlas_snapshot_ids"}
    assert db["snapshots"]["rows"] == [["<SNAPSHOT1>", "<ROOT>", "<VOLATILE>", 0],
                                       ["<SNAPSHOT2>", "<ROOT>", "<VOLATILE>", 1]]
    assert db["snapshot_files"]["rows"][0][0] == "<SNAPSHOT1>"
    # a real difference survives the rules
    con = sqlite3.connect(tmp_path / "c.db")
    con.execute("UPDATE snapshots SET n = 7 WHERE id = 'snp_yyy'")
    con.commit()
    con.close()
    dc, _ = eq.sqlite_dump(tmp_path / "c.db", ROOT_C, rules, first=("snapshots",))
    assert eq.first_json_diff(db, dc)[0] == "$.snapshots.rows[1][3]"


def test_compare_names_the_artifact_and_the_first_difference():
    b, c = eq.Side(), eq.Side()
    b.items["index/graph.json"] = ("bytes", ('{"a": [1]}', {"a": [1]}))
    c.items["index/graph.json"] = ("bytes", ('{"a": [2]}', {"a": [2]}))
    b.items["index/GRAPH_REPORT.md"] = ("text", "x\ny\n")
    c.items["index/GRAPH_REPORT.md"] = ("text", "x\ny\n")
    b.items["index/copies.json"] = ("bytes", ('{"a": 1}', {"a": 1}))
    c.items["index/copies.json"] = ("bytes", ('{"a":1}', {"a": 1}))  # same JSON, other bytes
    out = eq.compare(b, c)
    assert out[0] == ("index/graph.json", "$.a[0]: 1 != 2")
    assert out[1][0] == "index/copies.json" and out[1][1].startswith("same JSON, bytes differ")
    assert len(out) == 2


def test_edits_are_deterministic(tmp_path):
    for side in ("b", "c"):
        r = tmp_path / side
        (r / "orders").mkdir(parents=True)
        (r / "orders" / "pricing.py").write_text("def compute_total(items):\n    return sum(items)\n",
                                                 encoding="utf-8")
        (r / "orders" / "service.py").write_text("def place(x):\n    return compute_total(x)\n", encoding="utf-8")
        assert eq.edit_add_function(r) == ["orders/pricing.py"]
        assert eq.edit_body_edit(r) == ["orders/service.py"]
        assert eq.edit_add_duplicate_stem(r) == ["eq_dup/pricing.py"]
        assert eq.edit_objc_pair(r) is None and eq.edit_go(r) is None  # nothing of that kind: skipped
    for rel in ("orders/pricing.py", "orders/service.py", "eq_dup/pricing.py"):
        assert (tmp_path / "b" / rel).read_bytes() == (tmp_path / "c" / rel).read_bytes()
    assert "return (compute_total(x)) if True else None" in (tmp_path / "b" / "orders" / "service.py").read_text()
