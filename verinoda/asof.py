"""Claims as of a moment or a commit (``verinoda claim asof``): the two times of a claim.

A claim has two histories (Zep Graphiti's bi-temporal model):

- **recorded time**: when Verinoda believed what. ``claim_history`` is append-only, so the status of every claim
  at a past moment is read back exactly: the last transition recorded at or before it (``--time``);
- **code time**: at which commits the claim held. A claim is checked at a snapshot (a commit); ``update`` keeps it
  (rebinds it to the new snapshot) when nothing it depends on changed, and makes it stale at the snapshot where
  something did. ``--commit REV`` answers per claim from the last transition recorded at a commit in REV's
  history (REV itself or an ancestor): recorded at REV is ``observed``; carried over from an ancestor is
  ``strong_inference`` (nothing was recorded at REV; the code may have changed in between and back); a claim
  with no transition in REV's history (made later, or on another branch) is ``unknown``.

Every transition records the snapshot it was made at (``payload.snapshot``: for an invalidation the snapshot it
saw the change in, ``None`` for the working tree; else the claim's). For transitions recorded before that, the
snapshot is rebuilt: a claim starts at its first snapshot (the earliest
``rebound_from``, else its own), a rebind moves it to the snapshot its reason names, a stale transition records
``new_snapshot``; any other transition is taken at the snapshot the claim was then bound to.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

HOLDING = ("experiment_verified", "statically_verified", "primary_source_verified", "observed", "strong_inference",
           "weak_inference")
MAX_LISTED = 200
_REBOUND = re.compile(r"rebound to snapshot (\S+?)(?:[:\s]|$)")


class AsOfError(ValueError):
    pass


def _when(stamp: str) -> datetime:
    t = datetime.fromisoformat(stamp)
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def events(store, claim: dict) -> list[dict]:
    """A claim's transitions in recorded order, each with the snapshot it was made at (``None`` if unknown)."""
    hist = store.history(claim["id"])
    first = next((h["payload"].get("rebound_from") for h in hist
                  if isinstance(h.get("payload"), dict) and h["payload"].get("rebound_from")), None)
    cur = first or claim.get("snapshot_id")
    out = []
    for h in hist:
        p = h.get("payload") if isinstance(h.get("payload"), dict) else {}
        snap = p.get("snapshot")
        if "snapshot" not in p:
            if p.get("rebound_from"):
                m = _REBOUND.search(h.get("reason") or "")
                snap = m.group(1) if m else None
            elif h["to_status"] == "stale" and "new_snapshot" in p:
                snap = p["new_snapshot"]       # None: seen in the working tree, at no commit
            else:
                snap = cur
        if p.get("rebound_from") or "snapshot" in p:
            cur = snap or cur
        out.append({"seq": h["seq"], "at": h["created_at"], "status": h["to_status"], "snapshot": snap,
                    "recorded": "snapshot" in p, "reason": (h.get("reason") or "")[:160]})
    return out


def at_time(store, when: str, *, status: str | None = None) -> dict:
    """Every claim's status as Verinoda had recorded it at ``when`` (an ISO date or time; a date means its end)."""
    try:
        t = _when(when + "T23:59:59+00:00" if re.fullmatch(r"\d{4}-\d{2}-\d{2}", when.strip()) else when.strip())
    except ValueError:
        raise AsOfError(f"--time takes an ISO date or time (2026-05-01 or 2026-05-01T12:00:00+02:00), not {when!r}") \
            from None
    rows, counts = [], {}
    for c in store.all("SELECT * FROM claims ORDER BY created_at"):
        if _when(c["created_at"]) > t:
            continue
        past = [h for h in store.history(c["id"]) if _when(h["created_at"]) <= t]
        if not past:
            continue
        st = past[-1]["to_status"]
        counts[st] = counts.get(st, 0) + 1
        if status is None or st == status:
            rows.append({"id": c["id"], "text": c["text"][:200], "status_then": st, "status_now": c["status"],
                         "since": past[-1]["created_at"]})
    return {"as_of": t.isoformat(timespec="seconds"), "axis": "recorded", "counts": counts,
            "claims": rows[:MAX_LISTED], "total": len(rows), "truncated": len(rows) > MAX_LISTED,
            "status": "observed",
            "method": "the last transition of each claim's append-only history at or before the moment"}


def at_commit(store, repo, rev: str, *, status: str | None = None) -> dict:
    """Every claim's status at commit ``rev``, from the transitions recorded at commits in its history."""
    from verinoda.snapshot import git

    sha = git(repo, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}")
    if not sha:
        raise AsOfError(f"{rev} is not a commit here")
    sha = sha.strip()
    commit_of: dict[str, str | None] = {}
    inside: dict[str, bool] = {}

    def commit(snap):
        if snap not in commit_of:
            s = store.snapshot(snap) if snap else None
            commit_of[snap] = (s or {}).get("commit_sha")
        return commit_of[snap]

    def in_history(c):
        if c not in inside:
            inside[c] = c == sha or git(repo, "merge-base", "--is-ancestor", c, sha) is not None
        return inside[c]

    rows, counts = [], {}
    for c in store.all("SELECT * FROM claims ORDER BY created_at"):
        last = None
        for e in events(store, c):
            k = commit(e["snapshot"])
            if k and in_history(k):
                last = (e, k)
        if last is None:
            verdict, st, basis = "unknown", None, "no transition recorded at REV or an ancestor of it"
        else:
            e, k = last
            st = e["status"]
            exact = k == sha
            verdict = "observed" if exact else "strong_inference"
            basis = (f"recorded at {rev}" if exact else f"recorded at {k[:12]}, an ancestor of {rev}; nothing "
                     "recorded at it since") + ("" if e["recorded"] else " (snapshot rebuilt from an older record)")
        key = st or "unknown"
        counts[key] = counts.get(key, 0) + 1
        if status is None or st == status:
            rows.append({"id": c["id"], "text": c["text"][:200], "status_then": st, "held": st in HOLDING,
                         "status_now": c["status"], "verdict": verdict, "basis": basis,
                         **({"seq": last[0]["seq"]} if last else {})})
    return {"as_of": sha[:12], "rev": rev, "axis": "code", "counts": counts,
            "claims": rows[:MAX_LISTED], "total": len(rows), "truncated": len(rows) > MAX_LISTED,
            "method": "per claim, the last transition recorded at a snapshot whose commit is the revision or an "
                      "ancestor of it (git merge-base --is-ancestor); observed when recorded at the revision itself",
            "limits": ["a claim that held at an ancestor is carried over (strong_inference): nothing was checked at "
                       "the revision", "transitions recorded before snapshots were stored in the history have "
                                       "their snapshot rebuilt (a re-verification may be placed at the older "
                                       "snapshot)"]}


def render(res: dict) -> str:
    head = (f"claims as recorded at {res['as_of']}" if res["axis"] == "recorded"
            else f"claims at commit {res['as_of']} ({res['rev']})")
    out = [f"{head}: " + ", ".join(f"{k} {v}" for k, v in sorted(res["counts"].items()))]
    for r in res["claims"][:60]:
        tag = f" ({r['verdict']})" if "verdict" in r else ""
        out.append(f"  {r['id']} [{r['status_then'] or 'unknown'}{tag}; now {r['status_now']}] {r['text'][:90]}")
    if res["total"] > 60:
        out.append(f"  ... {res['total'] - 60} more (--json)")
    return "\n".join(out)
