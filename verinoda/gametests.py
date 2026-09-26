"""Minecraft GameTest registry and the tests a change should run (docs/DESIGN.md D49).

Fabric runs only the GameTest classes named in a ``fabric.mod.json`` under the ``fabric-gametest`` (server) and
``fabric-client-gametest`` (client) entrypoints. A class with ``@GameTest`` methods that no entrypoint names is
never run, however well it tests the change. :func:`registry` reads those entrypoints (every ``fabric.mod.json``
of the repository outside build output; a file with trailing junk after its JSON object is still read);
:func:`for_change` answers "which registered GameTests reach these symbols" from static test reach
(:func:`verinoda.testcode.reach`: calls, uses and references from the test method, up to three steps), nearest
first, and names the test classes that reach the change but are not registered.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ENTRYPOINTS = {"fabric-gametest": "server", "fabric-client-gametest": "client"}
_SKIP_DIRS = {"build", ".gradle", "out", "bin", "node_modules", ".git", ".verinoda", "run"}
_PACKAGE = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.M)


def _json_prefix(text: str):
    """The first JSON value of ``text`` (a file may carry junk after it, as a literal ``\\n``)."""
    return json.JSONDecoder().raw_decode(text.lstrip("﻿ \t\r\n"))[0]


def registry(repo: Path) -> dict:
    """``{"files": [...], "classes": {fqn: {"kind", "at"}}, "errors": [...]}`` from the repository's
    ``fabric.mod.json`` files."""
    repo = Path(repo)
    files: list[str] = []
    classes: dict[str, dict] = {}
    errors: list[str] = []
    for p in sorted(repo.rglob("fabric.mod.json")):
        rel = p.relative_to(repo).as_posix()
        if any(part in _SKIP_DIRS for part in p.relative_to(repo).parts[:-1]):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
            data = _json_prefix(text)
        except (OSError, ValueError) as exc:
            errors.append(f"{rel}: {type(exc).__name__}")
            continue
        eps = data.get("entrypoints") if isinstance(data, dict) else None
        if not isinstance(eps, dict):
            continue
        lines = text.splitlines()
        for key, kind in ENTRYPOINTS.items():
            for e in eps.get(key) or []:
                fqn = e.get("value") if isinstance(e, dict) else e
                if not isinstance(fqn, str) or not fqn:
                    continue
                fqn = fqn.split("::", 1)[0]
                ln = next((i for i, s in enumerate(lines, 1) if f'"{fqn}"' in s), 0)
                classes.setdefault(fqn, {"kind": kind, "at": f"{rel}:{ln}" if ln else rel})
        if any(k in eps for k in ENTRYPOINTS):
            files.append(rel)
    return {"files": files, "classes": classes, "errors": errors}


def _fqn(g, cls: str) -> str | None:
    f = g.file(cls)
    if not f:
        return None
    try:
        head = (g.root / f).read_text(encoding="utf-8", errors="replace")[:4000]
    except OSError:
        return None
    m = _PACKAGE.search(head)
    name = g.label(cls).strip()
    return f"{m.group(1)}.{name}" if m else name


def gametest_classes(g) -> dict[str, dict]:
    """Class node -> ``{"fqn", "file", "tests": [TestUnit]}`` for every class holding a ``@GameTest`` method or a
    Fabric client game test (``runTest`` of a class under a gametest source set)."""
    from verinoda import testcode

    out: dict[str, dict] = {}
    for u in testcode.test_units(g):
        if u.node is None or u.lang != "jvm":
            continue
        cls = next((c for c, _ in g.in_edges(u.node, {"method"})), None)
        if cls is None:
            continue
        head = _decl(g, u.node)
        if "GameTest" not in head and "gametest" not in (u.file or "").lower():
            continue
        e = out.setdefault(cls, {"fqn": _fqn(g, cls), "file": g.file(cls), "tests": []})
        e["tests"].append(u)
    return out


def _decl(g, n: str) -> str:
    """The annotation lines above a symbol's declaration (for ``@GameTest``)."""
    f, ln = g.file(n), g.line(n)
    if not f or not ln:
        return ""
    try:
        lines = (g.root / f).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[max(0, ln - 4):ln + 1])


HUB_FAN_IN = 60   # a class more code points at than this (the mod's main class) is not a neighbour of the change


def _pkg_distance(a: str | None, b: str | None) -> int:
    """How many directory steps separate two files (0: the same folder)."""
    pa, pb = (a or "").split("/")[:-1], (b or "").split("/")[:-1]
    k = 0
    while k < min(len(pa), len(pb)) and pa[k] == pb[k]:
        k += 1
    return (len(pa) - k) + (len(pb) - k)


def for_change(g, seeds: set[str], *, depth: int = 2, limit: int = 12) -> dict | None:
    """The GameTest classes whose tests reach ``seeds`` (changed symbols), registered ones first, nearest first;
    None when the repository has no GameTest class.

    A test reaches the change directly (static test reach, ``depth`` steps) or through a neighbour: a class one of
    whose methods calls or registers a changed symbol (not the main class every feature hangs from, over
    :data:`HUB_FAN_IN` edges in); a test that reaches any method of that class exercises the state the change
    shares with it (a test that summons an NPC, then waits for the tick handler the change is in)."""
    from verinoda import testcode

    classes = gametest_classes(g)
    if not classes:
        return None
    reg = registry(g.root)
    changed_files = {g.file(s) for s in seeds if g.file(s)}
    callers = {u for s in seeds for u, _d in g.in_edges(s, {"calls", "registers"})
               if g.file(u) and g.file(u) not in changed_files and not testcode.is_test_code(g, u)}
    near: dict[str, tuple[str, str]] = {}  # method of a neighbour class -> (class, its method calling the change)
    for u in sorted(callers):
        for c, _d in g.in_edges(u, {"method"}):
            if sum(1 for _ in g.in_edges(c)) > HUB_FAN_IN:
                continue
            for m, _d2 in g.out_edges(c, {"method"}):
                near.setdefault(m, (c, u))
    best: dict[str, dict] = {}
    for dd in range(1, depth + 1):
        covers = testcode.reach(g, dd)["covers"]
        for s in seeds:
            for tid in covers.get(s, ()):
                best.setdefault(tid, {"distance": dd, "reaches": g.label(s).strip(), "via": None, "pkg": 0})
        for m, (c, u) in near.items():
            for tid in covers.get(m, ()):
                best.setdefault(tid, {"distance": dd + 1, "reaches": g.label(m).strip(),
                                      "via": f"{g.label(c).strip()}.{g.label(u).strip(".()")} calls the change",
                                      "pkg": min(_pkg_distance(g.file(c), f) for f in changed_files)})
    rows: list[dict] = []
    for cls, e in classes.items():
        hits = sorted(((best[u.id], u) for u in e["tests"] if u.id in best),
                      key=lambda x: (x[0]["distance"], x[0]["pkg"], x[1].line))
        if not hits:
            continue
        top = hits[0][0]
        r = reg["classes"].get(e["fqn"] or "")
        rows.append({"class": g.label(cls).strip(), "fqn": e["fqn"], "file": e["file"], "distance": top["distance"],
                     "tests": [testcode.clean_name(u.name) for _b, u in hits][:8], "n_tests": len(hits),
                     "reaches": top["reaches"], **({"via": top["via"]} if top["via"] else {}),
                     "registered": bool(r), **({"kind": r["kind"], "registered_at": r["at"]} if r else {}),
                     "_pkg": top["pkg"]})
    rows.sort(key=lambda r: (not r["registered"], r["distance"], r["_pkg"], -r["n_tests"], r["class"]))
    for r in rows:
        r.pop("_pkg")
    registered = [r for r in rows if r["registered"]]
    unregistered = [r for r in rows if not r["registered"]]
    return {"registered": registered[:limit], "more_registered": max(0, len(registered) - limit),
            "unregistered": unregistered[:limit], "registry_files": reg["files"],
            **({"registry_errors": reg["errors"]} if reg["errors"] else {}),
            "basis": f"static test reach from each GameTest method (calls, uses, references; up to {depth} steps), "
                     "directly or to a class that calls the change; registration read from fabric.mod.json "
                     "entrypoints"}


def render(res: dict | None) -> list[str]:
    if not res:
        return []
    out = []
    reg = res["registered"]
    if reg:
        out.append("GameTests to run (registered, nearest first): " + "; ".join(
            f"{r['class']} (d{r['distance']}: {', '.join(r['tests'][:3])}"
            + (f" +{r['n_tests'] - 3}" if r["n_tests"] > 3 else "") + ")" for r in reg[:8])
            + (f"; +{len(reg) - 8 + res.get('more_registered', 0)} more" if len(reg) > 8 or res.get("more_registered")
               else ""))
    elif res["registry_files"]:
        out.append("GameTests to run: no registered GameTest reaches the change")
    for r in res["unregistered"][:5]:
        out.append(f" warning: {r['class']} ({r['file']}) has GameTests that reach the change but no "
                   f"fabric-gametest entrypoint names {r['fqn']}: they never run")
    return out
