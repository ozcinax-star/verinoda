"""Operations shared by the CLI and the MCP server (one implementation each)."""

from __future__ import annotations

import time
from pathlib import Path

from verinoda import evidence as evmod
from verinoda import index
from verinoda.claims import ACTIVE, CONFIDENCE_CAP, RECHECK_TYPES, Claims, assess_change, invalidate_stale
from verinoda.paths import ensure_atlas, graph_path
from verinoda.snapshot import changed_files, current_state, take_snapshot
from verinoda.store import Store

# Evidence types whose cited lines can be re-read and re-hashed right now (a static resolver's
# answer is anchored on the call line it resolved).
RECHECKABLE = RECHECK_TYPES


def init(repo: Path) -> dict:
    import json

    from verinoda.paths import DEFAULT_CONFIG, atlas_dir
    from verinoda.store import open_store

    repo = Path(repo).resolve()
    ensure_atlas(repo)
    cfg = atlas_dir(repo) / "config.json"
    created = not cfg.exists()
    if created:
        cfg.write_text(json.dumps(DEFAULT_CONFIG, indent=2), encoding="utf-8")
    open_store(repo).close()
    return {"repo": str(repo), "atlas_dir": str(atlas_dir(repo)), "config_created": created}


def _index_refused(store: Store, repo: Path, stats: dict, *, force: bool, files: dict[str, str] | None = None
                   ) -> dict:
    """The indexer did not rewrite the graph: keep the previous snapshot, say so, say what to do.

    No snapshot is recorded (it would bind new claims to a graph that describes
    an older tree). Claims whose files changed are still marked stale - that
    needs only file hashes, not the graph.
    """
    files = current_state(repo)["files"] if files is None else files
    stale = invalidate_stale(store, None, files=files)
    what = ("the indexer did not rewrite the graph" + (" even with --force" if force else
            " (it refuses a rebuild that would shrink the graph sharply, e.g. after deleting code)")
            + "; no new snapshot was recorded, the index still describes an older tree")
    log = (stats.get("log") or "").strip().splitlines()
    return {"snapshot": store.latest_snapshot(), "error": what,
            "hint": f"verinoda scan {repo} --force",
            "index_log_tail": [ln[:200] for ln in log[-3:]], "stale": stale,
            "graph": {k: stats.get(k) for k in ("nodes", "edges", "graph_path")}}


def _graph_counts(repo: Path, stats: dict) -> dict:
    """``stats`` with node/edge counts re-read from graph.json (after a prune)."""
    import json

    try:
        data = json.loads(graph_path(repo).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return stats
    return {**stats, "nodes": len(data.get("nodes", [])), "edges": len(data.get("links", data.get("edges", [])))}


def _no_missing_files(repo: Path, stats: dict) -> tuple[dict, list[str]]:
    """The graph must never describe a deleted file: prune what the indexer kept."""
    pruned = index.prune_missing_files(repo)
    return (_graph_counts(repo, stats) if pruned else stats), pruned


def _call_hook(fn, *args, **kwargs) -> dict:
    t0 = time.monotonic()
    try:
        res = fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - derived data never fails a scan; the error is reported
        return {"error": f"{type(exc).__name__}: {exc}"[:300], "seconds": round(time.monotonic() - t0, 3)}
    out = res if isinstance(res, dict) else {"result": res}
    return {**out, "seconds": out.get("seconds", round(time.monotonic() - t0, 3))}


def _derive(store: Store, repo: Path, *, changed: list[str] | None, all_files: list[str],
            file_hashes: dict[str, str] | None = None, tree: str | None = None) -> dict:
    """Refresh the data derived from the graph after an index build.

    ``search_index.update`` first, then - when those modules are installed -
    ``lexicon.build`` and ``anchors.update_facts``. ``changed`` is the list of
    changed paths (None after a full scan, when ``all_files`` is passed to
    ``update_facts``). Errors are reported per step, never raised.
    """
    import importlib
    import inspect

    from verinoda import search_index

    out: dict = {}
    try:
        g = index.load(repo)
    except (OSError, ValueError) as exc:
        return {"error": f"graph not loadable: {type(exc).__name__}: {exc}"[:300]}
    out["search_index"] = _call_hook(search_index.update, repo, g, changed)
    for mod_name, fn_name in (("verinoda.lexicon", "build"), ("verinoda.anchors", "update_facts")):
        try:
            mod = importlib.import_module(mod_name)
        except Exception:  # noqa: BLE001 - the module is not installed: nothing to derive
            continue
        fn = getattr(mod, fn_name, None)
        if not callable(fn):
            continue
        if fn_name == "build":
            kwargs: dict = {"changed": changed}
            try:
                params = inspect.signature(fn).parameters
            except (TypeError, ValueError):
                params = {}
            if file_hashes is not None and "file_hashes" in params:
                kwargs["file_hashes"] = file_hashes
            if tree is not None and "tree_hash" in params:
                kwargs["tree_hash"] = tree
            out["lexicon"] = _call_hook(fn, repo, g, **kwargs)
        else:
            out["anchors"] = _call_hook(fn, store, repo, all_files if changed is None else changed)
    return out


def scan(store: Store, repo: Path, *, force: bool = False) -> dict:
    """Full index build, snapshot, stale invalidation and derived data.

    A graph that still describes deleted files is rebuilt with ``force`` (the
    indexer otherwise refuses a rebuild that shrinks the graph), and anything
    it keeps is pruned: no node may point at a missing file.
    """
    repo = Path(repo).resolve()
    t0 = time.monotonic()
    had_graph = graph_path(repo).exists()
    missing_before = index.missing_source_files(repo) if had_graph else []
    force = force or not had_graph or bool(missing_before)
    stats = index.build(repo, force=force)
    t_index = time.monotonic() - t0
    if not stats.get("ok", True):
        return {**_index_refused(store, repo, stats, force=force), "index_seconds": round(t_index, 3)}
    stats, pruned = _no_missing_files(repo, stats)
    snap = take_snapshot(store, repo, graph_stats=stats)
    stale = invalidate_stale(store, snap)
    files = store.snapshot_files(snap["id"])
    out = {"snapshot": snap, "graph": {k: stats[k] for k in ("nodes", "edges", "graph_path")},
           "index_seconds": round(t_index, 3), "stale": stale,
           "derived": _derive(store, repo, changed=None, all_files=sorted(files), file_hashes=files,
                              tree=snap.get("tree_hash"))}
    if missing_before:
        out["forced_for_deleted_files"] = missing_before[:20]
    if pruned:
        out["pruned_missing_files"] = pruned[:20]
    return out


def update(store: Store, repo: Path) -> dict:
    """Incremental: re-extract only files changed since the last snapshot.

    When files were *added* the whole index is rebuilt instead: unchanged files
    that import or call the new (or restored) file were extracted while it did
    not exist, and an incremental pass would leave their cross-file edges
    unresolved. When files were *removed* and the indexer refuses the shrunken
    graph, the rebuild is repeated in full with force; in every case any node
    still pointing at a missing file is pruned. ``index_mode`` in the result
    says which rebuild ran.

    When the indexer refuses to rewrite the graph the previous snapshot is
    returned with ``error`` and ``hint`` (``mode`` = ``"index_refused"``); no
    snapshot is recorded over the stale graph.
    """
    repo = Path(repo).resolve()
    prev = store.latest_snapshot()
    if prev is None or not graph_path(repo).exists():
        res = scan(store, repo)
        res["changed_count"] = (res["snapshot"] or {}).get("file_count", 0)
        res["mode"] = "index_refused" if res.get("error") else "full"
        res["index_mode"] = "full"
        return res
    state = current_state(repo, store=store)
    diff = changed_files(store.snapshot_files(prev["id"]), state["files"])
    changed = diff["added"] + diff["modified"] + diff["removed"]
    t0 = time.monotonic()
    stats = None
    index_mode = "none"
    if diff["added"]:
        stats = index.build(repo, force=True)
        index_mode = "full"
    elif changed:
        stats = index.build(repo, changed=[repo / p for p in changed])
        index_mode = "incremental"
        if not stats.get("ok", True) and diff["removed"]:
            # The indexer refuses a graph that shrinks; removed files explain the
            # shrink, so rebuild in full with force rather than keep their nodes.
            stats = index.build(repo, force=True)
            index_mode = "full"
    t_index = time.monotonic() - t0
    if stats is not None and not stats.get("ok", True):
        return {**_index_refused(store, repo, stats, force=index_mode == "full", files=state["files"]),
                "changed": diff, "changed_count": len(changed), "mode": "index_refused",
                "index_mode": index_mode, "index_seconds": round(t_index, 3)}
    if not changed and prev["commit_sha"] == state["commit"]:
        # Nothing tracked changed, but a claim may cite a file the snapshot does not hash
        # (gitignored or untracked): re-check those citations against the working tree.
        untracked = _untracked_citations(store, store.snapshot_files(prev["id"]))
        stale = invalidate_stale(store, None, files=state["files"], repo=repo) if untracked else []
        return {"snapshot": prev, "changed": diff, "changed_count": 0, "stale": stale, "mode": "noop",
                "index_mode": "none", "index_seconds": 0.0,
                **({"untracked_citations": untracked[:20]} if untracked else {})}
    pruned: list[str] = []
    if stats is not None:
        stats, pruned = _no_missing_files(repo, stats)
    snap = take_snapshot(store, repo, graph_stats=stats or {
        "graph_path": prev["graph_path"], "nodes": prev["graph_nodes"], "edges": prev["graph_edges"]})
    stale = invalidate_stale(store, snap)
    out = {"snapshot": snap, "changed": diff, "changed_count": len(changed), "stale": stale,
           "mode": "incremental", "index_mode": index_mode, "index_seconds": round(t_index, 3)}
    if changed:
        files = store.snapshot_files(snap["id"])
        out["derived"] = _derive(store, repo, changed=changed, all_files=sorted(files), file_hashes=files,
                                 tree=snap.get("tree_hash"))
    if pruned:
        out["pruned_missing_files"] = pruned[:20]
    return out


def _untracked_citations(store: Store, files: dict[str, str]) -> list[str]:
    """Paths cited by active claims' file evidence that are not in ``files`` (the snapshot's map)."""
    rows = store.all(
        "SELECT DISTINCT e.path AS path, e.meta AS meta FROM claim_evidence ce JOIN evidence e ON e.id = ce.evidence_id"
        " JOIN claims c ON c.id = ce.claim_id WHERE e.path IS NOT NULL AND e.line_start IS NOT NULL"
        f" AND c.superseded_by IS NULL AND c.status IN ({','.join('?' * len(ACTIVE))})"
        f" AND e.source_type IN ({','.join('?' * len(RECHECK_TYPES))})", (*ACTIVE, *RECHECK_TYPES))
    out = []
    for r in rows:
        meta = r.get("meta") or {}
        if isinstance(meta, dict) and meta.get("root"):
            continue  # a reference checkout, not this working tree
        if r["path"] not in files and r["path"] not in out:
            out.append(r["path"])
    return sorted(out)


def _current_snapshot(store: Store, repo: Path) -> tuple[dict | None, dict | None]:
    """A snapshot of the working tree as it is now (refreshing the index if needed).

    Returns ``(snapshot, refresh)`` where ``refresh`` summarises the update
    that had to run, or None when the latest snapshot already matched. When
    the index refused to rebuild, ``refresh`` carries ``error``/``hint`` and
    the snapshot returned is the previous one, which does NOT describe the
    current tree.
    """
    snap = store.latest_snapshot()
    if snap is not None and snap["tree_hash"] == current_state(repo, store=store)["tree_hash"]:
        return snap, None
    if graph_path(repo).exists():
        res = update(store, repo)
        refresh = {"mode": res["mode"], "changed_count": res["changed_count"],
                   "stale": [s["id"] for s in res["stale"]]}
        if res.get("error"):
            refresh.update(error=res["error"], hint=res["hint"])
        return res["snapshot"], refresh
    # A project that was never scanned (claims added by hand): record the
    # tree without building an index.
    snap = take_snapshot(store, repo)
    stale = invalidate_stale(store, snap)
    return snap, {"mode": "snapshot_only", "changed_count": None, "stale": [s["id"] for s in stale]}


def verify(store: Store, repo: Path, cid: str, *, run: bool = False) -> dict:
    """Re-check a claim's evidence against the current tree; optionally re-run its experiment.

    A claim is restored (up to its recorded ceiling) and re-bound to a snapshot
    of the current tree only when every file behind it is either unchanged
    since the claim's snapshot or re-confirmed now (cited lines hash-match,
    or a fresh experiment run passed). Older experiment results are never
    carried over to changed code.
    """
    repo = Path(repo).resolve()
    cl = Claims(store, repo)
    c = cl.get(cid)
    before = {"status": c["status"], "confidence": c["confidence"]}
    snap, refresh = _current_snapshot(store, repo)
    # A refused index rebuild leaves no snapshot of the current tree: compare
    # against the working tree directly and do not re-bind the claim.
    current = not (refresh and refresh.get("error"))
    c = cl.get(cid)  # the refresh may have marked it stale
    checks = []
    all_ok = True
    confirmed: set[str] = set()
    for e in cl.evidence(cid):
        if e["source_type"] in RECHECKABLE and e.get("path") and e.get("line_start"):
            chk = evmod.check_source(repo, e)
            # details(): status (same / moved / changed / gone / ambiguous) and a candidate location
            checks.append({"evidence": e["id"], "at": e["locator"], **chk.details()})
            if e["relation"] == "supports":
                if not chk.ok:
                    all_ok = False
                elif not (e.get("meta") or {}).get("root"):
                    confirmed.add(e["path"])
    reran = None
    spec = c.get("spec") or {}
    if run and spec.get("command"):
        from verinoda import experiments

        reran = experiments.run(store, repo, [experiments.python_for(repo), "-m", "pytest", "-q", *spec["command"]],
                                hypothesis=f"re-verify: {c['text']}", claim_id=cid,
                                commit=(snap or {}).get("commit_sha") if current else None)
    inconclusive = reran is not None and reran["outcome"] == "inconclusive"
    old_files = store.snapshot_files(c["snapshot_id"]) if c["snapshot_id"] else {}
    if current:
        now_files = store.snapshot_files(snap["id"]) if snap else {}
    else:  # no snapshot of the current tree: compare with the working tree itself (stat-cached hashes)
        now_files = current_state(repo, store=store)["files"]
    # facet-level: only what this claim depends on counts (claims.assess_change), and a changed
    # file still counts as re-confirmed when the claim's evidence in it re-checks now
    assessed = assess_change(store, repo, c, old_files, now_files)
    changed = sorted({t["file"] for t in assessed["changed"] if t["file"] not in confirmed})
    if reran is not None and reran["matches_expectation"]:
        changed = []  # a fresh run on the current tree re-established it
    stale_conf = min(c["confidence"] or 0.0, CONFIDENCE_CAP["stale"])
    unconfirmed = []
    note = None
    if reran is not None and not reran["matches_expectation"] and not inconclusive:
        # A fresh run on the current tree is direct refutation (attached by
        # experiments.run); older passing runs do not outweigh it.
        after = cl.set_status(cid, "contradicted", actor="verify", downgrade=True,
                              reason=f"verify: re-run {reran['id']} {reran['outcome']} on the current tree",
                              payload={"experiment": reran["id"], "checks": checks},
                              confidence=min(c["confidence"] or 0.0, CONFIDENCE_CAP["contradicted"]))
    elif not all_ok:
        after = cl.set_status(cid, "stale", reason="verify: cited source changed", actor="verify",
                              payload={"checks": checks}, confidence=stale_conf)
    elif changed and c["status"] != "contradicted":
        unconfirmed = changed
        note = ("verify: " + ", ".join(changed) + " changed since the claim's snapshot and no re-checkable "
                "evidence covers it" + ("; re-run with --run" if spec.get("command") and not run else "")
                + (f"; the re-run was inconclusive ({reran.get('inconclusive_reason')})" if inconclusive else ""))
        after = cl.set_status(cid, "stale", actor="verify", confidence=stale_conf, payload={"unconfirmed": changed},
                              reason=note)
    else:
        after = cl.reassess(cid, reason="verify: evidence re-checked", actor="verify")
        if current and snap and c["snapshot_id"] != snap["id"] and after["status"] not in ("stale", "contradicted"):
            extra = {"snapshot_id": snap["id"], "commit_sha": snap["commit_sha"]}
            after = cl.set_status(cid, after["status"], reason="verify: rebound to current snapshot",
                                  actor="verify", extra_fields=extra, payload=extra,
                                  confidence=after["confidence"])
    out = {"claim": cid, "text": c["text"], "before": before,
           "after": {"status": after["status"], "confidence": after["confidence"]},
           "source_checks": checks, "experiment": reran,
           "snapshot": {"id": after["snapshot_id"], "commit": after["commit_sha"]}}
    if assessed["facets"]:
        out["changed_facets"] = [{k: f[k] for k in ("dep", "facet", "file", "change") if k in f}
                                 for f in assessed["facets"][:10]]
    if unconfirmed:
        out["unconfirmed_files"] = unconfirmed
        out["note"] = note
    elif inconclusive:
        out["note"] = (f"verify: the re-run was inconclusive ({reran.get('inconclusive_reason')}); "
                       "it neither confirms nor refutes the claim")
    if refresh:
        out["index_refresh"] = refresh
    return out
