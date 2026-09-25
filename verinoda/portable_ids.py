"""Graph ids that do not carry the machine's path (applied after every index build, ``index.build``).

The upstream pipeline (``project_index``, never hand-edited: docs/UPSTREAM.md) makes every node's id
repo-relative, but an id with no file of its own keeps what it was minted from. The target of an
import whose file does not exist (``require('./missing')``, resolved against the importer) and the
element ids of a ``.dmf`` interface file (they embed the window's id) carry the scan root folded
into ``_`` parts - ``c_users_me_src_proj_...``, the user name included - into graph.json and
everything built from it: the search index, answers given to agents, exported pages.

After each build Verinoda rewrites graph.json so those ids are the same on every machine. Only
such ids are touched: one that STARTS with the root's folded form (the form an absolute path
mints) and belongs to no node with a source file (a real node is already canonical), or one that
holds the root right after ``_elem_`` (the ``.dmf`` form, on real nodes); such a node's ``label`` and
``norm_label``, minted from the same path, lose it too. Nothing under a root of one folder
(``/app``, ``C:\\code``: its folded name is an ordinary word as often as a path). A rewritten id
that would take one another thing already has (a missing ``./secrets`` beside the ``secrets``
module) keeps apart as ``<id>_unresolved``.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path, PurePath


def strip_root_from_ids(nodes: list, edges: list, root: Path, hyperedges: list | None = None) -> int:
    """Rewrite, in place, the ids of ``nodes`` / ``edges`` (and hyperedge members) minted from an
    absolute path under ``root``. Returns how many ids changed."""
    from verinoda.project_index.ids import make_id

    try:
        pure = PurePath(root)
        parts = [x for x in pure.parts if x not in (pure.anchor, "")]
    except (TypeError, ValueError):
        return 0
    if len(parts) < 2:
        return 0
    slug = make_id(str(root))
    if not slug:
        return 0
    lead = slug + "_"
    mid = "_elem_" + slug + "_"
    owned = {n.get("id") for n in nodes if isinstance(n, dict) and n.get("source_file")}

    def rewritable(i) -> bool:  # the .dmf form is on real nodes; the leading form never is
        return isinstance(i, str) and (mid in i or (i not in owned and (i == slug or i.startswith(lead))))

    touched = {i for n in nodes if isinstance(n, dict) for i in [n.get("id")] if rewritable(i)}
    touched |= {i for e in edges if isinstance(e, dict) for k in ("source", "target") for i in [e.get(k)]
                if rewritable(i)}
    if not touched:
        return 0
    taken = ({n.get("id") for n in nodes if isinstance(n, dict)}
             | {e.get(k) for e in edges if isinstance(e, dict) for k in ("source", "target")}) - touched
    def without_root(i: str) -> str:
        new = i[len(lead):] if i.startswith(lead) else i.replace(mid, "_elem_") if mid in i else i
        return new.strip("_") or i

    remap: dict[str, str] = {}
    for i in touched:
        new = without_root(i)
        if new == i:
            continue
        if new in taken:
            new = f"{new}_unresolved"
        remap[i] = new
    changed = 0
    for n in nodes:
        if isinstance(n, dict) and n.get("id") in remap:
            n["id"] = remap[n["id"]]
            changed += 1
            for key in ("label", "norm_label"):  # a node with no file of its own is labelled with its id
                v = n.get(key)
                if isinstance(v, str) and (v == slug or v.startswith(lead) or mid in v):
                    n[key] = without_root(v)
    for e in edges:
        if not isinstance(e, dict):
            continue
        for k in ("source", "target"):
            if e.get(k) in remap:
                e[k] = remap[e[k]]
                changed += 1
    for he in hyperedges or []:
        if not isinstance(he, dict):
            continue
        for key in ("nodes", "members", "node_ids"):
            if isinstance(he.get(key), list):
                he[key] = [remap.get(x, x) if isinstance(x, str) else x for x in he[key]]
    return changed


def make_graph_portable(graph_file: Path, root: Path) -> dict:
    """Rewrite ``graph_file`` (graph.json) in place when some ids carry ``root``; ``{"changed": n}``.
    ``index.build`` does the same in the one rewrite that also drops the nodes of missing files
    (``index._post_process``)."""
    p = Path(graph_file)
    data = json.loads(p.read_text(encoding="utf-8"))
    edges = data.get("links") if isinstance(data.get("links"), list) else data.get("edges") or []
    n = strip_root_from_ids(data.get("nodes") or [], edges, Path(root).resolve(), data.get("hyperedges"))
    if n:
        write_graph(p, data)
    return {"changed": n}


def write_graph(graph_file: Path, data: dict) -> None:
    """Write graph.json the way the upstream pipeline writes it (indent 2, key order kept), so its
    own "the graph did not change" comparison on the next update still works."""
    p = Path(graph_file)
    fd, tmp = tempfile.mkstemp(prefix=p.name + ".", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        os.replace(tmp, p)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
