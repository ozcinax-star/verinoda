"""Tests for the Verinoda MCP server (verinoda.mcp.server).

1. In-process: every tool adapter on a scanned copy of examples/orders_app,
   checking the structured shape and that results equal the core functions'.
2. Kept state (docs/DESIGN.md D21): the graph, its spans and project_query
   answers survive between calls and follow file edits and re-indexing.
3. Real stdio round trip: ``python -m verinoda mcp serve --repo <copy>``
   driven by the MCP client SDK (initialize, list_tools, call_tool).
4. Unscanned repositories: structured ``not_initialised`` errors, in-process
   and over stdio, without creating ``.verinoda/``.

examples/ itself is never scanned or written: each test copies it to tmp_path
and runs ``git init`` + commit there. Reference tests run offline (sockets
refused, git limited to file://).
"""

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import contextlib  # noqa: E402
import json  # noqa: E402
import shutil  # noqa: E402
import socket  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
import types  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda.mcp import server as mcp_server  # noqa: E402
from verinoda.mcp.server import TOOL_NAMES, AtlasTools, _size, cap_response  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "orders_app"
QUESTION = "where is the order saved to the database?"
STDIO_TIMEOUT = 240  # seconds for a whole stdio session (spawn + scan-free calls)
GOLD_DISCOUNT_TESTS = ["tests/test_pricing.py::test_compute_total",
                       "tests/test_pricing.py::test_discount_applies_above_threshold",
                       "tests/test_pricing.py::test_no_discount_below_threshold",
                       "tests/test_service.py::test_place_and_fetch_roundtrip"]

EXPECTED_PARAMS = {
    "project_query": ({"question", "max_items", "format"}, {"question"}),
    "node_inspect": ({"name"}, {"name"}),
    "relation_trace": ({"source", "target", "mode"}, {"source", "target"}),
    "map_view": ({"view", "targets"}, {"view"}),
    "question_plan_draft": ({"question"}, {"question"}),
    "question_plan_check": ({"plan_json"}, {"plan_json"}),
    "analyze": ({"question", "run_tests", "budget_seconds", "budget_calls", "plan_json", "observe"}, set()),
    "plan_audit": ({"analysis_id", "refresh"}, {"analysis_id"}),
    "lexicon_show": ({"word"}, {"word"}),
    "claim_inspect": ({"claim_id"}, {"claim_id"}),
    "claim_list": ({"status", "limit"}, set()),
    "evidence_inspect": ({"evidence_id"}, {"evidence_id"}),
    "claim_verify": ({"claim_id", "run"}, {"claim_id"}),
    "claim_challenge": ({"claim_id"}, {"claim_id"}),
    "resolve_call": ({"path", "line", "target", "target_path", "target_line"}, {"path", "line", "target"}),
    "code_check": ({"paths", "diff", "snippet", "as_path", "env", "include_exists"}, set()),
    "api_members": ({"target", "env", "private"}, {"target"}),
    "runtime_observe": ({"test_ids", "symbols", "terms", "timeout"}, set()),
    "reference_resolve": ({"text", "references", "network", "local_intent"}, {"text"}),
    "reference_research": ({"reference", "ref", "topic", "kind", "resolution_id", "reference_id"}, set()),
    "reference_compare": ({"reference", "topic", "ref"}, {"reference", "topic"}),
    "feedback_submit": ({"text", "claim_id", "reference", "ref", "correction", "process", "expect_pattern",
                         "expect_in", "topic", "references"}, {"text"}),
    "feedback_process": ({"feedback_id", "network", "topic"}, {"feedback_id"}),
    "feedback_resolve": ({"feedback_id", "verdict", "reason", "evidence_ids", "correction"},
                         {"feedback_id", "verdict", "reason", "evidence_ids"}),
    "index_update": (set(), set()),
    "decision_record": ({"action", "decision_id", "chosen", "rationale", "title", "brief_id", "guards", "governs",
                         "revisit_when", "supersedes", "guard_ids", "at", "reason", "until", "document",
                         "user_statement", "question_id"}, {"action"}),
    "decision_check": ({"base", "changed_only", "refresh"}, set()),
    "decision_brief": ({"question", "options", "quotes", "agent_arguments"}, {"question"}),
}
READ_ONLY = {"project_query", "node_inspect", "relation_trace", "map_view", "claim_inspect", "claim_list",
             "evidence_inspect", "question_plan_draft", "lexicon_show", "resolve_call", "code_check", "api_members"}


# -- fixtures & helpers -----------------------------------------------------------

def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args],
                   cwd=cwd, check=True, capture_output=True)


def _copy_example(dst: Path) -> Path:
    shutil.copytree(EXAMPLE, dst, ignore=shutil.ignore_patterns(".verinoda", "__pycache__", "*.pyc",
                                                                ".pytest_cache", "*.db"))
    _git(dst, "init", "-q")
    _git(dst, "add", "-A")
    _git(dst, "commit", "-q", "-m", "init")
    return dst


def _scan(repo: Path) -> None:
    from verinoda import workflow
    from verinoda.store import open_store

    workflow.init(repo)
    st = open_store(repo)
    try:
        workflow.scan(st, repo)
    finally:
        st.close()


@contextlib.contextmanager
def _store(repo: Path):
    from verinoda.store import open_store

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


def _edit(p: Path, old: str, new: str) -> None:
    text = p.read_text(encoding="utf-8")
    assert old in text
    p.write_bytes(text.replace(old, new).encode("utf-8"))


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
    _clock_past_writes(r)
    return r


def _clock_past_writes(repo: Path) -> None:
    """Wait until the clock has moved past the newest file write in ``repo``: on a coarse clock
    (Windows) a file stamped in the same tick as a later call's start counts as modified during that
    call (racy) and nothing read from it is kept."""
    newest = max(p.stat().st_mtime_ns for p in repo.rglob("*") if p.is_file())
    while time.time_ns() <= newest:
        time.sleep(0.001)


@pytest.fixture
def offline(monkeypatch):
    """No network: sockets refuse to resolve names and git only speaks file://."""
    def refuse(*a, **k):
        raise OSError("network disabled in tests")

    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")


@pytest.fixture(scope="module")
def tagged_ref(tmp_path_factory) -> tuple[Path, str]:
    """A local reference repository with tag v1.0 (and a later commit on top of it)."""
    ref = _copy_example(tmp_path_factory.mktemp("mcp_ref") / "reference_repo")
    _git(ref, "tag", "v1.0")
    tagged = subprocess.run(["git", "rev-parse", "v1.0"], cwd=ref, capture_output=True, text=True,
                            check=True).stdout.strip()
    (ref / "CHANGES.md").write_text("later work\n", encoding="utf-8")
    _git(ref, "add", "-A")
    _git(ref, "commit", "-q", "-m", "after the tag")
    return ref, tagged


def _host_plan(message: str, sub_questions: list[dict], mentions: list[dict] | None = None, **extra) -> dict:
    from verinoda import question_plan as qp

    return {"schema": qp.SCHEMA_ID, "user_message": message, "language": "en", "restated_goal": message,
            "sub_questions": sub_questions, "mentions": mentions or [], **extra}


AMBIGUOUS_PLAN = _host_plan(
    "Where is order handled?",
    [{"id": "q1", "text": "Where is order handled?", "intent": "locate", "mentions": ["m1"],
      "done_when": {"kind": "location_verified", "subjects": ["m1"], "detail": "file:line"}}],
    [{"id": "m1", "text": "order", "kind": "symbol"}], on_ambiguity="ask")


def _all_calls(t: AtlasTools) -> dict:
    """One plausible call per tool (used for the not-initialised checks)."""
    return {
        "project_query": lambda: t.project_query("where is the order saved?"),
        "node_inspect": lambda: t.node_inspect("place_order"),
        "relation_trace": lambda: t.relation_trace("a", "b"),
        "map_view": lambda: t.map_view("hierarchy"),
        "question_plan_draft": lambda: t.question_plan_draft("where is the order saved?"),
        "question_plan_check": lambda: t.question_plan_check(json.dumps(AMBIGUOUS_PLAN)),
        "analyze": lambda: t.analyze("where is the order saved?"),
        "plan_audit": lambda: t.plan_audit("ana_000000000000"),
        "lexicon_show": lambda: t.lexicon_show("order"),
        "claim_inspect": lambda: t.claim_inspect("clm_000000000000"),
        "claim_list": lambda: t.claim_list(),
        "evidence_inspect": lambda: t.evidence_inspect("evd_000000000000"),
        "claim_verify": lambda: t.claim_verify("clm_000000000000"),
        "claim_challenge": lambda: t.claim_challenge("clm_000000000000"),
        "resolve_call": lambda: t.resolve_call("app.py", 2, "main"),
        "code_check": lambda: t.code_check(paths=["app.py"]),
        "api_members": lambda: t.api_members("json"),
        "runtime_observe": lambda: t.runtime_observe(symbols=["main"]),
        "reference_resolve": lambda: t.reference_resolve("requests 2.31", network="off"),
        "reference_research": lambda: t.reference_research("https://example.org/ref.git"),
        "reference_compare": lambda: t.reference_compare("https://example.org/ref.git", "persistence"),
        "feedback_submit": lambda: t.feedback_submit("that claim is wrong"),
        "feedback_process": lambda: t.feedback_process("fb_1"),
        "feedback_resolve": lambda: t.feedback_resolve("fb_1", "confirmed", "because", ["evd_1"]),
        "index_update": lambda: t.index_update(),
        "decision_record": lambda: t.decision_record("list"),
        "decision_check": lambda: t.decision_check(),
        "decision_brief": lambda: t.decision_brief("should we move to PostgreSQL?"),
    }


# -- SDK wiring -------------------------------------------------------------------

def test_import_mcp_resolves_to_installed_sdk():
    import mcp

    import verinoda.mcp as ra_mcp

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
    assert set(mcp_server.DESCRIPTIONS) == set(TOOL_NAMES)
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
    by_name = {t.name: getattr(t, "input_schema", None) or getattr(t, "inputSchema") for t in listed}
    assert set(by_name["map_view"]["properties"]["view"]["enum"]) == set(mcp_server.VIEWS)
    assert set(by_name["project_query"]["properties"]["format"]["enum"]) == {"text", "json"}
    assert by_name["project_query"]["properties"]["format"]["default"] == "text"
    net = by_name["reference_resolve"]["properties"]["network"]
    assert {"off", "cache", "on"} in [set(o.get("enum", [])) for o in net.get("anyOf", [net])]
    assert net.get("default") is None
    instructions = mcp_server.INSTRUCTIONS
    assert all(name in instructions for name in TOOL_NAMES)
    assert "question_plan_draft" in instructions and "reference_resolve first" in instructions


# -- retrieval & graph tools ------------------------------------------------------

def test_project_query_text_and_json_equal_core(repo, tools):
    from verinoda import index, retrieval

    core = retrieval.retrieve(index.load(repo), QUESTION, retrieval.Budget(max_items=5, max_chars=6000))
    text = tools.project_query(QUESTION, max_items=5)  # text is the default (D20)
    assert text == {"format": "text", "question": QUESTION, "text": retrieval.render_text(core, 6000)}
    assert text["text"].startswith(f"# {QUESTION}") and "orders/repository.py:" in text["text"]
    res = tools.project_query(QUESTION, max_items=5, format="json")
    assert res == _norm(core)
    assert 0 < len(res["items"]) <= 5
    assert all(i["file"] and i["why"] and i["lines"] for i in res["items"])
    assert any(i["file"] == "orders/repository.py" for i in res["items"])
    assert tools.project_query(QUESTION, max_items=1000, format="json")["budget"]["max_items"] == 25
    bad = tools.project_query("   ")
    assert bad["error"] == "invalid_argument" and bad["hint"] and bad["tool"] == "project_query"
    fmt = tools.project_query(QUESTION, format="yaml")
    assert fmt["error"] == "invalid_argument" and set(fmt["valid"]) == {"text", "json"}


def test_project_query_text_fits_a_small_cap(repo):
    small = AtlasTools(repo, max_chars=2000)
    res = small.project_query(QUESTION)
    assert "truncated" not in res and _size(res) <= 2000  # render_text packs to the budget itself
    assert "more candidates not shown" in res["text"] or len(res["text"]) < 1500


def test_node_inspect_location_excerpt_and_edges(repo, tools):
    from verinoda import index

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
    assert save["confidence"] == "INFERRED" and save["derived_by"] == "verinoda.receiver"
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
    from verinoda import index, retrieval

    core = retrieval.trace(index.load(repo), "create_order_handler", "OrderRepository.save", mode="flow")
    res = tools.relation_trace("create_order_handler", "OrderRepository.save")
    assert res == _norm(core) and res["status"] == "found"
    hops = res["paths"][0]
    assert hops[0]["from"] == "create_order_handler()" and hops[-1]["to_id"] == core["resolved"]["target"]["id"]
    assert all(h["at"] and h["relation"] == "calls" for h in hops)
    assert any(h.get("derived_by") == "verinoda.receiver" for h in hops)

    rev_core = _norm(retrieval.trace(index.load(repo), "OrderRepository.save", "create_order_handler"))
    rev = tools.relation_trace("OrderRepository.save", "create_order_handler")
    assert rev["status"] == "no directed path" and {k: rev[k] for k in rev_core} == rev_core
    assert "mode='any'" in rev["next_step"] and "does not prove" in rev["next_step"]
    bad = tools.relation_trace("a", "b", mode="sideways")
    assert bad["error"] == "invalid_argument"


def test_relation_trace_passes_hints_reachability_and_note(repo, tools):
    from verinoda import index, retrieval

    g = index.load(repo)
    unres = tools.relation_trace("zzqq_no_such_symbol", "place_order")
    core_unres = _norm(retrieval.trace(g, "zzqq_no_such_symbol", "place_order"))
    assert unres == core_unres and unres["status"] == "unresolved"
    assert "hints" in unres and unres["next_step"] == core_unres["next_step"]  # the core's step, not replaced

    structural = tools.relation_trace("OrderRepository", "OrderRepository.save", mode="any")
    assert structural == _norm(retrieval.trace(g, "OrderRepository", "OrderRepository.save", mode="any"))
    assert structural["reachability"] == "structural" and "containment" in structural["note"]
    execution = tools.relation_trace("place_order", "compute_total", mode="any")
    assert execution["status"] == "found" and execution["reachability"] == "execution"


@pytest.mark.parametrize("view", ["hierarchy", "dependencies", "dataflow", "config", "tests", "history"])
def test_map_view_equals_core(repo, tools, view):
    from verinoda import architecture_map as am
    from verinoda import index

    core = _norm(am.VIEWS[view](index.load(repo)))
    res = tools.map_view(view)
    assert res["view"] == view and "coverage" in res and res["coverage"]["method"]
    if res.get("truncated"):
        assert _size(res) <= tools.max_chars
    else:
        assert res == core


def test_map_view_impact_and_errors(repo, tools):
    from verinoda import architecture_map as am
    from verinoda import index

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
    from verinoda import architecture_map as am
    from verinoda import index

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


# -- kept state (D21) -------------------------------------------------------------------

def test_graph_and_spans_are_kept_and_follow_file_edits(fresh_repo):
    from verinoda import index

    t = AtlasTools(fresh_repo)
    t.racy_ns = 0  # the fixture wrote every file a moment ago; see the racy-file test for the default
    first = t.node_inspect("compute_total")
    t.node_inspect("place_order")
    t.project_query("apply_discount threshold")
    assert t.cache_stats["graph_loads"] == 1
    g = t._graph_cache[1]
    nid = first["node"]["id"]
    assert nid in g._spans  # spans survive between calls (no per-call clearing)
    service_spans = {n: s for n, s in g._spans.items() if g.file(n) == "orders/service.py"}
    assert service_spans
    a = first["excerpt"]["span"][0]
    assert first["excerpt"]["span"] == [a, a + 2]

    # the body grows by two lines without re-indexing: the kept span of that file must follow
    _edit(fresh_repo / "orders" / "pricing.py", "    return apply_discount(subtotal)\n",
          "    subtotal = subtotal + 0\n    subtotal = subtotal * 1\n    return apply_discount(subtotal)\n")
    after = t.node_inspect("compute_total")
    assert t.cache_stats["graph_loads"] == 1 and t.cache_stats["span_files_invalidated"] >= 1
    fresh = index.load(fresh_repo)
    assert after["excerpt"]["span"] == list(fresh.span(nid)) == [a, a + 4]
    assert after["excerpt"]["text"] == fresh.source(nid, max_lines=30)[2]
    assert "subtotal * 1" in after["excerpt"]["text"]
    assert all(g._spans.get(n) == s for n, s in service_spans.items())  # untouched files keep their spans

    up = t.index_update()
    assert up["mode"] == "incremental"
    t.node_inspect("compute_total")
    assert t.cache_stats["graph_loads"] == 2  # a rewritten graph.json is loaded again


def test_the_kept_graph_follows_the_receiver_call_sidecar_when_graph_json_stays(fresh_repo):
    # An update can leave graph.json as it is (the pipeline built the same graph) and still
    # rewrite receiver_calls.json: a receiver call changed on the same line.
    from verinoda import index
    from verinoda.paths import graph_path

    def callees(res: dict) -> set[str]:
        return {e.get("to_id") for e in res.get("out_edges") or []}

    t = AtlasTools(fresh_repo)
    assert "orders_repository_orderrepository_get" in callees(t.node_inspect("fetch_order"))
    gp = graph_path(fresh_repo)
    before = gp.stat().st_mtime_ns, gp.read_bytes()
    _edit(fresh_repo / "orders" / "service.py", "    return repo.get(order_id)\n",
          '    return repo.save("x", 0.0)\n')
    index.refresh_receiver_sidecar(fresh_repo)
    assert (gp.stat().st_mtime_ns, gp.read_bytes()) == before
    after = callees(t.node_inspect("fetch_order"))
    assert t.cache_stats["graph_loads"] == 2
    assert "orders_repository_orderrepository_save" in after
    assert "orders_repository_orderrepository_get" not in after
    assert after == callees(AtlasTools(fresh_repo).node_inspect("fetch_order"))


def test_project_query_answers_are_kept_until_an_input_changes(fresh_repo, monkeypatch):
    from verinoda import index, retrieval

    t = AtlasTools(fresh_repo)
    t.racy_ns = 0  # the fixture wrote every file a moment ago; see the racy-file test for the default
    real = retrieval.retrieve
    calls: list[str] = []

    def spy(g, question, *a, **k):
        calls.append(question)
        return real(g, question, *a, **k)

    monkeypatch.setattr(retrieval, "retrieve", spy)
    first = t.project_query(QUESTION, max_items=5)
    again = t.project_query(QUESTION, max_items=5)
    assert again == first and len(calls) == 1 and t.cache_stats["query_memo_hits"] == 1
    t.project_query(QUESTION, max_items=5, format="json")
    t.project_query(QUESTION, max_items=4)
    assert len(calls) == 3  # another format or budget is another answer

    # an edited file that the answer read: recomputed, and equal to a fresh core computation
    _edit(fresh_repo / "orders" / "repository.py", "self.conn.commit()", "self.conn.commit()  # durable")
    edited = t.project_query(QUESTION, max_items=5)
    assert len(calls) == 4 and edited != first and "durable" in edited["text"]
    core = real(index.load(fresh_repo), QUESTION, retrieval.Budget(max_items=5, max_chars=6000))
    assert edited["text"] == retrieval.render_text(core, 6000)
    assert "orders/repository.py" in core["budget"]["stale_files"]  # stated as changed since indexing
    assert "changed since indexing" in edited["text"]

    # re-indexing rewrites graph.json and the search index: nothing is served from before
    assert t.index_update()["mode"] == "incremental"
    _clock_past_writes(fresh_repo)
    reindexed = t.project_query(QUESTION, max_items=5)
    assert len(calls) == 5 and "changed since indexing" not in reindexed["text"]
    hits = t.cache_stats["query_memo_hits"]
    assert t.project_query(QUESTION, max_items=5) == reindexed and t.cache_stats["query_memo_hits"] == hits + 1


def test_nothing_is_kept_from_a_file_modified_close_to_the_call(fresh_repo):
    t = AtlasTools(fresh_repo)
    assert t.racy_ns == mcp_server.RACY_NS == 2_000_000_000
    t.racy_ns = time.time_ns()  # every file counts as modified just now (copytree kept the old mtimes)
    first = t.project_query(QUESTION)
    assert t.project_query(QUESTION) == first  # recomputed, not served from memory
    assert t.cache_stats["query_memo_hits"] == 0 and not t._query_memo
    t.node_inspect("compute_total")
    assert t._span_stat and set(t._span_stat.values()) == {mcp_server._RACY}
    n = t.cache_stats["span_files_invalidated"]
    t.node_inspect("compute_total")
    assert t.cache_stats["span_files_invalidated"] > n  # re-derived on the next call
    t.racy_ns = 0
    t.project_query(QUESTION)
    t.project_query(QUESTION)
    assert t.cache_stats["query_memo_hits"] == 1


# -- question understanding ---------------------------------------------------------------

def test_question_plan_draft_and_check_equal_core(repo, tools):
    from verinoda import index, lexicon
    from verinoda import question_plan as qp

    q = "İndirim nerede uygulanıyor ve hangi testler bunu çalıştırıyor?"
    g, lex = index.load(repo), lexicon.load(repo)
    drafted = tools.question_plan_draft(q)
    assert drafted["plan"] == _norm(qp.draft(q, g, lex)) and "question_plan_check" in drafted["next_step"]
    plan = drafted["plan"]
    assert plan["user_message"] == q and len(plan["sub_questions"]) >= 1

    checked = tools.question_plan_check(json.dumps(plan, ensure_ascii=False))
    core = qp.check(plan, g, repo, lex, source="host")
    assert {k: checked[k] for k in qp.compact_check(core)} == _norm(qp.compact_check(core))
    assert checked["status"] == core["status"] and checked["unknowns"] == _norm(core["unknowns"])
    with _store(repo) as st:
        row = st.get("question_plans", checked["plan_id"])
    assert row["source"] == "host" and row["plan"] == plan and row["status"] == core["status"]
    assert list(checked)[:2] == ["status", "plan_id"] and checked["next_step"]


def test_question_plan_check_needs_clarification_and_invalid(repo, tools):
    from verinoda import question_plan as qp

    amb = tools.question_plan_check(json.dumps(AMBIGUOUS_PLAN))
    assert "error" not in amb and amb["status"] == "needs_clarification"
    (c,) = amb["clarifications"]
    assert c["kind"] == "entity" and [o["value"] for o in c["options"]][-2:] == ["__all__", "__other__"]
    assert "clarification_id" in amb["next_step"]

    broken = {**AMBIGUOUS_PLAN, "sub_questions": []}
    bad = tools.question_plan_check(json.dumps(broken))
    assert bad["error"] == "invalid_plan" and bad["tool"] == "question_plan_check" and bad["hint"]
    assert bad["problems"] == _norm(qp.check(broken, None)["errors"])
    with _store(repo) as st:
        assert st.get("question_plans", bad["plan_id"])["status"] == "invalid"  # recorded, never lost
    garbage = tools.question_plan_check("{not json")
    assert garbage["error"] == "invalid_plan" and "not valid JSON" in garbage["problems"][0]["msg"]
    with _store(repo) as st:
        assert st.get("question_plans", garbage["plan_id"])["plan"] == {"unparsed": "{not json"}
    # a file path is never read as a plan (JSON travels in the argument, D1)
    path = tools.question_plan_check(str(repo / "pyproject.toml"))
    assert path["error"] == "invalid_plan" and "JSON" in path["message"]


def test_lexicon_show_equals_core(repo, tools):
    from verinoda import index, lexicon

    res = tools.lexicon_show("sipariş")
    core = _norm(lexicon.show(repo, "sipariş", graph=index.load(repo)))
    assert {k: res[k] for k in core} == core and "never evidence" in res["note"]
    assert any("order" in s["targets"] for s in res["seed"])
    assert tools.lexicon_show(" ")["error"] == "invalid_argument"


# -- analysis & claims ------------------------------------------------------------

def test_analyze_records_claims_like_core(repo, tools, analysis):
    from verinoda.claims import STATUSES, VERIFIED, Claims

    assert analysis["analysis_id"].startswith("ana_")
    assert {"question", "intents", "snapshot", "claims", "unknowns", "critique", "usage"} <= set(analysis)
    assert list(analysis)[:3] == ["understood_as", "subquestions", "plan_check"]
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


def test_claim_list(repo, tools, analysis):  # runs before the other analyses of this module add claims
    res = tools.claim_list(limit=50)
    with _store(repo) as st:
        rows = st.claims(limit=50)
    assert res["count"] == len(rows) and [c["id"] for c in res["claims"]] == [r["id"] for r in rows]
    assert {c["id"] for c in analysis["claims"]} <= {c["id"] for c in res["claims"]}
    only = tools.claim_list(status="statically_verified")
    assert only["claims"] and all(c["status"] == "statically_verified" for c in only["claims"])
    assert tools.claim_list(status="made_up")["error"] == "invalid_argument"


def test_analyze_reuses_the_kept_graph_when_the_tree_is_unchanged(fresh_repo, monkeypatch):
    from verinoda import analysis as core_analysis
    from verinoda import index

    t = AtlasTools(fresh_repo)
    t.node_inspect("place_order")
    seen = []
    real = core_analysis.analyze

    def spy(*a, **k):
        seen.append(k.get("graph"))
        return real(*a, **k)

    monkeypatch.setattr(core_analysis, "analyze", spy)
    res = t.analyze("where is compute_total defined?")
    assert res["status"] == "answered" and seen[-1] is t._graph_cache[1]
    assert t.cache_stats["graph_loads"] == 1
    # an edit since the snapshot: analyze must re-index and load the new graph itself
    _edit(fresh_repo / "orders" / "pricing.py", '"""Pricing rules."""', '"""Pricing rules (edited)."""')
    res2 = t.analyze("where is compute_total defined?")
    assert seen[-1] is None and res2["snapshot"]["id"] != res["snapshot"]["id"]  # re-indexed by analyze
    t.node_inspect("compute_total")
    assert t.cache_stats["graph_loads"] == 2  # the re-indexed graph replaces the kept one
    g = index.load(fresh_repo)
    assert t._graph_cache[1].G.number_of_nodes() == g.G.number_of_nodes()


def test_analyze_with_a_host_plan(repo, tools):
    from verinoda import question_plan as qp

    plan = tools.question_plan_draft("where is compute_total defined?")["plan"]
    plan["restated_goal_user_lang"] = "Understood as: the location of compute_total"
    res = tools.analyze(plan_json=json.dumps(plan))
    assert "error" not in res and res["status"] == "answered" and res["plan_source"] == "host"
    assert res["question"] == plan["user_message"] and res["understood_as"] == plan["restated_goal_user_lang"]
    assert list(res)[:3] == ["understood_as", "subquestions", "plan_check"]
    assert any(c["text"].startswith("`compute_total()` is defined at orders/pricing.py") for c in res["claims"])
    with _store(repo) as st:
        assert qp.get_plan(st, res["plan_id"])["source"] == "host"

    # needs_clarification is a normal result: nothing analysed, the clarifications to ask
    amb = tools.analyze(plan_json=json.dumps(AMBIGUOUS_PLAN))
    assert "error" not in amb and amb["status"] == "needs_clarification" and amb["claims"] == []
    assert amb["clarifications"] and amb["plan_check"]["status"] == "needs_clarification"

    # an invalid plan is an error that names the problems
    bad = tools.analyze(plan_json=json.dumps({**AMBIGUOUS_PLAN, "language": "klingon"}))
    assert bad["error"] == "invalid_plan" and bad["problems"] and "question_plan_draft" in bad["hint"]
    assert any(pr["at"] == "/language" for pr in bad["problems"])
    with _store(repo) as st:
        assert st.get("analyses", bad["analysis_id"])["result"]["status"] == "invalid_plan"
    not_json = tools.analyze(plan_json="plans/q.json")
    assert not_json["error"] == "invalid_plan" and "analysis_id" not in not_json


def test_analyze_keeps_the_interpretation_under_a_small_cap(repo, analysis):
    small = AtlasTools(repo, max_chars=4000)
    res = small.analyze(QUESTION)
    assert res["truncated"] is True and _size(res) <= 4000
    assert list(res)[:3] == ["understood_as", "subquestions", "plan_check"]
    assert res["understood_as"] == analysis["understood_as"]
    assert [s["id"] for s in res["subquestions"]] == [s["id"] for s in analysis["subquestions"]]
    cut = res["truncation"]["cut"]
    assert not any(k.startswith(("understood_as", "subquestions", "plan_check")) for k in cut), cut
    assert any(k.startswith(("steps", "critique", "claims")) for k in cut)


def test_plan_audit_equals_core(repo, tools, analysis):
    from verinoda import analysis as core_analysis

    res = tools.plan_audit(analysis["analysis_id"], refresh=False)
    with _store(repo) as st:
        core = core_analysis.audit(st, repo, analysis["analysis_id"], refresh=False)
    assert res == _norm(core) and list(res)[0] == "changed"
    assert [s["id"] for s in res["subquestions"]] == [s["id"] for s in analysis["subquestions"]]
    refreshed = tools.plan_audit(analysis["analysis_id"])
    assert refreshed["refreshed"]["mode"] == "noop"
    missing = tools.plan_audit("ana_does_not_exist")
    assert missing["error"] == "not_found" and "analysis ids from analyze" in missing["hint"]


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
    from verinoda import evidence as evmod

    cid, eid = _first_claim_with(tools, analysis, "source_code")
    res = tools.evidence_inspect(eid)
    with _store(repo) as st:
        ev = st.evidence(eid)
    assert res["evidence"]["id"] == eid and res["evidence"]["content_hash"] == ev["content_hash"]
    assert res["verifying"] is True and res["source_rank"] == evmod.SOURCE_RANK["source_code"]
    rc = res["recheck"]
    assert rc["performed"] is True and rc["ok"] is True and rc["reason"] == "cited lines unchanged"
    details = evmod.check_source(repo, ev).details()
    assert {k: rc[k] for k in details} == _norm(details) and rc["status"] == "same" and rc["candidate"] is None
    a, b = ev["line_start"], ev["line_end"]
    assert rc["current_text"] == evmod.read_lines(repo / ev["path"], a, min(b, a + 29))
    assert any(c["claim"] == cid and c["relation"] == "supports" for c in res["cited_by"])

    _, gid = _first_claim_with(tools, analysis, "graph_edge")
    graph_ev = tools.evidence_inspect(gid)
    assert graph_ev["verifying"] is False and graph_ev["recheck"]["performed"] is False
    assert "never verifies" in graph_ev["note"]
    assert tools.evidence_inspect("evd_nope")["error"] == "not_found"


def test_claim_verify_and_challenge_use_core(repo, tools, analysis):
    from verinoda import critique, index, workflow
    from verinoda.claims import Claims

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
    from verinoda import evidence as evmod
    from verinoda.claims import Claims

    t = AtlasTools(fresh_repo)
    rel = "orders/pricing.py"
    a = _line_of(fresh_repo, rel, "def compute_total")
    with _store(fresh_repo) as st:
        snap = st.latest_snapshot()
        ev = evmod.source_evidence(fresh_repo, rel, a, a + 2, commit=snap["commit_sha"])
        c = Claims(st, fresh_repo).create(f'{rel}:{a + 1} contains: subtotal = sum(i["price"] * i["qty"] for i in items)',
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
    assert rc["status"] == "changed"
    assert "* 2" in rc["current_text"]

    up = t.index_update()
    assert up["mode"] == "incremental" and rel in up["changed"]["modified"]
    assert c["id"] in {s["id"] for s in up["stale"]}
    after = t.claim_inspect(c["id"])
    assert after["status"] == "stale" and after["history"][-1]["to_status"] == "stale"
    assert t.index_update()["mode"] == "noop"


# -- precise resolution & runtime observation -------------------------------------------------

def test_resolve_call_equals_core(repo, tools):
    from verinoda import precise

    stable = ("path", "line", "token", "kind", "targets", "receiver", "verdict")
    line = _line_of(repo, "orders/service.py", "total = compute_total(")
    res = tools.resolve_call("orders/service.py", line, "compute_total()", target_path="orders/pricing.py",
                             target_line=_line_of(repo, "orders/pricing.py", "def compute_total"))
    if not precise.available()[0]:
        assert res["status"] == "no precise answer" and res["answer"] is None and "pip install" in res["next_step"]
        return
    core = precise.resolve_call(repo, "orders/service.py", line, "compute_total()", target_path="orders/pricing.py",
                                target_line=_line_of(repo, "orders/pricing.py", "def compute_total"))
    assert {k: res.get(k) for k in stable} == _norm({k: core.get(k) for k in stable})
    assert res["kind"] == "definitive" and res["verdict"] == "confirms" and "definitive" in res["note"]

    save_line = _line_of(repo, "orders/service.py", "repo.save(")
    dyn = tools.resolve_call("orders/service.py", save_line, "save", target_path="orders/repository.py",
                             target_line=_line_of(repo, "orders/repository.py", "def save"))
    assert dyn["kind"] == "dynamic" and dyn["verdict"] == "undetermined"  # a parameter receiver never verifies

    none = tools.resolve_call("orders/nothing.js", 3, "save")
    assert none == {"answer": None, "status": "no precise answer", "site": "orders/nothing.js:3", "target": "save",
                    "resolver": precise.available()[1], "why": none["why"], "next_step": none["next_step"]}
    assert "index.scip" in none["why"] and "--scip" in none["next_step"]
    outside = tools.resolve_call("../outside.py", 1, "f")
    assert outside["status"] == "no precise answer" and "outside the repository" in outside["why"]
    assert tools.resolve_call("orders/service.py", 0, "save")["line"] == 1  # clamped, not an exception
    assert "definitive" in dyn["note"]
    assert tools.resolve_call("", 3, "save")["error"] == "invalid_argument"


def test_runtime_observe_reports_the_cli_summary(repo, tools):
    from verinoda import index
    from verinoda.runtime import trace

    res = tools.runtime_observe(symbols=["apply_discount"], timeout=240)
    assert "error" not in res, res
    g = index.load(repo)
    nid = g.resolve("apply_discount")[0]
    assert res["tests_selected"] == trace.select_tests(g, ["apply_discount"])
    assert "tests/test_service.py::test_empty_order_rejected" in res["tests_selected"]  # run, but never reached
    reach = res["target_reach"]["apply_discount"]
    assert res["complete"] is True and reach == {"node": nid, "tests": GOLD_DISCOUNT_TESTS, "n": 4, "observed": True}
    assert "edges" not in res and "evidence" not in res and res["edges_total"] > 0 and res["evidence_records"] > 0
    assert not (res.get("completeness") or {}).get("breach") and res["limits"]
    assert all(b["site"].startswith("orders/pricing.py:") for b in res["boundary_for_targets"])
    assert _size(res) <= tools.max_chars and list(res)[:2] == ["run_id", "complete"]
    with _store(repo) as st:
        assert trace.load_run(st, res["run_id"])["complete"]

    one = tools.runtime_observe(test_ids=["tests/test_service.py::test_place_and_fetch_roundtrip"],
                                symbols=["OrderRepository.save"], timeout=240)
    assert "tests_selected" not in one and one["scope"] == "1 selected test id(s)"
    roundtrip = "tests/test_service.py::test_place_and_fetch_roundtrip"
    assert one["target_reach"]["OrderRepository.save"]["tests"] == [roundtrip]
    assert any(b["callee"] == "sqlite3.connect" and b["site"] == "orders/repository.py:10"
               for b in one["boundary_for_targets"])

    nothing = tools.runtime_observe(symbols=["zz_no_such_symbol_qq"])
    assert nothing["status"] == "unknown" and "run_id" not in nothing and "test_ids" in nothing["next_step"]


def test_runtime_observe_returns_exactly_the_cli_summary(monkeypatch, repo, tools):
    from verinoda import cli, index
    from verinoda.runtime import trace

    canned = {"run_id": "rtr_1", "experiment_id": "exp_1", "complete": False, "scope": "2 selected test id(s)",
              "tracer": "sys.monitoring", "outcome": "fail", "tests_requested": ["t1", "t2"],
              "completeness": {"breach": "max_edges", "degraded_after": 12, "tests_run": 2, "trace_complete": False},
              "tests": {"t1": "passed", "t2": "failed"}, "edges": [{"x": i} for i in range(500)], "edges_total": 500,
              "boundary": [{"site": "orders/pricing.py:14", "callee": "builtins.round", "tests": ["t1"]},
                           {"site": "orders/api.py:9", "callee": "json.loads", "tests": ["t2"]}],
              "boundary_total": 2, "target_reach": {}, "evidence": [{}] * 7, "limits": ["run-scoped"],
              "cost": {"cpu_s": 1.0, "wall_s": 2.0, "stats": {"big": list(range(100))}}, "duration_s": 2.5,
              "error": "tracer budget breached", "next_step": "observe fewer tests"}
    seen = []

    def fake_observe(store, repo_, ids, **kw):
        seen.append((list(ids), kw.get("targets"), kw.get("timeout")))
        return canned

    monkeypatch.setattr(trace, "observe", fake_observe)
    res = tools.runtime_observe(symbols=["apply_discount"], timeout=30)
    g = index.load(repo)
    selected = trace.select_tests(g, ["apply_discount"])
    assert seen == [(selected, ["apply_discount"], 30.0)]
    assert res == _norm(cli._observe_summary(canned, g, ["apply_discount"], selected))
    assert "edges" not in res and res["completeness"]["breach"] == "max_edges" and res["error"]
    with_ids = tools.runtime_observe(test_ids=["tests/test_pricing.py::test_compute_total"])
    assert seen[-1] == (["tests/test_pricing.py::test_compute_total"], [], None)
    assert with_ids == _norm(cli._observe_summary(canned, g, [], None))
    assert tools.runtime_observe(test_ids="not-a-list-but-one-id")["run_id"] == "rtr_1"  # one id is accepted
    assert tools.runtime_observe(timeout="soon")["error"] == "invalid_argument"


# -- references & feedback (adapters over lazily imported modules) ---------------------

def test_research_adapter_passes_cli_arguments(monkeypatch, repo, tools):
    calls = []
    fake = types.ModuleType("verinoda.research")

    def research(store, repo_, reference, *, ref=None, topic=None, kind="auto"):
        calls.append(("research", type(store).__name__, Path(repo_), reference, ref, topic, kind))
        return {"status": "ok", "reference": reference, "rows": [{"i": i, "pad": "x" * 50} for i in range(2000)]}

    def compare(store, local_repo, reference, *, ref=None, topic):
        calls.append(("compare", type(store).__name__, Path(local_repo), reference, ref, topic))
        return {"status": "ok", "topic": topic}

    fake.research, fake.compare = research, compare
    monkeypatch.setitem(sys.modules, "verinoda.research", fake)

    res = tools.reference_research("https://example.org/ref.git", ref="v1.2", topic="persistence")
    assert calls[-1] == ("research", "Store", repo, "https://example.org/ref.git", "v1.2", "persistence", "auto")
    assert res["status"] == "ok" and res["truncated"] is True and _size(res) <= tools.max_chars
    assert res["truncation"]["cut"]["rows"]["total"] == 2000
    tools.reference_research("paper.pdf", kind="paper")
    assert calls[-1][-1] == "paper"
    assert tools.reference_research("x", kind="rumour")["error"] == "invalid_argument"
    assert tools.reference_research()["error"] == "invalid_argument"  # neither a reference nor a resolution

    res = tools.reference_compare("../elsewhere", "order persistence", ref="main")
    assert calls[-1] == ("compare", "Store", repo, "../elsewhere", "main", "order persistence")
    assert res == {"status": "ok", "topic": "order persistence"}
    assert tools.reference_compare("../elsewhere", "")["error"] == "invalid_argument"


def test_reference_resolve_adapter_passes_cli_arguments(monkeypatch, repo, tools):
    calls = []
    fake = types.ModuleType("verinoda.references")

    def resolve(store, repo_, text, *, explicit=(), network="cache", local_intent=None):
        calls.append((type(store).__name__, Path(repo_), text, list(explicit), network, local_intent))
        return {"id": "rrs_1", "status": "partial", "big": "x" * 100,
                "references": [{"id": "r1", "status": "unresolved"}, {"id": "r2", "status": "pinned"}]}

    def compact(result):
        return {"id": result["id"], "status": result["status"], "references": result["references"]}

    fake.resolve, fake.compact = resolve, compact
    monkeypatch.setitem(sys.modules, "verinoda.references", fake)
    res = tools.reference_resolve("see https://github.com/psf/requests/tree/v2.31.0",
                                  references=["https://example.org/r.git@v1", " "], network="off", local_intent=True)
    assert calls[-1] == ("Store", repo, "see https://github.com/psf/requests/tree/v2.31.0",
                         ["https://example.org/r.git@v1"], "off", True)
    assert {k: res[k] for k in ("id", "status", "references")} == compact(resolve(None, repo, "", explicit=()))
    assert "resolution_id='rrs_1', reference_id='r2'" in res["research_with"]  # the pinned one
    tools.reference_resolve("plain question")
    assert calls[-1][-2:] == ("cache", None)  # config default (research.network, else cache); no local intent
    n = len(calls)
    bad = tools.reference_resolve("x", network="sometimes")
    assert bad["error"] == "invalid_argument" and set(bad["valid"]) == {"off", "cache", "on"} and len(calls) == n
    assert tools.reference_resolve("  ")["error"] == "invalid_argument"


def test_reference_resolve_and_research_by_resolution(offline, fresh_repo, tagged_ref):
    from verinoda import references

    ref_repo, tagged = tagged_ref
    t = AtlasTools(fresh_repo)
    text = "does the reference repository save orders the same way?"
    res = t.reference_resolve(text, references=[f"{ref_repo}@v1.0"], network="off")
    assert "error" not in res, res
    with _store(fresh_repo) as st:
        stored = st.get("reference_resolutions", res["id"])  # recorded, append-only
        again = references.compact(references.resolve(st, fresh_repo, text, explicit=[f"{ref_repo}@v1.0"],
                                                      network="off"))
    # exactly references.compact() of the resolution the tool made (plus the research hint)
    assert {k: v for k, v in res.items() if k != "research_with"} == _norm(references.compact(stored["result"]))
    (r1,) = res["references"]
    assert r1["status"] == "pinned" and r1["value"] == tagged and r1["basis"] == "url_tag"
    stable = ("id", "class", "status", "what", "pin", "basis", "value")
    assert [{k: r.get(k) for k in stable} for r in again["references"]] == [{k: r1.get(k) for k in stable}]

    rs = t.reference_research(resolution_id=res["id"], reference_id=r1["id"], topic="order persistence")
    assert rs.get("status") == "ok", rs
    assert rs["resolved_commit"] == tagged  # exactly the pin, not the branch head after the tag
    assert rs["resolution_id"] == res["id"] and rs["reference_id"] == r1["id"] and _size(rs) <= t.max_chars

    assert t.reference_research(resolution_id=res["id"])["error"] == "invalid_argument"
    both = t.reference_research(str(ref_repo), resolution_id=res["id"], reference_id=r1["id"])
    assert both["error"] == "invalid_argument" and "not both" in both["message"]
    unres = t.reference_resolve("is it the same in https://github.com/psf/requests/tree/v2.31.0 ?", network="off")
    (u1,) = unres["references"]
    assert unres["status"] == "unresolved" and u1["status"] == "unresolved" and "research_with" not in unres
    unknown = t.reference_research(resolution_id=unres["id"], reference_id=u1["id"])
    assert unknown["status"] == "unresolved" and unknown["reference_status"] == "unresolved"
    assert "network is off" in unknown["why"] and unknown["next_step"] and "error" not in unknown
    missing = t.reference_research(resolution_id="rrs_nope", reference_id="r1")
    assert missing["error"] == "not_found" and "reference_resolve" in missing["hint"]
    wrong = t.reference_research(resolution_id=res["id"], reference_id="r99")
    assert wrong["error"] == "not_found" and wrong["valid"] == [r1["id"]]


def test_feedback_adapter_passes_cli_arguments(monkeypatch, repo, tools):
    calls = []
    fake = types.ModuleType("verinoda.feedback")

    def add(store, repo_, text, *, claim_id=None, reference=None, ref=None, correction=None,
            expect_pattern=None, expect_in=None, references=None):
        calls.append(("add", Path(repo_), text, claim_id, reference, ref, correction, expect_pattern, expect_in,
                      references))
        return {"id": "fb_1", "status": "open"}

    def process(store, repo_, fid, *, topic=None, network=None):
        calls.append(("process", fid, topic, network))
        if fid == "fb_boom":
            raise RuntimeError("verification crashed")
        return {"id": fid, "status": "processed"}

    def resolve(store, repo_, fid, verdict, *, reason, evidence_ids, correction=None):
        calls.append(("resolve", fid, verdict, reason, evidence_ids, correction))
        return {"id": fid, "verdict": verdict}

    fake.add, fake.process, fake.resolve = add, process, resolve
    monkeypatch.setitem(sys.modules, "verinoda.feedback", fake)

    res = tools.feedback_submit("the save path is wrong", claim_id="clm_1", correction="it uses X",
                                expect_pattern=r"INSERT INTO", expect_in="orders/*.py", topic="persistence",
                                references=["https://example.org/a.git@v2", "https://example.org/b.git"])
    assert calls[-2] == ("add", repo, "the save path is wrong", "clm_1", None, None, "it uses X",
                         "INSERT INTO", "orders/*.py", ["https://example.org/a.git@v2", "https://example.org/b.git"])
    assert calls[-1] == ("process", "fb_1", "persistence", None) and res == {"id": "fb_1", "status": "processed"}

    n = len(calls)
    assert tools.feedback_submit("note only", process=False) == {"id": "fb_1", "status": "open"}
    assert len(calls) == n + 1 and calls[-1][0] == "add" and calls[-1][-1] is None

    assert tools.feedback_process("fb_1", network="off", topic="persistence") == {"id": "fb_1",
                                                                                  "status": "processed"}
    assert calls[-1] == ("process", "fb_1", "persistence", "off")
    n = len(calls)
    assert tools.feedback_process("fb_1", network="always")["error"] == "invalid_argument" and len(calls) == n

    fake.add = lambda *a, **k: {"id": "fb_boom", "status": "open"}
    failed = tools.feedback_submit("will crash")
    assert failed["error"] == "process_failed" and failed["feedback_id"] == "fb_boom" and "fb_boom" in failed["hint"]
    assert "feedback_process" in failed["hint"]

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
        importlib.import_module("verinoda.research")
        importlib.import_module("verinoda.feedback")
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
    fb = t.feedback_submit("this claim is wrong", claim_id=cid, process=False)
    assert fb.get("id") and fb.get("status") and not fb.get("error"), fb
    processed = t.feedback_process(fb["id"], network="off")
    assert not processed.get("error") and _size(processed) <= t.max_chars, processed
    done = t.feedback_resolve(fb["id"], "unresolved", "not enough evidence either way", [])
    assert not done.get("error"), done
    with _store(fresh_repo) as st:
        row = st.get("feedback", fb["id"])
        assert row is not None and row["claim_id"] == cid
    assert t.feedback_process("fb_does_not_exist")["error"] == "not_found"


def test_missing_optional_modules_give_structured_error(monkeypatch, tools):
    for mod in ("verinoda.research", "verinoda.feedback", "verinoda.references", "verinoda.runtime.trace",
                "verinoda.precise", "verinoda.codecheck"):
        monkeypatch.setitem(sys.modules, mod, None)
    for res in (tools.reference_research("https://example.org/r.git"),
                tools.reference_compare("https://example.org/r.git", "topic"),
                tools.feedback_submit("x"),
                tools.feedback_process("fb_1"),
                tools.feedback_resolve("fb_1", "confirmed", "r", []),
                tools.reference_resolve("requests 2.31"),
                tools.runtime_observe(symbols=["apply_discount"]),
                tools.resolve_call("orders/service.py", 22, "save"),
                tools.code_check(paths=["orders/service.py"]),
                tools.api_members("orders.service")):
        assert res["error"] == "unavailable" and res["hint"] and "could not be imported" in res["message"]
    assert "text" in tools.project_query("which module computes the order total?")  # the rest still works


def test_internal_errors_are_structured_and_stdout_stays_clean(monkeypatch, capsys, repo):
    from verinoda import retrieval

    tools = AtlasTools(repo)  # no kept answers: every call reaches the (patched) core

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
    assert "text" in tools.project_query(QUESTION)  # lock released, server usable


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
        assert res["tool"] == name and "verinoda scan" in res["hint"] and res["message"]
    assert not (plain / ".verinoda").exists()


def test_initialised_but_unindexed_repo(tmp_path):
    from verinoda import workflow

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "calc.py").write_text("def add(a, b):\n    return a + b\n\n\ndef twice(x):\n    return add(x, x)\n",
                                  encoding="utf-8")
    workflow.init(proj)
    t = AtlasTools(proj)
    res = t.project_query("add")
    assert res["error"] == "no_index" and "index_update" in res["hint"]
    assert t.question_plan_draft("where is add?")["error"] == "no_index"
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


def test_cap_response_keeps_named_keys_first_and_cuts_them_last():
    keep = ("understood_as", "subquestions", "plan_check")
    big = {"analysis_id": "ana_1",
           "steps": [{"i": i, "pad": "s" * 50} for i in range(100)],
           "claims": [{"id": f"clm_{i}", "text": "c" * 80} for i in range(100)],
           "subquestions": [{"id": f"q{i}", "claim_ids": [f"clm_{j}" for j in range(10)]} for i in range(3)],
           "understood_as": "Anladığım: siparişin kaydedildiği yer",
           "plan_check": {"status": "ready", "links": [{"mention": f"m{i}", "status": "linked"} for i in range(5)]}}
    res = cap_response(big, 3000, first=("steps",), keep=keep)
    assert list(res)[:3] == list(keep) and _size(res) <= 3000
    assert {k: res[k] for k in keep} == _norm({k: big[k] for k in keep})  # untouched
    assert {"steps", "claims"} <= set(res["truncation"]["cut"])
    assert cap_response({"a": 1, "understood_as": "x"}, 3000, keep=keep) == {"understood_as": "x", "a": 1}
    tight = cap_response(big, 1200, keep=keep)  # nothing else left to cut: the kept keys are cut too
    assert _size(tight) <= 1200 and "over_limit" not in tight["truncation"]
    assert tight["understood_as"] == big["understood_as"]


# -- real stdio round trip ------------------------------------------------------------------

def _server_params(repo: Path):
    from mcp.client.stdio import StdioServerParameters

    env = {"GRAPHIFY_OUT": os.environ.get("GRAPHIFY_OUT", ".verinoda/index"), "PYTHONIOENCODING": "utf-8"}
    if os.environ.get("PYTHONPATH"):  # the server must import what this test process imports
        env["PYTHONPATH"] = os.environ["PYTHONPATH"]
    return StdioServerParameters(command=sys.executable,
                                 args=["-m", "verinoda", "mcp", "serve", "--repo", str(repo)],
                                 cwd=str(repo), env=env)


def _is_error(res) -> bool:
    v = getattr(res, "is_error", None)
    return bool(getattr(res, "isError", False) if v is None else v)


def _structured(res):
    sc = getattr(res, "structured_content", None)
    return getattr(res, "structuredContent", None) if sc is None else sc


def _payload(res) -> dict:
    data = json.loads(res.content[0].text)
    sc = _structured(res)
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
    from verinoda import index, lexicon, retrieval
    from verinoda import question_plan as qp
    from verinoda.claims import Claims

    plan_q = "where is compute_total defined?"

    async def body(s):
        listed = await s.list_tools()
        q = await s.call_tool("project_query", {"question": QUESTION, "max_items": 5})
        q_again = await s.call_tool("project_query", {"question": QUESTION, "max_items": 5})
        qj = await s.call_tool("project_query", {"question": QUESTION, "max_items": 5, "format": "json"})
        d = await s.call_tool("question_plan_draft", {"question": plan_q})
        plan = json.loads(d.content[0].text)["plan"]
        c = await s.call_tool("question_plan_check", {"plan_json": json.dumps(plan)})
        a = await s.call_tool("analyze", {"plan_json": json.dumps(plan), "budget_seconds": 90})
        bad_plan = await s.call_tool("analyze", {"plan_json": '{"schema": "nope"}'})
        cid = _payload(a)["claims"][0]["id"]
        ci = await s.call_tool("claim_inspect", {"claim_id": cid})
        bad = await s.call_tool("claim_inspect", {"claim_id": "clm_does_not_exist"})
        rr = await s.call_tool("reference_resolve", {"text": "is this the same in requests 2.31?", "network": "off"})
        return [t.name for t in listed.tools], q, q_again, qj, d, c, a, bad_plan, ci, bad, rr

    names, q, q_again, qj, d, c, a, bad_plan, ci, bad, rr = _session(repo, tmp_path / "server.err", body)
    assert sorted(names) == sorted(TOOL_NAMES)

    core = retrieval.retrieve(index.load(repo), QUESTION, retrieval.Budget(max_items=5, max_chars=6000))
    assert not _is_error(q)
    text = retrieval.render_text(core, 6000)
    assert q.content[0].text == text  # plain text on the wire, not an escaped JSON string
    sc = _structured(q)
    if sc is not None:
        assert sc == {"format": "text", "question": QUESTION, "text": text}
    assert q_again.content[0].text == text
    assert _payload(qj) == _norm(core)

    dp = _payload(d)
    assert dp["plan"] == _norm(qp.draft(plan_q, index.load(repo), lexicon.load(repo)))
    cp_check = _payload(c)
    assert not _is_error(c) and cp_check["status"] == "ready" and cp_check["plan_id"].startswith("qpl_")

    ap = _payload(a)
    assert not _is_error(a) and ap["analysis_id"].startswith("ana_") and ap["claims"]
    assert ap["plan_source"] == "host" and list(ap)[:3] == ["understood_as", "subquestions", "plan_check"]
    bpp = _payload(bad_plan)
    assert bpp["error"] == "invalid_plan" and bpp["problems"]
    if hasattr(bad_plan, "is_error"):
        assert bad_plan.is_error is True
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
    rp = _payload(rr)
    assert not _is_error(rr) and rp["id"].startswith("rrs_") and "summary" in rp
    assert "verinoda mcp: serving" in (tmp_path / "server.err").read_text(encoding="utf-8", errors="replace")


def test_stdio_server_starts_in_unscanned_dir(tmp_path):
    plain = tmp_path / "unscanned"
    plain.mkdir()
    (plain / "app.py").write_text("def main():\n    return 1\n", encoding="utf-8")

    async def body(s):
        listed = await s.list_tools()
        out = []
        for name, args in (("project_query", {"question": "anything"}),
                           ("claim_inspect", {"claim_id": "clm_x"}),
                           ("question_plan_draft", {"question": "anything"}),
                           ("index_update", {})):
            out.append(await s.call_tool(name, args))
        return [t.name for t in listed.tools], out

    names, results = _session(plain, tmp_path / "server.err", body)
    assert sorted(names) == sorted(TOOL_NAMES)
    for res in results:
        p = _payload(res)
        assert p["error"] == "not_initialised" and "verinoda scan" in p["hint"]
    assert not (plain / ".verinoda").exists()


def test_default_repo_serves_the_only_initialised_subfolder_of_a_workspace(tmp_path, capsys, monkeypatch):
    ws = tmp_path / "workspace"
    for name in ("tool", "other", ".hidden"):
        (ws / name).mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "elsewhere"))
    assert mcp_server.default_repo(ws) == ws  # nothing initialised: the start folder, no guess
    (ws / "tool" / ".verinoda").mkdir()
    (ws / "tool" / ".verinoda" / "atlas.db").write_bytes(b"")
    (ws / ".hidden" / ".verinoda").mkdir()
    (ws / ".hidden" / ".verinoda" / "atlas.db").write_bytes(b"")
    assert mcp_server.default_repo(ws) == ws / "tool"
    assert "only initialised sub-folder tool" in capsys.readouterr().err
    # inside a project the nearest project wins, as for the CLI
    (ws / "tool" / "pkg").mkdir()
    assert mcp_server.default_repo(ws / "tool" / "pkg") == ws / "tool"
    (ws / "other" / ".verinoda").mkdir()
    (ws / "other" / ".verinoda" / "atlas.db").write_bytes(b"")
    assert mcp_server.default_repo(ws) == ws  # two candidates: no guess, the log names them
    assert "other, tool" in capsys.readouterr().err
