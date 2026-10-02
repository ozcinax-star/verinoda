"""Score the big-repository study (DESIGN_BIG.md) from big_run.py's results.

Per task and arm: recall (gold files named / gold files), solved (all of them), hit@1, precision. Decisions
(pre-registered): the summed recall difference verinoda_mod - none, graphify - none and verinoda_mod - graphify with a
percentile bootstrap over tasks (10,000 resamples, seed 20261002). Cost and tool use are reported, not decided on.

    python benchmarks/agent_compare/score_big.py RESULTS.jsonl TASKS.json OUT_DIR
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from paired import RESAMPLES, SEED, boot_ratio

ARMS = ("none", "graphify", "verinoda_mod", "verinoda_setup")
DECISIONS = (("verinoda_mod", "none"), ("graphify", "none"), ("verinoda_mod", "graphify"))
SECONDARY = (("verinoda_mod", "verinoda_setup"),)


def cell(files: list[str], gold: list[str]) -> dict:
    hit = [f for f in files if f in set(gold)]
    return {"recall": len(hit) / len(gold), "solved": int(len(hit) == len(gold)),
            "hit1": int(bool(files) and files[0] in set(gold)), "precision": len(hit) / len(files) if files else 0.0,
            "named": len(files)}


def cells(results: list[dict], tasks: list[dict], skip_network: bool = False) -> dict[str, dict[str, dict]]:
    gold = {t["id"]: t["gold"] for t in tasks}
    out: dict[str, dict[str, dict]] = {}
    for r in results:
        if r["id"] not in gold or (skip_network and r.get("network")):
            continue
        out.setdefault(r["id"], {})[r["arm"]] = {
            **cell(r.get("files") or [], gold[r["id"]]), "turns": r.get("num_turns") or 0,
            "seconds": r.get("seconds") or 0.0, "cost": r.get("cost_usd") or 0.0, "input": r.get("input_tokens") or 0,
            "output": r.get("output_tokens") or 0, "tool": (r.get("verinoda_calls") or 0) + (r.get("graphify_calls") or 0),
            "network": len(r.get("network") or []), "answered": bool(r.get("answered"))}
    return out


def paired(c: dict, x: str, y: str, metric: str) -> dict:
    """``x - y`` summed over the tasks both arms have, with a percentile bootstrap over tasks."""
    ids = sorted(k for k, v in c.items() if x in v and y in v)
    d = [c[k][x][metric] - c[k][y][metric] for k in ids]
    rng = random.Random(SEED)
    sums = sorted(sum(d[rng.randrange(len(d))] for _ in d) for _ in range(RESAMPLES)) if d else [0.0]
    out = {"n": len(ids), "diff": round(sum(d), 4), "ci95": [round(sums[int(0.025 * len(sums))], 4),
                                                             round(sums[min(int(0.975 * len(sums)), len(sums) - 1)], 4)],
           "wins": sum(v > 1e-9 for v in d), "ties": sum(abs(v) <= 1e-9 for v in d), "losses": sum(v < -1e-9 for v in d)}
    for name in ("input", "output", "turns", "seconds", "cost"):
        a, b = [c[k][x][name] for k in ids], [c[k][y][name] for k in ids]
        if ids and sum(b) > 0:
            r, lo, hi = boot_ratio(a, b)
            out[name] = {"ratio": round(r, 3), "ci95": [round(lo, 3), round(hi, 3)]}
    return out


def summary(c: dict, tasks: list[dict]) -> dict:
    out = {}
    for a in ARMS:
        rows = [v[a] for v in c.values() if a in v]
        if not rows:
            continue
        n = len(rows)
        out[a] = {"tasks": n, "recall": round(sum(r["recall"] for r in rows), 2), "solved": sum(r["solved"] for r in rows),
                  "hit1": sum(r["hit1"] for r in rows), "precision": round(sum(r["precision"] for r in rows) / n, 3),
                  "sessions_using_tool": sum(r["tool"] > 0 for r in rows), "tool_calls": sum(r["tool"] for r in rows),
                  "turns": sum(r["turns"] for r in rows), "input_tokens": sum(r["input"] for r in rows),
                  "output_tokens": sum(r["output"] for r in rows), "cost_usd": round(sum(r["cost"] for r in rows), 2),
                  "seconds": round(sum(r["seconds"] for r in rows)), "network_sessions": sum(r["network"] > 0 for r in rows),
                  "unanswered": sum(not r["answered"] for r in rows)}
    return out


def report(results: list[dict], tasks: list[dict]) -> dict:
    out = {}
    for name, skip in (("all sessions", False), ("without sessions that looked something up on the network", True)):
        c = cells(results, tasks, skip)
        out[name] = {"summary": summary(c, tasks),
                     "decisions": {f"{x} - {y}": paired(c, x, y, "recall") for x, y in DECISIONS
                                   if any(x in v and y in v for v in c.values())},
                     "secondary": {f"{x} - {y}": {m: paired(c, x, y, m) for m in ("recall", "solved", "hit1")}
                                   for x, y in (*DECISIONS, *SECONDARY) if any(x in v and y in v for v in c.values())}}
    out["per_task"] = cells(results, tasks)
    return out


def main() -> int:
    res = [json.loads(x) for x in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines() if x.strip()]
    tasks = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))["tasks"]
    out = Path(sys.argv[3])
    rep = report(res, tasks)
    out.mkdir(parents=True, exist_ok=True)
    (out / "scores.json").write_bytes((json.dumps(rep, indent=1) + "\n").encode("utf-8"))
    lines = []
    for name in ("all sessions", "without sessions that looked something up on the network"):
        s = rep[name]["summary"]
        head = ("| arm | recall | solved | hit@1 | precision | sessions using the tool | turns | input tokens "
                "| output tokens | cost USD | seconds |")
        lines += [f"## {name}", "", head, "|---|---|---|---|---|---|---|---|---|---|---|"]
        lines += [f"| {a} | {v['recall']}/{v['tasks']} | {v['solved']} | {v['hit1']} | {v['precision']} | "
                  f"{v['sessions_using_tool']}/{v['tasks']} ({v['tool_calls']} calls) | {v['turns']} | {v['input_tokens']:,} "
                  f"| {v['output_tokens']:,} | {v['cost_usd']} | {v['seconds']} |" for a, v in s.items()]
        lines += ["", "| decision (recall, summed over tasks) | difference [95% CI] | W/T/L | input tokens | turns | seconds |",
                  "|---|---|---|---|---|---|"]
        for k, v in rep[name]["decisions"].items():
            cost = [f"{v[m]['ratio']} [{v[m]['ci95'][0]}-{v[m]['ci95'][1]}]" if m in v else "-" for m in ("input", "turns", "seconds")]
            lines.append(f"| {k} | {v['diff']:+.2f} [{v['ci95'][0]:+.2f} to {v['ci95'][1]:+.2f}] | "
                         f"{v['wins']}/{v['ties']}/{v['losses']} | " + " | ".join(cost) + " |")
        lines.append("")
    text = "\n".join(lines)
    (out / "summary.md").write_bytes(text.encode("utf-8"))
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
