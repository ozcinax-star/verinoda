"""Go extractor. Moved verbatim from graphify/extract.py."""
from __future__ import annotations


import hashlib
from pathlib import Path
from verinoda.project_index.extractors.base import _LANGUAGE_BUILTIN_GLOBALS, _file_stem, _make_id, _read_text


_GO_PREDECLARED_TYPES = frozenset({
    "bool", "byte", "complex64", "complex128", "error", "float32", "float64",
    "int", "int8", "int16", "int32", "int64", "rune", "string",
    "uint", "uint8", "uint16", "uint32", "uint64", "uintptr", "any", "comparable",
})

# Go predeclared functions, filtered only when the callee is a BARE identifier.
# The Go resolver looks a callee up by name, so an unexported method that happens
# to share a builtin's name (`func (h *history) append(...)`) absorbs every
# builtin call in the corpus: on an 8.9k-node Go codebase one such method
# collected 330 phantom inbound `calls` edges, inventing twelve database-layer ->
# service-layer edges — a layering violation absent from the source.
#
# Deliberately language-local (mirroring _RUST_TRAIT_METHOD_BLOCKLIST) rather
# than added to the shared _LANGUAGE_BUILTIN_GLOBALS: `new`, `close` and friends
# are ordinary method names in the ~11 other languages that consult the shared
# set — listing them there kills every in-file Rust `Type::new()` edge.
#
# Bare-identifier-only for the same reason within Go: `h.append(v)` and
# `pkg.Delete(x)` are selector_expression callees and are genuine calls, so the
# filter must not reach them. Builtin *types* stay out (see
# _GO_PREDECLARED_TYPES): Go conversions are call-shaped too, but they produced
# no phantom edges on that corpus and filtering them would suppress genuine
# constructor-like calls.
#
# The set is the Go spec's predeclared function list in full. Being Go-local and
# bare-identifier-only makes completeness safe here: `len`, `max`, `min` and
# `print` carry the same shadowing hazard as `append`, and a principled boundary
# (the spec list) beats a hand-picked subset.
_GO_PREDECLARED_FUNCS = frozenset({
    "append", "cap", "clear", "close", "complex", "copy", "delete", "imag",
    "len", "make", "max", "min", "new", "panic", "print", "println", "real",
    "recover",
})

def _go_collect_type_refs(node, source: bytes, generic: bool, out: list[tuple[str, str]]) -> None:
    """Walk a Go type expression; append (name, role) tuples."""
    if node is None:
        return
    t = node.type
    if t == "type_identifier":
        text = _read_text(node, source)
        if text and text not in _GO_PREDECLARED_TYPES:
            out.append((text, "generic_arg" if generic else "type"))
        return
    if t == "qualified_type":
        # Keep the package qualifier so the generic stub rewire cannot attach
        # `testing.T` to an unrelated local type or function named T.
        text = _read_text(node, source)
        if text:
            out.append((text, "generic_arg" if generic else "type"))
        return
    if t == "generic_type":
        type_field = node.child_by_field_name("type")
        if type_field is not None:
            sub: list[tuple[str, str]] = []
            _go_collect_type_refs(type_field, source, generic, sub)
            out.extend(sub)
        for c in node.children:
            if c.type == "type_arguments":
                for arg in c.children:
                    if arg.is_named:
                        _go_collect_type_refs(arg, source, True, out)
        return
    if t in ("pointer_type", "slice_type", "array_type", "map_type",
             "channel_type", "parenthesized_type"):
        for c in node.children:
            if c.is_named:
                _go_collect_type_refs(c, source, generic, out)
        return
    if node.is_named:
        for c in node.children:
            if c.is_named:
                _go_collect_type_refs(c, source, generic, out)

def extract_go(path: Path) -> dict:
    """Extract functions, methods, type declarations, and imports from a .go file."""
    try:
        import tree_sitter_go as tsgo
        from tree_sitter import Language, Parser
    except ImportError:
        return {"nodes": [], "edges": [], "error": "tree-sitter-go not installed"}

    try:
        language = Language(tsgo.language())
        parser = Parser(language)
        source = path.read_bytes()
        tree = parser.parse(source)
        root = tree.root_node
    except Exception as e:
        return {"nodes": [], "edges": [], "error": str(e)}

    stem = _file_stem(path)
    # Use directory name as package scope so methods on the same type across
    # multiple files in a package share one canonical type node.
    pkg_scope = path.parent.name or stem
    str_path = str(path)
    nodes: list[dict] = []
    edges: list[dict] = []
    seen_ids: set[str] = set()
    function_bodies: list[tuple[str, object]] = []
    # Local change (Verinoda): a method's own receiver (name, type) and the methods of each receiver type of this
    # file, so `c.m()` on the method's own receiver binds to its type's `m` and no other selector call binds to a
    # same-named function of the file.
    own_receiver_of: dict[str, tuple[str | None, str]] = {}
    methods_by_type: dict[tuple[str, str], str] = {}
    param_types_of: dict[str, dict[str, str]] = {}  # function -> parameter name -> its declared local type

    def _param_types(func_node) -> dict[str, str]:
        """Parameters declared with a type of this package (`h *metricHistory`, `c Context`); not `pkg.T`,
        slices, maps or funcs."""
        out: dict[str, str] = {}
        params = func_node.child_by_field_name("parameters")
        for param in (params.children if params is not None else ()):
            if param.type != "parameter_declaration":
                continue
            type_node = param.child_by_field_name("type")
            if type_node is None:
                continue
            tname = _read_text(type_node, source).lstrip("*").split("[", 1)[0].strip()
            if not tname.isidentifier():
                continue
            for child in param.children:
                if child.type == "identifier" and child != type_node:
                    out[_read_text(child, source)] = tname
        return out
    # local package name (including aliases) -> written Go import path
    go_imported_pkgs: dict[str, str] = {}

    def add_node(nid: str, label: str, line: int) -> None:
        if nid not in seen_ids:
            seen_ids.add(nid)
            nodes.append({
                "id": nid,
                "label": label,
                "file_type": "code",
                "source_file": str_path,
                "source_location": f"L{line}",
            })

    def add_edge(src: str, tgt: str, relation: str, line: int,
                 confidence: str = "EXTRACTED", weight: float = 1.0,
                 context: str | None = None) -> None:
        edge = {
            "source": src,
            "target": tgt,
            "relation": relation,
            "confidence": confidence,
            "source_file": str_path,
            "source_location": f"L{line}",
            "weight": weight,
        }
        if context:
            edge["context"] = context
        edges.append(edge)

    file_nid = _make_id(str(path))
    add_node(file_nid, path.name, 1)

    def ensure_named_node(name: str, line: int) -> str:
        nid = _make_id(pkg_scope, name)
        if nid in seen_ids:
            return nid
        nid = _make_id(name)
        if nid not in seen_ids:
            # The name isn't declared in this file, so this is a cross-file reference
            # (e.g. a type defined in another file of the package). Emit a SOURCELESS
            # stub — like the inheritance-base path in the other extractors — so the
            # corpus-level rewire can collapse it onto the real definition. A sourced
            # stub here makes _disambiguate_colliding_node_ids bake the referencing
            # file's path (with extension) into the id and blocks the rewire, which is
            # the phantom-duplicate-node bug (#1402).
            seen_ids.add(nid)
            nodes.append({
                "id": nid,
                "label": name,
                "file_type": "code",
                "source_file": "",
                "source_location": "",
                "origin_file": str_path,
            })
        return nid

    def emit_go_method_refs(func_node, func_nid: str, line: int) -> None:
        params = func_node.child_by_field_name("parameters")
        if params is not None:
            for p in params.children:
                if p.type != "parameter_declaration":
                    continue
                type_node = p.child_by_field_name("type")
                refs: list[tuple[str, str]] = []
                _go_collect_type_refs(type_node, source, False, refs)
                for ref_name, role in refs:
                    ctx = "generic_arg" if role == "generic_arg" else "parameter_type"
                    tgt = ensure_named_node(ref_name, line)
                    if tgt != func_nid:
                        add_edge(func_nid, tgt, "references", line, context=ctx)
        result = func_node.child_by_field_name("result")
        if result is not None:
            if result.type == "parameter_list":
                for p in result.children:
                    if p.type != "parameter_declaration":
                        continue
                    type_node = p.child_by_field_name("type")
                    if type_node is None:
                        for c in p.children:
                            if c.is_named:
                                type_node = c
                                break
                    refs = []
                    _go_collect_type_refs(type_node, source, False, refs)
                    for ref_name, role in refs:
                        ctx = "generic_arg" if role == "generic_arg" else "return_type"
                        tgt = ensure_named_node(ref_name, line)
                        if tgt != func_nid:
                            add_edge(func_nid, tgt, "references", line, context=ctx)
            else:
                refs = []
                _go_collect_type_refs(result, source, False, refs)
                for ref_name, role in refs:
                    ctx = "generic_arg" if role == "generic_arg" else "return_type"
                    tgt = ensure_named_node(ref_name, line)
                    if tgt != func_nid:
                        add_edge(func_nid, tgt, "references", line, context=ctx)

    # Node IDs are casefolded (ids.py), so `Run` and `run` declared in one file
    # produce the same id and add_node silently dropped the second — the unexported
    # half vanished from the graph and its call sites bound by bare name to a
    # same-named function in another package, which Go's visibility rules make
    # impossible (#2779). Salt the non-canonical member of a case-only collision
    # so both survive.
    #
    # The EXPORTED member keeps the plain id. Only exported symbols are reachable
    # across packages, so cross-package edges (and edges cached in graph.json from
    # files an incremental rebuild does not touch) target the exported one — keeping
    # its id stable means adding/removing an unexported sibling in an update never
    # re-points them. Docs likewise reference the exported API, so the casefolded id
    # a semantic node produces lands on the symbol it actually describes. Calls to
    # the unexported sibling can only come from the same package and only resolve
    # once the sibling exists, so salting it re-points nothing. When the collision
    # has no unique exported member (`Run`/`RUN`), every member is salted rather
    # than picking one arbitrarily, so the result never depends on declaration order.
    case_groups: dict[str, set[str]] = {}

    def _receiver_type_of(node) -> str | None:
        receiver = node.child_by_field_name("receiver")
        if not receiver:
            return None
        for param in receiver.children:
            if param.type == "parameter_declaration":
                type_node = param.child_by_field_name("type")
                if type_node:
                    return _read_text(type_node, source).lstrip("*").strip()
                break
        return None

    def _plain_symbol_nid(node) -> tuple[str, str] | None:
        name_node = node.child_by_field_name("name")
        if not name_node:
            return None
        name = _read_text(name_node, source)
        if node.type == "method_declaration":
            receiver_type = _receiver_type_of(node)
            base = _make_id(pkg_scope, receiver_type) if receiver_type else stem
        else:
            base = stem
        return _make_id(base, name), name

    def _scan_declarations(node) -> None:
        if node.type in ("function_declaration", "method_declaration"):
            found = _plain_symbol_nid(node)
            if found:
                case_groups.setdefault(found[0], set()).add(found[1])
            return
        for child in node.children:
            _scan_declarations(child)

    def symbol_nid(plain_nid: str, name: str) -> str:
        names = case_groups.get(plain_nid) or set()
        if len(names) < 2:
            return plain_nid
        exported = [n for n in names if n[:1].isupper()]
        if len(exported) == 1 and name == exported[0]:
            return plain_nid
        salt = hashlib.sha1(name.encode("utf-8"), usedforsecurity=False).hexdigest()[:6]
        return _make_id(plain_nid, salt)

    def walk(node) -> None:
        t = node.type

        if t == "function_declaration":
            name_node = node.child_by_field_name("name")
            if name_node:
                func_name = _read_text(name_node, source)
                line = node.start_point[0] + 1
                func_nid = symbol_nid(_make_id(stem, func_name), func_name)
                add_node(func_nid, f"{func_name}()", line)
                add_edge(file_nid, func_nid, "contains", line)
                emit_go_method_refs(node, func_nid, line)
                param_types_of[func_nid] = _param_types(node)
                body = node.child_by_field_name("body")
                if body:
                    function_bodies.append((func_nid, body))
            return

        if t == "method_declaration":
            receiver = node.child_by_field_name("receiver")
            receiver_type: str | None = None
            receiver_name: str | None = None
            if receiver:
                for param in receiver.children:
                    if param.type == "parameter_declaration":
                        type_node = param.child_by_field_name("type")
                        if type_node:
                            receiver_type = _read_text(type_node, source).lstrip("*").strip()
                        rname = param.child_by_field_name("name")
                        if rname is not None:
                            receiver_name = _read_text(rname, source)
                        break
            name_node = node.child_by_field_name("name")
            if not name_node:
                return
            method_name = _read_text(name_node, source)
            line = node.start_point[0] + 1

            if receiver_type:
                parent_nid = _make_id(pkg_scope, receiver_type)
                add_node(parent_nid, receiver_type, line)
                method_nid = symbol_nid(_make_id(parent_nid, method_name), method_name)
                add_node(method_nid, f".{method_name}()", line)
                add_edge(parent_nid, method_nid, "method", line)
                own_type = receiver_type.split("[", 1)[0].strip()
                methods_by_type.setdefault((own_type, method_name), method_nid)
                own_receiver_of[method_nid] = (
                    receiver_name if receiver_name and receiver_name != "_" else None, own_type)
                param_types_of[method_nid] = _param_types(node)
            else:
                method_nid = symbol_nid(_make_id(stem, method_name), method_name)
                add_node(method_nid, f"{method_name}()", line)
                add_edge(file_nid, method_nid, "contains", line)

            emit_go_method_refs(node, method_nid, line)
            body = node.child_by_field_name("body")
            if body:
                function_bodies.append((method_nid, body))
            return

        if t == "type_declaration":
            for child in node.children:
                if child.type != "type_spec":
                    continue
                name_node = child.child_by_field_name("name")
                if not name_node:
                    continue
                type_name = _read_text(name_node, source)
                line = child.start_point[0] + 1
                type_nid = _make_id(pkg_scope, type_name)
                add_node(type_nid, type_name, line)
                add_edge(file_nid, type_nid, "contains", line)
                # Type body: struct fields (with embeds) or interface embedding.
                type_body = None
                for tc in child.children:
                    if tc.type in ("struct_type", "interface_type"):
                        type_body = tc
                        break
                if type_body is None:
                    continue
                if type_body.type == "struct_type":
                    for fdl in type_body.children:
                        if fdl.type != "field_declaration_list":
                            continue
                        for field in fdl.children:
                            if field.type != "field_declaration":
                                continue
                            has_name = any(
                                fc.type == "field_identifier" for fc in field.children
                            )
                            type_node = field.child_by_field_name("type")
                            if type_node is None:
                                for fc in field.children:
                                    if fc.is_named and fc.type != "field_identifier":
                                        type_node = fc
                                        break
                            refs: list[tuple[str, str]] = []
                            _go_collect_type_refs(type_node, source, False, refs)
                            for ref_name, role in refs:
                                tgt = ensure_named_node(ref_name, field.start_point[0] + 1)
                                if tgt == type_nid:
                                    continue
                                if not has_name and role == "type":
                                    add_edge(type_nid, tgt, "embeds",
                                             field.start_point[0] + 1)
                                else:
                                    ctx = "generic_arg" if role == "generic_arg" else "field"
                                    add_edge(type_nid, tgt, "references",
                                             field.start_point[0] + 1, context=ctx)
                elif type_body.type == "interface_type":
                    for elem in type_body.children:
                        if elem.type == "method_elem":
                            # A method requirement declared in the interface body
                            # is part of the interface's contract. Emit it as a
                            # method node so the interface isn't left an empty
                            # shell and calls against the interface can resolve
                            # (mirrors receiver methods and the way the other
                            # extractors capture interface members).
                            m_name_node = elem.child_by_field_name("name")
                            if m_name_node is None:
                                for mc in elem.children:
                                    if mc.type == "field_identifier":
                                        m_name_node = mc
                                        break
                            if m_name_node is None:
                                continue
                            m_name = _read_text(m_name_node, source)
                            m_line = elem.start_point[0] + 1
                            m_nid = symbol_nid(_make_id(type_nid, m_name), m_name)
                            add_node(m_nid, f".{m_name}()", m_line)
                            add_edge(type_nid, m_nid, "method", m_line)
                            continue
                        if elem.type != "type_elem":
                            continue
                        # A type_elem that is a generics type-set constraint -
                        # a union (`A | B`) or an approximation (`~T`) - is NOT
                        # interface embedding. Go only embeds a lone interface
                        # type; union/approximation terms can never be embedded,
                        # so they must not emit `embeds` heritage edges. Keep the
                        # type link as a `references` (type_constraint) edge.
                        is_type_set = any(
                            c.type == "|" or c.type == "negated_type"
                            for c in elem.children
                        )
                        refs = []
                        for sub in elem.children:
                            if sub.is_named:
                                _go_collect_type_refs(sub, source, False, refs)
                        for ref_name, role in refs:
                            tgt = ensure_named_node(ref_name, elem.start_point[0] + 1)
                            if tgt == type_nid:
                                continue
                            if is_type_set:
                                add_edge(type_nid, tgt, "references",
                                         elem.start_point[0] + 1, context="type_constraint")
                            elif role == "type":
                                add_edge(type_nid, tgt, "embeds",
                                         elem.start_point[0] + 1)
                            else:
                                add_edge(type_nid, tgt, "references",
                                         elem.start_point[0] + 1, context="generic_arg")
            return

        if t == "import_declaration":
            for child in node.children:
                if child.type == "import_spec_list":
                    for spec in child.children:
                        if spec.type == "import_spec":
                            path_node = spec.child_by_field_name("path")
                            if path_node:
                                raw = _read_text(path_node, source).strip('"')
                                # Prefix with go_pkg_ so stdlib names (e.g. "context")
                                # don't collide with local files of the same basename.
                                tgt_nid = _make_id("go", "pkg", raw)
                                add_edge(file_nid, tgt_nid, "imports_from", spec.start_point[0] + 1, context="import")
                                # Track local name (alias or last path segment)
                                alias = spec.child_by_field_name("name")
                                local_name = _read_text(alias, source) if alias else raw.split("/")[-1]
                                if local_name and local_name != "_" and local_name != ".":
                                    go_imported_pkgs[local_name] = raw
                elif child.type == "import_spec":
                    path_node = child.child_by_field_name("path")
                    if path_node:
                        raw = _read_text(path_node, source).strip('"')
                        tgt_nid = _make_id("go", "pkg", raw)
                        add_edge(file_nid, tgt_nid, "imports_from", child.start_point[0] + 1, context="import")
                        alias = child.child_by_field_name("name")
                        local_name = _read_text(alias, source) if alias else raw.split("/")[-1]
                        if local_name and local_name != "_" and local_name != ".":
                            go_imported_pkgs[local_name] = raw
            return

        for child in node.children:
            walk(child)

    _scan_declarations(root)
    walk(root)

    # Local change (Verinoda): the receiver types this file states (M4 of the D65 review): each struct's named
    # fields and embedded types, the type a file function returns (`func NewT() *T`), package-level
    # `var x = &T{}` / `T{}` / `NewT()` / `var x T`, and the same forms as locals of each function body (with
    # its parameters). A name given two types in one scope has none.
    struct_fields: dict[str, dict[str, str]] = {}
    struct_embedded: dict[str, list[str | None]] = {}   # None: an embedded type of another package
    func_returns: dict[str, str] = {}
    pkg_var_types: dict[str, str | None] = {}
    local_types_of: dict[str, dict[str, str | None]] = {}

    def _local_type(type_node) -> str | None:
        """`T`, `*T`, `T[int]` -> `T`; another package's type, a slice, map, func or interface -> None."""
        if type_node is not None and type_node.type == "pointer_type":
            type_node = next(iter(type_node.named_children), None)
        if type_node is not None and type_node.type == "generic_type":
            type_node = type_node.child_by_field_name("type")
        if type_node is None or type_node.type != "type_identifier":
            return None
        return _read_text(type_node, source)

    def _expr_type(expr) -> str | None:
        """The local type of `&T{}`, `T{}` or `NewT()` (a file function returning `T` or `*T`)."""
        if expr is not None and expr.type == "unary_expression" and _read_text(
                expr.child_by_field_name("operator"), source) == "&":
            expr = expr.child_by_field_name("operand")
        if expr is None:
            return None
        if expr.type == "composite_literal":
            return _local_type(expr.child_by_field_name("type"))
        if expr.type == "call_expression":
            fn = expr.child_by_field_name("function")
            if fn is not None and fn.type == "identifier":
                return func_returns.get(_read_text(fn, source))
        return None

    def _bind(table: dict, name: str, typ: str | None) -> None:
        if name and name != "_":
            table[name] = typ if table.get(name, typ) == typ else None

    def _bind_spec(table: dict, spec) -> None:
        """One `var_spec` / `short_var_declaration` / `const_spec` into *table*."""
        if spec.type == "short_var_declaration":
            left = spec.child_by_field_name("left")
            right = spec.child_by_field_name("right")
            names = [c for c in (left.named_children if left is not None else ()) if c.type == "identifier"]
            values = list(right.named_children) if right is not None else []
            declared = None
        else:
            names = [c for c in spec.children if c.type == "identifier"]
            value = spec.child_by_field_name("value")
            values = list(value.named_children) if value is not None else []
            declared = _local_type(spec.child_by_field_name("type"))
        for i, ident in enumerate(names):
            typ = declared
            if typ is None and len(values) == len(names):
                typ = _expr_type(values[i])
            _bind(table, _read_text(ident, source), typ)

    for top in root.children:
        if top.type == "function_declaration":
            fname = top.child_by_field_name("name")
            rtype = _local_type(top.child_by_field_name("result"))
            if fname is not None and rtype:
                func_returns[_read_text(fname, source)] = rtype
        elif top.type == "type_declaration":
            for spec in top.named_children:
                if spec.type != "type_spec":
                    continue
                tname = spec.child_by_field_name("name")
                stype = spec.child_by_field_name("type")
                if tname is None or stype is None or stype.type != "struct_type":
                    continue
                fields: dict[str, str] = {}
                embedded: list[str | None] = []
                flist = next((c for c in stype.named_children if c.type == "field_declaration_list"), None)
                for fd in (flist.named_children if flist is not None else ()):
                    if fd.type != "field_declaration":
                        continue
                    ftype = fd.child_by_field_name("type")
                    fnames = [c for c in fd.children if c.type == "field_identifier"]
                    if fnames:
                        local = _local_type(ftype)
                        for fn in fnames:
                            if local:
                                fields[_read_text(fn, source)] = local
                    else:
                        embedded.append(_local_type(ftype))
                struct_fields[_read_text(tname, source)] = fields
                struct_embedded[_read_text(tname, source)] = embedded
    for top in root.children:
        if top.type == "var_declaration":
            for spec in top.named_children:
                if spec.type == "var_spec":
                    _bind_spec(pkg_var_types, spec)

    def _collect_locals(n, table: dict) -> None:
        for child in n.children:
            if child.type in ("short_var_declaration", "var_spec"):
                _bind_spec(table, child)
            _collect_locals(child, table)

    for fnid, body in function_bodies:
        # every parameter shadows a package variable of its name; only those of a local type carry one
        table: dict[str, str | None] = {}
        params = body.parent.child_by_field_name("parameters") if body.parent is not None else None
        for param in (params.named_children if params is not None else ()):
            for child in param.children:
                if child.type == "identifier":
                    table[_read_text(child, source)] = None
        table.update(param_types_of.get(fnid, {}))
        own_name, own_type = own_receiver_of.get(fnid, (None, ""))
        if own_name:
            table[own_name] = own_type
        _collect_locals(body, table)
        local_types_of[fnid] = table

    def _method_of_type(typ: str, name: str) -> str | None:
        """*typ*'s method *name*, or the one a struct it embeds promotes (the shallowest depth, one candidate;
        none when an embedded type of another package at a shallower depth may define it)."""
        hit = methods_by_type.get((typ, name))
        if hit:
            return hit
        level, seen = [typ], {typ}
        while level:
            found: list[str] = []
            nxt: list[str] = []
            external = False
            for t in level:
                for emb in struct_embedded.get(t, ()):
                    if emb is None:
                        external = True
                    elif emb not in seen:
                        seen.add(emb)
                        nxt.append(emb)
                        if (emb, name) in methods_by_type:
                            found.append(methods_by_type[(emb, name)])
            if found:
                return found[0] if len(set(found)) == 1 else None
            if external:
                return None
            level = nxt
        return None

    def _receiver_type(operand, caller: str) -> str | None:
        """The local type of a call's receiver: a typed name of the caller's scope or the package, or a field
        of a typed receiver (`s.h`)."""
        if operand is None:
            return None
        if operand.type == "identifier":
            name = _read_text(operand, source)
            scope = local_types_of.get(caller, {})
            if name in scope:
                return scope[name]
            return pkg_var_types.get(name)
        if operand.type == "selector_expression":
            base = _receiver_type(operand.child_by_field_name("operand"), caller)
            field = operand.child_by_field_name("field")
            if base and field is not None:
                return struct_fields.get(base, {}).get(_read_text(field, source))
        if operand.type == "parenthesized_expression":
            return _receiver_type(next(iter(operand.named_children), None), caller)
        return None

    label_to_nid: dict[str, str] = {}
    bare_label_to_nid: dict[str, str] = {}  # without methods: a bare `f()` never calls a method
    for n in nodes:
        raw = n["label"]
        normalised = raw.strip("()").lstrip(".")
        label_to_nid[normalised] = n["id"]
        if not raw.startswith("."):
            bare_label_to_nid[normalised] = n["id"]

    seen_call_pairs: set[tuple[str, str]] = set()
    raw_calls: list[dict] = []

    def walk_calls(node, caller_nid: str) -> None:
        if node.type in ("function_declaration", "method_declaration"):
            return
        if node.type == "call_expression":
            func_node = node.child_by_field_name("function")
            callee_name: str | None = None
            is_member_call: bool = False
            is_bare_identifier: bool = False
            package_receiver: str | None = None
            import_path: str | None = None
            receiver_name = ""
            operand_node = None
            if func_node:
                if func_node.type == "identifier":
                    is_bare_identifier = True
                    callee_name = _read_text(func_node, source)
                elif func_node.type == "selector_expression":
                    field = func_node.child_by_field_name("field")
                    operand = func_node.child_by_field_name("operand")
                    operand_node = operand
                    receiver_name = _read_text(operand, source) if operand else ""
                    # Package-qualified call (e.g. fmt.Println) → allow cross-file resolution.
                    # Receiver method call (e.g. s.logger.Log) → skip, no import evidence.
                    is_member_call = receiver_name not in go_imported_pkgs
                    if not is_member_call:
                        package_receiver = receiver_name
                        import_path = go_imported_pkgs[receiver_name]
                    if field:
                        callee_name = _read_text(field, source)
            if is_bare_identifier and callee_name in _GO_PREDECLARED_FUNCS:
                # A bare `append(s, x)` is the builtin, never the same-named
                # method a sibling file happens to declare. Skipping before both
                # branches drops the in-file phantom edge and keeps the name out
                # of raw_calls, so the cross-file pass cannot bind it either.
                callee_name = None
            if callee_name and callee_name not in _LANGUAGE_BUILTIN_GLOBALS:
                # Never resolve an imported selector through a bare local name.
                if import_path:
                    tgt_nid = None
                elif is_member_call:
                    # a receiver whose type this file states: the method's own receiver, a parameter, a local
                    # or package variable of a local type, a field of one; a promoted method of an embedded struct
                    recv_type = _receiver_type(operand_node, caller_nid)
                    tgt_nid = _method_of_type(recv_type, callee_name) if recv_type else None
                elif is_bare_identifier:
                    tgt_nid = bare_label_to_nid.get(callee_name)
                else:
                    tgt_nid = label_to_nid.get(callee_name)
                if tgt_nid and tgt_nid != caller_nid:
                    pair = (caller_nid, tgt_nid)
                    if pair not in seen_call_pairs:
                        seen_call_pairs.add(pair)
                        line = node.start_point[0] + 1
                        edges.append({
                            "source": caller_nid,
                            "target": tgt_nid,
                            "relation": "calls",
                            "context": "call",
                            "confidence": "EXTRACTED",
                            "source_file": str_path,
                            "source_location": f"L{line}",
                            "weight": 1.0,
                        })
                elif callee_name:
                    raw_calls.append({
                        "caller_nid": caller_nid,
                        "callee": callee_name,
                        "is_member_call": is_member_call,
                        "language": "go",
                        "receiver": package_receiver,
                        "import_path": import_path,
                        "source_file": str_path,
                        "source_location": f"L{node.start_point[0] + 1}",
                    })
        for child in node.children:
            walk_calls(child, caller_nid)

    for caller_nid, body_node in function_bodies:
        walk_calls(body_node, caller_nid)

    valid_ids = seen_ids
    clean_edges = []
    for edge in edges:
        src, tgt = edge["source"], edge["target"]
        if src in valid_ids and (tgt in valid_ids or edge["relation"] in ("imports", "imports_from")):
            clean_edges.append(edge)

    return {
        "nodes": nodes,
        "edges": clean_edges,
        "raw_calls": raw_calls,
        "go_imports": dict(go_imported_pkgs),
    }
