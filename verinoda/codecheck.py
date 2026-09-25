"""Name-existence check for Python code: ``verinoda check`` and ``verinoda api`` (docs/DESIGN.md D31).

AI-written code imports modules, calls functions, passes keyword arguments and
reads dict keys that do not exist, or not in the installed version. ``check``
walks a file, a diff or a snippet and gives every *site* one verdict:

``exists``        the name was found (where, and in which environment);
``absent``        not found in a container whose names are all known (below);
``unknown``       the container is open or the receiver's type is not known (why);
``not_installed`` the module is not in the environment checked (or there is none);
``guarded``       not found, but the code handles that (try/except ImportError,
                  ``if TYPE_CHECKING``, version and ``hasattr`` guards).

Sites: ``import`` (imports and from-imports), ``attribute`` (``x.name`` loads),
``kwarg`` (keyword arguments of calls) and ``dict_key`` (a constant key read
from the dict literal a function returns).

The closed-world rule decides when ``absent`` is allowed. Only these
containers are closed: a module resolved to a source or stub file with no
module ``__getattr__``, no ``exec``/``globals()`` writes and every star import
closed; a class object; an instance made by a direct constructor call in the
same expression, or held by a single unreassigned local that is not handed to
other code; a from-import; a keyword argument on one known signature without
``**kwargs`` or an unknown decorator; the keys of dict literals a function
returns. A receiver typed only by an annotation or an inferred return value is
``unknown``: a subclass may define the name. Before a name is called absent,
jedi must also fail to find it. Wording: "not found in <container> as
installed in <env> (<file>)", never "does not exist".

Resolution uses jedi (the ``precise`` extra) with the project's own
environment (:mod:`verinoda.codecheck_env`); standard-library names are read
from that environment's interpreter, third-party ones from their installed
source. Without jedi every site is ``unknown`` ("jedi is not installed").

Answers are cached per file under ``.verinoda/cache/check/`` keyed by the file's
sha256 and the environment fingerprint, and dropped when a project file they
were read from, the set of project files, or Verinoda changes.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import subprocess
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from verinoda import codecheck_env as cenv
from verinoda import codecheck_facts as cf
from verinoda.codecheck_facts import Member, Sig, dotted

CHECK_VERSION = "1"
VERDICTS = ("absent", "not_installed", "unknown", "guarded", "exists")
SITE_KINDS = ("import", "attribute", "kwarg", "dict_key")
SKIP_DIRS = {".git", ".hg", ".svn", ".verinoda", "__pycache__", "node_modules", ".tox", ".nox", ".mypy_cache",
             ".pytest_cache", ".ruff_cache", ".eggs", "site-packages", *cenv.VENV_DIRS}
MAX_FILES = 5000
_SPECIAL_BASES = {"typing.Generic", "typing.Protocol", "typing_extensions.Protocol", "typing_extensions.Generic",
                  "builtins.object", "object"}
_OPEN_BASES = {"typing.NamedTuple", "typing.TypedDict", "typing_extensions.TypedDict",
               "typing_extensions.NamedTuple"}
_LITERAL_TYPES = {str: "builtins.str", bytes: "builtins.bytes", int: "builtins.int", float: "builtins.float",
                  complex: "builtins.complex", bool: "builtins.bool"}
_GUARD_TEST = re.compile(r"\bsys\.version_info\b|\bsys\.platform\b|\bos\.name\b|\bplatform\.|\bTYPE_CHECKING\b|"
                         r"\bsys\.implementation\b|\bPY\d*\b|\b_?HAS_\w+|\w+_AVAILABLE\b|\bIS_[A-Z0-9_]+\b")
_CATCH = {"import": {"ImportError", "ModuleNotFoundError"}, "attribute": {"AttributeError"},
          "kwarg": {"TypeError"}, "dict_key": {"KeyError", "LookupError"}}
_SYNONYMS = [{"get", "fetch", "retrieve", "load", "read", "find", "lookup", "query"},
             {"save", "store", "persist", "write", "put", "insert", "add"},
             {"delete", "remove", "drop", "discard", "erase", "del"},
             {"create", "make", "new", "build", "construct"},
             {"update", "modify", "set", "change", "patch"},
             {"list", "all", "iter", "items", "entries"},
             {"start", "begin", "open", "run"}, {"stop", "end", "close", "finish"},
             {"matches", "contains", "match", "includes"}, {"parse", "loads", "decode"},
             {"dumps", "encode", "serialize", "format"}]


def _dunder(name: str) -> bool:
    return len(name) > 4 and name.startswith("__") and name.endswith("__")


def _char_col(line_text: str, byte_col: int) -> int:
    return len(line_text.encode("utf-8")[:byte_col].decode("utf-8", "replace"))


def _split_ident(name: str) -> set[str]:
    parts = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name).lower().split("_")
    return {p for p in parts if p}


# -- containers ----------------------------------------------------------------------------------------

@dataclass
class StdClass:
    full: str
    info: dict


@dataclass
class Container:
    kind: str                       # module | class | instance
    label: str                      # "module packaging.version"
    names: dict[str, Member]
    closed: bool
    why: str | None
    source: str                     # project | stdlib | installed:<dist> <version> | external
    where: str | None               # display path of the container
    files: list[Path] = field(default_factory=list)
    full: str | None = None         # dotted name
    mro: list = field(default_factory=list)   # ClassFacts | StdClass, most derived first
    stdlib_module: str | None = None


# -- scopes ---------------------------------------------------------------------------------------------

_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef, ast.Module)
_COMPS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


class FileCtx:
    """One checked file: its tree, parents and jedi script."""

    def __init__(self, checker: "Checker", rel: str, abs_path: Path, source: str, tree: ast.Module):
        self.checker = checker
        self.rel, self.path, self.source, self.tree = rel, abs_path, source, tree
        self.lines = source.splitlines()
        self.parents: dict[int, ast.AST] = {}
        for n in ast.walk(tree):
            for c in ast.iter_child_nodes(n):
                self.parents[id(c)] = n
        self._script = None
        self.deps: set[Path] = set()
        self.facts = cf.module_facts_from_tree(abs_path, tree)

    def script(self):
        if self._script is None:
            ck = self.checker
            self._script = ck.jedi.Script(self.source, path=str(self.path), project=ck.project,
                                          environment=ck.env.jedi_env)
        return self._script

    def pos(self, line: int, byte_col: int) -> tuple[int, int]:
        text = self.lines[line - 1] if 0 < line <= len(self.lines) else ""
        return line, _char_col(text, byte_col)

    def goto(self, line: int, col: int) -> list:
        return self.checker.goto(self.script(), line, col)

    # -- locals --------------------------------------------------------------------------------------
    def scope_of(self, node: ast.AST) -> ast.AST:
        cur = self.parents.get(id(node))
        while cur is not None and not isinstance(cur, _SCOPES):
            cur = self.parents.get(id(cur))
        return cur or self.tree

    def single_local(self, name: ast.Name, mode: str) -> tuple[ast.Assign | None, str]:
        """The one assignment of a function-local name from a call (``mode`` instance|dict), or why not."""
        n = name.id
        fn = self.scope_of(name)
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return None, f"`{n}` is a module- or class-level name: other code may rebind or change it"
        cur = self.parents.get(id(name))
        while cur is not None and cur is not fn:
            if isinstance(cur, _COMPS) and any(n in {t.id for t in ast.walk(g.target) if isinstance(t, ast.Name)}
                                               for g in cur.generators):
                return None, f"`{n}` is a comprehension variable"
            cur = self.parents.get(id(cur))
        a = fn.args
        if n in {p.arg for p in [*a.posonlyargs, *a.args, *a.kwonlyargs]} or \
                (a.vararg and a.vararg.arg == n) or (a.kwarg and a.kwarg.arg == n):
            return None, f"`{n}` is a parameter: its runtime type is not known (an annotation does not rule out " \
                         "a subclass that has the name)"
        binds: list[ast.AST] = []
        for stmt in fn.body:
            for x in cf._walk_no_scopes(stmt):
                if isinstance(x, ast.Name) and x.id == n and isinstance(x.ctx, (ast.Store, ast.Del)):
                    binds.append(x)
                elif isinstance(x, (ast.Global, ast.Nonlocal)) and n in x.names:
                    return None, f"`{n}` is declared {type(x).__name__.lower()}"
                elif isinstance(x, ast.ExceptHandler) and x.name == n:
                    binds.append(x)
                elif isinstance(x, (ast.Import, ast.ImportFrom)) and \
                        any((al.asname or al.name.split(".")[0]) == n for al in x.names):
                    binds.append(x)
                elif isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and x.name == n \
                        and x is not fn:
                    binds.append(x)
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and stmt.name == n:
                binds.append(stmt)
        for x in ast.walk(fn):
            if isinstance(x, ast.Nonlocal) and n in x.names:
                return None, f"`{n}` is rebound by a nested function (nonlocal)"
            if type(x).__name__ in ("MatchAs", "MatchStar") and getattr(x, "name", None) == n:
                binds.append(x)
        if len(binds) != 1:
            return None, f"`{n}` is bound {len(binds)} times in {fn.name}()"
        st = self.parents.get(id(binds[0]))
        if not (isinstance(st, ast.Assign) and len(st.targets) == 1 and st.targets[0] is binds[0]
                and isinstance(st.value, ast.Call)):
            return None, f"`{n}` is not assigned from a call"
        why = self._escapes(fn, n, mode)
        if why:
            return None, why
        return st, ""

    def _escapes(self, fn: ast.AST, n: str, mode: str) -> str | None:
        for x in ast.walk(fn):
            if not (isinstance(x, ast.Name) and x.id == n and isinstance(x.ctx, ast.Load)):
                continue
            p = self.parents.get(id(x))
            if isinstance(p, ast.Attribute) and p.value is x:
                if isinstance(p.ctx, (ast.Store, ast.Del)):
                    return f"attributes are set on `{n}` (line {p.lineno})" if mode == "instance" else \
                        f"`{n}` is changed (line {p.lineno})"
                if mode == "dict" and p.attr not in cf.DICT_READS:
                    return f"`{n}.{p.attr}` may change the dict (line {p.lineno})"
                if p.attr in ("__dict__", "__class__") and mode == "instance":
                    return f"`{n}.{p.attr}` is used (line {p.lineno})"
                continue
            if isinstance(p, ast.Subscript) and p.value is x:
                if mode == "dict" and isinstance(p.ctx, (ast.Store, ast.Del)):
                    return f"keys are added to or removed from `{n}` (line {p.lineno})"
                continue
            if isinstance(p, (ast.Compare, ast.BoolOp, ast.UnaryOp, ast.If, ast.While, ast.IfExp, ast.Assert,
                              ast.FormattedValue)):
                continue
            if isinstance(p, (ast.Call, ast.keyword, ast.Starred)):
                call = p if isinstance(p, ast.Call) else self.parents.get(id(p))
                if not isinstance(call, ast.Call) or call.func is x:
                    return f"`{n}` is handed to other code (line {getattr(p, 'lineno', '?')})"
                fname = dotted(call.func) or ""
                safe = cf.SELF_SAFE_CALLS if mode == "instance" else {"len", "list", "sorted", "dict", "set", "tuple",
                                                                      "iter", "print", "repr", "str", "bool",
                                                                      "isinstance", "id", "type"}
                if fname.rsplit(".", 1)[-1] in safe:
                    continue
                if mode == "instance":
                    ref = cf.arg_ref(call, n)
                    why = self.checker.callee_mutates(self.script(), self.path, call, ref or ("star", None),
                                                      fname or "a call", 0) if ref else None
                    if why is None:
                        continue
                    return f"`{n}` {why}"
                return f"`{n}` is passed to {fname or 'a call'} (line {call.lineno}), which may change it"
            if isinstance(p, (ast.For, ast.comprehension)) and getattr(p, "iter", None) is x:
                continue
            return f"`{n}` is handed to other code (line {getattr(p, 'lineno', '?')})"
        return None


# -- the checker -------------------------------------------------------------------------------------

class Checker:
    """Resolves and judges sites against one environment (a warm jedi project)."""

    def __init__(self, repo: Path, env: cenv.EnvInfo):
        import jedi

        self.jedi = jedi
        self.repo = Path(repo).resolve()
        self.env = env
        kw = {"sys_path": env.sys_path} if env.kind == "verinoda" else {}
        self.project = jedi.Project(str(self.repo), smart_sys_path=True, **kw)
        self._scripts: OrderedDict = OrderedDict()
        self._mod_memo: dict = {}
        self._cls_memo: dict = {}
        self._universes: dict[str, cenv.ImportUniverse] = {}
        self._builtin_modules: set[str] | None = None
        self._declared: dict | None = None
        self._defs_index: dict | None = None
        self._pkg_index: dict = {}
        self.stats = {"jedi_calls": 0, "jedi_s": 0.0}

    # -- plumbing --------------------------------------------------------------------------------------
    def goto(self, script, line: int, col: int) -> list:
        t0 = time.perf_counter()
        self.stats["jedi_calls"] += 1
        try:
            defs = script.goto(line, col, follow_imports=True)
        except Exception:  # noqa: BLE001 - jedi internal errors happen
            defs = []
        self.stats["jedi_s"] += time.perf_counter() - t0
        uniq: dict = {}
        for d in defs:
            uniq.setdefault((str(d.module_path) if d.module_path else d.module_name, d.line, d.column, d.type), d)
        return list(uniq.values())

    def script_for(self, path: Path):
        key = cf.stat_key(path)
        sc = self._scripts.get(key)
        if sc is None:
            try:
                code = path.read_bytes().decode("utf-8", "replace")
            except OSError:
                return None
            sc = self.jedi.Script(code, path=str(path), project=self.project, environment=self.env.jedi_env)
            self._scripts[key] = sc
            while len(self._scripts) > 24:
                self._scripts.popitem(last=False)
        return sc

    def builtin_modules(self) -> set[str]:
        """Modules compiled into the environment's interpreter (sys, builtins, ...)."""
        if self._builtin_modules is None:
            info = self.env.oracle().ask("sys")
            self._builtin_modules = set(info.get("builtin_module_names", [])) if info.get("ok") else set()
        return self._builtin_modules

    def universe(self, file_dir: Path) -> cenv.ImportUniverse:
        """Where imports of a file in ``file_dir`` are looked up: the project roots, the file's import root
        (its first ancestor that is not a package, as pytest and ``python script.py`` put it on sys.path),
        then the environment's search path."""
        key = os.path.normcase(str(file_dir))
        u = self._universes.get(key)
        if u is None:
            dirs = [self.repo]
            if (self.repo / "src").is_dir():
                dirs.append(self.repo / "src")
            d = file_dir
            while _inside(d, self.repo) and d != self.repo and \
                    ((d / "__init__.py").is_file() or (d / "__init__.pyi").is_file()):
                d = d.parent
            dirs.append(d)
            u = self._universes[key] = cenv.ImportUniverse(self.env, dirs, self.builtin_modules())
        return u

    def disp(self, path: Path | str | None) -> str | None:
        if not path:
            return None
        p = Path(path)
        try:
            return p.resolve().relative_to(self.repo).as_posix()
        except (ValueError, OSError):
            pass
        for d in self.env.stdlib_dirs:
            try:
                return "<stdlib>/" + p.relative_to(d).as_posix()
            except ValueError:
                continue
        ts = cenv.jedi_typeshed_dir()
        if ts:
            try:
                return "<jedi typeshed>/" + p.relative_to(ts).as_posix()
            except ValueError:
                pass
        return p.as_posix()

    def origin(self, path: Path | str | None) -> str | None:
        """installed | stdlib | None (the project, or elsewhere). The project's own files win over any
        search-path directory that happens to contain them (PYTHONPATH, the base prefix)."""
        if not path:
            return None
        p = Path(path)
        if cenv.is_jedi_stdlib_stub(p):
            return "stdlib"
        if cenv.is_jedi_bundled_stub(p):
            return None
        if _under_any(p, self.env.site_dirs):
            return "installed"
        if _inside(p, self.repo):
            return None
        return self.env.origin(p)

    def source_of(self, path: Path | str | None) -> str:
        if not path:
            return "stdlib"
        origin = self.origin(path)
        if origin == "stdlib":
            return "stdlib"
        if origin == "installed":
            d = self.env.dist_of(path)
            return f"installed:{d[0]} {d[1]}" if d else "installed"
        if cenv.is_jedi_bundled_stub(path):
            return "stub:jedi-bundled"
        return "project" if _inside(Path(path), self.repo) else "external"

    def _dep(self, fx: FileCtx | None, path: Path | str | None) -> None:
        if fx is not None and path and _inside(Path(path), self.repo) and self.origin(path) is None:
            fx.deps.add(Path(path))

    def in_env_phrase(self, c: Container) -> str:
        if c.source == "project":
            return "in this project"
        if c.source == "stdlib":
            return f"in the standard library of {self.env.label}"
        if c.source.startswith("installed:"):
            return f"as installed in {self.env.label}, {c.source.split(':', 1)[1]}"
        return f"in {c.source}"

    # -- module containers -----------------------------------------------------------------------------
    def module_container(self, name: str, path: Path | str | None, fx: FileCtx | None = None) -> Container:
        key = (name, str(path) if path else None)
        hit = self._mod_memo.get(key)
        if hit is not None:
            for f in hit.files:
                self._dep(fx, f)
            return hit
        c = self._module_container(name, Path(path) if path else None, set())
        self._mod_memo[key] = c
        for f in c.files:
            self._dep(fx, f)
        return c

    def _module_container(self, name: str, path: Path | None, seen: set) -> Container:
        label = f"module {name}"
        origin = self.origin(path) if path else ("stdlib" if name.split(".")[0] in self.builtin_modules()
                                                     else None)
        if origin == "stdlib":
            info = self.env.oracle().ask("module", name=name)
            if info.get("ok"):
                names = {n: Member(n, "variable") for n in info["names"]}
                for s in info.get("submodules", []):
                    names.setdefault(s, Member(s, "module"))
                why = "the module defines __getattr__ (names can be made on access)" if info.get("getattr") else None
                return Container("module", label, names, why is None, why, "stdlib",
                                 self.disp(info.get("file")) or f"<stdlib>/{name}", full=name, stdlib_module=name)
            if path is None or path.suffix != ".py":
                return Container("module", label, {}, False, f"the standard-library oracle could not load it "
                                 f"({info.get('error')})", "stdlib", self.disp(path), full=name)
        if path is None:
            return Container("module", label, {}, False, "a compiled or built-in module without source; not "
                             "inspected", "installed" if origin else "project", None, full=name)
        if cenv.is_jedi_bundled_stub(path):
            return Container("module", label, {}, False, "only a stub bundled with jedi describes it; the installed "
                             "version may differ", "stub:jedi-bundled", self.disp(path), full=name)
        files = [path]
        twin = path.with_suffix(".pyi" if path.suffix == ".py" else ".py")
        if twin.is_file():
            files.append(twin)
        names: dict[str, Member] = {}
        why: list[str] = []
        for f in files:
            mf = cf.module_facts(f)
            if mf.tree is None:
                why.append(f"{f.name} {mf.error}")
                continue
            for k, m in mf.names.items():
                names.setdefault(k, m)
            why += mf.open
            for level, mod, line in mf.stars:
                star = self._star_names(f, level, mod, seen | {str(f)})
                if isinstance(star, str):
                    why.append(f"star import from {'.' * level}{mod or ''} (line {line}): {star}")
                else:
                    for k, m in star.items():
                        names.setdefault(k, m)
            if f.name.startswith("__init__."):
                subs, open_subs = _submodules(f.parent)
                for s in subs:
                    names.setdefault(s, Member(s, "module", None, f.parent / s))
                if mf.path_hack:
                    why.append("the package changes its __path__ (submodules may come from elsewhere)")
                del open_subs
        src = self.source_of(path)
        return Container("module", label, names, not why, "; ".join(dict.fromkeys(why)) or None, src,
                         self.disp(path), files=files, full=name)

    def _star_names(self, importer: Path, level: int, mod: str | None, seen: set) -> dict[str, Member] | str:
        if level:
            base = importer.parent
            for _ in range(level - 1):
                base = base.parent
            specs = _find_in(base, (mod or "").split(".")) if mod else \
                [cenv.ModSpec(base.name, "package", base / "__init__.py", [base])]
        else:
            specs = self.universe(importer.parent).find(mod or "")
        specs = [s for s in specs if s.kind != "namespace"]
        if len(specs) != 1:
            return "the module is not found" if not specs else "the module is found in several places"
        spec = specs[0]
        if spec.kind in ("compiled", "builtin") or spec.file is None:
            origin = self.origin(spec.file) if spec.file else "stdlib"
            if origin == "stdlib" and mod and not level:
                info = self.env.oracle().ask("module", name=mod)
                if info.get("ok"):
                    if info.get("getattr"):
                        return "that module defines __getattr__"
                    return {n: Member(n, "variable") for n in info["names"] if not n.startswith("_")}
            return "a compiled module; its names are not read"
        if str(spec.file) in seen:
            return {}
        mf = cf.module_facts(spec.file)
        if mf.tree is None:
            return mf.error or "does not parse"
        if mf.open:
            return mf.open[0]
        names = dict(mf.names)
        for lv, m2, _ln in mf.stars:
            sub = self._star_names(spec.file, lv, m2, seen | {str(spec.file)})
            if isinstance(sub, str):
                return sub
            for k, m in sub.items():
                names.setdefault(k, m)
        if mf.all_names is not None and not mf.all_dynamic:
            return {k: names.get(k) or Member(k, "variable", None, spec.file) for k in mf.all_names}
        if mf.all_dynamic:
            return names
        return {k: m for k, m in names.items() if not k.startswith("_")}

    # -- class containers ------------------------------------------------------------------------------
    def class_container(self, d, instance: bool, fx: FileCtx | None = None) -> Container:
        """Container for a jedi class definition ``d`` (class-object access, or an instance of it)."""
        key = (str(d.module_path), d.line, d.full_name, instance)
        hit = self._cls_memo.get(key)
        if hit is None:
            hit = self._cls_memo[key] = self._class_container(d, instance)
        for f in hit.files:
            self._dep(fx, f)
        return hit

    def _class_container(self, d, instance: bool) -> Container:
        full = d.full_name or d.name
        path = Path(d.module_path) if d.module_path else None
        origin = self.origin(path) if path else "stdlib"
        if origin == "stdlib":
            return self.std_class_container(full, instance)
        if path is None or cenv.is_jedi_bundled_stub(path):
            what = "instance of class" if instance else "class"
            return Container("instance" if instance else "class", f"{what} {full}", {}, False,
                             "only a stub bundled with jedi describes it" if path else "no source", "external",
                             self.disp(path), full=full)
        facts, err = cf.class_at(path, d.line)
        if facts is None:
            return Container("instance" if instance else "class", f"class {full}", {}, False, err, "external",
                             self.disp(path), full=full)
        return self.facts_container(facts, full, instance)

    def facts_container(self, facts: cf.ClassFacts, full: str, instance: bool) -> Container:
        mro, why = self._mro(facts)
        names: dict[str, Member] = {}
        files: list[Path] = []
        opens: list[str] = list(why)
        # instances without a __dict__ (every class slotted) hold only the attributes their classes declare
        no_dict = all((not e.info.get("inst_dict")) if isinstance(e, StdClass) else
                      (e.slots is not None and "__dict__" not in e.slots) for e in mro)
        for i, e in enumerate(mro):
            if isinstance(e, StdClass):
                info = e.info
                for n in info.get("names", []):
                    names.setdefault(n, Member(n, "attribute", None, None, e.full))
                if not instance:
                    for n in info.get("meta_names", []):
                        names.setdefault(n, Member(n, "attribute", None, None, "type"))
                    if info.get("meta_getattr") or info.get("custom_meta"):
                        opens.append(f"the metaclass of {e.full} may add names")
                else:
                    opens += self._std_instance_open(e, no_dict)
                    for n, m in self._std_instance_names(e).items():
                        names.setdefault(n, m)
                continue
            files.append(e.path)
            for n, m in e.body.items():
                names.setdefault(n, m)
            if instance:
                for n, m in e.inst.items():
                    names.setdefault(n, m)
                opens += e.open_inst
                if not no_dict:
                    opens += e.open_dict
                    opens += self.escape_reasons(e)
                if e.defines_new:
                    opens.append(f"{e.qualname} defines __new__ (the constructor may return another type)")
            else:
                opens += e.open_class
            if i > 0 and e.init_subclass:
                opens.append(f"base {e.qualname} defines __init_subclass__ (it may add names to subclasses)")
        if not instance:
            tinfo = self.env.oracle().ask("object", name="builtins.type")
            for n in tinfo.get("names", []) if tinfo.get("ok") else []:
                names.setdefault(n, Member(n, "attribute", None, None, "type"))
            if not tinfo.get("ok"):
                opens.append("the names of `type` could not be read")
        what = "instance of class" if instance else "class"
        src = self.source_of(facts.path)
        return Container("instance" if instance else "class", f"{what} {full}", names, not opens,
                         "; ".join(dict.fromkeys(opens)) or None, src, f"{self.disp(facts.path)}:{facts.node.lineno}",
                         files=files, full=full, mro=mro)

    def std_class_container(self, full: str, instance: bool) -> Container:
        info = self.env.oracle().ask("object", name=full)
        what = "instance of class" if instance else "class"
        kind = "instance" if instance else "class"
        if not info.get("ok") or info.get("kind") != "class":
            return Container(kind, f"{what} {full}", {}, False, f"the standard-library oracle could not load it "
                             f"({info.get('error') or info.get('kind')})", "stdlib", None, full=full)
        e = StdClass(full, info)
        names = {n: Member(n, "attribute", None, None, full) for n in info.get("names", [])}
        opens: list[str] = []
        if instance:
            opens += self._std_instance_open(e, not info.get("inst_dict"))
            for n, m in self._std_instance_names(e).items():
                names.setdefault(n, m)
        else:
            for n in info.get("meta_names", []):
                names.setdefault(n, Member(n, "attribute", None, None, "type"))
            if info.get("meta_getattr") or info.get("custom_meta"):
                opens.append(f"the metaclass of {full} may add names")
        return Container(kind, f"{what} {full}", names, not opens, "; ".join(opens) or None, "stdlib",
                         f"<stdlib>:{full}", full=full, mro=[e])

    def _std_instance_open(self, e: StdClass, no_dict: bool = False) -> list[str]:
        info = e.info
        out = []
        if info.get("inst_getattr"):
            out.append(f"{e.full} defines __getattr__/__getattribute__")
        if no_dict:
            return out   # slotted instances: nothing can add attributes
        for entry in info.get("mro", []):
            mod, qual, src, has_dict = (list(entry) + [None] * 4)[:4]
            if has_dict and not src and qual not in ("object",):
                out.append(f"instances of {mod}.{qual} keep a __dict__ that C code may fill")
            elif src:
                facts = cf.class_by_qualname(Path(src), qual)
                if facts is not None:
                    out += facts.open_inst + facts.open_dict
                    out += self.escape_reasons(facts)
                elif has_dict:
                    out.append(f"the source of {mod}.{qual} was not read")
        return out

    def _std_instance_names(self, e: StdClass) -> dict[str, Member]:
        out: dict[str, Member] = {}
        for entry in e.info.get("mro", []):
            _mod, qual, src, _has = (list(entry) + [None] * 4)[:4]
            if src:
                facts = cf.class_by_qualname(Path(src), qual)
                if facts is not None:
                    for n, m in facts.inst.items():
                        out.setdefault(n, m)
        return out

    def _mro(self, facts: cf.ClassFacts, depth: int = 0) -> tuple[list, list[str]]:
        """C3 linearisation of the resolved bases (ClassFacts / StdClass entries) and why it is open."""
        if depth > 25:
            return [facts], ["the class hierarchy is too deep to follow"]
        opens: list[str] = []
        seqs: list[list] = []
        bases: list = []
        script = None
        for b in facts.node.bases:
            expr = b.value if isinstance(b, ast.Subscript) else b
            text = dotted(expr)
            if text is None:
                opens.append(f"base `{ast.unparse(b)}` of {facts.qualname} is an expression")
                continue
            if text in ("object", "builtins.object"):
                continue
            if script is None:
                script = self.script_for(facts.path)
            if script is None:
                opens.append(f"{facts.path.name} cannot be read")
                break
            tok = expr if isinstance(expr, ast.Name) else expr
            if isinstance(tok, ast.Name):
                line, bcol = tok.lineno, tok.col_offset
            else:
                line, bcol = tok.end_lineno or tok.lineno, (tok.end_col_offset or 0) - len(tok.attr)  # type: ignore
            lines = cf.parse_file(facts.path)[1]
            col = _char_col(lines[line - 1] if 0 < line <= len(lines) else "", bcol)
            defs = [d for d in self.goto(script, line, col) if d.type == "class"]
            if len(defs) != 1:
                opens.append(f"base {text} of {facts.qualname} does not resolve to one class")
                continue
            d = defs[0]
            fn = d.full_name or text
            if fn in _SPECIAL_BASES:
                continue
            if fn in _OPEN_BASES:
                opens.append(f"base {fn} builds the class dynamically")
                continue
            p = Path(d.module_path) if d.module_path else None
            if p is None or self.origin(p) == "stdlib":
                info = self.env.oracle().ask("object", name=fn)
                if not info.get("ok") or info.get("kind") != "class":
                    opens.append(f"base {fn} could not be read ({info.get('error') or info.get('kind')})")
                    continue
                std = StdClass(fn, info)
                bases.append(std)
                seqs.append([std])
                continue
            if cenv.is_jedi_bundled_stub(p):
                opens.append(f"base {fn} is described only by a stub bundled with jedi")
                continue
            bf, err = cf.class_at(p, d.line)
            if bf is None:
                opens.append(f"base {fn}: {err}")
                continue
            sub, why = self._mro(bf, depth + 1)
            opens += why
            bases.append(bf)
            seqs.append(sub)
        merged = _c3([[facts], *seqs, bases])
        if merged is None:
            return [facts, *bases], opens + [f"the bases of {facts.qualname} have no consistent MRO"]
        return merged, opens

    def escape_reasons(self, facts: cf.ClassFacts) -> list[str]:
        """Why ``self`` handed to other code (``facts.escapes``) may give instances more attributes."""
        key = ("escapes", str(facts.path), facts.node.lineno, cf.stat_key(facts.path))
        hit = self._cls_memo.get(key)
        if hit is None:
            hit = []
            script = self.script_for(facts.path) if facts.escapes else None
            for call, ref, fname in facts.escapes:
                why = self.callee_mutates(script, facts.path, call, ref, fname, 0) if script is not None else \
                    f"passes self to {fname} (line {call.lineno})"
                if why:
                    hit.append(f"{facts.qualname} {why}")
            self._cls_memo[key] = hit
        return hit

    def callee_mutates(self, script, path: Path, call: ast.Call, ref: tuple, fname: str, depth: int) -> str | None:
        """None when the callee provably does not set attributes on the object passed at ``ref``."""
        label = f"{fname} (line {call.lineno})"
        if depth > 2:
            return f"passes it on to {label} and further"
        if ref[0] == "star":
            return f"passes it through *args/**kwargs to {label}"
        f = call.func
        if isinstance(f, ast.Name):
            line, bcol = f.lineno, f.col_offset
        elif isinstance(f, ast.Attribute):
            line, bcol = f.end_lineno or f.lineno, (f.end_col_offset or 0) - len(f.attr.encode())
        else:
            return f"passes it to {label}"
        lines = cf.parse_file(path)[1] if path.is_file() else []
        col = _char_col(lines[line - 1] if 0 < line <= len(lines) else "", bcol)
        defs = [d for d in self.goto(script, line, col) if d.type in ("function", "class")]

        def c_function(x) -> bool:
            xp = Path(x.module_path) if x.module_path else None
            return (xp is None or xp.suffix == ".pyi") and (xp is None or self.origin(xp) == "stdlib") and \
                (x.name or "") not in ("setattr", "delattr", "__setattr__", "__delattr__", "update_wrapper")
        if defs and all(c_function(x) for x in defs):
            return None   # a C function of the standard library (only a stub describes it) sets no attributes
        if len(defs) != 1:
            return f"passes it to {label}, which does not resolve to one function"
        d = defs[0]
        p = Path(d.module_path) if d.module_path else None
        if p is None or cenv.is_jedi_bundled_stub(p) or not p.is_file():
            return f"passes it to {label} (no source to read)"
        if d.type == "class":
            facts, _err = cf.class_at(p, d.line)
            init = facts.body.get("__init__") if facts else None
            fn = cf.function_at(p, init.line) if init and init.line else None
            if fn is None:
                return f"passes it to {label}"
            drop = True
        else:
            fn = cf.function_at(p, d.line)
            if fn is None:
                return f"passes it to {label}"
            kind = cf.method_kind(fn) if _in_class(p, fn) else "function"
            drop = kind == "classmethod"
            if kind == "method":
                drop = True
                if isinstance(f, ast.Attribute):
                    rdefs = self.goto(script, *_attr_base_pos(f, lines))
                    drop = not (len(rdefs) == 1 and rdefs[0].type == "class")
        a = fn.args  # type: ignore[attr-defined]
        pos = [x.arg for x in [*a.posonlyargs, *a.args]]
        if drop:
            pos = pos[1:]
        if ref[0] == "pos":
            i = int(ref[1])  # type: ignore[arg-type]
            if i >= len(pos):
                return f"passes it to *{a.vararg.arg if a.vararg else 'args'} of {label}"
            param = pos[i]
        else:
            param = str(ref[1])
            if param not in pos and param not in [x.arg for x in a.kwonlyargs]:
                return f"passes it to **kwargs of {label}"
        whys, passed = cf.param_writes(fn, param)
        if whys:
            return f"passes it to {label}: {whys[0]}"
        if passed:
            sub = self.script_for(p)
            if sub is None:
                return f"passes it to {label}, which passes it on"
            for c2, r2, n2 in passed:
                w = self.callee_mutates(sub, p, c2, r2, n2, depth + 1)
                if w:
                    return f"passes it to {label}, which {w}"
        return None

    # -- receivers ------------------------------------------------------------------------------------
    def receiver(self, fx: FileCtx, base: ast.AST) -> Container | str:
        if isinstance(base, ast.Name):
            defs = fx.goto(*fx.pos(base.lineno, base.col_offset))
            return self._from_defs(fx, defs, base)
        if isinstance(base, ast.Attribute):
            if _dunder(base.attr):
                return f"the receiver `{ast.unparse(base)}` is a special attribute"
            line = base.end_lineno or base.lineno
            defs = fx.goto(*fx.pos(line, (base.end_col_offset or 0) - len(base.attr.encode())))
            return self._from_defs(fx, defs, None)
        if isinstance(base, ast.Call):
            f = base.func
            text = dotted(f)
            if text is None:
                return "the receiver is the result of a call to an expression"
            if text in ("super", "type", "builtins.type"):
                return f"`{text}()` does not say which class answers"
            defs = self._callee_defs(fx, f)
            if len(defs) == 1 and defs[0].type == "class":
                if (defs[0].full_name or "") in ("builtins.type", "builtins.super"):
                    return f"`{text}()` does not say which class answers"
                return self.class_container(defs[0], True, fx)
            if len(defs) == 1 and defs[0].type == "function":
                return f"the receiver is the return value of {text}(): a subclass or another type may be returned"
            return f"the receiver is the result of {text}(), whose type is not known"
        lit = _literal_type(base)
        if lit:
            return self.std_class_container(lit, True)
        return f"the receiver is an expression ({type(base).__name__}); its type is not known"

    def module_values(self, defs: list) -> list:
        """The modules that module-typed jedi names denote (``os.path`` goes to the ``path`` name in os.py,
        which is the module ntpath/posixpath)."""
        out: dict = {}
        for d in defs:
            try:
                vals = [v for v in d.infer() if v.type == "module"]
            except Exception:  # noqa: BLE001
                vals = []
            for v in vals or []:
                out.setdefault(str(v.module_path) if v.module_path else v.full_name, v)
        return list(out.values())

    def _callee_defs(self, fx: FileCtx, f: ast.AST) -> list:
        if isinstance(f, ast.Name):
            return fx.goto(*fx.pos(f.lineno, f.col_offset))
        if isinstance(f, ast.Attribute):
            return fx.goto(*fx.pos(f.end_lineno or f.lineno, (f.end_col_offset or 0) - len(f.attr.encode())))
        return []

    def _from_defs(self, fx: FileCtx, defs: list, name: ast.Name | None) -> Container | str:
        label = f"`{name.id}`" if name is not None else "the receiver"
        if not defs:
            if name is not None:
                return f"jedi found no definition of {label}"
            return "jedi found no definition of the receiver"
        types = {d.type for d in defs}
        if types == {"module"}:
            defs = self.module_values(defs)
            if not defs:
                return f"{label} names a module jedi cannot read"
            cs = [self.module_container(d.module_name or d.full_name or d.name, d.module_path, fx) for d in defs]
            if len(cs) == 1:
                return cs[0]
            names: dict[str, Member] = {}
            for c in cs:
                for k, m in c.names.items():
                    names.setdefault(k, m)
            whys = [c.why for c in cs if c.why]
            return Container("module", f"module {defs[0].full_name} ({', '.join(c.full or '?' for c in cs)})", names,
                             all(c.closed for c in cs), "; ".join(whys) or None, cs[0].source, cs[0].where,
                             files=[f for c in cs for f in c.files], full=defs[0].full_name)
        if len(defs) == 1 and defs[0].type == "class":
            return self.class_container(defs[0], False, fx)
        if name is not None and len(defs) == 1 and defs[0].type == "statement":
            st, why = fx.single_local(name, "instance")
            if st is None:
                return why
            cdefs = self._callee_defs(fx, st.value.func)
            if len(cdefs) == 1 and cdefs[0].type == "class" and \
                    (cdefs[0].full_name or "") not in ("builtins.type", "builtins.super"):
                return self.class_container(cdefs[0], True, fx)
            return f"{label} holds the result of {dotted(st.value.func) or 'a call'}() (line {st.lineno}), " \
                   "not of a direct constructor call"
        if "param" in types:
            return f"{label} is a parameter: its runtime type is not known (an annotation does not rule out a " \
                   "subclass that has the name)"
        if len(defs) > 1:
            return f"{label} has {len(defs)} possible definitions"
        return f"{label} is a {defs[0].type}: its runtime type is not known"

    # -- signatures -----------------------------------------------------------------------------------
    def callee_sig(self, fx: FileCtx, call: ast.Call) -> tuple[Sig | None, str, str]:
        """(signature, why unknown, callee label) of a call's callee."""
        f = call.func
        label = dotted(f) or _short(f, 40)
        if isinstance(f, ast.Name):
            defs = fx.goto(*fx.pos(f.lineno, f.col_offset))
            return self._sig_from_defs(fx, defs, label, None)
        if not isinstance(f, ast.Attribute):
            return None, "the callee is an expression", label
        rec = self.receiver(fx, f.value)
        if isinstance(rec, str):
            return None, rec + ": an override may take other keywords", label
        if rec.kind == "module":
            defs = self._callee_defs(fx, f)
            return self._sig_from_defs(fx, defs, label, rec)
        return self._method_sig(rec, f.attr, label)

    def _sig_from_defs(self, fx: FileCtx, defs: list, label: str, rec: Container | None) -> tuple[Sig | None, str, str]:
        if len(defs) != 1:
            return None, ("jedi found no definition of the callee" if not defs else
                          f"the callee has {len(defs)} possible definitions"), label
        d = defs[0]
        if d.type == "class":
            c = self.class_container(d, True, fx)
            return (*self._ctor_sig(c), label)
        if d.type != "function":
            return None, f"the callee is a {d.type}, not a known function", label
        path = Path(d.module_path) if d.module_path else None
        if path is None or self.origin(path) == "stdlib":
            return (*self._std_sig(d.full_name or label, drop_first=False), label)
        if cenv.is_jedi_bundled_stub(path):
            return None, "only a stub bundled with jedi describes the callee", label
        fn = cf.function_at(path, d.line)
        self._dep(fx, path)
        if fn is None:
            return None, f"no function definition at {self.disp(path)}:{d.line}", label
        if _in_class(path, fn):
            return None, "the callee is a method reached through a name, not through its class", label
        s, why = self._fn_sig(path, fn, False, "the callee")
        return s, why, label

    def _fn_sig(self, path: Path, fn: ast.AST, drop_first: bool, label: str) -> tuple[Sig | None, str]:
        """The keyword arguments of a source function (overloads merged; unknown under other decorators)."""
        group = [fn]
        if "overload" in {d.rsplit(".", 1)[-1] for d in cf._func_decorators(fn)}:
            tree = cf.module_facts(path).tree
            group = cf.overload_group(tree, fn) if tree is not None else [fn]
        sigs = []
        for f in group:
            bad = cf.unsafe_decorators(f)
            if bad:
                return None, f"{label} is decorated with @{bad[0]}: the wrapper may take other arguments"
            sigs.append(cf.signature_of(f, drop_first=drop_first))
        s = sigs[0] if len(sigs) == 1 else cf.merge_sigs(sigs)
        s.at = f"{self.disp(path)}:{group[0].lineno}"
        return s, ""

    def _std_sig(self, full: str, drop_first: bool) -> tuple[Sig | None, str]:
        info = self.env.oracle().ask("object", name=full)
        if not info.get("ok"):
            return None, f"the standard-library oracle could not load {full} ({info.get('error')})"
        if "params" not in info:
            return None, f"no signature for {full} ({info.get('sig_error', 'not callable')})"
        params = info["params"]
        if drop_first and params and params[0][1] in ("POSITIONAL_ONLY", "POSITIONAL_OR_KEYWORD"):
            params = params[1:]
        names = [p for p, k in params if k in ("POSITIONAL_OR_KEYWORD", "KEYWORD_ONLY")]
        posonly = [p for p, k in params if k == "POSITIONAL_ONLY"]
        varkw = any(k == "VAR_KEYWORD" for _p, k in params)
        return Sig(names, posonly, varkw, f"{full.rsplit('.', 1)[-1]}{info.get('signature', '(...)')}",
                   f"<stdlib>:{full}"), ""

    def _method_sig(self, rec: Container, meth: str, label: str) -> tuple[Sig | None, str, str]:
        if not rec.closed:
            return None, f"{rec.label} is not closed ({rec.why})", label
        for e in rec.mro:
            if isinstance(e, StdClass):
                if meth not in e.info.get("names", []):
                    continue
                info = self.env.oracle().ask("object", name=f"{e.full}.{meth}")
                static = info.get("static")
                cm = info.get("classmethod")
                drop = rec.kind == "instance" and not static and not cm
                s, why = self._std_sig(f"{e.full}.{meth}", drop_first=drop)
                return s, why, label
            m = e.body.get(meth)
            if m is None:
                continue
            fn = cf.function_at(e.path, m.line) if m.line else None
            if fn is None or m.kind not in ("method", "classmethod", "staticmethod"):
                return None, f"{e.qualname}.{meth} is a {m.kind}, not a plain method", label
            drop = m.kind == "classmethod" or (m.kind == "method" and rec.kind == "instance")
            s, why = self._fn_sig(e.path, fn, drop, f"{e.qualname}.{meth}")
            return s, why, label
        return None, f"{meth} is not found in {rec.label}", label

    def _ctor_sig(self, c: Container) -> tuple[Sig | None, str]:
        if not c.closed:
            return None, f"{c.label} is not closed ({c.why})"
        for e in c.mro:
            if isinstance(e, StdClass):
                if e.full in ("builtins.object", "object"):
                    return Sig([], [], False, "object()", None), ""
                return self._std_sig(e.full, drop_first=False)
            if "__new__" in e.body:
                return None, f"{e.qualname} defines __new__"
            if "__init__" in e.body:
                m = e.body["__init__"]
                fn = cf.function_at(e.path, m.line) if m.line else None
                if fn is None:
                    return None, f"{e.qualname}.__init__ is not a plain method"
                return self._fn_sig(e.path, fn, True, f"{e.qualname}.__init__")
            if e.dataclass:
                return self._dataclass_sig(c)
        return Sig([], [], False, "object()", None), ""

    def _dataclass_sig(self, c: Container) -> tuple[Sig | None, str]:
        fields: list[str] = []
        for e in reversed(c.mro):
            if isinstance(e, StdClass):
                if e.full not in ("builtins.object", "object"):
                    return None, f"base {e.full} of a dataclass"
                continue
            if not e.dataclass:
                continue
            for d in e.node.decorator_list:
                if isinstance(d, ast.Call) and any(k.arg == "init" for k in d.keywords):
                    return None, "the dataclass sets init="
            for st in e.node.body:
                if isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name):
                    ann = ast.unparse(st.annotation)
                    if "ClassVar" in ann or "KW_ONLY" in ann:
                        continue
                    if isinstance(st.value, ast.Call) and (dotted(st.value.func) or "").endswith("field") and \
                            any(k.arg == "init" for k in st.value.keywords):
                        continue
                    if st.target.id not in fields:
                        fields.append(st.target.id)
        return Sig(fields, [], False, f"{c.full}({', '.join(fields)})", c.where), ""

    # -- sites --------------------------------------------------------------------------------------------
    def eval_import(self, fx: FileCtx, node: ast.Import, alias: ast.alias) -> dict:
        name = alias.name
        site = self._site(fx, alias.lineno, alias.col_offset, "import", name, name.rsplit(".", 1)[-1])
        # jedi on the last component
        line = alias.lineno
        text = fx.lines[line - 1] if 0 < line <= len(fx.lines) else ""
        bcol = alias.col_offset + len(name.encode()) - 1
        defs = [d for d in fx.goto(line, _char_col(text, bcol)) if d.type == "module"]
        if defs:
            return self._exists(site, defs[0], fx)
        return self._module_missing(fx, node, name, site)

    def _module_missing(self, fx: FileCtx, node: ast.AST, dotted_name: str, site: dict,
                        base_dir: Path | None = None) -> dict:
        parts = dotted_name.split(".")
        u = self.universe(fx.path.parent)
        found_prefix: list[cenv.ModSpec] = []
        miss_at = 0
        for i in range(len(parts)):
            specs = _find_in(base_dir, parts[: i + 1]) if base_dir is not None else u.find(".".join(parts[: i + 1]))
            if not specs:
                miss_at = i
                break
            found_prefix = specs
        else:
            spec = found_prefix[0]
            site.update(source=self.source_of(spec.file) if spec.file else "stdlib",
                        at_def=self.disp(spec.file or (spec.dirs[0] if spec.dirs else None)))
            return self._verdict(site, "exists", note=f"found as a {spec.kind} module; jedi does not read it")
        guard = self._guard(fx, node, "import")
        missing = ".".join(parts[: miss_at + 1])
        if miss_at == 0 and base_dir is None:
            top = parts[0]
            if not self.env.third_party:
                v = self._verdict(site, "not_installed", container=f"the standard library of {self.env.label}",
                                  why=f"module {top} is not in the standard library, and "
                                      f"{self.env.note or 'there is no project environment'}",
                                  next_step="create the project's virtual environment (.venv) or pass --env PATH")
                return self._guarded(v, guard)
            decl = self.declared(top)
            if decl:
                v = self._verdict(site, "not_installed", container=self.env.label,
                                  why=f"{top} is declared ({decl}) but not installed in {self.env.label}",
                                  next_step=f"install the project's dependencies into {self.env.label.split(' ')[0]}")
                return self._guarded(v, guard)
            if u.hooks:
                return self._verdict(site, "unknown", why=f"site-packages runs import hooks ({u.hooks[0]}); "
                                                          "modules may come from elsewhere")
            if fx.facts.sys_path_edit:
                return self._verdict(site, "unknown", why="this file changes sys.path at runtime")
            near = nearest(top, {n: Member(n, "module") for n in u.top_level_names()})
            v = self._verdict(site, "absent", container=f"sys.path of {self.env.label}",
                              message=f"module {top} not found in this project or in {self.env.label}",
                              nearest=near, source="project")
            return self._guarded(v, guard)
        parent = ".".join(parts[:miss_at])
        pspecs = found_prefix
        pfile = next((s.file for s in pspecs if s.file), None)
        cont = self.module_container(parent, pfile, fx) if pfile or not pspecs else None
        if cont is not None and parts[miss_at] in cont.names:
            return self._verdict(site, "unknown", why=f"{missing} is a name in module {parent}, not a module file; "
                                                      "`import` of it works only if the module registers it")
        if any(s.kind in ("namespace",) for s in pspecs) or cont is None or not cont.closed:
            return self._verdict(site, "unknown", why=f"package {parent} is not closed "
                                                      f"({cont.why if cont else 'namespace package'})")
        subs = {n: m for n, m in cont.names.items() if m.kind == "module"}
        v = self._verdict(site, "absent", container=f"package {parent}", where=cont.where,
                          message=f"no module {parts[miss_at]} in package {parent} {self.in_env_phrase(cont)} "
                                  f"({cont.where})",
                          nearest=nearest(parts[miss_at], subs or cont.names), source=cont.source)
        return self._guarded(v, guard)

    def eval_from(self, fx: FileCtx, node: ast.ImportFrom) -> list[dict]:
        out: list[dict] = []
        level = node.level or 0
        mod = node.module or ""
        expr = "." * level + mod
        line = node.lineno
        text = fx.lines[line - 1] if 0 < line <= len(fx.lines) else ""
        # the module token: find "from <expr>" on the statement's first line
        m = re.search(r"\bfrom\s+(\.*)\s*([\w.]*)", text[node.col_offset:] if len(text) > node.col_offset else "")
        mod_site = self._site(fx, line, node.col_offset + (m.start(1) if m else 0), "import", expr or ".",
                              (mod or ".").rsplit(".", 1)[-1])
        base_dir = None
        if level:
            base_dir = fx.path.parent
            for _ in range(level - 1):
                base_dir = base_dir.parent
        mdefs = []
        if m and mod:
            end = node.col_offset + m.end(2)
            found = fx.goto(line, _char_col(text, max(end - 1, 0)))
            mdefs = self.module_values([d for d in found if d.type == "module"])
        if mod and not mdefs:
            v = self._module_missing(fx, node, mod, mod_site, base_dir=base_dir)
            out.append(v)
            if v["verdict"] != "exists":
                return out
            specs = _find_in(base_dir, mod.split(".")) if base_dir is not None else \
                self.universe(fx.path.parent).find(mod)
            spec = specs[0] if specs else None
            container = self.module_container(expr, spec.file if spec else None, fx) if spec and spec.file else None
            if container is None:
                for al in node.names:
                    if al.name != "*":
                        s = self._site(fx, al.lineno, al.col_offset, "import", f"{expr}.{al.name}", al.name)
                        out.append(self._verdict(s, "unknown", why=f"module {expr} is found but not read by jedi"))
                return out
        elif not mod:
            container = self.module_container(expr, base_dir / "__init__.py" if (base_dir / "__init__.py").is_file()
                                              else None, fx) if base_dir is not None else None
            if container is not None:
                out.append(self._verdict(mod_site, "exists", at_def=container.where, source=container.source))
        else:
            out.append(self._exists(mod_site, mdefs[0], fx))
            container = self.module_container(mdefs[0].module_name or mod, mdefs[0].module_path, fx) \
                if len(mdefs) == 1 else None
        for al in node.names:
            if al.name == "*":
                continue
            s = self._site(fx, al.lineno, al.col_offset, "import", f"{expr}.{al.name}", al.name)
            defs = fx.goto(*fx.pos(al.lineno, al.col_offset))
            if defs:
                out.append(self._exists(s, defs[0], fx))
                continue
            if container is None:
                out.append(self._verdict(s, "unknown", why=f"module {expr} resolves to several files"))
                continue
            m = container.names.get(al.name)
            if m is not None:
                out.append(self._verdict(s, "exists", source=container.source, container=container.label,
                                         at_def=self._member_at(m, container)))
                continue
            out.append(self._judge(fx, node, s, container, al.name))
        return out

    def eval_attribute(self, fx: FileCtx, node: ast.Attribute) -> dict:
        line = node.end_lineno or node.lineno
        bcol = (node.end_col_offset or 0) - len(node.attr.encode())
        site = self._site(fx, line, bcol, "attribute", _short(node), node.attr)
        rec = self.receiver(fx, node.value)
        if isinstance(rec, Container):
            m = rec.names.get(node.attr)
            if m is not None:
                return self._verdict(site, "exists", source=rec.source, container=rec.label,
                                     at_def=self._member_at(m, rec))
            defs = fx.goto(*fx.pos(line, bcol))
            if defs:
                return self._exists(site, defs[0], fx)
            return self._judge(fx, node, site, rec, node.attr)
        defs = fx.goto(*fx.pos(line, bcol))
        if defs:
            return self._exists(site, defs[0], fx)
        return self._verdict(site, "unknown", why=rec,
                             next_step="read the receiver's type definition, or run the tests that reach this line")

    def eval_kwargs(self, fx: FileCtx, call: ast.Call) -> list[dict]:
        kws = [k for k in call.keywords if k.arg]
        sig, why, label = self.callee_sig(fx, call)
        out = []
        for k in kws:
            site = self._site(fx, k.lineno, k.col_offset, "kwarg", f"{label}({k.arg}=)", k.arg)
            if sig is None:
                out.append(self._verdict(site, "unknown", why=why,
                                         next_step="read the callee's definition or documentation"))
            elif k.arg in sig.names:
                out.append(self._verdict(site, "exists", signature=sig.text, at_def=sig.at))
            elif sig.varkw:
                out.append(self._verdict(site, "unknown", why=f"the callee takes **kwargs: {sig.text}",
                                         signature=sig.text, at_def=sig.at,
                                         next_step="read where the callee passes **kwargs on"))
            else:
                extra = " (it is positional-only)" if k.arg in sig.posonly else ""
                v = self._verdict(site, "absent", container=f"the signature {sig.text}", where=sig.at,
                                  message=f"keyword {k.arg}= not found in the signature {sig.text}{extra} "
                                          f"({sig.at or 'standard library'})",
                                  nearest=nearest(k.arg, {n: Member(n, "parameter") for n in sig.names}),
                                  signature=sig.text)
                out.append(self._guarded(v, self._guard(fx, call, "kwarg")))
        return out

    def eval_dict_key(self, fx: FileCtx, sub: ast.Subscript) -> dict | None:
        key = sub.slice
        if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
            return None
        v = sub.value
        why_local = ""
        if isinstance(v, ast.Call):
            call = v
        elif isinstance(v, ast.Name):
            st, why_local = fx.single_local(v, "dict")
            if st is None:
                if "not assigned from a call" in why_local or "bound" in why_local or "parameter" in why_local \
                        or "module-" in why_local or "comprehension" in why_local:
                    return None
                call = self._local_call(fx, v)
                if call is None:
                    return None
            else:
                call = st.value
        else:
            return None
        if not isinstance(call.func, (ast.Name, ast.Attribute)):
            return None
        defs = self._callee_defs(fx, call.func)
        if len(defs) != 1 or defs[0].type != "function" or not defs[0].module_path:
            return None
        path = Path(defs[0].module_path)
        if self.origin(path) == "stdlib" or cenv.is_jedi_bundled_stub(path):
            return None
        fn = cf.function_at(path, defs[0].line)
        if fn is None or cf.unsafe_decorators(fn):
            return None
        keys, why = cf.dict_return_keys(fn)
        if why:
            return None
        self._dep(fx, path)
        fname = dotted(call.func) or "the function"
        site = self._site(fx, key.lineno, key.col_offset, "dict_key", f"{_short(sub)}", key.value)
        where = f"{self.disp(path)}:{fn.lineno}"
        if key.value in keys:
            return self._verdict(site, "exists", source=self.source_of(path),
                                 at_def=f"{self.disp(path)}:{keys[key.value]}")
        if why_local:
            return self._verdict(site, "unknown", why=why_local, container=f"dict returned by {fname}()")
        v = self._verdict(site, "absent", container=f"the dict literal(s) returned by {fname}()", where=where,
                          message=f"key {key.value!r} not found in the dict literal(s) returned by {fname}() "
                                  f"({where})",
                          nearest=nearest(key.value, {k: Member(k, "key", ln, path) for k, ln in keys.items()},
                                          disp=self.disp),
                          source=self.source_of(path))
        return self._guarded(v, self._guard(fx, sub, "dict_key"))

    def _local_call(self, fx: FileCtx, name: ast.Name) -> ast.Call | None:
        fn = fx.scope_of(name)
        for x in ast.walk(fn):
            if isinstance(x, ast.Assign) and len(x.targets) == 1 and isinstance(x.targets[0], ast.Name) and \
                    x.targets[0].id == name.id and isinstance(x.value, ast.Call):
                return x.value
        return None

    # -- verdict helpers ----------------------------------------------------------------------------------
    def _site(self, fx: FileCtx, line: int, byte_col: int, kind: str, expr: str, name: str) -> dict:
        col = fx.pos(line, byte_col)[1]
        return {"at": f"{fx.rel}:{line}:{col + 1}", "path": fx.rel, "line": line, "col": col + 1, "kind": kind,
                "expr": expr, "name": name}

    def _verdict(self, site: dict, verdict: str, **kw) -> dict:
        site["verdict"] = verdict
        for k, v in kw.items():
            if v not in (None, "", [], {}):
                site[k] = v
        return site

    def _exists(self, site: dict, d, fx: FileCtx | None) -> dict:
        path = d.module_path
        self._dep(fx, path)
        at = f"{self.disp(path)}:{d.line}" if path and d.line else self.disp(path)
        return self._verdict(site, "exists", source=self.source_of(path), at_def=at or f"<builtin> {d.full_name}")

    def _member_at(self, m: Member, c: Container) -> str | None:
        if m.file and m.line:
            return f"{self.disp(m.file)}:{m.line}"
        if m.owner and c.source == "stdlib":
            return f"<stdlib>:{m.owner}.{m.name}"
        return c.where

    def _judge(self, fx: FileCtx, node: ast.AST, site: dict, c: Container, name: str) -> dict:
        """``name`` is not in ``c`` and jedi did not find it: absent if ``c`` is closed, else unknown."""
        if not c.closed:
            return self._verdict(site, "unknown", why=f"{c.label} is not closed: {c.why}", container=c.label,
                                 where=c.where, source=c.source,
                                 next_step="read the container's source or run the tests that reach this line")
        kind = "attribute" if site["kind"] == "attribute" else "import"
        near = nearest(name, c.names, want_call=_is_called(fx, node) if isinstance(node, ast.Attribute) else None,
                       disp=self.disp)
        v = self._verdict(site, "absent", container=c.label, where=c.where, source=c.source,
                          message=f"not found in {c.label} {self.in_env_phrase(c)} ({c.where})", nearest=near,
                          elsewhere=self.elsewhere(name, c), next_step=self._next_absent(near, c))
        return self._guarded(v, self._guard(fx, node, kind, name))

    def _next_absent(self, near: list[dict], c: Container) -> str:
        hint = f"use a real name (nearest: {', '.join(n['name'] for n in near)})" if near else "use a real name"
        return f"{hint}; `verinoda api {c.full}` lists the real names" if c.full else hint

    def _guarded(self, v: dict, guard: str | None) -> dict:
        if guard and v["verdict"] in ("absent", "not_installed"):
            v["verdict_unguarded"] = v["verdict"]
            v["verdict"] = "guarded"
            v["guard"] = guard
            v.pop("next_step", None)
        return v

    def _guard(self, fx: FileCtx, node: ast.AST, kind: str, name: str | None = None) -> str | None:
        cur = node
        in_function = False
        while True:
            p = fx.parents.get(id(cur))
            if p is None:
                return None
            if isinstance(p, (ast.If, ast.IfExp, ast.While)) and cur is not p.test:
                t = ast.unparse(p.test)
                if _GUARD_TEST.search(t):
                    return f"under `if {t[:70]}` (line {p.lineno})"
                if name and _hasattr_test(p.test, name):
                    return f"under `if {t[:70]}` (line {p.lineno})"
            elif isinstance(p, ast.BoolOp) and isinstance(p.op, ast.And) and name:
                idx = next((i for i, v in enumerate(p.values) if v is cur), 0)
                if any(_hasattr_test(v, name) for v in p.values[:idx]):
                    return f"after a hasattr() test (line {p.lineno})"
            elif isinstance(p, ast.ExceptHandler) and kind == "import" and not in_function:
                caught = _caught(p.type)
                tr = fx.parents.get(id(p))
                if (caught is None or caught & {"ImportError", "ModuleNotFoundError"}) and tr is not None and \
                        any(isinstance(x, (ast.Import, ast.ImportFrom)) for s in tr.body for x in ast.walk(s)):
                    return f"a fallback import in `except ImportError` (line {p.lineno})"
            elif cf._TRY and isinstance(p, cf._TRY) and not in_function and any(cur is s for s in p.body):
                for h in p.handlers:
                    caught = _caught(h.type)
                    if caught is None or caught & (_CATCH.get(kind, set()) | {"Exception", "BaseException"}):
                        return f"inside try/except {', '.join(sorted(caught)) if caught else ''} (line {p.lineno})" \
                            .replace("except  ", "except ")
            elif isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                in_function = True
            cur = p

    # -- nearest / elsewhere ------------------------------------------------------------------------------
    def elsewhere(self, name: str, c: Container) -> list[dict]:
        if c.source == "project" or c.source == "external":
            idx = self._project_defs()
        elif c.files:
            idx = self._package_defs(c.files[0])
        else:
            return []
        own = {str(f) for f in c.files}
        out = []
        for path, line, kind, qual in idx.get(name, []):
            if str(path) in own:
                continue
            out.append({"at": f"{self.disp(path)}:{line}", "kind": kind, "qualname": qual})
            if len(out) >= 5:
                break
        return out

    def _project_defs(self) -> dict:
        if self._defs_index is None:
            self._defs_index = _defs_index(iter_py_files(self.repo, [self.repo]))
        return self._defs_index

    def _package_defs(self, f: Path) -> dict:
        top = f.parent
        for site in self.env.site_dirs + self.env.stdlib_dirs:
            try:
                rel = f.relative_to(site)
            except ValueError:
                continue
            top = site / rel.parts[0] if len(rel.parts) > 1 else f.parent
            break
        key = str(top)
        if key not in self._pkg_index:
            files = [top] if top.is_file() else [p for p in sorted(top.rglob("*.py"))[:400]] if top.is_dir() else []
            self._pkg_index[key] = _defs_index(files)
        return self._pkg_index[key]

    # -- declared dependencies -----------------------------------------------------------------------------
    def declared(self, module: str) -> str | None:
        """Where the project declares a package that provides ``module`` (lock, manifest), or None.

        A package named differently from its module (PyYAML/yaml, python-docx/docx) is matched when its
        name contains the module's name; that match is labelled "probably"."""
        if self._declared is None:
            try:
                from verinoda.references.local import local_versions

                lv = local_versions(self.repo)
                self._declared = {k.rsplit("/", 1)[-1]: items[0].get("locator") for k, items in
                                  (lv.get("packages") or {}).items() if k.startswith("pkg:pypi/") and items}
            except Exception:  # noqa: BLE001 - version files are optional
                self._declared = {}
        key = cenv._norm_dist(module)
        if key in self._declared:
            return f"{self._declared[key]}"
        if len(key) >= 3:
            for name, loc in sorted(self._declared.items()):
                if key in name.split("-") or name.replace("-", "") in (f"py{key}", f"python{key}") or \
                        name.startswith(key):
                    return f"probably as {name}, {loc}"
        return None


def _attr_base_pos(f: ast.Attribute, lines: list[str]) -> tuple[int, int]:
    """(line, char col) of the last name token of an attribute's receiver."""
    b = f.value
    if isinstance(b, ast.Attribute):
        line, bcol = b.end_lineno or b.lineno, (b.end_col_offset or 0) - len(b.attr.encode())
    else:
        line, bcol = b.lineno, b.col_offset
    return line, _char_col(lines[line - 1] if 0 < line <= len(lines) else "", bcol)


def _is_called(fx: FileCtx, node: ast.AST) -> bool:
    p = fx.parents.get(id(node))
    return isinstance(p, ast.Call) and p.func is node


def _hasattr_test(test: ast.AST, name: str) -> bool:
    for n in ast.walk(test):
        if isinstance(n, ast.Call) and dotted(n.func) == "hasattr" and len(n.args) >= 2 and \
                isinstance(n.args[1], ast.Constant) and n.args[1].value == name:
            return True
    return False


def _caught(t: ast.AST | None) -> set[str] | None:
    if t is None:
        return None
    if isinstance(t, ast.Tuple):
        out: set[str] = set()
        for e in t.elts:
            out |= _caught(e) or {"BaseException"}
        return out
    return {(dotted(t) or "?").rsplit(".", 1)[-1]}


def _literal_type(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant):
        return _LITERAL_TYPES.get(type(node.value))
    if isinstance(node, ast.JoinedStr):
        return "builtins.str"
    if isinstance(node, (ast.List, ast.ListComp)):
        return "builtins.list"
    if isinstance(node, ast.Tuple):
        return "builtins.tuple"
    if isinstance(node, (ast.Dict, ast.DictComp)):
        return "builtins.dict"
    if isinstance(node, (ast.Set, ast.SetComp)):
        return "builtins.set"
    return None


def _short(node: ast.AST, limit: int = 80) -> str:
    try:
        s = ast.unparse(node)
    except Exception:  # noqa: BLE001
        return "?"
    s = " ".join(s.split())
    return s if len(s) <= limit else s[: limit - 3] + "..."


def _under_any(p: Path, roots: list[Path]) -> bool:
    for r in roots:
        try:
            p.relative_to(r)
            return True
        except ValueError:
            continue
    return False


def _inside(p: Path, root: Path) -> bool:
    try:
        p.resolve().relative_to(root)
        return True
    except (ValueError, OSError):
        return False


def _in_class(path: Path, fn: ast.AST) -> bool:
    mf = cf.module_facts(path)
    for c in mf.classes.values():
        if any(s is fn for s in c.body):
            return True
    return False


def _find_in(base: Path | None, parts: list[str]) -> list[cenv.ModSpec]:
    if base is None:
        return []
    specs = [cenv.ModSpec(base.name, "package", base / "__init__.py", [base])]
    for part in parts:
        if not part:
            continue
        nxt: list[cenv.ModSpec] = []
        for s in specs:
            for d in s.dirs:
                nxt += cenv._in_dir(d, part)
        specs = nxt
        if not specs:
            break
    return specs


def _submodules(d: Path) -> tuple[list[str], bool]:
    out = []
    try:
        for e in os.listdir(d):
            if e.endswith((".py", ".pyi")) and not e.startswith("__init__."):
                out.append(e.rsplit(".", 1)[0])
            elif e.endswith((".pyd", ".so")):
                out.append(e.split(".")[0])
            elif (d / e).is_dir() and e.isidentifier() and e != "__pycache__":
                out.append(e)
    except OSError:
        return [], True
    return sorted(set(out)), False


def _c3(seqs: list[list]) -> list | None:
    seqs = [list(s) for s in seqs if s]
    out: list = []
    while seqs:
        for s in seqs:
            head = s[0]
            if not any(head in t[1:] for t in seqs):
                break
        else:
            return None
        out.append(head)
        seqs = [[x for x in s if x is not head] for s in seqs]
        seqs = [s for s in seqs if s]
    return out


def nearest(name: str, members: dict[str, Member], *, want_call: bool | None = None, limit: int = 3,
            disp=None) -> list[dict]:
    """Up to ``limit`` real names close to ``name`` (edit distance, shared word parts, a few synonyms)."""
    from rapidfuzz.distance import OSA

    low = name.lower()
    parts = _split_ident(name)
    syn = set().union(*(g for g in _SYNONYMS if parts & g)) if parts else set()
    scored = []
    for cand, m in members.items():
        if cand == name or not isinstance(cand, str) or (_dunder(cand) and not _dunder(name)):
            continue
        cl = cand.lower()
        lev = OSA.normalized_similarity(low, cl)   # edit distance counting a transposition once (rpeo ~ repo)
        cp = _split_ident(cand)
        tok = len(parts & cp) / len(parts | cp) if parts and cp else 0.0
        s = max(lev, 0.5 + 0.4 * tok if tok else 0.0)
        short, long_ = sorted((low.replace("_", ""), cl.replace("_", "")), key=len)
        if len(short) >= 3 and (long_.startswith(short) or long_.endswith(short)):
            s = max(s, 0.62 + 0.3 * len(short) / len(long_))   # joinpath ~ join, read_file ~ readfile
        if syn and (cp & syn) and (cp - syn) == (parts - syn):
            s = max(s, 0.75)
        if cand.startswith("_") and not name.startswith("_"):
            s -= 0.1
        if want_call is not None and m is not None:
            callable_ = m.kind in ("function", "method", "classmethod", "staticmethod", "class")
            if callable_ == want_call:
                s += 0.03
        if s >= 0.6:
            scored.append((s, cand, m))
    scored.sort(key=lambda t: (-t[0], t[1]))
    out = []
    for s, cand, m in scored[:limit]:
        row = {"name": cand, "kind": m.kind if m else None, "score": round(s, 2)}
        if m is not None and m.file and m.line:
            row["at"] = f"{disp(m.file) if disp else m.file}:{m.line}"
        out.append(row)
    return out


def _defs_index(files) -> dict:
    idx: dict[str, list] = {}
    for p in files:
        mf = cf.module_facts(p)
        if mf.tree is None:
            continue
        mod = p.stem
        for n, m in mf.names.items():
            if m.kind in ("function", "class", "variable"):
                idx.setdefault(n, []).append((p, m.line, m.kind, f"{mod}.{n}"))
        for cls in mf.classes.values():
            for st in cls.body:
                if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    idx.setdefault(st.name, []).append((p, st.lineno, "method", f"{cls.name}.{st.name}"))
    return idx


def iter_py_files(repo: Path, roots: list[Path]):
    """Python files under ``roots`` (virtual environments, caches and VCS folders skipped)."""
    n = 0
    for root in roots:
        if root.is_file():
            if root.suffix in (".py", ".pyi"):
                yield root
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.endswith(".egg-info")
                                 and not (Path(dirpath) / d / "pyvenv.cfg").is_file())
            for fn in sorted(filenames):
                if fn.endswith(".py"):
                    n += 1
                    if n > MAX_FILES:
                        return
                    yield Path(dirpath) / fn


# -- site collection ---------------------------------------------------------------------------------------

def collect_nodes(tree: ast.Module) -> list[tuple[str, ast.AST, ast.AST | None, int, int]]:
    """(kind, node, extra, first line, last line) of every candidate site, in source order."""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for al in node.names:
                out.append(("import", node, al, al.lineno, al.end_lineno or al.lineno))
        elif isinstance(node, ast.ImportFrom):
            out.append(("from", node, None, node.lineno, node.end_lineno or node.lineno))
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load) and not _dunder(node.attr):
            ln = node.end_lineno or node.lineno
            out.append(("attribute", node, None, ln, ln))
        elif isinstance(node, ast.Call) and any(k.arg for k in node.keywords):
            lines = [k.lineno for k in node.keywords if k.arg]
            out.append(("kwarg", node, None, min(lines), max(lines)))
        elif isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load) and \
                isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
            out.append(("dict_key", node, None, node.slice.lineno, node.slice.lineno))
    out.sort(key=lambda t: (t[3], getattr(t[1], "col_offset", 0)))
    return out


def _unknown_site(rel: str, kind: str, node: ast.AST, extra, why: str) -> list[dict]:
    def rec(line, col, k, expr, name):
        return {"at": f"{rel}:{line}:{col + 1}", "path": rel, "line": line, "col": col + 1, "kind": k,
                "expr": expr, "name": name, "verdict": "unknown", "why": why}
    if kind == "import":
        return [rec(extra.lineno, extra.col_offset, "import", extra.name, extra.name.rsplit(".", 1)[-1])]
    if kind == "from":
        mod = "." * (node.level or 0) + (node.module or "")
        return [rec(node.lineno, node.col_offset, "import", f"{mod}.{a.name}", a.name) for a in node.names
                if a.name != "*"]
    if kind == "attribute":
        return [rec(node.end_lineno or node.lineno, (node.end_col_offset or 0) - len(node.attr), "attribute",
                    _short(node), node.attr)]
    if kind == "kwarg":
        return [rec(k.lineno, k.col_offset, "kwarg", f"{dotted(node.func) or '?'}({k.arg}=)", k.arg)
                for k in node.keywords if k.arg]
    return []


# -- the check ----------------------------------------------------------------------------------------------

_CHECKERS: dict[tuple, Checker] = {}


def get_checker(repo: Path, env: cenv.EnvInfo) -> Checker:
    key = (str(Path(repo).resolve()), env.fingerprint, env.kind)
    ck = _CHECKERS.get(key)
    if ck is None:
        if len(_CHECKERS) > 4:
            for old in list(_CHECKERS.values()):
                oracle = getattr(old.env, "_oracle", None)
                if oracle is not None:
                    oracle.close()
            _CHECKERS.clear()
        ck = _CHECKERS[key] = Checker(repo, env)
    return ck


def get_env(repo: Path, env: str | None) -> cenv.EnvInfo:
    key = (str(Path(repo).resolve()), env or "auto")
    hit = _ENVS.get(key)
    if hit is not None and hit[0] == _env_stamp(Path(repo), env):
        return hit[1]
    info = cenv.select_env(Path(repo), env)
    _ENVS[key] = (_env_stamp(Path(repo), env), info)
    return info


_ENVS: dict[tuple, tuple] = {}


def _env_stamp(repo: Path, env: str | None) -> tuple:
    out = []
    for d in cenv.VENV_DIRS:
        cfg = Path(repo) / d / "pyvenv.cfg"
        st = cf.stat_key(cfg)
        out.append(st)
        lib = Path(repo) / d / "lib"
        for site in (Path(repo) / d / "Lib" / "site-packages", *sorted(lib.glob("python*/site-packages"))):
            out.append(cf.stat_key(site))
    return (env, tuple(out))


def check_source(ck: Checker | None, rel: str, abs_path: Path, source: str, only: set[int] | None = None,
                 why_no_jedi: str = "") -> tuple[list[dict], set[Path], str | None]:
    """All sites of one file (``only``: those touching these lines)."""
    try:
        tree = ast.parse(source, filename=rel)
    except (SyntaxError, ValueError) as exc:
        return [], set(), f"does not parse: {exc}"
    nodes = collect_nodes(tree)
    if only is not None:
        nodes = [t for t in nodes if any(ln in only for ln in range(t[3], t[4] + 1))]
    out: list[dict] = []
    if ck is None:
        for kind, node, extra, _a, _b in nodes:
            out += _unknown_site(rel, kind, node, extra, why_no_jedi)
        return out, set(), None
    fx = FileCtx(ck, rel, abs_path, source, tree)
    for kind, node, extra, _a, _b in nodes:
        try:
            if kind == "import":
                out.append(ck.eval_import(fx, node, extra))
            elif kind == "from":
                out += ck.eval_from(fx, node)
            elif kind == "attribute":
                out.append(ck.eval_attribute(fx, node))
            elif kind == "kwarg":
                out += ck.eval_kwargs(fx, node)
            elif kind == "dict_key":
                r = ck.eval_dict_key(fx, node)
                if r is not None:
                    out.append(r)
        except RecursionError:
            out += [dict(s, why="the check recursed too deeply") for s in _unknown_site(rel, kind, node, extra, "")]
    return out, fx.deps, None


# -- diff -----------------------------------------------------------------------------------------------

def changed_lines(repo: Path, rev: str = "HEAD") -> dict[str, set[int] | None]:
    """Changed lines of Python files against ``rev`` (None: a new, untracked file - every line)."""
    def git(*args) -> str:
        r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=60)
        if r.returncode != 0:
            raise ValueError(f"git {' '.join(args[:2])} failed: {(r.stderr or '').strip()[:200]}")
        return r.stdout

    try:
        inside = git("rev-parse", "--is-inside-work-tree").strip() == "true"
    except ValueError:
        inside = False
    if not inside:
        raise ValueError(f"{repo} is not a git work tree, so there is no diff to check: pass files or directories, "
                         "or --stdin")
    out: dict[str, set[int] | None] = {}
    cur: str | None = None
    for ln in git("diff", "--no-color", "--no-ext-diff", "-U0", rev, "--", "*.py", "*.pyi").splitlines():
        if ln.startswith("+++ "):
            p = ln[4:].strip()
            cur = p[2:] if p.startswith("b/") else (None if p == "/dev/null" else p)
            if cur is not None:
                out.setdefault(cur, set())
        elif ln.startswith("@@") and cur is not None:
            m = re.search(r"\+(\d+)(?:,(\d+))?", ln)
            if m:
                start, count = int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1)
                s = out[cur]
                if s is not None:
                    s.update(range(start, start + count))
    for p in git("ls-files", "--others", "--exclude-standard", "--", "*.py", "*.pyi").splitlines():
        if p.strip():
            out[p.strip()] = None
    return out


# -- cache -------------------------------------------------------------------------------------------------

def _cache_dir(repo: Path) -> Path | None:
    from verinoda.paths import atlas_dir

    d = atlas_dir(repo)
    return d / "cache" / "check" if d.is_dir() else None


def _tree_fp(repo: Path) -> str:
    h = hashlib.sha256()
    for p in iter_py_files(repo, [repo]):
        try:
            h.update(p.relative_to(repo).as_posix().encode("utf-8", "replace") + b"\n")
        except ValueError:
            continue
    return h.hexdigest()


class _Cache:
    def __init__(self, repo: Path, env: cenv.EnvInfo, enabled: bool):
        self.dir = _cache_dir(repo) if enabled else None
        self.repo = repo
        self.env = env
        self.hits = self.misses = 0
        self._tree: str | None = None
        self._sha: dict[str, str | None] = {}

    def tree(self) -> str:
        if self._tree is None:
            self._tree = _tree_fp(self.repo)
        return self._tree

    def _key(self, rel: str, sha: str) -> str:
        import verinoda

        return hashlib.sha256(f"{CHECK_VERSION}|{verinoda.__version__}|{self.env.fingerprint}|{self.env.kind}|"
                              f"{rel}|{sha}".encode()).hexdigest()

    def _dep_sha(self, rel: str) -> str | None:
        if rel not in self._sha:
            self._sha[rel] = cf.file_sha(self.repo / rel)
        return self._sha[rel]

    def get(self, rel: str, sha: str, want: set[int] | None) -> list[dict] | None:
        if self.dir is None:
            return None
        p = self.dir / f"{self._key(rel, sha)}.json"
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.misses += 1
            return None
        if data.get("tree") != self.tree() or any(self._dep_sha(d) != s for d, s in data.get("deps", {}).items()):
            self.misses += 1
            return None
        if want is None and not data.get("complete"):
            self.misses += 1
            return None
        if want is not None and not data.get("complete") and not want <= set(data.get("lines", [])):
            self.misses += 1
            return None
        self.hits += 1
        sites = data.get("sites", [])
        return sites if want is None else [s for s in sites if s["line"] in want or s.get("span", s["line"]) in want]

    def put(self, rel: str, sha: str, sites: list[dict], deps: set[Path], lines: set[int] | None) -> None:
        if self.dir is None:
            return
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            depmap = {}
            for d in deps:
                try:
                    r = d.resolve().relative_to(self.repo).as_posix()
                except (ValueError, OSError):
                    continue
                if r != rel:
                    depmap[r] = self._dep_sha(r)
            body = {"version": CHECK_VERSION, "rel": rel, "sha256": sha, "tree": self.tree(), "deps": depmap,
                    "complete": lines is None, "lines": sorted(lines) if lines else [], "sites": sites}
            tmp = self.dir / f".{os.getpid()}.tmp"
            tmp.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8", newline="\n")
            os.replace(tmp, self.dir / f"{self._key(rel, sha)}.json")
        except OSError:
            pass


# -- public entry points ------------------------------------------------------------------------------------

def _targets(repo: Path, paths: list[str] | None,
             diff: str | None) -> tuple[list[tuple[str, Path, set[int] | None]], str]:
    if paths:
        out = []
        for spec in paths:
            p = Path(spec)
            if not p.is_absolute():
                p = (repo / p) if (repo / p).exists() else (Path.cwd() / p)
            p = p.resolve()
            if not p.exists():
                raise FileNotFoundError(f"{spec} does not exist")
            try:
                p.relative_to(repo)
            except ValueError:
                raise ValueError(f"{spec} is outside the repository {repo}")
            for f in iter_py_files(repo, [p]):
                out.append((f.relative_to(repo).as_posix(), f, None))
        return out, "whole files"
    rev = diff or "HEAD"
    ch = changed_lines(repo, rev)
    out = []
    for rel, lines in sorted(ch.items()):
        f = repo / rel
        if f.is_file() and f.suffix == ".py":
            out.append((rel, f, lines))
    return out, f"changed lines against {rev} (sites on unchanged lines are not checked)"


def check(repo: Path, paths: list[str] | None = None, *, diff: str | None = None, snippet: str | None = None,
          as_path: str | None = None, env: str | None = "auto", include_exists: bool = False,
          use_cache: bool = True) -> dict:
    """Check the names used in files, a diff or a snippet (see the module docstring)."""
    from verinoda import precise

    t0 = time.perf_counter()
    repo = Path(repo).resolve()
    ok, jedi_why = precise.available()
    envinfo = get_env(repo, env) if ok else None
    ck = get_checker(repo, envinfo) if ok and envinfo is not None else None
    cache = _Cache(repo, envinfo, use_cache and ck is not None) if envinfo is not None else None
    files: list[dict] = []
    sites: list[dict] = []
    if snippet is not None:
        rel = (as_path or "snippet.py").replace("\\", "/")
        abs_path = (repo / rel).resolve()
        scope = f"a snippet checked as {rel}"
        got, _deps, err = check_source(ck, rel, abs_path, snippet, None, jedi_why)
        files.append({"path": rel, "sites": len(got), **({"error": err} if err else {})})
        sites += got
    else:
        targets, scope = _targets(repo, paths, diff)
        for rel, f, lines in targets:
            try:
                data = f.read_bytes()
            except OSError as exc:
                files.append({"path": rel, "error": f"unreadable: {exc}"})
                continue
            sha = hashlib.sha256(data).hexdigest()
            got = cache.get(rel, sha, lines) if cache is not None else None
            cached = got is not None
            err = None
            if got is None:
                got, deps, err = check_source(ck, rel, f, data.decode("utf-8", "replace"), lines, jedi_why)
                if cache is not None and not err:
                    cache.put(rel, sha, got, deps, lines)
            files.append({"path": rel, "sites": len(got), **({"cached": True} if cached else {}),
                          **({"error": err} if err else {})})
            sites += got
    counts = {v: 0 for v in VERDICTS}
    for s in sites:
        counts[s["verdict"]] = counts.get(s["verdict"], 0) + 1
    header = _env_header(repo, envinfo, sites, jedi_why if not ok else None)
    problems = counts["absent"] + len(header.get("lock_mismatches", []))
    shown = sites if include_exists else [s for s in sites if s["verdict"] != "exists"]
    shown.sort(key=lambda s: (VERDICTS.index(s["verdict"]), s["path"], s["line"], s["col"]))
    res = {
        "env": header,
        "scope": scope,
        "summary": {"sites": len(sites), **counts, "files": len(files)},
        "files": files,
        "sites": shown,
        "exit": 3 if problems else 0,
        "limits": [
            "existence and signature shape only: a real name used wrongly is not detected",
            "unknown = not checked (open container or receiver type not known), never 'fine'",
            "runtime-made names (setattr, ORM columns, mocks, __getattr__) are unknown by design",
        ],
        "elapsed_s": round(time.perf_counter() - t0, 3),
    }
    if not include_exists:
        res["exists_not_listed"] = counts["exists"]
    if cache is not None and cache.dir is not None:
        res["cache"] = {"hits": cache.hits, "misses": cache.misses}
    if ck is not None:
        res["jedi"] = {"calls": ck.stats["jedi_calls"], "seconds": round(ck.stats["jedi_s"], 3)}
    return res


def _env_header(repo: Path, env: cenv.EnvInfo | None, sites: list[dict], no_jedi: str | None) -> dict:
    if env is None:
        return {"python": "none", "note": no_jedi or "jedi is not installed", "third_party_checked": False}
    h = env.header()
    try:
        import jedi

        h["resolver"] = f"jedi {jedi.__version__}"
    except ImportError:  # pragma: no cover
        pass
    used = sorted({s["source"].split(":", 1)[1] for s in sites
                   if str(s.get("source", "")).startswith("installed:")})
    if used:
        lock: dict[str, dict] = {}
        try:
            from verinoda.references.local import local_versions

            lv = local_versions(repo)
            for k, items in (lv.get("packages") or {}).items():
                if k.startswith("pkg:pypi/"):
                    lock[k.rsplit("/", 1)[-1]] = {it["kind"]: it for it in items}
        except Exception:  # noqa: BLE001
            lock = {}
        checked = {}
        mism = []
        for u in used:
            name, _, ver = u.partition(" ")
            text = f"{ver} installed"
            li = (lock.get(cenv._norm_dist(name)) or {}).get("lock")
            if li and li.get("version"):
                text += f"; {li['source_file']} {li['version']}"
                if li["version"] != ver:
                    mism.append({"package": name, "installed": ver, "locked": li["version"],
                                 "at": li.get("locator")})
            checked[name] = text
        h["packages_checked"] = checked
        h["lock_mismatches"] = mism
    return h


# -- api --------------------------------------------------------------------------------------------------

def api(repo: Path, target: str, *, env: str | None = "auto", private: bool = False) -> dict:
    """The real members of a module, class or function, with signatures and locations."""
    from verinoda import precise

    repo = Path(repo).resolve()
    ok, why = precise.available()
    if not ok:
        return {"target": target, "found": False, "why": why, "exit": 3}
    envinfo = get_env(repo, env)
    ck = get_checker(repo, envinfo)
    parts = [p for p in target.replace(":", ".").split(".") if p]
    if not parts:
        raise ValueError("api: the target is empty")
    u = ck.universe(repo)
    head: dict = {"target": target, "env": envinfo.header()}
    spec = None
    i = len(parts)
    while i > 0:
        specs = [s for s in u.find(".".join(parts[:i])) if s.kind != "namespace" or i == len(parts)]
        if specs:
            spec = specs[0]
            break
        i -= 1
    if spec is None:
        top = {n: Member(n, "module") for n in u.top_level_names()}
        return {**head, "found": False, "why": f"no module {parts[0]} in this project or in {envinfo.label}",
                "nearest": nearest(parts[0], top), "exit": 3}
    modname = ".".join(parts[:i])
    path = spec.file
    c = ck.module_container(modname, path)
    obj_kind = "module"
    cls_facts = None
    std_full = modname if c.source == "stdlib" else None
    for part in parts[i:]:
        m = c.names.get(part)
        if m is None:
            return {**head, "found": False, "why": f"{part} not found in {c.label} {ck.in_env_phrase(c)} ({c.where})",
                    "nearest": nearest(part, c.names, disp=ck.disp), "closed": c.closed, "exit": 3}
        if std_full is not None:
            std_full = f"{std_full}.{part}"
            info = envinfo.oracle().ask("object", name=std_full)
            if info.get("kind") == "class":
                c = ck.std_class_container(std_full, True)
                obj_kind = "class"
                continue
            obj_kind = info.get("kind", "variable")
            return {**head, "found": True, "kind": obj_kind, "source": "stdlib", "at": f"<stdlib>:{std_full}",
                    "signature": info.get("signature"), "exit": 0}
        if m.kind == "class" and m.file and m.line:
            cls_facts, err = cf.class_at(Path(m.file), m.line)
            if cls_facts is None:
                return {**head, "found": True, "kind": "class", "why": err, "exit": 0}
            c = ck.facts_container(cls_facts, f"{c.full}.{part}", True)
            obj_kind = "class"
            continue
        fn = cf.function_at(Path(m.file), m.line) if m.file and m.line else None
        at = f"{ck.disp(m.file)}:{m.line}" if m.file and m.line else c.where
        return {**head, "found": True, "kind": m.kind, "source": c.source, "at": at,
                **({"signature": f"{part}({cf._args_text(fn)})"} if fn is not None else {}), "exit": 0}
    rows = _api_rows(ck, c, std_full, obj_kind, private)
    return {**head, "found": True, "kind": obj_kind, "name": c.full, "source": c.source, "at": c.where,
            "closed": c.closed, **({"why_open": c.why} if c.why else {}), "members": rows,
            "count": len(rows), "exit": 0}


def _api_rows(ck: Checker, c: Container, std_full: str | None, kind: str, private: bool) -> list[dict]:
    rows: list[dict] = []
    if std_full is not None:
        info = ck.env.oracle().ask("members", name=std_full)
        for r in info.get("members", []) if info.get("ok") else []:
            if (r["name"].startswith("_") and not private) or _dunder(r["name"]) and r["name"] != "__init__":
                continue
            rows.append(r)
        if kind == "class":
            for n, m in c.names.items():
                if m.kind == "attribute" and m.file and not any(r["name"] == n for r in rows) and \
                        (private or not n.startswith("_")):
                    rows.append({"name": n, "kind": "attribute", "at": f"{ck.disp(m.file)}:{m.line}"})
        return rows
    for n, m in sorted(c.names.items()):
        if (n.startswith("_") and not private and n != "__init__") or (_dunder(n) and n != "__init__"):
            continue
        row = {"name": n, "kind": m.kind}
        if m.signature:
            row["signature"] = m.signature
        elif m.file and m.line and m.kind in ("function",):
            fn = cf.function_at(Path(m.file), m.line)
            if fn is not None:
                row["signature"] = f"{n}({cf._args_text(fn)})"
        if m.file and m.line:
            row["at"] = f"{ck.disp(m.file)}:{m.line}"
        if m.owner and kind == "class" and m.owner != c.full and not (c.full or "").endswith("." + m.owner):
            row["defined_in"] = m.owner
        rows.append(row)
    return rows


def reset_caches() -> None:
    """Forget warm checkers, environments and parsed files (tests)."""
    for ck in _CHECKERS.values():
        oracle = getattr(ck.env, "_oracle", None)
        if oracle is not None:
            oracle.close()
    _CHECKERS.clear()
    _ENVS.clear()
    cf.reset_caches()
