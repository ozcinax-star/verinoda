"""Decisions (docs/DESIGN.md D33), review round 3: regression tests for the reviewer's findings. Runs on git
copies of examples/orders_app (never on examples/ itself)."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import ast  # noqa: E402
import collections  # noqa: E402
import json  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import analysis, workflow  # noqa: E402
from verinoda import question_plan as qp  # noqa: E402
from verinoda.store import open_store  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ORDERS = ROOT / "examples" / "orders_app"
ORD_G = "only_in calls=sqlite3.connect allowed=orders/repository.py"
CALL = "import sqlite3\n\n\ndef open_db(p):\n    return sqlite3.connect(p)\n"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false",
                           "-c", "commit.gpgsign=false", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout


def _copy(dst: Path, files: dict[str, str] | None = None) -> Path:
    shutil.copytree(ORDERS, dst, ignore=shutil.ignore_patterns(".verinoda", "__pycache__", "*.pyc", ".pytest_cache",
                                                               "*.db"))
    for rel, text in (files or {}).items():
        _write(dst, rel, text)
    _git(dst, "init", "-q")
    _git(dst, "add", "-A")
    _git(dst, "commit", "-q", "-m", "init")
    workflow.init(dst)
    return dst


def _write(repo: Path, rel: str, text: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8"))


def _record(repo: Path, specs: list[str]) -> dict:
    from verinoda import decisions as dm

    st = open_store(repo)
    try:
        return dm.record(st, repo, chosen="SQLite", rationale="r", guards=specs, user_statement="keep it")
    finally:
        st.close()


def _front_edit(repo: Path, rec: dict, old: str, new: str) -> None:
    p = repo / rec["file"]
    text = p.read_text(encoding="utf-8")
    assert old in text, old
    p.write_bytes(text.replace(old, new).encode("utf-8"))


def _rec(repo: Path, spec: str):
    from verinoda import decisions as dm

    return dm.Decision(id="ADR-0009", number=9, title="t", guards=[dm.parse_guard(spec, repo, "g1")])


@pytest.fixture(scope="module")
def orders(tmp_path_factory):
    repo = _copy(tmp_path_factory.mktemp("decide_r3") / "orders_app")
    (repo / ".verinoda" / "config.json").write_text('{"research": {"network": "off"}}', encoding="utf-8")
    st = open_store(repo)
    workflow.scan(st, repo)
    yield repo, st
    st.close()


# -- 1: one renderer per name (the merge had left two `def _r_check` in cli.py) ---------------------------

def test_no_module_defines_a_top_level_name_twice():
    dups = {}
    for p in sorted((ROOT / "verinoda").rglob("*.py")):
        if "project_index" in p.parts:  # vendored code, never edited by hand
            continue
        tree = ast.parse(p.read_text(encoding="utf-8"))
        names = collections.Counter(n.name for n in tree.body
                                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))
        if any(v > 1 for v in names.values()):
            dups[p.relative_to(ROOT).as_posix()] = [k for k, v in names.items() if v > 1]
    assert not dups, dups


# -- 2: a commented-out build dependency is not declared ------------------------------------------------

GRADLE = ("plugins {\n    id 'java'\n}\n\ndependencies {\n    implementation 'org.xerial:sqlite-jdbc:3.45.0.0'\n"
          "    // implementation 'org.postgresql:postgresql:42.7.3'   (dropped)\n    /*\n"
          "    runtimeOnly \"org.postgresql:postgresql:42.7.3\"\n    */\n"
          "    implementation \"com.example:client:1.0\" // was org.postgresql:postgresql\n}\n")
POM = ("<project>\n  <dependencies>\n    <!-- removed in 2025:\n    <dependency>\n"
       "      <groupId>org.postgresql</groupId>\n      <artifactId>postgresql</artifactId>\n"
       "      <version>42.7.3</version>\n    </dependency>\n    -->\n    <dependency>\n"
       "      <groupId>org.xerial</groupId>\n      <artifactId>sqlite-jdbc</artifactId>\n"
       "      <version>3.45.0.0</version>\n    </dependency>\n  </dependencies>\n</project>\n")


@pytest.mark.parametrize("files", [{"build.gradle": GRADLE}, {"pom.xml": POM}])
def test_a_commented_out_dependency_is_not_declared(tmp_path, files):
    from verinoda import decision_brief as dbr
    from verinoda import guards

    repo = _copy(tmp_path / "o", files)
    res = guards.check(repo, records=[_rec(repo, "dependency absent=postgresql")])
    assert res["exit"] == 0 and not res["violations"] and not res["possible"], res["violations"]
    assert res["status"] == "ok"
    # the declared one is still read
    res = guards.check(repo, records=[_rec(repo, "dependency absent=sqlite-jdbc")])
    assert res["exit"] == 1 and len(res["violations"]) == 1
    b = dbr.brief(repo, "Should we store orders in PostgreSQL or SQLite?", store=None, record=False)
    assert not any("postgresql" in f["fact"].lower() and f["probe"] == "P3" for f in b["forces"]), b["forces"]
    assert {o["name"]: o["present_in_project"] for o in b["options"]}["PostgreSQL"] is False


# -- 3 / 11: option presence reads imports and connections from the code -------------------------------

def test_brief_presence_reads_every_import_the_connection_and_never_a_docstring(tmp_path):
    from verinoda import decision_brief as dbr

    q = "Should we move orders from SQLite to PostgreSQL?"
    multi = ('"""Persistence."""\n\nimport os, sqlite3\n\n\nclass OrderRepository:\n    def __init__(self, url=None):\n'
             '        self.conn = sqlite3.connect(url or os.environ.get("ORDERS_DATABASE_URL", "orders.db"))\n')
    repo = _copy(tmp_path / "multi", {"orders/repository.py": multi})
    b = dbr.brief(repo, q, store=None, record=False)
    opts = {o["name"]: o for o in b["options"]}
    assert opts["SQLite"]["present_in_project"] is True
    assert {e["locator"] for e in opts["SQLite"]["presence_evidence"]} >= {"orders/repository.py:3"}

    sqla = ('"""Persistence."""\n\nfrom sqlalchemy import create_engine, text\n\n\nclass OrderRepository:\n'
            '    def __init__(self, url="sqlite:///orders.db"):\n        self.engine = create_engine(url)\n')
    repo = _copy(tmp_path / "sqla", {"orders/repository.py": sqla})
    b = dbr.brief(repo, q, store=None, record=False)
    opts = {o["name"]: o for o in b["options"]}
    # the engine does not name the database: neither option is shown absent
    assert opts["SQLite"]["present_in_project"] is None and opts["PostgreSQL"]["present_in_project"] is None
    assert "create_engine" in opts["SQLite"]["presence_note"]
    assert dbr.compact(b)["options"][0].get("presence_note")

    doc = ('"""Notes for operators.\n\nA PostgreSQL deployment would look like this::\n\n    import psycopg2\n\n'
           'and a MongoDB one::\n\n    from pymongo import MongoClient\n"""\n\nNOTE = 1\n')
    repo = _copy(tmp_path / "doc", {"orders/notes.py": doc})
    b = dbr.brief(repo, "Should we move orders to MongoDB?", store=None, record=False)
    opts = {o["name"]: o for o in b["options"]}
    assert opts["MongoDB"]["present_in_project"] is False
    assert "PostgreSQL" not in opts  # never added as an option the project uses
    assert opts["SQLite"]["present_in_project"] is True


# -- 4 / 5 / 6: routing ------------------------------------------------------------------------------------

def _read(q: str) -> list[str]:
    return [(qp.clause_cues(c["text"]) or [{"intent": "locate"}])[0]["intent"] for c in qp.segment(q)]


@pytest.mark.parametrize("question", [
    "Sence Redis eklemeli miyiz?", "Redis kullanmali miyiz?", "Postgres'e gecmeli miyiz?",
    "Siparisleri PostgreSQL'e tasimali miyiz?", "Redis kullanmalı mıyız?",
    "Would switching to PostgreSQL be worth the effort?", "What would you pick for the order store?",
    "Can we get away with SQLite in production?", "Do we keep the repository class or replace it with SQLAlchemy?",
    "Which database fits best for concurrent writes?", "Is now the right time to move to MongoDB?",
    "SQLAlchemy kullanmaya başlasak iyi olur mu?", "Şimdi PostgreSQL'e taşınmanın tam zamanı mı?",
    # a version choice stays a decision (only "which version should I use" asks what the project states)
    "Which Django version should we move to?", "Which version of PostgreSQL should we use in production?",
])
def test_choice_questions_are_read_as_decisions(question):
    assert _read(question) == ["decide"], _read(question)


@pytest.mark.parametrize("question", [
    "Which Python version should I use to run this project?", "Which database URL should I use for local development?",
    "Which database does OrderRepository connect to?", "Bu projede PostgreSQL kullanılıyor mu?",
    "get_repo her çağrıda yeni bir nesne mi döndürüyor?",
])
def test_code_questions_are_not_decisions(question):
    assert "decide" not in _read(question), _read(question)
    assert not qp.asks_for_choice(question)


def test_a_turkish_compound_splits_at_ve_and_keeps_both_parts(orders):
    repo, st = orders
    q = "SQLite bağlantısı nerede açılıyor ve PostgreSQL'e geçmeli miyiz?"
    assert _read(q) == ["locate", "decide"]
    res = analysis.analyze(st, repo, q, challenge=False)
    subs = [(s["intent"], s["status"]) for s in res["subquestions"]]
    assert subs[-1] == ("decide", qp.HUMAN_DECISION) and subs[0][0] != "decide", subs
    assert subs[0][1] != qp.HUMAN_DECISION


def test_words_that_may_ask_for_a_choice_cap_the_verdict_below_met():
    sq = {"id": "q1", "intent": "dataflow", "done_when": {"kind": "claim_exists"}}
    claims = [{"id": "c1", "kind": "flow", "status": "statically_verified"}]
    assert analysis.judge(sq, claims, {}) == "met"
    assert analysis.judge(sq, claims, {"may_ask_for_choice": "fits"}) == "met_with_inference"


# -- 7 / 8 / 9 / 12: hand-edited records --------------------------------------------------------------

def test_hand_edited_records_are_never_silently_enforced_or_waived(tmp_path, capsys):
    from verinoda import cli
    from verinoda import decisions as dm
    from verinoda import guards

    repo = _copy(tmp_path / "o")
    rec = _record(repo, [ORD_G])
    _write(repo, "orders/audit.py", CALL)
    d = dm.decisions_dir(repo)
    # a YAML block list: the record is not enforced, and the check does not say ok
    (d / "ADR-0005-yaml.md").write_text(
        "---\nverinoda-decision: 1\nid: ADR-0005\ntitle: y\nstatus: accepted\ndecided-by: human\nguards:\n"
        "  - id: g1\n    kind: only_in\n    calls: [sqlite3.connect]\n    allowed: [orders/repository.py]\n---\n",
        encoding="utf-8")
    yaml = next(x for x in dm.load_all(repo) if x.id == "ADR-0005")
    assert not yaml.enforced and "YAML block lists" in yaml.problems[0]
    # an expired waiver whose date is not zero-padded is not applied
    _front_edit(repo, rec, "waivers: []", 'waivers: [{"guard": "g1", "at": "orders/audit.py:5", "reason": "r", '
                                          '"until": "2026-9-1"}]')
    res = guards.check(repo)
    assert [v["at"] for v in res["violations"]] == ["orders/audit.py:5"] and res["exit"] == 1
    assert any("is not a date" in n["why"] for n in res["not_enforced"])
    assert dm.waived(dm.load_all(repo)[0], "g1", "orders/audit.py", 5, "2026-01-01") is None
    # allowed written as a string: a problem, never a VIOLATED on the allowed file
    _front_edit(repo, rec, '"allowed": ["orders/repository.py"]', '"allowed": "orders/repository.py"')
    res = guards.check(repo)
    assert not res["violations"] and res["status"] == "unknown", res
    assert any("allowed must be a JSON list" in n["why"] for n in res["not_enforced"])
    capsys.readouterr()
    assert cli.main(["decide", "list", "--repo", str(repo)]) == 0
    assert "problem: guard g1: allowed must be a JSON list" in capsys.readouterr().out


def test_records_that_contradict_each_other_are_not_both_enforced(tmp_path):
    from verinoda import decisions as dm
    from verinoda import guards

    repo = _copy(tmp_path / "o")
    old = _record(repo, [ORD_G])
    st = open_store(repo)
    try:
        dm.record(st, repo, chosen="SQLite", rationale="r", supersedes=old["id"], user_statement="u",
                  guards=["only_in calls=sqlite3.connect allowed=orders/repository.py,orders/audit.py"])
    finally:
        st.close()
    _write(repo, "orders/audit.py", CALL)
    _front_edit(repo, old, "status: superseded", "status: accepted")
    recs = {x.id: x for x in dm.load_all(repo)}
    assert not recs[old["id"]].enforced and recs[old["id"]].inactive
    res = guards.check(repo)
    assert res["exit"] == 0 and not res["violations"], res["violations"]
    # two files with the same id: a problem on both
    src = dm.decisions_dir(repo) / Path(old["file"]).name
    shutil.copyfile(src, src.with_name(f"{old['id']}-copy.md"))
    same = [x for x in dm.load_all(repo) if x.id == old["id"]]
    assert len(same) == 2 and all(any("also used by" in p for p in x.problems) for x in same)


# -- 10 / 13: BOM, status and JSON errors -------------------------------------------------------------

def test_a_bom_file_is_checked_and_the_status_is_never_ok_when_something_was_not(tmp_path, capsys):
    from verinoda import cli
    from verinoda import guards

    repo = _copy(tmp_path / "o")
    _record(repo, [ORD_G])
    (repo / "orders" / "audit.py").write_bytes(b'\xef\xbb\xbfimport sqlite3\r\n\r\n\r\ndef f():\r\n'
                                               b'    return sqlite3.connect("x")\r\n')
    res = guards.check(repo)
    assert [v["at"] for v in res["violations"]] == ["orders/audit.py:5"] and res["exit"] == 1
    _write(repo, "orders/audit.py", "import sqlite3\ndef f(:\n    return sqlite3.connect('x')\n")
    res = guards.check(repo)
    # a file that was not checked is never a pass in CI: exit 3 (not 1: nothing is shown violated)
    assert res["status"] == "unknown" and res["exit"] == 3 and "could not be completed" in res["next_step"]
    assert "not completed" in res["exit_because"]
    assert cli._decision_summary(repo)["not_checked"] == 1  # `update` never says "0 violated" alone then
    _write(repo, "orders/audit.py", CALL)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "audit")
    res = guards.check(repo, changed_only=True)
    assert res["status"] == "pre_existing" and res["exit"] == 0 and res["pre_existing"]
    assert "still breaks the decision" in res["next_step"]
    capsys.readouterr()
    assert cli.main(["decide", "check", "--repo", str(repo), "--base", "no-such-ref", "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "error"


# -- 14: a module-level call runs a function before a later rebinding ------------------------------------

@pytest.mark.parametrize("text,want", [
    ("import sqlite3\n\n\ndef open_all():\n    return sqlite3.connect('x')\n\n\nCONN = open_all()\nsqlite3 = None\n",
     [("POSSIBLE", 5)]),
    ("import sqlite3\n\n\ndef open_all():\n    return sqlite3.connect('x')\n\n\nsqlite3 = None\n", []),
    # an earlier literal that a later import replaces: every call after the module ran reaches the import
    ("sqlite3 = None\nprint('x')\nimport sqlite3\n\n\ndef open_all():\n    return sqlite3.connect('x')\n",
     [("VIOLATED", 7)]),
])
def test_a_call_while_the_module_runs_sees_the_earlier_binding(tmp_path, text, want):
    from verinoda import decisions as dm
    from verinoda import guards

    _write(tmp_path, "pkg/m.py", text)
    hits, _scan, _ = guards.check_only_in(guards._Ctx(tmp_path, ["pkg/m.py"]), dm.parse_guard(ORD_G, tmp_path, "g1"))
    assert [(h[0], h[2]) for h in hits] == want


# -- 15: the brief's reasons keep a force's status, in the question's language ---------------------------

def test_asked_because_keeps_the_inference_and_speaks_turkish(orders):
    from verinoda import decision_brief as dbr

    repo, _ = orders
    b = dbr.brief(repo, "Should we move orders from SQLite to PostgreSQL if traffic grows?", store=None, record=False)
    q1 = next(q for q in b["questions_for_human"] if q["id"] == "q1")
    assert "appears to keep one instance per process (strong_inference)" in q1["asked_because"]
    tr = dbr.brief(repo, "Trafik artarsa veritabanı olarak ne kullanmalıyız?", store=None, record=False)
    c = dbr.compact(tr)
    assert c["questions_for_human"][0]["asked_because"].startswith("kaç yazıcı beklediğiniz kodda yok")


# -- 16: generated code and sample folders are out of the default scope, named in the limits -------------

def test_generated_and_example_code_are_out_of_scope_and_named(tmp_path):
    from verinoda import guards

    gen = "# Generated by the protocol buffer compiler.  DO NOT EDIT!\n" + CALL
    repo = _copy(tmp_path / "o", {"orders/_generated/schema.py": gen, "examples/demo/app.py": CALL})
    res = guards.check(repo, records=[_rec(repo, ORD_G)])
    assert res["exit"] == 0 and not res["violations"], res["violations"]
    lims = [lim for o in res["ok"] for lim in o["limits"]]
    assert any("generated file(s)" in lim and "orders/_generated/schema.py" in lim for lim in lims), lims
    assert any("example, sample, demo" in lim and "examples/demo/app.py" in lim for lim in lims), lims
    res = guards.check(repo, records=[_rec(repo, ORD_G + " scope=all")])
    assert sorted(v["at"] for v in res["violations"]) == ["examples/demo/app.py:5", "orders/_generated/schema.py:6"]
