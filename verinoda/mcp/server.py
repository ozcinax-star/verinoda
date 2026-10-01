"""Verinoda MCP server: the CLI's core operations as tools for coding agents.

Design rules
------------
* One implementation: every tool calls the same core function as the matching
  CLI command (``verinoda query`` -> :func:`verinoda.retrieval.retrieve` +
  :func:`~verinoda.retrieval.render_text`, ``analyze`` ->
  :func:`verinoda.analysis.analyze`, ``plan draft/check/audit`` ->
  :mod:`verinoda.question_plan` / :func:`verinoda.analysis.audit`,
  ``resolve`` -> :func:`verinoda.references.resolve`, ``observe`` ->
  :func:`verinoda.runtime.trace.observe`, ``resolve-call`` ->
  :func:`verinoda.precise.resolve_call`, ``check``/``api`` ->
  :func:`verinoda.codecheck.check` / :func:`~verinoda.codecheck.api`, ...). :class:`AtlasTools` holds
  those adapters and has no MCP SDK dependency, so it is testable and usable
  in-process; :func:`build_server` only registers them with the SDK.
* Small, structured answers: every response is a JSON object capped at
  ``MAX_RESPONSE_CHARS`` (compact JSON, env ``VERINODA_MCP_MAX_CHARS``). Long
  lists/maps are cut and the response is marked ``"truncated": true`` with a
  ``truncation`` note naming what was cut (``kept``/``total``). A view's
  ``coverage`` (method + limits) is never cut; if that alone exceeds the cap,
  ``truncation.over_limit`` says so. ``project_query`` answers in plain text
  by default (docs/DESIGN.md D20): the text goes on the wire as the content
  block itself, not as an escaped JSON string.
* Errors never crash the server: they come back as
  ``{"error": <code>, "message": ..., "hint": <next step>, "tool": ...}``
  (flagged ``isError`` on mcp 2.x). A repository that was never scanned (no
  ``.verinoda/``) yields ``error="not_initialised"``; only ``index_update``
  creates ``.verinoda/`` (the first scan, which the agent asked for; never in a
  home folder or a drive root). Argument *type* violations are rejected by the
  SDK before a tool runs (plain-text ``isError`` result).
* Build-time work is not repeated per call (docs/DESIGN.md D21): the loaded
  graph is kept until ``graph.json`` changes (stat: mtime, size, file id);
  its symbol spans and per-file line owners are kept too and dropped per file
  when that file's stat changes; the search index handle and the lexicon are
  kept by their own stat-keyed caches. ``analyze`` and ``claim_challenge``
  reuse the kept graph (``graph=``) when it describes the working tree. A
  ``project_query`` answer is kept (64 most recent) with the stat of every
  input it was computed from - graph, ``search.db`` and its WAL, lexicon and
  each file it read lines from - and is recomputed as soon as one differs.
  Nothing derived from a file modified within ``RACY_NS`` (2 s) of the call
  is kept, so an edit inside the file system's timestamp granularity is
  never masked (git's racy-clean rule).
* The repository is resolved once at startup; the SQLite store is opened per
  call (and closed after it), so no connection crosses threads. Tool calls are
  serialised with a lock (index rebuilds and stdout redirection are global),
  and stdout is redirected to stderr during each call.
* Optional feature modules (``research``, ``feedback``, ``references``,
  ``runtime``, ``precise``, ``codecheck``) are imported lazily inside their tools, so the
  server still starts when one of them is missing or broken.
* SDK: mcp 2.x (``mcp.server.mcpserver.MCPServer``) or 1.x (``FastMCP``).
  ``import mcp`` here is absolute, so it resolves to the SDK, not to this
  ``verinoda.mcp`` package.
"""

import contextlib
import importlib
import itertools
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from collections import OrderedDict
from pathlib import Path
from typing import Annotated, Any, Callable, Literal

from verinoda.paths import configure_index_env

configure_index_env()  # before anything imports verinoda.project_index

from verinoda.paths import ATLAS_DIRNAME, atlas_dir, graph_path, receiver_calls_path  # noqa: E402

TOOL_NAMES: tuple[str, ...] = (
    "project_query",
    "node_inspect",
    "relation_trace",
    "run_when",
    "history_search",
    "map_view",
    "change_review",
    "question_plan_draft",
    "question_plan_check",
    "analyze",
    "plan_audit",
    "lexicon_show",
    "claim_inspect",
    "claim_list",
    "evidence_inspect",
    "claim_verify",
    "claim_challenge",
    "resolve_call",
    "code_check",
    "api_members",
    "runtime_observe",
    "reference_resolve",
    "reference_research",
    "reference_compare",
    "feedback_submit",
    "feedback_process",
    "feedback_resolve",
    "index_update",
    "decision_record",
    "decision_check",
    "dependency_ask",
    "decision_brief",
    "experiment_run",
    "debug_start",
    "debug_attempt",
    "debug_status",
    "debug_strategy",
    "grep_context",
    "read_context",
    "change_probe",
)

MAX_RESPONSE_CHARS = int(os.environ.get("VERINODA_MCP_MAX_CHARS", "12000"))
QUERY_MAX_CHARS = 6000          # same default as `verinoda query --max-chars` (retrieval.question_chars)
QUERY_MEMO_SIZE = 64            # project_query answers kept, re-validated by stat on every hit
EXCERPT_MAX_LINES = 30
EDGE_CAP = 25
RATIONALE_CAP = 5   # rationale comments node_inspect quotes (each at most 200 characters)
LIST_CAP = 50
CODE_CHECK_BUDGET_S = float(os.environ.get("VERINODA_MCP_CHECK_BUDGET_S", "90"))   # code_check holds the server
VIEWS = ("hierarchy", "dependencies", "dataflow", "config", "tests", "history", "impact", "cycles", "outline",
         "dead", "hotspots", "sides", "repo", "dsm", "model", "saved")
DEAD_FIRST_CUT = ("searched.entry_points", "searched.entry_modules")
VERDICTS = ("confirmed", "qualified", "corrected", "unresolved")
RESEARCH_KINDS = ("auto", "official_doc", "standard", "paper", "secondary", "reference_repo")
NETWORK_MODES = ("off", "cache", "on")
QUERY_FORMATS = ("text", "json")
DECISION_ACTIONS = ("list", "record", "import", "guard", "accept", "waive", "supersede", "link", "answer")
# what changes what is enforced, and the user's answers to a brief: the user's own words
DECISION_NEEDS_USER = ("record", "guard", "accept", "waive", "supersede", "link", "answer")
DEBUG_KINDS = ("fix", "probe", "rerun", "differential")
DEBUG_STRATEGIES = ("differential", "bisect", "rerun", "observe")
# analyze: what the agent reads first and what is cut last (the interpretation and the per-sub-question verdicts)
ANALYZE_KEEP = ("analysis_id", "snapshot", "understood_as", "subquestions")
# over the cap, bookkeeping goes before evidence: the critique log and the plan's links first, then the
# passages (from the end: the top-ranked stay; `project_query` gives them in full), claims last
ANALYZE_FIRST_CUT = ("critique", "plan_check.links", "passages")
PLAN_HINT = ("draft and check a plan with `verinoda plan draft` / `verinoda plan check` (MCP, profile full: "
             "question_plan_draft / question_plan_check); the format: `verinoda plan schema`")
# A file modified this close to (or after) the start of a call may have changed while it was read, or
# within the file system's timestamp granularity: what was derived from it is not kept (git's racy-clean rule).
RACY_NS = 2_000_000_000
MCP_BUILD_WAIT = 30.0          # index_update waits this long for another build of the project
# D62 (a), under study: the sentence that asks for analyze before a search by hand. Not in the instructions yet; the
# adoption study appends it verbatim, and it goes into _INSTRUCTIONS_CORE_HEAD byte for byte if it is adopted.
ANALYZE_FIRST = ("For a how, why, what-happens or flow question, call analyze once before searching by hand; a "
                 "question that names one symbol or file can start with Grep.")
_RACY = ("racy",)                # recorded instead of a stat: never equal to one, so it is re-derived


# -- errors ---------------------------------------------------------------------

class ToolFailure(Exception):
    """An expected failure, reported to the agent as a structured error."""

    def __init__(self, error: str, message: str, hint: str, **extra: Any):
        super().__init__(message)
        self.error, self.message, self.hint, self.extra = error, message, hint, extra

    def as_dict(self) -> dict:
        return {"error": self.error, "message": self.message, "hint": self.hint, **self.extra}


def _clamp(value: Any, lo: int | float, hi: int | float, name: str, cast: type = int):
    try:
        v = cast(value)
    except (TypeError, ValueError):
        raise ToolFailure("invalid_argument", f"{name} must be a number, got {value!r}",
                          f"pass {name} as a number between {lo} and {hi}") from None
    return max(lo, min(hi, v))


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ToolFailure("invalid_argument", f"{name} must be a non-empty string",
                          f"pass a non-empty {name}")
    return value.strip()


def _opt_text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _str_list(value: Any, name: str) -> list[str]:
    """None, one string or a list of strings -> the non-empty strings, in order, without duplicates."""
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise ToolFailure("invalid_argument", f"{name} must be a list of strings, got {type(value).__name__}",
                          f"pass {name} as a list of strings")
    return list(dict.fromkeys(str(v).strip() for v in value if str(v).strip()))


def _argv_list(value: Any, name: str) -> list[str]:
    """A command as given: a list of strings, in order, with repeats and empty strings kept (``-p a -p b``
    and ``-k ''`` mean something); None -> []."""
    if value is None:
        return []
    if not isinstance(value, (list, tuple)) or not all(isinstance(v, str) for v in value):
        raise ToolFailure("invalid_argument", f"{name} must be a list of strings (one per argument)",
                          f"pass {name} as a list of strings, e.g. ['python', '-m', 'pytest', '-q']")
    return list(value)


def _choice(value: Any, valid: tuple[str, ...], name: str) -> str:
    if value not in valid:
        raise ToolFailure("invalid_argument", f"unknown {name} {value!r}", "choose one of: " + ", ".join(valid),
                          valid=list(valid))
    return value


def _network(repo: Path, value: Any) -> str:
    """A network mode; None means the project's config (``research.network``, else ``cache``), as in the CLI."""
    if value is None:
        try:
            from verinoda.paths import network_mode
        except ImportError:  # pragma: no cover - older paths module
            return "cache"
        return network_mode(repo)
    return _choice(value, NETWORK_MODES, "network")


def _plan_text(plan_json: Any) -> str | None:
    """The plan's JSON text, or None. Never a file path: the MCP transport carries the JSON itself (D1)."""
    if plan_json is None or (isinstance(plan_json, str) and not plan_json.strip()):
        return None
    if isinstance(plan_json, dict):
        return json.dumps(plan_json, ensure_ascii=False)
    if not isinstance(plan_json, str) or not plan_json.lstrip().startswith("{"):
        raise ToolFailure("invalid_plan", "plan_json must be the plan's JSON text (a JSON object)", PLAN_HINT,
                          problems=[{"at": "/", "code": "schema", "msg": "not a JSON object",
                                     "fix": "pass a drafted plan (`verinoda plan draft`), edited, as JSON text"}])
    return plan_json


# -- response size cap ----------------------------------------------------------

def _jsonable(obj: Any) -> Any:
    """Plain JSON types only (tuples -> lists, Paths -> str)."""
    return json.loads(json.dumps(obj, ensure_ascii=False, default=str))


def _size(obj: Any) -> int:
    return len(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))


def _path_str(path: tuple) -> str:
    out = ""
    for p in path:
        out += f"[{p}]" if isinstance(p, int) else (f".{p}" if out else str(p))
    return out or "(root)"


def _map_like(d: dict) -> bool:
    """A dict used as a mapping (name -> data), not a record with fixed fields.

    Heuristic: at least 4 entries whose values are all dicts, all lists or all
    numbers (e.g. ``env_vars``, ``affected_files``, ``churn``). Records - hops,
    evidence rows, claims - mix value types or hold strings and are never cut
    field by field.
    """
    if len(d) < 4:
        return False
    kinds = set()
    for v in d.values():
        if isinstance(v, dict):
            kinds.add("dict")
        elif isinstance(v, list):
            kinds.add("list")
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            kinds.add("num")
        else:
            return False
    return len(kinds) == 1


# Top-level keys never cut: a view's method/limits statement must survive truncation.
PROTECTED_KEYS = frozenset({"coverage"})


def _scan(node: Any, parent: Any, key: Any, path: tuple, best: dict, protected: frozenset) -> int:
    """Estimate the compact-JSON size of ``node``; remember the biggest cuttables."""
    if len(path) == 1 and path[0] in protected:
        return _size(node)
    if isinstance(node, dict):
        size = 2 + max(0, len(node) - 1)
        for k, v in node.items():
            size += len(json.dumps(str(k), ensure_ascii=False)) + 1 + _scan(v, node, k, path + (k,), best, protected)
        if path and _map_like(node) and size > best["c"][0]:
            best["c"] = (size, parent, key, path, node)
        return size
    if isinstance(node, list):
        size = 2 + max(0, len(node) - 1)
        for i, v in enumerate(node):
            size += _scan(v, node, i, path + (i,), best, protected)
        if len(node) >= 2 and size > best["c"][0]:
            best["c"] = (size, parent, key, path, node)
        return size
    size = len(json.dumps(node, ensure_ascii=False))
    if isinstance(node, str) and len(node) > 240 and size > best["s"][0]:
        best["s"] = (size, parent, key, path, node)
    return size


def _clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 3] + "..."


def _clip_mid(text: str, n: int) -> str:
    """``text`` cut in the middle to ``n`` characters: the head says what, the tail often says why (a
    swallowing handler, where the name was looked for)."""
    if len(text) <= n:
        return text
    head = (n - 5) * 2 // 5
    return f"{text[:head]} ... {text[len(text) - (n - 5 - head):]}"


def check_site_line(s: dict) -> str:
    """One name-check site as one line (docs/DESIGN.md D64): ``VERDICT path:line:col kind expr | why | guard:
    ... | swallowed by ... | optional dependency | elsewhere: qualname (at) | nearest: names | next: step``.
    Unknown sites are labelled by their rank (HIGH, MEDIUM, LOW)."""
    v = s.get("verdict")
    tag = {"absent": "ABSENT", "mismatch": "MISMATCH", "not_installed": "NOT_INSTALLED", "guarded": "GUARDED",
           "exists": "EXISTS"}.get(
        str(v)) or str(s.get("rank") or "unknown").upper()
    parts = [f"{tag} {s.get('at')} {s.get('kind')} {_clip(str(s.get('expr') or ''), 80)}"]
    if v == "unknown":
        if s.get("rank") in ("high", "medium") and s.get("rank_why"):
            parts.append(_clip_mid(str(s["rank_why"]), 140))
        elif s.get("why"):
            parts.append("why: " + _clip_mid(str(s["why"]), 110))
    else:
        detail = s.get("message") or s.get("why") or (None if s.get("guard") else s.get("at_def"))
        if detail:
            parts.append(_clip_mid(str(detail), 150))
    if s.get("guard"):   # what makes a guarded site safe (or not): the reader judges it
        parts.append("guard: " + _clip(str(s["guard"]), 90))
    if s.get("swallowed_by") and "swallow" not in (parts[-1] if len(parts) > 1 else ""):
        parts.append("swallowed by " + _clip(str(s["swallowed_by"]), 60))
    if s.get("optional"):
        parts.append("optional dependency")
    if s.get("elsewhere"):   # the fix, when the name is defined in another module
        parts.append("elsewhere: " + ", ".join(f"{e.get('qualname')} ({e.get('at')})" for e in s["elsewhere"][:2]
                                               if isinstance(e, dict)))
    if s.get("nearest"):   # the fix is among them; the next step is then implied
        parts.append("nearest: " + ", ".join(str(n.get("name")) for n in s["nearest"][:3]))
    elif s.get("next_step") and v != "exists":
        parts.append("next: " + _clip(str(s["next_step"]), 100))
    return " | ".join(parts)


def compact_check_result(res: dict) -> dict:
    """code_check's MCP answer: each site as one line (:func:`check_site_line`), so the response cap keeps
    about five times more sites, in rank order (absent and HIGH unknowns first)."""
    if not isinstance(res.get("sites"), list):
        return res
    out = dict(res)
    out["sites"] = [check_site_line(s) if isinstance(s, dict) else s for s in res["sites"]]
    if isinstance(res.get("files"), list):   # summary.files counts them; the ones not read are listed
        out["files"] = [f for f in res["files"] if isinstance(f, dict) and f.get("error")]
    us = out.get("unknown_summary")
    if isinstance(us, dict) and us.get("low_by_cause"):   # "N cause -> step", one line per cause
        out["unknown_summary"] = {**us, "low_by_cause": [f"{g['sites']} {g['cause']} -> {g['next_step']}"
                                                         for g in us["low_by_cause"][:10]]}
    return out


def cap_response(obj: dict, limit: int = MAX_RESPONSE_CHARS, *, first: tuple[str, ...] = (),
                 keep_tail: tuple[str, ...] = ("history",), keep: tuple[str, ...] = ()) -> dict:
    """Cut ``obj`` until its compact JSON (truncation note included) fits ``limit`` chars.

    Lists named in ``first`` (top-level keys, or ``parent.key`` one level down) are shortened
    before anything else, in that order.
    Then the largest list (or name->data map) anywhere in the tree is cut, or
    the longest string when it dominates. Cuts are proportional to the excess,
    so the result stays close to the limit. Lists under a key in ``keep_tail``
    keep their last items (newest history) instead of the first. Top-level
    keys in ``keep`` are placed first in the response and are cut only when
    nothing else is left to cut. Nothing is re-ordered inside a value or
    invented; ``truncation.cut`` says what was cut (kept/total). Top-level
    ``coverage`` (method and limits of a heuristic view) is never cut.
    """
    obj = _jsonable(obj)
    if keep:
        obj = {**{k: obj[k] for k in keep if k in obj}, **{k: v for k, v in obj.items() if k not in keep}}
    if _size(obj) <= limit or (obj.get("truncated") is True and isinstance(obj.get("truncation"), dict)):
        return obj  # fits, or was cut already (a tool that caps its own response): never cut twice
    notes: dict[str, dict] = {}

    def note() -> dict:
        cut = dict(list(notes.items())[:25])
        if len(notes) > 25:
            cut["..."] = {"more_cut_fields": len(notes) - 25}
        return {"max_chars": limit, "cut": cut,
                "hint": "narrow the request (smaller max_items, explicit targets, one view) "
                        "or run the matching `verinoda ... --json` CLI command for the full result"}

    def excess() -> int:
        # size of obj + ',"truncated":true,"truncation":<note>' minus the limit
        return _size(obj) + 32 + _size(note()) - limit

    def record(path: tuple, total: int, kept: int) -> None:
        k = _path_str(path)
        prev = notes.get(k)
        notes[k] = {"kept": kept, "total": prev["total"] if prev else total}

    def cut_seq(path: tuple, node: list | dict, size: int, over: int) -> None:
        n = len(node)
        keep_n = int(n * (size - over) / size) if size > over else 1
        keep_n = max(1, min(n - 1, keep_n))
        record(path, n, keep_n)
        if isinstance(node, dict):
            for k in list(node)[keep_n:]:
                del node[k]
        elif bool(path) and path[-1] in keep_tail:
            del node[: n - keep_n]
        else:
            del node[keep_n:]

    for key in first:
        path = tuple(key.split(".", 1))
        parent = obj if len(path) == 1 else obj.get(path[0])
        while (isinstance(parent, dict) and isinstance(parent.get(path[-1]), list) and len(parent[path[-1]]) > 1
               and (over := excess()) > 0):
            cut_seq(path, parent[path[-1]], _size(parent[path[-1]]), over)

    # first everything but ``keep``; then, if still too big, the kept keys too (never ``coverage``)
    for protected in (PROTECTED_KEYS | frozenset(keep), PROTECTED_KEYS) if keep else (PROTECTED_KEYS,):
        for _ in range(400):
            over = excess()
            if over <= 0:
                break
            best: dict = {"c": (0, None, None, (), None), "s": (0, None, None, (), None)}
            _scan(obj, None, None, (), best, protected)
            c_size, _, _, c_path, c_node = best["c"]
            s_size, s_parent, s_key, s_path, s_node = best["s"]
            if s_node is not None and (c_node is None or s_size * 2 >= c_size):
                new_len = max(200, min(len(s_node) - 1, len(s_node) - over - 16))
                s_parent[s_key] = s_node[:new_len] + "...[truncated]"
                record(s_path, len(s_node), new_len)
            elif c_node is not None:
                cut_seq(c_path, c_node, c_size, over)
            else:
                break  # nothing left that can be cut at this level of protection
    unmet = excess() > 0
    obj["truncated"] = True
    obj["truncation"] = note()
    if unmet:  # only protected/indivisible content is left; say so instead of cutting it
        obj["truncation"]["over_limit"] = True
    return obj


# -- graph helpers ----------------------------------------------------------------

def _edge_at(d: dict) -> str | None:
    loc = d.get("source_location") or ""
    if d.get("source_file") and loc.startswith("L"):
        return f"{d['source_file']}:{loc[1:]}"
    return d.get("source_file")


def _node_at(g, nid: str) -> str | None:
    f, ln = g.file(nid), g.line(nid)
    return f"{f}:{ln}" if f and ln else f


def _node_kind(g, nid: str) -> str:
    d = g.G.nodes[nid]
    if g.is_file_node(nid):
        return "file"
    if d.get("_callable_class"):
        return "class"
    if d.get("_callable"):
        return "callable"
    if not d.get("source_file"):
        return "external"
    return str(d.get("file_type") or "other")


def _resolution(g, name: str, nid: str) -> str | None:
    """How ``name`` matched ``nid`` exactly, or None for a scored (heuristic) match."""
    if name in g.G:
        return "node id"
    label = g.label(nid).strip(".()")
    if "::" in name:
        f, sym = name.split("::", 1)
        if g.file(nid) == f and label == sym.strip(".()"):
            return "file::symbol"
    if g.file(nid) == name and g.is_file_node(nid):
        return "file path"
    if label == name.strip().strip(".()"):
        return "label"
    if "." in name and label == name.rpartition(".")[2].strip(".()"):
        return "Class.method"
    return None


def _deps_stats(deps: tuple):
    """Every stat inside a :meth:`AtlasTools._query_deps` tuple (graph key first)."""
    graph_key, *rest, files = deps
    yield graph_key[1:] if graph_key else None
    yield from rest
    for _, st in files:
        yield st


def _spawn(argv: list[str], **kw) -> subprocess.Popen:
    """Start a child process (a seam for tests: the background ``verinoda update``)."""
    return subprocess.Popen(argv, **kw)




def _updater_argv(repo: Path) -> list[str]:
    """``verinoda update --repo <repo>`` in an isolated child (:func:`verinoda.buildlock.updater_argv`)."""
    from verinoda import buildlock

    return buildlock.updater_argv(repo)


def _build_running(repo: Path) -> bool:
    """Is an index build of ``repo`` running in another process (:mod:`verinoda.buildlock`)?"""
    from verinoda import buildlock

    return buildlock.is_locked(repo)


def _file_stat(p: Path) -> tuple[int, int, int] | None:
    try:
        st = p.stat()
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size, getattr(st, "st_ino", 0)


# -- the tools --------------------------------------------------------------------

class AtlasTools:
    """Tool implementations over one repository (no MCP SDK needed)."""

    def __init__(self, repo: Path | str, *, max_chars: int = MAX_RESPONSE_CHARS):
        self.repo = Path(repo).resolve()
        self.max_chars = max_chars
        self._lock = threading.RLock()
        self._graph_cache: tuple[tuple, Any] | None = None
        self._span_stat: dict[str, tuple | None] = {}   # file -> stat when its cached spans were noted
        self._span_noted = 0                              # entries of g._spans already noted
        self._lex_cache: tuple[Any, Any] | None = None
        self._query_memo: OrderedDict[tuple, tuple[tuple, Any]] = OrderedDict()
        # the passages project_query printed in this session: file -> (its stat then, [(a, b), ...])
        self._session_seen: dict[str, tuple[tuple | None, list[tuple[int, int]]]] = {}
        self._background: subprocess.Popen | None = None  # a `verinoda update` started after a slow analyze
        self.racy_ns = RACY_NS
        self._call_started_ns = time.time_ns()
        self.cache_stats = {"graph_loads": 0, "span_files_invalidated": 0, "lexicon_loads": 0,
                            "query_memo_hits": 0}

    # -- plumbing ---------------------------------------------------------------
    def _check(self, need: str) -> None:
        if need == "none":
            return
        if not atlas_dir(self.repo).is_dir():
            raise ToolFailure(
                "not_initialised",
                f"{self.repo} has no {ATLAS_DIRNAME}/ directory: it has not been scanned by Verinoda",
                "call index_update (it scans this folder), then this tool again",
                repo=str(self.repo),
            )
        if need == "graph" and not graph_path(self.repo).exists():
            raise ToolFailure(
                "no_index",
                f"{self.repo} has {ATLAS_DIRNAME}/ but no index ({graph_path(self.repo)} is missing)",
                f"call index_update, or run `verinoda scan {self.repo}` in a terminal",
                repo=str(self.repo),
            )

    def _run(self, tool: str, fn: Callable[[], dict], *, need: str = "atlas",
             first: tuple[str, ...] = (), keep: tuple[str, ...] = ()) -> dict:
        with self._lock:
            self._call_started_ns = time.time_ns()
            try:
                # Stray prints from core/vendored code must never reach the
                # JSON-RPC channel on stdout.
                with contextlib.redirect_stdout(sys.stderr):
                    self._check(need)
                    res = fn()
                    if not isinstance(res, dict):
                        res = {"result": res}
                    return cap_response(res, self.max_chars, first=first, keep=keep)
            except ToolFailure as f:
                return cap_response({**f.as_dict(), "tool": tool}, self.max_chars)
            except KeyError as exc:
                msg = str(exc.args[0] if exc.args else exc)
                if not msg.startswith("no "):  # a missing dict key inside the core, not a missing record
                    traceback.print_exc(file=sys.stderr)
                    return {"error": "internal_error", "message": f"KeyError: {msg}"[:600], "tool": tool,
                            "hint": _error_hint(exc, self.repo)}
                return {"error": "not_found", "message": msg[:600], "tool": tool,
                        "hint": "check the id; claim ids come from analyze/claim_list, evidence ids from "
                                "claim_inspect, analysis ids from analyze, feedback ids from feedback_submit, "
                                "resolution ids from reference_resolve"}
            except FileNotFoundError as exc:
                missing_index = not graph_path(self.repo).exists()
                return {"error": "no_index" if missing_index else "file_not_found", "message": str(exc)[:600],
                        "tool": tool,
                        "hint": (f"run `verinoda scan {self.repo}` or call index_update" if missing_index
                                 else "the file may have been moved or deleted; call index_update")}
            except Exception as exc:  # never let a tool take the server down
                traceback.print_exc(file=sys.stderr)
                return {"error": _error_code(exc), "message": f"{type(exc).__name__}: {exc}"[:600],
                        "tool": tool, "hint": _error_hint(exc, self.repo)}
            finally:
                self._note_spans()

    @contextlib.contextmanager
    def _store(self):
        from verinoda.store import open_store

        st = open_store(self.repo)
        try:
            yield st
        finally:
            st.close()

    # -- kept state (D21) ---------------------------------------------------------
    def _graph(self):
        """The loaded graph, kept until graph.json or the receiver-call sidecar changes (an update
        can keep graph.json and still change the receiver and Java call edges ``load`` adds from
        the sidecar); spans of edited files are re-derived."""
        from verinoda import buildlock, index

        gp = graph_path(self.repo)
        key = (str(gp), *(_file_stat(gp) or (None,)), _file_stat(receiver_calls_path(self.repo)))
        if key[1] is None:
            raise FileNotFoundError(f"no graph at {gp}; run `verinoda scan {self.repo}` first")
        if self._graph_cache is not None and self._graph_cache[0] != key and buildlock.is_locked(self.repo):
            # another process is rewriting the index: keep answering from the graph already loaded
            self._sync_spans()
            return self._graph_cache[1]
        if self._graph_cache is None or self._graph_cache[0] != key:
            self._graph_cache = (key, index.load(self.repo))
            self._span_stat, self._span_noted = {}, 0
            self._query_memo.clear()
            self.cache_stats["graph_loads"] += 1
        else:
            self._sync_spans()
        return self._graph_cache[1]

    def _sync_spans(self) -> None:
        """Drop the kept spans / line owners of files whose stat changed since they were derived."""
        if self._graph_cache is None:
            return
        g = self._graph_cache[1]
        changed = {f for f, st in self._span_stat.items() if _file_stat(g.root / f) != st}
        if not changed:
            return
        for nid in [n for n in g._spans if g.file(n) in changed]:
            g._spans.pop(nid, None)
            g._span_how.pop(nid, None)
        for f in changed:
            g._owners.pop(f, None)
            del self._span_stat[f]
        self._span_noted = len(g._spans)  # every entry left was noted before
        self.cache_stats["span_files_invalidated"] += len(changed)

    def _note_spans(self) -> None:
        """Record the stat of each file whose spans or owners were derived during this call."""
        if self._graph_cache is None:
            return
        g = self._graph_cache[1]
        known = self._span_stat
        try:
            new = list(itertools.islice(g._spans, self._span_noted, None))
            files = {g.file(n) for n in new} | set(g._owners)
        except Exception:  # noqa: BLE001 - a graph without these caches: nothing to track
            return
        for f in files:
            if f and f not in known:
                st = _file_stat(g.root / f)
                known[f] = _RACY if self._racy(st) else st
        self._span_noted = len(g._spans)

    def _racy(self, st) -> bool:
        """Was the file (stat ``st``) modified too close to the start of this call to trust what was read?"""
        return st is not None and st != _RACY and st[0] >= self._call_started_ns - self.racy_ns

    def _lexicon(self):
        """The repository lexicon (``lexicon.load``), kept until lexicon.json changes; None when absent."""
        from verinoda import lexicon as lexmod

        p = lexmod.lexicon_path(self.repo)
        key = _file_stat(p)
        if self._lex_cache is None or self._lex_cache[0] != key:
            self._lex_cache = (key, lexmod.load(self.repo) if key else None)
            self.cache_stats["lexicon_loads"] += 1
        return self._lex_cache[1]

    def _stale_graph(self, st):
        """The kept (or loaded) graph for an analysis that will not refresh the index first: None when a
        refresh is expected (analyze then loads the new graph itself)."""
        from verinoda import analysis
        from verinoda.snapshot import current_state

        snap = st.latest_snapshot()
        if snap is None or not graph_path(self.repo).exists():
            return None
        try:
            state = current_state(self.repo, store=st)
            est, changed, rebuild = analysis._refresh_estimate(self.repo, st, snap, state, g=self._graph())
        except Exception:  # noqa: BLE001 - let analyze decide
            return None
        slow = analysis.slow_refresh(self.repo, snap, est, changed, rebuild)
        return self._graph() if slow or _build_running(self.repo) else None

    def _update_in_background(self) -> str:
        """Start ``verinoda update`` for this project in a separate process (one at a time; the build
        lock keeps it from colliding with any other build). A process, not a thread: index builds
        redirect stdout, which here is the protocol channel."""
        from verinoda.paths import index_dir

        bg = self._background
        if bg is not None and bg.poll() is None:
            return f"running (pid {bg.pid})"
        if _build_running(self.repo):
            return "another index build is running"
        log = index_dir(self.repo) / "background_update.log"
        try:
            log.parent.mkdir(parents=True, exist_ok=True)
            with open(log, "ab") as out:
                self._background = _spawn(
                    _updater_argv(self.repo),
                    # a working directory outside the project too (the isolated child ignores it anyway)
                    stdin=subprocess.DEVNULL, stdout=out, stderr=out, cwd=tempfile.gettempdir(),
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as exc:
            return f"could not start ({type(exc).__name__}): run index_update"
        return f"started (pid {self._background.pid}); the next question sees the new index"

    def _freshness(self) -> dict:
        """Files changed since the index (:func:`verinoda.freshness.check`); never fails a tool."""
        from verinoda import freshness

        try:
            return freshness.check(self.repo)
        except Exception as exc:  # noqa: BLE001 - a failed check is said, never taken for "fresh"
            return {"checked": False, "why": f"{type(exc).__name__}: {exc}"[:200], "count": 0, "files": []}

    def _graph_for_analysis(self, st):
        """The kept graph when it describes the current working tree, else None.

        ``analyze`` re-indexes first when the tree changed since the last
        snapshot and must then load the new graph itself, so the kept one is
        only handed over when no refresh will happen.
        """
        from verinoda.snapshot import current_state

        snap = st.latest_snapshot()
        if snap is None:
            return None
        try:
            state = current_state(self.repo, store=st)
        except Exception:  # noqa: BLE001 - let analyze decide and report
            return None
        return self._graph() if state.get("tree_hash") == snap.get("tree_hash") else None

    @staticmethod
    def _optional(module: str):
        try:
            return importlib.import_module(module)
        except Exception as exc:
            raise ToolFailure(
                "unavailable",
                f"{module} could not be imported: {type(exc).__name__}: {exc}"[:500],
                "this Verinoda installation lacks that feature or it is broken; "
                "run `verinoda doctor` and retry after fixing/upgrading",
            ) from None

    # -- retrieval & graph --------------------------------------------------------
    def _query_deps(self, g, files: tuple[str, ...]) -> tuple:
        """Everything a retrieval answer is computed from, by stat: the graph, the search index (with its
        write-ahead log), the lexicon (Turkish expansions) and the files the answer read lines from."""
        from verinoda import lexicon as lexmod
        from verinoda import search_index

        db = search_index.db_path_for(g)
        return (self._graph_cache[0] if self._graph_cache else None, _file_stat(db),
                _file_stat(Path(str(db) + "-wal")), _file_stat(lexmod.lexicon_path(self.repo)),
                tuple((f, _file_stat(g.root / f)) for f in files))

    def project_query(self, question: str, max_items: int = 8, format: str = "text") -> dict:
        def go():
            from verinoda import query_filters, retrieval

            q = _text(question, "question")
            n = _clamp(max_items, 1, 25, "max_items")
            fmt = _choice(format, QUERY_FORMATS, "format")
            g = self._graph()
            fresh = self._freshness()
            budget = retrieval.question_chars(q, self.repo)
            # the budget follows the config / environment, and stale files change the answer's notes: both key the memo
            key = (q, n, fmt, budget, tuple(fresh.get("files") or ()))
            hit = self._query_memo.get(key)
            res = None
            if hit is not None and hit[0] == self._query_deps(g, tuple(f for f, _ in hit[0][-1])):
                self._query_memo.move_to_end(key)
                self.cache_stats["query_memo_hits"] += 1
                res = hit[1]
            if res is None:
                try:
                    res = retrieval.retrieve(g, q, retrieval.Budget(max_items=n, max_chars=budget), filters=True)
                except query_filters.FilterError as exc:
                    raise ToolFailure("invalid_argument", str(exc),
                                      "filters: path:GLOB lang:NAME symbol:NAME is:vendored|generated|minified|test "
                                      "/regex/, AND OR NOT, -filter, parentheses") from None
                retrieval.attach_freshness(res, g, fresh)
                rd = getattr(res, "render", None)
                if rd is not None:  # the same answer is recomputed only when one of its inputs changed on disk
                    files = tuple(sorted({h.file for h in rd.ranking.hits} | {i["file"] for i in res.get("items", [])}))
                    deps = self._query_deps(g, files)
                    if not any(self._racy(st) for st in _deps_stats(deps)):
                        self._query_memo[key] = (deps, res)
                        while len(self._query_memo) > QUERY_MEMO_SIZE:
                            self._query_memo.popitem(last=False)
            from verinoda import facts

            facts.attach_leads(res, self.repo, q)  # read on every call: facts change without a file changing
            if fmt == "json":
                return _jsonable(res)
            # the escaped JSON string must still fit the response cap
            chars = min(budget, max(800, int((self.max_chars - 200) * 0.85)))
            dedup = os.environ.get("VERINODA_QUERY_DEDUP", "1") != "0"
            returned: list[tuple[str, int, int]] = []
            text = retrieval.render_text(res, budget_chars=chars, seen=self._seen_now() if dedup else None,
                                         returned=returned)
            if dedup:
                self._note_returned(returned)
            return {"format": "text", "question": q, "text": text}
        return self._run("project_query", go, need="graph")

    def _stat_key(self, rel: str) -> tuple | None:
        try:
            st = (self.repo / rel).stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def _seen_now(self) -> dict[str, list[tuple[int, int]]]:
        """The spans printed earlier in this session of files that did not change since (a changed file's passages
        are new again)."""
        out = {}
        for rel, (stamp, spans) in list(self._session_seen.items()):
            if stamp is not None and stamp == self._stat_key(rel):
                out[rel] = spans
            else:
                del self._session_seen[rel]
        return out

    def _note_returned(self, returned: list[tuple[str, int, int]]) -> None:
        """Remember the windows an answer printed, with each file's stat; a file modified within the racy window
        is not remembered (a same-size edit in the same clock tick would keep its stamp)."""
        for rel, a, b in returned:
            stamp = self._stat_key(rel)
            if stamp is None or self._racy(stamp):
                continue
            old_stamp, spans = self._session_seen.get(rel, (stamp, []))
            if old_stamp != stamp:
                spans = []
            if (a, b) not in spans:
                spans.append((a, b))
            self._session_seen[rel] = (stamp, spans)

    def read_context(self, file_path: str) -> dict:
        """What the project says about one file (decision records, notes, glob-scoped rules: :mod:`verinoda.scoped`)
        as a Claude Code PostToolUse hook output; ``{}`` when nothing does, or anything is wrong (a Read is never
        held up)."""
        from verinoda import scoped

        # no server lock: it reads files only (its own cache has its own lock), so a Read never waits for an analyze
        try:
            text = scoped.text(scoped.for_file(self.repo, str(file_path or ""))) if file_path else ""
        except Exception:  # noqa: BLE001 - a hook never breaks the agent's Read
            return {}
        if not text:
            return {}
        return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": text}}

    def grep_context(self, pattern: str = "", path: str | None = None, command: str | None = None) -> dict:
        """What the static graph says about the symbols a search names, as a Claude Code PostToolUse hook output
        (D62): the Grep tool's ``pattern``, or the patterns of ``grep`` / ``rg`` / ``git grep`` ... in a shell
        ``command`` (:func:`verinoda.tool_hook.shell_patterns`). Where each symbol found by its exact name is
        defined, who calls it and what it calls, in one short line. Nothing found, or anything wrong: ``{}`` (the
        hook adds nothing; a search is never held up). ``path`` is the Grep's own path argument, unused for now."""
        from verinoda import tool_hook

        try:   # every shell command reaches this: one with no search returns before waiting for the lock
            pats = ([pattern] if pattern else []) + (tool_hook.shell_patterns(command) if command else [])
            if not tool_hook.words_of(pats) or not graph_path(self.repo).exists():
                return {}
        except Exception:  # noqa: BLE001 - a hook never breaks the agent's Grep
            return {}
        with self._lock:
            try:
                with contextlib.redirect_stdout(sys.stderr):
                    return tool_hook.shape("claude", tool_hook.grep_text(self._graph(), pats))
            except Exception:  # noqa: BLE001 - a hook never breaks the agent's Grep
                return {}

    def node_inspect(self, name: str) -> dict:
        def go():
            from verinoda import freshness, naming

            g = self._graph()
            q = _text(name, "name")
            fresh = self._freshness()
            r = naming.resolve(g, q, stale=fresh.get("files") or ())
            if r.status == naming.AMBIGUOUS:
                raise ToolFailure("ambiguous", r.note or f"{q!r} names several symbols",
                                  "pass one of the candidates' ids, or 'path/file.py::symbol'",
                                  candidates=r.rows(g), **freshness.summary(fresh))
            if r.node is None:
                hint = ("call index_update, then node_inspect again" if r.status == naming.NOT_INDEXED else
                        "call project_query to find candidates, then pass a node id, "
                        "a label, 'path/file.py::symbol' or 'Class.method'")
                raise ToolFailure(r.status if r.status in (naming.NOT_INDEXED, naming.NOT_A_SYMBOL) else "not_found",
                                  r.note or f"no graph node matches {q!r}", hint,
                                  **({"candidates": r.rows(g)} if r.candidates else {}), **freshness.summary(fresh))
            nid = r.node
            how = _resolution(g, q, nid) if r.exact else None
            if r.exact and how is None:
                how = "exact name"
            label = g.label(nid)
            node = {"id": nid, "label": label, "kind": _node_kind(g, nid), "file": g.file(nid),
                    "line": g.line(nid), "file_type": g.G.nodes[nid].get("file_type"),
                    "resolution": how or "scored label match (heuristic) - check candidates"}
            out: dict = {"query": q, "node": node, **freshness.summary(fresh)}
            if r.note:
                out["resolution_note"] = r.note
            if how is None:
                out["candidates"] = r.rows(g, 5)
            same = sorted(n for n in g.G.nodes if n != nid and g.label(n).strip(".()") == label.strip(".()"))
            if same:
                out["same_label"] = [{"id": n, "at": _node_at(g, n)} for n in same[:5]]
            src = g.source(nid, max_lines=EXCERPT_MAX_LINES)
            if src:
                span = g.span(nid)
                from verinoda.retrieval import numbered_lines

                out["excerpt"] = {"lines": [src[0], src[1]], "span": list(span) if span else None,
                                  "truncated": bool(span and span[1] > src[1]),
                                  "text": numbered_lines(src[2], src[0])}  # the line a claim needs, by number
            else:
                out["excerpt"] = None
                if not g.file(nid):
                    out["note_location"] = "node has no source file in this repository (external or unresolved)"

            def rows(edges, other_key: str) -> tuple[list[dict], dict]:
                rs, rel_count = [], {}
                for other, d in edges:
                    rel = d.get("relation")
                    rel_count[rel] = rel_count.get(rel, 0) + 1
                    r = {other_key: g.label(other), f"{other_key}_id": other, "relation": rel,
                         "confidence": d.get("confidence"), "at": _edge_at(d)}
                    if str(d.get("_origin", "")).startswith("verinoda"):
                        r["derived_by"] = d["_origin"]
                    if d.get("context") and d.get("relation") in ("registers", "injects", "accesses"):
                        r["context"] = str(d["context"])[:160]
                    rs.append(r)
                rs.sort(key=lambda r: (str(r["relation"]), r["at"] or "", r[other_key]))
                return rs, rel_count

            outs, out_rel = rows(g.out_edges(nid), "to")
            ins, in_rel = rows(g.in_edges(nid), "from")
            span = g.span(nid)
            if span and g.file(nid):  # the backlog items its comments cite (docs/DESIGN.md D50)
                from verinoda import backlog

                cited = backlog.items_for(g.root, g.file(nid), span[0], span[1], context=2)[:5]
                if cited:
                    out["backlog"] = [{"id": x["item"].id, "title": x["item"].title[:120], "at": x["item"].at,
                                       "cited_at": f"{g.file(nid)}:{x['line']}", **({"via": x["via"]} if x["via"]
                                                                                   else {})} for x in cited]
            if g.file(nid):  # the rationale comments attached to it, quoted (rationale.py)
                from verinoda import rationale

                if not g.is_file_node(nid) and g.file(nid) in (fresh.get("files") or ()):  # line is the index's
                    why, out["rationale_note"] = [], rationale.STALE_NOTE
                else:
                    why = rationale.for_node(g, nid)
                if why:
                    out["rationale"] = [{k: r[k] for k in ("at", "tag", "attach", "status", "attach_status")}
                                        | {"text": rationale.clip(r["text"], 200)} for r in why[:RATIONALE_CAP]]
                    if len(why) > RATIONALE_CAP:
                        out["rationale_total"] = len(why)
                    out["rationale_note"] = rationale.NOTE
            out.update({
                "out_edges": outs[:EDGE_CAP], "out_total": len(outs), "out_relations": out_rel,
                "in_edges": ins[:EDGE_CAP], "in_total": len(ins), "in_relations": in_rel,
                "note": "edges are extractor output (EXTRACTED) or resolver guesses (INFERRED, derived_by); "
                        "they are not verified claims - use analyze / claim tools for verified statements",
            })
            return out
        return self._run("node_inspect", go, need="graph")

    def relation_trace(self, source: str, target: str, mode: str = "flow") -> dict:
        def go():
            from verinoda import retrieval

            s, t = _text(source, "source"), _text(target, "target")
            if mode not in ("flow", "any"):
                raise ToolFailure("invalid_argument", f"mode must be 'flow' or 'any', got {mode!r}",
                                  "use mode='flow' for call paths, mode='any' for structural relations")
            from verinoda import freshness

            # the core result is returned whole: hints (unresolved endpoints), reachability and note (mode='any')
            fresh = self._freshness()
            res = retrieval.trace(self._graph(), s, t, mode=mode, stale=fresh.get("files") or ())
            res.update(freshness.summary(fresh))
            st = res.get("status")
            if st == "unresolved":
                res.setdefault("next_step", "an endpoint did not resolve: see 'hints'/'candidates', or call "
                                            "node_inspect / project_query and pass 'path/file.py::symbol'")
            elif st and st.startswith("ambiguous"):
                res.setdefault("next_step", "a name resolved to several nodes (or both to one): disambiguate with "
                                            "'path/file.py::symbol'")
            elif st == "no directed path":
                res.setdefault("next_step", ("retry with mode='any' for structural relations; " if mode == "flow"
                                             else "")
                               + "no path in the static graph does not prove there is none at runtime "
                                 "(dynamic dispatch, callbacks and DI are not resolved; `verinoda observe` records "
                                 "what the tests actually call)")
            return res
        return self._run("relation_trace", go, need="graph")

    def run_when(self, symbol: str, depth: int = 6) -> dict:
        def go():
            from verinoda import freshness, when

            fresh = self._freshness()
            res = when.run(self._graph(), _text(symbol, "symbol"), stale=fresh.get("files") or (),
                           max_depth=max(1, min(int(depth), 10)))
            res.update(freshness.summary(fresh))
            if res["status"] != "found":
                res.setdefault("next_step", "pass 'path/File.java::method' or 'Class.method' (see 'candidates')")
            return res
        return self._run("run_when", go, need="graph")

    def history_search(self, text: str | None = None, regex: bool = False, message: str | None = None,
                       author: str | None = None, path: str | None = None, since: str | None = None,
                       until: str | None = None, diff: str | None = None, base: str | None = None,
                       head: str | None = None, limit: int = 20, symbol: str | None = None) -> dict:
        def go():
            from verinoda import history

            # four modes; a parameter of another mode is an error, not silently dropped
            given = {k for k, v in (("text", text), ("message", message), ("author", author), ("since", since),
                                    ("until", until), ("diff", diff), ("base", base), ("head", head),
                                    ("symbol", symbol), ("path", path))
                     if _opt_text(v)} | ({"regex"} if regex else set())
            mode, own = (("symbol", {"symbol"}) if "symbol" in given else
                         ("base", {"base", "head", "path"}) if "base" in given else
                         ("text", {"text", "regex", "path"}) if "text" in given else
                         ("commit search", {"message", "author", "since", "until", "diff", "path"}))
            if given - own:
                raise ValueError(f"{', '.join(sorted(given - own))}: not used with {mode} (history_search has "
                                 "four modes: symbol, text (+regex), base (+head), or the commit filters; path goes "
                                 "with the last three)")
            if isinstance(symbol, str) and symbol and not symbol.strip():
                raise ValueError("symbol is empty: pass a name or path:A-B")
            if _opt_text(symbol):
                t = _opt_text(symbol)
                g = stale = None
                if not history._SPAN.match(t):
                    g, stale = self._graph(), self._freshness().get("files") or ()
                return history.symbol_history(self.repo, g, t, stale=stale or (), limit=_clamp(limit, 1, 100, "limit"))
            if _opt_text(base):
                return history.compare(self.repo, base, _opt_text(head) or "HEAD", path=_opt_text(path))
            if _opt_text(text):
                res = history.text_history(self.repo, text, regex=bool(regex), path=_opt_text(path))
            else:
                res = history.commits(self.repo, message=_opt_text(message), author=_opt_text(author),
                                      path=_opt_text(path), since=_opt_text(since), until=_opt_text(until),
                                      diff=_opt_text(diff), limit=_clamp(limit, 1, 100, "limit"))
            if res["status"] == "not_found":
                res.setdefault("next_step", "fewer filters, no path, or a shorter text; regex=true matches "
                                            "changed lines")
            return res
        return self._run("history_search", go, keep=("status", "claims", "unknowns", "appeared", "disappeared"))

    def map_view(self, view: str, targets: list[str] | None = None) -> dict:
        def go():
            from verinoda import architecture_map as am

            from verinoda import freshness

            if view not in VIEWS:
                raise ToolFailure("invalid_argument", f"unknown view {view!r}",
                                  "choose one of: " + ", ".join(VIEWS), valid=list(VIEWS))
            g = self._graph()
            fresh = self._freshness()
            tg = [str(t).strip() for t in ([targets] if isinstance(targets, str) else targets or []) if str(t).strip()]
            if view == "impact":
                source = "argument"
                if not tg:
                    tg = am.changed_files_from_git(self.repo)
                    source = "git working-tree changes (HEAD + untracked)"
                res = am.impact(g, tg, stale=fresh.get("files") or ())
                res["targets_source"] = source
                if not tg:
                    res["note"] = "no targets given and git reports no changed files: nothing to analyse"
                elif res.get("unresolved") and source == "argument":
                    res["next_step"] = ("a target did not name one symbol exactly (see resolution): pass it as "
                                        "'path/file.py::Name' or a node id; nothing was assumed for it")
                res.update(freshness.summary(fresh))
                return res
            if view == "repo":   # the files in play: the targets, else the working-tree changes
                focus = tg or am.changed_files_from_git(self.repo)
                # the budget is lowered until the map fits the response cap, so that no file is cut from it
                # afterwards and its counts describe what is sent
                budget, room = am.REPO_MAP_TOKENS, self.max_chars * 3 // 4
                res = am.repo_map(g, focus, max_tokens=budget)
                for _ in range(6):
                    size = _size(res)
                    if size <= room or budget <= 16:
                        break
                    budget = max(16, min(budget - 1, budget * room * 9 // (size * 10)))
                    res = am.repo_map(g, focus, max_tokens=budget)
                if budget < am.REPO_MAP_TOKENS:
                    res["budget_note"] = f"max_tokens lowered from {am.REPO_MAP_TOKENS} to fit the response cap"
                res["focus_source"] = "argument" if tg else "git working-tree changes (HEAD + untracked)"
                if tg and res.get("focus_unresolved"):
                    res["next_step"] = ("a target is not a file of the graph (see focus_unresolved): pass a "
                                        "repository-relative path; the map was ranked without it")
                res.update(freshness.summary(fresh))
                return res
            if view == "outline":  # the wiki page tree; the pages named in targets with their Mermaid diagrams
                from verinoda import diagrams

                # with targets only those pages: the whole tree would crowd their diagrams out of the budget
                res = diagrams.outline(g, self.repo, pages=tg or None, diagrams=bool(tg), only=bool(tg),
                                       stale=fresh.get("files") or ())
                if not tg:
                    res["next_step"] = "targets=[page id] for a page's Mermaid diagrams and their evidence"
                res.update(freshness.summary(fresh))
                return res
            if view in ("dsm", "model"):
                from verinoda import dsm

                if len(tg) > 1 or (view == "dsm" and tg and tg[0] not in ("tag", "folder")):
                    raise ToolFailure("invalid_argument", f"the {view} view takes one target at most",
                                      "dsm: targets=['tag'] or ['folder']; model: targets=['path/to/workspace.dsl']")
                res = (dsm.dsm(g, by=tg[0] if tg else "folder") if view == "dsm"
                       else dsm.model_check(g, model=tg[0] if tg else None))
                res.update(freshness.summary(fresh))
                return res
            res = am.VIEWS[view](g)
            if tg:
                res["note"] = f"targets are only used by the impact view; ignored for {view}"
            res.update(freshness.summary(fresh))
            return res

        def saved():  # a map kept with `verinoda map save NAME`: its cited files checked again, or the list
            from verinoda import named_maps

            tg = _str_list(targets, "targets")
            if len(tg) > 1:
                raise ToolFailure("invalid_argument", "the saved view reads one map: targets=[name]",
                                  "targets=[] lists the saved maps")
            if not tg:
                return named_maps.listing(self.repo)
            res = named_maps.read(self.repo, tg[0])
            if res["status"] == "invalid_name":
                raise ToolFailure("invalid_argument", res["message"], "targets=[] lists the saved maps")
            return res
        if view == "saved":  # the status, as_of and changed files are never cut before the saved result
            return self._run("map_view", saved, keep=("status", "name", "as_of", "changed_files", "changed_count",
                                                      "claims", "next_step"))
        if view != "dead":
            return self._run("map_view", go, need="graph")
        # the dead view's claims are the answer: the lists of what was searched give way first
        res = self._run("map_view", go, need="graph", first=DEAD_FIRST_CUT)
        cut = ((res.get("truncation") or {}).get("cut") or {}).get("claims")
        if cut:
            res["claims_not_shown"] = res.get("claims_not_shown", 0) + cut["total"] - cut["kept"]
            res["truncated"] = True
        return res

    # -- question understanding -----------------------------------------------------
    def change_review(self, base: str | None = None, staged: bool = False, targets: list[str] | None = None,
                      change: str | None = None, concerns: list[str] | None = None, run_tests: bool = False,
                      observe: bool = False, max_chars: int = 6000, findings: str = "introduced", since_last: bool = False) -> dict:
        def go():
            from verinoda import review as rv

            fs = _choice(findings, rv.FINDINGS_SHOWN, "findings")
            b = _opt_text(base)
            tg = _str_list(targets, "targets")
            ch = _choice(change, rv.PLANNED_KINDS, "change") if change is not None else None
            cs = _str_list(concerns, "concerns") or None
            for c in cs or []:
                _choice(c, rv.CONCERNS, "concerns")
            if b and staged:
                raise ToolFailure("invalid_argument", "give base or staged, not both", "staged compares the index "
                                                                                     "with HEAD")
            if ch and not tg:
                raise ToolFailure("invalid_argument", "change needs targets", "targets: ['path/file.py::Qual.name']")
            if tg and (b or staged):
                raise ToolFailure("invalid_argument", "targets (a planned change) take no base or staged",
                                  "review the planned change on the current code, or drop targets")
            with self._store() as st:
                try:
                    res = rv.review(self.repo, store=st, graph=self._graph(), base=b, staged=bool(staged),
                                    targets=tg or None, change=ch or ("body" if tg else None), concerns=cs,
                                    run_tests=bool(run_tests), observe=bool(observe),
                                    max_chars=_clamp(max_chars, 500, 50_000, "max_chars"), findings=fs,
                                    since_last=bool(since_last))
                except ValueError as exc:
                    raise ToolFailure("invalid_argument", str(exc)[:600], "base is a git revision such as HEAD~1; "
                                      "targets are 'path/file.py' or 'path/file.py::Qual.name'") from None
            res.pop("files", None)
            ms = res.get("made_stale")
            if isinstance(ms, dict) and not any(ms.get(k) for k in ("claims", "notes", "decisions")):
                # nothing made stale: the counts say what was compared; the limits stay in `review --json`
                res["made_stale"] = {k: ms[k] for k in ("counts", "checked") if k in ms}
            if isinstance(res.get("reviewers"), dict):
                from verinoda import reviewers

                res["reviewers"] = reviewers.compact(res["reviewers"])
            if isinstance(res.get("affected"), dict):
                from verinoda import affected as aff

                res["affected"] = aff.compact(res["affected"])
            if isinstance(res.get("risk"), dict):
                from verinoda import risk

                res["risk"] = risk.compact(res["risk"])
            aff = (res.get("tests") or {}).pop("affected", None) if isinstance(res.get("tests"), dict) else None
            if aff:
                from verinoda import testmap

                res["affected_tests"] = testmap.compact(aff)
            return res
        return self._run("change_review", go, need="graph",
                         # the findings come before the lists that lead to them: what to read first, the tests,
                         # what ran (concerns_checked) and what went stale are cut before any concern is
                         keep=("summary", "exit", "counts", "affected_tests", "risk", "concerns", "unknown", "changes", "api_changes",
                               "decisions", "differential", "since_last"),
                         first=("dependents", "binding_readers", "skipped", "read_first"))

    def question_plan_draft(self, question: str) -> dict:
        def go():
            from verinoda import question_plan as qp

            q = _text(question, "question")
            plan = qp.draft(q, self._graph(), self._lexicon())
            return {"plan": plan,
                    "next_step": "edit the plan (split compound questions, gloss domain words in English, copy "
                                 "versions exactly as the user wrote them, never invent candidates), then call "
                                 "question_plan_check with plan_json=<the plan as JSON text>"}
        return self._run("question_plan_draft", go, need="graph")

    def question_plan_check(self, plan_json: str) -> dict:
        def go():
            from verinoda import question_plan as qp

            text = _plan_text(plan_json)
            if text is None:
                raise ToolFailure("invalid_argument", "plan_json must be a non-empty string", PLAN_HINT)
            plan, problems = qp.parse(text)
            if plan is None:  # recorded like any other checked plan, then reported
                check = {"schema": qp.CHECK_SCHEMA_ID, "status": "invalid", "errors": problems, "warnings": [],
                         "links": [], "references": [], "clarifications": [], "unknowns": []}
                with self._store() as st:
                    snap = st.latest_snapshot()
                    pid = qp.store_plan(st, {"unparsed": text[:20000]}, check, "host",
                                        snapshot_id=(snap or {}).get("id"))
                raise ToolFailure("invalid_plan", f"the plan is not a JSON object; it was recorded as {pid}",
                                  PLAN_HINT, problems=problems, plan_id=pid, status="invalid")
            g = self._graph()
            res = qp.check(plan, g, self.repo, self._lexicon(), source="host")
            with self._store() as st:
                snap = st.latest_snapshot()
                pid = qp.store_plan(st, plan, res, "host", snapshot_id=(snap or {}).get("id"))
            if res["status"] == "invalid":
                raise ToolFailure("invalid_plan", f"the plan failed validation ({len(res['errors'])} problem(s)); "
                                                  f"it was recorded as {pid}", PLAN_HINT,
                                  problems=res["errors"], plan_id=pid, status="invalid")
            out = {"plan_id": pid, **qp.compact_check(res), "unknowns": res.get("unknowns") or [],
                   "language_detected": res.get("language_detected"),
                   "intents_detected": res.get("intents_detected") or []}
            out["next_step"] = (
                "ask the user the clarifications (options as listed), add answers[] with each clarification_id "
                "to the plan, then call question_plan_check again or analyze with plan_json"
                if res["status"] == "needs_clarification" else
                "call analyze with plan_json=<this plan>; start the answer with 'Understood as / Anladığım: ...'")
            return out
        return self._run("question_plan_check", go, need="graph", keep=("status", "plan_id", "errors",
                                                                         "clarifications"))

    def lexicon_show(self, word: str) -> dict:
        def go():
            from verinoda import lexicon as lexmod

            w = _text(word, "word")
            res = lexmod.show(self.repo, w, graph=self._graph())
            res["note"] = ("lexicon pairs and seed glosses only widen the search; they are candidates, "
                           "never evidence for a claim")
            return res
        return self._run("lexicon_show", go, need="graph", first=("text_sites",))

    # -- analysis & claims ----------------------------------------------------------
    def analyze(self, question: str = "", run_tests: bool = False, budget_seconds: float = 60,
                budget_calls: int = 40, plan_json: str | None = None, observe: bool = False,
                intent: str | None = None) -> dict:
        def go():
            from verinoda import analysis
            from verinoda.question_plan import INTENTS

            q = _opt_text(question) or ""
            plan = _plan_text(plan_json)
            if not q and plan is None:
                raise ToolFailure("invalid_argument", "question must be a non-empty string (or pass plan_json)",
                                  "pass the user's question, or a checked plan as plan_json")
            want = _opt_text(intent)
            if want is not None and want not in INTENTS:
                raise ToolFailure("invalid_argument", f"unknown intent {want!r}",
                                  "choose one of: " + ", ".join(INTENTS) + ", or leave it out", valid=list(INTENTS))
            if want is not None and plan is not None:
                raise ToolFailure("invalid_argument", "intent is for a question; a plan's sub-questions carry "
                                  "their own intents", "leave intent out, or set it in the plan")
            b = analysis.Budget(seconds=_clamp(budget_seconds, 1, 600, "budget_seconds", float),
                                tool_calls=_clamp(budget_calls, 1, 200, "budget_calls"),
                                context_tokens=6000)  # same default as `verinoda analyze`
            with self._store() as st:
                g = self._graph_for_analysis(st)
                res = analysis.analyze(st, self.repo, q, plan=plan, budget=b, run_tests=bool(run_tests),
                                       challenge=True, graph=g if g is not None else self._stale_graph(st),
                                       observe=bool(observe), intent=want)
            ref = res.get("index_refresh") or {}
            if ref.get("skipped") and not ref.get("busy") and ref.get("stale_count"):
                # too slow to run inside the answer: refresh in the background for the next question
                ref["background_update"] = self._update_in_background()
            if res.get("status") == "invalid_plan":
                problems = res.get("errors") or []
                raise ToolFailure("invalid_plan", f"the plan failed validation ({len(problems)} problem(s)); "
                                                  "nothing was analysed", PLAN_HINT, problems=problems,
                                  analysis_id=res.get("analysis_id"), plan_id=res.get("plan_id"))
            from verinoda.analysis_view import lean_capped

            # the answer, not the run (verinoda.analysis_view); plan_audit / claim_inspect for more. Capped
            # here: a claim left out as printed by the passages comes back when the cut removes its lines.
            return lean_capped(res, lambda d: cap_response(d, self.max_chars, first=ANALYZE_FIRST_CUT,
                                                           keep=ANALYZE_KEEP))
        return self._run("analyze", go, need="graph", first=ANALYZE_FIRST_CUT, keep=ANALYZE_KEEP)

    def plan_audit(self, analysis_id: str, refresh: bool = True) -> dict:
        def go():
            from verinoda import analysis

            aid = _text(analysis_id, "analysis_id")
            with self._store() as st:
                return analysis.audit(st, self.repo, aid, refresh=bool(refresh))
        return self._run("plan_audit", go, keep=("changed",))

    def claim_inspect(self, claim_id: str) -> dict:
        def go():
            from verinoda.claims import Claims

            with self._store() as st:
                return Claims(st, self.repo).show(_text(claim_id, "claim_id"))
        return self._run("claim_inspect", go)

    def claim_list(self, status: str | None = None, limit: int = 20) -> dict:
        def go():
            from verinoda.claims import STATUSES

            s = _opt_text(status)
            if s and s not in STATUSES:
                raise ToolFailure("invalid_argument", f"unknown status {s!r}",
                                  "choose one of: " + ", ".join(STATUSES), valid=list(STATUSES))
            n = _clamp(limit, 1, LIST_CAP, "limit")
            with self._store() as st:
                rows = st.claims(status=s, limit=n)
            claims = [{k: r[k] for k in ("id", "status", "confidence", "kind", "text", "updated_at")} for r in rows]
            return {"status_filter": s, "limit": n, "count": len(claims), "claims": claims}
        return self._run("claim_list", go)

    def evidence_inspect(self, evidence_id: str) -> dict:
        def go():
            from verinoda import evidence as evmod

            eid = _text(evidence_id, "evidence_id")
            with self._store() as st:
                ev = st.evidence(eid)
                if ev is None:
                    raise ToolFailure("not_found", f"no evidence {eid}",
                                      "evidence ids are listed by claim_inspect (supporting/refuting/qualifying)")
                cited_by = st.claims_for_evidence(eid)
            stype = ev["source_type"]
            out = {
                "evidence": {k: ev.get(k) for k in ("id", "source_type", "locator", "path", "line_start", "line_end",
                                                    "url", "commit_sha", "version", "content_hash", "excerpt",
                                                    "meta", "collected_at")},
                "source_rank": evmod.SOURCE_RANK.get(stype),
                "verifying": evmod.is_verifying(ev),
                "cited_by": [{"claim": c["id"], "relation": c["relation"], "status": c["status"],
                              "confidence": c["confidence"], "text": c["text"][:200]} for c in cited_by[:20]],
                "cited_by_total": len(cited_by),
            }
            if not out["verifying"]:
                out["note"] = f"{stype} evidence is recorded for traceability and never verifies a claim by itself"
            if ev.get("path") and ev.get("line_start"):
                root = Path((ev.get("meta") or {}).get("root") or self.repo)
                a = int(ev["line_start"])
                b = int(ev.get("line_end") or a)
                shown_end = min(b, a + EXCERPT_MAX_LINES - 1)
                if ev.get("content_hash"):
                    # status: same | moved | changed | gone | ambiguous; candidate: where changed anchored
                    # lines probably are now (never counted as OK)
                    recheck = {"performed": True, **evmod.check_source(self.repo, ev).details()}
                else:
                    recheck = {"performed": False,
                               "reason": "no content hash was recorded for this evidence (e.g. a graph edge), "
                                         "so there is nothing to compare; the current lines are shown for reference"}
                recheck.update({"root": str(root), "file_exists": (root / ev["path"]).is_file(),
                                "current_lines": [a, shown_end],
                                "current_text": evmod.read_lines(root / ev["path"], a, shown_end)})
            else:
                recheck = {"performed": False,
                           "reason": f"not file/line evidence (source_type={stype}); "
                                     + ("re-fetch the URL with `verinoda research` to compare"
                                        if ev.get("url") else "there is nothing on disk to re-read")}
            out["recheck"] = recheck
            return out
        return self._run("evidence_inspect", go)

    def claim_verify(self, claim_id: str, run: bool = False) -> dict:
        def go():
            from verinoda import workflow

            with self._store() as st:
                return workflow.verify(st, self.repo, _text(claim_id, "claim_id"), run=bool(run))
        return self._run("claim_verify", go)

    def claim_challenge(self, claim_id: str) -> dict:
        def go():
            from verinoda import critique

            cid = _text(claim_id, "claim_id")
            g = self._graph() if graph_path(self.repo).exists() else None
            with self._store() as st:
                return critique.challenge(st, self.repo, cid, graph=g)
        return self._run("claim_challenge", go)

    # -- precise resolution & runtime observation --------------------------------------
    def resolve_call(self, path: str, line: int, target: str, target_path: str | None = None,
                     target_line: int | None = None) -> dict:
        def go():
            precise = self._optional("verinoda.precise")
            p = _text(path, "path").replace("\\", "/")
            ln = _clamp(line, 1, 10_000_000, "line")
            tgt = _text(target, "target")
            tp = _opt_text(target_path)
            tl = None if target_line is None else _clamp(target_line, 1, 10_000_000, "target_line")
            with self._store() as st:
                res = precise.resolve_call(self.repo, p, ln, tgt, store=st, target_path=tp, target_line=tl)
            if res is None:  # the same record as `verinoda resolve-call`
                ok, tool = precise.available()
                if not p.endswith((".py", ".pyi")):
                    why = "no fresh index.scip covers this file (SCIP answers non-Python files)"
                    step = "generate a SCIP index for the language and run `verinoda scan <path> --scip FILE`"
                elif not ok:
                    why, step = tool, "pip install 'verinoda[precise]'"
                else:
                    why = f"{p} cannot be read or is outside the repository"
                    step = "check the path (repository-relative) and line"
                return {"answer": None, "status": "no precise answer", "site": f"{p}:{ln}", "target": tgt,
                        "resolver": tool, "why": why, "next_step": step}
            res = dict(res)
            res["note"] = ("only a 'definitive' answer verifies or refutes an edge; dynamic/ambiguous/external/"
                           "unresolved answers are inference at most")
            return res
        return self._run("resolve_call", go)

    # -- name-existence check (D32) ------------------------------------------------------
    def code_check(self, paths: list[str] | None = None, diff: str | None = None, snippet: str | None = None,
                   as_path: str | None = None, env: str | None = None, include_exists: bool = False,
                   deps: bool = False) -> dict:
        if deps:
            def go_deps():
                depcheck = self._optional("verinoda.depcheck")
                if paths or _opt_text(diff) or _opt_text(snippet) or _opt_text(as_path):
                    raise ToolFailure("invalid_argument", "deps checks the whole project's manifests",
                                      "call code_check(deps=true) without paths, diff or snippet")
                # the environment is read from its files; nothing it names is started
                return depcheck.check_deps(self.repo, env=_opt_text(env) or "auto")
            return self._run("code_check", go_deps, first=("findings",),
                             keep=("summary", "exit", "exit_because", "manifests", "limits"))

        def go():
            codecheck = self._optional("verinoda.codecheck")
            ps = [p.replace("\\", "/") for p in _str_list(paths, "paths")] if paths else None
            code = snippet if isinstance(snippet, str) and snippet.strip() else None
            if code is not None and (ps or diff):
                raise ToolFailure("invalid_argument", "give a snippet, paths or diff - not several",
                                  "check a snippet alone (with as_path), or files, or the diff")
            if ps and diff:
                raise ToolFailure("invalid_argument", "give paths or diff, not both", "drop one of them")
            ap = _opt_text(as_path)
            if ap:
                ap = ap.replace("\\", "/")
                if Path(ap).is_absolute() or ".." in Path(ap).parts:
                    raise ToolFailure("invalid_argument", f"as_path {ap!r} must be repository-relative",
                                      "pass a path such as 'pkg/module.py'")
            # env comes from the model, not the user: nothing the checked repository supplies is started
            return compact_check_result(codecheck.check(
                self.repo, ps, diff=_opt_text(diff), snippet=code, as_path=ap, env=_opt_text(env) or "auto",
                include_exists=bool(include_exists), budget_s=CODE_CHECK_BUDGET_S, trust_env=False))
        return self._run("code_check", go, first=("sites",),
                         keep=("status", "summary", "exit", "exit_because", "incomplete", "not_checked", "env",
                               "unknown_summary"))

    def api_members(self, target: str, env: str | None = None, private: bool = False, docs: bool = False) -> dict:
        def go():
            codecheck = self._optional("verinoda.codecheck")
            return codecheck.api(self.repo, _text(target, "target"), env=_opt_text(env) or "auto",
                                 private=bool(private), trust_env=False, docs=bool(docs))
        return self._run("api_members", go, first=("members",))

    def runtime_observe(self, test_ids: list[str] | None = None, symbols: list[str] | None = None,
                        terms: list[str] | None = None, timeout: float | None = None) -> dict:
        def go():
            rt = self._optional("verinoda.runtime.trace")
            cli = self._optional("verinoda.cli")  # its summariser: the CLI and MCP answers are the same
            ids = _str_list(test_ids, "test_ids")
            syms = _str_list(symbols, "symbols")
            words = _str_list(terms, "terms")
            t = None if timeout is None else _clamp(timeout, 1, 3600, "timeout", float)
            g = self._graph()
            selected = None
            if not ids and (syms or words):
                ids = rt.select_tests(g, syms, terms=words)
                selected = ids
                if not ids:
                    return {"status": "unknown", "question": f"which tests run {', '.join(syms or words)}?",
                            "why": "no test statically reaches these symbols and no test name contains them or "
                                   "one of the terms; nothing was run",
                            "next_step": "pass pytest node ids explicitly (test_ids), or call runtime_observe "
                                         "without symbols/terms to observe the whole suite"}
            with self._store() as st:
                res = rt.observe(st, self.repo, ids, timeout=t, snapshot=st.latest_snapshot(), graph=g,
                                 targets=syms)
            return cli._observe_summary(res, g, syms, selected)
        return self._run("runtime_observe", go, need="graph",
                         keep=("run_id", "complete", "error", "next_step", "target_reach"))

    # -- references ---------------------------------------------------------------
    def reference_resolve(self, text: str, references: list[str] | None = None, network: str | None = None,
                          local_intent: bool = False) -> dict:
        def go():
            refs = self._optional("verinoda.references")
            body = _text(text, "text")
            explicit = _str_list(references, "references")
            net = _network(self.repo, network)
            with self._store() as st:
                res = refs.resolve(st, self.repo, body, explicit=explicit, network=net,
                                   local_intent=True if local_intent else None)
            out = refs.compact(res)
            if res.get("error"):
                out["error_detail"] = res["error"]
            pinned = [r["id"] for r in out.get("references") or [] if r.get("status") in ("pinned", "pinned_floating")]
            if out.get("id") and pinned:
                out["research_with"] = (f"reference_research(resolution_id='{out['id']}', reference_id='{pinned[0]}')"
                                        " inspects that reference at exactly its pin")
            return out
        return self._run("reference_resolve", go, keep=("id", "status", "questions_for_user"))

    def reference_research(self, reference: str | None = None, ref: str | None = None, topic: str | None = None,
                           kind: str = "auto", resolution_id: str | None = None,
                           reference_id: str | None = None) -> dict:
        def go():
            if kind not in RESEARCH_KINDS:
                raise ToolFailure("invalid_argument", f"unknown kind {kind!r}",
                                  "choose one of: " + ", ".join(RESEARCH_KINDS), valid=list(RESEARCH_KINDS))
            rid, rref = _opt_text(resolution_id), _opt_text(reference_id)
            if rid or rref:
                return self._research_resolved(rid, rref, _opt_text(reference), _opt_text(ref), _opt_text(topic),
                                               kind)
            r = _text(reference, "reference")
            research = self._optional("verinoda.research")
            with self._store() as st:
                return research.research(st, self.repo, r, ref=_opt_text(ref), topic=_opt_text(topic), kind=kind)
        return self._run("reference_research", go)

    def _research_resolved(self, rid: str | None, rref: str | None, reference: str | None, ref: str | None,
                           topic: str | None, kind: str) -> dict:
        """One reference of a stored resolution at exactly its pin (``verinoda research --resolution``)."""
        if not (rid and rref):
            raise ToolFailure("invalid_argument", "resolution_id and reference_id go together",
                              "pass both, e.g. resolution_id='rrs_...' and reference_id='r1' from reference_resolve")
        if reference:
            raise ToolFailure("invalid_argument", "give either a reference or resolution_id/reference_id, not both",
                              "drop reference: the resolution already names what to research and at which pin")
        research = self._optional("verinoda.research")
        feedback = self._optional("verinoda.feedback")
        with self._store() as st:
            row = st.get("reference_resolutions", rid)
            if row is None:
                raise ToolFailure("not_found", f"no reference resolution {rid}",
                                  "resolution ids come from reference_resolve (field 'id')")
            refs_ = (row.get("result") or {}).get("references") or []
            r = next((x for x in refs_ if x.get("id") == rref), None)
            if r is None:
                raise ToolFailure("not_found", f"resolution {rid} has no reference {rref}",
                                  "choose one of the listed reference ids", valid=[x.get("id") for x in refs_])
            target, pin = (feedback._research_target(r) if r.get("status") in ("pinned", "pinned_floating")
                           else (None, None))
            if not target:  # nothing pinned to inspect: an unknown with its next step, never a guessed version
                unres = (r.get("unresolved") or [{}])[0]
                return {"status": "unresolved", "resolution_id": rid, "reference_id": rref, "class": r.get("class"),
                        "reference_status": r.get("status"),
                        "why": unres.get("why") or (f"reference {rref} is {r.get('status')}; it has no pin that "
                                                    f"can be researched (class {r.get('class')})"),
                        "next_step": unres.get("next_step") or "resolve the reference again with the version the "
                                                                "user meant (reference_resolve, network='on')"}
            pin = {**pin, "resolution_id": rid, "reference_id": rref}
            if feedback._is_dependency(r) and r.get("class") not in getattr(feedback, "_DOC_CLASSES", ()):
                pin["source_type"] = "dependency_source"  # the version this project locks: rank 4 evidence
            out = research.compact(research.research_full(st, self.repo, target, topic=topic, kind=kind, pin=pin))
        if ref:
            out.setdefault("warnings", []).append(f"ref {ref!r} is ignored: the resolution's pin is used")
        return out

    def reference_compare(self, reference: str, topic: str, ref: str | None = None) -> dict:
        def go():
            r, t = _text(reference, "reference"), _text(topic, "topic")
            research = self._optional("verinoda.research")
            with self._store() as st:
                return research.compare(st, self.repo, r, ref=_opt_text(ref), topic=t)
        return self._run("reference_compare", go)

    # -- feedback -----------------------------------------------------------------
    def feedback_submit(self, text: str, claim_id: str | None = None, reference: str | None = None,
                        ref: str | None = None, correction: str | None = None, process: bool = True,
                        expect_pattern: str | None = None, expect_in: str | None = None,
                        topic: str | None = None, references: list[str] | None = None) -> dict:
        def go():
            body = _text(text, "text")
            more = _str_list(references, "references")
            feedback = self._optional("verinoda.feedback")
            kw: dict[str, Any] = {"claim_id": _opt_text(claim_id), "reference": _opt_text(reference),
                                  "ref": _opt_text(ref), "correction": _opt_text(correction),
                                  "expect_pattern": _opt_text(expect_pattern), "expect_in": _opt_text(expect_in)}
            if more:
                kw["references"] = more
            with self._store() as st:
                res = feedback.add(st, self.repo, body, **kw)
                if not process:
                    return res
                fid = res["id"]
                try:
                    return feedback.process(st, self.repo, fid, topic=_opt_text(topic))
                except Exception as exc:  # the feedback is recorded; say so instead of losing its id
                    traceback.print_exc(file=sys.stderr)
                    return {"error": "process_failed", "feedback_id": fid, "added": res,
                            "message": f"{type(exc).__name__}: {exc}"[:600],
                            "hint": f"the feedback was recorded as {fid}; retry with feedback_process "
                                    f"(or `verinoda feedback process {fid} --repo {self.repo}`)"}
        return self._run("feedback_submit", go)

    def feedback_process(self, feedback_id: str, network: str | None = None, topic: str | None = None) -> dict:
        def go():
            fid = _text(feedback_id, "feedback_id")
            net = None if network is None else _choice(network, NETWORK_MODES, "network")
            feedback = self._optional("verinoda.feedback")
            with self._store() as st:
                return feedback.process(st, self.repo, fid, topic=_opt_text(topic), network=net)
        return self._run("feedback_process", go)

    def feedback_resolve(self, feedback_id: str, verdict: str, reason: str, evidence_ids: list[str] | None = None,
                         correction: str | None = None) -> dict:
        def go():
            fid, why = _text(feedback_id, "feedback_id"), _text(reason, "reason")
            if verdict not in VERDICTS:
                raise ToolFailure("invalid_argument", f"unknown verdict {verdict!r}",
                                  "choose one of: " + ", ".join(VERDICTS), valid=list(VERDICTS))
            ids = [evidence_ids] if isinstance(evidence_ids, str) else list(evidence_ids or [])
            feedback = self._optional("verinoda.feedback")
            with self._store() as st:
                return feedback.resolve(st, self.repo, fid, verdict, reason=why,
                                        evidence_ids=[str(i) for i in ids if str(i).strip()],
                                        correction=_opt_text(correction))
        return self._run("feedback_resolve", go)

    # -- decisions (docs/DESIGN.md D33) ---------------------------------------------
    def decision_record(self, action: str, decision_id: str | None = None, chosen: str | None = None,
                        rationale: str | None = None, title: str | None = None, brief_id: str | None = None,
                        guards: list[str] | None = None, governs: list[str] | None = None,
                        revisit_when: list[str] | None = None, supersedes: str | None = None,
                        link: str | None = None,
                        guard_ids: list[str] | None = None, at: str | None = None, reason: str | None = None,
                        until: str | None = None, document: str | None = None,
                        user_statement: str | None = None, question_id: str | None = None) -> dict:
        def go():
            from verinoda import decisions as dm

            act = _choice(action, DECISION_ACTIONS, "action")
            said = _opt_text(user_statement)
            if act in DECISION_NEEDS_USER and not said:
                raise ToolFailure("user_statement_required",
                                  f"'{act}' needs the user's own words (it changes what is enforced, or it is the "
                                  "user's answer)",
                                  "ask the user; pass their answer verbatim as user_statement. Never record, accept, "
                                  "waive or answer on your own judgement")
            try:
                with self._store() as st:
                    if act == "list":
                        return dm.listing(st, self.repo)
                    if act == "answer":
                        from verinoda import decision_brief as dbr

                        try:
                            return dbr.answer(st, _text(brief_id, "brief_id"), _text(question_id, "question_id"),
                                              said)
                        except ValueError as exc:
                            raise ToolFailure("invalid_argument", str(exc)[:600],
                                              "question ids are listed in the brief's questions_for_human") from None
                    if act in ("record", "guard"):  # a script guard runs code: the CLI only (checked again parsed)
                        dm.refuse_script_guards(_str_list(guards, "guards"))
                    if act == "record":
                        g = self._graph() if governs and graph_path(self.repo).exists() else None
                        return dm.record(st, self.repo, chosen=_text(chosen, "chosen"),
                                         rationale=_text(rationale, "rationale"), title=_opt_text(title),
                                         brief_id=_opt_text(brief_id), guards=_str_list(guards, "guards"),
                                         governs=_str_list(governs, "governs"),
                                         revisit_when=_str_list(revisit_when, "revisit_when"),
                                         supersedes=_opt_text(supersedes), user_statement=said, graph=g,
                                         allow_scripts=False)
                    if act == "import":
                        g = self._graph() if graph_path(self.repo).exists() else None
                        return dm.import_doc(st, self.repo, _text(document, "document"), graph=g, user_statement=said)
                    did = _text(decision_id, "decision_id")
                    if act == "guard":
                        return dm.add_guards(st, self.repo, did, _str_list(guards, "guards"), user_statement=said,
                                             allow_scripts=False)
                    if act == "accept":
                        return dm.accept(st, self.repo, did, _str_list(guard_ids, "guard_ids"), user_statement=said,
                                         allow_scripts=False)
                    if act == "supersede":
                        return dm.supersede(st, self.repo, _text(supersedes, "supersedes"), did, user_statement=said)
                    if act == "link":
                        kind, _, target = _text(link, "link").strip().rpartition(" ")  # a kind may be two words
                        if not kind.strip() or not target.strip():
                            raise ToolFailure("invalid_argument", "link is 'KIND ADR-N', e.g. 'amends ADR-0002'",
                                              f"kinds: {', '.join(sorted(dm.REVERSE_LINK))}")
                        return dm.link(st, self.repo, did, kind, target.strip(), user_statement=said)
                    ids = _str_list(guard_ids, "guard_ids")
                    if len(ids) != 1:
                        raise ToolFailure("invalid_argument", "waive takes exactly one guard id in guard_ids",
                                          "pass guard_ids=['g1'] and at='path[:line]'")
                    return dm.waive(st, self.repo, did, ids[0], at=_text(at, "at"), reason=_text(reason, "reason"),
                                    until=_opt_text(until), user_statement=said)
            except dm.DecisionError as exc:
                raise ToolFailure("invalid_argument", str(exc)[:600],
                                  "decision_record(action='list') shows the records, their guards and ids") from None
        return self._run("decision_record", go)

    def decision_brief(self, question: str, options: list[str] | None = None, quotes: list[dict] | None = None,
                       agent_arguments: list[str] | None = None) -> dict:
        def go():
            from verinoda import decision_brief as dbr

            q = _text(question, "question")
            qs = []
            for item in quotes or []:
                if not isinstance(item, dict) or not _opt_text(item.get("url")) or not _opt_text(item.get("text")):
                    raise ToolFailure("invalid_argument", "each quote is {url, text[, option]}",
                                      "pass the page URL and the verbatim sentence it must contain")
                qs.append({"url": str(item["url"]), "text": str(item["text"]),
                           **({"option": str(item["option"])} if item.get("option") else {})})
            g = self._graph() if graph_path(self.repo).exists() else None
            with self._store() as st:
                return dbr.brief(self.repo, q, store=st, graph=g, options=_str_list(options, "options"), quotes=qs,
                                 agent_arguments=_str_list(agent_arguments, "agent_arguments"))
        return self._run("decision_brief", go, keep=("brief_id", "verdict", "questions_for_human", "next_step"))

    def decision_check(self, base: str | None = None, changed_only: bool = False, refresh: bool = True) -> dict:
        def go():
            from verinoda import decisions as dm
            from verinoda import guards

            b = _opt_text(base)
            if b and changed_only:
                raise ToolFailure("invalid_argument", "give base or changed_only, not both",
                                  "changed_only=true is base='HEAD'")
            try:
                recs = dm.load_all(self.repo)
            except dm.DecisionError as exc:
                raise ToolFailure("invalid_argument", str(exc)[:600], "fix the decisions folder (decisions.dir in "
                                  ".verinoda/config.json, [decisions] dir in verinoda.toml or "
                                  "[tool.verinoda.decisions] dir in pyproject.toml)")
            note, graph, stale_graph = None, None, None
            if any(d.enforced and g.get("kind") in dm.EDGE_KINDS and g.get("status") == "accepted"
                   for d in recs for g in d.guards):
                with self._store() as st:
                    graph = self._graph_for_analysis(st)
                    if graph is None and refresh:
                        from verinoda import buildlock, workflow

                        # a gate: a build already running is waited for (bounded), never checked around
                        up = workflow.update(st, self.repo, wait=buildlock.GATE_WAIT_SECONDS_MCP,
                                             purpose="decision_check refresh (MCP)")
                        note = (f"refreshed first ({up.get('mode')}, {up.get('changed_count') or 0} changed "
                                "file(s))") if not up.get("error") else f"could not be refreshed: {up['error']}"
                        if up.get("error"):
                            stale_graph = guards.stale_graph_note(up)
                if graph is None and graph_path(self.repo).exists():
                    graph = self._graph()
            try:
                res = guards.check(self.repo, graph=graph, base=b, changed_only=bool(changed_only), records=recs,
                                   graph_stale=stale_graph)
            except ValueError as exc:
                raise ToolFailure("invalid_argument", str(exc)[:600], "base is a git revision such as HEAD~1 or "
                                                                       "origin/main") from None
            if note:
                res["index"] = note
            return res
        return self._run("decision_check", go, keep=("status", "exit", "violations", "next_step"))

    def dependency_ask(self, source: str, target: str) -> dict:
        def go():
            from verinoda import decisions as dm
            from verinoda import dependency_ask as da

            try:
                return da.ask(self.repo, source, target)
            except (da.AskError, dm.DecisionError) as exc:
                raise ToolFailure("invalid_argument", str(exc)[:600], "source: a project path such as "
                                  "src/ui/view.py; target: a project file, a module or a package name") from None
        return self._run("dependency_ask", go, keep=("verdict", "rules", "next_step"))

    # -- index ----------------------------------------------------------------------
    def index_update(self) -> dict:
        if not atlas_dir(self.repo).is_dir():
            return self._first_scan()

        def go():
            from verinoda import analysis, buildlock, workflow

            # a project whose last graph build was slow takes the changed files in now and rebuilds the
            # graph in the background (D44): the agent is not held for half a minute per edit
            last = buildlock.last_build_seconds(self.repo)
            fast = last is not None and last > analysis.REFRESH_INLINE_SECONDS
            with self._store() as st:
                return workflow.update(st, self.repo, wait=MCP_BUILD_WAIT, purpose="index_update (MCP)", fast=fast)
        return self._run("index_update", go)

    def _first_scan(self) -> dict:
        """index_update on a folder never scanned: the agent asked for an index, so it is built here (the same
        `verinoda scan`), except in a folder that is not one project - a home folder, a drive root, or a workspace
        holding two or more projects (sub-folders with their own .git or .verinoda) - which is a wrongly resolved
        project."""
        def go():
            from verinoda import workflow

            home = Path.home().resolve()
            try:
                projects = [d.name for d in self.repo.iterdir() if d.is_dir() and not d.name.startswith(".")
                            and ((d / ".git").exists() or atlas_dir(d).is_dir())]
            except OSError:
                projects = []
            why = ("a home folder" if self.repo == home else "a drive root" if self.repo.parent == self.repo
                   else f"a workspace of {len(projects)} projects ({', '.join(sorted(projects)[:5])})"
                   if len(projects) >= 2 else None)
            if why:
                raise ToolFailure(
                    "not_initialised",
                    f"{self.repo} is not scanned, and it is {why}, not one project",
                    "start the server with --repo <project>, or run `verinoda scan <project>` in a terminal",
                    repo=str(self.repo))
            workflow.init(self.repo)
            with self._store() as st:
                res = workflow.scan(st, self.repo, wait=MCP_BUILD_WAIT, purpose="index_update (MCP, first scan)")
            res["mode"] = "first_scan"
            return res
        return self._run("index_update", go, need="none")

    # -- experiments and the debug ledger ----------------------------------------------
    def experiment_run(self, command: list[str], hypothesis: str, expect: str = "pass", timeout: float | None = None,
                       claim_id: str | None = None, ref: str | None = None,
                       overlay: list[str] | None = None) -> dict:
        def go():
            from verinoda import experiments

            argv = _argv_list(command, "command")
            if not argv:
                raise ToolFailure("invalid_argument", "command is empty", "pass the command as a list of arguments, "
                                  "e.g. ['python', '-m', 'pytest', '-q', 'tests/test_x.py']")
            exp = _choice(expect, ("pass", "fail"), "expect")
            t = None if timeout is None else _clamp(timeout, 1, 3600, "timeout", float)
            with self._store() as st:
                try:
                    return experiments.run(st, self.repo, argv, hypothesis=_text(hypothesis, "hypothesis"), expect=exp,
                                           timeout=t, claim_id=_opt_text(claim_id), ref=_opt_text(ref),
                                           overlay=_str_list(overlay, "overlay") or None)
                except experiments.ExperimentRefused as exc:
                    return experiments.refusal(self.repo, exc)
        return self._run("experiment_run", go, keep=("id", "outcome", "status", "reason", "tree"))

    def change_probe(self, symbol: str | None = None, base: str | None = None, no_base: bool = False,
                     changed: bool = False, inputs: int | None = None, seed: int | None = None,
                     properties: list[str] | None = None, examples: list[str] | None = None, scaling: bool = False,
                     allow_side_effects: bool = False, emit_test: bool = False) -> dict:
        def go():
            from verinoda import probe

            sym = _opt_text(symbol)
            if bool(sym) == bool(changed):
                raise ToolFailure("invalid_argument", "give either symbol or changed=true",
                                  "symbol='path.py::name', or changed=true to probe every changed function")
            if changed and no_base:
                raise ToolFailure("invalid_argument", "changed=true compares with a base; no_base cannot be used",
                                  "drop no_base, or name one function")
            kw = dict(inputs=300 if inputs is None else _clamp(inputs, 10, probe.MAX_INPUTS, "inputs"),
                      seed=0 if seed is None else _clamp(seed, 0, 2**31 - 1, "seed"),
                      properties=_str_list(properties, "properties"), examples=_str_list(examples, "examples"),
                      scaling=bool(scaling), allow_side_effects=bool(allow_side_effects), emit_test=bool(emit_test))
            with self._store() as st:
                if changed:
                    return probe.probe_changed(st, self.repo, base=_opt_text(base) or "HEAD", **kw)
                return probe.compact(probe.probe(st, self.repo, sym, base=_opt_text(base) or "HEAD",
                                                 no_base=bool(no_base), **kw))
        return self._run("change_probe", go, keep=("status", "headline", "symbol", "probe_id", "differences",
                                                   "next_step"),
                         first=("status", "headline", "differences"))

    def _debug(self, tool: str, fn: Callable, keep: tuple[str, ...] = ()) -> dict:
        def go():
            from verinoda import debug, experiments

            with self._store() as st:
                try:
                    return debug.compact(fn(debug, st))
                except experiments.ExperimentRefused as exc:
                    out = experiments.refusal(self.repo, exc)
                    out["next_step"] = ((f"{exc.next_step}; or " if exc.untrusted and exc.next_step else "")
                                        + "run the command yourself and record it with debug_attempt(observed_output="
                                        "..., exit_code=..., command=[the command you ran]) (agent-reported, lower "
                                        "trust), or debug_start(..., observed_output=..., exit_code=...)")
                    return out
        return self._run(tool, go, keep=("session", "attempt", "outcome", "stop", "stop_reason", "strategies",
                                         "questions_for_human", "loop", "status", "reason", *keep),
                         first=("stop", "stop_reason", "outcome", "loop", "strategies"))

    def debug_start(self, symptom: str, command: list[str], base: str | None = None, trace: bool = False,
                    observed_output: str | None = None, exit_code: int | None = None) -> dict:
        def call(d, st):
            argv = _argv_list(command, "command")
            code = None if exit_code is None else _clamp(exit_code, -2 ** 31, 2 ** 31, "exit_code")
            return d.start(st, self.repo, _text(symptom, "symptom"), argv, base=_opt_text(base), trace=bool(trace),
                           observed_output=None if observed_output is None else str(observed_output), exit_code=code)
        return self._debug("debug_start", call)

    def debug_attempt(self, hypothesis: str, session_id: str | None = None, command: list[str] | None = None,
                      expect: str = "pass", kind: str = "fix", observed_output: str | None = None,
                      exit_code: int | None = None, trace: bool | None = None) -> dict:
        def call(d, st):
            argv = _argv_list(command, "command") or None
            exp = _choice(expect, ("pass", "fail"), "expect")
            k = _choice(kind, DEBUG_KINDS, "kind")
            code = None if exit_code is None else _clamp(exit_code, -2 ** 31, 2 ** 31, "exit_code")
            return d.attempt(st, self.repo, _opt_text(session_id), hypothesis=_text(hypothesis, "hypothesis"),
                             expect=exp, command=argv, kind=k,
                             observed_output=None if observed_output is None else str(observed_output),
                             exit_code=code, trace=trace)
        return self._debug("debug_attempt", call)

    def debug_status(self, session_id: str | None = None) -> dict:
        return self._debug("debug_status", lambda d, st: d.status(st, self.repo, _opt_text(session_id)),
                           keep=("symptom", "attempts", "latest", "result"))

    def debug_strategy(self, strategy: str, session_id: str | None = None, good: str | None = None,
                       bad: str | None = None, times: int | None = None, prepare: bool = False,
                       trace: bool = False, overlay: list[str] | None = None) -> dict:
        def call(d, st):
            s = _choice(strategy, DEBUG_STRATEGIES, "strategy")
            sid = _opt_text(session_id)
            lay = _str_list(overlay, "overlay") or None
            if s == "differential":
                return d.differential(st, self.repo, sid, prepare=bool(prepare), trace=bool(trace), overlay=lay)
            if s == "bisect":
                return d.bisect(st, self.repo, sid, good=_opt_text(good), bad=_opt_text(bad), overlay=lay)
            if s == "rerun":
                return d.rerun(st, self.repo, sid, times=None if times is None else _clamp(times, 1, 20, "times"))
            return d.observe(st, self.repo, sid)
        return self._debug("debug_strategy", call, keep=("strategy", "conclusion", "hunks", "first_bad_commit",
                                                         "pass_rate", "next_step"))


def _error_code(exc: BaseException) -> str:
    name = type(exc).__name__
    if name == "ClaimRuleError":
        return "rule_violation"
    if name == "ExperimentRefused":
        return "refused"
    if name == "NotAGitTree":
        return "not_a_git_tree"
    if isinstance(exc, (ValueError, TypeError)):
        return "invalid_argument"
    return "internal_error"


def _error_hint(exc: BaseException, repo: Path) -> str:
    name = type(exc).__name__
    if name == "ClaimRuleError":
        return "the evidence does not allow that status; inspect the claim with claim_inspect"
    if name == "ExperimentRefused":
        return getattr(exc, "next_step", None) or ("the command is outside the process-isolation allowlist; enable "
                                                   "docker/podman or run it manually")
    return (f"see the server log (stderr); `verinoda doctor --repo {repo}` checks the installation "
            "and index")


# -- MCP registration -------------------------------------------------------------

# the core tool profile: what an agent needs to answer questions about code, check its own edits and
# re-index; every other family (question plans, references, feedback, decisions, the debug ledger,
# experiments, runtime tracing, claim re-checks) is served with `--profile full` (or config mcp.profile)
CORE_TOOLS: tuple[str, ...] = (
    "project_query", "analyze", "node_inspect", "relation_trace", "map_view", "claim_inspect", "claim_list",
    "evidence_inspect", "index_update", "code_check", "decision_check", "dependency_ask", "change_review",
    "history_search",
)
PROFILES: dict[str, tuple[str, ...]] = {"core": CORE_TOOLS, "full": TOOL_NAMES}
DEFAULT_PROFILE = "core"
# The core menu lists only these (docs/DESIGN.md D61): the tools agents called in the benchmark sessions, and
# the ones every edit needs. The other core tools are reached through GATEWAY, one listed tool whose description
# names them with their arguments; every listed tool is standing context in every request.
CORE_DIRECT: tuple[str, ...] = ("project_query", "analyze", "code_check", "index_update")
GATEWAY = "run_tool"
# what the gateway's description says of each tool behind it: its arguments and what it returns
GATEWAY_CATALOG: dict[str, str] = {
    "node_inspect": "node_inspect {name}: a symbol's definition and edges with file:line",
    "relation_trace": "relation_trace {source, target, mode?: flow|any}: call paths between symbols",
    "map_view": "map_view {view: hierarchy|dependencies|dataflow|config|tests|history|impact|cycles|outline|dead|hotspots|sides|repo|"
                "dsm|model|saved, targets?}",
    "claim_list": "claim_list {status?}, claim_inspect {claim_id}, evidence_inspect {evidence_id}: claims, "
                  "evidence re-checked",
    "change_review": "change_review {targets?, change?: body|signature|remove} before editing, {since_last?} after: "
                     "what it touches",
    "decision_check": "decision_check {changed_only?}: tree vs accepted decisions",
    "dependency_ask": "dependency_ask {source, target}: may source import it",
    "history_search": "history_search {text, regex?, path?}: when text came/went; {symbol}: its "
                      "commits; {message?, author?, since?, until?, diff?, path?}: commits; {base, head?}: "
                      "compare",
}

_INSTRUCTIONS_HEAD = """Verinoda: evidence-first answers about the repository {repo}.
Questions: project_query (where is X; hits are leads) or analyze (claims with evidence: start the answer with its
understood_as, one block per sub-question; never state weak_inference or unknown as fact). One symbol: node_inspect;
call paths: relation_trace; architecture: map_view; earlier claims: claim_list, claim_inspect, evidence_inspect.
Cite the narrowest lines that support a claim (source lines are numbered), not the whole span of a header.
Editing: change_review before and after (report every concern); index_update after editing; code_check only on
code you wrote or edited, not to read code."""

_INSTRUCTIONS_DECISIONS = """ dependency_ask(source, target) before adding an import; decision_check(changed_only=true)
before finishing - on VIOLATED fix the code or ask the user."""

_INSTRUCTIONS_FULL = """
Understand the question first: question_plan_draft(question) -> edit the plan (split compound questions, gloss
domain words in English, copy versions as written, never invent candidates) -> question_plan_check(plan_json):
ready | needs_clarification (ask the user, add answers[] with each clarification_id) | invalid -> analyze(plan_json).
References the user gives (links, repos, packages, versions, commits, PR/issue numbers, papers, docs): call
reference_resolve first; report each as <name> @ <pin> (basis: <basis>) with its mismatches; never substitute
the default branch for a named version; ask only questions_for_user. reference_research(resolution_id,
reference_id) inspects one at its pin; reference_compare compares a mechanism.
- plan_audit: re-judge an analysis later. lexicon_show: the code words the repository ties to a word.
- run_when: when a method runs - the event or caller that starts it and the conditions on the way (not evaluated).
- history_search: when a text appeared or disappeared (the commit is the evidence), commits by message, author,
  path, date or diff content, two revisions compared.
- claim_verify / claim_challenge: re-check a claim's lines; adversarial check. resolve_call: which definition
  a call binds to (only 'definitive' verifies). api_members: the real members of a module or class.
- runtime_observe: selected tests under the call tracer. experiment_run: one allowlisted command in a copy.
- feedback_submit / feedback_process / feedback_resolve: user critique as a hypothesis, verified, resolved.
- decision_brief: the code's side of a should/which question; no recommendation - ask the user its questions and
  record each answer (decision_record action='answer'). decision_record: never record, accept, waive, supersede
  or link without the user's own words (user_statement).
- debug_start before the first edit of a bug fix, debug_attempt after every edit; on stop=true stop editing, run
  strategies[0] with debug_strategy and show debug_status. Never say "fixed": the repro passed at tree T in run R.
- grep_context: what the Grep hook adds (definition, callers, callees of a searched symbol).
- read_context: what the Read/Edit hook adds (decision records, notes and rules about the file).
- change_probe: after editing a Python function, its base and working-tree versions on generated inputs. A
  difference is a behaviour change, not a bug; say "no difference found in N inputs", never "verified"; a refusal,
  inconclusive or incomplete is not a pass."""

_INSTRUCTIONS_CORE_HEAD = """Verinoda: evidence-first answers about the repository {repo}.
Questions: project_query (where is X; hits are leads) or analyze (claims with evidence: start the answer with its
understood_as, one block per sub-question; never state weak_inference or unknown as fact). Cite the narrowest lines
that support a claim (source lines are numbered), not the whole span of a header. index_update after editing;
code_check only on code you wrote or edited, not to read code. run_tool reaches node_inspect, relation_trace,
map_view, claim_list, claim_inspect, evidence_inspect, change_review (before and after editing: report every
concern) and history_search (when a text appeared or disappeared: the commit is the evidence)."""

_INSTRUCTIONS_CORE_DECISIONS = """ Through run_tool, dependency_ask before adding an import and
decision_check(changed_only=true) before finishing - on VIOLATED fix the code or ask the user."""

_INSTRUCTIONS_CORE = """
More tools (question plans, references, feedback, decision records and briefs, the debug ledger, experiments,
runtime tracing): `verinoda mcp serve --profile full`, or the `verinoda` CLI."""

_INSTRUCTIONS_TAIL = """
Rules: graph edges are extractions, never verification. Missing evidence -> 'unknown' plus a next step, never a
guess. "truncated": true means lists were cut. Errors: {{"error", "message", "hint"}}; not_initialised/no_index:
call index_update."""


def instructions(profile: str = DEFAULT_PROFILE, *, decisions: bool = True) -> str:
    """The server instructions for a profile (``{repo}`` still to be filled in); ``decisions=False``: the
    menu has no decision_check or dependency_ask (:func:`served_tools`), so they do not name them."""
    if profile == "core":
        return (_INSTRUCTIONS_CORE_HEAD + (_INSTRUCTIONS_CORE_DECISIONS if decisions else "") + _INSTRUCTIONS_CORE
                + _INSTRUCTIONS_TAIL)
    return _INSTRUCTIONS_HEAD + (_INSTRUCTIONS_DECISIONS if decisions else "") + _INSTRUCTIONS_FULL + _INSTRUCTIONS_TAIL


def _has_decision_records(repo: Path) -> bool:
    """Does the project keep decision records (a Markdown file in its decisions folder)? When that cannot be
    told (a config the folder cannot be read from), yes: the tool stays listed and says what is wrong."""
    try:
        from verinoda.decisions import decisions_dir

        d = decisions_dir(repo)
        return d.is_dir() and any(d.glob("*.md"))
    except Exception:  # noqa: BLE001
        return True


def served_tools(repo: Path, profile: str) -> tuple[str, ...]:
    """The tools a profile lists for ``repo``. The core menu is standing context in every request, so it
    leaves out decision_check and dependency_ask in a project without decision records (nothing to check; `--profile full`
    always lists it). The menu is read at startup: records added later show after a restart."""
    names = PROFILES[profile]
    if profile == "core" and not _has_decision_records(Path(repo)):
        names = tuple(n for n in names if n not in ("decision_check", "dependency_ask"))
    if profile == "core":
        # the Grep hook's call (D62) and the Read/Edit hook's: reached through run_tool, never listed
        names = (*names, "grep_context", "read_context")
    return names


def listed_tools(repo: Path, profile: str) -> tuple[str, ...]:
    """The tools the menu lists: the core profile's direct tools and the gateway to the rest of
    :func:`served_tools`; the full profile lists every tool."""
    if profile != "core":
        return served_tools(repo, profile)
    behind = [n for n in served_tools(repo, profile) if n not in CORE_DIRECT]
    return (*CORE_DIRECT, *((GATEWAY,) if behind else ()))


def gateway_description(behind: list[str]) -> str:
    """run_tool's description: the tools behind it, with their arguments."""
    parts, said = [], set()
    for n in behind:
        key = "claim_list" if n in ("claim_list", "claim_inspect", "evidence_inspect") else n
        if key in GATEWAY_CATALOG and key not in said:
            said.add(key)
            parts.append(GATEWAY_CATALOG[key])
    return ("Run one more Verinoda tool (name, arguments). " + "; ".join(parts) + ".")


INSTRUCTIONS = instructions("full")

DESCRIPTIONS: dict[str, str] = {
    "project_query": (
        "Where is X / what handles Y: ranked code locations as plain text (path:lines headers, call outlines, "
        "numbered source lines; up to 6000 chars, truncation stated). format='json' for programs. Hits are "
        "leads, not verified claims."),
    "node_inspect": (
        "One symbol or file (id, label, 'path/file.py::symbol', 'Class.method', path): location, source (at "
        "most 30 lines) and edges in and out with file:line. A 'scored' resolution is a guess: check "
        "'candidates'. Edges are extractions, not verification."),
    "relation_trace": (
        "Paths (up to 3, at most 8 hops) from source to target, each hop with relation, confidence and "
        "file:line. mode='flow': calls; 'any': also uses/imports/inherits. No static path does not prove "
        "there is none at runtime."),
    "run_when": (
        "When a method runs: paths back through its callers to the event or scheduler that starts it (JVM "
        "registrations and lambdas: 'at the end of every server tick', '80 ticks later'), each call with the "
        "conditions around it as written (if/for/while blocks, early returns) and file:line. Conditions are not "
        "evaluated; a path that ends at a method nothing calls is an entry point or dead code."),
    "history_search": (
        "Git history. With text: the commit that first added it and, when HEAD has none, the one that last "
        "removed it, each a claim with the commit as evidence and file:line (regex=true: a pattern over changed "
        "lines). Without text: commits by message, author, path, since/until dates and diff content (a regex), "
        "newest first. With base: what head (default HEAD) has that base has not - commits and changed files. "
        "With symbol (a name or path:A-B): the commits that changed its lines, each message quoted. "
        "Regexes are git's (POSIX extended). A parameter of another mode is an error."),
    "map_view": (
        "One architecture view: hierarchy, dependencies, dataflow, config, tests, history, impact "
        "(dependents of targets; default the working-tree changes), cycles (file dependency cycles and the "
        "fewest dependencies to cut), outline (the wiki page tree; targets = page ids for their Mermaid "
        "diagrams), dead (code no entry point reaches, as claims), hotspots (files and functions by changes x "
        "complexity), sides (client-only code reachable from server code, each path as a claim), or repo (files "
        "ranked by PageRank toward the targets, with their signatures, under a token budget), dsm (the dependency "
        "structure matrix between folders; targets=['tag'] groups by [architecture.tags]), model (a C4 model "
        "against the code: targets=[a Structurizr DSL path], default [architecture.model]), or saved (a map "
        "kept with `verinoda map save`: targets=[name]; status current, or stale when a file it cites changed; "
        "no targets: the list). 'coverage' states the method and its limits."),
    "change_review": (
        "What a change touches, by concern: the working tree vs HEAD (base=REV, staged), or planned targets "
        "('path.py[::Name]') + change. Dependents, findings ('no finding' is not 'safe'), tests reaching it, "
        "unknowns, read_first. Never edits code."),
    "question_plan_draft": (
        "Draft a question plan (verinoda.question_plan/1) from the user's message by deterministic Turkish/English "
        "rules: sub-questions with intent and done_when, mentions with candidate names, references with the "
        "version as written. Nothing is stored; edit it, then question_plan_check / analyze."),
    "question_plan_check": (
        "Validate and ground a plan (JSON text): schema, sub-question DAG, mentions and versions quoted verbatim, "
        "mentions linked to graph nodes (linked / ambiguous / weak / unlinked; a code name the repository spells "
        "nowhere is not_found with did_you_mean, never replaced). Stored (plan_id). status: ready | "
        "needs_clarification (ask the user, record answers[] with clarification_id) | invalid (an error)."),
    "analyze": (
        "Answer a question as claims with evidence: one verdict per sub-question, claims with file:line, "
        "unknowns with next steps, and the passages. needs_clarification is a normal result (ask the user). "
        "run_tests / observe also run or trace the tests that reach the answer (isolated copy). Re-indexes "
        "first if the tree changed."),
    "plan_audit": (
        "Re-judge an earlier analysis' sub-questions on the current claim statuses (refresh=true re-indexes "
        "first, so claims whose files changed are stale); 'changed' lists the verdicts that moved."),
    "lexicon_show": (
        "The identifier parts, seed glosses and text sites (file:line) the repository associates with a "
        "natural-language word (Turkish or English). They widen search and plan linking only; never evidence. "
        "Read-only."),
    "claim_inspect": (
        "A claim's record: text, status, evidence with its current state, status history. Read-only."),
    "claim_list": (
        "Recorded claims, newest first, optionally by status (ids from earlier sessions). Read-only."),
    "evidence_inspect": (
        "One evidence record re-checked now (same | moved | changed | gone | ambiguous), whether its type can "
        "verify a claim, and the claims citing it. Read-only."),
    "claim_verify": (
        "Re-check a claim's cited lines against the working tree and re-assess its status (changed evidence -> "
        "stale); run=true also re-runs its recorded test experiment in an isolated copy. Returns before/after "
        "and every source check."),
    "claim_challenge": (
        "Adversarial check of a claim: verifying support, cited lines still matching, graph-only support, the "
        "call-site line naming the target, ambiguous names, 'only X' patterns, changed files. Failures attach "
        "refuting evidence; confidence only goes down."),
    "resolve_call": (
        "Which definition the call to target on path:line binds to (jedi for Python, a fresh index.scip for "
        "other languages): definitive | dynamic | ambiguous | external | unresolved. With target_path/target_line "
        "a verdict: confirms | refutes | undetermined (definitive answers only). Read-only."),
    "code_check": (
        "Python, Java, Kotlin, TS/JS imports; other languages: not_checked (exit 4). For code you wrote or "
        "edited: do the modules, names, methods, arguments and keys it uses exist in the project and its "
        "environment? Input: paths, diff (a revision; nothing: changes against HEAD), or snippet + as_path. "
        "Each site: exists | absent (nearest names) | unknown | not_installed | guarded; exit 3 = absent."),
    "api_members": (
        "The real members of a Python module, class or function (dotted target) in the project's environment, "
        "or of a Java class on the build's classpath (access included): name, kind, signature, file:line, "
        "inherited-from, source version; private=true adds '_' "
        "names; docs=true quotes the installed docstring and README section. found=false (exit 3) comes with nearest names; found=null ('unknown' / 'not_installed') was "
        "not decided; 'unsupported_language' (exit 4): the project's code in another language. Read-only."),
    "runtime_observe": (
        "Run tests in an isolated copy under the call tracer (test_ids, else tests selected for symbols/terms, "
        "else the suite): complete, target_reach per symbol, test outcomes, boundary calls, limits and cost. "
        "Observations are run-scoped; calls seen only through test doubles never support production edges."),
    "reference_resolve": (
        "Resolve every reference in a message (links, owner/repo, packages with versions, commits, PR/issue "
        "numbers, papers, docs) to the version the user meant (pin + basis); a named version is never replaced "
        "by the default branch. One line per reference with mismatches, unresolved parts with next steps, "
        "questions_for_user; recorded (id). network: off | cache | on (default: config research.network)."),
    "reference_research": (
        "Inspect a reference at its pin: a git repository (URL or path; ref, else the default branch HEAD) or a "
        "document URL, optionally tracing a topic; or resolution_id + reference_id from reference_resolve. "
        "Recorded with its pinned commit/hash as evidence. May use the network; writes under .verinoda/research."),
    "reference_compare": (
        "Compare a mechanism (topic) between this repository and a reference repository or document pinned at "
        "ref; findings are recorded with evidence from both sides. May use the network."),
    "feedback_submit": (
        "Record critique of a claim (claim_id) or a statement as a hypothesis to verify, never as fact; optional "
        "correction, references to check against, or a checkable proposition (expect_pattern in files matching "
        "expect_in). process=true runs the verification protocol now (else feedback_process later)."),
    "feedback_process": (
        "Run the verification protocol for recorded feedback: resolve its references (network off | cache | on), "
        "inspect them at their pins, re-check the claim and propose a verdict with evidence (unresolved when a "
        "named version cannot be pinned). May use the network."),
    "feedback_resolve": (
        "Close feedback with a verdict (confirmed | qualified | corrected | unresolved), a reason and the "
        "evidence ids that justify it; 'corrected' can carry the corrected statement."),
    "grep_context": (
        "The Grep hook's call (install --hooks): what the static call graph says about the symbols a Grep pattern "
        "names - definition, callers, callees - as a PostToolUse hook output; {} when it knows none."),
    "read_context": (
        "The Read/Edit hook's call: what the project says about one file - decision records whose guards name it, "
        "the user's notes on it or on a glob matching it, Cursor/Kiro rules for it - as a PostToolUse hook "
        "output; {} when nothing does."),
    "index_update": (
        "Re-index after editing files (on a folder never scanned: the first scan); claims whose files changed "
        "become stale. mode: noop | incremental | full | first_scan."),
    "decision_record": (
        "Decision records (Markdown with front matter in decisions.dir, logged append-only). action: list | "
        "record (chosen + rationale; optional brief_id, guards, governs, revisit_when, supersedes) | import (a "
        "record for a hand-written ADR, document=path; guards only proposed) | guard (add guards to decision_id) "
        "| accept (guard_ids) | waive (one guard id, at='path[:line]', reason, until) | supersede (decision_id "
        "replaces supersedes; both records updated) | link (decision_id, link='amends ADR-N'; the reverse link "
        "is written too) | answer (the user's answer to question_id of brief_id). Guards: 'only_in "
        "calls=sqlite3.connect allowed=orders/repository.py', 'no_edge from=src/main/** to=src/client/**', "
        "'layers order=ui/**,core/**', 'allow_edges from=GLOB allowed=GLOB,...', 'public module=GLOB "
        "api=GLOB,...', 'dependency absent=psycopg'; revisit_when: 'dependency_added=NAME' / 'file_appears=GLOB'. "
        "All but list and import need user_statement, the user's own words verbatim: never decide for the user."),
    "decision_brief": (
        "What a human needs to decide a should/which/scale question, from the code - never a recommendation: "
        "forces with re-checkable evidence, absences (searched, not found), existing decisions, options with "
        "presence and the code a change touches, external claims only as quote-checked pins, your arguments as "
        "weak_inference, at most 5 questions_for_human (EN and TR). verdict is always human_decision_required; "
        "record the user's answers with decision_record(action='answer')."),
    "decision_check": (
        "The working tree against the guards of accepted decision records: VIOLATED, possible, reviews, "
        "triggers, ok, unknown. changed_only=true (or base=REV) counts only new findings. exit 3: something "
        "was not checked - never ok. Never edits code or records."),
    "dependency_ask": (
        "Before writing an import: may source (a project file, new or not) depend on target (a project file, "
        "module or package)? forbidden / restricted / allowed / unknown, with each accepted decision guard that "
        "applies and its record line. Reads the records, not the code."),
    "experiment_run": (
        "Run one command as a recorded experiment in a throw-away copy of the working tree (or of commit ref, "
        "with overlay files): allowlisted test runners under process isolation, anything else needs "
        "docker/podman or is refused. Returns outcome (pass | fail | timeout | inconclusive), tree hash, log "
        "paths and evidence id; with claim_id the run supports or refutes that claim."),
    "debug_start": (
        "Open a debug session before the first edit of a bug fix: symptom, repro command (argument list), base "
        "commit (default HEAD). The repro runs once as attempt 0 in a throw-away copy (trace=true adds the call "
        "tracer). A command Verinoda may not run: pass observed_output + exit_code (agent-reported)."),
    "debug_attempt": (
        "Record one attempt after an edit: hypothesis (required); the repro runs again on the current tree (or "
        "command, or observed_output + exit_code for your own run). Returns outcome, tree, failure signature, "
        "progress (improved | same | regressed | unknown), loop findings (definitive: tree_reverted, "
        "signature_recurred, no_progress, test_edited, failing_tests_skipped, off_path; heuristic: file_reverted, "
        "error_moved, masking, hypothesis_repeated, possibly_flaky), flaky, stop, strategies and "
        "questions_for_human. stop=true: stop editing, follow strategies[0]. Never 'fixed': 'the repro passed "
        "at tree T in run R'."),
    "debug_status": (
        "The ledger of a debug session (default: the latest open one): every attempt with hypothesis, tree, "
        "failure, progress and findings, the latest stop and strategies, and whether a passing tree is still "
        "current. Show it to the user when a session stops. Read-only."),
    "debug_strategy": (
        "Run a proposed strategy (recorded as an attempt): differential (the repro on the base commit; the diff's "
        "hunks ranked by the failure; trace=true compares observed calls; prepare=true only writes the copy), "
        "bisect (the first failing commit between good and bad, in throw-away copies), rerun (pass rate over "
        "times runs), observe (one traced run: which failing tests reached each edited function)."),
    "change_probe": (
        "Probe one changed Python function (symbol 'path.py::name' or 'path.py::Class.method'; or changed=true for "
        "every function changed against base): inputs derived from its annotations, call-site literals, boundaries "
        "mined from both versions (comparisons, len checks, slices, imported constants), standard edges (empty, "
        "None, +-1, unicode, special floats, large sizes) and hypothesis; each input runs at the base commit and in "
        "the working tree through the isolated runner (throw-away copies, allowlisted pytest plugin). Returns "
        "status (differences_found | numeric_drift_only (float rounding only, low priority) | no_difference_found | "
        "property_violated | undeclared_exceptions | nothing_found | refused | unsupported | inconclusive; with "
        "changed=true: differences_found | done | incomplete (not every function compared: no pass) | "
        "nothing_changed), difference classes with minimal examples "
        "(reproduced in a second run pair, recorded as run-scoped claims), property violations, undeclared "
        "exceptions, nondeterminism, with scaling=true rough growth, the side-effect gate's reasons, the inputs "
        "used and what was not checked. A static gate refuses functions that write files, use the network, start "
        "processes or change global state (allow_side_effects is the user's decision). Never edits code."),
}

_READ_ONLY = {"project_query", "node_inspect", "relation_trace", "run_when", "history_search", "map_view",
              "claim_inspect", "claim_list", "evidence_inspect", "question_plan_draft", "lexicon_show", "resolve_call",
              "code_check", "api_members", "debug_status", "grep_context", "dependency_ask", "read_context"}
_OPEN_WORLD = {"reference_research", "reference_compare", "feedback_submit", "feedback_process", "reference_resolve"}


def _load_sdk():
    """(server class, major version) from the installed MCP SDK (v2 MCPServer or v1 FastMCP)."""
    try:
        from mcp.server.mcpserver import MCPServer as Server  # mcp >= 2
        major = 2
    except ImportError:
        try:
            from mcp.server.fastmcp import FastMCP as Server  # mcp 1.x
            major = 1
        except ImportError as exc:
            raise RuntimeError("the MCP SDK is not installed; install it with `pip install \"mcp>=1,<3\"` "
                               "(or the verinoda[mcp] extra)") from exc
    sdk_file = Path(sys.modules[Server.__module__].__file__ or "").resolve()
    if Path(__file__).resolve().parent in sdk_file.parents:  # pragma: no cover - misconfigured sys.path
        raise RuntimeError(f"`import mcp` resolved to {sdk_file} inside verinoda.mcp; "
                           "do not put the verinoda package directory itself on sys.path")
    return Server, major


def _tool_annotations(name: str):
    try:
        from mcp.types import ToolAnnotations
    except ImportError:  # pragma: no cover - very old SDK
        return None
    ro = name in _READ_ONLY  # no title: the tool's name says it (every listed byte is standing context)
    return ToolAnnotations(readOnlyHint=ro, destructiveHint=False, idempotentHint=ro,
                           openWorldHint=name in _OPEN_WORLD)


def resolve_profile(repo: Path | str, profile: str | None = None) -> str:
    """The tool profile to serve: ``profile`` (``--profile``), else ``mcp.profile`` in the project's
    config when the user trusts the project (:func:`verinoda.paths.is_trusted`), else ``mcp.profile`` in
    the user-level config, else :data:`DEFAULT_PROFILE`. A cloned repository's own config cannot switch
    an untrusted project to the full tool list (docs/DESIGN.md D63). An unknown name, or a config the
    setting cannot be read from (not JSON, ``mcp`` not an object), is an error, never a silent fallback;
    only a missing config file or a config without ``mcp.profile`` serves the default. An untrusted project's
    file is not read (a broken one cannot stop the server)."""
    from verinoda.paths import is_trusted, user_config_path

    if profile is None:
        fix = 'write it as {"mcp": {"profile": "full"}} (or "core"), or pass --profile'
        trusted = is_trusted(Path(repo))
        for cfg, use in ((atlas_dir(Path(repo)) / "config.json", trusted), (user_config_path(), True)):
            if not use:  # an untrusted project's file is not read at all: it cannot stop the server either
                continue
            user: Any = {}
            if cfg.is_file():
                try:
                    user = json.loads(cfg.read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    raise ValueError(f"cannot read the MCP tool profile from {cfg}: {exc}; {fix}") from None
            mcp = user.get("mcp") if isinstance(user, dict) else None
            if not isinstance(user, dict) or not isinstance(mcp, (dict, type(None))):
                raise ValueError(f"cannot read the MCP tool profile from {cfg}: "
                                 f"{'the file' if not isinstance(user, dict) else 'mcp'} is not a JSON object; {fix}")
            value = (mcp or {}).get("profile")
            if value is not None and not isinstance(value, str):
                raise ValueError(f"mcp.profile in {cfg} must be a string, not {value!r}; {fix}")
            if value is not None:
                profile = value
                break
    profile = profile or DEFAULT_PROFILE
    if profile not in PROFILES:
        raise ValueError(f"unknown MCP tool profile {profile!r}; choose one of: {', '.join(PROFILES)}")
    return profile


def _drop_titles(schema: Any) -> Any:
    """A JSON schema without pydantic's generated ``title`` keys (a property named "title" stays)."""
    if isinstance(schema, dict):
        out = {}
        for k, v in schema.items():
            if k == "title" and isinstance(v, str):
                continue
            out[k] = ({pk: _drop_titles(pv) for pk, pv in v.items()} if k in ("properties", "$defs")
                      and isinstance(v, dict) else _drop_titles(v))
        return out
    if isinstance(schema, list):
        return [_drop_titles(x) for x in schema]
    return schema


def _compact_schemas(srv) -> None:
    """Drop generated titles from every registered tool's input schema (best effort: SDK internals)."""
    try:
        registered = srv._tool_manager._tools
    except AttributeError:  # pragma: no cover - an SDK that stores tools elsewhere keeps its titles
        return
    for tool in registered.values():
        params = getattr(tool, "parameters", None)
        if isinstance(params, dict):
            tool.parameters = _drop_titles(params)


def is_text_result(res: Any) -> bool:
    """A plain-text answer (``project_query`` format='text'): its text is the content block itself."""
    return (isinstance(res, dict) and res.get("format") == "text" and isinstance(res.get("text"), str)
            and "error" not in res)


def _result_wrapper(major: int) -> Callable[[dict], Any]:
    """How a tool's dict reaches the wire.

    mcp 2.x: an explicit CallToolResult - compact JSON text (the SDK default
    would pretty-print it, inflating what the agent reads), the same dict as
    structured content, and ``is_error`` set for ``{"error": ...}`` results.
    A plain-text answer is sent as that text (not as an escaped JSON string),
    with the dict as structured content.
    mcp 1.x: the plain dict (the SDK serialises it).
    """
    if major < 2:
        return lambda res: res
    from mcp.types import CallToolResult, TextContent

    def emit(res: dict):
        if is_text_result(res):
            return CallToolResult(content=[TextContent(type="text", text=res["text"])], structured_content=res,
                                  is_error=False)
        text = json.dumps(res, ensure_ascii=False, separators=(",", ":"))
        return CallToolResult(content=[TextContent(type="text", text=text)], structured_content=res,
                              is_error=bool(res.get("error")))
    return emit


def build_server(repo: Path | str, tools: AtlasTools | None = None, *, profile: str | None = None):
    """An MCP server (SDK object) exposing the tools of a profile (:func:`resolve_profile`; ``core`` by
    default, ``full`` = :data:`TOOL_NAMES`) over ``repo``."""
    from pydantic import Field

    Server, major = _load_sdk()
    t = tools or AtlasTools(repo)
    profile = resolve_profile(t.repo, profile)
    served = set(served_tools(t.repo, profile))
    emit = _result_wrapper(major)
    text = instructions(profile, decisions="decision_check" in served).format(repo=t.repo)
    from verinoda import buildinfo

    # serverInfo.version names the build (``0.1.0.dev0+<commit12>``, ``+unknown``), so a client can tell
    # which Verinoda it started when two installs share a version number
    version = buildinfo.server_version()
    try:
        srv = Server("verinoda", instructions=text, version=version)
    except TypeError:  # mcp 1.x FastMCP has no version argument: set it on the low-level server
        srv = Server("verinoda", instructions=text)
        try:
            srv._mcp_server.version = version
        except AttributeError:  # pragma: no cover - SDK layout changed
            pass
    srv.verinoda_profile = profile
    srv.verinoda_instructions = text
    # a description names only what this profile serves (else the CLI)
    plan_check = "see question_plan_check" if "question_plan_check" in served else "from `verinoda plan check`"

    listed = set(listed_tools(t.repo, profile))
    impl: dict[str, Callable] = {}   # every served tool's function: run_tool calls the ones not listed
    slim = {"analyze", "code_check"} if profile == "core" else set()  # listed with fewer arguments (below)

    def add(name: str, fn, description: str) -> None:
        kwargs: dict[str, Any] = {"name": name, "description": description,
                                  "structured_output": False}  # no output schema: results are open objects
        ann = _tool_annotations(name)
        if ann is not None:
            kwargs["annotations"] = ann
        for drop in ((), ("structured_output",), ("structured_output", "annotations")):
            try:
                srv.add_tool(fn, **{k: v for k, v in kwargs.items() if k not in drop})
                break
            except TypeError:  # pragma: no cover - an older SDK without these arguments
                continue

    from verinoda.question_plan import INTENTS  # the full analyze's intent values

    def register(name: str):
        def deco(fn):
            if name in served:
                impl[name] = fn
            if name in listed and name not in slim:
                add(name, fn, DESCRIPTIONS[name])
            return fn
        return deco

    Id = Annotated[str, Field(description="A claim id such as 'clm_0123456789ab' (from analyze or claim_list).")]
    OptStr = str | None
    Network = Literal["off", "cache", "on"]

    @register("project_query")
    def project_query(
        question: Annotated[str, Field(description="Question, symbol or file names to look up; filters narrow it: "
                                                   "path:GLOB lang: symbol: is:vendored /regex/ AND OR NOT.")],
        max_items: Annotated[int, Field(description="Maximum code locations to return (1-25).")] = 8,
        format: Annotated[Literal["text", "json"], Field(description="'text' (default): plain text for reading, "
                                                                      "skeleton first; 'json': structured items.")]
        = "text",
    ) -> dict[str, Any]:
        return emit(t.project_query(question, max_items=max_items, format=format))

    @register("node_inspect")
    def node_inspect(
        name: Annotated[str, Field(description="Node id, label, 'path/file.py::symbol', 'Class.method' or file path.")],
    ) -> dict[str, Any]:
        return emit(t.node_inspect(name))

    @register("relation_trace")
    def relation_trace(
        source: Annotated[str, Field(description="Start symbol/file (label, 'path/file.py::symbol', 'Class.method').")],
        target: Annotated[str, Field(description="End symbol/file.")],
        mode: Annotated[Literal["flow", "any"],
                        Field(description="'flow' = calls only; 'any' = also uses/imports/inherits.")] = "flow",
    ) -> dict[str, Any]:
        return emit(t.relation_trace(source, target, mode=mode))

    @register("run_when")
    def run_when(
        symbol: Annotated[str, Field(description="The method: 'Class.method' or 'path/File.java::method'.")],
        depth: Annotated[int, Field(description="Caller hops to walk back (1-10).")] = 6,
    ) -> dict[str, Any]:
        return emit(t.run_when(symbol, depth=depth))

    @register("history_search")
    def history_search(
        text: Annotated[OptStr, Field(description="A text (a name, a line) to find when it appeared and "
                                                  "disappeared.")] = None,
        regex: Annotated[bool, Field(description="text is a regular expression over added and removed lines.")]
        = False,
        message: Annotated[OptStr, Field(description="Without text: a regex the commit message matches.")] = None,
        author: Annotated[OptStr, Field(description="Without text: a regex the author's name or e-mail matches.")]
        = None,
        path: Annotated[OptStr, Field(description="Only the history of this file or folder.")] = None,
        since: Annotated[OptStr, Field(description="Commits after this date ('2026-01-31', '2 weeks ago').")] = None,
        until: Annotated[OptStr, Field(description="Commits before this date.")] = None,
        diff: Annotated[OptStr, Field(description="Commits whose added or removed lines match this regex.")] = None,
        base: Annotated[OptStr, Field(description="Compare: the base revision (branch, tag, sha).")] = None,
        head: Annotated[OptStr, Field(description="Compare: the other revision (default HEAD).")] = None,
        limit: Annotated[int, Field(description="Commits to list (1-100).")] = 20,
        symbol: Annotated[OptStr, Field(description="Alone: a symbol or path:A-B; the commits that changed it.")]
        = None,
    ) -> dict[str, Any]:
        return emit(t.history_search(text=text, regex=regex, message=message, author=author, path=path, since=since,
                                     until=until, diff=diff, base=base, head=head, limit=limit, symbol=symbol))

    @register("map_view")
    def map_view(
        view: Annotated[Literal["hierarchy", "dependencies", "dataflow", "config", "tests", "history", "impact",
                                "cycles", "outline", "dead", "hotspots", "sides", "repo", "dsm", "model",
                                "saved"],
                        Field(description="Which architecture view to return.")],
        targets: Annotated[list[str] | None, Field(description="impact view: changed files or symbols (default = "
                                                               "git working-tree changes); outline view: page ids "
                                                               "whose diagrams to return; repo view: files in "
                                                               "play (default the same changes); saved view: "
                                                               "the map's name; dsm: ['tag'] to group by tags; "
                                                               "model: a Structurizr DSL path.")] = None,
    ) -> dict[str, Any]:
        return emit(t.map_view(view, targets=targets))

    @register("change_review")
    def change_review(
        base: Annotated[OptStr, Field(description="Compare the working tree with this commit (default HEAD).")] = None,
        staged: Annotated[bool, Field(description="Review the staged changes (the index) against HEAD.")] = False,
        targets: Annotated[list[str] | None, Field(description="A planned change, before editing: 'path/file.py' or "
                                                               "'path/file.py::Qual.name' items.")] = None,
        change: Annotated[Literal["body", "signature", "remove"] | None,
                          Field(description="With targets: the kind of planned change (default body).")] = None,
        concerns: Annotated[list[str] | None, Field(description="Subset of persistence, security, performance, "
                                                                "public_api, config, entry_points, health "
                                                                "(default all).")]
        = None,
        run_tests: Annotated[bool, Field(description="Run the pytest tests that reach the change (isolated copy).")]
        = False,
        observe: Annotated[bool, Field(description="Run them under the call tracer.")] = False,
        max_chars: Annotated[int, Field(description="Budget of read_first in characters (500-50000).")] = 6000,
        findings: Annotated[Literal["introduced", "all"],
                            Field(description="introduced: only what the change introduced (preexisting and fixed "
                                              "under differential); all: preexisting too.")] = "introduced",
        since_last: Annotated[bool, Field(description="Leave out findings the last review already listed.")] = False,
    ) -> dict[str, Any]:
        return emit(t.change_review(base=base, staged=staged, targets=targets, change=change, concerns=concerns,
                                    run_tests=run_tests, observe=observe, max_chars=max_chars, findings=findings,
                                    since_last=since_last))

    @register("question_plan_draft")
    def question_plan_draft(
        question: Annotated[str, Field(description="The user's message, verbatim (Turkish or English).")],
    ) -> dict[str, Any]:
        return emit(t.question_plan_draft(question))

    @register("question_plan_check")
    def question_plan_check(
        plan_json: Annotated[str, Field(description="The plan (verinoda.question_plan/1) as JSON text, e.g. a "
                                                    "draft from question_plan_draft after editing.")],
    ) -> dict[str, Any]:
        return emit(t.question_plan_check(plan_json))

    @register("analyze")
    def analyze(
        question: Annotated[str, Field(description="The question (optional with plan_json).")] = "",
        run_tests: Annotated[bool, Field(description="Also run the tests that reach the answer (isolated copy).")]
        = False,
        budget_seconds: Annotated[float, Field(description="Wall-time budget in seconds (1-600).")] = 60,
        budget_calls: Annotated[int, Field(description="Internal tool-call budget (1-200).")] = 40,
        # plain `str`: the SDK would parse a JSON string into an object for an optional (str | None) field
        plan_json: Annotated[str, Field(description=f"A checked question plan as JSON text ({plan_check}).")]
        = "",
        observe: Annotated[bool, Field(description="Trace the tests that reach the answer (isolated copy).")]
        = False,
        intent: Annotated[Literal[INTENTS] | None,  # type: ignore[valid-type]
                          Field(description="What the question asks for, as you read it (not with plan_json). "
                                            "Used only when the rules' reading agrees, else intent_check gives "
                                            "both and the rules' is used. Never changes a claim's status.")]
        = None,
    ) -> dict[str, Any]:
        return emit(t.analyze(question, run_tests=run_tests, budget_seconds=budget_seconds,
                              budget_calls=budget_calls, plan_json=plan_json, observe=observe, intent=intent))

    @register("plan_audit")
    def plan_audit(
        analysis_id: Annotated[str, Field(description="An analysis id such as 'ana_0123456789ab' (from analyze).")],
        refresh: Annotated[bool, Field(description="Re-index first so claims whose files changed are stale.")]
        = True,
    ) -> dict[str, Any]:
        return emit(t.plan_audit(analysis_id, refresh=refresh))

    @register("lexicon_show")
    def lexicon_show(
        word: Annotated[str, Field(description="A natural-language word or phrase (e.g. 'siparis', 'persistence').")],
    ) -> dict[str, Any]:
        return emit(t.lexicon_show(word))

    @register("claim_inspect")
    def claim_inspect(claim_id: Id) -> dict[str, Any]:
        return emit(t.claim_inspect(claim_id))

    @register("claim_list")
    def claim_list(
        status: Annotated[OptStr, Field(description="Only claims with this status (e.g. 'stale').")] = None,
        limit: Annotated[int, Field(description="Maximum claims to return (1-50).")] = 20,
    ) -> dict[str, Any]:
        return emit(t.claim_list(status=status, limit=limit))

    @register("evidence_inspect")
    def evidence_inspect(
        evidence_id: Annotated[str, Field(description="An evidence id such as 'evd_0123456789ab' "
                                                      "(from claim_inspect).")],
    ) -> dict[str, Any]:
        return emit(t.evidence_inspect(evidence_id))

    @register("claim_verify")
    def claim_verify(
        claim_id: Id,
        run: Annotated[bool, Field(description="Also re-run the claim's recorded test experiment.")] = False,
    ) -> dict[str, Any]:
        return emit(t.claim_verify(claim_id, run=run))

    @register("claim_challenge")
    def claim_challenge(claim_id: Id) -> dict[str, Any]:
        return emit(t.claim_challenge(claim_id))

    @register("resolve_call")
    def resolve_call(
        path: Annotated[str, Field(description="Repository-relative file of the call site "
                                               "(e.g. 'orders/service.py').")],
        line: Annotated[int, Field(description="Line of the call (1-based).")],
        target: Annotated[str, Field(description="The called name as the graph labels it (e.g. 'save', "
                                                 "'OrderRepository.save', 'compute_total()').")],
        target_path: Annotated[OptStr, Field(description="File of the expected definition, for a verdict.")] = None,
        target_line: Annotated[int | None, Field(description="Line of the expected definition, for a verdict.")]
        = None,
    ) -> dict[str, Any]:
        return emit(t.resolve_call(path, line, target, target_path=target_path, target_line=target_line))

    EnvArg = Annotated[OptStr, Field(description="'auto' (the project's .venv, venv or env), a venv outside the "
                                                 "project, or 'none' (standard library only).")]

    @register("code_check")
    def code_check(
        paths: Annotated[list[str] | None, Field(description="Repository-relative files or directories to check "
                                                             "(whole files).")] = None,
        diff: Annotated[OptStr, Field(description="A revision (e.g. 'HEAD'): only lines changed against it.")]
        = None,
        snippet: Annotated[OptStr, Field(description="Python code not written yet, checked as if it were in "
                                                     "as_path.")] = None,
        as_path: Annotated[OptStr, Field(description="With snippet: the repository-relative file it is for.")]
        = None,
        env: EnvArg = None,
        include_exists: Annotated[bool, Field(description="Also list the sites that exist and the LOW unknowns.")] = False,
        deps: Annotated[bool, Field(description="Instead: the manifests' dependencies vs the imports.")] = False,
    ) -> dict[str, Any]:
        return emit(t.code_check(paths=paths, diff=diff, snippet=snippet, as_path=as_path, env=env,
                                 include_exists=include_exists, deps=deps))

    @register("api_members")
    def api_members(
        target: Annotated[str, Field(description="Dotted module, class or function name "
                                                 "(e.g. 'packaging.specifiers.SpecifierSet').")],
        env: EnvArg = None,
        private: Annotated[bool, Field(description="Also list names starting with '_'.")] = False,
        docs: Annotated[bool, Field(description="Also quote the installed docstring and README section.")] = False,
    ) -> dict[str, Any]:
        return emit(t.api_members(target, env=env, private=private, docs=docs))

    @register("runtime_observe")
    def runtime_observe(
        test_ids: Annotated[list[str] | None, Field(description="Pytest node ids to run (at most 50).")] = None,
        symbols: Annotated[list[str] | None, Field(description="Symbols to observe (labels, 'path::symbol', "
                                                               "node ids); selects tests when test_ids is "
                                                               "empty.")] = None,
        terms: Annotated[list[str] | None, Field(description="Words that also select tests by name.")] = None,
        timeout: Annotated[float | None, Field(description="Seconds for the test run (1-3600; default: twice "
                                                           "the configured experiment timeout).")] = None,
    ) -> dict[str, Any]:
        return emit(t.runtime_observe(test_ids=test_ids, symbols=symbols, terms=terms, timeout=timeout))

    @register("change_probe")
    def change_probe(
        symbol: Annotated[str | None, Field(description="The function: 'path.py::name' or 'path.py::Class.method' "
                                                        "(or a unique name). Omit with changed=true.")] = None,
        base: Annotated[str | None, Field(description="Commit to compare with (default HEAD).")] = None,
        no_base: Annotated[bool, Field(description="No differential: the working tree only (properties, undeclared "
                                                   "exceptions, nondeterminism).")] = False,
        changed: Annotated[bool, Field(description="Probe every Python function changed against base (at most "
                                                   "10).")] = False,
        inputs: Annotated[int | None, Field(description="Input budget (10-5000, default 300).")] = None,
        seed: Annotated[int | None, Field(description="Seed of the generated inputs (default 0).")] = None,
        properties: Annotated[list[str] | None, Field(description="Python expressions over the parameters and "
                                                                  "`result` that must hold, from the user's request "
                                                                  "(e.g. 'result <= subtotal').")] = None,
        examples: Annotated[list[str] | None, Field(description="Arguments to try first, each a Python literal "
                                                                "tuple such as '(100.0,)'.")] = None,
        scaling: Annotated[bool, Field(description="Also time one list/string argument at growing sizes (rough "
                                                   "growth, both versions).")] = False,
        allow_side_effects: Annotated[bool, Field(description="Run although the side-effect gate refused. Only "
                                                              "when the user agreed.")] = False,
        emit_test: Annotated[bool, Field(description="Return pytest functions that pin the base behaviour (text "
                                                     "only; nothing is written).")] = False,
    ) -> dict[str, Any]:
        return emit(t.change_probe(symbol=symbol, base=base, no_base=no_base, changed=changed, inputs=inputs,
                                   seed=seed, properties=properties, examples=examples, scaling=scaling,
                                   allow_side_effects=allow_side_effects, emit_test=emit_test))

    @register("reference_resolve")
    def reference_resolve(
        text: Annotated[str, Field(description="The user's message (or the part naming references), verbatim.")],
        references: Annotated[list[str] | None, Field(description="Extra references as 'URL[@ref]' strings.")]
        = None,
        network: Annotated[Network | None, Field(description="'off' = cached answers only, no network; 'cache' = "
                                                             "cached first, the network on a miss; 'on' = the "
                                                             "network (default: config research.network, else "
                                                             "'cache').")] = None,
        local_intent: Annotated[bool, Field(description="The question is about this project's own behaviour "
                                                        "(prefer the version the project uses).")] = False,
    ) -> dict[str, Any]:
        return emit(t.reference_resolve(text, references=references, network=network, local_intent=local_intent))

    @register("reference_research")
    def reference_research(
        reference: Annotated[OptStr, Field(description="Git repository URL or local path, or a document URL "
                                                       "(not needed with resolution_id).")] = None,
        ref: Annotated[OptStr, Field(description="Branch, tag or commit to pin (default: default branch "
                                                 "HEAD).")] = None,
        topic: Annotated[OptStr, Field(description="Mechanism to trace in the reference.")] = None,
        kind: Annotated[Literal["auto", "official_doc", "standard", "paper", "secondary", "reference_repo"],
                        Field(description="Source kind; 'auto' detects it.")] = "auto",
        resolution_id: Annotated[OptStr, Field(description="A resolution id from reference_resolve ('rrs_...').")]
        = None,
        reference_id: Annotated[OptStr, Field(description="The reference id inside that resolution ('r1').")]
        = None,
    ) -> dict[str, Any]:
        return emit(t.reference_research(reference, ref=ref, topic=topic, kind=kind, resolution_id=resolution_id,
                                         reference_id=reference_id))

    @register("reference_compare")
    def reference_compare(
        reference: Annotated[str, Field(description="Git repository URL or local path, or a document URL.")],
        topic: Annotated[str, Field(description="The mechanism to compare (e.g. 'order persistence').")],
        ref: Annotated[OptStr, Field(description="Branch, tag or commit of the reference to pin.")] = None,
    ) -> dict[str, Any]:
        return emit(t.reference_compare(reference, topic, ref=ref))

    @register("feedback_submit")
    def feedback_submit(
        text: Annotated[str, Field(description="The critique or statement, in the user's words.")],
        claim_id: Annotated[OptStr, Field(description="The claim the feedback is about, if any.")] = None,
        reference: Annotated[OptStr, Field(description="Repo URL/path or document URL the user cites.")] = None,
        ref: Annotated[OptStr, Field(description="Branch/tag/commit of the reference.")] = None,
        correction: Annotated[OptStr, Field(description="The corrected statement the user proposes.")] = None,
        process: Annotated[bool, Field(description="Run the verification protocol now.")] = True,
        expect_pattern: Annotated[OptStr, Field(description="Regex the user says occurs "
                                                            "(checkable proposition).")] = None,
        expect_in: Annotated[OptStr, Field(description="File glob where expect_pattern should occur.")] = None,
        topic: Annotated[OptStr, Field(description="Mechanism to trace when processing against a reference.")] = None,
        references: Annotated[list[str] | None, Field(description="More references the user cites, as "
                                                                  "'URL[@ref]' strings.")] = None,
    ) -> dict[str, Any]:
        return emit(t.feedback_submit(text, claim_id=claim_id, reference=reference, ref=ref, correction=correction,
                                      process=process, expect_pattern=expect_pattern, expect_in=expect_in,
                                      topic=topic, references=references))

    @register("feedback_process")
    def feedback_process(
        feedback_id: Annotated[str, Field(description="The feedback id (from feedback_submit).")],
        network: Annotated[Network | None, Field(description="Reference resolution mode: 'off' | 'cache' | 'on' "
                                                             "(default: config research.network, else "
                                                             "'cache').")] = None,
        topic: Annotated[OptStr, Field(description="Mechanism to trace when checking against a reference.")] = None,
    ) -> dict[str, Any]:
        return emit(t.feedback_process(feedback_id, network=network, topic=topic))

    @register("feedback_resolve")
    def feedback_resolve(
        feedback_id: Annotated[str, Field(description="The feedback id (from feedback_submit).")],
        verdict: Annotated[Literal["confirmed", "qualified", "corrected", "unresolved"],
                           Field(description="Outcome of the verification.")],
        reason: Annotated[str, Field(description="Why this verdict follows from the evidence.")],
        evidence_ids: Annotated[list[str], Field(description="Evidence ids that justify the verdict.")],
        correction: Annotated[OptStr, Field(description="Corrected statement (verdict 'corrected').")] = None,
    ) -> dict[str, Any]:
        return emit(t.feedback_resolve(feedback_id, verdict, reason, evidence_ids, correction=correction))

    @register("index_update")
    def index_update() -> dict[str, Any]:
        return emit(t.index_update())

    StrList = list[str] | None

    @register("decision_record")
    def decision_record(
        action: Annotated[Literal["list", "record", "import", "guard", "accept", "waive", "supersede", "link",
                                  "answer"], Field(description="What to do (see the tool description).")],
        decision_id: Annotated[OptStr, Field(description="ADR-0001 (guard, accept, waive, link; supersede: the "
                                                         "record that replaces).")] = None,
        chosen: Annotated[OptStr, Field(description="record: the option the user chose.")] = None,
        rationale: Annotated[OptStr, Field(description="record: why, in the user's words.")] = None,
        title: Annotated[OptStr, Field(description="record: a short title.")] = None,
        brief_id: Annotated[OptStr, Field(description="record/answer: the decision brief (dbr_...).")] = None,
        guards: Annotated[StrList, Field(description="record/guard: guard specs.")] = None,
        governs: Annotated[StrList, Field(description="record: 'path/file.py::Symbol' whose changes need a "
                                                      "review.")] = None,
        revisit_when: Annotated[StrList, Field(description="record: 'dependency_added=NAME' / "
                                                           "'file_appears=GLOB'.")] = None,
        supersedes: Annotated[OptStr, Field(description="record/supersede: the decision this one replaces.")] = None,
        link: Annotated[OptStr, Field(description="link: 'KIND ADR-N', e.g. 'amends ADR-0002'.")] = None,
        guard_ids: Annotated[StrList, Field(description="accept: guard ids; waive: exactly one.")] = None,
        at: Annotated[OptStr, Field(description="waive: 'path' or 'path:line' of the excused site.")] = None,
        reason: Annotated[OptStr, Field(description="waive: why the user excuses it.")] = None,
        until: Annotated[OptStr, Field(description="waive: expiry date YYYY-MM-DD.")] = None,
        document: Annotated[OptStr, Field(description="import: the hand-written ADR's path.")] = None,
        user_statement: Annotated[OptStr, Field(description="The user's own words, verbatim (required for "
                                                            "record, guard, accept, waive; for answer it is the "
                                                            "answer).")] = None,
        question_id: Annotated[OptStr, Field(description="answer: the brief's question id (q1, q2 ...).")] = None,
    ) -> dict[str, Any]:
        return emit(t.decision_record(action, decision_id=decision_id, chosen=chosen, rationale=rationale, title=title,
                                      brief_id=brief_id, guards=guards, governs=governs, revisit_when=revisit_when,
                                      supersedes=supersedes, link=link, guard_ids=guard_ids, at=at, reason=reason,
                                      until=until, document=document, user_statement=user_statement,
                                      question_id=question_id))

    @register("decision_check")
    def decision_check(
        base: Annotated[OptStr, Field(description="A git revision (e.g. 'origin/main'): findings in files changed "
                                                  "since it are new/touched, the rest pre-existing.")] = None,
        changed_only: Annotated[bool, Field(description="The same against HEAD (the agent's own changes).")] = False,
        refresh: Annotated[bool, Field(description="Update a stale index first when an edge guard "
                                                   "(no_edge, layers, allow_edges, public) needs the graph.")] = True,
    ) -> dict[str, Any]:
        return emit(t.decision_check(base=base, changed_only=changed_only, refresh=refresh))

    @register("dependency_ask")
    def dependency_ask(
        source: Annotated[str, Field(description="The project file that would depend (may not exist yet).")],
        target: Annotated[str, Field(description="A project file, a module (app.db.store) or a package.")],
    ) -> dict[str, Any]:
        return emit(t.dependency_ask(source, target))

    @register("decision_brief")
    def decision_brief(
        question: Annotated[str, Field(description="The user's question about a choice, verbatim.")],
        options: Annotated[StrList, Field(description="Options the user named (e.g. ['SQLite', 'PostgreSQL']); "
                                                      "names in the question are found too.")] = None,
        quotes: Annotated[list[dict[str, str]] | None,
                          Field(description="External claims as {url, text[, option]}: counted only when the page "
                                            "contains text verbatim (network per research.network).")] = None,
        agent_arguments: Annotated[StrList, Field(description="Your own arguments; shown as weak_inference, "
                                                              "never as evidence. 'OPTION: text' ties one to a "
                                                              "named option.")] = None,
    ) -> dict[str, Any]:
        return emit(t.decision_brief(question, options=options, quotes=quotes, agent_arguments=agent_arguments))
    Cmd = Annotated[list[str], Field(description="The command as a list of arguments, e.g. ['python', '-m', "
                                                 "'pytest', '-q', 'tests/test_x.py'].")]
    SessionId = Annotated[OptStr, Field(description="A debug session id ('dbg_...'); default: the latest open "
                                                    "session.")]

    @register("experiment_run")
    def experiment_run(
        command: Cmd,
        hypothesis: Annotated[str, Field(description="What the run is meant to show.")],
        expect: Annotated[Literal["pass", "fail"], Field(description="The outcome the hypothesis predicts.")] = "pass",
        timeout: Annotated[float | None, Field(description="Seconds (1-3600; default: the configured timeout).")]
        = None,
        claim_id: Annotated[OptStr, Field(description="A claim the run supports or refutes.")] = None,
        ref: Annotated[OptStr, Field(description="Run on a copy of this commit instead of the working tree.")] = None,
        overlay: Annotated[list[str] | None, Field(description="With ref: working-tree files laid over the commit "
                                                               "copy (recorded).")] = None,
    ) -> dict[str, Any]:
        return emit(t.experiment_run(command, hypothesis, expect=expect, timeout=timeout, claim_id=claim_id, ref=ref,
                                     overlay=overlay))

    @register("debug_start")
    def debug_start(
        symptom: Annotated[str, Field(description="What is wrong, in a sentence.")],
        command: Annotated[list[str], Field(description="The repro command as a list of arguments (e.g. the "
                                                        "failing test).")],
        base: Annotated[OptStr, Field(description="The commit every attempt is compared with (default HEAD).")]
        = None,
        trace: Annotated[bool, Field(description="pytest repro: run every attempt under the call tracer.")] = False,
        observed_output: Annotated[OptStr, Field(description="The output of a run you made yourself (for commands "
                                                             "Verinoda may not run).")] = None,
        exit_code: Annotated[int | None, Field(description="With observed_output: that run's exit code.")] = None,
    ) -> dict[str, Any]:
        return emit(t.debug_start(symptom, command, base=base, trace=trace, observed_output=observed_output,
                                  exit_code=exit_code))

    @register("debug_attempt")
    def debug_attempt(
        hypothesis: Annotated[str, Field(description="What you believe and why (repeats of refuted hypotheses are "
                                                     "flagged).")],
        session_id: SessionId = None,
        command: Annotated[list[str] | None, Field(description="A command instead of the session's repro (its pass "
                                                               "is never a pass of the repro); required with "
                                                               "observed_output: the command you ran.")] = None,
        expect: Annotated[Literal["pass", "fail"], Field(description="The outcome the hypothesis predicts.")] = "pass",
        kind: Annotated[Literal["fix", "probe", "rerun", "differential"],
                        Field(description="fix (default), probe (no fix intended), rerun, differential (with "
                                          "observed_output: a run on the prepared base copy).")] = "fix",
        observed_output: Annotated[OptStr, Field(description="The output of a run you made yourself "
                                                             "(agent-reported).")] = None,
        exit_code: Annotated[int | None, Field(description="With observed_output: that run's exit code.")] = None,
        trace: Annotated[bool | None, Field(description="Run this attempt under the call tracer (default: the "
                                                        "session setting).")] = None,
    ) -> dict[str, Any]:
        return emit(t.debug_attempt(hypothesis, session_id=session_id, command=command, expect=expect, kind=kind,
                                    observed_output=observed_output, exit_code=exit_code, trace=trace))

    @register("debug_status")
    def debug_status(session_id: SessionId = None) -> dict[str, Any]:
        return emit(t.debug_status(session_id))

    @register("debug_strategy")
    def debug_strategy(
        strategy: Annotated[Literal["differential", "bisect", "rerun", "observe"],
                            Field(description="Which strategy to run (see strategies in debug_attempt).")],
        session_id: SessionId = None,
        good: Annotated[OptStr, Field(description="bisect: a commit where the repro passes (default: a known "
                                                  "passing run, else Verinoda steps back).")] = None,
        bad: Annotated[OptStr, Field(description="bisect: a commit where it fails (default: the session base).")]
        = None,
        times: Annotated[int | None, Field(description="rerun: how many runs (1-20, default 5).")] = None,
        prepare: Annotated[bool, Field(description="differential: only write a copy of the base for a command you "
                                                   "run yourself.")] = False,
        trace: Annotated[bool, Field(description="differential, pytest: also compare the failing tests' observed "
                                                 "calls at the base with the failing tree.")] = False,
        overlay: Annotated[list[str] | None, Field(description="differential, bisect: working-tree files (e.g. a new "
                                                               "test) laid over the old commit copies; recorded.")]
        = None,
    ) -> dict[str, Any]:
        return emit(t.debug_strategy(strategy, session_id=session_id, good=good, bad=bad, times=times,
                                     prepare=prepare, trace=trace, overlay=overlay))

    @register("grep_context")
    def grep_context(
        pattern: Annotated[str, Field(description="The Grep pattern.")] = "",
        path: Annotated[OptStr, Field(description="The Grep's path argument.")] = None,
        command: Annotated[OptStr, Field(description="Instead: a shell command; its grep/rg patterns.")] = None,
    ) -> dict[str, Any]:
        return emit(t.grep_context(pattern, path, command))

    @register("read_context")
    def read_context(
        file_path: Annotated[str, Field(description="The file the agent read or edited.")],
    ) -> dict[str, Any]:
        return emit(t.read_context(file_path))

    if slim:
        # the core menu's analyze and code_check: the arguments a question or an edit needs (the full profile, and
        # the CLI, keep the rest: plans, test runs, tracing, budgets, environments)
        def analyze_core(
            question: Annotated[str, Field(description="The question.")],
            budget_seconds: Annotated[float, Field(description="Wall-time budget in seconds (1-600).")] = 60,
        ) -> dict[str, Any]:
            return emit(t.analyze(question, budget_seconds=budget_seconds))

        def code_check_core(
            paths: Annotated[list[str] | None, Field(description="Repository-relative files or directories.")]
            = None,
            diff: Annotated[OptStr, Field(description="A revision (e.g. 'HEAD'): only lines changed against it.")]
            = None,
            snippet: Annotated[OptStr, Field(description="Code not written yet, checked as if it were in as_path.")]
            = None,
            as_path: Annotated[OptStr, Field(description="With snippet: the file it is for.")] = None,
            deps: Annotated[bool, Field(description="Instead: the manifests' dependencies vs the imports.")] = False,
        ) -> dict[str, Any]:
            return emit(t.code_check(paths=paths, diff=diff, snippet=snippet, as_path=as_path, deps=deps))

        add("analyze", analyze_core, DESCRIPTIONS["analyze"])
        add("code_check", code_check_core, DESCRIPTIONS["code_check"])
    behind = [n for n in (*CORE_TOOLS, "grep_context", "read_context") if n in impl and n not in listed] \
        if GATEWAY in listed else []
    if behind:
        import inspect

        from pydantic import ValidationError, validate_call

        def run_tool(
            name: Annotated[Literal[tuple(behind)], Field(description="The tool.")],  # type: ignore[valid-type]
            arguments: Annotated[dict[str, Any], Field(description="Its arguments, e.g. {\"name\": \"Cls.method\"}.")]
            = {},  # noqa: B006 - never mutated
        ) -> dict[str, Any]:
            fn = impl[name]
            try:
                return validate_call(fn)(**(arguments or {}))  # the listed tools' own argument checks
            except (ValidationError, TypeError) as exc:
                params = ", ".join(inspect.signature(fn).parameters)
                what = ("; ".join(f"{'.'.join(map(str, e['loc'])) or 'arguments'}: {e['msg']}" for e in exc.errors()[:4])
                        if isinstance(exc, ValidationError) else str(exc))
                return emit({"error": "invalid_arguments", "tool": GATEWAY, "message": f"{name}: {what}"[:300],
                             "hint": f"{name}({params})"})

        add(GATEWAY, run_tool, gateway_description(behind))

    _add_prompts(srv, listed, served)
    _compact_schemas(srv)
    return srv


def _invalid_params(message: str) -> Exception:
    """A JSON-RPC invalid-params error the SDK passes to the client with its message (not 'Error rendering')."""
    try:
        from mcp.shared.exceptions import MCPError  # mcp >= 2
        return MCPError(-32602, message)
    except ImportError:  # pragma: no cover - mcp 1.x
        try:
            from mcp.shared.exceptions import McpError
            from mcp.types import ErrorData
            return McpError(ErrorData(code=-32602, message=message))
        except ImportError:
            return ValueError(message)


def _add_prompts(srv, listed: set[str], served: set[str]) -> None:
    """The ready workflows (:mod:`verinoda.mcp.prompts`) as MCP prompts, worded for this profile's menu."""
    from pydantic import Field

    from verinoda.mcp import prompts as P

    def text(name: str, **args) -> str:
        given = {k: v for k, v in args.items() if v is not None}
        problems = P.argument_problems(name, given)
        if problems:  # the same check as `verinoda mcp prompts`, sent as invalid params rather than a render error
            raise _invalid_params("; ".join(problems))
        return P.render(name, given, listed=listed, served=served, gateway=GATEWAY)

    def arg(prompt: str, name: str):
        return Field(description=next(d for a, d, _ in P.ARGUMENTS[prompt] if a == name))

    def review(base: Annotated[str | None, arg("review", "base")] = None) -> str:
        return text("review", base=base)

    def onboarding(topic: Annotated[str | None, arg("onboarding", "topic")] = None) -> str:
        return text("onboarding", topic=topic)

    def debug(symptom: Annotated[str, arg("debug", "symptom")],
              repro: Annotated[str | None, arg("debug", "repro")] = None) -> str:
        return text("debug", symptom=symptom, repro=repro)

    def pre_merge(base: Annotated[str | None, arg("pre_merge", "base")] = None) -> str:
        return text("pre_merge", base=base)

    for fn in (review, onboarding, debug, pre_merge):
        try:
            srv.prompt(name=fn.__name__, description=P.DESCRIPTIONS[fn.__name__])(fn)
        except (AttributeError, TypeError):  # pragma: no cover - an SDK without prompts: the tools still serve
            return


def _win_rebind_std_handle(fd: int) -> None:
    """Point the Win32 std-handle slot of fd 0/1 at the fd's current OS handle.

    ``os.dup2`` only updates the CRT table; ``subprocess`` inherits from the
    Win32 slot, so it must be repointed as well.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        import msvcrt
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.SetStdHandle.argtypes = [wintypes.DWORD, wintypes.HANDLE]
        k32.SetStdHandle.restype = wintypes.BOOL
        slot = {0: -10, 1: -11}[fd] & 0xFFFFFFFF  # STD_INPUT_HANDLE / STD_OUTPUT_HANDLE
        k32.SetStdHandle(slot, msvcrt.get_osfhandle(fd))
    except Exception:  # pragma: no cover - best effort
        pass


def _move_protocol_off_std_fds() -> None:
    """mcp 1.x only (2.x does this itself): serve JSON-RPC on private descriptors.

    Otherwise every child process (git, test runs) inherits the protocol pipes.
    On Windows a child that inherits the stdin pipe blocks while the server
    has a read pending on it, stalling each git call until its timeout. After
    this, fd 0 reads the null device, fd 1 writes to stderr, and the SDK's
    ``stdio_server()`` picks the private streams up from ``sys.stdin/stdout``.
    """
    import io

    try:
        sys.stdout.flush()
        in_fd, out_fd = os.dup(0), os.dup(1)  # non-inheritable duplicates of the wire
    except (OSError, ValueError, AttributeError):
        return  # no usable std streams: serve on whatever the SDK finds
    try:
        null = os.open(os.devnull, os.O_RDONLY)
        os.dup2(null, 0)
        os.close(null)
        os.dup2(2, 1)
    except OSError:
        os.dup2(in_fd, 0)
        os.dup2(out_fd, 1)
        os.close(in_fd)
        os.close(out_fd)
        return
    _win_rebind_std_handle(0)
    _win_rebind_std_handle(1)
    sys.stdin = io.TextIOWrapper(io.open(in_fd, "rb"), encoding="utf-8", errors="replace")
    sys.stdout = io.TextIOWrapper(io.open(out_fd, "wb"), encoding="utf-8", write_through=True)


def default_repo(start: Path) -> Path:
    """The project a ``verinoda mcp serve`` without ``--repo`` serves when started in ``start``.

    A user-scope server is started in the folder the agent session was opened in. That is
    normally the project (or a folder inside it: the nearest ``.git`` / initialised
    ``.verinoda`` above wins, as for the CLI). When the session was opened one level
    *above* the project - a workspace folder holding it - and exactly one direct
    sub-folder is an initialised Verinoda project, that one is served; with several, the
    start folder is kept and the log names them (pass ``--repo``).
    """
    from verinoda.paths import find_repo_root

    root = find_repo_root(Path(start))
    if (root / ".git").exists() or (atlas_dir(root) / "atlas.db").is_file():
        return root
    try:
        subs = sorted(d for d in root.iterdir()
                      if d.is_dir() and not d.name.startswith(".") and (atlas_dir(d) / "atlas.db").is_file())
    except OSError:
        subs = []
    if len(subs) == 1:
        print(f"verinoda mcp: {root} is not a Verinoda project; serving its only initialised sub-folder "
              f"{subs[0].name} (pass --repo to choose)", file=sys.stderr, flush=True)
        return subs[0]
    if subs:
        print(f"verinoda mcp: {root} is not a Verinoda project and holds several: "
              f"{', '.join(d.name for d in subs)}; pass --repo to choose one", file=sys.stderr, flush=True)
    return root


def _registers_repo_of(path: Path) -> bool:
    """``path`` (an agent's MCP config) holds a server entry started with ``--repo-of``."""
    try:
        text = path.read_text(encoding="utf-8-sig")
        if path.suffix == ".toml":
            try:
                import tomllib
            except ModuleNotFoundError:  # Python 3.10
                import tomli as tomllib
            servers = tomllib.loads(text).get("mcp_servers")
        else:
            servers = json.loads(text).get("mcpServers")
    except (OSError, UnicodeDecodeError, ValueError, AttributeError):
        return False
    return isinstance(servers, dict) and any(
        isinstance(e, dict) and "--repo-of" in [str(a) for a in (e.get("args") or [])] for e in servers.values())


def repo_of_config(start: Path, rel: str) -> Path:
    """The project a ``verinoda mcp serve --repo-of REL`` serves when started in ``start``.

    Project-scope agent configs carry no absolute project path, so moving the project keeps them
    working: Claude Code reads ``.mcp.json`` from the session folder and every folder above it and
    starts servers in the session folder, so the project is the nearest folder at or above ``start``
    whose ``REL`` registers a server with ``--repo-of``. The home folder and the folders above it are
    never chosen; when nothing matches, the ``default_repo`` rules apply and the log says so.
    """
    from verinoda.paths import _is_home_or_above

    parts = Path(rel).parts
    if not rel or Path(rel).is_absolute() or Path(rel).drive or ".." in parts:
        raise SystemExit(f"error: --repo-of takes a path relative to the project folder, such as .mcp.json "
                         f"(got {rel!r})")
    here = Path(start).resolve()
    for p in (here, *here.parents):
        if _is_home_or_above(p):
            break
        f = p / rel
        if f.is_file() and _registers_repo_of(f):
            return p
    print(f"verinoda mcp: no {rel} registering this server at or above {here}; using the nearest project "
          "folder instead", file=sys.stderr, flush=True)
    return default_repo(here)


def serve(repo: Path, profile: str | None = None, *, watch: bool = False) -> None:
    """Serve the Verinoda tools for ``repo`` over stdio (``verinoda mcp serve [--profile core|full] [--watch]``).

    ``watch``: also run a fast ``update`` when the project's files change (:class:`verinoda.fswatch.Watcher`,
    woken by file events), so the next tool call answers from the edited code without ``index_update``."""
    repo = Path(repo).resolve()
    try:
        srv = build_server(repo, profile=profile)
        major = _load_sdk()[1]
    except (RuntimeError, ValueError) as exc:  # SDK missing or shadowed, unknown profile: a message
        raise SystemExit(f"error: {exc}") from None
    if major < 2:
        _move_protocol_off_std_fds()
    if graph_path(repo).exists():
        state = "indexed"
    elif atlas_dir(repo).is_dir():
        state = "initialised, not indexed - index_update builds the index"
    else:
        state = "not scanned - index_update scans it"
    served = getattr(srv, "verinoda_profile", DEFAULT_PROFILE)
    from verinoda import buildinfo

    print(f"verinoda mcp: serving {repo} over stdio ({state}; {len(listed_tools(repo, served))} tools, "
          f"profile {served}); "
          f"build {buildinfo.server_version()} ({sys.executable})", file=sys.stderr, flush=True)
    from verinoda.paths import ignored_settings_note

    note = ignored_settings_note(repo)
    if note:
        print(f"verinoda mcp: {note}", file=sys.stderr, flush=True)
    watcher = None
    if watch and graph_path(repo).exists():
        from verinoda.fswatch import Watcher

        watcher = Watcher(repo, purpose="mcp serve --watch", fast=True)
        watcher.start()
    elif watch:
        print("verinoda mcp: --watch needs an index: run index_update, then restart the server to watch",
              file=sys.stderr, flush=True)
    try:
        srv.run("stdio")
    except KeyboardInterrupt:  # pragma: no cover - interactive stop
        pass
    finally:
        if watcher is not None:
            watcher.stop.set()
