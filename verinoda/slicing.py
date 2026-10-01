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
from typing import Callable, NamedTuple

MAX_DEPTH = 3          # calls up a backward slice may follow
MAX_CALLERS = 8        # callers read per parameter
MAX_NODES = 4000       # a function larger than this is not analysed (its graph would be slow to solve)

_TRY_TYPES = (ast.Try, getattr(ast, "TryStar", ast.Try))
# nodes the graph needs that are no statement of their own: the way into a try body (from where an exception
# raised before its first statement reaches the handlers) and the dispatch of an exception leaving a finally block
_PSEUDO = ("entry", "exit", "try", "reraise")
_PREDICATES = ("if", "while", "for", "match", "case")


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


def _header_exprs(s: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> list[ast.AST]:
    """What a ``def`` or ``class`` statement evaluates where it stands: decorators, defaults and annotations, or
    bases and keywords."""
    parts: list[ast.AST] = list(s.decorator_list)
    if isinstance(s, ast.ClassDef):
        return parts + list(s.bases) + [k.value for k in s.keywords]
    a = s.args
    parts += list(a.defaults) + [d for d in a.kw_defaults if d]
    parts += [x.annotation for x in (*a.posonlyargs, *a.args, *a.kwonlyargs, a.vararg, a.kwarg)
              if x is not None and x.annotation is not None]
    return parts + ([s.returns] if s.returns is not None else [])


def _scope_free(scope: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> set[str]:
    """Names a nested function or class reads from the scope around it: what its body reads and does not bind
    for itself (parameters, assignments, loop, ``with`` and ``except`` targets, imports, nested definitions), and
    what the scopes nested in it read the same way. ``nonlocal`` names are the enclosing scope's, ``global`` ones
    are not; a class body's names are not seen by the functions inside it."""
    reads: set[str] = set()
    bound: set[str] = set()
    nonlocal_: set[str] = set()
    global_: set[str] = set()
    inner: set[str] = set()
    if not isinstance(scope, ast.ClassDef):
        a = scope.args
        bound |= {x.arg for x in (*a.posonlyargs, *a.args, *a.kwonlyargs, a.vararg, a.kwarg) if x is not None}

    def visit(n: ast.AST) -> None:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(n.name)
            for e in _header_exprs(n):
                reads.update(_uses(e))
            inner.update(_scope_free(n))
            return
        if isinstance(n, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            reads.update(_uses(n))
            bound.update(_walrus_defs(n))   # a walrus in a comprehension binds in the scope around it
            return
        if isinstance(n, ast.Lambda):
            reads.update(_uses(n))
            return
        if isinstance(n, ast.Global):
            global_.update(n.names)
            return
        if isinstance(n, ast.Nonlocal):
            nonlocal_.update(n.names)
            return
        if isinstance(n, ast.Name):
            (reads if isinstance(n.ctx, ast.Load) else bound).add(n.id)
            return
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            bound.update((x.asname or x.name).split(".")[0] for x in n.names if x.name != "*")
        elif isinstance(n, ast.ExceptHandler) and n.name:
            bound.add(n.name)
        elif isinstance(n, (ast.MatchAs, ast.MatchStar)) and n.name:
            bound.add(n.name)
        elif isinstance(n, ast.MatchMapping) and n.rest:
            bound.add(n.rest)
        for child in ast.iter_child_nodes(n):
            visit(child)

    for st in scope.body:
        visit(st)
    local = bound - nonlocal_
    if isinstance(scope, ast.ClassDef):
        return (reads - local - global_) | (inner - global_)
    return (reads | inner) - local - global_


def _first_line(s: ast.AST) -> int:
    """A statement's first line: its first decorator's for a decorated definition."""
    return min([s.lineno] + [d.lineno for d in getattr(s, "decorator_list", None) or ()])


# -- the control-flow graph -------------------------------------------------------------------------

class Node:
    __slots__ = ("id", "kind", "stmt", "line", "uses", "defs", "replaces", "expr")

    def __init__(self, nid: int, kind: str, stmt: ast.AST | None, line: int):
        self.id, self.kind, self.stmt, self.line = nid, kind, stmt, line
        self.uses: set[str] = set()
        self.defs: set[str] = set()
        self.replaces: set[str] = set()   # the defs that replace earlier ones (kill them)
        self.expr: ast.AST | None = None  # the expression a header node evaluates


class _Ctx(NamedTuple):
    """Where the ways out of a statement lead: ``brk`` and ``cont`` (None outside a loop) and ``ret`` give the
    target node, through every ``finally`` block in between (built when first needed); ``exc`` holds the nodes an
    exception raised by the statement reaches (none: not followed, and a ``raise`` leaves the function)."""
    brk: Callable[[], int] | None
    cont: Callable[[], int] | None
    ret: Callable[[], int]
    exc: tuple[int, ...]


class CFG:
    """The statements of one function as a control-flow graph (``ENTRY`` defines the parameters)."""

    def __init__(self, func: ast.FunctionDef | ast.AsyncFunctionDef):
        self.func = func
        self.nodes: list[Node] = []
        self.succ: dict[int, set[int]] = defaultdict(set)
        self._exc: tuple[int, ...] = ()   # where an exception raised by the node being made goes
        self.entry = self._node("entry", func, func.lineno)
        self.exit = self._node("exit", None, getattr(func, "end_lineno", func.lineno))
        args = func.args
        self.params = [a.arg for a in (*args.posonlyargs, *args.args)]
        self.params += [args.vararg.arg] if args.vararg else []
        self.params += [a.arg for a in args.kwonlyargs] + ([args.kwarg.arg] if args.kwarg else [])
        self.nodes[self.entry].defs = set(self.params)
        self.nodes[self.entry].replaces = set(self.params)
        first = self._list(func.body, self.exit, _Ctx(None, None, lambda: self.exit, ()))
        self.succ[self.entry].add(first)

    def _node(self, kind: str, stmt, line: int, raises: bool = True) -> int:
        if len(self.nodes) >= MAX_NODES:
            raise SliceError(f"the function has over {MAX_NODES} statements: not analysed")
        n = Node(len(self.nodes), kind, stmt, line)
        self.nodes.append(n)
        if raises and self._exc:   # any statement may raise into the handlers around it
            self._link(n.id, *self._exc)
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

    def _list(self, stmts: list, follow: int, ctx: _Ctx) -> int:
        nxt = follow
        for s in reversed(stmts):
            nxt = self._stmt(s, nxt, ctx)
        return nxt

    def _link(self, a: int, *bs: int) -> None:
        self.succ[a].update(bs)

    def _stmt(self, s: ast.stmt, follow: int, ctx: _Ctx) -> int:
        saved, self._exc = self._exc, ctx.exc
        try:
            return self._stmt_inner(s, follow, ctx)
        finally:
            self._exc = saved

    def _try(self, s, follow: int, ctx: _Ctx) -> int:
        """``try``: the body may raise before any of its statements and after each (into the handlers, else
        through ``finally``); every way out of the body, the handlers and ``else`` - falling through, ``return``,
        ``break``, ``continue``, an exception - runs the ``finally`` block, then goes on to its own target: a copy
        of the block per target, so each way out keeps where it leads."""
        outer = ctx
        if s.finalbody:
            copies: dict[int, int] = {}
            reraise: list[int] = []

            def through_fin(target: int) -> int:
                if target not in copies:
                    copies[target] = self._list(s.finalbody, target, outer)
                return copies[target]

            def leave() -> int:   # an exception the try does not catch, once the finally block has run
                if len(outer.exc) <= 1:
                    return outer.exc[0] if outer.exc else self.exit
                if not reraise:
                    r = self._node("reraise", None, s.lineno, raises=False)
                    self._link(r, *outer.exc)
                    reraise.append(r)
                return reraise[0]

            def route(f):
                return None if f is None else (lambda: through_fin(f()))

            inside = _Ctx(route(outer.brk), route(outer.cont), route(outer.ret), (through_fin(leave()),))
            fin = through_fin(follow)
        else:
            inside, fin = outer, follow
        hs = []
        for h in s.handlers:
            body = self._list(h.body, fin, inside)
            saved, self._exc = self._exc, ()   # matching an exception's type is not taken to raise
            try:
                hn = self._simple("except", h, h.type, {h.name: True} if h.name else None)
            finally:
                self._exc = saved
            self._link(hn, body)
            hs.append(hn)
        else_ = self._list(s.orelse, fin, inside) if s.orelse else fin
        body_ctx = inside._replace(exc=tuple(hs) + inside.exc)
        body = self._list(s.body, else_, body_ctx)
        t = self._node("try", s, s.lineno, raises=False)
        self._link(t, body, *body_ctx.exc)
        return t

    def _stmt_inner(self, s: ast.stmt, follow: int, ctx: _Ctx) -> int:
        if isinstance(s, ast.If):
            n = self._simple("if", s, s.test)
            then_ = self._list(s.body, follow, ctx)
            else_ = self._list(s.orelse, follow, ctx) if s.orelse else follow
            self._link(n, then_, else_)
            return n
        if isinstance(s, (ast.While, ast.For, ast.AsyncFor)):
            if isinstance(s, ast.While):
                n = self._simple("while", s, s.test)
            else:
                tn: dict[str, bool] = {}
                _target_names(s.target, tn)
                n = self._simple("for", s, s.iter, tn)
                self.nodes[n].uses |= {k for k, v in tn.items() if not v}
            else_ = self._list(s.orelse, follow, ctx) if s.orelse else follow
            loop = ctx._replace(brk=lambda f=follow: f, cont=lambda h=n: h)
            body = self._list(s.body, n, loop)
            self._link(n, body, else_)
            return n
        if isinstance(s, (ast.With, ast.AsyncWith)):
            tn = {}
            for item in s.items:
                if item.optional_vars is not None:
                    _target_names(item.optional_vars, tn)
            n = self._simple("with", s, ast.Tuple(elts=[i.context_expr for i in s.items], ctx=ast.Load()), tn)
            self._link(n, self._list(s.body, follow, ctx))
            return n
        if isinstance(s, _TRY_TYPES):
            return self._try(s, follow, ctx)
        if isinstance(s, ast.Match):
            n = self._simple("match", s, s.subject)
            nxt = follow
            for case in reversed(s.cases):
                binds = {x.name for x in ast.walk(case.pattern) if isinstance(x, (ast.MatchAs, ast.MatchStar))
                         and x.name}
                binds |= {x.rest for x in ast.walk(case.pattern) if isinstance(x, ast.MatchMapping) and x.rest}
                body = self._list(case.body, follow, ctx)
                cn = self._simple("case", case.pattern, case.guard, {b: True for b in binds})
                self.nodes[cn].uses.add(_SUBJECT)
                self._link(cn, body, nxt)
                nxt = cn
            self.nodes[n].defs.add(_SUBJECT)
            self.nodes[n].replaces.add(_SUBJECT)
            self._link(n, nxt)
            return n
        if isinstance(s, ast.Return):
            n = self._simple("return", s, s.value)
            self._link(n, ctx.ret())
            return n
        if isinstance(s, ast.Raise):
            n = self._simple("raise", s, ast.Tuple(elts=[e for e in (s.exc, s.cause) if e], ctx=ast.Load()))
            self._link(n, *(ctx.exc or (self.exit,)))
            return n
        if isinstance(s, ast.Break):
            n = self._simple("break", s, None)
            self._link(n, ctx.brk() if ctx.brk is not None else self.exit)
            return n
        if isinstance(s, ast.Continue):
            n = self._simple("continue", s, None)
            self._link(n, ctx.cont() if ctx.cont is not None else self.exit)
            return n
        if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            n = self._simple("def", s, ast.Tuple(elts=_header_exprs(s), ctx=ast.Load()), {s.name: True})
            # a class body runs here; a closure reads the names it uses when it runs
            self.nodes[n].uses |= _scope_free(s)
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
            # `del x` ends x (what it held reaches no later read); `del d[k]` changes what d holds
            for t in s.targets:
                _target_names(t, defs)
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

    def _order(self) -> list[int]:
        """The nodes in reverse postorder from the entry (each before its successors, loops aside), then those
        the entry does not reach."""
        seen = {self.entry}
        post: list[int] = []
        stack = [(self.entry, iter(sorted(self.succ.get(self.entry, ()))))]
        while stack:
            node, it = stack[-1]
            nxt = next(it, None)
            if nxt is None:
                stack.pop()
                post.append(node)
            elif nxt not in seen:
                seen.add(nxt)
                stack.append((nxt, iter(sorted(self.succ.get(nxt, ())))))
        order = post[::-1]
        return order + [i for i in range(len(self.nodes)) if i not in seen]

    def reaching(self) -> Reaching:
        """For each node, the nodes whose definition of each name can reach it (before the node runs)."""
        return Reaching(self)

    def control_deps(self) -> dict[int, set[int]]:
        """``node -> the branch nodes it is control dependent on`` (from post-dominators; nodes that never reach
        the exit, inside an endless loop, depend on nothing)."""
        n = len(self.nodes)
        full = (1 << n) - 1
        pd = [full] * n
        pd[self.exit] = 1 << self.exit
        order = self._order()[::-1]   # successors first: post-dominators flow backwards
        changed = True
        while changed:
            changed = False
            for i in order:
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
        # a node's post-dominators are itself and its immediate post-dominator's: find that one by its set
        by_set: dict[int, int] = {}
        for d in range(n):
            by_set.setdefault(pd[d], d)

        def ipdom(i: int) -> int | None:
            strict = pd[i] & ~(1 << i)
            if not strict or strict == full & ~(1 << i):
                return None
            return by_set.get(strict)

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


class Reaching:
    """Reaching definitions as bit sets: one bit per (node, name) definition, solved over the nodes in reverse
    postorder until nothing changes."""

    def __init__(self, cfg: CFG):
        self.site: list[int] = []                 # bit -> the node defining
        self.bit: dict[tuple[int, str], int] = {}
        self.mask: dict[str, int] = defaultdict(int)   # name -> the bits of its definitions
        gen = [0] * len(cfg.nodes)
        for node in cfg.nodes:
            for v in sorted(node.defs):
                b = len(self.site)
                self.site.append(node.id)
                self.bit[(node.id, v)] = b
                self.mask[v] |= 1 << b
                gen[node.id] |= 1 << b
        kill = [0] * len(cfg.nodes)
        for node in cfg.nodes:
            for v in node.replaces:
                kill[node.id] |= self.mask[v]
        preds: list[list[int]] = [[] for _ in cfg.nodes]
        for a, bs in cfg.succ.items():
            for b in bs:
                preds[b].append(a)
        order = cfg._order()
        IN = [0] * len(cfg.nodes)
        OUT = list(gen)
        changed = True
        while changed:
            changed = False
            for i in order:
                inn = 0
                for p in preds[i]:
                    inn |= OUT[p]
                IN[i] = inn
                out = (inn & ~kill[i]) | gen[i]
                if out != OUT[i]:
                    OUT[i] = out
                    changed = True
        self.IN = IN

    def get(self, i: int, v: str) -> set[int]:
        """The nodes whose definition of ``v`` reaches node ``i``."""
        m = self.IN[i] & self.mask.get(v, 0)
        out = set()
        while m:
            low = m & -m
            out.add(self.site[low.bit_length() - 1])
            m ^= low
        return out

    def has(self, i: int, v: str, d: int) -> bool:
        """Whether node ``d``'s definition of ``v`` reaches node ``i``."""
        b = self.bit.get((d, v))
        return b is not None and bool(self.IN[i] >> b & 1)


_SUBJECT = "<match subject>"


# -- locating ---------------------------------------------------------------------------------------

def _functions(tree: ast.AST) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def function_at(tree: ast.AST, line: int):
    """The innermost function whose body holds ``line`` (None at module level). A line of a nested function's
    decorators or ``def`` header belongs to the function around it, where the definition is a statement."""
    best = None
    for f in _functions(tree):
        if _first_line(f.body[0]) <= line <= (f.end_lineno or f.lineno) and (best is None or f.lineno >= best.lineno):
            best = f
    return best


def _nodes_at(cfg: CFG, line: int) -> list[Node]:
    """The node of the statement that holds ``line``, the innermost (latest starting) one; with the copies of it
    when it is in a ``finally`` block (one per way out of the ``try``)."""
    best, best_start = None, 0
    for n in cfg.nodes:
        if n.kind in _PSEUDO or n.stmt is None:
            continue
        start = _first_line(n.stmt) if n.kind == "def" else n.line
        end = getattr(n.stmt, "end_lineno", n.line) or n.line
        if n.kind in ("if", "while", "for", "with", "match", "except"):   # a header: its own lines only
            body = getattr(n.stmt, "body", None) or getattr(n.stmt, "cases", None)
            if body:
                first = body[0]
                begin = getattr(first, "lineno", None) or first.pattern.lineno   # a match_case has no line
                end = max(n.line, begin - 1)
        if start <= line <= end and (best is None or start >= best_start):
            best, best_start = n, start
    if best is None:
        return []
    return [n for n in cfg.nodes if n.stmt is best.stmt and n.kind == best.kind]


def _call_at(stmt: ast.AST, line: int) -> ast.Call | None:
    """The outermost call on ``line``: of the calls starting there the first and widest, else the outermost
    call around the line."""
    calls = [c for c in ast.walk(stmt) if isinstance(c, ast.Call) and c.lineno <= line <= (c.end_lineno or c.lineno)]
    starting = [c for c in calls if c.lineno == line] or calls
    return min(starting, key=lambda c: (c.lineno, c.col_offset, -(c.end_lineno or c.lineno),
                                        -(c.end_col_offset or 0))) if starting else None


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


def _class_names(tree: ast.AST) -> set[str]:
    return {n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}


class _Fn:
    """One function's graph and its analyses, built once per slice."""

    def __init__(self, rel: str, func, lines: list[str], tree: ast.AST | None = None):
        self.rel, self.func, self.lines = rel, func, lines
        self.tree = tree
        self.cfg = CFG(func)
        self.reach = self.cfg.reaching()
        self.cd = self.cfg.control_deps()
        # control dependence on statements only: a pseudo node passes on the branches it depends on
        self.rcd: dict[int, set[int]] = {}
        nodes = self.cfg.nodes
        for i, cs in self.cd.items():
            real, todo, seen = set(), list(cs), set()
            while todo:
                c = todo.pop()
                if c in seen:
                    continue
                seen.add(c)
                if nodes[c].kind in _PSEUDO:
                    todo.extend(self.cd.get(c, ()))
                else:
                    real.add(c)
            self.rcd[i] = real


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


def _part_for(t: ast.AST, value: ast.AST, var: str) -> set[str] | None:
    """The names the part of ``value`` assigned to ``var`` by target ``t`` reads (None: ``var`` is not in
    ``t``); the whole value when the parts do not line up."""
    names: dict[str, bool] = {}
    _target_names(t, names)
    if var not in names:
        return None
    if (isinstance(t, (ast.Tuple, ast.List)) and isinstance(value, (ast.Tuple, ast.List))
            and len(t.elts) == len(value.elts)
            and not any(isinstance(e, ast.Starred) for e in (*t.elts, *value.elts))):
        out: set[str] = set()
        for te, ve in zip(t.elts, value.elts):
            r = _part_for(te, ve, var)
            if r is not None:
                out |= r
        return out
    return _uses(value)


def _def_reads(node: Node, var: str) -> set[str]:
    """What the criterion's definition of ``var`` is made of: in ``a, b = x, y`` the ``x`` for ``a``; else every
    name the line reads."""
    s = node.stmt
    if (node.kind == "stmt" and isinstance(s, ast.Assign) and var not in _walrus_defs(s.value)
            and all(isinstance(t, (ast.Name, ast.Tuple, ast.List)) for t in s.targets)):
        parts = [p for p in (_part_for(t, s.value, var) for t in s.targets) if p is not None]
        if parts:
            return set().union(*parts)
    return set(node.uses)


def backward(repo: Path, rel: str, line: int, *, var: str | None = None, arg: str | None = None,
             depth: int = 2, graph=None) -> dict:
    """The backward slice of ``rel:line`` (see the module docstring). ``var``: one name the line reads or
    defines; ``arg``: an argument (position or keyword) of the call on the line; neither: everything the line
    reads. ``depth``: calls up followed from a parameter (0: none)."""
    if not 0 <= depth <= MAX_DEPTH:
        raise SliceError(f"--depth must be 0..{MAX_DEPTH}")
    if var and arg is not None:
        raise SliceError("give --var or --arg, not both")
    tree, lines = _read(repo, rel)
    func = function_at(tree, line)
    if func is None:
        raise SliceError(f"{rel}:{line} is not inside a function (a slice is computed inside one)")
    fn = _Fn(rel, func, lines, tree)
    nodes = _nodes_at(fn.cfg, line)
    if not nodes:
        raise SliceError(f"no statement at {rel}:{line}")
    node = nodes[0]
    want, what = _criterion_vars(fn, node, var, arg, line)
    if var and var not in node.uses and var not in node.defs:
        raise SliceError(f"`{var}` is neither read nor defined at {rel}:{line}")
    res = {"direction": "backward", "criterion": {"at": f"{rel}:{line}", "text": _line_text(lines, line),
                                                  "of": what, "function": func.name},
           "status": "strong_inference", "callers_followed": 0}
    if var is not None and var in node.defs and var not in node.uses:   # the line defines it: what it is made of
        want = _def_reads(node, var)
    out_lines, edges, params, free = _slice_in(fn, nodes, want)
    res.update(lines=out_lines, edges=edges, free=sorted(free))
    res["parameters"] = []
    if params and depth and graph is not None:
        res["_cache"] = {("file", rel): (tree, lines)}   # files parsed and functions analysed once per slice
        res["parameters"] = _callers(repo, graph, fn, sorted(params), depth, res)
        del res["_cache"]
    elif params:
        res["parameters"] = [{"name": p, "callers": [], "note": "callers not followed" + (
            " (no index: run `verinoda scan`)" if graph is None and depth else "")} for p in sorted(params)]
    res["limits"] = list(LIMITS)
    return res


def _lines_of(fn: _Fn, roles: dict[int, set[str]]) -> list[dict]:
    """One entry per source line (a line may hold several nodes: copies of a finally block, ``if a: return b``)."""
    by_line: dict[int, dict] = {}
    for i, rs in roles.items():
        n = fn.cfg.nodes[i]
        if n.kind in _PSEUDO:
            continue
        e = by_line.setdefault(n.line, {"at": f"{fn.rel}:{n.line}", "text": _line_text(fn.lines, n.line),
                                        "role": set(), "defines": set(), "reads": set()})
        e["role"] |= rs
        e["defines"] |= {x for x in n.defs if x != _SUBJECT}
        e["reads"] |= {x for x in n.uses if x != _SUBJECT}
    return [{**e, "role": sorted(e["role"]), "defines": sorted(e["defines"]), "reads": sorted(e["reads"])}
            for _ln, e in sorted(by_line.items())]


def _slice_in(fn: _Fn, nodes: list[Node], want: set[str]):
    """Backward closure inside one function from ``nodes``' reads of ``want``: (lines, edges, parameters reached,
    names read with no definition in the function)."""
    cfg = fn.cfg
    roles: dict[int, set[str]] = defaultdict(set)
    edges: dict[tuple, None] = {}   # (kind, name, from node, to node), in the order found
    params: set[str] = set()
    free: set[str] = set()
    work: deque[tuple[int, str]] = deque()
    seen_pairs: set[tuple[int, str]] = set()
    seen_ctrl: set[int] = set()
    for node in nodes:
        roles[node.id].add("criterion")
        for v in want:
            work.append((node.id, v))
    ctrl_queue = deque(n.id for n in nodes)
    while work or ctrl_queue:
        while ctrl_queue:
            i = ctrl_queue.popleft()
            if i in seen_ctrl:
                continue
            seen_ctrl.add(i)
            for c in fn.rcd.get(i, ()):
                roles[c].add("control")
                edges[("control", None, c, i)] = None
                for v in cfg.nodes[c].uses:
                    work.append((c, v))
                ctrl_queue.append(c)
        if not work:
            break
        i, v = work.popleft()
        if (i, v) in seen_pairs:
            continue
        seen_pairs.add((i, v))
        defs = fn.reach.get(i, v)
        if not defs:
            free.add(v)
            continue
        for d in defs:
            dn = cfg.nodes[d]
            edges[("data", v, d, i)] = None
            if d == cfg.entry:
                params.add(v)
                continue
            roles[d].add("def")
            for u in dn.uses:   # an update (x.a = v) also reads x, so what x held before is followed too
                work.append((d, u))
            ctrl_queue.append(d)
    return _lines_of(fn, roles), _edges_of(fn, edges), params, free


def _edges_of(fn: _Fn, edges: dict[tuple, None]) -> list[dict]:
    """The edges as ``file:line`` pairs, once each (copies of a finally block share their lines)."""
    at: dict[int, str] = {}
    out, seen = [], set()
    for kind, var, a, b in edges:
        for i in (a, b):
            if i not in at:
                at[i] = _at(fn, i)
        k = (kind, var, at[a], at[b])
        if k in seen:
            continue
        seen.add(k)
        out.append({"kind": kind, "var": var, "from": at[a], "to": at[b]} if var is not None
                   else {"kind": kind, "from": at[a], "to": at[b]})
    return out


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
    for p in params:
        entry: dict = {"name": p, "callers": []}
        if nid is None:
            entry["note"] = "the function is not in the index (run `verinoda update`): callers not followed"
            out.append(entry)
            continue
        notes = []
        if graph.line(nid) != fn.func.lineno:
            notes.append(f"the index places {fn.func.name} at line {graph.line(nid)}, the file at "
                         f"{fn.func.lineno}: the index is older than the file, so call lines may have moved "
                         "(run `verinoda update`)")
        if fn.func.name == "__init__":
            notes.append("calls of the class (`Cls(...)`) are not followed to __init__: only calls of the name "
                         "`__init__` are")
        calls = sorted(graph.in_edges(nid, {"calls"}), key=lambda x: (str(x[1].get("source_file")),
                                                                     str(x[1].get("source_location"))))
        if len(calls) > MAX_CALLERS:
            notes.append(f"{len(calls)} call sites: the first {MAX_CALLERS} are followed")
        if notes:
            entry["note"] = "; ".join(notes)
        for _u, d in calls[:MAX_CALLERS]:
            src = d.get("source_file")
            loc = str(d.get("source_location") or "")
            if not src or not loc.startswith("L") or not loc[1:].isdigit():
                continue
            cline = int(loc[1:])
            hop = {"at": f"{src}:{cline}", "edge": d.get("confidence") or "?"}
            try:
                hop.update(_caller_hop(repo, graph, fn, p, src, cline, depth, res))
            except SliceError as exc:
                hop["unknown"] = str(exc)
            entry["callers"].append(hop)
        out.append(entry)
    return out


def _caller_hop(repo: Path, graph, fn: _Fn, p: str, src: str, cline: int, depth: int, res: dict) -> dict:
    cache = res.setdefault("_cache", {})
    if ("file", src) not in cache:
        cache[("file", src)] = _read(repo, src)
    tree, lines = cache[("file", src)]
    for f, t in ((fn.rel, fn.tree), (src, tree)):   # class names of the callee's file and the caller's
        if ("classes", f) not in cache:
            cache[("classes", f)] = _class_names(t) if t is not None else set()
    stmt_func = function_at(tree, cline)
    calls = [c for c in ast.walk(tree) if isinstance(c, ast.Call) and c.lineno <= cline <= (c.end_lineno or c.lineno)
             and _callee_name(c) == fn.func.name]
    if not calls:
        raise SliceError(f"no call of {fn.func.name} found at {src}:{cline}")
    call = calls[0]
    expr = _argument_for(fn, call, p, cache[("classes", fn.rel)] | cache[("classes", src)])
    if expr is _SPREAD:
        raise SliceError(f"the call at {src}:{cline} may pass `{p}` through *args or **kwargs: its value is unknown")
    if expr is None:
        default = _default_for(fn, p)
        if default is None:
            raise SliceError(f"the call at {src}:{cline} passes nothing for `{p}` that can be read")
        return {"argument": f"default {ast.unparse(default)[:80]}", "text": _line_text(lines, cline),
                "from_default": f"{fn.rel}:{default.lineno}"}
    hop = {"argument": ast.unparse(expr)[:80], "text": _line_text(lines, cline)}
    if stmt_func is None:
        hop["note"] = "the call is at module level: the argument's names are module names"
        return hop
    key = ("fn", src, stmt_func.lineno, stmt_func.col_offset)
    if key not in cache:
        cache[key] = _Fn(src, stmt_func, lines, tree)
    cfn = cache[key]
    nodes = _nodes_at(cfn.cfg, cline)
    if not nodes:
        return hop
    res["callers_followed"] += 1
    sub_lines, sub_edges, sub_params, sub_free = _slice_in(cfn, nodes, _uses(expr))
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


_SPREAD = object()   # the argument may come from *args or **kwargs at the call


def _argument_for(fn: _Fn, call: ast.Call, p: str, classes: set[str]) -> ast.AST | object | None:
    """The expression a call passes for parameter ``p``: by keyword, else by position (the first parameter of a
    method is the object before the dot - except an instance method called through its class, ``Cls.m(obj,
    x)``); ``_SPREAD`` when ``*args`` or ``**kwargs`` may carry it; None when nothing does (the default)."""
    args = fn.func.args
    positional = [a.arg for a in (*args.posonlyargs, *args.args)]
    first = positional[0] if positional else None
    if first in ("self", "cls") and isinstance(call.func, ast.Attribute):
        base = call.func.value
        through_class = isinstance(base, ast.Name) and base.id in classes
        if not (first == "self" and through_class):
            positional = positional[1:]   # obj.m(x), Cls.cm(x): the object or class is the first parameter
    for k in call.keywords:
        if k.arg == p:
            return k.value
    spread_kw = any(k.arg is None for k in call.keywords)
    if p in positional:
        i = positional.index(p)
        pos = call.args
        if any(isinstance(a, ast.Starred) for a in pos[:i + 1]):
            return _SPREAD
        if i < len(pos):
            return pos[i]
        if spread_kw or any(isinstance(a, ast.Starred) for a in pos):
            return _SPREAD
        return None
    return _SPREAD if spread_kw else None


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
    every node they can reach and be read at, transitively, and the nodes those control (and the nodes the
    branches among them control, in turn)."""
    tree, lines = _read(repo, rel)
    func = function_at(tree, line)
    if func is None:
        raise SliceError(f"{rel}:{line} is not inside a function (a slice is computed inside one)")
    fn = _Fn(rel, func, lines, tree)
    cfg = fn.cfg
    nodes = _nodes_at(cfg, line)
    if not nodes:
        raise SliceError(f"no statement at {rel}:{line}")
    node = nodes[0]
    start_vars = {var} if var else set(node.defs) - {_SUBJECT}
    if var and var not in node.defs:
        raise SliceError(f"`{var}` is not defined at {rel}:{line}")
    if not start_vars:
        raise SliceError(f"{rel}:{line} defines no name: nothing to follow forward")
    controls: dict[int, set[int]] = defaultdict(set)   # branch -> statements it controls
    for n_, cs in fn.rcd.items():
        if cfg.nodes[n_].kind in _PSEUDO:
            continue
        for c in cs:
            controls[c].add(n_)
    readers: dict[str, list[int]] = defaultdict(list)
    for n_ in cfg.nodes:
        for v in n_.uses:
            readers[v].append(n_.id)
    roles: dict[int, set[str]] = defaultdict(set)
    for n_ in nodes:
        roles[n_.id].add("criterion")
    edges: dict[tuple, None] = {}
    work = deque((n_.id, v) for n_ in nodes for v in start_vars)
    seen: set[tuple[int, str]] = set()
    branched: set[int] = set()
    while work:
        d, v = work.popleft()
        if (d, v) in seen:
            continue
        seen.add((d, v))
        for i in readers.get(v, ()):
            if not fn.reach.has(i, v, d):
                continue
            n = cfg.nodes[i]
            roles[i].add("use")
            edges[("data", v, d, i)] = None
            for w in n.defs:
                work.append((i, w))
            if n.kind not in _PREDICATES:
                continue
            branches = [i]   # a branch the value decides, and the branches it controls in turn
            while branches:
                b = branches.pop()
                if b in branched:
                    continue
                branched.add(b)
                for ctl in controls.get(b, ()):
                    roles[ctl].add("controlled")
                    edges[("control", None, b, ctl)] = None
                    for w in cfg.nodes[ctl].defs:
                        work.append((ctl, w))
                    if cfg.nodes[ctl].kind in _PREDICATES:
                        branches.append(ctl)
    return {"direction": "forward", "criterion": {"at": f"{rel}:{line}", "text": _line_text(lines, line),
                                                  "of": ", ".join(f"`{v}`" for v in sorted(start_vars)),
                                                  "function": func.name},
            "status": "strong_inference", "lines": _lines_of(fn, roles), "edges": _edges_of(fn, edges),
            "limits": list(LIMITS)}


LIMITS = [
    "inside a function: statements as nodes, reaching definitions and control dependence from post-dominators",
    "the heap is approximated: x.a = v and x[i] = v update x without replacing it, a method called on a name as a "
    "statement may change it; aliasing, globals changed elsewhere, exec/eval and getattr are not seen",
    "an exception may leave any statement of a try body for its handlers, and any statement of the body, the "
    "handlers or else for finally; exceptions raised inside called functions are not followed",
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
