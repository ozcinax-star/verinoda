"""Loop rules of the debug ledger: deterministic checks over recorded attempts (docs/DESIGN.md D34).

Every finding names the attempts (and runs) it rests on. Two strengths:

* **definitive** - a fact about the recorded attempts that means the session is
  going round in circles; any one of them sets ``stop``:

  - ``tree_reverted``: the whole tree is identical (content, line endings
    aside) to an earlier attempt's after being different in between, or one
    file is back to a content an *earlier fix attempt introduced* after having
    another content in between (a file going back to its starting content is
    an undo, not a loop, and is not reported);
  - ``signature_recurred``: failure signature A, then a different known
    failing signature B, then A again on another tree (``sig_exact``);
  - ``no_progress``: the same ``sig_exact`` on 3 or more fix attempts with
    different trees;
  - ``test_edited``: an attempt changed only existing test files, or changed
    an assertion / expected-value line of an existing test;
  - ``off_path``: the symbols a fix attempt edited were not reached by the
    failing tests in a *complete* call trace taken with or after that edit
    (run-scoped; it says nothing about other tests or other runs).

* **heuristic** - a pattern worth a look; it never sets ``stop`` on its own:

  - ``error_moved``: the same failing tests, a different coarse signature;
  - ``masking``: the attempt added ``.get(k, default)``, ``try/except``,
    ``or 0``, an early return (or the language's equivalent) inside the
    previous crash symbol;
  - ``hypothesis_repeated``: term Jaccard >= 0.6 with a refuted earlier
    hypothesis, or the same touched symbol and the same resulting exception
    type as a refuted earlier attempt.

``flaky``: the same tree and command gave different outcomes (or different
known signatures). It suspends every rule - and ``stop`` - until a rerun
series (3 or more runs) of a tree whose recorded runs all agree; a series on
the tree that disagreed never clears it. An unknown signature never counts as
"the same" as anything.

Separately, ``stop`` is also set after ``debug.max_no_progress`` fix attempts
in a row (default 3) without measured progress; that is a budget, reported as
``stop_reason: max_no_progress``, not a loop finding.

Only attempts on the working tree with the session's repro command take part
(baseline, fix, probe, rerun); differential and bisect runs are strategies.
"""

from __future__ import annotations

import re

from verinoda import textnorm

LOOP_KINDS = ("baseline", "fix", "probe", "rerun")
DEFINITIVE = ("tree_reverted", "signature_recurred", "no_progress", "test_edited", "off_path")
HEURISTIC = ("error_moved", "masking", "hypothesis_repeated")
JACCARD = 0.6
NO_PROGRESS_MIN = 3
STABLE_RERUNS = 3
BASE = "<base>"

_ASSERT_LINE = re.compile(
    r"^\s*(assert\b|self\.(assert|fail)\w*\(|assert\w*\(|expect\(|.*\bassert(Equals|That|True|False|Null|Raises)\b|"
    r"assert(_eq|_ne)?!\(|t\.(Error|Errorf|Fatal|Fatalf)\(|.*\bpytest\.(raises|approx)\(|.*\.should\b|"
    r"(expected|want|expect)\w*\s*(:?=|==))", re.I)
_MASKING = [
    (re.compile(r"\.get\(\s*[^,()]+,\s*[^)]+\)"), ".get(key, default)"),
    (re.compile(r"^\s*try\s*:"), "try/except"),
    (re.compile(r"^\s*except\b"), "try/except"),
    (re.compile(r"\bor\s+(0|0\.0|None|''|\"\"|\[\]|\{\}|False)\b"), "`or <default>`"),
    (re.compile(r"^\s*if\b.*:\s*return\b|^\s*if\b.*\)\s*\{?\s*return\b"), "early return"),
    (re.compile(r"\btry\s*\{|\bcatch\s*\("), "try/catch"),
    (re.compile(r"\?\?|\?\.|\|\|\s*(0|null|undefined|''|\"\"|\[\]|\{\})"), "default on null"),
    (re.compile(r"Optional\.ofNullable|getOrDefault\(|\?:"), "default on null"),
    (re.compile(r"\bunwrap_or(_default|_else)?\(|\.ok\(\)|\bif let (Some|Ok)\b"), "default on error"),
    (re.compile(r"\brecover\(\)"), "recover()"),
]
_RETURN = re.compile(r"^\s*return\b")


def terms(text: str | None) -> list[str]:
    """Content terms of a hypothesis (folded, stop words out, light English stemming)."""
    return sorted({textnorm.en_stem(w) for w in textnorm.words(text or "")})


def _jaccard(a, b) -> float:
    a, b = set(a), set(b)
    return len(a & b) / len(a | b) if a | b else 0.0


def _ref(a: dict) -> dict:
    return {"attempt": a["n"], "run": a.get("experiment_id") or ("agent-reported" if a.get("run_by") == "agent"
                                                                   else None)}


def _finding(rule: str, text: str, refs: list[dict], **extra) -> dict:
    return {"rule": rule, "strength": "definitive" if rule in DEFINITIVE else "heuristic", "text": text,
            "evidence": refs, **extra}


def _failing(a: dict) -> bool:
    return a.get("outcome") == "fail"


def _known(a: dict) -> bool:
    return _failing(a) and bool(a.get("sig_exact"))


def _file_id(a: dict, path: str) -> str | None:
    tf = a.get("tree_files") or {}
    return tf[path] if path in tf else BASE


def _exc_types(a: dict) -> set[str]:
    return {f.get("exc") for f in (a.get("signature") or {}).get("failures") or [] if f.get("exc")}


def _crash_syms(a: dict) -> set[tuple[str, str]]:
    return {(f["path"], f["symbol"]) for f in (a.get("signature") or {}).get("failures") or []
            if f.get("path") and f.get("symbol")}


def _failed_set(a: dict) -> set[str] | None:
    sig = a.get("signature") or {}
    if sig.get("status") not in ("parsed", "partial"):
        return None
    got = set(sig.get("failed_tests") or [])
    return got or None


def _summ(a: dict) -> str:
    fs = (a.get("signature") or {}).get("failures") or []
    if not fs:
        return a.get("outcome") or "?"
    f = fs[0]
    return f"{f.get('exc') or '?'}@{f.get('at') or '?'}" + (f" (+{len(fs) - 1} more)" if len(fs) > 1 else "")


# -- progress ------------------------------------------------------------------------------------

def progress(prev: dict | None, cur: dict) -> str:
    """improved | same | regressed | unknown - the current attempt against the previous one."""
    if prev is None:
        return "unknown"
    po, co = prev.get("outcome"), cur.get("outcome")
    if co == "pass":
        return "same" if po == "pass" else ("improved" if po == "fail" else "unknown")
    if co != "fail" or po not in ("pass", "fail"):
        return "unknown"
    if po == "pass":
        return "regressed"
    a, b = _failed_set(prev), _failed_set(cur)
    if a is None or b is None:
        if prev.get("sig_exact") and prev.get("sig_exact") == cur.get("sig_exact"):
            return "same"
        return "unknown"
    if b < a:
        return "improved"
    if b > a:
        return "regressed"
    if a == b:
        return "same"
    return "unknown"


# -- flakiness -------------------------------------------------------------------------------------

def flaky_state(attempts: list[dict]) -> dict | None:
    """``{"tree", "attempts", "text"}`` when one tree gave different results; None when there is no such
    evidence, or when a rerun series (3 or more) after it ran a tree whose recorded runs all agree - a series
    on a tree that already disagreed can never clear it."""
    by_tree: dict[str, list[dict]] = {}
    last_flaky = None
    flaky_trees: set[str] = set()
    for a in attempts:
        if not a.get("tree_hash"):
            continue
        group = by_tree.setdefault(a["tree_hash"], [])
        for b in group:
            differs = (a.get("outcome") != b.get("outcome")) or \
                (_known(a) and _known(b) and a["sig_exact"] != b["sig_exact"])
            if differs and {a.get("outcome"), b.get("outcome")} <= {"pass", "fail"}:
                flaky_trees.add(a["tree_hash"])
                last_flaky = {"tree": a["tree_hash"], "attempts": [b["n"], a["n"]],
                              "text": f"attempts {b['n']} and {a['n']} ran the same tree and got "
                                      f"{b.get('outcome')} ({_summ(b)}) vs {a.get('outcome')} ({_summ(a)})",
                              "at": a["n"]}
                break
        group.append(a)
    if last_flaky is None:
        return None
    for tree, group in by_tree.items():
        if tree in flaky_trees:
            continue
        reruns = [a for a in group if a.get("kind") == "rerun" and a["n"] > last_flaky["at"]]
        if len(reruns) >= STABLE_RERUNS:
            return None
    return last_flaky


# -- the rules -------------------------------------------------------------------------------------

def _tree_reverted(hist: list[dict], cur: dict) -> list[dict]:
    prev = hist[-1] if hist else None
    if prev is None or not cur.get("tree_hash") or prev.get("tree_hash") == cur.get("tree_hash"):
        return []
    for a in reversed(hist[:-1]):
        if a.get("tree_hash") == cur["tree_hash"]:
            return [_finding("tree_reverted",
                             f"the tree is identical to attempt {a['n']}'s (content, line endings aside); that state "
                             f"was already run: {a.get('outcome')} ({_summ(a)})", [_ref(a), _ref(prev), _ref(cur)])]
    out = []
    baseline = hist[0]
    for ch in cur.get("vs_prev") or []:
        path = ch["path"]
        now = _file_id(cur, path)
        if now in (BASE, None) or now == _file_id(baseline, path):
            continue
        for a in reversed(hist[1:-1]):
            if _file_id(a, path) == now and _file_id(prev, path) != now:
                out.append(_finding("tree_reverted",
                                    f"{path} is back to the content attempt {a['n']} gave it (and attempt "
                                    f"{prev['n']} changed); that version was already run: {a.get('outcome')} "
                                    f"({_summ(a)})", [_ref(a), _ref(prev), _ref(cur)], path=path))
                break
    return out


def _signature_recurred(hist: list[dict], cur: dict) -> list[dict]:
    prev = hist[-1] if hist else None
    if not _known(cur) or prev is None or not _known(prev) or prev["sig_exact"] == cur["sig_exact"]:
        return []
    for a in reversed(hist[:-1]):
        if _known(a) and a["sig_exact"] == cur["sig_exact"] and a.get("tree_hash") != cur.get("tree_hash"):
            return [_finding("signature_recurred",
                             f"the failure of attempt {a['n']} is back ({_summ(cur)}) after attempt {prev['n']} "
                             f"failed differently ({_summ(prev)})", [_ref(a), _ref(prev), _ref(cur)])]
    return []


def _no_progress(hist: list[dict], cur: dict) -> list[dict]:
    if cur.get("kind") != "fix" or not _known(cur):
        return []
    same = [a for a in hist + [cur] if a.get("kind") == "fix" and _known(a) and a["sig_exact"] == cur["sig_exact"]]
    trees = {a.get("tree_hash") for a in same}
    if len(same) >= NO_PROGRESS_MIN and len(trees) >= NO_PROGRESS_MIN:
        ns = [a["n"] for a in same]
        return [_finding("no_progress", f"the same failure ({_summ(cur)}) after {len(same)} fix attempts on "
                                        f"{len(trees)} different trees (attempts {', '.join(map(str, ns))})",
                         [_ref(a) for a in same])]
    return []


def _test_edited(hist: list[dict], cur: dict) -> tuple[list[dict], list[dict]]:
    """(findings, the changed assertion lines as {path, line, text})."""
    changes = [c for c in cur.get("vs_prev") or [] if not c.get("content_unknown")]
    if not changes:
        return [], []
    prev = hist[-1] if hist else None
    existing_tests = [c for c in changes if c.get("test") and c.get("status") in ("modified", "removed")]
    lines = []
    for c in existing_tests:
        for h in c.get("hunks") or []:
            for i, text in enumerate(h.get("removed") or []):
                if _ASSERT_LINE.match(text):
                    lines.append({"path": c["path"], "line": h["old"][0] + i, "text": text.strip()[:200]})
    refs = [_ref(prev), _ref(cur)] if prev else [_ref(cur)]
    if existing_tests and all(c.get("test") for c in changes):
        paths = ", ".join(c["path"] for c in changes)
        return [_finding("test_edited", f"attempt {cur['n']} changed only test files ({paths}); whether the test "
                                        "or the code is right is the user's call", refs, lines=lines[:5])], lines
    if lines:
        at = ", ".join(f"{x['path']}:{x['line']}" for x in lines[:3])
        return [_finding("test_edited", f"attempt {cur['n']} changed an assertion or expected value of an existing "
                                        f"test ({at}); whether the test or the code is right is the user's call",
                         refs, lines=lines[:5])], lines
    return [], []


def _edited_symbols(ch_list: list[dict]) -> tuple[list[tuple[str, str]], bool]:
    """(path, symbol) of functions edited in non-test Python files; False when an edit cannot be judged by a
    call trace (non-Python file, module-level or class-level code, unknown content)."""
    out: list[tuple[str, str]] = []
    for c in ch_list:
        if c.get("test"):
            continue
        if not c["path"].endswith(".py") or c.get("content_unknown") or c.get("binary") or c.get("too_large"):
            return [], False
        kinds = c.get("kinds") or {}
        for s in c.get("symbols") or []:
            if s == "<module>" or kinds.get(s) != "def":
                return [], False
            out.append((c["path"], s))
    return out, bool(out)


def _off_path(hist: list[dict], cur: dict) -> list[dict]:
    tr = cur.get("trace") or {}
    if not tr.get("complete") or not _failing(cur):
        return []
    fixes = [a for a in hist + [cur] if a.get("kind") == "fix"]
    if not fixes:
        return []
    j = fixes[-1]
    edited, ok = _edited_symbols(j.get("vs_prev") or [])
    if not ok:
        return []
    for path, _ in edited:  # the edit must still be in the traced tree
        if _file_id(j, path) != _file_id(cur, path):
            return []
    failing = [t for t in tr.get("failing") or [] if t in (tr.get("called") or [])]
    if not failing:
        return []
    reached = tr.get("reached") or {}
    for t in failing:
        got = set(reached.get(t) or [])
        if any(f"{p}::{s}" in got for p, s in edited):
            return []
    names = ", ".join(f"{p}::{s}" for p, s in edited)
    refs = [_ref(j)] + ([_ref(cur)] if cur is not j else [])
    return [_finding("off_path", f"the symbols attempt {j['n']} edited ({names}) were not reached by the failing "
                                 f"test(s) {', '.join(failing[:3])} in the complete call trace of run "
                                 f"{tr.get('run_id')} (that run only)", refs, trace_run=tr.get("run_id"))]


def _error_moved(prev: dict | None, cur: dict) -> list[dict]:
    if prev is None or not (_known(prev) and _known(cur)):
        return []
    a, b = _failed_set(prev), _failed_set(cur)
    if a and a == b and prev.get("sig_coarse") != cur.get("sig_coarse"):
        return [_finding("error_moved", f"the same failing tests, a different failure: {_summ(prev)} -> {_summ(cur)}",
                         [_ref(prev), _ref(cur)])]
    return []


def _masking(prev: dict | None, cur: dict) -> list[dict]:
    if prev is None or not _failing(prev):
        return []
    crash = _crash_syms(prev)
    if not crash:
        return []
    out = []
    for c in cur.get("vs_prev") or []:
        for h in c.get("hunks") or []:
            if not any((c["path"], s) in crash for s in h.get("symbols") or []):
                continue
            added = h.get("added") or []
            for i, text in enumerate(added):
                kind = next((k for rx, k in _MASKING if rx.search(text)), None)
                if kind is None and _RETURN.match(text) and i + 1 < len(added):
                    kind = "early return"
                if kind:
                    line = h["new"][0] + i
                    out.append(_finding("masking", f"attempt {cur['n']} added {kind} at {c['path']}:{line} inside "
                                                   f"the previous crash symbol; it may hide the failure instead of "
                                                   "fixing it", [_ref(prev), _ref(cur)], at=f"{c['path']}:{line}"))
                    return out
    return out


def _refuted(a: dict) -> bool:
    exp = a.get("expect") or "pass"
    return a.get("kind") == "fix" and a.get("outcome") in ("pass", "fail") and a.get("outcome") != exp


def _hypothesis_repeated(hist: list[dict], cur: dict) -> list[dict]:
    if cur.get("kind") != "fix":
        return []
    ct = cur.get("hypothesis_terms") or terms(cur.get("hypothesis"))
    cur_syms = {(c["path"], s) for c in cur.get("vs_prev") or [] for s in c.get("symbols") or []}
    for a in reversed(hist):
        if not _refuted(a):
            continue
        at = a.get("hypothesis_terms") or terms(a.get("hypothesis"))
        j = _jaccard(ct, at)
        if j >= JACCARD:
            return [_finding("hypothesis_repeated", f"this hypothesis shares {j:.0%} of its terms with attempt "
                                                    f"{a['n']}'s, which the run refuted", [_ref(a), _ref(cur)])]
        a_syms = {(c["path"], s) for c in a.get("vs_prev") or [] for s in c.get("symbols") or []}
        both = cur_syms & a_syms
        if both and _failing(cur) and _exc_types(cur) & _exc_types(a):
            s = sorted(both)[0]
            return [_finding("hypothesis_repeated", f"attempt {a['n']} also edited {s[0]}::{s[1]} and ended with the "
                                                    f"same exception type ({', '.join(sorted(_exc_types(cur) & _exc_types(a)))}); "
                                                    "it was refuted", [_ref(a), _ref(cur)])]
    return []


# -- the evaluation --------------------------------------------------------------------------------

def evaluate(history: list[dict], cur: dict, *, max_no_progress: int = 3) -> dict:
    """Progress, loop findings, flakiness and ``stop`` for ``cur`` given the earlier loop attempts."""
    hist = [a for a in history if a.get("kind") in LOOP_KINDS]
    prev = hist[-1] if hist else None
    prog = progress(prev, cur)
    flaky = flaky_state(hist + [cur])
    findings: list[dict] = []
    assertion_lines: list[dict] = []
    if hist and cur.get("kind") in LOOP_KINDS:
        findings += _tree_reverted(hist, cur)
        findings += _signature_recurred(hist, cur)
        findings += _no_progress(hist, cur)
        te, assertion_lines = _test_edited(hist, cur)
        findings += te
        findings += _off_path(hist, cur)
        findings += _error_moved(prev, cur)
        findings += _masking(prev, cur)
        findings += _hypothesis_repeated(hist, cur)
    elif cur.get("trace"):
        findings += _off_path(hist, cur)
    # budget: fix attempts in a row without measured progress
    streak = 0
    for a in hist + [cur]:
        if a.get("kind") != "fix":
            continue
        p = a.get("progress") if a is not cur else prog
        if a.get("outcome") == "pass" or p == "improved":
            streak = 0
        else:
            streak += 1
    suspended = flaky is not None
    definitive = [f for f in findings if f["strength"] == "definitive"]
    stop, reason = False, None
    if not suspended:
        # a definitive finding stops even a passing attempt: a test edited until it passes is the case to ask about
        if definitive:
            stop, reason = True, "definitive: " + ", ".join(dict.fromkeys(f["rule"] for f in definitive))
        elif streak >= max_no_progress and cur.get("outcome") != "pass":
            stop, reason = True, f"max_no_progress: {streak} fix attempts in a row without measured progress"
    if suspended:
        for f in findings:
            f["suspended"] = "flaky"
    return {"progress": prog, "findings": findings, "stop": stop, "stop_reason": reason, "flaky": flaky,
            "no_progress_streak": streak, "assertion_lines": assertion_lines}
