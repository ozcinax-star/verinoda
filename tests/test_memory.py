"""Memory: versioned learnings tied to claims; invalidated (never deleted) when the claim falls."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import sqlite3  # noqa: E402

import pytest  # noqa: E402

from verinoda import evidence as evmod  # noqa: E402
from verinoda.claims import Claims  # noqa: E402
from verinoda.memory import Memory  # noqa: E402
from verinoda.store import Store  # noqa: E402


@pytest.fixture
def env(tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "m.py").write_text("def owner():\n    return 'repo'\n", encoding="utf-8")
    st = Store(tmp_path / "atlas.db")
    cl = Claims(st, repo)

    def claim(text="owner() returns repo"):
        ev = evmod.source_evidence(repo, "m.py", 1, 2, commit=None)
        return cl.create(text, project="p", snapshot=None, status="statically_verified",
                         evidence=[(ev, "supports")], subjects=["m.py"])

    yield st, cl, Memory(st), claim, repo
    st.close()


def test_learn_versions_and_supersedes(env):
    st, cl, mem, claim, repo = env
    v1 = mem.learn("persistence.owner", "orders/repository.py")
    assert (v1["version"], v1["valid"]) == (1, 1)
    assert mem.learn("persistence.owner", "orders/repository.py")["id"] == v1["id"]  # identical -> no new version
    v2 = mem.learn("persistence.owner", "orders/store.py")
    assert v2["version"] == 2
    old = st.get("memory", v1["id"])
    assert old["valid"] == 0 and old["superseded_by"] == v2["id"] and old["invalidation_reason"] == "superseded"
    assert [m["value"] for m in mem.recall("persistence.owner")] == ["orders/store.py"]
    assert [m["version"] for m in mem.history("persistence.owner")] == [1, 2]
    mem.learn("other.key", "x")
    assert [m["key"] for m in mem.recall()] == ["other.key", "persistence.owner"]


@pytest.mark.parametrize("fall_to", ["stale", "contradicted"])
def test_memory_invalidated_when_source_claim_falls(env, fall_to):
    st, cl, mem, claim, repo = env
    c = claim()
    m = mem.learn("owner.fn", "m.py::owner", source_claim_id=c["id"], snapshot_id=None)
    assert mem.recall("owner.fn")[0]["id"] == m["id"]
    if fall_to == "contradicted":
        cl.attach(c["id"], evmod.source_evidence(repo, "m.py", 2, 2, commit=None), "refutes")
    cl.set_status(c["id"], fall_to, reason="test", downgrade=False)
    assert mem.recall("owner.fn") == []
    row = st.get("memory", m["id"])
    assert row["valid"] == 0 and row["invalidation_reason"] == f"source claim became {fall_to}"
    assert row["value"] == "m.py::owner"  # kept for audit


def test_memory_rows_are_never_deleted(env):
    st, cl, mem, claim, repo = env
    m = mem.learn("k", "v")
    with pytest.raises(sqlite3.DatabaseError, match="never deleted"):
        st.conn.execute("DELETE FROM memory WHERE id = ?", (m["id"],))
    st.conn.rollback()
    assert st.get("memory", m["id"])


def test_learning_requires_a_standing_existing_claim(env):
    st, cl, mem, claim, repo = env
    with pytest.raises(ValueError, match="no claim"):
        mem.learn("k", "v", source_claim_id="clm_missing")
    c = claim()
    cl.set_status(c["id"], "stale", reason="changed")
    with pytest.raises(ValueError, match="stale"):
        mem.learn("k", "v", source_claim_id=c["id"])
    assert mem.history("k") == []


def test_invalidate_for_claim_counts_only_valid_rows(env):
    st, cl, mem, claim, repo = env
    c = claim()
    mem.learn("a", "1", source_claim_id=c["id"])
    mem.learn("b", "2", source_claim_id=c["id"])
    mem.learn("b", "3", source_claim_id=c["id"])  # supersedes b v1
    assert mem.invalidate_for_claim(c["id"], "manual") == 2
    assert mem.invalidate_for_claim(c["id"], "manual") == 0
    assert mem.recall() == []


def test_recall_hides_and_invalidates_memories_of_a_fallen_claim(env):
    st, cl, mem, claim, repo = env
    c = claim()
    mem.learn("owner.fn", "m.py::owner", source_claim_id=c["id"])
    # a status written without the claims layer (e.g. by an older Verinoda) skipped the invalidation
    st.record_transition(c["id"], from_status=c["status"], to_status="contradicted", from_conf=c["confidence"],
                         to_conf=0.05, reason="legacy write", actor="old")
    assert mem.recall("owner.fn") == [] and mem.recall() == []
    row = mem.history("owner.fn")[0]
    assert row["valid"] == 0 and row["invalidation_reason"] == "source claim became contradicted"


def test_memory_of_an_unverified_claim_can_be_learned_and_recalled(env):
    st, cl, mem, claim, repo = env
    c = claim("Orders are stored in PostgreSQL")  # the cited lines do not say so: not verified
    assert c["status"] not in ("statically_verified",)
    mem.learn("orders.db", "postgres?", source_claim_id=c["id"])
    assert [m["value"] for m in mem.recall("orders.db")] == ["postgres?"]


def test_events_add_update_delete_and_readd(env):
    st, cl, mem, claim, repo = env
    mem.learn("k", "a")
    mem.learn("k", "b")
    gone = mem.forget("k", reason="moved to the wiki")
    assert gone["valid"] == 0 and gone["invalidation_reason"] == "forgotten: moved to the wiki"
    assert mem.recall("k") == []
    with pytest.raises(ValueError, match="no current memory"):
        mem.forget("k")
    mem.learn("k", "c")
    ev = mem.events("k")
    assert [(e["event"], e["version"], e["value"]) for e in ev] == [
        ("ADD", 1, "a"), ("UPDATE", 2, "b"), ("DELETE", 2, "b"), ("ADD", 3, "c")]
    assert ev[2]["reason"] == "forgotten: moved to the wiki"
    assert [m["value"] for m in mem.recall("k")] == ["c"]


def test_ttl_expires_never_deletes(env):
    from datetime import timedelta

    from verinoda.memory import parse_ttl

    st, cl, mem, claim, repo = env
    assert parse_ttl("30d") == timedelta(days=30) and parse_ttl(" 12h ") == timedelta(hours=12)
    for bad in ("0d", "3", "1y", "-2d", ""):
        with pytest.raises(ValueError):
            parse_ttl(bad)
    m = mem.learn("cache.ttl", "short", ttl=timedelta(hours=1))
    assert m["expires_at"] > m["created_at"] and [r["id"] for r in mem.recall("cache.ttl")] == [m["id"]]
    # the same fact again with no time-to-live is a new version that never expires
    m2 = mem.learn("cache.ttl", "short")
    assert m2["version"] == 2 and m2["expires_at"] is None
    st.update("memory", m2["id"], {"expires_at": "2000-01-01T00:00:00+00:00"})   # its time is up
    assert mem.recall() == [] and mem.recall("cache.ttl") == []
    row = st.get("memory", m2["id"])
    assert row["valid"] == 0 and row["invalidation_reason"] == "expired" and row["value"] == "short"
    assert row["invalidated_at"] == row["created_at"]          # never dated before the learning itself
    assert [e["event"] for e in mem.events("cache.ttl")] == ["ADD", "UPDATE", "EXPIRE"]
    with pytest.raises(ValueError):
        mem.learn("x", "y", ttl=timedelta(0))


def test_claim_fall_is_an_invalidate_event(env):
    st, cl, mem, claim, repo = env
    c = claim()
    mem.learn("owner.fn", "m.py::owner", source_claim_id=c["id"])
    cl.set_status(c["id"], "stale", reason="test", downgrade=False)
    ev = mem.events("owner.fn")
    assert [e["event"] for e in ev] == ["ADD", "INVALIDATE"] and ev[1]["reason"] == "source claim became stale"


def test_v7_database_gains_expiry(tmp_path):
    from verinoda import store as S

    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    for v in range(1, 8):
        conn.executescript(S._MIGRATIONS[v])
    conn.execute("INSERT INTO meta VALUES ('schema_version', '7')")
    conn.execute("INSERT INTO memory (id, key, value, version, valid, created_at) "
                 "VALUES ('mem_old', 'k', 'v', 1, 1, '2026-01-01T00:00:00+00:00')")
    conn.commit()
    conn.close()
    st = Store(db)
    try:
        mem = Memory(st)
        assert [r["id"] for r in mem.recall("k")] == ["mem_old"] and mem.recall("k")[0]["expires_at"] is None
        assert [e["event"] for e in mem.events("k")] == ["ADD"]
    finally:
        st.close()
