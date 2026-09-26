"""The one test rule (verinoda.testcode): test files, the tests of each language, and the reach the graph
does not hold (``import pkg.main; pkg.main.run()``, a vitest ``it()``), as the tests view, impact, the runtime
tracer's selection, the change review and analyze use them. Runs on a scanned polyglot copy in a temp dir."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

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
    "numpy/testing/utils.py", "src/test/java/a/ForgeTickTest.java", "src/test/kotlin/a/Price.kt",
    "src/gametest/java/com/example/glowmod/test/WispTests.java", "src/integrationTest/java/a/B.java",
    "src/main/java/a/HeatMathTest.java", "src/main/java/a/OrdersIT.java", "src/main/java/a/SiparisTesti.java",
    "cs/CalcTests.cs", "src/FooSpec.groovy", "src/FooSpec.scala", "apps/api/src/orderService.test.ts",
    "web/CheckoutPage.test.tsx", "web/cart.spec.mjs", "web/cart.test.cjs", "web/src/__tests__/cart.js",
    "go/calc_test.go", "rs/tests/integration.rs", "spec/models/user_spec.rb", "test/models/user_test.rb",
])
def test_a_test_file_by_its_path(path):
    assert testcode.is_test_file(path)


@pytest.mark.parametrize("path", [
    "orders/pricing.py", "latest.py", "src/contest.py", "src/latest/x.py", "src/contest/Y.java",
    "src/main/java/a/Contest.java", "src/main/java/a/Greatest.java", "src/main/java/a/EDIT.java",
    "src/main/java/a/ModConfigSpec.java", "src/main/kotlin/a/ConfigSpec.kt", "web/cart.js", "go/latest.go",
    "rs/src/lib.rs", "", None,
])
def test_not_a_test_file(path):
    assert not testcode.is_test_file(path)


def test_every_place_that_recognises_a_test_file_asks_the_same_rule():
    # the map, the ledger (test_edited), guards and decision briefs, the runtime tracer, search ranking and
    # the change review used to keep rules of their own that disagreed (conftest.py, x.test.mjs, testing/)
    for fn in (am.is_test_file, ts.is_test_file, guards.is_test_file, trace.is_test_path,
               si.is_test_file, rv._is_test):
        assert fn is testcode.is_test_file


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


# -- a polyglot project -------------------------------------------------------------------------------------------

FILES = {
    # Python: a test that calls through a dotted module path, which the extractor does not resolve
    "pkg/__init__.py": "",
    "pkg/main.py": "from pkg import helper\n\n\ndef run():\n    return helper.go()\n\n\ndef untested():\n"
                   "    return 0\n",
    "pkg/helper.py": "def go():\n    return True\n",
    "tests/__init__.py": "",
    "tests/test_main.py": "import pkg.main\n\n\ndef test_run():\n    assert pkg.main.run()\n\n\nclass TestMain:\n"
                          "    def test_run_again(self):\n        assert pkg.main.run()\n",
    "tests/conftest.py": "import pytest\n\n\n@pytest.fixture\ndef items_value():\n    return 1\n\n\n"
                         "def should_skip():\n    return False\n",
    # JUnit 5 methods not named test*, a private helper, a Minecraft game test in src/main and in src/gametest
    "src/main/java/demo/Heat.java": "package demo;\n\npublic class Heat {\n    public static int cooled(int h) {\n"
                                    "        return h - 1;\n    }\n\n    public static int unused() {\n"
                                    "        return 0;\n    }\n}\n",
    "src/test/java/demo/HeatTest.java": "package demo;\n\nimport org.junit.jupiter.api.Test;\n"
                                        "import org.junit.jupiter.params.ParameterizedTest;\n\nclass HeatTest {\n"
                                        "    @Test\n    void coolsByOne() {\n        Heat.cooled(2);\n    }\n\n"
                                        "    @ParameterizedTest\n    void coolsMany() {\n        Heat.cooled(3);\n"
                                        "    }\n\n    private int helper() {\n        return 1;\n    }\n}\n",
    "src/main/java/demo/HeatChecks.java": "package demo;\n\nimport net.minecraft.test.GameTest;\n\n"
                                          "public class HeatChecks {\n    @GameTest(templateName = \"empty\")\n"
                                          "    public void heatWorks(Object ctx) {\n        Heat.cooled(5);\n    }\n\n"
                                          "    public void notATest() {\n        Heat.cooled(6);\n    }\n}\n",
    "src/gametest/java/demo/WispCheck.java": "package demo;\n\nimport net.minecraft.test.GameTest;\n\n"
                                             "public class WispCheck {\n    @GameTest\n"
                                             "    public void wispSpawns(Object ctx) {\n        Heat.unused();\n"
                                             "    }\n}\n",
    "src/test/kotlin/demo/PriceTest.kt": "package demo\n\nimport org.junit.jupiter.api.Test\n\nclass PriceTest {\n"
                                         "    @Test\n    fun `cools by one`() {\n        Heat.cooled(1)\n    }\n}\n",
    # Go, C#, Rust (a #[cfg(test)] module, calls inside assert macros)
    "go/calc.go": "package calc\n\nfunc Mul(a, b int) int {\n\treturn a * b\n}\n",
    "go/calc_test.go": "package calc\n\nimport \"testing\"\n\nfunc TestMul(t *testing.T) {\n\tMul(2, 3)\n}\n\n"
                       "func BenchmarkMul(b *testing.B) {\n\tMul(2, 3)\n}\n\nfunc helperMul() int {\n"
                       "\treturn Mul(1, 1)\n}\n",
    "cs/Calc.cs": "namespace Demo {\n  public class Calc {\n    public int Twice(int x) { return x * 2; }\n  }\n}\n",
    "cs/CalcTests.cs": "using Xunit;\nnamespace Demo {\n  public class CalcTests {\n    [Fact]\n"
                       "    public void TwiceDoubles() {\n      Assert.Equal(4, new Calc().Twice(2));\n    }\n  }\n}\n",
    "rs/src/lib.rs": "pub fn add(a: i32, b: i32) -> i32 {\n    a + b\n}\n\n#[cfg(test)]\nmod tests {\n"
                     "    use super::*;\n\n    fn helper() -> i32 {\n        add(1, 1)\n    }\n\n    #[test]\n"
                     "    fn adds_two() {\n        assert_eq!(add(1, 2), 3);\n    }\n}\n",
    # vitest: tests are it() calls; the class and the singleton share one graph node (names differ in case)
    "web/src/orderService.ts": "export class OrderService {\n  placeOrder(total: number): number {\n"
                               "    return total;\n  }\n\n  listOrders(): number[] {\n    return [];\n  }\n}\n\n"
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
            ("tests/test_main.py", "TestMain"),
            ("src/test/java/demo/HeatTest.java", ".coolsByOne()"),
            ("src/test/java/demo/HeatTest.java", ".coolsMany()"),
            ("src/main/java/demo/HeatChecks.java", ".heatWorks()"),
            ("src/gametest/java/demo/WispCheck.java", ".wispSpawns()"),
            ("src/test/kotlin/demo/PriceTest.kt", ".`cools by one`()"),
            ("go/calc_test.go", "TestMul()"), ("go/calc_test.go", "BenchmarkMul()"),
            ("cs/CalcTests.cs", ".TwiceDoubles()"), ("rs/src/lib.rs", "adds_two()"),
            ("web/src/orderService.test.ts", "OrderService > places an order")} <= units
    names = {n for _, n in units}
    # helpers, fixtures, skipped tests and un-annotated methods are not tests (the old rule took any
    # symbol of a test file named it*/should*)
    assert not names & {".helper()", ".notATest()", "helperMul()", "helper()", "items_value()", "should_skip()",
                        "OrderService > lists orders"}
    heat = _node(g, "src/test/java/demo/HeatTest.java", "coolsByOne")
    assert testcode.test_id(g, heat) == "src/test/java/demo/HeatTest.java::coolsByOne"
    again = _node(g, "tests/test_main.py", "test_run_again")
    assert testcode.test_id(g, again) == "tests/test_main.py::TestMain::test_run_again"
    assert testcode.test_id(g, _node(g, "src/main/java/demo/HeatChecks.java", "notATest")) is None


def test_rust_cfg_test_modules_are_test_code(g):
    helper = _node(g, "rs/src/lib.rs", "helper")
    add = _node(g, "rs/src/lib.rs", "add")
    assert testcode.is_test_code(g, helper) and not testcode.is_test_function(g, helper)
    assert not testcode.is_test_code(g, add)
    adds_two = next(u for u in testcode.test_units(g) if u.name == "adds_two()")
    assert add in testcode.first_hop(g, adds_two)   # assert_eq!(add(1, 2), 3): a call inside a macro


def test_tests_view_sees_junit_gametest_vitest_go_csharp_rust_and_dotted_calls(g):
    tv = am.tests_view(g)
    assert tv["tests"] == len(testcode.test_units(g))
    assert {"python", "jvm", "go", "csharp", "rust", "js"} <= set(tv["tests_by_language"])
    cov = {k.split(" (")[0] + " (" + k.split(" (")[1].split(":")[0] + ")": v for k, v in tv["covered"].items()}
    assert {".coolsByOne()", ".coolsMany()", ".heatWorks()", ".`cools by one`()"} <= set(
        cov[".cooled() (src/main/java/demo/Heat.java)"])
    assert cov[".unused() (src/main/java/demo/Heat.java)"] == [".wispSpawns()"]
    assert {"test_run()", ".test_run_again()"} <= set(cov["run() (pkg/main.py)"])   # pkg.main.run()
    assert cov["Mul() (go/calc.go)"] == ["BenchmarkMul()", "TestMul()"]
    assert cov["add() (rs/src/lib.rs)"] == ["adds_two()"] and cov["Calc (cs/Calc.cs)"] == [".TwiceDoubles()"]
    assert cov[".placeOrder() (web/src/orderService.ts)"] == ["OrderService > places an order"]
    reached = " ".join(tv["not_reached_by_tests"])
    assert "untested()" in reached and ".listOrders()" in reached   # its only test is skipped
    assert "cooled" not in reached and "helper()" not in reached    # a Rust test helper is not product code


def test_dotted_module_calls_reach_the_tracer_selection_and_impact(g):
    run = _node(g, "pkg/main.py", "run")
    by = testcode.extra_callers(g)[run]
    assert {u.id for u in by} == {"tests/test_main.py::test_run", "tests/test_main.py::TestMain::test_run_again"}
    assert trace.select_tests(g, [run])[:2] == ["tests/test_main.py::TestMain::test_run_again",
                                                "tests/test_main.py::test_run"]
    assert "tests/test_main.py" in am.impact(g, [run])["tests_to_run"]
    # what the graph already holds is not repeated as a call it lacks
    go = _node(g, "go/calc.go", "Mul")
    assert go not in testcode.extra_callers(g)


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
    st = open_store(copy)
    try:
        res = rv.review(copy, store=st, record=False)
    finally:
        st.close()
    static = {t["test"]: t for t in res["tests"]["static"]}
    assert {"tests/test_main.py::test_run", "tests/test_main.py::TestMain::test_run_again"} <= set(static)
    assert "module path" in static["tests/test_main.py::test_run"]["basis"]
    assert "pkg/main.py::run" not in res["tests"]["no_test_reaches"]


def test_analyze_says_which_test_reaches_a_function_called_through_its_module_path(repo, tmp_path):
    from verinoda import analysis

    copy = tmp_path / "poly"
    shutil.copytree(repo, copy)
    st = open_store(copy)
    try:
        res = analysis.analyze(st, copy, "Which tests exercise run in pkg/main.py?")
    finally:
        st.close()
    texts = [c["text"] for c in res["claims"]]
    assert any(t.startswith("Test code statically reaches `run()`") and "test_run()" in t for t in texts), texts
    assert not any("No test statically reaches" in t for t in texts)
    assert res["subquestions"][0]["status"] != "unmet"
