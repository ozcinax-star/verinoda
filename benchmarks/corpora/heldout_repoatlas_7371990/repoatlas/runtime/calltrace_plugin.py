"""RepoAtlas call-trace pytest plugin: which call site started which function, per test phase.

Standalone on purpose: only the standard library and pytest. RepoAtlas copies
this file *next to* (never into) the throw-away repository copy made by
:mod:`repoatlas.experiments`, puts that directory on ``PYTHONPATH`` and loads it
with ``-p repoatlas_calltrace``. It never imports RepoAtlas.

Tracer (``sys.monitoring``, Python 3.12+; docs/DESIGN.md D28):

* ``PY_START`` is the primary signal. Every start of a code object inside the
  repository copy is recorded with its *caller frame* (the call-site line), so
  functions started by C code (``sorted(key=f)``, ``map(f, xs)``) are seen too,
  attributed to the Python line that called into C. Code outside the copy
  (standard library, site-packages, a venv inside the copy, this plugin)
  returns ``DISABLE`` on its first start and costs nothing afterwards.
* ``CALL`` is enabled only on in-repo code objects and fires once per call
  site (it returns ``DISABLE``): calls into library or C code are recorded as
  *boundary* calls (``sqlite3.Connection.execute``), in-repo callees are left
  to ``PY_START``. Boundary records therefore name the first callee seen at a
  site and the context it was first seen in - not every test that made it.
* The tool id is the first free of 4, 3, 2 - never ``COVERAGE_ID`` - so a
  user's coverage keeps working. Without ``sys.monitoring`` (Python < 3.12,
  PyPy) or with no free tool id, a ``sys.setprofile`` tracer with the same
  output schema is used (slower: 4.5x CPU on 533 upstream Graphify tests, against
  1.4x for sys.monitoring).

Contexts: ``<nodeid>|setup``, ``<nodeid>|call``, ``<nodeid>|teardown`` while a
test runs, ``<collection>`` before the first test, ``<session>`` between tests.
Events on threads other than the one that loaded the plugin carry the ``thread``
flag: their context is whatever test the main thread was running.

Budgets (environment, all optional): ``REPOATLAS_TRACE_MAX_PY_START``,
``REPOATLAS_TRACE_MAX_EDGES``, ``REPOATLAS_TRACE_MAX_BYTES`` and
``REPOATLAS_TRACE_DEADLINE_S`` (seconds after plugin load). On a breach the
tracer switches itself off, the header says ``complete: false``, which budget
(``breach``) and after which context (``degraded_after``); a deadline breach
also asks pytest to stop after the current test and writes the trace at once,
so a later hard kill does not lose it.

Output: ``$REPOATLAS_ARTIFACTS/$REPOATLAS_TRACE_FILE`` (default
``calltrace.jsonl``), JSON lines: one ``header`` (schema
``repoatlas.calltrace/1``), one ``test`` record per test (phase outcomes), one
``edge`` record per distinct (caller site, callee). Paths are relative to the
copy root with ``/`` separators; ``<ext>`` marks a caller or callee outside it.
``REPOATLAS_TRACE_MODE`` = ``auto`` (default) | ``monitoring`` | ``setprofile``
| ``off`` (outcomes and timing only; the overhead baseline).
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time

import pytest

SCHEMA = "repoatlas.calltrace/1"
_T0_WALL = time.perf_counter()
_T0_CPU = time.process_time()


def _int_env(name: str, default: int) -> int:
    try:
        return int(float(os.environ.get(name, default)))
    except ValueError:
        return default


ROOT_RAW = os.path.abspath(os.environ.get("REPOATLAS_TRACE_ROOT") or os.getcwd())
ROOT = os.path.normcase(ROOT_RAW)
_ROOT_PREFIX = ROOT.rstrip("\\/") + os.sep
_SELF = os.path.normcase(os.path.abspath(__file__))
# Inside the copy but not project code: virtualenvs, tool caches, vendored installs.
_EXCLUDED = tuple(os.path.normcase(os.path.join(ROOT, d)) + os.sep
                  for d in (".venv", "venv", ".tox", ".nox", "node_modules", ".eggs"))
OUT_DIR = os.environ.get("REPOATLAS_ARTIFACTS") or os.getcwd()
OUT_FILE = os.path.join(OUT_DIR, os.path.basename(os.environ.get("REPOATLAS_TRACE_FILE") or "calltrace.jsonl"))
MODE = (os.environ.get("REPOATLAS_TRACE_MODE") or "auto").lower()
MAX_PY_START = _int_env("REPOATLAS_TRACE_MAX_PY_START", 20_000_000)
MAX_EDGES = _int_env("REPOATLAS_TRACE_MAX_EDGES", 200_000)
MAX_BYTES = _int_env("REPOATLAS_TRACE_MAX_BYTES", 20 * 1024 * 1024)
DEADLINE_S = float(os.environ.get("REPOATLAS_TRACE_DEADLINE_S") or 0) or None
EDGE_BYTES = 160   # estimated JSON bytes per edge record (before its context list)
CTX_BYTES = 6      # estimated bytes per context id in an edge record

_in_repo: dict = {}      # code object -> bool (is it repository code?)
_started: dict = {}      # code object -> bool, set on its first PY_START (local CALL events enabled)
_edges: dict = {}        # (caller code, caller line, callee code) -> [hits, set(ctx)]
_bounds: dict = {}       # (caller code, caller line, callee name) -> [hits, set(ctx)]
_threaded: set = set()   # edge keys seen on a non-main thread
_outcomes: dict = {}     # nodeid -> {phase: outcome, "duration": s}
ctx = "<collection>"
_main_ident = threading.get_ident()
_get_ident = threading.get_ident
_getframe = sys._getframe
_state = {"tracer": "off", "tool_id": None, "active": False, "complete": True, "breach": None,
          "degraded_after": None, "n_call": 0, "dropped": 0,
          "written": False, "session": None, "exitstatus": None, "stopped_early": False}
_n_start = 0     # in-repo function starts seen (hot path: a module global, not a dict entry)
_est_bytes = 0   # estimated size of the output so far


def _is_repo_file(filename: str) -> bool:
    f = os.path.normcase(filename)
    return f.startswith(_ROOT_PREFIX) and f != _SELF and not f.startswith(_EXCLUDED) \
        and (os.sep + "site-packages" + os.sep) not in f


def _repo_code(code) -> bool:
    r = _in_repo.get(code)
    if r is None:
        r = _in_repo[code] = _is_repo_file(code.co_filename)
    return r


def _qual(code) -> str:
    return getattr(code, "co_qualname", None) or code.co_name


def _breach(reason: str) -> None:
    """Switch tracing off for the rest of the session and say why."""
    if _state["breach"] is None:
        _state.update(breach=reason, complete=False, degraded_after=ctx)
    _stop_tracer()


def _note_ctx(ent: list) -> None:
    global _est_bytes
    s = ent[1]
    if ctx not in s:
        s.add(ctx)
        _est_bytes += CTX_BYTES
        if _est_bytes > MAX_BYTES:
            _breach("max_bytes")


def _new_edge(table: dict, key, extra_bytes: int) -> list | None:
    global _est_bytes
    if len(_edges) + len(_bounds) >= MAX_EDGES:
        _state["dropped"] += 1
        _breach("max_edges")
        return None
    ent = table[key] = [0, set()]
    _est_bytes += EDGE_BYTES + extra_bytes
    return ent


def _module_file(obj) -> str | None:
    mod = sys.modules.get(getattr(obj, "__module__", None) or "")
    return getattr(mod, "__file__", None)


def _callable_is_repo(callable_) -> bool:
    """True when the callable is defined in the repository (PY_START records it)."""
    f = getattr(callable_, "__func__", callable_)
    code = getattr(f, "__code__", None)
    if code is not None:
        return _repo_code(code)
    target = callable_ if isinstance(callable_, type) else type(callable_)
    fn = _module_file(target)
    return bool(fn) and _is_repo_file(fn)


def _ext_name(callable_) -> str:
    """Dotted name of a library/C callee: module.Type.method or module.function."""
    try:
        name = getattr(callable_, "__name__", None) or type(callable_).__name__
        # 3.12 method calls pass the unbound descriptor (self is arg0): name its class.
        owner = getattr(callable_, "__objclass__", None)
        self_ = getattr(callable_, "__self__", None)
        if owner is None and self_ is not None and not isinstance(self_, type(sys)):
            owner = self_ if isinstance(self_, type) else type(self_)
        if isinstance(owner, type):
            return f"{owner.__module__}.{owner.__qualname__}.{name}"
        f = getattr(callable_, "__func__", callable_)
        mod = getattr(f, "__module__", None) or type(f).__module__
        if isinstance(self_, type(sys)):
            mod = getattr(f, "__module__", None) or self_.__name__
            # C accelerator modules (_sqlite3.connect): prefer the public module that re-exports it.
            public = sys.modules.get(mod.lstrip("_")) if mod.startswith("_") else None
            if public is not None and getattr(public, name, None) is callable_:
                mod = public.__name__
        qual = getattr(f, "__qualname__", None) or type(f).__qualname__
        return f"{mod}.{qual}"
    except Exception:  # noqa: BLE001 - a hostile __getattr__ must not break the test run
        return "<unknown>"


def _record_boundary(code, line: int, callable_) -> None:
    key = (code, line or 0, _ext_name(callable_))
    ent = _bounds.get(key)
    if ent is None:
        ent = _new_edge(_bounds, key, len(key[2]))
        if ent is None:
            return
    ent[0] += 1
    _note_ctx(ent)


def _stop_tracer() -> None:
    if not _state["active"]:
        return
    _state["active"] = False
    if _state["tracer"] == "sys.monitoring":
        mon = sys.monitoring
        tool = _state["tool_id"]
        try:
            # Local CALL events stay set on repo code objects; without a callback they are no-ops.
            mon.set_events(tool, 0)
            mon.register_callback(tool, mon.events.PY_START, None)
            mon.register_callback(tool, mon.events.CALL, None)
        except (ValueError, RuntimeError):
            pass
    elif _state["tracer"] == "setprofile":
        sys.setprofile(None)
        threading.setprofile(None)


# -- sys.monitoring tracer ------------------------------------------------------------

def _start_monitoring() -> bool:
    mon = getattr(sys, "monitoring", None)
    if mon is None:
        return False
    tool = next((i for i in (4, 3, 2) if mon.get_tool(i) is None), None)
    if tool is None:
        return False
    mon.use_tool_id(tool, "repoatlas-calltrace")
    DISABLE = mon.DISABLE
    CALL = mon.events.CALL
    set_local = mon.set_local_events

    def py_start(code, offset):
        global _n_start, _est_bytes
        # Not _in_repo: a CALL event may classify a function before its first start, and
        # the local CALL events must still be enabled when it does start.
        r = _started.get(code)
        if r is None:
            r = _started[code] = _repo_code(code)
            if r:
                set_local(tool, code, CALL)
        if not r:
            return DISABLE
        _n_start += 1
        if _n_start > MAX_PY_START:
            _breach("max_py_start")
            return None
        try:
            fr = _getframe(2)   # 0 = this callback, 1 = the starting frame, 2 = its caller
        except ValueError:
            fr = None
        if fr is None:
            key = (None, 0, code)
        else:
            cc = fr.f_code
            cr = _in_repo.get(cc)
            if cr is None:
                cr = _in_repo[cc] = _is_repo_file(cc.co_filename)
            key = (cc, (fr.f_lineno or cc.co_firstlineno) if cr else 0, code)
        ent = _edges.get(key)
        if ent is None:
            ent = _new_edge(_edges, key, 0)
            if ent is None:
                return None
        ent[0] += 1
        s = ent[1]
        if ctx not in s:
            s.add(ctx)
            _est_bytes += CTX_BYTES
            if _est_bytes > MAX_BYTES:
                _breach("max_bytes")
        if _get_ident() != _main_ident:
            _threaded.add(key)
        return None

    def call(code, offset, callable_, arg0):
        # Fires once per in-repo call site (DISABLE): note calls that leave the repository.
        _state["n_call"] += 1
        try:
            if not _callable_is_repo(callable_):
                fr = _getframe(1)
                line = fr.f_lineno if fr.f_code is code else _line_of(code, offset)
                _record_boundary(code, line, callable_)
        except Exception:  # noqa: BLE001
            pass
        return DISABLE

    mon.register_callback(tool, mon.events.PY_START, py_start)
    mon.register_callback(tool, mon.events.CALL, call)
    mon.set_events(tool, mon.events.PY_START)
    _state.update(tracer="sys.monitoring", tool_id=tool, active=True)
    return True


def _line_of(code, offset: int) -> int:
    for start, end, line in code.co_lines():
        if start <= offset < end:
            return line or code.co_firstlineno
    return code.co_firstlineno


# -- sys.setprofile fallback ------------------------------------------------------------

def _start_setprofile() -> None:
    seen_sites: set = set()

    def prof(frame, event, arg):
        global _n_start
        if event == "call":
            code = frame.f_code
            back = frame.f_back
            if not _repo_code(code):
                # A Python library function called from repo code is a boundary call.
                if back is not None and _repo_code(back.f_code):
                    site = (back.f_code, back.f_lineno)
                    if site not in seen_sites:
                        seen_sites.add(site)
                        _record_boundary(back.f_code, back.f_lineno, _CodeName(code, frame))
                return
            _n_start += 1
            if _n_start > MAX_PY_START:
                _breach("max_py_start")
                return
            if back is None:
                key = (None, 0, code)
            else:
                cc = back.f_code
                key = (cc, (back.f_lineno or cc.co_firstlineno) if _repo_code(cc) else 0, code)
            ent = _edges.get(key)
            if ent is None:
                ent = _new_edge(_edges, key, 0)
                if ent is None:
                    return
            ent[0] += 1
            _note_ctx(ent)
            if _get_ident() != _main_ident:
                _threaded.add(key)
        elif event == "c_call":
            code = frame.f_code
            if _repo_code(code):
                site = (code, frame.f_lineno)
                if site not in seen_sites:
                    seen_sites.add(site)
                    _state["n_call"] += 1
                    _record_boundary(code, frame.f_lineno, arg)

    sys.setprofile(prof)
    threading.setprofile(prof)
    _state.update(tracer="setprofile", active=True)


class _CodeName:
    """Adapter so a library *code object* gets the same dotted name as a callable."""

    def __init__(self, code, frame):
        self.__name__ = code.co_name
        self.__qualname__ = _qual(code)
        self.__module__ = frame.f_globals.get("__name__", "?")


def _start() -> None:
    if MODE == "off":
        return
    if MODE in ("auto", "monitoring") and _start_monitoring():
        return
    _start_setprofile()


# -- output ------------------------------------------------------------------------------

def _rel(filename: str) -> str:
    try:
        return os.path.relpath(filename, ROOT_RAW).replace("\\", "/")
    except ValueError:  # another drive
        return filename.replace("\\", "/")


def _site(code, line) -> list:
    if code is None:
        return ["<ext>", 0, "<root>"]
    if _repo_code(code):
        return [_rel(code.co_filename), int(line or code.co_firstlineno), _qual(code)]
    return ["<ext>", 0, _qual(code)]


def _write(final: bool) -> None:
    edges = list(_edges.items())
    bounds = list(_bounds.items())
    threaded = set(_threaded)
    ctxs = sorted({c for _, (_, cs) in edges for c in list(cs)} | {c for _, (_, cs) in bounds for c in list(cs)})
    cid = {c: i for i, c in enumerate(ctxs)}
    lines: list[str] = []
    size = 0
    truncated = False
    records = []
    for key, (hits, cs) in edges:
        caller_code, line, code = key
        records.append({"k": "edge", "caller": _site(caller_code, line),
                        "callee": [_rel(code.co_filename), code.co_firstlineno, _qual(code)],
                        "hits": hits, "ctx": sorted(cid[c] for c in list(cs)),
                        **({"thread": True} if key in threaded else {})})
    for (caller_code, line, name), (hits, cs) in bounds:
        records.append({"k": "edge", "caller": _site(caller_code, line), "callee": ["<ext>", 0, name],
                        "hits": hits, "ctx": sorted(cid[c] for c in list(cs)), "boundary": True})
    records.sort(key=lambda r: (r["caller"][0], r["caller"][1], r["callee"][0], r["callee"][2]))
    for rec in records:
        text = json.dumps(rec, separators=(",", ":"))
        if size + len(text) + 1 > MAX_BYTES:
            truncated = True
            break
        lines.append(text)
        size += len(text) + 1
    if truncated and _state["breach"] is None:
        _state.update(breach="max_bytes", complete=False, degraded_after=ctx)
    tests = [json.dumps({"k": "test", "id": nid, **res}, separators=(",", ":"))
             for nid, res in sorted(_outcomes.items())]
    header = {
        "k": "header", "schema": SCHEMA, "final": final, "tracer": _state["tracer"], "mode": MODE,
        "tool_id": _state["tool_id"], "python": sys.version.split()[0],
        "implementation": sys.implementation.name, "root": ROOT_RAW,
        "complete": bool(_state["complete"]) and final, "breach": _state["breach"],
        "degraded_after": _state["degraded_after"], "stopped_early": _state["stopped_early"],
        "exitstatus": _state["exitstatus"],
        "limits": {"max_py_start": MAX_PY_START, "max_edges": MAX_EDGES, "max_bytes": MAX_BYTES,
                   "deadline_s": DEADLINE_S},
        "stats": {"py_start": _n_start, "call_sites": _state["n_call"], "edges": len(edges),
                  "boundary": len(bounds), "written_records": len(lines), "dropped": _state["dropped"],
                  "est_bytes": _est_bytes, "bytes": size},
        "cpu_s": round(time.process_time() - _T0_CPU, 3), "wall_s": round(time.perf_counter() - _T0_WALL, 3),
        "contexts": ctxs, "tests": len(tests),
    }
    if not final and _state["breach"] is None:
        header["complete"] = False
    data = "\n".join([json.dumps(header, separators=(",", ":")), *tests, *lines]) + "\n"
    tmp = OUT_FILE + ".tmp"
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write(data)
    os.replace(tmp, OUT_FILE)
    _state["written"] = True


def _on_deadline() -> None:
    _breach("timeout")
    _state["stopped_early"] = True
    sess = _state["session"]
    if sess is not None:
        sess.shouldstop = "repoatlas call trace: deadline reached"
    try:
        _write(final=False)  # survive a hard kill of the process tree
    except (OSError, RuntimeError):
        pass


# -- pytest hooks ------------------------------------------------------------------------

def _set_ctx(value: str) -> None:
    global ctx
    ctx = value


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    _set_ctx(item.nodeid + "|setup")
    yield


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    _set_ctx(item.nodeid + "|call")
    yield


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item):
    _set_ctx(item.nodeid + "|teardown")
    yield


def pytest_runtest_logfinish(nodeid, location):
    _set_ctx("<session>")


def pytest_runtest_logreport(report):
    res = _outcomes.setdefault(report.nodeid, {"phases": {}, "duration": 0.0})
    res["phases"][report.when] = report.outcome
    res["duration"] = round(res["duration"] + float(getattr(report, "duration", 0.0) or 0.0), 4)


def pytest_sessionstart(session):
    _state["session"] = session


@pytest.hookimpl(tryfirst=True)
def pytest_sessionfinish(session, exitstatus):
    if not ACTIVE_ON_IMPORT:
        return
    _stop_tracer()
    if _state["tracer"] == "sys.monitoring":
        try:
            sys.monitoring.free_tool_id(_state["tool_id"])
        except (ValueError, RuntimeError):
            pass
    _state["exitstatus"] = int(exitstatus)
    if int(exitstatus) == 2:  # interrupted: not every selected test ran
        _state["stopped_early"] = True
    _write(final=True)


def pytest_unconfigure(config):
    if not ACTIVE_ON_IMPORT:
        return
    _stop_tracer()
    if not _state["written"]:
        _write(final=True)


# Trace only when loaded as the pytest plugin copied next to an experiment copy. Imported as
# repoatlas.runtime.calltrace_plugin (inside RepoAtlas itself, by a module walker, ...) it does
# nothing: tracing the RepoAtlas process would be wrong and would hold a sys.monitoring tool id.
ACTIVE_ON_IMPORT = __name__ != "repoatlas.runtime.calltrace_plugin"
if ACTIVE_ON_IMPORT:
    _start()
    if DEADLINE_S and MODE != "off":
        _timer = threading.Timer(DEADLINE_S, _on_deadline)
        _timer.daemon = True
        _timer.start()
