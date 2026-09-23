"""Facade over verinoda.project_index (the Graphify-derived extractor).

The rest of Verinoda talks to the knowledge graph only through this module,
so the vendored package can be re-synced from upstream without touching the
analysis layers. All building is AST-only (no LLM, no network).

Build-time work is done once and not repeated at query time (docs/DESIGN.md D21):

* **Symbol spans** come from one pass per file: Python ``ast`` end lines,
  tree-sitter ``end_point`` for the other languages, and for Markdown the
  section of a heading, which ends before the next heading of the same or a
  higher level. Only when none of these knows a symbol does the old
  next-symbol rule (capped at :data:`HEURISTIC_SPAN_CAP` lines) apply, and
  :meth:`Graph.span_basis` says so.
* **Receiver-call edges** (``param.method()`` on annotated parameters) are
  computed by :func:`build` and stored next to the graph in
  ``receiver_calls.json``, keyed by the graph file's identity; :func:`load`
  only applies them. Per-file facts are cached under the file's sha256, so an
  incremental build re-parses only changed files.
* The vendored rebuild's path-identity helpers are memoised by a monkeypatch
  applied from :func:`build` (see :func:`install_path_identity_memo`); the
  graph it writes is unchanged.
"""

from __future__ import annotations

import ast
import hashlib
import io
import json
import os
import re
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import networkx as nx

from verinoda.paths import configure_index_env, ensure_atlas, graph_path, index_dir, receiver_calls_path

configure_index_env()

CODE_RELATIONS = {"calls", "imports", "imports_from", "uses", "inherits", "method", "implements",
                  "extends", "references", "contains"}
FLOW_RELATIONS = {"calls"}
RECEIVER_ORIGIN = "verinoda.receiver"
JAVA_CALL_ORIGIN = "verinoda.java_calls"
RECEIVER_SIDECAR_VERSION = 2
HEURISTIC_SPAN_CAP = 80        # the next-symbol fallback never spans more lines than this
PROSE_SUFFIXES = (".md", ".markdown", ".mdx", ".rst", ".txt", ".adoc")
MARKDOWN_SUFFIXES = (".md", ".markdown", ".mdx")
_CACHE_MAX = 4096              # per-file caches are cleared when they grow past this


def build(repo: Path, *, force: bool = False, changed: list[Path] | None = None,
          quiet: bool = True) -> dict:
    """Run the Graphify-derived AST pipeline; return graph stats.

    After a successful rebuild the receiver-call sidecar is refreshed (per-file
    facts are reused for files whose content did not change).
    """
    from verinoda.project_index.watch import _rebuild_code

    install_path_identity_memo()
    repo = Path(repo).resolve()
    ensure_atlas(repo)  # the upstream pipeline expects its output directory to exist
    index_dir(repo).mkdir(parents=True, exist_ok=True)
    buf = io.StringIO()
    # The upstream pipeline also logs to stderr (e.g. hints to run `graphify
    # label`, which is not a Verinoda command); keep both streams in the log.
    with (redirect_stdout(buf) if quiet else _null()), (redirect_stderr(buf) if quiet else _null()),             _without_report_questions():
        ok = _rebuild_code(repo, changed_paths=changed, force=force, block_on_lock=True)
    gp = graph_path(repo)
    if not ok and not gp.exists():
        raise RuntimeError("index build failed:\n" + buf.getvalue()[-2000:])
    data = json.loads(gp.read_text(encoding="utf-8"))
    out = {"ok": bool(ok), "graph_path": str(gp), "nodes": len(data.get("nodes", [])),
           "edges": len(data.get("links", data.get("edges", []))), "log": buf.getvalue()[-2000:]}
    if ok:
        try:
            out["receiver_calls"] = refresh_receiver_sidecar(repo)
        except (OSError, ValueError) as exc:  # the sidecar is derived data; load() recomputes it
            out["receiver_calls"] = {"error": f"{type(exc).__name__}: {exc}"[:300]}
    return out


class _without_report_questions:
    """Skip the "suggested questions" of the upstream GRAPH_REPORT.md during a build.

    Verinoda never reads the report; on a 2,000-file repository the suggestions took
    8.5 of 42 s of a full rebuild. The graph and every other output are unchanged.
    """

    def __enter__(self):
        try:
            from verinoda.project_index import analyze as upstream_analyze
        except Exception:  # noqa: BLE001 - nothing to skip
            self.mod = None
            return self
        self.mod, self.real = upstream_analyze, upstream_analyze.suggest_questions
        upstream_analyze.suggest_questions = lambda *a, **k: []
        return self

    def __exit__(self, *a):
        if self.mod is not None:
            self.mod.suggest_questions = self.real
        return False


class _null:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# -- vendored rebuild: path-identity memo ------------------------------------------------------

def install_path_identity_memo() -> bool:
    """Memoise ``watch._StoredSourcePaths.identity``/``in_watch_root``/``rebase_preserved``.

    The vendored incremental rebuild recomputes a pathlib identity for every
    node and edge (about 60% of a one-file update on graphify_core). All three
    methods are pure functions of the ``source_file`` string and of fields set
    once in ``__init__``, so a per-instance dict memo returns exactly what the
    original would. Applied as a monkeypatch (the vendored file stays
    read-only); idempotent. Returns True when the patch is in place.
    """
    try:
        from verinoda.project_index import watch
    except Exception:  # noqa: BLE001 - a missing/renamed upstream class just means no memo
        return False
    cls = getattr(watch, "_StoredSourcePaths", None)
    if cls is None:
        return False
    if getattr(cls, "_verinoda_memo", False):
        return True
    orig_identity, orig_in_root, orig_rebase = cls.identity, cls.in_watch_root, cls.rebase_preserved

    def _memo(self, name: str) -> dict:
        memo = self.__dict__.get("_verinoda_memo_" + name)
        if memo is None:
            memo = self.__dict__["_verinoda_memo_" + name] = {}
        return memo

    def identity(self, source_file):
        memo = _memo(self, "identity")
        try:
            return memo[source_file]
        except KeyError:
            v = memo[source_file] = orig_identity(self, source_file)
            return v
        except TypeError:  # unhashable source_file: no memo
            return orig_identity(self, source_file)

    def in_watch_root(self, source_file):
        memo = _memo(self, "in_root")
        try:
            return memo[source_file]
        except KeyError:
            v = memo[source_file] = orig_in_root(self, source_file)
            return v
        except TypeError:
            return orig_in_root(self, source_file)

    def rebase_preserved(self, item):
        sf = item.get("source_file")
        memo = _memo(self, "rebase")
        try:
            new = memo[sf]
        except KeyError:
            probe = {"source_file": sf}
            orig_rebase(self, probe)
            new = memo[sf] = probe["source_file"]
        except TypeError:
            orig_rebase(self, item)
            return
        if new is not sf and new != sf:
            item["source_file"] = new

    identity.__doc__ = orig_identity.__doc__
    cls.identity, cls.in_watch_root, cls.rebase_preserved = identity, in_watch_root, rebase_preserved
    cls._verinoda_memo = True
    cls._verinoda_originals = (orig_identity, orig_in_root, orig_rebase)
    return True


def uninstall_path_identity_memo() -> None:
    """Restore the vendored methods (tests compare builds with and without the memo)."""
    try:
        from verinoda.project_index import watch
    except Exception:  # noqa: BLE001
        return
    cls = getattr(watch, "_StoredSourcePaths", None)
    if cls is not None and getattr(cls, "_verinoda_memo", False):
        cls.identity, cls.in_watch_root, cls.rebase_preserved = cls._verinoda_originals
        cls._verinoda_memo = False


# -- graph --------------------------------------------------------------------------------------

@dataclass
class Graph:
    """Directed multigraph with true edge direction restored from _src/_tgt."""

    G: nx.MultiDiGraph
    path: Path
    root: Path
    _spans: dict[str, tuple[int, int]] = field(default_factory=dict)
    _span_how: dict[str, str] = field(default_factory=dict, repr=False)
    # Lazily built lookups; rebuilt when the node count changes.
    _by_file: dict[str, list[str]] = field(default_factory=dict, repr=False)
    _by_file_n: int = field(default=-1, repr=False)
    _docs_by_file: dict[str, list[tuple[int, str]]] = field(default_factory=dict, repr=False)
    _owners: dict[str, list[str | None]] = field(default_factory=dict, repr=False)
    _und: nx.Graph | None = field(default=None, repr=False)
    _und_key: tuple[int, int] = field(default=(-1, -1), repr=False)

    # -- basic lookups ------------------------------------------------------
    def node(self, nid: str) -> dict:
        return dict(self.G.nodes[nid], id=nid)

    def label(self, nid: str) -> str:
        return self.G.nodes[nid].get("label", nid)

    def line(self, nid: str) -> int | None:
        loc = self.G.nodes[nid].get("source_location") or ""
        return int(loc[1:]) if loc.startswith("L") and loc[1:].isdigit() else None

    def file(self, nid: str) -> str | None:
        return self.G.nodes[nid].get("source_file") or None

    def is_symbol(self, nid: str) -> bool:
        d = self.G.nodes[nid]
        return d.get("file_type") == "code" and bool(d.get("source_file")) and not self.is_file_node(nid)

    def is_file_node(self, nid: str) -> bool:
        """The module/file node of a source file.

        Its label is normally the basename; when basenames collide the
        extractor labels it with (a suffix of) the path instead, e.g.
        ``extractors/__init__.py`` or the full ``pkg/extractors/__init__.py``.
        """
        d = self.G.nodes[nid]
        sf = (d.get("source_file") or "").replace("\\", "/")
        if not sf:
            return False
        label = str(d.get("label") or "").replace("\\", "/")
        name = PurePosixPath(sf).name
        return label == name or label == sf or (label.endswith("/" + name) and sf.endswith(label))

    def is_heading(self, nid: str) -> bool:
        """A section heading of a prose file (a document node that is not the page itself)."""
        d = self.G.nodes[nid]
        f = d.get("source_file") or ""
        return (d.get("file_type") == "document" and f.lower().endswith(PROSE_SUFFIXES)
                and not self.is_file_node(nid) and self.line(nid) is not None)

    def edges(self, relations: set[str] | None = None):
        for u, v, d in self.G.edges(data=True):
            if relations is None or d.get("relation") in relations:
                yield u, v, d

    def out_edges(self, nid: str, relations: set[str] | None = None):
        for _, v, d in self.G.out_edges(nid, data=True):
            if relations is None or d.get("relation") in relations:
                yield v, d

    def in_edges(self, nid: str, relations: set[str] | None = None):
        for u, _, d in self.G.in_edges(nid, data=True):
            if relations is None or d.get("relation") in relations:
                yield u, d

    def _index_files(self) -> None:
        if self._by_file_n == len(self.G):
            return
        by: dict[str, list[str]] = {}
        docs: dict[str, list[tuple[int, str]]] = {}
        for n, d in self.G.nodes(data=True):
            f = d.get("source_file")
            if not f:
                continue
            if self.is_symbol(n):
                by.setdefault(f, []).append(n)
            elif self.is_heading(n):
                docs.setdefault(f, []).append((self.line(n) or 0, n))
        for f in by:
            by[f].sort(key=lambda n: (self.line(n) or 0, n))
        for f in docs:
            docs[f].sort()
        self._by_file, self._docs_by_file, self._by_file_n = by, docs, len(self.G)
        self._owners.clear()

    def symbols_in(self, rel_file: str) -> list[str]:
        """Symbols defined in ``rel_file``, by start line (one index built per graph)."""
        self._index_files()
        return list(self._by_file.get(rel_file, ()))

    def headings_in(self, rel_file: str) -> list[str]:
        """Section headings of a prose file, by line."""
        self._index_files()
        return [n for _, n in self._docs_by_file.get(rel_file, ())]

    def symbol_at(self, rel_file: str, line: int) -> str | None:
        """Innermost symbol whose span contains ``line`` (None at module level)."""
        owners = self._owners_of(rel_file)
        return owners[line] if 0 < line < len(owners) else None

    def own_line_count(self, nid: str) -> int:
        """Lines of ``nid``'s span not inside a nested symbol (its own text)."""
        f, sp = self.file(nid), self.span(nid)
        if not f or not sp:
            return 0
        owners = self._owners_of(f)
        return sum(1 for i in range(sp[0], min(sp[1], len(owners) - 1) + 1) if owners[i] == nid)

    def _owners_of(self, rel_file: str) -> list[str | None]:
        """Per-line innermost owner of ``rel_file`` (the per-file interval index)."""
        syms = self.symbols_in(rel_file)  # also resets the owner cache on graph change
        if rel_file in self._owners:
            return self._owners[rel_file]
        spans = [(sp, n) for n in syms if (sp := self.span(n))]
        top = max((sp[1] for sp, _ in spans), default=0)
        owners: list[str | None] = [None] * (top + 1)
        # Paint outer spans first; a later start is nested deeper (innermost wins,
        # the same rule as "latest start among the spans containing the line").
        for (a, b), n in sorted(spans, key=lambda x: (x[0][0], -x[0][1])):
            for i in range(a, b + 1):
                owners[i] = n
        self._owners[rel_file] = owners
        return owners

    def undirected(self) -> nx.Graph:
        """Undirected simple view for Graphify's scorers (cached per graph size)."""
        key = (len(self.G), self.G.number_of_edges())
        if self._und is None or self._und_key != key:
            self._und, self._und_key = nx.Graph(self.G), key
        return self._und

    # -- symbol spans (start/end lines) -------------------------------------
    def span(self, nid: str) -> tuple[int, int] | None:
        """(start, end) lines of a symbol's definition or of a prose section.

        See :meth:`span_basis` for how the end line was found.
        """
        if nid in self._spans:
            return self._spans[nid]
        start, f = self.line(nid), self.file(nid)
        if not start or not f:
            return None
        p = self.root / f
        suffix = p.suffix.lower()
        end, how = None, None
        if f.lower().endswith(PROSE_SUFFIXES) and self.G.nodes[nid].get("file_type") == "document":
            n_lines = _line_count(p)
            if self.is_file_node(nid):
                end, how = (n_lines or start), "file"
            elif suffix in MARKDOWN_SUFFIXES:
                end = _md_section_ends(p).get(start)
                how = "section" if end is not None else None
            if end is None:  # other prose: up to the next heading node
                later = [ln for ln, _ in self._docs_by_file_sorted(f) if ln > start]
                end, how = ((later[0] - 1) if later else (n_lines or start)), "section"
        else:
            if suffix == ".py":
                end = _py_end_line(p, start)
                how = "ast" if end is not None else None
            elif suffix in _TS_LANGS:
                end = _ts_def_ends(p).get(start)
                how = "tree-sitter" if end is not None else None
            if end is None:
                nxt = [self.line(s) for s in self.symbols_in(f)]
                later = sorted(x for x in nxt if x and x > start)
                total = _line_count(p) or start
                end = (later[0] - 1) if later else total
                end = max(start, min(end, start + HEURISTIC_SPAN_CAP))
                how = "heuristic"
        self._spans[nid] = (start, max(start, end))
        self._span_how[nid] = how or "heuristic"
        return self._spans[nid]

    def span_basis(self, nid: str) -> str | None:
        """How :meth:`span` found the end line: ``ast``, ``tree-sitter``, ``section``,
        ``file`` or ``heuristic`` (next symbol, capped); None without a span."""
        if self.span(nid) is None:
            return None
        return self._span_how.get(nid, "heuristic")

    def _docs_by_file_sorted(self, rel_file: str) -> list[tuple[int, str]]:
        self._index_files()
        return self._docs_by_file.get(rel_file, [])

    def source(self, nid: str, max_lines: int = 60) -> tuple[int, int, str] | None:
        sp = self.span(nid)
        f = self.file(nid)
        if not sp or not f:
            return None
        lines = _file_lines(self.root / f)
        if lines is None:
            return None
        s, e = sp
        e = min(e, s + max_lines - 1, len(lines))
        return s, e, "\n".join(lines[s - 1 : e])

    # -- resolution ---------------------------------------------------------
    def resolve(self, query: str) -> tuple[str | None, list[tuple[float, str]]]:
        """Resolve a label / id / file::symbol to a node id (Graphify scoring)."""
        if query in self.G:
            return query, [(1.0, query)]
        if "::" in query:
            f, sym = query.split("::", 1)
            for n in self.symbols_in(f):
                if self.label(n).strip(".()") == sym.strip(".()"):
                    return n, [(1.0, n)]
        for n, d in self.G.nodes(data=True):
            if d.get("source_file") == query and self.is_file_node(n):
                return n, [(1.0, n)]
        if "." in query and " " not in query and not Path(query).suffix[1:] in ("py", "js", "ts", "go", "rs"):
            cls, _, meth = query.rpartition(".")
            for n, d in self.G.nodes(data=True):
                if d.get("_callable_class") and d.get("label") == cls.rpartition(".")[2]:
                    for v, _ in self.out_edges(n, {"method"}):
                        if self.label(v).strip(".()") == meth:
                            return v, [(1.0, v)]
        from verinoda.project_index.serve import _pick_scored_endpoint, _score_nodes, _search_tokens

        und = self.undirected()
        scored = _score_nodes(und, [t.lower() for t in query.replace("::", " ").split()])
        if not scored:
            return None, []
        pick = _pick_scored_endpoint(und, scored, query)
        # The scorer is fuzzy (IDF over sub-tokens): 'definitely_not_a_symbol'
        # can land on some file node. Accept a scored pick only when a
        # meaningful query token really occurs in its label, id or file;
        # otherwise report it as unresolved and let callers show candidates.
        tokens = _search_tokens(query)
        meaningful = [t for t in tokens if len(t) >= 3] or tokens
        d = self.G.nodes[pick]
        hay = " ".join([str(d.get("label") or ""), pick, str(d.get("source_file") or "")]).lower()
        if meaningful and not any(t in hay for t in meaningful):
            return None, scored[:5]
        return pick, scored[:5]


# -- per-file facts (one parse per file version) ------------------------------------------------

_LINES_CACHE: dict[tuple[str, int, int], list[str]] = {}
_PYINFO_CACHE: dict[tuple[str, int, int], "PyInfo"] = {}
_TS_CACHE: dict[tuple[str, int, int], dict[int, int]] = {}
_MD_CACHE: dict[tuple[str, int, int], dict[int, int]] = {}
_AST_CACHE: dict[tuple[str, int, int], ast.AST] = {}


def _stat_key(p: Path) -> tuple[str, int, int]:
    st = p.stat()
    return str(p), st.st_mtime_ns, st.st_size


def _cached(cache: dict, key, make):
    if key not in cache:
        if len(cache) > _CACHE_MAX:
            cache.clear()
        cache[key] = make()
    return cache[key]


@dataclass
class PyInfo:
    """What one ``ast.parse`` of a Python file yields for Verinoda.

    ``ends``: def/class start line (and first decorator line) -> end line; the
    first definition met in ``ast.walk`` order wins a shared line.
    ``docs``: the same start lines -> docstring. ``receivers``: per function
    start line, the facts :func:`receiver_call_edges` resolves against the
    graph (annotated parameters, ``x = Cls(...)`` assignments and
    ``x.method()`` calls, in the order the original per-function pass saw them).
    """

    ends: dict[int, int]
    docs: dict[int, str]
    receivers: dict[int, dict]


def py_file_info(p: Path) -> PyInfo:
    """Parse ``p`` once per version (size, mtime) and keep only small derived facts."""
    key = _stat_key(p)
    return _cached(_PYINFO_CACHE, key, lambda: _py_info(p.read_bytes()))


def _py_info(source: bytes) -> PyInfo:
    tree = ast.parse(source.decode("utf-8", errors="replace"))
    ends: dict[int, int] = {}
    docs: dict[int, str] = {}
    funcs: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            end = getattr(node, "end_lineno", None)
            if end is not None:
                first = min([node.lineno] + [d.lineno for d in node.decorator_list])
                ends.setdefault(node.lineno, end)
                ends.setdefault(first, end)
            doc = ast.get_docstring(node)
            if doc:
                docs.setdefault(node.lineno, doc)
                for d in node.decorator_list:
                    docs.setdefault(d.lineno, doc)
            if not isinstance(node, ast.ClassDef):
                funcs.setdefault(node.lineno, node)
    return PyInfo(ends, docs, {ln: _receiver_facts(fn) for ln, fn in funcs.items()})


def _receiver_facts(fn) -> dict:
    ann: list[tuple[str, str]] = []
    for a in fn.args.args + fn.args.kwonlyargs:
        an = a.annotation
        name = an.id if isinstance(an, ast.Name) else (
            an.value if isinstance(an, ast.Constant) and isinstance(an.value, str) else None)
        if name:
            ann.append((a.arg, name))
    assigns: list[tuple[str, str]] = []
    for st in ast.walk(fn):
        if isinstance(st, ast.Assign) and isinstance(st.value, ast.Call) and isinstance(st.value.func, ast.Name):
            for t in st.targets:
                if isinstance(t, ast.Name):
                    assigns.append((t.id, st.value.func.id))
    receivers = {v for v, _ in ann} | {v for v, _ in assigns}
    calls: list[tuple[int, str, str]] = []
    if receivers:
        for call in ast.walk(fn):
            if (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                    and isinstance(call.func.value, ast.Name) and call.func.value.id in receivers):
                calls.append((call.lineno, call.func.value.id, call.func.attr))
    return {"ann": ann, "assign": assigns, "calls": calls}


def _py_end_line(p: Path, start: int) -> int | None:
    try:
        return py_file_info(p).ends.get(start)
    except (SyntaxError, ValueError, OSError, RecursionError):
        return None


def _py_def_ends(p: Path) -> dict[int, int]:
    """def/class start line (and first decorator line) -> end line, for one file."""
    return py_file_info(p).ends


def _file_lines(p: Path) -> list[str] | None:
    """Lines of a source file, cached per file version (size and mtime)."""
    try:
        return _cached(_LINES_CACHE, _stat_key(p),
                       lambda: p.read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError:
        return None


def file_lines(p: Path) -> list[str] | None:
    """Public alias of the cached line reader (``None`` when unreadable)."""
    return _file_lines(p)


def _line_count(p: Path) -> int | None:
    lines = _file_lines(p)
    return None if lines is None else len(lines)


def _py_ast(p: Path) -> ast.AST:
    """Parsed module, cached per file version (kept for callers outside the index)."""
    return _cached(_AST_CACHE, _stat_key(p),
                   lambda: ast.parse(p.read_text(encoding="utf-8", errors="replace")))


# Markdown sections -----------------------------------------------------------------------------

_ATX = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]|$)")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_SETEXT = re.compile(r"^ {0,3}(=+|-+)[ \t]*$")


def md_headings(lines: list[str]) -> list[tuple[int, int]]:
    """(line, level) of every ATX or setext heading outside fenced code and front matter."""
    heads: list[tuple[int, int]] = []
    fence = None
    start = 1
    if lines and lines[0].strip() == "---":  # YAML front matter
        for i in range(1, len(lines)):
            if lines[i].strip() in ("---", "..."):
                start = i + 2
                break
    for i in range(start, len(lines) + 1):
        line = lines[i - 1]
        m = _FENCE.match(line)
        if m:
            if fence is None:
                fence = m.group(1)[0]
            elif m.group(1)[0] == fence:
                fence = None
            continue
        if fence is not None:
            continue
        m = _ATX.match(line)
        if m:
            heads.append((i, len(m.group(1))))
            continue
        m = _SETEXT.match(line)
        if m and i > start and lines[i - 2].strip() and not _ATX.match(lines[i - 2]) \
                and not (heads and heads[-1][0] == i - 1):
            heads.append((i - 1, 1 if m.group(1)[0] == "=" else 2))
    return heads


def md_section_ends(lines: list[str]) -> dict[int, int]:
    """Heading line -> last line of its section.

    A section ends before the next heading of the same or a higher level (so a
    ``##`` release entry keeps its ``###`` sub-sections), trailing blank lines
    excluded. The former rule - up to 80 lines past the heading - let one
    CHANGELOG entry swallow the next ones.
    """
    heads = md_headings(lines)
    ends: dict[int, int] = {}
    for k, (ln, lvl) in enumerate(heads):
        end = len(lines)
        for ln2, lvl2 in heads[k + 1:]:
            if lvl2 <= lvl:
                end = ln2 - 1
                break
        while end > ln and not lines[end - 1].strip():
            end -= 1
        ends[ln] = max(ln, end)
    return ends


def _md_section_ends(p: Path) -> dict[int, int]:
    try:
        return _cached(_MD_CACHE, _stat_key(p), lambda: md_section_ends(_file_lines(p) or []))
    except OSError:
        return {}


# Tree-sitter spans (non-Python code) ------------------------------------------------------------

_TS_LANGS: dict[str, tuple[str, str]] = {
    ".js": ("tree_sitter_javascript", "language"), ".jsx": ("tree_sitter_javascript", "language"),
    ".mjs": ("tree_sitter_javascript", "language"), ".cjs": ("tree_sitter_javascript", "language"),
    ".ts": ("tree_sitter_typescript", "language_typescript"), ".mts": ("tree_sitter_typescript", "language_typescript"),
    ".cts": ("tree_sitter_typescript", "language_typescript"), ".tsx": ("tree_sitter_typescript", "language_tsx"),
    ".go": ("tree_sitter_go", "language"), ".rs": ("tree_sitter_rust", "language"),
    ".java": ("tree_sitter_java", "language"), ".groovy": ("tree_sitter_groovy", "language"),
    ".c": ("tree_sitter_c", "language"), ".h": ("tree_sitter_c", "language"),
    ".cpp": ("tree_sitter_cpp", "language"), ".cc": ("tree_sitter_cpp", "language"),
    ".cxx": ("tree_sitter_cpp", "language"), ".hpp": ("tree_sitter_cpp", "language"),
    ".hh": ("tree_sitter_cpp", "language"), ".rb": ("tree_sitter_ruby", "language"),
    ".cs": ("tree_sitter_c_sharp", "language"), ".kt": ("tree_sitter_kotlin", "language"),
    ".kts": ("tree_sitter_kotlin", "language"), ".scala": ("tree_sitter_scala", "language"),
    ".php": ("tree_sitter_php", "language_php"), ".lua": ("tree_sitter_lua", "language"),
    ".swift": ("tree_sitter_swift", "language"),
}
# Node types that define something (the outermost one starting on a line gives its end line).
_TS_DEF_SUFFIXES = ("_definition", "_declaration", "_item", "_declarator", "_spec")
_TS_DEF_TYPES = frozenset({
    "method", "function", "class", "module", "singleton_method", "singleton_class", "arrow_function",
    "function_expression", "generator_function", "func_literal", "decorated_definition",
    "companion_object", "object_definition", "closure_expression", "lambda_expression",
})
_TS_PARSERS: dict[str, object] = {}


def _ts_parser(suffix: str):
    spec = _TS_LANGS.get(suffix)
    if spec is None:
        return None
    if suffix not in _TS_PARSERS:
        try:
            import importlib

            from tree_sitter import Language, Parser

            mod = importlib.import_module(spec[0])
            _TS_PARSERS[suffix] = Parser(Language(getattr(mod, spec[1])()))
        except Exception:  # noqa: BLE001 - grammar missing or incompatible: no tree-sitter spans
            _TS_PARSERS[suffix] = None
    return _TS_PARSERS[suffix]


def ts_def_ends(source: bytes, suffix: str) -> dict[int, int]:
    """Start line -> end line of the outermost definition-like node starting there."""
    parser = _ts_parser(suffix)
    if parser is None:
        return {}
    tree = parser.parse(source)
    ends: dict[int, int] = {}
    cursor = tree.walk()
    while True:
        node = cursor.node
        t = node.type
        if node.is_named and (t in _TS_DEF_TYPES or t.endswith(_TS_DEF_SUFFIXES)):
            a, b = node.start_point[0] + 1, node.end_point[0] + 1
            if b > ends.get(a, 0):
                ends[a] = b
        if cursor.goto_first_child():
            continue
        while not cursor.goto_next_sibling():
            if not cursor.goto_parent():
                return ends


def _ts_def_ends(p: Path) -> dict[int, int]:
    try:
        return _cached(_TS_CACHE, _stat_key(p), lambda: ts_def_ends(p.read_bytes(), p.suffix.lower()))
    except (OSError, ValueError):
        return {}


# -- load ---------------------------------------------------------------------------------------

def load(repo: Path, gp: Path | None = None, *, augment: bool = True) -> Graph:
    repo = Path(repo).resolve()
    gp = Path(gp) if gp else graph_path(repo)
    if not gp.exists():
        raise FileNotFoundError(f"no graph at {gp}; run `verinoda scan {repo}` first")
    data = json.loads(gp.read_text(encoding="utf-8"))
    G = nx.MultiDiGraph()
    for n in data.get("nodes", []):
        G.add_node(n["id"], **{k: v for k, v in n.items() if k != "id"})
    for e in data.get("links", data.get("edges", [])):
        u, v = e.get("_src", e["source"]), e.get("_tgt", e["target"])
        if u in G and v in G:
            G.add_edge(u, v, **{k: val for k, val in e.items() if k not in ("source", "target")})
    g = Graph(G=G, path=gp, root=repo)
    if augment:
        apply_receiver_calls(g)
    return g


# -- graph identity (shared with the search index) --------------------------------------------------

def sha256_path(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def graph_identity(gp: Path, *, with_sha: bool = True) -> dict:
    """``{"size", "mtime_ns"[, "sha256"]}`` of a graph file."""
    st = Path(gp).stat()
    out = {"size": st.st_size, "mtime_ns": st.st_mtime_ns}
    if with_sha:
        out["sha256"] = sha256_path(Path(gp))
    return out


def same_graph(gp: Path, recorded: dict | None) -> bool:
    """Is ``gp`` the graph ``recorded`` describes? Stat first, content hash when the stat differs."""
    if not recorded:
        return False
    try:
        st = Path(gp).stat()
    except OSError:
        return False
    if recorded.get("size") != st.st_size:
        return False
    if recorded.get("mtime_ns") == st.st_mtime_ns:
        return True
    return bool(recorded.get("sha256")) and recorded["sha256"] == sha256_path(Path(gp))


def write_json_atomic(p: Path, obj) -> None:
    """Write JSON (LF, utf-8) through a temporary file and an atomic replace."""
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=p.name + ".", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# -- receiver-call edges ----------------------------------------------------------------------------

def _receiver_candidates(g: Graph) -> list[tuple[str, str]]:
    """(node id, file) of the Python callables the receiver pass looks into, in graph order."""
    out = []
    for n, d in g.G.nodes(data=True):
        f = d.get("source_file") or ""
        if f.endswith(".py") and d.get("_callable") and not d.get("_callable_class"):
            out.append((n, f))
    return out


def receiver_call_edges(g: Graph, facts_for=None) -> list[tuple[str, str, dict]]:
    """``calls`` edges for ``param.method()`` where ``param: SomeClass`` (not applied).

    The Graphify-derived extractor records ``place_order -uses-> OrderRepository``
    from the annotation but not ``repo.save(...)``. This pass resolves such
    calls when the receiver is a parameter annotated with (or a local assigned
    from) a class that has that method in the graph. Edges are marked
    ``confidence=INFERRED`` and ``_origin=verinoda.receiver`` so they can never
    be mistaken for extractor facts. ``facts_for(file) -> {start line: facts}``
    supplies per-file facts (default: parse the file now).
    """
    classes: dict[str, str] = {}
    methods: dict[tuple[str, str], str] = {}
    for n, d in g.G.nodes(data=True):
        if d.get("_callable_class") and d.get("file_type") == "code":
            classes.setdefault(d.get("label", ""), n)
    for _cls_label, cid in classes.items():
        for v, _ in g.out_edges(cid, {"method"}):
            methods[(cid, g.label(v).strip(".()"))] = v
    if facts_for is None:
        def facts_for(f: str) -> dict | None:
            try:
                return py_file_info(g.root / f).receivers
            except (SyntaxError, ValueError, OSError, RecursionError):
                return None
    have = {(u, v) for u, v, d in g.G.edges(data=True) if d.get("relation") == "calls"}
    out: list[tuple[str, str, dict]] = []
    per_file: dict[str, dict | None] = {}
    for n, f in _receiver_candidates(g):
        if f not in per_file:
            per_file[f] = facts_for(f)
        facts = per_file[f]
        fx = facts.get(g.line(n)) if facts else None
        if fx is None:
            fx = facts.get(str(g.line(n))) if facts else None  # facts read back from JSON
        if not fx:
            continue
        typed: dict[str, str] = {}
        for arg, name in fx["ann"]:
            if name in classes:
                typed[arg] = classes[name]
        for var, cname in fx["assign"]:
            if cname in classes:
                typed[var] = classes[cname]
        for line, var, attr in fx["calls"]:
            cid = typed.get(var)
            target = methods.get((cid, attr)) if cid else None
            if target and (n, target) not in have:
                have.add((n, target))
                out.append((n, target, {"relation": "calls", "confidence": "INFERRED", "confidence_score": 0.7,
                                        "_origin": RECEIVER_ORIGIN, "source_file": f, "source_location": f"L{line}",
                                        "context": f"{var}.{attr}() on {g.label(cid)}"}))
    return out


# -- Java calls the extractor left out ---------------------------------------------------------------

_JAVA_STRING = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'')
_JAVA_CALL = re.compile(r"(?<![\w.$])([A-Za-z_$][\w$]*)\s*\.\s*([a-z_$][\w$]*)\s*\(")
_JAVA_DECL = re.compile(r"(?<![\w.$])([A-Z][\w$]*)(?:<[^<>;()]*(?:<[^<>;()]*>[^<>;()]*)*>)?(?:\[\])?\s+([a-z_$][\w$]*)\s*(?=[=;,):])")
_JAVA_IMPORT = re.compile(r"\s*import\s+(static\s+)?([\w.]+)\.([\w$]+|\*)\s*;")
_JAVA_PACKAGE_DECL = re.compile(r"\s*package\s+([\w.]+)\s*;")


def _java_code_lines(text: str) -> list[str]:
    """The file's lines with string literals and comments blanked (same line numbers)."""
    out, in_block = [], False
    for ln in text.splitlines():
        s = _JAVA_STRING.sub('""', ln)
        if in_block:
            end = s.find("*/")
            if end < 0:
                out.append("")
                continue
            s, in_block = s[end + 2:], False
        while "/*" in s:
            a = s.find("/*")
            b = s.find("*/", a + 2)
            if b < 0:
                s, in_block = s[:a], True
                break
            s = s[:a] + " " + s[b + 2:]
        out.append(s.split("//", 1)[0])
    return out


def java_call_edges(g: Graph, read=None) -> list[tuple[str, str, dict]]:
    """``calls`` edges for Java ``Cls.method(...)`` and ``var.method(...)`` the extractor did not record.

    Graphify's Java pass drops a call when the class name is ambiguous in the repository (a
    reference copy of the code, two ``Wisp`` classes) and never follows typed variables. Here a
    class is resolved the way javac does it for the file: an explicit import of that class, the
    file's own package, or a ``pkg.*`` import; with none of these, or with an import from another
    package, no edge is made. ``var.method()`` follows the declared type of a parameter, local or
    field (``Wisp w = ...``). Edges are ``INFERRED`` with ``_origin=verinoda.java_calls``.
    """
    classes: dict[str, list[tuple[str, str]]] = {}
    methods: dict[tuple[str, str], str] = {}
    for n, d in g.G.nodes(data=True):
        f = d.get("source_file") or ""
        if f.endswith(".java") and d.get("_callable_class"):
            classes.setdefault(d.get("label", ""), []).append((n, f))
    if not classes:
        return []
    by_file: dict[str, list[str]] = {}
    for n, d in g.G.nodes(data=True):
        f = d.get("source_file") or ""
        if f.endswith(".java") and d.get("_callable") and not d.get("_callable_class"):
            by_file.setdefault(f, []).append(n)
    # a method belongs to the innermost class whose span holds it (the extractor's `method` edge
    # can be lost when the method also calls its own class's constructor)
    class_spans: dict[str, list[tuple[int, int, str]]] = {}
    for lst in classes.values():
        for cid, cf in lst:
            sp = g.span(cid)
            if sp:
                class_spans.setdefault(cf, []).append((sp[0], sp[1], cid))
    for f, ms in by_file.items():
        for m in ms:
            line = g.line(m)
            owners = [(b - a, cid) for a, b, cid in class_spans.get(f, []) if line and a <= line <= b]
            if owners:
                methods.setdefault((min(owners)[1], g.label(m).strip(".()")), m)
    if read is None:
        def read(f: str) -> str | None:
            try:
                return (g.root / f).read_text(encoding="utf-8", errors="replace")
            except OSError:
                return None
    have = {(u, v) for u, v, d in g.G.edges(data=True) if d.get("relation") == "calls"}
    out: list[tuple[str, str, dict]] = []
    for f in sorted(by_file):
        text = read(f)
        if text is None:
            continue
        raw = text.splitlines()
        code = _java_code_lines(text)
        imports = [m.groups() for ln in raw if (m := _JAVA_IMPORT.match(ln))]
        pkg = next((m.group(1) for ln in raw if (m := _JAVA_PACKAGE_DECL.match(ln))), None)
        here = str(PurePosixPath(f).parent)

        def resolve(cls: str) -> str | None:
            cands = classes.get(cls) or []
            if not cands:
                return None

            def pkg_of(cf: str, name: str) -> bool:
                return str(PurePosixPath(cf).parent).replace("\\", "/").endswith("/" + name.replace(".", "/")) \
                    or str(PurePosixPath(cf).parent) == name.replace(".", "/")

            explicit = [p for st, p, c in imports if c == cls and not st]
            if explicit:
                hit = [cid for cid, cf in cands if any(pkg_of(cf, p) for p in explicit)]
            else:
                hit = [cid for cid, cf in cands if str(PurePosixPath(cf).parent) == here
                       or (pkg is not None and pkg_of(cf, pkg))]
                if not hit:
                    wild = [p for st, p, c in imports if c == "*" and not st]
                    hit = [cid for cid, cf in cands if any(pkg_of(cf, p) for p in wild)]
            return hit[0] if len(hit) == 1 else None

        fields = {m.group(2): m.group(1) for ln in code for m in _JAVA_DECL.finditer(ln)
                  if re.match(r"\s*(?:(?:private|protected|public|static|final|volatile|transient)\s+)+", ln)}
        for n in by_file[f]:
            sp = g.span(n)
            if not sp:
                continue
            a, b = sp[0], min(sp[1], len(code))
            local = dict(fields)
            for i in range(a, b + 1):
                for m in _JAVA_DECL.finditer(code[i - 1]):
                    local[m.group(2)] = m.group(1)
            for i in range(a, b + 1):
                for m in _JAVA_CALL.finditer(code[i - 1]):
                    recv, meth = m.group(1), m.group(2)
                    if recv[0].isupper():
                        cls, how = recv, "class"
                    elif recv in local and recv not in ("this", "super"):
                        cls, how = local[recv], "typed"
                    else:
                        continue
                    cid = resolve(cls)
                    target = methods.get((cid, meth)) if cid else None
                    if not target or target == n or (n, target) in have:
                        continue
                    have.add((n, target))
                    ctx = f"{recv}.{meth}()" + ("" if how == "class" else f" on {cls}")
                    out.append((n, target, {"relation": "calls", "confidence": "INFERRED",
                                            "confidence_score": 0.9 if how == "class" else 0.75,
                                            "_origin": JAVA_CALL_ORIGIN, "source_file": f,
                                            "source_location": f"L{i}", "context": ctx}))
    return out


def _apply_edges(g: Graph, edges) -> int:
    g.__dict__.pop("_ppr_adj", None)  # search_index caches weighted neighbours per graph
    added = 0
    for u, v, d in edges:
        if u not in g.G or v not in g.G:
            continue
        if any(dd.get("relation") == "calls" for dd in (g.G.get_edge_data(u, v) or {}).values()):
            continue
        g.G.add_edge(u, v, **d)
        added += 1
    return added


def augment_python_receiver_calls(g: Graph) -> int:
    """Compute and add the receiver-call (and Java call) edges in memory; returns the number added."""
    return _apply_edges(g, receiver_call_edges(g) + java_call_edges(g))


def _read_sidecar(repo: Path) -> dict | None:
    try:
        data = json.loads(receiver_calls_path(repo).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("version") == RECEIVER_SIDECAR_VERSION else None


def refresh_receiver_sidecar(repo: Path, g: Graph | None = None) -> dict:
    """Recompute ``receiver_calls.json`` for the current graph (per-file facts reused by sha256)."""
    repo = Path(repo).resolve()
    g = g or load(repo, augment=False)
    old = _read_sidecar(repo) or {}
    old_files = old.get("files") or {}
    files: dict[str, dict] = {}
    parsed = 0

    def facts_for(f: str) -> dict | None:
        nonlocal parsed
        p = repo / f
        try:
            data = p.read_bytes()
        except OSError:
            return None
        sha = hashlib.sha256(data).hexdigest()
        prev = old_files.get(f)
        if prev and prev.get("sha256") == sha:
            files[f] = prev
            return prev.get("facts")
        try:
            facts = _py_info(data).receivers
        except (SyntaxError, ValueError, RecursionError):
            facts = None
        parsed += 1
        files[f] = {"sha256": sha, "facts": None if facts is None else {str(k): v for k, v in facts.items()}}
        return files[f]["facts"]

    edges = receiver_call_edges(g, facts_for) + java_call_edges(g)
    sidecar = {"version": RECEIVER_SIDECAR_VERSION, "graph": graph_identity(g.path),
               "files": files, "edges": [[u, v, d] for u, v, d in edges]}
    write_json_atomic(receiver_calls_path(repo), sidecar)
    return {"edges": len(edges), "files_parsed": parsed, "files_reused": len(files) - parsed}


def apply_receiver_calls(g: Graph) -> int:
    """Add the stored receiver-call edges when they describe this graph file, else compute them.

    A missing or stale sidecar (older Verinoda, graph rewritten elsewhere) is
    recomputed in memory and, when the index directory is writable, stored.
    """
    side = _read_sidecar(g.root)
    if side and same_graph(g.path, side.get("graph")):
        return _apply_edges(g, [(u, v, d) for u, v, d in side.get("edges") or []])
    try:
        if g.path == graph_path(g.root):
            refresh_receiver_sidecar(g.root, g)
            side = _read_sidecar(g.root)
            if side and same_graph(g.path, side.get("graph")):
                return _apply_edges(g, [(u, v, d) for u, v, d in side.get("edges") or []])
    except OSError:
        pass
    return augment_python_receiver_calls(g)


# -- deleted files ------------------------------------------------------------------------------------

_REMOTE_SOURCE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]+:/")


def _local_source(repo: Path, sf) -> Path | None:
    """The file a node's ``source_file`` names inside ``repo`` (None for URLs/virtual/outside)."""
    if not isinstance(sf, str) or not sf or _REMOTE_SOURCE.match(sf):
        return None
    p = Path(sf)
    if not p.is_absolute():
        p = repo / p
    try:
        p.resolve().relative_to(repo)
    except (ValueError, OSError):
        return None
    return p


def missing_source_files(repo: Path, gp: Path | None = None) -> list[str]:
    """``source_file`` values of graph nodes whose file no longer exists in ``repo``."""
    repo = Path(repo).resolve()
    gp = Path(gp) if gp else graph_path(repo)
    try:
        data = json.loads(gp.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    seen: dict[str, bool] = {}
    for n in data.get("nodes", []):
        sf = n.get("source_file")
        if not isinstance(sf, str) or sf in seen:
            continue
        p = _local_source(repo, sf)
        seen[sf] = p is not None and not p.exists()
    return sorted(sf for sf, gone in seen.items() if gone)


def prune_missing_files(repo: Path, gp: Path | None = None) -> list[str]:
    """Drop nodes (and their edges) whose source file was deleted; returns the files pruned.

    The last line of defence behind a forced rebuild: the graph must never
    describe a file that no longer exists. The receiver sidecar is refreshed
    when anything was removed.
    """
    repo = Path(repo).resolve()
    gp = Path(gp) if gp else graph_path(repo)
    gone = set(missing_source_files(repo, gp))
    if not gone:
        return []
    data = json.loads(gp.read_text(encoding="utf-8"))
    drop = {n["id"] for n in data.get("nodes", []) if n.get("source_file") in gone}
    data["nodes"] = [n for n in data.get("nodes", []) if n["id"] not in drop]
    for key in ("links", "edges"):
        if key in data:
            data[key] = [e for e in data[key]
                         if e.get("source") not in drop and e.get("target") not in drop
                         and e.get("_src") not in drop and e.get("_tgt") not in drop
                         and e.get("source_file") not in gone]
    if isinstance(data.get("hyperedges"), list):
        data["hyperedges"] = [h for h in data["hyperedges"]
                              if not (isinstance(h, dict) and h.get("source_file") in gone)]
    fd, tmp = tempfile.mkstemp(prefix="graph.", suffix=".tmp", dir=str(gp.parent))
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, gp)
    try:
        refresh_receiver_sidecar(repo)
    except (OSError, ValueError):
        pass
    return sorted(gone)


def graphify_query_text(repo: Path, question: str, budget: int = 2000) -> str:
    """Graphify's own query renderer - the baseline the benchmark compares to."""
    from verinoda.project_index.serve import _load_graph, _query_graph_text

    gp = graph_path(Path(repo).resolve())
    G = _load_graph(str(gp))
    return _query_graph_text(G, question, token_budget=budget, graph_path=str(gp))

