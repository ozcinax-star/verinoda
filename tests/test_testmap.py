"""Persistent test-to-code map (verinoda/testmap.py) and the affected tests of a change review.

Unit tests on synthetic traces (update, replace vs merge, fingerprints, parameter sets, the command), a v7 store
migrating to v9, and one end-to-end run on a copy of orders_app: observe every test, change apply_discount, and
the review lists the four tests the map shows reaching it - not test_empty_order_rejected, which the static
graph reaches - and prints one pytest command that runs exactly those.
"""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import json  # noqa: E402
import shutil  # noqa: E402
import sqlite3  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import testmap  # noqa: E402
from verinoda.runtime import trace  # noqa: E402
from verinoda.store import SCHEMA_VERSION, Store  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "orders_app"
DISCOUNT_TESTS = ["tests/test_pricing.py::test_compute_total",
                  "tests/test_pricing.py::test_discount_applies_above_threshold",
                  "tests/test_pricing.py::test_no_discount_below_threshold",
                  "tests/test_service.py::test_place_and_fetch_roundtrip"]
EMPTY = "tests/test_service.py::test_empty_order_rejected"


def _jsonl(*recs: dict) -> bytes:
    return ("\n".join(json.dumps(r) for r in recs) + "\n").encode()


def _run(st: Store, tests: dict[str, dict], edges: list[tuple], *, complete: bool = True, commit: str = "c1") -> str:
    """Ingest a synthetic trace: ``tests`` {id: phases}; ``edges`` (callee path, callee qual, [contexts])."""
    ctxs = sorted({c for _p, _q, cs in edges for c in cs})
    recs = [{"k": "header", "schema": trace.SCHEMA, "final": True, "complete": complete, "contexts": ctxs}]
    recs += [{"k": "test", "id": t, "phases": ph} for t, ph in tests.items()]
    recs += [{"k": "edge", "caller": ["<ext>", 0, "x"], "callee": [p, 1, q], "hits": 1,
              "ctx": [ctxs.index(c) for c in cs]} for p, q, cs in edges]
    return trace.ingest(st, trace.parse_trace(_jsonl(*recs)), experiment_id=None, snapshot_id=None, commit=commit,
                        trace_sha256=None)


def _map(st: Store, test: str) -> dict[tuple[str, str], str | None]:
    return {(r["path"], r["qual"]): r["fingerprint"]
            for r in st.all("SELECT * FROM test_map WHERE test = ?", (test,))}


CALL = {"setup": "passed", "call": "passed", "teardown": "passed"}


def test_update_records_what_each_test_ran_with_fingerprints():
    st = Store(":memory:")
    rid = _run(st, {"t.py::a": CALL, "t.py::b": {"setup": "failed"}},
               [("m.py", "f", ["t.py::a|call"]), ("m.py", "outer.<locals>.inner", ["t.py::a|setup"]),
                ("m.py", "<module>", ["t.py::a|call"]), ("m.py", "g", ["t.py::b|setup", "<collection>"])])
    res = testmap.update(st, rid, {"m.py": "fp1"})
    assert res == {"tests": 1, "functions": 2}
    # b's call phase never ran: it is not mapped; module bodies are not functions; nested names as the review spells
    assert _map(st, "t.py::a") == {("m.py", "f"): "fp1", ("m.py", "outer.inner"): "fp1"}
    assert _map(st, "t.py::b") == {}
    row = st.one("SELECT * FROM test_map_tests WHERE test = 't.py::a'")
    assert row["run_id"] == rid and row["commit_sha"] == "c1" and row["complete"] == 1 and row["functions"] == 2
    assert row["outcome"] == "passed"


def test_complete_run_replaces_and_incomplete_run_only_adds():
    st = Store(":memory:")
    testmap.update(st, _run(st, {"t.py::a": CALL}, [("m.py", "f", ["t.py::a|call"])]), {"m.py": "fp1"})
    testmap.update(st, _run(st, {"t.py::a": CALL}, [("m.py", "g", ["t.py::a|call"])], commit="c2"), {"m.py": "fp2"})
    assert _map(st, "t.py::a") == {("m.py", "g"): "fp2"}
    rid = _run(st, {"t.py::a": CALL}, [("n.py", "h", ["t.py::a|call"])], complete=False, commit="c3")
    testmap.update(st, rid, {"n.py": "fp3"})
    assert _map(st, "t.py::a") == {("m.py", "g"): "fp2", ("n.py", "h"): "fp3"}
    row = st.one("SELECT * FROM test_map_tests WHERE test = 't.py::a'")
    assert row["complete"] == 0 and row["commit_sha"] == "c3" and row["functions"] == 2
    assert testmap.update(st, "rtr_missing") == {"tests": 0, "functions": 0}


def test_affected_reads_hits_freshness_and_parameter_sets():
    st = Store(":memory:")
    rid = _run(st, {"t.py::a[1]": CALL, "t.py::a[2]": CALL, "t.py::b": CALL, "t.py::c": CALL},
               [("m.py", "Order.total", ["t.py::a[1]|call"]), ("m.py", "other", ["t.py::a[2]|call", "t.py::b|call"]),
                ("x.py", "helper", ["t.py::c|call"])])
    testmap.update(st, rid, {"m.py": "base", "x.py": "x-old"})
    vers = {"m.py": {"base", "new"}, "x.py": {"x-now"}}
    res = testmap.affected(st, [("m.py", "Order")], lambda p: vers.get(p, set()),
                           candidates=["t.py::a", "t.py::b", "t.py::c", "t.py::d"])
    # a method of the changed class counts; the parameter sets are one test
    assert list(res["observed"]) == ["t.py::a"]
    assert res["observed"]["t.py::a"]["reaches"] == ["m.py::Order"]
    assert res["observed"]["t.py::a"]["current"] is True and res["observed"]["t.py::a"]["commit"] == "c1"
    # b: complete, current, never ran it -> not reached; c: x.py changed since its run -> not trusted; d: unmapped
    assert res["not_reached"] == ["t.py::b"] and res["mapped"] == 3


def test_fixture_code_and_failing_tests_never_rule_a_test_out():
    st = Store(":memory:")
    # a module-scoped fixture ran make_db once, in a's setup; b shares it and records nothing of it
    rid = _run(st, {"t.py::a": CALL, "t.py::b": CALL, "t.py::c": CALL},
               [("app.py", "make_db", ["t.py::a|setup"]), ("app.py", "other", ["t.py::b|call", "t.py::c|call"])])
    testmap.update(st, rid, {"app.py": "fp"})
    assert st.one("SELECT fixture FROM test_map WHERE test = 't.py::a'")["fixture"] == 1
    res = testmap.affected(st, [("app.py", "make_db")], lambda p: {"fp"}, candidates=["t.py::a", "t.py::b"])
    assert list(res["observed"]) == ["t.py::a"] and res["not_reached"] == []
    # a call-phase function still rules b out; an incomplete rerun keeps the fixture flag
    res = testmap.affected(st, [("app.py", "other")], lambda p: {"fp"}, candidates=["t.py::a"])
    assert res["not_reached"] == ["t.py::a"]
    testmap.update(st, _run(st, {"t.py::a": CALL}, [("app.py", "make_db", ["t.py::a|call"])], complete=False),
                   {"app.py": "fp"})
    assert st.one("SELECT fixture FROM test_map WHERE test = 't.py::a'")["fixture"] == 1
    # a test that failed stopped early: its mapping never rules it out
    st2 = Store(":memory:")
    rid = _run(st2, {"t.py::x": {"setup": "passed", "call": "failed", "teardown": "passed"}},
               [("app.py", "setup_x", ["t.py::x|call"])])
    testmap.update(st2, rid, {"app.py": "fp"})
    res = testmap.affected(st2, [("app.py", "compute")], lambda p: {"fp"}, candidates=["t.py::x"])
    assert res["not_reached"] == []
    res = testmap.affected(st2, [("app.py", "setup_x")], lambda p: {"fp"})
    assert res["observed"]["t.py::x"]["outcome"] == "failed"


def test_runner_id_and_pytest_command():
    assert testmap.runner_id("tests/t.py::C::test_a[x-1]") == "tests/t.py::C::test_a"
    cmd = testmap.pytest_command(["tests/t.py::test_a[1]", "tests/t.py::test_a[2]", "-p.py::x",
                                 "src/test/FooTest.java::bar", "tests/u v.py::test_b"])
    # double quotes: cmd.exe, PowerShell and POSIX shells read the id with a space alike
    assert cmd == {"command": 'python -m pytest -q tests/t.py::test_a "tests/u v.py::test_b"', "by": "test",
                   "tests": 2}
    assert testmap.shell_arg("tests/a$b.py") == "'tests/a$b.py'" and testmap.shell_arg("t/x.py::C::t") == "t/x.py::C::t"
    assert testmap.pytest_command(["src/test/FooTest.java::bar"]) is None
    many = [f"tests/t{i % 3}.py::test_{i}" for i in range(testmap.MAX_COMMAND_IDS + 1)]
    by_file = testmap.pytest_command(many)
    assert by_file["by"] == "file" and by_file["command"].endswith("tests/t0.py tests/t1.py tests/t2.py")
    aff = {"total": 60, "by": {"observed": 60, "changed": 0, "static": 0},
           "tests": [{"test": f"tests/test_{i:03}.py::test_x"} for i in range(60)],
           "command": testmap.pytest_command([f"tests/test_{i:03}.py::test_x" for i in range(30)])}
    small = testmap.compact(aff)
    assert small["n"] == 60 and small["observed"] == 60 and len(small["command"]) <= testmap.MAX_COMPACT_CHARS
    assert small["truncated"] is True and small["by"] == "file"


def test_a_v8_store_gains_the_map_tables(tmp_path):
    p = tmp_path / "atlas.db"
    Store(p).close()
    conn = sqlite3.connect(p)
    conn.execute("DROP TABLE test_map")
    conn.execute("DROP TABLE test_map_tests")
    conn.execute("UPDATE meta SET value = '8' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()
    st = Store(p)
    assert st.one("SELECT value FROM meta WHERE key = 'schema_version'")["value"] == str(SCHEMA_VERSION)
    assert st.all("SELECT * FROM test_map") == [] and st.all("SELECT * FROM test_map_tests") == []
    st.close()


# -- end to end ----------------------------------------------------------------------------------------------

def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args],
                   cwd=cwd, check=True, capture_output=True, stdin=subprocess.DEVNULL)


@pytest.mark.experiment
@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_observe_fills_the_map_and_review_prints_the_affected_tests(tmp_path):
    from verinoda import index, workflow
    from verinoda import review as rv
    from verinoda.mcp.server import AtlasTools
    from verinoda.store import open_store

    repo = tmp_path / "orders_app"
    shutil.copytree(EXAMPLE, repo, ignore=shutil.ignore_patterns(".verinoda", "__pycache__", "*.pyc", "*.db"))
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    st = open_store(repo)
    workflow.scan(st, repo)
    g = index.load(repo)
    obs = trace.observe(st, repo, [], graph=g)
    assert obs["complete"] is True, obs.get("completeness")
    assert obs["test_map"]["tests"] == 5
    assert set(DISCOUNT_TESTS) == {r["test"] for r in st.all("SELECT test FROM test_map WHERE path = ? AND qual = ?",
                                                              ("orders/pricing.py", "apply_discount"))}

    p = repo / "orders" / "pricing.py"
    p.write_bytes(p.read_bytes().replace(b"round(subtotal * 0.9, 2)", b"round(subtotal * 0.85, 2)"))
    res = rv.review(repo, store=st, graph=g)
    aff = res["tests"]["affected"]
    observed = [t["test"] for t in aff["tests"] if t["source"] == "observed"]
    assert observed == sorted(DISCOUNT_TESTS)
    assert all(t["current"] and t["status"] == "strong_inference" and t["at"].startswith("tests/test_")
               for t in aff["tests"] if t["source"] == "observed")
    # the static graph reaches test_empty_order_rejected; its mapped run did not: left out, and said so
    assert EMPTY not in {t["test"] for t in aff["tests"]} and EMPTY in aff["not_reached_when_observed"]
    assert aff["command"]["command"] == "python -m pytest -q " + " ".join(sorted(DISCOUNT_TESTS))
    text = rv.render_text(res)
    assert "affected tests: 4 (4 observed in the test map" in text
    assert "run them: python -m pytest -q tests/test_pricing.py::test_compute_total" in text
    st.close()

    mcp = AtlasTools(repo).change_review()
    assert mcp["affected_tests"]["n"] == 4 and mcp["affected_tests"]["observed"] == 4
    assert mcp["affected_tests"]["command"] == aff["command"]["command"]
    assert "affected" not in (mcp.get("tests") or {})

    # a test renamed since its observed run: its map rows are stale; the id would make pytest run nothing
    gone = "tests/test_pricing.py::test_compute_total"
    tp = repo / "tests" / "test_pricing.py"
    tp.write_bytes(tp.read_bytes().replace(b"def test_compute_total(", b"def test_compute_total_renamed("))
    _git(repo, "add", "tests/test_pricing.py")
    _git(repo, "commit", "-q", "-m", "rename")
    st = open_store(repo)
    workflow.scan(st, repo)
    g = index.load(repo)
    aff = rv.review(repo, store=st, graph=g)["tests"]["affected"]
    assert gone not in {t["test"] for t in aff["tests"]} and aff["stale_in_map"] == 1
    assert gone + " " not in aff["command"]["command"] + " "
    assert all(t["at"] and t["at"].rpartition(":")[2].isdigit() for t in aff["tests"] if t["source"] == "observed")

    # a class body changes (a new __init__ the tracer has no row for): static reach is not overruled by the map
    _git(repo, "checkout", "--", "orders/pricing.py")
    sp = repo / "orders" / "service.py"
    sp.write_bytes(sp.read_bytes().replace(
        b"class ValidationError(ValueError):\n    pass",
        b"class ValidationError(ValueError):\n    def __init__(self, msg):\n        super().__init__(msg.upper())"))
    aff = rv.review(repo, store=st, graph=g)["tests"]["affected"]
    assert EMPTY in {t["test"] for t in aff["tests"]} and EMPTY not in aff["not_reached_when_observed"]
    assert EMPTY in aff["command"]["command"]
    st.close()
