"""Go extractor. Moved verbatim from graphify/extract.py."""
from __future__ import annotations


import hashlib
import os
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

# Verinoda patch: receiver types of member calls, as far as the package's source states them.
#
# A receiver is described by a *chain*: a start and the steps applied to it, so a call can be typed in its own
# file when that file states every type on the way, and otherwise by the package pass
# (`resolve_go_receiver_calls`), which reads every file of the package. A start is `["v", value]` (a stated type),
# `["f", F, i]` (the i-th result of the package function F, or a conversion to the package type F) or
# `["g", name]` (a package-level name of another file: a variable, or a type in a method expression).
# A step is `[".", field]`, `["[]"]` (an element of a slice, array or map) or `["()", method, i]` (the i-th
# result of a method). A value is a type name of the package (`T`, from `T`, `*T` or `T[int]`) or
# `["[]", value]` (a slice, array or map of one). Another package's type, a type parameter, a func, a channel or
# an interface literal is no value: a chain through one types nothing, and no edge is made.
_GO_MISSING = "?missing"   # a lookup the file cannot answer: the package pass may


def _go_type_value(type_node, source: bytes, tparams: frozenset = frozenset()):
    while type_node is not None and type_node.type in ("pointer_type", "parenthesized_type"):
        type_node = next(iter(type_node.named_children), None)
    if type_node is None:
        return None
    if type_node.type == "generic_type":
        type_node = type_node.child_by_field_name("type")
        if type_node is None:
            return None
    if type_node.type == "type_identifier":
        name = _read_text(type_node, source)
        return None if name in _GO_PREDECLARED_TYPES or name in tparams else name
    if type_node.type in ("slice_type", "array_type"):
        elem = _go_type_value(type_node.child_by_field_name("element"), source, tparams)
        return ["[]", elem] if elem is not None else None
    if type_node.type == "map_type":
        elem = _go_type_value(type_node.child_by_field_name("value"), source, tparams)
        return ["[]", elem] if elem is not None else None
    return None


def _go_type_params(decl, source: bytes) -> frozenset:
    """The type parameters of a function, method (its receiver's) or generic type declaration."""
    names: set[str] = set()
    tp = decl.child_by_field_name("type_parameters")
    for d in (tp.named_children if tp is not None else ()):
        for c in d.children:
            if c.type == "identifier":
                names.add(_read_text(c, source))
    receiver = decl.child_by_field_name("receiver")
    for param in (receiver.named_children if receiver is not None else ()):
        rtype = param.child_by_field_name("type")
        while rtype is not None and rtype.type == "pointer_type":
            rtype = next(iter(rtype.named_children), None)
        if rtype is not None and rtype.type == "generic_type":
            for args in rtype.named_children:
                if args.type == "type_arguments":
                    for a in args.named_children:
                        names.add(_read_text(a, source))
    return frozenset(names)


def _go_result_values(result, source: bytes, tparams: frozenset) -> list:
    if result is None:
        return []
    if result.type != "parameter_list":
        return [_go_type_value(result, source, tparams)]
    out: list = []
    for p in result.named_children:
        if p.type != "parameter_declaration":
            continue
        value = _go_type_value(p.child_by_field_name("type"), source, tparams)
        n = sum(1 for c in p.children if c.type == "identifier")
        out.extend([value] * max(1, n))
    return out


def _go_own_type(decl, source: bytes) -> str | None:
    """The receiver's type name of a method declaration (`(s *Server[T])` -> `Server`)."""
    receiver = decl.child_by_field_name("receiver")
    for param in (receiver.named_children if receiver is not None else ()):
        if param.type == "parameter_declaration":
            value = _go_type_value(param.child_by_field_name("type"), source)
            return value if isinstance(value, str) else None
    return None


def _go_imported_names(root, source: bytes) -> set[str]:
    names: set[str] = set()
    for top in root.children:
        if top.type != "import_declaration":
            continue
        specs = [c for c in top.named_children if c.type == "import_spec"]
        for lst in top.named_children:
            if lst.type == "import_spec_list":
                specs += [c for c in lst.named_children if c.type == "import_spec"]
        for spec in specs:
            path_node = spec.child_by_field_name("path")
            alias = spec.child_by_field_name("name")
            raw = _read_text(path_node, source).strip('"') if path_node is not None else ""
            local = _read_text(alias, source) if alias is not None else raw.split("/")[-1]
            if local and local not in ("_", "."):
                names.add(local)
    return names


def _go_chain(expr, source: bytes, scope: dict, imported: set, idx: int = 0):
    """The chain of an expression (see above), or None when no type of the package can be read from it.
    *scope* maps the names a function binds to their chain (None: bound, of no known type)."""
    if expr is None:
        return None
    t = expr.type
    if t == "parenthesized_expression":
        return _go_chain(next(iter(expr.named_children), None), source, scope, imported, idx)
    if t == "unary_expression":
        if _read_text(expr.child_by_field_name("operator"), source) != "&":
            return None
        return _go_chain(expr.child_by_field_name("operand"), source, scope, imported, idx)
    if t == "identifier":
        name = _read_text(expr, source)
        if idx:
            return None
        if name in scope:
            return scope[name]
        if name in imported or name in ("nil", "true", "false", "iota"):
            return None
        return [["g", name]]
    if t in ("composite_literal", "type_assertion_expression"):
        value = _go_type_value(expr.child_by_field_name("type"), source)
        return [["v", value]] if value is not None and not idx else None
    if t == "call_expression":
        fn = expr.child_by_field_name("function")
        if fn is None:
            return None
        if fn.type == "identifier":
            name = _read_text(fn, source)
            if name == "new" and name not in scope:
                args = expr.child_by_field_name("arguments")
                value = _go_type_value(next(iter(args.named_children), None) if args is not None else None, source)
                return [["v", value]] if value is not None and not idx else None
            if name in scope or name in imported or name in _GO_PREDECLARED_FUNCS:
                return None
            return [["f", name, idx]]
        if fn.type == "selector_expression":
            operand = fn.child_by_field_name("operand")
            field = fn.child_by_field_name("field")
            if operand is None or field is None:
                return None
            if operand.type == "identifier" and _read_text(operand, source) in imported and \
                    _read_text(operand, source) not in scope:
                return None   # another package's function
            base = _go_chain(operand, source, scope, imported)
            return base + [["()", _read_text(field, source), idx]] if base else None
        return None
    if idx:
        return None
    if t == "selector_expression":
        operand = expr.child_by_field_name("operand")
        field = expr.child_by_field_name("field")
        if operand is None or field is None:
            return None
        if operand.type == "identifier" and _read_text(operand, source) in imported and \
                _read_text(operand, source) not in scope:
            return None   # another package's variable
        base = _go_chain(operand, source, scope, imported)
        return base + [[".", _read_text(field, source)]] if base else None
    if t == "index_expression":
        base = _go_chain(expr.child_by_field_name("operand"), source, scope, imported)
        return base + [["[]"]] if base else None
    return None


def _go_bind(table: dict, name: str, chain) -> None:
    """A name bound twice in one function keeps a chain only when both bindings give the same one."""
    if name and name != "_":
        table[name] = chain if table.get(name, chain) == chain else None


def _go_bind_spec(table: dict, spec, source: bytes, imported: set, tparams: frozenset = frozenset()) -> None:
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
        declared = _go_type_value(spec.child_by_field_name("type"), source, tparams) \
            if spec.child_by_field_name("type") is not None else None
        if spec.child_by_field_name("type") is not None:
            for ident in names:
                _go_bind(table, _read_text(ident, source), [["v", declared]] if declared is not None else None)
            return
    for i, ident in enumerate(names):
        chain = None
        if len(values) == len(names):
            chain = _go_chain(values[i], source, table, imported)
        elif len(values) == 1 and values[0].type == "call_expression":
            chain = _go_chain(values[0], source, table, imported, idx=i)
        _go_bind(table, _read_text(ident, source), chain)


def _go_type_facts(root, source: bytes) -> dict:
    """What one Go file states about the types of its package (see `_go_eval`): struct fields and embedded
    types, the underlying type of other named types, aliases, interface methods, the result types of functions
    and methods, the methods each type has (with their line), and package-level variables as chains."""
    facts: dict = {"package": "", "fields": {}, "embedded": {}, "underlying": {}, "aliases": {}, "ifaces": {},
                   "results": {}, "methods": {}, "vars": {}}
    imported = _go_imported_names(root, source)
    for top in root.children:
        if top.type == "package_clause":
            name = next((c for c in top.named_children if c.type == "package_identifier"), None)
            facts["package"] = _read_text(name, source) if name is not None else ""
        elif top.type in ("function_declaration", "method_declaration"):
            name = top.child_by_field_name("name")
            if name is None:
                continue
            tparams = _go_type_params(top, source)
            results = _go_result_values(top.child_by_field_name("result"), source, tparams)
            if top.type == "function_declaration":
                facts["results"][_read_text(name, source)] = results
            else:
                own = _go_own_type(top, source)
                if own:
                    facts["results"][f"{own}.{_read_text(name, source)}"] = results
                    facts["methods"].setdefault(own, {})[_read_text(name, source)] = top.start_point[0] + 1
        elif top.type == "type_declaration":
            for spec in top.named_children:
                tname = spec.child_by_field_name("name")
                stype = spec.child_by_field_name("type")
                if tname is None or stype is None:
                    continue
                tname_text = _read_text(tname, source)
                if spec.type == "type_alias":
                    facts["aliases"][tname_text] = _go_type_value(stype, source)
                    continue
                if spec.type != "type_spec":
                    continue
                tparams = _go_type_params(spec, source)
                if stype.type == "struct_type":
                    fields: dict = {}
                    embedded: list = []
                    flist = next((c for c in stype.named_children if c.type == "field_declaration_list"), None)
                    for fd in (flist.named_children if flist is not None else ()):
                        if fd.type != "field_declaration":
                            continue
                        ftype = fd.child_by_field_name("type")
                        fnames = [c for c in fd.children if c.type == "field_identifier"]
                        if fnames:
                            value = _go_type_value(ftype, source, tparams)
                            for fn in fnames:
                                fields[_read_text(fn, source)] = value
                        else:
                            value = _go_type_value(ftype, source, tparams)
                            embedded.append(value if isinstance(value, str) else None)
                    facts["fields"][tname_text] = fields
                    facts["embedded"][tname_text] = embedded
                elif stype.type == "interface_type":
                    methods: dict = {}
                    for elem in stype.named_children:
                        if elem.type == "method_elem":
                            mname = elem.child_by_field_name("name")
                            if mname is None:
                                mname = next((c for c in elem.children if c.type == "field_identifier"), None)
                            if mname is not None:
                                methods[_read_text(mname, source)] = elem.start_point[0] + 1
                    facts["ifaces"][tname_text] = methods
                else:
                    facts["underlying"][tname_text] = _go_type_value(stype, source, tparams)
    for top in root.children:
        if top.type == "var_declaration":
            specs = [c for c in top.named_children if c.type == "var_spec"]
            for lst in top.named_children:
                if lst.type == "var_spec_list":
                    specs += [c for c in lst.named_children if c.type == "var_spec"]
            for spec in specs:
                _go_bind_spec(facts["vars"], spec, source, imported)
    return facts


def _go_merge_facts(all_facts: list[dict]) -> dict:
    """The facts of one package from those of its files. A name two files state differently (build-tagged
    twins) states nothing."""
    merged: dict = {"package": "", "fields": {}, "embedded": {}, "underlying": {}, "aliases": {}, "ifaces": {},
                    "results": {}, "methods": {}, "vars": {}}
    for facts in all_facts:
        for key in ("fields", "embedded", "underlying", "aliases", "ifaces", "results", "vars"):
            for name, value in facts.get(key, {}).items():
                if name in merged[key] and merged[key][name] != value:
                    merged[key][name] = None
                else:
                    merged[key][name] = value
        for owner, methods in facts.get("methods", {}).items():
            mine = merged["methods"].setdefault(owner, {})
            for m, where in methods.items():
                mine[m] = where if mine.get(m, where) == where else None
    return merged


def _go_declared(facts: dict, name) -> bool:
    return isinstance(name, str) and any(
        name in facts[k] for k in ("fields", "underlying", "aliases", "ifaces"))


def _go_dealias(facts: dict, value, depth: int = 0):
    while isinstance(value, str) and value in facts["aliases"] and depth < 8:
        value = facts["aliases"][value]
        depth += 1
    return value


def _go_unknown(facts: dict, name, complete: bool):
    """A lookup on *name* that found nothing: no type when the facts are the whole package or *name* is
    declared in them; else the package pass may know more."""
    return None if complete or _go_declared(facts, name) else _GO_MISSING


def _go_promoted(facts: dict, typ: str, hit, complete: bool, depth: int):
    """Search the structs *typ* embeds, shallowest first, with ``hit(t) -> value | None``. One answer at the
    shallowest depth that has one; none when two differ or an embedded type of another package comes first."""
    level, seen = [typ], {typ}
    while level and depth < 8:
        found: list = []
        nxt: list[str] = []
        external = missing = False
        for t in level:
            for emb in facts["embedded"].get(t) or ():
                if emb is None:
                    external = True
                    continue
                emb = _go_dealias(facts, emb)
                if not isinstance(emb, str) or emb in seen:
                    continue
                seen.add(emb)
                value = hit(emb)
                if value is not None:
                    found.append(value)
                elif not _go_declared(facts, emb):
                    missing = True
                nxt.append(emb)
        if found:
            return found[0] if all(f == found[0] for f in found) else None
        if external:
            return None
        if missing and not complete:
            return _GO_MISSING
        level = nxt
        depth += 1
    return None


def _go_field(facts: dict, typ: str, field: str, complete: bool, depth: int):
    if typ not in facts["fields"]:
        return _go_unknown(facts, typ, complete)
    if field in facts["fields"][typ]:
        return facts["fields"][typ][field]

    def hit(t):
        if field == t:
            return t   # the embedded type is a field named after it
        return facts["fields"].get(t, {}).get(field)
    return _go_promoted(facts, typ, hit, complete, depth)


def _go_method_result(facts: dict, typ: str, method: str, i: int, complete: bool, depth: int):
    def hit(t):
        results = facts["results"].get(f"{t}.{method}")
        if results is None:
            return None
        return results[i] if i < len(results) and results[i] is not None else "?none"
    own = hit(typ)
    if own is None:
        own = _go_promoted(facts, typ, hit, complete, depth) if typ in facts["embedded"] else None
    if own is None:
        return None if complete else _GO_MISSING   # a method may be declared in any file of the package
    return None if own == "?none" else own


def _go_eval(chain, facts: dict, complete: bool, depth: int = 0):
    """The value a chain names under *facts*; None when it names no type of the package; ``_GO_MISSING`` when
    *facts* (one file's, ``complete=False``) cannot tell."""
    if not chain or depth > 8:
        return None
    start = chain[0]
    if start[0] == "v":
        value = start[1]
    elif start[0] == "f":
        results = facts["results"].get(start[1]) if "." not in start[1] else None
        if results is not None:
            value = results[start[2]] if start[2] < len(results) else None
        elif _go_declared(facts, start[1]) and start[2] == 0:
            value = start[1]   # a conversion `T(x)`
        else:
            value = _go_unknown(facts, start[1], complete)
    elif start[0] == "g":
        if start[1] in facts["vars"]:
            value = _go_eval(facts["vars"][start[1]], facts, complete, depth + 1)
        elif _go_declared(facts, start[1]):
            value = start[1]   # a method expression `T.m(recv, ...)`
        else:
            value = _go_unknown(facts, start[1], complete)
    else:
        return None
    for step in chain[1:]:
        value = _go_dealias(facts, value)
        if value is None or value == _GO_MISSING:
            return value
        if step[0] == "[]":
            if isinstance(value, list):
                value = value[1]
            elif value in facts["underlying"]:
                under = _go_dealias(facts, facts["underlying"][value])
                value = under[1] if isinstance(under, list) else None
            else:
                value = _go_unknown(facts, value, complete)
        elif not isinstance(value, str):
            return None
        elif step[0] == ".":
            value = _go_field(facts, value, step[1], complete, depth)
        elif step[0] == "()":
            value = _go_method_result(facts, value, step[1], step[2], complete, depth)
        else:
            return None
    return _go_dealias(facts, value)


def _go_find_method(facts: dict, typ, method: str, complete: bool):
    """``(kind, owner type)`` of the method *method* a value of type *typ* calls: its own (``"method"``), one
    an embedded struct promotes, or an interface's declaration (``"iface"``, a lead to its implementations);
    None when it has none, ``_GO_MISSING`` when one file's facts cannot tell."""
    typ = _go_dealias(facts, typ)
    if not isinstance(typ, str):
        return None
    if method in (facts["methods"].get(typ) or {}):
        return ("method", typ)
    if method in (facts["ifaces"].get(typ) or {}):
        return ("iface", typ)

    def hit(t):
        if method in (facts["methods"].get(t) or {}):
            return ("method", t)
        return None
    if typ in facts["embedded"]:
        found = _go_promoted(facts, typ, hit, complete, 0)
        if found is not None:
            return found
    if not complete:
        return _GO_MISSING
    return None


def _go_simple_chain(chain) -> bool:
    """A chain of the forms read before the package pass existed (a stated type, a constructor's result, a
    package variable of the file, a field): its in-file edge stays EXTRACTED."""
    return bool(chain) and chain[0][0] in ("v", "f", "g") and (chain[0][0] != "f" or chain[0][2] == 0) and all(
        step[0] == "." for step in chain[1:])


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
    # Local change (Verinoda): the methods of each receiver type of this file and the methods each interface of
    # this file declares, so `c.m()` binds to the type of `c` (see the receiver chains below) and no other
    # selector call binds to a same-named function of the file.
    methods_by_type: dict[tuple[str, str], str] = {}
    iface_methods_by_type: dict[tuple[str, str], str] = {}
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
                            iface_methods_by_type.setdefault((type_name, m_name), m_nid)
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

    # Local change (Verinoda): the receiver types this file states, as chains
    # (`_go_chain`): the method's own receiver, its parameters and named results, and each name the body binds
    # (`x := &T{}`, `T{}`, `NewT()`, `var x T`, `x := y.f`, `y[i]`, `y.m()`, `x.(T)`, a range value), read with
    # this file's facts (`_go_type_facts`). A name given two chains in one function has none. What the file
    # cannot tell (a type, field or method of another file) is left to `resolve_go_receiver_calls`.
    file_facts = _go_type_facts(root, source)
    local_imports = set(go_imported_pkgs)
    local_types_of: dict[str, dict] = {}

    def _collect_locals(n, table: dict, tparams: frozenset) -> None:
        """Every name the body declares: with its chain, or None (a closure parameter, a select receive, a
        type switch alias, a range key), so no local falls through to a package variable of its name."""
        for child in n.children:
            if child.type in ("short_var_declaration", "var_spec", "const_spec"):
                _go_bind_spec(table, child, source, local_imports, tparams)
            elif child.type == "range_clause":
                names = child.child_by_field_name("left")
                idents = [i for i in (names.named_children if names is not None else ()) if i.type == "identifier"]
                ranged = _go_chain(child.child_by_field_name("right"), source, table, local_imports)
                for k, ident in enumerate(idents):
                    # `for i, v := range xs`: v is an element of xs; i (or a lone key) is no type of the package
                    _go_bind(table, _read_text(ident, source),
                             ranged + [["[]"]] if ranged and k == 1 else None)
            elif child.type in ("receive_statement", "type_switch_statement"):
                names = child.child_by_field_name("alias" if child.type == "type_switch_statement" else "left")
                for ident in (names.named_children if names is not None else ()):
                    if ident.type == "identifier":
                        _go_bind(table, _read_text(ident, source), None)
            elif child.type == "func_literal":
                params = child.child_by_field_name("parameters")
                for param in (params.named_children if params is not None else ()):
                    for ident in param.children:
                        if ident.type == "identifier":
                            _go_bind(table, _read_text(ident, source), None)
            _collect_locals(child, table, tparams)

    for fnid, body in function_bodies:
        # every parameter shadows a package variable of its name; only those of a type of the package carry one
        table: dict = {}
        decl = body.parent
        tparams = _go_type_params(decl, source) if decl is not None else frozenset()
        for field in ("receiver", "parameters", "result"):
            plist = decl.child_by_field_name(field) if decl is not None else None
            if plist is None or plist.type != "parameter_list":
                continue
            for param in plist.named_children:
                value = _go_type_value(param.child_by_field_name("type"), source, tparams)
                if param.type == "variadic_parameter_declaration":
                    value = ["[]", value] if value is not None else None
                for child in param.children:
                    if child.type == "identifier":
                        table[_read_text(child, source)] = [["v", value]] if value is not None else None
        _collect_locals(body, table, tparams)
        local_types_of[fnid] = table

    iface_lead_nids = set(iface_methods_by_type.values())

    def _receiver_chain(operand, caller: str):
        return _go_chain(operand, source, local_types_of.get(caller, {}), local_imports)

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
            # Local change (Verinoda): a member call's receiver chain; a call on a receiver of a known type is
            # not dropped for sharing a name with some language's builtin (`root.next()`)
            chain = None
            if callee_name and is_member_call and not import_path:
                chain = _receiver_chain(operand_node, caller_nid)
            if callee_name and (callee_name not in _LANGUAGE_BUILTIN_GLOBALS or chain):
                confidence = "EXTRACTED"
                # Never resolve an imported selector through a bare local name.
                if import_path:
                    tgt_nid = None
                elif is_member_call:
                    # a receiver whose type this file states: the method's own receiver, a parameter, a local
                    # or package variable, a field, an element or a method's result of one; a promoted method of
                    # an embedded struct; an interface's declaration of the method (a lead, INFERRED)
                    tgt_nid = None
                    value = _go_eval(chain, file_facts, False) if chain else None
                    found = _go_find_method(file_facts, value, callee_name, False) \
                        if value is not None and value != _GO_MISSING else value
                    if isinstance(found, tuple):
                        kind, owner = found
                        table = methods_by_type if kind == "method" else iface_methods_by_type
                        tgt_nid = table.get((owner, callee_name))
                        if kind != "method" or not _go_simple_chain(chain):
                            confidence = "INFERRED"
                    elif found is None:
                        chain = None   # the file states the type, and it has no such method
                elif is_bare_identifier:
                    tgt_nid = bare_label_to_nid.get(callee_name)
                else:
                    tgt_nid = label_to_nid.get(callee_name)
                if tgt_nid and tgt_nid != caller_nid:
                    pair = (caller_nid, tgt_nid)
                    if pair not in seen_call_pairs:
                        seen_call_pairs.add(pair)
                        line = node.start_point[0] + 1
                        edge = {
                            "source": caller_nid,
                            "target": tgt_nid,
                            "relation": "calls",
                            "context": "call",
                            "confidence": confidence,
                            "source_file": str_path,
                            "source_location": f"L{line}",
                            "weight": 1.0,
                        }
                        if confidence == "INFERRED":
                            edge["confidence_score"] = 0.75 if tgt_nid in iface_lead_nids else 0.85
                        edges.append(edge)
                elif callee_name and not (tgt_nid and is_member_call):
                    rc_entry = {
                        "caller_nid": caller_nid,
                        "callee": callee_name,
                        "is_member_call": is_member_call,
                        "language": "go",
                        "receiver": package_receiver,
                        "import_path": import_path,
                        "source_file": str_path,
                        "source_location": f"L{node.start_point[0] + 1}",
                    }
                    if chain:
                        # Local change (Verinoda): typed by the package pass (resolve_go_receiver_calls)
                        rc_entry["go_chain"] = chain
                        rc_entry["go_package"] = file_facts["package"]
                    raw_calls.append(rc_entry)
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


def _go_package_facts(directory: Path, package: str, parser) -> dict | None:
    """The merged facts of the files in *directory* whose package clause is *package*, read from disk; each
    method's and interface method's place is ``[file name, line]``."""
    found: list[dict] = []
    try:
        files = sorted(p for p in directory.glob("*.go") if p.is_file())
    except OSError:
        return None
    for f in files:
        try:
            source = f.read_bytes()
            facts = _go_type_facts(parser.parse(source).root_node, source)
        except Exception:  # noqa: BLE001 - an unreadable file states nothing
            continue
        if facts["package"] != package:
            continue
        facts["methods"] = {t: {m: [f.name, line] for m, line in ms.items()} for t, ms in facts["methods"].items()}
        facts["ifaces"] = {t: {m: [f.name, line] for m, line in ms.items()} for t, ms in facts["ifaces"].items()}
        found.append(facts)
    return _go_merge_facts(found) if found else None


def resolve_go_receiver_calls(per_file: list[dict], all_nodes: list[dict], all_edges: list[dict]) -> None:
    """Verinoda patch: bind Go member calls whose receiver type the caller's file could not read alone.

    The extractor types a receiver from what its own file states and leaves a chain (`_go_chain`) on the call
    when a step needs another file: `root := engine.trees.get(m); root.addRoute(...)` in gin.go, where the
    field, the method's result and `addRoute` are declared in tree.go. This pass evaluates the chain with the
    facts of every file of the caller's package (its directory, the same package clause), read from disk, and
    binds the call to the method the type has (or promotes from an embedded struct). A receiver of an interface
    type binds to the interface's declaration of the method, never to a guessed implementation. Nothing else
    binds: another package's type, a name of no stated type, two files stating different types. Edges are
    INFERRED (0.85; 0.75 for an interface method).
    """
    raw = [rc for result in per_file for rc in result.get("raw_calls", [])
           if rc.get("go_chain") and rc.get("caller_nid") and rc.get("source_file")]
    if not raw:
        return
    try:
        import tree_sitter_go as tsgo
        from tree_sitter import Language, Parser
        parser = Parser(Language(tsgo.language()))
    except Exception:  # noqa: BLE001 - no grammar, no pass
        return

    label_of = {n.get("id"): str(n.get("label") or "") for n in all_nodes}
    owner_of: dict[str, str] = {}
    for e in all_edges:
        if e.get("relation") == "method":
            owner_of.setdefault(e.get("target"), e.get("source"))
    # (file name, label) -> (id, source file, line); the line is missing on the unchanged files' nodes an
    # incremental build passes as context
    by_place: dict[tuple[str, str], list[tuple[str, str, str]]] = {}
    for n in all_nodes:
        sf = str(n.get("source_file") or "")
        if not sf.endswith(".go"):
            continue
        key = (os.path.basename(sf.replace("\\", "/")), label_of.get(n.get("id"), ""))
        by_place.setdefault(key, []).append((n["id"], sf, str(n.get("source_location") or "")))

    def same_dir(directory: Path, sf: str) -> bool:
        p = Path(sf)
        if p.is_absolute():
            return os.path.normcase(str(p.parent)) == os.path.normcase(str(directory))
        parent = p.parent.as_posix().strip("./")
        return not parent or directory.as_posix().lower().endswith("/" + parent.lower())

    existing = {(e.get("source"), e.get("target")) for e in all_edges if e.get("relation") == "calls"}
    packages: dict[tuple[str, str], dict | None] = {}
    for rc in raw:
        directory = Path(str(rc["source_file"])).parent
        key = (os.path.normcase(str(directory)), str(rc.get("go_package") or ""))
        if key not in packages:
            packages[key] = _go_package_facts(directory, key[1], parser)
        facts = packages[key]
        if facts is None:
            continue
        callee = str(rc["callee"])
        value = _go_eval(rc["go_chain"], facts, True)
        found = _go_find_method(facts, value, callee, True) if value is not None else None
        if not isinstance(found, tuple):
            continue
        kind, owner = found
        place = (facts["methods"] if kind == "method" else facts["ifaces"]).get(owner, {}).get(callee)
        if not place:
            continue
        cands = [(nid, loc) for nid, sf, loc in by_place.get((place[0], f".{callee}()"), [])
                 if same_dir(directory, sf) and label_of.get(owner_of.get(nid, ""), "").split("[", 1)[0] == owner]
        if len(cands) > 1:   # case-only twins of one name: the line decides
            cands = [c for c in cands if c[1] == f"L{place[1]}"]
        if len(cands) != 1:
            continue
        caller, tgt = rc["caller_nid"], cands[0][0]
        if tgt == caller or (caller, tgt) in existing:
            continue
        existing.add((caller, tgt))
        all_edges.append({
            "source": caller,
            "target": tgt,
            "relation": "calls",
            "context": "call",
            "confidence": "INFERRED",
            "confidence_score": 0.85 if kind == "method" else 0.75,
            "source_file": rc.get("source_file", ""),
            "source_location": rc.get("source_location"),
            "weight": 1.0,
        })
