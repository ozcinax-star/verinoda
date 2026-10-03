"""Analyses of the stale-index study that were NOT pre-registered (README, "Not pre-registered").

- by whether `analyze` refreshed the stale index itself during the sessions: it refreshes projects under 300 files
  (`analysis.REFRESH_SMALL_PROJECT`) and declines larger ones when over 200 files changed or the rebuild is estimated
  over 15 s; the split is the index state measured after the sessions, not the results;
- run 1, the incomplete first run (126 of 150 sessions; 24 lost to a network outage), as a repeat of the same cells.

    python benchmarks/results/agent-compare-stale-2026-10-02/extra.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "agent_compare"))

import score_stale

# freshness.check of each `stale` copy after the sessions: 0 where analyze had refreshed it
STAYED_STALE = {"axios__axios": 202, "fastapi__sqlmodel": 217, "google__gson": 70, "Graphify-Labs__graphify": 157,
                "sharkdp__bat": 83}
REFRESHED = {"expressjs__express": 77, "fastapi__full-stack-fastapi-template": 133, "gin-gonic__gin": 57,
             "guzzle__guzzle": 94, "junegunn__fzf": 67}
PAIRS = (("verinoda_fresh", "verinoda_stale"), ("verinoda_stale", "none"), ("verinoda_fresh", "none"))


def load(name: str):
    return json.loads((HERE / name).read_text(encoding="utf-8"))


def groups(per_question: dict, use: dict) -> dict:
    out = {}
    for name, repos in (("stayed_stale", STAYED_STALE), ("refreshed_by_analyze", REFRESHED)):
        cells = {k: v for k, v in per_question.items() if k.split("/")[0] in repos}
        arms = ("none", "verinoda_stale", "verinoda_fresh")
        out[name] = {"repos": sorted(repos), "questions": len(cells),
                     "found": {a: f"{sum(c[a]['found'] for c in cells.values() if a in c)}/"
                                  f"{sum(c[a]['total'] for c in cells.values() if a in c)}" for a in arms},
                     "pairs": {f"{x}-{y}": score_stale.paired(cells, x, y, "found", use) for x, y in PAIRS}}
    return out


def main() -> None:
    questions, tree = load("questions.json"), load("tree_new.json")
    run2 = score_stale.score(load("sessions.json"), questions, tree)
    run1 = score_stale.score(load("sessions_run1.json"), questions, tree)
    same = [run1["per_question"][k][a]["found"] == run2["per_question"][k][a]["found"]
            for k in run1["per_question"] for a in run1["per_question"][k]]
    out = {"run2": groups(run2["per_question"], load("usage.json")),
           "run1": {"overall": run1["overall"], **groups(run1["per_question"], load("usage_run1.json"))},
           "repeat_cells_same_facts": f"{sum(same)}/{len(same)}"}
    (HERE / "extra.json").write_bytes((json.dumps(out, indent=1) + "\n").encode("utf-8"))
    for run in ("run2", "run1"):
        for g in ("stayed_stale", "refreshed_by_analyze"):
            d = out[run][g]
            print(run, g, d["found"])
            for k, p in d["pairs"].items():
                print(f"   {k}: n={p['n']} facts {p['diff']:+d} {p['ci95']}; input {p['input']['ratio']} "
                      f"{p['input']['ci95']}; output {p['output']['ratio']} {p['output']['ci95']}; "
                      f"tool calls {p['tool_calls']['ratio']} {p['tool_calls']['ci95']}")
    print("repeat cells with the same facts:", out["repeat_cells_same_facts"])


if __name__ == "__main__":
    main()
