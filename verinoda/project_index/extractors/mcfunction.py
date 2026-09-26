"""Minecraft datapack functions (``.mcfunction``) for the graph (docs/DESIGN.md D52).

One node per function file, labelled with its id (``wings:fold``); ``function ns:x`` and ``execute ... run
function ns:x`` are ``calls`` edges to the called function's file, ``schedule function ns:x 20t`` a ``registers``
edge with the delay in ticks (the call happens later, as for a scheduler in Java: docs/DESIGN.md D47). A function
the datapack's ``#minecraft:tick`` or ``#minecraft:load`` tag lists carries it in ``metadata.events``. The parsing
is :mod:`verinoda.datapack`'s, so the graph and ``verinoda datapack`` read a function the same way.
"""
from __future__ import annotations

from pathlib import Path

from verinoda.project_index.extractors.base import _make_id
from verinoda.project_index.security import sanitize_metadata

_UNITS = {"t": 1, "s": 20, "d": 24000}


def _ticks(delay: str | None) -> str | None:
    if not delay:
        return None
    unit = delay[-1] if delay[-1] in _UNITS else "t"
    num = delay[:-1] if delay[-1] in _UNITS else delay
    return str(int(float(num) * _UNITS[unit])) if num.replace(".", "", 1).isdigit() else delay


def extract_mcfunction(path: Path) -> dict:
    from verinoda import datapack

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return {"nodes": [], "edges": [], "error": str(e)}
    str_path = str(path)
    fid = datapack.function_id(path) or path.stem
    root = datapack.datapack_root(path)
    events = []
    if root is not None:
        for ev in ("minecraft:tick", "minecraft:load"):
            if fid in datapack.tag_members(root, ev):
                events.append("#" + ev)
    file_nid = _make_id(str_path)
    nodes = [{"id": file_nid, "label": fid, "file_type": "code", "source_file": str_path, "source_location": "L1",
              "metadata": sanitize_metadata({"language": "mcfunction", "kind": "function",
                                             **({"events": events} if events else {})})}]
    edges = []
    calls, _tags, _objs = datapack.parse_function(text)
    seen: set[tuple[str, str]] = set()
    for line, target, how, delay in calls:
        tgt = datapack.resolve_call(root, target) if root is not None else None
        if tgt is None:
            continue
        tid = _make_id(str(tgt))
        rel = "registers" if how == "schedule" else "calls"
        if tid == file_nid or (tid, rel) in seen:
            continue
        seen.add((tid, rel))
        edge = {"source": file_nid, "target": tid, "relation": rel, "confidence": "EXTRACTED",
                "source_file": str_path, "source_location": f"L{line}", "weight": 1.0, "target_file": str(tgt),
                "context": f"{how} {target}" + (f" {delay}" if delay else "")}
        if rel == "registers":
            edge.update(registrar="schedule function", delay=_ticks(delay))
        edges.append(edge)
    return {"nodes": nodes, "edges": edges}
