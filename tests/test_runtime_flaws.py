"""Runtime flaws from traced test runs: N+1 queries, repeated SQL and slow paths.

The end-to-end tests run pytest with the call tracer and the flaws plugin in the
experiment runner's throw-away copy of a small sqlite3 project: an N+1 in a loop
next to the same data read with one JOIN, a query repeated with the same
parameters, a slow function next to a quick one, a loop in the test itself and a
stand-in SQLAlchemy engine (a package named ``sqlalchemy`` under a ``site-packages`` folder, with
the ``event.listen`` / ``before_cursor_execute`` / ``after_cursor_execute``
contract) driving a driver that is not sqlite3.
"""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import ast  # noqa: E402
import json  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda.runtime import flaws as flawmod  # noqa: E402
from verinoda.runtime import trace  # noqa: E402
from verinoda.runtime.flaws_plugin import normalise  # noqa: E402
from verinoda.store import open_store  # noqa: E402

PROJECT = {
    "pyproject.toml": '[tool.pytest.ini_options]\ntestpaths = ["tests"]\npythonpath = [".", "vendor/site-packages"]\n',
    "app/__init__.py": "",
    "app/store.py": (
        "import sqlite3\n"                                                              # 1
        "import time\n"                                                                 # 2
        "\n"                                                                            # 3
        "\n"                                                                            # 4
        "def make():\n"                                                                 # 5
        "    c = sqlite3.connect(':memory:')\n"                                         # 6
        "    c.executescript('CREATE TABLE author (id INTEGER PRIMARY KEY, name TEXT);'\n"   # 7
        "                    'CREATE TABLE book (id INTEGER PRIMARY KEY, author_id INTEGER, title TEXT);')\n"  # 8
        "    c.executemany('INSERT INTO author VALUES (?, ?)', [(i, f'a{i}') for i in range(1, 9)])\n"  # 9
        "    c.executemany('INSERT INTO book VALUES (?, ?, ?)', [(i, 1 + i % 8, f'b{i}') for i in range(1, 25)])\n"
        "    return c\n"                                                                # 11
        "\n"                                                                            # 12
        "\n"                                                                            # 13
        "def books_of(c, author_id):\n"                                                 # 14
        "    return [t for (t,) in c.execute('SELECT title FROM book WHERE author_id = ?', (author_id,))]\n"  # 15
        "\n"                                                                            # 16
        "\n"                                                                            # 17
        "def titles_n_plus_one(c):\n"                                                   # 18
        "    out = {}\n"                                                                # 19
        "    for aid, name in c.execute('SELECT id, name FROM author ORDER BY id').fetchall():\n"  # 20
        "        out[name] = books_of(c, aid)\n"                                        # 21
        "    return out\n"                                                              # 22
        "\n"                                                                            # 23
        "\n"                                                                            # 24
        "def titles_join(c):\n"                                                         # 25
        "    out = {}\n"                                                                # 26
        "    rows = c.execute('SELECT a.name, b.title FROM author a JOIN book b ON b.author_id = a.id'\n"  # 27
        "                     ' ORDER BY a.id, b.id')\n"                                # 28
        "    for name, title in rows:\n"                                                # 29
        "        out.setdefault(name, []).append(title)\n"                              # 30
        "    return out\n"                                                              # 31
        "\n"                                                                            # 32
        "\n"                                                                            # 33
        "def first_author(c):\n"                                                        # 34
        "    cur = c.cursor()\n"                                                        # 35
        "    cur.execute('SELECT name FROM author WHERE id = 1')\n"                     # 36
        "    return cur.fetchone()[0]\n"                                                # 37
        "\n"                                                                            # 38
        "\n"                                                                            # 39
        "def slow_report(n):\n"                                                         # 40
        "    time.sleep(0.4)\n"                                                         # 41
        "    return n\n"                                                                # 42
        "\n"                                                                            # 43
        "\n"                                                                            # 44
        "def quick(n):\n"                                                               # 45
        "    return n + 1\n"                                                            # 46
    ),
    "app/people.py": (
        "def names(engine, ids):\n"                                                     # 1
        "    return [engine.execute('SELECT name FROM person WHERE id = %(id)s', {'id': i}) for i in ids]\n"  # 2
    ),
    "vendor/site-packages/sqlalchemy/__init__.py": "from . import engine, event  # noqa: F401\n",
    "vendor/site-packages/sqlalchemy/event.py": (
        "_listeners = {}\n\n\n"
        "def listen(target, name, fn):\n"
        "    _listeners.setdefault(name, []).append(fn)\n"
    ),
    "vendor/site-packages/sqlalchemy/engine.py": (
        "from . import event\n\n\n"
        "class FakeCursor:\n"
        "    def run(self, statement, parameters):\n"
        "        return parameters\n\n\n"
        "class Engine:\n"
        "    def __init__(self):\n"
        "        self.cursor = FakeCursor()\n\n"
        "    def execute(self, statement, parameters):\n"
        "        for fn in event._listeners.get('before_cursor_execute', []):\n"
        "            fn(self, self.cursor, statement, parameters, None, False)\n"
        "        out = self.cursor.run(statement, parameters)\n"
        "        for fn in event._listeners.get('after_cursor_execute', []):\n"
        "            fn(self, self.cursor, statement, parameters, None, False)\n"
        "        return out\n"
    ),
    "tests/test_app.py": (
        "from sqlalchemy.engine import Engine\n"                                        # 1
        "\n"                                                                            # 2
        "from app import people, store\n"                                               # 3
        "\n"                                                                            # 4
        "\n"                                                                            # 5
        "def test_n_plus_one():\n"                                                      # 6
        "    c = store.make()\n"                                                        # 7
        "    assert len(store.titles_n_plus_one(c)) == 8\n"                             # 8
        "\n"                                                                            # 9
        "\n"                                                                            # 10
        "def test_join():\n"                                                            # 11
        "    c = store.make()\n"                                                        # 12
        "    assert len(store.titles_join(c)) == 8\n"                                   # 13
        "\n"                                                                            # 14
        "\n"                                                                            # 15
        "def test_repeated():\n"                                                        # 16
        "    c = store.make()\n"                                                        # 17
        "    assert {store.first_author(c) for _ in range(4)} == {'a1'}\n"              # 18
        "\n"                                                                            # 19
        "\n"                                                                            # 20
        "def test_slow():\n"                                                            # 21
        "    assert store.slow_report(1) == 1 and store.quick(1) == 2\n"                # 22
        "\n"                                                                            # 23
        "\n"                                                                            # 24
        "def test_loop_in_test():\n"                                                    # 25
        "    c = store.make()\n"                                                        # 26
        "    for aid in range(1, 7):\n"                                                 # 27
        "        assert store.books_of(c, aid)\n"                                       # 28
        "\n"                                                                            # 29
        "\n"                                                                            # 30
        "def test_other_driver():\n"                                                    # 31
        "    assert len(people.names(Engine(), range(6))) == 6\n"                       # 32
    ),
}
N1 = "SELECT title FROM book WHERE author_id = ?"

pytestmark = [pytest.mark.experiment,
              pytest.mark.skipif(shutil.which("git") is None, reason="git not available")]


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args],
                   cwd=cwd, check=True, capture_output=True, stdin=subprocess.DEVNULL)


@pytest.fixture(scope="module")
def shop(tmp_path_factory):
    repo = tmp_path_factory.mktemp("flaws") / "shop"
    for rel, text in PROJECT.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode("utf-8"))
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    st = open_store(repo)
    res = trace.observe(st, repo, [], graph=False, flaws=True, timeout=180)
    yield repo, st, res
    st.close()


def _tests(f: dict) -> set[str]:
    return {t["test"].rpartition("::")[2] for t in f["tests"]}


# -- end to end ---------------------------------------------------------------------------------------

def test_the_n_plus_one_is_found_with_its_call_path(shop):
    repo, st, res = shop
    assert all(v == "passed" for v in res["tests"].values()), (res["tests"], res.get("logs"))
    fl = res["runtime_flaws"]
    assert fl["recorded"] and fl["complete"] and "sqlite3" in fl["hooks"]
    found = [f for f in fl["n_plus_one"] if f["statement"] == N1 and not f["loop_in_test"]]
    assert len(found) == 1, fl["n_plus_one"]
    f = found[0]
    assert f["status"] == "observed" and f["run_id"] == res["run_id"] and f["executions"] == 8
    assert f["test"] == "tests/test_app.py::test_n_plus_one" and _tests(f) == {"test_n_plus_one"}
    assert f["tests"][0]["distinct_parameters"] == 8 and f["tests"][0]["phase"] == "call"
    assert f["loop"] == {"path": "app/store.py", "line": 20, "end_line": 21, "kind": "for",
                         "qual": "titles_n_plus_one"}
    assert f["cite"] == "app/store.py:21" and f["site"] == {"path": "app/store.py", "line": 15, "qual": "books_of"}
    # the path from the test to the statement, outermost first
    assert [(p["path"], p["line"], p["qual"]) for p in f["call_path"]] == [
        ("tests/test_app.py", 8, "test_n_plus_one"), ("app/store.py", 21, "titles_n_plus_one"),
        ("app/store.py", 15, "books_of")]
    # "this is an N+1" is an inference, never more; the observation names the run and the loop
    assert f["interpretation"]["status"] == "strong_inference" and "JOIN" in f["interpretation"]["fix"]
    assert res["run_id"] in f["observation"] and "app/store.py:20" in f["observation"]
    ev = f["evidence"]
    assert ev["source_type"] == "experiment" and ev["meta"]["kind"] == "runtime_flaw"
    assert ev["meta"]["run_id"] == res["run_id"] and ev["path"] == "app/store.py" and ev["line_start"] == 21
    # the outer list query ran once per test: not an N+1, not repeated
    assert not any("FROM author ORDER BY id" in g["statement"] for g in fl["n_plus_one"] + fl["repeated_sql"])


def test_the_join_version_is_clean(shop):
    _, _, res = shop
    fl = res["runtime_flaws"]
    for key in ("n_plus_one", "repeated_sql", "slow_paths"):
        assert not any("test_join" in t["test"] for f in fl[key] for t in f.get("tests") or [f]), fl[key]
    joined = [s for s in fl["sql"]["statements"] if "JOIN" in s["statement"]]
    assert joined and joined[0]["executions"] == 1


def test_a_loop_in_the_test_itself_is_only_weak(shop):
    _, _, res = shop
    f = next(f for f in res["runtime_flaws"]["n_plus_one"] if f["loop_in_test"])
    assert f["statement"] == N1 and f["executions"] == 6 and f["test"].endswith("::test_loop_in_test")
    assert f["loop"]["path"] == "tests/test_app.py" and f["loop"]["line"] == 27 and f["cite"] == "tests/test_app.py:28"
    assert f["interpretation"]["status"] == "weak_inference" and f["interpretation"]["fix"] is None
    assert res["runtime_flaws"]["n_plus_one"][-1] is f   # the code's own N+1 first


def test_repeated_identical_sql_through_a_cursor(shop):
    _, _, res = shop
    reps = [f for f in res["runtime_flaws"]["repeated_sql"] if f["statement"] == "SELECT name FROM author WHERE id = ?"]
    assert len(reps) == 1, res["runtime_flaws"]["repeated_sql"]
    f = reps[0]
    assert f["max_repeats"] == 4 and f["repeated_parameter_sets"] == 1 and f["test"].endswith("::test_repeated")
    assert f["cite"] == "app/store.py:36" and f["call_path"][-1]["qual"] == "first_author"
    assert f["status"] == "observed" and f["interpretation"]["status"] == "strong_inference"
    # the literal 1 is the same every time: identical executions, so no N+1 (it needs different parameters)
    assert not any(g["statement"] == f["statement"] for g in res["runtime_flaws"]["n_plus_one"])


def test_a_slow_function_is_a_slow_path_and_a_quick_one_is_not(shop):
    _, _, res = shop
    slow = res["runtime_flaws"]["slow_paths"]
    f = next(f for f in slow if f["function"]["qual"] == "slow_report")
    assert f["status"] == "observed" and f["by"] == "self" and f["test"].endswith("::test_slow")
    assert 250 <= f["self_ms"] <= 2000 and f["share_of_test"] >= 0.5 and f["cite"] == "app/store.py:41"
    assert f["function"] == {"path": "app/store.py", "line": 40, "qual": "slow_report"}
    assert [p["qual"] for p in f["call_path"]] == ["test_slow", "slow_report"]
    assert "interpretation" not in f and f["evidence"]["line_start"] == 41
    assert not any(g["function"]["qual"] == "quick" for g in slow)
    assert not any(g["function"]["path"].startswith("tests/") for g in slow)   # test code is never a slow path


def test_another_driver_through_sqlalchemy_events(shop):
    _, _, res = shop
    fl = res["runtime_flaws"]
    assert "sqlalchemy" in fl["hooks"]
    f = next(f for f in fl["n_plus_one"] if f["statement"] == "SELECT name FROM person WHERE id = ?")
    assert f["executions"] == 6 and f["loop"]["kind"] == "comprehension" and f["loop"]["path"] == "app/people.py"
    assert f["cite"] == "app/people.py:2" and not f["loop_in_test"]


def test_findings_are_kept_with_the_run_without_evidence(shop):
    _, st, res = shop
    run = trace.load_run(st, res["run_id"])
    kept = run["header"]["runtime_flaws"]
    assert kept["n_plus_one_total"] == res["runtime_flaws"]["n_plus_one_total"]
    assert all("evidence" not in f for f in kept["n_plus_one"] + kept["slow_paths"])
    assert kept["file_sha256"] == res["runtime_flaws"]["file_sha256"]


def test_thresholds_move_what_is_reported(shop):
    repo, _, res = shop
    data = (Path(res["trace_path"]).parent / flawmod.FLAWS_FILE).read_bytes()
    parsed = flawmod.parse(data)
    src = trace._Sources(repo)
    strict = flawmod.report(parsed, src, res["run_id"], flawmod.thresholds({"n_plus_one": 9, "repeated": 5}),
                            with_evidence=False)
    assert strict["n_plus_one"] == [] and strict["repeated_sql"] == []
    assert strict["thresholds"]["n_plus_one"] == 9
    loose = flawmod.report(parsed, src, res["run_id"], flawmod.thresholds({"slow_ms": 10_000}), with_evidence=False)
    assert loose["slow_paths"] == [] and loose["n_plus_one_total"] == res["runtime_flaws"]["n_plus_one_total"]


def test_without_flaws_nothing_is_recorded(shop):
    repo, st, _ = shop
    res = trace.observe(st, repo, ["tests/test_app.py::test_join"], graph=False, timeout=120)
    assert "runtime_flaws" not in res and res["tests"] == {"tests/test_app.py::test_join": "passed"}
    # the boundary names stay sqlite3's when the flaws plugin wraps the connection
    assert any(b["callee"] == "sqlite3.connect" for b in shop[2]["boundary"])
    assert any(b["callee"] == "sqlite3.Connection.execute" for b in shop[2]["boundary"])


def test_a_cut_run_marks_the_flaws_incomplete(shop):
    repo, st, _ = shop
    ids = ["tests/test_app.py::test_n_plus_one"] + [f"tests/test_app.py::test_missing_{i}" for i in range(60)]
    res = trace.observe(st, repo, ids, graph=False, flaws=True, mode="off", timeout=120)
    fl = res["runtime_flaws"]
    assert res["truncated_ids"] and fl["complete"] is False
    assert any("fewer tests than asked" in x for x in fl["limits"])


def test_the_setprofile_tracer_records_the_same_and_keeps_sqlite3_names(shop):
    repo, st, _ = shop
    res = trace.observe(st, repo, ["tests/test_app.py::test_n_plus_one"], graph=False, flaws=True,
                        mode="setprofile", timeout=120)
    assert res["tracer"] == "setprofile" and res["tests"] == {"tests/test_app.py::test_n_plus_one": "passed"}
    callees = {b["callee"] for b in res["boundary"]}
    assert {"sqlite3.connect", "sqlite3.Connection.execute"} <= callees
    assert not any("verinoda_flaws" in c or "Mixin" in c for c in callees), callees
    f = res["runtime_flaws"]["n_plus_one"]
    assert len(f) == 1 and f[0]["executions"] == 8 and f[0]["cite"] == "app/store.py:21"


def test_cli_json_has_the_block_and_the_text_lists_the_call_path(shop, capsys):
    from verinoda import cli, workflow

    repo, st, _ = shop
    workflow.scan(st, repo)
    assert cli.main(["observe", "tests/test_app.py::test_n_plus_one", "tests/test_app.py::test_join",
                     "--repo", str(repo), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    fl = out["runtime_flaws"]
    assert set(fl) >= {"recorded", "run_id", "complete", "thresholds", "n_plus_one", "n_plus_one_total",
                       "repeated_sql", "repeated_sql_total", "slow_paths", "slow_paths_total", "sql", "limits"}
    assert fl["run_id"] == out["run_id"] and fl["n_plus_one_total"] == 1
    assert "evidence" not in fl["n_plus_one"][0] and fl["n_plus_one"][0]["cite"] == "app/store.py:21"
    assert cli.main(["observe", "tests/test_app.py::test_n_plus_one", "--repo", str(repo)]) == 0
    text = capsys.readouterr().out
    assert f"N+1: `{N1}` ran 8 times in tests/test_app.py::test_n_plus_one, from the for loop at app/store.py:20" \
        in text
    assert ("call path: test_n_plus_one (tests/test_app.py:8) -> titles_n_plus_one (app/store.py:21) -> "
            "books_of (app/store.py:15)") in text
    assert "strong_inference: an N+1 query" in text
    assert cli.main(["observe", "tests/test_app.py::test_join", "--no-flaws", "--repo", str(repo), "--json"]) == 0
    assert "runtime_flaws" not in json.loads(capsys.readouterr().out)
    with pytest.raises(SystemExit, match="at least 2"):
        cli.main(["observe", "--n-plus-one", "1", "--repo", str(repo)])


# -- units --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("raw, norm", [
    ("SELECT * FROM t WHERE id = 42", "SELECT * FROM t WHERE id = ?"),
    ("select x from t where name = 'O''Brien' and v > -3.5e2", "select x from t where name = ? and v > -?"),
    ("SELECT a FROM t WHERE id IN (1, 2, 3)", "SELECT a FROM t WHERE id IN (?)"),
    ("SELECT a FROM t WHERE id in (?,?,?) AND b = :b AND c = %(c)s AND d = $1 AND e = %s",
     "SELECT a FROM t WHERE id IN (?) AND b = ? AND c = ? AND d = ? AND e = ?"),
    ("INSERT INTO t VALUES (?, ?), (?, ?), (?, ?);", "INSERT INTO t VALUES (?, ?)"),
    ("SELECT t1.a::int\n  FROM t1 -- comment\n /* c */ WHERE x = 0x1F", "SELECT t1.a::int FROM t1 WHERE x = ?"),
])
def test_statement_normalisation(raw, norm):
    assert normalise(raw) == norm


def _loop(src: str, line: int, col: int | None = None, end_col: int | None = None):
    return flawmod.enclosing_loop(ast.parse(src), (line, col, line if col is not None else None, end_col))


def test_enclosing_loop_rules():
    src = ("def f(c, xs):\n"                                     # 1
           "    for x in c.q():\n"                               # 2
           "        c.get(x)\n"                                  # 3
           "    else:\n"                                         # 4
           "        c.done()\n"                                  # 5
           "    while c.more():\n"                               # 6
           "        pass\n"                                      # 7
           "    ys = [c.get(y) for y in c.q2()]\n"               # 8
           "    for z in xs:\n"                                  # 9
           "        def inner():\n"                              # 10
           "            return c.get(z)\n"                       # 11
           "    return ys\n")                                    # 12
    assert _loop(src, 3) == {"line": 2, "end_line": 5, "kind": "for"}
    assert _loop(src, 2) is None                                 # the iterable runs once
    assert _loop(src, 5) is None                                 # so does the else
    assert _loop(src, 6)["kind"] == "while"                      # a while's test runs per iteration
    line8 = src.splitlines()[7]
    get, q2 = line8.index("c.get(y)"), line8.index("c.q2()")
    assert _loop(src, 8, get, get + 8)["kind"] == "comprehension"
    assert _loop(src, 8, q2, q2 + 6) is None                     # the first iterable runs once
    assert _loop(src, 11) is None                                # another function's frame
    assert flawmod.enclosing_loop(None, (1, None, None, None)) is None


def test_thresholds_are_checked():
    assert flawmod.thresholds() == flawmod.DEFAULTS
    assert flawmod.thresholds({"n_plus_one": "7", "slow_ms": None})["n_plus_one"] == 7
    for bad in ({"n_plus_one": 1}, {"repeated": 0}, {"slow_share": 2}, {"slow_ms": -1}, {"nope": 3}):
        with pytest.raises(ValueError):
            flawmod.thresholds(bad)


def _parsed(samples: list[tuple[list[tuple], float]], ctx_ms: float) -> dict:
    """A parsed recording of one test phase from (stack innermost first as (path, line, qual, def), ms) pairs."""
    recs = []
    for stack, ms in samples:
        recs.append({"ctx": "t.py::t|call", "ms": ms, "n": 1,
                     "stack": [{"path": p, "line": ln, "qual": q, "def_line": d, "col": None, "end_line": None,
                                "end_col": None} for p, ln, q, d in stack]})
    return {"header": {"sample_ms": 5, "stats": {"samples": len(recs)}}, "sql": [], "samples": recs,
            "ctx_ms": {"t.py::t|call": ctx_ms}}


def test_slow_paths_keep_the_function_that_explains_the_time():
    test = ("t.py", 3, "t", 1)
    outer, inner = ("a.py", 5, "outer", 4), ("a.py", 9, "inner", 8)
    p = _parsed([([inner, outer, test], 900.0), ([outer, test], 50.0), ([test], 50.0)], 1000.0)
    got = flawmod.slow_paths(p, "rtr_1", flawmod.thresholds())
    assert [(f["function"]["qual"], f["by"]) for f in got] == [("inner", "self")]
    assert [x["qual"] for x in got[0]["call_path"]] == ["t", "outer", "inner"]
    # outer keeps its place when its own lines take the time
    p = _parsed([([inner, outer, test], 300.0), ([outer, test], 600.0), ([test], 100.0)], 1000.0)
    got = {f["function"]["qual"]: f for f in flawmod.slow_paths(p, "rtr_1", flawmod.thresholds())}
    assert got["outer"]["by"] == "self" and got["outer"]["total_ms"] == 900.0 and got["inner"]["by"] == "self"
    # under the share of the test phase: nothing
    p = _parsed([([inner, outer, test], 150.0)], 1000.0)
    assert flawmod.slow_paths(p, "rtr_1", flawmod.thresholds()) == []


def test_parse_skips_noise_and_refuses_another_schema():
    data = (json.dumps({"k": "header", "schema": flawmod.SCHEMA, "contexts": ["t.py::a|call"], "complete": True,
                        "stats": {"samples": 1}}) + "\n"
            + json.dumps({"k": "frames", "frames": [["m.py", 3, "f", 1, 4, 3, 9]]}) + "\n"
            + json.dumps({"k": "sql", "ctx": 0, "stmt": "SELECT 1", "stack": [0, 7], "n": 2}) + "\n"
            + json.dumps({"k": "sql", "ctx": 9, "stmt": "SELECT 2", "stack": [], "n": 1}) + "\n"
            + json.dumps({"k": "ctx", "ctx": 0, "ms": 12.5}) + "\n[1]\n{broken\n").encode()
    p = flawmod.parse(data)
    assert len(p["sql"]) == 1 and p["sql"][0]["stack"][0]["col"] == 4 and len(p["sql"][0]["stack"]) == 1
    assert p["ctx_ms"] == {"t.py::a|call": 12.5}
    p["header"]["samples_complete"] = False
    rep = flawmod.report(p, None, "rtr_x", flawmod.thresholds(), with_evidence=False)
    assert rep["complete"] is True and any("slow-path times are lower bounds" in x for x in rep["limits"])
    assert rep["sql"]["executions"] == 2 and rep["effective_sample_ms"] == 12.5
    with pytest.raises(ValueError, match="schema"):
        flawmod.parse(b'{"k": "header", "schema": "other/2"}\n')
    assert flawmod.report(None, None, "rtr_x", flawmod.thresholds(), why_missing="no file") == {
        "recorded": False, "run_id": "rtr_x", "why": "no file", "thresholds": flawmod.DEFAULTS}


def test_plugin_is_standalone_and_inert_inside_verinoda():
    from verinoda.runtime import flaws_plugin as plugin

    src = flawmod.plugin_source()
    assert b"import verinoda" not in src and b"from verinoda" not in src
    assert plugin.ACTIVE_ON_IMPORT is False and plugin._state["active"] is False
    import sqlite3

    assert not hasattr(sqlite3.connect, "__code__")  # the C function, not the plugin's wrapper
    plugin.pytest_unconfigure(None)
    assert not plugin._state["written"]
