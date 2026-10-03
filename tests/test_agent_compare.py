"""The agent comparison's scoring scripts (benchmarks/agent_compare): arms and pairs from the sessions, totals, the
transcript reader's tool and token accounting and leak check, and the paired bootstrap."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"agent_compare_{name}", ROOT / "benchmarks" / "agent_compare" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


score = _load("score")
usage = _load("usage")
paired = _load("paired")

GOLD = {"s/q1": [{"id": "q1.a", "fact": "f", "match_any": [{"loc": "a.py:10"}]},
                 {"id": "q1.b", "fact": "g", "match_any": [{"text": "special_token"}]}]}
QUESTIONS = [{"set": "s", "id": "q1", "question": "?", "in_sample": False}]


def test_arms_come_from_the_sessions_none_first_and_every_tool_arm_is_paired():
    arms, pairs = score.arms_of([{"arm": "graphify"}, {"arm": "none"}, {"arm": "verinoda"}])
    assert arms == ("none", "graphify", "verinoda")
    assert pairs == (("graphify", "none"), ("verinoda", "none"), ("graphify", "verinoda"))
    arms, pairs = score.arms_of([{"arm": "auto_context"}])
    assert arms == ("auto_context",) and pairs == ()


def test_answers_are_scored_with_the_benchmark_scorer_and_a_failed_session_scores_nothing():
    sessions = [
        {"set": "s", "id": "q1", "arm": "none", "answer": "see a.py:10 and special_token", "tool_calls": 3,
         "index_tool_calls": 0, "stayed_inside": True, "used_forbidden_tool": False},
        {"set": "s", "id": "q1", "arm": "verinoda", "failed": True},
    ]
    res = score.score(sessions, GOLD, QUESTIONS)
    cell = res["per_question"]["s/q1"]
    assert cell["none"]["found"] == 2 and cell["verinoda"]["found"] == 0 and cell["verinoda"]["failed"]
    pair = res["overall"]["pairs"]["verinoda-none"]
    assert (pair["wins"], pair["ties"], pair["losses"], pair["sum_diff"]) == (0, 0, 1, -2)
    assert res["out_of_sample"]["arms"]["none"]["found"] == 2 and res["in_sample"]["arms"]["none"]["sessions"] == 0


def _transcript(path: Path, lines: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")


def test_usage_reads_tools_tokens_and_leaks_from_the_transcript(tmp_path):
    wf = tmp_path / "wf"
    wf.mkdir()
    (wf / "journal.jsonl").write_text(
        json.dumps({"type": "started", "agentId": "a1", "label": "r2 s/q1:verinoda_first"}) + "\n"
        + json.dumps({"type": "started", "agentId": "a2", "label": "s/q1:none"}) + "\n", encoding="utf-8")
    msg = {"id": "m1", "usage": {"input_tokens": 2, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 50,
                                 "output_tokens": 7},
           "content": [{"type": "tool_use", "name": "Bash", "input": {"command": "D:\\x\\verinoda.exe analyze q"}}]}
    # a streamed message logged twice counts once (its last usage)
    _transcript(wf / "agent-a1.jsonl", [{"message": msg}, {"message": {**msg, "content": []}}])
    _transcript(wf / "agent-a2.jsonl", [{"message": {"id": "m2", "usage": {"output_tokens": 1}, "content": [
        {"type": "tool_use", "name": "Read", "input": {"file_path": "D:/bench/x/agent/gold.json"}},
        {"type": "tool_use", "name": "Read", "input": {"file_path": "D:/bench/x/agent/s/none/a.py"}},
        {"type": "tool_use", "name": "Read", "input": {"file_path": "D:/bench/x/agent/s/verinoda/a.py"}}]}}])
    u = usage.usage(wf)
    a = u["s/q1:verinoda_first"]
    assert a["tool_calls"] == 1 and a["verinoda_cli"] == 1 and a["model_calls"] == 1
    assert a["input_total"] == 152 and a["tokens"]["output_tokens"] == 7 and a["leaks"] == []
    b = u["s/q1:none"]
    assert sorted(b["leaks"]) == ["agent/s/verinoda", "gold.json"]  # its own folder is not a leak


def test_usage_reads_real_world_sessions_whose_verinoda_runs_from_its_checkout(tmp_path):
    wf = tmp_path / "wf"
    wf.mkdir()
    (wf / "journal.jsonl").write_text(
        json.dumps({"type": "started", "agentId": "a1", "label": "rw axios__axios/q1:verinoda_first"}) + "\n",
        encoding="utf-8")
    exe = r"C:\Users\u\verinoda-mod\.venv\Scripts\verinoda.exe"  # the build under test, from its checkout
    _transcript(wf / "agent-a1.jsonl", [{"message": {"id": "m1", "usage": {"output_tokens": 1}, "content": [
        {"type": "tool_use", "name": "Bash", "input": {"command": f'"{exe}" analyze q --repo C:/b/rw/axios__axios/verinoda'}},
        {"type": "tool_use", "name": "Read", "input": {"file_path": "C:/b/rw/axios__axios/verinoda/lib/core/Axios.js"}},
        {"type": "tool_use", "name": "Read", "input": {"file_path": "C:/b/rw/axios__axios/none/lib/core/Axios.js"}},
        {"type": "tool_use", "name": "Read", "input": {"file_path": "C:/Users/u/verinoda-mod/benchmarks/realworld/gold/axios__axios.json"}}]}}])
    a = usage.usage(wf)["axios__axios/q1:verinoda_first"]
    assert a["verinoda_cli"] == 1 and a["tool_calls"] == 4
    assert sorted(a["leaks"]) == ["realworld/gold", "rw/axios__axios/none", "verinoda-mod"]


def test_paired_bootstrap_is_deterministic_and_brackets_the_ratio():
    a, b = [10, 20, 30, 40], [20, 20, 40, 40]
    r1 = paired.boot_ratio(a, b)
    assert r1 == paired.boot_ratio(a, b)
    assert r1[0] == 100 / 120 and r1[1] <= r1[0] <= r1[2]


score_rw = _load("score_rw")


def test_real_world_facts_are_found_by_a_citation_near_any_source_line():
    srcs = score_rw.sources("lib/core/Axios.js:40, lib/core/dispatchRequest.js:18-20")
    assert srcs == [("lib/core/Axios.js", 40), ("lib/core/dispatchRequest.js", 18)]
    assert score_rw.found(srcs, "it builds the chain (lib/core/Axios.js:42)")  # within 3 lines
    assert score_rw.found(srcs, "see core/dispatchRequest.js:10-17")  # a range ending 1 line before
    assert not score_rw.found(srcs, "lib/core/Axios.js:44 and Axios.js")  # 4 lines away; a bare file name
    assert not score_rw.found(srcs, "lib/other/Axios.js:40")  # another file of that name


def test_real_world_pairs_carry_a_bootstrap_interval_of_the_fact_difference_and_cost_ratios():
    cells = {"r/q1": {"none": {"found": 1}, "verinoda_first": {"found": 2}},
             "r/q2": {"none": {"found": 2}, "verinoda_first": {"found": 2}},
             "r/q3": {"none": {"found": 0}, "verinoda_first": {"found": 1}}}
    use = {f"{q}:{a}": {"input_total": 100 if a == "none" else 80, "tokens": {"output_tokens": 10},
                        "tool_calls": 5 if a == "none" else 4} for q in cells for a in cells[q]}
    p = score_rw.paired_against_none(cells, use, "verinoda_first")
    assert p["n"] == 3 and p["found_diff"] == 2
    lo, hi = p["found_diff_ci95"]
    assert 0 <= lo <= 2 <= hi <= 3  # resampled sums of the per-question differences (1, 0, 1)
    assert p["found_diff_ci95"] == score_rw.paired_against_none(cells, use, "verinoda_first")["found_diff_ci95"]
    assert p["input"]["ratio"] == 0.8 and p["output"]["ratio"] == 1.0 and p["tool_calls"]["ratio"] == 0.8


sensitivity_rw = _load("sensitivity_rw")


def test_declared_aliases_are_resolved_only_where_the_answer_declares_them():
    answer = "G = src/core/Gson.java. It calls getAdapter (G:647-649); see also XG:3 and `G:12`."
    out = sensitivity_rw.resolve_aliases(answer)
    assert "src/core/Gson.java:647-649" in out and "`src/core/Gson.java:12`" in out and "XG:3" in out
    assert sensitivity_rw.resolve_aliases("no alias here, Gson.java:4") == "no alias here, Gson.java:4"


score_stale = _load("score_stale")
TREE = {"src/core/Client.php": 120, "src/Handler/CurlFactory.php": 400}
TRAPS = [{"text": "doTransfer", "kind": "identifier", "old_source": "src/core/Client.php:90"},
         {"text": "src/Old.php", "kind": "path", "old_source": "src/Old.php:10"}]


def test_stale_citations_are_missing_files_lines_past_the_end_and_old_trap_locations():
    answer = ("send() calls transfer (src/core/Client.php:40); it used to be in src/Old.php:12, "
              "see Client.php:300 and core/Client.php:91; CurlFactory.php:50 is fine")
    assert score_stale.stale_citations(answer, TREE, TRAPS) == {"missing_file": 1, "past_end": 1, "old_location": 1}
    assert score_stale.stale_citations("nothing cited", TREE, TRAPS) == {"missing_file": 0, "past_end": 0,
                                                                         "old_location": 0}


def test_trap_mentions_are_whole_words_for_identifiers_and_substrings_for_paths():
    assert score_stale.trap_mentions("calls doTransfer() then reads src/Old.php", TRAPS) == ["doTransfer", "src/Old.php"]
    assert score_stale.trap_mentions("calls doTransferAsync", TRAPS) == []


def test_stale_study_scores_facts_and_pairs_fresh_against_stale():
    questions = [{"repo": "r", "qid": f"q{i}", "question": "?", "stale_traps": TRAPS,
                  "facts": [{"id": f"f{i}", "fact": "x", "source": "src/core/Client.php:40"}]} for i in (1, 2)]
    sessions = [{"repo": "r", "qid": "q1", "arm": "verinoda_fresh", "answer": "src/core/Client.php:41"},
                {"repo": "r", "qid": "q1", "arm": "verinoda_stale", "answer": "src/Old.php:10"},
                {"repo": "r", "qid": "q2", "arm": "verinoda_fresh", "answer": "src/core/Client.php:40"},
                {"repo": "r", "qid": "q2", "arm": "verinoda_stale", "answer": "src/core/Client.php:40"}]
    res = score_stale.score(sessions, questions, {"r": TREE})
    assert res["overall"]["verinoda_fresh"]["found"] == 2 and res["overall"]["verinoda_stale"]["found"] == 1
    assert res["overall"]["verinoda_stale"]["stale_citations"] == 1
    p = score_stale.paired(res["per_question"], "verinoda_fresh", "verinoda_stale", "found")
    assert p["n"] == 2 and p["diff"] == 1 and (p["wins"], p["ties"], p["losses"]) == (1, 1, 0)
    assert p["ci95"][0] <= 1 <= p["ci95"][1]


def test_usage_reads_stale_study_sessions_and_flags_the_history_and_other_copies(tmp_path):
    wf = tmp_path / "wf"
    wf.mkdir()
    (wf / "journal.jsonl").write_text(
        json.dumps({"type": "started", "agentId": "a1", "label": "st gin-gonic__gin/q2:verinoda_stale"}) + "\n",
        encoding="utf-8")
    _transcript(wf / "agent-a1.jsonl", [{"message": {"id": "m1", "usage": {"output_tokens": 1}, "content": [
        {"type": "tool_use", "name": "Read", "input": {"file_path": "C:/b/stale/gin-gonic__gin/stale/gin.go"}},
        {"type": "tool_use", "name": "Read", "input": {"file_path": "C:/b/stale/gin-gonic__gin/fresh/gin.go"}},
        {"type": "tool_use", "name": "Bash", "input": {"command": "git -C C:/b/stale/hist/gin-gonic__gin log"}},
        {"type": "tool_use", "name": "Read", "input": {"file_path": "C:/b/stale/tree_new.json"}}]}}])
    a = usage.usage(wf)["gin-gonic__gin/q2:verinoda_stale"]
    assert a["tool_calls"] == 4
    assert sorted(a["leaks"]) == ["stale/gin-gonic__gin/fresh", "stale/hist", "tree_new.json"]


guard_run = _load("guard_run")


def test_guard_arms_differ_only_by_the_plugin_and_the_prompt_never_names_verinoda(tmp_path):
    task = {"id": "t1", "prompt": "Add f() to pkg/a.py."}
    p = guard_run.prompt_for(task, tmp_path, "C:/env/python.exe")
    assert "Add f() to pkg/a.py." in p and "verinoda" not in p.lower()
    plain = guard_run.session_argv(p, "plain", "C:/mod", 100)
    guard = guard_run.session_argv(p, "guard", "C:/mod", 100)
    assert guard[:len(plain)] == plain and guard[len(plain):] == ["--plugin-dir", "C:/mod"]
    assert "--strict-mcp-config" in plain


def test_guard_notes_are_counted_from_the_hook_context_and_bad_sites_from_the_check(tmp_path):
    lines = [{"type": "attachment", "attachment": {"type": "hook_additional_context",
                                                   "content": ["[Verinoda check] The lines you just changed ..."]}},
             {"type": "user", "message": {"content": "quoting [Verinoda check] in a prompt is not a note"}},
             {"type": "attachment", "attachment": {"type": "hook_additional_context", "content": ["other hook"]}}]
    t = tmp_path / "s.jsonl"
    t.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    assert guard_run.count_notes(t) == 1 and guard_run.count_notes(None) == 0
    report = {"sites": [{"at": "a.py:1:2", "verdict": "absent"}, {"at": "a.py:3:1", "verdict": "unknown"},
                        {"at": "b.py:9:9", "verdict": "mismatch"}]}
    assert guard_run.bad_sites(report) == ["a.py:1:2", "b.py:9:9"]


score_guard = _load("score_guard")


def test_guard_scores_pair_tasks_both_arms_ran_and_leave_unknown_checks_out_of_the_sites_pair():
    tasks = [{"id": i, "repo": "graphify", "prompt_names_helpers": []} for i in ("a", "b", "c")]
    row = lambda i, arm, passed, bad: {"id": i, "arm": arm, "passed": passed, "check_bad_sites": bad,
                                       "notes": 1 if arm == "guard" and bad == [] else 0, "num_turns": 5,
                                       "cost_usd": 0.5, "seconds": 60}
    res = [row("a", "plain", False, ["x.py:1:1"]), row("a", "guard", True, []),
           row("b", "plain", True, []), row("b", "guard", True, []),
           row("c", "plain", True, None), row("c", "guard", False, []),
           row("d", "plain", True, [])]  # not a task of the study
    c = score_guard.cells(res, tasks)
    assert sorted(c) == ["a", "b", "c"]
    s = score_guard.summary(c)
    assert s["plain"]["passed"] == 2 and s["guard"]["passed"] == 2 and s["plain"]["absent_sites"] == 1
    d = score_guard.decide(c)
    assert d["passes guard-plain"]["diff"] == 0 and d["passes guard-plain"]["n"] == 3
    assert d["absent sites plain-guard"]["n"] == 2 and d["absent sites plain-guard"]["diff"] == 1


big_run = _load("big_run")
score_big = _load("score_big")


def test_big_arms_differ_in_tools_and_plugin_only_and_the_prompt_names_no_tool(tmp_path):
    cfg = {"model": "claude-sonnet-5-5", "plugin_dir": "C:/mod", "mod_options": {"auto": "nudge"}}
    task = {"id": "pr1", "title": "Light does not turn off", "body": "It stays on after the timeout."}
    p = big_run.prompt_for(task, tmp_path)
    assert "Light does not turn off" in p and "verinoda" not in p.lower() and "graphify" not in p.lower()
    argv = {a: big_run.session_argv(p, a, tmp_path, cfg) for a in ("none", "graphify", "verinoda_setup", "verinoda_mod")}
    assert argv["none"] == argv["graphify"] and "--mcp-config" not in argv["none"]
    assert "--mcp-config" in argv["verinoda_setup"] and "--plugin-dir" not in argv["verinoda_setup"]
    assert argv["verinoda_mod"][:len(argv["verinoda_setup"])] == argv["verinoda_setup"]
    assert argv["verinoda_mod"][-4:-2] == ["--plugin-dir", "C:/mod"]
    assert json.loads(argv["verinoda_mod"][-1]) == {"pluginConfigs": {"verinoda-live": {"options": {"auto": "nudge"}}}}
    assert argv["none"][argv["none"].index("--allowedTools") + 1] == "Bash Read Glob Grep Skill"
    assert argv["verinoda_mod"][argv["verinoda_mod"].index("--allowedTools") + 1].endswith("mcp__verinoda")
    assert "--setting-sources" in argv["none"] and "project,local" in argv["none"]


def test_big_session_env_takes_the_users_tool_commands_off_path_and_adds_the_arms_own():
    cfg = {"drop_path": ["C:/Users/u/.local/bin"], "path_dirs": {"graphify": ["C:/g/Scripts"]}}
    base = {"PATH": os.pathsep.join([r"C:\Users\u\.local\bin", r"C:\Windows", "C:\\Users\\u\\.local\\bin\\"])}
    g = big_run.session_env("graphify", cfg, base)["PATH"].split(os.pathsep)
    assert g == ["C:/g/Scripts", r"C:\Windows"]
    assert big_run.session_env("none", cfg, base)["PATH"].split(os.pathsep) == [r"C:\Windows"]
    assert big_run.session_env("none", cfg, base)["GRAPHIFY_NO_AUTO_REFRESH"] == "1"


def test_big_answers_name_at_most_five_distinct_files_relative_to_the_root(tmp_path):
    root = tmp_path.as_posix()
    ans = {"files": [{"path": f"{root}/homeassistant/a.py", "why": "x"}, {"path": r".\homeassistant\b.py", "why": "y"},
                     {"path": "homeassistant/a.py", "why": "dup"}, {"path": "/homeassistant/c.py"}, {"path": ""},
                     {"path": "d.py"}, {"path": "e.py"}, {"path": "f.py"}]}
    assert big_run.files_named(ans, tmp_path) == ["homeassistant/a.py", "homeassistant/b.py", "homeassistant/c.py", "d.py", "e.py"]
    assert big_run.files_named(None, tmp_path) == []
    assert big_run.extract_answer({"structured_output": {"files": []}}) == {"files": []}
    assert big_run.extract_answer({"result": 'text {"files": [{"path": "a.py"}]} more'}) == {"files": [{"path": "a.py"}]}
    assert big_run.extract_answer({"result": "no json"}) is None


def test_big_session_stats_count_tool_use_and_flag_network_lookups(tmp_path):
    def use(name, **inp):
        return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "name": name, "input": inp}]}}
    lines = [use("Bash", command="graphify query 'light timeout'"), use("Read", file_path="graphify-out/GRAPH_REPORT.md"),
             use("mcp__verinoda__analyze", question="q"), use("Bash", command=r"C:\x\verinoda.exe query q --repo ."),
             use("Bash", command="curl -s https://api.github.com/repos/home-assistant/core/pulls/1"), use("Grep", pattern="x")]
    t = tmp_path / "s.jsonl"
    t.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    s = big_run.session_stats(t)
    assert s["tool_calls"] == 6 and s["verinoda_calls"] == 2 and s["graphify_calls"] == 2 and len(s["network"]) == 1
    assert s["first_tools"] == ["Bash", "Read", "mcp__verinoda__analyze", "Bash", "Bash"]
    assert big_run.session_stats(None)["tool_calls"] == 0


def test_big_scores_pair_arms_on_recall_and_leave_network_sessions_out_in_the_second_view():
    tasks = [{"id": f"t{i}", "gold": ["a.py", "b.py"] if i == 0 else ["c.py"]} for i in range(3)]

    def row(i, arm, files, net=()):
        return {"id": f"t{i}", "arm": arm, "files": files, "num_turns": 5, "seconds": 60.0, "cost_usd": 0.5,
                "input_tokens": 1000, "output_tokens": 100, "verinoda_calls": 2 if arm == "verinoda_mod" else 0,
                "graphify_calls": 0, "network": list(net), "answered": True}
    res = [row(0, "none", ["a.py"]), row(0, "verinoda_mod", ["a.py", "b.py", "z.py"]),
           row(1, "none", ["x.py"]), row(1, "verinoda_mod", ["c.py"], net=["curl x"]),
           row(2, "none", ["c.py"]), row(2, "verinoda_mod", ["c.py"])]
    rep = score_big.report(res, tasks)
    s = rep["all sessions"]["summary"]
    assert s["none"]["recall"] == 1.5 and s["verinoda_mod"]["recall"] == 3.0 and s["verinoda_mod"]["sessions_using_tool"] == 3
    d = rep["all sessions"]["decisions"]["verinoda_mod - none"]
    assert d["n"] == 3 and d["diff"] == 1.5 and (d["wins"], d["ties"], d["losses"]) == (2, 1, 0)
    assert d["ci95"][0] <= 1.5 <= d["ci95"][1]
    d2 = rep["without sessions that looked something up on the network"]["decisions"]["verinoda_mod - none"]
    assert d2["n"] == 2 and d2["diff"] == 0.5
    assert score_big.cell(["c.py", "a.py"], ["a.py", "b.py"]) == {"recall": 0.5, "solved": 0, "hit1": 0, "precision": 0.5, "named": 2}


def test_big_status_difference_is_what_a_run_changed_not_what_the_setup_added(tmp_path):
    import subprocess

    def git(*a):
        subprocess.run(["git", "-C", str(tmp_path), *a], check=True, capture_output=True)

    git("init", "-q")
    (tmp_path / "a.py").write_bytes(b"x = 1\n")
    git("add", "-A")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    (tmp_path / ".mcp.json").write_bytes(b"{}")  # the setup's file, there before the run
    before = big_run.status_paths(str(tmp_path))
    assert before == {"?? .mcp.json"}
    (tmp_path / "a.py").write_bytes(b"x = 2\n")  # what a run changed
    assert sorted(big_run.status_paths(str(tmp_path)) ^ before) == [" M a.py"]


def test_big_run_limits_how_many_sessions_run_at_once_per_arm_and_survives_a_crashing_one():
    import threading
    import time
    lock, now, peak, seen = threading.Lock(), {}, {}, []

    def work(item):
        _, arm = item
        with lock:
            now[arm] = now.get(arm, 0) + 1
            peak[arm] = max(peak.get(arm, 0), now[arm])
        time.sleep(0.05)
        with lock:
            now[arm] -= 1
            seen.append(item)

    todo = [(f"t{i}", arm) for i in range(6) for arm in ("none", "verinoda_mod")]
    big_run.run_by_arm(todo, {"none": 3, "verinoda_mod": 1}, work)
    assert sorted(seen) == sorted(todo) and peak["verinoda_mod"] == 1 and 1 < peak["none"] <= 3

    def crashing(item):
        if item[0] == "t1":
            raise RuntimeError("boom")
        seen.append(("ok", *item))

    seen.clear()
    failed = big_run.run_by_arm([("t0", "none"), ("t1", "none"), ("t2", "none")], {}, crashing)
    assert [f[0] for f in failed] == [("t1", "none")] and len(seen) == 2


def test_big_run_reads_free_memory_where_it_can():
    assert big_run.free_mb() is None or big_run.free_mb() > 0


def test_big_extra_pools_runs_by_mean_and_measures_the_noise_of_one_arm_against_itself():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "big_extra", ROOT / "benchmarks" / "results" / "agent-compare-big-2026-10-02" / "extra.py")
    extra = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = extra
    spec.loader.exec_module(extra)
    cell = lambda r: {"recall": r, "solved": 0, "hit1": 0, "precision": 0.0, "named": 1, "turns": 4, "seconds": 10.0,
                      "cost": 0.1, "input": 100, "output": 10, "tool": 0, "network": 0, "answered": True}
    r1 = {"t1": {"none": cell(0.0)}, "t2": {"none": cell(1.0)}}
    r2 = {"t1": {"none": cell(1.0)}, "t2": {"none": cell(1.0)}}
    assert extra.pool([r1, r2])["t1"]["none"]["recall"] == 0.5
    n = extra.noise_floor(r1, r2, "none")
    assert n["n"] == 2 and n["diff"] == -1.0 and (n["wins"], n["ties"], n["losses"]) == (0, 1, 1)


commit_tasks = _load("commit_tasks")


def test_commit_tasks_gold_paths_are_code_and_build_files_not_tests_docs_or_ci():
    for good in ("src/arch/arm/kernel/boot.c", "include/arch/x86/arch/machine.h", "CMakeLists.txt", "tools/dts.py",
                 "libsel4/arch_include/arm/sel4/arch/types.bf", "src/plat/tx1/overlay-tx1.dts", "src/arch/riscv/head.S"):
        assert commit_tasks.is_gold_path(good), good
    for bad in ("manual/parts/threads.tex", "README.md", "docs/design.md", ".github/workflows/x.yml", "tests/a.c",
                "libsel4/tests/b.c", "LICENSE", "src/x.png"):
        assert not commit_tasks.is_gold_path(bad), bad


def test_commit_tasks_closing_keywords_include_the_cross_repository_spelling():
    f = commit_tasks.CLOSES.findall
    assert f("Fixes #12") == ["12"] and f("closes seL4/seL4#34") == ["34"] and f("Resolves: https://github.com/seL4/seL4/issues/56") == ["56"]
    assert f("see #7, related to #8") == []


def test_commit_tasks_selection_applies_the_filters_at_the_base_commit(tmp_path):
    import subprocess

    def git(*a):
        subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t", *a], check=True, capture_output=True)

    git("init", "-q")
    for name in ("src/a.c", "src/b.c", "README.md"):
        (tmp_path / name).parent.mkdir(exist_ok=True)
        (tmp_path / name).write_bytes(b"x = 1")
    git("add", "-A")
    git("commit", "-qm", "base")
    sha = subprocess.run(["git", "-C", str(tmp_path), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    body = "The kernel hangs when the scheduler is preempted twice in a row on SMP. " * 4

    def cand(issue, files, text=body, created="2023-01-01T00:00:00Z"):
        return {"issue": issue, "title": "hang", "body": text, "created": created, "labels": [], "via": "commit", "ref": "r",
                "base_sha": sha, "fix_at": "2024-01-01T00:00:00Z", "files": files}

    cands = [cand(1, [("src/a.c", "modified")]),                                           # kept
             cand(2, [("src/a.c", "modified"), ("src/new.c", "added")]),                   # kept: the added file is not gold
             cand(3, [("README.md", "modified")]),                                         # docs only
             cand(4, [("src/gone.c", "modified")]),                                        # not at the base commit
             cand(5, [("src/b.c", "modified")], text="short"),                             # body too short
             cand(6, [("src/a.c", "modified")], text=body + " see src/a.c"),               # names the gold file
             cand(7, [("src/a.c", "modified")], created="2025-01-01T00:00:00Z")]           # newer than the fix
    cfg = {"clone": str(tmp_path), "seed": 1, "want": 10, "gold_max": 6}
    tasks, info = commit_tasks.select(cfg, cands)
    assert sorted(t["issue"] for t in tasks) == [1, 2] and tasks[0]["base_sha"] == sha
    assert info["passed_filters"] == 2 and sum(info["dropped"].values()) == 5
    assert commit_tasks.select({**cfg, "want": 1}, cands)[0].__len__() == 1


def test_big_prompt_takes_the_project_from_the_config_and_defaults_to_home_assistant(tmp_path):
    task = {"id": "i1", "title": "T", "body": "B"}
    assert big_run.prompt_for(task, tmp_path).startswith("You are working in the Home Assistant Core repository at ")
    p = big_run.prompt_for(task, tmp_path, "the seL4 microkernel repository at {c} (a git checkout of it)")
    assert p.startswith("You are working in the seL4 microkernel repository at ") and "Home Assistant" not in p
    assert "verinoda" not in p.lower() and "graphify" not in p.lower()


def test_big_copy_of_is_one_per_task_when_the_config_has_a_pattern():
    assert big_run.copy_of({"copies": {"none": "/w/none"}}, {"id": "t1"}, "none").as_posix() == "/w/none"
    assert big_run.copy_of({"copy_pattern": "/w/{task}/{arm}"}, {"id": "t1"}, "graphify").as_posix() == "/w/t1/graphify"


def test_big_pooled_report_averages_the_runs_per_task_and_arm_and_pairs_the_runs_for_the_noise_floor():
    tasks = [{"id": "t0", "gold": ["a.py", "b.py"]}, {"id": "t1", "gold": ["c.py"]}]

    def row(i, arm, files):
        return {"id": i, "arm": arm, "files": files, "num_turns": 4, "seconds": 10.0, "cost_usd": 0.1, "input_tokens": 100,
                "output_tokens": 10, "verinoda_calls": 0, "graphify_calls": 0, "network": [], "answered": True}
    r1 = [row("t0", "none", ["a.py"]), row("t0", "verinoda_mod", ["a.py", "b.py"]), row("t1", "none", ["x.py"]), row("t1", "verinoda_mod", ["c.py"])]
    r2 = [row("t0", "none", ["a.py", "b.py"]), row("t0", "verinoda_mod", ["a.py", "b.py"]), row("t1", "none", ["c.py"]), row("t1", "verinoda_mod", ["c.py"])]
    rep = score_big.report_pooled([r1, r2], tasks)
    s = rep["pooled"]["summary"]
    assert s["none"]["recall"] == 1.25 and s["verinoda_mod"]["recall"] == 2.0
    d = rep["pooled"]["decisions"]["verinoda_mod - none"]
    assert d["n"] == 2 and d["diff"] == 0.75
    assert len(rep["runs"]) == 2 and rep["runs"][0]["summary"]["none"]["recall"] == 0.5
    n = rep["noise_floor"]["none"]
    assert len(n) == 1 and n[0]["runs"] == [1, 2] and n[0]["diff"] == -1.5
    assert rep["noise_floor"]["verinoda_mod"][0]["diff"] == 0.0


def test_big_session_stats_do_not_take_a_working_copys_name_for_a_call_to_the_tool(tmp_path):
    """The copies are folders called `verinoda`, `verinoda_mod`, `graphify`: a command that only names the folder is not a call."""
    def use(name, **inp):
        return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "name": name, "input": inp}]}}
    not_calls = [use("Bash", command="cd C:/w/i805/verinoda_mod && ls && wc -l gcc.cmake"),
                 use("Bash", command="grep -rn fastpath C:/vbench/big/verinoda src include"),
                 use("Bash", command="cd C:/vbench/big/verinoda; grep -n x y.c"),
                 use("Bash", command="cd C:/vbench/big/graphify && grep -rn handler homeassistant"),
                 use("Bash", command="ls C:/w/i1/verinoda_setup/src"),
                 use("Grep", pattern="verinoda", path="C:/w/i1/verinoda"),
                 use("Read", file_path="C:/vbench/big/graphify/homeassistant/x.py")]
    calls = [use("Bash", command="verinoda analyze \"why\" --repo ."),
             use("Bash", command=r'"C:\x\.venv\Scripts\verinoda.exe" query "q" --repo C:/w/verinoda'),
             use("Bash", command="cd C:/w/verinoda && verinoda query x"),
             use("mcp__verinoda__analyze", question="q"),
             use("Bash", command="graphify query 'light timeout'"),
             use("Bash", command="C:/g/Scripts/graphify.exe explain Notifications"),
             use("Read", file_path="graphify-out/GRAPH_REPORT.md"),
             use("Bash", command="cat graphify-out/GRAPH_REPORT.md | head -50")]
    path = tmp_path / "s.jsonl"
    path.write_text("\n".join(json.dumps(x) for x in not_calls) + "\n", encoding="utf-8")
    s = big_run.session_stats(path)
    assert s["tool_calls"] == 7 and s["verinoda_calls"] == 0 and s["graphify_calls"] == 0
    path.write_text("\n".join(json.dumps(x) for x in calls) + "\n", encoding="utf-8")
    s = big_run.session_stats(path)
    assert s["verinoda_calls"] == 4 and s["graphify_calls"] == 4, s


def test_big_arm_defs_add_arms_and_leave_the_old_ones_as_they_were(tmp_path):
    cfg = {"model": "claude-sonnet-5-5", "plugin_dir": "C:/mod", "mod_options": {"auto": "nudge"},
           "arm_defs": {"assist_full": {"kind": "verinoda", "plugin": True, "copy_arm": "verinoda_mod", "mod_options": {"assist": "full"}},
                        "forced": {"kind": "verinoda", "plugin": True, "copy_arm": "verinoda_mod", "mod_options": {"assist": "tool"},
                                   "prompt_suffix": "Start by calling the locate tool.", "model": "claude-haiku-4-5-20251001"}}}
    task = {"id": "t", "title": "T", "body": "B"}
    p = big_run.prompt_for(task, tmp_path)
    full = big_run.session_argv(p, "assist_full", tmp_path, cfg)
    assert "--mcp-config" in full and full[full.index("--plugin-dir") + 1] == "C:/mod"
    assert json.loads(full[-1]) == {"pluginConfigs": {"verinoda-live": {"options": {"assist": "full"}}}}  # the arm's options, not the study's
    allowed = full[full.index("--allowedTools") + 1].split()
    assert "mcp__verinoda" in allowed and "mcp__verinoda-live" in allowed
    assert full[full.index("--model") + 1] == "claude-sonnet-5-5"
    forced = big_run.session_argv(p, "forced", tmp_path, cfg)
    assert forced[forced.index("--model") + 1] == "claude-haiku-4-5-20251001"
    # the old arms are what they were: the mod arm takes the study's options and no assist tools are allowed elsewhere
    old = big_run.session_argv(p, "verinoda_mod", tmp_path, cfg)
    assert json.loads(old[-1]) == {"pluginConfigs": {"verinoda-live": {"options": {"auto": "nudge"}}}}
    assert "mcp__verinoda-live" not in old[old.index("--allowedTools") + 1].split()
    assert "--plugin-dir" not in big_run.session_argv(p, "verinoda_setup", tmp_path, cfg)
    assert "--mcp-config" not in big_run.session_argv(p, "none", tmp_path, cfg)
    # the prompt: the arm's suffix follows the shared text; without one it is the shared text
    assert big_run.prompt_for(task, tmp_path, None, "Start by calling the locate tool.").endswith("\n\nStart by calling the locate tool.")
    assert big_run.prompt_for(task, tmp_path, None, "").endswith("one sentence on why.")
    with pytest.raises(KeyError):
        big_run.arm_def(cfg, "nonesuch")


def test_an_arm_that_uses_another_arms_copy_gets_that_arms_tool_dirs_on_path():
    cfg = {"path_dirs": {"verinoda_mod": ["C:/v/Scripts"], "alone": ["C:/a"]},
           "arm_defs": {"assist": {"kind": "verinoda", "copy_arm": "verinoda_mod"}, "alone": {"kind": "none"}}}
    base = {"PATH": r"C:\Windows"}
    assert big_run.session_env("assist", cfg, base)["PATH"].split(os.pathsep) == ["C:/v/Scripts", r"C:\Windows"]
    assert big_run.session_env("alone", cfg, base)["PATH"].split(os.pathsep) == ["C:/a", r"C:\Windows"]
    assert big_run.session_env("none", cfg, base)["PATH"].split(os.pathsep) == [r"C:\Windows"]


def test_big_arms_may_share_a_working_copy_one_session_at_a_time():
    cfg = {"copy_pattern": "/w/{task}/{arm}", "arm_defs": {"assist": {"kind": "verinoda", "copy_arm": "verinoda_mod"}}}
    assert big_run.copy_of(cfg, {"id": "t1"}, "assist").as_posix() == "/w/t1/verinoda_mod"
    assert big_run.copy_of(cfg, {"id": "t1"}, "none").as_posix() == "/w/t1/none"
    assert big_run.copy_of({"copies": {"verinoda_mod": "/w/vm"}, "arm_defs": cfg["arm_defs"]}, {"id": "t1"}, "assist").as_posix() == "/w/vm"
    a, b = big_run.copy_lock("/w/t1/verinoda_mod"), big_run.copy_lock("/w/t1/verinoda_mod")
    assert a is b and big_run.copy_lock("/w/t2/verinoda_mod") is not a


def test_big_session_stats_count_what_the_assist_features_did(tmp_path):
    def use(name, **inp):
        return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "name": name, "input": inp}]}}

    def hook(text):
        return {"type": "attachment", "attachment": {"type": "hook_additional_context", "content": [text], "hookName": "tool.call"}}

    def result(text, error=False):
        return {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": text, "is_error": error}]}}
    lines = [hook("[Verinoda locate] Files Verinoda's index point to:\nsrc/a.c"), use("ToolSearch", query="select:mcp__verinoda-live__locate"),
             use("mcp__verinoda-live__locate", text="bug"), use("mcp__verinoda-live__coupled", files=["src/a.c"]),
             use("Grep", pattern="x"), result("[Verinoda] This search was not run. Before searching, ...", error=True),
             use("Read", file_path="src/a.c"), hook("[Verinoda coupled] src/a.c is usually changed together with these files"),
             use("Read", file_path="src/b.c"), hook("[Verinoda coupled] src/b.c is usually changed together with these files"),
             hook("[Verinoda auto-context] This project has a Verinoda code index"), result("a normal result")]
    t = tmp_path / "s.jsonl"
    t.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    s = big_run.session_stats(t)
    assert s["assist"] == {"inject": 1, "coupled_notes": 2, "gate": 1, "nudge": 1, "locate_calls": 1, "coupled_calls": 1, "tool_search": 1}
    assert s["verinoda_calls"] == 2  # the mod's own tools are Verinoda use
    assert big_run.session_stats(None)["assist"] == {"inject": 0, "coupled_notes": 0, "gate": 0, "nudge": 0, "locate_calls": 0,
                                                     "coupled_calls": 0, "tool_search": 0}


def test_big_scores_take_any_arm_in_the_data_and_decide_each_new_one_against_none():
    tasks = [{"id": "t0", "gold": ["a.py", "b.py"]}, {"id": "t1", "gold": ["c.py"]}]
    assist = {"inject": 0, "coupled_notes": 1, "gate": 0, "locate_calls": 0, "coupled_calls": 1, "tool_search": 1}

    def row(i, arm, files, a=None):
        return {"id": i, "arm": arm, "files": files, "num_turns": 4, "seconds": 10.0, "cost_usd": 0.1, "input_tokens": 100,
                "output_tokens": 10, "verinoda_calls": 1 if a else 0, "graphify_calls": 0, "network": [], "answered": True,
                "assist": a or {k: 0 for k in assist}}
    r1 = [row("t0", "none", ["a.py"]), row("t0", "assist_full", ["a.py", "b.py"], assist), row("t0", "verinoda_setup", ["a.py"]),
          row("t1", "none", ["x.py"]), row("t1", "assist_full", ["c.py"], assist), row("t1", "verinoda_setup", ["x.py"])]
    r2 = [row("t0", "none", ["a.py", "b.py"]), row("t0", "assist_full", ["a.py", "b.py"]), row("t0", "verinoda_setup", ["a.py"]),
          row("t1", "none", ["c.py"]), row("t1", "assist_full", ["c.py"]), row("t1", "verinoda_setup", ["x.py"])]
    # Graphify joins: each added arm is also decided against it (the pre-registered second decision of the assist study)
    r1 += [row("t0", "graphify", ["a.py"]), row("t1", "graphify", ["x.py"])]
    r2 += [row("t0", "graphify", ["a.py"]), row("t1", "graphify", ["c.py"])]
    rep = score_big.report_pooled([r1, r2], tasks)
    s = rep["pooled"]["summary"]
    assert list(s) == ["none", "graphify", "verinoda_setup", "assist_full"]  # the known arms in their order, the others after
    assert "assist_full - graphify" in rep["pooled"]["decisions"]
    # pooled over runs, a task counts once if any of its runs had the feature show or the tool called
    assert s["assist_full"]["recall"] == 2.0 and s["assist_full"]["sessions_assist_shown"] == 2
    assert s["assist_full"]["sessions_assist_called"] == 2 and s["none"]["sessions_assist_shown"] == 0
    assert rep["pooled"]["decisions"]["assist_full - none"]["diff"] == 0.75
    assert "assist_full - verinoda_setup" in rep["pooled"]["secondary"]
    assert "assist_full" in rep["noise_floor"] and "none" in rep["noise_floor"]
    one = score_big.report(r1, tasks)["all sessions"]
    assert "assist_full - none" in one["decisions"] and one["summary"]["assist_full"]["sessions_assist_shown"] == 2
    # a result written before the counters existed still scores
    old = {k: v for k, v in r1[0].items() if k != "assist"}
    assert score_big.cells([old], tasks)["t0"]["none"]["assist_shown"] == 0


def test_a_choice_the_mod_stored_is_emptied_for_the_run_and_put_back_after_it(tmp_path):
    """A choice stored by /verinoda-auto, -guard or -assist wins over the settings a scripted session is given: the
    smoke run of the assist arms got the nudge of an earlier `/verinoda-auto nudge` that way."""
    d = tmp_path / "store"
    d.mkdir()
    mine = d / "verinoda-live_inline-abc.json"
    mine.write_text('{"auto": "nudge", "guard": false}', encoding="utf-8")
    other = d / "diff_builtin-x.json"
    other.write_text('{"k": 1}', encoding="utf-8")
    with big_run.isolated_plugin_store(d):
        assert json.loads(mine.read_text(encoding="utf-8")) == {}  # nothing stored: the arm's own options decide
        assert other.read_text(encoding="utf-8") == '{"k": 1}'  # the stores of other plugins are not touched
    assert json.loads(mine.read_text(encoding="utf-8")) == {"auto": "nudge", "guard": False}
    assert not list(d.glob("*.study-backup"))
    with pytest.raises(RuntimeError), big_run.isolated_plugin_store(d):  # an error inside puts it back too
        raise RuntimeError("boom")
    assert json.loads(mine.read_text(encoding="utf-8")) == {"auto": "nudge", "guard": False}
    # a run that died before it could put the file back: the next one starts by doing that
    mine.write_text("{}", encoding="utf-8")
    (d / (mine.name + ".study-backup")).write_text('{"auto": "search"}', encoding="utf-8")
    with big_run.isolated_plugin_store(d):
        assert json.loads(mine.read_text(encoding="utf-8")) == {}
    assert json.loads(mine.read_text(encoding="utf-8")) == {"auto": "search"}
    with big_run.isolated_plugin_store(tmp_path / "no-such-folder"):  # nothing stored yet is fine
        pass


def test_a_study_may_cap_the_sessions_running_at_once_whatever_the_arms(tmp_path, monkeypatch):
    """Every arm has its own pool, and a Verinoda session holds a daemon and a `claude` process: a cap over all arms."""
    import threading
    import time
    state = {"now": 0, "peak": 0}
    guard = threading.Lock()

    def fake_run(argv, cwd, env, timeout):
        with guard:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
        time.sleep(0.05)
        with guard:
            state["now"] -= 1
        return 0, json.dumps({"session_id": "s", "usage": {}, "result": "{}"}), "", 0.1
    monkeypatch.setattr(big_run, "run", fake_run)
    monkeypatch.setattr(big_run, "find_transcript", lambda sid: None)
    cfg = {"model": "m", "copy_pattern": str(tmp_path / "{task}" / "{arm}"), "arms": ["none", "verinoda_setup"]}
    tasks = [{"id": f"t{i}", "title": "T", "body": "B", "gold": ["a.py"]} for i in range(8)]
    big_run.set_session_limit(2)
    try:
        threads = [threading.Thread(target=big_run.one, args=(cfg, t, a)) for t in tasks for a in cfg["arms"]]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
    finally:
        big_run.set_session_limit(None)
    assert state["peak"] == 2
    state.update(now=0, peak=0)
    threads = [threading.Thread(target=big_run.one, args=(cfg, t, "none")) for t in tasks]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert state["peak"] > 2  # without a cap they run together


choose_arm = _load("choose_arm")


def _dev_rows(table, runs=1):
    """table: {task: {arm: [files named per run]}} -> a list of runs of result rows."""
    return [[{"id": t, "arm": a, "files": files[min(r, len(files) - 1)], "num_turns": 3, "seconds": 5.0, "cost_usd": 0.05,
              "input_tokens": 10, "output_tokens": 1, "verinoda_calls": 0, "graphify_calls": 0, "network": [], "answered": True}
             for t, arms in table.items() for a, files in arms.items()] for r in range(runs)]


def test_the_arm_is_chosen_by_the_summed_gain_over_both_development_sets_and_ties_go_to_fewer_features():
    tasks_a = [{"id": "a1", "gold": ["x", "y"]}, {"id": "a2", "gold": ["x", "y"]}]
    tasks_b = [{"id": "b1", "gold": ["x", "y"]}]
    # seL4-like set, two runs: none finds half, `strict` all of it, `inject` all of one task, `forced` everything (never a candidate)
    a = {"a1": {"none": [["x"], ["x"]], "strict": [["x", "y"]], "inject": [["x", "y"]], "coupled": [["x"]], "forced": [["x", "y"]]},
         "a2": {"none": [["x"], ["x"]], "strict": [["x", "y"]], "inject": [["x"]], "coupled": [["x"]], "forced": [["x", "y"]]}}
    b = {"b1": {"none": [["x"]], "strict": [["x"]], "inject": [["x", "y"]], "coupled": [["x"]], "forced": [["x", "y"]]}}
    res = choose_arm.choose([(tasks_a, _dev_rows(a, 2)), (tasks_b, _dev_rows(b, 1))])
    t = res["totals"]
    assert t["strict"] == 1.0 and t["inject"] == 1.0 and t["coupled"] == 0.0 and "forced" not in t and "none" not in t
    # an exact tie goes to the one with fewer features (inject: 1, strict: 3)
    assert res["mod_best"] == "inject" and res["mod_second"] == "strict" and res["swapped"] is False
    # strict gains 0.5 more: within 1.0 of inject, so inject (fewer features) is still first, and that is a swap
    b["b1"]["strict"] = [["x", "y"]]
    res = choose_arm.choose([(tasks_a, _dev_rows(a, 2)), (tasks_b, _dev_rows(b, 1))])
    assert res["totals"]["strict"] == 1.5 and res["totals"]["inject"] == 1.0
    assert res["mod_best"] == "inject" and res["mod_second"] == "strict" and res["swapped"] is True
    # a gap of 1.0 is not "within 1.0": the winner stays first whatever its features
    b["b1"]["inject"] = [["x"]]
    res = choose_arm.choose([(tasks_a, _dev_rows(a, 2)), (tasks_b, _dev_rows(b, 1))])
    assert res["totals"]["strict"] == 1.5 and res["totals"]["inject"] == 0.5
    assert res["mod_best"] == "strict" and res["mod_second"] == "inject" and res["swapped"] is False
    assert [r["arm"] for r in res["ranking"]][:2] == ["strict", "inject"] and res["n_tasks"] == 3


def test_only_arms_that_ran_on_every_set_are_candidates():
    tasks_a, tasks_b = [{"id": "a1", "gold": ["x", "y"]}], [{"id": "b1", "gold": ["x", "y"]}]
    a = {"a1": {"none": [["x"]], "strict": [["x", "y"]], "tool": [["x", "y"]]}}
    b = {"b1": {"none": [["x"]], "strict": [["x", "y"]]}}  # `tool` did not run on the second set
    res = choose_arm.choose([(tasks_a, _dev_rows(a)), (tasks_b, _dev_rows(b))])
    assert set(res["totals"]) == {"strict"} and res["mod_best"] == "strict" and res["mod_second"] is None


answer_checks = _load("answer_checks")


def test_answers_are_checked_for_files_that_do_not_exist_at_the_base_commit(tmp_path):
    import subprocess
    repo = tmp_path / "t1" / "none"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "src").mkdir()
    for f in ("src/a.c", "src/b.c", "README.md"):
        (repo / f).write_bytes(b"x\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "i"], cwd=repo, check=True)
    cfg = {"copy_pattern": str(tmp_path / "{task}" / "{arm}")}
    rows = [{"id": "t1", "arm": "none", "files": ["src/a.c", "src/gone.c"]},
            {"id": "t1", "arm": "none", "files": ["src/b.c"]},
            {"id": "t1", "arm": "strict", "files": ["src/a.c", "SRC/A.C", "src/new.c"]},
            {"id": "t1", "arm": "strict", "files": []}]
    out = answer_checks.check(cfg, rows)
    assert out["none"] == {"sessions": 2, "named": 3, "missing": 1, "share_missing": round(1 / 3, 3), "sessions_with_a_missing": 1}
    assert out["strict"]["named"] == 3 and out["strict"]["missing"] == 2 and out["strict"]["sessions_with_a_missing"] == 1
    assert answer_checks.tracked(repo) == {"src/a.c", "src/b.c", "README.md"}


assist_report = _load("assist_report")


def test_the_assist_report_gives_each_arm_its_gain_over_none_and_its_recall_by_number_of_gold_files():
    tasks = [{"id": "t0", "gold": ["a.py"]}, {"id": "t1", "gold": ["a.py", "b.py"]}, {"id": "t2", "gold": ["a.py", "b.py", "c.py"]}]
    a_shown = {"inject": 1, "coupled_notes": 0, "gate": 0, "nudge": 0, "locate_calls": 0, "coupled_calls": 0, "tool_search": 0}

    def row(t, arm, files, assist=None, secs=10.0, cost=0.1):
        return {"id": t, "arm": arm, "files": files, "num_turns": 4, "seconds": secs, "cost_usd": cost, "input_tokens": 100,
                "output_tokens": 10, "verinoda_calls": 0, "graphify_calls": 0, "network": [], "answered": True,
                "assist": assist or {k: 0 for k in a_shown}}
    runs = [[row("t0", "none", ["a.py"]), row("t1", "none", ["a.py"]), row("t2", "none", ["a.py"]),
             row("t0", "inject", ["a.py"], a_shown, 20.0, 0.12), row("t1", "inject", ["a.py", "b.py"], a_shown, 20.0, 0.12),
             row("t2", "inject", ["a.py", "b.py"], a_shown, 20.0, 0.12)]]
    md = assist_report.report(tasks, runs, title="a set")
    assert md.startswith("### a set")
    assert "| inject | 2.67/3 | +0.83 [" in md  # 1 + 1 + 0.67 against 1 + 0.5 + 0.33
    assert "| none | 1.83/3 |" in md
    by = assist_report.by_gold_count(tasks, runs)
    assert by["1 gold file"]["none"] == 1.0 and by["2 gold files"]["inject"] == 1.0
    assert round(by["3 or more gold files"]["none"], 3) == 0.333 and round(by["3 or more gold files"]["inject"], 3) == 0.667
    assert "| 3 or more gold files |" in md
    assert "| inject - none | +0.83 [" in md  # the pairs the decisions name, in a table of their own


def test_the_project_sentence_may_name_the_tasks_own_repository(tmp_path):
    task = {"id": "x", "title": "T", "body": "B", "repo": "fmtlib/fmt"}
    p = big_run.prompt_for(task, tmp_path, "the {repo} repository at {c} (a git checkout at the commit before the fix)")
    assert p.startswith("You are working in the fmtlib/fmt repository at ") and "(a git checkout at the commit before the fix)" in p
    assert big_run.prompt_for({"id": "x", "title": "T", "body": "B"}, tmp_path, "the repository at {c}").startswith("You are working in the repository at ")


stale_locate = _load("stale_locate")


def test_the_stale_experiment_walks_k_commits_on_from_the_base_and_reads_the_listed_paths(tmp_path):
    import subprocess

    def git(*a):
        return subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t", *a], check=True,
                              capture_output=True, text=True).stdout.strip()
    git("init", "-q")
    shas = []
    for i in range(6):
        (tmp_path / "f.txt").write_bytes(f"{i}\n".encode())
        git("add", "-A")
        git("commit", "-qm", f"c{i}")
        shas.append(git("rev-parse", "HEAD"))
    assert stale_locate.descendant(str(tmp_path), shas[1], 1) == shas[2]
    assert stale_locate.descendant(str(tmp_path), shas[1], 4) == shas[5]
    assert stale_locate.descendant(str(tmp_path), shas[1], 5) is None  # only four commits come after it
    assert stale_locate.listed({"files": [{"path": "a.c"}, {"path": "b.h"}]}) == ["a.c", "b.h"] and stale_locate.listed({}) == []


delivery_funnel = _load("delivery_funnel")


def test_the_funnel_counts_gold_files_the_mod_showed_and_the_agent_named(tmp_path):
    text = ("verinoda locate: 3 files (history: 9 commits read)\nlikely\n  src/kernel/boot.c:581-597 clock_sync_test  - matches: clock\n"
            "also check (change together with the above)\n  include/kernel/boot.h  - changed together in 3 of 4 commits\n")
    lines = [{"type": "attachment", "attachment": {"type": "hook_additional_context", "hookName": "prompt.submit",
                                                   "content": ["[Verinoda locate] Files Verinoda points to.\n\n" + text]}},
             {"type": "attachment", "attachment": {"type": "hook_additional_context", "hookName": "tool.call", "content": [
                 "[Verinoda coupled] src/a.c is usually changed together with these files:\n- src/b.c: changed together in 2 of 3 commits"]}},
             {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": "nothing to do with it"}]}}]
    t = tmp_path / "s.jsonl"
    t.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    assert delivery_funnel.shown_paths(t) == {"src/kernel/boot.c", "include/kernel/boot.h", "src/b.c"}
    tasks = [{"id": "t", "gold": ["src/kernel/boot.c", "include/kernel/boot.h", "src/arch/x86/machine.h", "src/c.c"]}]
    rows = [{"id": "t", "arm": "inject", "files": ["src/kernel/boot.c", "src/arch/x86/machine.h"]},
            {"id": "t", "arm": "none", "files": ["src/c.c"]}]
    out = delivery_funnel.funnel(tasks, rows, lambda r: t if r["arm"] == "inject" else None)
    assert out["inject"] == {"sessions": 1, "gold": 4, "shown": 2, "named": 2, "shown_and_named": 1, "shown_not_named": 1,
                             "named_not_shown": 1, "neither": 1}
    assert out["none"]["shown"] == 0 and out["none"]["named"] == 1 and out["none"]["neither"] == 3


def test_a_search_of_the_repository_for_a_github_import_path_is_not_a_network_lookup(tmp_path):
    def use(**inp):
        return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "name": "Bash", "input": inp}]}}
    lines = [use(command='grep -rn "github.com/spf13/cobra" --include=*.go .'), use(command="rg github.com/cli/cli pkg"),
             use(command="curl -s https://api.github.com/repos/cli/cli/pulls/5698"), use(command="gh pr view 5698"),
             use(command="git clone https://github.com/cli/cli.git /tmp/x"), use(command="wget -qO- https://example.org/x"),
             use(command="git log --oneline -5"), use(command="git fetch origin")]
    t = tmp_path / "s.jsonl"
    t.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    s = big_run.session_stats(t)
    assert [n.split()[0] for n in s["network"]] == ["curl", "gh", "git", "wget", "git"]


def test_the_version_of_claude_is_read_once_and_kept_with_every_session(tmp_path, monkeypatch):
    calls = []

    def fake(argv, **kw):
        calls.append(argv)
        import subprocess
        return subprocess.CompletedProcess(argv, 0, stdout="2.9.9 (Claude Code)\n", stderr="")
    monkeypatch.setattr(big_run.subprocess, "run", fake)
    monkeypatch.setattr(big_run, "_CLAUDE_VERSION", None)
    assert big_run.claude_version() == "2.9.9 (Claude Code)" and big_run.claude_version() == "2.9.9 (Claude Code)"
    assert calls == [["claude", "--version"]]
    monkeypatch.undo()
    monkeypatch.setattr(big_run, "_CLAUDE_VERSION", "2.9.9 (Claude Code)")
    monkeypatch.setattr(big_run, "run", lambda argv, cwd, env, timeout: (0, json.dumps({"session_id": "s", "usage": {}, "result": "{}"}), "", 0.1))
    monkeypatch.setattr(big_run, "find_transcript", lambda sid: None)
    row = big_run.one({"model": "m", "copy_pattern": str(tmp_path / "{task}" / "{arm}")}, {"id": "t", "title": "T", "body": "B"}, "none")
    assert row["claude"] == "2.9.9 (Claude Code)"


def test_the_arm_choice_counts_only_tasks_every_candidate_has_so_no_arm_gains_by_missing_a_task():
    tasks = [{"id": "a1", "gold": ["x", "y"]}, {"id": "a2", "gold": ["x", "y"]}]
    a = {"a1": {"none": [["x"]], "strict": [["x"]], "inject": [["x", "y"]]},
         "a2": {"none": [["x"]], "strict": [["x", "y"]]}}  # inject has no session for a2 (it failed)
    res = choose_arm.choose([(tasks, _dev_rows(a))])
    assert res["n_tasks"] == 1 and res["excluded_tasks"] == ["a2"]
    assert res["totals"] == {"inject": 0.5, "strict": 0.0}  # a2's gain of strict is not counted: inject cannot have it


drop_faults = _load("drop_faults")


def test_sessions_that_got_the_nudge_are_set_aside_for_a_rerun_and_kept_for_the_record(tmp_path):
    ok = {"id": "t1", "arm": "inject", "files": [], "assist": {"nudge": 0}}
    bad = {"id": "t2", "arm": "inject", "files": [], "assist": {"nudge": 1}}
    old = {"id": "t3", "arm": "none", "files": []}  # a result written before the counters existed
    f = tmp_path / "r.jsonl"
    f.write_text("\n".join(json.dumps(x) for x in (ok, bad, old)) + "\n", encoding="utf-8")
    assert drop_faults.main(["--count", str(f)]) == 1 and len(f.read_text(encoding="utf-8").splitlines()) == 3  # counting moves nothing
    assert drop_faults.main([str(f)]) == 0
    assert [json.loads(x)["id"] for x in f.read_text(encoding="utf-8").splitlines()] == ["t1", "t3"]
    aside = tmp_path / "r.jsonl.nudged"
    assert [json.loads(x)["id"] for x in aside.read_text(encoding="utf-8").splitlines()] == ["t2"]
    assert drop_faults.main(["--count", str(f)]) == 0
    assert drop_faults.main([str(tmp_path / "missing.jsonl")]) == 0


def test_the_assist_report_adds_the_languages_the_network_free_pairs_and_any_harness_fault():
    tasks = [{"id": "t0", "gold": ["a.py", "b.py"], "lang": "python"}, {"id": "t1", "gold": ["a.go", "b.go"], "lang": "go"}]
    zero = {"inject": 0, "coupled_notes": 0, "gate": 0, "nudge": 0, "locate_calls": 0, "coupled_calls": 0, "tool_search": 0}

    def row(t, arm, files, nudge=0, net=()):
        return {"id": t, "arm": arm, "files": files, "num_turns": 4, "seconds": 10.0, "cost_usd": 0.1, "input_tokens": 100,
                "output_tokens": 10, "verinoda_calls": 0, "graphify_calls": 0, "network": list(net), "answered": True,
                "assist": {**zero, "nudge": nudge}}
    runs = [[row("t0", "none", ["a.py"]), row("t0", "inject", ["a.py", "b.py"], nudge=1),
             row("t1", "none", ["a.go"]), row("t1", "inject", ["a.go", "b.go"], net=["curl x"])]]
    md = assist_report.report(tasks, runs, title="langs")
    assert "| python |" in md and "| go |" in md  # the mean recall by language
    assert "without the sessions that looked something up on the network" in md
    assert "1 session of the data shows the mod's nudge" in md  # a fault is said, not hidden
    clean = assist_report.report(tasks, [[row("t0", "none", ["a.py"]), row("t0", "inject", ["a.py"])]], title="clean")
    assert "nudge" not in clean
