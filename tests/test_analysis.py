"""Analysis loop on a scanned copy of examples/orders_app: claims with file:line evidence,
INFERRED hops never statically verified, budgets produce unknowns, optional test runs,
decision records for "why" questions, and no invented claims when nothing matches.
"""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import json  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import analysis, workflow  # noqa: E402
from verinoda import evidence as evmod  # noqa: E402
from verinoda.claims import VERIFIED, Claims  # noqa: E402
from verinoda.store import open_store  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "orders_app"
FLOW_Q = "How does an order get from the API handler to the database?"
LOCATOR = re.compile(r"^[\w./-]+\.\w+:\d+(-\d+)?$")

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


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


@pytest.fixture(scope="module")
def proj(tmp_path_factory):
    repo = _copy_example(tmp_path_factory.mktemp("analysis") / "orders_app")
    workflow.init(repo)
    st = open_store(repo)
    workflow.scan(st, repo)
    yield repo, st
    st.close()


@pytest.fixture(scope="module")
def flow(proj):
    repo, st = proj
    return analysis.analyze(st, repo, FLOW_Q)


def _evidence_rows(st, cid):
    return Claims(st).evidence(cid)


def test_intents_for():
    assert analysis.intents_for(FLOW_Q)[:2] == ["flow", "dataflow"]
    assert analysis.intents_for("Why does OrderRepository use SQLite?")[0] == "why"
    assert "config" in analysis.intents_for("Which environment variables configure pricing?")
    assert analysis.intents_for("compute_total") == ["location"]


def test_flow_question_produces_flow_claims_with_file_line_evidence(proj, flow):
    repo, st = proj
    assert {"flow", "dataflow"} <= set(flow["intents"])
    flows = [c for c in flow["claims"] if c["text"].startswith("Data path ")]
    assert flows, flow["claims"]
    save = next(c for c in flows if "create_order_handler() -> place_order() -> .save()" in c["text"])
    assert "orders/repository.py:15" in save["text"]
    for c in flows:
        rows = _evidence_rows(st, c["id"])
        src = [e for e in rows if e["source_type"] == "source_code"]
        assert src and all(LOCATOR.match(e["locator"]) for e in src)
        for e in src:  # every cited line really exists and still hashes the same
            assert evmod.check_source(repo, e).ok
    save_locs = {e["locator"] for e in _evidence_rows(st, save["id"])}
    assert {"orders/api.py:18", "orders/service.py:22", "orders/repository.py:17"} <= save_locs
    assert flow["snapshot"]["commit"] and flow["usage"]["tool_calls"] > 0 and not flow["usage"]["exhausted"]
    stored = st.get("analyses", flow["analysis_id"])
    assert stored["result"]["claims"] == [c["id"] for c in flow["claims"]]
    assert len(json.dumps(flow)) < 40_000  # small, structured output


def test_inferred_hops_are_never_statically_verified(proj, flow):
    repo, st = proj
    cl = Claims(st, repo)
    seen_inferred = 0
    for c in flow["claims"]:
        full = cl.get(c["id"])
        spec = full["spec"] or {}
        inferred = (full["kind"] == "flow" and any(h["confidence"] != "EXTRACTED" for h in spec.get("hops", []))) \
            or (full["kind"] == "relation" and spec.get("confidence") != "EXTRACTED")
        if inferred:
            seen_inferred += 1
            assert c["status"] != "statically_verified", c
            assert c["status"] not in VERIFIED, c
            assert c["uncertainties"], c
    assert seen_inferred >= 1
    save = next(c for c in flow["claims"] if ".save()" in c["text"] and c["text"].startswith("Data path"))
    assert save["status"] == "strong_inference" and "at least one hop is INFERRED" in save["uncertainties"]
    # A fully EXTRACTED hop chain may be verified, and its hops are named on the cited lines.
    extracted = [c for c in flow["claims"] if c["text"].startswith("Data path") and
                 "at least one hop is INFERRED" not in c["uncertainties"]]
    assert all(c["status"] == "statically_verified" for c in extracted)


def test_inferred_relation_claims_are_capped(proj):
    repo, st = proj
    res = analysis.analyze(st, repo, "Which function calls fetch_order and get?")
    cl = Claims(st, repo)
    rel = [cl.get(c["id"]) for c in res["claims"] if cl.get(c["id"])["kind"] == "relation"]
    inferred = [c for c in rel if (c["spec"] or {}).get("confidence") != "EXTRACTED"]
    assert inferred, [c["text"] for c in rel]
    assert all(c["status"] not in VERIFIED for c in inferred)


def test_budget_exhaustion_yields_unknown_with_reason(proj):
    repo, st = proj
    res = analysis.analyze(st, repo, FLOW_Q, budget=analysis.Budget(tool_calls=3))
    assert res["usage"]["exhausted"] == "tool-call budget 3 spent"
    u = next(x for x in res["unknowns"] if x["why"] == "tool-call budget 3 spent")
    assert u["next_step"] and "budget" in u["next_step"]
    assert not any(c["text"].startswith("Data path") for c in res["claims"])  # never reached, not guessed
    assert all(c.get("challenged") is False for c in res["claims"])  # honestly flagged
    assert res["critique"] == []


def test_time_budget_exhaustion_is_reported(proj):
    repo, st = proj
    res = analysis.analyze(st, repo, FLOW_Q, budget=analysis.Budget(seconds=0.0))
    assert res["usage"]["exhausted"].startswith("time budget")
    assert any(u["why"].startswith("time budget") for u in res["unknowns"])


def test_run_tests_produces_an_experiment_verified_claim(proj):
    repo, st = proj
    res = analysis.analyze(st, repo, "Which tests cover compute_total?", run_tests=True)
    runs = [c for c in res["claims"] if c["text"].startswith("Tests that reach")]
    assert runs, (res["claims"], res["unknowns"])
    c = next(c for c in runs if "compute_total" in c["text"])
    assert c["status"] == "experiment_verified" and c["confidence"] > 0.9
    rows = _evidence_rows(st, c["id"])
    tr = next(e for e in rows if e["source_type"] == "test_result")
    assert tr["relation"] == "supports" and tr["meta"]["outcome"] == "pass" and tr["meta"]["isolation"] == "process"
    exp = st.get("experiments", tr["meta"]["experiment_id"])
    assert exp["status"] == "pass" and "tests/test_pricing.py::test_compute_total" in exp["command"]
    full = Claims(st, repo).get(c["id"])
    assert "tests/test_pricing.py" in full["subjects"]  # editing the tests makes it stale too
    assert any(x["step"] == "experiment" for x in res["steps"])


def test_test_run_claim_goes_stale_and_is_only_restored_by_a_fresh_run(tmp_path):
    repo = _copy_example(tmp_path / "orders_app")
    workflow.init(repo)
    st = open_store(repo)
    try:
        workflow.scan(st, repo)
        res = analysis.analyze(st, repo, "Which tests cover compute_total?", run_tests=True, challenge=False)
        c = next(c for c in res["claims"] if c["text"].startswith("Tests that reach `compute_total()`"))
        assert c["status"] == "experiment_verified"
        p = repo / "orders" / "pricing.py"
        original = p.read_text(encoding="utf-8")
        p.write_text(original + "\n# harmless trailing comment\n", encoding="utf-8")
        assert c["id"] in {s["id"] for s in workflow.update(st, repo)["stale"]}

        v = workflow.verify(st, repo, c["id"])  # no re-run: the old pass says nothing about new code
        assert v["after"]["status"] == "stale" and "orders/pricing.py" in v["unconfirmed_files"]
        assert v["note"].endswith("re-run with --run")

        v = workflow.verify(st, repo, c["id"], run=True)
        assert v["experiment"]["outcome"] == "pass" and v["after"]["status"] == "experiment_verified"
        assert Claims(st, repo).get(c["id"])["snapshot_id"] == st.latest_snapshot()["id"]

        p.write_text(original.replace('i["price"] * i["qty"]', 'i["price"] + i["qty"]'), encoding="utf-8")
        workflow.update(st, repo)
        v = workflow.verify(st, repo, c["id"], run=True)
        assert v["experiment"]["outcome"] == "fail"
        assert v["after"]["status"] == "contradicted"  # a fresh failing run refutes "these tests pass"
        assert any(e["relation"] == "refutes" and e["source_type"] == "test_result"
                   for e in Claims(st, repo).evidence(c["id"]))
    finally:
        st.close()


def test_why_question_finds_the_adr_as_primary_source(proj):
    repo, st = proj
    res = analysis.analyze(st, repo, "Why does OrderRepository use SQLite?")
    assert res["intents"][0] == "why"
    adr = next(c for c in res["claims"] if c["text"].startswith("Decision record docs/adr/0001-sqlite-persistence.md"))
    assert adr["status"] == "primary_source_verified" and "(status: accepted)" in adr["text"]
    ev = next(e for e in _evidence_rows(st, adr["id"]) if e["relation"] == "supports")
    assert ev["source_type"] == "design_doc" and ev["path"] == "docs/adr/0001-sqlite-persistence.md"
    assert evmod.check_source(repo, ev).ok
    assert any("rationale" in u["question"] for u in res["unknowns"])  # docs state intent, not behaviour


def test_question_without_matching_terms_yields_unknowns_not_claims(proj):
    repo, st = proj
    n_before = st.one("SELECT COUNT(*) AS n FROM claims")["n"]
    res = analysis.analyze(st, repo, "What does the flux capacitor quantize?")
    assert res["claims"] == []
    assert res["unknowns"] and res["unknowns"][0]["why"] == "no graph node or text matched the question terms"
    assert res["unknowns"][0]["next_step"]
    assert st.one("SELECT COUNT(*) AS n FROM claims")["n"] == n_before


def test_config_question_cites_env_reads(proj):
    repo, st = proj
    res = analysis.analyze(st, repo, "Which environment variable sets the discount threshold?")
    cfg = [c for c in res["claims"] if "reads environment variable" in c["text"]]
    assert any("ORDERS_DISCOUNT_THRESHOLD" in c["text"] and "orders/config.py:7" in c["text"] for c in cfg)
    assert all(c["status"] == "statically_verified" for c in cfg)


def test_repeated_analysis_reuses_claims_of_the_same_snapshot(proj, flow):
    repo, st = proj
    again = analysis.analyze(st, repo, FLOW_Q)
    assert {c["id"] for c in again["claims"]} == {c["id"] for c in flow["claims"]}
    assert again["analysis_id"] != flow["analysis_id"]


def test_analysis_refreshes_a_changed_tree_before_citing(tmp_path):
    repo = _copy_example(tmp_path / "orders_app")
    workflow.init(repo)
    st = open_store(repo)
    try:
        workflow.scan(st, repo)
        old = st.latest_snapshot()["id"]
        p = repo / "orders" / "pricing.py"
        p.write_text(p.read_text(encoding="utf-8") + "\n\ndef surcharge(total):\n    return total + 1\n",
                     encoding="utf-8")
        res = analysis.analyze(st, repo, "Where is surcharge defined?")
        assert res["snapshot"]["id"] != old and res["steps"][0]["step"] == "refresh_index"
        loc = next(c for c in res["claims"] if c["text"].startswith("`surcharge()` is defined at"))
        assert "orders/pricing.py:18-19" in loc["text"] and loc["status"] == "statically_verified"
    finally:
        st.close()


def test_entry_point_that_writes_directly_is_a_one_node_path(tmp_path):
    """An entry point that is itself the sink must not crash the flow step."""
    repo = tmp_path / "mini"
    (repo / "app").mkdir(parents=True)
    (repo / "app" / "api.py").write_text(
        "import sqlite3\n\n\ndef store_handler(value):\n    conn = sqlite3.connect('x.db')\n"
        "    conn.execute('INSERT INTO t VALUES (?)', (value,))\n    conn.commit()\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    workflow.init(repo)
    st = open_store(repo)
    try:
        workflow.scan(st, repo)
        res = analysis.analyze(st, repo, "How does data flow from the handler to the database?")
        flows = [c for c in res["claims"] if c["text"].startswith("Data path")]
        assert flows and flows[0]["text"].startswith("Data path store_handler() reaches persistence")
        assert flows[0]["status"] in VERIFIED | {"strong_inference"}
    finally:
        st.close()


# -- regressions: spans, aliases, budgets, Turkish intents, tests wording, test runs, reuse ----------

def test_turkish_intent_keywords():
    got = analysis.intents_for("Sipariş API'den veritabanına nasıl ulaşıyor?")
    assert {"flow", "dataflow"} <= set(got)
    assert analysis.intents_for("NEDEN SQLİTE KULLANILIYOR?") == ["why"]  # Turkish capital I / dotted İ
    assert analysis.intents_for("Ortam değişkeni nerede okunuyor?") == ["config", "location"]  # not "impact"
    assert "impact" in analysis.intents_for("pricing.py değişirse ne bozulur?")
    assert "tests" in analysis.intents_for("Hangi testler kapsıyor?")
    assert analysis.intents_for("Why is the build hanging?") == ["why"]  # 'hanging' is not Turkish 'hangi'
    assert analysis.fold_tr("İŞLEM ıŞık") == "islem isik"


def test_line_mentions_accepts_import_aliases(tmp_path):
    (tmp_path / "m.py").write_text("from pkg.analyze import god_nodes as _god_nodes\n\n\ndef f(G):\n"
                                   "    return _god_nodes(G)\n", encoding="utf-8")
    assert analysis._line_mentions(tmp_path, "m.py:5", "god_nodes()") == (True, "_god_nodes")
    assert analysis._line_mentions(tmp_path, "m.py:4", "god_nodes()")[0] is False


def _mini(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root


def test_location_claim_cites_the_full_definition_span(tmp_path):
    body = "".join(f"    step_{i} = {i}\n" for i in range(40))
    repo = _mini(tmp_path / "big", {"app/core.py": "def long_worker(x):\n" + body + "    return x\n"})
    workflow.init(repo)
    st = open_store(repo)
    try:
        workflow.scan(st, repo)
        res = analysis.analyze(st, repo, "Where is long_worker defined?")
        loc = next(c for c in res["claims"] if c["text"].startswith("`long_worker()` is defined at"))
        assert "app/core.py:1-42" in loc["text"]  # the whole function, not a 14-line excerpt window
        ev = next(e for e in _evidence_rows(st, loc["id"]) if e["source_type"] == "source_code")
        assert ev["locator"] == "app/core.py:1-42" and evmod.check_source(repo, ev).ok
    finally:
        st.close()


def test_context_budget_stops_claims_and_names_skipped_subquestions(proj, flow):
    repo, st = proj
    small = analysis.Budget(context_tokens=320)
    res = analysis.analyze(st, repo, FLOW_Q, budget=small)
    assert res["usage"]["exhausted"].startswith("context budget")
    assert len(res["claims"]) < len(flow["claims"])
    claims_tokens = sum(len(json.dumps(c, ensure_ascii=False)) for c in res["claims"]) // 4
    assert claims_tokens <= 320
    named = {u["question"] for u in res["unknowns"]}
    assert analysis.SUBQUESTIONS["flow"] in named  # the flow/dataflow sub-question was not answered
    assert all(u["next_step"] for u in res["unknowns"])
    assert res["usage"]["context_chars"] >= sum(len(json.dumps(c, ensure_ascii=False)) for c in res["claims"])


def test_repeat_reuses_contradicted_claims_and_marks_them(tmp_path):
    repo = _copy_example(tmp_path / "orders_app")
    workflow.init(repo)
    st = open_store(repo)
    try:
        workflow.scan(st, repo)
        q = "Where is compute_total defined?"
        first = analysis.analyze(st, repo, q, challenge=False)
        c0 = first["claims"][0]
        cl = Claims(st, repo)
        ev = evmod.source_evidence(repo, "orders/pricing.py", 1, 1, commit=None, meta={"test": "refute"})
        cl.attach(c0["id"], ev, "refutes")
        cl.set_status(c0["id"], "contradicted", reason="test", downgrade=True)
        assert cl.get(c0["id"])["status"] == "contradicted"
        n_before = st.one("SELECT COUNT(*) AS n FROM claims")["n"]
        again = analysis.analyze(st, repo, q, challenge=False)
        re0 = next(c for c in again["claims"] if c["id"] == c0["id"])
        assert re0["reused"] is True and re0["status"] == "contradicted"
        assert all(c.get("reused") for c in again["claims"])
        assert st.one("SELECT COUNT(*) AS n FROM claims")["n"] == n_before  # no duplicates
        assert "reused" not in first["claims"][0]
    finally:
        st.close()


def test_unchallenged_claims_say_why(proj):
    repo, st = proj
    q = "Where is the order saved to the database?"
    off = analysis.analyze(st, repo, q, challenge=False)
    assert off["claims"] and all(c["not_challenged_reason"] == "disabled" for c in off["claims"])
    capped = analysis.analyze(st, repo, q, max_claims=1)
    challenged = [c for c in capped["claims"] if "not_challenged_reason" not in c]  # claims are listed answer first
    assert len(challenged) == 1
    assert all(c["not_challenged_reason"] == "claim_limit" for c in capped["claims"] if c not in challenged)
    broke = analysis.analyze(st, repo, q, budget=analysis.Budget(tool_calls=3))
    assert all(c["challenged"] is False and c["not_challenged_reason"] == "budget" for c in broke["claims"])


def test_tests_intent_says_statically_reaches_and_negative_watches_tests(proj):
    repo, st = proj
    res = analysis.analyze(st, repo, "Which tests cover compute_total?")
    pos = [c for c in res["claims"] if "statically reaches `compute_total()`" in c["text"]]
    assert pos and not any("exercised" in c["text"] for c in res["claims"])
    neg = analysis.analyze(st, repo, "Which tests cover load_settings?")
    none = next(c for c in neg["claims"] if c["text"] == "No test statically reaches `load_settings()`")
    assert Claims(st, repo).get(none["id"])["spec"]["watch"] == "tests"


def test_pytest_ids_include_the_class_and_only_python_tests_run(tmp_path, monkeypatch):
    from verinoda import experiments
    from verinoda import index as idx

    repo = _mini(tmp_path / "cls", {
        "app/calc.py": "def add(a, b):\n    return a + b\n",
        "tests/test_calc.py": "from app.calc import add\n\n\nclass TestAdd:\n    def test_small(self):\n"
                              "        assert add(1, 2) == 3\n\n\ndef test_plain():\n    assert add(2, 2) == 4\n",
    })
    workflow.init(repo)
    st = open_store(repo)
    seen = {}

    def fake_run(store, repo_, argv, **kw):
        seen["argv"], seen["timeout"] = argv, kw.get("timeout")
        eid = evmod.add(store, {"source_type": "test_result", "locator": "run fake", "path": None,
                                "commit_sha": None, "content_hash": "sha256:0", "excerpt": "no tests ran",
                                "meta": {"outcome": "inconclusive",
                                         "inconclusive_reason": "pytest collected no tests"}})
        return {"id": "exp_fake", "isolation": "process", "outcome": "inconclusive", "matches_expectation": False,
                "duration_s": 0.1, "evidence_id": eid, "inconclusive_reason": "pytest collected no tests",
                "next_step": "check the test ids/arguments and re-run"}

    monkeypatch.setattr(experiments, "run", fake_run)
    monkeypatch.setattr(experiments, "python_for", lambda r: "/project/.venv/bin/python", raising=False)
    try:
        workflow.scan(st, repo)
        g = idx.load(repo)
        meth = next(n for n in g.G.nodes if g.label(n).strip(".()") == "test_small")
        plain = next(n for n in g.G.nodes if g.label(n).strip(".()") == "test_plain")
        assert analysis._pytest_id(g, meth) == "tests/test_calc.py::TestAdd::test_small"
        assert analysis._pytest_id(g, plain) == "tests/test_calc.py::test_plain"
        res = analysis.analyze(st, repo, "Which tests cover add?", run_tests=True, budget=analysis.Budget(seconds=45))
        assert seen["argv"][:4] == ["/project/.venv/bin/python", "-m", "pytest", "-q"]
        assert set(seen["argv"][4:]) <= {"tests/test_calc.py::TestAdd::test_small", "tests/test_calc.py::test_plain"}
        assert seen["timeout"] <= 45  # the remaining time budget, not the 120 s default
        run = next(c for c in res["claims"] if c["text"].startswith("Tests that reach `add()`"))
        assert run["status"] == "unknown"  # inconclusive: neither verified nor contradicted
        assert any("inconclusive" in u and "no tests" in u for u in run["uncertainties"])
        assert {e["relation"] for e in _evidence_rows(st, run["id"])} == {"qualifies"}
    finally:
        st.close()


def test_refused_index_refresh_is_an_unknown_and_qualifies_every_claim(tmp_path, monkeypatch):
    repo = _copy_example(tmp_path / "orders_app")
    workflow.init(repo)
    st = open_store(repo)
    try:
        workflow.scan(st, repo)
        prev = st.latest_snapshot()
        p = repo / "orders" / "pricing.py"
        p.write_text(p.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8")

        def refused(store, repo_):
            return {"snapshot": prev, "error": "the indexer did not rewrite the graph",
                    "hint": "verinoda scan --force", "stale": [], "changed_count": 1, "mode": "index_refused"}

        monkeypatch.setattr(workflow, "update", refused)
        res = analysis.analyze(st, repo, "Where is compute_total defined?")
        u = res["unknowns"][0]
        assert u["why"].startswith("index could not be refreshed: the indexer did not rewrite the graph")
        assert "verinoda scan --force" in u["next_step"]
        assert res["snapshot"]["id"] == prev["id"] and res["claims"]
        assert all(any(x.startswith("index could not be refreshed") for x in c["uncertainties"])
                   for c in res["claims"])
    finally:
        st.close()


def test_config_claims_rank_env_vars_by_the_question_words_they_carry(tmp_path):
    """Many variables share the project prefix; the ones naming the question's words come first."""
    reads = "".join(f'    a{i} = os.environ.get("TOOL_{name}")\n' for i, name in enumerate(
        ["ALPHA", "BETA", "CACHE_DIR", "DEBUG", "EDITOR", "FORMAT"]))
    repo = _mini(tmp_path / "cfg", {
        "tool/settings.py": "import os\n\n\ndef load():\n" + reads + "    return a0\n",
        "tool/querylog.py": "import os\n\n\ndef log_path():\n"
                            '    if os.environ.get("TOOL_QUERY_LOG_DISABLE"):\n        return None\n'
                            '    return os.environ.get("TOOL_QUERY_LOG")\n',
    })
    workflow.init(repo)
    st = open_store(repo)
    try:
        workflow.scan(st, repo)
        res = analysis.analyze(st, repo, "Which environment variables control the tool's query log?")
        cfg = [c["text"] for c in res["claims"] if "reads environment variable" in c["text"]]
        assert cfg[:2] and all("TOOL_QUERY_LOG" in t for t in cfg[:2]), cfg
        # a "where" answer is not needed for the location claims to appear alongside another intent
        assert any(" is defined at tool/querylog.py:4-" in c["text"] for c in res["claims"])
    finally:
        st.close()


# -- question plans (docs/DESIGN.md D1-D9) --------------------------------------------------------

from verinoda import question_plan as qp  # noqa: E402


def _host_plan(message: str, sub_questions: list[dict], mentions: list[dict] | None = None, **extra) -> dict:
    return {"schema": qp.SCHEMA_ID, "user_message": message, "language": "en", "restated_goal": message,
            "sub_questions": sub_questions, "mentions": mentions or [], **extra}


def test_every_analysis_runs_through_a_stored_plan(proj, flow):
    repo, st = proj
    assert flow["status"] == "answered" and flow["plan_source"] == "fallback"
    assert flow["understood_as"] and flow["plan_check"]["status"] in ("ready", "needs_clarification")
    row = st.get("question_plans", flow["plan_id"])
    assert row["analysis_id"] == flow["analysis_id"] and row["source"] == "fallback"
    assert row["plan"]["user_message"] == FLOW_Q and row["plan"]["derived_by"] == qp.DRAFT_RULES
    ana = st.get("analyses", flow["analysis_id"])
    assert ana["plan_id"] == flow["plan_id"] and ana["result"]["plan_id"] == flow["plan_id"]
    (sq,) = flow["subquestions"]
    assert sq["intent"] == "flow" and sq["status"] in ("met", "met_with_inference") and sq["claim_ids"]
    assert set(sq["claim_ids"]) <= {c["id"] for c in flow["claims"]}
    assert ana["result"]["subquestions"][0]["status"] == sq["status"]


def test_compound_question_gets_one_verdict_per_clause(proj):
    repo, st = proj
    res = analysis.analyze(st, repo, "Where is the 10% discount applied and what threshold controls it?")
    q1, q2 = res["subquestions"]
    assert (q1["intent"], q2["intent"]) == ("locate", "config")
    assert q1["status"] == "met" and q2["status"] == "met"
    by_id = {c["id"]: c for c in res["claims"]}
    q2_texts = [by_id[c]["text"] for c in q2["claim_ids"] if c in by_id]
    assert any("ORDERS_DISCOUNT_THRESHOLD" in t and "orders/config.py:7" in t for t in q2_texts), q2_texts
    assert any("apply_discount" in by_id[c]["text"] for c in q1["claim_ids"] if c in by_id)


def test_a_name_written_as_code_that_does_not_exist_is_unmet_not_substituted(proj):
    repo, st = proj
    for q in ("Where is place_orders defined?", "place_orders fonksiyonu nerede tanımlı?"):
        res = analysis.analyze(st, repo, q)
        (sq,) = res["subquestions"]
        assert sq["status"] == "unmet" and sq["claim_ids"] == [], q
        assert res["unknowns"][0]["why"] == ("no symbol named `place_orders` in this repository; nearest: "
                                             "place_order (orders/service.py:19)"), q
    # with another name that does exist the sub-question still runs, but it is not answered as asked
    sq = {"id": "q1", "intent": "locate", "done_when": {"kind": "location_verified"}}
    rows = [{"id": "c1", "kind": "location", "status": "statically_verified"}]
    assert analysis.judge(sq, rows, {}) == "met"
    assert analysis.judge(sq, rows, {"not_found": ["m1"]}) == "unmet"


def test_turkish_question_is_understood_and_answered(proj):
    repo, st = proj
    res = analysis.analyze(st, repo, "İndirim nerede uygulanıyor?")
    assert res["understood_as"].startswith("Anladığım") and res["subquestions"][0]["intent"] == "locate"
    assert any(c["text"].startswith("`apply_discount()` is defined at orders/pricing.py:11-15") for c in res["claims"])
    assert res["subquestions"][0]["status"] == "met"


def test_host_plan_needing_clarification_writes_no_claims(proj):
    repo, st = proj
    n_before = st.one("SELECT COUNT(*) AS n FROM claims")["n"]
    p = _host_plan("Where is order handled?", [{"id": "q1", "text": "Where is order handled?", "intent": "locate",
                                                "mentions": ["m1"],
                                                "done_when": {"kind": "location_verified", "subjects": ["m1"],
                                                              "detail": "file:line"}}],
                   [{"id": "m1", "text": "order", "kind": "symbol"}], on_ambiguity="ask")
    res = analysis.analyze(st, repo, "", plan=p)
    assert res["status"] == "needs_clarification" and res["claims"] == []
    (c,) = res["clarifications"]
    assert c["kind"] == "entity" and [o["value"] for o in c["options"]][-2:] == ["__all__", "__other__"]
    assert st.one("SELECT COUNT(*) AS n FROM claims")["n"] == n_before
    assert st.get("question_plans", res["plan_id"])["status"] == "needs_clarification"
    assert st.get("analyses", res["analysis_id"])["result"]["status"] == "needs_clarification"
    # the same plan with answer_all proceeds, and its claims say what was not confirmed
    res2 = analysis.analyze(st, repo, "", plan={**p, "on_ambiguity": "answer_all"})
    assert res2["status"] == "answered" and res2["claims"]
    assert any(u.startswith("open clarification c-m1") for c in res2["claims"] for u in c["uncertainties"])


def test_invalid_plan_is_refused_before_any_work(proj):
    repo, st = proj
    n_before = st.one("SELECT COUNT(*) AS n FROM claims")["n"]
    bad = _host_plan("Where is compute_total?", [{"id": "q1", "text": "x", "intent": "locate", "mentions": ["m1"],
                                                  "done_when": {"kind": "location_verified", "detail": "x"}}],
                     [{"id": "m1", "text": "QueryTokenizer", "kind": "symbol"}])
    res = analysis.analyze(st, repo, "", plan=bad)
    assert res["status"] == "invalid_plan" and res["claims"] == []
    assert "ungrounded_text" in {e["code"] for e in res["errors"]}
    assert st.one("SELECT COUNT(*) AS n FROM claims")["n"] == n_before
    res = analysis.analyze(st, repo, "", plan="{not json")
    assert res["status"] == "invalid_plan" and res["errors"][0]["code"] == "schema"


def test_plan_can_come_from_a_file(proj, tmp_path):
    repo, st = proj
    p = _host_plan("Where is compute_total defined?",
                   [{"id": "q1", "text": "Where is compute_total defined?", "intent": "locate", "mentions": ["m1"],
                     "done_when": {"kind": "location_verified", "subjects": ["m1"], "detail": "file:line"}}],
                   [{"id": "m1", "text": "compute_total", "kind": "symbol"}])
    f = tmp_path / "plan.json"
    f.write_bytes(json.dumps(p).encode("utf-8"))
    res = analysis.analyze(st, repo, "", plan=str(f))
    assert res["status"] == "answered" and res["plan_source"] == "host"
    assert res["question"] == p["user_message"] and res["subquestions"][0]["status"] == "met"


def test_off_topic_question_yields_unknowns_not_location_claims(proj):
    repo, st = proj
    res = analysis.analyze(st, repo, "How does the payment gateway retry declined charges?")
    assert not [c for c in res["claims"] if " is defined at " in c["text"]]
    u = next(u for u in res["unknowns"] if "occur nowhere" in u["why"])
    assert "'payment'" in u["why"] and "'gateway'" in u["why"] and u["next_step"]
    assert res["subquestions"][0]["status"] == "unmet"


def test_flow_question_never_falls_back_to_unrelated_paths(proj):
    repo, st = proj
    res = analysis.analyze(st, repo, "How does compute_total reach the database?")
    assert not [c for c in res["claims"] if c["text"].startswith(("Data path", "Call path"))], res["claims"]
    assert any("passes through" in u["why"] or "no directed call path" in u["why"] for u in res["unknowns"])
    assert res["subquestions"][0]["status"] == "unmet"


def test_named_source_selects_its_own_data_paths(proj):
    repo, st = proj
    res = analysis.analyze(st, repo, "How does get_order_handler load an order from storage?")
    paths = [c["text"] for c in res["claims"] if c["text"].startswith("Data path")]
    assert paths and all(t.startswith("Data path get_order_handler()") for t in paths), paths


def test_decision_record_matched_only_by_a_symbol_is_an_inference(tmp_path):
    repo = _mini(tmp_path / "adr", {
        "app/pricing.py": "def compute_total(items):\n    return round(sum(items), 2)\n",
        "docs/adr/0001-caching.md": "# ADR 1: Cache prices\n\nStatus: accepted\n\nWe cache the results of "
                                    "`compute_total` in memory to avoid recomputing them.\n",
    })
    workflow.init(repo)
    st = open_store(repo)
    try:
        workflow.scan(st, repo)
        res = analysis.analyze(st, repo, "Why does compute_total round to two decimals?")
        dec = next(c for c in res["claims"] if c["text"].startswith("Decision record docs/adr/0001-caching.md"))
        assert " mentions `compute_total`" in dec["text"] and "explains" not in dec["text"]
        assert dec["status"] in ("strong_inference", "weak_inference")  # at most an inference, never verified
        assert any("matched only because the document names" in u for u in dec["uncertainties"])
        topic = analysis.analyze(st, repo, "Why are prices cached in memory?")
        exp = next(c for c in topic["claims"] if c["text"].startswith("Decision record docs/adr/0001-caching.md"))
        assert " explains it" in exp["text"] and exp["status"] == "primary_source_verified"
    finally:
        st.close()


def test_counter_probe_removes_a_test_that_does_not_reach_the_code(proj, monkeypatch):
    from verinoda import critique

    repo, st = proj
    real = critique.challenge

    def probing(store, repo_, cid, **kw):
        out = real(store, repo_, cid, **kw)
        c = store.claim(cid)
        if c["kind"] == "tests" and "test_empty_order_rejected" in c["text"]:
            out["findings"].append({"check": "early_exit", "result": "fail",
                                    "detail": "test_empty_order_rejected stops at pytest.raises before "
                                              "apply_discount (tests/test_service.py:15)"})
        return out

    monkeypatch.setattr(critique, "challenge", probing)
    res = analysis.analyze(st, repo, "Which tests exercise apply_discount?")
    tests_claims = [c for c in res["claims"] if "reaches `apply_discount()`" in c["text"]
                    or "shown to reach `apply_discount()`" in c["text"]]
    assert tests_claims and all("test_empty_order_rejected()" not in c["text"].split("(counter-probe")[0]
                                for c in tests_claims), tests_claims
    corrected = next(c for c in tests_claims if "counter-probe excluded test_empty_order_rejected" in c["text"])
    assert corrected["not_challenged_reason"] == "correction_of_challenged_claim"
    assert any(u.startswith("counter-probe:") for u in corrected["uncertainties"])
    old = next(x for x in res["critique"] if x.get("superseded_by") == corrected["id"])
    assert Claims(st, repo).get(old["claim"])["status"] == "contradicted"
    sq = res["subquestions"][0]
    assert corrected["id"] in sq["claim_ids"] and old["claim"] not in sq["claim_ids"]


def test_critique_errors_and_current_files_are_handled(proj, monkeypatch):
    from verinoda import critique

    repo, st = proj
    seen = {}

    def picky(store, repo_, cid, *, graph=None, actor="critique", current_files=None):
        seen["current_files"] = current_files
        raise RuntimeError("probe crashed")

    monkeypatch.setattr(critique, "challenge", picky)
    res = analysis.analyze(st, repo, "Where is compute_total defined?")
    assert isinstance(seen["current_files"], dict) and "orders/pricing.py" in seen["current_files"]
    assert res["claims"] and all(c["not_challenged_reason"] == "critique_error: RuntimeError" for c in res["claims"])


def test_update_errors_are_unknowns_not_crashes(tmp_path, monkeypatch):
    repo = _copy_example(tmp_path / "orders_app")
    workflow.init(repo)
    st = open_store(repo)
    try:
        workflow.scan(st, repo)
        p = repo / "orders" / "pricing.py"
        p.write_bytes(p.read_bytes() + b"\n# edited\n")

        def boom(store, repo_):
            raise RuntimeError("index lock held")

        monkeypatch.setattr(workflow, "update", boom)
        res = analysis.analyze(st, repo, "Where is compute_total defined?")
        assert res["unknowns"][0]["why"] == "index could not be refreshed: RuntimeError: index lock held"
        assert res["claims"]
        monkeypatch.setattr(workflow, "update", lambda store, repo_: {"snapshot": None, "error": "no snapshot"})
        res = analysis.analyze(st, repo, "Where is compute_total defined?")
        assert res["unknowns"][0]["why"] == "index could not be refreshed: no snapshot" and res["snapshot"]
    finally:
        st.close()


def test_observe_uses_the_runtime_tracer_when_present(proj, monkeypatch):
    repo, st = proj
    monkeypatch.setattr(analysis, "_runtime_observe", lambda: None)
    res = analysis.analyze(st, repo, "Which tests cover compute_total?", observe=True, challenge=False)
    assert any("runtime observation is not available" in u["why"] for u in res["unknowns"])
    calls = []

    def fake_observe(store, repo_, test_ids, *, timeout=None, snapshot=None, graph=None, targets=()):
        calls.append({"test_ids": list(test_ids), "targets": list(targets), "snapshot": snapshot})
        return {"run_id": "rt_1", "complete": True, "claims": [], "unknowns": []}

    monkeypatch.setattr(analysis, "_runtime_observe", lambda: fake_observe)
    res = analysis.analyze(st, repo, "Which tests cover compute_total?", observe=True, challenge=False)
    assert len(calls) == 1  # one tracer run covers every target of the sub-question
    first = calls[0]  # the symbol the question names comes first
    assert "tests/test_pricing.py::test_compute_total" in first["test_ids"]
    assert first["targets"][0] == "orders_pricing_compute_total" and first["snapshot"]["id"]
    assert len(first["test_ids"]) <= analysis.MAX_OBSERVED_TESTS
    assert any(s["step"] == "runtime_observe" for s in res["steps"])


def test_behaviour_proposition_is_checked(proj):
    repo, st = proj
    msg = "Is validate_items called before compute_total in place_order?"
    p = _host_plan(msg, [{"id": "q1", "text": msg, "intent": "behaviour", "mentions": ["m1", "m2", "m3"],
                          "done_when": {"kind": "proposition_checked", "subjects": ["m1", "m2", "m3"],
                                        "detail": "call order inside place_order",
                                        "proposition": "validate_items before compute_total in place_order"}}],
                   [{"id": "m1", "text": "validate_items", "kind": "symbol"},
                    {"id": "m2", "text": "compute_total", "kind": "symbol"},
                    {"id": "m3", "text": "place_order", "kind": "symbol"}])
    res = analysis.analyze(st, repo, "", plan=p)
    (sq,) = res["subquestions"]
    assert sq["proposition"]["holds"] is True and sq["status"] == "met"
    assert sq["proposition"]["lines"] == {"validate_items": 20, "compute_total": 21}
    c = next(c for c in res["claims"] if c["text"].startswith("`place_order` calls"))
    assert c["text"] == ("`place_order` calls `validate_items` at orders/service.py:20 and `compute_total` at "
                         "orders/service.py:21")
    p["sub_questions"][0]["done_when"]["proposition"] = "regex:INSERT INTO orders in:orders/*.py"
    res = analysis.analyze(st, repo, "", plan=p)
    assert res["subquestions"][0]["proposition"]["holds"] is True
    assert any(c["text"] == "orders/repository.py:17 contains `INSERT INTO orders`" for c in res["claims"])


def test_behaviour_without_a_proposition_is_not_supported(proj):
    repo, st = proj
    res = analysis.analyze(st, repo, "What happens when an order is submitted with no items?")
    (sq,) = res["subquestions"]
    assert sq["status"] == "not_supported" and any("proposition" in u["next_step"] for u in sq["unknowns"])
    assert any(c["text"].startswith("`validate_items()` is defined at") for c in res["claims"])


def test_audit_marks_a_subquestion_stale_when_its_claim_goes_stale(tmp_path):
    repo = _copy_example(tmp_path / "orders_app")
    workflow.init(repo)
    st = open_store(repo)
    try:
        workflow.scan(st, repo)
        res = analysis.analyze(st, repo, "Where is compute_total defined?", challenge=False)
        assert res["subquestions"][0]["status"] == "met"
        clean = analysis.audit(st, repo, res["analysis_id"])
        assert clean["subquestions"][0]["now"] == "met" and clean["changed"] == []
        p = repo / "orders" / "pricing.py"
        p.write_bytes(p.read_bytes().replace(b"def compute_total(items: list[dict]) -> float:",
                                             b"def compute_total(lines: list[dict], rate: float = 1.0) -> float:")
                      .replace(b"for i in items)", b"for i in lines)"))
        audit = analysis.audit(st, repo, res["analysis_id"])
        sq = audit["subquestions"][0]
        assert sq["now"] == "stale" and sq["stale_claims"] and audit["changed"] == ["q1"]
        assert audit["plan_id"] == res["plan_id"] and audit["refreshed"]["mode"] != "noop"
    finally:
        st.close()


def test_lexicon_is_built_once_and_reused(proj):
    from verinoda import lexicon

    repo, st = proj
    path = lexicon.lexicon_path(repo)
    if path.exists():
        path.unlink()  # derived data: analysis rebuilds it when missing
    first = analysis.analyze(st, repo, "Where is compute_total defined?", challenge=False)
    assert any(s["step"] == "lexicon" and s["detail"].startswith("built") for s in first["steps"])
    assert lexicon.load(repo).tree_hash == st.latest_snapshot()["tree_hash"]
    again = analysis.analyze(st, repo, "Where is compute_total defined?", challenge=False)
    assert not any(s["step"] == "lexicon" for s in again["steps"])


def test_turkish_storage_source_is_where_the_path_ends(proj):
    """'depodan' (from storage) is the end of the data path, not its entry point."""
    repo, st = proj
    res = analysis.analyze(st, repo, "get_order_handler bir siparişi depodan nasıl yüklüyor?", challenge=False)
    paths = [c["text"] for c in res["claims"] if c["text"].startswith("Data path")]
    assert paths and all(t.startswith("Data path get_order_handler()") for t in paths), paths
    assert any("fetch_order() -> .get()" in t for t in paths)


def test_host_plan_flow_between_named_endpoints_cites_the_sink(proj):
    repo, st = proj
    msg = "How does create_order_handler reach save?"
    p = _host_plan(msg, [{"id": "q1", "text": msg, "intent": "flow", "mentions": ["m1", "m2"],
                          "done_when": {"kind": "path_found", "subjects": ["m1", "m2"], "detail": "call path"}}],
                   [{"id": "m1", "text": "create_order_handler", "kind": "symbol", "role": "source"},
                    {"id": "m2", "text": "save", "kind": "symbol", "role": "target",
                     "candidates": ["OrderRepository.save"]}])
    res = analysis.analyze(st, repo, "", plan=p, challenge=False)
    c = next(c for c in res["claims"] if c["text"].startswith("Call path create_order_handler()"))
    assert c["text"].endswith("reaches persistence (orm-write, sql-write) at orders/repository.py:15")
    assert "supports:source_code:orders/repository.py:17" in c["evidence"]  # the INSERT line
    assert res["subquestions"][0]["status"] in ("met", "met_with_inference")


def test_relative_version_is_never_answered_from_the_working_tree(proj):
    repo, st = proj
    n_before = st.one("SELECT COUNT(*) AS n FROM claims")["n"]
    res = analysis.analyze(st, repo, "Eski sürümde indirim nerede uygulanıyordu?")
    (sq,) = res["subquestions"]
    assert sq["status"] == "blocked_by_clarification" and sq["claim_ids"] == [] and res["claims"] == []
    assert any("not pinned" in u["why"] for u in sq["unknowns"])
    assert res["plan_check"]["clarifications"][0]["kind"] == "version"
    assert st.one("SELECT COUNT(*) AS n FROM claims")["n"] == n_before


def test_a_named_version_is_reported_not_substituted(proj):
    repo, st = proj
    msg = "In v0.3, where is compute_total defined?"
    p = _host_plan(msg, [{"id": "q1", "text": msg, "intent": "locate", "mentions": ["m1"], "references": ["r1"],
                          "done_when": {"kind": "location_verified", "subjects": ["m1"], "detail": "file:line"}}],
                   [{"id": "m1", "text": "compute_total", "kind": "symbol"}],
                   references=[{"id": "r1", "text": "v0.3", "kind": "git_repo",
                                "version": {"spec": "v0.3", "source": "user_explicit", "evidence": "v0.3"}}])
    res = analysis.analyze(st, repo, "", plan=p, challenge=False)
    assert res["claims"] and all(any("the question names v0.3 (v0.3)" in u for u in c["uncertainties"])
                                 for c in res["claims"])
    assert any("only the working tree was analysed" in u["why"] for u in res["unknowns"])


# -- round-3 wiring: precise resolution, call-site codes, spans, groups, observation, references ------------

def _fresh(tmp_path: Path, name: str = "orders_app"):
    """A scanned private copy of orders_app (claims of the shared fixture would be reused by text)."""
    repo = _copy_example(tmp_path / name)
    workflow.init(repo)
    st = open_store(repo)
    workflow.scan(st, repo)
    return repo, st


def _fake_resolver(calls: list, outcomes: dict):
    """A stand-in for ``precise.resolve_call``: ``outcomes[token]`` = verdict | 'dynamic' | 'budget'."""
    from verinoda import callsite

    def fake(repo, path, line, target_label, *, store=None, budget=None, target_path=None, target_line=None):
        token = callsite.target_token(target_label)
        calls.append((path, line, token, target_path, target_line, id(budget)))
        what = outcomes.get(token)
        if what == "budget":
            budget.skipped += 1
            return None
        base = {"tool": "fake 1", "path": path, "line": line, "token": token}
        if what == "dynamic":
            return {**base, "kind": "dynamic", "targets": [], "verdict": "undetermined", "receiver": "parameter",
                    "reason": "method called on a parameter receiver: the runtime class decides"}
        if what == "confirms":
            return {**base, "kind": "definitive", "verdict": "confirms", "receiver": "name", "reason": None,
                    "targets": [{"path": target_path, "line": target_line, "name": token, "type": "function",
                                 "in_repo": True}]}
        if what == "refutes":
            return {**base, "kind": "definitive", "verdict": "refutes", "receiver": "name", "reason": None,
                    "targets": [{"path": "orders/config.py", "line": 5, "name": token, "type": "function",
                                 "in_repo": True}]}
        return None
    return fake


def test_precise_resolution_supports_refutes_or_qualifies_relation_claims(tmp_path, monkeypatch):
    from verinoda import precise

    repo, st = _fresh(tmp_path)
    calls: list = []
    budgets: list = []

    def one_budget(r):
        budgets.append(precise.Budget())
        return budgets[-1]

    monkeypatch.setattr(analysis, "_precise_budget", one_budget)
    monkeypatch.setattr(precise, "resolve_call", _fake_resolver(calls, {
        "get": "confirms", "fetch_order": "refutes", "get_repo": "dynamic", "OrderRepository": "budget"}))
    try:
        res = analysis.analyze(st, repo, FLOW_Q, challenge=False)
        assert len(budgets) == 1 and {c[5] for c in calls} == {id(budgets[0])}  # one budget per analysis
        assert calls and all(c[3] for c in calls)  # the graph's target is always passed
        cl = Claims(st, repo)
        rel = {c["text"]: c for c in res["claims"] if cl.get(c["id"])["kind"] == "relation"}
        # confirms: an INFERRED receiver edge is statically verified by the resolver's definitive answer
        get = rel["`fetch_order()` calls `.get()` (orders/service.py:26)"]
        assert get["status"] == "statically_verified", get
        assert any(e.startswith("supports:static_resolution:orders/service.py:26") for e in get["evidence"])
        assert not any("is INFERRED" in u for u in get["uncertainties"])
        assert not any("not resolved statically" in u for u in get["uncertainties"])  # the resolver settled it
        # refutes: a definitive mismatch contradicts the claim; the call line no longer counts as support
        fo = rel["`get_order_handler()` calls `fetch_order()` (orders/api.py:25)"]
        assert fo["status"] == "contradicted", fo
        rows = _evidence_rows(st, fo["id"])
        refute = [e for e in rows if e["relation"] == "refutes"]
        assert refute and refute[0]["source_type"] == "static_resolution"
        assert refute[0]["meta"]["strength"] == "definitive" and refute[0]["meta"]["verdict"] == "refutes"
        assert not any(e["relation"] == "supports" and e["source_type"] == "source_code" for e in rows)
        assert any("binds to orders/config.py:5" in u for u in fo["uncertainties"])
        # anything else qualifies: at most strong_inference, even for an EXTRACTED call the line states
        gr = rel["`create_order_handler()` calls `get_repo()` (orders/api.py:18)"]
        assert gr["status"] == "strong_inference"
        assert any(e.startswith("qualifies:static_resolution") for e in gr["evidence"])
        assert any(u.startswith("fake 1: dynamic") for u in gr["uncertainties"])
        # budget spent: said so, nothing invented, the call line still states the call
        orr = rel["`get_repo()` calls `OrderRepository` (orders/api.py:12)"]
        assert "precise resolution: not resolved (budget)" in orr["uncertainties"]
        assert orr["status"] == "statically_verified"
        assert res["usage"]["precise"]["skipped"] >= 1
    finally:
        st.close()


def test_without_a_precise_resolver_nothing_changes(tmp_path, monkeypatch):
    from verinoda import precise

    repo, st = _fresh(tmp_path)
    monkeypatch.setattr(analysis, "_precise_budget", lambda r: None)
    monkeypatch.setattr(precise, "resolve_call", lambda *a, **k: pytest.fail("resolver called without a budget"))
    try:
        res = analysis.analyze(st, repo, FLOW_Q, challenge=False)
        assert res["claims"] and "precise" not in res["usage"]
        assert not any("static_resolution" in e for c in res["claims"] for e in c["evidence"])
    finally:
        st.close()


def test_precise_budget_comes_from_config_and_zero_switches_it_off(tmp_path, monkeypatch):
    import json as _json

    from verinoda import precise
    from verinoda.paths import atlas_dir

    monkeypatch.setattr(precise, "available", lambda: (True, "fake 1"))
    atlas_dir(tmp_path).mkdir(parents=True)
    cfg = atlas_dir(tmp_path) / "config.json"
    cfg.write_bytes(_json.dumps({"budget": {"precise_sites": 3, "precise_seconds": 0.5}}).encode("utf-8"))
    b = analysis._precise_budget(tmp_path)
    assert (b.max_sites, b.max_seconds) == (3, 0.5)
    cfg.write_bytes(_json.dumps({"budget": {"precise_sites": 0}}).encode("utf-8"))
    assert analysis._precise_budget(tmp_path) is None  # off, not "every site skipped for budget"
    monkeypatch.setattr(precise, "available", lambda: (False, "jedi is not installed"))
    cfg.write_bytes(b"{}")
    assert analysis._precise_budget(tmp_path) is None


def test_real_precise_resolver_on_orders_app(tmp_path):
    pytest.importorskip("jedi")
    from verinoda import precise

    precise.reset_caches()
    repo, st = _fresh(tmp_path)
    try:
        res = analysis.analyze(st, repo, FLOW_Q, challenge=False)
        assert res["usage"]["precise"]["sites"] >= 1
        rel = {c["text"]: c for c in res["claims"]}
        handler = rel["`get_order_handler()` calls `fetch_order()` (orders/api.py:25)"]
        assert handler["status"] == "statically_verified"
        assert any(e == "supports:static_resolution:orders/api.py:25 definitive"
                   for e in handler["evidence"])
        # repo.get(...) on a parameter: jedi cannot tell the runtime class -> qualifies, never verifies
        get = rel["`fetch_order()` calls `.get()` (orders/service.py:26)"]
        assert get["status"] == "strong_inference"
        assert any(e == "qualifies:static_resolution:orders/service.py:26 dynamic"
                   for e in get["evidence"])
    finally:
        st.close()


def test_relation_status_follows_the_call_site_grade_not_a_name_match(tmp_path):
    from verinoda import index as idx

    repo = _mini(tmp_path / "cs", {"app/m.py": "def helper():\n    return 1\n\n\ndef caller():\n    fn = helper\n"
                                               "    return fn()\n\n\ndef other():\n    return helper()\n"})
    workflow.init(repo)
    st = open_store(repo)
    try:
        workflow.scan(st, repo)
        g = idx.load(repo)
        snap = st.latest_snapshot()
        rec = analysis._Recorder(st, repo, snap, "ana_cs", analysis.Budget())
        node = {g.label(n).strip("()"): n for n in g.G.nodes if g.is_symbol(n)}
        # `fn = helper` names the target but does not call it: entail grades it partial ("not_called")
        d = {"relation": "calls", "confidence": "EXTRACTED", "source_file": "app/m.py", "source_location": "L6"}
        c = analysis._edge_claim(rec, g, node["caller"], node["helper"], d, snap["commit_sha"])
        assert c["status"] == "strong_inference"
        assert any("has no call to it" in u for u in c["uncertainties"])
        assert not any("downgraded" in h["reason"] for h in st.history(c["id"]))  # the request met the rules
        # a call inside another function: the line is outside the claimed caller -> no source support
        d2 = {**d, "source_location": "L11"}
        c2 = analysis._edge_claim(rec, g, node["caller"], node["helper"], d2, snap["commit_sha"])
        assert c2["status"] in ("strong_inference", "weak_inference")
        assert any("not confirmed to name the target" in u and "not inside" in u for u in c2["uncertainties"])
        assert not any(e["source_type"] == "source_code" for e in _evidence_rows(st, c2["id"]))
    finally:
        st.close()


def test_flow_hops_and_sinks_form_one_evidence_group(proj, flow):
    repo, st = proj
    cl = Claims(st, repo)
    flows = [c for c in flow["claims"] if cl.get(c["id"])["kind"] == "flow"]
    assert flows
    for c in flows:
        rows = [e for e in cl.evidence(c["id"]) if e["source_type"] == "source_code"]
        assert rows and all(e["grp"] == "flow" for e in rows), [(e["locator"], e["grp"]) for e in rows]


def test_location_claims_name_their_symbol(proj, flow):
    repo, st = proj
    cl = Claims(st, repo)
    locs = [cl.get(c["id"]) for c in flow["claims"] if " is defined at " in c["text"]]
    assert locs
    for c in locs:
        sym = re.match(r"`([^`]+)`", c["text"]).group(1)
        assert c["spec"]["symbol"] == sym


def test_non_python_spans_come_from_syntax_facts(tmp_path):
    """Audit: 'CHANGELOG.md:1380-1460' (an 80-line cap) was stated as verified for a 10-line section."""
    section = "".join(f"- cache rule {i}\n" for i in range(90))
    repo = _mini(tmp_path / "np", {
        "docs/NOTES.md": "# Notes\n\n## Caching rules\n\n" + section + "\n## Storage rules\n\n- sqlite only\n",
        "web/cart.js": "function addItem(cart, item) {\n  cart.items.push(item);\n  return cart;\n}\n\n\n"
                       "const TAX = 0.2;\n\nfunction cartTotal(cart) {\n  let t = 0;\n  for (const i of cart.items) "
                       "{\n    t += i.price;\n  }\n  return t * (1 + TAX);\n}\n",
    })
    workflow.init(repo)
    st = open_store(repo)
    try:
        workflow.scan(st, repo)
        res = analysis.analyze(st, repo, "Where is cartTotal defined?", challenge=False)
        js = next(c for c in res["claims"] if c["text"].startswith("`cartTotal()` is defined at"))
        assert js["text"].endswith("web/cart.js:9-15") and js["status"] == "statically_verified", js
        res = analysis.analyze(st, repo, "Where are the caching rules written down?", challenge=False)
        md = next(c for c in res["claims"] if c["text"].startswith("`Caching rules` is defined at"))
        # heading at 3, a blank line, 90 items (5-94); the next heading is at 96: the section ends at its
        # last line, well past the old 80-line cap (3-83) and before the next heading
        assert md["text"].endswith("docs/NOTES.md:3-94"), md
        assert md["status"] in VERIFIED
    finally:
        st.close()


def test_a_heuristic_end_line_is_an_inference(tmp_path):
    from types import SimpleNamespace

    (tmp_path / "x.cfgscript").write_bytes(b"block a\n  one\nblock b\n  two\n")
    g = SimpleNamespace(span_basis=lambda n: "heuristic")
    ctx = SimpleNamespace(store=None, repo=tmp_path, g=g)
    a, b, note = analysis._exact_span(ctx, "n1", "x.cfgscript", "a", (1, 2))
    assert (a, b) == (1, 2) and "heuristic" in note
    g.span_basis = lambda n: "ast"
    assert analysis._exact_span(ctx, "n1", "x.py", "a", (1, 2))[2] is None
    g.span_basis = lambda n: "tree-sitter"
    assert "not confirmed" in analysis._exact_span(ctx, "n1", "x.cfgscript", "a", (1, 2))[2]


def test_analysis_hashes_the_tree_once_with_the_stat_cache(proj, monkeypatch):
    from verinoda import critique

    repo, st = proj
    seen = []
    real = analysis.current_state

    def spy(r, **kw):
        seen.append(kw.get("store"))
        return real(r, **kw)

    files = []
    real_ch = critique.challenge

    def ch(store, repo_, cid, **kw):
        files.append(kw.get("current_files"))
        return real_ch(store, repo_, cid, **kw)

    monkeypatch.setattr(analysis, "current_state", spy)
    monkeypatch.setattr(critique, "challenge", ch)
    analysis.analyze(st, repo, "Where is compute_total defined?")
    assert seen == [st]
    assert files and all(f is files[0] for f in files)  # one map, computed once, passed to every critique


def test_observed_edges_never_come_from_test_doubles():
    from types import SimpleNamespace

    g = SimpleNamespace(file=lambda n: "orders/repository.py")
    ctx = SimpleNamespace(g=g)
    spec = {"at": "orders/service.py:22", "target": "orders_repository_orderrepository_save", "target_label": ".save()"}
    ev = {"path": "orders/service.py", "line_start": 22,
          "meta": {"kind": "call_trace", "outcome": "pass", "callee_node": spec["target"], "flags": {}}}
    assert analysis._observed_edge_matches(ctx, spec, ev)
    double = {**ev, "meta": {**ev["meta"], "flags": {"test_double": True}}}
    assert not analysis._observed_edge_matches(ctx, spec, double)
    assert not analysis._observed_edge_matches(ctx, spec, {**ev, "line_start": 23})
    assert not analysis._observed_edge_matches(ctx, spec, {**ev, "meta": {**ev["meta"], "callee_node": "orders_x"}})
    assert not analysis._observed_edge_matches(ctx, spec, {**ev, "meta": {**ev["meta"], "outcome": "fail"}})
    unmapped = {**ev, "meta": {"kind": "call_trace", "outcome": "pass", "callee_path": "orders/repository.py",
                               "callee_qual": "OrderRepository.save", "flags": {}}}
    assert analysis._observed_edge_matches(ctx, spec, unmapped)


@pytest.mark.experiment
def test_observation_supersedes_a_static_tests_claim_it_contradicts(tmp_path):
    """The benchmark's wrong finding: test_empty_order_rejected statically reaches apply_discount but
    stops at pytest.raises before it. Observed, the reach set excludes it (docs/DESIGN.md D28)."""
    import sys as _sys

    if not hasattr(_sys, "monitoring"):
        pytest.skip("the sys.monitoring tracer needs Python 3.12+")
    repo, st = _fresh(tmp_path)
    try:
        res = analysis.analyze(st, repo, "Which tests exercise apply_discount?", observe=True,
                               budget=analysis.Budget(seconds=120))
        assert any(s["step"] == "runtime_observe" for s in res["steps"])
        obs = next(c for c in res["claims"] if c["text"].startswith("Observed in run ")
                   and "`apply_discount()`" in c["text"])
        assert obs["status"] == "experiment_verified", obs
        assert "not by test_empty_order_rejected" in obs["text"]
        for t in ("test_compute_total", "test_discount_applies_above_threshold", "test_no_discount_below_threshold",
                  "test_place_and_fetch_roundtrip"):
            assert t in obs["text"].split(";")[0], t
        assert any(u.startswith("run-scoped") for u in obs["uncertainties"])
        cl = Claims(st, repo)
        full = cl.get(obs["id"])
        assert full["kind"] == "test_run" and full["supersedes"]
        old = cl.get(full["supersedes"])
        assert old["kind"] == "tests" and old["status"] == "contradicted" and old["superseded_by"] == obs["id"]
        assert "test_empty_order_rejected" in old["text"]
        assert old["id"] not in {c["id"] for c in res["claims"]}
        sq = res["subquestions"][0]
        assert obs["id"] in sq["claim_ids"] and old["id"] not in sq["claim_ids"]
        # observed calls support the relation claims they show, never through test doubles
        observed = [c for c in res["claims"] if any(e.startswith("supports:experiment:run ") for e in c["evidence"])
                    and cl.get(c["id"])["kind"] == "relation"]
        assert observed and all(c["status"] == "experiment_verified" for c in observed)
        for c in observed:
            for e in cl.evidence(c["id"]):
                if (e.get("meta") or {}).get("kind") == "call_trace":
                    assert not (e["meta"].get("flags") or {}).get("test_double")
    finally:
        st.close()


def test_reference_comparison_is_resolved_offline_with_a_research_next_step(proj, monkeypatch):
    from verinoda import references

    repo, st = proj
    seen = {}

    def fake_resolve(store, repo_, text, **kw):
        seen.update(kw, text=text)
        return {"id": "rrs_test1", "status": "complete", "summary": {"pinned": 1},
                "references": [{"id": "r1", "class": "git_repo", "status": "pinned", "mentions": ["m1"],
                                "identity": {"canonical_url": "https://example.org/acme/orders"},
                                "requested": {"url": "https://example.org/acme/orders"},
                                "pin": {"display": "v0.3 -> 1234567", "basis": "explicit_text_version",
                                        "value": "1234567"},
                                "mismatches": [], "unresolved": [], "warnings": []}],
                "unbound_mentions": [], "questions_for_user": []}

    monkeypatch.setattr(references, "resolve", fake_resolve)
    msg = "How does apply_discount differ from https://example.org/acme/orders at v0.3?"
    res = analysis.analyze(st, repo, msg, challenge=False)
    assert seen["network"] == "off" and seen["local_intent"] is True  # analyze never goes to the network
    sq = next(s for s in res["subquestions"] if s["intent"] == "compare_reference")
    u = next(u for u in sq["unknowns"] if "rrs_test1" in u["next_step"])
    assert "`verinoda research --resolution rrs_test1 --reference-id r1`" in u["next_step"]
    assert "@ v0.3 -> 1234567 (basis: explicit_text_version)" in u["why"]
    comp = next(r["resolution"] for r in sq["references"] if "resolution" in r)
    assert comp["id"] == "rrs_test1" and comp["references"][0]["pin"] == "v0.3 -> 1234567"


def test_unresolved_reference_is_an_unknown_with_the_resolvers_next_step(proj):
    repo, st = proj
    res = analysis.analyze(st, repo, "How does our discount differ from "
                                     "https://github.com/psf/requests/blob/v2.31.0/src/requests/adapters.py?",
                           challenge=False)
    sq = next(s for s in res["subquestions"] if s["intent"] == "compare_reference")
    comp = next(r["resolution"] for r in sq["references"] if "resolution" in r)
    ref = comp["references"][0]
    assert ref["status"] == "unresolved" and ref["path"] == "src/requests/adapters.py"
    u = next(u for u in sq["unknowns"] if "unresolved" in u["why"])
    assert "network is off" in u["why"] and u["next_step"]
    assert not any(c for c in res["claims"] if "requests" in c["text"])  # nothing claimed about it


def test_an_analysis_carries_the_passages_query_gives(proj, flow):
    """Nothing `verinoda query` finds is lost by analyzing: its passages travel with the claims, line by line."""
    from verinoda import index, retrieval

    repo, _ = proj
    g = index.load(repo)
    want = retrieval.render_text(retrieval.retrieve(g, FLOW_Q, retrieval.Budget(max_items=10, max_chars=6000)), 6000)
    assert flow["passages"] == want.splitlines()
    assert any(ln.startswith("## ") for ln in flow["passages"])
    assert all("\n" not in ln for ln in flow["passages"])  # a list of lines: no escaped line breaks in JSON


def test_the_claims_that_answer_come_first(proj):
    """A reader sees the answer before what the search found on the way."""
    repo, st = proj
    res = analysis.analyze(st, repo, "Who calls place_order?")
    q1 = res["subquestions"][0]
    assert q1["answer_claim_ids"] and res["claims"][0]["id"] == q1["answer_claim_ids"][0]
    assert "create_order_handler" in res["claims"][0]["text"]  # the product's own caller, before the tests
    res = analysis.analyze(st, repo, "Where is an order written to the database?")
    assert "orders/repository.py:17" in res["claims"][0]["text"]  # the INSERT, not a settings loader
