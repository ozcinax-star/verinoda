"""Observe which call sites start which functions while selected tests run (DESIGN D28).

:func:`observe` runs ``python -m pytest -p verinoda_calltrace <test ids>`` with
the project's interpreter (:func:`verinoda.experiments.python_for`) through
:func:`verinoda.experiments.run` - so it gets the same throw-away copy,
scrubbed environment, allowlist policy and tree-killing timeout as any
experiment - and ingests the trace file the plugin leaves in the run's
artifacts:

* ``runtime_runs``: one row per run (experiment id, snapshot, commit, the
  plugin header, ``complete``, sha256 of the trace file);
* ``runtime_calls``: one row per distinct (caller site, callee), with hit
  count, the ``<nodeid>|<phase>`` contexts that observed it and flags.

What an observation means (and does not):

* ``outcome: pass`` on a ``call_trace`` evidence record means *the edge was
  observed* in run R at commit C - whatever the tests' own verdict. It is an
  existential, run-scoped fact: it never supports an "always" claim.
* Edges flagged ``test_double`` (production code calling into code defined in a
  test file, or into ``unittest.mock``) never support a production edge.
* "Test T did not reach X" needs a complete trace in which T's call phase was
  recorded (:func:`reach_evidence`); otherwise the answer is inconclusive.
* Boundary calls (into library or C code) are recorded once per call site, with
  the context they were first seen in (``first_seen_only``).
* Child processes started by tests are not traced; events on other threads
  carry the ``thread`` flag because their test attribution is uncertain.

Edge flags (``runtime_calls.flags``): ``boundary`` (callee outside the
repository), ``ext_caller`` (started by library/C code, e.g. pytest calling a
test), ``thread``, ``test_double``, ``anonymous`` (lambda, generator
expression) / ``nested`` (a function defined inside another), ``via_c`` (the
call line does not name the callee but calls C code that did, e.g.
``sorted(items)`` -> ``Item.__lt__``) and ``indirect`` (the call line does not
name the callee: a call through a variable, a decorator wrapper, a dunder or
property).
"""

from __future__ import annotations

import ast
import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Iterable

from verinoda import evidence as evmod
from verinoda import testcode
from verinoda.store import Store, new_id, now

# test code, by the one rule of verinoda.testcode (tests/, testing/, test_x.py, x_test.py, conftest.py ...)
is_test_path = testcode.is_test_file

PLUGIN_MODULE = "verinoda_calltrace"
TRACE_FILE = "calltrace.jsonl"
SCHEMA = "verinoda.calltrace/1"
MAX_TESTS = 50
DEFAULT_LIMITS = {"max_py_start": 20_000_000, "max_edges": 200_000, "max_bytes": 20 * 1024 * 1024}
MODES = ("auto", "monitoring", "setprofile", "off")
MAX_RESULT_EDGES = 300       # edges returned inline (all of them are in runtime_calls)
MAX_REACH_PER_TEST = 200
MAX_EVIDENCE = 200
EXT = "<ext>"
MOCK_PREFIXES = ("unittest.mock.", "mock.", "pytest_mock.")
LIMITS_TEXT = [
    "observation is run-scoped: it shows what these tests executed at this commit, never what always happens",
    "child processes started by tests are not traced",
    "boundary (library/C) calls are recorded once per call site, with the first test that made them",
    "events on non-main threads are attributed to the test the main thread was running (flag 'thread')",
]


def plugin_source() -> bytes:
    """Bytes of the standalone pytest plugin copied next to the experiment copy."""
    return (Path(__file__).with_name("calltrace_plugin.py")).read_bytes()


# -- test selection ------------------------------------------------------------------------

def _clean_label(label: str) -> str:
    return label.strip().lstrip(".").split("(")[0].strip()


def _is_test_function(g, n: str) -> bool:
    """A pytest test (the tracer runs pytest): a Python test by :func:`verinoda.testcode.is_test_function`."""
    return (g.file(n) or "").endswith(".py") and testcode.is_test_function(g, n)


def pytest_id(g, n: str) -> str:
    """``file::test`` or ``file::Class::test`` for a test-function node."""
    name = _clean_label(g.label(n)).rpartition(".")[2]
    cls = next((u for u, _ in g.in_edges(n, {"method"}) if g.file(u) == g.file(n)), None)
    return f"{g.file(n)}::{_clean_label(g.label(cls))}::{name}" if cls else f"{g.file(n)}::{name}"


def select_tests(g, symbols: Iterable[str], *, terms: Iterable[str] = (), limit: int = MAX_TESTS,
                 depth: int = 3) -> list[str]:
    """Pytest node ids of tests likely to run ``symbols``, at most ``limit`` (<= 50).

    First the tests that statically reach a symbol - the rule of
    :func:`verinoda.architecture_map.tests_view`: call/uses/references edges
    and the calls through a dotted module path the graph does not hold
    (:func:`verinoda.testcode.extra_callers`), depth ``depth``, only through
    non-test code - nearest first; then test
    functions whose name contains a symbol name or one of ``terms`` (3+
    characters). Static reachability is only a *selection* heuristic: the
    trace decides what really ran.
    """
    limit = max(1, min(int(limit), MAX_TESTS))
    targets: list[str] = []
    for s in symbols:
        nid = s if s in g.G else g.resolve(s)[0]
        if nid and nid not in targets:
            targets.append(nid)
    found: dict[str, int] = {}
    extra = testcode.extra_callers(g)   # calls the graph does not hold: pkg.main.run() in a test
    for t in targets:
        frontier, seen = {t}, {t}
        for dist in range(1, depth + 1):
            nxt = set()
            for v in frontier:
                callers = [u for u, _ in g.in_edges(v, {"calls", "uses", "references"})]
                callers += [x.node for x in extra.get(v, ()) if x.node is not None]
                for u in callers:
                    if u in seen:
                        continue
                    seen.add(u)
                    if _is_test_function(g, u):
                        found[u] = min(found.get(u, dist), dist)
                    elif g.file(u) and not testcode.is_test_code(g, u):
                        nxt.add(u)
            frontier = nxt
    ordered = [n for n, _ in sorted(found.items(), key=lambda kv: (kv[1], pytest_id(g, kv[0])))]
    words = {_clean_label(g.label(t)).rpartition(".")[2].lower() for t in targets}
    words |= {w.lower() for w in terms if len(w) >= 3}
    words = {w for w in words if len(w) >= 3}
    if words:
        by_name = sorted((n for n in g.G.nodes if _is_test_function(g, n) and n not in found
                          and any(w in _clean_label(g.label(n)).lower() for w in words)),
                         key=lambda n: pytest_id(g, n))
        ordered += by_name
    out: list[str] = []
    for n in ordered:
        pid = pytest_id(g, n)
        if pid not in out:
            out.append(pid)
        if len(out) >= limit:
            break
    return out


# -- trace file ----------------------------------------------------------------------------

def parse_trace(data: bytes) -> dict:
    """``{"header": {...}, "tests": {nodeid: {...}}, "edges": [...]}`` from plugin JSON lines."""
    header: dict = {}
    tests: dict[str, dict] = {}
    edges: list[dict] = []
    for raw in data.decode("utf-8", "replace").splitlines():
        if not raw.strip():
            continue
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        k = rec.get("k")
        if k == "header":
            header = rec
        elif k == "test":
            tests[rec["id"]] = {"phases": rec.get("phases", {}), "duration": rec.get("duration")}
        elif k == "edge":
            edges.append(rec)
    if header.get("schema") not in (None, SCHEMA):
        raise ValueError(f"unsupported trace schema {header.get('schema')!r}")
    ctxs = header.get("contexts", [])
    for e in edges:
        e["contexts"] = [ctxs[i] for i in e.get("ctx", []) if 0 <= i < len(ctxs)]
    return {"header": header, "tests": tests, "edges": edges}


def phase_outcome(phases: dict) -> str:
    """One word per test: failed/error if any phase failed, else the call phase's outcome."""
    if phases.get("setup") == "failed" or phases.get("teardown") == "failed":
        return "error"
    return phases.get("call") or phases.get("setup") or "unknown"


def _ctx_test(ctx: str) -> str | None:
    nid, sep, _ = ctx.rpartition("|")
    return nid if sep and not ctx.startswith("<") else None


# -- source helpers ------------------------------------------------------------------------

class _Sources:
    """Per-call cache of the user's files (lines + AST), read lazily."""

    def __init__(self, repo: Path):
        self.repo = repo
        self._lines: dict[str, list[str] | None] = {}
        self._trees: dict[str, ast.AST | None] = {}

    def lines(self, rel: str) -> list[str] | None:
        if rel not in self._lines:
            try:
                self._lines[rel] = (self.repo / rel).read_text(encoding="utf-8", errors="replace").splitlines()
            except (OSError, ValueError):
                self._lines[rel] = None
        return self._lines[rel]

    def line(self, rel: str, n: int) -> str | None:
        ls = self.lines(rel)
        return ls[n - 1] if ls and 0 < n <= len(ls) else None

    def tree(self, rel: str) -> ast.AST | None:
        if rel not in self._trees:
            ls = self.lines(rel)
            try:
                self._trees[rel] = ast.parse("\n".join(ls)) if ls is not None else None
            except (SyntaxError, ValueError):
                self._trees[rel] = None
        return self._trees[rel]

    def def_line(self, rel: str, first_line: int) -> int:
        """``co_firstlineno`` is the first decorator line; return the ``def``/``class`` line."""
        tree = self.tree(rel)
        if tree is None:
            return first_line
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                first = min([node.lineno] + [d.lineno for d in node.decorator_list])
                if first == first_line:
                    return node.lineno
        return first_line

    def test_def_line(self, nodeid: str) -> tuple[str, int] | None:
        """(file, def line) of a pytest node id (``file::[Class::]name[params]``)."""
        parts = nodeid.split("::")
        rel, names = parts[0], [p.split("[")[0] for p in parts[1:]]
        tree = self.tree(rel)
        if tree is None or not names:
            return None
        scope: list = list(getattr(tree, "body", []))
        node = None
        for name in names:
            node = next((n for n in scope if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                         and n.name == name), None)
            if node is None:
                return None
            scope = list(node.body)
        return (rel, node.lineno) if node is not None else None


def _short_name(qual: str) -> str:
    name = qual.rpartition(".")[2]
    if name in ("__init__", "__new__", "__call__") and "." in qual:
        return qual.rsplit(".", 2)[-2]  # Class.__init__ is spelled Class(...) at the call site
    return name


def _edge_flags(e: dict, src: _Sources, boundary_sites: set) -> dict:
    caller_path, caller_line, _ = e["caller"]
    callee_path, _, callee_qual = e["callee"]
    flags: dict = {}
    if e.get("thread"):
        flags["thread"] = True
    if e.get("boundary"):
        flags["boundary"] = True
    if caller_path == EXT:
        flags["ext_caller"] = True
    if "<" in callee_qual and not callee_qual.endswith("<module>"):
        flags["anonymous" if "<locals>" not in callee_qual or callee_qual.endswith(">") else "nested"] = True
    prod_caller = caller_path != EXT and not is_test_path(caller_path)
    if (callee_path != EXT and is_test_path(callee_path) and prod_caller) or \
            (e.get("boundary") and callee_qual.startswith(MOCK_PREFIXES)):
        flags["test_double"] = True
    if caller_path != EXT and not e.get("boundary") and not callee_qual.endswith("<module>"):
        text = src.line(caller_path, int(caller_line or 0)) or ""
        if _short_name(callee_qual) not in text:
            flags["via_c" if (caller_path, caller_line) in boundary_sites else "indirect"] = True
    return flags


# -- graph mapping -------------------------------------------------------------------------

def _file_node(g, rel: str) -> str | None:
    for n, d in g.G.nodes(data=True):
        if d.get("source_file") == rel and g.is_file_node(n):
            return n
    return None


class _Mapper:
    def __init__(self, g, src: _Sources):
        self.g, self.src = g, src
        self._cache: dict = {}
        self._files: dict[str, str | None] = {}

    def callee(self, path: str, line: int, qual: str) -> str | None:
        if self.g is None or path == EXT:
            return None
        key = ("callee", path, line, qual)
        if key not in self._cache:
            self._cache[key] = self._callee(path, line, qual)
        return self._cache[key]

    def _callee(self, path: str, line: int, qual: str) -> str | None:
        g = self.g
        name = qual.rpartition(".")[2]
        if name == "<module>":
            if path not in self._files:
                self._files[path] = _file_node(g, path)
            return self._files[path]
        if name.startswith("<"):
            return None
        def_line = self.src.def_line(path, line)
        cands = [n for n in g.symbols_in(path) if _clean_label(g.label(n)).rpartition(".")[2] == name]
        if len(cands) == 1:
            return cands[0]
        exact = [n for n in cands if g.line(n) in (line, def_line)]
        return exact[0] if len(exact) == 1 else None

    def site(self, path: str, line: int) -> str | None:
        if self.g is None or path == EXT or not line:
            return None
        return self.g.symbol_at(path, int(line))


# -- ingest --------------------------------------------------------------------------------

def ingest(store: Store, trace: dict, *, experiment_id: str | None, snapshot_id: str | None,
           commit: str | None, trace_sha256: str | None, extra_header: dict | None = None,
           flags: dict[int, dict] | None = None) -> str:
    """Record a parsed trace; returns the ``runtime_runs`` id."""
    header = dict(trace["header"])
    header.pop("contexts", None)
    header["tests_outcomes"] = trace["tests"]
    header.update(extra_header or {})
    rid = new_id("rtr")
    rows = []
    for i, e in enumerate(trace["edges"]):
        (cp, cl, cq), (tp, tl, tq) = e["caller"], e["callee"]
        rows.append((rid, cp, int(cl or 0), cq, tp, (int(tl) if tp != EXT and tl else None), tq,
                     int(e.get("hits", 0)), json.dumps(e.get("contexts", [])),
                     json.dumps((flags or {}).get(i, {}), sort_keys=True)))
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO runtime_runs (id, experiment_id, snapshot_id, commit_sha, header, complete, trace_sha256,"
            " created_at) VALUES (?,?,?,?,?,?,?,?)",
            (rid, experiment_id, snapshot_id, commit, json.dumps(header, sort_keys=True),
             int(bool(header.get("complete"))), trace_sha256, now()))
        conn.executemany(
            "INSERT INTO runtime_calls (run_id, caller_path, caller_line, caller_qual, callee_path, callee_line,"
            " callee_qual, hits, tests, flags) VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    return rid


def load_run(store: Store, run_id: str) -> dict | None:
    """The run row plus its calls (``tests`` = contexts, ``flags`` decoded)."""
    run = store.one("SELECT * FROM runtime_runs WHERE id = ?", (run_id,))
    if run is None:
        return None
    run["calls"] = store.all("SELECT * FROM runtime_calls WHERE run_id = ? ORDER BY rowid", (run_id,))
    return run


def latest_run(store: Store, *, complete_only: bool = False) -> dict | None:
    sql = "SELECT id FROM runtime_runs" + (" WHERE complete = 1" if complete_only else "") \
        + " ORDER BY created_at DESC, rowid DESC LIMIT 1"
    row = store.one(sql)
    return load_run(store, row["id"]) if row else None


def tests_reaching(store: Store, run_id: str, path: str, qual: str) -> dict[str, list[str]]:
    """{test id: [phases]} in which ``path``/``qual`` was started (any caller)."""
    out: dict[str, set[str]] = defaultdict(set)
    for r in store.all("SELECT tests FROM runtime_calls WHERE run_id = ? AND callee_path = ? AND callee_qual = ?",
                       (run_id, path, qual)):
        for ctx in r["tests"]:
            nid = _ctx_test(ctx)
            if nid:
                out[nid].add(ctx.rpartition("|")[2])
    return {k: sorted(v) for k, v in sorted(out.items())}


# -- evidence ------------------------------------------------------------------------------

def _run_meta(run: dict) -> dict:
    h = run.get("header") or {}
    return {"run_id": run["id"], "experiment_id": run.get("experiment_id"), "trace_sha256": run.get("trace_sha256"),
            "tracer": h.get("tracer"), "complete": bool(run.get("complete")), "commit": run.get("commit_sha")}


def edge_evidence(repo: Path, run: dict, call: dict, *, src: _Sources | None = None,
                  caller_node: str | None = None, callee_node: str | None = None) -> dict | None:
    """``call_trace`` evidence for one observed ``runtime_calls`` row (not inserted).

    Anchored on the caller's call-site line (path, line, content hash), so it
    goes stale like any source citation. ``meta.outcome == 'pass'`` means the
    edge was observed. None when the call site is outside the repository or
    its line can no longer be cited.
    """
    repo = Path(repo).resolve()
    src = src or _Sources(repo)
    if call["caller_path"] == EXT or not call["caller_line"]:
        return None
    contexts = call["tests"] if isinstance(call["tests"], list) else json.loads(call["tests"] or "[]")
    tests = sorted({t for t in (_ctx_test(c) for c in contexts) if t})
    outcomes = ((run.get("header") or {}).get("tests_outcomes") or {})
    flags = call["flags"] if isinstance(call["flags"], dict) else json.loads(call["flags"] or "{}")
    external = call["callee_path"] == EXT
    meta = {
        "kind": "call_trace", **_run_meta(run),
        "callee_path": None if external else call["callee_path"],
        "callee_def_line": None if external else call["callee_line"],
        "callee_qual": call["callee_qual"], "external_callee": external,
        "caller_node": caller_node, "callee_node": callee_node,
        "tests": tests[:10], "n_tests": len(tests),
        "test_outcomes": {t: phase_outcome((outcomes.get(t) or {}).get("phases", {})) for t in tests[:10]},
        "hits": call["hits"], "flags": flags, "outcome": "pass",
        "scope": "existential: observed in this run at this commit; never evidence for 'always'",
    }
    if flags.get("boundary"):
        meta["first_seen_only"] = True
    if not external and call["callee_line"]:
        text = src.line(call["callee_path"], src.def_line(call["callee_path"], int(call["callee_line"])))
        meta["callee_def_hash"] = evmod.content_hash(text) if text and text.strip() else None
    ev = evmod.source_evidence(repo, call["caller_path"], int(call["caller_line"]), commit=run.get("commit_sha"),
                               source_type="experiment", meta=meta)
    if ev is None:
        return None
    target = call["callee_qual"] if external else f"{call['callee_path']}:{call['callee_line']} {call['callee_qual']}"
    ev["locator"] = f"run {run['id']}: {call['caller_path']}:{call['caller_line']} -> {target}"
    return ev


def reach_evidence(store: Store, repo: Path, run_id: str, test_id: str, target_path: str, target_qual: str, *,
                   hypothesis: str = "reached") -> dict | None:
    """Evidence for "test T reached X" (``hypothesis='reached'``) or its absence (``'not_reached'``).

    ``meta.outcome``: ``pass`` when the run shows the hypothesis, ``fail``
    when it shows the opposite, ``inconclusive`` when it cannot tell - the
    absence of X needs a complete trace in which T's call phase ran. Anchored
    on the test function's ``def`` line. Not inserted; None if that line
    cannot be cited.
    """
    if hypothesis not in ("reached", "not_reached"):
        raise ValueError("hypothesis must be 'reached' or 'not_reached'")
    run = store.one("SELECT * FROM runtime_runs WHERE id = ?", (run_id,))
    if run is None:
        return None
    repo = Path(repo).resolve()
    src = _Sources(repo)
    phases = tests_reaching(store, run_id, target_path, target_qual).get(test_id, [])
    ran = ((run["header"] or {}).get("tests_outcomes") or {}).get(test_id, {}).get("phases", {})
    reached = bool(phases)
    why = None
    if reached:
        outcome = "pass" if hypothesis == "reached" else "fail"
    elif not ran.get("call"):
        outcome, why = "inconclusive", "the test's call phase did not run in this trace"
    elif not run["complete"]:
        outcome, why = "inconclusive", "the trace is incomplete (a budget stopped it)"
    else:
        outcome = "fail" if hypothesis == "reached" else "pass"
    loc = src.test_def_line(test_id)
    if loc is None:
        return None
    meta = {"kind": "call_trace_reach", **_run_meta(run), "test": test_id, "hypothesis": hypothesis,
            "target_path": target_path, "target_qual": target_qual, "reached_in_phases": phases,
            "test_phases": ran, "outcome": outcome,
            "scope": "run-scoped: what this test executed in this run at this commit"}
    if why:
        meta["inconclusive_reason"] = why
    ev = evmod.source_evidence(repo, loc[0], loc[1], commit=run.get("commit_sha"), source_type="experiment",
                               meta=meta)
    if ev is not None:
        verb = "reached" if reached else "did not reach"
        ev["locator"] = f"run {run_id}: {test_id} {verb} {target_path}::{target_qual}"
    return ev


# -- observe -------------------------------------------------------------------------------

def _snapshot_row(store: Store, snapshot) -> dict | None:
    if isinstance(snapshot, dict):
        return snapshot
    if isinstance(snapshot, str):
        return store.snapshot(snapshot)
    return store.latest_snapshot()


def _load_graph(repo: Path):
    try:
        from verinoda import index
        from verinoda.paths import graph_path

        return index.load(repo) if graph_path(repo).is_file() else None
    except Exception:  # noqa: BLE001 - mapping to graph nodes is optional
        return None


def _resolve_targets(g, targets: Iterable[str]) -> list[tuple[str, str | None, str | None]]:
    """(target as given, file, bare name) for node ids / labels / ``file::symbol``."""
    out = []
    for t in targets:
        nid = None
        if g is not None:
            nid = t if t in g.G else g.resolve(t)[0]
        if nid is not None:
            out.append((nid, g.file(nid), _clean_label(g.label(nid)).rpartition(".")[2]))
        else:
            f, _, sym = t.rpartition("::")
            out.append((t, f or None, _clean_label(sym or t).rpartition(".")[2]))
    return out


def observe(store: Store, repo: Path, test_ids: Iterable[str], *, timeout: float | None = None,
            snapshot=None, graph=None, targets: Iterable[str] = (), mode: str = "auto",
            limits: dict | None = None, max_evidence: int = MAX_EVIDENCE) -> dict:
    """Run ``test_ids`` (<= 50; empty = the whole suite) under the call tracer and ingest the trace.

    Returns ``run_id``, ``complete`` (+ ``completeness`` details), observed
    in-repo ``edges`` mapped to graph node ids where possible, per-test
    ``reach`` sets, ``boundary`` calls and ``evidence`` - ``call_trace``
    evidence dicts (not inserted: attach the relevant ones with
    ``Claims.attach``), edges into ``targets`` first.
    """
    from verinoda import experiments
    from verinoda.paths import load_config
    from verinoda.snapshot import git

    repo = Path(repo).resolve()
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    requested = [str(t).replace("\\", "/") for t in test_ids]
    ids = list(dict.fromkeys(requested))[:MAX_TESTS]
    truncated = [t for t in dict.fromkeys(requested) if t not in ids]
    cfg = load_config(repo)["experiments"]
    timeout = float(timeout or 2 * float(cfg["default_timeout"]))
    grace = max(2.0, min(30.0, 0.15 * timeout))
    lim = {**DEFAULT_LIMITS, **(limits or {})}
    snap = _snapshot_row(store, snapshot)
    # The commit that is checked out now is what runs (the copy is made from the working tree);
    # edits since the snapshot are reported per traced file (files_changed_since_snapshot).
    commit = (git(repo, "rev-parse", "HEAD") or "").strip() or (snap or {}).get("commit_sha")
    env = {"VERINODA_TRACE_MODE": mode, "VERINODA_TRACE_FILE": TRACE_FILE,
           "VERINODA_TRACE_DEADLINE_S": f"{max(1.0, timeout - grace):.1f}",
           "VERINODA_TRACE_MAX_PY_START": str(int(lim["max_py_start"])),
           "VERINODA_TRACE_MAX_EDGES": str(int(lim["max_edges"])),
           "VERINODA_TRACE_MAX_BYTES": str(int(lim["max_bytes"]))}
    argv = [experiments.python_for(repo), "-m", "pytest", "-q", "-p", PLUGIN_MODULE, "-p", "no:cacheprovider",
            *ids]
    scope = f"{len(ids)} selected test id(s)" if ids else "whole test suite"
    base = {"run_id": None, "complete": False, "scope": scope, "tests_requested": ids,
            "truncated_ids": truncated, "limits": list(LIMITS_TEXT)}
    try:
        exp = experiments.run(store, repo, argv, hypothesis=f"observe calls made by {scope}", commit=commit,
                              timeout=timeout, plugins={f"{PLUGIN_MODULE}.py": plugin_source()}, env_extra=env)
    except experiments.ExperimentRefused as exc:
        return {**base, "error": f"refused: {exc}",
                "next_step": "check the test ids (paths must stay inside the repository) or enable a container"}
    except OSError as exc:
        return {**base, "error": f"could not start the test run: {exc}",
                "next_step": "check the project's Python interpreter (experiments.python_for)"}
    trace_path = (exp.get("artifacts") or {}).get(TRACE_FILE)
    data = Path(trace_path).read_bytes() if trace_path and Path(trace_path).is_file() else None
    snap_id = (snap or {}).get("id")
    if data is None:
        why = (f"test run {exp['outcome']} and the tracer wrote no trace"
               + (f" ({exp.get('inconclusive_reason')})" if exp.get("inconclusive_reason") else ""))
        rid = ingest(store, {"header": {"schema": SCHEMA, "complete": False, "error": why}, "tests": {}, "edges": []},
                     experiment_id=exp["id"], snapshot_id=snap_id, commit=commit, trace_sha256=None,
                     extra_header={"requested": ids})
        return {**base, "run_id": rid, "experiment_id": exp["id"], "outcome": exp["outcome"], "error": why,
                "test_run_evidence_id": exp["evidence_id"], "logs": exp["logs"],
                "next_step": "read the run's stderr log; fix the test environment and observe again"}
    sha = hashlib.sha256(data).hexdigest()
    trace = parse_trace(data)
    h = trace["header"]
    src = _Sources(repo)
    boundary_sites = {(e["caller"][0], e["caller"][1]) for e in trace["edges"] if e.get("boundary")}
    flags = {i: _edge_flags(e, src, boundary_sites) for i, e in enumerate(trace["edges"])}
    session_complete = h.get("exitstatus") in (0, 1) and not h.get("stopped_early") and exp["outcome"] != "timeout"
    complete = bool(h.get("complete")) and bool(h.get("final")) and session_complete and not truncated
    changed = _changed_since_snapshot(store, repo, snap, trace["edges"])
    extra = {"requested": ids, "truncated_ids": truncated, "session_complete": session_complete,
             "complete_trace": bool(h.get("complete")), "experiment_outcome": exp["outcome"],
             "files_changed_since_snapshot": changed, "trace_path": trace_path}
    trace["header"] = {**h, "complete": complete}
    rid = ingest(store, trace, experiment_id=exp["id"], snapshot_id=snap_id, commit=commit, trace_sha256=sha,
                 extra_header=extra, flags=flags)
    run = load_run(store, rid)
    g = (graph if graph is not None else _load_graph(repo)) or None  # graph=False: skip node mapping
    mapper = _Mapper(g, src)
    tset = _resolve_targets(g, targets) if targets else []
    return _result(repo, run, trace, flags, mapper, src, tset, exp, base, max_evidence)


def _changed_since_snapshot(store: Store, repo: Path, snap: dict | None, edges: list[dict]) -> list[str]:
    """Traced files whose current content differs from the snapshot the run is filed under."""
    if not snap:
        return []
    from verinoda.snapshot import sha256_file

    recorded = store.snapshot_files(snap["id"])
    files = {p for e in edges for p in (e["caller"][0], e["callee"][0]) if p != EXT}
    out = []
    for rel in sorted(files):
        try:
            cur = sha256_file(repo / rel)
        except OSError:
            cur = None
        if recorded.get(rel) != cur:
            out.append(rel)
    return out


def _result(repo, run, trace, flags, mapper, src, tset, exp, base, max_evidence) -> dict:
    h = run["header"]
    calls = run["calls"]
    target_keys = {(f, name) for _, f, name in tset}
    edges_out, boundary_out, evidence, target_ev = [], [], [], []
    reach: dict[str, set[str]] = defaultdict(set)
    reach_by_key: dict[tuple, set[str]] = defaultdict(set)   # (callee path, bare name) -> tests
    reach_by_node: dict[str, set[str]] = defaultdict(set)    # callee node id -> tests
    for call in calls:
        tests = sorted({t for t in (_ctx_test(c) for c in call["tests"]) if t})
        callee_node = mapper.callee(call["callee_path"], call["callee_line"] or 0, call["callee_qual"])
        caller_node = mapper.site(call["caller_path"], call["caller_line"])
        if call["callee_path"] == EXT:
            boundary_out.append({"site": f"{call['caller_path']}:{call['caller_line']}", "caller_node": caller_node,
                                 "callee": call["callee_qual"], "tests": tests[:10], "first_seen_only": True,
                                 **({"flags": call["flags"]} if len(call["flags"]) > 1 else {})})
        else:
            label = callee_node or f"{call['callee_path']}::{call['callee_qual']}"
            for t in tests:
                reach[t].add(label)
            reach_by_key[(call["callee_path"], call["callee_qual"].rpartition(".")[2])].update(tests)
            if callee_node:
                reach_by_node[callee_node].update(tests)
            edges_out.append({
                "caller": {"path": call["caller_path"], "line": call["caller_line"], "qual": call["caller_qual"],
                           "node": caller_node},
                "callee": {"path": call["callee_path"], "line": call["callee_line"], "qual": call["callee_qual"],
                           "node": callee_node},
                "hits": call["hits"], "tests": tests[:10], "n_tests": len(tests), "flags": call["flags"]})
        is_target = (call["callee_path"], call["callee_qual"].rpartition(".")[2]) in target_keys \
            or (callee_node is not None and any(callee_node == t for t, _, _ in tset))
        ev = None
        if is_target or len(evidence) < max_evidence:
            ev = edge_evidence(repo, run, call, src=src, caller_node=caller_node, callee_node=callee_node)
        if ev is not None:
            (target_ev if is_target else evidence).append(ev)
    evidence = (target_ev + evidence)[:max(max_evidence, len(target_ev))]
    tests = {t: phase_outcome(v.get("phases", {})) for t, v in trace["tests"].items()}
    completeness = {"trace_complete": h.get("complete_trace"), "session_complete": h.get("session_complete"),
                    "breach": h.get("breach"), "degraded_after": h.get("degraded_after"),
                    "stopped_early": h.get("stopped_early"), "truncated_ids": h.get("truncated_ids"),
                    "tests_run": len(tests), "experiment_outcome": exp["outcome"]}
    limits = list(base["limits"])
    if h.get("files_changed_since_snapshot"):
        limits.append("files changed since the snapshot: " + ", ".join(h["files_changed_since_snapshot"][:10]))
    if h.get("tracer") == "setprofile":
        limits.append("sys.monitoring unavailable: traced with sys.setprofile (same schema, higher overhead)")
    target_reach = {t: sorted(reach_by_node.get(t, set()) | reach_by_key.get((f, name), set()))
                    for t, f, name in tset}
    edges_out.sort(key=lambda e: (e["caller"]["path"], e["caller"]["line"] or 0, e["callee"]["path"]))
    return {
        **base, "run_id": run["id"], "experiment_id": exp["id"], "outcome": exp["outcome"],
        "complete": bool(run["complete"]), "completeness": completeness,
        "tracer": h.get("tracer"), "python": h.get("python"), "commit": run.get("commit_sha"),
        "snapshot_id": run.get("snapshot_id"), "trace_sha256": run.get("trace_sha256"),
        "trace_path": h.get("trace_path"), "duration_s": exp["duration_s"],
        "cost": {"cpu_s": h.get("cpu_s"), "wall_s": h.get("wall_s"), "stats": h.get("stats")},
        "tests": tests,
        "edges": edges_out[:MAX_RESULT_EDGES], "edges_total": len(edges_out),
        "reach": {t: sorted(v)[:MAX_REACH_PER_TEST] for t, v in sorted(reach.items())},
        "target_reach": target_reach,
        "boundary": boundary_out[:MAX_RESULT_EDGES], "boundary_total": len(boundary_out),
        "evidence": evidence, "test_run_evidence_id": exp["evidence_id"], "logs": exp["logs"],
        "limits": limits,
    }
