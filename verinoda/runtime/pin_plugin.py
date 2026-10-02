"""Verinoda pin pytest plugin: record one function's inputs and results while the project's tests run.

Standalone on purpose: only the standard library and pytest. Verinoda copies
this file *next to* (never into) the throw-away repository copy made by
:mod:`verinoda.experiments`, puts that directory on ``PYTHONPATH`` and loads it
with ``-p verinoda_pin``. It never imports Verinoda.

Modes (``VERINODA_PIN_MODE``):

``record`` (default)
    ``sys.monitoring`` (Python 3.12+) watches one code object: the function
    named by ``VERINODA_PIN_FILE`` (path relative to the copy) and
    ``VERINODA_PIN_QUAL`` (its qualified name). ``PY_START`` is enabled
    globally and returns ``DISABLE`` for every other code object, so the rest
    of the suite runs untraced after its first call; ``PY_RETURN`` is enabled on
    the target only, and ``PY_UNWIND`` (global by nature) returns at once for
    other frames. For each call the arguments are rendered at entry - before
    the function can change them - and the return value or the raised
    exception (type and message) when it ends. A value is kept only as Python
    source that rebuilds an equal value of the same type: literals (``None``,
    bool, int, float, str, bytes, tuple, list, dict, set, frozenset),
    ``decimal.Decimal``, ``fractions.Fraction``, ``datetime`` date, datetime
    and timedelta values, enum members by their qualified name, and dataclass
    instances of project classes rebuilt by their constructor from such
    values. The source is evaluated once more and compared with the original
    (type and value, all the way down); a call with any other value is
    skipped and counted under its reason. ``self`` of an instance method is an
    argument like the others: only a rebuildable dataclass instance is kept.
    Distinct calls are kept up to ``VERINODA_PIN_MAX_INPUTS``, with the tests
    that made them.

``replay``
    No tracer: only the outcome of every test phase, to tell which generated
    tests passed.

Output: ``$VERINODA_ARTIFACTS/$VERINODA_PIN_OUT`` (default ``pin.jsonl``), JSON
lines: one ``header`` (schema ``verinoda.pin/1``), one ``test`` record per
test (phase outcomes) and in record mode one ``case`` record per distinct
input (``call``: receiver, positional and keyword argument sources; ``outs``:
the distinct results seen; ``imports``; ``tests``; ``hits``).
"""

from __future__ import annotations

import builtins
import dataclasses
import datetime
import decimal
import enum
import fractions
import json
import os
import sys
import threading

import pytest

SCHEMA = "verinoda.pin/1"
MODE = (os.environ.get("VERINODA_PIN_MODE") or "record").lower()
ROOT_RAW = os.path.abspath(os.getcwd())
ROOT = os.path.normcase(ROOT_RAW)
_ROOT_PREFIX = ROOT.rstrip("\\/") + os.sep
OUT_DIR = os.environ.get("VERINODA_ARTIFACTS") or os.getcwd()
OUT_FILE = os.path.join(OUT_DIR, os.path.basename(os.environ.get("VERINODA_PIN_OUT") or "pin.jsonl"))
TARGET_REL = (os.environ.get("VERINODA_PIN_FILE") or "").replace("\\", "/")
TARGET_FILE = os.path.normcase(os.path.abspath(os.path.join(ROOT_RAW, TARGET_REL))) if TARGET_REL else ""
TARGET_QUAL = os.environ.get("VERINODA_PIN_QUAL") or ""
KIND = os.environ.get("VERINODA_PIN_KIND") or "function"   # function | static | class | method
try:
    MAX_INPUTS = max(1, int(os.environ.get("VERINODA_PIN_MAX_INPUTS") or 500))
except ValueError:
    MAX_INPUTS = 500
MAX_LEN = 10_000        # characters of one string/bytes value, items of one container
MAX_DEPTH = 20
MAX_SOURCE = 20_000     # characters of the source of one value
MAX_MESSAGE = 500       # characters of an exception message
TESTS_PER_CASE = 5
_GEN_FLAGS = 0x20 | 0x100 | 0x200   # CO_GENERATOR | CO_COROUTINE | CO_ASYNC_GENERATOR
_TEST_DIRS = ("tests", "test", "testing")

ctx = "<collection>"
_state = {"tracer": "off", "tool_id": None, "active": False, "written": False, "exitstatus": None,
          "error": None, "module": None, "calls": 0}
_cases: dict = {}       # call key -> {"call", "imports", "outs": {out key: out}, "tests": [...], "hits"}
_skipped: dict = {}     # reason -> calls
_outcomes: dict = {}    # nodeid -> {"phases": {...}}
_stacks: dict = {}      # thread id -> [entry or None] for calls of the target in flight
_local = threading.local()
_targets: dict = {}     # code object -> bool


class _Skip(Exception):
    """A value (or call) that cannot be pinned; the message is the reason counted in the header."""


def _skip(reason: str) -> None:
    _skipped[reason] = _skipped.get(reason, 0) + 1


# -- values as source -----------------------------------------------------------------------------

def _is_test_file(filename: str | None) -> bool:
    if not filename:
        return False
    try:
        rel = os.path.relpath(os.path.abspath(filename), ROOT_RAW).replace("\\", "/")
    except ValueError:  # another drive: not the project's
        return False
    base = rel.rpartition("/")[2]
    return (base.startswith("test_") or base.endswith("_test.py") or base == "conftest.py"
            or any(p in _TEST_DIRS for p in rel.split("/")[:-1]))


def _in_repo(filename: str | None) -> bool:
    return bool(filename) and os.path.normcase(os.path.abspath(filename)).startswith(_ROOT_PREFIX)


def _module_ref(cls, imports: set, what: str) -> str:
    """``module.Qual`` for an importable class (not local, not a test module's), adding the import."""
    mod = getattr(cls, "__module__", None) or ""
    qual = getattr(cls, "__qualname__", None) or ""
    if not mod or mod == "__main__" or "<" in qual or not all(p.isidentifier() for p in mod.split(".")):
        raise _Skip(f"{what} {mod}.{qual} cannot be imported by name")
    m = sys.modules.get(mod)
    if m is None:
        raise _Skip(f"{what} {mod}.{qual} cannot be imported by name")
    if _is_test_file(getattr(m, "__file__", None)):
        raise _Skip(f"{what} {mod}.{qual} is defined in test code")
    obj = m
    for part in qual.split("."):
        obj = getattr(obj, part, None)
    if obj is not cls:
        raise _Skip(f"{what} {mod}.{qual} is not reachable by its qualified name")
    imports.add(mod)
    return f"{mod}.{qual}"


def _float_src(v: float) -> str:
    if v != v:
        return "float('nan')"
    if v in (float("inf"), float("-inf")):
        return "float('inf')" if v > 0 else "float('-inf')"
    return repr(v)


def encode(v, imports: set, depth: int = 0, seen: set | None = None) -> str:
    """Python source that rebuilds ``v`` (see the module docstring); raises :class:`_Skip` with the reason."""
    t = type(v)
    if v is None or t is bool:
        return repr(v)
    if t is int:
        try:
            return repr(v)
        except ValueError:
            raise _Skip("integer too long to write as text") from None
    if t is float:
        return _float_src(v)
    if t in (str, bytes):
        if len(v) > MAX_LEN:
            raise _Skip(f"{t.__name__} longer than {MAX_LEN}")
        return repr(v)
    if depth > MAX_DEPTH:
        raise _Skip("value nested too deeply")
    seen = seen if seen is not None else set()
    if t in (tuple, list, set, frozenset, dict):
        if len(v) > MAX_LEN:
            raise _Skip(f"{t.__name__} with more than {MAX_LEN} items")
        if id(v) in seen:
            raise _Skip("value contains itself")
        seen.add(id(v))
        try:
            if t is dict:
                return "{" + ", ".join(f"{encode(k, imports, depth + 1, seen)}: {encode(x, imports, depth + 1, seen)}"
                                       for k, x in v.items()) + "}"
            items = [encode(x, imports, depth + 1, seen) for x in v]
        finally:
            seen.discard(id(v))
        if t is tuple:
            return "(" + ", ".join(items) + ("," if len(items) == 1 else "") + ")"
        if t is list:
            return "[" + ", ".join(items) + "]"
        items.sort()   # a set's iteration order is not part of its value
        if t is set:
            return "{" + ", ".join(items) + "}" if items else "set()"
        return "frozenset({" + ", ".join(items) + "})" if items else "frozenset()"
    if t is decimal.Decimal:
        imports.add("decimal")
        return f"decimal.Decimal({str(v)!r})"
    if t is fractions.Fraction:
        imports.add("fractions")
        return f"fractions.Fraction({v.numerator!r}, {v.denominator!r})"
    if t in (datetime.date, datetime.datetime, datetime.timedelta):
        tz = getattr(v, "tzinfo", None)
        if tz is not None and type(tz) is not datetime.timezone:
            raise _Skip(f"datetime with time zone {type(tz).__module__}.{type(tz).__qualname__}")
        imports.add("datetime")
        return repr(v)
    if isinstance(v, enum.Enum):
        ref = _module_ref(t, imports, "enum")
        name = v.name
        if isinstance(name, str) and name.isidentifier() and getattr(t, name, None) is v:
            return f"{ref}.{name}"
        if not isinstance(name, str):
            raise _Skip(f"enum member of {t.__qualname__} without a name")
        return f"{ref}[{name!r}]"
    if dataclasses.is_dataclass(v) and not isinstance(v, type):
        mod = sys.modules.get(getattr(t, "__module__", "") or "")
        if not _in_repo(getattr(mod, "__file__", None)):
            raise _Skip(f"dataclass {t.__module__}.{t.__qualname__} is not a project class")
        ref = _module_ref(t, imports, "dataclass")
        if id(v) in seen:
            raise _Skip("value contains itself")
        seen.add(id(v))
        try:
            parts = []
            for f in dataclasses.fields(v):
                if not f.init:
                    raise _Skip(f"dataclass {t.__qualname__} has a field its constructor does not set ({f.name})")
                parts.append(f"{f.name}={encode(getattr(v, f.name), imports, depth + 1, seen)}")
        finally:
            seen.discard(id(v))
        return f"{ref}(" + ", ".join(parts) + ")"
    raise _Skip(f"unsupported type {t.__module__}.{t.__qualname__}")


def same(a, b) -> bool:
    """Equal values of the same types all the way down (1 == 1.0 == True is not enough); NaN equals NaN."""
    if type(a) is not type(b):
        return False
    if type(a) is float:
        return a == b or (a != a and b != b)
    if type(a) in (list, tuple):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    if type(a) is dict:
        return a.keys() == b.keys() and all(same(a[k], b[k]) for k in a)
    return bool(a == b)


_SAFE_BUILTINS = {"float": float, "set": set, "frozenset": frozenset}


def rebuild(src: str, imports: set):
    ns: dict = {"__builtins__": _SAFE_BUILTINS}
    for mod in imports:
        top = mod.split(".")[0]
        ns[top] = sys.modules[top]
    return eval(src, ns)  # noqa: S307 - source this plugin wrote from literals and imported names only


def value_source(v, imports: set) -> str:
    """:func:`encode`, then evaluated again and compared: the source must rebuild an equal value."""
    mine: set = set()
    src = encode(v, mine)
    if len(src) > MAX_SOURCE:
        raise _Skip(f"value longer than {MAX_SOURCE} characters as source")
    try:
        back = rebuild(src, mine)
    except Exception as exc:  # noqa: BLE001 - a constructor that does not take its own fields back
        raise _Skip(f"value does not rebuild from its source ({type(exc).__name__})") from None
    try:
        ok = same(back, v)
    except Exception:  # noqa: BLE001 - an __eq__ that raises
        ok = False
    if not ok:
        raise _Skip(f"value of type {type(v).__module__}.{type(v).__qualname__} is not equal to its rebuilt copy")
    imports |= mine
    return src


def exception_source(exc: BaseException, imports: set) -> tuple[str, str]:
    """(type reference, message) of an exception a test can name; raises :class:`_Skip`."""
    t = type(exc)
    if not isinstance(exc, Exception):
        raise _Skip(f"raised {t.__qualname__}, not an Exception")
    if t.__module__ == "builtins" and getattr(builtins, t.__name__, None) is t:
        ref = t.__name__
    else:
        mine: set = set()
        ref = _module_ref(t, mine, "exception")
        imports |= mine
    try:
        msg = str(exc)
    except Exception:  # noqa: BLE001
        raise _Skip("exception message cannot be read") from None
    if len(msg) > MAX_MESSAGE:
        raise _Skip(f"exception message longer than {MAX_MESSAGE} characters")
    if " at 0x" in msg:
        raise _Skip("exception message holds a memory address")
    if ROOT_RAW in msg or ROOT_RAW.replace("\\", "/") in msg:
        raise _Skip("exception message holds the run's directory")
    return ref, msg


# -- recorder ---------------------------------------------------------------------------------------

def _is_target(code) -> bool:
    r = _targets.get(code)
    if r is None:
        r = _targets[code] = bool(TARGET_FILE) and getattr(code, "co_qualname", code.co_name) == TARGET_QUAL \
            and os.path.normcase(os.path.abspath(code.co_filename)) == TARGET_FILE
    return r


def _arguments(code, frame) -> list:
    """[(kind, name, value)] of the call's parameters, kind in pos / star / kw / starstar."""
    names = code.co_varnames
    n_pos, n_kw = code.co_argcount, code.co_kwonlyargcount
    loc = frame.f_locals
    out = [("pos", names[i], loc[names[i]]) for i in range(n_pos)]
    out += [("kw", names[n_pos + i], loc[names[n_pos + i]]) for i in range(n_kw)]
    i = n_pos + n_kw
    if code.co_flags & 0x04:
        out.append(("star", names[i], loc[names[i]]))
        i += 1
    if code.co_flags & 0x08:
        out.append(("starstar", names[i], loc[names[i]]))
    return out


def _call_entry(code, frame) -> dict:
    """The call as sources, or raise :class:`_Skip`."""
    if code.co_flags & _GEN_FLAGS:
        raise _Skip("generator or coroutine function")
    imports: set = set()
    args = _arguments(code, frame)
    recv = None
    cls_name = TARGET_QUAL.rpartition(".")[0]
    if KIND in ("method", "class"):
        if not args or args[0][0] != "pos":
            raise _Skip("no self/cls argument")
        first = args.pop(0)[2]
        if KIND == "method":
            if not (dataclasses.is_dataclass(first) and not isinstance(first, type)):
                raise _Skip("self is not a dataclass instance")
            if type(first).__qualname__ != cls_name:
                raise _Skip("self is an instance of a subclass")
            recv = value_source(first, imports)
        elif getattr(first, "__qualname__", None) != cls_name or \
                getattr(first, "__module__", None) != frame.f_globals.get("__name__"):
            raise _Skip("classmethod called on a subclass")
    pos, kw = [], []
    for kind, name, value in args:
        if kind == "pos":
            pos.append(value_source(value, imports))
        elif kind == "star":
            if type(value) is not tuple:
                raise _Skip("*args is not a tuple")
            pos += [value_source(x, imports) for x in value]
        elif kind == "kw":
            kw.append([name, value_source(value, imports)])
        else:
            if type(value) is not dict or not all(type(k) is str for k in value):
                raise _Skip("**kwargs keys are not strings")
            kw += [[k, value_source(x, imports)] for k, x in value.items()]
    if _state["module"] is None:
        _state["module"] = frame.f_globals.get("__name__")
    return {"call": {"self": recv, "args": pos, "kwargs": kw}, "imports": imports}


def _stack() -> list:
    tid = threading.get_ident()
    s = _stacks.get(tid)
    if s is None:
        s = _stacks[tid] = []
    return s


def _finish(entry, out: dict | None, reason: str | None) -> None:
    if entry is None:
        return
    if isinstance(entry, str):        # the call itself was skipped at entry
        _skip(entry)
        return
    if out is None:
        _skip(reason or "result not recorded")
        return
    key = json.dumps(entry["call"], sort_keys=True)
    case = _cases.get(key)
    if case is None:
        if len(_cases) >= MAX_INPUTS:
            _skip(f"more than {MAX_INPUTS} distinct inputs")
            return
        case = _cases[key] = {"call": entry["call"], "imports": set(), "outs": {}, "tests": [], "hits": 0}
    case["imports"] |= entry["imports"] | out.pop("_imports", set())
    case["outs"].setdefault(json.dumps(out, sort_keys=True), out)
    case["hits"] += 1
    test = ctx.rpartition("|")[0] if "|" in ctx and not ctx.startswith("<") else ctx
    if test not in case["tests"] and len(case["tests"]) < TESTS_PER_CASE:
        case["tests"].append(test)


def _on_start(code, frame) -> None:
    _state["calls"] += 1
    if getattr(_local, "busy", False):   # a call made while this plugin rebuilds a value: not the tests'
        _stack().append(None)
        return
    _local.busy = True
    try:
        entry = _call_entry(code, frame)
    except _Skip as exc:
        entry = str(exc)
    except Exception as exc:  # noqa: BLE001 - a value whose repr/eq/getattr misbehaves
        entry = f"argument could not be read ({type(exc).__name__})"
    finally:
        _local.busy = False
    _stack().append(entry)


def _on_end(retval=None, exc: BaseException | None = None) -> None:
    s = _stack()
    if not s:
        return
    entry = s.pop()
    if entry is None or isinstance(entry, str):
        _finish(entry, None, None)
        return
    _local.busy = True
    try:
        imports: set = set()
        if exc is not None:
            ref, msg = exception_source(exc, imports)
            out = {"e": ref, "m": msg}
        else:
            out = {"r": value_source(retval, imports)}
        out["_imports"] = imports
        _finish(entry, out, None)
    except _Skip as e:
        _finish(entry, None, ("result: " if exc is None else "") + str(e))
    except Exception as e:  # noqa: BLE001
        _finish(entry, None, f"result could not be read ({type(e).__name__})")
    finally:
        _local.busy = False


def _start_monitoring() -> bool:
    mon = getattr(sys, "monitoring", None)
    if mon is None:
        _state["error"] = "sys.monitoring is not available (Python 3.12+ is needed)"
        return False
    tool = next((i for i in (4, 3, 2) if mon.get_tool(i) is None), None)
    if tool is None:
        _state["error"] = "no free sys.monitoring tool id"
        return False
    mon.use_tool_id(tool, "verinoda-pin")
    DISABLE = mon.DISABLE
    E = mon.events
    getframe = sys._getframe

    def py_start(code, offset):
        if not _is_target(code):
            return DISABLE
        mon.set_local_events(tool, code, E.PY_RETURN)
        _on_start(code, getframe(1))
        return None

    def py_return(code, offset, retval):
        if not _is_target(code):
            return DISABLE
        _on_end(retval=retval)
        return None

    def py_unwind(code, offset, exc):
        if _targets.get(code):
            _on_end(exc=exc)

    mon.register_callback(tool, E.PY_START, py_start)
    mon.register_callback(tool, E.PY_RETURN, py_return)
    mon.register_callback(tool, E.PY_UNWIND, py_unwind)
    mon.set_events(tool, E.PY_START | E.PY_UNWIND)
    _state.update(tracer="sys.monitoring", tool_id=tool, active=True)
    return True


def _stop() -> None:
    if not _state["active"]:
        return
    _state["active"] = False
    mon = sys.monitoring
    tool = _state["tool_id"]
    try:
        mon.set_events(tool, 0)
        for ev in (mon.events.PY_START, mon.events.PY_RETURN, mon.events.PY_UNWIND):
            mon.register_callback(tool, ev, None)
        mon.free_tool_id(tool)
    except (ValueError, RuntimeError):
        pass


# -- output -----------------------------------------------------------------------------------------

def _write() -> None:
    header = {"k": "header", "schema": SCHEMA, "mode": MODE, "tracer": _state["tracer"],
              "python": sys.version.split()[0], "target": [TARGET_REL, TARGET_QUAL], "kind": KIND,
              "module": _state["module"], "calls": _state["calls"], "cases": len(_cases),
              "skipped": dict(sorted(_skipped.items())), "exitstatus": _state["exitstatus"],
              "error": _state["error"], "max_inputs": MAX_INPUTS}
    lines = [json.dumps(header, separators=(",", ":"))]
    lines += [json.dumps({"k": "test", "id": nid, **res}, separators=(",", ":"))
              for nid, res in sorted(_outcomes.items())]
    for case in _cases.values():
        lines.append(json.dumps({"k": "case", "call": case["call"], "imports": sorted(case["imports"]),
                                 "outs": list(case["outs"].values()), "tests": case["tests"],
                                 "hits": case["hits"]}, separators=(",", ":")))
    os.makedirs(OUT_DIR, exist_ok=True)
    tmp = OUT_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write("\n".join(lines) + "\n")
    os.replace(tmp, OUT_FILE)
    _state["written"] = True


# -- pytest hooks -----------------------------------------------------------------------------------

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
    res = _outcomes.setdefault(report.nodeid, {"phases": {}})
    res["phases"][report.when] = report.outcome


@pytest.hookimpl(tryfirst=True)
def pytest_sessionfinish(session, exitstatus):
    if not ACTIVE_ON_IMPORT:
        return
    _stop()
    _state["exitstatus"] = int(exitstatus)
    _write()


def pytest_unconfigure(config):
    if not ACTIVE_ON_IMPORT:
        return
    _stop()
    if not _state["written"]:
        _write()


# Record only when loaded as the pytest plugin copied next to an experiment copy; imported as
# verinoda.runtime.pin_plugin (inside Verinoda itself) it does nothing.
ACTIVE_ON_IMPORT = __name__ != "verinoda.runtime.pin_plugin"
if ACTIVE_ON_IMPORT and MODE == "record":
    _start_monitoring()
