"""Name-existence check for Java code (docs/DESIGN.md D43): the Java half of ``verinoda check``.

AI-written Java imports classes that do not exist, calls methods a class does not have or with a number
of arguments no overload takes, and (in Fabric/Mixin mods) injects into target methods that are not
there - or that exist under another mapping's name (``PlayerEntity`` is Yarn's, ``Player`` Mojang's).
Each *site* gets one verdict, as the Python check gives (:mod:`verinoda.codecheck`):

``exists``   found (in the project's sources, a jar of the classpath, or the JDK);
``absent``   not found where every place it could come from is known (below);
``unknown``  the receiver's type is not known, or a place it could come from is not read (why).

Sites: ``import`` (single-type, static and on-demand imports), ``type`` (a type name in a declaration,
``new``, a cast, ``instanceof``, ``X.class``, a static receiver), ``method`` (a call: its name and its
number of arguments), ``field`` (``expr.name`` on a receiver of known type), ``constructor`` (``new
X(...)``'s arguments) and ``mixin`` (``@Mixin`` targets, ``@Inject``/``@Redirect``/... ``method =``
targets, ``@Shadow``, ``@Accessor``, ``@Invoker``).

Where names come from (:mod:`verinoda.jvmclass`): every Java source file of the project (its packages
are closed: all their sources are read), the jars of the classpath (closed when the classpath is
complete: configured, or the one a Loom build resolved), and the JDK's API for the build's release
(``ct.sym``). A receiver's type is known from a declaration (a local, a parameter, a field, ``this``,
``super``), ``new``, a cast, a string or class literal, and a call or field whose declared type is known
(a method returning a type variable is not). Java's static typing makes that type closed: a method the
type and its super types do not declare does not compile, so it is ``absent`` - when every super type is
known. Generic type arguments, lambdas' parameters, ``var`` from an unknown value, and Kotlin/Groovy
sources are ``unknown``.

Checked: existence and arity. Not checked: argument types, visibility, checked exceptions.
"""
from __future__ import annotations

import os
import re
from collections import ChainMap
from dataclasses import dataclass, field
from pathlib import Path

from verinoda import jvmclass

CHECK_VERSION = "1"
SITE_KINDS = ("import", "type", "method", "field", "constructor", "mixin")
SKIP_DIRS = {".git", ".gradle", ".idea", ".verinoda", "node_modules", "out", "bin", "target", "run", ".vscode"}
MIXIN_INJECTORS = {"Inject", "Redirect", "ModifyArg", "ModifyArgs", "ModifyVariable", "ModifyConstant",
                   "ModifyExpressionValue", "ModifyReturnValue", "WrapOperation", "WrapWithCondition",
                   "WrapMethod", "ModifyReceiver"}
OBJECT = "java/lang/Object"
PRIMITIVES = {"int", "long", "short", "byte", "char", "boolean", "float", "double", "void"}
_PRIM_DESC = {"I": "int", "J": "long", "S": "short", "B": "byte", "C": "char", "Z": "boolean", "F": "float",
              "D": "double", "V": "void"}

_LANG = None


def _parser():
    global _LANG
    import tree_sitter_java as tsj
    from tree_sitter import Language, Parser

    if _LANG is None:
        _LANG = Language(tsj.language())
    return Parser(_LANG)


def _t(node, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", "replace")


# -- the project's own types ------------------------------------------------------------------------

def java_files(repo: Path) -> list[Path]:
    """The project's Java sources: every ``.java`` file outside build output and tool folders. A build's
    output folder is a ``build`` (or ``target``) folder next to a build file; of it only ``generated``
    (annotation processors' sources, compiled with the rest) is read. A package named ``build`` is read."""
    out = []
    outputs: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(repo):
        here = Path(dirpath)
        is_build = any(f in filenames for f in jvmclass.BUILD_FILES)
        keep = []
        for d in dirnames:
            if d in SKIP_DIRS or d.startswith("."):
                continue
            if is_build and d in ("build", "target"):
                if (here / d / "generated").is_dir():
                    outputs.add(str(here / d))
                    keep.append(d)
                continue
            if str(here) in outputs and d != "generated":
                continue
            keep.append(d)
        dirnames[:] = keep
        if str(here) in outputs:
            continue
        out += [here / f for f in filenames if f.endswith(".java")]
    return sorted(out)


def _type_text(node, src: bytes) -> str:
    """A type as written, without type arguments and annotations (``List<Foo>`` -> ``List``, ``a.B[]``
    -> ``a.B[]``)."""
    if node is None:
        return ""
    ty = node.type
    if ty == "generic_type":
        return _type_text(node.named_children[0], src)
    if ty == "array_type":
        return _type_text(node.child_by_field_name("element"), src) + "[]"
    if ty == "annotated_type":
        return _type_text(node.named_children[-1], src)
    if ty == "scoped_type_identifier":
        return ".".join(_type_text(c, src) for c in node.named_children if c.type != "annotation"
                        and c.type != "marker_annotation")
    return _t(node, src)


def _params(node, src: bytes) -> tuple[int, bool, list[tuple[str, str]]]:
    """(count, varargs, [(type text, name)]) of formal_parameters."""
    out, varargs = [], False
    if node is None:
        return 0, False, out
    for p in node.named_children:
        if p.type == "formal_parameter":
            out.append((_type_text(p.child_by_field_name("type"), src), _t(p.child_by_field_name("name"), src)))
        elif p.type == "spread_parameter":
            varargs = True
            ty = next((c for c in p.named_children if c.type not in ("modifiers", "variable_declarator")), None)
            nm = next((c for c in p.named_children if c.type == "variable_declarator"), None)
            name = nm.child_by_field_name("name") if nm is not None else None
            out.append(((_type_text(ty, src) + "[]") if ty is not None else "?",
                        _t(name, src) if name is not None else "?"))
    return len(out), varargs, out


def _typevars(node, src: bytes) -> set[str]:
    tp = node.child_by_field_name("type_parameters") if node is not None else None
    if tp is None:
        return set()
    out = set()
    for c in tp.named_children:
        if c.type == "type_parameter":
            ident = next((x for x in c.named_children if x.type in ("type_identifier", "identifier")), None)
            if ident is not None:
                out.add(_t(ident, src))
    return out


_DECLS = ("class_declaration", "interface_declaration", "enum_declaration", "record_declaration",
          "annotation_type_declaration")


@dataclass
class FileCtx:
    rel: str
    package: str                                   # a/b
    single: dict[str, str] = field(default_factory=dict)          # simple -> dotted as written
    ondemand: list[str] = field(default_factory=list)             # dotted
    static_single: dict[str, list[str]] = field(default_factory=dict)  # member -> [dotted class]
    static_ondemand: list[str] = field(default_factory=list)      # dotted class


def _file_ctx(rel: str, root, src: bytes) -> FileCtx:
    ctx = FileCtx(rel, "")
    for n in root.named_children:
        if n.type == "package_declaration":
            name = next((c for c in n.named_children if c.type in ("scoped_identifier", "identifier")), None)
            if name is not None:
                ctx.package = _t(name, src).replace(".", "/")
        elif n.type == "import_declaration":
            static = any(c.type == "static" for c in n.children)
            star = any(c.type == "asterisk" for c in n.children)
            name = next((c for c in n.named_children if c.type in ("scoped_identifier", "identifier")), None)
            if name is None:
                continue
            dotted = _t(name, src).replace(" ", "")
            if static and star:
                ctx.static_ondemand.append(dotted)
            elif static:
                cls, _, member = dotted.rpartition(".")
                ctx.static_single.setdefault(member, []).append(cls)
            elif star:
                ctx.ondemand.append(dotted)
            else:
                ctx.single[dotted.rsplit(".", 1)[-1]] = dotted
    return ctx


def _project_types(rel: str, src: bytes, tree) -> tuple[FileCtx, dict[str, dict]]:
    """The file's context and its types as ``jvmclass`` class dicts (``origin`` project; super types and
    member types as written, resolved later against the file)."""
    ctx = _file_ctx(rel, tree.root_node, src)
    types: dict[str, dict] = {}
    for n in tree.root_node.named_children:
        if n.type in _DECLS:
            _declare(n, src, ctx, None, set(), types)
    return ctx, types


def _declare(node, src: bytes, ctx: FileCtx, outer: str | None, outer_vars: set[str], types: dict[str, dict],
             binary: str | None = None) -> str | None:
    """Add a type declaration (and its member types) to ``types``; ``binary`` names a local type."""
    rel = ctx.rel

    def add(node, outer, outer_vars, binary=None):
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return None
        simple = _t(name_node, src)
        binary = binary or (f"{outer}${simple}" if outer else (f"{ctx.package}/{simple}" if ctx.package else simple))
        kind = node.type.split("_")[0]
        tv = outer_vars | _typevars(node, src)
        sup = node.child_by_field_name("superclass")
        sup_text = _type_text(sup.named_children[0], src) if sup is not None and sup.named_children else None
        ifaces = []
        for fld in ("interfaces", None):
            iface_node = node.child_by_field_name(fld) if fld else \
                next((c for c in node.named_children if c.type == "extends_interfaces"), None)
            if iface_node is not None:
                lst = next((c for c in iface_node.named_children if c.type == "type_list"), None)
                ifaces += [_type_text(c, src) for c in (lst.named_children if lst is not None else [])]
        cls = {"name": binary, "super_src": sup_text, "ifaces_src": ifaces, "origin": "project", "kind": kind,
               "flags": 0x0200 if kind in ("interface", "annotation") else 0, "methods": {}, "fields": {},
               "inner": {}, "file": rel, "line": node.start_point[0] + 1, "typevars": sorted(tv),
               "has_ctor": False}
        types[binary] = cls
        if outer and outer in types:
            types[outer]["inner"][simple] = binary
        if kind == "record":
            n, _v, ps = _params(node.child_by_field_name("parameters"), src)
            for ty, nm in ps:
                cls["fields"][nm] = [ty, 0]
                cls["methods"].setdefault(nm, []).append([0, 0, ty])
            cls["methods"].setdefault("<init>", []).append([n, 0, "void"])
            cls["has_ctor"] = True
        body = node.child_by_field_name("body")
        members = list(body.named_children) if body is not None else []
        if kind == "enum":
            cls["methods"]["values"] = [[0, 0x0008, None]]
            cls["methods"]["valueOf"] = [[1, 0x0008, simple]]  # the enum, by the name its own scope knows
        if kind == "enum" and body is not None:
            for c in body.named_children:
                if c.type == "enum_constant":
                    cls["fields"][_t(c.child_by_field_name("name"), src)] = [binary, 0x0008]
                elif c.type == "enum_body_declarations":
                    members += list(c.named_children)
        for m in members:
            if m.type == "method_declaration":
                n, varargs, _ps = _params(m.child_by_field_name("parameters"), src)
                mods = next((c for c in m.named_children if c.type == "modifiers"), None)
                static = mods is not None and "static" in _t(mods, src).split()
                ret = _type_text(m.child_by_field_name("type"), src)
                mtv = _typevars(m, src)
                cls["methods"].setdefault(_t(m.child_by_field_name("name"), src), []).append(
                    [n, (0x0008 if static else 0) | (0x0080 if varargs else 0), None if ret in mtv else ret])
            elif m.type in ("constructor_declaration", "compact_constructor_declaration"):
                if m.type == "constructor_declaration":
                    n, varargs, _ps = _params(m.child_by_field_name("parameters"), src)
                    cls["methods"].setdefault("<init>", []).append([n, 0x0080 if varargs else 0, "void"])
                cls["has_ctor"] = True
            elif m.type in ("field_declaration", "constant_declaration"):
                ty = _type_text(m.child_by_field_name("type"), src)
                mods = next((c for c in m.named_children if c.type == "modifiers"), None)
                static = (mods is not None and "static" in _t(mods, src).split()) or kind == "interface"
                for d in m.named_children:
                    if d.type == "variable_declarator":
                        cls["fields"][_t(d.child_by_field_name("name"), src)] = [ty, 0x0008 if static else 0]
            elif m.type == "annotation_type_element_declaration":
                cls["methods"].setdefault(_t(m.child_by_field_name("name"), src), []).append(
                    [0, 0, _type_text(m.child_by_field_name("type"), src)])
            elif m.type in _DECLS:
                add(m, binary, tv if kind != "interface" else set())
        if not cls["has_ctor"] and kind == "class":
            cls["methods"].setdefault("<init>", []).append([0, 0, "void"])
        return binary

    return add(node, outer, outer_vars, binary)


# -- the universe of types ----------------------------------------------------------------------------

class Universe:
    """Every type a check can resolve: the project's (with each file's context), the classpath's, the JDK's."""

    def __init__(self, repo: Path, cp: jvmclass.Classpath, cache_dir: Path | None, root: Path | None = None,
                 overrides: dict[str, bytes] | None = None):
        self.repo, self.cp = repo, cp
        root = root or repo
        overrides = dict(overrides or {})
        self.project: dict[str, dict] = {}
        self.ctx: dict[str, FileCtx] = {}
        self.trees: dict[str, tuple[bytes, object]] = {}
        self.lib: dict[str, dict] = {}
        self.jdk: dict[str, dict] = {}
        parser = _parser()
        paths = java_files(root)
        for rel in overrides:  # a snippet or an unsaved file: checked as if it were there
            p = repo / rel
            if p not in paths and _inside(p, root):
                paths.append(p)
        for p in paths:
            rel = p.relative_to(repo).as_posix()
            try:
                src = overrides[rel] if rel in overrides else p.read_bytes()
            except OSError:
                continue
            tree = parser.parse(src)
            ctx, types = _project_types(rel, src, tree)
            self.ctx[rel] = ctx
            self.trees[rel] = (src, tree)
            self.project.update(types)
        for jar in cp.jars:
            try:
                self.lib.update(jvmclass.load_jar(jar, cache_dir))
            except (OSError, ValueError):
                cp.complete = False
                cp.notes.append(f"unreadable jar: {jar.name}")
        if cp.jdk is not None and cp.release:
            try:
                self.jdk = jvmclass.load_jar(cp.jdk, cache_dir, release=cp.release)
            except (OSError, ValueError):
                self.jdk = {}
        self.packages: dict[str, set[str]] = {}
        for origin, table in (("project", self.project), ("lib", self.lib), ("jdk", self.jdk)):
            for name in table:
                self.packages.setdefault(name.rpartition("/")[0], set()).add(origin)

    def get(self, binary: str | None) -> dict | None:
        if not binary:
            return None
        return self.project.get(binary) or self.lib.get(binary) or self.jdk.get(binary)

    def origin(self, binary: str) -> str:
        return "project" if binary in self.project else "jar" if binary in self.lib else \
            "JDK" if binary in self.jdk else "?"

    def closed_package(self, pkg: str) -> bool:
        """Every class this package can hold is known: the project's sources are all read, the JDK's API is
        read, and the classpath is complete (a package on none of them does not exist then)."""
        origins = self.packages.get(pkg, set())
        if not origins:  # a package nothing read holds: known not to exist only when everything is read
            return self.cp.complete and bool(self.jdk)
        return "lib" not in origins or self.cp.complete

    def lookup_dotted(self, dotted: str) -> str | None:
        """A written type name (``a.b.C.D``) as a binary name: the longest package, then nested classes."""
        parts = dotted.split(".")
        for k in range(len(parts) - 1, -1, -1):
            pkg, chain = "/".join(parts[:k]), parts[k:]
            binary = (pkg + "/" if pkg else "") + "$".join(chain)
            if self.get(binary):
                return binary
        return None

    # -- resolution against a file ------------------------------------------------------------------

    def resolve(self, name: str, ctx: FileCtx, scope: list[str], typevars: set[str]) -> tuple[str | None, str]:
        """``(binary, "")`` or ``(None, "typevar" | "absent: why" | "unknown: why")`` for a type name as
        written in ``ctx``'s file inside the classes ``scope`` (innermost last)."""
        name = name.rstrip("[]").strip()
        while name.endswith("[]"):
            name = name[:-2]
        if not name or name in PRIMITIVES or name == "var":
            return None, "primitive"
        head, _, rest = name.partition(".")
        if head in typevars and not rest:
            return None, "typevar"
        first = self._resolve_simple(head, ctx, scope)
        if first:
            if not rest:
                return first, ""
            binary = first
            for part in rest.split("."):
                nxt = self.member_type(binary, part)
                if nxt is None:
                    return None, (f"absent: no member type {part} in {_src(binary)}" if self.hierarchy_closed(binary)
                                  else f"unknown: {_src(binary)} has super types that are not read")
                binary = nxt
            return binary, ""
        if rest:  # a qualified name: a package path
            got = self.lookup_dotted(name)
            if got:
                return got, ""
            pkg = "/".join(name.split(".")[:-1])
            if self.closed_package(pkg) and pkg in self.packages:
                return None, f"absent: no class {name.rsplit('.', 1)[-1]} in package {pkg.replace('/', '.')}"
            return None, f"unknown: {name} is not in a package that is read"
        return None, self._absent_or_unknown(head, ctx, scope)

    def _resolve_simple(self, name: str, ctx: FileCtx, scope: list[str]) -> str | None:
        for cls in reversed(scope):  # nested types of the enclosing classes and their super types
            got = self.member_type(cls, name)
            if got:
                return got
            if cls.rsplit("/", 1)[-1].split("$")[-1] == name:
                return cls
        if name in ctx.single:
            return self.lookup_dotted(ctx.single[name]) or None
        same = (ctx.package + "/" if ctx.package else "") + name
        if self.get(same):
            return same
        for pkg in ctx.ondemand:
            got = self.lookup_dotted(f"{pkg}.{name}")
            if got:
                return got
        if self.get(f"java/lang/{name}"):
            return f"java/lang/{name}"
        return None

    def _absent_or_unknown(self, name: str, ctx: FileCtx, scope: list[str] = ()) -> str:
        if name in ctx.single:
            return f"unknown: its import {ctx.single[name]} is not found"
        for cls in reversed(scope):  # a member type inherited from a super type that is not read
            missing = self.ancestors(cls)[1] if self.get(cls) else [cls]
            if missing:
                return f"unknown: may be a member type of {_src(missing[0])}, which is not read"
        open_places = [p for p in ctx.ondemand if not self._ondemand_closed(p)]
        if open_places:
            return f"unknown: may come from {open_places[0]}.*, which is not read"
        if not self.jdk:
            return "unknown: the JDK's classes are not read (java.lang)"
        if ctx.package and not self.closed_package(ctx.package):
            return f"unknown: package {ctx.package.replace('/', '.')} is not fully read"
        return f"absent: no class {name} in this file's package, its imports or java.lang"

    def _ondemand_closed(self, dotted: str) -> bool:
        cls = self.lookup_dotted(dotted)
        if cls:
            return self.hierarchy_closed(cls)
        pkg = dotted.replace(".", "/")
        return self.closed_package(pkg)

    # -- members ------------------------------------------------------------------------------------

    def supers(self, binary: str) -> tuple[list[str], list[str]]:
        """(the resolved direct super types, the ones that could not be resolved)."""
        c = self.get(binary)
        if c is None:
            return [], [binary]
        if c.get("origin") != "project":
            sup = [c["super"]] if c.get("super") else ([] if binary == OBJECT else [OBJECT])
            names = sup + list(c.get("ifaces") or [])
            return [n for n in names if self.get(n)], [n for n in names if not self.get(n)]
        cache = c.get("_supers")
        if cache is not None:
            return cache
        ctx = self.ctx.get(c["file"])
        outer = binary.split("$")
        scope = ["$".join(outer[:i]) for i in range(1, len(outer))]
        tv = set(c.get("typevars") or ())
        ok, bad = [], []
        written = ([c["super_src"]] if c.get("super_src") else []) + list(c.get("ifaces_src") or [])
        for w in written:
            got, _why = self.resolve(w, ctx, scope, tv) if ctx else (None, "")
            (ok if got else bad).append(got or w)
        kind = c.get("kind")
        implicit = {"enum": "java/lang/Enum", "record": "java/lang/Record"}.get(kind, OBJECT)
        if not c.get("super_src"):
            (ok if self.get(implicit) else bad).append(implicit)
        c["_supers"] = (ok, bad)
        return ok, bad

    def ancestors(self, binary: str) -> tuple[list[str], list[str]]:
        """The type and all its super types (breadth first), and the super types that are not known."""
        seen, order, missing = {binary}, [binary], []
        i = 0
        while i < len(order):
            ok, bad = self.supers(order[i])
            missing += [b for b in bad if b not in missing]
            for s in ok:
                if s not in seen:
                    seen.add(s)
                    order.append(s)
            i += 1
        return order, missing

    def hierarchy_closed(self, binary: str) -> bool:
        return self.get(binary) is not None and not self.ancestors(binary)[1]

    def member_type(self, binary: str, name: str) -> str | None:
        for a in self.ancestors(binary)[0]:
            c = self.get(a)
            got = (c or {}).get("inner", {}).get(name)
            if got:
                return got
            if self.get(f"{a}${name}"):
                return f"{a}${name}"
        return None

    def methods(self, binary: str, name: str) -> list[tuple[str, list]]:
        out = []
        for a in self.ancestors(binary)[0]:
            for ov in (self.get(a) or {}).get("methods", {}).get(name, ()):
                out.append((a, ov))
        return out

    def field_of(self, binary: str, name: str) -> tuple[str, list] | None:
        for a in self.ancestors(binary)[0]:
            f = (self.get(a) or {}).get("fields", {}).get(name)
            if f is not None:
                return a, f
        return None

    def type_of_written(self, written: str | None, owner: str) -> str | None:
        """A member's declared type (as a binary name, or None): a jar's erased descriptor type, or a
        project member's written type resolved in its file."""
        if not written or written in PRIMITIVES or written in _PRIM_DESC:
            return None
        c = self.get(owner)
        if c is None or c.get("origin") != "project":
            return None if written.startswith("[") else written
        if written.endswith("[]"):
            return None
        ctx = self.ctx.get(c["file"])
        outer = owner.split("$")
        scope = ["$".join(outer[:i]) for i in range(1, len(outer) + 1)]
        got, _ = self.resolve(written, ctx, scope, set(c.get("typevars") or ())) if ctx else (None, "")
        return got


def _short(text: str, limit: int = 60) -> str:
    text = re.sub(r"\s+", " ", text)
    return text if len(text) <= limit else "..." + text[-(limit - 3):]


def _src(binary: str) -> str:
    return binary.replace("/", ".").replace("$", ".")


def _arity_ok(ov: list, n: int) -> bool:
    npar, flags = ov[0], ov[1]
    return npar == n or (flags & 0x0080 and n >= npar - 1)


# -- checking one file ------------------------------------------------------------------------------

class _FileCheck:
    def __init__(self, u: Universe, rel: str, lines: set[int] | None):
        self.u, self.rel, self.lines = u, rel, lines
        self.src, self.tree = u.trees[rel]
        self.ctx = u.ctx[rel]
        self.sites: list[dict] = []
        self.local_types: list[dict[str, str]] = [{}]

    def res(self, written: str, scope, tv) -> tuple[str | None, str]:
        """:meth:`Universe.resolve`, local types (declared in a method body) first."""
        head, _, rest = written.rstrip("[]").partition(".")
        for frame in reversed(self.local_types):
            if head in frame:
                if not rest:
                    return frame[head], ""
                binary = frame[head]
                for part in rest.split("."):
                    binary = self.u.member_type(binary, part) or ""
                return (binary or None), ("" if binary else "unknown: a member type of a local type")
        return self.u.resolve(written, self.ctx, scope, tv)

    def site(self, node, kind: str, expr: str, name: str, verdict: str, why: str = "", where: str = "",
             nearest: list[str] | None = None) -> None:
        line, col = node.start_point[0] + 1, node.start_point[1] + 1
        if self.lines is not None and line not in self.lines:
            return
        s = {"at": f"{self.rel}:{line}:{col}", "path": self.rel, "line": line, "col": col, "kind": kind,
             "expr": expr[:120], "name": name, "verdict": verdict, "language": "Java"}
        if why:
            s["why"] = why
        if where:
            s["where"] = where
        if nearest:
            s["nearest"] = [{"name": n} for n in nearest]
        self.sites.append(s)

    def verdict_of(self, why: str) -> tuple[str, str]:
        v, _, w = why.partition(": ")
        return (v, w) if v in ("absent", "unknown") else ("unknown", why)

    # -- the walk -----------------------------------------------------------------------------------

    def run(self) -> list[dict]:
        root = self.tree.root_node
        for n in root.named_children:
            if n.type == "import_declaration":
                self.check_import(n)
        for n in root.named_children:
            if n.type in _DECLS:
                self.walk_class(n, [], set())
        return self.sites

    def check_import(self, n) -> None:
        static = any(c.type == "static" for c in n.children)
        star = any(c.type == "asterisk" for c in n.children)
        name = next((c for c in n.named_children if c.type in ("scoped_identifier", "identifier")), None)
        if name is None:
            return
        dotted = _t(name, self.src).replace(" ", "")
        u = self.u
        if static:
            cls_dotted, _, member = dotted.rpartition(".") if not star else (dotted, "", "")
            cls = u.lookup_dotted(cls_dotted)
            if cls is None:
                self._missing_class(name, "import", dotted, cls_dotted)
                return
            if star:
                self.site(name, "import", dotted, cls_dotted.rsplit(".", 1)[-1], "exists", where=u.origin(cls))
                return
            found = u.methods(cls, member) or u.field_of(cls, member) or u.member_type(cls, member)
            if found:
                self.site(name, "import", dotted, member, "exists", where=u.origin(cls))
            elif u.hierarchy_closed(cls):
                self.site(name, "import", dotted, member, "absent",
                          why=f"{_src(cls)} and its super types declare no member {member}",
                          nearest=_nearest(member, self._member_names(cls)))
            else:
                self.site(name, "import", dotted, member, "unknown", why=f"{_src(cls)} has super types that are not read")
            return
        if star:
            if u.lookup_dotted(dotted) or dotted.replace(".", "/") in u.packages:
                self.site(name, "import", dotted + ".*", dotted, "exists")
            elif u.closed_package(dotted.replace(".", "/")) and u.cp.complete:
                self.site(name, "import", dotted + ".*", dotted, "absent",
                          why=f"no package or class {dotted} in the project, the classpath or the JDK")
            else:
                self.site(name, "import", dotted + ".*", dotted, "unknown", why="the classpath is not complete")
            return
        binary = u.lookup_dotted(dotted)
        if binary:
            self.site(name, "import", dotted, dotted.rsplit(".", 1)[-1], "exists", where=u.origin(binary))
        else:
            self._missing_class(name, "import", dotted, dotted)

    def _missing_class(self, node, kind: str, expr: str, dotted: str) -> None:
        u = self.u
        parts = dotted.split(".")
        simple = parts[-1]
        pkg = None
        for k in range(len(parts) - 1, 0, -1):
            cand = "/".join(parts[:k])
            if cand in u.packages:
                pkg = cand
                break
            outer = u.lookup_dotted(".".join(parts[:k]))
            if outer:
                if u.hierarchy_closed(outer):
                    self.site(node, kind, expr, simple, "absent", why=f"{_src(outer)} has no member type {simple}",
                              nearest=_nearest(simple, list((u.get(outer) or {}).get("inner", {}))))
                else:
                    self.site(node, kind, expr, simple, "unknown", why=f"{_src(outer)} has super types that are not read")
                return
        whole_pkg = "/".join(parts[:-1])
        if pkg is not None and pkg == whole_pkg and u.closed_package(pkg):
            names = [n.rsplit("/", 1)[-1] for n in _package_classes(u, pkg)]
            self.site(node, kind, expr, simple, "absent",
                      why=f"no class {simple} in package {pkg.replace('/', '.')} ({_where_pkg(u, pkg)})",
                      nearest=_nearest(simple, names))
        else:
            self.site(node, kind, expr, simple, "unknown",
                      why="the classpath is not complete (" + (u.cp.notes[0] if u.cp.notes else u.cp.source) + ")")

    def _member_names(self, binary: str) -> list[str]:
        names = set()
        for a in self.u.ancestors(binary)[0]:
            c = self.u.get(a) or {}
            names |= set(c.get("methods", {})) | set(c.get("fields", {}))
        return sorted(n for n in names if not n.startswith("<"))

    # classes, members, bodies

    def walk_class(self, node, scope: list[str], typevars: set[str], binary: str | None = None) -> None:
        name_node = node.child_by_field_name("name")
        if binary is None:
            if name_node is None:
                return
            simple = _t(name_node, self.src)
            binary = f"{scope[-1]}${simple}" if scope else \
                ((self.ctx.package + "/" if self.ctx.package else "") + simple)
        scope = scope + [binary]
        c = self.u.get(binary) or {}
        tv = typevars | set(c.get("typevars") or ())
        for fld in ("superclass", "interfaces"):
            part = node.child_by_field_name(fld)
            if part is not None:
                self.check_types_in(part, scope[:-1] if fld == "superclass" else scope, tv)
        ext = next((ch for ch in node.named_children if ch.type == "extends_interfaces"), None)
        if ext is not None:
            self.check_types_in(ext, scope, tv)
        mods = next((ch for ch in node.named_children if ch.type == "modifiers"), None)
        mixin_targets = self.mixin_targets(mods, scope, tv) if mods is not None else None
        body = node.child_by_field_name("body")
        if body is None:
            return
        members = list(body.named_children)
        if node.type == "enum_declaration":
            for ch in body.named_children:
                if ch.type == "enum_body_declarations":
                    members += list(ch.named_children)
                elif ch.type == "enum_constant":
                    args = ch.child_by_field_name("arguments")
                    if args is not None:
                        self.walk_expr(args, ChainMap(), scope, tv)
                    cb = ch.child_by_field_name("body")
                    if cb is not None:
                        self.walk_class_body(cb, scope, tv, binary)
        if node.type == "record_declaration":
            self.check_types_in(node.child_by_field_name("parameters"), scope, tv)
        for m in members:
            self.walk_member(m, scope, tv, mixin_targets)

    def walk_class_body(self, body, scope: list[str], tv: set[str], anon_super: str | None) -> None:
        """An anonymous class body: its members see the super type's members."""
        fake = f"{scope[-1]}$anon{body.start_byte}" if scope else f"anon{body.start_byte}"
        self.u.project.setdefault(fake, {"name": fake, "super_src": None, "ifaces_src": [], "origin": "project",
                                         "kind": "class", "flags": 0, "methods": {}, "fields": {}, "inner": {},
                                         "file": self.rel, "typevars": [], "_supers": ([anon_super], [])
                                         if anon_super else ([], ["?"])})
        for m in body.named_children:
            if m.type == "method_declaration":
                n, varargs, _ps = _params(m.child_by_field_name("parameters"), self.src)
                self.u.project[fake]["methods"].setdefault(_t(m.child_by_field_name("name"), self.src), []).append(
                    [n, 0x0080 if varargs else 0, None])
        for m in body.named_children:
            self.walk_member(m, scope + [fake], tv, None)

    def walk_member(self, m, scope: list[str], tv: set[str], mixin_targets) -> None:
        if m.type in _DECLS:
            self.walk_class(m, scope, tv if m.type != "interface_declaration" else set())
            return
        if m.type in ("method_declaration", "constructor_declaration", "compact_constructor_declaration"):
            mtv = tv | _typevars(m, self.src)
            self.check_types_in(m.child_by_field_name("type"), scope, mtv)
            env = ChainMap({})
            params = m.child_by_field_name("parameters")
            if params is not None:
                for p in params.named_children:
                    if p.type in ("formal_parameter", "spread_parameter"):
                        self.declare_param(p, env, scope, mtv)
            if m.type == "compact_constructor_declaration":
                c = self.u.get(scope[-1]) or {}
                for fname, (fty, _f) in c.get("fields", {}).items():
                    env[fname] = self.u.type_of_written(fty, scope[-1])
            mods = next((c for c in m.named_children if c.type == "modifiers"), None)
            if mods is not None:
                self.check_annotations(mods, scope, mtv, mixin_targets, m)
            body = m.child_by_field_name("body")
            if body is not None:
                self.walk_block(body, env, scope, mtv)
        elif m.type in ("field_declaration", "constant_declaration"):
            self.check_types_in(m.child_by_field_name("type"), scope, tv)
            mods = next((c for c in m.named_children if c.type == "modifiers"), None)
            if mods is not None:
                self.check_annotations(mods, scope, tv, mixin_targets, m)
            for d in m.named_children:
                if d.type == "variable_declarator":
                    v = d.child_by_field_name("value")
                    if v is not None:
                        self.walk_expr(v, ChainMap({}), scope, tv)
        elif m.type in ("static_initializer", "block"):
            blk = m if m.type == "block" else next((c for c in m.named_children if c.type == "block"), None)
            if blk is not None:
                self.walk_block(blk, ChainMap({}), scope, tv)

    def declare_param(self, p, env, scope, tv) -> None:
        ty = p.child_by_field_name("type")
        if p.type == "spread_parameter":
            ty = next((c for c in p.named_children if c.type not in ("modifiers", "variable_declarator")), None)
            d = next((c for c in p.named_children if c.type == "variable_declarator"), None)
            name = d.child_by_field_name("name") if d is not None else None
            self.check_types_in(ty, scope, tv)
            if name is not None:
                env[_t(name, self.src)] = None  # an array
            return
        name = p.child_by_field_name("name")
        self.check_types_in(ty, scope, tv)
        if name is not None:
            written = _type_text(ty, self.src)
            env[_t(name, self.src)] = None if written.endswith("[]") else self.res(written, scope, tv)[0]

    # statements

    def walk_block(self, node, env: ChainMap, scope, tv) -> None:
        env = env.new_child()
        self.local_types.append({})
        try:
            for st in node.named_children:
                self.walk_stmt(st, env, scope, tv)
        finally:
            self.local_types.pop()

    def walk_stmt(self, st, env: ChainMap, scope, tv) -> None:
        ty = st.type
        if ty == "block":
            self.walk_block(st, env, scope, tv)
        elif ty == "local_variable_declaration":
            tnode = st.child_by_field_name("type")
            written = _type_text(tnode, self.src)
            if written != "var":
                self.check_types_in(tnode, scope, tv)
            declared = None if written.endswith("[]") or written == "var" else \
                self.res(written, scope, tv)[0]
            for d in st.named_children:
                if d.type == "variable_declarator":
                    v = d.child_by_field_name("value")
                    got = self.walk_expr(v, env, scope, tv) if v is not None else None
                    env[_t(d.child_by_field_name("name"), self.src)] = got if written == "var" else declared
        elif ty == "local_class_declaration" or ty in _DECLS:
            decl = st if ty in _DECLS else st.named_children[-1]
            name = decl.child_by_field_name("name")
            if name is not None and scope:
                binary = f"{scope[-1]}${_t(name, self.src)}#{decl.start_byte}"
                _declare(decl, self.src, self.ctx, None, tv, self.u.project, binary)
                self.local_types[-1][_t(name, self.src)] = binary
                self.walk_class(decl, scope, tv, binary)
        elif ty == "enhanced_for_statement":
            inner = env.new_child()
            tnode = st.child_by_field_name("type")
            written = _type_text(tnode, self.src)
            if written != "var":
                self.check_types_in(tnode, scope, tv)
            self.walk_expr(st.child_by_field_name("value"), env, scope, tv)
            name = st.child_by_field_name("name")
            if name is not None:
                inner[_t(name, self.src)] = None if written in ("var",) or written.endswith("[]") else \
                    self.res(written, scope, tv)[0]
            body = st.child_by_field_name("body")
            if body is not None:
                self.walk_stmt(body, inner, scope, tv)
        elif ty == "try_with_resources_statement" or ty == "try_statement":
            inner = env.new_child()
            for ch in st.named_children:
                if ch.type == "resource_specification":
                    for r in ch.named_children:
                        if r.type == "resource":
                            tnode, name, val = r.child_by_field_name("type"), r.child_by_field_name("name"), \
                                r.child_by_field_name("value")
                            got = self.walk_expr(val, inner, scope, tv) if val is not None else None
                            if tnode is not None and _type_text(tnode, self.src) != "var":
                                self.check_types_in(tnode, scope, tv)
                                got = self.res(_type_text(tnode, self.src), scope, tv)[0]
                            if name is not None:
                                inner[_t(name, self.src)] = got
                            if tnode is None and name is None:
                                self.walk_expr(r, inner, scope, tv)
                elif ch.type == "catch_clause":
                    cenv = inner.new_child()
                    for cc in ch.named_children:
                        if cc.type == "catch_formal_parameter":
                            ctype = next((x for x in cc.named_children if x.type == "catch_type"), None)
                            if ctype is not None:
                                self.check_types_in(ctype, scope, tv)
                            nm = cc.child_by_field_name("name")
                            if nm is not None:
                                first = next((x for x in (ctype.named_children if ctype else [])), None)
                                cenv[_t(nm, self.src)] = self.res(_type_text(first, self.src), scope, tv)[0] if first is not None and \
                                    len(ctype.named_children) == 1 else None
                        elif cc.type == "block":
                            self.walk_block(cc, cenv, scope, tv)
                elif ch.type == "block":
                    self.walk_block(ch, inner, scope, tv)
                elif ch.type == "finally_clause":
                    for b in ch.named_children:
                        if b.type == "block":
                            self.walk_block(b, inner, scope, tv)
        else:
            for ch in st.named_children:
                if ch.type in ("block",):
                    self.walk_block(ch, env, scope, tv)
                elif ch.type.endswith("_statement") or ch.type in ("local_variable_declaration", "switch_block",
                                                                     "switch_block_statement_group", "switch_rule"):
                    self.walk_stmt(ch, env, scope, tv)
                else:
                    self.walk_expr(ch, env, scope, tv)

    # expressions: walked for their sites; the value is the expression's type when it is known

    def walk_expr(self, n, env: ChainMap, scope, tv) -> str | None:
        if n is None:
            return None
        ty = n.type
        if ty == "method_invocation":
            return self.check_call(n, env, scope, tv)
        if ty == "object_creation_expression":
            return self.check_new(n, env, scope, tv)
        if ty == "field_access":
            return self.check_field_access(n, env, scope, tv)
        if ty == "identifier":
            name = _t(n, self.src)
            if name in env:
                return env[name]
            return self.field_type_in_scope(name, scope)
        if ty == "this":
            return scope[-1] if scope else None
        if ty in ("string_literal", "text_block"):
            return "java/lang/String"
        if ty == "class_literal":
            self.check_types_in(n.named_children[0] if n.named_children else None, scope, tv)
            return "java/lang/Class"
        if ty == "cast_expression":
            tnode = n.child_by_field_name("type")
            self.check_types_in(tnode, scope, tv)
            self.walk_expr(n.child_by_field_name("value"), env, scope, tv)
            written = _type_text(tnode, self.src)
            return None if written.endswith("[]") else self.res(written, scope, tv)[0]
        if ty == "parenthesized_expression":
            return self.walk_expr(n.named_children[0] if n.named_children else None, env, scope, tv)
        if ty == "instanceof_expression":
            self.walk_expr(n.child_by_field_name("left"), env, scope, tv)
            right = n.child_by_field_name("right")
            pattern = n.child_by_field_name("pattern")
            name = n.child_by_field_name("name")
            if right is not None:
                self.check_types_in(right, scope, tv)
                if name is not None:
                    env[_t(name, self.src)] = self.res(_type_text(right, self.src), scope, tv)[0]
            if pattern is not None:
                self.walk_pattern(pattern, env, scope, tv)
            return None
        if ty == "lambda_expression":
            inner = env.new_child()
            params = n.child_by_field_name("parameters")
            if params is not None:
                for p in params.named_children:
                    if p.type == "identifier":
                        inner[_t(p, self.src)] = None
                    elif p.type in ("formal_parameter", "spread_parameter"):
                        self.declare_param(p, inner, scope, tv)
                    elif p.type == "inferred_parameters":
                        for q in p.named_children:
                            inner[_t(q, self.src)] = None
                if params.type == "identifier":
                    inner[_t(params, self.src)] = None
            body = n.child_by_field_name("body")
            if body is not None and body.type == "block":
                self.walk_block(body, inner, scope, tv)
            elif body is not None:
                self.walk_expr(body, inner, scope, tv)
            return None
        if ty == "method_reference":
            first = n.named_children[0] if n.named_children else None
            if first is not None and first.type in ("type_identifier", "scoped_type_identifier", "generic_type"):
                self.check_types_in(first, scope, tv)
            elif first is not None:
                self.walk_expr(first, env, scope, tv)
            return None
        if ty in ("array_creation_expression",):
            self.check_types_in(n.child_by_field_name("type"), scope, tv)
            for ch in n.named_children:
                if ch.type in ("dimensions_expr", "array_initializer"):
                    self.walk_expr(ch, env, scope, tv)
            return None
        if ty in ("switch_expression", "switch_block", "switch_rule", "switch_block_statement_group"):
            for ch in n.named_children:
                if ch.type == "block":
                    self.walk_block(ch, env, scope, tv)
                elif ch.type.endswith("_statement"):
                    self.walk_stmt(ch, env, scope, tv)
                elif ch.type == "switch_label":
                    for lab in ch.named_children:
                        if lab.type in ("type_pattern", "record_pattern", "pattern"):
                            self.walk_pattern(lab, env, scope, tv)
                        else:
                            self.walk_expr(lab, env, scope, tv)
                else:
                    self.walk_expr(ch, env, scope, tv)
            return None
        if ty in ("block",):
            self.walk_block(n, env, scope, tv)
            return None
        if ty in ("type_identifier", "scoped_type_identifier", "generic_type"):
            self.check_types_in(n, scope, tv)
            return None
        for ch in n.named_children:
            self.walk_expr(ch, env, scope, tv)
        return None

    def walk_pattern(self, p, env, scope, tv) -> None:
        if p.type == "type_pattern":
            tnode = next((c for c in p.named_children if c.type not in ("identifier", "modifiers")), None)
            name = next((c for c in p.named_children if c.type == "identifier"), None)
            if tnode is not None:
                self.check_types_in(tnode, scope, tv)
            if name is not None:
                env[_t(name, self.src)] = self.res(_type_text(tnode, self.src), scope, tv)[0] \
                    if tnode is not None else None
            return
        for ch in p.named_children:
            if ch.type in ("type_pattern", "record_pattern", "record_pattern_body", "record_pattern_component",
                           "pattern"):
                self.walk_pattern(ch, env, scope, tv)
            elif ch.type in ("type_identifier", "scoped_type_identifier", "generic_type"):
                self.check_types_in(ch, scope, tv)
            elif ch.type == "identifier":
                env[_t(ch, self.src)] = None

    def field_type_in_scope(self, name: str, scope: list[str]) -> str | None:
        for cls in reversed(scope):
            f = self.u.field_of(cls, name)
            if f is not None:
                return self.u.type_of_written(f[1][0], f[0])
        for cls_dotted in self.ctx.static_single.get(name, ()):
            cls = self.u.lookup_dotted(cls_dotted)
            f = self.u.field_of(cls, name) if cls else None
            if f is not None:
                return self.u.type_of_written(f[1][0], f[0])
        return None

    # types written in code

    def check_types_in(self, node, scope, tv) -> None:
        if node is None:
            return
        for tnode in _type_nodes(node):
            written = _type_text(tnode, self.src)
            if not written or written.rstrip("[]") in PRIMITIVES or written == "var":
                continue
            got, why = self.res(written, scope, tv)
            if got:
                self.site(tnode, "type", written, written.rsplit(".", 1)[-1], "exists", where=self.u.origin(got))
            elif why and why not in ("typevar", "primitive"):
                v, w = self.verdict_of(why)
                near = _nearest(written.rsplit(".", 1)[-1], self._visible_type_names(scope)) if v == "absent" else None
                self.site(tnode, "type", written, written.rsplit(".", 1)[-1], v, why=w, nearest=near)

    def _visible_type_names(self, scope) -> list[str]:
        names = set(self.ctx.single)
        pkg = self.ctx.package
        names |= {n.rsplit("/", 1)[-1] for n in _package_classes(self.u, pkg)}
        for p in self.ctx.ondemand:
            names |= {n.rsplit("/", 1)[-1] for n in _package_classes(self.u, p.replace(".", "/"))}
        return sorted(n for n in names if "$" not in n)

    # calls

    def receiver(self, obj, env, scope, tv) -> tuple[str, str | None, str]:
        """``(kind, binary, why)`` of a call's or field access's object: ``value`` (an expression of that
        type), ``type`` (a type used as a static qualifier), ``super``, or ``unknown``."""
        if obj.type == "super":
            sups = self.u.supers(scope[-1])[0] if scope else []
            return ("super", sups[0], "") if sups else ("unknown", None, "super type not read")
        if obj.type == "identifier":
            name = _t(obj, self.src)
            if name in env:
                return ("value", env[name], "") if env[name] else ("unknown", None, f"the type of {name} is not known")
            ftype = self.field_type_in_scope(name, scope)
            if ftype:
                return "value", ftype, ""
            if any(self.u.field_of(c, name) for c in scope):
                return "unknown", None, f"the type of field {name} is not known"
            got, why = self.res(name, scope, tv)
            if got:
                self.site(obj, "type", name, name, "exists", where=self.u.origin(got))
                return "type", got, ""
            if why.startswith("absent") and name[:1].isupper():
                v, w = self.verdict_of(why)
                self.site(obj, "type", name, name, v, why=w)
            return "unknown", None, f"{name} is not a known variable, field or type"
        if obj.type in ("field_access", "scoped_identifier") and _all_identifiers(obj):
            dotted = _t(obj, self.src).replace(" ", "")
            head = dotted.split(".")[0]
            if head not in env and not self.field_type_in_scope(head, scope) and \
                    not self.u._resolve_simple(head, self.ctx, scope):
                got = self.u.lookup_dotted(dotted)
                if got:
                    return "type", got, ""
                if head[:1].islower():
                    return "unknown", None, f"{dotted} is not a known variable or type"
        t = self.walk_expr(obj, env, scope, tv)
        if t:
            return "value", t, ""
        return "unknown", None, "the receiver's type is not known"

    def check_call(self, n, env, scope, tv) -> str | None:
        u = self.u
        name_node = n.child_by_field_name("name")
        name = _t(name_node, self.src)
        args = n.child_by_field_name("arguments")
        nargs = _count_args(args)
        if args is not None:
            for a in args.named_children:
                self.walk_expr(a, env, scope, tv)
        obj = n.child_by_field_name("object")
        expr = ((_short(_t(obj, self.src)) + ".") if obj is not None else "") + name + "(...)"
        if obj is None:
            for cls in reversed(scope):
                ovs = u.methods(cls, name)
                if ovs:
                    return self._judge(name_node, expr, name, nargs, ovs, cls)
            for cls_dotted in self.ctx.static_single.get(name, ()):
                cls = u.lookup_dotted(cls_dotted)
                if cls and u.methods(cls, name):
                    return self._judge(name_node, expr, name, nargs, u.methods(cls, name), cls)
                if not cls or not u.hierarchy_closed(cls):
                    self.site(name_node, "method", expr, name, "unknown",
                              why=f"statically imported from {cls_dotted}, which is not read")
                    return None
            open_ = [c for c in scope if not u.hierarchy_closed(c)]
            open_static = [s for s in self.ctx.static_ondemand if not (u.lookup_dotted(s) and
                                                                       u.hierarchy_closed(u.lookup_dotted(s)))]
            for s in self.ctx.static_ondemand:
                cls = u.lookup_dotted(s)
                if cls and u.methods(cls, name):
                    return self._judge(name_node, expr, name, nargs, u.methods(cls, name), cls)
            if open_ or open_static:
                self.site(name_node, "method", expr, name, "unknown",
                          why=f"{_src(open_[0]) if open_ else open_static[0]} has super types that are not read")
            else:
                self.site(name_node, "method", expr, name, "absent",
                          why=f"no method {name} in {_src(scope[-1]) if scope else 'this file'}, its enclosing "
                              "classes, their super types or the static imports",
                          nearest=_nearest(name, self._member_names(scope[-1])) if scope else None)
            return None
        _kind, binary, why = self.receiver(obj, env, scope, tv)
        if binary is None:
            self.site(name_node, "method", expr, name, "unknown", why=why)
            return None
        ovs = u.methods(binary, name)
        if not ovs and u.get(binary) and u.get(binary).get("flags", 0) & 0x0200:
            ovs = u.methods(OBJECT, name)  # an interface's values are Objects
        if ovs:
            return self._judge(name_node, expr, name, nargs, ovs, binary)
        if u.hierarchy_closed(binary):
            self.site(name_node, "method", expr, name, "absent",
                      why=f"{_src(binary)} and its super types declare no method {name}",
                      where=u.origin(binary), nearest=_nearest(name, self._member_names(binary)))
        else:
            missing = u.ancestors(binary)[1]
            self.site(name_node, "method", expr, name, "unknown",
                      why=f"{_src(binary)} has super types that are not read ({_src(missing[0])})"
                      if missing else f"{_src(binary)} is not read")
        return None

    def _judge(self, node, expr: str, name: str, nargs: int, ovs: list, binary: str) -> str | None:
        fits = [(owner, ov) for owner, ov in ovs if _arity_ok(ov, nargs)]
        if fits:
            owner = fits[0][0]
            self.site(node, "method", expr, name, "exists", where=f"{_src(owner)} ({self.u.origin(owner)})")
            rets = {self.u.type_of_written(o[2], w) if o[2] is not None else None for w, o in fits}
            return rets.pop() if len(rets) == 1 else None
        if self.u.hierarchy_closed(binary):
            ar = sorted({ov[0] for _o, ov in ovs})
            self.site(node, "method", expr, name, "absent",
                      why=f"no {name} with {nargs} argument{'s' if nargs != 1 else ''} in {_src(binary)} or its super "
                          f"types: the overloads take {', '.join(map(str, ar))}")
        else:
            self.site(node, "method", expr, name, "unknown",
                      why=f"no {name} with {nargs} arguments found, and {_src(binary)} has super types that are not read")
        return None

    def check_new(self, n, env, scope, tv) -> str | None:
        u = self.u
        tnode = n.child_by_field_name("type")
        args = n.child_by_field_name("arguments")
        nargs = _count_args(args)
        if args is not None:
            for a in args.named_children:
                self.walk_expr(a, env, scope, tv)
        outer = n.named_children[0] if n.named_children and n.named_children[0] not in (tnode, args) else None
        if outer is not None and outer.type not in ("type_arguments",):
            self.walk_expr(outer, env, scope, tv)
            return None  # outer.new Inner(): the qualified inner class creation is not typed here
        self.check_types_in(tnode, scope, tv)
        written = _type_text(tnode, self.src)
        binary, _ = self.res(written, scope, tv)
        body = n.child_by_field_name("body") or next((c for c in n.named_children if c.type == "class_body"), None)
        if body is not None:
            self.walk_class_body(body, scope, tv, binary)
            return binary
        c = u.get(binary) if binary else None
        if c is None or "$" in binary or c.get("flags", 0) & 0x0200:
            return binary
        ctors = c.get("methods", {}).get("<init>", [])
        if any(_arity_ok(ov, nargs) for ov in ctors):
            self.site(tnode, "constructor", f"new {written}(...)", written, "exists", where=u.origin(binary))
        elif ctors:
            self.site(tnode, "constructor", f"new {written}(...)", written, "absent",
                      why=f"no constructor of {_src(binary)} takes {nargs} argument{'s' if nargs != 1 else ''}: they take "
                          + ", ".join(str(a) for a in sorted({ov[0] for ov in ctors})))
        return binary

    def check_field_access(self, n, env, scope, tv) -> str | None:
        u = self.u
        obj, fld = n.child_by_field_name("object"), n.child_by_field_name("field")
        name = _t(fld, self.src) if fld is not None else ""
        if name in ("this", "super", "class", "") or fld is None or fld.type != "identifier":
            if obj is not None:
                self.walk_expr(obj, env, scope, tv)
            return scope[-1] if name == "this" and scope else None
        if obj is not None and _all_identifiers(n):  # a.b.C: maybe a type
            dotted = _t(n, self.src).replace(" ", "")
            head = dotted.split(".")[0]
            if head not in env and not self.field_type_in_scope(head, scope) and \
                    not u._resolve_simple(head, self.ctx, scope):
                got = u.lookup_dotted(dotted)
                if got:
                    return None
        _kind, binary, _why = self.receiver(obj, env, scope, tv)
        if binary is None:
            return None
        if name == "length" and binary.startswith("["):
            return None
        f = u.field_of(binary, name)
        if f is not None:
            self.site(fld, "field", _t(n, self.src)[-80:], name, "exists", where=f"{_src(f[0])} ({u.origin(f[0])})")
            return u.type_of_written(f[1][0], f[0])
        inner = u.member_type(binary, name)
        if inner:
            return None
        if u.hierarchy_closed(binary):
            self.site(fld, "field", _t(n, self.src)[-80:], name, "absent",
                      why=f"{_src(binary)} and its super types declare no field {name}",
                      nearest=_nearest(name, self._member_names(binary)))
        else:
            self.site(fld, "field", _t(n, self.src)[-80:], name, "unknown", why=f"{_src(binary)} has super types that are not read")
        return None

    # annotations: Mixin

    def check_annotations(self, mods, scope, tv, mixin_targets, member) -> None:
        for a in mods.named_children:
            if a.type not in ("annotation", "marker_annotation"):
                continue
            aname = _t(a.child_by_field_name("name"), self.src).rsplit(".", 1)[-1]
            args = a.child_by_field_name("arguments")
            if args is not None:
                self.walk_expr(args, ChainMap({}), scope, tv)
            if not mixin_targets:
                continue
            if aname in MIXIN_INJECTORS:
                for node, target in _annotation_strings(a, self.src, "method"):
                    self.check_mixin_method(node, aname, target, mixin_targets)
            elif aname == "Shadow":
                if _annotation_strings(a, self.src, "prefix"):
                    continue
                nm = member.child_by_field_name("name")
                if member.type == "field_declaration":
                    for d in member.named_children:
                        if d.type == "variable_declarator":
                            self.check_mixin_member(d.child_by_field_name("name"), "Shadow",
                                                    _t(d.child_by_field_name("name"), self.src), mixin_targets, "field")
                elif nm is not None:
                    self.check_mixin_member(nm, "Shadow", _t(nm, self.src), mixin_targets, "method")
            elif aname in ("Accessor", "Invoker"):
                vals = _annotation_strings(a, self.src, "value")
                nm = member.child_by_field_name("name")
                if vals:
                    node, target = vals[0]
                elif nm is not None:
                    node, target = nm, _derived_accessor(_t(nm, self.src), aname)
                else:
                    continue
                if target:
                    self.check_mixin_member(node, aname, target, mixin_targets,
                                            "field" if aname == "Accessor" else "method")

    def mixin_targets(self, mods, scope, tv) -> list[str] | None:
        for a in mods.named_children:
            if a.type != "annotation" or _t(a.child_by_field_name("name"), self.src).rsplit(".", 1)[-1] != "Mixin":
                continue
            out = []
            args = a.child_by_field_name("arguments")
            for cl in _descendants(args, "class_literal"):
                tnode = cl.named_children[0] if cl.named_children else None
                if tnode is not None:
                    got, _ = self.res(_type_text(tnode, self.src), scope[:-1], tv)
                    if got:
                        out.append(got)
            for node, s in _annotation_strings(a, self.src, "targets"):
                got = self.u.lookup_dotted(s.replace("/", ".").replace("$", "."))
                if got:
                    out.append(got)
                else:
                    self.site(node, "mixin", f"@Mixin(targets = \"{s}\")", s.rsplit(".", 1)[-1], "unknown"
                              if not self.u.cp.complete else "absent", why="no such class on the classpath")
            return out
        return None

    def check_mixin_method(self, node, ann: str, target: str, classes: list[str]) -> None:
        name = target.split("(")[0].split(":")[0].strip()
        if "L" in name and ";" in name:  # an owner-qualified target: Lpkg/Owner;name(...)
            name = name.rsplit(";", 1)[-1]
        if not name or "*" in name or name.startswith("<"):
            return
        self.check_mixin_member(node, ann, name, classes, "method", expr=f"@{ann}(method = \"{target}\")")

    def check_mixin_member(self, node, ann: str, name: str, classes: list[str], what: str, expr: str = "") -> None:
        u = self.u
        expr = expr or f"@{ann} {name}"
        for cls in classes:
            c = u.get(cls) or {}
            own = c.get("methods" if what == "method" else "fields", {})
            if name in own:
                self.site(node, "mixin", expr, name, "exists", where=f"{_src(cls)} ({u.origin(cls)})")
                return
        inherited = [cls for cls in classes if (u.methods(cls, name) if what == "method" else u.field_of(cls, name))]
        if inherited:
            self.site(node, "mixin", expr, name, "unknown",
                      why=f"{name} is inherited by {_src(inherited[0])}, not declared in it: a Mixin reaches only "
                          "the target's own members")
            return
        if all(u.get(cls) for cls in classes):
            names = sorted({n for cls in classes for n in (u.get(cls) or {}).get(
                "methods" if what == "method" else "fields", {}) if not n.startswith("<")})
            self.site(node, "mixin", expr, name, "absent",
                      why=f"the Mixin target {', '.join(_src(c) for c in classes)} declares no {what} {name}",
                      nearest=_nearest(name, names))
        else:
            self.site(node, "mixin", expr, name, "unknown", why="the Mixin target is not read")


def _derived_accessor(method: str, ann: str) -> str | None:
    prefixes = ("get", "is", "set") if ann == "Accessor" else ("call", "invoke")
    for p in prefixes:
        if method.startswith(p) and len(method) > len(p) and method[len(p)].isupper():
            rest = method[len(p):]
            return rest[0].lower() + rest[1:]
    return None


def _string_value(n, src: bytes) -> str | None:
    """A constant string expression: a literal, or literals joined with ``+`` (None otherwise)."""
    if n.type == "string_literal":
        return "".join(_t(c, src) for c in n.named_children if c.type == "string_fragment")
    if n.type == "binary_expression" and any(c.type == "+" for c in n.children):
        parts = [_string_value(c, src) for c in n.named_children]
        return None if any(p is None for p in parts) else "".join(parts)
    if n.type == "parenthesized_expression" and n.named_children:
        return _string_value(n.named_children[0], src)
    return None


def _annotation_strings(a, src: bytes, key: str) -> list[tuple[object, str]]:
    """String values of an annotation element (``method = "x"``, ``method = {"a", "b"}``, ``"a" + "b"``;
    ``value`` also for a lone argument)."""
    args = a.child_by_field_name("arguments")
    out = []
    if args is None:
        return out

    def values(v):
        if v is None:
            return
        if v.type in ("element_value_array_initializer", "array_initializer"):
            for c in v.named_children:
                values(c)
            return
        s = _string_value(v, src)
        if s is not None:
            out.append((v, s))

    for ch in args.named_children:
        if ch.type == "element_value_pair":
            k = ch.child_by_field_name("key")
            if k is not None and _t(k, src) == key:
                values(ch.child_by_field_name("value"))
        elif key == "value" and ch.type not in _COMMENTS:
            values(ch)
    return out


def _descendants(node, kind: str) -> list:
    out = []
    if node is None:
        return out
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type == kind:
            out.append(n)
            continue
        stack.extend(reversed(n.children))
    return out


def _type_nodes(node) -> list:
    """The outermost type names inside a type node (generic arguments included, primitives excluded)."""
    out = []
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type in ("type_identifier", "scoped_type_identifier"):
            out.append(n)
            if n.type == "scoped_type_identifier":
                stack.extend(c for c in n.children if c.type == "type_arguments")
            continue
        if n.type == "generic_type":
            out.append(n.named_children[0])
            stack.extend(c for c in n.named_children[1:])
            continue
        if n.type in ("class_body", "block", "argument_list", "annotation", "marker_annotation"):
            continue
        stack.extend(reversed(n.children))
    return out


_COMMENTS = ("line_comment", "block_comment", "comment")


def _count_args(args) -> int:
    return 0 if args is None else sum(1 for a in args.named_children if a.type not in _COMMENTS)


def _all_identifiers(n) -> bool:
    if n.type == "identifier":
        return True
    if n.type in ("field_access", "scoped_identifier"):
        return all(_all_identifiers(c) for c in n.named_children)
    return False


def _package_classes(u: Universe, pkg: str) -> list[str]:
    out = []
    for table in (u.project, u.lib, u.jdk):
        out += [n for n in table if n.rpartition("/")[0] == pkg and "$" not in n]
    return out


def _where_pkg(u: Universe, pkg: str) -> str:
    o = u.packages.get(pkg, set())
    return " and ".join(sorted({"project": "the project's sources", "lib": "the classpath's jars",
                                "jdk": "the JDK"}[x] for x in o)) or "nowhere"


def _nearest(name: str, names: list[str], limit: int = 3) -> list[str]:
    import difflib

    low = {n.lower(): n for n in names}
    got = difflib.get_close_matches(name.lower(), list(low), n=limit, cutoff=0.6)
    return [low[g] for g in got]


# -- entry point ----------------------------------------------------------------------------------

def _inside(p: Path, root: Path) -> bool:
    try:
        p.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def check_files(repo: Path, targets: list[tuple[str, Path, set[int] | None]], config: dict | None = None,
                cache_dir: Path | None = None, overrides: dict[str, bytes] | None = None) -> dict:
    """Sites of the Java files ``targets`` (``(rel, path, changed lines or None)``) and what was read."""
    import time

    t0 = time.perf_counter()
    repo = Path(repo).resolve()
    by_root: dict[Path, list] = {}
    for rel, p, lines in targets:
        by_root.setdefault(jvmclass.build_root(repo, Path(p)), []).append((rel, p, lines))
    sites, files, builds, notes = [], [], [], []
    for root, group in sorted(by_root.items()):
        # the configured classpath belongs to the repository's own build; another build finds its own
        cp = jvmclass.discover(root, config if root == repo else None)
        u = Universe(repo, cp, cache_dir, root, overrides)
        for rel, _p, lines in group:
            if rel not in u.trees:
                files.append({"path": rel, "error": "not read (outside the build's Java sources?)"})
                continue
            try:
                got = _FileCheck(u, rel, lines).run()
            except RecursionError:
                files.append({"path": rel, "error": "too deeply nested to walk"})
                continue
            files.append({"path": rel, "sites": len(got)})
            sites += got
        where = root.relative_to(repo).as_posix() if root != repo else "."
        builds.append({"build": where, "classpath": cp.source, "complete": cp.complete, "jars": len(cp.jars),
                       "jdk": str(cp.jdk) if cp.jdk else None, "release": cp.release,
                       "classes": {"project": len(u.project), "jars": len(u.lib), "jdk": len(u.jdk)}})
        notes += [f"{where}: {n}" for n in cp.notes]
    return {"sites": sites, "files": files, "builds": builds, "notes": notes,
            "seconds": round(time.perf_counter() - t0, 3)}
