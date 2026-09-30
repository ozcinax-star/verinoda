"""The decision record lifecycle: superseding and links written on both records, the table of contents
with its Mermaid graph, and the timeline `verinoda ui` shows (docs/DESIGN.md D33)."""

from __future__ import annotations

import json
import shutil
import subprocess
from importlib import resources
from pathlib import Path

import pytest

from verinoda import decisions as dm
from verinoda import workflow
from verinoda.store import open_store

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false",
                    "-c", "commit.gpgsign=false", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture()
def repo(tmp_path):
    r = tmp_path / "proj"
    (r / "app").mkdir(parents=True)
    (r / "app" / "store.py").write_bytes(b"import sqlite3\n\n\ndef connect():\n    return sqlite3.connect('x.db')\n")
    (r / "docs" / "adr").mkdir(parents=True)
    (r / "docs" / "adr" / "0007-use-sqlite.md").write_bytes(b"# ADR 7: Use SQLite\n\nStatus: accepted\n\nWe use "
                                                             b"SQLite.\n")
    _git(r, "init", "-q")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "init")
    workflow.init(r)
    return r


@pytest.fixture()
def st(repo):
    s = open_store(repo)
    yield s
    s.close()


def _front(repo: Path, rec: dict) -> dict:
    return dm.split_front((repo / rec["file"]).read_text(encoding="utf-8"))[0]


def _set_date(repo: Path, rec: dict, day: str) -> None:
    p = repo / rec["file"]
    text = p.read_text(encoding="utf-8")
    old = next(ln for ln in text.split("\n") if ln.startswith("date: "))
    p.write_bytes(text.replace(old, f"date: {day}", 1).encode("utf-8"))


def test_supersede_updates_both_records_and_logs_both(repo, st):
    a = dm.record(st, repo, chosen="SQLite", rationale="local", guards=["dependency absent=psycopg"])
    b = dm.record(st, repo, chosen="PostgreSQL", rationale="managed")
    res = dm.supersede(st, repo, a["id"], b["id"], user_statement="Postgres replaces SQLite")
    assert res["id"] == b["id"] and res["supersedes"] == a["id"] and res["superseded"] == a["id"]
    assert res["superseded_record"]["status"] == "superseded"
    old, new = _front(repo, a), _front(repo, b)
    assert old["status"] == "superseded" and old["superseded-by"] == b["id"]
    assert new["status"] == "accepted" and new["supersedes"] == a["id"]
    assert not dm.find(repo, a["id"]).enforced and dm.find(repo, b["id"]).enforced
    assert f"- superseded by {b['id']}" in (repo / a["file"]).read_text(encoding="utf-8")
    rows = st.all("SELECT id, event, status, user_statement FROM decisions WHERE event = 'supersede' ORDER BY seq")
    assert [(r["id"], r["status"]) for r in rows] == [(b["id"], "accepted"), (a["id"], "superseded")]
    assert all(r["user_statement"] == "Postgres replaces SQLite" for r in rows)
    # neither record now warns: each states its side
    assert not dm.find(repo, a["id"]).warnings and not dm.find(repo, b["id"]).warnings


def test_supersede_refuses_what_would_leave_the_records_contradicting(repo, st):
    a = dm.record(st, repo, chosen="A", rationale="r")
    b = dm.record(st, repo, chosen="B", rationale="r")
    c = dm.record(st, repo, chosen="C", rationale="r")
    with pytest.raises(dm.DecisionError, match="itself"):
        dm.supersede(st, repo, a["id"], a["id"])
    with pytest.raises(dm.DecisionError, match="no decision record ADR-0099"):
        dm.supersede(st, repo, "ADR-99", a["id"])
    dm.supersede(st, repo, a["id"], b["id"])
    with pytest.raises(dm.DecisionError, match="already superseded by"):
        dm.supersede(st, repo, a["id"], c["id"])
    with pytest.raises(dm.DecisionError, match="status superseded"):
        dm.supersede(st, repo, c["id"], a["id"])  # a superseded record replaces nothing
    with pytest.raises(dm.DecisionError, match="already supersedes"):
        dm.supersede(st, repo, c["id"], b["id"])  # a record replaces one record
    # e says by a hand edit that it supersedes d: d cannot then supersede e
    d = dm.record(st, repo, chosen="D", rationale="r")
    e = dm.record(st, repo, chosen="E", rationale="r")
    p = repo / e["file"]
    p.write_bytes(p.read_bytes().replace(b"supersedes: null", f"supersedes: {d['id']}".encode()))
    with pytest.raises(dm.DecisionError, match="each other"):
        dm.supersede(st, repo, e["id"], d["id"])


def test_supersede_of_an_imported_adr_never_edits_the_document(repo, st):
    doc = repo / "docs" / "adr" / "0007-use-sqlite.md"
    before = doc.read_bytes()
    imp = dm.import_doc(st, repo, "docs/adr/0007-use-sqlite.md")
    new = dm.record(st, repo, chosen="PostgreSQL", rationale="r")
    res = dm.supersede(st, repo, imp["id"], new["id"], user_statement="yes")
    assert doc.read_bytes() == before
    assert any("docs/adr/0007-use-sqlite.md" in n for n in res["not_changed"])
    assert dm.find(repo, imp["id"]).status == "superseded"


def test_a_proposed_record_supersedes_nothing(repo, st):
    a = dm.record(st, repo, chosen="A", rationale="r")
    b = dm.record(st, repo, chosen="B", rationale="r")
    p = repo / b["file"]
    p.write_bytes(p.read_bytes().replace(b"status: accepted", b"status: proposed"))
    with pytest.raises(dm.DecisionError, match="status proposed"):
        dm.supersede(st, repo, a["id"], b["id"])
    assert dm.find(repo, a["id"]).status == "accepted"


def test_link_writes_the_reverse_on_the_other_record(repo, st):
    a = dm.record(st, repo, chosen="A", rationale="r")
    b = dm.record(st, repo, chosen="B", rationale="r")
    res = dm.link(st, repo, b["id"], "amends", a["id"], user_statement="B amends A")
    assert res["linked"] == {"from": b["id"], "kind": "amends", "to": a["id"], "reverse": "amended-by"}
    assert _front(repo, b)["links"] == [{"kind": "amends", "id": a["id"]}]
    assert _front(repo, a)["links"] == [{"kind": "amended-by", "id": b["id"]}]
    with pytest.raises(dm.DecisionError, match="already recorded"):
        dm.link(st, repo, b["id"], "amends", a["id"])
    with pytest.raises(dm.DecisionError, match="already recorded"):
        dm.link(st, repo, a["id"], "amended by", b["id"])  # the same relation named from the other side
    with pytest.raises(dm.DecisionError, match="decide supersede"):
        dm.link(st, repo, a["id"], "supersedes", b["id"])
    with pytest.raises(dm.DecisionError, match="not one of"):
        dm.link(st, repo, a["id"], "likes", b["id"])
    with pytest.raises(dm.DecisionError, match="itself"):
        dm.link(st, repo, a["id"], "relates-to", a["id"])
    # a record may link another in two ways
    dm.link(st, repo, a["id"], "relates_to", b["id"])
    assert {x["kind"] for x in dm.find(repo, b["id"]).links} == {"amends", "relates-to"}
    assert not dm.find(repo, a["id"]).problems and not dm.find(repo, a["id"]).warnings
    assert [r["event"] for r in st.all("SELECT event FROM decisions WHERE event = 'link'")] == ["link"] * 4


def test_a_relation_one_record_states_is_a_warning_not_a_problem(repo, st):
    a = dm.record(st, repo, chosen="A", rationale="r", guards=["dependency absent=psycopg"])
    b = dm.record(st, repo, chosen="B", rationale="r")
    p = repo / b["file"]
    p.write_bytes(p.read_bytes().replace(b"links: []", f'links: [{{"kind": "clarifies", "id": "{a["id"]}"}}, '
                                                           '{"kind": "amends", "id": "ADR-0042"}, '
                                                           '{"kind": "admires", "id": "ADR-0001"}]'.encode()))
    rb = dm.find(repo, b["id"])
    assert rb.enforced and not rb.problems
    assert any("has no link clarified-by" in w for w in rb.warnings)
    assert any("no record ADR-0042" in w for w in rb.warnings)
    assert any("admires" in w and "not one of" in w for w in rb.warnings)
    # the older superseding by hand: only the new record says so
    q = repo / a["file"]
    text = (repo / b["file"]).read_text(encoding="utf-8").replace("supersedes: null", f"supersedes: {a['id']}")
    (repo / b["file"]).write_bytes(text.encode("utf-8"))
    ra = dm.find(repo, a["id"])
    assert not ra.enforced and ra.inactive  # as before: an enforced record superseding it wins
    assert any("does not say superseded-by" in w for w in dm.find(repo, b["id"]).warnings)
    assert q.read_bytes().count(b"status: accepted") == 1
    tl = dm.timeline(repo)
    sup = next(e for e in tl["relations"] if e["kind"] == "supersedes")
    assert sup["one_sided"] and sup["stated_by"] == [b["id"]]
    assert next(e for e in tl["relations"] if e["to"] == "ADR-0042")["missing"] == ["ADR-0042"]
    # the supersede command completes a one-sided relation the new record already states
    dm.supersede(st, repo, a["id"], b["id"])
    assert not any(e["one_sided"] for e in dm.timeline(repo)["relations"] if e["kind"] == "supersedes")


def test_timeline_is_by_date_with_each_relation_once(repo, st):
    a = dm.record(st, repo, chosen="A", rationale="r", title="First")
    b = dm.record(st, repo, chosen="B", rationale="r", title="Second")
    c = dm.record(st, repo, chosen="C", rationale="r", title='Third | "quoted"')
    dm.supersede(st, repo, a["id"], c["id"])
    dm.link(st, repo, b["id"], "relates-to", c["id"])
    dm.link(st, repo, c["id"], "depends-on", b["id"])
    _set_date(repo, a, "2026-01-05")
    _set_date(repo, b, "2026-03-01")
    _set_date(repo, c, "2026-02-10")
    tl = dm.timeline(repo)
    assert [r["id"] for r in tl["records"]] == [a["id"], c["id"], b["id"]]
    assert [r["date"] for r in tl["records"]] == ["2026-01-05", "2026-02-10", "2026-03-01"]
    rels = {(e["from"], e["kind"], e["to"]) for e in tl["relations"]}
    assert rels == {(c["id"], "supersedes", a["id"]), (b["id"], "relates-to", c["id"]),
                    (c["id"], "depends-on", b["id"])}
    assert not any(e["one_sided"] for e in tl["relations"])
    mm = dm.mermaid(tl)
    # numbers start after the hand-written ADR 0007
    assert mm.startswith("graph LR") and "ADR0010 -->|supersedes| ADR0008" in mm
    assert "#quot;quoted#quot;" in mm and "class ADR0008 inactive" in mm
    md = dm.toc_markdown(tl, ".verinoda/decisions")
    assert md.startswith(dm.TOC_MARK) and "```mermaid" in md
    row = next(ln for ln in md.split("\n") if ln.startswith("| 2026-02-10"))
    assert f"({Path(c['file']).name})" in row and "Third \\| " in row
    assert f"supersedes {a['id']}" in row and f"depends-on {b['id']}" in row
    assert f"superseded-by {c['id']}" in next(ln for ln in md.split("\n") if ln.startswith("| 2026-01-05"))
    undated = dm.record(st, repo, chosen="D", rationale="r")
    p = repo / undated["file"]
    p.write_bytes(p.read_bytes().replace(b"date: 20", b"date: 'soon' 20"))
    tl = dm.timeline(repo)
    assert tl["records"][-1]["id"] == undated["id"] and tl["undated"] == [undated["id"]]


def test_toc_write_never_overwrites_a_file_it_did_not_write(repo, st):
    dm.record(st, repo, chosen="A", rationale="r")
    readme = repo / "docs" / "adr" / "README.md"
    readme.write_bytes(b"# our ADRs\n")
    with pytest.raises(dm.DecisionError, match="not overwritten"):
        dm.write_toc(repo, "docs/adr/README.md")
    assert readme.read_bytes() == b"# our ADRs\n"
    for bad in ("../toc.md", "/tmp/toc.md", "docs/toc.txt"):
        with pytest.raises(dm.DecisionError):
            dm.write_toc(repo, bad)
    res = dm.write_toc(repo, ".verinoda/decisions/README.md")
    assert res["written"] == ".verinoda/decisions/README.md"
    out = repo / ".verinoda" / "decisions" / "README.md"
    assert b"\r" not in out.read_bytes() and "(ADR-0008-" in out.read_text(encoding="utf-8")
    dm.write_toc(repo, ".verinoda/decisions/README.md")  # its own file: rewritten
    # in the decisions folder it is no record, and elsewhere it is no hand-written ADR without a record
    assert [d.id for d in dm.load_all(repo)] == ["ADR-0008"]
    dm.write_toc(repo, "docs/adr/index.md")
    assert "docs/adr/index.md" not in dm.listing(st, repo)["unrecorded_docs"]


def test_cli_supersede_link_and_toc(repo, st, capsys):
    from verinoda import cli

    a = dm.record(st, repo, chosen="A", rationale="r")
    b = dm.record(st, repo, chosen="B", rationale="r")
    capsys.readouterr()
    assert cli.main(["decide", "supersede", a["id"], "--by", b["id"], "--said", "B now", "--repo", str(repo),
                     "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["superseded"] == a["id"] and out["superseded_record"]["superseded_by"] == b["id"]
    assert cli.main(["decide", "supersede", a["id"], "--by", b["id"], "--repo", str(repo)]) == 2
    assert "already recorded on both records" in capsys.readouterr().err
    assert cli.main(["decide", "link", "8", "clarifies", "9", "--repo", str(repo)]) == 0
    assert f"{b['id']} clarified-by {a['id']}" in capsys.readouterr().out
    assert cli.main(["decide", "toc", "--repo", str(repo)]) == 0
    text = capsys.readouterr().out
    assert "| Date | Record | Status | Relations |" in text and "ADR0009 -->|supersedes| ADR0008" in text
    assert cli.main(["decide", "toc", "--json", "--repo", str(repo)]) == 0
    tl = json.loads(capsys.readouterr().out)
    assert {r["id"] for r in tl["records"]} == {a["id"], b["id"]} and len(tl["relations"]) == 2
    assert cli.main(["decide", "toc", "--write", "docs/decisions.md", "--repo", str(repo)]) == 0
    assert "wrote docs/decisions.md" in capsys.readouterr().out
    assert "(../.verinoda/decisions/ADR-0008-" in (repo / "docs" / "decisions.md").read_text(encoding="utf-8")
    assert cli.main(["decide", "list", "--repo", str(repo)]) == 0
    assert f"clarifies {b['id']}" in capsys.readouterr().out
    # an error of toc --json is JSON on stdout too, as for decide check
    assert cli.main(["decide", "toc", "--json", "--write", "../../outside.md", "--repo", str(repo)]) == 2
    captured = capsys.readouterr()
    assert json.loads(captured.out)["status"] == "error" and "outside" in captured.err


def test_mcp_supersede_and_link_need_the_users_words(repo, st):
    from verinoda.mcp.server import AtlasTools

    a = dm.record(st, repo, chosen="A", rationale="r")
    b = dm.record(st, repo, chosen="B", rationale="r")
    t = AtlasTools(repo)
    assert t.decision_record("supersede", decision_id=b["id"], supersedes=a["id"])["error"] == \
        "user_statement_required"
    ok = t.decision_record("supersede", decision_id=b["id"], supersedes=a["id"], user_statement="B replaces A")
    assert ok["superseded"] == a["id"]
    assert t.decision_record("link", decision_id=b["id"], link="amends", user_statement="y")["error"] == \
        "invalid_argument"
    ok = t.decision_record("link", decision_id=b["id"], link=f"amends {a['id']}", user_statement="y")
    assert ok["linked"]["reverse"] == "amended-by"


def test_the_ui_timeline_reads_the_records_without_an_index(repo, st):
    from verinoda.ui.data import Atlas

    assert Atlas(repo).decisions()["records"] == []
    a = dm.record(st, repo, chosen="A", rationale="r")
    b = dm.record(st, repo, chosen="B", rationale="r")
    dm.supersede(st, repo, a["id"], b["id"])
    res = Atlas(repo).decisions()
    assert [r["status"] for r in res["records"]] == ["superseded", "accepted"]
    assert "ADR0009 -->|supersedes| ADR0008" in res["mermaid"]
    js = resources.files("verinoda.ui").joinpath("static", "app.js").read_text(encoding="utf-8")
    assert 'h === "#/d") renderDecisions()' in js and 'case "/api/decisions"' in js


def test_supersede_completes_a_relation_only_the_old_record_states(repo, st):
    a = dm.record(st, repo, chosen="A", rationale="r")
    b = dm.record(st, repo, chosen="B", rationale="r")
    p = repo / a["file"]
    p.write_bytes(p.read_bytes().replace(b"status: accepted", b"status: superseded")
                  .replace(b"superseded-by: null", f"superseded-by: {b['id']}".encode()))
    assert any("does not say supersedes" in w for w in dm.find(repo, a["id"]).warnings)
    dm.supersede(st, repo, a["id"], b["id"], user_statement="as the file says")
    assert _front(repo, b)["supersedes"] == a["id"]
    assert not dm.find(repo, a["id"]).warnings and not dm.find(repo, b["id"]).warnings
    with pytest.raises(dm.DecisionError, match="already recorded on both records"):
        dm.supersede(st, repo, a["id"], b["id"])
    c = dm.record(st, repo, chosen="C", rationale="r")
    with pytest.raises(dm.DecisionError, match="already superseded by"):
        dm.supersede(st, repo, a["id"], c["id"])  # superseded by another record: still refused


def test_a_malformed_link_entry_is_a_problem_of_its_record_only(repo, st, capsys):
    from verinoda import cli

    a = dm.record(st, repo, chosen="A", rationale="r")
    b = dm.record(st, repo, chosen="B", rationale="r")
    c = dm.record(st, repo, chosen="C", rationale="r")
    dm.link(st, repo, a["id"], "amends", b["id"])
    p = repo / c["file"]
    clean = p.read_bytes()
    for bad in (f'[{{"id": "{a["id"]}"}}]', f'[{{"kind": ["x"], "id": "{a["id"]}"}}]',
                '[{"kind": "amends", "id": 7}]'):
        p.write_bytes(clean.replace(b"links: []", f"links: {bad}".encode()))
        recs = {d.id: d for d in dm.load_all(repo)}
        assert any("links entry" in x for x in recs[c["id"]].problems)
        assert not recs[a["id"]].problems and not recs[a["id"]].warnings
        assert dm.timeline(repo)["relations"]
        capsys.readouterr()
        assert cli.main(["decide", "check", "--json", "--repo", str(repo)]) != 2
        assert cli.main(["decide", "list", "--repo", str(repo)]) == 0
        assert cli.main(["decide", "toc", "--repo", str(repo)]) == 0


def test_mcp_link_reads_a_kind_of_two_words(repo, st):
    from verinoda.mcp.server import AtlasTools

    a = dm.record(st, repo, chosen="A", rationale="r")
    b = dm.record(st, repo, chosen="B", rationale="r")
    t = AtlasTools(repo)
    ok = t.decision_record("link", decision_id=b["id"], link=f"amended by {a['id']}", user_statement="y")
    assert ok["linked"] == {"from": b["id"], "kind": "amended-by", "to": a["id"], "reverse": "amends"}
    ok = t.decision_record("link", decision_id=b["id"], link=f"relates to {a['id']}", user_statement="y")
    assert ok["linked"]["kind"] == "relates-to"


def test_toc_links_survive_brackets_and_spaces_and_mermaid_odd_ids(repo, st):
    tl = {"dir": "docs/my decisions", "relations": [],
          "records": [{"id": "ADR-0001", "title": "Use A [draft", "status": "accepted", "enforced": True,
                       "date": "2026-10-01", "file": "docs/my decisions/ADR-0001-use-a-draft.md"}]}
    row = next(ln for ln in dm.toc_markdown(tl, "docs").split("\n") if ln.startswith("| 2026-10-01"))
    assert r"[ADR-0001: Use A \[draft](my%20decisions/ADR-0001-use-a-draft.md)" in row
    dm.record(st, repo, chosen="A", rationale="r")
    (repo / ".verinoda" / "decisions" / "weird.md").write_bytes(
        b"---\nverinoda-decision: 1\nid: my id; x\ntitle: T\n---\n")
    mm = dm.mermaid(dm.timeline(repo))
    assert '  rec1["my id; x: T' in mm and "\n  my id" not in mm
    assert "  ADR0008[" in mm
