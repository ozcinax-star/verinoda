"""Runtime observation (DESIGN D28): the sys.monitoring call-trace plugin, observe() and ingest.

Real child processes throughout: every observe() call runs pytest with the
plugin in the experiment runner's throw-away copy. The orders_app checks are
the benchmark's gold facts (q04/q06): apply_discount is reached by exactly four
tests - not by test_empty_order_rejected - OrderRepository.save only by the
round trip, and the sqlite3 boundary is at orders/repository.py:10.
"""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".repoatlas/index")

import hashlib  # noqa: E402
import json  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from repoatlas import architecture_map as am  # noqa: E402
from repoatlas import evidence as evmod  # noqa: E402
from repoatlas import index, workflow  # noqa: E402
from repoatlas.claims import Claims, check_status  # noqa: E402
from repoatlas.runtime import trace  # noqa: E402
from repoatlas.store import Store, open_store  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "orders_app"
HAS_MONITORING = hasattr(sys, "monitoring")
DISCOUNT_TESTS = ["tests/test_pricing.py::test_compute_total",
                  "tests/test_pricing.py::test_discount_applies_above_threshold",
                  "tests/test_pricing.py::test_no_discount_below_threshold",
                  "tests/test_service.py::test_place_and_fetch_roundtrip"]
EMPTY = "tests/test_service.py::test_empty_order_rejected"

pytestmark = [pytest.mark.experiment,
              pytest.mark.skipif(shutil.which("git") is None, reason="git not available")]


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args],
                   cwd=cwd, check=True, capture_output=True, stdin=subprocess.DEVNULL)


def _commit(repo: Path) -> None:
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode("utf-8"))


@pytest.fixture(scope="module")
def orders(tmp_path_factory):
    repo = tmp_path_factory.mktemp("rt") / "orders_app"
    shutil.copytree(EXAMPLE, repo, ignore=shutil.ignore_patterns(".repoatlas", "__pycache__", "*.pyc", "*.db"))
    _commit(repo)
    st = open_store(repo)
    workflow.scan(st, repo)
    g = index.load(repo)
    res = trace.observe(st, repo, [], graph=g, targets=["apply_discount", "OrderRepository.save"])
    yield repo, st, g, res
    st.close()


# -- orders_app gold facts ---------------------------------------------------------------------

def test_orders_app_reach_sets_match_the_gold_facts(orders):
    repo, st, g, res = orders
    assert res["complete"] is True, res.get("completeness")
    assert res["scope"] == "whole test suite" and res["outcome"] == "pass"
    assert res["tracer"] == ("sys.monitoring" if HAS_MONITORING else "setprofile")
    assert set(res["tests"]) == set(DISCOUNT_TESTS) | {EMPTY}
    reach = res["target_reach"]
    assert reach["orders_pricing_apply_discount"] == sorted(DISCOUNT_TESTS)
    assert EMPTY not in reach["orders_pricing_apply_discount"]
    assert reach["orders_repository_orderrepository_save"] == ["tests/test_service.py::test_place_and_fetch_roundtrip"]
    # per-test reach sets: the empty order stops in validate_items
    assert "orders_service_validate_items" in res["reach"][EMPTY]
    assert "orders_pricing_apply_discount" not in res["reach"][EMPTY]
    # the same answer straight from the store
    assert set(trace.tests_reaching(st, res["run_id"], "orders/pricing.py", "apply_discount")) == set(DISCOUNT_TESTS)


def test_sqlite3_boundary_is_recorded_at_the_connect_line(orders):
    repo, st, g, res = orders
    sqlite = [b for b in res["boundary"] if b["callee"] == "sqlite3.connect"]
    assert [b["site"] for b in sqlite] == ["orders/repository.py:10"]
    assert sqlite[0]["caller_node"] == "orders_repository_orderrepository_init" and sqlite[0]["first_seen_only"]
    assert any(b["callee"] == "sqlite3.Connection.execute" and b["site"] == "orders/repository.py:16"
               for b in res["boundary"])


def test_run_is_ingested_with_trace_hash_snapshot_and_commit(orders):
    repo, st, g, res = orders
    run = trace.load_run(st, res["run_id"])
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True,
                          stdin=subprocess.DEVNULL).stdout.strip()
    snap = st.latest_snapshot()
    assert run["complete"] == 1 and run["commit_sha"] == head == res["commit"]
    assert run["snapshot_id"] == snap["id"] == res["snapshot_id"]
    assert run["experiment_id"] == res["experiment_id"] and st.get("experiments", res["experiment_id"])
    data = Path(res["trace_path"]).read_bytes()
    assert run["trace_sha256"] == hashlib.sha256(data).hexdigest() == res["trace_sha256"]
    assert Path(res["trace_path"]).is_relative_to(repo / ".repoatlas" / "runs")
    h = run["header"]
    assert h["schema"] == trace.SCHEMA and h["requested"] == [] and h["files_changed_since_snapshot"] == []
    assert h["limits"]["max_edges"] == trace.DEFAULT_LIMITS["max_edges"]
    edge = next(c for c in run["calls"] if c["callee_qual"] == "apply_discount" and c["caller_line"] == 8)
    assert edge["caller_path"] == "orders/pricing.py" and edge["callee_path"] == "orders/pricing.py"
    assert edge["callee_line"] == 11 and edge["hits"] == 2
    assert sorted(edge["tests"]) == ["tests/test_pricing.py::test_compute_total|call",
                                     "tests/test_service.py::test_place_and_fetch_roundtrip|call"]
    # nothing in atlas.db is deleted: runs and calls are append-only
    with pytest.raises(Exception, match="never deleted"):
        st.conn.execute("DELETE FROM runtime_calls WHERE run_id = ?", (res["run_id"],))
    with pytest.raises(Exception, match="never deleted"):
        st.conn.execute("DELETE FROM runtime_runs WHERE id = ?", (res["run_id"],))


def test_edges_are_mapped_to_graph_nodes(orders):
    repo, st, g, res = orders
    e = next(e for e in res["edges"] if e["callee"]["qual"] == "apply_discount" and e["caller"]["line"] == 8)
    assert e["caller"]["node"] == "orders_pricing_compute_total"
    assert e["callee"]["node"] == "orders_pricing_apply_discount"
    save = next(e for e in res["edges"] if e["callee"]["qual"] == "OrderRepository.save")
    assert save["caller"] == {"path": "orders/service.py", "line": 22, "qual": "place_order",
                              "node": "orders_service_place_order"}
    assert save["tests"] == ["tests/test_service.py::test_place_and_fetch_roundtrip"]
    # tests started by pytest are edges from an external caller
    started = [e for e in res["edges"] if e["flags"].get("ext_caller")]
    assert {e["callee"]["qual"] for e in started} >= {"test_compute_total", "test_empty_order_rejected"}


def test_evidence_is_call_trace_and_can_verify_an_observed_edge(orders):
    repo, st, g, res = orders
    evs = [e for e in res["evidence"] if e["meta"].get("callee_qual") == "apply_discount"]
    # evidence for edges into the requested targets comes first
    assert res["evidence"][0]["meta"]["callee_qual"] in ("apply_discount", "OrderRepository.save")
    ev = next(e for e in evs if e["line_start"] == 8)
    assert ev["source_type"] == "experiment" and ev["path"] == "orders/pricing.py"
    assert ev["excerpt"].strip() == "return apply_discount(subtotal)"
    m = ev["meta"]
    assert m["kind"] == "call_trace" and m["outcome"] == "pass" and m["run_id"] == res["run_id"]
    assert m["trace_sha256"] == res["trace_sha256"] and m["complete"] is True
    assert m["callee_node"] == "orders_pricing_apply_discount" and m["n_tests"] == 2
    assert "never evidence for 'always'" in m["scope"]
    assert evmod.check_source(repo, ev).ok  # anchored on the call-site line: goes stale with it
    assert check_status("experiment_verified", [{**ev, "id": "e1", "relation": "supports"}]) is None
    cl = Claims(st, repo)
    c = cl.create("In run R, compute_total called apply_discount at orders/pricing.py:8", project="orders_app",
                  snapshot=st.latest_snapshot(), status="experiment_verified", evidence=[(ev, "supports")])
    assert c["status"] == "experiment_verified"


def test_reach_evidence_for_presence_and_absence(orders):
    repo, st, g, res = orders
    rid = res["run_id"]
    absent = trace.reach_evidence(st, repo, rid, EMPTY, "orders/pricing.py", "apply_discount",
                                  hypothesis="not_reached")
    assert absent["meta"]["outcome"] == "pass" and absent["meta"]["kind"] == "call_trace_reach"
    assert absent["path"] == "tests/test_service.py" and absent["line_start"] == 13
    assert "did not reach" in absent["locator"]
    wrong = trace.reach_evidence(st, repo, rid, EMPTY, "orders/pricing.py", "apply_discount")
    assert wrong["meta"]["outcome"] == "fail"  # the benchmark's wrong finding is refuted by the run
    present = trace.reach_evidence(st, repo, rid, "tests/test_service.py::test_place_and_fetch_roundtrip",
                                   "orders/repository.py", "OrderRepository.save")
    assert present["meta"]["outcome"] == "pass" and present["meta"]["reached_in_phases"] == ["call"]
    unknown = trace.reach_evidence(st, repo, rid, "tests/test_service.py::test_not_in_this_run",
                                   "orders/pricing.py", "apply_discount", hypothesis="not_reached")
    assert unknown is None  # no such test function to anchor on
    with pytest.raises(ValueError):
        trace.reach_evidence(st, repo, rid, EMPTY, "orders/pricing.py", "apply_discount", hypothesis="always")


def test_selected_run_scopes_absence_to_tests_that_ran(orders):
    repo, st, g, _ = orders
    res = trace.observe(st, repo, ["tests/test_pricing.py"], graph=g, targets=["apply_discount"])
    assert res["complete"] and res["scope"] == "1 selected test id(s)"
    assert set(res["tests"]) == {t for t in DISCOUNT_TESTS if "pricing" in t}
    ev = trace.reach_evidence(st, repo, res["run_id"], EMPTY, "orders/pricing.py", "apply_discount",
                              hypothesis="not_reached")
    assert ev["meta"]["outcome"] == "inconclusive" and "did not run" in ev["meta"]["inconclusive_reason"]


def test_select_tests_follows_the_tests_view_rule(orders):
    repo, st, g, _ = orders
    ids = trace.select_tests(g, ["apply_discount"])
    static = am.tests_view(g)["covered"]
    key = next(k for k in static if k.startswith("apply_discount"))
    assert {i.rpartition("::")[2] for i in ids} == {t.strip(".()") for t in static[key]}
    # static reach over-approximates: it includes the test the run shows never gets there
    assert EMPTY in ids and set(DISCOUNT_TESTS) <= set(ids)
    assert trace.select_tests(g, ["OrderRepository.save"]) == [
        "tests/test_service.py::test_empty_order_rejected", "tests/test_service.py::test_place_and_fetch_roundtrip"]
    by_name = trace.select_tests(g, [], terms=["roundtrip"])
    assert by_name == ["tests/test_service.py::test_place_and_fetch_roundtrip"]
    assert len(trace.select_tests(g, ["apply_discount"], limit=2)) == 2
    assert len(trace.select_tests(g, ["apply_discount"], limit=999)) <= trace.MAX_TESTS


def test_refused_test_ids_return_an_error_not_a_crash(orders):
    repo, st, g, _ = orders
    res = trace.observe(st, repo, ["../outside/test_x.py"], graph=g)
    if res.get("run_id") is not None:  # a container runtime ran it instead
        pytest.skip("container runtime available")
    assert res["complete"] is False and res["error"].startswith("refused") and res["next_step"]


# -- tracer semantics on a purpose-built project -----------------------------------------------

PROBE = {
    "pyproject.toml": '[tool.pytest.ini_options]\ntestpaths = ["tests"]\npythonpath = ["."]\n',
    "app/__init__.py": "",
    "app/core.py": (
        "import functools\n"
        "import threading\n"
        "\n"
        "\n"
        "def keyfn(x):\n"
        "    return -x\n"
        "\n"
        "\n"
        "def double(x):\n"
        "    return 2 * x\n"
        "\n"
        "\n"
        "def sort_desc(xs):\n"
        "    return sorted(xs, key=keyfn)\n"
        "\n"
        "\n"
        "def doubled(xs):\n"
        "    return list(map(double, xs))\n"
        "\n"
        "\n"
        "def logged(fn):\n"
        "    @functools.wraps(fn)\n"
        "    def wrapper(*a, **k):\n"
        "        return fn(*a, **k)\n"
        "    return wrapper\n"
        "\n"
        "\n"
        "@logged\n"
        "def decorated(x):\n"
        "    return x + 1\n"
        "\n"
        "\n"
        "class Item:\n"
        "    def __init__(self, v):\n"
        "        self.v = v\n"
        "\n"
        "    def __lt__(self, other):\n"
        "        return self.v < other.v\n"
        "\n"
        "\n"
        "def order_items(items):\n"
        "    return sorted(items)\n"
        "\n"
        "\n"
        "def in_thread(out):\n"
        "    t = threading.Thread(target=worker, args=(out,))\n"
        "    t.start()\n"
        "    t.join()\n"
        "\n"
        "\n"
        "def worker(out):\n"
        "    out.append(helper_in_thread())\n"
        "\n"
        "\n"
        "def helper_in_thread():\n"
        "    return 1\n"
        "\n"
        "\n"
        "def apply(fn, x):\n"
        "    return fn(x)\n"
    ),
    "tests/test_core.py": (
        "import os\n"
        "import sys\n"
        "\n"
        "from app import core\n"
        "\n"
        "\n"
        "def test_sort():\n"
        "    assert core.sort_desc([1, 3, 2]) == [3, 2, 1]\n"
        "\n"
        "\n"
        "def test_map():\n"
        "    assert core.doubled([1, 2]) == [2, 4]\n"
        "\n"
        "\n"
        "def test_decorated():\n"
        "    assert core.decorated(1) == 2\n"
        "\n"
        "\n"
        "def test_order_items():\n"
        "    a, b = core.Item(2), core.Item(1)\n"
        "    assert core.order_items([a, b])[0] is b\n"
        "\n"
        "\n"
        "def test_thread():\n"
        "    out = []\n"
        "    core.in_thread(out)\n"
        "    assert out == [1]\n"
        "\n"
        "\n"
        "def fake(x):\n"
        "    return x\n"
        "\n"
        "\n"
        "def test_double_in_test():\n"
        "    assert core.apply(fake, 3) == 3\n"
        "\n"
        "\n"
        "def test_tool_ids():\n"
        "    if os.environ.get('REPOATLAS_TRACE_MODE') != 'monitoring':\n"
        "        return\n"
        "    mon = sys.monitoring\n"
        "    assert mon.get_tool(mon.COVERAGE_ID) is None  # the user's coverage stays usable\n"
        "    assert 'repoatlas-calltrace' in {mon.get_tool(i) for i in (2, 3, 4)}\n"
    ),
    "tests/test_slow.py": "import time\n\n\ndef test_sleeps():\n    time.sleep(60)\n",
}


@pytest.fixture(scope="module")
def probe(tmp_path_factory):
    repo = tmp_path_factory.mktemp("rtp") / "probe"
    _write(repo, PROBE)
    _commit(repo)
    st = open_store(repo)
    yield repo, st
    st.close()


def _edge(res: dict, callee_qual: str, **caller) -> dict:
    out = [e for e in res["edges"] if e["callee"]["qual"] == callee_qual
           and all(e["caller"].get(k) == v for k, v in caller.items())]
    assert out, f"no edge into {callee_qual} from {caller}: {[e['callee']['qual'] for e in res['edges']]}"
    return out[0]


@pytest.mark.parametrize("mode", [pytest.param("monitoring", marks=pytest.mark.skipif(
    not HAS_MONITORING, reason="needs sys.monitoring (Python 3.12+)")), "setprofile"])
def test_callbacks_from_c_decorators_threads_and_doubles(probe, mode):
    repo, st = probe
    res = trace.observe(st, repo, ["tests/test_core.py"], mode=mode, timeout=120)
    assert res["complete"], res.get("completeness")
    assert res["tracer"] == ("sys.monitoring" if mode == "monitoring" else "setprofile")
    assert all(v == "passed" for v in res["tests"].values()), res["tests"]
    # functions called back from C are attributed to the Python line that called into C
    e = _edge(res, "keyfn", path="app/core.py", line=14)
    assert e["tests"] == ["tests/test_core.py::test_sort"] and e["hits"] == 3 and not e["flags"]
    assert _edge(res, "double", path="app/core.py", line=18)["tests"] == ["tests/test_core.py::test_map"]
    # C code calling a dunder: the line does not name it, a C call on it did
    lt = _edge(res, "Item.__lt__", path="app/core.py", line=42)
    assert lt["flags"].get("via_c") is True
    # decorator: the wrapper is a nested function; the wrapped call goes through a variable
    wrapper = _edge(res, "logged.<locals>.wrapper", path="tests/test_core.py", line=16)
    assert wrapper["flags"].get("nested") is True
    dec = _edge(res, "decorated", path="app/core.py", line=24)
    assert dec["flags"].get("indirect") is True and dec["callee"]["line"] == 28  # first decorator line
    # a thread: flagged, since its test attribution comes from the main thread
    assert _edge(res, "helper_in_thread", path="app/core.py", line=52)["flags"].get("thread") is True
    # production code calling into code defined in a test file: a test double
    fake = _edge(res, "fake", path="app/core.py", line=60)
    assert fake["flags"].get("test_double") is True and fake["flags"].get("indirect") is True
    assert res["tests"]["tests/test_core.py::test_tool_ids"] == "passed"


def test_edge_budget_breach_marks_the_trace_incomplete(probe):
    repo, st = probe
    res = trace.observe(st, repo, ["tests/test_core.py"], limits={"max_edges": 3}, timeout=120)
    c = res["completeness"]
    assert res["complete"] is False and c["trace_complete"] is False and c["breach"] == "max_edges"
    assert c["degraded_after"] and c["session_complete"] is True
    run = trace.load_run(st, res["run_id"])
    assert run["complete"] == 0 and len(run["calls"]) <= 3 and run["header"]["stats"]["dropped"] >= 1
    # absence cannot be concluded from an incomplete trace
    ev = trace.reach_evidence(st, repo, res["run_id"], "tests/test_core.py::test_map", "app/core.py",
                              "helper_in_thread", hypothesis="not_reached")
    assert ev["meta"]["outcome"] == "inconclusive"
    assert all(e["meta"]["complete"] is False for e in res["evidence"])


def test_deadline_writes_a_partial_trace_before_the_hard_kill(probe):
    repo, st = probe
    res = trace.observe(st, repo, ["tests/test_core.py::test_sort", "tests/test_slow.py::test_sleeps"],
                        timeout=8)
    assert res["outcome"] == "timeout"
    assert res["complete"] is False and res["completeness"]["breach"] == "timeout"
    assert res["completeness"]["stopped_early"] is True
    assert any(e["callee"]["qual"] == "keyfn" for e in res["edges"])  # observed before the deadline
    assert res["tests"]["tests/test_core.py::test_sort"] == "passed"


def test_off_mode_records_outcomes_only(probe):
    repo, st = probe
    res = trace.observe(st, repo, ["tests/test_core.py::test_sort"], mode="off")
    assert res["tracer"] == "off" and res["edges_total"] == 0 and res["boundary_total"] == 0
    assert res["tests"] == {"tests/test_core.py::test_sort": "passed"}
    with pytest.raises(ValueError):
        trace.observe(st, repo, [], mode="coverage")


def test_more_than_fifty_ids_are_truncated_and_reported(probe):
    repo, st = probe
    ids = ["tests/test_core.py::test_sort"] + [f"tests/test_core.py::test_missing_{i}" for i in range(60)]
    res = trace.observe(st, repo, ids, mode="off")
    assert len(res["tests_requested"]) == trace.MAX_TESTS and len(res["truncated_ids"]) == 11
    assert res["complete"] is False


# -- units: parse, ingest, flags ---------------------------------------------------------------

def _jsonl(*recs: dict) -> bytes:
    return ("\n".join(json.dumps(r) for r in recs) + "\n").encode()


def test_parse_ingest_and_query_roundtrip(tmp_path):
    data = _jsonl(
        {"k": "header", "schema": trace.SCHEMA, "final": True, "complete": True, "tracer": "sys.monitoring",
         "contexts": ["<collection>", "t.py::a|call", "t.py::b|setup"]},
        {"k": "test", "id": "t.py::a", "phases": {"setup": "passed", "call": "failed"}, "duration": 0.1},
        {"k": "edge", "caller": ["m.py", 3, "f"], "callee": ["m.py", 7, "g"], "hits": 4, "ctx": [1, 2]},
        {"k": "edge", "caller": ["m.py", 8, "g"], "callee": ["<ext>", 0, "sqlite3.connect"], "hits": 1,
         "ctx": [1], "boundary": True},
        "not a record",
    ) + b"{broken" + bytes([10])
    parsed = trace.parse_trace(data)
    assert parsed["edges"][0]["contexts"] == ["t.py::a|call", "t.py::b|setup"]
    assert trace.phase_outcome(parsed["tests"]["t.py::a"]["phases"]) == "failed"
    assert trace.phase_outcome({"setup": "failed"}) == "error"
    st = Store(":memory:")
    rid = trace.ingest(st, parsed, experiment_id=None, snapshot_id=None, commit="c0", trace_sha256="ab",
                       flags={1: {"boundary": True}})
    run = trace.load_run(st, rid)
    assert run["complete"] == 1 and run["header"]["tests_outcomes"]["t.py::a"]["phases"]["call"] == "failed"
    assert [c["callee_line"] for c in run["calls"]] == [7, None]
    assert run["calls"][1]["flags"] == {"boundary": True}
    assert trace.tests_reaching(st, rid, "m.py", "g") == {"t.py::a": ["call"], "t.py::b": ["setup"]}
    assert trace.latest_run(st)["id"] == rid and trace.latest_run(st, complete_only=True)["id"] == rid
    with pytest.raises(ValueError, match="schema"):
        trace.parse_trace(_jsonl({"k": "header", "schema": "other/9"}))


def test_test_path_rule_and_plugin_source():
    assert trace.is_test_path("tests/test_x.py") and trace.is_test_path("pkg/foo_test.py")
    assert trace.is_test_path("conftest.py") and trace.is_test_path("a\\tests\\helpers.py")
    assert not trace.is_test_path("orders/pricing.py") and not trace.is_test_path("contest.py")
    src = trace.plugin_source()
    assert b"def pytest_runtest_setup" in src and b"import repoatlas" not in src
    assert b"COVERAGE_ID" in src  # documented: never taken


def test_importing_the_plugin_inside_repoatlas_does_not_trace():
    from repoatlas.runtime import calltrace_plugin as plugin

    assert plugin.ACTIVE_ON_IMPORT is False and plugin._state["tracer"] == "off" and not plugin._state["active"]
    if HAS_MONITORING:
        assert "repoatlas-calltrace" not in {sys.monitoring.get_tool(i) for i in range(6)}
    plugin.pytest_unconfigure(None)  # a no-op: nothing is written into the cwd
    assert not plugin._state["written"]
