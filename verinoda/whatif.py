"""What-if refactoring (``verinoda what-if --move OLD=NEW``): move or rename files and folders in memory, then
re-check the architecture rules and the dependency cycles against the new paths. Nothing is edited.

A move relabels the file paths of the loaded graph (:class:`MovedGraph`): every node keeps its edges, only the file
the graph says it is in changes. The edge guards of the decision records (``no_edge``, ``layers``,
``allow_edges``, ``public``: :mod:`verinoda.guards`) and the cycles view (:func:`verinoda.architecture_map.
cycle_components`) are computed twice, on the graph as it is and on the moved one, and compared:

- **rules**: the findings (VIOLATED and POSSIBLE sites) the move would add or remove, each as the guard reports
  it, with the line where the code is now (a move changes where a file is, not its code), and the checks that
  could no longer be completed (a glob that would match no file); the findings of a guard the move leaves unable
  to run are counted as not re-checked, not as removed;
- **cycles**: the cycles the move would create, break or reshape. Only a merge changes them: renaming files
  leaves the file graph the same shape, but two files moved onto one path (``--move a.py=b.py`` where ``b.py``
  exists) are one file, so a dependency between them disappears and their other dependencies meet. A cycle is
  compared by its files, the old ones renamed as the move renames them: a cycle after the move that shares files
  with cycles before it is ``changed`` (it grows, joins them or shrinks), neither added nor removed.

The rules and the cycles are strong_inference at most for the same reasons as their own commands (graph edges are
extractions); a move adds one more assumption: the code that names a moved module (an import) is updated with it,
so every edge stays as the index extracted it. A finding the move would add holds only under that assumption, so
its status is capped at strong_inference (the guard's statically_verified is about the line as it reads today).
"""
from __future__ import annotations

import posixpath
import re
from collections import defaultdict
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath

import networkx as nx

from verinoda.index import Graph

LISTED = 40          # findings listed per side (added, removed); all are counted
CYCLES_LISTED = 20   # cycles listed per side
CYCLE_FILES = 20     # files listed per cycle
CYCLE_DEPS = 8       # dependencies listed per cycle, the heaviest first
UNKNOWN_LISTED = 10
FINDING_KEYS = ("violations", "possible", "pre_existing")


class WhatIfError(ValueError):
    pass


@dataclass
class MovedGraph(Graph):
    """The graph with some files at other paths: :meth:`file` answers with the new path; node data, labels and
    the edges' own ``source_file`` (the file a cited line is in) stay as they are, so every line is read where the
    code is now."""

    moves: dict[str, str] = field(default_factory=dict)

    def file(self, nid: str) -> str | None:
        f = Graph.file(self, nid)
        return self.moves.get(f, f) if f else None

    def edges(self, relations: set[str] | None = None):
        for u, v, d in Graph.edges(self, relations):
            if not d.get("source_file"):  # the cited line is in the file the edge leaves, where it is now
                d = {**d, "source_file": Graph.file(self, u)}
            yield u, v, d


def moved(g: Graph, moves: dict[str, str]) -> MovedGraph:
    return MovedGraph(G=g.G, path=g.path, root=g.root, _spans=g._spans, _span_how=g._span_how, moves=dict(moves))


def _norm(p: str) -> str:
    """The path with ``/`` separators and no ``.``, empty or trailing parts (``app/./ui//x.py/`` is
    ``app/ui/x.py``); ``""`` for the project root. A leading ``/`` or ``..`` stays, for :func:`_checked`."""
    s = p.strip().replace("\\", "/")
    s = posixpath.normpath(s) if s else "."
    return "" if s == "." else s


def _checked(p: str, spec: str) -> str:
    if not p or p.startswith("/") or re.match(r"^[A-Za-z]:", p) or ".." in PurePosixPath(p).parts:
        raise WhatIfError(f"--move {spec}: paths are relative to the project root and stay inside it")
    return p


def plan(files: list[str], specs: list[str]) -> tuple[dict[str, str], list[dict], list[dict]]:
    """``(moves, rows, merges)`` for ``--move OLD=NEW`` specs over the indexed ``files``: ``moves`` maps each file
    that moves to its new path; OLD is a file or a folder of the index; NEW is the new path, or, with a trailing
    ``/``, ``.`` (the project root) or an existing folder, the folder it goes into (as ``git mv`` does). A file
    two moves would move, a move that moves nothing, a folder moved into itself and a file put where an indexed
    file or folder is are refused. ``merges`` lists the paths two or more files would share."""
    fileset = set(files)
    folders = {str(p) for f in files for p in PurePosixPath(f).parents if str(p) != "."}
    mapping: dict[str, str] = {}
    rows = []
    for spec in specs:
        old, sep, new = str(spec).partition("=")
        if not sep or not old.strip() or not new.strip():
            raise WhatIfError(f"--move {spec}: a move is OLD=NEW (a file or folder of the index, and where it goes)")
        into = new.strip().replace("\\", "/").endswith("/")
        old, new = _checked(_norm(old), spec), _norm(new)
        if new:
            _checked(new, spec)
        into = into or not new or new in folders
        if old in fileset:
            kind = "file"
            base = posixpath.join(new, PurePosixPath(old).name) if into else new
            hits = {old: base}
        elif old in folders:
            kind = "folder"
            base = posixpath.join(new, PurePosixPath(old).name) if into else new
            if base.startswith(old + "/"):
                raise WhatIfError(f"--move {spec}: a folder cannot move into itself")
            hits = {f: base + f[len(old):] for f in files if f.startswith(old + "/")}
        else:
            raise WhatIfError(f"--move {spec}: {old} is no file or folder of the index")
        if all(k == v for k, v in hits.items()):
            raise WhatIfError(f"--move {spec}: nothing would move")
        twice = sorted(f for f in hits if f in mapping)
        if twice:
            raise WhatIfError(f"--move {spec}: {twice[0]} is moved by an earlier --move too")
        staying = fileset - set(mapping) - set(hits)
        for dest in hits.values():
            if dest in folders:
                raise WhatIfError(f"--move {spec}: {dest} is a folder of the index, not a file path")
            under = next((str(q) for q in PurePosixPath(dest).parents if str(q) in staying), None)
            if under:
                raise WhatIfError(f"--move {spec}: {under} is a file of the index, not a folder")
        mapping.update({k: v for k, v in hits.items() if k != v})
        rows.append({"from": old, "to": base, "kind": kind, "files": len(hits)})
    landing: dict[str, list[str]] = defaultdict(list)
    for f in files:
        landing[mapping.get(f, f)].append(f)
    merges = [{"into": t, "files": sorted(fs)} for t, fs in sorted(landing.items()) if len(fs) > 1]
    return mapping, rows, merges


# -- rules -------------------------------------------------------------------------------------------

def _edge_records(recs: list) -> tuple[list, int, int]:
    """The records with only their edge guards, the number of accepted edge guards, and the number of the other
    checks of enforced records (guards, governs, revisit conditions), which a move does not change here."""
    from verinoda import decisions as dm

    out, edge, other = [], 0, 0
    for d in recs:
        keep = [g for g in d.guards if g.get("kind") in dm.EDGE_KINDS]
        if d.enforced:
            edge += sum(1 for g in keep if g.get("status") == "accepted")
            other += sum(1 for g in d.guards if g.get("kind") not in dm.EDGE_KINDS and g.get("status") == "accepted")
            other += len(d.governs) + len(d.revisit_when)
        out.append(replace(d, guards=keep, governs=[], revisit_when=[], warnings=[]))
    return out, edge, other


def _fkey(f: dict) -> tuple:
    return (f.get("decision"), f.get("guard"), f.get("at"), f.get("level"))


def _finding(f: dict, *, simulated: bool = False) -> dict:
    out = {k: f[k] for k in ("decision", "guard", "kind", "level", "status", "at", "line", "why") if f.get(k)}
    if simulated and out.get("status") == "statically_verified":
        # the line names the target as it is today; the site exists only if the move updates it as assumed
        out["status"] = "strong_inference"
    return out


def _findings(res: dict) -> list[dict]:
    return [f for k in FINDING_KEYS for f in res.get(k) or []]


def _rules(repo: Path, g: Graph, mg: MovedGraph, recs: list, decisions_dir: str | None) -> dict:
    from verinoda import guards

    before = guards.check(repo, graph=g, records=recs, decisions_dir=decisions_dir, use_baseline=False)
    after = guards.check(repo, graph=mg, records=recs, decisions_dir=decisions_dir, use_baseline=False)
    fb, fa = _findings(before), _findings(after)
    # a guard that checks nothing reports no finding: one the move leaves unable to run fixes none of its
    # findings, they are not re-checked. An unknown is new when its guard ran before the move (its text may
    # only count files differently)
    gb = {(u.get("decision"), u.get("guard")) for u in before["unknown"]}
    ga = {(u.get("decision"), u.get("guard")) for u in after["unknown"]}
    stopped = ga - gb
    kb, ka = {_fkey(f) for f in fb}, {_fkey(f) for f in fa}
    added = [_finding(f, simulated=True) for f in fa if _fkey(f) not in kb]
    gone = [f for f in fb if _fkey(f) not in ka]
    removed = [_finding(f) for f in gone if (f.get("decision"), f.get("guard")) not in stopped]
    unknown = [{**{k: u[k] for k in ("decision", "guard", "kind", "why") if u.get(k)},
                **({"new": True} if (u.get("decision"), u.get("guard")) in stopped else {})}
               for u in after["unknown"]]
    unknown.sort(key=lambda u: not u.get("new"))
    out = {"added": added[:LISTED], "removed": removed[:LISTED], "unchanged": len(ka & kb),
           "violations_before": len(before["violations"]), "violations_after": len(after["violations"])}
    if len(gone) > len(removed):
        out["not_rechecked"] = len(gone) - len(removed)
    if len(added) > LISTED or len(removed) > LISTED:
        out.update({"added_total": len(added), "removed_total": len(removed), "truncated": True})
    if unknown:
        out["unknown"] = unknown[:UNKNOWN_LISTED]
        out["unknown_new"] = sum(1 for u in unknown if u.get("new"))
    return out


# -- cycles ------------------------------------------------------------------------------------------

def _cycle(files: list[str], deps: dict, claim: str) -> dict:
    fs = set(files)
    inner = {e: d for e, d in deps.items() if e[0] in fs and e[1] in fs}
    xg = nx.DiGraph()
    xg.add_nodes_from(files)
    xg.add_edges_from(e for e, d in inner.items() if d["extracted"])
    strong = nx.is_strongly_connected(xg)
    names = ", ".join(files[:4]) + (f" and {len(files) - 4} more" if len(files) > 4 else "")
    heavy = sorted(inner, key=lambda e: (-inner[e]["references"], e))[:CYCLE_DEPS]
    return {"files": files[:CYCLE_FILES], "size": len(files), "claim": claim.format(names=names),
            "status": "strong_inference" if strong else "weak_inference",
            "dependencies": [{"from": a, "to": b, "references": inner[(a, b)]["references"],
                              "at": [at for _, at in sorted(inner[(a, b)]["sites"])][:1]} for a, b in heavy],
            **({"dependencies_total": len(inner)} if len(inner) > len(heavy) else {})}


def _cycles(g: Graph, mg: MovedGraph) -> dict:
    """The cycles before (renamed as the move renames their files) and after, compared: one after that is one of
    before is unchanged; one that shares no file with any before is ``added``; one before that shares no file
    with any after is ``removed``; the others are ``changed``: each cycle after with the cycles before it shares
    files with (``was``: their sizes), ``grows`` when it holds a file none of them held or joins two of them,
    ``shrinks`` when one of them has a file it no longer holds, or both."""
    from verinoda import architecture_map as am

    cb, db = am.cycle_components(g)
    ca, da = am.cycle_components(mg)
    before = [frozenset(mg.moves.get(f, f) for f in c) for c in cb]
    after = [frozenset(c) for c in ca]
    same = set(before) & set(after)
    added = [c for c, k in zip(ca, after) if not any(k & b for b in before)]
    removed = [c for c, k in zip(cb, before) if not any(k & a for a in after)]
    changed = []
    for c, k in zip(ca, after):
        if k in same:
            continue
        was = [(b, len(old)) for b, old in zip(before, cb) if k & b]
        if not was:
            continue
        grows = bool(k - frozenset().union(*(b for b, _ in was))) or len(was) > 1
        shrinks = any(b - k for b, _ in was)
        changed.append((c, [n for _, n in was], grows, shrinks))
    out = {"before": len(cb), "after": len(ca),
           "added": [_cycle(c, da, "after the move {names} would depend on each other in a cycle")
                     for c in added[:CYCLES_LISTED]],
           "removed": [_cycle(c, db, "{names} depend on each other in a cycle the move would break")
                       for c in removed[:CYCLES_LISTED]]}
    if changed:
        out["changed"] = [{**_cycle(c, da, "after the move {names} would depend on each other in one cycle "
                                          f"(today {'a cycle' if len(was) == 1 else 'cycles'} of "
                                          f"{', '.join(map(str, was))} file(s))"),
                           "change": "grows and shrinks" if grows and shrinks else "grows" if grows else "shrinks",
                           "was": was}
                          for c, was, grows, shrinks in changed[:CYCLES_LISTED]]
    if max(len(added), len(removed), len(changed)) > CYCLES_LISTED:
        out.update({"added_total": len(added), "removed_total": len(removed), "changed_total": len(changed),
                    "truncated": True})
    out["_grows"] = any(x[2] for x in changed)
    out["_shrinks"] = any(x[3] for x in changed)
    return out


# -- the command -------------------------------------------------------------------------------------

def run(g: Graph, specs: list[str], *, records=None, decisions_dir: str | None = None) -> dict:
    """Simulate the ``--move`` specs on ``g`` and compare the edge guards and the cycles before and after.
    ``records``: the decision records (default: those of the decisions folder). WhatIfError for a bad move."""
    from verinoda import decisions as dm

    repo = Path(g.root)
    files = sorted({f for _n, f in g.G.nodes(data="source_file") if f})
    mapping, rows, merges = plan(files, specs)
    mg = moved(g, mapping)
    recs = records if records is not None else dm.load_all(repo, decisions_dir)
    edge_recs, n_edge, n_other = _edge_records(recs)
    res: dict = {"view": "what-if", "moves": rows, "files_moved": len(mapping)}
    if merges:
        res["merges"] = merges[:CYCLES_LISTED]
    limits = ["simulated: no file was moved or edited; every line cited is where the code is now",
              "a move is assumed to update the code that names the moved files (imports, package names), so "
              "each edge stays as the index extracted it; a module the new path no longer resolves is not seen",
              "file-level only: moving a symbol between files is not simulated"]
    if n_edge:
        res["rules"] = {"edge_guards": n_edge, **_rules(repo, g, mg, edge_recs, decisions_dir)}
    else:
        res["rules"] = {"edge_guards": 0}
        limits.append("no accepted edge guard (no_edge, layers, allow_edges, public) in an enforced decision "
                      "record: no rule was re-checked (`verinoda decide record --guard ...`)")
    if n_other:
        limits.append(f"{n_other} other check(s) of the decision records (only_in, dependency, governs, "
                      "revisit_when) read files at their current paths: not re-checked")
    limits.append("findings are compared without the baseline; waivers apply to the sites where the code is now")
    cy = _cycles(g, mg)
    grows, shrinks = cy.pop("_grows"), cy.pop("_shrinks")
    res["cycles"] = cy
    ru = res["rules"]
    adds = bool(ru.get("added") or cy["added"] or grows or ru.get("unknown_new"))
    removes = bool(ru.get("removed") or cy["removed"] or shrinks)
    res["status"] = "adds" if adds else "removes" if removes else "no_change"
    res["limits"] = limits
    if adds:
        res["next_step"] = "read each added site and cycle before moving; `verinoda decide check` after the move " \
                           "checks the real code"
    return res


def render(res: dict) -> str:
    out = [f"what-if ({res['files_moved']} file(s) moved; nothing edited): {res['status']}"]
    for m in res["moves"]:
        out.append(f"  move {m['kind']} {m['from']} -> {m['to']} ({m['files']} file(s))")
    for m in res.get("merges") or []:
        out.append(f"  merge: {', '.join(m['files'])} -> {m['into']} (simulated as one file)")
    ru = res["rules"]
    if not ru.get("edge_guards"):
        out.append("  rules: no accepted edge guard to re-check")
    else:
        out.append(f"  rules: {ru['edge_guards']} edge guard(s); violations {ru['violations_before']} -> "
                   f"{ru['violations_after']}; {ru.get('added_total', len(ru['added']))} finding(s) added, "
                   f"{ru.get('removed_total', len(ru['removed']))} removed, {ru['unchanged']} unchanged"
                   + (f", {ru['not_rechecked']} not re-checked (their guard could not run)"
                      if ru.get("not_rechecked") else ""))
        for word, key in (("+", "added"), ("-", "removed")):
            for f in ru[key]:
                out.append(f"    {word} {f['level']} {f['decision']}/{f['guard']} {f.get('at')} "
                           f"[{f.get('status')}]: {f['why']}")
                if f.get("line"):
                    out.append(f"        {f['line']}")
        for u in ru.get("unknown") or []:
            out.append(f"    {'+ ' if u.get('new') else ''}unknown {u.get('decision')}/{u.get('guard')}: {u['why']}")
    cy = res["cycles"]
    out.append(f"  cycles: {cy['before']} -> {cy['after']}")
    for word, key in (("+", "added"), ("-", "removed"), ("~", "changed")):
        for c in cy.get(key) or []:
            out.append(f"    {word} [{c['status']}] {c['claim']}" + (f" ({c['change']})" if c.get("change") else ""))
            for d in c["dependencies"]:
                out.append(f"        {d['from']} -> {d['to']} ({d['references']} ref.) {', '.join(d['at'])}")
    for lim in res["limits"]:
        out.append(f"  limit: {lim}")
    if res.get("next_step"):
        out.append(f"  next: {res['next_step']}")
    return "\n".join(out)
