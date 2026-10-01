"""Groups of repositories with links between them (``verinoda group ...``).

A group names two or more project folders (registered names, ``NAME=PATH`` or plain folders, as
``verinoda mcp serve --projects`` takes them). Each member keeps its own index in its own ``.verinoda``; nothing
is copied and nothing of the group is written into a member. The group list is ``groups.json`` in the user config
folder, next to the project registry (:mod:`verinoda.mcp.projects`).

``link`` reads the members' existing indexes (never rebuilds them) and finds the calls from one member into
another:

* what each member defines for others to import: Python modules (packages found through ``__init__.py`` from the
  indexed ``.py`` files and named from their source root, namespace folders included, plus a namespace folder
  named after the distribution in ``pyproject.toml``, ``setup.cfg`` or ``setup.py``; a single module at a source
  root only under ``src``/``lib``/``python`` or when the distribution names it), JavaScript/TypeScript packages
  (the ``name`` of each ``package.json``, its entry and its ``exports`` map), Go modules (``go.mod``) and Java
  classes (``package`` lines of the indexed ``.java`` files);
* in every other member, the imports that name one of those (a module the importing member defines itself, or a
  module next to one of its scripts, is its own and is left alone) and the calls made through the names those
  imports bind (Python by its scopes; JavaScript, Go and Java with comments and literals blanked);
* a call whose callee resolves through such an import to exactly one definition in the other member's graph is a
  link: the caller with its call site (``file:line`` in the calling member) and the definition (``file:line`` in
  the defining member). A module two members define, or a name with several definitions, is no link: it is listed
  as ambiguous with its candidates.

A link is read from two texts, so it is a lead (``strong_inference``) unless the lines are re-read and confirm
it: the call-site line names the callee (:func:`verinoda.callsite.line_names_target`, or the local name an import
on a re-read line binds to it) and the definition line names it; then it is ``statically_verified``. Which
definition runs still depends on what is installed where the code runs (a published version of the other member,
a different path), which nothing here can see.

The links are kept in a sidecar (``<group>.links.json`` in the user config folder's ``groups`` folder, or a
folder chosen at ``group create --links-dir``), with each member's graph identity (size, mtime, sha256 of its
``graph.json``). A member re-indexed since makes its links stale until ``group link`` runs again.
"""

from __future__ import annotations

import ast
import json
import os
import re
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

GROUPS_NAME = "groups.json"
LINKS_DIRNAME = "groups"
LINKS_VERSION = 1
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
# Windows device names: a file named after one (with any extension) cannot be written there
RESERVED_NAMES = frozenset({"con", "prn", "aux", "nul", *(f"com{i}" for i in range(10)),
                            *(f"lpt{i}" for i in range(10))})
PROJECT_FILES = ("pyproject.toml", "setup.py", "setup.cfg")
# top-level Python folders every project may have; a module of that name is never another member's API
SKIP_TOP = frozenset({"tests", "test", "docs", "doc", "examples", "example", "scripts", "benchmarks", "conftest",
                      "setup", "build", "dist", "noxfile", "tasks", "__init__"})
PY_SRC_DIRS = ("", "src", "lib", "python")
JS_SUFFIXES = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts")
JS_ENTRY_NAMES = ("src/index.ts", "src/index.tsx", "src/index.js", "src/index.mjs", "index.ts", "index.tsx",
                  "index.js", "index.mjs", "lib/index.js", "lib/index.ts")
MAX_SOURCE_CHARS = 1_000_000   # a larger source file is generated code, not read
REPORT_CAP = 500               # entries kept per list in the sidecar
IMPORT_RELATIONS = {"imports", "imports_from", "re_exports"}


class GroupError(ValueError):
    """A group spec or change that cannot be done (the message says why and what to do)."""


# -- the group list -----------------------------------------------------------------------

def groups_path() -> Path:
    from verinoda.paths import user_config_dir

    return user_config_dir() / GROUPS_NAME


def groups() -> list[dict]:
    p = groups_path()
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise GroupError(f"cannot read the group list {p}: {exc}") from None
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise GroupError(f"the group list {p} is not valid JSON ({exc}); fix or remove the file") from None
    rows = data.get("groups") if isinstance(data, dict) else None
    return [r for r in rows or [] if isinstance(r, dict) and isinstance(r.get("name"), str)
            and isinstance(r.get("members"), list)]


def _write_groups(rows: list[dict]) -> None:
    p = groups_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"version": 1, "groups": rows}, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def get(name: str) -> dict:
    for r in groups():
        if r["name"] == name:
            return r
    raise GroupError(f"no group is named {name!r} (`verinoda group list`)")


def _inside(p: Path, root: Path) -> bool:
    return p == root or root in p.parents


def create(name: str, specs: list[str], *, links_dir: str | None = None) -> dict:
    """A group of the folders ``specs`` names (registered project names, ``NAME=PATH`` or folders)."""
    from verinoda.mcp import projects as P

    if not NAME_RE.match(name or ""):
        raise GroupError(f"group name {name!r}: use letters, digits, '.', '_' or '-' (at most 64, not starting "
                         "with '.', '_' or '-')")
    if name.split(".")[0].lower() in RESERVED_NAMES:
        raise GroupError(f"group name {name!r} is a Windows device name; pick another")
    if any(r["name"] == name for r in groups()):
        raise GroupError(f"a group named {name!r} exists; `verinoda group remove {name}` first")
    try:
        members = P.resolve_specs(list(specs))
    except P.ProjectError as exc:
        raise GroupError(str(exc)) from None
    if len(members) < 2:
        raise GroupError("a group needs two members or more")
    row: dict = {"name": name, "members": [{"name": n, "path": str(p)} for n, p in members],
                 "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    if links_dir:
        d = Path(os.path.realpath(str(Path(links_dir).expanduser())))
        for n, p in members:
            if _inside(d, Path(p)):
                raise GroupError(f"--links-dir {d} lies inside member {n}: nothing of a group is written into a "
                                 "member; pick a folder outside every member")
        row["links_dir"] = str(d)
    _write_groups([*groups(), row])
    return {**row, "groups": str(groups_path())}


def remove(name: str) -> dict:
    """Forget a group and delete its links file (the members and their indexes are not touched)."""
    row = get(name)
    _write_groups([r for r in groups() if r["name"] != name])
    lp = links_path(row)
    try:
        lp.unlink()
        removed = True
    except OSError:
        removed = False
    return {"removed": name, "links_removed": removed, "groups": str(groups_path())}


def links_path(row: dict) -> Path:
    from verinoda.paths import user_config_dir

    d = Path(row["links_dir"]) if row.get("links_dir") else user_config_dir() / LINKS_DIRNAME
    return d / f"{row['name']}.links.json"


def members_of(row: dict) -> list[tuple[str, Path]]:
    return [(m["name"], Path(m["path"])) for m in row["members"] if isinstance(m, dict) and m.get("name")
            and m.get("path")]


def read_links(row: dict) -> dict | None:
    try:
        data = json.loads(links_path(row).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("version") == LINKS_VERSION else None


def stale_members(row: dict, side: dict | None) -> list[str]:
    """The members whose graph is not the one the links were computed from (re-indexed, removed, or a member
    the links do not know)."""
    from verinoda.index import same_graph
    from verinoda.paths import graph_path

    known = (side or {}).get("members") or {}
    out = []
    for n, p in members_of(row):
        rec = known.get(n)
        if not rec or Path(rec.get("path") or "") != p or not same_graph(graph_path(p), rec.get("graph")):
            out.append(n)
    return out


# -- what a member defines ----------------------------------------------------------------

def _read(p: Path) -> str | None:
    try:
        if p.stat().st_size > MAX_SOURCE_CHARS:
            return None
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _dist_names(root: Path) -> list[str]:
    """Distribution names a Python project declares (pyproject.toml, setup.cfg, setup.py), as import names."""
    names: list[str] = []
    text = _read(root / "pyproject.toml") or ""
    m = re.search(r"(?ms)^\[project\]\s*$.*?^name\s*=\s*[\"']([^\"']+)[\"']", text) or \
        re.search(r"(?ms)^\[tool\.poetry\]\s*$.*?^name\s*=\s*[\"']([^\"']+)[\"']", text)
    if m:
        names.append(m.group(1))
    m = re.search(r"(?ms)^\[metadata\]\s*$.*?^name\s*=\s*(\S+)", _read(root / "setup.cfg") or "")
    if m:
        names.append(m.group(1))
    m = re.search(r"""\bname\s*=\s*["']([A-Za-z0-9._-]+)["']""", _read(root / "setup.py") or "")
    if m:
        names.append(m.group(1))
    return sorted({re.sub(r"[-.]+", "_", n).lower() for n in names})


def _listed_py_modules(root: Path) -> set[str]:
    """Single-file modules a Python project lists for its distribution (``py-modules`` in pyproject.toml,
    ``py_modules`` in setup.cfg or setup.py)."""
    out: set[str] = set()
    m = re.search(r"(?ms)^py-modules\s*=\s*\[(.*?)\]", _read(root / "pyproject.toml") or "")
    if m:
        out.update(re.findall(r"[\"']([\w.]+)[\"']", m.group(1)))
    m = re.search(r"(?ms)^py_modules\s*=\s*(.*?)(?=^\S|\Z)", _read(root / "setup.cfg") or "")
    if m:
        out.update(re.findall(r"[\w.]+", m.group(1)))
    m = re.search(r"\bpy_modules\s*=\s*\[(.*?)\]", _read(root / "setup.py") or "", re.S)
    if m:
        out.update(re.findall(r"[\"']([\w.]+)[\"']", m.group(1)))
    return out


_TEST_DIR = re.compile(r"(?:^|/)(?:tests?|__tests__|spec|examples?|fixtures?|e2e)/")
_EXPORT_CONDITIONS = ("source", "import", "module", "default", "node", "require", "types")


def _js_export_target(v) -> str | None:
    """The path a ``package.json`` ``exports`` value names: a string, the first condition of
    :data:`_EXPORT_CONDITIONS` it has (nested conditions followed), or the first such entry of a list."""
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        return next((t for t in map(_js_export_target, v) if t), None)
    if isinstance(v, dict):
        for k in _EXPORT_CONDITIONS:
            if k in v:
                t = _js_export_target(v[k])
                if t:
                    return t
    return None


class Member:
    """One member: its graph and what it defines for other members to import."""

    def __init__(self, name: str, root: Path, *, augment: bool = False):
        from verinoda import index
        from verinoda.paths import graph_path

        self.name, self.root = name, Path(root)
        gp = graph_path(self.root)
        if not gp.exists():
            raise GroupError(f"member {name} ({root}) has no index: run `verinoda scan {root}` (group link reads "
                             "the members' indexes, it never builds them)")
        self.g = index.load(self.root, augment=augment)
        self.files: list[str] = sorted({d.get("source_file") for _, d in self.g.G.nodes(data=True)
                                        if d.get("source_file") and d.get("file_type") == "code"})
        self.file_node: dict[str, str] = {}
        for n, d in self.g.G.nodes(data=True):
            f = d.get("source_file")
            if f and f not in self.file_node and self.g.is_file_node(n):
                self.file_node[f] = n
        self.py_modules, self.py_own = self._py_modules()
        self.py_tops = {m.split(".")[0] for m in self.py_modules} - SKIP_TOP
        self.js_packages = self._js_packages()
        self.go_modules = self._go_modules()
        self.java_classes = self._java_classes()
        self._spans: dict[str, list[tuple[int, int, str]]] = {}
        self._trees: dict[str, ast.AST | None] = {}

    # Python: dotted module name -> its file
    def _py_modules(self) -> tuple[dict[str, str], set[str]]:
        """(dotted module name -> its file, for what other members may import; the top-level names this member
        can import itself). A package is named from its source root (the repository, a folder with its own
        pyproject.toml, setup.py or setup.cfg, or the ``src``/``lib``/``python`` folder of one): folders without
        ``__init__.py`` between the root and the topmost package are part of the name (a PEP 420 namespace, such
        as ``acme`` in ``acme/core/__init__.py``). A single module at a source root is the member's API only
        under ``src``/``lib``/``python`` or when the distribution names it (``py-modules``, or the distribution's
        own name); other files there are scripts. Every module a file of a folder without ``__init__.py`` sits
        next to, and every topmost package, is a name the member imports itself (a script's folder and pytest's
        rootdir are on its path)."""
        out: dict[str, str] = {}
        own: set[str] = set()
        dist = _dist_names(self.root)
        listed = _listed_py_modules(self.root) | set(dist)
        roots = sorted({(PurePosixPath(s) / n if s else PurePosixPath(n)) for s in PY_SRC_DIRS for n in dist},
                       key=str)
        pkg: dict[str, bool] = {}
        src: dict[str, bool] = {}

        def is_pkg(d: PurePosixPath) -> bool:
            k = str(d)
            if k not in pkg:
                pkg[k] = k not in ("", ".") and (self.root / d / "__init__.py").exists()
            return pkg[k]

        def is_src_root(d: PurePosixPath) -> bool:
            k = str(d)
            if k in ("", "."):
                return True
            if k not in src:
                src[k] = any((self.root / d / p).is_file() for p in PROJECT_FILES) or (
                    d.name in PY_SRC_DIRS[1:] and not is_pkg(d) and is_src_root(d.parent))
            return src[k]

        for f in self.files:
            if not f.endswith(".py"):
                continue
            rel = PurePosixPath(f)
            parent = rel.parent
            if not is_pkg(parent) and rel.stem != "__init__":
                own.add(rel.stem)
            top = None
            d = parent
            while is_pkg(d):
                top = d
                d = d.parent
            if top is not None:
                own.add(top.name)
                d = top.parent
                while not is_src_root(d):  # namespace folders down to the source root
                    d = d.parent
                base = len(d.parts) if str(d) not in ("", ".") else 0
            else:
                ns = next((r for r in roots if r == parent or r in parent.parents), None)
                if ns is not None:
                    base = len(ns.parent.parts) if str(ns.parent) not in ("", ".") else 0
                elif is_src_root(parent) and rel.stem != "__init__" and (
                        parent.name in PY_SRC_DIRS[1:] or rel.stem in listed):
                    out.setdefault(rel.stem, f)
                    continue
                else:
                    continue
            mod = list(rel.parts)[base:]
            mod[-1] = rel.stem
            if mod[-1] == "__init__":
                mod = mod[:-1]
            if mod and all(p.isidentifier() for p in mod):
                out.setdefault(".".join(mod), f)
        return out, own

    def defines_py(self, module: str) -> bool:
        """Whether ``module`` or a package it is in is one of this member's modules."""
        parts = module.split(".")
        return parts[0] not in SKIP_TOP and any(".".join(parts[:i]) in self.py_modules
                                                for i in range(len(parts), 0, -1))

    def _js_packages(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        cands = [self.root / "package.json"]
        for f in self.files:  # a monorepo's packages: package.json files the index read
            if PurePosixPath(f).name == "package.json" and "node_modules" not in f:
                cands.append(self.root / f)
        for pj in cands:
            text = _read(pj)
            try:
                data = json.loads(text) if text else None
            except ValueError:
                data = None
            if not isinstance(data, dict) or not isinstance(data.get("name"), str):
                continue
            d = PurePosixPath(pj.parent.relative_to(self.root).as_posix())
            d_s = "" if str(d) == "." else str(d)
            entry = None
            exp = data.get("exports")
            subpaths: dict[str, str | None] | None = None  # "./sub" -> target, when exports maps subpaths
            if isinstance(exp, dict) and any(k.startswith(".") for k in exp):
                subpaths = {k: _js_export_target(v) for k, v in exp.items() if k.startswith(".")}
                exp = subpaths.get(".")
            elif isinstance(exp, (dict, list)):
                exp = _js_export_target(exp)
            for e in (data.get("source"), exp if isinstance(exp, str) else None, data.get("module"),
                      data.get("main"), data.get("types")):
                if isinstance(e, str) and e:
                    entry = self.js_file(d_s, e)
                    if entry:
                        break
            if entry is None:
                for e in JS_ENTRY_NAMES:
                    rel = f"{d_s}/{e}" if d_s else e
                    if rel in self.file_node:
                        entry = rel
                        break
            out.setdefault(data["name"], {"dir": d_s, "entry": entry, "exports": subpaths})
        return out

    def js_file(self, d: str, target: str) -> str | None:
        """The indexed file a package-relative path names: the path itself, its stem with another JavaScript or
        TypeScript extension, or (for built output under ``dist``, ``lib``, ``build`` or ``out``) the same path
        under ``src``."""
        t = target[2:] if target.startswith("./") else target.lstrip("/")
        cands = [t]
        head, _, rest = t.partition("/")
        if head in ("dist", "lib", "build", "out", "esm", "cjs") and rest:
            cands.append(f"src/{rest}")
        for c in cands:
            rel = f"{d}/{c}" if d else c
            if rel in self.file_node:
                return rel
            stem = re.sub(r"\.d\.ts$|\.[cm]?[jt]sx?$", "", rel)
            for suf in (*JS_SUFFIXES, *(f"/index{s}" for s in JS_SUFFIXES)):
                if stem + suf in self.file_node:
                    return stem + suf
        return None

    def js_subpath(self, info: dict, sub: str) -> str | None:
        """The indexed file ``import 'pkg/sub'`` loads: through the package's ``exports`` map when it has one
        (``./sub`` or a ``./prefix/*`` pattern), else ``sub`` or ``src/sub`` as a file or a folder's index."""
        base, exp = info["dir"], info.get("exports")
        if exp is not None:
            key = "./" + sub
            target = exp.get(key)
            if target is None:
                for k, v in exp.items():
                    pre, star, post = k.partition("*")
                    if star and isinstance(v, str) and key.startswith(pre) and key.endswith(post) and \
                            len(key) >= len(pre) + len(post):
                        target = v.replace("*", key[len(pre):len(key) - len(post)])
                        break
            return self.js_file(base, target) if isinstance(target, str) else None
        for stem in (sub, f"src/{sub}"):
            rel = f"{base}/{stem}" if base else stem
            for suf in (*JS_SUFFIXES, *(f"/index{s}" for s in JS_SUFFIXES)):
                if rel + suf in self.file_node:
                    return rel + suf
            if rel in self.file_node and rel.endswith(JS_SUFFIXES):
                return rel
        return None

    def js_exported(self, f: str, name: str, depth: int = 0) -> list[str]:
        """Definitions named ``name`` a module ``f`` exports: its own, the symbols it imports or re-exports
        (graph edges), and through ``export * from`` (a re-export of a whole file) at most four files deep."""
        found = self.in_module(f, name)
        if found or depth >= 4:
            return found
        fn = self.file_node.get(f)
        if fn is None:
            return []
        out: list[str] = []
        for v, _ in self.g.out_edges(fn, {"re_exports"}):
            g = self.g.file(v) if self.g.is_file_node(v) else None
            if g and g != f and not _TEST_DIR.search(g):
                out.extend(self.js_exported(g, name, depth + 1))
        return out

    def _go_modules(self) -> dict[str, str]:
        out: dict[str, str] = {}
        cands = [self.root / "go.mod"] + [self.root / f for f in self.files if PurePosixPath(f).name == "go.mod"]
        for gm in cands:
            m = re.search(r"(?m)^module\s+(\S+)", _read(gm) or "")
            if m:
                d = gm.parent.relative_to(self.root).as_posix()
                out.setdefault(m.group(1), "" if d == "." else d)
        return out

    def _java_classes(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for f in self.files:
            if not f.endswith(".java"):
                continue
            text = _read(self.root / f) or ""
            m = re.search(r"(?m)^\s*package\s+([\w.]+)\s*;", text[:20000])
            fq = f"{m.group(1)}.{PurePosixPath(f).stem}" if m else PurePosixPath(f).stem
            out.setdefault(fq, f)
        return out

    # -- symbols ----------------------------------------------------------------------------
    def bare(self, nid: str) -> str:
        return str(self.g.label(nid)).strip().lstrip(".").split("(")[0]

    def top_symbols(self, f: str, name: str) -> list[str]:
        """Symbols named ``name`` defined at the top of ``f`` (not methods)."""
        return [n for n in self.g.symbols_in(f) if self.bare(n) == name
                and not str(self.g.label(n)).startswith(".")]

    def imported_into(self, f: str, name: str) -> list[str]:
        """Symbols named ``name`` that file ``f`` imports or re-exports (graph edges)."""
        fn = self.file_node.get(f)
        if fn is None:
            return []
        return sorted({v for v, _ in self.g.out_edges(fn, IMPORT_RELATIONS)
                       if self.g.is_symbol(v) and self.bare(v) == name})

    def member_of(self, cls: str, name: str) -> list[str]:
        return [v for v, _ in self.g.out_edges(cls, {"method"}) if self.bare(v) == name]

    def in_module(self, f: str, name: str) -> list[str]:
        return self.top_symbols(f, name) or self.imported_into(f, name)

    def symbols_under(self, d: str, name: str, *, direct: bool = False) -> list[str]:
        out = []
        for f in self.files:
            p = PurePosixPath(f)
            if (str(p.parent) == (d or ".")) if direct else (not d or f.startswith(d.rstrip("/") + "/")):
                out.extend(self.top_symbols(f, name))
        return out

    def caller_at(self, f: str, line: int) -> str | None:
        """The innermost symbol of ``f`` whose span holds ``line`` (else the file's own node)."""
        if f not in self._spans:
            rows = []
            for n in self.g.symbols_in(f):
                sp = self.g.span(n)
                if sp:
                    rows.append((sp[0], sp[1], n))
            self._spans[f] = rows
        best = None
        for a, b, n in self._spans[f]:
            if a <= line <= b and (best is None or a >= best[0]):
                best = (a, b, n)
        return best[2] if best else self.file_node.get(f)

    def loc(self, nid: str) -> str | None:
        f, ln = self.g.file(nid), self.g.line(nid)
        return f"{f}:{ln}" if f and ln else f

    def identity(self) -> dict:
        from verinoda.index import graph_identity
        from verinoda.paths import graph_path

        return graph_identity(graph_path(self.root))


# -- the importing side -------------------------------------------------------------------

def _line(text_lines: list[str], n: int) -> str:
    return text_lines[n - 1] if 1 <= n <= len(text_lines) else ""


def _dotted(node) -> str | None:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


class _PyScope:
    __slots__ = ("kind", "parent", "binds", "globals")

    def __init__(self, kind: str, parent: _PyScope | None):
        self.kind, self.parent = kind, parent
        self.binds: dict[str, list[int | None]] = defaultdict(list)  # name -> import indexes, None: not an import
        self.globals: set[str] = set()

    def lookup(self, name: str) -> _PyScope | None:
        """The scope a name read here is bound in (Python's rules: a class body is not seen from the functions
        in it; a name bound anywhere in a function is that function's)."""
        s: _PyScope | None = self
        while s is not None:
            if name in s.binds:
                return s
            s = s.parent
            while s is not None and s.kind == "class":
                s = s.parent
        return None


class _PyUses(ast.NodeVisitor):
    """Imports and calls of a Python module with the scope each is in."""

    def __init__(self):
        self.imports: list[dict] = []
        self.calls: list[tuple[dict, _PyScope]] = []
        self.module = self.scope = _PyScope("module", None)

    def bind(self, name: str, how: int | None) -> None:
        s = self.module if name in self.scope.globals else self.scope
        s.binds[name].append(how)

    def _args(self, args: ast.arguments) -> None:
        for a in [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]:
            if a is not None:
                self.bind(a.arg, None)

    def _in(self, kind: str, body, args: ast.arguments | None = None) -> None:
        outer, self.scope = self.scope, _PyScope(kind, self.scope)
        if args is not None:
            self._args(args)
        for n in body if isinstance(body, list) else [body]:
            self.visit(n)
        self.scope = outer

    def visit_Import(self, node: ast.Import) -> None:
        for a in node.names:
            local = a.asname or a.name.split(".")[0]
            self.imports.append({"module": a.name, "line": node.lineno,
                                 "binds": {local: a.name if a.asname else a.name.split(".")[0]}})
            self.bind(local, len(self.imports) - 1)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        names = [a for a in node.names if a.name != "*"]
        if node.level == 0 and node.module:
            self.imports.append({"module": node.module, "line": node.lineno,
                                 "binds": {(a.asname or a.name): f"{node.module}.{a.name}" for a in names}})
            for a in names:
                self.bind(a.asname or a.name, len(self.imports) - 1)
        else:  # a relative import: the member's own module
            for a in names:
                self.bind(a.asname or a.name, None)

    def visit_FunctionDef(self, node) -> None:
        for d in [*node.decorator_list, *node.args.defaults, *[x for x in node.args.kw_defaults if x]]:
            self.visit(d)
        self.bind(node.name, None)
        self._in("function", node.body, node.args)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node: ast.Lambda) -> None:
        for d in [*node.args.defaults, *[x for x in node.args.kw_defaults if x]]:
            self.visit(d)
        self._in("function", node.body, node.args)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for d in [*node.decorator_list, *node.bases, *[k.value for k in node.keywords]]:
            self.visit(d)
        self.bind(node.name, None)
        self._in("class", node.body)

    def _comp(self, node) -> None:
        outer, self.scope = self.scope, _PyScope("function", self.scope)
        for g in node.generators:
            self.visit(g)
        for n in ([node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt]):
            self.visit(n)
        self.scope = outer

    visit_ListComp = visit_SetComp = visit_GeneratorExp = visit_DictComp = _comp

    def visit_Global(self, node) -> None:
        self.scope.globals.update(node.names)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.bind(node.id, None)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name:
            self.bind(node.name, None)
        self.generic_visit(node)

    def visit_MatchAs(self, node) -> None:
        if node.name:
            self.bind(node.name, None)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        name = _dotted(node.func)
        if name:
            self.calls.append(({"name": name, "line": node.lineno}, self.scope))
        self.generic_visit(node)


def py_uses(text: str) -> tuple[list[dict], list[dict]]:
    """(imports, calls) of a Python file: each import ``{module, line, binds: {local: dotted}}``, each call
    ``{name (the dotted callee as written), line, import}``. ``import`` is the index of the import whose binding
    the call's first name reads, by Python's scopes: an import inside a function binds only there, and a name
    the reading scope binds otherwise too (a ``def``, a class, an assignment, a parameter, a loop target), or no
    import binds, gives None."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return [], []
    v = _PyUses()
    try:
        v.visit(tree)
    except RecursionError:
        return [], []
    calls = []
    for c, scope in v.calls:
        head = c["name"].split(".")[0]
        s = scope.lookup(head)
        hows = s.binds[head] if s is not None else []
        c["import"] = hows[-1] if hows and None not in hows else None
        calls.append(c)
    return v.imports, calls


_JS_IMPORT = re.compile(r"""\bimport\s+(?:type\s+)?([\w$*{}\s,]+?)\s+from\s*['"]([^'"]+)['"]""")
_JS_REQUIRE = re.compile(r"""\b(?:const|let|var)\s+([\w$]+|\{[^}]*\})\s*=\s*require\(\s*['"]([^'"]+)['"]\s*\)""")


def _js_binds(clause: str) -> dict[str, str]:
    """local name -> imported name ('*' for a namespace, 'default' for a default import)."""
    out: dict[str, str] = {}
    clause = clause.strip()
    m = re.search(r"\{([^}]*)\}", clause)
    if m:
        for part in m.group(1).split(","):
            part = part.strip()
            if not part or part.startswith("type "):
                continue
            src, _, alias = part.partition(" as ")
            src = src.strip()
            for sep in (":",):  # require destructuring: { a: b }
                if sep in src and not alias:
                    src, _, alias = src.partition(sep)
            out[(alias or src).strip()] = src.strip()
        clause = clause[:m.start()] + clause[m.end():]
    m = re.search(r"\*\s*as\s+([\w$]+)", clause)
    if m:
        out[m.group(1)] = "*"
        clause = clause[:m.start()] + clause[m.end():]
    for part in clause.split(","):
        part = part.strip()
        if re.fullmatch(r"[\w$]+", part or "-"):
            out[part] = "default"
    return out


_NOT_NL = re.compile(r"[^\n]")


def _blank_js_literals(code: str) -> str:
    """Comment-free JavaScript/TypeScript ``code`` with the insides of its string, template and regular
    expression literals replaced by spaces (newlines kept): a call spelled in a literal is not code. The
    ``${...}`` parts of a template are code and stay."""
    from verinoda.cross_service import _JS_SPECIAL, _regex_start, _skip_regex, _skip_string

    buf = list(code)
    n = len(code)

    def blank(a: int, b: int) -> None:
        for k in range(a, min(b, n)):
            if buf[k] != "\n":
                buf[k] = " "

    def string(i: int) -> int:
        j = min(_skip_string(code, i), n)
        blank(i + 1, j - 1 if j - 1 > i and code[j - 1] == code[i] else j)
        return j

    def template(i: int) -> int:
        j = start = i + 1
        while j < n:
            c = code[j]
            if c == "\\":
                j += 2
            elif c == "`":
                blank(start, j)
                return j + 1
            elif code.startswith("${", j):
                blank(start, j)
                j = start = interpolation(j + 2)
            else:
                j += 1
        blank(start, n)
        return n

    def interpolation(j: int) -> int:
        depth = 1
        while j < n:
            c = code[j]
            if c in "'\"":
                j = string(j)
                continue
            if c == "`":
                j = template(j)
                continue
            depth += (c == "{") - (c == "}")
            j += 1
            if not depth:
                return j
        return n

    i = 0
    while True:
        m = _JS_SPECIAL.search(code, i)
        if not m:
            break
        i = m.start()
        c = code[i]
        if c in "'\"":
            i = string(i)
        elif c == "`":
            i = template(i)
        elif _regex_start(code, i):
            j = _skip_regex(code, i)
            blank(i + 1, j)
            i = j
        else:
            i += 1
    return "".join(buf)


# comments, then string literals (Go: interpreted, raw and rune; Java: text blocks, strings and chars)
_GO_NOISE = re.compile(r"//[^\n]*|/\*.*?(?:\*/|\Z)|\"(?:[^\"\\\n]|\\.)*\"?|`[^`]*`?|'(?:[^'\\\n]|\\.)*'?", re.S)
_JAVA_NOISE = re.compile(r"//[^\n]*|/\*.*?(?:\*/|\Z)|\"\"\".*?(?:\"\"\"|\Z)|\"(?:[^\"\\\n]|\\.)*\"?"
                         r"|'(?:[^'\\\n]|\\.)*'?", re.S)


def _blank_noise(text: str, rx: re.Pattern, *, strings: bool) -> str:
    """``text`` with the comments ``rx`` finds replaced by spaces, and with ``strings`` the insides of its
    literals too (quotes, offsets and newlines kept)."""
    def sub(m: re.Match) -> str:
        s = m.group(0)
        if s[:2] in ("//", "/*"):
            return _NOT_NL.sub(" ", s)
        if not strings:
            return s
        q = '"""' if s.startswith('"""') else s[0]
        tail = q if len(s) >= 2 * len(q) and s.endswith(q) else ""
        return q + _NOT_NL.sub(" ", s[len(q):len(s) - len(tail)]) + tail
    return rx.sub(sub, text)


def js_uses(text: str) -> tuple[list[dict], list[dict]]:
    """(imports, calls) of a JavaScript/TypeScript file, read as text with its comments blanked out; calls are
    read with the literals blanked too."""
    from verinoda.cross_service import strip_js

    text = strip_js(text)  # same length and lines: a commented-out import or call is not read

    starts = [0] + [m.end() for m in re.finditer("\n", text)]

    def line_of(pos: int) -> int:
        import bisect

        return bisect.bisect_right(starts, pos)

    imports = []
    for rx, req in ((_JS_IMPORT, False), (_JS_REQUIRE, True)):
        for m in rx.finditer(text):
            clause, spec = m.group(1), m.group(2)
            binds = _js_binds(clause) if (not req or clause.startswith("{")) else {clause: "*"}
            imports.append({"module": spec, "line": line_of(m.start()), "binds": binds})
    calls = []
    locals_ = {b for i in imports for b in i["binds"]}
    if locals_:
        code = _blank_js_literals(text)
        rx = re.compile(r"(?<![\w$.])(?:new\s+)?(" + "|".join(re.escape(x) for x in sorted(locals_, key=len,
                                                                                          reverse=True))
                        + r")((?:\s*\.\s*[\w$]+)?)\s*(?:<[^>()]*>)?\s*\(")
        for m in rx.finditer(code):
            name = m.group(1) + re.sub(r"\s+", "", m.group(2) or "")
            calls.append({"name": name, "line": line_of(m.start())})
    return imports, calls


_GO_IMPORT_BLOCK = re.compile(r"(?ms)^import\s*\((.*?)^\)")
_GO_IMPORT_ONE = re.compile(r"""(?m)^import[ \t]+([\w.]+[ \t]+)?"([^"]+)"[ \t\r]*$""")
_GO_SPEC = re.compile(r"""^\s*([\w.]+\s+)?"([^"]+)"\s*$""")


def go_uses(text: str) -> tuple[list[dict], list[dict]]:
    """(imports, calls) of a Go file: imports read with comments blanked, calls with literals blanked too."""
    code = _blank_noise(text, _GO_NOISE, strings=True)
    text = _blank_noise(text, _GO_NOISE, strings=False)
    starts = [0] + [m.end() for m in re.finditer("\n", text)]

    def line_of(pos: int) -> int:
        import bisect

        return bisect.bisect_right(starts, pos)

    imports = []
    for m in _GO_IMPORT_BLOCK.finditer(text):
        off = m.start(1)
        for ln in m.group(1).splitlines(keepends=True):
            s = _GO_SPEC.match(ln)
            if s:
                alias = (s.group(1) or "").strip() or s.group(2).rsplit("/", 1)[-1]
                imports.append({"module": s.group(2), "line": line_of(off), "binds": {alias: "*"}})
            off += len(ln)
    for m in _GO_IMPORT_ONE.finditer(text):
        alias = (m.group(1) or "").strip() or m.group(2).rsplit("/", 1)[-1]
        imports.append({"module": m.group(2), "line": line_of(m.start()), "binds": {alias: "*"}})
    calls = []
    locals_ = {b for i in imports for b in i["binds"] if b not in ("_", ".")}
    if locals_:
        rx = re.compile(r"(?<![\w.])(" + "|".join(re.escape(x) for x in sorted(locals_)) + r")\.([A-Z]\w*)\s*\(")
        for m in rx.finditer(code):
            calls.append({"name": f"{m.group(1)}.{m.group(2)}", "line": line_of(m.start())})
    return imports, calls


_JAVA_IMPORT = re.compile(r"(?m)^\s*import\s+(static\s+)?([\w.]+)\s*;")


def java_uses(text: str) -> tuple[list[dict], list[dict]]:
    """(imports, calls) of a Java file, read with comments and literals blanked."""
    text = _blank_noise(text, _JAVA_NOISE, strings=True)
    starts = [0] + [m.end() for m in re.finditer("\n", text)]

    def line_of(pos: int) -> int:
        import bisect

        return bisect.bisect_right(starts, pos)

    imports = []
    for m in _JAVA_IMPORT.finditer(text):
        fq = m.group(2)
        if m.group(1):  # import static a.b.Cls.m;  binds m -> Cls.m
            cls, _, meth = fq.rpartition(".")
            imports.append({"module": cls, "line": line_of(m.start()), "binds": {meth: f"{cls}.{meth}"}})
        else:
            imports.append({"module": fq, "line": line_of(m.start()), "binds": {fq.rsplit(".", 1)[-1]: fq}})
    calls = []
    locals_ = {b for i in imports for b in i["binds"]}
    if locals_:
        alt = "|".join(re.escape(x) for x in sorted(locals_))
        for m in re.finditer(r"(?<![\w.])(?:(new)\s+)?(" + alt + r")((?:\s*\.\s*\w+)?)\s*(?:<[^>()]*>)?\s*\(",
                             text):
            name = m.group(2) + re.sub(r"\s+", "", m.group(3) or "")
            calls.append({"name": name, "line": line_of(m.start())})
    return imports, calls


def _lang(f: str) -> str | None:
    if f.endswith(".py"):
        return "python"
    if f.endswith(JS_SUFFIXES):
        return "js"
    if f.endswith(".go"):
        return "go"
    if f.endswith(".java"):
        return "java"
    return None


USES = {"python": py_uses, "js": js_uses, "go": go_uses, "java": java_uses}


def _py_reexport(owner: Member, f: str, mod: str, name: str) -> str | None:
    """``pkg.other.orig`` when module ``mod`` (file ``f``) binds ``name`` by ``from pkg.other import orig [as
    name]`` (relative imports made absolute); None otherwise."""
    tree = owner._trees.get(f, False)
    if tree is False:
        try:
            tree = ast.parse(_read(owner.root / f) or "")
        except (SyntaxError, ValueError):
            tree = None
        owner._trees[f] = tree
    if tree is None:
        return None
    package = mod if PurePosixPath(f).name == "__init__.py" else mod.rpartition(".")[0]
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom):
            continue
        for a in node.names:
            if (a.asname or a.name) != name or a.name == "*":
                continue
            if node.level:
                base = package.split(".") if package else []
                base = base[:len(base) - (node.level - 1)] if node.level > 1 else base
                src = ".".join([*base, *([node.module] if node.module else [])])
            else:
                src = node.module or ""
            return f"{src}.{a.name}" if src else None
    return None


def _js_package_of(spec: str) -> tuple[str, str] | None:
    if spec.startswith((".", "/")) or ":" in spec:
        return None
    parts = spec.split("/")
    if spec.startswith("@"):
        if len(parts) < 2:
            return None
        return "/".join(parts[:2]), "/".join(parts[2:])
    return parts[0], "/".join(parts[1:])


# -- linking ------------------------------------------------------------------------------

class _Linker:
    def __init__(self, members: list[Member]):
        self.members = members
        self.edges: list[dict] = []
        self.ambiguous: list[dict] = []
        self.unresolved: list[dict] = []  # a sample: calls through an import with no definition found
        self.counts: dict[str, int] = defaultdict(int)

    def owners(self, me: Member, lang: str, module: str) -> tuple[list[Member], str | None]:
        """The other members that define ``module`` (empty when ``me`` defines it or none does) and the key
        it is defined under."""
        if lang == "python":
            top = module.split(".")[0]
            if top in SKIP_TOP or top in me.py_own or me.defines_py(module):
                return [], None
            return [m for m in self.members if m is not me and m.defines_py(module)], top
        if lang == "js":
            pk = _js_package_of(module)
            if pk is None or pk[0] in me.js_packages:
                return [], None
            return [m for m in self.members if m is not me and pk[0] in m.js_packages], pk[0]
        if lang == "go":
            def best(m: Member) -> str | None:
                hits = [p for p in m.go_modules if module == p or module.startswith(p + "/")]
                return max(hits, key=len) if hits else None
            if best(me):
                return [], None
            return [m for m in self.members if m is not me and best(m)], module
        if lang == "java":
            if module in me.java_classes:
                return [], None
            return [m for m in self.members if m is not me and module in m.java_classes], module
        return [], None

    def resolve(self, owner: Member, lang: str, module: str, dotted: str, depth: int = 0) -> list[str]:
        """Definitions in ``owner`` the callee ``dotted`` (fully qualified through the import) names."""
        if lang == "python":
            parts = dotted.split(".")
            for i in range(len(parts), 0, -1):
                mod = ".".join(parts[:i])
                f = owner.py_modules.get(mod)
                if f is None:
                    continue
                rest = parts[i:]
                if not rest or len(rest) > 2:
                    return []
                found = owner.in_module(f, rest[0])
                if not found and depth < 6:  # a name the module imports from another of its modules
                    src = _py_reexport(owner, f, mod, rest[0])
                    if src:
                        return self.resolve(owner, lang, module, ".".join([src, *rest[1:]]), depth + 1)
                if len(rest) == 1:
                    return found
                return [v for c in found for v in owner.member_of(c, rest[1])]
            return []
        if lang == "js":
            pk, sub = _js_package_of(module) or ("", "")
            info = owner.js_packages[pk]
            name, _, attr = dotted.partition(".")  # dotted: imported name [. member]
            if name == "*":
                name, attr = attr, ""
            if name in ("default", ""):
                return []
            f = owner.js_subpath(info, sub) if sub else info["entry"]
            found = owner.js_exported(f, name) if f else []
            if attr:
                return [v for c in found for v in owner.member_of(c, attr)]
            return found
        if lang == "go":
            mods = [p for p in owner.go_modules if module == p or module.startswith(p + "/")]
            p = max(mods, key=len)
            d = owner.go_modules[p]
            rest = module[len(p):].strip("/")
            d = "/".join(x for x in (d, rest) if x)
            return [n for n in owner.symbols_under(d, dotted, direct=True)
                    if not str(owner.g.file(n) or "").endswith("_test.go")]
        if lang == "java":
            f = owner.java_classes[module]
            cls_name = module.rsplit(".", 1)[-1]
            classes = owner.top_symbols(f, cls_name)
            parts = dotted.split(".")
            if len(parts) == 1:
                return classes
            return [v for c in classes for v in owner.member_of(c, parts[1])]
        return []

    def link_member(self, me: Member) -> None:
        for f in me.files:
            lang = _lang(f)
            if lang is None:
                continue
            text = _read(me.root / f)
            if not text:
                continue
            imports, calls = USES[lang](text)
            owner_of: dict[int, Member] = {}  # import index -> the one other member defining its module
            for i, imp in enumerate(imports):
                owners, key = self.owners(me, lang, imp["module"])
                if not owners:
                    continue
                at = f"{f}:{imp['line']}"
                self.counts["imports"] += 1
                if len(owners) > 1:
                    self.ambiguous.append({"kind": "module", "member": me.name, "at": at, "language": lang,
                                           "module": imp["module"], "defined_in": [m.name for m in owners]})
                    continue
                owner_of[i] = owners[0]
            if not owner_of:
                continue
            binds: dict[str, int] = {}  # file-wide (JavaScript, Go, Java): local name -> import index
            for i, imp in enumerate(imports):
                for local in imp["binds"]:
                    binds[local] = i
            lines = text.splitlines()
            for c in calls:
                head, _, tail = c["name"].partition(".")
                i = c["import"] if "import" in c else binds.get(head)
                if i is None or i not in owner_of or head not in imports[i]["binds"]:
                    continue
                imp, owner = imports[i], owner_of[i]
                q = imp["binds"][head]
                if lang in ("python", "js"):  # q: the qualified module or symbol / the imported name
                    dotted = f"{q}.{tail}" if tail else q
                elif lang == "go":
                    dotted = tail
                else:  # java: q is the imported class, or for a static import the class's member
                    cls = imp["module"].rsplit(".", 1)[-1]
                    dotted = (cls + (f".{tail}" if tail else "")) if q == imp["module"] else \
                        f"{cls}.{q.rsplit('.', 1)[-1]}"
                if not dotted:
                    continue
                self.counts["calls_through_imports"] += 1
                defs = sorted(set(self.resolve(owner, lang, imp["module"], dotted)))
                at = f"{f}:{c['line']}"
                if not defs:
                    self.counts["unresolved"] += 1
                    if len(self.unresolved) < 50:
                        self.unresolved.append({"member": me.name, "at": at, "callee": c["name"],
                                                "to_member": owner.name, "qualified": dotted})
                    continue
                if len(defs) > 1:
                    self.ambiguous.append({"kind": "definition", "member": me.name, "at": at, "language": lang,
                                           "callee": c["name"], "to_member": owner.name,
                                           "candidates": [f"{owner.bare(d)} {owner.loc(d)}" for d in defs][:10]})
                    continue
                self.edges.append(self._edge(me, f, c, lines, imp, head, owner, defs[0], lang))

    def _edge(self, me: Member, f: str, c: dict, lines: list[str], imp: dict, local: str, owner: Member,
              target: str, lang: str) -> dict:
        from verinoda import callsite

        caller = me.caller_at(f, c["line"])
        call_text = _line(lines, c["line"]).strip()
        imp_text = _line(lines, imp["line"]).strip()
        label = str(owner.g.label(target))
        tok = owner.bare(target)
        names, matched = callsite.line_names_target(me.root / f, call_text, label)
        if not names and re.search(rf"(?<![\w$.]){re.escape(local)}\b", call_text) and \
                re.search(rf"\b{re.escape(tok)}\b", imp_text):
            names, matched = True, local
        dfile, dline = owner.g.file(target), owner.g.line(target)
        dtext = ""
        if dfile and dline:
            dtext = _line((_read(owner.root / dfile) or "").splitlines(), dline).strip()
        def_ok = bool(dtext) and re.search(rf"\b{re.escape(tok)}\b", dtext) is not None
        status = "statically_verified" if names and def_ok else "strong_inference"
        return {"from_member": me.name, "caller": str(me.g.label(caller)) if caller else None,
                "caller_id": caller, "call_site": f"{f}:{c['line']}", "callee": c["name"],
                "to_member": owner.name, "target": label, "target_id": target,
                "definition": f"{dfile}:{dline}" if dfile and dline else dfile, "language": lang,
                "via": f"{f}:{imp['line']}", "status": status,
                "check": {"call_site_names_target": bool(names), "matched_name": matched,
                          "definition_names_target": def_ok, "call_text": call_text[:160],
                          "definition_text": dtext[:160]}}


def link(name: str) -> dict:
    """Compute the group's cross-member links from the members' indexes and write the sidecar."""
    from verinoda.index import write_json_atomic

    row = get(name)
    t0 = time.perf_counter()
    members = [Member(n, p) for n, p in members_of(row)]
    t_load = time.perf_counter() - t0
    lk = _Linker(members)
    for m in members:
        lk.link_member(m)
    def where(e: dict) -> tuple:
        f, _, ln = e["call_site"].rpartition(":")
        return e["from_member"], f, int(ln) if ln.isdigit() else 0, e["to_member"], e["target_id"]

    lk.edges.sort(key=where)
    out = {"version": LINKS_VERSION, "group": name,
           "linked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "members": {m.name: {"path": str(m.root), "graph": m.identity(), "files": len(m.files),
                                "defines": {"python": sorted(m.py_tops), "js": sorted(m.js_packages),
                                            "go": sorted(m.go_modules), "java_classes": len(m.java_classes)}}
                       for m in members},
           "edges": lk.edges[:REPORT_CAP * 20],
           "ambiguous": lk.ambiguous[:REPORT_CAP],
           "unresolved_sample": lk.unresolved,
           "counts": {"edges": len(lk.edges), "statically_verified": sum(e["status"] == "statically_verified"
                                                                          for e in lk.edges),
                      "ambiguous": len(lk.ambiguous), **lk.counts},
           "seconds": {"load": round(t_load, 3), "total": round(time.perf_counter() - t0, 3)}}
    if len(lk.edges) > REPORT_CAP * 20 or len(lk.ambiguous) > REPORT_CAP:
        out["truncated"] = True
    write_json_atomic(links_path(row), out)
    return {**out, "links_path": str(links_path(row))}


def _loaded(name: str) -> tuple[dict, dict]:
    row = get(name)
    side = read_links(row)
    if side is None:
        raise GroupError(f"group {name} has no links yet: run `verinoda group link {name}`")
    return row, side


class _CallSites:
    """Re-reads link call sites: a call-site line whose text is no longer the one the link was made from (the
    calling member's source edited since) makes the link stale even before that member is re-indexed."""

    def __init__(self, row: dict):
        self.roots = dict(members_of(row))
        self.lines: dict[tuple[str, str], list[str]] = {}

    def changed(self, e: dict) -> bool:
        root = self.roots.get(e.get("from_member"))
        want = (e.get("check") or {}).get("call_text")
        f, _, ln = str(e.get("call_site") or "").rpartition(":")
        if root is None or want is None or not ln.isdigit():
            return False
        k = (e["from_member"], f)
        if k not in self.lines:
            self.lines[k] = (_read(root / f) or "").splitlines()
        return _line(self.lines[k], int(ln)).strip()[:160] != want


def show(name: str) -> dict:
    row = get(name)
    side = read_links(row)
    stale = stale_members(row, side) if side else [n for n, _ in members_of(row)]
    out = {"group": name, "members": row["members"], "links_path": str(links_path(row)),
           "linked": side is not None}
    if side:
        sites = _CallSites(row)
        edges = []
        for e in side.get("edges") or []:
            if e["from_member"] in stale or e["to_member"] in stale:
                e = {**e, "stale": True}
            elif sites.changed(e):
                e = {**e, "stale": True, "call_site_changed": True}
            edges.append(e)
        out.update(linked_at=side.get("linked_at"), counts=side.get("counts"), edges=edges,
                   ambiguous=side.get("ambiguous") or [])
        if any(e.get("call_site_changed") for e in edges):
            out["hint"] = f"call sites changed since the links were made: `verinoda group link {name}`"
    if stale:
        out["stale_members"] = stale
        out["hint"] = f"re-index changed members, then `verinoda group link {name}`" if side else \
            f"run `verinoda group link {name}`"
    return out


# -- trace and query ----------------------------------------------------------------------

def _find(members: dict[str, Member], spec: str) -> tuple[str, str]:
    """``member:symbol`` or a symbol one member defines -> (member, node id)."""
    head, sep, rest = spec.partition(":")
    pool = {head: members[head]} if sep and head in members else members
    sym = rest if sep and head in members else spec
    want = sym.strip().split("::")[-1].strip(".()").rsplit(".", 1)[-1]
    hits = []
    for mn, m in pool.items():
        nid, _ = m.g.resolve(sym)
        if nid and (nid == sym or "::" in sym or m.bare(nid) == want):
            hits.append((mn, nid))
    if not hits:
        raise GroupError(f"{spec!r} is not a symbol of " + ("member " + head if len(pool) == 1 else "any member"))
    if len(hits) > 1:
        raise GroupError(f"{spec!r} is in several members: " + ", ".join(
            f"{mn}:{members[mn].g.label(n)} ({members[mn].loc(n)})" for mn, n in hits) + "; write MEMBER:SYMBOL")
    return hits[0]


def trace(name: str, source: str, target: str, *, max_nodes: int = 200_000) -> dict:
    """The shortest call path from ``source`` to ``target`` through the members' call edges and the group's
    links. Each hop carries its call site; a link hop carries its status."""
    from verinoda.index import FLOW_RELATIONS

    row, side = _loaded(name)
    stale = stale_members(row, side)
    members = {n: Member(n, p, augment=True) for n, p in members_of(row)}
    src, dst = _find(members, source), _find(members, target)
    cross: dict[tuple[str, str], list[dict]] = defaultdict(list)
    sites = _CallSites(row)
    for e in side.get("edges") or []:
        if e.get("caller_id"):
            cross[(e["from_member"], e["caller_id"])].append(e)
    prev: dict[tuple[str, str], tuple[tuple[str, str], dict] | None] = {src: None}
    q = deque([src])
    while q and dst not in prev and len(prev) < max_nodes:
        cur = q.popleft()
        mn, nid = cur
        g = members[mn].g
        steps = [((mn, v), {"relation": "calls", "member": mn,
                            "call_site": f"{d.get('source_file')}:{str(d.get('source_location') or '').lstrip('L')}",
                            "status": "extracted"})
                 for v, d in g.out_edges(nid, FLOW_RELATIONS)]
        steps += [((e["to_member"], e["target_id"]), {"relation": "cross_repo_call", "member": mn,
                                                       "call_site": e["call_site"], "definition": e["definition"],
                                                       "status": e["status"],
                                                       "stale": mn in stale or e["to_member"] in stale
                                                       or sites.changed(e)})
                  for e in cross.get(cur, ())]
        for nxt, how in steps:
            if nxt not in prev and nxt[1] in members[nxt[0]].g.G:
                prev[nxt] = (cur, how)
                q.append(nxt)

    def hop(k: tuple[str, str]) -> dict:
        m = members[k[0]]
        return {"member": k[0], "symbol": str(m.g.label(k[1])), "at": m.loc(k[1])}

    out = {"group": name, "source": hop(src), "target": hop(dst)}
    if dst not in prev:
        out.update(status="not_found", next_step=f"no call path in the members' graphs and the group's links; "
                   f"`verinoda group show {name}` lists the links and the ambiguous imports")
    else:
        path, steps = [dst], []
        while prev[path[-1]] is not None:
            p, how = prev[path[-1]]
            steps.append(how)
            path.append(p)
        path.reverse()
        steps.reverse()
        out.update(status="found", path=[hop(k) for k in path], steps=steps,
                   crosses=sum(s["relation"] == "cross_repo_call" for s in steps))
        out["note"] = ("graph call edges are extractions (leads); a cross_repo_call is statically_verified only "
                       "when its call-site and definition lines were re-read and name the callee")
    if stale:
        out["stale_members"] = stale
    return out


def query(name: str, question: str, *, max_items: int = 5) -> dict:
    """The question asked of every member's index; each hit is labelled with its member."""
    from verinoda import index, retrieval

    row = get(name)
    results, per = [], []
    for mn, root in members_of(row):
        try:
            g = index.load(root)
        except FileNotFoundError:
            per.append({"member": mn, "error": "no index", "hint": f"verinoda scan {root}"})
            continue
        res = retrieval.retrieve(g, question, retrieval.Budget(max_items=max_items, max_chars=6000))
        items = [{"member": mn, "symbol": it.get("symbol"), "file": it.get("file"), "lines": it.get("lines"),
                  "score": it.get("score")} for it in res.get("items") or []]
        per.append({"member": mn, "count": len(items)})
        results.extend(items)
    results.sort(key=lambda r: -(r.get("score") or 0))
    return {"group": name, "question": question, "members": per, "results": results,
            "note": "scores come from each member's own index and compare only roughly across members"}


def view(name: str, action: str = "show", *, source: str | None = None, target: str | None = None,
         question: str | None = None, max_items: int = 5, served: set[Path] | None = None) -> dict:
    """The MCP tool's answer (``group_view``): ``show``, ``trace`` or ``query`` of a group whose every member is
    in ``served`` (the projects the server answers for; a server never reads a folder it does not serve).
    ``link`` stays a CLI command: it writes the links file."""
    row = get(name)
    if served is not None:
        roots = {Path(os.path.realpath(str(p))) for p in served}
        outside = [n for n, p in members_of(row) if Path(os.path.realpath(str(p))) not in roots]
        if outside:
            raise GroupError(f"group {name}: member(s) {', '.join(outside)} are not served here; serve every "
                             f"member: `verinoda mcp serve --profile full --projects "
                             f"{','.join(m['name'] for m in row['members'])}`")
    if action == "show":
        return show(name)
    if action == "trace":
        if not source or not target:
            raise GroupError("trace takes source and target (MEMBER:SYMBOL when a name is in several members)")
        return trace(name, source, target)
    if action == "query":
        if not question:
            raise GroupError("query takes question")
        return query(name, question, max_items=max(1, min(int(max_items), 25)))
    raise GroupError(f"action {action!r}: show, trace or query")


# -- text --------------------------------------------------------------------------------

def render_show(r: dict) -> str:
    lines = [f"group {r['group']}: " + ", ".join(f"{m['name']} ({m['path']})" for m in r["members"])]
    if not r.get("linked"):
        lines.append(f"  not linked yet: `verinoda group link {r['group']}`")
        return "\n".join(lines)
    c = r.get("counts") or {}
    lines.append(f"  linked {r.get('linked_at')}: {c.get('edges', 0)} link(s), {c.get('statically_verified', 0)} "
                 f"statically verified, {c.get('ambiguous', 0)} ambiguous; links in {r['links_path']}")
    for e in r.get("edges") or []:
        lines.append(f"  {e['from_member']}:{e['call_site']} {e.get('caller') or '?'} -> {e['to_member']}:"
                     f"{e['definition']} {e['target']}  [{e['status']}]" + ("  STALE" if e.get("stale") else ""))
    for a in r.get("ambiguous") or []:
        if a["kind"] == "module":
            lines.append(f"  ambiguous: {a['member']}:{a['at']} imports {a['module']}, defined in "
                         + ", ".join(a["defined_in"]) + " (no link)")
        else:
            lines.append(f"  ambiguous: {a['member']}:{a['at']} {a['callee']} -> {a['to_member']}: "
                         + "; ".join(a["candidates"]) + " (no link)")
    if r.get("stale_members"):
        lines.append(f"  stale: {', '.join(r['stale_members'])} changed since the links were made; {r['hint']}")
    return "\n".join(lines)


def render_trace(r: dict) -> str:
    if r["status"] != "found":
        return f"no path from {r['source']['member']}:{r['source']['symbol']} to {r['target']['member']}:" \
               f"{r['target']['symbol']}\n  {r.get('next_step', '')}"
    out = [f"{r['source']['member']}:{r['source']['symbol']} -> {r['target']['member']}:{r['target']['symbol']} "
           f"({len(r['steps'])} hop(s), {r['crosses']} across members)"]
    for k, s in zip(r["path"][1:], r["steps"]):
        extra = f" defined at {s['definition']}" if s.get("definition") else ""
        out.append(f"  {s['member']}:{s['call_site']} {s['relation']} -> {k['member']}:{k['symbol']} ({k['at']})"
                   f"{extra} [{s['status']}]" + (" STALE" if s.get("stale") else ""))
    if r.get("stale_members"):
        out.append(f"  stale members: {', '.join(r['stale_members'])}")
    return "\n".join(out)


def render_query(r: dict) -> str:
    out = [f"{r['question']!r} in group {r['group']}:"]
    for it in r["results"]:
        a, b = (it.get("lines") or [None, None])[:2]
        out.append(f"  [{it['member']}] {it['file']}:{a}-{b}  {it['symbol']}")
    for m in r["members"]:
        if m.get("error"):
            out.append(f"  [{m['member']}] {m['error']}: {m['hint']}")
    if not r["results"]:
        out.append("  (nothing found)")
    return "\n".join(out)
