"""A host's intent on analyze (full profile and ``verinoda analyze --intent``): weighed against the rule
reading of the question, used only when the two agree, reported when they do not, and never a claim's
status."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import shutil  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import analysis, analysis_view, workflow  # noqa: E402
from verinoda import question_plan as qp  # noqa: E402
from verinoda.store import open_store  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "orders_app"
WRITTEN = "Where is an order written to the database?"     # rules: locate, with dataflow second
CALLERS = "Who calls place_order?"                         # rules: callers only


# -- the plan, without a graph ----------------------------------------------------------------------

def test_an_intent_the_rules_also_read_takes_over_the_sub_question():
    plan = qp.draft(WRITTEN, None)
    assert [(s["intent"], s.get("secondary_intents")) for s in plan["sub_questions"]] == [("locate", ["dataflow"])]
    got, ic = qp.host_intent(plan, "dataflow")
    sq = got["sub_questions"][0]
    assert sq["intent"] == "dataflow" and sq["secondary_intents"] == ["locate"]
    assert sq["done_when"]["kind"] == qp.DEFAULT_DONE["dataflow"][0]
    assert sq["done_when"]["min_status"] == qp.DEFAULT_DONE["dataflow"][1]
    assert sq["done_when"]["subjects"] == plan["sub_questions"][0]["done_when"]["subjects"]
    assert sq["derived_by"].endswith("+host_intent") and got["derived_by"].endswith("+host_intent")
    assert ic == {"given": "dataflow", "read_as": ["locate", "dataflow"], "agrees": True, "applied": True,
                  "sub_questions": ["q1"], "retyped": ["q1"]}
    assert plan["sub_questions"][0]["intent"] == "locate"  # the drafted plan is not edited in place
    assert qp.validate(got) == []


def test_an_intent_the_rules_do_not_read_is_reported_and_not_used():
    plan = qp.draft(CALLERS, None)
    got, ic = qp.host_intent(plan, "why")
    assert got is None
    assert ic["given"] == "why" and ic["read_as"] == ["callers"] and ic["agrees"] is False
    assert ic["applied"] is False and "callers" in ic["why"]


def test_the_rules_own_intent_agrees_and_changes_nothing_but_the_order():
    plan = qp.draft(CALLERS, None)
    got, ic = qp.host_intent(plan, "callers")
    assert ic["agrees"] and ic["applied"] and "retyped" not in ic
    assert got["sub_questions"] == plan["sub_questions"]


def test_the_sub_questions_the_intent_names_come_first():
    plan = qp.draft("Who calls place_order and which tests cover compute_total?", None)
    assert [s["intent"] for s in plan["sub_questions"]] == ["callers", "tests"]
    got, ic = qp.host_intent(plan, "tests")
    assert [s["id"] for s in got["sub_questions"]] == ["q2", "q1"] and ic["sub_questions"] == ["q2"]
    errors, _warnings, order = qp._integrity(got)
    assert not errors and order == ["q2", "q1"]


def test_a_question_without_an_intent_cue_takes_the_hosts():
    plan = qp.draft("compute_total", None)
    assert qp.intents_for("compute_total") == []
    assert plan["sub_questions"][0]["derived_by"].endswith(":default_locate")
    got, ic = qp.host_intent(plan, "callers")
    assert got["sub_questions"][0]["intent"] == "callers" and "secondary_intents" not in got["sub_questions"][0]
    assert ic["agrees"] and ic["retyped"] == ["q1"] and ic["read_as"] == []


def test_a_choice_stays_the_users():
    plan = qp.draft("Should we switch to Postgres?", None)
    got, ic = qp.host_intent(plan, "dataflow")
    assert got is None and not ic["agrees"] and "choice" in ic["why"]
    # and decide is never put on a question that does not ask for a choice
    got, ic = qp.host_intent(qp.draft("compute_total", None), "decide")
    assert got is None and not ic["agrees"]
    got, ic = qp.host_intent(plan, "decide")
    assert got is not None and ic["agrees"]


@pytest.mark.parametrize("question", ["Pros and cons of using asyncio here", "Redis'in avantajları neler?"])
def test_decide_agrees_where_the_rules_read_a_weak_decide_cue(question):
    # the rules read decide here though the message does not ask for a choice in so many words: a host that
    # reads decide too agrees with them, and every other intent is the one that disagrees
    assert not qp.asks_for_choice(question) and qp.intents_for(question) == ["decide"]
    plan = qp.draft(question, None)
    assert [s["intent"] for s in plan["sub_questions"]] == ["decide"]
    got, ic = qp.host_intent(plan, "decide")
    assert ic["agrees"] is True and ic["applied"] is True and ic["sub_questions"] == ["q1"] and "retyped" not in ic
    assert got["sub_questions"] == plan["sub_questions"]
    got, ic = qp.host_intent(plan, "callers")
    assert got is None and ic["agrees"] is False


@pytest.mark.parametrize("question", ["Should we switch to Postgres? Who calls place_order?",
                                      "Who calls place_order? Should we switch to Postgres?"])
def test_a_choice_is_judged_per_sub_question_whatever_the_clause_order(question):
    plan = qp.draft(question, None)
    by_id = {s["id"]: s["intent"] for s in plan["sub_questions"]}
    assert sorted(by_id.values()) == ["callers", "decide"]
    choice_id = next(i for i, x in by_id.items() if x == "decide")
    order = [s["id"] for s in plan["sub_questions"]]
    # decide agrees with the clause that asks for the choice, in either order, and that clause comes first
    got, ic = qp.host_intent(plan, "decide")
    assert ic["agrees"] and ic["applied"] and ic["sub_questions"] == [choice_id] and "retyped" not in ic
    assert got["sub_questions"][0]["id"] == choice_id
    # callers applies to the clause that carries it; the choice is neither retyped nor moved from its place
    got, ic = qp.host_intent(plan, "callers")
    assert ic["agrees"] and ic["applied"] and ic["sub_questions"] != [choice_id] and "retyped" not in ic
    assert [s["id"] for s in got["sub_questions"]] == order
    assert {s["id"]: s["intent"] for s in got["sub_questions"]} == by_id


def test_an_intent_the_rules_read_only_on_a_choice_does_not_take_it():
    # q1 asks for a choice and carries behaviour as a secondary intent; q2 is locate
    plan = qp.draft("Should we switch to Postgres? Where is compute_total?", None)
    assert [(s["intent"], s.get("secondary_intents")) for s in plan["sub_questions"]] == \
        [("decide", ["behaviour"]), ("locate", None)]
    got, ic = qp.host_intent(plan, "behaviour")
    assert got is None and ic["agrees"] is False and "choice" in ic["why"]
    got, ic = qp.host_intent(plan, "locate")
    assert ic["agrees"] and ic["sub_questions"] == ["q2"]
    assert [(s["id"], s["intent"]) for s in got["sub_questions"]] == [("q1", "decide"), ("q2", "locate")]


# -- analyze on a scanned copy of examples/orders_app ------------------------------------------------

def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args],
                   cwd=cwd, check=True, capture_output=True)


def _scanned(dst: Path) -> Path:
    shutil.copytree(EXAMPLE, dst, ignore=shutil.ignore_patterns(".verinoda", "__pycache__", "*.pyc",
                                                                ".pytest_cache", "*.db"))
    _git(dst, "init", "-q")
    _git(dst, "add", "-A")
    _git(dst, "commit", "-q", "-m", "init")
    workflow.init(dst)
    st = open_store(dst)
    try:
        workflow.scan(st, dst)
    finally:
        st.close()
    return dst


@pytest.fixture(scope="module")
def copies(tmp_path_factory):
    """Two copies scanned alike: each analysis runs on its own, so no run reuses the other's claims."""
    if shutil.which("git") is None:
        pytest.skip("git not available")
    base = tmp_path_factory.mktemp("intent")
    return _scanned(base / "a" / "orders_app"), _scanned(base / "b" / "orders_app")


def _run(repo: Path, question: str, intent: str | None) -> dict:
    st = open_store(repo)
    try:
        return analysis.analyze(st, repo, question, intent=intent)
    finally:
        st.close()


def _statuses(res: dict) -> dict[str, str]:
    return {c["text"]: c["status"] for c in res["claims"]}


def test_a_disagreeing_intent_leaves_the_answer_as_the_rules_give_it(copies):
    a, b = copies
    plain = _run(a, CALLERS, None)
    hinted = _run(b, CALLERS, "why")
    assert "intent_check" not in plain
    assert hinted["intent_check"]["agrees"] is False and hinted["intent_check"]["read_as"] == ["callers"]
    assert [(s["intent"], s["status"]) for s in hinted["subquestions"]] == \
        [(s["intent"], s["status"]) for s in plain["subquestions"]]
    assert sorted(_statuses(hinted).items()) == sorted(_statuses(plain).items())
    text = analysis_view.render_text(hinted)
    assert "host intent why: not used" in text
    assert analysis_view.lean(hinted)["intent_check"] == hinted["intent_check"]


def test_an_agreeing_intent_changes_the_handlers_never_a_status(copies):
    a, b = copies
    plain = _run(a, WRITTEN, None)
    hinted = _run(b, WRITTEN, "dataflow")
    assert hinted["intent_check"]["applied"] is True and hinted["intent_check"]["retyped"] == ["q1"]
    sq = hinted["subquestions"][0]
    assert sq["intent"] == "dataflow" and sq["done_when"]["kind"] == "claim_exists"
    assert plain["subquestions"][0]["intent"] == "locate"
    st = open_store(b)
    try:
        row = qp.get_plan(st, hinted["plan_id"])  # checked as the host's reading, stored as the rules' draft
        assert row["check_result"]["source"] == "host" and row["source"] == "fallback"
        assert row["plan"]["derived_by"].endswith("+host_intent")
    finally:
        st.close()
    # a claim both runs make has the same status in both: the intent picks what is looked for, not how
    # sure an answer is
    p, h = _statuses(plain), _statuses(hinted)
    common = set(p) & set(h)
    assert common and all(p[t] == h[t] for t in common)
    assert "host intent dataflow: used for q1" in analysis_view.render_text(hinted)


def test_a_sub_question_left_at_the_default_does_not_make_the_rules_plan_disagree(copies):
    # q1 has no cue (locate by default), q2 is read as flow: plan check's intent_divergence names q1 for the
    # rules' plan itself, which is no disagreement with a host that also reads flow
    question = "In the orders service, how was an order placed and what called it?"
    plan = qp.draft(question, None)
    assert [(s["intent"], s["derived_by"].endswith(":default_locate")) for s in plan["sub_questions"]] == \
        [("locate", True), ("flow", False)]
    _a, b = copies
    res = _run(b, question, "flow")
    assert res["intent_check"]["agrees"] is True and res["intent_check"]["applied"] is True
    assert res["intent_check"]["sub_questions"] == ["q2"] and "retyped" not in res["intent_check"]
    # q2's "it" is q1's subject: the sub-question it depends on still runs first
    assert plan["sub_questions"][1].get("subject_from") == "q1"
    assert [s["id"] for s in res["subquestions"]] == ["q1", "q2"]


def test_decide_on_a_weak_decide_question_is_used_by_analyze(copies):
    _a, b = copies
    res = _run(b, "Pros and cons of using sqlite in place_order", "decide")
    assert res["intent_check"]["read_as"] == ["decide"]
    assert res["intent_check"]["agrees"] is True and res["intent_check"]["applied"] is True
    assert res["subquestions"][0]["intent"] == "decide"
    text = analysis_view.render_text(res)
    assert "host intent decide: used for q1" in text and "not used" not in text


@pytest.mark.parametrize("question,intent,used", [
    ("Should we switch to Postgres? Who calls place_order?", "callers", "q2"),
    ("Who calls place_order? Should we switch to Postgres?", "decide", "q2"),
    ("Who calls place_order? Should we switch to Postgres?", "callers", "q1")])
def test_analyze_applies_an_intent_to_its_clause_beside_a_choice(copies, question, intent, used):
    _a, b = copies
    res = _run(b, question, intent)
    ic = res["intent_check"]
    assert ic["agrees"] is True and ic["applied"] is True and ic["sub_questions"] == [used]
    assert "retyped" not in ic
    assert sorted(s["intent"] for s in res["subquestions"]) == ["callers", "decide"]


def test_a_failing_plan_that_the_intent_only_reordered_is_blamed_on_the_draft(copies, monkeypatch):
    # the host's plan only reorders the rules' one: when it fails its checks, the why names the draft
    real = qp.check

    def failing_host(plan, *args, **kwargs):
        res = real(plan, *args, **kwargs)
        if kwargs.get("source") == "host":
            res = {**res, "status": "invalid", "errors": [qp._problem("/", "schema", "made to fail")]}
        return res

    monkeypatch.setattr(qp, "check", failing_host)
    _a, b = copies
    res = _run(b, CALLERS, "callers")
    ic = res["intent_check"]
    assert ic["applied"] is False and ic["agrees"] is True
    assert ic["why"] == "the plan drafted by the rules fails its own checks: made to fail"
    res = _run(b, WRITTEN, "dataflow")  # this one retypes q1: the why names the intent
    assert res["intent_check"]["why"] == "the plan with this intent fails its own checks: made to fail"


def test_an_intent_is_for_a_question_not_a_plan(copies):
    a, _b = copies
    st = open_store(a)
    try:
        with pytest.raises(ValueError, match="plan"):
            analysis.analyze(st, a, "", plan=qp.draft(CALLERS, None), intent="callers")
        with pytest.raises(ValueError, match="unknown intent"):
            analysis.analyze(st, a, CALLERS, intent="callers_of")
    finally:
        st.close()


def test_the_cli_takes_intent_and_refuses_an_unknown_one(copies, capsys):
    from verinoda import cli

    a, _b = copies
    assert cli.main(["analyze", WRITTEN, "--intent", "dataflow", "--repo", str(a), "--refresh", "skip"]) == 0
    assert "host intent dataflow: used for q1" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(["analyze", WRITTEN, "--intent", "dataflows", "--repo", str(a)])
