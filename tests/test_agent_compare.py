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
