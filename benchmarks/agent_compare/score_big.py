"""Score the big-repository study (DESIGN_BIG.md) from big_run.py's results.

Per task and arm: recall (gold files named / gold files), solved (all of them), hit@1, precision. Decisions
(pre-registered): the summed recall difference verinoda_mod - none, graphify - none and verinoda_mod - graphify with a
percentile bootstrap over tasks (10,000 resamples, seed 20261002). Cost and tool use are reported, not decided on.

    python benchmarks/agent_compare/score_big.py RESULTS.jsonl TASKS.json OUT_DIR
    python benchmarks/agent_compare/score_big.py RESULTS_1.jsonl,RESULTS_2.jsonl TASKS.json OUT_DIR  (mean of runs)
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


def arms_of(c: dict[str, dict[str, dict]]) -> tuple[str, ...]:
    """The arms with a cell: the known ones in their order, any other after them in the order it first appears."""
    present = [a for v in c.values() for a in v]
    return (*[a for a in ARMS if a in present], *dict.fromkeys(a for a in present if a not in ARMS))


def decisions_for(arms: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    """The pre-registered decisions, and each arm added through `arm_defs` against `none`."""
    return (*DECISIONS, *((a, "none") for a in arms if a not in ARMS and "none" in arms))


def secondary_for(arms: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    """The secondary pairs, and each added arm against the Verinoda setup without the mod."""
    return (*SECONDARY, *((a, "verinoda_setup") for a in arms if a not in ARMS and "verinoda_setup" in arms))


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
        a = r.get("assist") or {}  # the mod's assist features: what was put in front of the model, and the tools it called
        out.setdefault(r["id"], {})[r["arm"]] = {
            **cell(r.get("files") or [], gold[r["id"]]), "turns": r.get("num_turns") or 0,
            "assist_shown": sum(a.get(k, 0) for k in ("inject", "coupled_notes", "gate")),
            "assist_called": sum(a.get(k, 0) for k in ("locate_calls", "coupled_calls")), "tool_search": a.get("tool_search", 0),
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
    for a in arms_of(c):
        rows = [v[a] for v in c.values() if a in v]
        n = len(rows)
        out[a] = {"tasks": n, "recall": round(sum(r["recall"] for r in rows), 2), "solved": sum(r["solved"] for r in rows),
                  "hit1": sum(r["hit1"] for r in rows), "precision": round(sum(r["precision"] for r in rows) / n, 3),
                  "sessions_using_tool": sum(r["tool"] > 0 for r in rows), "tool_calls": sum(r["tool"] for r in rows),
                  "sessions_assist_shown": sum(r["assist_shown"] > 0 for r in rows),
                  "sessions_assist_called": sum(r["assist_called"] > 0 for r in rows),
                  "sessions_tool_search": sum(r["tool_search"] > 0 for r in rows),
                  "turns": sum(r["turns"] for r in rows), "input_tokens": sum(r["input"] for r in rows),
                  "output_tokens": sum(r["output"] for r in rows), "cost_usd": round(sum(r["cost"] for r in rows), 2),
                  "seconds": round(sum(r["seconds"] for r in rows)), "network_sessions": sum(r["network"] > 0 for r in rows),
                  "unanswered": sum(not r["answered"] for r in rows)}
    return out


def report(results: list[dict], tasks: list[dict]) -> dict:
    out = {}
    for name, skip in (("all sessions", False), ("without sessions that looked something up on the network", True)):
        c = cells(results, tasks, skip)
        arms = arms_of(c)
        decisions = decisions_for(arms)
        out[name] = {"summary": summary(c, tasks),
                     "decisions": {f"{x} - {y}": paired(c, x, y, "recall") for x, y in decisions
                                   if any(x in v and y in v for v in c.values())},
                     "secondary": {f"{x} - {y}": {m: paired(c, x, y, m) for m in ("recall", "solved", "hit1")}
                                   for x, y in (*decisions, *secondary_for(arms)) if any(x in v and y in v for v in c.values())}}
    out["per_task"] = cells(results, tasks)
    return out


def pool(runs: list[dict[str, dict[str, dict]]]) -> dict[str, dict[str, dict]]:
    """Per task and arm the mean of every number over the runs that have the cell."""
    out: dict[str, dict[str, dict]] = {}
    for task in sorted(set().union(*[set(r) for r in runs])):
        for arm in sorted({a for r in runs if task in r for a in r[task]}):
            cs = [r[task][arm] for r in runs if task in r and arm in r[task]]
            if cs:
                out.setdefault(task, {})[arm] = {k: sum(float(c[k]) for c in cs) / len(cs) for k in cs[0]}
    return out


def report_pooled(runs: list[list[dict]], tasks: list[dict]) -> dict:
    """The decisions on the mean of several runs of the same sessions, each run's own summary, and, per arm, the
    run-to-run difference of every pair of runs (the noise floor)."""
    per_run = [cells(r, tasks) for r in runs]
    pooled = pool(per_run)
    noise: dict[str, list] = {}
    arms = arms_of(pooled)
    for arm in arms:
        for i in range(len(per_run)):
            for j in range(i + 1, len(per_run)):
                c = {t: {"a": per_run[i][t][arm], "b": per_run[j][t][arm]} for t in per_run[i]
                     if t in per_run[j] and arm in per_run[i][t] and arm in per_run[j][t]}
                if c:
                    noise.setdefault(arm, []).append({"runs": [i + 1, j + 1], **paired(c, "a", "b", "recall")})
    has = lambda x, y: any(x in v and y in v for v in pooled.values())
    decisions = decisions_for(arms)
    return {"pooled": {"summary": summary(pooled, tasks),
                       "decisions": {f"{x} - {y}": paired(pooled, x, y, "recall") for x, y in decisions if has(x, y)},
                       "secondary": {f"{x} - {y}": {m: paired(pooled, x, y, m) for m in ("recall", "solved", "hit1")}
                                     for x, y in (*decisions, *secondary_for(arms)) if has(x, y)}},
            "runs": [{"summary": summary(c, tasks)} for c in per_run], "noise_floor": noise}


def main_pooled(paths: list[str], tasks: list[dict], out: Path) -> int:
    runs = [[json.loads(x) for x in Path(p).read_text(encoding="utf-8").splitlines() if x.strip()] for p in paths]
    rep = report_pooled(runs, tasks)
    out.mkdir(parents=True, exist_ok=True)
    (out / "scores.json").write_bytes((json.dumps(rep, indent=1) + "\n").encode("utf-8"))
    s = rep["pooled"]["summary"]
    head = ("| arm | recall | solved | hit@1 | precision | tasks where the tool was used | tasks where the mod put something in "
            "front of the model / called its tool | turns | input tokens | output tokens | cost USD | seconds |")
    lines = [f"## the mean of {len(runs)} runs", "", head, "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for a, v in s.items():
        lines.append(f"| {a} | {v['recall']}/{v['tasks']} | {v['solved']:.1f} | {v['hit1']:.1f} | {v['precision']} | "
                     f"{v['sessions_using_tool']}/{v['tasks']} | {v['sessions_assist_shown']} / {v['sessions_assist_called']} | "
                     f"{v['turns']:.0f} | {v['input_tokens']:,.0f} | {v['output_tokens']:,.0f} | {v['cost_usd']} | {v['seconds']} |")
    lines += ["", "| decision (recall, summed over tasks, mean of the runs) | difference [95% CI] | W/T/L |", "|---|---|---|"]
    for k, v in rep["pooled"]["decisions"].items():
        lines.append(f"| {k} | {v['diff']:+.2f} [{v['ci95'][0]:+.2f} to {v['ci95'][1]:+.2f}] | "
                     f"{v['wins']}/{v['ties']}/{v['losses']} |")
    lines += ["", "| each run: recall / sessions that used the tool | " + " | ".join(s) + " |", "|---" * (1 + len(s)) + "|"]
    for i, r in enumerate(rep["runs"], 1):
        lines.append(f"| run {i} | " + " | ".join(f"{v['recall']} / {v['sessions_using_tool']}" for v in r["summary"].values()) + " |")
    lines += ["", "| noise floor: the same arm, run i - run j | difference [95% CI] |", "|---|---|"]
    for a, lst in rep["noise_floor"].items():
        lines += [f"| {a}, runs {x['runs'][0]}-{x['runs'][1]} | {x['diff']:+.2f} [{x['ci95'][0]:+.2f} to {x['ci95'][1]:+.2f}] |"
                  for x in lst]
    text = "\n".join(lines) + "\n"
    (out / "summary.md").write_bytes(text.encode("utf-8"))
    print(text)
    return 0


def main() -> int:
    tasks = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))["tasks"]
    out = Path(sys.argv[3])
    if "," in sys.argv[1]:  # several runs of the same sessions: RESULTS_1,RESULTS_2,...
        return main_pooled(sys.argv[1].split(","), tasks, out)
    res = [json.loads(x) for x in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines() if x.strip()]
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
