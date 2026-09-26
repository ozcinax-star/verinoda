"""The one test rule (verinoda.testcode): test files, the tests of each language, and the reach the graph
does not hold (``import pkg.main; pkg.main.run()``, a vitest ``it()``), as the tests view, impact, the runtime
tracer's selection, the change review and analyze use them. Runs on a scanned polyglot copy in a temp dir."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import json  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import architecture_map as am  # noqa: E402
from verinoda import guards, index, testcode, workflow  # noqa: E402
from verinoda import review as rv  # noqa: E402
from verinoda import search_index as si  # noqa: E402
from verinoda import treestate as ts  # noqa: E402
from verinoda.runtime import trace  # noqa: E402
from verinoda.store import open_store  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


# -- test files ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("path", [
    "tests/test_x.py", "pkg/x_test.py", "app/tests.py", "conftest.py", "tests/conftest.py", "a\\tests\\helpers.py",
    "src/test/java/a/ForgeTickTest.java", "src/test/kotlin/a/Price.kt",
    "src/gametest/java/com/example/glowmod/test/WispTests.java", "src/integrationTest/java/a/B.java",
    "src/main/java/a/HeatMathTest.java", "src/main/java/a/OrdersIT.java", "src/main/java/a/SiparisTesti.java",
    "cs/CalcTests.cs", "src/FooSpec.groovy", "src/FooSpec.scala", "apps/api/src/orderService.test.ts",
    "web/CheckoutPage.test.tsx", "web/cart.spec.mjs", "web/cart.test.cjs", "web/src/__tests__/cart.js",
    "go/calc_test.go", "rs/tests/integration.rs", "spec/models/user_spec.rb", "test/models/user_test.rb",
    "testing/test_x.py",
])
def test_a_test_file_by_its_path(path):
    assert testcode.is_test_file(path) and testcode.is_test_or_support_file(path)


@pytest.mark.parametrize("path", [
    "orders/pricing.py", "latest.py", "src/contest.py", "src/latest/x.py", "src/contest/Y.java",
    "src/main/java/a/Contest.java", "src/main/java/a/Greatest.java", "src/main/java/a/EDIT.java",
    "src/main/java/a/ModConfigSpec.java", "src/main/kotlin/a/ConfigSpec.kt", "web/cart.js", "go/latest.go",
    "rs/src/lib.rs", "src/main/java/a/testing/DebugCommand.java", "", None,
])
def test_not_a_test_file(path):
    assert not testcode.is_test_file(path) and not testcode.is_test_or_support_file(path)


@pytest.mark.parametrize("path", ["numpy/testing/utils.py", "mylib/testing/__init__.py", "testing/helpers.py",
                                  "packages/core/testing/src/test_bed.ts"])
def test_a_testing_folder_is_test_support_not_test_code(path):
    # a project may ship testing/ (numpy.testing, Angular core/testing): product code for the map, search,
    # analyze and guards; test support for the tracer, the ledger and the review (as before the one rule)
    assert testcode.is_test_or_support_file(path) and not testcode.is_test_file(path)


def test_every_place_that_recognises_a_test_file_asks_the_same_rule():
    # the map, the ledger (test_edited), guards and decision briefs, the runtime tracer, search ranking and
    # the change review used to keep rules of their own that disagreed (conftest.py, x.test.mjs, testing/)
    for fn in (am.is_test_file, guards.is_test_file, si.is_test_file):
        assert fn is testcode.is_test_file
    for fn in (ts.is_test_file, trace.is_test_path, rv._is_test):
        assert fn is testcode.is_test_or_support_file


def test_test_names_by_the_runners_convention():
    assert testcode.is_test_name("test_x") and testcode.is_test_name("TestCart")
    assert not testcode.is_test_name("it_works") and not testcode.is_test_name("should_skip")
    assert testcode.is_test_name("TestMul", "go") and testcode.is_test_name("BenchmarkMul", "go")
    assert testcode.is_test_name("Test", "go") and not testcode.is_test_name("Testify", "go")
    assert not testcode.is_test_name("coolsByOne", "jvm")   # JUnit marks a test by annotation, not by name


# -- JS/TS it()/test() calls ------------------------------------------------------------------------------------

def test_js_test_calls_are_named_by_their_describe_and_it_strings():
    text = """import { describe, it, test, expect } from 'vitest';
// it('commented out', () => {})
describe('Cart', () => {
  it('adds an item', () => { add(); });
  it.skip('skipped', () => {});
  test.each([[1], [2]])('sums %i', (n) => { total(n); });
  describe.skip('later', () => { it('not run', () => {}); });
  test.beforeEach(() => { reset(); });
  it(`a template name`, async () => { await load(); });
  const s = "it('in a string', () => {})";
});
test('top level', () => {
  total([]);
});
"""
    calls = testcode.js_test_calls(text)
    assert [c["name"] for c in calls] == ["Cart > adds an item", "Cart > sums %i", "Cart > a template name",
                                          "top level"]
    assert [c["line"] for c in calls] == [4, 6, 9, 12]
    body = calls[1]["blank"][calls[1]["body"][0]:calls[1]["body"][1]]
    assert "total(n)" in body and "[[1], [2]]" not in body   # the test's call, not the table


def test_js_tagged_template_tables_x_and_f_prefixes_and_a_local_function_named_test():
    tagged = ("describe('m', () => {\n  it.each`\n    a | b\n    ${1} | ${2}\n  `('adds $a', ({a, b}) => {\n"
              "    add(a, b);\n  });\n});\ndescribe.each`\n  a\n  ${1}\n`('suite $a', ({a}) => {\n"
              "  it('works', () => { add(a); });\n});\n")
    calls = testcode.js_test_calls(tagged)
    assert [(c["name"], c["line"]) for c in calls] == [("m > adds $a", 2), ("suite $a > works", 13)]
    body = calls[0]["blank"][calls[0]["body"][0]:calls[0]["body"][1]]
    assert "add(a, b)" in body
    # jasmine/jest: xit, xtest and an xdescribe's tests are skipped; fit / fdescribe run (focused)
    prefixed = ("xit('skipped', () => { add(); });\nxtest('skipped too', () => {});\nxdescribe('s', () => {\n"
                "  it('in skipped', () => { add(); });\n});\nfdescribe('f', () => {\n  fit('focused', () => {});\n"
                "});\n")
    assert [c["name"] for c in testcode.js_test_calls(prefixed)] == ["f > focused"]
    # the file's own function named test (and a class method named test) is not the runner
    local = ("function test(name) {\n  return /x/.test(name);\n}\n"
             "describe('cart', () => {\n  it('totals', () => {\n    test('x');\n    expect(total([1])).toBe(1);\n"
             "  });\n});\n")
    assert [(c["name"], c["line"]) for c in testcode.js_test_calls(local)] == [("cart > totals", 5)]
    method = ("class Probe {\n  test(x: string): boolean {\n    return !!x;\n  }\n}\n"
              "test('probes', () => { new Probe().test('x'); });\n")
    assert [(c["name"], c["line"]) for c in testcode.js_test_calls(method)] == [("probes", 6)]
    # a runner bound to a name (node:test, playwright's test.extend) is still the runner
    bound = ("const test = require('node:test');\ntest('a', () => {});\n"
             "const it2 = 1;\nexport const test2 = base.extend({});\n")
    assert [c["name"] for c in testcode.js_test_calls(bound)] == ["a"]


# -- a polyglot project -------------------------------------------------------------------------------------------

FILES = {
    # Python: a test that calls through a dotted module path, which the extractor does not resolve
    "pkg/__init__.py": "",
    "pkg/main.py": "from pkg import helper\n\n\ndef run():\n    return helper.go()\n\n\ndef untested():\n"
                   "    return 0\n",
    "pkg/helper.py": "def go():\n    return True\n",
    "pkg/db.py": "def make_session(url):\n    \"\"\"Open a database session for the url.\"\"\"\n"
                 "    return {\"url\": url}\n",
    "pkg/app.py": "def create_app():\n    return object()\n\n\ndef load_data():\n    return 1\n",
    "tests/__init__.py": "",
    "tests/test_main.py": "import logging.config\n\nimport pkg.main\n\n\n"
                          "def test_run():\n    assert pkg.main.run()\n\n\n"
                          "class TestMain:\n    def test_run_again(self):\n        assert pkg.main.run()\n\n\n"
                          "def test_logging():\n    logging.config.dictConfig({\"version\": 1})\n",
    # a test of the same name elsewhere that does not reach run()
    "tests/test_other.py": "from pkg import helper\n\n\ndef test_run():\n    assert helper.go()\n",
    # pytest fixtures (one named test_*): not tests, but a test reaches what the fixtures it requests call
    "tests/conftest.py": "import pytest\n\nfrom pkg.app import create_app\n\n\n@pytest.fixture\ndef items_value():\n"
                         "    return 1\n\n\ndef should_skip():\n    return False\n\n\n@pytest.fixture\n"
                         "def test_client():\n    return create_app()\n",
    "tests/test_views.py": "import pytest\n\nfrom pkg.app import load_data\n\n\n@pytest.fixture\ndef test_data():\n"
                           "    return load_data()\n\n\ndef test_index(test_client, test_data):\n"
                           "    assert test_client and test_data\n\n\n@pytest.mark.usefixtures(\"db_session\")\n"
                           "def test_with_db():\n    assert True\n",
    # a root conftest.py fixture and a shipped testing/ package: what analyze locates when asked
    "conftest.py": "import pytest\n\nfrom pkg.db import make_session\n\n\n@pytest.fixture\ndef db_session():\n"
                   "    \"\"\"A database session on an in-memory sqlite url.\"\"\"\n"
                   "    return make_session(\"sqlite://\")\n",
    "mylib/__init__.py": "",
    "mylib/core.py": "def close_enough(a, b):\n    return abs(a - b) < 1e-9\n",
    "mylib/testing/__init__.py": "from mylib.core import close_enough\n\n\n"
                                 "def assert_close(actual, desired):\n"
                                 "    \"\"\"Public helper: raise unless actual is close to desired.\"\"\"\n"
                                 "    if not close_enough(actual, desired):\n"
                                 "        raise AssertionError(f\"{actual} != {desired}\")\n",
    # a project module whose path ends in a standard-library name (app/ is not the import root that names it)
    "app/logging/__init__.py": "",
    "app/logging/config.py": "def dictConfig(cfg):\n    return cfg\n",
    # JUnit 5 methods not named test*, a private helper, a Minecraft game test in src/main and in src/gametest;
    # a JUnit test switched off by commenting its annotation out
    "src/main/java/demo/Heat.java": "package demo;\n\npublic class Heat {\n    public static int cooled(int h) {\n"
                                    "        return h - 1;\n    }\n\n    public static int unused() {\n"
                                    "        return 0;\n    }\n}\n",
    "src/test/java/demo/HeatTest.java": "package demo;\n\nimport org.junit.jupiter.api.Test;\n"
                                        "import org.junit.jupiter.params.ParameterizedTest;\n\nclass HeatTest {\n"
                                        "    @Test\n    void coolsByOne() {\n        Heat.cooled(2);\n    }\n\n"
                                        "    @ParameterizedTest\n    void coolsMany() {\n        Heat.cooled(3);\n"
                                        "    }\n\n    private int helper() {\n        return 1;\n    }\n\n"
                                        "    // @Test\n    void coolsLater() {\n        Heat.cooled(4);\n    }\n}\n",
    "src/main/java/demo/Notes.java": "package demo;\n\n/**\n * Mark each check with @Test when it moves to src/test.\n"
                                     " */\npublic class Notes {\n    // @Test\n    public void check() {\n"
                                     "        Heat.unused();\n    }\n}\n",
    "src/main/java/demo/HeatChecks.java": "package demo;\n\nimport net.minecraft.test.GameTest;\n\n"
                                          "public class HeatChecks {\n    @GameTest(templateName = \"empty\")\n"
                                          "    public void heatWorks(Object ctx) {\n        Heat.cooled(5);\n    }\n\n"
                                          "    public void notATest() {\n        Heat.cooled(6);\n    }\n}\n",
    "src/gametest/java/demo/WispCheck.java": "package demo;\n\nimport net.minecraft.test.GameTest;\n\n"
                                             "public class WispCheck {\n    @GameTest\n"
                                             "    public void wispSpawns(Object ctx) {\n        Heat.unused();\n"
                                             "    }\n}\n",
    "src/test/kotlin/demo/PriceTest.kt": "package demo\n\nimport org.junit.jupiter.api.Test\n\nclass PriceTest {\n"
                                         "    @Test\n    fun `cools by one`() {\n        Heat.cooled(1)\n    }\n\n"
                                         "    @Test fun quick() = Heat.cooled(0)\n"
                                         "    fun helperTwo() = Heat.cooled(5)\n"
                                         "}\n",
    # a Kotest spec: a test file whose tests this rule does not recognise
    "src/test/kotlin/demo/CartSpec.kt": "package demo\n\nimport io.kotest.core.spec.style.StringSpec\n\n"
                                        "class CartSpec : StringSpec({\n    \"cools\" {\n        Heat.cooled(7)\n"
                                        "    }\n})\n",
    # Go (TestMain is the package's test entry point, not a test), C#, Rust (a #[cfg(test)] module, calls inside
    # assert macros)
    "go/calc.go": "package calc\n\nfunc Mul(a, b int) int {\n\treturn a * b\n}\n",
    "go/calc_test.go": "package calc\n\nimport \"testing\"\n\nfunc TestMul(t *testing.T) {\n\tMul(2, 3)\n}\n\n"
                       "func BenchmarkMul(b *testing.B) {\n\tMul(2, 3)\n}\n\nfunc helperMul() int {\n"
                       "\treturn Mul(1, 1)\n}\n\nfunc TestMain(m *testing.M) {\n\tm.Run()\n}\n",
    "cs/Calc.cs": "namespace Demo {\n  public class Calc {\n    public int Twice(int x) { return x * 2; }\n  }\n}\n",
    "cs/CalcTests.cs": "using Xunit;\nnamespace Demo {\n  public class CalcTests {\n    [Fact]\n"
                       "    public void TwiceDoubles() {\n      Assert.Equal(4, new Calc().Twice(2));\n    }\n  }\n}\n",
    "rs/src/lib.rs": "pub fn add(a: i32, b: i32) -> i32 {\n    a + b\n}\n\n#[cfg(test)]\nmod tests {\n"
                     "    use super::*;\n\n    fn helper() -> i32 {\n        add(1, 1)\n    }\n\n    #[test]\n"
                     "    fn adds_two() {\n        assert_eq!(add(1, 2), 3);\n    }\n}\n",
    # vitest: tests are it() calls; the class and the singleton share one graph node (names differ in case)
    "web/src/receipt.ts": "export function receipt(total: number): number {\n  return total;\n}\n",
    "web/src/orderService.ts": "import { receipt } from './receipt';\n\n"
                               "export class OrderService {\n  placeOrder(total: number): number {\n"
                               "    return receipt(total);\n  }\n\n"
                               "  listOrders(): number[] {\n    return [];\n  }\n}\n\n"
                               "export const orderService = new OrderService();\n",
    "web/src/orderService.test.ts": "import { describe, expect, it } from 'vitest';\n"
                                    "import { OrderService } from './orderService';\n\n"
                                    "describe('OrderService', () => {\n  it('places an order', () => {\n"
                                    "    const service = new OrderService();\n"
                                    "    expect(service.placeOrder(3)).toBe(3);\n  });\n\n"
                                    "  it.skip('lists orders', () => {\n    new OrderService().listOrders();\n  });\n"
                                    "});\n",
}


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false",
                           "-c", "commit.gpgsign=false", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout


@pytest.fixture(scope="module")
def repo(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("testcode") / "poly"
    for rel, text in FILES.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")
    (root / ".gitignore").write_text(".verinoda/\n__pycache__/\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    workflow.init(root)
    st = open_store(root)
    try:
        workflow.scan(st, root)
    finally:
        st.close()
    return root


@pytest.fixture(scope="module")
def g(repo):
    return index.load(repo)


def _node(g, rel: str, name: str) -> str:
    return next(n for n in g.symbols_in(rel) if testcode.clean_name(g.label(n)) == name)


def test_the_tests_of_each_language(g):
    units = {(u.file, u.name) for u in testcode.test_units(g)}
    assert {("tests/test_main.py", "test_run()"), ("tests/test_main.py", ".test_run_again()"),
            ("tests/test_views.py", "test_index()"), ("tests/test_views.py", "test_with_db()"),
            ("src/test/java/demo/HeatTest.java", ".coolsByOne()"),
            ("src/test/java/demo/HeatTest.java", ".coolsMany()"),
            ("src/main/java/demo/HeatChecks.java", ".heatWorks()"),
            ("src/gametest/java/demo/WispCheck.java", ".wispSpawns()"),
            ("src/test/kotlin/demo/PriceTest.kt", ".`cools by one`()"),
            ("src/test/kotlin/demo/PriceTest.kt", ".quick()"),
            ("go/calc_test.go", "TestMul()"), ("go/calc_test.go", "BenchmarkMul()"),
            ("cs/CalcTests.cs", ".TwiceDoubles()"), ("rs/src/lib.rs", "adds_two()"),
            ("web/src/orderService.test.ts", "OrderService > places an order")} <= units
    names = {n for _, n in units}
    # helpers, fixtures (one named test_*, in conftest.py or in the test module), skipped tests, un-annotated
    # methods, a commented-out @Test, pytest's Test* container class and Go's TestMain are not tests
    assert not names & {".helper()", ".notATest()", "helperMul()", "helper()", "items_value()", "should_skip()",
                        "OrderService > lists orders", "test_client()", "test_data()", "db_session()", "TestMain",
                        "TestMain()", ".coolsLater()", ".check()", ".helperTwo()"}
    heat = _node(g, "src/test/java/demo/HeatTest.java", "coolsByOne")
    assert testcode.test_id(g, heat) == "src/test/java/demo/HeatTest.java::coolsByOne"
    again = _node(g, "tests/test_main.py", "test_run_again")
    assert testcode.test_id(g, again) == "tests/test_main.py::TestMain::test_run_again"
    assert testcode.test_id(g, _node(g, "src/main/java/demo/HeatChecks.java", "notATest")) is None
    assert testcode.test_id(g, _node(g, "tests/conftest.py", "test_client")) is None
    assert testcode.test_id(g, _node(g, "tests/test_views.py", "test_data")) is None
    # the tests view counts what the runners count: two pytest tests in test_main.py, not the class
    assert sum(1 for u in testcode.test_units(g) if u.file == "tests/test_main.py") == 3


def test_an_annotation_above_a_declaration_belongs_to_it_only_when_it_is_live():
    lines = ["class A {", "    // @Test", "    void off() {}", "    /** runs with @Test */", "    @Test",
             "    void on() {}", "    @Test fun one() = x()", "    fun two() = y()", "    @ParameterizedTest",
             "    @CsvSource({", "        \"1, 2\",", "        \"3, 4\"", "    })", "    fun adds(a: Int) {}", "}"]
    found = {ln: bool(testcode._JVM_ANN.search(testcode._head(lines, ln, name)))
             for ln, name in ((3, "off"), (6, "on"), (8, "two"), (14, "adds"))}
    assert found == {3: False, 6: True, 8: False, 14: True}
    assert testcode._ann_only("    @Test") is True and testcode._ann_only("@Test fun one() = x()") is False
    assert testcode._ann_only("[Fact] public void A() {}") is False and testcode._ann_only("#[test]") is True
    assert testcode._ann_only("@DisplayName(\"a (b\")") is True and testcode._ann_only("val x = 1") is None


def test_rust_cfg_test_modules_are_test_code(g):
    helper = _node(g, "rs/src/lib.rs", "helper")
    add = _node(g, "rs/src/lib.rs", "add")
    assert testcode.is_test_code(g, helper) and not testcode.is_test_function(g, helper)
    assert not testcode.is_test_code(g, add)
    adds_two = next(u for u in testcode.test_units(g) if u.name == "adds_two()")
    assert add in testcode.first_hop(g, adds_two)   # assert_eq!(add(1, 2), 3): a call inside a macro
    assert testcode.extra_kind(g, adds_two.id, add) == "macro"


def test_tests_view_sees_junit_gametest_vitest_go_csharp_rust_and_dotted_calls(g):
    tv = am.tests_view(g)
    assert tv["tests"] == len(testcode.test_units(g))
    assert {"python", "jvm", "go", "csharp", "rust", "js"} <= set(tv["tests_by_language"])
    cov = {k.split(" (")[0] + " (" + k.split(" (")[1].split(":")[0] + ")": v for k, v in tv["covered"].items()}
    assert {".coolsByOne()", ".coolsMany()", ".heatWorks()", ".`cools by one`()"} <= set(
        cov[".cooled() (src/main/java/demo/Heat.java)"])
    assert cov[".unused() (src/main/java/demo/Heat.java)"] == [".wispSpawns()"]   # not Notes' // @Test check()
    assert {"test_run()", ".test_run_again()"} <= set(cov["run() (pkg/main.py)"])   # pkg.main.run()
    assert cov["Mul() (go/calc.go)"] == ["BenchmarkMul()", "TestMul()"]
    assert cov["add() (rs/src/lib.rs)"] == ["adds_two()"] and cov["Calc (cs/Calc.cs)"] == [".TwiceDoubles()"]
    assert cov[".placeOrder() (web/src/orderService.ts)"] == ["OrderService > places an order"]
    reached = " ".join(tv["not_reached_by_tests"])
    assert "untested()" in reached and ".listOrders()" in reached   # its only test is skipped
    assert "cooled" not in reached and "helper()" not in reached    # a Rust test helper is not product code
    # a shipped testing/ package is product code; the root conftest.py fixture is test code
    assert "assert_close()" in reached and "db_session()" not in reached
    # a test file with no recognised test (a Kotest spec) is said, and what not_reached_by_tests then means
    silent = tv["test_files_without_recognised_tests"]
    assert "src/test/kotlin/demo/CartSpec.kt" in silent["files"]
    assert silent["count"] == len(testcode.files_without_tests(g))
    assert "tests/conftest.py" not in silent["files"]
    assert any("not reached by a recognised test" in lim for lim in tv["coverage"]["limits"])


def test_a_pytest_test_reaches_what_the_fixtures_it_requests_call(g):
    create_app, load_data = _node(g, "pkg/app.py", "create_app"), _node(g, "pkg/app.py", "load_data")
    make_session = _node(g, "pkg/db.py", "make_session")
    by = {x: {u.id for u in testcode.extra_callers(g).get(x, ())} for x in (create_app, load_data, make_session)}
    # test_client from tests/conftest.py, test_data from the test module, db_session (usefixtures) from ./conftest.py
    assert by == {create_app: {"tests/test_views.py::test_index"}, load_data: {"tests/test_views.py::test_index"},
                  make_session: {"tests/test_views.py::test_with_db"}}
    assert testcode.extra_kind(g, "tests/test_views.py::test_index", create_app) == "fixture"
    # the tracer selects the runnable test, never the fixture (pytest cannot run tests/conftest.py::test_client)
    assert trace.select_tests(g, [create_app]) == ["tests/test_views.py::test_index"]
    assert "fixture" in testcode.reach_basis("fixture")


def test_a_standard_library_module_path_is_not_a_project_module(g):
    # import logging.config; logging.config.dictConfig(...): the standard library, not app/logging/config.py
    shim = _node(g, "app/logging/config.py", "dictConfig")
    assert shim not in testcode.extra_callers(g)
    assert "tests/test_main.py::test_logging" not in testcode.extra_targets(g)
    modules, top = testcode._py_module_map(g.root, ["pkg/main.py", "src/lib/mod.py", "src/lib/__init__.py",
                                                    "app/logging/config.py"])
    assert modules["pkg.main"] == ["pkg/main.py"] and "main" not in modules   # pkg/ is a package: no bare tail
    assert "lib.mod" in modules and "logging.config" in modules and {"pkg", "src", "lib", "app"} <= top


def test_dotted_module_calls_reach_the_tracer_selection_and_impact(g):
    run = _node(g, "pkg/main.py", "run")
    by = testcode.extra_callers(g)[run]
    assert {u.id for u in by} == {"tests/test_main.py::test_run", "tests/test_main.py::TestMain::test_run_again"}
    assert trace.select_tests(g, [run])[:2] == ["tests/test_main.py::TestMain::test_run_again",
                                                "tests/test_main.py::test_run"]
    imp = am.impact(g, [run])
    assert "tests/test_main.py" in imp["tests_to_run"]
    # a reader sees why: the test, what it reaches and how (the graph walk does not show it)
    basis = {b["test"]: b for b in imp["tests_basis"]}
    assert basis["tests/test_main.py::test_run"]["reaches"] == "run()"
    assert "module path" in basis["tests/test_main.py::test_run"]["basis"]
    # what the graph already holds is not repeated as a call it lacks
    go = _node(g, "go/calc.go", "Mul")
    assert go not in testcode.extra_callers(g)


def test_the_ui_impact_lists_the_tests_the_map_impact_lists(repo, g):
    from verinoda.ui import data as uidata

    atlas = uidata.Atlas(repo)
    atlas.ensure()
    run = _node(g, "pkg/main.py", "run")
    r = atlas.impact(run, 3)
    by_file = {x["file"]: x for x in r["items"]}
    assert r["tests"] >= 1 and "tests/test_main.py" in by_file
    item = by_file["tests/test_main.py"]
    assert item["relation"] == "test reach" and "module path" in item["basis"] and item["via"] == run
    assert not any(x.get("basis") for x in atlas.impact(run, 3, tests=False)["items"])
    # a JS it() has no graph node: its test file's note stands for it, with the name-match basis
    place = _node(g, "web/src/orderService.ts", "placeOrder")
    js = {x["file"]: x for x in atlas.impact(place, 3)["items"]}["web/src/orderService.test.ts"]
    assert js["relation"] == "test reach" and "name match" in js["basis"]


def test_the_tests_are_computed_again_when_edges_are_added(g):
    testcode.test_units(g)
    assert "_testcode_cache" in g.__dict__
    index._apply_edges(g, [])
    assert "_testcode_cache" not in g.__dict__
    assert testcode.test_units(g)


def test_review_reaches_the_test_through_a_dotted_module_call(repo, tmp_path):
    copy = tmp_path / "poly"
    shutil.copytree(repo, copy)
    p = copy / "pkg" / "main.py"
    p.write_text(p.read_text(encoding="utf-8").replace("    return helper.go()\n", "    return not helper.go()\n"),
                 encoding="utf-8", newline="\n")
    r = copy / "web" / "src" / "receipt.ts"
    r.write_text(r.read_text(encoding="utf-8").replace("return total;", "return total + 1;"), encoding="utf-8",
                 newline="\n")
    st = open_store(copy)
    try:
        res = rv.review(copy, store=st, record=False)
    finally:
        st.close()
    static = {t["test"]: t for t in res["tests"]["static"]}
    assert {"tests/test_main.py::test_run", "tests/test_main.py::TestMain::test_run_again"} <= set(static)
    assert "module path" in static["tests/test_main.py::test_run"]["basis"]
    assert "pkg/main.py::run" not in res["tests"]["no_test_reaches"]
    # beyond distance 1 the basis says what the test names: placeOrder, which reaches the changed receipt()
    js = static["web/src/orderService.test.ts::OrderService > places an order"]
    assert js["distance"] == 2 and "`placeOrder` (which reaches it)" in js["basis"] and "name match" in js["basis"]


def _analyze(repo, tmp_path, question: str, **kw) -> dict:
    from verinoda import analysis

    copy = tmp_path / "poly"
    if not copy.exists():
        shutil.copytree(repo, copy)
    st = open_store(copy)
    try:
        return analysis.analyze(st, copy, question, **kw)
    finally:
        st.close()


def test_analyze_says_which_test_reaches_a_function_called_through_its_module_path(repo, tmp_path):
    res = _analyze(repo, tmp_path, "Which tests exercise run in pkg/main.py?")
    claims = [c for c in res["claims"] if c["text"].startswith("Test code statically reaches `run()`")]
    assert claims and "test_run()" in claims[0]["text"], [c["text"] for c in res["claims"]]
    assert not any("No test statically reaches" in c["text"] for c in res["claims"])
    assert res["subquestions"][0]["status"] != "unmet"
    # tests/test_other.py::test_run has the same name but does not reach run(): not cited, not to be run
    assert not any("tests/test_other.py" in e for e in claims[0]["evidence"]), claims[0]["evidence"]
    st = open_store(tmp_path / "poly")
    try:
        spec = st.claim(claims[0]["id"])["spec"]
    finally:
        st.close()
    spec = json.loads(spec) if isinstance(spec, str) else spec
    assert set(spec["tests"]) == {"tests/test_main.py::test_run", "tests/test_main.py::TestMain::test_run_again"}


def test_analyze_names_the_test_that_reaches_through_a_fixture_never_the_fixture(repo, tmp_path):
    # --run-tests runs spec.tests: one unrunnable id (tests/conftest.py::test_client) failed the whole pytest run
    res = _analyze(repo, tmp_path, "Which tests exercise create_app?")
    claim = next(c for c in res["claims"] if c["text"].startswith("Test code statically reaches `create_app()`"))
    assert "test_index()" in claim["text"] and "test_client" not in claim["text"]
    assert any("pytest fixture" in u for u in claim["uncertainties"])
    st = open_store(tmp_path / "poly")
    try:
        spec = st.claim(claim["id"])["spec"]
    finally:
        st.close()
    spec = json.loads(spec) if isinstance(spec, str) else spec
    assert spec["tests"] == ["tests/test_views.py::test_index"]


def test_analyze_says_when_a_test_reaches_only_by_a_name_match(repo, tmp_path):
    res = _analyze(repo, tmp_path, "Which tests exercise receipt?")
    claim = next(c for c in res["claims"] if c["text"].startswith("Test code statically reaches `receipt()`"))
    assert claim["status"] == "weak_inference"
    assert any("name match" in u for u in claim["uncertainties"])


@pytest.mark.parametrize("question, answer", [
    ("Where is the db_session fixture defined?", "`db_session()` is defined at conftest.py:"),
    ("Where is assert_close defined?", "`assert_close()` is defined at mylib/testing/__init__.py:"),
])
def test_analyze_locates_a_fixture_in_conftest_and_a_testing_helper(repo, tmp_path, question, answer):
    # test code named by the question is its answer when no product symbol has that name (was: the function it
    # calls, make_session / close_enough, was located instead)
    res = _analyze(repo, tmp_path, question)
    texts = [c["text"] for c in res["claims"]]
    assert any(t.startswith(answer) for t in texts), texts
    assert res["subquestions"][0]["status"] == "met"
