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
import textwrap
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
mode = json.loads(os.environ.get("FAKE_MODE", "{}")).get(args[0] if args else "", "ok")
if mode == "crash":
    print("Traceback (most recent call last):\n  File \"x.py\", line 1\nKeyError: 'boom'", file=sys.stderr)
    sys.exit(1)
if mode == "hang":
    time.sleep(120)
if mode == "exit7":
    sys.exit(7)
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


def _runner(clone, fakeroot, **kw):
    return run.Runner(clone, python=sys.executable, verinoda_root=fakeroot, module="fakevn", **kw)


# -- the never-run-the-repository's-code rule ------------------------------------------------------------

@pytest.mark.parametrize("argv", [["trust", "."], ["observe"], ["experiment", "x"], ["probe", "f.py"],
                                  ["mutate"], ["analyze", "q", "--run-tests"], ["review", "--observe"],
                                  ["check", "a.py", "--checker", "tsc"], ["check", "--env=/x/python"],
                                  ["check", "--deps", "--registry"], ["setup", "."], ["hooks", "install"]])
def test_guard_refuses_what_would_run_repo_code(argv):
    with pytest.raises(run.Forbidden):
        run.guard_argv(argv)


def test_guard_refuses_before_any_process_starts(tmp_path, monkeypatch, fake):
    def boom(*a, **k):
        raise AssertionError("a process was started")
    monkeypatch.setattr(run.subprocess, "Popen", boom)
    r = _runner(_repo(tmp_path), fake[0])
    with pytest.raises(run.Forbidden):
        r.run("analyze", ["analyze", "how", "--run-tests"])
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
            "update", "review"} <= cmds
    for a in argvs:
        run.guard_argv(a)                       # none of them is refused
        assert not (cmds & run.FORBIDDEN_COMMANDS)


def test_real_verinoda_executes_nothing_of_the_repository(tmp_path):
    """The real CLI on a repository whose sitecustomize, conftest, setup.py and a folder named like
    Verinoda's own package would each leave a marker file if executed."""
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
            "scenario": {"check_file": "pkg/core.py"}}
    gold = {"facts": [{"id": "calls", "check": {"kind": "q", "query": "match (a:function)-[calls]->(b:function) "
                                                "where a.name = \"apply\" return a, b",
                                                "cites": ["pkg/core.py:5"]}}]}
    res = run.run_repo(repo, clone, python=sys.executable, verinoda_root=ROOT, gold=gold,
                       steps={"init", "scan", "gold", "check", "taint"})
    assert not marker.exists(), marker.read_text(encoding="utf-8")
    assert res["crashes"] == [] and res["timeouts"] == []
    assert res["gold"]["hits"] == 1, res["gold"]


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
        {"id": "d", "check": {"kind": "trace", "source": "a", "target": "b"}}]}
    before = (clone / "pkg" / "core.py").read_bytes()
    res = run.run_repo({"name": "o/tiny", "tag": "v1", "sha": "0" * 40, "language": "Go"}, clone,
                       python=sys.executable, verinoda_root=root, module="fakevn", gold=gold)
    assert res["crashes"] == ["schema"]
    assert res["timeouts"] == []
    assert [f["hit"] for f in res["gold"]["facts"]] == [True, False, True, False]
    assert res["gold"]["hits"] == 2 and res["gold"]["total"] == 4
    assert not any(s["step"] == "taint" for s in res["steps"])          # not a Python repository
    assert not (clone / ".verinoda" / "old.db").exists()                # init starts cold
    assert (clone / "pkg" / "core.py").read_bytes() == before           # the edit was reverted
    assert res["edit"]["file"] == "pkg/core.py"
    assert {"update", "review", "update:revert"} <= {s["step"] for s in res["steps"]}


def test_doctor_hang_is_a_timeout(tmp_path, fake, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", json.dumps({"doctor": "hang"}))
    r = _runner(_repo(tmp_path), fake[0], timeouts={"doctor": 2})
    rec, _ = r.run("doctor", ["doctor", "--json"])
    assert rec["timeout"]


# -- gold judging ----------------------------------------------------------------------------------------

def test_cites_is_exact_on_the_line_number():
    rows = [{"values": {"a": {"at": "gin.go:2360"}}, "evidence": {"paths": [[{"at": "src\\core.go:69"}]]}}]
    assert not run.cites(rows, "gin.go:236")
    assert run.cites(rows, "gin.go:2360")
    assert run.cites(rows, "src/core.go:69")


def test_judge_kinds():
    q = {"kind": "q", "cites": ["a.go:3"]}
    assert run.judge(q, 0, json.dumps({"rows": [{"at": "a.go:3"}]}))[0]
    assert run.judge(q, 1, json.dumps({"rows": []})) == (False, "no row")
    assert run.judge(q, 0, "not json")[0] is False
    tr = {"kind": "trace", "via_files": ["tree.go"]}
    path = {"status": "found", "paths": [[{"at": "gin.go:672"}, {"at": "gin.go:715"}]],
            "resolved": {"target": {"at": "tree.go:418"}}}
    assert run.judge(tr, 0, json.dumps(path))[0]
    assert not run.judge({"kind": "trace", "via_files": ["x.go"]}, 0, json.dumps(path))[0]
    assert run.judge(tr, 2, json.dumps({"status": "no directed path", "paths": []}))[1].startswith("no path")
    qu = {"kind": "query", "top_k": 2, "expect_file": "src/options.go"}
    assert run.judge(qu, 0, json.dumps({"items": [{"file": "a"}, {"file": "src\\options.go"}]})) == (True, "rank 2")
    assert not run.judge(qu, 0, json.dumps({"items": [{"file": "a"}, {"file": "b"}, {"file": "src/options.go"}]}))[0]
    ro = {"kind": "routes", "method": "GET", "path": "/items", "handler": "read_items"}
    table = {"route_table": [{"method": "get", "path": "/items", "handler": "app/api.py:3 read_items"}]}
    assert run.judge(ro, 0, json.dumps(table))[0]
    assert run.judge({"kind": "schema", "table": "user"}, 0, json.dumps({"tables": [{"name": "user"}]}))[0]


def test_gold_files_are_well_formed():
    manifest = run.load_manifest()
    names = {r["name"] for r in manifest["repos"]}
    for p in (RW / "gold").glob("*.json"):
        g = json.loads(p.read_text(encoding="utf-8"))
        assert g["repo"] in names and p.stem == run.slug(g["repo"])
        assert g["sha"] == next(r["sha"] for r in manifest["repos"] if r["name"] == g["repo"])
        assert 5 <= len(g["facts"]) <= 10
        for f in g["facts"]:
            assert f["fact"] and f["source"]
            run.gold_argv(f["check"])          # a known kind
            run.guard_argv(run.gold_argv(f["check"]))


def test_manifest_pins_every_repo():
    for r in run.load_manifest()["repos"]:
        assert len(r["sha"]) == 40 and r["tag"] and r["license"] and r["stars"] > 10000
        assert r["url"] == f"https://github.com/{r['name']}"


# -- the clone step (mocked) ------------------------------------------------------------------------------

def test_ensure_clone_clones_once_then_reuses(tmp_path):
    calls = []

    def clone_fn(repo, dest):
        calls.append(dest)
        dest.mkdir(parents=True)

    repo = {"name": "o/r", "sha": "a" * 40, "tag": "v1"}
    p = run.ensure_clone(repo, tmp_path, clone_fn=clone_fn, sha_fn=lambda d: "a" * 40)
    assert p == tmp_path / "o__r" and len(calls) == 1
    run.ensure_clone(repo, tmp_path, clone_fn=clone_fn, sha_fn=lambda d: "a" * 40)
    assert len(calls) == 1


def test_ensure_clone_refuses_another_sha_and_keeps_the_folder(tmp_path):
    (tmp_path / "o__r").mkdir()
    (tmp_path / "o__r" / "work.txt").write_text("mine", encoding="utf-8")
    with pytest.raises(RuntimeError, match="not the pinned"):
        run.ensure_clone({"name": "o/r", "sha": "a" * 40, "tag": "v1"}, tmp_path,
                         clone_fn=lambda r, d: None, sha_fn=lambda d: "b" * 40)
    assert (tmp_path / "o__r" / "work.txt").exists()


def test_head_sha_of_a_local_repository(tmp_path):
    clone = _repo(tmp_path)
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@t")
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "x"]):
        subprocess.run(["git", "-C", str(clone), *args], check=True, env=env, capture_output=True)
    sha = run.head_sha(clone)
    assert sha and len(sha) == 40
    assert "pkg/core.py" in run.tracked_files(clone)


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


def test_default_edit_appends_a_comment_to_the_largest_source_file(tmp_path):
    clone = _repo(tmp_path)
    spec = run.pick_edit(clone, ["pkg/__init__.py", "pkg/core.py", "README.md"], {})
    assert spec == {"file": "pkg/core.py", "after_line": None, "insert": "# verinoda-bench edit"}
    ed = run.Edit(clone, spec)
    ed.apply()
    assert (clone / "pkg" / "core.py").read_text(encoding="utf-8").endswith("# verinoda-bench edit\n")
    ed.revert()


# -- report --------------------------------------------------------------------------------------------

def _results(name="o/r", hits=3):
    return {"date": "2026-10-01", "environment": {"verinoda_version": "0.3.2"}, "runs": [],
            "repos": [{"name": name, "tag": "v1", "language": "Go", "files": 12, "scan_s": 3.25, "update_s": 1.5,
                       "query_median_s": 0.8, "analyze_median_s": 2.0, "crashes": ["schema"], "timeouts": [],
                       "gold": {"hits": hits, "total": 5, "facts": [{"id": "x", "kind": "q", "hit": False,
                                                                     "why": "no row"}]},
                       "steps": [{"step": "schema", "exit": 1, "seconds": 0.5, "stdout_bytes": 0, "crash": True,
                                  "timeout": False, "reason": "traceback in stderr",
                                  "stderr_tail": "Traceback...\nKeyError: 'x'"}]},
                      {"name": "o/broken", "tag": "v2", "error": "clone failed"}]}


def test_report_table_and_details():
    md = report.render(_results())
    assert "| o/r (Go) | 12 | 3.2 | 1.5 | 0.8 / 2.0 | 1 | 0 | 3/5 |" in md
    assert "KeyError: 'x'" in md and "MISS `x`" in md
    assert "Not run: clone failed" in md


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
