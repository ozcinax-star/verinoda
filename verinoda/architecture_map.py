"""Top-down views of a project built on the knowledge graph.

Each view returns ``{"view", "coverage", ...data}`` where ``coverage`` states
the method used and what the view cannot see. Relations that the extractor or
a scanner did not observe are never invented; heuristics are labelled.

Views: hierarchy, dependencies (calls/imports), dataflow (entry -> persistence),
config (env vars and config files), tests (test -> behaviour), history (git +
decision records), impact (reverse dependencies of a change).
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict, deque
from pathlib import Path, PurePosixPath

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
    return out


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
    found = []
    for n in g.G.nodes:
        reasons = entry_reasons(g, n)
        if reasons:
            found.append({"id": n, "symbol": g.label(n), "at": _loc(g, n), "why": reasons})
    return sorted(found, key=lambda e: e["at"])


def dataflow(g: Graph, max_depth: int = 6, max_paths: int = 20) -> dict:
    sinks = _sinks(g)
    entries = entry_points(g)
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
    rendered = []
    for p in paths:
        hops = []
        for a, b in zip(p, p[1:]):
            ds = g.G.get_edge_data(a, b) or {}
            d = next(iter(ds.values()), {}) if ds else {}
            hops.append({"from": g.label(a), "to": g.label(b), "relation": d.get("relation"),
                         "confidence": d.get("confidence"), "at": _edge_loc(d),
                         **({"derived_by": d["_origin"]} if str(d.get("_origin", "")).startswith("verinoda") else {})})
        rendered.append({"entry": _loc(g, p[0]), "sink": _loc(g, p[-1]),
                         "sink_kinds": sorted({s["kind"] for s in sinks[p[-1]]}),
                         "sink_lines": [s["at"] for s in sinks[p[-1]]][:4], "hops": hops})
    return {
        "view": "dataflow",
        "coverage": {
            "method": "entry points (decorators/names/entry modules, heuristic) -> AST call edges -> "
                      "persistence sinks (regex over symbol bodies: SQL, db connect, ORM, file writes)",
            "limits": ["paths follow call edges only; values are not tracked (no taint analysis)",
                       "Python param.method() calls are resolved from type annotations/local constructors "
                       "(edges marked derived_by=verinoda.receiver, INFERRED)",
                       "entry-point and sink detection are heuristics and are labelled as such",
                       "framework routing tables and async queues are not followed"],
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
    cfg_files = [f for f in _files(g) if CONFIG_FILE_RE.search(f)]
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


# -- 5. tests ----------------------------------------------------------------------

REACH_RELATIONS = testcode.REACH_RELATIONS


def test_reach(g: Graph, depth: int = 3) -> dict[str, set[str]]:
    """Product symbol -> names of the tests that statically reach it: from each test
    (:func:`verinoda.testcode.test_units`) over call/use/reference edges and the calls the graph does not hold
    (:func:`verinoda.testcode.first_hop`), ``depth`` steps, only through code that is not test code."""
    covers: dict[str, set[str]] = defaultdict(set)
    for u in testcode.test_units(g):
        frontier = {v for v in testcode.first_hop(g, u, REACH_RELATIONS)
                    if g.file(v) and not testcode.is_test_code(g, v)}
        seen = frontier | ({u.node} if u.node else set())
        for v in frontier:
            covers[v].add(u.name)
        for _ in range(depth - 1):
            nxt = set()
            for n in frontier:
                for v, _ in g.out_edges(n, REACH_RELATIONS):
                    if v not in seen and g.file(v) and not testcode.is_test_code(g, v):
                        seen.add(v)
                        nxt.add(v)
                        covers[v].add(u.name)
            frontier = nxt
    return covers


def tests_view(g: Graph, depth: int = 3) -> dict:
    units = testcode.test_units(g)
    covers = test_reach(g, depth)
    prod = [n for n in g.G.nodes if g.is_symbol(n) and not testcode.is_test_code(g, n)]
    untested = [_loc(g, n) + f" {g.label(n)}" for n in prod if n not in covers]
    runtime = _coverage_xml(g.root)
    return {
        "view": "tests",
        "coverage": {
            "method": f"static reachability from tests over call edges (depth {depth}); tests are recognised by "
                      "verinoda.testcode: pytest-named functions, Go Test/Benchmark functions, @Test/"
                      "@ParameterizedTest/@GameTest methods (JUnit, TestNG, Minecraft game tests), [Test]/[Fact] "
                      "methods, Rust #[test] functions, and the it()/test() calls of JS/TS test files",
            "limits": ["static reachability is not runtime coverage; mocks and fixtures are invisible",
                       ("a JS/TS it()/test() reaches the symbols its body names from the project files its file "
                        "imports (a name match, not a resolved call)"),
                       "run `verinoda verify` with experiments or provide coverage.xml for runtime evidence"],
            "runtime_coverage_file": runtime.get("file"),
        },
        "tests": len(units),
        "tests_by_language": dict(sorted(Counter(u.lang for u in units).items())),
        "covered": {g.label(n) + f" ({_loc(g, n)})": sorted(ts)[:6]
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
    for f in _files(g):
        if not DOC_DECISION_RE.search(f):
            continue
        text = "\n".join(_read(root, f))
        mentions = sorted({cf for cf in code_files if cf in text or Path(cf).stem in text.split("`")})
        syms = sorted({g.label(n).strip(".()") for n in g.G.nodes if g.is_symbol(n)
                       and len(g.label(n).strip(".()")) > 3 and re.search(rf"\b{re.escape(g.label(n).strip('.()'))}\b", text)})
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


def impact(g: Graph, targets: list[str], depth: int = 4) -> dict:
    """Reverse reachability: who depends (calls/imports/uses/inherits) on the targets."""
    seeds: set[str] = set()
    unresolved = []
    for t in targets:
        hit = [n for n in g.G.nodes if g.file(n) == t]
        if hit:
            seeds.update(hit)
            continue
        nid, _ = g.resolve(t)
        if nid:
            seeds.add(nid)
        else:
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
    affected_files = Counter()
    tests = set()
    for n, dd in dist.items():
        f = g.file(n)
        if not f or dd == 0:
            continue
        affected_files[f] += 1
        if is_test_file(f):
            tests.add(f)
    # tests that reach an affected symbol through a call the graph does not hold (pkg.main.run(), a JS it())
    callers = testcode.extra_callers(g)
    for n, dd in dist.items():
        if dd < depth:
            tests.update(u.file for u in callers.get(n, ()))
    return {
        "view": "impact",
        "coverage": {
            "method": f"reverse traversal of call/import/use/inherit edges up to depth {depth}",
            "limits": ["dynamic/reflective dependents are missed", "a dependent is 'possibly affected', "
                       "not proven broken"],
        },
        "targets": targets,
        "unresolved": unresolved,
        "affected_symbols": sorted(
            ({"symbol": g.label(n), "at": _loc(g, n), "distance": dd} for n, dd in dist.items() if dd > 0),
            key=lambda x: (x["distance"], x["at"]))[:80],
        "affected_files": dict(affected_files.most_common(40)),
        "tests_to_run": sorted(tests),
    }


VIEWS = {
    "hierarchy": hierarchy, "dependencies": dependencies, "dataflow": dataflow,
    "config": config, "tests": tests_view, "history": history,
}


def build_map(g: Graph, views: list[str] | None = None) -> dict:
    views = views or list(VIEWS)
    return {v: VIEWS[v](g) for v in views}
