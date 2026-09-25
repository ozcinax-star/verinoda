"""The syntax of each Python file that the cross-file import pass looks at, kept between builds.

The cross-file import pass (``_resolve_cross_file_imports``) parses every ``.py`` file that has
symbols and walks every node of it on each build: about 3.2 million calls and 4-6 seconds of a
``verinoda update`` of Verinoda's own repository. What that walk can use depends only on the file's
bytes: its ``from ... import`` statements, the class and function definitions, and the identifiers
inside a definition whose name one of those imports binds. So each file is parsed once, and a
*pruned tree* holding just those nodes is kept per file, keyed by a hash of the bytes. For the
duration of the pass the upstream parse (``_parse_python_tree``) is replaced by one that hands out
the pruned tree; the upstream function itself runs unchanged (no file under
``verinoda/project_index`` is edited), so where imports point and which edges are added or
repointed are still worked out on every build from every file's current nodes.
``tests/test_python_cross.py`` compares the two.

Why pruning keeps the result exact (the upstream ``visit``): a node that is not an import, a
definition or an identifier only passes the enclosing symbol on to its children, so it is replaced
by its children; an identifier outside every definition is never recorded; an identifier whose name
no import binds is recorded but never read; and among the kept children of one definition, all get
the same enclosing symbol, so a later identifier with the same name as an earlier one is a no-op
(``setdefault``). A definition with none of these below it has no effect and is dropped. This
relies on what the upstream walk reads, so the pruned trees are used only while the upstream
function is the one they were checked against (``PINNED``); otherwise the pass runs as upstream.

The pass then repoints type references: for each importing file it scans every edge, but it can
only change an edge whose relation is a type reference (``_TYPE_REPOINT_RELATIONS``, about 8% of the
edges here). It is given just those edges; the only step that needs every edge, dropping the stubs
it repointed edges away from unless some edge still names them, runs on a scratch copy of the nodes
and is redone over every edge (see ``cross``).
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import sys
import tempfile
from pathlib import Path

from verinoda.python_facts import parse_current

VERSION = 1
FILE = "python_cross.json"
# blake2b-64 of the source of resolution._resolve_cross_file_imports the pruning was checked against.
# When the vendored function changes, the pass runs as upstream (and tests/test_python_cross.py fails)
# until the node types and fields its walk reads are checked again and this is updated.
PINNED = "d4c06d192b42dc4d"
DEEP = 400  # a file whose tree is this deep gets its full tree (the upstream walk is recursive)
# the relations of the edges the upstream repoint loop can change (a copy of its local constant)
_TYPE_REPOINT_RELATIONS = frozenset({"references", "inherits", "implements", "extends"})
_KEPT_TYPES = ("identifier", "import_from_statement", "class_definition", "function_definition")


def _source_digest(*objs) -> str:
    try:
        text = "".join(inspect.getsource(obj) for obj in objs)
    except (OSError, TypeError):
        return "?"
    return hashlib.blake2b(text.encode("utf-8"), digest_size=8).hexdigest()


def _stamp(res, fn_digest: str) -> str:
    """What the kept trees were made with: the tree-sitter packages, the upstream parse and pass."""
    from importlib import metadata

    vers = []
    for dist in ("tree-sitter", "tree-sitter-python"):
        try:
            vers.append(metadata.version(dist))
        except metadata.PackageNotFoundError:
            vers.append("?")
    parse = _source_digest(res._parse_python_tree, res._parse_python_tree_cached)
    return f"{VERSION}:{':'.join(vers)}:{parse}:{fn_digest}"


# -- pruning (a file not in the cache) ---------------------------------------------------------------

class _Whole(Exception):
    """This file is handed to the upstream walk as its full tree."""


def _prune(source: bytes, root) -> tuple[list | None, int]:
    """The kept tree of one parsed file (JSON-able lists) and the depth of its full tree.

    The kept tree is None when the full tree is to be walked: a tree ``DEEP`` or more levels deep,
    or a shape tree-sitter-python does not produce (a root or an identifier the walk would treat
    specially).
    """

    def text(n) -> str:
        return source[n.start_byte:n.end_byte].decode("utf-8", errors="replace")

    bound: set[str] = set()  # local names any `from ... import` of the file binds

    def import_record(node) -> list:
        # the children resolve_import reads, in their order: the module, the keyword, the names
        kids, past = [], False
        for child in node.children:
            t = child.type
            if t == "relative_import":
                kids.append(["r", [[s.type, text(s)] for s in child.children
                                   if s.type in ("import_prefix", "dotted_name")]])
            elif t == "dotted_name":
                kids.append(["d", text(child)])
                if past:
                    bound.add(kids[-1][1])
            elif t == "import":
                kids.append(["k"])
                past = True
            elif t == "aliased_import":
                nm, al = child.child_by_field_name("name"), child.child_by_field_name("alias")
                rec = ["a", None if nm is None else [nm.type, text(nm)], None if al is None else [al.type, text(al)]]
                kids.append(rec)
                if past and nm is not None:
                    bound.add(rec[2][1] if al is not None else rec[1][1])
        return ["m", kids]

    # A kept node is a list [tag, ..., children]; its children are in the order the walk visits
    # them, with every other node replaced by its children. Nodes are told apart by type only, as
    # the upstream walk does.
    maxd = 1

    def walk(node, out: list, seen: set, in_def: bool, d: int) -> None:
        nonlocal maxd
        if d > maxd:
            maxd = d
            if d >= DEEP:
                raise _Whole
        for c in node.children:
            t = c.type
            if t == "identifier":
                if c.child_count:
                    raise _Whole
                if in_def:
                    name = source[c.start_byte:c.end_byte]
                    if name not in seen:  # a later one under the same symbol is a no-op
                        seen.add(name)
                        out.append(["i", name.decode("utf-8", errors="replace"), c.start_point[0]])
            elif t == "import_from_statement":
                out.append(import_record(c))
            elif t == "class_definition" or t == "function_definition":
                nm = c.child_by_field_name("name")
                rec = ["c" if t == "class_definition" else "f", None if nm is None else text(nm), []]
                out.append(rec)
                walk(c, rec[2], set(), True, d + 1)
            elif c.child_count:
                walk(c, out, seen, in_def, d + 1)

    if root.type in _KEPT_TYPES:
        return None, maxd
    top = ["R", root.type, []]
    try:
        walk(root, top[2], set(), False, 1)
    except _Whole:
        return None, maxd

    def keep(children: list) -> list:  # only names an import binds, only definitions with something kept
        kept = []
        for rec in children:
            tag = rec[0]
            if tag == "i":
                if rec[1] in bound:
                    kept.append(rec)
            elif tag in ("c", "f"):
                rec[2] = keep(rec[2])
                if rec[2]:
                    kept.append(rec)
            else:
                kept.append(rec)
        return kept

    top[2] = keep(top[2])
    return top, maxd


# -- the stand-in tree (a file in the cache) ---------------------------------------------------------

class _Node:
    """The parts of a tree-sitter node that the upstream cross-file walk reads."""

    __slots__ = ("type", "children", "start_byte", "end_byte", "start_point", "fields")

    def __init__(self, type_, children=(), span=(0, 0), row=0, fields=None):
        self.type = type_
        self.children = list(children)
        self.start_byte, self.end_byte = span
        self.start_point = (row, 0)  # the walk reads the row only
        self.fields = fields

    def child_by_field_name(self, name):
        return self.fields.get(name) if self.fields else None


def _build(kept: list) -> tuple[bytes, _Node]:
    """(source, root) standing in for the parse; the texts are laid out in a small byte string."""
    buf = bytearray()

    def span(s: str) -> tuple[int, int]:
        start = len(buf)
        buf.extend(s.encode("utf-8"))  # decode(errors="replace") of this gives s back
        return start, len(buf)

    def node(rec) -> _Node:
        tag = rec[0]
        if tag == "i":
            if type(rec[2]) is not int:
                raise TypeError(rec[2])
            return _Node("identifier", span=span(rec[1]), row=rec[2])
        if tag in ("c", "f"):
            name = None if rec[1] is None else {"name": _Node("identifier", span=span(rec[1]))}
            return _Node("class_definition" if tag == "c" else "function_definition",
                         [node(c) for c in rec[2]], fields=name)
        if tag == "m":
            kids = []
            for k in rec[1]:
                if k[0] == "r":
                    kids.append(_Node("relative_import", [_Node(t, span=span(s)) for t, s in k[1]]))
                elif k[0] == "d":
                    kids.append(_Node("dotted_name", span=span(k[1])))
                elif k[0] == "k":
                    kids.append(_Node("import"))
                elif k[0] == "a":
                    f = {}
                    if k[1] is not None:
                        f["name"] = _Node(k[1][0], span=span(k[1][1]))
                    if k[2] is not None:
                        f["alias"] = _Node(k[2][0], span=span(k[2][1]))
                    kids.append(_Node("aliased_import", fields=f))
                else:
                    raise ValueError(k[0])
            return _Node("import_from_statement", kids)
        raise ValueError(tag)

    if kept[0] != "R":
        raise ValueError(kept[0])
    root = _Node(kept[1], [node(c) for c in kept[2]])
    return bytes(buf), root


def _typed_edges(all_edges: list) -> list | None:
    """The edges the upstream repoint loop can change, in their order; None if it must see them all.

    The loop reads every edge's source file and relation, and the prune after it every edge's
    source and target. When one of those is not a hashable value upstream may fail on that edge, so
    it is then given every edge and fails where it fails.
    """
    typed = []
    try:
        for e in all_edges:
            hash(e.get("source_file")), hash(e.get("source")), hash(e.get("target"))
            if e.get("relation") in _TYPE_REPOINT_RELATIONS:
                typed.append(e)
    except Exception:
        return None
    return typed


def _frames() -> int:
    f, n = sys._getframe(), 0
    while f is not None:
        n, f = n + 1, f.f_back
    return n


# -- the context manager -----------------------------------------------------------------------------

class python_cross_cache:
    """Context manager: during a build, the cross-file import pass walks a kept pruned tree per unchanged file."""

    def __init__(self, index_dir: Path, pinned: str | None = None):
        self.path = Path(index_dir) / FILE
        self.pinned = PINNED if pinned is None else pinned
        self.hits = self.misses = self.deep = 0
        self.active = False

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError):
            return {}
        files = data.get("files") if isinstance(data, dict) and data.get("stamp") == self.stamp else None
        return files if isinstance(files, dict) else {}  # a file of another shape counts as empty

    def _save(self, files: dict) -> None:
        text = json.dumps({"stamp": self.stamp, "files": files}, separators=(",", ":"))
        tmp = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix="python_cross.", suffix=".tmp", dir=str(self.path.parent))
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, self.path)  # refused on Windows while another process has the file open
            tmp = None
        except OSError:
            pass  # a cache: the next build parses the files again
        finally:
            if tmp is not None:  # not moved into place: no temp file is left behind
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    def __enter__(self):
        from verinoda.project_index import extract
        from verinoda.project_index.extractors import resolution as res

        self.res, self.extract = res, extract
        self.real = res._resolve_cross_file_imports
        self.real_in_extract = extract._resolve_cross_file_imports
        digest = _source_digest(self.real)
        if self.real_in_extract is not self.real or digest != self.pinned:
            return self  # not the function the pruning was checked against: upstream runs as it is
        try:
            import tree_sitter  # noqa: F401  (without it the upstream parse gives None)
        except ImportError:
            return self
        self.stamp = _stamp(res, digest)
        cache = self._load()
        real, real_parse = self.real, res._parse_python_tree

        def cross(per_file, paths, all_nodes=None, all_edges=None):
            # frames the upstream walk may use before the recursion limit, with room to spare (see DEEP)
            room = sys.getrecursionlimit() - _frames() - 32
            seen, changed, done = {}, False, False

            def parse(path: Path):
                nonlocal changed
                key = str(path)
                try:
                    data = path.read_bytes()
                except OSError:
                    return None  # the upstream parse fails the same way
                digest = hashlib.blake2b(data, digest_size=16).hexdigest()
                hit = cache.get(key)
                if isinstance(hit, dict) and hit.get("h") == digest:
                    try:
                        built = _build(hit["t"]) if hit["t"] is not None and hit["d"] < room else None
                    except (AttributeError, KeyError, IndexError, TypeError, ValueError):
                        built = False  # a damaged entry: parsed again below
                    if built is not False:
                        self.hits += 1
                        seen[key] = hit
                        if built is not None:
                            return built
                        self.deep += 1
                        return parse_current(res, path, data, real_parse)  # the full tree, as without the cache
                self.misses += 1
                parsed = parse_current(res, path, data, real_parse)
                if parsed is None:
                    return None
                if room < DEEP + 64:  # no room for the pruning walk: the full tree, not kept
                    self.deep += 1
                    return parsed
                source, root = parsed
                kept, depth = _prune(source, root)
                if source == data:  # the parse read the bytes that were hashed
                    seen[key] = {"h": digest, "d": depth, "t": kept}
                    changed = True
                if kept is None or depth >= room:
                    self.deep += 1
                    return parsed
                return _build(kept)

            # The repoint loop scans every edge once per importing file but can only change the typed
            # ones, so it is given those; the prune of stubs it repointed away from then runs on a
            # scratch copy of the nodes and is redone below over every edge.
            nodes_arg, edges_arg = all_nodes, all_edges
            typed = _typed_edges(all_edges) if all_nodes is not None and all_edges else None
            if typed is not None:
                nodes_arg, edges_arg = list(all_nodes), typed
            res._parse_python_tree = parse
            # the upstream walk is recursive, and this wrapper adds one frame below it
            limit = sys.getrecursionlimit()
            sys.setrecursionlimit(limit + 1)
            try:
                out = real(per_file, paths, nodes_arg, edges_arg)
                if nodes_arg is not all_nodes and len(nodes_arg) != len(all_nodes):
                    # upstream dropped stubs it repointed edges away from that no typed edge names; its
                    # rule keeps one that any edge names (the typed edges are some of all the edges)
                    still_referenced = {e.get("source") for e in all_edges} | {e.get("target") for e in all_edges}
                    dropped = {n.get("id") for n in all_nodes} - {n.get("id") for n in nodes_arg}
                    all_nodes[:] = [
                        node for node in all_nodes
                        if node.get("id") not in dropped or node.get("id") in still_referenced
                    ]
                done = True
                return out
            finally:
                sys.setrecursionlimit(limit)
                res._parse_python_tree = real_parse
                # a finished pass keeps only this build's files; one that failed part-way keeps the rest too
                files = seen if done else {**cache, **seen}
                if changed or set(files) != set(cache):
                    self._save(files)
                    cache.clear()
                    cache.update(files)

        res._resolve_cross_file_imports = cross
        extract._resolve_cross_file_imports = cross
        self.active = True
        return self

    def __exit__(self, *a):
        self.res._resolve_cross_file_imports = self.real
        self.extract._resolve_cross_file_imports = self.real_in_extract
        return False
