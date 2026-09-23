"""Question plans (docs/DESIGN.md D1-D4, D8): the contract, the deterministic check, mention
linking in evidence tiers, clarifications, the rule-based draft, storage, and the measured
intent / segmentation / Turkish-retrieval behaviour on the benchmark questions."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import copy  # noqa: E402
import json  # noqa: E402
import shutil  # noqa: E402
import sqlite3  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import index, lexicon, workflow  # noqa: E402
from verinoda import question_plan as qp  # noqa: E402
from verinoda.store import open_store  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "orders_app"

HOST_PLAN = {
    "schema": "verinoda.question_plan/1",
    "user_message": "Sipariş API'den veritabanına nasıl ulaşıyor ve bunu hangi testler kapsıyor?",
    "language": "tr",
    "restated_goal": "Trace how an order request travels from the HTTP API layer to the database write, and list "
                     "the tests that exercise that path.",
    "restated_goal_user_lang": "Siparişin API katmanından veritabanı yazımına kadar izlediği yolu ve bu yolu "
                               "çalıştıran testleri bul.",
    "sub_questions": [
        {"id": "q1", "text": "What is the call path from the order API handler to the database write?",
         "intent": "flow", "mentions": ["m1", "m2", "m3"],
         "done_when": {"kind": "path_found", "subjects": ["m2", "m3"], "min_status": "strong_inference",
                       "detail": "a directed call path from an API handler to a persistence sink"}},
        {"id": "q2", "text": "Which tests statically reach the symbols on that path?", "intent": "tests",
         "subject_from": "q1", "depends_on": ["q1"],
         "done_when": {"kind": "set_enumerated", "subjects": [], "min_status": "strong_inference",
                       "detail": "tests reaching the path's symbols, or an explicit none-found"}},
    ],
    "mentions": [
        {"id": "m1", "text": "Sipariş", "span": [0, 7], "kind": "domain_concept", "gloss_en": "order",
         "candidates": ["Order", "place_order", "orders/service.py"]},
        {"id": "m2", "text": "API'den", "span": [8, 15], "kind": "module", "gloss_en": "from the API",
         "role": "source", "candidates": ["orders/api.py", "create_order_handler"]},
        {"id": "m3", "text": "veritabanına", "span": [16, 28], "kind": "domain_concept", "gloss_en": "to the database",
         "role": "target", "candidates": ["OrderRepository.save", "orders/repository.py"]},
    ],
    "constraints": {"include_tests": True},
    "assumptions": ["'bunu' in the second clause refers to the path found for q1"],
    "on_ambiguity": "ask",
    "host": "claude_code",
}


def plan(**changes) -> dict:
    p = copy.deepcopy(HOST_PLAN)
    p.update(changes)
    return p


def codes(problems) -> set[str]:
    return {p["code"] for p in problems}


# -- schema --------------------------------------------------------------------------------------

def test_schema_is_flat_packaged_and_matches_the_module():
    text = (ROOT / "verinoda" / "schemas" / "question_plan.v1.json").read_text(encoding="utf-8")
    assert "$ref" not in text and "anyOf" not in text and "oneOf" not in text
    s = qp.schema()
    assert s["$id"] == qp.SCHEMA_ID == s["properties"]["schema"]["const"]
    sq = s["properties"]["sub_questions"]["items"]["properties"]
    assert tuple(sq["intent"]["enum"]) == qp.INTENTS
    assert tuple(sq["done_when"]["properties"]["kind"]["enum"]) == qp.DONE_KINDS
    assert set(qp.DEFAULT_DONE) == set(qp.INTENTS) == set(qp.INTENT_DONE_OK)


def test_example_host_plan_is_valid():
    assert qp.validate(HOST_PLAN) == []
    p, probs = qp.parse(json.dumps(HOST_PLAN, ensure_ascii=False))
    assert probs == [] and p == HOST_PLAN


@pytest.mark.parametrize("mutate, at", [
    (lambda p: p.pop("restated_goal"), "/restated_goal"),
    (lambda p: p.update(language="de"), "/language"),
    (lambda p: p["sub_questions"][0].update(intent="guess"), "/sub_questions/0/intent"),
    (lambda p: p["sub_questions"][0].update(id="Q1"), "/sub_questions/0/id"),
    (lambda p: p["mentions"][0].update(extra=1), "/mentions/0/extra"),
    (lambda p: p.update(sub_questions=[]), "/sub_questions"),
    (lambda p: p["mentions"][0].update(candidates=["a", "b", "c", "d", "e", "f"]), "/mentions/0/candidates"),
])
def test_schema_problems_point_at_the_field(mutate, at):
    p = copy.deepcopy(HOST_PLAN)
    mutate(p)
    probs = qp.validate(p)
    assert probs and any(x["at"] == at for x in probs), probs
    assert all(x["fix"] for x in probs)


def test_own_validator_agrees_with_jsonschema():
    jsonschema = pytest.importorskip("jsonschema")
    v = jsonschema.Draft202012Validator(qp.schema())
    cases = [HOST_PLAN, plan(language="xx"), plan(sub_questions=[]), plan(extra=True),
             plan(mentions=[{"id": "m1", "text": "x"}])]
    for c in cases:
        assert (not list(v.iter_errors(c))) == (not qp.validate(c)), c


def test_parse_reports_bad_json_with_the_file_hint(tmp_path):
    p, probs = qp.parse("{not json")
    assert p is None and probs[0]["code"] == "schema" and ".verinoda/plans/" in probs[0]["fix"]
    f = tmp_path / "plan.json"
    f.write_bytes(json.dumps(HOST_PLAN, ensure_ascii=False).encode("utf-8"))
    p, probs = qp.parse(str(f))
    assert probs == [] and p["user_message"] == HOST_PLAN["user_message"]


def test_plan_hash_is_canonical():
    a = qp.plan_hash(HOST_PLAN)
    b = qp.plan_hash(json.loads(json.dumps(HOST_PLAN, sort_keys=False, indent=3)))
    assert a == b and a.startswith("sha256:")
    assert qp.plan_hash(plan(language="en")) != a


# -- integrity, grounding, versions (no graph needed) ---------------------------------------------

def test_integrity_duplicate_dangling_cycle_and_limits():
    p = copy.deepcopy(HOST_PLAN)
    p["mentions"].append(dict(p["mentions"][0]))  # duplicate m1
    p["sub_questions"][0]["mentions"].append("m9")
    p["sub_questions"][0]["depends_on"] = ["q2"]  # q1 <-> q2
    res = qp.check(p, None)
    assert {"duplicate_id", "dangling_id", "cycle"} <= codes(res["errors"])
    assert res["status"] == "invalid"
    many = plan(sub_questions=[dict(HOST_PLAN["sub_questions"][0], id=f"q{i}", mentions=[]) for i in range(1, 8)])
    assert "limit" in codes(qp.check(many, None)["errors"])


def test_topological_order_follows_depends_on_and_subject_from():
    res = qp.check(HOST_PLAN, None)
    assert res["topo_order"] == ["q1", "q2"] and res["errors"] == []


def test_mentions_must_occur_verbatim_in_the_message():
    p = plan()
    p["mentions"].append({"id": "m4", "text": "QueryTokenizer", "kind": "symbol"})
    res = qp.check(p, None)
    bad = [e for e in res["errors"] if e["code"] == "ungrounded_text"]
    assert bad and bad[0]["at"] == "/mentions/3/text" and "candidates" in bad[0]["fix"]
    # case, Turkish letters and apostrophe variants do not matter
    ok = plan()
    ok["mentions"][0]["text"] = "SİPARİŞ"
    ok["mentions"][1]["text"] = "api’den"
    assert "ungrounded_text" not in codes(qp.check(ok, None)["errors"])
    # a conversation mention is grounded in the quoted context instead
    conv = plan(conversation_context=["Önceki cevapta place_order fonksiyonundan bahsettin."])
    conv["mentions"].append({"id": "m4", "text": "place_order", "kind": "symbol", "source": "conversation"})
    assert "ungrounded_text" not in codes(qp.check(conv, None)["errors"])


def test_span_mismatch_is_a_warning_and_ignored():
    p = plan()
    p["mentions"][0]["span"] = [3, 9]
    res = qp.check(p, None)
    assert "span_mismatch" in codes(res["warnings"]) and not res["errors"]


def test_version_named_by_the_user_must_be_carried():
    msg = "Graphify v0.3'teki _query_terms ile bizimki farklı mı?"
    base = {"schema": qp.SCHEMA_ID, "user_message": msg, "language": "tr", "restated_goal": "Compare.",
            "sub_questions": [{"id": "q1", "text": "Compare", "intent": "compare_reference", "references": ["r1"],
                               "done_when": {"kind": "comparison_done", "subjects": ["r1"], "detail": "compared"}}],
            "references": [{"id": "r1", "text": "Graphify", "kind": "git_repo", "version": {"source": "unspecified"},
                            "purpose": "compare"}]}
    res = qp.check(base, None)
    dropped = [e for e in res["errors"] if e["code"] == "version_dropped"]
    assert dropped and "v0.3" in dropped[0]["msg"] and "never drop" in dropped[0]["fix"]
    fixed = copy.deepcopy(base)
    fixed["references"][0]["version"] = {"spec": "v0.3", "source": "user_explicit", "evidence": "v0.3"}
    assert "version_dropped" not in codes(qp.check(fixed, None)["errors"])


@pytest.mark.parametrize("msg, carried_by", [
    ("PR #42'deki değişiklik fiyatlamayı bozdu mu?", {"locator": "owner/repo#42"}),
    ("does commit 3f2a9c1d break pricing?", {"version": {"spec": "3f2a9c1d", "source": "user_explicit"}}),
    ("requests==2.31.0 ile ne değişti?", {"text": "requests==2.31.0"}),
])
def test_other_version_forms_are_checked(msg, carried_by):
    ref = {"id": "r1", "text": msg.split()[0] if "text" not in carried_by else carried_by["text"],
           "kind": "pull_request", "version": {"source": "unspecified"}}
    ref.update({k: v for k, v in carried_by.items() if k != "text"})
    doc = {"schema": qp.SCHEMA_ID, "user_message": msg, "language": "tr", "restated_goal": "x",
           "sub_questions": [{"id": "q1", "text": "x", "intent": "impact", "references": ["r1"],
                              "done_when": {"kind": "set_enumerated", "detail": "x"}}]}
    assert "version_dropped" in codes(qp.check({**doc, "references": []}, None)["errors"])
    assert "version_dropped" not in codes(qp.check({**doc, "references": [ref]}, None)["errors"])


def test_bare_decimal_without_a_version_word_is_only_a_warning():
    doc = {"schema": qp.SCHEMA_ID, "user_message": "Is the threshold 100.0 by default?", "language": "en",
           "restated_goal": "x", "sub_questions": [{"id": "q1", "text": "x", "intent": "config",
                                                   "done_when": {"kind": "claim_exists", "detail": "x"}}]}
    res = qp.check(doc, None)
    assert "version_dropped" in codes(res["warnings"]) and not res["errors"]


def test_relative_version_word_produces_a_version_clarification():
    doc = {"schema": qp.SCHEMA_ID, "user_message": "Eski sürümde indirim nasıl hesaplanıyordu?", "language": "tr",
           "restated_goal": "x", "sub_questions": [{"id": "q1", "text": "x", "intent": "flow",
                                                   "done_when": {"kind": "path_found", "detail": "x"}}]}
    res = qp.check(doc, None)
    assert "relative_version" in codes(res["warnings"])
    (c,) = res["clarifications"]
    assert c["kind"] == "version" and "sürüm" in c["question_user_lang"] and c["options"][-1]["value"] == "__other__"
    assert res["status"] == "needs_clarification"


def test_language_intent_and_done_when_cross_checks_are_warnings():
    p = plan(language="en")
    p["sub_questions"][1]["intent"] = "why"  # rules read tests/flow, the plan says why; done_when is set_enumerated
    res = qp.check(p, None)
    assert {"language", "intent_divergence", "intent_done_when_mismatch"} <= codes(res["warnings"])
    assert res["intent_divergence"][0] == {"sub_question": "q2", "plan": "why",
                                           "rules": res["intents_detected"]}
    assert not res["errors"]  # never overridden, never an error


# -- intents and segmentation (measured on the benchmark questions + Turkish paraphrases) ---------

# (question, gold intents per sub-question); gold written by the rule author - see the report.
GOLD = [
    ("Where is an order written to the database?", ["locate"]),
    ("What is the call path from the create-order HTTP handler to the database write?", ["flow"]),
    ("Which environment variables configure the orders service and where are they read?", ["config", "locate"]),
    ("Which tests exercise the discount calculation?", ["tests"]),
    ("Why does the project use SQLite for persistence?", ["why"]),
    ("What needs retesting if OrderRepository.save changes?", ["impact"]),
    ("Where is the 10% discount applied and what threshold controls it?", ["locate", "config"]),
    ("How does get_order_handler load an order from storage?", ["flow"]),
    ("Where is the SQLite connection opened and what decides the database file?", ["locate", "config"]),
    ("What happens when an order is submitted with no items?", ["behaviour"]),
    ("Where is graphify query output cut to fit the token budget?", ["locate"]),
    ("Which functions does the graphify query command call to turn a question into graph context?", ["flow"]),
    ("Which environment variables control graphify's query log?", ["config"]),
    ("Where does graphify store its AST extraction cache and how is a cache entry keyed?", ["locate", "locate"]),
    ("Which environment variable raises the maximum graph.json size, and what is the default limit?",
     ["config", "define"]),
    ("Why does graphify drop stopwords from query terms?", ["why"]),
    ("What is affected if _query_terms changes?", ["impact"]),
    ("How does graphify update rebuild the code graph without an LLM?", ["flow"]),
    ("Which tests cover the graph file size cap and its environment override?", ["tests"]),
    # Turkish paraphrases of the same questions
    ("Sipariş veritabanına nerede yazılıyor?", ["locate"]),
    ("Sipariş oluşturma API'sinden veritabanı yazımına kadar çağrı yolu nedir?", ["flow"]),
    ("Sipariş servisini hangi ortam değişkenleri yapılandırıyor ve bunlar nerede okunuyor?",
     ["config", "locate"]),
    ("İndirim hesaplamasını hangi testler çalıştırıyor?", ["tests"]),
    ("Proje kalıcılık için neden SQLite kullanıyor?", ["why"]),
    ("OrderRepository.save değişirse neleri yeniden test etmek gerekir?", ["impact"]),
    ("%10 indirim nerede uygulanıyor ve bunu hangi eşik kontrol ediyor?", ["locate", "config"]),
    ("get_order_handler bir siparişi depodan nasıl yüklüyor?", ["flow"]),
    ("SQLite bağlantısı nerede açılıyor ve veritabanı dosyasını ne belirliyor?", ["locate", "config"]),
    ("Kalemsiz bir sipariş gönderilince ne oluyor?", ["behaviour"]),
    ("Graphify sorgu çıktısı token bütçesine sığmak için nerede kesiliyor?", ["locate"]),
    ("Graphify'ın query log'unu hangi ortam değişkenleri kontrol ediyor?", ["config"]),
    ("Graphify sorgu terimlerinden neden stopword'leri atıyor?", ["why"]),
    ("_query_terms değişirse ne etkilenir?", ["impact"]),
    ("Kaydetme fonksiyonunu kim çağırıyor?", ["callers"]),
    ("Graphify v0.3'teki _query_terms ile bizimki farklı mı, farklıysa hangi testler bunu yakalar?",
     ["compare_reference", "tests"]),
    ("Sipariş API'den veritabanına nasıl ulaşıyor ve bunu hangi testler kapsıyor?", ["flow", "tests"]),
]


def _read(question: str) -> list[str]:
    return [(qp.clause_cues(c["text"]) or [{"intent": "locate"}])[0]["intent"] for c in qp.segment(question)]


def test_intent_and_segmentation_accuracy_on_the_gold_table():
    seg_ok = sum(len(_read(q)) == len(g) for q, g in GOLD)
    pairs = [(a, b) for q, g in GOLD if len(_read(q)) == len(g) for a, b in zip(_read(q), g)]
    intent_ok = sum(a == b for a, b in pairs)
    wrong = [(q, _read(q), g) for q, g in GOLD if _read(q) != g]
    assert not wrong, wrong
    assert seg_ok == len(GOLD) and intent_ok == sum(len(g) for _, g in GOLD)


def test_known_misreads_are_fixed():
    # "what decides the database file" is configuration, not a why question
    assert _read("What decides the database file?") == ["config"]
    # Turkish domain nouns do not trigger intents when the repository uses them (domain shadowing)
    class Lex:
        def has(self, w):
            return False

        def seed(self, words):
            return [{"targets": ["expense"]}] if words and words[0].startswith("gider") else []

    assert "flow" not in [c["intent"] for c in qp.clause_cues("Gider faturası nerede hesaplanıyor?", Lex())]
    assert "flow" in [c["intent"] for c in qp.clause_cues("Gider faturası nerede hesaplanıyor?")]
    # 'hanging' is not the Turkish question word 'hangi'; plain English is read with English cues only
    assert _read("Why is the build hanging?") == ["why"]


def test_segmentation_keeps_noun_phrases_and_marks_conditionals():
    assert len(qp.segment("sipariş ve fatura nerede?")) == 1
    assert len(qp.segment("Siparişi kaydeden ve faturayı gönderen fonksiyon nerede?")) == 1
    assert len(qp.segment("SQLite bağlantısı nerede açılıyor ve veritabanı dosyasını ne belirliyor?")) == 2
    clauses = qp.segment("Graphify v0.3'teki _query_terms ile bizimki farklı mı, farklıysa hangi testler bunu "
                         "yakalar?")
    assert len(clauses) == 2 and clauses[1]["conditional"] and not clauses[0]["conditional"]
    msg = "Where is X? Which tests cover it?"
    for c in qp.segment(msg):
        assert msg[c["start"]:c["end"]] == c["text"]


# -- linking against a real graph ------------------------------------------------------------------

def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args],
                   cwd=cwd, check=True, capture_output=True)


def _scan_copy(dst: Path, src: Path | None = None, files: dict[str, str] | None = None) -> Path:
    if src is not None:
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns(".verinoda", "__pycache__", "*.pyc", "*.db"))
    for rel, text in (files or {}).items():
        p = dst / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode("utf-8"))
    _git(dst, "init", "-q")
    _git(dst, "add", "-A")
    _git(dst, "commit", "-q", "-m", "init")
    workflow.init(dst)
    st = open_store(dst)
    workflow.scan(st, dst)
    st.close()
    return dst


@pytest.fixture(scope="module")
def orders(tmp_path_factory):
    if shutil.which("git") is None:
        pytest.skip("git not available")
    repo = _scan_copy(tmp_path_factory.mktemp("qp") / "orders_app", EXAMPLE)
    g = index.load(repo)
    lex = lexicon.load(repo)
    if lex is None:
        lexicon.build(repo, g)
        lex = lexicon.load(repo)
    return repo, g, lex


def _link(orders, text, **m):
    repo, g, lex = orders
    return qp.link_mention({"id": "m1", "text": text, "kind": m.pop("kind", "symbol"), **m}, g, lex)


@pytest.mark.parametrize("text, extra, tier, label", [
    ("orders_repository_orderrepository_save", {}, "exact_id", ".save()"),
    ("orders/pricing.py", {"kind": "file"}, "path", "pricing.py"),
    ("pricing.py", {"kind": "file"}, "path", "pricing.py"),
    ("OrderRepository.save", {}, "qualified", ".save()"),
    ("compute_total", {}, "exact_label", "compute_total()"),
    ("computeTotal", {}, "label_folded", "compute_total()"),
    ("compute_totl", {}, "fuzzy", "compute_total()"),
])
def test_link_tiers(orders, text, extra, tier, label):
    lk = _link(orders, text, **extra)
    assert lk["best"]["label"] == label and lk["best"]["matches"][0]["type"] == tier, lk
    assert lk["best"]["score"] == qp.TIER_SCORES[tier]
    assert lk["best"]["at"].count(":") == 1 and lk["best"]["matches"][0]["site"]


def test_identifier_parts_seed_and_text_tiers(orders):
    lk = _link(orders, "discount", kind="domain_concept")
    assert lk["tier"] == "identifier_parts" and lk["best"]["label"] == "apply_discount()"
    tr = _link(orders, "İndirimi", kind="domain_concept")  # Turkish, through the seed dictionary only
    assert tr["best"]["label"] == "apply_discount()" and tr["best"]["score"] == qp.TIER_SCORES["seed_dictionary"]
    assert tr["status"] == "weak" and "seed_dictionary" in tr["uncertainty"]
    db = _link(orders, "database", kind="domain_concept")  # only DATABASE_URL uses the word
    assert db["tier"] == "text_hit" and db["status"] == "weak"
    assert db["best"]["matches"][0]["site_hash"].startswith("sha256:")


def test_host_candidates_that_match_nothing_are_rejected_and_never_seed(orders):
    repo, g, lex = orders
    p = plan()
    p["mentions"][0]["candidates"] = ["QueryTokenizer", "place_order"]
    res = qp.check(p, g, repo, lex)
    lk = next(x for x in res["links"] if x["mention"] == "m1")
    assert {"candidate": "QueryTokenizer", "why": "no node, file or label in the graph matches it"} in \
        lk["rejected_candidates"]
    seeds = qp.retrieval_inputs(p, res, p["sub_questions"][0], lex)["seeds"]
    assert all("QueryTokenizer" not in n and "querytokenizer" not in n for n in seeds)


def test_host_plan_links_every_mention_and_is_ready(orders):
    repo, g, lex = orders
    res = qp.check(HOST_PLAN, g, repo, lex)
    assert res["status"] == "ready" and not res["errors"], res
    links = {lk["mention"]: lk for lk in res["links"]}
    assert links["m2"]["status"] == "linked" and links["m2"]["best"]["at"].startswith("orders/api.py")
    assert links["m3"]["status"] == "linked" and links["m3"]["best"]["label"] == ".save()"
    inputs = qp.retrieval_inputs(HOST_PLAN, res, HOST_PLAN["sub_questions"][0], lex)
    assert "orders_repository_orderrepository_save" in inputs["seeds"]  # a candidate the host named
    assert inputs["expansions"].get("siparis") == ["order"]


def test_unlinked_required_mention_is_an_unknown_with_a_next_step(orders):
    repo, g, lex = orders
    doc = {"schema": qp.SCHEMA_ID, "user_message": "Where is the FluxCapacitor configured?", "language": "en",
           "restated_goal": "x", "sub_questions": [{"id": "q1", "text": "x", "intent": "config", "mentions": ["m1"],
                                                   "done_when": {"kind": "claim_exists", "detail": "x"}}],
           "mentions": [{"id": "m1", "text": "FluxCapacitor", "kind": "symbol"}]}
    res = qp.check(doc, g, repo, lex)
    assert res["links"][0]["status"] == "unlinked"
    (u,) = res["unknowns"]
    assert u["about"] == "m1" and "FluxCapacitor" in u["why"] and u["next_step"]


def test_near_miss_asks_did_you_mean_with_grounded_options(orders):
    repo, g, lex = orders
    doc = {"schema": qp.SCHEMA_ID, "user_message": "Who calls compute_totall?", "language": "en",
           "restated_goal": "x", "sub_questions": [{"id": "q1", "text": "x", "intent": "callers", "mentions": ["m1"],
                                                   "done_when": {"kind": "set_enumerated", "detail": "x"}}],
           "mentions": [{"id": "m1", "text": "compute_totall", "kind": "symbol"}]}
    lk = qp.link_mention(doc["mentions"][0], g, lex)
    if lk["status"] != "unlinked":  # the fuzzy tier already links typos this close
        assert lk["best"]["label"] == "compute_total()"
        return
    res = qp.check(doc, g, repo, lex)
    (c,) = res["clarifications"]
    assert c["kind"] == "did_you_mean" and c["options"][0]["label"].startswith("compute_total()")


AMBIG = {
    "billing/invoices.py": "class InvoiceStore:\n    def save(self, invoice):\n        return invoice\n",
    "orders/store.py": "class OrderStore:\n    def save(self, order):\n        return order\n",
    "orders/twins.py": "class A:\n    def flush(self):\n        return 1\n\n\nclass B:\n    def flush(self):\n"
                       "        return 2\n",
    "tests/test_store.py": "from orders.store import OrderStore\n\n\ndef test_save():\n    OrderStore().save(1)\n",
}


@pytest.fixture(scope="module")
def ambiguous(tmp_path_factory):
    if shutil.which("git") is None:
        pytest.skip("git not available")
    repo = _scan_copy(tmp_path_factory.mktemp("qp_amb") / "amb", files=AMBIG)
    return repo, index.load(repo)


def _one_mention_plan(msg: str, text: str, intent: str = "locate", **extra) -> dict:
    kind, _, _ = qp.DEFAULT_DONE[intent]
    return {"schema": qp.SCHEMA_ID, "user_message": msg, "language": "tr" if "nerede" in msg else "en",
            "restated_goal": "x", "sub_questions": [{"id": "q1", "text": msg, "intent": intent, "mentions": ["m1"],
                                                    "done_when": {"kind": kind, "subjects": ["m1"], "detail": "x"}}],
            "mentions": [{"id": "m1", "text": text, "kind": "symbol"}], "on_ambiguity": "ask", **extra}


def test_ambiguous_entity_asks_one_grounded_question(ambiguous):
    repo, g = ambiguous
    res = qp.check(_one_mention_plan("save nerede tanımlı?", "save"), g, repo)
    assert res["status"] == "needs_clarification"
    (c,) = res["clarifications"]
    assert c["id"] == "c-m1" and c["kind"] == "entity" and c["blocks"] == ["q1"]
    assert c["question_user_lang"] == "'save' ile hangisini kastediyorsunuz?"
    values = [o["value"] for o in c["options"]]
    assert values[-2:] == ["__all__", "__other__"]
    grounded = values[:-2]
    assert len(grounded) == 2 and all(v in g.G for v in grounded)
    assert "different files" in c["why"]


def test_same_file_candidates_merge_silently(ambiguous):
    repo, g = ambiguous
    res = qp.check(_one_mention_plan("Where is flush defined?", "flush"), g, repo)
    lk = res["links"][0]
    # A.flush and B.flush share a file and a name: one family, no question
    assert res["status"] == "ready" and lk["status"] == "linked" and not res["clarifications"]
    assert lk["best"]["at"].startswith("orders/twins.py") and lk["alternatives"] == []


def test_answer_resolves_the_clarification(ambiguous):
    repo, g = ambiguous
    first = qp.check(_one_mention_plan("save nerede tanımlı?", "save"), g, repo)
    choice = first["clarifications"][0]["options"][0]["value"]
    answered = _one_mention_plan("save nerede tanımlı?", "save",
                                 answers=[{"clarification_id": "c-m1", "choice": choice, "answered_by": "user"}])
    res = qp.check(answered, g, repo)
    assert res["status"] == "ready" and res["links"][0]["nodes"] == [choice]
    assert res["links"][0]["answered"]["answered_by"] == "user"


def test_domain_concepts_are_merged_not_asked(ambiguous):
    repo, g = ambiguous
    p = _one_mention_plan("Where is the save logic?", "save")
    p["mentions"][0]["kind"] = "domain_concept"
    res = qp.check(p, g, repo)
    assert res["status"] == "ready" and res["links"][0]["merge_reason"].startswith("domain concept")


def test_probe_merges_when_the_same_tests_reach_both(ambiguous):
    repo, g = ambiguous
    res = qp.check(_one_mention_plan("Which tests reach flush?", "flush", intent="tests"), g, repo)
    assert res["status"] == "ready"


# -- draft ----------------------------------------------------------------------------------------------

def test_draft_builds_a_valid_tagged_plan_with_roles_and_anaphora(orders):
    repo, g, lex = orders
    msg = HOST_PLAN["user_message"]
    p = qp.draft(msg, g, lex)
    assert qp.validate(p) == [] and p["derived_by"] == qp.DRAFT_RULES and p["language"] == "tr"
    q1, q2 = p["sub_questions"]
    assert q1["intent"] == "flow" and q2["intent"] == "tests" and q2["subject_from"] == "q1"
    assert all(sq["derived_by"].startswith(qp.DRAFT_RULES) for sq in p["sub_questions"])
    ms = {m["text"]: m for m in p["mentions"]}
    assert ms["API'den"]["role"] == "source" and ms["veritabanına"]["role"] == "target"
    assert ms["Sipariş"]["gloss_en"] == "order" and all(m["derived_by"].startswith(qp.DRAFT_RULES)
                                                        for m in ms.values())
    for m in p["mentions"]:
        assert msg[m["span"][0]:m["span"][1]] == m["text"]
    res = qp.check(p, g, repo, lex, source="fallback")
    assert res["status"] == "ready" and not res["errors"]
    assert q1["done_when"]["subjects"][0] == ms["API'den"]["id"]
    assert q1["done_when"]["subjects"][-1] == ms["veritabanına"]["id"]
    assert p["restated_goal_user_lang"].startswith("Anladığım")


def test_draft_carries_versions_into_references(orders):
    repo, g, lex = orders
    msg = "Graphify v0.3'teki _query_terms ile bizimki farklı mı, farklıysa hangi testler bunu yakalar?"
    p = qp.draft(msg, g, lex)
    (r,) = p["references"]
    assert r["text"] == "Graphify v0.3" and r["version"] == {"spec": "v0.3", "source": "user_explicit",
                                                             "evidence": "v0.3"}
    res = qp.check(p, g, repo, lex)
    assert "version_dropped" not in codes(res["errors"] + res["warnings"])
    assert res["references"][0]["status"] == "pending_research"
    code = next(m for m in p["mentions"] if m["text"] == "_query_terms")
    assert code["kind"] == "symbol" and code["required"] is True
    assert any(u["about"] == code["id"] for u in res["unknowns"])  # not in orders_app: unknown + next step
    assert p["sub_questions"][1]["depends_on"] == ["q1"]


def test_draft_relative_version_becomes_a_user_relative_reference(orders):
    repo, g, lex = orders
    p = qp.draft("Önceki sürümde indirim nerede uygulanıyordu?", g, lex)
    (r,) = p["references"]
    assert r["version"]["source"] == "user_relative"
    res = qp.check(p, g, repo, lex)
    assert any(c["kind"] == "version" for c in res["clarifications"])


def test_draft_keeps_only_grounded_content_words_and_never_seeds_them(orders):
    repo, g, lex = orders
    p = qp.draft("Where is the query order written?", g, lex)
    texts = {m["text"] for m in p["mentions"]}
    assert "order" in texts and "written" not in texts  # 'written' matches nothing in the repository
    res = qp.check(p, g, repo, lex)
    assert qp.retrieval_inputs(p, res, p["sub_questions"][0], lex)["seeds"] == {}


def test_draft_off_topic_question_has_no_mentions(orders):
    repo, g, lex = orders
    p = qp.draft("What does the flux capacitor quantize?", g, lex)
    assert p["mentions"] == [] and p["sub_questions"][0]["intent"] == "locate"


def test_retrieval_inputs_expand_only_words_the_repository_lacks(orders):
    repo, g, lex = orders
    p = qp.draft("İndirim nerede uygulanıyor?", g, lex)
    res = qp.check(p, g, repo, lex)
    exp = qp.retrieval_inputs(p, res, p["sub_questions"][0], lex)["expansions"]
    assert exp.get("indirim") == ["discount"] and exp.get("uygulaniyor") == ["apply"]
    en = qp.draft("Where is the discount applied?", g, lex)
    assert qp.retrieval_inputs(en, qp.check(en, g, repo, lex), en["sub_questions"][0], lex)["expansions"] == {}


TR_GOLD = [  # Turkish paraphrase -> gold (measured: 0/4 before this track, see the report)
    ("Sipariş veritabanına nerede yazılıyor?", lambda it: it["id"] == "orders_repository_orderrepository_save"),
    ("Toplam tutar nerede hesaplanıyor?", lambda it: it["id"] == "orders_pricing_compute_total"),
    ("İndirim nerede uygulanıyor?", lambda it: it["id"] == "orders_pricing_apply_discount"),
    ("Ortam değişkenleri nerede okunuyor?", lambda it: it["file"] == "orders/config.py"),
]


def test_turkish_paraphrases_reach_the_gold_symbol(orders):
    from verinoda import analysis

    repo, g, lex = orders

    class Ctx:
        pass

    ctx = Ctx()
    ctx.g = g
    ranks = []
    for question, gold in TR_GOLD:
        p = qp.draft(question, g, lex)
        res = qp.check(p, g, repo, lex)
        inputs = qp.retrieval_inputs(p, res, p["sub_questions"][0], lex)
        items = analysis._retrieve(ctx, inputs, True)["items"]
        ranks.append(next((i for i, it in enumerate(items, 1) if gold(it)), None))
    assert all(r is not None and r <= 4 for r in ranks), ranks


# -- storage ----------------------------------------------------------------------------------------

def test_store_plan_is_immutable_and_revisable(orders):
    repo, g, lex = orders
    st = open_store(repo)
    try:
        res = qp.check(HOST_PLAN, g, repo, lex)
        pid = qp.store_plan(st, HOST_PLAN, res, "host")
        row = qp.get_plan(st, pid)
        assert row["plan"] == HOST_PLAN and row["plan_hash"] == qp.plan_hash(HOST_PLAN)
        assert row["status"] == "ready" and row["check_result"]["status"] == "ready"
        with pytest.raises(sqlite3.DatabaseError):
            st.update("question_plans", pid, {"plan": {"changed": True}})
        with pytest.raises(sqlite3.DatabaseError):
            st.conn.execute("DELETE FROM question_plans WHERE id = ?", (pid,))
        st.update("question_plans", pid, {"analysis_id": "ana_x"})  # analysis_id/status stay updatable
        rev = qp.store_plan(st, plan(language="tr"), res, "revised", parent_id=pid)
        assert qp.get_plan(st, rev)["parent_id"] == pid
        with pytest.raises(ValueError):
            qp.store_plan(st, HOST_PLAN, res, "llm")
    finally:
        st.close()


def test_compact_check_is_small(orders):
    repo, g, lex = orders
    res = qp.check(HOST_PLAN, g, repo, lex)
    small = qp.compact_check(res)
    assert set(small) == {"status", "errors", "warnings", "links", "references", "clarifications",
                          "intent_divergence"}
    assert len(json.dumps(small, ensure_ascii=False)) < len(json.dumps(res, ensure_ascii=False))
    assert all({"mention", "text", "status"} <= set(lk) for lk in small["links"])
