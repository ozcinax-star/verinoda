"""Memory: versioned learnings tied to claims; invalidated (never deleted) when the claim falls."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".repoatlas/index")

import sqlite3  # noqa: E402

import pytest  # noqa: E402

from repoatlas import evidence as evmod  # noqa: E402
from repoatlas.claims import Claims  # noqa: E402
from repoatlas.memory import Memory  # noqa: E402
from repoatlas.store import Store  # noqa: E402


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
    # a status written without the claims layer (e.g. by an older RepoAtlas) skipped the invalidation
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
