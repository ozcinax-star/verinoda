"""RepoAtlas MCP server: the CLI's core operations as tools for coding agents.

Design rules
------------
* One implementation: every tool calls the same core function as the matching
  CLI command (``repoatlas query`` -> :func:`repoatlas.retrieval.retrieve`,
  ``analyze`` -> :func:`repoatlas.analysis.analyze`, ...). :class:`AtlasTools`
  holds those adapters and has no MCP SDK dependency, so it is testable and
  usable in-process; :func:`build_server` only registers them with the SDK.
* Small, structured answers: every response is a JSON object capped at
  ``MAX_RESPONSE_CHARS`` (compact JSON, env ``REPOATLAS_MCP_MAX_CHARS``). Long
  lists/maps are cut and the response is marked ``"truncated": true`` with a
  ``truncation`` note naming what was cut (``kept``/``total``). A view's
  ``coverage`` (method + limits) is never cut; if that alone exceeds the cap,
  ``truncation.over_limit`` says so.
* Errors never crash the server: they come back as
  ``{"error": <code>, "message": ..., "hint": <next step>, "tool": ...}``
  (flagged ``isError`` on mcp 2.x). A repository that was never scanned (no
  ``.repoatlas/``) yields ``error="not_initialised"``; the server never creates
  ``.repoatlas/`` itself. Argument *type* violations are rejected by the SDK
  before a tool runs (plain-text ``isError`` result).
* The repository is resolved once at startup; the SQLite store is opened per
  call (and closed after it), so no connection crosses threads. Tool calls are
  serialised with a lock (index rebuilds and stdout redirection are global),
  and stdout is redirected to stderr during each call.
* ``research`` and ``feedback`` are imported lazily inside their tools, so the
  server still starts when those modules are missing or broken.
* SDK: mcp 2.x (``mcp.server.mcpserver.MCPServer``) or 1.x (``FastMCP``).
  ``import mcp`` here is absolute, so it resolves to the SDK, not to this
  ``repoatlas.mcp`` package.
"""

import contextlib
import importlib
import json
import os
import sys
import threading
import traceback
from pathlib import Path
from typing import Annotated, Any, Callable, Literal

from repoatlas.paths import configure_index_env

configure_index_env()  # before anything imports repoatlas.project_index

from repoatlas.paths import ATLAS_DIRNAME, atlas_dir, graph_path  # noqa: E402

TOOL_NAMES: tuple[str, ...] = (
    "project_query",
    "node_inspect",
    "relation_trace",
    "map_view",
    "analyze",
    "claim_inspect",
    "claim_list",
    "evidence_inspect",
    "claim_verify",
    "claim_challenge",
    "reference_research",
    "reference_compare",
    "feedback_submit",
    "feedback_resolve",
    "index_update",
)

MAX_RESPONSE_CHARS = int(os.environ.get("REPOATLAS_MCP_MAX_CHARS", "12000"))
QUERY_MAX_CHARS = 6000          # same default as `repoatlas query --max-chars`
EXCERPT_MAX_LINES = 30
EDGE_CAP = 25
LIST_CAP = 50
VIEWS = ("hierarchy", "dependencies", "dataflow", "config", "tests", "history", "impact")
VERDICTS = ("confirmed", "qualified", "corrected", "unresolved")
RESEARCH_KINDS = ("auto", "official_doc", "standard", "paper", "secondary", "reference_repo")


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


def _scan(node: Any, parent: Any, key: Any, path: tuple, best: dict) -> int:
    """Estimate the compact-JSON size of ``node``; remember the biggest cuttables."""
    if len(path) == 1 and path[0] in PROTECTED_KEYS:
        return _size(node)
    if isinstance(node, dict):
        size = 2 + max(0, len(node) - 1)
        for k, v in node.items():
            size += len(json.dumps(str(k), ensure_ascii=False)) + 1 + _scan(v, node, k, path + (k,), best)
        if path and _map_like(node) and size > best["c"][0]:
            best["c"] = (size, parent, key, path, node)
        return size
    if isinstance(node, list):
        size = 2 + max(0, len(node) - 1)
        for i, v in enumerate(node):
            size += _scan(v, node, i, path + (i,), best)
        if len(node) >= 2 and size > best["c"][0]:
            best["c"] = (size, parent, key, path, node)
        return size
    size = len(json.dumps(node, ensure_ascii=False))
    if isinstance(node, str) and len(node) > 240 and size > best["s"][0]:
        best["s"] = (size, parent, key, path, node)
    return size


def cap_response(obj: dict, limit: int = MAX_RESPONSE_CHARS, *, first: tuple[str, ...] = (),
                 keep_tail: tuple[str, ...] = ("history",)) -> dict:
    """Cut ``obj`` until its compact JSON (truncation note included) fits ``limit`` chars.

    Top-level lists named in ``first`` are shortened before anything else.
    Then the largest list (or name->data map) anywhere in the tree is cut, or
    the longest string when it dominates. Cuts are proportional to the excess,
    so the result stays close to the limit. Lists under a key in ``keep_tail``
    keep their last items (newest history) instead of the first. Nothing is
    re-ordered or invented; ``truncation.cut`` says what was cut (kept/total).
    Top-level ``coverage`` (method and limits of a heuristic view) is never cut.
    """
    obj = _jsonable(obj)
    if _size(obj) <= limit:
        return obj
    notes: dict[str, dict] = {}

    def note() -> dict:
        cut = dict(list(notes.items())[:25])
        if len(notes) > 25:
            cut["..."] = {"more_cut_fields": len(notes) - 25}
        return {"max_chars": limit, "cut": cut,
                "hint": "narrow the request (smaller max_items, explicit targets, one view) "
                        "or run the matching `repoatlas ... --json` CLI command for the full result"}

    def excess() -> int:
        # size of obj + ',"truncated":true,"truncation":<note>' minus the limit
        return _size(obj) + 32 + _size(note()) - limit

    def record(path: tuple, total: int, kept: int) -> None:
        k = _path_str(path)
        prev = notes.get(k)
        notes[k] = {"kept": kept, "total": prev["total"] if prev else total}

    def cut_seq(path: tuple, node: list | dict, size: int, over: int) -> None:
        n = len(node)
        keep = int(n * (size - over) / size) if size > over else 1
        keep = max(1, min(n - 1, keep))
        record(path, n, keep)
        if isinstance(node, dict):
            for k in list(node)[keep:]:
                del node[k]
        elif bool(path) and path[-1] in keep_tail:
            del node[: n - keep]
        else:
            del node[keep:]

    for key in first:
        while isinstance(obj.get(key), list) and len(obj[key]) > 1 and (over := excess()) > 0:
            cut_seq((key,), obj[key], _size(obj[key]), over)

    for _ in range(400):
        over = excess()
        if over <= 0:
            break
        best: dict = {"c": (0, None, None, (), None), "s": (0, None, None, (), None)}
        _scan(obj, None, None, (), best)
        c_size, _, _, c_path, c_node = best["c"]
        s_size, s_parent, s_key, s_path, s_node = best["s"]
        if s_node is not None and (c_node is None or s_size * 2 >= c_size):
            new_len = max(200, min(len(s_node) - 1, len(s_node) - over - 16))
            s_parent[s_key] = s_node[:new_len] + "...[truncated]"
            record(s_path, len(s_node), new_len)
        elif c_node is not None:
            cut_seq(c_path, c_node, c_size, over)
        else:
            break  # nothing left that can be cut safely
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


# -- the tools --------------------------------------------------------------------

class AtlasTools:
    """Tool implementations over one repository (no MCP SDK needed)."""

    def __init__(self, repo: Path | str, *, max_chars: int = MAX_RESPONSE_CHARS):
        self.repo = Path(repo).resolve()
        self.max_chars = max_chars
        self._lock = threading.RLock()
        self._graph_cache: tuple[tuple, Any] | None = None

    # -- plumbing ---------------------------------------------------------------
    def _check(self, need: str) -> None:
        if need == "none":
            return
        if not atlas_dir(self.repo).is_dir():
            raise ToolFailure(
                "not_initialised",
                f"{self.repo} has no {ATLAS_DIRNAME}/ directory: it has not been scanned by RepoAtlas",
                f"run `repoatlas scan {self.repo}` in a terminal, then call this tool again",
                repo=str(self.repo),
            )
        if need == "graph" and not graph_path(self.repo).exists():
            raise ToolFailure(
                "no_index",
                f"{self.repo} has {ATLAS_DIRNAME}/ but no index ({graph_path(self.repo)} is missing)",
                f"call index_update, or run `repoatlas scan {self.repo}` in a terminal",
                repo=str(self.repo),
            )

    def _run(self, tool: str, fn: Callable[[], dict], *, need: str = "atlas",
             first: tuple[str, ...] = ()) -> dict:
        with self._lock:
            try:
                # Stray prints from core/vendored code must never reach the
                # JSON-RPC channel on stdout.
                with contextlib.redirect_stdout(sys.stderr):
                    self._check(need)
                    res = fn()
                    if not isinstance(res, dict):
                        res = {"result": res}
                    return cap_response(res, self.max_chars, first=first)
            except ToolFailure as f:
                return {**f.as_dict(), "tool": tool}
            except KeyError as exc:
                msg = str(exc.args[0] if exc.args else exc)
                if not msg.startswith("no "):  # a missing dict key inside the core, not a missing record
                    traceback.print_exc(file=sys.stderr)
                    return {"error": "internal_error", "message": f"KeyError: {msg}"[:600], "tool": tool,
                            "hint": _error_hint(exc, self.repo)}
                return {"error": "not_found", "message": msg, "tool": tool,
                        "hint": "check the id; claim ids come from analyze/claim_list, "
                                "evidence ids from claim_inspect"}
            except FileNotFoundError as exc:
                missing_index = not graph_path(self.repo).exists()
                return {"error": "no_index" if missing_index else "file_not_found", "message": str(exc),
                        "tool": tool,
                        "hint": (f"run `repoatlas scan {self.repo}` or call index_update" if missing_index
                                 else "the file may have been moved or deleted; call index_update")}
            except Exception as exc:  # never let a tool take the server down
                traceback.print_exc(file=sys.stderr)
                return {"error": _error_code(exc), "message": f"{type(exc).__name__}: {exc}"[:600],
                        "tool": tool, "hint": _error_hint(exc, self.repo)}

    @contextlib.contextmanager
    def _store(self):
        from repoatlas.store import open_store

        st = open_store(self.repo)
        try:
            yield st
        finally:
            st.close()

    def _graph(self):
        """The loaded graph, cached until graph.json changes."""
        from repoatlas import index

        gp = graph_path(self.repo)
        stt = gp.stat()
        key = (str(gp), stt.st_mtime_ns, stt.st_size)
        if self._graph_cache is None or self._graph_cache[0] != key:
            self._graph_cache = (key, index.load(self.repo))
        g = self._graph_cache[1]
        g._spans.clear()  # spans are re-derived from the current files, as the CLI does
        return g

    @staticmethod
    def _optional(module: str):
        try:
            return importlib.import_module(module)
        except Exception as exc:
            raise ToolFailure(
                "unavailable",
                f"{module} could not be imported: {type(exc).__name__}: {exc}"[:500],
                "this RepoAtlas installation lacks that feature or it is broken; "
                "run `repoatlas doctor` and retry after fixing/upgrading",
            ) from None

    # -- retrieval & graph --------------------------------------------------------
    def project_query(self, question: str, max_items: int = 8) -> dict:
        def go():
            from repoatlas import retrieval

            q = _text(question, "question")
            n = _clamp(max_items, 1, 25, "max_items")
            return retrieval.retrieve(self._graph(), q,
                                      retrieval.Budget(max_items=n, max_chars=QUERY_MAX_CHARS))
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
                    if str(d.get("_origin", "")).startswith("repoatlas"):
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
            from repoatlas import retrieval

            s, t = _text(source, "source"), _text(target, "target")
            if mode not in ("flow", "any"):
                raise ToolFailure("invalid_argument", f"mode must be 'flow' or 'any', got {mode!r}",
                                  "use mode='flow' for call paths, mode='any' for structural relations")
            res = retrieval.trace(self._graph(), s, t, mode=mode)
            st = res.get("status")
            if st == "unresolved":
                res["next_step"] = ("an endpoint did not resolve: see 'candidates', or call node_inspect / "
                                    "project_query and pass 'path/file.py::symbol'")
            elif st and st.startswith("ambiguous"):
                res["next_step"] = "both names resolved to one node: disambiguate with 'path/file.py::symbol'"
            elif st == "no directed path":
                res["next_step"] = (("retry with mode='any' for structural relations; " if mode == "flow" else "")
                                    + "no path in the static graph does not prove there is none at runtime "
                                      "(dynamic dispatch, callbacks and DI are not resolved)")
            return res
        return self._run("relation_trace", go, need="graph")

    def map_view(self, view: str, targets: list[str] | None = None) -> dict:
        def go():
            from repoatlas import architecture_map as am

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

    # -- analysis & claims ----------------------------------------------------------
    def analyze(self, question: str, run_tests: bool = False, budget_seconds: float = 60,
                budget_calls: int = 40) -> dict:
        def go():
            from repoatlas import analysis

            q = _text(question, "question")
            b = analysis.Budget(seconds=_clamp(budget_seconds, 1, 600, "budget_seconds", float),
                                tool_calls=_clamp(budget_calls, 1, 200, "budget_calls"),
                                context_tokens=6000)  # same default as `repoatlas analyze`
            with self._store() as st:
                return analysis.analyze(st, self.repo, q, budget=b, run_tests=bool(run_tests), challenge=True)
        return self._run("analyze", go, need="graph", first=("steps", "critique"))

    def claim_inspect(self, claim_id: str) -> dict:
        def go():
            from repoatlas.claims import Claims

            with self._store() as st:
                return Claims(st, self.repo).show(_text(claim_id, "claim_id"))
        return self._run("claim_inspect", go)

    def claim_list(self, status: str | None = None, limit: int = 20) -> dict:
        def go():
            from repoatlas.claims import STATUSES

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
            from repoatlas import evidence as evmod

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
                    recheck = {"performed": True, **evmod.check_source(self.repo, ev).as_dict()}
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
            from repoatlas import workflow

            with self._store() as st:
                return workflow.verify(st, self.repo, _text(claim_id, "claim_id"), run=bool(run))
        return self._run("claim_verify", go)

    def claim_challenge(self, claim_id: str) -> dict:
        def go():
            from repoatlas import critique

            cid = _text(claim_id, "claim_id")
            g = self._graph() if graph_path(self.repo).exists() else None
            with self._store() as st:
                return critique.challenge(st, self.repo, cid, graph=g)
        return self._run("claim_challenge", go)

    # -- references ---------------------------------------------------------------
    def reference_research(self, reference: str, ref: str | None = None, topic: str | None = None,
                           kind: str = "auto") -> dict:
        def go():
            r = _text(reference, "reference")
            if kind not in RESEARCH_KINDS:
                raise ToolFailure("invalid_argument", f"unknown kind {kind!r}",
                                  "choose one of: " + ", ".join(RESEARCH_KINDS), valid=list(RESEARCH_KINDS))
            research = self._optional("repoatlas.research")
            with self._store() as st:
                return research.research(st, self.repo, r, ref=_opt_text(ref), topic=_opt_text(topic), kind=kind)
        return self._run("reference_research", go)

    def reference_compare(self, reference: str, topic: str, ref: str | None = None) -> dict:
        def go():
            r, t = _text(reference, "reference"), _text(topic, "topic")
            research = self._optional("repoatlas.research")
            with self._store() as st:
                return research.compare(st, self.repo, r, ref=_opt_text(ref), topic=t)
        return self._run("reference_compare", go)

    # -- feedback -----------------------------------------------------------------
    def feedback_submit(self, text: str, claim_id: str | None = None, reference: str | None = None,
                        ref: str | None = None, correction: str | None = None, process: bool = True,
                        expect_pattern: str | None = None, expect_in: str | None = None,
                        topic: str | None = None) -> dict:
        def go():
            body = _text(text, "text")
            feedback = self._optional("repoatlas.feedback")
            with self._store() as st:
                res = feedback.add(st, self.repo, body, claim_id=_opt_text(claim_id), reference=_opt_text(reference),
                                   ref=_opt_text(ref), correction=_opt_text(correction),
                                   expect_pattern=_opt_text(expect_pattern), expect_in=_opt_text(expect_in))
                if not process:
                    return res
                fid = res["id"]
                try:
                    return feedback.process(st, self.repo, fid, topic=_opt_text(topic))
                except Exception as exc:  # the feedback is recorded; say so instead of losing its id
                    traceback.print_exc(file=sys.stderr)
                    return {"error": "process_failed", "feedback_id": fid, "added": res,
                            "message": f"{type(exc).__name__}: {exc}"[:600],
                            "hint": f"the feedback was recorded as {fid}; retry with "
                                    f"`repoatlas feedback process {fid} --repo {self.repo}`"}
        return self._run("feedback_submit", go)

    def feedback_resolve(self, feedback_id: str, verdict: str, reason: str, evidence_ids: list[str] | None = None,
                         correction: str | None = None) -> dict:
        def go():
            fid, why = _text(feedback_id, "feedback_id"), _text(reason, "reason")
            if verdict not in VERDICTS:
                raise ToolFailure("invalid_argument", f"unknown verdict {verdict!r}",
                                  "choose one of: " + ", ".join(VERDICTS), valid=list(VERDICTS))
            ids = [evidence_ids] if isinstance(evidence_ids, str) else list(evidence_ids or [])
            feedback = self._optional("repoatlas.feedback")
            with self._store() as st:
                return feedback.resolve(st, self.repo, fid, verdict, reason=why,
                                        evidence_ids=[str(i) for i in ids if str(i).strip()],
                                        correction=_opt_text(correction))
        return self._run("feedback_resolve", go)

    # -- index ----------------------------------------------------------------------
    def index_update(self) -> dict:
        def go():
            from repoatlas import workflow

            with self._store() as st:
                return workflow.update(st, self.repo)
        return self._run("index_update", go)


def _error_code(exc: BaseException) -> str:
    name = type(exc).__name__
    if name == "ClaimRuleError":
        return "rule_violation"
    if name == "ExperimentRefused":
        return "refused"
    if isinstance(exc, (ValueError, TypeError)):
        return "invalid_argument"
    return "internal_error"


def _error_hint(exc: BaseException, repo: Path) -> str:
    name = type(exc).__name__
    if name == "ClaimRuleError":
        return "the evidence does not allow that status; inspect the claim with claim_inspect"
    if name == "ExperimentRefused":
        return "the command is outside the process-isolation allowlist; enable docker/podman or run it manually"
    return (f"see the server log (stderr); `repoatlas doctor --repo {repo}` checks the installation "
            "and index")


# -- MCP registration -------------------------------------------------------------

INSTRUCTIONS = """RepoAtlas: evidence-first analysis of the repository {repo}.

Use it before reading many files:
- project_query: where is X / what handles Y (bounded hits with file:line and reasons).
- node_inspect / relation_trace: one symbol's definition and edges; call paths between two symbols.
- map_view: architecture views (hierarchy, dependencies, dataflow, config, tests, history, impact).
- analyze: answer a question as recorded claims with evidence, critique and unknowns.
- claim_inspect / claim_list / evidence_inspect / claim_verify / claim_challenge: audit claims.
- reference_research / reference_compare: pinned external references.
- feedback_submit / feedback_resolve: record critique of a claim as a hypothesis, then resolve it.
- index_update: re-index after editing files (marks affected claims stale).

Rules: graph edges (EXTRACTED/INFERRED) are extractions, never verification. Claim status is one of
observed, experiment_verified, statically_verified, primary_source_verified, strong_inference,
weak_inference, unknown, contradicted, stale; heuristic results say so in 'coverage'/'uncertainties'.
When evidence is missing the answer is 'unknown' plus a next step - do not fill the gap by guessing.
Responses are small JSON objects; "truncated": true means lists were cut (see 'truncation').
Errors come back as {{"error", "message", "hint"}}; not_initialised/no_index means the user must run
`repoatlas scan {repo}` first."""

DESCRIPTIONS: dict[str, str] = {
    "project_query": (
        "Bounded, justified retrieval for a question: up to max_items code locations (symbol, file, "
        "start-end lines, short excerpt), each with the reason it was selected (label score, text hits, "
        "graph neighbour), plus the relations among them (relation, EXTRACTED/INFERRED, file:line). "
        "Read-only; uses the existing index. Start here for 'where is X / what handles Y'. "
        "Hits are retrieval results, not verified claims."),
    "node_inspect": (
        "Inspect one graph node. name may be a node id, a label ('place_order'), 'path/file.py::symbol', "
        "'Class.method' or a file path. Returns the resolved node and how it was resolved (a 'scored' "
        "resolution is a heuristic label match - check 'candidates'/'same_label'), its location, the "
        "source excerpt of its definition (at most 30 lines) and its outgoing/incoming edges with relation, "
        "confidence and file:line (at most 25 each, totals given). Edges are extractions, not verification."),
    "relation_trace": (
        "Directed paths (up to 3, at most 8 hops) from source to target; every hop has relation, "
        "confidence (EXTRACTED/INFERRED) and call-site file:line. mode='flow' follows calls only "
        "(control flow, plus construction -> __init__); mode='any' also follows uses/imports/inherits/"
        "method/references. status: found | unresolved | no directed path | ambiguous. No path in the "
        "static graph does not prove there is none at runtime."),
    "map_view": (
        "One top-down architecture view. view: hierarchy (subsystems/packages/files), dependencies "
        "(file-level call/import edges), dataflow (entry points -> persistence sinks), config (env vars "
        "and config files), tests (static test reachability), history (git log + decision records), "
        "impact (reverse dependents of targets). 'coverage' states the method and its limits; heuristics "
        "are labelled. For impact pass targets (files or symbols); without targets the git working-tree "
        "changes are used. Large views are truncated."),
    "analyze": (
        "Answer a question as claims with evidence: retrieves code, re-checks cited lines, uses git "
        "history/decision records for 'why', optionally runs the tests that reach the answer "
        "(run_tests=true, isolated copy), records every conclusion as a claim (status, confidence, "
        "evidence, uncertainties), challenges each claim and lists unknowns with the next verification "
        "step. Writes claims to the project store; re-indexes first if the working tree changed. "
        "Bounded by budget_seconds and budget_calls; when the budget runs out the rest is reported as unknown."),
    "claim_inspect": (
        "Full record of one claim: text, status, confidence, kind, the snapshot/commit it is pinned to, "
        "supporting/refuting/qualifying evidence (id, type, file:line or URL, hash) and its complete "
        "status history (newest kept if truncated). Read-only."),
    "claim_list": (
        "List recorded claims, newest first (id, status, confidence, kind, text), optionally filtered by "
        "status. Use it to find claim ids from earlier sessions. Read-only."),
    "evidence_inspect": (
        "One evidence record (source type, locator, commit, content hash, excerpt, metadata), whether that "
        "source type can verify a claim, the claims citing it, and a live re-check: file/line evidence is "
        "re-read now (from meta.root for reference checkouts) and compared with the recorded hash "
        "(unchanged / changed / moved to other lines / file missing). Read-only."),
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
    "reference_research": (
        "Inspect a reference: a git repository (URL or local path, pinned to ref or the default branch "
        "HEAD) or a document URL, optionally tracing a topic/mechanism in it; the research is recorded "
        "with its pinned commit/hash as evidence. May use the network (git clone / HTTP) and write under "
        ".repoatlas/research."),
    "reference_compare": (
        "Compare a mechanism's assumptions (topic) between this repository and a reference repository or "
        "document pinned at ref; findings are recorded with evidence from both sides. May use the network."),
    "feedback_submit": (
        "Record critique of an earlier claim (claim_id) or a free statement as a hypothesis to verify - "
        "never as fact. Optional: correction (the statement the user proposes), reference (+ref) to check "
        "against, or a checkable proposition (expect_pattern regex expected in files matching expect_in). "
        "process=true runs the verification protocol immediately (topic narrows reference research)."),
    "feedback_resolve": (
        "Close a feedback item with a verdict (confirmed | qualified | corrected | unresolved), a reason and "
        "the evidence ids that justify it; 'corrected' can carry the corrected statement (correction)."),
    "index_update": (
        "Re-index files changed since the last snapshot, record a new snapshot and mark claims whose files "
        "changed as stale. Full scan when there is no previous snapshot (requires .repoatlas/ to exist). "
        "Returns mode (noop | incremental | full), changed files and the claims marked stale."),
}

_READ_ONLY = {"project_query", "node_inspect", "relation_trace", "map_view", "claim_inspect", "claim_list",
              "evidence_inspect"}
_OPEN_WORLD = {"reference_research", "reference_compare", "feedback_submit"}


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
                               "(or the repoatlas[mcp] extra)") from exc
    sdk_file = Path(sys.modules[Server.__module__].__file__ or "").resolve()
    if Path(__file__).resolve().parent in sdk_file.parents:  # pragma: no cover - misconfigured sys.path
        raise RuntimeError(f"`import mcp` resolved to {sdk_file} inside repoatlas.mcp; "
                           "do not put the repoatlas package directory itself on sys.path")
    return Server, major


def _tool_annotations(name: str):
    try:
        from mcp.types import ToolAnnotations
    except ImportError:  # pragma: no cover - very old SDK
        return None
    ro = name in _READ_ONLY
    return ToolAnnotations(title=name.replace("_", " "), readOnlyHint=ro, destructiveHint=False,
                           idempotentHint=ro, openWorldHint=name in _OPEN_WORLD)


def _result_wrapper(major: int) -> Callable[[dict], Any]:
    """How a tool's dict reaches the wire.

    mcp 2.x: an explicit CallToolResult - compact JSON text (the SDK default
    would pretty-print it, inflating what the agent reads), the same dict as
    structured content, and ``is_error`` set for ``{"error": ...}`` results.
    mcp 1.x: the plain dict (the SDK serialises it).
    """
    if major < 2:
        return lambda res: res
    from mcp.types import CallToolResult, TextContent

    def emit(res: dict):
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
        from repoatlas import __version__
    except Exception:  # pragma: no cover
        __version__ = "0"
    try:
        srv = Server("repoatlas", instructions=INSTRUCTIONS.format(repo=t.repo), version=__version__)
    except TypeError:  # mcp 1.x FastMCP has no version argument
        srv = Server("repoatlas", instructions=INSTRUCTIONS.format(repo=t.repo))

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

    @register("project_query")
    def project_query(
        question: Annotated[str, Field(description="Question, symbol or file names to look up.")],
        max_items: Annotated[int, Field(description="Maximum code locations to return (1-25).")] = 8,
    ) -> dict[str, Any]:
        return emit(t.project_query(question, max_items=max_items))

    @register("node_inspect")
    def node_inspect(
        name: Annotated[str, Field(description="Node id, label, 'path/file.py::symbol', 'Class.method' or file path.")],
    ) -> dict[str, Any]:
        return emit(t.node_inspect(name))

    @register("relation_trace")
    def relation_trace(
        source: Annotated[str, Field(description="Start symbol/file (label, 'path/file.py::symbol', 'Class.method').")],
        target: Annotated[str, Field(description="End symbol/file.")],
        mode: Annotated[Literal["flow", "any"], Field(description="'flow' = calls only; 'any' = also uses/imports/inherits.")] = "flow",
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

    @register("analyze")
    def analyze(
        question: Annotated[str, Field(description="The question to answer with claims and evidence.")],
        run_tests: Annotated[bool, Field(description="Also run the tests that statically reach the answer "
                                                     "(isolated copy, allowlisted runners only).")] = False,
        budget_seconds: Annotated[float, Field(description="Wall-time budget in seconds (1-600).")] = 60,
        budget_calls: Annotated[int, Field(description="Internal tool-call budget (1-200).")] = 40,
    ) -> dict[str, Any]:
        return emit(t.analyze(question, run_tests=run_tests, budget_seconds=budget_seconds, budget_calls=budget_calls))

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
        evidence_id: Annotated[str, Field(description="An evidence id such as 'evd_0123456789ab' (from claim_inspect).")],
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

    @register("reference_research")
    def reference_research(
        reference: Annotated[str, Field(description="Git repository URL or local path, or a document URL.")],
        ref: Annotated[OptStr, Field(description="Branch, tag or commit to pin (default: default branch HEAD).")] = None,
        topic: Annotated[OptStr, Field(description="Mechanism to trace in the reference.")] = None,
        kind: Annotated[Literal["auto", "official_doc", "standard", "paper", "secondary", "reference_repo"],
                        Field(description="Source kind; 'auto' detects it.")] = "auto",
    ) -> dict[str, Any]:
        return emit(t.reference_research(reference, ref=ref, topic=topic, kind=kind))

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
        expect_pattern: Annotated[OptStr, Field(description="Regex the user says occurs (checkable proposition).")] = None,
        expect_in: Annotated[OptStr, Field(description="File glob where expect_pattern should occur.")] = None,
        topic: Annotated[OptStr, Field(description="Mechanism to trace when processing against a reference.")] = None,
    ) -> dict[str, Any]:
        return emit(t.feedback_submit(text, claim_id=claim_id, reference=reference, ref=ref, correction=correction,
                                      process=process, expect_pattern=expect_pattern, expect_in=expect_in,
                                      topic=topic))

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


def serve(repo: Path) -> None:
    """Serve the RepoAtlas tools for ``repo`` over stdio (``repoatlas mcp serve``)."""
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
        state = "initialised, not indexed - tools will ask for `repoatlas scan`"
    else:
        state = "not scanned - tools will ask for `repoatlas scan`"
    print(f"repoatlas mcp: serving {repo} over stdio ({state})", file=sys.stderr, flush=True)
    try:
        srv.run("stdio")
    except KeyboardInterrupt:  # pragma: no cover - interactive stop
        pass
