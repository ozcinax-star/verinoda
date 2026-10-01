"""Taint analysis (``verinoda taint``): sources, sinks and sanitizers as data, paths across callers and returns,
route parameters, the project's own rules, the CLI, JSON and SARIF."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import json  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import taint  # noqa: E402

WEB = '''import os
import shlex
import sqlite3
import subprocess

from flask import Flask, request

app = Flask(__name__)
db = sqlite3.connect(":memory:")


def find_user(name):
    q = "SELECT * FROM users WHERE name = '" + name + "'"
    return db.execute(q)


@app.route("/user")
def user():
    who = request.args.get("name")
    return find_user(who)


@app.route("/safe")
def safe():
    n = int(request.args["n"])
    return db.execute("SELECT * FROM t WHERE id = %d" % n)


@app.route("/files/<path>")
def files(path):
    return open(path).read()


@app.route("/items/<int:item_id>")
def item(item_id):
    return open(str(item_id))


def get_cmd():
    return os.environ.get("CMD")


def run():
    cmd = get_cmd()
    subprocess.run(cmd, shell=True)


def run_list():
    subprocess.run([get_cmd()])


def direct():
    eval(input())


def split():
    a, b = request.args["x"], "constant"
    db.execute(b)
    db.execute(a)


def gather():
    parts = []
    parts.append(request.form["x"])
    os.system(" ".join(parts))


def quoted():
    x = request.args["x"]
    os.system("ls " + shlex.quote(x))
'''

API = '''from fastapi import APIRouter, Depends

router = APIRouter()


def get_db():
    return None


@router.get("/search")
def search(term: str, limit: int = 10, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("SELECT * FROM t WHERE name LIKE '%" + term + "%'")
    cur.execute("SELECT * FROM t LIMIT " + str(db))
'''

OWN = '''from runner import go


def get_secret_input():
    return "x"


def clean(v):
    return v


def job():
    raw = get_secret_input()
    go(raw)
    go(clean(raw))
'''

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args],
                   cwd=cwd, check=True, capture_output=True, stdin=subprocess.DEVNULL)


def _project(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root


@pytest.fixture(scope="module")
def web(tmp_path_factory):
    from verinoda import workflow
    from verinoda.store import open_store

    repo = _project(tmp_path_factory.mktemp("taint") / "web", {"app/__init__.py": "", "app/web.py": WEB,
                                                                "app/api.py": API})
    workflow.init(repo)
    st = open_store(repo)
    try:
        workflow.scan(st, repo)
    finally:
        st.close()
    return repo, taint.run(repo, local=True)


def _at(repo_res, rel_line_text: str) -> list[dict]:
    """Findings whose sink line holds the text."""
    repo, res = repo_res
    lines = (repo / "app" / "web.py").read_text(encoding="utf-8").splitlines()
    want = {f"app/web.py:{i}" for i, ln in enumerate(lines, 1) if rel_line_text in ln}
    return [f for f in res["findings"] if f["sink"]["at"] in want]


def _line(repo: Path, rel: str, text: str) -> int:
    return next(i for i, ln in enumerate((repo / rel).read_text(encoding="utf-8").splitlines(), 1) if text in ln)


def test_a_source_reaches_a_sink_through_a_caller_with_every_hop(web):
    repo, _res = web
    (f,) = _at(web, "return db.execute(q)")
    assert f["source"]["match"] == "request.args" and f["sink"]["kind"] == "sql" and f["rule"] == "taint/sql"
    assert f["status"] == "strong_inference" and f["hops"] == 1
    whats = [s["what"] for s in f["path"]]
    ats = [s["at"] for s in f["path"]]
    assert ats == [f"app/web.py:{_line(repo, 'app/web.py', 'who = request.args')}",
                   f"app/web.py:{_line(repo, 'app/web.py', 'return find_user(who)')}",
                   f"app/web.py:{_line(repo, 'app/web.py', 'q = ')}",
                   f"app/web.py:{_line(repo, 'app/web.py', 'return db.execute(q)')}"]
    assert whats[0] == "source request.args -> who"
    assert whats[1].startswith("passes who as name of find_user")
    assert whats[2] == "defines q" and whats[3] == "sink *.execute (sql)"


def test_without_an_index_callers_are_not_followed(web):
    repo, _ = web
    copy = repo.parent / "noindex"
    if not copy.exists():
        shutil.copytree(repo, copy, ignore=shutil.ignore_patterns(".verinoda"))
    res = taint.run(copy, local=True)
    assert not [f for f in res["findings"] if f["sink"]["call"].startswith("db.execute(q)")]
    assert any("no index" in lim for lim in res["limits"])


def test_a_sanitizer_stops_the_path(web):
    assert _at(web, "%d") == []
    assert _at(web, "shlex.quote") == []


def test_route_parameters_are_sources_unless_the_framework_parses_them(web):
    repo, _ = web
    (f,) = _at(web, "return open(path)")
    assert f["source"]["match"] == "route parameter path" and f["source"]["kind"] == "http"
    assert f["path"][0]["at"] == f"app/web.py:{_line(repo, 'app/web.py', 'def files(path)')}"
    assert "'/files/<path>'" in f["path"][0]["what"]
    assert _at(web, "open(str(item_id))") == []   # <int:item_id>


def test_a_value_returned_by_a_project_function_is_followed(web):
    repo, _ = web
    (f,) = _at(web, "subprocess.run(cmd, shell=True)")
    assert f["source"]["match"] == "os.environ" and f["sink"]["kind"] == "command" and f["hops"] == 1
    assert f["path"][0]["at"] == f"app/web.py:{_line(repo, 'app/web.py', 'return os.environ')}"
    assert "returned by get_cmd" in f["path"][0]["what"]
    assert [s["what"] for s in f["path"][1:]] == ["defines cmd", "sink subprocess.run (command)"]


def test_a_shell_free_subprocess_call_is_not_a_sink(web):
    assert _at(web, "subprocess.run([get_cmd()])") == []


def test_a_source_in_the_sink_argument(web):
    (f,) = _at(web, "eval(input())")
    assert f["source"]["match"] == "input()" and len(f["path"]) == 1 and f["sink"]["kind"] == "code"


def test_only_the_part_of_a_tuple_assignment_is_followed(web):
    assert _at(web, "db.execute(b)") == []
    (f,) = _at(web, "db.execute(a)")
    assert f["path"][0]["what"] == "source request.args -> a"


def test_an_update_carries_the_taint(web):
    (f,) = _at(web, 'os.system(" ".join(parts))')
    assert f["source"]["match"] == "request.form"
    assert [s["what"] for s in f["path"]][:1] == ["source request.form -> parts"]


def test_fastapi_query_parameters_and_not_dependencies(web):
    repo, res = web
    fs = [f for f in res["findings"] if f["sink"]["at"].startswith("app/api.py")]
    assert len(fs) == 1 and fs[0]["source"]["match"] == "route parameter term"
    assert fs[0]["sink"]["at"] == f"app/api.py:{_line(repo, 'app/api.py', 'LIKE')}"


def test_the_result_counts_and_names_its_rules(web):
    _repo, res = web
    assert res["status"] == "strong_inference" and res["files"] == 3 and res["sinks"] >= 12
    assert res["spec"]["threats"] == ["remote", "local"]
    assert res["spec"]["from"] == ["verinoda/data/taint_python.json"]
    assert res["by_kind"]["sql"] == 3 and res["by_kind"]["command"] == 2
    assert not res["truncated"] and res["depth"] == 2


def test_the_projects_own_rules(tmp_path):
    repo = _project(tmp_path / "own", {
        "job.py": OWN,
        "verinoda.toml": '[taint]\nsources = ["get_secret_input()"]\n'
                         'sinks = [{match = "go", arg = 0, kind = "command"}]\nsanitizers = ["clean()"]\n'})
    res = taint.run(repo)
    assert [f["sink"]["call"] for f in res["findings"]] == ["go(raw)"]
    assert res["findings"][0]["source"]["origin"] == "verinoda.toml [taint]"
    assert res["spec"]["from"] == ["verinoda/data/taint_python.json", "verinoda.toml [taint]"]
    only = taint.run(repo, builtin=False)
    assert only["spec"]["sinks"] == 1 and only["spec"]["from"] == ["verinoda.toml [taint]"]


@pytest.mark.parametrize("toml,msg", [
    ('[taint]\nsinks = ["go"]\n', "a sink is"),
    ('[taint]\nsanitisers = ["clean()"]\n', "unknown key"),
    ('[taint]\nsinks = [{match = "go", agr = 1}]\n', "unknown key"),
    ('[taint]\nsinks = [{match = "go", when_keyword = 1}]\n', "when_keyword is a string"),
    ('[taint]\nsanitizers = [{match = "x()", kinds = "sql"}]\n', "kinds is a list"),
    ('[taint]\nsources = [{match = "x()", threat = "far"}]\n', "threat is"),
    ('[taint]\nsources = ["not a name"]\n', "not a name pattern"),
    ('[taint]\nsinks = [{match = "go", arg = -1}]\n', "arg is a position"),
    ('[taint]\nbuiltin = "yes"\n', "builtin is true or false"),
    ("[taint\n", "cannot be read"),
])
def test_bad_rules_are_errors(tmp_path, toml, msg):
    repo = _project(tmp_path / "bad", {"a.py": "x = 1\n", "verinoda.toml": toml})
    with pytest.raises(taint.TaintError, match=msg):
        taint.run(repo)


def test_cli_text_json_sarif_and_exit_codes(web, capsys, tmp_path):
    from verinoda import cli

    repo, _ = web
    assert cli.main(["taint", "--repo", str(repo), "--local"]) == 3
    text = capsys.readouterr().out
    assert "path(s) from a source to a sink (strong_inference)" in text
    assert "taint/sql: request.args -> *.execute at app/web.py:" in text
    assert cli.main(["taint", "app/api.py", "--repo", str(repo), "--json"]) == 3
    out = json.loads(capsys.readouterr().out)
    assert out["files"] == 1 and len(out["findings"]) == 1
    assert cli.main(["taint", "--repo", str(repo), "--sarif"]) == 3
    log = json.loads(capsys.readouterr().out)
    results = log["runs"][0]["results"]
    assert results and all(r["level"] == "warning" for r in results)
    flow = next(r for r in results if r["properties"]["source"] == "request.args" and "codeFlows" in r)
    locs = flow["codeFlows"][0]["threadFlows"][0]["locations"]
    assert len(locs) >= 2 and locs[0]["location"]["message"]["text"].startswith("source")
    assert cli.main(["taint", "--repo", str(repo), "--depth", "9"]) == 2
    capsys.readouterr()
    assert cli.main(["taint", str(tmp_path), "--repo", str(repo)]) == 2
    capsys.readouterr()
    clean = _project(tmp_path / "clean", {"a.py": "def f(x):\n    return x + 1\n"})
    assert cli.main(["taint", "--repo", str(clean)]) == 0
    assert "no path found" in capsys.readouterr().out


def test_a_function_too_large_is_a_note_not_a_crash(tmp_path):
    from verinoda import slicing

    body = "".join(f"    v{i} = {i}\n" for i in range(slicing.MAX_NODES + 5))
    repo = _project(tmp_path / "big", {"big.py": "import os\n\n\ndef big():\n    x = input()\n" + body
                                       + "    os.system(x)\n    os.system(input())\n"})
    res = taint.run(repo, local=True)
    # the source in the argument itself is still seen; the one through `x` needs the function's graph
    assert [f["sink"]["call"] for f in res["findings"]] == ["os.system(input())"]
    assert any("not analysed" in n for n in res["notes"])


def test_local_sources_only_when_asked(web):
    repo, local = web
    remote = taint.run(repo)
    assert remote["spec"]["threats"] == ["remote"]
    kinds = {f["source"]["match"] for f in remote["findings"]}
    assert kinds and not kinds & {"os.environ", "input()", "sys.argv"}
    assert {f["source"]["match"] for f in local["findings"]} >= {"os.environ", "input()"}
    assert len(remote["findings"]) == len(local["findings"]) - 2


# -- review round ---------------------------------------------------------------------------------

LIB = """import os


def run(cmd):
    os.system(cmd)


def f(cmd=os.environ["X"]):
    os.system(cmd)


def find(n):
    return n
"""

MAIN = """import os
import pickle
import re
import html
import subprocess

import requests
import yaml
from flask import request

from lib import f, find, run


def handler():
    run("ls")
    run(request.args["c"])


def two():
    return run("a") or run(request.args["t"])


def defaulted():
    f()


def by_keyword():
    requests.get(url=request.args["u"])
    subprocess.run(args=request.args["c"], shell=True)


def escapes():
    x = request.args["x"]
    os.system("echo " + html.escape(x))
    os.system("echo " + re.escape(x))
    return html.escape(x)


def comprehension():
    return [find(n) for n in request.args.getlist("n")]


def comp_sink():
    return [os.system(n) for n in request.args.getlist("n")]


def outer():
    x = request.args["x"]

    def inner():
        os.system(x)
    return inner


def safe_yaml():
    return yaml.load(request.data, Loader=yaml.SafeLoader)


def unsafe_yaml():
    return pickle.loads(request.data)


def both():
    y = request.form["y"]
    os.system(request.args["a"] + y)


TOP = request.cookies["t"]
os.system(TOP)
"""

LIB2 = """import os


def find2(n):
    os.system(n)
"""


@pytest.fixture(scope="module")
def round2(tmp_path_factory):
    from verinoda import workflow
    from verinoda.store import open_store

    main = MAIN.replace("from lib import f, find, run", "from lib import f, find, run\nfrom lib2 import find2")
    main = main.replace("return [find(n) for n in", "return [find2(n) for n in")
    repo = _project(tmp_path_factory.mktemp("taint2") / "r2", {"lib.py": LIB, "lib2.py": LIB2, "main.py": main})
    workflow.init(repo)
    st = open_store(repo)
    try:
        workflow.scan(st, repo)
    finally:
        st.close()
    return repo, taint.run(repo, local=True)


def _sinks_from(res, rel_prefix: str, src: str) -> list[dict]:
    return [f for f in res["findings"] if f["source"]["at"].startswith(rel_prefix) and f["source"]["match"] == src]


def _line_of(repo: Path, rel: str, text: str) -> int:
    return next(i for i, ln in enumerate((repo / rel).read_text(encoding="utf-8").splitlines(), 1) if text in ln)


def test_every_call_in_a_caller_is_followed(round2):
    repo, res = round2
    at = {f["source"]["at"] for f in res["findings"] if f["sink"]["at"] == f"lib.py:{_line_of(repo, 'lib.py', 'os.system(cmd)')}"}
    assert f"main.py:{_line_of(repo, 'main.py', 'run(request.args[\"c\"])')}" in at
    assert f"main.py:{_line_of(repo, 'main.py', 'or run(request.args')}" in at


def test_a_default_is_read_in_the_callees_file(round2):
    repo, res = round2
    sink = f"lib.py:{_line_of(repo, 'lib.py', 'os.system(cmd)') + 4}"
    (fd,) = [f for f in res["findings"] if f["sink"]["at"] == sink]
    first = fd["path"][0]
    assert first["at"] == f"lib.py:{_line_of(repo, 'lib.py', 'def f(cmd=')}" and first["text"].startswith("def f(")
    assert first["what"].startswith("source os.environ -> default of cmd in f")


def test_keyword_arguments_reach_library_sinks(round2):
    repo, res = round2
    kinds = {f["sink"]["kind"] for f in res["findings"]
             if f["sink"]["at"] in (f"main.py:{_line_of(repo, 'main.py', 'requests.get(url=')}",
                                    f"main.py:{_line_of(repo, 'main.py', 'subprocess.run(args=')}")}
    assert kinds == {"request", "command"}


def test_a_sanitizer_clears_only_its_own_kinds(round2):
    repo, res = round2
    html_line = f"main.py:{_line_of(repo, 'main.py', 'html.escape(x))')}"
    re_line = f"main.py:{_line_of(repo, 'main.py', 're.escape(x))')}"
    assert {f["sink"]["at"] for f in res["findings"]} >= {html_line, re_line}   # neither quotes for a shell


def test_comprehension_variables_carry_the_taint(round2):
    repo, res = round2
    assert [f for f in res["findings"] if f["sink"]["at"] == "lib2.py:5"]
    assert [f for f in res["findings"] if f["sink"]["at"] == f"main.py:{_line_of(repo, 'main.py', 'os.system(n) for n')}"]


def test_closures_and_module_level_code(round2):
    repo, res = round2
    inner = [f for f in res["findings"] if f["sink"]["at"] == f"main.py:{_line_of(repo, 'main.py', 'os.system(x)')}"]
    assert inner and inner[0]["source"]["match"] == "request.args"
    top = [f for f in res["findings"] if f["sink"]["at"] == f"main.py:{_line_of(repo, 'main.py', 'os.system(TOP)')}"]
    assert top and top[0]["source"]["match"] == "request.cookies"


def test_safe_keyword_values_and_several_sources_into_one_sink(round2):
    repo, res = round2
    assert not [f for f in res["findings"] if "SafeLoader" in f["sink"]["call"]]
    assert [f for f in res["findings"] if f["sink"]["call"].startswith("pickle.loads")]
    srcs = {f["source"]["match"] for f in res["findings"]
            if f["sink"]["at"] == f"main.py:{_line_of(repo, 'main.py', 'request.args[\"a\"] + y')}"}
    assert srcs == {"request.args", "request.form"}


def test_project_sinks_come_before_the_librarys_and_sanitizers_are_calls(tmp_path):
    repo = _project(tmp_path / "own2", {
        "a.py": "import os\nimport requests\nfrom flask import request\n\n\n"
                "def g():\n    requests.get(timeout=1, endpoint=request.args['u'])\n"
                "    os.system(clean(request.args['x']))\n",
        "verinoda.toml": '[taint]\nsinks = [{match = "requests.get", arg = "endpoint", kind = "request"}]\n'
                         'sanitizers = ["clean"]\n'})
    res = taint.run(repo)
    assert [f["sink"]["call"][:12] for f in res["findings"]] == ["requests.get"]


@pytest.mark.parametrize("ann,src", [
    ("q: str | None = None", True), ("q: Annotated[str | None, Query()] = None", True),
    ("q: Annotated[str, Depends(get_user)]", False), ("q: str = Security(scheme)", False),
    ("q: Optional[str] = None", True), ("q: int = 1", False),
])
def test_fastapi_parameters(ann, src):
    import ast

    func = ast.parse(f"@app.get('/x')\ndef h({ann}):\n    pass\n").body[0]
    assert ("q" in taint.route_params(func)) is src


def test_typed_path_parameters_are_parsed_by_the_framework():
    import ast

    func = ast.parse("@app.get('/c/{item_id}/{name}')\ndef c(item_id: int, name: str):\n    pass\n").body[0]
    assert set(taint.route_params(func)) == {"name"}


def test_cli_paths_that_do_not_exist_and_dot_in_a_subfolder(web, capsys, monkeypatch):
    from verinoda import cli

    repo, _ = web
    assert cli.main(["taint", "nosuch.py", "--repo", str(repo)]) == 2
    assert "does not exist" in capsys.readouterr().err
    monkeypatch.chdir(repo / "app")
    assert cli.main(["taint", ".", "--repo", str(repo), "--json"]) in (0, 3)
    assert json.loads(capsys.readouterr().out)["files"] == 3
