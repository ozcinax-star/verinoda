"""Claims: status rules, relevance grades, evidence groups, refutation strength, ceilings, history."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".repoatlas/index")

from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from repoatlas import evidence as evmod  # noqa: E402
from repoatlas.claims import (  # noqa: E402
    CONFIDENCE_CAP,
    ORDER,
    VERIFIED,
    ClaimRuleError,
    Claims,
    best_status,
    check_status,
    claim_key,
    step_down,
)
from repoatlas.memory import Memory  # noqa: E402
from repoatlas.store import Store  # noqa: E402

CODE = "def total(items):\n    s = sum(items)\n    return s\n\n\ndef helper():\n    return 1\n"
DOC = "# ADR 7\n\nStatus: accepted\n\nWe sum items in total().\n"
CONFIG = 'import os\n\nDATABASE_URL = os.environ.get("ORDERS_DATABASE_URL", "orders.db")\n'
TEXT = "total() sums items"  # what pkg/calc.py:1-3 states


@pytest.fixture
def env(tmp_path):
    repo = tmp_path / "proj"
    (repo / "pkg").mkdir(parents=True)
    (repo / "docs").mkdir()
    (repo / "pkg" / "calc.py").write_bytes(CODE.encode())
    (repo / "pkg" / "config.py").write_bytes(CONFIG.encode())
    (repo / "docs" / "adr.md").write_bytes(DOC.encode())
    st = Store(tmp_path / "atlas.db")
    yield repo, st, Claims(st, repo)
    st.close()


def src(repo: Path, a=1, b=3, **kw) -> dict:
    return evmod.source_evidence(repo, "pkg/calc.py", a, b, commit="c0ffee", **kw)


def doc(repo: Path) -> dict:
    return evmod.source_evidence(repo, "docs/adr.md", 1, 5, commit="c0ffee", source_type="design_doc")


def edge(conf: str) -> dict:
    return evmod.graph_edge_evidence({"source": "calc_total", "target": "calc_helper", "relation": "calls",
                                      "confidence": conf, "source_file": "pkg/calc.py", "source_location": "L2"},
                                     graph_path="g.json", commit="c0ffee")


EDGE_CLAIM = {"text": "total() calls helper()", "kind": "relation",
              "spec": {"source": "calc_total", "target": "calc_helper", "target_label": "helper()"}}


def run_result(outcome: str) -> dict:
    return {"source_type": "test_result", "locator": "run exp_x: pytest tests/test_total.py",
            "excerpt": "1 passed" if outcome == "pass" else "1 failed", "content_hash": "sha256:0",
            "meta": {"outcome": outcome, "experiment_id": "exp_x"}}


RUN_TEXT = "the total tests pass"


def make(cl: Claims, status: str, evidence=(), **kw) -> dict:
    return cl.create(kw.pop("text", TEXT), project="proj", snapshot=None, status=status,
                     evidence=list(evidence), **kw)


# -- the core rule: no evidence, no verification --------------------------------------

@pytest.mark.parametrize("status", sorted(VERIFIED))
def test_claim_without_evidence_is_never_verified_and_is_unknown(env, status):
    repo, st, cl = env
    c = make(cl, status)
    assert c["status"] == "unknown" and c["confidence"] == 0.0  # not weak_inference 0.4
    last = cl.show(c["id"])["history"][-1]
    assert "downgraded" in last["reason"] and "requires" in last["reason"]
    with pytest.raises(ClaimRuleError, match="requires"):
        cl.set_status(c["id"], status, reason="force it", downgrade=False)
    assert cl.get(c["id"])["status"] == "unknown"


def test_weak_inference_needs_some_support(env):
    repo, st, cl = env
    c = make(cl, "weak_inference")
    assert c["status"] == "unknown"
    ptr = {"source_type": "search_result", "locator": "https://search.example/?q=total", "content_hash": "sha256:2"}
    assert make(cl, "weak_inference", [(ptr, "supports")])["status"] == "weak_inference"


@pytest.mark.parametrize("conf", ["EXTRACTED", "INFERRED"])
@pytest.mark.parametrize("status", sorted(VERIFIED))
def test_graph_edge_only_support_is_never_verified(env, conf, status):
    repo, st, cl = env
    c = make(cl, status, [(edge(conf), "supports")], **EDGE_CLAIM)
    assert c["status"] == "strong_inference"  # an edge is inference, not verification
    with pytest.raises(ClaimRuleError, match="only: graph_edge"):
        cl.set_status(c["id"], status, reason="edge says so", downgrade=False)


def test_statically_verified_needs_source_lines_that_still_match(env):
    repo, st, cl = env
    c = make(cl, "statically_verified", [(src(repo), "supports")])
    assert (c["status"], c["confidence"]) == ("statically_verified", CONFIDENCE_CAP["statically_verified"])
    (repo / "pkg" / "calc.py").write_bytes(CODE.replace("sum(items)", "sum(items) + 1").encode())
    with pytest.raises(ClaimRuleError, match="no longer match"):
        cl.set_status(c["id"], "statically_verified", reason="again", downgrade=False)
    # A cited block that no longer matches is not support for any verified status
    # (it must not fall back to 'observed' with the same confidence).
    after = cl.reassess(c["id"], reason="re-check")
    assert after["status"] not in VERIFIED and after["confidence"] < CONFIDENCE_CAP["statically_verified"]
    # Without a repo (and a claim to grade against) this is a pure evidence-type query.
    assert check_status("statically_verified", cl.evidence(c["id"])) is None


def test_anchored_source_that_only_moved_stays_verified(env):
    repo, st, cl = env
    c = make(cl, "statically_verified", [(src(repo), "supports")])
    (repo / "pkg" / "calc.py").write_bytes(("# moved\n\n" + CODE).encode())
    after = cl.reassess(c["id"], reason="moved")
    assert after["status"] == "statically_verified"  # relocated exactly through its anchor
    eid = cl.evidence(c["id"])[0]["id"]
    loc = st.latest_evidence_location(eid)
    assert (loc["status"], loc["line_start"], loc["line_end"]) == ("moved", 3, 5)
    assert cl.show(c["id"])["supporting"][0]["now"] == "pkg/calc.py:3-5 (moved)"
    assert st.evidence(eid)["line_start"] == 1  # the evidence row itself never changes


def test_unanchored_moved_or_deleted_source_blocks_verification(env):
    repo, st, cl = env
    c = make(cl, "statically_verified", [(src(repo, anchor=False), "supports")])
    (repo / "pkg" / "calc.py").write_bytes(("# moved\n" + CODE).encode())
    assert cl.reassess(c["id"], reason="moved")["status"] not in VERIFIED
    (repo / "pkg" / "calc.py").write_bytes(CODE.encode())
    assert cl.reassess(c["id"], reason="restored")["status"] == "statically_verified"
    (repo / "pkg" / "calc.py").unlink()
    assert cl.reassess(c["id"], reason="deleted")["status"] not in VERIFIED


def test_experiment_verified_needs_a_passing_relevant_run(env):
    repo, st, cl = env
    failing = make(cl, "experiment_verified", [(run_result("fail"), "supports")], text=RUN_TEXT)
    assert failing["status"] not in VERIFIED
    with pytest.raises(ClaimRuleError, match="passed"):
        cl.set_status(failing["id"], "experiment_verified", reason="x", downgrade=False)
    passing = make(cl, "experiment_verified", [(run_result("pass"), "supports")], text=RUN_TEXT)
    assert (passing["status"], passing["confidence"]) == ("experiment_verified", 0.95)
    # A source line is not an experiment.
    assert make(cl, "experiment_verified", [(src(repo), "supports")])["status"] == "statically_verified"


def test_primary_source_verified_needs_a_primary_source(env):
    repo, st, cl = env
    text = "We sum items in total()"
    assert make(cl, "primary_source_verified", [(doc(repo), "supports")], text=text)["status"] == \
        "primary_source_verified"
    sec = {"source_type": "secondary", "locator": "https://blog.example", "content_hash": "sha256:1",
           "excerpt": "We sum items in total()"}
    assert make(cl, "primary_source_verified", [(sec, "supports")], text=text)["status"] == "strong_inference"


def test_refutation_at_least_as_strong_blocks_verification(env):
    repo, st, cl = env
    same_rank = make(cl, "statically_verified", [(src(repo), "supports"), (src(repo, 6, 7), "refutes")])
    assert same_rank["status"] not in VERIFIED and same_rank["status"] == "strong_inference"
    with pytest.raises(ClaimRuleError, match="refuting evidence at least as strong"):
        cl.set_status(same_rank["id"], "statically_verified", reason="x", downgrade=False)
    # A weaker refutation (design doc vs source code) does not block.
    weaker = make(cl, "statically_verified", [(src(repo), "supports"), (doc(repo), "refutes")])
    assert weaker["status"] == "statically_verified"
    # Non-verifying refutation never blocks either.
    assert make(cl, "statically_verified", [(src(repo), "supports"), (edge("EXTRACTED"), "refutes")]
                )["status"] == "statically_verified"


def test_refutation_that_outranks_support_contradicts(env):
    repo, st, cl = env
    c = make(cl, "primary_source_verified", [(doc(repo), "supports")], text="We sum items in total()")
    assert c["status"] == "primary_source_verified"
    cl.attach(c["id"], src(repo), "refutes", note="code says otherwise")
    after = cl.reassess(c["id"], reason="code refutes the doc")
    assert (after["status"], after["confidence"]) == ("contradicted", CONFIDENCE_CAP["contradicted"])
    assert "outranks" in cl.show(c["id"])["history"][-1]["reason"]
    # Only refutation, no support -> contradicted as well (at creation too).
    only_ref = make(cl, "unknown", [(src(repo), "refutes")])
    assert cl.reassess(only_ref["id"], reason="x")["status"] == "contradicted"
    assert make(cl, "statically_verified", [(src(repo), "refutes")])["status"] == "contradicted"
    # contradicted is only allowed with refuting evidence.
    lonely = make(cl, "weak_inference")
    with pytest.raises(ClaimRuleError, match="refuting"):
        cl.set_status(lonely["id"], "contradicted", reason="x", downgrade=False)


def test_heuristic_refutation_lowers_one_step_and_never_contradicts(env):
    repo, st, cl = env
    heur = src(repo, 6, 7, meta={"strength": "heuristic", "check": "probe"})
    c = make(cl, "statically_verified", [(src(repo), "supports"), (heur, "refutes")])
    assert c["status"] == "strong_inference"  # one step down from statically_verified
    assert "heuristic refutation" in cl.show(c["id"])["history"][-1]["reason"]
    assert cl.reassess(c["id"], reason="again")["status"] == "strong_inference"
    with pytest.raises(ClaimRuleError, match="definitive"):
        cl.set_status(c["id"], "contradicted", reason="x", downgrade=False)
    only_heur = make(cl, "unknown", [(dict(heur), "refutes")])
    assert cl.reassess(only_heur["id"], reason="x")["status"] != "contradicted"
    # a definitive refutation still contradicts
    definitive = src(repo, 6, 7, meta={"strength": "definitive"})
    d = make(cl, "unknown", [(definitive, "refutes")])
    assert cl.reassess(d["id"], reason="x")["status"] == "contradicted"
    assert [step_down(s) for s in ("statically_verified", "strong_inference", "weak_inference", "unknown")] == \
        ["strong_inference", "weak_inference", "unknown", "unknown"]


def test_refused_contradiction_never_raises_the_claim(env):
    repo, st, cl = env
    c = make(cl, "strong_inference", [(src(repo), "supports")])  # evidence would allow statically_verified
    after = cl.set_status(c["id"], "contradicted", reason="no proof given", downgrade=True)
    assert after["status"] == "strong_inference"


def test_ceiling_prevents_reverification_from_escalating_an_inference(env):
    repo, st, cl = env
    c = make(cl, "strong_inference", [(src(repo), "supports")])
    assert c["status"] == "strong_inference" and c["spec"]["ceiling"] == "strong_inference"
    assert check_status("statically_verified", cl.evidence(c["id"]), claim=c, repo=repo) is None
    for _ in range(2):
        again = cl.reassess(c["id"], reason="re-verify")
        assert (again["status"], again["confidence"]) == ("strong_inference", CONFIDENCE_CAP["strong_inference"])
    cl.set_status(c["id"], "stale", reason="file changed")
    assert cl.reassess(c["id"], reason="restored")["status"] == "strong_inference"
    # lower_ceiling only ever lowers.
    cl.lower_ceiling(c["id"], "weak_inference")
    cl.lower_ceiling(c["id"], "statically_verified")
    assert cl.get(c["id"])["spec"]["ceiling"] == "weak_inference"
    assert cl.reassess(c["id"], reason="x")["status"] == "weak_inference"


def test_best_status_respects_order_and_ceiling(env):
    repo, st, cl = env
    c = make(cl, "unknown", [(src(repo), "supports"), (run_result("pass"), "supports")])
    evs = cl.evidence(c["id"])
    full = {e["id"]: "full" for e in evs}
    assert best_status(evs, grades=full)[0] == "experiment_verified"
    assert best_status(evs, "statically_verified", grades=full)[0] == "statically_verified"
    assert best_status(evs, "weak_inference", grades=full)[0] == "weak_inference"
    assert best_status([], None)[0] == "unknown"
    assert ORDER[0] == "experiment_verified" and ORDER[-1] == "unknown"
    # graded for this claim ("total() sums items"), the run is irrelevant: the source lines decide
    assert best_status(evs, claim=cl.get(c["id"]), repo=repo)[0] == "statically_verified"


def test_unknown_status_and_relation_are_rejected(env):
    repo, st, cl = env
    c = make(cl, "unknown")
    with pytest.raises(ClaimRuleError, match="unknown status"):
        cl.set_status(c["id"], "probably_true", reason="x")
    with pytest.raises(ClaimRuleError, match="relation"):
        cl.attach(c["id"], src(repo), "likes")
    with pytest.raises(KeyError):
        cl.get("clm_missing")


def test_confidence_is_capped_by_status_and_rounded(env):
    repo, st, cl = env
    c = make(cl, "strong_inference", [(edge("EXTRACTED"), "supports")], confidence=0.99, **EDGE_CLAIM)
    assert c["confidence"] == CONFIDENCE_CAP["strong_inference"]
    low = make(cl, "strong_inference", [(edge("EXTRACTED"), "supports")], confidence=0.2, **EDGE_CLAIM)
    assert low["confidence"] == 0.2
    odd = make(cl, "strong_inference", [(edge("EXTRACTED"), "supports")], confidence=0.4 - 0.2 + 1e-9,
               **EDGE_CLAIM)
    assert odd["confidence"] == 0.2 and cl.show(odd["id"])["confidence"] == 0.2


def test_critique_penalty_persists_through_reassessment(env):
    repo, st, cl = env
    c = make(cl, "strong_inference", [(edge("EXTRACTED"), "supports")], **EDGE_CLAIM)
    cl.set_penalty(c["id"], 0.3)
    after = cl.reassess(c["id"], reason="verify")
    assert after["status"] == "strong_inference" and after["confidence"] == 0.4  # 0.7 - 0.3, not the cap
    cl.set_penalty(c["id"], 0.0)
    assert cl.reassess(c["id"], reason="clean critique")["confidence"] == 0.7


def test_stale_never_raises_confidence_and_invalidates_memory(env):
    repo, st, cl = env
    c = make(cl, "unknown")
    assert cl.set_status(c["id"], "stale", reason="changed")["confidence"] == 0.0
    v = make(cl, "statically_verified", [(src(repo), "supports")])
    Memory(st).learn("calc.total", "pkg/calc.py", source_claim_id=v["id"])
    after = cl.set_status(v["id"], "stale", reason="changed")
    assert after["confidence"] == CONFIDENCE_CAP["stale"]
    assert Memory(st).recall("calc.total") == []
    assert Memory(st).history("calc.total")[0]["invalidation_reason"] == "source claim became stale"


def test_supersede_keeps_old_claim_contradicted_with_full_history(env):
    repo, st, cl = env
    old = make(cl, "strong_inference", [(edge("EXTRACTED"), "supports")], **{**EDGE_CLAIM, "text":
                                                                             "total() calls helper() twice"})
    new = cl.supersede(old["id"], TEXT, evidence=[(src(repo), "supports")], kind="general",
                       status="statically_verified", reason="user correction checked", actor="user", snapshot=None)
    o = cl.show(old["id"])
    assert o["status"] == "contradicted" and o["superseded_by"] == new["id"]
    assert new["supersedes"] == old["id"] and new["status"] == "statically_verified"
    assert [h["to_status"] for h in o["history"]] == ["unknown", "strong_inference", "contradicted"]
    assert o["history"][-1]["actor"] == "user" and o["history"][-1]["reason"] == "user correction checked"
    assert [e["at"] for e in o["refuting"]] == ["pkg/calc.py:1-3"]  # the correction's support refutes the old claim
    assert o["supporting"]  # original evidence kept
    # Re-assessing a superseded claim never revives it.
    assert cl.reassess(old["id"], reason="again")["status"] == "contradicted"
    assert st.one("SELECT COUNT(*) AS n FROM claims")["n"] == 2


def test_history_records_every_transition_with_actor(env):
    repo, st, cl = env
    c = make(cl, "statically_verified", [(src(repo), "supports")], actor="alice")
    cl.set_status(c["id"], "stale", reason="file changed", actor="updater", payload={"changed": ["pkg/calc.py"]})
    cl.reassess(c["id"], reason="verified again", actor="verify")
    h = cl.show(c["id"])["history"]
    assert [(x["from_status"], x["to_status"], x["actor"]) for x in h] == [
        (None, "unknown", "repoatlas"),
        ("unknown", "statically_verified", "alice"),
        ("statically_verified", "stale", "updater"),
        ("stale", "statically_verified", "verify"),
    ]
    # Setting the same status/confidence again is a no-op (no noise in history).
    cl.set_status(c["id"], "statically_verified", reason="same")
    assert len(cl.show(c["id"])["history"]) == 4


def test_rejected_request_is_recorded_in_history(env):
    repo, st, cl = env
    c = make(cl, "unknown", [(src(repo, 6, 7), "supports")], text="Orders are stored in PostgreSQL")
    n = len(st.history(c["id"]))
    with pytest.raises(ClaimRuleError):
        cl.set_status(c["id"], "statically_verified", reason="trust me", actor="alice", downgrade=False)
    h = st.history(c["id"])
    assert len(h) == n + 1 and h[-1]["from_status"] == h[-1]["to_status"] == "unknown"
    assert h[-1]["reason"].startswith("requested statically_verified rejected") and h[-1]["actor"] == "alice"
    assert h[-1]["payload"]["rejected"] == "statically_verified" and cl.get(c["id"])["status"] == "unknown"


# -- criterion 6: irrelevant evidence never verifies (acceptance audit 2026-09-23) --------------

def test_unrelated_source_line_never_verifies_a_claim_on_any_path(env):
    repo, st, cl = env
    unrelated = evmod.source_evidence(repo, "pkg/config.py", 3, commit="c0ffee")
    pg = "Orders are stored in PostgreSQL"
    c = make(cl, "statically_verified", [(unrelated, "supports")], text=pg)
    assert c["status"] not in VERIFIED
    assert "does not state the claim" in cl.show(c["id"])["history"][-1]["reason"] or \
        "not about the claim" in cl.show(c["id"])["history"][-1]["reason"]
    # attach + reassess (what `feedback resolve confirmed` and `verify` do)
    d = make(cl, "unknown", text=pg)
    eid = cl.attach(d["id"], evmod.source_evidence(repo, "pkg/config.py", 3, commit="c0ffee"), "supports")
    assert cl.reassess(d["id"], reason="confirmed by user")["status"] not in VERIFIED
    with pytest.raises(ClaimRuleError):
        cl.set_status(d["id"], "statically_verified", reason="force", downgrade=False)
    # an existing evidence id linked to a second claim is graded for that claim
    e = make(cl, "unknown", text=pg)
    cl.attach(e["id"], eid, "supports")
    assert cl.reassess(e["id"], reason="x")["status"] not in VERIFIED
    assert cl.grades(e["id"]) == {eid: "none"}
    # the same line verifies a claim it does state
    ok = make(cl, "statically_verified", [(eid, "supports")],
              text="DATABASE_URL is read from ORDERS_DATABASE_URL")
    assert ok["status"] == "statically_verified"


def test_unrelated_passing_run_never_verifies(env):
    repo, st, cl = env
    pricing_run = {"source_type": "test_result", "locator": "run exp_p: python -m pytest -q tests/test_pricing.py",
                   "excerpt": "3 passed in 0.05s", "content_hash": "sha256:9",
                   "meta": {"outcome": "pass", "experiment_id": "exp_p"}}
    c = make(cl, "experiment_verified", [(pricing_run, "supports")], text="Orders are encrypted with AES-256")
    assert c["status"] not in VERIFIED
    d = make(cl, "unknown", text="Orders are encrypted with AES-256")
    cl.attach(d["id"], dict(pricing_run), "supports")
    assert cl.reassess(d["id"], reason="ran tests")["status"] not in VERIFIED
    assert make(cl, "experiment_verified", [(dict(pricing_run), "supports")],
                text="the pricing tests pass")["status"] == "experiment_verified"


def test_negations_and_quantifiers_are_not_verified_by_term_overlap(env):
    repo, st, cl = env
    for text in ("total() never sums items", "only total() sums items", "total() does not sum items"):
        assert make(cl, "statically_verified", [(src(repo), "supports")], text=text)["status"] == \
            "strong_inference", text


def test_markdown_recorded_as_source_code_is_a_design_doc(env):
    repo, st, cl = env
    ev = evmod.source_evidence(repo, "docs/adr.md", 5, commit="c0ffee")  # asked for source_code
    assert ev["source_type"] == "design_doc" and ev["meta"]["reclassified_from"] == "source_code"
    legacy = {**ev, "source_type": "source_code"}  # a row recorded before the rule
    assert evmod.effective_type(legacy) == "design_doc" and evmod.rank(legacy) == 3
    c = make(cl, "statically_verified", [(legacy, "supports")], text="We sum items in total()")
    assert c["status"] == "primary_source_verified"  # a document, not verified code


# -- evidence groups -----------------------------------------------------------------------

def test_flow_group_is_verified_only_when_every_hop_is(env):
    repo, st, cl = env
    (repo / "pkg" / "flow.py").write_bytes(
        b"def a(x):\n    return b(x)\n\n\ndef b(x):\n    return c(x)\n\n\ndef c(x):\n    return x\n")
    spec = {"hops": [{"from": "a()", "to": "b()", "at": "pkg/flow.py:2", "relation": "calls",
                      "confidence": "EXTRACTED"},
                     {"from": "b()", "to": "c()", "at": "pkg/flow.py:6", "relation": "calls",
                      "confidence": "EXTRACTED"}]}

    def hop(line, h):
        return evmod.source_evidence(repo, "pkg/flow.py", line, commit=None, meta={"hop": h})

    good = make(cl, "statically_verified", [(hop(2, "a()->b()"), "supports"), (hop(6, "b()->c()"), "supports")],
                text="Data path a -> b -> c", kind="flow", spec=spec)
    assert good["status"] == "statically_verified"
    bad_spec = {"hops": [spec["hops"][0], {**spec["hops"][1], "to": "d()"}]}
    bad = make(cl, "statically_verified", [(hop(2, "a()->b()"), "supports"), (hop(6, "b()->d()"), "supports")],
               text="Data path a -> b -> d", kind="flow", spec=bad_spec)
    assert bad["status"] not in VERIFIED  # one broken hop sinks the whole group
    assert "does not state the claim" in cl.show(bad["id"])["history"][-1]["reason"]


def test_explicit_groups_are_conjunctive(env):
    repo, st, cl = env
    c = make(cl, "unknown")
    cl.attach(c["id"], src(repo), "supports", grp="g1")
    cl.attach(c["id"], src(repo, 6, 7), "supports", grp="g1")  # helper(): does not state the claim
    assert cl.reassess(c["id"], reason="x", ceiling="statically_verified")["status"] not in VERIFIED
    cl.attach(c["id"], src(repo, 1, 2), "supports", grp="g2")
    assert cl.set_status(c["id"], "statically_verified", reason="g2 states it", downgrade=False)["status"] == \
        "statically_verified"
    assert {e["grp"] for e in cl.evidence(c["id"])} == {"g1", "g2"}


# -- strong_inference needs real (if weak) support ---------------------------------------------

@pytest.mark.parametrize("stype", ["search_result", "model_summary", "user_feedback"])
def test_search_results_summaries_and_feedback_alone_are_not_inference(env, stype):
    repo, st, cl = env
    pointer = {"source_type": stype, "locator": "https://search.example/?q=total", "content_hash": "sha256:2"}
    c = make(cl, "strong_inference", [(dict(pointer), "supports")])
    assert c["status"] == "weak_inference" and c["confidence"] <= CONFIDENCE_CAP["weak_inference"]
    assert f"other than {stype}" in cl.show(c["id"])["history"][-1]["reason"]
    with pytest.raises(ClaimRuleError, match="other than"):
        cl.set_status(c["id"], "strong_inference", reason="a search says so", downgrade=False)
    # The same pointer next to a graph edge (an extraction) is inference again.
    both = make(cl, "strong_inference", [(dict(pointer), "supports"), (edge("INFERRED"), "supports")],
                **EDGE_CLAIM)
    assert both["status"] == "strong_inference"
    assert make(cl, "strong_inference", [(src(repo), "supports")])["status"] == "strong_inference"


def test_strong_inference_needs_relevant_support(env):
    repo, st, cl = env
    c = make(cl, "strong_inference", [(src(repo, 6, 7), "supports")], text="Orders are stored in PostgreSQL")
    assert c["status"] == "weak_inference"
    assert "not about the claim" in cl.show(c["id"])["history"][-1]["reason"]


# -- requested statuses outside the ordered list ------------------------------------------------

@pytest.mark.parametrize("requested", ["contradicted", "statically_verifed", "probably_true"])
def test_status_outside_order_never_rises_above_strong_inference(env, requested):
    repo, st, cl = env
    # The evidence alone would allow statically_verified.
    c = make(cl, requested, [(src(repo), "supports")])
    assert c["status"] == "unknown"  # contradicted needs refutation; a typo is not a status
    assert c["spec"]["ceiling"] == c["spec"]["assessed"] == "strong_inference"
    for _ in range(2):
        assert cl.reassess(c["id"], reason="re-verify")["status"] == "strong_inference"
    cl.set_status(c["id"], "stale", reason="file changed")
    assert cl.reassess(c["id"], reason="restored")["status"] == "strong_inference"


def test_requested_contradiction_with_refutation_is_kept_and_capped(env):
    repo, st, cl = env
    c = make(cl, "contradicted", [(src(repo), "refutes")])
    assert c["status"] == "contradicted" and c["spec"]["ceiling"] == "strong_inference"


def test_stale_cannot_be_requested_at_creation(env):
    repo, st, cl = env
    n = st.one("SELECT COUNT(*) AS n FROM claims")["n"]
    with pytest.raises(ClaimRuleError, match="stale is set only by invalidation"):
        make(cl, "stale", [(src(repo), "supports")])
    assert st.one("SELECT COUNT(*) AS n FROM claims")["n"] == n  # nothing half-created


def test_assessed_level_is_recorded_and_kept_apart_from_the_ceiling(env):
    repo, st, cl = env
    c = make(cl, "statically_verified", [(src(repo), "supports")])
    assert c["spec"]["assessed"] == c["spec"]["ceiling"] == "statically_verified"
    cl.lower_ceiling(c["id"], "weak_inference")
    spec = cl.get(c["id"])["spec"]
    assert spec["ceiling"] == "weak_inference" and spec["assessed"] == "statically_verified"
    assert cl.assessed(cl.get(c["id"])) == "statically_verified"
    # A spec passed in (e.g. copied from a superseded claim) cannot smuggle in a level or a penalty.
    d = make(cl, "weak_inference", [(src(repo), "supports")],
             spec={"assessed": "experiment_verified", "ceiling": "experiment_verified", "penalty": 0.5})
    assert d["spec"]["assessed"] == d["spec"]["ceiling"] == "weak_inference" and "penalty" not in d["spec"]


def test_explicit_rule_checked_raise_is_a_new_assessment(env):
    repo, st, cl = env
    c = make(cl, "unknown", [(src(repo), "supports")])
    assert (c["spec"]["assessed"], c["spec"]["ceiling"]) == ("unknown", "unknown")
    up = cl.set_status(c["id"], "statically_verified", reason="checked the lines", downgrade=False)
    assert up["status"] == "statically_verified"
    assert (up["spec"]["assessed"], up["spec"]["ceiling"]) == ("statically_verified", "statically_verified")
    # Re-verification now keeps what was explicitly established (it used to fall back to 'unknown').
    assert cl.reassess(c["id"], reason="re-verify")["status"] == "statically_verified"
    # A refused or downgrading request never raises the recorded levels.
    w = make(cl, "weak_inference", [(edge("EXTRACTED"), "supports")], **EDGE_CLAIM)
    with pytest.raises(ClaimRuleError):
        cl.set_status(w["id"], "statically_verified", reason="x", downgrade=False)
    cl.set_status(w["id"], "statically_verified", reason="x", downgrade=True)
    assert cl.get(w["id"])["spec"]["ceiling"] == "weak_inference"


def test_assessed_falls_back_to_the_initial_assessment_for_older_claims(env):
    repo, st, cl = env
    c = make(cl, "statically_verified", [(edge("EXTRACTED"), "supports")], **EDGE_CLAIM)  # -> strong_inference
    spec = dict(c["spec"])
    spec.pop("assessed")
    spec["ceiling"] = "weak_inference"  # lowered by an earlier critique
    st.update_claim(c["id"], {"spec": spec})
    assert cl.assessed(cl.get(c["id"])) == "strong_inference"


def test_claim_key_is_line_independent(env):
    repo, st, cl = env
    k1 = claim_key("relation", {"source": "a", "target": "b", "at": "x.py:3"}, ["x.py::a"], "a calls b (x.py:3)")
    k2 = claim_key("relation", {"source": "a", "target": "b", "at": "x.py:9"}, ["x.py::a"], "a calls b (x.py:9)")
    assert k1 == k2 and k1 != claim_key("relation", {"source": "a", "target": "c"}, ["x.py::a"], "")
    assert claim_key("general", {}, [], "X is read at a.py:3") == claim_key("general", {}, [], "X is read at a.py:30")
    c = make(cl, "statically_verified", [(src(repo), "supports")])
    assert c["claim_key"].startswith("ck:") and c["verified_at"]
