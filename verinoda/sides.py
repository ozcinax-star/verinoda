"""Client and server separation: client-only code reachable from server code (the map's ``sides`` view).

A Minecraft mod runs on the client and on a dedicated server; code that only exists on the client (Loom's
``src/client`` source set, a class or method annotated ``@Environment(EnvType.CLIENT)`` / ``@OnlyIn(Dist.CLIENT)``,
an ``@EventBusSubscriber(value = Dist.CLIENT)`` class, the game's ``net.minecraft.client`` packages) crashes a
server that loads it. This view walks the graph from the server-side entry points
(:func:`verinoda.architecture_map.framework_entries` without the client ones) over call, use and type edges and
reports every client-only symbol or class it reaches, with the whole path: each hop at its ``file:line``, the
reason the target is client-only at its line. Reachability over extracted edges and heuristic entry points is
``strong_inference`` at most (``weak_inference`` when a hop is an INFERRED edge). Files are read as text; nothing
runs the project's code or reaches the network.
"""

from __future__ import annotations

import re
from collections import Counter, deque

from verinoda import architecture_map as am
from verinoda.index import Graph
from verinoda.testcode import is_test_file

# packages that exist only in the client jar (Mojmap and Yarn share net.minecraft.client) or a loader's client API
CLIENT_PACKAGES = ("net.minecraft.client.", "com.mojang.blaze3d.", "net.fabricmc.fabric.api.client.",
                   "net.minecraftforge.client.", "net.neoforged.neoforge.client.")
CLIENT_SOURCE_SET_RE = re.compile(r"(?:^|/)src/client/")
CLIENT_MARK_RE = re.compile(
    r"@Environment\s*\(\s*(?:value\s*=\s*)?(?:(?:net\.fabricmc\.api\.)?EnvType\s*\.\s*)?CLIENT\s*\)"
    r"|@OnlyIn\s*\(\s*(?:value\s*=\s*)?(?:(?:net\.\w+\.api\.distmarker\.)?Dist\s*\.\s*)?CLIENT\s*\)"
    # an event subscriber or a NeoForge @Mod class for the client alone (one that also names the dedicated
    # server runs on both sides)
    r"|@(?:Mod\s*\.\s*)?EventBusSubscriber\s*\((?![^)]*\bDEDICATED_SERVER\b)[^)]*\bDist\s*\.\s*CLIENT\b[^)]*\)"
    r"|@Mod\s*\((?![^)]*\bDEDICATED_SERVER\b)[^)]*\bdist\s*=\s*\{?\s*(?:Dist\s*\.\s*)?CLIENT\b[^)]*\)")
# "Client" as a word of a name: ClientModInitializer, MinecraftClient, ClientTickEvents.END_CLIENT_TICK, a
# net.minecraft.client package (not "clientsync" or "onClientCommand"'s server handler: see _client_entry)
CLIENT_WORD_RE = re.compile(r"Client(?![a-z])|(?<![A-Za-z])CLIENT(?![A-Za-z])|(?:^|\.)client(?:\.|$)")
# the parts of framework_entries' reasons that say which side runs the entry point
_ENTRY_REASON_RES = (
    re.compile(r'^fabric\.mod\.json entrypoint "(client)"'),
    re.compile(r"^implements (\w+):"),
    re.compile(r"^@SubscribeEvent handler of ([\w.$]+)"),
    re.compile(r"^mixin @\w+ into ((?:[a-z_]\w*\.)*[\w$]+?)(?:\.[a-z_<][^.]*)?$"),
    re.compile(r"^callback (?:registered with|handed to) ([\w.$]+)\("),
)
ANNOTATION_RE = re.compile(r"@[\w.:]+(?:\s*\((?:[^()]|\([^()]*\))*\))?")
# a constructor call in a reached method's code lines (the extractor emits no edge for some)
NEW_RE = re.compile(r"(?<![\w$.])new\s+([A-Z][\w$]*)\s*(?:<[^()]*>)?\s*\(")
# edges a reached method follows; a reached class loads its supertypes (its methods run when called, its
# constructors when it is instantiated)
FOLLOW = frozenset({"calls", "uses", "references", "references_constant", "instantiates", "indirect_call",
                    "registers", "inherits", "extends", "implements"})
SUPERTYPES = frozenset({"inherits", "extends", "implements"})
IMPORT_RE = re.compile(r"^\s*import\s+(?:static\s+)?([\w.]+)(?:\s+as\s+(\w+))?")
QUALIFIED_RE = re.compile(r"(?<![\w.$])((?:" + "|".join(re.escape(p) for p in CLIENT_PACKAGES)
                          + r")(?:[a-z_]\w*\.)*[A-Z]\w*)")
CLAIMS_SHOWN = 60
CROSSINGS_SHOWN = 20
ENTRIES_SHOWN = 20


def _bare(g: Graph, n: str) -> str:
    return g.label(n).strip().strip(".()").rpartition(".")[2]


def _name(g: Graph, n: str) -> str:
    """``Owner.method`` for a method (its class's name before it), else the symbol's bare name."""
    owner = next((u for u, _d in g.in_edges(n, {"method"})), None)
    return f"{_bare(g, owner)}.{_bare(g, n)}" if owner else _bare(g, n)


def _where(g: Graph, n: str) -> str:
    return am._loc(g, n)


def _client_entry(why: str) -> bool:
    """Whether one of an entry point's reasons says it runs on the client: a ``client`` fabric.mod.json
    entrypoint, ``ClientModInitializer``, a client event, a mixin into a client class, a client registrar. Only
    that part of the reason is read: a package, a file path or a mixin target method that says "client" (a
    server handler of a client packet) does not count."""
    for rx in _ENTRY_REASON_RES:
        m = rx.search(why)
        if m:
            return bool(CLIENT_WORD_RE.search(m.group(1)))
    return False


def _own_head(code: list[str], line: int | None, name: str) -> tuple[str, int] | None:
    """``(text, first line)``: a symbol's declaration head (:func:`architecture_map._decl_head`) without what
    belongs to the member above it. Kotlin statements and expression bodies end in no ``;``, ``{`` or ``}``, so
    the head can reach back over another member and its annotations; it starts after the last line holding
    anything but annotations."""
    hd = am._decl_head(code, line, name)
    if not hd:
        return None
    lines = hd[0].split("\n")
    first = hd[1] - (len(lines) - 1)
    above = "\n".join(lines[:-1])
    rest = ANNOTATION_RE.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), above)
    last = len(rest.rstrip())
    keep = rest.count("\n", 0, last) + 1 if last else 0
    return "\n".join(lines[keep:]), first + keep


def _client_marks(g: Graph, files: list[str]) -> dict[str, dict]:
    """``{node: {"why", "at"}}``: the project's client-only symbols, by source set or annotation."""
    out: dict[str, dict] = {}
    for f in files:
        syms = g.symbols_in(f)
        if CLIENT_SOURCE_SET_RE.search(f):
            why = {"why": "in the client source set (src/client/): it is compiled for the client only",
                   "at": f"{f}:1", "basis": "source_set"}
            for n in syms:
                out[n] = why
            continue
        lines = am._read(g.root, f)
        if not lines or not CLIENT_MARK_RE.search("\n".join(lines)):
            continue
        code = am._jvm_code(lines)
        for n in syms:
            if n in out:
                continue
            hd = _own_head(code, g.line(n), _bare(g, n))
            if not hd:
                continue
            m = CLIENT_MARK_RE.search(hd[0])
            if not m:
                continue
            at_line = hd[1] + hd[0].count("\n", 0, m.start())
            # quoted from the file as written (the code lines have their strings emptied)
            raw = CLIENT_MARK_RE.search("\n".join(lines[at_line - 1:hd[1] + hd[0].count("\n")]))
            mark = re.sub(r"\s+", " ", (raw or m).group(0))
            why = {"why": f"{mark} on `{_name(g, n)}`: the loader strips it from the server", "at": f"{f}:{at_line}",
                   "basis": "annotation"}
            out[n] = why
            sp = g.span(n)
            if sp and g.G.nodes[n].get("_callable_class"):  # a client class: everything declared in it
                for v in syms:
                    ln = g.line(v)
                    if v not in out and ln and sp[0] <= ln <= sp[1]:
                        out[v] = why
    return out


def _client_imports(code: list[str]) -> dict[str, tuple[str, int]]:
    """``{simple name: (fully qualified name, import line)}`` of a file's imports from :data:`CLIENT_PACKAGES`."""
    out: dict[str, tuple[str, int]] = {}
    for i, ln in enumerate(code, 1):
        m = IMPORT_RE.match(ln)
        if m and m.group(1).startswith(CLIENT_PACKAGES) and not m.group(1).endswith("*"):
            fqn = m.group(1)
            out[m.group(2) or fqn.rpartition(".")[2]] = (fqn, i)
    return out


def _external_uses(g: Graph, n: str, cache: dict) -> list[tuple[str, int, int | None]]:
    """``(class, use line, import line)``: the client-package classes a reached method's own lines name."""
    f, sp = g.file(n), g.span(n)
    if not f or not sp or g.G.nodes[n].get("_callable_class"):
        return []
    if f not in cache:
        code = am._jvm_code(am._read(g.root, f))
        cache[f] = (code, _client_imports(code))
    code, imports = cache[f]
    found: dict[str, tuple[str, int, int | None]] = {}
    for ln in range(sp[0], min(sp[1], len(code)) + 1):
        if g.symbol_at(f, ln) != n:  # a nested symbol's line is that symbol's
            continue
        text = code[ln - 1]
        for name, (fqn, imp) in imports.items():
            if fqn not in found and re.search(rf"(?<![\w$.]){re.escape(name)}(?![\w$])", text):
                found[fqn] = (fqn, ln, imp)
        for m in QUALIFIED_RE.finditer(text):
            found.setdefault(m.group(1), (m.group(1), ln, None))
    return sorted(found.values(), key=lambda x: (x[1], x[0]))


def _constructors(g: Graph, cid: str) -> list[str]:
    """A class's constructors (Java's methods named after the class, Kotlin's ``constructor``)."""
    return [v for v, _d in g.out_edges(cid, {"method"}) if _bare(g, v) in (_bare(g, cid), "constructor")]


def _next(g: Graph, u: str, classes: dict[str, list[str]], cache: dict) -> list[tuple[str, dict]]:
    """What a reached symbol leads to: a class its supertypes (loading it loads them; its methods run only when
    called); a method or function its :data:`FOLLOW` edges, the constructors of a class it instantiates, and the
    constructors of the project classes its own code lines call with ``new`` (a unique class name; the hop is
    ``INFERRED``, read from the text)."""
    if g.G.nodes[u].get("_callable_class"):
        return list(g.out_edges(u, set(SUPERTYPES)))
    out = []
    for v, d in g.out_edges(u, set(FOLLOW)):
        out.append((v, d))
        if d.get("relation") == "instantiates" and g.G.nodes[v].get("_callable_class"):
            out += [(c, d) for c in _constructors(g, v)]
    f, sp = g.file(u), g.span(u)
    if not f or not sp or not f.endswith(".java"):
        return out
    if f not in cache:
        code = am._jvm_code(am._read(g.root, f))
        cache[f] = (code, _client_imports(code))
    code = cache[f][0]
    for ln in range(sp[0], min(sp[1], len(code)) + 1):
        if g.symbol_at(f, ln) != u:
            continue
        for m in NEW_RE.finditer(code[ln - 1]):
            cids = classes.get(m.group(1).rpartition("$")[2], [])
            if len(cids) != 1:
                continue
            d = {"relation": "instantiates", "source_file": f, "source_location": f"L{ln}", "confidence": "INFERRED"}
            out += [(v, d) for v in _constructors(g, cids[0]) or cids]
    return out


def _hop(g: Graph, u: str, v: str, d: dict) -> dict:
    return {"from": _name(g, u), "to": _name(g, v), "relation": d.get("relation"),
            "at": am._edge_loc(d) or _where(g, u), "confidence": d.get("confidence") or "?"}


def _path(g: Graph, prev: dict, n: str) -> list[dict]:
    hops = []
    while prev.get(n):
        u, d = prev[n]
        hops.append(_hop(g, u, n, d))
        n = u
    return hops[::-1]


def _sentence(entry: dict, hops: list[dict], target: str, why: str) -> str:
    steps = "; ".join(f"{h['from']} {h['relation']} {h['to']} ({h['at']})" for h in hops)
    return (f"server entry point `{entry['symbol']}` ({entry['at']}) reaches client-only {target} in "
            f"{len(hops)} step(s): {steps}; client-only because {why}")


def sides(g: Graph) -> dict:
    files = sorted({f for _n, f in g.G.nodes(data="source_file")
                    if f and f.endswith(am.JVM_SUFFIXES) and not is_test_file(f)})
    coverage = {
        "method": "reachability from the server-side entry points over call, use and type edges to client-only "
                  "code: the src/client source set, @Environment(EnvType.CLIENT) / @OnlyIn(Dist.CLIENT) / "
                  "@EventBusSubscriber(Dist.CLIENT) / @Mod(dist = Dist.CLIENT) classes and methods, and classes of "
                  + ", ".join(p.rstrip(".") for p in CLIENT_PACKAGES) + " named in a reached method",
        "limits": [
            "entry points are heuristics (the dataflow view's JVM entries); one the reason puts on the client "
            "(a client entrypoint, ClientModInitializer, an event type, mixin target class or registrar with "
            "Client as a word of its name) is left out",
            "calls the extractor did not resolve (reflection, lambdas stored and run later, dynamic dispatch "
            "through an interface) are not followed; a reached class loads its supertypes, its methods only when "
            "called, its constructors when instantiated (a `new` in Java code the extractor gave no edge for is "
            "read from the text, an INFERRED hop); field initialisers and static initialisers are not walked",
            "a guarded use (`if (world.isClient)`, a DistExecutor or EnvType check) is still a path: the check is "
            "not read",
            "a path is strong_inference at most (weak_inference through an INFERRED edge); a class-loading "
            "crash also depends on the JVM verifier and the loader",
        ],
    }
    base = {"view": "sides", "coverage": coverage}
    if not files:
        return base | {"summary": {"jvm_files": 0}, "claims": [],
                       "note": "no Java or Kotlin source files: nothing to check"}
    client = _client_marks(g, files)
    entries = am.framework_entries(g)
    roots, left_out = [], 0
    for n, e in sorted(entries.items(), key=lambda kv: (am.ENTRY_TIERS[kv[1]["basis"]], _where(g, kv[0]))):
        if n in client or any(_client_entry(w) for w in e["why"]):
            left_out += 1
            continue
        roots.append({"id": n, "symbol": _name(g, n), "at": _where(g, n), "basis": e["basis"], "why": e["why"]})
    summary = {"jvm_files": len(files), "client_only_symbols": len(client),
               "client_only_by_basis": dict(sorted(Counter(w["basis"] for w in client.values()).items())),
               "server_entry_points": len(roots), "client_entry_points_left_out": left_out}
    searched = {"entry_points": [{k: r[k] for k in ("symbol", "at", "basis", "why")} for r in roots[:ENTRIES_SHOWN]],
                "relations_followed": sorted(FOLLOW)}
    if not roots:
        return base | {"summary": summary, "searched": searched, "claims": [],
                       "unknown": "no server-side entry point found (fabric.mod.json main/server entrypoints, "
                                  "ModInitializer, @Mod, event handlers): no path was searched"}
    origin = {r["id"]: r for r in roots}
    prev: dict[str, tuple[str, dict] | None] = {r["id"]: None for r in roots}
    start: dict[str, str] = {r["id"]: r["id"] for r in roots}
    queue = deque(r["id"] for r in roots)
    claims: list[dict] = []
    cache: dict = {}
    classes: dict[str, list[str]] = {}
    for f in files:
        for n in g.symbols_in(f):
            if g.G.nodes[n].get("_callable_class"):
                classes.setdefault(_bare(g, n), []).append(n)
    while queue:
        u = queue.popleft()
        hops = None
        for fqn, ln, imp in _external_uses(g, u, cache):
            hops = hops if hops is not None else _path(g, prev, u)
            f = g.file(u)
            why = (f"its package ({fqn.rpartition('.')[0]}) is in the client jar only"
                   + (f"; imported at {f}:{imp}" if imp else ""))
            entry = origin[start[u]]
            step = {"from": _name(g, u), "to": fqn, "relation": "names", "at": f"{f}:{ln}", "confidence": "EXTRACTED"}
            claims.append(_claim("client_class_use", fqn, f"{f}:{ln}", entry, hops + [step],
                                 _sentence(entry, hops + [step], f"class `{fqn}`", why),
                                 {"class": fqn, "why": why, "at": f"{f}:{imp}" if imp else f"{f}:{ln}"}))
        for v, d in _next(g, u, classes, cache):
            if v in prev or not g.file(v) or not g.file(v).endswith(am.JVM_SUFFIXES) or is_test_file(g.file(v)):
                continue
            prev[v] = (u, d)
            start[v] = start[u]
            if v in client:
                entry, path = origin[start[v]], _path(g, prev, v)
                mark = client[v]
                what = f"{'class' if g.G.nodes[v].get('_callable_class') else 'symbol'} `{_name(g, v)}` ({_where(g, v)})"
                claims.append(_claim("client_symbol_reached", _name(g, v), path[-1]["at"], entry, path,
                                     _sentence(entry, path, what, mark["why"]),
                                     {"symbol": _name(g, v), "defined_at": _where(g, v), "why": mark["why"],
                                      "at": mark["at"], "basis": mark["basis"]}))
                continue  # the crossing is the finding: not walked further
            queue.append(v)
    # edges from common code into client-only code that no server entry point reaches: listed, not claimed (a
    # compile error under split source sets, dead or client-guarded code otherwise)
    common = set(files)
    crossings = [_hop(g, u, v, d) | {"client_only": client[v]["at"]} for u, v, d in g.edges(set(FOLLOW))
                 if v in client and u not in client and u not in prev and g.file(u) in common]
    crossings.sort(key=lambda c: c["at"])
    order = {"strong_inference": 0, "weak_inference": 1}
    claims.sort(key=lambda c: (order[c["status"]], len(c["path"]), c["at"]))
    counts = Counter(c["status"] for c in claims)
    summary |= {"symbols_reached": len(prev), "paths": len(claims), "strong_inference": counts.get("strong_inference", 0),
                "weak_inference": counts.get("weak_inference", 0), "unreached_crossings": len(crossings)}
    return base | {
        "summary": summary,
        "searched": searched,
        "claims": claims[:CLAIMS_SHOWN],
        **({"unreached_crossings": crossings[:CROSSINGS_SHOWN]} if crossings else {}),
        **({"truncated": True, "claims_not_shown": len(claims) - CLAIMS_SHOWN} if len(claims) > CLAIMS_SHOWN else {}),
    }


def _claim(kind: str, subject: str, at: str, entry: dict, path: list[dict], text: str, client_only: dict) -> dict:
    status = "weak_inference" if any(h["confidence"] != "EXTRACTED" for h in path) else "strong_inference"
    return {"kind": kind, "subject": subject, "at": at, "claim": text, "status": status,
            "entry": {k: entry[k] for k in ("symbol", "at", "basis")}, "path": path, "client_only": client_only,
            "evidence_at": [entry["at"]] + [h["at"] for h in path] + [client_only["at"]],
            "derived_by": "verinoda.sides (reachability over graph edges from heuristic entry points)"}
