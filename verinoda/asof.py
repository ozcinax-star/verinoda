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

Every transition records the snapshot whose tree it read (``payload.snapshot``): the claim's at creation, the
new one at a rebind, the one an invalidation saw the change in, the current one when ``verify`` read a tree
with a snapshot; ``None`` when it read the working tree (no commit). A snapshot of a dirty tree is at no commit
either. Per claim, a transition at REV itself wins; else the last one at the nearest commits with a record (none
an ancestor of another). For transitions recorded before that, the snapshot is rebuilt: a claim starts at its first snapshot (the earliest
``rebound_from``, else its own), a rebind moves it to the snapshot its reason names, a stale transition records
``new_snapshot``; the initial assessment is at its first snapshot; any other transition is at no commit.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

# held: a supporting status at least strong_inference (weak_inference is never stated as holding)
HOLDING = ("experiment_verified", "statically_verified", "primary_source_verified", "observed", "strong_inference")
MAX_LISTED = 200
_REBOUND = re.compile(r"rebound to snapshot (\S+?)(?:[:\s]|$)")


class AsOfError(ValueError):
    pass


def _when(stamp: str) -> datetime:
    t = datetime.fromisoformat(stamp)
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def events(store, claim: dict) -> list[dict]:
    """A claim's transitions in recorded order, each with the snapshot whose tree it read (``None``: the working
    tree, or not known) and whether that snapshot is on record (``recorded``) or rebuilt from an older row."""
    hist = store.history(claim["id"])
    first = next((h["payload"].get("rebound_from") for h in hist
                  if isinstance(h.get("payload"), dict) and h["payload"].get("rebound_from")), None)
    cur = first or claim.get("snapshot_id")
    out = []
    for k, h in enumerate(hist):
        p = h.get("payload") if isinstance(h.get("payload"), dict) else {}
        if "snapshot" in p:
            snap, sure = p["snapshot"], True
        elif p.get("rebound_from"):                       # an update's rebind names its snapshot
            m = _REBOUND.search(h.get("reason") or "")
            snap, sure = (m.group(1) if m else None), m is not None
        elif p.get("snapshot_id"):                        # verify's rebind carries it
            snap, sure = p["snapshot_id"], True
        elif h["to_status"] == "stale" and "new_snapshot" in p:
            snap, sure = p["new_snapshot"], True           # None: seen in the working tree, at no commit
        elif k <= 1 or h.get("reason") == "initial assessment":
            snap, sure = cur, True                         # made at its first snapshot
        else:
            snap, sure = None, False                       # read the working tree then: no commit is known
        if snap and (p.get("rebound_from") or p.get("snapshot_id")):
            cur = snap
        out.append({"seq": h["seq"], "at": h["created_at"], "status": h["to_status"], "snapshot": snap,
                    "recorded": "snapshot" in p, "sure": sure, "reason": (h.get("reason") or "")[:160]})
    return out


def _parse_time(when: str) -> datetime:
    w = (when or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", w):
        w += "T23:59:59+00:00"
    if w.endswith(("Z", "z")):                             # Python 3.10 reads no "Z"
        w = w[:-1] + "+00:00"
    try:
        return _when(w)
    except ValueError:
        raise AsOfError(f"--time takes an ISO date or time (2026-05-01 or 2026-05-01T12:00:00+02:00), not "
                        f"{when!r}") from None


def at_time(store, when: str, *, status: str | None = None) -> dict:
    """Every claim's status as Verinoda had recorded it at ``when`` (an ISO date or time; a date means its end)."""
    t = _parse_time(when)
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
    anc: dict[tuple[str, str], bool] = {}

    def commit(snap):
        """A snapshot's commit; None for a snapshot of a dirty tree (its files are not that commit's)."""
        if snap not in commit_of:
            s = store.snapshot(snap) if snap else None
            commit_of[snap] = (s or {}).get("commit_sha") if s and not s.get("dirty") else None
        return commit_of[snap]

    def is_anc(a, b):        # a is b or an ancestor of it
        if a == b:
            return True
        if (a, b) not in anc:
            anc[(a, b)] = git(repo, "merge-base", "--is-ancestor", a, b) is not None
        return anc[(a, b)]

    rows, counts = [], {}
    for c in store.all("SELECT * FROM claims ORDER BY created_at"):
        cands = [(e, k) for e in events(store, c) for k in [commit(e["snapshot"])] if k and is_anc(k, sha)]
        at_rev = [x for x in cands if x[1] == sha]
        if at_rev:
            pick = at_rev
        else:   # the nearest records: none whose commit is an ancestor of another record's commit
            ks = {k for _, k in cands}
            near = {k for k in ks if not any(o != k and is_anc(k, o) for o in ks)}
            pick = [x for x in cands if x[1] in near]
        last = pick[-1] if pick else None
        if last is None:
            verdict, st, basis = "unknown", None, "no transition recorded at REV or an ancestor of it"
        else:
            e, k = last
            st = e["status"]
            exact = k == sha and e["sure"]
            verdict = "observed" if exact else "strong_inference"
            basis = (f"recorded at {rev}" if k == sha else f"recorded at {k[:12]}, an ancestor of {rev}, the "
                     "nearest commit with a record") + ("" if e["recorded"] else " (snapshot rebuilt from an "
                                                                                 "older record)")
        key = st if st else "no_record"
        counts[key] = counts.get(key, 0) + 1
        if status is None or st == status:
            rows.append({"id": c["id"], "text": c["text"][:200], "status_then": st, "held": st in HOLDING,
                         "status_now": c["status"], "verdict": verdict, "basis": basis,
                         **({"seq": last[0]["seq"]} if last else {})})
    return {"as_of": sha[:12], "rev": rev, "axis": "code", "counts": counts,
            "claims": rows[:MAX_LISTED], "total": len(rows), "truncated": len(rows) > MAX_LISTED,
            "method": "per claim, the last transition recorded at the revision, else at the nearest commits with a "
                      "record in its history (git merge-base --is-ancestor); observed when recorded at the "
                      "revision itself; snapshots of a dirty tree and transitions made on the working tree are at "
                      "no commit",
            "limits": ["a status carried over from an ancestor (strong_inference) was not checked at the revision",
                       "transitions recorded before snapshots were stored in the history have their snapshot "
                       "rebuilt; one that read the working tree is at no commit"]}


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
