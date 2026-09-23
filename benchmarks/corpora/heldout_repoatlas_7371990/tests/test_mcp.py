"""Tests for the RepoAtlas MCP server (repoatlas.mcp.server).

1. In-process: every tool adapter on a scanned copy of examples/orders_app,
   checking the structured shape and that results equal the core functions'.
2. Real stdio round trip: ``python -m repoatlas mcp serve --repo <copy>``
   driven by the MCP client SDK (initialize, list_tools, call_tool).
3. Unscanned repositories: structured ``not_initialised`` errors, in-process
   and over stdio, without creating ``.repoatlas/``.

examples/ itself is never scanned or written: each test copies it to tmp_path
and runs ``git init`` + commit there.
"""

import os

os.environ.setdefault("GRAPHIFY_OUT", ".repoatlas/index")

import contextlib  # noqa: E402
import json  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import types  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from repoatlas.mcp import server as mcp_server  # noqa: E402
from repoatlas.mcp.server import TOOL_NAMES, AtlasTools, _size, cap_response  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "orders_app"
QUESTION = "where is the order saved to the database?"
STDIO_TIMEOUT = 240  # seconds for a whole stdio session (spawn + scan-free calls)

EXPECTED_PARAMS = {
    "project_query": ({"question", "max_items"}, {"question"}),
    "node_inspect": ({"name"}, {"name"}),
    "relation_trace": ({"source", "target", "mode"}, {"source", "target"}),
    "map_view": ({"view", "targets"}, {"view"}),
    "analyze": ({"question", "run_tests", "budget_seconds", "budget_calls"}, {"question"}),
    "claim_inspect": ({"claim_id"}, {"claim_id"}),
    "claim_list": ({"status", "limit"}, set()),
    "evidence_inspect": ({"evidence_id"}, {"evidence_id"}),
    "claim_verify": ({"claim_id", "run"}, {"claim_id"}),
    "claim_challenge": ({"claim_id"}, {"claim_id"}),
    "reference_research": ({"reference", "ref", "topic", "kind"}, {"reference"}),
    "reference_compare": ({"reference", "topic", "ref"}, {"reference", "topic"}),
    "feedback_submit": ({"text", "claim_id", "reference", "ref", "correction", "process", "expect_pattern",
                         "expect_in", "topic"}, {"text"}),
    "feedback_resolve": ({"feedback_id", "verdict", "reason", "evidence_ids", "correction"},
                         {"feedback_id", "verdict", "reason", "evidence_ids"}),
    "index_update": (set(), set()),
}
READ_ONLY = {"project_query", "node_inspect", "relation_trace", "map_view", "claim_inspect", "claim_list",
             "evidence_inspect"}


# -- fixtures & helpers -----------------------------------------------------------

def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args],
                   cwd=cwd, check=True, capture_output=True)


def _copy_example(dst: Path) -> Path:
    shutil.copytree(EXAMPLE, dst, ignore=shutil.ignore_patterns(".repoatlas", "__pycache__", "*.pyc",
                                                                ".pytest_cache", "*.db"))
    _git(dst, "init", "-q")
    _git(dst, "add", "-A")
    _git(dst, "commit", "-q", "-m", "init")
    return dst


def _scan(repo: Path) -> None:
    from repoatlas import workflow
    from repoatlas.store import open_store

    workflow.init(repo)
    st = open_store(repo)
    try:
        workflow.scan(st, repo)
    finally:
        st.close()


@contextlib.contextmanager
def _store(repo: Path):
    from repoatlas.store import open_store

    st = open_store(repo)
    try:
        yield st
    finally:
        st.close()


def _norm(obj):
    return json.loads(json.dumps(obj, ensure_ascii=False, default=str))


def _line_of(repo: Path, rel: str, needle: str) -> int:
    lines = (repo / rel).read_text(encoding="utf-8").splitlines()
    return next(i for i, line in enumerate(lines, 1) if needle in line)


@pytest.fixture(scope="module")
def repo(tmp_path_factory) -> Path:
    r = _copy_example(tmp_path_factory.mktemp("mcp_scanned") / "orders_app")
    _scan(r)
    return r


@pytest.fixture(scope="module")
def tools(repo) -> AtlasTools:
    return AtlasTools(repo)


@pytest.fixture(scope="module")
def analysis(tools) -> dict:
    res = tools.analyze(QUESTION)
    assert "error" not in res, res
    return res


@pytest.fixture
def fresh_repo(tmp_path) -> Path:
    r = _copy_example(tmp_path / "orders_app")
    _scan(r)
    return r


def _all_calls(t: AtlasTools) -> dict:
    """One plausible call per tool (used for the not-initialised checks)."""
    return {
        "project_query": lambda: t.project_query("where is the order saved?"),
        "node_inspect": lambda: t.node_inspect("place_order"),
        "relation_trace": lambda: t.relation_trace("a", "b"),
        "map_view": lambda: t.map_view("hierarchy"),
        "analyze": lambda: t.analyze("where is the order saved?"),
        "claim_inspect": lambda: t.claim_inspect("clm_000000000000"),
        "claim_list": lambda: t.claim_list(),
        "evidence_inspect": lambda: t.evidence_inspect("evd_000000000000"),
        "claim_verify": lambda: t.claim_verify("clm_000000000000"),
        "claim_challenge": lambda: t.claim_challenge("clm_000000000000"),
        "reference_research": lambda: t.reference_research("https://example.org/ref.git"),
        "reference_compare": lambda: t.reference_compare("https://example.org/ref.git", "persistence"),
        "feedback_submit": lambda: t.feedback_submit("that claim is wrong"),
        "feedback_resolve": lambda: t.feedback_resolve("fb_1", "confirmed", "because", ["evd_1"]),
        "index_update": lambda: t.index_update(),
    }


# -- SDK wiring -------------------------------------------------------------------

def test_import_mcp_resolves_to_installed_sdk():
    import mcp

    import repoatlas.mcp as ra_mcp

    pkg_dir = Path(mcp_server.__file__).resolve().parent
    sdk_file = Path(mcp.__file__).resolve()
    assert ra_mcp is not mcp
    assert pkg_dir != sdk_file.parent and pkg_dir not in sdk_file.parents
    server_cls, major = mcp_server._load_sdk()
    cls_file = Path(sys.modules[server_cls.__module__].__file__).resolve()
    assert pkg_dir not in cls_file.parents
    assert major in (1, 2)


def test_registered_tools_have_descriptions_and_typed_params(repo):
    anyio = pytest.importorskip("anyio")
    srv = mcp_server.build_server(repo)
    listed = anyio.run(srv.list_tools)
    assert sorted(t.name for t in listed) == sorted(TOOL_NAMES) == sorted(EXPECTED_PARAMS)
    for t in listed:
        schema = getattr(t, "input_schema", None) or getattr(t, "inputSchema")
        props, required = EXPECTED_PARAMS[t.name]
        assert set(schema.get("properties", {})) == props, t.name
        assert set(schema.get("required", [])) == required, t.name
        for pname, p in schema.get("properties", {}).items():
            assert p.get("description"), f"{t.name}.{pname} lacks a description"
            assert "type" in p or "anyOf" in p or "enum" in p, f"{t.name}.{pname} is untyped"
        assert t.description and len(t.description) > 80, t.name
        ann = t.annotations
        ro = getattr(ann, "read_only_hint", None)
        if ro is None:
            ro = getattr(ann, "readOnlyHint", None)
        assert ro is (t.name in READ_ONLY), t.name
    view = next(t for t in listed if t.name == "map_view")
    vschema = getattr(view, "input_schema", None) or getattr(view, "inputSchema")
    assert set(vschema["properties"]["view"]["enum"]) == set(mcp_server.VIEWS)


# -- retrieval & graph tools ------------------------------------------------------

def test_project_query_equals_core(repo, tools):
    from repoatlas import index, retrieval

    core = retrieval.retrieve(index.load(repo), QUESTION, retrieval.Budget(max_items=5, max_chars=6000))
    res = tools.project_query(QUESTION, max_items=5)
    assert res == _norm(core)
    assert 0 < len(res["items"]) <= 5
    assert all(i["file"] and i["why"] and i["lines"] for i in res["items"])
    assert any(i["file"] == "orders/repository.py" for i in res["items"])
    assert tools.project_query(QUESTION, max_items=1000)["budget"]["max_items"] == 25
    bad = tools.project_query("   ")
    assert bad["error"] == "invalid_argument" and bad["hint"] and bad["tool"] == "project_query"


def test_node_inspect_location_excerpt_and_edges(repo, tools):
    from repoatlas import index

    g = index.load(repo)
    res = tools.node_inspect("place_order")
    nid, _ = g.resolve("place_order")
    node = res["node"]
    assert node["id"] == nid and node["file"] == "orders/service.py" and node["kind"] == "callable"
    assert node["resolution"] == "label" and node["line"] == _line_of(repo, "orders/service.py", "def place_order")
    s, e, text = g.source(nid, max_lines=30)
    assert res["excerpt"]["lines"] == [s, e] and res["excerpt"]["text"] == text
    assert res["excerpt"]["truncated"] is False and e - s + 1 <= 30
    core_out = sorted((v, d.get("relation")) for v, d in g.out_edges(nid))
    core_in = sorted((u, d.get("relation")) for u, d in g.in_edges(nid))
    assert sorted((o["to_id"], o["relation"]) for o in res["out_edges"]) == core_out
    assert sorted((i["from_id"], i["relation"]) for i in res["in_edges"]) == core_in
    assert res["out_total"] == len(core_out) and res["in_total"] == len(core_in)
    save = next(o for o in res["out_edges"] if o["to"] == ".save()")
    assert save["confidence"] == "INFERRED" and save["derived_by"] == "repoatlas.receiver"
    assert save["at"] == f"orders/service.py:{_line_of(repo, 'orders/service.py', 'repo.save(')}"
    assert any(i["from"] == "create_order_handler()" and i["relation"] == "calls" for i in res["in_edges"])
    assert "not verified" in res["note"]

    assert tools.node_inspect("orders/service.py::place_order")["node"]["resolution"] == "file::symbol"
    meth = tools.node_inspect("OrderRepository.save")
    assert meth["node"]["resolution"] == "Class.method" and meth["node"]["file"] == "orders/repository.py"
    missing = tools.node_inspect("zzz_qqq_nothing_like_this")
    assert missing["error"] == "not_found" and "project_query" in missing["hint"]


def test_node_inspect_caps_long_definitions_after_index_update(fresh_repo):
    t = AtlasTools(fresh_repo)
    body = "\n".join(f"    total += {i}" for i in range(70))
    (fresh_repo / "orders" / "big.py").write_text(
        f'"""Long function."""\n\n\ndef very_long_function():\n    total = 0\n{body}\n    return total\n',
        encoding="utf-8")
    pre = t.node_inspect("orders/big.py::very_long_function")  # not indexed yet
    assert "error" in pre or pre["node"]["file"] != "orders/big.py"
    up = t.index_update()
    assert up["mode"] == "incremental" and "orders/big.py" in up["changed"]["added"]
    res = t.node_inspect("orders/big.py::very_long_function")  # graph cache must see the new index
    assert res["node"]["file"] == "orders/big.py"
    ex = res["excerpt"]
    assert ex["truncated"] is True and ex["lines"][1] - ex["lines"][0] + 1 == 30
    assert len(ex["text"].splitlines()) == 30 and ex["span"][1] > ex["lines"][1]


def test_relation_trace_equals_core(repo, tools):
    from repoatlas import index, retrieval

    core = retrieval.trace(index.load(repo), "create_order_handler", "OrderRepository.save", mode="flow")
    res = tools.relation_trace("create_order_handler", "OrderRepository.save")
    assert res == _norm(core) and res["status"] == "found"
    hops = res["paths"][0]
    assert hops[0]["from"] == "create_order_handler()" and hops[-1]["to_id"] == core["resolved"]["target"]["id"]
    assert all(h["at"] and h["relation"] == "calls" for h in hops)
    assert any(h.get("derived_by") == "repoatlas.receiver" for h in hops)

    rev_core = _norm(retrieval.trace(index.load(repo), "OrderRepository.save", "create_order_handler"))
    rev = tools.relation_trace("OrderRepository.save", "create_order_handler")
    assert rev["status"] == "no directed path" and {k: rev[k] for k in rev_core} == rev_core
    assert "mode='any'" in rev["next_step"] and "does not prove" in rev["next_step"]
    bad = tools.relation_trace("a", "b", mode="sideways")
    assert bad["error"] == "invalid_argument"


@pytest.mark.parametrize("view", ["hierarchy", "dependencies", "dataflow", "config", "tests", "history"])
def test_map_view_equals_core(repo, tools, view):
    from repoatlas import architecture_map as am
    from repoatlas import index

    core = _norm(am.VIEWS[view](index.load(repo)))
    res = tools.map_view(view)
    assert res["view"] == view and "coverage" in res and res["coverage"]["method"]
    if res.get("truncated"):
        assert _size(res) <= tools.max_chars
    else:
        assert res == core


def test_map_view_impact_and_errors(repo, tools):
    from repoatlas import architecture_map as am
    from repoatlas import index

    res = tools.map_view("impact", targets=["orders/repository.py"])
    core = _norm(am.impact(index.load(repo), ["orders/repository.py"]))
    assert res["targets_source"] == "argument"
    assert {k: v for k, v in res.items() if k != "targets_source"} == core
    assert "tests/test_service.py" in res["tests_to_run"]
    assert "orders/service.py" in res["affected_files"]
    git_default = tools.map_view("impact")
    assert git_default["targets_source"].startswith("git") and git_default["targets"] == []
    assert "note" in git_default
    bad = tools.map_view("galaxy")
    assert bad["error"] == "invalid_argument" and set(bad["valid"]) == set(mcp_server.VIEWS)


def test_map_view_respects_small_response_cap(repo, tools):
    from repoatlas import architecture_map as am
    from repoatlas import index

    full = tools.map_view("dataflow")
    small = AtlasTools(repo, max_chars=2500)
    res = small.map_view("dataflow")
    assert res["truncated"] is True and _size(res) <= 2500 and "over_limit" not in res["truncation"]
    assert res["view"] == "dataflow" and res["truncation"]["cut"]["paths"]["total"] == len(full["paths"])
    assert res["coverage"] == full["coverage"]  # method/limits are never cut
    assert res["paths"] == full["paths"][: len(res["paths"])]  # a prefix, not a rewrite
    tiny = AtlasTools(repo, max_chars=600).map_view("dataflow")  # below the protected coverage block
    assert tiny["truncation"]["over_limit"] is True
    assert tiny["coverage"] == _norm(am.dataflow(index.load(repo)))["coverage"]


# -- analysis & claims ------------------------------------------------------------

def test_analyze_records_claims_like_core(repo, tools, analysis):
    from repoatlas.claims import STATUSES, VERIFIED, Claims

    assert analysis["analysis_id"].startswith("ana_")
    assert {"question", "intents", "snapshot", "claims", "unknowns", "critique", "usage"} <= set(analysis)
    assert analysis["claims"] and analysis["usage"]["tool_calls"] > 0
    with _store(repo) as st:
        cl = Claims(st, repo)
        row = st.get("analyses", analysis["analysis_id"])
        assert row["result"]["claims"] == [c["id"] for c in analysis["claims"]]
        for c in analysis["claims"]:
            shown = tools.claim_inspect(c["id"])
            assert shown == _norm(cl.show(c["id"]))
            assert shown["status"] == c["status"] and shown["status"] in STATUSES
            support = [e["type"] for e in shown["supporting"]]
            if support and set(support) == {"graph_edge"}:
                assert shown["status"] not in VERIFIED  # a graph edge alone never verifies
    bad = tools.analyze("")
    assert bad["error"] == "invalid_argument"


def test_claim_list(repo, tools, analysis):
    res = tools.claim_list(limit=50)
    with _store(repo) as st:
        rows = st.claims(limit=50)
    assert res["count"] == len(rows) and [c["id"] for c in res["claims"]] == [r["id"] for r in rows]
    assert {c["id"] for c in analysis["claims"]} <= {c["id"] for c in res["claims"]}
    only = tools.claim_list(status="statically_verified")
    assert only["claims"] and all(c["status"] == "statically_verified" for c in only["claims"])
    assert tools.claim_list(status="made_up")["error"] == "invalid_argument"


def _first_claim_with(tools: AtlasTools, analysis: dict, ev_type: str, status: str | None = None):
    for c in analysis["claims"]:
        if status and c["status"] != status:
            continue
        shown = tools.claim_inspect(c["id"])
        for e in shown["supporting"]:
            if e["type"] == ev_type:
                return c["id"], e["id"]
    raise AssertionError(f"no claim with {ev_type} evidence in {analysis['claims']}")


def test_evidence_inspect_rechecks_source(repo, tools, analysis):
    from repoatlas import evidence as evmod

    cid, eid = _first_claim_with(tools, analysis, "source_code")
    res = tools.evidence_inspect(eid)
    with _store(repo) as st:
        ev = st.evidence(eid)
    assert res["evidence"]["id"] == eid and res["evidence"]["content_hash"] == ev["content_hash"]
    assert res["verifying"] is True and res["source_rank"] == evmod.SOURCE_RANK["source_code"]
    rc = res["recheck"]
    assert rc["performed"] is True and rc["ok"] is True and rc["reason"] == "cited lines unchanged"
    assert {k: rc[k] for k in ("ok", "reason", "current_hash", "moved_to")} == evmod.check_source(repo, ev).as_dict()
    a, b = ev["line_start"], ev["line_end"]
    assert rc["current_text"] == evmod.read_lines(repo / ev["path"], a, min(b, a + 29))
    assert any(c["claim"] == cid and c["relation"] == "supports" for c in res["cited_by"])

    _, gid = _first_claim_with(tools, analysis, "graph_edge")
    graph_ev = tools.evidence_inspect(gid)
    assert graph_ev["verifying"] is False and graph_ev["recheck"]["performed"] is False
    assert "never verifies" in graph_ev["note"]
    assert tools.evidence_inspect("evd_nope")["error"] == "not_found"


def test_claim_verify_and_challenge_use_core(repo, tools, analysis):
    from repoatlas import critique, index, workflow
    from repoatlas.claims import Claims

    cid, _ = _first_claim_with(tools, analysis, "source_code", status="statically_verified")
    res = tools.claim_verify(cid)
    assert res["claim"] == cid and res["source_checks"] and all(c["ok"] for c in res["source_checks"])
    with _store(repo) as st:
        assert res["after"]["status"] == Claims(st, repo).get(cid)["status"] == "statically_verified"
        core = _norm(workflow.verify(st, repo, cid))
    assert core["after"] == res["after"] and core["source_checks"] == res["source_checks"]

    ch = tools.claim_challenge(cid)
    assert ch["claim"] == cid and {"support", "source_recheck", "staleness"} <= {f["check"] for f in ch["findings"]}
    assert ch["after"]["confidence"] <= ch["before"]["confidence"]
    with _store(repo) as st:
        core_ch = critique.challenge(st, repo, cid, graph=index.load(repo))
        assert ch["after"]["status"] == core_ch["before"]["status"]
    assert [f["check"] for f in core_ch["findings"]] == [f["check"] for f in ch["findings"]]

    for fn in (tools.claim_inspect, tools.claim_verify, tools.claim_challenge):
        err = fn("clm_does_not_exist")
        assert err["error"] == "not_found" and "no claim" in err["message"] and err["hint"]


def test_edit_makes_evidence_changed_and_claim_stale(fresh_repo):
    from repoatlas import evidence as evmod
    from repoatlas.claims import Claims

    t = AtlasTools(fresh_repo)
    rel = "orders/pricing.py"
    a = _line_of(fresh_repo, rel, "def compute_total")
    with _store(fresh_repo) as st:
        snap = st.latest_snapshot()
        ev = evmod.source_evidence(fresh_repo, rel, a, a + 2, commit=snap["commit_sha"])
        c = Claims(st, fresh_repo).create("compute_total sums price*qty and applies the discount",
                                          project=snap["project"], snapshot=snap, status="statically_verified",
                                          evidence=[(ev, "supports")], subjects=[rel], actor="test")
    shown = t.claim_inspect(c["id"])
    assert shown["status"] == "statically_verified"
    eid = shown["supporting"][0]["id"]
    assert t.evidence_inspect(eid)["recheck"]["ok"] is True

    p = fresh_repo / rel
    p.write_text(p.read_text(encoding="utf-8").replace('i["price"] * i["qty"]', 'i["price"] * i["qty"] * 2'),
                 encoding="utf-8")
    rc = t.evidence_inspect(eid)["recheck"]
    assert rc["performed"] is True and rc["ok"] is False and rc["reason"] == "cited lines changed"
    assert "* 2" in rc["current_text"]

    up = t.index_update()
    assert up["mode"] == "incremental" and rel in up["changed"]["modified"]
    assert c["id"] in {s["id"] for s in up["stale"]}
    after = t.claim_inspect(c["id"])
    assert after["status"] == "stale" and after["history"][-1]["to_status"] == "stale"
    assert t.index_update()["mode"] == "noop"


# -- references & feedback (adapters over lazily imported modules) ---------------------

def test_research_adapter_passes_cli_arguments(monkeypatch, repo, tools):
    calls = []
    fake = types.ModuleType("repoatlas.research")

    def research(store, repo_, reference, *, ref=None, topic=None, kind="auto"):
        calls.append(("research", type(store).__name__, Path(repo_), reference, ref, topic, kind))
        return {"status": "ok", "reference": reference, "rows": [{"i": i, "pad": "x" * 50} for i in range(2000)]}

    def compare(store, local_repo, reference, *, ref=None, topic):
        calls.append(("compare", type(store).__name__, Path(local_repo), reference, ref, topic))
        return {"status": "ok", "topic": topic}

    fake.research, fake.compare = research, compare
    monkeypatch.setitem(sys.modules, "repoatlas.research", fake)

    res = tools.reference_research("https://example.org/ref.git", ref="v1.2", topic="persistence")
    assert calls[-1] == ("research", "Store", repo, "https://example.org/ref.git", "v1.2", "persistence", "auto")
    assert res["status"] == "ok" and res["truncated"] is True and _size(res) <= tools.max_chars
    assert res["truncation"]["cut"]["rows"]["total"] == 2000
    tools.reference_research("paper.pdf", kind="paper")
    assert calls[-1][-1] == "paper"
    assert tools.reference_research("x", kind="rumour")["error"] == "invalid_argument"

    res = tools.reference_compare("../elsewhere", "order persistence", ref="main")
    assert calls[-1] == ("compare", "Store", repo, "../elsewhere", "main", "order persistence")
    assert res == {"status": "ok", "topic": "order persistence"}
    assert tools.reference_compare("../elsewhere", "")["error"] == "invalid_argument"


def test_feedback_adapter_passes_cli_arguments(monkeypatch, repo, tools):
    calls = []
    fake = types.ModuleType("repoatlas.feedback")

    def add(store, repo_, text, *, claim_id=None, reference=None, ref=None, correction=None,
            expect_pattern=None, expect_in=None):
        calls.append(("add", Path(repo_), text, claim_id, reference, ref, correction, expect_pattern, expect_in))
        return {"id": "fb_1", "status": "open"}

    def process(store, repo_, fid, *, topic=None):
        calls.append(("process", fid, topic))
        if fid == "fb_boom":
            raise RuntimeError("verification crashed")
        return {"id": fid, "status": "processed"}

    def resolve(store, repo_, fid, verdict, *, reason, evidence_ids, correction=None):
        calls.append(("resolve", fid, verdict, reason, evidence_ids, correction))
        return {"id": fid, "verdict": verdict}

    fake.add, fake.process, fake.resolve = add, process, resolve
    monkeypatch.setitem(sys.modules, "repoatlas.feedback", fake)

    res = tools.feedback_submit("the save path is wrong", claim_id="clm_1", correction="it uses X",
                                expect_pattern=r"INSERT INTO", expect_in="orders/*.py", topic="persistence")
    assert calls[-2] == ("add", repo, "the save path is wrong", "clm_1", None, None, "it uses X",
                         "INSERT INTO", "orders/*.py")
    assert calls[-1] == ("process", "fb_1", "persistence") and res == {"id": "fb_1", "status": "processed"}

    n = len(calls)
    assert tools.feedback_submit("note only", process=False) == {"id": "fb_1", "status": "open"}
    assert len(calls) == n + 1 and calls[-1][0] == "add"

    fake.add = lambda *a, **k: {"id": "fb_boom", "status": "open"}
    failed = tools.feedback_submit("will crash")
    assert failed["error"] == "process_failed" and failed["feedback_id"] == "fb_boom" and "fb_boom" in failed["hint"]

    res = tools.feedback_resolve("fb_1", "corrected", "the code says otherwise", ["evd_1", "evd_2"],
                                 correction="it uses Y")
    assert calls[-1] == ("resolve", "fb_1", "corrected", "the code says otherwise", ["evd_1", "evd_2"], "it uses Y")
    assert res == {"id": "fb_1", "verdict": "corrected"}
    n = len(calls)
    assert tools.feedback_resolve("fb_1", "maybe", "why", [])["error"] == "invalid_argument"
    assert len(calls) == n


def test_real_research_and_feedback_modules_through_tools(tmp_path, fresh_repo):
    """Integration with the real modules (skipped if they cannot be imported).

    Only the adapter's responsibilities are asserted: the call succeeds, the
    records land in *this* repository's store, and the response is capped.
    """
    import importlib

    try:
        importlib.import_module("repoatlas.research")
        importlib.import_module("repoatlas.feedback")
    except Exception as exc:  # pragma: no cover - modules developed separately
        pytest.skip(f"research/feedback not importable: {exc}")
    ref_repo = _copy_example(tmp_path / "reference_repo")
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ref_repo, capture_output=True, text=True,
                          check=True).stdout.strip()
    t = AtlasTools(fresh_repo)

    res = t.reference_research(str(ref_repo), topic="order persistence")
    assert res.get("status") == "ok" and not res.get("error"), res
    assert res.get("resolved_commit") == head and _size(res) <= t.max_chars
    with _store(fresh_repo) as st:
        assert st.get("research", res["id"]) is not None
        assert all(st.evidence(e) is not None for e in res.get("evidence_ids", []))

    cmp = t.reference_compare(str(ref_repo), "order persistence")
    assert cmp.get("status") == "ok" and _size(cmp) <= t.max_chars, cmp

    a = t.analyze("where is the order saved to the database?")
    cid = a["claims"][0]["id"]
    fb = t.feedback_submit("this claim is wrong", claim_id=cid)
    assert fb.get("id") and fb.get("status") and not fb.get("error"), fb
    done = t.feedback_resolve(fb["id"], "unresolved", "not enough evidence either way", [])
    assert not done.get("error"), done
    with _store(fresh_repo) as st:
        row = st.get("feedback", fb["id"])
        assert row is not None and row["claim_id"] == cid


def test_missing_optional_modules_give_structured_error(monkeypatch, tools):
    monkeypatch.setitem(sys.modules, "repoatlas.research", None)
    monkeypatch.setitem(sys.modules, "repoatlas.feedback", None)
    for res in (tools.reference_research("https://example.org/r.git"),
                tools.reference_compare("https://example.org/r.git", "topic"),
                tools.feedback_submit("x"),
                tools.feedback_resolve("fb_1", "confirmed", "r", [])):
        assert res["error"] == "unavailable" and res["hint"] and "could not be imported" in res["message"]
    assert "items" in tools.project_query(QUESTION)  # the rest of the server still works


def test_internal_errors_are_structured_and_stdout_stays_clean(monkeypatch, capsys, tools):
    from repoatlas import retrieval

    def boom(*a, **k):
        print("stray output from a core function")
        raise RuntimeError("boom")

    monkeypatch.setattr(retrieval, "retrieve", boom)
    res = tools.project_query(QUESTION)
    assert res["error"] == "internal_error" and "boom" in res["message"] and res["hint"]
    out, err = capsys.readouterr()
    assert "stray output" not in out and "stray output" in err

    def missing_key(*a, **k):
        return {}["items"]  # a core bug, not a missing record: must not be reported as not_found

    monkeypatch.setattr(retrieval, "retrieve", missing_key)
    res = tools.project_query(QUESTION)
    assert res["error"] == "internal_error" and "KeyError" in res["message"]
    monkeypatch.undo()
    assert "items" in tools.project_query(QUESTION)  # lock released, server usable


# -- not initialised / not indexed ----------------------------------------------------

def test_unscanned_repo_returns_structured_error_for_every_tool(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "app.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    t = AtlasTools(plain)
    calls = _all_calls(t)
    assert set(calls) == set(TOOL_NAMES)
    for name, fn in calls.items():
        res = fn()
        assert res["error"] == "not_initialised", (name, res)
        assert res["tool"] == name and "repoatlas scan" in res["hint"] and res["message"]
    assert not (plain / ".repoatlas").exists()


def test_initialised_but_unindexed_repo(tmp_path):
    from repoatlas import workflow

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "calc.py").write_text("def add(a, b):\n    return a + b\n\n\ndef twice(x):\n    return add(x, x)\n",
                                  encoding="utf-8")
    workflow.init(proj)
    t = AtlasTools(proj)
    res = t.project_query("add")
    assert res["error"] == "no_index" and "index_update" in res["hint"]
    assert t.claim_list()["count"] == 0
    up = t.index_update()
    assert up["mode"] == "full" and up["snapshot"]["file_count"] >= 1
    node = t.node_inspect("add")
    assert node["node"]["file"] == "calc.py" and any(i["from"] == "twice()" for i in node["in_edges"])


# -- response cap -------------------------------------------------------------------------

def test_cap_response_truncates_and_reports():
    big = {
        "question": "q",
        "items": [{"id": i, "excerpt": "x" * 300} for i in range(200)],
        "history": [{"seq": i} for i in range(500)],
        "long": "y" * 50000,
    }
    res = cap_response(big, 4000)
    assert res["truncated"] is True and _size(res) <= 4000
    assert res["question"] == "q" and res["items"][0]["id"] == 0
    cut = res["truncation"]["cut"]
    assert cut["items"] == {"kept": len(res["items"]), "total": 200}
    assert res["history"][-1] == {"seq": 499}  # history keeps the newest entries
    assert len(res["long"]) < 50000 and res["long"].endswith("[truncated]")
    small = {"a": [1, 2, 3], "b": (1, 2)}
    assert cap_response(small, 4000) == {"a": [1, 2, 3], "b": [1, 2]}


# -- real stdio round trip ------------------------------------------------------------------

def _server_params(repo: Path):
    from mcp.client.stdio import StdioServerParameters

    env = {"GRAPHIFY_OUT": os.environ.get("GRAPHIFY_OUT", ".repoatlas/index"), "PYTHONIOENCODING": "utf-8"}
    if os.environ.get("PYTHONPATH"):  # the server must import what this test process imports
        env["PYTHONPATH"] = os.environ["PYTHONPATH"]
    return StdioServerParameters(command=sys.executable,
                                 args=["-m", "repoatlas", "mcp", "serve", "--repo", str(repo)],
                                 cwd=str(repo), env=env)


def _is_error(res) -> bool:
    v = getattr(res, "is_error", None)
    return bool(getattr(res, "isError", False) if v is None else v)


def _payload(res) -> dict:
    data = json.loads(res.content[0].text)
    sc = getattr(res, "structured_content", None)
    if sc is None:
        sc = getattr(res, "structuredContent", None)
    if sc is not None:
        assert sc == data
    return data


def _session(repo: Path, errlog: Path, body):
    """Spawn the server, initialize, run ``body(session)``, shut down; return its result."""
    anyio = pytest.importorskip("anyio")
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    async def main():
        with open(errlog, "w", encoding="utf-8") as err:
            with anyio.fail_after(STDIO_TIMEOUT):
                async with stdio_client(_server_params(repo), errlog=err) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        return await body(session)

    try:
        return anyio.run(main)
    except BaseException as exc:  # show the server's stderr when the session fails
        log = errlog.read_text(encoding="utf-8", errors="replace") if errlog.exists() else ""
        raise AssertionError(f"stdio session failed: {exc!r}\n--- server stderr ---\n{log[-4000:]}") from exc


def test_stdio_roundtrip(repo, tmp_path):
    from repoatlas import index, retrieval
    from repoatlas.claims import Claims

    async def body(s):
        listed = await s.list_tools()
        q = await s.call_tool("project_query", {"question": QUESTION, "max_items": 5})
        a = await s.call_tool("analyze", {"question": "where is compute_total defined?", "budget_seconds": 90})
        cid = _payload(a)["claims"][0]["id"]
        ci = await s.call_tool("claim_inspect", {"claim_id": cid})
        bad = await s.call_tool("claim_inspect", {"claim_id": "clm_does_not_exist"})
        return [t.name for t in listed.tools], q, a, ci, bad

    names, q, a, ci, bad = _session(repo, tmp_path / "server.err", body)
    assert sorted(names) == sorted(TOOL_NAMES)

    assert not _is_error(q)
    core = retrieval.retrieve(index.load(repo), QUESTION, retrieval.Budget(max_items=5, max_chars=6000))
    assert _payload(q) == _norm(core)

    ap = _payload(a)
    assert not _is_error(a) and ap["analysis_id"].startswith("ana_") and ap["claims"]
    cp = _payload(ci)
    assert not _is_error(ci) and cp["id"] == ap["claims"][0]["id"]
    with _store(repo) as st:
        assert cp == _norm(Claims(st, repo).show(cp["id"]))
        assert st.get("analyses", ap["analysis_id"]) is not None
    assert cp["status"] == ap["claims"][0]["status"] and cp["supporting"]

    bp = _payload(bad)
    assert bp["error"] == "not_found" and bp["hint"]
    if hasattr(bad, "is_error"):  # mcp 2.x: error dicts are flagged for the client
        assert bad.is_error is True
    assert "repoatlas mcp: serving" in (tmp_path / "server.err").read_text(encoding="utf-8", errors="replace")


def test_stdio_server_starts_in_unscanned_dir(tmp_path):
    plain = tmp_path / "unscanned"
    plain.mkdir()
    (plain / "app.py").write_text("def main():\n    return 1\n", encoding="utf-8")

    async def body(s):
        listed = await s.list_tools()
        out = []
        for name, args in (("project_query", {"question": "anything"}),
                           ("claim_inspect", {"claim_id": "clm_x"}),
                           ("index_update", {})):
            out.append(await s.call_tool(name, args))
        return [t.name for t in listed.tools], out

    names, results = _session(plain, tmp_path / "server.err", body)
    assert sorted(names) == sorted(TOOL_NAMES)
    for res in results:
        p = _payload(res)
        assert p["error"] == "not_initialised" and "repoatlas scan" in p["hint"]
    assert not (plain / ".repoatlas").exists()
