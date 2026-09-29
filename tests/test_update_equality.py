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


def test_mask_changes_only_the_volatile_value_and_keeps_the_bytes_around_it():
    text = '{\n  "graph_seconds": 0.7,\n  "files":null, "at": 17.5,\n  "extraction": "s8-a"\n}\n'
    sb = eq.SideCtx("B", stamps={"extraction": "s8-a"})
    out, fired = eq.mask_text("index/build_stats.json", text, sb)
    assert out == ('{\n  "graph_seconds": "<CLOCK>",\n  "files":null, "at": "<CLOCK>",\n'
                   '  "extraction": "<EXTRACTION_STAMP>"\n}\n')
    assert fired == ["build_stats_clock", "extraction_stamp"]
    # a clock that is not a number is not a clock: kept, so the comparison sees it
    out, _ = eq.mask_text("index/build_stats.json", '{"at": "soon"}', sb)
    assert out == '{"at": "soon"}'
    # text that is not JSON is compared as it is
    assert eq.mask_text("index/build_stats.json", '{"at": 1', sb) == ('{"at": 1', [])


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
    assert eq.mask_text("update_result", upd, sc)[0] == \
        '{"extraction": {"was": "<EXTRACTION_STAMP>", "now": "<EXTRACTION_STAMP>"}, "seconds": "<DURATION>"}'
    assert eq.mask_text("update_result", upd, sb)[0].startswith('{"extraction": {"was": "s8-cand"')


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
    sb = eq.SideCtx("B", snapshots={"snp_a": "<SNAPSHOT1>", "snp_b": "<SNAPSHOT2>"})
    text = ('{"snapshot": {"id": "snp_b", "file_count": 3, "created_at": "2026-09-29T23:17:04+00:00"}, '
            '"index_seconds": 0.6, "derived": {"lexicon": {"seconds": 2, "units": 4}, "anchors": {"ms": 2.4}}}')
    out, fired = eq.mask_text("update_result", text, sb)
    assert json.loads(out) == {"snapshot": {"id": "<SNAPSHOT2>", "file_count": 3, "created_at": "<CLOCK>"},
                               "index_seconds": "<DURATION>",
                               "derived": {"lexicon": {"seconds": "<DURATION>", "units": 4},
                                           "anchors": {"ms": "<DURATION>"}}}
    assert set(fired) == {"update_snapshot", "update_snapshot_clock", "update_timings"}
    # an id this side's atlas.db does not hold is compared as it is
    assert '"snp_zzz"' in eq.mask_text("update_result", '{"snapshot": {"id": "snp_zzz"}}', sb)[0]


def test_manifest_and_lexicon_and_report_rules():
    s = eq.SideCtx("B")
    man = '{\n  "a.py": {\n    "mtime": 1.5,\n    "seen": 1790.25,\n    "ast_hash": "h"\n  }\n}'
    assert eq.mask_text("index/manifest.json", man, s)[0] == man.replace("1790.25", '"<CLOCK>"')
    lex = '{"version":1,"built_at":"2026-09-29T23:17:05+00:00","units":21}'
    assert eq.mask_text("index/lexicon.json", lex, s)[0] == '{"version":1,"built_at":"<CLOCK>","units":21}'
    rep = "# Graph Report - orders  (2026-09-30)\n\nBuilt 2026-09-30\n"
    assert eq.mask_text("index/GRAPH_REPORT.md", rep, s) == \
        ("# Graph Report - orders  (<DATE>)\n\nBuilt 2026-09-30\n", ["report_date"])


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
    cb = _atlas(tmp_path / "b.db", ["snp_aaa", "snp_bbb"], "2026-01-01T00:00:00+00:00")
    cc = _atlas(tmp_path / "c.db", ["snp_zzz", "snp_yyy"], "2026-01-02T00:00:00+00:00")
    sb, sc = eq.SideCtx("B"), eq.SideCtx("C")
    db, fired = eq.sqlite_dump(tmp_path / "b.db", rules, sb, tmp_path / "tb")
    dc, _ = eq.sqlite_dump(tmp_path / "c.db", rules, sc, tmp_path / "tc")
    assert db == dc
    assert set(fired) == {"atlas_clock_columns", "atlas_snapshot_ids"}
    assert sb.snapshots == {"snp_aaa": "<SNAPSHOT1>", "snp_bbb": "<SNAPSHOT2>"}
    assert db["pragmas"]["journal_mode"] == "wal"  # read from a copy that holds the WAL
    assert db["snapshots"]["columns"] == ["<rowid>", "id", "created_at", "n"]
    assert db["snapshots"]["rows"] == [[1, "<SNAPSHOT1>", "<CLOCK>", 0], [2, "<SNAPSHOT2>", "<CLOCK>", 1]]
    assert db["snapshot_files"]["rows"][0] == [1, "<SNAPSHOT1>", "a.py"]
    assert db["sqlite_sequence"]["rows"] == [[1, "log", 2]]
    assert db["terms"] == {"columns": ["term", "n"], "rows": [["alpha", 2], ["zeta", 1]]}  # WITHOUT ROWID: key order
    assert [r[1] for r in db["sqlite_master"]][:2] == ["snapshots", "sqlite_autoindex_snapshots_1"]
    # the originals were never opened by the dump: no copy is left behind either
    assert not list((tmp_path / "tb").iterdir())
    # rows inserted in another order are a difference (rowid order, not sorted)
    cr = _atlas(tmp_path / "r.db", ["snp_q", "snp_r"], "2026-01-02T00:00:00+00:00", reverse_files=True)
    dr, _ = eq.sqlite_dump(tmp_path / "r.db", rules, eq.SideCtx("C"), tmp_path / "tr")
    assert eq.first_json_diff(db, dr)[0] == "$.snapshot_files.rows[0][1]"
    # ... unless a named rule says calibration found that table's order nondeterministic
    eq.SORTED_TABLES[("b.db", "snapshot_files")] = eq.SORTED_TABLES[("r.db", "snapshot_files")] = "sorted_test"
    try:
        db2, fired2 = eq.sqlite_dump(tmp_path / "b.db", rules, eq.SideCtx("B"), tmp_path / "tb")
        dr2, _ = eq.sqlite_dump(tmp_path / "r.db", rules, eq.SideCtx("C"), tmp_path / "tr")
        assert db2["snapshot_files"]["rows"][0][1:] == dr2["snapshot_files"]["rows"][0][1:]
        assert "sorted_test" in fired2
    finally:
        eq.SORTED_TABLES.clear()
    # a real difference survives the rules; so does a changed user_version
    cc.execute("UPDATE snapshots SET n = 7 WHERE id = 'snp_yyy'")
    cc.execute("PRAGMA user_version = 3")
    cc.commit()
    dc, _ = eq.sqlite_dump(tmp_path / "c.db", rules, eq.SideCtx("C"), tmp_path / "tc")
    assert eq.first_json_diff(db, dc)[0] == "$.pragmas.user_version"
    dc["pragmas"]["user_version"] = 0
    assert eq.first_json_diff(db, dc)[0] == "$.snapshots.rows[1][3]"
    for con in (cb, cc, cr):
        con.close()


def test_search_meta_rules(tmp_path):
    con = sqlite3.connect(tmp_path / "search.db")
    con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    con.executemany("INSERT INTO meta VALUES (?,?)", [
        ("graph", '{"mtime_ns": 123, "sha256": "h", "size": 9}'), ("built_at", "1790723822.42"), ("n", "4")])
    con.commit()
    con.close()
    rules = eq._RULES_BY_ARTIFACT["index/search.db"]
    d, fired = eq.sqlite_dump(tmp_path / "search.db", rules, eq.SideCtx("B", graph_mtimes={123: "scan"}),
                              tmp_path / "t")
    assert d["meta"]["rows"] == [[1, "graph", '{"mtime_ns": "<GRAPH_MTIME after scan>", "sha256": "h", "size": 9}'],
                                 [2, "built_at", "<CLOCK>"], [3, "n", "4"]]
    assert set(fired) == {"search_meta_built_at", "search_meta_graph_mtime"}
    d, _ = eq.sqlite_dump(tmp_path / "search.db", rules, eq.SideCtx("C", graph_mtimes={5: "scan"}), tmp_path / "t")
    assert d["meta"]["rows"][0][2].startswith('{"mtime_ns": 123')


# -- files -----------------------------------------------------------------------------------------------------------

def test_the_exclusions_are_few_and_named():
    assert eq.excluded("index/cache/ast/x.json") and eq.excluded("build.lock")
    assert eq.excluded("atlas.db-wal") and eq.excluded("index/search.db-shm")
    for rel in ("atlas.db", "index/graph.json", "config.json", ".gitignore", "background_update.log",
                "index/x.tmp", "atlas.db-journal", "index/cachefile.json"):
        assert not eq.excluded(rel)


def test_artifact_names_of_dated_backups():
    names = eq.artifact_names(["index/graph.json", "index/2026-09-30/graph.json", "index/2026-10-01/GRAPH_REPORT.md",
                               "index/2026-9-30/x", "atlas.db"])
    assert names["index/graph.json"] == ("index/graph.json", "index/graph.json")
    assert names["index/2026-09-30/graph.json"] == ("index/<DATE>/graph.json", "index/graph.json")
    assert names["index/2026-10-01/GRAPH_REPORT.md"] == ("index/<DATE+1>/GRAPH_REPORT.md", "index/GRAPH_REPORT.md")
    assert names["index/2026-9-30/x"] == ("index/2026-9-30/x", "index/2026-9-30/x")


def test_tree_state_and_changes(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "x.json").write_bytes(b"1")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_bytes(b"ref")
    before = eq.tree_state(tmp_path, skip_dirs=(".git",))
    assert list(before) == ["a/x.json"]
    (tmp_path / "a" / "x.json").write_bytes(b"2")
    (tmp_path / "b.txt").write_bytes(b"")
    after = eq.tree_state(tmp_path, skip_dirs=(".git",))
    assert eq.state_changes(before, after) == ["modified a/x.json", "added b.txt"]
    st = after["b.txt"]
    os.utime(tmp_path / "b.txt", ns=(st[1] + 10**9, st[1] + 10**9))
    assert eq.state_changes(after, eq.tree_state(tmp_path, skip_dirs=(".git",))) == ["modified b.txt (stat only)"]
    assert eq.state_changes(after, before)[1] == "removed b.txt"


# -- edit plans --------------------------------------------------------------------------------------------------------

def _orders(r: Path):
    (r / "orders").mkdir(parents=True)
    (r / "docs").mkdir()
    (r / "docs" / "a.md").write_text("# A\n", encoding="utf-8")
    (r / "README.md").write_text("# R\n", encoding="utf-8")
    (r / "orders" / "pricing.py").write_text("def compute_total(items):\n    return sum(items)\n", encoding="utf-8")
    (r / "orders" / "service.py").write_text("def place(x):\n    return compute_total(x)\n", encoding="utf-8")
    (r / "orders" / "repository.py").write_text("def save(x):\n    return x\n", encoding="utf-8")
    (r / "orders" / "api.py").write_text("def create():\n    return 1", encoding="utf-8")
    (r / "orders" / "config.py").write_text("X = 1\n", encoding="utf-8")
    for p in r.rglob("*"):
        if p.is_file():
            os.utime(p, (1_700_000_000, 1_700_000_000))


def test_one_plan_applied_to_both_copies_gives_the_same_trees(tmp_path):
    b, c = tmp_path / "b", tmp_path / "c"
    _orders(b)
    _orders(c)
    commits = []
    skipped = []
    for step, name in enumerate(eq.DEFAULT_EDITS, 1):
        ops = eq.PLANS[name](b)
        if ops is None:
            skipped.append(name)
            continue
        when = (1_800_000_000 + step) * 10**9
        for r in (b, c):
            eq.apply_plan(r, ops, when, git=lambda repo, *a: commits.append((repo.name, a)))
        assert eq.tree_state(b) == eq.tree_state(c), name
    assert skipped == ["edit_objc_pair", "edit_go", "edit_go_mod"]
    assert [n for n, _a in commits] == ["b", "b", "c", "c"]  # add -A and commit, on each side
    assert not (b / "docs").exists() and not (b / "orders" / "config.py").exists()
    assert (b / "orders" / "api_renamed.py").read_text(encoding="utf-8").endswith("# eq: a comment and nothing else\n")
    assert "return (compute_total(x)) if True else None" in (b / "orders" / "service.py").read_text()


def test_same_size_edit_puts_the_old_mtime_back(tmp_path):
    _orders(tmp_path)
    p = tmp_path / "orders" / "repository.py"
    before = p.stat()
    ops = eq.plan_same_size_keep_mtime(tmp_path)
    eq.apply_plan(tmp_path, ops, 1_900_000_000 * 10**9)
    after = p.stat()
    assert p.read_text(encoding="utf-8").startswith("def savx(x):")
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)
    assert "(old mtime)" in eq.plan_text(ops)


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
