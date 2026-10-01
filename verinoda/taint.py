"""Taint analysis for Python: untrusted values that reach dangerous calls, each with its whole path.

Sources (where untrusted values enter), sinks (calls that must not get them unchecked, with the argument that
matters) and sanitizers (calls whose result is safe to pass on) are data: the library behaviour in
``verinoda/data/taint_python.json``, and the project's own in ``[taint]`` of ``verinoda.toml`` (or
``[tool.verinoda.taint]`` of ``pyproject.toml``)::

    [taint]
    builtin = true                                  # keep the library's (false: only these)
    sources = ["get_param()", "settings.raw_input"]
    sinks = [{match = "db.run", arg = 0, kind = "sql"}]
    sanitizers = ["clean_sql()"]

Each library source has a threat: ``remote`` (request data from the network) or ``local`` (what the user
running the program controls: ``sys.argv``, the environment, ``input()``). Only remote sources are used unless
``local`` is asked for (``--local``), as CodeQL's default threat model does: in a command-line tool the user's own
environment reaching ``open()`` is the design, not a finding. The project's own sources are always used.

A name ``a.b`` matches a name or attribute chain ending in ``.a.b`` (``flask.request.args`` matches
``request.args``); ``a.b()`` matches a call of it; ``*.m`` matches the method ``m`` of any receiver. The
parameters of a web route handler that come from the request are sources too: the ``<name>`` (Flask) and
``{name}`` (FastAPI, Starlette) parts of its path, and for a FastAPI-style ``@x.get/post/...`` handler a
parameter annotated ``str`` or ``bytes`` (or not annotated) with no ``Depends(...)`` default.

For each sink call in the project's Python code, the argument is followed backward over data dependence only
(:mod:`verinoda.slicing`'s reaching definitions): every definition its names come from and what those are made of;
when a parameter is reached, the argument each caller passes for it (the index's ``calls`` edges), up to ``depth``
calls up; and when a value is the result of a call of a project function, what that function returns (one call
down, ``RETURN_DEPTH`` levels). A name or expression inside a sanitizer call is not followed. A source found on the
way is a finding, with every step from the source to the sink at ``file:line``.

Findings are ``strong_inference``: the path is read from the source code, but the analysis approximates the heap
(an attribute or item updates its object), takes a library call's result to depend on its arguments, and does not
see implicit flows (a branch on a tainted value).
"""

from __future__ import annotations

import ast
import json
import re
from collections import deque
from importlib import resources
from pathlib import Path

from verinoda import slicing

DEPTH = 2              # calls up from a parameter
RETURN_DEPTH = 2       # calls down into what a project function returns
MAX_STEPS = 20_000     # (function, node, name) states visited per sink, at most
MAX_FINDINGS = 500
ROUTE_ATTRS = {"route", "get", "post", "put", "patch", "delete", "api_route", "websocket"}
FASTAPI_ATTRS = {"get", "post", "put", "patch", "delete", "api_route"}
_FLASK_PARAM = re.compile(r"<(?:(?P<conv>[A-Za-z_]\w*)(?:\([^)]*\))?:)?(?P<name>[A-Za-z_]\w*)>")
_BRACE_PARAM = re.compile(r"\{(?P<name>[A-Za-z_]\w*)(?::(?P<conv>[A-Za-z_]\w*))?\}")
_SAFE_CONVERTERS = {"int", "float", "uuid"}   # the framework parses these: never free text
LIMITS = [
    "data dependence only: a branch on a tainted value (an implicit flow) is not followed",
    "a library call's result is taken to depend on its arguments; a project function's result is followed into "
    "its return statements (two levels), not through its parameters",
    "the heap is approximated: x.a = v and x[i] = v taint x; aliasing and globals changed elsewhere are not seen",
    "callers come from the index's calls edges (INFERRED ones are a resolver's guess) and are matched by name",
    "sources, sinks and sanitizers are names: a sink called through a variable or a wrapper is not seen",
    "a path found is not proof of a vulnerability (a check may happen in a way not declared as a sanitizer); "
    "no path found is not proof of safety",
]


class TaintError(ValueError):
    pass


# -- the specification ------------------------------------------------------------------------------

def _pattern(text: str) -> tuple[tuple[str, ...], bool, bool]:
    """``(segments, a call, any receiver)`` of a pattern."""
    if not isinstance(text, str):
        raise TaintError(f"not a name pattern: {text!r}")
    t = text.strip()
    call = t.endswith("()")
    if call:
        t = t[:-2]
    anyrecv = t.startswith("*.")
    if anyrecv:
        t = t[2:]
    if not t or not all(s.isidentifier() for s in t.split(".")):
        raise TaintError(f"not a name pattern: {text!r}")
    return tuple(t.split(".")), call, anyrecv


def load_spec(repo: Path, builtin: bool | None = None, *, local: bool = False) -> dict:
    """The sources, sinks and sanitizers in force, and where they come from. ``local``: also the library's local
    sources (argv, environment, input())."""
    from verinoda.decisions import _committed_tables

    lib = json.loads(resources.files("verinoda").joinpath("data/taint_python.json").read_text(encoding="utf-8"))
    own: dict = {}
    where = None
    for data, src, problem in _committed_tables(Path(repo), "taint"):
        if problem:
            raise TaintError(problem)
        if data is None:
            continue
        if not isinstance(data, dict):
            raise TaintError(f"{src}: [taint] is a table")
        own, where = data, src
        break
    keep = own.get("builtin", True) if builtin is None else builtin
    if not isinstance(keep, bool):
        raise TaintError(f"{where}: builtin is true or false")
    spec: dict = {"sources": [], "sinks": [], "sanitizers": [], "from": []}
    if keep:
        spec["sources"] += [dict(s, origin="library") for s in lib["sources"]
                            if local or s.get("threat", "remote") == "remote"]
        spec["sinks"] += [dict(s, origin="library") for s in lib["sinks"]]
        spec["sanitizers"] += [{"match": s, "origin": "library"} for s in lib["sanitizers"]]
        spec["from"].append("verinoda/data/taint_python.json")
    if own:
        spec["from"].append(where)
        for key in ("sources", "sinks", "sanitizers"):
            if not isinstance(own.get(key, []), list):
                raise TaintError(f"{where}: {key} is a list")
        for s in own.get("sources") or []:
            spec["sources"].append({"match": s, "kind": "project", "origin": where} if isinstance(s, str)
                                   else dict(s, origin=where) if isinstance(s, dict) else {"match": s})
        for s in own.get("sinks") or []:
            if not isinstance(s, dict) or "match" not in s:
                raise TaintError(f"{where} sinks: each sink is {{match = \"...\", arg = 0, kind = \"...\"}}")
            spec["sinks"].append(dict(s, origin=where))
        for s in own.get("sanitizers") or []:
            spec["sanitizers"].append({"match": s, "origin": where})
    for group in ("sources", "sinks", "sanitizers"):
        for s in spec[group]:
            if "match" not in s:
                raise TaintError(f"{where}: a {group[:-1]} needs match = \"...\"")
            s["_p"] = _pattern(s["match"])
            if group == "sinks":
                arg = s.get("arg", 0)
                if isinstance(arg, bool) or not isinstance(arg, (int, str)) or (isinstance(arg, int) and arg < 0):
                    raise TaintError(f"sink {s['match']}: arg is a position (0, 1, ...) or a keyword name")
    return spec


# -- matching ---------------------------------------------------------------------------------------

def _dotted(e: ast.AST) -> tuple[str, ...] | None:
    parts = []
    while isinstance(e, ast.Attribute):
        parts.append(e.attr)
        e = e.value
    if isinstance(e, ast.Name):
        parts.append(e.id)
        return tuple(reversed(parts))
    if parts and isinstance(e, (ast.Call, ast.Subscript)):   # get_app().request.args: the chain after the call
        return ("()", *reversed(parts))
    return None


def _ends(chain: tuple[str, ...] | None, segs: tuple[str, ...], anyrecv: bool) -> bool:
    if not chain:
        return False
    if anyrecv:
        return len(chain) > len(segs) and chain[-len(segs):] == segs
    return chain == segs or (len(chain) > len(segs) and chain[-len(segs):] == segs)


def _matches(node: ast.AST, pat) -> bool:
    segs, call, anyrecv = pat
    if call:
        return isinstance(node, ast.Call) and _ends(_dotted(node.func), segs, anyrecv)
    return isinstance(node, (ast.Attribute, ast.Name)) and _ends(_dotted(node), segs, anyrecv)


def _walk_unsanitized(expr: ast.AST | None, spec: dict):
    """The nodes of ``expr`` outside every sanitizer call (and outside lambdas, whose body runs later)."""
    if expr is None:
        return
    stack = [expr]
    while stack:
        n = stack.pop()
        if any(_matches(n, s["_p"]) for s in spec["sanitizers"]):
            continue
        if isinstance(n, ast.Lambda):
            continue
        yield n
        stack.extend(ast.iter_child_nodes(n))


def _source_in(exprs, spec: dict) -> tuple[dict, ast.AST] | None:
    for expr in exprs:
        for n in _walk_unsanitized(expr, spec):
            for s in spec["sources"]:
                if _matches(n, s["_p"]):
                    return s, n
    return None


def _names(exprs, spec: dict) -> set[str]:
    """Names the expressions read outside sanitizer calls (a comprehension's own names left out)."""
    out: set[str] = set()
    for expr in exprs:
        if expr is None:
            continue
        own = {n.id for n in _walk_unsanitized(expr, spec) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        out |= own & slicing._uses(expr)
    return out


def _part_exprs(t: ast.AST, value: ast.AST, var: str) -> list[ast.AST] | None:
    """The part of ``value`` that target ``t`` assigns to ``var`` (None: ``var`` is not in ``t``)."""
    names: dict[str, bool] = {}
    slicing._target_names(t, names)
    if var not in names:
        return None
    if (isinstance(t, (ast.Tuple, ast.List)) and isinstance(value, (ast.Tuple, ast.List))
            and len(t.elts) == len(value.elts)
            and not any(isinstance(e, ast.Starred) for e in (*t.elts, *value.elts))):
        out: list[ast.AST] = []
        for te, ve in zip(t.elts, value.elts):
            r = _part_exprs(te, ve, var)
            if r is not None:
                out += r
        return out
    return [value]


def _def_exprs(node: slicing.Node, var: str) -> list[ast.AST]:
    """What a node's definition of ``var`` is made of: in ``a, b = x, y`` the ``x`` for ``a``; else what the node
    evaluates."""
    s = node.stmt
    if (node.kind == "stmt" and isinstance(s, ast.Assign) and var not in slicing._walrus_defs(s.value)
            and all(isinstance(t, (ast.Name, ast.Tuple, ast.List)) for t in s.targets)):
        parts = [p for p in (_part_exprs(t, s.value, var) for t in s.targets) if p is not None]
        if parts:
            return [e for p in parts for e in p]
    return [node.expr] if node.expr is not None else []


def _extra_reads(node: slicing.Node) -> set[str]:
    """Names a node reads besides its expression (a match subject, the object an update changes)."""
    return set(node.uses) - (slicing._uses(node.expr) if node.expr is not None else set())


def _sink_calls(tree: ast.AST, spec: dict):
    for call in ast.walk(tree):
        if not isinstance(call, ast.Call):
            continue
        for s in spec["sinks"]:
            if not _matches(call, (s["_p"][0], True, s["_p"][2])):
                continue
            kw = s.get("when_keyword")
            if kw and not any(k.arg == kw and not (isinstance(k.value, ast.Constant) and not k.value.value)
                              for k in call.keywords):
                continue   # e.g. subprocess.run(...) without shell=True (shell=False / absent: not a shell)
            arg = s.get("arg", 0)
            expr = None
            if isinstance(arg, int):
                pos = call.args
                if not any(isinstance(a, ast.Starred) for a in pos[:arg + 1]) and arg < len(pos):
                    expr = pos[arg]
            if expr is None:
                kname = arg if isinstance(arg, str) else s.get("keyword")
                expr = next((k.value for k in call.keywords if kname and k.arg == kname), None)
            if expr is not None:
                yield call, s, expr
            break


def route_params(func) -> dict[str, str]:
    """{parameter: why} for the parameters of a web route handler that come from the request."""
    out: dict[str, str] = {}
    for dec in getattr(func, "decorator_list", []):
        if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) and dec.func.attr in ROUTE_ATTRS):
            continue
        path = dec.args[0] if dec.args else next((k.value for k in dec.keywords if k.arg in ("path", "rule")), None)
        if not (isinstance(path, ast.Constant) and isinstance(path.value, str)):
            continue
        text = path.value
        for rx, style in ((_FLASK_PARAM, "Flask"), (_BRACE_PARAM, "FastAPI/Starlette")):
            for m in rx.finditer(text):
                if (m.group("conv") or "") not in _SAFE_CONVERTERS:
                    out.setdefault(m.group("name"), f"the {style} route path {text!r}")
        if dec.func.attr in FASTAPI_ATTRS and "<" not in text:
            args = func.args
            pos = [*args.posonlyargs, *args.args]
            defaults = [None] * (len(pos) - len(args.defaults)) + list(args.defaults)
            for a, d in [*zip(pos, defaults), *zip(args.kwonlyargs, args.kw_defaults)]:
                if a.arg in ("self", "cls", "request", "response") or a.arg in out:
                    continue
                if isinstance(d, ast.Call) and isinstance(d.func, (ast.Name, ast.Attribute)) and \
                        (getattr(d.func, "id", None) or getattr(d.func, "attr", None)) == "Depends":
                    continue
                ann = a.annotation
                if isinstance(ann, ast.Subscript):   # Annotated[str, Query()], Optional[str]
                    ann = ann.slice.elts[0] if isinstance(ann.slice, ast.Tuple) else ann.slice
                if ann is None or (isinstance(ann, ast.Name) and ann.id in ("str", "bytes", "Any")):
                    out[a.arg] = f"a query or body parameter of the route {text!r}"
    return out


# -- the analysis -----------------------------------------------------------------------------------

class _Flow:
    """One function's control-flow graph and reaching definitions (what taint needs of a slice's ``_Fn``)."""

    def __init__(self, rel: str, func, lines: list[str], tree: ast.AST):
        self.rel, self.func, self.lines, self.tree = rel, func, lines, tree
        self.cfg = slicing.CFG(func)
        self.reach = self.cfg.reaching()


class _File:
    """A parsed file with its functions and the calls on each line, found once."""

    def __init__(self, tree: ast.AST, lines: list[str]):
        self.tree, self.lines = tree, lines
        self.funcs = slicing._functions(tree)
        self.by_name: dict[str, list] = {}
        for f in self.funcs:
            self.by_name.setdefault(f.name, []).append(f)
        self.calls: dict[int, list[ast.Call]] = {}
        for c in ast.walk(tree):
            if isinstance(c, ast.Call):
                for ln in range(c.lineno, (c.end_lineno or c.lineno) + 1):
                    self.calls.setdefault(ln, []).append(c)
        self._classes: set[str] | None = None

    def function_at(self, line: int):
        """As :func:`verinoda.slicing.function_at`, over the functions found once."""
        best = None
        for f in self.funcs:
            if slicing._first_line(f.body[0]) <= line <= (f.end_lineno or f.lineno) and \
                    (best is None or f.lineno >= best.lineno):
                best = f
        return best

    @property
    def classes(self) -> set[str]:
        if self._classes is None:
            self._classes = slicing._class_names(self.tree)
        return self._classes


class _Analysis:
    def __init__(self, repo: Path, spec: dict, graph, depth: int):
        self.repo, self.spec, self.graph, self.depth = repo, spec, graph, depth
        self.files: dict[str, _File] = {}
        self.fns: dict[tuple, _Flow | None] = {}
        self.nodes: dict[tuple, str | None] = {}
        self.returns: dict[tuple, list] = {}
        self.notes: list[str] = []

    def file(self, rel: str) -> _File:
        if rel not in self.files:
            self.files[rel] = _File(*slicing._read(self.repo, rel))
        return self.files[rel]

    def fn(self, rel: str, func) -> _Flow | None:
        key = (rel, func.lineno, func.col_offset)
        if key not in self.fns:
            try:
                f = self.file(rel)
                self.fns[key] = _Flow(rel, func, f.lines, f.tree)
            except slicing.SliceError as exc:   # too large a function
                self.notes.append(f"{rel}:{func.lineno}: {exc}")
                self.fns[key] = None
        return self.fns[key]

    def graph_node(self, rel: str, func) -> str | None:
        key = (rel, func.lineno, func.name)
        if key not in self.nodes:
            self.nodes[key] = slicing._graph_node(self.graph, rel, func)
        return self.nodes[key]

    def step(self, rel: str, line: int, what: str, var: str | None = None) -> dict:
        lines = self.file(rel).lines
        return {"at": f"{rel}:{line}", "text": slicing._line_text(lines, line), "what": what,
                **({"var": var} if var else {})}

    # the search: (source, steps from the source to the end of ``path``) for each source reached
    def search(self, fn: _Flow, nid: int, names: set[str], path: list[dict], depth: int, *,
               callers: bool, ret: int) -> list[tuple[dict, list[dict]]]:
        found: list[tuple[dict, list[dict]]] = []
        work = deque((fn, nid, v, path, depth) for v in sorted(names))
        seen: set[tuple] = set()
        visited = 0
        while work:
            if visited >= MAX_STEPS:
                self.notes.append(f"{fn.rel}:{fn.cfg.nodes[nid].line}: the search stopped after {MAX_STEPS} steps")
                break
            f, n, var, p, dep = work.popleft()
            key = (f.rel, f.func.lineno, n, var)
            if key in seen:
                continue
            seen.add(key)
            visited += 1
            for d in sorted(f.reach.get(n, var)):
                dn = f.cfg.nodes[d]
                if d == f.cfg.entry:   # a parameter
                    why = route_params(f.func).get(var)
                    if why:
                        src = {"match": f"route parameter {var}", "kind": "http", "origin": why}
                        found.append((src, [self.step(f.rel, f.func.lineno, f"source: {var} comes from {why}", var)]
                                      + p))
                        continue
                    if not callers:
                        continue
                    for cfn, cnode, arg, hop in self._callers(f, var, dep):
                        here = [hop] + p
                        crel = hop["at"].rsplit(":", 1)[0]
                        hit = _source_in([arg], self.spec)
                        if hit:
                            found.append((hit[0], [self.step(crel, hit[1].lineno, f"source {hit[0]['match']}")]
                                          + here))
                            continue
                        if ret > 0:
                            found += self._via_returns(crel, [arg], here, ret)
                        if cfn is not None:
                            for v in sorted(_names([arg], self.spec)):
                                work.append((cfn, cnode, v, here, dep - 1))
                    continue
                st = self.step(f.rel, dn.line, f"defines {var}", var)
                here = [st] + p
                exprs = _def_exprs(dn, var)
                hit = _source_in(exprs, self.spec)
                if hit:
                    src, sn = hit
                    lead = [] if sn.lineno == dn.line else [self.step(f.rel, sn.lineno, f"source {src['match']}")]
                    if not lead:
                        here = [dict(st, what=f"source {src['match']} -> {var}")] + p
                    found.append((src, lead + here))
                    continue
                if ret > 0:
                    found += self._via_returns(f.rel, exprs, here, ret)
                reads = _names(exprs, self.spec) | _extra_reads(dn)
                if var not in dn.replaces:   # an update (x.a = v, items.append(v)): what x held before too
                    reads.add(var)
                for v in sorted(reads):
                    work.append((f, d, v, here, dep))
        return found

    # one call down: what a project function returns
    def _via_returns(self, rel: str, exprs: list[ast.AST], here: list[dict], ret: int) -> list:
        out = []
        for expr in exprs:
            for c in _walk_unsanitized(expr, self.spec):
                if not isinstance(c, ast.Call):
                    continue
                target = self._callee(rel, c)
                if target is None:
                    continue
                for src, steps in self._returns(target, ret - 1):
                    out.append((src, steps + here))
        return out

    def _returns(self, cfn: _Flow, ret: int) -> list:
        key = (cfn.rel, cfn.func.lineno, ret)
        if key in self.returns:
            return self.returns[key]
        self.returns[key] = []   # recursion: nothing more while it is being computed
        found = []
        for r in cfn.cfg.nodes:
            if r.kind != "return" or r.expr is None:
                continue
            rstep = self.step(cfn.rel, r.line, f"{cfn.func.name} returns it")
            hit = _source_in([r.expr], self.spec)
            if hit:
                found.append((hit[0], [dict(rstep, what=f"source {hit[0]['match']}, returned by {cfn.func.name}")]))
                continue
            found += self.search(cfn, r.id, _names([r.expr], self.spec), [rstep], 0, callers=False, ret=ret)
            if ret > 0:
                found += self._via_returns(cfn.rel, [r.expr], [rstep], ret)
        self.returns[key] = found
        return found

    def _callee(self, rel: str, call: ast.Call) -> _Flow | None:
        """The project function a call starts: by the index's calls edge at that line, else the one function of
        that name in the same file."""
        name = slicing._callee_name(call)
        if not name:
            return None
        here = self.file(rel)
        if self.graph is not None:
            caller = here.function_at(call.lineno)
            src_nid = self.graph_node(rel, caller) if caller is not None else None
            if src_nid is not None:
                for v, d in self.graph.out_edges(src_nid, {"calls"}):
                    if str(d.get("source_location")) != f"L{call.lineno}":
                        continue
                    if self.graph.label(v).strip(".()").rpartition(".")[2] != name:
                        continue
                    frel, fline = self.graph.file(v), self.graph.line(v)
                    if not frel or not frel.endswith(".py") or fline is None:
                        continue
                    try:
                        defs = self.file(frel).by_name.get(name, [])
                    except slicing.SliceError:
                        continue
                    best = min(defs, key=lambda f: abs(f.lineno - fline), default=None)
                    if best is not None:
                        return self.fn(frel, best)
        defs = here.by_name.get(name, [])
        return self.fn(rel, defs[0]) if len(defs) == 1 else None

    def _callers(self, fn: _Flow, param: str, depth: int):
        if depth <= 0 or self.graph is None:
            return
        nid = self.graph_node(fn.rel, fn.func)
        if nid is None:
            return
        calls = sorted(self.graph.in_edges(nid, {"calls"}), key=lambda x: (str(x[1].get("source_file")),
                                                                          str(x[1].get("source_location"))))
        for _u, d in calls[:slicing.MAX_CALLERS]:
            src = d.get("source_file")
            loc = str(d.get("source_location") or "")
            if not src or not src.endswith(".py") or not loc.startswith("L") or not loc[1:].isdigit():
                continue
            cline = int(loc[1:])
            try:
                cf = self.file(src)
            except slicing.SliceError:
                continue
            calls_here = [c for c in cf.calls.get(cline, ()) if slicing._callee_name(c) == fn.func.name]
            if not calls_here:
                continue
            arg = slicing._argument_for(fn, calls_here[0], param, self.file(fn.rel).classes | cf.classes)
            if arg is slicing._SPREAD:
                self.notes.append(f"{src}:{cline}: may pass `{param}` through *args or **kwargs: not followed")
                continue
            if arg is None:
                arg = slicing._default_for(fn, param)
                if arg is None:
                    continue
            hop = self.step(src, cline, f"passes {ast.unparse(arg)[:60]} as {param} of {fn.func.name} "
                                        f"({d.get('confidence') or '?'} call edge)", param)
            cfunc = cf.function_at(cline)
            cfn = self.fn(src, cfunc) if cfunc is not None else None
            nodes = slicing._nodes_at(cfn.cfg, cline) if cfn is not None else []
            yield (cfn, nodes[0].id, arg, hop) if nodes else (None, None, arg, hop)

    def follow(self, rel: str, call: ast.Call, sink: dict, expr: ast.AST) -> list[dict]:
        """The paths from sources to this sink argument, one per source site (the shortest first)."""
        line = call.lineno
        sink_step = self.step(rel, line, f"sink {sink['match']} ({sink.get('kind') or 'sink'})")
        direct = _source_in([expr], self.spec)
        if direct:
            src, node = direct
            lead = [] if node.lineno == line else [self.step(rel, node.lineno, f"source {src['match']}")]
            if not lead:
                sink_step = dict(sink_step, what=f"source {src['match']} -> {sink_step['what']}")
            return [self._finding(rel, call, sink, src, lead + [sink_step])]
        found: list[tuple[dict, list[dict]]] = []
        func = self.file(rel).function_at(line)
        if func is not None:
            fn = self.fn(rel, func)
            nodes = slicing._nodes_at(fn.cfg, line) if fn is not None else []
            if nodes:
                found = self.search(fn, nodes[0].id, _names([expr], self.spec), [sink_step], self.depth,
                                    callers=True, ret=RETURN_DEPTH)
        found += self._via_returns(rel, [expr], [sink_step], RETURN_DEPTH)
        out, seen = [], set()
        for src, steps in found:
            k = (steps[0]["at"], src["match"])
            if k in seen:
                continue
            seen.add(k)
            out.append(self._finding(rel, call, sink, src, steps))
        return out

    def _finding(self, rel: str, call: ast.Call, sink: dict, src: dict, path: list[dict]) -> dict:
        return {"sink": {"at": f"{rel}:{call.lineno}", "call": ast.unparse(call)[:120], "match": sink["match"],
                         "kind": sink.get("kind"), "origin": sink.get("origin")},
                "source": {"at": path[0]["at"], "match": src["match"], "kind": src.get("kind"),
                           "origin": src.get("origin")},
                "path": path, "hops": sum(1 for s in path if s["what"].startswith("passes ")
                                          or " returns it" in s["what"] or "returned by" in s["what"]),
                "status": "strong_inference", "rule": f"taint/{sink.get('kind') or 'sink'}"}


def run(repo: Path, paths: list[str] | None = None, *, depth: int = DEPTH, builtin: bool | None = None,
        tests: bool = False, graph=None, local: bool = False) -> dict:
    """Every source-to-sink path in the project's Python files (``paths``: only under these)."""
    from verinoda import testcode
    from verinoda.paths import graph_path
    from verinoda.snapshot import list_files

    if not 0 <= depth <= slicing.MAX_DEPTH:
        raise TaintError(f"--depth must be 0..{slicing.MAX_DEPTH}")
    repo = Path(repo).resolve()
    spec = load_spec(repo, builtin, local=local)
    if graph is None and graph_path(repo).exists():
        from verinoda import index

        graph = index.load(repo)
    files = [f for f in list_files(repo) if f.endswith(".py")]
    scope = [p.replace("\\", "/").strip("/") for p in paths or []]
    scope = [s for s in scope if s not in ("", ".")] if scope else []
    if scope:
        files = [f for f in files if any(f == s or f.startswith(s + "/") for s in scope)]
    if not tests:
        files = [f for f in files if not testcode.is_test_or_support_file(f)]
    an = _Analysis(repo, spec, graph, depth)
    findings: list[dict] = []
    sinks_seen = 0
    unreadable = []
    for rel in files:
        try:
            tree = an.file(rel).tree
        except slicing.SliceError as exc:
            unreadable.append(str(exc))
            continue
        for call, sink, expr in _sink_calls(tree, spec):
            sinks_seen += 1
            findings += an.follow(rel, call, sink, expr)
        if len(findings) > MAX_FINDINGS:
            break
    findings.sort(key=lambda f: (f["sink"]["at"], f["source"]["at"]))
    limits = list(LIMITS)
    if graph is None:
        limits.append("no index: a parameter's callers are not followed and calls are resolved in their own file "
                      "only (run `verinoda scan`)")
    by_kind: dict[str, int] = {}
    for f in findings[:MAX_FINDINGS]:
        by_kind[f["sink"]["kind"] or "sink"] = by_kind.get(f["sink"]["kind"] or "sink", 0) + 1
    return {"status": "strong_inference" if findings else "none_found", "findings": findings[:MAX_FINDINGS],
            "truncated": len(findings) > MAX_FINDINGS, "by_kind": by_kind, "files": len(files), "sinks": sinks_seen,
            "spec": {"from": spec["from"], "sources": len(spec["sources"]), "sinks": len(spec["sinks"]),
                     "sanitizers": len(spec["sanitizers"]), "threats": ["remote", "local"] if local else ["remote"]},
            "depth": depth, "notes": (an.notes + unreadable)[:50], "limits": limits}


def render(res: dict) -> str:
    out = [f"{len(res['findings'])} path(s) from a source to a sink ({res['status']}); {res['sinks']} sink call(s) "
           f"in {res['files']} file(s); {' and '.join(res['spec']['threats'])} sources; rules from "
           f"{', '.join(res['spec']['from'])}"]
    for f in res["findings"]:
        out.append(f"  {f['rule']}: {f['source']['match']} -> {f['sink']['match']} at {f['sink']['at']}"
                   f" ({len(f['path'])} steps)")
        for s in f["path"]:
            out.append(f"      {s['at']:<34} {s['what'][:60]:<60} {s['text'][:80]}")
    for n in res["notes"][:10]:
        out.append(f"  note: {n}")
    if res.get("truncated"):
        out.append(f"  (cut at {MAX_FINDINGS} findings)")
    if not res["findings"]:
        out.append("  no path found: that is not a proof of safety (see the limits in --json)")
    return "\n".join(out) + "\n"
