"""Consolidation between sessions (``verinoda consolidate``; ``update --consolidate``).

Letta's sleep-time agents and Cognee's memify tidy a memory while no one is asking. Verinoda's equivalent is
mechanical and evidence-bound:

- **stale claims are re-verified**: each one (newest first, within a count and a time budget) goes through
  ``verify`` against the current tree, the same check a user would run: restored (up to its recorded ceiling)
  and rebound to the current snapshot when the code behind it is unchanged or its cited lines re-check, else
  left stale. No test is run (``verify --run`` stays the user's call);
- **duplicates are found**: claims that state the same thing (the same ``claim_key``: kind, semantic spec,
  subjects, or normalised text) and are not superseded. ``--merge`` keeps one (the best status, then the most
  recently verified) and folds each other one into it: its evidence is linked to the kept claim (noted "merged
  from"), the kept claim is re-assessed with the union, and the duplicate keeps its row and status, with
  ``spec.duplicate_of`` and a history entry naming the kept claim. Nothing is deleted.

``update`` runs it after a successful update with ``--consolidate`` or ``claims.consolidate_on_update`` in the
configuration (so a background update, from the git hooks or ``ui --watch``, re-checks stale claims).
"""
from __future__ import annotations

import time
from pathlib import Path

DEFAULT_LIMIT = 50
DEFAULT_BUDGET_S = 60.0
ON_UPDATE_LIMIT = 20
ON_UPDATE_BUDGET_S = 20.0


def duplicate_groups(store) -> list[dict]:
    """Groups of claims with the same ``claim_key`` (two or more, none superseded or already folded): the one to
    keep first."""
    from verinoda.claims import ORDER

    rank = {s: k for k, s in enumerate(ORDER)}
    rows = store.all("SELECT * FROM claims WHERE superseded_by IS NULL AND claim_key IS NOT NULL "
                     "ORDER BY claim_key, created_at")
    by_key: dict[str, list[dict]] = {}
    for c in rows:
        if (c.get("spec") or {}).get("duplicate_of"):
            continue
        by_key.setdefault(c["claim_key"], []).append(c)
    out = []
    for key, cs in sorted(by_key.items()):
        if len(cs) < 2:
            continue
        cs = sorted(cs, key=lambda c: (rank.get(c["status"], len(ORDER) + (c["status"] == "contradicted")),
                                       -_stamp(c.get("verified_at") or c.get("updated_at")), c["id"]))
        out.append({"key": key, "keep": cs[0]["id"], "fold": [c["id"] for c in cs[1:]],
                    "text": cs[0]["text"][:160], "statuses": [c["status"] for c in cs]})
    return out


def _stamp(v) -> float:
    from datetime import datetime

    try:
        return datetime.fromisoformat(str(v)).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _fold(store, repo: Path, keep: str, dup: str) -> dict:
    from verinoda.claims import Claims

    cl = Claims(store, repo)
    linked = 0
    for e in store.claim_evidence(dup):
        before = len(store.claim_evidence(keep))
        store.link(keep, e["id"], e["relation"], note=f"merged from {dup}", grp=e.get("grp"))
        linked += len(store.claim_evidence(keep)) - before
    d = store.claim(dup)
    spec = {**(d.get("spec") or {}), "duplicate_of": keep}
    store.record_transition(dup, from_status=d["status"], to_status=d["status"], from_conf=d["confidence"],
                            to_conf=d["confidence"], reason=f"consolidate: merged into {keep} (same statement)",
                            actor="consolidate", payload={"duplicate_of": keep}, extra_fields={"spec": spec})
    after = cl.reassess(keep, reason=f"consolidate: evidence of {dup} merged in", actor="consolidate")
    return {"duplicate": dup, "kept": keep, "evidence_linked": linked, "kept_status": after["status"]}


def run(store, repo: Path, *, limit: int = DEFAULT_LIMIT, budget: float = DEFAULT_BUDGET_S, merge: bool = False,
        dry_run: bool = False) -> dict:
    """Re-verify up to ``limit`` stale claims within ``budget`` seconds; find duplicates, folding them with
    ``merge``. ``dry_run`` only lists what would be done."""
    from verinoda import workflow

    repo = Path(repo)
    t_end = time.monotonic() + max(0.0, budget)
    stale = store.all("SELECT id, text FROM claims WHERE status = 'stale' AND superseded_by IS NULL "
                      "ORDER BY updated_at DESC")
    todo = stale[:max(0, limit)]
    rechecked, restored, still, errors, skipped = [], 0, 0, [], len(stale) - len(todo)
    for k, c in enumerate(todo):
        if dry_run:
            rechecked.append({"id": c["id"], "text": c["text"][:120], "would": "verify"})
            continue
        if time.monotonic() > t_end:
            skipped += len(todo) - k
            break
        try:
            res = workflow.verify(store, repo, c["id"])
        except Exception as exc:          # one claim never stops the others
            errors.append({"id": c["id"], "error": f"{type(exc).__name__}: {exc}"[:300]})
            continue
        now = res["after"]["status"]
        if now == "stale":
            still += 1
        else:
            restored += 1
        rechecked.append({"id": c["id"], "text": c["text"][:120], "now": now})
    groups = duplicate_groups(store)
    folded = []
    if merge and not dry_run:
        for g in groups:
            for dup in g["fold"]:
                try:
                    folded.append(_fold(store, repo, g["keep"], dup))
                except Exception as exc:
                    errors.append({"id": dup, "error": f"{type(exc).__name__}: {exc}"[:300]})
    return {"stale": len(stale), "rechecked": rechecked, "restored": restored, "still_stale": still,
            "not_reached": skipped, "duplicates": groups, "merged": folded, "errors": errors, "dry_run": dry_run,
            "method": "verify (static: cited lines and the files behind each claim) on the current tree, newest "
                      "stale first; duplicates by claim_key, kept by best status then latest verification"}


def summary(res: dict) -> dict:
    """The few numbers ``update`` prints."""
    return {"stale": res["stale"], "restored": res["restored"], "still_stale": res["still_stale"],
            "not_reached": res["not_reached"], "duplicate_groups": len(res["duplicates"]),
            "errors": len(res["errors"])}


def render(res: dict) -> str:
    out = [("would re-verify" if res["dry_run"] else "re-verified")
           + f" {len(res['rechecked'])} of {res['stale']} stale claim(s)"
           + ("" if res["dry_run"] else f": {res['restored']} restored, {res['still_stale']} still stale")
           + (f", {res['not_reached']} not reached (limit or time budget)" if res["not_reached"] else "")]
    for r in res["rechecked"][:30]:
        out.append(f"  {r['id']} -> {r.get('now') or r.get('would')}: {r['text']}")
    out.append(f"{len(res['duplicates'])} group(s) of duplicate claims"
               + (f", {len(res['merged'])} claim(s) merged" if res["merged"] else
                  " (`--merge` folds each into the one kept)" if res["duplicates"] else ""))
    for g in res["duplicates"][:20]:
        out.append(f"  keep {g['keep']} ({g['statuses'][0]}), fold {', '.join(g['fold'])}: {g['text']}")
    for e in res["errors"][:10]:
        out.append(f"  error {e['id']}: {e['error']}")
    return "\n".join(out)
