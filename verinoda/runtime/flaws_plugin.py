"""Verinoda runtime-flaws pytest plugin: SQL statements and sampled stacks, per test phase.

Standalone on purpose, like ``calltrace_plugin.py``: only the standard library
and pytest. Verinoda copies this file next to the throw-away repository copy and
loads it with ``-p verinoda_flaws`` beside the call tracer. It never imports
Verinoda and sends nothing anywhere: it writes one file into the run's
artifacts.

What it records:

* **SQL** - every ``execute`` / ``executemany`` / ``executescript`` on a
  ``sqlite3`` connection or cursor. ``sqlite3.connect`` (and
  ``sqlite3.dbapi2.connect``) is wrapped at plugin load: a connection is made
  from ``_Conn`` (a ``sqlite3.Connection`` subclass whose cursors are ``_Cur``,
  a ``sqlite3.Cursor`` subclass that times each execution), or, for a user's
  own ``factory``, from a class placed *after* the user's class (``(MyConn,
  _Conn)``), so the user's overrides run first, with their own signatures, and
  reach the timing through ``super()``. A connection's ``execute`` makes a
  new cursor as sqlite3's own does (``_Conn.cursor``, never an overridden
  ``cursor()``, since Python 3.11; ``self.cursor()`` on 3.10), runs the
  statement on it once and returns it, so a cursor returned by
  ``Connection.execute`` keeps recording.
  When SQLAlchemy has been imported by the end of collection, its
  ``before/after_cursor_execute`` engine events record statements of other
  drivers (a statement on a ``_Cur`` is left to the wrapper, so nothing is
  counted twice). Per statement: the text with literals, placeholders and
  comments replaced (one tokenising pass; a literal is never written), a hash
  of the raw text and parameters (only to tell identical executions apart),
  the stack of project frames that made it (code and instruction offset,
  innermost first, at most ``MAX_DEPTH``) and the duration.
* **Samples** - a daemon thread reads the main thread's stack every
  ``VERINODA_FLAWS_SAMPLE_MS`` (default 5) milliseconds; each sample weighs the
  wall time since the previous one (at most a second), on the project frames
  of that stack (code and line).

Contexts: ``<nodeid>|setup``, ``<nodeid>|call``, ``<nodeid>|teardown`` while a
test runs, ``<collection>`` before the first test, ``<session>`` between tests.
Statements on threads other than the main one carry ``thread``.

Budgets: ``VERINODA_FLAWS_MAX_KEYS`` distinct (context, statement, stack) keys
and ``VERINODA_FLAWS_MAX_BYTES`` of output, header and frame table included
(past either the header says ``complete: false``; a budget smaller than the
header alone still gets the header); ``VERINODA_FLAWS_MAX_SAMPLE_KEYS`` distinct
(context, stack) sample keys (past it, or when the byte budget leaves out
samples, the lightest stacks are dropped, each context's time still counted,
and the header says ``samples_complete: false``). ``VERINODA_TRACE_DEADLINE_S``
writes a partial file before the runner's hard kill.

Output: ``$VERINODA_ARTIFACTS/flaws.jsonl``: one ``header`` (schema
``verinoda.flaws/1``), one ``frames`` table (``[path, line, qual, def line,
col, end line, end col]``, paths relative to the copy root), then ``ctx``
(sampled time per context), ``sql`` (per context, statement and stack),
``same`` (per context and statement: the repeated identical executions over
all its stacks) and ``sample`` records.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import threading
import time
import warnings
import weakref

import pytest

try:
    import sqlite3 as _sqlite3
except ImportError:  # an interpreter built without _sqlite3
    _sqlite3 = None

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
MAX_STMT = 2000      # characters of a normalised statement written (longer ones end in a marker and hash)
MAX_WEIGHT_S = 1.0   # one sample never weighs more than this (a stalled sampler thread)
_SALT = os.urandom(16)   # quoted identifiers are written as a keyed hash: stable in this run only

# id(code) -> (code, True) for project code (kept: the file names its frames at the end), or
# (weak reference, False) for any other code, dropped when the code object dies, so library and
# generated code is not kept alive and a reused id is classified afresh.
_in_repo: dict = {}
_sql: dict = {}       # (ctx, statement, stack) -> [n, many, total_s, max_s, {hash: n}, overflow, thread]
_same: dict = {}      # (ctx, statement) -> [{hash: n}, overflow]: identical executions over all stacks
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


def _classify(code, cid: int) -> bool:
    if _is_repo_file(code.co_filename):
        _in_repo[cid] = (code, True)
        return True
    try:
        ref = weakref.ref(code, lambda _r, cid=cid: _in_repo.pop(cid, None))
    except TypeError:   # no weak references to this object: keep it (rare)
        ref = code
    _in_repo[cid] = (ref, False)
    return False


def _stack(fr, lines: bool = False) -> tuple:
    """The project frames from ``fr`` outwards, innermost first: (id of the code, instruction offset),
    or with ``lines`` (id of the code, -(line + 1)) - samples, where offsets would split one line's time.

    Keyed by ``id(code)``: hashing a code object hashes its constants and names on every lookup (it
    made a recorded execute five times slower). Project code objects are kept, so their ids are never
    reused within the run."""
    out = []
    in_repo = _in_repo
    while fr is not None:
        code = fr.f_code
        cid = id(code)
        e = in_repo.get(cid)
        r = e[1] if e is not None else _classify(code, cid)
        if r:
            out.append((cid, -(fr.f_lineno or 0) - 1) if lines else (cid, fr.f_lasti))
            if len(out) >= MAX_DEPTH:
                break
        fr = fr.f_back
    return tuple(out)


def _breach(reason: str) -> None:
    if _state["breach"] is None:
        _state.update(breach=reason, complete=False)


# -- SQL text ----------------------------------------------------------------------------
#
# One pass over the tokens that hide others, leftmost first: a quote inside a comment and a comment
# marker inside a string are both seen for what they are. Strings (also E'..' with backslash escapes
# and X'..' / B'..' / N'..') become ?, comments a space, double-quoted names a keyed hash; an opened
# string, quoted name or comment that never closes ends the statement with ? (nothing after it is
# kept). Numbers and placeholders are replaced in the text between those tokens only.

_TOKEN = re.compile(
    r"(?P<estr>(?<![\w$])[eE]'(?:[^'\\]|\\.|'')*')"
    r"|(?P<str>(?:(?<![\w$])[xXbBnN])?'(?:[^']|'')*')"
    r"|(?P<dq>\"(?:[^\"]|\"\")*\")"
    r"|(?P<lc>--[^\n]*)"
    r"|(?P<bc>/\*.*?\*/)"
    r"|(?P<open>(?:(?<![\w$])[eExXbBnN])?'|\"|/\*)",
    re.S)
_NUM = re.compile(r"(?<![\w$])(?:0[xX][0-9a-fA-F]+|\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)(?![\w$])")
_PARAM = re.compile(r"%\(\w+\)s|%s|\$\d+|(?<![:\w]):\w+|\?\d*|@\w+")
_IN_LIST = re.compile(r"\bIN\s*\(\s*\?(?:\s*,\s*\?)*\s*\)", re.I)
_VALUES = re.compile(r"(\(\s*\?(?:\s*,\s*\?)*\s*\))(?:\s*,\s*\(\s*\?(?:\s*,\s*\?)*\s*\))+")
_WS = re.compile(r"\s+")
_norm_cache: dict = {}


def _plain(seg: str) -> str:
    return _PARAM.sub("?", _NUM.sub("?", seg))


def _quoted_name(tok: str) -> str:
    return '"~' + hashlib.blake2s(tok.encode("utf-8", "replace"), key=_SALT, digest_size=4).hexdigest() + '"'


def normalise(sql) -> str:
    """Statement text with literals and placeholders as ``?``, comments dropped, ``IN (?, ?)`` as ``IN (?)``,
    double-quoted names as a keyed hash, one space. Never holds a literal of the input."""
    if not isinstance(sql, str):
        try:
            sql = sql.decode("utf-8", "replace") if isinstance(sql, (bytes, bytearray)) else str(sql)
        except Exception:  # noqa: BLE001
            return "<unreadable>"
    hit = _norm_cache.get(sql)
    if hit is not None:
        return hit
    out, pos = [], 0
    for m in _TOKEN.finditer(sql):
        out.append(_plain(sql[pos:m.start()]))
        kind = m.lastgroup
        if kind in ("estr", "str"):
            out.append("?")
        elif kind == "dq":
            out.append(_quoted_name(m.group()))
        elif kind in ("lc", "bc"):
            out.append(" ")
        else:   # opened and never closed: whatever follows is not kept
            out.append(" " if m.group() == "/*" else "?")
            pos = len(sql)
            break
        pos = m.end()
    out.append(_plain(sql[pos:]))
    s = "".join(out)
    s = _IN_LIST.sub("IN (?)", s)
    s = _VALUES.sub(r"\1", s)
    s = _WS.sub(" ", s).strip().rstrip(";").strip()
    if len(_norm_cache) > 4096:
        _norm_cache.clear()
    _norm_cache[sql] = s
    return s


def _stmt_out(stmt: str) -> str:
    """The statement as written: a long one cut with a marker naming its length and a hash of all of it."""
    if len(stmt) <= MAX_STMT:
        return stmt
    h = hashlib.blake2s(stmt.encode("utf-8", "replace"), digest_size=6).hexdigest()
    return f"{stmt[:MAX_STMT]} ...[{len(stmt)} chars #{h}]"


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


def _count(ids: dict, h: int) -> bool:
    """Count ``h`` in ``ids``; False when it is new and ``ids`` is full."""
    if h in ids:
        ids[h] += 1
    elif len(ids) < MAX_SAME:
        ids[h] = 1
    else:
        return False
    return True


def _record(sql, params, many: bool, t0: float, t1: float, fr) -> None:
    if not _state["active"]:
        return
    try:
        stmt = normalise(sql)
        stack = _stack(fr)
        same = None if many else _same_hash(sql, params)
        thread = threading.get_ident() != _main_ident
        c = ctx
        key = (c, stmt, stack)
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
                if not _count(ent[4], same):
                    ent[5] = True
                sk = (c, stmt)
                se = _same.get(sk)
                if se is None:
                    se = _same[sk] = [{}, False]
                if not _count(se[0], same):
                    se[1] = True
            if thread:
                ent[6] = True
    except Exception:  # noqa: BLE001 - recording must never break the test run
        pass


# -- sqlite3 ------------------------------------------------------------------------------

_Cur = _Conn = None
# The call tracer names a boundary call by the callee's module and qualified name: the recorder's
# methods carry the names of the sqlite3 methods they stand for (``sqlite3.Connection.execute``), and
# this table gives them to the ``setprofile`` tracer, which sees code objects.
VERINODA_EXT_NAMES: dict = {}

if _sqlite3 is not None:
    class _Cur(_sqlite3.Cursor):
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

    class _Conn(_sqlite3.Connection):
        def cursor(self, *args, **kwargs):
            if not args and not kwargs:
                return super().cursor(_Cur)
            if args:
                return super().cursor(_cursor_class(args[0]), *args[1:], **kwargs)
            if "factory" in kwargs:
                kwargs["factory"] = _cursor_class(kwargs["factory"])
            return super().cursor(**kwargs)

        # As sqlite3's own: a new cursor, the statement run on it once, the cursor returned - recorded by
        # the cursor, so it keeps recording afterwards. Since Python 3.11 sqlite3 makes that cursor in C,
        # never through an overridden ``cursor()``; 3.10 called ``self.cursor()``.
        def execute(self, sql, parameters=(), /):
            return _new_cursor(self).execute(sql, parameters)

        def executemany(self, sql, seq_of_parameters, /):
            return _new_cursor(self).executemany(sql, seq_of_parameters)

        def executescript(self, sql_script, /):
            return _new_cursor(self).executescript(sql_script)

    if sys.version_info >= (3, 11):
        _new_cursor = _Conn.cursor
    else:
        def _new_cursor(conn):
            return conn.cursor()

    for _cls, _public in ((_Cur, "Cursor"), (_Conn, "Connection")):
        for _name, _fn in list(vars(_cls).items()):
            if callable(_fn) and hasattr(_fn, "__code__"):
                _fn.__module__ = "sqlite3"
                _fn.__qualname__ = f"{_public}.{_name}"
                VERINODA_EXT_NAMES[_fn.__code__] = f"sqlite3.{_public}.{_name}"
        _cls.__module__, _cls.__qualname__, _cls.__name__ = "sqlite3", _public, _public

_classes: dict = {}


def _behind(base, recorder, root):
    """A class with the user's ``base`` first and ``recorder`` after it (cached): the user's methods run
    first and reach the recorder through ``super()``. ``recorder`` for ``root`` itself; ``base`` unchanged
    when it is not a ``root`` subclass (sqlite3 then reports it as it would) or the two cannot be
    combined."""
    if base is root:
        return recorder
    try:
        if not isinstance(base, type) or not issubclass(base, root) or issubclass(base, recorder):
            return base
    except TypeError:
        return base
    cls = _classes.get(base)
    if cls is None:
        try:
            cls = type(base.__name__, (base, recorder),
                       {"__module__": base.__module__, "__qualname__": base.__qualname__})
        except TypeError:   # a metaclass or layout conflict: not recorded, never broken
            cls = base
        _classes[base] = cls
    return cls


def _cursor_class(factory):
    return _behind(factory, _Cur, _sqlite3.Cursor)


def _conn_class(factory):
    return _behind(factory, _Conn, _sqlite3.Connection)


def _reemit(caught: list, caller) -> None:
    """Warnings sqlite3 raised inside the wrapper, again, at the caller's line (as without the wrapper)."""
    registry = caller.f_globals.setdefault("__warningregistry__", {})
    for w in caught:
        warnings.warn_explicit(w.message, w.category, caller.f_code.co_filename, caller.f_lineno,
                               registry=registry)


def _make_connect(orig):
    def connect(*args, **kwargs):
        if len(args) > 5:   # factory is the sixth positional parameter
            args = (*args[:5], _conn_class(args[5]), *args[6:])
        elif "factory" in kwargs:
            kwargs["factory"] = _conn_class(kwargs["factory"])
        else:
            kwargs["factory"] = _Conn
        caller = _getframe(1)
        err = None
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            try:
                conn = orig(*args, **kwargs)
            except BaseException as exc:  # noqa: BLE001 - raised again below, after its warnings
                err = exc
        _reemit(caught, caller)
        if err is not None:
            try:
                raise err
            finally:
                err = None
        return conn

    connect.__module__ = "sqlite3"
    connect.__qualname__ = connect.__name__ = "connect"
    connect.__doc__ = orig.__doc__
    return connect


def _wrap_sqlite() -> None:
    if _sqlite3 is None:
        return
    try:
        import sqlite3.dbapi2 as dbapi2
    except ImportError:
        dbapi2 = None
    orig = _sqlite3.connect
    connect = _make_connect(orig)
    VERINODA_EXT_NAMES[connect.__code__] = "sqlite3.connect"
    _sqlite3.connect = connect
    if dbapi2 is not None and getattr(dbapi2, "connect", None) is orig:
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
        if _Cur is not None and isinstance(cursor, _Cur):
            return   # the sqlite3 recorder records it
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


def _frame_row(fr) -> list:
    cid, lasti = fr
    code = _in_repo[cid][0]
    if lasti < 0:
        line, col, end_line, end_col = (-lasti - 1) or code.co_firstlineno, None, None, None
    else:
        line, col, end_line, end_col = _position(code, lasti)
    return [_rel(code.co_filename), line, _qual(code), code.co_firstlineno, col, end_line, end_col]


def _dumps(obj) -> str:
    return json.dumps(obj, separators=(",", ":"))


class _Out:
    """The records written, within ``MAX_BYTES`` counting the header, the frame table and the context
    names: a frame or a context enters the tables only with a record that was kept."""

    def __init__(self, reserve: int):
        self.size = reserve
        self.lines: list[str] = []
        self.frames: list = []
        self.frame_ids: dict = {}
        self.ctxs: list[str] = []
        self.ctx_ids: dict = {}

    def add(self, c: str, stack: tuple, make) -> bool:
        new = [f for f in dict.fromkeys(stack) if f not in self.frame_ids]
        rows = [_frame_row(f) for f in new]
        extra = sum(len(_dumps(r)) + 1 for r in rows)
        ci = self.ctx_ids.get(c)
        if ci is None:
            extra += len(_dumps(c)) + 1
            ci = len(self.ctxs)
        ids = {f: len(self.frames) + i for i, f in enumerate(new)}
        rec = make(ci, [self.frame_ids.get(f, ids.get(f)) for f in stack])
        text = _dumps(rec)
        if self.size + extra + len(text) + 1 > MAX_BYTES:
            return False
        self.size += extra + len(text) + 1
        if c not in self.ctx_ids:
            self.ctx_ids[c] = ci
            self.ctxs.append(c)
        self.frame_ids.update(ids)
        self.frames.extend(rows)
        self.lines.append(text)
        return True


def _header(final: bool, sql: list, samples: list, size: int, ctxs: list) -> dict:
    return {"k": "header", "schema": SCHEMA, "final": final, "complete": bool(_state["complete"]) and final,
            "breach": _state["breach"], "stopped_early": _state["stopped_early"], "hooks": _state["hooks"],
            "sample_ms": round(SAMPLE_S * 1000, 3), "python": sys.version.split()[0], "root": ROOT_RAW,
            "samples_complete": bool(_state["samples_complete"]) and final,
            "limits": {"max_keys": MAX_KEYS, "max_sample_keys": MAX_SAMPLE_KEYS, "max_bytes": MAX_BYTES,
                       "max_depth": MAX_DEPTH},
            "stats": {"sql_executions": _state["n_sql"], "sql_keys": len(sql), "samples": _state["n_samples"],
                      "sample_keys": len(samples), "dropped": _state["dropped"],
                      "samples_dropped": _state["samples_dropped"], "bytes": size},
            "contexts": ctxs}


def _write(final: bool) -> None:
    with _lock:
        sql = list(_sql.items())
        same = [(k, (dict(v[0]), v[1])) for k, v in _same.items()]
        samples = list(_samples.items())
        ctx_time = list(_ctx_time.items())
    # the header with the widest numbers it can hold, the breach and an empty frame table: reserved first
    probe = _header(final, sql, samples, 10 ** 12, [])
    probe["breach"] = "max_bytes"
    probe["complete"] = probe["samples_complete"] = False
    reserve = len(_dumps(probe)) + 1 + len(_dumps({"k": "frames", "frames": []})) + 1 + 32
    out = _Out(reserve)
    cut = False
    for c, (w, k) in ctx_time:   # first: each context's sampled time
        cut |= not out.add(c, (), lambda ci, _s, w=w, k=k: {"k": "ctx", "ctx": ci, "ms": round(w * 1000, 3),
                                                             "n": k})
    for (c, stmt, stack), (n, many, total, mx, ids, more, thread) in sorted(sql, key=lambda kv: -kv[1][0]):
        def make(ci, st, stmt=stmt, n=n, many=many, total=total, mx=mx, ids=ids, more=more, thread=thread):
            rec = {"k": "sql", "ctx": ci, "stmt": _stmt_out(stmt), "stack": st, "n": n, "many": many,
                   "ms": round(total * 1000, 3), "max_ms": round(mx * 1000, 3), "distinct": len(ids)}
            if more:
                rec["distinct_more"] = True
            if thread:
                rec["thread"] = True
            return rec
        cut |= not out.add(c, stack, make)
    for (c, stmt), (ids, more) in same:
        rep = sorted(((h, k) for h, k in ids.items() if k > 1), key=lambda kv: -kv[1])
        if not rep:
            continue
        cut |= not out.add(c, (), lambda ci, _s, stmt=stmt, rep=rep, more=more: {
            "k": "same", "ctx": ci, "stmt": _stmt_out(stmt), "same": [[f"{h:012x}", k] for h, k in rep],
            **({"more": True} if more else {})})
    if cut:
        _breach("max_bytes")
    # samples last and the heaviest stacks first, so a byte budget drops the lightest ones
    for (c, stack), (w, k) in sorted(samples, key=lambda kv: -kv[1][0]):
        if not out.add(c, stack, lambda ci, st, w=w, k=k: {"k": "sample", "ctx": ci, "stack": st,
                                                            "ms": round(w * 1000, 3), "n": k}):
            _state["samples_complete"] = False
    header = _header(final, sql, samples, 0, out.ctxs)
    frames_text = _dumps({"k": "frames", "frames": out.frames})
    body = len(frames_text) + 2 + sum(len(x) + 1 for x in out.lines)
    head_text = _dumps(header)
    for _ in range(3):   # the size of the file, the header's own digits included
        header["stats"]["bytes"] = len(head_text) + body
        head_text = _dumps(header)
    data = "\n".join([head_text, frames_text, *out.lines]) + "\n"
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
