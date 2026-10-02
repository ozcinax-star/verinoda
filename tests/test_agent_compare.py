"""The agent comparison's scoring scripts (benchmarks/agent_compare): arms and pairs from the sessions, totals, the
transcript reader's tool and token accounting and leak check, and the paired bootstrap."""

from __future__ import annotations

import importlib.util
import json
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
