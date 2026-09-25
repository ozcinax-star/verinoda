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
  ``.verinoda/``) yields ``error="not_initialised"``; the server never creates
  ``.verinoda/`` itself. Argument *type* violations are rejected by the SDK
  before a tool runs (plain-text ``isError`` result).
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
import sys
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
    "map_view",
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
    "decision_brief",
    "experiment_run",
    "debug_start",
    "debug_attempt",
    "debug_status",
    "debug_strategy",
)

MAX_RESPONSE_CHARS = int(os.environ.get("VERINODA_MCP_MAX_CHARS", "12000"))
QUERY_MAX_CHARS = 6000          # same default as `verinoda query --max-chars`
QUERY_MEMO_SIZE = 64            # project_query answers kept, re-validated by stat on every hit
EXCERPT_MAX_LINES = 30
EDGE_CAP = 25
LIST_CAP = 50
CODE_CHECK_BUDGET_S = float(os.environ.get("VERINODA_MCP_CHECK_BUDGET_S", "90"))   # code_check holds the server
VIEWS = ("hierarchy", "dependencies", "dataflow", "config", "tests", "history", "impact")
VERDICTS = ("confirmed", "qualified", "corrected", "unresolved")
RESEARCH_KINDS = ("auto", "official_doc", "standard", "paper", "secondary", "reference_repo")
NETWORK_MODES = ("off", "cache", "on")
QUERY_FORMATS = ("text", "json")
DECISION_ACTIONS = ("list", "record", "import", "guard", "accept", "waive", "answer")
# what changes what is enforced, and the user's answers to a brief: the user's own words
DECISION_NEEDS_USER = ("record", "guard", "accept", "waive", "answer")
DEBUG_KINDS = ("fix", "probe", "rerun", "differential")
DEBUG_STRATEGIES = ("differential", "bisect", "rerun", "observe")
# analyze: what the agent reads first and what is cut last (the interpretation and the per-sub-question verdicts)
ANALYZE_KEEP = ("understood_as", "subquestions", "plan_check")
ANALYZE_FIRST_CUT = ("steps", "critique", "passages")  # passages: `query` gives them in full; claims come first
PLAN_HINT = "call question_plan_draft, or see `verinoda plan schema` for the plan format"
# A file modified this close to (or after) the start of a call may have changed while it was read, or
# within the file system's timestamp granularity: what was derived from it is not kept (git's racy-clean rule).
RACY_NS = 2_000_000_000
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
                                     "fix": "pass the plan returned by question_plan_draft, edited, as JSON text"}])
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


def cap_response(obj: dict, limit: int = MAX_RESPONSE_CHARS, *, first: tuple[str, ...] = (),
                 keep_tail: tuple[str, ...] = ("history",), keep: tuple[str, ...] = ()) -> dict:
    """Cut ``obj`` until its compact JSON (truncation note included) fits ``limit`` chars.

    Top-level lists named in ``first`` are shortened before anything else.
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
    if _size(obj) <= limit:
        return obj
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
        while isinstance(obj.get(key), list) and len(obj[key]) > 1 and (over := excess()) > 0:
            cut_seq((key,), obj[key], _size(obj[key]), over)

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
        self._query_memo: OrderedDict[tuple, tuple[tuple, dict]] = OrderedDict()
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
                f"run `verinoda scan {self.repo}` in a terminal, then call this tool again",
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
        from verinoda import index

        gp = graph_path(self.repo)
        key = (str(gp), *(_file_stat(gp) or (None,)), _file_stat(receiver_calls_path(self.repo)))
        if key[1] is None:
            raise FileNotFoundError(f"no graph at {gp}; run `verinoda scan {self.repo}` first")
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
            from verinoda import retrieval

            q = _text(question, "question")
            n = _clamp(max_items, 1, 25, "max_items")
            fmt = _choice(format, QUERY_FORMATS, "format")
            g = self._graph()
            key = (q, n, fmt)
            hit = self._query_memo.get(key)
            if hit is not None and hit[0] == self._query_deps(g, tuple(f for f, _ in hit[0][-1])):
                self._query_memo.move_to_end(key)
                self.cache_stats["query_memo_hits"] += 1
                return hit[1]
            res = retrieval.retrieve(g, q, retrieval.Budget(max_items=n, max_chars=QUERY_MAX_CHARS))
            if fmt == "json":
                out = _jsonable(res)
            else:
                # the escaped JSON string must still fit the response cap
                chars = min(QUERY_MAX_CHARS, max(800, int((self.max_chars - 200) * 0.85)))
                out = {"format": "text", "question": q, "text": retrieval.render_text(res, budget_chars=chars)}
            rd = getattr(res, "render", None)
            if rd is not None:  # the same answer is recomputed only when one of its inputs changed on disk
                files = tuple(sorted({h.file for h in rd.ranking.hits} | {i["file"] for i in res.get("items", [])}))
                deps = self._query_deps(g, files)
                if not any(self._racy(st) for st in _deps_stats(deps)):
                    self._query_memo[key] = (deps, out)
                    while len(self._query_memo) > QUERY_MEMO_SIZE:
                        self._query_memo.popitem(last=False)
            return out
        return self._run("project_query", go, need="graph")

    def node_inspect(self, name: str) -> dict:
        def go():
            g = self._graph()
            q = _text(name, "name")
            nid, cands = g.resolve(q)
            if nid is None:
                raise ToolFailure("not_found", f"no graph node matches {q!r}",
                                  "call project_query to find candidates, then pass a node id, "
                                  "a label, 'path/file.py::symbol' or 'Class.method'")
            how = _resolution(g, q, nid)
            label = g.label(nid)
            node = {"id": nid, "label": label, "kind": _node_kind(g, nid), "file": g.file(nid),
                    "line": g.line(nid), "file_type": g.G.nodes[nid].get("file_type"),
                    "resolution": how or "scored label match (heuristic) - check candidates"}
            out: dict = {"query": q, "node": node}
            if how is None:
                out["candidates"] = [{"id": c, "label": g.label(c), "at": _node_at(g, c), "score": round(float(s), 3)}
                                     for s, c in cands[:5]]
            same = sorted(n for n in g.G.nodes if n != nid and g.label(n).strip(".()") == label.strip(".()"))
            if same:
                out["same_label"] = [{"id": n, "at": _node_at(g, n)} for n in same[:5]]
            src = g.source(nid, max_lines=EXCERPT_MAX_LINES)
            if src:
                span = g.span(nid)
                out["excerpt"] = {"lines": [src[0], src[1]], "span": list(span) if span else None,
                                  "truncated": bool(span and span[1] > src[1]), "text": src[2]}
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
                    rs.append(r)
                rs.sort(key=lambda r: (str(r["relation"]), r["at"] or "", r[other_key]))
                return rs, rel_count

            outs, out_rel = rows(g.out_edges(nid), "to")
            ins, in_rel = rows(g.in_edges(nid), "from")
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
            # the core result is returned whole: hints (unresolved endpoints), reachability and note (mode='any')
            res = retrieval.trace(self._graph(), s, t, mode=mode)
            st = res.get("status")
            if st == "unresolved":
                res.setdefault("next_step", "an endpoint did not resolve: see 'hints'/'candidates', or call "
                                            "node_inspect / project_query and pass 'path/file.py::symbol'")
            elif st and st.startswith("ambiguous"):
                res.setdefault("next_step", "both names resolved to one node: disambiguate with "
                                            "'path/file.py::symbol'")
            elif st == "no directed path":
                res.setdefault("next_step", ("retry with mode='any' for structural relations; " if mode == "flow"
                                             else "")
                               + "no path in the static graph does not prove there is none at runtime "
                                 "(dynamic dispatch, callbacks and DI are not resolved; runtime_observe records "
                                 "what the tests actually call)")
            return res
        return self._run("relation_trace", go, need="graph")

    def map_view(self, view: str, targets: list[str] | None = None) -> dict:
        def go():
            from verinoda import architecture_map as am

            if view not in VIEWS:
                raise ToolFailure("invalid_argument", f"unknown view {view!r}",
                                  "choose one of: " + ", ".join(VIEWS), valid=list(VIEWS))
            g = self._graph()
            tg = [str(t).strip() for t in ([targets] if isinstance(targets, str) else targets or []) if str(t).strip()]
            if view == "impact":
                source = "argument"
                if not tg:
                    tg = am.changed_files_from_git(self.repo)
                    source = "git working-tree changes (HEAD + untracked)"
                res = am.impact(g, tg)
                res["targets_source"] = source
                if not tg:
                    res["note"] = "no targets given and git reports no changed files: nothing to analyse"
                return res
            res = am.VIEWS[view](g)
            if tg:
                res["note"] = f"targets are only used by the impact view; ignored for {view}"
            return res
        return self._run("map_view", go, need="graph")

    # -- question understanding -----------------------------------------------------
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
                budget_calls: int = 40, plan_json: str | None = None, observe: bool = False) -> dict:
        def go():
            from verinoda import analysis

            q = _opt_text(question) or ""
            plan = _plan_text(plan_json)
            if not q and plan is None:
                raise ToolFailure("invalid_argument", "question must be a non-empty string (or pass plan_json)",
                                  "pass the user's question, or a checked plan as plan_json")
            b = analysis.Budget(seconds=_clamp(budget_seconds, 1, 600, "budget_seconds", float),
                                tool_calls=_clamp(budget_calls, 1, 200, "budget_calls"),
                                context_tokens=6000)  # same default as `verinoda analyze`
            with self._store() as st:
                g = self._graph_for_analysis(st)
                res = analysis.analyze(st, self.repo, q, plan=plan, budget=b, run_tests=bool(run_tests),
                                       challenge=True, graph=g, observe=bool(observe))
            if res.get("status") == "invalid_plan":
                problems = res.get("errors") or []
                raise ToolFailure("invalid_plan", f"the plan failed validation ({len(problems)} problem(s)); "
                                                  "nothing was analysed", PLAN_HINT, problems=problems,
                                  analysis_id=res.get("analysis_id"), plan_id=res.get("plan_id"))
            return res
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
                                     + ("re-fetch the URL with reference_research to compare"
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
                   as_path: str | None = None, env: str | None = None, include_exists: bool = False) -> dict:
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
            return codecheck.check(self.repo, ps, diff=_opt_text(diff), snippet=code, as_path=ap,
                                   env=_opt_text(env) or "auto", include_exists=bool(include_exists),
                                   budget_s=CODE_CHECK_BUDGET_S, trust_env=False)
        return self._run("code_check", go, first=("sites",),
                         keep=("status", "summary", "exit", "exit_because", "incomplete", "not_checked", "env"))

    def api_members(self, target: str, env: str | None = None, private: bool = False) -> dict:
        def go():
            codecheck = self._optional("verinoda.codecheck")
            return codecheck.api(self.repo, _text(target, "target"), env=_opt_text(env) or "auto",
                                 private=bool(private), trust_env=False)
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
                    if act == "record":
                        g = self._graph() if governs and graph_path(self.repo).exists() else None
                        return dm.record(st, self.repo, chosen=_text(chosen, "chosen"),
                                         rationale=_text(rationale, "rationale"), title=_opt_text(title),
                                         brief_id=_opt_text(brief_id), guards=_str_list(guards, "guards"),
                                         governs=_str_list(governs, "governs"),
                                         revisit_when=_str_list(revisit_when, "revisit_when"),
                                         supersedes=_opt_text(supersedes), user_statement=said, graph=g)
                    if act == "import":
                        g = self._graph() if graph_path(self.repo).exists() else None
                        return dm.import_doc(st, self.repo, _text(document, "document"), graph=g, user_statement=said)
                    did = _text(decision_id, "decision_id")
                    if act == "guard":
                        return dm.add_guards(st, self.repo, did, _str_list(guards, "guards"), user_statement=said)
                    if act == "accept":
                        return dm.accept(st, self.repo, did, _str_list(guard_ids, "guard_ids"), user_statement=said)
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
            note, graph = None, None
            if any(d.enforced and g.get("kind") == "no_edge" and g.get("status") == "accepted"
                   for d in recs for g in d.guards):
                with self._store() as st:
                    graph = self._graph_for_analysis(st)
                    if graph is None and refresh:
                        from verinoda import workflow

                        up = workflow.update(st, self.repo)
                        note = (f"refreshed first ({up.get('mode')}, {up.get('changed_count') or 0} changed "
                                "file(s))") if not up.get("error") else f"could not be refreshed: {up['error']}"
                if graph is None and graph_path(self.repo).exists():
                    graph = self._graph()
            try:
                res = guards.check(self.repo, graph=graph, base=b, changed_only=bool(changed_only), records=recs)
            except ValueError as exc:
                raise ToolFailure("invalid_argument", str(exc)[:600], "base is a git revision such as HEAD~1 or "
                                                                       "origin/main") from None
            if note:
                res["index"] = note
            return res
        return self._run("decision_check", go, keep=("status", "exit", "violations", "next_step"))

    # -- index ----------------------------------------------------------------------
    def index_update(self) -> dict:
        def go():
            from verinoda import workflow

            with self._store() as st:
                return workflow.update(st, self.repo)
        return self._run("index_update", go)

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

    def _debug(self, tool: str, fn: Callable, keep: tuple[str, ...] = ()) -> dict:
        def go():
            from verinoda import debug, experiments

            with self._store() as st:
                try:
                    return debug.compact(fn(debug, st))
                except experiments.ExperimentRefused as exc:
                    out = experiments.refusal(self.repo, exc)
                    out["next_step"] = ("run the command yourself and record it with debug_attempt(observed_output="
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
        return "the command is outside the process-isolation allowlist; enable docker/podman or run it manually"
    return (f"see the server log (stderr); `verinoda doctor --repo {repo}` checks the installation "
            "and index")


# -- MCP registration -------------------------------------------------------------

INSTRUCTIONS = """Verinoda: evidence-first analysis of the repository {repo}.

Understand the question first (Turkish or English):
1. question_plan_draft(question) -> a plan. Edit it: split compound questions, gloss domain words in English,
   copy versions exactly as the user wrote them, never invent candidates.
2. question_plan_check(plan_json) -> ready | needs_clarification (ask the user the listed clarifications, add
   answers[] with each clarification_id, check again) | invalid (an error listing the problems).
3. analyze(plan_json=...) (or analyze(question) for a quick answer). Start the answer with
   "Understood as / Anladığım: ..." (understood_as), then one block per sub-question.
References the user gives (links, repos, packages, versions, commits, PR/issue numbers, papers, docs): call
reference_resolve first. Report each as <name> @ <pin> (basis: <basis>) with its mismatches; never substitute
the default branch for a named version; ask only questions_for_user; state unresolved ones with their next step.
reference_research(resolution_id, reference_id) inspects one at exactly its pin.

Tools:
- project_query: where is X / what handles Y - plain text, skeleton first (format='json' for programs).
- node_inspect / relation_trace: one symbol's definition and edges; call paths between two symbols.
- map_view: architecture views (hierarchy, dependencies, dataflow, config, tests, history, impact).
- analyze / plan_audit: answer as recorded claims with evidence, critique and unknowns; re-judge it later.
- lexicon_show: which code words the repository associates with a natural-language word.
- claim_inspect / claim_list / evidence_inspect / claim_verify / claim_challenge: audit claims.
- resolve_call: which definition a call on path:line binds to (only 'definitive' answers verify).
- code_check (Python only): after editing Python code, and before proposing it, check that the modules, names,
  keyword arguments and dict keys it uses exist in the project's environment; fix every absent site
  (nearest/elsewhere), treat unknown as unverified; other languages come back not_checked, never checked.
  api_members: the real members of a Python module or class before you write calls to it.
- runtime_observe: run selected tests under the call tracer; which tests reach which symbols.
- reference_resolve / reference_research / reference_compare: pinned external references.
- feedback_submit / feedback_process / feedback_resolve: record critique as a hypothesis, verify, resolve.
- index_update: re-index after editing files (marks affected claims stale).
- decision_record: what the user decided (records, guards, waivers). A choice between options is the user's:
  never record, accept or waive without their words (user_statement).
- decision_check: the code against every accepted guard (VIOLATED / POSSIBLE / REVIEW / TRIGGER, ok with its
  limits). Call it (changed_only=true) before finishing a code change; on VIOLATED fix the code or ask the user.
- decision_brief: for a should/which/scale question: forces from the code with evidence, what is absent, decisions
  on record, options and questions_for_human. No recommendation: ask the user those questions, record each answer
  (decision_record action='answer'), and never pick an option for the user.
- experiment_run: run one allowlisted command (tests) in a throw-away copy (of the working tree or a commit, ref).
- debug_start / debug_attempt / debug_status / debug_strategy: the debug ledger. Before the first edit of a bug fix,
  debug_start(symptom, command); after every edit, debug_attempt(hypothesis). When the answer says stop=true, stop
  editing, run strategies[0] with debug_strategy and show the user debug_status. Never change a test's expected
  value without asking the user (questions_for_human). Never say "fixed": say the repro passed at tree T in run R.

Rules: graph edges (EXTRACTED/INFERRED) are extractions, never verification. Claim status is one of
observed, experiment_verified, statically_verified, primary_source_verified, strong_inference,
weak_inference, unknown, contradicted, stale; heuristic results say so in 'coverage'/'uncertainties'.
When evidence is missing the answer is 'unknown' plus a next step - do not fill the gap by guessing.
Responses are small; "truncated": true means lists were cut (see 'truncation').
Errors come back as {{"error", "message", "hint"}}; not_initialised/no_index means the user must run
`verinoda scan {repo}` first."""

DESCRIPTIONS: dict[str, str] = {
    "project_query": (
        "Bounded, justified retrieval for a question: the best code locations (symbol, file, start-end lines) "
        "ranked by a persistent passage index (BM25F + graph prior). format='text' (default) is plain text, "
        "skeleton first: path:lines headers with signatures, call outlines and the matching lines, packed to "
        "6000 chars with any truncation stated; format='json' returns items with reasons, excerpts and the "
        "relations among them. Read-only. Start here for 'where is X / what handles Y'. Hits are retrieval "
        "results, not verified claims."),
    "node_inspect": (
        "Inspect one graph node. name may be a node id, a label ('place_order'), 'path/file.py::symbol', "
        "'Class.method' or a file path. Returns the resolved node and how it was resolved (a 'scored' "
        "resolution is a heuristic label match - check 'candidates'/'same_label'), its location, the "
        "source excerpt of its definition (at most 30 lines) and its outgoing/incoming edges with relation, "
        "confidence and file:line (at most 25 each, totals given). Edges are extractions, not verification."),
    "relation_trace": (
        "Directed paths (up to 3, at most 8 hops) from source to target; every hop has relation, kind, "
        "confidence (EXTRACTED/INFERRED) and call-site file:line. mode='flow' follows calls only "
        "(control flow, plus construction -> __init__); mode='any' also follows uses/imports/inherits/"
        "method/references and says whether a path is execution or only structural reachability "
        "('reachability', 'note'). status: found | unresolved (with 'hints': likely symbols) | no directed "
        "path | ambiguous. No path in the static graph does not prove there is none at runtime."),
    "map_view": (
        "One top-down architecture view. view: hierarchy (subsystems/packages/files), dependencies "
        "(file-level call/import edges), dataflow (entry points -> persistence sinks), config (env vars "
        "and config files), tests (static test reachability), history (git log + decision records), "
        "impact (reverse dependents of targets). 'coverage' states the method and its limits; heuristics "
        "are labelled. For impact pass targets (files or symbols); without targets the git working-tree "
        "changes are used. Large views are truncated."),
    "question_plan_draft": (
        "Draft a question plan (verinoda.question_plan/1) from the user's message with deterministic "
        "Turkish/English rules: sub-questions with intent and a checkable done_when, mentions (the user's "
        "words that name code) with candidate names, references with the version as written. Every element "
        "is tagged derived_by. Nothing is stored. Edit it, then pass it to question_plan_check / analyze."),
    "question_plan_check": (
        "Validate and ground a plan given as JSON text: schema, unique ids and a sub-question DAG, every "
        "mention/reference quoted verbatim from the message, every version token carried, mentions linked "
        "to graph nodes in evidence tiers (linked / ambiguous / weak / unlinked; host candidates that match "
        "nothing are rejected; a name written as code that the repository spells nowhere is not_found, with "
        "did_you_mean, never replaced by a similar name). The plan and its check are stored (plan_id). status: ready | "
        "needs_clarification (ask the user the clarifications, record answers[] with their clarification_id) "
        "| invalid (returned as an error with the problems)."),
    "analyze": (
        "Answer a question as claims with evidence. The question (or a checked plan_json) is turned into a "
        "stored plan with sub-questions; each is answered by retrieving code, re-checking cited lines, "
        "git history/decision records for 'why', optionally running (run_tests) or tracing (observe) the "
        "tests that reach the answer in an isolated copy. Every conclusion is a claim (status, confidence, "
        "evidence, uncertainties), challenged by critique; unknowns carry the next verification step. "
        "Returns understood_as, per-sub-question verdicts, claims and unknowns. needs_clarification is a "
        "normal result (ask the user); an invalid plan is an error. Re-indexes first if the working tree "
        "changed. Bounded by budget_seconds and budget_calls."),
    "plan_audit": (
        "Re-judge an earlier analysis' sub-questions against their done_when on the current claim "
        "statuses. refresh=true (default) re-indexes first, so claims whose files changed are marked stale; "
        "a sub-question relying on a stale claim is reported 'stale'. 'changed' lists the sub-questions "
        "whose verdict moved since the analysis."),
    "lexicon_show": (
        "Audit view of one natural-language word (Turkish or English): the identifier parts this repository "
        "associates with it (log-likelihood association with file:line sites), grounded seed-dictionary "
        "glosses and text sites. These widen search and plan linking only; they are never evidence. Read-only."),
    "claim_inspect": (
        "Full record of one claim: text, status, confidence, kind, the snapshot/commit it is pinned to, "
        "supporting/refuting/qualifying evidence (id, type, file:line or URL, hash, grade, current state) and "
        "its complete status history (newest kept if truncated). Read-only."),
    "claim_list": (
        "List recorded claims, newest first (id, status, confidence, kind, text), optionally filtered by "
        "status. Use it to find claim ids from earlier sessions. Read-only."),
    "evidence_inspect": (
        "One evidence record (source type, locator, commit, content hash, excerpt, metadata), whether that "
        "source type can verify a claim, the claims citing it, and a live re-check: file/line evidence is "
        "re-read now (from meta.root for reference checkouts) and compared with the recorded hash; "
        "recheck.status is same | moved (with the new lines) | changed | gone | ambiguous, and 'candidate' "
        "says where changed anchored lines probably are now (never counted as unchanged). Read-only."),
    "claim_verify": (
        "Re-check a claim's cited source lines against the current working tree and re-assess its status: "
        "unchanged evidence keeps (or restores up to its assessed ceiling) the status, changed evidence "
        "makes it stale. run=true also re-runs the claim's recorded test experiment in an isolated copy. "
        "Returns before/after status and every source check."),
    "claim_challenge": (
        "Adversarial check of a claim: is there verifying support, do cited lines still match, is support "
        "only graph edges, does the call-site line name the target, is the target name ambiguous, does an "
        "'only X does Y' pattern occur elsewhere, did the files change. Failures attach refuting evidence; "
        "confidence only goes down. Returns findings, alternatives and before/after status."),
    "resolve_call": (
        "Precise resolution of one call site: which definition does the call to target on path:line bind "
        "to (jedi for Python; a fresh index.scip for other languages; cached by file hash)? kind: definitive "
        "| dynamic | ambiguous | external | unresolved, with the target definitions. With target_path/"
        "target_line a verdict confirms | refutes | undetermined (definitive answers only). 'no precise "
        "answer' with the reason when the resolver is unavailable or the site cannot be read. Read-only."),
    "code_check": (
        "Python only (files in other languages come back under not_checked, status unsupported_language, exit 3 - "
        "never as checked). Check that the modules, imported names, attributes, keyword arguments and constant dict "
        "keys that Python code uses exist - in the project's own environment (.venv/venv/env, or env=PATH; 'none' = standard "
        "library only; env=PATH only a virtual environment whose base interpreter is a known Python "
        "installation outside the project). Input: paths (files/directories), or diff (a revision: only sites "
        "on changed lines plus new files; default when nothing is given: changes against HEAD), or snippet + "
        "as_path (code not written yet). Each site gets exists | absent | unknown | not_installed | guarded; "
        "absent is given only "
        "for closed containers (a module or class whose names are all known, a direct instance, one known "
        "signature) and comes with nearest real names and where the name is defined elsewhere; unknown carries "
        "why. env names the interpreter and package versions checked (and lock mismatches). exit 3 = something "
        "is absent, or an installed package version differs from the lock (exit_because says which). Files are "
        "started only within a time budget (90 s): 'incomplete' lists what was not checked - pass fewer paths "
        "or use diff. Existence and signature shape only, not behaviour. Read-only (answers are cached under "
        ".verinoda/cache/check)."),
    "api_members": (
        "Python only. The real members of a module, class or function (dotted target, e.g. 'packaging.specifiers.SpecifierSet' "
        "or 'orders.service') in the project's environment: name, kind, signature, file:line, inherited-from, "
        "and the version the source came from. Use it before writing calls to an API you have not read. "
        "private=true also lists names starting with '_'. found=false (exit 3: missing from a module or class "
        "whose names are all known, or no such module) comes with nearest names; found=null with decided="
        "'unknown' or 'not_installed' (exit 0) was not decided - treat it as unverified. Read-only."),
    "runtime_observe": (
        "Run tests in an isolated copy under the sys.monitoring call tracer and record the observed calls "
        "(stored under run_id). Tests: test_ids (pytest node ids), else tests selected for symbols/terms "
        "(static reachability, then test names), else the whole suite. Returns the same compact summary as "
        "`verinoda observe`: complete (+ breach/degraded_after), target_reach per symbol (tests that reached "
        "it; 'observed' says whether absence is meaningful), test outcomes, boundary calls in the targets' "
        "files, limits and cost - never the edge list. Observations are run-scoped, never 'always'; edges "
        "seen only through test doubles never support production edges."),
    "reference_resolve": (
        "Resolve every reference in a message - links, owner/repo, packages with versions, commits, PR/issue "
        "numbers, papers, docs - to the exact version the user meant (pin + basis); a named version is never "
        "replaced by the default branch. Returns one line per reference with mismatches (M1-M12), unresolved "
        "parts with next steps, unbound mentions and questions_for_user. references adds explicit "
        "'URL[@ref]' items; network: off (cached answers only) | cache (cached first, the network on a miss) "
        "| on, default from the project config (research.network, else cache); local_intent marks a question "
        "about this project's own behaviour. The resolution is recorded (id)."),
    "reference_research": (
        "Inspect a reference: a git repository (URL or local path, pinned to ref or the default branch "
        "HEAD) or a document URL, optionally tracing a topic/mechanism in it; or, with resolution_id + "
        "reference_id from reference_resolve, exactly that reference at its resolved pin. The research is "
        "recorded with its pinned commit/hash as evidence. May use the network (git clone / HTTP) and write "
        "under .verinoda/research."),
    "reference_compare": (
        "Compare a mechanism's assumptions (topic) between this repository and a reference repository or "
        "document pinned at ref; findings are recorded with evidence from both sides. May use the network."),
    "feedback_submit": (
        "Record critique of an earlier claim (claim_id) or a free statement as a hypothesis to verify - "
        "never as fact. Optional: correction (the statement the user proposes), reference (+ref) and more "
        "references ('URL[@ref]') to check against, or a checkable proposition (expect_pattern regex "
        "expected in files matching expect_in). process=true runs the verification protocol immediately "
        "(topic narrows reference research); otherwise call feedback_process later."),
    "feedback_process": (
        "Run the verification protocol for recorded feedback (feedback_id from feedback_submit): resolve the "
        "references it relies on (network off | cache | on; default from config), inspect them at their "
        "pins, re-check the claim and propose a verdict with evidence. A named version that cannot be "
        "pinned makes the verdict unresolved. May use the network."),
    "feedback_resolve": (
        "Close a feedback item with a verdict (confirmed | qualified | corrected | unresolved), a reason and "
        "the evidence ids that justify it; 'corrected' can carry the corrected statement (correction)."),
    "index_update": (
        "Re-index files changed since the last snapshot, record a new snapshot and mark claims whose files "
        "changed as stale. Full scan when there is no previous snapshot (requires .verinoda/ to exist). "
        "Returns mode (noop | incremental | full), changed files and the claims marked stale."),
    "decision_record": (
        "Decision records (Markdown with a front matter in decisions.dir, default .verinoda/decisions; logged "
        "append-only). action: list | record (chosen + rationale, optional brief_id, guards, governs, "
        "revisit_when, supersedes) | import (a record for a hand-written ADR, document=path; guards only "
        "proposed) | guard (add guards to decision_id) | accept (guard_ids of decision_id) | waive (one guard "
        "id in guard_ids, at='path[:line]', reason, until) | answer (the user's answer to question_id of a "
        "decision brief, brief_id). Guard specs: 'only_in calls=sqlite3.connect "
        "allowed=orders/repository.py', 'no_edge from=src/main/** to=src/client/**', 'dependency "
        "absent=psycopg'; revisit_when: 'dependency_added=NAME' or 'file_appears=GLOB'. record, guard, accept, "
        "waive and answer need user_statement: the user's own words, verbatim - Verinoda never decides and "
        "neither may the agent."),
    "decision_brief": (
        "What a human needs to decide a should/which/scale question, collected from the code - never a "
        "recommendation. forces: facts with evidence that re-checks now (storage sinks and how concentrated they "
        "are, configuration reads and defaults, declared dependencies and requires-python, decision documents, "
        "deployment files, concurrency and shared state, tests that pin the implementation, churn, numeric "
        "limits); absences: what was searched and not found, with the globs; existing_decisions; options: presence "
        "in the project with evidence, the code a change touches, installed metadata, external claims only as "
        "quote-checked pins, your own arguments as weak_inference; questions_for_human: at most 5, EN and TR, "
        "never one the code answers. verdict is always human_decision_required. Stored under brief_id; record the "
        "user's answers with decision_record(action='answer')."),
    "decision_check": (
        "Check the working tree against every accepted guard of every accepted decision record (the same as "
        "`verinoda decide check`). violations: VIOLATED sites, statically verified (Python calls bound through "
        "imports and aliases, Java/Kotlin import-bound calls, EXTRACTED graph edges whose line re-checks, "
        "declared dependencies); possible: heuristic hits (text matches, INFERRED edges, unresolved receivers); "
        "reviews: governed code that changed; triggers: revisit conditions that hold; ok: guards that hold, each "
        "with its scope and limits; waived; unknown. base (a git revision) or changed_only (= HEAD) labels "
        "findings new/touched or pre-existing, and then only new ones count (exit 1). exit 3 (status unknown): "
        "nothing violated, but something was not checked (a guard that read no file, edge or manifest; no record "
        "while ADR-like files exist) - never read it as ok. Refreshes a stale index "
        "first when a no_edge guard needs the graph (refresh=false skips it). Never edits code or records."),
    "experiment_run": (
        "Run one command as a recorded experiment in a throw-away copy of the working tree (or, with ref, of that "
        "commit; overlay lays working-tree files over it): allowlisted test runners only under process isolation, "
        "anything else needs docker/podman or is refused (the refusal says why). Returns outcome (pass | fail | "
        "timeout | inconclusive), the tree hash of what ran, log paths and the evidence id; with claim_id the run "
        "supports or refutes that claim (an inconclusive run only qualifies it)."),
    "debug_start": (
        "Open a debug session before the first edit of a bug fix: the symptom, the repro command (argument list) "
        "and a base commit (default HEAD). The repro runs once as attempt 0 in a throw-away copy (pytest gets a "
        "failure-signature plugin; trace=true adds the call tracer, which enables the off_path rule). A command "
        "Verinoda may not run (e.g. Gradle) is reported instead: observed_output (the run's output) + exit_code, "
        "recorded as agent-reported. Returns the session id and the attempt record (see debug_attempt)."),
    "debug_attempt": (
        "Record one attempt after an edit: hypothesis (what you believe and why) is required; the session's repro "
        "runs again on the current tree (or give command, or observed_output + exit_code for a run you made). "
        "Returns outcome, the tree (hash, files changed vs base and since the previous attempt, with symbols), the "
        "failure signature (exception at file::symbol per failing test), progress (improved | same | regressed | "
        "unknown), loop findings (definitive: tree_reverted, signature_recurred, no_progress, test_edited, "
        "failing_tests_skipped, off_path; heuristic: file_reverted, error_moved, masking, hypothesis_repeated, "
        "possibly_flaky; each cites the attempts it rests on), "
        "flaky, stop, strategies and questions_for_human. stop=true: stop editing and follow strategies[0]. "
        "Never reports 'fixed': a pass is 'the repro passed at tree T in run R' plus what was not run."),
    "debug_status": (
        "The ledger of a debug session (default: the latest open one): every attempt with its hypothesis, tree, "
        "failure, progress and loop findings, the latest stop and strategies, and - when an attempt passed - "
        "whether that passing tree is still the current one. Show it to the user when a session stops. Read-only."),
    "debug_strategy": (
        "Run a strategy the ledger proposed (each run is recorded as an attempt): differential (the repro on a copy "
        "of the base commit; when it passes there, the diff's hunks ranked by the failure's traceback; trace=true "
        "also compares the failing tests' observed calls at the base and in the failing tree; prepare=true only "
        "writes the copy for a command you run yourself), bisect (binary search over commits in throw-away copies "
        "from good to bad; commits that cannot run are skipped; returns the first failing commit and its hunks), "
        "rerun (times runs of the current tree: pass rate, flakiness), observe (one run under the call tracer: "
        "which failing tests reached each edited function, and the call chain to the crash)."),
}

_READ_ONLY = {"project_query", "node_inspect", "relation_trace", "map_view", "claim_inspect", "claim_list",
              "evidence_inspect", "question_plan_draft", "lexicon_show", "resolve_call", "code_check", "api_members",
              "debug_status"}
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
    ro = name in _READ_ONLY
    return ToolAnnotations(title=name.replace("_", " "), readOnlyHint=ro, destructiveHint=False,
                           idempotentHint=ro, openWorldHint=name in _OPEN_WORLD)


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


def build_server(repo: Path | str, tools: AtlasTools | None = None):
    """An MCP server (SDK object) exposing :data:`TOOL_NAMES` over ``repo``."""
    from pydantic import Field

    Server, major = _load_sdk()
    t = tools or AtlasTools(repo)
    emit = _result_wrapper(major)
    try:
        from verinoda import __version__
    except Exception:  # pragma: no cover
        __version__ = "0"
    try:
        srv = Server("verinoda", instructions=INSTRUCTIONS.format(repo=t.repo), version=__version__)
    except TypeError:  # mcp 1.x FastMCP has no version argument
        srv = Server("verinoda", instructions=INSTRUCTIONS.format(repo=t.repo))

    def register(name: str):
        def deco(fn):
            kwargs: dict[str, Any] = {"name": name, "description": DESCRIPTIONS[name]}
            ann = _tool_annotations(name)
            if ann is not None:
                kwargs["annotations"] = ann
            try:
                srv.add_tool(fn, **kwargs)
            except TypeError:  # pragma: no cover - SDK without annotations support
                kwargs.pop("annotations", None)
                srv.add_tool(fn, **kwargs)
            return fn
        return deco

    Id = Annotated[str, Field(description="A claim id such as 'clm_0123456789ab' (from analyze or claim_list).")]
    OptStr = str | None
    Network = Literal["off", "cache", "on"]

    @register("project_query")
    def project_query(
        question: Annotated[str, Field(description="Question, symbol or file names to look up.")],
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

    @register("map_view")
    def map_view(
        view: Annotated[Literal["hierarchy", "dependencies", "dataflow", "config", "tests", "history", "impact"],
                        Field(description="Which architecture view to return.")],
        targets: Annotated[list[str] | None, Field(description="impact view only: changed files or symbols; "
                                                               "default = git working-tree changes.")] = None,
    ) -> dict[str, Any]:
        return emit(t.map_view(view, targets=targets))

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
        question: Annotated[str, Field(description="The question to answer with claims and evidence (optional "
                                                   "when plan_json is given: the plan's user_message is used).")]
        = "",
        run_tests: Annotated[bool, Field(description="Also run the tests that statically reach the answer "
                                                     "(isolated copy, allowlisted runners only).")] = False,
        budget_seconds: Annotated[float, Field(description="Wall-time budget in seconds (1-600).")] = 60,
        budget_calls: Annotated[int, Field(description="Internal tool-call budget (1-200).")] = 40,
        # plain `str`: the SDK would parse a JSON string into an object for an optional (str | None) field
        plan_json: Annotated[str, Field(description="A checked question plan as JSON text (see "
                                                    "question_plan_check); used instead of drafting one.")] = "",
        observe: Annotated[bool, Field(description="Trace the tests that reach the answer with the runtime "
                                                   "call tracer (isolated copy) and attach what they observed.")]
        = False,
    ) -> dict[str, Any]:
        return emit(t.analyze(question, run_tests=run_tests, budget_seconds=budget_seconds,
                              budget_calls=budget_calls, plan_json=plan_json, observe=observe))

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

    EnvArg = Annotated[OptStr, Field(description="'auto' (default: the project's .venv, venv or env), a virtual "
                                                 "environment directory whose base interpreter is a Python "
                                                 "installation this system knows, outside the project (nothing "
                                                 "the repository supplies is started), or 'none' (standard "
                                                 "library only).")]

    @register("code_check")
    def code_check(
        paths: Annotated[list[str] | None, Field(description="Repository-relative files or directories to check "
                                                             "(whole files).")] = None,
        diff: Annotated[OptStr, Field(description="A revision: check only the sites on lines changed "
                                                  "against it, plus new files (e.g. 'HEAD').")] = None,
        snippet: Annotated[OptStr, Field(description="Python code not written yet, checked as if it were in "
                                                     "as_path.")] = None,
        as_path: Annotated[OptStr, Field(description="With snippet: the repository-relative file it is meant "
                                                     "for (imports and relative imports resolve from there).")]
        = None,
        env: EnvArg = None,
        include_exists: Annotated[bool, Field(description="Also list the sites that exist.")] = False,
    ) -> dict[str, Any]:
        return emit(t.code_check(paths=paths, diff=diff, snippet=snippet, as_path=as_path, env=env,
                                 include_exists=include_exists))

    @register("api_members")
    def api_members(
        target: Annotated[str, Field(description="Dotted module, class or function name "
                                                 "(e.g. 'packaging.specifiers.SpecifierSet').")],
        env: EnvArg = None,
        private: Annotated[bool, Field(description="Also list names starting with '_'.")] = False,
    ) -> dict[str, Any]:
        return emit(t.api_members(target, env=env, private=private))

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
        action: Annotated[Literal["list", "record", "import", "guard", "accept", "waive", "answer"],
                          Field(description="What to do (see the tool description).")],
        decision_id: Annotated[OptStr, Field(description="ADR-0001 (guard, accept, waive).")] = None,
        chosen: Annotated[OptStr, Field(description="record: the option the user chose.")] = None,
        rationale: Annotated[OptStr, Field(description="record: why, in the user's words.")] = None,
        title: Annotated[OptStr, Field(description="record: a short title.")] = None,
        brief_id: Annotated[OptStr, Field(description="record/answer: the decision brief (dbr_...).")] = None,
        guards: Annotated[StrList, Field(description="record/guard: guard specs.")] = None,
        governs: Annotated[StrList, Field(description="record: 'path/file.py::Symbol' whose changes need a "
                                                      "review.")] = None,
        revisit_when: Annotated[StrList, Field(description="record: 'dependency_added=NAME' / "
                                                           "'file_appears=GLOB'.")] = None,
        supersedes: Annotated[OptStr, Field(description="record: the decision this one replaces.")] = None,
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
                                      supersedes=supersedes, guard_ids=guard_ids, at=at, reason=reason, until=until,
                                      document=document, user_statement=user_statement,
                                      question_id=question_id))

    @register("decision_check")
    def decision_check(
        base: Annotated[OptStr, Field(description="A git revision (e.g. 'origin/main'): findings in files changed "
                                                  "since it are new/touched, the rest pre-existing.")] = None,
        changed_only: Annotated[bool, Field(description="The same against HEAD (the agent's own changes).")] = False,
        refresh: Annotated[bool, Field(description="Update a stale index first when a no_edge guard needs "
                                                   "the graph.")] = True,
    ) -> dict[str, Any]:
        return emit(t.decision_check(base=base, changed_only=changed_only, refresh=refresh))

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

    return srv


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


def serve(repo: Path) -> None:
    """Serve the Verinoda tools for ``repo`` over stdio (``verinoda mcp serve``)."""
    repo = Path(repo).resolve()
    try:
        srv = build_server(repo)
        major = _load_sdk()[1]
    except RuntimeError as exc:  # SDK missing or shadowed: a clear message, not a traceback
        raise SystemExit(f"error: {exc}") from None
    if major < 2:
        _move_protocol_off_std_fds()
    if graph_path(repo).exists():
        state = "indexed"
    elif atlas_dir(repo).is_dir():
        state = "initialised, not indexed - tools will ask for `verinoda scan`"
    else:
        state = "not scanned - tools will ask for `verinoda scan`"
    print(f"verinoda mcp: serving {repo} over stdio ({state})", file=sys.stderr, flush=True)
    try:
        srv.run("stdio")
    except KeyboardInterrupt:  # pragma: no cover - interactive stop
        pass
