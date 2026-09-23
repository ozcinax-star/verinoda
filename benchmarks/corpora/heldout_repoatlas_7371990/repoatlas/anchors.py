"""Symbol facts, facet fingerprints and evidence anchors (docs/DESIGN.md D23-D25).

A file's *facts* describe its definitions without line numbers, so that an edit
can be diffed per definition instead of per file:

* ``symbols``  qualified name -> ``{start, def, end, kind, sig, body, doc, full}``.
  ``sig`` covers the header (name, parameters, defaults, return annotation,
  decorators; bases for classes). ``body`` covers the header plus the
  statements without the docstring; a nested definition contributes only its
  own ``full`` hash (Merkle), so each definition is serialised once. A class
  ``body`` is its header plus class-level statements; methods are separate
  symbols. ``doc`` is the docstring hash. ``full`` covers everything, docstring
  and nested definitions included (the anchor fingerprint).
* ``bindings`` module-level name -> fingerprint of every statement that binds
  it (imports, definitions, assignments; star imports feed every name).
* ``module``   top-level statements keyed ``IMPORT:a,b`` / ``ASSIGN:NAME`` /
  ``DOC`` / ``STMT:<hash>`` -> ``{start, end, h}``.
* ``sections`` Markdown heading path -> ``{start, end, h, title, level}``.

Python uses its own serializer over ``node._fields`` (never ``ast.dump``,
whose output changed in Python 3.13): positions, ``ctx`` and empty optional
fields are dropped, so whitespace, comments, blank lines, CRLF and moving a
definition leave every fingerprint unchanged. Other languages hash tree-sitter
leaf sequences with comment nodes skipped. A file that cannot be parsed, or a
language without a grammar, has no facts: callers then fall back to file-level
rules, which are never less safe than before.

Facts are cached in ``file_facts`` by the file's sha256 and a *scheme* string
(serializer version, plus the Python minor version for Python). A scheme
mismatch is never read as a change: the caller falls back to file level.

An *anchor* (``evidence.meta.anchor``) pins cited lines to the enclosing
symbol, module statement or Markdown section: ``{sym|mod|sec, fp, off,
n_lines, occ, scheme}`` plus, for Python, the AST path (``ast_path``,
``stmt_off``) to the innermost statement containing the first cited line.
:func:`relocate` re-finds the lines after an edit:

* ``same``    fingerprint equal, same lines, same text;
* ``moved``   fingerprint equal, text found at exact new lines;
* ``changed`` the anchor's fingerprint or text differs: a candidate location
  only, never verification by itself;
* ``gone``    the symbol, statement or section no longer exists.
"""

from __future__ import annotations

import ast
import hashlib
import re
import sys
import time
import warnings
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

SCHEME_VERSION = 1
PY_SCHEME = f"py{SCHEME_VERSION}-{sys.version_info[0]}.{sys.version_info[1]}"
MD_SCHEME = f"md{SCHEME_VERSION}"
TS_SCHEME = f"ts{SCHEME_VERSION}"
ABSENT = "-"  # facet fingerprint of something that does not exist (a dep on it still counts)

MD_SUFFIXES = (".md", ".markdown")
# extension -> (tree-sitter module, language function)
TS_LANGS: dict[str, tuple[str, str]] = {
    ".js": ("tree_sitter_javascript", "language"), ".jsx": ("tree_sitter_javascript", "language"),
    ".mjs": ("tree_sitter_javascript", "language"), ".cjs": ("tree_sitter_javascript", "language"),
    ".ts": ("tree_sitter_typescript", "language_typescript"),
    ".mts": ("tree_sitter_typescript", "language_typescript"),
    ".cts": ("tree_sitter_typescript", "language_typescript"),
    ".tsx": ("tree_sitter_typescript", "language_tsx"),
    ".go": ("tree_sitter_go", "language"), ".java": ("tree_sitter_java", "language"),
    ".rs": ("tree_sitter_rust", "language"), ".c": ("tree_sitter_c", "language"),
    ".h": ("tree_sitter_c", "language"), ".cc": ("tree_sitter_cpp", "language"),
    ".cpp": ("tree_sitter_cpp", "language"), ".cxx": ("tree_sitter_cpp", "language"),
    ".hpp": ("tree_sitter_cpp", "language"), ".hh": ("tree_sitter_cpp", "language"),
    ".rb": ("tree_sitter_ruby", "language"), ".cs": ("tree_sitter_c_sharp", "language"),
    ".php": ("tree_sitter_php", "language_php"), ".kt": ("tree_sitter_kotlin", "language"),
    ".kts": ("tree_sitter_kotlin", "language"), ".scala": ("tree_sitter_scala", "language"),
    ".swift": ("tree_sitter_swift", "language"), ".lua": ("tree_sitter_lua", "language"),
}
# Definition node types across the bundled grammars (a node needs a name to count).
TS_DEF_TYPES = frozenset({
    "function_declaration", "generator_function_declaration", "function_definition", "method_definition",
    "method_declaration", "constructor_declaration", "class_declaration", "abstract_class_declaration",
    "interface_declaration", "enum_declaration", "record_declaration", "struct_declaration",
    "type_declaration", "function_item", "impl_item", "struct_item", "enum_item", "trait_item", "mod_item",
    "class", "method", "singleton_method", "module", "class_specifier", "struct_specifier",
    "namespace_definition", "namespace_declaration", "object_declaration", "protocol_declaration",
    "trait_definition", "class_definition", "object_definition", "function_signature_item",
})

_DEF_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _h() -> Any:
    return hashlib.blake2b(digest_size=12)


def parse_python(text: str) -> ast.AST:
    """``ast.parse`` without the SyntaxWarnings (invalid escapes ...) that older sources emit."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        return ast.parse(text)


def _hex(parts: Iterable[str]) -> str:
    h = _h()
    for p in parts:
        h.update(p.encode("utf-8", "surrogatepass"))
        h.update(b"\x1f")
    return h.hexdigest()


def norm_line(text: str) -> str:
    """Whitespace-collapsed line text (used to count occurrences of a cited line)."""
    return " ".join(text.split())


def text_hash(text: str) -> str:
    """Same normalisation as :func:`repoatlas.evidence.content_hash` (CRLF and trailing spaces ignored)."""
    norm = "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").split("\n"))
    return "sha256:" + hashlib.sha256(norm.encode("utf-8")).hexdigest()


# =============================================================================
# Python
# =============================================================================

class _PySerializer:
    """Canonical, position-free serialisation of Python AST nodes into a hash."""

    def __init__(self, placeholders: dict[int, str]):
        self.placeholders = placeholders  # id(nested def) -> its full hash (Merkle)

    def feed(self, h: Any, node: Any, *, root: bool = False) -> None:
        if isinstance(node, ast.AST):
            if not root and isinstance(node, _DEF_NODES) and id(node) in self.placeholders:
                h.update(b"D:" + self.placeholders[id(node)].encode())
                return
            h.update(b"(" + type(node).__name__.encode())
            for name in node._fields:
                if name in ("ctx", "type_comment") or (name == "kind" and isinstance(node, ast.Constant)):
                    continue
                value = getattr(node, name, None)
                if value is None or (isinstance(value, list) and not value):
                    continue  # optional fields added by newer Pythons stay invisible
                h.update(b" " + name.encode() + b"=")
                self.feed(h, value)
            h.update(b")")
        elif isinstance(node, list):
            h.update(b"[")
            for item in node:
                self.feed(h, item)
                h.update(b",")
            h.update(b"]")
        else:
            h.update(type(node).__name__.encode() + b":" + repr(node).encode("utf-8", "surrogatepass"))

    def digest(self, *nodes: Any, tag: str = "") -> str:
        h = _h()
        h.update(tag.encode())
        for n in nodes:
            self.feed(h, n, root=isinstance(n, _DEF_NODES))
            h.update(b"|")
        return h.hexdigest()


def _docstring_node(body: list[ast.stmt]) -> ast.stmt | None:
    if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
            and isinstance(body[0].value.value, str):
        return body[0]
    return None


def _nested_defs(node: ast.AST) -> list[ast.AST]:
    """Definitions inside ``node``'s body, not descending into them."""
    out: list[ast.AST] = []
    stack = list(ast.iter_child_nodes(node))
    while stack:
        cur = stack.pop()
        if isinstance(cur, _DEF_NODES):
            out.append(cur)
            continue
        stack.extend(ast.iter_child_nodes(cur))
    return sorted(out, key=lambda n: (n.lineno, n.col_offset))


def _unique(key: str, taken: dict) -> str:
    if key not in taken:
        return key
    i = 2
    while f"{key}#{i}" in taken:
        i += 1
    return f"{key}#{i}"


def _python_facts(text: str) -> dict:
    tree = parse_python(text)
    placeholders: dict[int, str] = {}
    ser = _PySerializer(placeholders)
    symbols: dict[str, dict] = {}

    def header(node: ast.AST) -> list:
        if isinstance(node, ast.ClassDef):
            return [node.name, node.bases, node.keywords, node.decorator_list,
                    getattr(node, "type_params", None) or []]
        return [type(node).__name__, node.name, node.args, node.returns, node.decorator_list,
                getattr(node, "type_params", None) or []]

    def process(node: ast.AST, qual: str) -> None:
        for child in _nested_defs(node):
            process(child, f"{qual}.{child.name}")
        doc = _docstring_node(node.body)
        stmts = [s for s in node.body if s is not doc]
        hdr = header(node)
        sig = ser.digest(*hdr, tag="sig")
        if isinstance(node, ast.ClassDef):
            body = ser.digest(*hdr, [s for s in stmts if not isinstance(s, _DEF_NODES)], tag="body")
        else:
            body = ser.digest(*hdr, stmts, tag="body")
        full = ser.digest(*hdr, node.body, tag="full")
        placeholders[id(node)] = full
        key = _unique(qual, symbols)
        symbols[key] = {
            "kind": "class" if isinstance(node, ast.ClassDef) else "def",
            "start": min([node.lineno] + [d.lineno for d in node.decorator_list]),
            "def": node.lineno, "end": node.end_lineno or node.lineno,
            "sig": sig, "body": body, "full": full,
            "doc": _hex([doc.value.value]) if doc is not None else None,  # type: ignore[attr-defined]
        }

    for stmt in tree.body:
        if isinstance(stmt, _DEF_NODES):
            process(stmt, stmt.name)
        else:
            for child in _nested_defs(stmt):  # defs under module-level if/try
                process(child, child.name)

    # module-level statements
    module: dict[str, dict] = {}
    for stmt in tree.body:
        if isinstance(stmt, _DEF_NODES):
            continue
        if stmt is _docstring_node(tree.body):
            key = "DOC"
        elif isinstance(stmt, (ast.Import, ast.ImportFrom)):
            key = "IMPORT:" + ",".join(sorted(a.asname or a.name for a in stmt.names))
        elif isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            names = [n.id for t in targets for n in ast.walk(t) if isinstance(n, ast.Name)]
            key = "ASSIGN:" + (names[0] if names else ser.digest(stmt)[:10])
        else:
            key = "STMT:" + ser.digest(stmt)[:12]
        module[_unique(key, module)] = {"start": stmt.lineno, "end": stmt.end_lineno or stmt.lineno,
                                        "h": ser.digest(stmt)}

    # module-level bindings (statements under module-level if/try/with/for count; function bodies do not)
    binds: dict[str, list[str]] = {}
    stars: list[str] = []

    def bind(name: str, what: str) -> None:
        binds.setdefault(name, []).append(what)

    def scan(stmts: list[ast.stmt]) -> None:
        for s in stmts:
            if isinstance(s, ast.Import):
                for a in s.names:
                    bind(a.asname or a.name.split(".")[0], f"import:{a.name}:{a.asname or ''}")
            elif isinstance(s, ast.ImportFrom):
                for a in s.names:
                    if a.name == "*":
                        stars.append(f"{'.' * s.level}{s.module or ''}")
                    else:
                        bind(a.asname or a.name, f"from:{'.' * s.level}{s.module or ''}:{a.name}")
            elif isinstance(s, _DEF_NODES):
                bind(s.name, f"def:{type(s).__name__}")
            else:
                targets: list[ast.AST] = []
                if isinstance(s, ast.Assign):
                    targets = list(s.targets)
                elif isinstance(s, (ast.AnnAssign, ast.AugAssign)):
                    targets = [s.target]
                elif isinstance(s, (ast.For, ast.AsyncFor)):
                    targets = [s.target]
                elif isinstance(s, (ast.With, ast.AsyncWith)):
                    targets = [i.optional_vars for i in s.items if i.optional_vars is not None]
                for t in targets:
                    for n in ast.walk(t):
                        if isinstance(n, ast.Name):
                            bind(n.id, "assign:" + ser.digest(s))
                for n in ast.walk(s):
                    if isinstance(n, ast.NamedExpr) and isinstance(n.target, ast.Name):
                        bind(n.target.id, "walrus:" + ser.digest(s))
                for sub in ("body", "orelse", "finalbody"):
                    if isinstance(getattr(s, sub, None), list) and not isinstance(s, _DEF_NODES):
                        scan(getattr(s, sub))
                for handler in getattr(s, "handlers", None) or []:
                    if handler.name:
                        bind(handler.name, "except")
                    scan(handler.body)

    scan(tree.body)
    star_part = "star:" + ",".join(sorted(stars))
    bindings = {name: _hex([*sorted(whats), star_part]) for name, whats in binds.items()}
    if stars:
        bindings["*"] = _hex([star_part])
    return {"lang": "python", "symbols": symbols, "bindings": bindings, "module": module}


# =============================================================================
# Markdown
# =============================================================================

_ATX = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE = re.compile(r"^\s{0,3}(```|~~~)")


def _markdown_facts(text: str) -> dict:
    lines = text.splitlines()
    heads: list[tuple[int, int, str]] = []  # (line, level, title)
    fence = None
    for i, line in enumerate(lines, 1):
        m = _FENCE.match(line)
        if m:
            fence = None if fence == m.group(1) else (fence or m.group(1))
            continue
        if fence:
            continue
        a = _ATX.match(line)
        if a:
            heads.append((i, len(a.group(1)), a.group(2).strip()))
        elif i > 1 and re.match(r"^\s{0,3}(=+|-+)\s*$", line) and lines[i - 2].strip() \
                and not _ATX.match(lines[i - 2]) and (not heads or heads[-1][0] != i - 1):
            heads.append((i - 1, 1 if line.strip().startswith("=") else 2, lines[i - 2].strip()))
    sections: dict[str, dict] = {}
    path: list[tuple[int, str]] = []
    if heads and heads[0][0] > 1 and any(ln.strip() for ln in lines[: heads[0][0] - 1]):
        body = lines[: heads[0][0] - 1]
        sections["(preamble)"] = {"start": 1, "end": heads[0][0] - 1, "title": "", "level": 0,
                                  "h": _hex([norm_line(" ".join(body))])}
    for k, (ln, level, title) in enumerate(heads):
        end = (heads[k + 1][0] - 1) if k + 1 < len(heads) else len(lines)
        while end > ln and not lines[end - 1].strip():
            end -= 1
        path = [p for p in path if p[0] < level] + [(level, title)]
        key = _unique("/".join(t for _, t in path), sections)
        sections[key] = {"start": ln, "end": end, "title": title, "level": level,
                         "h": _hex([norm_line(" ".join(lines[ln - 1: end]))])}
    if not heads and any(ln.strip() for ln in lines):
        sections["(preamble)"] = {"start": 1, "end": len(lines), "title": "", "level": 0,
                                  "h": _hex([norm_line(" ".join(lines))])}
    return {"lang": "markdown", "symbols": {}, "bindings": {}, "module": {}, "sections": sections}


# =============================================================================
# tree-sitter languages
# =============================================================================

@lru_cache(maxsize=32)
def _ts_parser(suffix: str):
    spec = TS_LANGS.get(suffix)
    if not spec:
        return None
    try:
        import importlib

        from tree_sitter import Language, Parser

        mod = importlib.import_module(spec[0])
        return Parser(Language(getattr(mod, spec[1])()))
    except Exception:  # grammar missing or incompatible: file-level fallback
        return None


def _ts_name(node) -> str | None:
    for fld in ("name", "declarator"):
        ch = node.child_by_field_name(fld)
        depth = 0
        while ch is not None and depth < 6:
            if ch.type in ("identifier", "field_identifier", "type_identifier", "property_identifier",
                           "constant", "qualified_identifier", "scoped_identifier", "simple_identifier",
                           "name", "operator_name", "destructor_name"):
                return ch.text.decode("utf-8", "replace")
            nxt = ch.child_by_field_name("declarator") or ch.child_by_field_name("name")
            if nxt is None:
                named = [c for c in ch.children if c.is_named]
                if len(named) == 1:
                    nxt = named[0]
            ch, depth = nxt, depth + 1
    typ = node.child_by_field_name("type")
    if node.type == "impl_item" and typ is not None:
        return "impl " + typ.text.decode("utf-8", "replace")
    if node.type == "type_declaration":
        for c in node.children:
            if c.type == "type_spec":
                n = c.child_by_field_name("name")
                if n is not None:
                    return n.text.decode("utf-8", "replace")
    return None


def _ts_facts(text: str, suffix: str) -> dict | None:
    parser = _ts_parser(suffix)
    if parser is None:
        return None
    data = text.encode("utf-8", "surrogatepass")
    tree = parser.parse(data)
    root = tree.root_node
    symbols: dict[str, dict] = {}
    placeholders: dict[int, str] = {}

    def is_def(n) -> bool:
        return n.type in TS_DEF_TYPES and _ts_name(n) is not None

    def leaves(n, h, skip_defs: bool, stop_at=None) -> None:
        stack = [n]
        while stack:
            cur = stack.pop()
            if stop_at is not None and cur.start_byte >= stop_at:
                continue
            if cur.id != n.id and is_def(cur):
                if not skip_defs:
                    h.update(b"D:" + placeholders.get(cur.id, "?").encode())
                continue
            if "comment" in cur.type:
                continue
            if cur.child_count == 0:
                h.update(cur.type.encode() + b"=" + cur.text + b"\x1f")
            else:
                stack.extend(reversed(cur.children))

    def process(n, prefix: str) -> None:
        name = _ts_name(n)
        qual = f"{prefix}{name}"
        for ch in _ts_nested(n):
            process(ch, qual + ".")
        body = n.child_by_field_name("body")
        hs, hb, hf = _h(), _h(), _h()
        leaves(n, hs, True, stop_at=body.start_byte if body is not None else None)
        is_class = any(k in n.type for k in ("class", "struct", "interface", "impl", "trait", "module",
                                             "namespace", "object", "protocol", "enum", "record"))
        leaves(n, hb, is_class)
        leaves(n, hf, False)
        full = hf.hexdigest()
        placeholders[n.id] = full
        key = _unique(qual, symbols)
        symbols[key] = {"kind": "class" if is_class else "def", "start": n.start_point[0] + 1,
                        "def": n.start_point[0] + 1, "end": n.end_point[0] + 1, "sig": hs.hexdigest(),
                        "body": hb.hexdigest(), "full": full, "doc": None}

    def _ts_nested(n):
        out, stack = [], [c for c in n.children]
        while stack:
            cur = stack.pop()
            if is_def(cur):
                out.append(cur)
                continue
            stack.extend(cur.children)
        return sorted(out, key=lambda c: c.start_byte)

    module: dict[str, dict] = {}
    top_h = _h()
    for ch in root.children:
        if is_def(ch):
            process(ch, "")
            continue
        for d in _ts_nested(ch):  # e.g. `export function f` / decorated definitions
            process(d, "")
        if "comment" in ch.type:
            continue
        h = _h()
        leaves(ch, h, False)
        hx = h.hexdigest()
        top_h.update(hx.encode())
        module[_unique("STMT:" + hx[:12], module)] = {"start": ch.start_point[0] + 1, "end": ch.end_point[0] + 1,
                                                       "h": hx}
    # No per-name binding table for these grammars: every name's binding is
    # the fingerprint of all top-level non-definition statements (imports and
    # declarations) - coarser, never less safe.
    return {"lang": suffix.lstrip("."), "symbols": symbols, "bindings": {}, "module": module,
            "top": top_h.hexdigest()}


# =============================================================================
# public API: facts
# =============================================================================

def scheme_for(path: str | Path) -> str | None:
    suffix = Path(str(path)).suffix.lower()
    if suffix == ".py" or suffix == ".pyi":
        return PY_SCHEME
    if suffix in MD_SUFFIXES:
        return MD_SCHEME
    if suffix in TS_LANGS:
        return TS_SCHEME
    return None


def compute_facts(rel: str, data: bytes) -> dict | None:
    """Facts for one file version, or None when the language is unsupported."""
    scheme = scheme_for(rel)
    if scheme is None:
        return None
    text = data.decode("utf-8", errors="replace")
    suffix = Path(rel).suffix.lower()
    try:
        if scheme == PY_SCHEME:
            facts = _python_facts(text)
        elif scheme == MD_SCHEME:
            facts = _markdown_facts(text)
        else:
            facts = _ts_facts(text, suffix)
            if facts is None:
                return None
    except (SyntaxError, ValueError, RecursionError, MemoryError) as exc:
        return {"scheme": scheme, "lang": "python" if scheme == PY_SCHEME else suffix, "error": type(exc).__name__}
    facts["scheme"] = scheme
    facts["lines"] = text.count("\n") + (0 if text.endswith("\n") or not text else 1)
    return facts


def usable(facts: dict | None) -> bool:
    return bool(facts) and "error" not in facts


_MEM: dict[tuple[str, str], dict | None] = {}


def _mem_get(sha: str, scheme: str) -> dict | None:
    return _MEM.get((sha, scheme))


def _mem_put(sha: str, scheme: str, facts: dict | None) -> None:
    if len(_MEM) > 2048:
        _MEM.clear()
    _MEM[(sha, scheme)] = facts


def facts_by_sha(store, rel: str, sha256: str) -> dict | None:
    """Cached facts for a file version identified only by its hash (None on a cache miss)."""
    scheme = scheme_for(rel)
    if scheme is None or not sha256:
        return None
    hit = _mem_get(sha256, scheme)
    if hit is not None:
        return hit
    if store is not None:
        facts = store.file_facts(sha256, scheme)
        if facts is not None:
            _mem_put(sha256, scheme, facts)
            return facts
    return None


def facts_for(store, repo: Path, rel: str, *, sha256: str | None = None) -> dict | None:
    """Facts of ``repo/rel``; with ``sha256`` only for that exact version.

    The file is read and hashed; when it no longer has the requested content
    the cache is the only source (and a miss returns None: file level).
    """
    scheme = scheme_for(rel)
    if scheme is None:
        return None
    if sha256:
        hit = facts_by_sha(store, rel, sha256)
        if hit is not None:
            return hit
    try:
        data = (Path(repo) / rel).read_bytes()
    except OSError:
        return None
    sha = hashlib.sha256(data).hexdigest()
    if sha256 and sha != sha256:
        return None
    hit = facts_by_sha(store, rel, sha)
    if hit is not None:
        return hit
    facts = compute_facts(rel, data)
    if facts is None:
        return None
    facts["sha256"] = sha
    _mem_put(sha, scheme, facts)
    if store is not None:
        try:
            store.put_file_facts(sha, scheme, facts)
        except Exception:  # a read-only or busy store must not break a check
            pass
    return facts


def update_facts(store, repo: Path, changed_paths: Iterable[str | Path] | None) -> dict:
    """Compute and cache facts for changed/added files (called after an index build).

    Returns counts and the time taken; unsupported and deleted files are skipped.
    """
    repo = Path(repo).resolve()
    t0 = time.perf_counter()
    out = {"computed": 0, "cached": 0, "unsupported": 0, "missing": 0, "errors": 0}
    for p in changed_paths or []:
        p = Path(p)
        try:
            rel = (p.resolve().relative_to(repo) if p.is_absolute() else p).as_posix()
        except ValueError:
            out["missing"] += 1
            continue
        scheme = scheme_for(rel)
        if scheme is None:
            out["unsupported"] += 1
            continue
        try:
            data = (repo / rel).read_bytes()
        except OSError:
            out["missing"] += 1
            continue
        sha = hashlib.sha256(data).hexdigest()
        if facts_by_sha(store, rel, sha) is not None:
            out["cached"] += 1
            continue
        facts = facts_for(store, repo, rel, sha256=sha)
        if not usable(facts):
            out["errors"] += 1
        else:
            out["computed"] += 1
    out["ms"] = round(1000 * (time.perf_counter() - t0), 1)
    return out


# =============================================================================
# dependency keys and facet fingerprints
# =============================================================================

def sym_key(path: str, qual: str) -> str:
    return f"sym:{path}::{qual}"


def split_key(dep_key: str) -> tuple[str, str, str]:
    """``sym:a/b.py::C.m`` -> ("sym", "a/b.py", "C.m"); ``file:a.py`` -> ("file", "a.py", "")."""
    kind, _, rest = dep_key.partition(":")
    path, _, name = rest.partition("::")
    return kind, path, name


def facet_fp(facts: dict | None, dep_key: str, facet: str) -> str | None:
    """Fingerprint of one facet in these facts; ABSENT when missing; None when facts are unusable."""
    if not usable(facts):
        return None
    kind, _, name = split_key(dep_key)
    if kind == "sym":
        s = facts.get("symbols", {}).get(name)
        return ABSENT if s is None else (s.get(facet) or ABSENT)
    if kind == "bind":
        if facts.get("lang") != "python":
            return facts.get("top") or ABSENT
        b = facts.get("bindings", {})
        return b.get(name) or (_hex(["star-only", b["*"]]) if "*" in b else ABSENT)
    if kind == "mod":
        m = facts.get("module", {}).get(name)
        return ABSENT if m is None else m["h"]
    if kind == "sec":
        s = facts.get("sections", {}).get(name)
        return ABSENT if s is None else s["h"]
    return None


def enclosing(facts: dict | None, start: int, end: int | None = None) -> tuple[str, str] | None:
    """Innermost symbol containing ``start..end``, else the module statement or Markdown section."""
    if not usable(facts):
        return None
    end = end or start
    best = None
    for q, s in facts.get("symbols", {}).items():
        if s["start"] <= start and end <= s["end"]:
            if best is None or (s["end"] - s["start"]) < (best[1]["end"] - best[1]["start"]):
                best = (q, s)
    if best:
        return ("sym", best[0])
    for key, m in facts.get("module", {}).items():
        if m["start"] <= start and end <= m["end"]:
            return ("mod", key)
    secs = [(k, s) for k, s in facts.get("sections", {}).items() if s["start"] <= start <= s["end"]]
    if secs:
        k, s = secs[0]
        if end <= s["end"]:
            return ("sec", k)
    return None


def symbols_named(facts: dict | None, name: str) -> list[tuple[str, dict]]:
    """Symbols named ``name`` (``m`` matches ``C.m``; ``C.m`` matches only ``C.m``)."""
    if not usable(facts):
        return []
    name = name.strip().lstrip(".").split("(")[0].strip()
    if not name:
        return []
    return [(q, s) for q, s in facts.get("symbols", {}).items()
            if q.split("#")[0] == name or q.split("#")[0].endswith("." + name)]


def sections_overlapping(facts: dict | None, start: int, end: int) -> list[str]:
    if not usable(facts):
        return []
    return [k for k, s in facts.get("sections", {}).items() if s["start"] <= end and start <= s["end"]]


# =============================================================================
# anchors and relocation
# =============================================================================

@dataclass
class Relocation:
    status: str  # same | moved | changed | gone | unanchored
    start: int | None = None
    end: int | None = None
    detail: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in ("same", "moved")


@lru_cache(maxsize=64)
def _parse_py(text: str) -> ast.AST | None:
    try:
        return parse_python(text)
    except (SyntaxError, ValueError, RecursionError):
        return None


def _find_def(tree: ast.AST, qual: str, facts: dict) -> ast.AST | None:
    """The def node for ``qual`` (duplicates resolved through the facts' line numbers)."""
    s = facts.get("symbols", {}).get(qual)
    if s is None:
        return None
    for node in ast.walk(tree):
        if isinstance(node, _DEF_NODES) and node.lineno == s["def"]:
            return node
    return None


def _ast_path(defnode: ast.AST, line: int) -> tuple[list, ast.stmt | None]:
    """(field, index) path from the def to the innermost statement containing ``line``."""
    best: tuple[list, ast.stmt | None] = ([], None)
    containers = (ast.excepthandler,) + ((ast.match_case,) if hasattr(ast, "match_case") else ())

    def contains(n: ast.AST) -> bool:
        lo = getattr(n, "lineno", None)
        return lo is not None and lo <= line <= (getattr(n, "end_lineno", None) or lo)

    def walk(node: ast.AST, path: list) -> None:
        nonlocal best
        for fname, value in ast.iter_fields(node):
            items = value if isinstance(value, list) else [value]
            for i, ch in enumerate(items):
                step = path + [[fname, i if isinstance(value, list) else None]]
                if isinstance(ch, ast.stmt) and contains(ch):
                    best = (step, ch)
                    walk(ch, step)
                elif isinstance(ch, containers):
                    walk(ch, step)  # except/case clauses hold statements

    walk(defnode, [])
    return best


def _resolve_path(defnode: ast.AST, path: list) -> ast.AST | None:
    node: Any = defnode
    try:
        for fname, i in path:
            v = getattr(node, fname)
            node = v[i] if i is not None else v
    except (AttributeError, IndexError, TypeError):
        return None
    return node if isinstance(node, ast.stmt) else None


def _occurrence(lines: list[str], lo: int, hi: int, line: int) -> int:
    """How many earlier lines in ``lo..hi`` have the same normalised text as ``line``."""
    target = norm_line(lines[line - 1]) if 0 < line <= len(lines) else ""
    return sum(1 for i in range(lo, line) if 0 < i <= len(lines) and norm_line(lines[i - 1]) == target)


def make_anchor(facts: dict | None, text: str, start: int, end: int) -> dict | None:
    """Anchor for cited lines ``start..end`` of a file with these facts (None when unsupported)."""
    where = enclosing(facts, start, end)
    if where is None:
        return None
    kind, key = where
    lines = text.splitlines()
    anchor: dict[str, Any] = {"scheme": facts["scheme"], "n_lines": end - start + 1}
    if 0 < start <= len(lines):
        anchor["first_line"] = norm_line(lines[start - 1])
    if kind == "sym":
        s = facts["symbols"][key]
        anchor.update(sym=key, fp=s["full"], off=start - s["start"],
                      occ=_occurrence(lines, s["start"], s["end"], start))
        if facts.get("lang") == "python":
            # body fingerprint and docstring presence: a docstring-only edit keeps the code
            # (and, shifted by one statement, the AST path) valid
            anchor.update(bfp=s["body"], has_doc=s.get("doc") is not None)
            tree = _parse_py(text)
            d = _find_def(tree, key, facts) if tree is not None else None
            if d is not None:
                path, stmt = _ast_path(d, start)
                if stmt is not None:
                    anchor.update(ast_path=path, stmt_off=start - stmt.lineno)
    elif kind == "mod":
        m = facts["module"][key]
        anchor.update(mod=key, fp=m["h"], off=start - m["start"], occ=_occurrence(lines, m["start"], m["end"], start))
    else:
        sec = facts["sections"][key]
        anchor.update(sec=key, fp=sec["h"], off=start - sec["start"],
                      occ=_occurrence(lines, sec["start"], sec["end"], start))
    return anchor


def _block_at(lines: list[str], start: int, n: int) -> str | None:
    if start < 1 or start + n - 1 > len(lines):
        return None
    return "\n".join(lines[start - 1: start - 1 + n])


def relocate_in(anchor: dict, facts: dict | None, text: str, content_hash: str | None,
                old_start: int | None) -> Relocation:
    """Re-find anchored lines in a new file version (see the module docstring for the outcomes)."""
    if not usable(facts) or facts.get("scheme") != anchor.get("scheme"):
        return Relocation("unanchored", detail={"why": "no comparable facts for this file version"})
    lines = text.splitlines()
    n = int(anchor.get("n_lines") or 1)
    if "sym" in anchor:
        region = facts.get("symbols", {}).get(anchor["sym"])
        what = f"symbol {anchor['sym']}"
        cur_fp = region["full"] if region else None
    elif "mod" in anchor:
        region = facts.get("module", {}).get(anchor["mod"])
        what = f"module statement {anchor['mod']}"
        cur_fp = region["h"] if region else None
    else:
        region = facts.get("sections", {}).get(anchor.get("sec", ""))
        what = f"section {anchor.get('sec')}"
        cur_fp = region["h"] if region else None
    if region is None:
        return Relocation("gone", detail={"why": f"{what} no longer exists"})
    lo, hi = region["start"], region["end"]

    def matches(s: int) -> bool:
        blk = _block_at(lines, s, n)
        return blk is not None and (content_hash is None or text_hash(blk) == content_hash)

    def verdict(s: int, how: str) -> Relocation:
        st = "same" if s == old_start else "moved"
        return Relocation(st, s, s + n - 1, {"via": how})

    candidates: list[int] = []
    first = anchor.get("first_line")
    k = int(anchor.get("occ") or 0)

    def kth_occurrence() -> int | None:
        if not first:
            return None
        ordered = [i for i in range(lo, hi + 1) if 0 < i <= len(lines) and norm_line(lines[i - 1]) == first]
        return ordered[k] if k < len(ordered) else None

    if cur_fp == anchor.get("fp"):
        if "ast_path" in anchor and "sym" in anchor:
            tree = _parse_py(text)
            d = _find_def(tree, anchor["sym"], facts) if tree is not None else None
            stmt = _resolve_path(d, anchor["ast_path"]) if d is not None else None
            if stmt is not None:
                s = stmt.lineno + int(anchor.get("stmt_off") or 0)
                if matches(s):
                    return verdict(s, "ast_path")
                candidates.append(s)
        s = lo + int(anchor.get("off") or 0)
        if matches(s):
            return verdict(s, "offset")
        candidates.append(s)
        # Same code, re-formatted around the citation: the block that is unique in the
        # region, or the k-th occurrence of the first cited line (same code, same order).
        if content_hash is not None:
            hits = [i for i in range(lo, hi - n + 2) if matches(i)]
            kth = kth_occurrence()
            pick = hits[0] if len(hits) == 1 else (kth if kth in hits else None)
            if pick is not None:
                return verdict(pick, "occurrence")
        return Relocation("changed", candidates[0], candidates[0] + n - 1,
                          {"why": f"{what} is unchanged but the cited text differs (comment or formatting)"})
    # Only the docstring changed (same body fingerprint): the statements are the same code, one
    # index further or closer when a docstring was added or removed.
    if "sym" in anchor and anchor.get("bfp") and region.get("body") == anchor["bfp"] and content_hash is not None:
        path = [list(step) for step in anchor.get("ast_path") or []]
        shift = int(region.get("doc") is not None) - int(bool(anchor.get("has_doc")))
        if path and path[0][0] == "body" and isinstance(path[0][1], int):
            path[0][1] += shift
        tree = _parse_py(text)
        d = _find_def(tree, anchor["sym"], facts) if tree is not None else None
        stmt = _resolve_path(d, path) if (d is not None and path) else None
        if stmt is not None:
            s = stmt.lineno + int(anchor.get("stmt_off") or 0)
            if matches(s):
                return verdict(s, "ast_path (docstring edit)")
        hits = [i for i in range(lo, hi - n + 2) if matches(i)]
        if len(hits) == 1:
            return verdict(hits[0], "unique block (docstring edit)")
    # The region changed: a candidate location only, never verification.
    cand = kth_occurrence() or (lo + int(anchor.get("off") or 0))
    return Relocation("changed", cand, cand + n - 1, {"why": f"{what} changed since the evidence was recorded"})


def anchor_for_file(path: Path, rel: str, start: int, end: int) -> dict | None:
    """Anchor for lines of a file on disk (facts computed in-process, no store)."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    scheme = scheme_for(rel)
    if scheme is None:
        return None
    sha = hashlib.sha256(data).hexdigest()
    facts = _mem_get(sha, scheme)
    if facts is None:
        facts = compute_facts(rel, data)
        if facts is not None:
            facts["sha256"] = sha
            _mem_put(sha, scheme, facts)
    if not usable(facts):
        return None
    text = data.decode("utf-8", errors="replace")
    return make_anchor(facts, text, start, end)


def relocate_file(path: Path, rel: str, anchor: dict, content_hash: str | None,
                  old_start: int | None) -> Relocation:
    """:func:`relocate_in` against the file currently on disk."""
    try:
        data = path.read_bytes()
    except OSError:
        return Relocation("gone", detail={"why": f"file no longer exists: {rel}"})
    scheme = scheme_for(rel)
    sha = hashlib.sha256(data).hexdigest()
    facts = _mem_get(sha, scheme) if scheme else None
    if facts is None and scheme:
        facts = compute_facts(rel, data)
        if facts is not None:
            facts["sha256"] = sha
            _mem_put(sha, scheme, facts)
    return relocate_in(anchor, facts, data.decode("utf-8", errors="replace"), content_hash, old_start)
