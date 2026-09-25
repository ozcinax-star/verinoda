"""Decisions (docs/DESIGN.md D33): a choice is the human's. Runs on git copies of the examples (never on
examples/ itself)."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import shutil  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import analysis, workflow  # noqa: E402
from verinoda import question_plan as qp  # noqa: E402
from verinoda.store import open_store  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ORDERS = ROOT / "examples" / "orders_app"
EN_Q = "Should we move orders from SQLite to PostgreSQL if traffic grows?"
TR_Q = "Sipariş sayısı artarsa SQLite'tan PostgreSQL'e geçmeli miyiz?"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false",
                           "-c", "commit.gpgsign=false", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout


def _copy(src: Path, dst: Path, *, scan: bool = True) -> Path:
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns(".verinoda", "__pycache__", "*.pyc", ".pytest_cache",
                                                            "*.db"))
    _git(dst, "init", "-q")
    _git(dst, "add", "-A")
    _git(dst, "commit", "-q", "-m", "init")
    if scan:
        workflow.init(dst)
        st = open_store(dst)
        try:
            workflow.scan(st, dst)
        finally:
            st.close()
    return dst


@pytest.fixture(scope="module")
def orders(tmp_path_factory):
    repo = _copy(ORDERS, tmp_path_factory.mktemp("decide") / "orders_app")
    st = open_store(repo)
    yield repo, st
    st.close()


# -- the decide intent (step 1) ---------------------------------------------------------------------

@pytest.mark.parametrize("question", [EN_Q, TR_Q,
                                      "Sipariş sayısı artarsa bu sistemi nasıl büyütürüz, hangi veritabanını "
                                      "seçmeliyiz?"])
def test_a_decision_question_is_never_met(orders, question):
    repo, st = orders
    res = analysis.analyze(st, repo, question, challenge=False)
    assert res["status"] == "answered"
    subs = res["subquestions"]
    assert subs and all(s["intent"] == "decide" for s in subs)
    assert all(s["status"] == qp.HUMAN_DECISION for s in subs), [(s["id"], s["status"]) for s in subs]
    # the options the question names are not expected in the code: no "words occur nowhere" unknown
    assert not any("occur nowhere" in u["why"] for u in res["unknowns"])
    assert any("the human decides" in u["why"] for u in res["unknowns"])


def test_judge_never_meets_a_decision_whatever_its_claims():
    sq = {"id": "q1", "intent": "decide", "done_when": {"kind": "decision_brief", "detail": "x"}}
    strong = [{"id": "c1", "kind": "decision", "status": "primary_source_verified"}]
    assert analysis.judge(sq, strong) == qp.HUMAN_DECISION
    assert analysis.judge(sq, []) == qp.HUMAN_DECISION
    assert analysis.judge(sq, strong, {"blocked": "c-version"}) == "blocked_by_clarification"


def test_a_compound_question_keeps_its_code_part_answerable(orders):
    repo, st = orders
    res = analysis.analyze(st, repo, "Why do we use SQLite for orders, and should we move to PostgreSQL?",
                           challenge=False)
    by_intent = {s["intent"]: s["status"] for s in res["subquestions"]}
    assert by_intent.get("decide") == qp.HUMAN_DECISION
    assert by_intent.get("why") in ("met", "met_with_inference"), res["subquestions"]
