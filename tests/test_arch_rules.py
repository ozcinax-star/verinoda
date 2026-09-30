"""Architecture rules as decision guards (docs/DESIGN.md D95): layers, allowed dependencies, public interfaces
and tags, checked by ``decide check`` over the graph's edges, each violation at its call site."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import json  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402
from types import SimpleNamespace  # noqa: E402

import pytest  # noqa: E402

from verinoda import decision_reach, guards, index, workflow  # noqa: E402
from verinoda import decisions as dm  # noqa: E402
from verinoda.store import open_store  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")

FILES = {
    "app/__init__.py": "",
    "app/ui/__init__.py": "",
    "app/ui/views.py": "from app.core.service import place\nfrom orders.api import submit\n\n\n"
                       "def show():\n    return place(1), submit(2)\n",
    "app/core/__init__.py": "",
    "app/core/service.py": "from app.db.store import save\n\n\ndef place(x):\n    return save(x)\n",
    "app/db/__init__.py": "",
    "app/db/store.py": "def save(x):\n    return x\n",
    "orders/__init__.py": "",
    "orders/api.py": "from orders._impl import run\n\n\ndef submit(x):\n    return run(x)\n",
    "orders/_impl.py": "def run(x):\n    return x\n",
    # a test reaches into the module's internals: test code, out of public's and allow_edges' default scope
    "tests/test_orders.py": "from orders._impl import run\n\n\ndef test_run():\n    assert run(1) == 1\n",
}


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false",
                    "-c", "commit.gpgsign=false", *args], cwd=cwd, check=True, capture_output=True)


def _write(repo: Path, rel: str, text: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8"))


def _repo(tmp_path: Path, extra: dict[str, str] | None = None) -> Path:
    repo = tmp_path / "layered"
    for rel, text in {**FILES, **(extra or {})}.items():
        _write(repo, rel, text)
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    workflow.init(repo)
    st = open_store(repo)
    try:
        workflow.scan(st, repo)
    finally:
        st.close()
    return repo


def _rec(repo: Path, *specs: str) -> dm.Decision:
    return dm.Decision(id="ADR-0007", number=7, title="t",
                       guards=[dm.parse_guard(s, repo, f"g{i}") for i, s in enumerate(specs, 1)])


def _check(repo: Path, *specs: str) -> dict:
    return guards.check(repo, graph=index.load(repo), records=[_rec(repo, *specs)])


@pytest.fixture(scope="module")
def clean(tmp_path_factory):
    return _repo(tmp_path_factory.mktemp("arch"))


@pytest.fixture(scope="module")
def broken(tmp_path_factory):
    # the data layer reaches up into the ui; the ui reaches past the orders api into its internals
    return _repo(tmp_path_factory.mktemp("arch"), {
        "app/db/store.py": "from app.ui.views import show\n\n\ndef save(x):\n    return x or show()\n",
        "app/ui/extra.py": "from orders._impl import run\n\n\ndef go():\n    return run(3)\n",
    })


LAYERS = "layers order=app/ui/**,app/core/**,app/db/**"


def test_layers_hold_downward_and_fail_upward_at_the_import_line(clean, broken):
    res = _check(clean, LAYERS)
    assert res["exit"] == 0 and res["status"] == "ok", res
    (ok,) = res["ok"]
    assert ok["kind"] == "layers" and ok["scope"]["edges_checked"] > 0 and ok["scope"]["layer_3_files"] == 2
    res = _check(broken, LAYERS)
    assert res["exit"] == 1 and res["status"] == "violated"
    v = res["violations"][0]
    assert v["at"] == "app/db/store.py:1" and v["line"].startswith("from app.ui.views import show")
    assert v["status"] == "statically_verified" and v["kind"] == "layers"
    assert "layer 3 (app/db/**) may not depend on layer 1 (app/ui/**) above it" in v["why"]
    assert {x["at"].partition(":")[0] for x in res["violations"]} == {"app/db/store.py"}


def test_allow_edges_names_the_edge_outside_the_allowed_files(clean):
    assert _check(clean, "allow_edges from=app/ui/** allowed=app/core/**,orders/api.py")["exit"] == 0
    res = _check(clean, "allow_edges from=app/ui/** allowed=app/core/**")
    assert res["exit"] == 1
    # the import and the call site, each at its own line
    assert {v["at"] for v in res["violations"]} == {"app/ui/views.py:2", "app/ui/views.py:6"}
    assert "app/ui/** may use only itself and app/core/**" in res["violations"][0]["why"]
    # an allowed glob that matches no file is a limit, not a reason to stop
    res = _check(clean, "allow_edges from=app/ui/** allowed=app/core/**,orders/api.py,app/gone/**")
    assert res["exit"] == 0 and any("app/gone/** matches no file" in x for x in res["ok"][0]["limits"])


def test_public_api_is_the_only_way_in(clean, broken):
    spec = "public module=orders/** api=orders/api.py,orders/__init__.py"
    res = _check(clean, spec)
    assert res["exit"] == 0 and res["ok"][0]["scope"]["module_files"] == 3
    res = _check(broken, spec)
    assert res["exit"] == 1 and {v["at"].partition(":")[0] for v in res["violations"]} == {"app/ui/extra.py"}
    assert "app/ui/extra.py:1" in {v["at"] for v in res["violations"]}
    assert "orders/** is reached only through orders/api.py" in res["violations"][0]["why"]


def test_test_code_is_out_of_public_and_allow_edges_unless_scope_all(clean):
    spec = "public module=orders/** api=orders/api.py,orders/__init__.py"
    res = _check(clean, spec)
    assert res["exit"] == 0 and any("1 test file(s) outside the module are out of scope" in x
                                    for x in res["ok"][0]["limits"])
    res = _check(clean, spec + " scope=all")
    assert res["exit"] == 1 and {v["at"] for v in res["violations"]} >= {"tests/test_orders.py:1"}
    # from files that are all test code: nothing is checked by default, never ok
    res = _check(clean, "allow_edges from=tests/** allowed=orders/api.py")
    assert res["exit"] == 3 and "all test code" in res["unknown"][0]["why"]
    res = _check(clean, "allow_edges from=tests/** allowed=orders/api.py scope=all")
    assert res["exit"] == 1 and res["violations"][0]["at"] == "tests/test_orders.py:1"


def test_api_files_outside_the_module_leave_it_unknown(clean):
    res = _check(clean, "public module=orders/** api=app/core/service.py")
    assert res["exit"] == 3 and not res["violations"]
    assert "matches no file inside module=orders/**" in res["unknown"][0]["why"]
    # one api file inside and one outside: checked, the stray one is a limit
    res = _check(clean, "public module=orders/** api=orders/api.py,orders/__init__.py,app/core/service.py")
    assert res["exit"] == 0 and any("1 api file(s) lie outside" in x for x in res["ok"][0]["limits"])


def test_a_layer_left_with_no_file_of_its_own_is_unknown(broken):
    res = _check(broken, "layers order=app/**,app/db/**")
    assert res["exit"] == 3 and "layer 2=app/db/** has no file of its own" in res["unknown"][0]["why"]
    res = _check(broken, "layers order=app/**,app/**")
    assert res["exit"] == 3
    # overlapping layers that each keep files: a file counts once, in the highest layer it matches
    res = _check(broken, "layers order=app/ui/**,app/**")
    assert res["exit"] == 1 and res["violations"][0]["at"] == "app/db/store.py:1"


def test_tags_from_verinoda_toml_name_file_sets(broken):
    toml = broken / "verinoda.toml"
    toml.write_text('[architecture.tags]\nui = ["app/ui/**"]\ncore = "app/core/**"\ndata = ["app/db/**"]\n',
                    encoding="utf-8")
    try:
        res = _check(broken, "layers order=tag:ui,tag:core,tag:data")
        assert res["exit"] == 1 and res["violations"][0]["at"] == "app/db/store.py:1"
        res = _check(broken, "no_edge from=tag:data to=tag:ui")
        assert res["exit"] == 1 and res["violations"][0]["at"] == "app/db/store.py:1"
        # a tag that is not defined: nothing was checked, never ok
        res = _check(broken, "layers order=tag:ui,tag:nope")
        assert res["exit"] == 3 and "no tag 'nope' in verinoda.toml [architecture.tags]" in res["unknown"][0]["why"]
    finally:
        toml.unlink()


def test_a_rule_that_would_look_at_nothing_is_unknown(clean):
    res = _check(clean, "layers order=app/ui/**,app/gone/**")
    assert res["exit"] == 3 and "layer 2=app/gone/** matches no file" in res["unknown"][0]["why"]
    res = _check(clean, "public module=orders/** api=orders/missing.py")
    assert res["exit"] == 3 and "api=orders/missing.py matches no file" in res["unknown"][0]["why"]
    res = guards.check(clean, graph=None, records=[_rec(clean, LAYERS)])
    assert res["exit"] == 3 and "layers reads the graph's edges" in res["unknown"][0]["why"]


def test_specs_are_validated(tmp_path):
    for spec, msg in (("layers order=app/**", "two layers or more"),
                      ("allow_edges from=app/**", "allowed=GLOB"),
                      ("public module=orders/**", "api=GLOB"),
                      ("layers order=../x,y", "inside the repository"),
                      ("public module=a api=b relations=bogus", "not graph relations"),
                      ("public module=a api=b scope=tests", "scope must be product")):
        with pytest.raises(dm.DecisionError, match=msg):
            dm.parse_guard(spec, tmp_path, "g1")
    g = dm.parse_guard("layers order=a/**,b/** relations=imports", tmp_path, "g1")
    assert g["order"] == ["a/**", "b/**"] and g["relations"] == ["imports", "imports_from"] and "scope" not in g
    assert dm.parse_guard("public module=a api=b", tmp_path, "g1")["scope"] == "product"


def test_architecture_tags_report_bad_entries(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[tool.verinoda.architecture.tags]\nui = ["src/ui/**"]\nbad = 3\n'
                                             'out = ["../x"]\n', encoding="utf-8")
    tags, where, problems = dm.architecture_tags(tmp_path)
    assert tags == {"ui": ["src/ui/**"]} and where == ["pyproject.toml [tool.verinoda.architecture.tags]"]
    assert len(problems) == 2


def test_decide_check_fails_ci_on_a_layer_violation(broken, capsys):
    from verinoda import cli

    repo = broken
    (repo / "verinoda.toml").write_text('[decisions]\ndir = "docs/decisions"\n', encoding="utf-8")
    st = open_store(repo)
    try:
        dm.record(st, repo, chosen="three layers", rationale="the ui sits on top", guards=[LAYERS])
    finally:
        st.close()
    try:
        assert cli.main(["decide", "check", "--repo", str(repo), "--json"]) == 1
        res = json.loads(capsys.readouterr().out)
        assert res["violations"][0]["at"] == "app/db/store.py:1"
        assert cli.main(["decide", "check", "--repo", str(repo)]) == 1
        out = capsys.readouterr().out
        assert "VIOLATED ADR-0001 g1 layers app/ui/** > app/core/** > app/db/**" in out
        assert "app/db/store.py:1 from app.ui.views import show" in out
        # review lists the record a change to a layer's file reaches
        text = (repo / "app/db/store.py").read_text(encoding="utf-8")
        diff = SimpleNamespace(rel="app/db/store.py", old=text, new=text.replace("x or show()", "show() or x"))
        reach = decision_reach.reached(repo, [], [diff])
        (rec,) = reach["records"]
        assert any("matches order=app/db/**" in h["why"] and h["status"] == "statically_verified"
                   for h in rec["reached_by"]), rec
    finally:
        shutil.rmtree(repo / "docs")
        (repo / "verinoda.toml").unlink()


def test_review_names_a_rule_whose_tag_changes(clean):
    repo = clean
    tags = '[architecture.tags]\nui = ["app/ui/**"]\ndata = "app/db/**"\n'
    (repo / "verinoda.toml").write_text(tags + '\n[decisions]\ndir = "docs/decisions"\n', encoding="utf-8")
    st = open_store(repo)
    try:
        dm.record(st, repo, chosen="no ui from data", rationale="t", guards=["no_edge from=tag:data to=tag:ui"])
    finally:
        st.close()
    try:
        text = (repo / "verinoda.toml").read_text(encoding="utf-8")
        diff = SimpleNamespace(rel="verinoda.toml", old=text, new=text.replace('"app/ui/**"', '"app/nothing/**"'))
        (rec,) = decision_reach.reached(repo, [], [diff])["records"]
        (hit,) = [h for h in rec["reached_by"] if "changes tag:ui" in h["why"]]
        assert hit["at"] == "verinoda.toml:2" and hit["status"] == "statically_verified"
        assert not any("changes tag:data" in h["why"] for h in rec["reached_by"])
        # an edit that leaves the tags as they were reaches nothing through them
        diff = SimpleNamespace(rel="verinoda.toml", old=text, new=text + "# a comment\n")
        assert not decision_reach.reached(repo, [], [diff])["records"]
    finally:
        shutil.rmtree(repo / "docs")
        (repo / "verinoda.toml").unlink()
