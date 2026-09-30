"""`verinoda review` lists the claims, notes and decision records a change makes stale (verinoda/stale_reach.py).

Each test edits its own git copy of a small indexed project with stored claims, notes and a decision record."""

from __future__ import annotations

import hashlib
import json
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
    # a comment inside the symbol the record governs leaves what the record rests on ("Decisions to read" says it)
    assert ms["counts"] == {"claims": 0, "notes": 0, "decisions": 0}
    assert ms["claims"] == [] and ms["notes"] == [] and ms["decisions"] == []


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


def test_no_store_with_a_real_diff_says_claims_were_not_checked(shop):
    _edit(shop, "orders/service.py", 'sqlite3.connect("x.db")', 'sqlite3.connect("y.db")')
    old = FILES["orders/service.py"]
    new = (shop / "orders/service.py").read_text(encoding="utf-8")
    res = stale_reach.reached(shop, None, [rv.FileDiff("orders/service.py", old, new)], {})
    assert res["not_checked"] == ["claims (no claim store)"] and res["checked"]["claims"] == 0
    assert res["counts"]["notes"] == 1   # notes are read without a store


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _dep(key: str, fp, facet: str = "content", scheme: str = "file") -> dict:
    return {"dep_key": key, "facet": facet, "fp": fp, "scheme": scheme}


def test_a_whole_file_dependency_is_compared_with_its_recorded_sha():
    old, new = "[db]\nname = x\n", "[db]\nname = y\n"
    v = stale_reach._Versions([rv.FileDiff("settings.cfg", old, new, True, old.encode(), new.encode())])
    r, was = stale_reach._dep_reach(_dep("file:settings.cfg", _sha(old)), v)
    assert r["status"] == "statically_verified" and "edits settings.cfg" in r["why"] and not was
    assert r["at"] == "settings.cfg:2"
    # recorded from a file with CRLF line ends: the same version
    assert stale_reach._dep_reach(_dep("file:settings.cfg", _sha(old.replace("\n", "\r\n"))), v)[0] is not None
    # made on the new version: neither listed nor counted (`update` would not mark it stale)
    assert stale_reach._dep_reach(_dep("file:settings.cfg", _sha(new)), v) == (None, False)
    assert stale_reach._dep_reach(_dep("file:settings.cfg", _sha(new.replace("\n", "\r\n"))), v) == (None, False)
    # matching neither version: stale before the change
    assert stale_reach._dep_reach(_dep("file:settings.cfg", _sha("[db]\nname = z\n")), v) == (None, True)
    # removed by the change
    v = stale_reach._Versions([rv.FileDiff("settings.cfg", old, None, True, old.encode(), None)])
    r, _ = stale_reach._dep_reach(_dep("file:settings.cfg", _sha(old)), v)
    assert "removes settings.cfg" in r["why"] and r["at"].endswith(" (base)")


def test_a_symbol_dependency_made_on_the_new_version_is_not_stale_before():
    from verinoda import anchors

    old, new = "def f():\n    return 1\n", "def f():\n    return 2\n"
    v = stale_reach._Versions([rv.FileDiff("a.py", old, new)])
    fo, fn = anchors.compute_facts("a.py", old.encode()), anchors.compute_facts("a.py", new.encode())
    key = anchors.sym_key("a.py", "f")

    def dep(f):
        return _dep(key, anchors.facet_fp(f, key, "body"), "body", f["scheme"])

    r, was = stale_reach._dep_reach(dep(fo), v)
    assert r["status"] == "statically_verified" and r["at"] == "a.py:2" and not was
    assert stale_reach._dep_reach(dep(fn), v) == (None, False)
    other = anchors.compute_facts("a.py", b"def f():\n    return 3\n")
    assert stale_reach._dep_reach(dep(other), v) == (None, True)


def test_the_file_rule_branches_scope_tests_and_an_unparsable_version():
    code = rv.FileDiff("pkg/core.py", "def f():\n    return 1\n", "def f(:\n    return 1\n")
    test = rv.FileDiff("tests/test_core.py", "def test_f():\n    pass\n", "def test_f():\n    assert 1\n")
    doc = rv.FileDiff("README.md", "# A\n", "# B\n")
    v = stale_reach._Versions([code, test, doc])
    r, _ = stale_reach._dep_reach(_dep("scope:code", None, "any", "scope"), v)
    assert r["status"] == "strong_inference" and "whole project" in r["why"] and "pkg/core.py" in r["why"]
    assert stale_reach._dep_reach(_dep("scope:code", None, "any", "scope"),
                                  stale_reach._Versions([doc]))[0] is None
    r, _ = stale_reach._dep_reach(_dep("testset", "abc", "tests", "testset"), v)
    assert r["status"] == "strong_inference" and "tests/test_core.py" in r["why"]
    v.head_testset = "abc"   # the claim matches the new version's tests
    assert stale_reach._dep_reach(_dep("testset", "abc", "tests", "testset"), v) == (None, False)
    r, _ = stale_reach._dep_reach(_dep("sym:pkg/core.py::f", "x", "body", "py1"), v)
    assert r["status"] == "strong_inference" and "could not be compared" in r["why"]


def test_notes_on_files_with_a_byte_order_mark(shop):
    _write(shop, "b.py", "﻿def h():\n    return 1\n")
    _write(shop, "cfg.txt", "﻿key=1\nother=2\n")
    _write(shop, "doc.md", "﻿# Title\n\nSome text.\n")
    _git(shop, "add", "-A")
    _git(shop, "commit", "-q", "-m", "bom")
    usernotes.save(shop, "b.py::h()", "b.py", 1, 2, "h returns one")
    usernotes.save(shop, "cfg.txt", "cfg.txt", 1, 2, "the key")
    usernotes.save(shop, "doc.md", "doc.md", 1, 3, "the doc")
    assert {usernotes.check(shop, usernotes.find(shop, s))["status"]
            for s in ("b.py::h()", "cfg.txt", "doc.md")} == {"fresh"}
    _edit(shop, "b.py", "return 1", "return 2")
    _edit(shop, "cfg.txt", "key=1", "key=2")
    _edit(shop, "doc.md", "Some text.", "Other text.")
    ms = _review(shop)["made_stale"]
    assert sorted(n["note"] for n in ms["notes"]) == ["b.py::h()", "cfg.txt", "doc.md"]
    assert "stale_before" not in ms


def test_items_made_on_the_new_version_are_neither_listed_nor_stale_before(shop):
    _edit(shop, "orders/service.py", "    return sum(items)\n", "    return sum(items) + 0\n")
    usernotes.save(shop, "orders/service.py::total()", "orders/service.py", 9, 10, "written after the edit")
    st = open_store(shop)
    try:
        workflow.scan(st, shop)
        Claims(st, shop).create("total() sums the items", project="shop", snapshot=st.latest_snapshot(),
                                subjects=["orders/service.py::total()"], status="strong_inference",
                                evidence=[(evmod.source_evidence(shop, "orders/service.py", 9, 10, commit=None),
                                           "supports")])
    finally:
        st.close()
    ms = _review(shop)["made_stale"]
    assert ms["claims"] == [] and ms["notes"] == [] and "stale_before" not in ms
    assert ms["checked"] == {"claims": 3, "notes": 2}


def test_cli_and_mcp_show_what_is_made_stale(shop, capsys):
    from verinoda.cli import main
    from verinoda.mcp.server import AtlasTools

    _edit(shop, "orders/service.py", 'sqlite3.connect("x.db")', 'sqlite3.connect("y.db")')
    main(["review", str(shop), "--json"])
    res = json.loads(capsys.readouterr().out)
    assert res["made_stale"]["counts"] == {"claims": 1, "notes": 1, "decisions": 1}
    main(["review", str(shop)])
    text = capsys.readouterr().out
    assert "Made stale by the change (1 claim(s), 1 note(s), 1 decision record(s)" in text
    got = AtlasTools(shop).change_review()
    assert got["made_stale"]["counts"] == {"claims": 1, "notes": 1, "decisions": 1}


def test_check_text_matches_check(shop):
    note = usernotes.find(shop, PLACE)
    text = (shop / "orders/service.py").read_text(encoding="utf-8")
    from verinoda import anchors

    facts = anchors.compute_facts("orders/service.py", text.encode())
    assert usernotes.check_text(note, text, facts) == usernotes.check(shop, note)
    assert usernotes.check_text(note, None, None)["status"] == "gone"
