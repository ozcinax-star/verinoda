"""Tests for repoatlas.benchmark: matching rules, token fallback, gold checks, a tiny end-to-end run.

Self-contained: the example project is copied to tmp_path and committed there;
nothing is scanned or written inside examples/. No network, no LLM.
"""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".repoatlas/index")

import copy  # noqa: E402
import json  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import types  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from repoatlas.benchmark import approaches as ap  # noqa: E402
from repoatlas.benchmark import llm as llmmod  # noqa: E402
from repoatlas.benchmark import metrics as mx  # noqa: E402
from repoatlas.benchmark import report  # noqa: E402
from repoatlas.benchmark.runner import builtin_sets, load_questions, run_benchmark  # noqa: E402

ORDERS = Path(__file__).resolve().parents[1] / "examples" / "orders_app"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


@pytest.fixture
def orders_copy(tmp_path: Path) -> Path:
    if shutil.which("git") is None:
        pytest.skip("git not available")
    dst = tmp_path / "orders_app"
    shutil.copytree(ORDERS, dst, ignore=shutil.ignore_patterns(".repoatlas", "graphify-out", "__pycache__"))
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
    monkeypatch.setenv("GRAPHIFY_OUT", ".repoatlas/index")
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
                        workdir=work)
    assert res["status"] == "ok", res.get("gold_validation")
    assert res["approaches"] == ["raw", "graphify_vendored", "repoatlas_analyze", "repoatlas_retrieve"]
    assert res["graphify_cli_status"].startswith("not run")
    assert res["token_count_method"] in ("chars/4 estimate", "tiktoken cl100k_base")
    assert res["index"]["repoatlas"]["cold_seconds"] > 0 and res["index"]["repoatlas"]["nodes"] > 10
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
    assert s["raw"]["facts_total"] == 6 and s["repoatlas_analyze"]["facts_found"] >= 3
    assert s["repoatlas_analyze"]["claims"]["weak_or_unknown_unlabelled"] == 0
    assert any("not measured" in n for n in res["not_measured"])
    # Results and delivered contexts written; source and workdir left clean.
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["summary"] == json.loads(json.dumps(s))
    assert (tmp_path / "results" / "raw" / "mini" / "q01" / "repoatlas_analyze.txt").exists()
    assert not (orders_copy / ".repoatlas").exists()
    status = subprocess.run(["git", "status", "--porcelain"], cwd=str(orders_copy), capture_output=True, text=True)
    assert status.stdout.strip() == ""
    assert list(work.iterdir()) == []  # only the run's own subdirectory was created, then removed
    report.render(res)
    printed = capsys.readouterr().out
    assert "facts found" in printed and "not measured" in printed
    assert "| RepoAtlas analyze |" in report.markdown(res)


def test_run_benchmark_refuses_unverifiable_gold(orders_copy, orders_questions, tmp_path):
    bad = copy.deepcopy(orders_questions)
    bad["questions"] = bad["questions"][:1]
    bad["questions"][0]["facts"][0]["source"]["at"] = "orders/repository.py:999"
    res = run_benchmark(orders_copy, questions=bad, repeat=1, workdir=tmp_path / "w2")
    assert res["status"] == "gold_invalid" and res["gold_validation"]["failures"]
    assert "questions" not in res
