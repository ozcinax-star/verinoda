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


# -- decision records (step 2) ----------------------------------------------------------------------

def _fresh_orders(tmp_path):
    repo = _copy(ORDERS, tmp_path / "orders_app")
    return repo, open_store(repo)


def test_record_writes_a_human_decision_with_front_matter_and_logs_it(tmp_path):
    from verinoda import decisions as dm

    repo, st = _fresh_orders(tmp_path)
    try:
        res = dm.record(st, repo, chosen="SQLite", rationale="zero-ops local development",
                        guards=["only_in calls=sqlite3.connect allowed=orders/repository.py"],
                        governs=["orders/repository.py::OrderRepository.__init__"],
                        revisit_when=["dependency_added=psycopg"], user_statement="keep SQLite for now")
        # docs/adr/0001-... already holds number 1: a new record never reuses it
        assert res["id"] == "ADR-0002" and res["status"] == "accepted" and res["decided_by"] == "human"
        path = repo / res["file"]
        assert path.parent == (repo / ".verinoda" / "decisions").resolve() and b"\r" not in path.read_bytes()
        front, body = dm.split_front(path.read_text(encoding="utf-8"))
        assert front["verinoda-decision"] == 1 and front["decided-by"] == "human" and front["chosen"] == "SQLite"
        assert front["guards"][0]["calls"] == ["sqlite3.connect"] and front["guards"][0]["status"] == "accepted"
        assert front["governs"][0]["qual"] == "OrderRepository.__init__" and front["governs"][0]["fp"]
        assert front["revisit-when"] == [{"id": "r1", "kind": "dependency_added", "value": "psycopg",
                                          "spec": "dependency_added=psycopg"}]
        assert "keep SQLite for now" in body and dm.GEN_START in body
        d = dm.find(repo, "adr-2")
        assert d is not None and d.enforced and d.guards == front["guards"]
        rows = st.all("SELECT * FROM decisions WHERE id = 'ADR-0002'")
        assert [r["event"] for r in rows] == ["record"] and rows[0]["user_statement"] == "keep SQLite for now"
        assert rows[0]["doc_hash"] == dm.content_hash(path.read_bytes())
    finally:
        st.close()


def test_supersede_waive_and_the_log_is_append_only(tmp_path):
    import sqlite3

    from verinoda import decisions as dm

    repo, st = _fresh_orders(tmp_path)
    try:
        a = dm.record(st, repo, chosen="SQLite", rationale="r", guards=["dependency absent=psycopg"])
        w = dm.waive(st, repo, a["id"], "g1", at="pyproject.toml:4", reason="spike", until="2020-01-01")
        assert w["waivers"][0]["at"] == "pyproject.toml:4"
        d = dm.find(repo, a["id"])
        assert dm.waived(d, "g1", "pyproject.toml", 4, today="2019-12-31") is not None
        assert dm.waived(d, "g1", "pyproject.toml", 4, today="2020-01-02") is None  # expired
        assert dm.waived(d, "g1", "pyproject.toml", 5, today="2019-12-31") is None
        b = dm.record(st, repo, chosen="PostgreSQL", rationale="managed Postgres", supersedes=a["id"])
        assert b["supersedes"] == a["id"] and b["superseded"] == a["id"]
        old = dm.find(repo, a["id"])
        assert old.status == "superseded" and old.superseded_by == b["id"] and not old.enforced
        with pytest.raises(dm.DecisionError, match="already superseded"):
            dm.record(st, repo, chosen="x", rationale="y", supersedes=a["id"])
        events = [r["event"] for r in st.all("SELECT event FROM decisions ORDER BY seq")]
        assert events == ["record", "waive", "record", "supersede"]
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            st.conn.execute("UPDATE decisions SET status = 'accepted'")
        st.conn.rollback()
        with pytest.raises(sqlite3.DatabaseError, match="never deleted"):
            st.conn.execute("DELETE FROM decisions")
        st.conn.rollback()
    finally:
        st.close()


def test_import_only_proposes_guards_and_accept_activates_them(tmp_path):
    from verinoda import decisions as dm
    from verinoda import index

    repo, st = _fresh_orders(tmp_path)
    try:
        before = (repo / "docs/adr/0001-sqlite-persistence.md").read_bytes()
        res = dm.import_doc(st, repo, "docs/adr/0001-sqlite-persistence.md", graph=index.load(repo))
        assert res["id"] == "ADR-0001" and res["source"] == "docs/adr/0001-sqlite-persistence.md"
        assert (repo / "docs/adr/0001-sqlite-persistence.md").read_bytes() == before  # never edited
        (g,) = res["guards"]
        assert g["status"] == "proposed" and g["sink"] == "db-connection" and g["allowed"] == ["orders/repository.py"]
        assert g["from_sentence"]["at"] == "docs/adr/0001-sqlite-persistence.md:5"
        assert any("No other module" in s["text"] for s in res["not_turned_into_guards"])
        assert dm.listing(st, repo)["unrecorded_docs"] == []
        with pytest.raises(dm.DecisionError, match="already has a record"):
            dm.import_doc(st, repo, "docs/adr/0001-sqlite-persistence.md")
        with pytest.raises(dm.DecisionError, match="no guard g9"):
            dm.accept(st, repo, "ADR-1", ["g9"])
        acc = dm.accept(st, repo, "ADR-1", ["g1"], user_statement="yes")
        assert acc["guards"][0]["status"] == "accepted"
    finally:
        st.close()


def test_records_refuse_what_a_human_did_not_decide_and_paths_outside_the_repo(tmp_path):
    import json

    from verinoda import decisions as dm

    repo, st = _fresh_orders(tmp_path)
    try:
        for bad in ("only_in calls=sqlite3.connect",                      # no allowed files
                    "only_in calls=connect allowed=a.py",                 # not a dotted name
                    "only_in calls=sqlite3.connect allowed=../x.py",      # leaves the repository
                    "only_in calls=sqlite3.connect allowed=/etc/passwd",
                    "only_in calls=sqlite3.connect allowed=-rf",
                    "only_in pattern=( allowed=a.py", "no_edge from=src/**", "dependency absent=",
                    "whatever x=1", "only_in calls=a.b allowed=a.py color=red"):
            with pytest.raises(dm.DecisionError):
                dm.parse_guard(bad, repo, "g1")
        with pytest.raises(dm.DecisionError):
            dm.record(st, repo, chosen="", rationale="r")
        a = dm.record(st, repo, chosen="SQLite", rationale="r", guards=["dependency absent=psycopg"])
        with pytest.raises(dm.DecisionError, match="inside the repository"):
            dm.waive(st, repo, a["id"], "g1", at="../elsewhere.py", reason="x")
        with pytest.raises(dm.DecisionError, match="YYYY-MM-DD"):
            dm.waive(st, repo, a["id"], "g1", at="pyproject.toml", reason="x", until="soon")
        # a record edited to say an agent decided is reported and not enforced
        p = repo / a["file"]
        p.write_text(p.read_text(encoding="utf-8").replace("decided-by: human", "decided-by: agent"),
                     encoding="utf-8", newline="\n")
        d = dm.find(repo, a["id"])
        assert not d.enforced and any("always the human" in x for x in d.problems)
        listed = dm.listing(st, repo)["decisions"][0]
        assert "edited by hand" in listed["log"]
        cfg = repo / ".verinoda" / "config.json"
        cfg.write_text(json.dumps({"decisions": {"dir": "../outside"}}), encoding="utf-8")
        with pytest.raises(dm.DecisionError, match="outside the repository"):
            dm.decisions_dir(repo)
        cfg.write_text(json.dumps({"decisions": {"dir": "docs/decisions"}}), encoding="utf-8")
        assert dm.decisions_dir(repo) == (repo / "docs" / "decisions").resolve()
    finally:
        st.close()


def test_cli_and_mcp_record_need_the_users_words(tmp_path, capsys):
    import json

    from verinoda import cli
    from verinoda.mcp.server import AtlasTools

    repo, st = _fresh_orders(tmp_path)
    st.close()
    t = AtlasTools(repo)
    res = t.decision_record("record", chosen="SQLite", rationale="r")
    assert res["error"] == "user_statement_required"
    ok = t.decision_record("record", chosen="SQLite", rationale="r", user_statement="use SQLite",
                           guards=["dependency absent=psycopg"])
    assert ok["id"] == "ADR-0002" and ok["guards"][0]["kind"] == "dependency"
    assert t.decision_record("accept", decision_id="ADR-2", guard_ids=["g7"], user_statement="y")["error"] == \
        "invalid_argument"
    assert t.decision_record("list")["decisions"][0]["id"] == "ADR-0002"
    capsys.readouterr()
    assert cli.main(["decide", "waive", "ADR-2", "g1", "--at", "pyproject.toml", "--reason", "spike",
                     "--repo", str(repo), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["waivers"][0]["at"] == "pyproject.toml"
    assert cli.main(["decide", "guard", "ADR-2", "no_edge from=../x to=y", "--repo", str(repo)]) == 2
    assert "inside the repository" in capsys.readouterr().err
    assert cli.main(["decide", "list", "--repo", str(repo)]) == 0
    assert "ADR-0002 [accepted]" in capsys.readouterr().out
