"""Consolidation between sessions (``verinoda consolidate``; ``update --consolidate``).

Letta's sleep-time agents and Cognee's memify tidy a memory while no one is asking. Verinoda's equivalent is
mechanical and evidence-bound:

- **stale claims are re-verified**: each one goes through ``verify`` against the current tree, the same check a
  user would run: restored (up to its recorded ceiling) and rebound to the current snapshot when the code behind
  it is unchanged or its cited lines re-check, else left stale. No test is run (``verify --run`` stays the
  user's call). The queue rotates: claims never tried by a consolidation first (newest stale first), then the
  one tried longest ago (``spec.consolidate_tried``), so a run limited to N claims reaches every stale claim
  over a few runs. A claim folded into another (``spec.duplicate_of``) is not re-verified. The time budget
  bounds the run: an index refresh first (only when the tree changed since the last snapshot) waits for
  another build at most the budget, and no verify inside waits for one at all;
- **duplicates are found**: claims that state the same thing (the same ``claim_key``: kind, semantic spec,
  subjects, or normalised text) in the same project and ``valid_env``, not superseded and neither stale nor
  contradicted (a claim that is not stale holds on the current tree as far as invalidation knows, whichever
  snapshot it was verified at; a stale one is folded only once a re-verification restores it). A contradicted
  claim and a live one with the same key are a **conflict**, reported and never folded. ``--merge`` keeps one
  (the best status, then the most recently verified) and folds each other one into it, in one transaction:
  its supporting and qualifying evidence is linked to the kept claim (noted "merged from", its evidence group
  renamed after the duplicate), its refuting evidence is not carried, and the duplicate keeps its row and
  status, with ``spec.duplicate_of`` and a history entry naming the kept claim. The kept claim's status is not
  changed by a fold: the next ``verify`` applies the rules to the union. Nothing is deleted.

``update`` runs the re-verification after a complete update with ``--consolidate`` or
``claims.consolidate_on_update`` in the configuration (so the git hooks' background updates re-check stale
claims); it never merges.
"""
from __future__ import annotations

import time
from pathlib import Path

DEFAULT_LIMIT = 50
DEFAULT_BUDGET_S = 60.0
ON_UPDATE_LIMIT = 20
ON_UPDATE_BUDGET_S = 20.0
LIVE_EXCLUDED = ("stale", "contradicted")


def _by_scope(store) -> dict[tuple, list[dict]]:
    """Claims not superseded nor already folded, by (claim_key, project, valid_env)."""
    rows = store.all("SELECT * FROM claims WHERE superseded_by IS NULL AND claim_key IS NOT NULL "
                     "ORDER BY claim_key, created_at")
    out: dict[tuple, list[dict]] = {}
    for c in rows:
        if (c.get("spec") or {}).get("duplicate_of"):
            continue
        out.setdefault((c["claim_key"], c["project"], c.get("valid_env") or ""), []).append(c)
    return out


def duplicate_groups(store) -> list[dict]:
    """Groups of two or more live claims (neither stale nor contradicted) with the same claim key, project and
    valid_env: the one to keep first."""
    from verinoda.claims import ORDER

    rank = {s: k for k, s in enumerate(ORDER)}
    out = []
    for (key, project, env), cs in sorted(_by_scope(store).items()):
        live = [c for c in cs if c["status"] not in LIVE_EXCLUDED]
        if len(live) < 2:
            continue
        live.sort(key=lambda c: (rank.get(c["status"], len(ORDER)), *[-x for x in _verified_stamp(store, c)], c["id"]))
        out.append({"key": key, "project": project, "valid_env": env or None, "keep": live[0]["id"],
                    "fold": [c["id"] for c in live[1:]], "text": live[0]["text"][:160],
                    "statuses": [c["status"] for c in live]})
    return out


def conflicts(store) -> list[dict]:
    """The same statement (claim key, project, valid_env) both contradicted and not: never folded."""
    out = []
    for (key, project, env), cs in sorted(_by_scope(store).items()):
        bad = [c["id"] for c in cs if c["status"] == "contradicted"]
        good = [c["id"] for c in cs if c["status"] != "contradicted"]
        if bad and good:
            out.append({"key": key, "project": project, "valid_env": env or None, "contradicted": bad,
                        "not_contradicted": good, "text": cs[0]["text"][:160]})
    return out


def _stamp(v) -> float:
    from datetime import datetime

    try:
        return datetime.fromisoformat(str(v)).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _verified_stamp(store, c: dict) -> tuple[float, int]:
    """When the claim was last verified, as ``(time, snapshot order)``: ``verified_at`` names the snapshot it was
    verified at (older rows may hold a time). Snapshot times have a resolution of one second, so two snapshots
    made within it are told apart by the order they were recorded in, as :meth:`Store.latest_snapshot` does;
    ``(0, 0)`` for a claim never verified."""
    v = c.get("verified_at")
    if not v:
        return 0.0, 0
    s = store.one("SELECT created_at, rowid AS seq FROM snapshots WHERE id = ?", (v,))
    return (_stamp(s["created_at"]), int(s["seq"])) if s else (_stamp(v), 0)


def _fold(store, repo: Path, keep: str, dup: str) -> dict:
    from verinoda.claims import Claims

    cl = Claims(store, repo)
    not_carried = 0
    with store.tx():  # the links, the kept claim's dependencies and the duplicate's mark: all or nothing
        before = len(store.claim_evidence(keep))
        for e in store.claim_evidence(dup):
            if e["relation"] == "refutes":
                not_carried += 1
                continue
            grp = f"{dup}:{e['grp']}" if e.get("grp") else None
            cl.attach(keep, e["id"], e["relation"], note=f"merged from {dup}", grp=grp)
        linked = len(store.claim_evidence(keep)) - before
        d = store.claim(dup)
        spec = {**(d.get("spec") or {}), "duplicate_of": keep}
        store.record_transition(dup, from_status=d["status"], to_status=d["status"], from_conf=d["confidence"],
                                to_conf=d["confidence"], reason=f"consolidate: merged into {keep} (same statement)",
                                actor="consolidate", payload={"duplicate_of": keep}, extra_fields={"spec": spec})
    return {"duplicate": dup, "kept": keep, "evidence_linked": linked, "refutes_not_carried": not_carried,
            "kept_status": store.claim(keep)["status"]}


def _stale_queue(store) -> list[dict]:
    """Stale claims to re-verify: never tried by a consolidation first (newest stale first), then the one tried
    longest ago; folded duplicates left out."""
    rows = store.all("SELECT id, text, spec, updated_at FROM claims WHERE status = 'stale' AND superseded_by IS NULL "
                     "ORDER BY updated_at DESC")
    rows = [r for r in rows if not (r.get("spec") or {}).get("duplicate_of")]
    rows.sort(key=lambda r: str((r.get("spec") or {}).get("consolidate_tried") or ""))   # stable: "" first
    return rows


_last_tried = ""


def _tried_stamp() -> str:
    """The time of an attempt, to the microsecond and never equal to the one before it in this process: the queue
    orders claims by these stamps, and several runs within one clock tick (a second for ``store.now``, 15 ms on a
    Windows clock) must still rotate through every claim instead of tying and coming back to the first."""
    global _last_tried
    from datetime import datetime, timedelta, timezone

    stamp = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    if stamp <= _last_tried:
        stamp = (datetime.fromisoformat(_last_tried) + timedelta(microseconds=1)).isoformat(timespec="microseconds")
    _last_tried = stamp
    return stamp


def _mark_tried(store, cid: str) -> None:
    c = store.claim(cid)
    store.update_claim(cid, {"spec": {**(c.get("spec") or {}), "consolidate_tried": _tried_stamp()}})


def _status_before_stale(store, cid: str) -> str | None:
    row = store.one("SELECT from_status FROM claim_history WHERE claim_id = ? AND to_status = 'stale' AND "
                    "from_status IS NOT NULL AND from_status != 'stale' ORDER BY seq DESC LIMIT 1", (cid,))
    return row["from_status"] if row else None


def _refresh(store, repo: Path, t_end: float) -> str | None:
    """Bring the index up to the working tree before re-verifying, waiting for another build at most the time
    left; the reason re-verification cannot run, or None."""
    from verinoda import workflow
    from verinoda.paths import graph_path
    from verinoda.snapshot import current_state

    snap = store.latest_snapshot()
    if snap is not None and snap["tree_hash"] == current_state(repo, store=store)["tree_hash"]:
        return None
    if not graph_path(repo).exists():
        return None  # never scanned: verify records a snapshot of the tree itself, without a build
    res = workflow.update(store, repo, wait=max(0.0, t_end - time.monotonic()), purpose="consolidate")
    if res.get("error"):
        return f"the index could not be refreshed: {res['error']}"[:300]
    return None


def run(store, repo: Path, *, limit: int = DEFAULT_LIMIT, budget: float = DEFAULT_BUDGET_S, merge: bool = False,
        dry_run: bool = False) -> dict:
    """Re-verify up to ``limit`` stale claims within ``budget`` seconds; find duplicates, folding them with
    ``merge`` (not bounded by ``limit`` or ``budget``: a fold is a few writes, and only an explicit ``--merge``
    does it). ``dry_run`` only lists what would be done."""
    from verinoda import workflow
    from verinoda.claims import ORDER

    repo = Path(repo)
    t_end = time.monotonic() + max(0.0, budget)
    queue = _stale_queue(store)
    todo = queue[:max(0, limit)]
    rechecked, restored, changed, still, errors = [], 0, 0, 0, []
    skipped = len(queue) - len(todo)
    not_run = _refresh(store, repo, t_end) if todo and not dry_run else None
    if not_run:
        skipped += len(todo)
        todo = []
    for k, c in enumerate(todo):
        if dry_run:
            rechecked.append({"id": c["id"], "text": c["text"][:120], "would": "verify"})
            continue
        if time.monotonic() > t_end:
            skipped += len(todo) - k
            break
        was = _status_before_stale(store, c["id"])
        try:
            res = workflow.verify(store, repo, c["id"], wait=0)
        except Exception as exc:          # one claim never stops the others
            errors.append({"id": c["id"], "error": f"{type(exc).__name__}: {exc}"[:300]})
            res = None
        try:
            _mark_tried(store, c["id"])   # the rotation: tried now, last in the queue next time
        except Exception as exc:
            errors.append({"id": c["id"], "error": f"{type(exc).__name__}: {exc}"[:300]})
        if res is None:
            continue
        now = res["after"]["status"]
        if now == "stale":
            still += 1
        elif now in ORDER and (was not in ORDER or ORDER.index(now) <= ORDER.index(was)):
            restored += 1
        else:                             # left stale for a lower status than before, or contradicted
            changed += 1
        rechecked.append({"id": c["id"], "text": c["text"][:120], "now": now, "was": was})
    groups = duplicate_groups(store)
    folded = []
    if merge and not dry_run:
        for g in groups:
            for dup in g["fold"]:
                try:
                    folded.append(_fold(store, repo, g["keep"], dup))
                except Exception as exc:
                    errors.append({"id": dup, "error": f"{type(exc).__name__}: {exc}"[:300]})
    return {"stale": len(queue), "rechecked": rechecked, "restored": restored, "changed_status": changed,
            "still_stale": still, "not_reached": skipped, "not_run": not_run, "duplicates": groups,
            "conflicts": conflicts(store), "merged": folded, "errors": errors, "dry_run": dry_run,
            "method": "verify (static: cited lines and the files behind each claim) on the current tree, never "
                      "tried first then the longest since the last attempt; duplicates by claim_key, project "
                      "and valid_env among claims neither stale nor contradicted, kept by best status then "
                      "latest verification"}


def summary(res: dict) -> dict:
    """The few numbers ``update`` prints."""
    return {"stale": res["stale"], "restored": res["restored"], "changed_status": res["changed_status"],
            "still_stale": res["still_stale"], "not_reached": res["not_reached"], "not_run": res["not_run"],
            "duplicate_groups": len(res["duplicates"]), "conflicts": len(res["conflicts"]),
            "errors": len(res["errors"])}


def render(res: dict) -> str:
    out = [("would re-verify" if res["dry_run"] else "re-verified")
           + f" {len(res['rechecked'])} of {res['stale']} stale claim(s)"
           + ("" if res["dry_run"] else f": {res['restored']} restored, {res['changed_status']} changed status, "
                                        f"{res['still_stale']} still stale")
           + (f", {res['not_reached']} not reached (limit or time budget)" if res["not_reached"] else "")]
    if res.get("not_run"):
        out.append(f"  not re-verified: {res['not_run']}")
    for r in res["rechecked"][:30]:
        out.append(f"  {r['id']} -> {r.get('now') or r.get('would')}: {r['text']}")
    out.append(f"{len(res['duplicates'])} group(s) of duplicate claims"
               + (f", {len(res['merged'])} claim(s) merged" if res["merged"] else
                  " (`--merge` folds each into the one kept)" if res["duplicates"] else ""))
    for g in res["duplicates"][:20]:
        out.append(f"  keep {g['keep']} ({g['statuses'][0]}), fold {', '.join(g['fold'])}: {g['text']}")
    if res["conflicts"]:
        out.append(f"{len(res['conflicts'])} statement(s) both contradicted and not (never merged; "
                   "`verinoda verify` or a correction settles them)")
        for x in res["conflicts"][:20]:
            out.append(f"  contradicted {', '.join(x['contradicted'])} vs {', '.join(x['not_contradicted'])}: "
                       f"{x['text']}")
    for e in res["errors"][:10]:
        out.append(f"  error {e['id']}: {e['error']}")
    return "\n".join(out)
