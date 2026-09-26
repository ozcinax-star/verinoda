"""Decisions (docs/DESIGN.md D33): a choice is the human's. Runs on git copies of the examples (never on
examples/ itself)."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import re  # noqa: E402
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
    # no network in these tests: an external quote not fetched before stays unknown
    (repo / ".verinoda" / "config.json").write_text('{"research": {"network": "off"}}', encoding="utf-8")
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


def test_a_choice_under_another_intent_is_never_met(orders):
    """R2-5: a plan (a host agent's, or a clause the rules read otherwise) that gives a choice another intent
    keeps its facts as context, but the verdict is the human's; words that only may ask for a choice get a
    note, and the verdict is at most met_with_inference (review round 3: "what would you pick" is a decide
    cue now)."""
    repo, st = orders
    plan = qp.draft("What database would you recommend for this app?", None)
    for sq in plan["sub_questions"]:
        sq["intent"], sq["done_when"] = "dataflow", {"kind": "path_found", "min_status": "strong_inference",
                                                     "detail": "a data path"}
    res = analysis.analyze(st, repo, plan["user_message"], plan=plan, challenge=False)
    assert [s["status"] for s in res["subquestions"]] == [qp.HUMAN_DECISION]
    assert res["subquestions"][0]["intent"] == "dataflow" and res["subquestions"][0].get("decision_brief")
    maybe = analysis.analyze(st, repo, "Which cache suits order lookups best, Redis or a dict?", challenge=False)
    assert all(s["status"] not in (qp.HUMAN_DECISION, "met") for s in maybe["subquestions"])
    assert any("may ask for a choice ('best')" in u["why"] for u in maybe["unknowns"])


def test_a_compound_question_keeps_its_code_part_answerable(orders):
    repo, st = orders
    res = analysis.analyze(st, repo, "Why do we use SQLite for orders, and should we move to PostgreSQL?",
                           challenge=False)
    by_intent = {s["intent"]: s["status"] for s in res["subquestions"]}
    assert by_intent.get("decide") == qp.HUMAN_DECISION
    assert by_intent.get("why") in ("met", "met_with_inference"), res["subquestions"]


# -- the decision brief (step 4) --------------------------------------------------------------------

def _locs(f):
    return [e["locator"] for e in f["evidence"]]


@pytest.mark.parametrize("question", [EN_Q, TR_Q])
def test_brief_collects_the_forces_asks_the_human_and_recommends_nothing(orders, question):
    from verinoda import decision_brief as dbr
    from verinoda import index

    repo, st = orders
    b = dbr.brief(repo, question, store=st, graph=index.load(repo))
    assert b["verdict"] == qp.HUMAN_DECISION and not any("recommend" in k for k in b)
    assert b["decision_kinds"][0] == "datastore" and [o["name"] for o in b["options"]] == ["SQLite", "PostgreSQL"]
    assert [o["present_in_project"] for o in b["options"]] == [True, False]
    facts = {f["fact"]: _locs(f) for f in b["forces"]}
    assert any("sqlite3.connect" in t and "orders/repository.py:10" in at for t, at in facts.items())
    assert any("ORDERS_DATABASE_URL" in t and '"orders.db"' in t and at == ["orders/config.py:5"]
               for t, at in facts.items())
    assert any("same repository interface" in t and "docs/adr/0001-sqlite-persistence.md:5-7" in at
               for t, at in facts.items())
    assert any(re.search(r"all \d+ storage sink.*in one file: orders/repository.py", t) for t in facts)
    # one shared instance: the module-level global is set only while it is unset (`if _repo is None`, line 11),
    # which the evidence cites; the sharing is read from that code (strong_inference)
    assert any("`_repo`" in f["fact"] and "only while it is unset" in f["fact"] and f["status"] == "strong_inference"
               and _locs(f) == ["orders/api.py:6", "orders/api.py:9-13", "orders/api.py:11"] for f in b["forces"])
    assert any("':memory:'" in t and at == ["tests/test_service.py:8", "tests/test_service.py:15"]
               for t, at in facts.items())
    absences = " | ".join(a["what"] for a in b["absences"])
    assert "deployment or CI file name" in absences and "no database driver" in absences
    assert all(a["searched"] and a["scope_note"] for a in b["absences"])
    # every cited line re-checks, and nothing is claimed without evidence
    assert all(f["evidence"] and all(dbr.recheck(repo, e) for e in f["evidence"]) for f in b["forces"])
    qs = b["questions_for_human"]
    assert 1 <= len(qs) <= dbr.MAX_QUESTIONS and all(q["text_en"] and q["text_tr"] and q["asked_because"] and
                                                       q["discriminates"] for q in qs)
    assert [q["kind"] for q in qs] == ["concurrency", "volume", "hosting", "operations", "adr_reason"]
    assert not any(re.search(r"which (database|db)\b.*\buse", q["text_en"].lower()) for q in qs)
    assert "zero-ops local development" in qs[-1]["text_en"]
    assert st.get("decision_briefs", b["brief_id"])["result"]["verdict"] == qp.HUMAN_DECISION


def test_a_file_that_bears_on_a_question_is_context_not_an_answer(tmp_path):
    """A compose file with a database image does not say where production runs; a Procfile does not say how
    many processes will write at the growth the user expects: the questions are still asked, with the file."""
    from verinoda import decision_brief as dbr

    repo = _copy(ORDERS, tmp_path / "orders_app", scan=False)
    (repo / "docker-compose.yml").write_text("services:\n  db:\n    image: postgres:16\n", encoding="utf-8")
    (repo / "Procfile").write_text("web: python -m orders.api\n", encoding="utf-8")
    b = dbr.brief(repo, EN_Q, record=False)
    asked = {q["kind"]: q for q in b["questions_for_human"]}
    assert asked["hosting"]["partly_answered_by"] == ["docker-compose.yml"]
    assert asked["concurrency"]["partly_answered_by"] == ["Procfile"]
    assert b["answered_by_code"] == []
    assert any("docker-compose.yml" in f["fact"] for f in b["forces"])
    assert not any("deployment" in a["what"] for a in b["absences"])


def test_brief_reads_code_not_comments_and_claims_only_what_it_saw(tmp_path):
    """R2-8 .. R2-14, R2-20 .. R2-23 (review of p2/decide): a comment is no storage sink and pins no test; a
    global assigned on every call is not a shared instance; 'rds' inside 'records' is no database service;
    the churn window counts only the last 50 commits; a context sentence of an ADR is not its reason; an
    argument that names no option is not put under the first option."""
    from verinoda import decision_brief as dbr

    repo = _copy(ORDERS, tmp_path / "orders_app", scan=False)
    _write(repo, "orders/pricing.py", '"""Prices."""\n\n\ndef price(x):\n    # never call sqlite3.connect( here: '
                                      'storage goes through OrderRepository\n    return x\n')
    _write(repo, "tests/test_pricing.py", "# TODO: one day also run the service tests against postgres\n"
                                          "def test_price():\n    assert 1\n")
    _write(repo, "docker-compose.yml", "services:\n  web:\n    build: .\n    volumes:\n      - ./records:/data\n")
    _write(repo, ".travis.yml", "language: python\ndeploy:\n  provider: heroku\n")
    # a configured reference tree: its tests are not this project's tests
    _write(repo, "ref/tests/test_other.py", "import sqlite3\n\n\ndef test_x():\n    sqlite3.connect('other.db')\n")
    _write(repo, ".verinoda/config.json", '{"index": {"reference": ["ref"]}}')
    api = repo / "orders/api.py"
    api.write_bytes(api.read_bytes().replace(b"    if _repo is None:\n        _repo = OrderRepository()\n",
                                             b"    _repo = OrderRepository()\n"))
    _write(repo, "docs/adr/0001-sqlite-persistence.md",
           "# ADR 0001: SQLite\n\nStatus: accepted\n\nOrders were lost in the old CSV export because two writers "
           "appended to the same file at once.\n\nThe team met twice about it.\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "case")
    for i in range(51):  # repository.py changed in none of the last 50 commits
        _write(repo, "CHANGES.txt", f"{i}\n")
        _git(repo, "add", "CHANGES.txt")
        _git(repo, "commit", "-q", "-m", f"c{i}")
    b = dbr.brief(repo, EN_Q, record=False, agent_arguments=["PostgreSQL handles many writers",
                                                             "PostgreSQL: MVCC"])
    facts = [f["fact"] for f in b["forces"]]
    assert any(re.search(r"all \d+ storage sink.*in one file: orders/repository.py", t) for t in facts)
    assert not any("orders/pricing.py" in e["locator"] for f in b["forces"] for e in f["evidence"])
    assert not any("postgres" in t for t in facts if t.startswith("test code"))
    assert not any("shares" in t or "share that instance" in t for t in facts)
    assert any("get_repo() assigns the module-level `_repo`" in t for t in facts)
    assert any(t == "orders/repository.py changed in 0 of the last 50 commit(s)" for t in facts)
    assert all(dbr.recheck(repo, e) for f in b["forces"] for e in f["evidence"])
    assert not any("ref/" in e["locator"] for f in b["forces"] for e in f["evidence"])  # R2-10
    assert "deployment/CI file present: .travis.yml" in facts  # R2-20
    assert any(t.startswith("numeric setting ORDERS_DISCOUNT_THRESHOLD") for t in facts)  # R2-22
    asked = {q["kind"]: q for q in b["questions_for_human"]}
    assert asked["concurrency"]["asked_because"].startswith("how many writers you expect is not in the code")
    assert "hosting" in asked and not asked["hosting"].get("partly_answered_by")
    assert "adr_reason" not in asked and all(not d.get("reason") for d in b["existing_decisions"])
    opts = {o["name"]: o for o in b["options"]}
    assert not opts["SQLite"]["agent_arguments"]
    assert [a["text"] for a in opts["PostgreSQL"]["agent_arguments"]] == ["MVCC"]
    assert [a["text"] for a in b["not_tied_to_an_option"]["agent_arguments"]] == ["PostgreSQL handles many writers"]


def test_brief_on_a_jvm_project_and_an_sdk_dependency(templates, tmp_path):
    """R2-12, R2-13: a JDBC connection and a Maven/Gradle driver count for the options and the storage absence;
    boto3 declared for another AWS service does not make DynamoDB present."""
    from verinoda import decision_brief as dbr

    repo = tmp_path / "forge"
    shutil.copytree(templates["forge"], repo)
    _write(repo, "src/main/java/net/ashvale/emberforge/util/ForgeStore.java",
           "package net.ashvale.emberforge.util;\n\nimport java.sql.Connection;\nimport java.sql.DriverManager;\n\n"
           "public final class ForgeStore {\n    public static Connection open(String url) throws Exception {\n"
           "        return DriverManager.getConnection(url);\n    }\n}\n")
    gradle = repo / "build.gradle"
    gradle.write_bytes(gradle.read_bytes().replace(b"dependencies {", b'dependencies {\n    implementation '
                                                                        b'"org.postgresql:postgresql:42.7.3"', 1))
    b = dbr.brief(repo, "Should we move the forge statistics from PostgreSQL to SQLite?", record=False)
    assert not any("no storage sink" in a["what"] for a in b["absences"])
    opts = {o["name"]: o for o in b["options"]}
    assert opts["PostgreSQL"]["present_in_project"] is True
    assert not any("psycopg" in c["fact"] for o in b["options"] for c in o["constraints"])
    orders = _copy(ORDERS, tmp_path / "orders_boto", scan=False)
    _write(orders, "orders/ai.py", "import boto3\n\n\ndef ask(q):\n    return boto3.client('bedrock-runtime')\n")
    b = dbr.brief(orders, EN_Q, record=False)
    assert "DynamoDB" not in [o["name"] for o in b["options"]]
    _write(orders, "orders/table.py", "import boto3\n\n\ndef t():\n    return boto3.resource('dynamodb')\n")
    b = dbr.brief(orders, "Should we keep SQLite or use DynamoDB?", record=False)
    assert {o["name"]: o["present_in_project"] for o in b["options"]}["DynamoDB"] is True


def test_brief_answers_quotes_and_the_record_that_uses_them(orders, tmp_path, capsys):
    import json

    from verinoda import cli
    from verinoda import decision_brief as dbr
    from verinoda import decisions as dm
    from verinoda.store import now

    repo, st = orders
    page = tmp_path / "pg.txt"
    page.write_text("PostgreSQL supports many concurrent writers through MVCC.", encoding="utf-8")
    url = "https://example.org/pg-concurrency"
    st.insert("research", {"id": "res_test0000001", "reference": url, "kind": "document", "local_path": str(page),
                           "content_hash": "sha256:x", "status": "ok", "notes": {}, "created_at": now()})
    b = dbr.brief(repo, EN_Q, store=st,
                  quotes=[{"url": url, "text": "supports many concurrent writers", "option": "PostgreSQL"},
                          {"url": url, "text": "is always faster than SQLite", "option": "PostgreSQL"},
                          {"url": "https://example.org/never-fetched", "text": "x"}],
                  agent_arguments=["PostgreSQL is the usual choice for many writers"])
    pg = next(o for o in b["options"] if o["name"] == "PostgreSQL")
    assert [p["quote_found"] for p in pg["external"]] == [True, False]
    assert pg["external"][0]["status"] == "primary_source_verified" and "not that it applies" in pg["external"][0]["why"]
    # a quote and an argument that name no option are kept on their own, not put under the first option (R2-23)
    assert not next(o for o in b["options"] if o["name"] == "SQLite")["external"]
    loose = b["not_tied_to_an_option"]
    assert loose["external"][0]["status"] == "unknown"  # not fetched: cache, then network per config (off here)
    assert [a["status"] for a in loose["agent_arguments"]] == ["weak_inference"]
    assert not any(o["agent_arguments"] for o in b["options"])
    with pytest.raises(ValueError, match="no question q9"):
        dbr.answer(st, b["brief_id"], "q9", "x")
    res = dbr.answer(st, b["brief_id"], "q1", "two app servers, maybe four next year")
    assert res["answers"][0]["question_id"] == "q1" and "q1" not in [q["id"] for q in res["open_questions"]]
    rows = st.all("SELECT answered_by FROM decision_answers WHERE brief_id = ?", (b["brief_id"],))
    assert [r["answered_by"] for r in rows] == ["user"]
    rec = dm.record(st, repo, chosen="PostgreSQL", rationale="two servers write", brief_id=b["brief_id"])
    body = (repo / rec["file"]).read_text(encoding="utf-8")
    assert "[answered by the user] q1: two app servers" in body and "sqlite3.connect" in body
    capsys.readouterr()
    assert cli.main(["decide", "answer", b["brief_id"], "--q", "q2", "about 5000 a day, kept 7 years",
                     "--repo", str(repo), "--json"]) == 0
    assert [a["question_id"] for a in json.loads(capsys.readouterr().out)["answers"]] == ["q1", "q2"]
    assert cli.main(["decide", "brief", TR_Q, "--repo", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "human_decision_required" in out and "Nerede çalışacak" in out and "recommend" not in out.lower()


def test_analyze_routes_a_decision_to_the_brief_and_mcp_offers_it(orders):
    from verinoda.mcp.server import AtlasTools

    repo, st = orders
    res = analysis.analyze(st, repo, "Sipariş sayısı artarsa bu sistemi nasıl büyütürüz, hangi veritabanını "
                                     "seçmeliyiz?", challenge=False)
    q1, q2 = res["subquestions"]
    assert q1["status"] == q2["status"] == qp.HUMAN_DECISION
    b = q1["decision_brief"]
    assert b["forces"] and b["questions_for_human"] and b["questions_for_human"][0]["text"].startswith("Şu an")
    assert q2["decision_brief"]["brief_id"] == b["brief_id"]  # one brief per message
    assert any(b["brief_id"] in u["why"] for u in res["unknowns"])
    t = AtlasTools(repo)
    m = t.decision_brief(EN_Q)
    assert m["verdict"] == qp.HUMAN_DECISION and m["questions_for_human"]
    assert t.decision_record("answer", brief_id=m["brief_id"], question_id="q1")["error"] == "user_statement_required"
    ok = t.decision_record("answer", brief_id=m["brief_id"], question_id="q1", user_statement="one process")
    assert ok["answers"][0]["answer"] == "one process"


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
                                          "spec": "dependency_added=psycopg", "baseline": False}]
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


# -- guards and `decide check` (step 3) --------------------------------------------------------------

GLOW = ROOT / "examples" / "glow_mod"
FORGE = ROOT / "examples" / "forge_mod"
NET = "src/main/java/net/ashvale/emberforge/network/EmberNetwork.java"
EVENTS = "src/main/java/net/ashvale/emberforge/event/ModEvents.java"


@pytest.fixture(scope="module")
def templates(tmp_path_factory):
    base = tmp_path_factory.mktemp("guard_tpl")
    return {name: _copy(src, base / name) for name, src in (("orders", ORDERS), ("glow", GLOW), ("forge", FORGE))}


def _case(templates, tmp_path, name: str, guards: list[str], **kw):
    from verinoda import decisions as dm

    repo = tmp_path / name
    shutil.copytree(templates[name], repo)
    st = open_store(repo)
    try:
        rec = dm.record(st, repo, chosen="c", rationale="r", guards=guards, **kw)
    finally:
        st.close()
    return repo, rec


def _write(repo: Path, rel: str, text: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8"))


def _check(repo: Path, **kw):
    from verinoda import guards, index, workflow

    st = open_store(repo)
    try:
        workflow.update(st, repo)
    finally:
        st.close()
    return guards.check(repo, graph=index.load(repo), **kw)


def _at(items):
    return sorted(f["at"] for f in items)


def test_only_in_python_catches_aliases_and_ignores_comments_strings_and_tests(templates, tmp_path):
    repo, _ = _case(templates, tmp_path, "orders", ["only_in calls=sqlite3.connect allowed=orders/repository.py"])
    clean = _check(repo)
    assert clean["exit"] == 0 and not clean["violations"] and not clean["possible"]
    (ok,) = clean["ok"]
    assert ok["scope"]["python"] >= 1 and any("getattr" in lim for lim in ok["limits"])
    assert any("test file" in lim for lim in ok["limits"])
    _write(repo, "orders/reports.py",
           '"""Unlike sqlite3.connect(...), this module reads a cache.\n\nsqlite3.connect is not used."""\n'
           "import sqlite3\nimport sqlite3 as sq\nfrom sqlite3 import connect as open_db\n"
           "from orders.config import DATABASE_URL\n\n\n"
           "def daily_total():\n    # sqlite3.connect(DATABASE_URL) would be wrong here\n"
           "    a = sqlite3.connect(DATABASE_URL)\n    b = sq.connect(DATABASE_URL)\n    c = open_db(DATABASE_URL)\n"
           "    d = getattr(sqlite3, 'connect')(DATABASE_URL)\n    msg = 'sqlite3.connect(x)'\n"
           "    return a, b, c, d, msg\n")
    _write(repo, "tests/test_reports.py", "import sqlite3\n\n\ndef test_x():\n    sqlite3.connect(':memory:')\n")
    res = _check(repo)
    assert _at(res["violations"]) == ["orders/reports.py:12", "orders/reports.py:13", "orders/reports.py:14"]
    assert all(v["status"] == "statically_verified" for v in res["violations"])
    assert "as `sq.connect`" in res["violations"][1]["why"] and "as `open_db`" in res["violations"][2]["why"]
    assert _at(res["possible"]) == ["orders/reports.py:15"] and "getattr" in res["possible"][0]["why"]
    assert res["exit"] == 1 and "never edit or supersede" in res["next_step"]


def test_only_in_follows_a_re_export_and_grades_a_shadowing_parameter_possible(templates, tmp_path):
    repo, _ = _case(templates, tmp_path, "orders", ["only_in sink=db-connection allowed=orders/repository.py"])
    _write(repo, "orders/dbutil.py", "from sqlite3 import connect as open_db\n")
    _write(repo, "orders/reports.py", "import sqlite3\n\nfrom .dbutil import open_db\n\n\ndef d(u):\n"
                                      "    return open_db(u)\n\n\ndef e(sqlite3):\n    return sqlite3.connect('x')\n")
    res = _check(repo)
    assert _at(res["violations"]) == ["orders/reports.py:7"] and "through orders.dbutil.open_db" in \
        res["violations"][0]["why"]
    # the parameter shadows the imported module: not verified
    assert _at(res["possible"]) == ["orders/reports.py:11"] and "parameter" in res["possible"][0]["why"]


def test_only_in_never_verifies_a_name_a_def_shadows(tmp_path):
    from verinoda import guards

    repo = tmp_path / "r"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "a.py").write_text("from sqlite3 import connect\n\n\ndef connect(u):\n    return u\n\n\n"
                                       "def d():\n    return connect(1)\n", encoding="utf-8")
    (repo / "pkg" / "b.py").write_text("import sqlite3\n\n\ndef e(u):\n    return sqlite3.connect(u)\n",
                                       encoding="utf-8")
    ctx = guards._Ctx(repo, ["pkg/a.py", "pkg/b.py"])
    hits, scan, _ = guards.check_only_in(ctx, {"kind": "only_in", "calls": ["sqlite3.connect"], "allowed": []})
    assert [(h[0], h[1], h[2]) for h in hits] == [("VIOLATED", "pkg/b.py", 5)]
    assert scan.files["python"] == 2


def test_no_edge_on_glow_mod_import_is_violated_inferred_call_possible_comment_nothing(templates, tmp_path):
    repo, _ = _case(templates, tmp_path, "glow", ["no_edge from=src/main/** to=src/client/**"])
    clean = _check(repo)
    # an ok says what it looked at: the files each side matches and the edges out of the `from` files
    scope = clean["ok"][0]["scope"]
    assert clean["exit"] == 0 and scope["edges_matched"] == 0 and scope["from_files"] > 5 and \
        scope["to_files"] >= 1 and scope["edges_checked"] > 0
    assert any("string class loading" in lim for lim in clean["ok"][0]["limits"])
    gm = repo / "src/main/java/com/example/glowmod/GlowMod.java"
    t = gm.read_text(encoding="utf-8").replace(
        "import com.example.glowmod.command.GlowCommands;",
        "import com.example.glowmod.client.GlowModClient;\nimport com.example.glowmod.command.GlowCommands;")
    t = t.replace("        LOGGER.info", "        // GlowModClient is client only\n"
                                         "        new GlowModClient().onInitializeClient();\n        LOGGER.info")
    gm.write_bytes(t.encode("utf-8"))
    res = _check(repo)
    main = "src/main/java/com/example/glowmod/GlowMod.java"
    assert _at(res["violations"]) == [f"{main}:3"] and "EXTRACTED" in res["violations"][0]["why"]
    assert all(p["at"].startswith(main) and "INFERRED" in p["why"] for p in res["possible"])
    lines = gm.read_text(encoding="utf-8").split("\n")
    assert not any("//" in lines[int(p["at"].rpartition(":")[2]) - 1] for p in res["possible"])  # not the comment


def test_only_in_java_and_kotlin_bind_calls_through_imports_and_declared_types(templates, tmp_path):
    repo, _ = _case(templates, tmp_path, "forge",
                    [f"only_in calls=PayloadRegistrar.playToServer,PayloadRegistrar.playToClient allowed={NET}",
                     "no_edge from=src/main/java/** to=src/main/kotlin/net/ashvale/emberforge/client/**"])
    clean = _check(repo)
    assert clean["exit"] == 0 and len(clean["ok"]) == 2
    ev = repo / EVENTS
    t = ev.read_text(encoding="utf-8").replace(
        "import net.neoforged.neoforge.event.RegisterCommandsEvent;",
        "import net.neoforged.neoforge.event.RegisterCommandsEvent;\n"
        "import net.neoforged.neoforge.network.event.RegisterPayloadHandlersEvent;\n"
        "import net.neoforged.neoforge.network.registration.PayloadRegistrar;\n"
        "import net.ashvale.emberforge.client.EmberForgeScreen;")
    t = t.replace("    /** Punching", "    @SubscribeEvent\n    public static void onPayloads(RegisterPayloadHandlersEvent e) {\n"
                  "        PayloadRegistrar registrar = e.registrar(\"1\");\n"
                  "        registrar.playToClient(A.TYPE, A.CODEC, ModEvents::h); // registrar.playToServer(x)\n"
                  "        e.registrar(\"2\").playToServer(B.TYPE, B.CODEC, ModEvents::h);\n    }\n\n    /** Punching")
    ev.write_bytes(t.encode("utf-8"))
    _write(repo, "src/main/kotlin/net/ashvale/emberforge/heat/Wire.kt",
           "package net.ashvale.emberforge.heat\n\nimport net.neoforged.neoforge.network.registration.PayloadRegistrar\n"
           "\nfun wire(r: PayloadRegistrar) {\n    r.playToServer(T, C)\n}\n")
    res = _check(repo)
    viol = _at(res["violations"])
    assert f"{EVENTS}:31" in viol and "src/main/kotlin/net/ashvale/emberforge/heat/Wire.kt:6" in viol
    assert f"{EVENTS}:15" in viol  # the Java import of the Kotlin client screen (a file node "X.kt")
    assert len(viol) == 3
    assert f"{EVENTS}:32" in _at(res["possible"])  # a call chain: the receiver's type is not resolved


def test_governs_is_a_review_revisit_a_trigger_and_dependencies_are_read(templates, tmp_path):
    repo, rec = _case(templates, tmp_path, "orders", ["dependency absent=psycopg"],
                      governs=["orders/repository.py::OrderRepository.__init__"],
                      revisit_when=["dependency_added=psycopg", "file_appears=docker-compose*.yml"])
    clean = _check(repo)
    assert clean["exit"] == 0 and not clean["reviews"] and not clean["triggers"]
    p = repo / "orders/repository.py"
    p.write_bytes(p.read_bytes().replace(b"sqlite3.connect(url)", b"sqlite3.connect(url, check_same_thread=False)"))
    pp = repo / "pyproject.toml"
    pp.write_bytes(pp.read_bytes().replace(b'requires-python = ">=3.10"',
                                           b'requires-python = ">=3.10"\ndependencies = ["psycopg>=3.1"]'))
    _write(repo, "docker-compose.yml", "services: {}\n")
    res = _check(repo)
    assert [r["kind"] for r in res["reviews"]] == ["governs"] and "changed since the decision" in res["reviews"][0]["why"]
    assert not any(v["kind"] == "governs" for v in res["violations"])  # a review, never a violation
    assert sorted(t["guard"] for t in res["triggers"]) == ["r1", "r2"]
    assert _at(res["violations"]) == ["pyproject.toml:5"] and res["violations"][0]["kind"] == "dependency"


def test_base_labels_new_and_pre_existing_waivers_and_proposed_guards(templates, tmp_path):
    from verinoda import decisions as dm
    from verinoda import guards

    repo, rec = _case(templates, tmp_path, "orders", ["only_in calls=sqlite3.connect allowed=orders/repository.py"])
    _write(repo, "orders/old.py", "import sqlite3\n\n\ndef f():\n    return sqlite3.connect('a')\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "old violation")
    _write(repo, "orders/new.py", "import sqlite3\n\n\ndef g():\n    return sqlite3.connect('b')\n")
    res = guards.check(repo, changed_only=True)
    assert _at(res["violations"]) == ["orders/new.py:5"] and res["violations"][0]["since"] == "new/touched since HEAD"
    assert _at(res["pre_existing"]) == ["orders/old.py:5"] and res["exit"] == 1
    (repo / "orders/new.py").unlink()
    only_old = guards.check(repo, base="HEAD")
    assert only_old["exit"] == 0 and only_old["pre_existing"] and not only_old["violations"]
    assert guards.check(repo)["exit"] == 1  # without a base every violation counts
    for bad in ("--output=/tmp/x", "-p", "HEAD\nx", "no-such-ref"):
        with pytest.raises(ValueError):
            guards.check(repo, base=bad)
    st = open_store(repo)
    try:
        dm.waive(st, repo, rec["id"], "g1", at="orders/old.py:5", reason="legacy, until the port",
                 until="2999-01-01")
        dm.add_guards(st, repo, rec["id"], ["dependency absent=psycopg"], status="proposed")
    finally:
        st.close()
    res = guards.check(repo)
    assert res["exit"] == 0 and _at(res["waived"]) == ["orders/old.py:5"]
    assert any(n.get("guard") == "g2" and "proposed" in n["why"] for n in res["not_enforced"])


def test_cli_and_mcp_decide_check_exit_codes(templates, tmp_path, capsys):
    import json

    from verinoda import cli
    from verinoda.mcp.server import AtlasTools

    repo, _ = _case(templates, tmp_path, "orders", ["only_in calls=sqlite3.connect allowed=orders/repository.py"])
    assert cli.main(["decide", "check", "--repo", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "ok ADR-0002 g1 only_in" in out and "limit:" in out
    _write(repo, "orders/reports.py", "import sqlite3\n\n\ndef f():\n    return sqlite3.connect('x')\n")
    assert cli.main(["decide", "check", "--repo", str(repo), "--changed", "--json"]) == 1
    res = json.loads(capsys.readouterr().out)
    assert res["violations"][0]["at"] == "orders/reports.py:5"
    assert cli.main(["decide", "check", "--repo", str(repo), "--base=--output=x"]) == 2
    assert "not a git revision" in capsys.readouterr().err
    t = AtlasTools(repo)
    m = t.decision_check(changed_only=True)
    assert m["exit"] == 1 and m["violations"][0]["at"] == "orders/reports.py:5"
    assert t.decision_check(base="-x")["error"] == "invalid_argument"
    capsys.readouterr()
    assert cli.main(["update", str(repo)]) == 0
    assert "decisions: 1 violated, 0 possible, 0 review, 0 trigger" in capsys.readouterr().out
    # R1-9: a no-op update does not run every guard again, and says so
    assert cli.main(["update", str(repo)]) == 0
    assert "decisions: no file changed since the last update, so not checked again" in capsys.readouterr().out


def test_critique_exclusivity_uses_the_engine(templates, tmp_path):
    from verinoda import critique
    from verinoda import evidence as evmod
    from verinoda.claims import Claims

    repo = tmp_path / "orders"
    shutil.copytree(templates["orders"], repo)
    st = open_store(repo)
    try:
        snap = st.latest_snapshot()
        ev = evmod.source_evidence(repo, "orders/repository.py", 10, commit=snap["commit_sha"])
        c = Claims(st, repo).create("Only orders/repository.py opens a database connection", project=snap["project"],
                                    snapshot=snap, status="statically_verified", evidence=[(ev, "supports")],
                                    kind="exclusive", subjects=["orders/repository.py"],
                                    spec={"pattern": r"sqlite3\.connect\(", "allowed_files": ["orders/repository.py"]})
        _write(repo, "orders/notes.py", '"""Never call sqlite3.connect( here."""\n# sqlite3.connect( is not used\n'
                                        "MSG = 'sqlite3.connect('\n")
        ok = critique.challenge(st, repo, c["id"])
        assert {f["check"]: f["result"] for f in ok["findings"]}["exclusivity"] == "pass"
        _write(repo, "orders/audit.py", "import sqlite3 as sq\n\n\ndef log():\n    return sq.connect('audit.db')\n")
        bad = critique.challenge(st, repo, c["id"])
        fail = next(f for f in bad["findings"] if f["check"] == "exclusivity")
        assert fail["result"] == "fail" and "orders/audit.py:5" in fail["detail"]
        assert "orders/notes.py" not in fail["detail"]
    finally:
        st.close()


# -- review of p2/decide: regression tests for the reviewers' findings ---------------------------------

def _only_in(repo: Path, rel: str, text: str, spec: str):
    from verinoda import decisions as dm
    from verinoda import guards

    _write(repo, rel, text)
    ctx = guards._Ctx(repo, [rel])
    hits, scan, _ = guards.check_only_in(ctx, dm.parse_guard(spec, repo, "g1"))
    return [(h[0], h[2]) for h in hits], scan


ORD_G = "only_in calls=sqlite3.connect allowed=orders/repository.py"
SINK_G = "only_in sink=db-connection allowed=orders/repository.py"


@pytest.mark.parametrize("text,spec,want", [
    # R1-1 / R2-3: never a silent ok when the import can reach the call
    ("try:\n    import psycopg2\nexcept ImportError:\n    psycopg2 = None\n\n\ndef r(dsn):\n"
     "    return psycopg2.connect(dsn)\n", SINK_G, [("POSSIBLE", 8)]),
    ("import sqlite3\n\n\ndef r(u):\n    return sqlite3.connect(u)\n\n\ndef f():\n    sqlite3 = None\n"
     "    return sqlite3\n", ORD_G, [("VIOLATED", 5)]),  # a local of another function rebinds nothing here
    ("import os\n\nif os.environ.get('PG'):\n    from psycopg import connect\nelse:\n    from sqlite3 import connect\n"
     "\n\ndef o(d):\n    return connect(d)\n", SINK_G, [("VIOLATED", 10)]),  # every branch binds a connection
    ("import os\n\nif os.environ.get('PG'):\n    from psycopg import connect\nelse:\n    from sqlite3 import connect\n"
     "\n\ndef o(d):\n    return connect(d)\n", ORD_G, [("POSSIBLE", 10)]),  # only one branch binds sqlite3.connect
    ("from sqlite3 import connect\n\n\nclass Archive:\n    def connect(self):\n        return connect('a.db')\n",
     ORD_G, [("VIOLATED", 6)]),  # a method name does not shadow the module name inside the method
    ("def a(u):\n    import sqlite3 as db\n    return db.connect(u)\n\n\ndef b(u):\n    import json as db\n"
     "    return db.loads(u)\n", ORD_G, [("VIOLATED", 3)]),
    ("import sqlite3\n\n\ndef reset():\n    global sqlite3\n    sqlite3 = None\n\n\ndef go(u):\n"
     "    return sqlite3.connect(u)\n", ORD_G, [("POSSIBLE", 10)]),
    # R1-3 / R2-0: a name bound by an enclosing function, a loop, with, except or a comprehension is not verified
    ("from sqlite3 import connect\n\n\ndef with_driver(connect):\n    def open_db(url):\n        return connect(url)\n"
     "    return open_db\n\n\ndef local_default():\n    return connect(':memory:')\n", ORD_G,
     [("POSSIBLE", 6), ("VIOLATED", 11)]),
    ("import sqlite3\nFAKES = []\nCONNS = [sqlite3.connect(':memory:') for sqlite3 in FAKES]\n", ORD_G,
     [("POSSIBLE", 3)]),
    ("import sqlite3\n\nfor sqlite3 in []:\n    sqlite3.connect('x')\n", ORD_G, [("POSSIBLE", 4)]),
    ("import sqlite3\nfrom contextlib import nullcontext\n\nwith nullcontext(object()) as sqlite3:\n    pass\n\n"
     "sqlite3.connect('x')\n", ORD_G, [("POSSIBLE", 7)]),
    ("import sqlite3\n\ntry:\n    pass\nexcept Exception as sqlite3:\n    sqlite3.connect('x')\n", ORD_G,
     [("POSSIBLE", 6)]),
    ("from sqlite3 import connect\n\n\ndef connect(u):\n    return u\n\n\ndef d():\n    return connect(1)\n", ORD_G, []),
    # R2-18: the target used as a value
    ("import functools\nimport sqlite3\n\nopener = functools.partial(sqlite3.connect, 'x.db')\n", ORD_G,
     [("POSSIBLE", 4)]),
    ("import sqlite3\n\n\nclass Repo:\n    opener = sqlite3.connect\n\n    def go(self):\n        return self.opener('x')\n",
     ORD_G, [("POSSIBLE", 5)]),
    ("import sqlite3\n\nopener = sqlite3.connect\n\n\ndef d(u):\n    return opener(u)\n", ORD_G, [("VIOLATED", 7)]),
])
def test_python_only_in_resolves_names_by_scope(tmp_path, text, spec, want):
    got, _scan = _only_in(tmp_path, "pkg/m.py", text, spec)
    assert [(lvl, line) for lvl, line in got] == want, got


def test_java_and_kotlin_receivers_bound_without_a_type_and_import_aliases(templates, tmp_path):
    """R2-1: a lambda parameter or `var` loop variable of another type is not VIOLATED; R2-17: a Kotlin
    import alias binds the class."""
    from verinoda import decisions as dm
    from verinoda import guards

    repo = tmp_path / "forge"
    shutil.copytree(templates["forge"], repo)
    base = "src/main/java/net/ashvale/emberforge/net2/"
    kt = "src/main/kotlin/net/ashvale/emberforge/net2/"
    imp = "import net.neoforged.neoforge.network.registration.PayloadRegistrar"
    files = {
        base + "Helper.java": "package x;\n\n" + imp + ";\nimport java.util.List;\n\nclass Helper {\n"
        "    static String d(PayloadRegistrar registrar) { return String.valueOf(registrar); }\n"
        "    static void f(List<Bus> buses, Object m) {\n        buses.forEach(registrar -> registrar.playToServer(m));\n"
        "    }\n}\n",
        base + "Helper2.java": "package x;\n\n" + imp + ";\nimport java.util.List;\n\nclass Helper2 {\n"
        "    static String d(PayloadRegistrar registrar) { return String.valueOf(registrar); }\n"
        "    static void f(List<Bus> buses, Object m) {\n        for (var registrar : buses) {\n"
        "            registrar.playToServer(m);\n        }\n    }\n}\n",
        kt + "Helper3.kt": "package x\n\n" + imp + "\n\nfun d(registrar: PayloadRegistrar) = registrar.toString()\n\n"
        "fun f(buses: List<Bus>, m: Any) {\n    buses.forEach { registrar -> registrar.playToServer(m) }\n}\n",
        kt + "Reg.kt": "package x\n\n" + imp + " as PR\n\nfun reg(r: PR, p: Any) {\n    r.playToServer(p, p, p)\n}\n",
    }
    for rel, text in files.items():
        _write(repo, rel, text)
    g = dm.parse_guard(f"only_in calls=PayloadRegistrar.playToServer allowed={NET}", repo, "g1")
    hits, scan, _ = guards.check_only_in(guards._Ctx(repo, list(files)), g)
    got = {(h[1].rpartition("/")[2], h[2]): h[0] for h in hits}
    assert got == {("Helper.java", 9): "POSSIBLE", ("Helper2.java", 10): "POSSIBLE", ("Helper3.kt", 8): "POSSIBLE",
                   ("Reg.kt", 6): "VIOLATED"}, got
    assert any("without a written type" in lim for lim in scan.limits)


def test_changed_counts_a_subdirectory_project_non_ascii_paths_and_re_export_chains(tmp_path):
    """R1-0 / R2-2: the project is a subdirectory of its git repository; R1-2: a Turkish file name;
    R2-7: a re-export module changed, the call site did not."""
    from verinoda import decisions as dm
    from verinoda import guards

    root = tmp_path / "mono"
    proj = root / "svc"
    shutil.copytree(ORDERS, proj, ignore=shutil.ignore_patterns(".verinoda", "__pycache__", "*.pyc", "*.db"))
    _write(proj, "orders/rapor_sipariş.py", '"""rapor"""\n')
    _write(proj, "orders/dbutil.py", "def open_db(path):\n    return None\n")
    _write(proj, "orders/reports.py", "from orders.dbutil import open_db\n\n\ndef r(path):\n    return open_db(path)\n")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    workflow.init(proj)
    st = open_store(proj)
    try:
        dm.record(st, proj, chosen="SQLite", rationale="r", guards=[ORD_G])
    finally:
        st.close()
    svc = proj / "orders/service.py"
    svc.write_bytes(svc.read_bytes() + b"\n\nimport sqlite3\n\n\ndef audit():\n    return sqlite3.connect('a.db')\n")
    _write(proj, "orders/rapor_sipariş.py", '"""rapor"""\nimport sqlite3\n\n\ndef r():\n    return sqlite3.connect("r")\n')
    _write(proj, "orders/dbutil.py", "from sqlite3 import connect as open_db\n")
    res = guards.check(proj, changed_only=True)
    since = {v["at"]: v["since"] for v in res["violations"]}
    assert res["exit"] == 1 and not res["pre_existing"], res["pre_existing"]
    assert since["orders/rapor_sipariş.py:6"] == "new/touched since HEAD"
    assert any(at.startswith("orders/service.py:") for at in since)
    assert since["orders/reports.py:5"] == "new/touched since HEAD (through orders/dbutil.py)"
    assert guards.changed_since(proj, guards.validate_ref(proj, "HEAD")) >= {"orders/service.py", "orders/dbutil.py",
                                                                             "orders/rapor_sipariş.py"}


POM = ("<project>\n  <dependencies>\n    <dependency>\n      <groupId>org.postgresql</groupId>\n"
       "      <artifactId>postgresql</artifactId>\n      <version>42.7.3</version>\n    </dependency>\n"
       "  </dependencies>\n</project>\n")


def _rec(repo: Path, spec: str):
    from verinoda import decisions as dm

    return dm.Decision(id="ADR-0009", number=9, title="t", guards=[dm.parse_guard(spec, repo, "g1")])


def test_dependency_guard_reads_the_projects_builds_and_cites_the_named_line(tmp_path):
    """R1-7 / R2-15: a multi-line Maven dependency is VIOLATED at its artifactId line; R2-4: a test fixture's
    build file is not the project's, a build the root does not include is POSSIBLE, a git-ignored one is not
    read; R1-10: one declaration in two optional groups is cited once per line, each in its own group."""
    from verinoda import guards

    def case(name, files, spec):
        repo = _copy(ORDERS, tmp_path / name, scan=False)
        for rel, text in files.items():
            _write(repo, rel, text)
        return guards.check(repo, records=[_rec(repo, spec)])

    res = case("pom", {"pom.xml": POM}, "dependency absent=org.postgresql:postgresql")
    assert [v["at"] for v in res["violations"]] == ["pom.xml:5"] and res["exit"] == 1
    res = case("ignored", {".gitignore": "node_modules/\n", "node_modules/lib/pom.xml": POM},
               "dependency absent=postgresql")
    assert res["exit"] == 0 and not res["possible"] and not res["violations"]
    res = case("fixture", {"tests/fixtures/legacy/build.gradle": 'dependencies {\n    runtimeOnly '
                           '"org.postgresql:postgresql:42.7.3"\n}\n'}, "dependency absent=postgresql")
    assert res["exit"] == 0 and not res["violations"] and not res["possible"]
    assert any("tests/fixtures/legacy/build.gradle" in lim for o in res["ok"] for lim in o["limits"])
    res = case("nested", {"tools/gen/build.gradle": 'dependencies {\n    implementation '
                          '"org.postgresql:postgresql:42.7.3"\n}\n'}, "dependency absent=postgresql")
    assert res["exit"] == 0 and [p["at"] for p in res["possible"]] == ["tools/gen/build.gradle:2"]
    res = case("included", {"settings.gradle": "include ':tools:gen'\n", "build.gradle": "plugins { id 'java' }\n",
                            "tools/gen/build.gradle": 'dependencies {\n    implementation '
                            '"org.postgresql:postgresql:42.7.3"\n}\n'}, "dependency absent=postgresql")
    assert [v["at"] for v in res["violations"]] == ["tools/gen/build.gradle:2"]
    repo = _copy(ORDERS, tmp_path / "optional", scan=False)
    pp = repo / "pyproject.toml"
    pp.write_bytes(pp.read_bytes().replace(b'requires-python = ">=3.10"', b'requires-python = ">=3.10"\n\n'
                                           b'[project.optional-dependencies]\npostgres = [\n    "psycopg[binary]",\n]\n'
                                           b'all = [\n    "rich[jupyter]",\n    "psycopg[binary]",\n]'))
    res = guards.check(repo, records=[_rec(repo, "dependency absent=psycopg")])
    assert sorted(v["at"] for v in res["violations"]) == ["pyproject.toml:12", "pyproject.toml:8"]


def test_conftest_is_test_code(tmp_path):
    """R2-16: pytest fixtures in a root conftest.py are out of the product scope."""
    from verinoda import guards

    repo = _copy(ORDERS, tmp_path / "o", scan=False)
    _write(repo, "conftest.py", "import sqlite3\n\n\ndef db():\n    return sqlite3.connect(':memory:')\n")
    res = guards.check(repo, records=[_rec(repo, ORD_G)])
    assert res["exit"] == 0 and not res["violations"]


def test_guard_specs_keep_backslashes_and_records_survive_hand_edits(tmp_path, capsys):
    """R1-4: a Windows path and a regex keep their backslashes; R1-5: a byte-order mark does not hide a
    record; R1-6: a malformed hand-edited entry makes that record not enforced, the others are checked, and
    an error never exits 1."""
    import json

    from verinoda import cli
    from verinoda import decisions as dm
    from verinoda import guards

    repo = _copy(ORDERS, tmp_path / "o", scan=False)
    workflow.init(repo)
    assert dm.parse_guard(r"only_in calls=sqlite3.connect allowed=orders\repository.py", repo, "g1")["allowed"] == \
        ["orders/repository.py"]
    assert dm.parse_guard(r"only_in pattern=\bexecute\b allowed=a.py", repo, "g1")["pattern"] == r"\bexecute\b"
    assert dm.parse_guard(r"only_in pattern=sqlite3\.connect\( allowed=a.py", repo, "g1")["pattern"] == \
        r"sqlite3\.connect\("
    st = open_store(repo)
    try:
        rec = dm.record(st, repo, chosen="SQLite", rationale="r", guards=[ORD_G])
    finally:
        st.close()
    p = repo / rec["file"]
    p.write_bytes(b"\xef\xbb\xbf" + p.read_bytes())
    _write(repo, "orders/audit.py", "import sqlite3\n\n\ndef a():\n    return sqlite3.connect('a.db')\n")
    assert [d.id for d in dm.load_all(repo)] == [rec["id"]] and guards.check(repo)["exit"] == 1
    d = dm.decisions_dir(repo)
    (d / "ADR-0003-hand.md").write_text('---\nverinoda-decision: 1\nid: ADR-0003\ntitle: h\nstatus: accepted\n'
                                        'decided-by: human\ngoverns: [{"id": "v1", "symbol": "orders/x.py::X"}]\n'
                                        '---\n', encoding="utf-8")
    (d / "ADR-0006-hand.md").write_text('verinoda-decision: 1\nid: ADR-0006\n', encoding="utf-8")
    capsys.readouterr()
    assert cli.main(["decide", "check", "--repo", str(repo), "--json", "--no-refresh"]) == 1  # the real violation
    res = json.loads(capsys.readouterr().out)
    why = {n["decision"]: n["why"] for n in res["not_enforced"]}
    assert "governs entry v1: file, qual missing" in why["ADR-0003"]
    assert "front matter is not readable" in why["ADR-0006"]
    st = open_store(repo)
    try:
        with pytest.raises(dm.DecisionError, match="has problems"):
            dm.add_guards(st, repo, "ADR-3", ["dependency absent=psycopg"])
    finally:
        st.close()


def test_form_feed_does_not_shift_the_comment_mask():
    """R1-11: rows are counted as tokenize counts them."""
    from verinoda import guards

    text = "import os\n\x0c\ndef helper():\n    # never call sqlite3.connect here\n    return os.getcwd()\n"
    assert "sqlite3" not in guards.code_text(text, ".py")


def test_ok_prints_every_limit(templates, tmp_path, capsys):
    """R2-19: the test-exclusion scope note is printed with the other limits."""
    from verinoda import cli

    repo, _ = _case(templates, tmp_path, "orders", [ORD_G])
    _write(repo, "web/app.js", "export function hello() { return 1; }\n")
    capsys.readouterr()
    assert cli.main(["decide", "check", "--repo", str(repo), "--no-refresh"]) == 0
    out = capsys.readouterr().out
    assert "languages other than Python" in out and "test file(s) are out of scope" in out
