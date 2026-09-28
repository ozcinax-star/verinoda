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
    assert by["no_such_attr_zq"]["verdict"] == "guarded"
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
    assert ret["declared"] == "builtins.str" and ret["rank"] == "high" and ret["nearest"][0]["name"] == "startswith"
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
