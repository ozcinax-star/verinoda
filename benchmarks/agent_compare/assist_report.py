"""The tables of an assist study (DESIGN_ASSIST.md) from its results: per arm the recall (summed over tasks, a task's mean
over its runs), the gain over `none` with a paired bootstrap, how often the mod put something in front of the model and
the tool was called, the time and cost of a session; and the recall by the number of gold files.

    python benchmarks/agent_compare/assist_report.py TASKS.json RUN1.jsonl[,RUN2.jsonl ...] OUT.md "title"
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from score_big import arms_of, cells, decisions_for, paired, pool, secondary_for

STRATA = (("1 gold file", lambda n: n == 1), ("2 gold files", lambda n: n == 2), ("3 or more gold files", lambda n: n >= 3))


def by_gold_count(tasks: list[dict], runs: list[list[dict]]) -> dict[str, dict[str, float]]:
    """Per stratum of the number of gold files, each arm's mean recall (a task's mean over its runs)."""
    pooled = pool([cells(r, tasks) for r in runs])
    size = {t["id"]: len(t["gold"]) for t in tasks}
    out: dict[str, dict[str, float]] = {}
    for name, test in STRATA:
        ids = [t for t in pooled if test(size[t])]
        arms = {a for t in ids for a in pooled[t]}
        out[name] = {a: statistics.mean(pooled[t][a]["recall"] for t in ids if a in pooled[t]) for a in sorted(arms)}
    return {k: v for k, v in out.items() if v}


def by_language(tasks: list[dict], runs: list[list[dict]]) -> dict[str, dict[str, float]]:
    """Per language of the task (when the tasks have one), each arm's mean recall (a task's mean over its runs)."""
    pooled = pool([cells(r, tasks) for r in runs])
    lang = {t["id"]: t["lang"] for t in tasks if t.get("lang")}
    out: dict[str, dict[str, float]] = {}
    for name in sorted(set(lang.values())):
        ids = [t for t in pooled if lang.get(t) == name]
        arms = {a for t in ids for a in pooled[t]}
        out[name] = {a: statistics.mean(pooled[t][a]["recall"] for t in ids if a in pooled[t]) for a in sorted(arms)}
    return out


def pair_table(pooled: dict, arms: tuple[str, ...], heading: str) -> list[str]:
    pairs = [(x, y) for x, y in (*decisions_for(arms), *secondary_for(arms)) if any(x in v and y in v for v in pooled.values())]
    if not pairs:
        return []
    lines = ["", heading, "", "| pair (recall, summed over tasks) | difference [95 % CI] | W/T/L |", "|---|---|---|"]
    for x, y in dict.fromkeys(pairs):
        d = paired(pooled, x, y, "recall")
        lines.append(f"| {x} - {y} | {d['diff']:+.2f} [{d['ci95'][0]:+.2f} to {d['ci95'][1]:+.2f}] | {d['wins']}/{d['ties']}/{d['losses']} |")
    return lines


def report(tasks: list[dict], runs: list[list[dict]], title: str) -> str:
    per_run = [cells(r, tasks) for r in runs]
    pooled = pool(per_run)
    arms = arms_of(pooled)
    n_tasks = len(pooled)
    rows = [row for r in runs for row in r if row["id"] in {t["id"] for t in tasks}]
    lines = [f"### {title}", "",
             f"{n_tasks} tasks, {len(runs)} run{'s' if len(runs) != 1 else ''}; recall is summed over tasks (a task's mean over its runs).", "",
             ("| arm | recall | gain over `none` [95 % CI] | W/T/L | solved | hit@1 | sessions where the mod put something in "
              "front of the model | sessions that called its tool | sessions that used ToolSearch | median seconds | cost per session |"),
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for arm in arms:
        mine = [r for r in rows if r["arm"] == arm]
        a = [r.get("assist") or {} for r in mine]
        shown = sum(x.get("inject", 0) + x.get("coupled_notes", 0) + x.get("gate", 0) > 0 for x in a)
        called = sum(x.get("locate_calls", 0) + x.get("coupled_calls", 0) > 0 for x in a)
        searched = sum(x.get("tool_search", 0) > 0 for x in a)
        have = [pooled[t][arm] for t in pooled if arm in pooled[t]]
        recall = sum(c["recall"] for c in have)
        gain = "-"
        wtl = "-"
        if arm != "none" and any(arm in v and "none" in v for v in pooled.values()):
            d = paired(pooled, arm, "none", "recall")
            gain = f"{d['diff']:+.2f} [{d['ci95'][0]:+.2f} to {d['ci95'][1]:+.2f}]"
            wtl = f"{d['wins']}/{d['ties']}/{d['losses']}"
        secs = sorted(r["seconds"] or 0.0 for r in mine)
        cost = statistics.mean(r["cost_usd"] or 0.0 for r in mine) if mine else 0.0
        lines.append(f"| {arm} | {recall:.2f}/{len(have)} | {gain} | {wtl} | {sum(c['solved'] for c in have):.1f} | "
                     f"{sum(c['hit1'] for c in have):.1f} | {shown}/{len(mine)} | {called}/{len(mine)} | {searched}/{len(mine)} | "
                     f"{secs[len(secs) // 2]:.0f} | ${cost:.3f} |" if secs else f"| {arm} | - |")
    lines += pair_table(pooled, arms, "Pairs:")
    quiet = pool([cells(r, tasks, skip_network=True) for r in runs])
    if any(row.get("network") for row in rows):
        lines += pair_table(quiet, arms_of(quiet), "The same pairs without the sessions that looked something up on the network:")
    faults = sum(bool((row.get("assist") or {}).get("nudge")) for row in rows)
    if faults:
        lines += ["", (f"**{faults} session{'s' if faults != 1 else ''} of the data shows the mod's nudge although no arm asks for it** "
                       "(a harness fault: `drop_faults.py` sets such sessions aside for a rerun).")]
    strata = by_gold_count(tasks, runs)
    if strata:
        names = [a for a in arms if any(a in v for v in strata.values())]
        lines += ["", "Mean recall by the number of gold files (what is left for a tool to add is where `none` is low):", "",
                  "| tasks | " + " | ".join(names) + " |", "|---" * (1 + len(names)) + "|"]
        for name, v in strata.items():
            lines.append(f"| {name} | " + " | ".join(f"{v[a]:.2f}" if a in v else "-" for a in names) + " |")
    langs = by_language(tasks, runs)
    if langs:
        names = [a for a in arms if any(a in v for v in langs.values())]
        lines += ["", "Mean recall by language:", "", "| language | " + " | ".join(names) + " |", "|---" * (1 + len(names)) + "|"]
        for name, v in langs.items():
            lines.append(f"| {name} | " + " | ".join(f"{v[a]:.2f}" if a in v else "-" for a in names) + " |")
    return "\n".join(lines) + "\n"


def main() -> int:
    tasks = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))["tasks"]
    runs = [[json.loads(x) for x in Path(p).read_text(encoding="utf-8").splitlines() if x.strip()] for p in sys.argv[2].split(",")]
    text = report(tasks, runs, sys.argv[4] if len(sys.argv) > 4 else "results")
    Path(sys.argv[3]).write_bytes(text.encode("utf-8"))
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
