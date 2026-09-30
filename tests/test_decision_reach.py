"""`verinoda review` lists the decision records a change reaches (verinoda/decision_reach.py).

Each test edits its own git copy of a small indexed project whose records live in a committed folder."""

from __future__ import annotations

import json
import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import shutil  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import decisions as dm  # noqa: E402
from verinoda import review as rv  # noqa: E402
from verinoda import workflow  # noqa: E402
from verinoda.store import open_store  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")

FILES = {
    "verinoda.toml": '[decisions]\ndir = "docs/decisions"\n',
    "store/__init__.py": "",
    "store/db.py": "import sqlite3\n\n\ndef connect(path):\n    return sqlite3.connect(path)\n",
    "orders/__init__.py": "",
    "orders/service.py": ("from store.db import connect\n\n\ndef place(order):\n    conn = connect(\"x.db\")\n"
                          "    return conn\n\n\ndef total(items):\n    return sum(items)\n"),
    "ui/__init__.py": "",
    "ui/view.py": "def render():\n    return \"ok\"\n\n\ndef title():\n    return \"t\"\n",
    "requirements.txt": "requests\n",
    "README.md": "# Shop\n\nOnly store/db.py may call sqlite3.connect(path).\n",
}


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false",
                           "-c", "commit.gpgsign=false", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout


def _write(repo: Path, rel: str, text: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


def _edit(repo: Path, rel: str, find: str, replace: str) -> None:
    text = (repo / rel).read_text(encoding="utf-8")
    assert text.count(find) == 1, (rel, find)
    _write(repo, rel, text.replace(find, replace, 1))


@pytest.fixture(scope="module")
def base(tmp_path_factory):
    repo = tmp_path_factory.mktemp("reach") / "shop"
    for rel, text in FILES.items():
        _write(repo, rel, text)
    _write(repo, ".gitignore", "__pycache__/\n*.pyc\n.verinoda/\n")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    workflow.init(repo)
    st = open_store(repo)
    try:
        workflow.scan(st, repo)
        dm.record(st, repo, chosen="SQLite", rationale="one local file", title="Keep SQLite behind store.db",
                  governs=["orders/service.py::place"],
                  guards=["only_in calls=sqlite3.connect allowed=store/db.py", "no_edge from=ui/** to=store/**",
                          "dependency absent=psycopg", "dependency absent=psycopg_binary"],
                  revisit_when=["file_appears=migrations/*.sql"])
        dm.record(st, repo, chosen="plain strings", rationale="no templates yet", title="Views return strings",
                  governs=["ui/view.py::render"])
        dm.add_guards(st, repo, "ADR-0002", ["only_in pattern=return allowed=store/db.py"], status="proposed")
        old = dm.record(st, repo, chosen="totals in Python", rationale="small carts", title="Totals in Python",
                        governs=["orders/service.py::total"])
        dm.record(st, repo, chosen="totals in SQL", rationale="big carts", title="Totals in SQL",
                  supersedes=old["id"])
    finally:
        st.close()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "records")
    return repo


@pytest.fixture()
def shop(base, tmp_path):
    dst = tmp_path / "shop"
    shutil.copytree(base, dst)
    return dst


def _review(repo: Path, **kw) -> dict:
    st = open_store(repo)
    try:
        return rv.review(repo, store=st, record=False, **kw)
    finally:
        st.close()


def _rec(res: dict, did: str) -> dict | None:
    return next((r for r in res["decisions"]["records"] if r["decision"] == did), None)


def test_a_change_to_a_governed_symbol_lists_its_record_with_evidence(shop):
    _edit(shop, "orders/service.py", 'conn = connect("x.db")', 'conn = connect("y.db")')
    res = _review(shop)
    rec = _rec(res, "ADR-0001")
    assert rec is not None and rec["enforced"] and rec["file"].startswith("docs/decisions/")
    gov = [h for h in rec["reached_by"] if h["kind"] == "governs"]
    assert gov and gov[0]["status"] == "statically_verified"
    assert gov[0]["at"].startswith("orders/service.py:")
    assert gov[0]["evidence_at"][0].startswith(rec["file"] + ":")
    # the record of another symbol is not reached; the superseded record of the same file is not listed
    assert _rec(res, "ADR-0002") is None and _rec(res, "ADR-0003") is None
    assert "superseded" in res["decisions"]["not_listed"]
    assert "ADR-0001" in res["summary"]
    text = rv.render_text(res)
    assert "Decisions to read (1 record(s)" in text and "ADR-0001 Keep SQLite behind store.db" in text


def test_a_guarded_call_on_a_changed_line_outside_the_allowed_file(shop):
    _edit(shop, "orders/service.py", "    return sum(items)\n",
          "    import sqlite3\n    sqlite3.connect(\":memory:\")\n    return sum(items)\n")
    res = _review(shop)
    rec = _rec(res, "ADR-0001")
    calls = [h for h in rec["reached_by"] if h["kind"] == "guard" and h["entry"] == "g1"]
    assert calls and calls[0]["status"] == "strong_inference" and "sqlite3.connect" in calls[0]["why"]
    assert calls[0]["at"] == "orders/service.py:11"
    # total's own record is superseded: not listed
    assert _rec(res, "ADR-0003") is None


def test_the_allowed_file_and_an_edge_glob(shop):
    _edit(shop, "store/db.py", "return sqlite3.connect(path)", "return sqlite3.connect(path, timeout=5)")
    res = _review(shop)
    by = {(h["entry"], h["status"]) for h in _rec(res, "ADR-0001")["reached_by"]}
    assert ("g1", "statically_verified") in by   # the allowed file
    assert ("g2", "statically_verified") in by   # to=store/**


def test_manifest_and_new_file_reach_dependency_guard_and_revisit(shop):
    _edit(shop, "requirements.txt", "requests\n", "requests\npsycopg\n")
    _write(shop, "migrations/001.sql", "create table t (id int);\n")
    res = _review(shop)
    hits = _rec(res, "ADR-0001")["reached_by"]
    dep = [h for h in hits if h["kind"] == "guard" and h["entry"] == "g3"]
    assert dep and dep[0]["at"] == "requirements.txt:2" and dep[0]["status"] == "strong_inference"
    rev = [h for h in hits if h["kind"] == "revisit_when"]
    assert rev and rev[0]["at"] == "migrations/001.sql" and rev[0]["status"] == "statically_verified"


def test_an_edited_record_is_a_record_to_read(shop):
    rec_file = next((shop / "docs" / "decisions").glob("*0002*.md"))
    rec_file.write_text(rec_file.read_text(encoding="utf-8") + "\nMore context.\n", encoding="utf-8",
                        newline="\n")
    res = _review(shop)
    rec = _rec(res, "ADR-0002")
    assert rec is not None and rec["reached_by"][0]["kind"] == "record"


def test_no_change_to_what_records_name_lists_nothing(shop):
    _edit(shop, "orders/service.py", "return sum(items)", "return sum(items) + 0")
    res = _review(shop)
    assert res["decisions"]["records"] == [] and res["decisions"]["read"] == 4
    assert "decision record" not in res["summary"]
    assert "Decisions to read" not in rv.render_text(res)


def test_a_planned_change_reaches_the_governed_symbol(shop):
    res = _review(shop, targets=["orders/service.py::place"], change="body")
    assert _rec(res, "ADR-0001") is not None


def _front_edit(repo: Path, did: str, key: str, fn) -> None:
    """Hand-edit one front matter line of a record (a JSON value), as a person editing the file would."""
    path = next((repo / "docs" / "decisions").glob(f"{did}*.md"))
    lines = path.read_text(encoding="utf-8").split("\n")
    i = next(i for i, ln in enumerate(lines) if ln.startswith(key + ":"))
    raw = lines[i].partition(":")[2].strip()
    new = fn(json.loads(raw) if raw[:1] in "[{\"" else raw)
    lines[i] = f"{key}: " + (new if isinstance(new, str) else json.dumps(new))
    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")


def _commit(repo: Path) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "edit")


def test_a_package_name_is_compared_as_decide_check_compares_it(shop):
    _edit(shop, "requirements.txt", "requests\n", "requests\npsycopg-binary\n")
    hits = _rec(_review(shop), "ADR-0001")["reached_by"]
    dep = [h for h in hits if h["entry"] == "g4"]
    assert dep and dep[0]["at"] == "requirements.txt:2"
    assert not [h for h in hits if h["entry"] == "g3"]   # psycopg is another package


def test_a_manifest_decide_check_does_not_read_reaches_nothing(shop):
    _write(shop, "tests/fixtures/app/requirements.txt", "psycopg\n")
    assert _rec(_review(shop), "ADR-0001") is None


def test_a_call_named_in_prose_or_in_an_example_folder_reaches_nothing(shop):
    _edit(shop, "README.md", "call sqlite3.connect(path).", "call sqlite3.connect(path) or sqlite3.connect().")
    _write(shop, "examples/demo.py", "import sqlite3\n\n\ndef main():\n    return sqlite3.connect(':memory:')\n")
    assert _rec(_review(shop), "ADR-0001") is None


def test_a_docstring_edit_of_a_governed_symbol_reaches_its_record(shop):
    _edit(shop, "orders/service.py", "def place(order):\n", "def place(order):\n    \"\"\"Place an order.\"\"\"\n")
    res = _review(shop)
    gov = [h for h in _rec(res, "ADR-0001")["reached_by"] if h["kind"] == "governs"]
    assert gov and gov[0]["at"] == "orders/service.py:5" and gov[0]["status"] == "statically_verified"
    assert "docstring" in gov[0]["why"]


def test_a_proposed_guard_says_so_and_its_pattern_is_not_run(shop):
    _edit(shop, "ui/view.py", 'return "t"', 'return "title"')
    assert _rec(_review(shop), "ADR-0002") is None   # `return` matches, but a proposed pattern is not run
    _edit(shop, "store/db.py", "return sqlite3.connect(path)", "return sqlite3.connect(path, timeout=5)")
    hits = _rec(_review(shop), "ADR-0002")["reached_by"]
    assert hits[0]["entry"] == "g1" and "proposed, not enforced" in hits[0]["why"]


def test_a_planned_change_does_not_call_its_lines_changed(shop):
    res = _review(shop, targets=["orders/service.py::place"], change="body")
    whys = [h["why"] for h in _rec(res, "ADR-0001")["reached_by"] if h["kind"] == "guard"]
    assert whys and not any(w.startswith("a changed line") for w in whys)


@pytest.mark.parametrize("fn", [
    lambda gs: [{**g, "relations": [1]} if g["kind"] == "no_edge" else g for g in gs],
    lambda gs: [{**g, "pattern": 5, "calls": "sqlite3.connect", "allowed": "store/db.py"}
                if g["kind"] == "only_in" else g for g in gs],
], ids=["relations_int", "pattern_int_calls_str"])
def test_a_hand_edited_entry_of_the_wrong_type_does_not_stop_the_review(shop, fn):
    _front_edit(shop, "ADR-0001", "guards", fn)
    _commit(shop)
    _edit(shop, "ui/view.py", 'return "t"', 'return "title"')
    _edit(shop, "orders/service.py", "    return sum(items)\n", "    e(1)\n    return sum(items)\n")
    rec = _rec(_review(shop), "ADR-0001")
    assert rec is not None and rec["problems"]
    assert not [h for h in rec["reached_by"] if h["at"].startswith("orders/service.py")]


def test_a_deleted_or_demoted_record_is_listed(shop):
    rec_file = next((shop / "docs" / "decisions").glob("*0002*.md"))
    rec_file.unlink()
    rec = _rec(_review(shop), "ADR-0002")
    assert rec is not None and rec["status"] == "deleted" and "accepted in the base" in rec["reached_by"][0]["why"]
    _git(shop, "checkout", "--", ".")
    _front_edit(shop, "ADR-0002", "status", lambda v: "deprecated")
    rec = _rec(_review(shop), "ADR-0002")
    assert rec is not None and "status accepted in the base, deprecated now" in rec["reached_by"][0]["why"]


def test_a_superseded_record_whose_file_says_accepted_is_not_listed(shop):
    _front_edit(shop, "ADR-0003", "status", lambda v: "accepted")
    _commit(shop)
    _edit(shop, "orders/service.py", "return sum(items)", "return sum(items) + 0")
    res = _review(shop)
    assert _rec(res, "ADR-0003") is None and "1 superseded" in res["decisions"]["not_listed"]


def test_a_folder_that_cannot_be_read_is_said(shop):
    _write(shop, "verinoda.toml", '[decisions]\ndir = "../../elsewhere"\n')
    res = _review(shop)
    assert res["decisions"]["error"] and str(shop) not in res["decisions"]["error"]
    assert any(u["kind"] == "decision_records" for u in res["unknown"])
    assert "Decisions: the records could not all be read" in rv.render_text(res)
