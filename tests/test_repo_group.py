"""Groups of repositories and the calls from one member into another (verinoda.repo_group, `verinoda group`).

Small fixture repositories are written and scanned on their own: a Python application that imports a library's
package (src layout, named in pyproject.toml) and calls its functions, a TypeScript application that imports a
package by its package.json name, Go and Java pairs, and a third repository that defines the same Python package
(the ambiguous case). The links are computed from the members' indexes; nothing is written into a member except
its own .verinoda.
"""

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import json  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import cli  # noqa: E402
from verinoda import repo_group as G  # noqa: E402

PY_LIB = {
    "pyproject.toml": '[project]\nname = "libb-dist"\nversion = "0.1"\n',
    "src/libb/__init__.py": "from .core import compute\n",
    "src/libb/core.py": ("def compute(x):\n    return helper(x) * 2\n\n\ndef helper(x):\n    return x + 1\n\n\n"
                         "class Engine:\n    def run(self):\n        return compute(1)\n"),
}
PY_APP = {
    "app/__init__.py": "",
    "app/main.py": ("from libb.core import compute\nimport libb.core as lc\nimport libb\nfrom libb.core import Engine\n"
                    "\n\ndef main():\n    a = compute(3)\n    b = libb.compute(a)\n    Engine.run(None)\n"
                    "    return lc.helper(b)\n"),
}
PY_OTHER = {  # a third repository that defines the package `libb` too
    "libb/__init__.py": "def compute(x):\n    return x\n",
}
JS_LIB = {
    "package.json": '{"name": "@acme/money", "main": "dist/index.js"}\n',
    "src/index.ts": "export { formatPrice } from './format';\nexport function addTax(x: number): number {\n"
                    "  return x * 1.2;\n}\n",
    "src/format.ts": "export function formatPrice(n: number): string {\n  return '$' + n.toFixed(2);\n}\n",
}
JS_APP = {
    "package.json": '{"name": "shop-ui", "dependencies": {"@acme/money": "1.0.0"}}\n',
    "src/cart.ts": ("import { formatPrice, addTax as tax } from '@acme/money';\nimport * as money from '@acme/money';\n"
                    "// import { gone } from '@acme/money';\n\nexport function total(items: number[]): string {\n"
                    "  const sum = items.reduce((a, b) => a + b, 0);\n  return formatPrice(tax(sum));\n}\n\n"
                    "export function again(n: number) {\n  return money.formatPrice(n);\n}\n"),
}
GO_LIB = {"go.mod": "module example.com/libgo\n\ngo 1.21\n",
          "calc/calc.go": "package calc\n\nfunc Add(a, b int) int {\n\treturn a + b\n}\n"}
GO_APP = {"go.mod": "module example.com/appgo\n\ngo 1.21\n",
          "main.go": 'package main\n\nimport (\n\t"fmt"\n\tc "example.com/libgo/calc"\n)\n\nfunc main() {\n'
                     '\tfmt.Println(c.Add(1, 2))\n}\n'}
JAVA_LIB = {"src/main/java/com/acme/util/Strings.java":
            "package com.acme.util;\n\npublic class Strings {\n    public static String shout(String s) {\n"
            "        return s.toUpperCase();\n    }\n}\n"}
JAVA_APP = {"src/main/java/com/shop/App.java":
            "package com.shop;\n\nimport com.acme.util.Strings;\n\npublic class App {\n"
            "    public static void main(String[] args) {\n        System.out.println(Strings.shout(\"hi\"));\n"
            "    }\n}\n"}


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")


def _repo(base: Path, name: str, files: dict[str, str]) -> Path:
    root = base / name
    _write(root, files)
    assert cli.main(["scan", str(root)]) == 0
    return root


def _tree(root: Path) -> dict[str, int]:
    """Every file of a member outside its .verinoda, with its size and mtime."""
    out = {}
    for p in root.rglob("*"):
        if p.is_file() and ".verinoda" not in p.relative_to(root).parts:
            st = p.stat()
            out[p.relative_to(root).as_posix()] = (st.st_size, st.st_mtime_ns)
    return out


@pytest.fixture(scope="module")
def repos(tmp_path_factory) -> dict[str, Path]:
    base = tmp_path_factory.mktemp("group")
    return {n: _repo(base, n, f) for n, f in (("libb", PY_LIB), ("appa", PY_APP), ("other", PY_OTHER),
                                               ("libjs", JS_LIB), ("appjs", JS_APP), ("libgo", GO_LIB),
                                               ("appgo", GO_APP), ("libjava", JAVA_LIB), ("appjava", JAVA_APP))}


@pytest.fixture
def config_dir(tmp_path, monkeypatch) -> Path:
    d = tmp_path / "userconfig"
    monkeypatch.setenv("VERINODA_CONFIG_DIR", str(d))
    return d


def _edges(res: dict) -> dict[tuple[str, str], dict]:
    return {(e["call_site"], e["target"]): e for e in res["edges"]}


def test_a_python_call_into_another_repository_is_an_edge_with_both_locators(repos, config_dir):
    before = {n: _tree(repos[n]) for n in ("appa", "libb")}
    G.create("py", [str(repos["appa"]), str(repos["libb"])])
    res = G.link("py")
    edges = _edges(res)
    # compute through `from libb.core import compute` and through the package's re-export, a method through its
    # class, and helper through `import libb.core as lc`
    assert set(edges) == {("app/main.py:8", "compute()"), ("app/main.py:9", "compute()"),
                          ("app/main.py:10", ".run()"),
                          ("app/main.py:11", "helper()")}
    e = edges[("app/main.py:8", "compute()")]
    assert e["from_member"] == "appa" and e["to_member"] == "libb" and e["caller"] == "main()"
    assert e["definition"] == "src/libb/core.py:1" and e["via"] == "app/main.py:1"
    assert e["status"] == "statically_verified" and e["check"]["definition_text"] == "def compute(x):"
    assert edges[("app/main.py:11", "helper()")]["definition"] == "src/libb/core.py:5"
    assert edges[("app/main.py:10", ".run()")]["definition"] == "src/libb/core.py:10"
    assert all(x["status"] == "statically_verified" for x in res["edges"]) and not res["ambiguous"]
    # stored in the user config folder, keyed by each member's graph
    side = json.loads((config_dir / "groups" / "py.links.json").read_text(encoding="utf-8"))
    assert set(side["members"]) == {"appa", "libb"} and side["members"]["libb"]["graph"]["sha256"]
    assert side["members"]["libb"]["defines"]["python"] == ["libb"]
    # nothing written into a member: every file outside its own .verinoda is as it was
    G.show("py")
    G.trace("py", "appa:main", "libb:helper")
    assert {n: _tree(repos[n]) for n in ("appa", "libb")} == before
    assert not list(repos["appa"].rglob("*.links.json")) and not list(repos["libb"].rglob("*.links.json"))


def test_a_call_site_that_does_not_name_the_definition_stays_an_inference(repos, config_dir, monkeypatch):
    G.create("py", [str(repos["appa"]), str(repos["libb"])])
    real = G._line

    def shifted(lines, n):  # the definition line read back is another line: not confirmed
        return "" if lines and lines[0].startswith("def compute") else real(lines, n)
    monkeypatch.setattr(G, "_line", shifted)
    res = G.link("py")
    assert {e["status"] for e in res["edges"]} == {"strong_inference"}


def test_a_typescript_import_by_package_name_links_to_the_definition(repos, config_dir):
    G.create("js", [str(repos["appjs"]), str(repos["libjs"])])
    res = G.link("js")
    edges = _edges(res)
    assert set(edges) == {("src/cart.ts:7", "formatPrice()"), ("src/cart.ts:7", "addTax()"),
                          ("src/cart.ts:11", "formatPrice()")}
    assert edges[("src/cart.ts:7", "formatPrice()")]["definition"] == "src/format.ts:1"
    assert edges[("src/cart.ts:7", "formatPrice()")]["caller"] == "total()"
    # an alias: the call line names `tax`, the import line binds it to addTax
    tax = edges[("src/cart.ts:7", "addTax()")]
    assert tax["definition"] == "src/index.ts:2" and tax["status"] == "statically_verified"
    assert tax["check"]["matched_name"] == "tax"
    assert edges[("src/cart.ts:11", "formatPrice()")]["caller"] == "again()"
    assert res["counts"]["imports"] == 2  # the commented-out import is not read


@pytest.mark.parametrize("pair, site, target, definition", [
    (("appgo", "libgo"), "main.go:9", "Add()", "calc/calc.go:3"),
    (("appjava", "libjava"), "src/main/java/com/shop/App.java:7", ".shout()",
     "src/main/java/com/acme/util/Strings.java:4"),
])
def test_go_and_java_imports_link_too(repos, config_dir, pair, site, target, definition):
    G.create("g", [str(repos[pair[0]]), str(repos[pair[1]])])
    res = G.link("g")
    assert [(e["call_site"], e["target"], e["definition"], e["status"]) for e in res["edges"]] == \
        [(site, target, definition, "statically_verified")]


def test_a_module_two_members_define_is_ambiguous_not_an_edge(repos, config_dir):
    G.create("three", [str(repos["appa"]), str(repos["libb"]), str(repos["other"])])
    res = G.link("three")
    assert not [e for e in res["edges"] if e["from_member"] == "appa"]
    amb = [a for a in res["ambiguous"] if a["member"] == "appa"]
    assert amb and all(a["kind"] == "module" and sorted(a["defined_in"]) == ["libb", "other"] for a in amb)
    assert {a["at"] for a in amb} == {"app/main.py:1", "app/main.py:2", "app/main.py:3", "app/main.py:4"}
    assert "ambiguous: appa:app/main.py:1 imports libb.core, defined in" in G.render_show(G.show("three"))


def test_links_are_stale_after_a_member_is_reindexed(tmp_path, config_dir):
    lib = _repo(tmp_path, "libb", PY_LIB)
    app = _repo(tmp_path, "appa", PY_APP)
    G.create("py", [str(app), str(lib)])
    G.link("py")
    assert "stale_members" not in G.show("py")
    (lib / "src/libb/core.py").write_text("\n\n" + PY_LIB["src/libb/core.py"], encoding="utf-8", newline="\n")
    assert "stale_members" not in G.show("py")  # keyed by the graph: the index has not changed yet
    assert cli.main(["update", "--repo", str(lib)]) == 0
    shown = G.show("py")
    assert shown["stale_members"] == ["libb"] and all(e.get("stale") for e in shown["edges"])
    assert "group link py" in shown["hint"]
    tr = G.trace("py", "appa:main", "libb:helper")
    assert tr["stale_members"] == ["libb"] and tr["steps"][-1]["stale"]
    res = G.link("py")
    assert _edges(res)[("app/main.py:8", "compute()")]["definition"] == "src/libb/core.py:3"
    assert "stale_members" not in G.show("py")


def test_trace_and_query_across_members(repos, config_dir):
    G.create("py", [str(repos["appa"]), str(repos["libb"])])
    with pytest.raises(G.GroupError, match="group link py"):
        G.trace("py", "main", "helper")
    G.link("py")
    tr = G.trace("py", "main", "libb:helper")
    assert tr["status"] == "found" and tr["crosses"] == 1
    assert [h["member"] for h in tr["path"]] == ["appa", "libb"]
    assert tr["steps"][0]["call_site"] == "app/main.py:11" and tr["steps"][0]["definition"] == "src/libb/core.py:5"
    two_hops = G.trace("py", "appa:main", "libb:Engine.run")
    assert two_hops["status"] == "found"
    assert G.trace("py", "libb:helper", "appa:main")["status"] == "not_found"
    q = G.query("py", "compute")
    assert {r["member"] for r in q["results"]} == {"appa", "libb"}
    assert "[libb] src/libb/core.py" in G.render_query(q)


def test_group_list_create_and_remove_through_the_cli(repos, config_dir, capsys, tmp_path):
    assert cli.main(["group", "create", "py", str(repos["appa"]), str(repos["libb"])]) == 0
    with pytest.raises(SystemExit, match="exists"):
        cli.main(["group", "create", "py", str(repos["appa"]), str(repos["libb"])])
    with pytest.raises(SystemExit, match="inside member"):
        cli.main(["group", "create", "in", str(repos["appa"]), str(repos["libb"]), "--links-dir",
                  str(repos["appa"] / "links")])
    with pytest.raises(SystemExit, match="two members"):
        cli.main(["group", "create", "one", str(repos["appa"])])
    assert cli.main(["group", "link", "py"]) == 0
    out = capsys.readouterr().out
    assert "appa:app/main.py:8 main() -> libb:src/libb/core.py:1 compute()  [statically_verified]" in out
    assert cli.main(["group", "trace", "py", "main", "libb:helper", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "found"
    assert cli.main(["group", "trace", "py", "libb:helper", "main"]) == 2
    capsys.readouterr()
    assert cli.main(["group", "list", "--json"]) == 0
    assert [g["name"] for g in json.loads(capsys.readouterr().out)["groups"]] == ["py"]
    links = tmp_path / "links"
    assert cli.main(["group", "create", "elsewhere", str(repos["appa"]), str(repos["libb"]), "--links-dir",
                     str(links)]) == 0
    assert cli.main(["group", "link", "elsewhere"]) == 0
    assert (links / "elsewhere.links.json").is_file()
    assert cli.main(["group", "remove", "elsewhere"]) == 0
    assert not (links / "elsewhere.links.json").exists()
    assert cli.main(["group", "query", "py", "helper"]) == 0
    assert "[libb]" in capsys.readouterr().out


def test_group_view_in_a_server_over_the_members(repos, config_dir):
    anyio = pytest.importorskip("anyio")
    from verinoda.mcp import projects as P
    from verinoda.mcp import server as S

    G.create("py", [str(repos["appa"]), str(repos["libb"])])
    G.link("py")
    hub = P.ProjectHub([("appa", repos["appa"]), ("libb", repos["libb"])], profile="full")
    srv = S.build_server(None, hub=hub)
    listed = {t.name for t in anyio.run(srv.list_tools)}
    assert "group_view" in listed

    async def calls():
        show = await srv.call_tool("group_view", {"group": "py"})
        tr = await srv.call_tool("group_view", {"group": "py", "action": "trace", "source": "main",
                                                "target": "libb:helper"})
        return show, tr

    show, tr = anyio.run(calls)
    assert json.loads(show.content[0].text)["counts"]["edges"] == 4
    assert json.loads(tr.content[0].text)["status"] == "found"
    # a server that does not serve every member, or a core server, does not read the group
    one = P.ProjectHub([("appa", repos["appa"]), ("libjs", repos["libjs"])], profile="full")
    res = one.group_view("py")
    assert res["error"] == "group_unavailable" and "libb" in res["message"]
    core = P.ProjectHub([("appa", repos["appa"]), ("libb", repos["libb"])], profile="core")
    assert "group_view" not in {t.name for t in anyio.run(S.build_server(None, hub=core).list_tools)}
    assert core.group_view("py")["error"] == "group_unavailable"
    assert S.AtlasTools(repos["appa"]).group_view("py")["error"] == "group_unavailable"


# -- review round: what is an import of the other member, and what is a call --------------------------------------

SH_A = {  # the importing repository's own non-package modules (pytest rootdir style, a script folder)
    "tests/helpers.py": "def make():\n    return 1\n",
    "tests/test_x.py": "from helpers import make\n\n\ndef test_x():\n    assert make() == 1\n",
    "scripts/common.py": "def run():\n    return 1\n",
    "scripts/go.py": "import common\n\n\ndef main():\n    common.run()\n",
    "use/__init__.py": "",
    "use/u.py": "import solo\nimport helpers\n\n\ndef f():\n    solo.hello()\n    helpers.make()\n",
}
SH_B = {  # root-level scripts are not the distribution's API; the module the distribution is named after is
    "pyproject.toml": '[project]\nname = "solo"\nversion = "0.1"\n',
    "helpers.py": "def make():\n    return 2\n",
    "common.py": "def run():\n    return 2\n",
    "solo.py": "def hello():\n    return 2\n",
}
NS_B = {  # a PEP 420 namespace: acme has no __init__.py
    "pyproject.toml": '[project]\nname = "acme-core"\nversion = "0.1"\n',
    "acme/core/__init__.py": "def boot():\n    return 1\n",
}
NS_A = {
    "nsapp/__init__.py": "",
    "nsapp/run.py": "from acme.core import boot\n\n\ndef main():\n    return boot()\n",
    "core_user/x.py": "import core\n\n\ndef main():\n    return core.boot()\n",
}
SCOPE_B = {
    "libb/__init__.py": "def compute(x):\n    return x\n",
    "libb/client.py": "def get(i):\n    return i\n",
}
SCOPE_A = {
    "app/__init__.py": "",
    "app/m.py": ("from libb import client\n\n\ndef g():\n    from libb import compute\n    return compute(1)\n\n\n"
                 "def compute(x):\n    return x\n\n\ndef h():\n    return compute(2)\n\n\n"
                 "def f(client):\n    return client.get(1)\n\n\ndef k():\n    return client.get(2)\n"),
}
GO_APP_NOISE = {"go.mod": "module example.com/appgo2\n\ngo 1.21\n",
                "main.go": ('package main\n\nimport (\n\t"fmt"\n\t// d "example.com/libgo/calc"\n'
                            '\tc "example.com/libgo/calc"\n)\n\nfunc main() {\n\t// old: c.Add(1, 2)\n'
                            '\tfmt.Println("c.Add(3, 4)")\n\tfmt.Println(`c.Add(5, 6)`)\n\t/* c.Add(7, 8) */\n'
                            '\tfmt.Println(c.Add(1, 2))\n}\n')}
JAVA_APP_NOISE = {"src/main/java/com/shop/App.java":
                  ("package com.shop;\n\nimport com.acme.util.Strings;\n\npublic class App {\n"
                   "    public static void main(String[] args) {\n        // Strings.shout(\"old\");\n"
                   "        System.out.println(\"Strings.shout(x)\");\n        String b = \"\"\"\n"
                   "            Strings.shout(y)\n            \"\"\";\n        char q = '(';\n"
                   "        System.out.println(Strings.shout(\"hi\"));\n    }\n}\n")}
JS_B = {
    "package.json": ('{"name": "@acme/b", "exports": {".": {"import": "./src/index.ts"}, '
                     '"./utils": "./src/helpers/u.ts", "./feat/*": "./src/features/*.ts"}}\n'),
    "src/index.ts": "export * from './star';\nexport function top(): number {\n  return 1;\n}\n",
    "src/star.ts": "export function starred(): number {\n  return 2;\n}\n",
    "src/helpers/u.ts": "export function fmt(n: number): string {\n  return String(n);\n}\n",
    "src/utils-legacy.ts": "export function fmt(n: number): string {\n  return 'old' + n;\n}\n",
    "src/features/pay.ts": "export function pay(): number {\n  return 3;\n}\n",
    "test/t.ts": "export function only(): number {\n  return 4;\n}\n",
}
JS_A = {
    "package.json": '{"name": "js-a"}\n',
    "src/a.ts": ("import { fmt } from '@acme/b/utils';\nimport { top, only, starred } from '@acme/b';\n"
                 "const s = 'fmt(1)';\nconst t = `x ${fmt(2)} fmt(3)`;\nconst r = /fmt\\(/;\n"
                 "export function go() {\n  fmt(4);\n  only();\n  top();\n  return starred();\n}\n"
                 "import { pay } from '@acme/b/feat/pay';\nexport const p = () => pay();\n"
                 "const d = \"top()\";\n"),
}


@pytest.fixture(scope="module")
def review_repos(tmp_path_factory, repos) -> dict[str, Path]:
    base = tmp_path_factory.mktemp("review")
    out = {n: _repo(base, n, f) for n, f in (("sh_a", SH_A), ("sh_b", SH_B), ("ns_a", NS_A), ("ns_b", NS_B),
                                              ("scope_a", SCOPE_A), ("scope_b", SCOPE_B),
                                              ("goapp2", GO_APP_NOISE), ("javaapp2", JAVA_APP_NOISE),
                                              ("js_a", JS_A), ("js_b", JS_B))}
    return {**repos, **out}


def _sites(res: dict) -> set[tuple[str, str, str]]:
    return {(e["call_site"], e["callee"], e["definition"]) for e in res["edges"]}


def test_the_importing_members_own_modules_are_not_links_and_root_scripts_are_not_api(review_repos, config_dir):
    G.create("sh", [str(review_repos["sh_a"]), str(review_repos["sh_b"])])
    res = G.link("sh")
    # tests/helpers.py and scripts/common.py are sh_a's own (each sits next to its importer); sh_b's root-level
    # helpers.py and common.py are scripts, while solo.py is the module the distribution is named after
    assert _sites(res) == {("use/u.py:6", "solo.hello", "solo.py:1")} and len(res["edges"]) == 1
    assert res["members"]["sh_b"]["defines"]["python"] == ["solo"]


def test_a_namespace_package_is_named_from_its_namespace_folder(review_repos, config_dir):
    G.create("ns", [str(review_repos["ns_a"]), str(review_repos["ns_b"])])
    res = G.link("ns")
    assert _sites(res) == {("nsapp/run.py:5", "boot", "acme/core/__init__.py:1")} and len(res["edges"]) == 1
    assert res["edges"][0]["status"] == "statically_verified"
    assert res["members"]["ns_b"]["defines"]["python"] == ["acme"]


def test_python_bindings_follow_scopes_and_shadowing(review_repos, config_dir):
    G.create("sc", [str(review_repos["scope_a"]), str(review_repos["scope_b"])])
    res = G.link("sc")
    # g's own import links; h calls the module's def compute; f's parameter shadows the module's import
    assert _sites(res) == {("app/m.py:6", "compute", "libb/__init__.py:1"),
                           ("app/m.py:22", "client.get", "libb/client.py:1")} and len(res["edges"]) == 2


def test_py_uses_scopes():
    imports, calls = G.py_uses("import x\n\nclass C:\n    x = 1\n    def m(self):\n        return x.f()\n"
                               "\n\ndef g():\n    for x in []:\n        x.f()\n    return [x.f() for x in ()]\n")
    by_line = {c["line"]: c["import"] for c in calls}
    assert by_line == {6: 0, 11: None, 12: None}  # a class attribute is not seen from its methods


@pytest.mark.parametrize("pair, site, definition", [
    (("goapp2", "libgo"), "main.go:14", "calc/calc.go:3"),
    (("javaapp2", "libjava"), "src/main/java/com/shop/App.java:13", "src/main/java/com/acme/util/Strings.java:4"),
])
def test_go_and_java_calls_in_comments_and_literals_are_not_links(review_repos, config_dir, pair, site,
                                                                   definition):
    G.create("noise", [str(review_repos[pair[0]]), str(review_repos[pair[1]])])
    res = G.link("noise")
    assert [(e["call_site"], e["definition"], e["status"]) for e in res["edges"]] == \
        [(site, definition, "statically_verified")]
    assert res["counts"]["imports"] == 1  # the commented-out Go import is not read


def test_javascript_literals_exports_map_and_entry_without_the_name(review_repos, config_dir):
    G.create("jsx", [str(review_repos["js_a"]), str(review_repos["js_b"])])
    res = G.link("jsx")
    # the strings and the regular expression are not calls, a template's ${...} is code; './utils' is the
    # exports map's file, not src/utils-legacy.ts; `only` is not exported by the entry (test/t.ts is not it);
    # starred comes through `export * from './star'`; './feat/*' is a pattern
    sites = {(e["call_site"], e["definition"]) for e in res["edges"]}
    assert sites == {("src/a.ts:4", "src/helpers/u.ts:1"), ("src/a.ts:7", "src/helpers/u.ts:1"),
                     ("src/a.ts:9", "src/index.ts:2"), ("src/a.ts:10", "src/star.ts:1"),
                     ("src/a.ts:13", "src/features/pay.ts:1")}
    assert len(res["edges"]) == 5
    assert any(u["callee"] == "only" for u in res["unresolved_sample"])


def test_blank_js_literals_keeps_code_and_lines():
    src = "a('x(') + `t ${b(1)} c(` + /d\\(/.test(s)\n'e(\nf()"
    out = G._blank_js_literals(src)
    assert len(out) == len(src) and out.count("\n") == src.count("\n")
    assert "a(" in out and "b(1)" in out and "f()" in out
    assert "x(" not in out and "c(" not in out and "d\\(" not in out and "e(" not in out


@pytest.mark.parametrize("name", ["CON", "nul", "com1", "LPT9", "aux.links"])
def test_windows_device_names_are_refused(repos, config_dir, name):
    with pytest.raises(G.GroupError, match="device name"):
        G.create(name, [str(repos["appa"]), str(repos["libb"])])


def test_a_call_site_edited_since_link_is_marked_stale(tmp_path, config_dir):
    lib = _repo(tmp_path, "libb", PY_LIB)
    app = _repo(tmp_path, "appa", PY_APP)
    G.create("py", [str(app), str(lib)])
    G.link("py")
    main = app / "app/main.py"
    main.write_text(PY_APP["app/main.py"].replace("a = compute(3)", "a = 3"), encoding="utf-8", newline="\n")
    shown = G.show("py")
    changed = [e["call_site"] for e in shown["edges"] if e.get("call_site_changed")]
    assert changed == ["app/main.py:8"] and "group link py" in shown["hint"]
    assert "STALE" in G.render_show(shown)
    tr = G.trace("py", "appa:main", "libb:compute")
    assert tr["status"] == "found" and tr["steps"][-1]["stale"]


def test_a_hub_answers_group_view_through_a_member_of_the_group(repos, config_dir):
    from verinoda.mcp import projects as P

    G.create("py", [str(repos["appa"]), str(repos["libb"])])
    G.link("py")
    hub = P.ProjectHub([("libjs", repos["libjs"]), ("libb", repos["libb"]), ("appa", repos["appa"])],
                       profile="full")
    seen = []
    real = hub._get

    def get(name):
        seen.append(name)
        return real(name)
    hub._get = get
    assert hub.group_view("py")["counts"]["edges"] == 4
    assert seen == ["libb"]  # not libjs, the first served project, which is no member
