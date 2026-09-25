"""Store: schema creation/migration, schema_version, and the no-delete / append-only triggers."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import sqlite3  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import store as storemod  # noqa: E402
from verinoda.store import SCHEMA_VERSION, Store, new_id, now, open_store  # noqa: E402

TABLES = {"meta", "snapshots", "snapshot_files", "claims", "evidence", "claim_evidence", "claim_history",
          "feedback", "experiments", "research", "memory", "analyses"}


def _tables(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _triggers(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}


def _version(conn) -> int:
    return int(conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0])


def _seed(st: Store) -> dict:
    """One row in every table that must never lose rows."""
    ts = now()
    snap = {"id": new_id("snp"), "repo_root": "/r", "project": "p", "commit_sha": None, "branch": None,
            "dirty": 0, "tree_hash": "t", "file_count": 1, "created_at": ts}
    st.add_snapshot(snap, {"a.py": "0" * 64})
    cid = new_id("clm")
    st.insert_claim({"id": cid, "text": "x", "project": "p", "snapshot_id": snap["id"], "status": "unknown",
                     "confidence": 0.0, "created_at": ts, "updated_at": ts})
    eid = st.add_evidence({"source_type": "source_code", "locator": "a.py:1", "path": "a.py", "line_start": 1,
                           "line_end": 1, "content_hash": "sha256:x", "meta": {}})
    st.link(cid, eid, "supports")
    st.record_transition(cid, from_status="unknown", to_status="weak_inference", from_conf=0.0, to_conf=0.4,
                         reason="r", actor="test")
    fid, xid, mid, aid, rid = (new_id(p) for p in ("fbk", "exp", "mem", "ana", "res"))
    st.insert("feedback", {"id": fid, "claim_id": cid, "text": "no", "created_at": ts, "updated_at": ts})
    st.insert("experiments", {"id": xid, "hypothesis": "h", "command": ["pytest"], "cwd": ".",
                              "isolation": "process", "timeout_s": 1.0, "status": "pass", "created_at": ts})
    st.insert("memory", {"id": mid, "key": "k", "value": "v", "version": 1, "source_claim_id": cid,
                         "created_at": ts})
    st.insert("analyses", {"id": aid, "question": "q", "budget": {}, "usage": {}, "result": {}, "created_at": ts})
    st.insert("research", {"id": rid, "reference": "r", "kind": "paper", "status": "ok", "created_at": ts})
    return {"claim": cid, "evidence": eid, "snapshot": snap["id"], "feedback": fid, "experiment": xid,
            "memory": mid, "analysis": aid, "research": rid}


@pytest.fixture
def st(tmp_path):
    s = Store(tmp_path / "atlas.db")
    yield s
    s.close()


def test_schema_created_with_version_and_triggers(st):
    assert TABLES <= _tables(st.conn)
    assert _version(st.conn) == SCHEMA_VERSION >= 2
    trig = _triggers(st.conn)
    for name in ("no_delete_history", "no_update_history", "no_delete_claims", "no_delete_evidence",
                 "no_delete_feedback", "no_delete_memory", "no_delete_claim_evidence", "no_update_claim_evidence",
                 "no_delete_snapshots", "no_delete_experiments", "no_delete_analyses", "no_delete_research"):
        assert name in trig, name
    assert st.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_reopening_is_idempotent(tmp_path):
    p = tmp_path / "atlas.db"
    s1 = Store(p)
    ids = _seed(s1)
    s1.close()
    s2 = Store(p)
    assert _version(s2.conn) == SCHEMA_VERSION
    assert s2.claim(ids["claim"])["text"] == "x"
    assert len(s2.history(ids["claim"])) == 2
    s2.close()


def test_migrates_a_v1_database_keeping_data(tmp_path):
    p = tmp_path / "old.db"
    conn = sqlite3.connect(p)
    conn.executescript(storemod._MIGRATIONS[1])
    conn.execute("INSERT INTO meta(key, value) VALUES ('schema_version', '1')")
    ts = now()
    conn.execute("INSERT INTO claims (id, text, project, status, confidence, created_at, updated_at)"
                 " VALUES ('clm_old', 'legacy', 'p', 'weak_inference', 0.4, ?, ?)", (ts, ts))
    conn.commit()
    assert "no_delete_claim_evidence" not in _triggers(conn)
    conn.close()

    st = Store(p)
    assert _version(st.conn) == SCHEMA_VERSION
    assert st.claim("clm_old")["text"] == "legacy"
    assert "no_delete_claim_evidence" in _triggers(st.conn)
    st.close()


def test_migrates_a_database_without_meta(tmp_path):
    p = tmp_path / "bare.db"
    sqlite3.connect(p).close()  # empty file, no tables at all
    st = Store(p)
    assert TABLES <= _tables(st.conn) and _version(st.conn) == SCHEMA_VERSION
    st.close()


def test_refuses_a_newer_schema(tmp_path):
    p = tmp_path / "future.db"
    conn = sqlite3.connect(p)
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("INSERT INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION + 1),))
    conn.commit()
    conn.close()
    with pytest.raises(RuntimeError, match="newer than this Verinoda"):
        Store(p)


@pytest.mark.parametrize("table,key", [
    ("claims", "claim"), ("evidence", "evidence"), ("feedback", "feedback"), ("memory", "memory"),
    ("snapshots", "snapshot"), ("experiments", "experiment"), ("analyses", "analysis"), ("research", "research"),
])
def test_deletes_are_rejected_by_triggers(st, table, key):
    ids = _seed(st)
    with pytest.raises(sqlite3.DatabaseError, match="never deleted|invalidated, never deleted"):
        st.conn.execute(f"DELETE FROM {table} WHERE id = ?", (ids[key],))
    st.conn.rollback()
    assert st.one(f"SELECT COUNT(*) AS n FROM {table} WHERE id = ?", (ids[key],))["n"] == 1


def test_claim_history_and_links_are_append_only(st):
    ids = _seed(st)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        st.conn.execute("DELETE FROM claim_history WHERE claim_id = ?", (ids["claim"],))
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        st.conn.execute("UPDATE claim_history SET reason = 'rewritten' WHERE claim_id = ?", (ids["claim"],))
    with pytest.raises(sqlite3.DatabaseError, match="never deleted"):
        st.conn.execute("DELETE FROM claim_evidence WHERE claim_id = ?", (ids["claim"],))
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        st.conn.execute("UPDATE claim_evidence SET relation = 'refutes' WHERE claim_id = ?", (ids["claim"],))
    with pytest.raises(sqlite3.DatabaseError, match="never deleted"):
        st.conn.execute("DELETE FROM snapshot_files")
    st.conn.rollback()
    assert [h["reason"] for h in st.history(ids["claim"])] == ["created", "r"]
    assert [e["relation"] for e in st.claim_evidence(ids["claim"])] == ["supports"]


def test_record_transition_updates_claim_and_appends_history(st):
    ids = _seed(st)
    st.record_transition(ids["claim"], from_status="weak_inference", to_status="stale", from_conf=0.4,
                         to_conf=0.3, reason="file changed", actor="test", payload={"changed": ["a.py"]},
                         extra_fields={"valid_env": "py3"})
    c = st.claim(ids["claim"])
    assert (c["status"], c["confidence"], c["valid_env"]) == ("stale", 0.3, "py3")
    h = st.history(ids["claim"])
    assert [x["to_status"] for x in h] == ["unknown", "weak_inference", "stale"]
    assert h[-1]["payload"] == {"changed": ["a.py"]} and [x["seq"] for x in h] == sorted(x["seq"] for x in h)


def test_json_columns_round_trip_and_links_are_idempotent(st):
    ids = _seed(st)
    ev = st.evidence(ids["evidence"])
    assert ev["meta"] == {} and st.get("experiments", ids["experiment"])["command"] == ["pytest"]
    st.link(ids["claim"], ids["evidence"], "supports")  # INSERT OR IGNORE
    assert len(st.claim_evidence(ids["claim"])) == 1
    with pytest.raises(ValueError, match="relation"):
        st.link(ids["claim"], ids["evidence"], "endorses")  # would be silently ignored by INSERT OR IGNORE
    with pytest.raises(sqlite3.IntegrityError):  # the CHECK constraint still guards raw SQL
        st.conn.execute("INSERT INTO claim_evidence (claim_id, evidence_id, relation, created_at)"
                        " VALUES (?, ?, 'endorses', ?)", (ids["claim"], ids["evidence"], now()))
    st.conn.rollback()
    assert st.claims_for_evidence(ids["evidence"])[0]["id"] == ids["claim"]


def test_open_store_creates_atlas_dir_ignored_by_git(tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    st = open_store(repo)
    try:
        assert (repo / ".verinoda" / "atlas.db").exists()
        assert (repo / ".verinoda" / ".gitignore").read_text(encoding="utf-8").strip() == "*"
        assert Path(st.path).name == "atlas.db"
    finally:
        st.close()


def test_v4_claim_text_and_created_at_are_immutable(st):
    ids = _seed(st)
    assert SCHEMA_VERSION >= 4 and "no_update_claim_identity" in _triggers(st.conn)
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        st.conn.execute("UPDATE claims SET text = 'rewritten' WHERE id = ?", (ids["claim"],))
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        st.conn.execute("UPDATE claims SET created_at = '1999-01-01' WHERE id = ?", (ids["claim"],))
    st.conn.rollback()
    st.update_claim(ids["claim"], {"status": "unknown", "spec": {"x": 1}})  # other columns still change
    c = st.claim(ids["claim"])
    assert c["text"] == "x" and c["spec"] == {"x": 1}


def test_migrates_a_v3_database_to_v4(tmp_path):
    p = tmp_path / "v3.db"
    conn = sqlite3.connect(p)
    for v in (1, 2, 3):
        conn.executescript(storemod._MIGRATIONS[v])
    conn.execute("INSERT INTO meta(key, value) VALUES ('schema_version', '3')")
    ts = now()
    conn.execute("INSERT INTO claims (id, text, project, status, confidence, created_at, updated_at)"
                 " VALUES ('clm_v3', 'kept', 'p', 'unknown', 0.0, ?, ?)", (ts, ts))
    conn.commit()
    conn.close()
    s = Store(p)
    try:
        assert _version(s.conn) == SCHEMA_VERSION and s.claim("clm_v3")["text"] == "kept"
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            s.conn.execute("UPDATE claims SET text = 'x' WHERE id = 'clm_v3'")
        s.conn.rollback()
    finally:
        s.close()


def test_migrates_a_v4_database_to_v5_with_append_only_decisions(tmp_path):
    p = tmp_path / "v4.db"
    conn = sqlite3.connect(p)
    for v in (1, 2, 3, 4):
        conn.executescript(storemod._MIGRATIONS[v])
    conn.execute("INSERT INTO meta(key, value) VALUES ('schema_version', '4')")
    conn.commit()
    conn.close()
    s = Store(p)
    try:
        assert _version(s.conn) == SCHEMA_VERSION >= 5
        assert {"decisions", "decision_briefs", "decision_answers"} <= _tables(s.conn)
        s.insert("decisions", {"id": "ADR-0001", "number": 1, "event": "record", "status": "accepted",
                               "decided_by": "human", "guards": [{"id": "g1"}], "created_at": now()})
        assert s.one("SELECT guards FROM decisions")["guards"] == [{"id": "g1"}]
        for sql in ("UPDATE decisions SET status = 'x'", "DELETE FROM decisions"):
            with pytest.raises(sqlite3.DatabaseError, match="append-only|never deleted"):
                s.conn.execute(sql)
            s.conn.rollback()
        s.insert("decision_briefs", {"id": "dbr_1", "question": "q", "result": {"a": 1}, "created_at": now()})
        with pytest.raises(sqlite3.DatabaseError, match="CHECK"):
            s.insert("decision_answers", {"brief_id": "dbr_1", "question_id": "q1", "answer": "a",
                                          "answered_by": "agent", "created_at": now()})
        s.conn.rollback()
    finally:
        s.close()


def test_trust_engine_helpers(st):
    ids = _seed(st)
    assert st.file_facts("abc", "py1") is None
    st.put_file_facts("abc", "py1", {"symbols": {"f": {"sig": "1"}}})
    st.put_file_facts("abc", "py1", {"symbols": {}})  # a cached version is never rewritten
    assert st.file_facts("abc", "py1") == {"symbols": {"f": {"sig": "1"}}}
    st.add_claim_deps(ids["claim"], [{"dep_key": "sym:a.py::f", "facet": "body", "fp": "h1", "scheme": "py1"},
                                     {"dep_key": "file:a.py", "facet": "content", "fp": "0" * 64, "scheme": "file"}])
    st.add_claim_deps(ids["claim"], [{"dep_key": "sym:a.py::f", "facet": "body", "fp": "other", "scheme": "py1"}])
    deps = st.claim_deps(ids["claim"])
    assert [(d["dep_key"], d["fp"]) for d in deps] == [("file:a.py", "0" * 64), ("sym:a.py::f", "h1")]
    st.rebase_claim_deps(ids["claim"], [{"dep_key": "sym:a.py::f", "facet": "body", "fp": "h2", "scheme": "py1"}])
    assert st.claim_deps(ids["claim"])[1]["fp"] == "h2"
    with pytest.raises(sqlite3.DatabaseError, match="never deleted"):
        st.conn.execute("DELETE FROM claim_deps")
    st.conn.rollback()
    assert st.add_evidence_location(ids["evidence"], snapshot_id=None, line_start=3, line_end=3, status="moved")
    assert not st.add_evidence_location(ids["evidence"], snapshot_id="s2", line_start=3, line_end=3, status="moved")
    assert st.add_evidence_location(ids["evidence"], snapshot_id=None, line_start=None, line_end=None, status="gone",
                                    detail={"why": "deleted"})
    locs = st.evidence_locations(ids["evidence"])
    assert [x["status"] for x in locs] == ["moved", "gone"] and locs[1]["detail"] == {"why": "deleted"}
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        st.conn.execute("UPDATE evidence_locations SET status = 'same'")
    st.conn.rollback()
    st.link(ids["claim"], ids["evidence"], "qualifies", grp="g1")
    assert {e["relation"]: e["grp"] for e in st.claim_evidence(ids["claim"])} == {"supports": None, "qualifies": "g1"}
