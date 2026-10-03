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
    """``sets``: per development set its tasks and its runs (each a list of result rows)."""
    totals: dict[str, float] = {}
    ran: list[set[str]] = []
    n_tasks = 0
    for tasks, runs in sets:
        pooled = pool([cells(r, tasks) for r in runs])
        n_tasks += len(pooled)
        ran.append({a for v in pooled.values() for a in v} - NOT_CANDIDATES)
        for v in pooled.values():
            if "none" not in v:
                continue
            for arm, cell in v.items():
                if arm not in NOT_CANDIDATES:
                    totals[arm] = totals.get(arm, 0.0) + cell["recall"] - v["none"]["recall"]
    eligible = set.intersection(*ran) if ran else set()
    totals = {a: round(totals[a], 4) for a in sorted(eligible)}
    ranking = [{"arm": a, "gain": g, "features": FEATURES.get(a)} for a, g in sorted(totals.items(), key=lambda kv: (-kv[1], FEATURES.get(kv[0], 9), kv[0]))]
    best = ranking[0]["arm"] if ranking else None
    second = ranking[1]["arm"] if len(ranking) > 1 else None
    swapped = False
    if best and second and ranking[0]["gain"] - ranking[1]["gain"] < TIE and FEATURES[second] < FEATURES[best]:
        best, second, swapped = second, best, True
    return {"totals": totals, "ranking": ranking, "mod_best": best, "mod_second": second, "swapped": swapped,
            "within": TIE, "n_tasks": n_tasks}


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
