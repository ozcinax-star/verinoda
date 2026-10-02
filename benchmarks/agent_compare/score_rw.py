"""Score the real-world agent comparison (DESIGN_REALWORLD.md): a fact is found when the answer cites a location in
one of the fact's source files within WINDOW lines of one of its source lines (paths compared by suffix, case kept).
Each question scores only the facts it lists. Totals per arm, paired per question, per repository.

    python benchmarks/agent_compare/score_rw.py SESSIONS.json QUESTIONS.json OUT_DIR [USAGE.json]
"""

from __future__ import annotations

import json
import random
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from paired import RESAMPLES, SEED, boot_ratio

from verinoda.benchmark.metrics import count_tokens, extract_locators

GOLD = ROOT / "benchmarks" / "realworld" / "gold"
WINDOW = 3


def sources(spec: str) -> list[tuple[str, int]]:
    """``'a.go:10, b/c.go:20'`` -> ``[('a.go', 10), ('b/c.go', 20)]``."""
    out = []
    for part in spec.split(","):
        path, _, line = part.strip().rpartition(":")
        if path and line.split("-")[0].isdigit():
            out.append((path.replace("\\", "/"), int(line.split("-")[0])))
    return out


def same_file(cited: str, gold: str) -> bool:
    c, g = cited.replace("\\", "/").lstrip("./"), gold.lstrip("./")
    return c == g or c.endswith("/" + g) or g.endswith("/" + c)


def found(fact_sources: list[tuple[str, int]], answer: str) -> bool:
    for loc in extract_locators(answer):
        for path, line in fact_sources:
            if same_file(loc.path, path) and loc.start - WINDOW <= line <= loc.end + WINDOW:
                return True
    return False


def score(sessions: list[dict], questions: list[dict]) -> dict:
    gold = {g.stem: {f["id"]: sources(f["source"]) for f in json.loads(g.read_text(encoding="utf-8"))["facts"]}
            for g in GOLD.glob("*.json")}
    lists = {f"{q['repo']}/{q['qid']}": q["fact_ids"] for q in questions}
    cells: dict[str, dict[str, dict]] = defaultdict(dict)
    for s in sessions:
        key = f"{s['repo']}/{s['qid']}"
        answer = "" if s.get("failed") else s.get("answer", "")
        ids = lists[key]
        hit = [i for i in ids if found(gold[s["repo"]][i], answer)]
        cells[key][s["arm"]] = {"found": len(hit), "total": len(ids), "found_ids": hit, "tokens": count_tokens(answer),
                                "failed": bool(s.get("failed"))}
    arms = sorted({a for c in cells.values() for a in c}, key=lambda a: (a != "none", a))

    def totals(keys: list[str]) -> dict:
        out = {a: {"found": sum(cells[k][a]["found"] for k in keys if a in cells[k]),
                   "facts": sum(cells[k][a]["total"] for k in keys if a in cells[k]),
                   "sessions": sum(1 for k in keys if a in cells[k])} for a in arms}
        pairs = {}
        for a in arms:
            if a == "none":
                continue
            d = [cells[k][a]["found"] - cells[k]["none"]["found"] for k in keys if a in cells[k] and "none" in cells[k]]
            pairs[f"{a}-none"] = {"wins": sum(x > 0 for x in d), "ties": sum(x == 0 for x in d),
                                  "losses": sum(x < 0 for x in d), "sum_diff": sum(d), "n": len(d)}
        return {"arms": out, "pairs": pairs}

    keys = sorted(cells)
    by_repo = defaultdict(list)
    for k in keys:
        by_repo[k.split("/")[0]].append(k)
    return {"overall": totals(keys), "by_repo": {r: totals(ks) for r, ks in by_repo.items()},
            "per_question": {k: cells[k] for k in keys}}


def paired_against_none(cells: dict[str, dict], use: dict[str, dict], arm: str) -> dict:
    """The pre-registered decision: the summed per-question fact difference ``arm - none`` with a percentile bootstrap
    over questions (SEED, RESAMPLES), and the ratios of summed input/output tokens and tool calls (``usage.py``)."""
    qs = sorted(q for q, c in cells.items() if arm in c and "none" in c)
    d = [cells[q][arm]["found"] - cells[q]["none"]["found"] for q in qs]
    rng = random.Random(SEED)
    sums = sorted(sum(d[rng.randrange(len(d))] for _ in d) for _ in range(RESAMPLES))
    out = {"n": len(qs), "found_diff": sum(d),
           "found_diff_ci95": [sums[int(0.025 * RESAMPLES)], sums[int(0.975 * RESAMPLES)]]}
    metrics = {"input": lambda u: u["input_total"], "output": lambda u: u["tokens"]["output_tokens"],
               "tool_calls": lambda u: u["tool_calls"]}
    have = [q for q in qs if f"{q}:{arm}" in use and f"{q}:none" in use]
    for name, get in metrics.items():
        if not have:
            continue
        a = [get(use[f"{q}:{arm}"]) for q in have]
        b = [get(use[f"{q}:none"]) for q in have]
        r, lo, hi = boot_ratio(a, b)
        out[name] = {"ratio": round(r, 3), "ci95": [round(lo, 3), round(hi, 3)], "n": len(have)}
    return out


def main() -> int:
    sessions_p, questions_p, out = (Path(x) for x in sys.argv[1:4])
    res = score(json.loads(sessions_p.read_text(encoding="utf-8")), json.loads(questions_p.read_text(encoding="utf-8")))
    use = json.loads(Path(sys.argv[4]).read_text(encoding="utf-8")) if len(sys.argv) > 4 else {}
    arms = [a for a in res["overall"]["arms"] if a != "none"]
    res["decision"] = {f"{a}-none": paired_against_none(res["per_question"], use, a) for a in arms}
    out.mkdir(parents=True, exist_ok=True)
    (out / "scores.json").write_bytes((json.dumps(res, indent=1) + "\n").encode("utf-8"))
    o = res["overall"]
    lines = ["| arm | facts found |", "|---|---|"] + [f"| {a} | {v['found']}/{v['facts']} |" for a, v in o["arms"].items()]
    lines += ["", "| pair | wins | ties | losses | facts difference |", "|---|---|---|---|---|"]
    lines += [f"| {p} | {d['wins']} | {d['ties']} | {d['losses']} | {d['sum_diff']:+d} |" for p, d in o["pairs"].items()]
    lines += ["", "| repository | " + " | ".join(o["arms"]) + " |", "|---" * (1 + len(o["arms"])) + "|"]
    for r, b in res["by_repo"].items():
        lines.append(f"| {r} | " + " | ".join(f"{b['arms'][a]['found']}/{b['arms'][a]['facts']}" for a in o["arms"]) + " |")
    head = ("| pair | facts difference [95% CI] | input tokens ratio [95% CI] | output tokens ratio [95% CI] "
            "| tool calls ratio [95% CI] |")
    lines += ["", head, "|---|---|---|---|---|"]
    for p, d in res["decision"].items():
        cost = [f"{d[m]['ratio']} [{d[m]['ci95'][0]}-{d[m]['ci95'][1]}]" if m in d else "-"
                for m in ("input", "output", "tool_calls")]
        lines.append(f"| {p} | {d['found_diff']:+d} [{d['found_diff_ci95'][0]:+d} to {d['found_diff_ci95'][1]:+d}] | "
                     + " | ".join(cost) + " |")
    text = "\n".join(lines) + "\n"
    (out / "summary.md").write_bytes(text.encode("utf-8"))
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
