"""The butterfly view: one symbol in the middle, its callers on one side and its callees on the other,
or, for a type, what it extends and implements on one side and what extends or implements it on the other.

Each side is a tree walked outwards from the centre over the index's graph, up to ``depth`` links: an item
names the node it hangs from (``via``), the link's relation, the ``file:line`` the link is written on and the
extractor's confidence. Every link is a claim ("A calls B") whose status is the ceiling of an unchecked
graph edge (:func:`verinoda.graph_export.edge_status`: ``strong_inference`` for an EXTRACTED edge,
``weak_inference`` for an INFERRED one, ``unknown`` without a line): graph edges are extractions, never
verification. A node already shown on a side is not listed again there (a recursive call, a diamond);
the link that reached it first is the one given.

Callers and callees are ``calls`` edges only. Code outside the project (a library class, a builtin) has no
node of its own to open: it is a leaf, listed only in the inheritance view (``Exception``, ``Runnable``).
"""

from __future__ import annotations

from verinoda.graph_export import edge_status

MODES = ("calls", "inherits")
# mode -> ((side key, relations, the centre's edges run out of it), (the other side ...))
SIDES = {
    "calls": (("callers", ("calls",), False), ("callees", ("calls",), True)),
    "inherits": (("supertypes", ("inherits", "implements"), True), ("subtypes", ("inherits", "implements"), False)),
}
MAX_DEPTH = 4
MAX_SIDE = 200      # nodes per side (the side says it stopped)


def _at(d: dict) -> str | None:
    loc = str(d.get("source_location") or "")
    f = d.get("source_file")
    if f and loc.startswith("L") and loc[1:].isdigit():
        return f"{f}:{loc[1:]}"
    return f or None


def _node_at(g, n: str) -> str | None:
    f, ln = g.file(n), g.line(n)
    return f"{f}:{ln}" if f and ln else f


def is_external(g, n: str) -> bool:
    d = g.G.nodes[n]
    return bool(d.get("external") or d.get("file_type") == "concept" or not d.get("source_file"))


def _hidden(g, n: str) -> bool:
    d = g.G.nodes[n]
    return d.get("file_type") == "rationale" or g.is_file_node(n)


def default_mode(g, nid: str) -> str:
    """``inherits`` for a class (it has a supertype or a subtype), else ``calls``."""
    if g.G.nodes[nid].get("_callable_class") or any(True for _ in g.out_edges(nid, {"inherits", "implements"})) \
            or any(True for _ in g.in_edges(nid, {"inherits", "implements"})):
        return "inherits"
    return "calls"


def butterfly(g, nid: str, *, mode: str | None = None, depth: int = 2, tests: bool = True, keep=None,
              is_test=None) -> dict:
    """Both sides of ``nid`` (see the module docstring). ``keep(n)`` (optional) drops nodes a caller does not
    show; ``is_test(file)`` with ``tests=False`` drops test code."""
    if nid not in g.G:
        raise KeyError(f"no node {nid!r}")
    mode = mode or default_mode(g, nid)
    if mode not in MODES:
        raise ValueError(f"mode is one of {', '.join(MODES)}")
    depth = max(1, min(MAX_DEPTH, int(depth)))
    sides = []
    for key, rels, out in SIDES[mode]:
        items, truncated = _walk(g, nid, set(rels), out, depth, mode, tests, keep, is_test)
        sides.append({"key": key, "count": len(items), "truncated": truncated, "items": items})
    return {"id": nid, "label": g.label(nid), "at": _node_at(g, nid), "mode": mode, "depth": depth, "sides": sides}


def _walk(g, nid: str, rels: set[str], out: bool, depth: int, mode: str, tests: bool, keep, is_test):
    seen = {nid}
    items: list[dict] = []
    frontier = [nid]
    truncated = False
    for level in range(1, depth + 1):
        nxt = []
        for u in frontier:
            edges = g.out_edges(u, rels) if out else g.in_edges(u, rels)
            # stated links before inferred ones, then by where they are written
            for v, d in sorted(edges, key=lambda e: (e[1].get("confidence") != "EXTRACTED", str(_at(e[1]) or ""),
                                                     e[0])):
                if v in seen or v not in g.G or _hidden(g, v):
                    continue
                ext = is_external(g, v)
                if ext and mode == "calls":
                    continue
                if keep is not None and not keep(v):
                    continue
                if not tests and is_test is not None and is_test(g.file(v) or ""):
                    continue
                if len(items) >= MAX_SIDE:
                    truncated = True
                    break
                seen.add(v)
                at = _at(d)
                a, b = (u, v) if out else (v, u)
                rel = str(d.get("relation"))
                verb = {"calls": "calls", "inherits": "extends", "implements": "implements"}.get(rel, rel)
                items.append({"id": v, "label": g.label(v), "at": _node_at(g, v), "depth": level, "via": u,
                              "relation": rel, "edge_at": at, "confidence": d.get("confidence"),
                              "external": ext,
                              "claim": {"text": f"{g.label(a)} {verb} {g.label(b)}",
                                        "status": edge_status(d.get("confidence"), at),
                                        "evidence": [at] if at else []}})
                if not ext:
                    nxt.append(v)
            if truncated:
                break
        frontier = nxt
        if truncated or not frontier:
            break
    return items, truncated


def run(g, symbol: str, *, mode: str | None = None, depth: int = 2, stale=None, tests: bool = True) -> dict:
    """:func:`butterfly` for a name resolved by :func:`verinoda.naming.resolve`; ``status`` found, ambiguous
    (the candidates, none picked) or not_found."""
    from verinoda import naming
    from verinoda.testcode import is_test_file

    r = naming.resolve(g, symbol, stale=stale)
    if r.status == naming.EXACT and r.node:
        res = butterfly(g, r.node, mode=mode, depth=depth, tests=tests, is_test=is_test_file)
        return {"status": "found", "query": symbol, **res, **({"resolution_note": r.note} if r.note else {})}
    return {"status": "ambiguous" if r.status == naming.AMBIGUOUS else "not_found", "query": symbol,
            "candidates": r.rows(g, 10), "note": r.note}


HEADS = {"callers": "called by", "callees": "calls", "supertypes": "extends / implements",
         "subtypes": "extended / implemented by"}


def render(res: dict) -> str:
    if res.get("status") not in (None, "found"):
        out = [f"{res['query']}: {res['status']}" + (f" ({res['note']})" if res.get("note") else "")]
        out += [f"  - {c['label']} ({c['at']})" for c in res.get("candidates") or []]
        return "\n".join(out)
    out = [f"{res['label']} ({res['at']}), {res['mode']} view, {res['depth']} link(s) out:"]
    if res.get("resolution_note"):
        out.insert(0, f"note: {res['resolution_note']}")
    for side in res["sides"]:
        out.append(f"  {HEADS.get(side['key'], side['key'])}: {side['count']}"
                   + (" (stopped)" if side["truncated"] else ""))
        if not side["items"]:
            out.append("    none in the index")
        children: dict[str, list[dict]] = {}
        for it in side["items"]:
            children.setdefault(it["via"], []).append(it)

        def walk(parent: str, indent: int) -> None:
            for it in children.get(parent, []):
                where = it["edge_at"] or "no line"
                ext = ", outside the project" if it["external"] else ""
                out.append(f"{'  ' * indent}- {it['label']}  at {where}  [{it['claim']['status']}{ext}]")
                walk(it["id"], indent + 1)

        walk(res["id"], 2)
    out.append("links are extractions from the index, not verified (`verinoda analyze` checks one against its line)")
    return "\n".join(out)
