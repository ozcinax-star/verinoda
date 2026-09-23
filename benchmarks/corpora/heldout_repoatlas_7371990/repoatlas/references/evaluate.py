"""Scoring a reference-resolution corpus (docs/DESIGN.md 2.3 measurements).

``score(cases, results)`` compares, per question, the references the resolver
produced with the expected ones (``class``, ``status``, ``basis``, mismatch
codes) and returns:

* precision/recall of reference classes, of (class, pin basis) pairs, of
  statuses and of mismatch codes (multisets per question, summed);
* ``mentions_accounted`` (invariant I1; must be 100%);
* ``silent_floating_pins`` (must be 0);
* mismatch recall on the seeded cases and false positives on the clean ones.

A case is ``{"id", "text", "clean"?: bool, "refs": [{"class", "status",
"basis", "mismatches": [codes]}], "min_questions"?: int}``.
"""

from __future__ import annotations

from collections import Counter


def _pr(tp: int, pred: int, gold: int) -> dict:
    return {"tp": tp, "predicted": pred, "expected": gold,
            "precision": round(tp / pred, 4) if pred else 1.0, "recall": round(tp / gold, 4) if gold else 1.0}


def _overlap(a: Counter, b: Counter) -> int:
    return sum((a & b).values())


def score(cases: list[dict], results: list[dict]) -> dict:
    agg = {k: [0, 0, 0] for k in ("class", "class_basis", "status", "mismatch")}
    per_case = []
    accounted = total = silent = 0
    clean_fp = 0
    seeded_expected = seeded_found = 0
    questions_missing = 0
    for case, res in zip(cases, results):
        got_refs = res.get("references") or []
        exp_refs = case.get("refs") or []
        views = {
            "class": (Counter(r["class"] for r in got_refs), Counter(r["class"] for r in exp_refs)),
            "class_basis": (Counter((r["class"], (r.get("pin") or {}).get("basis")) for r in got_refs),
                            Counter((r["class"], r.get("basis")) for r in exp_refs)),
            "status": (Counter((r["class"], r["status"]) for r in got_refs),
                       Counter((r["class"], r["status"]) for r in exp_refs)),
            "mismatch": (Counter(m["code"] for r in got_refs for m in r.get("mismatches") or []),
                         Counter(c for r in exp_refs for c in r.get("mismatches") or [])),
        }
        row = {"id": case["id"], "ok": True}
        for k, (got, exp) in views.items():
            tp = _overlap(got, exp)
            agg[k][0] += tp
            agg[k][1] += sum(got.values())
            agg[k][2] += sum(exp.values())
            if got != exp:
                row["ok"] = False
                row[k] = {"got": sorted(map(str, got.elements())), "expected": sorted(map(str, exp.elements()))}
        s = res.get("summary") or {}
        accounted += s.get("mentions_accounted", 0)
        total += s.get("mentions_total", 0)
        silent += s.get("silent_floating_pins", 0)
        got_mm = sum(views["mismatch"][0].values())
        if case.get("clean"):
            clean_fp += got_mm
        else:
            seeded_expected += sum(views["mismatch"][1].values())
            seeded_found += _overlap(*views["mismatch"])
        if len(res.get("questions_for_user") or []) < case.get("min_questions", 0):
            questions_missing += 1
            row["ok"] = False
            row["questions"] = len(res.get("questions_for_user") or [])
        per_case.append(row)
    out = {k: _pr(*v) for k, v in agg.items()}
    out.update({
        "cases": len(cases), "cases_exact": sum(1 for r in per_case if r["ok"]),
        "mentions_accounted": f"{accounted}/{total}", "mentions_accounted_ratio": round(accounted / total, 4)
        if total else 1.0,
        "silent_floating_pins": silent,
        "clean_cases": sum(1 for c in cases if c.get("clean")), "clean_false_positive_mismatches": clean_fp,
        "seeded_mismatch_recall": round(seeded_found / seeded_expected, 4) if seeded_expected else 1.0,
        "questions_missing": questions_missing,
        "failures": [r for r in per_case if not r["ok"]],
    })
    return out
