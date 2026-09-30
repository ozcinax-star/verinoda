"""Dead code: code symbols and files that nothing the project starts from reaches (the map's ``dead`` view).

Roots are what the project starts from: the entry points of :func:`verinoda.architecture_map.entry_points`
(framework entries, route/command decorators, entry-like names, public callables of entry modules), every
node of test code, entry modules (a ``__main__`` guard, ``__main__.py``, a file named like ``setup.py`` or an
entry module such as ``cli.py``, a ``package.json`` ``main`` / ``bin``) and the scripts ``pyproject.toml``
declares. From them reachability follows every graph edge but containment (a file holding a function does not
run it); a reached symbol reaches its module (importing it runs the module body) and the package
``__init__.py`` files above it, a reached method its class, a reached class its protocol members (dunders,
constructors), and a reached method the same-named methods of the project's subclasses (dynamic dispatch).

What stays unreached is a claim: a file none of whose code is reached (one claim, its symbols counted under
it), else a class (its members counted under it), else a function or method, each ``zero callers`` or
``callers unreached`` with those callers' sites. Every claim names the roots searched and the dynamic uses
that could keep it alive: the name outside its definition (a string, a config or data file, code the graph
did not resolve), a decorator or annotation that may register it, an override of a type outside the
project. Reachability over extracted edges is a heuristic: a claim is ``strong_inference`` at most, and
``weak_inference`` when a dynamic use was found. Nothing is stored in the claim store.
"""

from __future__ import annotations

import json
import re
from collections import Counter, deque
from pathlib import Path, PurePosixPath

from verinoda import architecture_map as am
from verinoda.claims import CODE_SUFFIXES
from verinoda.index import Graph
from verinoda.testcode import is_test_code, is_test_or_support_file

# containment and documentation: a file holding a function, a class holding a method, a comment about a
# module neither runs nor uses it
NOT_REACH = frozenset({"contains", "defines", "method", "rationale_for", "cites"})
INHERIT = frozenset({"inherits", "extends", "implements"})
# files a tool or a person runs by name
ENTRY_MODULE_NAMES = frozenset({"__main__.py", "setup.py", "conftest.py", "noxfile.py", "manage.py", "wsgi.py",
                                "asgi.py", "fabfile.py", "tasks.py", "conf.py"})
MAIN_GUARD_RE = re.compile(r"""__name__\s*==\s*['"]__main__['"]""")
# decorators and annotations that change how a symbol is called, not whether something calls it
PLAIN_DECORATORS = frozenset("staticmethod classmethod property cached_property abstractmethod override wraps "
                             "lru_cache cache dataclass total_ordering Override Deprecated deprecated "
                             "SuppressWarnings Nullable NotNull Nonnull NonNull Contract Environment OnlyIn "
                             "SafeVarargs FunctionalInterface JvmStatic JvmOverloads Synchronized".split())
# names a framework calls without the project naming them (pytest hooks, unittest fixtures, load_tests)
NAME_CONVENTION_RE = re.compile(r"pytest_\w+$|(?:setUp|tearDown)(?:Class|Module)?$|load_tests$")
IDENTIFIER_RE = re.compile(r"[A-Za-z_$][\w$]*")
DECORATOR_RE = re.compile(r"^\s*@\s*([\w.]+)")
WORD_RE = re.compile(r"[A-Za-z_$][\w$]*")
COMMENT_STARTS = ("#", "//", "/*", "*", "--")
# files whose text is searched for the names of unreached symbols (code, config and build files; prose is not
# a use)
MENTION_SUFFIXES = CODE_SUFFIXES + (".kts", ".mjs", ".cjs", ".pyi", ".json", ".toml", ".yml", ".yaml", ".xml",
                                    ".cfg", ".ini", ".properties", ".gradle", ".sh", ".ps1", ".bat", ".cmd",
                                    ".mcfunction", ".html", ".vue", ".svelte")
DATA_SUFFIXES = (".json", ".toml", ".yml", ".yaml", ".xml", ".cfg", ".ini", ".properties", ".gradle",
                 ".mcfunction", ".html")
MENTION_FILE_MAX_BYTES = 2_000_000
MAX_MENTIONS = 5
MAX_CALLERS = 5
DEFAULT_LIMIT = 80


def _bare(g: Graph, n: str) -> str:
    return g.label(n).strip().strip(".()").rpartition(".")[2]


def _lines(g: Graph, f: str) -> list[str]:
    """A file's lines from the index's cache (not copied: read-only here)."""
    from verinoda.index import file_lines

    return file_lines(Path(g.root) / f) or []


def _suffix(f: str) -> str:
    return PurePosixPath(f).suffix.lower()


def _is_test(g: Graph, n: str) -> bool:
    return is_test_or_support_file(g.file(n)) or is_test_code(g, n)


# -- roots ---------------------------------------------------------------------------------------------

def _declared_scripts(root: Path) -> list[tuple[str, str]]:
    """``(module:function, where)`` of the scripts and entry points ``pyproject.toml`` declares."""
    try:
        import tomllib
    except ImportError:  # Python 3.10: no reader, no declared scripts
        return []
    try:
        data = tomllib.loads((Path(root) / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    project = data.get("project") or {}
    tables = [("project.scripts", project.get("scripts")), ("project.gui-scripts", project.get("gui-scripts")),
              ("tool.poetry.scripts", ((data.get("tool") or {}).get("poetry") or {}).get("scripts"))]
    tables += [(f"project.entry-points.{k}", v) for k, v in (project.get("entry-points") or {}).items()]
    out = []
    for where, table in tables:
        for val in (table.values() if isinstance(table, dict) else ()):
            if isinstance(val, str) and ":" in val:
                out.append((val.strip(), f"pyproject.toml [{where}]"))
    return out


def _package_json_files(root: Path, files: set[str]) -> dict[str, str]:
    """Project files ``package.json`` names as ``main`` or ``bin``: ``{file: why}``."""
    try:
        data = json.loads((Path(root) / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    named = [("main", data.get("main"))]
    b = data.get("bin")
    named += [("bin", v) for v in (b.values() if isinstance(b, dict) else [b])]
    out = {}
    for key, val in named:
        if isinstance(val, str):
            f = str(PurePosixPath(val.strip())).removeprefix("./")
            if f in files:
                out[f] = f"package.json {key}"
    return out


def _roots(g: Graph, file_node: dict[str, str]) -> tuple[list[dict], dict[str, str], list[str]]:
    """``(entry points, entry modules {file: why}, test nodes)``."""
    entries = am._cached(g, "entries", am.entry_points)
    modules: dict[str, str] = {}
    for f in file_node:
        name = PurePosixPath(f).name
        if name in ENTRY_MODULE_NAMES:
            modules[f] = f"{name}: run by a tool or by name"
        elif am.ENTRY_FILE_RE.search(f):
            modules[f] = "entry-like module name (heuristic)"
        elif f.endswith(".py") and any(MAIN_GUARD_RE.search(t) for t in _lines(g, f)):
            modules[f] = "__main__ guard: run as a script"
    modules.update(_package_json_files(g.root, set(file_node)))
    declared = []
    for val, where in _declared_scripts(g.root):
        mod, _, fn = val.partition(":")
        path = mod.strip().replace(".", "/")
        for f in file_node:
            if f.endswith(".py") and f[:-3].endswith(path) and (len(f) == len(path) + 3 or f[-len(path) - 4] == "/"):
                hit = next((n for n in g.symbols_in(f) if _bare(g, n) == fn.strip().split(".")[-1]), None)
                if hit:
                    declared.append({"id": hit, "symbol": g.label(hit), "at": am._loc(g, hit),
                                     "why": [f"declared script {val} ({where})"], "basis": "declared"})
                else:
                    modules.setdefault(f, f"declared script {val} ({where})")
    ids = {d["id"] for d in declared}
    entries = declared + [e for e in entries if e["id"] not in ids]
    tests = [n for n in g.G.nodes if g.file(n) and _is_test(g, n)]
    return entries, modules, tests


# -- reachability ---------------------------------------------------------------------------------------

def _nested(g: Graph, owner: dict[str, str]) -> dict[str, str]:
    """Symbol -> the function whose body defines it (a nested function or class: the graph holds no edge from
    the enclosing function, which calls it by a local name)."""
    out: dict[str, str] = {}
    files = set()
    for n in g.G.nodes:  # an indented definition that is no class member: only its file needs the spans
        f, line = g.file(n), g.line(n)
        if f and line and n not in owner and g.is_symbol(n) and f not in files:
            text = _lines(g, f)
            if 0 < line <= len(text) and text[line - 1][:1] in (" ", "\t"):
                files.add(f)
    for f in files:
        spans = [(sp, n) for n in g.symbols_in(f) if n not in owner and (sp := g.span(n))]
        funcs = [(sp, n) for sp, n in spans if not g.G.nodes[n].get("_callable_class")]
        for (a, _b), n in spans:
            inside = [(sp, p) for sp, p in funcs if p != n and sp[0] < a <= sp[1]]
            if inside:
                out[n] = max(inside, key=lambda x: x[0][0])[1]
    return out


def _reacher(g: Graph, file_node: dict[str, str], owner: dict[str, str], nested: dict[str, str]):
    """``reach(start, skip=())``: what ``start`` reaches (module docstring), never stepping into ``skip``; the
    tables it walks are built once."""
    members: dict[str, list[str]] = {}       # class -> its methods
    for m, c in owner.items():
        members.setdefault(c, []).append(m)
    inner: dict[str, list[str]] = {}         # function -> the functions and classes its body defines
    for n, p in nested.items():
        inner.setdefault(p, []).append(n)
    subclasses: dict[str, list[str]] = {}
    for u, v, _ in g.edges(INHERIT):
        subclasses.setdefault(v, []).append(u)

    def inits(f: str) -> list[str]:  # importing pkg/sub/mod.py runs pkg/__init__.py and pkg/sub/__init__.py
        out, parts = [], PurePosixPath(f).parts[:-1]
        for i in range(1, len(parts) + 1):
            n = file_node.get(str(PurePosixPath(*parts[:i], "__init__.py")))
            if n:
                out.append(n)
        return out

    def below(cls: str) -> list[str]:
        seen, q = {cls}, deque([cls])
        while q:
            for s in subclasses.get(q.popleft(), ()):
                if s not in seen:
                    seen.add(s)
                    q.append(s)
        return [s for s in seen if s != cls]

    def reach(start, skip: set[str] | frozenset = frozenset()) -> set[str]:
        seen: set[str] = set()
        q = deque(n for n in start if n in g.G)
        while q:
            n = q.popleft()
            if n in seen:
                continue
            seen.add(n)
            nxt = [v for v, d in g.out_edges(n) if d.get("relation") not in NOT_REACH]
            f = g.file(n)
            if f:
                nxt += [x for x in (file_node.get(f), *(inits(f) if f.endswith(".py") else ())) if x]
            if n in owner:
                cls = owner[n]
                nxt.append(cls)
                name = _bare(g, n)
                nxt += [m for s in below(cls) for m in members.get(s, ()) if _bare(g, m) == name]
            if n in members:  # protocol members: the language calls them on a live class
                cname = _bare(g, n)
                nxt += [m for m in members[n] if _protocol(_bare(g, m), cname)]
            nxt += inner.get(n, ())
            q.extend(v for v in nxt if v not in seen and v not in skip)
        return seen

    return reach


def _protocol(name: str, cls: str) -> bool:
    """A member the language calls itself: a dunder, a constructor."""
    return (name.startswith("__") and name.endswith("__")) or name in (cls, "constructor", "init", "<init>")


# -- dynamic uses ---------------------------------------------------------------------------------------

def _mention_files(g: Graph, aside: tuple[str, ...]) -> list[str]:
    """Code, config and build files to search for names (not Verinoda's index, not a copy of the project)."""
    from verinoda.snapshot import listed_files

    files = set(am._files(g))
    try:
        files |= set(listed_files(g.root))
    except OSError:
        pass
    out = []
    for f in sorted(files):
        if f.startswith(".verinoda/") or _suffix(f) not in MENTION_SUFFIXES or (aside and f.startswith(aside)):
            continue
        try:
            if (Path(g.root) / f).stat().st_size > MENTION_FILE_MAX_BYTES:
                continue
        except OSError:
            continue
        out.append(f)
    return out


def _mentions(g: Graph, words: set[str], aside: tuple[str, ...]) -> dict[str, list[tuple[str, int, str]]]:
    """``word -> [(file, line, text)]``: the lines of code, config and build files naming each word (comment
    lines left out)."""
    out: dict[str, list[tuple[str, int, str]]] = {}
    if not words:
        return out
    for f in _mention_files(g, aside):
        for i, text in enumerate(_lines(g, f), 1):
            s = text.lstrip()
            if not s or s.startswith(COMMENT_STARTS):
                continue
            for w in set(WORD_RE.findall(text)) & words:
                out.setdefault(w, []).append((f, i, text))
    return out


def _decorators(g: Graph, n: str) -> list[tuple[str, int]]:
    """``(name, line)`` of the decorators / annotations right above a definition."""
    f, line = g.file(n), g.line(n)
    if not f or not line:
        return []
    lines = _lines(g, f)
    out, i = [], line - 1
    if 0 <= i < len(lines):  # an annotation on the definition's own line (Java: `@Override public void x()`)
        out += [(m, line) for m in re.findall(r"@\s*([\w.]+)", lines[i].split("(")[0])]
    i -= 1
    while i >= 0 and line - i <= 8:
        m = DECORATOR_RE.match(lines[i])
        if not m:
            if lines[i].strip().endswith((")", ",")) and not lines[i].strip().endswith(";"):
                i -= 1  # an argument line of a multi-line annotation
                continue
            break
        out.append((m.group(1), i + 1))
        i -= 1
    return out


PY_BASES_RE = re.compile(r"^\s*class\s+\w+\s*\((.*)\)\s*:")
JVM_BASES_RE = re.compile(r"\b(?:extends|implements)\s+([\w.$<>?,\s]+?)\s*(?=\{|\bimplements\b|\bextends\b|$)")
KT_BASES_RE = re.compile(r"^[^:(]*\bclass\s+\w+[^:]*?:\s*([\w.<>?,()\s]+?)\s*(?:\{|$)")


def _written_bases(g: Graph, cls: str) -> list[str]:
    """The base types a class's own header names (the graph has no edge to a base outside the project written
    as ``module.Base``)."""
    f, line = g.file(cls) or "", g.line(cls)
    lines = _lines(g, f) if f and line else []
    head = " ".join(t.strip() for t in lines[line - 1:line + 3]) if lines else ""
    if not f.endswith(".py"):
        head = head.split("{")[0]
    out: list[str] = []
    for rx in (PY_BASES_RE, JVM_BASES_RE, KT_BASES_RE):
        for m in rx.finditer(head):
            for b in re.split(r",(?![^<]*>)", m.group(1)):
                b = re.sub(r"<.*|\(.*", "", b).strip()
                if b and "=" not in b:
                    out.append(b)
        if out:
            break
    return out


def _dynamic_uses(g: Graph, n: str, name: str, mentions: dict, defs: dict[str, set[tuple[str, int]]],
                  external_bases: dict[str, list[str]], owner: dict[str, str], edge_sites: set[str],
                  project_classes: set[str]) -> tuple[list[dict], int]:
    """The dynamic uses that could keep ``n`` alive (first :data:`MAX_MENTIONS` name mentions) and how many
    name mentions there are in all. The sites of the graph's own edges to ``n`` (``edge_sites``) are not
    mentions: those callers are already known, and unreached."""
    out: list[dict] = []
    f = g.file(n) or ""
    for dec, line in _decorators(g, n):
        short = dec.rpartition(".")[2]
        if short == "Override":
            out.append({"kind": "override", "at": f"{f}:{line}",
                        "why": "@Override: called through the supertype's method"})
        elif short not in PLAIN_DECORATORS:
            out.append({"kind": "decorator", "at": f"{f}:{line}",
                        "why": f"@{dec}: a decorator or annotation may register it with a framework"})
    if NAME_CONVENTION_RE.match(name):
        out.append({"kind": "convention", "at": am._loc(g, n),
                    "why": f"`{name}` is a name a framework calls by convention (a pytest hook, a unittest fixture)"})
    cls = owner.get(n)
    if cls and cls not in external_bases:
        external_bases[cls] = [b for b in _written_bases(g, cls) if b.rpartition(".")[2] not in project_classes]
    if cls and external_bases.get(cls):
        out.append({"kind": "override", "at": am._loc(g, cls),
                    "why": f"its class extends {', '.join(external_bases[cls][:3])} (outside the project): it "
                           "may override a method called through it"})
    span = g.span(n) or (g.line(n) or 0, g.line(n) or 0)
    own_defs = defs.get(name, set())
    hits, total = _name_mentions(name, mentions, lambda mf, line: (mf == f and span[0] <= line <= span[1])
                                 or (mf, line) in own_defs or f"{mf}:{line}" in edge_sites)
    return out + hits, total


def _name_mentions(name: str, mentions: dict, skip) -> tuple[list[dict], int]:
    """The first :data:`MAX_MENTIONS` lines naming ``name`` that ``skip(file, line)`` does not leave out, strings
    and config files first, and how many there are in all."""
    hits = []
    for mf, line, text in mentions.get(name, ()):
        if skip(mf, line):
            continue
        if re.search(rf"""["'`][^"'`]*(?<![\w$]){re.escape(name)}(?![\w$])[^"'`]*["'`]""", text):
            kind, why = "string", "named in a string (getattr, reflection, a registry or a config by name)"
        elif mf.endswith(DATA_SUFFIXES):
            kind, why = "config", "named in a config, data or build file"
        else:
            kind, why = "name", "the name appears in code the graph did not resolve to it (a call through an " \
                                "untyped receiver, a callback, a same-named symbol)"
        hits.append({"kind": kind, "at": f"{mf}:{line}", "line": text.strip()[:120], "why": why})
    hits.sort(key=lambda h: (("string", "config", "name").index(h["kind"]), h["at"]))
    return hits[:MAX_MENTIONS], len(hits)


# -- the view -------------------------------------------------------------------------------------------

def dead_code(g: Graph, limit: int = DEFAULT_LIMIT) -> dict:
    """The ``dead`` view: unreached code symbols and files as claims (module docstring)."""
    aside = am._aside_roots(g)
    file_node: dict[str, str] = {}
    for n, d in g.G.nodes(data=True):
        f = d.get("source_file")
        if f and d.get("file_type") == "code" and g.is_file_node(n):
            file_node.setdefault(f, n)
    entries, modules, tests = _roots(g, file_node)
    start = [e["id"] for e in entries] + [file_node[f] for f in modules] + tests
    owner = {v: u for u, v, _ in g.edges({"method"})}
    nested = _nested(g, owner)
    reach = _reacher(g, file_node, owner, nested)
    reached = reach(start)
    external_bases: dict[str, list[str]] = {}   # class -> its bases outside the project, filled as needed
    project_classes = {_bare(g, n) for n in g.G.nodes if g.G.nodes[n].get("_callable_class") and g.file(n)}

    def candidate(n: str) -> bool:
        f = g.file(n) or ""
        return (g.is_symbol(n) and _suffix(f) in CODE_SUFFIXES and not _is_test(g, n)
                and not (aside and f.startswith(aside)) and bool(IDENTIFIER_RE.fullmatch(_bare(g, n))))

    considered = [n for n in g.G.nodes if candidate(n)]
    code_files = sorted({g.file(n) for n in considered} | {f for f in file_node if _suffix(f) in CODE_SUFFIXES
                                                             and not is_test_or_support_file(f)
                                                             and not (aside and f.startswith(aside))})
    dead_files = [f for f in code_files if file_node.get(f) and file_node[f] not in reached
                  and not any(n in reached for n in g.symbols_in(f))]
    dead_file_set = set(dead_files)
    kept_by_protocol = 0
    units: list[str] = []
    folded: Counter = Counter()
    for n in considered:
        if n in reached or g.file(n) in dead_file_set:
            continue
        top = n  # a member of an unreached class, a function defined in an unreached function: counted under it
        while (up := owner.get(top) or nested.get(top)) and up not in reached and candidate(up):
            top = up
        if top != n:
            folded[top] += 1
            continue
        cls = owner.get(n)
        if cls and _protocol(_bare(g, n), _bare(g, cls)):
            kept_by_protocol += 1
            continue
        units.append(n)

    names = {_bare(g, n) for n in units} | {PurePosixPath(f).stem for f in dead_files}
    names.discard("__init__")
    mentions = _mentions(g, names, aside)
    defs: dict[str, set[tuple[str, int]]] = {}
    for n in g.G.nodes:  # definition lines are not uses of a same-named symbol
        if g.is_symbol(n) and g.line(n):
            defs.setdefault(_bare(g, n), set()).add((g.file(n), g.line(n)))

    searched_text = (f"{len(entries)} entry points, {len(tests)} test-code nodes and {len(modules)} entry "
                     "modules searched")
    by_basis = Counter(e.get("basis") or "heuristic" for e in entries)
    found: dict[str, tuple[list[dict], list[dict], int]] = {}   # unit -> (callers, dynamic uses, mentions)
    for n in units:
        callers = []
        for u, d in g.in_edges(n):
            if d.get("relation") in NOT_REACH or u == n or not g.file(u) or g.G.nodes[u].get("file_type") != "code":
                continue
            callers.append({"from": g.label(u), "from_at": am._loc(g, u), "relation": d.get("relation"),
                            "at": am._edge_loc(d)})
        uses, total = _dynamic_uses(g, n, _bare(g, n), mentions, defs, external_bases, owner,
                                    {c["at"] for c in callers if c["at"]}, project_classes)
        found[n] = (callers, uses, total)
    # what a unit that a dynamic use could keep alive reaches could be kept alive with it
    unit_set = set(units)
    via: dict[str, str] = {}
    for n in sorted((n for n in units if found[n][1]), key=lambda x: am._loc(g, x)):
        for m in reach([n], reached) & unit_set:
            if m != n and not found[m][1]:
                via.setdefault(m, n)

    claims: list[dict] = []
    for f in dead_files:
        nsym = len([n for n in g.symbols_in(f) if candidate(n)])
        stem = PurePosixPath(f).stem
        uses, total = ([], 0) if stem == "__init__" else _name_mentions(stem, mentions, lambda mf, _l, f=f: mf == f)
        importers = sorted({g.file(u) for u, d in g.in_edges(file_node[f]) if d.get("relation") not in NOT_REACH
                            and g.file(u) and g.file(u) != f})
        text = (f"`{f}` is not reached: none of the {searched_text} imports it or reaches any of its {nsym} "
                "symbols" + (f"; it is imported only by unreached files ({', '.join(importers[:3])})"
                             if importers else ""))
        claims.append(_claim("orphan_file", f, f"{f}:1", text, uses, total, searched_text,
                             callers=[{"from": x} for x in importers[:MAX_CALLERS]], members=nsym))
    for n in units:
        callers, uses, total = found[n]
        if n in via:
            uses = [{"kind": "via", "at": am._loc(g, via[n]),
                     "why": f"reached from `{g.label(via[n])}`, which a dynamic use could keep alive"}]
        at = am._loc(g, n)
        kind = "zero_callers" if not callers else "callers_unreached"
        what = "class" if g.G.nodes[n].get("_callable_class") else ("method" if n in owner else "function")
        text = (f"{what} `{g.label(n)}` ({at}) is not reached from any of the {searched_text}; "
                + ("nothing in the project calls or uses it" if not callers else
                   "its only callers are unreached: " + ", ".join(c["from"] for c in callers[:3])))
        if folded.get(n):
            text += f" (with the {folded[n]} unreached symbols defined in it)"
        claims.append(_claim(kind, g.label(n), at, text, uses, total, searched_text,
                             callers=callers[:MAX_CALLERS], members=folded.get(n) or None, symbol_kind=what))
    order = {"strong_inference": 0, "weak_inference": 1}
    claims.sort(key=lambda c: (c["kind"] != "orphan_file", order[c["status"]], c["at"]))
    counts = Counter(c["status"] for c in claims)
    return {
        "view": "dead",
        "coverage": {
            "method": "reachability from entry points, test code and entry modules over every graph edge but "
                      "containment; each unreached code file, class, function or method is a claim checked for "
                      "dynamic uses (its name outside its definition, decorators and annotations, overrides of "
                      "types outside the project)",
            "limits": [
                "calls the extractor did not resolve (dynamic dispatch, reflection, getattr, dependency injection, "
                "framework routing tables) are not edges: the name search is the check for them, and a name found "
                "makes the claim weak_inference",
                "entry points are heuristics (entry_points of the dataflow view) plus test code, entry modules and "
                "declared scripts; a library's public API that other projects call is not an entry point here",
                "Python module-level calls are not graph edges: a name a reached module imports counts as used",
                "code reached only by tests counts as reached",
                "a claim is strong_inference at most (reachability over extracted edges is a heuristic); claims "
                "are not stored in the claim store",
            ] + ([f"detected copies and reference trees ({', '.join(aside[:3])}) are searched from but not "
                  "reported"] if aside else []),
        },
        "searched": {
            "entry_points": [{k: e[k] for k in ("symbol", "at", "why", "basis") if k in e} for e in entries[:100]],
            "entry_points_total": len(entries),
            "entry_points_by_basis": dict(sorted(by_basis.items())),
            "entry_modules": [{"file": f, "why": w} for f, w in sorted(modules.items())][:100],
            "entry_modules_total": len(modules),
            "test_code_nodes": len(tests),
            "relations_followed": "every relation but " + ", ".join(sorted(NOT_REACH)),
        },
        "summary": {"code_symbols": len(considered), "reached": sum(1 for n in considered if n in reached),
                    "orphan_files": len(dead_files), "dead_symbols": len(units),
                    "counted_under_a_dead_symbol": sum(folded.values()), "kept_by_protocol": kept_by_protocol,
                    "strong_inference": counts.get("strong_inference", 0),
                    "weak_inference": counts.get("weak_inference", 0)},
        "claims": claims[:limit],
        **({"truncated": True, "claims_not_shown": len(claims) - limit} if len(claims) > limit else {}),
    }


def _claim(kind: str, subject: str, at: str, text: str, uses: list[dict], mentions_total: int, searched: str, *,
           callers: list[dict], members: int | None = None, symbol_kind: str | None = None) -> dict:
    status = "weak_inference" if uses else "strong_inference"
    if uses:
        text += f"; {len(uses)} dynamic use(s) could keep it alive"
    d = {"kind": kind, "subject": subject, "at": at, "claim": text, "status": status,
         "evidence_at": [at] + [c["at"] for c in callers if c.get("at")],
         "entry_points_searched": searched, "callers": callers, "dynamic_uses": uses,
         "derived_by": "verinoda.deadcode (reachability over graph edges, heuristic)"}
    if mentions_total > MAX_MENTIONS:
        d["name_mentions_total"] = mentions_total
    if members:
        d["members"] = members
    if symbol_kind:
        d["symbol_kind"] = symbol_kind
    return d
