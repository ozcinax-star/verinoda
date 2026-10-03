"""The rule of DESIGN_ASSIST.md that picks the mod arm from the development sets, applied as written.

Among the mod arms that ran on every development set, ``mod_best`` is the one with the largest recall difference from
`none` summed over all the development tasks (a task's mean over its runs); ``mod_second`` is the next. When the two are
within 1.0 recall of each other, the one with fewer features is first (inject, coupled, gate, tool: 1; passive, full: 2;
passive_gate, strict: 3). `forced` is a diagnostic and never a candidate.

    python benchmarks/agent_compare/choose_arm.py OUT.json TASKS.json:RUN1.jsonl,RUN2.jsonl [TASKS.json:RUN.jsonl ...]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from score_big import cells, pool

FEATURES = {"inject": 1, "coupled": 1, "gate": 1, "tool": 1, "passive": 2, "full": 2, "passive_gate": 3, "strict": 3}
NOT_CANDIDATES = {"none", "graphify", "verinoda_setup", "verinoda_mod", "forced"}
TIE = 1.0


def choose(sets: list[tuple[list[dict], list[list[dict]]]]) -> dict:
    """``sets``: per development set its tasks and its runs (each a list of result rows). Only tasks that `none` and every
    candidate arm have a session for are counted (an arm with a failed session is not helped by having fewer tasks)."""
    pooled_sets = [pool([cells(r, tasks) for r in runs]) for tasks, runs in sets]
    ran = [{a for v in pooled.values() for a in v} - NOT_CANDIDATES for pooled in pooled_sets]
    eligible = set.intersection(*ran) if ran else set()
    totals = dict.fromkeys(sorted(eligible), 0.0)
    n_tasks = 0
    excluded: list[str] = []
    for pooled in pooled_sets:
        for task, v in pooled.items():
            if "none" not in v or not eligible <= set(v):
                excluded.append(task)
                continue
            n_tasks += 1
            for arm in eligible:
                totals[arm] += v[arm]["recall"] - v["none"]["recall"]
    totals = {a: round(g, 4) for a, g in totals.items()}
    ranking = [{"arm": a, "gain": g, "features": FEATURES.get(a)}
               for a, g in sorted(totals.items(), key=lambda kv: (-kv[1], FEATURES.get(kv[0], 9), kv[0]))]
    best = ranking[0]["arm"] if ranking else None
    second = ranking[1]["arm"] if len(ranking) > 1 else None
    swapped = False
    if best and second and ranking[0]["gain"] - ranking[1]["gain"] < TIE and FEATURES[second] < FEATURES[best]:
        best, second, swapped = second, best, True
    return {"totals": totals, "ranking": ranking, "mod_best": best, "mod_second": second, "swapped": swapped,
            "within": TIE, "n_tasks": n_tasks, "excluded_tasks": sorted(excluded)}


def main() -> int:
    out, specs = Path(sys.argv[1]), sys.argv[2:]
    sets = []
    for spec in specs:
        tasks_path, _, runs = spec.partition(":")
        tasks = json.loads(Path(tasks_path).read_text(encoding="utf-8"))["tasks"]
        rows = [[json.loads(x) for x in Path(p).read_text(encoding="utf-8").splitlines() if x.strip()] for p in runs.split(",")]
        sets.append((tasks, rows))
    res = choose(sets)
    out.write_bytes((json.dumps(res, indent=1) + "\n").encode("utf-8"))
    print(json.dumps({k: res[k] for k in ("ranking", "mod_best", "mod_second", "swapped", "n_tasks")}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
