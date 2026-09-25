"""Which names a Python module, class or instance has, read from source (docs/DESIGN.md D31).

Nothing here imports or runs the code. Each collector also records why its
answer might be incomplete - the *closed-world* rule of :mod:`verinoda.codecheck`:
a name may be called absent only from a container whose names are all visible
in its source. A module is not closed when it defines ``__getattr__``, calls
``exec``, writes ``globals()`` or patches itself through ``sys.modules``; a
class is not closed under an unknown decorator or metaclass; an instance is
not closed when its class (or a base) defines ``__getattr__``, sets attributes
through ``setattr``/``__dict__``, or hands ``self`` to other code.
"""

from __future__ import annotations

import ast
import hashlib
import os
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

# decorators that keep a function's signature (or only wrap it in a descriptor), by where they come from:
# a project's own `cache` or `dataclass` may do anything
SAFE_FUNC_DECORATORS = {
    "builtins.staticmethod", "builtins.classmethod", "abc.abstractmethod", "functools.cache",
    "functools.lru_cache", "typing.final", "typing_extensions.final", "typing.override",
    "typing_extensions.override", "contextlib.contextmanager", "contextlib.asynccontextmanager",
    "functools.wraps", "functools.total_ordering",
}
# class decorators that add dunder methods only
SAFE_CLASS_DECORATORS = {"dataclasses.dataclass", "functools.total_ordering", "enum.unique", "typing.final",
                         "typing_extensions.final", "typing.runtime_checkable",
                         "typing_extensions.runtime_checkable", "typing.dataclass_transform",
                         "typing_extensions.dataclass_transform"}
SAFE_METACLASSES = {"type", "ABCMeta", "EnumMeta", "EnumType"}
# builtins that cannot set attributes on an object passed to them
SELF_SAFE_CALLS = {
    "isinstance", "issubclass", "id", "hash", "repr", "str", "len", "type", "print", "getattr", "hasattr",
    "super", "iter", "next", "bool", "format", "sorted", "list", "tuple", "set", "frozenset", "min", "max",
    "sum", "any", "all", "enumerate", "zip", "map", "filter", "reversed", "callable", "int", "float", "abs",
    "round", "ascii", "bin", "hex", "oct", "ord", "chr", "divmod", "pow", "bytes", "ref", "proxy", "dict",
}
# methods of a dict that do not add keys
DICT_READS = {"get", "keys", "items", "values", "copy", "__contains__", "__getitem__"}
# calls that set attributes on the object they are given (vars(): its __dict__ can be changed)
ATTR_SETTERS = {"setattr", "delattr", "vars", "__setattr__", "__delattr__"}
# C functions of the standard library that neither set attributes on an object nor keep it
PURE_C_CALLS = {"dir", "getsizeof", "dumps", "dump", "getrefcount", "pformat", "pprint", "encode", "join"}


@dataclass
class Member:
    name: str
    kind: str                  # function | class | module | variable | import | method | property | attribute
    line: int | None = None
    file: Path | None = None
    owner: str | None = None   # the class that defines an inherited member
    signature: str | None = None


@dataclass
class ModFacts:
    path: Path
    tree: ast.Module | None
    names: dict[str, Member] = field(default_factory=dict)
    stars: list[tuple[int, str | None, int]] = field(default_factory=list)   # (level, module, line)
    open: list[str] = field(default_factory=list)
    all_names: list[str] | None = None
    all_dynamic: bool = False
    path_hack: bool = False
    classes: dict[int, ast.ClassDef] = field(default_factory=dict)
    functions: dict[int, ast.AST] = field(default_factory=dict)
    sys_path_edit: bool = False
    error: str | None = None
    expr_calls: list[ast.Call] = field(default_factory=list)   # module-level calls whose result is dropped
    own_submodules: set[str] = field(default_factory=set)      # a package __init__: submodules it imports
    imports: dict[str, str] = field(default_factory=dict)      # local name -> what it imports ("functools.cache")


def origin_of(name: str, mf: "ModFacts | None") -> str:
    """Where a dotted name used in a module comes from: ``lru_cache`` imported from functools ->
    "functools.lru_cache"; a name the module defines -> "<local>.name"; anything else is a builtin."""
    root, _, rest = name.partition(".")
    if mf is not None and root in mf.imports:
        return mf.imports[root] + ("." + rest if rest else "")
    if mf is not None and root in mf.names:
        return "<local>." + name
    return "builtins." + name


def dotted(node: ast.AST | None) -> str | None:
    """``a.b.c`` for a Name/Attribute chain (calls stripped: ``@x.y(...)`` -> ``x.y``)."""
    if isinstance(node, ast.Call):
        node = node.func
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _last(name: str | None) -> str:
    return (name or "").rsplit(".", 1)[-1]


def bind_targets(t: ast.AST, out: list[tuple[str, int]]) -> None:
    if isinstance(t, ast.Name):
        out.append((t.id, t.lineno))
    elif isinstance(t, (ast.Tuple, ast.List)):
        for e in t.elts:
            bind_targets(e, out)
    elif isinstance(t, ast.Starred):
        bind_targets(t.value, out)


def _pattern_names(p: ast.AST, out: list[tuple[str, int]]) -> None:
    for n in ast.walk(p):
        name = getattr(n, "name", None) if type(n).__name__ in ("MatchAs", "MatchStar") else None
        rest = getattr(n, "rest", None) if type(n).__name__ == "MatchMapping" else None
        for x in (name, rest):
            if isinstance(x, str):
                out.append((x, getattr(n, "lineno", 0)))


_SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


def _walk_no_scopes(node: ast.AST):
    """ast.walk that yields function, lambda and class nodes (the root too) but does not enter their bodies."""
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        if isinstance(n, _SCOPE_NODES):
            continue
        stack.extend(ast.iter_child_nodes(n))


_TRY = tuple(t for t in (ast.Try, getattr(ast, "TryStar", None)) if t is not None)
_MATCH = getattr(ast, "Match", None)


def _block_bindings(stmts: list[ast.stmt], add, on_star=None, on_all=None) -> None:
    """Names bound by a block of statements (module or class level), descending into compound statements."""
    for s in stmts:
        if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)):
            add(s.name, "function", s.lineno, s)
        elif isinstance(s, ast.ClassDef):
            add(s.name, "class", s.lineno, s)
        elif isinstance(s, ast.Assign):
            out: list[tuple[str, int]] = []
            for t in s.targets:
                bind_targets(t, out)
            for n, ln in out:
                add(n, "variable", ln, s)
            if on_all and any(isinstance(t, ast.Name) and t.id == "__all__" for t in s.targets):
                on_all("set", s.value)
        elif isinstance(s, ast.AnnAssign):
            if isinstance(s.target, ast.Name):
                add(s.target.id, "variable", s.lineno, s)
                if on_all and s.target.id == "__all__" and s.value is not None:
                    on_all("set", s.value)
        elif isinstance(s, ast.AugAssign):
            if isinstance(s.target, ast.Name):
                add(s.target.id, "variable", s.lineno, s)
                if on_all and s.target.id == "__all__":
                    on_all("add", s.value)
        elif isinstance(s, ast.Import):
            for a in s.names:
                add(a.asname or a.name.split(".")[0], "module", s.lineno, s)
        elif isinstance(s, ast.ImportFrom):
            for a in s.names:
                if a.name == "*":
                    if on_star:
                        on_star(s.level or 0, s.module, s.lineno)
                else:
                    add(a.asname or a.name, "import", s.lineno, s)
        elif isinstance(s, ast.If):
            _block_bindings(s.body, add, on_star, on_all)
            _block_bindings(s.orelse, add, on_star, on_all)
        elif isinstance(s, (ast.For, ast.AsyncFor)):
            out = []
            bind_targets(s.target, out)
            for n, ln in out:
                add(n, "variable", ln, s)
            _block_bindings(s.body, add, on_star, on_all)
            _block_bindings(s.orelse, add, on_star, on_all)
        elif isinstance(s, ast.While):
            _block_bindings(s.body, add, on_star, on_all)
            _block_bindings(s.orelse, add, on_star, on_all)
        elif isinstance(s, (ast.With, ast.AsyncWith)):
            for it in s.items:
                if it.optional_vars is not None:
                    out = []
                    bind_targets(it.optional_vars, out)
                    for n, ln in out:
                        add(n, "variable", ln, s)
            _block_bindings(s.body, add, on_star, on_all)
        elif _TRY and isinstance(s, _TRY):
            _block_bindings(s.body, add, on_star, on_all)
            for h in s.handlers:
                _block_bindings(h.body, add, on_star, on_all)
            _block_bindings(s.orelse, add, on_star, on_all)
            _block_bindings(s.finalbody, add, on_star, on_all)
        elif _MATCH is not None and isinstance(s, _MATCH):
            for case in s.cases:
                out = []
                _pattern_names(case.pattern, out)
                for n, ln in out:
                    add(n, "variable", ln, s)
                _block_bindings(case.body, add, on_star, on_all)
        elif isinstance(s, ast.Expr) and on_all and isinstance(s.value, ast.Call):
            f = s.value.func
            if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id == "__all__":
                on_all(f.attr, s.value.args[0] if s.value.args else None)
        # walrus targets at this level (also inside comprehensions) bind in this scope
        if not isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for n in _walk_no_scopes(s):
                if isinstance(n, ast.NamedExpr) and isinstance(n.target, ast.Name):
                    add(n.target.id, "variable", n.lineno, s)


def _str_list(node: ast.AST | None) -> list[str] | None:
    if isinstance(node, (ast.List, ast.Tuple)) and all(isinstance(e, ast.Constant) and isinstance(e.value, str)
                                                       for e in node.elts):
        return [e.value for e in node.elts]  # type: ignore[attr-defined]
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    return None


def _is_sys_modules(node: ast.AST) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == "modules" and dotted(node.value) == "sys"


def _at_module_level(node: ast.AST, parents: dict[int, ast.AST]) -> bool:
    cur = parents.get(id(node))
    while cur is not None:
        if isinstance(cur, _SCOPE_NODES):
            return False
        cur = parents.get(id(cur))
    return True


def module_facts_from_tree(path: Path, tree: ast.Module) -> ModFacts:
    f = ModFacts(path=path, tree=tree)

    def add(name, kind, line, _node):
        f.names.setdefault(name, Member(name, kind, line, path))
        if name == "__getattr__":
            f.open.append(f"defines a module-level __getattr__ (line {line})")

    def on_star(level, module, line):
        f.stars.append((level, module, line))

    def on_all(op, value):
        vals = _str_list(value)
        if op in ("set",) and vals is not None and f.all_names is None:
            f.all_names = list(vals)
        elif op in ("set", "add", "extend", "append") and vals is not None and f.all_names is not None:
            f.all_names += vals
        elif op not in ("remove", "sort"):
            f.all_dynamic = True

    _block_bindings(tree.body, add, on_star, on_all)
    parents: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for c in ast.iter_child_nodes(node):
            parents[id(c)] = node
    for node in ast.walk(tree):
        if isinstance(node, ast.Global):
            for n in node.names:
                f.names.setdefault(n, Member(n, "variable", node.lineno, path))
        elif isinstance(node, ast.ClassDef):
            f.classes[node.lineno] = node
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            f.functions[node.lineno] = node
        elif isinstance(node, ast.Name) and node.id == "__path__":
            f.path_hack = True
        elif isinstance(node, ast.Call):
            fn = dotted(node.func)
            if fn in ("exec", "builtins.exec"):
                f.open.append(f"calls exec() (line {node.lineno})")
            elif (fn in ("globals", "vars") or (fn == "locals" and _at_module_level(node, parents))) and \
                    not node.args:   # at module level, locals() is the module's namespace too
                p = parents.get(id(node))
                reading = (isinstance(p, ast.Subscript) and isinstance(p.ctx, ast.Load)) or \
                    (isinstance(p, ast.Attribute) and p.attr in ("get", "keys", "items", "values", "__contains__")
                     and isinstance(parents.get(id(p)), ast.Call)) or isinstance(p, ast.Compare)
                if not reading:
                    f.open.append(f"writes or hands out {fn}() (line {node.lineno})")
            elif fn in ("sys.path.insert", "sys.path.append", "sys.path.extend", "site.addsitedir"):
                f.sys_path_edit = True
            elif fn == "setattr" and node.args and _is_sys_modules(getattr(node.args[0], "value", None) or ast.Pass()):
                f.open.append(f"sets attributes on itself through sys.modules (line {node.lineno})")
        elif isinstance(node, ast.Subscript) and _is_sys_modules(node.value):
            key = node.slice
            if isinstance(node.ctx, ast.Store) or (isinstance(key, ast.Name) and key.id == "__name__"):
                f.open.append(f"replaces or patches a module through sys.modules (line {node.lineno})")
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store) and dotted(node.value) == "sys.path":
            f.sys_path_edit = True
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if dotted(t) == "sys.path":
                    f.sys_path_edit = True
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call) and _at_module_level(node, parents):
            f.expr_calls.append(node.value)
        if isinstance(node, ast.Import):
            for al in node.names:   # `import a.b` binds a; `import a.b as c` binds c to a.b
                top = al.name.split(".")[0]
                f.imports.setdefault(al.asname or top, al.name if al.asname else top)
        elif isinstance(node, ast.ImportFrom):
            base = "." * (node.level or 0) + (node.module or "")
            for al in node.names:
                if al.name != "*":
                    f.imports.setdefault(al.asname or al.name, f"{base}.{al.name}" if node.module else base + al.name)
        if path.name.startswith("__init__.") and isinstance(node, (ast.Import, ast.ImportFrom)) and \
                _at_module_level(node, parents):
            f.own_submodules |= _own_submodule_imports(node, path.parent.name)
    return f


def _own_submodule_imports(node: ast.AST, pkg: str) -> set[str]:
    """The submodules of package ``pkg`` an import in its __init__ binds as attributes of the package
    (``from . import a``, ``from .a import x``, ``import pkg.a``)."""
    def after_pkg(parts: list[str]) -> str | None:   # `a.pkg.sub.x` -> "sub" (pkg nested or not)
        idx = max((i for i, p in enumerate(parts[:-1]) if p == pkg), default=None)
        return parts[idx + 1] if idx is not None else None

    out: set[str] = set()
    if isinstance(node, ast.ImportFrom):
        mod = (node.module or "").split(".")
        if node.level == 1:
            if mod[0]:
                out.add(mod[0])
            else:
                out |= {al.name for al in node.names if al.name != "*"}
        elif not node.level and after_pkg(mod + [""]):
            out.add(after_pkg(mod + [""]))   # type: ignore[arg-type]
    elif isinstance(node, ast.Import):
        for al in node.names:
            sub = after_pkg(al.name.split("."))
            if sub:
                out.add(sub)
    return out


# -- classes ----------------------------------------------------------------------------------------

@dataclass
class ClassFacts:
    node: ast.ClassDef
    path: Path
    qualname: str
    body: dict[str, Member] = field(default_factory=dict)
    inst: dict[str, Member] = field(default_factory=dict)
    open_class: list[str] = field(default_factory=list)
    open_inst: list[str] = field(default_factory=list)
    defines_new: bool = False
    defines_getattr: bool = False
    init_subclass: bool = False
    dataclass: bool = False
    decorators: list[str] = field(default_factory=list)
    escapes: list[tuple[ast.Call, tuple[str, object], str]] = field(default_factory=list)   # self handed to calls
    open_dict: list[str] = field(default_factory=list)   # reasons that matter only when instances have a __dict__
    slots: list[str] | None = None                       # the literal __slots__ (None: the class has none)


def arg_ref(call: ast.Call, name: str) -> tuple[str, object] | None:
    """Where the bare name ``name`` is passed in ``call``: ("pos", i), ("kw", k), ("star", None) or None."""
    for i, a in enumerate(call.args):
        if isinstance(a, ast.Name) and a.id == name:
            return ("pos", i)
        if isinstance(a, ast.Starred) and any(isinstance(x, ast.Name) and x.id == name for x in ast.walk(a)):
            return ("star", None)
    for k in call.keywords:
        if isinstance(k.value, ast.Name) and k.value.id == name:
            return ("kw", k.arg) if k.arg else ("star", None)
    return None


def param_writes(fn: ast.AST, param: str) -> tuple[list[str], list[tuple[ast.Call, tuple[str, object], str]]]:
    """What ``fn`` does with its parameter ``param``: (reasons it may set attributes, calls it is passed to)."""
    why: list[str] = []
    passed: list[tuple[ast.Call, tuple[str, object], str]] = []
    parents: dict[int, ast.AST] = {}
    for n in ast.walk(fn):
        for ch in ast.iter_child_nodes(n):
            parents[id(ch)] = n
    for n in ast.walk(fn):
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == param:
            if isinstance(n.ctx, (ast.Store, ast.Del)) or n.attr in ("__dict__", "__class__", "__setattr__"):
                why.append(f"{getattr(fn, 'name', '?')}() sets {param}.{n.attr} (line {n.lineno})")
        elif isinstance(n, ast.Call):
            fname = dotted(n.func) or ""
            ref = arg_ref(n, param)
            if ref is None:
                continue
            if _last(fname) in ATTR_SETTERS:
                why.append(f"{getattr(fn, 'name', '?')}() calls {fname}({param}, ...) (line {n.lineno})")
            elif _last(fname) not in SELF_SAFE_CALLS:
                passed.append((n, ref, fname or "a call"))
        elif isinstance(n, ast.Name) and n.id == param and isinstance(n.ctx, ast.Load):
            p = parents.get(id(n))
            if isinstance(p, (ast.Assign, ast.AnnAssign, ast.NamedExpr, ast.Return, ast.Yield, ast.List, ast.Tuple,
                              ast.Set, ast.Dict)) and not isinstance(p, ast.Call):
                why.append(f"{getattr(fn, 'name', '?')}() stores or returns {param} (line {n.lineno})")
    return why, passed


def _func_decorators(fn: ast.AST) -> list[str]:
    return [dotted(d) or "<expr>" for d in getattr(fn, "decorator_list", [])]


def method_kind(fn: ast.AST) -> str:
    decos = {_last(d) for d in _func_decorators(fn)}
    if decos & {"property", "cached_property", "getter", "setter", "deleter"}:
        return "property"
    if "staticmethod" in decos:
        return "staticmethod"
    if "classmethod" in decos:
        return "classmethod"
    return "method"


def _first_param(fn: ast.AST) -> str | None:
    a = fn.args  # type: ignore[attr-defined]
    ps = list(a.posonlyargs) + list(a.args)
    return ps[0].arg if ps else None


def class_facts(node: ast.ClassDef, path: Path, qualname: str, mf: ModFacts | None = None) -> ClassFacts:
    c = ClassFacts(node=node, path=path, qualname=qualname)
    for d in node.decorator_list:
        name = dotted(d) or "<expr>"
        c.decorators.append(name)
        full = origin_of(name, mf)
        if full == "dataclasses.dataclass":
            c.dataclass = True
        if full not in SAFE_CLASS_DECORATORS:
            why = f"class decorator @{name} may change the class (line {d.lineno})"
            c.open_class.append(why)
            c.open_inst.append(why)
    for kw in node.keywords:
        if kw.arg == "metaclass":
            m = dotted(kw.value) or "<expr>"
            if _last(m) not in SAFE_METACLASSES:
                why = f"metaclass {m} may add names"
                c.open_class.append(why)
                c.open_inst.append(why)
        else:
            why = f"class keyword {kw.arg}= goes to __init_subclass__ of a base"
            c.open_class.append(why)
            c.open_inst.append(why)

    methods: list[ast.AST] = []

    def add(name, kind, line, stmt):
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and name == stmt.name:
            kind = method_kind(stmt)
            methods.append(stmt)
            sig = f"{name}({_args_text(stmt)})"
        else:
            sig = None
        c.body.setdefault(name, Member(name, kind, line, path, qualname, sig))
        mangled = _mangled(name, node.name)
        if mangled:   # __x in the body of class C is C._C__x
            c.body.setdefault(mangled, Member(mangled, kind, line, path, qualname, sig))
        if isinstance(stmt, ast.AnnAssign) and stmt.value is None and isinstance(stmt.target, ast.Name):
            c.inst.setdefault(name, Member(name, "attribute", line, path, qualname))
        if name in ("__getattr__", "__getattribute__"):
            c.defines_getattr = True
            c.open_inst.append(f"defines {name} (line {line})")
        elif name == "__new__":
            c.defines_new = True
        elif name == "__init_subclass__":
            c.init_subclass = True
        elif name == "__slots__" and isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            slots = _str_list(stmt.value)
            c.slots = slots if slots is not None else ["__dict__"]   # computed slots: assume a __dict__
            for s in slots or []:
                c.body.setdefault(s, Member(s, "attribute", line, path, qualname))

    _block_bindings(node.body, add)
    for n in ast.walk(node):
        if isinstance(n, ast.Call) and dotted(n.func) in ("locals", "vars", "exec") and not n.args and \
                _owner_scope(node, n) is node:
            c.open_class.append(f"calls {dotted(n.func)}() in the class body (line {n.lineno})")
    for fn in methods:
        if method_kind(fn) == "staticmethod":
            continue
        _instance_writes(c, fn)
    return c


def _owner_scope(cls: ast.ClassDef, target: ast.AST) -> ast.AST | None:
    for stmt in cls.body:
        for n in _walk_no_scopes(stmt):
            if n is target:
                return cls
    return None


def _args_text(fn: ast.AST) -> str:
    try:
        return ast.unparse(fn.args)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return "..."


def _mangled(name: str, cls_name: str) -> str | None:
    """``__x`` written inside class ``C`` is stored as ``_C__x``."""
    if name.startswith("__") and not name.endswith("__") and cls_name.strip("_"):
        return f"_{cls_name.lstrip('_')}{name}"
    return None


def _instance_writes(c: ClassFacts, fn: ast.AST) -> None:
    """Attributes a method sets on its first parameter, and what makes the instance open. A classmethod's
    first parameter is the class: what it sets there is a class attribute (instances see it too)."""
    me = _first_param(fn)
    if not me:
        return
    on_class = method_kind(fn) == "classmethod"
    dest = c.body if on_class else c.inst
    cls_name = c.qualname.rsplit(".", 1)[-1]

    def record(name: str, line: int) -> None:
        dest.setdefault(name, Member(name, "attribute", line, c.path, c.qualname))
        m = _mangled(name, cls_name)
        if m:
            dest.setdefault(m, Member(m, "attribute", line, c.path, c.qualname))

    parents: dict[int, ast.AST] = {}
    for n in ast.walk(fn):
        for ch in ast.iter_child_nodes(n):
            parents[id(ch)] = n
    for n in ast.walk(fn):
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == me:
            if isinstance(n.ctx, ast.Store):
                if n.attr == "__class__":
                    c.open_inst.append(f"reassigns {me}.__class__ (line {n.lineno})")
                record(n.attr, n.lineno)
            elif n.attr == "__dict__":
                if on_class:
                    c.open_class.append(f"uses {me}.__dict__ in a classmethod (line {n.lineno})")
                c.open_dict.append(f"uses {me}.__dict__ (line {n.lineno})")
        elif isinstance(n, ast.Call):
            f = n.func
            fname = dotted(f) or ""
            args = list(n.args) + [k.value for k in n.keywords]
            target: ast.AST | None = None
            key: ast.AST | None = None
            if fname == "setattr" and len(n.args) >= 2:
                target, key = n.args[0], n.args[1]
            elif isinstance(f, ast.Attribute) and f.attr == "__setattr__":
                recv = f.value
                if (isinstance(recv, ast.Name) and recv.id == me) or \
                        (isinstance(recv, ast.Call) and dotted(recv.func) == "super"):
                    target, key = ast.Name(id=me, ctx=ast.Load()), (n.args[0] if n.args else None)
                elif n.args:
                    target, key = n.args[0], (n.args[1] if len(n.args) > 1 else None)
            if target is not None:
                if isinstance(target, ast.Name) and target.id == me:
                    if isinstance(key, ast.Constant) and isinstance(key.value, str):
                        dest.setdefault(key.value, Member(key.value, "attribute", n.lineno, c.path, c.qualname))
                    else:
                        why = f"sets attributes by computed name ({fname}, line {n.lineno})"
                        if on_class:
                            c.open_class.append(why)
                            c.open_inst.append(why)
                        c.open_dict.append(why)
                continue
            if fname == "vars" and args and isinstance(args[0], ast.Name) and args[0].id == me:
                if on_class:
                    c.open_class.append(f"uses vars({me}) in a classmethod (line {n.lineno})")
                c.open_dict.append(f"uses vars({me}) (line {n.lineno})")
                continue
            if _last(fname) in SELF_SAFE_CALLS or fname.endswith(".__init__") or fname.endswith(".__new__"):
                continue
            ref = arg_ref(n, me)
            if ref is not None:
                # resolved later (the checker looks at what the callee does with it)
                c.escapes.append((n, ref, fname or "a call"))
        elif isinstance(n, ast.Name) and n.id == me and isinstance(n.ctx, ast.Load):
            p = parents.get(id(n))
            if isinstance(p, (ast.Assign, ast.AnnAssign, ast.NamedExpr)) and getattr(p, "value", None) is n:
                c.open_dict.append(f"aliases {me} (line {n.lineno})")
            elif isinstance(p, (ast.Tuple, ast.List, ast.Set, ast.Dict, ast.Starred)):
                # `a, b = self, other`, `for obj in (self,)`, `[self]`: another name may hold it
                c.open_dict.append(f"puts {me} in a {type(p).__name__.lower()} (line {n.lineno})")


# -- functions: signature and constant-key dict returns -------------------------------------------------

@dataclass
class Sig:
    """The keyword arguments a callee accepts (``varkw``: it takes ``**kwargs``)."""

    names: list[str]
    posonly: list[str]
    varkw: bool
    text: str
    at: str | None = None


def signature_of(fn: ast.AST, drop_first: bool) -> Sig:
    a = fn.args  # type: ignore[attr-defined]
    pos = list(a.posonlyargs)
    reg = list(a.args)
    if drop_first:
        if pos:
            pos = pos[1:]
        elif reg:
            reg = reg[1:]
    names = [p.arg for p in reg] + [p.arg for p in a.kwonlyargs]
    return Sig(names=names, posonly=[p.arg for p in pos], varkw=a.kwarg is not None,
               text=f"{getattr(fn, 'name', '?')}({_args_text(fn)})")


def overload_group(tree: ast.Module, fn: ast.AST) -> list[ast.AST]:
    """``fn`` is an ``@overload``: the implementation (a later def of the same name without it), else every
    overload of that name in the same scope (a stub)."""
    def scopes(nodes):
        yield nodes
        for n in nodes:
            for attr in ("body", "orelse", "finalbody"):
                sub = getattr(n, attr, None)
                if isinstance(sub, list) and sub and isinstance(sub[0], ast.stmt):
                    yield from scopes(sub)
            for h in getattr(n, "handlers", []) or []:
                yield from scopes(h.body)

    for body in scopes(tree.body):
        if any(s is fn for s in body):
            same = [s for s in body if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and s.name == getattr(fn, "name", None)]
            impl = [s for s in same if "overload" not in {_last(d) for d in _func_decorators(s)}]
            return impl[-1:] if impl else same
    return [fn]


def def_variants(tree: ast.Module, fn: ast.AST) -> list[ast.AST]:
    """Every definition of ``fn``'s name in the same scope (module or class body), through if/else and try
    (``if sys.version_info >= ...: def read(self, n) else: def read(self, n, *, timeout=None)``)."""
    name = getattr(fn, "name", None)

    def scope_of(nodes, owner):
        for n in nodes:
            if n is fn:
                return owner
            if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                hit = scope_of(n.body, n)
            else:
                subs = [getattr(n, a, None) or [] for a in ("body", "orelse", "finalbody")]
                subs += [h.body for h in getattr(n, "handlers", []) or []]
                subs += [c.body for c in getattr(n, "cases", []) or []]
                hit = next((r for r in (scope_of(s, owner) for s in subs if isinstance(s, list)) if r is not None),
                           None)
            if hit is not None:
                return hit
        return None

    owner = scope_of(tree.body, tree)
    if owner is None:
        return [fn]
    out = [n for stmt in owner.body for n in _walk_no_scopes(stmt)   # type: ignore[attr-defined]
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name]
    return out or [fn]


def merge_sigs(sigs: list[Sig]) -> Sig:
    """Keyword arguments any of several overloads accepts."""
    names: list[str] = []
    for s in sigs:
        names += [n for n in s.names if n not in names]
    posonly = [p for p in sigs[0].posonly if all(p in s.posonly for s in sigs)] if sigs else []
    return Sig(names=names, posonly=posonly, varkw=any(s.varkw for s in sigs),
               text=" | ".join(s.text for s in sigs[:3]) + (" | ..." if len(sigs) > 3 else ""))


def unsafe_decorators(fn: ast.AST, mf: ModFacts | None = None) -> list[str]:
    """The decorators of ``fn`` that may change what it accepts (anything not known by its origin)."""
    out = []
    for d in _func_decorators(fn):
        if _last(d) in ("setter", "getter", "deleter", "overload") or origin_of(d, mf) in SAFE_FUNC_DECORATORS:
            continue
        out.append(d)
    return out


def dict_return_keys(fn: ast.AST) -> tuple[dict[str, int], str | None]:
    """Keys of the dict literals ``fn`` returns: ({key: line}, None), or ({}, why not)."""
    keys: dict[str, int] = {}
    returns = [n for n in _walk_no_scopes_body(fn) if isinstance(n, ast.Return)]
    if not returns:
        return {}, "it has no return statement"
    if any(isinstance(n, (ast.Yield, ast.YieldFrom)) for n in _walk_no_scopes_body(fn)):
        return {}, "it is a generator"
    for r in returns:
        v = r.value
        if not isinstance(v, ast.Dict):
            return {}, f"line {r.lineno} returns something other than a dict literal"
        for k in v.keys:
            if k is None:
                return {}, f"the dict at line {r.lineno} unpacks another mapping (**)"
            if not (isinstance(k, ast.Constant) and isinstance(k.value, str)):
                return {}, f"the dict at line {r.lineno} has a computed key"
            keys.setdefault(k.value, k.lineno)
    return keys, None


def _walk_no_scopes_body(fn: ast.AST):
    for stmt in getattr(fn, "body", []):
        yield from _walk_no_scopes(stmt)


# -- parse cache ------------------------------------------------------------------------------------------

_TREES: OrderedDict = OrderedDict()
_TREES_MAX = 512
_MODFACTS: dict[tuple, ModFacts] = {}
_CLASSFACTS: dict[tuple, ClassFacts] = {}
# text that stands for a file while a snippet is checked as that file (``check --stdin --as PATH``)
_OVERRIDES: dict[str, tuple[str, str, Path]] = {}


def _okey(path: Path | str) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


class override:
    """``with override(path, text):`` - every reader here (and the checker's jedi scripts) sees ``text`` as
    the content of ``path``: a snippet's own definitions are the source of truth for the file it is meant
    for, even when an older version of that file is on disk."""

    def __init__(self, path: Path, text: str):
        self.key = _okey(path)
        self.value = (text, hashlib.sha256(text.encode("utf-8", "replace")).hexdigest(), Path(path))

    def __enter__(self):
        self.prev = _OVERRIDES.get(self.key)
        _OVERRIDES[self.key] = self.value
        return self

    def __exit__(self, *exc):
        if self.prev is None:
            _OVERRIDES.pop(self.key, None)
        else:
            _OVERRIDES[self.key] = self.prev
        return False


def overridden(path: Path | str) -> str | None:
    """The text standing for ``path`` (see :class:`override`), or None."""
    hit = _OVERRIDES.get(_okey(path)) if _OVERRIDES else None
    return hit[0] if hit else None


def overridden_paths() -> list[Path]:
    return [v[2] for v in _OVERRIDES.values()]


def split_lines(text: str) -> list[str]:
    """Lines as the tokenizer counts them: only \\n, \\r\\n and \\r end a line (str.splitlines also splits
    at form feeds and other separators, which shifts every later line)."""
    lines = re.split(r"\r\n|\r|\n", text)
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def stat_key(path: Path) -> tuple | None:
    if _OVERRIDES:
        hit = _OVERRIDES.get(_okey(path))
        if hit is not None:
            return ("<override>", str(path), hit[1])
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (str(path), st.st_mtime_ns, st.st_size)


def read_text(path: Path) -> str:
    """The file's text (UTF-8, errors replaced), or the text standing for it; OSError when unreadable."""
    text = overridden(path)
    if text is not None:
        return text
    return Path(path).read_bytes().decode("utf-8-sig", "replace")   # a byte order mark is not code


def parse_file(path: Path) -> tuple[ast.Module | None, list[str], str | None]:
    """(tree, lines, error) of a file, cached by its stat."""
    key = stat_key(path)
    if key is None:
        return None, [], "unreadable"
    hit = _TREES.get(key)
    if hit is not None:
        _TREES.move_to_end(key)
        return hit
    try:
        text = read_text(path)
    except OSError as exc:
        return None, [], f"unreadable: {exc}"
    try:
        tree = ast.parse(text, filename=str(path))
        res = (tree, split_lines(text), None)
    except (SyntaxError, ValueError) as exc:
        res = (None, split_lines(text), f"does not parse: {exc}")
    _TREES[key] = res
    while len(_TREES) > _TREES_MAX:
        _TREES.popitem(last=False)
    return res


def module_facts(path: Path) -> ModFacts:
    key = stat_key(path)
    hit = _MODFACTS.get(key) if key else None
    if hit is not None:
        return hit
    tree, _, err = parse_file(path)
    if tree is None:
        f = ModFacts(path=path, tree=None, error=err, open=[f"{path.name} {err}"])
    else:
        f = module_facts_from_tree(path, tree)
    if key:
        if len(_MODFACTS) > 2048:
            _MODFACTS.clear()
        _MODFACTS[key] = f
    return f


def class_at(path: Path, line: int) -> tuple[ClassFacts | None, str | None]:
    """The class defined at ``path:line`` (jedi's line of the class name)."""
    mf = module_facts(path)
    if mf.tree is None:
        return None, mf.error
    node = mf.classes.get(line)
    if node is None:
        return None, f"no class definition at {path.name}:{line}"
    key = (stat_key(path), line)
    hit = _CLASSFACTS.get(key)
    if hit is None:
        hit = _CLASSFACTS[key] = class_facts(node, path, _qualname(mf.tree, node), mf)
    return hit, None


def class_by_qualname(path: Path, qualname: str) -> ClassFacts | None:
    """The class ``qualname`` (``Outer.Inner``) defined in ``path``, or None."""
    mf = module_facts(path)
    if mf.tree is None:
        return None
    for line, node in mf.classes.items():
        if node.name == qualname.rsplit(".", 1)[-1] and _qualname(mf.tree, node) == qualname:
            return class_at(path, line)[0]
    return None


def function_at(path: Path, line: int) -> ast.AST | None:
    mf = module_facts(path)
    return mf.functions.get(line)


def _qualname(tree: ast.Module, target: ast.AST) -> str:
    def walk(nodes, prefix):
        for n in nodes:
            if n is target:
                return prefix + n.name  # type: ignore[attr-defined]
            if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                r = walk(n.body, prefix + n.name + ".")
                if r:
                    return r
            elif isinstance(n, (ast.If, *(_TRY or ()))):
                r = walk(getattr(n, "body", []) + getattr(n, "orelse", []), prefix)
                if r:
                    return r
        return None
    return walk(tree.body, "") or getattr(target, "name", "?")


def file_sha(path: Path) -> str | None:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def reset_caches() -> None:
    _TREES.clear()
    _MODFACTS.clear()
    _CLASSFACTS.clear()
