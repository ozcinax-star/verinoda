"""Failure signatures: what failed, with which exception, where - as a symbol (docs/DESIGN.md D34).

A signature is one record per failing test (or per crash when no test
framework ran): test id, phase, exception type, first message line, the crash
location (the innermost frame inside the repository) mapped to a *symbol*
(``path::Qualified.name`` via :func:`verinoda.anchors.enclosing`, so a line
shift does not make a new signature) and the in-repository frames.

Sources, first match wins:

* the pytest plugin ``verinoda_failsig`` (:mod:`verinoda.runtime.failsig_plugin`):
  exception type, message and in-repo traceback entries from pytest itself;
* regular expressions over the log for pytest text output, Python tracebacks
  and unittest, JUnit/Gradle/Maven (``at pkg.Cls.m(File.java:N)``), Go
  (``--- FAIL``, panics), Rust panics and Node stacks (uncaught errors,
  ``node --test``, Jest).

Two keys are derived (:func:`keys`): ``sig_exact`` over (test, exception,
symbol, normalised message) and ``sig_coarse`` over (exception, symbol).
Messages are normalised for memory addresses, UUIDs, long ids, durations and
temp/copy paths. A failing run whose output yields no complete failure record
has status ``unknown`` (or ``partial``) and no keys: it never counts as "the
same" as anything.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Callable, Iterable

PLUGIN_MODULE = "verinoda_failsig"
PLUGIN_FILE = "failsig.jsonl"
MAX_FAILURES = 200
MAX_FRAMES = 12

# -- message normalisation ----------------------------------------------------------------------

_NORM = [
    (re.compile(r"(?:[A-Za-z]:)?[\\/](?:[^\s'\"<>|]*[\\/])?verinoda-exp-[^\\/\s'\"]+[\\/]repo[\\/]"), ""),
    (re.compile(r"(?:[A-Za-z]:)?[\\/](?:[^\s'\"<>|]*[\\/])?pytest-of-[^\\/\s'\"]+[\\/]pytest-\d+[\\/][^\\/\s'\"]+"),
     "<tmp>"),
    (re.compile(r"(?:[A-Za-z]:)?[\\/](?:[^\s'\"<>|]*[\\/])?(?:Temp|tmp)[\\/]tmp[\w-]+"), "<tmp>"),
    (re.compile(r"0x[0-9a-fA-F]+"), "0x?"),
    (re.compile(r"(?<=[\w'\"\]>)])\s?@[0-9a-fA-F]{5,16}\b"), "@?"),  # JVM identity hashes: Obj@1b6d3586, 'app' @5e2d
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), "<uuid>"),
    (re.compile(r"\b\d{7,}\b"), "<n>"),
    (re.compile(r"\b\d+(?:\.\d+)?\s?(?:ms|s)\b"), "<t>"),
    (re.compile(r"\s+"), " "),
]


def norm_msg(msg: str | None) -> str:
    s = (msg or "").strip()
    for rx, rep in _NORM:
        s = rx.sub(rep, s)
    return s.strip()[:300]


# -- paths and symbols ---------------------------------------------------------------------------

_FOREIGN = ("site-packages/", "dist-packages/", "node_modules/", "/lib/python", "<frozen", "node:internal",
            "internal/", "/rustc/", "/.cargo/registry/", "/go/src/runtime/", "/go/src/testing/", "<anonymous>")
_COPY_ROOT = re.compile(r"^.*?/verinoda-exp-[^/]+/repo/(.+)$")


class PathResolver:
    """Map a path as a log prints it (absolute, in the throw-away copy, Windows or POSIX) to a repository file."""

    def __init__(self, files: Iterable[str], roots: Iterable[str] = ()):
        self.files = set(files)
        self.by_name: dict[str, list[str]] = {}
        for f in self.files:
            self.by_name.setdefault(f.rsplit("/", 1)[-1], []).append(f)
        self.roots = [r.replace("\\", "/").rstrip("/").lower() for r in roots if r]

    def resolve(self, raw: str | None) -> str | None:
        """The repository path a logged path names, or None (outside the repository, or not unique).

        A path under the experiment copy or a given root is cut to its relative
        part. Otherwise the longest repository path that is a whole suffix of
        the logged path wins (``C:/Python312/Lib/importlib/__init__.py`` does
        not match ``orders/__init__.py``); a logged path that is itself a short
        relative tail (``WispTest.java``, ``src\\lib.rs``) matches the one file
        that ends with it.
        """
        if not raw:
            return None
        s = raw.strip().strip("\"'")
        if s.startswith("file:///"):
            s = s[len("file:///"):]
        elif s.startswith("file://"):
            s = s[len("file://"):]
        s = s.replace("\\", "/")
        low = s.lower()
        m = _COPY_ROOT.match(s)
        if m and m.group(1) in self.files:
            return m.group(1)
        for r in self.roots:
            if low.startswith(r + "/") and s[len(r) + 1:] in self.files:
                return s[len(r) + 1:]
        if any(f in low for f in _FOREIGN):
            return None
        s = re.sub(r"^(?:\./)+", "", s)
        if s in self.files:
            return s
        parts = [p for p in s.split("/") if p and p != "."]
        if not parts:
            return None
        cands = self.by_name.get(parts[-1], [])
        best, best_len, tie = None, 0, False
        for c in cands:
            cp = c.split("/")
            k = 0
            while k < min(len(cp), len(parts)) and cp[-1 - k] == parts[-1 - k]:
                k += 1
            if k < len(cp) and k < len(parts):  # neither path is a whole suffix of the other
                continue
            if k > best_len:
                best, best_len, tie = c, k, False
            elif k == best_len:
                tie = True
        if best is None or tie:
            return None
        return best

    def resolve_java(self, cls: str, file_name: str | None) -> str | None:
        """``pkg.sub.Outer$Inner`` + ``Outer.java`` -> ``src/main/java/pkg/sub/Outer.java``."""
        pkg = cls.rsplit(".", 1)[0] if "." in cls else ""
        top = cls.rsplit(".", 1)[-1].split("$")[0]
        name = file_name or f"{top}.java"
        rel = (pkg.replace(".", "/") + "/" if pkg else "") + name
        cands = [f for f in self.by_name.get(name, []) if f == rel or f.endswith("/" + rel)]
        if len(cands) == 1:
            return cands[0]
        if not cands and not pkg:
            return self.resolve(name)
        return None


class SymbolMapper:
    """``(path, line) -> symbol`` over the content of the tree that ran (``reader(path) -> bytes | None``)."""

    def __init__(self, reader: Callable[[str], bytes | None] | None):
        self.reader = reader
        self._facts: dict[str, object] = {}

    def facts(self, path: str):
        if path not in self._facts:
            data = self.reader(path) if self.reader else None
            f = None
            if data is not None:
                from verinoda import anchors

                try:
                    f = anchors.compute_facts(path, data)
                except Exception:  # noqa: BLE001 - mapping is best effort
                    f = None
            self._facts[path] = f
        return self._facts[path]

    def symbol(self, path: str, line: int | None, hint: str | None = None) -> str | None:
        """The definition around ``line`` by the file's syntax tree; the frame's own name (``hint``) or, for
        Python, the enclosing ``def``/``class`` by indentation only when the file cannot be parsed."""
        from verinoda import anchors
        from verinoda.treestate import symbol_at

        facts = self.facts(path)
        if anchors.usable(facts) and line:
            s = symbol_at(facts, int(line))
            if s and not s.startswith("#"):
                return s
        if line and path.endswith((".py", ".pyi")):
            s = _py_indent_symbol(self.reader(path) if self.reader else None, int(line))
            if s:
                return s
        return clean_qual(hint) if hint else None


_PY_DEF = re.compile(r"^(\s*)(?:async\s+)?(?:def|class)\s+(\w+)")


def _py_indent_symbol(data: bytes | None, line: int) -> str | None:
    """``Class.method`` holding ``line`` by indentation alone (for a file with a syntax error)."""
    if data is None:
        return None
    lines = data.decode("utf-8", "replace").split("\n")
    if not 0 < line <= len(lines):
        return None
    target = lines[line - 1]
    indent = len(target) - len(target.lstrip()) if target.strip() else 10 ** 6
    names: list[str] = []
    for ln in reversed(lines[:line - 1]):
        m = _PY_DEF.match(ln)
        if m and len(m.group(1)) < indent:
            names.append(m.group(2))
            indent = len(m.group(1))
            if indent == 0:
                break
        elif ln.strip() and not ln.lstrip().startswith(("#", "@", ")", "]", "}")) and \
                len(ln) - len(ln.lstrip()) < indent:
            indent = len(ln) - len(ln.lstrip())
            if indent == 0:
                break
    return ".".join(reversed(names)) or "<module>"


def clean_qual(q: str | None) -> str | None:
    """``outer.<locals>.inner`` -> ``outer.inner``; comprehension/lambda parts dropped (``f.<locals>.<genexpr>`` -> ``f``)."""
    if not q:
        return None
    parts = [p for p in q.split(".") if p and p != "<locals>"]
    while len(parts) > 1 and parts[-1].startswith("<") and parts[-1] != "<module>":
        parts.pop()
    return ".".join(parts) or None


# -- records --------------------------------------------------------------------------------------

def _failure(test, phase, exc, msg, loc, frames, mapper: SymbolMapper | None, hint=None) -> dict:
    path, line = (loc or (None, None))
    if exc and exc not in ("go.TestFailure", "runtime.Error"):
        exc = exc.rsplit(".", 1)[-1]
    sym = mapper.symbol(path, line, hint) if (mapper and path) else (clean_qual(hint) if path else None)
    rec = {"test": test, "phase": phase, "exc": exc, "msg": (msg or "").strip()[:500], "msg_norm": norm_msg(msg),
           "path": path, "line": line, "symbol": sym,
           "at": f"{path}::{sym}" if path and sym else (f"{path}:?" if path else None),
           "frames": []}
    for f in (frames or [])[-MAX_FRAMES:]:
        fp, fl, fq = f
        fs = mapper.symbol(fp, fl, fq) if mapper else clean_qual(fq)
        rec["frames"].append({"path": fp, "line": fl, "symbol": fs})
    return rec


def complete(f: dict) -> bool:
    return bool(f.get("exc")) and bool(f.get("path")) and bool(f.get("symbol"))


def keys(sig: dict) -> tuple[str | None, str | None]:
    """``(sig_exact, sig_coarse)``; both None unless every failure is complete (status ``parsed``)."""
    if sig.get("status") != "parsed" or not sig.get("failures"):
        return None, None
    exact = sorted([f.get("test") or "", f["exc"], f["at"], f.get("msg_norm") or ""] for f in sig["failures"])
    coarse = sorted({(f["exc"], f["at"]) for f in sig["failures"]})
    h = lambda o: hashlib.sha256(json.dumps(o, sort_keys=True).encode()).hexdigest()[:16]  # noqa: E731
    return h(exact), h([list(c) for c in coarse])


def _finish(parser: str, failures: list[dict], extra: dict | None = None) -> dict:
    failures = failures[:MAX_FAILURES]
    status = "parsed" if failures and all(complete(f) for f in failures) else ("partial" if failures else "unknown")
    out = {"status": status, "parser": parser, "failures": failures,
           "failed_tests": sorted({f["test"] for f in failures if f.get("test")})}
    out.update(extra or {})
    return out


def summary(sig: dict) -> list[str]:
    """One line per failure: ``Exc@path::symbol (test)``."""
    out = []
    for f in sig.get("failures") or []:
        out.append(f"{f.get('exc') or '?'}@{f.get('at') or '?'}" + (f" ({f['test']})" if f.get("test") else ""))
    return out


# -- the plugin --------------------------------------------------------------------------------------

def plugin_source() -> bytes:
    return (Path(__file__).parent / "runtime" / "failsig_plugin.py").read_bytes()


def from_plugin(data: bytes, resolver: PathResolver, mapper: SymbolMapper | None) -> dict | None:
    """The signature from the plugin's JSON lines, or None when they are unusable."""
    header, recs, outcomes = None, [], {}
    for raw in data.decode("utf-8", "replace").splitlines():
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        if rec.get("k") == "header":
            header = rec
        elif rec.get("k") == "failure":
            recs.append(rec)
        elif rec.get("k") == "outcomes" and isinstance(rec.get("tests"), dict):
            outcomes = rec["tests"]
    if header is None or header.get("schema") != "verinoda.failsig/1":
        return None
    failures = []
    for r in recs:
        if r.get("phase") == "collect":
            inner = parse_text(r.get("longrepr") or "", resolver, mapper, prefer="pytest")
            got = inner.get("failures") or []
            if got:
                for f in got:
                    f["test"] = r.get("test") or f.get("test")
                    f["phase"] = "collect"
                failures.extend(got)
            else:
                failures.append(_failure(r.get("test"), "collect", None, (r.get("longrepr") or "")[-300:], None, [],
                                         mapper))
            continue
        frames = []
        for fr in r.get("frames") or []:
            p = resolver.resolve(fr.get("path"))
            if p:
                frames.append((p, int(fr.get("line") or 0), fr.get("qual")))
        loc = (frames[-1][0], frames[-1][1]) if frames else None
        hint = frames[-1][2] if frames else None
        failures.append(_failure(r.get("test"), r.get("phase"), r.get("exc"), r.get("msg"), loc, frames, mapper, hint))
    out = _finish("pytest_plugin", failures, {"tests": outcomes, "exitstatus": header.get("exitstatus")})
    if outcomes:
        out["failed_tests"] = sorted(t for t, o in outcomes.items() if o in ("failed", "error"))
    return out


# -- text parsers ----------------------------------------------------------------------------------

_EXC_LINE = re.compile(r"^([A-Za-z_][\w.]*)(?::\s?(.*))?$")
_PY_EXC_NAME = re.compile(r"^[A-Za-z_][\w.]*(Error|Exception|Exit|Interrupt|Warning|Failure|Failed|Iteration|Timeout|"
                          r"Fault|Refused|Denied|Invalid|NotFound|Abort|Cancelled)\w*$|^[A-Z]\w*$")


def _is_exc_name(name: str) -> bool:
    last = name.rsplit(".", 1)[-1]
    return bool(_PY_EXC_NAME.match(last)) and last not in ("E", "FAILED", "ERROR", "PASSED", "FAIL", "OK")


# pytest -----------------------------------------------------------------------------------------

_PT_MARK = re.compile(r"^(=+ (FAILURES|ERRORS|short test summary info) =+$|(FAILED|ERROR) \S+?(::\S+|\.py)( - .*)?$)",
                      re.M)
_PT_SECTION = re.compile(r"^_{3,} (.+?) _{3,}$")
_PT_BLOCK = re.compile(r"^=+ (FAILURES|ERRORS|warnings summary|short test summary info|.*(passed|failed|error).*) =+$")
_PT_LOC = re.compile(r"^(?P<path>(?:[A-Za-z]:)?[^\s:<>|\"][^:<>|\"\n]*?\.[A-Za-z0-9]+):(?P<line>\d+):"
                     r"(?: in (?P<func>\S+)| (?P<exc>[A-Za-z_][\w.]*)(?:: (?P<msg>.*))?)?\s*$")
_PT_SUMMARY = re.compile(r"^(FAILED|ERROR) (\S+?)(?: - (.*))?$", re.M)
_PT_E = re.compile(r"^E\s{1,}(.*)$")
_PT_E_FILE = re.compile(r'^E\s+File "(.+?)", line (\d+)')
_PT_SUBTESTS = re.compile(r"\[(.*)\]$")


def _pt_title_to_id(title: str, summary_ids: list[str]) -> str:
    t = title.strip()
    for pre in ("ERROR at setup of ", "ERROR at teardown of ", "ERROR at call of "):
        if t.startswith(pre):
            t = t[len(pre):]
    if t.startswith("ERROR collecting "):
        return t[len("ERROR collecting "):]
    want = "::" + t.replace(".", "::")
    for sid in summary_ids:
        if sid.endswith(want) or sid == t:
            return sid
    return t


def _pt_phase(title: str) -> str:
    t = title.strip()
    if t.startswith("ERROR collecting "):
        return "collect"
    if t.startswith("ERROR at setup of "):
        return "setup"
    if t.startswith("ERROR at teardown of "):
        return "teardown"
    return "call"


def _pt_section(lines: list[str], resolver: PathResolver) -> tuple:
    """(exc, msg, crash location, frames) of one pytest failure section."""
    starts = [i for i, ln in enumerate(lines) if ln.strip() == _TB_START]
    if starts and not any(_PT_E.match(ln) for ln in lines):  # --tb=native: a plain Python traceback
        tb_frames, syn, exc, msg, _ = _traceback_block(lines, starts[-1], resolver)
        loc = syn if (syn and exc in ("SyntaxError", "IndentationError", "TabError")) else \
            ((tb_frames[-1][0], tb_frames[-1][1]) if tb_frames else None)
        return exc, msg, loc, tb_frames
    frames: list[tuple[str, int, str | None]] = []
    exc = msg = None
    e_lines: list[str] = []
    loc_exc = None
    for ln in lines:
        m = _PT_LOC.match(ln)
        if m:
            p = resolver.resolve(m.group("path"))
            if p:
                frames.append((p, int(m.group("line")), m.group("func")))
            if m.group("exc") and _is_exc_name(m.group("exc")):
                loc_exc = m.group("exc")
                if m.group("msg") and not msg:
                    msg = m.group("msg")
            continue
        e = _PT_E.match(ln)
        if e:
            e_lines.append(e.group(1))
    efile = None
    for ln in lines:
        m = _PT_E_FILE.match(ln)
        if m:
            efile = (resolver.resolve(m.group(1)), int(m.group(2)))
    for text in e_lines:
        t = text.strip()
        m = _EXC_LINE.match(t)
        if m and _is_exc_name(m.group(1)) and (m.group(2) is not None or t == m.group(1)):
            exc, msg = m.group(1), m.group(2) or ""
            break
    first = next((t.strip() for t in e_lines if t.strip()), "")
    if exc is None and first.startswith("assert"):
        exc, msg = "AssertionError", first
    if loc_exc:
        if exc is None or (exc != loc_exc and loc_exc.rsplit(".", 1)[-1] != exc.rsplit(".", 1)[-1]):
            exc = loc_exc
            msg = msg or first
    if msg is None:
        msg = first
    loc = None
    if efile and efile[0] and exc in ("SyntaxError", "IndentationError", "TabError"):
        loc = efile
    elif frames:
        loc = (frames[-1][0], frames[-1][1])
    return exc, msg, loc, frames


def _parse_pytest(text: str, resolver: PathResolver, mapper) -> list[dict] | None:
    if not _PT_MARK.search(text):
        return None
    summary_items = [(m.group(1), m.group(2), m.group(3) or "") for m in _PT_SUMMARY.finditer(text)]
    summary_ids = [s[1] for s in summary_items]
    lines = text.splitlines()
    sections: list[tuple[str, list[str]]] = []
    in_block = False
    cur: tuple[str, list[str]] | None = None
    for ln in lines:
        b = _PT_BLOCK.match(ln)
        if b:
            in_block = b.group(1) in ("FAILURES", "ERRORS")
            if cur:
                sections.append(cur)
                cur = None
            continue
        if not in_block:
            continue
        s = _PT_SECTION.match(ln)
        if s and not ln.startswith("_ _"):
            if cur:
                sections.append(cur)
            cur = (s.group(1), [])
            continue
        if cur:
            cur[1].append(ln)
    if cur:
        sections.append(cur)
    failures: list[dict] = []
    seen: set[str] = set()
    for title, body in sections:
        exc, msg, loc, frames = _pt_section(body, resolver)
        tid = _pt_title_to_id(title, summary_ids)
        seen.add(tid)
        hint = frames[-1][2] if frames and loc and loc == (frames[-1][0], frames[-1][1]) else None
        failures.append(_failure(tid, _pt_phase(title), exc, msg, loc, frames, mapper, hint))
    # --tb=line / --tb=no: only the location lines and/or the short summary
    if not sections:
        locs = []
        for ln in lines:
            m = _PT_LOC.match(ln)
            if m and m.group("exc") and _is_exc_name(m.group("exc")):
                p = resolver.resolve(m.group("path"))
                locs.append(((p, int(m.group("line"))) if p else None, m.group("exc"), m.group("msg") or ""))
        for i, (kind, sid, smsg) in enumerate(summary_items):
            if sid in seen:
                continue
            if i < len(locs) and len(locs) == len(summary_items):
                loc, exc, msg = locs[i]
            else:
                loc, exc, msg = None, None, smsg
                m = _EXC_LINE.match(smsg.strip())
                if m and _is_exc_name(m.group(1)):
                    exc, msg = m.group(1), m.group(2) or ""
                elif smsg.strip().startswith("assert"):
                    exc = "AssertionError"
            failures.append(_failure(sid, "call" if kind == "FAILED" else "error", exc, msg, loc,
                                     [(loc[0], loc[1], None)] if loc else [], mapper))
    return failures or None


# Python tracebacks and unittest ---------------------------------------------------------------------

_TB_START = "Traceback (most recent call last):"
_TB_FILE = re.compile(r'^\s*File "(.+?)", line (\d+)(?:, in (.+))?\s*$')
_UT_HEAD = re.compile(r"^(FAIL|ERROR): (\S+) \(([^)]*)\)")


def _traceback_block(lines: list[str], start: int, resolver: PathResolver):
    """(frames, exc, msg, end index) of the traceback starting at ``lines[start]``."""
    frames: list[tuple[str, int, str | None]] = []
    syntax_loc = None
    i = start + 1
    while i < len(lines):
        ln = lines[i]
        m = _TB_FILE.match(ln)
        if m:
            p = resolver.resolve(m.group(1))
            if m.group(3) is None:
                syntax_loc = (p, int(m.group(2))) if p else None
            elif p:
                frames.append((p, int(m.group(2)), m.group(3).strip()))
            i += 1
            continue
        if ln.startswith((" ", "\t")) or not ln.strip():
            i += 1
            continue
        m = _EXC_LINE.match(ln.strip())
        if m and _is_exc_name(m.group(1)):
            return frames, syntax_loc, m.group(1), m.group(2) or "", i
        return frames, syntax_loc, None, None, i
    return frames, syntax_loc, None, None, i


def _parse_python(text: str, resolver: PathResolver, mapper) -> list[dict] | None:
    if _TB_START not in text:
        return None
    lines = text.splitlines()
    failures: list[dict] = []
    ut = [i for i, ln in enumerate(lines) if _UT_HEAD.match(ln)]
    if ut:
        for k, i in enumerate(ut):
            m = _UT_HEAD.match(lines[i])
            name, where = m.group(2), m.group(3)
            tid = where if where.endswith("." + name) else f"{where}.{name}"
            end = ut[k + 1] if k + 1 < len(ut) else len(lines)
            starts = [j for j in range(i, end) if lines[j].strip() == _TB_START]
            if not starts:
                failures.append(_failure(tid, "call", None, None, None, [], mapper))
                continue
            frames, syn, exc, msg, _ = _traceback_block(lines, starts[-1], resolver)
            loc = syn if (syn and exc in ("SyntaxError", "IndentationError", "TabError")) else \
                ((frames[-1][0], frames[-1][1]) if frames else None)
            hint = frames[-1][2] if frames and loc == (frames[-1][0], frames[-1][1]) else None
            failures.append(_failure(tid, "call" if m.group(1) == "FAIL" else "error", exc, msg, loc, frames, mapper,
                                     hint))
        return failures
    starts = [i for i, ln in enumerate(lines) if ln.strip() == _TB_START]
    frames, syn, exc, msg, _ = _traceback_block(lines, starts[-1], resolver)  # the last of a chain
    if exc is None and not frames:
        return None
    loc = syn if (syn and exc in ("SyntaxError", "IndentationError", "TabError")) else \
        ((frames[-1][0], frames[-1][1]) if frames else None)
    hint = frames[-1][2] if frames and loc == (frames[-1][0], frames[-1][1]) else None
    return [_failure(None, "run", exc, msg, loc, frames, mapper, hint)]


# JVM (JUnit, Gradle, Maven, plain stack traces) -------------------------------------------------------

_J_FRAME = re.compile(r"^\s*at ([\w$.<>/]+)\.([\w$<>]+)\(([^)]*)\)")
_J_EXC = re.compile(r"^\s*(?:Exception in thread \"[^\"]*\" |Caused by: )?"
                    r"((?:[a-zA-Z_$][\w$]*\.)+[A-Za-z_$][\w$]*(?:Exception|Error|Throwable|Failure|Failed)\w*"
                    r"|[A-Z][\w$]*(?:Exception|Error))(?:: ?(.*))?$")
_J_SHORT = re.compile(r"^\s*((?:[a-zA-Z_$][\w$]*\.)*[A-Za-z_$][\w$]*(?:Exception|Error|Throwable|Failure|Failed)\w*)"
                      r" at ([\w$]+\.(?:java|kt|scala|groovy)):(\d+)\s*$")
_J_GRADLE_TEST = re.compile(r"^(\S.*?) > (.+?) FAILED\s*$")
_J_SUREFIRE = re.compile(r"^\[ERROR\] ([\w$]+)\(([\w.$]+)\)\s+Time elapsed.*<<< (?:FAILURE|ERROR)!")
_J_SUREFIRE5 = re.compile(r"^\[ERROR\] ([\w.$]+)\.([\w$]+)\s+(?:-- )?Time elapsed.*<<< (?:FAILURE|ERROR)!")
_J_MARK = re.compile(r"^\s*at [\w$.<>/]+\([\w$]+\.(?:java|kt|scala|groovy):\d+\)|^\S.*? > .+? FAILED\s*$|"
                     r"^Exception in thread |^\s*(?:[a-z_$][\w$]*\.)+[A-Z]\w*(?:Exception|Error)\w* at \w+\.java:\d+",
                     re.M)


def _java_symbol_hint(cls: str, method: str) -> str:
    top = cls.rsplit(".", 1)[-1]
    parts = [p for p in top.split("$") if p and not p.isdigit()]
    m = method
    lm = re.match(r"^lambda\$(\w+?)\$\d+$", m)
    if lm:
        m = lm.group(1)
    if m in ("<init>", "<clinit>"):
        m = parts[-1] if parts else m
    return ".".join(parts + [m])


def _java_block(lines: list[str], resolver: PathResolver) -> tuple:
    """(exc, msg, loc, frames, hint) of one JVM failure block (root cause first choice)."""
    groups: list[dict] = []
    for ln in lines:
        m = _J_EXC.match(ln)
        if m and not _J_FRAME.match(ln):
            groups.append({"exc": m.group(1).rsplit(".", 1)[-1], "msg": m.group(2) or "", "frames": []})
            continue
        s = _J_SHORT.match(ln)
        if s:
            p = resolver.resolve(s.group(2))
            groups.append({"exc": s.group(1).rsplit(".", 1)[-1], "msg": "",
                           "frames": [(p, int(s.group(3)), None)] if p else []})
            continue
        f = _J_FRAME.match(ln)
        if f and groups:
            cls, meth, where = f.group(1).rsplit("/", 1)[-1], f.group(2), f.group(3)
            fm = re.match(r"^([\w$]+\.(?:java|kt|scala|groovy)):(\d+)$", where)
            if not fm:
                continue
            p = resolver.resolve_java(cls, fm.group(1))
            if p:
                groups[-1]["frames"].append((p, int(fm.group(2)), _java_symbol_hint(cls, meth)))
    if not groups:
        return None
    root = groups[-1]
    for g in reversed(groups):
        if g["frames"]:
            chosen = g
            break
    else:
        chosen = root
    frames = list(reversed(chosen["frames"]))  # innermost last, like Python
    loc = (chosen["frames"][0][0], chosen["frames"][0][1]) if chosen["frames"] else None
    hint = chosen["frames"][0][2] if chosen["frames"] else None
    return root["exc"], root["msg"], loc, frames, hint


def _parse_jvm(text: str, resolver: PathResolver, mapper) -> list[dict] | None:
    if not _J_MARK.search(text):
        return None
    lines = text.splitlines()
    heads: list[tuple[int, str]] = []
    for i, ln in enumerate(lines):
        g = _J_GRADLE_TEST.match(ln)
        if g:
            heads.append((i, f"{g.group(1).strip()} > {g.group(2).strip()}"))
            continue
        s = _J_SUREFIRE.match(ln)
        if s:
            heads.append((i, f"{s.group(2)}.{s.group(1)}"))
            continue
        s5 = _J_SUREFIRE5.match(ln)
        if s5:
            heads.append((i, f"{s5.group(1)}.{s5.group(2)}"))
    failures: list[dict] = []
    if heads:
        for k, (i, tid) in enumerate(heads):
            end = heads[k + 1][0] if k + 1 < len(heads) else len(lines)
            body = lines[i + 1:end]
            got = _java_block(body, resolver)
            if got is None:
                failures.append(_failure(tid, "call", None, None, None, [], mapper))
                continue
            exc, msg, loc, frames, hint = got
            failures.append(_failure(tid, "call", exc, msg, loc, frames, mapper, hint))
        return failures
    got = _java_block(lines, resolver)
    if got is None:
        return None
    exc, msg, loc, frames, hint = got
    return [_failure(None, "run", exc, msg, loc, frames, mapper, hint)]


# Go ------------------------------------------------------------------------------------------------

_GO_FAIL = re.compile(r"^\s*--- FAIL: (\S+)")
_GO_MSG = re.compile(r"^\s+(\S+\.go):(\d+): ?(.*)$")
_GO_PANIC = re.compile(r"^panic: (.*?)(?: \[recovered\])?\s*$")
_GO_FUNC = re.compile(r"^([\w./\-]+?)\.((?:\(\*?[\w]+\)\.)?[\w]+(?:\.func\d+)*)\(.*\)\s*$")
_GO_AT = re.compile(r"^\s+(\S+\.go):(\d+)(?: \+0x[0-9a-f]+)?\s*$")


def _go_symbol_hint(fn: str) -> str:
    m = re.match(r"^\(\*?(\w+)\)\.(\w+)", fn)
    base = f"{m.group(1)}.{m.group(2)}" if m else fn
    return re.sub(r"(\.func\d+)+$", "", base)


def _parse_go(text: str, resolver: PathResolver, mapper) -> list[dict] | None:
    if not (re.search(r"^\s*--- FAIL: ", text, re.M) or (re.search(r"^panic: ", text, re.M)
                                                         and "goroutine " in text)):
        return None
    lines = text.splitlines()
    failures: list[dict] = []
    panic_idx = next((i for i, ln in enumerate(lines) if _GO_PANIC.match(ln)), None)
    if panic_idx is not None:
        msg = _GO_PANIC.match(lines[panic_idx]).group(1)
        exc = "runtime.Error" if msg.startswith("runtime error:") else "panic"
        frames: list[tuple[str, int, str | None]] = []
        i = panic_idx + 1
        while i < len(lines):
            fm = _GO_FUNC.match(lines[i].strip())
            if fm and i + 1 < len(lines):
                at = _GO_AT.match(lines[i + 1])
                if at:
                    p = resolver.resolve(at.group(1))
                    if p:
                        frames.append((p, int(at.group(2)), _go_symbol_hint(fm.group(2))))
                    i += 2
                    continue
            i += 1
        test = next((m.group(1) for ln in lines for m in [_GO_FAIL.match(ln)] if m), None)
        inner = frames[0] if frames else None
        failures.append(_failure(test, "call", exc, msg, (inner[0], inner[1]) if inner else None,
                                 list(reversed(frames)), mapper, inner[2] if inner else None))
        return failures
    for i, ln in enumerate(lines):
        m = _GO_FAIL.match(ln)
        if not m:
            continue
        test = m.group(1)
        loc = msg = None
        for nxt in lines[i + 1:i + 30]:
            if _GO_FAIL.match(nxt) or nxt.startswith(("FAIL", "ok ", "=== RUN")):
                break
            g = _GO_MSG.match(nxt)
            if g:
                p = resolver.resolve(g.group(1))
                loc, msg = ((p, int(g.group(2))) if p else None), g.group(3)
                break
        failures.append(_failure(test, "call", "go.TestFailure", msg, loc, [(loc[0], loc[1], None)] if loc else [],
                                 mapper))
    # a parent test fails because its subtest failed: keep the innermost ones
    names = [f["test"] for f in failures]
    failures = [f for f in failures if not any(n != f["test"] and n.startswith(f["test"] + "/") for n in names)]
    return failures or None


# Rust ----------------------------------------------------------------------------------------------

_RS_NEW = re.compile(r"^thread '([^']*)'(?: \(\d+\))? panicked at (.+?\.rs):(\d+):(\d+):\s*$")
_RS_OLD = re.compile(r"^thread '([^']*)'(?: \(\d+\))? panicked at '(.*)', (.+?\.rs):(\d+):(\d+)\s*$")
_RS_AT = re.compile(r"^\s+at (\S+\.rs):(\d+):(\d+)\s*$")
_RS_FN = re.compile(r"^\s+\d+: ([\w:<>]+?)(?:::h[0-9a-f]{16})?\s*$")


def _parse_rust(text: str, resolver: PathResolver, mapper) -> list[dict] | None:
    if "panicked at" not in text:
        return None
    lines = text.splitlines()
    failures: list[dict] = []
    for i, ln in enumerate(lines):
        m, msg = _RS_NEW.match(ln), None
        if m:
            thread, path, line = m.group(1), m.group(2), int(m.group(3))
            msg = lines[i + 1].strip() if i + 1 < len(lines) else ""
        else:
            m = _RS_OLD.match(ln)
            if not m:
                continue
            thread, msg, path, line = m.group(1), m.group(2), m.group(3), int(m.group(4))
        p = resolver.resolve(path)
        frames: list[tuple[str, int, str | None]] = []
        fn = None
        for nxt in lines[i + 1:i + 200]:
            if nxt.startswith("thread '"):
                break
            f = _RS_FN.match(nxt)
            if f:
                fn = f.group(1)
                continue
            a = _RS_AT.match(nxt)
            if a:
                q = resolver.resolve(a.group(1))
                if q:
                    frames.append((q, int(a.group(2)), fn.split("::")[-1] if fn else None))
        loc = (p, line) if p else ((frames[0][0], frames[0][1]) if frames else None)
        test = thread if thread not in ("main", "<unnamed>") else None
        failures.append(_failure(test, "call", "panic", msg, loc, list(reversed(frames)) or
                                 ([(loc[0], loc[1], None)] if loc else []), mapper))
    return failures or None


# Node -----------------------------------------------------------------------------------------------

_N_FRAME = re.compile(r"^\s*(?:at )?(?:(?:async )?(.+?) \()?((?:file:///)?(?:[A-Za-z]:)?[^\s():]*?"
                      r"\.(?:m?[jt]sx?|cjs|cts|mts)):(\d+):(\d+)\)?\s*$")
_N_ERR = re.compile(r"^\s*(?:Uncaught )?([A-Z][\w$]*(?:Error|Exception)|Error)(?: \[(\w+)\])?(?:: (.*))?$")
_N_TAP_FAIL = re.compile(r"^\s*not ok \d+ - (.+?)\s*(?:#.*)?$")
_N_SPEC_FAIL = re.compile(r"^\s*(?:✖|×|✕) (.+?)(?: \([\d.]+ ?m?s\))?\s*$")
_N_JEST = re.compile(r"^\s*● (.+?)\s*$")
_N_TAP_NAME = re.compile(r"^\s*name: '?([A-Z]\w*)'?\s*$")
_N_TAP_ERR = re.compile(r"^\s*error: (?:\|-|'(.*)'|\"(.*)\"|(.*))\s*$")
_N_MARK = re.compile(r"^\s+at .*\.(?:m?[jt]sx?|cjs):\d+:\d+\)?$|^not ok \d+ |^\s*✖ |^\s*● ", re.M)


def _node_frames(lines: list[str], resolver: PathResolver):
    frames = []
    for ln in lines:
        m = _N_FRAME.match(ln)
        if m and ("at " in ln or ln.strip().startswith(("file:", "/", "\\")) or re.match(r"^\s*[A-Za-z]:", ln)
                  or " (" in ln):
            p = resolver.resolve(m.group(2))
            if p:
                name = (m.group(1) or "").strip()
                name = re.sub(r"^(?:Object|TestContext|Test|Suite|Context)\.", "", name)
                frames.append((p, int(m.group(3)), None if (not name or "<anonymous>" in name) else name))
    return frames


def _parse_node(text: str, resolver: PathResolver, mapper) -> list[dict] | None:
    if not _N_MARK.search(text):
        return None
    lines = text.splitlines()
    heads = []
    for i, ln in enumerate(lines):
        for rx, kind in ((_N_TAP_FAIL, "tap"), (_N_SPEC_FAIL, "spec"), (_N_JEST, "jest")):
            m = rx.match(ln)
            if m:
                heads.append((i, m.group(1), kind))
                break
    heads = [h for h in heads if not h[1].rstrip().endswith(":")]  # "✖ failing tests:" is a heading
    failures: list[dict] = []
    blocks: list[tuple] = []
    if heads:
        # node --test prints a failure twice (inline and in the summary), Jest as a tick and a detail block:
        # per test name keep the block that carries the error
        by_name: dict[str, tuple] = {}
        for k, (i, name, kind) in enumerate(heads):
            end = heads[k + 1][0] if k + 1 < len(heads) else len(lines)
            body = lines[i + 1:end]
            score = sum(1 for ln in body if _N_FRAME.match(ln)) + sum(1 for ln in body if _N_ERR.match(ln))
            key = name.split(" › ")[-1].strip()
            if key not in by_name or score > by_name[key][0]:
                by_name[key] = (score, name, kind, body)
        blocks = [(n, kd, b) for _, n, kd, b in by_name.values()]
    else:
        blocks.append((None, "uncaught", lines))
    for name, kind, body in blocks:
        exc = msg = None
        for ln in body:
            m = _N_ERR.match(ln)
            if m:
                exc, msg = m.group(1), m.group(3) or ""
                break
            if kind == "tap":
                n = _N_TAP_NAME.match(ln)
                if n:
                    exc = n.group(1)
        if kind == "tap" and msg is None:
            for j, ln in enumerate(body):
                e = _N_TAP_ERR.match(ln)
                if e:
                    msg = next((x for x in e.groups() if x), None) or (body[j + 1].strip() if j + 1 < len(body) else "")
                    break
        if exc is None and kind == "jest":
            first = next((ln.strip() for ln in body if ln.strip()), "")
            if first.startswith("expect("):
                exc, msg = "expect", first
        frames = _node_frames(body, resolver)
        if not frames and exc is None:
            continue
        inner = frames[0] if frames else None
        failures.append(_failure(name, "call" if name else "run", exc, msg,
                                 (inner[0], inner[1]) if inner else None, list(reversed(frames)), mapper,
                                 inner[2] if inner else None))
    return failures or None


# -- entry points --------------------------------------------------------------------------------------

_PARSERS: tuple[tuple[str, Callable], ...] = (
    ("pytest_text", _parse_pytest), ("python_traceback", _parse_python), ("jvm", _parse_jvm),
    ("go", _parse_go), ("rust", _parse_rust), ("node", _parse_node),
)


def parse_text(text: str, resolver: PathResolver, mapper: SymbolMapper | None = None, *,
               prefer: str | None = None) -> dict:
    """The signature of a log by the regex parsers (first that finds failures wins)."""
    order = sorted(_PARSERS, key=lambda p: 0 if prefer and p[0].startswith(prefer) else 1)
    for name, fn in order:
        try:
            got = fn(text, resolver, mapper)
        except Exception:  # noqa: BLE001 - a parser bug must never break a run; the log stays unknown
            got = None
        if got:
            return _finish(name, got)
    return _finish("none", [])


def extract(stdout: str, stderr: str, *, outcome: str, files: Iterable[str],
            reader: Callable[[str], bytes | None] | None = None, plugin_data: bytes | None = None,
            roots: Iterable[str] = ()) -> dict:
    """The signature of one run: from the plugin when it wrote one, else from the logs.

    A passing run has status ``none``. ``files`` is the file set of the tree
    that ran; ``reader`` gives a file's content in that tree (for symbols).
    """
    if outcome == "pass":
        return {"status": "none", "parser": None, "failures": [], "failed_tests": []}
    resolver = PathResolver(files, roots)
    mapper = SymbolMapper(reader)
    if plugin_data:
        got = from_plugin(plugin_data, resolver, mapper)
        if got is not None and got.get("failures"):
            return got
    return parse_text((stdout or "") + "\n" + (stderr or ""), resolver, mapper)
