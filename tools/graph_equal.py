"""Compare two Verinoda graphs node for node and edge for edge (the test of an incremental update).

    python tools/graph_equal.py A/.verinoda/index/graph.json B/.verinoda/index/graph.json [--json]

What is compared: the set of node ids with each node's attributes, and the multiset of edges with theirs, both
without the attributes that depend on history rather than on the tree (``community`` - clustering is remapped to
the previous build's numbers - and layout or timestamp fields). Exit 0 when equal, 1 with the first differences
otherwise, 2 when a file cannot be read.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

HISTORY_FIELDS = {"community", "x", "y", "z", "built_at", "generated_at"}


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _clean(d: dict) -> dict:
    return {k: v for k, v in d.items() if k not in HISTORY_FIELDS}


def canonical(data: dict) -> tuple[dict, Counter]:
    nodes = {n["id"]: _clean({k: v for k, v in n.items() if k != "id"}) for n in data.get("nodes") or []}
    key = "links" if "links" in data else "edges"
    edges: Counter = Counter()
    for e in data.get(key) or []:
        d = _clean({k: v for k, v in e.items() if k not in ("source", "target", "_src", "_tgt", "key")})
        edges[(e.get("_src", e.get("source")), e.get("_tgt", e.get("target")), json.dumps(d, sort_keys=True))] += 1
    return nodes, edges


def compare(a: dict, b: dict, limit: int = 20) -> dict:
    na, ea = canonical(a)
    nb, eb = canonical(b)
    only_a = sorted(set(na) - set(nb))
    only_b = sorted(set(nb) - set(na))
    changed = sorted(n for n in set(na) & set(nb) if na[n] != nb[n])
    e_only_a = list((ea - eb).elements())
    e_only_b = list((eb - ea).elements())
    return {"equal": not (only_a or only_b or changed or e_only_a or e_only_b),
            "nodes": [len(na), len(nb)], "edges": [sum(ea.values()), sum(eb.values())],
            "nodes_only_in_a": only_a[:limit], "nodes_only_in_b": only_b[:limit],
            "nodes_changed": [{"id": n, "a": na[n], "b": nb[n]} for n in changed[:limit]],
            "edges_only_in_a": [list(e) for e in e_only_a[:limit]], "edges_only_in_b": [list(e) for e in e_only_b[:limit]],
            "counts": {"nodes_only_in_a": len(only_a), "nodes_only_in_b": len(only_b), "nodes_changed": len(changed),
                       "edges_only_in_a": len(e_only_a), "edges_only_in_b": len(e_only_b)}}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("a")
    ap.add_argument("b")
    ap.add_argument("--json", action="store_true")
    ns = ap.parse_args(argv)
    try:
        res = compare(_load(Path(ns.a)), _load(Path(ns.b)))
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if ns.json:
        print(json.dumps(res, indent=1))
    else:
        print(("EQUAL" if res["equal"] else "DIFFERENT") + f": nodes {res['nodes']}, edges {res['edges']}, "
              + ", ".join(f"{k} {v}" for k, v in res["counts"].items()))
        for k in ("nodes_only_in_a", "nodes_only_in_b", "edges_only_in_a", "edges_only_in_b"):
            for x in res[k][:5]:
                print(f"  {k}: {x}")
        for x in res["nodes_changed"][:5]:
            print(f"  changed {x['id']}: " + json.dumps({k: (x['a'].get(k), x['b'].get(k)) for k in
                                                         set(x['a']) | set(x['b']) if x['a'].get(k) != x['b'].get(k)})[:300])
    return 0 if res["equal"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
