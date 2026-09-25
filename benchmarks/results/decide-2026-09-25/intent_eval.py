"""Decide-intent routing on the question files next to this script (docs/DESIGN.md D33).

A message is read as a decision when any sub-question's primary intent (rules, no graph) is `decide`.
usage: python benchmarks/results/decide-2026-09-25/intent_eval.py [--out FILE] [FILE ...]
"""
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2]))
from verinoda import question_plan as qp  # noqa: E402


def read(q: str) -> list[str]:
    return [(qp.clause_cues(c["text"]) or [{"intent": "locate"}])[0]["intent"] for c in qp.segment(q)]


args = sys.argv[1:]
dest = None
if args[:1] == ["--out"]:
    dest, args = Path(args[1]), args[2:]
out = {}
for f in [Path(a) for a in args] or sorted(HERE.glob("intent_*.json")):
    if f.name.startswith("intent_results"):
        continue
    data = json.loads(f.read_text(encoding="utf-8"))
    tp = fp = fn = tn = 0
    rows = []
    for item in data["questions"]:
        got = read(item["q"])
        pred, gold = "decide" in got, item["decide"]
        tp, fp, fn, tn = tp + (pred and gold), fp + (pred and not gold), fn + (gold and not pred), \
            tn + ((not pred) and (not gold))
        rows.append({"id": item["id"], "gold": gold, "read": got, "ok": pred == gold})
    out[f.name] = {"tp": tp, "fp": fp, "fn": fn, "tn": tn,
                   "precision": round(tp / (tp + fp), 3) if tp + fp else None,
                   "recall": round(tp / (tp + fn), 3) if tp + fn else None, "rows": rows}
    print(f.name, {k: out[f.name][k] for k in ("tp", "fp", "fn", "tn", "precision", "recall")})
if dest is not None:
    dest.write_bytes((json.dumps(out, indent=1, ensure_ascii=False) + "\n").encode("utf-8"))
