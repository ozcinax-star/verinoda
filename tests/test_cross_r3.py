"""Where the merged features meet (review of integrate/0925, cross lens): renderers, question routing across
decide/analyze, exclusivity, surface texts, a newer database and the per-file caches under `scan --force`.
Runs on git copies of examples/orders_app (never on examples/ itself)."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import ast  # noqa: E402
import json  # noqa: E402
import shutil  # noqa: E402
import sqlite3  # noqa: E402
import subprocess  # noqa: E402
from collections import Counter  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import analysis, cli, workflow  # noqa: E402
from verinoda import question_plan as qp  # noqa: E402
from verinoda.store import SCHEMA_VERSION, open_store  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ORDERS = ROOT / "examples" / "orders_app"
PT = ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]
REPORTS = ('import sqlite3\n\n\ndef export_rows(path):\n'
           '    return sqlite3.connect(path).execute("SELECT 1").fetchall()\n')

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false",
                           "-c", "commit.gpgsign=false", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout


def _copy(dst: Path, extra: dict[str, str] | None = None) -> Path:
    shutil.copytree(ORDERS, dst, ignore=shutil.ignore_patterns(".verinoda", "__pycache__", "*.pyc", ".pytest_cache",
                                                               "*.db"))
    for rel, text in (extra or {}).items():
        (dst / rel).parent.mkdir(parents=True, exist_ok=True)
        (dst / rel).write_bytes(text.encode("utf-8"))
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
def orders(tmp_path_factory):
    repo = _copy(tmp_path_factory.mktemp("cross") / "orders_app")
    st = open_store(repo)
    yield repo, st
    st.close()


@pytest.fixture(scope="module")
def orders_reports(tmp_path_factory):
    repo = _copy(tmp_path_factory.mktemp("cross_rep") / "orders_app", {"orders/reports.py": REPORTS})
    st = open_store(repo)
    yield repo, st
    st.close()


# -- a merge must not shadow a definition --------------------------------------------------------------

def test_no_top_level_name_is_defined_twice():
    """The merge left two `def _r_check` in cli.py: the later one silently replaced `decide check`'s renderer."""
    dup = {}
    for base in (ROOT / "verinoda", ROOT / "tests"):
        for p in sorted(base.rglob("*.py")):
            rel = p.relative_to(ROOT).as_posix()
            if rel.startswith(("verinoda/project_index/", "tests/fixtures/")):  # vendored / fixture code
                continue
            tree = ast.parse(p.read_text(encoding="utf-8"))
            names = Counter(n.name for n in tree.body
                            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))
            dup.update({f"{rel}::{k}": v for k, v in names.items() if v > 1})
    assert not dup, dup


@needs_git
def test_decide_check_in_text_mode_names_the_violation(tmp_path, capsys):
    from verinoda import decisions as dm

    repo = _copy(tmp_path / "orders")
    st = open_store(repo)
    try:
        dm.record(st, repo, chosen="pricing stays pure", rationale="r",
                  guards=["only_in calls=sqlite3.connect allowed=orders/repository.py"])
    finally:
        st.close()
    capsys.readouterr()
    assert cli.main(["decide", "check", "--repo", str(repo)]) == 0
    assert "decide check: 0 violated" in capsys.readouterr().out
    (repo / "orders" / "reports.py").write_bytes(REPORTS.encode("utf-8"))
    assert cli.main(["decide", "check", "--repo", str(repo)]) == 1
    cap = capsys.readouterr()
    assert "decide check: 1 violated" in cap.out and "orders/reports.py:5" in cap.out and "error" not in cap.err


# -- debug rerun in text mode ------------------------------------------------------------------------------

def test_the_strategy_renderer_takes_a_rerun_count(capsys):
    cli._r_debug_strategy({"strategy": "rerun", "runs": 2, "passed": 1, "pass_rate": 0.5,
                           "conclusion": "flaky: this tree passed 1 of 2 runs Verinoda made (1 of 2 in this series)"})
    out = capsys.readouterr().out
    assert "rerun: flaky" in out and "pass rate 0.5 (1/2)" in out


@needs_git
def test_debug_rerun_in_text_mode_exits_3_on_a_flaky_tree(tmp_path, capsys):
    counter = (tmp_path / "count.txt").as_posix()
    flip = (f"import pathlib\n\n\ndef test_flip():\n    p = pathlib.Path({counter!r})\n"
            "    n = int(p.read_text()) if p.exists() else 0\n    p.write_text(str(n + 1))\n    assert n % 2 == 0\n")
    repo = _copy(tmp_path / "orders", {"tests/test_flip.py": flip})
    r = str(repo)
    assert cli.main(["debug", "start", "sometimes fails", "--repo", r, "--json", "--", *PT,
                     "tests/test_flip.py"]) == 0
    capsys.readouterr()
    assert cli.main(["debug", "rerun", "--times", "2", "--repo", r]) == 3  # a flaky tree: not settled
    out = capsys.readouterr().out
    assert "rerun: flaky" in out and "pass rate 0.5 (1/2)" in out


# -- decisions and code questions in one message -------------------------------------------------------

@pytest.mark.parametrize("question,intents", [
    ("Siparişler için SQLite yeterli mi, place_order siparişi nasıl kaydediyor?", ["decide", "dataflow"]),
    ("place_order siparişi nasıl kaydediyor, SQLite yeterli mi?", ["dataflow", "decide"]),
    ("PostgreSQL'e geçmeli miyiz, validate_items nereden çağrılıyor?", ["decide", "callers"]),
])
def test_a_turkish_comma_splits_a_decision_from_a_code_question(question, intents):
    clauses = qp.segment(question)
    assert [qp.clause_cues(c["text"])[0]["intent"] for c in clauses] == intents


@pytest.mark.parametrize("question", [
    "SQLite yeterli mi, yoksa PostgreSQL mi?",               # one choice between two options
    "place_order, validate_items ve fetch_order nerede tanımlı?",  # a list of names
    "Trafik artarsa, SQLite yeterli mi?",                     # a condition
    "Sipariş kaydı, veritabanına nasıl yazılıyor?",           # a noun phrase before the comma
    "Sence Fabric'e mi geçelim, NeoForge'da mı kalalım?",     # the two options of one choice
    "NeoForge yerine Fabric'e geçmek bize ne kazandırır, geçmeli miyiz?",  # what it gains: part of the choice
])
def test_a_turkish_comma_that_does_not_separate_two_questions_stays(question):
    assert len(qp.segment(question)) == 1


@needs_git
def test_the_code_part_of_a_turkish_decision_message_is_answered(orders):
    repo, st = orders
    res = analysis.analyze(st, repo, "PostgreSQL'e geçmeli miyiz, validate_items nereden çağrılıyor?",
                           challenge=False)
    subs = {s["intent"]: s for s in res["subquestions"]}
    assert subs["decide"]["status"] == qp.HUMAN_DECISION
    assert subs["callers"]["status"] == "met" and subs["callers"]["claim_ids"]
    assert not any("PostgreSQL" in u["why"] for s in res["subquestions"] for u in s["unknowns"])


@needs_git
def test_a_code_name_that_does_not_exist_in_a_decision_is_said(orders):
    repo, st = orders
    for q in ("Should we keep OrderCache.lookup_fast or replace it with Redis?",
              "OrderCache.lookup_fast mi kullanalım yoksa Redis mi?"):
        res = analysis.analyze(st, repo, q, challenge=False)
        s = res["subquestions"][0]
        assert s["intent"] == "decide" and s["status"] == qp.HUMAN_DECISION
        assert any("no symbol named `OrderCache.lookup_fast`" in u["why"] for u in s["unknowns"]), s["unknowns"]
        # SQLite is the project's, not an option the user named
        assert "Redis" in s["decision_brief"]["understood_as"]
        assert ("the project uses: SQLite" in s["decision_brief"]["understood_as"]
                or "projede kullanılan: SQLite" in s["decision_brief"]["understood_as"])
    # an option the code does not have is still no unknown
    res = analysis.analyze(st, repo, "Should we move orders from SQLite to PostgreSQL?", challenge=False)
    assert not any("no symbol named" in u["why"] for u in res["subquestions"][0]["unknowns"])


@needs_git
def test_a_code_question_inside_a_decision_clause_is_not_dropped(orders):
    repo, st = orders
    res = analysis.analyze(st, repo, "Siparişler nasıl kaydediliyor, bu bizim için yeterli mi?", challenge=False)
    decide = [s for s in res["subquestions"] if s["intent"] == "decide"]
    assert decide and decide[0]["status"] == qp.HUMAN_DECISION
    for q, word in (("Siparişleri nasıl kaydettiği bizim için yeterli mi?", "nasıl"),
                    ("How does place_order save an order, is SQLite enough for us?", "how does")):
        s = analysis.analyze(st, repo, q, challenge=False)["subquestions"][0]
        assert s["intent"] == "decide"
        assert any("also asks what the code does" in u["why"] and word in u["why"].lower()
                   for u in s["unknowns"]), q
    # "how do we grow it" is the decision itself, not a question about the code
    for q in ("Sipariş sayısı artarsa bu sistemi nasıl büyütürüz, hangi veritabanını seçmeliyiz?",
              "What is the right choice for storing orders?"):
        res = analysis.analyze(st, repo, q, challenge=False)
        assert not any("also asks what the code does" in u["why"] for s in res["subquestions"]
                       for u in s["unknowns"]), q


def test_yeterli_mi_about_a_name_written_as_code_is_no_decision():
    for q in ("validate_items boş siparişi reddetmek için yeterli mi?",
              "MAX_ITEMS_PER_ORDER değeri 50 yeterli mi?"):
        assert "decide" not in qp.intents_for(q) and not qp.asks_for_choice(q)
        assert qp.may_ask_for_choice(q) == "yeterli"  # a note, as for "is validate_items enough to ..."
    for q in ("Siparişler için SQLite yeterli mi?", "Tek bir sunucu yeterli mi?", "Bu kod bizim için yeterli mi?"):
        assert qp.asks_for_choice(q) and "decide" in qp.intents_for(q)


# -- "is X only called in Y?" -------------------------------------------------------------------------

@needs_git
def test_an_exclusivity_question_is_answered_by_the_call_outside(orders_reports):
    repo, st = orders_reports
    for q in ("Is sqlite3.connect only called in repository.py?",
              "sqlite3.connect sadece repository.py içinde mi çağrılıyor?"):
        res = analysis.analyze(st, repo, q, challenge=False)
        s = res["subquestions"][0]
        claims = {c["id"]: c for c in res["claims"]}
        answer = [claims[c] for c in s["answer_claim_ids"]]
        assert s["status"] == "met", (q, s["status"])
        assert answer and all("orders/reports.py:5" in c["text"] and "outside orders/repository.py" in c["text"]
                              for c in answer), [c["text"] for c in answer]
        assert answer[0]["status"] == "statically_verified"


@needs_git
def test_an_exclusivity_that_holds_is_an_inference_not_verified(orders):
    repo, st = orders
    res = analysis.analyze(st, repo, "Is sqlite3.connect only called in repository.py?", challenge=True)
    s = res["subquestions"][0]
    claims = {c["id"]: c for c in res["claims"]}
    answer = [claims[c] for c in s["answer_claim_ids"]]
    assert s["status"] == "met_with_inference"
    assert [c["status"] for c in answer] == ["strong_inference"]
    assert "called only in orders/repository.py" in answer[0]["text"]
    # a project function: the index's call edges
    res = analysis.analyze(st, repo, "Is validate_items only called in api.py?", challenge=False)
    s = res["subquestions"][0]
    claims = {c["id"]: c for c in res["claims"]}
    assert s["status"] == "met" and any("place_order" in claims[c]["text"] for c in s["answer_claim_ids"])


@needs_git
def test_an_exclusivity_that_cannot_be_checked_is_not_met(orders):
    repo, st = orders
    for q in ("Is OrderRepository.save only called in service.py?",   # a method: instances are not resolved
              "Is the database opened only in repository.py?"):         # no call written as code
        res = analysis.analyze(st, repo, q, challenge=False)
        s = res["subquestions"][0]
        assert s["status"] == "not_supported", (q, s["status"])
        assert any("not that it happens nowhere else" in u["why"] for u in s["unknowns"])


def test_where_only_questions_that_are_not_yes_no_are_left_alone():
    assert analysis.asks_exclusive("What is the only module that uses sqlite3?") is None
    assert analysis.asks_exclusive("Does validate_items only check for empty orders?") is None
    assert analysis.asks_exclusive("Is sqlite3.connect only called in repository.py?") == "only"


# -- the surfaces say the same thing ----------------------------------------------------------------------

def test_both_skills_keep_the_code_writing_rules_and_ask_for_the_users_words():
    from verinoda import agents

    for agent in ("claude", "codex"):
        text = agents.render_skill(agent).decode("utf-8")
        assert "use only the name check below" not in text
        assert "the name check, the debug ledger (bug fixes) and\n`decide check --changed` below still apply" in text
        record = text[text.index("verinoda decide record <brief-id>"):][:200]
        assert "--said" in record, agent


def test_mcp_descriptions_list_every_loop_rule_and_decision_action():
    from verinoda import looprules
    from verinoda.mcp import server

    desc = server.DESCRIPTIONS["debug_attempt"]
    for rule in looprules.DEFINITIVE + looprules.HEURISTIC:
        assert rule in desc, rule
    for action in server.DECISION_ACTIONS:
        assert action in server.DESCRIPTIONS["decision_record"], action


# -- a database a newer Verinoda wrote ----------------------------------------------------------------------

def test_a_newer_database_is_a_failed_check_and_an_error_line(tmp_path, capsys):
    from verinoda import doctor
    from verinoda.paths import db_path

    repo = tmp_path / "proj"
    repo.mkdir()
    workflow.init(repo)
    open_store(repo).close()
    conn = sqlite3.connect(db_path(repo))
    conn.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'", (str(SCHEMA_VERSION + 1),))
    conn.commit()
    conn.close()
    res = doctor.run(repo)
    db = [c for c in res["checks"] if c["check"] == "database"]
    assert db and not db[0]["ok"] and "newer than this Verinoda" in db[0]["detail"]
    assert res["project"]["schema_version"] == SCHEMA_VERSION + 1 and not res["ok"]
    capsys.readouterr()
    assert cli.main(["claim", "list", "--repo", str(repo)]) == 2
    err = capsys.readouterr().err
    assert err.startswith("error: atlas.db schema v") and "Traceback" not in err


# -- `scan --force` rebuilds the per-file caches -------------------------------------------------------------

@needs_git
def test_scan_force_does_not_reuse_a_kept_python_fact(tmp_path):
    from verinoda.paths import graph_path, index_dir

    repo = _copy(tmp_path / "orders")

    def imports_out_of_service() -> int:
        g = json.loads(graph_path(repo).read_text(encoding="utf-8"))
        return sum(1 for e in g.get("links", g.get("edges", []))
                   if e.get("relation") in ("imports", "imports_from")
                   and str(e.get("source_file") or "").replace("\\", "/").endswith("orders/service.py"))

    before = imports_out_of_service()
    kept = index_dir(repo) / "python_facts.json"
    data = json.loads(kept.read_text(encoding="utf-8"))
    key = next(k for k in data["files"] if k.replace("\\", "/").endswith("orders/service.py"))
    assert data["files"][key]["imports"]
    data["files"][key]["imports"] = []  # a wrong kept entry, its hash unchanged
    kept.write_text(json.dumps(data), encoding="utf-8")
    st = open_store(repo)
    try:
        workflow.scan(st, repo, force=True)
    finally:
        st.close()
    assert imports_out_of_service() == before
    again = json.loads(kept.read_text(encoding="utf-8"))
    assert again["files"][key]["imports"]
