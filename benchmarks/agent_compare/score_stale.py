"""Score the stale-index study (DESIGN_STALE.md).

- facts found: a cited location in a fact's NEW source file within WINDOW lines of its NEW line (score_rw's rule);
- stale citations: citations that do not exist at NEW (no file of that path at NEW, or a line past its end) and
  citations within WINDOW lines of a stale trap's OLD location;
- trap mentions: stale-trap identifiers (whole word) or paths named anywhere in the answer.

Decisions: fresh - stale facts and stale - fresh stale citations, summed over questions with a percentile bootstrap.

    python benchmarks/agent_compare/score_stale.py RESULTS_DIR [USAGE.json]

RESULTS_DIR holds sessions.json, questions.json and tree_new.json ({repo: {path: line count at NEW}}).
"""

from __future__ import annotations

import json
import random
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from paired import RESAMPLES, SEED, boot_ratio
from score_rw import WINDOW, found, same_file, sources

from verinoda.benchmark.metrics import extract_locators

ARMS = ("none", "verinoda_stale", "verinoda_fresh")


def stale_citations(answer: str, tree: dict[str, int], traps: list[dict]) -> dict[str, int]:
    """Counts of the answer's citations that are not at NEW: ``missing_file``, ``past_end``, ``old_location``."""
    out = {"missing_file": 0, "past_end": 0, "old_location": 0}
    olds = [s for t in traps for s in sources(t.get("old_source", ""))]
    for loc in extract_locators(answer):
        files = [n for p, n in tree.items() if same_file(loc.path, p)]
        if not files:
            out["missing_file"] += 1
        elif loc.start > max(files):
            out["past_end"] += 1
        elif any(same_file(loc.path, p) and loc.start - WINDOW <= line <= loc.end + WINDOW for p, line in olds):
            out["old_location"] += 1
    return out


def trap_mentions(answer: str, traps: list[dict]) -> list[str]:
    hit = []
    for t in traps:
        text = t["text"]
        pattern = re.escape(text) if t.get("kind") == "path" else rf"(?<![\w$]){re.escape(text)}(?![\w$])"
        if re.search(pattern, answer):
            hit.append(text)
    return hit


def score(sessions: list[dict], questions: list[dict], trees: dict[str, dict[str, int]]) -> dict:
    qs = {f"{q['repo']}/{q['qid']}": q for q in questions}
    cells: dict[str, dict[str, dict]] = defaultdict(dict)
    for s in sessions:
        key = f"{s['repo']}/{s['qid']}"
        q = qs[key]
        answer = "" if s.get("failed") else s.get("answer", "")
        hit = [f["id"] for f in q["facts"] if found(sources(f["source"]), answer)]
        stale = stale_citations(answer, trees[s["repo"]], q.get("stale_traps", []))
        cells[key][s["arm"]] = {"found": len(hit), "total": len(q["facts"]), "found_ids": hit,
                                "stale": sum(stale.values()), "stale_kinds": stale,
                                "citations": len(extract_locators(answer)),
                                "trap_mentions": trap_mentions(answer, q.get("stale_traps", [])),
                                "failed": bool(s.get("failed"))}
    keys = sorted(cells)
    arms = [a for a in ARMS if any(a in cells[k] for k in keys)]
    totals = {a: {"found": sum(cells[k][a]["found"] for k in keys if a in cells[k]),
                  "facts": sum(cells[k][a]["total"] for k in keys if a in cells[k]),
                  "stale_citations": sum(cells[k][a]["stale"] for k in keys if a in cells[k]),
                  "answers_with_stale_citation": sum(cells[k][a]["stale"] > 0 for k in keys if a in cells[k]),
                  "citations": sum(cells[k][a]["citations"] for k in keys if a in cells[k]),
                  "answers_naming_a_trap": sum(bool(cells[k][a]["trap_mentions"]) for k in keys if a in cells[k]),
                  "sessions": sum(a in cells[k] for k in keys)} for a in arms}
    by_repo = defaultdict(list)
    for k in keys:
        by_repo[k.split("/")[0]].append(k)
    return {"overall": totals, "by_repo": {r: {a: f"{sum(cells[k][a]['found'] for k in ks if a in cells[k])}/"
                                                   f"{sum(cells[k][a]['total'] for k in ks if a in cells[k])}"
                                               for a in arms} for r, ks in by_repo.items()},
            "per_question": {k: cells[k] for k in keys}}


def paired(cells: dict[str, dict], x: str, y: str, metric: str, use: dict | None = None) -> dict:
    """``x - y`` of ``metric`` summed over the questions both arms answered, with a percentile bootstrap over
    questions; with ``use`` (usage.py) also the ratios of input/output tokens and tool calls."""
    qs = sorted(q for q, c in cells.items() if x in c and y in c)
    d = [cells[q][x][metric] - cells[q][y][metric] for q in qs]
    rng = random.Random(SEED)
    sums = sorted(sum(d[rng.randrange(len(d))] for _ in d) for _ in range(RESAMPLES)) if d else [0]
    out = {"n": len(qs), "diff": sum(d), "ci95": [sums[int(0.025 * len(sums))], sums[min(int(0.975 * len(sums)),
                                                                                             len(sums) - 1)]],
           "wins": sum(v > 0 for v in d), "ties": sum(v == 0 for v in d), "losses": sum(v < 0 for v in d)}
    have = [q for q in qs if use and f"{q}:{x}" in use and f"{q}:{y}" in use]
    for name, get in (("input", lambda u: u["input_total"]), ("output", lambda u: u["tokens"]["output_tokens"]),
                      ("tool_calls", lambda u: u["tool_calls"])):
        if have:
            r, lo, hi = boot_ratio([get(use[f"{q}:{x}"]) for q in have], [get(use[f"{q}:{y}"]) for q in have])
            out[name] = {"ratio": round(r, 3), "ci95": [round(lo, 3), round(hi, 3)]}
    return out


def main() -> int:
    d = Path(sys.argv[1])
    load = lambda name: json.loads((d / name).read_text(encoding="utf-8"))
    use = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8")) if len(sys.argv) > 2 else None
    res = score(load("sessions.json"), load("questions.json"), load("tree_new.json"))
    pq = res["per_question"]
    res["decisions"] = {
        "facts fresh-stale": paired(pq, "verinoda_fresh", "verinoda_stale", "found", use),
        "stale citations stale-fresh": paired(pq, "verinoda_stale", "verinoda_fresh", "stale", use),
        "facts fresh-none": paired(pq, "verinoda_fresh", "none", "found", use),
        "facts stale-none": paired(pq, "verinoda_stale", "none", "found", use),
        "stale citations stale-none": paired(pq, "verinoda_stale", "none", "stale", use),
        "stale citations fresh-none": paired(pq, "verinoda_fresh", "none", "stale", use),
    }
    (d / "scores.json").write_bytes((json.dumps(res, indent=1) + "\n").encode("utf-8"))
    o = res["overall"]
    lines = ["| arm | facts found | stale citations (answers with one) | citations | answers naming a removed name |",
             "|---|---|---|---|---|"]
    lines += [f"| {a} | {v['found']}/{v['facts']} | {v['stale_citations']} ({v['answers_with_stale_citation']}) | "
              f"{v['citations']} | {v['answers_naming_a_trap']} |" for a, v in o.items()]
    head = ("| comparison | difference [95% CI] | W/T/L | input tokens [95% CI] | output tokens [95% CI] "
            "| tool calls [95% CI] |")
    lines += ["", head, "|---|---|---|---|---|---|"]
    for k, v in res["decisions"].items():
        cost = [f"{v[m]['ratio']} [{v[m]['ci95'][0]}-{v[m]['ci95'][1]}]" if m in v else "-"
                for m in ("input", "output", "tool_calls")]
        lines.append(f"| {k} | {v['diff']:+d} [{v['ci95'][0]:+d} to {v['ci95'][1]:+d}] | "
                     f"{v['wins']}/{v['ties']}/{v['losses']} | " + " | ".join(cost) + " |")
    lines += ["", "| repository | " + " | ".join(o) + " |", "|---" * (1 + len(o)) + "|"]
    lines += [f"| {r} | " + " | ".join(b[a] for a in o) + " |" for r, b in res["by_repo"].items()]
    text = "\n".join(lines) + "\n"
    (d / "summary.md").write_bytes(text.encode("utf-8"))
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
