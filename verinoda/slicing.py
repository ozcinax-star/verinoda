"""Control and data dependence in Python functions, and slices over them.

For one function :func:`build` makes a control-flow graph whose nodes are its statements (a compound statement is
its header: an ``if``'s test, a ``for``'s target and iterable, a ``with``'s items), with the names each node
defines and uses. On it:

- **reaching definitions** (which assignments of a name can reach a node: an iterative data-flow analysis);
- **control dependence** from post-dominators (a node depends on a branch when one of the branch's ways leads to
  it and another can avoid it: an ``if``'s body, the statements after an ``if`` that returns, a loop's body);
- together, the **program dependence graph**: data edges (definition -> use of a name) and control edges.

A **backward slice** from a line is every node the line's values or its being run depend on, transitively; when
the slice reaches a parameter of the function, the callers the index knows (its ``calls`` edges) are read and the
argument passed for that parameter is sliced in each caller, up to ``depth`` calls up. A **forward slice** is every
node a definition at the line can affect.

Heap state is approximated, never followed: ``x.a = v`` and ``x[i] = v`` define ``x`` without replacing its
earlier definitions, and a method call on a name as a statement (``items.append(v)``) may change it. Aliasing,
globals changed by other functions, ``exec``/``eval``, ``getattr`` and exceptions raised inside called functions
are not seen. So a slice is ``strong_inference``: the lines are read from the source, the dependence between them
is the analysis's, not an observation.
"""

from __future__ import annotations

import ast
from collections import defaultdict, deque
from pathlib import Path

MAX_DEPTH = 3          # calls up a backward slice may follow
MAX_CALLERS = 8        # callers read per parameter
MAX_NODES = 4000       # a function larger than this is not analysed (its graph would be slow to solve)


class SliceError(ValueError):
    pass


# -- names ------------------------------------------------------------------------------------------

def _target_names(t: ast.AST, out: dict[str, bool]) -> None:
    """Names a target defines: ``True`` when it replaces the name (``x = v``), ``False`` when it only updates
    the object the name holds (``x.a = v``, ``x[i] = v``)."""
    if isinstance(t, ast.Name):
        out[t.id] = True
    elif isinstance(t, (ast.Tuple, ast.List)):
        for e in t.elts:
            _target_names(e, out)
    elif isinstance(t, ast.Starred):
        _target_names(t.value, out)
    elif isinstance(t, (ast.Attribute, ast.Subscript)):
        base = t.value
        while isinstance(base, (ast.Attribute, ast.Subscript)):
            base = base.value
        if isinstance(base, ast.Name):
            out.setdefault(base.id, False)


def _uses(node: ast.AST | None) -> set[str]:
    """Names an expression reads (not those a comprehension or lambda inside it binds for itself)."""
    if node is None:
        return set()
    out: set[str] = set()

    def walk(n: ast.AST, bound: frozenset) -> None:
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
            if n.id not in bound:
                out.add(n.id)
            return
        if isinstance(n, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            inner = set(bound)
            for gen in n.generators:
                walk(gen.iter, frozenset(inner))
                tn: dict[str, bool] = {}
                _target_names(gen.target, tn)
                inner |= set(tn)
                for cond in gen.ifs:
                    walk(cond, frozenset(inner))
            for part in ((n.key, n.value) if isinstance(n, ast.DictComp) else (n.elt,)):
                walk(part, frozenset(inner))
            return
        if isinstance(n, ast.Lambda):
            params = {a.arg for a in (*n.args.posonlyargs, *n.args.args, *n.args.kwonlyargs)}
            params |= {a.arg for a in (n.args.vararg, n.args.kwarg) if a}
            for d in (*n.args.defaults, *[d for d in n.args.kw_defaults if d]):
                walk(d, bound)
            walk(n.body, bound | frozenset(params))
            return
        for child in ast.iter_child_nodes(n):
            walk(child, bound)

    walk(node, frozenset())
    return out


def _walrus_defs(node: ast.AST | None) -> set[str]:
    return {n.target.id for n in ast.walk(node) if isinstance(n, ast.NamedExpr)} if node is not None else set()


# -- the control-flow graph -------------------------------------------------------------------------

class Node:
    __slots__ = ("id", "kind", "stmt", "line", "uses", "defs", "replaces", "expr")

    def __init__(self, nid: int, kind: str, stmt: ast.AST | None, line: int):
        self.id, self.kind, self.stmt, self.line = nid, kind, stmt, line
        self.uses: set[str] = set()
        self.defs: set[str] = set()
        self.replaces: set[str] = set()   # the defs that replace earlier ones (kill them)
        self.expr: ast.AST | None = None  # the expression a header node evaluates


class CFG:
    """The statements of one function as a control-flow graph (``ENTRY`` defines the parameters)."""

    def __init__(self, func: ast.FunctionDef | ast.AsyncFunctionDef):
        self.func = func
        self.nodes: list[Node] = []
        self.succ: dict[int, set[int]] = defaultdict(set)
        self.entry = self._node("entry", func, func.lineno)
        self.exit = self._node("exit", None, getattr(func, "end_lineno", func.lineno))
        args = func.args
        self.params = [a.arg for a in (*args.posonlyargs, *args.args)]
        self.params += [args.vararg.arg] if args.vararg else []
        self.params += [a.arg for a in args.kwonlyargs] + ([args.kwarg.arg] if args.kwarg else [])
        self.nodes[self.entry].defs = set(self.params)
        self.nodes[self.entry].replaces = set(self.params)
        first = self._list(func.body, self.exit, None, None, [])
        self.succ[self.entry].add(first)

    def _node(self, kind: str, stmt, line: int) -> int:
        if len(self.nodes) >= MAX_NODES:
            raise SliceError(f"the function has over {MAX_NODES} statements: not analysed")
        n = Node(len(self.nodes), kind, stmt, line)
        self.nodes.append(n)
        return n.id

    def _simple(self, kind: str, stmt, expr, defs: dict[str, bool] | None = None) -> int:
        nid = self._node(kind, stmt, stmt.lineno)
        n = self.nodes[nid]
        n.expr = expr
        n.uses = _uses(expr)
        walrus = _walrus_defs(expr)
        d = dict(defs or {})
        for w in walrus:
            d[w] = True
        n.defs = set(d)
        n.replaces = {k for k, v in d.items() if v}
        return nid

    def _list(self, stmts: list, follow: int, brk, cont, handlers: list[int]) -> int:
        nxt = follow
        for s in reversed(stmts):
            nxt = self._stmt(s, nxt, brk, cont, handlers)
        return nxt

    def _link(self, a: int, *bs: int) -> None:
        self.succ[a].update(bs)

    def _stmt(self, s: ast.stmt, follow: int, brk, cont, handlers: list[int]) -> int:
        start = len(self.nodes)
        nid = self._stmt_inner(s, follow, brk, cont, handlers)
        if handlers:   # any statement of a try body may raise into its handlers
            for n in range(start, len(self.nodes)):
                self._link(n, *handlers)
        return nid

    def _stmt_inner(self, s: ast.stmt, follow: int, brk, cont, handlers: list[int]) -> int:
        if isinstance(s, ast.If):
            n = self._simple("if", s, s.test)
            then_ = self._list(s.body, follow, brk, cont, handlers)
            else_ = self._list(s.orelse, follow, brk, cont, handlers) if s.orelse else follow
            self._link(n, then_, else_)
            return n
        if isinstance(s, ast.While):
            n = self._simple("while", s, s.test)
            else_ = self._list(s.orelse, follow, brk, cont, handlers) if s.orelse else follow
            body = self._list(s.body, n, follow, n, handlers)
            self._link(n, body, else_)
            return n
        if isinstance(s, (ast.For, ast.AsyncFor)):
            tn: dict[str, bool] = {}
            _target_names(s.target, tn)
            n = self._simple("for", s, s.iter, tn)
            n_uses = self.nodes[n].uses
            n_uses |= {k for k, v in tn.items() if not v}
            else_ = self._list(s.orelse, follow, brk, cont, handlers) if s.orelse else follow
            body = self._list(s.body, n, follow, n, handlers)
            self._link(n, body, else_)
            return n
        if isinstance(s, (ast.With, ast.AsyncWith)):
            tn = {}
            for item in s.items:
                if item.optional_vars is not None:
                    _target_names(item.optional_vars, tn)
            n = self._simple("with", s, ast.Tuple(elts=[i.context_expr for i in s.items], ctx=ast.Load()), tn)
            self._link(n, self._list(s.body, follow, brk, cont, handlers))
            return n
        if isinstance(s, (ast.Try, getattr(ast, "TryStar", ast.Try))):
            fin = self._list(s.finalbody, follow, brk, cont, handlers) if s.finalbody else follow
            hs = []
            for h in s.handlers:
                hn = self._simple("except", h, h.type, {h.name: True} if h.name else None)
                self._link(hn, self._list(h.body, fin, brk, cont, handlers))
                hs.append(hn)
            else_ = self._list(s.orelse, fin, brk, cont, handlers) if s.orelse else fin
            start = len(self.nodes)
            body = self._list(s.body, else_, brk, cont, handlers + hs)
            if s.finalbody:   # an exception no handler catches still runs the finally block
                for n in range(start, len(self.nodes)):
                    self._link(n, fin)
            return body
        if isinstance(s, ast.Match):
            n = self._simple("match", s, s.subject)
            nxt = follow
            for case in reversed(s.cases):
                binds = {x.name for x in ast.walk(case.pattern) if isinstance(x, (ast.MatchAs, ast.MatchStar))
                         and x.name}
                binds |= {x.rest for x in ast.walk(case.pattern) if isinstance(x, ast.MatchMapping) and x.rest}
                cn = self._simple("case", case.pattern, case.guard, {b: True for b in binds})
                self.nodes[cn].uses.add(_SUBJECT)
                self._link(cn, self._list(case.body, follow, brk, cont, handlers), nxt)
                nxt = cn
            self.nodes[n].defs.add(_SUBJECT)
            self.nodes[n].replaces.add(_SUBJECT)
            self._link(n, nxt)
            return n
        if isinstance(s, ast.Return):
            n = self._simple("return", s, s.value)
            self._link(n, self.exit)
            return n
        if isinstance(s, ast.Raise):
            n = self._simple("raise", s, ast.Tuple(elts=[e for e in (s.exc, s.cause) if e], ctx=ast.Load()))
            self._link(n, *(handlers or [self.exit]))
            return n
        if isinstance(s, ast.Break):
            n = self._simple("break", s, None)
            self._link(n, brk if brk is not None else self.exit)
            return n
        if isinstance(s, ast.Continue):
            n = self._simple("continue", s, None)
            self._link(n, cont if cont is not None else self.exit)
            return n
        if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            parts = list(s.decorator_list)
            if not isinstance(s, ast.ClassDef):
                parts += list(s.args.defaults) + [d for d in s.args.kw_defaults if d]
            else:
                parts += list(s.bases) + [k.value for k in s.keywords]
            n = self._simple("def", s, ast.Tuple(elts=parts, ctx=ast.Load()), {s.name: True})
            if not isinstance(s, ast.ClassDef):   # a closure reads the names it uses when it runs
                self.nodes[n].uses |= _uses(s) - {a.arg for a in ast.walk(s.args) if isinstance(a, ast.arg)}
            self._link(n, follow)
            return n
        defs: dict[str, bool] = {}
        expr: ast.AST | None = None
        if isinstance(s, ast.Assign):
            for t in s.targets:
                _target_names(t, defs)
            expr = ast.Tuple(elts=[s.value, *[t for t in s.targets if not isinstance(t, (ast.Name, ast.Tuple,
                                                                                         ast.List))]], ctx=ast.Load())
        elif isinstance(s, ast.AnnAssign):
            _target_names(s.target, defs)
            expr = s.value
            if not isinstance(s.target, ast.Name):
                expr = ast.Tuple(elts=[e for e in (s.value, s.target) if e], ctx=ast.Load())
            if s.value is None:
                defs = {}
        elif isinstance(s, ast.AugAssign):
            _target_names(s.target, defs)
            expr = ast.Tuple(elts=[s.value, s.target if not isinstance(s.target, ast.Name)
                                   else ast.Name(id=s.target.id, ctx=ast.Load())], ctx=ast.Load())
            defs = {k: isinstance(s.target, ast.Name) for k in defs}
        elif isinstance(s, (ast.Import, ast.ImportFrom)):
            defs = {(a.asname or a.name).split(".")[0]: True for a in s.names if a.name != "*"}
        elif isinstance(s, ast.Delete):
            expr = ast.Tuple(elts=list(s.targets), ctx=ast.Load())
        elif isinstance(s, ast.Expr):
            expr = s.value
            call = s.value
            if isinstance(call, ast.Await):
                call = call.value
            # items.append(v): a method called on a name as a statement may change what the name holds
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute):
                base = call.func.value
                while isinstance(base, (ast.Attribute, ast.Subscript)):
                    base = base.value
                if isinstance(base, ast.Name):
                    defs[base.id] = False
        elif isinstance(s, (ast.Global, ast.Nonlocal, ast.Pass)):
            expr = None
        elif isinstance(s, ast.Assert):
            expr = ast.Tuple(elts=[e for e in (s.test, s.msg) if e], ctx=ast.Load())
        else:
            expr = s
        n = self._simple("stmt", s, expr, defs)
        nd = self.nodes[n]
        nd.uses |= {k for k, v in defs.items() if not v}   # an update reads the object it changes
        self._link(n, follow)
        return n

    # -- analyses ---------------------------------------------------------------------------------

    def reaching(self) -> list[dict[str, set[int]]]:
        """For each node, the nodes whose definition of each name can reach it (before the node runs)."""
        n = len(self.nodes)
        preds: dict[int, set[int]] = defaultdict(set)
        for a, bs in self.succ.items():
            for b in bs:
                preds[b].add(a)
        IN = [defaultdict(set) for _ in range(n)]
        OUT = [defaultdict(set) for _ in range(n)]
        work = deque(range(n))
        queued = set(work)
        while work:
            i = work.popleft()
            queued.discard(i)
            inn: dict[str, set[int]] = defaultdict(set)
            for p in preds[i]:
                for var, ds in OUT[p].items():
                    inn[var] |= ds
            node = self.nodes[i]
            out = defaultdict(set, {v: set(ds) for v, ds in inn.items()})
            for v in node.defs:
                if v in node.replaces:
                    out[v] = {i}
                else:
                    out[v] = out[v] | {i}
            IN[i] = inn
            if out != OUT[i]:
                OUT[i] = out
                for s in self.succ[i]:
                    if s not in queued:
                        work.append(s)
                        queued.add(s)
        return IN

    def control_deps(self) -> dict[int, set[int]]:
        """``node -> the branch nodes it is control dependent on`` (from post-dominators; nodes that never reach
        the exit, inside an endless loop, depend on nothing)."""
        n = len(self.nodes)
        full = (1 << n) - 1
        pd = [full] * n
        pd[self.exit] = 1 << self.exit
        changed = True
        while changed:
            changed = False
            for i in range(n):
                if i == self.exit:
                    continue
                ss = self.succ.get(i)
                if not ss:
                    new = 1 << i
                else:
                    acc = full
                    for s in ss:
                        acc &= pd[s]
                    new = acc | (1 << i)
                if new != pd[i]:
                    pd[i] = new
                    changed = True

        def ipdom(i: int) -> int | None:
            strict = pd[i] & ~(1 << i)
            if not strict or strict == full & ~(1 << i):
                return None
            size = bin(strict).count("1")
            for d in range(n):
                if strict >> d & 1 and bin(pd[d]).count("1") == size:
                    return d
            return None

        ip = [ipdom(i) for i in range(n)]
        cd: dict[int, set[int]] = defaultdict(set)
        for a, bs in self.succ.items():
            if len(bs) < 2:
                continue
            for b in bs:
                if pd[a] >> b & 1:
                    continue
                runner, seen = b, set()
                while runner is not None and runner != ip[a] and runner not in seen:
                    seen.add(runner)
                    if runner != a:
                        cd[runner].add(a)
                    runner = ip[runner]
        return cd


_SUBJECT = "<match subject>"


# -- locating ---------------------------------------------------------------------------------------

def _functions(tree: ast.AST) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def function_at(tree: ast.AST, line: int):
    """The innermost function whose body holds ``line`` (None at module level)."""
    best = None
    for f in _functions(tree):
        start = min([f.lineno] + [d.lineno for d in f.decorator_list])
        if start <= line <= (f.end_lineno or f.lineno) and (best is None or f.lineno >= best.lineno):
            best = f
    return best


def _node_at(cfg: CFG, line: int) -> Node | None:
    """The node of a statement that holds ``line``: the innermost (latest starting) one."""
    best = None
    for n in cfg.nodes:
        if n.kind in ("entry", "exit") or n.stmt is None:
            continue
        end = getattr(n.stmt, "end_lineno", n.line) or n.line
        if n.kind in ("if", "while", "for", "with", "match", "except"):   # a header: its own lines only
            body = getattr(n.stmt, "body", None) or getattr(n.stmt, "cases", None)
            if body:
                first = body[0]
                start = getattr(first, "lineno", None) or first.pattern.lineno   # a match_case has no line
                end = max(n.line, start - 1)
        if n.line <= line <= end and (best is None or n.line >= best.line):
            best = n
    return best


def _call_at(stmt: ast.AST, line: int) -> ast.Call | None:
    calls = [c for c in ast.walk(stmt) if isinstance(c, ast.Call) and c.lineno <= line <= (c.end_lineno or c.lineno)]
    return max(calls, key=lambda c: (c.lineno, c.col_offset)) if calls else None


# -- slices -----------------------------------------------------------------------------------------

def _read(repo: Path, rel: str) -> tuple[ast.AST, list[str]]:
    p = repo / rel
    try:
        text = p.read_bytes().decode("utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise SliceError(f"{rel} cannot be read ({exc})") from None
    try:
        return ast.parse(text), text.splitlines()
    except (SyntaxError, ValueError, RecursionError) as exc:
        raise SliceError(f"{rel} does not parse as Python ({exc})") from None


def _line_text(lines: list[str], n: int) -> str:
    return lines[n - 1].strip()[:160] if 0 < n <= len(lines) else ""


class _Fn:
    """One function's graph and its analyses, built once per slice."""

    def __init__(self, rel: str, func, lines: list[str]):
        self.rel, self.func, self.lines = rel, func, lines
        self.cfg = CFG(func)
        self.reach = self.cfg.reaching()
        self.cd = self.cfg.control_deps()


def _criterion_vars(fn: _Fn, node: Node, var: str | None, arg: str | None, line: int) -> tuple[set[str], str]:
    if var:
        return {var}, f"`{var}`"
    if arg is not None:
        call = _call_at(node.stmt, line)
        if call is None:
            raise SliceError(f"no call at {fn.rel}:{line}")
        expr = None
        if arg.isdigit():
            pos = [a for a in call.args if not isinstance(a, ast.Starred)]
            if int(arg) < len(pos):
                expr = pos[int(arg)]
        else:
            expr = next((k.value for k in call.keywords if k.arg == arg), None)
        if expr is None:
            raise SliceError(f"the call at {fn.rel}:{line} has no argument {arg}")
        return _uses(expr), f"argument {arg} of the call at line {line} (`{ast.unparse(expr)[:60]}`)"
    return set(node.uses), "the values the line reads"


def backward(repo: Path, rel: str, line: int, *, var: str | None = None, arg: str | None = None,
             depth: int = 2, graph=None) -> dict:
    """The backward slice of ``rel:line`` (see the module docstring). ``var``: one name the line reads or
    defines; ``arg``: an argument (position or keyword) of the call on the line; neither: everything the line
    reads. ``depth``: calls up followed from a parameter (0: none)."""
    if not 0 <= depth <= MAX_DEPTH:
        raise SliceError(f"--depth must be 0..{MAX_DEPTH}")
    tree, lines = _read(repo, rel)
    func = function_at(tree, line)
    if func is None:
        raise SliceError(f"{rel}:{line} is not inside a function (a slice is computed inside one)")
    fn = _Fn(rel, func, lines)
    node = _node_at(fn.cfg, line)
    if node is None:
        raise SliceError(f"no statement at {rel}:{line}")
    want, what = _criterion_vars(fn, node, var, arg, line)
    if var and var not in node.uses and var not in node.defs:
        raise SliceError(f"`{var}` is neither read nor defined at {rel}:{line}")
    res = {"direction": "backward", "criterion": {"at": f"{rel}:{line}", "text": _line_text(lines, line),
                                                  "of": what, "function": func.name},
           "status": "strong_inference", "callers_followed": 0}
    out_lines, edges, params, free = _slice_in(fn, node, want, var is not None and var in node.defs
                                               and var not in node.uses)
    res.update(lines=out_lines, edges=edges, free=sorted(free))
    res["parameters"] = []
    if params and depth and graph is not None:
        res["_cache"] = {}   # files parsed and functions analysed once per slice, however many parameters
        res["parameters"] = _callers(repo, graph, fn, sorted(params), depth, res)
        del res["_cache"]
    elif params:
        res["parameters"] = [{"name": p, "callers": [], "note": "callers not followed" + (
            " (no index: run `verinoda scan`)" if graph is None and depth else "")} for p in sorted(params)]
    res["limits"] = list(LIMITS)
    return res


def _slice_in(fn: _Fn, node: Node, want: set[str], self_def: bool):
    """Backward closure inside one function from ``node``'s reads of ``want``: (lines, edges, parameters reached,
    names read with no definition in the function)."""
    cfg = fn.cfg
    roles: dict[int, set[str]] = defaultdict(set)
    roles[node.id].add("criterion")
    edges: list[dict] = []
    params: set[str] = set()
    free: set[str] = set()
    work: deque[tuple[int, str]] = deque()
    seen_pairs: set[tuple[int, str]] = set()
    seen_ctrl: set[int] = set()
    if self_def:   # the criterion defines the name: what it is made of
        want = set(node.uses)
    for v in want:
        work.append((node.id, v))
    ctrl_queue = deque([node.id])
    while work or ctrl_queue:
        while ctrl_queue:
            i = ctrl_queue.popleft()
            if i in seen_ctrl:
                continue
            seen_ctrl.add(i)
            for c in fn.cd.get(i, ()):
                roles[c].add("control")
                edges.append({"kind": "control", "from": _at(fn, c), "to": _at(fn, i)})
                for v in cfg.nodes[c].uses:
                    work.append((c, v))
                ctrl_queue.append(c)
        if not work:
            break
        i, v = work.popleft()
        if (i, v) in seen_pairs:
            continue
        seen_pairs.add((i, v))
        defs = fn.reach[i].get(v, set())
        if not defs:
            free.add(v)
            continue
        for d in defs:
            dn = cfg.nodes[d]
            edges.append({"kind": "data", "var": v, "from": _at(fn, d), "to": _at(fn, i)})
            if d == cfg.entry:
                params.add(v)
                continue
            roles[d].add("def")
            for u in dn.uses:   # an update (x.a = v) also reads x, so what x held before is followed too
                work.append((d, u))
            ctrl_queue.append(d)
    out = []
    for i, rs in sorted(roles.items(), key=lambda kv: cfg.nodes[kv[0]].line):
        n = cfg.nodes[i]
        out.append({"at": f"{fn.rel}:{n.line}", "text": _line_text(fn.lines, n.line), "role": sorted(rs),
                    "defines": sorted(x for x in n.defs if x != _SUBJECT),
                    "reads": sorted(x for x in n.uses if x != _SUBJECT)})
    uniq = []
    seen_e = set()
    for e in edges:
        k = (e["kind"], e.get("var"), e["from"], e["to"])
        if k not in seen_e:
            seen_e.add(k)
            uniq.append(e)
    return out, uniq, params, free


def _at(fn: _Fn, i: int) -> str:
    n = fn.cfg.nodes[i]
    if n.kind == "entry":
        return f"{fn.rel}:{n.line} (parameters)"
    return f"{fn.rel}:{n.line}"


def _graph_node(graph, rel: str, func) -> str | None:
    """The function's node: by its name and definition line, else by its name alone in the file (the index may
    be older than the file), the nearest definition when the name repeats."""
    named = [nid for nid in graph.symbols_in(rel) if graph.label(nid).strip(".()").rpartition(".")[2] == func.name]
    exact = [nid for nid in named if graph.line(nid) == func.lineno]
    if exact:
        return exact[0]
    if named:
        return min(named, key=lambda nid: abs((graph.line(nid) or 0) - func.lineno))
    return None


def _callers(repo: Path, graph, fn: _Fn, params: list[str], depth: int, res: dict) -> list[dict]:
    """For each parameter the slice reached: the argument each known caller passes, sliced in that caller."""
    nid = _graph_node(graph, fn.rel, fn.func)
    out = []
    method = bool(fn.cfg.params) and fn.cfg.params[0] in ("self", "cls")
    for p in params:
        entry: dict = {"name": p, "callers": []}
        if nid is None:
            entry["note"] = "the function is not in the index (run `verinoda update`): callers not followed"
            out.append(entry)
            continue
        if graph.line(nid) != fn.func.lineno:
            entry["note"] = (f"the index places {fn.func.name} at line {graph.line(nid)}, the file at "
                             f"{fn.func.lineno}: the index is older than the file, so call lines may have moved "
                             "(run `verinoda update`)")
        calls = sorted(graph.in_edges(nid, {"calls"}), key=lambda x: (str(x[1].get("source_file")),
                                                                     str(x[1].get("source_location"))))
        if len(calls) > MAX_CALLERS:
            entry["note"] = "; ".join(x for x in (entry.get("note"), f"{len(calls)} call sites: the first "
                                                                      f"{MAX_CALLERS} are followed") if x)
        for _u, d in calls[:MAX_CALLERS]:
            src = d.get("source_file")
            loc = str(d.get("source_location") or "")
            if not src or not loc.startswith("L") or not loc[1:].isdigit():
                continue
            cline = int(loc[1:])
            hop = {"at": f"{src}:{cline}", "edge": d.get("confidence") or "?"}
            try:
                hop.update(_caller_hop(repo, graph, fn, p, src, cline, method, depth, res))
            except SliceError as exc:
                hop["unknown"] = str(exc)
            entry["callers"].append(hop)
        out.append(entry)
    return out


def _caller_hop(repo: Path, graph, fn: _Fn, p: str, src: str, cline: int, method: bool, depth: int,
                res: dict) -> dict:
    cache = res.setdefault("_cache", {})
    if ("file", src) not in cache:
        cache[("file", src)] = _read(repo, src)
    tree, lines = cache[("file", src)]
    stmt_func = function_at(tree, cline)
    calls = [c for c in ast.walk(tree) if isinstance(c, ast.Call) and c.lineno <= cline <= (c.end_lineno or c.lineno)
             and _callee_name(c) == fn.func.name]
    if not calls:
        raise SliceError(f"no call of {fn.func.name} found at {src}:{cline}")
    call = calls[0]
    expr = _argument_for(fn, call, p, method)
    if expr is None:
        default = _default_for(fn, p)
        if default is None:
            raise SliceError(f"the call at {src}:{cline} passes nothing for `{p}` that can be read (*args or **kwargs)")
        return {"argument": f"default {ast.unparse(default)[:80]}", "text": _line_text(lines, cline),
                "from_default": f"{fn.rel}:{default.lineno}"}
    hop = {"argument": ast.unparse(expr)[:80], "text": _line_text(lines, cline)}
    if stmt_func is None:
        hop["note"] = "the call is at module level: the argument's names are module names"
        return hop
    key = ("fn", src, stmt_func.lineno, stmt_func.col_offset)
    if key not in cache:
        cache[key] = _Fn(src, stmt_func, lines)
    cfn = cache[key]
    node = _node_at(cfn.cfg, cline)
    if node is None:
        return hop
    res["callers_followed"] += 1
    sub_lines, sub_edges, sub_params, sub_free = _slice_in(cfn, node, _uses(expr), False)
    hop.update(lines=sub_lines, free=sorted(sub_free))
    if sub_params and depth > 1:
        hop["parameters"] = _callers(repo, graph, cfn, sorted(sub_params), depth - 1, res)
    elif sub_params:
        hop["parameters"] = [{"name": q, "callers": [], "note": "--depth reached"} for q in sorted(sub_params)]
    return hop


def _callee_name(call: ast.Call) -> str | None:
    f = call.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return None


def _argument_for(fn: _Fn, call: ast.Call, p: str, method: bool) -> ast.AST | None:
    args = fn.func.args
    positional = [a.arg for a in (*args.posonlyargs, *args.args)]
    if method and isinstance(call.func, ast.Attribute):
        positional = positional[1:]   # obj.m(x): self is the object
    for k in call.keywords:
        if k.arg == p:
            return k.value
    if p in positional:
        i = positional.index(p)
        pos = call.args
        if any(isinstance(a, ast.Starred) for a in pos[:i + 1]):
            return None
        if i < len(pos):
            return pos[i]
    return None


def _default_for(fn: _Fn, p: str) -> ast.AST | None:
    args = fn.func.args
    positional = [a.arg for a in (*args.posonlyargs, *args.args)]
    if p in positional and args.defaults:
        first = len(positional) - len(args.defaults)
        i = positional.index(p)
        if i >= first:
            return args.defaults[i - first]
    for a, d in zip(args.kwonlyargs, args.kw_defaults):
        if a.arg == p and d is not None:
            return d
    return None


def forward(repo: Path, rel: str, line: int, *, var: str | None = None) -> dict:
    """The forward slice of the definitions at ``rel:line`` (of ``var``, or of every name the line defines):
    every node they can reach and be read at, transitively, and the nodes those control."""
    tree, lines = _read(repo, rel)
    func = function_at(tree, line)
    if func is None:
        raise SliceError(f"{rel}:{line} is not inside a function (a slice is computed inside one)")
    fn = _Fn(rel, func, lines)
    cfg = fn.cfg
    node = _node_at(cfg, line)
    if node is None:
        raise SliceError(f"no statement at {rel}:{line}")
    start_vars = {var} if var else set(node.defs) - {_SUBJECT}
    if var and var not in node.defs:
        raise SliceError(f"`{var}` is not defined at {rel}:{line}")
    if not start_vars:
        raise SliceError(f"{rel}:{line} defines no name: nothing to follow forward")
    controls: dict[int, set[int]] = defaultdict(set)   # branch -> nodes it controls
    for n_, cs in fn.cd.items():
        for c in cs:
            controls[c].add(n_)
    roles: dict[int, set[str]] = defaultdict(set)
    roles[node.id].add("criterion")
    edges = []
    work = deque((node.id, v) for v in start_vars)
    seen = set()
    while work:
        d, v = work.popleft()
        if (d, v) in seen:
            continue
        seen.add((d, v))
        for i, n in enumerate(cfg.nodes):
            if v in n.uses and d in fn.reach[i].get(v, ()):
                roles[i].add("use")
                edges.append({"kind": "data", "var": v, "from": _at(fn, d), "to": _at(fn, i)})
                for w in n.defs:
                    work.append((i, w))
                if n.kind in ("if", "while", "for", "match", "case"):
                    for ctl in controls.get(i, ()):
                        roles[ctl].add("controlled")
                        edges.append({"kind": "control", "from": _at(fn, i), "to": _at(fn, ctl)})
                        for w in cfg.nodes[ctl].defs:
                            work.append((ctl, w))
    out = []
    for i, rs in sorted(roles.items(), key=lambda kv: cfg.nodes[kv[0]].line):
        n = cfg.nodes[i]
        if n.kind in ("entry", "exit"):
            continue
        out.append({"at": f"{rel}:{n.line}", "text": _line_text(lines, n.line), "role": sorted(rs),
                    "defines": sorted(x for x in n.defs if x != _SUBJECT),
                    "reads": sorted(x for x in n.uses if x != _SUBJECT)})
    return {"direction": "forward", "criterion": {"at": f"{rel}:{line}", "text": _line_text(lines, line),
                                                  "of": ", ".join(f"`{v}`" for v in sorted(start_vars)),
                                                  "function": func.name},
            "status": "strong_inference", "lines": out, "edges": edges, "limits": list(LIMITS)}


LIMITS = [
    "inside a function: statements as nodes, reaching definitions and control dependence from post-dominators",
    "the heap is approximated: x.a = v and x[i] = v update x without replacing it, a method called on a name as a "
    "statement may change it; aliasing, globals changed elsewhere, exec/eval and getattr are not seen",
    "an exception may leave any statement of a try body for its handlers; exceptions raised inside called "
    "functions are not followed",
    "callers come from the index's calls edges (INFERRED ones are a resolver's guess) and are matched by the "
    "callee's name at the cited line",
]


def render(res: dict, indent: str = "") -> str:
    c = res["criterion"]
    out = [f"{indent}{res['direction']} slice of {c['of']} at {c['at']} in {c['function']}() "
           f"({res['status']}): {len(res['lines'])} line(s)"]
    for ln in res["lines"]:
        out.append(f"{indent}  {ln['at']:<32} {','.join(ln['role']):<18} {ln['text']}")
    if res.get("free"):
        out.append(f"{indent}  not defined in the function (module, builtin or closure): {', '.join(res['free'])}")
    for p in res.get("parameters") or []:
        out.append(f"{indent}  parameter `{p['name']}`" + (f": {p['note']}" if p.get("note") else ""))
        for hop in p["callers"]:
            head = f"{indent}    called at {hop['at']} ({hop['edge']} edge) with {hop.get('argument', '?')}"
            out.append(head + (f": {hop['unknown']}" if hop.get("unknown") else ""))
            for ln in hop.get("lines") or []:
                out.append(f"{indent}      {ln['at']:<30} {','.join(ln['role']):<18} {ln['text']}")
            if hop.get("free"):
                out.append(f"{indent}      not defined in the caller: {', '.join(hop['free'])}")
            for q in hop.get("parameters") or []:
                out.append(f"{indent}      parameter `{q['name']}` of the caller: "
                           + (q.get("note") or f"{len(q['callers'])} caller(s) (--json)"))
    return "\n".join(out) + "\n"
