"""Analyses of the big-repository study that were NOT pre-registered (README, "Not pre-registered"): a second replicate
of every session, the run-to-run noise floor, the two runs pooled, and what the sessions that used a tool did.

    python benchmarks/results/agent-compare-big-2026-10-02/extra.py RESULTS_R1.jsonl RESULTS_R2.jsonl
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "agent_compare"))

import score_big

ARMS = score_big.ARMS


def load(path: str) -> list[dict]:
    return [json.loads(x) for x in Path(path).read_text(encoding="utf-8").splitlines() if x.strip()]


def pool(runs: list[dict[str, dict[str, dict]]]) -> dict[str, dict[str, dict]]:
    """Per task and arm the mean of every number over the runs that have the cell."""
    out: dict[str, dict[str, dict]] = {}
    for task in set().union(*[set(r) for r in runs]):
        for arm in ARMS:
            cs = [r[task][arm] for r in runs if task in r and arm in r[task]]
            if cs:
                out.setdefault(task, {})[arm] = {k: sum(float(c[k]) for c in cs) / len(cs) for k in cs[0]}
    return out


def noise_floor(r1: dict, r2: dict, arm: str) -> dict:
    """The same arm, the same prompts, run twice: how much recall moves by chance."""
    c = {t: {"a": r1[t][arm], "b": r2[t][arm]} for t in r1 if t in r2 and arm in r1[t] and arm in r2[t]}
    return score_big.paired(c, "a", "b", "recall")


def tool_users(c: dict, arm: str) -> dict:
    users = [t for t, v in c.items() if arm in v and v[arm]["tool"] > 0]
    rest = [t for t in c if t not in users and arm in c[t]]
    total = lambda ids, a: round(sum(c[t][a]["recall"] for t in ids), 2)
    return {"sessions_using_tool": len(users), "recall_users": total(users, arm), "none_on_the_same_tasks": total(users, "none"),
            "recall_non_users": total(rest, arm), "none_on_the_non_user_tasks": total(rest, "none"), "non_users": len(rest)}


def main() -> None:
    tasks = json.loads((HERE / "tasks.json").read_text(encoding="utf-8"))["tasks"]
    r1, r2 = (score_big.cells(load(p), tasks) for p in sys.argv[1:3])
    pooled = pool([r1, r2])
    out = {"noise_floor": {a: noise_floor(r1, r2, a) for a in ARMS},
           "run1": {"summary": score_big.summary(r1, tasks)}, "run2": {"summary": score_big.summary(r2, tasks)},
           "pooled": {"decisions": {f"{x} - {y}": score_big.paired(pooled, x, y, "recall") for x, y in score_big.DECISIONS},
                      "secondary": {f"{x} - {y}": score_big.paired(pooled, x, y, "recall") for x, y in score_big.SECONDARY}},
           "tool_users": {"run1": {a: tool_users(r1, a) for a in ARMS[1:]}, "run2": {a: tool_users(r2, a) for a in ARMS[1:]}},
           "pooled_recall": {a: round(sum(v[a]["recall"] for v in pooled.values() if a in v), 2) for a in ARMS}}
    (HERE / "extra.json").write_bytes((json.dumps(out, indent=1) + "\n").encode("utf-8"))
    print("pooled recall", out["pooled_recall"])
    for a, v in out["noise_floor"].items():
        print(f"noise floor {a}: run1 - run2 = {v['diff']:+.2f} [{v['ci95'][0]:+.2f} to {v['ci95'][1]:+.2f}] W/T/L {v['wins']}/{v['ties']}/{v['losses']}")
    for k, v in out["pooled"]["decisions"].items():
        print(f"pooled {k}: {v['diff']:+.2f} [{v['ci95'][0]:+.2f} to {v['ci95'][1]:+.2f}]")
    for k, v in out["pooled"]["secondary"].items():
        print(f"pooled {k}: {v['diff']:+.2f} [{v['ci95'][0]:+.2f} to {v['ci95'][1]:+.2f}]")
    for run in ("run1", "run2"):
        for a, v in out["tool_users"][run].items():
            print(run, a, v)


if __name__ == "__main__":
    main()
