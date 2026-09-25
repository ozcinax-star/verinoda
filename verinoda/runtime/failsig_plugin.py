"""Verinoda failure-signature pytest plugin: where each failing test crashed, and through which code.

Standalone on purpose, like ``calltrace_plugin.py``: only the standard library
and pytest. :mod:`verinoda.debug` copies this file *next to* (never into) the
throw-away repository copy made by :mod:`verinoda.experiments`, puts that
directory on ``PYTHONPATH`` and loads it with ``-p verinoda_failsig``. It never
imports Verinoda and never changes a test's outcome.

For every failed report (setup, call or teardown phase) it records the
exception type (``__qualname__``, module), the first line of its message,
pytest's crash location (``reprcrash``: path, line, message) and the traceback
entries inside the repository copy (path relative to the copy, line, the
function's qualified name). A failed collection records pytest's text report,
which Verinoda parses. At the end it records each test's outcome, the
session's exit status and whether the run stopped early (``-x``,
``--maxfail``, ``--stepwise``: later tests were not reached). Under
pytest-xdist only the controller writes the file (outcomes and collection
errors; the workers' tracebacks are not in it, and
Verinoda falls back to pytest's text output for them).

Output: ``$VERINODA_ARTIFACTS/failsig.jsonl`` (JSON lines; schema
``verinoda.failsig/1``). Paths use ``/``; frames outside the copy (standard
library, site-packages, a virtualenv inside the copy) are left out.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

SCHEMA = "verinoda.failsig/1"
ROOT_RAW = os.path.abspath(os.getcwd())
ROOT = os.path.normcase(ROOT_RAW)
_ROOT_PREFIX = ROOT.rstrip("\\/") + os.sep
_SELF = os.path.normcase(os.path.abspath(__file__))
_EXCLUDED = tuple(os.path.normcase(os.path.join(ROOT, d)) + os.sep
                  for d in (".venv", "venv", ".tox", ".nox", "node_modules", ".eggs"))
OUT_DIR = os.environ.get("VERINODA_ARTIFACTS") or os.getcwd()
OUT_FILE = os.path.join(OUT_DIR, "failsig.jsonl")
MAX_FRAMES = 40
MAX_MSG = 2000
MAX_TESTS = 5000
MAX_LONGREPR = 8000

_failures: list = []
_outcomes: dict = {}
_state = {"exitstatus": None, "written": False, "collect_errors": 0, "stopped_early": False}
_quals: dict = {}
_source_quals: dict = {}


def _in_repo(filename) -> bool:
    f = os.path.normcase(os.path.abspath(str(filename)))
    return f.startswith(_ROOT_PREFIX) and f != _SELF and not f.startswith(_EXCLUDED) \
        and (os.sep + "site-packages" + os.sep) not in f


def _rel(filename) -> str:
    try:
        return os.path.relpath(os.path.abspath(str(filename)), ROOT_RAW).replace("\\", "/")
    except ValueError:
        return str(filename).replace("\\", "/")


def _qual_from_source(code):
    """Python 3.10 has no ``co_qualname``: find ``Class.method`` from the file's syntax tree."""
    f = code.co_filename
    table = _source_quals.get(f)
    if table is None:
        table = _source_quals[f] = {}
        try:
            import ast

            with open(f, encoding="utf-8") as fh:
                tree = ast.parse(fh.read())
        except (OSError, SyntaxError, ValueError, UnicodeDecodeError):
            return None

        def walk(node, prefix):
            for ch in ast.iter_child_nodes(node):
                if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    q = prefix + ch.name
                    if not isinstance(ch, ast.ClassDef):
                        for ln in [ch.lineno] + [d.lineno for d in ch.decorator_list]:
                            table.setdefault((ln, ch.name), q)
                    walk(ch, q + ("." if isinstance(ch, ast.ClassDef) else ".<locals>."))
                else:
                    walk(ch, prefix)

        walk(tree, "")
    return table.get((code.co_firstlineno, code.co_name))


def _qual(code) -> str:
    q = getattr(code, "co_qualname", None)
    if q:
        return q
    q = _quals.get(code)
    if q is None:
        q = _quals[code] = _qual_from_source(code) or code.co_name
    return q


def _exc_name(exc_type) -> str:
    try:
        return getattr(exc_type, "__qualname__", None) or exc_type.__name__
    except Exception:  # noqa: BLE001 - a hostile type must not break the run
        return "<unknown>"


def _first_line(text) -> str:
    s = str(text or "").strip()
    return s.splitlines()[0][:MAX_MSG] if s else ""


def _frames(excinfo) -> list:
    out = []
    try:
        entries = list(excinfo.traceback)
    except Exception:  # noqa: BLE001
        return out
    for entry in entries:
        try:
            path = str(entry.path)
            if not _in_repo(path):
                continue
            code = entry.frame.code.raw
            out.append({"path": _rel(path), "line": int(entry.lineno) + 1, "qual": _qual(code)})
        except Exception:  # noqa: BLE001
            continue
    return out[-MAX_FRAMES:]


def _crash(report) -> dict | None:
    longrepr = getattr(report, "longrepr", None)
    crash = getattr(longrepr, "reprcrash", None)
    if crash is None:
        # a doctest failure: the location of the failing example (ReprFailDoctest.reprlocation_lines)
        try:
            crash = (getattr(longrepr, "reprlocation_lines", None) or [(None, None)])[0][0]
        except Exception:  # noqa: BLE001
            crash = None
    if crash is None:
        return None
    try:
        path = str(crash.path)
        return {"path": _rel(path) if _in_repo(path) else None, "abs_in_repo": _in_repo(path),
                "line": int(crash.lineno), "message": _first_line(crash.message)}
    except Exception:  # noqa: BLE001
        return None


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    try:
        rep = outcome.get_result()
    except Exception:  # noqa: BLE001
        return
    if not rep.failed or call.excinfo is None or getattr(rep, "wasxfail", None) is not None:
        return
    exc = call.excinfo
    try:
        value = exc.value
        msg = _first_line(str(value)) if str(value) else ""
    except Exception:  # noqa: BLE001
        msg = ""
    rec = {"k": "failure", "test": item.nodeid, "phase": call.when, "exc": _exc_name(exc.type),
           "exc_module": getattr(exc.type, "__module__", None), "msg": msg, "frames": _frames(exc)}
    crash = _crash(rep)
    if crash:
        rec["crash"] = crash
    if len(_failures) < MAX_TESTS:
        _failures.append(rec)


def pytest_collectreport(report):
    if not report.failed:
        return
    _state["collect_errors"] += 1
    try:
        text = str(report.longreprtext or "")
    except Exception:  # noqa: BLE001
        text = ""
    if len(_failures) < MAX_TESTS:
        _failures.append({"k": "failure", "test": report.nodeid, "phase": "collect", "longrepr": text[-MAX_LONGREPR:]})


def pytest_runtest_logreport(report):
    if len(_outcomes) >= MAX_TESTS and report.nodeid not in _outcomes:
        return
    res = _outcomes.setdefault(report.nodeid, "passed")
    if report.failed:
        res = "error" if report.when != "call" else "failed"
    elif report.skipped and report.when in ("setup", "call") and res == "passed":
        res = "xfailed" if getattr(report, "wasxfail", None) is not None else "skipped"
    _outcomes[report.nodeid] = res


def _write() -> None:
    header = {"k": "header", "schema": SCHEMA, "python": sys.version.split()[0], "root": ROOT_RAW,
              "exitstatus": _state["exitstatus"], "collect_errors": _state["collect_errors"],
              "stopped_early": _state["stopped_early"], "tests": len(_outcomes), "failures": len(_failures)}
    lines = [json.dumps(header, separators=(",", ":"))]
    lines += [json.dumps(r, separators=(",", ":")) for r in _failures]
    lines.append(json.dumps({"k": "outcomes", "tests": _outcomes}, separators=(",", ":")))
    os.makedirs(OUT_DIR, exist_ok=True)
    tmp = OUT_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write("\n".join(lines) + "\n")
    os.replace(tmp, OUT_FILE)
    _state["written"] = True


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session, exitstatus):
    _state["exitstatus"] = int(exitstatus)
    # -x / --maxfail set shouldfail, --stepwise sets shouldstop: the tests after that point were not reached
    try:
        _state["stopped_early"] = bool(getattr(session, "shouldfail", False) or getattr(session, "shouldstop", False))
    except Exception:  # noqa: BLE001
        pass
    if hasattr(session.config, "workerinput"):  # a pytest-xdist worker: only the controller writes the file
        _state["written"] = True
        return
    try:
        _write()
    except (OSError, ValueError, TypeError):
        pass


def pytest_unconfigure(config):
    if not _state["written"] and not hasattr(config, "workerinput"):
        try:
            _write()
        except (OSError, ValueError, TypeError):
            pass
