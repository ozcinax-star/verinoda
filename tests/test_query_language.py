"""The graph query language (verinoda/graphquery.py, `verinoda q`): patterns, joins, bounded paths, negation,
count, precise errors, budgets, and rows that cite their evidence with an honest status."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import json  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import cli, graphquery, workflow  # noqa: E402
from verinoda.store import open_store  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")

WEB = '''from flask import Flask, request

from app.service import audit, create_order, list_orders, persist
from app.store import Repo

app = Flask(__name__)


@app.route("/orders", methods=["POST"])
def post_order():
    return create_order(request.json)


@app.route("/orders", methods=["GET"])
def get_orders():
    return list_orders()


@app.route("/items", methods=["PUT"])
def put_item():
    return persist(Repo(), 1)


def not_a_handler():
    return audit("x")
'''

SERVICE = '''from app.store import Repo, read_orders, save_order, write_log


def create_order(data):
    check(data)
    return save_order(data)


def check(data):
    return bool(data)


def list_orders():
    return read_orders()


def audit(msg):
    write_log(msg)


def persist(repo: Repo, x):
    repo.save(x)
'''

STORE = '''import sqlite3

DB = sqlite3.connect(":memory:")


def save_order(data):
    DB.execute("INSERT INTO orders VALUES (?)", (data,))
    DB.commit()
    return 1


def read_orders():
    return DB.execute("SELECT * FROM orders").fetchall()


def write_log(msg):
    DB.execute("INSERT INTO log VALUES (?)", (msg,))


def unused():
    return 0


class Repo:
    def save(self, x):
        DB.execute("UPDATE items SET x = ?", (x,))
'''

TESTS = '''from app.service import check


def test_check():
    assert check({"a": 1})
'''

WRITES = r'f.text ~ "INSERT|UPDATE|DELETE|\.commit\("'
DONE_WHEN = f"match (h:handler)-[calls*1..4]->(f:function) where {WRITES} return f, h"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false",
                    "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main", *args], cwd=cwd, check=True,
                   capture_output=True, stdin=subprocess.DEVNULL)


def _make(root: Path) -> Path:
    for rel, text in {".gitignore": ".verinoda/\n", "app/__init__.py": "", "app/web.py": WEB,
                      "app/service.py": SERVICE, "app/store.py": STORE, "tests/test_service.py": TESTS}.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "one")
    workflow.init(root)
    st = open_store(root)
    try:
        workflow.scan(st, root)
    finally:
        st.close()
    return root


@pytest.fixture(scope="module")
def proj(tmp_path_factory) -> Path:
    return _make(tmp_path_factory.mktemp("gq") / "shop ğ")


def _q(proj, text, **kw):
    return graphquery.run(proj, text, **kw)


def _names(res, *cols):
    out = []
    for r in res["rows"]:
        vals = []
        for c in cols:
            v = r["values"][c]
            vals.append(v["label"] if isinstance(v, dict) else v)
        out.append(tuple(vals))
    return sorted(out)


# -- the done-when query ----------------------------------------------------------------------------------

def test_functions_that_write_storage_reachable_from_an_http_handler_return_their_sites(proj):
    res = _q(proj, DONE_WHEN)
    assert res["complete"] and res["columns"] == ["f", "h"]
    # write_log() writes too, but only not_a_handler() reaches it; read_orders() is reached and only reads
    assert _names(res, "f", "h") == [(".save()", "put_item()"), ("save_order()", "post_order()")]
    row = next(r for r in res["rows"] if r["values"]["f"]["label"] == "save_order()")
    assert row["status"] == "strong_inference"
    assert row["values"]["f"]["at"] == "app/store.py:6" and row["values"]["h"]["at"] == "app/web.py:10"
    ev = row["evidence"]
    assert ev["routes"] == [{"var": "h", "route": "POST /orders", "at": "app/web.py:9", "framework": "flask"}]
    (path,) = ev["paths"]
    assert [(h["from"], h["to"], h["relation"], h["confidence"], h["at"]) for h in path] == [
        ("post_order()", "create_order()", "calls", "EXTRACTED", "app/web.py:11"),
        ("create_order()", "save_order()", "calls", "EXTRACTED", "app/service.py:6")]
    assert [(ln["at"], ln["text"]) for ln in ev["lines"]] == [
        ("app/store.py:7", 'DB.execute("INSERT INTO orders VALUES (?)", (data,))'), ("app/store.py:8", "DB.commit()")]
    text = graphquery.render(res)
    assert "route POST /orders at app/web.py:9 (h)" in text
    assert "path post_order() -calls-> create_order() (app/web.py:11) -calls-> save_order() (app/service.py:6)" in text
    assert "line app/store.py:8  DB.commit()" in text


def test_an_inferred_edge_makes_the_row_weak_and_verify_raises_only_extracted_rows(proj):
    res = _q(proj, DONE_WHEN, verify=True)
    by = {r["values"]["f"]["label"]: r for r in res["rows"]}
    weak = by[".save()"]
    assert weak["status"] == "weak_inference"
    assert [h["confidence"] for h in weak["evidence"]["paths"][0]] == ["EXTRACTED", "INFERRED"]
    assert weak["verified"]["result"] == "not confirmed"
    assert any("INFERRED" in p for p in weak["verified"]["problems"])
    assert any("INFERRED" in u for u in weak["uncertain"])
    strong = by["save_order()"]
    assert strong["status"] == "statically_verified" and strong["verified"]["result"] == "confirmed"
    assert "-calls?-> .save()" in graphquery.render(res)
    # without --verify nothing is above strong_inference
    assert set(_q(proj, DONE_WHEN)["statuses"]) == {"strong_inference", "weak_inference"}


# -- joins, paths, negation, count ----------------------------------------------------------------------

def test_a_variable_joins_two_patterns(proj):
    res = _q(proj, "match (a:function)-[calls]->(b:function), (b)-[calls]->(c:function) "
                   "where c.file = \"app/store.py\" return a, b, c")
    assert _names(res, "a", "b", "c") == [("get_orders()", "list_orders()", "read_orders()"),
                                          ("not_a_handler()", "audit()", "write_log()"),
                                          ("post_order()", "create_order()", "save_order()"),
                                          ("put_item()", "persist()", ".save()")]
    row = res["rows"][0]
    assert len(row["evidence"]["paths"]) == 2
    # two variables compared with each other
    same = _q(proj, "match (a:function)-[calls]->(b:function) where a.file = b.file return a, b")
    assert _names(same, "a", "b") == [("create_order()", "check()")]


def test_bounded_paths_and_directions(proj):
    two = _q(proj, 'match (h)-[calls*2]->(f) where h.name = "post_order" return f')
    assert _names(two, "f") == [("check()",), ("save_order()",)]
    upto = _q(proj, 'match (h)-[calls*0..1]->(f) where h.name = "create_order" return f')
    assert _names(upto, "f") == [("check()",), ("create_order()",), ("save_order()",)]
    callers = _q(proj, 'match (f)<-[calls*1..3]-(h:handler) where f.name = "save_order" return h')
    assert _names(callers, "h") == [("post_order()",)]
    either = _q(proj, 'match (f)-[calls]-(x:function) where f.name = "create_order" return x')
    assert _names(either, "x") == [("check()",), ("post_order()",), ("save_order()",)]
    contains = _q(proj, 'match (c:class)-[method]->(m:method) return c, m')
    assert _names(contains, "c", "m") == [("Repo", ".save()")]


def test_not_exists_reads_an_absence_and_says_so(proj):
    res = _q(proj, 'match (f:function) where f.file glob "app/*" and not exists (f)<-[calls]-() return f')
    assert _names(res, "f") == [("get_orders()",), ("not_a_handler()",), ("post_order()",), ("put_item()",),
                                ("unused()",)]
    assert all(r["status"] == "strong_inference" for r in res["rows"])
    assert all(any("absence" in u for u in r["uncertain"]) for r in res["rows"])
    assert any("NOT EXISTS" in n for n in res["notes"])
    untested = _q(proj, 'match (f:function) where f.file = "app/service.py" '
                        'and not exists (t:test)-[calls*1..3]->(f) return f.name')
    assert _names(untested, "f.name") == [("audit",), ("create_order",), ("list_orders",), ("persist",)]
    # an absence is never verified by re-reading lines
    ver = _q(proj, 'match (f:function) where f.name = "unused" and not exists (f)<-[calls]-()', verify=True)
    assert ver["rows"][0]["status"] == "strong_inference"
    assert ver["rows"][0]["verified"]["result"] == "not confirmed"
    # EXISTS keeps the rows that have one, and its edges are cited
    has = _q(proj, 'match (f:function) where exists (f)-[calls]->(:function) and f.file = "app/web.py" '
                   'return f')
    assert _names(has, "f") == [("get_orders()",), ("not_a_handler()",), ("post_order()",), ("put_item()",)]
    assert has["rows"][0]["evidence"]["paths"]


def test_count_groups_and_counts_distinct_nodes(proj):
    res = _q(proj, "match (f:function)-[calls]->(g:function) return f.name, count(g)")
    by = {r["values"]["f.name"]: r["values"]["count(g)"] for r in res["rows"]}
    assert by["create_order"] == 2 and by["persist"] == 1 and "unused" not in by
    assert res["rows"][0]["values"]["f.name"] == "create_order"   # most first
    assert res["rows"][0]["examples"][0]["paths"]
    total = _q(proj, 'match (f:function) where f.file = "app/store.py" return count(*)')
    assert total["rows"][0]["values"] == {"count(*)": 5}
    none = _q(proj, 'match (f:function) where f.name = "nothing_here" return count(*)')
    assert none["rows"] == [{"values": {"count(*)": 0}, "status": "strong_inference", "bindings": 0,
                             "examples": []}]
    by_file = _q(proj, 'match (f:function) where f.file glob "app/*.py" return f.file, count(f) limit 1')
    # service.py and store.py tie at 5: by name
    assert by_file["rows"][0]["values"] == {"f.file": "app/service.py", "count(f)": 5} and by_file["limited"]
    listed = _q(proj, 'match (f:function) return f.file, count(f)', max_rows=1)
    assert listed["complete"] and not listed["limited"] and listed["groups_total"] == 4
    assert listed["more"] == "3 more group(s) counted but not listed (--max-rows)"


def test_kinds_and_fields(proj):
    tests = _q(proj, "match (t:test)-[calls]->(f) return t, f")
    assert _names(tests, "t", "f") == [("test_check()", "check()")]
    assert "test" in tests["rows"][0]["values"]["t"]["kinds"]
    files = _q(proj, 'match (m:file) where m.name glob "s*.py" return m.name')
    assert _names(files, "m.name") == [("service.py",), ("store.py",)]
    late = _q(proj, 'match (f:function) where f.file = "app/store.py" and f.line >= 16 return f.name, f.line')
    assert _names(late, "f.name", "f.line") == [("save", 25), ("unused", 20), ("write_log", 16)]
    rx = _q(proj, 'match (f:method|class) where f.label ~ "^[.R]" return f')
    assert _names(rx, "f") == [(".save()",), ("Repo",)]
    # an unnamed node never takes the place of a variable the query names
    anon = _q(proj, 'match (_1:function)-[calls]->()-[calls]->(:function) where _1.file = "app/web.py" return _1')
    assert _names(anon, "_1") == [("get_orders()",), ("not_a_handler()",), ("post_order()",), ("put_item()",)]


# -- errors ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("query, column, fragment", [
    ("match (f:function", 18, "expected ')' to close the node"),
    ("match (f:fn)", 10, "unknown kind 'fn'"),
    ('match (f) where g.name = "x"', 17, "`g` is not a variable of MATCH"),
    ("match (f)-[calls*1..20]->(g)", 17, "a path of at most 10 edges"),
    ("match (f)-[calls*3..2]->(g)", 17, "the range 3..2 is empty"),
    ('match (f) where f.text = "x"', 24, "text is matched with ~"),
    ('match (f) where f.name ~ "("', 24, "not a regular expression"),
    ("match (f)-[nope]->(g)", 10, "unknown relation 'nope'"),
    ("find (f)", 1, "a query starts with MATCH"),
    ('match (f) where f.line ~ "3"', 24, "line is a number"),
    ('match (f) where f.name = 3', 24, "name is text"),
    ("match (f)-[r:calls]->(g)", 12, "edge variables are not supported"),
    ("match (f) return f, f", 21, "f is returned twice"),
    ("match (f) limit 0", 17, "LIMIT must be at least 1"),
    ('match (f) where f.name = "x', 26, "a string is not closed"),
    ("match (f) where __import__('os')", 27, "expected '.'"),
    ("match (f) where f.name = 'a' $", 30, "unexpected character '$'"),
    ("match (f) return g", 18, "`g` is not a variable of MATCH"),
    ("match (match)", 8, "`match` is a keyword"),
])
def test_errors_say_where(proj, query, column, fragment):
    with pytest.raises(graphquery.QueryError) as exc:
        _q(proj, query)
    assert fragment in exc.value.message
    assert exc.value.where()["column"] == column
    assert exc.value.render().splitlines()[-1].index("^") == column + 1   # two spaces of indent


def test_an_error_on_a_later_line_gives_line_and_column(proj):
    with pytest.raises(graphquery.QueryError) as exc:
        _q(proj, "match (f)\nwhere f.name = 'x' and\n  f.colour = 'red'")
    assert exc.value.where() == {"offset": 37, "line": 3, "column": 5}
    assert "(at line 3, column 5)" in exc.value.render()


def test_a_known_relation_with_no_edge_is_answered_not_refused(proj):
    res = _q(proj, "match (a)-[emits]->(b) return a")
    assert res["rows"] == [] and res["complete"]
    assert "no `emits` edge in this graph" in res["notes"]


def test_deep_nesting_is_refused():
    with pytest.raises(graphquery.QueryError, match="nested in more than 30 parentheses and NOTs"):
        graphquery.parse("match (f) where " + "not " * 100 + "f.name = 'x'")
    with pytest.raises(graphquery.QueryError, match="nested in more than 30 parentheses and NOTs"):
        graphquery.parse("match (f) where " + "(" * 31 + "f.name = 'x'" + ")" * 31)
    graphquery.parse("match (f) where " + "(" * 30 + "f.name = 'x'" + ")" * 30)   # each parenthesis counts one
    with pytest.raises(graphquery.QueryError, match="over 4000 characters"):
        graphquery.parse("match (f) where " + " or ".join(["f.name = 'x'"] * 400))


# -- budgets --------------------------------------------------------------------------------------------

def test_budgets_cut_the_answer_and_say_so(proj):
    rows = _q(proj, "match (f:function)", max_rows=2)
    assert rows["row_count"] == 2 and not rows["complete"] and rows["truncated"] == "max_rows"
    assert "more rows exist" in rows["note"]
    exp = _q(proj, "match (a)-[*1..10]-(b) return count(*)", max_expansions=50)
    assert exp["truncated"] == "max_expansions" and not exp["complete"]
    assert "lower bounds" in exp["note"] and exp["expansions"] > 50
    late = _q(proj, "match (a)-[calls]->(b)", timeout=1e-9)
    assert late["truncated"] == "timeout"
    lim = _q(proj, "match (f:function) limit 3")
    assert lim["complete"] and lim["limited"] and lim["row_count"] == 3 and lim["more"]


# -- stale files ----------------------------------------------------------------------------------------

def test_a_file_changed_since_the_index_makes_its_rows_unknown(tmp_path):
    root = _make(tmp_path / "stale")
    (root / "app" / "store.py").write_text(STORE + "\n# edited\n", encoding="utf-8", newline="\n")
    res = graphquery.run(root, DONE_WHEN, verify=True)
    assert res["stale"] == {"count": 1, "files": ["app/store.py"]}
    assert any("1 file(s) changed since the index" in n for n in res["notes"])
    assert {r["status"] for r in res["rows"]} == {"unknown"}
    row = res["rows"][0]
    assert row["stale_files"] == ["app/store.py"] and row["verified"]["result"] == "not checked"
    fresh = graphquery.run(root, 'match (f:function) where f.file = "app/web.py" return f')
    assert {r["status"] for r in fresh["rows"]} == {"strong_inference"}


# -- the command ------------------------------------------------------------------------------------------

def test_cli_json_shape_and_exit_codes(proj, capsys):
    assert cli.main(["q", "--repo", str(proj), DONE_WHEN, "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert set(out) >= {"query", "columns", "rows", "row_count", "statuses", "complete", "truncated", "limited",
                        "expansions", "seconds", "verify", "notes", "limits"}
    row = out["rows"][0]
    assert set(row) >= {"values", "status", "bindings", "evidence"}
    assert set(row["values"]["f"]) == {"id", "label", "kinds", "at"}
    assert cli.main(["q", "--repo", str(proj), 'match (f) where f.name = "zzz"']) == 1
    capsys.readouterr()
    assert cli.main(["q", "--repo", str(proj), "match (f:fn)", "--json"]) == 2
    err = json.loads(capsys.readouterr().out)
    assert err["status"] == "error" and err["position"]["column"] == 10
    assert cli.main(["q", "--repo", str(proj), "match (f:fn)"]) == 2
    assert "^" in capsys.readouterr().err
    assert cli.main(["q", "--repo", str(proj), "match (f:function)", "--max-rows", "1"]) == 3
    text = capsys.readouterr().out
    assert "note: stopped at 1 row(s)" in text
    assert cli.main(["q", "--repo", str(proj), DONE_WHEN, "--verify"]) == 0
    assert "[statically_verified]" in capsys.readouterr().out


# -- review round: cycles, directions, budgets inside a text scan, odd input, failed branches -------------

REC = '''def fact(n):
    return 1 if n < 2 else n * fact(n - 1)


def ping(n):
    return pong(n - 1) if n else 0


def pong(n):
    return ping(n - 1) if n else 0


def outer():
    def inner():
        return 1
    return inner()


def chain1():
    return chain2()


def chain2():
    return chain3()


def chain3():
    return leaf()


def leaf():
    return 0


def caller():
    return fact(3)


class WithMethods:
    def run(self):
        return leaf()


class Empty:
    pass


def tri1(n):
    return tri2(n - 1) if n else 0


def tri2(n):
    return tri3(n - 1) if n else 0


def tri3(n):
    return tri1(n - 1) if n else 0
'''

SLOW = "def slow():\n" + "    x = 'aaaaaaaaaaaaaaaaaaaaaaaa'\n" * 1500 + "    return x\n"


@pytest.fixture(scope="module")
def rec(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("gqrec") / "rec"
    for rel, text in {".gitignore": ".verinoda/\n", "rec.py": REC, "slow.py": SLOW}.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "one")
    workflow.init(root)
    st = open_store(root)
    try:
        workflow.scan(st, root)
    finally:
        st.close()
    return root


def test_a_self_loop_and_a_cycle_back_to_the_start_match(rec):
    loop = _q(rec, "match (a)-[calls]->(a) return a")
    assert _names(loop, "a") == [("fact()",)]
    (hop,) = loop["rows"][0]["evidence"]["paths"][0]
    assert (hop["from"], hop["to"], hop["at"]) == ("fact()", "fact()", "rec.py:2")
    assert _names(_q(rec, "match (a)-[calls*1..1]->(a) return a"), "a") == [("fact()",)]
    cycles = _q(rec, 'match (a)-[calls*1..3]->(a) where a.file = "rec.py" return a')
    assert _names(cycles, "a") == [("fact()",), ("tri1()",), ("tri2()",), ("tri3()",)]
    tri = next(r for r in cycles["rows"] if r["values"]["a"]["label"] == "tri1()")
    assert [(h["from"], h["to"]) for h in tri["evidence"]["paths"][0]] == [
        ("tri1()", "tri2()"), ("tri2()", "tri3()"), ("tri3()", "tri1()")]
    # a lower bound above one walks by level: the same cycles, and no self-loop used twice
    assert _names(_q(rec, "match (a)-[calls*3]->(a) return a"), "a") == [("tri1()",), ("tri2()",), ("tri3()",)]
    # the extraction keeps one edge between two nodes: pong() calling ping() back is not an edge
    assert _names(_q(rec, 'match (a)-[calls]->(b) where a.name = "pong" return b'), "b") == []
    # a recursive function is not uncalled
    uncalled = _q(rec, 'match (a:function) where a.file = "rec.py" and not exists (a)<-[calls]-() return a.name')
    names = {r[0] for r in _names(uncalled, "a.name")}
    assert "fact" not in names and {"caller", "chain1", "outer"} <= names
    # *0..1 gives the start once, not again through its self-loop
    zero = _q(rec, 'match (a)-[calls*0..1]->(b) where a.name = "fact" return b')
    assert _names(zero, "b") == [("fact()",)] and zero["rows"][0]["bindings"] == 1


def test_a_path_walked_from_its_other_end_is_cited_in_the_order_the_pattern_reads(rec):
    res = _q(rec, 'match (a:function), (b)-[calls*3]->(a) where a.name = "leaf" return b')
    assert _names(res, "b") == [("chain1()",)]
    (path,) = res["rows"][0]["evidence"]["paths"]
    assert [(h["from"], h["to"], h["walked"]) for h in path] == [
        ("chain1()", "chain2()", "forward"), ("chain2()", "chain3()", "forward"), ("chain3()", "leaf()", "forward")]
    assert ("path chain1() -calls-> chain2() (rec.py:20) -calls-> chain3() (rec.py:24) -calls-> leaf() (rec.py:28)"
            in graphquery.render(res))
    back = _q(rec, 'match (a)<-[calls*3]-(b) where a.name = "leaf" return b')
    (path,) = back["rows"][0]["evidence"]["paths"]
    assert [(h["from"], h["to"], h["walked"]) for h in path] == [
        ("chain3()", "leaf()", "backward"), ("chain2()", "chain3()", "backward"), ("chain1()", "chain2()", "backward")]
    assert ("path leaf() <-calls- chain3() (rec.py:28) <-calls- chain2() (rec.py:24) <-calls- chain1() (rec.py:20)"
            in graphquery.render(back))


def test_an_either_way_walk_never_goes_back_along_the_same_edge(rec):
    res = _q(rec, 'match (a)-[contains*2]-(c) where a.name = "fact" return c')
    names = {r[0] for r in _names(res, "c")}
    assert "fact()" not in names and "ping()" in names
    text = graphquery.render(res)
    assert "path fact() <-contains- rec.py (rec.py:1) -contains-> ping() (rec.py:5)" in text


def test_the_timeout_holds_inside_a_text_scan(rec):
    import time

    t0 = time.perf_counter()
    res = _q(rec, 'match (f:function) where f.name = "slow" and f.text ~ "(a|aa)*c"', timeout=0.5)
    assert time.perf_counter() - t0 < 5
    assert res["truncated"] == "timeout" and not res["complete"]


@pytest.mark.parametrize("query, fragment", [
    ("match (f) limit ²", "unexpected character '²'"),
    ("match (f) limit 1٣", "unexpected character '٣'"),
    ("match (f٣) return f", "unexpected character '٣'"),
    ('match (f) where f.name ~ "a{1,99999999999}"', "cannot be compiled"),
    ("match (f) where f.line<-1", "`<-` is an edge arrow here"),
])
def test_odd_input_is_a_query_error(rec, query, fragment, capsys):
    with pytest.raises(graphquery.QueryError) as exc:
        _q(rec, query)
    assert fragment in exc.value.message
    assert cli.main(["q", "--repo", str(rec), query]) == 2
    capsys.readouterr()


def test_the_caret_of_an_unknown_count_variable_points_at_it():
    with pytest.raises(graphquery.QueryError) as exc:
        graphquery.parse("match (f) return count(g)")
    assert exc.value.where()["column"] == 24


def test_a_branch_that_failed_cites_nothing(rec):
    res = _q(rec, 'match (f:function) where f.file = "rec.py" and '
                  '((exists (f)-[calls]->(x) and f.name = "nomatch") or f.name = "chain1") return f')
    assert _names(res, "f") == [("chain1()",)]
    assert "paths" not in res["rows"][0]["evidence"]
    # the absence is said for the rows a NOT EXISTS kept, not for the rows the other branch kept
    mix = _q(rec, 'match (f:function) where f.file = "rec.py" and '
                  '(not exists (f)<-[calls]-() or f.name = "leaf") return f')
    by = {r["values"]["f"]["label"]: r for r in mix["rows"]}
    assert graphquery.ABSENCE_NOTE in by["outer()"]["uncertain"]
    assert "uncertain" not in by["leaf()"]
    assert by["leaf()"]["status"] == "strong_inference"
    ver = _q(rec, 'match (f:function) where f.name = "leaf" and (not exists (f)<-[calls]-() or f.line > 0)',
             verify=True)
    assert ver["rows"][0]["status"] == "statically_verified"


def test_classes_with_and_without_methods_and_a_nested_function(rec):
    assert _names(_q(rec, 'match (c:class) where c.file = "rec.py" return c'), "c") == [("Empty",),
                                                                                         ("WithMethods",)]
    assert _names(_q(rec, "match (c:class)-[method]->(m:method) return c, m"), "c", "m") == [("WithMethods",
                                                                                              ".run()")]
    nested = _q(rec, 'match (f:function) where f.name = "outer" and f.text ~ "return 1" return f')
    assert [ln["at"] for ln in nested["rows"][0]["evidence"]["lines"]] == ["rec.py:15"]


# -- the object form and the shared context (typed questions build queries from node ids) -----------------

def test_text_and_object_queries_give_equal_rows(rec):
    from verinoda import index

    g = index.load(rec)
    a, b = "rec_chain1", "rec_leaf"
    assert a in g.G and b in g.G
    text = f'match (a)-[calls*1..3]->(b) where a.id = "{a}" and b.id = "{b}" return a, b'
    by_name = 'match (a)-[calls*1..3]->(b) where a.name = "chain1" and b.name = "leaf" return a, b'
    gq = graphquery
    obj = gq.build([gq.path(gq.node("a"), gq.edge("calls", lo=1, hi=3), gq.node("b"))],
                   gq.BoolOp("and", [gq.id_in("a", [a]), gq.id_in("b", [b])]), ["a", "b"])
    for verify in (False, True):
        rows = [gq.run(rec, text, graph=g, verify=verify)["rows"],
                gq.run(rec, by_name, graph=g, verify=verify)["rows"],
                gq.run(rec, query=obj, graph=g, verify=verify)["rows"]]
        assert rows[0] and rows[0] == rows[1] == rows[2]
    # a count over several ids, as text (an OR of ids) and as an object
    cnt = gq.build([gq.path(gq.node("c"), gq.edge("calls"), gq.node("x"))], gq.id_in("x", ["rec_leaf", "rec_fact"]),
                   ["x", "count(c)"])
    t2 = 'match (c)-[calls]->(x) where x.id = "rec_leaf" or x.id = "rec_fact" return x, count(c)'
    assert gq.run(rec, query=cnt, graph=g)["rows"] == gq.run(rec, t2, graph=g)["rows"]
    with pytest.raises(gq.QueryError):
        gq.run(rec, text, query=obj, graph=g)
    with pytest.raises(gq.QueryError):
        gq.build([gq.path(gq.node("a"))], None, ["z"])
    with pytest.raises(gq.QueryError):
        gq.edge("calls", lo=0, hi=11)


def test_a_shared_context_checks_freshness_once_and_verify_reads_the_asked_route(tmp_path, monkeypatch):
    from verinoda import freshness

    web = ('from flask import Flask\n\napp = Flask(__name__)\n\n\n@app.route("/a", methods=["GET"])\n'
           '@app.route("/b", methods=["POST"])\ndef both():\n    return 1\n')
    root = tmp_path / "routes"
    (root / "app").mkdir(parents=True)
    (root / ".gitignore").write_text(".verinoda/\n", encoding="utf-8")
    (root / "app" / "__init__.py").write_text("", encoding="utf-8")
    (root / "app" / "web.py").write_text(web, encoding="utf-8", newline="\n")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "one")
    workflow.init(root)
    st = open_store(root)
    try:
        workflow.scan(st, root)
    finally:
        st.close()
    calls = []
    real = freshness.check
    monkeypatch.setattr(freshness, "check", lambda *a, **k: calls.append(1) or real(*a, **k))
    ctx = graphquery.Shared()
    q = "match (h:handler) return h"
    graphquery.run(root, q, ctx=ctx)
    graphquery.run(root, "match (f:function) return f", ctx=ctx)
    assert len(calls) == 1 and ctx.computed == {"fresh": 1, "routes": 1}
    # the GET /a declaration made blank in the shared lines: only the route asked about is re-read
    lines = web.split("\n")
    lines[5] = ""
    for route, want in ((None, "not confirmed"), ("POST /b", "confirmed"), ("GET /a", "not confirmed")):
        c = graphquery.Shared(lines_cache={"app/web.py": list(lines)})
        row = graphquery.run(root, q, ctx=c, verify=True, route=route)["rows"][0]
        assert row["verified"]["result"] == want, route
