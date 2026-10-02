"""The agent comparison's scoring scripts (benchmarks/agent_compare): arms and pairs from the sessions, totals, the
transcript reader's tool and token accounting and leak check, and the paired bootstrap."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

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
