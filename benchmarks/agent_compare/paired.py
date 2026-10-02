"""Paired per-question comparisons across the two rounds of the agent comparison: facts found and model tokens, each
tool arm against round 2's ``none``, and round 1's ``none`` against round 2's ``none`` as the noise floor. The
interval is a percentile bootstrap over questions (10,000 resamples, fixed seed) of the ratio of summed tokens.

    python benchmarks/agent_compare/paired.py RESULTS_DIR
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

SEED, RESAMPLES = 20261002, 10_000


def load(results: Path) -> dict[str, dict[str, dict]]:
    """``{"r1:none": {question: cell}, ...}``: scores and transcript usage joined per session."""
    cells: dict[str, dict[str, dict]] = {}
    rounds = [("r1", results), ("r2", results / "round2"), ("r3", results / "round3")]
    for rnd, d in [(r, d) for r, d in rounds if (d / "scores.json").exists()]:
        scores = json.loads((d / "scores.json").read_text(encoding="utf-8"))["per_question"]
        usage = json.loads((d / "usage.json").read_text(encoding="utf-8"))
        for q, arms in scores.items():
            for arm, sc in arms.items():
                u = usage[f"{q}:{arm}"]
                cells.setdefault(f"{rnd}:{arm}", {})[q] = {
                    "found": sc["found"], "pinpointed": sc["pinpointed"], "input": u["input_total"],
                    "output": u["tokens"]["output_tokens"], "tool_calls": u["tool_calls"],
                    "model_calls": u["model_calls"]}
    return cells


def boot_ratio(a: list[float], b: list[float]) -> tuple[float, float, float]:
    rng = random.Random(SEED)
    n = len(a)
    ratios = []
    for _ in range(RESAMPLES):
        idx = [rng.randrange(n) for _ in range(n)]
        ratios.append(sum(a[i] for i in idx) / sum(b[i] for i in idx))
    ratios.sort()
    return sum(a) / sum(b), ratios[int(0.025 * RESAMPLES)], ratios[int(0.975 * RESAMPLES)]


def compare(cells: dict, x: str, y: str) -> dict:
    qs = sorted(set(cells[x]) & set(cells[y]))
    out = {"n": len(qs)}
    for metric in ("found", "pinpointed"):
        d = [cells[x][q][metric] - cells[y][q][metric] for q in qs]
        out[metric] = {"x": sum(cells[x][q][metric] for q in qs), "y": sum(cells[y][q][metric] for q in qs),
                       "wins": sum(v > 0 for v in d), "ties": sum(v == 0 for v in d), "losses": sum(v < 0 for v in d)}
    for metric in ("input", "output", "tool_calls", "model_calls"):
        r, lo, hi = boot_ratio([cells[x][q][metric] for q in qs], [cells[y][q][metric] for q in qs])
        out[metric] = {"ratio": round(r, 3), "ci95": [round(lo, 3), round(hi, 3)],
                       "lower_in": sum(cells[x][q][metric] < cells[y][q][metric] for q in qs)}
    return out


def main() -> int:
    results = Path(sys.argv[1])
    cells = load(results)
    pairs = [("r1:none", "r2:none"), ("r2:verinoda_first", "r2:none"), ("r2:graphify_first", "r2:none"),
             ("r2:verinoda_first", "r2:graphify_first"), ("r1:verinoda", "r1:none"), ("r1:graphify", "r1:none"),
             ("r3:auto_context", "r2:none"), ("r3:auto_context", "r1:none"), ("r3:auto_context", "r2:verinoda_first")]
    res = {f"{x} vs {y}": compare(cells, x, y) for x, y in pairs if x in cells and y in cells}
    (results / "paired.json").write_text(json.dumps(res, indent=1) + "\n", encoding="utf-8")
    print("| comparison | facts found | pinpointed | input tokens ratio [95% CI] | output tokens ratio [95% CI] "
          "| tool calls ratio [95% CI] |")
    print("|---|---|---|---|---|---|")
    for k, v in res.items():
        f, p = v["found"], v["pinpointed"]
        cell = [f"{v[m]['ratio']} [{v[m]['ci95'][0]}-{v[m]['ci95'][1]}]" for m in ("input", "output", "tool_calls")]
        print(f"| {k} | {f['x']} vs {f['y']} (W{f['wins']}/T{f['ties']}/L{f['losses']}) | {p['x']} vs {p['y']} "
              f"(W{p['wins']}/T{p['ties']}/L{p['losses']}) | " + " | ".join(cell) + " |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
