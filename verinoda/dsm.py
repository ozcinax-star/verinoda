"""Dependency structure matrix and an architecture model checked against the code.

``dsm``: the file-to-file dependencies of the cycles view (:func:`verinoda.architecture_map._file_deps`: calls,
imports, uses and inherits, standard-library imports apart) summed between groups of files - folders at one
depth, or the committed architecture tags (``[architecture.tags]``, D95). Row uses column. The groups are
ordered so that a group comes before the groups it uses (a topological order of the group graph; inside a
cycle of groups, the order that leaves the fewest references pointing back), so every mark below the diagonal
is a dependency against that order, and there are such marks only inside a cycle.

``model``: a C4 model - a Structurizr DSL file, or relations between tags in ``[architecture.model]`` - compared
with the same dependencies. Every model relation is ``matched`` (code of the one element uses code of the
other), ``model_only`` (no extracted dependency), ``not_checked`` (an end has no code mapped: a person, an
external system) or ``unknown`` (an end's globs match no indexed file); every dependency between two modelled
elements that no relation covers is listed as ``undeclared``. Graph edges are extractions, never
verification: each result is ``strong_inference`` at most, with the reference lines behind it.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath

import networkx as nx

from verinoda.index import Graph

SITES = 3                  # reference lines kept per cell, relation or undeclared pair
MAX_GROUPS = 30            # automatic folder depth: the deepest one with at most this many groups
EXACT_ORDER_MAX = 10       # up to this many groups in one cycle the order is searched exactly (2^n subsets)
OTHER = "(other)"          # files no tag matches, when grouping by tags
ROOT = "(root)"            # files at the top of the repository
DSL_PROPERTY = "verinoda.code"
EDGE_LIMITS = [
    "edges are what the index extracted: reflection, DI containers, string class loading, HTTP and message "
    "calls between services and build-time wiring are not seen",
    "graph edges are extractions, never verification: every count and mark is strong_inference at most",
]


# -- shared ----------------------------------------------------------------------------------------

def _deps(g: Graph) -> tuple[dict[tuple[str, str], dict], list[str]]:
    """The cycles view's file dependencies, and the limits naming what it set apart: standard-library imports the
    graph resolved to a project module, and edges between a detected copy of the project and the project (a name
    resolved into the wrong tree, which would join the two trees' groups into one cycle)."""
    from verinoda.architecture_map import ASIDE, _cycle_parts

    parts = _cycle_parts(g)
    limits = []
    if parts["stdlib"]:
        limits.append(f"{len(parts['stdlib'])} Python import(s) of the standard library the graph resolved to a "
                      "project module are left out, as in the cycles view")
    if parts["crossing"]:
        refs = sum(d["references"] for _e, d in parts["crossing"])
        limits.append(f"{refs} reference(s) between a {ASIDE} ({', '.join(parts['copy_roots'])}) and the project "
                      "are left out, as in the cycles view")
    return parts["deps"], limits


def _code_files(g: Graph) -> list[str]:
    from verinoda.index import PROSE_SUFFIXES

    return sorted({f for n in g.G.nodes if (f := g.file(n)) and not f.lower().endswith(PROSE_SUFFIXES)})


def _add(cell: dict, d: dict) -> None:
    cell["references"] += d["references"]
    cell["extracted"] += d["extracted"]
    cell["relations"].update(d["relations"])
    cell["sites"] |= d["sites"]


def _new_cell() -> dict:
    return {"references": 0, "extracted": 0, "relations": Counter(), "sites": set()}


def _cell_out(cell: dict) -> dict:
    sites = [at for _, at in sorted(cell["sites"])]   # EXTRACTED references first, then by place
    return {"references": cell["references"], "extracted": cell["extracted"],
            "relations": dict(sorted(cell["relations"].items())), "sites": sites[:SITES],
            "status": "strong_inference" if cell["extracted"] else "weak_inference"}


# -- grouping --------------------------------------------------------------------------------------

def _folder(f: str, depth: int) -> str:
    parts = PurePosixPath(f).parts[:-1]
    return "/".join(parts[:depth]) if parts else ROOT


def auto_depth(files: list[str], max_groups: int = MAX_GROUPS) -> int:
    """The deepest folder depth (1-8) whose groups number at most ``max_groups`` (1 when even that has more)."""
    best = 1
    for depth in range(1, 9):
        n = len({_folder(f, depth) for f in files})
        if n > max_groups:
            break
        best = depth
        if all(len(PurePosixPath(f).parts) - 1 <= depth for f in files):
            break   # deeper adds nothing
    return best


def _tag_groups(repo: Path, files: list[str]) -> tuple[dict[str, str], list[str], list[str], list[str]]:
    """``(file -> tag, tags in table order, limits, problems)``. A file two tags match counts in the first."""
    from verinoda.decisions import architecture_tags
    from verinoda.guards import glob_match

    tags, where, problems = architecture_tags(repo)
    if not tags:
        problems = problems or ["no [architecture.tags] in verinoda.toml or [tool.verinoda.architecture.tags] in "
                                "pyproject.toml: group by folder, or name the groups there"]
        return {}, [], [], problems
    of: dict[str, str] = {}
    twice = 0
    for f in files:
        hits = [t for t, globs in tags.items() if any(glob_match(f, p) for p in globs)]
        if hits:
            of[f] = hits[0]
            twice += len(hits) > 1
        else:
            of[f] = OTHER
    limits = [f"groups are the tags of {', '.join(where)}"]
    if twice:
        limits.append(f"{twice} file(s) match two tags or more: each counts in the first tag of the table")
    empty = [t for t in tags if t not in set(of.values())]
    if empty:
        problems = problems + [f"tag {t} matches no indexed code file" for t in empty]
    order = [t for t in tags if t not in empty] + ([OTHER] if OTHER in of.values() else [])
    return of, order, limits, problems


# -- ordering --------------------------------------------------------------------------------------

def _exact_order(nodes: list[str], w: dict[tuple[str, str], int]) -> list[str]:
    """The order of a small cycle of groups that leaves the fewest references pointing back (later -> earlier)."""
    n = len(nodes)
    idx = {x: i for i, x in enumerate(nodes)}
    ins: list[list[tuple[int, int]]] = [[] for _ in nodes]
    for (a, b), c in w.items():
        ins[idx[b]].append((idx[a], c))
    best: list[tuple[int, tuple] | None] = [None] * (1 << n)
    best[0] = (0, ())
    for mask in range(1 << n):
        cur = best[mask]
        if cur is None:
            continue
        for v in range(n):
            if mask >> v & 1:
                continue
            # v goes next: a group placed after it that uses it points back
            back = sum(c for a, c in ins[v] if not mask >> a & 1 and a != v)
            cand = (cur[0] + back, cur[1] + (nodes[v],))
            nm = mask | 1 << v
            if best[nm] is None or cand < best[nm]:
                best[nm] = cand
    return list(best[(1 << n) - 1][1])


def _greedy_order(nodes: list[str], w: dict[tuple[str, str], int]) -> list[str]:
    """Eades-Lin-Smyth: groups nothing is left to use go last, groups nothing left uses first, else the group
    with the most outgoing minus incoming references next."""
    out_w: dict[str, Counter] = defaultdict(Counter)
    in_w: dict[str, Counter] = defaultdict(Counter)
    for (a, b), c in w.items():
        out_w[a][b] += c
        in_w[b][a] += c
    left = set(nodes)
    head: list[str] = []
    tail: list[str] = []
    while left:
        moved = True
        while moved:
            moved = False
            for x in sorted(left):
                if not any(y in left for y in out_w[x]):
                    tail.append(x)
                    left.discard(x)
                    moved = True
                elif not any(y in left for y in in_w[x]):
                    head.append(x)
                    left.discard(x)
                    moved = True
        if left:
            x = max(sorted(left), key=lambda z: sum(c for y, c in out_w[z].items() if y in left)
                    - sum(c for y, c in in_w[z].items() if y in left))
            head.append(x)
            left.discard(x)
    return head + tail[::-1]


def order_groups(groups: list[str], w: dict[tuple[str, str], int]) -> tuple[list[str], list[list[str]]]:
    """``(order, cycles)``: a group before the groups it uses; each cycle of groups ordered to leave the fewest
    references pointing back. Ties by the given order of ``groups``."""
    gg = nx.DiGraph()
    gg.add_nodes_from(groups)
    gg.add_edges_from(e for e in w if e[0] != e[1])
    rank = {x: i for i, x in enumerate(groups)}
    comps = list(nx.strongly_connected_components(gg))
    comp_of = {x: k for k, c in enumerate(comps) for x in c}
    cg = nx.condensation(gg, scc=comps)
    order: list[str] = []
    cycles: list[list[str]] = []
    for k in nx.lexicographical_topological_sort(cg, key=lambda k: min(rank[x] for x in comps[k])):
        members = sorted(comps[k], key=rank.__getitem__)
        if len(members) == 1:
            order += members
            continue
        inner = {e: c for e, c in w.items() if comp_of.get(e[0]) == k == comp_of.get(e[1]) and e[0] != e[1]}
        part = _exact_order(members, inner) if len(members) <= EXACT_ORDER_MAX else _greedy_order(members, inner)
        order += part
        cycles.append(part)
    return order, cycles


# -- the matrix ------------------------------------------------------------------------------------

def dsm(g: Graph, *, by: str = "folder", depth: int | None = None) -> dict:
    """The dependency structure matrix between groups of files (see the module docstring)."""
    files = _code_files(g)
    deps, dep_limits = _deps(g)
    limits = list(EDGE_LIMITS) + dep_limits
    problems: list[str] = []
    if by == "tag":
        of, groups, lim, problems = _tag_groups(Path(g.root), files)
        limits += lim
        basis = "architecture tags"
    elif by == "folder":
        if depth is not None and depth < 1:
            raise ValueError("--depth must be at least 1")
        d = depth or auto_depth(files)
        of = {f: _folder(f, d) for f in files}
        groups = sorted(set(of.values()))
        basis = f"folders at depth {d}" + ("" if depth else f", the deepest with at most {MAX_GROUPS} groups")
    else:
        raise ValueError(f"group by folder or tag, not {by!r}")
    cells: dict[tuple[str, str], dict] = defaultdict(_new_cell)
    inside = Counter()
    for (fu, fv), d in deps.items():
        a, b = of.get(fu), of.get(fv)
        if a is None or b is None:
            continue
        if a == b:
            inside[a] += d["references"]
            continue
        _add(cells[(a, b)], d)
    order, cycles = order_groups(groups, {e: c["references"] for e, c in cells.items()})
    pos = {x: i for i, x in enumerate(order)}
    size = Counter(of.values())
    rows = []
    against = 0
    for (a, b), c in sorted(cells.items(), key=lambda kv: (pos[kv[0][0]], pos[kv[0][1]])):
        row = {"from": a, "to": b, **_cell_out(c), "against_order": pos[a] > pos[b]}
        against += row["against_order"] * c["references"]
        rows.append(row)
    return {
        "view": "dsm",
        "coverage": {"method": "file-to-file calls/imports/uses/inherits of project_index (as the cycles view) "
                               f"summed between {basis}; row uses column",
                     "limits": limits},
        "grouped_by": by, "basis": basis,
        "groups": [{"index": i + 1, "name": x, "files": size[x], "inside_references": inside[x]}
                   for i, x in enumerate(order)],
        "cells": rows,
        "cycles": cycles,
        "against_order_references": against,
        "problems": problems,
    }


# -- the model -------------------------------------------------------------------------------------

_ELEMENT = re.compile(r'^(?:([A-Za-z_][\w.-]*)\s*=\s*)?(person|softwareSystem|container|component|element)\b'
                      r'\s*(?:"([^"]*)")?', re.I)
_RELATION = re.compile(r'^(?:([A-Za-z_][\w.-]*|this)\s*)?->\s*([A-Za-z_][\w.-]*)(?:\s+"([^"]*)")?')
_SKIP_BLOCKS = {"views", "styles", "deploymentenvironment", "configuration", "branding", "terminology",
                "deploymentnode", "infrastructurenode", "softwaresysteminstance", "containerinstance",
                "dynamic", "filtered"}


def _dsl_lines(text: str) -> list[tuple[int, str]]:
    """The DSL's lines with comments blanked (``//`` and ``/* */`` outside strings, ``#`` at a line's start),
    numbered from 1. A glob such as ``"src/**"`` is a string: its ``/*`` opens no comment."""
    keep: list[str] = []
    i, n, quoted, block = 0, len(text), False, False
    while i < n:
        c = text[i]
        if block:
            if text.startswith("*/", i):
                block, i = False, i + 2
                continue
            keep.append("\n" if c == "\n" else "")
        elif quoted:
            keep.append(c)
            if c == "\\" and i + 1 < n:
                keep.append(text[i + 1])
                i += 1
            elif c in '"\n':
                quoted = False
        elif c == '"':
            quoted = True
            keep.append(c)
        elif text.startswith("/*", i):
            block, i = True, i + 2
            continue
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
            continue
        else:
            keep.append(c)
        i += 1
    out = []
    for no, line in enumerate("".join(keep).splitlines(), 1):
        s = line.strip()
        if not s.startswith("#"):
            out.append((no, s))
    return out


def parse_dsl(text: str, src: str) -> tuple[list[dict], list[dict], list[str]]:
    """``(elements, relations, problems)`` of a Structurizr DSL text (the model's elements and relationships;
    views, styles and deployment are skipped). An element maps to code through its property ``verinoda.code``
    (globs, comma-separated). Element ids are hierarchical under ``!identifiers hierarchical``."""
    elements: list[dict] = []
    relations: list[dict] = []
    problems: list[str] = []
    hierarchical = bool(re.search(r"^\s*!identifiers\s+hierarchical\b", text, re.M | re.I))
    stack: list[tuple[str, dict | None]] = []   # (block kind, element or None)
    pending: list[tuple[int, str | None, str, str | None]] = []
    anon = 0

    def current() -> dict | None:
        for kind, el in reversed(stack):
            if el is not None:
                return el
        return None

    for no, s in _dsl_lines(text):
        if not s:
            continue
        opens = s.endswith("{")
        body = s[:-1].strip() if opens else s
        if body == "}" or s == "}":
            if stack:
                stack.pop()
            continue
        if any(k == "skip" for k, _ in stack):
            if opens:
                stack.append(("skip", None))
            continue
        if any(k == "properties" for k, _ in stack):
            m = re.match(r'^"([^"]+)"\s+"([^"]*)"$|^(\S+)\s+"([^"]*)"$|^(\S+)\s+(\S+)$', body)
            el = current()
            if m and el is not None:
                key = m.group(1) or m.group(3) or m.group(5)
                val = m.group(2) if m.group(1) else m.group(4) if m.group(3) else m.group(6)
                if key == DSL_PROPERTY:
                    globs = [x.strip().replace("\\", "/") for x in (val or "").split(",") if x.strip()]
                    el["globs"] = globs
                    el["mapped_by"] = f"{src}:{no} {DSL_PROPERTY}"
            if opens:
                stack.append(("skip", None))
            continue
        word = body.split(None, 1)[0].lower() if body else ""
        if word in _SKIP_BLOCKS:
            if opens:
                stack.append(("skip", None))
            continue
        if word == "properties":
            stack.append(("properties", None) if opens else ("skip", None))
            continue
        m = _ELEMENT.match(body)
        if m:
            parent = current()
            local = m.group(1)
            if not local:
                anon += 1
                local = f"_{anon}"
            full = f"{parent['id']}.{local}" if hierarchical and parent and m.group(1) else local
            el = {"id": full, "name": m.group(3) or local, "kind": m.group(2), "parent": parent["id"] if parent else None,
                  "at": f"{src}:{no}", "globs": [], "mapped_by": None, "named": bool(m.group(1))}
            elements.append(el)
            if opens:
                stack.append(("element", el))
            continue
        m = _RELATION.match(body)
        if m:
            here = current()
            a = m.group(1)
            if a in (None, "this"):
                a = here["id"] if here else None
            if a is None:
                problems.append(f"{src}:{no}: a relationship with no source outside an element")
            else:
                pending.append((no, a, m.group(2), m.group(3)))
            if opens:
                stack.append(("skip", None))
            continue
        if opens:   # model, workspace, group "...", a block not read: its elements are still elements
            stack.append(("group" if word in ("model", "workspace", "group", "enterprise") else "skip", None))
    ids = {e["id"] for e in elements if e["named"]}

    def resolve(x: str) -> str | None:
        if x in ids:
            return x
        hits = [i for i in ids if i.endswith("." + x)]
        return hits[0] if len(hits) == 1 else None

    for no, a, b, desc in pending:
        ra, rb = resolve(a), resolve(b)
        if ra is None or rb is None:
            problems.append(f"{src}:{no}: {a if ra is None else b} is not an element of the model")
            continue
        relations.append({"from": ra, "to": rb, "description": desc or "", "at": f"{src}:{no}"})
    return elements, relations, problems


def load_model(repo: Path, model: str | None = None) -> tuple[list[dict], list[dict], list[str], list[str]]:
    """``(elements, relations, sources, problems)``: ``model`` (a DSL file) when given, else the committed
    ``[architecture.model]`` (``dsl = "path"`` and/or ``relations = ["a -> b", ...]`` between tags). An element
    without ``verinoda.code`` maps to the tag of its id (or of its last id segment) when there is one."""
    from verinoda.decisions import _committed_tables, _inside, architecture_tags

    repo = Path(repo)
    elements: list[dict] = []
    relations: list[dict] = []
    sources: list[str] = []
    problems: list[str] = []
    dsl_paths: list[tuple[str, str]] = []
    text_rel: list[tuple[str, str]] = []
    if model:
        dsl_paths.append((model, "--model"))
    else:
        for data, where, problem in _committed_tables(repo, "architecture", "model"):
            if problem:
                problems.append(problem)
                continue
            if data is None:
                continue
            if not isinstance(data, dict):
                problems.append(f"{where} is not a table")
                continue
            if isinstance(data.get("dsl"), str):
                dsl_paths.append((data["dsl"], f"{where} dsl"))
            rel = data.get("relations")
            if rel is not None:
                if not isinstance(rel, list) or not all(isinstance(x, str) for x in rel):
                    problems.append(f"{where} relations: not a list of \"a -> b\" strings")
                else:
                    text_rel += [(x, where) for x in rel]
            break   # verinoda.toml wins over pyproject.toml, as for the tags
    for path, where in dsl_paths:
        p = Path(path) if Path(path).is_absolute() else repo / path
        rel_name = path.replace("\\", "/")
        if not Path(path).is_absolute() and not _inside(path):
            problems.append(f"{where}: {path} is outside the repository")
            continue
        try:
            text = p.read_bytes().decode("utf-8-sig", errors="replace")
        except OSError as exc:
            problems.append(f"{where}: {path} cannot be read ({exc.strerror or exc})")
            continue
        els, rels, probs = parse_dsl(text, rel_name)
        elements += els
        relations += rels
        problems += probs
        sources.append(rel_name)
    tags, _where, tag_problems = architecture_tags(repo)
    problems += tag_problems
    known = {e["id"] for e in elements}
    for line, where in text_rel:
        m = re.match(r"^\s*([\w.-]+)\s*->\s*([\w.-]+)\s*$", line)
        if not m:
            problems.append(f"{where} relations: {line!r} is not \"a -> b\"")
            continue
        for x in m.groups():
            if x not in known:
                elements.append({"id": x, "name": x, "kind": "tag", "parent": None, "at": where, "globs": [],
                                 "mapped_by": None, "named": True})
                known.add(x)
        relations.append({"from": m.group(1), "to": m.group(2), "description": "", "at": where})
    if text_rel:
        sources.append(text_rel[0][1])
    for e in elements:
        if e["globs"]:
            continue
        for name in (e["id"], e["id"].rpartition(".")[2]):
            if name in tags:
                e["globs"], e["mapped_by"] = list(tags[name]), f"tag {name}"
                break
    return elements, relations, sources, problems


def model_check(g: Graph, *, model: str | None = None) -> dict:
    """The model's relations against the code's dependencies (see the module docstring)."""
    from verinoda.guards import glob_match

    repo = Path(g.root)
    elements, relations, sources, problems = load_model(repo, model)
    base = {"view": "model", "coverage": {"method": "model relations compared with file-to-file calls/imports/"
                                                    "uses/inherits of project_index (as the cycles view)",
                                          "limits": list(EDGE_LIMITS)}}
    if not elements:
        return {**base, "status": "no_model", "sources": sources, "problems": problems,
                "next_step": "name a Structurizr DSL file with --model PATH (elements map to code with the "
                             f"property \"{DSL_PROPERTY}\" \"glob,glob\" or a tag of the same name), or commit "
                             "[architecture.model] relations = [\"ui -> core\"] between [architecture.tags] in "
                             "verinoda.toml",
                "elements": [], "relations": [], "undeclared": [], "summary": {}}
    by_id = {e["id"]: e for e in elements}
    kids: dict[str, list[str]] = defaultdict(list)
    for e in elements:
        if e["parent"]:
            kids[e["parent"]].append(e["id"])

    def depth(x: str) -> int:
        n = 0
        while by_id[x]["parent"]:
            x, n = by_id[x]["parent"], n + 1
        return n

    def ancestors(x: str) -> list[str]:
        out = [x]
        while by_id[x]["parent"]:
            x = by_id[x]["parent"]
            out.append(x)
        return out

    files = _code_files(g)
    inner: dict[str, str] = {}
    overlap = 0
    for f in files:
        hits = [e["id"] for e in elements if e["globs"] and any(glob_match(f, p) for p in e["globs"])]
        if not hits:
            continue
        top = max(depth(h) for h in hits)
        deepest = [h for h in hits if depth(h) == top]
        overlap += len(deepest) > 1
        inner[f] = deepest[0]
    under: dict[str, set[str]] = defaultdict(set)   # element -> files of it and of the elements inside it
    for f, e in inner.items():
        for a in ancestors(e):
            under[a].add(f)

    def mapped(x: str) -> bool:
        return bool(by_id[x]["globs"]) or any(mapped(k) for k in kids[x])

    deps, dep_limits = _deps(g)
    rel_cells = [_new_cell() for _ in relations]
    undeclared: dict[tuple[str, str], dict] = defaultdict(_new_cell)
    outside = 0
    for (fu, fv), d in deps.items():
        a, b = inner.get(fu), inner.get(fv)
        if a is None or b is None:
            outside += a is None or b is None
            continue
        anc_a, anc_b = ancestors(a), ancestors(b)
        if a == b or a in anc_b or b in anc_a:
            continue   # inside one element
        covered = False
        for i, r in enumerate(relations):
            if r["from"] in anc_a and r["to"] in anc_b:
                _add(rel_cells[i], d)
                covered = True
        if not covered:
            _add(undeclared[(a, b)], d)
    rel_out = []
    count = Counter()
    for r, c in zip(relations, rel_cells):
        row = dict(r)
        ends = (r["from"], r["to"])
        unmapped = [x for x in ends if not mapped(x)]
        empty = [x for x in ends if mapped(x) and not under[x]]
        if unmapped:
            row.update(result="not_checked", status="unknown",
                       why=f"{', '.join(unmapped)} has no code mapped (no {DSL_PROPERTY} and no tag of its name)")
        elif empty:
            row.update(result="unknown", status="unknown",
                       why=f"the globs of {', '.join(empty)} match no indexed code file",
                       next_step="fix the globs, or run `verinoda update` if the files are new")
        elif c["references"]:
            row.update(result="matched", **_cell_out(c),
                       why=f"code of {r['from']} uses code of {r['to']}")
        else:
            rev = sum(cc["references"] for (a, b), cc in undeclared.items()
                      if r["to"] in ancestors(a) and r["from"] in ancestors(b))
            row.update(result="model_only", status="strong_inference", references=0,
                       why=f"no extracted dependency from the code of {r['from']} ({len(under[r['from']])} file(s)) "
                           f"to the code of {r['to']} ({len(under[r['to']])} file(s))"
                           + (f"; {rev} reference(s) go the other way (undeclared)" if rev else ""))
        count[row["result"]] += 1
        rel_out.append(row)
    und = []
    for (a, b), c in sorted(undeclared.items(), key=lambda kv: (-kv[1]["references"], kv[0])):
        und.append({"from": a, "to": b, **_cell_out(c),
                    "why": f"code of {a} uses code of {b}; no model relation covers it"})
    limits = base["coverage"]["limits"]
    if overlap:
        limits.append(f"{overlap} file(s) match two elements at the same depth: each counts in the first defined")
    limits += dep_limits
    unmapped_els = [e["id"] for e in elements if not mapped(e["id"])]
    status = "unknown" if count["unknown"] else ("differs" if count["model_only"] or und else "consistent")
    return {
        **base, "status": status, "sources": sources, "problems": problems,
        "elements": [{"id": e["id"], "name": e["name"], "kind": e["kind"], "parent": e["parent"], "at": e["at"],
                      "globs": e["globs"], "mapped_by": e["mapped_by"], "files": len(under.get(e["id"], ()))}
                     for e in elements],
        "relations": rel_out, "undeclared": und,
        "summary": {"relations": len(relations), "matched": count["matched"], "model_only": count["model_only"],
                    "not_checked": count["not_checked"], "unknown": count["unknown"], "undeclared": len(und),
                    "unmapped_elements": unmapped_els, "files_in_model": len(inner),
                    "dependencies_with_an_end_outside": outside},
    }


# -- text ------------------------------------------------------------------------------------------

def render_dsm(v: dict, cap: int) -> list[str]:
    groups = v.get("groups") or []
    if not groups:
        return [f"   {p}" for p in v.get("problems") or ["no groups"]]
    shown = groups[:max(cap, 1)]
    cell = {(c["from"], c["to"]): c for c in v["cells"]}
    width = max(len(g["name"]) for g in shown)
    width = min(width, 40)
    out = [f"   {len(groups)} groups ({v['basis']}); row uses column; a group comes before what it uses, so a "
           "mark below the diagonal (*) points back inside a cycle"]
    out.append("   " + " " * (width + 5) + "".join(f"{g['index']:>5}" for g in shown))
    for g in shown:
        marks = []
        for h in shown:
            if h is g:
                marks.append("    -")
                continue
            c = cell.get((g["name"], h["name"]))
            if not c:
                marks.append("    .")
                continue
            n = c["references"]
            text = (str(n) if n < 1000 else "999+") + ("*" if c["against_order"] else "")
            marks.append(f"{text:>5}")
        name = g["name"] if len(g["name"]) <= width else "..." + g["name"][-(width - 3):]
        out.append(f"   {g['index']:>3} {name:<{width}} " + "".join(marks))
    if len(groups) > len(shown):
        out.append(f"   ... {len(groups) - len(shown)} more groups (--max-lines, or --json)")
    for cyc in v.get("cycles") or []:
        out.append(f"   cycle of {len(cyc)} groups: {', '.join(cyc)}")
    if v.get("against_order_references"):
        out.append(f"   {v['against_order_references']} reference(s) point back against the order")
    back = [c for c in v["cells"] if c["against_order"]]
    for c in back[:cap]:
        out.append(f"   back: {c['from']} -> {c['to']}: {c['references']} reference(s), e.g. "
                   f"{', '.join(c['sites']) or 'no cited line'}")
    out += [f"   problem: {p}" for p in v.get("problems") or []]
    return out


def render_model(v: dict, cap: int) -> list[str]:
    if v.get("status") == "no_model":
        return [f"   no model: {v['next_step']}"] + [f"   problem: {p}" for p in v.get("problems") or []]
    s = v["summary"]
    out = [f"   {v['status']}: {s['relations']} relation(s) in {', '.join(v['sources']) or 'the model'}: "
           f"{s['matched']} matched, {s['model_only']} without a code edge, {s['not_checked']} not checked, "
           f"{s['unknown']} unknown; {s['undeclared']} undeclared dependency pair(s)"]
    for r in v["relations"]:
        if r["result"] == "matched":
            continue
        out.append(f"   {r['result']}: {r['from']} -> {r['to']}  ({r['at']}) {r['why']}")
    for u in v["undeclared"][:cap]:
        out.append(f"   undeclared: {u['from']} -> {u['to']}: {u['references']} reference(s), e.g. "
                   f"{', '.join(u['sites']) or 'no cited line'}")
    if len(v["undeclared"]) > cap:
        out.append(f"   ... {len(v['undeclared']) - cap} more undeclared pairs in --json")
    out += [f"   problem: {p}" for p in v.get("problems") or []]
    return out
