"""Derived facts (verinoda/facts.py): a claim or a search kept under a name, stale with the code it rests on,
recomputed on refresh, never stronger than its inputs."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import json  # noqa: E402
import shutil  # noqa: E402
import sqlite3  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import cli, facts, workflow  # noqa: E402
from verinoda import evidence as evmod  # noqa: E402
from verinoda.claims import Claims  # noqa: E402
from verinoda.store import SCHEMA_VERSION, Store, open_store  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false",
                    "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main", *args], cwd=cwd, check=True,
                   capture_output=True, stdin=subprocess.DEVNULL)


def _write(repo: Path, rel: str, text: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


def _config(repo: Path, **facts_cfg) -> None:
    cfg = repo / ".verinoda" / "config.json"
    data = json.loads(cfg.read_text(encoding="utf-8"))
    data["facts"] = facts_cfg
    cfg.write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture()
def proj(tmp_path):
    repo = tmp_path / "facts ğ"
    repo.mkdir()
    _write(repo, ".gitignore", ".verinoda/\n")
    _write(repo, "a.py", "def a():\n    return 1\n")
    _write(repo, "b.py", "def b():\n    return 2\n")
    _write(repo, "pkg/c.py", "def c():\n    return 3\n")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "one")
    workflow.init(repo)
    st = open_store(repo)
    workflow.scan(st, repo)
    yield repo, st
    st.close()


def _claim(repo, st, path, text, start=1, end=2, status="statically_verified"):
    snap = st.latest_snapshot()
    ev = evmod.source_evidence(repo, path, start, end, commit=snap["commit_sha"])
    return Claims(st, repo).create(text, project="p", snapshot=snap, status=status, evidence=[(ev, "supports")],
                                   subjects=[path])


def _run(capsys, *argv) -> tuple[int, dict]:
    capsys.readouterr()
    code = cli.main(list(argv))
    return code, json.loads(capsys.readouterr().out)


def test_a_fact_goes_stale_with_the_code_it_rests_on(proj):
    repo, st = proj
    c = _claim(repo, st, "a.py", "a() returns 1")
    status = st.claim(c["id"])["status"]
    f = facts.add_derived(st, repo, "a-returns-one", claims=[c["id"]])
    assert f["status"] == status and f["statement"] == "a() returns 1"
    _write(repo, "b.py", "def b():\n    return 22\n")          # code the fact does not rest on
    res = workflow.update(st, repo)
    assert facts.get(st, "a-returns-one")["status"] == status
    _write(repo, "a.py", "def a():\n    return 5\n")           # the code it rests on
    res = workflow.update(st, repo)
    assert st.claim(c["id"])["status"] == "stale"
    row = facts.get(st, "a-returns-one")
    assert row["status"] == "stale" and c["id"] in row["reason"]
    assert [x["fact"] for x in res["facts"]["lowered"]] == ["a-returns-one"]
    assert res["facts"]["waiting"] == ["a-returns-one"]        # update never runs verify: it waits for refresh
    hist = facts.history(st, row["id"])
    assert [h["to_status"] for h in hist] == [status, "stale"] and hist[-1]["actor"] == "update"


def test_refresh_restores_a_fact_when_the_code_still_supports_it(proj):
    repo, st = proj
    c = _claim(repo, st, "a.py", "a() returns 1")
    status = st.claim(c["id"])["status"]
    facts.add_derived(st, repo, "a1", claims=[c["id"]])
    _write(repo, "a.py", "def a():\n    return 5\n")
    workflow.update(st, repo)
    res = facts.refresh(st, repo)
    assert res["still_stale"] == ["a1"] and res["refreshed"][0]["verified"][0]["after"] == "stale"
    _write(repo, "a.py", "def a():\n    return 1\n")           # the code is back
    res = facts.refresh(st, repo)
    (row,) = res["refreshed"]
    assert res["restored"] == ["a1"] and row["now"] == status and not row["changed"]
    assert facts.get(st, "a1")["status"] == status == st.claim(c["id"])["status"]


def test_a_search_fact_is_stale_when_its_scope_changes_and_refresh_restores_it(proj):
    repo, st = proj
    _config(repo, refresh_on_update=False)
    f = facts.add_search(st, repo, "defs", r"^def \w+", cwd=repo)
    assert f["status"] == "observed" and f["total"] == 3 and f["files_matched"] == 3
    _write(repo, "b.py", "def b():\n    return 2\n# a note\n")   # a matched file changed, not its match
    res = workflow.update(st, repo)
    assert facts.get(st, "defs")["status"] == "stale" and "b.py" in res["facts"]["lowered"][0]["why"]
    out = facts.refresh(st, repo)
    assert out["restored"] == ["defs"] and out["changed"] == []
    assert facts.get(st, "defs")["status"] == "observed"
    _write(repo, "notes.txt", "nothing here\n")                 # a new file in the scope: a match may appear
    workflow.update(st, repo)
    assert "scope" in facts.get(st, "defs")["reason"]


def test_a_search_limited_to_a_folder_ignores_changes_elsewhere(proj):
    repo, st = proj
    facts.add_search(st, repo, "pkg-defs", "def ", fixed=True, paths=["pkg"], cwd=repo)
    _write(repo, "a.py", "def a():\n    return 7\n")
    res = workflow.update(st, repo)
    assert facts.get(st, "pkg-defs")["status"] == "observed" and not res["facts"]["lowered"]


def test_update_recomputes_and_reports_a_changed_result(proj):
    repo, st = proj
    facts.add_search(st, repo, "returns", r"return \d", cwd=repo)
    top = facts.add_derived(st, repo, "on-returns", facts=["returns"])
    assert top["status"] == "observed"
    _write(repo, "d.py", "def d():\n    return 4\n")
    res = workflow.update(st, repo)
    fx = res["facts"]
    assert set(fx["changed"]) == {"returns", "on-returns"}
    ch = {x["fact"]: x for x in fx["changes"]}
    assert ch["returns"]["old"]["total"] == 3 and ch["returns"]["new"]["total"] == 4
    assert ch["returns"]["added"] == ["d.py: return 4"] and ch["returns"]["removed"] == []
    old, new = ch["on-returns"]["old"]["statement"], ch["on-returns"]["new"]["statement"]
    assert "3 match(es)" in old and "4 match(es)" in new
    shown = facts.show(st, "returns")
    assert shown["status"] == "observed" and shown["history"][-1]["payload"]["added"] == ["d.py: return 4"]
    _write(repo, "a.py", "\n\ndef a():\n    return 1\n")          # a line that only moved is not a change
    res = workflow.update(st, repo)
    assert "returns" not in res["facts"]["changed"]


def test_status_is_capped_by_the_weakest_input_and_never_raised_by_derivation(proj):
    repo, st = proj
    strong = _claim(repo, st, "a.py", "a() returns 1")
    weak = _claim(repo, st, "b.py", "b returns two somewhere", status="weak_inference")
    ws = st.claim(weak["id"])["status"]
    assert ws in ("weak_inference", "unknown") and st.claim(strong["id"])["status"] != ws
    f = facts.add_derived(st, repo, "both", claims=[strong["id"], weak["id"]])
    assert f["status"] == ws and weak["id"] in f["reason"]
    on = facts.add_derived(st, repo, "on-both", facts=["both"], claims=[strong["id"]])
    assert on["status"] == ws                                    # a fact on a fact: no stronger
    assert facts.refresh(st, repo, ["on-both"])["refreshed"][0]["now"] == ws
    # an input lowered by something other than an update (verify): shown capped at once, recorded at the next one
    Claims(st, repo).set_status(strong["id"], "stale", reason="verify: cited source changed", actor="verify")
    view = facts.listing(st)
    assert {x["name"]: x["status"] for x in view["facts"]} == {"both": "stale", "on-both": "stale"}
    assert facts.get(st, "both")["status"] == ws                 # nothing written by a read
    low = facts.invalidate(st, repo)
    assert {x["fact"]: x["now"] for x in low} == {"both": "stale", "on-both": "stale"}
    assert facts.weakest(["statically_verified", "observed", "strong_inference"]) == "strong_inference"
    assert facts.weakest(["stale", "contradicted"]) == "contradicted" and facts.weakest([]) == "unknown"


def test_a_superseded_claim_and_a_retired_fact_leave_their_users_stale(proj):
    repo, st = proj
    c = _claim(repo, st, "a.py", "a() returns 1")
    facts.add_derived(st, repo, "base", claims=[c["id"]])
    facts.add_derived(st, repo, "user", facts=["base"])
    ret = facts.retire(st, "base", reason="replaced")
    assert ret["now_stale"] == ["user"]
    facts.invalidate(st, repo)
    assert facts.get(st, "user")["status"] == "stale" and "retired" in facts.get(st, "user")["reason"]
    facts.add_derived(st, repo, "base", claims=[c["id"]])         # the name is free again
    with pytest.raises(facts.FactError):
        facts.add_derived(st, repo, "base", claims=[c["id"]])
    with pytest.raises(sqlite3.IntegrityError):
        st.conn.execute("DELETE FROM facts")
    with pytest.raises(sqlite3.IntegrityError):
        st.conn.execute("UPDATE facts SET definition = '{}'")
    Claims(st, repo).supersede(c["id"], "a() returns one", evidence=[], status="weak_inference", reason="test",
                               actor="test", snapshot=st.latest_snapshot())
    with pytest.raises(facts.FactError):
        facts.add_derived(st, repo, "x", claims=[c["id"]])
    facts.invalidate(st, repo)
    assert facts.get(st, "base")["status"] == "stale" and "superseded" in facts.get(st, "base")["reason"]


def test_cli_json_and_the_query_lead(proj, capsys):
    repo, st = proj
    c = _claim(repo, st, "a.py", "a() returns 1")
    code, out = _run(capsys, "fact", "add", "answer-of-a", "--from-claim", c["id"], "--repo", str(repo), "--json")
    assert code == 0 and out["name"] == "answer-of-a" and out["inputs"][0]["id"] == c["id"]
    assert out["evidence"] == ["a.py:1-2"]
    code, out = _run(capsys, "fact", "add", "defs", "--search", "def ", "-F", "--repo", str(repo), "--json")
    assert code == 0 and out["status"] == "observed" and len(out["result"]["sites"]) == 3
    code, out = _run(capsys, "fact", "list", "--repo", str(repo), "--json")
    assert code == 0 and [f["name"] for f in out["facts"]] == ["answer-of-a", "defs"]
    code, out = _run(capsys, "fact", "add", "bad name", "--search", "x", "--repo", str(repo), "--json")
    assert code == 2 and out["status"] == "error"
    code, out = _run(capsys, "fact", "add", "both", "--search", "x", "--from-claim", c["id"], "--repo", str(repo),
                     "--json")
    assert code == 2
    code, out = _run(capsys, "fact", "refresh", "defs", "--repo", str(repo), "--json")
    assert code == 0 and out["refreshed"][0]["fact"] == "defs" and out["refreshed"][0]["changed"] is False
    code, out = _run(capsys, "query", "what is the answer of a", "--repo", str(repo), "--json")
    assert code == 0 and out["facts"][0]["fact"] == "answer-of-a" and out["facts"][0]["at"] == ["a.py:1-2"]
    capsys.readouterr()
    assert cli.main(["query", "what is the answer of a", "--repo", str(repo)]) == 0
    assert "answer-of-a [" in capsys.readouterr().out
    _write(repo, "a.py", "def a():\n    return 5\n")
    capsys.readouterr()
    assert cli.main(["update", str(repo)]) == 0
    assert "answer-of-a -> stale" in capsys.readouterr().out
    code, out = _run(capsys, "query", "what is the answer of a", "--repo", str(repo), "--json")
    assert out["facts"][0]["status"] == "stale" and out["facts"][0]["note"]


def test_analyze_lists_the_facts_a_question_names(proj):
    from verinoda import analysis
    from verinoda.analysis_view import lean, render_text

    repo, st = proj
    c = _claim(repo, st, "a.py", "a() returns 1")
    facts.add_derived(st, repo, "answer-of-a", claims=[c["id"]])
    res = analysis.analyze(st, repo, "where is the answer of a computed?", challenge=False)
    assert res["facts"][0]["fact"] == "answer-of-a"
    assert lean(res)["facts"] == res["facts"] and "answer-of-a [" in render_text(res)


def test_a_changed_result_reaches_the_facts_resting_on_it(proj):
    repo, st = proj
    _config(repo, refresh_on_update=False)
    facts.add_search(st, repo, "returns", r"return \d", cwd=repo)
    facts.add_derived(st, repo, "on-returns", facts=["returns"])
    _write(repo, "d.py", "def d():\n    return 4\n")          # no update ran: the fact is refreshed by name
    res = facts.refresh(st, repo, ["returns"])
    assert [r["fact"] for r in res["refreshed"]] == ["returns", "on-returns"]
    assert res["changed"] == ["returns", "on-returns"]
    assert "4 match(es)" in facts.get(st, "on-returns")["result"]["statement"]
    hist = facts.history(st, facts.get(st, "on-returns")["id"])
    assert [h["to_status"] for h in hist][-2:] == ["stale", "observed"]


def test_leads_need_the_fact_named_not_template_words(proj):
    repo, st = proj
    facts.add_search(st, repo, "storage-writes", "write(", fixed=True, cwd=repo)
    assert facts.leads(st, "which file matches the pattern in the project lines") == []
    assert [x["fact"] for x in facts.leads(st, "where are the storage writes?")] == ["storage-writes"]


# -- review round ------------------------------------------------------------------------------------------

def test_an_unfinished_search_keeps_the_old_result_stale_and_a_short_budget_stops_the_queue(proj, monkeypatch):
    from verinoda import trigram

    repo, st = proj
    _config(repo, refresh_on_update=False)
    facts.add_search(st, repo, "returns", r"return \d", cwd=repo)
    facts.add_derived(st, repo, "on-returns", facts=["returns"])
    _write(repo, "d.py", "def d():\n    return 4\n")
    workflow.update(st, repo)
    assert facts.get(st, "returns")["status"] == "stale"
    real = trigram.search

    def cut(*a, **k):
        res = real(*a, **k)
        return {**res, "status": "incomplete", "matches": res["matches"][:1], "total": 1, "files_matched": 1,
                "not_read": {"total": 3, "files": ["a.py"], "why": "the time limit ran out"}}
    monkeypatch.setattr(trigram, "search", cut)
    out = facts.refresh(st, repo)
    row = out["refreshed"][0]
    assert row["fact"] == "returns" and row["now"] == "stale" and not row["changed"] and row["incomplete"]
    assert out["restored"] == [] and out["changed"] == [] and out["incomplete"] == ["returns"]
    kept = facts.get(st, "returns")
    assert kept["status"] == "stale" and kept["result"]["total"] == 3          # the old, complete result
    dep = facts.get(st, "on-returns")
    assert "changed its result" not in dep["reason"] and "3 match(es)" in dep["result"]["statement"]
    calls = []
    monkeypatch.setattr(trigram, "search", lambda *a, **k: calls.append(a) or real(*a, **k))
    out = facts.refresh(st, repo, budget=facts.MIN_SEARCH_S / 2)
    assert calls == [] and out["stopped"] and "returns" in out["not_reached"] and out["refreshed"] == []


def test_files_the_search_skips_make_it_weak_and_named(proj):
    repo, st = proj
    (repo / "dump.sql").write_bytes(b"MARKER_X here\0binary\n" + b"x" * 50)
    f = facts.add_search(st, repo, "marker", "MARKER_X", fixed=True, cwd=repo)
    shown = facts.show(st, "marker")
    assert f["status"] == "weak_inference" and f["total"] == 0 and not f["complete"]
    assert shown["result"]["skipped"]["files"] == ["dump.sql"] and "dump.sql" in shown["reason"]
    facts.add_derived(st, repo, "on-marker", facts=["marker"])
    assert facts.get(st, "on-marker")["status"] == "weak_inference"
    (repo / "big.sql").write_bytes(b"-- MARKER_X\n" + b"insert into t values (1);\n" * 105_000)   # over 2 MB
    (repo / "dump.sql").unlink()
    res = workflow.update(st, repo)
    big = facts.get(st, "marker")
    assert big["status"] == "weak_inference" and big["result"]["total"] == 0
    assert big["result"]["skipped"]["files"] == ["big.sql"]
    (repo / "big.sql").unlink()
    res = workflow.update(st, repo)                    # inputs recovered: both recomputed and raised
    assert facts.get(st, "marker")["status"] == "observed"
    assert facts.get(st, "on-marker")["status"] == "observed" and "on-marker" in res["facts"]["restored"]


def test_facts_that_cannot_recover_stay_out_of_the_automatic_queue(proj):
    repo, st = proj
    c = _claim(repo, st, "a.py", "a() returns 1")
    facts.add_derived(st, repo, "base", claims=[c["id"]])
    for k in range(3):
        facts.add_derived(st, repo, f"lost{k}", facts=["base"])
    facts.add_search(st, repo, "defs", "def ", fixed=True, cwd=repo)
    facts.retire(st, "base")
    facts.invalidate(st, repo)
    _write(repo, "b.py", "def b():\n    return 2\n# note\n")
    facts.invalidate(st, repo)
    assert facts.get(st, "defs")["status"] == "stale"
    out = facts.refresh(st, repo, limit=1, verify_claims=False)
    assert [r["fact"] for r in out["refreshed"]] == ["defs"] and out["restored"] == ["defs"]
    assert sorted(x["fact"] for x in out["cannot_recover"]) == ["lost0", "lost1", "lost2"]
    line = facts.update_line({"lowered": [], "cannot_recover": out["cannot_recover"], "not_reached": ["x"],
                              "seconds": 0.1})
    assert "3 cannot recover" in line and "re-verifies their claims" not in line and "(budget)" not in line
    assert "limit of 20" in line


def test_a_read_shows_a_search_fact_stale_once_a_matched_file_changed(proj):
    repo, st = proj
    facts.add_search(st, repo, "defs", "def ", fixed=True, cwd=repo)
    facts.add_derived(st, repo, "on-defs", facts=["defs"])
    _write(repo, "a.py", "def a():\n    return 9\n")      # no update ran
    view = {x["name"]: x for x in facts.listing(st, repo=repo)["facts"]}
    assert view["defs"]["status"] == "stale" and "a.py" in view["defs"]["reason"]
    assert view["on-defs"]["status"] == "stale" and view["defs"]["recorded_status"] == "observed"
    assert facts.get(st, "defs")["status"] == "observed"     # nothing written
    assert facts.leads(st, "where is defs", repo=repo)[0]["status"] == "stale"


def test_writes_hold_across_connections(proj, capsys):
    repo, st = proj
    c = _claim(repo, st, "a.py", "a() returns 1")
    facts.add_derived(st, repo, "x", claims=[c["id"]])
    row = facts.get(st, "x")
    other = open_store(repo)
    try:
        facts.retire(other, "x")
        facts._record(st, row, status="stale", reason="late", actor="t")    # read before the retire
        assert facts._by_id(st, row["id"])["status"] != "stale"
        assert [h["reason"] for h in facts.history(st, row["id"])][-1] == "retired"
        with pytest.raises(facts.FactError):
            facts.retire(st, "x")                          # the second retire: refused, not a traceback
        with st.tx(immediate=True):                        # the write lock is taken at the start
            other.conn.execute("PRAGMA busy_timeout = 0")
            with pytest.raises(sqlite3.OperationalError):
                other.conn.execute("BEGIN IMMEDIATE")
    finally:
        other.close()
    facts.add_derived(st, repo, "y", claims=[c["id"]])
    orig = facts._check_name
    facts._check_name = lambda store, name: None           # as if another process took the name meanwhile
    try:
        with pytest.raises(facts.FactError):
            facts.add_derived(st, repo, "y", claims=[c["id"]])
    finally:
        facts._check_name = orig
    code, out = _run(capsys, "fact", "list", "--status", "bogus", "--repo", str(repo), "--json")
    assert code == 2 and "unknown status" in out["error"]


def test_a_fast_update_only_lowers_facts_and_the_facts_time_is_reported(proj):
    repo, st = proj
    facts.add_search(st, repo, "defs", "def ", fixed=True, cwd=repo)
    _write(repo, "a.py", "def a():\n    return 9\n")
    out = facts.after_update(st, repo, {"mode": "incremental", "index_mode": "deferred",
                                        "snapshot": st.latest_snapshot()})
    assert [x["fact"] for x in out["lowered"]] == ["defs"] and "restored" not in out and out["skipped"]
    assert facts.get(st, "defs")["status"] == "stale" and out["seconds"] >= 0
    res = workflow.update(st, repo)
    assert res["facts"]["restored"] == ["defs"] and res["facts"]["seconds"] > 0


def test_a_lead_needs_the_whole_name(proj):
    repo, st = proj
    facts.add_search(st, repo, "db", "def ", fixed=True, cwd=repo)
    assert facts.leads(st, "how is feedback stored?") == []
    assert [x["fact"] for x in facts.leads(st, "what does db say?")] == ["db"]


def test_an_older_store_is_migrated(tmp_path):
    db = tmp_path / "atlas.db"
    st = Store(db)
    st.conn.executescript("DROP TABLE fact_history; DROP TABLE facts;"
                          "UPDATE meta SET value = '9' WHERE key = 'schema_version';")
    st.conn.commit()
    st.close()
    st = Store(db)
    try:
        assert st.one("SELECT value FROM meta WHERE key = 'schema_version'")["value"] == str(SCHEMA_VERSION)
        assert st.all("SELECT * FROM facts") == [] and st.all("SELECT * FROM fact_history") == []
    finally:
        st.close()
