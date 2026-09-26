"""One exact resolver for a name a user or an agent writes (trace endpoints, ``map --view impact``
targets, MCP ``node_inspect``).

A name resolves to the node it names exactly (:func:`verinoda.retrieval._names_exactly`: a node
id, a path with or without its extension, a dotted module name, ``path::Class.method``,
``Class#method``, ``Owner.name``, ``name()``). When several nodes carry that name:

1. a node in a detected copy of the project (``copies.json``) or a configured reference tree gives
   way to the project's own code, unless the text names that tree ("heldout", its path): a copy
   and its original are never offered as a choice;
2. a definition in test code or in an example, sample, fixture or vendored folder gives way to one
   of the product's own code (``call_soon`` is asyncio's method, not a test's local helper);
3. a function's local definition (a helper nested in another function) gives way to a module-level
   or class-level one: it cannot be named from outside that function;
4. within one file, a class comes before its own constructor (a member with the class's name) and
   a symbol before the file of that name;
5. what is still tied is **ambiguous**: the candidates are listed and none is picked (a top-level
   function and a method of that name in other files are a tie, not a preference).

What gave way is kept in ``set_aside`` and said in the note, so a reader sees which node was used.

A name that names nothing exactly is never replaced by a similar one when it is written as code:

* spelled in a file changed since the index: ``not_indexed`` ("not in the index yet ... run
  `verinoda update`");
* spelled nowhere in the repository: ``not_found``, with the nearest names;
* spelled somewhere the index describes but not a symbol of it (a module constant, an attribute,
  an imported name): ``not_a_symbol``, saying where it occurs, with the nearest symbols as
  candidates (it used to be resolved by similarity, which answered about another name);

plain words are ``similar`` (the scorer's node, with a note) or ``unresolved``.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Iterable

from verinoda.textnorm import fold_tr

EXACT, AMBIGUOUS, SIMILAR = "exact", "ambiguous", "similar"
NOT_FOUND, NOT_INDEXED, NOT_A_SYMBOL, UNRESOLVED = "not_found", "not_indexed", "not_a_symbol", "unresolved"


@dataclass
class Resolution:
    text: str
    status: str
    node: str | None = None
    candidates: list[str] = field(default_factory=list)   # the tied exact nodes, or the nearest names
    note: str | None = None
    set_aside: list[str] = field(default_factory=list)    # exact nodes in copies / reference trees
    site: str | None = None                                # where a code name is spelled (not_indexed)

    @property
    def exact(self) -> bool:
        return self.status == EXACT

    def rows(self, g, limit: int = 6) -> list[dict]:
        """The candidates as ``{"id", "label", "at"}`` (one in a copy, test code or a function body says so)."""
        out = []
        for n in self.candidates[:limit]:
            if n not in g.G:
                continue
            f, ln = g.file(n), g.line(n)
            row = {"id": n, "label": g.label(n), "at": f"{f}:{ln}" if f and ln else f}
            why = _aside_kind(g, self.text, n)
            if why:
                row["in"] = why
            out.append(row)
        return out

    def as_dict(self, g) -> dict:
        out: dict = {"text": self.text, "status": self.status}
        if self.node:
            out["node"] = {"id": self.node, "label": g.label(self.node),
                           "at": _loc(g, self.node)}
        if self.candidates and self.status != EXACT:
            out["candidates"] = self.rows(g)
        if self.set_aside:
            out["set_aside"] = [{"id": n, "at": _loc(g, n), "in": _aside_kind(g, self.text, n)}
                                for n in self.set_aside[:4]]
        if self.note:
            out["note"] = self.note
        return out


def _loc(g, n: str) -> str | None:
    f, ln = g.file(n), g.line(n)
    return f"{f}:{ln}" if f and ln else f


def _aside_roots(g, text: str) -> tuple[str, ...]:
    """Copies and reference trees ``text`` does not name (cached per graph and text)."""
    import re

    from verinoda import copies

    cache = g.__dict__.setdefault("_aside_roots", {})
    if text not in cache:
        words = set(re.findall(r"[a-z0-9_]+", fold_tr(text or "")))
        try:
            cache[text] = copies.roots_not_named(g.root, words, text or "")
        except Exception:  # noqa: BLE001 - no config, no copies: nothing is set aside
            cache[text] = ()
    return cache[text]


COPY, NOT_PRODUCT, LOCAL = "copy or reference tree", "test, example or fixture code", "local to a function"


def not_product(f: str | None) -> bool:
    """Test code (:func:`verinoda.guards.is_test_file`) or a file under an example, sample, demo,
    fixture or vendored folder: code that is not the product's own."""
    from verinoda import guards

    return bool(f) and (guards.is_test_file(f) or bool(set(PurePosixPath(f).parts[:-1]) & guards._NOT_PRODUCT_DIRS))


def _local(g, n: str) -> bool:
    """Is ``n`` defined inside a function's body (a nested helper)? Told by a ``contains`` edge from a
    function or by the span of an enclosing function (Python AST / tree-sitter ends; a guessed span
    never encloses the next symbol, so it never makes one local)."""
    if g.is_file_node(n):
        return False
    if any(g.label(u).endswith(")") for u, _d in g.in_edges(n, {"contains"})):
        return True
    f, ln = g.file(n), g.line(n)
    if not f or not ln:
        return False
    if f.endswith(".py"):  # the file's own parse (cached per version): its functions' start and end lines
        from verinoda.index import py_file_info

        try:
            info = py_file_info(g.root / f)
        except (SyntaxError, ValueError, OSError, RecursionError):
            return False
        return any(s < ln <= info.ends.get(s, s) for s in info.receivers)
    for s in g.symbols_in(f):  # by start line
        start = g.line(s)
        if start is None or s == n:
            continue
        if start >= ln:
            break
        if g.label(s).endswith(")"):
            sp = g.span(s)
            if sp and sp[0] < ln <= sp[1]:
                return True
    return False


def _gives_way(g, text: str, n: str, why: str) -> bool:
    """Does ``n`` give way for reason ``why`` (:data:`COPY`, :data:`NOT_PRODUCT`, :data:`LOCAL`)?"""
    f = g.file(n) or ""
    if why == COPY:
        roots = _aside_roots(g, text)
        return bool(roots) and f.startswith(roots)
    if why == NOT_PRODUCT:
        return not_product(f)
    return _local(g, n)


def _aside_kind(g, text: str, n: str) -> str | None:
    """The first reason ``n`` gives way when another node has the same name (None: it does not)."""
    return next((why for why in (COPY, NOT_PRODUCT, LOCAL) if _gives_way(g, text, n, why)), None)


def _aside_summary(g, text: str, aside: list[str]) -> str:
    """``2 in test, example or fixture code (a.py:3, b.py:9)``, per reason."""
    by: dict[str, list[str]] = defaultdict(list)
    for n in aside:
        by[_aside_kind(g, text, n) or "other"].append(_loc(g, n) or n)
    parts = []
    for why, locs in by.items():
        where = ", ".join(locs[:2]) + (", ..." if len(locs) > 2 else "")
        parts.append(f"{len(locs)} {why if why == LOCAL else 'in ' + why} ({where})")
    return "; ".join(parts)


class _Names:
    """Exact-name lookups over a graph (see :func:`_names`): the nodes with a file by their folded bare
    label (``.run()`` -> ``run``), the file nodes, and the file nodes by their basename stem. One pass
    over the nodes, no stems or identifier parts: much cheaper than question_plan's linking index,
    which an exact lookup does not need."""

    def __init__(self, g):
        self.by_bare: dict[str, list[str]] = defaultdict(list)
        self.files: dict[str, str] = {}
        self.by_stem: dict[str, list[str]] = defaultdict(list)
        for n, d in g.G.nodes(data=True):
            f = d.get("source_file")
            if not f:
                continue
            label = str(d.get("label") or n)
            base = f.replace("\\", "/").rpartition("/")[2]
            # a file node's label is its basename or a path suffix (g.is_file_node, asked only then)
            if label.replace("\\", "/").endswith(base) and g.is_file_node(n):
                self.files[f] = n
                self.by_stem[base.rpartition(".")[0] if "." in base else base].append(n)
            elif (d.get("metadata") or {}).get("language") == "mcfunction":  # one node per file, its function id
                self.files.setdefault(f, n)
            self.by_bare[fold_tr(label.strip().lstrip(".").split("(")[0].strip())].append(n)


def _names(g) -> _Names:
    """The exact-name lookups of ``g``, kept on the graph object while its node count holds."""
    memo = g.__dict__.get("_naming_names")
    if memo is None or memo[0] != len(g.G):
        memo = g.__dict__["_naming_names"] = (len(g.G), _Names(g))
    return memo[1]


def scored(g, text: str) -> tuple[str | None, list[tuple[float, str]]]:
    """``g.resolve(text)`` (the fuzzy scorer), computed once per graph and text: it costs seconds on a
    large graph, and an unknown name needs it twice (the exact fallback, then the nearest names)."""
    memo = g.__dict__.setdefault("_naming_scored", {})
    key = (len(g.G), text)
    if key not in memo:
        if len(memo) > 64:
            memo.clear()
        memo[key] = g.resolve(text)
    return memo[key]


def candidates(g, text: str) -> list[str]:
    """The nodes ``text`` names exactly (:func:`verinoda.retrieval._names_exactly`), found through the
    exact-name lookups: the symbols of a named file, the nodes whose label is the name or its last
    dotted part, and for a dotted name the files it names as a module path."""
    from verinoda import question_plan as qp
    from verinoda import retrieval as rt

    nm = _names(g)
    path, name = qp.split_code_name(text)
    cands: set[str] = set()
    if path:
        files = [f for f in nm.files if qp._names_file(f, {path})]
        cands = {nm.files[f] for f in files} if not name else {n for f in files for n in g.symbols_in(f)}
    elif name:
        for form in {name, name.rpartition(".")[2]}:
            cands.update(nm.by_bare.get(fold_tr(form), ()))
        if "." in name:
            cands.update(nm.by_stem.get(name.rpartition(".")[2], ()))
    return sorted(n for n in cands if rt._names_exactly(g, text, n) or (
        path and not name and (g.G.nodes[n].get("metadata") or {}).get("language") == "mcfunction"))


def _via_import(g, text: str) -> list[str]:
    """``module.name`` where that module imports ``name`` (``api.place_order`` when orders/api.py does
    ``from orders.service import place_order``): the node the import binds - the same object, not a
    similar name."""
    from verinoda import question_plan as qp

    path, name = qp.split_code_name(text)
    if path or "." not in name:
        return []
    owner, _, last = name.rpartition(".")
    out = set()
    for f, fnode in _names(g).files.items():
        if not qp._names_file(f, {owner.replace(".", "/")}):
            continue
        for v, _d in g.out_edges(fnode, {"imports"}):
            if g.is_symbol(v) and fold_tr(qp._bare(g.label(v))) == fold_tr(last):
                out.add(v)
    return sorted(out)


def _same_file(g, pool: list[str]) -> list[str]:
    """Within one file: a class before its own constructor (a member with the class's name) and a
    symbol before the file node of that name."""
    keep = set(pool)
    for n in pool:
        if g.is_file_node(n) and any(m != n and g.file(m) == g.file(n) for m in pool):
            keep.discard(n)
        elif any(c in keep and g.file(c) == g.file(n) for c, _d in g.in_edges(n, {"method"})):
            keep.discard(n)
    return sorted(keep) or pool


def exact_nodes(g, text: str, *, fallback: bool = True) -> tuple[list[str], list[str]]:
    """``(best, set_aside)``: the exact nodes of ``text`` that stay after copies and reference trees,
    then test / example / fixture code, then a function's local definitions give way to the others,
    and a class is kept over its own constructor or file (one node: exact; more: tied); and those
    set aside. ``fallback``: when the lookups find nothing, ask the fuzzy scorer whether its top node
    is named exactly (a node without a file, such as an external base class)."""
    from verinoda import retrieval as rt

    text = (text or "").strip()
    if text in g.G:
        return [text], []
    exact = candidates(g, text)
    if not exact and fallback:
        nid = scored(g, text)[0]
        if nid and rt._names_exactly(g, text, nid):
            exact = [nid]
    if not exact:
        exact = _via_import(g, text)
    if not exact:
        return [], []
    pool, aside = list(exact), []
    if len(pool) > 1:
        # names that differ only in case are distinct symbols (verinoda.case_ids): the one written with the
        # text's own case is meant (`orderService` the const, `OrderService` the class)
        bare = text.rsplit("::", 1)[-1].rsplit(".", 1)[-1].rstrip("()")
        cased = [n for n in pool if g.label(n).strip(".()").rsplit(".", 1)[-1] == bare]
        if cased and len(cased) < len(pool) and \
                {g.label(n).strip(".()").rsplit(".", 1)[-1].lower() for n in pool} == {bare.lower()}:
            aside += [n for n in pool if n not in cased]
            pool = cased
        for why in (COPY, NOT_PRODUCT, LOCAL):
            gone = [n for n in pool if _gives_way(g, text, n, why)]
            if gone and len(gone) < len(pool):
                pool = [n for n in pool if n not in gone]
                aside += gone
        pool = _same_file(g, pool)
    return sorted(pool), sorted(aside)


def indexed_spells(g, f: str, name: str) -> bool | None:
    """Does the version of ``f`` the index describes spell ``name``? Read from the search index's
    tokens of that file (a compound identifier is kept whole, folded: ``max_response_chars``; a
    single word by its stem), and only when the search index holds the version the latest snapshot
    recorded (same content hash). None when that cannot be told: no search index, no row for the
    file, a skipped file, or another version of it."""
    import sqlite3

    from verinoda import freshness
    from verinoda import search_index as si
    from verinoda.paths import db_path

    toks = si.tokens(name)
    db = si.db_path_for(g)
    if not toks or not db.is_file():
        return None
    try:
        _snap, rows = freshness._latest(db_path(g.root))
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=2.0)
    except (sqlite3.Error, OSError):
        return None
    try:
        row = conn.execute("SELECT sha256, pid_lo, pid_hi, skipped FROM files WHERE file = ?", (f,)).fetchone()
        if row is None or row[3] or f not in rows or row[0] != rows[f][0]:
            return None
        if row[1] is not None and conn.execute("SELECT 1 FROM body WHERE term = ? AND pid BETWEEN ? AND ? LIMIT 1",
                                               (toks[0], row[1], row[2])).fetchone():
            return True
        return conn.execute("SELECT 1 FROM names n JOIN units u ON u.uid = n.uid WHERE n.term = ? AND u.file = ? "
                            "LIMIT 1", (toks[0], f)).fetchone() is not None
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def unindexed_names(g, question: str, stale_files: Iterable[str], limit: int = 3) -> list[dict]:
    """Code names of ``question`` (``snake_case``, ``camelCase``, dotted, ``path::name``) that name no
    node of the index but are spelled in one of ``stale_files`` (changed since the index), where the
    version the index describes did not spell them: ``[{"name", "at": "file:line", "new"}]``. ``new``
    is True when the index's version of the file was read and lacks the name (whatever the index
    answers is about something else), None when that could not be told. A name the indexed file
    already spelled (a constant in a file with an unrelated edit) is not listed."""
    import re
    from pathlib import Path

    from verinoda import question_plan as qp

    files = [f for f in dict.fromkeys(stale_files or ()) if (Path(g.root) / f).is_file()]
    if not files:
        return []
    out: list[dict] = []
    seen: set[str] = set()
    texts: dict[str, list[str] | None] = {}
    for tok in re.findall(r"`[^`]+`|[\w.:/\\#()-]+", question or ""):
        t = tok.strip("`'\"").rstrip("?,;:!.").strip()
        if not t or t in seen or not qp.code_shape(t):
            continue
        seen.add(t)
        if exact_nodes(g, t, fallback=False)[0]:
            continue
        path, name = qp.split_code_name(t)
        needle = name.rpartition(".")[2] if name else ""
        if not needle:
            continue
        rx = re.compile(rf"(?<![\w]){re.escape(needle)}(?![\w])")
        for f in files:
            if path and not qp._names_file(f, {path}):
                continue
            if f not in texts:
                texts[f] = qp._lines_of(g, f) or None
            hit = next((i for i, ln in enumerate(texts[f] or [], 1) if rx.search(ln)), None)
            if not hit:
                continue
            before = indexed_spells(g, f, needle)
            if before is True:  # the index's version spelled it already: not new
                continue
            out.append({"name": t, "at": f"{f}:{hit}", "new": True if before is False else None})
            break
        if len(out) >= limit:
            break
    return out


def _stale_site(site: str | None, stale: set[str]) -> bool:
    """Is ``site`` (``file:line`` or a file) in one of the ``stale`` files?"""
    return _site_file(site) in stale if site else False


def _site_file(site: str) -> str:
    f, sep, ln = site.rpartition(":")
    return f if sep and ln.isdigit() else site


def _not_indexed(text: str, name: str, site: str, new: bool | None) -> Resolution:
    if new:
        note = (f"`{name or text}` is not in the index yet: it occurs at {site}, in a file changed since the "
                "index, whose indexed version does not spell it (run `verinoda update`); no similar name is used "
                "instead")
    else:
        note = (f"`{name or text}` may not be in the index yet: it occurs at {site}, in a file changed since the "
                "index, and whether the indexed version spells it could not be read (run `verinoda update`); "
                "no similar name is used instead")
    return Resolution(text, NOT_INDEXED, None, [], note, site=site)


def resolve(g, text: str, *, stale: Iterable[str] = ()) -> Resolution:
    """Resolve ``text`` (see the module docstring). ``stale``: files changed since the index
    (:func:`verinoda.freshness.check`); a code name spelled only there, where the indexed version did
    not spell it, is ``not_indexed`` (with no candidates: a similar name is never offered for it)."""
    from verinoda import question_plan as qp
    from verinoda import retrieval as rt

    text = (text or "").strip()
    stale = set(stale or ())
    code = rt._code_written(text)
    # the fuzzy scorer (seconds on a large graph) only for plain words: a name written as code is
    # never replaced by a similar one, and a node it names exactly is found by the lookups
    best, aside = exact_nodes(g, text, fallback=not code)
    if len(best) == 1:
        n = best[0]
        notes = []
        roots = _aside_roots(g, text)
        if roots and (g.file(n) or "").startswith(roots):  # only a copy defines it: say so
            notes.append(f"'{text}' is defined only in a copy of the project or a reference tree "
                         f"({_loc(g, n)})")
        if aside:  # the node itself is shown next to the note (trace's `resolved`, impact's `node`)
            notes.append("also defined, set aside: " + _aside_summary(g, text, aside))
        if g.file(n) in stale:
            notes.append(f"{g.file(n)} changed since the index: its lines may have moved (run `verinoda update`)")
        return Resolution(text, EXACT, n, [n], "; ".join(notes) or None, aside)
    if best:
        others = ", ".join(f"{g.label(n)} ({_loc(g, n)})" for n in best[:4])
        return Resolution(text, AMBIGUOUS, None, best,
                          f"'{text}' names {len(best)} symbols: {others}"
                          + (", ..." if len(best) > 4 else "") + "; give one as path/file.py::Name or a node id"
                          + (f" (set aside: {_aside_summary(g, text, aside)})" if aside else ""),
                          aside)
    if not code:
        nid, ranked = scored(g, text)
        if nid:
            return Resolution(text, SIMILAR, nid, [nid] + [c for _s, c in ranked if c != nid][:4],
                              f"'{text}' has no exact match; resolved by similarity to {g.label(nid)} "
                              f"({_loc(g, nid)})")
        return Resolution(text, UNRESOLVED, None, [c for _s, c in ranked][:5])
    # a name written as code is never replaced by a similar one. A changed file is read first (the
    # index does not know what it now defines; a large repository's scan below may not finish)
    path, name = qp.split_code_name(text)
    new = unindexed_names(g, f"`{text}`", stale, limit=1) if stale else []
    if new:
        return _not_indexed(text, name, new[0]["at"], new[0]["new"])
    # the same existence check as analyze (a member of a known class or module is looked for in it);
    # an owner the graph does not define needs the whole name spelled
    site = qp.name_site(g, text)
    if site not in (None, qp.UNCHECKED) and "." in name and not path and not qp.owner_known(g, text):
        site = qp.name_site(g, text, strict=True)
    ix = qp._index(g)
    near = [{"node": n, "label": g.label(n), "site": qp._site(g, n)} for n, _ in qp._member_near(g, ix, text)]
    near += [{"node": n, "label": g.label(n), "site": qp._site(g, n)} for n, _ in qp._near_misses(ix, text)
             if qp._site(g, n) not in {x["site"] for x in near}]
    if site is None:
        return Resolution(text, NOT_FOUND, None, [x["node"] for x in near], qp.not_found_line(g, text, near))
    changed = site != qp.UNCHECKED and _stale_site(site, stale)
    if changed:
        before = indexed_spells(g, _site_file(site), name.rpartition(".")[2] or text)
        if before is not True:
            return _not_indexed(text, name, site, before is False)
    if site == qp.UNCHECKED:
        where = "whether the repository spells it was not checked (too large to scan)"
    elif qp.spells_whole(g, site, text):
        where = f"the name occurs at {site}"
    else:
        where = f"`{name.rpartition('.')[2]}` occurs at {site}, not the whole name"
    if changed:
        where += ", in a file changed since the index (run `verinoda update`)"
    cands = list(dict.fromkeys(x["node"] for x in near))
    nearest = ", ".join(f"{g.label(n)} ({_loc(g, n)})" for n in cands[:2])
    return Resolution(text, NOT_A_SYMBOL, None, cands,
                      f"no symbol in the index is named `{text}` ({where}); no similar name is used instead"
                      + (f"; nearest symbols: {nearest}" if nearest else ""), site=site)
