"""Analysis loop on a scanned copy of examples/orders_app: claims with file:line evidence,
INFERRED hops never statically verified, budgets produce unknowns, optional test runs,
decision records for "why" questions, and no invented claims when nothing matches.
"""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".repoatlas/index")

import json  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from repoatlas import analysis, workflow  # noqa: E402
from repoatlas import evidence as evmod  # noqa: E402
from repoatlas.claims import VERIFIED, Claims  # noqa: E402
from repoatlas.store import open_store  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "orders_app"
FLOW_Q = "How does an order get from the API handler to the database?"
LOCATOR = re.compile(r"^[\w./-]+\.\w+:\d+(-\d+)?$")

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


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
    assert "not_challenged_reason" not in capped["claims"][0]
    assert all(c["not_challenged_reason"] == "claim_limit" for c in capped["claims"][1:])
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
    from repoatlas import experiments
    from repoatlas import index as idx

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
                                "meta": {"outcome": "inconclusive", "inconclusive_reason": "pytest collected no tests"}})
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
                    "hint": "repoatlas scan --force", "stale": [], "changed_count": 1, "mode": "index_refused"}

        monkeypatch.setattr(workflow, "update", refused)
        res = analysis.analyze(st, repo, "Where is compute_total defined?")
        u = res["unknowns"][0]
        assert u["why"].startswith("index could not be refreshed: the indexer did not rewrite the graph")
        assert "repoatlas scan --force" in u["next_step"]
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

from repoatlas import question_plan as qp  # noqa: E402


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
    from repoatlas import critique

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
    from repoatlas import critique

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
    first = calls[0]  # the symbol the question names comes first
    assert "tests/test_pricing.py::test_compute_total" in first["test_ids"]
    assert first["targets"] == ["orders_pricing_compute_total"] and first["snapshot"]["id"]
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
    from repoatlas import lexicon

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
