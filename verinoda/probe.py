"""``verinoda probe``: call one changed Python function on many inputs, at the base and in the working tree.

docs/DESIGN.md D36. For one function (``path.py::name``, ``path.py::Class.method``
or a unique bare name; or every function changed against the base with
``changed``):

1. **Eligibility**: Python only; top-level functions, static and class
   methods, and instance methods whose class has a *recipe* (a constructor call
   with literal arguments in the code or tests). Anything else is
   ``unsupported``, with the reason.
2. **Side-effect gate** (:mod:`verinoda.probe_gate`): the static closure plus
   module-level statements; a refusal names the line and the call chain.
   ``allow_side_effects`` is the user's decision; the reasons stay in the result.
3. **Inputs** (:mod:`verinoda.probe_inputs`, from the syntax tree only):
   annotations, call-site literals, recipes, boundaries mined from both
   versions and their callees, standard edges, then hypothesis (or a fixed
   pseudo-random list) up to ``inputs``. One corpus, with its sha256, for both
   runs.
4. **Runs**: ``python -m pytest -p verinoda_probe`` through
   :func:`verinoda.experiments.run` - the throw-away copy, allowlist, scrubbed
   environment and tree-killing timeout of every experiment. The base is a
   commit copy (``ref``); the working tree is the default copy. Each input is
   called twice per run; an audit hook blocks file writes, network, processes
   and environment changes during the calls; a call that hangs ends the run and
   the next run resumes after it.
5. **Oracles**: the differential classes (``new_exception``,
   ``exception_type_changed``, ``value_changed_at_mined_boundary``,
   ``value_changed``, ``type_changed``, ``exception_removed``,
   ``argument_mutation_changed``, ``new_timeout``, ``timeout_removed``,
   ``numeric_drift``), exceptions no ``raise`` in the closure, test or
   docstring declares (on inputs of the annotated domain), the agent's
   properties, inputs whose two calls disagree (``nondeterministic``: never a
   difference), and with ``scaling`` the rough growth per 10x of one collection
   or string argument (a change is flagged only when the working tree grows at
   least 3x faster than the base in every measured round).
6. **Confirmation**: the simplest examples of every class are run again in a
   fresh pair of runs; a class whose outputs differ from the first pair is
   ``unstable`` and not reported as a difference.
7. **Recording**: every confirmed class becomes a ``behaviour`` claim with
   ``experiment_verified`` evidence scoped to the runs ("in run R at tree T",
   never "always"); "no difference" is ``weak_inference`` with the search space
   stated. Nothing is judged a bug: a difference is a behaviour change, and the
   agent compares it with what the user asked for. ``emit_test`` prints (never
   writes) pytest functions that pin the base behaviour.

Files: ``.verinoda/runs/<probe id>/corpus.json`` (the inputs) and
``probe.json`` (the result); each run's raw output is in its experiment's
``runs/<experiment id>/artifacts/probe.jsonl``.
"""

from __future__ import annotations

import ast
import hashlib
import json
import math
import re
import time
from pathlib import Path

from verinoda import experiments, probe_gate, treestate
from verinoda import probe_inputs as pin
from verinoda.paths import runs_dir
from verinoda.snapshot import list_files
from verinoda.store import Store, new_id, now

PLUGIN_MODULE = "verinoda_probe"
SPEC_MODULE = "verinoda_probe_spec"
OUT_FILE = "probe.jsonl"
DEFAULT_INPUTS = 300
MAX_INPUTS = 5000
DEFAULT_PER_CALL_TIMEOUT = 2.0
MAX_RESUMES = 3
MAX_CHANGED = 10
EXAMPLES_PER_CLASS = 3
CONFIRM_MAX = 40
SCALING_SIZES = (100, 1000, 10000)
SCALING_FLAG = 3.0
DRIFT_REL = 1e-9
CLASS_ORDER = ("new_exception", "exception_type_changed", "value_changed_at_mined_boundary", "value_changed",
               "type_changed", "exception_removed", "argument_mutation_changed", "new_timeout", "timeout_removed",
               "numeric_drift")
CLASS_TEXT = {
    "new_exception": "raises where the base returned a value",
    "exception_type_changed": "raises another exception type than the base",
    "value_changed_at_mined_boundary": "returns another value on a boundary mined from the code",
    "value_changed": "returns another value",
    "type_changed": "returns another type",
    "exception_removed": "returns a value where the base raised",
    "argument_mutation_changed": "leaves its arguments in another state after the call",
    "new_timeout": "runs past the per-call timeout where the base returned",
    "timeout_removed": "returns where the base ran past the per-call timeout",
    "numeric_drift": "returns a float that differs from the base's by at most 1e-9 (relative)",
}
NOT_CHECKED = [
    "side effects and state: only return values, raised exception types and the arguments' state after the call "
    "are compared",
    "inputs outside the generated domain (see inputs.sources)",
    "exception messages (types are compared, messages are not)",
    "concurrency and thread safety",
]
NOT_CHECKED_PERF = "performance (run again with scaling)"
_NUM = re.compile(r"(?<![\w.])-?(?:\d+\.\d*(?:[eE][+-]?\d+)?|\d+[eE][+-]?\d+|\d+|inf|nan)(?![\w.])")
_LANG = {".kt": "Kotlin", ".kts": "Kotlin", ".java": "Java", ".js": "JavaScript", ".ts": "TypeScript",
         ".go": "Go", ".rs": "Rust", ".rb": "Ruby", ".cs": "C#", ".php": "PHP", ".c": "C", ".cpp": "C++",
         ".scala": "Scala", ".swift": "Swift", ".lua": "Lua"}


def plugin_source() -> bytes:
    return (Path(__file__).parent / "runtime" / "probe_plugin.py").read_bytes()


# -- target -----------------------------------------------------------------------------------

def _defs(tree: ast.AST) -> dict[str, ast.AST]:
    """Qualified name -> definition for top-level functions, classes and their direct methods."""
    out: dict[str, ast.AST] = {}
    for st in tree.body:
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[st.name] = st
        elif isinstance(st, ast.ClassDef):
            out[st.name] = st
            for sub in st.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out[f"{st.name}.{sub.name}"] = sub
    return out


def _parse(text: str | None) -> ast.AST | None:
    if text is None:
        return None
    try:
        return ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return None


def _read(repo: Path, rel: str) -> str | None:
    try:
        return (repo / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def resolve_target(repo: Path, symbol: str, files: list[str]) -> tuple[str, str]:
    """``(file, qualified name)`` for ``path.py::name`` or a bare (``name`` / ``Class.method``) unique name."""
    s = (symbol or "").strip()
    if not s:
        raise ValueError("give the function to probe as path.py::name (or a unique name)")
    if "::" in s:
        rel, _, qual = s.partition("::")
        rel = rel.replace("\\", "/").strip()
        while rel.startswith("./"):
            rel = rel[2:]
        if not rel or rel.startswith("-") or any(ord(c) < 32 for c in rel) or experiments.path_escape(rel) \
                or not treestate.safe_path(rel):
            raise ValueError(f"{rel!r} must be a file path inside the repository")
        if not qual.strip():
            raise ValueError("give the function name after ::")
        return rel, qual.strip()
    found = []
    for rel in files:
        if not rel.endswith(".py") or treestate.is_test_file(rel):
            continue
        text = _read(repo, rel)
        if text is None or s.rpartition(".")[2] not in text:
            continue
        tree = _parse(text)
        if tree is not None and s in _defs(tree):
            found.append((rel, s))
    if len(found) == 1:
        return found[0]
    if not found:
        raise ValueError(f"no function {s!r} in the project's Python files; give it as path.py::name")
    raise ValueError(f"{s!r} is defined in {len(found)} files ({', '.join(r for r, _ in found[:5])}); give it as "
                     "path.py::name")


class _Project:
    """Name resolution in the project's own Python files (syntax trees only)."""

    def __init__(self, repo: Path, files: list[str]):
        self.repo = repo
        self.files = [f for f in files if f.endswith(".py")]
        self.gate = probe_gate.Gate(repo, files)
        self.ix = self.gate.ix
        self._recipes: dict[tuple[str, str], list[dict]] = {}

    def module_of(self, rel: str) -> str | None:
        from verinoda.guards import _module_of

        return _module_of(rel)

    def qual_of(self, rel: str, name: str) -> str | None:
        return self.gate._module_qual(rel, name)

    def locate(self, full: str) -> tuple[str, str] | None:
        return self.gate._locate(full)

    def node(self, rel: str, qual: str) -> ast.AST | None:
        tree, _ = self.ix.tree(rel)
        return probe_gate._find_def(tree, qual) if tree is not None else None

    def import_name(self, rel: str) -> str:
        """The name the plugin imports ``rel`` by (packages walked up by their __init__.py)."""
        mod, _root = pin.module_name(rel, lambda r: (self.repo / r).is_file())
        return mod

    def const_lookup(self, rel: str):
        """A ``lookup(node, depth)`` for :func:`probe_inputs.const_eval`: module constants, imported ones too."""
        def lookup(node: ast.AST, depth: int):
            if depth > 6:
                raise ValueError("too deep")
            if isinstance(node, ast.Name):
                return self._const(rel, node.id, depth)
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                full = self.qual_of(rel, node.value.id)
                if full:
                    loc = self.locate(f"{full}.{node.attr}")
                    if loc is not None:
                        return self._const(loc[0], loc[1], depth)
                    trel = self.ix.modules.get(full)
                    if trel is not None:
                        return self._const(trel, node.attr, depth)
            raise ValueError("not a constant")
        return lookup

    def _const(self, rel: str, name: str, depth: int):
        tree, _ = self.ix.tree(rel)
        if tree is None:
            raise ValueError("unparsable")
        value_node = None
        for st in tree.body:
            if isinstance(st, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in st.targets):
                value_node = st.value
            elif isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name) and st.target.id == name \
                    and st.value is not None:
                value_node = st.value
            elif isinstance(st, ast.ImportFrom):
                for a in st.names:
                    if (a.asname or a.name) == name:
                        value_node = None
                        full = self.qual_of(rel, name)
                        loc = self.locate(full) if full else None
                        if loc is None:
                            raise ValueError("imported from outside the project")
                        return self._const(loc[0], loc[1], depth + 1)
        if value_node is None:
            raise ValueError(f"{name} is not a module constant")
        return pin.const_eval(value_node, self.const_lookup(rel), depth + 1)

    def const_source(self, rel: str, name: str) -> str | None:
        """``path:line`` of the assignment that defines a module constant (following imports)."""
        for _ in range(5):
            tree, _t = self.ix.tree(rel)
            if tree is None:
                return None
            nxt = None
            for st in tree.body:
                if isinstance(st, (ast.Assign, ast.AnnAssign)):
                    targets = st.targets if isinstance(st, ast.Assign) else [st.target]
                    if any(isinstance(t, ast.Name) and t.id == name for t in targets):
                        return f"{rel}:{st.lineno}"
                if isinstance(st, ast.ImportFrom) and any((a.asname or a.name) == name for a in st.names):
                    full = self.qual_of(rel, name)
                    nxt = self.locate(full) if full else None
            if nxt is None:
                return None
            rel, name = nxt
        return None

    # -- classes and recipes -------------------------------------------------------------------
    def class_td(self, rel: str, name: str) -> dict | None:
        """Descriptor for a project class used as an annotation in ``rel``: enum members or recipes."""
        full = self.qual_of(rel, name)
        loc = self.locate(full) if full else None
        if loc is None:
            return None
        node = self.node(*loc)
        if not isinstance(node, ast.ClassDef):
            return None
        bases = {ast.unparse(b).rpartition(".")[2] for b in node.bases}
        if bases & pin._ENUM_BASES:
            members = [t.id for st in node.body if isinstance(st, ast.Assign) for t in st.targets
                       if isinstance(t, ast.Name) and not t.id.startswith("_")]
            return {"k": "enum", "m": self.import_name(loc[0]), "n": loc[1], "members": members}
        recipes = self.recipes(loc[0], loc[1])
        if recipes:
            return {"k": "recipe", "recipes": recipes, "cls": loc[1]}
        return {"k": "unsupported", "why": f"no recipe for {loc[1]}: no constructor call with literal arguments in "
                                           "the code or tests"}

    def recipes(self, rel: str, cls: str) -> list[dict]:
        """Constructor calls ``Cls(<literals>)`` found in the project (tests first), as encoded recipes."""
        key = (rel, cls)
        if key in self._recipes:
            return self._recipes[key]
        out: list[dict] = []
        seen: set[str] = set()
        short = cls.rpartition(".")[2]
        for frel in sorted(self.files, key=lambda f: (not treestate.is_test_file(f), f)):
            text = _read(self.repo, frel)
            if text is None or short + "(" not in text:
                continue
            tree, _ = self.ix.tree(frel)
            if tree is None:
                continue
            for n in ast.walk(tree):
                if not isinstance(n, ast.Call):
                    continue
                name = n.func.id if isinstance(n.func, ast.Name) else (n.func.attr if isinstance(n.func,
                                                                                             ast.Attribute) else None)
                if name != short:
                    continue
                rec = self._recipe_of(frel, n)
                if rec is not None and rec["$new"]["n"] == cls and pin.canon(rec) not in seen:
                    seen.add(pin.canon(rec))
                    out.append(rec)
                if len(out) >= 3:
                    break
            if len(out) >= 3:
                break
        self._recipes[key] = out
        return out

    def _recipe_of(self, frel: str, call: ast.Call) -> dict | None:
        """``{"$new": ...}`` for a constructor call with literal arguments of a project class, else None."""
        root = call.func
        if isinstance(root, ast.Attribute):
            full = None
            if isinstance(root.value, ast.Name):
                base = self.qual_of(frel, root.value.id)
                full = f"{base}.{root.attr}" if base else None
        elif isinstance(root, ast.Name):
            full = self.qual_of(frel, root.id)
        else:
            return None
        loc = self.locate(full) if full else None
        if loc is None or not isinstance(self.node(*loc), ast.ClassDef) or "." in loc[1]:
            return None
        try:
            args = [ast.literal_eval(a) for a in call.args]
            kwargs = [(k.arg, ast.literal_eval(k.value)) for k in call.keywords if k.arg]
        except (ValueError, SyntaxError, TypeError):
            return None
        if len(kwargs) != len(call.keywords) or any(isinstance(a, ast.Starred) for a in call.args):
            return None
        try:
            return {"$new": {"m": self.import_name(loc[0]), "n": loc[1], "a": [pin.encode(a) for a in args],
                             "k": [[k, pin.encode(v)] for k, v in kwargs]}}
        except TypeError:
            return None

    # -- call sites --------------------------------------------------------------------------
    def call_sites(self, name: str, params: list[str], depth: int = 0) -> dict:
        """Literal arguments, recipes and ``pytest.raises`` seen around calls of ``name`` (tests first)."""
        lits: dict[str, list] = {}
        recs: dict[str, list[dict]] = {}
        raises: set[str] = set()
        flows: list[tuple[str, int, str]] = []
        tuples: list[dict] = []
        n_sites = 0
        for frel in sorted(self.files, key=lambda f: (not treestate.is_test_file(f), f)):
            text = _read(self.repo, frel)
            if text is None or name + "(" not in text and name + " (" not in text:
                continue
            tree, _ = self.ix.tree(frel)
            if tree is None:
                continue
            for fn, call, withs in _calls_of(tree, name):
                n_sites += 1
                for w in withs:
                    raises.add(w)
                mapped = _map_args(call, params)
                if mapped is None:
                    continue
                row: dict = {}
                for p, expr in mapped.items():
                    got = self._arg_value(frel, fn, expr, call.lineno)
                    if got is None:
                        if isinstance(expr, ast.Name) and fn is not None:
                            fargs = [a.arg for a in [*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs]]
                            if expr.id in fargs:
                                flows.append((fn.name, fargs.index(expr.id), p))
                        continue
                    kind, v = got
                    if kind == "lit":
                        lits.setdefault(p, []).append(v)
                        row[p] = pin.encode(v)
                    else:
                        recs.setdefault(p, [])
                        if pin.canon(v) not in {pin.canon(r) for r in recs[p]}:
                            recs[p].append(v)
                        row[p] = v
                if row and len(row) == len(mapped):
                    tuples.append(row)
        if depth < 2:
            for caller, idx, p in flows[:10]:
                sub = self.call_sites(caller, [f"_{i}" for i in range(idx)] + [p], depth + 1)
                for v in sub["literals"].get(p, []):
                    lits.setdefault(p, []).append(v)
                for r in sub["recipes"].get(p, []):
                    recs.setdefault(p, []).append(r)
                raises |= sub["raises"] if depth == 0 else set()
        return {"literals": lits, "recipes": recs, "raises": raises, "sites": n_sites, "tuples": tuples[:50]}

    def _arg_value(self, frel: str, fn: ast.AST | None, expr: ast.AST, line: int) -> tuple[str, object] | None:
        try:
            v = ast.literal_eval(expr)
            pin.encode(v)
            if len(repr(v)) <= 20000:
                return "lit", v
            return None
        except (ValueError, SyntaxError, TypeError, RecursionError):
            pass
        if isinstance(expr, ast.Call):
            rec = self._recipe_of(frel, expr)
            return ("recipe", rec) if rec else None
        if isinstance(expr, ast.Name) and fn is not None:
            value = None
            for st in ast.walk(fn):
                if isinstance(st, ast.Assign) and st.lineno < line and len(st.targets) == 1 and \
                        isinstance(st.targets[0], ast.Name) and st.targets[0].id == expr.id:
                    value = st.value
            if value is not None:
                return self._arg_value(frel, None, value, line)
        return None


def _calls_of(tree: ast.AST, name: str) -> list[tuple[ast.AST | None, ast.Call, list[str]]]:
    """``(enclosing function, call, exception names of enclosing pytest.raises)`` for calls of ``name``."""
    out = []

    def visit(node: ast.AST, fn, raises: list[str]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visit(child, child, [])
                continue
            here = raises
            if isinstance(child, (ast.With, ast.AsyncWith)):
                names = []
                for item in child.items:
                    ce = item.context_expr
                    if isinstance(ce, ast.Call) and ast.unparse(ce.func).rpartition(".")[2] == "raises" and ce.args:
                        a = ce.args[0]
                        elts = a.elts if isinstance(a, ast.Tuple) else [a]
                        names += [ast.unparse(e).rpartition(".")[2] for e in elts]
                here = raises + names
            if isinstance(child, ast.Call):
                f = child.func
                called = f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else None)
                if called == name:
                    out.append((fn, child, list(here)))
                elif called == "raises" and child.args and len(child.args) >= 2:
                    target = child.args[1]
                    tname = target.id if isinstance(target, ast.Name) else (
                        target.attr if isinstance(target, ast.Attribute) else None)
                    if tname == name:
                        a = child.args[0]
                        elts = a.elts if isinstance(a, ast.Tuple) else [a]
                        out.append((fn, ast.Call(func=target, args=child.args[2:], keywords=child.keywords),
                                    [ast.unparse(e).rpartition(".")[2] for e in elts]))
            visit(child, fn, here)
    visit(tree, None, [])
    return out


def _map_args(call: ast.Call, params: list[str]) -> dict[str, ast.AST] | None:
    if any(isinstance(a, ast.Starred) for a in call.args) or any(k.arg is None for k in call.keywords):
        return None
    out: dict[str, ast.AST] = {}
    for i, a in enumerate(call.args):
        if i < len(params):
            out[params[i]] = a
    for k in call.keywords:
        if k.arg in params:
            out[k.arg] = k.value
    return out


# -- signature --------------------------------------------------------------------------------

def _params(node: ast.AST, kind: str) -> list[dict]:
    a = node.args
    pos = [*a.posonlyargs, *a.args]
    if kind in ("method", "class") and pos:
        pos = pos[1:]
    n_posonly = max(0, len(a.posonlyargs) - (1 if kind in ("method", "class") and a.posonlyargs else 0))
    defaults = [None] * (len([*a.posonlyargs, *a.args]) - len(a.defaults)) + list(a.defaults)
    if kind in ("method", "class") and [*a.posonlyargs, *a.args]:
        defaults = defaults[1:]
    out = []
    for i, p in enumerate(pos):
        out.append({"name": p.arg, "kind": "posonly" if i < n_posonly else "pos", "ann": p.annotation,
                    "default": defaults[i]})
    for p, d in zip(a.kwonlyargs, a.kw_defaults):
        out.append({"name": p.arg, "kind": "kwonly", "ann": p.annotation, "default": d})
    return out


def _call_kind(node: ast.AST, qual: str) -> str:
    if "." not in qual:
        return "function"
    decos = {ast.unparse(d).rpartition(".")[2] for d in node.decorator_list}
    if "staticmethod" in decos:
        return "static"
    if "classmethod" in decos:
        return "class"
    return "method"


def _is_generator(node: ast.AST) -> bool:
    for n in ast.walk(node):
        if isinstance(n, (ast.Yield, ast.YieldFrom)):
            return True
    return False


# -- corpus -----------------------------------------------------------------------------------

def _literal_default(p: dict):
    if p["default"] is None:
        return None, False
    try:
        return ast.literal_eval(p["default"]), True
    except (ValueError, SyntaxError, TypeError):
        return None, False


def build_corpus(params: list[dict], tds: list[dict], bounds: pin.Bounds, site_tuples: list[dict],
                 examples: list[list], n: int, seed: int) -> tuple[list[dict], list[dict], dict]:
    """``(cases, meta, info)``: deterministic cases first (examples, the call with defaults, call-site
    tuples, one parameter at a time over its edges, pairs of mined boundaries), then generated ones."""
    cases: list[dict] = []
    meta: list[dict] = []
    seen: set[str] = set()
    req = [i for i, p in enumerate(params) if p["default"] is None]
    doms = [pin.in_domain(td) for td in tds]

    def add(values: dict[int, object], tags: list[str], src: str, dom: bool) -> None:
        if len(cases) >= n:
            return
        a, k = [], []
        last_pos = max([i for i in values if params[i]["kind"] != "kwonly"], default=-1)
        for i, p in enumerate(params):
            if p["kind"] == "kwonly":
                if i in values:
                    k.append([p["name"], pin.encode(values[i])])
                continue
            if i > last_pos:
                break
            if i in values:
                a.append(pin.encode(values[i]))
            else:
                d, ok = _literal_default(p)
                if not ok:
                    if p["kind"] == "posonly":
                        return
                    # pass the later ones by keyword instead
                    for j in range(i, last_pos + 1):
                        if j in values and params[j]["kind"] != "posonly":
                            k.append([params[j]["name"], pin.encode(values[j])])
                    break
                a.append(pin.encode(d))
        case = {"a": a, "k": k}
        key = pin.canon(case)
        if key in seen:
            return
        seen.add(key)
        cases.append(case)
        meta.append({"tags": sorted(set(t for t in tags if t)), "src": src, "dom": dom})

    base_vals = {i: pin.typical(tds[i]) for i in req}
    for ex in examples:
        vals = {i: v for i, v in enumerate(ex) if i < len(params)}
        add({i: pin.Raw(pin.encode(v)) if not isinstance(v, pin.Raw) else v for i, v in vals.items()}, [],
            "example", False)
    add(dict(base_vals), [], "defaults", all(doms[i] for i in req))
    for row in site_tuples:
        vals = {}
        for i, p in enumerate(params):
            if p["name"] in row:
                vals[i] = pin.Raw(row[p["name"]])
        if all(i in vals for i in req):
            add(vals, [], "call site", True)
    per_param = [pin.edges(td, bounds) for td in tds]
    # boundary-tagged edges of every parameter first, then the rest, round robin
    tagged = [[e for e in edges if e[1] and not e[1].startswith(("large",))] for edges in per_param]
    for i, lst in enumerate(tagged):
        for v, t in lst:
            vals = dict(base_vals)
            vals[i] = v
            add(vals, [t], "boundary", all(doms[j] for j in vals))
    rest = [[e for e in edges if not (e[1] and not e[1].startswith("large"))] for edges in per_param]
    for step in range(max((len(r) for r in rest), default=0)):
        for i, lst in enumerate(rest):
            if step < len(lst):
                v, t = lst[step]
                vals = dict(base_vals)
                vals[i] = v
                add(vals, [t] if t else [], "edge", all(doms[j] for j in vals))
    mined = [i for i, lst in enumerate(tagged) if lst]
    if len(mined) >= 2:
        for x in range(len(mined)):
            for y in range(x + 1, len(mined)):
                i, j = mined[x], mined[y]
                for vi, ti in tagged[i][:6]:
                    for vj, tj in tagged[j][:6]:
                        vals = dict(base_vals)
                        vals[i], vals[j] = vi, vj
                        add(vals, [ti, tj], "boundary pair", all(doms[k] for k in vals))
    deterministic = len(cases)
    want = max(0, n - len(cases))
    gen, how = pin.generated(tds, want * 2 if want else 0, seed)
    for row in gen:
        if len(cases) >= n:
            break
        add({i: pin.Raw(v) for i, v in enumerate(row)}, [], "generated", all(doms))
    return cases, meta, {"deterministic": deterministic, "generated": len(cases) - deterministic, "generator": how}


# -- runs ---------------------------------------------------------------------------------------

def _argv(repo: Path) -> list[str]:
    return [experiments.python_for(repo), "-m", "pytest", "-q", "-p", PLUGIN_MODULE, "-p", "no:cacheprovider",
            "--noconftest", "-o", "addopts="]


def _spec_module(spec: dict) -> bytes:
    text = json.dumps(spec, ensure_ascii=True, separators=(",", ":"))
    return (f"# written by verinoda probe; read by verinoda_probe.py\nSPEC_JSON = {text!r}\n").encode("ascii")


def parse_output(data: bytes) -> dict:
    out: dict = {"header": None, "rows": {}, "import_error": None, "scaling": None, "complete": False,
                 "plugin_error": None, "import_events": []}
    for raw in data.decode("utf-8", "replace").splitlines():
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        k = rec.get("k")
        if k == "header":
            out["header"] = rec
        elif k == "row":
            out["rows"][int(rec["i"])] = rec
        elif k == "import_error":
            out["import_error"] = rec
        elif k == "import_events":
            out["import_events"] = rec.get("ev") or []
        elif k == "scaling":
            out["scaling"] = rec
        elif k == "plugin_error":
            out["plugin_error"] = rec
        elif k == "footer":
            out["complete"] = bool(rec.get("complete"))
    return out


def _run_side(store: Store, repo: Path, spec: dict, *, side: str, probe_id: str, symbol: str, ref: str | None,
              commit: str | None, timeout: float) -> dict:
    """Run the corpus on one side (resuming after a call that hung); rows keyed by input index."""
    n = len(spec["cases"])
    plugins = {f"{PLUGIN_MODULE}.py": plugin_source(), f"{SPEC_MODULE}.py": _spec_module(spec)}
    res: dict = {"side": side, "experiments": [], "rows": {}, "hangs": [], "import_error": None, "scaling": None,
                 "tree": None, "guarantees": None, "isolation": None, "limits": [], "error": None,
                 "import_events": []}
    start = 0
    for _attempt in range(MAX_RESUMES + 1):
        env = {"VERINODA_PROBE_START": str(start), "VERINODA_PROBE_SIDE": side, "VERINODA_PROBE_OUT": OUT_FILE}
        hyp = f"probe {probe_id}: {symbol} on the {'base commit' if ref else 'working tree'}" + \
            (f" from input {start}" if start else "")
        try:
            exp = experiments.run(store, repo, _argv(repo), hypothesis=hyp, commit=commit, timeout=timeout,
                                  plugins=plugins, env_extra=env, ref=ref)
        except experiments.ExperimentRefused as exc:
            res["error"] = f"refused: {exc}"
            return res
        except OSError as exc:
            res["error"] = f"could not start the run: {exc}"
            return res
        res["experiments"].append(exp["id"])
        res["tree"] = res["tree"] or (exp.get("tree") or {}).get("hash")
        res["guarantees"] = exp.get("guarantees")
        res["isolation"] = exp.get("isolation")
        for lim in exp.get("limits") or []:
            if lim not in res["limits"]:
                res["limits"].append(lim)
        path = (exp.get("artifacts") or {}).get(OUT_FILE)
        data = Path(path).read_bytes() if path and Path(path).is_file() else b""
        out = parse_output(data)
        if out["import_error"]:
            res["import_error"] = out["import_error"]
            return res
        if out["import_events"]:
            res["import_events"] = out["import_events"]
        if out["header"] is None:
            why = exp.get("inconclusive_reason") or f"the run ended {exp['outcome']} without probe output"
            res["error"] = f"{why} (logs: {exp['logs']['stderr']})"
            return res
        if out["plugin_error"]:
            res["error"] = f"the probe plugin failed: {out['plugin_error'].get('e')}: {out['plugin_error'].get('m')}"
        res["rows"].update(out["rows"])
        if out["scaling"]:
            res["scaling"] = out["scaling"]
        if out["complete"]:
            return res
        last = max(out["rows"], default=start - 1)
        if exp["outcome"] == "timeout":  # the whole run hit its timeout: no input is known to hang
            res["error"] = (f"the run reached its {timeout:g} s timeout after input {last}; the remaining "
                            f"{n - last - 1} input(s) were not run (raise timeout)")
            return res
        hung = last + 1
        if hung >= n:  # every input ran; the scaling part did not finish
            res["scaling"] = res["scaling"] or {"problem": {"error": "the scaling run did not finish (a call ran "
                                                                     "past the timeout)"}}
            return res
        res["hangs"].append(hung)
        res["rows"][hung] = {"i": hung, "x": [{"hang": True}], "ms": []}
        start = hung + 1
        if start >= n:
            return res
    res["error"] = f"{len(res['hangs'])} inputs ran past the per-call timeout; the rest were not run"
    return res


# -- comparison ---------------------------------------------------------------------------------

def _out_key(o: dict) -> tuple:
    if o.get("hang"):
        return ("hang",)
    if "b" in o:
        return ("blocked",)
    if "e" in o:
        return ("e", o["e"], o.get("m"), o.get("at"))
    return ("r", o.get("t"), o.get("r"), o.get("h"), o.get("ap"))


def _stable(row: dict) -> bool:
    xs = row.get("x") or []
    return bool(xs) and all(_out_key(o) == _out_key(xs[0]) for o in xs[1:])


def _drift(a: str, b: str) -> bool:
    if _NUM.sub("#", a) != _NUM.sub("#", b):
        return False
    na, nb = _NUM.findall(a), _NUM.findall(b)
    if len(na) != len(nb):
        return False
    differs = False
    for x, y in zip(na, nb):
        if x == y:
            continue
        if not any(c in x + y for c in ".eEn"):  # integers that differ are a value change
            return False
        try:
            fx, fy = float(x), float(y)
        except ValueError:
            return False
        if not (math.isfinite(fx) and math.isfinite(fy)):
            return False
        if abs(fx - fy) > DRIFT_REL * max(abs(fx), abs(fy)):
            return False
        differs = True
    return differs


def classify(b: dict, h: dict, mined: bool) -> str | None:
    """The difference class of one input's base and working-tree outcomes (None: the same)."""
    bh, hh = bool(b.get("hang")), bool(h.get("hang"))
    if bh or hh:
        return None if bh == hh else ("new_timeout" if hh else "timeout_removed")
    be, he = "e" in b, "e" in h
    if be and he:
        return None if b["e"] == h["e"] else "exception_type_changed"
    if he:
        return "new_exception"
    if be:
        return "exception_removed"
    if b.get("t") != h.get("t"):
        return "type_changed"
    if b.get("r") == h.get("r") and b.get("h") == h.get("h"):
        return None if b.get("ap") == h.get("ap") else "argument_mutation_changed"
    if b.get("h") is None and h.get("h") is None and _drift(b.get("r") or "", h.get("r") or ""):
        return "numeric_drift"
    return "value_changed_at_mined_boundary" if mined else "value_changed"


def describe(o: dict | None) -> str:
    if o is None:
        return "not run"
    if o.get("hang"):
        return "ran past the per-call timeout"
    if "b" in o:
        return f"was blocked ({o['b']})"
    if "e" in o:
        t = o["e"].rpartition(".")[2] if o["e"].startswith("builtins.") else o["e"]
        return f"raises {t}" + (f": {o['m'][:120]}" if o.get("m") else "")
    r = o.get("r") or ""
    return f"returns {r[:160] + ('...' if len(r) > 160 else '')}"


def _brief(o: dict | None) -> dict:
    if o is None:
        return {"not_run": True}
    if o.get("hang"):
        return {"timeout": True}
    if "b" in o:
        return {"blocked": o["b"]}
    if "e" in o:
        return {"raises": o["e"], "message": (o.get("m") or "")[:200]}
    return {"returns": (o.get("r") or "")[:400], "type": o.get("t")}


def _declared(project: _Project, closure: list[str], node: ast.AST, site_raises: set[str]) -> set[str]:
    names = set(site_raises)
    doc = ast.get_docstring(node) or ""
    m = re.search(r"(?:Raises|Raise)\s*:?\s*\n(.*?)(?:\n\s*\n|\Z)", doc, re.S)
    if m:
        names |= set(re.findall(r"\b([A-Z]\w*(?:Error|Exception|Warning|Exit|Interrupt))\b", m.group(1)))
    names |= set(re.findall(r":raises\s+([A-Z]\w+)", doc))
    for item in closure:
        rel, _, qual = item.partition("::")
        fn = project.node(rel, qual)
        for n in ast.walk(fn) if fn is not None else []:
            if isinstance(n, ast.Raise) and n.exc is not None:
                e = n.exc.func if isinstance(n.exc, ast.Call) else n.exc
                names.add(ast.unparse(e).rpartition(".")[2])
    return names


def _is_declared(exc_type: str, declared: set[str], project_classes: dict[str, set[str]]) -> bool:
    name = exc_type.rpartition(".")[2]
    seen = set()
    stack = [name]
    while stack:
        c = stack.pop()
        if c in declared:
            return True
        if c in seen:
            continue
        seen.add(c)
        stack.extend(project_classes.get(c, ()))
    return False


def _project_classes(project: _Project) -> dict[str, set[str]]:
    classes: dict[str, set[str]] = {}
    for rel in project.files:
        tree, _ = project.ix.tree(rel)
        for st in getattr(tree, "body", []) if tree is not None else []:
            if isinstance(st, ast.ClassDef):
                classes.setdefault(st.name, {ast.unparse(b).rpartition(".")[2] for b in st.bases})
    return classes


def _growth(times: list) -> list[float]:
    """Growth ratios between consecutive sizes (None where a size was not measured)."""
    out = []
    for a, b in zip(times, times[1:]):
        if a and b and a > 0:
            out.append(b / a)
    return out


def _scaling_verdict(base_sc: dict | None, head_sc: dict | None) -> dict:
    res: dict = {"sizes": list(SCALING_SIZES)}
    for side, sc in (("base", base_sc), ("head", head_sc)):
        if not sc:
            res[f"{side}_problem"] = "not measured"
            continue
        res[f"{side}_seconds"] = [[round(t, 7) for t in r] for r in sc.get("rounds") or []]
        if sc.get("problem"):
            res[f"{side}_problem"] = sc["problem"]
    if base_sc is None or head_sc is None or not base_sc.get("rounds") or not head_sc.get("rounds"):
        res["flag"] = None
        res["verdict"] = "unknown: the growth could not be measured on both sides"
        return res
    ratios, bg, hg = [], [], []
    for rb, rh in zip(base_sc["rounds"], head_sc["rounds"]):
        k = min(len(rb), len(rh))
        if k < 2:
            continue
        gb, gh = rb[k - 1] / rb[k - 2] if rb[k - 2] else None, rh[k - 1] / rh[k - 2] if rh[k - 2] else None
        if gb and gh:
            bg.append(gb)
            hg.append(gh)
            ratios.append(gh / gb)
    if not ratios:
        res["flag"] = None
        res["verdict"] = "unknown: fewer than two sizes were measured on one side"
        return res
    res["growth_base_per_10x"] = round(sorted(bg)[len(bg) // 2], 2)
    res["growth_head_per_10x"] = round(sorted(hg)[len(hg) // 2], 2)
    res["ratio_per_round"] = [round(r, 2) for r in ratios]
    res["flag"] = len(ratios) >= 3 and all(r >= SCALING_FLAG for r in ratios)
    res["verdict"] = (f"the working tree grows about {res['growth_head_per_10x']}x per 10x more input against "
                      f"{res['growth_base_per_10x']}x at the base, in all {len(ratios)} rounds (rough timing)"
                      if res["flag"] else
                      f"no growth change of {SCALING_FLAG:g}x or more in every round (per-round ratios "
                      f"{res['ratio_per_round']})")
    return res


def _scaling_spec(params: list[dict], tds: list[dict]) -> dict | None:
    for i, (p, td) in enumerate(zip(params, tds)):
        if p["kind"] == "kwonly":
            continue
        k = td.get("k")
        if k in ("list", "tuple", "set", "str") and not (k == "tuple" and "items" in td):
            base = []
            for j, (q, tq) in enumerate(zip(params, tds)):
                if q["kind"] == "kwonly" or j > i and q["default"] is not None:
                    break
                base.append(pin.encode(pin.typical(tq)))
            el = None
            if k != "str":
                el_td = td["of"]
                el = pin.encode(pin._unit_shape(el_td) if el_td.get("k") == "shape" else pin._unit(el_td))
            return {"index": i, "param": p["name"], "kind": k, "element": el, "base": base,
                    "sizes": list(SCALING_SIZES), "rounds": 3, "samples": 5, "budget_s": 1.0}
    return None


# -- the probe ----------------------------------------------------------------------------------

def _mined(tags: list[str]) -> bool:
    return any(t.startswith(("boundary ", "length ", "string ")) for t in tags)


def _parse_example(text: str) -> list:
    """Arguments from an agent's example: a Python literal tuple/list of arguments, or one argument."""
    try:
        v = ast.literal_eval(text)
    except (ValueError, SyntaxError, TypeError, RecursionError):
        raise ValueError(f"example {text!r} is not a Python literal (write the arguments as a tuple, e.g. "
                         "\"(100.0,)\")") from None
    return list(v) if isinstance(v, tuple) else [v]


def _check_properties(properties: list[str]) -> list[str]:
    out = []
    for p in properties or []:
        p = str(p).strip()
        if not p:
            continue
        try:
            ast.parse(p, mode="eval")
        except SyntaxError as exc:
            raise ValueError(f"property {p!r} is not a Python expression: {exc.msg}") from None
        out.append(p)
    return out


def _base_version(repo: Path, rel: str, sha: str) -> str | None:
    spec = treestate.blob_spec(repo, sha, rel)
    raw = treestate.read_blobs(repo, [spec]).get(spec)
    return raw.decode("utf-8", "replace") if raw is not None else None


def _result(pid: str, sym: str, status: str, headline: str, **kw) -> dict:
    out = {"probe_id": pid, "symbol": sym, "status": status, "headline": headline}
    out.update({k: v for k, v in kw.items() if v is not None})
    return out


def probe(store: Store, repo: Path, symbol: str, *, base: str | None = "HEAD", no_base: bool = False,
          inputs: int = DEFAULT_INPUTS, seed: int = 0, properties: list[str] | tuple = (),
          examples: list[str] | tuple = (), scaling: bool = False, timeout: float | None = None,
          per_call_timeout: float = DEFAULT_PER_CALL_TIMEOUT, allow_side_effects: bool = False,
          emit_test: bool = False, record: bool = True, files: list[str] | None = None) -> dict:
    """Probe one function (see the module docstring). Never raises for a probe that cannot run: the result's
    ``status`` says ``unsupported`` / ``refused`` / ``inconclusive`` with the reason; a bad argument (an
    unknown function, a ref that is not a commit, a property that does not parse) raises ValueError."""
    t0 = time.monotonic()
    res = _probe(store, repo, symbol, base=base, no_base=no_base, inputs=inputs, seed=seed, properties=properties,
                 examples=examples, scaling=scaling, timeout=timeout, per_call_timeout=per_call_timeout,
                 allow_side_effects=allow_side_effects, emit_test=emit_test, record=record, files=files, t0=t0)
    if "duration_s" not in res:  # refused / unsupported before any run: still timed and written
        out_dir = runs_dir(Path(repo).resolve()) / res["probe_id"]
        out_dir.mkdir(parents=True, exist_ok=True)
        res = _finish(store, repo, res, out_dir, t0)
    return res


def _probe(store: Store, repo: Path, symbol: str, *, base, no_base, inputs, seed, properties, examples, scaling,
           timeout, per_call_timeout, allow_side_effects, emit_test, record, files, t0: float) -> dict:
    repo = Path(repo).resolve()
    files = files if files is not None else list_files(repo)
    rel, qual = resolve_target(repo, symbol, files)
    sym = f"{rel}::{qual}"
    pid = new_id("prb")
    props = _check_properties(list(properties))
    ex_vals = [_parse_example(e) for e in examples or []]
    n_inputs = max(10, min(int(inputs or DEFAULT_INPUTS), MAX_INPUTS))
    per_call = max(0.1, min(float(per_call_timeout or DEFAULT_PER_CALL_TIMEOUT), 60.0))
    suffix = Path(rel).suffix.lower()
    common = {"not_checked": list(NOT_CHECKED) + ([] if scaling else [NOT_CHECKED_PERF])}
    if suffix != ".py":
        lang = _LANG.get(suffix, f"'{suffix or rel}' files")
        return _result(pid, sym, "unsupported", f"{lang}: the probe runs Python functions only; use review and the "
                                                "project's own tests for this change", **common,
                       next_step="read the change with `verinoda map --view impact` and run the project's tests")
    base_sha, base_note = None, None
    if not no_base:
        ref = base or "HEAD"
        try:
            base_sha = treestate.resolve_commit(repo, ref)
        except (ValueError, treestate.NotAGitTree) as exc:
            if base not in (None, "HEAD"):
                raise ValueError(str(exc)) from None
            base_note = f"no base commit ({exc}): only the working tree was probed"
    head_text = _read(repo, rel)
    head_tree = _parse(head_text)
    if head_text is None:
        raise ValueError(f"{rel} is not a file in the working tree")
    if head_tree is None:
        return _result(pid, sym, "inconclusive", f"{rel} does not parse as Python in the working tree", **common)
    head_node = _defs(head_tree).get(qual)
    base_text = _base_version(repo, rel, base_sha) if base_sha else None
    base_tree = _parse(base_text)
    base_node = _defs(base_tree).get(qual) if base_tree is not None else None
    if head_node is None:
        if base_node is not None:
            return _result(pid, sym, "unsupported", f"{qual} was removed in the working tree: nothing to call",
                           **common)
        raise ValueError(f"no function {qual!r} in {rel} (top-level functions and methods of top-level classes)")
    if isinstance(head_node, ast.ClassDef):
        return _result(pid, sym, "unsupported", f"{qual} is a class: probe one of its methods "
                                                f"({rel}::{qual}.method)", **common)
    if isinstance(head_node, ast.AsyncFunctionDef):
        return _result(pid, sym, "unsupported", f"{qual} is a coroutine function (async def): not probed", **common)
    decos = {ast.unparse(d).rpartition(".")[2] for d in head_node.decorator_list}
    if decos & {"property", "cached_property", "setter", "getter", "deleter"}:
        return _result(pid, sym, "unsupported", f"{qual} is a property: not probed", **common)
    kind = _call_kind(head_node, qual)
    differential = base_sha is not None and base_node is not None
    notes: list[str] = [base_note] if base_note else []
    if base_sha and base_node is None:
        notes.append(f"{qual} does not exist at the base {base_sha[:12]} (new): no differential; the working tree "
                     "alone was probed")
    project = _Project(repo, files)
    params = _params(head_node, kind)
    sites = project.call_sites(qual.rpartition(".")[2], [p["name"] for p in params])
    tds: list[dict] = []
    for p in params:
        td = pin.annotation_td(p["ann"], lambda name: project.class_td(rel, name))
        obs_vals = sites["literals"].get(p["name"]) or []
        obs = pin.merge_observed([pin.observed_td(v) for v in obs_vals]) if obs_vals else None
        td = pin.refine(td, obs)
        recs = sites["recipes"].get(p["name"])
        if recs and td.get("k") in ("unsupported", "any", "recipe"):
            extra = [r for r in td.get("recipes", []) if pin.canon(r) not in {pin.canon(x) for x in recs}]
            td = {"k": "recipe", "recipes": (recs + extra)[:3], "cls": recs[0]["$new"]["n"]}
        why = pin.unsupported_reason(td)
        if why:
            if p["default"] is None:
                return _result(pid, sym, "unsupported", f"parameter `{p['name']}`: {why}", **common,
                               next_step="add a test that builds it from literal arguments, or name the value "
                                         "with an example (--example)")
            td = {"k": "default-only"}
        tds.append(td)
    call: dict = {"kind": kind}
    if kind == "method":
        cls = qual.rpartition(".")[0]
        recs = project.recipes(rel, cls)
        if not recs:
            return _result(pid, sym, "unsupported", f"{qual} is an instance method and {cls} has no recipe (a "
                                                    "constructor call with literal arguments in the code or tests)",
                           **common, next_step=f"add a test that builds {cls}(...) from literal arguments")
        call["recipe"] = recs[0]
    # side-effect gate: the function, the constructors the inputs and the receiver will run
    roots = [(rel, qual)]
    for td in tds:
        for r in td.get("recipes") or []:
            loc = project.locate(f"{r['$new']['m']}.{r['$new']['n']}")
            if loc:
                roots.append(loc)
    if call.get("recipe"):
        loc = project.locate(f"{call['recipe']['$new']['m']}.{call['recipe']['$new']['n']}")
        if loc:
            roots.append(loc)
    gate_res = project.gate.run(roots)
    if gate_res["verdict"] == "refused":
        gate_res["overridden_by_user"] = bool(allow_side_effects)
        if not allow_side_effects:
            first = gate_res["reasons"][0]
            chain = " -> ".join(first["via"])
            return _result(pid, sym, "refused", f"refused: {qual} reaches {first['kind']} at {first['at']} "
                                                f"({chain}); nothing was run", gate=gate_res, **common,
                           next_step="probe a pure function it calls, or - only if the user agrees to run these "
                                     "side effects in a throw-away copy - allow_side_effects")
    # boundaries: both versions of the function and the project functions it calls
    bounds = pin.Bounds()
    lookup = project.const_lookup(rel)

    def define_in(r: str):
        return lambda name: project.const_source(r, name.rpartition(".")[2]) if "." not in name else None

    pin.mine(head_node, rel, lookup, bounds, define_in(rel))
    if base_node is not None:
        pin.mine(base_node, rel, lookup, bounds, define_in(rel))
    for item in project.gate.checked[1:40]:
        crel, _, cq = item.partition("::")
        cnode = project.node(crel, cq)
        if cnode is not None:
            pin.mine(cnode, crel, project.const_lookup(crel), bounds, define_in(crel))
    used = [td if td.get("k") != "default-only" else None for td in tds]
    cases, meta, info = build_corpus(params, [td if td is not None else {"k": "none"} for td in used], bounds,
                                     sites["tuples"], ex_vals, n_inputs, int(seed or 0))
    # a parameter whose type cannot be generated keeps its default: drop cases that set it
    fixed = {params[i]["name"] for i, td in enumerate(used) if td is None}
    if fixed:
        keep = [j for j, c in enumerate(cases) if not any(k in fixed for k, _ in c["k"])
                and all(i >= len(c["a"]) for i, p in enumerate(params) if p["name"] in fixed)]
        cases, meta = [cases[j] for j in keep], [meta[j] for j in keep]
    corpus_sha = pin.corpus_sha256(cases)
    mod, root = pin.module_name(rel, lambda r: (repo / r).is_file())
    sys_path = [root] + (["."] if root != "." else []) + (["src"] if (repo / "src").is_dir() and root != "src"
                                                          else [])
    spec = {"module": mod, "qual": qual, "call": call,
            "params": [p["name"] for p in params if p["kind"] != "kwonly"], "sys_path": sys_path, "cases": cases,
            "repeat": 2, "per_call_timeout": per_call, "block": not allow_side_effects, "properties": props}
    scaling_note = None
    if scaling:
        sc = _scaling_spec(params, [td if td.get("k") != "default-only" else {"k": "none"} for td in tds])
        if sc is None:
            scaling_note = "scaling: no list, tuple, set or string parameter to grow"
        else:
            spec["scaling"] = sc
    out_dir = runs_dir(repo) / pid
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "corpus.json").write_text(json.dumps(
        {"probe_id": pid, "symbol": sym, "corpus_sha256": corpus_sha, "cases": cases, "meta": meta,
         "bounds": bounds.summary(), "generator": info["generator"], "spec": {k: v for k, v in spec.items()
                                                                              if k != "cases"}},
        ensure_ascii=True, indent=1), encoding="utf-8")
    from verinoda.paths import load_config

    run_timeout = float(timeout or load_config(repo)["experiments"]["default_timeout"])
    head_commit = treestate.head_commit(repo)
    base_run = _run_side(store, repo, spec, side="base", probe_id=pid, symbol=sym, ref=base_sha, commit=base_sha,
                         timeout=run_timeout) if differential else None
    head_run = _run_side(store, repo, spec, side="head", probe_id=pid, symbol=sym, ref=None, commit=head_commit,
                         timeout=run_timeout)
    return _analyse(store, repo, pid, sym, rel, qual, head_node, kind, spec, cases, meta, info, corpus_sha, bounds,
                    tds, params, sites, project, gate_res, base_sha, head_commit, base_run, head_run, differential,
                    notes, scaling_note, props, record, emit_test, allow_side_effects, run_timeout, t0, common,
                    out_dir)


def _analyse(store, repo, pid, sym, rel, qual, head_node, kind, spec, cases, meta, info, corpus_sha, bounds, tds,
             params, sites, project, gate_res, base_sha, head_commit, base_run, head_run, differential, notes,
             scaling_note, props, record, emit_test, allow, run_timeout, t0, common, out_dir) -> dict:
    label = _callee_label(qual, spec)
    runs = {"head": head_run["experiments"], **({"base": base_run["experiments"]} if base_run else {})}
    trees = {"head": head_run.get("tree"), **({"base": base_run.get("tree")} if base_run else {})}
    limits = list(notes) + list(head_run.get("limits") or [])
    for lim in (base_run or {}).get("limits") or []:
        if lim not in limits:
            limits.append(lim)
    sources = [f"annotations and defaults of {qual}"]
    if sites["sites"]:
        sources.append(f"{sites['sites']} call site(s) in the project (literal arguments, recipes)")
    b_sum = bounds.summary()
    if b_sum:
        sources.append("boundaries: " + "; ".join(_bound_text(b) for b in b_sum[:6]) + (" ..." if len(b_sum) > 6
                                                                                      else ""))
    sources.append("standard edge values (empty, zero, bounds, special floats, unicode, large sizes)")
    sources.append(f"generated: {info['generator']}")
    low_div = len(cases) < 10 or all(td.get("k") in ("any", "default-only") for td in tds) and bool(tds)
    inputs_info = {"count": len(cases), "deterministic": info["deterministic"], "generated": info["generated"],
                   "generator": info["generator"], "boundaries": b_sum[:20], "sources": sources,
                   "corpus_path": str(out_dir / "corpus.json"), "corpus_sha256": corpus_sha,
                   "low_diversity": low_div}
    base_info = {"commit": base_sha, "differential": differential}
    res_common = dict(common)
    res_common["gate"] = gate_res
    res_common["runs"] = runs
    res_common["trees"] = trees
    res_common["base"] = base_info
    res_common["inputs"] = inputs_info
    res_common["isolation"] = head_run.get("isolation")
    res_common["guarantees"] = head_run.get("guarantees")
    for side_run in (head_run, base_run):
        if side_run is None:
            continue
        err = side_run.get("import_error")
        if err:
            name = "working tree" if side_run["side"] == "head" else f"base {base_sha[:12]}"
            if err.get("blocked"):
                return _finish(store, repo, _result(
                    pid, sym, "refused", f"refused at run time: importing {spec['module']} in the {name} tried a "
                                         f"side effect ({err['blocked']}); it was blocked and nothing was called",
                    limits=limits, **res_common), out_dir, t0)
            return _finish(store, repo, _result(
                pid, sym, "inconclusive", f"inconclusive: {spec['module']} could not be imported in the {name} "
                                          f"copy ({err.get('e', '?')}: {err.get('m', '')[:160]})", limits=limits,
                next_step="check that the module imports in a fresh copy (python -m pytest must find it); a "
                          "missing dependency or a conftest-only setup is not probed", **res_common), out_dir, t0)
        if side_run.get("error") and not side_run.get("rows"):
            return _finish(store, repo, _result(
                pid, sym, "inconclusive", f"inconclusive: the {side_run['side']} run did not produce results "
                                          f"({side_run['error']})", limits=limits, **res_common), out_dir, t0)
    rows_h = head_run["rows"]
    rows_b = base_run["rows"] if base_run else {}
    classes: dict[str, list[int]] = {}
    nondet, blocked, not_run = [], [], []
    for i in range(len(cases)):
        rh, rb = rows_h.get(i), rows_b.get(i) if differential else None
        if rh is None or (differential and rb is None):
            not_run.append(i)
            continue
        if any("b" in o for o in rh["x"]) or (rb and any("b" in o for o in rb["x"])):
            blocked.append(i)
            continue
        if not _stable(rh) or (rb is not None and not _stable(rb)):
            nondet.append(i)
            continue
        if differential:
            c = classify(rb["x"][0], rh["x"][0], _mined(meta[i]["tags"]))
            if c:
                classes.setdefault(c, []).append(i)
    if blocked:
        i = blocked[0]
        o = next((o for o in (rows_h.get(i) or {}).get("x", []) + (rows_b.get(i) or {}).get("x", []) if "b" in o),
                 {})
        return _finish(store, repo, _result(
            pid, sym, "refused", f"refused at run time: {len(blocked)} input(s) made {qual} attempt a side effect "
                                 f"the static gate did not see ({o.get('b', '?')}); it was blocked, and no "
                                 "difference is reported", limits=limits,
            blocked={"count": len(blocked), "example": {"call": _call_text(label, cases[i]), "event": o.get("b"),
                                                        "events": o.get("ev")}},
            next_step="probe a pure function it calls, or - only if the user agrees - allow_side_effects",
            **res_common), out_dir, t0)
    # a call that ran past the per-call timeout is slow or does not end: without scaling that is performance,
    # which the probe does not judge (docs/DESIGN.md D36); it is listed, never a difference
    timeouts: dict[str, list[int]] = {}
    if not spec.get("scaling"):
        for c in ("new_timeout", "timeout_removed"):
            if c in classes:
                timeouts[c] = classes.pop(c)
    # confirmation: the simplest examples of every class, again, in a fresh pair of runs
    simple = lambda j: _simple_key(cases, meta, j)  # noqa: E731
    picks: dict[str, list[int]] = {c: sorted(ix, key=simple)[:EXAMPLES_PER_CLASS]
                                   for c, ix in classes.items()}
    confirm = {"ran": False}
    reproduced: dict[int, bool | None] = {}
    if picks:
        conf_idx = list(dict.fromkeys(j for c in CLASS_ORDER for j in picks.get(c, [])))[:CONFIRM_MAX]
        cspec = {**spec, "cases": [cases[j] for j in conf_idx]}
        cspec.pop("scaling", None)
        cb = _run_side(store, repo, cspec, side="base", probe_id=pid, symbol=sym, ref=base_sha, commit=base_sha,
                       timeout=run_timeout)
        ch = _run_side(store, repo, cspec, side="head", probe_id=pid, symbol=sym, ref=None, commit=head_commit,
                       timeout=run_timeout)
        confirm = {"ran": True, "base": cb["experiments"], "head": ch["experiments"],
                   "error": cb.get("error") or ch.get("error") or (cb.get("import_error") and "import error")
                   or (ch.get("import_error") and "import error")}
        for k, j in enumerate(conf_idx):
            b2, h2 = (cb["rows"].get(k) or {}).get("x"), (ch["rows"].get(k) or {}).get("x")
            if not b2 or not h2:
                reproduced[j] = None
                continue
            reproduced[j] = (_out_key(b2[0]) == _out_key(rows_b[j]["x"][0]) and
                             _out_key(h2[0]) == _out_key(rows_h[j]["x"][0]))
        runs["confirm"] = {"base": cb["experiments"], "head": ch["experiments"]}
    differences, unstable = [], []
    for c in CLASS_ORDER:
        if c not in classes:
            continue
        ex = picks[c]
        good = [j for j in ex if reproduced.get(j)]
        bad = [j for j in ex if reproduced.get(j) is False]
        if not good and bad:
            unstable.append({"class": c, "count": len(classes[c]),
                             "why": "the outputs changed between the first runs and the confirmation runs",
                             "example": _example(label, cases, meta, rows_b, rows_h, bad[0])})
            continue
        shown = good or ex
        differences.append({
            "class": c, "text": CLASS_TEXT[c], "count": len(classes[c]),
            "examples": [_example(label, cases, meta, rows_b, rows_h, j) for j in shown],
            "reproduced": True if good else None,
            **({"low_priority": True} if c == "numeric_drift" else {})})
    # properties, undeclared exceptions, nondeterminism
    violations, prop_errors = [], []
    for k, ptxt in enumerate(props):
        hit = [i for i, row in rows_h.items() if k in (row["x"][0].get("pv") or []) and not row["x"][0].get("hang")]
        errs = [e for row in rows_h.values() for e in (row["x"][0].get("pe") or []) if e[0] == k]
        if errs:
            prop_errors.append({"property": ptxt, "count": len(errs), "error": errs[0][1]})
        if hit:
            hit.sort(key=simple)
            violations.append({"property": ptxt, "count": len(hit), "examples": [
                {**_example(label, cases, meta, rows_b, rows_h, j),
                 **({"holds_at_base": k not in (rows_b.get(j, {}).get("x", [{}])[0].get("pv") or [])}
                    if differential and rows_b.get(j) else {})} for j in hit[:EXAMPLES_PER_CLASS]]})
    declared = _declared(project, project.gate.checked, head_node, sites["raises"])
    pclasses = _project_classes(project)
    undeclared: dict[str, list[int]] = {}
    for i, row in rows_h.items():
        o = row["x"][0]
        if "e" in o and o.get("at") != "setup" and meta[i]["dom"] and i not in nondet and \
                not _is_declared(o["e"], declared, pclasses):
            undeclared.setdefault(o["e"], []).append(i)
    und_out = []
    for etype, ix in sorted(undeclared.items(), key=lambda kv: -len(kv[1])):
        ix.sort(key=simple)
        j = ix[0]
        at_base = None
        if differential and rows_b.get(j):
            at_base = rows_b[j]["x"][0].get("e") == etype
        und_out.append({"type": etype, "count": len(ix), "example": _example(label, cases, meta, rows_b, rows_h, j),
                        **({"also_at_base": at_base} if at_base is not None else {})})
    to_out = [{"class": c, "text": CLASS_TEXT[c], "count": len(ix),
               "examples": [_example(label, cases, meta, rows_b, rows_h, j) for j in sorted(ix, key=simple)[:2]]}
              for c, ix in timeouts.items()]
    nd_out = None
    if nondet:
        j = nondet[0]
        nd_out = {"count": len(nondet), "example": {"call": _call_text(label, cases[j]),
                                                     "calls": [describe(o) for o in rows_h[j]["x"]]}}
    scaling_res = None
    if spec.get("scaling"):
        if differential:
            scaling_res = _scaling_verdict(base_run.get("scaling"), head_run.get("scaling"))
        else:
            scaling_res = {"head_seconds": (head_run.get("scaling") or {}).get("rounds"), "flag": None,
                           "verdict": "no base: growth is shown, not compared"}
        scaling_res["param"] = spec["scaling"]["param"]
        if scaling_res.get("flag"):
            differences.append({"class": "growth_changed", "text": scaling_res["verdict"], "count": 1,
                                "examples": [], "reproduced": None})
    elif scaling_note:
        limits.append(scaling_note)
    # status
    changed = [d for d in differences if d["class"] != "numeric_drift"]
    counted = len(cases) - len(not_run)
    if violations:
        status = "property_violated"
        headline = (f"{qual}: {len(violations)} of {len(props)} stated properties do not hold in the working tree "
                    f"(in {counted} inputs, run {runs['head'][0]})")
    elif changed or differences:
        status = "differences_found"
        headline = (f"{qual}: {len(differences)} kind(s) of behaviour difference between the base "
                    f"{base_sha[:12]} and the working tree in {counted} inputs (probe {pid}). These are behaviour "
                    "changes, not verdicts: compare each with what the user asked for")
    elif to_out:
        status = "inconclusive"
        headline = (f"{qual}: {sum(t['count'] for t in to_out)} input(s) ran past the {spec['per_call_timeout']:g} s "
                    "per-call timeout on one side only: slow or not ending, not judged (raise per_call_timeout, or "
                    "measure growth with scaling); no other difference in "
                    f"{len(cases) - len(not_run)} inputs")
    elif unstable and not differences:
        status = "inconclusive"
        headline = (f"{qual}: differences in the first runs did not reproduce in the confirmation runs "
                    f"({len(unstable)} kind(s)); nothing is reported as a difference")
    elif not differential and und_out:
        status = "undeclared_exceptions"
        headline = (f"{qual}: raises exceptions nothing declares on {sum(u['count'] for u in und_out)} of "
                    f"{counted} inputs of its annotated domain (working tree only)")
    elif low_div:
        status = "inconclusive"
        headline = (f"{qual}: no finding in {counted} inputs, but the inputs are not diverse (no annotations or "
                    "call-site literals to derive them from): this says little")
    elif nondet and len(nondet) >= max(1, counted // 2):
        status = "inconclusive"
        headline = f"{qual}: {len(nondet)} of {counted} inputs gave different results on two calls (nondeterministic)"
    elif differential:
        status = "no_difference_found"
        headline = (f"{qual}: no behaviour difference found between the base {base_sha[:12]} and the working tree in "
                    f"{counted} inputs (a search, not a proof; the inputs are listed under inputs.sources)")
    else:
        status = "nothing_found"
        headline = (f"{qual}: no counterexample in {counted} inputs (working tree only; a search, not a proof)")
    if head_run.get("hangs") or (base_run or {}).get("hangs"):
        limits.append(f"inputs that ran past the per-call timeout: working tree {len(head_run.get('hangs') or [])}"
                      + (f", base {len(base_run.get('hangs') or [])}" if base_run else ""))
    for side_run in (base_run, head_run):
        if side_run is not None and side_run.get("error"):
            limits.append(f"{'working-tree' if side_run['side'] == 'head' else 'base'} run: {side_run['error']}")
    if not_run:
        limits.append(f"{len(not_run)} input(s) were not run on both sides (a hang budget or a failed run)")
    if head_run.get("import_events") and allow:
        limits.append(f"side effects observed while importing: {head_run['import_events'][:2]}")
    res = _result(
        pid, sym, status, headline, differences=differences, unstable=unstable or None,
        nondeterministic=nd_out, property_violations=violations, property_errors=prop_errors or None,
        undeclared_exceptions=und_out, scaling=scaling_res, confirm=confirm if confirm.get("ran") else None,
        timeouts=to_out or None,
        limits=limits, **res_common)
    if emit_test and differences:
        res["regression_test"] = emit_tests(res, cases, rows_b, rows_h, spec, qual, base_sha, pid)
    if record:
        try:
            _record_claims(store, repo, res, rel, qual, head_node, cases, rows_b, rows_h, base_sha, head_commit,
                           trees, runs, corpus_sha)
        except Exception as exc:  # noqa: BLE001 - the probe's answer stands without its claims
            res["claims_error"] = f"{type(exc).__name__}: {exc}"
    res["next_step"] = _next_step(res)
    return _finish(store, repo, res, out_dir, t0)


def _simple_key(cases: list[dict], meta: list[dict], j: int) -> tuple:
    """Simplest first: no special floats, ASCII, inputs derived from the code before generated ones (they are
    built to be readable), short, small numbers."""
    special, nonascii, length, mag = pin.complexity(cases[j])
    return special, nonascii, meta[j]["src"] == "generated", length, mag, j


def _call_text(label: str, case: dict, full: bool = False) -> str:
    return f"{label}({pin.call_source(case, full=full)})"


def _callee_label(qual: str, spec: dict) -> str:
    """How a call is shown: ``f``, ``Cls.f`` (static/class methods), ``Recipe(...).f`` (instance methods)."""
    kind = spec["call"]["kind"]
    if kind == "method":
        return f"{pin.to_source(spec['call']['recipe'])}.{qual.rpartition('.')[2]}"
    return qual if kind in ("static", "class") else qual.rpartition(".")[2]


def _example(label, cases, meta, rows_b, rows_h, j) -> dict:
    ob = (rows_b.get(j) or {}).get("x", [None])[0] if rows_b else None
    oh = (rows_h.get(j) or {}).get("x", [None])[0]
    out = {"input": j, "call": _call_text(label, cases[j]), "head": _brief(oh)}
    if rows_b:
        out["base"] = _brief(ob)
    if meta[j]["tags"]:
        out["why_this_input"] = meta[j]["tags"][:2]
    return out


def _finish(store, repo, res: dict, out_dir: Path, t0: float) -> dict:
    res["duration_s"] = round(time.monotonic() - t0, 3)
    res["created_at"] = now()
    try:
        (out_dir / "probe.json").write_text(json.dumps(res, ensure_ascii=True, indent=1, default=str),
                                            encoding="utf-8")
        res["result_path"] = str(out_dir / "probe.json")
    except OSError:
        pass
    return res


def _next_step(res: dict) -> str:
    s = res["status"]
    if s == "differences_found":
        return ("for each class, decide with the user's request whether the change is intended; ask the user when "
                "unclear, showing the example; emit_test prints tests that pin the base behaviour")
    if s == "property_violated":
        return "the stated property fails on the examples: fix the code or correct the property with the user"
    if s == "no_difference_found":
        return "say 'no difference found in N inputs', not 'verified'; add --property for what the user asked for"
    if s == "undeclared_exceptions":
        return "check whether callers handle these exceptions, or whether the inputs are outside the intended domain"
    return "see headline and limits"


# -- claims and emitted tests -----------------------------------------------------------------------

def _record_claims(store, repo, res, rel, qual, head_node, cases, rows_b, rows_h, base_sha, head_commit, trees,
                   runs, corpus_sha) -> None:
    from verinoda import evidence as evmod
    from verinoda.claims import Claims

    snap = store.latest_snapshot()
    project = (snap or {}).get("project") or Path(repo).name
    cl = Claims(store, repo)
    sym = f"{rel}::{qual}"
    tree12 = (trees.get("head") or "")[:12]
    for d in res.get("differences") or []:
        if d["class"] == "growth_changed" or not d.get("reproduced") or not d["examples"]:
            continue
        ex = d["examples"][0]
        j = ex["input"]
        input_sha = hashlib.sha256(pin.canon(cases[j]).encode("ascii")).hexdigest()
        ob, oh = rows_b[j]["x"][0], rows_h[j]["x"][0]
        meta = {"kind": "probe_counterexample", "probe_id": res["probe_id"], "class": d["class"], "symbol": sym,
                "input_sha256": input_sha, "input": ex["call"][:300], "base": _brief(ob), "head": _brief(oh),
                "experiments": {"base": runs.get("base"), "head": runs.get("head"), "confirm": runs.get("confirm")},
                "base_commit": base_sha, "tree_hash": trees.get("head"), "corpus_sha256": corpus_sha,
                "reproduced": True, "outcome": "pass",
                "scope": "existential: observed in these runs on this input; never evidence for 'always'"}
        ev = evmod.source_evidence(repo, rel, head_node.lineno, commit=head_commit, source_type="experiment",
                                   meta=meta)
        if ev is None:
            continue
        ev["locator"] = (f"probe {res['probe_id']}: base run {(runs.get('base') or ['?'])[0]} at commit "
                         f"{(base_sha or '')[:12]}, working-tree run {runs['head'][0]} at tree {tree12}, input #{j}")
        ev["excerpt"] = f"{ex['call']}: base {describe(ob)}; working tree {describe(oh)}"[:400]
        text = (f"In probe {res['probe_id']} (base run {(runs.get('base') or ['?'])[0]} at commit "
                f"{(base_sha or '')[:12]}, working-tree run {runs['head'][0]} at tree {tree12}), `{ex['call'][:160]}` "
                f"{describe(ob)} at the base and {describe(oh)} in the working tree ({d['class']}).")
        c = cl.create(text, project=project, snapshot=snap, subjects=[sym], status="experiment_verified",
                      kind="behaviour", spec={"probe": {"id": res["probe_id"], "class": d["class"],
                                                        "input": input_sha, "symbol": sym}},
                      evidence=[(ev, "supports")])
        d["claim_id"] = c["id"]
        d["claim_status"] = c["status"]
    if res["status"] in ("no_difference_found", "nothing_found"):
        meta = {"kind": "probe_summary", "probe_id": res["probe_id"], "symbol": sym, "outcome": "pass",
                "inputs": res["inputs"]["count"], "corpus_sha256": corpus_sha, "base_commit": base_sha,
                "tree_hash": trees.get("head"), "experiments": runs,
                "scope": "a search over the listed inputs; never evidence of equivalence"}
        ev = evmod.source_evidence(repo, rel, head_node.lineno, commit=head_commit, source_type="experiment",
                                   meta=meta)
        if ev is not None:
            ev["locator"] = f"probe {res['probe_id']}: {res['inputs']['count']} inputs, runs {runs}"[:300]
            ev["excerpt"] = res["headline"][:400]
            c = cl.create(res["headline"] + ".", project=project, snapshot=snap, subjects=[sym],
                          status="weak_inference", kind="behaviour",
                          spec={"probe": {"id": res["probe_id"], "class": "no_difference", "symbol": sym}},
                          evidence=[(ev, "supports")])
            res["claim_id"] = c["id"]
            res["claim_status"] = c["status"]


def _expect_line(call: str, o: dict) -> list[str]:
    if "e" in o:
        exc = o["e"]
        name = exc.rpartition(".")[2]
        return [f"    with pytest.raises({name}):", f"        {call}"]
    r = o.get("r") or ""
    if o.get("h"):
        return [f"    # the base result is long ({o.get('n')} characters); its sha256 is {o['h']}",
                f"    assert hashlib.sha256(repr({call}).encode()).hexdigest() == {o['h']!r}"]
    if r == "nan":
        return [f"    assert math.isnan({call})"]
    try:
        ast.literal_eval(r)
        return [f"    assert {call} == {r}"]
    except (ValueError, SyntaxError, TypeError, RecursionError):
        return [f"    assert repr({call}) == {r!r}"]


def emit_tests(res: dict, cases, rows_b, rows_h, spec: dict, qual: str, base_sha: str | None, pid: str) -> str:
    """pytest functions that pin the BASE behaviour on each difference's first example (text only)."""
    mod = spec["module"]
    head = qual.split(".")[0]
    call_kind = spec["call"]["kind"]
    imports: dict[str, set[str]] = {mod: {head}}
    body: list[str] = []
    n = 0
    for d in res.get("differences") or []:
        if not d.get("examples"):
            continue
        j = d["examples"][0]["input"]
        ob = (rows_b.get(j) or {}).get("x", [None])[0]
        if ob is None or ob.get("hang"):
            continue
        case = cases[j]
        for enc in case.get("a", []) + [v for _, v in case.get("k", [])]:
            pin.imports_needed(enc, imports)
        if call_kind == "method":
            rec = spec["call"]["recipe"]
            pin.imports_needed(rec, imports)
            target = f"{pin.to_source(rec, full=True)}.{qual.rpartition('.')[2]}"
        else:
            target = qual
        call = f"{target}({pin.call_source(case, full=True)})"
        if "e" in ob and "." in ob["e"] and not ob["e"].startswith("builtins."):
            m, _, name = ob["e"].rpartition(".")
            imports.setdefault(m, set()).add(name)
        n += 1
        body += ["", "", f"def test_{qual.replace('.', '_').lower()}_probe_{n}():",
                 f"    # {d['class']}: the working tree {describe((rows_h.get(j) or {}).get('x', [{}])[0])[:100]}"]
        body += _expect_line(call, ob)
    head_lines = [f"# verinoda probe {pid}: behaviour that differs between the base {(base_sha or '')[:12]} and the "
                  "working tree.",
                  "# Each test pins the BASE behaviour on one input. Whether the change is intended is the user's "
                  "decision:", "# flip the expectation when it was meant. Verinoda did not write this file.",
                  "import hashlib", "import math", "", "import pytest", ""]
    head_lines += [f"from {m} import {', '.join(sorted(names))}" for m, names in sorted(imports.items())]
    return "\n".join(head_lines + body) + "\n"


# -- changed functions ------------------------------------------------------------------------------

def changed_functions(repo: Path, base_sha: str) -> list[dict]:
    """Python functions (top-level, and methods of top-level classes) whose signature or body differs from
    the base, or that are new; test files are left out. Comments, docstrings and formatting are not changes."""
    from verinoda import anchors

    ch = treestate.changes_vs_base(Path(repo).resolve(), base_sha)
    out = []
    for rel in sorted(ch["tree_files"]):
        if not rel.endswith(".py") or treestate.is_test_file(rel):
            continue
        new, old = ch["contents"].get(rel), ch["base"].get(rel)
        if new is None:
            continue
        fn = anchors.compute_facts(rel, new)
        fo = anchors.compute_facts(rel, old) if old is not None else None
        tree = _parse(new.decode("utf-8", "replace"))
        if not anchors.usable(fn) or tree is None:
            continue
        defs = _defs(tree)
        for q, s in fn["symbols"].items():
            if s.get("kind") != "def" or q not in defs:
                continue
            o = ((fo or {}).get("symbols") or {}).get(q) if anchors.usable(fo) else None
            change = "added" if o is None else ("signature" if o["sig"] != s["sig"] else
                                               ("body" if o["body"] != s["body"] else None))
            if change:
                out.append({"symbol": f"{rel}::{q}", "change": change, "line": s["def"]})
    return out


def probe_changed(store: Store, repo: Path, *, base: str | None = "HEAD", limit: int = MAX_CHANGED, **kw) -> dict:
    """Probe every changed function (at most ``limit``); each result is compacted."""
    repo = Path(repo).resolve()
    sha = treestate.resolve_commit(repo, base or "HEAD")
    changed = changed_functions(repo, sha)
    files = list_files(repo)
    probes, skipped = [], []
    for item in changed:
        if len(probes) >= limit:
            skipped.append({"symbol": item["symbol"], "why": f"over the limit of {limit} probes"})
            continue
        try:
            res = probe(store, repo, item["symbol"], base=sha, files=files, **kw)
        except ValueError as exc:
            skipped.append({"symbol": item["symbol"], "why": str(exc)})
            continue
        probes.append({"change": item["change"], **compact(res)})
    counts: dict[str, int] = {}
    for p in probes:
        counts[p["status"]] = counts.get(p["status"], 0) + 1
    head = (f"{len(changed)} changed Python function(s) against {sha[:12]}; probed {len(probes)}: "
            + (", ".join(f"{v} {k}" for k, v in sorted(counts.items())) or "none"))
    return {"status": "differences_found" if any(p["status"] in ("differences_found", "property_violated")
                                                 for p in probes) else ("nothing_changed" if not changed else "done"),
            "headline": head, "base": sha, "changed": changed, "probes": probes, "skipped": skipped,
            "limits": ["only functions whose own signature or body changed are listed; a function whose behaviour "
                       "changed through a changed callee or constant is probed only when named"]}


def compact(res: dict, examples: int = 2) -> dict:
    """The result without long lists (for MCP and the changed-function listing)."""
    out = {k: v for k, v in res.items() if k not in ("gate", "inputs", "regression_test")}
    if res.get("gate"):
        g = res["gate"]
        out["gate"] = {"verdict": g["verdict"], "reasons": g["reasons"][:3],
                       **({"overridden_by_user": g["overridden_by_user"]} if "overridden_by_user" in g else {}),
                       "unresolved_calls": g.get("unresolved_calls")}
    if res.get("inputs"):
        i = res["inputs"]
        out["inputs"] = {k: i[k] for k in ("count", "generator", "corpus_sha256", "low_diversity", "corpus_path")
                         if k in i}
        out["inputs"]["boundaries"] = i.get("boundaries", [])[:5]
    if res.get("differences"):
        out["differences"] = [{**d, "examples": d["examples"][:examples]} for d in res["differences"]]
    if res.get("regression_test"):
        out["regression_test"] = res["regression_test"]
    return out


def render(res: dict) -> str:
    """Plain text for people: the answer first, then the evidence and what was not checked."""
    lines = [res["headline"]]
    for d in res.get("differences") or []:
        lines.append(f"  {d['class']} ({d['count']} input(s)): the working tree {d['text']}"
                     + ("" if d.get("reproduced") or d["class"] == "growth_changed"
                        else " [not reproduced in a second run pair]")
                     + (f" - claim {d['claim_id']}" if d.get("claim_id") else ""))
        for ex in d.get("examples") or []:
            b = ex.get("base")
            lines.append(f"    {ex['call']}: base {_brief_text(b)}; working tree {_brief_text(ex['head'])}"
                         + (f"  [{ex['why_this_input'][0]}]" if ex.get("why_this_input") else ""))
    for u in res.get("unstable") or []:
        lines.append(f"  unstable {u['class']} ({u['count']}): {u['why']}")
    for t in res.get("timeouts") or []:
        lines.append(f"  {t['class']} ({t['count']} input(s), not judged: slow or not ending)")
        for ex in t["examples"]:
            lines.append(f"    {ex['call']}: base {_brief_text(ex.get('base'))}; working tree "
                         f"{_brief_text(ex['head'])}")
    for v in res.get("property_violations") or []:
        lines.append(f"  property `{v['property']}` does not hold on {v['count']} input(s)")
        for ex in v["examples"]:
            lines.append(f"    {ex['call']}: working tree {_brief_text(ex['head'])}"
                         + ("" if ex.get("holds_at_base") is None else
                            f" (at the base it {'held' if ex['holds_at_base'] else 'did not hold either'})"))
    for e in res.get("property_errors") or []:
        lines.append(f"  property `{e['property']}` could not be evaluated: {e['error']}")
    for u in res.get("undeclared_exceptions") or []:
        lines.append(f"  undeclared {u['type']} on {u['count']} input(s) of the annotated domain, e.g. "
                     f"{u['example']['call']}" + (" (also at the base)" if u.get("also_at_base") else ""))
    if res.get("nondeterministic"):
        nd = res["nondeterministic"]
        lines.append(f"  nondeterministic on {nd['count']} input(s), e.g. {nd['example']['call']}: "
                     + " / ".join(nd["example"]["calls"]))
    if res.get("scaling"):
        lines.append(f"  scaling ({res['scaling'].get('param')}): {res['scaling'].get('verdict')}")
    gate = res.get("gate") or {}
    for r in (gate.get("reasons") or [])[:5]:
        lines.append(f"  gate: {r['kind']} at {r['at']} via {' -> '.join(r['via'])}: {r['why']}"
                     + (" (run anyway: the user allowed side effects)" if gate.get("overridden_by_user") else ""))
    if res.get("blocked"):
        lines.append(f"  blocked at run time: {res['blocked']['example']['call']}: {res['blocked']['example']['event']}")
    i = res.get("inputs")
    if i:
        lines.append(f"  inputs: {i['count']} ({i['deterministic']} from annotations, call sites, boundaries and "
                     f"edges; {i['generated']} generated by {i['generator']}); corpus sha256 {i['corpus_sha256'][:16]}")
        for b in i.get("boundaries", [])[:4]:
            lines.append(f"    boundary: {_bound_text(b)}")
    runs = res.get("runs")
    if runs:
        lines.append("  runs: " + ", ".join(f"{k} {v}" for k, v in runs.items()))
    if res.get("guarantees"):
        g = res["guarantees"]
        lines.append(f"  isolation {res.get('isolation')}: NOT " + ", ".join(k for k, v in g.items() if not v))
    if res.get("claim_id"):
        lines.append(f"  claim {res['claim_id']} ({res.get('claim_status')})")
    for n in res.get("not_checked") or []:
        lines.append(f"  not checked: {n}")
    for lim in res.get("limits") or []:
        lines.append(f"  limit: {lim}")
    if res.get("regression_test"):
        lines += ["", res["regression_test"]]
    if res.get("next_step"):
        lines.append(f"  next: {res['next_step']}")
    return "\n".join(lines)


def _bound_text(b: dict) -> str:
    if b["kind"] == "rounding":
        return "rounding edges (the code rounds or truncates)"
    return f"{b['kind']} {b['value']!r} from {b['source']}"


def _brief_text(b: dict | None) -> str:
    if not b:
        return "-"
    if b.get("not_run"):
        return "not run"
    if b.get("timeout"):
        return "timed out"
    if b.get("blocked"):
        return f"blocked ({b['blocked']})"
    if "raises" in b:
        t = b["raises"]
        return f"raises {t[9:] if t.startswith('builtins.') else t}"
    r = b.get("returns") or ""
    return f"returns {r[:100]}" + ("..." if len(r) > 100 else "")
