"""Mermaid diagrams of the graph and a wiki outline over them.

Three diagrams, each a Mermaid text with the claims it draws:

- **architecture** (``flowchart LR``): the parts of the project (a wiki page's files, or by default the
  first two folders of a path, as the graph view colours them) and the call, import, use and inheritance
  edges between them, counted;
- **flow** (``flowchart TD``): the call paths of ``trace`` between two symbols, or what one symbol calls,
  a few calls deep;
- **sequence** (``sequenceDiagram``): the first call path between two symbols as messages between the
  classes (or files) that own each method.

Every arrow is a claim with ``file:line`` evidence (the edge's own line). An edge is an extraction, never a
verification: an arrow whose edges include one the extractor read from the code is ``strong_inference``, one
drawn only from edges a second pass derived (``INFERRED``) is ``weak_inference`` and dashed.

The **outline** is a page tree: an overview page with the architecture diagram, then a page per part, each
with a diagram of its files and the parts they reach. A repository file, :data:`STEERING_FILE`, can steer it:
its ``pages`` name the titles, what each page is for, its parent, the files it covers (``paths``: a folder, a
file or a glob) and the call flows it should draw (``flows``: ``["from", "to"]`` pairs, each giving a flow and
a sequence diagram). What the file names that the index does not have (a path matching no file, a flow whose
ends do not resolve, a parent no page has) is listed under ``problems``, never guessed.
"""

from __future__ import annotations

import fnmatch
import json
import re
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath

STEERING_FILE = ".verinoda-wiki.json"
USE_RELATIONS = frozenset({"calls", "imports", "imports_from", "uses", "inherits", "implements"})
MAX_NODES = 30          # boxes of one diagram (the best connected; the diagram says how many were left out)
MAX_EDGES = 60          # arrows of one diagram
MAX_EVIDENCE = 3        # edge lines cited per arrow
MAX_PAGE_FILES = 60     # files a page lists (it says how many it has)
FLOW_DEPTH = 2          # calls deep, for the flow of one symbol
KINDS = ("architecture", "flow", "sequence")
_ESCAPE = str.maketrans({'"': "#quot;", "#": "#35;", ";": "#59;", "<": "#lt;", ">": "#gt;", "\n": " ",
                         "\r": " ", "|": "#124;", "`": "'"})


def _text(s: str, cap: int = 80) -> str:
    """``s`` as Mermaid label text: the characters Mermaid reads as syntax written as entities."""
    s = " ".join(str(s).split())
    return (s if len(s) <= cap else s[:cap - 1] + "…").translate(_ESCAPE)


def _at(d: dict) -> str | None:
    loc = str(d.get("source_location") or "")
    f = d.get("source_file")
    if f and loc.startswith("L") and loc[1:].isdigit():
        return f"{f}:{loc[1:]}"
    return f or None


def _status(extracted: bool) -> str:
    return "strong_inference" if extracted else "weak_inference"


def _files(g) -> list[str]:
    return sorted({d["source_file"] for _, d in g.G.nodes(data=True) if d.get("source_file")})


def area_of(file: str) -> str:
    """The default part of the project a file is in: its first two folders (``(root)`` at the top)."""
    parts = PurePosixPath(file).parts[:-1]
    return "/".join(parts[:2]) or "(root)"


def _coverage(method: str) -> dict:
    return {"method": method,
            "limits": ["graph edges are extractions from the code, not verification",
                       "dynamic dispatch, reflection, dependency injection and callbacks may be missing",
                       f"at most {MAX_NODES} boxes and {MAX_EDGES} arrows per diagram (truncated says so)"]}


# -- architecture -----------------------------------------------------------------------------------

def _file_edges(g) -> dict[tuple[str, str], dict]:
    """The edges between two files, per pair of files (read once per graph: every page reuses them)."""
    cache = g.__dict__.setdefault("_diagram_cache", {})
    if "files" in cache:
        return cache["files"]
    agg: dict[tuple[str, str], dict] = {}
    for u, v, d in g.edges(USE_RELATIONS):
        fu, fv = g.file(u), g.file(v)
        if not fu or not fv or fu == fv:
            continue
        row = agg.setdefault((fu, fv), {"count": 0, "extracted": 0, "relations": Counter(), "at": [],
                                        "inferred_at": []})
        row["count"] += 1
        row["relations"][d.get("relation")] += 1
        at = _at(d)
        if d.get("confidence") == "EXTRACTED":
            row["extracted"] += 1
            if at and at not in row["at"] and len(row["at"]) < MAX_EVIDENCE:
                row["at"].append(at)
        elif at and at not in row["inferred_at"] and len(row["inferred_at"]) < MAX_EVIDENCE:
            row["inferred_at"].append(at)
    cache["files"] = agg
    return agg


def _aggregate(g, key_of) -> dict[tuple[str, str], dict]:
    """Edges between the keys files map to (``key_of(file)``: a key, or None to leave the file out); an
    arrow cites the lines of extracted edges first."""
    agg: dict[tuple[str, str], dict] = {}
    for (fu, fv), r in _file_edges(g).items():
        a, b = key_of(fu), key_of(fv)
        if a is None or b is None or a == b:
            continue
        row = agg.setdefault((a, b), {"count": 0, "extracted": 0, "relations": Counter(), "at": [],
                                      "inferred_at": []})
        row["count"] += r["count"]
        row["extracted"] += r["extracted"]
        row["relations"].update(r["relations"])
        for key in ("at", "inferred_at"):
            for x in r[key]:
                if len(row[key]) < MAX_EVIDENCE and x not in row[key]:
                    row[key].append(x)
    for row in agg.values():
        row["at"] = (row["at"] + [x for x in row.pop("inferred_at") if x not in row["at"]])[:MAX_EVIDENCE]
    return agg


def _box_diagram(title: str, agg: dict, labels: dict[str, str], names: dict[str, str] | None = None,
                 rounded: set[str] = frozenset()) -> dict:
    """A flowchart of boxes (``rounded``: drawn with round ends) and counted arrows, the best connected
    boxes first. A claim names a box by ``names`` (else by its label)."""
    names = names or labels
    degree: Counter = Counter()
    for (a, b), row in agg.items():
        degree[a] += row["count"]
        degree[b] += row["count"]
    shown = set(sorted(labels, key=lambda k: (-degree[k], k))[:MAX_NODES])
    rows = sorted(((a, b, r) for (a, b), r in agg.items() if a in shown and b in shown),
                  key=lambda x: (-x[2]["count"], x[0], x[1]))
    dropped = len(rows) - MAX_EDGES
    rows = rows[:MAX_EDGES]
    ids = {k: f"n{i}" for i, k in enumerate(sorted(shown))}
    lines = ["flowchart LR"]
    for k in sorted(shown):
        left, right = ("([", "])") if k in rounded else ("[", "]")
        lines.append(f'  {ids[k]}{left}"{_text(labels[k])}"{right}')
    claims = []
    for a, b, r in rows:
        lines.append(f"  {ids[a]} {'-->' if r['extracted'] else '-.->'}|{r['count']}| {ids[b]}")
        rel = ", ".join(f"{n} {k}" for k, n in r["relations"].most_common())
        claims.append({"text": f"{names.get(a, a)} uses {names.get(b, b)} ({rel})", "from": a, "to": b,
                       "count": r["count"], "status": _status(bool(r["extracted"])),
                       "evidence": r["at"][:MAX_EVIDENCE]})
    out = {"kind": "architecture", "title": title, "mermaid": "\n".join(lines), "claims": claims,
           "coverage": _coverage("call/import/use/inheritance edges of the graph between files, counted by the "
                                 "box each file is in")}
    left_out = len(labels) - len(shown)
    if left_out > 0 or dropped > 0:
        out["truncated"] = True
        out["left_out"] = {"boxes": max(left_out, 0), "arrows": max(dropped, 0)}
    return out


def architecture(g, pages: list[dict] | None = None) -> dict:
    """The parts of the project and the edges between them: ``pages``' files (a file in several pages
    counts for the first), else the default parts (:func:`area_of`)."""
    if pages:
        owner: dict[str, str] = {}
        for p in pages:
            for f in p["_files"]:
                owner.setdefault(f, p["id"])
        labels = {p["id"]: p["title"] for p in pages if p["_files"]}
        agg = _aggregate(g, owner.get)
    else:
        labels = {a: a for a in {area_of(f) for f in _files(g)}}
        agg = _aggregate(g, area_of)
    return _box_diagram("Architecture", agg, labels)


def page_diagram(g, page: dict, pages: list[dict]) -> dict:
    """A page's files and the other pages they reach (or are reached from)."""
    mine = set(page["_files"])
    owner: dict[str, str] = {}
    for p in pages:
        if p["id"] != page["id"]:
            for f in p["_files"]:
                if f not in mine:
                    owner.setdefault(f, "page:" + p["id"])
    titles = {"page:" + p["id"]: p["title"] for p in pages}

    def key_of(f: str) -> str | None:
        return f if f in mine else owner.get(f)

    agg = _aggregate(g, key_of)
    agg = {k: r for k, r in agg.items() if k[0] in mine or k[1] in mine}
    labels = {f: PurePosixPath(f).name for f in mine}
    others = {k for ab in agg for k in ab if k not in mine}
    labels.update({k: titles[k] for k in others})
    # a file's box shows its name, its claim the whole path; another page is a rounded box
    out = _box_diagram(f"{page['title']}: files", agg, labels, {**titles, **{f: f for f in mine}}, others)
    out["kind"] = "files"
    return out


# -- call flow and sequence -------------------------------------------------------------------------

def _owner(g, nid: str) -> str:
    """The class that owns a method, else the file the symbol is in."""
    for u, _d in g.in_edges(nid, {"method"}):
        if not g.is_file_node(u):
            return g.label(u)
    f = g.file(nid)
    return PurePosixPath(f).name if f else g.label(nid)


def _name(g, nid: str) -> str:
    """``Owner.method()`` for a method, else its label."""
    label = g.label(nid)
    if label.startswith("."):
        return _owner(g, nid) + label
    return label


def _node_at(g, nid: str) -> str:
    f, ln = g.file(nid), g.line(nid)
    return f"{f}:{ln}" if f and ln else (f or nid)


def _unresolved(kind: str, res: dict) -> dict:
    out = {"kind": kind, "status": res.get("status"), "mermaid": None, "claims": []}
    for k in ("hints", "next_step", "not_found", "not_indexed", "ambiguous", "not_a_symbol", "undirected_hint"):
        if res.get(k):
            out[k] = res[k]
    return out


def _trace(g, source: str, target: str, mode: str, stale) -> dict:
    from verinoda import retrieval

    return retrieval.trace(g, source, target, mode=mode, stale=stale)


def _callees(g, source: str, stale) -> dict:
    """What ``source`` calls, :data:`FLOW_DEPTH` calls deep, as the hops of a trace."""
    from verinoda import naming

    r = naming.resolve(g, source, stale=stale)
    if not r.node:
        out = {"status": "unresolved" if r.status != naming.AMBIGUOUS else "ambiguous",
               "hints": {"source": r.rows(g, 5)} if r.status in (naming.AMBIGUOUS, naming.NOT_FOUND) else {},
               "next_step": "pass a node id, 'path/file.py::symbol' or 'Class.method' (see hints)"}
        if r.note and r.status in (naming.NOT_FOUND, naming.NOT_INDEXED, naming.AMBIGUOUS, naming.NOT_A_SYMBOL):
            out[r.status] = {"source": r.note}
        if r.status == naming.NOT_INDEXED:
            out["next_step"] = "run `verinoda update` so the index describes the changed file(s), then try again"
        return out
    hops, seen, frontier = [], {r.node}, [r.node]
    for _ in range(FLOW_DEPTH):
        nxt = []
        for u in frontier:
            for v, d in sorted(g.out_edges(u, {"calls"}), key=lambda x: (_at(x[1]) or "", x[0])):
                if len(hops) >= MAX_EDGES:
                    break
                if not g.file(v):
                    continue  # an external callee: no line to cite for it
                hops.append({"from_id": u, "to_id": v, "relation": "calls", "confidence": d.get("confidence"),
                             "at": _at(d)})
                if v not in seen:
                    seen.add(v)
                    nxt.append(v)
        frontier = nxt
    out = {"status": "found", "paths": [hops], "resolved": {"source": {"id": r.node}}}
    if r.status == naming.SIMILAR and r.note:
        out["fuzzy"] = {"source": r.note}
    return out


def flow(g, source: str, target: str | None = None, *, mode: str = "flow", stale=()) -> dict:
    """The call paths from ``source`` to ``target`` (``trace``), or what ``source`` calls."""
    res = _trace(g, source, target, mode, stale) if target else _callees(g, source, stale)
    if res.get("status") != "found":
        return _unresolved("flow", res)
    nodes: dict[str, str] = {}
    edges: dict[tuple[str, str], dict] = {}
    for path in res["paths"]:
        for h in path:
            for n in (h["from_id"], h["to_id"]):
                nodes.setdefault(n, f"n{len(nodes)}")
            e = edges.setdefault((h["from_id"], h["to_id"]), {"relation": h.get("relation"), "extracted": False,
                                                              "at": h.get("at")})
            if h.get("confidence") == "EXTRACTED":
                e["extracted"], e["at"] = True, h.get("at") or e["at"]
    truncated = len(nodes) > MAX_NODES
    keep = set(list(nodes)[:MAX_NODES])
    lines = ["flowchart TD"]
    for n, i in nodes.items():
        if n in keep:
            lines.append(f'  {i}["{_text(_name(g, n))}<br/><small>{_text(_node_at(g, n))}</small>"]')
    claims = []
    for (a, b), e in edges.items():
        if a not in keep or b not in keep:
            continue
        rel = e["relation"] or "calls"
        lines.append(f"  {nodes[a]} {'-->' if e['extracted'] else '-.->'}|{_text(rel, 20)}| {nodes[b]}")
        claims.append({"text": f"{_name(g, a)} {rel} {_name(g, b)}", "status": _status(e["extracted"]),
                       "evidence": [e["at"]] if e["at"] else []})
    title = f"{_name(g, res['resolved']['source']['id'])} -> {_name(g, res['resolved']['target']['id'])}" \
        if target else f"what {_name(g, res['resolved']['source']['id'])} calls"
    out = {"kind": "flow", "status": "found", "title": title, "mermaid": "\n".join(lines), "claims": claims,
           "coverage": _coverage("the call paths of `trace` (mode " + mode + ")" if target
                                 else f"the calls edges out of the symbol, {FLOW_DEPTH} deep")}
    for k in ("note", "fuzzy", "resolution_notes"):  # a name resolved by similarity says so
        if res.get(k):
            out[k] = res[k]
    if truncated:
        out["truncated"] = True
    return out


def sequence(g, source: str, target: str, *, stale=()) -> dict:
    """The first call path from ``source`` to ``target`` as messages between the owners of the methods."""
    res = _trace(g, source, target, "flow", stale)
    if res.get("status") != "found":
        return _unresolved("sequence", res)
    path = res["paths"][0]
    parts: dict[str, str] = {}
    for h in path:
        for n in (h["from_id"], h["to_id"]):
            parts.setdefault(_owner(g, n), f"p{len(parts)}")
    lines = ["sequenceDiagram"] + [f"  participant {i} as {_text(o)}" for o, i in parts.items()]
    claims = []
    for h in path:
        a, b = _owner(g, h["from_id"]), _owner(g, h["to_id"])
        callee = g.label(h["to_id"]).lstrip(".")
        extracted = h.get("confidence") == "EXTRACTED"
        lines.append(f"  {parts[a]}{'->>' if extracted else '-)'}{parts[b]}: {_text(callee)}")
        claims.append({"text": f"{_name(g, h['from_id'])} calls {_name(g, h['to_id'])}", "status": _status(extracted),
                       "evidence": [h["at"]] if h.get("at") else []})
    out = {"kind": "sequence", "status": "found",
           "title": f"{_name(g, path[0]['from_id'])} -> {_name(g, path[-1]['to_id'])}",
           "mermaid": "\n".join(lines), "claims": claims,
           "coverage": _coverage("the first call path of `trace`, one message per call; a message drawn as an "
                                 "open arrow comes from an INFERRED edge")}
    if len(res["paths"]) > 1:
        out["other_paths"] = len(res["paths"]) - 1
    for k in ("fuzzy", "resolution_notes"):
        if res.get(k):
            out[k] = res[k]
    return out


def diagram(g, kind: str, source: str | None = None, target: str | None = None, *, mode: str = "flow",
            stale=()) -> dict:
    """One diagram by kind (:data:`KINDS`)."""
    if kind == "architecture":
        return {**architecture(g), "status": "found"}
    if not source:
        raise ValueError(f"a {kind} diagram needs a source symbol")
    if kind == "flow":
        return flow(g, source, target, mode=mode, stale=stale)
    if kind == "sequence":
        if not target:
            raise ValueError("a sequence diagram needs a source and a target symbol")
        return sequence(g, source, target, stale=stale)
    raise ValueError(f"unknown diagram kind {kind!r}: choose one of {', '.join(KINDS)}")


def as_text(d: dict) -> str:
    """A diagram as a Mermaid file: its claims and limits as ``%%`` comments above the diagram."""
    lines = [f"%% {d.get('title') or d['kind']}"]
    for side, note in (d.get("fuzzy") or {}).items():
        lines.append(f"%% {side} resolved by similarity: {note}")
    for c in d.get("claims") or []:
        ev = ", ".join(c.get("evidence") or []) or "no line"
        lines.append(f"%% {c['status']}: {c['text']} at {ev}")
    if d.get("truncated"):
        lines.append(f"%% truncated: {json.dumps(d.get('left_out') or {})}")
    for lim in (d.get("coverage") or {}).get("limits") or []:
        lines.append(f"%% limit: {lim}")
    if d.get("stale_count"):
        lines.append(f"%% note: {d['stale_count']} file(s) changed since the index (run `verinoda update`): "
                     + ", ".join((d.get("stale_files") or [])[:10]))
    return "\n".join(lines + [d["mermaid"]]) + "\n"


# -- the outline ------------------------------------------------------------------------------------

def _slug(title: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "page"
    slug, i = base, 2
    while slug in taken:
        slug, i = f"{base}-{i}", i + 1
    taken.add(slug)
    return slug


def _matches(f: str, pattern: str) -> bool:
    pat = pattern.replace("\\", "/").strip().lstrip("./") if pattern.strip() not in (".", "./") else ""
    if not pat:
        return True
    if any(ch in pat for ch in "*?["):
        return fnmatch.fnmatchcase(f, pat) or fnmatch.fnmatchcase(f, pat.rstrip("/") + "/*")
    pat = pat.rstrip("/")
    return f == pat or f.startswith(pat + "/")


def read_steering(repo: Path) -> tuple[dict | None, list[str]]:
    """The steering file's content (None when there is none) and what is wrong with it."""
    p = Path(repo) / STEERING_FILE
    if not p.is_file():
        return None, []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, [f"{STEERING_FILE}: not read ({type(exc).__name__}: {exc}); the default outline is used"]
    if not isinstance(data, dict) or not isinstance(data.get("pages"), list) or not data["pages"]:
        return None, [f"{STEERING_FILE}: no \"pages\" list; the default outline is used"]
    return data, []


def _default_pages(g) -> list[dict]:
    by: dict[str, list[str]] = defaultdict(list)
    for f in _files(g):
        by[area_of(f)].append(f)
    pages = [{"title": "Overview", "purpose": "The parts of the project and how they depend on each other.",
              "parent": None, "_files": [], "_flows": [], "_overview": True}]
    for a in sorted(by):
        pages.append({"title": a, "purpose": f"The files under {a}/." if a != "(root)" else "The top-level files.",
                      "parent": "Overview", "_files": by[a], "_flows": []})
    return pages


def _steered_pages(g, data: dict, problems: list[str]) -> list[dict]:
    files = _files(g)
    pages = []
    for i, raw in enumerate(data["pages"]):
        if not isinstance(raw, dict) or not str(raw.get("title") or "").strip():
            problems.append(f"pages[{i}]: no title; left out")
            continue
        title = str(raw["title"]).strip()
        paths = raw.get("paths") or []
        paths = [paths] if isinstance(paths, str) else [str(p) for p in paths]
        mine = [f for f in files if any(_matches(f, p) for p in paths)]
        for p in paths:
            if not any(_matches(f, p) for f in files):
                problems.append(f"{title}: path {p!r} matches no indexed file")
        flows = []
        for fl in raw.get("flows") or []:
            if isinstance(fl, (list, tuple)) and len(fl) == 2 and all(isinstance(x, str) and x for x in fl):
                flows.append((fl[0], fl[1]))
            elif isinstance(fl, dict) and fl.get("from") and fl.get("to"):
                flows.append((str(fl["from"]), str(fl["to"])))
            else:
                problems.append(f"{title}: flow {fl!r} is not [\"from\", \"to\"]; left out")
        pages.append({"title": title, "purpose": str(raw.get("purpose") or ""), "parent": raw.get("parent"),
                      "_files": mine, "_flows": flows, "_overview": bool(raw.get("overview"))})
    titles = {p["title"] for p in pages}
    for p in pages:
        if p["parent"] is not None and p["parent"] not in titles:
            problems.append(f"{p['title']}: parent {p['parent']!r} is no page's title; shown at the top")
            p["parent"] = None
    if pages and not any(p["_overview"] for p in pages):
        pages[0]["_overview"] = True  # the first page carries the architecture diagram
    return pages


def _ordered(pages: list[dict]) -> list[dict]:
    """Parents before children, each level in the file's order; ``depth`` set. A parent cycle is cut."""
    kids: dict[str | None, list[dict]] = defaultdict(list)
    for p in pages:
        kids[p["parent"]].append(p)
    out, seen = [], set()

    def walk(parent, depth):
        for p in kids.get(parent, []):
            if id(p) in seen:
                continue
            seen.add(id(p))
            out.append({**p, "depth": depth})
            walk(p["title"], depth + 1)

    walk(None, 0)
    for p in pages:  # in a cycle of parents: at the top
        if id(p) not in seen:
            seen.add(id(p))
            out.append({**p, "parent": None, "depth": 0})
            walk(p["title"], 1)
    return out


def outline(g, repo: Path | str | None = None, *, pages: list[str] | None = None, diagrams: bool = True,
            stale=()) -> dict:
    """The page tree: from :data:`STEERING_FILE` when the repository has one, else an overview and a page
    per part. ``pages``: only these (by id or title) get their diagrams; ``diagrams=False``: none do (each
    page still names the diagrams it has)."""
    repo = Path(repo or g.root)
    # the steering file spells the flows' names: that never makes them names of a changed code file
    stale = [f for f in stale if f != STEERING_FILE]
    data, problems = read_steering(repo)
    raw = _steered_pages(g, data, problems) if data else _default_pages(g)
    taken: set[str] = set()
    for p in raw:
        p["id"] = _slug(p["title"], taken)
    by_title = {p["title"]: p["id"] for p in raw}
    tree = _ordered(raw)
    want = None if pages is None else {str(x).strip().lower() for x in pages}
    assigned = {f for p in tree for f in p["_files"]}
    out_pages = []
    for p in tree:
        page = {"id": p["id"], "title": p["title"], "purpose": p["purpose"], "depth": p["depth"],
                "parent": by_title.get(p["parent"]) if p["parent"] else None,
                "file_count": len(p["_files"]), "files": p["_files"][:MAX_PAGE_FILES]}
        kinds = (["architecture"] if p.get("_overview") else []) + (["files"] if p["_files"] else []) + \
            [k for _ in p["_flows"] for k in ("flow", "sequence")]
        page["diagram_kinds"] = kinds
        if p["_flows"]:
            page["flows"] = [list(f) for f in p["_flows"]]
        if diagrams and (want is None or p["id"] in want or p["title"].lower() in want):
            ds = []
            if p.get("_overview"):
                ds.append(architecture(g, raw if data else None))
            if p["_files"]:
                ds.append(page_diagram(g, p, raw))
            for a, b in p["_flows"]:
                for d in (flow(g, a, b, stale=stale), sequence(g, a, b, stale=stale)):
                    if d.get("status") != "found":
                        problems.append(f"{p['title']}: {d['kind']} {a} -> {b}: {d.get('status')}"
                                        + (f" ({d['next_step']})" if d.get("next_step") else ""))
                    ds.append({**d, "from": a, "to": b})
            page["diagrams"] = ds
        out_pages.append(page)
    res = {"source": STEERING_FILE if data else "default: a page per folder (first two folders of a path)",
           "pages": out_pages, "unassigned_files": len(set(_files(g)) - assigned),
           "coverage": {"method": "pages from " + (STEERING_FILE if data else "the folders of the indexed files")
                        + "; diagrams from the graph's edges",
                        "limits": ["a page's text is its purpose as written; nothing is generated",
                                   "diagram arrows are graph extractions, strong_inference at most"]}}
    if want is not None:
        missing = sorted(want - {p["id"] for p in out_pages} - {p["title"].lower() for p in out_pages})
        if missing:
            problems.append("no page named " + ", ".join(repr(m) for m in missing))
    if problems:
        res["problems"] = problems
    return res


def as_markdown(o: dict) -> str:
    """The outline as one Markdown document: a heading per page, its purpose, files and diagrams."""
    lines = []
    for p in o["pages"]:
        lines += [f"{'#' * min(6, p['depth'] + 1)} {p['title']}", ""]
        if p["purpose"]:
            lines += [p["purpose"], ""]
        if p["files"]:
            more = p["file_count"] - len(p["files"])
            lines += [f"Files ({p['file_count']}): " + ", ".join(f"`{f}`" for f in p["files"])
                      + (f" and {more} more" if more else ""), ""]
        for d in p.get("diagrams") or []:
            if not d.get("mermaid"):
                lines += [f"*{d['kind']} {d.get('from')} -> {d.get('to')}: {d.get('status')}*", ""]
                continue
            head = f"{d['kind'].capitalize()}: {d['title']}" if d["kind"] in ("flow", "sequence") else d["title"]
            lines += [f"**{head}**", "", "```mermaid", d["mermaid"], "```", ""]
            ev = [f"- {c['status']}: {c['text']} ({', '.join(c['evidence']) or 'no line'})" for c in d["claims"]]
            if ev:
                lines += ["<details><summary>Evidence</summary>", "", *ev, "", "</details>", ""]
    for pr in o.get("problems") or []:
        lines.append(f"> problem: {pr}")
    return "\n".join(lines).rstrip() + "\n"


def render(o: dict) -> str:
    """The outline as an indented tree."""
    lines = [f"wiki outline ({o['source']})"]
    for p in o["pages"]:
        kinds = ", ".join(p["diagram_kinds"]) or "no diagram"
        lines.append(f"{'  ' * (p['depth'] + 1)}{p['title']}  [{p['id']}]  {p['file_count']} files; {kinds}")
    if o.get("unassigned_files"):
        lines.append(f"files in no page: {o['unassigned_files']}")
    for pr in o.get("problems") or []:
        lines.append(f"problem: {pr}")
    return "\n".join(lines)
