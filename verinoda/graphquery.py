"""A small declarative query language over the graph: node and edge patterns, bounded paths, negation, count.

``verinoda q`` reads a query such as::

    match (h:handler)-[calls*1..4]->(f:function)
    where f.text ~ "INSERT|UPDATE|DELETE|\\.commit\\("
    return f, h

and answers with rows, each citing its evidence: the nodes' definitions (``file:line``), every edge of the
paths that bound them (relation, confidence, ``file:line`` of the call), the lines a ``text`` condition matched
and the route declaration of a handler.

The language (keywords in any case)::

    query    := MATCH pattern (',' pattern)* [WHERE cond] [RETURN item (',' item)*] [LIMIT n]
    pattern  := node (edge node)*
    node     := '(' [var] [':' kind ('|' kind)*] ')'
    edge     := '-[' rels ']->'  |  '<-[' rels ']-'  |  '-[' rels ']-'          (out, in, either way)
    rels     := [':'] [relation ('|' relation)*] ['*' [lo] ['..' [hi]]]          (hi at most 10)
    cond     := cond OR cond | cond AND cond | NOT cond | '(' cond ')' | EXISTS pattern
              | var '.' field op value
    op       := '=' | '!=' | '~' (regular expression, searched) | GLOB | '<' | '<=' | '>' | '>='
    value    := 'string' | "string" | whole number | var '.' field
    item     := var | var '.' field | COUNT '(' var ')' | COUNT '(' '*' ')'

Kinds: ``file``, ``symbol`` (any code symbol), ``function`` (functions and methods), ``method``, ``class``,
``handler`` (a handler in the HTTP route table, ``verinoda routes``), ``test`` (code in a test file) and
``section`` (a heading of a document). Fields: ``name`` (the bare name), ``label``, ``file``, ``line``, ``id``
and ``text`` (the lines of the node's definition, nested definitions included; ``~`` only).

It is parsed by hand into a tree and evaluated by walking the graph: there is no ``eval``, no code of the query
is ever run, and a regular expression is compiled by :mod:`re` (300 characters at most). Evaluation has budgets
(rows, edge expansions, seconds); a result cut by one says so and its counts are lower bounds.

Status: graph edges are extractions, so a row is ``strong_inference`` at most when every
edge it rests on is ``EXTRACTED`` and ``weak_inference`` when one is ``INFERRED``; a row citing a file changed
since the index is ``unknown``. ``verify=True`` re-reads the cited lines (the call sites through
:func:`verinoda.entail.call_site`, the definitions; a route declaration must be a readable line of a Python file,
the route table being parsed from the current files) and raises a row to ``statically_verified`` only when every
part of it is confirmed in Python code and no part is an absence.
"""

from __future__ import annotations

import fnmatch
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

KINDS = ("file", "symbol", "function", "method", "class", "handler", "test", "section")
FIELDS = ("name", "label", "file", "line", "id", "text")
KEYWORDS = {"match", "where", "return", "limit", "and", "or", "not", "exists", "count", "glob"}
# relations Verinoda or its extractors make; one of these with no edge in a graph is answered (no row), not refused
KNOWN_RELATIONS = {"calls", "contains", "rationale_for", "references", "imports", "imports_from", "method",
                   "indirect_call", "uses", "inherits", "defines", "re_exports", "implements", "case_of", "embeds",
                   "listened_by", "references_constant", "bound_to", "mixes_in", "uses_config", "uses_static_prop",
                   "extends", "instantiates", "dispatches_to", "registers", "requests", "rpc_calls", "emits",
                   "mixin_target", "runs_function", "maps_to", "writes_table", "reads_table", "migrates", "injects"}
MAX_QUERY_CHARS = 4000
MAX_REGEX_CHARS = 300
MAX_HOPS = 10
DEFAULT_STAR_HOPS = 5
MAX_PATTERNS = 8
MAX_NODES_PER_PATTERN = 12
MAX_COND_DEPTH = 30
MAX_ROWS = 200
MAX_EXPANSIONS = 1_000_000
TIMEOUT_S = 30.0
LINES_PER_NODE = 3
EXAMPLES_PER_GROUP = 3
SMALL_START = 64           # a pattern end with at most this many candidates is walked from without comparing
STATUS_ORDER = ("statically_verified", "strong_inference", "weak_inference", "unknown")


class QueryError(ValueError):
    """A query that cannot be read or does not fit the graph; ``pos`` is the character offset of the problem."""

    def __init__(self, message: str, pos: int | None = None, text: str | None = None):
        self.message, self.pos, self.text = message, pos, text
        super().__init__(self.render())

    def where(self) -> dict | None:
        if self.pos is None or self.text is None:
            return None
        before = self.text[:self.pos]
        line = before.count("\n") + 1
        col = self.pos - (before.rfind("\n") + 1) + 1
        return {"offset": self.pos, "line": line, "column": col}

    def render(self) -> str:
        w = self.where()
        if w is None:
            return self.message
        src = self.text.split("\n")[w["line"] - 1]
        at = f"line {w['line']}, column {w['column']}" if "\n" in self.text else f"column {w['column']}"
        return f"{self.message} (at {at})\n  {src}\n  {' ' * (w['column'] - 1)}^"


class _Stop(Exception):
    def __init__(self, why: str):
        self.why = why


# -- tokens ---------------------------------------------------------------------------------------------

@dataclass
class Tok:
    kind: str     # ident, int, str, punct, kw, end
    value: object
    pos: int


_PUNCT = ("->", "<-", "<=", ">=", "!=", "..", "(", ")", "[", "]", ",", ":", "|", ".", "*", "-", "=", "~", "<",
          ">")


_DIGITS = frozenset("0123456789")
_WORD_TAIL = frozenset("_0123456789")


def tokenize(text: str) -> list[Tok]:
    out: list[Tok] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
            continue
        if c.isalpha() or c == "_":
            j = i + 1
            while j < n and (text[j].isalpha() or text[j] in _WORD_TAIL):
                j += 1
            word = text[i:j]
            out.append(Tok("kw", word.lower(), i) if word.lower() in KEYWORDS else Tok("ident", word, i))
            i = j
            continue
        if c in _DIGITS:   # ASCII digits only: `²` or `٣` are not numbers here
            j = i
            while j < n and text[j] in _DIGITS:
                j += 1
            if j < n and (text[j].isalpha() or text[j] == "_"):
                raise QueryError("a number runs into a name", j, text)
            if j - i > 9:
                raise QueryError("a number with more than 9 digits", i, text)
            out.append(Tok("int", int(text[i:j]), i))
            i = j
            continue
        if c in "'\"":
            j, buf = i + 1, []
            while True:
                if j >= n:
                    raise QueryError("a string is not closed", i, text)
                ch = text[j]
                if ch == "\\" and j + 1 < n and text[j + 1] in (c, "\\"):
                    buf.append(text[j + 1])   # only the quote and the backslash are escapes: `\.` stays `\.`
                    j += 2
                    continue
                if ch == c:
                    break
                buf.append(ch)
                j += 1
            out.append(Tok("str", "".join(buf), i))
            i = j + 1
            continue
        for p in _PUNCT:
            if text.startswith(p, i):
                out.append(Tok("punct", p, i))
                i += len(p)
                break
        else:
            raise QueryError(f"unexpected character {c!r}", i, text)
    out.append(Tok("end", None, n))
    return out


# -- syntax tree ----------------------------------------------------------------------------------------

@dataclass
class NodePat:
    var: str
    kinds: tuple[str, ...]
    pos: int
    named: bool


@dataclass
class RelPat:
    rels: tuple[str, ...] | None
    direction: str          # out, in, both
    lo: int
    hi: int
    pos: int


@dataclass
class Pattern:
    nodes: list[NodePat]
    rels: list[RelPat]
    pos: int

    def vars(self) -> set[str]:
        return {n.var for n in self.nodes}


@dataclass
class FieldRef:
    var: str
    field: str
    pos: int


@dataclass
class Cmp:
    left: FieldRef
    op: str
    value: object           # str, int or FieldRef
    pos: int
    rx: re.Pattern | None = None


@dataclass
class BoolOp:
    op: str                 # and, or
    items: list


@dataclass
class Not:
    item: object
    pos: int


@dataclass
class Exists:
    pattern: Pattern
    pos: int


@dataclass
class Item:
    kind: str               # var, field, count
    var: str | None
    field: str | None
    pos: int

    @property
    def name(self) -> str:
        if self.kind == "count":
            return f"count({self.var or '*'})"
        return f"{self.var}.{self.field}" if self.kind == "field" else str(self.var)


@dataclass
class Query:
    text: str
    patterns: list[Pattern]
    where: object | None
    items: list[Item]
    limit: int | None
    match_vars: list[str] = field(default_factory=list)


class _Parser:
    def __init__(self, text: str):
        self.text = text
        self.toks = tokenize(text)
        self.i = 0
        self.anon = 0

    # helpers
    def peek(self, k: int = 0) -> Tok:
        return self.toks[min(self.i + k, len(self.toks) - 1)]

    def next(self) -> Tok:
        t = self.toks[self.i]
        self.i = min(self.i + 1, len(self.toks) - 1)
        return t

    def is_(self, kind: str, value=None, k: int = 0) -> bool:
        t = self.peek(k)
        return t.kind == kind and (value is None or t.value == value)

    def err(self, msg: str, tok: Tok | None = None):
        t = tok or self.peek()
        got = "the end of the query" if t.kind == "end" else repr(str(t.value))
        raise QueryError(f"{msg}, found {got}", t.pos, self.text)

    def expect(self, kind: str, value=None, what: str | None = None) -> Tok:
        if not self.is_(kind, value):
            self.err(f"expected {what or repr(value) if value is not None else what or kind}")
        return self.next()

    def ident(self, what: str) -> Tok:
        t = self.peek()
        if t.kind == "kw":
            raise QueryError(f"`{t.value}` is a keyword, not a {what}", t.pos, self.text)
        return self.expect("ident", what=what)

    # grammar
    def query(self) -> Query:
        if self.is_("kw", "match"):
            self.next()
        elif not self.is_("punct", "("):
            self.err("a query starts with MATCH and a pattern such as (f:function)")
        patterns = [self.pattern()]
        while self.is_("punct", ","):
            self.next()
            patterns.append(self.pattern())
            if len(patterns) > MAX_PATTERNS:
                raise QueryError(f"more than {MAX_PATTERNS} patterns", patterns[-1].pos, self.text)
        where = None
        if self.is_("kw", "where"):
            self.next()
            where = self.cond(0)
        items: list[Item] = []
        if self.is_("kw", "return"):
            self.next()
            items.append(self.item())
            while self.is_("punct", ","):
                self.next()
                items.append(self.item())
        limit = None
        if self.is_("kw", "limit"):
            self.next()
            t = self.expect("int", what="a whole number after LIMIT")
            if t.value < 1:
                raise QueryError("LIMIT must be at least 1", t.pos, self.text)
            limit = int(t.value)
        if not self.is_("end"):
            self.err("expected WHERE, RETURN, LIMIT or the end of the query")
        return Query(self.text, patterns, where, items, limit)

    def pattern(self) -> Pattern:
        start = self.peek().pos
        nodes = [self.node()]
        rels: list[RelPat] = []
        while self.is_("punct", "-") or self.is_("punct", "<-"):
            rels.append(self.rel())
            nodes.append(self.node())
            if len(nodes) > MAX_NODES_PER_PATTERN:
                raise QueryError(f"a pattern of more than {MAX_NODES_PER_PATTERN} nodes", nodes[-1].pos, self.text)
        return Pattern(nodes, rels, start)

    def node(self) -> NodePat:
        t = self.expect("punct", "(", "'(' to start a node such as (f:function)")
        var, named = None, False
        if self.is_("ident") or self.is_("kw"):
            var, named = str(self.ident("variable name").value), True
        kinds: list[str] = []
        if self.is_("punct", ":"):
            self.next()
            while True:
                k = self.peek()
                if k.kind not in ("ident", "kw"):
                    self.err("expected a kind (" + ", ".join(KINDS) + ")")
                if str(k.value).lower() not in KINDS:
                    raise QueryError(f"unknown kind {k.value!r}; kinds: {', '.join(KINDS)}", k.pos, self.text)
                kinds.append(str(k.value).lower())
                self.next()
                if not self.is_("punct", "|"):
                    break
                self.next()
        self.expect("punct", ")", "')' to close the node")
        if var is None:
            self.anon += 1
            var = f"#{self.anon}"   # never a name the query can spell
        return NodePat(var, tuple(kinds), t.pos, named)

    def rel(self) -> RelPat:
        t = self.next()
        incoming = t.value == "<-"
        self.expect("punct", "[", "'[' after '-' (an edge is written -[calls]->)")
        if self.is_("punct", ":"):
            self.next()
        rels: list[str] = []
        if self.is_("ident") or self.is_("kw"):
            while True:
                r = self.peek()
                if r.kind not in ("ident", "kw"):
                    self.err("expected a relation name")
                if self.is_("punct", ":", 1):
                    raise QueryError("edge variables are not supported: write -[calls]-> (paths are cited in the "
                                     "evidence)", r.pos, self.text)
                rels.append(str(r.value))
                self.next()
                if not self.is_("punct", "|"):
                    break
                self.next()
        lo, hi = 1, 1
        if self.is_("punct", "*"):
            star = self.next()
            lo, hi = 1, DEFAULT_STAR_HOPS
            if self.is_("int"):
                lo = hi = int(self.next().value)
            if self.is_("punct", ".."):
                self.next()
                hi = int(self.next().value) if self.is_("int") else MAX_HOPS
                if not (self.is_("punct", "]")):
                    self.err("expected the end of the range ']'")
            if hi > MAX_HOPS:
                raise QueryError(f"a path of at most {MAX_HOPS} edges (*1..{MAX_HOPS})", star.pos, self.text)
            if lo > hi:
                raise QueryError(f"the range {lo}..{hi} is empty", star.pos, self.text)
        self.expect("punct", "]", "']' to close the edge")
        if incoming:
            self.expect("punct", "-", "'-' after ']' (an incoming edge is written <-[calls]-)")
            direction = "in"
        else:
            if self.is_("punct", "->"):
                self.next()
                direction = "out"
            elif self.is_("punct", "-"):
                self.next()
                direction = "both"
            else:
                self.err("expected '->' or '-' after ']'")
        return RelPat(tuple(rels) or None, direction, lo, hi, t.pos)

    # ``depth`` counts the parentheses and NOTs a condition sits in
    def cond(self, depth: int):
        items = [self.cond_and(depth)]
        while self.is_("kw", "or"):
            self.next()
            items.append(self.cond_and(depth))
        return items[0] if len(items) == 1 else BoolOp("or", items)

    def cond_and(self, depth: int):
        items = [self.cond_not(depth)]
        while self.is_("kw", "and"):
            self.next()
            items.append(self.cond_not(depth))
        return items[0] if len(items) == 1 else BoolOp("and", items)

    def cond_not(self, depth: int):
        if depth > MAX_COND_DEPTH:
            raise QueryError(f"the condition is nested in more than {MAX_COND_DEPTH} parentheses and NOTs",
                             self.peek().pos, self.text)
        if self.is_("kw", "not"):
            t = self.next()
            return Not(self.cond_not(depth + 1), t.pos)
        if self.is_("kw", "exists"):
            t = self.next()
            return Exists(self.pattern(), t.pos)
        if self.is_("punct", "("):
            # a parenthesised condition; a node pattern only follows EXISTS
            self.next()
            c = self.cond(depth + 1)
            self.expect("punct", ")", "')' to close the condition")
            return c
        left = self.field_ref()
        t = self.peek()
        if t.kind == "punct" and t.value in ("=", "!=", "~", "<", "<=", ">", ">="):
            op = str(self.next().value)
        elif t.kind == "kw" and t.value == "glob":
            op = "glob"
            self.next()
        elif t.kind == "punct" and t.value == "<-":
            raise QueryError("`<-` is an edge arrow here; write `< 1` with a space (numbers are whole and not "
                             "negative)", t.pos, self.text)
        else:
            self.err("expected an operator (=, !=, ~, glob, <, <=, >, >=)")
        v = self.peek()
        if v.kind in ("str", "int"):
            value = self.next().value
        elif v.kind == "ident":
            value = self.field_ref()
        else:
            self.err("expected a string, a whole number or var.field")
        return Cmp(left, op, value, t.pos)

    def field_ref(self) -> FieldRef:
        v = self.peek()
        if v.kind not in ("ident", "kw"):
            self.err("expected a condition such as f.name = \"save\", NOT ..., or EXISTS (pattern)")
        var = self.ident("variable name")
        self.expect("punct", ".", "'.' and a field after the variable (f.name)")
        f = self.peek()
        if f.kind not in ("ident", "kw") or str(f.value).lower() not in FIELDS:
            raise QueryError(f"unknown field {f.value!r}; fields: {', '.join(FIELDS)}", f.pos, self.text)
        self.next()
        return FieldRef(str(var.value), str(f.value).lower(), var.pos)

    def item(self) -> Item:
        t = self.peek()
        if t.kind == "kw" and t.value == "count":
            self.next()
            self.expect("punct", "(", "'(' after COUNT")
            pos = t.pos
            if self.is_("punct", "*"):
                self.next()
                var = None
            else:
                v = self.ident("variable name")
                var, pos = str(v.value), v.pos   # an unknown variable is pointed at, not COUNT
            self.expect("punct", ")", "')' to close COUNT")
            return Item("count", var, None, pos)
        var = self.ident("variable name or COUNT(...)")
        if self.is_("punct", "."):
            self.next()
            f = self.peek()
            if f.kind not in ("ident", "kw") or str(f.value).lower() not in FIELDS or str(f.value).lower() == "text":
                raise QueryError(f"unknown field {f.value!r} to return; fields: "
                                 f"{', '.join(x for x in FIELDS if x != 'text')}", f.pos, self.text)
            self.next()
            return Item("field", str(var.value), str(f.value).lower(), var.pos)
        return Item("var", str(var.value), None, var.pos)


def _walk_cond(c):
    stack = [c]
    while stack:
        x = stack.pop()
        if x is None:
            continue
        yield x
        if isinstance(x, BoolOp):
            stack.extend(x.items)
        elif isinstance(x, Not):
            stack.append(x.item)


def _cond_vars(c, match_vars: set[str]) -> set[str]:
    """The MATCH variables a condition reads (an EXISTS pattern's own new variables are not among them)."""
    out: set[str] = set()
    for x in _walk_cond(c):
        if isinstance(x, Cmp):
            out.add(x.left.var)
            if isinstance(x.value, FieldRef):
                out.add(x.value.var)
        elif isinstance(x, Exists):
            out |= x.pattern.vars() & match_vars
    return out


def parse(text: str) -> Query:
    """The query's tree, checked: variables bound by MATCH, fields and operators that fit, regular expressions
    that compile. :class:`QueryError` (with the position) otherwise."""
    if not (text or "").strip():
        raise QueryError("the query is empty")
    if len(text) > MAX_QUERY_CHARS:
        raise QueryError(f"the query is over {MAX_QUERY_CHARS} characters")
    q = _Parser(text).query()
    match_vars: list[str] = []
    for p in q.patterns:
        for n in p.nodes:
            if n.named and n.var not in match_vars:
                match_vars.append(n.var)
    q.match_vars = match_vars
    mv = set(match_vars)
    for x in _walk_cond(q.where):
        if isinstance(x, Cmp):
            for ref in (x.left, x.value):
                if isinstance(ref, FieldRef) and ref.var not in mv:
                    raise QueryError(f"`{ref.var}` is not a variable of MATCH", ref.pos, text)
            _check_cmp(x, text)
    for it in q.items:
        if it.var is not None and it.var not in mv:
            raise QueryError(f"`{it.var}` is not a variable of MATCH", it.pos, text)
    names = [it.name for it in q.items]
    for k, it in enumerate(q.items):
        if names[k] in names[:k]:
            raise QueryError(f"{it.name} is returned twice", it.pos, text)
    if not q.items and not match_vars:
        raise QueryError("name at least one node, such as (f:function), or RETURN COUNT(*)", q.patterns[0].pos,
                         text)
    return q


def _check_cmp(c: Cmp, text: str) -> None:
    f = c.left.field
    v = c.value
    if f == "text":
        if c.op != "~" or not isinstance(v, str):
            raise QueryError("text is matched with ~ \"regular expression\" only", c.pos, text)
    elif f == "line":
        if c.op in ("~", "glob"):
            raise QueryError("line is a number: compare it with =, !=, <, <=, >, >=", c.pos, text)
        if not isinstance(v, int) and not (isinstance(v, FieldRef) and v.field == "line"):
            raise QueryError("line is compared with a whole number or another .line", c.pos, text)
    else:
        if c.op in ("<", "<=", ">", ">="):
            raise QueryError(f"{f} is text: compare it with =, !=, ~ or glob", c.pos, text)
        if isinstance(v, int):
            raise QueryError(f"{f} is text: give a quoted string", c.pos, text)
        if isinstance(v, FieldRef) and (c.op in ("~", "glob") or v.field in ("line", "text")):
            raise QueryError("a field is compared with another field by = or != only, text with text", c.pos, text)
    if c.op == "~":
        if len(v) > MAX_REGEX_CHARS:
            raise QueryError(f"a regular expression of more than {MAX_REGEX_CHARS} characters", c.pos, text)
        try:
            c.rx = re.compile(v)
        except re.error as exc:
            raise QueryError(f"not a regular expression: {exc.msg}", c.pos, text) from None
        except (OverflowError, RecursionError, ValueError, MemoryError) as exc:   # `a{1,99999999999}`, deep groups
            raise QueryError(f"the regular expression cannot be compiled: {type(exc).__name__}", c.pos,
                             text) from None


# -- evaluation -----------------------------------------------------------------------------------------

def _weaker(a: str, b: str) -> str:
    return a if STATUS_ORDER.index(a) >= STATUS_ORDER.index(b) else b


def _bare(label: str) -> str:
    return str(label or "").strip().lstrip(".").split("(")[0]


# A hop is ``(source, target, data, forward)``: the edge in the graph's own direction, and whether the walk went
# from its source to its target (False: it was walked backwards, against the edge).
ABSENCE = "absence"   # marks, among a condition's witnesses, a NOT EXISTS that held
ABSENCE_NOTE = ("rests on an absence in the graph: an edge the extraction cannot see (dynamic dispatch, "
                "reflection, a call through a variable) is not there")


@dataclass
class _Binding:
    nodes: dict[str, str]
    paths: list[list[tuple[str, str, dict, bool]]]
    absence: bool = False


class _Eval:
    def __init__(self, g, q: Query, *, max_expansions: int, deadline: float, stale: set[str]):
        self.g, self.q = g, q
        self.max_expansions, self.deadline = max_expansions, deadline
        self.used = 0
        self.stale = stale
        self.kind_cache: dict[str, frozenset] = {}
        self.ok_cache: dict[tuple[str, str], bool] = {}
        self.text_hits: dict[str, dict[str, list[tuple[int, str]]]] = {}   # node -> regex -> lines
        self.lines_cache: dict[str, list[str] | None] = {}
        self._routes: dict[str, list[dict]] | None = None
        self.cands_cache: dict[str, list[str]] = {}
        mv = set(q.match_vars)
        self.match_vars = mv
        # kinds every occurrence of a MATCH variable asks for (each occurrence is one OR-set; all must hold)
        self.var_kinds: dict[str, list[tuple[str, ...]]] = {}
        for p in q.patterns:
            for n in p.nodes:
                if n.kinds:
                    self.var_kinds.setdefault(n.var, []).append(n.kinds)
        # the conjuncts of WHERE that read one variable are checked when it is bound
        self.pushed: dict[str, list] = {}
        self.residual: list = []
        conj = q.where.items if isinstance(q.where, BoolOp) and q.where.op == "and" else \
            ([q.where] if q.where is not None else [])
        for c in conj:
            vs = _cond_vars(c, mv)
            if len(vs) == 1 and not any(isinstance(x, Exists) for x in _walk_cond(c)):
                self.pushed.setdefault(next(iter(vs)), []).append(c)
            else:
                self.residual.append(c)
    # budget
    def spend(self, n: int = 1) -> None:
        self.used += n
        if self.used > self.max_expansions:
            raise _Stop("max_expansions")
        if (self.used & 0x3FF) < n and time.monotonic() > self.deadline:
            raise _Stop("timeout")

    def check_time(self) -> None:
        if time.monotonic() > self.deadline:
            raise _Stop("timeout")

    # routes
    def routes(self) -> dict[str, list[dict]]:
        if self._routes is None:
            from verinoda import cross_service, index

            self._routes = {}
            try:
                side = index._read_sidecar(self.g.root) or {}
                block = side.get("cross_service") or {}
                old = block.get("files") if block.get("facts_version") == cross_service.FACTS_VERSION else None
                _files, _edges, report, _parsed = cross_service.collect(self.g, old=old)
            except Exception:  # noqa: BLE001 - no route table: no node is a handler, and the result says so
                self.route_error = "the route table could not be read"
                return self._routes
            for r in report.get("route_table") or []:
                ms = "|".join(r["methods"]) if r.get("methods") else "ANY"
                self._routes.setdefault(r["handler"], []).append(
                    {"route": f"{ms} {r['path']}", "at": r["at"], "framework": r.get("fw")})
        return self._routes

    # nodes
    def kinds(self, n: str) -> frozenset:
        k = self.kind_cache.get(n)
        if k is not None:
            return k
        g = self.g
        G = g.G
        d = G.nodes[n]
        ks: set[str] = set()
        sf = str(d.get("source_file") or "").replace("\\", "/")
        if sf:
            # Graph.is_file_node, with string operations (it is asked for every node of a large graph)
            label = str(d.get("label") or "").replace("\\", "/")
            base = sf.rsplit("/", 1)[-1]
            is_file = label == base or label == sf or (label.endswith("/" + base) and sf.endswith(label))
            if is_file:
                ks.add("file")
            elif d.get("file_type") == "code":
                ks.add("symbol")
                lab = str(d.get("label") or "")
                owners, members = self._method_ends()
                if lab.endswith("()"):
                    ks.add("function")
                    if lab.startswith(".") or n in members:
                        ks.add("method")
                elif d.get("_callable_class") or n in owners:
                    ks.add("class")
            elif d.get("file_type") == "document" and g.is_heading(n):
                ks.add("section")
        if ks & {"file", "symbol"}:
            from verinoda.testcode import is_test_file

            if is_test_file(d.get("source_file")):
                ks.add("test")
        if "handler" in self._wanted_kinds() and n in self.routes():
            ks.add("handler")
        k = frozenset(ks)
        self.kind_cache[n] = k
        return k

    def _method_ends(self) -> tuple[set[str], set[str]]:
        """(classes, methods): the two ends of the graph's ``method`` edges (one pass over the edges)."""
        if not hasattr(self, "_methods"):
            owners, members = set(), set()
            for u, v, r in self.g.G.edges(data="relation"):
                if r == "method":
                    owners.add(u)
                    members.add(v)
            self._methods = (owners, members)
        return self._methods

    def _wanted_kinds(self) -> set[str]:
        if not hasattr(self, "_wanted"):
            w: set[str] = set()
            for p in self.q.patterns + [x.pattern for x in _walk_cond(self.q.where) if isinstance(x, Exists)]:
                for nd in p.nodes:
                    w.update(nd.kinds)
            self._wanted = w
        return self._wanted

    def node_lines(self, n: str) -> tuple[int, list[str]] | None:
        """(first line, lines) of the node's definition, read now (a file node: the whole file)."""
        g = self.g
        f = g.file(n)
        if not f:
            return None
        if f not in self.lines_cache:
            from verinoda.index import file_lines

            self.lines_cache[f] = file_lines(g.root / f)
        lines = self.lines_cache[f]
        if lines is None:
            return None
        if g.is_file_node(n):
            return 1, lines
        sp = g.span(n)
        if not sp:
            return None
        return sp[0], lines[sp[0] - 1:sp[1]]

    def value(self, n: str, fld: str):
        g = self.g
        if fld == "name":
            return g.label(n) if g.is_file_node(n) else _bare(g.label(n))
        if fld == "label":
            return str(g.label(n))
        if fld == "file":
            return g.file(n) or ""
        if fld == "line":
            return g.line(n)
        if fld == "id":
            return n
        raise AssertionError(fld)

    def text_match(self, n: str, c: Cmp) -> bool:
        got = self.text_hits.get(n, {}).get(c.value)
        if got is not None:
            return bool(got)
        span = self.node_lines(n)
        hits: list[tuple[int, str]] = []
        if span is not None:
            first, lines = span
            self.spend(1 + len(lines) // 50)
            for i, ln in enumerate(lines):
                self.check_time()   # per line: a slow pattern over a long definition stops at the deadline
                if c.rx.search(ln):
                    hits.append((first + i, ln.strip()[:160]))
                    if len(hits) >= LINES_PER_NODE:
                        break
        self.text_hits.setdefault(n, {})[c.value] = hits
        return bool(hits)

    def eval_cond(self, c, nodes: dict[str, str], ev: list | None = None) -> bool:
        """Whether ``c`` holds; ``ev`` receives the witnesses (EXISTS paths, :data:`ABSENCE` for a NOT EXISTS
        that held) of the branches that made it true, never those of a branch that failed."""
        if isinstance(c, BoolOp):
            if c.op == "and":
                local: list | None = [] if ev is not None else None
                for x in c.items:
                    if not self.eval_cond(x, nodes, local):
                        return False
                if ev is not None:
                    ev.extend(local)
                return True
            for x in c.items:
                local = [] if ev is not None else None
                if self.eval_cond(x, nodes, local):
                    if ev is not None:
                        ev.extend(local)
                    return True
            return False
        if isinstance(c, Not):
            held = not self.eval_cond(c.item, nodes, None)
            if held and ev is not None and any(isinstance(x, Exists) for x in _walk_cond(c.item)):
                ev.append(ABSENCE)
            return held
        if isinstance(c, Exists):
            for b in self.match_pattern(c.pattern, nodes, local=True):
                if ev is not None:
                    ev.extend(b.paths)
                return True
            return False
        n = nodes[c.left.var]
        if c.left.field == "text":
            return self.text_match(n, c)
        a = self.value(n, c.left.field)
        b = self.value(nodes[c.value.var], c.value.field) if isinstance(c.value, FieldRef) else c.value
        op = c.op
        if op == "=":
            return a == b
        if op == "!=":
            return a != b
        if op == "~":
            return bool(c.rx.search(str(a)))
        if op == "glob":
            return fnmatch.fnmatchcase(str(a), b)
        if a is None or b is None:
            return False
        return {"<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b}[op]

    def node_ok(self, nd: NodePat, n: str, local: bool) -> bool:
        if nd.kinds and not (self.kinds(n) & set(nd.kinds)):
            return False
        if local and nd.var not in self.match_vars:
            return True
        key = (nd.var, n)
        hit = self.ok_cache.get(key)
        if hit is not None:
            return hit
        self.spend()
        ok = all(self.kinds(n) & set(ks) for ks in self.var_kinds.get(nd.var, ()))
        if ok:
            ok = all(self.eval_cond(c, {nd.var: n}) for c in self.pushed.get(nd.var, ()))
        self.ok_cache[key] = ok
        return ok

    def cheap_cands(self, nd: NodePat) -> list[str]:
        """Nodes of the variable's kinds (the conditions are checked as each is used)."""
        key = nd.var if nd.var in self.match_vars else f"{nd.var}@{nd.pos}"
        got = self.cands_cache.get(key)
        if got is not None:
            return got
        sets = list(self.var_kinds.get(nd.var, ())) if nd.var in self.match_vars else []
        if nd.kinds:
            sets.append(nd.kinds)
        if any(ks == ("handler",) for ks in sets):
            pool = list(self.routes())
        else:
            pool = list(self.g.G.nodes)
        self.spend(len(pool) // 20 + 1)
        out = [n for n in pool if n in self.g.G and all(self.kinds(n) & set(ks) for ks in sets)]
        self.cands_cache[key] = out
        return out

    # edges
    def neighbours(self, u: str, rel: RelPat, avoid: frozenset = frozenset()):
        """``{v: best hop (source, target, data, forward)}`` one step from ``u``; ``avoid``: edges (by
        :func:`_edge_key`) the path already used, never walked again (an either-way walk would go back along
        the edge it came by)."""
        G = self.g.G
        rels = set(rel.rels) if rel.rels else None
        best: dict[str, tuple[str, str, dict, bool]] = {}
        sides = []
        if rel.direction in ("out", "both"):
            sides.append(((v, (u, v, d, True)) for _, v, d in G.out_edges(u, data=True)))
        if rel.direction in ("in", "both"):
            sides.append(((w, (w, u, d, False)) for w, _, d in G.in_edges(u, data=True)))
        for side in sides:
            for v, e in side:
                self.spend()
                if rels is not None and e[2].get("relation") not in rels:
                    continue
                if avoid and _edge_key(e) in avoid:
                    continue
                cur = best.get(v)
                if cur is None or _edge_rank(e[2]) < _edge_rank(cur[2]):
                    best[v] = e
        return best

    def step(self, u: str, rel: RelPat):
        """``(v, hops)`` reachable from ``u`` in ``lo..hi`` steps; one path per end node, the shortest (at the
        same length, one with no INFERRED edge when there is one). ``u`` itself is an end at length 0 when
        ``lo`` is 0, and at the length of a path back to it (a self-loop, a cycle) otherwise; no edge is used
        twice in one path."""
        if rel.lo == 0:
            yield u, []
        if rel.hi == 0:
            return
        if rel.lo <= 1:
            # breadth first: each node is an end once (the start only when it was not one at length 0) and is
            # expanded once
            seen = {u} if rel.lo == 0 else set()
            expanded = {u}
            frontier = {u: []}
            for depth in range(1, rel.hi + 1):
                nxt: dict[str, list] = {}
                for x, hops in frontier.items():
                    avoid = frozenset(_edge_key(h) for h in hops)
                    for v, e in self.neighbours(x, rel, avoid).items():
                        if v in seen:
                            continue
                        cand = hops + [e]
                        if v not in nxt or (_weak(nxt[v]) and not _weak(cand)):
                            nxt[v] = cand
                for v, hops in nxt.items():
                    seen.add(v)
                    yield v, hops
                frontier = {v: h for v, h in nxt.items() if v not in expanded}
                expanded.update(frontier)
                if not frontier:
                    return
            return
        # a lower bound above one: walks by level (a node may be reached at several lengths)
        emitted: set[str] = set()
        frontier = {u: []}
        for depth in range(1, rel.hi + 1):
            nxt = {}
            for x, hops in frontier.items():
                avoid = frozenset(_edge_key(h) for h in hops)
                for v, e in self.neighbours(x, rel, avoid).items():
                    cand = hops + [e]
                    if v not in nxt or (_weak(nxt[v]) and not _weak(cand)):
                        nxt[v] = cand
            if depth >= rel.lo:
                for v, hops in nxt.items():
                    if v not in emitted:
                        emitted.add(v)
                        yield v, hops
            frontier = nxt
            if not frontier:
                return

    # patterns
    def match_pattern(self, p: Pattern, bound: dict[str, str], local: bool = False):
        """Bindings extending ``bound`` that match ``p``; ``local``: an EXISTS pattern (its new variables are
        existential and carry no WHERE condition). The paths come back in the pattern's order, each hop in the
        order the pattern reads it, also when the pattern was walked from its other end."""
        nodes, rels = list(p.nodes), list(p.rels)
        first, last = nodes[0], nodes[-1]
        if first.var in bound:
            flip = False
        elif last.var in bound:
            flip = True
        elif len(nodes) == 1:
            flip = False
        else:
            # a small first end (the handlers, say) is started from without listing the other end's candidates
            n_first = len(self.cheap_cands(first))
            flip = n_first > SMALL_START and len(self.cheap_cands(last)) < n_first
        if flip:
            nodes = nodes[::-1]
            rels = [RelPat(r.rels, {"out": "in", "in": "out"}.get(r.direction, "both"), r.lo, r.hi, r.pos)
                    for r in rels[::-1]]
        start = nodes[0]
        if start.var in bound:
            starts = [bound[start.var]]
        else:
            starts = self.cheap_cands(start)
        for s in starts:
            self.spend()
            self.check_time()
            if start.var in bound:
                if start.kinds and not (self.kinds(s) & set(start.kinds)):
                    continue
            elif not self.node_ok(start, s, local):
                continue
            b = dict(bound)
            b[start.var] = s
            for nb, paths in self._extend(nodes, rels, 1, b, [], local):
                if flip:   # walked from the last node: each path, and each hop's direction, read the other way
                    paths = [[(a, z, d, not fwd) for a, z, d, fwd in path[::-1]] for path in paths[::-1]]
                yield _Binding(nb, paths)

    def _extend(self, nodes, rels, i, b, paths, local):
        if i == len(nodes):
            yield b, [list(x) for x in paths]
            return
        nd, rel = nodes[i], rels[i - 1]
        u = b[nodes[i - 1].var]
        for v, hops in self.step(u, rel):
            if nd.var in b:
                if b[nd.var] != v:
                    continue
                if nd.kinds and not (self.kinds(v) & set(nd.kinds)):
                    continue
                yield from self._extend(nodes, rels, i + 1, b, paths + [hops], local)
                continue
            if not self.node_ok(nd, v, local):
                continue
            b2 = dict(b)
            b2[nd.var] = v
            yield from self._extend(nodes, rels, i + 1, b2, paths + [hops], local)

    def bindings(self):
        """Every complete binding of MATCH that the WHERE condition keeps, with its paths."""
        yield from self._join(0, {}, [])

    def _join(self, k: int, b: dict, paths: list):
        pats = self.q.patterns
        if k == len(pats):
            witness: list = []
            for c in self.residual:
                if not self.eval_cond(c, b, witness):
                    return
            yield _Binding(b, paths + [w for w in witness if w is not ABSENCE], absence=ABSENCE in witness)
            return
        for m in self.match_pattern(pats[k], b):
            yield from self._join(k + 1, m.nodes, paths + m.paths)


def _edge_rank(d: dict) -> tuple:
    loc = str(d.get("source_location") or "")
    line = int(loc[1:]) if loc.startswith("L") and loc[1:].isdigit() else 10 ** 9
    return (d.get("confidence") != "EXTRACTED", line)


def _edge_key(e) -> tuple:
    """One edge of the multigraph: its ends and its data (two parallel edges have two data dicts)."""
    return (e[0], e[1], id(e[2]))


def _weak(hops) -> bool:
    return any(e[2].get("confidence") != "EXTRACTED" for e in hops)


def _at_edge(d: dict) -> str | None:
    loc = str(d.get("source_location") or "")
    f = d.get("source_file")
    if f and loc.startswith("L") and loc[1:].isdigit():
        return f"{f}:{loc[1:]}"
    return f or None


# -- the answer -----------------------------------------------------------------------------------------

def _node_view(ev: _Eval, n: str) -> dict:
    g = ev.g
    f, ln = g.file(n), g.line(n)
    return {"id": n, "label": str(g.label(n)), "kinds": sorted(ev.kinds(n)),
            "at": f"{f}:{ln}" if f and ln else f}


def _binding_status(ev: _Eval, b: _Binding) -> tuple[str, list[str], list[str]]:
    """(status, uncertainties, stale files) of one binding."""
    unc: list[str] = []
    files: set[str] = set()
    status = "strong_inference"
    for path in b.paths:
        for _u, _v, d, _fwd in path:
            if d.get("confidence") != "EXTRACTED":
                status = "weak_inference"
            if d.get("source_file"):
                files.add(d["source_file"])
    if status == "weak_inference":
        unc.append("an edge is INFERRED (which definition a name means was guessed by the extractor)")
    for n in b.nodes.values():
        f = ev.g.file(n)
        if f:
            files.add(f)
    stale = sorted(files & ev.stale)
    if stale:
        status = "unknown"
        unc.append("cites a file changed since the index: the row may no longer hold (`verinoda update`)")
    if b.absence:   # a NOT EXISTS that held on the branch that kept this row
        unc.append(ABSENCE_NOTE)
    return status, unc, stale


def _evidence(ev: _Eval, b: _Binding, named: list[str]) -> dict:
    g = ev.g
    out: dict = {"nodes": [], "paths": [], "lines": [], "routes": []}
    for var in named:
        n = b.nodes.get(var)
        if n is None:
            continue
        out["nodes"].append({"var": var, **{k: v for k, v in _node_view(ev, n).items() if k != "id"}})
        for rx, hits in ev.text_hits.get(n, {}).items():
            for ln, text in hits:
                out["lines"].append({"var": var, "at": f"{g.file(n)}:{ln}", "text": text, "pattern": rx})
        if "handler" in ev.kinds(n):
            for r in ev.routes().get(n, [])[:3]:
                out["routes"].append({"var": var, **r})
    for path in b.paths:
        # each hop is the edge as the graph has it (from -> to); `walked` says whether the pattern read it that
        # way ("forward") or against it ("backward": an incoming or either-way edge)
        out["paths"].append([{"from": str(g.label(u)), "to": str(g.label(v)), "from_id": u, "to_id": v,
                              "relation": d.get("relation"), "confidence": d.get("confidence"),
                              "at": _at_edge(d), "walked": "forward" if fwd else "backward"}
                             for u, v, d, fwd in path])
    return {k: v for k, v in out.items() if v}


def _project(ev: _Eval, b: _Binding, items: list[Item]) -> tuple[tuple, dict]:
    key, vals = [], {}
    for it in items:
        if it.kind == "count":
            continue
        n = b.nodes[it.var]
        if it.kind == "var":
            key.append(n)
            vals[it.name] = _node_view(ev, n)
        else:
            v = ev.value(n, it.field)
            key.append(v)
            vals[it.name] = v
    return tuple(key), vals


def run(repo: Path, text: str, *, graph=None, max_rows: int = MAX_ROWS, max_expansions: int = MAX_EXPANSIONS,
        timeout: float = TIMEOUT_S, verify: bool = False) -> dict:
    """Evaluate a query over the project's graph (see the module docstring). :class:`QueryError` for a query that
    cannot be read or names a relation the graph and Verinoda do not know."""
    from verinoda import freshness, index

    t0 = time.perf_counter()
    if max_rows < 1 or max_expansions < 1 or timeout <= 0:
        raise QueryError("--max-rows, --max-expansions and --timeout must be positive")
    q = parse(text)
    repo = Path(repo).resolve()
    g = graph if graph is not None else index.load(repo)
    present = {d.get("relation") for _u, _v, d in g.G.edges(data=True)}
    notes: list[str] = []
    for p in q.patterns + [x.pattern for x in _walk_cond(q.where) if isinstance(x, Exists)]:
        for r in p.rels:
            for name in r.rels or ():
                if name not in present and name not in KNOWN_RELATIONS:
                    raise QueryError(f"unknown relation {name!r}; this graph has: {', '.join(sorted(present))}",
                                     r.pos, text)
                if name not in present:
                    notes.append(f"no `{name}` edge in this graph")
    fresh = freshness.check(repo)
    stale = set(fresh.get("files") or [])
    deadline = time.monotonic() + timeout
    ev = _Eval(g, q, max_expansions=max_expansions, deadline=deadline, stale=stale)
    items = q.items or [Item("var", v, None, 0) for v in q.match_vars]
    aggregate = any(it.kind == "count" for it in items)
    named = [v for v in q.match_vars]
    cap = min(q.limit or max_rows, max_rows)
    rows: dict[tuple, dict] = {}
    groups: dict[tuple, dict] = {}
    stopped, limited = None, False
    try:
        for b in ev.bindings():
            key, vals = _project(ev, b, items)
            st, unc, stale_files = _binding_status(ev, b)
            if aggregate:
                grp = groups.get(key)
                if grp is None:
                    grp = groups[key] = {"values": vals, "sets": {}, "bindings": 0, "status": st,
                                         "examples": [], "uncertain": [], "stale_files": set()}
                grp["bindings"] += 1
                grp["status"] = _weaker(grp["status"], st) if grp["bindings"] > 1 else st
                for it in items:
                    if it.kind == "count" and it.var is not None:
                        grp["sets"].setdefault(it.var, set()).add(b.nodes[it.var])
                for u in unc:
                    if u not in grp["uncertain"]:
                        grp["uncertain"].append(u)
                grp["stale_files"].update(stale_files)
                if len(grp["examples"]) < EXAMPLES_PER_GROUP:
                    grp["examples"].append(_evidence(ev, b, named))
                continue
            row = rows.get(key)
            if row is None:
                if len(rows) >= cap:
                    if q.limit is not None and q.limit <= max_rows:
                        limited = True
                    else:
                        stopped = "max_rows"
                    break
                rows[key] = {"values": vals, "status": st, "bindings": 1, "evidence": _evidence(ev, b, named),
                             "uncertain": unc, "stale_files": stale_files, "_b": b}
            else:
                row["bindings"] += 1
                if STATUS_ORDER.index(st) < STATUS_ORDER.index(row["status"]):
                    # a binding with a stronger status cites the row
                    row.update(status=st, evidence=_evidence(ev, b, named), uncertain=unc, stale_files=stale_files,
                               _b=b)
    except _Stop as s:
        stopped = s.why
    out_rows: list[dict] = []
    unlisted = 0
    if aggregate:
        ordered = sorted(groups.values(), key=lambda r: (-r["bindings"], str(r["values"])))
        groups_total = len(ordered)
        for grp in ordered[:cap]:
            counts = {}
            for it in items:
                if it.kind == "count":
                    counts[it.name] = grp["bindings"] if it.var is None else len(grp["sets"].get(it.var, ()))
            out_rows.append({"values": {**grp["values"], **counts}, "status": grp["status"],
                             "bindings": grp["bindings"], "examples": grp["examples"],
                             "uncertain": grp["uncertain"], "stale_files": sorted(grp["stale_files"])})
        if not groups and not any(it.kind != "count" for it in items):
            # COUNT with no grouping key: one row, also when nothing matched
            out_rows.append({"values": {it.name: 0 for it in items}, "status": "strong_inference", "bindings": 0,
                             "examples": [], "uncertain": [], "stale_files": []})
        if groups_total > cap:
            # every group was counted; only the listing is cut
            limited = q.limit is not None and q.limit <= max_rows
            unlisted = groups_total - cap
    else:
        for row in sorted(rows.values(), key=lambda r: _sort_key(r["values"])):
            if verify:
                _verify_row(ev, row)
            row.pop("_b", None)
            out_rows.append(row)
    for row in out_rows:
        if not row["uncertain"]:
            row.pop("uncertain")
        if not row["stale_files"]:
            row.pop("stale_files")
    complete = stopped is None
    statuses: dict[str, int] = {}
    for row in out_rows:
        statuses[row["status"]] = statuses.get(row["status"], 0) + 1
    res = {"query": text, "columns": [it.name for it in items], "rows": out_rows, "row_count": len(out_rows),
           "statuses": statuses, "complete": complete, "truncated": stopped, "limited": limited,
           "expansions": ev.used, "seconds": round(time.perf_counter() - t0, 3), "verify": verify}
    if not complete:
        res["note"] = ({"max_rows": f"stopped at {max_rows} row(s) (--max-rows): more rows exist",
                        "max_expansions": f"stopped after {max_expansions} edge and node expansions "
                                          "(--max-expansions): rows may be missing",
                        "timeout": f"stopped after {timeout:g} s (--timeout): rows may be missing"}[stopped]
                       + ("; the counts and bindings are lower bounds" if aggregate or out_rows else ""))
    if limited:
        res["more"] = "LIMIT reached: more rows exist"
    if unlisted and not limited:
        res["more"] = f"{unlisted} more group(s) counted but not listed (--max-rows)"
    if aggregate:
        res["groups_total"] = len(groups)
    if fresh.get("checked") is False:
        notes.append(f"freshness not checked ({fresh.get('why')}): a file changed since the index is not "
                     "flagged")
    elif stale:
        notes.append(f"{len(stale)} file(s) changed since the index: rows citing them are `unknown`, and rows "
                     "through them may be missing or wrong (`verinoda update`)")
        res["stale"] = {"count": len(stale), "files": sorted(stale)[:20]}
    if "handler" in ev._wanted_kinds():
        n_handlers = len(ev.routes())
        notes.append(f"handler: {n_handlers} handler(s) in the route table (`verinoda routes`)"
                     + (f"; {ev.route_error}" if getattr(ev, "route_error", None) else ""))
    if any(ABSENCE_NOTE in r.get("uncertain", []) for r in out_rows):
        notes.append("NOT EXISTS reads an absence in the graph: an edge the extraction cannot see is not there "
                     "(the rows that rest on one say so)")
    if aggregate and verify:
        notes.append("--verify confirms rows, not counts: the counted rows were not re-read")
    res["notes"] = list(dict.fromkeys(notes))
    res["limits"] = ["graph edges are extractions: a row is strong_inference at most (weak_inference with an "
                     "INFERRED edge) unless --verify confirms every part of it in Python code",
                     "a path is cited once per pair of ends (the shortest; at that length one without an INFERRED "
                     "edge when there is one)",
                     "the time budget is checked between nodes, edges and the lines a text condition reads: one "
                     "regular expression that backtracks badly on one long line is not interrupted"]
    return res


def _sort_key(vals: dict) -> tuple:
    out = []
    for v in vals.values():
        if isinstance(v, dict):
            at = str(v.get("at") or "")
            f, _, ln = at.rpartition(":")
            out.append((f or at, int(ln) if ln.isdigit() else 0, v.get("label") or ""))
        else:
            out.append((str(v), 0, ""))
    return tuple(out)


def _verify_row(ev: _Eval, row: dict) -> None:
    """Re-read what the row cites; ``statically_verified`` only when every part is confirmed in Python code."""
    from verinoda import entail

    b: _Binding = row["_b"]
    g, repo = ev.g, ev.g.root
    problems: list[str] = []
    checked = 0
    if row["status"] == "unknown":
        row["verified"] = {"result": "not checked", "why": "a cited file changed since the index"}
        return
    for path in b.paths:
        for u, v, d, _fwd in path:
            at = _at_edge(d)
            rel = d.get("relation")
            if rel not in ("calls",):
                problems.append(f"{at}: a `{rel}` edge is not a call site this check can read")
                continue
            if d.get("confidence") != "EXTRACTED":
                problems.append(f"{at}: the edge is INFERRED")
                continue
            p, _, ln = (at or "").rpartition(":")
            if not p or not ln.isdigit():
                problems.append(f"{g.label(u)} -> {g.label(v)}: the edge has no line")
                continue
            if not p.endswith((".py", ".pyi")):
                problems.append(f"{at}: which definition a call binds to is checked for Python only")
                continue
            lab = str(g.label(u)).strip()
            caller = None if g.is_file_node(u) else lab
            try:
                gr = entail.call_site(repo, p, int(ln), str(g.label(v)), caller=caller, target_path=g.file(v),
                                      target_qual=str(g.label(v)), relation=rel)
            except (OSError, ValueError, RecursionError):
                gr = None
            checked += 1
            if gr is None or gr.grade != "full":
                problems.append(f"{at}: {gr.reason if gr is not None else 'unreadable'}")
    for var, n in b.nodes.items():
        f, ln = g.file(n), g.line(n)
        if "handler" in ev.kinds(n) and any("handler" in ks for ks in ev.var_kinds.get(var, ())):
            for r in ev.routes().get(n, [])[:1]:
                rf, _, rl = r["at"].rpartition(":")
                if not rf.endswith(".py"):
                    problems.append(f"{r['at']}: a route outside Python is read by pattern, not parsed")
                    continue
                if rf not in ev.lines_cache:
                    from verinoda.index import file_lines

                    ev.lines_cache[rf] = file_lines(g.root / rf)
                rlines = ev.lines_cache[rf] or []
                if not (rl.isdigit() and 0 < int(rl) <= len(rlines) and rlines[int(rl) - 1].strip()):
                    problems.append(f"{r['at']}: the route declaration line cannot be read")
                else:
                    checked += 1
        if not f:
            continue
        span = ev.node_lines(n)
        if span is None:
            problems.append(f"{f}: cannot be read")
            continue
        if g.is_file_node(n):
            checked += 1
            continue
        first, lines = span
        name = _bare(g.label(n))
        if not lines or not re.search(rf"(?<![\w]){re.escape(name)}(?![\w])", lines[0]):
            problems.append(f"{f}:{ln} does not name `{name}`")
        else:
            checked += 1
        if not f.endswith((".py", ".pyi")):
            problems.append(f"{f}:{ln}: definitions are re-read by name only outside Python")
    if b.absence:
        problems.append("the row rests on an absence in the graph, which re-reading lines cannot confirm")
    if problems:
        row["verified"] = {"result": "not confirmed", "checked": checked, "problems": problems[:8]}
    else:
        row["status"] = "statically_verified"
        row["verified"] = {"result": "confirmed", "checked": checked,
                           "how": "each call site names its target (entail.call_site), each definition line names "
                                  "its symbol, the matched lines were read now"}


# -- text -----------------------------------------------------------------------------------------------

def render(res: dict) -> str:
    head = (f"{res['row_count']} row(s)"
            + (" (" + ", ".join(f"{k}: {v}" for k, v in res["statuses"].items()) + ")" if res["statuses"] else "")
            + f"; {res['expansions']} expansion(s), {res['seconds']} s")
    out = [head]
    said: set[str] = set()
    for row in res["rows"]:
        cells = []
        shown = set()
        for k, v in row["values"].items():
            if isinstance(v, dict):
                cells.append(f"{k} = {v['label']} {v.get('at') or ''}".rstrip())
                shown.add(k)
            else:
                cells.append(f"{k} = {v}")
        more = f", {row['bindings']} binding(s)" if row.get("bindings", 1) > 1 else ""
        out.append("  " + "  ".join(cells) + f"  [{row['status']}{more}]")
        evs = [row["evidence"]] if "evidence" in row else row.get("examples", [])
        for k, e in enumerate(evs):
            pre = "    " if "evidence" in row else f"    e.g. #{k + 1} "
            for nd in e.get("nodes", []):
                if nd["var"] not in shown:
                    out.append(f"{pre}{nd['var']} = {nd['label']} at {nd.get('at')}")
            for r in e.get("routes", []):
                out.append(f"{pre}route {r['route']} at {r['at']} ({r['var']})")
            for p in e.get("paths", []):
                if not p:
                    continue
                # in the order the pattern reads it; an edge walked against its direction is drawn `<-rel-`
                fwd0 = p[0].get("walked", "forward") == "forward"
                s = str(p[0]["from"] if fwd0 else p[0]["to"])
                for h in p:
                    mark = "" if h["confidence"] == "EXTRACTED" else "?"
                    if h.get("walked", "forward") == "forward":
                        s += f" -{h['relation']}{mark}-> {h['to']} ({h['at']})"
                    else:
                        s += f" <-{h['relation']}{mark}- {h['from']} ({h['at']})"
                out.append(f"{pre}path {s}")
            for ln in e.get("lines", []):
                out.append(f"{pre}line {ln['at']}  {ln['text']}")
        if row.get("verified"):
            vf = row["verified"]
            out.append(f"    verify: {vf['result']}" + (f" - {'; '.join(vf['problems'][:3])}" if vf.get("problems")
                                                     else ""))
        for u in row.get("uncertain", []):
            if u not in said:   # said once; the status in brackets marks every row it applies to
                said.add(u)
                out.append(f"    uncertain: {u}")
    if res.get("note"):
        out.append(f"note: {res['note']}")
    if res.get("more"):
        out.append(f"note: {res['more']}")
    for n in res.get("notes", []):
        out.append(f"note: {n}")
    return "\n".join(out) + "\n"
