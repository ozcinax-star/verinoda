"""Rust extractor. Moved verbatim from graphify/extract.py."""
from __future__ import annotations


from pathlib import Path
from verinoda.project_index.extractors.base import _LANGUAGE_BUILTIN_GLOBALS, _file_stem, _make_id, _read_text


def _rust_collect_type_refs(node, source: bytes, generic: bool, out: list[tuple[str, str]]) -> None:
    """Walk a Rust type expression; append (name, role) tuples."""
    if node is None:
        return
    t = node.type
    if t == "primitive_type":
        return
    if t == "type_identifier":
        text = _read_text(node, source)
        if text:
            out.append((text, "generic_arg" if generic else "type"))
        return
    if t == "scoped_type_identifier":
        text = _read_text(node, source).rsplit("::", 1)[-1]
        if text:
            out.append((text, "generic_arg" if generic else "type"))
        return
    if t == "generic_type":
        name_node = node.child_by_field_name("type")
        if name_node is None:
            for c in node.children:
                if c.type in ("type_identifier", "scoped_type_identifier"):
                    name_node = c
                    break
        if name_node is not None:
            text = _read_text(name_node, source).rsplit("::", 1)[-1]
            if text:
                out.append((text, "generic_arg" if generic else "type"))
        for c in node.children:
            if c.type == "type_arguments":
                for arg in c.children:
                    if arg.is_named:
                        _rust_collect_type_refs(arg, source, True, out)
        return
    if t in ("reference_type", "pointer_type", "array_type", "tuple_type", "slice_type"):
        for c in node.children:
            if c.is_named:
                _rust_collect_type_refs(c, source, generic, out)
        return
    if node.is_named:
        for c in node.children:
            if c.is_named:
                _rust_collect_type_refs(c, source, generic, out)


def _rust_simple_generic_impl_key(node, source: bytes) -> str | None:
    """Return a stable owner/arity key for a deliberately narrow impl shape."""
    if node.child_by_field_name("trait") is not None:
        return None
    parameters = node.child_by_field_name("type_parameters")
    owner_type = node.child_by_field_name("type")
    if parameters is None or owner_type is None or owner_type.type != "generic_type":
        return None
    if any(child.type == "where_clause" for child in node.named_children):
        return None

    parameter_names: list[str] = []
    for parameter in parameters.named_children:
        if parameter.type != "type_parameter":
            return None
        named = parameter.named_children
        if len(named) != 1 or named[0].type != "type_identifier":
            return None
        name = _read_text(named[0], source)
        if not name or name in parameter_names:
            return None
        parameter_names.append(name)
    if not parameter_names:
        return None

    owner = owner_type.child_by_field_name("type")
    if owner is None or owner.type != "type_identifier":
        return None
    arguments = next(
        (child for child in owner_type.named_children if child.type == "type_arguments"),
        None,
    )
    if arguments is None:
        return None
    argument_names = [
        _read_text(argument, source)
        for argument in arguments.named_children
        if argument.type == "type_identifier"
    ]
    if len(argument_names) != len(arguments.named_children):
        return None
    if argument_names != parameter_names:
        return None

    owner_name = _read_text(owner, source)
    return f"{owner_name}/{len(parameter_names)}" if owner_name else None


_RUST_TRAIT_METHOD_BLOCKLIST: frozenset[str] = frozenset({
    "new", "default", "parse", "from_str", "now", "clone", "into", "from",
    "to_string", "to_owned", "len", "is_empty", "iter", "next", "build",
    "start", "run", "init", "app", "get", "set", "push", "pop", "insert",
    "remove", "contains", "collect", "map", "filter", "unwrap", "expect",
    "ok", "err", "some", "none", "send", "recv", "lock", "read", "write",
})

# Verinoda patch: what a file states about the type of a call's receiver.
#
# `Type::m()` names its type; `x.m()` has one when `x` is a parameter of a stated type (`x: &T`, `x: &mut dyn Tr`,
# `x: Box<dyn Tr>`, `x: impl Tr`), or a `let` of a stated type (`let x: T`), a struct literal (`T { .. }`) or a
# constructor (`T::f(..)` whose declaration returns `Self` or `T`). A trait object or `impl Trait` names the
# trait: the call binds to the trait's declaration of the method, a lead to its implementations, never to one
# of them. Where the type's name comes from is kept as its *roots*: the first segment of the path it was written
# or imported with (`crate`, `self`, `super`, a crate or module name), so the corpus pass binds only a type the
# corpus defines, never a same-named type of another crate (`std::process::Command`).
_RUST_WRAPPERS = frozenset({"Box", "Rc", "Arc"})


def _rust_use_map(root, source: bytes) -> tuple[dict[str, list[str]], list[str]]:
    """The names a file's `use` declarations bring in (name -> written path segments) and the first segments
    of its glob imports (`use super::*` -> `super`)."""
    names: dict[str, list[str]] = {}
    globs: list[str] = []

    def segments(node) -> list[str]:
        text = _read_text(node, source).replace(" ", "")
        return [s for s in text.split("::") if s]

    def visit(node, prefix: list[str]) -> None:
        t = node.type
        if t in ("identifier", "scoped_identifier", "crate", "self", "super"):
            segs = prefix + segments(node)
            if segs:
                names[segs[-1]] = segs
        elif t == "use_as_clause":
            path = node.child_by_field_name("path")
            alias = node.child_by_field_name("alias")
            if path is not None and alias is not None:
                names[_read_text(alias, source)] = prefix + segments(path)
        elif t == "use_wildcard":
            inner = next(iter(node.named_children), None)
            segs = prefix + (segments(inner) if inner is not None else [])
            if segs:
                globs.append(segs[0])
        elif t == "scoped_use_list":
            path = node.child_by_field_name("path")
            lst = node.child_by_field_name("list")
            sub = prefix + (segments(path) if path is not None else [])
            for c in (lst.named_children if lst is not None else ()):
                visit(c, sub)
        elif t == "use_list":
            for c in node.named_children:
                visit(c, prefix)

    def walk(node) -> None:   # a `use` in a function or module body counts for the whole file
        for c in node.children:
            if c.type == "use_declaration":
                arg = c.child_by_field_name("argument")
                if arg is not None:
                    visit(arg, [])
            elif c.named_child_count:
                walk(c)
    walk(root)
    return names, globs


def _rust_path_segments(node, source: bytes) -> list[str]:
    """`crate::m::T::<u8>` -> [crate, m, T] (generic arguments dropped)."""
    text, depth = "", 0
    for ch in _read_text(node, source):
        depth += (ch == "<") - (ch == ">")
        if depth == 0 and ch != ">":
            text += ch
    return [s.strip() for s in text.split("::") if s.strip()]


def _rust_generic_names(node, source: bytes) -> set[str]:
    """The type parameters of the function a body belongs to and of the impl or trait around it."""
    names: set[str] = set()
    cur = node
    while cur is not None:
        if cur.type in ("function_item", "impl_item", "trait_item"):
            tp = cur.child_by_field_name("type_parameters")
            for p in (tp.named_children if tp is not None else ()):
                if p.type in ("type_parameter", "constrained_type_parameter", "optional_type_parameter"):
                    name = p.child_by_field_name("name")
                    if name is None:
                        name = next((c for c in p.named_children if c.type == "type_identifier"), None)
                    if name is not None:
                        names.add(_read_text(name, source))
        cur = cur.parent
    return names


def _rust_type_desc(type_node, source: bytes) -> tuple[str, list[str]] | None:
    """(kind, path segments) of a stated type: ``("type", [.., "T"])`` for `T`, `&T`, `&mut T`, `T<'a>`,
    `m::T`; ``("dyn", [.., "Tr"])`` for `dyn Tr`, `impl Tr` and `Box`/`Rc`/`Arc` of one. None otherwise."""
    node = type_node
    while node is not None and node.type == "reference_type":
        node = node.child_by_field_name("type")
    if node is None:
        return None
    if node.type in ("dynamic_type", "abstract_type"):
        trait = node.child_by_field_name("trait")
        if trait is not None and trait.type == "generic_type":
            trait = trait.child_by_field_name("type")
        if trait is None or trait.type not in ("type_identifier", "scoped_type_identifier"):
            return None
        return ("dyn", _rust_path_segments(trait, source))
    if node.type == "generic_type":
        base = node.child_by_field_name("type")
        if base is not None and _read_text(base, source) in _RUST_WRAPPERS:
            args = node.child_by_field_name("type_arguments")
            inner = [a for a in (args.named_children if args is not None else ()) if a.type != "lifetime"]
            if len(inner) == 1 and inner[0].type in ("dynamic_type", "abstract_type"):
                return _rust_type_desc(inner[0], source)
            return None
        node = base
    if node is None or node.type not in ("type_identifier", "scoped_type_identifier"):
        return None
    return ("type", _rust_path_segments(node, source))


def extract_rust(path: Path) -> dict:
    """Extract functions, structs, enums, traits, impl methods, statics/consts, and use declarations from a .rs file."""
    try:
        import tree_sitter_rust as tsrust
        from tree_sitter import Language, Parser
    except ImportError:
        return {"nodes": [], "edges": [], "error": "tree-sitter-rust not installed"}

    try:
        language = Language(tsrust.language())
        parser = Parser(language)
        source = path.read_bytes()
        tree = parser.parse(source)
        root = tree.root_node
    except Exception as e:
        return {"nodes": [], "edges": [], "error": str(e)}

    stem = _file_stem(path)
    str_path = str(path)
    nodes: list[dict] = []
    edges: list[dict] = []
    seen_ids: set[str] = set()
    function_bodies: list[tuple[str, object, str | None, str | None]] = []
    # Local change (Verinoda): the methods of each impl type (or trait) of this file and each method's owner, so
    # `self.m()` / `Self::m()` / `Type::m()` bind to that type's `m` and no other member call binds to a same-named
    # function of the file.
    methods_by_owner: dict[tuple[str, str], str] = {}
    owner_of: dict[str, str] = {}
    impl_keys: dict[str, str | None] = {}

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
        nid = _make_id(stem, name)
        if nid in seen_ids:
            return nid
        nid = _make_id(name)
        if nid not in seen_ids:
            # The name isn't defined in this file, so this is a cross-file reference
            # (e.g. a `Thing` type annotation imported from another module). Emit a
            # SOURCELESS stub — like the inheritance-base path below — so the
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

    def emit_param_return_refs(func_node, func_nid: str, line: int) -> None:
        params = func_node.child_by_field_name("parameters")
        if params is not None:
            for p in params.children:
                if p.type != "parameter":
                    continue
                type_node = p.child_by_field_name("type")
                refs: list[tuple[str, str]] = []
                _rust_collect_type_refs(type_node, source, False, refs)
                for ref_name, role in refs:
                    ctx = "generic_arg" if role == "generic_arg" else "parameter_type"
                    tgt = ensure_named_node(ref_name, line)
                    if tgt != func_nid:
                        add_edge(func_nid, tgt, "references", line, context=ctx)
        return_type = func_node.child_by_field_name("return_type")
        if return_type is not None:
            refs = []
            _rust_collect_type_refs(return_type, source, False, refs)
            for ref_name, role in refs:
                ctx = "generic_arg" if role == "generic_arg" else "return_type"
                tgt = ensure_named_node(ref_name, line)
                if tgt != func_nid:
                    add_edge(func_nid, tgt, "references", line, context=ctx)

    def walk(
        node,
        parent_impl_nid: str | None = None,
        parent_impl_type: str | None = None,
        parent_impl_key: str | None = None,
    ) -> None:
        t = node.type

        if t == "function_item":
            name_node = node.child_by_field_name("name")
            if name_node:
                func_name = _read_text(name_node, source)
                line = node.start_point[0] + 1
                if parent_impl_nid:
                    func_nid = _make_id(parent_impl_nid, func_name)
                    add_node(func_nid, f".{func_name}()", line)
                    add_edge(parent_impl_nid, func_nid, "method", line)
                    owner = parent_impl_type or parent_impl_nid
                    methods_by_owner.setdefault((owner, func_name), func_nid)
                    owner_of[func_nid] = owner
                    # Verinoda patch: a constructor (`-> Self`, `-> T` in `impl T`) types `let x = T::f(..)`
                    ret = _rust_type_desc(node.child_by_field_name("return_type"), source)
                    if ret and ret[0] == "type" and len(ret[1]) == 1 and ret[1][0] in ("Self", parent_impl_type):
                        hit = next((n for n in reversed(nodes) if n["id"] == func_nid), None)
                        if hit is not None:
                            hit["_rust_returns_self"] = True
                else:
                    func_nid = _make_id(stem, func_name)
                    add_node(func_nid, f"{func_name}()", line)
                    add_edge(file_nid, func_nid, "contains", line)
                emit_param_return_refs(node, func_nid, line)
                body = node.child_by_field_name("body")
                if body:
                    function_bodies.append((
                        func_nid,
                        body,
                        parent_impl_type,
                        parent_impl_key,
                    ))
            return

        if t == "function_signature_item":
            # `fn greet(&self) -> String;` — a trait method with no body. This node type
            # had no branch at all, so a trait's required methods were unreachable: the
            # only method nodes in a graph came from impl blocks, and a trait with no
            # implementor in the corpus contributed none. Mirrors function_item, minus
            # the body walk (there is no body).
            name_node = node.child_by_field_name("name")
            if name_node:
                func_name = _read_text(name_node, source)
                line = node.start_point[0] + 1
                if parent_impl_nid:
                    func_nid = _make_id(parent_impl_nid, func_name)
                    add_node(func_nid, f".{func_name}()", line)
                    add_edge(parent_impl_nid, func_nid, "method", line)
                    methods_by_owner.setdefault((parent_impl_type or parent_impl_nid, func_name), func_nid)
                else:
                    func_nid = _make_id(stem, func_name)
                    add_node(func_nid, f"{func_name}()", line)
                    add_edge(file_nid, func_nid, "contains", line)
                emit_param_return_refs(node, func_nid, line)
            return

        if t in ("struct_item", "enum_item", "trait_item"):
            name_node = node.child_by_field_name("name")
            if name_node:
                item_name = _read_text(name_node, source)
                line = node.start_point[0] + 1
                item_nid = _make_id(stem, item_name)
                add_node(item_nid, item_name, line)
                declaration_node = next(n for n in nodes if n["id"] == item_nid)
                declaration_node["_rust_declaration_count"] = (
                    declaration_node.get("_rust_declaration_count", 0) + 1
                )
                add_edge(file_nid, item_nid, "contains", line)
                if t == "trait_item":
                    for c in node.children:
                        if c.type != "trait_bounds":
                            continue
                        for sub in c.children:
                            if not sub.is_named:
                                continue
                            refs: list[tuple[str, str]] = []
                            _rust_collect_type_refs(sub, source, False, refs)
                            for idx, (ref_name, _role) in enumerate(refs):
                                tgt = ensure_named_node(ref_name, line)
                                if tgt == item_nid:
                                    continue
                                rel = "inherits" if idx == 0 else "references"
                                if rel == "inherits":
                                    add_edge(item_nid, tgt, "inherits", line)
                                else:
                                    add_edge(item_nid, tgt, "references", line,
                                             context="generic_arg")
                if t == "struct_item":
                    for c in node.children:
                        if c.type != "field_declaration_list":
                            continue
                        for field in c.children:
                            if field.type != "field_declaration":
                                continue
                            type_node = field.child_by_field_name("type")
                            if type_node is None:
                                for fc in field.children:
                                    if fc.type in ("type_identifier", "generic_type",
                                                    "scoped_type_identifier",
                                                    "reference_type", "primitive_type"):
                                        type_node = fc
                                        break
                            refs = []
                            _rust_collect_type_refs(type_node, source, False, refs)
                            for ref_name, role in refs:
                                ctx = "generic_arg" if role == "generic_arg" else "field"
                                tgt = ensure_named_node(ref_name, field.start_point[0] + 1)
                                if tgt != item_nid:
                                    add_edge(item_nid, tgt, "references",
                                             field.start_point[0] + 1, context=ctx)
                    # Tuple structs (`struct Wrapper(pub Logger, Config);`) nest their
                    # positional field types directly under ordered_field_declaration_list
                    # with no field_declaration wrapper -- the same shape handled for tuple
                    # enum variants below. Without this branch these field type references
                    # are silently dropped.
                    for c in node.children:
                        if c.type != "ordered_field_declaration_list":
                            continue
                        fline = c.start_point[0] + 1
                        for tc in c.children:
                            if tc.type not in ("type_identifier", "generic_type",
                                               "scoped_type_identifier", "reference_type",
                                               "primitive_type", "tuple_type", "array_type"):
                                continue
                            refs = []
                            _rust_collect_type_refs(tc, source, False, refs)
                            for ref_name, role in refs:
                                ctx = "generic_arg" if role == "generic_arg" else "field"
                                tgt = ensure_named_node(ref_name, fline)
                                if tgt != item_nid:
                                    add_edge(item_nid, tgt, "references", fline, context=ctx)
                if t == "enum_item":
                    # Variant payload types nest under enum_variant_list ->
                    # enum_variant -> ordered_field_declaration_list (tuple variant,
                    # `Click(Logger)`) | field_declaration_list (struct variant,
                    # `Resize { size: Dim }`). Neither was traversed, so every
                    # enum-variant type reference was silently dropped.
                    _TYPE_NODES = ("type_identifier", "generic_type",
                                   "scoped_type_identifier", "reference_type",
                                   "primitive_type", "tuple_type", "array_type")

                    def _emit_enum_type(type_node, at_line):
                        if type_node is None:
                            return
                        refs2: list[tuple[str, str]] = []
                        _rust_collect_type_refs(type_node, source, False, refs2)
                        for ref_name, role in refs2:
                            ctx = "generic_arg" if role == "generic_arg" else "field"
                            tgt = ensure_named_node(ref_name, at_line)
                            if tgt != item_nid:
                                add_edge(item_nid, tgt, "references", at_line, context=ctx)

                    for c in node.children:
                        if c.type != "enum_variant_list":
                            continue
                        for variant in c.children:
                            if variant.type != "enum_variant":
                                continue
                            vline = variant.start_point[0] + 1
                            for vc in variant.children:
                                if vc.type == "ordered_field_declaration_list":
                                    for tc in vc.children:
                                        if tc.type in _TYPE_NODES:
                                            _emit_enum_type(tc, vline)
                                elif vc.type == "field_declaration_list":
                                    for field in vc.children:
                                        if field.type != "field_declaration":
                                            continue
                                        type_node = field.child_by_field_name("type")
                                        _emit_enum_type(type_node, field.start_point[0] + 1)
                if t == "trait_item":
                    # The methods a trait declares are its contract, and they were not
                    # reached: this branch returns below, so nothing walked the body.
                    # Descend the same way impl_item does, attributing each method to
                    # the trait node — so `explain <Trait>` can list what an
                    # implementor must provide.
                    body = node.child_by_field_name("body")
                    if body:
                        for child in body.children:
                            walk(child, parent_impl_nid=item_nid)
            return

        if t in ("static_item", "const_item"):
            # `static NAME: T = …;` / `const NAME: T = …;` at module level, or an
            # associated const inside an impl. Neither node type had a branch, so a
            # constant reached the graph only through files that referenced it,
            # never from the Rust that defines it (#3471).
            name_node = node.child_by_field_name("name")
            if name_node:
                item_name = _read_text(name_node, source)
                line = node.start_point[0] + 1
                if parent_impl_nid:
                    item_nid = _make_id(parent_impl_nid, item_name)
                    add_node(item_nid, f".{item_name}", line)
                    add_edge(parent_impl_nid, item_nid, "contains", line)
                else:
                    item_nid = _make_id(stem, item_name)
                    add_node(item_nid, item_name, line)
                    add_edge(file_nid, item_nid, "contains", line)
                type_node = node.child_by_field_name("type")
                if type_node is not None:
                    refs: list[tuple[str, str]] = []
                    _rust_collect_type_refs(type_node, source, False, refs)
                    for ref_name, role in refs:
                        tgt = ensure_named_node(ref_name, line)
                        if tgt == item_nid:
                            continue
                        ctx = "generic_arg" if role == "generic_arg" else "field"
                        add_edge(item_nid, tgt, "references", line, context=ctx)
            return

        if t == "impl_item":
            type_node = node.child_by_field_name("type")
            trait_node = node.child_by_field_name("trait")
            impl_nid: str | None = None
            impl_type_bare: str | None = None
            impl_key: str | None = None
            if type_node:
                type_name = _read_text(type_node, source).strip()
                impl_nid = _make_id(stem, type_name)
                add_node(impl_nid, type_name, node.start_point[0] + 1)
                impl_key = _rust_simple_generic_impl_key(node, source)
                # Bare name (generics stripped) for typing a `self.` receiver
                # inside this block's methods (#2234) — `impl Foo<T>` types
                # `self` as `Foo`, not the literal `Foo<T>` text.
                impl_type_bare = type_name.split("<")[0].strip()
            if trait_node is not None and impl_nid is not None:
                refs: list[tuple[str, str]] = []
                _rust_collect_type_refs(trait_node, source, False, refs)
                for idx, (ref_name, _role) in enumerate(refs):
                    tgt = ensure_named_node(ref_name, node.start_point[0] + 1)
                    if tgt == impl_nid:
                        continue
                    if idx == 0:
                        add_edge(impl_nid, tgt, "implements", node.start_point[0] + 1)
                    else:
                        add_edge(impl_nid, tgt, "references", node.start_point[0] + 1,
                                 context="generic_arg")
            body = node.child_by_field_name("body")
            if body:
                has_methods = any(
                    child.type in ("function_item", "function_signature_item")
                    for child in body.children
                )
                if impl_nid is not None and has_methods:
                    if impl_nid not in impl_keys:
                        impl_keys[impl_nid] = impl_key
                    elif impl_keys[impl_nid] != impl_key:
                        impl_keys[impl_nid] = None
                    impl_node = next(n for n in nodes if n["id"] == impl_nid)
                    if impl_keys[impl_nid]:
                        impl_node["_rust_impl_key"] = impl_keys[impl_nid]
                    else:
                        impl_node.pop("_rust_impl_key", None)
                for child in body.children:
                    walk(
                        child,
                        parent_impl_nid=impl_nid,
                        parent_impl_type=impl_type_bare,
                        parent_impl_key=impl_key,
                    )
            return

        if t == "use_declaration":
            arg = node.child_by_field_name("argument")
            if arg:
                raw = _read_text(arg, source)
                clean = raw.split("{")[0].rstrip(":").rstrip("*").rstrip(":")
                module_name = clean.split("::")[-1].strip()
                if module_name:
                    tgt_nid = _make_id(module_name)
                    add_edge(file_nid, tgt_nid, "imports_from", node.start_point[0] + 1, context="import")
            return

        for child in node.children:
            walk(child, parent_impl_nid=None)

    walk(root)

    label_to_nid: dict[str, str] = {}
    bare_label_to_nid: dict[str, str] = {}  # without methods: a bare `f()` or `module::f()` never calls a method
    for n in nodes:
        raw = n["label"]
        normalised = raw.strip("()").lstrip(".")
        label_to_nid[normalised] = n["id"]
        if not raw.startswith("."):
            bare_label_to_nid[normalised] = n["id"]

    seen_call_pairs: set[tuple[str, str]] = set()
    raw_calls: list[dict] = []

    # Verinoda patch: receiver types (see `_rust_type_desc`): the file's imports, the types it declares, and
    # the type each parameter and `let` of a function states. A name bound twice in one function with
    # different types, or by a pattern, a closure parameter, a `for`, `match` or `if let`, has none.
    use_names, use_globs = _rust_use_map(root, source)
    declared_here: set[str] = set()
    trait_nid_of: dict[str, str] = {}
    returns_self = {n["id"] for n in nodes if n.get("_rust_returns_self")}

    def _declared(node) -> None:
        for c in node.children:
            if c.type in ("struct_item", "enum_item", "trait_item", "type_item", "union_item"):
                name = c.child_by_field_name("name")
                if name is not None:
                    declared_here.add(_read_text(name, source))
                    if c.type == "trait_item":
                        trait_nid_of[_read_text(name, source)] = _make_id(stem, _read_text(name, source))
            elif c.type in ("mod_item", "declaration_list"):
                _declared(c)
    _declared(root)

    def _roots(segs: list[str]) -> list[str] | None:
        """Where a type's name comes from: the first segment of its written or imported path; `self` for a type
        this file declares; the roots of the glob imports for any other name. None: not from this crate's
        source (the prelude, or nothing this file imports)."""
        if len(segs) >= 2:
            first = use_names.get(segs[0])   # `use std::fmt;` ... `fmt::Write::write_str`
            return [first[0]] if first and len(first) >= 2 else [segs[0]]
        name = segs[-1]
        if name in declared_here:
            return ["self"]
        if name in use_names:
            return [use_names[name][0]] if len(use_names[name]) >= 2 else None
        return sorted(set(use_globs)) or None

    def _pattern_names(pat) -> list[str]:
        out: list[str] = []
        stack = [pat]
        while stack:
            n = stack.pop()
            if n.type in ("identifier", "shorthand_field_identifier"):   # `Foo { a, .. }` binds `a`
                out.append(_read_text(n, source))
            stack.extend(n.named_children)
        return out

    def _value_desc(value) -> tuple | None:
        while value is not None and value.type in ("reference_expression", "parenthesized_expression"):
            value = value.child_by_field_name("value") or next(iter(value.named_children), None)
        if value is None:
            return None
        if value.type == "struct_expression":
            name = value.child_by_field_name("name")
            if name is not None and name.type == "generic_type_with_turbofish":
                name = name.child_by_field_name("type")
            if name is not None and name.type in ("type_identifier", "scoped_type_identifier"):
                return ("type", _rust_path_segments(name, source))
            return None
        if value.type == "call_expression":
            fn = value.child_by_field_name("function")
            if fn is not None and fn.type == "scoped_identifier":
                path_node = fn.child_by_field_name("path")
                name = fn.child_by_field_name("name")
                segs = _rust_path_segments(path_node, source) if path_node is not None else []
                if segs and name is not None and segs[-1][:1].isupper():
                    return ("ret", segs, _read_text(name, source))
        return None

    def _bindings(body) -> dict[str, tuple | None]:
        table: dict[str, tuple | None] = {}

        def bind(name: str, desc) -> None:
            if name and name != "_":
                table[name] = desc if table.get(name, desc) == desc else None

        fn = body.parent
        params = fn.child_by_field_name("parameters") if fn is not None else None
        for p in (params.named_children if params is not None else ()):
            if p.type != "parameter":
                continue
            pat = p.child_by_field_name("pattern")
            if pat is not None and pat.type == "identifier":
                bind(_read_text(pat, source), _rust_type_desc(p.child_by_field_name("type"), source))
            elif pat is not None:
                for name in _pattern_names(pat):
                    bind(name, None)

        def visit(n) -> None:
            for c in n.children:
                t = c.type
                if t == "function_item":
                    continue
                if t == "let_declaration":
                    pat = c.child_by_field_name("pattern")
                    if pat is not None and pat.type == "identifier":
                        typ = c.child_by_field_name("type")
                        desc = _rust_type_desc(typ, source) if typ is not None else \
                            _value_desc(c.child_by_field_name("value"))
                        bind(_read_text(pat, source), desc)
                    elif pat is not None:
                        for name in _pattern_names(pat):
                            bind(name, None)
                elif t in ("closure_parameters", "match_pattern"):
                    for name in _pattern_names(c):
                        bind(name, None)
                elif t in ("for_expression", "let_condition", "let_chain"):
                    pat = c.child_by_field_name("pattern")
                    if pat is not None:
                        for name in _pattern_names(pat):
                            bind(name, None)
                visit(c)
        visit(body)
        return table

    bindings_of: dict[str, dict] = {}

    def _call_edge(caller_nid: str, tgt_nid: str, node, confidence: str = "EXTRACTED",
                   score: float | None = None) -> None:
        pair = (caller_nid, tgt_nid)
        if tgt_nid == caller_nid or pair in seen_call_pairs:
            return
        seen_call_pairs.add(pair)
        edge = {
            "source": caller_nid,
            "target": tgt_nid,
            "relation": "calls",
            "context": "call",
            "confidence": confidence,
            "source_file": str_path,
            "source_location": f"L{node.start_point[0] + 1}",
            "weight": 1.0,
        }
        if score is not None:
            edge["confidence_score"] = score
        edges.append(edge)

    def _typed_member_call(node, caller_nid: str, callee: str, desc, own: str | None, generic: set[str]) -> bool:
        """Bind (or leave to the corpus pass) `x.callee()` on a receiver of a stated type; False when the
        receiver has none."""
        kind, segs = desc[0], desc[1]
        name = own if segs[-1] == "Self" and len(segs) == 1 else segs[-1]
        if not name or name in generic or name == "Self":
            return False
        roots = ["self"] if name == own else _roots(segs)
        via, ctor = "local", None
        if kind == "dyn":
            tgt = methods_by_owner.get((trait_nid_of.get(name, ""), callee))
            if tgt:
                _call_edge(caller_nid, tgt, node, "INFERRED", 0.75)
                return True
            via = "dyn"
        elif kind == "ret":
            ctor_nid = methods_by_owner.get((name, desc[2]))
            if ctor_nid is None:
                via, ctor = "ret", desc[2]
            elif ctor_nid not in returns_self:
                return False
        if via == "local":
            tgt = methods_by_owner.get((name, callee))
            if tgt:
                _call_edge(caller_nid, tgt, node, "INFERRED", 0.85)
                return True
        if not roots:
            return False
        rc_entry = {
            "caller_nid": caller_nid,
            "callee": callee,
            "is_member_call": True,
            "source_file": str_path,
            "source_location": f"L{node.start_point[0] + 1}",
            "rust_type": name,
            "rust_via": via,
            "rust_roots": roots,
        }
        if ctor:
            rc_entry["rust_ctor"] = ctor
        raw_calls.append(rc_entry)
        return True

    def walk_calls(
        node,
        caller_nid: str,
        self_type: str | None = None,
        self_impl_key: str | None = None,
        generic: set[str] | None = None,
    ) -> None:
        if node.type == "function_item":
            return
        if node.type == "call_expression":
            func_node = node.child_by_field_name("function")
            callee_name: str | None = None
            is_member_call: bool = False
            is_scoped_call: bool = False
            is_self_call: bool = False
            scope_type: str | None = None  # `Type` of `Type::m()`, `Self` resolved to the caller's owner
            scope_segs: list[str] = []
            receiver = None
            if func_node:
                if func_node.type == "identifier":
                    callee_name = _read_text(func_node, source)
                elif func_node.type == "field_expression":
                    is_member_call = True
                    field = func_node.child_by_field_name("field")
                    if field:
                        callee_name = _read_text(field, source)
                    receiver = func_node.child_by_field_name("value")
                    if receiver is not None and receiver.type == "self":
                        is_self_call = True
                elif func_node.type == "scoped_identifier":
                    # Type::method() — still allow in-file EXTRACTED match, but
                    # skip cross-file resolution: bare last-segment lookup ignores
                    # crate boundaries and produces spurious INFERRED edges (#908).
                    # Verinoda patch: ... unless the corpus pass can tell the type is this crate's (`rust_roots`).
                    is_scoped_call = True
                    name = func_node.child_by_field_name("name")
                    if name:
                        callee_name = _read_text(name, source)
                    path_node = func_node.child_by_field_name("path")
                    if path_node is not None:
                        # generic arguments dropped first: the turbofish `Pool::<u8>::new()` is Pool's `new`
                        scope_segs = _rust_path_segments(path_node, source)
                        last = scope_segs[-1] if scope_segs else ""
                        if last == "Self":
                            scope_type = owner_of.get(caller_nid) or self_type or ""
                        elif last[:1].isupper():
                            scope_type = last
            # Verinoda patch: a receiver of a stated type (a parameter or `let` of this function)
            desc = None
            if is_member_call and not is_self_call and receiver is not None and receiver.type == "identifier":
                desc = bindings_of.get(caller_nid, {}).get(_read_text(receiver, source))
            if callee_name and (callee_name not in _LANGUAGE_BUILTIN_GLOBALS or desc):
                if is_member_call:
                    # only `self.m()` binds in this file, to the caller's own type's `m`
                    own = owner_of.get(caller_nid) or self_type
                    tgt_nid = methods_by_owner.get((own, callee_name)) if is_self_call and own else None
                elif scope_type is not None:
                    tgt_nid = methods_by_owner.get((scope_type, callee_name))
                else:
                    tgt_nid = bare_label_to_nid.get(callee_name)
                if desc and not tgt_nid:
                    if _typed_member_call(node, caller_nid, callee_name, desc, self_type, generic or set()):
                        callee_name = None
                if not callee_name:
                    pass
                elif tgt_nid and tgt_nid != caller_nid:
                    _call_edge(caller_nid, tgt_nid, node)
                elif (is_scoped_call and not tgt_nid and scope_type and scope_segs
                      and scope_segs[-1] != "Self" and scope_type not in (generic or set())):
                    # Verinoda patch: `Type::m()` of a type another file of this crate defines
                    roots = _roots(scope_segs)
                    if roots:
                        raw_calls.append({
                            "caller_nid": caller_nid,
                            "callee": callee_name,
                            "is_member_call": True,
                            "source_file": str_path,
                            "source_location": f"L{node.start_point[0] + 1}",
                            "rust_type": scope_type,
                            "rust_via": "path",
                            "rust_roots": roots,
                            # only a glob import names the type: INFERRED
                            "rust_glob": len(scope_segs) == 1 and scope_type not in declared_here
                            and scope_type not in use_names,
                        })
                elif is_scoped_call and not tgt_nid and scope_segs and scope_segs[-1] == "Self" and self_type:
                    # Verinoda patch: `Self::m()` of an impl block in another file, as `self.m()`
                    rc_entry = {
                        "caller_nid": caller_nid,
                        "callee": callee_name,
                        "is_member_call": True,
                        "source_file": str_path,
                        "source_location": f"L{node.start_point[0] + 1}",
                        "rust_self_type": self_type,
                    }
                    if self_impl_key:
                        rc_entry["rust_self_impl_key"] = self_impl_key
                    raw_calls.append(rc_entry)
                elif not is_scoped_call and callee_name.lower() not in _RUST_TRAIT_METHOD_BLOCKLIST:
                    rc_entry = {
                        "caller_nid": caller_nid,
                        "callee": callee_name,
                        "is_member_call": is_member_call,
                        "source_file": str_path,
                        "source_location": f"L{node.start_point[0] + 1}",
                    }
                    if is_self_call and self_type:
                        rc_entry["rust_self_type"] = self_type
                        if self_impl_key:
                            rc_entry["rust_self_impl_key"] = self_impl_key
                    raw_calls.append(rc_entry)
        for child in node.children:
            walk_calls(child, caller_nid, self_type, self_impl_key, generic)

    for caller_nid, body_node, impl_type, impl_key in function_bodies:
        bindings_of[caller_nid] = _bindings(body_node)
        walk_calls(body_node, caller_nid, impl_type, impl_key, _rust_generic_names(body_node, source))

    valid_ids = seen_ids
    clean_edges = []
    for edge in edges:
        src, tgt = edge["source"], edge["target"]
        if src in valid_ids and (tgt in valid_ids or edge["relation"] in ("imports", "imports_from")):
            clean_edges.append(edge)

    return {"nodes": nodes, "edges": clean_edges, "raw_calls": raw_calls}
