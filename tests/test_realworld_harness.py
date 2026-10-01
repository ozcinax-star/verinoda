"""The real-world benchmark harness (benchmarks/realworld): runner, gold checks, report.

No network and no clone from GitHub: the clone step is replaced by a local folder. Most tests drive the
runner with a fake ``-m`` module that prints, crashes or hangs on demand; one runs the real Verinoda on a
tiny repository whose files would leave a mark if anything executed them.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
RW = ROOT / "benchmarks" / "realworld"


def _load(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, RW / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


run = _load("realworld_run", "run.py")
report = _load("realworld_report", "report.py")

FAKE_MAIN = r'''
import json, os, sys, time
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as f:
    f.write(json.dumps(args) + "\n")
if os.environ.get("FAKE_ENV_LOG"):
    with open(os.environ["FAKE_ENV_LOG"], "a", encoding="utf-8") as f:
        cfg = os.environ.get("VERINODA_CONFIG_DIR")
        f.write(json.dumps({"cfg": cfg, "cfg_files": sorted(os.listdir(cfg)) if cfg else None,
                            "other": sorted(k for k in os.environ if k.startswith("VERINODA_")
                                            and k != "VERINODA_CONFIG_DIR")}) + "\n")
mode = json.loads(os.environ.get("FAKE_MODE", "{}")).get(args[0] if args else "", "ok")
if mode == "crash":
    print("Traceback (most recent call last):\n  File \"x.py\", line 1\nKeyError: 'boom'", file=sys.stderr)
    sys.exit(1)
if mode == "hang":
    time.sleep(120)
if mode == "exit7":
    sys.exit(7)
if mode == "err1":
    print("error: no index here; run verinoda scan first", file=sys.stderr)
    sys.exit(1)
if mode == "norow":
    print(json.dumps({"rows": [], "row_count": 0}))
    sys.exit(1)
if mode == "nokey":
    print(json.dumps({"something": 1}))
    sys.exit(1)
if mode == "list":
    print("[1, 2]")
    sys.exit(0)
if mode == "notjson":
    print("plain text")
    sys.exit(0)
if args[0] == "q":
    print(json.dumps({"rows": [{"values": {"a": {"at": "pkg/core.py:3"}}}]}))
elif args[0] == "query":
    print(json.dumps({"items": [{"file": "README.md"}, {"file": "pkg/core.py"}]}))
elif args[0] == "trace":
    print(json.dumps({"status": "no directed path", "paths": []}))
    sys.exit(2)
else:
    print(json.dumps({"ok": True, "cmd": args[0]}))
'''


@pytest.fixture
def fake(tmp_path, monkeypatch):
    """A fake Verinoda package `fakevn` on its own PYTHONPATH root, logging each argv."""
    root = tmp_path / "fakeroot"
    (root / "fakevn").mkdir(parents=True)
    (root / "fakevn" / "__init__.py").write_text("", encoding="utf-8")
    (root / "fakevn" / "__main__.py").write_text(FAKE_MAIN, encoding="utf-8")
    log = tmp_path / "argv.log"
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("FAKE_MODE", "{}")
    return root, log


def _repo(tmp_path: Path) -> Path:
    clone = tmp_path / "clone"
    (clone / "pkg").mkdir(parents=True)
    (clone / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (clone / "pkg" / "core.py").write_text("import os\n\ndef apply(x):\n    return helper(x)\n\n\n"
                                           "def helper(x):\n    return x + 1\n", encoding="utf-8")
    (clone / "README.md").write_text("# tiny\n", encoding="utf-8")
    return clone


GIT_ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@t")


def _git_commit_all(clone: Path) -> None:
    for args in (["init", "-q"], ["config", "core.autocrlf", "false"], ["add", "-A"], ["commit", "-qm", "x"]):
        subprocess.run(["git", "-C", str(clone), *args], check=True, env=GIT_ENV, capture_output=True)


def _runner(clone, fakeroot, **kw):
    return run.Runner(clone, python=sys.executable, verinoda_root=fakeroot, module="fakevn", **kw)


# -- the never-run-the-repository's-code rule ------------------------------------------------------------

@pytest.mark.parametrize("argv", [
    ["trust", "."], ["observe"], ["experiment", "x"], ["probe", "f.py"], ["mutate"],
    ["analyze", "q", "--run-tests"], ["review", "--observe"], ["check", "a.py", "--checker", "tsc"],
    ["check", "--env=/x/python"], ["check", "--deps", "--registry"], ["setup", "."], ["hooks", "install"],
    # abbreviations argparse would expand (review finding 1)
    ["analyze", "q", "--run-test"], ["analyze", "q", "--obs"], ["analyze", "q", "--run-test", "--obs"],
    ["review", ".", "--run"], ["check", "a.py", "--che", "tsc"], ["query", "x", "--max-item", "3"],
    ["query", "x", "--js"],
    # commands that run code and were missing from the old deny list
    ["debug", "try", "--", "go", "test", "./..."], ["run"], ["spec"], ["mutations"],
    ["bench", "run"], ["index"], ["", "x"], [],
    # allowed commands with options not on their allow list
    ["map", ".", "--mode", "x"], ["scan", ".", "--force"], ["analyze", "q", "--refresh"], ["q", "x", "--verify"],
    ["doctor", "-h"], ["trace", "a", "b", "--repo", "/elsewhere"], ["check", "--stdin"], ["init", "--"],
])
def test_guard_refuses_what_is_not_on_the_allow_list(argv):
    with pytest.raises(run.Forbidden):
        run.guard_argv(argv)


@pytest.mark.parametrize("argv", [
    ["init", "."], ["scan", ".", "--json"], ["query", "where", "--json", "--max-items", "8"],
    ["trace", "gin.go::Default", "Recovery", "--json", "--mode", "flow"], ["map", ".", "--view=dead", "--json"],
    ["q", "match (f:function) return count(f)", "--json"], ["review", ".", "--json"]])
def test_guard_lets_the_scenarios_through(argv):
    run.guard_argv(argv)


def test_guard_refuses_before_any_process_starts(tmp_path, monkeypatch, fake):
    def boom(*a, **k):
        raise AssertionError("a process was started")
    monkeypatch.setattr(run.subprocess, "Popen", boom)
    r = _runner(_repo(tmp_path), fake[0])
    for bad in (["analyze", "how", "--run-tests"], ["analyze", "how", "--obs"], ["debug", "try", "--", "x"]):
        with pytest.raises(run.Forbidden):
            r.run("analyze", bad)
    assert r.steps == []


def test_full_scenario_never_asks_for_repo_code(tmp_path, fake):
    root, log = fake
    clone = _repo(tmp_path)
    repo = {"name": "o/tiny", "tag": "v1", "sha": "0" * 40, "language": "Python"}
    run.run_repo(repo, clone, python=sys.executable, verinoda_root=root, module="fakevn",
                 gold={"facts": []})
    argvs = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
    assert argvs, "nothing ran"
    cmds = {a[0] for a in argvs}
    assert {"init", "scan", "query", "analyze", "q", "check", "map", "routes", "schema", "taint", "doctor",
            "update", "review"} <= cmds <= set(run.ALLOWED)
    for a in argvs:
        run.guard_argv(a)                       # none of them is refused


def test_real_verinoda_executes_nothing_of_the_repository(tmp_path):
    """The real CLI on a repository whose sitecustomize, conftest, setup.py and a folder named like
    Verinoda's own package would each leave a marker file if executed; every step group runs, the MCP
    probe, doctor, map, update and review included."""
    clone = _repo(tmp_path)
    marker = tmp_path / "EXECUTED"
    body = f"open({str(marker)!r}, 'a').write(__file__ + '\\n')\n"
    for f in ("sitecustomize.py", "usercustomize.py", "conftest.py", "setup.py", "pkg/__main__.py"):
        (clone / f).write_text(body, encoding="utf-8")
    (clone / "verinoda").mkdir()
    (clone / "verinoda" / "__init__.py").write_text(body, encoding="utf-8")
    (clone / "pkg" / "core.py").write_text("import os\nimport pkg.__main__\n\ndef apply(x):\n"
                                           "    return helper(x)\n\n\ndef helper(x):\n    return x + 1\n",
                                           encoding="utf-8")
    repo = {"name": "o/tiny", "tag": "v1", "sha": "0" * 40, "language": "Python",
            "scenario": {"check_file": "pkg/core.py", "queries": ["where is apply"], "analyze": ["what does apply do"],
                         "q": ["match (f:function) return count(f)"], "map_views": ["dependencies", "dead"],
                         "trace": ["apply", "helper"]}}
    gold = {"facts": [{"id": "calls", "check": {"kind": "q", "query": "match (a:function)-[calls]->(b:function) "
                                                "where a.name = \"apply\" return a, b",
                                                "cites": ["pkg/core.py:5"]}}]}
    _git_commit_all(clone)                         # review reads HEAD, like on a real clone
    res = run.run_repo(repo, clone, python=sys.executable, verinoda_root=ROOT, gold=gold,
                       steps={"init", "scan", "gold", "check", "taint", "trace", "map", "doctor", "mcp", "edit"})
    assert res["clean_after"] is True
    assert not marker.exists(), marker.read_text(encoding="utf-8")
    assert res["crashes"] == [] and res["timeouts"] == [], [(s["step"], s["reason"], s["stderr_tail"])
                                                             for s in res["steps"] if s["crash"]]
    assert res["gold"]["hits"] == 1, res["gold"]
    steps = {s["step"] for s in res["steps"]}
    assert {"doctor", "mcp:project_query", "mcp:analyze", "map:dead", "update", "review", "update:revert",
            "trace"} <= steps


# -- crash and timeout detection ----------------------------------------------------------------------

def test_traceback_and_undocumented_exit_are_crashes(tmp_path, fake, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", json.dumps({"scan": "crash", "doctor": "exit7", "trace": "ok"}))
    r = _runner(_repo(tmp_path), fake[0])
    scan, _ = r.run("scan", ["scan", ".", "--json"])
    doc, _ = r.run("doctor", ["doctor", "--json"])
    tr, _ = r.run("trace", ["trace", "a", "b", "--json"])
    assert scan["crash"] and scan["reason"] == "traceback in stderr" and "KeyError" in scan["stderr_tail"]
    assert doc["crash"] and doc["reason"].startswith("exit 7")
    assert not tr["crash"] and tr["exit"] == 2           # documented: no directed path


@pytest.mark.parametrize("cmd,args", [("q", ["q", "match (f) return f", "--json"]), ("doctor", ["doctor", "--json"])])
def test_exit_1_without_json_is_a_crash_for_q_and_doctor(tmp_path, fake, monkeypatch, cmd, args):
    """cli.main turns KeyError/ValueError/FileNotFoundError and 'no index' into exit 1 + 'error:' (finding 2)."""
    monkeypatch.setenv("FAKE_MODE", json.dumps({cmd: "err1"}))
    rec, _ = _runner(_repo(tmp_path), fake[0]).run(cmd, args)
    assert rec["exit"] == 1 and rec["crash"]
    assert rec["reason"].startswith("exit 1 without a JSON answer") and "error: no index" in rec["reason"]


def test_q_exit_1_with_rows_is_an_answer_but_without_rows_key_a_crash(tmp_path, fake, monkeypatch):
    r = _runner(_repo(tmp_path), fake[0])
    monkeypatch.setenv("FAKE_MODE", json.dumps({"q": "norow"}))
    rec, _ = r.run("q1", ["q", "x", "--json"])
    assert rec["exit"] == 1 and not rec["crash"]
    monkeypatch.setenv("FAKE_MODE", json.dumps({"q": "nokey", "doctor": "nokey"}))
    rec, _ = r.run("q2", ["q", "x", "--json"])
    assert rec["crash"]
    doc, _ = r.run("doctor", ["doctor", "--json"])
    assert not doc["crash"]                              # doctor: any JSON object


def test_gold_steps_use_each_kinds_own_exit_codes(tmp_path, fake, monkeypatch):
    """A gold check that hits an internal error is a crash, not just a miss (finding 2)."""
    monkeypatch.setenv("FAKE_MODE", json.dumps({"query": "err1", "q": "err1", "trace": "ok"}))
    r = _runner(_repo(tmp_path), fake[0])
    gold = {"facts": [{"id": "a", "check": {"kind": "query", "question": "x", "expect_file": "pkg/core.py"}},
                      {"id": "b", "check": {"kind": "q", "query": "match (f) return f", "cites": ["pkg/core.py:3"]}},
                      {"id": "c", "check": {"kind": "trace", "source": "a", "target": "b"}}]}
    out = run.check_gold(r, gold)
    by = {s["step"]: s for s in r.steps}
    assert by["gold:a"]["crash"] and by["gold:a"]["ok_codes"] == [0]
    assert by["gold:b"]["crash"] and by["gold:b"]["ok_codes"] == [0, 1, 3]
    assert not by["gold:c"]["crash"] and by["gold:c"]["ok_codes"] == [0, 2]
    assert [g["hit"] for g in out] == [False, False, False]
    assert out[0]["why"] == "exit 1 not in [0]"
    assert out[1]["why"].startswith("exit 1 without a JSON answer: error: no index")


def test_traceback_with_exit_zero_is_still_a_crash(tmp_path, fake, monkeypatch):
    fakeroot = fake[0]
    (fakeroot / "fakevn" / "__main__.py").write_text(
        "import sys\nprint('Traceback (most recent call last):', file=sys.stderr)\n", encoding="utf-8")
    rec, _ = _runner(_repo(tmp_path), fakeroot).run("query", ["query", "x", "--json"])
    assert rec["exit"] == 0 and rec["crash"]


def test_timeout_kills_the_process_and_is_recorded(tmp_path, fake, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", json.dumps({"analyze": "hang"}))
    r = _runner(_repo(tmp_path), fake[0], timeouts={"analyze": 2})
    t0 = time.perf_counter()
    rec, _ = r.run("analyze1", ["analyze", "q", "--json"])
    assert time.perf_counter() - t0 < 60
    assert rec["timeout"] and not rec["crash"] and rec["exit"] is None
    assert rec["reason"].startswith("timeout after 2")


def test_json_and_sizes_are_recorded_and_paths_sanitized(tmp_path, fake, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", json.dumps({"routes": "notjson"}))
    clone = _repo(tmp_path)
    r = _runner(clone, fake[0])
    rec, _ = r.run("routes", ["routes", "--json"])
    assert rec["json_ok"] is False and rec["stdout_bytes"] > 0
    rec2, _ = r.run("check", ["check", str(clone / "pkg" / "core.py"), "--json"])
    assert rec2["json_ok"] is True
    assert str(clone) not in json.dumps(rec2) and "<CORPUS>" in rec2["argv"][1]


def test_summary_counts_crashes_timeouts_and_gold(tmp_path, fake, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", json.dumps({"schema": "crash"}))
    root, _ = fake
    clone = _repo(tmp_path)
    (clone / ".verinoda").mkdir()
    (clone / ".verinoda" / "old.db").write_text("x", encoding="utf-8")
    gold = {"facts": [
        {"id": "a", "check": {"kind": "q", "query": "match (f) return f", "cites": ["pkg/core.py:3"]}},
        {"id": "b", "check": {"kind": "q", "query": "match (f) return f", "cites": ["pkg/core.py:30"]}},
        {"id": "c", "check": {"kind": "query", "question": "x", "top_k": 2, "expect_file": "pkg/core.py"}},
        {"id": "d", "check": {"kind": "trace", "source": "a", "target": "b"}},
        {"id": "a@v2", "v": 2, "check": {"kind": "q", "query": "match (f) return f", "cites": ["pkg/core.py:3"]}}]}
    before = (clone / "pkg" / "core.py").read_bytes()
    res = run.run_repo({"name": "o/tiny", "tag": "v1", "sha": "0" * 40, "language": "Go"}, clone,
                       python=sys.executable, verinoda_root=root, module="fakevn", gold=gold)
    assert res["crashes"] == ["schema"]
    assert res["timeouts"] == []
    assert [f["hit"] for f in res["gold"]["facts"]] == [True, False, True, False, True]
    assert res["gold"]["hits"] == 2 and res["gold"]["total"] == 4             # v1 only
    assert res["gold"]["v2"] == {"hits": 1, "total": 1}
    assert not any(s["step"] == "taint" for s in res["steps"])          # not a Python repository
    assert not (clone / ".verinoda" / "old.db").exists()                # init starts cold
    assert (clone / "pkg" / "core.py").read_bytes() == before           # the edit was reverted
    assert res["edit"]["file"] == "pkg/core.py"
    assert {"update", "review", "update:revert"} <= {s["step"] for s in res["steps"]}
    assert res["clean_after"] is None                                   # not a git checkout


def test_doctor_hang_is_a_timeout(tmp_path, fake, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", json.dumps({"doctor": "hang"}))
    r = _runner(_repo(tmp_path), fake[0], timeouts={"doctor": 2})
    rec, _ = r.run("doctor", ["doctor", "--json"])
    assert rec["timeout"]


# -- the MCP probe (finding 3) ----------------------------------------------------------------------------

FAKE_SERVER = '''
class AtlasTools:
    def __init__(self, repo):
        self.repo = repo
    def project_query(self, q):
        return {"error": "no_index", "hint": "run verinoda scan"} if q == "bad" else {"items": [], "q": q}
    def analyze(self, q, run_tests=True, observe=True):
        assert run_tests is False and observe is False
        return {"claims": []}
'''


def test_mcp_probe_exits_1_on_a_tool_error(tmp_path, fake):
    fakeroot = fake[0]
    (fakeroot / "verinoda" / "mcp").mkdir(parents=True)
    (fakeroot / "verinoda" / "__init__.py").write_text("", encoding="utf-8")
    (fakeroot / "verinoda" / "mcp" / "__init__.py").write_text("", encoding="utf-8")
    (fakeroot / "verinoda" / "mcp" / "server.py").write_text(FAKE_SERVER, encoding="utf-8")
    clone = _repo(tmp_path)
    r = _runner(clone, fakeroot)
    recs = {}
    for tool, q in (("project_query", "bad"), ("project_query", "fine"), ("analyze", "x")):
        rec, out = r.run(f"mcp:{tool}:{q}", [], command="mcp",
                         raw_argv=[sys.executable, "-P", str(run.MCP_PROBE), str(clone), tool, q])
        recs[(tool, q)] = rec
    bad = recs[("project_query", "bad")]
    assert bad["exit"] == 1 and bad["crash"] and "tool error: no_index" in bad["stderr_tail"]
    assert not recs[("project_query", "fine")]["crash"] and not recs[("analyze", "x")]["crash"]


# -- gold judging ----------------------------------------------------------------------------------------

def test_cites_is_exact_on_the_line_number():
    rows = [{"values": {"a": {"at": "gin.go:2360"}}, "evidence": {"paths": [[{"at": "src\\core.go:69"}]]}}]
    assert not run.cites(rows, "gin.go:236")
    assert run.cites(rows, "gin.go:2360")
    assert run.cites(rows, "src/core.go:69")


def test_cites_is_exact_on_the_path_and_reads_only_at_fields():
    """binding/gin.go:236 does not cite gin.go:236, nor vendor/src/core.go:55 src/core.go:55 (finding 4)."""
    assert not run.cites([{"at": "binding/gin.go:236"}], "gin.go:236")
    assert not run.cites([{"at": "vendor/src/core.go:55"}], "src/core.go:55")
    assert not run.cites([{"at": "vendor\\src\\core.go:55"}], "src/core.go:55")
    assert not run.cites([{"label": "gin.go:236", "note": "see gin.go:236"}], "gin.go:236")
    assert not run.cites([{"at": "gin.go:236.5"}], "gin.go:236")
    assert run.cites([{"at": "gin.go:236"}], "gin.go:236")
    assert run.cites([{"x": [{"at": "  gin.go:236"}]}], "gin.go:236")


def test_judge_kinds():
    q = {"kind": "q", "cites": ["a.go:3"]}
    assert run.judge(q, 0, json.dumps({"rows": [{"at": "a.go:3"}]}))[0]
    assert run.judge(q, 1, json.dumps({"rows": []})) == (False, "no row")
    assert run.judge(q, 0, "not json")[0] is False
    tr = {"kind": "trace", "via_files": ["gin.go"], "via_at": ["gin.go:715"]}
    path = {"status": "found", "paths": [[{"at": "gin.go:672"}, {"at": "gin.go:715"}]],
            "resolved": {"source": {"at": "gin.go:662"}, "target": {"at": "tree.go:418"}}}
    assert run.judge(tr, 0, json.dumps(path))[0]
    assert not run.judge({"kind": "trace", "via_files": ["x.go"]}, 0, json.dumps(path))[0]
    assert run.judge(tr, 2, json.dumps({"status": "no directed path", "paths": [],
                                        "resolved": path["resolved"]}))[1].startswith("no path")
    qu = {"kind": "query", "top_k": 2, "expect_file": "src/options.go"}
    assert run.judge(qu, 0, json.dumps({"items": [{"file": "a"}, {"file": "src\\options.go"}]})) == (True, "rank 2")
    assert not run.judge(qu, 0, json.dumps({"items": [{"file": "a"}, {"file": "b"}, {"file": "src/options.go"}]}))[0]
    ro = {"kind": "routes", "method": "GET", "path": "/items", "handler": "read_items"}
    table = {"route_table": [{"method": "get", "path": "/items", "handler": "app/api.py:3 read_items"}]}
    assert run.judge(ro, 0, json.dumps(table))[0]
    assert run.judge({"kind": "schema", "table": "user"}, 0, json.dumps({"tables": [{"name": "user"}]}))[0]


def test_trace_via_files_must_be_on_a_path_edge_not_a_resolved_end():
    """fzf run-calls-newmatcher hit only through resolved.target (finding 4): now a miss."""
    ans = {"status": "found", "paths": [[{"from": "Run()", "to": "NewMatcher()", "at": "src/core.go:258"}]],
           "resolved": {"source": {"at": "src/core.go:55"}, "target": {"at": "src/matcher.go:59"}}}
    hit, why = run.judge({"kind": "trace", "via_files": ["src/matcher.go"]}, 0, json.dumps(ans))
    assert not hit and "src/matcher.go" in why
    v2 = {"kind": "trace", "source_at": "src/core.go:55", "target_at": "src/matcher.go:59",
          "via_at": ["src/core.go:258"]}
    assert run.judge(v2, 0, json.dumps(ans)) == (True, "")
    assert not run.judge(dict(v2, via_at=["src/core.go:25"]), 0, json.dumps(ans))[0]


def test_trace_checks_both_resolved_ends():
    paths = [[{"at": "gin.go:239"}]]
    no_source = {"status": "found", "paths": paths, "resolved": {"source": None, "target": {"at": "recovery.go:35"}}}
    assert run.judge({"kind": "trace", "via_files": ["gin.go"]}, 0, json.dumps(no_source)) == \
        (False, "source not resolved (found)")
    wrong = {"status": "found", "paths": paths,
             "resolved": {"source": {"at": "binding/binding.go:95"}, "target": {"at": "recovery.go:35"}}}
    hit, why = run.judge({"kind": "trace", "source_at": "gin.go:236", "via_at": ["gin.go:239"]}, 0, json.dumps(wrong))
    assert not hit and why.startswith("source resolved at binding/binding.go:95")


def test_via_edges_must_be_on_one_path():
    ans = {"status": "found", "paths": [[{"at": "a.go:1"}], [{"at": "b.go:2"}]],
           "resolved": {"source": {"at": "a.go:0"}, "target": {"at": "c.go:9"}}}
    assert not run.judge({"kind": "trace", "via_at": ["a.go:1", "b.go:2"]}, 0, json.dumps(ans))[0]
    assert run.judge({"kind": "trace", "via_at": ["b.go:2"]}, 0, json.dumps(ans))[0]


def test_judge_survives_json_that_is_not_an_object():
    """`judge` on a JSON list used to raise AttributeError and end the whole run (finding 5)."""
    for kind in ("q", "trace", "query", "routes", "schema"):
        hit, why = run.judge({"kind": kind, "expect_file": "x", "table": "t", "path": "/"}, 0, "[1, 2]")
        assert not hit and "not an object" in why
    assert not run.judge({"kind": "query", "expect_file": "x"}, 0, json.dumps({"items": ["a", None]}))[0]


def test_gold_files_are_well_formed():
    manifest = run.load_manifest()
    names = {r["name"] for r in manifest["repos"]}
    for p in (RW / "gold").glob("*.json"):
        g = json.loads(p.read_text(encoding="utf-8"))
        assert g["repo"] in names and p.stem == run.slug(g["repo"])
        assert g["sha"] == next(r["sha"] for r in manifest["repos"] if r["name"] == g["repo"])
        v1 = [f for f in g["facts"] if f.get("v", 1) == 1]
        assert 5 <= len(v1) <= 10
        ids = [f["id"] for f in g["facts"]]
        assert len(ids) == len(set(ids))
        for f in g["facts"]:
            assert f["fact"] and f["source"]
            run.gold_argv(f["check"])          # a known kind
            run.guard_argv(run.gold_argv(f["check"]))
            if f.get("v", 1) != 1:
                assert f["replaces"] in {x["id"] for x in v1} and f["why"] and f["added"]
                if f["check"]["kind"] == "trace":   # v2 trace checks: a path edge and both ends located
                    assert f["check"]["via_at"] and f["check"]["source_at"] and f["check"]["target_at"]


def test_v2_gold_matchitem_cites_the_call_line_not_the_definition():
    g = run.load_gold("junegunn/fzf")
    v2 = next(f for f in g["facts"] if f["id"] == "pattern-matchitem@v2")
    assert v2["check"]["cites"] == ["src/pattern.go:395"]
    v1 = next(f for f in g["facts"] if f["id"] == "pattern-matchitem")
    assert v1["check"]["cites"] == ["src/pattern.go:388"]                  # the frozen v1 is untouched


def test_manifest_pins_every_repo():
    for r in run.load_manifest()["repos"]:
        assert len(r["sha"]) == 40 and r["tag"] and r["license"] and r["stars"] > 10000
        assert r["url"] == f"https://github.com/{r['name']}"


def test_scenarios_measure_something():
    """A check file `check` reads, and trace pairs that name one symbol each (not 'Default')."""
    for r in run.load_manifest()["repos"]:
        sc = r.get("scenario") or {}
        if sc.get("check_file"):
            assert Path(sc["check_file"]).suffix in run.CHECK_EXT, r["name"]
        if r["name"] in ("gin-gonic/gin", "junegunn/fzf"):
            assert "::" in sc["trace"][0]
            for argv in (["trace", *sc["trace"], "--json"],):
                run.guard_argv(argv)


def test_check_is_skipped_on_a_repository_without_a_checkable_file(tmp_path, fake):
    root, log = fake
    clone = tmp_path / "goclone"
    clone.mkdir()
    (clone / "main.go").write_text("package main\n\nfunc main() {}\n", encoding="utf-8")
    res = run.run_repo({"name": "o/go", "tag": "v1", "sha": "0" * 40, "language": "Go",
                        "scenario": {"check_file": "main.go"}}, clone, python=sys.executable, verinoda_root=root,
                       module="fakevn", steps={"check"})
    assert not any(s["step"] == "check" for s in res["steps"])
    assert res["skipped"] == ["check: no Python, Java or Kotlin file (check reads only those)"]
    assert run.pick_check_file(_repo(tmp_path), ["pkg/core.py", "README.md"], {}, {}) == "pkg/core.py"


# -- the clone step (mocked) ------------------------------------------------------------------------------

def test_ensure_clone_clones_once_then_reuses(tmp_path):
    calls = []

    def clone_fn(repo, dest):
        calls.append(dest)
        dest.mkdir(parents=True)

    repo = {"name": "o/r", "sha": "a" * 40, "tag": "v1"}
    p = run.ensure_clone(repo, tmp_path, clone_fn=clone_fn, sha_fn=lambda d: "a" * 40)
    assert p == tmp_path / "o__r" and len(calls) == 1 and calls[0] == tmp_path / "o__r.partial"
    run.ensure_clone(repo, tmp_path, clone_fn=clone_fn, sha_fn=lambda d: "a" * 40)
    assert len(calls) == 1


def test_ensure_clone_refuses_another_sha_and_keeps_the_folder(tmp_path):
    (tmp_path / "o__r").mkdir()
    (tmp_path / "o__r" / "work.txt").write_text("mine", encoding="utf-8")
    with pytest.raises(RuntimeError, match="not the pinned"):
        run.ensure_clone({"name": "o/r", "sha": "a" * 40, "tag": "v1"}, tmp_path,
                         clone_fn=lambda r, d: None, sha_fn=lambda d: "b" * 40)
    assert (tmp_path / "o__r" / "work.txt").exists()


def test_a_half_finished_clone_is_cleaned_up_and_cloned_again(tmp_path):
    repo = {"name": "o/r", "sha": "a" * 40, "tag": "v1"}

    def dies(r, dest):
        dest.mkdir(parents=True)
        (dest / "half").write_text("x", encoding="utf-8")
        raise subprocess.TimeoutExpired("git", 1)

    with pytest.raises(subprocess.TimeoutExpired):
        run.ensure_clone(repo, tmp_path, clone_fn=dies, sha_fn=lambda d: "a" * 40)
    assert not (tmp_path / "o__r").exists() and not (tmp_path / "o__r.partial").exists()
    # a .partial left by a killed run (no except ran) is ours: removed, then cloned again
    (tmp_path / "o__r.partial" / ".git").mkdir(parents=True)
    ro = tmp_path / "o__r.partial" / ".git" / "pack"
    ro.write_text("x", encoding="utf-8")
    os.chmod(ro, 0o444)

    def ok(r, dest):
        assert not dest.exists()
        dest.mkdir()
        (dest / "done").write_text("x", encoding="utf-8")

    p = run.ensure_clone(repo, tmp_path, clone_fn=ok, sha_fn=lambda d: "a" * 40)
    assert (p / "done").exists() and not (tmp_path / "o__r.partial").exists()


def test_git_clone_without_symlinks_lfs_or_short_paths(tmp_path, monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"], seen["env"] = argv, kw["env"]
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(run.subprocess, "run", fake_run)
    run.git_clone({"name": "o/r", "url": "https://example.invalid/o/r", "tag": "v1"}, tmp_path / "d")
    a = seen["argv"]
    assert "core.symlinks=false" in a and "core.longpaths=true" in a and "--no-recurse-submodules" in a
    assert seen["env"]["GIT_LFS_SKIP_SMUDGE"] == "1" and seen["env"]["GIT_TERMINAL_PROMPT"] == "0"


def test_head_sha_of_a_local_repository(tmp_path):
    clone = _repo(tmp_path)
    _git_commit_all(clone)
    sha = run.head_sha(clone)
    assert sha and len(sha) == 40
    assert "pkg/core.py" in run.tracked_files(clone)


def test_a_dirty_clone_is_refused_and_cleanliness_is_checked_after_the_revert(tmp_path, fake, monkeypatch):
    """Finding 6: a run killed mid-edit leaves a change; the next run must not read modified sources."""
    root, _ = fake
    clone = _repo(tmp_path)
    _git_commit_all(clone)
    run.assert_clean(clone)
    (clone / "pkg" / "core.py").write_text("changed\n", encoding="utf-8")
    with pytest.raises(run.DirtyClone, match="pkg/core.py"):
        run.assert_clean(clone)
    subprocess.run(["git", "-C", str(clone), "checkout", "--", "."], check=True, capture_output=True)
    repo = {"name": "o/tiny", "tag": "v1", "sha": "0" * 40, "language": "Python"}
    res = run.run_repo(repo, clone, python=sys.executable, verinoda_root=root, module="fakevn", steps={"edit"})
    assert res["clean_after"] is True and res["dirty_after"] == []
    monkeypatch.setattr(run.Edit, "revert", lambda self: None)            # a revert that did not happen
    res = run.run_repo(repo, clone, python=sys.executable, verinoda_root=root, module="fakevn", steps={"edit"})
    assert res["clean_after"] is False and any("pkg/core.py" in d for d in res["dirty_after"])
    assert run._failed(res)


def test_a_tracked_verinoda_folder_is_not_deleted(tmp_path, fake):
    root, _ = fake
    clone = _repo(tmp_path)
    (clone / ".verinoda").mkdir()
    (clone / ".verinoda" / "config.json").write_text("{}", encoding="utf-8")
    _git_commit_all(clone)
    res = run.run_repo({"name": "o/tiny", "tag": "v1", "sha": "0" * 40, "language": "Python"}, clone,
                       python=sys.executable, verinoda_root=root, module="fakevn", steps={"init"})
    assert (clone / ".verinoda" / "config.json").exists()
    assert res["notes"] and "tracks a .verinoda/" in res["notes"][0]
    assert res["clean_after"] is True


# -- the edit ------------------------------------------------------------------------------------------

def test_edit_inserts_one_line_and_restores_exact_bytes(tmp_path):
    f = tmp_path / "a.go"
    original = b"package a\r\n\r\nfunc A() {\r\n\treturn\r\n}\r\n"
    f.write_bytes(original)
    ed = run.Edit(tmp_path, {"file": "a.go", "after_line": 3, "insert": "\t_ = 1 // edit"})
    ed.apply()
    assert f.read_bytes().split(b"\r\n")[3] == b"\t_ = 1 // edit"
    ed.revert()
    assert f.read_bytes() == original


def test_edit_works_on_bytes_not_utf8_text(tmp_path):
    """A Latin-1 / cp1252 source used to raise UnicodeDecodeError and end the run (finding 5)."""
    f = tmp_path / "A.php"
    original = b"<?php\n// caf\xe9 \x93quoted\x94\nfunction a() {}\n"
    f.write_bytes(original)
    ed = run.Edit(tmp_path, {"file": "A.php", "after_line": None, "insert": "// verinoda-bench edit"})
    ed.apply()
    assert f.read_bytes() == original + b"// verinoda-bench edit\n"
    ed.revert()
    assert f.read_bytes() == original


def test_default_edit_appends_a_comment_to_the_largest_source_file(tmp_path):
    clone = _repo(tmp_path)
    spec = run.pick_edit(clone, ["pkg/__init__.py", "pkg/core.py", "README.md"], {})
    assert spec == {"file": "pkg/core.py", "after_line": None, "insert": "# verinoda-bench edit"}
    ed = run.Edit(clone, spec)
    ed.apply()
    assert (clone / "pkg" / "core.py").read_text(encoding="utf-8").endswith("# verinoda-bench edit\n")
    ed.revert()


def test_edit_never_goes_through_a_symlink_or_outside_the_clone(tmp_path):
    """Finding 7: a tracked symlink to a file outside the clone must never be picked or written."""
    clone = _repo(tmp_path)
    outside = tmp_path / "outside.py"
    outside.write_text("x = 1\n" * 500, encoding="utf-8")       # larger than every file of the clone
    assert not run.inside(clone, "../outside.py")
    spec = run.pick_edit(clone, ["pkg/core.py", "../outside.py"], {})
    assert spec["file"] == "pkg/core.py"
    with pytest.raises(ValueError, match="edit refused"):
        run.Edit(clone, {"file": "../outside.py", "after_line": None, "insert": "# x"}).apply()
    try:
        os.symlink(outside, clone / "big.py")
    except (OSError, NotImplementedError):
        pytest.skip("no symlink privilege here; the path case above covers the resolve() check")
    assert not run.inside(clone, "big.py")
    assert run.pick_edit(clone, ["pkg/core.py", "big.py"], {})["file"] == "pkg/core.py"
    with pytest.raises(ValueError, match="edit refused"):
        run.Edit(clone, {"file": "big.py", "after_line": None, "insert": "# x"}).apply()
    assert outside.read_text(encoding="utf-8") == "x = 1\n" * 500


# -- isolation from the user's settings -----------------------------------------------------------------

def test_steps_get_an_empty_config_folder_not_the_users(tmp_path, fake, monkeypatch):
    """The user's trust.json could trust the clone (doctor would then run its .venv Python)."""
    root, _ = fake
    user_cfg = tmp_path / "usercfg"
    user_cfg.mkdir()
    (user_cfg / "trust.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("VERINODA_CONFIG_DIR", str(user_cfg))
    monkeypatch.setenv("VERINODA_TRACE_FILE", "x")
    envlog = tmp_path / "env.log"
    monkeypatch.setenv("FAKE_ENV_LOG", str(envlog))
    run.run_repo({"name": "o/tiny", "tag": "v1", "sha": "0" * 40, "language": "Python"}, _repo(tmp_path),
                 python=sys.executable, verinoda_root=root, module="fakevn", steps={"init", "scan", "doctor"})
    seen = [json.loads(x) for x in envlog.read_text(encoding="utf-8").splitlines()]
    assert len(seen) == 3
    for s in seen:
        assert s["cfg"] and Path(s["cfg"]) != user_cfg and s["cfg_files"] == [] and s["other"] == []


def test_a_trusted_clone_is_not_trusted_under_the_runner(tmp_path, monkeypatch):
    clone = _repo(tmp_path)
    user_cfg = tmp_path / "usercfg"
    user_cfg.mkdir()
    (user_cfg / "trust.json").write_text(json.dumps({"trusted": [{"path": str(tmp_path), "subfolders": True}]}),
                                         encoding="utf-8")
    monkeypatch.setenv("VERINODA_CONFIG_DIR", str(user_cfg))
    code = f"from verinoda.paths import is_trusted; print(is_trusted({str(clone)!r}))"
    plain = subprocess.run([sys.executable, "-P", "-c", code], capture_output=True, text=True,
                           env=dict(os.environ, PYTHONPATH=str(ROOT)), cwd=clone)
    assert plain.stdout.strip() == "True"                 # the user's config trusts it
    cfg = tmp_path / "isolated"
    cfg.mkdir()
    r = run.Runner(clone, verinoda_root=ROOT, config_dir=cfg)
    iso = subprocess.run([sys.executable, "-P", "-c", code], capture_output=True, text=True, env=r.env(), cwd=clone)
    assert iso.stdout.strip() == "False", iso.stderr


# -- the run: per-repository errors, environment, Python version ---------------------------------------

def _two_repo_manifest(tmp_path: Path) -> Path:
    m = {"repos": [{"name": "o/a", "tag": "v1", "sha": "a" * 40, "url": "u", "language": "PHP"},
                   {"name": "o/b", "tag": "v2", "sha": "b" * 40, "url": "u", "language": "Go"}]}
    p = tmp_path / "manifest.json"
    p.write_text(json.dumps(m), encoding="utf-8")
    return p


def test_one_failing_repository_is_recorded_and_results_are_written_after_each(tmp_path, monkeypatch):
    """Finding 5: an unexpected exception in repository 1 used to lose the whole run."""
    out = tmp_path / "out"
    monkeypatch.setattr(run, "python_version", lambda p: (3, 13))
    monkeypatch.setattr(run, "environment", lambda p, r: {"verinoda_commit": "abc1234", "python": "3.13"})
    monkeypatch.setattr(run, "ensure_clone", lambda repo, work: tmp_path / run.slug(repo["name"]))
    monkeypatch.setattr(run, "assert_clean", lambda clone: None)
    seen_before_b = {}

    def fake_run_repo(repo, clone, **kw):
        if repo["name"] == "o/a":
            raise UnicodeDecodeError("utf-8", b"\xe9", 0, 1, "invalid continuation byte")
        seen_before_b.update(json.loads((out / "results.json").read_text(encoding="utf-8")))
        return run.summarize(repo, [], [], [], None, "t") | {"environment": kw["env"]}

    monkeypatch.setattr(run, "run_repo", fake_run_repo)
    code = run.main(["--repos", "o/a,o/b", "--manifest", str(_two_repo_manifest(tmp_path)), "--out", str(out),
                     "--work", str(tmp_path)])
    assert code == 1
    assert [r["name"] for r in seen_before_b["repos"]] == ["o/a"]           # o/a was on disk before o/b ran
    data = json.loads((out / "results.json").read_text(encoding="utf-8"))
    a, b = data["repos"]
    assert a["error"].startswith("UnicodeDecodeError") and a["environment"]["verinoda_commit"] == "abc1234"
    assert "error" not in b and b["environment"]["verinoda_commit"] == "abc1234"
    assert data["runs"][0]["environment"]["verinoda_commit"] == "abc1234"
    assert "Not run: UnicodeDecodeError" in (out / "summary.md").read_text(encoding="utf-8")


def test_environment_is_kept_per_run_and_per_repository(tmp_path):
    """Finding 8: a later run on another commit must not relabel the earlier repositories."""
    p = tmp_path / "results.json"
    r1 = {"runs": [{"at": "1", "environment": {"verinoda_commit": "aaa"}, "repos": ["o/a"]}],
          "environment": {"verinoda_commit": "aaa"},
          "repos": [_results("o/a")["repos"][0] | {"environment": {"verinoda_commit": "aaa"}}]}
    r2 = {"runs": [{"at": "2", "environment": {"verinoda_commit": "bbb"}, "repos": ["o/b"]}],
          "environment": {"verinoda_commit": "bbb"},
          "repos": [_results("o/b")["repos"][0] | {"environment": {"verinoda_commit": "bbb"}}]}
    run.merge_results(p, r1)
    merged = run.merge_results(p, r2)
    envs = {r["name"]: r["environment"]["verinoda_commit"] for r in merged["repos"]}
    assert envs == {"o/a": "aaa", "o/b": "bbb"}
    assert [x["environment"]["verinoda_commit"] for x in merged["runs"]] == ["aaa", "bbb"]
    md = report.render(merged)
    assert "at aaa" in md and "at bbb" in md


def test_python_older_than_3_11_is_refused(tmp_path, monkeypatch):
    assert run.python_version(sys.executable) == sys.version_info[:2]
    monkeypatch.setattr(run, "python_version", lambda p: (3, 10))
    with pytest.raises(SystemExit, match="3.11 or later"):
        run.main(["--repos", "o/a", "--manifest", str(_two_repo_manifest(tmp_path)), "--out", str(tmp_path)])
    assert not (tmp_path / "results.json").exists()


# -- report --------------------------------------------------------------------------------------------

def _results(name="o/r", hits=3):
    return {"date": "2026-10-01", "environment": {"verinoda_version": "0.3.2"}, "runs": [],
            "repos": [{"name": name, "tag": "v1", "language": "Go", "files": 12, "scan_s": 3.25, "update_s": 1.5,
                       "query_median_s": 0.8, "analyze_median_s": 2.0, "crashes": ["schema"], "timeouts": [],
                       "clean_after": True,
                       "gold": {"hits": hits, "total": 5, "v2": {"hits": 1, "total": 2},
                                "facts": [{"id": "x", "kind": "q", "hit": False, "why": "no row"},
                                          {"id": "x@v2", "v": 2, "kind": "q", "hit": True, "why": ""}]},
                       "skipped": ["check: no Python, Java or Kotlin file (check reads only those)"],
                       "steps": [{"step": "schema", "exit": 1, "seconds": 0.5, "stdout_bytes": 0, "crash": True,
                                  "timeout": False, "reason": "traceback in stderr",
                                  "stderr_tail": "Traceback...\nKeyError: 'x'"}]},
                      {"name": "o/broken", "tag": "v2", "error": "clone failed"}]}


def test_report_table_and_details():
    md = report.render(_results())
    assert "| o/r (Go) | 12 | 3.2 | 1.5 | 0.8 / 2.0 | 1 | 0 | yes | 3/5 | 1/2 |" in md
    assert "KeyError: 'x'" in md and "MISS `x`" in md and "hit  `x@v2` v2" in md
    assert "Not run: clone failed" in md and "Skipped: check: no Python" in md


def test_merge_results_replaces_a_repo_run_again(tmp_path):
    p = tmp_path / "results.json"
    run.merge_results(p, _results("o/r", hits=1))
    merged = run.merge_results(p, {"runs": [{"repos": ["o/r"]}], "repos": [_results("o/r", hits=4)["repos"][0]]})
    names = [r["name"] for r in merged["repos"]]
    assert names.count("o/r") == 1 and "o/broken" in names
    assert next(r for r in merged["repos"] if r["name"] == "o/r")["gold"]["hits"] == 4
    assert b"\r\n" not in p.read_bytes()


def test_report_cli_writes_summary(tmp_path):
    p = tmp_path / "results.json"
    p.write_text(json.dumps(_results()), encoding="utf-8")
    assert report.main([str(p)]) == 0
    assert (tmp_path / "summary.md").read_text(encoding="utf-8").startswith("# Real-world run")
