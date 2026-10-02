"""Score the check-after-edits study (DESIGN_GUARD.md) from guard_run.py's results.

Decisions (pre-registered): `guard - plain` hidden-test passes and `plain - guard` absent sites left, each summed
over tasks with a percentile bootstrap over tasks. Also reported, not decided on: by repository, by whether the
prompt names a helper (`prompt_names_helpers`), notes the check gave, turns and cost.

    python benchmarks/agent_compare/score_guard.py RESULTS.jsonl TASKS.json OUT_DIR
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from score_stale import paired

ARMS = ("plain", "guard")


def cells(results: list[dict], tasks: list[dict]) -> dict[str, dict[str, dict]]:
    """``{task id: {arm: {passed, bad, notes, turns, cost}}}`` for the tasks both arms ran; a session whose check
    could not read the result counts its absent sites as unknown (left out of that pair)."""
    ids = {t["id"] for t in tasks}
    out: dict[str, dict[str, dict]] = {}
    for r in results:
        if r["id"] not in ids:
            continue
        out.setdefault(r["id"], {})[r["arm"]] = {
            "passed": int(bool(r["passed"])), "bad": None if r["check_bad_sites"] is None else len(r["check_bad_sites"]),
            "notes": r.get("notes") or 0, "turns": r.get("num_turns") or 0, "cost": r.get("cost_usd") or 0.0,
            "seconds": r.get("seconds") or 0.0}
    return {k: v for k, v in out.items() if all(a in v for a in ARMS)}


def summary(c: dict[str, dict[str, dict]]) -> dict:
    out = {}
    for a in ARMS:
        rows = [v[a] for v in c.values()]
        out[a] = {"tasks": len(rows), "passed": sum(r["passed"] for r in rows),
                  "absent_sites": sum(r["bad"] or 0 for r in rows),
                  "sessions_leaving_absent_sites": sum(bool(r["bad"]) for r in rows),
                  "notes": sum(r["notes"] for r in rows), "sessions_with_notes": sum(r["notes"] > 0 for r in rows),
                  "turns": sum(r["turns"] for r in rows), "cost_usd": round(sum(r["cost"] for r in rows), 2),
                  "seconds": round(sum(r["seconds"] for r in rows))}
    return out


def decide(c: dict[str, dict[str, dict]]) -> dict:
    known = {k: v for k, v in c.items() if all(v[a]["bad"] is not None for a in ARMS)}
    return {"passes guard-plain": paired(c, "guard", "plain", "passed"),
            "absent sites plain-guard": paired(known, "plain", "guard", "bad"),
            "turns guard-plain": paired(c, "guard", "plain", "turns")}


def main() -> int:
    res = [json.loads(x) for x in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines() if x.strip()]
    tasks = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
    out = Path(sys.argv[3])
    c = cells(res, tasks)
    by = {t["id"]: t for t in tasks}
    groups = {"all": c,
              "graphify": {k: v for k, v in c.items() if by[k]["repo"] == "graphify"},
              "sqlmodel": {k: v for k, v in c.items() if by[k]["repo"] == "sqlmodel"},
              "prompt names a helper": {k: v for k, v in c.items() if by[k].get("prompt_names_helpers")},
              "prompt names no helper": {k: v for k, v in c.items() if not by[k].get("prompt_names_helpers")}}
    report = {"groups": {g: {"summary": summary(v), "pairs": decide(v)} for g, v in groups.items() if v},
              "per_task": c}
    out.mkdir(parents=True, exist_ok=True)
    (out / "scores.json").write_bytes((json.dumps(report, indent=1) + "\n").encode("utf-8"))
    lines = []
    for g, d in report["groups"].items():
        s, p = d["summary"], d["pairs"]
        lines += [f"## {g} ({s['plain']['tasks']} tasks)", "",
                  "| arm | passed | absent sites left (sessions) | check notes (sessions) | turns | cost USD |",
                  "|---|---|---|---|---|---|"]
        lines += [f"| {a} | {s[a]['passed']}/{s[a]['tasks']} | {s[a]['absent_sites']} ({s[a]['sessions_leaving_absent_sites']}) "
                  f"| {s[a]['notes']} ({s[a]['sessions_with_notes']}) | {s[a]['turns']} | {s[a]['cost_usd']} |" for a in ARMS]
        lines += ["", "| pair | difference [95% CI] | W/T/L |", "|---|---|---|"]
        lines += [f"| {k} | {v['diff']:+d} [{v['ci95'][0]:+d} to {v['ci95'][1]:+d}] | {v['wins']}/{v['ties']}/{v['losses']} |"
                  for k, v in p.items()]
        lines.append("")
    text = "\n".join(lines)
    (out / "summary.md").write_bytes(text.encode("utf-8"))
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
