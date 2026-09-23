"""Versioned learnings that stay tied to the claims they came from.

A memory is a (key, value) fact such as ``"persistence.owner" ->
"orders/repository.py"``. Writing a new value for a key creates version n+1
and marks the previous one superseded. When the source claim goes stale or is
contradicted, every memory derived from it is invalidated (never deleted),
so ``recall`` only returns facts whose backing claim is still standing.
"""

from __future__ import annotations

from repoatlas.store import Store, new_id, now

FALLEN = ("stale", "contradicted")


class Memory:
    def __init__(self, store: Store):
        self.store = store

    def learn(self, key: str, value: str, *, source_claim_id: str | None = None,
              snapshot_id: str | None = None) -> dict:
        if source_claim_id:
            src = self.store.claim(source_claim_id)
            if src is None:
                raise ValueError(f"no claim {source_claim_id}")
            if src["status"] in FALLEN:
                # recall() must only return facts whose backing claim stands.
                raise ValueError(f"claim {source_claim_id} is {src['status']}; "
                                 "re-verify it before learning from it")
        prev = self.store.one(
            "SELECT * FROM memory WHERE key = ? ORDER BY version DESC LIMIT 1", (key,)
        )
        if prev and prev["valid"] and prev["value"] == value and prev["source_claim_id"] == source_claim_id:
            return prev
        mid = new_id("mem")
        row = {
            "id": mid, "key": key, "value": value, "version": (prev["version"] + 1) if prev else 1,
            "source_claim_id": source_claim_id, "snapshot_id": snapshot_id, "valid": 1,
            "created_at": now(),
        }
        self.store.insert("memory", row)
        if prev and prev["valid"]:
            self.store.update("memory", prev["id"], {
                "valid": 0, "invalidated_at": now(), "invalidation_reason": "superseded",
                "superseded_by": mid,
            })
        return self.store.get("memory", mid)

    def recall(self, key: str | None = None) -> list[dict]:
        """Valid memories whose backing claim (if any) still stands.

        A memory whose claim is stale or contradicted is invalidated when the
        claim falls; the join here also hides one whose invalidation was
        missed (e.g. a status written by an older RepoAtlas), and invalidates
        it now, so a fallen claim can never be recalled.
        """
        rows = (self.store.all("SELECT * FROM memory WHERE key = ? AND valid = 1", (key,)) if key else
                self.store.all("SELECT * FROM memory WHERE valid = 1 ORDER BY key"))
        out = []
        for r in rows:
            src = self.store.claim(r["source_claim_id"]) if r.get("source_claim_id") else None
            if src is not None and src["status"] in FALLEN:
                self.store.update("memory", r["id"], {
                    "valid": 0, "invalidated_at": now(),
                    "invalidation_reason": f"source claim became {src['status']}",
                })
                continue
            out.append(r)
        return out

    def history(self, key: str) -> list[dict]:
        return self.store.all("SELECT * FROM memory WHERE key = ? ORDER BY version", (key,))

    def invalidate_for_claim(self, claim_id: str, reason: str) -> int:
        rows = self.store.all(
            "SELECT id FROM memory WHERE source_claim_id = ? AND valid = 1", (claim_id,)
        )
        for r in rows:
            self.store.update("memory", r["id"], {
                "valid": 0, "invalidated_at": now(), "invalidation_reason": reason,
            })
        return len(rows)
