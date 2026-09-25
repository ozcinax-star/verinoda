"""Name-existence check (docs/DESIGN.md D32): closed-world rule, guards, keywords, dict keys, nearest
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
import enum
import functools


class Color(enum.Enum):
    RED = 1


class Repo:
    def __init__(self, url=":memory:"):
        self.url = url

    def save(self, customer, total):
        return 1

    def get(self, order_id):
        return None

    def conf(self):
        return {"a": 1}


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
from pkg.core import (Color, Dyn, Leaky, Reads, Repo, Slotted, compute, flexible, cached, mixed, po,
                      settings, wrapped)
from pkg.core import compute_totl
from pkg.helpers import nothing
from .helpers2 import nothing2
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
    Color(value=1)
    s1 = settings()["database_url"]
    s2 = settings()["database_uri"]
    st = settings()
    s3 = st["max_item"]
    grown = settings()
    grown["x"] = 1
    s4 = grown["x"]
    s5 = mixed(True)["a"]
    s6 = Repo().conf()["zzz"]
    s7 = json.patched_here
    return a, b, c, d, e, f, g, h, i, j, k, m, n, o, p, q, s1, s2, s3, s4, s5, s6, s7, other
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
    _write(root, "pkg/patcher.py", "import json\n\njson.patched_here = 1\n")
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
    patched = site(result, "s7 = json.patched_here", "patched_here")                  # json.patched_here = 1
    assert patched["verdict"] == "unknown" and "pkg/patcher.py:3" in patched["why"]


def test_imports(result):
    t = site(result, "from pkg.core import compute_totl", "compute_totl")
    assert t["verdict"] == "absent" and t["nearest"][0]["name"] == "compute"
    assert t["nearest"][0]["at"] == f"pkg/core.py:{core_line('def compute(')}"
    h = site(result, "from pkg.helpers import nothing", "helpers")
    assert h["verdict"] == "absent" and "no module helpers in package pkg" in h["message"]
    rel = site(result, "from .helpers2 import nothing2", "helpers2")
    assert rel["verdict"] == "absent" and "no module helpers2 in package pkg" in rel["message"]
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
    e = site(result, "Color(value=1)", "value", "kwarg")                                        # EnumType.__call__
    assert e["verdict"] == "unknown" and "metaclass" in e["why"]


def test_dict_keys_of_functions_returning_literals(result):
    assert site(result, 's1 = settings()["database_url"]', "database_url", "dict_key")["verdict"] == "exists"
    bad = site(result, 's2 = settings()["database_uri"]', "database_uri", "dict_key")
    assert bad["verdict"] == "absent" and bad["nearest"][0]["name"] == "database_url"
    assert "returned by settings()" in bad["message"]
    assert site(result, 's3 = st["max_item"]', "max_item", "dict_key")["verdict"] == "absent"   # one local
    grown = site(result, 's4 = grown["x"]', "x", "dict_key")
    assert grown["verdict"] == "unknown" and "keys are added" in grown["why"]
    assert not [s for s in result["sites"] if s["kind"] == "dict_key" and s["name"] == "a"]    # not all literals
    assert not [s for s in result["sites"] if s["kind"] == "dict_key" and s["name"] == "zzz"]  # a method


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
                               "fancylib.real_fn(1, retry=2)\nimport not_anywhere_xyz\nimport jedi\n")
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
    # installed next to Verinoda (jedi is), not in the project's .venv: judged by the .venv only
    assert by[(7, "jedi", "import")]["verdict"] == "absent"
    decl = [s for s in res["sites"] if s["path"] == "b.py"][0]
    assert decl["verdict"] == "not_installed" and "pyproject.toml" in decl["why"]
    # the same code without the project environment: never absent for third-party names
    none = codecheck.check(tmp_path, ["app.py"], env="none", include_exists=True, use_cache=False)
    third = [s for s in none["sites"] if "fancylib" in s["expr"] or s["name"] in ("not_anywhere_xyz", "jedi")]
    assert third and {s["verdict"] for s in third} <= {"not_installed", "unknown"}
    codecheck.reset_caches()


def _std_extension() -> Path | None:
    """A compiled standard-library module file (its base name must stay: the loader calls PyInit_<name>)."""
    import importlib.machinery
    import sysconfig

    for d in {sysconfig.get_path("platstdlib"), os.path.join(sys.base_prefix, "DLLs"),
              os.path.join(sysconfig.get_path("platstdlib") or "", "lib-dynload")}:
        for name in ("_socket", "_json", "select", "_struct", "_bisect"):
            for suffix in importlib.machinery.EXTENSION_SUFFIXES:
                p = Path(d or "") / f"{name}{suffix}"
                if p.is_file():
                    return p
    return None


def _marker_init(marker: Path) -> str:
    return f"import pathlib\npathlib.Path({str(marker)!r}).write_text('ran')\n\n\ndef real():\n    return 1\n"


def test_nothing_from_the_checked_project_or_its_environment_runs(tmp_path):
    import shutil

    proj, marks = tmp_path / "proj", tmp_path / "marks"
    marks.mkdir()
    venv = _make_venv(proj)
    site = next(iter(cenv.venv_site_dirs(venv, sys.version_info)))
    ext = _std_extension()
    # the environment's own interpreter is not a program at all: it must never be started
    for exe in [venv / "Scripts" / "python.exe"] if os.name == "nt" else list((venv / "bin").glob("python*")):
        exe.unlink()
        exe.write_bytes(b"not an interpreter\n")
    (site / "zz_hook.pth").write_text(f"import pathlib; pathlib.Path({str(marks / 'pth')!r}).write_text('ran')\n",
                                      encoding="utf-8")
    _write(site, "evilpkg/__init__.py", _marker_init(marks / "site_init"))
    _write(site, "evilpkg-1.0.dist-info/METADATA", "Metadata-Version: 2.1\nName: evilpkg\nVersion: 1.0\n")
    _write(proj, "mypkg/__init__.py", _marker_init(marks / "project_init"))
    (site / "__editable__.mypkg-0.1.pth").write_text(str(proj) + "\n", encoding="utf-8")
    if ext is not None:
        shutil.copy(ext, site / "evilpkg" / ext.name)
        shutil.copy(ext, proj / "mypkg" / ext.name)
    mod = ext.name.split(".")[0] if ext is not None else "_nothing"
    _write(proj, "use.py", f"import evilpkg\nfrom evilpkg import {mod}\nfrom mypkg import {mod} as m2\n"
                           "evilpkg.real()\nevilpkg.fake_xyz()\n")
    res = codecheck.check(proj, ["use.py"], include_exists=True, use_cache=False)
    env = res["env"]
    assert env["kind"] == "project" and not Path(env["executable"]).resolve().is_relative_to(proj.resolve())
    assert "not run" in env["started"]
    # the base interpreter (pyvenv.cfg `home`), not the launcher of the environment that ran `-m venv`
    exe = Path(env["executable"])
    assert not any((d / "pyvenv.cfg").is_file() for d in (exe.parent, exe.parent.parent))
    by = {s["name"]: s["verdict"] for s in res["sites"]}
    assert by["real"] == "exists" and by["fake_xyz"] == "absent"
    # the same project with Verinoda's own interpreter (stdlib only): jedi runs in this very process
    none = codecheck.check(proj, ["use.py"], env="none", include_exists=True, use_cache=False)
    assert none["sites"]
    assert sorted(p.name for p in marks.iterdir()) == []
    # a base interpreter inside the project is not trusted
    other = tmp_path / "other"
    _write(other, "fakebase/python.exe" if os.name == "nt" else "fakebase/python3", "not an interpreter\n")
    _write(other, ".venv/pyvenv.cfg", f"home = {other / 'fakebase'}\nversion = 3.12.0\n")
    env2 = cenv.select_env(other, "auto")
    assert env2.kind == "verinoda" and "inside the project" in env2.note
    codecheck.reset_caches()


def test_environment_changes_outside_the_project_reach_a_long_lived_process(tmp_path):
    proj, sib = tmp_path / "proj", tmp_path / "siblib"
    venv = _make_venv(tmp_path / "envroot")   # an explicit --env outside the project
    site = next(iter(cenv.venv_site_dirs(venv, sys.version_info)))
    _write(sib, "sib.py", "def a():\n    pass\n")
    (site / "__editable__.sib-0.1.pth").write_text(str(sib) + "\n", encoding="utf-8")
    _write(proj, "main.py", "import sib\nimport newpkg\nsib.b()\nnewpkg.go()\n")
    (proj / ".verinoda").mkdir()

    def verdicts():
        r = codecheck.check(proj, ["main.py"], env=str(venv), include_exists=True)
        return {s["expr"]: s["verdict"] for s in r["sites"]}, r

    first, r1 = verdicts()
    assert first["sib.b"] == "absent" and "outside the project and its environment" in \
        [s for s in r1["sites"] if s["expr"] == "sib.b"][0]["message"]
    assert first["newpkg"] == "absent"
    _write(sib, "sib.py", "def a():\n    pass\n\n\ndef b():\n    pass\n")            # the sibling gains b()
    _write(site, "newpkg/__init__.py", "def go():\n    pass\n")                       # a package is installed
    _write(site, "newpkg-1.0.dist-info/METADATA", "Metadata-Version: 2.1\nName: newpkg\nVersion: 1.0\n")
    second, r2 = verdicts()
    assert second["sib.b"] == "exists" and second["newpkg"] == "exists" and second["newpkg.go"] == "exists"
    assert r2["exit"] == 0
    codecheck.reset_caches()


def test_installed_plugin_stores_and_lock_mismatch_exit_reason(tmp_path):
    venv = _make_venv(tmp_path)
    site = next(iter(cenv.venv_site_dirs(venv, sys.version_info)))
    _write(site, "hostlib/__init__.py", "def real():\n    return 1\n")
    _write(site, "hostlib_ext/__init__.py", "import hostlib\n\n\ndef lazy_thing(x):\n    return x\n\n\n"
                                            "hostlib.lazy_thing = lazy_thing\n")
    _write(tmp_path, "requirements.txt", "fancylib==9.9\n")
    _write(tmp_path, "app.py", "import hostlib\nimport hostlib_ext\nimport fancylib\n\n"
                               "hostlib.lazy_thing(1)\nhostlib.fake_xyz\nfancylib.real_fn(1)\n")
    res = codecheck.check(tmp_path, ["app.py"], include_exists=True, use_cache=False)
    by = {s["expr"]: s for s in res["sites"]}
    assert by["hostlib.lazy_thing"]["verdict"] == "unknown" and "hostlib_ext" in by["hostlib.lazy_thing"]["why"]
    assert by["hostlib.fake_xyz"]["verdict"] == "absent"
    assert res["exit"] == 3 and "absent" in res["exit_because"] and "lock" in res["exit_because"]
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


def test_a_name_of_another_platform_is_unknown_not_absent(proj):
    other = "fork" if sys.platform == "win32" else "startfile"   # os.fork: not on Windows; startfile: only there
    res = codecheck.check(proj, snippet=f"import os\nos.{other}\nos.forkk_nope\n", as_path="pkg/plat.py",
                          env="none", include_exists=True, use_cache=False)
    by = {s["name"]: s for s in res["sites"] if s["kind"] == "attribute"}
    assert by[other]["verdict"] == "unknown" and "stubs declare it" in by[other]["why"]
    assert by["forkk_nope"]["verdict"] == "absent"


def test_os_path_is_its_platform_module_and_a_sibling_file_does_not_shadow_a_package(proj):
    res = codecheck.check(proj, snippet="import os\nos.path.join\nos.path.joinz\n", as_path="pkg/p2.py", env="none",
                          include_exists=True, use_cache=False)
    by = {s["name"]: s for s in res["sites"] if s["kind"] == "attribute"}
    assert by["join"]["verdict"] == "exists"
    assert by["joinz"]["verdict"] == "absent" and "module os.path (" in by["joinz"]["message"]
    assert "join" in [n["name"] for n in by["joinz"]["nearest"]]
    # pkg/sub/robot.py is not the `robot` package for pkg/sub/user.py (a package imports absolutely)
    _write(proj, "pkg/sub/__init__.py", "")
    _write(proj, "pkg/sub/robot.py", "X = 1\n")
    _write(proj, "pkg/sub/user.py", "import robot.api\n")
    r2 = codecheck.check(proj, ["pkg/sub/user.py"], env="none", include_exists=True, use_cache=False)
    assert {s["verdict"] for s in r2["sites"]} <= {"not_installed", "exists"}
    assert not [s for s in r2["sites"] if s["verdict"] == "absent"]


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
    api = codecheck.api(proj, "pkg.core.Repo")   # not decided, so not "not found"
    assert api["found"] is None and api["decided"] == "unknown" and api["exit"] == 0
    assert "jedi is not installed" in api["why"]


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
    # a re-export is followed to the class; module names are case-sensitive even on Windows
    _write(proj, "pkg/facade.py", "from pkg.core import Repo\n")
    fac = codecheck.api(proj, "pkg.facade.Repo", env="none")
    assert fac["found"] and fac["kind"] == "class" and "save" in {m["name"] for m in fac["members"]}
    assert codecheck.api(proj, "pkg.Core.Repo", env="none")["found"] is False


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
    # another file starts assigning that attribute: the absent answer read every file, so it is redone
    _write(tmp_path, "pkg/other.py", "import pkg.core\n\npkg.core.Repo.fetch = None\n")
    _write(tmp_path, "pkg/other.py", "import pkg.core\n\npkg.core.Repo.fetch = None  # set at runtime\n")
    other = codecheck.check(tmp_path, ["pkg/a.py"], env="none")
    assert other["cache"]["hits"] == 0 and other["summary"]["absent"] == 0 and other["summary"]["unknown"] == 1
    (tmp_path / "pkg" / "other.py").unlink()
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
    assert list(res)[:5] == ["status", "summary", "exit", "exit_because", "env"] and "absent" in res["exit_because"]
    assert res["status"] == "absent"
    snip = t.code_check(snippet="import json\njson.loadz\n", as_path="pkg/s.py", env="none")
    assert snip["summary"]["absent"] == 1
    assert t.code_check(snippet="x", paths=["pkg/use.py"])["error"] == "invalid_argument"
    assert t.code_check(snippet="x", as_path="../out.py")["error"] == "invalid_argument"
    api = t.api_members("pkg.core.Repo", env="none")
    assert api["found"] and "save" in {m["name"] for m in api["members"]}


# -- names that reach a container from elsewhere (review findings) ---------------------------------------------

OBJS = '''\
import abc
import enum
import queue
import sys
from ctypes import Structure, c_int
from typing import Protocol


class Opts:
    def __init__(self):
        self.a = 1


def by_setattr():
    o = Opts()
    setattr(o, "verbose", True)
    return o.verbose


def by_vars():
    o = Opts()
    vars(o).update({"quiet": 1})
    return o.quiet


def by_object_setattr():
    o = Opts()
    object.__setattr__(o, "level", 3)
    return o.level


class Base(metaclass=abc.ABCMeta):
    pass


class Impl(Base):
    pass


def meta_names():
    Base.register(int)
    return Impl.register, Impl()._abc_impl


class Counter:
    @classmethod
    def reset(cls):
        cls.count = 0


def counted():
    return Counter.count


class Job:
    def __init__(self, n):
        self.n = n


def queued():
    q = queue.Queue()
    job = Job(3)
    q.put(job)
    return job.result


REGISTRY = []


def registered():
    p = Job(1)
    REGISTRY.append(p)
    return p.enabled


class Point(Structure):
    _fields_ = [("x", c_int)]


def fields():
    p = Point()
    return p.x


def functional_enum():
    E = enum.Enum("E", "A B")
    return E.A


def frozen_app():
    if getattr(sys, "frozen", False):
        return sys._MEIPASS
    return None


class Greeter(Protocol):
    def greet(self) -> str: ...


def protocol_flag():
    return Greeter._is_protocol


class Conn:
    def __init__(self):
        self.__response = None


def mangled():
    return Conn()._Conn__response


class Rec:
    def __init__(self, other):
        a, b = self, other
        a.via_tuple = 1


def via_tuple():
    return Rec(None).via_tuple


def invented():
    o = Opts()
    return o.not_there_xyz, Counter.nope_xyz, Job(1).nope_xyz
'''

REGISTRY = '''\
from opn import settings


class Registered:
    pass


class Model:
    pass


class Flags:
    pass


def register(cls):
    cls.plugin_id = 1
    return cls


register(Registered)


def configure(mod):
    mod.READY = True


configure(settings)


def load(values):
    for k, v in values.items():
        setattr(settings, k.upper(), v)


class Field:
    def __set_name__(self, owner, name):
        owner.registry = {}


for _name in ("debug", "trace"):
    setattr(Flags, _name, False)
'''

USERS = '''\
from opn import dynmod, lazyalias, mid, plain, registry, settings
from opn.registry import Flags, Model, Registered


def use():
    return (Registered.plugin_id, settings.READY, settings.LEVEL, Model.registry, Flags.debug,
            dynmod.alpha_dyn, lazyalias.old_name, mid.sub, mid.helper, plain.nope_xyz)
'''


@pytest.fixture(scope="module")
def opn(tmp_path_factory):
    root = tmp_path_factory.mktemp("opn")
    _write(root, "opn/__init__.py", "")
    _write(root, "opn/objs.py", OBJS)
    _write(root, "opn/registry.py", REGISTRY)
    _write(root, "opn/settings.py", "DEBUG_DEFAULT = False\n")
    _write(root, "opn/plain.py", "VALUE = 1\n")
    _write(root, "opn/dynmod.py", 'for _n in ("alpha", "beta"):\n    locals()[_n + "_dyn"] = 1\n')
    _write(root, "opn/hooks.py", "import sys\n\n\ndef install(aliases):\n    g = sys._getframe(1).f_globals\n"
                                 "    g['__getattr__'] = lambda name: aliases[name]\n")
    _write(root, "opn/lazyalias.py", "from opn.hooks import install\n\ninstall({'old_name': 1})\n")
    _write(root, "opn/pkgx/__init__.py", "import opn.pkgx.sub\n\n\ndef helper():\n    return 1\n")
    _write(root, "opn/pkgx/sub.py", "X = 1\n")
    _write(root, "opn/mid.py", "from opn.pkgx import *\n")
    _write(root, "opn/users.py", USERS)
    res = codecheck.check(root, ["opn/objs.py", "opn/users.py"], env="none", include_exists=True, use_cache=False)
    yield res
    codecheck.reset_caches()


def _at(res: dict, path: str, text: str, name: str, source: str) -> dict:
    lines = [i for i, ln in enumerate(source.splitlines(), 1) if text in ln]
    hits = [s for s in res["sites"] if s["path"] == path and s["line"] in lines and s["name"] == name]
    assert hits, (text, name)
    return hits[0]


def test_attributes_set_on_a_local_through_setattr_vars_or_object_setattr_open_it(opn):
    for text, name, how in (("return o.verbose", "verbose", "setattr"), ("return o.quiet", "quiet", "vars"),
                            ("return o.level", "level", "object.__setattr__")):
        s = _at(opn, "opn/objs.py", text, name, OBJS)
        assert s["verdict"] == "unknown" and how in s["why"], s


def test_metaclass_names_and_class_attributes_set_by_a_classmethod_exist(opn):
    for text, name in (("Base.register(int)", "register"), ("return Impl.register", "register"),
                       ("return Impl.register", "_abc_impl"), ("return Counter.count", "count"),
                       ("return Greeter._is_protocol", "_is_protocol"), ("_Conn__response", "_Conn__response")):
        assert _at(opn, "opn/objs.py", text, name, OBJS)["verdict"] == "exists", (text, name)


def test_instances_kept_by_a_container_or_aliased_are_open(opn):
    job = _at(opn, "opn/objs.py", "return job.result", "result", OBJS)
    assert job["verdict"] == "unknown" and "may keep it" in job["why"]
    reg = _at(opn, "opn/objs.py", "return p.enabled", "enabled", OBJS)
    assert reg["verdict"] == "unknown" and "REGISTRY.append" in reg["why"]
    tup = _at(opn, "opn/objs.py", "return Rec(None).via_tuple", "via_tuple", OBJS)
    assert tup["verdict"] == "unknown" and "tuple" in tup["why"]


def test_ctypes_fields_enum_functional_api_and_sometimes_sys_names_are_not_absent(opn):
    assert "metaclass" in _at(opn, "opn/objs.py", "return p.x", "x", OBJS)["why"]
    assert "functional API" in _at(opn, "opn/objs.py", "return E.A", "A", OBJS)["why"]
    assert _at(opn, "opn/objs.py", "sys._MEIPASS", "_MEIPASS", OBJS)["verdict"] in ("unknown", "guarded")
    # invented names on the same containers are still absent
    for name in ("not_there_xyz", "nope_xyz"):
        hits = [s for s in opn["sites"] if s["path"] == "opn/objs.py" and s["name"] == name]
        assert hits and {s["verdict"] for s in hits} == {"absent"}, hits


def test_names_the_project_sets_from_outside_are_unknown(opn):
    by = {s["name"]: s for s in opn["sites"] if s["path"] == "opn/users.py" and s["kind"] == "attribute"}
    assert "register()" in by["plugin_id"]["why"] and "Registered" in by["plugin_id"]["why"]   # reg(A)
    assert by["READY"]["verdict"] == "unknown" and "configure()" in by["READY"]["why"]          # a parameter
    assert by["LEVEL"]["verdict"] == "unknown" and "computed name" in by["LEVEL"]["why"]        # setattr(mod, k)
    assert by["registry"]["verdict"] == "unknown"                                              # __set_name__
    assert by["debug"]["verdict"] == "unknown" and "computed name" in by["debug"]["why"]        # loop setattr
    assert by["alpha_dyn"]["verdict"] == "unknown" and "locals()" in by["alpha_dyn"]["why"]
    assert by["old_name"]["verdict"] == "unknown" and "caller's frame" in by["old_name"]["why"]
    assert by["sub"]["verdict"] == "exists" and by["helper"]["verdict"] == "exists"             # star + submodule
    assert by["nope_xyz"]["verdict"] == "absent"


# -- signatures and decorators -------------------------------------------------------------------------------

SIGS = '''\
import sys
from dataclasses import dataclass, field

from sigs_lib import cache, dataclass as frozen


@dataclass
class Cfg:
    name: str
    tags: list = field(default_factory=list, init=True)
    hidden: int = field(default=0, init=False)


class Reader:
    if sys.version_info >= (3, 99):
        def read(self, n):
            return n
    else:
        def read(self, n, *, timeout=None):
            return n


@cache
def load(key):
    return key


@frozen
class State:
    step: int


def use():
    c = Cfg(name="a", tags=[], hidden=1)
    r = Reader()
    s = State(step=1)
    return c, r.read(1, timeout=2), r.read(1, timout=2), load("k", refresh=True), s.replace
'''


def test_signatures_follow_field_init_conditional_defs_and_where_a_decorator_comes_from(tmp_path):
    _write(tmp_path, "sigs.py", SIGS)
    _write(tmp_path, "sigs_lib.py", "def cache(fn):\n    return fn\n\n\ndef dataclass(cls):\n    return cls\n")
    res = codecheck.check(tmp_path, ["sigs.py"], env="none", include_exists=True, use_cache=False)
    by = {(s["name"], s["kind"]): s for s in res["sites"] if s["kind"] in ("kwarg", "attribute")}
    assert by[("tags", "kwarg")]["verdict"] == "exists"                     # field(init=True) is an argument
    assert by[("hidden", "kwarg")]["verdict"] == "absent"                   # field(init=False) is not
    assert by[("timeout", "kwarg")]["verdict"] == "exists"                  # the else branch defines it
    assert by[("timout", "kwarg")]["verdict"] == "absent"                   # no branch does
    assert by[("refresh", "kwarg")]["verdict"] == "unknown"                 # sigs_lib.cache, not functools.cache
    assert by[("replace", "attribute")]["verdict"] == "unknown"             # sigs_lib.dataclass may add names
    assert by[("step", "kwarg")]["verdict"] == "unknown"


def test_bom_and_form_feed_do_not_shift_or_break_a_file(tmp_path):
    (tmp_path / "bom.py").write_bytes(b"\xef\xbb\xbfimport json\n\njson.loads_file('x')\n")
    for name, sep in (("plain.py", "\n"), ("ff.py", "\x0c\n")):
        _write(tmp_path, name, "import json\nimport os\n" + sep + "print(os.sep, json.nosuch)\n")
    res = codecheck.check(tmp_path, ["bom.py", "plain.py", "ff.py"], env="none", use_cache=False)
    absent = sorted((s["path"], s["line"], s["col"], s["name"]) for s in res["sites"] if s["verdict"] == "absent")
    assert absent == [("bom.py", 3, 6, "loads_file"), ("ff.py", 4, 20, "nosuch"), ("plain.py", 4, 20, "nosuch")]
    assert not [f for f in res["files"] if f.get("error")]


def test_api_looks_up_every_part_of_the_target(proj):
    join = codecheck.api(proj, "os.path.join", env="none")
    assert join["found"] and join["kind"] == "function" and join["exit"] == 0
    bad = codecheck.api(proj, "os.path.joinpath_invented", env="none")
    assert bad["found"] is False and bad["exit"] == 3 and "join" in [n["name"] for n in bad["nearest"]]
    # not decided is not "not found": found null, exit 0 (review round 3)
    for t in ("json.dumps.invented_attr", "sys.argv.invented", "pkg.core.compute.invented", "os.environ.copy",
              "concurrent.futures.ProcessPoolExecutor", "pkg.lazymod.magic"):
        r = codecheck.api(proj, t, env="none")
        assert r["found"] is None and r["decided"] == "unknown" and r["exit"] == 0, (t, r)
    # a third-party module without a project environment is not installed, not missing
    r = codecheck.api(proj, "requests.get", env="none")
    assert r["found"] is None and r["decided"] == "not_installed" and r["exit"] == 0, r
    # what check calls unknown, api does not call missing (a platform's name, a name the project assigns)
    other = "fork" if sys.platform == "win32" else "startfile"
    r = codecheck.api(proj, f"os.{other}", env="none")
    assert r["found"] is None and r["decided"] == "unknown" and "stubs declare" in r["why"], r
    r = codecheck.api(proj, "json.patched_here", env="none")
    assert r["found"] is None and "pkg/patcher.py" in r["why"], r


def test_snippet_definitions_are_the_source_of_truth_for_as_path(tmp_path):
    _write(tmp_path, "net.py", "def fetch(url):\n    return url\n")
    _write(tmp_path, "models.py", "from dataclasses import dataclass\n\n\n@dataclass\nclass Cfg:\n    name: str\n")
    new_net = "def fetch(url, *, timeout=10):\n    return url\n\n\nprint(fetch('u', timeout=3), fetch('u', tmeout=3))\n"
    res = codecheck.check(tmp_path, snippet=new_net, as_path="net.py", env="none", include_exists=True,
                          use_cache=False)
    kw = {s["name"]: s["verdict"] for s in res["sites"] if s["kind"] == "kwarg"}
    assert kw == {"timeout": "exists", "tmeout": "absent"}
    new_models = ("from dataclasses import dataclass\n\n\n@dataclass\nclass Cfg:\n    name: str\n"
                  "    port: int = 80\n\n\nprint(Cfg(name='a', port=1))\n")
    for as_path in ("models.py", "new_models.py"):   # an existing file, and one not written yet
        res = codecheck.check(tmp_path, snippet=new_models, as_path=as_path, env="none", include_exists=True,
                              use_cache=False)
        assert {s["name"]: s["verdict"] for s in res["sites"] if s["kind"] == "kwarg"} == \
            {"name": "exists", "port": "exists"}, as_path
    assert (tmp_path / "net.py").read_text(encoding="utf-8") == "def fetch(url):\n    return url\n"


def test_conftest_and_pytest_pythonpath_put_modules_on_the_search_path(tmp_path):
    _write(tmp_path, "scripts/release_tool.py", "def bump(v):\n    return v + 1\n")
    _write(tmp_path, "tools/helper_mod.py", "X = 1\n")
    _write(tmp_path, "pyproject.toml", '[tool.pytest.ini_options]\npythonpath = ["scripts"]\n')
    _write(tmp_path, "tests/conftest.py",
           "import os\nimport sys\n\nsys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'tools'))\n")
    _write(tmp_path, "tests/test_a.py", "import release_tool\nimport helper_mod\nimport missing_mod_xyz\n")
    res = codecheck.check(tmp_path, ["tests/test_a.py"], env="none", include_exists=True, use_cache=False)
    by = {s["name"]: s for s in res["sites"]}
    assert by["release_tool"]["verdict"] == "exists"                                     # pytest pythonpath
    for name in ("helper_mod", "missing_mod_xyz"):                                        # conftest edits sys.path
        assert by[name]["verdict"] == "unknown" and "tests/conftest.py" in by[name]["why"]


def test_a_large_project_says_what_it_did_not_read(tmp_path, monkeypatch):
    monkeypatch.setattr(codecheck, "MAX_FILES", 3)
    _write(tmp_path, "conf.py", "DEBUG = False\n")
    _write(tmp_path, "a_main.py", "import conf\nprint(conf.EXTRA, conf.DEBUGG)\n")
    for i in range(4):
        _write(tmp_path, f"b{i}.py", "x = 1\n")
    _write(tmp_path, "zzz/setup_extra.py", "import conf\nconf.EXTRA = 1\n")
    (tmp_path / ".verinoda").mkdir()
    one = codecheck.check(tmp_path, ["a_main.py"], env="none")
    assert {s["name"]: s["verdict"] for s in one["sites"]} == {"EXTRA": "unknown", "DEBUGG": "unknown"}
    assert "more than 3 Python files" in one["sites"][0]["why"] and "more than 3" in one["cache"]["off"]
    whole = codecheck.check(tmp_path, ["."], env="none")
    assert whole["summary"]["files"] == 3 and "stopped at 3 Python files" in whole["incomplete"][0]
    codecheck.reset_caches()


def test_a_time_budget_stops_before_the_next_file(proj):
    res = codecheck.check(proj, ["pkg"], env="none", use_cache=False, budget_s=0)
    assert res["summary"]["files"] == 0 and "time budget" in res["incomplete"][0]


def test_the_oracle_never_imports_a_main_module():
    env = cenv.own_env("test")
    info = env.oracle().ask("module", name="unittest.__main__")
    assert info["ok"] is False and "a program" in info["error"]
    env.oracle().close()


# -- diff ----------------------------------------------------------------------------------------------------------

def test_diff_revision_is_never_read_as_an_option(tmp_path):
    from verinoda.mcp.server import AtlasTools

    _write(tmp_path, "a.py", "import json\n")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "x")
    (tmp_path / ".verinoda").mkdir()
    target = tmp_path / "precious.txt"
    target.write_text("keep me\n", encoding="utf-8")
    for rev in (f"--output={target}", "-p", "no-such-revision"):
        with pytest.raises(ValueError):
            codecheck.check(tmp_path, diff=rev, env="none", use_cache=False)
    out = AtlasTools(tmp_path).code_check(diff=f"--output={target}", env="none")
    assert out["error"] == "invalid_argument"
    assert target.read_text(encoding="utf-8") == "keep me\n"


def test_diff_reads_non_ascii_file_names_and_the_cache_selects_like_a_fresh_run(tmp_path):
    _write(tmp_path, "a.py", "import json\n\n\ndef f():\n    return json.dumps(\n        1,\n        indent=2)\n")
    _write(tmp_path, "çalışma.py", "import json\n")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "x")
    _write(tmp_path, "çalışma.py", "import json\njson.dumpz(1)\n")
    _write(tmp_path, "yeni_dosya_ğ.py", "import json\njson.loadz\n")
    res = codecheck.check(tmp_path, diff="HEAD", env="none", use_cache=False)
    assert sorted((s["path"], s["name"]) for s in res["sites"] if s["verdict"] == "absent") == \
        [("yeni_dosya_ğ.py", "loadz"), ("çalışma.py", "dumpz")]
    # only the last line of a two-line call changes: the cached whole-file answer selects the same sites
    (tmp_path / ".verinoda").mkdir()
    _write(tmp_path, "a.py", "import json\n\n\ndef f():\n    return json.dumps(\n        1,\n        indnt=2)\n")
    codecheck.check(tmp_path, ["a.py"], env="none", include_exists=True)
    warm = codecheck.check(tmp_path, diff="HEAD", env="none", include_exists=True)
    fresh = codecheck.check(tmp_path, diff="HEAD", env="none", include_exists=True, use_cache=False)
    assert warm["cache"]["hits"] >= 1

    def key(r):
        return sorted((s["path"], s["line"], s["expr"], s["verdict"]) for s in r["sites"] if s["path"] == "a.py")
    assert key(warm) == key(fresh) and key(fresh)
    codecheck.reset_caches()


# -- round-2 settlement of the review findings -----------------------------------------------------------------

MIXINS = '''\
import abc
import queue


class Base:
    def __init__(self, *, name):
        self.name = name


class Plugin(abc.ABC, Base):
    pass


class Bare(abc.ABC):
    pass


class Stack(abc.ABC, queue.LifoQueue):
    pass


class Mixed(queue.LifoQueue, Base):
    pass


def use():
    Plugin(name="x")
    Plugin(nme="x")
    Bare(x=1)
    Stack(maxsize=1)
    Stack(maxsze=1)
    Mixed(name="x")
'''


def test_a_standard_library_base_without_its_own_constructor_does_not_answer_for_the_class(tmp_path):
    _write(tmp_path, "mixins.py", MIXINS)
    res = codecheck.check(tmp_path, ["mixins.py"], env="none", include_exists=True, use_cache=False)
    kw = {s["name"]: s for s in res["sites"] if s["kind"] == "kwarg" and not s["expr"].startswith("Mixed(")}
    assert kw["name"]["verdict"] == "exists" and "mixins.py:6" in kw["name"]["at_def"]   # abc.ABC is skipped
    assert kw["nme"]["verdict"] == "absent"
    assert kw["x"]["verdict"] == "absent" and "object()" in kw["x"]["message"]           # Bare(abc.ABC)
    assert kw["maxsize"]["verdict"] == "exists"                     # abc.ABC skipped, LifoQueue answers
    assert kw["maxsze"]["verdict"] == "absent"
    # LifoQueue's constructor is Queue's; Base may come before Queue in the real MRO
    mixed = [s for s in res["sites"] if s["kind"] == "kwarg" and s["expr"].startswith("Mixed(")]
    assert mixed and mixed[0]["verdict"] == "unknown" and "Base" in mixed[0]["why"]


def _chain(root: Path, target: str) -> None:
    _write(root, "pkg/__init__.py", "from .api import fetch\n")
    _write(root, "pkg/api.py", f"from .{target} import fetch\n")
    _write(root, "pkg/old.py", "def fetch(url):\n    return url\n")
    _write(root, "pkg/new.py", "def fetch(url, *, timeout=10):\n    return url\n")


def test_the_cache_follows_every_file_of_a_re_export_chain(tmp_path):
    _chain(tmp_path, "old")
    _write(tmp_path, "app.py", "from pkg import fetch\n\nprint(fetch('u', timeout=3))\n")
    (tmp_path / ".verinoda").mkdir()

    def kw():
        r = codecheck.check(tmp_path, ["app.py"], env="none", include_exists=True)
        return r["cache"], [s["verdict"] for s in r["sites"] if s["kind"] == "kwarg"]
    assert kw() == ({"hits": 0, "misses": 1}, ["absent"])
    assert kw() == ({"hits": 1, "misses": 0}, ["absent"])
    _write(tmp_path, "pkg/api.py", "from .new import fetch\n")     # only the middle of the chain changes
    assert kw() == ({"hits": 0, "misses": 1}, ["exists"])
    _write(tmp_path, "pkg/api.py", "from .old import fetch\n")     # and back: a cached "exists" is not reused
    assert kw() == ({"hits": 0, "misses": 1}, ["absent"])
    # a module's names read through a star import: the star's source is a dependency too
    _write(tmp_path, "lib/__init__.py", "from .impl import *\n")
    _write(tmp_path, "lib/impl.py", "def helper():\n    return 1\n")
    _write(tmp_path, "uses_lib.py", "import lib\n\nlib.helper()\n")
    first = codecheck.check(tmp_path, ["uses_lib.py"], env="none")
    assert first["summary"]["absent"] == 0
    _write(tmp_path, "lib/impl.py", "def helper2():\n    return 1\n")
    again = codecheck.check(tmp_path, ["uses_lib.py"], env="none")
    assert again["cache"]["hits"] == 0 and again["summary"]["absent"] == 1
    codecheck.reset_caches()


def test_a_long_lived_process_sees_a_base_class_change_through_a_re_export(tmp_path):
    _write(tmp_path, "base.py", "class Base:\n    def __init__(self, a):\n        self.a = a\n")
    _write(tmp_path, "base2.py", "class Base:\n    def __init__(self, a, b=0):\n        self.a = a\n")
    _write(tmp_path, "mid.py", "from base import Base\n")
    _write(tmp_path, "models.py", "from mid import Base\n\n\nclass Model(Base):\n    pass\n")
    _write(tmp_path, "app.py", "from models import Model\n\nModel(a=1, b=2)\n")

    def b():
        r = codecheck.check(tmp_path, ["app.py"], env="none", include_exists=True, use_cache=False)
        return next(s["verdict"] for s in r["sites"] if s["name"] == "b")
    assert b() == "absent"
    _write(tmp_path, "mid.py", "from base2 import Base\n")     # models.py itself does not change
    assert b() == "exists"
    # with the disk cache: the second file reuses the class worked out for the first, and its answer
    # depends on the same chain
    _write(tmp_path, "mid.py", "from base import Base\n")
    _write(tmp_path, "app2.py", "from models import Model\n\nModel(a=1, b=2)\n")
    (tmp_path / ".verinoda").mkdir()

    def both():
        r = codecheck.check(tmp_path, ["app.py", "app2.py"], env="none", include_exists=True)
        return r["cache"]["hits"], sorted(s["verdict"] for s in r["sites"] if s["name"] == "b")
    assert both() == (0, ["absent", "absent"])
    _write(tmp_path, "mid.py", "from base2 import Base\n")
    assert both() == (0, ["exists", "exists"])
    codecheck.reset_caches()


GUARDS = '''\
import json
import os

IS_PROD = True
HAS_FAST = False
try:
    import fastjson_not_here
    HAS_FAST = True
except ImportError:
    pass


def reraised(path):
    try:
        return json.loads_file(path)
    except Exception as exc:
        raise exc


def bare_reraised():
    try:
        return os.getcwdu()
    except:  # noqa: E722
        raise


def kw_reraised():
    try:
        return sorted([2, 1], reversed=True)
    except Exception:
        raise


def fallback():
    try:
        return os.getcwdu()
    except Exception:
        return os.getcwd()


def specific():
    try:
        return json.dumps_file
    except AttributeError:
        raise


def flags():
    if IS_PROD:
        json.loads_text("x")
    if HAS_FAST:
        json.fast_dumps("x")


try:
    import yaml_not_here_either
except Exception:
    raise SystemExit("pip install pyyaml")
'''


def test_broad_handlers_that_raise_again_and_constant_flags_are_not_guards(tmp_path):
    _write(tmp_path, "guards.py", GUARDS)
    res = codecheck.check(tmp_path, ["guards.py"], env="none", use_cache=False)
    by = {s["name"]: s for s in res["sites"]}
    for name in ("loads_file", "reversed", "loads_text"):
        assert by[name]["verdict"] == "absent", by[name]
    # the same invented name: absent where the handler raises again, and absent where a broad handler falls
    # back - `except Exception` is no guard for an attribute (it would also swallow a typo); it says so
    getcwdu = [s for s in res["sites"] if s["name"] == "getcwdu"]
    assert [s["verdict"] for s in getcwdu] == ["absent", "absent"]
    assert "swallowed_by" not in getcwdu[0] and getcwdu[1]["swallowed_by"].startswith("try/except Exception")
    assert "would swallow the error" in getcwdu[1]["message"]
    assert by["dumps_file"]["verdict"] == "guarded"                    # a specific handler guards
    assert by["fast_dumps"]["verdict"] == "guarded"                    # HAS_FAST is set by an import test
    assert by["yaml_not_here_either"]["verdict"] == "guarded"          # an import under a broad handler
    assert res["exit"] == 3


def test_a_project_module_off_the_search_path_is_never_called_missing_from_the_project(tmp_path):
    _write(tmp_path, "lib/helpers.py", "def make_user():\n    return 1\n")
    _write(tmp_path, "pkgz/__init__.py", "")
    _write(tmp_path, "pkgz/robot.py", "X = 1\n")          # inside a package: imported as pkgz.robot only
    _write(tmp_path, "app/main.py", "import helpers\nimport no_such_module_xyz\nimport robot\nimport helpers.sub\n")
    res = codecheck.check(tmp_path, ["app/main.py"], env="none", include_exists=True, use_cache=False)
    by = {s["expr"]: s for s in res["sites"]}
    assert by["helpers"]["verdict"] == "unknown" and "lib/helpers.py" in by["helpers"]["why"]
    for expr in ("no_such_module_xyz", "robot", "helpers.sub"):   # helpers.py is a module: no helpers.sub
        assert by[expr]["verdict"] == "not_installed", by[expr]


def test_a_module_level_receiver_says_so_and_unparsed_files_are_incomplete(tmp_path):
    _write(tmp_path, "mod.py", "class Client:\n    pass\n\n\nclient = Client()\n\n\ndef run():\n"
                               "    return client.fetch_all()\n")
    _write(tmp_path, "broken.py", "def f(:\n    pass\n")
    res = codecheck.check(tmp_path, ["mod.py", "broken.py"], env="none", use_cache=False)
    (s,) = [x for x in res["sites"] if x["name"] == "fetch_all"]
    assert s["verdict"] == "unknown" and "module-level" in s["why"]
    assert res["exit"] == 0 and "broken.py (does not parse" in res["incomplete"][0]


# -- third review round -----------------------------------------------------------------------------------------

@pytest.mark.skipif(sys.version_info < (3, 10), reason="collections lost its ABC aliases in Python 3.10")
def test_a_name_a_stdlib_stub_imports_for_its_annotations_is_not_a_name_of_the_module(tmp_path):
    # typeshed's collections stub imports Mapping from collections.abc for its own annotations; jedi follows
    # that to typing.py, but the interpreter's collections has no Mapping: never "exists"
    other = "select" if sys.platform == "win32" else "_winapi"   # bound by subprocess.py for another platform
    _write(tmp_path, "a.py", "import collections\nimport os\nimport subprocess\n\n"
                             f"print(collections.Mapping, os.Mapping, collections.OrderedDict, subprocess.{other})\n")
    _write(tmp_path, "b.py", "from collections import Iterable, OrderedDict\n")
    res = codecheck.check(tmp_path, ["a.py", "b.py"], env="none", include_exists=True, use_cache=False)
    v = {s["expr"]: s for s in res["sites"]}
    assert v["collections.Mapping"]["verdict"] == "unknown" and v["collections.Iterable"]["verdict"] == "unknown"
    assert v["os.Mapping"]["verdict"] == "exists" and v["collections.OrderedDict"]["verdict"] == "exists"
    s = v[f"subprocess.{other}"]
    assert s["verdict"] == "unknown" and "the module's source binds it" in s["why"], s
    snip = codecheck.check(tmp_path, snippet="from collections import Mapping\n", as_path="s.py", env="none",
                           include_exists=True)
    assert [x["verdict"] for x in snip["sites"] if x["name"] == "Mapping"] == ["unknown"]
    assert codecheck.api(tmp_path, "collections.Mapping", env="none")["found"] is not True


def _popen_log(monkeypatch) -> list[str]:
    started: list[str] = []
    orig = subprocess.Popen.__init__

    def rec(self, args, *a, **kw):
        started.append(str(args[0] if isinstance(args, (list, tuple)) else args))
        return orig(self, args, *a, **kw)

    monkeypatch.setattr(subprocess.Popen, "__init__", rec)
    return started


def test_mcp_never_starts_a_program_the_checked_repository_supplies(tmp_path, monkeypatch):
    from verinoda.mcp.server import AtlasTools

    repo = tmp_path / "repo"
    tools = repo / "tools"
    for exe in ("python.exe", "python3", "python"):
        _write(tools, exe, "not an interpreter\n")
    _write(repo, ".venv/pyvenv.cfg", f"home = {tools}\nversion = 3.12.0\n")
    _write(repo, "app.py", "import json\n\njson.dumps(1)\n")
    (repo / ".verinoda").mkdir()
    started = _popen_log(monkeypatch)
    t = AtlasTools(repo)
    auto = t.code_check(paths=["app.py"])
    note = auto["env"]["note"]
    assert auto["env"]["kind"] == "verinoda" and "inside the project" in note and "would start" in note
    assert "to trust it" not in note
    for env in (".venv", "tools/python.exe", str(tools / "python.exe"), "tools"):
        r = t.code_check(paths=["app.py"], env=env)
        assert r["error"] == "invalid_argument", (env, r)
        assert t.api_members("json", env=env)["error"] == "invalid_argument", env
    assert not [s for s in started if Path(s).resolve().is_relative_to(repo.resolve())], started
    # a virtual environment whose base interpreter is a known installation outside the project is accepted
    ok = tmp_path / "ok"
    _make_venv(ok)
    _write(ok, "app.py", "import fancylib\n\nfancylib.fake_fn(1)\n")
    (ok / ".verinoda").mkdir()
    r = AtlasTools(ok).code_check(paths=["app.py"], env=".venv")
    assert r["env"]["kind"] == "explicit" and r["summary"]["absent"] == 1, r
    codecheck.reset_caches()


DESCRIPTORS = '''\
import functools


class Memo:
    def __init__(self, fn):
        self.fn = fn

    def __set_name__(self, owner, name):
        self.key = "_" + name + "_cache"

    def __get__(self, obj, owner=None):
        if obj is None:
            return self
        v = self.fn(obj)
        setattr(obj, self.key, v)
        return v


class Lazy:
    def __init__(self, fn):
        self.fn = fn

    def __get__(self, obj, owner=None):
        obj.__dict__["_loaded_" + self.fn.__name__] = True
        return self.fn(obj)


def lazy_property(fn):
    attr_name = "_lazy_" + fn.__name__

    @property
    def _lazy_property(self):
        if not hasattr(self, attr_name):
            setattr(self, attr_name, fn(self))
        return getattr(self, attr_name)
    return _lazy_property


def remember(prefix):
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(self, *args):
            setattr(self, prefix + fn.__name__, fn(self, *args))
            return getattr(self, prefix + fn.__name__)
        return wrapper
    return deco


class Field:
    def __set_name__(self, owner, name):
        setattr(owner, name + "_default", 0)


def logged(fn):
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        print(fn.__name__)
        return fn(self, *args, **kwargs)
    return wrapper


REG = []


def register(fn):
    REG.append(fn.__name__)
    return fn


class A:
    @Memo
    def total(self):
        return 3


class B:
    @Lazy
    def data(self):
        return [1]


class C:
    @lazy_property
    def rows(self):
        return [1, 2]


class D:
    @remember("_last_")
    def compute(self):
        return 5


class E:
    size = Field()


class Plain:
    @property
    def p(self):
        return 1

    @functools.cached_property
    def q(self):
        return 2

    @logged
    def go(self):
        return 3

    @register
    def r(self):
        return 4


def use():
    a = A()
    b = B()
    c = C()
    d = D()
    p = Plain()
    return a._total_cache, b._loaded_data, c._lazy_rows, d._last_compute, E.size_default, p.missing_name
'''


def test_descriptors_and_wrappers_that_set_attributes_on_the_instance_open_it(tmp_path):
    _write(tmp_path, "desc.py", DESCRIPTORS)
    res = codecheck.check(tmp_path, ["desc.py"], env="none", include_exists=True, use_cache=False)
    by = {s["name"]: s for s in res["sites"] if s["kind"] == "attribute"}
    for name, why in (("_total_cache", "Memo.__get__"), ("_loaded_data", "Lazy.__get__"),
                      ("_lazy_rows", "_lazy_property"), ("_last_compute", "wrapper"),
                      ("size_default", "Field.__set_name__")):
        assert by[name]["verdict"] == "unknown" and why in by[name]["why"], by[name]
    # property, cached_property, a wrapper that only calls the method, a registering decorator: still closed
    assert by["missing_name"]["verdict"] == "absent", by["missing_name"]


def test_every_way_of_changing_sys_path_or_sys_modules_counts(tmp_path):
    import ast

    from verinoda import codecheck_facts as cf

    edits = ["import sys\nsys.path.insert(0, D)", "import sys\nsys.path += [D]", "import sys\nsys.path[:0] = [D]",
             "import sys as _s\n_s.path.append(D)", "from sys import path\npath.insert(0, D)",
             "from sys import path as p\np += [D]", "import site\nsite.addsitedir(D)",
             "from site import addsitedir\naddsitedir(D)", "import sys\nsys.path = [D, *sys.path]",
             "import sys\nsys.modules['shared_util'] = object()", "import sys\nsys.meta_path.append(F)",
             "import sys\ndef pytest_configure(config):\n    sys.path.extend([D])"]
    reads = ["import sys\nprint(sys.path)", "import sys\nx = sys.path[0]", "import sys\nsys.path.index(D)",
             "from sys import path\npath = [D]", "import sys\nm = sys.modules.get('x')", "import os\nos.path.join(D)"]
    for src in edits:
        assert cf.changes_import_path(ast.parse(src)), src
    for src in reads:
        assert not cf.changes_import_path(ast.parse(src)), src
    # end to end: a conftest.py and the checked script itself
    _write(tmp_path, "shared/shared_util.py", "def helper():\n    return 1\n")
    app = tmp_path / "app"
    _write(app, "tests/conftest.py", "import sys\nsys.path[:0] = [r'" + str(tmp_path / "shared") + "']\n")
    _write(app, "tests/test_x.py", "import shared_util\n")
    _write(app, "run.py", "import sys\nfrom pathlib import Path\n\n"
                          "sys.path += [str(Path(__file__).resolve().parents[1] / 'shared')]\nimport shared_util\n")
    res = codecheck.check(app, ["tests/test_x.py", "run.py"], env="none", use_cache=False)
    got = {s["path"]: s for s in res["sites"] if s["expr"] == "shared_util"}
    assert got["tests/test_x.py"]["verdict"] == "unknown" and "conftest.py" in got["tests/test_x.py"]["why"]
    assert got["run.py"]["verdict"] == "unknown" and "this file changes sys.path" in got["run.py"]["why"]


def test_conftest_and_pytest_config_changes_drop_cached_import_answers(tmp_path):
    _write(tmp_path, "shared/shared_util.py", "def helper():\n    return 1\n")
    app = tmp_path / "app"
    (app / ".verinoda").mkdir(parents=True)
    _write(app, "tests/test_x.py", "import shared_util\n")

    def both() -> tuple:   # (with the cache, fresh) in one long-lived process
        a = codecheck.check(app, ["tests/test_x.py"], env="none", include_exists=True)
        b = codecheck.check(app, ["tests/test_x.py"], env="none", use_cache=False, include_exists=True)
        return tuple([s["verdict"] for s in r["sites"] if s["expr"] == "shared_util"][0] for r in (a, b))

    assert both() == ("not_installed", "not_installed")
    _write(app, "tests/conftest.py", "import sys\nsys.path.insert(0, '../shared')\n")
    assert both() == ("unknown", "unknown")
    (app / "tests" / "conftest.py").unlink()
    assert both() == ("not_installed", "not_installed")
    _write(app, "pytest.ini", "[pytest]\npythonpath = ../shared\n")
    assert both() == ("exists", "exists")
    (app / "pytest.ini").unlink()   # the warm checker's jedi project must forget the directory too
    assert both() == ("not_installed", "not_installed")
    codecheck.reset_caches()


def test_diff_ignores_the_users_prefix_settings_and_works_from_a_subdirectory(tmp_path):
    _write(tmp_path, "app.py", "import json\n\nprint(json.dumps(1))\n")
    _write(tmp_path, "pkgdir/tracked.py", "import json\n\nprint(json.dumps(1))\n")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "x")
    _write(tmp_path, "app.py", "import json\n\nprint(json.dumps(1))\nprint(json.loadz('1'))\n")
    _write(tmp_path, "pkgdir/tracked.py", "import json\n\nprint(json.dumps(1))\nprint(json.loadz('1'))\n")
    _write(tmp_path, "pkgdir/new.py", "import json\n\njson.loadz\n")
    want = [("app.py", "loadz"), ("pkgdir/new.py", "loadz"), ("pkgdir/tracked.py", "loadz")]
    for key, value in (("diff.mnemonicPrefix", "true"), ("diff.dstPrefix", "new/"), ("diff.noprefix", "true")):
        _git(tmp_path, "config", key, value)
        res = codecheck.check(tmp_path, diff="HEAD", env="none", use_cache=False)
        got = sorted((s["path"], s["name"]) for s in res["sites"] if s["verdict"] == "absent")
        assert got == want, (key, got)
        _git(tmp_path, "config", "--unset", key)
    sub = codecheck.check(tmp_path / "pkgdir", diff="HEAD", env="none", use_cache=False)
    assert sorted((s["path"], s["name"]) for s in sub["sites"] if s["verdict"] == "absent") == \
        [("new.py", "loadz"), ("tracked.py", "loadz")]
    codecheck.reset_caches()


LOCALS = '''\
import collections
import sys
import threading


def literal_dict():
    d = {"a": 1}
    return d.iteritems()


def deque_returned():
    dq = collections.deque()
    dq.push(1)
    return dq


def thread_keyword():
    return threading.Thread(target=print, deamon=True)


def version():
    return sys.version_info.majr
'''


def test_literal_locals_dict_less_instances_and_constructor_keywords_are_decided(tmp_path):
    _write(tmp_path, "loc.py", LOCALS)
    res = codecheck.check(tmp_path, ["loc.py"], env="none", use_cache=False)
    by = {s["name"]: s for s in res["sites"]}
    for name in ("iteritems", "push", "deamon"):
        assert by[name]["verdict"] == "absent", by[name]
    assert by["majr"]["verdict"] == "unknown" and "sys.version_info" in by["majr"]["why"]
    assert "statement" not in by["majr"]["why"]


def test_a_jedi_internal_error_is_named_in_the_unknown(tmp_path, monkeypatch):
    # jedi raises inside its own inference for some names, depending on set order (starlette's
    # self.router.routes): the answer is unknown either way, and says so instead of blaming the receiver
    _write(tmp_path, "m.py", "def f(x):\n    return x.value.inner\n")
    orig = jedi.Script.goto

    def goto(self, line=None, column=None, **kw):
        if line == 2 and column >= len("    return x.value."):
            raise AttributeError("boom")
        return orig(self, line, column, **kw)

    monkeypatch.setattr(jedi.Script, "goto", goto)
    res = codecheck.check(tmp_path, ["m.py"], env="none", use_cache=False)
    (s,) = [x for x in res["sites"] if x["name"] == "inner"]
    assert s["verdict"] == "unknown" and "jedi also failed internally" in s["why"] and "boom" in s["why"], s


def test_a_class_whose_base_is_a_call_and_a_failing_site_do_not_stop_the_check(tmp_path, monkeypatch):
    _write(tmp_path, "nt.py", "from collections import namedtuple\n\n\nclass P(namedtuple('P', 'x y')):\n    pass\n\n\n"
                              "def use():\n    p = P(1, 2)\n    return p.x, p.zz, P(1, 2).zz\n")
    res = codecheck.check(tmp_path, ["nt.py"], env="none", use_cache=False)
    zz = [s for s in res["sites"] if s["name"] == "zz"]
    assert len(zz) == 2 and all(s["verdict"] == "unknown" and "is an expression" in s["why"] for s in zz), zz
    # a defect of the check on one site: that site is unknown and listed as not decided, the rest is checked
    orig = codecheck.Checker.eval_attribute

    def boom(self, fx, node):
        if node.attr == "zz":
            raise KeyError("defect")
        return orig(self, fx, node)

    monkeypatch.setattr(codecheck.Checker, "eval_attribute", boom)
    res = codecheck.check(tmp_path, ["nt.py"], env="none", include_exists=True, use_cache=False)
    zz = [s for s in res["sites"] if s["name"] == "zz"]
    assert len(zz) == 2 and all(s["verdict"] == "unknown" and "check failed" in s["why"] for s in zz)
    assert any(s["name"] == "x" and s["verdict"] == "exists" for s in res["sites"])
    assert "the check failed on 2 sites" in res["incomplete"][0] and "check_error" not in zz[0]


def test_a_venv_made_from_another_venv_is_followed_to_its_base(tmp_path):
    """Python 3.10 writes the ``bin`` of the environment that ran ``python -m venv`` as ``home``: the base
    interpreter is found through that environment's own pyvenv.cfg, as the interpreter itself finds it."""
    outer = tmp_path / "outer"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(outer)], check=True, capture_output=True)
    inner = tmp_path / "inner"
    inner.mkdir()
    bindir = outer / ("Scripts" if os.name == "nt" else "bin")
    vi = sys.version_info
    (inner / "pyvenv.cfg").write_text(f"home = {bindir}\ninclude-system-site-packages = false\n"
                                      f"version = {vi[0]}.{vi[1]}.{vi[2]}\n", encoding="utf-8")
    got = cenv.base_interpreter(inner)
    assert got is not None and got == cenv.base_interpreter(outer)
    assert not got.resolve().is_relative_to(outer.resolve())
    # a chain that loops back is not followed forever
    (outer / "pyvenv.cfg").write_text(f"home = {inner}\n", encoding="utf-8")
    assert cenv.base_interpreter(inner) is None or not cenv.base_interpreter(inner).is_relative_to(outer)
