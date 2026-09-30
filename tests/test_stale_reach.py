"""`verinoda review` lists the claims, notes and decision records a change makes stale (verinoda/stale_reach.py).

Each test edits its own git copy of a small indexed project with stored claims, notes and a decision record."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import shutil  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import decisions as dm  # noqa: E402
from verinoda import evidence as evmod  # noqa: E402
from verinoda import review as rv  # noqa: E402
from verinoda import stale_reach, usernotes, workflow  # noqa: E402
from verinoda.claims import Claims  # noqa: E402
from verinoda.store import open_store  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")

FILES = {
    "verinoda.toml": '[decisions]\ndir = "docs/decisions"\n',
    "orders/__init__.py": "",
    "orders/service.py": ("import sqlite3\n\n\ndef place(order):\n    conn = sqlite3.connect(\"x.db\")\n"
                          "    return conn\n\n\ndef total(items):\n    return sum(items)\n"),
    "ui/__init__.py": "",
    "ui/view.py": "def render():\n    return \"ok\"\n",
    "README.md": "# Shop\n\nOrders are placed in orders/service.py.\n",
}
PLACE = "orders/service.py::place()"


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
    repo = tmp_path_factory.mktemp("stale") / "shop"
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
        snap = st.latest_snapshot()
        cl = Claims(st, repo)
        cl.create("place() opens x.db with sqlite3.connect", project="shop", snapshot=snap, subjects=[PLACE],
                  status="strong_inference",
                  evidence=[(evmod.source_evidence(repo, "orders/service.py", 4, 6, commit=None), "supports")])
        # an older claim: no recorded dependencies, so the file rule of the invalidation applies
        Claims(st, None).create("render() returns ok", project="shop", snapshot=snap, subjects=["ui/view.py"],
                                status="weak_inference",
                                evidence=[(evmod.source_evidence(repo, "ui/view.py", 1, 2, commit=None),
                                           "supports")])
        dm.record(st, repo, chosen="SQLite", rationale="one local file", title="Orders on SQLite",
                  governs=["orders/service.py::place"])
    finally:
        st.close()
    usernotes.save(repo, PLACE, "orders/service.py", 4, 6, "x.db is the test database; production sets it.")
    usernotes.save(repo, "orders/service.py::total()", "orders/service.py", 9, 10, "Totals stay in Python.")
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


def test_a_body_change_lists_the_claim_the_note_and_the_record(shop):
    _edit(shop, "orders/service.py", 'sqlite3.connect("x.db")', 'sqlite3.connect("y.db")')
    res = _review(shop)
    ms = res["made_stale"]
    assert ms["counts"] == {"claims": 1, "notes": 1, "decisions": 1}
    c = ms["claims"][0]
    assert c["status"] == "statically_verified" and c["claim_status"] == "strong_inference"
    assert c["at"] == "orders/service.py:5" and "orders/service.py::place" in c["why"]
    assert c["evidence_at"] == ["orders/service.py:4"]
    n = ms["notes"][0]
    assert n["note"] == PLACE and n["now"] == "changed" and n["status"] == "statically_verified"
    assert n["at"] == "orders/service.py:5" and n["evidence_at"][0].startswith(".verinoda/notes/")
    d = ms["decisions"][0]
    assert d["decision"] == "ADR-0001" and d["status"] == "statically_verified"
    assert d["evidence_at"][0].startswith("docs/decisions/")
    # the note on total() and the claim on ui/view.py are not reached
    assert ms["checked"] == {"claims": 2, "notes": 2} and "stale_before" not in ms
    assert "Made stale: 1 claim, 1 note, 1 decision record(s)." in res["summary"]
    text = rv.render_text(res)
    assert "Made stale by the change (1 claim(s), 1 note(s), 1 decision record(s)" in text
    assert f"note {PLACE}" in text
    # nothing is written: the claim keeps its status until `verinoda update`
    st = open_store(shop)
    try:
        assert st.claim(c["claim"])["status"] == "strong_inference"
    finally:
        st.close()


def test_an_unrelated_or_comment_only_change_lists_nothing(shop):
    _edit(shop, "orders/service.py", "    return conn\n", "    return conn  # the caller closes it\n")
    _edit(shop, "README.md", "Orders are placed", "Orders get placed")
    ms = _review(shop)["made_stale"]
    assert ms["counts"] == {"claims": 0, "notes": 0, "decisions": 1}   # the record: a changed line in its span
    assert ms["claims"] == [] and ms["notes"] == []


def test_a_removed_symbol_is_gone_at_its_base_line(shop):
    _edit(shop, "orders/service.py", "def place(order):\n    conn = sqlite3.connect(\"x.db\")\n    return conn\n\n\n",
          "")
    ms = _review(shop)["made_stale"]
    c = ms["claims"][0]
    assert c["status"] == "statically_verified" and "gone" in c["why"]
    assert c["at"].endswith(" (base)") and c["at"].startswith("orders/service.py:")
    n = ms["notes"][0]
    assert n["note"] == PLACE and n["now"] == "gone" and n["at"].endswith(" (base)")


def test_a_claim_without_dependencies_uses_the_file_rule(shop):
    _edit(shop, "ui/view.py", 'return "ok"', 'return "fine"')
    ms = _review(shop)["made_stale"]
    assert [c["text"] for c in ms["claims"]] == ["render() returns ok"]
    c = ms["claims"][0]
    assert c["status"] == "strong_inference" and "ui/view.py" in c["why"] and c["at"] == "ui/view.py:2"


def test_what_was_stale_before_the_change_is_counted_not_listed(shop):
    _edit(shop, "orders/service.py", 'sqlite3.connect("x.db")', 'sqlite3.connect("y.db")')
    _git(shop, "commit", "-q", "-am", "y")
    _edit(shop, "orders/service.py", 'sqlite3.connect("y.db")', 'sqlite3.connect("z.db")')
    ms = _review(shop)["made_stale"]
    assert ms["claims"] == [] and ms["notes"] == []
    assert ms["stale_before"] == {"claims": 1, "notes": 1}
    # against the commit the claim and the note were made on, the same change reaches both
    ms = _review(shop, base="HEAD~1")["made_stale"]
    assert ms["counts"]["claims"] == 1 and ms["counts"]["notes"] == 1


def test_the_staged_version_is_what_is_compared(shop):
    _edit(shop, "orders/service.py", 'sqlite3.connect("x.db")', 'sqlite3.connect("y.db")')
    _git(shop, "add", "orders/service.py")
    _edit(shop, "orders/service.py", 'sqlite3.connect("y.db")', 'sqlite3.connect("x.db")')
    assert _review(shop)["made_stale"]["counts"]["claims"] == 0
    assert _review(shop, staged=True)["made_stale"]["counts"]["claims"] == 1


def test_a_planned_change_and_no_store():
    res = stale_reach.reached(Path("."), None, [], {})
    assert res["counts"] == {"claims": 0, "notes": 0, "decisions": 0} and stale_reach.render_lines(res) == []
    assert stale_reach.summary_part(res) == ""


def test_check_text_matches_check(shop):
    note = usernotes.find(shop, PLACE)
    text = (shop / "orders/service.py").read_text(encoding="utf-8")
    from verinoda import anchors

    facts = anchors.compute_facts("orders/service.py", text.encode())
    assert usernotes.check_text(note, text, facts) == usernotes.check(shop, note)
    assert usernotes.check_text(note, None, None)["status"] == "gone"
