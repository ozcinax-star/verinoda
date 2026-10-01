"""A change's risk score: ``review``'s ``risk`` key, one roll-up of what the review measured, with its parts.

The score adds points for each input the review already computed - the findings the change introduced (the
differential leaves the base's own findings out), the public definitions whose change breaks call sites or could
not be judged, the dependents the change reaches, the changed code no test reaches, the changed lines a coverage
report shows no test ran, and what the review could not tell (its unknowns) - each ``count x weight`` up to a cap.
The caps add up to 100. Every input is listed with its value, weight, cap and points, also when it counted
nothing, and an input the review could not measure is listed as not measured, never as zero risk.

The weights are a choice, not a measurement: the score is a heuristic (``strong_inference``). It orders changes
by how much of what the review found there is; a low score says the rules found little, never that the change is
safe. A review with no changed definition scores 0 with no parts: what it could not tell is in its ``unknown``.
"""
from __future__ import annotations

from verinoda import review_rules as rr

MAX_SCORE = 100
BANDS = ((50, "high"), (20, "medium"), (0, "low"))
MAX_AT = 3   # locations listed per part
# (name, what it counts, points per item, cap); the caps add up to MAX_SCORE
PARTS = (
    ("findings_strong", "findings the change introduced, statically verified or strong_inference", 10, 30),
    ("findings_weak", "findings the change introduced, weak_inference", 2, 6),
    ("api_breaking", "public definitions whose change breaks call sites (api_changes: breaking)", 10, 20),
    ("api_unknown", "public definitions whose compatibility could not be judged (api_changes: unknown)", 2, 4),
    ("dependents", "definitions that reach the changed ones (dependents, possibly affected)", 1, 10),
    ("untested", "changed definitions no test reaches in the static graph (tests.no_test_reaches)", 5, 10),
    ("reach_unknown", "changed definitions whose tests are unknown: no static caller (tests.reach_unknown)", 1, 5),
    ("uncovered_lines", "changed lines a coverage report shows no test ran (tests.coverage)", 1, 10),
    ("unknowns", "what the review could not tell (unknown)", 1, 5),
)
METHOD = ("the sum over the parts of min(count x weight, cap); the caps add up to 100; the weights are a choice, "
          "not a measurement")
LIMITS = [
    "a heuristic: the weights and caps are chosen, and a count says how many, not how bad each one is",
    "a low score means the rules, the graph and the reports found little; never that the change is safe",
    "the preexisting findings (the base had them) are listed with weight 0; a test run's outcome "
    "(--run-tests) is reported under tests, not scored",
]
NOT_SAFE = "a heuristic roll-up of the parts below, never a verdict that the change is safe"


def _band(score: int) -> str:
    return next(name for low, name in BANDS if score >= low)


def _line_of(change: dict) -> str:
    lines = change.get("lines") or change.get("base_lines")
    return f"{change['file']}:{str(lines).split('-')[0]}" if lines else change["file"]


def _uncovered_unmeasured(res: dict, cov: dict | None) -> str | None:
    """Why the uncovered changed lines were not measured, or None when a report measured the changed files."""
    if res.get("mode") == "planned":
        return "a planned change has no changed lines to measure"
    if cov is None:
        if any(u.get("kind") == "coverage_report" for u in res.get("unknown") or []):
            return "the coverage report given could not be read (unknown: coverage_report)"
        return "no coverage report was read (pass --coverage REPORT)"
    missing = _missing(cov)
    if missing and not (cov.get("patch") or {}).get("measured_changed_lines"):
        return "the coverage report measured none of the changed lines (not in it: " + ", ".join(missing[:3]) + ")"
    return None


def _missing(cov: dict) -> list[str]:
    return sorted({*(cov.get("not_in_report") or []), *(cov.get("ambiguous") or [])})


def _zero() -> dict:
    """The block of a review with no changed definition: nothing to score, no parts."""
    return {"score": 0, "of": MAX_SCORE, "band": _band(0), "status": "strong_inference",
            "finding": f"risk score 0 of {MAX_SCORE}: no changed definition to score; {NOT_SAFE}",
            "evidence_at": [], "parts": [], "method": METHOD, "limits": LIMITS}


def score(res: dict) -> dict:
    """The ``risk`` block of a ``review`` result ``res``."""
    if not res.get("changes"):
        return _zero()
    concerns = res.get("concerns") or {}
    findings = [f for fs in concerns.values() for f in fs if f.get("delta") != "preexisting"]
    strong = [f for f in findings if rr.at_least_strong(f["status"])]
    weak = [f for f in findings if not rr.at_least_strong(f["status"])]
    api = res.get("api_changes") or []
    tests = res.get("tests") or {}
    at_of = {c["symbol"]: _line_of(c) for c in res.get("changes") or []}
    unknown = res.get("unknown") or []
    cov = tests.get("coverage")
    counts = res.get("counts") or {}
    got: dict[str, tuple[int, list[str]]] = {
        "findings_strong": (len(strong), [f["at"] for f in strong]),
        "findings_weak": (len(weak), [f["at"] for f in weak]),
        "api_breaking": (counts.get("api_breaking", sum(1 for a in api if a["verdict"] == "breaking")),
                         [a.get("at") or a.get("base_at") for a in api if a["verdict"] == "breaking"]),
        "api_unknown": (sum(1 for a in api if a["verdict"] == "unknown"),
                        [a.get("at") or a.get("base_at") for a in api if a["verdict"] == "unknown"]),
        "dependents": (res.get("dependents_total") or 0, [d["at"] for d in res.get("dependents") or []]),
        "untested": (len(tests.get("no_test_reaches") or []),
                     [at_of.get(s, s) for s in tests.get("no_test_reaches") or []]),
        "reach_unknown": (len(tests.get("reach_unknown") or []),
                          [at_of.get(r["symbol"], r["symbol"]) for r in tests.get("reach_unknown") or []]),
        "uncovered_lines": (counts.get("uncovered_changed_lines", 0),
                            [u["at"] for u in (cov or {}).get("uncovered") or []]),
        "unknowns": (len(unknown), [u["at"] for u in unknown if u.get("at")]),
    }
    not_measured: dict[str, str] = {}
    why_cov = _uncovered_unmeasured(res, cov)
    if why_cov:
        not_measured["uncovered_lines"] = why_cov
    dif = res.get("differential") or {}
    if "preexisting" not in dif:
        not_measured["preexisting"] = dif.get("skipped") or "no base version was compared"
    from verinoda.review import CONCERNS

    skipped = [k for k in CONCERNS if k not in concerns]
    notes = list(((res.get("coverage") or {}).get("not_checked")) or [])
    if skipped:
        notes.append("findings of " + ", ".join(skipped) + ": not checked (the concerns asked for)")
    if (res.get("concerns_truncated") or {}):
        notes.append("findings beyond the ones listed per concern are not counted (concerns_truncated)")
    if res.get("mode") == "planned":
        notes.append("a planned change: the findings parts count the findings in the targets' current code, "
                     "not only ones the change would introduce")
    if any(u.get("kind") == "graph_stale" for u in unknown):
        notes.append("the graph is older than some files (unknown: graph_stale): dependents and the tests' reach "
                     "may miss callers there")
    if cov is not None and not why_cov:
        if _missing(cov):
            notes.append("uncovered_lines counts only the changed files the coverage report measured (not in it: "
                         + ", ".join(_missing(cov)[:3]) + ")")
        stale = sum(1 for u in cov.get("uncovered") or [] if u.get("status") != "strong_inference")
        if stale:
            notes.append(f"uncovered_lines: {stale} change(s) read from a report older than the file "
                         "(weak_inference: lines may have moved), counted at full weight")
    parts = []
    total = 0
    for name, what, weight, cap in PARTS:
        n, at = got[name]
        row = {"part": name, "counts": what, "value": n, "weight": weight, "cap": cap}
        if name in not_measured:
            row.update(value=None, points=0, not_measured=not_measured[name])
        else:
            row["points"] = min(n * weight, cap)
            total += row["points"]
            ats = [a for a in dict.fromkeys(at) if a]
            if ats:
                row["at"] = ats[:MAX_AT]
        parts.append(row)
    pre = {"part": "preexisting", "counts": "findings the base had too (differential), not the change's",
           "value": dif.get("preexisting", 0), "weight": 0, "cap": 0, "points": 0}
    if "preexisting" in not_measured:
        pre.update(value=None, not_measured=not_measured["preexisting"])
    parts.append(pre)
    band = _band(total)
    counted = [f"{p['part']} {p['value']}x{p['weight']}={p['points']}" for p in parts if p["points"]]
    text = (f"risk score {total} of {MAX_SCORE} ({band}): " + (", ".join(counted) if counted else
                                                               "none of the inputs counted anything")
            + f"; {NOT_SAFE}")
    out = {"score": total, "of": MAX_SCORE, "band": band, "status": "strong_inference", "finding": text,
           "evidence_at": list(dict.fromkeys(a for p in parts for a in p.get("at") or []))[:10],
           "parts": parts, "method": METHOD, "limits": LIMITS}
    if not_measured:
        out["not_measured"] = sorted(not_measured)
    if notes:
        out["notes"] = notes
    return out


def summary_part(block: dict) -> str:
    """One sentence for ``review``'s summary."""
    if not block or block.get("score") is None:
        return ""
    return (f"Risk score {block['score']}/{block['of']} ({block['band']}, {block['status']}; a heuristic, "
            "never 'safe' - parts under risk).")


def compact(block: dict) -> dict:
    """The block for the MCP response (capped): the score and each part as ``value x weight = points``;
    ``review --json`` has the locations, the method and the limits."""
    out = {k: block[k] for k in ("score", "of", "band", "status") if k in block}
    out["parts"] = {p["part"]: ("not measured" if p.get("not_measured") else f"{p['value']}x{p['weight']}="
                                f"{p['points']}") for p in block.get("parts") or []}
    out["note"] = "heuristic; a low score is never 'safe'"
    return out


def render(block: dict) -> list[str]:
    """Text lines for ``verinoda review``."""
    if not block:
        return []
    out = ["", f"Risk score: {block['score']} of {block['of']} ({block['band']}, {block['status']}) - {NOT_SAFE}:"]
    for p in block.get("parts") or []:
        if p.get("not_measured"):
            out.append(f"  {p['part']}: not measured ({p['not_measured']})")
            continue
        out.append(f"  {p['part']}: {p['value']} x {p['weight']} = {p['points']}"
                   + (f" (cap {p['cap']})" if p["cap"] and p["value"] * p["weight"] > p["cap"] else "")
                   + (f"  at {', '.join(p['at'])}" if p.get("at") else ""))
    for n in block.get("notes") or []:
        out.append(f"  note: {n}")
    return out
