"""A baseline of known decision-guard violations, and its ratchet (``verinoda decide baseline``).

A team that adopts a rule on old code has violations it will not fix today. Waiving each site is one decision per
site; a baseline records them all at once, as the user's call, in ``baseline.json`` next to the decision records
(committed, so CI reads the same list). ``decide check`` then fails only on violations the baseline does not list:
a listed one is reported as ``baselined`` (still a violation, still cited), never as ``ok``.

A site is keyed by what it is, not by where it is: the decision, the guard, the file, the code line's text with
its white space collapsed, and which occurrence of that text in the file it is. A line that moves (an edit above
it) stays the same entry; a line that changes is a new violation. The ratchet only turns one way: an entry the
check no longer finds is reported as fixed (``baseline_fixed``), and ``decide baseline --shrink`` removes such
entries without asking; adding entries needs ``--record`` with the user's own words, and replacing a baseline
that would grow needs ``--replace`` as well.
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

FILE = "baseline.json"
FORMAT = 1
KEYS = ("violations", "possible", "pre_existing")   # the findings a baseline can hold


class BaselineError(ValueError):
    pass


def path(ddir: Path) -> Path:
    return Path(ddir) / FILE


def _site(f: dict) -> tuple[str, str]:
    """(file, collapsed code line) of a finding."""
    at = str(f.get("at") or "")
    rel, sep, line = at.rpartition(":")
    if not sep or not line.isdigit():
        rel = at
    return rel, " ".join(str(f.get("line") or "").split())


def key(f: dict) -> tuple[str, str, str, str]:
    rel, text = _site(f)
    return (str(f.get("decision")), str(f.get("guard")), rel, text)


def _line_no(f: dict) -> int:
    at = str(f.get("at") or "")
    tail = at.rpartition(":")[2]
    return int(tail) if tail.isdigit() else 0


def load(ddir: Path) -> dict | None:
    """The baseline of the decisions folder ``ddir``; None when there is none. BaselineError for a file that
    cannot be read as a baseline (a gate never passes on a list it could not read)."""
    p = path(ddir)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BaselineError(f"{p.name} cannot be read: {exc}") from None
    if not isinstance(data, dict) or data.get("verinoda-baseline") != FORMAT or \
            not isinstance(data.get("entries"), list):
        raise BaselineError(f"{p.name} is not a Verinoda baseline (format {FORMAT})")
    for e in data["entries"]:
        if not isinstance(e, dict) or not all(isinstance(e.get(k), str) for k in ("decision", "guard", "file")):
            raise BaselineError(f"{p.name} has an entry without decision, guard and file: {e!r}"[:300])
    return data


def _counts(entries: list[dict]) -> Counter:
    return Counter((e["decision"], e["guard"], e["file"], " ".join(str(e.get("text") or "").split()))
                   for e in entries)


def apply(res: dict, ddir: Path) -> None:
    """Move the findings of a ``guards.check`` result that the baseline lists to ``res["baselined"]`` and list
    the entries nothing matched as ``res["baseline_fixed"]``; ``res["baseline"]`` says what was read. A baseline
    that cannot be read is an ``unknown`` (so the check cannot pass), and nothing is moved."""
    try:
        data = load(ddir)
    except BaselineError as exc:
        res["unknown"].append({"decision": None, "guard": None, "kind": "baseline", "why": str(exc)})
        return
    if data is None:
        return
    left = _counts(data["entries"])
    res["baselined"] = []
    for k in KEYS:
        keep = []
        # in file order, so the first occurrences of a text are the ones the baseline counted
        for f in sorted(res.get(k) or [], key=lambda f: (key(f), _line_no(f))):
            kk = key(f)
            if left[kk] > 0:
                left[kk] -= 1
                res["baselined"].append({**f, "baselined_from": k})
            else:
                keep.append(f)
        order = {id(f): i for i, f in enumerate(res.get(k) or [])}
        res[k] = sorted(keep, key=lambda f: order[id(f)])
    fixed = [{"decision": d, "guard": g, "file": rel, "text": text, "count": n}
             for (d, g, rel, text), n in sorted(left.items()) if n > 0]
    res["baseline_fixed"] = fixed
    res["baseline"] = {"file": FILE, "entries": len(data["entries"]), "recorded": data.get("recorded"),
                       "matched": len(res["baselined"]), "fixed": sum(e["count"] for e in fixed)}


def entries(findings: list[dict]) -> list[dict]:
    """Baseline entries for ``findings`` (one per finding, in file order)."""
    out = []
    for f in sorted(findings, key=lambda f: (key(f), _line_no(f))):
        d, g, rel, text = key(f)
        out.append({"decision": d, "guard": g, "file": rel, "text": text, "at": f.get("at"),
                    "level": f.get("level")})
    return out


def _write(ddir: Path, data: dict) -> Path:
    p = path(ddir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    return p


def record(ddir: Path, res: dict, *, statement: str | None, replace: bool = False, today: str) -> dict:
    """Write the baseline from a ``guards.check`` result run without one: every violation and possible site it
    found. The user's call: ``statement`` (their own words) is required. A baseline that exists and would
    gain an entry is replaced only with ``replace``."""
    if not str(statement or "").strip():
        raise BaselineError("recording a baseline accepts the current violations: that is the user's call, so it "
                            "needs their own words (--said)")
    if res.get("graph_stale"):
        raise BaselineError(f"the index could not be refreshed ({res['graph_stale']}): edge guards were not read, "
                            "so the baseline would be incomplete")
    found = [f for k in KEYS for f in res.get(k) or []]
    new = entries(found)
    old = load(ddir)
    if old is not None and not replace:
        gained = _counts(new) - _counts(old["entries"])
        if gained:
            raise BaselineError(f"the baseline would gain {sum(gained.values())} violation(s) it does not list; "
                                "to accept them too, pass --replace (the user's call)")
    data = {"verinoda-baseline": FORMAT, "recorded": today, "statement": statement.strip(),
            "note": "known violations `decide check` does not fail on; `decide baseline --shrink` removes the fixed "
                    "ones", "entries": new}
    p = _write(ddir, data)
    return {"status": "recorded", "file": str(p), "entries": len(new),
            "unknown": len(res.get("unknown") or []),
            **({"note": f"{len(res['unknown'])} check(s) could not be completed: their sites are not in the "
                        "baseline"} if res.get("unknown") else {})}


def shrink(ddir: Path, res: dict, *, today: str) -> dict:
    """Remove the entries a ``guards.check`` result (run with the baseline applied) did not find any more. The
    ratchet: never adds, needs no one's words. Nothing is removed when a check was incomplete (an entry of a
    guard that could not run is not fixed)."""
    data = load(ddir)
    if data is None:
        raise BaselineError(f"there is no {FILE} in the decisions folder")
    fixed = res.get("baseline_fixed") or []
    if not fixed:
        return {"status": "unchanged", "file": str(path(ddir)), "entries": len(data["entries"]), "removed": 0}
    if res.get("unknown") or res.get("graph_stale"):
        raise BaselineError("some checks could not be completed (see decide check): a baseline entry of a guard "
                            "that did not run is not fixed, so nothing was removed")
    drop = Counter({(e["decision"], e["guard"], e["file"], e["text"]): e["count"] for e in fixed})
    kept = []
    by_key: dict = defaultdict(list)
    for e in data["entries"]:
        by_key[(e["decision"], e["guard"], e["file"], " ".join(str(e.get("text") or "").split()))].append(e)
    for k, rows in by_key.items():
        n = drop.get(k, 0)
        kept += rows[:max(0, len(rows) - n)]
    removed = len(data["entries"]) - len(kept)
    data["entries"] = sorted(kept, key=lambda e: (e["decision"], e["guard"], e["file"], e.get("text") or ""))
    data["shrunk"] = today
    p = _write(ddir, data)
    return {"status": "shrunk", "file": str(p), "entries": len(kept), "removed": removed}
