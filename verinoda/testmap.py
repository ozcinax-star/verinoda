"""Persistent test-to-code map: which in-repository functions each test ran, kept across observed runs.

Every traced pytest run Verinoda makes (``verinoda observe``, ``analyze --observe``, ``review --observe``, MCP
``runtime_observe``, and the debug ledger's traced attempts) updates the map (:func:`update`) from the run's
``runtime_calls`` rows (:mod:`verinoda.runtime.trace`):

* ``test_map_tests``: one row per test whose call phase ran - the run, its commit, whether the trace was
  complete, the test's outcome and how many functions it ran;
* ``test_map``: one row per (test, file, function) the test ran in any of its phases (setup, call, teardown),
  with the file's content id (:func:`verinoda.treestate.content_id`) in the copy that ran - its fingerprint -
  and ``fixture`` = 1 when it ran in a setup or teardown phase.

A test seen again in a complete trace has its rows replaced (what it runs now); an incomplete trace (a budget
stopped it) only adds rows, since a function it did not record may still have run. Both tables are a derived
cache of the append-only ``runtime_calls``: rewriting them loses nothing.

:func:`affected` reads the map for a change: the tests that ran a changed function. A test's mapping is
**current** when every file it recorded has, now, the content it had in that run (or, for a file the change
touches, its base content) - otherwise the test may run other code now and is not trusted to be unaffected.
A mapping rules a test out only when it is complete, current and its test passed (a failing test stopped early),
and never for a changed function that ran in a fixture phase: a module- or session-scoped fixture runs once, in
the first test that uses it, so the map does not show the later tests that share it.
:func:`pytest_command` turns the selected test ids into one command line.

What the map says is run-scoped (it ran F in run R at commit C), never "always": a test that did not run F then
may run it after the change (a new call, other data). The selection built on it is a heuristic
(``strong_inference``).
"""

from __future__ import annotations

import re
import shlex
from collections import defaultdict
from typing import Callable, Iterable

from verinoda.store import Store, now

EXT = "<ext>"
MAX_COMMAND_IDS = 40       # more test ids than this: the command names their files
MAX_COMMAND_CHARS = 4000
LIMITS = [
    "the map is run-scoped: a test that did not run a function when it was observed may run it after the change",
    "only pytest runs traced by Verinoda update the map; child processes and other runners are not seen",
    "module-level statements, config and data files, classes and added definitions are matched by static reach only",
    "code a module- or session-scoped fixture runs is recorded for the first test that used it only",
]


def clean_qual(q: str | None) -> str | None:
    """``outer.<locals>.inner`` -> ``outer.inner`` (the review's spelling of a nested function)."""
    from verinoda.failsig import clean_qual as cq

    return cq(q)


def _ctx_test(ctx: str) -> tuple[str, str] | None:
    nid, sep, phase = ctx.rpartition("|")
    return (nid, phase) if sep and not ctx.startswith("<") else None


def update(store: Store, run_id: str, file_ids: dict[str, str] | None = None) -> dict:
    """Update the map from one ingested run; ``file_ids`` are the content ids of the copy that ran
    (:func:`verinoda.experiments.run`). Returns ``{"tests": n updated, "functions": rows written}``."""
    run = store.one("SELECT * FROM runtime_runs WHERE id = ?", (run_id,))
    if run is None:
        return {"tests": 0, "functions": 0}
    header = run.get("header") or {}
    outcomes = header.get("tests_outcomes") or {}
    ran = {t for t, v in outcomes.items() if ((v or {}).get("phases") or {}).get("call")}
    if not ran:
        return {"tests": 0, "functions": 0}
    reach: dict[str, dict[tuple[str, str], int]] = defaultdict(dict)
    for r in store.all("SELECT callee_path, callee_qual, tests FROM runtime_calls WHERE run_id = ? AND callee_path <> ?",
                       (run_id, EXT)):
        qual = clean_qual(r["callee_qual"])
        if not qual or qual == "<module>":
            continue
        for ctx in r["tests"] or []:
            tp = _ctx_test(ctx)
            if tp and tp[0] in ran:
                k = (r["callee_path"], qual)
                reach[tp[0]][k] = reach[tp[0]].get(k, 0) | (tp[1] in ("setup", "teardown"))
    complete = bool(run.get("complete"))
    from verinoda.runtime.trace import phase_outcome

    at = now()
    fids = file_ids or {}
    rows = 0
    with store.tx() as c:
        for t in sorted(ran):
            if complete:
                c.execute("DELETE FROM test_map WHERE test = ?", (t,))
            fns = sorted((reach.get(t) or {}).items())
            # an incomplete run adds rows: a fixture flag already set stays set
            c.executemany("INSERT INTO test_map (test, path, qual, fingerprint, run_id, fixture) VALUES (?,?,?,?,?,?)"
                          " ON CONFLICT (test, path, qual) DO UPDATE SET fingerprint = excluded.fingerprint,"
                          " run_id = excluded.run_id, fixture = MAX(fixture, excluded.fixture)",
                          [(t, p, q, fids.get(p), run_id, int(fx)) for (p, q), fx in fns])
            rows += len(fns)
            n = c.execute("SELECT COUNT(*) FROM test_map WHERE test = ?", (t,)).fetchone()[0]
            c.execute("INSERT OR REPLACE INTO test_map_tests (test, run_id, commit_sha, complete, outcome, functions,"
                      " updated_at) VALUES (?,?,?,?,?,?,?)",
                      (t, run_id, run.get("commit_sha"), int(complete),
                       phase_outcome((outcomes.get(t) or {}).get("phases") or {}), n, at))
    return {"tests": len(ran), "functions": rows}


def _covers(qual: str, changed: str) -> bool:
    """A recorded function is the changed definition or one defined inside it (a method of a changed class)."""
    return qual == changed or qual.startswith(changed + ".")


def affected(store: Store, changed: Iterable[tuple[str, str]], versions: Callable[[str], set[str]],
             candidates: Iterable[str] = ()) -> dict:
    """The tests the map shows running a changed function, and what it says of ``candidates`` (other test ids).

    ``changed``: (file, qualified name) of the changed definitions; ``versions(path)``: the content ids a file
    may have for the mapping to be current (its working and base versions). Tests are keyed by their
    :func:`runner_id` (the parameter sets of one test are one entry). Returns ``observed`` ({test: {"reaches":
    [file::qual], "run", "commit", "complete", "current"}}), ``not_reached`` (candidates whose every mapping is
    complete and current, whose test passed, and ran none of the changed functions; empty when a changed function
    ran in a fixture phase) and ``mapped`` (the number of tests in the map).
    """
    rows = store.all("SELECT * FROM test_map_tests ORDER BY updated_at, run_id")
    changed = [(f, q.split("#")[0]) for f, q in changed if f and q]
    hits: dict[str, set[str]] = defaultdict(set)
    shared = False   # a changed function ran in a fixture phase: tests sharing that fixture are not all mapped
    for f in sorted({f for f, _ in changed}):
        for r in store.all("SELECT test, qual, fixture FROM test_map WHERE path = ?", (f,)):
            for cf, cq in changed:
                if cf == f and _covers(r["qual"], cq):
                    hits[runner_id(r["test"])].add(f"{f}::{cq}")
                    shared = shared or bool(r["fixture"])
    variants: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        variants[runner_id(r["test"])].append(r)
    fp_cache: dict[str, set[str]] = {}

    def current(test: str) -> bool:
        for r in store.all("SELECT DISTINCT path, fingerprint FROM test_map WHERE test = ?", (test,)):
            if r["path"] not in fp_cache:
                fp_cache[r["path"]] = versions(r["path"])
            if not r["fingerprint"] or r["fingerprint"] not in fp_cache[r["path"]]:
                return False
        return True

    meta = {}
    for t in sorted(set(hits) | {runner_id(c) for c in candidates}):
        vs = variants.get(t)
        if vs:
            meta[t] = {"run": vs[-1]["run_id"], "commit": vs[-1]["commit_sha"],
                       "complete": all(v["complete"] for v in vs), "current": all(current(v["test"]) for v in vs)}
            passed = all(v["outcome"] in ("passed", "skipped") for v in vs)
            if not passed:
                meta[t]["outcome"] = next(v["outcome"] for v in vs if v["outcome"] not in ("passed", "skipped"))
    observed = {t: {"reaches": sorted(v), **meta.get(t, {})} for t, v in sorted(hits.items())}
    not_reached = [] if shared else sorted({
        c for c in candidates if runner_id(c) not in hits and meta.get(runner_id(c), {}).get("complete")
        and meta[runner_id(c)]["current"] and "outcome" not in meta[runner_id(c)]})
    return {"observed": observed, "not_reached": not_reached, "mapped": len(variants)}


def runner_id(test: str) -> str:
    """A pytest node id without its parameters (``test_x.py::test_a[1-2]`` -> ``test_x.py::test_a``): the base id
    runs every parameter set, and brackets are no shell's business."""
    head, sep, last = test.rpartition("::")
    return f"{head}{sep}{last.split('[', 1)[0]}"


_PLAIN = re.compile(r"[\w./:+=-]+")


def shell_arg(a: str) -> str:
    """``a`` quoted for the shell the command is pasted into: bare when plain, else in double quotes, which
    cmd.exe, PowerShell and POSIX shells read alike when ``a`` holds no double quote, dollar, backtick,
    backslash, exclamation or percent sign; POSIX quoting past that."""
    if _PLAIN.fullmatch(a):
        return a
    if not re.search(r'["$`\\!%]', a):
        return f'"{a}"'
    return shlex.quote(a)


def pytest_command(test_ids: Iterable[str]) -> dict | None:
    """``{"command", "by": "test" | "file", "tests"}`` that runs ``test_ids`` (Python test files only; an id that
    could be read as an option is left out); None when there is nothing to run. Past :data:`MAX_COMMAND_IDS`
    ids the command names their files (it then runs more than the listed tests)."""
    ids = list(dict.fromkeys(runner_id(t) for t in test_ids
                             if t.split("::", 1)[0].endswith(".py") and not t.startswith("-")))
    if not ids:
        return None
    by = "test"
    args = ids
    if len(ids) > MAX_COMMAND_IDS:
        by, args = "file", list(dict.fromkeys(t.split("::", 1)[0] for t in ids))
    cmd = "python -m pytest -q " + " ".join(shell_arg(a) for a in args)
    out = {"command": cmd, "by": by, "tests": len(ids)}
    if len(cmd) > MAX_COMMAND_CHARS:
        out["command"] = cmd[:MAX_COMMAND_CHARS].rsplit(" ", 1)[0]
        out["truncated"] = True
    return out


MAX_COMPACT_CHARS = 600


def compact(aff: dict) -> dict:
    """The MCP form of a review's affected tests: the counts and one command of at most
    :data:`MAX_COMPACT_CHARS` characters (by file when the ids do not fit; cut, and ``truncated``, past that)."""
    out = {"n": aff.get("total", 0), **(aff.get("by") or {})}
    cmd = aff.get("command")
    if cmd:
        text, by = cmd["command"], cmd["by"]
        if len(text) > MAX_COMPACT_CHARS and by == "test":
            files = dict.fromkeys(r["test"].split("::", 1)[0] for r in aff.get("tests") or []
                                  if r["test"].split("::", 1)[0].endswith(".py"))
            text, by = "python -m pytest -q " + " ".join(shell_arg(f) for f in files), "file"
        if len(text) > MAX_COMPACT_CHARS:
            text, out["truncated"] = text[:MAX_COMPACT_CHARS].rsplit(" ", 1)[0], True
        out.update(command=text, by=by)
    return out
