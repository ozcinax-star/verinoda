"""What a model reads of an analysis (verinoda.analysis_view): lean claims, the text and the MCP view."""

from __future__ import annotations

import json

from verinoda import analysis_view as av

PASSAGES = [
    "expanded: databas->db",
    "## orders/pricing.py:6-8 compute_total",
    "  calls: apply_discount@8",
    "def compute_total(items: list[dict]) -> float:",
    "    subtotal = sum(i['price'] * i['qty'] for i in items)",
    "    return apply_discount(subtotal)",
    "## orders/repository.py:8-26 OrderRepository",
    "  called by: get_repo (orders/api.py:12)",
    "  orders/repository.py:15-20",
    "def save(self, customer: str, total: float) -> int:",
    "## orders/api.py:16-21 create_order_handler",
    "## orders/api.py:24-28 get_order_handler",
    "orders/config.py:5-7 DATABASE_URL = os.environ.get('ORDERS_DATABASE_URL', 'orders.db')",
    "… 3 more candidates not shown: a.py:1-2 x",
    "next: same query, --max-chars 12000",
]


def _claim(cid: str, text: str, status: str = "statically_verified", **kw) -> dict:
    conf = {"statically_verified": 0.9, "strong_inference": 0.7, "primary_source_verified": 0.85}[status]
    return {"id": cid, "text": text, "status": status, "confidence": kw.pop("confidence", conf),
            "evidence": kw.pop("evidence", []), "uncertainties": kw.pop("uncertainties", []), **kw}


def _result(**over) -> dict:
    res = {
        "analysis_id": "ana_1", "question": "how is an order saved?", "intents": ["flow"], "plan_id": "qpl_1",
        "plan_source": "fallback", "understood_as": "Understood (rules): q1 [flow] how is an order saved?",
        "snapshot": {"id": "snp_1", "commit": "0123456789abcdef", "dirty": False}, "status": "answered",
        "plan_check": {"status": "ready", "errors": [], "warnings": [], "references": [], "clarifications": [],
                       "intent_divergence": [],
                       "links": [{"mention": "m1", "text": "order", "status": "linked", "at": "orders/service.py:19-22",
                                  "label": "place_order", "score": 1.0, "tier": "name"},
                                 {"mention": "m2", "text": "saved", "status": "weak", "at": "x.py:1-2"},
                                 {"mention": "m3", "text": "persist_order", "status": "not_found",
                                  "did_you_mean": ["place_order"]}]},
        "subquestions": [{"id": "q1", "intent": "flow", "text": "how is an order saved?",
                          "done_when": {"kind": "path_found"}, "links_used": ["m1"], "retrieval": {"items": 10},
                          "handler": "flow", "claim_ids": ["c1", "c2", "c3", "c4", "c5"], "unknowns": [],
                          "status": "met_with_inference", "answer_claim_ids": ["c1", "c2"]}],
        "claims": [
            _claim("c1", "`place_order()` calls `compute_total()` (orders/service.py:21)",
                   evidence=["supports:source_code:orders/service.py:21",
                             "supports:graph_edge:place_order -[calls]-> compute_total"]),
            _claim("c2", "`place_order()` -> `.save()` reaches the database", "strong_inference", confidence=0.6,
                   evidence=["supports:source_code:orders/service.py:22", "qualifies:graph_edge:x -[calls]-> y"],
                   uncertainties=["the receiver is not resolved"], challenged=False, not_challenged_reason="budget"),
            _claim("c3", "`compute_total()` calls `apply_discount()` (orders/pricing.py:8)"),  # printed: hidden
            _claim("c4", "`save()` is defined at orders/repository.py:15-20"),                 # printed: hidden
            _claim("c5", "`get_order_handler()` is defined at orders/api.py:24-28"),           # header only: kept
            _claim("c6", "`compute_total()` probably rounds (orders/pricing.py:7)", "strong_inference"),  # not verified
        ],
        "unknowns": [{"question": "which tests reach save?", "why": "no test reaches it", "next_step": "run the tests",
                      "sub_question": "q1"},
                     {"question": "does the index describe the tree?", "why": "refresh failed", "next_step": "scan"}],
        "critique": [], "steps": [{"step": "plan", "detail": "x", "t": 0.1}],
        "passages": PASSAGES,
        "usage": {"elapsed_s": 0.3, "tool_calls": 12, "exhausted": None},
    }
    res.update(over)
    return res


def test_lean_claim_keeps_what_the_text_does_not_say():
    c1, c2 = _result()["claims"][:2]
    assert av.lean_claim(c1) == {"id": "c1", "status": "statically_verified", "text": c1["text"]}
    assert av.lean_claim(c2) == {"id": "c2", "status": "strong_inference", "text": c2["text"], "confidence": 0.6,
                                 "evidence": ["source_code:orders/service.py:22",
                                              "qualifies:graph_edge:x -[calls]-> y"],
                                 "uncertainties": ["the receiver is not resolved"], "not_challenged": "budget"}


def test_printed_windows_counts_only_lines_the_passages_print():
    wins = av.printed_windows(PASSAGES)
    assert ("orders/pricing.py", 6, 8) in wins            # header, calls line, then the whole body
    assert ("orders/repository.py", 15, 20) in wins       # a sub-window
    assert ("orders/repository.py", 8, 26) not in wins   # its header is followed by a sub-window, not its body
    assert not any(w[0] == "orders/api.py" for w in wins)  # headers with no body (budget ran out)
    assert not any(w[0] == "orders/config.py" for w in wins)  # a one-line item prints no body


def test_verified_context_claims_the_passages_print_are_left_out_and_counted():
    res = _result()
    assert av.shown_by_passages(res) == {"c3", "c4"}
    # never an answer claim, never a claim that is not line-verified
    res["subquestions"][0]["answer_claim_ids"] = ["c1", "c3"]
    assert av.shown_by_passages(res) == {"c4"}
    assert av.shown_by_passages(_result(passages=[])) == set()


def test_text_is_the_answer_then_the_passages():
    text = av.render_text(_result())
    lines = text.splitlines()
    assert lines[0] == "analysis ana_1, snapshot snp_1 (commit 0123456789)"
    assert lines[1].startswith("understood as: Understood (rules)")
    assert "plan check: persist_order: not_found (did you mean place_order)" in text
    q1 = text.index("\nq1 [met_with_inference] flow: how is an order saved?")
    c1 = text.index("  [statically_verified] `place_order()` calls `compute_total()` (orders/service.py:21)  (c1)\n")
    c2 = text.index("  [strong_inference 0.60] `place_order()` -> `.save()` reaches the database "
                    "{source_code:orders/service.py:22; qualifies:graph_edge:x -[calls]-> y} "
                    "(uncertain: the receiver is not resolved)  (c2)  [not challenged: budget]")
    unk = text.index("  unknown: which tests reach save?: no test reaches it; next: run the tests")
    ctx = text.index("context (found on the way; not what answers):")
    assert q1 < c1 < c2 < unk < ctx
    assert "(c5)" in text and "(c6)" in text and "(c3)" not in text and "(c4)" not in text
    assert "+2 verified claim(s) about lines the passages below print" in text
    assert "\nunknown: does the index describe the tree?: refresh failed; next: scan" in text
    assert text.index("\npassages (") > ctx and text.endswith("next: same query, --max-chars 12000")
    for noise in ("steps", "usage", "tool_calls", "done_when", "links_used", "qpl_1", "0.90"):
        assert noise not in text, noise
    exhausted = av.render_text(_result(usage={"exhausted": "time budget 60.0s spent"}))
    assert "budget exhausted: time budget 60.0s spent" in exhausted


def test_text_of_a_plan_that_needs_clarification_or_is_invalid():
    clar = av.render_text(_result(status="needs_clarification", subquestions=[], claims=[], passages=[],
                                  clarifications=[{"id": "c-version", "question_en": "which version?",
                                                   "options": [{"value": "v1", "label": "the old one"}]}]))
    assert "clarification needed" in clar and "c-version: which version?" in clar and "- v1: the old one" in clar
    bad = av.render_text(_result(status="invalid_plan", subquestions=[], claims=[], passages=[],
                                 errors=[{"at": "/language", "msg": "unknown language", "fix": "use tr or en"}]))
    assert "the plan is invalid" in bad and "/language: unknown language  fix: use tr or en" in bad


def test_lean_is_the_same_content_as_json():
    res = _result(critique=[{"claim": "c2", "before": "statically_verified", "after": "strong_inference",
                             "fails": ["receiver not resolved"], "warns": []}])
    lean = av.lean(res)
    assert list(lean)[:4] == ["analysis_id", "status", "understood_as", "snapshot"]
    assert lean["snapshot"] == {"id": "snp_1", "commit": "0123456789abcdef"}
    assert lean["plan_check"] == {"status": "ready", "links": ["order -> orders/service.py:19-22 (linked)",
                                                              "persist_order: not_found (did you mean place_order)"]}
    assert lean["subquestions"] == [{"id": "q1", "intent": "flow", "text": "how is an order saved?",
                                     "status": "met_with_inference", "answer_claim_ids": ["c1", "c2"]}]
    assert [c["id"] for c in lean["claims"]] == ["c1", "c2", "c5", "c6"] and lean["claims_in_passages"] == 2
    assert lean["unknowns"] == res["unknowns"] and lean["critique"] == res["critique"]
    assert lean["passages"] == PASSAGES
    for gone in ("steps", "usage", "question", "intents", "plan_id", "plan_source", "budget_exhausted"):
        assert gone not in lean, gone
    assert av.lean(_result(usage={"exhausted": "tool-call budget 3 spent"}))["budget_exhausted"] == \
        "tool-call budget 3 spent"
    assert av.lean(_result(plan_source="host"))["plan_id"] == "qpl_1"
    assert len(json.dumps(lean)) < len(json.dumps(res))
