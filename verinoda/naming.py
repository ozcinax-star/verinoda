"""One exact resolver for a name a user or an agent writes (trace endpoints, ``map --view impact``
targets, MCP ``node_inspect``).

A name resolves to the node it names exactly (:func:`verinoda.retrieval._names_exactly`: a node
id, a path with or without its extension, a dotted module name, ``path::Class.method``,
``Class#method``, ``Owner.name``, ``name()``). When several nodes carry that name:

1. a node in a detected copy of the project (``copies.json``) or a configured reference tree gives
   way to the project's own code, unless the text names that tree ("heldout", its path): a copy
   and its original are never offered as a choice;
2. a class comes before its constructor or its file, a top-level symbol before a member of that
   name (``trace``'s long-standing order);
3. what is still tied is **ambiguous**: the candidates are listed and none is picked.

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

from dataclasses import dataclass, field
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
        """The candidates as ``{"id", "label", "at"}`` (a copy's node says so)."""
        roots = _aside_roots(g, self.text)
        out = []
        for n in self.candidates[:limit]:
            if n not in g.G:
                continue
            f, ln = g.file(n), g.line(n)
            row = {"id": n, "label": g.label(n), "at": f"{f}:{ln}" if f and ln else f}
            if roots and (f or "").startswith(roots):
                row["in"] = "copy or reference tree"
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
            out["set_aside"] = [{"id": n, "at": _loc(g, n)} for n in self.set_aside[:4]]
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


def _rank(g, n: str) -> tuple:
    """A class before its constructor or its file; a top-level symbol before a member of that name."""
    return (g.is_file_node(n), any(True for _ in g.in_edges(n, {"method"})), g.label(n).endswith(")"))


def _via_import(g, text: str) -> list[str]:
    """``module.name`` where that module imports ``name`` (``api.place_order`` when orders/api.py does
    ``from orders.service import place_order``): the node the import binds - the same object, not a
    similar name."""
    from verinoda import question_plan as qp

    path, name = qp.split_code_name(text)
    if path or "." not in name:
        return []
    owner, _, last = name.rpartition(".")
    ix = qp._index(g)
    out = set()
    for f, fnode in ix.files.items():
        if not qp._names_file(f, {owner.replace(".", "/")}):
            continue
        for v, _d in g.out_edges(fnode, {"imports"}):
            if g.is_symbol(v) and fold_tr(qp._bare(g.label(v))) == fold_tr(last):
                out.add(v)
    return sorted(out)


def exact_nodes(g, text: str) -> tuple[list[str], list[str]]:
    """``(best, set_aside)``: the exact nodes of ``text`` that stay after copies and reference trees
    give way and the rank order is applied (one node: exact; more: tied), and those set aside."""
    from verinoda import retrieval as rt

    text = (text or "").strip()
    if text in g.G:
        return [text], []
    exact = rt._exact_nodes(g, text)
    if not exact:
        nid = g.resolve(text)[0]
        if nid and rt._names_exactly(g, text, nid):
            exact = [nid]
    if not exact:
        exact = _via_import(g, text)
    if not exact:
        return [], []
    roots = _aside_roots(g, text)
    own = [n for n in exact if not (roots and (g.file(n) or "").startswith(roots))]
    aside = [n for n in exact if n not in own] if own else []
    pool = own or exact
    top = min(map(lambda n: _rank(g, n), pool))
    return sorted(n for n in pool if _rank(g, n) == top), sorted(aside)


def unindexed_names(g, question: str, stale_files: Iterable[str], limit: int = 3) -> list[dict]:
    """Code names of ``question`` (``snake_case``, ``camelCase``, dotted, ``path::name``) that name no
    node of the index but are spelled in one of ``stale_files`` (changed since the index):
    ``[{"name", "at": "file:line"}]``. Such a name is new: whatever the index answers is about
    something else."""
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
        if exact_nodes(g, t)[0]:
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
            if hit:
                out.append({"name": t, "at": f"{f}:{hit}"})
                break
        if len(out) >= limit:
            break
    return out


def _stale_site(site: str | None, stale: set[str]) -> bool:
    """Is ``site`` (``file:line`` or a file) in one of the ``stale`` files?"""
    if not site:
        return False
    f, sep, ln = site.rpartition(":")
    return (f if sep and ln.isdigit() else site) in stale


def resolve(g, text: str, *, stale: Iterable[str] = ()) -> Resolution:
    """Resolve ``text`` (see the module docstring). ``stale``: files changed since the index
    (:func:`verinoda.freshness.check`); a code name spelled only there is ``not_indexed``."""
    from verinoda import question_plan as qp
    from verinoda import retrieval as rt

    text = (text or "").strip()
    stale = set(stale or ())
    best, aside = exact_nodes(g, text)
    if len(best) == 1:
        n = best[0]
        notes = []
        roots = _aside_roots(g, text)
        if roots and (g.file(n) or "").startswith(roots):  # only a copy defines it: say so
            notes.append(f"'{text}' is defined only in a copy of the project or a reference tree "
                         f"({_loc(g, n)})")
        if g.file(n) in stale:
            notes.append(f"{g.file(n)} changed since the index: its lines may have moved (run `verinoda update`)")
        return Resolution(text, EXACT, n, [n], "; ".join(notes) or None, aside)
    if best:
        others = ", ".join(f"{g.label(n)} ({_loc(g, n)})" for n in best[:4])
        return Resolution(text, AMBIGUOUS, None, best,
                          f"'{text}' names {len(best)} symbols: {others}"
                          + (", ..." if len(best) > 4 else "") + "; give one as path/file.py::Name or a node id",
                          aside)
    nid, scored = g.resolve(text)
    if not rt._code_written(text):
        if nid:
            return Resolution(text, SIMILAR, nid, [nid] + [c for _s, c in scored if c != nid][:4],
                              f"'{text}' has no exact match; resolved by similarity to {g.label(nid)} "
                              f"({_loc(g, nid)})")
        return Resolution(text, UNRESOLVED, None, [c for _s, c in scored][:5])
    # a name written as code is never replaced by a similar one. A changed file is read first (the
    # index does not know what it now defines; a large repository's scan below may not finish)
    path, name = qp.split_code_name(text)
    new = unindexed_names(g, f"`{text}`", stale, limit=1) if stale else []
    if new:
        site = new[0]["at"]
        return Resolution(text, NOT_INDEXED, None, [nid] if nid else [],
                          f"`{name or text}` is not in the index yet: it occurs at {site}, in a file changed since "
                          "the index (run `verinoda update`); no similar name is used instead", site=site)
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
    if site != qp.UNCHECKED and _stale_site(site, stale):
        return Resolution(text, NOT_INDEXED, None, [nid] if nid else [],
                          f"`{name or text}` is not in the index yet: it occurs at {site}, in a file changed since "
                          "the index (run `verinoda update`); no similar name is used instead", site=site)
    if site == qp.UNCHECKED:
        where = "whether the repository spells it was not checked (too large to scan)"
    elif qp.spells_whole(g, site, text):
        where = f"the name occurs at {site}"
    else:
        where = f"`{name.rpartition('.')[2]}` occurs at {site}, not the whole name"
    cands = list(dict.fromkeys(([nid] if nid else []) + [x["node"] for x in near]))
    nearest = ", ".join(f"{g.label(n)} ({_loc(g, n)})" for n in cands[:2])
    return Resolution(text, NOT_A_SYMBOL, None, cands,
                      f"no symbol in the index is named `{text}` ({where}); no similar name is used instead"
                      + (f"; nearest symbols: {nearest}" if nearest else ""), site=site)
