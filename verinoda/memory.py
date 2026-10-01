"""Versioned learnings that stay tied to the claims they came from.

A memory is a (key, value) fact such as ``"persistence.owner" ->
"orders/repository.py"``. Writing a new value for a key creates version n+1
and marks the previous one superseded. When the source claim goes stale or is
contradicted, every memory derived from it is invalidated (never deleted),
so ``recall`` only returns facts whose backing claim is still standing.

A learning may carry a time-to-live (``expires_at``): past it, the memory is
invalidated as "expired" the next time it is read. ``forget`` invalidates a
key's current version on the user's word. ``events`` reads a key's history as
mem0-style events (ADD, UPDATE, DELETE, EXPIRE, INVALIDATE), derived from the
rows themselves, so a database written before events existed has them too.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from verinoda.store import Store, new_id, now

FALLEN = ("stale", "contradicted")
FORGOTTEN = "forgotten"
EXPIRED = "expired"
_TTL = re.compile(r"^\s*(\d{1,6})\s*([mhdw])\s*$")
_UNIT = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}


def parse_ttl(text: str) -> timedelta:
    """``"30d"``, ``"12h"``, ``"90m"``, ``"2w"`` -> a positive timedelta; anything else is a ValueError."""
    m = _TTL.match(str(text or ""))
    if not m or int(m.group(1)) <= 0:
        raise ValueError(f"a time-to-live is a positive number with m, h, d or w (as 30d or 12h), not {text!r}")
    return timedelta(**{_UNIT[m.group(2)]: int(m.group(1))})


def _when(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    try:
        t = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _expired(row: dict, at: datetime) -> bool:
    end = _when(row.get("expires_at"))
    return end is not None and end <= at


class Memory:
    def __init__(self, store: Store):
        self.store = store

    def learn(self, key: str, value: str, *, source_claim_id: str | None = None,
              snapshot_id: str | None = None, ttl: timedelta | None = None) -> dict:
        if source_claim_id:
            src = self.store.claim(source_claim_id)
            if src is None:
                raise ValueError(f"no claim {source_claim_id}")
            if src["status"] in FALLEN:
                # recall() must only return facts whose backing claim stands.
                raise ValueError(f"claim {source_claim_id} is {src['status']}; "
                                 "re-verify it before learning from it")
        if ttl is not None and ttl <= timedelta(0):
            raise ValueError("a time-to-live must be positive")
        self._expire(key)
        prev = self.store.one(
            "SELECT * FROM memory WHERE key = ? ORDER BY version DESC LIMIT 1", (key,)
        )
        if (prev and prev["valid"] and prev["value"] == value and prev["source_claim_id"] == source_claim_id
                and ttl is None and prev.get("expires_at") is None):
            return prev
        stamp = now()
        mid = new_id("mem")
        row = {
            "id": mid, "key": key, "value": value, "version": (prev["version"] + 1) if prev else 1,
            "source_claim_id": source_claim_id, "snapshot_id": snapshot_id, "valid": 1,
            "created_at": stamp,
            "expires_at": ((_when(stamp) + ttl).isoformat(timespec="seconds") if ttl is not None else None),
        }
        self.store.insert("memory", row)
        if prev and prev["valid"]:
            self.store.update("memory", prev["id"], {
                "valid": 0, "invalidated_at": stamp, "invalidation_reason": "superseded",
                "superseded_by": mid,
            })
        return self.store.get("memory", mid)

    def forget(self, key: str, reason: str | None = None) -> dict:
        """Invalidate ``key``'s current version (a DELETE event); the row and its value are kept."""
        self._expire(key)
        cur = self.store.one("SELECT * FROM memory WHERE key = ? AND valid = 1 ORDER BY version DESC LIMIT 1",
                             (key,))
        if cur is None:
            raise ValueError(f"no current memory for {key!r}")
        why = FORGOTTEN + (f": {reason.strip()[:300]}" if reason and reason.strip() else "")
        self.store.update("memory", cur["id"], {"valid": 0, "invalidated_at": now(), "invalidation_reason": why})
        return self.store.get("memory", cur["id"])

    def _expire(self, key: str | None = None) -> int:
        """Invalidate the valid memories (of ``key``, or all) whose time-to-live has passed."""
        rows = (self.store.all("SELECT * FROM memory WHERE key = ? AND valid = 1 AND expires_at IS NOT NULL",
                               (key,)) if key is not None else
                self.store.all("SELECT * FROM memory WHERE valid = 1 AND expires_at IS NOT NULL"))
        at = datetime.now(timezone.utc)
        n = 0
        for r in rows:
            if _expired(r, at):
                # the invalidation is dated when its time-to-live ran out, not when it was noticed
                end = max(_when(r["expires_at"]), _when(r.get("created_at")) or _when(r["expires_at"]))
                self.store.update("memory", r["id"], {"valid": 0, "invalidation_reason": EXPIRED,
                                                      "invalidated_at": end.isoformat(timespec="seconds")})
                n += 1
        return n

    def recall(self, key: str | None = None) -> list[dict]:
        """Valid memories whose backing claim (if any) still stands and whose time-to-live has not passed.

        A memory whose claim is stale or contradicted is invalidated when the
        claim falls; the join here also hides one whose invalidation was
        missed (e.g. a status written by an older Verinoda), and invalidates
        it now, so a fallen claim can never be recalled.
        """
        self._expire(key)
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
        self._expire(key)
        return self.store.all("SELECT * FROM memory WHERE key = ? ORDER BY version", (key,))

    def events(self, key: str) -> list[dict]:
        """``key``'s history as events, oldest first.

        Each version gives ADD (the key had no current value) or UPDATE (it replaced the current one) when it was
        written, and, when it was invalidated for a reason other than being replaced, DELETE (``forget``),
        EXPIRE (its time-to-live ran out) or INVALIDATE (its source claim fell).
        """
        rows = self.history(key)
        replaced = {r["superseded_by"] for r in rows
                    if r.get("invalidation_reason") == "superseded" and r.get("superseded_by")}
        out = []
        for r in rows:
            base = {"version": r["version"], "memory_id": r["id"], "value": r["value"],
                    "source_claim_id": r.get("source_claim_id")}
            out.append({"event": "UPDATE" if r["id"] in replaced else "ADD", "at": r["created_at"], **base,
                        **({"expires_at": r["expires_at"]} if r.get("expires_at") else {})})
            why = r.get("invalidation_reason")
            if r["valid"] or not why or why == "superseded":
                continue
            kind = ("DELETE" if why.startswith(FORGOTTEN) else "EXPIRE" if why == EXPIRED else "INVALIDATE")
            out.append({"event": kind, "at": r.get("invalidated_at"), **base, "reason": why})
        # by time, the write of a version before its own end when both carry the same second
        order = {"ADD": 0, "UPDATE": 0}
        out.sort(key=lambda e: (e["at"] or "", e["version"], order.get(e["event"], 1)))
        return out

    def invalidate_for_claim(self, claim_id: str, reason: str) -> int:
        rows = self.store.all(
            "SELECT id FROM memory WHERE source_claim_id = ? AND valid = 1", (claim_id,)
        )
        for r in rows:
            self.store.update("memory", r["id"], {
                "valid": 0, "invalidated_at": now(), "invalidation_reason": reason,
            })
        return len(rows)
