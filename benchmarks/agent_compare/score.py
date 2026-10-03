"""Score the agent comparison (DESIGN.md): every session's answer against its set's gold with the benchmark's own
``score_facts``; totals per arm, paired per question, per set and out of sample.

    python benchmarks/agent_compare/score.py SESSIONS.json GOLD.json QUESTIONS.json OUT_DIR
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from verinoda.benchmark.metrics import count_tokens, score_facts

ARMS: tuple[str, ...] = ("none", "verinoda", "graphify")  # set from the sessions, in this order of preference
PAIRS: tuple[tuple[str, str], ...] = ()


def arms_of(sessions: list[dict]) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...]]:
    """The arms in the sessions (``none`` first) and the pairs: every tool arm against ``none``, then the tool arms
    against each other."""
    seen = list(dict.fromkeys(s["arm"] for s in sessions))
    arms = tuple(["none"] * ("none" in seen) + [a for a in seen if a != "none"])
    tools = [a for a in arms if a != "none"]
    pairs = [(a, "none") for a in tools if "none" in arms]
    pairs += [(a, b) for i, a in enumerate(tools) for b in tools[i + 1:]]
    return arms, tuple(pairs)


def score(sessions: list[dict], gold: dict, questions: list[dict]) -> dict:
    global ARMS, PAIRS
    ARMS, PAIRS = arms_of(sessions)
    meta = {f"{q['set']}/{q['id']}": q for q in questions}
    cells: dict[str, dict[str, dict]] = defaultdict(dict)
    for s in sessions:
        key = f"{s['set']}/{s['id']}"
        facts = gold[key]
        answer = "" if s.get("failed") else s.get("answer", "")
        sc = score_facts(facts, answer)
        cells[key][s["arm"]] = {
            "found": sc["found_n"], "pinpointed": sc["pinpointed_n"], "shown": sc["shown_n"], "total": sc["total"],
            "found_ids": sc["found"], "tokens": count_tokens(answer), "failed": bool(s.get("failed")),
            "tool_calls": s.get("tool_calls"), "index_tool_calls": s.get("index_tool_calls"),
            "stayed_inside": s.get("stayed_inside"), "used_forbidden_tool": s.get("used_forbidden_tool"),
        }

    def totals(keys: list[str]) -> dict:
        out = {}
        for arm in ARMS:
            rows = [cells[k][arm] for k in keys if arm in cells[k]]
            n = len(rows)
            out[arm] = {
                "sessions": n, "failed": sum(r["failed"] for r in rows),
                "found": sum(r["found"] for r in rows), "pinpointed": sum(r["pinpointed"] for r in rows),
                "facts": sum(r["total"] for r in rows),
                "all_facts_questions": sum(1 for r in rows if r["found"] == r["total"]),
                "tokens_mean": round(sum(r["tokens"] for r in rows) / n) if n else None,
                "tool_calls_mean": round(sum(r["tool_calls"] or 0 for r in rows) / n, 1) if n else None,
                "index_calls_mean": round(sum(r["index_tool_calls"] or 0 for r in rows) / n, 1) if n else None,
                "used_index_tool": sum(1 for r in rows if (r["index_tool_calls"] or 0) > 0),
                "reported_outside": sum(1 for r in rows if r["stayed_inside"] is False),
                "reported_forbidden": sum(1 for r in rows if r["used_forbidden_tool"]),
            }
        pairs = {}
        for a, b in PAIRS:
            diffs = [cells[k][a]["found"] - cells[k][b]["found"] for k in keys if a in cells[k] and b in cells[k]]
            pairs[f"{a}-{b}"] = {"wins": sum(d > 0 for d in diffs), "ties": sum(d == 0 for d in diffs),
                                 "losses": sum(d < 0 for d in diffs), "sum_diff": sum(diffs), "n": len(diffs)}
        return {"arms": out, "pairs": pairs}

    keys = sorted(cells)
    by_set = defaultdict(list)
    for k in keys:
        by_set[k.split("/")[0]].append(k)
    oos = [k for k in keys if not meta[k].get("in_sample")]
    return {
        "overall": totals(keys),
        "out_of_sample": totals(oos),
        "in_sample": totals([k for k in keys if meta[k].get("in_sample")]),
        "by_set": {s: totals(ks) for s, ks in by_set.items()},
        "per_question": {k: cells[k] for k in keys},
    }


def markdown(res: dict) -> str:
    lines = []
    for title, block in (("All 57 questions", res["overall"]), ("Out of sample (glow, forge, heldout)",
                                                                res["out_of_sample"]),
                         ("In sample (graphify_core, verinoda_user_tr)", res["in_sample"])):
        header = ("| arm | facts found | pinpointed | all-facts questions | answer tokens (mean) "
                  "| tool calls (mean, self-reported) | sessions using the index tool (self-reported) |")
        lines += [f"### {title}", "", header, "|---|---|---|---|---|---|---|"]
        for arm, a in block["arms"].items():
            lines.append(f"| {arm} | {a['found']}/{a['facts']} | {a['pinpointed']} | {a['all_facts_questions']}/"
                         f"{a['sessions']} | {a['tokens_mean']} | {a['tool_calls_mean']} | "
                         f"{a['used_index_tool'] if arm != 'none' else '-'} |")
        lines += ["", "| pair | wins | ties | losses | facts difference |", "|---|---|---|---|---|"]
        for p, d in block["pairs"].items():
            lines.append(f"| {p} | {d['wins']} | {d['ties']} | {d['losses']} | {d['sum_diff']:+d} |")
        lines.append("")
    lines += ["### Per set (facts found)", "", "| set | " + " | ".join(ARMS) + " |", "|---" * (len(ARMS) + 1) + "|"]
    for s, block in res["by_set"].items():
        a = block["arms"]
        facts = next(iter(a.values()))["facts"]
        lines.append(f"| {s} ({facts} facts) | " + " | ".join(str(a[x]["found"]) for x in ARMS) + " |")
    return "\n".join(lines) + "\n"


def main() -> int:
    sessions_p, gold_p, questions_p, out = (Path(x) for x in sys.argv[1:5])
    sessions = json.loads(sessions_p.read_text(encoding="utf-8"))
    gold = json.loads(gold_p.read_text(encoding="utf-8"))
    questions = json.loads(questions_p.read_text(encoding="utf-8"))
    res = score(sessions, gold, questions)
    out.mkdir(parents=True, exist_ok=True)
    (out / "scores.json").write_text(json.dumps(res, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    (out / "summary.md").write_text(markdown(res), encoding="utf-8")
    print(markdown(res))
    return 0


if __name__ == "__main__":
    sys.exit(main())
