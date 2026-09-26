"""Name-existence check for Kotlin code (docs/DESIGN.md D45): the Kotlin half of ``verinoda check``.

It shares the Java check's universe (:mod:`verinoda.codecheck_java`): the project's Java and Kotlin sources,
the classpath's jars and the JDK. Kotlin makes more names open than Java, and the check says ``unknown``
for them rather than guess:

- an **extension function or property** can give any type a member from anywhere: a member that is not
  found is ``absent`` only when no extension of that name exists in the project's Kotlin sources or in the
  classpath's Kotlin libraries (their file facades, ``...Kt`` classes), and only when kotlin-stdlib itself
  was read;
- **default and named arguments**: the number of arguments is not checked;
- Kotlin's own types (``String``, ``Int``, ``List``...) are mapped onto Java's with extra members: a member
  of one of them is ``exists`` or ``unknown``, never ``absent``;
- a call without a receiver (a top-level function, a local function, an import) is ``exists`` or ``unknown``.

Sites: ``import``, ``type`` (a type as written), ``method`` (``recv.name(...)``) and ``property``
(``recv.name``), on receivers whose type is known: a parameter's or property's declared type, a constructor
call, ``this``, a string literal, and the declared type of a call or property further down a chain (a Java
getter counts for a Kotlin property: ``player.mainHandItem`` is ``getMainHandItem()``).
"""
from __future__ import annotations

import re
from pathlib import Path

from verinoda import codecheck_java as cj

KOTLIN_DEFAULT_IMPORTS = ("kotlin", "kotlin.annotation", "kotlin.collections", "kotlin.comparisons", "kotlin.io",
                          "kotlin.ranges", "kotlin.sequences", "kotlin.text", "kotlin.jvm", "java.lang")
# Kotlin's own types: they exist; their members are Java's plus the standard library's extensions
BUILTINS = frozenset("""Any Unit Nothing Int Long Short Byte Double Float Boolean Char String CharSequence Number
    Comparable Array IntArray LongArray ShortArray ByteArray DoubleArray FloatArray BooleanArray CharArray List
    MutableList Set MutableSet Map MutableMap Collection MutableCollection Iterable MutableIterable Iterator
    MutableIterator ListIterator MutableListIterator Pair Triple Sequence Function Lazy Result Enum Annotation
    Throwable Exception RuntimeException Error IllegalArgumentException IllegalStateException
    UnsupportedOperationException IndexOutOfBoundsException NoSuchElementException ArithmeticException
    NullPointerException ClassCastException AssertionError NumberFormatException ConcurrentModificationException
    Comparator Cloneable KClass Suppress Deprecated JvmStatic JvmField JvmOverloads JvmName Volatile Transient
    Synchronized Throws UInt ULong UShort UByte Regex StringBuilder HashMap HashSet ArrayList LinkedHashMap
    LinkedHashSet""".split())
MAPPED = {"Any": "java/lang/Object", "String": "java/lang/String", "CharSequence": "java/lang/CharSequence",
          "Number": "java/lang/Number", "Comparable": "java/lang/Comparable", "Throwable": "java/lang/Throwable",
          "List": "java/util/List", "MutableList": "java/util/List", "Set": "java/util/Set",
          "MutableSet": "java/util/Set", "Map": "java/util/Map", "MutableMap": "java/util/Map",
          "Collection": "java/util/Collection", "MutableCollection": "java/util/Collection",
          "Iterable": "java/lang/Iterable", "Iterator": "java/util/Iterator", "Comparator": "java/util/Comparator",
          "StringBuilder": "java/lang/StringBuilder", "HashMap": "java/util/HashMap", "ArrayList": "java/util/ArrayList",
          "HashSet": "java/util/HashSet", "LinkedHashMap": "java/util/LinkedHashMap"}
# the standard library's scope functions and infix helpers, on every type
SCOPE_FUNCTIONS = frozenset({"let", "run", "apply", "also", "with", "takeIf", "takeUnless", "to", "use",
                             "equals", "hashCode", "toString"})
_DECLS = ("class_declaration", "object_declaration")


def kotlin_files(root: Path) -> list[Path]:
    """The build's Kotlin sources (``.kt``; build scripts ``.gradle.kts`` are not code of the project)."""
    return cj.java_files(root, (".kt",))


def _t(n, src: bytes) -> str:
    return src[n.start_byte:n.end_byte].decode("utf-8", "replace")


_LANG = None


def _parser():
    import tree_sitter_kotlin as tsk
    from tree_sitter import Language, Parser

    global _LANG
    if _LANG is None:
        _LANG = Language(tsk.language())
    return Parser(_LANG)


def _type_name(n, src: bytes) -> str:
    """A type as written, without arguments and nullability (``List<Foo>?`` -> ``List``, ``a.B`` -> ``a.B``)."""
    if n is None:
        return ""
    if n.type in ("nullable_type", "type", "parenthesized_type"):
        inner = next((c for c in n.named_children if c.type not in ("type_modifiers",)), None)
        return _type_name(inner, src)
    if n.type == "user_type":
        return ".".join(_t(c, src) for c in n.named_children if c.type in ("identifier", "type_identifier",
                                                                             "simple_identifier"))
    if n.type == "function_type":
        return "Function"
    return _t(n, src).split("<")[0].rstrip("?").strip()


def _file_ctx(rel: str, root, src: bytes) -> cj.FileCtx:
    ctx = cj.FileCtx(rel, "", lang="kotlin")
    aliases: dict[str, str] = {}
    for n in root.named_children:
        if n.type == "package_header":
            q = next((c for c in n.named_children if c.type in ("qualified_identifier", "identifier")), None)
            if q is not None:
                ctx.package = _t(q, src).replace(" ", "").replace(".", "/")
        elif n.type in ("import", "import_header", "import_list"):
            items = [n] if n.type != "import_list" else [c for c in n.named_children if c.type in ("import",
                                                                                                    "import_header")]
            for it in items:
                q = next((c for c in it.named_children if c.type in ("qualified_identifier", "identifier")), None)
                if q is None:
                    continue
                dotted = _t(q, src).replace(" ", "")
                text = _t(it, src)
                if text.rstrip().endswith("*"):
                    ctx.ondemand.append(dotted)
                    continue
                m = re.search(r"\bas\s+([A-Za-z_]\w*)\s*$", text)
                simple = m.group(1) if m else dotted.rsplit(".", 1)[-1]
                ctx.single[simple] = dotted
                if m:
                    aliases[simple] = dotted
    ctx.ondemand += list(KOTLIN_DEFAULT_IMPORTS)
    return ctx


def declarations(rel: str, src: bytes, tree) -> tuple[cj.FileCtx, dict[str, dict], dict[str, set[str]],
                                                    dict[str, set[str]]]:
    """The file's context, its types (``jvmclass`` class dicts, ``origin`` project, ``lang`` kotlin), its
    top-level names per package, and the names of the extension functions and properties it declares."""
    ctx = _file_ctx(rel, tree.root_node, src)
    types: dict[str, dict] = {}
    toplevel: dict[str, set[str]] = {}
    extensions: dict[str, set[str]] = {}   # name -> the simple names of the receiver types it extends
    facade = (ctx.package + "/" if ctx.package else "") + (Path(rel).stem[:1].upper() + Path(rel).stem[1:]) + "Kt"
    fac = {"name": facade, "super_src": None, "ifaces_src": [], "origin": "project", "kind": "class", "lang": "kotlin",
           "flags": 0, "methods": {}, "fields": {}, "inner": {}, "file": rel, "line": 1, "typevars": [],
           "has_ctor": True, "open_members": True}

    def receiver_of(fn) -> str | None:
        # `fun Type.name(...)`: a receiver type before the name
        name = fn.child_by_field_name("name")
        for c in fn.children:
            if c == name:
                return None
            if c.type in ("user_type", "nullable_type", "type"):
                return _type_name(c, src).rsplit(".", 1)[-1] or "?"
        return None

    def prop_names(p) -> list[tuple[str, str]]:
        out = []
        for c in p.named_children:
            if c.type == "variable_declaration":
                nm = next((x for x in c.named_children if x.type in ("identifier", "simple_identifier")), None)
                ty = next((x for x in c.named_children if x.type in ("user_type", "nullable_type", "type")), None)
                if nm is not None:
                    out.append((_t(nm, src), _type_name(ty, src) if ty is not None else ""))
            elif c.type == "multi_variable_declaration":
                for v in c.named_children:
                    nm = next((x for x in v.named_children if x.type in ("identifier", "simple_identifier")), None)
                    if nm is not None:
                        out.append((_t(nm, src), ""))
        return out

    def add_member(cls: dict, m, static: bool) -> None:
        if m.type == "function_declaration":
            name = m.child_by_field_name("name")
            if name is None:
                return
            recv = receiver_of(m)
            if recv is not None:
                extensions.setdefault(_t(name, src), set()).add(recv)
                return
            ret = next((c for c in m.children if c.type in ("user_type", "nullable_type", "type")
                        and c.start_byte > name.start_byte), None)
            cls["methods"].setdefault(_t(name, src), []).append(
                [-1, (0x0008 if static else 0) | 0x0080, _type_name(ret, src) if ret is not None else None])
        elif m.type == "property_declaration":
            text_head = _t(m, src).split("=")[0]
            for pname, ptype in prop_names(m):
                m_ext = re.search(r"\b([A-Za-z_][\w.]*)(?:<[^>]*>)?\??\s*\.\s*" + re.escape(pname) + r"\b",
                                  text_head)
                if m_ext:  # `val Type.name`
                    extensions.setdefault(pname, set()).add(m_ext.group(1).rsplit(".", 1)[-1])
                    continue
                _prop(cls, pname, ptype, static)

    def _prop(cls: dict, pname: str, ptype: str, static: bool) -> None:
        cls["fields"][pname] = [ptype or None, 0x0008 if static else 0]
        cap = pname[:1].upper() + pname[1:]
        getter = pname if pname.startswith("is") and pname[2:3].isupper() else "get" + cap
        cls["methods"].setdefault(getter, []).append([0, 0x0008 if static else 0, ptype or None])
        cls["methods"].setdefault("set" + cap, []).append([1, 0x0008 if static else 0, "void"])

    def add(node, outer: str | None, outer_vars: set[str]) -> None:
        name_node = node.child_by_field_name("name") or next(
            (c for c in node.named_children if c.type in ("identifier", "type_identifier", "simple_identifier")), None)
        if name_node is None:
            return
        simple = _t(name_node, src)
        binary = f"{outer}${simple}" if outer else ((ctx.package + "/" if ctx.package else "") + simple)
        head = _t(node, src)[:200]
        kind = "interface" if re.search(r"\binterface\b", head.split("{")[0]) else \
            "enum" if re.search(r"\benum\s+class\b", head) else "object" if node.type == "object_declaration" \
            else "class"
        tv = set(outer_vars)
        tps = next((c for c in node.named_children if c.type == "type_parameters"), None)
        for tp in (tps.named_children if tps is not None else []):
            ident = next((x for x in tp.named_children if x.type in ("identifier", "type_identifier")), None)
            if ident is not None:
                tv.add(_t(ident, src))
        supers = []
        ds = next((c for c in node.named_children if c.type == "delegation_specifiers"), None)
        for spec in (ds.named_children if ds is not None else []):
            ut = next((x for x in cj._descendants(spec, "user_type")), None)
            if ut is not None:
                supers.append(_type_name(ut, src))
        cls = {"name": binary, "super_src": None, "ifaces_src": supers, "origin": "project", "kind": kind,
               "lang": "kotlin", "flags": 0x0200 if kind == "interface" else 0, "methods": {}, "fields": {},
               "inner": {}, "file": rel, "line": node.start_point[0] + 1, "typevars": sorted(tv), "has_ctor": True,
               "kotlin_supers": True}
        types[binary] = cls
        if outer and outer in types:
            types[outer]["inner"][simple] = binary
        pc = next((c for c in node.named_children if c.type == "primary_constructor"), None)
        if pc is not None:
            for cp in cj._descendants(pc, "class_parameter"):
                if re.match(r"\s*(?:\w+\s+)*(val|var)\b", _t(cp, src)):
                    nm = next((x for x in cp.named_children if x.type in ("identifier", "simple_identifier")), None)
                    ty = next((x for x in cp.named_children if x.type in ("user_type", "nullable_type", "type")), None)
                    if nm is not None:
                        _prop(cls, _t(nm, src), _type_name(ty, src), False)
        cls["methods"].setdefault("<init>", []).append([-1, 0x0080, "void"])
        body = next((c for c in node.named_children if c.type in ("class_body", "enum_class_body")), None)
        for m in (body.named_children if body is not None else []):
            if m.type == "enum_entry":
                nm = next((x for x in m.named_children if x.type in ("identifier", "simple_identifier")), None)
                if nm is not None:
                    cls["fields"][_t(nm, src)] = [simple, 0x0008]
            elif m.type == "companion_object":
                cb = next((x for x in m.named_children if x.type == "class_body"), None)
                for cm in (cb.named_children if cb is not None else []):
                    if cm.type in _DECLS:
                        add(cm, binary, tv)
                    else:
                        add_member(cls, cm, True)
                cname = next((x for x in m.named_children if x.type in ("identifier", "type_identifier")), None)
                cls["inner"][_t(cname, src) if cname is not None else "Companion"] = binary
            elif m.type in _DECLS:
                add(m, binary, tv if kind != "interface" else set())
            else:
                add_member(cls, m, kind == "object")
        if kind == "object":  # Java reaches an object's members through INSTANCE
            cls["fields"]["INSTANCE"] = [simple, 0x0008]
        if kind == "enum":
            cls["methods"]["values"] = [[0, 0x0008, None]]
            cls["methods"]["valueOf"] = [[1, 0x0008, simple]]
            cls["methods"]["entries"] = [[0, 0x0008, None]]
            cls["fields"]["entries"] = [None, 0x0008]

    broken = tree.root_node.has_error
    for n in tree.root_node.named_children:
        if n.type in _DECLS:
            add(n, None, set())
        elif n.type in ("function_declaration", "property_declaration"):
            before = set(extensions)
            add_member(fac, n, True)
            name = n.child_by_field_name("name")
            names = [_t(name, src)] if name is not None else [p for p, _ in prop_names(n)]
            for nm in names:
                toplevel.setdefault(ctx.package, set()).add(nm)
            del before
        elif n.type == "type_alias":
            nm = next((x for x in n.named_children if x.type in ("type_identifier", "identifier")), None)
            if nm is not None:
                alias = (ctx.package + "/" if ctx.package else "") + _t(nm, src)
                types[alias] = {"name": alias, "super_src": None, "ifaces_src": [], "origin": "project",
                                "kind": "typealias", "lang": "kotlin", "flags": 0, "methods": {}, "fields": {},
                                "inner": {}, "file": rel, "line": n.start_point[0] + 1, "typevars": [],
                                "has_ctor": True, "open_members": True}
    types[facade] = fac
    if broken:
        # tree-sitter-kotlin does not read all of today's Kotlin (a constructor on the next line, context
        # receivers): what it lost may be any member or type, so nothing here closes a name
        for c in types.values():
            c["open_members"] = True
        for m in re.finditer(r"(?m)^[ \t]*(?:@[\w.]+(?:\([^)]*\))?\s+)*(?:(?:public|private|internal|protected|"
                             r"open|abstract|sealed|data|enum|annotation|value|inner|inline|fun|expect|actual)\s+)*"
                             r"(?:class|interface|object)\s+([A-Z]\w*)", src.decode("utf-8", "replace")):
            binary = (ctx.package + "/" if ctx.package else "") + m.group(1)
            types.setdefault(binary, {"name": binary, "super_src": None, "ifaces_src": [], "origin": "project",
                                      "kind": "class", "lang": "kotlin", "flags": 0, "methods": {}, "fields": {},
                                      "inner": {}, "file": rel, "line": 1, "typevars": [], "has_ctor": True,
                                      "open_members": True})
        ctx.broken = True
    return ctx, types, toplevel, extensions


# -- the universe's Kotlin side ----------------------------------------------------------------------

def library_extensions(u) -> tuple[set[str], bool]:
    """(names of the extension functions and properties the classpath's Kotlin libraries declare, whether
    kotlin-stdlib itself was read). A Kotlin file facade (``CollectionsKt``, ``StringsKt__StringsKt``) holds
    its top-level functions as static methods; an extension property ``val T.size`` is a ``getSize(T)``."""
    cached = getattr(u, "_kt_lib_ext", None)
    if cached is not None:
        return cached
    names: set[str] = set()
    for binary, c in u.lib.items():
        simple = binary.rsplit("/", 1)[-1]
        if not re.search(r"Kt(?:$|__)", simple):
            continue
        names.update(n for n in (c.get("kt_names") or ()) if n and n.isidentifier())
        for mname, ovs in (c.get("methods") or {}).items():
            if not any(ov[1] & 0x0008 for ov in ovs):
                continue
            names.add(mname)
            if mname.startswith("get") and len(mname) > 3 and any(ov[0] >= 1 for ov in ovs):
                names.add(mname[3].lower() + mname[4:])
    stdlib = any(k.startswith("kotlin/collections/CollectionsKt") for k in u.lib)
    u._kt_lib_ext = (names, stdlib)
    return u._kt_lib_ext


def _cap(name: str) -> str:
    return name[:1].upper() + name[1:]


class _KtFileCheck(cj._FileCheck):
    """The walk of one Kotlin file; sites as the Java check's (``language`` Kotlin)."""

    def site(self, node, kind, expr, name, verdict, why="", where="", nearest=None):
        n = len(self.sites)
        super().site(node, kind, expr, name, verdict, why, where, nearest)
        if len(self.sites) > n:
            self.sites[-1]["language"] = "Kotlin"

    def run(self) -> list[dict]:
        root = self.tree.root_node
        for n in root.named_children:
            if n.type in ("import", "import_header"):
                self.kt_import(n)
            elif n.type == "import_list":
                for it in n.named_children:
                    if it.type in ("import", "import_header"):
                        self.kt_import(it)
        for n in root.named_children:
            if n.type in _DECLS:
                self.kt_class(n, [], set())
            elif n.type in ("function_declaration", "property_declaration"):
                self.kt_member(n, [], set())
        return self.sites

    # imports and types

    def kt_import(self, n) -> None:
        q = next((c for c in n.named_children if c.type in ("qualified_identifier", "identifier")), None)
        if q is None:
            return
        dotted = _t(q, self.src).replace(" ", "")
        u = self.u
        if _t(n, self.src).rstrip().endswith("*"):
            known = u.lookup_dotted(dotted) or dotted.replace(".", "/") in u.packages
            self.site(q, "import", dotted + ".*", dotted, "exists" if known else "unknown",
                      why="" if known else "no package or class read by that name")
            return
        simple = dotted.rsplit(".", 1)[-1]
        binary = u.lookup_dotted(dotted)
        if binary:
            self.site(q, "import", dotted, simple, "exists", where=u.origin(binary))
            return
        pkg = "/".join(dotted.split(".")[:-1])
        if dotted.startswith("kotlin.") and simple in BUILTINS:  # a built-in type: no class file of its own
            self.site(q, "import", dotted, simple, "exists", where="a Kotlin built-in type")
            return
        if simple in u.kt_toplevel.get(pkg, ()) or self._facade_has(pkg, simple):
            self.site(q, "import", dotted, simple, "exists", where="a top-level function or property")
            return
        owner = u.lookup_dotted(dotted.rsplit(".", 1)[0])
        if owner and (u.methods(owner, simple) or u.field_of(owner, simple)):  # import a.b.Object.member
            self.site(q, "import", dotted, simple, "exists", where=u.origin(owner))
            return
        _names, stdlib = library_extensions(u)
        kotlin_pkg = dotted.startswith("kotlin.")
        if pkg in u.packages and u.closed_package(pkg) and (stdlib or not kotlin_pkg):
            self.site(q, "import", dotted, simple, "absent",
                      why=f"no class, top-level function or property {simple} in package {pkg.replace('/', '.')}",
                      nearest=cj._nearest(simple, [x.rsplit("/", 1)[-1] for x in cj._package_classes(u, pkg)]
                                          + sorted(u.kt_toplevel.get(pkg, ()))))
        else:
            self.site(q, "import", dotted, simple, "unknown", why="its package is not read (or not completely)")

    def _facade_has(self, pkg: str, name: str) -> bool:
        cap = "get" + _cap(name)
        for binary, c in self.u.lib.items():
            if binary.rpartition("/")[0] == pkg and re.search(r"Kt(?:$|__)", binary.rsplit("/", 1)[-1]):
                if name in c.get("methods", {}) or cap in c.get("methods", {}) or name in c.get("fields", {}) \
                        or name in (c.get("kt_names") or ()):
                    return True
        return False

    def kt_res(self, written: str, scope, tv) -> tuple[str | None, str]:
        head = written.split(".")[0]
        if not written or head in tv:
            return None, "typevar"
        if written in MAPPED and self.u.get(MAPPED[written]) and written not in self.ctx.single:
            return MAPPED[written], ""
        if written in BUILTINS and written not in self.ctx.single:
            return None, "builtin"
        got, why = self.res(written, scope, tv)
        if got:
            return got, ""
        if written in BUILTINS:
            return None, "builtin"
        return None, why

    def kt_types(self, node, scope, tv) -> None:
        for ut in cj._descendants(node, "user_type") if node is not None else []:
            written = _type_name(ut, self.src)
            if not written:
                continue
            got, why = self.kt_res(written, scope, tv)
            if got:
                self.site(ut, "type", written, written.rsplit(".", 1)[-1], "exists", where=self.u.origin(got))
            elif why == "builtin":
                self.site(ut, "type", written, written, "exists", where="a Kotlin built-in type")
            elif why not in ("typevar", "primitive"):
                v, w = self.verdict_of(why)
                _names, stdlib = library_extensions(self.u)
                if v == "absent" and not stdlib:
                    v, w = "unknown", "kotlin-stdlib is not on the classpath: a default import may declare it"
                self.site(ut, "type", written, written.rsplit(".", 1)[-1], v, why=w,
                          nearest=cj._nearest(written, self._visible_type_names(scope)) if v == "absent" else None)

    # declarations

    def kt_class(self, node, scope: list[str], tv: set[str], binary: str | None = None) -> None:
        name_node = node.child_by_field_name("name") or next(
            (c for c in node.named_children if c.type in ("identifier", "type_identifier", "simple_identifier")), None)
        if name_node is None:
            return
        simple = _t(name_node, self.src)
        binary = binary or (f"{scope[-1]}${simple}" if scope else ((self.ctx.package + "/" if self.ctx.package else "")
                                                                   + simple))
        c = self.u.get(binary) or {}
        tv = tv | set(c.get("typevars") or ())
        inner_scope = scope + [binary]
        for part in node.named_children:
            if part.type in ("delegation_specifiers", "primary_constructor"):
                self.kt_types(part, inner_scope, tv)
                for arg in cj._descendants(part, "value_arguments"):
                    self.kt_expr(arg, {}, inner_scope, tv)
            elif part.type in ("class_body", "enum_class_body"):
                for m in part.named_children:
                    if m.type in _DECLS:
                        self.kt_class(m, inner_scope, tv)
                    elif m.type == "companion_object":
                        cb = next((x for x in m.named_children if x.type == "class_body"), None)
                        for cm in (cb.named_children if cb is not None else []):
                            if cm.type in _DECLS:
                                self.kt_class(cm, inner_scope, tv)
                            else:
                                self.kt_member(cm, inner_scope, tv)
                    elif m.type == "enum_entry":
                        for arg in cj._descendants(m, "value_arguments"):
                            self.kt_expr(arg, {}, inner_scope, tv)
                    else:
                        self.kt_member(m, inner_scope, tv)

    def kt_member(self, m, scope, tv) -> None:
        if m.type == "function_declaration":
            outer_fn = getattr(self, "_fn", None)
            self._fn = m  # smart casts are looked for in the function a site is in
            try:
                self._kt_function(m, scope, tv)
            finally:
                self._fn = outer_fn
            return
        self._kt_other_member(m, scope, tv)

    def _kt_function(self, m, scope, tv) -> None:
        ftv = set(tv)
        tps = next((c for c in m.named_children if c.type == "type_parameters"), None)
        for tp in (tps.named_children if tps is not None else []):
            ident = next((x for x in tp.named_children if x.type in ("identifier", "type_identifier")), None)
            if ident is not None:
                ftv.add(_t(ident, self.src))
        env: dict = {}
        params = next((c for c in m.named_children if c.type == "function_value_parameters"), None)
        for p in (params.named_children if params is not None else []):
            if p.type == "parameter":
                nm = next((x for x in p.named_children if x.type in ("identifier", "simple_identifier")), None)
                ty = next((x for x in p.named_children if x.type in ("user_type", "nullable_type", "type",
                                                                      "function_type")), None)
                self.kt_types(ty, scope, ftv)
                if nm is not None:
                    env[_t(nm, self.src)] = self.kt_res(_type_name(ty, self.src), scope, ftv)[0] \
                        if ty is not None else None
            else:
                self.kt_expr(p, env, scope, ftv)  # default values
        end = params.end_byte if params is not None else 0
        for c in m.named_children:
            if c.type in ("user_type", "nullable_type", "type") and c.start_byte > end:
                self.kt_types(c, scope, ftv)
        body = next((c for c in m.named_children if c.type == "function_body"), None)
        if body is not None:
            self.kt_expr(body, env, scope, ftv)

    def _kt_other_member(self, m, scope, tv) -> None:
        if m.type == "property_declaration":
            for c in m.named_children:
                if c.type in ("variable_declaration", "multi_variable_declaration"):
                    self.kt_types(c, scope, tv)
                elif c.type not in ("modifiers",):
                    self.kt_expr(c, {}, scope, tv)
        elif m.type in ("anonymous_initializer", "secondary_constructor", "getter", "setter"):
            self.kt_expr(m, {}, scope, tv)

    # expressions

    _SCOPES = ("statements", "block", "function_body", "control_structure_body", "lambda_literal", "annotated_lambda",
               "if_expression", "when_expression", "when_entry", "try_expression", "catch_block", "finally_block",
               "for_statement", "while_statement", "do_while_statement", "anonymous_initializer",
               "secondary_constructor", "getter", "setter")

    def kt_expr(self, n, env: dict, scope, tv) -> str | None:
        """Walk ``n`` for its sites; its type (a binary name) when it is known."""
        if n is None:
            return None
        ty = n.type
        u = self.u
        if ty in self._SCOPES:
            inner = dict(env)
            if ty == "lambda_literal":
                for p in cj._descendants(n, "lambda_parameters"):
                    for nm in cj._descendants(p, "identifier"):
                        inner[_t(nm, self.src)] = None
                inner["it"] = None
            elif ty == "for_statement":
                for v in [c for c in n.named_children if c.type in ("variable_declaration",
                                                                    "multi_variable_declaration")]:
                    for nm in cj._descendants(v, "identifier"):
                        inner[_t(nm, self.src)] = None
            elif ty == "catch_block":
                nm = next((x for x in n.named_children if x.type in ("identifier", "simple_identifier")), None)
                cty = next((x for x in n.named_children if x.type in ("user_type", "type")), None)
                self.kt_types(cty, scope, tv)
                if nm is not None:
                    inner[_t(nm, self.src)] = self.kt_res(_type_name(cty, self.src), scope, tv)[0] if cty else None
            for c in n.named_children:
                if c.type == "property_declaration":
                    self.kt_local(c, inner, scope, tv)
                elif c.type == "function_declaration":
                    self.kt_member(c, scope, tv)
                elif c.type in _DECLS:
                    self.kt_class(c, scope, tv)
                elif c.type == "catch_block" or ty != "catch_block" or c.type not in ("identifier", "user_type"):
                    self.kt_expr(c, inner, scope, tv)
            return None
        if ty in ("identifier", "simple_identifier"):
            name = _t(n, self.src)
            if name in env:
                return env[name]
            f = next((u.field_of(c, name) for c in reversed(scope) if u.field_of(c, name)), None)
            if f is not None:
                return u.type_of_written(f[1][0], f[0]) if f[1][0] else None
            return None
        if ty == "this_expression":
            return scope[-1] if scope else None
        if ty in ("string_literal", "multiline_string_literal"):
            for c in n.named_children:
                self.kt_expr(c, env, scope, tv)
            return "java/lang/String"
        if ty == "parenthesized_expression":
            return self.kt_expr(n.named_children[0] if n.named_children else None, env, scope, tv)
        if ty == "as_expression":
            first = n.named_children[0] if n.named_children else None
            self.kt_expr(first, env, scope, tv)
            tnode = next((c for c in n.named_children[1:] if c.type in ("user_type", "nullable_type", "type")), None)
            self.kt_types(tnode, scope, tv)
            return self.kt_res(_type_name(tnode, self.src), scope, tv)[0] if tnode is not None else None
        if ty == "call_expression":
            return self.kt_call(n, env, scope, tv)
        if ty == "navigation_expression":
            return self.kt_nav(n, env, scope, tv, called=False, nargs=None)
        if ty in ("user_type", "nullable_type", "type"):
            self.kt_types(n, scope, tv)
            return None
        for c in n.named_children:
            self.kt_expr(c, env, scope, tv)
        return None

    def kt_local(self, p, env: dict, scope, tv) -> None:
        decl = next((c for c in p.named_children if c.type in ("variable_declaration",
                                                               "multi_variable_declaration")), None)
        value = None
        for c in p.named_children:
            if c.type not in ("variable_declaration", "multi_variable_declaration", "modifiers"):
                value = c
        got = self.kt_expr(value, env, scope, tv) if value is not None else None
        if decl is None:
            return
        self.kt_types(decl, scope, tv)
        if decl.type == "variable_declaration":
            nm = next((x for x in decl.named_children if x.type in ("identifier", "simple_identifier")), None)
            tyn = next((x for x in decl.named_children if x.type in ("user_type", "nullable_type", "type")), None)
            if nm is not None:
                env[_t(nm, self.src)] = self.kt_res(_type_name(tyn, self.src), scope, tv)[0] if tyn is not None \
                    else got
        else:
            for nm in cj._descendants(decl, "identifier"):
                env[_t(nm, self.src)] = None

    def kt_receiver(self, obj, env, scope, tv) -> tuple[str, str | None, str]:
        """(kind, binary, why): ``value`` of a known type, ``type`` (a class, object or companion used as
        the receiver), or ``unknown``."""
        u = self.u
        if obj.type in ("identifier", "simple_identifier"):
            name = _t(obj, self.src)
            if name in env:
                return ("value", env[name], "") if env[name] else ("unknown", None, f"the type of {name} is not known")
            f = next((u.field_of(c, name) for c in reversed(scope) if u.field_of(c, name)), None)
            if f is not None:
                t = u.type_of_written(f[1][0], f[0]) if f[1][0] else None
                return ("value", t, "") if t else ("unknown", None, f"the type of {name} is not known")
            got, _why = self.kt_res(name, scope, tv)
            if got and name[:1].isupper():
                return "type", got, ""
            return "unknown", None, f"{name} is not a known value or type"
        if obj.type == "navigation_expression" and not cj._descendants(obj, "call_expression"):
            dotted = _t(obj, self.src).replace(" ", "").replace("?", "")
            head = dotted.split(".")[0]
            if re.fullmatch(r"[\w.]+", dotted) and head not in env and not any(u.field_of(c, head) for c in scope) \
                    and not self.kt_res(head, scope, tv)[0]:
                got = u.lookup_dotted(dotted)
                if got:
                    return "type", got, ""
                if head[:1].islower():
                    return "unknown", None, f"{dotted} is not a known value or type"
        t = self.kt_expr(obj, env, scope, tv)
        return ("value", t, "") if t else ("unknown", None, "the receiver's type is not known")

    def kt_call(self, n, env, scope, tv) -> str | None:
        u = self.u
        callee = n.named_children[0] if n.named_children else None
        for c in n.named_children[1:]:
            self.kt_expr(c, env, scope, tv)
        if callee is None:
            return None
        if callee.type == "navigation_expression":
            return self.kt_nav(callee, env, scope, tv, called=True, nargs=None)
        if callee.type in ("identifier", "simple_identifier"):
            name = _t(callee, self.src)
            got = self.kt_res(name, scope, tv)[0] if name[:1].isupper() else None
            if got:  # a constructor call: the class
                self.site(callee, "type", name, name, "exists", where=u.origin(got))
                return got
            return None  # a top-level or local function: not checked (see the module docstring)
        return self.kt_expr(callee, env, scope, tv)

    def kt_nav(self, n, env, scope, tv, *, called: bool, nargs) -> str | None:
        u = self.u
        obj = n.named_children[0] if n.named_children else None
        name_node = n.named_children[-1] if len(n.named_children) > 1 else None
        if name_node is not None and name_node.type == "navigation_suffix":
            name_node = next((x for x in name_node.named_children if x.type in ("identifier", "simple_identifier")),
                             None)
        if obj is None or name_node is None or name_node.type not in ("identifier", "simple_identifier"):
            if obj is not None:
                self.kt_expr(obj, env, scope, tv)
            return None
        name = _t(name_node, self.src)
        reference = any(c.type == "::" for c in n.children)
        if reference and name == "class":
            self.kt_expr(obj, env, scope, tv)
            return "kotlin/reflect/KClass" if u.get("kotlin/reflect/KClass") else None
        _kind, binary, why = self.kt_receiver(obj, env, scope, tv)
        expr = cj._short(_t(obj, self.src)) + ("::" if reference else ".") + name + ("(...)" if called else "")
        site_kind = "method" if called else "property"
        if reference:
            if binary is None:
                return None
            found = self._member(binary, name, True) or self._member(binary, name, False)
            if found is not None:
                self.site(name_node, "reference", expr, name, "exists", where=cj._src(found[0]))
            return None
        if binary is None:
            self.site(name_node, site_kind, expr, name, "unknown", why=why)
            return None
        builtin = binary in MAPPED.values()
        found = self._member(binary, name, called)
        if found is not None:
            owner, ret = found
            self.site(name_node, site_kind, expr, name, "exists", where=f"{cj._src(owner)} ({u.origin(owner)})")
            return u.type_of_written(ret, owner) if ret and ret not in ("void", "V") else None
        if name in SCOPE_FUNCTIONS:
            self.site(name_node, site_kind, expr, name, "exists", where="kotlin-stdlib")
            return None
        ext, stdlib = library_extensions(u)
        if builtin:
            v, w = "unknown", f"{cj._src(binary)} is a Kotlin built-in type: the standard library adds members"
        elif not u.hierarchy_closed(binary):
            missing = u.ancestors(binary)[1]
            v, w = "unknown", (f"{cj._src(binary)} has super types that are not read ({cj._src(missing[0])})"
                               if missing else f"{cj._src(binary)} is not read")
        elif _kind == "value" and self._smart_cast(obj, binary, name, called):
            v, w = "unknown", (f"{_t(obj, self.src)} may be smart-cast to a subtype that declares {name}")
        elif name in u.kt_extensions and self._project_extension(binary, name):
            v, w = "exists", "an extension the project declares for this type"
        elif name in u.kt_extensions or name in ext:
            v, w = "unknown", (f"{name} may be an extension {'function' if called else 'property'} "
                               f"({'the project' if name in u.kt_extensions else 'a library'} declares one)")
        elif not stdlib:
            v, w = "unknown", "kotlin-stdlib is not on the classpath: an extension may declare it"
        else:
            v, w = "absent", (f"{cj._src(binary)} and its super types declare no "
                              f"{'function' if called else 'property'} {name}, and no extension of that name is "
                              "declared in the project or its libraries")
        if v == "exists":
            self.site(name_node, site_kind, expr, name, "exists", where=w)
            return None
        self.site(name_node, site_kind, expr, name, v, why=w,
                  nearest=cj._nearest(name, self._kt_member_names(binary)) if v == "absent" else None)
        return None

    def _smart_cast(self, obj, binary: str, name: str, called: bool) -> bool:
        """Was the receiver checked with ``is``/``as``/``when`` earlier in the function (a smart cast may give
        it a subtype's members)?"""
        if obj.type not in ("identifier", "simple_identifier", "this_expression"):
            return False
        fn = getattr(self, "_fn", None)
        start = fn.start_byte if fn is not None else 0
        text = self.src[start:obj.start_byte].decode("utf-8", "replace")
        v = re.escape(_t(obj, self.src))
        return bool(re.search(r"\b" + v + r"\s+(?:!?is|as\??)\s", text)
                    or re.search(r"\bwhen\s*\(\s*(?:val\s+\w+\s*=\s*)?" + v + r"\s*\)", text))

    def _project_extension(self, binary: str, name: str) -> bool:
        recv = self.u.kt_extensions.get(name) or set()
        simple = {a.rsplit("/", 1)[-1].split("$")[-1] for a in self.u.ancestors(binary)[0]}
        return bool(recv & (simple | {"Any", "T"}))

    def _member(self, binary: str, name: str, called: bool) -> tuple[str, str | None] | None:
        """``(owner, declared type)`` of a function, property, Java getter or member type ``name``."""
        u = self.u
        if called:
            nested = u.member_type(binary, name)
            if nested:  # `Outer.Inner(...)`: a nested class's constructor
                return nested, nested
            ovs = u.methods(binary, name)
            if ovs:
                first = ovs[0][0]  # the closest type that declares it: its overloads' return type
                rets = {ov[2] for o, ov in ovs if o == first}
                return first, (rets.pop() if len(rets) == 1 else None)
            f = u.field_of(binary, name)  # a property holding a function
            return (f[0], None) if f else None
        f = u.field_of(binary, name)
        if f is not None:
            return f[0], f[1][0]
        for getter in ("get" + _cap(name), name if name.startswith("is") else "is" + _cap(name)):
            ovs = [(o, ov) for o, ov in u.methods(binary, getter) if ov[0] in (0, -1)]
            if ovs:
                return ovs[0][0], ovs[0][1][2]
        if u.member_type(binary, name):
            return binary, None
        companion = f"{binary}$Companion"  # a library class's companion object: `KTypeProjection.STAR`
        if u.get(companion) and companion != binary:
            return self._member(companion, name, called)
        return None

    def _kt_member_names(self, binary: str) -> list[str]:
        names = set()
        for a in self.u.ancestors(binary)[0]:
            c = self.u.get(a) or {}
            for m in c.get("methods", {}):
                if m.startswith("get") and len(m) > 3:
                    names.add(m[3].lower() + m[4:])
                names.add(m)
            names |= set(c.get("fields", {}))
        return sorted(n for n in names if not n.startswith("<"))
