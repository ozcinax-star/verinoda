"""Name-existence check (docs/DESIGN.md D31): closed-world rule, guards, keywords, dict keys, nearest
names, the environment choice, the cache, the CLI/MCP wiring and the no-jedi degradation.

Most tests use ``env="none"`` (the standard library of the running interpreter, no project
environment) so no virtual environment has to be built; one test builds a small project .venv
(``python -m venv --without-pip`` plus a hand-written package) for the environment choice.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import pytest  # noqa: E402

from verinoda import codecheck, precise  # noqa: E402
from verinoda import codecheck_env as cenv  # noqa: E402

jedi = pytest.importorskip("jedi", reason="optional extra 'precise' (jedi) not installed")

CORE = '''\
import functools


class Repo:
    def __init__(self, url=":memory:"):
        self.url = url

    def save(self, customer, total):
        return 1

    def get(self, order_id):
        return None


class Dyn:
    def __getattr__(self, name):
        return name


def register(obj):
    obj.extra = 1


def inspect_only(obj):
    return obj.url


class Leaky:
    def __init__(self):
        register(self)


class Reads:
    def __init__(self):
        self.url = "x"
        inspect_only(self)


class Slotted:
    __slots__ = ("a",)

    def __init__(self):
        self.a = 1
        register(self)


def compute(items, *, currency="EUR"):
    return 0


def flexible(a, **kwargs):
    return a


def po(a, /, b):
    return a


def logged(fn):
    return fn


@logged
def wrapped(x, y=1):
    return x


@functools.lru_cache(maxsize=None)
def cached(x, y=1):
    return x


def settings():
    return {"database_url": "x", "max_items": 3}


def mixed(flag):
    if flag:
        return {"a": 1}
    return dict(b=2)
'''

LAZY = '''\
def __getattr__(name):
    if name == "magic":
        return 42
    raise AttributeError(name)
'''

USE = '''\
import json
import sys
from typing import TYPE_CHECKING

from pkg import lazymod
from pkg.core import (Dyn, Leaky, Reads, Repo, Slotted, compute, flexible, cached, mixed, po, settings,
                      wrapped)
from pkg.core import compute_totl
from pkg.helpers import nothing
import definitely_missing_xyz

try:
    import ujson_not_here
except ImportError:
    ujson_not_here = None

try:
    import tomllib as toml_impl
except ImportError:
    import fallback_not_here as toml_impl

if TYPE_CHECKING:
    from pkg.core import Nope

if sys.version_info >= (3, 99):
    from itertools import not_yet


def use(r: Repo, other):
    a = Repo.saev
    b = Repo().fetch
    local = Repo()
    c = local.fetch
    d = Repo().url
    e = r.fetch
    f = Dyn().anything
    g = lazymod.anything
    h = json.loadz
    i = json.loads
    j = "x".starts_with
    k = "x".startswith
    changed = Repo()
    changed.extra = 1
    m = changed.extra
    n = Leaky().extra
    o = Reads().extra
    p = Slotted().extra
    if hasattr(json, "nope"):
        q = json.nope
    compute([], currency="USD")
    compute([], curency="USD")
    flexible(1, anything=2)
    Repo(url="x")
    Repo(path="x")
    Repo().save(customer="a", total=1)
    Repo().save("a", 1, commit=True)
    sorted([], reverse=True)
    sorted([], descending=True)
    wrapped(1, z=2)
    cached(1, z=2)
    po(a=1, b=2)
    s1 = settings()["database_url"]
    s2 = settings()["database_uri"]
    st = settings()
    s3 = st["max_item"]
    grown = settings()
    grown["x"] = 1
    s4 = grown["x"]
    s5 = mixed(True)["a"]
    return a, b, c, d, e, f, g, h, i, j, k, m, n, o, p, q, s1, s2, s3, s4, s5, other
'''


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(text), encoding="utf-8", newline="\n")


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args],
                   cwd=cwd, check=True, capture_output=True)


@pytest.fixture(scope="module")
def proj(tmp_path_factory):
    root = tmp_path_factory.mktemp("cc")
    _write(root, "pkg/__init__.py", "")
    _write(root, "pkg/core.py", CORE)
    _write(root, "pkg/lazymod.py", LAZY)
    _write(root, "pkg/use.py", USE)
    _write(root, "pyproject.toml", '[project]\nname = "demo"\nversion = "0"\ndependencies = ["declaredpkg>=1"]\n')
    yield root
    codecheck.reset_caches()


@pytest.fixture(scope="module")
def result(proj):
    return codecheck.check(proj, ["pkg/use.py"], env="none", include_exists=True, use_cache=False)


def core_line(text: str) -> int:
    return next(i for i, ln in enumerate(CORE.splitlines(), 1) if ln.startswith(text))


def site(res: dict, line_text: str, name: str, kind: str | None = None) -> dict:
    """The site named ``name`` on the line of pkg/use.py that contains ``line_text``."""
    want = [i for i, ln in enumerate(USE.splitlines(), 1) if line_text in ln]
    assert want, line_text
    hits = [s for s in res["sites"] if s["path"] == "pkg/use.py" and s["line"] in want and s["name"] == name
            and (kind is None or s["kind"] == kind)]
    assert hits, (line_text, name, [s for s in res["sites"] if s["line"] in want])
    return hits[0]


# -- the closed-world rule ---------------------------------------------------------------------------

def test_absent_needs_a_closed_container(result):
    assert site(result, "Repo.saev", "saev")["verdict"] == "absent"                 # class object
    b = site(result, "b = Repo().fetch", "fetch")                                    # direct instance
    assert b["verdict"] == "absent" and "not found in instance of class pkg.core.Repo in this project" in \
        b["message"] and "does not exist" not in b["message"]
    assert "get" in [n["name"] for n in b["nearest"]]                                # synonym fetch -> get
    assert site(result, "c = local.fetch", "fetch")["verdict"] == "absent"           # single unreassigned local
    assert site(result, "d = Repo().url", "url")["verdict"] == "exists"              # set in __init__
    assert site(result, "i = json.loads", "loads")["verdict"] == "exists"
    assert site(result, "h = json.loadz", "loadz")["verdict"] == "absent"            # stdlib module
    assert site(result, 'j = "x".starts_with', "starts_with")["verdict"] == "absent"  # builtin instance
    assert site(result, 'k = "x".startswith', "startswith")["verdict"] == "exists"


def test_open_receivers_are_unknown_never_absent(result):
    e = site(result, "e = r.fetch", "fetch")
    assert e["verdict"] == "unknown" and "parameter" in e["why"]                     # annotated parameter
    f = site(result, "f = Dyn().anything", "anything")
    assert f["verdict"] == "unknown" and "__getattr__" in f["why"]
    g = site(result, "g = lazymod.anything", "anything")
    assert g["verdict"] == "unknown" and "__getattr__" in g["why"]                   # module __getattr__
    m = site(result, "m = changed.extra", "extra")
    assert m["verdict"] == "unknown" and "attributes are set" in m["why"]
    n = site(result, "n = Leaky().extra", "extra")
    assert n["verdict"] == "unknown" and "register" in n["why"]                      # self handed to a setter
    assert site(result, "o = Reads().extra", "extra")["verdict"] == "absent"         # handed to a reader only
    assert site(result, "p = Slotted().extra", "extra")["verdict"] == "absent"       # no __dict__ at all


def test_imports(result):
    t = site(result, "from pkg.core import compute_totl", "compute_totl")
    assert t["verdict"] == "absent" and t["nearest"][0]["name"] == "compute"
    assert t["nearest"][0]["at"] == f"pkg/core.py:{core_line('def compute(')}"
    h = site(result, "from pkg.helpers import nothing", "helpers")
    assert h["verdict"] == "absent" and "no module helpers in package pkg" in h["message"]
    # no project environment: a third-party module is never absent
    x = site(result, "import definitely_missing_xyz", "definitely_missing_xyz")
    assert x["verdict"] == "not_installed" and "third-party names are not checked" in x["why"]


def test_guarded_imports_and_attributes(result):
    u = site(result, "import ujson_not_here", "ujson_not_here")
    assert u["verdict"] == "guarded" and "ImportError" in u["guard"]
    fb = site(result, "import fallback_not_here", "fallback_not_here")
    assert fb["verdict"] == "guarded" and "fallback import" in fb["guard"]
    tc = site(result, "from pkg.core import Nope", "Nope")
    assert tc["verdict"] == "guarded" and tc["verdict_unguarded"] == "absent" and "TYPE_CHECKING" in tc["guard"]
    v = site(result, "from itertools import not_yet", "not_yet")
    assert v["verdict"] == "guarded" and "version_info" in v["guard"]
    q = site(result, "q = json.nope", "nope")
    assert q["verdict"] == "guarded" and "hasattr" in q["guard"]


def test_keyword_arguments(result):
    assert site(result, 'compute([], currency="USD")', "currency", "kwarg")["verdict"] == "exists"
    bad = site(result, 'compute([], curency="USD")', "curency", "kwarg")
    assert bad["verdict"] == "absent" and bad["nearest"][0]["name"] == "currency"
    assert "compute(items, *, currency='EUR')" in bad["message"]
    assert site(result, "flexible(1, anything=2)", "anything", "kwarg")["verdict"] == "unknown"   # **kwargs
    assert site(result, 'Repo(url="x")', "url", "kwarg")["verdict"] == "exists"
    assert site(result, 'Repo(path="x")', "path", "kwarg")["verdict"] == "absent"                # __init__
    assert site(result, 'save(customer="a", total=1)', "customer", "kwarg")["verdict"] == "exists"
    assert site(result, "commit=True", "commit", "kwarg")["verdict"] == "absent"                 # bound method
    assert site(result, "reverse=True", "reverse", "kwarg")["verdict"] == "exists"               # builtin
    assert site(result, "descending=True", "descending", "kwarg")["verdict"] == "absent"
    w = site(result, "wrapped(1, z=2)", "z", "kwarg")
    assert w["verdict"] == "unknown" and "@logged" in w["why"]                                   # unknown decorator
    assert site(result, "cached(1, z=2)", "z", "kwarg")["verdict"] == "absent"                   # lru_cache keeps it
    p = site(result, "po(a=1, b=2)", "a", "kwarg")
    assert p["verdict"] == "absent" and "positional-only" in p["message"]


def test_dict_keys_of_functions_returning_literals(result):
    assert site(result, 's1 = settings()["database_url"]', "database_url", "dict_key")["verdict"] == "exists"
    bad = site(result, 's2 = settings()["database_uri"]', "database_uri", "dict_key")
    assert bad["verdict"] == "absent" and bad["nearest"][0]["name"] == "database_url"
    assert "returned by settings()" in bad["message"]
    assert site(result, 's3 = st["max_item"]', "max_item", "dict_key")["verdict"] == "absent"   # one local
    grown = site(result, 's4 = grown["x"]', "x", "dict_key")
    assert grown["verdict"] == "unknown" and "keys are added" in grown["why"]
    assert not [s for s in result["sites"] if s["kind"] == "dict_key" and s["name"] == "a"]    # not all literals


def test_report_shape_and_exit(result):
    assert result["exit"] == 3
    env = result["env"]
    assert env["kind"] == "verinoda" and env["third_party_checked"] is False and env["note"]
    counts = result["summary"]
    assert counts["sites"] == sum(counts[v] for v in codecheck.VERDICTS)
    for s in result["sites"]:
        assert s["verdict"] in codecheck.VERDICTS and s["kind"] in codecheck.SITE_KINDS
        if s["verdict"] == "absent":
            assert s["message"].startswith(("not found", "keyword", "key ", "no module", "module "))
        if s["verdict"] == "unknown":
            assert s.get("why")


def test_nearest_ranks_near_names_and_synonyms():
    from verinoda.codecheck_facts import Member

    names = {n: Member(n, "function") for n in ("place_order", "fetch_order", "validate_items", "__init__",
                                                  "_private_place")}
    got = [n["name"] for n in codecheck.nearest("place_orders", names)]
    assert got[0] == "place_order" and "__init__" not in got
    assert [n["name"] for n in codecheck.nearest("get_order", names)][0] == "fetch_order"   # get ~ fetch
    assert codecheck.nearest("zzz", names) == []


# -- environment ----------------------------------------------------------------------------------------

def _make_venv(root: Path) -> Path:
    venv = root / ".venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True, capture_output=True)
    site_dirs = [venv / "Lib" / "site-packages"] if os.name == "nt" else \
        sorted((venv / "lib").glob("python*/site-packages"))
    site_pkgs = site_dirs[0]
    _write(site_pkgs, "fancylib/__init__.py", "def real_fn(x, *, retries=3):\n    return x\n")
    _write(site_pkgs, "fancylib-2.1.0.dist-info/METADATA", "Metadata-Version: 2.1\nName: fancylib\nVersion: 2.1.0\n")
    _write(site_pkgs, "fancylib-2.1.0.dist-info/RECORD", "fancylib/__init__.py,,\n")
    return venv


def test_project_environment_is_used_and_third_party_is_judged_there(tmp_path):
    _make_venv(tmp_path)
    _write(tmp_path, "app.py", "import fancylib\n\nfancylib.real_fn(1, retries=2)\nfancylib.fake_fn(1)\n"
                               "fancylib.real_fn(1, retry=2)\nimport not_anywhere_xyz\n")
    _write(tmp_path, "pyproject.toml", '[project]\nname = "a"\nversion = "0"\ndependencies = ["declaredpkg"]\n')
    _write(tmp_path, "b.py", "import declaredpkg\n")
    res = codecheck.check(tmp_path, ["app.py", "b.py"], include_exists=True, use_cache=False)
    env = res["env"]
    assert env["kind"] == "project" and env["python"].startswith(".venv (python ")
    assert env["packages_checked"] == {"fancylib": "2.1.0 installed"}
    by = {(s["line"], s["name"], s["kind"]): s for s in res["sites"] if s["path"] == "app.py"}
    assert by[(3, "real_fn", "attribute")]["source"] == "installed:fancylib 2.1.0"
    fake = by[(4, "fake_fn", "attribute")]
    assert fake["verdict"] == "absent" and "as installed in .venv" in fake["message"]
    assert fake["nearest"][0]["name"] == "real_fn"
    assert by[(5, "retry", "kwarg")]["verdict"] == "absent"
    assert by[(6, "not_anywhere_xyz", "import")]["verdict"] == "absent"
    decl = [s for s in res["sites"] if s["path"] == "b.py"][0]
    assert decl["verdict"] == "not_installed" and "pyproject.toml" in decl["why"]
    # the same code without the project environment: never absent for third-party names
    none = codecheck.check(tmp_path, ["app.py"], env="none", include_exists=True, use_cache=False)
    assert {s["verdict"] for s in none["sites"] if "fancylib" in s["expr"] or s["name"] == "not_anywhere_xyz"} \
        <= {"not_installed", "unknown"}
    codecheck.reset_caches()


def test_environment_choice_fallbacks(tmp_path):
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "pyvenv.cfg").write_text("home = /nowhere\n", encoding="utf-8")
    env = cenv.select_env(tmp_path, "auto")
    assert env.kind == "verinoda" and not env.third_party and ".venv/ was found but not used" in env.note
    assert cenv.select_env(tmp_path, "none").kind == "verinoda"
    with pytest.raises(ValueError, match="not a usable Python environment"):
        cenv.select_env(tmp_path, str(tmp_path / "no-such-env"))
    env.oracle().close()


def test_stdlib_names_come_from_the_interpreter_not_from_stubs(tmp_path):
    env = cenv.own_env("test")
    info = env.oracle().ask("module", name="json")
    assert info["ok"] and "loads" in info["names"] and not info["getattr"]
    assert env.oracle().ask("module", name="not_a_stdlib_module_xyz")["ok"] is False
    assert env.oracle().ask("module", name="antigravity")["ok"] is False   # never imported
    cls = env.oracle().ask("object", name="builtins.str")
    assert cls["kind"] == "class" and "startswith" in cls["names"] and not cls["inst_getattr"]
    env.oracle().close()


# -- no jedi ----------------------------------------------------------------------------------------------

def test_without_jedi_every_site_is_unknown(proj, monkeypatch):
    why = "jedi is not installed (pip install 'verinoda[precise]')"
    monkeypatch.setattr(precise, "available", lambda: (False, why))
    res = codecheck.check(proj, ["pkg/use.py"], include_exists=True, use_cache=False)
    assert res["exit"] == 0 and res["sites"]
    assert {s["verdict"] for s in res["sites"]} == {"unknown"}
    assert all("jedi is not installed" in s["why"] for s in res["sites"])
    assert res["env"]["third_party_checked"] is False
    api = codecheck.api(proj, "pkg.core.Repo")
    assert api["found"] is False and "jedi is not installed" in api["why"]


# -- api --------------------------------------------------------------------------------------------------

def test_api_lists_real_members(proj):
    r = codecheck.api(proj, "pkg.core.Repo", env="none")
    names = {m["name"]: m for m in r["members"]}
    assert r["found"] and r["kind"] == "class" and {"save", "get", "url", "__init__"} <= set(names)
    assert names["save"]["signature"] == "save(self, customer, total)"
    assert names["save"]["at"] == f"pkg/core.py:{core_line('    def save(')}"
    mod = codecheck.api(proj, "pkg.core", env="none")
    assert {"Repo", "compute", "settings"} <= {m["name"] for m in mod["members"]}
    miss = codecheck.api(proj, "pkg.core.Rpeo", env="none")
    assert miss["found"] is False and miss["exit"] == 3 and miss["nearest"][0]["name"] == "Repo"
    std = codecheck.api(proj, "json.loads", env="none")
    assert std["found"] and std["source"] == "stdlib" and "s" in std["signature"]


# -- scope: snippets, diffs, cache ----------------------------------------------------------------------------

def test_snippet_is_checked_as_if_written_at_its_path(proj):
    res = codecheck.check(proj, snippet="from .core import Repo\nRepo().fetch\n", as_path="pkg/new.py",
                          env="none", use_cache=False)
    assert res["scope"] == "a snippet checked as pkg/new.py"
    assert [s["name"] for s in res["sites"] if s["verdict"] == "absent"] == ["fetch"]
    assert not (proj / "pkg" / "new.py").exists()


def test_diff_checks_only_changed_lines(tmp_path):
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/core.py", CORE)
    _write(tmp_path, "pkg/a.py", "from pkg.core import Repo\n\n\ndef f():\n    return Repo().fetch\n")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "x")
    _write(tmp_path, "pkg/a.py", "from pkg.core import Repo\n\n\ndef f():\n    return Repo().fetch\n\n\n"
                                 "def g():\n    return Repo().gett\n")
    _write(tmp_path, "pkg/new.py", "import json\njson.loadz\n")
    res = codecheck.check(tmp_path, diff="HEAD", env="none", use_cache=False)
    absent = sorted((s["path"], s["name"]) for s in res["sites"] if s["verdict"] == "absent")
    assert absent == [("pkg/a.py", "gett"), ("pkg/new.py", "loadz")]   # line 5 (fetch) did not change
    assert "changed lines against HEAD" in res["scope"]
    codecheck.reset_caches()


def test_cache_is_keyed_by_content_and_dropped_when_a_dependency_changes(tmp_path):
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/core.py", CORE)
    _write(tmp_path, "pkg/a.py", "from pkg.core import Repo\n\n\ndef f():\n    return Repo().fetch\n")
    first = codecheck.check(tmp_path, ["pkg/a.py"], env="none")
    assert "cache" not in first and not (tmp_path / ".verinoda").exists()   # no .verinoda: nothing written
    (tmp_path / ".verinoda").mkdir()
    a = codecheck.check(tmp_path, ["pkg/a.py"], env="none")
    b = codecheck.check(tmp_path, ["pkg/a.py"], env="none")
    assert a["cache"] == {"hits": 0, "misses": 1} and b["cache"] == {"hits": 1, "misses": 0}
    assert a["sites"] == b["sites"] and b["summary"]["absent"] == 1
    # the class the verdict was read from changes: the cached answer is not used
    _write(tmp_path, "pkg/core.py", CORE.replace("    def get(self, order_id):", "    def fetch(self):\n"
                                                 "        return 1\n\n    def get(self, order_id):"))
    c = codecheck.check(tmp_path, ["pkg/a.py"], env="none")
    assert c["cache"]["hits"] == 0 and c["summary"]["absent"] == 0
    codecheck.reset_caches()


# -- CLI and MCP ------------------------------------------------------------------------------------------------

def test_cli_exit_codes_and_rendering(proj, capsys):
    from verinoda import cli

    rc = cli.main(["check", "pkg/use.py", "--repo", str(proj), "--env", "none", "--no-cache"])
    out = capsys.readouterr().out
    assert rc == 3 and "ABSENT  import pkg.core.compute_totl" in out
    assert f"nearest: compute (pkg/core.py:{core_line('def compute(')})" in out
    assert "environment: Verinoda's interpreter, standard library only" in out
    rc = cli.main(["check", "pkg/core.py", "--repo", str(proj), "--env", "none", "--no-cache", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert rc == 0 and data["exit"] == 0 and data["summary"]["absent"] == 0
    rc = cli.main(["api", "pkg.core.Repo", "--repo", str(proj), "--env", "none"])
    assert rc == 0 and "save(self, customer, total)" in capsys.readouterr().out
    assert cli.main(["api", "pkg.core.Nope", "--repo", str(proj), "--env", "none"]) == 3
    with pytest.raises(SystemExit):
        cli.main(["check", "pkg/use.py", "--diff", "--repo", str(proj)])


def test_cli_stdin(proj, capsys, monkeypatch):
    import io

    from verinoda import cli

    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"import json\njson.dumps({}, indnt=2)\n"),
                                                       encoding="utf-8"))
    rc = cli.main(["check", "--stdin", "--as", "pkg/x.py", "--repo", str(proj), "--env", "none", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert data["scope"] == "a snippet checked as pkg/x.py"
    kw = [s for s in data["sites"] if s["kind"] == "kwarg"]
    assert kw and kw[0]["verdict"] == "unknown" and "**kw" in kw[0]["why"]   # json.dumps takes **kw
    assert rc == 0


def test_mcp_tools_call_the_same_core(proj):
    from verinoda.mcp.server import AtlasTools

    (proj / ".verinoda").mkdir(exist_ok=True)
    t = AtlasTools(proj)
    res = t.code_check(paths=["pkg/use.py"], env="none")
    core = codecheck.check(proj, ["pkg/use.py"], env="none")
    assert res["summary"] == core["summary"] and res["exit"] == 3
    assert list(res)[:3] == ["summary", "exit", "env"]
    snip = t.code_check(snippet="import json\njson.loadz\n", as_path="pkg/s.py", env="none")
    assert snip["summary"]["absent"] == 1
    assert t.code_check(snippet="x", paths=["pkg/use.py"])["error"] == "invalid_argument"
    assert t.code_check(snippet="x", as_path="../out.py")["error"] == "invalid_argument"
    api = t.api_members("pkg.core.Repo", env="none")
    assert api["found"] and "save" in {m["name"] for m in api["members"]}
