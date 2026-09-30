"""Top-down views of a project built on the knowledge graph.

Each view returns ``{"view", "coverage", ...data}`` where ``coverage`` states
the method used and what the view cannot see. Relations that the extractor or
a scanner did not observe are never invented; heuristics are labelled.

Views: hierarchy, dependencies (calls/imports), dataflow (entry -> persistence),
config (env vars and config files), tests (test -> behaviour), history (git +
decision records), impact (reverse dependencies of a change), cycles (dependency
cycles between files and the fewest dependencies to cut), dead (code no entry point
reaches, :mod:`verinoda.deadcode`), hotspots (change frequency times complexity,
:mod:`verinoda.hotspots`), sides (client-only code reachable from server code,
:mod:`verinoda.sides`), repo (files ranked by PageRank toward the files in play, with their signatures, under
a token budget).
"""

from __future__ import annotations

import heapq
import re
import sys
from collections import Counter, defaultdict, deque
from pathlib import Path, PurePosixPath

import networkx as nx

from verinoda import testcode
from verinoda.index import CODE_RELATIONS, FLOW_RELATIONS, Graph
from verinoda.snapshot import git

# the one test rule (verinoda.testcode), re-exported for the callers that import it from the map
from verinoda.testcode import TEST_FILE_RE, is_test_file  # noqa: F401

# -- shared helpers -------------------------------------------------------------

DOC_DECISION_RE = re.compile(r"(^|/)(adr|adrs|decisions?|rfcs?)/|ARCHITECTURE\.md$|DESIGN\.md$", re.I)

ENV_PATTERNS = [
    (re.compile(r"os\.environ\.get\(\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]"), "python"),
    (re.compile(r"os\.environ\[\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]\s*\]"), "python"),
    (re.compile(r"os\.getenv\(\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]"), "python"),
    (re.compile(r"process\.env\.([A-Za-z_][A-Za-z0-9_]*)"), "js"),
    (re.compile(r"process\.env\[\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]"), "js"),
    (re.compile(r"os\.Getenv\(\s*\"([A-Za-z_][A-Za-z0-9_]*)\""), "go"),
    (re.compile(r"env::var\(\s*\"([A-Za-z_][A-Za-z0-9_]*)\""), "rust"),
    (re.compile(r"System\.getenv\(\s*\"([A-Za-z_][A-Za-z0-9_]*)\""), "java"),
]
CONFIG_FILE_RE = re.compile(
    r"(^|/)(\.env(\.[\w-]+)?|settings\.py|config\.(py|ts|js|json|ya?ml|toml)|[\w.-]+\.(ini|cfg|toml|ya?ml))$"
)

SINK_PATTERNS = [
    (re.compile(r"\bsqlite3\.connect\b|\bpsycopg2?\.connect\b|\bcreate_engine\(|\bMongoClient\("), "db-connection"),
    (re.compile(r"\b(INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|CREATE\s+TABLE)\b", re.I), "sql-write"),
    (re.compile(r"\bSELECT\s+.+\s+FROM\b", re.I), "sql-read"),
    (re.compile(r"\.commit\(\)|\bsession\.add\(|\.save\(\s*\)|\.objects\.create\("), "orm-write"),
    (re.compile(r"open\([^)]*['\"][wa]b?\+?['\"]|\.write_text\(|\.write_bytes\(|json\.dump\("), "file-write"),
    (re.compile(r"\bredis\.|\.set\(\s*['\"]|\bputObject\b|\bs3\.put_object\b"), "kv/object-store"),
]
ENTRY_DECORATOR_RE = re.compile(
    r"@\s*(app|router|bp|blueprint|api)\.(route|get|post|put|patch|delete)\b|@(Get|Post|Put|Delete|RequestMapping)Mapping|@click\.command|@app\.command"
)
ENTRY_NAME_RE = re.compile(r"(^|_)(handler|handle|endpoint|view|route|controller|main|cli|command)(_|$)", re.I)
ENTRY_FILE_RE = re.compile(r"(^|/)(api|apis|views?|routes?|handlers?|controllers?|endpoints?|cli|main|__main__|app|server)\.[a-z]+$", re.I)

# JVM mods (Fabric, NeoForge / Forge): entry points the framework calls, read from the code text and
# fabric.mod.json (heuristics, labelled); see framework_entries
JVM_SUFFIXES = (".java", ".kt")
FABRIC_INITIALIZERS = {"ModInitializer": "onInitialize", "ClientModInitializer": "onInitializeClient",
                       "DedicatedServerModInitializer": "onInitializeServer", "PreLaunchEntrypoint": "onPreLaunch"}
FABRIC_ENTRY_METHODS = {"main": "onInitialize", "client": "onInitializeClient", "server": "onInitializeServer",
                        "preLaunch": "onPreLaunch"}
MIXIN_HANDLER_RE = re.compile(r"@(Inject|Redirect|ModifyVariable|ModifyArgs?|ModifyConstant|Overwrite|WrapOperation|"
                              r"WrapWithCondition|ModifyExpressionValue|ModifyReturnValue)\b")
# `ServerTickEvents.END_SERVER_TICK.register`, `UseBlockCallback.EVENT.register`: an event field's register;
# `modBus.addListener`: a NeoForge / Forge event bus listener
EVENT_REGISTRAR_RE = re.compile(r"(?:^|\.)[A-Z][A-Z0-9_]*\.register$|(?:^|\.)addListener$")
# other calls that hand a method to the game to call later: a block entity ticker, a packet handler, a command
# (a method handed to `forEach` / `map` / `computeIfAbsent` runs at once and is no entry point)
CALLBACK_REGISTRAR_RE = re.compile(r"(?:^|\.)(?:createTickerHelper|playToServer|playToClient|playBidirectional|"
                                   r"registerGlobalReceiver|registerReceiver|executes)$")
ENTRY_TIERS = {"declared": 0, "framework": 1, "callback": 2}   # before the name heuristics (3)
# persistence in JVM code (comment- and string-free lines of .java / .kt files; heuristics)
JVM_SINK_PATTERNS = [
    (re.compile(r"\bFiles\s*\.\s*(?:write|writeString|newBufferedWriter|newOutputStream|copy|move|createFile)\s*\(|"
                r"\bnew\s+(?:FileWriter|FileOutputStream)\s*\("), "file-write"),
    (re.compile(r"\bNbtIo\s*\.\s*write\w*\s*\("), "saved-data-write (NBT file)"),
    # SavedData.setDirty() / PersistentState.markDirty(); a NeoForge block entity's setChanged()
    (re.compile(r"\b(?:setDirty|markDirty|setChanged)\s*\("), "saved-data-write (dirty flag)"),
]
_JVM_SINK_WORDS = re.compile(r"\bFiles\b|FileWriter|FileOutputStream|NbtIo|setDirty|markDirty|setChanged")
# a file without one of these words declares no framework entry point in its text
_JVM_ENTRY_WORDS = re.compile(r"@(?:Mod|Mixin|SubscribeEvent)\b|EventBusSubscriber|Initializer\b|PreLaunchEntrypoint")


def _read(root: Path, rel: str) -> list[str]:
    """Lines of a file, from the index's cache keyed by (size, mtime) - views re-read many files."""
    from verinoda.index import file_lines

    return list(file_lines(Path(root) / rel) or [])


def _files(g: Graph) -> list[str]:
    return sorted({d["source_file"] for _, d in g.G.nodes(data=True) if d.get("source_file")})


def _symbol_at(g: Graph, rel: str, line: int) -> str | None:
    """Innermost symbol whose span contains ``line`` (per-file index in :class:`Graph`)."""
    return g.symbol_at(rel, line)


def _loc(g: Graph, nid: str) -> str:
    f, ln = g.file(nid), g.line(nid)
    return f"{f}:{ln}" if f and ln else (f or nid)


def _edge_loc(d: dict) -> str | None:
    loc = d.get("source_location") or ""
    if d.get("source_file") and loc.startswith("L"):
        return f"{d['source_file']}:{loc[1:]}"
    return d.get("source_file")


# -- 1. hierarchy ---------------------------------------------------------------

def hierarchy(g: Graph, max_symbols: int = 12) -> dict:
    tree: dict = {}
    for f in _files(g):
        parts = PurePosixPath(f).parts
        subsystem = parts[0] if len(parts) > 1 else "(root)"
        package = str(PurePosixPath(*parts[:-1])) if len(parts) > 1 else "."
        syms = g.symbols_in(f)
        entry = tree.setdefault(subsystem, {}).setdefault(package, {})
        entry[f] = {
            "symbols": len(syms),
            "top": [f"{g.label(s)}@L{g.line(s)}" for s in syms[:max_symbols]],
            "kind": "test" if is_test_file(f) else ("doc" if Path(f).suffix in (".md", ".rst", ".txt") else "code"),
        }
    return {
        "view": "hierarchy",
        "coverage": {
            "method": "graph source_file paths; subsystem = first directory; package = directory",
            "limits": ["package = directory, not language-level module/namespace",
                       "symbols are what the AST extractor emitted for the language"],
        },
        "repo": g.root.name,
        "subsystems": {
            s: {"files": sum(len(p) for p in pk.values()), "packages": pk} for s, pk in sorted(tree.items())
        },
    }


# -- 2. dependencies ---------------------------------------------------------------

def dependencies(g: Graph, level: str = "file", limit: int = 60) -> dict:
    agg: Counter = Counter()
    conf: dict = defaultdict(Counter)
    for u, v, d in g.edges({"calls", "imports", "imports_from", "uses", "inherits"}):
        fu, fv = g.file(u), g.file(v)
        if not fu or not fv or fu == fv:
            continue
        if level == "package":
            fu, fv = str(PurePosixPath(fu).parent), str(PurePosixPath(fv).parent)
            if fu == fv:
                continue
        key = (fu, fv, d.get("relation"))
        agg[key] += 1
        conf[key][d.get("confidence", "?")] += 1
    rows = [
        {"from": a, "to": b, "relation": r, "count": c, "confidence": dict(conf[(a, b, r)])}
        for (a, b, r), c in agg.most_common(limit)
    ]
    top = Counter()  # between top-level folders, over every edge (the rows above are cut at `limit`)
    for (a, b, _r), c in agg.items():
        sa, sb = PurePosixPath(a).parts[0], PurePosixPath(b).parts[0]
        if sa != sb:
            top[(sa, sb)] += c
    ext = Counter()
    for u, v, d in g.edges({"imports", "imports_from"}):
        if not g.file(v) and g.file(u):
            ext[g.label(v)] += 1
    return {
        "view": "dependencies",
        "coverage": {
            "method": "AST call/import edges from project_index, aggregated by " + level,
            "limits": ["dynamic dispatch, reflection, DI containers and callbacks are not resolved",
                       "INFERRED edges come from the call-graph second pass and are not verified"],
        },
        "level": level,
        "internal": rows,
        "between_subsystems": [{"from": a, "to": b, "count": c} for (a, b), c in top.most_common(20)],
        "external_imports": dict(ext.most_common(25)),
    }


# -- 3. dataflow -------------------------------------------------------------------

def _sinks(g: Graph) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for f in _files(g):
        if is_test_file(f):
            continue
        lines = _read(g.root, f)
        if not lines:
            continue
        for i, text in enumerate(lines, 1):
            for rx, kind in SINK_PATTERNS:
                if rx.search(text):
                    sym = _symbol_at(g, f, i)
                    if sym:
                        out.setdefault(sym, []).append({"kind": kind, "at": f"{f}:{i}", "line": text.strip()[:120]})
        if f.endswith(JVM_SUFFIXES):
            _jvm_sinks(g, f, lines, out)
    return out


def _jvm_code(lines: list[str]) -> list[str]:
    """Comment- and string-free lines of a Java / Kotlin file (a Java text block is not code either)."""
    from verinoda.index import _java_code_lines

    return _java_code_lines("\n".join(lines), kotlin=True) if lines else []


def _jvm_sinks(g: Graph, f: str, lines: list[str], out: dict[str, list[dict]]) -> None:
    """JVM persistence sinks (:data:`JVM_SINK_PATTERNS`) on the code lines of ``f``, labelled as heuristics."""
    if not _JVM_SINK_WORDS.search("\n".join(lines)):
        return
    for i, text in enumerate(_jvm_code(lines), 1):
        for rx, kind in JVM_SINK_PATTERNS:
            if not rx.search(text):
                continue
            sym = _symbol_at(g, f, i)
            if sym and not any(e["at"] == f"{f}:{i}" and e["kind"] == kind for e in out.get(sym, ())):
                out.setdefault(sym, []).append({"kind": kind, "at": f"{f}:{i}", "line": lines[i - 1].strip()[:120],
                                                "derived_by": "architecture_map.JVM_SINK_PATTERNS (heuristic)"})
            break


# -- JVM framework entry points (heuristics over the code text, labelled) --------------------------------

def _decl_head(code: list[str], line: int | None, name: str, *, until_brace: bool = False) -> tuple[str, int] | None:
    """``(text, declaration line)``: a JVM symbol's annotations and declaration. The declaration is the first
    line from ``line`` that names ``name``; the lines above it back to the end of the previous statement or
    member (``;``, ``{``, ``}``, a blank line) are its annotations. ``until_brace``: on to the ``{`` (a class's
    ``extends`` / ``implements``)."""
    if not line or not name or line > len(code):
        return None
    rx = re.compile(rf"(?<![\w$]){re.escape(name)}(?![\w$])")
    s = next((i for i in range(line, min(line + 6, len(code)) + 1) if rx.search(code[i - 1])), None)
    if s is None:
        return None
    a = s
    while a > 1 and code[a - 2].strip() and not code[a - 2].rstrip().endswith((";", "{", "}")):
        a -= 1
    b = s
    if until_brace:
        while b < min(len(code), s + 6) and "{" not in code[b - 1]:
            b += 1
    return "\n".join(code[a - 1:b]), s


def _fabric_entrypoints(g: Graph, jvm_files: list[str]) -> list[tuple[str, str, str]]:
    """``(entry kind, value, fabric.mod.json path)`` of the ``entrypoints`` of the project's fabric.mod.json
    files (in a source set's resources, ``src/<set>/resources``, or at the root)."""
    import json

    roots = {""}
    for f in jvm_files:
        m = re.match(r"(?:(.*)/)?src/([^/]+)/(?:java|kotlin)/", f)
        if m:
            roots.add(f"{m.group(1) + '/' if m.group(1) else ''}src/{m.group(2)}/resources/")
    out = []
    for r in sorted(roots):
        rel = f"{r}fabric.mod.json"
        try:
            data = json.loads((Path(g.root) / rel).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        eps = data.get("entrypoints") if isinstance(data, dict) else None
        for key, vals in (eps.items() if isinstance(eps, dict) else ()):
            for v in vals if isinstance(vals, list) else [vals]:
                val = v.get("value") if isinstance(v, dict) else v
                if isinstance(val, str) and val.strip():
                    out.append((str(key), val.strip(), rel))
    return out


def framework_entries(g: Graph) -> dict[str, dict]:
    """JVM mod entry points the framework calls: ``{node: {"basis", "why": [...]}}``.

    ``declared``: a class or method listed in fabric.mod.json ``entrypoints``, a class that implements a Fabric
    initializer (its ``onInitialize...`` method), an ``@Mod`` class (its constructor). ``framework``: an
    ``@SubscribeEvent`` method (with its event type), an ``@EventBusSubscriber`` class, a mixin handler
    (``@Inject`` ...), a method registered with an event's ``register`` (``END_SERVER_TICK.register(X::tick)``,
    a ``registers`` edge) or a bus's ``addListener``. ``callback``: a method handed by reference to a known game
    registrar (:data:`CALLBACK_REGISTRAR_RE`: a ticker, a packet handler, a command). Read from the code text:
    heuristics. Test files are left out."""
    jvm = [f for f in _files(g) if f.endswith(JVM_SUFFIXES) and not is_test_file(f)]
    if not jvm:
        return {}
    found: dict[str, dict] = {}

    def add(n: str | None, basis: str, why: str) -> None:
        if n is None or not g.file(n) or is_test_file(g.file(n)):
            return
        e = found.setdefault(n, {"basis": basis, "why": []})
        if ENTRY_TIERS[basis] < ENTRY_TIERS[e["basis"]]:
            e["basis"] = basis
        if why not in e["why"]:
            e["why"].append(why)

    def bare(n: str) -> str:
        return g.label(n).strip().strip(".()")

    classes: dict[str, list[str]] = {f: [n for n in g.symbols_in(f) if g.G.nodes[n].get("_callable_class")]
                                     for f in jvm}

    def members(cid: str) -> list[str]:
        return [v for v, _ in g.out_edges(cid, {"method"})]

    def member(cid: str, name: str) -> str | None:
        return next((v for v in members(cid) if bare(v) == name), None)

    for key, val, rel in _fabric_entrypoints(g, jvm):
        cls_path, _, meth = val.partition("::")
        cls = cls_path.replace("$", ".").rpartition(".")[2]
        path = cls_path.split("$")[0].replace(".", "/")
        for f, cids in classes.items():
            if not f.rsplit(".", 1)[0].endswith(path):
                continue
            for cid in cids:
                if bare(cid) == cls:
                    target = member(cid, meth) if meth else (member(cid, FABRIC_ENTRY_METHODS.get(key, "")) or cid)
                    add(target, "declared", f'fabric.mod.json entrypoint "{key}": {val} ({rel})')
    for f in jvm:
        lines = _read(g.root, f)
        if not classes[f] or not _JVM_ENTRY_WORDS.search("\n".join(lines)):
            continue
        code = _jvm_code(lines)
        for cid in classes[f]:
            hd = _decl_head(code, g.line(cid), bare(cid), until_brace=True)
            head = hd[0] if hd else ""
            ifaces = {g.label(v).rpartition(".")[2] for v, _ in g.out_edges(cid, {"implements", "inherits"})}
            ifaces |= {i for i in FABRIC_INITIALIZERS if re.search(rf"(?:\bimplements\b|:)[^{{]*\b{i}\b", head)}
            for iface in sorted(set(FABRIC_INITIALIZERS) & ifaces):
                meth = FABRIC_INITIALIZERS[iface]
                add(member(cid, meth) or cid, "declared", f"implements {iface}: Fabric calls {meth}() at start")
            if re.search(r"@Mod\b(?!\s*\.)", head):  # not @Mod.EventBusSubscriber
                for n in [v for v in members(cid) if bare(v) == bare(cid)] or [cid]:
                    add(n, "declared", "@Mod class: the mod loader constructs it")
            if re.search(r"@(?:Mod\.)?EventBusSubscriber\b", head):
                add(cid, "framework", "@EventBusSubscriber: its static @SubscribeEvent methods are registered "
                                      "on the event bus")
            mixin = re.search(r"@Mixin\s*\(\s*(?:value\s*=\s*)?\{?\s*([\w.$]+?)(?:\.class|::class)", head)
            for v in members(cid):
                vh = _decl_head(code, g.line(v), bare(v))
                if not vh:
                    continue
                if re.search(r"@SubscribeEvent\b", vh[0]):
                    sig = code[vh[1] - 1]
                    name = re.escape(bare(v))
                    ev = re.search(rf"{name}\s*\(\s*(?:final\s+)?(?:@\w+\s+)*([\w.$]+)(?:<[^()]*?>)?\s+\w+", sig) or \
                        re.search(rf"{name}\s*\(\s*\w+\s*:\s*([\w.]+)", sig)
                    add(v, "framework", "@SubscribeEvent handler" + (f" of {ev.group(1)}" if ev else ""))
                hm = MIXIN_HANDLER_RE.search(vh[0])
                if hm and mixin:
                    raw = "\n".join(lines[max(0, vh[1] - 6):vh[1]])
                    into = re.search(r"method\s*=\s*\{?\s*\"([^\"]+)\"", raw)
                    add(v, "framework", f"mixin @{hm.group(1)} into {mixin.group(1)}"
                                        + (f".{into.group(1)}" if into else ""))
    # methods handed over by reference (verinoda.index.java_registers_edges)
    for u, v, d in g.edges({"registers"}):
        reg = str(d.get("registrar") or "")
        at = _edge_loc(d) or g.file(u) or ""
        if EVENT_REGISTRAR_RE.search(reg):
            add(v, "framework", f"callback registered with {reg}(...) at {at}")
        elif CALLBACK_REGISTRAR_RE.search(reg):
            add(v, "callback", f"callback handed to {reg}(...) at {at}")
    return found


def entry_reasons(g: Graph, n: str) -> list[str]:
    """Why symbol ``n`` looks like an entry point (heuristics: decorator, name, entry module); [] if not."""
    if not g.is_symbol(n):
        return []
    f = g.file(n) or ""
    if is_test_file(f):
        return []
    reasons = []
    name = g.label(n).strip(".()")
    src = g.source(n, max_lines=4)
    if src and ENTRY_DECORATOR_RE.search(src[2]):
        reasons.append("route/command decorator")
    if ENTRY_NAME_RE.search(name):
        reasons.append("entry-like name")
    if ENTRY_FILE_RE.search(f):
        prod_callers = [u for u, _ in g.in_edges(n, {"calls"}) if not is_test_file(g.file(u))]
        if not prod_callers and not name.startswith("_") and not g.G.nodes[n].get("_callable_class"):
            reasons.append("public callable in entry module with no in-project callers")
    return reasons


def entry_points(g: Graph) -> list[dict]:
    """Entry points: JVM framework entries first (:func:`framework_entries`, with their ``basis``: declared,
    framework, callback), then the name / decorator / module heuristics (:func:`entry_reasons`)."""
    fw = framework_entries(g)
    found = []
    for n in g.G.nodes:
        reasons = entry_reasons(g, n)
        if n in fw:
            found.append({"id": n, "symbol": g.label(n), "at": _loc(g, n), "why": fw[n]["why"] + reasons,
                          "basis": fw[n]["basis"]})
        elif reasons:
            found.append({"id": n, "symbol": g.label(n), "at": _loc(g, n), "why": reasons})
    return sorted(found, key=lambda e: (ENTRY_TIERS.get(e.get("basis"), 3), e["at"]))


ASIDE = "copy or reference tree"


def _aside_roots(g: Graph) -> tuple[str, ...]:
    """Path prefixes of the detected copies and configured reference trees (:func:`verinoda.copies.roots_not_named`)."""
    from verinoda import copies

    try:
        return copies.roots_not_named(g.root, set(), "")
    except Exception:  # noqa: BLE001 - no config, no detection: nothing is set aside
        return ()


def _render_path(g: Graph, p: list[str], sinks: dict, roots: tuple[str, ...] = ()) -> dict:
    hops = []
    for a, b in zip(p, p[1:]):
        ds = g.G.get_edge_data(a, b) or {}
        d = next(iter(ds.values()), {}) if ds else {}
        hops.append({"from": g.label(a), "to": g.label(b), "from_id": a, "to_id": b, "relation": d.get("relation"),
                     "confidence": d.get("confidence"), "at": _edge_loc(d),
                     **({"derived_by": d["_origin"]} if str(d.get("_origin", "")).startswith("verinoda") else {})})
    entry = _loc(g, p[0])
    return {"entry": entry, "sink": _loc(g, p[-1]), "sink_kinds": sorted({s["kind"] for s in sinks[p[-1]]}),
            "sink_lines": [s["at"] for s in sinks[p[-1]]][:4], "hops": hops,
            **({"in": ASIDE} if roots and (entry or "").startswith(roots) else {})}


def _cached(g: Graph, key: str, fn):
    cache = g.__dict__.setdefault("_am_cache", {})
    if key not in cache:
        cache[key] = fn(g)
    return cache[key]


def paths_through(g: Graph, nodes: list[str], max_depth: int = 5, max_paths: int = 3) -> list[dict]:
    """Entry -> ... -> node -> ... -> sink paths through the given nodes, in their order: from each node the nearest
    persistence sink over call edges, and back from it the nearest entry point or uncalled caller (none within
    ``max_depth``: the path starts at the node). :func:`dataflow` caps its paths for the whole repository; in a large one the code a question is
    about is often on none of them."""
    sinks = _cached(g, "sinks", _sinks)
    entries = {e["id"] for e in _cached(g, "entries", entry_points)}
    out: list[dict] = []
    seen_paths: set[tuple[str, ...]] = set()
    expanded: list[str] = []
    for n in nodes:  # a class stands for its methods (a SavedData class writes in one of them)
        expanded.append(n)
        if n in g.G and g.G.nodes[n].get("_callable_class"):
            expanded += sorted((v for v, _ in g.out_edges(n, {"method"})), key=lambda v: g.line(v) or 0)[:12]
    for n in dict.fromkeys(expanded):
        if n not in g.G or len(out) >= max_paths:
            continue
        fwd = _bfs(g, n, lambda c: [v for v, _ in g.out_edges(c, FLOW_RELATIONS)], lambda c: c in sinks, max_depth)
        if not fwd:
            continue
        def callers(c: str) -> list[str]:
            return [u for u, _ in g.in_edges(c, FLOW_RELATIONS) if not is_test_file(g.file(u) or "")]

        # back to an entry point, or to the first caller nothing in the project calls (the framework does:
        # a game's tick override, a handler the index cannot see registered)
        back = _bfs(g, n, callers, lambda c: c in entries or not callers(c), max_depth)
        path = (list(reversed(back)) if back else [n]) + fwd[1:]
        if (len(path) < 2 and path[0] not in entries) or tuple(path) in seen_paths:  # "X writes at X" says nothing
            continue
        seen_paths.add(tuple(path))
        out.append(_render_path(g, path, sinks))
    return out


def _bfs(g: Graph, start: str, step, goal, max_depth: int) -> list[str] | None:
    """The shortest path from ``start`` to a node ``goal`` accepts (``start`` itself included)."""
    q = deque([[start]])
    seen = {start}
    while q:
        path = q.popleft()
        if goal(path[-1]):
            return path
        if len(path) > max_depth:
            continue
        for v in sorted(set(step(path[-1]))):
            if v not in seen and g.file(v):
                seen.add(v)
                q.append(path + [v])
    return None


def dataflow(g: Graph, max_depth: int = 6, max_paths: int = 20) -> dict:
    """Entry points -> call paths -> persistence sinks. The project's own entry points come first: one in a
    detected copy of the project or a configured reference tree is listed after them (``"in"``) and its
    paths only fill what ``max_paths`` leaves (a frozen copy never crowds out the project's own flows)."""
    sinks = _cached(g, "sinks", _sinks)
    entries = _cached(g, "entries", entry_points)
    roots = _aside_roots(g)
    if roots:
        entries = ([e for e in entries if not e["at"].startswith(roots)]
                   + [{**e, "in": ASIDE} for e in entries if e["at"].startswith(roots)])
    paths = []
    for e in entries:
        # BFS over call-ish edges; also step from a method to its class and
        # from a class construction to its __init__ (both are real edges).
        q = deque([[e["id"]]])
        seen = {e["id"]}
        while q and len(paths) < max_paths:
            path = q.popleft()
            cur = path[-1]
            if cur in sinks and len(path) > 1 or (cur in sinks and cur == e["id"]):
                paths.append(path)
                continue
            if len(path) > max_depth:
                continue
            nxt = [v for v, _ in g.out_edges(cur, FLOW_RELATIONS)]
            # Constructing a class runs its __init__ - the only class->method
            # step that is a real control-flow edge.
            if g.G.nodes[cur].get("_callable_class"):
                nxt += [v for v, _ in g.out_edges(cur, {"method"}) if g.label(v).strip(".()") == "__init__"]
            for v in sorted(set(nxt)):
                if v not in seen and g.file(v):
                    seen.add(v)
                    q.append(path + [v])
    rendered = [_render_path(g, p, sinks, roots) for p in paths]
    limits = ["paths follow call edges only; values are not tracked (no taint analysis)",
              "Python param.method() calls are resolved from type annotations/local constructors "
              "(edges marked derived_by=verinoda.receiver, INFERRED)",
              "entry-point and sink detection are heuristics and are labelled as such",
              "framework routing tables and async queues are not followed"]
    if roots:
        limits.append("entry points in detected copies or reference trees (" + ", ".join(roots[:3])
                      + ") come after the project's own, marked \"in\"")
    if any(e.get("basis") for e in entries) or any(x.get("derived_by") for v in sinks.values() for x in v):
        limits.append("JVM mods: framework entry points (fabric.mod.json entrypoints, Fabric initializers, @Mod, "
                      "@EventBusSubscriber / @SubscribeEvent, mixin handlers, methods registered by reference; each "
                      "says why and its 'basis') and JVM sinks (file writes, NbtIo, setDirty / markDirty / "
                      "setChanged) are read from the code text")
    return {
        "view": "dataflow",
        "coverage": {
            "method": "entry points (decorators/names/entry modules, heuristic) -> AST call edges -> "
                      "persistence sinks (regex over symbol bodies: SQL, db connect, ORM, file writes)",
            "limits": limits,
        },
        "entries": entries,
        "sinks": [{"symbol": g.label(s), "at": _loc(g, s), "evidence": v[:4]} for s, v in sorted(sinks.items())],
        "paths": rendered,
    }


# -- 4. config ---------------------------------------------------------------------

def config(g: Graph) -> dict:
    env: dict[str, list[dict]] = defaultdict(list)
    for f in _files(g):
        for i, text in enumerate(_read(g.root, f), 1):
            for rx, lang in ENV_PATTERNS:
                for m in rx.finditer(text):
                    sym = _symbol_at(g, f, i)
                    env[m.group(1)].append({"at": f"{f}:{i}", "symbol": g.label(sym) if sym else "(module)",
                                            "line": text.strip()[:120]})
    # config files are often data, not graph nodes (a .yml or .toml has no symbols): take them from the file list
    from verinoda.snapshot import list_files

    known = set(_files(g))
    try:
        known |= set(list_files(g.root)) if g.root and Path(g.root).is_dir() else set()
    except OSError:
        pass
    cfg_files = sorted(f for f in known if CONFIG_FILE_RE.search(f))
    # modules that import a config-ish module
    readers = defaultdict(set)
    for u, v, d in g.edges({"imports", "imports_from"}):
        tv = g.file(v) or ""
        if tv and CONFIG_FILE_RE.search(tv) and g.file(u) and g.file(u) != tv:
            readers[tv].add(g.file(u))
    return {
        "view": "config",
        "coverage": {
            "method": "regex scan for environment reads (Python/JS/Go/Rust/Java idioms) + config-like files "
                      "+ import edges into config modules",
            "limits": ["indirect reads (settings objects, dotenv loaders, framework config) are not traced",
                       "values are not resolved; only names and read sites"],
        },
        "env_vars": {k: v for k, v in sorted(env.items())},
        "config_files": cfg_files,
        "config_importers": {k: sorted(v) for k, v in readers.items()},
    }


# A read of a setting by a string key: `Config.getInt("car.door-ticks", 140)`, `cfg.get("server.port")`. The
# key has two or more dotted parts; a second argument is the default. Calls that take such a string for another
# reason (a translation key, a resource id, a log line, a format) are left out by name.
KEY_READ_RE = re.compile(r"\b([A-Za-z_]\w*)\s*\(\s*\"([A-Za-z][\w-]*(?:\.[\w-]+)+)\"\s*(?:,\s*([^()\"]{1,40}?|\"[^\"]{0,40}\"))?\s*\)")
NOT_KEY_READS = frozenset("translatable literal id of parse resource getResource getResourceAsStream format printf "
                          "info warn error debug trace log equals equalsIgnoreCase startsWith endsWith contains "
                          "matches split replace replaceAll indexOf forName loadClass require import_module "
                          "getLogger getMessage t tr i18n _ gettext".split())
CONFIG_DATA_SUFFIXES = (".yml", ".yaml", ".toml", ".properties", ".cfg", ".ini", ".conf")


def config_key_reads(g: Graph, files: list[str]) -> list[dict]:
    """String-keyed setting reads in ``files``: ``{"key", "method", "default", "at", "symbol", "line"}``."""
    out = []
    for f in files:
        if Path(f).suffix.lower() in CONFIG_DATA_SUFFIXES or f.endswith((".md", ".json")):
            continue
        for i, text in enumerate(_read(g.root, f), 1):
            if "\"" not in text or text.lstrip().startswith(("//", "#", "*", "/*")):
                continue
            for m in KEY_READ_RE.finditer(text):
                if m.group(1) in NOT_KEY_READS:
                    continue
                sym = _symbol_at(g, f, i)
                out.append({"key": m.group(2), "method": m.group(1), "default": (m.group(3) or "").strip() or None,
                            "at": f"{f}:{i}", "symbol": g.label(sym) if sym else "(module)",
                            "line": text.strip()[:140]})
    return out


def config_key_definitions(g: Graph) -> dict[str, str]:
    """Dotted key -> ``file:line`` where a configuration file of the repository defines it (YAML nesting is
    followed: ``car:`` then ``  door-ticks: 140`` defines ``car.door-ticks``; TOML ``[section]`` and flat
    ``a.b = 1`` lines too)."""
    from verinoda.snapshot import listed_files

    try:
        files = [f for f in listed_files(g.root) if f.lower().endswith(CONFIG_DATA_SUFFIXES)]
    except OSError:
        files = []
    out: dict[str, str] = {}
    for f in files[:200]:
        stack: list[tuple[int, str]] = []
        section = ""
        yaml = f.lower().endswith((".yml", ".yaml"))
        for i, text in enumerate(_read(g.root, f), 1):
            if not text.strip() or text.lstrip().startswith(("#", ";", "//")):
                continue
            if yaml:
                m = re.match(r"^(\s*)(?:- )?[\"']?([\w.-]+)[\"']?\s*:(?:\s|$)", text)
                if not m:
                    continue
                ind = len(m.group(1))
                while stack and stack[-1][0] >= ind:
                    stack.pop()
                stack.append((ind, m.group(2)))
                out.setdefault(".".join(k for _, k in stack), f"{f}:{i}")
            else:
                m = re.match(r"^\s*\[([\w.-]+)\]\s*$", text)
                if m:
                    section = m.group(1)
                    continue
                m = re.match(r"^\s*[\"']?([\w.-]+)[\"']?\s*[=:]", text)
                if m:
                    out.setdefault(f"{section}.{m.group(1)}" if section else m.group(1), f"{f}:{i}")
    return out


# -- 5. tests ----------------------------------------------------------------------

REACH_RELATIONS = testcode.REACH_RELATIONS


def test_reach(g: Graph, depth: int = 3) -> dict[str, set[str]]:
    """Product symbol -> ids of the tests that statically reach it (:func:`verinoda.testcode.reach`: from each
    test over call/use/reference edges and the calls the graph does not hold, ``depth`` steps, only through code
    that is not test code)."""
    return testcode.reach(g, depth)["covers"]


def tests_view(g: Graph, depth: int = 3) -> dict:
    units = testcode.test_units(g)
    name_of = {u.id: u.name for u in units}
    covers = test_reach(g, depth)
    prod = [n for n in g.G.nodes if g.is_symbol(n) and not testcode.is_test_code(g, n)]
    untested = [_loc(g, n) + f" {g.label(n)}" for n in prod if n not in covers]
    runtime = _coverage_xml(g.root)
    silent = testcode.files_without_tests(g)
    limits = ["static reachability is not runtime coverage; mocks are invisible, and so are fixtures other than the "
              "pytest fixtures a test requests (a pytest test reaches what those call)",
              ("a JS/TS it()/test() reaches the symbols its body names from the project files its file imports (a "
               "name match, not a resolved call)"),
              "run `verinoda verify` with experiments or provide coverage.xml for runtime evidence"]
    if silent:
        limits.insert(0, f"{len(silent)} test file(s) hold no recognised test (helpers, or tests of a framework "
                         "verinoda.testcode does not know, such as Kotest, Spock or ScalaTest): not_reached_by_tests "
                         "means not reached by a recognised test")
    return {
        "view": "tests",
        "coverage": {
            "method": f"static reachability from tests over call edges (depth {depth}); tests are recognised by "
                      "verinoda.testcode: pytest-named functions, Go Test/Benchmark functions, @Test/"
                      "@ParameterizedTest/@GameTest methods (JUnit, TestNG, Minecraft game tests), [Test]/[Fact] "
                      "methods, Rust #[test] functions, and the it()/test() calls of JS/TS test files",
            "limits": limits,
            "runtime_coverage_file": runtime.get("file"),
        },
        "tests": len(units),
        "tests_by_language": dict(sorted(Counter(u.lang for u in units).items())),
        "test_files_without_recognised_tests": {"count": len(silent), "files": silent[:10]},
        "covered": {g.label(n) + f" ({_loc(g, n)})": sorted({name_of.get(t, t) for t in ts})[:6]
                    for n, ts in sorted(covers.items(), key=lambda kv: _loc(g, kv[0]))},
        "not_reached_by_tests": untested[:50],
        "runtime": runtime,
    }


def _coverage_xml(root: Path) -> dict:
    for cand in ("coverage.xml", "reports/coverage.xml", "build/coverage.xml"):
        p = root / cand
        if p.exists():
            try:
                import xml.etree.ElementTree as ET

                tree = ET.parse(p)
                files = {}
                for cls in tree.iter("class"):
                    fn = cls.get("filename")
                    if fn:
                        files[fn] = float(cls.get("line-rate", 0))
                return {"file": cand, "line_rate_by_file": files}
            except Exception as exc:  # malformed report
                return {"file": cand, "error": str(exc)}
    return {}


# -- 6. history & decisions -----------------------------------------------------------

def history(g: Graph, n_commits: int = 30) -> dict:
    root = g.root
    log = git(root, "log", f"-n{n_commits}", "--name-only", "--format=@@%H%x1f%an%x1f%aI%x1f%s")
    commits = []
    churn: Counter = Counter()
    if log:
        cur = None
        for line in log.splitlines():
            if line.startswith("@@"):
                sha, author, date, subj = line[2:].split("\x1f", 3)
                cur = {"sha": sha, "author": author, "date": date, "subject": subj, "files": []}
                commits.append(cur)
            elif line.strip() and cur is not None:
                cur["files"].append(line.strip())
                churn[line.strip()] += 1
    decisions = []
    code_files = [f for f in _files(g) if Path(f).suffix not in (".md", ".rst", ".txt")]
    labels: set[str] | None = None  # symbol names, read once (a large graph has tens of thousands)
    for f in _files(g):
        if not DOC_DECISION_RE.search(f):
            continue
        text = "\n".join(_read(root, f))
        mentions = sorted({cf for cf in code_files if cf in text or Path(cf).stem in text.split("`")})
        if labels is None:
            labels = {lab for n in g.G.nodes if g.is_symbol(n) and len(lab := g.label(n).strip(".()")) > 3}
        # a name is mentioned when the text has it as a whole word: one word set per document, not a regex
        # search of the whole document per symbol (90 s on a 30k-node graph with a long design document)
        words = set(re.findall(r"\w+", text))
        syms = sorted(lab for lab in labels if (lab in words if re.fullmatch(r"\w+", lab)
                                                 else re.search(rf"\b{re.escape(lab)}\b", text)))
        status = re.search(r"^\s*Status:\s*(\w+)", text, re.M | re.I)
        decisions.append({"doc": f, "status": status.group(1) if status else None,
                          "mentions_files": mentions[:20], "mentions_symbols": syms[:20]})
    decision_commits = [c for c in commits if re.search(r"\b(adr|decision|decide|design|rfc)\b", c["subject"], re.I)
                        or any(DOC_DECISION_RE.search(f) for f in c["files"])]
    return {
        "view": "history",
        "coverage": {
            "method": "git log (last %d commits) + decision docs (adr/decisions/rfcs dirs, ARCHITECTURE/DESIGN.md) "
                      "linked to code by literal file/symbol mentions" % n_commits,
            "limits": ["links are textual mentions, not semantic understanding",
                       "no git repository -> no commit history" if not log else "only the last N commits"],
        },
        "is_git": log is not None,
        "recent_commits": [{k: c[k] for k in ("sha", "date", "subject")} | {"files": c["files"][:10]}
                           for c in commits[:15]],
        "churn": dict(churn.most_common(15)),
        "decisions": decisions,
        "decision_commits": [{"sha": c["sha"][:12], "subject": c["subject"]} for c in decision_commits[:10]],
    }


# -- 7. impact ----------------------------------------------------------------------------

def changed_files_from_git(root: Path, base: str | None = None) -> list[str]:
    args = ["diff", "--name-only", base] if base else ["diff", "--name-only", "HEAD"]
    out = git(root, *args) or ""
    untracked = git(root, "ls-files", "--others", "--exclude-standard") or ""
    return sorted({l.strip() for l in (out + "\n" + untracked).splitlines() if l.strip()})


CALLBACK_VIA = "registers (callback)"


def callback_dependents(g: Graph, dist: dict[str, int], rels: set[str], depth: int) -> set[str]:
    """Extend a reverse walk ``dist`` (node -> distance, filled over ``rels``) in place with what reaches it
    through a callback registration (``registers`` edges: the method that hands a changed method over, and
    what depends on that). The nodes found before keep their distance; returns the nodes added."""
    added: set[str] = set()
    from verinoda.index import has_registers

    if not has_registers(g):
        return added
    heap = [(d, n) for n, d in dist.items()]
    heapq.heapify(heap)
    while heap:
        dd, n = heapq.heappop(heap)
        if dd != dist.get(n) or dd >= depth:
            continue
        for u, _ in g.in_edges(n, rels | {"registers"}):
            if u not in dist:
                dist[u] = dd + 1
                added.add(u)
                heapq.heappush(heap, (dd + 1, u))
    return added


def _linked_files(g: Graph, files: set[str]) -> dict[str, set[str]]:
    """For each of ``files``, the other files a graph edge of any relation joins it to, in either direction."""
    out: dict[str, set[str]] = {f: set() for f in files}
    for u, v in g.G.edges():
        fu, fv = g.file(u), g.file(v)
        if fu and fv and fu != fv:
            if fu in out:
                out[fu].add(fv)
            if fv in out:
                out[fv].add(fu)
    return out


def impact(g: Graph, targets: list[str], depth: int = 4, *, stale=(), co_change: bool = True) -> dict:
    """Reverse reachability: who depends (calls/imports/uses/inherits) on the targets.

    A target is a file of the graph (all its nodes) or a name resolved exactly
    (:func:`verinoda.naming.resolve`; a detected copy gives way to the project's own code). A target
    that names several symbols, that names nothing, or that only a file changed since the index
    (``stale``) spells is ``unresolved``, with its candidates in ``resolution``: impact is never
    computed for a merely similar name.

    With ``co_change``, the files that changed together with the targets' files in git and that no graph edge
    links to them (nor the walk reached) are listed as ``history_coupled`` claims (``strong_inference``, with
    their commit counts; :func:`verinoda.history.co_changes`). The files read are those of the resolved targets
    and any target that is a file of the working tree the graph does not hold (a document, a config file): such a
    target is listed in ``history_only_targets``, not in ``unresolved``, since it names a file, not a symbol."""
    from verinoda import naming

    seeds: set[str] = set()
    unresolved = []
    resolution = []
    plain_files: set[str] = set()  # targets that are files of the working tree without a graph node
    stale = list(stale or ())
    changed = {str(f).replace("\\", "/") for f in stale}
    for t in targets:
        hit = [n for n in g.G.nodes if g.file(n) == t]
        if hit:
            seeds.update(hit)
            continue
        r = naming.resolve(g, t, stale=stale)
        if not r.exact and co_change and "::" not in t and (Path(g.root) / t).is_file():
            plain_files.add(t.replace("\\", "/"))  # read in git; a file, not a symbol, so not unresolved ...
            if t.replace("\\", "/") not in changed:
                continue  # ... unless it changed since the index, whose graph may just lack it
        if r.exact:
            f = g.file(r.node)
            seeds.add(r.node)
            if f and g.is_file_node(r.node):  # `orders/api` names the file: every node of it, as above
                seeds.update(n for n in g.G.nodes if g.file(n) == f)
        # what every name target resolved to (or why not), so a wrong pick is visible
        resolution.append(r.as_dict(g))
        if not r.exact:
            unresolved.append(t)
    rel = {"calls", "imports", "imports_from", "uses", "inherits", "method", "references"}
    dist = {s: 0 for s in seeds}
    q = deque(seeds)
    while q:
        n = q.popleft()
        if dist[n] >= depth:
            continue
        for u, _ in g.in_edges(n, rel):
            if u not in dist:
                dist[u] = dist[n] + 1
                q.append(u)
    by_callback = callback_dependents(g, dist, rel, depth)
    affected_files = Counter()
    tests = set()
    for n, dd in dist.items():
        f = g.file(n)
        if not f or dd == 0:
            continue
        affected_files[f] += 1
        if is_test_file(f):
            tests.add(f)
    # tests that reach an affected symbol through a call the graph does not hold (pkg.main.run(), a pytest
    # fixture, a JS it()): listed with what they reach and how, since the graph walk above does not show them
    callers = testcode.extra_callers(g)
    basis: dict[str, dict] = {}
    for n, dd in sorted(dist.items(), key=lambda kv: (kv[1], _loc(g, kv[0]))):
        if dd >= depth:
            continue
        for u in callers.get(n, ()):
            tests.add(u.file)
            if u.id not in basis and len(basis) < 40:
                what = "it" if dd == 0 else f"`{g.label(n)}` (which reaches the target)"
                basis[u.id] = {"test": u.id, "at": u.at, "reaches": g.label(n), "reaches_at": _loc(g, n),
                               "distance": dd + 1, "basis": testcode.reach_basis(testcode.extra_kind(g, u.id, n),
                                                                                 what)}
    out_basis = {"tests_basis": list(basis.values())} if basis else {}
    from verinoda import gametests

    gt = gametests.for_change(g, seeds) if seeds else None  # Minecraft GameTests: registered ones, nearest first
    coupling: dict = {}
    if co_change:
        from verinoda import history as hist

        target_files = {f for s in seeds if (f := g.file(s))} | plain_files
        co = hist.co_changes(g.root, sorted(target_files),
                             linked=_linked_files(g, target_files), skip=set(affected_files))
        coupling = {"history_coupled": co["coupled"],
                    "history_coupling": {k: co[k] for k in ("is_git", "commits_read", "bulk_skipped")}
                    | {"method": co["coverage"]["method"], "limits": co["coverage"]["limits"]}
                    | {k: co[k] for k in ("truncated", "shallow", "error") if co.get(k)},
                    **({"history_only_targets": sorted(plain_files)} if plain_files else {})}
    return {
        "view": "impact",
        "coverage": {
            "method": f"reverse traversal of call/import/use/inherit edges up to depth {depth}"
                      + ("; then callback registrations (a method handed over by reference: 'registers', "
                         "INFERRED), marked via" if by_callback else ""),
            "limits": ["dynamic/reflective dependents are missed", "a dependent is 'possibly affected', "
                       "not proven broken"],
        },
        "targets": targets,
        "unresolved": unresolved,
        **({"resolution": resolution} if resolution else {}),
        "affected_symbols": sorted(
            ({"symbol": g.label(n), "at": _loc(g, n), "distance": dd,
              **({"via": CALLBACK_VIA} if n in by_callback else {})} for n, dd in dist.items() if dd > 0),
            key=lambda x: (x["distance"], x["at"]))[:80],
        "affected_files": dict(affected_files.most_common(40)),
        "tests_to_run": sorted(tests),
        **out_basis,
        **({"gametests": gt} if gt else {}),
        **coupling,
    }


def dead(g: Graph) -> dict:
    from verinoda import deadcode

    return deadcode.dead_code(g)


def hotspots(g: Graph) -> dict:
    from verinoda import hotspots as hs

    return hs.hotspots(g)


def sides(g: Graph) -> dict:
    from verinoda import sides as sd

    return sd.sides(g)


# -- 8. cycles ------------------------------------------------------------------------------

CYCLE_RELATIONS = {"calls", "imports", "imports_from", "uses", "inherits"}   # the dependencies view's edges
EXACT_BREAK_MAX = 12      # up to this many files the smallest break set is searched exactly (2^n file orderings)
CYCLE_EDGE_SITES = 3      # reference lines listed per file-to-file dependency
CYCLE_EDGES_SHOWN = 60    # file-to-file dependencies listed per cycle
CYCLES_SHOWN = 50         # cycles listed; all are counted
CYCLE_FILES_SHOWN = 100   # files listed per cycle
CYCLE_CUTS_SHOWN = 60     # cuts listed per cycle, each with a ring it closes; all are counted
CYCLE_RING_SHOWN = 20     # steps of a ring listed
LEFT_OUT_SHOWN = 20       # dependencies left out (standard library imports, copies) listed with their lines
GREEDY_REDUCE_STEPS = 2_000_000   # edges visited while putting cuts back in large cycles; past it the rest stay cut
_PY_SUFFIXES = (".py", ".pyi")
_PY_STDLIB = frozenset(getattr(sys, "stdlib_module_names", ())) | frozenset(sys.builtin_module_names)
_PY_IMPORT_RE = re.compile(r"^\s*import\s+([\w.]+(?:\s+as\s+\w+)?(?:\s*,\s*[\w.]+(?:\s+as\s+\w+)?)*)")
_PY_FROM_RE = re.compile(r"^\s*from\s+(\.*[\w.]*)\s+import\b")


def _py_top_names(g: Graph) -> set[str]:
    """The top-level names the project's Python files are importable by: from the repository root or ``src/``."""
    top: set[str] = set()
    for rel in _files(g):
        if rel.endswith(_PY_SUFFIXES):
            parts = PurePosixPath(rel).with_suffix("").parts
            top.add(parts[0])
            if parts[0] == "src" and len(parts) > 1:
                top.add(parts[1])
    return top


def _imports_stdlib(line: str, top: set[str]) -> bool:
    """A Python import line whose modules are all the standard library's: absolute, and no top-level name of the
    project shadows them. ``import html`` in ``pkg/security.py`` never loads ``pkg/exporters/html.py``."""
    m = _PY_IMPORT_RE.match(line)
    if m:
        heads = [x.split()[0].split(".")[0] for x in m.group(1).split(",")]
    elif (m := _PY_FROM_RE.match(line)) and not m.group(1).startswith("."):
        heads = [m.group(1).split(".")[0]]
    else:
        return False
    return all(h in _PY_STDLIB and h not in top for h in heads)


def _sites(d: dict) -> list[str]:
    return [at for _, at in sorted(d["sites"])]


def _file_deps(g: Graph) -> tuple[dict[tuple[str, str], dict], dict[tuple[str, str], dict]]:
    """File -> file dependencies with the references behind them, and apart from them the Python imports whose
    line imports the standard library (the graph gave the name to a project module of the same name). A
    type-only import (``import type``) and a deferred ``import(...)`` are left out: neither closes a cycle when
    the code loads. Prose files are no code."""
    from verinoda.index import PROSE_SUFFIXES

    deps: dict[tuple[str, str], dict] = {}
    stdlib: dict[tuple[str, str], dict] = {}
    top: set[str] | None = None
    for u, v, d in g.edges(CYCLE_RELATIONS):
        if d.get("type_only") or d.get("deferred"):
            continue
        fu, fv = g.file(u), g.file(v)
        if not fu or not fv or fu == fv or fu.lower().endswith(PROSE_SUFFIXES) or fv.lower().endswith(PROSE_SUFFIXES):
            continue
        at = _edge_loc(d)
        into = deps
        ln = (d.get("source_location") or "")[1:]
        if d.get("relation") in ("imports", "imports_from") and fu.endswith(_PY_SUFFIXES) and ln.isdigit():
            lines = _read(g.root, fu)
            if 0 < int(ln) <= len(lines):
                top = _py_top_names(g) if top is None else top
                if _imports_stdlib(lines[int(ln) - 1], top):
                    into = stdlib
        e = into.setdefault((fu, fv), {"references": 0, "relations": Counter(), "extracted": 0, "sites": set()})
        e["references"] += 1
        e["relations"][d.get("relation")] += 1
        extracted = d.get("confidence") == "EXTRACTED"
        e["extracted"] += extracted
        if at:
            e["sites"].add((not extracted, at))
    return deps, stdlib


def _exact_break(nodes: list[str], cost: dict[tuple[str, str], tuple[int, int]]) -> list[tuple[str, str]]:
    """The cheapest feedback edge set of a small component: over every ordering of the files (dynamic
    programming over subsets), the edges that point back against the order; cost = (edges, references)."""
    n = len(nodes)
    idx = {x: i for i, x in enumerate(nodes)}
    outs: list[list[tuple[int, tuple[int, int]]]] = [[] for _ in nodes]
    for (a, b), w in cost.items():
        outs[idx[a]].append((idx[b], w))
    best: list[tuple[int, int] | None] = [None] * (1 << n)
    last = [0] * (1 << n)
    best[0] = (0, 0)
    for mask in range(1 << n):
        c = best[mask]
        if c is None:
            continue
        for v in range(n):
            bit = 1 << v
            if mask & bit:
                continue
            k, r = c
            for j, (wk, wr) in outs[v]:   # v placed after the files in mask: its edges into them point back
                if mask >> j & 1:
                    k, r = k + wk, r + wr
            nm = mask | bit
            if best[nm] is None or (k, r) < best[nm]:
                best[nm], last[nm] = (k, r), v
    order, mask = [], (1 << n) - 1
    while mask:
        order.append(last[mask])
        mask ^= 1 << last[mask]
    pos = {nodes[v]: i for i, v in enumerate(reversed(order))}
    return sorted(e for e in cost if pos[e[0]] > pos[e[1]])


def _greedy_break(nodes: list[str], cost: dict[tuple[str, str], tuple[int, int]],
                  budget: list[int] | None = None) -> list[tuple[str, str]]:
    """A small feedback edge set of a large component: the Eades-Lin-Smyth ordering (sinks to the end, sources
    to the front, else the file with most outgoing minus incoming edges next), then every cut edge that closes
    no cycle with what is kept is put back, the heaviest first. An upper bound, not proven the smallest.

    ``budget`` (``[edges]``, shared across calls) bounds the searches of the put-back: when it runs out, the
    cuts not yet tried stay cut (still a break set) and ``budget[0]`` is set to -1."""
    succ: dict[str, set[str]] = defaultdict(set)
    pred: dict[str, set[str]] = defaultdict(set)
    for a, b in cost:
        succ[a].add(b)
        pred[b].add(a)
    alive = set(nodes)
    outd = {x: len(succ[x]) for x in nodes}
    ind = {x: len(pred[x]) for x in nodes}
    left: list[str] = []
    right: list[str] = []
    ends = {x for x in nodes if outd[x] == 0 or ind[x] == 0}
    heap = [(ind[x] - outd[x], x) for x in nodes]   # most outgoing minus incoming first, then by name
    heapq.heapify(heap)

    def remove(x: str) -> None:
        alive.discard(x)
        for y, deg in [*((y, ind) for y in succ[x]), *((y, outd) for y in pred[x])]:
            if y in alive:
                deg[y] -= 1
                heapq.heappush(heap, (ind[y] - outd[y], y))   # the older entry is stale and skipped
                if deg[y] == 0:
                    ends.add(y)

    while alive:
        if ends:
            batch = sorted(ends)
            ends.clear()
            for x in batch:
                if x in alive:
                    (right if outd[x] == 0 else left).append(x)
                    remove(x)
            continue
        k, x = heapq.heappop(heap)
        if x in alive and k == ind[x] - outd[x]:
            left.append(x)
            remove(x)
    pos = {x: i for i, x in enumerate(left + right[::-1])}
    cut = [e for e in cost if pos[e[0]] > pos[e[1]]]
    kept: dict[str, set[str]] = defaultdict(set)
    for a, b in cost:
        if pos[a] < pos[b]:
            kept[a].add(b)
    final = []
    for a, b in sorted(cut, key=lambda e: (-cost[e][1], e)):
        if budget is not None and budget[0] <= 0:
            budget[0] = -1
            final.append((a, b))
        elif _reaches(kept, b, a, budget):
            final.append((a, b))
        else:
            kept[a].add(b)
    if budget is not None and budget[0] <= 0:   # the last search may have stopped short too
        budget[0] = -1
    return sorted(final)


def _reaches(succ: dict[str, set[str]], start: str, goal: str, budget: list[int] | None = None) -> bool:
    """Whether ``goal`` is reachable from ``start``; True too when ``budget`` (edges to visit) runs out first."""
    seen, stack = {start}, [start]
    while stack:
        x = stack.pop()
        if x == goal:
            return True
        nxt = succ.get(x, ())
        if budget is not None:
            budget[0] -= len(nxt)
            if budget[0] <= 0:
                return True
        for y in nxt:
            if y not in seen:
                seen.add(y)
                stack.append(y)
    return False


def _shortest_back(succ: dict[str, set[str]], start: str, goal: str) -> list[str] | None:
    """The shortest path start -> goal (breadth first, files in name order)."""
    prev: dict[str, str | None] = {start: None}
    q = deque([start])
    while q:
        x = q.popleft()
        if x == goal:
            path = [x]
            while prev[path[-1]] is not None:
                path.append(prev[path[-1]])
            return path[::-1]
        for y in sorted(succ.get(x, ())):
            if y not in prev:
                prev[y] = x
                q.append(y)
    return None


def _cycle_row(files: list[str], inner: dict[tuple[str, str], dict], cut: list[tuple[str, str]], method: str,
               aside: bool) -> tuple[dict, bool]:
    """One listed cycle: its status, the break set with the ring each cut closes, the heaviest dependencies;
    and whether any of its lists was cut."""
    succ: dict[str, set[str]] = defaultdict(set)
    succ_x: dict[str, set[str]] = defaultdict(set)   # the parser's own edges only
    for (a, b), d in inner.items():
        succ[a].add(b)
        if d["extracted"]:
            succ_x[a].add(b)
    xg = nx.DiGraph()
    xg.add_nodes_from(files)
    xg.add_edges_from((a, b) for (a, b), d in inner.items() if d["extracted"])
    strong = nx.is_strongly_connected(xg)

    def row(e: tuple[str, str]) -> dict:
        d = inner[e]
        return {"from": e[0], "to": e[1], "references": d["references"],
                "relations": dict(sorted(d["relations"].items())), "extracted": d["extracted"],
                "at": _sites(d)[:CYCLE_EDGE_SITES]}

    breaks, long_ring = [], False
    for a, b in cut[:CYCLE_CUTS_SHOWN]:
        back = _shortest_back(succ_x, b, a) if inner[(a, b)]["extracted"] else None
        how = "strong_inference" if back else "weak_inference"
        back = back or _shortest_back(succ, b, a) or [b, a]
        loop = [a, *back]
        shown = loop[:CYCLE_RING_SHOWN + 1]
        long_ring |= len(loop) > len(shown)
        steps = [{"from": x, "to": y, "at": _sites(inner[(x, y)])[:1]} for x, y in zip(shown, shown[1:])]
        breaks.append({**row((a, b)), "closes": shown, "closes_status": how, "steps": steps,
                       **({"closes_steps_total": len(loop) - 1} if len(loop) > len(shown) else {})})
    edges = sorted(inner, key=lambda e: (-inner[e]["references"], e))
    names = ", ".join(files[:4]) + (f" and {len(files) - 4} more" if len(files) > 4 else "")
    cut_short = long_ring or len(cut) > CYCLE_CUTS_SHOWN or len(edges) > CYCLE_EDGES_SHOWN \
        or len(files) > CYCLE_FILES_SHOWN
    return {
        "files": files[:CYCLE_FILES_SHOWN], "size": len(files),
        "claim": f"{names} depend on each other in a cycle ({len(files)} files, {len(inner)} dependencies)",
        "status": "strong_inference" if strong else "weak_inference",
        **({} if strong else {"note": "the parser's own edges do not connect every file of it: some link "
                                      "rests on an INFERRED edge (a receiver's type, a name) only"}),
        "break_set": breaks,
        "break_set_total": len(cut),
        "break_method": method,
        "dependencies": [row(e) for e in edges[:CYCLE_EDGES_SHOWN]],
        "dependencies_total": len(inner),
        **({"in": ASIDE} if aside else {}),
    }, cut_short


def _copy_roots(g: Graph, roots: tuple[str, ...]) -> tuple[str, ...]:
    """Those of ``roots`` that are detected copies of the project, not configured reference trees."""
    from verinoda import copies

    try:
        found = {str(c["path"]).strip("/") + "/" for c in copies.load(g.root)}
    except Exception:  # noqa: BLE001 - no detection result: no copies
        return ()
    return tuple(r for r in roots if r in found)


def _all_under(files: list[str], roots: tuple[str, ...]) -> bool:
    return bool(roots) and all(f.startswith(roots) for f in files)


def cycles(g: Graph) -> dict:
    """Dependency cycles between files and the smallest set of file-to-file dependencies to cut.

    A cycle is a strongly connected set of files over the dependencies view's edges. Its ``status`` is
    ``strong_inference`` when the parser's own (EXTRACTED) edges already connect every file of it, else
    ``weak_inference``: graph edges are extractions, never verification. Each dependency of the break set
    names a cycle it closes (the dependency, then the shortest way back) with the reference lines of every
    step, so the cut can be read in the code. Every list is capped: ``truncated`` says one was, and the
    ``*_total`` keys and ``size`` give the full counts."""
    deps, stdlib = _file_deps(g)
    with_deps = {f for e in deps for f in e}
    roots = _aside_roots(g)
    copy_roots = _copy_roots(g, roots)
    # a detected copy is hardly used from outside its folder (that is how it was detected): an edge between it
    # and the project is a name the graph resolved into the wrong tree, and it would merge the two trees' cycles
    # into one. A configured reference tree may be vendored code the project does load: its edges stay.
    crossing = sorted(e for e in deps if e[0].startswith(copy_roots) != e[1].startswith(copy_roots)) \
        if copy_roots else []
    left_out = [{"from": a, "to": b, "references": d["references"], "at": _sites(d)[:CYCLE_EDGE_SITES],
                 "why": "the line imports the standard library"} for (a, b), d in sorted(stdlib.items())]
    left_out += [{"from": a, "to": b, "references": deps[(a, b)]["references"],
                  "at": _sites(deps[(a, b)])[:CYCLE_EDGE_SITES], "why": f"between a {ASIDE} and the project"}
                 for a, b in crossing]
    for e in crossing:
        del deps[e]
    fg = nx.DiGraph()
    fg.add_edges_from(deps)
    comps = sorted((sorted(c) for c in nx.strongly_connected_components(fg) if len(c) > 1),
                   key=lambda fs: (_all_under(fs, roots), -len(fs), fs))
    comp_of = {f: k for k, fs in enumerate(comps) for f in fs}
    inners: list[dict[tuple[str, str], dict]] = [{} for _ in comps]
    for e, d in deps.items():   # one pass: each dependency goes to the cycle that holds both its files
        k = comp_of.get(e[0])
        if k is not None and comp_of.get(e[1]) == k:
            inners[k][e] = d
    budget = [GREEDY_REDUCE_STEPS]
    out, breaks_total, exact_n, unreduced, cut_short = [], 0, 0, 0, len(comps) > CYCLES_SHOWN
    for i, (files, inner) in enumerate(zip(comps, inners)):
        cost = {e: (1, d["references"]) for e, d in inner.items()}
        if len(files) <= EXACT_BREAK_MAX:
            cut, method = _exact_break(files, cost), "exact"
            exact_n += 1
        else:
            cut = _greedy_break(files, cost, budget)
            method = "greedy" if budget[0] >= 0 else "greedy, not reduced"
            unreduced += budget[0] < 0
        breaks_total += len(cut)
        if i < CYCLES_SHOWN:
            row, short = _cycle_row(files, inner, cut, method, _all_under(files, roots))
            out.append(row)
            cut_short |= short
    limits = [
        "graph edges are extractions, never verification: a cycle is strong_inference at most, weak_inference "
        "when only INFERRED edges connect some of its files",
        "calls, uses and inherits count as dependencies, not only imports: two files calling each other form a "
        "cycle even without an import cycle (each dependency lists its relations)",
        "type-only imports and deferred import(...) are left out; an import inside a function body counts like "
        "one at the top of the file",
        f"the break set is the fewest file-to-file dependencies to cut, then the fewest references behind them; "
        f"exact for a cycle of up to {EXACT_BREAK_MAX} files, an upper bound (greedy, no cut can be put back) "
        f"beyond; it names what to cut, not how; a dependency on INFERRED edges only (extracted 0) costs the "
        f"same as one the parser extracted",
        "dynamic dispatch, reflection and DI containers are not resolved; package-level cycles are not computed",
    ]
    if unreduced:
        limits.append(f"{unreduced} large cycles ran past the work limit of the greedy search "
                      "(\"greedy, not reduced\"): some of their cuts may not be needed")
    if stdlib:
        limits.append(f"{len(stdlib)} Python import dependencies are left out (listed in left_out): their line "
                      "imports the standard library (import html) and the graph gave the name to a project "
                      "module of the same name")
    if copy_roots:
        limits.append("detected copies of the project (" + ", ".join(copy_roots[:3]) + ") are kept apart from "
                      "it: their cycles come after the project's own, marked \"in\", and the "
                      f"{len(crossing)} dependencies between them and the project are left out (listed in "
                      "left_out)")
    refs = [r for r in roots if r not in copy_roots]
    if refs:
        limits.append("configured reference trees (" + ", ".join(refs[:3]) + ") keep their dependencies on the "
                      "project and back (vendored code can be loaded): a cycle wholly inside one comes after the "
                      "project's own, marked \"in\"; one that crosses into the project is the project's")
    cut_short |= len(left_out) > LEFT_OUT_SHOWN
    return {
        "view": "cycles",
        "coverage": {
            "method": "strongly connected components of the file-level graph of AST call/import/use/inherit "
                      "edges; smallest break set by search over file orderings (greedy ordering for large cycles)",
            "limits": limits,
        },
        "level": "file",
        "cycles": out,
        "cycles_total": len(comps),
        "exact_break_sets": exact_n,
        "files_in_cycles": sum(len(c) for c in comps),
        "break_set_size": breaks_total,
        "files_with_dependencies": len(with_deps),
        **({"left_out": left_out[:LEFT_OUT_SHOWN], "left_out_total": len(left_out)} if left_out else {}),
        **({"truncated": True} if cut_short else {}),
    }


# -- 9. repo map -----------------------------------------------------------------------------

REPO_MAP_TOKENS = 1024        # default budget of `map --view repo` (estimated tokens of the rendered map)
REPO_MAP_DAMPING = 0.85
REPO_MAP_SIGNATURE_CHARS = 160
REPO_MAP_UNRESOLVED_SHOWN = 20
# share of the random jump that lands on the files in play (split among them), the rest spread over all files:
# a fixed share, so that the focus weighs as much on a thousand files as on ten
REPO_MAP_FOCUS_JUMP = 0.9
# data files whose keys the extractor emits as symbols: no signatures to show
REPO_MAP_DATA_SUFFIXES = (".json", ".toml", ".ini", ".cfg", ".yml", ".yaml", ".properties", ".xml")


def repo_tokens(text: str) -> int:
    """The budget's token estimate of one rendered line: four characters a token, the newline counted."""
    return (len(text) + 4) // 4


def repo_map_lines(row: dict) -> list[str]:
    """How the repo view renders a file: its path, then each signature with its line (map_text uses this)."""
    return [f"{row['file']}:"] + [f"  L{s['at'].rsplit(':', 1)[1]} {s['text']}" for s in row["signatures"]]


def _pagerank(nodes: list[str], edges: dict[tuple[str, str], float], focus: set[str]) -> dict[str, float]:
    """Weighted PageRank by power iteration; ``REPO_MAP_FOCUS_JUMP`` of the random jump lands on the files in
    play, the rest on all files alike (uniform without them). A file without dependencies spreads its rank like
    a jump."""
    n = len(nodes)
    focus = focus & set(nodes)
    rest = (1 - REPO_MAP_FOCUS_JUMP) if focus else 1.0
    jump = {f: rest / n + (REPO_MAP_FOCUS_JUMP / len(focus) if f in focus else 0.0) for f in nodes}
    out_w: dict[str, float] = defaultdict(float)
    for (a, _b), w in edges.items():
        out_w[a] += w
    rank = dict(jump)
    for _ in range(100):
        dangling = sum(rank[f] for f in nodes if not out_w.get(f))
        nxt = {f: (1 - REPO_MAP_DAMPING + REPO_MAP_DAMPING * dangling) * jump[f] for f in nodes}
        for (a, b), w in edges.items():
            nxt[b] += REPO_MAP_DAMPING * rank[a] * w / out_w[a]
        if sum(abs(nxt[f] - rank[f]) for f in nodes) < 1e-10 * n:
            return nxt
        rank = nxt
    return rank


def _signature(g: Graph, sym: str, lines: list[str]) -> tuple[int, str] | None:
    """The definition line of a symbol (its indentation kept): its own line, or past its decorators the first
    line naming it as a whole word. None when no such line is found (the file changed since the scan)."""
    ln = g.line(sym)
    if not ln or ln > len(lines):
        return None
    name = g.label(sym).lstrip(".").split("(")[0]
    word = re.compile(rf"(?<![\w$]){re.escape(name)}(?![\w$])") if name else None
    for i in range(ln, min(ln + 12, len(lines) + 1)):
        text = lines[i - 1].rstrip()
        if not text.strip() or text.lstrip().startswith("@"):
            continue
        if word is None or word.search(text):
            if len(text) > REPO_MAP_SIGNATURE_CHARS:
                text = text[:REPO_MAP_SIGNATURE_CHARS - 3] + "..."
            return i, text
    return None


def _outer_symbols(g: Graph, f: str) -> list[str]:
    """The symbols of a file that no function or method holds: classes, their methods, module functions."""
    kept, stack = [], []   # stack: (end line, holds_code) of the enclosing symbols
    for s in g.symbols_in(f):
        ln = g.line(s) or 0
        while stack and stack[-1][0] < ln:
            stack.pop()
        if stack and stack[-1][1]:
            continue
        kept.append(s)
        sp = g.span(s)
        if sp:
            stack.append((sp[1], g.label(s).endswith(")")))
    return kept


def repo_map(g: Graph, focus: list[str] | None = None, max_tokens: int = REPO_MAP_TOKENS) -> dict:
    """The files most worth reading first and their signatures, under a token budget (Aider's repo map).

    Files are ranked by PageRank over the dependencies view's file-to-file edges (weighted by the square root
    of their references), the random jump weighted toward the files in play (``focus``). A file's rank is shared
    among its outer symbols by the references other files make to each. Symbols are taken by that score until
    the estimated tokens of the rendered map reach ``max_tokens``: everything shown outranks everything left
    out. The files in play are left out of the map (they are already being read). The ranking is
    ``strong_inference``; each signature is the source line at its ``at``."""
    from verinoda.index import PROSE_SUFFIXES

    code = [f for f in _files(g) if not f.lower().endswith(PROSE_SUFFIXES + REPO_MAP_DATA_SUFFIXES)
            and g.symbols_in(f)]
    known = set(_files(g))
    in_play, unresolved = [], []
    for t in focus or []:
        rel = repo_relative(g.root, str(t).split("::")[0].strip())
        (in_play if rel in known else unresolved).append(rel if rel in known else str(t))
    in_play = sorted(set(in_play))
    deps, _stdlib = _file_deps(g)
    edges = {e: d["references"] ** 0.5 for e, d in deps.items()}
    nodes = sorted(set(code) | {f for e in edges for f in e})
    rank = _pagerank(nodes, edges, set(in_play)) if nodes else {}
    inbound: Counter = Counter()
    for u, v, _d in g.edges(CYCLE_RELATIONS):
        if g.file(u) != g.file(v) and g.is_symbol(v):
            inbound[v] += 1
    scored = []
    for f in code:
        if f in in_play:
            continue
        syms = _outer_symbols(g, f)
        share = sum(inbound[s] for s in syms) + len(syms)
        for s in syms:
            scored.append((-rank.get(f, 0.0) * (1 + inbound[s]) / share, f, g.line(s) or 0, s))
    scored.sort()
    rows: dict[str, dict] = {}
    used, shown, not_found, full = 0, 0, 0, False
    lines: dict[str, list[str]] = {}
    for _score, f, _ln, s in scored:
        if f not in lines:
            lines[f] = _read(g.root, f)
        sig = _signature(g, s, lines[f])
        if not sig:   # counted, so that "N of M" is never a budget cut it was not
            not_found += 1
            continue
        if full:
            continue
        entry = {"at": f"{f}:{sig[0]}", "text": sig[1]}   # the text names the symbol
        cost = repo_tokens(repo_map_lines({"file": f, "signatures": [entry]})[1])
        cost += 0 if f in rows else repo_tokens(f"{f}:")
        if used + cost > max_tokens:
            full = True   # a prefix of the ranking: nothing after this one is shown
            continue
        used += cost
        shown += 1
        rows.setdefault(f, {"file": f, "rank": round(rank.get(f, 0.0), 5), "status": "strong_inference",
                            "signatures": []})["signatures"].append(entry)
    files = sorted(rows.values(), key=lambda r: (-r["rank"], r["file"]))
    for r in files:
        r["signatures"].sort(key=lambda e: int(e["at"].rsplit(":", 1)[1]))
    return {
        "view": "repo",
        "coverage": {
            "method": "PageRank over the dependencies view's file edges, the random jump weighted toward the "
                      "files in play; outer symbols by references from other files; tokens = characters / 4",
            "limits": ["the ranking is strong_inference: a heuristic of what to read first, not of what matters",
                       "graph edges are extractions, never verification; dynamic dispatch, reflection and DI "
                       "containers are not resolved",
                       "a signature is the definition's first line (a multi-line signature is cut there)",
                       "the files in play are left out of the map; their dependents rank only through what "
                       "they use (the impact view lists dependents)",
                       "a symbol whose definition line is no longer where the index says (the file changed since "
                       "the scan) is not shown, and counted in signatures_not_found"],
        },
        "focus": in_play,
        **({"focus_unresolved": unresolved[:REPO_MAP_UNRESOLVED_SHOWN],
            "focus_unresolved_total": len(unresolved)} if unresolved else {}),
        "max_tokens": max_tokens,
        "tokens": used,
        "files": files,
        "files_ranked": len(nodes),
        "symbols_shown": shown,
        "symbols_total": len(scored) - not_found,
        **({"signatures_not_found": not_found} if not_found else {}),
    }


def repo_relative(root: Path | str, target: str) -> str:
    """A path as the graph names files: relative to the repository, forward slashes, ``.`` and ``..`` resolved
    (an absolute path inside the repository is made relative; one outside it is returned as given)."""
    import posixpath

    t = target.replace("\\", "/")
    if Path(target).is_absolute():
        try:
            t = Path(target).resolve().relative_to(Path(root).resolve()).as_posix()
        except (OSError, ValueError):
            return t
    t = posixpath.normpath(t) if t else t
    return "" if t == "." else t


VIEWS = {
    "hierarchy": hierarchy, "dependencies": dependencies, "dataflow": dataflow,
    "config": config, "tests": tests_view, "history": history, "cycles": cycles, "dead": dead,
    "hotspots": hotspots, "sides": sides, "repo": repo_map,
}


# the views `verinoda map` builds when none is named; dead, hotspots and sides are asked for by name (--view dead)
DEFAULT_VIEWS = ("hierarchy", "dependencies", "dataflow", "config", "tests", "history", "cycles")


def build_map(g: Graph, views: list[str] | None = None) -> dict:
    views = views or list(DEFAULT_VIEWS)
    return {v: VIEWS[v](g) for v in views}
