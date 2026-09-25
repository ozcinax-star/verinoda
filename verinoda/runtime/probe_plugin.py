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
module is imported and while each call runs. With ``spec["block"]`` it stops
file writes, network use, new processes, environment/working-directory changes,
class-attribute writes and database connections other than ``:memory:`` by
raising a ``BaseException`` subclass inside the operation; the call is recorded
as ``blocked`` with the event (also when the function swallowed the
exception). At import time writes inside the run's throw-away directory are
allowed (library caches under the run's HOME). Without ``block`` the events are
only recorded.

A call that runs longer than ``spec["per_call_timeout"]`` seconds ends the
process through ``faulthandler`` (the stack goes to ``probe_hang.txt``);
Verinoda reads the last recorded input and resumes after it in a new run.

Optional ``spec["scaling"]``: one argument built at growing sizes, timed as
the median of several calls per size, in several rounds (rough growth only).

Output: ``$VERINODA_ARTIFACTS/$VERINODA_PROBE_OUT`` (default ``probe.jsonl``),
JSON lines with schema ``verinoda.probe/1``: ``header``, ``import_error``,
``row`` (one per input), ``scaling``, ``footer``.
"""

from __future__ import annotations

import base64
import builtins
import decimal
import faulthandler
import hashlib
import importlib
import inspect
import itertools
import json
import math
import os
import re
import sys
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
                "os.startfile", "os.kill", "os.killpg", "pty.spawn", "os.popen"}
_STATE_EVENTS = {"os.putenv", "os.unsetenv", "os.chdir", "os.fchdir"}


class _Blocked(BaseException):
    """Raised inside a blocked operation; a BaseException so that ``except Exception`` does not swallow it."""


class _S:
    active = False
    phase = "idle"
    block = True
    events: list = []
    allowed_root = ""


def _detail(args) -> str:
    try:
        return " ".join(repr(a)[:120] for a in args[:3])
    except Exception:  # noqa: BLE001 - a repr may fail
        return ""


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
        if _S.phase == "import" and _S.allowed_root and isinstance(path, (str, bytes, os.PathLike)):
            try:
                p = os.path.abspath(os.fsdecode(path))
                if os.path.normcase(p).startswith(_S.allowed_root):
                    return None
            except (TypeError, ValueError):
                pass
        return "file-write"
    if event in _FS_EVENTS:
        return "file-write"
    if event in _NET_EVENTS:
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
    if event == "object.__setattr__" and _S.phase == "call" and args and isinstance(args[0], type):
        name = args[1] if len(args) > 1 else ""
        if isinstance(name, str) and not (name.startswith("__") and name.endswith("__")) \
                and not name.startswith("_abc_"):
            return "class-state"
    return None


def _hook(event: str, args) -> None:
    if not _S.active:
        return
    kind = _classify(event, args)
    if kind is None:
        return
    if len(_S.events) < EVENTS_MAX:
        _S.events.append({"kind": kind, "event": event, "detail": _detail(args)[:200]})
    if _S.block:
        raise _Blocked(f"{kind}: {event} {_detail(args)[:120]}")


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
    return _ADDR.sub("at 0x?", text)


def _type_name(v) -> str:
    t = type(v)
    return f"{t.__module__}.{t.__qualname__}"


def _result(v) -> dict:
    try:
        text = repr(v)
    except BaseException as exc:  # noqa: BLE001 - a user repr may raise anything
        text = f"<repr raised {type(exc).__name__}>"
    text = _norm(text)
    out = {"t": _type_name(v), "r": text[:REPR_MAX]}
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
        text = _norm(repr((args, sorted(kwargs.items()))))
    except BaseException:  # noqa: BLE001
        return None
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()[:16]


# -- the target ----------------------------------------------------------------------------

def _target(spec: dict):
    mod = importlib.import_module(spec["module"])
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
    try:
        try:
            fn = _bound(target, method, spec)
            args = [decode(a) for a in case.get("a", [])]
            kwargs = {k: decode(v) for k, v in case.get("k", [])}
        except _Blocked:
            raise
        except BaseException as exc:  # noqa: BLE001 - a recipe that fails is recorded, not fatal
            out = _exc(exc)
            out["at"] = "setup"
            return out, ms
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
        args = [decode(a) for a in base]
        args[idx] = arg
        _S.events = []
        _S.phase = "call"
        _S.active = True
        faulthandler.dump_traceback_later(max(10.0, budget * 10), exit=True, file=hang_file)
        try:
            fn = _bound(target, method, spec)
            t0 = time.perf_counter()
            fn(*args)
            return time.perf_counter() - t0, None
        except _Blocked as b:
            return None, f"blocked: {b}"[:MSG_MAX]
        except BaseException as exc:  # noqa: BLE001
            return None, f"{type(exc).__name__}: {exc}"[:MSG_MAX]
        finally:
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
    _S.allowed_root = os.path.normcase(os.path.dirname(os.path.abspath(os.getcwd()))) + os.sep
    write({"k": "header", "schema": SCHEMA, "python": sys.version.split()[0], "start": start,
           "side": os.environ.get("VERINODA_PROBE_SIDE", ""), "block": _S.block, "cases": len(spec["cases"])})
    for rel in reversed(spec.get("sys_path") or []):
        p = os.path.abspath(rel)
        if p not in sys.path:
            sys.path.insert(0, p)
    sys.addaudithook(_hook)
    _S.events = []
    _S.phase = "import"
    _S.active = True
    try:
        target, method = _target(spec)
        err = None
    except _Blocked as b:
        target, method, err = None, None, {"blocked": str(b)[:MSG_MAX], "ev": list(_S.events)}
    except BaseException as exc:  # noqa: BLE001 - an import error is the answer, not a crash
        target, method, err = None, None, _exc(exc)
    finally:
        _S.active = False
        _S.phase = "idle"
    if err is None and _S.events:
        err = {"blocked": f"{_S.events[0]['kind']}: {_S.events[0]['event']} {_S.events[0]['detail']}",
               "ev": list(_S.events)} if _S.block else None
    import_events = list(_S.events)
    if err is not None:
        write({"k": "import_error", **err})
        write({"k": "footer", "complete": True, "rows": 0})
        return
    if import_events:
        write({"k": "import_events", "ev": import_events})
    props = _properties(spec)
    names = _param_names(spec)
    repeat = max(1, int(spec.get("repeat", 2)))
    timeout = float(spec.get("per_call_timeout") or 0)
    rows = 0
    for i in range(start, len(spec["cases"])):
        case = spec["cases"][i]
        outs, times = [], []
        for _r in range(repeat):
            o, ms = _one_call(target, method, spec, case, props if _r == 0 else [], names, timeout, hang_file)
            outs.append(o)
            times.append(round(ms, 4))
        write({"k": "row", "i": i, "x": outs, "ms": times})
        rows += 1
    if spec.get("scaling"):
        _scaling(target, method, spec, write, hang_file)
    write({"k": "footer", "complete": True, "rows": rows})
    fh.close()


@pytest.hookimpl(trylast=True)
def pytest_sessionstart(session):
    try:
        _main()
    except BaseException as exc:  # noqa: BLE001 - reported to Verinoda through the missing footer
        try:
            art = os.environ.get("VERINODA_ARTIFACTS") or os.getcwd()
            with open(os.path.join(art, os.environ.get("VERINODA_PROBE_OUT", "probe.jsonl")), "a",
                      encoding="utf-8") as fh:
                fh.write(json.dumps({"k": "plugin_error", **_exc(exc)}, ensure_ascii=True) + "\n")
        except OSError:
            pass
    pytest.exit("verinoda probe finished", returncode=0)
