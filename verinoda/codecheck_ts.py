"""Import check for TypeScript and JavaScript (docs/DESIGN.md D46): the web half of ``verinoda check``.

The invented name an agent writes most often in TypeScript and JavaScript is an import: a file that is not
there (``./utils/format``), a package that is not installed, or a name the module does not export
(``import { useNavigat } from "react-router"``, a hook from another version). Each *site* gets one verdict:

``exists``         found where the module is (the project's file, the package's types or code);
``absent``         the module is read completely and does not export that name, or a relative path names
                   no file;
``not_installed``  a package that is not in ``node_modules`` (installed dependencies are read from there);
``unknown``        the module's exports are not all known (``export *`` from what is not read, ``export =``
                   of something other than a namespace, CommonJS assignments that are not plain), or the
                   module is not read (a bundler alias, a built-in module without ``@types/node``) - why.

Modules are resolved as TypeScript does: relative paths with ``.ts``/``.tsx``/``.d.ts``/``.js``/``...``
and ``index`` files, ``tsconfig.json`` ``baseUrl``/``paths``, packages through ``node_modules`` (a
package's ``types``/``typings``, its ``exports`` map's ``types``/``import``/``default`` conditions,
``@types/<name>``, then its JavaScript ``main``/``module``), ambient ``declare module "x"`` blocks.
Nothing is run and no package is installed.

Sites: ``import`` (each imported name, the default import, the module itself), re-exports (``export ... from``),
``require`` calls with destructured names, and dynamic ``import()``. Members (``obj.method()``) are not
checked: their types need the TypeScript compiler.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

SUFFIXES = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")
LANG = {".ts": "TypeScript", ".tsx": "TypeScript", ".mts": "TypeScript", ".cts": "TypeScript",
        ".js": "JavaScript", ".jsx": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript"}
_TRY_EXT = (".ts", ".tsx", ".d.ts", ".mts", ".cts", ".d.mts", ".d.cts", ".js", ".jsx", ".mjs", ".cjs", ".json")
NODE_BUILTINS = frozenset("""assert async_hooks buffer child_process cluster console constants crypto dgram
    diagnostics_channel dns domain events fs http http2 https inspector module net os path perf_hooks process
    punycode querystring readline repl stream string_decoder sys timers tls trace_events tty url util v8 vm wasi
    worker_threads zlib test""".split())
SKIP_DIRS = {".git", "node_modules", ".verinoda", "dist", "build", "out", "coverage", ".next", ".nuxt", ".turbo",
             ".cache", ".svelte-kit"}

_PARSERS: dict[str, object] = {}


def _parser(path: str):
    from tree_sitter import Language, Parser

    low = path.lower()
    key = "tsx" if low.endswith((".tsx", ".jsx")) else "ts" if low.endswith((".ts", ".mts", ".cts")) else "js"
    if key not in _PARSERS:
        if key == "js":
            import tree_sitter_javascript as tsj

            _PARSERS[key] = Language(tsj.language())
        else:
            import tree_sitter_typescript as tst

            _PARSERS[key] = Language(tst.language_tsx() if key == "tsx" else tst.language_typescript())
    return Parser(_PARSERS[key])


def _t(n, src: bytes) -> str:
    return src[n.start_byte:n.end_byte].decode("utf-8", "replace")


def _str(n, src: bytes) -> str | None:
    if n is None or n.type not in ("string", "template_string"):
        return None
    frag = [c for c in n.named_children if c.type == "string_fragment"]
    if n.type == "template_string" and any(c.type == "template_substitution" for c in n.named_children):
        return None
    return "".join(_t(c, src) for c in frag) if frag else ""


@dataclass
class Exports:
    names: set[str] = field(default_factory=set)
    default: bool = False
    open: str | None = None            # why the names are not all known
    stars: list[str] = field(default_factory=list)   # modules re-exported with `export *`
    esm: bool = True                    # an ES module (not CommonJS, not `export =`)
    closed: bool = False                # not an ES module, but every name is known (`export =` of a namespace)


class Resolver:
    """Where a module specifier leads from a file, and what a module exports (cached)."""

    def __init__(self, repo: Path, overrides: dict[str, bytes] | None = None):
        self.repo = repo.resolve()
        self.overrides = {str((self.repo / k).resolve()): v for k, v in (overrides or {}).items()}
        self._exports: dict[str, Exports] = {}
        self._tsconfig: dict[str, tuple[Path | None, list[tuple[str, list[str]]]]] = {}
        self.ambient: dict[str, Exports] = {}
        self._ambient_read: set[str] = set()

    # -- reading files -----------------------------------------------------------------------------

    def read(self, p: Path) -> bytes | None:
        key = str(p.resolve())
        if key in self.overrides:
            return self.overrides[key]
        try:
            return p.read_bytes()
        except OSError:
            return None

    def _file(self, p: Path) -> Path | None:
        k = str(p.resolve())
        if k in self.overrides or p.is_file():
            return p
        return None

    # -- resolution --------------------------------------------------------------------------------

    def _with_ext(self, base: Path) -> Path | None:
        suffix = base.suffix.lower()
        if suffix in (".js", ".jsx", ".mjs", ".cjs"):
            # `./x.js` written for a `./x.ts` source (TypeScript's ESM convention), and the declaration file
            # beside a package's JavaScript: the types come first
            stem = base.with_suffix("")
            decl = {".mjs": (".mts", ".d.mts"), ".cjs": (".cts", ".d.cts")}.get(suffix, ())
            for ext in (*decl, ".ts", ".tsx", ".d.ts"):
                if self._file(Path(str(stem) + ext)):
                    return Path(str(stem) + ext)
        if self._file(base) and suffix in _TRY_EXT:
            return base
        for ext in _TRY_EXT:
            cand = Path(str(base) + ext)
            if self._file(cand):
                return cand
        if base.is_dir():
            pkg = self._package_json(base)
            for key in ("types", "typings", "module", "main"):
                v = pkg.get(key) if isinstance(pkg.get(key), str) else None
                if v:
                    got = self._with_ext((base / v))
                    if got:
                        return got
            for ext in _TRY_EXT:
                cand = base / f"index{ext}"
                if self._file(cand):
                    return cand
        return None

    def _package_json(self, d: Path) -> dict:
        try:
            data = json.loads((d / "package.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def tsconfig(self, file: Path) -> tuple[Path | None, list[tuple[str, list[str]]]]:
        """(baseUrl, paths) of the nearest ``tsconfig.json``/``jsconfig.json`` above ``file``."""
        for d in file.resolve().parents:
            if str(d) in self._tsconfig:
                return self._tsconfig[str(d)]
            for name in ("tsconfig.json", "jsconfig.json"):
                cfg = d / name
                if cfg.is_file():
                    got = self._read_tsconfig(cfg)
                    self._tsconfig[str(d)] = got
                    return got
            if d == self.repo or d.parent == d:
                break
        return None, []

    def _read_tsconfig(self, cfg: Path, depth: int = 0) -> tuple[Path | None, list[tuple[str, list[str]]]]:
        try:
            text = cfg.read_text(encoding="utf-8")
        except OSError:
            return None, []
        text = re.sub(r"//[^\n]*|/\*.*?\*/", "", text, flags=re.S)
        text = re.sub(r",(\s*[}\]])", r"\1", text)
        try:
            data = json.loads(text)
        except ValueError:
            return None, []
        base, paths = None, []
        ext = data.get("extends")
        if isinstance(ext, str) and ext.startswith(".") and depth < 5:
            base, paths = self._read_tsconfig((cfg.parent / ext).with_suffix(".json") if not ext.endswith(".json")
                                              else cfg.parent / ext, depth + 1)
        co = data.get("compilerOptions") or {}
        if isinstance(co.get("baseUrl"), str):
            base = (cfg.parent / co["baseUrl"]).resolve()
        if isinstance(co.get("paths"), dict):
            root = base or cfg.parent
            paths = [(k, [str(root / v) for v in vs if isinstance(v, str)]) for k, vs in co["paths"].items()
                     if isinstance(vs, list)]
        return base, paths

    def resolve(self, spec: str, importer: Path) -> tuple[str, Path | None, str]:
        """``(kind, path, why)``: kind ``file`` (the module's file), ``package`` (a package's entry file),
        ``ambient`` (a ``declare module``), ``missing`` (a relative path to nothing), ``not_installed``, or
        ``unknown`` (with why)."""
        if spec.startswith((".", "/")):
            got = self._with_ext((importer.parent / spec).resolve() if spec.startswith(".") else Path(spec))
            return ("file", got, "") if got else ("missing", None, f"no file {spec} (with .ts, .tsx, .js, "
                                                                      "... or an index file) next to it")
        base, paths = self.tsconfig(importer)
        for pattern, targets in paths:
            m = re.fullmatch(re.escape(pattern).replace(r"\*", "(.*)"), spec)
            if m:
                for tgt in targets:
                    got = self._with_ext(Path(tgt.replace("*", m.group(1) if m.groups() else "")))
                    if got:
                        return "file", got, ""
                return "missing", None, f"{spec} matches the tsconfig path {pattern}, which names no file"
        if base is not None:
            got = self._with_ext(base / spec)
            if got:
                return "file", got, ""
        name = spec[5:] if spec.startswith("node:") else spec
        self._read_ambient(importer)
        if spec in self.ambient or name in self.ambient:
            return "ambient", None, spec if spec in self.ambient else name
        if name.split("/")[0] in NODE_BUILTINS:
            return "unknown", None, f"{spec} is a Node.js built-in module (install @types/node to check its names)"
        if spec.startswith(("~", "@/", "#", "$", "virtual:")) or ":" in spec:
            return "unknown", None, f"{spec} is resolved by the bundler or framework (an alias), not read here"
        parts = spec.split("/")
        pkg_name = "/".join(parts[:2]) if spec.startswith("@") else parts[0]
        sub = "/".join(parts[2:] if spec.startswith("@") else parts[1:])
        for d in importer.resolve().parents:  # the package itself, by its own name
            own = self._package_json(d)
            if own.get("name") == pkg_name and "exports" in own:
                got = self._package_entry(d, sub)
                if got:
                    return "package", got, ""
                return "unknown", None, (f"{pkg_name} is this package itself; the file its exports name is not "
                                         "there (not built yet?)")
            if (d / "package.json").is_file() or d == self.repo or d.parent == d:
                break
        nm = self._node_modules(importer)
        if nm is None:
            return "unknown", None, "the dependencies are not installed (no node_modules): run npm install"
        pkg = nm / pkg_name
        if not pkg.is_dir():
            types = nm / "@types" / (pkg_name[1:].replace("/", "__") if pkg_name.startswith("@") else pkg_name)
            if types.is_dir():
                got = self._with_ext(types / sub) if sub else self._with_ext(types)
                return ("package", got, "") if got else ("unknown", None, f"{types} has no entry file")
            return "not_installed", None, f"{pkg_name} is not in {nm}"
        got = self._package_entry(pkg, sub)
        types = nm / "@types" / (pkg_name[1:].replace("/", "__") if pkg_name.startswith("@") else pkg_name)
        if (got is None or not got.name.endswith((".d.ts", ".d.mts", ".d.cts", ".ts", ".tsx"))) and types.is_dir():
            got = (self._with_ext(types / sub) if sub else self._with_ext(types)) or got
        if got is None:
            return "unknown", None, f"no types or entry file found for {spec} in {pkg}"
        return "package", got, ""

    def _node_modules(self, importer: Path) -> Path | None:
        """The nearest ``node_modules`` from the importing file up to the repository (never above it)."""
        for d in importer.resolve().parents:
            if (d / "node_modules").is_dir():
                return d / "node_modules"
            if d == self.repo or d.parent == d:
                break
        return None

    def _package_entry(self, pkg: Path, sub: str) -> Path | None:
        data = self._package_json(pkg)
        exports = data.get("exports")
        if exports is not None:
            key = "./" + sub if sub else "."
            target = None
            if isinstance(exports, (str, list)) or (isinstance(exports, dict) and not any(
                    str(k).startswith(".") for k in exports)):
                target = exports if key == "." else None
            elif isinstance(exports, dict):
                target = exports.get(key)
                if target is None:
                    for k, v in exports.items():
                        if "*" in k:
                            m = re.fullmatch(re.escape(k).replace(r"\*", "(.*)"), key)
                            if m:
                                target = json.loads(json.dumps(v).replace("*", m.group(1)))
                                break
            got = self._condition(pkg, target)
            if got:
                return got
        if sub:
            return self._with_ext(pkg / sub)
        for key in ("types", "typings"):
            if isinstance(data.get(key), str):
                got = self._with_ext(pkg / data[key])
                if got:
                    return got
        for key in ("module", "main"):
            if isinstance(data.get(key), str):
                got = self._with_ext(pkg / data[key])
                if got:
                    return got
        return self._with_ext(pkg / "index")

    def _condition(self, pkg: Path, target) -> Path | None:
        if isinstance(target, str):
            got = self._with_ext(pkg / target)
            if got and not got.name.endswith((".d.ts", ".d.mts", ".d.cts")):
                dts = Path(re.sub(r"\.(m|c)?js$", r".d.\1ts", str(got)).replace(".d.ts", ".d.ts"))
                if self._file(dts):
                    return dts
            return got
        if isinstance(target, list):
            for t in target:
                got = self._condition(pkg, t)
                if got:
                    return got
            return None
        if isinstance(target, dict):
            for cond in ("types", "import", "module", "require", "node", "default"):
                if cond in target:
                    got = self._condition(pkg, target[cond])
                    if got:
                        return got
        return None

    def _read_ambient(self, importer: Path) -> None:
        """``declare module "x"`` blocks of the project's ``.d.ts`` files and of ``@types`` packages."""
        nm = self._node_modules(importer)
        roots = [self.repo] + ([nm / "@types"] if nm is not None and (nm / "@types").is_dir() else [])
        for root in roots:
            if str(root) in self._ambient_read:
                continue
            self._ambient_read.add(str(root))
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS or root.name == "@types"]
                for f in filenames:
                    if f.endswith(".d.ts"):
                        self._exports_of(Path(dirpath) / f)

    # -- exports -----------------------------------------------------------------------------------

    def exports(self, p: Path, seen: frozenset = frozenset()) -> Exports:
        """What module ``p`` exports, ``export *`` followed (``open`` says why the list may be short)."""
        e = self._exports_of(p)
        if not e.stars:
            return e
        out = Exports(set(e.names), e.default, e.open, [], e.esm)
        for s in e.stars:
            if s in seen:
                continue
            kind, sp, why = self.resolve(s, p)
            if kind in ("file", "package") and sp is not None:
                sub = self.exports(sp, seen | {s})
                out.names |= sub.names
                if sub.open and not out.open:
                    out.open = f"export * from {s}: {sub.open}"
            elif kind == "ambient":
                sub = self.ambient_exports(why)
                out.names |= sub.names
                if sub.open and not out.open:
                    out.open = f"export * from {s}: {sub.open}"
            elif not out.open:
                out.open = f"export * from {s}, which is not read ({why or kind})"
        return out

    def _scan(self, nodes, src: bytes, e: Exports, dts: bool, p: Path, ambient: bool = False) -> bool:
        """Fill ``e`` from a module's statements (a file's, or a ``declare module`` block's); True for CommonJS."""
        namespaces: dict[str, set[str]] = {}
        declared: set[str] = set()
        value_decls: set[str] = set()   # functions, classes, constants: `export =` of one has members not read
        export_eq = None
        cjs = False

        def decl_names(d) -> list[str]:
            if d is None:
                return []
            if d.type in ("lexical_declaration", "variable_declaration"):
                out = []
                for v in d.named_children:
                    if v.type == "variable_declarator":
                        nm = v.child_by_field_name("name")
                        if nm is not None and nm.type == "identifier":
                            out.append(_t(nm, src))
                        elif nm is not None:  # a destructuring pattern
                            out += [_t(x, src) for x in _walk(nm) if x.type in (
                                "shorthand_property_identifier_pattern", "identifier")]
                return out
            nm = d.child_by_field_name("name")
            return [_t(nm, src)] if nm is not None and nm.type in ("identifier", "type_identifier") else []

        def ns_members(body) -> set[str]:
            out = set()
            for c in (body.named_children if body is not None else []):
                target = c.child_by_field_name("declaration") if c.type == "export_statement" else c
                if target is not None and target.type == "ambient_declaration":
                    target = target.named_children[0] if target.named_children else None
                out.update(decl_names(target))
            return out

        for n in nodes:
            node = n
            if n.type == "ambient_declaration" and n.named_children:
                inner = n.named_children[0]
                if inner.type == "module":
                    mname = _str(inner.child_by_field_name("name"), src)
                    if mname is not None:  # declare module "x" { ... }: its own exports
                        amb = Exports()
                        body = inner.child_by_field_name("body")
                        self._scan(body.named_children if body is not None else [], src, amb, True, p, ambient=True)
                        have = self.ambient.get(mname)
                        if have is None:
                            self.ambient[mname] = amb
                        else:  # declarations merge
                            have.names |= amb.names
                            have.stars += amb.stars
                            have.default = have.default or amb.default
                            have.esm = have.esm and amb.esm
                            have.closed = have.closed and amb.closed
                            have.open = have.open or amb.open
                        continue
                node = inner
            if node.type in ("module", "internal_module") and not n.type == "export_statement":
                nm = node.child_by_field_name("name")
                if nm is not None and nm.type in ("identifier", "nested_identifier"):
                    namespaces.setdefault(_t(nm, src), set()).update(ns_members(node.child_by_field_name("body")))
                declared.update(decl_names(node))
                continue
            if n.type == "export_statement":
                src_node = n.child_by_field_name("source")
                decl = n.child_by_field_name("declaration")
                kids = [c.type for c in n.children]
                if "=" in kids:  # export = X
                    ident = next((c for c in n.named_children if c.type in ("identifier", "nested_identifier")), None)
                    export_eq = _t(ident, src) if ident is not None else "?"
                    e.esm, e.default = False, True
                    continue
                if "default" in kids:
                    e.default = True
                    continue
                if decl is not None:
                    target = decl.named_children[0] if decl.type == "ambient_declaration" and decl.named_children \
                        else decl
                    e.names.update(decl_names(target))
                    continue
                clause = next((c for c in n.named_children if c.type == "export_clause"), None)
                ns = next((c for c in n.named_children if c.type == "namespace_export"), None)
                if clause is not None:
                    for spec in clause.named_children:
                        if spec.type == "export_specifier":
                            alias = spec.child_by_field_name("alias") or spec.child_by_field_name("name")
                            if alias is not None:
                                name = _t(alias, src).strip("'\"")
                                if name == "default":
                                    e.default = True
                                else:
                                    e.names.add(name)
                elif ns is not None:
                    ident = next((c for c in ns.named_children if c.type == "identifier"), None)
                    if ident is not None:
                        e.names.add(_t(ident, src))
                elif "*" in kids and src_node is not None:
                    s = _str(src_node, src)
                    if s is not None:
                        e.stars.append(s)
                continue
            if n.type == "expression_statement":  # CommonJS
                text = _t(n, src)
                m = re.match(r"\s*(?:module\.)?exports\.(\w+)\s*=", text)
                if m:
                    e.names.add(m.group(1))
                    cjs = True
                    continue
                if re.match(r"\s*module\.exports\s*=", text):
                    cjs = True
                    obj = next((x for x in _walk(n) if x.type == "object"), None)
                    rhs = text.split("=", 1)[1].strip().rstrip(";")
                    if obj is not None and rhs.startswith("{"):
                        for prop in obj.named_children:
                            key = prop.child_by_field_name("key") if prop.type == "pair" else prop
                            if key is not None and key.type in ("property_identifier", "shorthand_property_identifier",
                                                                "identifier"):
                                e.names.add(_t(key, src))
                            elif prop.type == "spread_element":
                                e.open = e.open or "module.exports spreads another object"
                    else:
                        e.open = e.open or "module.exports is assigned something other than an object literal"
                    e.default = True
                    continue
            if n.type == "import_alias":  # import x = require("y") / import x = N.M
                continue
            if dts or n.type == "ambient_declaration":
                target = n.named_children[0] if n.type == "ambient_declaration" and n.named_children else n
                names_here = decl_names(target)
                declared.update(names_here)
                if target.type not in ("module", "internal_module", "interface_declaration",
                                       "type_alias_declaration"):
                    value_decls.update(names_here)
        if ambient and export_eq is None:  # in a declared module every declaration is exported
            e.names |= declared | set(namespaces)
        if export_eq is not None:
            if export_eq in namespaces and export_eq not in value_decls:
                e.names |= namespaces[export_eq]
                e.closed = not e.open
            elif export_eq in namespaces:
                e.names |= namespaces[export_eq]
                e.open = e.open or f"export = {export_eq}: a value and a namespace; the value's members are not read"
            elif export_eq in declared:
                e.open = e.open or f"export = {export_eq}: its members are not read"
            else:
                e.open = e.open or f"export = {export_eq}"
        return cjs

    def ambient_exports(self, name: str, seen: frozenset = frozenset()) -> Exports:
        """A declared module's exports, its `export * from "other"` followed through the declared modules."""
        e = self.ambient.get(name) or Exports(open=f"no declared module {name}")
        if not e.stars:
            return e
        out = Exports(set(e.names), e.default, e.open, [], e.esm, e.closed)
        for s in e.stars:
            if s in seen:
                continue
            if s in self.ambient:
                sub = self.ambient_exports(s, seen | {name})
                out.names |= sub.names
                out.open = out.open or (f"export * from {s}: {sub.open}" if sub.open else None)
            elif not out.open:
                out.open = f"export * from {s}, which is not a declared module"
        return out

    def _exports_of(self, p: Path) -> Exports:
        key = str(p.resolve())
        if key in self._exports:
            return self._exports[key]
        e = Exports()
        self._exports[key] = e
        if p.suffix.lower() == ".json":
            e.default, e.open, e.esm = True, "a JSON module", False
            return e
        src = self.read(p)
        if src is None:
            e.open = "not readable"
            return e
        tree = _parser(p.name).parse(src)
        if tree.root_node.has_error:
            e.open = "the file does not parse completely"
        dts = p.name.endswith((".d.ts", ".d.mts", ".d.cts"))
        cjs = self._scan(tree.root_node.named_children, src, e, dts, p)
        if cjs:
            e.esm, e.default = False, True
        if not p.suffix.lower() in (".ts", ".tsx", ".mts", ".cts") and not dts and not e.names and not e.stars \
                and not e.default and not cjs:
            e.open = e.open or "a script without ES or CommonJS exports (it may set globals)"
        return e


def _walk(n):
    stack = [n]
    while stack:
        x = stack.pop()
        yield x
        stack.extend(reversed(x.children))


# -- checking files ---------------------------------------------------------------------------------

def ts_files(root: Path) -> list[Path]:
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        out += [Path(dirpath) / f for f in filenames if f.endswith(SUFFIXES) and not f.endswith(".d.ts")]
    return sorted(out)


def _nearest(name: str, names, limit: int = 3) -> list[dict]:
    import difflib

    low = {n.lower(): n for n in names}
    return [{"name": low[g]} for g in difflib.get_close_matches(name.lower(), list(low), n=limit, cutoff=0.6)]


def check_file(r: Resolver, repo: Path, rel: str, lines: set[int] | None) -> list[dict]:
    p = (repo / rel).resolve()
    src = r.read(p)
    if src is None:
        return []
    tree = _parser(rel).parse(src)
    lang = LANG.get(Path(rel).suffix.lower(), "JavaScript")
    broken = tree.root_node.has_error
    sites: list[dict] = []

    def site(node, kind, expr, name, verdict, why="", where="", nearest=None):
        line, col = node.start_point[0] + 1, node.start_point[1] + 1
        if lines is not None and line not in lines:
            return
        if verdict == "absent" and broken:
            verdict, why, nearest = "unknown", f"{why}; but the file does not parse completely", None
        s = {"at": f"{rel}:{line}:{col}", "path": rel, "line": line, "col": col, "kind": kind, "expr": expr[:120],
             "name": name, "verdict": verdict, "language": lang}
        if why:
            s["why"] = why
        if where:
            s["where"] = where
        if nearest:
            s["nearest"] = nearest
        sites.append(s)

    def module(node, spec: str, names: list[tuple[object, str]], default_node=None, kind="import") -> None:
        k, target, why = r.resolve(spec, p)
        if k == "missing":
            site(node, kind, spec, spec, "absent", why=why)
            return
        if k in ("not_installed",):
            site(node, kind, spec, spec, "not_installed", why=why)
            return
        if k == "unknown":
            site(node, kind, spec, spec, "unknown", why=why)
            return
        where = str(target.relative_to(repo)) if target is not None and _inside(target, repo) else \
            (str(target) if target is not None else f"declare module {why!r}")
        ex = r.ambient_exports(why) if k == "ambient" else r.exports(target)
        own = target is not None and _inside(target, repo) and "node_modules" not in target.parts \
            and not target.name.endswith((".d.ts", ".d.mts", ".d.cts"))
        site(node, kind, spec, spec, "exists", where=where)
        if default_node is not None:
            if ex.default or not ex.esm:
                site(default_node, kind, f"default from {spec}", "default", "exists", where=where)
            elif ex.open or ex.stars or not own:
                site(default_node, kind, f"default from {spec}", "default", "unknown",
                     why=ex.open or ("export * never carries a default" if ex.stars else
                                     "a package or a declaration: esModuleInterop may give it a default"))
            else:
                site(default_node, kind, f"default from {spec}", "default", "absent",
                     why=f"{spec} has no default export ({where})")
        for nnode, name in names:
            if name in ex.names or (name == "default" and ex.default):
                site(nnode, kind, f"{{ {name} }} from {spec}", name, "exists", where=where)
            elif ex.open:
                site(nnode, kind, f"{{ {name} }} from {spec}", name, "unknown", why=ex.open)
            elif not ex.esm and not ex.closed and k != "ambient":
                site(nnode, kind, f"{{ {name} }} from {spec}", name, "unknown",
                     why="a CommonJS module: its exports object may carry more")
            else:
                site(nnode, kind, f"{{ {name} }} from {spec}", name, "absent",
                     why=f"{spec} does not export {name} ({where})", nearest=_nearest(name, ex.names))

    for n in _walk(tree.root_node):
        if n.type == "import_statement":
            spec = _str(n.child_by_field_name("source"), src)
            if spec is None:
                continue
            clause = next((c for c in n.named_children if c.type == "import_clause"), None)
            names, default_node = [], None
            for c in (clause.named_children if clause is not None else []):
                if c.type == "identifier":
                    default_node = c
                elif c.type == "named_imports":
                    for s in c.named_children:
                        if s.type == "import_specifier":
                            nm = s.child_by_field_name("name")
                            if nm is not None:
                                names.append((nm, _t(nm, src).strip("'\"")))
            module(n.child_by_field_name("source"), spec, names, default_node)
        elif n.type == "export_statement" and n.child_by_field_name("source") is not None:
            spec = _str(n.child_by_field_name("source"), src)
            if spec is None:
                continue
            clause = next((c for c in n.named_children if c.type == "export_clause"), None)
            names = []
            for s in (clause.named_children if clause is not None else []):
                if s.type == "export_specifier":
                    nm = s.child_by_field_name("name")
                    if nm is not None:
                        names.append((nm, _t(nm, src).strip("'\"")))
            module(n.child_by_field_name("source"), spec, names, kind="import")
        elif n.type == "call_expression":
            fn = n.child_by_field_name("function")
            args = n.child_by_field_name("arguments")
            first = args.named_children[0] if args is not None and args.named_children else None
            spec = _str(first, src)
            if fn is None or spec is None:
                continue
            if fn.type == "import" or _t(fn, src) == "import":
                module(first, spec, [])
            elif _t(fn, src) == "require":
                names = []
                parent = n.parent
                if parent is not None and parent.type == "variable_declarator":
                    pat = parent.child_by_field_name("name")
                    if pat is not None and pat.type == "object_pattern":
                        for prop in pat.named_children:
                            key = prop.child_by_field_name("key") if prop.type == "pair_pattern" else prop
                            if key is not None and key.type in ("shorthand_property_identifier_pattern",
                                                                "property_identifier"):
                                names.append((key, _t(key, src)))
                module(first, spec, names)
    return sites


def _inside(p: Path, root: Path) -> bool:
    try:
        p.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def check_files(repo: Path, targets: list[tuple[str, Path, set[int] | None]],
                overrides: dict[str, bytes] | None = None) -> dict:
    import time

    t0 = time.perf_counter()
    repo = Path(repo).resolve()
    r = Resolver(repo, overrides)
    sites, files = [], []
    for rel, _p, lines in targets:
        try:
            got = check_file(r, repo, rel, lines)
        except RecursionError:
            files.append({"path": rel, "error": "too deeply nested to walk"})
            continue
        files.append({"path": rel, "sites": len(got)})
        sites += got
    notes = []
    if not any(r._node_modules(repo / rel) for rel, _p, _l in targets[:1]):
        notes.append("no node_modules: packages are not installed, so their names are unknown (run npm install)")
    return {"sites": sites, "files": files, "notes": notes, "seconds": round(time.perf_counter() - t0, 3)}
