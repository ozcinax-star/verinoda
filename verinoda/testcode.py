"""What Verinoda counts as a test: one rule for every place a test is recognised.

The architecture map's tests view, impact's ``tests_to_run``, the change review's test reach, the runtime
tracer's test selection, the debug ledger's test edits, search ranking's test demotion and the UI all ask
this module, so they cannot disagree about what a test is.

**Test files** (:func:`is_test_file`, from the path alone): test directories (``tests/``, ``test/``,
``__tests__/``, ``spec/``), Gradle/Maven test source sets (``src/test``, ``src/gametest``,
``src/integrationTest``, ``src/testFixtures`` ...), Python ``test_x.py`` / ``x_test.py`` / ``tests.py`` and
pytest's ``conftest.py``, Go ``x_test.go``, JS/TS ``x.test.ts`` / ``x.spec.mjs`` (any of js, jsx, ts, tsx
with an optional c/m), JVM and .NET classes ``FooTest``, ``FooTests``, ``FooIT`` (and the Turkish
``FooTesti`` / ``FooTestleri``), Spock/ScalaTest ``FooSpec.groovy`` / ``.scala``, Ruby ``x_spec.rb`` /
``x_test.rb``. ``latest.py``, ``contest.py``, ``src/latest/`` and ``src/contest/`` are not tests.

A ``testing/`` directory is test support only for the runtime tracer, the debug ledger and the change review
(:func:`is_test_or_support_file`): many projects ship one as product code (``numpy.testing``,
``torch.testing``, Angular's ``core/testing``, a mod's in-game testing commands), so the map, search, analyze,
guards and decision briefs keep it in the product. Under a main source set (``src/main/``) it is never test
code.

**Tests** (:func:`is_test_function` for a graph symbol, :func:`test_units` for every test of a graph):

- Python: in a test file, a function or method named ``test*`` (what pytest collects); not a pytest fixture
  (``@pytest.fixture``, whatever its name), not a function of ``conftest.py`` (pytest collects no test there),
  not a ``Test*`` class (a container: its methods are the tests);
- Go: in a ``_test.go`` file, ``TestX``, ``BenchmarkX``, ``ExampleX``, ``FuzzX`` (not ``TestMain``, the
  package's test entry point);
- Java, Kotlin, Groovy, Scala: a method annotated ``@Test``, ``@ParameterizedTest``, ``@RepeatedTest``,
  ``@TestFactory``, ``@TestTemplate`` (JUnit 4/5, TestNG), ``@GameTest`` or ``@GameTestGenerator``
  (Minecraft game tests: vanilla, Fabric, NeoForge) - in any source set - or, in a test file, a method named
  ``test*`` (JUnit 3). A commented-out annotation (``// @Test``) does not count;
- C#: a method with ``[Test]``, ``[TestCase]``, ``[TestMethod]``, ``[Fact]``, ``[Theory]`` ...;
- Rust: a function with ``#[test]``, ``#[tokio::test]`` (any ``#[...::test]``), ``#[rstest]``,
  ``#[test_case]``; the functions of a ``#[cfg(test)]`` module are test code (:func:`is_test_code`);
- JS/TS (vitest, jest, jasmine, mocha, node:test, playwright): the ``it(...)`` / ``test(...)`` calls of a
  test file (``.only``, ``.concurrent``, ``.each(...)(...)``, a tagged-template ``.each`` table, ``fit``
  included; ``.skip`` / ``.todo``, ``xit`` / ``xtest`` and those inside a skipped ``describe`` or an
  ``xdescribe`` are not run, so they are not tests; a function the file itself declares under the name
  ``test`` or ``it`` is not the runner). These have no graph node: each is a :class:`TestUnit` named by its
  ``describe > it`` strings, at the line of the call.

**Reach the graph does not hold** (:func:`extra_targets`, :func:`extra_callers`, each with its kind,
:func:`extra_kind`, and the words for it, :func:`reach_basis`), added to static test reach only (never to the
graph, so search ranking and the other views are unchanged):

- ``module_path``: Python calls through a dotted module path, ``import pkg.main`` then ``pkg.main.run()``
  (also ``import a.b as x; x.c.f()`` and a class in between, ``pkg.mod.Cls.method()``), resolved through the
  test file's imports to a project module and a symbol defined in it. A module is named from an import root
  (the repository, ``src/``, a folder that is not a package); a standard-library name (``logging.config``)
  names the project's module only when the project has that top-level package at its root or in ``src/``;
- ``fixture``: a pytest test reaches what the fixtures it requests call (its parameters and
  ``@pytest.mark.usefixtures``, resolved in its own module, then in the ``conftest.py`` files of its folder
  and the folders above; the fixtures those fixtures request too);
- ``macro``: Rust calls inside assert macros (``assert_eq!(add(1, 2), 3)``), same file;
- ``name_match``: a JS/TS call-site test reaches the symbols of the project files its file imports that the
  test's body names (``OrderService``, ``<CheckoutPage />``), and methods of their classes called in the body
  (``.placeOrder(``). This is a name match inside imported files, not a resolved call.
"""

from __future__ import annotations

import ast
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path, PurePosixPath

TEST_FILE_RE = re.compile(
    # test directories
    r"(^|/)(tests?|__tests__|spec)/"
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
# a line above a declaration that belongs to it: an annotation or an attribute; comments are passed over
_DECL_PREFIX = ("@", "#[", "[")
_COMMENT_PREFIX = ("//", "/*", "*")
# leading annotations/attributes of a line (their arguments blanked or balanced): what is left declares something
_LEAD_ANN = re.compile(r"\s*(?:@[\w.:]+|#!?\[|\[)")
_CFG_TEST_MOD = re.compile(r"#\[\s*cfg\s*\(\s*test\s*\)\s*\]\s*(?:pub(?:\([^)]*\))?\s+)?mod\s+\w+\s*\{")
# test support that a run or an edit touches (tracer, ledger, review): a testing/ folder outside a main source set
_TESTING_DIR = re.compile(r"(^|/)testing/")
_MAIN_SOURCE_SET = re.compile(r"(^|/)src/main/")
_STDLIB = frozenset(getattr(sys, "stdlib_module_names", ()))


@lru_cache(maxsize=1 << 16)
def _test_path(p: str) -> bool:
    return bool(TEST_FILE_RE.search(p))


def is_test_file(path: str | None) -> bool:
    """Test code by its path (the module docstring lists the conventions); either separator."""
    if not path:
        return False
    return _test_path(str(path).replace("\\", "/"))


def is_test_or_support_file(path: str | None) -> bool:
    """:func:`is_test_file`, or a file of a ``testing/`` folder outside a main source set: test support for the
    runtime tracer, the debug ledger and the change review (a project may ship ``testing/`` as product code, so
    the map, search, analyze and guards use :func:`is_test_file`)."""
    if not path:
        return False
    p = str(path).replace("\\", "/")
    return _test_path(p) or (bool(_TESTING_DIR.search(p)) and not _MAIN_SOURCE_SET.search(p))


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


def _ann_only(line: str) -> bool | None:
    """For a line above a declaration: True when it holds only annotations or attributes (with their
    arguments, which may go on over the next lines), False when something follows them (a declaration of its
    own: ``@Test fun a() = ...``, ``#[test] fn a() {}``), None when it does not start with one."""
    s = _blank_c_like(line)[0].strip()
    if not s.startswith(_DECL_PREFIX):
        return None
    i = 0
    while i < len(s):
        m = _LEAD_ANN.match(s, i)
        if m is None:
            break
        i = m.end()
        if m.group(0).lstrip().startswith("@"):
            while i < len(s) and s[i] in " \t":
                i += 1
            if i >= len(s) or s[i] != "(":
                continue
            close = _match_close(s, i)
        else:
            close = _match_close(s, i - 1, "[", "]")
        if close >= len(s):
            return True          # the arguments go on over the next lines
        i = close + 1
    return not s[i:].strip()


def _head(lines: list[str], line: int, name: str) -> str:
    """The text that decorates a declaration starting at ``line``, comments and strings blanked: its own first
    lines up to the one naming it (Java, Kotlin and C# annotations belong to the declaration), and the
    annotation and attribute lines right above it (a Rust attribute is an item of its own; an annotation whose
    arguments span lines counts whole). Comment lines are passed over (``// @Test`` is no annotation); a line
    that declares something after its annotations ends the walk (they belong to that declaration)."""
    if not lines or not line or line > len(lines):
        return ""
    above: list[str] = []
    group: list[str] = []   # the lines of a multi-line annotation's arguments, bottom up
    depth = 0               # its brackets still open, going up
    i = line - 2
    while i >= 0 and len(above) + len(group) < 16:
        raw = lines[i]
        s = raw.strip()
        blank = _blank_c_like(raw)[0].strip()
        if depth > 0:
            group.append(s)
            depth += blank.count(")") - blank.count("(")
            if depth <= 0:
                if not blank.startswith("@"):
                    break        # not the arguments of an annotation
                above.extend(group)
                group, depth = [], 0
            i -= 1
            continue
        if s.startswith(_COMMENT_PREFIX):
            i -= 1
            continue
        only = _ann_only(raw)
        if only is None:
            closes = blank.count(")") - blank.count("(")
            if closes > 0 and blank[:1] in ")}]":   # the last line of a multi-line annotation's arguments
                group, depth = [s], closes
                i -= 1
                continue
            break
        if not only:
            break
        above.append(s)
        i -= 1
    own = []
    bare = name.strip("`")
    for j in range(line - 1, min(len(lines), line + 5)):
        own.append(lines[j])
        if bare and bare in lines[j]:
            break
    return _blank_c_like("\n".join(list(reversed(above)) + own))[0]


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
    return _is_test(g, n, None)


def _is_test(g, n: str, fixtures: _Fixtures | None) -> bool:
    """:func:`is_test_function`, with the pytest fixtures of ``n``'s file when the caller has them."""
    if n not in g.G or not g.is_symbol(n):
        return False
    f = g.file(n) or ""
    lang = lang_of(f)
    name = clean_name(g.label(n))
    if not name:
        return False
    if lang == "python":
        # pytest collects test* functions and methods of test files: not conftest.py, not a fixture named
        # test_*, not the Test* class that holds them (a container)
        if not (is_test_file(f) and is_test_name(name, lang) and PurePosixPath(f).name != "conftest.py"
                and g.label(n).rstrip().endswith(")")):
            return False
        return (g.line(n) or 0) not in (fixtures if fixtures is not None else _py_fixtures(g, f)).lines
    if lang == "go":
        return f.endswith("_test.go") and is_test_name(name, lang) and name != "TestMain"
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
    return _runner_id(g, n)


def _runner_id(g, n: str) -> str:
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


def extra_kind(g, unit_id: str, target: str) -> str | None:
    """How test ``unit_id`` reaches ``target`` in a step the graph does not hold: ``module_path``, ``fixture``,
    ``macro`` or ``name_match`` (the module docstring); None when it is no such step."""
    return _data(g)["how"].get((unit_id, target))


REACH_BASIS = {
    "module_path": "the test calls {what} through a module path (pkg.mod.f()), read from the test's syntax tree",
    "fixture": "the test requests a pytest fixture that calls {what} (its parameters, and the fixtures of its "
               "module and of the conftest.py files above it)",
    "macro": "the test calls {what} by name inside a macro (assert_eq!(f(..), ..)), same file",
    "name_match": "the it()/test() body names {what} from a project file its file imports (a name match, not a "
                  "resolved call)",
}


def reach_basis(kind: str | None, what: str = "it") -> str:
    """The words for a reach step of ``kind`` (:func:`extra_kind`), ``what`` being what the test reaches."""
    return REACH_BASIS.get(kind or "", REACH_BASIS["module_path"]).format(what=what)


def files_without_tests(g) -> list[str]:
    """Test files with code in which no test was recognised: helpers, fixtures, or tests of a framework this
    module does not know (Kotest, Spock, ScalaTest ...). ``conftest.py`` and ``__init__.py`` are left out."""
    return _data(g)["no_tests"]


def first_hop(g, unit: TestUnit, relations: set[str] | frozenset[str] = REACH_RELATIONS) -> set[str]:
    """What a test reaches in one step: its graph edges and the calls the graph does not hold."""
    out = set(extra_targets(g).get(unit.id, ()))
    if unit.node is not None and unit.node in g.G:
        out |= {v for v, _ in g.out_edges(unit.node, set(relations))}
    return out


def reach(g, depth: int = 3) -> dict:
    """Static test reach, cached per graph: ``covers`` maps a product symbol to the ids of the tests that reach
    it - from each test over call/use/reference edges and the steps the graph does not hold
    (:func:`first_hop`), ``depth`` steps, only through code that is not test code; ``via`` maps
    ``(symbol, test id)`` to the kind of the first step (:func:`extra_kind`) when the reach starts with one the
    graph does not hold (a reach the graph holds as well is not listed)."""
    data = _data(g)
    key = ("reach", depth)
    if key in data:
        return data[key]
    covers: dict[str, set[str]] = defaultdict(set)
    via: dict[tuple[str, str], str] = {}
    how = data["how"]
    rel = set(REACH_RELATIONS)
    for u in data["units"]:
        held = {v for v, _ in g.out_edges(u.node, rel)} if u.node is not None and u.node in g.G else set()
        frontier: dict[str, str | None] = {}
        for v in held | data["extra"].get(u.id, set()):
            if g.file(v) and not is_test_code(g, v):
                frontier[v] = None if v in held else how.get((u.id, v))
        seen = set(frontier) | ({u.node} if u.node else set())
        step = 1
        while frontier:
            for v, k in frontier.items():
                covers[v].add(u.id)
                if k:
                    via[(v, u.id)] = k
            if step >= depth:
                break
            nxt: dict[str, str | None] = {}
            for n, k in frontier.items():
                for v, _ in g.out_edges(n, rel):
                    if v in nxt:
                        if k is None:
                            nxt[v] = None   # reached through the graph as well
                    elif v not in seen and g.file(v) and not is_test_code(g, v):
                        seen.add(v)
                        nxt[v] = k
            frontier = nxt
            step += 1
    out = {"covers": dict(covers), "via": via}
    data[key] = out
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
    py_files: set[str] = set()
    for n, d in g.G.nodes(data=True):
        f = d.get("source_file")
        if not f:
            continue
        if f.endswith(PY_SUFFIXES):
            py_files.add(f)
        if _module_node(d, f):
            if f.endswith(JS_SUFFIXES) and is_test_file(f):
                file_node.setdefault(f, n)
        elif d.get("file_type") == "code":
            by_file.setdefault(f, []).append(n)
    units: list[TestUnit] = []
    extra: dict[str, set[str]] = {}
    how: dict[tuple[str, str], str] = {}
    cfg_test: dict[str, list[tuple[int, int]]] = {}
    no_tests: list[str] = []
    py_tests: dict[str, list[str]] = {}
    modules = None

    def reached(key: str, targets, kind: str) -> None:
        extra.setdefault(key, set()).update(targets)
        for t in targets:
            how.setdefault((key, t), kind)

    for f in sorted(set(by_file) | set(file_node)):
        lang = lang_of(f)
        test_file = is_test_file(f)
        if lang == "js":
            if test_file:
                found_js = _js_units(g, f, file_node.get(f))
                for u, targets in found_js:
                    units.append(u)
                    if targets:
                        reached(u.id, targets, "name_match")
                if not found_js and f in by_file:
                    no_tests.append(f)
            continue
        if lang == "rust" and f in by_file:
            spans = _cfg_test_spans(g, f)
            if spans:
                cfg_test[f] = spans
        if lang in ("python", "go") and not test_file:
            continue
        if lang in ("jvm", "csharp", "rust") and not test_file and not _has_marker(g, f, lang):
            continue
        fx = _py_fixtures(g, f) if lang == "python" else None   # read once per file
        found = [n for n in by_file.get(f, ()) if _is_test(g, n, fx)]
        if test_file and not found and PurePosixPath(f).name not in ("conftest.py", "__init__.py"):
            no_tests.append(f)
        for n in found:
            units.append(TestUnit(id=_runner_id(g, n), file=f, line=g.line(n) or 0, name=g.label(n), node=n,
                                  lang=lang))
        if lang == "python" and found:
            py_tests[f] = found
            if modules is None:
                modules = _py_module_map(g.root, sorted(py_files))
            for tid, targets in _py_dotted_calls(g, f, found, modules).items():
                reached(tid, targets, "module_path")
        if lang == "rust" and found:
            for tid, targets in _rs_macro_calls(g, f, found, by_file.get(f, ()), cfg_test.get(f, ())).items():
                reached(tid, targets, "macro")
    if py_tests:
        for tid, targets in _py_fixture_reach(g, py_tests, by_file).items():
            reached(tid, targets, "fixture")
    units.sort(key=lambda u: (u.file, u.line, u.name))
    ids = {u.node: u.id for u in units if u.node is not None}
    # only what the graph does not already hold (an edge it has keeps its own basis)
    kept: dict[str, set[str]] = {}
    kept_how: dict[tuple[str, str], str] = {}
    for k, v in extra.items():
        held = {x for x, _ in g.out_edges(k, set(REACH_RELATIONS))} if k in g.G else set()
        if v - held:
            uid = ids.get(k, k)
            kept[uid] = v - held
            for t in kept[uid]:
                kept_how[(uid, t)] = how[(k, t)]
    extra = kept
    by_id = {u.id: u for u in units}
    callers: dict[str, list[TestUnit]] = {}
    for uid, targets in extra.items():
        u = by_id.get(uid)
        if u is None:
            continue
        for t in sorted(targets):
            callers.setdefault(t, []).append(u)
    return {"units": units, "extra": extra, "how": kept_how, "callers": callers, "cfg_test": cfg_test,
            "no_tests": sorted(no_tests)}


def _has_marker(g, rel: str, lang: str) -> bool:
    text = "\n".join(_lines(g, rel))
    if lang == "jvm":
        return "@" in text and bool(_JVM_ANN.search(text))
    if lang == "csharp":
        return bool(_CS_ATTR.search(text))
    return "#[" in text and bool(_RS_ATTR.search(text))


# -- Python: pytest fixtures -------------------------------------------------------------------------------------

@dataclass(frozen=True)
class _Fixtures:
    names: dict        # fixture name -> (def line, the def and decorator lines)
    lines: frozenset   # the def and decorator lines of every fixture of the file
    requests: dict     # a fixture's def line -> the fixtures it requests (its parameters)


_NO_FIXTURES = _Fixtures({}, frozenset(), {})
_FIXTURE_CACHE: dict = {}


# a fixture's decorator line: @pytest.fixture, @fixture, @pytest_asyncio.fixture(...), @pytest.yield_fixture
_FIXTURE_DECO = re.compile(r"^\s*@\s*(?:[\w.]+\.)?(?:fixture|yield_fixture)\b")
_FIXTURE_NAME = re.compile(r"\bname\s*=\s*['\"]([A-Za-z_]\w*)['\"]")


def _fixtures_of(lines: list[str]) -> _Fixtures:
    """The fixtures of a module's lines: a ``@...fixture`` decorator, the ``def`` that follows it (its
    ``name=`` if the decorator gives one), its decorator and def lines, and the fixtures it requests (read
    from the lines, no syntax tree: this runs over every Python test file)."""
    if not any("fixture" in ln for ln in lines):
        return _NO_FIXTURES
    names: dict[str, tuple[int, frozenset]] = {}
    marked: set[int] = set()
    requests: dict[int, list[str]] = {}
    i = 0
    while i < len(lines):
        if not _FIXTURE_DECO.match(lines[i]):
            i += 1
            continue
        j = i
        while j < len(lines) and j - i < 40 and not _PY_DEF.match(lines[j]):
            j += 1
        m = _PY_DEF.match(lines[j]) if j < len(lines) else None
        if m is None:
            i = j + 1
            continue
        first = i
        while first > 0 and lines[first - 1].strip().startswith("@"):
            first -= 1
        named = _FIXTURE_NAME.search("\n".join(lines[i:j]))
        own = frozenset(range(first + 1, j + 2))       # 1-based: the decorators to the def line
        names.setdefault(named.group(1) if named else m.group(1), (j + 1, own))
        marked |= own
        requests[j + 1] = _py_requests(lines, j + 1, m.group(1))
        i = j + 1
    return _Fixtures(names, frozenset(marked), requests) if names else _NO_FIXTURES


def _py_fixtures(g, rel: str) -> _Fixtures:
    """The pytest fixtures a Python file defines (cached per file version)."""
    from verinoda import index

    if not rel or not rel.endswith(PY_SUFFIXES):
        return _NO_FIXTURES
    p = Path(g.root) / rel
    try:
        key = index._stat_key(p)
    except OSError:
        return _NO_FIXTURES
    return index._cached(_FIXTURE_CACHE, key, lambda: _fixtures_of(_lines(g, rel)))


_PY_DEF = re.compile(r"^\s*(?:async\s+)?def\s+(\w+)\s*\(")
_USEFIXTURES = re.compile(r"usefixtures\s*\(([^)]*)\)")
_STR_LIT = re.compile(r"""['"]([A-Za-z_]\w*)['"]""")


def _py_requests(lines: list[str], line: int, name: str) -> list[str]:
    """The fixtures a test function requests: its parameters (not self/cls/request) and the names of
    ``@pytest.mark.usefixtures(...)`` above it, read from its lines (no syntax tree)."""
    if not lines or not line:
        return []
    start = next((j for j in range(line - 1, min(len(lines), line + 8))
                  if (m := _PY_DEF.match(lines[j])) and m.group(1) == name), None)
    if start is None:
        return []
    sig = "\n".join(lines[start:start + 20])
    i = sig.index("(", _PY_DEF.match(lines[start]).start(1))
    body = sig[i + 1:_match_close(sig, i)]
    out: list[str] = []
    depth, cur = 0, []
    for ch in body + ",":
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            p = "".join(cur).strip().lstrip("*").split(":", 1)[0].split("=", 1)[0].strip()
            if p.isidentifier() and p not in ("self", "cls", "request"):
                out.append(p)
            cur = []
        else:
            cur.append(ch)
    above: list[str] = []   # its decorators: the lines right above it, up to a blank line or another definition
    k = start - 1
    while k >= 0 and len(above) < 15 and lines[k].strip() and \
            not lines[k].strip().startswith(("def ", "async def ", "class ")):
        above.append(lines[k])
        k -= 1
    for m in _USEFIXTURES.finditer("\n".join(reversed(above))):
        out += _STR_LIT.findall(m.group(1))
    return list(dict.fromkeys(out))


def _conftests_above(rel: str) -> list[str]:
    """The ``conftest.py`` paths pytest reads for a test file: its folder's, then each folder above's."""
    out = []
    for d in PurePosixPath(rel).parents:
        s = d.as_posix()
        out.append("conftest.py" if s in (".", "") else f"{s}/conftest.py")
    return out


def _py_fixture_reach(g, py_tests: dict[str, list[str]], by_file: dict[str, list[str]]) -> dict[str, set[str]]:
    """Test node -> the graph symbols that the fixtures it requests call (and the fixtures they request, ten
    at most per test): pytest's fixture lookup, the test's module first, then the conftest.py files of its
    folder and the folders above."""
    fx: dict[str, _Fixtures] = {}
    for rel in by_file:
        if rel in py_tests or PurePosixPath(rel).name == "conftest.py":
            f = _py_fixtures(g, rel)
            if f.names:
                fx[rel] = f
    if not fx:
        return {}
    at_line: dict[str, dict[int, str]] = {}

    def node_at(rel: str, own: frozenset) -> str | None:
        if rel not in at_line:
            at_line[rel] = {}
            for n in by_file.get(rel, ()):
                at_line[rel].setdefault(g.line(n) or 0, n)
        return next((at_line[rel][ln] for ln in sorted(own) if ln in at_line[rel]), None)

    out: dict[str, set[str]] = {}
    for rel, tests in py_tests.items():
        chain = [c for c in [rel, *_conftests_above(rel)] if c in fx]
        if not chain:
            continue
        lines = _lines(g, rel)
        for t in tests:
            todo = _py_requests(lines, g.line(t) or 0, clean_name(g.label(t)))
            done: set[str] = set()
            hit: set[str] = set()
            while todo and len(done) < 10:
                want = todo.pop(0)
                if want in done:
                    continue
                done.add(want)
                src = next((c for c in chain if want in fx[c].names), None)
                if src is None:
                    continue
                line, own = fx[src].names[want]
                node = node_at(src, own)
                if node is not None:
                    hit |= {v for v, _ in g.out_edges(node, set(REACH_RELATIONS)) if v != t}
                todo += fx[src].requests.get(line, [])
            if hit:
                out[t] = hit
    return out


# -- Python: calls through a dotted module path ----------------------------------------------------------------

def _py_module_map(root, files: list[str]) -> tuple[dict[str, list[str]], set[str]]:
    """Dotted module name -> the project's Python files it can name, from an import root: the repository, or a
    folder that is not a package (``src/pkg/main.py`` is ``src.pkg.main`` and ``pkg.main``, not ``main`` when
    ``src/pkg`` has an ``__init__.py``); and the top-level names importable from the repository or ``src/``."""
    out: dict[str, list[str]] = {}
    top: set[str] = set()
    pkg: dict[str, bool] = {}

    def is_pkg(d: str) -> bool:
        if d not in pkg:
            pkg[d] = (Path(root) / d / "__init__.py").is_file()
        return pkg[d]

    for rel in sorted(files):
        if not rel.endswith(PY_SUFFIXES):
            continue
        parts = list(PurePosixPath(rel).with_suffix("").parts)
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        if not parts:
            continue
        top.add(parts[0])
        if parts[0] == "src" and len(parts) > 1:
            top.add(parts[1])
        for i in range(len(parts)):
            if i and is_pkg("/".join(parts[:i])):
                continue   # inside a package: importable only with the package's name in front
            out.setdefault(".".join(parts[i:]), []).append(rel)
    return out, top


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


def _py_dotted_calls(g, rel: str, tests: list[str],
                     module_map: tuple[dict[str, list[str]], set[str]]) -> dict[str, set[str]]:
    """Test node -> project symbols it calls through a module path (``pkg.main.run()``). Only files with a
    dotted module import are parsed. A standard-library module (``import logging.config``) names a project
    module only when the project has that top-level name at its root or in ``src/``."""
    modules, top = module_map
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
        first = heads[chain[0]].split(".")[0]
        if first in _STDLIB and first not in top:
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
# it/test/describe calls; x (jasmine/jest: skipped) and f (focused) in front; mocha's specify/context/suite
_JS_CALL = re.compile(r"(?<![\w$.])([xf]?)(it|test|describe|context|suite|specify)((?:\s*\.\s*[A-Za-z_$][\w$]*)*)"
                      r"\s*\(")
# a function the file declares under a runner's name (function test(name) {...}, const it = () => ...)
_JS_SHADOW = re.compile(r"(?:\bfunction\s*\*?\s*|\b(?:const|let|var)\s+)([xf]?(?:it|test|describe|context|suite"
                        r"|specify))\s*(?:\(|=\s*(?:async\s+)?(?:function\b|\([^()]*\)\s*=>|[A-Za-z_$][\w$]*\s*=>))")
# what follows a call's closing parenthesis when it was a definition (a method named test: test(a) { ... })
_JS_DEF_AFTER = re.compile(r"\s*(?::\s*[^{};=\n]+)?\{")
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
    shadowed = set(_JS_SHADOW.findall(blank))
    scopes: list[tuple[int, int, str | None, bool]] = []   # describe blocks: start, end, name, skipped
    calls: list[tuple[int, int, int, str | None]] = []       # it/test: token offset, body start, body end, name
    for m in _JS_CALL.finditer(blank):
        prefix, kind = m.group(1), m.group(2)
        if prefix + kind in shadowed:
            continue   # the file's own function of that name, not the runner
        mods = [x.strip() for x in m.group(3).split(".") if x.strip()]
        start = m.end() - 1
        if any(x in ("each", "for") for x in mods) and "`" not in text[m.end(3):start]:
            # .each(table)(name, fn): the test is the second call (a tagged-template table is followed by it)
            close = _match_close(blank, start)
            k = close + 1
            while k < len(blank) and blank[k] in " \t\r\n":
                k += 1
            if k >= len(blank) or blank[k] != "(":
                continue
            start = k
        end = _match_close(blank, start)
        if _JS_DEF_AFTER.match(blank, end + 1):
            continue   # test(a) { ... }: a method defined under that name
        name = _first_string(blank, strings, start + 1)
        if prefix and name is None:
            continue   # fit(model, data): a function of that name; a focused or skipped test is named
        skipped = prefix == "x" or any(x in _SKIP_MODS for x in mods)
        scope = kind in ("describe", "context", "suite") or "describe" in mods
        if scope:
            scopes.append((start, end, name, skipped))
            continue
        if skipped or any(x not in _RUN_MODS for x in mods):
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
