"""Verinoda probe pytest plugin: call one function on a fixed list of inputs and record what it did.

Standalone on purpose, like ``calltrace_plugin.py``: only the standard library
and pytest. :mod:`verinoda.probe` copies this file and a generated
``verinoda_probe_spec.py`` (the input corpus, as JSON text) *next to* (never
into) the throw-away repository copy made by :mod:`verinoda.experiments`, puts
that directory on ``PYTHONPATH`` and loads it with ``-p verinoda_probe``. It
never imports Verinoda. No project test is collected: the plugin does its work
in ``pytest_sessionstart`` and ends the session with exit status 0.

For every input (``spec["cases"]``, from ``VERINODA_PROBE_START`` on) the
function is called ``spec["repeat"]`` times (default 2), each time on freshly
decoded arguments, and one JSON line records each call's result: the return
value's type and ``repr`` (memory addresses masked, long reprs cut with the
sha256 of the whole), or the exception's type and message, the time taken, a
digest of the arguments after the call when one of them is mutable, and the
agent's properties that did not hold. Two calls of one input that disagree
mark the input nondeterministic in Verinoda.

Side effects: an audit hook (``sys.addaudithook``) is active while the target
module is imported and while each call runs, and from the import to the end of
the process in the main thread and in every thread the project starts (a thread
that outlives its call stays restricted, and so does a result's ``__del__`` or
a ``weakref.finalize`` callback that runs after its call returned).
With ``spec["block"]`` it stops file writes, network use, new processes
(``subprocess``, ``os`` process functions, ``multiprocessing`` through
``_winapi.CreateProcess`` or ``_posixsubprocess.fork_exec``),
environment/working-directory changes and database connections other than
``:memory:`` by raising a ``BaseException`` subclass inside the operation; the
call is recorded as ``blocked`` with the event (also when the function
swallowed the exception). What a call leaves behind is released, and young
reference cycles collected, before its window closes, so its finalizers count
as the call's. An event outside any call (a project thread, a later finalizer,
an exit handler) is recorded in a ``threads`` line with the input it followed.
Exit handlers registered after the import (``atexit.register``) are kept by the
plugin and run, under the hook, after the calls; the process then ends with
``os._exit``, so nothing the project left for interpreter exit runs unobserved
(objects still alive then are not finalized at all). At import and exit, writes
inside the run's throw-away directory are allowed (library caches under the
run's HOME); the loopback socket pair ``socket.socketpair()`` builds on Windows
(``asyncio``'s event loop) is not network use. Without ``block`` the events are
only recorded. Not seen: class attribute writes (CPython raises no audit event
for them; the static gate checks them) and what C extensions or ``ctypes`` do
without an audited Python call.

The target module must be the file Verinoda named: when its import name resolves
to another file (a module of the same name imported before, like ``json``), the
plugin writes ``import_error`` with ``mismatch`` and calls nothing.

A call that runs longer than ``spec["per_call_timeout"]`` seconds ends the
process through ``faulthandler`` (the stack goes to ``probe_hang.txt``);
Verinoda reads the last recorded input and resumes after it in a new run. A
process that ends during a call without that stack (``os._exit``, a crash) is
told apart by the empty ``probe_hang.txt``.

Optional ``spec["scaling"]``: one argument built at growing sizes, timed as
the median of several calls per size, in several rounds (rough growth only).

Output: ``$VERINODA_ARTIFACTS/$VERINODA_PROBE_OUT`` (default ``probe.jsonl``),
JSON lines with schema ``verinoda.probe/1``: ``header``, ``import_error``,
``import_events``, ``ready`` (the target imported), ``row`` (one per input),
``scaling``, ``threads``, ``footer``.
"""

from __future__ import annotations

import atexit
import base64
import builtins
import decimal
import faulthandler
import gc
import hashlib
import importlib
import inspect
import itertools
import json
import math
import os
import re
import sys
import threading
import time

import pytest

SCHEMA = "verinoda.probe/1"
REPR_MAX = 2000
MSG_MAX = 300
EVENTS_MAX = 5
MATERIALIZE_MAX = 10001
_ADDR = re.compile(r"\bat 0x[0-9A-Fa-f]+")
_WRITE_FLAGS = 0
for _name in ("O_WRONLY", "O_RDWR", "O_APPEND", "O_CREAT", "O_TRUNC"):
    _WRITE_FLAGS |= getattr(os, _name, 0)
_FS_EVENTS = {"os.remove", "os.rename", "os.rmdir", "os.mkdir", "os.chmod", "os.chown", "os.link", "os.symlink",
              "os.truncate", "os.utime", "os.chflags", "os.lchflags", "shutil.rmtree", "shutil.copyfile",
              "shutil.copymode", "shutil.copystat", "shutil.copytree", "shutil.move", "shutil.make_archive",
              "shutil.unpack_archive", "shutil.chown"}
_NET_EVENTS = {"socket.connect", "socket.bind", "socket.sendto", "socket.sendmsg", "socket.getaddrinfo",
               "socket.gethostbyname", "socket.gethostbyname_ex", "socket.gethostbyaddr", "socket.getnameinfo",
               "urllib.Request", "http.client.connect", "http.client.send", "ftplib.connect", "smtplib.connect",
               "poplib.connect", "imaplib.open", "nntplib.connect", "telnetlib.Telnet.open", "webbrowser.open"}
_PROC_EVENTS = {"subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn", "os.fork", "os.forkpty",
                "os.startfile", "os.kill", "os.killpg", "pty.spawn", "os.popen", "_winapi.CreateProcess",
                "_posixsubprocess.fork_exec"}
_STATE_EVENTS = {"os.putenv", "os.unsetenv", "os.chdir", "os.fchdir"}
THREAD_WAIT_MAX = 5.0  # seconds to wait, in all, for threads the project started before the run ends


class _Blocked(BaseException):
    """Raised inside a blocked operation; a BaseException so that ``except Exception`` does not swallow it."""


class _S:
    active = False
    phase = "idle"
    block = True
    events: list = []
    allowed_root = ""
    run_root = ""  # the run's throw-away directory as written (masked in reprs and messages)
    main_thread = None  # the thread that runs the probe; None until the hook is installed
    known_threads: frozenset = frozenset()  # threads that existed before the target was imported
    stray: list = []  # side effects outside any call (project threads, finalizers, exit handlers)
    last_input = -1
    armed = False  # the main thread is restricted outside the calls too (from the import to the end)
    render_notes: set = set()  # why the last rendered result is not plain repr (see _result)
    exit_handlers: list = []  # (func, args, kwargs, input) registered through atexit.register after the import


class _Mismatch(Exception):
    """The target's import name resolves to another file than the one Verinoda named."""


def _detail(args) -> str:
    try:
        return " ".join(repr(a)[:120] for a in args[:3])
    except Exception:  # noqa: BLE001 - a repr may fail
        return ""


def _inside_run_dir(path) -> bool:
    """Is ``path`` inside the run's throw-away directory (the copy, its HOME, its artifacts)?"""
    if not _S.allowed_root or not isinstance(path, (str, bytes, os.PathLike)):
        return False
    try:
        return os.path.normcase(os.path.abspath(os.fsdecode(path))).startswith(_S.allowed_root)
    except (TypeError, ValueError):
        return False


_LOOPBACK = ("127.0.0.1", "::1")


def _socketpair_loopback(event: str, args) -> bool:
    """Is this the bind/connect of the loopback pair ``socket.socketpair()`` builds where the OS has none
    (Windows; ``asyncio``'s event loop makes one)? Only the standard library's own function on the stack, and
    only a loopback address, count: that pair never leaves the process."""
    if event not in ("socket.bind", "socket.connect") or len(args) < 2:
        return False
    addr = args[1]
    if not (isinstance(addr, tuple) and addr and addr[0] in _LOOPBACK):
        return False
    if event == "socket.bind" and (len(addr) < 2 or addr[1] != 0):
        return False
    f = sys._getframe(1)
    for _ in range(6):
        if f is None:
            return False
        if f.f_code.co_name in ("socketpair", "_fallback_socketpair") and f.f_globals.get("__name__") == "socket":
            return True
        f = f.f_back
    return False


def _classify(event: str, args) -> str | None:
    """The kind of side effect an audit event is, or None when it is not one the probe stops."""
    if event == "open":
        path, mode, flags = (list(args) + [None, None, None])[:3]
        if isinstance(path, int):
            return None
        writes = isinstance(flags, int) and bool(flags & _WRITE_FLAGS)
        if not writes and isinstance(mode, str):
            writes = any(c in mode for c in "wax+")
        if not writes:
            return None
        if _S.phase in ("import", "exit") and _inside_run_dir(path):  # a library cache under the run's HOME
            return None
        return "file-write"
    if event in _FS_EVENTS:
        if _S.phase in ("import", "exit") and args and all(_inside_run_dir(a) for a in args[:2]
                                                            if isinstance(a, (str, bytes, os.PathLike))):
            return None
        return "file-write"
    if event in _NET_EVENTS:
        if _socketpair_loopback(event, args):
            return None
        return "network"
    if event in _PROC_EVENTS or event.startswith("os.exec") or event.startswith("os.spawn"):
        return "process"
    if event in _STATE_EVENTS:
        return "global-state"
    if event == "sqlite3.connect":
        db = args[0] if args else None
        if isinstance(db, (str, bytes)) and os.fsdecode(db) in (":memory:", ""):
            return None
        if isinstance(db, str) and db.startswith("file::memory:"):
            return None
        return "db-connection"
    if event.startswith("winreg.") and any(w in event for w in ("Set", "Create", "Delete", "Save", "Load")):
        return "global-state"
    if event.startswith("syslog."):
        return "global-state"
    return None


def _project_thread() -> bool:
    """Is the current thread one the project started (after the hook was installed)?"""
    if _S.main_thread is None:
        return False
    ident = threading.get_ident()
    return ident != _S.main_thread and ident not in _S.known_threads


def _hook(event: str, args) -> None:
    if not _S.active and not _S.armed and not _project_thread():
        return
    kind = _classify(event, args)
    if kind is None:
        return
    rec = {"kind": kind, "event": event, "detail": _detail(args)[:200]}
    if _S.main_thread is not None and threading.get_ident() != _S.main_thread:
        rec["thread"] = True
    if _S.active:
        if len(_S.events) < EVENTS_MAX:
            _S.events.append(rec)
    elif len(_S.stray) < EVENTS_MAX:
        where = {} if rec.get("thread") else ({"at_exit": True} if _S.phase == "exit" else {"main": True})
        _S.stray.append({**rec, **where, "after_input": _S.last_input})
    if _S.block:
        raise _Blocked(f"{kind}: {event} {_detail(args)[:120]}")


def _capture_atexit() -> None:
    """``atexit.register`` from now on keeps the handler in the plugin instead: :func:`_exit_phase` runs it under
    the hook, and the process ends with ``os._exit`` - a handler the project registers never runs unobserved."""
    real_unregister = atexit.unregister

    def register(func, *args, **kwargs):
        _S.exit_handlers.append((func, args, kwargs, _S.last_input))
        return func

    def unregister(func):
        _S.exit_handlers[:] = [h for h in _S.exit_handlers if h[0] != func]
        return real_unregister(func)

    atexit.register = register
    atexit.unregister = unregister


def _exit_phase() -> None:
    """After the calls, still under the hook (main thread armed): collect the garbage the calls left (their
    finalizers run now) and run the exit handlers the project registered, last registered first."""
    _S.phase = "exit"
    try:
        gc.collect()
        while _S.exit_handlers:
            func, args, kwargs, after = _S.exit_handlers.pop()
            _S.last_input = after
            try:
                func(*args, **kwargs)
            except BaseException:  # noqa: BLE001 - a failing (or blocked) handler is not the probe's failure
                pass
        gc.collect()
    finally:
        _S.phase = "idle"


def _guard_fork_exec() -> None:
    """``multiprocessing`` on POSIX starts its spawn/forkserver children through ``_posixsubprocess.fork_exec``,
    which raises no audit event of its own: raise one, so that the hook sees the process start."""
    try:
        import _posixsubprocess
    except ImportError:
        return
    real = getattr(_posixsubprocess, "fork_exec", None)
    if real is None or getattr(real, "_verinoda", False):
        return

    def fork_exec(*args, **kwargs):
        sys.audit("_posixsubprocess.fork_exec", args[0] if args else None)
        return real(*args, **kwargs)

    fork_exec._verinoda = True
    _posixsubprocess.fork_exec = fork_exec


# -- values --------------------------------------------------------------------------------

def _vary(v, k: int):
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, int):
        return v + k
    if isinstance(v, float):
        return v + k
    if isinstance(v, str):
        return f"{v}{k}"
    if isinstance(v, list):
        return [_vary(x, k) for x in v]
    if isinstance(v, tuple):
        return tuple(_vary(x, k) for x in v)
    if isinstance(v, dict):
        return {key: _vary(x, k) for key, x in v.items()}
    return v


def decode(v):
    """A fresh Python value from the corpus encoding (see verinoda.probe_inputs.encode)."""
    if isinstance(v, list):
        return [decode(x) for x in v]
    if not isinstance(v, dict):
        return v
    if "$f" in v:
        return float(v["$f"])
    if "$d" in v:
        return {decode(k): decode(x) for k, x in v["$d"]}
    if "$t" in v:
        return tuple(decode(x) for x in v["$t"])
    if "$set" in v:
        return {decode(x) for x in v["$set"]}
    if "$fs" in v:
        return frozenset(decode(x) for x in v["$fs"])
    if "$b" in v:
        return base64.b64decode(v["$b"])
    if "$dec" in v:
        return decimal.Decimal(v["$dec"])
    if "$rep" in v:
        el, n = v["$rep"]
        return [decode(el) for _ in range(int(n))]
    if "$seq" in v:
        el, n = v["$seq"]
        tmpl = decode(el)
        return [_vary(tmpl, k) for k in range(int(n))]
    if "$srep" in v:
        s, n = v["$srep"]
        return str(s) * int(n)
    if "$new" in v:
        spec = v["$new"]
        cls = getattr(importlib.import_module(spec["m"]), spec["n"])
        return cls(*[decode(a) for a in spec.get("a", [])], **{k: decode(x) for k, x in spec.get("k", [])})
    if "$enum" in v:
        spec = v["$enum"]
        return getattr(getattr(importlib.import_module(spec["m"]), spec["n"]), spec["v"])
    raise ValueError(f"unknown value encoding: {sorted(v)}")


def _norm(text: str) -> str:
    """Masks what differs between runs of the same code: memory addresses and the run's throw-away directory
    (as written and in ``repr`` form, with doubled backslashes)."""
    text = _ADDR.sub("at 0x?", text)
    root = _S.run_root
    if root:
        for form in {root, root.replace("\\", "\\\\"), root.replace("\\", "/")}:
            if form and form in text:
                text = text.replace(form, "<run>")
    return text


def _type_name(v) -> str:
    t = type(v)
    return f"{t.__module__}.{t.__qualname__}"


def _repr(v, depth: int = 0) -> str:
    """``repr`` with the elements of built-in sets in sorted order: the iteration order of a set is not part of
    its value, and two equal sets built in another order would otherwise differ. Everything else (dict order
    included, which Python guarantees) is the plain ``repr``."""
    t = type(v)
    if depth > 20:
        return repr(v)
    if t in (set, frozenset) and len(v) <= 10_000:
        if len(v) > 1:
            _S.render_notes.add("set")
        items = sorted(_repr(x, depth + 1) for x in v)
        if t is set:
            return "{" + ", ".join(items) + "}" if items else "set()"
        return "frozenset({" + ", ".join(items) + "})" if items else "frozenset()"
    if t in (list, tuple) and len(v) <= 10_000 and any(type(x) in (set, frozenset, list, tuple, dict) for x in v):
        inner = ", ".join(_repr(x, depth + 1) for x in v)
        return f"[{inner}]" if t is list else ("(" + inner + ("," if len(v) == 1 else "") + ")")
    if t is dict and len(v) <= 10_000 and any(type(x) in (set, frozenset, list, tuple, dict) for x in v.values()):
        return "{" + ", ".join(f"{_repr(k, depth + 1)}: {_repr(x, depth + 1)}" for k, x in v.items()) + "}"
    return repr(v)


def _render(v) -> str:
    """``_repr``, retried without the int-to-str digit limit (Python 3.11+) when that limit made it fail: two
    different integers of more than 4300 digits must not both read ``<repr raised ValueError>``. The limit is
    lifted only while rendering, never while the project's code runs."""
    try:
        return _repr(v)
    except ValueError:
        get = getattr(sys, "get_int_max_str_digits", None)
        if get is None:
            raise
        old = get()
        sys.set_int_max_str_digits(0)
        _S.render_notes.add("digits")
        try:
            return _repr(v)
        finally:
            sys.set_int_max_str_digits(old)


def _result(v) -> dict:
    """The result's type and text; ``np`` lists why the text is not plain ``repr`` (sorted set elements, the
    int digit limit lifted, masked addresses or run directory), so that an emitted test can rebuild it."""
    _S.render_notes = set()
    try:
        text = _render(v)
    except BaseException as exc:  # noqa: BLE001 - a user repr may raise anything
        text = f"<repr raised {type(exc).__name__}>"
        _S.render_notes.add("raised")
    masked = _norm(text)
    if masked != text:
        _S.render_notes.add("masked")
    text = masked
    out = {"t": _type_name(v), "r": text[:REPR_MAX]}
    if _S.render_notes:
        out["np"] = sorted(_S.render_notes)
    if len(text) > REPR_MAX:
        out["h"] = hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()
        out["n"] = len(text)
    return out


def _exc(e: BaseException) -> dict:
    try:
        msg = str(e)
    except BaseException:  # noqa: BLE001
        msg = "<str raised>"
    return {"e": _type_name(e), "m": _norm(msg)[:MSG_MAX]}


def _is_iterator(v) -> bool:
    if inspect.isgenerator(v):
        return True
    try:
        return hasattr(v, "__next__") and iter(v) is v and not isinstance(v, (str, bytes))
    except BaseException:  # noqa: BLE001
        return False


_MUTABLE = (list, dict, set, bytearray)


def _args_digest(args: list, kwargs: dict) -> str | None:
    if not any(isinstance(a, _MUTABLE) for a in (*args, *kwargs.values())):
        return None
    try:
        text = _norm(_render((list(args), sorted(kwargs.items()))))
    except BaseException:  # noqa: BLE001
        return None
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()[:16]


# -- the target ----------------------------------------------------------------------------

def _same_file(a: str, b: str) -> bool:
    try:
        return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))
    except (OSError, ValueError):
        return False


def _target(spec: dict):
    mod = importlib.import_module(spec["module"])
    want = spec.get("file")
    got = getattr(mod, "__file__", None)
    if want and not (got and _same_file(got, os.path.abspath(want))):
        raise _Mismatch(f"`{spec['module']}` resolves to {got or 'a namespace package'}, not {want} (a module of "
                        "the same name was imported first)")
    parts = spec["qual"].split(".")
    kind = spec.get("call", {}).get("kind", "function")
    if kind == "method":
        cls = mod
        for p in parts[:-1]:
            cls = getattr(cls, p)
        return cls, parts[-1]
    obj = mod
    for p in parts:
        obj = getattr(obj, p)
    return obj, None


def _bound(target, method: str | None, spec: dict):
    """The callable for one call: the function, or the method of an instance built from the recipe."""
    if method is None:
        return target
    inst = decode(spec["call"]["recipe"])
    return getattr(inst, method)


def _param_names(spec: dict) -> list[str]:
    return list(spec.get("params") or [])


def _properties(spec: dict) -> list:
    out = []
    for text in spec.get("properties") or []:
        try:
            out.append(compile(text, "<property>", "eval"))
        except SyntaxError:
            out.append(None)
    return out


def _check_properties(props: list, names: list[str], args: list, kwargs: dict, result) -> tuple[list, list]:
    ns: dict = {}
    for name, val in zip(names, args):
        ns[name] = val
    ns.update(kwargs)
    ns["result"] = result
    violated, errors = [], []
    for k, code in enumerate(props):
        if code is None:
            errors.append([k, "SyntaxError"])
            continue
        try:
            if not eval(code, {"__builtins__": builtins, "math": math}, dict(ns)):  # noqa: S307 - the
                # agent's own property, evaluated inside the isolated run only
                violated.append(k)
        except _Blocked as b:
            errors.append([k, f"blocked: {b}"[:MSG_MAX]])
        except BaseException as exc:  # noqa: BLE001
            errors.append([k, f"{type(exc).__name__}: {exc}"[:MSG_MAX]])
    return violated, errors


def _one_call(target, method, spec, case, props, names, timeout, hang_file) -> tuple[dict, float]:
    _S.events = []
    ms = 0.0
    if timeout:
        faulthandler.dump_traceback_later(timeout, exit=True, file=hang_file)
    _S.phase = "call"
    _S.active = True
    fn = args = kwargs = res = None
    try:
        try:
            fn = _bound(target, method, spec)
            args = [decode(a) for a in case.get("a", [])]
            kwargs = {k: decode(v) for k, v in case.get("k", [])}
            setup_error = None
        except _Blocked:
            raise
        except BaseException as exc:  # noqa: BLE001 - a recipe that fails is recorded, not fatal
            setup_error = _exc(exc)
            setup_error["at"] = "setup"
        if setup_error is not None:
            out = setup_error
        else:
            t0 = time.perf_counter()
            try:
                res = fn(*args, **kwargs)
                lazy = _type_name(res) if _is_iterator(res) else None
                if lazy:  # a generator or iterator: what it yields is the behaviour, not its repr
                    res = list(itertools.islice(res, MATERIALIZE_MAX))
                ms = (time.perf_counter() - t0) * 1000
                out = _result(res)
                if lazy:
                    out["t"] = f"{lazy} (materialized)"
                if props:
                    pv, pe = _check_properties(props, names, args, kwargs, res)
                    if pv:
                        out["pv"] = pv
                    if pe:
                        out["pe"] = pe
            except _Blocked:
                raise
            except BaseException as exc:  # noqa: BLE001 - SystemExit, KeyboardInterrupt from the function too
                ms = (time.perf_counter() - t0) * 1000
                out = _exc(exc)
            dig = _args_digest(args, kwargs)
            if dig:
                out["ap"] = dig
    except _Blocked as b:
        out = {"b": str(b)[:MSG_MAX]}
    finally:
        # what the call made is released while its window is open: a result's __del__, a weakref.finalize
        # callback or a generator's finally run now, as part of this call, not later outside it
        fn = args = kwargs = res = None
        try:
            gc.collect(1)
        except BaseException:  # noqa: BLE001 - a finalizer's error is recorded through the hook, if at all
            pass
        _S.active = False
        _S.phase = "idle"
        if timeout:
            faulthandler.cancel_dump_traceback_later()
    if _S.events:
        out["ev"] = list(_S.events)
        if _S.block and "b" not in out:
            out = {"b": f"{_S.events[0]['kind']}: {_S.events[0]['event']} {_S.events[0]['detail']}"[:MSG_MAX],
                   "ev": out["ev"], "swallowed": True}
    return out, ms


def _scaling(target, method, spec: dict, write, hang_file) -> None:
    sc = spec["scaling"]
    idx = int(sc["index"])
    sizes = [int(s) for s in sc["sizes"]]
    budget = float(sc.get("budget_s", 1.0))
    rounds = int(sc.get("rounds", 3))
    samples = int(sc.get("samples", 5))
    base = sc["base"]
    elem = sc.get("element")
    kind = sc["kind"]

    def build(n: int):
        if kind == "str":
            return ("abcdefghij" * (n // 10 + 1))[:n]
        tmpl = decode(elem) if elem is not None else 0
        items = [_vary(tmpl, k) for k in range(n)]
        if kind == "tuple":
            return tuple(items)
        if kind == "set":
            return set(items) if all(isinstance(x, (int, float, str, bytes, tuple)) for x in items[:1]) else items
        return items

    def call_once(arg) -> tuple[float | None, str | None]:
        _S.events = []
        _S.phase = "call"
        _S.active = True
        faulthandler.dump_traceback_later(max(10.0, budget * 10), exit=True, file=hang_file)
        args = fn = None
        try:
            args = [decode(a) for a in base]
            args[idx] = arg
            fn = _bound(target, method, spec)
            t0 = time.perf_counter()
            fn(*args)
            return time.perf_counter() - t0, None
        except _Blocked as b:
            return None, f"blocked: {b}"[:MSG_MAX]
        except BaseException as exc:  # noqa: BLE001
            return None, f"{type(exc).__name__}: {exc}"[:MSG_MAX]
        finally:
            args = fn = arg = None
            _S.active = False
            _S.phase = "idle"
            faulthandler.cancel_dump_traceback_later()

    out_rounds, problem, too_slow = [], None, set()
    for _ in range(rounds):
        times: list = []
        for n in sizes:
            if problem or n in too_slow:
                break
            if len(times) >= 2 and times[-2] > 0 and times[-1] * (times[-1] / times[-2]) > 3 * budget:
                too_slow.update(s for s in sizes if s >= n)  # predicted far over the budget: not called
                break
            t1, err = call_once(build(n))
            if err:
                problem = {"size": n, "error": err}
                break
            if t1 > budget:
                times.append(t1)
                too_slow.update(s for s in sizes if s >= n)  # measured once; later rounds stop before it
                break
            loops = 1 if t1 >= 0.002 else min(200, max(1, int(0.002 / max(t1, 1e-7))))
            n_samples = 1 if t1 > budget / 4 else (3 if t1 > 0.02 else samples)
            per = []
            for _s in range(n_samples):
                args_list = [build(n) for _ in range(loops)] if loops * n <= 2_000_000 else [build(n)] * loops
                total = 0.0
                for a in args_list:
                    t, err = call_once(a)
                    if err:
                        problem = {"size": n, "error": err}
                        break
                    total += t
                if problem:
                    break
                per.append(total / loops)
            if problem:
                break
            per.sort()
            times.append(per[len(per) // 2])
        out_rounds.append(times)
        if problem:
            break
    write({"k": "scaling", "sizes": sizes, "rounds": out_rounds, "budget_s": budget,
           **({"problem": problem} if problem else {}),
           **({"not_called": sorted(too_slow)} if too_slow else {})})


def _main() -> None:
    import verinoda_probe_spec  # written next to this plugin by Verinoda

    spec = json.loads(verinoda_probe_spec.SPEC_JSON)
    art = os.environ.get("VERINODA_ARTIFACTS") or os.getcwd()
    out_path = os.path.join(art, os.environ.get("VERINODA_PROBE_OUT", "probe.jsonl"))
    start = int(os.environ.get("VERINODA_PROBE_START", "0") or 0)
    fh = open(out_path, "a", encoding="utf-8")
    hang_file = open(os.path.join(art, "probe_hang.txt"), "w", encoding="utf-8")

    def write(rec: dict) -> None:
        fh.write(json.dumps(rec, ensure_ascii=True) + "\n")
        fh.flush()

    _S.block = bool(spec.get("block", True))
    _S.stray = []
    _S.run_root = os.path.dirname(os.path.abspath(os.getcwd()))
    _S.allowed_root = os.path.normcase(_S.run_root) + os.sep
    write({"k": "header", "schema": SCHEMA, "python": sys.version.split()[0], "start": start,
           "side": os.environ.get("VERINODA_PROBE_SIDE", ""), "block": _S.block, "cases": len(spec["cases"])})
    for rel in reversed(spec.get("sys_path") or []):
        p = os.path.abspath(rel)
        if p not in sys.path:
            sys.path.insert(0, p)
    _S.main_thread = threading.get_ident()
    _S.known_threads = frozenset(t.ident for t in threading.enumerate())
    sys.addaudithook(_hook)
    _guard_fork_exec()
    _capture_atexit()
    _S.events = []
    _S.phase = "import"
    _S.active = True
    try:
        target, method = _target(spec)
        err = None
    except _Blocked as b:
        target, method, err = None, None, {"blocked": str(b)[:MSG_MAX], "ev": list(_S.events)}
    except _Mismatch as m:
        target, method, err = None, None, {"e": "module_mismatch", "m": _norm(str(m))[:MSG_MAX], "mismatch": True}
    except BaseException as exc:  # noqa: BLE001 - an import error is the answer, not a crash
        target, method, err = None, None, _exc(exc)
    finally:
        _S.armed = True  # from here to the end the main thread is restricted outside the calls too
        _S.active = False
        _S.phase = "idle"
    if err is None and _S.events:
        err = {"blocked": f"{_S.events[0]['kind']}: {_S.events[0]['event']} {_S.events[0]['detail']}",
               "ev": list(_S.events)} if _S.block else None
    import_events = list(_S.events)
    if err is not None:
        write({"k": "import_error", **err})
        write({"k": "footer", "complete": True, "rows": 0})
        _end_process(fh, hang_file)
    if import_events:
        write({"k": "import_events", "ev": import_events})
    write({"k": "ready"})
    props = _properties(spec)
    names = _param_names(spec)
    repeat = max(1, int(spec.get("repeat", 2)))
    timeout = float(spec.get("per_call_timeout") or 0)
    rows = 0
    for i in range(start, len(spec["cases"])):
        case = spec["cases"][i]
        _S.last_input = i
        outs, times = [], []
        for _r in range(repeat):
            o, ms = _one_call(target, method, spec, case, props if _r == 0 else [], names, timeout, hang_file)
            outs.append(o)
            times.append(round(ms, 4))
        write({"k": "row", "i": i, "x": outs, "ms": times})
        rows += 1
    if spec.get("scaling"):
        _scaling(target, method, spec, write, hang_file)
    alive = _wait_threads(min(THREAD_WAIT_MAX, max(1.0, timeout)))
    _exit_phase()
    if _S.stray or alive:
        write({"k": "threads", "ev": list(_S.stray), "alive": alive})
    write({"k": "footer", "complete": True, "rows": rows})
    _end_process(fh, hang_file)


def _end_process(fh, hang_file) -> None:
    """End the run here: interpreter exit would run what the project left for it (exit handlers registered
    before the plugin kept them, finalizers of objects still alive) with nothing recording it, and a project
    thread still running would keep the process alive past pytest's exit."""
    for f in (fh, hang_file):
        try:
            f.close()
        except (OSError, ValueError):
            pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except (OSError, ValueError, AttributeError):
            pass
    os._exit(0)


def _wait_threads(limit_s: float) -> int:
    """Wait (at most ``limit_s`` in all) for the threads the project started; the number still running."""
    end = time.monotonic() + limit_s
    left = 0
    for th in threading.enumerate():
        if th.ident == _S.main_thread or th.ident in _S.known_threads:
            continue
        try:
            th.join(max(0.0, end - time.monotonic()))
        except (RuntimeError, AssertionError):  # a dummy thread (started outside the threading module)
            pass
        left += th.is_alive()
    return left


@pytest.hookimpl(trylast=True)
def pytest_sessionstart(session):
    try:
        _main()
    except BaseException as exc:  # noqa: BLE001 - reported to Verinoda through the missing footer
        _S.armed = _S.active = False  # the plugin's own error report is not the project's side effect
        try:
            art = os.environ.get("VERINODA_ARTIFACTS") or os.getcwd()
            with open(os.path.join(art, os.environ.get("VERINODA_PROBE_OUT", "probe.jsonl")), "a",
                      encoding="utf-8") as fh:
                fh.write(json.dumps({"k": "plugin_error", **_exc(exc)}, ensure_ascii=True) + "\n")
        except OSError:
            pass
    pytest.exit("verinoda probe finished", returncode=0)
