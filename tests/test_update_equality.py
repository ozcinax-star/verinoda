"""The pure helpers of tools/update_equality.py (the update equality harness): JSON value spans and raw-text
masks, the side-specific volatile rules, SQLite dumps, file naming, edit plans, the run wrapper and the
first-difference reports. No checkout is run here."""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "tools" / "update_equality.py"
_spec = importlib.util.spec_from_file_location("update_equality", _PATH)
eq = importlib.util.module_from_spec(_spec)
sys.modules["update_equality"] = eq  # dataclasses look the module up by name
_spec.loader.exec_module(eq)


# -- JSON value spans and masks ------------------------------------------------------------------------------------

def test_json_value_spans_follow_the_pattern():
    text = '{"a": {"seconds": 1.5, "b": [1, {"seconds": 2}]}, "seconds" :3, "graph": {"mtime_ns": 5}}'
    spans = eq.json_value_spans(text, ("**", "seconds"))
    assert [(p, text[a:b]) for p, a, b in spans] == [(("a", "seconds"), "1.5"), (("a", "b", 1, "seconds"), "2"),
                                                      (("seconds",), "3")]
    assert [text[a:b] for _p, a, b in eq.json_value_spans(text, ("graph", "mtime_ns"))] == ["5"]
    assert [p for p, _a, _b in eq.json_value_spans(text, ("a", "b", "*"))] == [("a", "b", 0), ("a", "b", 1)]
    assert [text[a:b] for _p, a, b in eq.json_value_spans(text, ("a", "b", 0))] == ["1"]
    assert eq.json_value_spans(text, ("missing", "x")) == []
    assert eq.json_value_spans('{"a": "x\\"}y"}', ("a",))[0][1:] == (6, 13)  # escapes inside strings


@pytest.mark.parametrize("bad", ['{"a": 1', '{"a" 1}', '[1 2]', '{"a": 1} x', ''])
def test_json_value_spans_reject_what_is_not_one_document(bad):
    with pytest.raises(ValueError):
        eq.json_value_spans(bad, ("a",))


def test_replace_spans_refuses_overlaps():
    assert eq.replace_spans("abcdef", [(4, 5, "E"), (0, 1, "A")]) == "AbcdEf"
    with pytest.raises(ValueError):
        eq.replace_spans("abcdef", [(0, 3, "x"), (2, 4, "y")])


T0 = 1_790_000_000.0  # a run window used below: [T0, T0 + 10]
ISO = "dddd-dd-ddTdd:dd:dd+dd:dd"  # the shape of an ISO clock written with timespec="seconds" in UTC


def _ctx(name="B", **kw):
    kw.setdefault("windows", [("scan", T0, T0 + 10), ("add_function", T0 + 20, T0 + 30)])
    return eq.SideCtx(name, **kw)


def _iso(t: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds")


def test_mask_changes_only_the_volatile_value_and_keeps_the_bytes_around_it():
    text = ('{\n  "graph_seconds": 0.7,\n  "files":null, "at": ' + str(T0 + 5.25) +
            ',\n  "extraction": "s8-a"\n}\n')
    sb = _ctx(stamps={"extraction": "s8-a"})
    out, fired = eq.mask_text("index/build_stats.json", text, sb)
    assert out == ('{\n  "graph_seconds": "<DURATION>",\n  "files":null, "at": "<CLOCK run scan float>",\n'
                   '  "extraction": "<EXTRACTION_STAMP>"\n}\n')
    assert fired == ["build_stats_clock", "build_stats_duration", "extraction_stamp"]
    # a clock that is not a number is not a clock: kept, so the comparison sees it
    out, _ = eq.mask_text("index/build_stats.json", '{"at": "soon"}', sb)
    assert out == '{"at": "soon"}'
    # text that is not JSON is compared as it is
    assert eq.mask_text("index/build_stats.json", '{"at": 1', sb) == ('{"at": 1', [])


def test_clocks_are_accepted_only_inside_a_run_of_this_side_and_named_by_it():
    s = _ctx()
    assert eq.clock_run(T0, s) == "scan" and eq.clock_run(T0 + 25.5, s) == "add_function"
    assert eq.clock_run(T0 + 15, s) is None                          # between two runs
    assert eq.clock_run(T0 - 1, s) is None                           # before every run (stale)
    assert eq.clock_run(T0 + 86_400, s) is None                      # a day ahead (future)
    assert eq.clock_run(T0 - 0.005, s) == "scan"                     # rounding slack
    # a value truncated to the second may lie before the window's start (ISO text has whole seconds)
    s2 = eq.SideCtx("B", windows=[("scan", T0 + 0.7, T0 + 3)])
    assert eq._clock_text(_iso(T0), s2) == "<CLOCK run scan dddd-dd-ddTdd:dd:dd+dd:dd>"
    assert eq._clock_number(T0, s2) is None
    # numbers, nanoseconds, ISO text and numbers written as text; the token keeps the kind of value
    assert eq._clock_number(T0 + 1.5, s) == "<CLOCK run scan float>"
    assert eq._clock_ns(int((T0 + 21.5) * 1e9), s) == "<CLOCK run add_function ns>"
    assert eq._clock_ns(int((T0 + 86_421) * 1e9), s) is None
    assert eq._clock_text(_iso(T0 + 22), s) == "<CLOCK run add_function dddd-dd-ddTdd:dd:dd+dd:dd>"
    assert eq._clock_text("2026-09-30", s) is None and eq._clock_text(5, s) is None
    assert eq._clock_numeric_text(str(T0 + 2.5), s) == "<CLOCK run scan text-float>"
    assert eq._clock_numeric_text("nan", s) is None and eq._clock_numeric_text(T0, s) is None
    # the other side's windows do not count
    other = eq.SideCtx("C", windows=[("scan", T0 + 100, T0 + 110)])
    assert eq._clock_number(T0 + 1, other) is None
    assert eq.iso_seconds("2026-09-30T00:23:22") == (eq.iso_seconds("2026-09-30T00:23:22+00:00")[0], True)
    assert eq.iso_seconds("2026-09-30T00:23:22.5Z")[1] is False
    assert eq.iso_seconds("not a date") is None


def test_manifest_seen_names_the_run_so_a_stale_or_future_one_is_a_difference():
    man = '{"a.py": {"mtime": 1.5, "seen": %s, "ast_hash": "h"}}'
    sb, sc = _ctx("B"), _ctx("C")
    b = eq.mask_text("index/manifest.json", man % (T0 + 25.386277), sb)[0]
    assert b == man % '"<CLOCK run add_function float>"'
    # the candidate kept the scan's `seen` (mutant g): named by another run, so it differs
    assert eq.mask_text("index/manifest.json", man % (T0 + 5.5), sc)[0] == man % '"<CLOCK run scan float>"'
    assert eq.mask_text("index/manifest.json", man % (T0 + 86_400.5), sc)[0] == man % (T0 + 86_400.5)
    # the candidate writes whole seconds (an int: mutant m1, or a float without fraction): another kind of value
    assert eq.mask_text("index/manifest.json", man % int(T0 + 25), sc)[0] == man % '"<CLOCK run add_function int>"'
    assert eq.mask_text("index/manifest.json", man % float(int(T0 + 25)), sc)[0] == \
        man % '"<CLOCK run add_function float-whole>"'


def test_clock_shape_keeps_the_kind_of_value_but_not_the_digit_count():
    assert eq.clock_shape(1790730897.386277) == eq.clock_shape(1790730897.38627) == "float"
    assert eq.clock_shape(1790730900) == "int" and eq.clock_shape(1790730900.0) == "float-whole"
    assert eq.clock_shape(1790730897_386277700, ns=True) == "ns"
    assert eq.clock_shape(1790730897_000000000, ns=True) == "ns-whole-second"
    assert eq.clock_shape(1790730897.5e9, ns=True) == "float-ns"
    assert eq.clock_shape("2026-09-30T01:27:13+00:00") == "dddd-dd-ddTdd:dd:dd+dd:dd"
    assert eq.clock_shape("2026-09-30T01:27:13.123456Z") == "dddd-dd-ddTdd:dd:dd.ddddddZ"
    assert eq.clock_shape(True) == "bool"
    s = _ctx()
    assert eq._clock_numeric_text("1790000002", s) == "<CLOCK run scan text-int>"
    assert eq._clock_numeric_text("1790000002.25", s) == "<CLOCK run scan text-float>"
    assert eq._clock_numeric_text("1_790_000_002.25", s) == "<CLOCK run scan text-other>"  # float() only
    assert eq._clock_ns(int(T0 + 1) * 10**9, s) == "<CLOCK run scan ns-whole-second>"


def test_replace_clock_texts_and_durations():
    s = _ctx()
    text = f"created {_iso(T0 + 3)} and {_iso(T0 - 3600)}"
    assert eq.replace_clock_texts(text, s) == (f"created <CLOCK run scan {ISO}> and {_iso(T0 - 3600)}", 1)
    assert eq.replace_durations("took 1.5s, 30 ms and 2 seconds; v1.2s3 x2s") == \
        ("took <DURATION>s, <DURATION> ms and <DURATION> seconds; v1.2s3 x2s", 3)


def test_random_ids_are_numbered_per_prefix_and_only_this_sides_are_replaced():
    s = eq.SideCtx("B")
    eq.number_ids(["snp_0123456789ab", "snp_00000000000f", "ADR-0002", None, "snp_0123456789ab"], s)
    eq.number_ids(["clm_aaaaaaaaaaaa"], s)
    eq.number_ids(["snp_111111111111"], s)
    assert s.ids == {"snp_0123456789ab": "<snp#1>", "snp_00000000000f": "<snp#2>", "clm_aaaaaaaaaaaa": "<clm#1>",
                     "snp_111111111111": "<snp#3>"}
    text = '{"id": "clm_aaaaaaaaaaaa", "snapshot_id": "snp_00000000000f", "other": "clm_bbbbbbbbbbbb"}'
    assert eq.replace_known_ids(text, s) == (
        '{"id": "<clm#1>", "snapshot_id": "<snp#2>", "other": "clm_bbbbbbbbbbbb"}', 2)
    assert eq.replace_known_ids("xsnp_0123456789ab snp_0123456789abc", s)[1] == 0  # whole ids only


def test_stamps_are_accepted_only_on_their_own_side():
    sb = eq.SideCtx("B", stamps={"extraction": "s8-base", "code": "c-base", "python_facts": "2:f",
                                 "python_cross": "1:x", "empty_json": "e1"})
    sc = eq.SideCtx("C", stamps={"extraction": "s8-cand", "code": "c-cand", "python_facts": "2:g",
                                 "python_cross": "1:y", "empty_json": "e2"})
    assert eq.mask_text("index/build_stats.json", '{"extraction": "s8-base"}', sb)[0] == \
        eq.mask_text("index/build_stats.json", '{"extraction": "s8-cand"}', sc)[0] == \
        '{"extraction": "<EXTRACTION_STAMP>"}'
    # the baseline's stamp recorded on the candidate's side (stale) is not the candidate's: kept
    assert eq.mask_text("index/build_stats.json", '{"extraction": "s8-base"}', sc)[0] == '{"extraction": "s8-base"}'
    assert eq.mask_text("index/build_stats.json", '{"extraction": "s8-base-vendored"}', sb)[0] == \
        '{"extraction": "<EXTRACTION_STAMP>-vendored"}'
    for art, value, token in (("index/rebuild_record.json", "c-base", "<CODE_STAMP>"),
                              ("index/python_facts.json", "2:f", "<PYTHON_FACTS_STAMP>"),
                              ("index/python_cross.json", "1:x", "<PYTHON_CROSS_STAMP>"),
                              ("index/empty_json.json", "e1", "<EMPTY_JSON_STAMP>")):
        text = json.dumps({"stamp": value, "files": {}})
        assert eq.mask_text(art, text, sb)[0] == json.dumps({"stamp": token, "files": {}})
        assert eq.mask_text(art, text, sc)[0] == text
    upd = '{"extraction": {"was": "s8-cand", "now": "s8-cand"}, "seconds": 1}'
    assert eq.mask_text(eq.JSON_OUT, upd, sc)[0] == \
        '{"extraction": {"was": "<EXTRACTION_STAMP>", "now": "<EXTRACTION_STAMP>"}, "seconds": "<DURATION>"}'
    assert eq.mask_text(eq.JSON_OUT, upd, sb)[0].startswith('{"extraction": {"was": "s8-cand"')


def test_graph_mtimes_must_be_ones_this_side_had():
    sb = eq.SideCtx("B", graph_mtimes={100: "scan", 200: "add_function"})
    sc = eq.SideCtx("C", graph_mtimes={150: "scan", 250: "add_function"})
    rb = eq.mask_text("index/receiver_calls.json", '{"graph": {"size": 3, "mtime_ns": 200, "sha256": "x"}}', sb)[0]
    rc = eq.mask_text("index/receiver_calls.json", '{"graph": {"size": 3, "mtime_ns": 250, "sha256": "x"}}', sc)[0]
    assert rb == rc == '{"graph": {"size": 3, "mtime_ns": "<GRAPH_MTIME after add_function>", "sha256": "x"}}'
    # the graph of another step, or a value the side's graph.json never had, stays visible
    assert "scan" in eq.mask_text("index/rebuild_record.json", '{"graph": {"mtime_ns": 150}}', sc)[0]
    assert eq.mask_text("index/rebuild_record.json", '{"graph": {"mtime_ns": 7}}', sc)[0] == \
        '{"graph": {"mtime_ns": 7}}'


def test_update_result_rules():
    sb = _ctx(ids={"snp_00000000000a": "<snp#1>", "snp_00000000000b": "<snp#2>", "clm_00000000000c": "<clm#1>"})
    text = ('{"snapshot": {"id": "snp_00000000000b", "file_count": 3, "created_at": "' + _iso(T0 + 21) + '"}, '
            '"stale": [{"id": "clm_00000000000c", "text": "t"}], "background": {"started": true, "pid": 4242}, '
            '"index_seconds": 0.6, "derived": {"lexicon": {"seconds": 2, "units": 4}, "anchors": {"ms": 2.4}}}')
    out, fired = eq.mask_text(eq.JSON_OUT, text, sb)
    assert json.loads(out) == {"snapshot": {"id": "<snp#2>", "file_count": 3,
                                            "created_at": "<CLOCK run add_function dddd-dd-ddTdd:dd:dd+dd:dd>"},
                               "stale": [{"id": "<clm#1>", "text": "t"}],
                               "background": {"started": True, "pid": "<PID>"},
                               "index_seconds": "<DURATION>",
                               "derived": {"lexicon": {"seconds": "<DURATION>", "units": 4},
                                           "anchors": {"ms": "<DURATION>"}}}
    assert set(fired) == {"output_ids", "output_clocks", "update_timings", "update_background_pid"}
    # an id this side's atlas.db does not hold, or a clock outside its runs, is compared as it is
    assert '"snp_zzzzzzzzzzzz"' in eq.mask_text(eq.JSON_OUT, '{"snapshot": {"id": "snp_zzzzzzzzzzzz"}}', sb)[0]
    late = '{"created_at": "' + _iso(T0 + 3600) + '"}'
    assert eq.mask_text(eq.JSON_OUT, late, sb)[0] == late


def test_text_output_and_stderr_rules():
    s = _ctx(ids={"snp_00000000000a": "<snp#1>"})
    out, fired = eq.mask_text(eq.TEXT_OUT, "noop: 0 changed file(s); index none; snapshot snp_00000000000a\n", s)
    assert out == "noop: 0 changed file(s); index none; snapshot <snp#1>\n" and fired == ["output_ids"]
    out, _ = eq.mask_text(eq.ERR_OUT, "waited 2.5 s for the lock\n", s)
    assert out == "waited <DURATION> s for the lock\n"


def test_manifest_and_lexicon_and_report_rules():
    s = _ctx()
    man = '{\n  "a.py": {\n    "mtime": 1.5,\n    "seen": %s,\n    "ast_hash": "h"\n  }\n}' % (T0 + 1.25)
    assert eq.mask_text("index/manifest.json", man, s)[0] == man.replace(str(T0 + 1.25), '"<CLOCK run scan float>"')
    lex = '{"version":1,"built_at":"' + _iso(T0 + 2) + '","units":21}'
    assert eq.mask_text("index/lexicon.json", lex, s)[0] == \
        '{"version":1,"built_at":"<CLOCK run scan dddd-dd-ddTdd:dd:dd+dd:dd>","units":21}'
    day = time.strftime("%Y-%m-%d", time.localtime(T0))
    rep = f"# Graph Report - orders  ({day})\n\nBuilt {day}\n"
    assert eq.mask_text("index/GRAPH_REPORT.md", rep, s) == \
        (f"# Graph Report - orders  (<DATE>)\n\nBuilt {day}\n", ["report_date"])
    # a date on which no run of this side ran is compared as it is
    old = "# Graph Report - orders  (2001-01-01)\n"
    assert eq.mask_text("index/GRAPH_REPORT.md", old, s) == (old, [])


# -- first differences -----------------------------------------------------------------------------------------

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


def test_bytes_diff():
    assert eq.bytes_diff(b'{"a": [1]}', b'{"a": [1]}') is None
    assert eq.bytes_diff(b'{"a": [1]}', b'{"a": [2]}') == "$.a[0]: 1 != 2"
    assert eq.bytes_diff(b'{"a": 1}', b'{"a":1}').startswith("same JSON, bytes differ at line 1")
    assert eq.bytes_diff(b'{\n"a": 1}', b'{\n  "a": 1}').startswith("same JSON, bytes differ at line 2")
    assert eq.bytes_diff(b"x\ny\n", b"x\nz\n") == "line 2: y != z"


def test_compare_names_the_artifact_and_the_first_difference():
    b, c = eq.Side(), eq.Side()
    b.items["index/graph.json"] = ("bytes", b'{"a": [1]}')
    c.items["index/graph.json"] = ("bytes", b'{"a": [2]}')
    b.items["index/GRAPH_REPORT.md"] = c.items["index/GRAPH_REPORT.md"] = ("bytes", b"x\ny\n")
    b.items["index/copies.json"] = ("bytes", b'{"a": 1}')
    c.items["index/copies.json"] = ("bytes", b'{"a":1}')  # same JSON, other bytes
    c.items["index/extra.txt"] = ("bytes", b"")           # written by one side only
    b.items["graph_kept"], c.items["graph_kept"] = ("json", True), ("json", False)
    out = eq.compare(b, c)
    assert out[0] == ("graph_kept", "$: true != false")
    assert out[1] == ("index/graph.json", "$.a[0]: 1 != 2")
    assert out[2][0] == "index/copies.json" and out[2][1].startswith("same JSON, bytes differ")
    assert out[3] == ("index/extra.txt", "only on the candidate side")
    assert len(out) == 4
    assert [a for a, _w in eq.compare(b, c, skip={"graph_kept", "index/extra.txt"})] == \
        ["index/graph.json", "index/copies.json"]


# -- SQLite --------------------------------------------------------------------------------------------------------

def _atlas(p: Path, ids: list[str], created: str, reverse_files: bool = False):
    con = sqlite3.connect(p)
    con.execute("PRAGMA journal_mode=wal")
    con.execute("CREATE TABLE snapshots (id TEXT PRIMARY KEY, created_at TEXT, n INTEGER)")
    con.execute("CREATE TABLE snapshot_files (snapshot_id TEXT, path TEXT, PRIMARY KEY (snapshot_id, path))")
    con.execute("CREATE TABLE log (seq INTEGER PRIMARY KEY AUTOINCREMENT, what TEXT)")
    con.execute("CREATE TABLE terms (term TEXT PRIMARY KEY, n INTEGER) WITHOUT ROWID")
    for i, sid in enumerate(ids):
        con.execute("INSERT INTO snapshots VALUES (?,?,?)", (sid, created, i))
    files = [(sid, f) for sid in ids for f in ("a.py", "b.py")]
    for row in (reversed(files) if reverse_files else files):
        con.execute("INSERT INTO snapshot_files VALUES (?,?)", row)
    for w in ("x", "y"):
        con.execute("INSERT INTO log (what) VALUES (?)", (w,))
    for t in (("zeta", 1), ("alpha", 2)) if reverse_files else (("alpha", 2), ("zeta", 1)):
        con.execute("INSERT INTO terms VALUES (?,?)", t)
    con.commit()
    return con  # left open: the WAL is not checkpointed yet


def test_sqlite_dump_reads_rowid_order_through_the_rules(tmp_path):
    rules = eq._RULES_BY_ARTIFACT["atlas.db"]
    cb = _atlas(tmp_path / "b.db", ["snp_00000000000a", "snp_00000000000b"], _iso(T0 + 1))
    cc = _atlas(tmp_path / "c.db", ["snp_ffffffffffff", "snp_eeeeeeeeeeee"], _iso(T0 + 105))
    sb, sc = _ctx("B"), eq.SideCtx("C", windows=[("scan", T0 + 100, T0 + 110)])
    db, fired = eq.sqlite_dump(tmp_path / "b.db", rules, sb, tmp_path / "tb")
    dc, _ = eq.sqlite_dump(tmp_path / "c.db", rules, sc, tmp_path / "tc")
    assert db == dc
    assert set(fired) == {"atlas_clock_columns", "atlas_random_ids"}
    assert sb.ids == {"snp_00000000000a": "<snp#1>", "snp_00000000000b": "<snp#2>"}
    assert db["pragmas"]["journal_mode"] == "wal"  # read from a copy that holds the WAL
    assert db["snapshots"]["columns"] == ["<rowid>", "id", "created_at", "n"]
    clock = "<CLOCK run scan dddd-dd-ddTdd:dd:dd+dd:dd>"
    assert db["snapshots"]["rows"] == [[1, "<snp#1>", clock, 0], [2, "<snp#2>", clock, 1]]
    assert db["snapshot_files"]["rows"][0] == [1, "<snp#1>", "a.py"]
    assert db["sqlite_sequence"]["rows"] == [[1, "log", 2]]
    assert db["terms"] == {"columns": ["term", "n"], "rows": [["alpha", 2], ["zeta", 1]]}  # WITHOUT ROWID: key order
    assert [r[1] for r in db["sqlite_master"]][:2] == ["snapshots", "sqlite_autoindex_snapshots_1"]
    # the originals were never opened by the dump: no copy is left behind either
    assert not list((tmp_path / "tb").iterdir())
    # a clock outside this side's runs (a stale or future one) is kept, so it differs
    dl, _ = eq.sqlite_dump(tmp_path / "c.db", rules, eq.SideCtx("C", windows=[("scan", T0, T0 + 10)]),
                           tmp_path / "tc")
    assert dl["snapshots"]["rows"][0][2] == _iso(T0 + 105)
    # rows inserted in another order are a difference (rowid order, not sorted)
    cr = _atlas(tmp_path / "r.db", ["snp_000000000001", "snp_000000000002"], _iso(T0 + 1), reverse_files=True)
    dr, _ = eq.sqlite_dump(tmp_path / "r.db", rules, _ctx("C"), tmp_path / "tr")
    assert eq.first_json_diff(db, dr)[0] == "$.snapshot_files.rows[0][1]"
    # ... unless a named rule says calibration found that table's order nondeterministic
    eq.SORTED_TABLES[("b.db", "snapshot_files")] = eq.SORTED_TABLES[("r.db", "snapshot_files")] = "sorted_test"
    try:
        db2, fired2 = eq.sqlite_dump(tmp_path / "b.db", rules, _ctx("B"), tmp_path / "tb")
        dr2, _ = eq.sqlite_dump(tmp_path / "r.db", rules, _ctx("C"), tmp_path / "tr")
        assert db2["snapshot_files"]["rows"][0][1:] == dr2["snapshot_files"]["rows"][0][1:]
        assert "sorted_test" in fired2
    finally:
        eq.SORTED_TABLES.clear()
    # a real difference survives the rules; so does a changed user_version
    cc.execute("UPDATE snapshots SET n = 7 WHERE id = 'snp_eeeeeeeeeeee'")
    cc.execute("PRAGMA user_version = 3")
    cc.commit()
    dc, _ = eq.sqlite_dump(tmp_path / "c.db", rules, eq.SideCtx("C", windows=[("scan", T0 + 100, T0 + 110)]),
                           tmp_path / "tc")
    assert eq.first_json_diff(db, dc)[0] == "$.pragmas.user_version"
    dc["pragmas"]["user_version"] = 0
    assert eq.first_json_diff(db, dc)[0] == "$.snapshots.rows[1][3]"
    for con in (cb, cc, cr):
        con.close()


def _claims_db(p: Path, ids: list[str], recorded_ns: int):
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE claims (id TEXT PRIMARY KEY, status TEXT, created_at TEXT, supersedes TEXT)")
    con.execute("CREATE TABLE claim_history (seq INTEGER PRIMARY KEY AUTOINCREMENT, claim_id TEXT, payload TEXT, "
                "created_at TEXT)")
    con.execute("CREATE TABLE file_stat (path TEXT PRIMARY KEY, mtime_ns INTEGER, recorded_at_ns INTEGER)")
    for i, cid in enumerate(ids):
        con.execute("INSERT INTO claims VALUES (?,?,?,?)", (cid, "stale", _iso(T0 + 2), ids[0] if i else None))
        con.execute("INSERT INTO claim_history (claim_id, payload, created_at) VALUES (?,?,?)",
                    (cid, json.dumps({"claim": cid, "at": _iso(T0 + 22)}), _iso(T0 + 22)))
    con.execute("INSERT INTO file_stat VALUES (?,?,?)", ("a.py", 5, recorded_ns))
    con.commit()
    con.close()


def test_claim_ids_are_numbered_in_rowid_order_and_clock_columns_must_fall_in_a_run(tmp_path):
    rules = eq._RULES_BY_ARTIFACT["atlas.db"]
    _claims_db(tmp_path / "b.db", ["clm_00000000000a", "clm_00000000000b"], int((T0 + 25.5) * 1e9))
    _claims_db(tmp_path / "c.db", ["clm_ffffffffffff", "clm_111111111111"], int((T0 + 25.25) * 1e9))
    db, fired = eq.sqlite_dump(tmp_path / "b.db", rules, _ctx("B"), tmp_path / "t")
    dc, _ = eq.sqlite_dump(tmp_path / "c.db", rules, _ctx("C"), tmp_path / "t")
    assert db == dc and set(fired) == {"atlas_clock_columns", "atlas_random_ids"}
    assert db["claims"]["rows"][1] == [2, "<clm#2>", "stale", "<CLOCK run scan dddd-dd-ddTdd:dd:dd+dd:dd>", "<clm#1>"]
    assert db["claim_history"]["rows"][0][3:] == [
        json.dumps({"claim": "<clm#1>", "at": f"<CLOCK run add_function {ISO}>"}), f"<CLOCK run add_function {ISO}>"]
    assert db["file_stat"]["rows"][0][3] == "<CLOCK run add_function ns>"
    # recorded_at_ns written as whole seconds: another kind of value, so it differs
    _claims_db(tmp_path / "w.db", ["clm_ffffffffffff", "clm_111111111111"], int(T0 + 25) * 10**9)
    dw, _ = eq.sqlite_dump(tmp_path / "w.db", rules, _ctx("C"), tmp_path / "t")
    assert eq.first_json_diff(db, dw)[0] == "$.file_stat.rows[0][3]"
    # recorded_at_ns a day ahead (mutant h) is outside every run: kept, so it differs
    _claims_db(tmp_path / "h.db", ["clm_ffffffffffff", "clm_111111111111"], int((T0 + 86_425) * 1e9))
    dh, _ = eq.sqlite_dump(tmp_path / "h.db", rules, _ctx("C"), tmp_path / "t")
    assert eq.first_json_diff(db, dh)[0] == "$.file_stat.rows[0][3]"


def test_search_meta_rules(tmp_path):
    con = sqlite3.connect(tmp_path / "search.db")
    con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    con.executemany("INSERT INTO meta VALUES (?,?)", [
        ("graph", '{"mtime_ns": 123, "sha256": "h", "size": 9}'), ("built_at", str(T0 + 4.42)), ("n", "4")])
    con.commit()
    con.close()
    rules = eq._RULES_BY_ARTIFACT["index/search.db"]
    d, fired = eq.sqlite_dump(tmp_path / "search.db", rules, _ctx("B", graph_mtimes={123: "scan"}), tmp_path / "t")
    assert d["meta"]["rows"] == [[1, "graph", '{"mtime_ns": "<GRAPH_MTIME after scan>", "sha256": "h", "size": 9}'],
                                 [2, "built_at", "<CLOCK run scan text-float>"], [3, "n", "4"]]
    assert set(fired) == {"search_meta_built_at", "search_meta_graph_mtime"}
    d, _ = eq.sqlite_dump(tmp_path / "search.db", rules, eq.SideCtx("C", graph_mtimes={5: "scan"}), tmp_path / "t")
    assert d["meta"]["rows"][0][2].startswith('{"mtime_ns": 123')
    assert d["meta"]["rows"][1][2] == str(T0 + 4.42)  # no run of C: the clock is compared as it is


# -- files -----------------------------------------------------------------------------------------------------------

def test_the_exclusions_are_few_and_named():
    assert eq.excluded("index/cache/ast/x.json") and eq.excluded("build.lock")
    assert eq.excluded("index/.rebuild.lock")
    assert eq.excluded("atlas.db-wal") and eq.excluded("index/search.db-shm")
    for rel in ("atlas.db", "index/graph.json", "config.json", ".gitignore", "background_update.log",
                "index/x.tmp", "atlas.db-journal", "index/cachefile.json", "index/build.lock", "other.lock",
                "index/eq.lock", "decisions/.rebuild.lock"):
        assert not eq.excluded(rel)


def _noon(day: str) -> float:
    return time.mktime(time.strptime(day + " 12:00:00", "%Y-%m-%d %H:%M:%S"))  # local noon of that day


def test_artifact_names_of_dated_backups():
    s = eq.SideCtx("B", windows=[("scan", _noon("2026-09-30"), _noon("2026-09-30") + 5),
                                 ("add_function", _noon("2026-10-01"), _noon("2026-10-01") + 5)])
    names = eq.artifact_names(["index/graph.json", "index/2026-09-30/graph.json", "index/2026-10-01/GRAPH_REPORT.md",
                               "index/2026-9-30/x", "atlas.db", "index/2026-09-29/graph.json"], s)
    assert names["index/graph.json"] == ("index/graph.json", "index/graph.json")
    assert names["index/2026-09-30/graph.json"] == ("index/<DATE>/graph.json", "index/graph.json")
    assert names["index/2026-10-01/GRAPH_REPORT.md"] == ("index/<DATE+1>/GRAPH_REPORT.md", "index/GRAPH_REPORT.md")
    assert names["index/2026-9-30/x"] == ("index/2026-9-30/x", "index/2026-9-30/x")
    # a date on which no run of this side ran (mutant m2: yesterday) keeps its name, so it is compared; its
    # files still take the rules of the files they copy
    assert names["index/2026-09-29/graph.json"] == ("index/2026-09-29/graph.json", "index/graph.json")
    # the other side's dates do not count
    other = eq.SideCtx("C", windows=[("scan", _noon("2026-09-29"), _noon("2026-09-29") + 5)])
    assert eq.artifact_names(["index/2026-09-30/graph.json"], other)["index/2026-09-30/graph.json"][0] == \
        "index/2026-09-30/graph.json"


def test_tree_state_and_changes(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "x.json").write_bytes(b"1")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_bytes(b"ref")
    before = eq.tree_state(tmp_path, skip_root=(".git",))
    assert list(before) == ["a/x.json"]
    (tmp_path / "a" / "x.json").write_bytes(b"2")
    (tmp_path / "b.txt").write_bytes(b"")
    after = eq.tree_state(tmp_path, skip_root=(".git",))
    assert eq.state_changes(before, after) == ["modified a/x.json", "added b.txt"]
    st = after["b.txt"]
    os.utime(tmp_path / "b.txt", ns=(st[1] + 10**9, st[1] + 10**9))
    assert eq.state_changes(after, eq.tree_state(tmp_path, skip_root=(".git",))) == ["modified b.txt (stat only)"]
    assert eq.state_changes(after, before)[1] == "removed b.txt"


def test_tree_state_skips_git_and_verinoda_only_at_the_root(tmp_path):
    for rel in (".git/HEAD", ".verinoda/atlas.db", "sub/.verinoda/x.json", "sub/.git/config", "a.py"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_bytes(b"x")
    assert list(eq.tree_state(tmp_path, skip_root=(".git", ".verinoda"))) == \
        ["a.py", "sub/.git/config", "sub/.verinoda/x.json"]


def test_git_files_leave_out_only_objects_index_logs_and_leftovers(tmp_path):
    g = tmp_path / ".git"
    for rel in ("HEAD", "config", "index", "ORIG_HEAD", "FETCH_HEAD", "COMMIT_EDITMSG", "info/exclude",
                "hooks/pre-commit", "objects/pack/p.pack", "objects/pack/p.idx", "objects/pack/p.rev",
                "objects/info/packs", "objects/ab/cdef0123", "logs/HEAD", "refs/heads/main", "verinoda-eq",
                "packed-refs"):
        (g / rel).parent.mkdir(parents=True, exist_ok=True)
        (g / rel).write_bytes(b"x")
    # the object store is compared (loose objects and packs); only objects/info and the packs' .idx are not
    assert list(eq.tree_state(g, skip=eq.git_uncompared)) == [
        "COMMIT_EDITMSG", "HEAD", "config", "hooks/pre-commit", "info/exclude", "objects/ab/cdef0123",
        "objects/pack/p.pack", "objects/pack/p.rev", "packed-refs", "refs/heads/main", "verinoda-eq"]


# -- edit plans --------------------------------------------------------------------------------------------------------

def _orders(r: Path):
    (r / "orders").mkdir(parents=True)
    (r / "docs").mkdir()
    (r / "docs" / "a.md").write_text("# A\n", encoding="utf-8")
    (r / "README.md").write_text("# R\n", encoding="utf-8")
    (r / "orders" / "__init__.py").write_text('"""Orders."""\n', encoding="utf-8")
    (r / "orders" / "pricing.py").write_text("def compute_total(items):\n    return sum(items)\n", encoding="utf-8")
    (r / "orders" / "service.py").write_text("def place(x):\n    return compute_total(x)\n", encoding="utf-8")
    (r / "orders" / "repository.py").write_text("class Repo:\n    def save(self, x):\n        return x\n",
                                                encoding="utf-8")
    (r / "orders" / "api.py").write_text("def create():\n    return 1", encoding="utf-8")
    (r / "orders" / "config.py").write_text("X = 1\n", encoding="utf-8")
    eq.add_untracked(r)
    for p in r.rglob("*"):
        if p.is_file():
            os.utime(p, (1_700_000_000, 1_700_000_000))


def test_one_plan_applied_to_both_copies_gives_the_same_trees(tmp_path):
    b, c = tmp_path / "b", tmp_path / "c"
    _orders(b)
    _orders(c)
    commits = []
    skipped = []
    start = int(time.time())
    for step, name in enumerate(eq.DEFAULT_EDITS, 1):
        ops = eq.PLANS[name](b)
        if ops is None:
            skipped.append(name)
            continue
        when = (start + step) * 10**9
        for r in (b, c):
            eq.apply_plan(r, ops, when, git=lambda repo, *a: commits.append((repo.name, a)))
        assert eq.tree_state(b) == eq.tree_state(c), name
    assert skipped == ["retarget_call", "edit_objc_pair", "edit_go", "edit_go_mod", "edit_java", "edit_rust"]
    assert [n for n, _a in commits] == ["b", "b", "c", "c"]  # add -A and commit, on each side
    assert not (b / "docs").exists() and not (b / "orders" / "config.py").exists()
    assert (b / "orders" / "api_renamed.py").read_text(encoding="utf-8").endswith("# eq: a comment and nothing else\n")
    assert "return (compute_total(x)) if True else None" in (b / "orders" / "service.py").read_text()
    assert "    def savx(self, x):" in (b / "orders" / "repository.py").read_text()  # same_size: the original file
    assert (b / "orders" / "api_renamed.py").read_text().startswith("def creatx():")  # racy_same_size
    assert "changed outside git" in (b / eq.UNTRACKED_REL).read_text()
    assert (b / "orders" / "__init__.py").read_text() == '"""Orders."""  # eq: edited\n'  # human_edit
    assert json.loads((b / "eq_config" / "package.json").read_text())["name"] == "eq-config"
    assert "eqTotal" in (b / "eq_web" / "helpers.ts").read_text()
    assert "def eq_fast_added():" in (b / "orders" / "service.py").read_text()


def test_retarget_call_moves_one_edge_and_keeps_the_line_length():
    src = "def alpha():\n    return 1\n\n\ndef bravo():\n    return 2\n\n\ndef caller():\n    return alpha()\n"
    new, caller, old, target = eq.retarget_call(src)
    assert (caller, old, target) == ("caller", "alpha", "bravo")
    assert new == src.replace("return alpha()", "return bravo()")
    # a shorter name is padded before the line end (CRLF kept); a method may be the caller
    src = ("def compute_score(d):\r\n    return sum(d)\r\n\r\ndef norm(v):\r\n    return v\r\n\r\n"
           "def run_analysis(d):\r\n    return d\r\n\r\nclass A:\r\n    def process(self, d):\r\n"
           "        return run_analysis(d)\r\n")
    new, caller, old, target = eq.retarget_call(src)
    assert (caller, old, target) == ("process", "run_analysis", "norm")  # compute_score is longer
    assert "        return norm(d)        \r\n" in new and len(new) == len(src)
    # the caller already names every other function (the edge would merge), a call named twice, one function
    assert eq.retarget_call("def a():\n    return b()\n\n\ndef b():\n    return a()\n") is None
    assert eq.retarget_call("def a():\n    return 1\n\n\ndef b():\n    return c(a) + a()\n\n\n"
                            "def c(x):\n    return x\n") is None
    assert eq.retarget_call("def a():\n    return a()\n") is None
    assert eq.retarget_call("def (:") is None
    # no other name fits in length: a longer one is used, the line grows
    src = "def ab():\n    return 1\n\n\ndef longer_name():\n    return 2\n\n\ndef c():\n    return ab()\n"
    assert eq.retarget_call(src)[0].endswith("def c():\n    return longer_name()\n")


def test_retarget_plan_picks_a_file_of_the_corpus(tmp_path):
    _orders(tmp_path)
    assert eq.plan_retarget_call(tmp_path) is None  # no file there has two functions and a call between them
    (tmp_path / "orders" / "service.py").write_bytes(
        b"def validate(x):\n    return x\n\n\ndef place(x):\n    validate(x)\n    return x\n\n\n"
        b"def fetch(x):\n    return x\n")
    ops = eq.plan_retarget_call(tmp_path)
    assert [o.rel for o in ops] == ["orders/service.py"]
    assert b"    fetch(x)   \n" in ops[0].data


def test_listing_names_files_and_folders_without_times(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "tmp" / "empty").mkdir(parents=True)
    (tmp_path / "config" / "trust.json").write_bytes(b"{}")
    got = eq.listing(tmp_path)
    assert list(got) == ["config/", "config/trust.json", "tmp/", "tmp/empty/"]
    assert got["config/trust.json"][0] == 2 and got["tmp/empty/"] == "dir"
    os.utime(tmp_path / "config" / "trust.json", (1, 1))
    assert eq.listing(tmp_path) == got
    assert eq.listing(tmp_path / "missing") == {}


def test_c0_accepts_the_baseline_stamps_besides_its_own():
    s = eq.SideCtx("C0", stamps={"extraction": "s8-cand", "server_version": "v+c"},
                   also={"extraction": "s8-base", "server_version": "v+b"})
    for v in ("s8-cand", "s8-base"):
        assert eq.mask_text("index/build_stats.json", '{"extraction": "%s"}' % v, s)[0] == \
            '{"extraction": "<EXTRACTION_STAMP>"}'
    assert eq.mask_text("index/build_stats.json", '{"extraction": "s8-old"}', s)[0] == '{"extraction": "s8-old"}'
    row = {"key": "schema_written_by", "value": "v+b"}
    assert eq._db_written_by("meta", row, s) == 1 and row["value"] == "<SERVER_VERSION>"


def test_same_size_edit_puts_the_old_mtime_back(tmp_path):
    _orders(tmp_path)
    (tmp_path / "eq_dup").mkdir()
    (tmp_path / "eq_dup" / "pricing.py").write_text("def a():\n    pass\n", encoding="utf-8")
    p = tmp_path / "orders" / "repository.py"
    before = p.stat()
    ops = eq.plan_same_size_keep_mtime(tmp_path)
    assert [o.rel for o in ops] == ["orders/repository.py"]  # never the harness's own eq_* files
    eq.apply_plan(tmp_path, ops, 1_900_000_000 * 10**9)
    after = p.stat()
    assert p.read_text(encoding="utf-8").startswith("class Repo:\n    def savx(self, x):")
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)
    assert f"(mtime {before.st_mtime_ns // 10**9})" in eq.plan_text(ops)


def test_racy_plans_move_the_mtime_ahead_and_keep_it_through_a_same_size_edit(tmp_path):
    _orders(tmp_path)
    p = tmp_path / "orders" / "api.py"
    data = p.read_bytes()
    assert eq.plan_racy_same_size(tmp_path, now=1_790_000_000) is None  # nothing is ahead yet
    ops = eq.plan_racy_prepare(tmp_path, now=1_790_000_000)
    eq.apply_plan(tmp_path, ops, 1_790_000_001 * 10**9)
    assert p.read_bytes() == data and p.stat().st_mtime_ns == (1_790_000_000 + eq.RACY_AHEAD) * 10**9
    ops = eq.plan_racy_same_size(tmp_path, now=1_790_000_100)
    eq.apply_plan(tmp_path, ops, 1_790_000_101 * 10**9)
    assert p.read_text() == "def creatx():\n    return 1" and len(p.read_bytes()) == len(data)
    assert p.stat().st_mtime_ns == (1_790_000_000 + eq.RACY_AHEAD) * 10**9


def test_json_and_ts_plans_edit_an_existing_file_when_there_is_one(tmp_path):
    (tmp_path / "sample.json").write_text('{\n  "name": "my-app"\n}\n', encoding="utf-8")
    (tmp_path / "sample.ts").write_text("function buildHeaders(t: string) {\n    return t;\n}\n", encoding="utf-8")
    eq.apply_plan(tmp_path, eq.plan_json(tmp_path), 1_790_000_000 * 10**9)
    eq.apply_plan(tmp_path, eq.plan_ts(tmp_path), 1_790_000_000 * 10**9)
    assert json.loads((tmp_path / "sample.json").read_text()) == {"eqAdded": "by the equality harness",
                                                                   "name": "my-app"}
    assert 'return buildHeaders("eq");' in (tmp_path / "sample.ts").read_text()


def test_claim_targets_and_decision_args(tmp_path):
    _orders(tmp_path)
    t = eq.claim_targets(tmp_path)
    assert list(t) == ["body", "defn", "untracked", "steady"]
    assert t["body"] == ("the function returns its result: contains: return sum(items)", "orders/pricing.py:2") or \
        t["body"][1] == "orders/service.py:2"
    assert t["defn"] == ("compute_total is defined here: contains: def compute_total(items):", "orders/pricing.py:1")
    assert t["untracked"][1] == "eq_local/notes.py:1-2"
    assert t["steady"] == ('the file starts so: contains: """Orders."""', "orders/__init__.py:1")
    args = eq.decision_args(tmp_path)
    assert args[:2] == ["decide", "record"] and args[-2:] == ["--governs", "orders/pricing.py::compute_total"]
    assert (tmp_path / ".gitignore").read_text() == "eq_local/\n"


def test_written_files_get_the_step_time(tmp_path):
    _orders(tmp_path)
    eq.apply_plan(tmp_path, eq.plan_add_data_file(tmp_path), 1_900_000_123 * 10**9)
    assert (tmp_path / "eq_notes" / "notes.txt").stat().st_mtime_ns == 1_900_000_123 * 10**9


# -- the run wrapper ---------------------------------------------------------------------------------------------------

def _fake_tree(root: Path, marker: str) -> Path:
    pkg = root / "verinoda"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "cli.py").write_text(f"import sys\nprint({marker!r}, sys.argv[1:])\nsys.exit(3)\n", encoding="utf-8")
    return root


def test_the_wrapper_runs_the_intended_tree_and_refuses_another(tmp_path):
    good = _fake_tree(tmp_path / "good", "from-good")
    other = _fake_tree(tmp_path / "other", "from-other")
    env = dict(os.environ, PYTHONPATH=str(good))
    r = subprocess.run([sys.executable, "-c", eq.WRAPPER, str(good), "cli", "update", "x"], cwd=good, env=env,
                       capture_output=True, text=True, check=False)
    assert r.returncode == 3 and "from-good ['update', 'x']" in r.stdout
    r = subprocess.run([sys.executable, "-c", eq.WRAPPER, str(other), "cli", "update"], cwd=tmp_path, env=env,
                       capture_output=True, text=True, check=False)
    assert r.returncode == eq.WRAPPER_EXIT and "eq-wrapper:" in r.stderr
    r = subprocess.run([sys.executable, "-c", eq.WRAPPER, str(good), "code", "import sys; print(sys.argv[1:])", "a"],
                       cwd=good, env=env, capture_output=True, text=True, check=False)
    assert r.returncode == 0 and r.stdout.strip() == "['a']"


def test_schema_written_by_is_accepted_only_as_this_sides_build(tmp_path):
    con = sqlite3.connect(tmp_path / "atlas.db")
    con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    con.executemany("INSERT INTO meta VALUES (?,?)", [("schema_version", "6"), ("schema_written_by", "0.3.2+aaa")])
    con.commit()
    con.close()
    rules = eq._RULES_BY_ARTIFACT["atlas.db"]
    d, fired = eq.sqlite_dump(tmp_path / "atlas.db", rules, eq.SideCtx("B", stamps={"server_version": "0.3.2+aaa"}),
                              tmp_path / "t")
    assert d["meta"]["rows"][1] == [2, "schema_written_by", "<SERVER_VERSION>"] and fired == ["atlas_written_by"]
    d, _ = eq.sqlite_dump(tmp_path / "atlas.db", rules, eq.SideCtx("C", stamps={"server_version": "0.3.2+bbb"}),
                          tmp_path / "t")
    assert d["meta"]["rows"][1] == [2, "schema_written_by", "0.3.2+aaa"]
