"""What Verinoda counts as a test: one rule for every place a test is recognised.

The architecture map's tests view, impact's ``tests_to_run``, the change review's test reach, the runtime
tracer's test selection, the debug ledger's test edits, search ranking's test demotion and the UI all ask
this module, so they cannot disagree about what a test is.

**Test files** (:func:`is_test_file`, from the path alone): test directories (``tests/``, ``test/``,
``testing/``, ``__tests__/``, ``spec/``), Gradle/Maven test source sets (``src/test``, ``src/gametest``,
``src/integrationTest``, ``src/testFixtures`` ...), Python ``test_x.py`` / ``x_test.py`` / ``tests.py`` and
pytest's ``conftest.py``, Go ``x_test.go``, JS/TS ``x.test.ts`` / ``x.spec.mjs`` (any of js, jsx, ts, tsx
with an optional c/m), JVM and .NET classes ``FooTest``, ``FooTests``, ``FooIT`` (and the Turkish
``FooTesti`` / ``FooTestleri``), Spock/ScalaTest ``FooSpec.groovy`` / ``.scala``, Ruby ``x_spec.rb`` /
``x_test.rb``. ``latest.py``, ``contest.py``, ``src/latest/`` and ``src/contest/`` are not tests.

**Tests** (:func:`is_test_function` for a graph symbol, :func:`test_units` for every test of a graph):

- Python: in a test file, a function, method or class named ``test*`` / ``Test*`` (what pytest collects);
- Go: in a ``_test.go`` file, ``TestX``, ``BenchmarkX``, ``ExampleX``, ``FuzzX``;
- Java, Kotlin, Groovy, Scala: a method annotated ``@Test``, ``@ParameterizedTest``, ``@RepeatedTest``,
  ``@TestFactory``, ``@TestTemplate`` (JUnit 4/5, TestNG), ``@GameTest`` or ``@GameTestGenerator``
  (Minecraft game tests: vanilla, Fabric, NeoForge) - in any source set - or, in a test file, a method named
  ``test*`` (JUnit 3);
- C#: a method with ``[Test]``, ``[TestCase]``, ``[TestMethod]``, ``[Fact]``, ``[Theory]`` ...;
- Rust: a function with ``#[test]``, ``#[tokio::test]`` (any ``#[...::test]``), ``#[rstest]``,
  ``#[test_case]``; the functions of a ``#[cfg(test)]`` module are test code (:func:`is_test_code`);
- JS/TS (vitest, jest, mocha, node:test, playwright): the ``it(...)`` / ``test(...)`` calls of a test file
  (``.only``, ``.concurrent``, ``.each(...)(...)`` included; ``.skip`` / ``.todo`` and those inside a
  skipped ``describe`` are not run, so they are not tests). These have no graph node: each is a
  :class:`TestUnit` named by its ``describe > it`` strings, at the line of the call.

**Reach the graph does not hold** (:func:`extra_targets`, :func:`extra_callers`), added to static test
reach only (never to the graph, so search ranking and the other views are unchanged):

- Python calls through a dotted module path, ``import pkg.main`` then ``pkg.main.run()`` (also
  ``import a.b as x; x.c.f()`` and a class in between, ``pkg.mod.Cls.method()``), resolved through the
  test file's imports to a project module and a symbol defined in it;
- a JS/TS call-site test reaches the symbols of the project files its file imports that the test's body
  names (``OrderService``, ``<CheckoutPage />``), and methods of their classes called in the body
  (``.placeOrder(``). This is a name match inside imported files, not a resolved call.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path, PurePosixPath

TEST_FILE_RE = re.compile(
    # test directories
    r"(^|/)(tests?|testing|__tests__|spec)/"
    # Python (pytest's default names, Django's tests.py, pytest fixtures in conftest.py) and Go
    r"|(^|/)test_[^/]+\.py$|_test\.(py|go)$|(^|/)(tests|conftest)\.py$"
    # JS/TS: x.test.ts, x.spec.mjs, X.test.tsx
    r"|\.(test|spec)\.[cm]?[jt]sx?$"
    # Gradle/Maven test source sets (src/test, src/gametest, src/integrationTest, src/testFixtures);
    # not src/latest/, src/contest/, src/_pytest/
    r"|(^|/)src/(test[A-Z0-9_][A-Za-z0-9_]*|tests?|gametest|[a-z]+Tests?)/"
    # JVM / .NET test classes; Spock and ScalaTest specifications; Ruby
    r"|(Tests?|[a-z0-9]IT|Testleri|Testi)\.(java|cs|kt|groovy|scala)$|[a-z0-9]Spec\.(groovy|scala)$"
    r"|_(spec|test)\.rb$"
)

REACH_RELATIONS = frozenset({"calls", "uses", "references"})   # the edges static test reach follows
PY_SUFFIXES = (".py", ".pyi")
JVM_SUFFIXES = (".java", ".kt", ".kts", ".groovy", ".scala")
JS_SUFFIXES = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts")
JVM_TEST_ANNOTATIONS = ("Test", "ParameterizedTest", "RepeatedTest", "TestFactory", "TestTemplate", "GameTest",
                        "GameTestGenerator")
CS_TEST_ATTRIBUTES = ("Test", "TestCase", "TestCaseSource", "TestMethod", "DataTestMethod", "Fact", "Theory")
_JVM_ANN = re.compile(r"@(?:[\w.]+\.)?(?:" + "|".join(JVM_TEST_ANNOTATIONS) + r")\b(?!\.)")
_CS_ATTR = re.compile(r"\[\s*(?:[\w.]+\.)?(?:" + "|".join(CS_TEST_ATTRIBUTES) + r")\b(?:Attribute)?")
_RS_ATTR = re.compile(r"#\[\s*(?:\w+::)*(?:test|rstest|test_case|quickcheck)\b")
_GO_TEST = re.compile(r"(Test|Benchmark|Example|Fuzz)([A-Z0-9_]|$)")
# a line above a declaration that belongs to it: an annotation, an attribute, a comment
_DECL_PREFIX = ("@", "#[", "[", "//", "/*", "*")
_CFG_TEST_MOD = re.compile(r"#\[\s*cfg\s*\(\s*test\s*\)\s*\]\s*(?:pub(?:\([^)]*\))?\s+)?mod\s+\w+\s*\{")


@lru_cache(maxsize=1 << 16)
def _test_path(p: str) -> bool:
    return bool(TEST_FILE_RE.search(p))


def is_test_file(path: str | None) -> bool:
    """Test code by its path (the module docstring lists the conventions); either separator."""
    if not path:
        return False
    return _test_path(str(path).replace("\\", "/"))


def lang_of(path: str | None) -> str:
    s = PurePosixPath(str(path or "")).suffix.lower()
    if s in PY_SUFFIXES:
        return "python"
    if s in JVM_SUFFIXES:
        return "jvm"
    if s in JS_SUFFIXES:
        return "js"
    return {".go": "go", ".rs": "rust", ".cs": "csharp"}.get(s, s.lstrip("."))


def clean_name(label: str) -> str:
    """``.forgeCoolsOnlyOnDecayTicks()`` -> ``forgeCoolsOnlyOnDecayTicks``; ``Cls.test_x()`` -> ``test_x``."""
    return label.strip().lstrip(".").split("(")[0].strip().rpartition(".")[2]


def _lines(g, rel: str) -> list[str]:
    from verinoda.index import file_lines

    return list(file_lines(Path(g.root) / rel) or [])


def _head(lines: list[str], line: int, name: str) -> str:
    """The text that decorates a declaration starting at ``line``: its own first lines up to the one naming
    it (Java, Kotlin and C# annotations belong to the declaration), and the annotation, attribute and comment
    lines right above it (a Rust attribute is an item of its own)."""
    if not lines or not line or line > len(lines):
        return ""
    above: list[str] = []
    i = line - 2
    while i >= 0 and len(above) < 8:
        s = lines[i].strip()
        if not s.startswith(_DECL_PREFIX):
            break
        above.append(s)
        i -= 1
    own = []
    bare = name.strip("`")
    for j in range(line - 1, min(len(lines), line + 5)):
        own.append(lines[j])
        if bare and bare in lines[j]:
            break
    return "\n".join(list(reversed(above)) + own)


def is_test_name(name: str, lang: str = "python") -> bool:
    """A test's name by its runner's convention: pytest collects ``test*`` functions and ``Test*`` classes, Go
    runs ``TestX``, ``BenchmarkX``, ``ExampleX``, ``FuzzX``. Other languages mark a test by annotation or call."""
    if lang == "python":
        return name.startswith(("test", "Test"))
    if lang == "go":
        return bool(_GO_TEST.match(name))
    return False


def _is_method(g, n: str) -> bool:
    return g.label(n).strip().startswith(".") or any(True for _ in g.in_edges(n, {"method"}))


def is_test_function(g, n: str) -> bool:
    """Whether graph symbol ``n`` is a test (the per-language rules of the module docstring)."""
    if n not in g.G or not g.is_symbol(n):
        return False
    f = g.file(n) or ""
    lang = lang_of(f)
    name = clean_name(g.label(n))
    if not name:
        return False
    if lang == "python":
        return is_test_file(f) and is_test_name(name, lang)
    if lang == "go":
        return f.endswith("_test.go") and is_test_name(name, lang)
    if lang in ("jvm", "csharp", "rust"):
        if g.G.nodes[n].get("_callable_class"):
            return False
        rx = {"jvm": _JVM_ANN, "csharp": _CS_ATTR, "rust": _RS_ATTR}[lang]
        if rx.search(_head(_lines(g, f), g.line(n) or 0, name)):
            return True
        return lang == "jvm" and is_test_file(f) and name.startswith("test") and _is_method(g, n)
    return False


def _cfg_test_spans(g, rel: str) -> list[tuple[int, int]]:
    """Line spans of the ``#[cfg(test)] mod x { ... }`` blocks of a Rust file."""
    lines = _lines(g, rel)
    text = "\n".join(lines)
    if "cfg" not in text:
        return []
    blank = _blank_c_like(text)[0]
    out = []
    for m in _CFG_TEST_MOD.finditer(blank):
        end = _match_close(blank, m.end() - 1, "{", "}")
        out.append((blank.count("\n", 0, m.start()) + 1, blank.count("\n", 0, end) + 1))
    return out


def is_test_code(g, n: str) -> bool:
    """Test code, not only tests: a symbol of a test file, a test, or a function of a Rust ``#[cfg(test)]``
    module (its helpers included)."""
    f = g.file(n) or ""
    if is_test_file(f):
        return True
    if f.endswith(".rs") and n in g.G:
        line = g.line(n) or 0
        return any(a <= line <= b for a, b in _data(g)["cfg_test"].get(f, ()))
    return False


def test_id(g, n: str) -> str | None:
    """How a test is named to its runner: ``file::test`` or ``file::Class::test`` (pytest), ``file::method``
    (JUnit, Go, C#, Rust); None when ``n`` is not a test."""
    if not is_test_function(g, n):
        return None
    f = g.file(n) or ""
    name = clean_name(g.label(n))
    if lang_of(f) == "python":
        cls = next((u for u, _ in g.in_edges(n, {"method"}) if g.file(u) == f), None)
        return f"{f}::{clean_name(g.label(cls))}::{name}" if cls else f"{f}::{name}"
    return f"{f}::{name}"


# -- every test of a graph -----------------------------------------------------------------------------------

@dataclass(frozen=True)
class TestUnit:
    """One test: a graph symbol (``node``) or a JS/TS ``it``/``test`` call (``node`` None)."""

    __test__ = False  # not a pytest class

    id: str
    file: str
    line: int
    name: str
    node: str | None
    lang: str

    @property
    def at(self) -> str:
        return f"{self.file}:{self.line}"


def test_units(g) -> list[TestUnit]:
    """Every test of the graph (cached per graph until it changes), in file and line order."""
    return _data(g)["units"]


def extra_targets(g) -> dict[str, set[str]]:
    """Unit id -> graph symbols it reaches through calls the graph does not hold (see the module docstring)."""
    return _data(g)["extra"]


def extra_callers(g) -> dict[str, list[TestUnit]]:
    """Graph symbol -> the tests that reach it through calls the graph does not hold."""
    return _data(g)["callers"]


def first_hop(g, unit: TestUnit, relations: set[str] | frozenset[str] = REACH_RELATIONS) -> set[str]:
    """What a test reaches in one step: its graph edges and the calls the graph does not hold."""
    out = set(extra_targets(g).get(unit.id, ()))
    if unit.node is not None and unit.node in g.G:
        out |= {v for v, _ in g.out_edges(unit.node, set(relations))}
    return out


def _data(g) -> dict:
    # cheap on purpose (asked once per test and per visited node): counting a multigraph's edges walks them all
    key = (id(g.G), len(g.G), str(g.root))
    hit = getattr(g, "_testcode_cache", None)
    if hit is not None and hit[0] == key:
        return hit[1]
    data = _build(g)
    try:
        g._testcode_cache = (key, data)
    except AttributeError:  # a graph object that takes no attributes: compute again next time
        pass
    return data


def _module_node(d: dict, source_file: str) -> bool:
    """:meth:`verinoda.index.Graph.is_file_node` on a node's data, without a path object per node."""
    sf = source_file.replace("\\", "/")
    label = str(d.get("label") or "").replace("\\", "/")
    name = sf.rsplit("/", 1)[-1]
    return label == name or label == sf or (label.endswith("/" + name) and sf.endswith(label))


def _build(g) -> dict:
    by_file: dict[str, list[str]] = {}
    file_node: dict[str, str] = {}   # the module node of each JS/TS test file (its import edges)
    for n, d in g.G.nodes(data=True):
        f = d.get("source_file")
        if not f:
            continue
        if _module_node(d, f):
            if f.endswith(JS_SUFFIXES) and is_test_file(f):
                file_node.setdefault(f, n)
        elif d.get("file_type") == "code":
            by_file.setdefault(f, []).append(n)
    units: list[TestUnit] = []
    extra: dict[str, set[str]] = {}
    cfg_test: dict[str, list[tuple[int, int]]] = {}
    modules = None
    for f in sorted(set(by_file) | set(file_node)):
        lang = lang_of(f)
        test_file = is_test_file(f)
        if lang == "js":
            if test_file:
                for u, targets in _js_units(g, f, file_node.get(f)):
                    units.append(u)
                    if targets:
                        extra[u.id] = targets
            continue
        if lang == "rust" and f in by_file:
            spans = _cfg_test_spans(g, f)
            if spans:
                cfg_test[f] = spans
        if lang in ("python", "go") and not test_file:
            continue
        if lang in ("jvm", "csharp", "rust") and not test_file and not _has_marker(g, f, lang):
            continue
        found = [n for n in by_file.get(f, ()) if is_test_function(g, n)]
        for n in found:
            units.append(TestUnit(id=test_id(g, n) or n, file=f, line=g.line(n) or 0, name=g.label(n), node=n,
                                  lang=lang))
        if lang == "python" and found:
            if modules is None:
                modules = _py_module_map(by_file, file_node)
            for tid, targets in _py_dotted_calls(g, f, found, modules).items():
                extra.setdefault(tid, set()).update(targets)
        if lang == "rust" and found:
            for tid, targets in _rs_macro_calls(g, f, found, by_file.get(f, ()), cfg_test.get(f, ())).items():
                extra.setdefault(tid, set()).update(targets)
    units.sort(key=lambda u: (u.file, u.line, u.name))
    ids = {u.node: u.id for u in units if u.node is not None}
    # only what the graph does not already hold (an edge it has keeps its own basis)
    kept: dict[str, set[str]] = {}
    for k, v in extra.items():
        held = {x for x, _ in g.out_edges(k, set(REACH_RELATIONS))} if k in g.G else set()
        if v - held:
            kept[ids.get(k, k)] = v - held
    extra = kept
    by_id = {u.id: u for u in units}
    callers: dict[str, list[TestUnit]] = {}
    for uid, targets in extra.items():
        u = by_id.get(uid)
        if u is None:
            continue
        for t in sorted(targets):
            callers.setdefault(t, []).append(u)
    return {"units": units, "extra": extra, "callers": callers, "cfg_test": cfg_test}


def _has_marker(g, rel: str, lang: str) -> bool:
    text = "\n".join(_lines(g, rel))
    if lang == "jvm":
        return "@" in text and bool(_JVM_ANN.search(text))
    if lang == "csharp":
        return bool(_CS_ATTR.search(text))
    return "#[" in text and bool(_RS_ATTR.search(text))


# -- Python: calls through a dotted module path ----------------------------------------------------------------

def _py_module_map(by_file: dict[str, list[str]], file_node: dict[str, str]) -> dict[str, list[str]]:
    """Dotted module name -> the project's Python files it can name: every tail of the file's path
    (``src/pkg/main.py`` is ``pkg.main``, ``main`` and ``src.pkg.main``)."""
    out: dict[str, list[str]] = {}
    for rel in sorted(set(by_file) | set(file_node)):
        if not rel.endswith(PY_SUFFIXES):
            continue
        parts = list(PurePosixPath(rel).with_suffix("").parts)
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        for i in range(len(parts)):
            out.setdefault(".".join(parts[i:]), []).append(rel)
    return out


def _resolve_module(name: str, modules: dict[str, list[str]], importer: str) -> str | None:
    rels = modules.get(name) or []
    if len(rels) == 1:
        return rels[0]
    if not rels:
        return None
    # several files end in this dotted path: the one named from the repository root (or src/), else the
    # one sharing the longest leading directories with the importing test
    want = name.replace(".", "/")
    exact = [r for r in rels if PurePosixPath(r).with_suffix("").as_posix() in (want, "src/" + want, want +
                                                                                  "/__init__", "src/" + want +
                                                                                  "/__init__")]
    if len(exact) == 1:
        return exact[0]

    def shared(r: str) -> int:
        a, b = PurePosixPath(r).parts, PurePosixPath(importer).parts
        k = 0
        while k < min(len(a), len(b)) - 1 and a[k] == b[k]:
            k += 1
        return k

    best = sorted(rels, key=lambda r: (-shared(r), r))
    return best[0] if shared(best[0]) > shared(best[1]) else None


def _dotted(expr: ast.AST) -> list[str] | None:
    parts = []
    while isinstance(expr, ast.Attribute):
        parts.append(expr.attr)
        expr = expr.value
    if not isinstance(expr, ast.Name):
        return None
    parts.append(expr.id)
    return list(reversed(parts))


def _symbol_named(g, rel: str, name: str, top: bool = True) -> str | None:
    for n in g.symbols_in(rel):
        label = g.label(n).strip()
        if (not top or not label.startswith(".")) and clean_name(label) == name and \
                not any(u for u, _ in g.in_edges(n, {"method"})):
            return n
    return None


def _method_named(g, cls: str, name: str) -> str | None:
    return next((v for v, _ in g.out_edges(cls, {"method"}) if clean_name(g.label(v)) == name), None)


# a dotted module import (``import pkg.main``, ``import a.b as x``): the file may call ``pkg.main.run()``, which
# the extractor does not resolve (it does resolve ``import m; m.f()``, ``from pkg import m; m.f()`` and aliases)
_PY_DOTTED_IMPORT = re.compile(r"^[ \t]*import[ \t]+[\w.]+\.\w", re.MULTILINE)


def _py_dotted_calls(g, rel: str, tests: list[str], modules: dict[str, list[str]]) -> dict[str, set[str]]:
    """Test node -> project symbols it calls through a module path (``pkg.main.run()``). Only files with a
    dotted module import are parsed."""
    text = "\n".join(_lines(g, rel))
    if not _PY_DOTTED_IMPORT.search(text):
        return {}
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return {}
    heads: dict[str, str] = {}      # name -> the module a dotted import binds it to
    ends: dict[int, int] = {}       # def/decorator line -> last line of the definition
    calls: list[tuple[int, list[str]]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if "." in a.name:
                    if a.asname:
                        heads[a.asname] = a.name
                    else:
                        heads.setdefault(a.name.split(".")[0], a.name.split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            end = getattr(node, "end_lineno", None) or node.lineno
            for ln in [node.lineno, *(x.lineno for x in node.decorator_list)]:
                ends[ln] = max(ends.get(ln, 0), end)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            chain = _dotted(node.func)
            if chain and len(chain) >= 2:
                calls.append((node.lineno, chain))
    spans = sorted(((g.line(n) or 0, ends.get(g.line(n) or 0, g.line(n) or 0)), n) for n in tests)
    out: dict[str, set[str]] = {}
    for line, chain in calls:
        if chain[0] not in heads:
            continue
        owner = [n for (a, b), n in spans if a <= line <= b]
        if not owner:
            continue
        target = _py_target(g, heads[chain[0]], chain[1:], modules, rel)
        if target is not None:
            out.setdefault(owner[-1], set()).add(target)
    return out


def _py_target(g, base: str, rest: list[str], modules: dict[str, list[str]], importer: str) -> str | None:
    """The symbol ``base.rest[0]...rest[-1]`` names: a function or class of a module, or a method of a class."""
    *path, name = rest
    mod = ".".join([base, *path])
    rel = _resolve_module(mod, modules, importer)
    if rel is not None:
        return _symbol_named(g, rel, name)
    if path:
        rel = _resolve_module(".".join([base, *path[:-1]]), modules, importer)
        cls = _symbol_named(g, rel, path[-1]) if rel is not None else None
        if cls is not None:
            return _method_named(g, cls, name)
    return None


# -- Rust: calls inside macros (assert_eq!(add(1, 2), 3)) ------------------------------------------------------

_RS_CALL = re.compile(r"(?<![\w:.])(?:super::|self::|crate::)*([a-z_][a-z0-9_]*)\s*\(")


def _rs_macro_calls(g, rel: str, tests: list[str], symbols, spans) -> dict[str, set[str]]:
    """Test node -> functions of the same file (outside its ``#[cfg(test)]`` modules) that the test's body
    calls by name. The extractor does not look inside macro arguments, where Rust tests make most calls."""
    lines = _lines(g, rel)
    fns: dict[str, str] = {}
    for n in symbols:
        line = g.line(n) or 0
        label = g.label(n).strip()
        if not label.startswith(".") and not any(a <= line <= b for a, b in spans):
            fns.setdefault(clean_name(label), n)
    out: dict[str, set[str]] = {}
    for t in tests:
        sp = g.span(t)
        if not sp:
            continue
        body = _blank_c_like("\n".join(lines[sp[0] - 1:sp[1]]))[0]
        hit = {fns[m] for m in _RS_CALL.findall(body) if m in fns and fns[m] != t}
        if hit:
            out[t] = hit
    return out


# -- JS/TS: it()/test() calls ------------------------------------------------------------------------------------

_JS_TOKENS = re.compile(r"//[^\n]*|/\*.*?\*/|'(?:\\.|[^'\\\n])*'|\"(?:\\.|[^\"\\\n])*\"|`(?:\\.|[^`\\])*`",
                        re.DOTALL)
_JS_CALL = re.compile(r"(?<![\w$.])(it|test|describe|context|suite)((?:\s*\.\s*[A-Za-z_$][\w$]*)*)\s*\(")
_JS_IDENT = re.compile(r"(?<![\w$])[A-Za-z_$][\w$]*")
_JS_MEMBER = re.compile(r"\.\s*([A-Za-z_$][\w$]*)")
_RUN_MODS = {"only", "concurrent", "sequential", "failing", "each", "for", "serial", "parallel"}
_SKIP_MODS = {"skip", "todo", "fixme", "skipIf", "runIf"}


def _blank_c_like(text: str) -> tuple[str, dict[int, str]]:
    """``text`` with comments and string literals blanked (same length, newlines kept), and the strings'
    contents by start offset."""
    strings: dict[int, str] = {}

    def sub(m: re.Match) -> str:
        s = m.group(0)
        if s[0] in "'\"`":
            strings[m.start()] = s[1:-1]
        return re.sub(r"[^\n]", " ", s)

    return _JS_TOKENS.sub(sub, text), strings


def _match_close(text: str, i: int, open_ch: str = "(", close_ch: str = ")") -> int:
    """Offset of the bracket closing the one at ``i`` (the end of the text when it is not closed)."""
    depth = 0
    for j in range(i, len(text)):
        c = text[j]
        if c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return j
    return len(text)


def _first_string(text: str, strings: dict[int, str], i: int) -> str | None:
    """The string literal that is the first argument of a call whose ``(`` is just before ``i``."""
    j = i
    while j < len(text) and j not in strings and text[j] in " \t\r\n":
        j += 1
    return strings.get(j)


def js_test_calls(text: str) -> list[dict]:
    """The ``it``/``test`` calls of a JS/TS test file: ``{"name", "line", "body": (start, end)}`` in order;
    ``name`` joins the enclosing ``describe`` names with `` > ``."""
    blank, strings = _blank_c_like(text)
    scopes: list[tuple[int, int, str | None, bool]] = []   # describe blocks: start, end, name, skipped
    calls: list[tuple[int, int, int, str | None]] = []       # it/test: token offset, body start, body end, name
    for m in _JS_CALL.finditer(blank):
        kind = m.group(1)
        mods = [x.strip() for x in m.group(2).split(".") if x.strip()]
        start = m.end() - 1
        if any(x in ("each", "for") for x in mods):
            close = _match_close(blank, start)
            k = close + 1
            while k < len(blank) and blank[k] in " \t\r\n":
                k += 1
            if k >= len(blank) or blank[k] != "(":
                continue
            start = k
        end = _match_close(blank, start)
        name = _first_string(blank, strings, start + 1)
        scope = kind in ("describe", "context", "suite") or "describe" in mods
        if scope:
            scopes.append((start, end, name, any(x in _SKIP_MODS for x in mods)))
            continue
        if any(x in _SKIP_MODS for x in mods) or any(x not in _RUN_MODS for x in mods):
            continue   # skipped, or a hook / helper of the runner (test.beforeEach, test.step, test.extend)
        calls.append((m.start(), start, end, name))
    out = []
    for pos, start, end, name in calls:
        outer = [s for s in scopes if s[0] < pos <= s[1]]
        if any(s[3] for s in outer):
            continue
        names = [s[2] or "?" for s in sorted(outer)] + [name or "?"]
        out.append({"name": " > ".join(names), "line": blank.count("\n", 0, pos) + 1, "body": (start, end),
                    "blank": blank})
    return out


def _js_units(g, rel: str, fnode: str | None) -> list[tuple[TestUnit, set[str]]]:
    lines = _lines(g, rel)
    if not lines:
        return []
    text = "\n".join(lines)
    if "(" not in text:
        return []
    calls = js_test_calls(text)
    if not calls:
        return []
    imported: dict[str, dict] = {}
    if fnode is not None:
        for v, _d in g.out_edges(fnode, {"imports", "imports_from"}):
            f = g.file(v)
            if f and f != rel and lang_of(f) == "js" and not is_test_file(f) and f not in imported:
                top: dict[str, str] = {}
                folded: dict[str, str] = {}
                classes = []
                for s in g.symbols_in(f):
                    label = g.label(s).strip()
                    if label.startswith("."):
                        continue
                    top.setdefault(clean_name(label), s)
                    folded.setdefault(clean_name(label).lower(), s)
                    if any(True for _ in g.out_edges(s, {"method"})):
                        classes.append(s)
                imported[f] = {"top": top, "folded": folded, "classes": classes}
    out = []
    seen: dict[str, int] = {}
    for c in calls:
        base = f"{rel}::{c['name']}"
        seen[base] = seen.get(base, 0) + 1
        uid = base if seen[base] == 1 else f"{base} [{seen[base]}]"
        body = c["blank"][c["body"][0]:c["body"][1]]
        idents = set(_JS_IDENT.findall(body))
        members = set(_JS_MEMBER.findall(body))
        targets: set[str] = set()
        for info in imported.values():
            named = {n for k, n in info["top"].items() if k in idents}
            # the graph keeps one node for names that differ only in case (class OrderService and
            # const orderService): a name with no exact match counts for that node
            named |= {info["folded"][k.lower()] for k in idents
                      if k not in info["top"] and k.lower() in info["folded"]}
            if not named:
                continue
            targets |= named
            for cls in info["classes"]:
                for v, _d in g.out_edges(cls, {"method"}):
                    if clean_name(g.label(v)) in members:
                        targets.add(v)
        out.append((TestUnit(id=uid, file=rel, line=c["line"], name=c["name"], node=None, lang="js"), targets))
    return out
