"""The tables of docs/drafts/13.8.md from a run's result: python summarize.py [RESULT.json] (default: the
result.json next to this script)."""
import json
import sys
from collections import Counter
from pathlib import Path

d = json.load(open(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).with_name("result.json"), encoding="utf-8"))
CONDS = ("none", "right", "wrong")

if "audit" in d:
    rows = d["audit"]
    print("== verdict audit")
    for split in ("dev", "held_out", "all"):
        for c in CONDS:
            rs = [r for r in rows if r["cond"] == c and (split == "all" or r["split"] == split)]
            ctl = [r for r in rs if r["kind"] == "control"]
            ic = [r["intent_check"] for r in rs if r["intent_check"]]
            print(f"{split:8} {c:5} n={len(rs)} wrong_met={sum(r['wrong_met'] for r in rs)} "
                  f"above_ceiling={sum(r['above_ceiling'] for r in rs)} "
                  f"controls_kept={sum(r['right_met'] for r in ctl)}/{len(ctl)} "
                  f"applied={sum(1 for i in ic if i['applied'])} retyped={sum(1 for i in ic if i.get('retyped'))} "
                  f"disagree={sum(1 for i in ic if not i['agrees'])} unknowns={sum(r['unknowns'] for r in rs)} "
                  f"secs={round(sum(r['seconds'] for r in rs), 1)}")
    base = {r["id"]: r for r in rows if r["cond"] == "none"}
    for c in ("right", "wrong"):
        for r in rows:
            if r["cond"] != c:
                continue
            b = base[r["id"]]
            if (r["verdict"], r["wrong_met"]) != (b["verdict"], b["wrong_met"]) or r["sub_intents"] != b["sub_intents"]:
                print(f"  {c} {r['id']}: {b['sub_intents']} {b['verdict']}{' WM' if b['wrong_met'] else ''} -> "
                      f"{r['sub_intents']} {r['verdict']}{' WM' if r['wrong_met'] else ''} ({r['intent']})")
    # statuses of the same claim texts are not in the rows; compare status multisets where intent was not applied
    same = [r for r in rows if r["cond"] != "none" and not (r["intent_check"] or {}).get("applied")]
    diff = [r["id"] for r in same if r["statuses"] != base[r["id"]]["statuses"]]
    print("  not applied:", len(same), "status multiset differs from none:", diff)

if "fast" in d:
    rows = d["fast"]
    print("== fastbench")
    for c in CONDS:
        rs = [r for r in rows if r["cond"] == c]
        ic = [r["intent_check"] for r in rs if r["intent_check"]]
        print(f"{c:5} n={len(rs)} facts={sum(r['facts'] for r in rs)}/{sum(r['facts_total'] for r in rs)} "
              f"met={sum(r['met'] for r in rs)} met_wrong={sum(r['met_wrong'] for r in rs)} "
              f"neg_as_findings={sum(r['neg_as_findings'] for r in rs)} applied={sum(1 for i in ic if i['applied'])} "
              f"retyped={sum(1 for i in ic if i.get('retyped'))} disagree={sum(1 for i in ic if not i['agrees'])} "
              f"unknowns={sum(r['unknowns'] for r in rs)} chars={sum(r['chars'] for r in rs)} "
              f"secs={round(sum(r['seconds'] for r in rs), 1)}")
        per = Counter()
        for r in rs:
            per[r["set"]] += r["facts"]
        print("      per set facts:", dict(per))
    base = {(r["set"], r["id"]): r for r in rows if r["cond"] == "none"}
    for c in ("right", "wrong"):
        for r in rows:
            if r["cond"] != c:
                continue
            b = base[(r["set"], r["id"])]
            if (r["facts"], r["met"], r["met_wrong"]) != (b["facts"], b["met"], b["met_wrong"]) or \
                    r["sub_intents"] != b["sub_intents"]:
                print(f"  {c} {r['set']}/{r['id']}: {b['sub_intents']} f{b['facts']} met={b['met']}"
                      f"{' MW' if b['met_wrong'] else ''} -> {r['sub_intents']} f{r['facts']} met={r['met']}"
                      f"{' MW' if r['met_wrong'] else ''} ({r['intent']}, applied={(r['intent_check'] or {}).get('applied')})")
