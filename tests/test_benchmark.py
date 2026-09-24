"""Tests for verinoda.benchmark: matching rules, token fallback, gold checks, a tiny end-to-end run.

Self-contained: the example project is copied to tmp_path and committed there;
nothing is scanned or written inside examples/. No network, no LLM.
"""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import copy  # noqa: E402
import json  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import types  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda.benchmark import approaches as ap  # noqa: E402
from verinoda.benchmark import llm as llmmod  # noqa: E402
from verinoda.benchmark import metrics as mx  # noqa: E402
from verinoda.benchmark import report  # noqa: E402
from verinoda.benchmark.runner import builtin_sets, load_questions, run_benchmark  # noqa: E402

ORDERS = Path(__file__).resolve().parents[1] / "examples" / "orders_app"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


@pytest.fixture
def orders_copy(tmp_path: Path) -> Path:
    if shutil.which("git") is None:
        pytest.skip("git not available")
    dst = tmp_path / "orders_app"
    shutil.copytree(ORDERS, dst, ignore=shutil.ignore_patterns(".verinoda", "graphify-out", "__pycache__"))
    _git(dst, "init", "-q")
    _git(dst, "-c", "core.autocrlf=false", "add", "-A")
    _git(dst, "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", "commit", "-q", "-m", "init")
    return dst


@pytest.fixture
def orders_questions() -> dict:
    return json.loads(builtin_sets()["orders_app"].read_text(encoding="utf-8"))


# -- token counting ----------------------------------------------------------------

def test_token_count_falls_back_to_chars_over_4(monkeypatch):
    mx.reset_tokenizer()
    monkeypatch.setitem(sys.modules, "tiktoken", None)  # import tiktoken -> ImportError
    try:
        assert mx.token_method() == "chars/4 estimate"
        assert mx.count_tokens("") == 0
        assert mx.count_tokens("abcd") == 1
        assert mx.count_tokens("abcdefghi") == 3  # ceil(9 / 4)
    finally:
        mx.reset_tokenizer()


def test_token_count_uses_tiktoken_when_importable(monkeypatch):
    class _Enc:
        def encode(self, text, disallowed_special=()):
            return text.split()

    fake = types.SimpleNamespace(get_encoding=lambda name: _Enc() if name == "cl100k_base" else None)
    mx.reset_tokenizer()
    monkeypatch.setitem(sys.modules, "tiktoken", fake)
    try:
        assert mx.token_method() == "tiktoken cl100k_base"
        assert mx.count_tokens("one two three") == 3
    finally:
        mx.reset_tokenizer()


# -- locators and fact matching -------------------------------------------------------

def test_extract_locators_understands_every_output_format():
    text = "\n".join([
        "NODE .save() [src=orders/repository.py loc=L15 community=x]",
        "EDGE place_order() --calls [EXTRACTED context=call]--> compute_total() at=orders/service.py:L21",
        "supports:source_code:orders/api.py:18-20",
        '{"file":"orders/pricing.py","lines":[11,15],"score":1}',
        "==> orders/config.py:1-15 <==",
        "absolute C:/Users/me/x.py:12 is not a repo locator",
    ])
    got = {str(l) for l in mx.extract_locators(text)}
    assert got == {"orders/repository.py:15", "orders/service.py:21", "orders/api.py:18-20",
                   "orders/pricing.py:11-15", "orders/config.py:1-15"}


def test_fact_found_by_overlapping_locator_and_pinpoint_rule():
    fact = {"id": "f", "match_any": [{"loc": "orders/repository.py:15-20"}]}
    point = mx.match_fact(fact, "see orders/repository.py:17")
    assert point["found"] and point["pinpointed"]
    whole_file = mx.match_fact(fact, "==> orders/repository.py:1-80 <==")
    assert whole_file["found"] and not whole_file["pinpointed"]
    assert not mx.match_fact(fact, "orders/repository.py:21-26")["found"]
    assert not mx.match_fact(fact, "orders/service.py:15")["found"]


def test_fact_token_rules_are_literal_case_sensitive_and_word_bounded():
    fact = {"id": "f", "match_any": [{"text": "GRAPHIFY_QUERY_LOG"}]}
    assert not mx.match_fact(fact, 'os.environ.get("GRAPHIFY_QUERY_LOG_DISABLE")')["found"]
    assert mx.match_fact(fact, 'os.environ.get("GRAPHIFY_QUERY_LOG", "")')["found"]
    assert not mx.match_fact(fact, "graphify_query_log")["found"]
    assert mx.match_fact({"id": "g", "match_any": [{"text": "repo.save("}]}, "return repo.save(customer)")["found"]
    both = {"id": "h", "match_any": [{"all": ["create_order_handler", "place_order"]}]}
    assert mx.match_fact(both, "create_order_handler -> place_order")["found"]
    assert not mx.match_fact(both, "create_order_handler only")["found"]


def test_score_facts_reports_found_missed_and_how():
    facts = [{"id": "a", "match_any": [{"loc": "x.py:3"}]}, {"id": "b", "match_any": [{"text": "zzz"}]}]
    s = mx.score_facts(facts, "x.py:1-5")
    assert (s["found_n"], s["total"], s["found"], s["missed"]) == (1, 2, ["a"], ["b"])
    assert s["via"]["a"] == "loc x.py:1-5"


# -- negative facts and citation checks --------------------------------------------------

def test_negative_relations_match_graphify_edges_and_claims_by_direction():
    neg = {"id": "n", "statement": "handler calls save directly", "relation": ["create_order_handler", "calls", "save"]}
    wrong = mx.graphify_assertions("EDGE create_order_handler() --calls [INFERRED]--> .save() at=orders/api.py:L18")
    right = mx.graphify_assertions("EDGE create_order_handler() --calls [EXTRACTED]--> place_order() at=orders/api.py:L18")
    assert len(mx.match_negative(neg, wrong)) == 1
    assert mx.match_negative(neg, right) == []
    rev = {"id": "r", "statement": "reversed", "relation": ["apply_discount", "calls", "compute_total"]}
    fwd = mx.graphify_assertions("EDGE compute_total() --calls [EXTRACTED]--> apply_discount() at=orders/pricing.py:L8")
    assert mx.match_negative(rev, fwd) == []
    claims = [{"id": "c1", "status": "strong_inference",
               "text": "Data path create_order_handler() -> place_order() -> .save() reaches persistence (sql-write) at x"},
              {"id": "c2", "status": "statically_verified", "text": "`place_order()` calls `.save()` (orders/service.py:22)"}]
    ca = mx.claim_assertions(claims)
    assert mx.match_negative(neg, ca) == []  # the chain never states handler -> save
    assert mx.match_negative({"id": "p", "statement": "s", "relation": ["place_order", "calls", "save"]}, ca)
    rx = {"id": "x", "statement": "s", "assertion_regex": "discount[^\\n]*test_empty_order_rejected"}
    hit = mx.claim_assertions([{"id": "c3", "status": "strong_inference",
                                "text": "`apply_discount()` is exercised by tests: test_empty_order_rejected()"}])
    assert mx.match_negative(rx, hit) and mx.match_negative(rx, ca) == []


def test_check_citations_flags_lines_that_do_not_name_the_target(tmp_path):
    (tmp_path / "m.py").write_text("def a():\n    return b()\n\ndef b():\n    return 1\n", encoding="utf-8")
    good = mx.graphify_assertions("EDGE a() --calls [EXTRACTED]--> b() at=m.py:L2")
    bad = mx.graphify_assertions("EDGE a() --calls [INFERRED]--> b() at=m.py:L1")
    imports = mx.graphify_assertions("EDGE m.py --imports [EXTRACTED]--> os at=m.py:L1")
    assert mx.check_citations(tmp_path, good) == {"checked": 1, "cited_line_missing_target": 0,
                                                  "named_via_import_alias": 0, "examples": []}
    res = mx.check_citations(tmp_path, bad + imports)
    assert res["checked"] == 1 and res["cited_line_missing_target"] == 1  # imports are not checked


def test_check_citations_accepts_import_aliases(tmp_path):
    (tmp_path / "c.py").write_text(
        "def run():\n    from pkg.analyze import god_nodes as _god_nodes\n    return _god_nodes(1)\n", encoding="utf-8")
    edge = mx.graphify_assertions("EDGE run() --calls [EXTRACTED]--> god_nodes() at=c.py:L3")
    res = mx.check_citations(tmp_path, edge)
    assert res["cited_line_missing_target"] == 0 and res["named_via_import_alias"] == 1
    assert mx.import_aliases("import numpy as np\nfrom a.b import c as _c\n") == {"numpy": {"np"}, "c": {"_c"}}


# -- gold facts and question sets ---------------------------------------------------------

def test_builtin_question_sets_are_well_formed(orders_questions):
    sets = builtin_sets()
    assert {"orders_app", "graphify_core"} <= set(sets)
    for name, (lo, hi) in {"orders_app": (8, 12), "graphify_core": (6, 10)}.items():
        qs = json.loads(sets[name].read_text(encoding="utf-8"))
        assert lo <= len(qs["questions"]) <= hi
        ids = [f["id"] for q in qs["questions"] for f in q["facts"] + q.get("negatives", [])]
        assert len(ids) == len(set(ids))
        for q in qs["questions"]:
            assert q["facts"] and q["category"]
            for f in q["facts"]:
                assert f["source"]["at"] and f["source"]["contains"] and f["match_any"]
    cats = {q["category"] for q in orders_questions["questions"]}
    assert {"where", "flow", "config", "tests", "why", "impact"} <= cats


def test_orders_gold_facts_verify_against_the_source(orders_copy, orders_questions):
    res = mx.validate_gold(orders_copy, orders_questions["questions"])
    assert res["ok"], res["failures"]
    broken = copy.deepcopy(orders_questions["questions"][:1])
    broken[0]["facts"][0]["source"]["contains"] = "def not_there("
    broken[0]["facts"][1]["match_any"] = [{"text": "INVENTED_TOKEN_XYZ"}]
    bad = mx.validate_gold(orders_copy, broken, corpus_files=["orders/repository.py"])
    assert not bad["ok"] and len(bad["failures"]) == 2


def test_questions_auto_detected_for_the_example(orders_copy):
    data, path = load_questions(None, orders_copy)
    assert data["name"] == "orders_app" and path.endswith("orders_app.json")


# -- raw approach, CLI env, llm gating ----------------------------------------------------

def test_raw_context_is_deterministic_numbered_and_capped(orders_copy):
    q = "Where is an order written to the database?"
    t1, m1 = ap.raw_context(orders_copy, q)
    t2, _ = ap.raw_context(orders_copy, q)
    assert t1 == t2
    assert m1["files_read"][0]["file"] == "orders/repository.py"
    assert "==> orders/repository.py:1-26 <==" in t1 and "    17\t" in t1
    assert m1["agent_tool_calls"] == 1 + len(m1["files_read"])
    small, ms = ap.raw_context(orders_copy, q, char_cap=900)
    assert len(small) <= 900 and ms["truncated"]


def test_cli_env_uses_upstream_defaults_and_drops_secrets(monkeypatch):
    monkeypatch.setenv("GRAPHIFY_OUT", ".verinoda/index")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("SOME_AUTH_TOKEN", "y")
    env = ap.cli_env()
    assert "GRAPHIFY_OUT" not in env and "ANTHROPIC_API_KEY" not in env and "SOME_AUTH_TOKEN" not in env
    assert env["GRAPHIFY_QUERY_LOG_DISABLE"] == "1" and env["PYTHONIOENCODING"] == "utf-8"


def test_model_cost_is_not_measured_without_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert llmmod.status("none") == (False, llmmod.status("none")[1])
    on, why = llmmod.status("anthropic")
    assert not on and why.startswith("not measured")
    assert llmmod.cost_usd("claude-sonnet-5", 1_000_000, 1_000_000) == 12.0
    assert llmmod.cost_usd("unknown-model", 10, 10) is None


# -- end to end -----------------------------------------------------------------------

def test_run_benchmark_end_to_end_on_example_copy(orders_copy, orders_questions, tmp_path, capsys):
    subset = dict(orders_questions, questions=[q for q in orders_questions["questions"] if q["id"] in ("q01", "q07")])
    out = tmp_path / "results" / "mini.json"
    work = tmp_path / "work"
    res = run_benchmark(orders_copy, questions=subset, out=out, graphify_cmd=None, llm="none", repeat=1,
                        workdir=work, sweep=[750])
    assert res["status"] == "ok", res.get("gold_validation")
    assert res["schema"] == 2
    # The Graphify CLI is not given, so it is left out of the sweep too.
    assert res["approaches"] == ["raw", "graphify_vendored", "verinoda_analyze", "verinoda_retrieve",
                                 "verinoda_retrieve_text", "verinoda_retrieve_text@750", "graphify_vendored@750",
                                 "raw@750"]
    assert res["graphify_cli_status"].startswith("not run")
    assert res["token_count_method"] in ("chars/4 estimate", "tiktoken cl100k_base")
    assert res["index"]["verinoda"]["cold_seconds"] > 0 and res["index"]["verinoda"]["nodes"] > 10
    assert len(res["questions"]) == 2
    for q in res["questions"]:
        for a, slot in q["approaches"].items():
            assert "error" not in slot, slot.get("error")
            assert [r["kind"] for r in slot["runs"]] == ["cold"]
            sc = slot["score"]
            assert sc["chars"] > 0 and sc["tokens"] > 0 and sc["facts"]["total"] == 3
            assert slot["model"]["status"].startswith("not measured")
        assert q["approaches"]["raw"]["score"]["claims"].startswith("n/a (no claims)")
    s = res["summary"]
    assert s["raw"]["facts_total"] == 6 and s["verinoda_analyze"]["facts_found"] >= 3
    assert s["verinoda_analyze"]["claims"]["weak_or_unknown_unlabelled"] == 0
    # facts per 1k tokens is facts_found / tokens_total, for every approach
    for a, sa in s.items():
        assert sa["facts_per_1k_tokens"] == round(1000 * sa["facts_found"] / sa["tokens_total"], 2), a
    # the text approach delivers exactly what `verinoda query` prints, scored with its outline relations
    rt = res["questions"][0]["approaches"]["verinoda_retrieve_text"]["score"]
    assert rt["claims"].startswith("n/a (no claims)") and rt["retrieve_text"]["params"]["max_chars"] == 6000
    assert rt["chars"] <= 6000 and s["verinoda_retrieve_text"]["relations_cited_line_missing_target"] == 0
    # sweep: 750 tokens = a 3,000-character cap for every approach
    for a in ("raw@750", "graphify_vendored@750", "verinoda_retrieve_text@750"):
        assert s[a]["sweep"]["char_cap"] == 3000, a
        for q in res["questions"]:
            assert q["approaches"][a]["score"]["sweep"] == {"tokens": 750, "char_cap": 3000}
    assert all(q["approaches"]["raw@750"]["score"]["chars"] <= 3000 for q in res["questions"])
    assert all(q["approaches"]["verinoda_retrieve_text@750"]["score"]["chars"] <= 3000 for q in res["questions"])
    assert set(res["sweep"]) == {"verinoda_retrieve_text", "graphify_vendored", "raw"}
    assert res["sweep"]["raw"]["750"]["facts_total"] == 6
    assert res["params"]["sweep"]["tokens"] == [750]
    assert any("not measured" in n for n in res["not_measured"])
    # Results and delivered contexts written; source and workdir left clean.
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["summary"] == json.loads(json.dumps(s))
    assert (tmp_path / "results" / "raw" / "mini" / "q01" / "verinoda_analyze.txt").exists()
    assert not (orders_copy / ".verinoda").exists()
    status = subprocess.run(["git", "status", "--porcelain"], cwd=str(orders_copy), capture_output=True, text=True)
    assert status.stdout.strip() == ""
    assert list(work.iterdir()) == []  # only the run's own subdirectory was created, then removed
    report.render(res)
    printed = capsys.readouterr().out
    assert "facts found" in printed and "not measured" in printed and "budget sweep" in printed
    md = report.markdown(res)
    assert "| Verinoda analyze |" in md and "| Verinoda retrieve (text) |" in md and "Budget sweep" in md
    assert "@750" not in report.summary_table(res)  # sweep points only in the sweep table
    cmp = report.compare_table(saved, saved)
    assert "| Verinoda retrieve (text) |" in cmp and " -> " in cmp


def test_run_benchmark_refuses_unverifiable_gold(orders_copy, orders_questions, tmp_path):
    bad = copy.deepcopy(orders_questions)
    bad["questions"] = bad["questions"][:1]
    bad["questions"][0]["facts"][0]["source"]["at"] = "orders/repository.py:999"
    res = run_benchmark(orders_copy, questions=bad, repeat=1, workdir=tmp_path / "w2")
    assert res["status"] == "gold_invalid" and res["gold_validation"]["failures"]
    assert "questions" not in res


# -- round 3: text approach, budget sweep, pinned corpora, new sets -------------------------

ROOT = Path(__file__).resolve().parents[1]
HELDOUT_COMMIT = "73719901c9dea49c2e87120ca040c6b998298fe3"


def test_sweep_names_and_equal_character_caps():
    assert ap.split_approach("raw") == ("raw", None)
    assert ap.split_approach("graphify_cli@1500") == ("graphify_cli", 1500)
    for bad in ("verinoda_analyze@750", "raw@x", "raw@0", "nope@10"):
        with pytest.raises(ValueError):
            ap.split_approach(bad)
    assert [ap.sweep_chars(t) for t in ap.DEFAULT_SWEEP] == [3000, 6000, 12000]
    # Graphify counts 3 chars per token: these budgets give it the same character caps
    assert [ap.graphify_budget(t) for t in ap.DEFAULT_SWEEP] == [1000, 2000, 4000]
    assert all(ap.graphify_budget(t) * ap.GRAPHIFY_CHARS_PER_TOKEN >= ap.sweep_chars(t) for t in (1, 7, 750, 1001))
    assert mx.per_1k(3, 1500) == 2.0 and mx.per_1k(1, 0) is None


def test_graphify_cli_budget_is_passed_only_for_sweep_points(monkeypatch, tmp_path):
    seen = []

    def fake_run(argv, cwd, timeout=900):
        seen.append(argv)
        return types.SimpleNamespace(returncode=0, stdout="NODE a() [src=a.py loc=L1]\n", stderr=""), 0.01, None

    monkeypatch.setattr(ap, "_run", fake_run)
    ap.graphify_cli_context("graphify", tmp_path, "q?")
    text, meta = ap.graphify_cli_context("graphify", tmp_path, "q?", budget=1000)
    assert seen == [["graphify", "query", "q?"], ["graphify", "query", "q?", "--budget", "1000"]]
    assert meta["budget_tokens"] == 1000 and meta["nodes_shown"] == 1


def test_text_outline_assertions_read_calls_and_callers_only_from_outlines():
    text = "\n".join([
        "# where is it saved?",
        "## orders/service.py:19-22 def place_order(repo, customer, items) -> int",
        "  doc: Place an order.",
        "  calls: validate_items@20, compute_total@21, save@22? (+2)",
        "  called by: create_order_handler (orders/api.py:18); test_x (tests/t.py:9)?",
        "def place_order(repo, customer, items) -> int:",
        "  calls: not_an_outline@1",
        "## orders/api.py:9-13 def get_repo() -> OrderRepository",
        "  called by: create_order_handler (orders/api.py:18) <- main (app.py:3), cli (app.py:7) (+1); x (orders)",
        "## orders/pricing.py:6-8 def place_order(repo, customer, items) -> int",
        "  calls: validate_items@20",
        "## README.md:1-5 # orders_app",
        "orders/repository.py:15-20 def save(self, customer, total) -> int",
    ])
    got = [(a["src"], a["tgt"], a["at"], a["confidence"]) for a in mx.text_outline_assertions(text)]
    assert got == [
        ("place_order", "validate_items", "orders/service.py:20", "EXTRACTED"),
        ("place_order", "compute_total", "orders/service.py:21", "EXTRACTED"),
        ("place_order", "save", "orders/service.py:22", "INFERRED"),
        ("create_order_handler", "place_order", "orders/api.py:18", "EXTRACTED"),
        ("test_x", "place_order", "tests/t.py:9", "INFERRED"),
        ("create_order_handler", "get_repo", "orders/api.py:18", "EXTRACTED"),
        ("main", "create_order_handler", "app.py:3", "EXTRACTED"),
        ("cli", "create_order_handler", "app.py:7", "EXTRACTED"),
        ("x", "get_repo", None, "EXTRACTED"),
        ("place_order", "validate_items", "orders/pricing.py:20", "EXTRACTED"),
    ]
    # the assertions carry the same text shape as the JSON retrieve edges, so negatives match alike
    neg = {"id": "n", "statement": "s", "relation": ["create_order_handler", "calls", "save"]}
    assert mx.match_negative(neg, mx.text_outline_assertions(text)) == []
    hit = {"id": "m", "statement": "s", "relation": ["place_order", "calls", "save"]}
    assert len(mx.match_negative(hit, mx.text_outline_assertions(text))) == 1
    assert mx.text_outline_assertions("") == []
    # the same edge under two items is one assertion
    twice = "## a.py:1-3 def f()\n  calls: g@2\n## a.py:1-3 def f()\n  calls: g@2\n"
    assert len(mx.text_outline_assertions(twice)) == 1


def test_retrieve_text_approach_is_the_query_text_output(orders_copy):
    from verinoda import index, retrieval, workflow
    from verinoda.store import open_store

    workflow.init(orders_copy)
    st = open_store(orders_copy)
    try:
        workflow.scan(st, orders_copy)
    finally:
        st.close()
    q = "What is the call path from the create-order HTTP handler to the database write?"
    text, meta = ap.verinoda_retrieve_text(orders_copy, q)
    g = index.load(orders_copy)
    assert text == retrieval.render_text(retrieval.retrieve(g, q, retrieval.Budget(10, 6000)), 6000)
    assert len(text) <= 6000 and meta["params"] == {"max_items": 10, "max_chars": 6000, "render_chars": 6000}
    edges = mx.text_outline_assertions(text)
    assert meta["outline_edges"] == len(edges) > 0
    assert mx.check_citations(orders_copy, edges)["cited_line_missing_target"] == 0
    small, _ = ap.verinoda_retrieve_text(orders_copy, q, max_chars=ap.sweep_chars(750))
    assert len(small) <= 3000


def test_prepare_workdir_from_a_pinned_commit_ignores_the_working_tree(orders_copy, tmp_path):
    first = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(orders_copy), capture_output=True,
                           text=True).stdout.strip()
    (orders_copy / "orders" / "pricing.py").write_bytes(b"# changed after the pin\n")
    _git(orders_copy, "-c", "core.autocrlf=false", "add", "-A")
    _git(orders_copy, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "later")
    (orders_copy / "orders" / "config.py").write_bytes(b"# dirty working tree\n")
    info = ap.prepare_workdir(orders_copy, tmp_path / "pinned", include=["orders"], exclude=["*/api.py"],
                              commit=first[:7])
    assert info["source_commit"] == first and info["source_dirty"] is False
    assert info["snapshot"] == f"git archive {first}"
    assert "orders/api.py" not in info["files"] and "tests/test_pricing.py" not in info["files"]
    got = (tmp_path / "pinned" / "orders" / "pricing.py").read_bytes()
    assert got == subprocess.run(["git", "show", f"{first}:orders/pricing.py"], cwd=str(orders_copy),
                                 capture_output=True).stdout
    assert b"dirty" not in (tmp_path / "pinned" / "orders" / "config.py").read_bytes()
    assert (orders_copy / "orders" / "config.py").read_bytes() == b"# dirty working tree\n"  # source untouched
    with pytest.raises(ValueError, match="not found"):
        ap.prepare_workdir(orders_copy, tmp_path / "nope", commit="0" * 40)
    for bad in ("../x.py", "/etc/passwd", "C:/x.py", "a/../../b.py", ""):
        assert ap._safe_rel(bad) is None, bad
    assert ap._safe_rel("./a//b.py") == "a/b.py"


def test_every_builtin_set_records_who_wrote_it():
    sets = builtin_sets()
    assert {"orders_app", "graphify_core", "heldout_repoatlas", "orders_app_tr", "graphify_core_tr"} <= set(sets)
    for name, p in sets.items():
        raw = p.read_bytes()
        assert b"\r" not in raw, name
        pv = json.loads(raw.decode("utf-8"))["provenance"]
        assert isinstance(pv["in_sample"], bool) and pv["questions_written_by"] and pv["gold_written_by"], name
    assert json.loads(sets["heldout_repoatlas"].read_text(encoding="utf-8"))["provenance"]["in_sample"] is False
    for name in ("orders_app_tr", "graphify_core_tr"):
        assert json.loads(sets[name].read_text(encoding="utf-8"))["provenance"]["in_sample"] is True


def test_english_questions_and_gold_are_unchanged_since_round_2():
    # sha256 of the `questions` arrays as committed in 05890a1 / a42a57c; a change here must be deliberate
    import hashlib

    want = {"orders_app": "a9d32c9b56b4d02d4ccc79a8c11ee94656dacbac0cafc564da5c62af1dba1d8a",
            "graphify_core": "45c4fc6317de63270977f7ce54d294aa24448d4904a9d4861c145dcc6203cc22"}
    for name, h in want.items():
        qs = json.loads(builtin_sets()[name].read_text(encoding="utf-8"))["questions"]
        got = hashlib.sha256(json.dumps(qs, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        assert got == h, name


@pytest.mark.parametrize("name", ["orders_app", "graphify_core"])
def test_turkish_sets_share_the_english_gold(name):
    sets = builtin_sets()
    en = json.loads(sets[name].read_text(encoding="utf-8"))
    tr = json.loads(sets[f"{name}_tr"].read_text(encoding="utf-8"))
    assert "detect" not in tr["corpus"]  # auto-detection keeps picking the English set
    assert tr["corpus"] == {k: v for k, v in en["corpus"].items() if k != "detect"}
    assert [q["id"] for q in tr["questions"]] == [q["id"] for q in en["questions"]]
    for t, e in zip(tr["questions"], en["questions"]):
        assert t["facts"] == e["facts"] and t.get("negatives") == e.get("negatives")
        assert t["question_en"] == e["question"] != t["question"] and t["category"] == e["category"]
    assert tr["provenance"]["english_set"] == name


def test_heldout_set_is_reviewed_and_verifies_against_its_snapshot(tmp_path):
    """The held-out corpus is RepoAtlas (the pre-rename name) at commit 7371990, shipped as a snapshot."""
    qs = json.loads(builtin_sets()["heldout_repoatlas"].read_text(encoding="utf-8"))
    c = qs["corpus"]
    assert c["source_commit"] == HELDOUT_COMMIT and "detect" not in c and "git_commit" not in c
    facts = [f for q in qs["questions"] for f in q["facts"]]
    assert len(qs["questions"]) == 8 and len(facts) == 33
    assert {f["review"]["status"] for f in facts} <= {"unchanged", "reanchored", "restated"}
    assert all(f["review"]["original"]["at"] and f["review"]["note"] for f in facts)
    assert all("fact_original" in f for f in facts if f["review"]["status"] == "restated")
    snap = ROOT / c["snapshot_dir"]
    files = sorted(p.relative_to(snap).as_posix() for p in snap.rglob("*") if p.is_file())
    assert len(files) == 106 and not any(f.startswith("repoatlas/project_index/") for f in files)
    res = mx.validate_gold(snap, qs["questions"], corpus_files=files)
    assert res["ok"], res["failures"]


def test_harness_results_are_written_sanitised(tmp_path, monkeypatch, capsys):
    from verinoda.benchmark import critique_eval
    from verinoda.benchmark.sanitize import for_result, write_json

    home = str(Path.home())
    body = {"corpus": "upstream-graphify", "note": f"{home}{os.sep}x{os.sep}y.py", "tmp": str(tmp_path / "w")}
    assert for_result(body).text("plain") == "plain"  # a non-dict corpus is a name, not a path
    clean = write_json(tmp_path / "out" / "r.json", body, corpus=tmp_path / "corpus")
    raw = (tmp_path / "out" / "r.json").read_bytes()
    assert b"\r\n" not in raw and home.encode() not in raw and str(tmp_path).encode() not in raw
    assert clean["note"] == "<HOME>/x/y.py" and clean["tmp"].startswith("<TMP>")
    assert clean["paths_sanitized"]["placeholders"] == ["<TMP>", "<HOME>"]

    monkeypatch.setattr(critique_eval, "evaluate", lambda: {"benchmark_negatives": {"stated_as_verified": 0},
                                                            "where": str(tmp_path / "x")})
    out = tmp_path / "c.json"
    assert critique_eval.main(["--out", str(out)]) == 0
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["where"].startswith("<TMP>") and saved["environment"]["python"]
    assert "<TMP>" in capsys.readouterr().out


def test_sweep_only_runs_just_the_budget_points(orders_copy, orders_questions, tmp_path):
    subset = dict(orders_questions, questions=[orders_questions["questions"][0]])
    res = run_benchmark(orders_copy, questions=subset, repeat=1, workdir=tmp_path / "w", sweep=[1500, 750, 750],
                        sweep_only=True)
    assert res["status"] == "ok"
    assert res["approaches"] == ["verinoda_retrieve_text@750", "verinoda_retrieve_text@1500",
                                 "graphify_vendored@750", "graphify_vendored@1500", "raw@750", "raw@1500"]
    assert res["params"]["sweep_only"] is True and "verinoda_analyze" not in res["summary"]
    assert res["sweep"]["raw"]["1500"]["facts_total"] == 3
    assert res["questions"][0]["approaches"]["raw@1500"]["score"]["chars"] <= 6000
    assert "Budget sweep" in report.markdown(res)
    with pytest.raises(ValueError):
        run_benchmark(orders_copy, questions=subset, repeat=1, workdir=tmp_path / "w2", sweep_only=True)


def test_module_entry_passes_sweep_pin_and_compares(monkeypatch, tmp_path, capsys):
    import verinoda.benchmark as bench
    from verinoda.benchmark.__main__ import main

    seen: dict = {}

    def fake_run(repo, **kw):
        seen.update(kw, repo=repo)
        return {"status": "ok"}

    monkeypatch.setattr(bench, "run_benchmark", fake_run)
    assert main(["run", "--repo", str(tmp_path), "--sweep", "750, 3000", "--sweep-only", "--at", "abc123",
                 "--json"]) == 0
    assert seen["sweep"] == [750, 3000] and seen["sweep_only"] is True and seen["at"] == "abc123"
    assert main(["run", "--repo", str(tmp_path), "--json"]) == 0
    assert seen["sweep"] == [] and seen["sweep_only"] is False and seen["at"] is None
    with pytest.raises(SystemExit):
        main(["run", "--repo", str(tmp_path), "--sweep", "lots"])
    capsys.readouterr()
    old = {"approaches": ["raw"], "summary": {"raw": {"facts_found": 2, "facts_total": 4, "facts_pinpointed": 1,
                                                      "negatives_matched": 0, "negatives_checked": 0,
                                                      "assertions_scored": False, "tokens_mean": 500,
                                                      "tokens_total": 1000, "seconds_cold_median": 0.1,
                                                      "seconds_warm_median": 0.1}}}
    new = json.loads(json.dumps(old))
    new["approaches"] = ["raw", "verinoda_retrieve_text"]
    new["summary"]["raw"].update(facts_found=3, facts_per_1k_tokens=3.0)
    new["summary"]["verinoda_retrieve_text"] = dict(new["summary"]["raw"], assertions_scored=True)
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    a.write_text(json.dumps(old), encoding="utf-8")
    b.write_text(json.dumps(new), encoding="utf-8")
    assert main(["compare", str(a), str(b)]) == 0
    out = capsys.readouterr().out
    # schema-1 results have no facts_per_1k_tokens: it is computed from their totals
    assert "| raw grep+read | 2/4 -> 3/4 | 1 -> 1 | n/a -> n/a | 500 -> 500 | 2.00 -> 3.00 |" in out
    assert "| Verinoda retrieve (text) | n/a -> 3/4 |" in out


def test_a_final_answer_from_any_command_is_scored_for_facts_and_wrong_statements(orders_copy, orders_questions, tmp_path):
    """--answer-cmd: the prompt goes to the command's stdin; its answer is scored like an API model's."""
    import sys

    from verinoda.benchmark.runner import score_answer

    echo = tmp_path / "echo_answer.py"  # an "answerer" that repeats the context: every fact the context has
    echo.write_text("import sys\nprint(sys.stdin.read())\n", encoding="utf-8")
    subset = dict(orders_questions, questions=[q for q in orders_questions["questions"] if q["id"] == "q01"])
    res = run_benchmark(orders_copy, questions=subset, repeat=1, workdir=tmp_path / "w",
                        answer_cmd=f'"{sys.executable}" "{echo}"')
    assert res["status"] == "ok" and res["llm"]["enabled"]
    s = res["summary"]
    for a in ("verinoda_analyze", "verinoda_retrieve_text"):
        m = res["questions"][0]["approaches"][a]["model"]
        assert m["status"] == "measured" and m["answer_facts"]["found_n"] == \
            res["questions"][0]["approaches"][a]["score"]["facts"]["found_n"]
        assert s[a]["model"]["answers"] == 1 and "answers_correct" in s[a]["model"]
    v = s["verinoda_analyze"]["verdicts"]
    assert v["questions_met"] == v["met_backed_fully"] + v["met_backed_partly"] + v["met_unbacked"]
    q = {"facts": subset["questions"][0]["facts"],
         "negatives": [{"id": "n1", "statement": "it writes to a cache", "assertion_regex": "writes? (?:it )?to a cache"}]}
    wrong = score_answer(q, "The order goes to OrderRepository.save. It writes to a cache first.")
    assert wrong["answer_negatives"] == ["n1"] and wrong["answer_correct"] is False
