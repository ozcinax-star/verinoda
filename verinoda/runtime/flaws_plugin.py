"""Verinoda runtime-flaws pytest plugin: SQL statements and sampled stacks, per test phase.

Standalone on purpose, like ``calltrace_plugin.py``: only the standard library
and pytest. Verinoda copies this file next to the throw-away repository copy and
loads it with ``-p verinoda_flaws`` beside the call tracer. It never imports
Verinoda and sends nothing anywhere: it writes one file into the run's
artifacts.

What it records:

* **SQL** - every ``execute`` / ``executemany`` / ``executescript`` on a
  ``sqlite3`` connection or cursor: ``sqlite3.connect`` (and
  ``sqlite3.dbapi2.connect``) is wrapped at plugin load so connections get a
  subclass of their factory whose methods time the call. When SQLAlchemy has
  been imported by the end of collection, its ``before/after_cursor_execute``
  engine events record statements of other drivers (a statement on a sqlite3
  cursor is left to the wrapper, so nothing is counted twice). Per statement:
  the text with literals and placeholders replaced by ``?`` (the raw text and
  the parameter values are never written, only a hash of them, to tell
  identical executions apart), the stack of project frames that made it
  (code object and instruction offset, innermost first, at most
  ``MAX_DEPTH``) and the duration.
* **Samples** - a daemon thread reads the main thread's stack every
  ``VERINODA_FLAWS_SAMPLE_MS`` (default 5) milliseconds; each sample weighs the
  wall time since the previous one (at most a second), on the project frames
  of that stack (code and line).

Contexts: ``<nodeid>|setup``, ``<nodeid>|call``, ``<nodeid>|teardown`` while a
test runs, ``<collection>`` before the first test, ``<session>`` between tests.
Statements on threads other than the main one carry ``thread``.

Budgets: ``VERINODA_FLAWS_MAX_KEYS`` distinct (context, statement, stack) keys
and ``VERINODA_FLAWS_MAX_BYTES`` of output (past either the header says
``complete: false``); ``VERINODA_FLAWS_MAX_SAMPLE_KEYS`` distinct (context,
stack) sample keys (past it, or when the byte budget leaves out samples, the
lightest stacks are dropped, each context's time still counted, and the header
says ``samples_complete: false``). ``VERINODA_TRACE_DEADLINE_S`` writes a partial
file before the runner's hard kill.

Output: ``$VERINODA_ARTIFACTS/flaws.jsonl``: one ``header`` (schema
``verinoda.flaws/1``), one ``frames`` table (``[path, line, qual, def line,
col, end line, end col]``, paths relative to the copy root), ``sql``,
``sample`` and ``ctx`` (sampled time per context) records.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time

import pytest

SCHEMA = "verinoda.flaws/1"
_perf = time.perf_counter
_getframe = sys._getframe


def _int_env(name: str, default: int) -> int:
    try:
        return int(float(os.environ.get(name, default)))
    except ValueError:
        return default


ROOT_RAW = os.path.abspath(os.environ.get("VERINODA_TRACE_ROOT") or os.getcwd())
ROOT = os.path.normcase(ROOT_RAW)
_ROOT_PREFIX = ROOT.rstrip("\\/") + os.sep
_SELF = os.path.normcase(os.path.abspath(__file__))
_EXCLUDED = tuple(os.path.normcase(os.path.join(ROOT, d)) + os.sep
                  for d in (".venv", "venv", ".tox", ".nox", "node_modules", ".eggs"))
OUT_DIR = os.environ.get("VERINODA_ARTIFACTS") or os.getcwd()
OUT_FILE = os.path.join(OUT_DIR, "flaws.jsonl")
try:
    SAMPLE_S = max(0.001, float(os.environ.get("VERINODA_FLAWS_SAMPLE_MS") or 5) / 1000.0)
except ValueError:
    SAMPLE_S = 0.005
MAX_KEYS = _int_env("VERINODA_FLAWS_MAX_KEYS", 50_000)
MAX_SAMPLE_KEYS = _int_env("VERINODA_FLAWS_MAX_SAMPLE_KEYS", 50_000)
MAX_BYTES = _int_env("VERINODA_FLAWS_MAX_BYTES", 10 * 1024 * 1024)
DEADLINE_S = float(os.environ.get("VERINODA_TRACE_DEADLINE_S") or 0) or None
MAX_DEPTH = 40       # project frames kept per stack (the innermost ones)
MAX_SAME = 64        # distinct (statement text, parameters) hashes kept per key
MAX_WEIGHT_S = 1.0   # one sample never weighs more than this (a stalled sampler thread)

_in_repo: dict = {}   # id(code) -> (code, is repository code)
_sql: dict = {}       # (ctx, statement, stack) -> [n, many, total_s, max_s, {hash: n}, overflow, thread]
_samples: dict = {}   # (ctx, stack) -> [weight_s, n]
_ctx_time: dict = {}  # ctx -> [weight_s, n]
_lock = threading.Lock()
_local = threading.local()
_main_ident = threading.get_ident()
_stop = threading.Event()
ctx = "<collection>"
_state = {"active": False, "complete": True, "breach": None, "dropped": 0, "written": False,
          "hooks": [], "n_sql": 0, "n_samples": 0, "stopped_early": False, "samples_complete": True,
          "samples_dropped": 0}


def _is_repo_file(filename: str) -> bool:
    f = os.path.normcase(filename)
    return f.startswith(_ROOT_PREFIX) and f != _SELF and not f.startswith(_EXCLUDED) \
        and (os.sep + "site-packages" + os.sep) not in f


def _stack(fr, lines: bool = False) -> tuple:
    """The project frames from ``fr`` outwards, innermost first: (id of the code, instruction offset),
    or with ``lines`` (id of the code, line) - samples, where offsets would split one line's time.

    Keyed by ``id(code)``: hashing a code object hashes its constants and names on every lookup (it
    made a recorded execute five times slower). ``_in_repo`` keeps every code object it has seen, so
    an id is never reused within the run."""
    out = []
    in_repo = _in_repo
    while fr is not None:
        code = fr.f_code
        cid = id(code)
        e = in_repo.get(cid)
        if e is None:
            e = in_repo[cid] = (code, _is_repo_file(code.co_filename))
        if e[1]:
            # a line is stored as -(line + 1): offsets are never negative
            out.append((cid, -(fr.f_lineno or 0) - 1) if lines else (cid, fr.f_lasti))
            if len(out) >= MAX_DEPTH:
                break
        fr = fr.f_back
    return tuple(out)


def _breach(reason: str) -> None:
    if _state["breach"] is None:
        _state.update(breach=reason, complete=False)


# -- SQL text ----------------------------------------------------------------------------

_STR = re.compile(r"'(?:[^']|'')*'")
_COMMENT = re.compile(r"--[^\n]*|/\*.*?\*/", re.S)
_NUM = re.compile(r"(?<![\w$])(?:0[xX][0-9a-fA-F]+|\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)(?![\w$])")
_PARAM = re.compile(r"%\(\w+\)s|%s|\$\d+|(?<![:\w]):\w+|\?\d*|@\w+")
_IN_LIST = re.compile(r"\bIN\s*\(\s*\?(?:\s*,\s*\?)*\s*\)", re.I)
_VALUES = re.compile(r"(\(\s*\?(?:\s*,\s*\?)*\s*\))(?:\s*,\s*\(\s*\?(?:\s*,\s*\?)*\s*\))+")
_WS = re.compile(r"\s+")
_norm_cache: dict = {}


def normalise(sql) -> str:
    """Statement text with literals and placeholders as ``?``, ``IN (?, ?)`` as ``IN (?)``, one space."""
    if not isinstance(sql, str):
        try:
            sql = sql.decode("utf-8", "replace") if isinstance(sql, (bytes, bytearray)) else str(sql)
        except Exception:  # noqa: BLE001
            return "<unreadable>"
    hit = _norm_cache.get(sql)
    if hit is not None:
        return hit
    s = _STR.sub("?", sql)
    s = _COMMENT.sub(" ", s)
    s = _NUM.sub("?", s)
    s = _PARAM.sub("?", s)
    s = _IN_LIST.sub("IN (?)", s)
    s = _VALUES.sub(r"\1", s)
    s = _WS.sub(" ", s).strip().rstrip(";").strip()
    if len(_norm_cache) > 4096:
        _norm_cache.clear()
    _norm_cache[sql] = s
    return s


def _same_hash(sql, params) -> int | None:
    """Tells identical executions apart (same text, same parameters) without keeping either."""
    try:
        if isinstance(params, dict):
            p = tuple(sorted(params.items()))
        elif isinstance(params, (list, tuple)):
            p = tuple(params)
        else:
            p = params
        return hash((sql, p)) & 0xFFFFFFFFFFFF
    except TypeError:
        try:
            return hash((sql, repr(params)[:4096])) & 0xFFFFFFFFFFFF
        except Exception:  # noqa: BLE001
            return None


def _record(sql, params, many: bool, t0: float, t1: float, fr) -> None:
    if not _state["active"]:
        return
    try:
        stmt = normalise(sql)
        stack = _stack(fr)
        same = None if many else _same_hash(sql, params)
        thread = threading.get_ident() != _main_ident
        key = (ctx, stmt, stack)
        d = t1 - t0
        with _lock:
            ent = _sql.get(key)
            if ent is None:
                if len(_sql) >= MAX_KEYS:
                    _state["dropped"] += 1
                    _breach("max_keys")
                    return
                ent = _sql[key] = [0, 0, 0.0, 0.0, {}, False, False]
            _state["n_sql"] += 1
            ent[0] += 1
            if many:
                ent[1] += 1
            ent[2] += d
            if d > ent[3]:
                ent[3] = d
            if same is not None:
                ids = ent[4]
                if same in ids:
                    ids[same] += 1
                elif len(ids) < MAX_SAME:
                    ids[same] = 1
                else:
                    ent[5] = True
            if thread:
                ent[6] = True
    except Exception:  # noqa: BLE001 - recording must never break the test run
        pass


# -- sqlite3 ------------------------------------------------------------------------------

class _CursorMixin:
    def execute(self, sql, parameters=(), /):
        fr = _getframe(1)
        t0 = _perf()
        try:
            return super().execute(sql, parameters)
        finally:
            _record(sql, parameters, False, t0, _perf(), fr)

    def executemany(self, sql, seq_of_parameters, /):
        fr = _getframe(1)
        t0 = _perf()
        try:
            return super().executemany(sql, seq_of_parameters)
        finally:
            _record(sql, None, True, t0, _perf(), fr)

    def executescript(self, sql_script, /):
        fr = _getframe(1)
        t0 = _perf()
        try:
            return super().executescript(sql_script)
        finally:
            _record(sql_script, None, True, t0, _perf(), fr)


class _ConnectionMixin:
    def cursor(self, factory=None):
        import sqlite3

        return super().cursor(_subclass(factory or sqlite3.Cursor, _CursorMixin, sqlite3.Cursor))

    def execute(self, sql, parameters=(), /):
        fr = _getframe(1)
        t0 = _perf()
        try:
            return super().execute(sql, parameters)
        finally:
            _record(sql, parameters, False, t0, _perf(), fr)

    def executemany(self, sql, seq_of_parameters, /):
        fr = _getframe(1)
        t0 = _perf()
        try:
            return super().executemany(sql, seq_of_parameters)
        finally:
            _record(sql, None, True, t0, _perf(), fr)

    def executescript(self, sql_script, /):
        fr = _getframe(1)
        t0 = _perf()
        try:
            return super().executescript(sql_script)
        finally:
            _record(sql_script, None, True, t0, _perf(), fr)


# The call tracer names a boundary call by the callee's module and qualified name: keep the names of the
# sqlite3 methods these wrappers stand for (``sqlite3.Connection.execute``, not this plugin's).
VERINODA_EXT_NAMES: dict = {}
for _cls, _public in ((_CursorMixin, "Cursor"), (_ConnectionMixin, "Connection")):
    for _name, _fn in list(vars(_cls).items()):
        if callable(_fn):
            _fn.__module__ = "sqlite3"
            _fn.__qualname__ = f"{_public}.{_name}"
            VERINODA_EXT_NAMES[_fn.__code__] = f"sqlite3.{_public}.{_name}"

_subclasses: dict = {}


def _subclass(base, mixin, root):
    """``base`` with ``mixin`` in front (cached); ``base`` itself when it is not a ``root`` class."""
    try:
        if not isinstance(base, type) or not issubclass(base, root) or issubclass(base, mixin):
            return base
    except TypeError:
        return base
    cls = _subclasses.get(base)
    if cls is None:
        cls = _subclasses[base] = type(base.__name__, (mixin, base),
                                       {"__module__": base.__module__, "__qualname__": base.__qualname__})
    return cls


def _wrap_sqlite() -> None:
    try:
        import sqlite3
        import sqlite3.dbapi2 as dbapi2
    except ImportError:
        return
    orig = sqlite3.connect

    def connect(*args, **kwargs):
        if len(args) > 5:   # factory is the sixth positional parameter
            args = (*args[:5], _subclass(args[5], _ConnectionMixin, sqlite3.Connection), *args[6:])
        else:
            kwargs["factory"] = _subclass(kwargs.get("factory") or sqlite3.Connection, _ConnectionMixin,
                                          sqlite3.Connection)
        return orig(*args, **kwargs)

    connect.__module__ = "sqlite3"
    connect.__qualname__ = connect.__name__ = "connect"
    connect.__doc__ = orig.__doc__
    VERINODA_EXT_NAMES[connect.__code__] = "sqlite3.connect"
    sqlite3.connect = connect
    if getattr(dbapi2, "connect", None) is orig:
        dbapi2.connect = connect
    _state["hooks"].append("sqlite3")


# -- SQLAlchemy (other drivers) ------------------------------------------------------------------

def _hook_sqlalchemy() -> None:
    if "sqlalchemy" not in sys.modules or _state.get("sa_tried"):
        return
    _state["sa_tried"] = True
    try:
        from sqlalchemy import event
        from sqlalchemy.engine import Engine
    except Exception:  # noqa: BLE001 - a broken or partial install: not recorded
        return

    def before(conn, cursor, statement, parameters, context, executemany):
        if isinstance(cursor, _CursorMixin):
            return   # the sqlite3 wrapper records it
        starts = getattr(_local, "sa", None)
        if starts is None or len(starts) > 256:
            starts = _local.sa = {}
        starts[id(cursor)] = _perf()

    def after(conn, cursor, statement, parameters, context, executemany):
        starts = getattr(_local, "sa", None) or {}
        t0 = starts.pop(id(cursor), None)
        if t0 is not None:
            _record(statement, parameters, bool(executemany), t0, _perf(), _getframe(1))

    try:
        event.listen(Engine, "before_cursor_execute", before)
        event.listen(Engine, "after_cursor_execute", after)
    except Exception:  # noqa: BLE001
        return
    _state["hooks"].append("sqlalchemy")


# -- sampler -------------------------------------------------------------------------------------

def _sampler() -> None:
    last = _perf()
    frames = sys._current_frames
    sleep = time.sleep   # a high-resolution timer on Windows (3.11+), where Event.wait ticks at ~15.6 ms
    while True:
        sleep(SAMPLE_S)
        if _stop.is_set():
            return
        now = _perf()
        w = min(now - last, MAX_WEIGHT_S)
        last = now
        if not _state["active"]:
            return
        try:
            fr = frames().get(_main_ident)
            stack = _stack(fr, lines=True) if fr is not None else ()
            del fr
            c = ctx
            with _lock:
                t = _ctx_time.get(c)
                if t is None:
                    t = _ctx_time[c] = [0.0, 0]
                t[0] += w
                t[1] += 1
                _state["n_samples"] += 1
                if not stack:
                    continue
                key = (c, stack)
                ent = _samples.get(key)
                if ent is None:
                    if len(_samples) >= MAX_SAMPLE_KEYS:   # the context's time is still counted
                        _state["samples_dropped"] += 1
                        _state["samples_complete"] = False
                        continue
                    ent = _samples[key] = [0.0, 0]
                ent[0] += w
                ent[1] += 1
        except Exception:  # noqa: BLE001
            pass


def _start() -> None:
    _wrap_sqlite()
    _state["active"] = True
    t = threading.Thread(target=_sampler, name="verinoda-flaws-sampler", daemon=True)
    t.start()
    _state["thread"] = t


def _stop_recording() -> None:
    _state["active"] = False
    _stop.set()
    t = _state.get("thread")
    if t is not None and t is not threading.current_thread():
        t.join(1.0)


# -- output --------------------------------------------------------------------------------------

def _rel(filename: str) -> str:
    try:
        return os.path.relpath(filename, ROOT_RAW).replace("\\", "/")
    except ValueError:
        return filename.replace("\\", "/")


def _position(code, lasti: int) -> list:
    """[line, col, end line, end col] of the instruction at ``lasti`` (col None before Python 3.11)."""
    line = None
    try:
        for start, end, ln in code.co_lines():
            if start <= lasti < end:
                line = ln
                break
    except Exception:  # noqa: BLE001
        pass
    pos = None
    positions = getattr(code, "co_positions", None)
    if positions is not None:
        try:
            for i, p in enumerate(positions()):
                if i == lasti // 2:
                    pos = p
                    break
        except Exception:  # noqa: BLE001
            pos = None
    if pos and pos[0]:
        return [pos[0], pos[2], pos[1], pos[3]]
    return [line or code.co_firstlineno, None, None, None]


def _qual(code) -> str:
    return getattr(code, "co_qualname", None) or code.co_name


def _write(final: bool) -> None:
    with _lock:
        sql = list(_sql.items())
        samples = list(_samples.items())
        ctx_time = list(_ctx_time.items())
    frame_ids: dict = {}
    frames: list = []

    def fid(fr) -> int:
        i = frame_ids.get(fr)
        if i is None:
            cid, lasti = fr
            code = _in_repo[cid][0]
            i = frame_ids[fr] = len(frames)
            if lasti < 0:
                line, col, end_line, end_col = (-lasti - 1) or code.co_firstlineno, None, None, None
            else:
                line, col, end_line, end_col = _position(code, lasti)
            frames.append([_rel(code.co_filename), line, _qual(code), code.co_firstlineno, col, end_line, end_col])
        return i

    ctxs = sorted({k[0] for k, _ in sql} | {k[0] for k, _ in samples} | {c for c, _ in ctx_time})
    cid = {c: i for i, c in enumerate(ctxs)}
    records = []
    for (c, stmt, stack), (n, many, total, mx, same, more, thread) in sql:
        repeated = sorted(((h, k) for h, k in same.items() if k > 1), key=lambda kv: -kv[1])
        rec = {"k": "sql", "ctx": cid[c], "stmt": stmt[:2000], "stack": [fid(f) for f in stack], "n": n,
               "many": many, "ms": round(total * 1000, 3), "max_ms": round(mx * 1000, 3),
               "same": [[f"{h:012x}", k] for h, k in repeated], "distinct": len(same)}
        if more:
            rec["distinct_more"] = True
        if thread:
            rec["thread"] = True
        records.append(rec)
    for c, (w, k) in ctx_time:
        records.append({"k": "ctx", "ctx": cid[c], "ms": round(w * 1000, 3), "n": k})
    lines, size = [], 0
    for rec in records:
        text = json.dumps(rec, separators=(",", ":"))
        if size + len(text) + 1 > MAX_BYTES:
            _breach("max_bytes")
            break
        lines.append(text)
        size += len(text) + 1
    # samples last and the heaviest stacks first, so a byte budget drops the lightest ones
    for (c, stack), (w, k) in sorted(samples, key=lambda kv: -kv[1][0]):
        text = json.dumps({"k": "sample", "ctx": cid[c], "stack": [fid(f) for f in stack],
                           "ms": round(w * 1000, 3), "n": k}, separators=(",", ":"))
        if size + len(text) + 1 > MAX_BYTES:
            _state["samples_complete"] = False
            break
        lines.append(text)
        size += len(text) + 1
    header = {"k": "header", "schema": SCHEMA, "final": final, "complete": bool(_state["complete"]) and final,
              "breach": _state["breach"], "stopped_early": _state["stopped_early"], "hooks": _state["hooks"],
              "sample_ms": round(SAMPLE_S * 1000, 3), "python": sys.version.split()[0], "root": ROOT_RAW,
              "samples_complete": bool(_state["samples_complete"]) and final,
              "limits": {"max_keys": MAX_KEYS, "max_sample_keys": MAX_SAMPLE_KEYS, "max_bytes": MAX_BYTES,
                         "max_depth": MAX_DEPTH},
              "stats": {"sql_executions": _state["n_sql"], "sql_keys": len(sql), "samples": _state["n_samples"],
                        "sample_keys": len(samples), "dropped": _state["dropped"],
                        "samples_dropped": _state["samples_dropped"], "bytes": size},
              "contexts": ctxs}
    data = "\n".join([json.dumps(header, separators=(",", ":")),
                      json.dumps({"k": "frames", "frames": frames}, separators=(",", ":")), *lines]) + "\n"
    os.makedirs(OUT_DIR, exist_ok=True)
    tmp = OUT_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write(data)
    os.replace(tmp, OUT_FILE)
    _state["written"] = True


def _on_deadline() -> None:
    _breach("timeout")
    _state["stopped_early"] = True
    try:
        _write(final=False)
    except (OSError, RuntimeError):
        pass


# -- pytest hooks ------------------------------------------------------------------------------

def _set_ctx(value: str) -> None:
    global ctx
    ctx = value


def pytest_collection_finish(session):
    _hook_sqlalchemy()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    _hook_sqlalchemy()   # imported by a fixture or a lazy import after collection
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


@pytest.hookimpl(tryfirst=True)
def pytest_sessionfinish(session, exitstatus):
    if not ACTIVE_ON_IMPORT:
        return
    _stop_recording()
    if int(exitstatus) == 2:
        _state["stopped_early"] = True
    _write(final=True)


def pytest_unconfigure(config):
    if not ACTIVE_ON_IMPORT:
        return
    _stop_recording()
    if not _state["written"]:
        _write(final=True)


# Record only when loaded as the plugin copied next to an experiment copy (see calltrace_plugin).
ACTIVE_ON_IMPORT = __name__ != "verinoda.runtime.flaws_plugin"
if ACTIVE_ON_IMPORT:
    _start()
    if DEADLINE_S:
        _timer = threading.Timer(DEADLINE_S, _on_deadline)
        _timer.daemon = True
        _timer.start()
