"""Distinct graph ids for two symbols of one file whose names mint one id (applied during every index
build, ``index.build``).

The upstream pipeline (``project_index``, never hand-edited but for the marked patches: docs/UPSTREAM.md)
folds case and drops leading and repeated underscores when it mints an id (``make_id``: ``OrderService``
and ``orderService`` -> ``..._orderservice``, ``request`` and ``_request`` -> ``..._request``) and keeps
colliding ids apart only across files. ``class OrderService`` and ``export const orderService = new
OrderService()`` in one TypeScript file were therefore ONE node: the graph merged their attributes (the
class's kind, the const's label and line), and every edge to either landed on it - ``new OrderApi()`` read
as a call of the instance ``orderApi`` (senior evaluation, web persona). The extractor of the common
languages now keeps two such definitions apart itself (``extractors.engine._distinct_def_id``: the one
declared first keeps the id); this pass is the net for the nodes that still arrive with one id.

Verinoda splits such a group before the graph is built. Names that differ only in case are two symbols in
case-sensitive languages only (in SQL, Pascal, Fortran, PHP function names ... the two spellings are one
symbol, and one node is right); names that differ in their underscores are two symbols in every language:

* the member with the fewest leading underscores keeps the id, of those the one whose name sorts first (by
  code point: ``OrderService`` before ``orderService``; ``request`` before ``_request``), the one every
  earlier build gave the merged node in the common case; each other member gets
  ``<id>_<6 hex of sha1(its name)>``. Both stay while the names do (moving a definition changes
  nothing). No other id changes.
* an edge that starts at the id belongs to the member whose definition is the last one at or before the
  edge's line in that file (the class's ``method`` edges, a call made in the const's initializer);
* an edge that ends at the id: a ``contains`` edge on a member's own line is that member's; otherwise the
  line the edge was read from decides when exactly one member's name is written on it, case and all
  (``new OrderService(`` names the class, ``import { orderService }`` the const; for a call, a name
  followed by ``(`` counts first); otherwise the edge stays on the member that kept the id.

An update re-extracts only changed files and keeps the rest of the last graph: an importer's new edge
then ends at the kept id with no collision in sight; and the extractor's own import edges name the plain
id (``import { _helper }`` -> ``..._helper``'s plain id, the one ``helper`` holds). So a split the nodes
already show (a node ``<id>_<hash>`` beside ``<id>`` in the same file, the hash that of its name, the two
names different but minting one id) routes the edges that end at ``<id>`` by the same rules.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

from verinoda.project_index.ids import normalize_id

# languages in which two names that differ only in case are two symbols
CASE_SENSITIVE_SUFFIXES = frozenset({
    ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts", ".vue", ".svelte", ".astro",
    ".py", ".pyi", ".java", ".kt", ".kts", ".scala", ".groovy", ".go", ".rs", ".c", ".h", ".cc", ".cpp",
    ".cxx", ".hpp", ".hh", ".hxx", ".m", ".mm", ".cs", ".swift", ".rb", ".dart", ".lua", ".ex", ".exs",
    ".jl", ".zig", ".erl", ".hrl", ".escript", ".r", ".sol",
})
# edges whose line is the definition of their target
_DEFINING = frozenset({"contains", "defines", "declares"})
_CALLING = frozenset({"calls", "instantiates", "indirect_call"})
_LOC = re.compile(r"^L(\d+)")
_SPLIT_ID = re.compile(r"^(.+)_([0-9a-f]{6,40})$")


@dataclass
class _Member:
    name: str        # the name without a member's "." and "()"
    line: int | None
    nid: str


@dataclass
class _Plan:
    file: str
    members: list[_Member]   # the first one kept the id


def _line(item: dict) -> int | None:
    m = _LOC.match(str(item.get("source_location") or ""))
    return int(m.group(1)) if m else None


def _bare(label) -> str:
    return str(label or "").strip().rstrip("()").lstrip(".")


def _file_key(source_file, root: Path | None) -> str:
    p = Path(str(source_file or ""))
    if not p.is_absolute() and root is not None:
        p = root / p
    return os.path.normcase(os.path.normpath(str(p)))


def _digest(name: str) -> str:
    return hashlib.sha1(name.encode("utf-8")).hexdigest()


def _case_sensitive(source_file) -> bool:
    return Path(str(source_file or "")).suffix.lower() in CASE_SENSITIVE_SUFFIXES


def _symbol(name: str, case_sensitive: bool) -> str:
    """What makes two names one symbol: the name itself, or its case-folded form where case is folded."""
    return name if case_sensitive else name.casefold()


def _keeps_id_first(name: str) -> tuple:
    """Sort key of the members of a split: fewest leading underscores (the public name), then code point."""
    return len(name) - len(name.lstrip("_")), name


_IDENT = re.compile(r"[\w$]+")


def _one_id(names) -> bool:
    """Are these (different) names identifiers that mint one id (they differ in case or underscores), rather
    than labels that share it by some other route (a heading ``Foo bar`` beside ``Foo-bar``, a quoted SQL
    name beside the bare one)?"""
    names = list(names)
    if len({n.casefold() for n in names}) == 1:
        return True   # case alone
    return all(_IDENT.fullmatch(n) for n in names) and len({normalize_id(n) for n in names}) == 1


def _new_id(nid: str, name: str, file: str, owner: dict[str, tuple[str, str]]) -> str:
    """``<nid>_<hash of name>``, longer when another symbol has that id (the same symbol may: an update
    keeps the last graph's node for it)."""
    digest = _digest(name)
    for n in (6, 10, 16, 40):
        cand = f"{nid}_{digest[:n]}"
        if owner.get(cand, (file, name)) == (file, name):
            return cand
    return f"{nid}_{digest}_{len(owner)}"


def _plans(nodes: list, root: Path | None) -> tuple[dict[str, _Plan], set[str]]:
    """(the plan of every split id, the ids split now)."""
    groups: dict[str, list[dict]] = {}
    owner: dict[str, tuple[str, str]] = {}
    for n in nodes:
        if isinstance(n, dict) and isinstance(n.get("id"), str) and n.get("source_file"):
            groups.setdefault(n["id"], []).append(n)
            owner.setdefault(n["id"], (_file_key(n["source_file"], root), _bare(n.get("label"))))
    plans: dict[str, _Plan] = {}
    split_now: set[str] = set()
    for nid, group in groups.items():
        if len(group) < 2:
            continue
        files = {_file_key(n["source_file"], root) for n in group}
        if len(files) != 1:
            continue   # across files the upstream pass keeps them apart by path
        cs = _case_sensitive(group[0]["source_file"])
        first_line: dict[str, int | None] = {}   # symbol -> its first line
        spelled: dict[str, str] = {}             # symbol -> the spelling it is known by (the first by code point)
        for n in group:
            name = _bare(n.get("label"))
            sym = _symbol(name, cs)
            spelled[sym] = min(spelled.get(sym, name), name)
            ln = _line(n)
            if sym not in first_line or (ln is not None and (first_line[sym] is None or ln < first_line[sym])):
                first_line[sym] = ln
        if len(first_line) < 2 or not _one_id(spelled.values()):
            continue   # one symbol (the same one twice, or one spelled two ways where case is folded), or
            # names that share the id by some other route than how it is minted
        file = files.pop()
        syms = sorted(first_line, key=lambda s: _keeps_id_first(spelled[s]))
        owner[nid] = (file, spelled[syms[0]])
        members = [_Member(spelled[syms[0]], first_line[syms[0]], nid)]
        sym_nid = {syms[0]: nid}
        for sym in syms[1:]:
            new = _new_id(nid, spelled[sym], file, owner)
            owner[new] = (file, spelled[sym])
            members.append(_Member(spelled[sym], first_line[sym], new))
            sym_nid[sym] = new
        plans[nid] = _Plan(file, members)
        split_now.add(nid)
        for n in group:
            n["id"] = sym_nid[_symbol(_bare(n.get("label")), cs)]
    # splits the nodes already show (kept from the last graph by an update)
    for nid, group in groups.items():
        m = _SPLIT_ID.match(nid)
        base = groups.get(m.group(1)) if m else None
        if not base or m.group(1) in plans and nid in {x.nid for x in plans[m.group(1)].members}:
            continue
        file, name = owner[nid]
        bfile, bname = owner[m.group(1)]
        cs = _case_sensitive(group[0]["source_file"])
        if (file != bfile or _symbol(name, cs) == _symbol(bname, cs) or not _one_id((name, bname))
                or not _digest(name).startswith(m.group(2))):
            continue
        plan = plans.setdefault(m.group(1), _Plan(file, [_Member(bname, _line(base[0]), m.group(1))]))
        plan.members.append(_Member(name, _line(group[0]), nid))
    return plans, split_now


class _Lines:
    def __init__(self, root: Path | None):
        self.root = root
        self.cache: dict[str, list[str] | None] = {}

    def at(self, source_file, line: int | None) -> str | None:
        if not source_file or not line:
            return None
        key = _file_key(source_file, self.root)
        if key not in self.cache:
            try:
                self.cache[key] = Path(key).read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                self.cache[key] = None
        lines = self.cache[key]
        return lines[line - 1] if lines and 0 < line <= len(lines) else None


def _written(name: str, text: str, call: bool) -> bool:
    tail = r"\s*\(" if call else r"(?![\w$])"
    return re.search(r"(?<![\w$])" + re.escape(name) + tail, text) is not None


def _route(plan: _Plan, edge: dict, end: str, root: Path | None, lines: _Lines) -> str:
    ln = _line(edge)
    same_file = _file_key(edge.get("source_file"), root) == plan.file
    if same_file and ln is not None:
        if end == "source":
            before = [m for m in plan.members if m.line is not None and m.line <= ln]
            if before:
                return max(before, key=lambda m: m.line).nid
        elif edge.get("relation") in _DEFINING:
            own = [m for m in plan.members if m.line == ln]
            if len(own) == 1:
                return own[0].nid
    text = lines.at(edge.get("source_file"), ln)
    if text:
        passes = [True, False] if edge.get("relation") in _CALLING else [False]
        for call in passes:
            named = [m for m in plan.members if _written(m.name, text, call)]
            if len(named) == 1:
                return named[0].nid
    return plan.members[0].nid


def split_case_collisions(nodes: list, edges: list, root: Path | str | None = None) -> dict[str, list[str]]:
    """Give, in place, the symbols of one file whose names differ only in case distinct ids, and move the
    edges that end or start at such an id to the symbol they belong to (see the module docstring).
    ``root`` resolves relative ``source_file`` values. Returns {kept id: [the new ids beside it]} for the ids
    split by this call."""
    root_path = Path(root) if root else None
    plans, split_now = _plans(nodes, root_path)
    if not plans:
        return {}
    lines = _Lines(root_path)
    for e in edges:
        if not isinstance(e, dict):
            continue
        for end in ("source", "target"):
            plan = plans.get(e.get(end))
            if plan is not None:
                e[end] = _route(plan, e, end, root_path, lines)
    return {nid: [m.nid for m in plans[nid].members[1:]] for nid in sorted(split_now)}
