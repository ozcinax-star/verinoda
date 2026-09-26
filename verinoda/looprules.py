"""Loop rules of the debug ledger: deterministic checks over recorded attempts (docs/DESIGN.md D34).

Every finding names the attempts (and runs) it rests on. Two strengths:

* **definitive** - a fact about the recorded attempts that means the session is
  going round in circles; any one of them sets ``stop`` (on a *passing*
  attempt only the two test rules do):

  - ``tree_reverted``: the whole tree is identical (content, line endings
    aside) to an earlier attempt's after being different in between, or the
    *code* is: the trees differ only in comments, docstrings or formatting of
    Python files (:func:`verinoda.treestate.code_tree_id`);
  - ``signature_recurred``: failure signature A, then a different known
    failing signature B, then A again on another tree (``sig_exact``);
  - ``no_progress``: the same ``sig_exact`` on 3 or more fix attempts with
    different trees since the last passing attempt;
  - ``test_edited``: an attempt changed only existing test files (replaced or
    removed lines, or lines that switch a test off: a skip/xfail marker, an
    early ``return``; a line only added, such as a print, does not count), or,
    whatever else it changed, what an assertion line of an existing test says
    (a change of formatting or quotes alone does not count), a literal value
    in an existing test file or its data (an expected value in a table, a
    golden file), a removed test file, a line that switches a test off, a
    doctest example when the repro runs doctests, or the test selection in the
    test configuration (``addopts``, ``-k``, ``--deselect``, ``collect_ignore``,
    ...; in pyproject.toml / setup.cfg / tox.ini only in pytest's own section).
    A test file is one by name or one a failing test of the session lives in
    (a module that only holds failing doctests is code, its docstrings tests);
  - ``failing_tests_skipped``: tests that failed at the baseline (or at the
    previous attempt) did not pass here: skipped, xfailed, deselected or not
    collected (pytest's per-test outcomes; unknown without them). A module that
    failed to collect counts as run when its tests ran; after an early stop
    (``-x``, ``--maxfail``) tests that were not reached are not counted;
  - ``off_path``: the functions a fix attempt edited were not called *at all*
    (by no test, not at import time) in a *complete* call trace taken with or
    after that edit, and the run started no child process the tracer saw
    (run-scoped; it says nothing about other runs).

* **heuristic** - a pattern worth a look; it never sets ``stop`` on its own:

  - ``file_reverted``: one file is back to a content an *earlier fix attempt
    introduced* after having another content in between, while the rest of
    the code differs from that attempt's (a file going back to its starting
    content is an undo and is not reported);
  - ``error_moved``: the same failing tests, a different coarse signature;
  - ``masking``: the attempt added ``.get(k, default)``, ``try/except``,
    ``or 0``, an early return (or the language's equivalent) inside the
    previous crash symbol;
  - ``hypothesis_repeated``: term Jaccard >= 0.6 with an earlier hypothesis
    whose attempt did not make the repro pass (and did not improve it), or the
    same touched symbol and the same resulting exception type as such an
    attempt;
  - ``possibly_flaky``: two attempts ran the same code (only comments,
    docstrings or formatting differ) and got different results.

``flaky``: the same tree and command gave different outcomes, a different set
of failing tests, or different coarse signatures (exception and crash symbol;
the message is not compared). Runs Verinoda made outweigh agent-reported runs:
an agent report never makes a tree that Verinoda ran flaky. It suspends every
rule except the two test rules - and their ``stop`` - until a rerun series (3
or more runs) of a tree whose recorded runs all agree; a series on the tree
that disagreed never clears it. An unknown signature never counts as "the
same" as anything.

Separately, ``stop`` is also set after ``debug.max_no_progress`` fix attempts
in a row (default 3) without measured progress; that is a budget, reported as
``stop_reason: max_no_progress``, not a loop finding, and it holds while the
session is flaky too.

Progress between two runs of the same tree, or of the same code, that got
different results is ``unknown`` (the edit did not cause it), and so is a run
in which earlier failing tests were skipped or not run, or after an attempt that
changed a test (``test_edited``).

Only attempts on the working tree with the session's repro command take part
(baseline, fix, probe, rerun); differential and bisect runs are strategies.
"""

from __future__ import annotations

import re

from verinoda import testcode, textnorm

LOOP_KINDS = ("baseline", "fix", "probe", "rerun")
DEFINITIVE = ("tree_reverted", "signature_recurred", "no_progress", "test_edited", "failing_tests_skipped",
              "off_path")
HEURISTIC = ("file_reverted", "error_moved", "masking", "hypothesis_repeated", "possibly_flaky")
TEST_RULES = ("test_edited", "failing_tests_skipped")  # facts about the edit: they stop a pass, flaky or not
JACCARD = 0.6
NO_PROGRESS_MIN = 3
STABLE_RERUNS = 3
BASE = "<base>"

_ASSERT_LINE = re.compile(
    r"^\s*(assert\b|self\.(assert|fail)\w*\(|assert\w*\(|expect\(|.*\bassert(Equals|That|True|False|Null|Raises)\b|"
    r"assert(_eq|_ne)?!\(|t\.(Error|Errorf|Fatal|Fatalf)\(|.*\bpytest\.(raises|approx)\(|.*\.should\b|"
    r"(expected|want|expect)\w*\s*(:?=|==))", re.I)
# A line that switches a test off (added to an existing test, it changes what the test checks).
_DISABLE_LINE = re.compile(
    r"^\s*(@pytest\.mark\.(skip|skipif|xfail)\b|@(unittest\.)?(skip|skipIf|skipUnless|expectedFailure)\b|"
    r"(pytest\.(skip|xfail|importorskip)|self\.skipTest|unittest\.skip)\(|"
    r"(if\b.*[:)]\s*\{?\s*)?return\b\s*(None)?\s*;?\s*\}?\s*((#|//).*)?$|"
    r"(it|test|describe)\.(skip|todo|only)\(|x(it|describe|test)\(|t\.Skip(Now|f)?\(|#\[ignore\]|"
    r"@(Disabled|Ignore)\b|.*\b(t|ctx|context|this)\.(skip|todo)\(|.*\{\s*(skip|todo)\s*:\s*(true|['\"`])|"
    r".*\bAssumptions\.assume(True|False|That)\(|.*\bassume(True|False)\(\s*(true|false)\s*\))")
# the return forms above: they switch a test off only inside a test function (a helper may return early)
_RETURNISH = re.compile(r"^\s*(if\b.*[:)]\s*\{?\s*)?return\b")
# Test configuration files and the settings in them that select, skip or ignore tests.
_TEST_CONFIG_FILE = re.compile(r"(^|/)(pyproject\.toml|pytest\.ini|setup\.cfg|tox\.ini|conftest\.py|package\.json|"
                               r"(jest|vitest)\.config\.[cm]?[jt]s)$")
_SELECT_OPTS = r"--deselect|--ignore|(^|[\s\"'=\[,])-k\b|(^|[\s\"'=\[,])-m\b"
# pytest's ini keys that select tests; in pyproject.toml / setup.cfg / tox.ini only inside pytest's own section
_PYTEST_INI_LINE = re.compile(rf"(^\s*(addopts|testpaths|python_files|python_classes|python_functions|norecursedirs)"
                              rf"\b|{_SELECT_OPTS})")
_CONFTEST_LINE = re.compile(rf"(collect_ignore|\bskip\b|deselect|{_SELECT_OPTS}|pytest_ignore_collect)")
_JS_CONFIG_LINE = re.compile(r"(testPathIgnorePatterns|testMatch|testRegex|modulePathIgnorePatterns|testNamePattern|"
                             r"\bexclude\b|\binclude\b|(^|[\s\"'=\[,])(-t|--testPathPattern)\b)")


def _selects_tests(path: str, text: str, section: str | None) -> bool:
    """An added line of a test configuration file that changes which tests run (a packaging ``exclude`` or a
    tool's ``skip`` elsewhere in pyproject.toml does not)."""
    name = path.rsplit("/", 1)[-1]
    if name == "conftest.py":
        return bool(_CONFTEST_LINE.search(text))
    if name == "package.json" or name.startswith(("jest.", "vitest.")):
        return bool(_JS_CONFIG_LINE.search(text))
    sec = (section or "").strip().lower()
    if name == "pytest.ini":
        pytest_sec = True
    elif name == "pyproject.toml":
        pytest_sec = sec.startswith("tool.pytest")
    elif name == "setup.cfg":
        pytest_sec = sec == "tool:pytest"
    else:  # tox.ini: [pytest], or a test environment's commands
        if sec.startswith("testenv"):
            return bool(re.search(_SELECT_OPTS, text))
        pytest_sec = sec == "pytest"
    return pytest_sec and bool(_PYTEST_INI_LINE.search(text))


# literal values of a line (strings by their content, numbers by their value), comments left out
_LITERAL = re.compile(r"(?P<s>\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*')|(?P<c>#|//)|"
                      r"(?P<n>(?<![\w.])-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?(?![\w.]))|"
                      r"(?P<k>\b(?:True|False|None|true|false|null|nil|undefined)\b)")


def _literals(text: str) -> list[tuple[str, object]]:
    out: list[tuple[str, object]] = []
    for m in _LITERAL.finditer(text):
        if m.group("c"):
            break
        if m.group("s"):
            out.append(("s", m.group("s")[1:-1]))
        elif m.group("n"):
            out.append(("n", float(m.group("n"))))
        else:
            out.append(("k", m.group("k").lower()))
    return out
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

def same_result(a: dict, b: dict) -> bool:
    """The same outcome and, for two failures, the same failing tests and coarse signature (when known)."""
    if a.get("outcome") != b.get("outcome"):
        return False
    fa, fb = _failed_set(a), _failed_set(b)
    if fa is not None and fb is not None and fa != fb:
        return False
    return not (_known(a) and _known(b) and a.get("sig_coarse") != b.get("sig_coarse"))


def _same_code(a: dict, b: dict) -> bool:
    if a.get("tree_hash") and a.get("tree_hash") == b.get("tree_hash"):
        return True
    return bool(a.get("code_hash")) and a.get("code_hash") == b.get("code_hash")


def progress(prev: dict | None, cur: dict) -> str:
    """improved | same | regressed | unknown - the current attempt against the previous one.

    Two runs of the same tree (or of the same code, comments and docstrings aside) that got different
    results are ``unknown``: the edit did not make the difference."""
    if prev is None:
        return "unknown"
    if _same_code(prev, cur) and not same_result(prev, cur):
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

def _counted(attempts: list[dict]) -> list[dict]:
    """The attempts that count for flakiness: an agent-reported run of a tree Verinoda also ran is left out
    (Verinoda's own runs outweigh a report)."""
    ran = {a.get("tree_hash") for a in attempts if a.get("run_by") != "agent" and a.get("tree_hash")}
    return [a for a in attempts if not (a.get("run_by") == "agent" and a.get("tree_hash") in ran)]


def flaky_state(attempts: list[dict]) -> dict | None:
    """``{"tree", "attempts", "text"}`` when one tree gave different results; None when there is no such
    evidence, or when a rerun series (3 or more) after it ran a tree whose recorded runs all agree - a series
    on a tree that already disagreed can never clear it.

    Different results: another outcome, another set of failing tests, or another coarse signature. The
    message is not compared (a temp path or a time in it would make every run look different)."""
    by_tree: dict[str, list[dict]] = {}
    last_flaky = None
    flaky_trees: set[str] = set()
    for a in _counted(attempts):
        if not a.get("tree_hash"):
            continue
        group = by_tree.setdefault(a["tree_hash"], [])
        for b in group:
            differs = not same_result(a, b)
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

def _earlier_run(hist: list[dict], match) -> dict | None:
    """The latest earlier attempt (before the previous one) that ``match``es, preferring a run Verinoda made."""
    cands = [a for a in hist[:-1] if match(a)]
    own = [a for a in cands if a.get("run_by") != "agent"]
    return (own or cands or [None])[-1]


def _tree_reverted(hist: list[dict], cur: dict) -> list[dict]:
    prev = hist[-1] if hist else None
    if prev is None or not cur.get("tree_hash") or prev.get("tree_hash") == cur.get("tree_hash"):
        return []
    a = _earlier_run(hist, lambda x: x.get("tree_hash") == cur["tree_hash"])
    if a is not None:
        return [_finding("tree_reverted",
                         f"the tree is identical to attempt {a['n']}'s (content, line endings aside); that state "
                         f"was already run: {a.get('outcome')} ({_summ(a)})", [_ref(a), _ref(prev), _ref(cur)])]
    code = cur.get("code_hash")
    if code and prev.get("code_hash") and prev.get("code_hash") != code:
        a = _earlier_run(hist, lambda x: x.get("code_hash") == code)
        if a is not None:
            return [_finding("tree_reverted",
                             f"the code is identical to attempt {a['n']}'s: the trees differ only in comments, "
                             f"docstrings or formatting of Python files; that code was already run: "
                             f"{a.get('outcome')} ({_summ(a)})", [_ref(a), _ref(prev), _ref(cur)], same="code")]
    return []


def _file_reverted(hist: list[dict], cur: dict) -> list[dict]:
    """Heuristic: one file back to the content an earlier fix attempt gave it, the rest of the code new."""
    prev = hist[-1] if hist else None
    if prev is None or len(hist) < 2:
        return []
    out = []
    baseline = hist[0]
    for ch in cur.get("vs_prev") or []:
        path = ch["path"]
        now = _file_id(cur, path)
        if now in (BASE, None) or now == _file_id(baseline, path):
            continue
        for a in reversed(hist[1:-1]):
            if _file_id(a, path) == now and _file_id(prev, path) != now:
                out.append(_finding("file_reverted",
                                    f"{path} is back to the content attempt {a['n']} gave it (attempt {prev['n']} had "
                                    f"changed it); the rest of the tree differs from attempt {a['n']}'s, whose run "
                                    f"was: {a.get('outcome')} ({_summ(a)})", [_ref(a), _ref(prev), _ref(cur)],
                                    path=path))
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
    last_pass = max((i for i, a in enumerate(hist) if a.get("outcome") == "pass"), default=-1)
    since = hist[last_pass + 1:]  # a pass in between breaks the series (a flaky test is not a loop)
    same = [a for a in since + [cur] if a.get("kind") == "fix" and _known(a) and a["sig_exact"] == cur["sig_exact"]]
    trees = {a.get("tree_hash") for a in same}
    if len(same) >= NO_PROGRESS_MIN and len(trees) >= NO_PROGRESS_MIN:
        ns = [a["n"] for a in same]
        return [_finding("no_progress", f"the same failure ({_summ(cur)}) after {len(same)} fix attempts on "
                                        f"{len(trees)} different trees (attempts {', '.join(map(str, ns))})",
                         [_ref(a) for a in same])]
    return []


def _failing_ids(attempts: list[dict]) -> set[str]:
    out = set()
    for a in attempts:
        sig = a.get("signature") or {}
        for t in list(sig.get("failed_tests") or []) + [f.get("test") for f in sig.get("failures") or []]:
            if t and "::" in t:
                out.add(t)
    return out


def _is_doctest_id(t: str) -> bool:
    """A pytest doctest item in a Python module: ``orders/pricing.py::orders.pricing.apply_discount`` (the module
    is not a test file by name, and the item is one dotted name)."""
    path, _, name = t.partition("::")
    return path.endswith(".py") and not testcode.is_test_or_support_file(path) and "::" not in name and \
        "." in name.split("[", 1)[0]


def doctest_hosts(attempts: list[dict]) -> set[str]:
    """Python modules whose doctests failed in these attempts: their docstrings are tests, their code is not."""
    return {t.split("::", 1)[0] for t in _failing_ids(attempts) if _is_doctest_id(t)}


def test_files_of(attempts: list[dict]) -> set[str]:
    """The files the failing tests of these attempts live in (``path::name`` test ids); a module that only holds
    failing doctests is not one (its code is the code under test; see :func:`doctest_hosts`)."""
    return {t.split("::", 1)[0] for t in _failing_ids(attempts) if not _is_doctest_id(t)}


def _runs_doctests(argv) -> bool:
    return any(str(a).startswith(("--doctest-modules", "--doctest-glob")) for a in argv or [])


def _stmt_key(text: str) -> str:
    """What a line says, formatting aside: a Python statement by its syntax tree, anything else with
    whitespace collapsed and one quote style."""
    import ast

    s = text.strip()
    try:
        return "ast:" + ast.dump(ast.parse(s))
    except (SyntaxError, ValueError, RecursionError):
        return "txt:" + re.sub(r"\s+", "", s.replace("'", '"'))


def _is_test_change(c: dict, test_paths: set[str]) -> bool:
    return bool(c.get("test")) or c["path"] in test_paths


def _value_change(removed: list[str], added: list[str]) -> int | None:
    """Index of the first removed line whose literal values (numbers, strings, true/false/null) are not all
    among the added lines' - an expected value or input changed, or a case removed - else None. A change of
    names or formatting alone keeps the literals; purely added lines (a new case) are not a change."""
    pool: dict[tuple, int] = {}
    for text in added:
        for lit in _literals(text):
            pool[lit] = pool.get(lit, 0) + 1
    for i, text in enumerate(removed):
        for lit in _literals(text):
            if pool.get(lit, 0) <= 0:
                return i
            pool[lit] -= 1
    return None


def _test_edited(hist: list[dict], cur: dict) -> tuple[list[dict], list[dict]]:
    """(findings, the changed assertion / value / disabling / selection lines as {path, line, text, change}).

    In an existing test file (by name, or the file a failing test of the session lives in), whatever else the
    attempt changed: a removed or replaced assertion line, a changed literal value (an expected value in a
    parametrize table, a golden data file under ``tests/``), a removed test file, a line that switches a test
    off. When the repro runs doctests (``--doctest-modules``, or a failing doctest), a changed line of a
    docstring's examples is a test edit too; the rest of that module is code. When the attempt changed only
    test files, any replaced or removed line counts."""
    changes = [c for c in cur.get("vs_prev") or [] if not c.get("content_unknown")]
    if not changes:
        return [], []
    prev = hist[-1] if hist else None
    test_paths = test_files_of(hist)
    all_doctests = _runs_doctests(cur.get("command"))
    doc_hosts = doctest_hosts(hist)
    lines: list[dict] = []
    existing_tests = []
    for c in changes:
        path = c["path"]
        if (all_doctests and path.endswith(".py") or path in doc_hosts) and not c.get("test"):
            for h in c.get("hunks") or []:
                dr = (h.get("doctest") or {}).get("removed") or []
                if dr:
                    k = dr[0]
                    text = (h.get("removed") or [""])[k] if k < len(h.get("removed") or []) else ""
                    lines.append({"path": path, "line": h["old"][0] + k, "text": text.strip()[:200],
                                  "change": "doctest"})
        if not _is_test_change(c, test_paths) or c.get("no_code_change"):
            continue  # not a test file, or only comments / docstrings / formatting changed
        if c.get("status") == "removed":
            existing_tests.append(c)
            lines.append({"path": path, "line": 0, "text": "(the whole file)", "change": "removed"})
            continue
        if c.get("status") != "modified":
            continue  # a new test file is not an edit of an existing test
        edited = False
        # a test file of another language: its tests are callbacks or methods of any name
        other_lang = bool(c.get("test")) and not path.endswith((".py", ".pyi"))
        for h in c.get("hunks") or []:
            added = h.get("added") or []
            removed = h.get("removed") or []
            added_keys = {_stmt_key(x) for x in added}
            if removed:
                edited = True
            asserted = set()
            for i, text in enumerate(removed):
                if _ASSERT_LINE.match(text) and _stmt_key(text) not in added_keys:
                    asserted.add(i)
                    lines.append({"path": path, "line": h["old"][0] + i, "text": text.strip()[:200],
                                  "change": "assertion"})
            k = _value_change(removed, added) if removed else None
            if k is not None and k not in asserted:
                lines.append({"path": path, "line": h["old"][0] + k, "text": removed[k].strip()[:200],
                              "change": "value"})
            in_test_fn = other_lang or any(testcode.is_test_name(s.rsplit(".", 1)[-1])
                                           for s in h.get("symbols") or [])
            for i, text in enumerate(added):
                # an early return only counts inside a test function (a helper or fixture may return early)
                if _DISABLE_LINE.match(text) and (in_test_fn or not _RETURNISH.match(text)):
                    edited = True
                    lines.append({"path": path, "line": h["new"][0] + i, "text": text.strip()[:200],
                                  "change": "switched off"})
        if edited:
            existing_tests.append(c)
    for c in changes:
        if _TEST_CONFIG_FILE.search(c["path"]) and not c.get("no_code_change"):
            for h in c.get("hunks") or []:
                secs = h.get("added_sections") or []
                # without the per-line sections, the section the hunk's symbol names (``#tool.pytest.ini_options``)
                fallback = next((s.lstrip("#") for s in h.get("symbols") or [] if s.startswith("#")), None)
                for i, text in enumerate(h.get("added") or []):
                    if _selects_tests(c["path"], text, secs[i] if i < len(secs) else fallback):
                        lines.append({"path": c["path"], "line": h["new"][0] + i, "text": text.strip()[:200],
                                      "change": "test selection"})
    refs = [_ref(prev), _ref(cur)] if prev else [_ref(cur)]
    if existing_tests and all(_is_test_change(c, test_paths) for c in changes):
        paths = ", ".join(c["path"] for c in changes)
        return [_finding("test_edited", f"attempt {cur['n']} changed only test files ({paths}); whether the test "
                                        "or the code is right is the user's call", refs, lines=lines[:5])], lines
    if lines:
        at = ", ".join(f"{x['path']}:{x['line']}" if x["line"] else x["path"] for x in lines[:3])
        what = sorted({x["change"] for x in lines})
        kinds = {"assertion": "an assertion or expected value of an existing test",
                 "value": "an expected value or input of an existing test (a literal in a test file or its data)",
                 "doctest": "a doctest example or its expected output",
                 "removed": "the test files (an existing one was removed)",
                 "switched off": "switched an existing test off (skip, xfail or an early return)",
                 "test selection": "the tests the configuration selects"}
        return [_finding("test_edited", f"attempt {cur['n']} changed {' and '.join(kinds[w] for w in what)} ({at}); "
                                        "whether the test or the code is right is the user's call",
                         refs, lines=lines[:5])], lines
    return [], []


def _outcome_of(test: str, tests: dict[str, str]) -> str:
    """A test's outcome in a run; a collector (``tests/test_x.py``, a module that failed to collect before) by its
    tests: run when any of them ran, skipped when all of them were skipped, else not run."""
    if test in tests:
        return tests[test]
    if "::" in test:
        return "not run"
    base = test.rstrip("/")
    members = [o for t, o in tests.items() if t.startswith(base + "::") or t.startswith(base + "/")]
    if not members:
        return "not run"
    ran = [o for o in members if o in ("passed", "failed", "error")]
    if ran:
        return "failed" if any(o in ("failed", "error") for o in ran) else "passed"
    return members[0]


def _skipped_failing(hist: list[dict], cur: dict) -> list[dict]:
    """Tests that failed at the baseline (or at the previous attempt, if they existed at the baseline) and
    did not pass now: skipped, xfailed, deselected or not collected. Needs per-test outcomes (pytest)."""
    sig = cur.get("signature") or {}
    tests = sig.get("tests")
    if not hist or not isinstance(tests, dict) or not tests:
        return []
    baseline, prev = hist[0], hist[-1]
    base_tests = (baseline.get("signature") or {}).get("tests") or {}
    required = set(_failed_set(baseline) or ())
    required |= {t for t in (_failed_set(prev) or ()) if not base_tests or t in base_tests}
    missing = {}
    for t in sorted(required):
        o = _outcome_of(t, tests)
        if o not in ("passed", "failed", "error"):
            missing[t] = o
    stopped = sig.get("stopped_early")
    if stopped is None and cur.get("outcome") == "fail":
        stopped = any(a in ("-x", "--exitfirst", "--sw", "--stepwise") or str(a).startswith("--maxfail")
                      for a in cur.get("command") or [])
    if stopped:
        # the run stopped at an earlier failure (-x, --maxfail, --stepwise): later tests were not reached, which
        # says nothing about them; skipped and xfailed ones still count
        missing = {t: o for t, o in missing.items() if o != "not run"}
    if not missing:
        return []
    shown = ", ".join(f"{t} ({o})" for t, o in list(missing.items())[:5])
    refs = [_ref(baseline)] + ([_ref(prev)] if prev is not baseline else []) + [_ref(cur)]
    return [_finding("failing_tests_skipped",
                     f"{len(missing)} test(s) that failed before did not pass in attempt {cur['n']}: {shown}; a "
                     "skipped, xfailed or deselected test is not a passing one (not run = deselected, renamed or not "
                     "collected)", refs, tests=missing)]


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


def trace_can_rule_out(tr: dict) -> bool:
    """A trace that can say a function was not called: complete, with the set of every in-repo function it
    saw called (``reached_any``), and no child process started (those run untraced)."""
    return bool(tr.get("complete")) and isinstance(tr.get("reached_any"), list) and not tr.get("spawns") \
        and not tr.get("reached_any_truncated")


def _off_path(hist: list[dict], cur: dict) -> list[dict]:
    tr = cur.get("trace") or {}
    if not trace_can_rule_out(tr) or not _failing(cur):
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
    # Reached anywhere in the run - by any test, at import/collection time, between tests - is enough to keep
    # the edit: module state set at import reaches every test.
    got = set(tr.get("reached_any") or [])
    if any(f"{p}::{s}" in got for p, s in edited):
        return []
    names = ", ".join(f"{p}::{s}" for p, s in edited)
    refs = [_ref(j)] + ([_ref(cur)] if cur is not j else [])
    return [_finding("off_path", f"the functions attempt {j['n']} edited ({names}) were not called at all in the "
                                 f"complete call trace of run {tr.get('run_id')} - not by the failing test(s) "
                                 f"{', '.join(failing[:3])}, not by any other test, not at import time - and the run "
                                 "started no child process the tracer saw (that run only)", refs,
                     trace_run=tr.get("run_id"))]


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
    """A fix attempt whose run did not give what it expected, and did not improve on the attempt before."""
    exp = a.get("expect") or "pass"
    return a.get("kind") == "fix" and a.get("outcome") in ("pass", "fail") and a.get("outcome") != exp \
        and a.get("progress") != "improved"


def _hypothesis_repeated(hist: list[dict], cur: dict) -> list[dict]:
    if cur.get("kind") != "fix":
        return []
    ct = cur.get("hypothesis_terms") or terms(cur.get("hypothesis"))
    cur_syms = {(c["path"], s) for c in cur.get("vs_prev") or [] for s in c.get("symbols") or []}
    for a in reversed(hist):
        if not _refuted(a):
            continue
        what = f"its run did not make the repro pass ({a.get('outcome')}, progress {a.get('progress') or 'unknown'})"
        at = a.get("hypothesis_terms") or terms(a.get("hypothesis"))
        j = _jaccard(ct, at)
        if j >= JACCARD:
            return [_finding("hypothesis_repeated", f"this hypothesis shares {j:.0%} of its terms with attempt "
                                                    f"{a['n']}'s; {what}", [_ref(a), _ref(cur)])]
        a_syms = {(c["path"], s) for c in a.get("vs_prev") or [] for s in c.get("symbols") or []}
        both = cur_syms & a_syms
        if both and _failing(cur) and _exc_types(cur) & _exc_types(a):
            s = sorted(both)[0]
            return [_finding("hypothesis_repeated", f"attempt {a['n']} also edited {s[0]}::{s[1]} and ended with the "
                                                    f"same exception type ({', '.join(sorted(_exc_types(cur) & _exc_types(a)))}); "
                                                    f"{what}", [_ref(a), _ref(cur)])]
    return []


def _possibly_flaky(hist: list[dict], cur: dict) -> list[dict]:
    """Two attempts ran the same code (only comments, docstrings or formatting of Python files differ) and got
    different results. The same *tree* is ``flaky`` proper; this is the weaker, heuristic case."""
    code = cur.get("code_hash")
    if not code or cur.get("outcome") not in ("pass", "fail"):
        return []
    for a in reversed(hist):
        if a.get("code_hash") == code and a.get("tree_hash") != cur.get("tree_hash") and \
                a.get("outcome") in ("pass", "fail") and not same_result(a, cur):
            return [_finding("possibly_flaky", f"attempts {a['n']} and {cur['n']} ran the same code (the trees differ "
                                               f"only in comments, docstrings or formatting) and got {a.get('outcome')}"
                                               f" ({_summ(a)}) vs {cur.get('outcome')} ({_summ(cur)}): the edit did "
                                               "not make the difference; rerun before editing further",
                             [_ref(a), _ref(cur)])]
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
        reverted = _tree_reverted(hist, cur)
        findings += reverted or _file_reverted(hist, cur)
        findings += _signature_recurred(hist, cur)
        findings += _no_progress(hist, cur)
        te, assertion_lines = _test_edited(hist, cur)
        findings += te
        findings += _skipped_failing(hist, cur)
        findings += _off_path(hist, cur)
        findings += _error_moved(prev, cur)
        findings += _masking(prev, cur)
        findings += _hypothesis_repeated(hist, cur)
        findings += _possibly_flaky(hist, cur)
    elif cur.get("trace"):
        findings += _off_path(hist, cur)
    suspect = ("failing_tests_skipped", "possibly_flaky", "test_edited")
    if any(f["rule"] in suspect for f in findings):
        # a skipped failure is no improvement, a flip on the same code is not the edit's doing, and fewer failures
        # after a test was changed say nothing about the code
        prog = "unknown"
    # budget: fix attempts in a row without measured progress (a pass counts unless it was a suspect one)
    streak = 0
    for a in hist + [cur]:
        if a.get("kind") != "fix":
            continue
        p = a.get("progress") if a is not cur else prog
        rules = {f["rule"] for f in findings} if a is cur else set(a.get("rules") or [])
        if (a.get("outcome") == "pass" and not rules & set(suspect)) or p == "improved":
            streak = 0
        else:
            streak += 1
    suspended = flaky is not None
    definitive = [f for f in findings if f["strength"] == "definitive" and (not suspended or f["rule"] in TEST_RULES)]
    if cur.get("outcome") == "pass":
        # on a passing attempt only a test edited (or skipped) until it passes is a reason to stop
        definitive = [f for f in definitive if f["rule"] in TEST_RULES]
    stop, reason = False, None
    if definitive:
        stop, reason = True, "definitive: " + ", ".join(dict.fromkeys(f["rule"] for f in definitive))
    elif streak >= max_no_progress and cur.get("outcome") != "pass":
        stop, reason = True, (f"max_no_progress: {streak} fix attempts in a row without measured progress"
                              + ("; the results are also flaky - rerun first" if suspended else ""))
    if suspended:
        for f in findings:
            if f["rule"] not in TEST_RULES:
                f["suspended"] = "flaky"
    return {"progress": prog, "findings": findings, "stop": stop, "stop_reason": reason, "flaky": flaky,
            "no_progress_streak": streak, "assertion_lines": assertion_lines}
