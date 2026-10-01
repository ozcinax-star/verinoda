"""Derived facts (``verinoda fact``): a result kept under a name, with what it rests on, re-checked when that changes.

Glean's derived predicates and jQAssistant's concepts store a computed result for other queries to use. Here a
fact is one of two kinds:

- **derived**: one or more claims (and earlier facts) taken together. Its statement is theirs joined with "and";
  its status is the weakest of theirs (``contradicted`` before ``stale`` before the ``claims.ORDER`` ranks), so a
  fact is never stronger than what it rests on. A superseded claim or a retired input fact counts as stale: a
  fact never follows a correction by itself.
- **search**: a stored ``verinoda search`` (a regular expression or a fixed string, optionally under some paths).
  Its result is the matching sites; it rests on the content hashes of every file in its scope (a digest) and of
  each file that matched. ``observed`` when the search read every candidate file, ``weak_inference`` when it did
  not (the sites are real, the inventory may be incomplete).

Staleness: after every ``scan`` and ``update`` (:func:`after_update`) a derived fact whose input went stale,
contradicted or weaker is lowered to that status, and a search fact whose scope changed is ``stale``. Reads
(:func:`effective`, the leads in ``query`` and ``analyze``) apply the same cap without writing, so a fact is
never shown stronger than its inputs between updates. Nothing raises a fact's status except a recomputation:
``verinoda fact refresh`` (re-verifies stale input claims with ``verify``, static, and re-runs searches) or the
bounded refresh at the end of an update (re-runs searches and re-reads input statuses; it never runs ``verify``).
A recomputation that changes the result says so: the sites added and removed, the statement or the inputs'
statuses before and after. Facts are retired, never deleted, and every change is a ``fact_history`` row.
"""
from __future__ import annotations

import hashlib
import os
import re
import threading
import time
from pathlib import Path

from verinoda.store import Store, new_id, now

KINDS = ("derived", "search")
NAME_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
MAX_SITES = 500                # sites a search fact keeps (the total is always kept)
STATEMENT_CHARS = 600
DEFAULT_LIMIT = 50
DEFAULT_BUDGET_S = 60.0
ON_UPDATE_LIMIT = 20
ON_UPDATE_BUDGET_S = 10.0
LEADS = 5
_STOP = {"the", "and", "for", "are", "was", "with", "that", "this", "what", "where", "which", "who", "how", "why",
         "does", "did", "from", "into", "not", "all", "any", "its", "has", "have", "can", "use", "used", "uses"}

_local = threading.local()     # a refresh in progress in this thread: nested updates only invalidate


class FactError(ValueError):
    """A request that cannot be honoured (bad name, unknown input, a name in use): exit 2 on the CLI."""


# -- status rules ----------------------------------------------------------------------------------------

def weakest(statuses) -> str:
    """The weakest of ``statuses``: contradicted, then stale, then the lowest ``claims.ORDER`` rank (a status
    outside it counts as unknown). An empty list is unknown."""
    from verinoda.claims import ORDER

    sts = list(statuses)
    if not sts:
        return "unknown"
    if "contradicted" in sts:
        return "contradicted"
    if "stale" in sts:
        return "stale"
    return ORDER[max(ORDER.index(s) if s in ORDER else len(ORDER) - 1 for s in sts)]


def _rank(status: str) -> int:
    """Higher is weaker (for "is this lower than that")."""
    from verinoda.claims import ORDER

    if status == "stale":
        return len(ORDER) + 1
    if status == "contradicted":
        return len(ORDER) + 2
    return ORDER.index(status) if status in ORDER else len(ORDER) - 1


# -- rows -----------------------------------------------------------------------------------------------

def live(store: Store) -> list[dict]:
    """Facts not retired, oldest first (an input fact is always older than the facts resting on it)."""
    return store.all("SELECT * FROM facts WHERE retired_at IS NULL ORDER BY rowid")


def get(store: Store, name: str) -> dict | None:
    return store.one("SELECT * FROM facts WHERE name = ? AND retired_at IS NULL", (name,))


def _by_id(store: Store, fid: str) -> dict | None:
    return store.one("SELECT * FROM facts WHERE id = ?", (fid,))


def history(store: Store, fid: str) -> list[dict]:
    return store.all("SELECT * FROM fact_history WHERE fact_id = ? ORDER BY seq", (fid,))


def _record(store: Store, fact: dict, *, status: str, reason: str, actor: str, payload: dict | None = None,
            fields: dict | None = None, history_row: bool = True) -> dict:
    """Write the fact's new status (and result/inputs in ``fields``) with a history row, in one transaction.
    A fact retired meanwhile is left as it is."""
    ts = now()
    with store.tx():
        cur = _by_id(store, fact["id"])
        if cur is None or cur.get("retired_at"):
            return cur or fact
        store._update("facts", "id", fact["id"], {**(fields or {}), "status": status, "reason": reason,
                                                  "updated_at": ts})
        if history_row:
            store._insert("fact_history", {"fact_id": fact["id"], "from_status": cur["status"], "to_status": status,
                                           "reason": reason, "actor": actor, "payload": payload or {},
                                           "created_at": ts})
    return _by_id(store, fact["id"])


# -- derived facts --------------------------------------------------------------------------------------

def _input_states(store: Store, fact: dict, memo: dict) -> list[dict]:
    """Each input of a derived fact as it is now: ``{"type", "id", "status", "text", "why"?}``."""
    d = fact.get("definition") or {}
    out = []
    for cid in d.get("claims") or []:
        c = store.claim(cid)
        if c is None:
            out.append({"type": "claim", "id": cid, "status": "stale", "text": "", "why": "the claim is not found"})
        elif c.get("superseded_by"):
            out.append({"type": "claim", "id": cid, "status": "stale", "text": c["text"],
                        "why": f"superseded by {c['superseded_by']} (add the fact again from the correction)"})
        else:
            out.append({"type": "claim", "id": cid, "status": c["status"], "text": c["text"]})
    for fid in d.get("facts") or []:
        f = _by_id(store, fid)
        if f is None or f.get("retired_at"):
            out.append({"type": "fact", "id": fid, "name": (f or {}).get("name"), "status": "stale",
                        "text": ((f or {}).get("result") or {}).get("statement", ""), "why": "the fact was retired"})
        else:
            st, _ = effective(store, f, memo)
            out.append({"type": "fact", "id": fid, "name": f["name"], "status": st,
                        "text": (f.get("result") or {}).get("statement", "")})
    return out


def _derived_result(states: list[dict]) -> dict:
    statement = " and ".join(s["text"] for s in states if s.get("text"))
    return {"statement": statement[:STATEMENT_CHARS],
            "inputs": [{k: s[k] for k in ("type", "id", "name", "status") if s.get(k) is not None} for s in states]}


def _why(states: list[dict], status: str) -> str:
    bad = [s for s in states if _rank(s["status"]) >= _rank(status)]
    return "; ".join(f"{s['type']} {s.get('name') or s['id']} is {s['status']}"
                     + (f" ({s['why']})" if s.get("why") else "") for s in bad[:4])


def effective(store: Store, fact: dict, memo: dict | None = None) -> tuple[str, str | None]:
    """The fact's status now, capped by its inputs as they are now (nothing written), and why it is lower than
    recorded (None when it is not)."""
    memo = {} if memo is None else memo
    if fact["id"] in memo:
        return memo[fact["id"]]
    out: tuple[str, str | None] = (fact["status"], None)
    if fact["kind"] == "derived":
        states = _input_states(store, fact, memo)
        now_st = weakest([fact["status"], *(s["status"] for s in states)])
        if now_st != fact["status"]:
            out = (now_st, _why(states, now_st))
    memo[fact["id"]] = out
    return out


# -- search facts ---------------------------------------------------------------------------------------

def _fold() -> bool:
    return os.name == "nt"   # Windows paths match whatever their case (as the search's own scope does)


def _in_scope(path: str, scope: list[str]) -> bool:
    if not scope:
        return True
    p = path.lower() if _fold() else path
    for s in scope:
        s = s.lower() if _fold() else s
        if p == s or p.startswith(s + "/"):
            return True
    return False


def _scope_digest(files: dict[str, str], scope: list[str]) -> tuple[str, int]:
    h = hashlib.sha256()
    n = 0
    for p, d in sorted(files.items()):
        if _in_scope(p, scope):
            h.update(p.encode("utf-8", "surrogateescape") + b"\0" + d.encode() + b"\n")
            n += 1
    return h.hexdigest(), n


def _hash(repo: Path, rel: str, files: dict[str, str]) -> str | None:
    if rel in files:
        return files[rel]
    from verinoda.snapshot import sha256_file

    try:
        return sha256_file(repo / rel)
    except OSError:
        return None


def _site_key(site: dict) -> tuple[str, str]:
    return (site["at"].rsplit(":", 1)[0], site["text"])


def _run_search(store: Store, repo: Path, d: dict, timeout: float, *,
                files: dict[str, str] | None = None) -> tuple[dict, dict, str, str]:
    """Run a search fact's definition now: ``(result, inputs, status, reason)``. The files are hashed before the
    search reads them (``files``: hashes read earlier still), so an edit in between leaves the fact stale rather
    than wrongly current."""
    from verinoda import trigram
    from verinoda.snapshot import current_state

    if files is None:
        files = current_state(repo, store=store)["files"]
    scope = list(d.get("paths") or [])
    digest, n_scope = _scope_digest(files, scope)
    present = [p for p in scope if (repo / p).exists()]
    if scope and not present:
        res = {"status": "observed", "matches": [], "total": 0, "files_matched": 0, "truncated": False}
        gone = True
    else:
        res = trigram.search(repo, d["pattern"], ignore_case=bool(d.get("ignore_case")), fixed=bool(d.get("fixed")),
                             paths=present or None, max_results=MAX_SITES, timeout=max(0.1, timeout), cwd=repo)
        gone = False
    sites = [{"at": m["at"], "text": m["text"]} for m in res["matches"]]
    matched = sorted({_site_key(s)[0] for s in sites})
    complete = res["status"] == "observed"
    result = {"statement": _search_statement(d, res["total"], res["files_matched"]),
              "total": res["total"], "files_matched": res["files_matched"], "sites": sites,
              "truncated": bool(res.get("truncated")), "complete": complete}
    if res.get("not_read"):
        result["not_read"] = res["not_read"]
    inputs = {"scope_digest": digest, "scope_files": n_scope,
              "files": {p: _hash(repo, p, files) for p in matched}}
    if complete:
        status, reason = "observed", ("the search read every file in its scope"
                                      + ("; the folders it was limited to no longer exist" if gone else ""))
    else:
        status, reason = "weak_inference", ("the search did not read every file in its scope "
                                            f"({(res.get('not_read') or {}).get('why', 'cut short')}): the sites "
                                            "are real, the inventory may be incomplete")
    return result, inputs, status, reason


def _search_statement(d: dict, total: int, files_matched: int) -> str:
    where = ", ".join(d.get("paths") or []) or "the project"
    kind = "the text" if d.get("fixed") else "the pattern"
    case = " (any case)" if d.get("ignore_case") else ""
    return (f"{kind} `{d['pattern']}`{case} matches {total} line(s) in {files_matched} file(s) under "
            f"{where}")[:STATEMENT_CHARS]


def _site_diff(old: dict, new: dict) -> dict:
    """Sites added and removed (by file and line text: a line that only moved is not a change)."""
    from collections import Counter

    a = Counter(_site_key(s) for s in old.get("sites") or [])
    b = Counter(_site_key(s) for s in new.get("sites") or [])
    added = [f"{p}: {t.strip()}" for (p, t), n in (b - a).items() for _ in range(n)]
    removed = [f"{p}: {t.strip()}" for (p, t), n in (a - b).items() for _ in range(n)]
    moved = sum(1 for s in new.get("sites") or [] if s not in (old.get("sites") or [])) - len(added)
    return {"added": added[:50], "removed": removed[:50], "added_count": len(added), "removed_count": len(removed),
            "moved": max(0, moved)}


# -- add / retire ---------------------------------------------------------------------------------------

def _check_name(store: Store, name: str) -> None:
    if not NAME_RX.match(name or ""):
        raise FactError(f"{name!r} is not a fact name: letters, digits, '.', '_' and '-', starting with a letter "
                        "or digit, at most 80 characters")
    if get(store, name) is not None:
        raise FactError(f"a fact named {name!r} exists; `verinoda fact retire {name}` first, or choose another name")


def _insert(store: Store, name: str, kind: str, definition: dict, result: dict, inputs: dict, status: str,
            reason: str, actor: str) -> dict:
    import sqlite3

    ts = now()
    fid = new_id("fact")
    try:
        _insert_rows(store, fid, ts, name, kind, definition, result, inputs, status, reason, actor)
    except sqlite3.IntegrityError:   # another process took the name since it was checked
        raise FactError(f"a fact named {name!r} exists; `verinoda fact retire {name}` first, or choose another "
                        "name") from None
    return _by_id(store, fid)


def _insert_rows(store: Store, fid: str, ts: str, name: str, kind: str, definition: dict, result: dict,
                 inputs: dict, status: str, reason: str, actor: str) -> None:
    with store.tx():
        store._insert("facts", {"id": fid, "name": name, "kind": kind, "definition": definition, "result": result,
                                "inputs": inputs, "status": status, "reason": reason, "computed_at": ts,
                                "created_at": ts, "updated_at": ts})
        store._insert("fact_history", {"fact_id": fid, "from_status": None, "to_status": status,
                                       "reason": f"added: {reason}", "actor": actor, "payload": {"result": result},
                                       "created_at": ts})


def add_derived(store: Store, repo: Path, name: str, *, claims: list[str] | None = None,
                facts: list[str] | None = None, actor: str = "user") -> dict:
    """A fact resting on claims (by id) and earlier facts (by name); its status is the weakest of theirs."""
    claims, facts = list(dict.fromkeys(claims or [])), list(dict.fromkeys(facts or []))
    if not claims and not facts:
        raise FactError("a derived fact needs at least one --from-claim or --from-fact")
    with store.tx():   # the name check, the inputs as read and the insert: one transaction
        _check_name(store, name)
        for cid in claims:
            c = store.claim(cid)
            if c is None:
                raise FactError(f"no claim {cid} (`verinoda claim list`)")
            if c.get("superseded_by"):
                raise FactError(f"claim {cid} was superseded by {c['superseded_by']}; use that one")
        fids = []
        for fname in facts:
            f = get(store, fname)
            if f is None:
                raise FactError(f"no fact named {fname!r} (`verinoda fact list`)")
            fids.append(f["id"])
        definition = {"claims": claims, "facts": fids}
        states = _input_states(store, {"id": "", "kind": "derived", "definition": definition, "status": "unknown"},
                               {})
        status = weakest(s["status"] for s in states)
        reason = ("the weakest of its inputs: " + _why(states, status)) if states else "no input"
        return _fact_view(store, _insert(store, name, "derived", definition, _derived_result(states),
                                         {"statuses": {s["id"]: s["status"] for s in states}}, status, reason, actor))


def add_search(store: Store, repo: Path, name: str, pattern: str, *, fixed: bool = False, ignore_case: bool = False,
               paths: list[str] | None = None, cwd: Path | None = None, timeout: float = DEFAULT_BUDGET_S,
               actor: str = "user") -> dict:
    """A fact that is a stored search: its sites now, the hashes of the files it rests on."""
    from verinoda import trigram

    repo = Path(repo).resolve()
    _check_name(store, name)
    try:
        scope = trigram.scope_paths(repo, paths, cwd)
    except ValueError as exc:
        raise FactError(str(exc)) from None
    d = {"pattern": pattern, "fixed": bool(fixed), "ignore_case": bool(ignore_case), "paths": scope}
    try:
        result, inputs, status, reason = _run_search(store, repo, d, timeout)
    except ValueError as exc:   # a bad pattern (trigram.SearchError)
        raise FactError(str(exc)) from None
    with store.tx():
        _check_name(store, name)
        return _fact_view(store, _insert(store, name, "search", d, result, inputs, status, reason, actor))


def retire(store: Store, name: str, *, reason: str | None = None, actor: str = "user") -> dict:
    f = get(store, name)
    if f is None:
        raise FactError(f"no fact named {name!r}")
    ts = now()
    with store.tx():
        store._update("facts", "id", f["id"], {"retired_at": ts, "updated_at": ts})
        store._insert("fact_history", {"fact_id": f["id"], "from_status": f["status"], "to_status": f["status"],
                                       "reason": "retired" + (f": {reason}" if reason else ""), "actor": actor,
                                       "payload": {}, "created_at": ts})
    users = [x["name"] for x in live(store) if f["id"] in ((x.get("definition") or {}).get("facts") or [])]
    return {"retired": name, "id": f["id"], "now_stale": users}


# -- invalidation ---------------------------------------------------------------------------------------

def invalidate(store: Store, repo: Path, *, files: dict[str, str] | None = None, actor: str = "update") -> list[dict]:
    """Lower every fact whose inputs are weaker now; mark a search fact stale when a file in its scope changed.
    ``files`` (path -> sha256) is the tree to compare with (default: the working tree, stat-cached hashes).
    Returns ``[{"fact", "was", "now", "why"}]``; never raises a status."""
    repo = Path(repo).resolve()
    rows = live(store)
    out: list[dict] = []
    memo: dict = {}
    if files is None and any(f["kind"] == "search" and f["status"] != "stale" for f in rows):
        from verinoda.snapshot import current_state

        files = current_state(repo, store=store)["files"]
    for f in rows:
        f = _by_id(store, f["id"])    # an input fact lowered just before is read as it is now
        if f["status"] == "stale" and f["kind"] == "search":
            continue
        if f["kind"] == "derived":
            st, why = effective(store, f, memo)
            memo.pop(f["id"], None)
            if st == f["status"]:
                continue
            _record(store, f, status=st, reason=f"an input changed: {why}", actor=actor,
                    payload={"inputs": [s for s in _input_states(store, f, memo)]})
            out.append({"fact": f["name"], "was": f["status"], "now": st, "why": why})
        else:
            inp = f.get("inputs") or {}
            digest, _ = _scope_digest(files or {}, (f.get("definition") or {}).get("paths") or [])
            if digest == inp.get("scope_digest"):
                continue
            changed = sorted(p for p, h in (inp.get("files") or {}).items() if _hash(repo, p, files or {}) != h)
            why = ((f"{len(changed)} file(s) with a match changed: {', '.join(changed[:5])}" if changed else
                    "a file in its scope changed, was added or removed (a match may have appeared)"))
            _record(store, f, status="stale", reason=why, actor=actor, payload={"changed": changed[:50]})
            out.append({"fact": f["name"], "was": f["status"], "now": "stale", "why": why})
        memo.pop(f["id"], None)
    return out


# -- refresh --------------------------------------------------------------------------------------------

def _needs_refresh(store: Store, f: dict, memo: dict) -> bool:
    return effective(store, f, memo)[0] == "stale" or f["status"] == "stale"


def _dependents(store: Store, fid: str) -> list[dict]:
    """Live facts resting on the fact ``fid``, oldest first."""
    return [x for x in live(store) if fid in ((x.get("definition") or {}).get("facts") or [])]


class _Run:
    """One refresh: its deadline, what it did, the working tree's hashes (read once, before any search reads a
    file: an edit during the run leaves a fact stale at the next update, never wrongly current)."""

    def __init__(self, store: Store, repo: Path, *, budget: float, verify_claims: bool, actor: str):
        self.store, self.repo, self.verify_claims, self.actor = store, repo, verify_claims, actor
        self.t_end = time.monotonic() + max(0.0, budget)
        self.rows: list[dict] = []
        self.errors: list[dict] = []
        self.done: set[str] = set()
        self._files: dict[str, str] | None = None

    def files(self) -> dict[str, str]:
        if self._files is None:
            from verinoda.snapshot import current_state

            self._files = current_state(self.repo, store=self.store)["files"]
        return self._files

    def left(self) -> float:
        return self.t_end - time.monotonic()


def _recompute(run: _Run, f: dict) -> None:
    """Recompute one fact (its stale input facts first; the facts resting on it after it when its result changed),
    appending the report rows to ``run.rows``."""
    from verinoda import workflow

    store = run.store
    if f["id"] in run.done:
        return
    run.done.add(f["id"])
    was = f["status"]
    old = f.get("result") or {}
    verified: list[dict] = []
    if f["kind"] == "search":
        result, inputs, status, reason = _run_search(store, run.repo, f.get("definition") or {},
                                                     max(0.1, run.left()), files=run.files())
        diff = _site_diff(old, result)
        changed = bool(diff["added_count"] or diff["removed_count"] or old.get("total") != result["total"])
        row = {"fact": f["name"], "kind": "search", "was": was, "now": status, "changed": changed}
        if changed:
            row.update(old={"total": old.get("total"), "files_matched": old.get("files_matched")},
                       new={"total": result["total"], "files_matched": result["files_matched"]},
                       added=diff["added"], removed=diff["removed"])
        if diff["moved"]:
            row["moved"] = diff["moved"]
    else:
        d = f.get("definition") or {}
        for fid in d.get("facts") or []:
            inner = _by_id(store, fid)
            if inner and not inner.get("retired_at") and _needs_refresh(store, inner, {}) and run.left() > 0:
                _recompute(run, inner)
        if run.verify_claims:
            for cid in d.get("claims") or []:
                c = store.claim(cid)
                if c is None or c.get("superseded_by") or c["status"] != "stale" or run.left() <= 0:
                    continue
                try:
                    v = workflow.verify(store, run.repo, cid, wait=0)
                    verified.append({"claim": cid, "before": v["before"]["status"], "after": v["after"]["status"]})
                except Exception as exc:   # one claim never stops the others; listed
                    run.errors.append({"fact": f["name"], "claim": cid,
                                       "error": f"{type(exc).__name__}: {exc}"[:300]})
        states = _input_states(store, f, {})
        status = weakest(s["status"] for s in states)
        reason = "recomputed: the weakest of its inputs" + (f": {_why(states, status)}" if states else "")
        result = _derived_result(states)
        inputs = {"statuses": {s["id"]: s["status"] for s in states}}
        old_statuses = {i["id"]: i.get("status") for i in old.get("inputs") or []}
        new_statuses = {i["id"]: i.get("status") for i in result["inputs"]}
        changed = old.get("statement") != result["statement"]
        row = {"fact": f["name"], "kind": "derived", "was": was, "now": status, "changed": changed}
        if changed:
            row.update(old={"statement": old.get("statement")}, new={"statement": result["statement"]})
        moved = {k: [old_statuses.get(k), v] for k, v in new_statuses.items() if old_statuses.get(k) != v}
        if moved:
            row["inputs_changed"] = moved
        if verified:
            row["verified"] = verified
    payload = {k: row[k] for k in ("old", "new", "added", "removed", "inputs_changed", "verified") if k in row}
    fields = {"result": result, "inputs": inputs, "computed_at": now()}
    if status != was or row["changed"]:
        why = reason + ("; the result changed" if row["changed"] else "")
        _record(store, f, status=status, reason=why, actor=run.actor, payload=payload, fields=fields)
    else:   # the same result and status: only when it was computed
        _record(store, f, status=status, reason=f["reason"] or reason, actor=run.actor, fields=fields,
                history_row=False)
    run.rows.append(row)
    if row["changed"]:   # a fact resting on this one states its old result: stale, then recomputed if time allows
        for dep in _dependents(store, f["id"]):
            if dep["id"] in run.done:
                continue
            if dep["status"] != "stale":
                dep = _record(store, dep, status="stale", reason=f"input fact {f['name']} changed its result",
                              actor=run.actor)
            if run.left() > 0:
                _recompute(run, dep)


def refresh(store: Store, repo: Path, names: list[str] | None = None, *, limit: int = DEFAULT_LIMIT,
            budget: float = DEFAULT_BUDGET_S, verify_claims: bool = True, actor: str = "fact refresh") -> dict:
    """Recompute the named facts (default: every stale one, oldest first), at most ``limit`` within ``budget``
    seconds. ``verify_claims``: a stale claim a derived fact rests on is re-verified first (``verify``, static,
    after one index refresh), as ``consolidate`` does; without it the claims' statuses are read as they are."""
    from verinoda import consolidate

    repo = Path(repo).resolve()
    run = _Run(store, repo, budget=budget, verify_claims=verify_claims, actor=actor)
    if names:
        todo = []
        for n in dict.fromkeys(names):
            f = get(store, n)
            if f is None:
                raise FactError(f"no fact named {n!r} (`verinoda fact list`)")
            todo.append(f)
    else:
        memo: dict = {}
        todo = [f for f in live(store) if _needs_refresh(store, f, memo)]
    queue = todo[:max(0, limit)]
    not_run = None
    prev = getattr(_local, "busy", False)
    _local.busy = True
    try:
        if verify_claims and any(f["kind"] == "derived" for f in queue):
            not_run = consolidate._refresh(store, repo, run.t_end)   # one index refresh; no verify then waits
            run.verify_claims = not not_run
        for f in queue:
            if run.left() <= 0:
                break
            f = _by_id(store, f["id"])
            if f is None or f.get("retired_at") or f["id"] in run.done:
                continue
            try:
                _recompute(run, f)
            except Exception as exc:   # one fact never stops the others; listed
                run.errors.append({"fact": f["name"], "error": f"{type(exc).__name__}: {exc}"[:300]})
    finally:
        _local.busy = prev
    rows = run.rows
    reached = {r["fact"] for r in rows} | {e["fact"] for e in run.errors}
    return {"refreshed": rows, "restored": [r["fact"] for r in rows if r["was"] == "stale" and r["now"] != "stale"],
            "changed": [r["fact"] for r in rows if r["changed"]],
            "still_stale": [r["fact"] for r in rows if r["now"] == "stale"],
            "not_reached": [f["name"] for f in todo if f["name"] not in reached], "errors": run.errors,
            **({"index_not_refreshed": not_run} if not_run else {})}


def refresh_on_update(repo: Path) -> bool:
    from verinoda.paths import load_config

    try:
        return (load_config(repo).get("facts") or {}).get("refresh_on_update", True) is not False
    except (OSError, ValueError, AttributeError):
        return True


def after_update(store: Store, repo: Path, res: dict) -> dict | None:
    """What ``scan`` and ``update`` do with the facts: lower the ones whose inputs changed, then (unless
    ``facts.refresh_on_update`` is false, or inside a refresh) recompute the stale ones within a small budget,
    without running ``verify``. None when the project has no fact."""
    if res.get("mode") == "busy" or not store.one("SELECT 1 AS x FROM facts WHERE retired_at IS NULL LIMIT 1"):
        return None
    files = None
    snap = res.get("snapshot") or {}
    if (snap.get("id") and res.get("mode") in ("full", "incremental", "noop", None) and not res.get("error")
            and res.get("index_mode") != "deferred"):
        files = store.snapshot_files(snap["id"])    # the snapshot just recorded describes the tree
    lowered = invalidate(store, repo, files=files)
    out: dict = {"lowered": lowered}
    if getattr(_local, "busy", False) or not refresh_on_update(repo):
        return out
    r = refresh(store, repo, limit=ON_UPDATE_LIMIT, budget=ON_UPDATE_BUDGET_S, verify_claims=False,
                actor="update")
    out.update({k: r[k] for k in ("restored", "changed", "still_stale", "not_reached", "errors")})
    out["changes"] = [x for x in r["refreshed"] if x["changed"]]
    return out


def after_consolidate(store: Store, repo: Path, current: dict) -> dict:
    """``update --consolidate`` restored claims after :func:`after_update` ran: the stale facts resting on them are
    recomputed once more (the same budget, no ``verify``) and the counts merged into ``current``."""
    if current.get("error") or not refresh_on_update(repo):
        return current
    r = refresh(store, repo, limit=ON_UPDATE_LIMIT, budget=ON_UPDATE_BUDGET_S, verify_claims=False, actor="update")
    for k in ("restored", "changed", "errors"):
        current[k] = list((current.get(k) or [])) + [x for x in r[k] if x not in (current.get(k) or [])]
    current["still_stale"], current["not_reached"] = r["still_stale"], r["not_reached"]
    current["changes"] = list(current.get("changes") or []) + [x for x in r["refreshed"] if x["changed"]]
    return current


# -- views ----------------------------------------------------------------------------------------------

def _locators(store: Store, f: dict, n: int = 3) -> list[str]:
    if f["kind"] == "search":
        return [s["at"] for s in ((f.get("result") or {}).get("sites") or [])[:n]]
    out: list[str] = []
    for cid in (f.get("definition") or {}).get("claims") or []:
        for e in store.claim_evidence(cid):
            if e["relation"] == "supports" and e.get("locator") and e["locator"] not in out:
                out.append(e["locator"])
    return out[:n]


def _fact_view(store: Store, f: dict, memo: dict | None = None, *, full: bool = False) -> dict:
    st, why = effective(store, f, memo)
    res = f.get("result") or {}
    out = {"name": f["name"], "id": f["id"], "kind": f["kind"], "status": st, "statement": res.get("statement"),
           "reason": why or f.get("reason"), "computed_at": f["computed_at"], "updated_at": f["updated_at"]}
    if st != f["status"]:
        out["recorded_status"] = f["status"]
    if f["kind"] == "search":
        out.update(total=res.get("total"), files_matched=res.get("files_matched"),
                   complete=res.get("complete"), truncated=res.get("truncated"))
    if full:
        out["definition"] = f.get("definition")
        out["result"] = res
        out["inputs"] = (_input_states(store, f, memo if memo is not None else {}) if f["kind"] == "derived"
                         else f.get("inputs"))
        out["evidence"] = _locators(store, f, 20)
        out["history"] = [{k: h[k] for k in ("from_status", "to_status", "reason", "actor", "payload", "created_at")}
                          for h in history(store, f["id"])][-20:]
        users = [x["name"] for x in live(store) if f["id"] in ((x.get("definition") or {}).get("facts") or [])]
        if users:
            out["used_by"] = users
    else:
        out["at"] = _locators(store, f)
    return out


def listing(store: Store, *, status: str | None = None) -> dict:
    memo: dict = {}
    rows = [_fact_view(store, f, memo) for f in live(store)]
    if status:
        rows = [r for r in rows if r["status"] == status]
    return {"facts": rows, "count": len(rows), "status_filter": status}


def show(store: Store, name: str) -> dict:
    f = get(store, name)
    if f is None:
        raise FactError(f"no fact named {name!r} (`verinoda fact list`)")
    return _fact_view(store, f, full=True)


# -- leads for query and analyze ------------------------------------------------------------------------

def _tokens(text: str) -> set[str]:
    words = re.findall(r"[A-Za-z0-9]+", re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text or ""))
    return {w.lower() for w in words if len(w) >= 3 and w.lower() not in _STOP}


def leads(store: Store, question: str, limit: int = LEADS) -> list[dict]:
    """Facts a question names (a word of the fact's name, or two words of its statement): leads with their
    status as it is now, never evidence. Nothing is written."""
    q = _tokens(question)
    low = (question or "").lower()
    if not q and not low.strip():
        return []
    memo: dict = {}
    scored = []
    for f in live(store):
        name_t = _tokens(f["name"].replace("-", " ").replace("_", " ").replace(".", " "))
        d = f.get("definition") or {}
        # a search fact's statement is a template ("matches N line(s) ..."): its pattern and paths are what it is
        body = (_tokens(" ".join([d.get("pattern") or "", *(d.get("paths") or [])])) if f["kind"] == "search"
                else _tokens((f.get("result") or {}).get("statement") or ""))
        score = 3 * len(q & name_t) + len(q & body) + (5 if f["name"].lower() in low else 0)
        if score >= 2:
            scored.append((score, f))
    scored.sort(key=lambda x: -x[0])
    out = []
    for _, f in scored[:limit]:
        v = _fact_view(store, f, memo)
        lead = {"fact": v["name"], "status": v["status"], "statement": v["statement"], "at": v["at"]}
        if v["status"] in ("stale", "contradicted") or v.get("recorded_status"):
            lead["note"] = v["reason"]
        if f["kind"] == "search":
            lead["total"] = v["total"]
        out.append(lead)
    return out


def attach_leads(result: dict, repo: Path, question: str) -> dict:
    """``result["facts"]``: the facts a ``query`` question names (no store, no facts: nothing added). A failure
    to read them is said in ``facts_error``, never raised: the answer stands without them."""
    from verinoda.paths import db_path
    from verinoda.store import open_store

    result.pop("facts", None)
    result.pop("facts_error", None)
    try:
        if not db_path(repo).is_file():
            return result
        st = open_store(repo, create=False)
        try:
            found = leads(st, question)
        finally:
            st.close()
    except Exception as exc:  # noqa: BLE001 - the answer stands; the error is reported with it
        result["facts_error"] = f"{type(exc).__name__}: {exc}"[:200]
        return result
    if found:
        result["facts"] = found
    return result


def lead_lines(items: list[dict] | None) -> list[str]:
    """Plain-text lines for the leads (the query text and the analyze text)."""
    if not items:
        return []
    out = ["facts (kept with `verinoda fact add`; leads with their status now, not evidence - cite the lines they "
           "rest on; `verinoda fact show NAME`):"]
    for x in items:
        at = "; ".join(x.get("at") or [])
        out.append(f"  {x['fact']} [{x['status']}]: {x.get('statement') or ''}"[:400]
                   + (f" {{{at}}}" if at else "") + (f" ({x['note']})"[:300] if x.get("note") else ""))
    return out


# -- text -----------------------------------------------------------------------------------------------

def render_list(res: dict) -> str:
    if not res["facts"]:
        return "no facts" + (f" with status {res['status_filter']}" if res.get("status_filter") else "") + \
            " (`verinoda fact add NAME --from-claim ID | --search PATTERN`)"
    out = []
    for f in res["facts"]:
        out.append(f"{f['name']} [{f['status']}] ({f['kind']}): {f.get('statement') or ''}"[:300])
        if f["status"] in ("stale", "contradicted") or f.get("recorded_status"):
            out.append(f"  why: {f.get('reason')}"[:300])
    return "\n".join(out)


def render_show(f: dict) -> str:
    out = [f"{f['name']} [{f['status']}] ({f['kind']}, {f['id']})", f"  {f.get('statement') or ''}"]
    if f.get("recorded_status"):
        out.append(f"  recorded {f['recorded_status']}; capped by its inputs now")
    if f.get("reason"):
        out.append(f"  why: {f['reason']}")
    out.append(f"  computed {f['computed_at']}")
    if f["kind"] == "derived":
        for s in f.get("inputs") or []:
            out.append(f"  rests on {s['type']} {s.get('name') or s['id']} [{s['status']}]: {s.get('text', '')}"[:300]
                       + (f" ({s['why']})" if s.get("why") else ""))
    else:
        res = f.get("result") or {}
        for s in (res.get("sites") or [])[:30]:
            out.append(f"  {s['at']}: {s['text']}"[:300])
        more = (res.get("total") or 0) - min(30, len(res.get("sites") or []))
        if more > 0:
            out.append(f"  ... {more} more (--json lists the {len(res.get('sites') or [])} kept)")
    if f.get("evidence") and f["kind"] == "derived":
        out.append("  evidence: " + "; ".join(f["evidence"]))
    if f.get("used_by"):
        out.append("  used by: " + ", ".join(f["used_by"]))
    for h in (f.get("history") or [])[-5:]:
        out.append(f"  {h['created_at']} {h['from_status'] or '-'} -> {h['to_status']} ({h['actor']}): "
                   f"{h['reason']}"[:300])
    return "\n".join(out)


def _change_lines(r: dict) -> list[str]:
    out = []
    if r.get("old") is not None:
        if r["kind"] == "search":
            out.append(f"    was {r['old']['total']} match(es) in {r['old']['files_matched']} file(s), now "
                       f"{r['new']['total']} in {r['new']['files_matched']}")
            out += [f"    + {a}"[:300] for a in (r.get("added") or [])[:10]]
            out += [f"    - {a}"[:300] for a in (r.get("removed") or [])[:10]]
        else:
            out.append(f"    was: {r['old']['statement']}"[:300])
            out.append(f"    now: {r['new']['statement']}"[:300])
    for k, (a, b) in (r.get("inputs_changed") or {}).items():
        out.append(f"    input {k}: {a} -> {b}")
    return out


def render_refresh(res: dict) -> str:
    rows = res["refreshed"]
    out = [f"recomputed {len(rows)} fact(s): {len(res['restored'])} restored, {len(res['changed'])} with a changed "
           f"result, {len(res['still_stale'])} still stale"
           + (f", {len(res['not_reached'])} not reached (limit or time budget)" if res["not_reached"] else "")]
    if res.get("index_not_refreshed"):
        out.append(f"  claims not re-verified: {res['index_not_refreshed']}")
    for r in rows:
        out.append(f"  {r['fact']}: {r['was']} -> {r['now']}" + ("  (result changed)" if r["changed"] else ""))
        out += _change_lines(r)
    for e in res["errors"][:10]:
        out.append(f"  error {e['fact']}" + (f" (claim {e['claim']})" if e.get("claim") else "") + f": {e['error']}")
    return "\n".join(out)


def update_line(d: dict | None) -> str | None:
    """The ``update`` / ``scan`` line (None when there is nothing to say)."""
    if not d:
        return None
    if d.get("error"):
        return f"facts: not checked ({d['error']})"
    low = d.get("lowered") or []
    parts = []
    if low:
        parts.append(f"{len(low)} lowered ({', '.join(x['fact'] + ' -> ' + x['now'] for x in low[:5])})")
    if d.get("restored"):
        parts.append(f"{len(d['restored'])} recomputed and restored")
    if d.get("changed"):
        parts.append(f"{len(d['changed'])} with a changed result ({', '.join(d['changed'][:5])}: "
                     "`verinoda fact show NAME`)")
    if d.get("still_stale"):
        parts.append(f"{len(d['still_stale'])} still stale (`verinoda fact refresh` re-verifies their claims)")
    if d.get("not_reached"):
        parts.append(f"{len(d['not_reached'])} not recomputed (budget)")
    if d.get("errors"):
        parts.append(f"{len(d['errors'])} error(s) (`verinoda fact refresh` lists them)")
    return ("facts: " + "; ".join(parts)) if parts else None
