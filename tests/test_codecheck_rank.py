"""Ranked unknowns of the name check (docs/DESIGN.md D64): HIGH / MEDIUM / LOW, the summary of LOW causes, the
order sites are listed in, the one-line MCP sites, expected-failure guards and optional dependencies.

``env="none"`` (the running interpreter's standard library) keeps these fast; the name index then covers the
standard library, jedi's stubs and the project.
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

from verinoda import codecheck, codecheck_rank  # noqa: E402

jedi = pytest.importorskip("jedi", reason="optional extra 'precise' (jedi) not installed")


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(text), encoding="utf-8", newline="\n")


APP = '''\
import argparse
import json


def handle(job, items):
    job.frobnicate_zqx()
    items.append(1)
    return json.loadz


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-mcp", action="store_true")
    args = ap.parse_args()
    return args.no_mcp, args.no_mpc_zq
'''


@pytest.fixture(scope="module")
def ranked(tmp_path_factory):
    root = tmp_path_factory.mktemp("rank")
    _write(root, "app.py", APP)
    yield root, codecheck.check(root, ["app.py"], env="none", use_cache=False)
    codecheck.reset_caches()


def _one(res: dict, name: str) -> dict:
    hits = [s for s in res["sites"] if s["name"] == name]
    assert len(hits) == 1, (name, res["sites"])
    return hits[0]


def test_a_name_defined_nowhere_is_high_and_listed_right_after_the_absent_ones(ranked):
    _root, res = ranked
    high = _one(res, "frobnicate_zqx")
    assert high["verdict"] == "unknown" and high["rank"] == "high" and "defined nowhere" in high["rank_why"]
    assert high["next_step"]
    order = [(s["verdict"], s.get("rank")) for s in res["sites"]]
    assert order[0] == ("absent", None) and order[1] == ("unknown", "high")   # json.loadz, then the HIGH site
    # argparse makes no_mcp of "--no-mcp": a real option is not HIGH, a misspelt one is
    assert not [s for s in res["sites"] if s["name"] == "no_mcp"]              # LOW: counted, not listed
    assert _one(res, "no_mpc_zq")["rank"] == "high"


def test_low_unknowns_are_counted_by_cause_and_listed_only_with_include_exists(ranked):
    root, res = ranked
    assert not [s for s in res["sites"] if s["name"] == "append"]              # a parameter's common name
    us = res["unknown_summary"]
    assert us["high"] == 2 and us["low"] >= 2 and us["high"] + us["medium"] + us["low"] == res["summary"]["unknown"]
    causes = {g["cause"]: g for g in us["low_by_cause"]}
    assert causes["the receiver is a parameter"]["sites"] >= 1
    assert all(g["next_step"] and g["example"] for g in us["low_by_cause"])
    full = codecheck.check(root, ["app.py"], env="none", use_cache=False, include_exists=True)
    low = _one(full, "append")
    assert low["rank"] == "low" and low["next_step"]
    ranks = [s.get("rank") for s in full["sites"] if s["verdict"] == "unknown"]
    assert ranks == sorted(ranks, key=lambda r: codecheck_rank.RANK_ORDER[r])   # HIGH first, LOW last
    assert all(s.get("next_step") for s in full["sites"] if s["verdict"] == "unknown")


def test_a_misspelling_in_a_comment_elsewhere_counts_as_defined_but_not_in_the_checked_file(tmp_path):
    _write(tmp_path, "a.py", "def f(x):\n    return x.qqzz_widget\n")
    _write(tmp_path, "b.py", "# qqzz_widget is mentioned here\n")
    res = codecheck.check(tmp_path, ["a.py"], env="none", use_cache=False, include_exists=True)
    assert _one(res, "qqzz_widget")["rank"] == "low"                           # a word elsewhere: a superset
    (tmp_path / "b.py").unlink()
    _write(tmp_path, "a.py", "def f(x):\n    # qqzz_widget\n    return x.qqzz_widget\n")
    res = codecheck.check(tmp_path, ["a.py"], env="none", use_cache=False, include_exists=True)
    assert _one(res, "qqzz_widget")["rank"] == "high"                          # the checked file only reads it


def test_a_receiver_from_a_module_that_is_not_installed_is_medium_never_high(tmp_path):
    _write(tmp_path, "a.py", "import numpy_not_here_zq as np\n\n\ndef f():\n    return np.zq_linspace_x(1)\n")
    res = codecheck.check(tmp_path, ["a.py"], env="none", use_cache=False)
    s = _one(res, "zq_linspace_x")
    assert s["rank"] == "medium" and "numpy_not_here_zq" in s["rank_why"]


def test_expected_failures_in_raises_blocks_are_guarded(tmp_path):
    _write(tmp_path, "test_x.py", '''\
        import pytest


        def test_gone():
            with pytest.raises(ImportError):
                from json import no_such_name_zq
            with pytest.raises(AttributeError):
                import json
                json.no_such_attr_zq
            with pytest.raises(ValueError):
                from json import other_missing_zq
        ''')
    res = codecheck.check(tmp_path, ["test_x.py"], env="none", use_cache=False)
    by = {s["name"]: s for s in res["sites"]}
    assert by["no_such_name_zq"]["verdict"] == "guarded" and "pytest.raises(ImportError)" in \
        by["no_such_name_zq"]["guard"]
    # an attribute among other statements of a raises(AttributeError) block: the block may expect another
    # error, and a misspelt name makes the test pass for the wrong reason - it stays absent (review M4)
    assert by["no_such_attr_zq"]["verdict"] == "absent"
    assert by["other_missing_zq"]["verdict"] == "absent"                      # ValueError is not what it raises


def _make_venv(root: Path) -> Path:
    venv = root / ".venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True, capture_output=True)
    return venv


def test_an_optional_dependency_listed_in_a_requirements_file_is_not_installed_not_absent(tmp_path):
    _make_venv(tmp_path)
    _write(tmp_path, "app/db.py", "import optional_driver_zq\nimport made_up_module_zq\n")
    _write(tmp_path, "tests/requirements/postgres.txt", "optional-driver-zq\n")
    res = codecheck.check(tmp_path, ["app/db.py"], env=".venv", use_cache=False)
    by = {s["name"]: s for s in res["sites"]}
    opt = by["optional_driver_zq"]
    assert opt["verdict"] == "not_installed" and opt.get("optional") is True
    assert "tests/requirements/postgres.txt:1" in opt["why"]
    assert by["made_up_module_zq"]["verdict"] == "absent"
    codecheck.reset_caches()


def test_mcp_code_check_lists_one_line_per_site_in_rank_order(ranked):
    from verinoda.mcp.server import AtlasTools, cap_response, compact_check_result

    root, res = ranked
    (root / ".verinoda").mkdir(exist_ok=True)
    out = AtlasTools(root).code_check(paths=["app.py"], env="none")
    assert list(out)[:5] == ["status", "summary", "exit", "exit_because", "env"]
    assert all(isinstance(s, str) for s in out["sites"])
    assert out["sites"][0].startswith("ABSENT app.py:") and out["sites"][1].startswith("HIGH app.py:")
    assert "defined nowhere" in out["sites"][1] and ("next:" in out["sites"][1] or "nearest:" in out["sites"][1])
    assert out["unknown_summary"]["high"] == 2 and isinstance(out["unknown_summary"]["low_by_cause"][0], str)
    # the cap cuts from the end: many LOW lines never push an absent or HIGH site out
    many = dict(res, sites=res["sites"] + [dict(res["sites"][-1], rank="low", at=f"app.py:{i}:1",
                                                    why="x" * 200) for i in range(400)])
    capped = cap_response(compact_check_result(many), 12000, first=("sites",))
    assert capped["truncated"] and capped["sites"][0].startswith("ABSENT") and capped["sites"][1].startswith("HIGH")


def test_cli_labels_ranked_unknowns_and_prints_the_summary(ranked, capsys):
    from verinoda import cli

    root, _res = ranked
    rc = cli.main(["check", "app.py", "--repo", str(root), "--env", "none", "--no-cache"])
    out = capsys.readouterr().out
    assert rc == 3 and "unknown HIGH  attribute job.frobnicate_zqx" in out
    assert "unknown: 2 HIGH (likely mistakes)" in out and "LOW" in out
    rc = cli.main(["check", "app.py", "--repo", str(root), "--env", "none", "--no-cache", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert data["unknown_summary"]["high"] == 2


DECLARED = '''\
import re
from pathlib import Path


class Store:
    def close(self):
        return None


def use(lines: list[str], root: Path, st: Store):
    a = [ln for ln in lines if ln.startswith("#")]
    b = [ln for ln in lines if ln.strat_with("#")]
    (root / "x").mkdir(parents=True)
    (root / "x").mdkir()
    (root / "x" / "y").write_text("t", encoding="utf-8")
    (root / "x" / "y").write_text("t", encdoing="utf-8")
    st.colse()
    st.close()
    p = root / "x"
    p.mkdir()
    root.as_posix().starts_with("x")
    return a, b, [m for m in map(re.compile, lines) if m.pattern]


def test_fixture(tmp_path, monkeypatch):
    tmp_path.mkdir(exist_ok=True)
    tmp_path.mdkir()
    monkeypatch.setattr("os.sep", "/")
    monkeypatch.setatr("os.sep", "/")


def test_own_fixture(request):
    return request.node, request.anything_goes_zq
'''


def test_declared_types_decide_exists_and_rank_a_close_misspelling_high(tmp_path):
    _write(tmp_path, "test_decl.py", DECLARED)
    # the interpreter running the tests has pytest: its fixture classes are read from there
    res = codecheck.check(tmp_path, ["test_decl.py"], env=sys.executable, use_cache=False, include_exists=True)
    by = {(s["line"], s["name"]): s for s in res["sites"]}

    def at(text: str, name: str) -> dict:
        line = next(i for i, ln in enumerate(DECLARED.splitlines(), 1) if text in ln)
        return by[(line, name)]

    # jedi 0.20 infers nothing for a comprehension variable in the `if` clause: read at the `for` target
    assert at('if ln.startswith("#")', "startswith")["verdict"] == "exists"
    assert at('re.compile, lines) if m.pattern', "pattern")["verdict"] == "exists"
    # Path / "x": jedi reads PurePath's `-> Self` as PurePath; the left operand's class is the answer
    assert at('(root / "x").mkdir(parents=True)', "mkdir")["verdict"] == "exists"
    assert at('(root / "x").mkdir(parents=True)', "parents")["verdict"] == "exists"
    assert at('encoding="utf-8"', "encoding")["verdict"] == "exists"
    assert at("st.close()", "close")["verdict"] == "exists"
    assert at("p.mkdir()", "mkdir")["verdict"] == "exists"                  # a local bound once to a path join
    ret = at('root.as_posix().starts_with("x")', "starts_with")              # what the called method returns
    if ret.get("declared"):
        assert ret["declared"] == "builtins.str" and ret["rank"] == "high"
        assert ret["nearest"][0]["name"] == "startswith"
    else:  # Python 3.13's pathlib: jedi 0.20 reads no declared return for as_posix; the site stays unknown
        assert ret["verdict"] == "unknown", ret
    # pytest's own fixtures: an unannotated test parameter named so has the fixture's type
    assert at("tmp_path.mkdir(exist_ok=True)", "mkdir")["verdict"] == "exists"
    assert at('monkeypatch.setattr("os.sep"', "setattr")["verdict"] == "exists"
    # a declared type decides exists only: a name it lacks stays unknown, HIGH when a close name is there
    for text, name, close in (('ln.strat_with("#")', "strat_with", "startswith"), ('.mdkir()', "mdkir", "mkdir"),
                              ('encdoing="utf-8"', "encdoing", "encoding"), ("st.colse()", "colse", "close"),
                              ("tmp_path.mdkir()", "mdkir", "mkdir"), ("monkeypatch.setatr", "setatr", "setattr")):
        s = at(text, name)
        assert s["verdict"] == "unknown" and s["rank"] == "high" and s["declared"], (name, s)
        assert s["nearest"][0]["name"] == close and "verinoda api" in s["next_step"], s
    # FixtureRequest objects keep a __dict__: a name it lacks is not "declared-type" evidence, and is ranked
    # by the name index alone
    assert at("request.node, request.anything_goes_zq", "node")["verdict"] == "exists"
    other = at("request.node, request.anything_goes_zq", "anything_goes_zq")
    assert other["verdict"] == "unknown" and "declared" not in other and other["rank"] == "high"


def test_a_project_fixture_of_the_same_name_is_not_typed_as_pytests(tmp_path):
    _write(tmp_path, "conftest.py", "import pytest\n\n\n@pytest.fixture\ndef tmp_path():\n    return 'mine'\n")
    _write(tmp_path, "test_own.py", "def test_x(tmp_path):\n    return tmp_path.mkdir()\n")
    res = codecheck.check(tmp_path, ["test_own.py"], env="none", use_cache=False, include_exists=True)
    s = _one(res, "mkdir")   # the project's tmp_path is a str (jedi infers it): pytest's Path must not answer
    assert s["verdict"] == "unknown" and s.get("declared") == "builtins.str"


def test_sites_of_other_languages_are_counted_as_not_ranked(tmp_path):
    java = {"at": "A.java:3:5", "path": "A.java", "line": 3, "col": 5, "kind": "method", "expr": "x.f()",
            "name": "f", "verdict": "unknown", "why": "the receiver's type is not known", "language": "Java"}
    got = codecheck_rank.rank_sites(None, tmp_path, [java], [], jedi=True)
    assert got["not_ranked"] == 1 and got["high"] == got["medium"] == got["low"] == 0 and "rank" not in java
    assert codecheck_rank.order_key(java)[0] == 3                              # listed with the MEDIUM ones


def test_defined_names_reads_definitions_not_uses():
    import ast

    tree = ast.parse("import a.b as c\nclass K:\n    x: int = 1\ndef f(p, *q, k=2, **kw):\n    o.attr = 1\n"
                     "    return o.used\nsetattr(o, 'dyn', 1)\nparser.add_argument('--dry-run')\n")
    got = codecheck_rank.defined_names(tree)
    assert {"c", "a", "b", "K", "x", "f", "p", "q", "k", "kw", "attr", "dyn", "dry_run"} <= got
    assert "used" not in got and "add_argument" not in got


# -- review of D64 (H1-H2, M1-M6, L1-L4): each test is the review's reproduction -------------------------------

def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args],
                   cwd=cwd, check=True, capture_output=True)


def test_an_invented_import_that_only_resembles_a_nested_requirement_stays_absent(tmp_path):
    # H1 + L3: docs/requirements.txt lists sentryzq-sdk; `import sentryzq` is what an invented import looks like
    _make_venv(tmp_path)
    _write(tmp_path, "docs/requirements.txt", "sphinxzq\nsentryzq-sdk\ngoogle-cloud-storagezq\n")
    _write(tmp_path, "app/obs.py", "import sentryzq\nfrom storagezq import Client\nimport sphinxzq\n\n"
                                   "sentryzq.init(dsn='x')\n")
    res = codecheck.check(tmp_path, ["app/obs.py"], env=".venv", use_cache=False)
    by = {s["name"]: s for s in res["sites"]}
    for name, listed in (("sentryzq", "sentryzq-sdk"), ("storagezq", "google-cloud-storagezq")):
        s = by[name]
        assert s["verdict"] == "absent" and not s.get("optional"), s
        assert "docs/requirements.txt:" in s["message"] and listed in s["message"], s   # a hint, not a verdict
    assert res["exit"] == 3
    exact = by["sphinxzq"]                                  # the exact name is still an optional dependency
    assert exact["verdict"] == "not_installed" and exact.get("optional") is True
    assert "(listed in docs/requirements.txt:1)" in exact["why"] and "probably" not in exact["why"]
    codecheck.reset_caches()


def test_a_prose_file_in_a_requirements_directory_lists_no_packages(tmp_path):
    # L4: a README in requirements/ is not a requirements file; a real one with options and hashes still is
    _make_venv(tmp_path)
    # nested: local_versions (the project's own manifest, declared()) reads only the root's requirements files
    _write(tmp_path, "docs/requirements/README.txt", "Install these with pip first.\nSee the docs for details.\n")
    (tmp_path / "docs" / "requirements" / "base.txt").write_text(
        "# pinned\nrealpkgzq==1.0 " + chr(92) + "\n    --hash=sha256:abc\n-r common.txt\n"
        "otherzq[extra]>=2; python_version > '3.8'\n", encoding="utf-8", newline="\n")
    _write(tmp_path, "app/m.py", "import install\nimport see\nimport realpkgzq\nimport otherzq\n")
    res = codecheck.check(tmp_path, ["app/m.py"], env=".venv", use_cache=False)
    by = {s["name"]: (s["verdict"], bool(s.get("optional"))) for s in res["sites"]}
    assert by == {"install": ("absent", False), "see": ("absent", False), "realpkgzq": ("not_installed", True),
                  "otherzq": ("not_installed", True)}, by
    assert codecheck._requirement_names(["Install these with pip first."]) is None
    codecheck.reset_caches()


STATS = "import numpy_zq as np\n\n\ndef fit(xs, ys):\n    return xs\n"


def test_a_receiver_imported_on_an_unchanged_line_of_a_diff_is_medium(tmp_path):
    # H2: the import line did not change, so it has no site in a diff; the rank reads the file's own imports
    _write(tmp_path, "stats.py", STATS)
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "x")
    _write(tmp_path, "stats.py", STATS.replace("return xs", "return np.zq_polyfit_x(xs, ys, 2)"))
    res = codecheck.check(tmp_path, diff="HEAD", env="none", use_cache=False)
    s = _one(res, "zq_polyfit_x")
    assert s["rank"] == "medium" and "numpy_zq" in s["rank_why"], s
    codecheck.reset_caches()


def test_a_receiver_imported_in_a_snippet_is_medium_whatever_the_file_on_disk_holds(tmp_path):
    # H2: snippet mode reads the snippet's own imports, not the file on disk (absent, or with other lines)
    code = "import numpy_zq as np\n\n\ndef fit(xs, ys):\n    return np.zq_polyfit_x(xs, ys, 2)\n"
    res = codecheck.check(tmp_path, None, snippet=code, as_path="pkg/stats.py", env="none", use_cache=False)
    assert _one(res, "zq_polyfit_x")["rank"] == "medium"
    _write(tmp_path, "pkg/stats.py", "x = 1\n")
    res = codecheck.check(tmp_path, None, snippet=code, as_path="pkg/stats.py", env="none", use_cache=False)
    assert _one(res, "zq_polyfit_x")["rank"] == "medium"


def test_a_local_bound_from_a_module_that_is_not_installed_is_medium_in_its_function(tmp_path):
    # L-extra: df = pd.read_csv(p); g = df.groupby(...): g's names are the uninstalled package's, not a mistake
    _write(tmp_path, "a.py", "import pandas_zq as pd\n\n\ndef f(path):\n    df = pd.read_csv(path)\n"
                             "    g = df.groupby('k')\n    return g.agg('sum').zq_reset_index_x()\n\n\n"
                             "def h(df):\n    return df.zq_other_name_x\n")
    res = codecheck.check(tmp_path, ["a.py"], env="none", use_cache=False, include_exists=True)
    assert _one(res, "zq_reset_index_x")["rank"] == "medium"
    assert _one(res, "zq_other_name_x")["rank"] == "high"            # another function's df is a parameter


SHAPES = '''\
class Reader:
    def close(self):
        return None


class Pipe:
    def closed(self):
        return True


def is_done(stream):
    return stream.closed()


def demo():
    return is_done(Reader())


def opts(**kwargs):
    return kwargs.gett("x")
'''


def test_an_unannotated_parameter_takes_no_declared_type_from_one_caller(tmp_path):
    # M1: jedi infers `stream` from the call site is_done(Reader()); Pipe callers make `closed` right
    _write(tmp_path, "shapes.py", SHAPES)
    _write(tmp_path, "other.py", "from shapes import Pipe, is_done\n\n\ndef run():\n    return is_done(Pipe())\n")
    res = codecheck.check(tmp_path, ["shapes.py"], env="none", use_cache=False, include_exists=True)
    s = _one(res, "closed")
    assert s["verdict"] == "unknown" and "declared" not in s and s["rank"] != "high", s
    assert "declared type" not in s["next_step"] and "declared type" not in s["rank_why"]
    kw = _one(res, "gett")                                   # **kwargs is a dict by syntax: still declared
    assert kw.get("declared") == "builtins.dict" and kw["rank"] == "high", kw


def test_an_attribute_set_by_a_keyword_in_the_checked_file_is_not_high(tmp_path):
    # M2: set_defaults(handler=...), SimpleNamespace(x=...) define attributes read in the same file
    _write(tmp_path, "app.py", '''\
        import argparse
        from types import SimpleNamespace


        def run(args):
            return args


        def load():
            return SimpleNamespace(zq_other_flag=1, zq_unread_kw=2)


        def main():
            p = argparse.ArgumentParser()
            p.set_defaults(zq_command_handler=run)
            args = p.parse_args()
            args.zq_command_handler(args)
            cfg = SimpleNamespace(zq_retry_budget_ms=3)
            return cfg.zq_retry_budget_ms, load().zq_other_flag, cfg.zq_nowhere_flag
        ''')
    res = codecheck.check(tmp_path, ["app.py"], env="none", use_cache=False, include_exists=True)
    for name in ("zq_command_handler", "zq_retry_budget_ms", "zq_other_flag"):
        for s in (x for x in res["sites"] if x["name"] == name):
            assert s.get("rank") != "high", s
    assert _one(res, "zq_nowhere_flag")["rank"] == "high"              # read, never set anywhere
    assert _one(res, "zq_unread_kw")["rank"] == "high"                 # a keyword does not define itself


def test_mcp_lines_keep_elsewhere_the_swallowing_handler_and_the_guard(tmp_path):
    # M3: the one-line MCP sites carry what the fix needs
    from verinoda.mcp.server import AtlasTools, check_site_line

    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/a.py", "x = 1\n")
    _write(tmp_path, "pkg/b.py", "def helper_zq():\n    return 1\n")
    _write(tmp_path, "main.py", "from pkg.a import helper_zq\n\ntry:\n    import json\n    json.lodas\n"
                                "except Exception:\n    pass\n\ntry:\n    import yaml_zq_mod\nexcept ImportError:\n"
                                "    yaml_zq_mod = None\n")
    (tmp_path / ".verinoda").mkdir()
    out = AtlasTools(tmp_path).code_check(paths=["main.py"], env="none")
    line = next(x for x in out["sites"] if "helper_zq" in x)
    assert line.startswith("ABSENT") and "elsewhere:" in line and "pkg/b.py:1" in line, line
    lodas = next(x for x in out["sites"] if "lodas" in x)
    assert "swallow" in lodas and "try/except Exception" in lodas, lodas
    guarded = next(x for x in out["sites"] if "yaml_zq_mod" in x)
    assert guarded.startswith("GUARDED") and "guard: inside try/except ImportError" in guarded, guarded
    # a long message keeps its tail
    long = check_site_line({"verdict": "absent", "at": "m.py:1:1", "kind": "attribute", "expr": "json.lodas",
                            "message": "not found in module json " + "x" * 300 + " (<stdlib>/json/__init__.py)"})
    assert long.endswith("(<stdlib>/json/__init__.py)") and " ... " in long
    codecheck.reset_caches()


def test_a_misspelling_inside_raises_type_error_is_not_guarded(tmp_path):
    # M4: `with assertRaises(TypeError): "a,b".split(sepp=1)` passes because of the typo, not the int
    _write(tmp_path, "test_parse.py", '''\
        import json
        import unittest


        class T(unittest.TestCase):
            def test_split(self):
                with self.assertRaises(TypeError):
                    "a,b".split(sepp=1)
                with self.assertRaises(TypeError):
                    int("3")
                    "a,b".split(sepp=2)
                with self.assertRaises(AttributeError):
                    json.no_such_attr_zq
        ''')
    res = codecheck.check(tmp_path, ["test_parse.py"], env="none", use_cache=False)
    sepp = sorted((s for s in res["sites"] if s["name"] == "sepp"), key=lambda s: s["line"])
    only, among = sepp
    # the whole body of the block: maybe a test that the keyword is refused, maybe a typo - MEDIUM, not guarded
    assert only["verdict"] == "unknown" and only["rank"] == "medium" and "assertRaises(TypeError)" in \
        only["rank_why"] and only["nearest"][0]["name"] == "sep", only
    assert among["verdict"] == "absent"                                # among other statements: absent
    attr = _one(res, "no_such_attr_zq")
    assert attr["verdict"] == "unknown" and attr["rank"] == "medium" and attr.get("expected_error")
    assert res["exit"] == 3


class _FakeEnv:
    def __init__(self, fp: str, site: Path):
        self.fingerprint = fp
        self.site_dirs = [str(site)]
        self.stdlib_dirs: list[str] = []


def test_the_environment_index_is_kept_in_the_user_cache_and_resumed_where_its_budget_stopped(tmp_path,
                                                                                               monkeypatch):
    # M5 + M6: kept without .verinoda/, in the user cache; a build cut by the budget is continued, not reused
    from verinoda import codecheck_env

    site = tmp_path / "site"
    for i in range(30):
        _write(site, f"p{i:02d}/m.py", f"def word_zq_{i}():\n    pass\n")
    monkeypatch.setattr(codecheck_env, "jedi_typeshed_dir", lambda: None)
    monkeypatch.setenv("VERINODA_CACHE_DIR", str(tmp_path / "cache"))
    env = _FakeEnv("fp-resume-test-" + "0" * 24, site)
    saved = dict(codecheck_rank._ENV)
    try:
        codecheck_rank._ENV.clear()
        first = codecheck_rank.env_names(env, budget=0)
        assert not first.complete and first.files < 30 and first.budget == 0
        kept = codecheck_rank.index_path(env)
        assert kept.is_file() and kept.parent.parent == tmp_path / "cache"
        assert json.loads(kept.read_text(encoding="utf-8").splitlines()[0])["complete"] is False
        codecheck_rank._ENV.clear()                                        # another process
        second = codecheck_rank.env_names(env)
        assert second.complete and second.files == 30 and {f"word_zq_{i}" for i in range(30)} <= second.words
        assert json.loads(kept.read_text(encoding="utf-8").splitlines()[0])["complete"] is True
        codecheck_rank._ENV.clear()
        kept.unlink()
        assert not codecheck_rank.env_names(env, budget=0).complete         # the same process: from memory
        assert codecheck_rank.env_names(env).complete
    finally:
        codecheck_rank._ENV.clear()
        codecheck_rank._ENV.update(saved)


def test_a_check_whose_name_index_stopped_says_which_budget_and_that_it_continues(tmp_path, monkeypatch):
    # M6: the note names the budget that stopped this index, and the project gets no .verinoda/ for it
    _write(tmp_path, "a.py", "def f(job):\n    return job.frobnicate_zqx()\n")
    monkeypatch.setenv("VERINODA_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(codecheck_rank, "ENV_INDEX_BUDGET_S", 0.0)
    saved = dict(codecheck_rank._ENV)
    try:
        codecheck_rank._ENV.clear()
        res = codecheck.check(tmp_path, ["a.py"], env="none", use_cache=False)
        s = _one(res, "frobnicate_zqx")
        assert s["rank"] == "medium" and "stopped early" in s["rank_why"]
        note = res["unknown_summary"]["note"]
        assert "0 s budget" in note and "continues" in note and "120" not in note, note
        assert not (tmp_path / ".verinoda").exists() and list((tmp_path / "cache" / "names").glob("names-*.txt"))
    finally:
        codecheck_rank._ENV.clear()
        codecheck_rank._ENV.update(saved)
        codecheck.reset_caches()


def test_a_name_index_budget_that_is_not_a_number_falls_back_to_the_default(monkeypatch):
    # L1: VERINODA_NAME_INDEX_BUDGET_S=abc broke every check (exit 1)
    monkeypatch.setenv("VERINODA_NAME_INDEX_BUDGET_S", "abc")
    with pytest.warns(UserWarning, match="not a number"):
        assert codecheck_rank._float_env("VERINODA_NAME_INDEX_BUDGET_S", 120.0) == 120.0
    monkeypatch.setenv("VERINODA_NAME_INDEX_BUDGET_S", "7.5")
    assert codecheck_rank._float_env("VERINODA_NAME_INDEX_BUDGET_S", 120.0) == 7.5
    got = subprocess.run([sys.executable, "-c", "import verinoda.codecheck_rank as r; print(r.ENV_INDEX_BUDGET_S)"],
                         capture_output=True, text=True, env={**os.environ, "VERINODA_NAME_INDEX_BUDGET_S": "abc"})
    assert got.returncode == 0 and got.stdout.strip() == "120.0", got.stderr


def test_help_texts_say_that_all_lists_the_low_unknowns():
    # L2
    from verinoda import cli
    from verinoda.mcp import server

    assert "also list the sites that exist and the LOW unknowns" in Path(cli.__file__).read_text(encoding="utf-8")
    assert "Also list the sites that exist and the LOW unknowns." in Path(server.__file__).read_text(encoding="utf-8")
