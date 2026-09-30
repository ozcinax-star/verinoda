"""Code health: complexity, nesting and near-duplicate functions, counted on the syntax tree.

``verinoda health`` (functions ranked by health, near-duplicate pairs) and the ``health`` concern of
``verinoda review`` (a changed function whose health dropped, a clone added next to it). Per function or method:

* **cyclomatic** complexity (McCabe): 1 + branches (``if``/``elif``, loops, ``except``/``catch``, case arms, the
  conditional expression, comprehension ``for``/``if``) + boolean operators (``a and b or c`` adds 2);
* **cognitive** complexity (after SonarSource's definition): +1 per branch or loop plus its nesting level,
  +1 per ``elif``/``else``, +1 per sequence of one boolean operator; lambdas nest without adding;
* **nesting**: the deepest level of nested branches, loops, handlers and ``match``/``switch``, with its line;
* **lines** of the definition and **params** (``self``/``cls`` not counted).

Python is read with :mod:`ast`; every language :mod:`verinoda.anchors` has a tree-sitter grammar for is read from
its tree with one table of node types (a construct a grammar names otherwise is not counted). A nested function
or class is its own entry, not part of its parent's counts. The metrics are counted, so ``statically_verified``; the **health** score (10 = no smell, 1 = worst) is a
heuristic over thresholds (:data:`SMELLS`), so ``strong_inference`` at most, and so is a clone: two functions
whose token sequences, names and literals normalised, have a similarity ratio (:class:`difflib.SequenceMatcher`)
of at least ``min_similarity``. Nothing here runs the project's code.
"""

from __future__ import annotations

import ast
import bisect
import difflib
import io
import keyword
import tokenize
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

# smell -> (metric, thresholds): each threshold reached takes one point off the health score
SMELLS: dict[str, tuple[str, tuple[int, ...]]] = {
    "complex_method": ("cyclomatic", (10, 20, 30)),
    "hard_to_read": ("cognitive", (15, 25, 40)),
    "deep_nesting": ("nesting", (4, 5, 6)),
    "long_method": ("lines", (70, 150, 300)),
    "many_parameters": ("params", (6, 9, 12)),
}
BANDS = (("healthy", 9), ("problematic", 5), ("unhealthy", 1))
MIN_SIMILARITY = 0.9
MIN_TOKENS = 50
MAX_COMPARISONS = 20000
MAX_WORK = 50_000_000      # pairs of equal tokens the similarity ratios scan (a few seconds per 10 million)
MAX_CLONE_TOKENS = 1500    # a longer function is not compared (one ratio could take seconds)
DEFAULT_LIMIT = 20
_SHINGLE = 5
_COMMON_SHINGLE = 25   # a shingle in more functions than this says nothing about which two are alike
LIMITS = [
    "metrics count syntax only: what a call does, recursion, early exits and data-dependent paths are not weighed",
    "cognitive complexity follows SonarSource's rules without the increments for recursion and labelled jumps",
    "the health score is a heuristic over fixed thresholds (verinoda/health.py SMELLS), not a quality verdict",
    "clones are compared on normalised tokens (names and literals replaced): two functions of the same shape "
    "that do different things can score high; a pair must share token runs to be compared at all",
    f"a function of more than {MAX_CLONE_TOKENS} normalised tokens is not compared for clones",
    "tree-sitter languages are counted from one table of node types (verinoda/health.py _TS_*): a construct a "
    "grammar names otherwise is not counted",
]


@dataclass
class FnMetrics:
    file: str
    qual: str
    start: int
    end: int
    cyclomatic: int = 1
    cognitive: int = 0
    nesting: int = 0
    deepest: int | None = None
    params: int = 0
    smells: list[dict] = field(default_factory=list)
    health: int = 10
    toks: list[str] | None = field(default=None, repr=False)   # normalised, for clones
    shingles: set[int] | None = field(default=None, repr=False, compare=False)

    @property
    def lines(self) -> int:
        return self.end - self.start + 1

    @property
    def symbol(self) -> str:
        return f"{self.file}::{self.qual}"

    def values(self) -> dict:
        return {"cyclomatic": self.cyclomatic, "cognitive": self.cognitive, "nesting": self.nesting,
                "lines": self.lines, "params": self.params}

    def as_dict(self) -> dict:
        d = {"symbol": self.symbol, "at": f"{self.file}:{self.start}-{self.end}", **self.values(),
             "metrics_status": "statically_verified", "health": self.health, "health_status": "strong_inference",
             "smells": self.smells}
        if self.deepest is not None and self.nesting:
            d["deepest_at"] = f"{self.file}:{self.deepest}"
        return d


def score(m: FnMetrics) -> FnMetrics:
    """Fill ``m.smells`` and ``m.health`` from its metrics."""
    vals = m.values()
    m.smells, lost = [], 0
    for smell, (metric, steps) in SMELLS.items():
        hit = [t for t in steps if vals[metric] >= t]
        if hit:
            lost += len(hit)
            m.smells.append({"smell": smell, "metric": metric, "value": vals[metric], "threshold": hit[-1]})
    m.health = max(1, 10 - lost)
    return m


def band(health: int) -> str:
    return next(name for name, lo in BANDS if health >= lo)


# -- Python ---------------------------------------------------------------------------------------------------

_PY_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
_PY_LOOPS = (ast.For, ast.AsyncFor, ast.While)
_PY_TRY = tuple(t for t in (ast.Try, getattr(ast, "TryStar", None)) if t is not None)
_PY_MATCH = getattr(ast, "Match", None)
_PY_COMPS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


def _py_params(fn: ast.AST) -> int:
    a = fn.args
    names = [x.arg for x in [*a.posonlyargs, *a.args, *a.kwonlyargs]]
    n = len(names) + (a.vararg is not None) + (a.kwarg is not None)
    return n - (1 if names and names[0] in ("self", "cls") else 0)


def _py_metrics(fn: ast.AST, m: FnMetrics, lines: list[str]) -> None:
    def enter(depth: int, line: int) -> None:
        if depth > m.nesting:
            m.nesting, m.deepest = depth, line

    def is_elif(n: ast.If) -> bool:
        src = lines[n.lineno - 1] if 0 < n.lineno <= len(lines) else ""
        return src[n.col_offset:].startswith("elif")

    def block(stmts, nest: int, depth: int) -> None:
        for s in stmts:
            visit(s, nest, depth, None)

    def if_chain(n: ast.If, nest: int, depth: int, chained: bool) -> None:
        m.cyclomatic += 1
        m.cognitive += 1 if chained else 1 + nest
        enter(depth + 1, n.lineno)
        visit(n.test, nest, depth, n)
        block(n.body, nest + 1, depth + 1)
        if len(n.orelse) == 1 and isinstance(n.orelse[0], ast.If) and is_elif(n.orelse[0]):
            if_chain(n.orelse[0], nest, depth, True)
        elif n.orelse:
            m.cognitive += 1
            block(n.orelse, nest + 1, depth + 1)

    def visit(n: ast.AST, nest: int, depth: int, parent: ast.AST | None) -> None:
        if isinstance(n, _PY_DEFS):
            return   # its own entry
        if isinstance(n, ast.If):
            if_chain(n, nest, depth, False)
        elif isinstance(n, _PY_LOOPS):
            m.cyclomatic += 1
            m.cognitive += 1 + nest
            enter(depth + 1, n.lineno)
            for e in (getattr(n, "target", None), getattr(n, "iter", None), getattr(n, "test", None)):
                if e is not None:
                    visit(e, nest, depth, n)
            block(n.body, nest + 1, depth + 1)
            if n.orelse:
                m.cognitive += 1
                block(n.orelse, nest + 1, depth + 1)
        elif _PY_TRY and isinstance(n, _PY_TRY):
            block(n.body, nest, depth)
            for h in n.handlers:
                m.cyclomatic += 1
                m.cognitive += 1 + nest
                enter(depth + 1, h.lineno)
                block(h.body, nest + 1, depth + 1)
            block(n.orelse, nest, depth)
            block(n.finalbody, nest, depth)
        elif _PY_MATCH is not None and isinstance(n, _PY_MATCH):
            m.cognitive += 1 + nest
            enter(depth + 1, n.lineno)
            visit(n.subject, nest, depth, n)
            for case in n.cases:
                p = case.pattern
                if case.guard is not None or not (isinstance(p, ast.MatchAs) and p.pattern is None and p.name is None):
                    m.cyclomatic += 1   # `case _:` is the default arm
                if case.guard is not None:
                    visit(case.guard, nest + 1, depth + 1, n)
                block(case.body, nest + 1, depth + 1)
        elif isinstance(n, ast.IfExp):
            m.cyclomatic += 1
            m.cognitive += 1 + nest
            for e in (n.test, n.body, n.orelse):
                visit(e, nest + 1, depth, n)
        elif isinstance(n, ast.BoolOp):
            m.cyclomatic += len(n.values) - 1
            if not (isinstance(parent, ast.BoolOp) and type(parent.op) is type(n.op)):
                m.cognitive += 1
            for e in n.values:
                visit(e, nest, depth, n)
        elif isinstance(n, _PY_COMPS):
            for g in n.generators:
                m.cyclomatic += 1 + len(g.ifs)
                m.cognitive += 1 + nest + len(g.ifs)
            for e in ast.iter_child_nodes(n):
                visit(e, nest + 1, depth, n)
        elif isinstance(n, ast.Lambda):
            visit(n.body, nest + 1, depth, n)
        else:
            for e in ast.iter_child_nodes(n):
                visit(e, nest, depth, n)

    for d in [*fn.decorator_list, *fn.args.defaults, *fn.args.kw_defaults]:
        if d is not None:
            visit(d, 0, 0, fn)
    block(fn.body, 0, 0)
    m.params = _py_params(fn)


# -- tree-sitter languages ------------------------------------------------------------------------------------

# Ruby names its if node `if` (the keyword shares the name but is not a named node)
_TS_IF = {"if_statement", "if_expression", "if", "elsif", "unless", "if_modifier", "unless_modifier",
          "else_if_clause", "elseif_statement"}
_TS_ELSE_IF = {"elsif", "else_if_clause", "elseif_statement"}   # always the else-if of the node holding it
_TS_ELSE = {"else_clause", "else", "else_statement"}
_TS_WRAP = {"else_clause", "else", "control_structure_body"}
_TS_LOOPS = {"for_statement", "enhanced_for_statement", "while_statement", "do_statement", "do_while_statement",
             "for_in_statement", "for_of_statement", "for_expression", "while_expression", "loop_expression",
             "for_range_loop", "range_based_for_statement", "foreach_statement", "while", "until", "for",
             "while_modifier", "until_modifier", "repeat_statement"}
_TS_SWITCH = {"switch_statement", "switch_expression", "match_expression", "when_expression",
              "expression_switch_statement", "type_switch_statement", "select_statement", "match_statement", "case"}
# one per arm (Java's `case A -> x` is a switch_rule holding a switch_label: the label is what counts)
_TS_ARMS = {"switch_case", "switch_label", "when_entry", "match_arm", "expression_case", "type_case",
            "communication_case", "case_statement", "switch_section", "when", "switch_entry", "case_clause"}
_TS_CATCH = {"catch_clause", "rescue", "catch_block"}
_TS_TERNARY = {"conditional_expression", "ternary_expression", "conditional"}
_TS_LAMBDA = {"arrow_function", "lambda_expression", "lambda_literal", "closure_expression", "anonymous_function",
              "function_expression", "func_literal", "lambda"}
_TS_BOOL_NODES = {"conjunction_expression": "&&", "disjunction_expression": "||"}
_TS_BOOL_OPS = {"&&", "||", "and", "or"}
_TS_PARAM_LISTS = {"formal_parameters", "function_value_parameters", "parameters", "parameter_list",
                   "method_parameters", "lambda_parameters"}


def _ts_named(n) -> list:
    return [c for c in n.children if c.is_named and "comment" not in c.type]


def _ts_bool_op(n) -> str | None:
    if n.type in _TS_BOOL_NODES:
        return _TS_BOOL_NODES[n.type]
    if n.type in ("binary_expression", "binary", "boolean_operator", "infix_expression"):
        op = n.child_by_field_name("operator")
        if op is None:
            op = next((c for c in n.children if not c.is_named), None)
        if op is None:
            return None
        t = op.type if not op.is_named else op.text.decode("utf-8", "replace")   # Scala: an operator_identifier
        return t if t in _TS_BOOL_OPS else None
    return None


def _ts_chained(n) -> bool:
    """Is the if-like node ``n`` the ``else if`` of an enclosing one (not nested in one of its branches)?"""
    if n.type in _TS_ELSE_IF:
        return True
    cur, p = n, n.parent
    while p is not None and p.type in _TS_WRAP:
        if len(_ts_named(p)) != 1:
            return False   # an else branch with more in it than the if (Ruby's `else` holds statements)
        cur, p = p, p.parent
    if p is None or p.type not in _TS_IF:
        return False
    prev = cur.prev_sibling
    return cur.type in _TS_ELSE or (prev is not None and prev.type == "else")


def _ts_plain_else(n) -> bool:
    """Has the if-like node ``n`` an ``else`` branch that is not an ``else if``?"""
    kids = n.children
    for i, c in enumerate(kids):
        if c.type not in _TS_ELSE:
            continue
        if c.named_child_count:   # a clause that holds its branch (JS, Rust, C)
            named = _ts_named(c)
            branch = named[0] if named else None
        else:                     # the keyword; the branch follows it (Java, Go, Kotlin)
            branch = next((x for x in kids[i + 1:] if x.is_named and "comment" not in x.type), None)
        while branch is not None and branch.type == "control_structure_body":
            named = _ts_named(branch)
            branch = named[0] if len(named) == 1 else None
        return branch is None or branch.type not in _TS_IF
    return False


def _ts_metrics(fn, m: FnMetrics, def_ids: set[int]) -> None:
    def enter(depth: int, node) -> None:
        if depth > m.nesting:
            m.nesting, m.deepest = depth, node.start_point[0] + 1

    stack = [(c, 0, 0) for c in reversed(fn.children)]
    while stack:
        n, nest, depth = stack.pop()
        if n.id in def_ids or not n.is_named or "comment" in n.type:
            continue   # a nested definition is its own entry; keywords (`if`, `while`) share node type names
        t = n.type
        inner_nest, inner_depth = nest, depth
        if t in _TS_IF:
            m.cyclomatic += 1
            if _ts_chained(n):   # its branches sit where the first branch's do: one level in already
                m.cognitive += 1
                enter(depth, n)
            else:
                m.cognitive += 1 + nest
                inner_nest, inner_depth = nest + 1, depth + 1
                enter(inner_depth, n)
            if _ts_plain_else(n):
                m.cognitive += 1
        elif t in _TS_LOOPS or t in _TS_CATCH:
            m.cyclomatic += 1
            m.cognitive += 1 + nest
            inner_nest, inner_depth = nest + 1, depth + 1
            enter(inner_depth, n)
        elif t in _TS_SWITCH:
            m.cognitive += 1 + nest
            inner_nest, inner_depth = nest + 1, depth + 1
            enter(inner_depth, n)
        elif t in _TS_ARMS:
            head = n.text[:16].lstrip().lower()
            if head.startswith(b"case") and head[4:5] in (b" ", b"\t", b"\n", b"\r"):
                head = head[4:].lstrip()   # Scala's `case _ =>`
            if not head.startswith((b"default", b"else", b"_ ", b"_=", b"_:")):
                m.cyclomatic += 1
        elif t in _TS_TERNARY:
            m.cyclomatic += 1
            m.cognitive += 1 + nest
            inner_nest = nest + 1
        elif t in _TS_LAMBDA:
            inner_nest = nest + 1
        else:
            op = _ts_bool_op(n)
            if op is not None:
                m.cyclomatic += 1
                if not (n.parent is not None and _ts_bool_op(n.parent) == op):
                    m.cognitive += 1
        stack.extend((c, inner_nest, inner_depth) for c in reversed(n.children))
    plist = fn.child_by_field_name("parameters")   # Go: not the receiver's list
    if plist is None:
        plist = next((c for c in fn.children if c.type in _TS_PARAM_LISTS), None)
    if plist is None:   # a declarator wraps the parameters (C, C++)
        decl = fn.child_by_field_name("declarator")
        plist = decl.child_by_field_name("parameters") if decl is not None else None
    if plist is not None:
        m.params = sum(1 for c in _ts_named(plist) if c.type not in ("self_parameter", "this", "receiver_parameter"))
    else:   # no list node: the parameters are the definition's own children (Swift)
        m.params = sum(1 for c in fn.children if c.type == "parameter")


# -- one file --------------------------------------------------------------------------------------------------

_CLASS_WORDS = ("class", "struct", "interface", "impl", "trait", "module", "namespace", "object", "protocol", "enum",
                "record")


def _py_defs(tree: ast.AST) -> list[tuple[str, ast.AST]]:
    """``(qual, node)`` of every definition, named as :func:`verinoda.anchors.compute_facts` names them."""
    from verinoda.anchors import _nested_defs, _unique

    taken: dict[str, None] = {}
    out: list[tuple[str, ast.AST]] = []

    def process(node: ast.AST, qual: str) -> None:
        for child in _nested_defs(node):
            process(child, f"{qual}.{child.name}")
        key = _unique(qual, taken)
        taken[key] = None
        out.append((key, node))

    for stmt in tree.body:
        if isinstance(stmt, _PY_DEFS):
            process(stmt, stmt.name)
        else:
            for child in _nested_defs(stmt):   # defs under module-level if/try
                process(child, child.name)
    return out


def _ts_defs(root) -> list[tuple[str, object, bool]]:
    """``(qual, node, is_class)`` of every named definition, named as :func:`verinoda.anchors.compute_facts`
    names them."""
    from verinoda.anchors import TS_DEF_TYPES, _ts_name, _unique

    taken: dict[str, None] = {}
    out: list[tuple[str, object, bool]] = []

    def is_def(n) -> bool:
        return n.type in TS_DEF_TYPES and _ts_name(n) is not None

    def nested(n) -> list:
        found, stack = [], list(n.children)
        while stack:
            cur = stack.pop()
            if is_def(cur):
                found.append(cur)
                continue
            stack.extend(cur.children)
        return sorted(found, key=lambda c: c.start_byte)

    def process(n, prefix: str) -> None:
        qual = f"{prefix}{_ts_name(n)}"
        for ch in nested(n):
            process(ch, qual + ".")
        key = _unique(qual, taken)
        taken[key] = None
        out.append((key, n, any(k in n.type for k in _CLASS_WORDS)))

    for ch in root.children:
        if is_def(ch):
            process(ch, "")
            continue
        for d in nested(ch):   # e.g. `export function f` / decorated definitions
            process(d, "")
    return out


def file_metrics(rel: str, text: str, *, with_tokens: bool = False) -> dict[str, FnMetrics] | None:
    """``qual -> FnMetrics`` of every function or method of one file version (named as
    :func:`verinoda.anchors.compute_facts` names them); None when the language is not supported or the text does
    not parse. ``with_tokens``: each also carries its normalised tokens (:func:`clones`)."""
    from verinoda import anchors

    suffix = PurePosixPath(rel).suffix.lower()
    out: dict[str, FnMetrics] = {}
    if suffix in (".py", ".pyi"):
        try:
            tree = anchors.parse_python(text)
            defs = _py_defs(tree)
        except (SyntaxError, ValueError, RecursionError, MemoryError):
            return None
        lines = text.split("\n")
        rows, toks = _py_tokens(text) if with_tokens else ([], [])
        for q, fn in defs:
            if isinstance(fn, ast.ClassDef):
                continue
            start = min([fn.lineno] + [d.lineno for d in fn.decorator_list])
            m = FnMetrics(rel, q, start, fn.end_lineno or fn.lineno)
            try:
                _py_metrics(fn, m, lines)
            except RecursionError:
                continue
            if with_tokens:
                m.toks = toks[bisect.bisect_left(rows, fn.lineno):bisect.bisect_right(rows, m.end)]
            out[q] = score(m)
        return out
    if suffix not in anchors.TS_LANGS:
        return None
    from verinoda.review_rules import ts_tree

    tree = ts_tree(text, suffix)
    if tree is None:
        return None
    try:
        defs = _ts_defs(tree.root_node)
    except RecursionError:
        return None
    def_ids = {n.id for _q, n, _c in defs}
    for q, fn, is_class in defs:
        if is_class:
            continue
        m = FnMetrics(rel, q, fn.start_point[0] + 1, fn.end_point[0] + 1)
        _ts_metrics(fn, m, def_ids - {fn.id})
        if with_tokens:
            m.toks = _ts_tokens(fn)
        out[q] = score(m)
    return out


# -- clones ----------------------------------------------------------------------------------------------------

_PY_KEEP = frozenset({"self", "cls"})


def _py_tokens(text: str) -> tuple[list[int], list[str]]:
    """``(line of each token, normalised tokens)`` of a Python text: keywords and operators as written, every
    other name ``N``, every number ``0``, every string ``S``; comments and layout dropped."""
    rows: list[int] = []
    toks: list[str] = []
    string_start = {tokenize.STRING, getattr(tokenize, "FSTRING_START", -1)}
    try:
        for t in tokenize.generate_tokens(io.StringIO(text).readline):
            if t.type == tokenize.NAME:
                s = t.string if keyword.iskeyword(t.string) or t.string in _PY_KEEP else "N"
            elif t.type == tokenize.NUMBER:
                s = "0"
            elif t.type in string_start:
                s = "S"
            elif t.type == tokenize.OP:
                s = t.string
            else:
                continue
            rows.append(t.start[0])
            toks.append(s)
    except (tokenize.TokenError, SyntaxError):
        pass   # the tokens so far: the text parsed, so this is its end
    return rows, toks


def _ts_tokens(node) -> list[str]:
    """Normalised tokens of a tree-sitter node: keywords and punctuation as written, every other name ``N``,
    every number or other literal ``0``, every string ``S``; comments dropped."""
    out: list[str] = []
    stack = [node]
    while stack:
        n = stack.pop()
        t = n.type
        if "comment" in t:
            continue
        if n.is_named and "string" in t:
            out.append("S")
        elif n.child_count == 0:
            if not n.is_named:
                out.append(t)
            else:
                out.append("0" if any(k in t for k in ("number", "integer", "float", "decimal", "literal")) else "N")
        else:
            stack.extend(reversed(n.children))
    return out


def similarity(a: list[str], b: list[str]) -> float:
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def _contains(a: FnMetrics, b: FnMetrics) -> bool:
    return a.file == b.file and (a.start <= b.start <= b.end <= a.end or b.start <= a.start <= a.end <= b.end)


def _may_reach(a: list[str], b: list[str], min_similarity: float) -> bool:
    """Can the ratio of ``a`` and ``b`` reach ``min_similarity`` at all (it is at most 2*shorter/(sum))?"""
    shorter, longer = sorted((len(a), len(b)))
    return shorter > 0 and 2 * shorter / (shorter + longer) >= min_similarity


def _shingles(m: FnMetrics) -> set[int]:
    if m.shingles is None:
        t = m.toks or []
        m.shingles = {hash(tuple(t[i:i + _SHINGLE])) for i in range(len(t) - _SHINGLE + 1)}
    return m.shingles


def new_budget(work: int = MAX_WORK, comparisons: int = MAX_COMPARISONS) -> dict:
    """What a clone search may still spend: ``comparisons`` and ``work`` (see :func:`_ratio`); ``truncated`` once
    either ran out, and ``too_long``: the functions left out for having more than :data:`MAX_CLONE_TOKENS`
    tokens."""
    return {"compared": 0, "comparisons_left": comparisons, "work_left": work, "truncated": False, "too_long": 0}


def _ratio(a: list[str], b: list[str], min_similarity: float, budget: dict) -> float | None:
    """The similarity ratio of ``a`` and ``b``, or an upper bound of it below ``min_similarity`` (the multisets of
    tokens already differ too much); None when ``budget`` ran out. The ratio's work, charged to the budget, is
    what its matching scans: every pair of equal tokens (a long run of one repeated token costs its square)."""
    if budget["comparisons_left"] <= 0:
        budget["truncated"] = True
        return None
    budget["comparisons_left"] -= 1
    budget["compared"] += 1
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    bound = sm.real_quick_ratio()
    if bound >= min_similarity:
        bound = sm.quick_ratio()
    if bound < min_similarity:
        return bound
    cb = Counter(b)
    cost = sum(n * cb[t] for t, n in Counter(a).items())
    if budget["work_left"] < cost:
        budget["truncated"] = True
        return None
    budget["work_left"] -= cost
    return sm.ratio()


def _stats(budget: dict) -> dict:
    return {"compared": budget["compared"], "truncated": budget["truncated"], "too_long": budget["too_long"]}


def _comparable(fns, min_tokens: int, budget: dict) -> list[FnMetrics]:
    out = []
    for m in fns:
        if m.toks is None or len(m.toks) < max(1, min_tokens):
            continue
        if len(m.toks) > MAX_CLONE_TOKENS:
            budget["too_long"] += 1
            continue
        out.append(m)
    return out


def clones(fns: list[FnMetrics], *, min_similarity: float = MIN_SIMILARITY, min_tokens: int = MIN_TOKENS,
           max_comparisons: int = MAX_COMPARISONS, max_work: int = MAX_WORK) -> tuple[list[dict], dict]:
    """Near-duplicate pairs among ``fns`` (with their tokens) of at least ``min_tokens`` and at most
    :data:`MAX_CLONE_TOKENS` tokens: candidates share at least half of the smaller one's token 5-grams that are
    rare (in at most :data:`_COMMON_SHINGLE` functions), then the similarity ratio decides. Returns
    ``(pairs, stats)``; ``stats["truncated"]`` when ``max_comparisons`` or ``max_work`` (:func:`new_budget`)
    stopped the search, ``stats["too_long"]`` the functions not compared for their length."""
    budget = new_budget(max_work, max_comparisons)
    units = _comparable(fns, min_tokens, budget)
    shingles = [_shingles(u) for u in units]
    index: dict[int, list[int]] = {}
    for i, sh in enumerate(shingles):
        for h in sh:
            index.setdefault(h, []).append(i)   # ascending ids
    rare = [[h for h in sh if len(index[h]) <= _COMMON_SHINGLE] for sh in shingles]
    pairs: list[dict] = []
    for i, hs in enumerate(rare):
        shared: Counter = Counter()
        for h in hs:
            ids = index[h]
            shared.update(ids[bisect.bisect_right(ids, i):])
        for j, k in sorted(shared.items()):
            a, b = units[i], units[j]
            if k < 0.5 * min(len(hs), len(rare[j])) or _contains(a, b) \
                    or not _may_reach(a.toks, b.toks, min_similarity):
                continue
            r = _ratio(a.toks, b.toks, min_similarity, budget)
            if r is None:
                break
            if r >= min_similarity:
                pairs.append(clone_pair(a, b, r))
        if budget["truncated"]:
            break
    pairs.sort(key=lambda p: (-p["similarity"], -p["tokens"], p["a"]["at"]))
    return pairs, _stats(budget)


def clone_pair(a: FnMetrics, b: FnMetrics, ratio: float) -> dict:
    return {"a": {"symbol": a.symbol, "at": f"{a.file}:{a.start}-{a.end}"},
            "b": {"symbol": b.symbol, "at": f"{b.file}:{b.start}-{b.end}"},
            "similarity": round(ratio, 3), "tokens": min(len(a.toks or ()), len(b.toks or ())),
            "status": "strong_inference",
            "basis": "similarity ratio of the normalised token sequences (names and literals replaced)"}


def alike(a: FnMetrics, b: FnMetrics, budget: dict, *, min_similarity: float = MIN_SIMILARITY,
          min_tokens: int = MIN_TOKENS) -> float | None:
    """The similarity ratio of two functions (with their tokens) when it reaches ``min_similarity``; None when
    it does not, when either is too short or too long to compare, or when ``budget`` ran out."""
    if a.toks is None or b.toks is None or _contains(a, b):
        return None
    n = max(1, min_tokens)
    if not (n <= len(a.toks) <= MAX_CLONE_TOKENS and n <= len(b.toks) <= MAX_CLONE_TOKENS) \
            or not _may_reach(a.toks, b.toks, min_similarity):
        return None
    sa, sb = _shingles(a), _shingles(b)
    if len(sa & sb) < 0.5 * min(len(sa), len(sb)):   # the prefilter of clones(), over all 5-grams
        return None
    r = _ratio(a.toks, b.toks, min_similarity, budget)
    return r if r is not None and r >= min_similarity else None


def near_duplicates(fns: dict[str, FnMetrics], qual: str, budget: dict | None = None, *,
                    min_similarity: float = MIN_SIMILARITY, min_tokens: int = MIN_TOKENS
                    ) -> list[tuple[FnMetrics, float]]:
    """The functions of one file version (``fns``, with their tokens) that are near-duplicates of ``qual``, as far
    as ``budget`` (:func:`new_budget`, shared across calls) reaches."""
    budget = new_budget() if budget is None else budget
    u = fns.get(qual)
    if u is None or u.toks is None or len(u.toks) < max(1, min_tokens):
        return []
    if len(u.toks) > MAX_CLONE_TOKENS:
        budget["too_long"] += 1
        return []
    out = []
    for v in fns.values():
        if v is u:
            continue
        r = alike(u, v, budget, min_similarity=min_similarity, min_tokens=min_tokens)
        if r is not None:
            out.append((v, r))
        if budget["truncated"]:
            break
    return out

# -- the report ------------------------------------------------------------------------------------------------

def _decode(data: bytes) -> str:
    t = data.decode("utf-8", "replace").replace("\r\n", "\n")
    return t[1:] if t.startswith("﻿") else t


def _select(repo: Path, paths: list[str] | None) -> tuple[list[str], list[str]]:
    """The code files under ``paths`` (repository-relative or absolute; default every code file that is not a
    test), and the paths that matched none."""
    from verinoda import anchors
    from verinoda.snapshot import list_files
    from verinoda.testcode import is_test_or_support_file

    code = {".py", ".pyi", *anchors.TS_LANGS}
    files = [f for f in list_files(repo) if PurePosixPath(f).suffix.lower() in code]
    if not paths:
        return [f for f in files if not is_test_or_support_file(f)], []
    want = []
    for p in paths:
        q = Path(p)
        try:
            rel = q.resolve().relative_to(repo).as_posix() if q.is_absolute() else p.replace("\\", "/")
        except ValueError:
            raise ValueError(f"{p} is outside the repository {repo}") from None
        want.append("" if rel in (".", "./") else rel.strip("/").removeprefix("./"))
    hit = {w: [f for f in files if not w or f == w or f.startswith(w + "/")] for w in want}
    return sorted({f for fs in hit.values() for f in fs}), [w for w, fs in hit.items() if not fs]


def report(repo: Path, paths: list[str] | None = None, *, limit: int = DEFAULT_LIMIT,
           min_similarity: float = MIN_SIMILARITY, min_tokens: int = MIN_TOKENS, with_clones: bool = True) -> dict:
    """Functions of ``paths`` (files or folders; default every code file that is not a test) ranked by health,
    lowest first, and their near-duplicate pairs."""
    repo = Path(repo).resolve()
    if not 0 < min_similarity <= 1:
        raise ValueError("min_similarity must be in (0, 1]")
    if min_tokens < 1:
        raise ValueError("min_tokens must be a positive number")
    files, unmatched = _select(repo, paths)
    fns: list[FnMetrics] = []
    unread: list[str] = []
    for rel in files:
        try:
            text = _decode((repo / rel).read_bytes())
        except OSError:
            unread.append(rel)
            continue
        ms = file_metrics(rel, text, with_tokens=with_clones)
        if ms is None:
            unread.append(rel)
            continue
        fns += ms.values()
    fns.sort(key=lambda m: (m.health, -m.cognitive, -m.cyclomatic, m.symbol))
    bands = Counter(band(m.health) for m in fns)
    pairs, stats = clones(fns, min_similarity=min_similarity, min_tokens=min_tokens) if with_clones else ([], {})
    shown = [m.as_dict() for m in fns[:limit]]
    res = {
        "files": len(files) - len(unread), "functions": len(fns),
        "bands": {name: bands.get(name, 0) for name, _lo in BANDS},
        "mean_health": round(sum(m.health for m in fns) / len(fns), 2) if fns else None,
        "worst": shown, "worst_truncated": len(fns) > limit,
        "clones": pairs[:limit] if with_clones else None,
        "clones_total": len(pairs) if with_clones else None,
        "unsupported": unread[:20], "unsupported_total": len(unread), "unmatched": unmatched,
        "coverage": {"method": "metrics counted on each function's syntax tree (Python ast, tree-sitter "
                               "grammars); health = 10 minus one point per smell threshold reached; clones: "
                               f"similarity ratio >= {min_similarity} of normalised tokens, functions of at "
                               f"least {min_tokens} tokens", "thresholds": {k: {"metric": v[0], "at": list(v[1])}
                                                                           for k, v in SMELLS.items()},
                     "limits": list(LIMITS), **({"clone_comparisons": stats} if with_clones else {})},
    }
    return res


def render_text(res: dict) -> str:
    b = res["bands"]
    out = [f"Code health of {res['functions']} function(s) in {res['files']} file(s): mean "
           f"{res['mean_health'] if res['mean_health'] is not None else '-'}; " + ", ".join(f"{v} {k}" for k, v in
                                                                                          b.items()) + "."]
    worst = [w for w in res["worst"] if w["health"] < 10]
    if worst:
        out += ["", "Lowest health (metrics counted; the score is a heuristic):"]
        for w in worst:
            out.append(f"  {w['health']:>2}  {w['symbol']}  cyclomatic {w['cyclomatic']}, cognitive {w['cognitive']}, "
                       f"nesting {w['nesting']}, {w['lines']} lines, {w['params']} params")
            out.append(f"      at {w['at']}" + (f"; deepest at {w['deepest_at']}" if w.get("deepest_at") else "")
                       + "; " + ", ".join(s["smell"] for s in w["smells"]))
        if res["worst_truncated"]:
            out.append("  ... more (--limit N, --json)")
    else:
        out += ["", "No function reaches a smell threshold."]
    if res.get("clones") is not None:
        out += ["", f"Near-duplicate functions: {res['clones_total']}" + (":" if res["clones"] else ".")]
        for p in res["clones"]:
            out.append(f"  {p['similarity']:.2f}  {p['a']['at']}  ~  {p['b']['at']}  ({p['tokens']} tokens)")
        stats = res["coverage"].get("clone_comparisons") or {}
        if stats.get("truncated"):
            out.append(f"  comparisons stopped after {stats['compared']}: pass paths to narrow the search")
        if stats.get("too_long"):
            out.append(f"  {stats['too_long']} function(s) of more than {MAX_CLONE_TOKENS} tokens not compared")
    if res.get("unsupported_total"):
        out += ["", f"Not measured ({res['unsupported_total']} file(s): no grammar or a parse error): "
                + ", ".join(res["unsupported"][:5]) + (" ..." if res["unsupported_total"] > 5 else "")]
    if res.get("unmatched"):
        out += ["", "No code file under: " + ", ".join(res["unmatched"])]
    return "\n".join(out)
