"""Change review: the blast radius of a diff by concern, with evidence and honest unknowns (docs/DESIGN.md D35).

``verinoda review`` / MCP ``change_review``. For the working tree against a base commit (default HEAD),
the staged changes, or a planned change (``targets`` + ``change``):

1. **Changed symbols** from :func:`verinoda.anchors.compute_facts` of both versions, compared per
   definition by signature / body / module-statement hashes: ``body``, ``signature``, ``added``,
   ``removed``, ``module_statement``; changed keys of config files (``config_key``); ``file_only`` for
   files without facts. Comments, whitespace and docstrings do not count.
2. **Dependents by change kind** over the last snapshot's graph, depth 3, each with its via-chain
   (``at`` line, EXTRACTED / INFERRED); a body change follows callers, a signature change call sites
   too, a removal every reference, a module statement the readers of its binding. Truncation is
   always reported.
3. **Concerns** from rule tables (:mod:`verinoda.review_rules`), each finding with ``status``,
   ``at``, ``evidence_at``, ``basis`` and ``derived_by``: persistence (a sink on a changed line, a
   changed call that reaches a sink, the changed function's value carried to a sink by its callers),
   security (security-sensitive operations on changed lines, exit guards removed or changed, permission
   checks removed or changed, a check made constant), performance (IO or queries inside loops, loops
   added to hot paths), public API (call sites whose arity no longer fits, removed names still used),
   config (environment and config values read on changed lines, changed config keys) and entry points
   (entries that reach the change).
4. **Tests**: static reach, observed reach from the runtime tracer's latest run or ``observe``, and a
   run of the selected tests (``run_tests``) through :mod:`verinoda.experiments`.
5. **Unknowns** with a next step, and a **read_first** list packed to ``max_chars``.

Honesty rules: a dependent is "possibly affected"; a finding never says "safe" or "no impact" - an empty
concern reads "no finding from rules R"; mechanical facts (a call bound through imports on a changed
line, a guard that the old syntax tree had and the new one does not, an arity mismatch against the new
signature) are ``statically_verified``, everything else is at most ``strong_inference`` and name-only
heuristics ``weak_inference``. Nothing here runs or imports the project's code except the tests the
caller asks for (``observe`` / ``run_tests``, isolated copies); git is read with plumbing through
:mod:`verinoda.treestate` (validated refs, ``--`` before paths).
"""

from __future__ import annotations

import ast
import builtins
import difflib
import hashlib
import re
import time
from collections import deque
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath

from verinoda import anchors
from verinoda import review_rules as rr
from verinoda.architecture_map import CONFIG_FILE_RE, ENV_PATTERNS, entry_reasons
from verinoda.architecture_map import is_test_file as _map_test_file

CONCERNS = ("persistence", "security", "performance", "public_api", "config", "entry_points")
PLANNED_KINDS = ("body", "signature", "remove")
DEPTH = 3
MAX_DEPENDENTS = 40
MAX_PER_CONCERN = 25
DEFAULT_MAX_CHARS = 6000
CODE_SUFFIXES = tuple(sorted({".py", ".pyi", *anchors.TS_LANGS}))
REVIEWABLE_SUFFIXES = tuple(sorted({*CODE_SUFFIXES, *rr.CONFIG_SUFFIXES, *anchors.MD_SUFFIXES, ".gradle", ".xml",
                                    ".mcfunction", ".sql", ".sh", ".ps1", ".cfg", ".txt"}))
DATA_SUFFIXES = (".json", ".mcfunction", ".mcmeta", ".txt", ".csv", ".lang", ".svg", ".html", ".css", ".rst")
DOC_SUFFIXES = (*anchors.MD_SUFFIXES, ".rst", ".txt", ".adoc")
# files that make their directory a project of its own (a vendored copy, a sub-project)
_PROJECT_MARKERS = {"pyproject.toml", "setup.py", "setup.cfg", "package.json", "build.gradle", "build.gradle.kts",
                    "pom.xml", "Cargo.toml", "go.mod"}
RULES = {
    "persistence": ["sink pattern on a changed line (architecture_map.SINK_PATTERNS + review NBT rows)",
                    "a call on a changed line whose callee reaches a write sink (depth <= 2)",
                    "Python: the changed function's return value carried by its callers (def-use per hop) into a "
                    "call that writes it in a sink statement"],
    "security": ["security-sensitive operations on changed lines (Python: calls bound through imports, builtins, "
                 "shell=True / verify=False, SQL text built from strings; other languages: text rows)",
                 "exit guards (if ...: return/raise/throw, assert) removed or changed (syntax tree diff)",
                 "permission checks written as calls removed or changed", "a check function made constant",
                 "security words on changed lines (weak_inference)"],
    "performance": ["a call inside a loop whose callee reaches IO/queries (N+1), or IO in the loop",
                    "a loop added or its condition changed in a hot path (tick/render registrations, tick-like "
                    "overrides, request handlers)"],
    "public_api": ["call sites whose arguments no longer fit a changed signature (Python: bound by imports; "
                   "Java/Kotlin: argument counts)", "removed names still imported or used"],
    "config": ["environment reads on changed lines", "names bound from environment/config modules read on "
               "changed lines", "config values and keys (config classes, key literals) on changed lines",
               "changed keys of config files, with their readers found by literal key search"],
    "entry_points": ["entries that reach the change within depth 3 (decorators, handler names, entry modules, "
                     "packet/command/event registrations found by text)"],
}
LIMITS = [
    "static analysis: dynamic dispatch, reflection, dependency injection and callbacks are not resolved; "
    "a dependent is possibly affected, not proven broken",
    "value flow is intra-procedural per hop (Python only): values carried through containers, objects or "
    "globals are not followed",
    "registrations by method reference (Owner::name) and hot-path/entry tables are text rules: inference",
    "the graph is the last snapshot's: the changed files are re-read from the working tree; a new caller in an "
    "unchanged file appears only after `verinoda update`",
    "which changed lines the tests execute is not measured (the call tracer records calls, not lines)",
    "findings are not stored as claims; the review record is stored in the analyses table",
]


# -- data -----------------------------------------------------------------------------------------------

@dataclass
class FileDiff:
    rel: str
    old: str | None
    new: str | None
    tracked: bool = True


@dataclass
class Change:
    file: str
    qual: str | None
    kind: str
    lines: tuple[int, int] | None = None \
        # span in the new version
    old_lines: tuple[int, int] | None = None    # span in the base version
    new_changed: set[int] = field(default_factory=set)
    old_changed: set[int] = field(default_factory=set)
    test: bool = False
    node: str | None = None
    def_line: int | None = None
    old_def_line: int | None = None
    renamed_from: str | None = None   # an added definition whose body equals a removed one's in the same file
    old_qual: str | None = None   # an import statement: the names it bound in the base version

    @property
    def symbol(self) -> str:
        return f"{self.file}::{self.qual}" if self.qual else self.file

    @property
    def name(self) -> str:
        return _last(self.qual or "")

    def as_dict(self) -> dict:
        d = {"file": self.file, "symbol": self.symbol, "kind": self.kind}
        if self.lines:
            d["lines"] = _span_text(self.lines)
        if self.old_lines and (self.kind == "removed" or self.old_lines != self.lines):
            d["base_lines"] = _span_text(self.old_lines)
        if self.test:
            d["test"] = True
        if self.renamed_from:
            d["renamed_from"] = self.renamed_from
        if self.old_qual and self.old_qual != self.qual and self.lines and self.old_lines:
            d["base_names"] = self.old_qual   # an import statement edited in place
        return d


def _span_text(sp: tuple[int, int]) -> str:
    return f"{sp[0]}-{sp[1]}" if sp[1] != sp[0] else str(sp[0])


def _last(qual: str) -> str:
    return qual.split("#")[0].rpartition(".")[2]


def _clean_label(label: str) -> str:
    return label.strip().lstrip(".").split("(")[0].strip()


def _is_test(rel: str) -> bool:
    from verinoda.treestate import is_test_file

    return is_test_file(rel) or _map_test_file(rel)


def _suffix(rel: str) -> str:
    return PurePosixPath(rel).suffix.lower()


def _lang(rel: str) -> str:
    s = _suffix(rel)
    return "python" if s in (".py", ".pyi") else ("jvm" if s in (".java", ".kt", ".kts") else s.lstrip("."))


# -- context: the working tree, the base and the graph ------------------------------------------------

class _Ctx:
    def __init__(self, repo: Path, g, store, base_texts: dict[str, str | None]):
        self.repo = Path(repo).resolve()
        self.g = g
        self.store = store
        self.base_texts = base_texts
        # the staged tree: files whose working-tree copy may differ from the index are served from the index
        self.overrides: dict[str, str | None] = {}
        self.index_files: list[str] | None = None
        self.staged: dict | None = None   # staged mode: the base commit and the staged paths (for test runs)
        self._text: dict[str, str | None] = {}
        self._code: dict[tuple[str, str], list[str]] = {}
        self._facts: dict[tuple[str, str], dict | None] = {}
        self._pytree: dict[tuple[str, str], ast.AST | None] = {}
        self._tstree: dict[tuple[str, str], object] = {}
        self._files: list[str] | None = None
        self._pyix = None
        self._sinks: dict[tuple[str, str], list[dict]] = {}
        self._regs: list[dict] | None = None
        self._node_units: dict[str, tuple[str, str] | None] = {}
        self._imports_cache: dict[str, dict] = {}
        self._callees_cache: dict[tuple, list] = {}
        self._shadow: dict[tuple[str, str], dict] = {}
        self._roots: list[str] | None = None
        self._edge_ok: dict[tuple[str, str], bool] = {}
        self._importers: dict[str, set[str]] | None = None
        self._graph_files: set[str] = set()
        self._drifted: list[str] | None = None
        self.reparsed: set[str] = set()

    # texts ------------------------------------------------------------------------------------------
    def text(self, rel: str, side: str = "new") -> str | None:
        if side == "old":   # the base version of a file in the diff; any other file is the same in both
            return self.base_texts[rel] if rel in self.base_texts else self.text(rel)
        if rel not in self._text:
            if rel in self.overrides:
                self._text[rel] = self.overrides[rel]
            else:
                try:
                    self._text[rel] = _decode((self.repo / rel).read_bytes())
                except OSError:
                    self._text[rel] = None
        return self._text[rel]

    def served(self, rel: str) -> bool:
        """Is ``rel`` read from somewhere else than its working-tree file (the diff, the index)?"""
        return rel in self.base_texts or rel in self.overrides

    def lines(self, rel: str, side: str = "new") -> list[str]:
        t = self.text(rel, side)
        return t.split("\n") if t is not None else []

    def code(self, rel: str, side: str = "new") -> list[str]:
        """Lines with comments blanked (strings kept)."""
        key = (rel, side)
        if key not in self._code:
            t = self.text(rel, side)
            self._code[key] = _code_lines(t, _suffix(rel)) if t is not None else []
        return self._code[key]

    def facts(self, rel: str, side: str = "new") -> dict | None:
        key = (rel, side)
        if key not in self._facts:
            if side == "new" and not self.served(rel) and self.store is not None:
                # a file outside the diff: the store's facts cache (keyed by the file's sha256) serves it
                f = anchors.facts_for(self.store, self.repo, rel)
            else:
                t = self.text(rel, side)
                f = self._facts_of_text(rel, t) if t is not None else None
            self._facts[key] = f if anchors.usable(f) else None
        return self._facts[key]

    def _facts_of_text(self, rel: str, text: str) -> dict | None:
        """Facts of one version's text through the facts cache (keyed by the content's sha256): the base version
        of a file usually is what the last ``update`` indexed, and an MCP session reviews the same text again."""
        data = text.encode("utf-8", "surrogatepass")
        sha = hashlib.sha256(data).hexdigest()
        hit = anchors.facts_by_sha(self.store, rel, sha)
        if hit is not None:
            return hit
        f = anchors.compute_facts(rel, data)
        scheme = anchors.scheme_for(rel)
        if f is not None and scheme is not None and anchors.usable(f):
            f["sha256"] = sha
            anchors._mem_put(sha, scheme, f)
            if self.store is not None:
                try:
                    self.store.put_file_facts(sha, scheme, f)
                except Exception:  # noqa: BLE001 - a read-only or busy store: the review still runs
                    pass
        return f

    def pytree(self, rel: str, side: str = "new") -> ast.AST | None:
        key = (rel, side)
        if key not in self._pytree:
            self._pytree[key] = rr.py_parse(self.text(rel, side)) if _suffix(rel) in (".py", ".pyi") else None
        return self._pytree[key]

    def tstree(self, rel: str, side: str = "new"):
        key = (rel, side)
        if key not in self._tstree:
            t = self.text(rel, side)
            self._tstree[key] = rr.ts_tree(t, _suffix(rel)) if (t is not None and _suffix(rel) in anchors.TS_LANGS) \
                else None
        return self._tstree[key]

    def files(self) -> list[str]:
        if self._files is None:
            if self.index_files is not None:   # the staged tree: what the index holds
                self._files = list(self.index_files)
            else:
                from verinoda.snapshot import list_files

                self._files = list_files(self.repo)
        return self._files

    def pyix(self):
        if self._pyix is None:
            from verinoda.guards import _PyIndex

            ix = _PyIndex(self.repo, self.files())
            # the import/alias engine reads the tree under review, not the working-tree files
            for rel in [*self.base_texts, *self.overrides]:
                if _suffix(rel) in (".py", ".pyi"):
                    ix._trees[rel] = (self.pytree(rel), self.text(rel))
            self._pyix = ix
        return self._pyix

    def shadow(self, rel: str, side: str = "new") -> dict:
        """id(ast.Name) -> names bound by the functions around it (:func:`review_rules.py_shadowing`)."""
        key = (rel, side)
        if key not in self._shadow:
            self._shadow[key] = rr.py_shadowing(self.pytree(rel, side))
        return self._shadow[key]

    def importers(self, rel: str) -> set[str] | None:
        """Files with an import edge (``imports`` / ``imports_from``) into ``rel`` or its definitions in the last
        snapshot's graph; None when the graph does not know ``rel`` (then the files are searched by text)."""
        g = self.g
        if g is None:
            return None
        if self._importers is None:
            idx: dict[str, set[str]] = {}
            for u, v, _d in g.edges({"imports", "imports_from"}):
                fv, fu = g.file(v), g.file(u)
                if fv and fu:
                    idx.setdefault(fv, set()).add(fu)
            self._importers = idx
            self._graph_files = {g.file(n) for n in g.G.nodes if g.file(n)}
        if rel not in self._graph_files:
            return None
        return self._importers.get(rel, set())

    def py_candidates(self, rel: str, name: str) -> list[str]:
        """Python files that may name ``rel``'s ``name`` through an import or in ``rel`` itself: ``rel``, the files
        importing it in the graph, every file the review reads from elsewhere than the snapshot (the diff, the
        index) and every Python file the snapshot does not have as it is now (added or edited since the last
        ``update``: the graph knows nothing of its imports) - or, when the graph does not know ``rel``, every Python
        file whose text holds ``name``."""
        imp = self.importers(rel)
        if imp is None:
            return [f for f in self.files() if f.endswith((".py", ".pyi")) and name in (self.text(f) or "")]
        extra = [f for f in [*self.base_texts, *self.overrides] if self.text(f) is not None]
        extra += [f for f in self.drifted() if f.endswith((".py", ".pyi")) and name in (self.text(f) or "")]
        return sorted({f for f in {rel, *imp, *extra} if f.endswith((".py", ".pyi"))})

    def drifted(self) -> list[str]:
        """Code files outside the diff that the last snapshot does not have with their current content (added or
        edited since the last ``update``): the graph's edges from and into them are missing or old. Empty without
        a snapshot."""
        if self._drifted is None:
            out: list[str] = []
            snap = None
            if self.store is not None:
                try:
                    snap = self.store.latest_snapshot()
                except Exception:  # noqa: BLE001 - no snapshot table yet: nothing to compare with
                    snap = None
            if snap is not None:
                recorded = self.store.snapshot_files(snap["id"])
                for rel in self.files():
                    if _suffix(rel) not in CODE_SUFFIXES or rel in self.base_texts:
                        continue
                    try:
                        if rel in self.overrides:   # staged mode: the index's version is the one reviewed
                            t = self.overrides[rel]
                            cur = hashlib.sha256(t.encode("utf-8")).hexdigest() if t is not None else None
                        else:
                            cur = hashlib.sha256((self.repo / rel).read_bytes()).hexdigest()
                    except OSError:
                        continue
                    if recorded.get(rel) != cur:
                        out.append(rel)
            self._drifted = sorted(out)
        return self._drifted

    def project_root(self, rel: str) -> str:
        """The innermost directory holding its own project marker (pyproject.toml, package.json, build.gradle
        ...) that contains ``rel``; "" for the repository's own."""
        if self._roots is None:
            roots = {str(PurePosixPath(f).parent) for f in self.files() if PurePosixPath(f).name in _PROJECT_MARKERS}
            self._roots = sorted((r for r in roots if r not in ("", ".")), key=len, reverse=True)
        return next((r for r in self._roots if rel.startswith(r + "/")), "")

    # symbols and nodes ------------------------------------------------------------------------------
    def span(self, rel: str, qual: str, side: str = "new") -> tuple[int, int] | None:
        s = ((self.facts(rel, side) or {}).get("symbols") or {}).get(qual)
        return (s["start"], s["end"]) if s else None

    def sym(self, rel: str, qual: str, side: str = "new") -> dict | None:
        return ((self.facts(rel, side) or {}).get("symbols") or {}).get(qual)

    def node_of(self, rel: str, qual: str) -> str | None:
        """The graph node of ``rel::qual``: a node of that name whose line is the definition's line in either
        version, else the only one whose class (or its absence) matches. None when no node fits - a new
        function named like an existing method of the same file is not that method."""
        g = self.g
        if g is None or not qual:
            return None
        name = _last(qual)
        cands = [n for n in g.symbols_in(rel) if _clean_label(g.label(n)) == name]
        if not cands:
            return None
        want: set[int] = set()
        for side in ("new", "old"):
            s = self.sym(rel, qual, side)
            if s:
                want |= {s["start"], s["def"]}
        owner_q = qual.split("#")[0].rpartition(".")[0]
        owner = _last(owner_q) if owner_q else ""
        owner_is_class = bool(owner) and any((self.sym(rel, owner_q, side) or {}).get("kind") == "class"
                                             for side in ("new", "old"))

        def owner_fits(n: str) -> bool:
            owners = {_clean_label(g.label(u)) for u, _ in g.in_edges(n, {"method"})}
            return owner in owners if owner_is_class else not owners

        by_line = [n for n in cands if g.line(n) in want]
        if len(by_line) == 1:
            return by_line[0]
        fit = [n for n in (by_line or cands) if owner_fits(n)]
        if by_line:
            return (fit or by_line)[0]
        return fit[0] if len(fit) == 1 else None

    def unit_of(self, node: str) -> tuple[str, str] | None:
        """(file, qualified name in the current facts) of a graph node."""
        if node in self._node_units:
            return self._node_units[node]
        g = self.g
        rel = g.file(node)
        out = None
        if rel:
            name = _clean_label(g.label(node))
            line = g.line(node)
            for side in ("new", "old"):
                syms = (self.facts(rel, side) or {}).get("symbols") or {}
                hits = [q for q, s in syms.items() if _last(q) == name and line in (s["start"], s["def"])]
                if not hits:
                    named = [q for q in syms if _last(q) == name]
                    hits = named if len(named) == 1 else []
                if hits:
                    out = (rel, hits[0])   # a symbol removed since keeps its base name
                    break
        self._node_units[node] = out
        return out

    def label_at(self, rel: str, line: int, side: str = "new") -> str | None:
        enc = anchors.enclosing(self.facts(rel, side), line, line)
        return enc[1] if enc and enc[0] == "sym" else None

    # registrations by method reference (Owner::name) ------------------------------------------------
    def registrations(self) -> list[dict]:
        if self._regs is None:
            regs = []
            for rel in self.files():
                if _suffix(rel) not in (".java", ".kt", ".kts", ".js", ".ts") or _is_test(rel):
                    continue
                t = self.text(rel)
                if not t or "::" not in t:
                    continue
                code = self.code(rel)
                for i, ln in enumerate(code, 1):
                    if "::" not in ln:
                        continue
                    for rx, what, hot, entry in rr.REGISTRATIONS:
                        m = rx.search(ln)
                        if m:
                            regs.append({"owner": m.group(1), "name": m.group(2), "at": f"{rel}:{i}",
                                         "what": what, "hot": hot, "entry": entry})
            self._regs = regs
        return self._regs

    def registered(self, rel: str, qual: str) -> list[dict]:
        if _suffix(rel) not in (".java", ".kt", ".kts", ".js", ".ts"):
            return []   # method references name JVM / JS members: a Python definition is never one
        name = _last(qual)
        owner = _last(qual.rpartition(".")[0]) if "." in qual else PurePosixPath(rel).stem
        return [r for r in self.registrations() if r["name"] == name and r["owner"] in (owner, "this", "super")]

    # sinks ------------------------------------------------------------------------------------------
    def unit_sinks(self, unit: tuple[str, str]) -> list[dict]:
        if unit in self._sinks:
            return self._sinks[unit]
        rel, qual = unit
        sp = self.span(rel, qual)
        out = []
        if sp and not _is_test(rel):
            # a class (a constructor call resolved to it) holds its own lines only - field initialisers, not the
            # bodies of its methods
            own = _own_lines(self.facts(rel), qual) if (self.sym(rel, qual) or {}).get("kind") == "class" else None
            for line, kind, by in rr.sink_hits(self.code(rel), sp[0], sp[1]):
                if own is not None and line not in own:
                    continue
                out.append({"at": f"{rel}:{line}", "kind": kind, "derived_by": by,
                            "text": self.lines(rel)[line - 1].strip()[:120]})
        self._sinks[unit] = out
        return out

    # calls out of a unit ----------------------------------------------------------------------------
    def callees(self, unit: tuple[str, str], lines: set[int] | None = None) -> list[tuple[int, str, tuple | None,
                                                                                         str, str | None]]:
        """``(line, called name, callee unit or None, how, edge confidence)`` of the calls in ``unit``
        (only on ``lines`` when given). Python is re-read from the current syntax tree; other languages use
        the graph's call edges (their lines re-found by name when the file changed)."""
        cache = self._callees_cache
        key = (unit, tuple(sorted(lines)) if lines is not None else None)
        if key not in cache:
            cache[key] = self._callees_uncached(unit, lines)
        return cache[key]

    def _callees_uncached(self, unit: tuple[str, str], lines: set[int] | None) -> list:
        rel, qual = unit
        out = []
        s = self.sym(rel, qual)
        if s is None:
            return out
        if _suffix(rel) in (".py", ".pyi"):
            fn = rr.py_def_at(self.pytree(rel), s["def"])
            if fn is not None:
                self.reparsed.add(rel)
                for call in rr.py_calls_in(fn):
                    if lines is not None and not (set(range(call.lineno, (call.end_lineno or call.lineno) + 1))
                                                  & lines):
                        continue
                    tgt, how, conf = self.py_resolve(rel, qual, fn, call)
                    out.append((call.lineno, rr.call_name(call) or "?", tgt, how, conf))
                return out
        node = self.node_of(rel, qual)
        if node is None or self.g is None:
            return out
        changed = rel in self.base_texts
        code = self.code(rel)
        for v, d in self.g.out_edges(node, {"calls"}):
            tgt = self.unit_of(v)
            name = _clean_label(self.g.label(v))
            loc = str(d.get("source_location") or "")
            line = int(loc[1:]) if loc.startswith("L") and loc[1:].isdigit() else None
            if changed or line is None:
                line = self._find_call_line(code, s, name, line)
            if line is None or (lines is not None and line not in lines):
                continue
            out.append((line, name, tgt, "graph call edge" + (f" ({d['_origin']})" if d.get("_origin") else ""),
                        d.get("confidence")))
        # calls on the given lines that the graph does not know (the change added them): by name in the same
        # file, or `Owner.name(` to a class of that name the file can see (same package or imported)
        if lines is not None:
            known = {(ln, nm) for ln, nm, *_ in out}
            syms = (self.facts(rel) or {}).get("symbols") or {}
            for ln in sorted(lines):
                if not (s["start"] <= ln <= s["end"]) or ln > len(code):
                    continue
                for m in re.finditer(r"(?:\b([A-Z]\w*)\s*\.\s*)?\b([A-Za-z_]\w*)\s*\(", code[ln - 1]):
                    owner, nm = m.group(1), m.group(2)
                    if (ln, nm) in known or nm in _NOT_CALLS:
                        continue
                    local = [q for q in syms if _last(q) == nm and (not owner or q.startswith(owner + ".")
                                                                    or f".{owner}." in q)]
                    if len(local) == 1:
                        out.append((ln, nm, (rel, local[0]), "same file, by name", "INFERRED"))
                        known.add((ln, nm))
                    elif owner:
                        tgt = self._class_member(rel, owner, nm)
                        if tgt:
                            out.append((ln, nm, tgt, f"`{owner}.{nm}(` to the class {owner} (text)", "INFERRED"))
                            known.add((ln, nm))
        return out

    def _class_member(self, rel: str, owner: str, name: str) -> tuple[str, str] | None:
        """``(file, qual)`` of ``owner.name`` in a JVM source file named after the class ``owner`` that ``rel``
        sees (same package or an import)."""
        text = self.text(rel) or ""
        for f in self.files():
            if PurePosixPath(f).stem != owner or _suffix(f) not in (".java", ".kt", ".kts"):
                continue
            if not _jvm_sees(text, _jvm_package(self.text(f) or ""), owner):
                continue
            quals = [q for q in ((self.facts(f) or {}).get("symbols") or {}) if q.split("#")[0] == f"{owner}.{name}"]
            if quals:
                return f, quals[0]
        return None

    @staticmethod
    def _find_call_line(code: list[str], s: dict, name: str, hint: int | None) -> int | None:
        rx = re.compile(rf"\b{re.escape(name)}\s*\(")
        hits = [i for i in range(s["start"], min(s["end"], len(code)) + 1) if rx.search(code[i - 1])]
        if not hits:
            return None
        return min(hits, key=lambda i: abs(i - (hint or i)))

    def py_resolve(self, rel: str, qual: str, fn: ast.AST, call: ast.Call) -> tuple[tuple | None, str, str | None]:
        """Resolve one Python call in ``fn`` (of ``rel``/``qual``) to a unit: same-file definitions, imports,
        ``self.m()``, annotated parameters and ``x = Cls()`` locals, then the graph's edges by name."""
        f = call.func
        syms = (self.facts(rel) or {}).get("symbols") or {}
        imports = self._py_imports(rel)

        def cls_method(crel: str, cqual: str, meth: str):
            s2 = ((self.facts(crel) or {}).get("symbols") or {})
            return (crel, f"{cqual}.{meth}") if f"{cqual}.{meth}" in s2 else None

        def resolve_name(name: str):
            if name in syms and "." not in name:
                if syms[name]["kind"] == "class":
                    return cls_method(rel, name, "__init__") or (rel, name)
                return (rel, name)
            if name in imports:
                mod, orig = imports[name]
                mrel = self.pyix().modules.get(mod) if mod else None
                if mrel and orig:
                    s2 = (self.facts(mrel) or {}).get("symbols") or {}
                    if orig in s2:
                        if s2[orig]["kind"] == "class":
                            return cls_method(mrel, orig, "__init__") or (mrel, orig)
                        return (mrel, orig)
            return None

        if isinstance(f, ast.Name):
            u = resolve_name(f.id) if f.id not in _fn_locals(fn) else None   # a parameter or local hides it
            if u:
                return u, "Python: name bound in the file or by an import", "EXTRACTED"
        elif isinstance(f, ast.Attribute):
            base = f.value
            mod = self.py_module_of_expr(rel, base)
            if mod:   # a module attribute: import m / from pkg import m / import pkg.m
                mrel = self.pyix().modules.get(mod)
                if mrel and f.attr in ((self.facts(mrel) or {}).get("symbols") or {}):
                    return (mrel, f.attr), "Python: module attribute", "EXTRACTED"
            if isinstance(base, ast.Name):
                owner = qual.rpartition(".")[0]
                if base.id in ("self", "cls") and owner:
                    u = cls_method(rel, owner, f.attr)
                    if u:
                        return u, "Python: self/cls method", "EXTRACTED"
                cls = self._py_receiver_class(fn, base.id)
                if cls:
                    cu = resolve_name(cls)
                    if cu:
                        cq = cu[1][:-len(".__init__")] if cu[1].endswith(".__init__") else cu[1]
                        u = cls_method(cu[0], cq, f.attr)
                        if u:
                            return u, f"Python: {base.id} is {cls} (annotation or constructor)", "INFERRED"
        # the graph's call edges from this unit, by name
        node = self.node_of(rel, qual)
        name = rr.call_name(call)
        if node is not None and name:
            for v, d in self.g.out_edges(node, {"calls"}):
                if _clean_label(self.g.label(v)) == name:
                    u = self.unit_of(v)
                    if u:
                        return u, "graph call edge" + (f" ({d['_origin']})" if d.get("_origin") else ""), \
                            d.get("confidence")
        return None, "unresolved", None

    def _py_imports(self, rel: str) -> dict[str, tuple[str | None, str | None]]:
        """alias -> (module, name or None for ``import m``) from the file's imports (cached per file)."""
        cache = self._imports_cache
        if rel not in cache:
            cache[rel] = self._py_imports_uncached(rel)
        return cache[rel]

    def _py_imports_uncached(self, rel: str) -> dict[str, tuple[str | None, str | None]]:
        out: dict[str, tuple[str | None, str | None]] = {}
        for alias, mod, name in self._py_import_rows(rel):
            out[alias] = (mod, name)
        return out

    def py_imported(self, rel: str) -> list[tuple[str, str | None]]:
        """``(module, name or None)`` of every import in a Python file, dotted module names in full."""
        key = f"\0full:{rel}"
        if key not in self._imports_cache:
            rows = []
            tree = self.pytree(rel)
            for n in ast.walk(tree) if tree is not None else ():
                if isinstance(n, ast.Import):
                    rows += [(a.name, None) for a in n.names]
            rows += [(mod, name) for _a, mod, name in self._py_import_rows(rel) if name is not None and mod]
            self._imports_cache[key] = rows
        return self._imports_cache[key]

    def _py_import_rows(self, rel: str) -> list[tuple[str, str | None, str | None]]:
        tree = self.pytree(rel)
        out: list[tuple[str, str | None, str | None]] = []
        if tree is None:
            return out
        from verinoda.guards import _module_of

        pkg = (_module_of(rel) or "")
        if not rel.endswith("__init__.py"):
            pkg = pkg.rpartition(".")[0]
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                for a in n.names:
                    out.append((a.asname or a.name.split(".")[0], a.name if a.asname else a.name.split(".")[0], None))
            elif isinstance(n, ast.ImportFrom):
                mod = n.module or ""
                if n.level:
                    parts = pkg.split(".") if pkg else []
                    parts = parts[: len(parts) - (n.level - 1)] if n.level > 1 else parts
                    mod = ".".join([*parts, *([mod] if mod else [])])
                for a in n.names:
                    if a.name != "*":
                        out.append((a.asname or a.name, mod, a.name))
        return out

    def py_module_of_expr(self, rel: str, expr: ast.AST) -> str | None:
        """The module an expression names through the file's imports: ``m`` for ``import pkg.m as m`` or
        ``from pkg import m``, ``pkg.m`` for ``import pkg.m`` - only when that module is a file of the project."""
        d = rr.dotted(expr)
        if not d:
            return None
        head, _, rest = d.partition(".")
        imp = self._py_imports(rel).get(head)
        if imp is None:
            return None
        m, orig = imp
        full = (f"{m}.{orig}" if orig else (m or "")) + (f".{rest}" if rest else "")
        return full if full in self.pyix().modules else None

    @staticmethod
    def _py_receiver_class(fn: ast.AST, var: str) -> str | None:
        args = getattr(fn, "args", None)
        if args is not None:
            for a in args.args + args.kwonlyargs:
                an = a.annotation
                if a.arg == var and an is not None:
                    if isinstance(an, ast.Name):
                        return an.id
                    if isinstance(an, ast.Constant) and isinstance(an.value, str):
                        return an.value
        for n in rr.own_nodes(fn):
            if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call) and isinstance(n.value.func, ast.Name) \
                    and any(isinstance(t, ast.Name) and t.id == var for t in n.targets):
                return n.value.func.id
        return None

    def sink_reach(self, unit: tuple[str, str], depth: int = 2, _seen: set | None = None) -> list[dict]:
        """Sinks in ``unit`` or in what it calls, up to ``depth`` call hops: ``{sink..., path: [units]}``."""
        seen = _seen if _seen is not None else set()
        if unit in seen:
            return []
        seen.add(unit)
        out = [dict(s, path=[unit]) for s in self.unit_sinks(unit)]
        if depth > 0 and not _is_test(unit[0]):
            for _line, _name, tgt, _how, conf in self.callees(unit):
                if tgt is None or tgt in seen:
                    continue
                for s in self.sink_reach(tgt, depth - 1, seen):
                    out.append(dict(s, path=[unit, *s["path"]], confidence=s.get("confidence") or conf))
        return out


# -- 1. the diff ------------------------------------------------------------------------------------------

def _fn_locals(fn: ast.AST | None) -> set[str]:
    """Names a function binds locally (:func:`review_rules.py_scope_names`), kept on its node."""
    if fn is None or not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        return set()
    got = getattr(fn, "_verinoda_locals", None)
    if got is None:
        got = rr.py_scope_names(fn)
        try:
            fn._verinoda_locals = got
        except AttributeError:
            pass
    return got


_CODE_CACHE: dict[tuple[str, str], list[str]] = {}


def _code_lines(text: str, suffix: str) -> list[str]:
    """Lines of ``text`` with comments blanked (strings kept), kept per content hash across reviews in this
    process (an MCP session reviews the same files again)."""
    from verinoda.guards import code_text

    key = (hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest(), suffix)
    hit = _CODE_CACHE.get(key)
    if hit is None:
        if len(_CODE_CACHE) > 1024:
            _CODE_CACHE.clear()
        hit = _CODE_CACHE[key] = code_text(text, suffix, keep_strings=True).split("\n")
    return hit


def _decode(data: bytes | None) -> str | None:
    """Text of a file as the review reads it: UTF-8, LF line ends, without a leading byte-order mark (Python
    runs such files; ``ast`` refuses the mark in a str)."""
    if data is None:
        return None
    t = data.decode("utf-8", "replace").replace("\r\n", "\n")
    return t[1:] if t.startswith("﻿") else t


def _diff_worktree(repo: Path, base_sha: str) -> tuple[list[FileDiff], list[dict]]:
    from verinoda import treestate

    ch = treestate.changes_vs_base(repo, base_sha)
    tracked_out = treestate._git(repo, "ls-files", "-z", "--cached") or ""
    tracked = {p for p in tracked_out.split("\0") if p}
    diffs, skipped = [], []
    for rel in sorted(ch["tree_files"]):
        old_b = ch["base"].get(rel)
        new_b = ch["contents"].get(rel)
        if ch["tree_files"][rel] is not None and new_b is None:
            skipped.append({"file": rel, "why": "changed while it was read"})
            continue
        in_git = rel in tracked or old_b is not None
        if not in_git and not rel.lower().endswith(REVIEWABLE_SUFFIXES):
            skipped.append({"file": rel, "why": "untracked and not a source, config or document file"})
            continue
        if (old_b is not None and treestate.is_binary(old_b)) or (new_b is not None and treestate.is_binary(new_b)):
            skipped.append({"file": rel, "why": "binary"})
            continue
        diffs.append(FileDiff(rel, _decode(old_b), _decode(new_b), in_git))
    return diffs, skipped


_ZERO_SHA = re.compile(r"^0+$")


def _raw_records(out: str) -> list[tuple[str, str, str, str, str, str]]:
    """``(old mode, new mode, old blob, new blob, status, path)`` of ``git diff-* --raw -z`` output."""
    recs = []
    toks = out.split("\0")
    i = 0
    while i + 1 < len(toks):
        meta, path = toks[i], toks[i + 1]
        i += 2
        parts = meta.lstrip(":").split(" ")
        if len(parts) >= 5 and path:
            recs.append((parts[0], parts[1], parts[2], parts[3], parts[4][:1], path))
    return recs


def _diff_staged(repo: Path, base_sha: str) -> tuple[list[FileDiff], list[dict]]:
    """The index (staged changes) against ``base_sha``. ``git diff-index --cached --raw`` names both blobs of
    every changed path (no path goes on a command line, so any number of staged files works); the blobs are
    read with one ``cat-file --batch``. A git failure is an error, never "every file deleted"."""
    from verinoda import treestate

    out = treestate._git(repo, "diff-index", "--cached", "--raw", "-z", "--no-abbrev", "--relative", "--no-renames",
                         base_sha, "--")
    if out is None:
        raise treestate.NotAGitTree(f"git diff-index --cached against {base_sha[:12]} failed")
    diffs, skipped = [], []
    recs = []
    for om, nm, ob, nb, status, p in _raw_records(out):
        if not treestate.safe_path(p):
            continue
        if status == "U":
            skipped.append({"file": p, "why": "unmerged in the index"})
            continue
        if "160000" in (om, nm):
            skipped.append({"file": p, "why": "a submodule"})
            continue
        if "120000" in (om, nm):
            skipped.append({"file": p, "why": "a symbolic link"})
            continue
        recs.append((None if _ZERO_SHA.match(ob) else ob, None if _ZERO_SHA.match(nb) else nb, p))
    want = [b for ob, nb, _ in recs for b in (ob, nb) if b]
    blobs = treestate.read_blobs(repo, want) if want else {}
    missing = [b for b in want if blobs.get(b) is None]
    if missing:
        raise treestate.NotAGitTree(f"could not read {len(missing)} staged or base blob(s) (e.g. {missing[0][:12]})")
    for ob, nb, p in recs:
        old_b = blobs.get(ob) if ob else None
        new_b = blobs.get(nb) if nb else None
        if (old_b is not None and treestate.is_binary(old_b)) or (new_b is not None and treestate.is_binary(new_b)):
            skipped.append({"file": p, "why": "binary"})
            continue
        old_t, new_t = _decode(old_b), _decode(new_b)
        if old_t == new_t:
            continue
        diffs.append(FileDiff(p, old_t, new_t, True))
    return diffs, skipped


def _staged_tree(repo: Path) -> tuple[list[str], dict[str, str | None]]:
    """The staged tree for the files outside the diff: (the paths the index holds, {path: staged text} for the
    paths whose working-tree file may differ from the index - ``diff-files`` lists them, stat-dirty ones
    included; their staged blob is read whatever the working tree holds)."""
    from verinoda import treestate
    from verinoda.snapshot import _GIT_SKIP_DIRS

    ls = treestate._git(repo, "ls-files", "-s", "-z")
    dirty = treestate._git(repo, "diff-files", "--name-only", "-z", "--relative")
    if ls is None or dirty is None:
        raise treestate.NotAGitTree("git ls-files / diff-files failed")
    index: dict[str, str] = {}
    for rec in ls.split("\0"):
        if "\t" not in rec:
            continue
        meta, p = rec.split("\t", 1)
        parts = meta.split(" ")
        if len(parts) >= 3 and parts[2] == "0" and parts[0] not in ("160000", "120000") and treestate.safe_path(p) \
                and not set(PurePosixPath(p).parts) & _GIT_SKIP_DIRS:
            index[p] = parts[1]
    changed = [p for p in dict.fromkeys(dirty.split("\0")) if p in index]
    blobs = treestate.read_blobs(repo, [index[p] for p in changed]) if changed else {}
    over: dict[str, str | None] = {}
    for p in changed:
        b = blobs.get(index[p])
        over[p] = None if b is None or treestate.is_binary(b) else _decode(b)
    return sorted(index), over


def _changed_lines(old: str | None, new: str | None) -> tuple[set[int], set[int]]:
    """(new-side lines added or modified, base-side lines removed or modified), 1-based."""
    a = old.split("\n") if old is not None else []
    b = new.split("\n") if new is not None else []
    sm = difflib.SequenceMatcher(None, a, b, autojunk=len(a) > 2000 or len(b) > 2000)
    new_l, old_l = set(), set()
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        old_l.update(range(i1 + 1, i2 + 1))
        new_l.update(range(j1 + 1, j2 + 1))
    return new_l, old_l


def _own_lines(facts: dict, qual: str) -> set[int]:
    """Lines of a symbol that are not inside a nested symbol."""
    s = facts["symbols"][qual]
    lines = set(range(s["start"], s["end"] + 1))
    for q, t in facts["symbols"].items():
        if q != qual and s["start"] <= t["start"] and t["end"] <= s["end"] and (t["start"], t["end"]) != \
                (s["start"], s["end"]):
            lines -= set(range(t["start"], t["end"] + 1))
    return lines


def _classify(ctx: _Ctx, fd: FileDiff) -> tuple[list[Change], dict]:
    """Changed definitions of one file, plus a note for docs / unsupported files."""
    rel = fd.rel
    new_l, old_l = _changed_lines(fd.old, fd.new)
    test = _is_test(rel)
    fo = ctx.facts(rel, "old") if fd.old is not None else None
    fn = ctx.facts(rel, "new") if fd.new is not None else None
    suffix = _suffix(rel)
    info: dict = {"file": rel, "status": "added" if fd.old is None else ("removed" if fd.new is None else "modified")}
    if _manifest_kind(rel) and fd.old is not None and fd.new is not None:
        # a registration manifest (entry points, mixins, services): its changed keys, reviewed as entry points
        return _config_changes(ctx, fd, new_l, old_l), {**info, "kind": "manifest", "manifest": _manifest_kind(rel)}
    if suffix not in CODE_SUFFIXES and (CONFIG_FILE_RE.search(rel) or suffix in (".properties", ".conf")):
        if fd.old is None or fd.new is None:   # a config file added or removed: one change, not one per key
            n = len((fd.new or fd.old or "").split("\n"))
            return [Change(rel, None, info["status"], lines=(1, n) if fd.new is not None else None,
                           old_lines=(1, n) if fd.old is not None else None, new_changed=new_l,
                           old_changed=old_l, test=test)], {**info, "kind": "config"}
        return _config_changes(ctx, fd, new_l, old_l), {**info, "kind": "config"}
    if suffix in anchors.MD_SUFFIXES:
        return [], {**info, "kind": "doc"}
    if suffix in DATA_SUFFIXES:   # data (json that is not configuration, game data, text): listed, not reviewed
        return [], {**info, "kind": "data"}
    if (fd.old is not None and fo is None) or (fd.new is not None and fn is None):
        why = "does not parse" if anchors.scheme_for(rel) else "no symbol facts for this language"
        return [Change(rel, None, "file_only", lines=(min(new_l), max(new_l)) if new_l else None,
                       old_lines=(min(old_l), max(old_l)) if old_l else None, new_changed=new_l, old_changed=old_l,
                       test=test)], {**info, "kind": "file_only", "why": why}
    so = (fo or {}).get("symbols") or {}
    sn = (fn or {}).get("symbols") or {}
    out: list[Change] = []
    for q in sorted(set(so) | set(sn), key=lambda q: ((sn.get(q) or so.get(q))["start"], q)):
        a, b = so.get(q), sn.get(q)
        if a is None:
            kind = "added"
        elif b is None:
            kind = "removed"
        elif a["sig"] != b["sig"]:
            kind = "signature"
            if suffix in anchors.TS_LANGS:
                # the facts' signature hash stops at a field named "body"; grammars without that field (Kotlin)
                # hash the whole definition: compare the text before the body instead
                ho = rr.ts_header(ctx.tstree(rel, "old"), a["def"])
                if ho is not None and ho == rr.ts_header(ctx.tstree(rel, "new"), b["def"]):
                    kind = "body"
        elif a["body"] != b["body"]:
            kind = "body"
        else:
            continue
        nc = {ln for ln in new_l if b and b["start"] <= ln <= b["end"]}
        oc = {ln for ln in old_l if a and a["start"] <= ln <= a["end"]}
        if kind == "body":
            # a change inside a nested definition changes the outer body hash too: keep the outer only when
            # one of its own lines changed
            own_new = _own_lines(fn, q)
            own_old = _own_lines(fo, q)
            if not (nc & own_new) and not (oc & own_old):
                continue
        out.append(Change(rel, q, kind, lines=(b["start"], b["end"]) if b else None,
                          old_lines=(a["start"], a["end"]) if a else None, new_changed=nc, old_changed=oc,
                          test=test, def_line=b["def"] if b else None, old_def_line=a["def"] if a else None))
    # module-level statements (the lines of definitions they wrap - `export function f` - belong to those)
    mo = (fo or {}).get("module") or {}
    mn = (fn or {}).get("module") or {}
    in_defs_new = {ln for s in sn.values() for ln in range(s["start"], s["end"] + 1)}
    in_defs_old = {ln for s in so.values() for ln in range(s["start"], s["end"] + 1)}
    for key in sorted(set(mo) | set(mn), key=lambda k: ((mn.get(k) or mo.get(k))["start"], k)):
        if key == "DOC" or key.split("#")[0] == "DOC":
            continue
        a, b = mo.get(key), mn.get(key)
        if a is not None and b is not None and a["h"] == b["h"]:
            continue
        if key.startswith("STMT:") and a is not None and b is not None:
            continue
        nc = {ln for ln in new_l if b and b["start"] <= ln <= b["end"]} - in_defs_new
        oc = {ln for ln in old_l if a and a["start"] <= ln <= a["end"]} - in_defs_old
        if not nc and not oc:
            continue
        name = key.split(":", 1)[1].split("#")[0] if key.startswith(("IMPORT:", "ASSIGN:")) else None
        if name is None:
            ln = (b or a)["start"]
            name = f"<module>@L{ln}"
        out.append(Change(rel, name, "module_statement", lines=(b["start"], b["end"]) if b else None,
                          old_lines=(a["start"], a["end"]) if a else None, new_changed=nc, old_changed=oc,
                          test=test, old_qual=name if key.startswith("IMPORT:") else None))
    return _pair_imports(out, fd), {**info, "kind": "code"}


def _pair_imports(changes: list[Change], fd: FileDiff) -> list[Change]:
    """An import statement edited in place (``from m import a, b`` -> ``from m import a, c``) is one change with its
    old and new name sets, not a removed statement and an added one: a statement only in the base version and one
    only in the new version that fall in the same hunk of the line diff are paired."""
    gone = [c for c in changes if c.kind == "module_statement" and c.old_qual and c.lines is None]
    came = [c for c in changes if c.kind == "module_statement" and c.old_qual and c.old_lines is None]
    if not gone or not came or fd.old is None or fd.new is None:
        return changes
    sm = difflib.SequenceMatcher(None, fd.old.split("\n"), fd.new.split("\n"), autojunk=False)
    old_h: dict[int, int] = {}
    new_h: dict[int, int] = {}
    for h, (tag, i1, i2, j1, j2) in enumerate(sm.get_opcodes()):
        if tag != "equal":
            old_h.update({ln: h for ln in range(i1 + 1, i2 + 1)})
            new_h.update({ln: h for ln in range(j1 + 1, j2 + 1)})
    merged: dict[int, Change] = {}
    drop: set[int] = set()
    for g in gone:
        h = old_h.get(g.old_lines[0]) if g.old_lines else None
        partner = next((a for a in came if id(a) not in merged and h is not None and a.lines
                        and new_h.get(a.lines[0]) == h), None)
        if partner is None:
            continue
        drop.add(id(g))
        merged[id(partner)] = replace(partner, old_lines=g.old_lines, old_changed=g.old_changed, old_qual=g.qual)
    return [merged.get(id(c), c) for c in changes if id(c) not in drop]


def _config_changes(ctx: _Ctx, fd: FileDiff, new_l: set[int], old_l: set[int]) -> list[Change]:
    rel = fd.rel
    new_keys = dict(rr.config_keys(rel, fd.new.split("\n"))) if fd.new is not None else {}
    old_keys = dict(rr.config_keys(rel, fd.old.split("\n"))) if fd.old is not None else {}
    out: dict[str, Change] = {}
    for ln in sorted(new_l):
        k = new_keys.get(ln)
        if k:
            out.setdefault(k, Change(rel, k, "config_key", lines=(ln, ln), new_changed={ln}))
    for ln in sorted(old_l):
        k = old_keys.get(ln)
        if k:
            c = out.setdefault(k, Change(rel, k, "config_key", old_lines=(ln, ln)))
            c.old_lines = (ln, ln)
            c.old_changed.add(ln)
    if not out and (new_l or old_l):
        return [Change(rel, None, "file_only", lines=(min(new_l), max(new_l)) if new_l else None,
                       old_lines=(min(old_l), max(old_l)) if old_l else None, new_changed=new_l, old_changed=old_l)]
    # a key whose line only gained or lost a trailing comma (a JSON entry removed after it) did not change
    nl, ol = fd.new.split("\n") if fd.new is not None else [], fd.old.split("\n") if fd.old is not None else []

    def same(c: Change) -> bool:
        if not c.lines or not c.old_lines:
            return False
        return nl[c.lines[0] - 1].strip().rstrip(",").rstrip() == ol[c.old_lines[0] - 1].strip().rstrip(",").rstrip()

    return [c for c in out.values() if not same(c)]


# -- 2. dependents -------------------------------------------------------------------------------------------

_BODY_RELS = {"calls", "references"}
_SIG_RELS = {"calls", "references", "imports_from", "imports", "inherits", "uses"}
_REMOVE_RELS = {"calls", "references", "imports_from", "imports", "inherits", "uses", "implements"}


def _edge_at(d: dict) -> str | None:
    loc = str(d.get("source_location") or "")
    f = d.get("source_file")
    return f"{f}:{loc[1:]}" if f and loc.startswith("L") else f


def _in_edges(ctx: _Ctx, node: str, rels: set[str]) -> list[tuple[str, dict]]:
    g = ctx.g
    out = [(u, d) for u, d in g.in_edges(node, rels)]
    # construction runs __init__ / a constructor: callers of the class are callers of it
    name = _clean_label(g.label(node))
    owners = [u for u, _ in g.in_edges(node, {"method"})]
    for cls in owners:
        if name in ("__init__", _clean_label(g.label(cls))):
            out += [(u, dict(d, _construction=True)) for u, d in g.in_edges(cls, {"calls"})]
    return [(u, d) for u, d in out if _edge_kept(ctx, u, node, d)]


def _edge_kept(ctx: _Ctx, caller: str, callee: str, d: dict) -> bool:
    """An incoming edge the review follows. Documentation never calls code; an INFERRED edge (a receiver's type
    guessed by name) is dropped when it crosses into another project of the repository (a directory with its own
    pyproject.toml / package.json ..., a vendored copy) or when the Python caller imports a module or class of the
    callee's name from elsewhere (a vendored copy importing its own ``store``)."""
    g = ctx.g
    uf, vf = g.file(caller) or "", g.file(callee) or ""
    if uf.lower().endswith(DOC_SUFFIXES):
        return False
    if d.get("confidence") == "EXTRACTED" or not uf or not vf or uf == vf:
        return True
    key = (uf, vf)
    if key not in ctx._edge_ok:
        vroot = ctx.project_root(vf)
        ok = ctx.project_root(uf) == vroot or _refers_to_project(ctx, uf, vf, vroot)
        if ok and uf.endswith((".py", ".pyi")) and vf.endswith((".py", ".pyi")):
            ok = not _imports_namesake(ctx, uf, vf)
        ctx._edge_ok[key] = ok
    return ctx._edge_ok[key]


_JS_IMPORT = re.compile(r"(?:\bfrom\s+|\brequire\s*\(\s*|\bimport\s*\(\s*|^\s*import\s+)[\"']([^\"'\n]+)[\"']", re.M)


def _refers_to_project(ctx: _Ctx, caller_file: str, callee_file: str, callee_root: str) -> bool:
    """Does ``caller_file``, in another project of the repository than ``callee_file`` (a monorepo package, a
    Gradle subproject), import from the callee's project? Python: an import whose top-level package is the one
    ``callee_file`` has under its project root (``from core.repo import ...`` for ``libs/core/core/repo.py``);
    Java / Kotlin: the callee's class seen through an import or the same package; JS / TS: a relative import into
    the callee's project or its package.json name. A vendored copy imports its own modules and is not linked."""
    text = ctx.text(caller_file) or ""
    cs = _suffix(callee_file)
    if cs in (".py", ".pyi") and _suffix(caller_file) in (".py", ".pyi"):
        tops = {m.split(".")[0] for m in _py_module_names(ctx, callee_file)}
        return any((m or "").split(".")[0] in tops for m, _n in ctx.py_imported(caller_file))
    if cs in (".java", ".kt", ".kts"):
        return _jvm_sees(text, _jvm_package(ctx.text(callee_file) or ""), PurePosixPath(callee_file).stem)
    if cs in (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"):
        pkg_name = None
        try:
            import json as _json

            meta = _json.loads(ctx.text(f"{callee_root}/package.json" if callee_root else "package.json") or "{}")
            pkg_name = meta.get("name") if isinstance(meta, dict) else None
        except ValueError:
            pkg_name = None
        here = PurePosixPath(caller_file).parent
        for spec in _JS_IMPORT.findall(text):
            if spec.startswith("."):
                parts: list[str] = []
                for p in (here / spec).parts:
                    if p == "..":
                        parts = parts[:-1]
                    elif p != ".":
                        parts.append(p)
                if callee_root and "/".join(parts).startswith(callee_root + "/"):
                    return True
            elif pkg_name and (spec == pkg_name or spec.startswith(pkg_name + "/")):
                return True
    return False


def _imports_namesake(ctx: _Ctx, caller_file: str, callee_file: str) -> bool:
    """Does the Python file ``caller_file`` import a module named like ``callee_file``'s module (or a name
    defined there) from another module, and nothing from ``callee_file``'s module itself?"""
    mods = _py_module_names(ctx, callee_file)
    if not mods:
        return False
    last = next(iter(mods)).rpartition(".")[2]
    classes = {q for q, s in ((ctx.facts(callee_file) or {}).get("symbols") or {}).items()
               if s.get("kind") == "class" and "." not in q}
    same, other = False, False
    for m, orig in ctx.py_imported(caller_file):
        full = f"{m}.{orig}" if orig else m
        if any(full == mod or m == mod or full.startswith(mod + ".") for mod in mods):
            same = True
        elif full.rpartition(".")[2] == last or (orig in classes and m not in mods):
            other = True
    return other and not same


def _py_module_names(ctx: _Ctx, rel: str) -> set[str]:
    """The names a Python file can be imported as: from the repository root, without a leading ``src``, and from
    its own project root (a monorepo package: ``libs/core/core/repo.py`` is ``core.repo``)."""
    from verinoda.guards import _module_of

    root = ctx.project_root(rel)
    out = set()
    for inner in (rel, rel[len(root) + 1:] if root else rel):
        for x in (inner, inner[4:] if inner.startswith("src/") else inner):
            m = _module_of(x)
            if m:
                out.add(m)
    return out


def _walk_back(ctx: _Ctx, seeds: list[tuple[str, Change]], rels: set[str], depth: int = DEPTH) -> dict:
    """BFS over incoming edges: ``{node: {"dist", "parent", "edge", "seed"}}`` (seeds at distance 0).
    Test files are recorded but not passed through."""
    g = ctx.g
    seen: dict[str, dict] = {}
    q: deque = deque()
    for n, ch in seeds:
        if n not in seen:
            seen[n] = {"dist": 0, "parent": None, "edge": None, "seed": ch}
            q.append(n)
    while q:
        v = q.popleft()
        info = seen[v]
        if info["dist"] >= depth:
            continue
        if info["dist"] > 0 and _is_test(g.file(v) or ""):
            continue
        for u, d in _in_edges(ctx, v, rels):
            if u in seen or not g.file(u) or g.is_file_node(u):
                continue
            seen[u] = {"dist": info["dist"] + 1, "parent": v, "edge": d, "seed": info["seed"]}
            q.append(u)
    return seen


def _chain(ctx: _Ctx, walk: dict, node: str) -> list[dict]:
    g = ctx.g
    out = []
    cur = node
    while walk.get(cur, {}).get("parent") is not None:
        info = walk[cur]
        d = info["edge"] or {}
        hop = {"from": _clean_label(g.label(cur)), "to": _clean_label(g.label(info["parent"])),
               "relation": "calls (construction)" if d.get("_construction") else d.get("relation"),
               "at": _relocated_at(ctx, cur, info["parent"], d), "confidence": d.get("confidence")}
        if str(d.get("_origin", "")).startswith("verinoda"):
            hop["derived_by"] = d["_origin"]
        out.append(hop)
        cur = info["parent"]
    return out


def _relocated_at(ctx: _Ctx, caller: str, callee: str, d: dict) -> str | None:
    """The edge's line, re-found by the callee's name in the caller's current span when the caller's file
    changed since the graph was built."""
    at = _edge_at(d)
    f = ctx.g.file(caller)
    if not f or f not in ctx.base_texts:
        return at
    unit = ctx.unit_of(caller)
    s = ctx.sym(*unit) if unit else None
    if s is None:
        return at
    name = _clean_label(ctx.g.label(callee))
    if d.get("_construction"):
        name = _clean_label(ctx.g.label(next((u for u, _ in ctx.g.in_edges(callee, {"method"})), callee)))
    hint = int(at.rsplit(":", 1)[1]) if at and at.rsplit(":", 1)[-1].isdigit() else None
    line = _Ctx._find_call_line(ctx.code(f), s, name, hint)
    return f"{f}:{line}" if line else at


# -- 3. concern rules --------------------------------------------------------------------------------------

def _finding(concern: str, rule: str, text: str, status: str, at: str | None, *, evidence_at=(), chain=None,
             basis: str = "", derived_by: str = "", for_symbol: str | None = None, **extra) -> dict:
    d = {"concern": concern, "rule": rule, "finding": text, "status": status, "at": at,
         "evidence_at": [e for e in dict.fromkeys(evidence_at) if e and e != at]}
    if chain:
        d["chain"] = chain
    if basis:
        d["basis"] = basis
    if derived_by:
        d["derived_by"] = derived_by
    if for_symbol:
        d["for"] = for_symbol
    d.update({k: v for k, v in extra.items() if v is not None})
    return d


def _code_units(changes: list[Change]) -> list[Change]:
    return [c for c in changes if c.qual and c.kind in ("body", "signature", "added") and not c.test
            and _suffix(c.file) in CODE_SUFFIXES]


def _persistence(ctx: _Ctx, changes: list[Change], walk: dict) -> list[dict]:
    out: list[dict] = []
    seen_at: set[str] = set()
    for c in changes:
        if c.test or _suffix(c.file) not in CODE_SUFFIXES or c.kind == "removed":
            continue
        code = ctx.code(c.file)
        for line, kind, by in rr.sink_hits(code, min(c.new_changed or {0}), max(c.new_changed or {0})):
            if line not in c.new_changed or f"{c.file}:{line}" in seen_at or kind in rr.IO_ONLY_SINKS:
                continue
            seen_at.add(f"{c.file}:{line}")
            out.append(_finding("persistence", "sink-on-changed-line",
                                f"a changed line holds a {kind}: {ctx.lines(c.file)[line - 1].strip()[:100]}",
                                "strong_inference", f"{c.file}:{line}", basis="pattern over the code of the line "
                                "(comments removed)", derived_by=by, for_symbol=c.symbol))
        out += _removed_sinks(ctx, c)
    # changed calls that reach a sink (a call whose text the base version of the definition has too did not change)
    for c in _code_units(changes):
        unit = (c.file, c.qual)
        new_calls = _new_calls(ctx, c)
        grouped: dict[str, dict] = {}   # one finding per sink kind and changed definition; more calls as evidence
        for line, name, tgt, how, conf in ctx.callees(unit, c.new_changed):
            if tgt is None or f"{c.file}:{line}" in seen_at:
                continue
            if new_calls is not None and (line, name) not in new_calls:
                continue
            hits = [h for h in ctx.sink_reach(tgt, depth=2) if h["kind"] in rr.WRITE_SINKS]
            if not hits:
                continue
            s = hits[0]
            seen_at.add(f"{c.file}:{line}")
            if s["kind"] in grouped:
                f = grouped[s["kind"]]
                if len(f["evidence_at"]) < 6:
                    f["evidence_at"].append(f"{c.file}:{line}")
                f["calls"] = f.get("calls", 1) + 1
                continue
            path = " -> ".join(_last(u[1]) for u in s["path"])
            grouped[s["kind"]] = _finding("persistence", "changed-call-reaches-sink",
                                          f"the changed call to {name}() at line {line} reaches a {s['kind']} at "
                                          f"{s['at']} (via {path})", "strong_inference", f"{c.file}:{line}",
                                          evidence_at=[s["at"]],
                                          basis=f"call resolved: {how}" + (f", edge {conf}" if conf else ""),
                                          derived_by=s["derived_by"], for_symbol=c.symbol)
        for f in grouped.values():
            if f.get("calls", 1) > 1:
                f["finding"] += f"; {f['calls'] - 1} more changed call(s) of {c.name} reach one (see evidence_at)"
            out.append(f)
    # a value bound on a changed line and written later in the same function (Python, def-use)
    for c in _code_units(changes):
        if _suffix(c.file) in (".py", ".pyi") and c.kind in ("body", "signature"):
            out += _py_local_flow(ctx, c, seen_at)
    # the changed function's value carried by its callers into a sink call (Python, def-use per hop)
    for c in _code_units(changes):
        if _suffix(c.file) not in (".py", ".pyi") or ((ctx.sym(c.file, c.qual) or {}).get("kind") == "class"):
            continue
        out += _value_flow(ctx, c, seen_at)
    out += _nbt_keys(ctx, changes)
    return out


def _removed_sinks(ctx: _Ctx, c: Change) -> list[dict]:
    """Sink lines of the base version that the new version of the definition no longer has: compared as a
    multiset of their code text over the definition's own spans (a moved line is not removed; another write left
    in the definition does not hide a removed commit; the file's line diff is not asked, since it may pair the
    removed line with the same text in another definition)."""
    if c.old_lines is None or c.lines is None or c.kind not in ("body", "signature"):
        return []
    ocode = ctx.code(c.file, "old")
    base = rr.sink_hits(ocode, *c.old_lines)
    if not base:
        return []
    code = ctx.code(c.file)

    def norm(t: str) -> str:   # a line moved into a list gains a trailing comma: the same sink
        return re.sub(r"[\s,;]+$", "", " ".join(t.split()))

    now: dict[str, int] = {}
    new_hits = rr.sink_hits(code, *c.lines) if c.lines else []
    for line, _k, _b in new_hits:
        t = norm(code[line - 1])
        now[t] = now.get(t, 0) + 1
    gone = []
    for line, kind, by in base:
        t = norm(ocode[line - 1])
        if now.get(t, 0) > 0:
            now[t] -= 1
            continue
        gone.append((line, kind, by))
    # a sink line whose text changed is still there: as many new sink lines of that kind as removed ones
    left: dict[str, int] = {}
    for line, kind, _b in new_hits:
        t = norm(code[line - 1])
        if now.get(t, 0) > 0:
            now[t] -= 1
            left[kind] = left.get(kind, 0) + 1
    out = []
    for line, kind, by in gone:
        if left.get(kind, 0) > 0:
            left[kind] -= 1
            continue
        if kind not in rr.IO_ONLY_SINKS:
            out.append(_finding("persistence", "sink-line-removed",
                                f"a {kind} line was removed or changed (base line {line}): "
                                f"`{ctx.lines(c.file, 'old')[line - 1].strip()[:90]}`", "strong_inference",
                                f"{c.file}:{line}", basis="sink pattern over the base version's code; the new version "
                                "of the definition has no line with that code", derived_by=by, for_symbol=c.symbol,
                                side="base"))
    return out


_NOT_CALLS = {"if", "for", "while", "switch", "catch", "return", "new", "synchronized", "super", "this", "when",
              "elif", "not", "and", "or", "print"}


def _jvm_call_keys(text: str, params: list[str]) -> list[tuple[int, str, str]]:
    """``(offset, name, key)`` of the calls in a text (the key: name and argument texts, parameters renamed)."""
    out = []
    for m in re.finditer(r"\b([A-Za-z_]\w*)\s*\(", text):
        if m.group(1) in _NOT_CALLS:
            continue
        args = rr.call_args(text[m.start():], m.group(1)) or []
        out.append((m.start(), m.group(1), m.group(1) + "(" + ",".join(rr.cond_key(rr.alpha(a, params))
                                                                       for a in args) + ")"))
    return out


def _new_calls(ctx: _Ctx, c: Change) -> set[tuple[int, str]] | None:
    """``(line, name)`` of the calls on changed lines that the base version of the definition does not make (its
    calls compared as text, parameters renamed by position); None when there is no base version to compare."""
    if c.old_lines is None or c.lines is None or c.old_def_line is None:
        return None
    if _suffix(c.file) in (".py", ".pyi"):
        fn = rr.py_def_at(ctx.pytree(c.file), c.def_line or -1)
        ofn = rr.py_def_at(ctx.pytree(c.file, "old"), c.old_def_line)
        if fn is None or ofn is None:
            return None
        np_, op_ = rr.py_param_names(fn), rr.py_param_names(ofn)
        old: dict[str, int] = {}
        for k in rr.py_calls_in(ofn):
            t = rr.alpha(rr._unparse(k), op_)
            old[t] = old.get(t, 0) + 1
        out = set()
        for k in sorted(rr.py_calls_in(fn), key=lambda k: (k.lineno, k.col_offset)):
            t = rr.alpha(rr._unparse(k), np_)
            if old.get(t, 0) > 0:
                old[t] -= 1
                continue
            out.add((k.lineno, rr.call_name(k) or "?"))
        return out
    np_ = rr.ts_param_names(ctx.tstree(c.file), c.def_line or -1)
    op_ = rr.ts_param_names(ctx.tstree(c.file, "old"), c.old_def_line)
    ocode = ctx.code(c.file, "old")
    old = {}
    for _o, _n, key in _jvm_call_keys("\n".join(ocode[c.old_lines[0] - 1: c.old_lines[1]]), op_):
        old[key] = old.get(key, 0) + 1
    code = ctx.code(c.file)
    out = set()
    for ln in sorted(c.new_changed):
        if ln > len(code):
            continue
        for _o, name, key in _jvm_call_keys(code[ln - 1], np_):
            if old.get(key, 0) > 0:
                old[key] -= 1
                continue
            out.add((ln, name))
    return out


def _py_local_flow(ctx: _Ctx, c: Change, seen_at: set[str]) -> list[dict]:
    """Python: a value bound on a changed line of ``c`` and written later in the same function - by a sink
    statement on an unchanged line, or by a call on an unchanged line whose callee writes the parameter that
    receives it (def-use within each function, straight-line; the rules on changed lines cover the rest)."""
    s = ctx.sym(c.file, c.qual)
    fn = rr.py_def_at(ctx.pytree(c.file), s["def"]) if s else None
    if fn is None or isinstance(fn, ast.ClassDef) or not c.new_changed:
        return []
    seeds: set[str] = set()
    first = None
    for n in rr.own_nodes(fn):
        if isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            span = set(range(n.lineno, (n.end_lineno or n.lineno) + 1))
            targets = n.targets if isinstance(n, ast.Assign) else [n.target]
        elif isinstance(n, (ast.For, ast.AsyncFor)):
            span, targets = {n.lineno}, [n.target]
        else:
            continue
        if not span & c.new_changed:
            continue
        names = {x.id for t in targets for x in ast.walk(t) if isinstance(x, ast.Name)}
        if names:
            seeds |= names
            first = n.lineno if first is None else min(first, n.lineno)
    if not seeds:
        return []
    tainted = rr.py_taint(fn, seeds)
    out = []
    code = ctx.code(c.file)
    for line, kind, by in rr.sink_hits(code, first + 1, fn.end_lineno or fn.lineno):
        at = f"{c.file}:{line}"
        if kind not in rr.WRITE_SINKS or line in c.new_changed or at in seen_at:
            continue
        if rr.py_flows_to_line(fn, seeds, line):
            seen_at.add(at)
            used = sorted(tainted & {x.id for x in ast.walk(_stmt_at(fn, line) or fn) if isinstance(x, ast.Name)})
            out.append(_finding("persistence", "changed-value-to-sink",
                                f"a value bound on a changed line ({', '.join(used) or ', '.join(sorted(seeds))}) is "
                                f"written by the {kind} at line {line}", "strong_inference", at,
                                evidence_at=[f"{c.file}:{first}"], basis="Python def-use within the function "
                                "(straight-line; branches not told apart)", derived_by=by, for_symbol=c.symbol))
    for call in sorted(rr.py_calls_in(fn), key=lambda k: (k.lineno, k.col_offset)):
        lines = set(range(call.lineno, (call.end_lineno or call.lineno) + 1))
        at = f"{c.file}:{call.lineno}"
        if call.lineno <= first or lines & c.new_changed or at in seen_at:
            continue
        args = list(call.args) + [k.value for k in call.keywords]
        if not any(rr._expr_carries(a, tainted, "\0") for a in args):
            continue
        tgt, how, conf = ctx.py_resolve(c.file, c.qual, fn, call)
        if tgt is None or not tgt[0].endswith((".py", ".pyi")):
            continue
        ts = ctx.sym(*tgt)
        tfn = rr.py_def_at(ctx.pytree(tgt[0]), ts["def"]) if ts else None
        if tfn is None or isinstance(tfn, ast.ClassDef):
            continue
        bound = isinstance(call.func, ast.Attribute) and "." in tgt[1] and \
            (ctx.sym(tgt[0], tgt[1].rpartition(".")[0]) or {}).get("kind") == "class"
        params = rr.py_carrying_params(call, tfn, tainted, "\0", bound=bound)
        hits = [h for h in ctx.unit_sinks(tgt) if h["kind"] in rr.WRITE_SINKS and params
                and rr.py_flows_to_line(tfn, params, int(h["at"].rsplit(":", 1)[1]))]
        if not hits:
            continue
        seen_at.add(at)
        name = rr.call_name(call) or "?"
        out.append(_finding("persistence", "changed-value-to-sink",
                            f"a value bound on a changed line is passed to {name}() at line {call.lineno}; {name}() "
                            f"writes it ({', '.join(sorted(params))}) in a {hits[0]['kind']} at {hits[0]['at']}",
                            "strong_inference", at, evidence_at=[hits[0]["at"], f"{c.file}:{first}"],
                            basis="Python def-use within the function and in the callee, the callee's parameter into "
                                  f"the sink's statement (straight-line); the call resolved by: {how}"
                                  + (f" ({conf})" if conf else ""), derived_by=hits[0]["derived_by"],
                            for_symbol=c.symbol))
    return out


def _stmt_at(fn: ast.AST, line: int) -> ast.AST | None:
    best = None
    for n in rr.own_nodes(fn):
        if isinstance(n, ast.stmt) and n.lineno <= line <= (n.end_lineno or n.lineno) and not isinstance(
                n, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try)):
            if best is None or (n.end_lineno or n.lineno) - n.lineno < (best.end_lineno or best.lineno) - best.lineno:
                best = n
    return best


def _nbt_keys(ctx: _Ctx, changes: list[Change]) -> list[dict]:
    """Saved data (NBT): a key read on a changed line that the class never writes, or written on a changed line
    and never read back - a renamed key loses the stored value on reload (Java / Kotlin, text rule)."""
    out: list[dict] = []
    done: set[tuple[str, str]] = set()
    for c in changes:
        if c.test or _suffix(c.file) not in (".java", ".kt", ".kts") or not c.new_changed:
            continue
        code = ctx.code(c.file)
        writes: dict[str, list[int]] = {}
        reads: dict[str, list[int]] = {}
        for i, ln in enumerate(code, 1):
            for m in rr.NBT_KEY.finditer(ln):
                (writes if m.group(1) == "put" else reads).setdefault(m.group(2), []).append(i)
        if not writes or not reads:
            continue
        for ln in sorted(c.new_changed):
            if ln > len(code):
                continue
            for m in rr.NBT_KEY.finditer(code[ln - 1]):
                op, key = m.group(1), m.group(2)
                at = f"{c.file}:{ln}"
                other = writes if op == "get" else reads
                if key in other or (at, key) in done:
                    continue
                done.add((at, key))
                near = [k for k in other if k.lower() == key.lower()] or sorted(other)[:3]
                ev = [f"{c.file}:{other[k][0]}" for k in near][:3]
                text = (f"saved-data key \"{key}\" is read here but the class writes no such key (it writes "
                        if op == "get" else
                        f"saved-data key \"{key}\" is written here but the class reads no such key back (it reads ")
                out.append(_finding("persistence", "saved-data-key-mismatch",
                                    text + ", ".join(f'"{k}"' for k in near) + ")", "strong_inference", at,
                                    evidence_at=ev, basis="NBT put/get keys of the class compared (text rule)",
                                    derived_by="review.NBT_KEY", for_symbol=c.symbol, key=key))
    return out


def _value_flow(ctx: _Ctx, c: Change, seen_at: set[str]) -> list[dict]:
    out = []
    node = c.node
    if node is None or ctx.g is None:
        return out
    frontier = [(node, c.name, [])]
    visited = {node}
    for _hop in range(DEPTH):
        nxt = []
        for callee_node, callee_name, chain in frontier:
            for u, d in _in_edges(ctx, callee_node, {"calls"}):
                if u in visited or _is_test(ctx.g.file(u) or ""):
                    continue
                unit = ctx.unit_of(u)
                if unit is None or _suffix(unit[0]) not in (".py", ".pyi"):
                    continue
                s = ctx.sym(*unit)
                fn = rr.py_def_at(ctx.pytree(unit[0]), s["def"]) if s else None
                if fn is None:
                    continue
                visited.add(u)
                carry = rr.py_carry(fn, callee_name)
                hop = {"from": _last(unit[1]), "to": callee_name, "relation": "calls",
                       "at": _relocated_at(ctx, u, callee_node, d), "confidence": d.get("confidence"),
                       "carries": "returns it" if carry.returns else ("passes it on" if carry.passes else "no")}
                chain2 = chain + [hop]
                for line, call, cname in carry.passes:
                    tgt, how, conf = ctx.py_resolve(unit[0], unit[1], fn, call)
                    at = f"{unit[0]}:{line}"
                    if tgt is None or at in seen_at or not tgt[0].endswith((".py", ".pyi")):
                        continue
                    # the last hop too must hold: the parameter that receives the value is on the sink's statement
                    ts = ctx.sym(*tgt)
                    tfn = rr.py_def_at(ctx.pytree(tgt[0]), ts["def"]) if ts else None
                    if tfn is None or isinstance(tfn, ast.ClassDef):
                        continue
                    bound = isinstance(call.func, ast.Attribute) and "." in tgt[1] and \
                        (ctx.sym(tgt[0], tgt[1].rpartition(".")[0]) or {}).get("kind") == "class"
                    params = rr.py_carrying_params(call, tfn, carry.tainted, callee_name, bound=bound)
                    hits = [s for s in ctx.unit_sinks(tgt) if s["kind"] in rr.WRITE_SINKS
                            and params and rr.py_flows_to_line(tfn, params, int(s["at"].rsplit(":", 1)[1]))]
                    if not hits:
                        continue
                    seen_at.add(at)
                    sink = hits[0]
                    via = " -> ".join([c.name] + [h["from"] for h in chain2])
                    out.append(_finding(
                        "persistence", "value-carried-to-sink",
                        f"a value computed from {c.name}() is passed to {cname}() in {_last(unit[1])} at line {line}; "
                        f"{cname}() writes it ({', '.join(sorted(params))}) in a {sink['kind']} at {sink['at']} "
                        f"(value path {via})", "strong_inference",
                        at, evidence_at=[sink["at"]], chain=list(reversed(chain2)),
                        basis="Python def-use within each function on the path, the last one into the sink's statement "
                              f"(straight-line; branches not told apart); the call to {cname}() resolved by: {how}"
                              + (f" ({conf})" if conf else ""),
                        derived_by=sink["derived_by"], for_symbol=c.symbol))
                if carry.returns:
                    nxt.append((u, _last(unit[1]), chain2))
        frontier = nxt
        if not frontier:
            break
    return out


def _security(ctx: _Ctx, changes: list[Change]) -> list[dict]:
    out: list[dict] = []
    by_file: dict[str, set[int]] = {}
    old_by_file: dict[str, set[int]] = {}
    for c in changes:
        if not c.test and _suffix(c.file) in CODE_SUFFIXES:
            by_file.setdefault(c.file, set()).update(c.new_changed)
            old_by_file.setdefault(c.file, set()).update(c.old_changed)
    # operations on changed lines; one the base version had on its changed lines too is not "added"
    for rel, lines in sorted(by_file.items()):
        if not lines:
            continue
        old_lines = old_by_file.get(rel) or set()
        if _suffix(rel) in (".py", ".pyi"):
            tree = ctx.pytree(rel)
            if tree is None:
                continue
            # the base version's operations on its changed lines, each with the call that holds it (text with the
            # parameters renamed by position) and the names its arguments read
            had: list[tuple[str, str, frozenset[str], str]] = []
            otree = ctx.pytree(rel, "old") if rel in ctx.base_texts and old_lines else None
            for ol, kind, _w, _b, _s in (rr.py_security_ops(otree, rel, None, old_lines) if otree is not None else []):
                had.append((kind, *_py_op_call(ctx, rel, "old", ol)[:3]))
            for line, kind, what, by, status in rr.py_security_ops(tree, rel, ctx.pyix(), lines):
                flow = _param_flow(ctx, rel, line)
                call_key, names, shown, real = _py_op_call(ctx, rel, "new", line)
                same = next((h for h in had if h[0] == kind and h[1] == call_key), None)
                prev = same or next((h for h in had if h[0] == kind), None)
                if prev is not None:
                    had.remove(prev)
                if same is not None:
                    text = f"a changed line holds {kind} that the base version had on its changed lines too: {what}"
                    status = "strong_inference" if flow and flow.get("from_entry") else "weak_inference"
                elif prev is not None:
                    fresh = sorted(names - prev[2])
                    text = f"a changed line changes {kind}: `{prev[3][:80]}` is now `{shown[:80]}` ({what})"
                    if fresh:
                        text += f"; its arguments now read {', '.join(real.get(x, x) for x in fresh)}"
                    else:
                        text += "; its arguments read no name they did not read before"
                        status = "strong_inference" if rr.rank(status) < rr.rank("strong_inference") else status
                else:
                    text = f"a changed line adds {kind}: {what}"
                if flow and flow.get("from_entry"):
                    text += f"; an entry point's parameter reaches it: {flow['from_entry']}"
                elif flow:
                    text += f"; it uses the parameter(s) {', '.join(flow['params'])}"
                out.append(_finding("security", "op-on-changed-line", text, status, f"{rel}:{line}",
                                    basis="Python syntax tree of the tree under review (calls bound through imports "
                                    "and aliases); parameter flow: def-use per hop, an inference"
                                    + ("; the base version had this call on its changed lines" if same is not None
                                       else "; the base version had this kind of operation on its changed lines, in "
                                            "another call" if prev is not None else ""),
                                    derived_by=by, for_symbol=_sym_for(ctx, rel, line),
                                    evidence_at=[flow["entry"]] if flow and flow.get("entry") else (),
                                    param_flow=flow))
        else:
            code, ocode = ctx.code(rel), ctx.code(rel, "old")
            had_t: list[tuple[str, str]] = []
            for ln in sorted(old_lines):
                for rx, kind in rr.TEXT_SECURITY_OPS:
                    if ln <= len(ocode) and rx.search(ocode[ln - 1]) and (kind != "sql-built-from-strings"
                                                                          or rr.sql_line(ocode[ln - 1])):
                        had_t.append((kind, _norm_code(ocode[ln - 1])))
                        break
            for line in sorted(lines):
                if line > len(code):
                    continue
                for rx, kind in rr.TEXT_SECURITY_OPS:
                    if rx.search(code[line - 1]) and (kind != "sql-built-from-strings" or rr.sql_line(code[line - 1])):
                        now = _norm_code(code[line - 1])
                        same_t = next((h for h in had_t if h == (kind, now)), None)
                        prev_t = same_t or next((h for h in had_t if h[0] == kind), None)
                        if prev_t is not None:
                            had_t.remove(prev_t)
                        if same_t is not None:
                            head = f"a changed line holds {kind} that the base version had too: "
                        elif prev_t is not None:
                            head = f"a changed line changes {kind} (the base line was `{prev_t[1][:80]}`): "
                        else:
                            head = f"a changed line adds {kind}: "
                        out.append(_finding("security", "op-on-changed-line",
                                            head + ctx.lines(rel)[line - 1].strip()[:100],
                                            "weak_inference" if same_t is not None else "strong_inference",
                                            f"{rel}:{line}", basis="text rule over the code of the line (comments "
                                            "removed); the base version's changed lines compared as text",
                                            derived_by="review.TEXT_SECURITY_OPS", for_symbol=_sym_for(ctx, rel, line)))
                        break
    out += _guard_diff(ctx, changes)
    out += _check_calls_removed(ctx, changes)
    # permission checks written as calls, removed or changed
    for c in changes:
        if c.test or _suffix(c.file) not in CODE_SUFFIXES or c.kind == "added":
            continue
        ocode, ncode = ctx.code(c.file, "old"), ctx.code(c.file)
        old_hits = {ln: m.group(0) for ln in sorted(c.old_changed) if ln <= len(ocode)
                    for m in [rr.PERMISSION_CALL.search(ocode[ln - 1])] if m}
        new_hits = {ln: m.group(0) for ln in sorted(c.new_changed) if ln <= len(ncode)
                    for m in [rr.PERMISSION_CALL.search(ncode[ln - 1])] if m}
        new_calls = {v: k for k, v in new_hits.items()}
        for ln, call in old_hits.items():
            old_text = ctx.lines(c.file, "old")[ln - 1].strip()
            if call in new_calls:
                nl = new_calls[call]
                new_text = ctx.lines(c.file)[nl - 1].strip()
                if " ".join(old_text.split()) != " ".join(new_text.split()):
                    out.append(_finding("security", "permission-changed",
                                        f"a permission check changed: `{old_text[:90]}` -> `{new_text[:90]}`",
                                        "strong_inference", f"{c.file}:{nl}", evidence_at=[f"{c.file}:{ln}"],
                                        basis="permission call on a changed line (text rule)",
                                        derived_by="review.PERMISSION_CALL", for_symbol=c.symbol))
                continue
            out.append(_finding("security", "permission-removed",
                                f"a permission check was removed (base line {ln}): `{old_text[:100]}`",
                                "strong_inference", f"{c.file}:{ln}", basis="permission call on a removed line "
                                "(text rule; the working tree no longer has that call on a changed line)",
                                derived_by="review.PERMISSION_CALL", for_symbol=c.symbol, side="base"))
    # a check function made constant
    for c in changes:
        if c.test or c.kind != "body" or not c.qual or not rr.CHECK_NAME.search(c.name):
            continue
        new_ret = _single_constant_return(ctx, c, "new")
        if new_ret and not _single_constant_return(ctx, c, "old"):
            out.append(_finding("security", "check-made-constant",
                                f"the check {c.name}() now always returns {new_ret[0]}", "strong_inference",
                                f"{c.file}:{new_ret[1]}", basis="its body is one constant return in the working tree "
                                "and was not in the base", derived_by="review.CHECK_NAME", for_symbol=c.symbol))
    # security words on changed lines: weak
    words = 0
    flagged = {f["at"] for f in out}
    for c in changes:
        if c.test or _suffix(c.file) not in CODE_SUFFIXES or words >= 3:
            continue
        code = ctx.code(c.file)
        for ln in sorted(c.new_changed):
            if ln <= len(code) and f"{c.file}:{ln}" not in flagged and rr.AUTH_WORDS.search(code[ln - 1]):
                words += 1
                flagged.add(f"{c.file}:{ln}")
                out.append(_finding("security", "security-words", "a changed line names a security-relevant "
                                    f"concept: `{ctx.lines(c.file)[ln - 1].strip()[:90]}`", "weak_inference",
                                    f"{c.file}:{ln}", basis="word match (heuristic)", derived_by="review.AUTH_WORDS",
                                    for_symbol=c.symbol))
                break
    return out


_BUILTIN_NAMES = frozenset(dir(builtins))


def _py_op_call(ctx: _Ctx, rel: str, side: str, line: int) -> tuple[str, frozenset[str], str, dict[str, str]]:
    """The call holding a security operation on ``line`` of one version of a Python file (the outermost call over
    that line): ``(key, names, text, real)`` - its source with the enclosing function's parameters renamed by
    position, the (renamed) names its arguments read (builtins aside), its source as written, and renamed -> real
    name. A line without a call (SQL text built with an f-string) gives its code line."""
    tree = ctx.pytree(rel, side)
    qual = ctx.label_at(rel, line, side)
    s = ctx.sym(rel, qual, side) if qual else None
    fn = rr.py_def_at(tree, s["def"]) if s else None
    scope = fn if fn is not None and not isinstance(fn, ast.ClassDef) else tree
    params = rr.py_param_names(fn) if scope is fn and fn is not None else []
    best = None
    for n in ast.walk(scope) if scope is not None else ():
        if isinstance(n, ast.Call) and n.lineno <= line <= (n.end_lineno or n.lineno):
            span, best_span = (n.lineno, -(n.end_lineno or n.lineno)), \
                (best.lineno, -(best.end_lineno or best.lineno)) if best is not None else None
            if best_span is None or span < best_span:
                best = n
    if best is None:
        code = ctx.code(rel, side)
        text = _norm_code(code[line - 1]) if 0 < line <= len(code) else ""
        return rr.alpha(text, params), frozenset(), text, {}
    text = rr._unparse(best)
    real: dict[str, str] = {}
    for a in [*best.args, *(k.value for k in best.keywords)]:
        for x in ast.walk(a):
            if isinstance(x, ast.Name) and x.id not in _BUILTIN_NAMES:
                real[rr.alpha(x.id, params)] = x.id
    return rr.alpha(text, params), frozenset(real), text, real


def _param_flow(ctx: _Ctx, rel: str, line: int) -> dict | None:
    """Python: which parameter of the function holding ``line`` reaches its statement, and a caller path (up to
    3 hops, def-use per hop) that feeds it from an entry point's parameter. Callers come from the graph and, for
    functions the snapshot does not know (added by the change), from the changed files' syntax trees."""
    qual = ctx.label_at(rel, line)
    s = ctx.sym(rel, qual) if qual else None
    fn = rr.py_def_at(ctx.pytree(rel), s["def"]) if s else None
    if fn is None or isinstance(fn, ast.ClassDef):
        return None
    wanted = rr.py_params_on_line(fn, line)
    if not wanted:
        return None
    start = ctx.node_of(rel, qual)
    path = [f"{_last(qual)}({', '.join(sorted(wanted))})"]
    if _entry_info(ctx, start, (rel, qual)):
        return {"params": sorted(wanted), "from_entry": path[0], "entry": f"{rel}:{s['def']}"}
    frontier = [((rel, qual), start, fn, wanted, path)]
    seen = {(rel, qual)}
    for _ in range(DEPTH):
        nxt = []
        for cunit, node, cfn, want, p in frontier:
            bound = "." in cunit[1] and (ctx.sym(cunit[0], cunit[1].rpartition(".")[0]) or {}).get("kind") == "class"
            for unit, u, ufn, calls in _py_callers(ctx, cunit, node):
                if unit in seen:
                    continue
                seen.add(unit)
                got: set[str] = set()
                for call in calls:
                    got |= rr.py_args_from_params(ufn, call, cfn, want,
                                                  bound=bound and isinstance(call.func, ast.Attribute))
                if not got:
                    continue
                us = ctx.sym(*unit)
                p2 = [f"{_last(unit[1])}({', '.join(sorted(got))})"] + p
                if _entry_info(ctx, u, unit):
                    return {"params": sorted(wanted), "from_entry": " -> ".join(p2),
                            "entry": f"{unit[0]}:{us['def']}"}
                nxt.append((unit, u, ufn, got, p2))
        frontier = nxt
    return {"params": sorted(wanted), "from_entry": None}


def _py_callers(ctx: _Ctx, unit: tuple[str, str], node: str | None) -> list[tuple[tuple[str, str], str | None,
                                                                                    ast.AST, list[ast.Call]]]:
    """``(caller unit, caller node, caller def, calls to unit)`` of the Python functions (not tests) that call
    ``unit``: over the graph's call edges, and by re-reading the changed files (calls bound through imports and
    module attributes) - a function the change added has no edge yet."""
    out: dict[tuple[str, str], tuple] = {}
    name = _last(unit[1])
    if node is not None and ctx.g is not None:
        for u, _d in _in_edges(ctx, node, {"calls"}):
            cu = ctx.unit_of(u)
            if cu is None or cu in out or _is_test(cu[0]) or not cu[0].endswith((".py", ".pyi")):
                continue
            us = ctx.sym(*cu)
            ufn = rr.py_def_at(ctx.pytree(cu[0]), us["def"]) if us else None
            if ufn is None or isinstance(ufn, ast.ClassDef):
                continue
            calls = [k for k in rr.py_calls_in(ufn) if rr.call_name(k) == name]
            if calls:
                out[cu] = (cu, u, ufn, calls)
    probe = Change(unit[0], unit[1], "added")
    for srel, call, _how, _status in _py_call_sites(ctx, probe, files=[f for f in ctx.base_texts
                                                                        if f.endswith((".py", ".pyi"))]):
        q = ctx.label_at(srel, call.lineno)
        if not q or _is_test(srel) or (srel, q) == unit:
            continue
        us = ctx.sym(srel, q)
        ufn = rr.py_def_at(ctx.pytree(srel), us["def"]) if us else None
        if ufn is None or isinstance(ufn, ast.ClassDef):
            continue
        if (srel, q) in out:
            if call not in out[(srel, q)][3]:
                out[(srel, q)][3].append(call)
            continue
        out[(srel, q)] = ((srel, q), ctx.node_of(srel, q), ufn, [call])
    return list(out.values())


def _sym_for(ctx: _Ctx, rel: str, line: int) -> str | None:
    q = ctx.label_at(rel, line)
    return f"{rel}::{q}" if q else None


def _guards(ctx: _Ctx, c: Change, side: str) -> list[tuple[str, int]]:
    span = c.old_lines if side == "old" else c.lines
    def_line = c.old_def_line if side == "old" else c.def_line
    if span is None:
        return []
    if _suffix(c.file) in (".py", ".pyi"):
        fn = rr.py_def_at(ctx.pytree(c.file, side), def_line or span[0])
        return rr.py_guards(fn) if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) else []
    tree = ctx.tstree(c.file, side)
    # nested definitions (lambdas are not definitions) are their own symbols: keep this one's own lines
    own = _own_lines(ctx.facts(c.file, side), c.qual) if ctx.facts(c.file, side) and c.qual in \
        (ctx.facts(c.file, side) or {}).get("symbols", {}) else set(range(span[0], span[1] + 1))
    return [g for g in rr.ts_guards(tree, span[0], span[1]) if g[1] in own]


def _params_of(ctx: _Ctx, c: Change, side: str) -> list[str]:
    def_line = c.old_def_line if side == "old" else c.def_line
    if def_line is None:
        return []
    if _suffix(c.file) in (".py", ".pyi"):
        return rr.py_param_names(rr.py_def_at(ctx.pytree(c.file, side), def_line))
    return rr.ts_param_names(ctx.tstree(c.file, side), def_line)


def _guard_rows(ctx: _Ctx, c: Change, side: str) -> list[dict]:
    """The exit guards of one version of a definition, each with its comparison keys: ``raw`` (the condition's
    text) and ``key`` (parameters renamed by position, so renaming a parameter changes no guard)."""
    params = _params_of(ctx, c, side)
    return [{"cond": cond, "line": ln, "raw": rr.cond_key(cond), "key": rr.cond_key(rr.alpha(cond, params))}
            for cond, ln in _guards(ctx, c, side)]


def _cond_rows(ctx: _Ctx, c: Change, side: str = "new") -> list[tuple[str, int]]:
    """Every if / while / conditional-expression condition of one version of ``c``."""
    span = c.old_lines if side == "old" else c.lines
    def_line = c.old_def_line if side == "old" else c.def_line
    if span is None:
        return []
    if _suffix(c.file) in (".py", ".pyi"):
        return rr.py_conditions(rr.py_def_at(ctx.pytree(c.file, side), def_line or span[0]))
    facts = ctx.facts(c.file, side) or {}
    own = _own_lines(facts, c.qual) if c.qual in (facts.get("symbols") or {}) else \
        set(range(span[0], span[1] + 1))
    return [r for r in rr.ts_conditions(ctx.tstree(c.file, side), *span) if r[1] in own]


def _branch_span(ctx: _Ctx, c: Change, side: str, line: int) -> tuple[int, int, int | None, int | None] | None:
    """``(first line, last line, body's first line, body's last line)`` of the if / while / assert of one version
    of ``c`` that starts on ``line``."""
    span = c.old_lines if side == "old" else c.lines
    def_line = c.old_def_line if side == "old" else c.def_line
    if span is None:
        return None
    if _suffix(c.file) in (".py", ".pyi"):
        return rr.py_branch_span(rr.py_def_at(ctx.pytree(c.file, side), def_line or span[0]), line)
    return rr.ts_branch_span(ctx.tstree(c.file, side), line)


def _norm_code(text: str) -> str:
    return " ".join(text.split())


def _work_before(ctx: _Ctx, c: Change, old_from: int, old_to: int, new_line: int) -> tuple[int, str, bool] | None:
    """Work that the new version of ``c`` runs before its line ``new_line`` although the base version ran it only
    after its lines ``old_from..old_to`` (a guard, a call to a check): a sink statement, or a call whose callee
    reaches a sink within 2 hops. ``(line, code, direct)`` of the first such statement (``direct``: the line holds
    the sink itself), None when there is none. Statements are compared as text - Python calls by their source,
    other languages by code line - and a text that the base version already ran before the guard as often as the
    new version does is not counted."""
    if c.lines is None or c.old_lines is None or not c.qual:
        return None
    code = ctx.code(c.file)
    if _suffix(c.file) in (".py", ".pyi"):
        ofn = rr.py_def_at(ctx.pytree(c.file, "old"), c.old_def_line or -1)
        fn = rr.py_def_at(ctx.pytree(c.file), c.def_line or -1)
        if ofn is None or fn is None or isinstance(fn, ast.ClassDef):
            return None
        before: dict[str, int] = {}
        after: set[str] = set()
        for k in rr.py_calls_in(ofn):
            t = rr._unparse(k)
            if (k.end_lineno or k.lineno) < old_from:
                before[t] = before.get(t, 0) + 1
            elif k.lineno > old_to:
                after.add(t)
        seen: dict[str, int] = {}
        for k in sorted(rr.py_calls_in(fn), key=lambda x: (x.lineno, x.col_offset)):
            if (k.end_lineno or k.lineno) >= new_line:
                continue
            t = rr._unparse(k)
            seen[t] = seen.get(t, 0) + 1
            if t not in after or seen[t] <= before.get(t, 0):
                continue
            direct = bool(rr.sink_hits(code, k.lineno, k.end_lineno or k.lineno))
            if not direct:
                tgt = ctx.py_resolve(c.file, c.qual, fn, k)[0]
                if tgt is None or not ctx.sink_reach(tgt):
                    continue
            return k.lineno, t, direct
        return None
    ocode = ctx.code(c.file, "old")
    (olo, ohi), (lo, _hi) = c.old_lines, c.lines
    before_l: dict[str, int] = {}
    for i in range(olo, min(old_from, len(ocode) + 1)):
        t = _norm_code(ocode[i - 1])
        before_l[t] = before_l.get(t, 0) + 1
    after_l = {_norm_code(ocode[i - 1]) for i in range(old_to + 1, min(ohi, len(ocode)) + 1)}
    work = {ln: True for ln, _k, _b in rr.sink_hits(code, lo, new_line - 1)}
    for ln, _nm, tgt, _how, _conf in ctx.callees((c.file, c.qual), set(range(lo, new_line))):
        if ln not in work and tgt is not None and ctx.sink_reach(tgt):
            work[ln] = False
    seen_l: dict[str, int] = {}
    for i in range(lo, min(new_line, len(code) + 1)):
        t = _norm_code(code[i - 1])
        seen_l[t] = seen_l.get(t, 0) + 1
        if i in work and "(" in t and t in after_l and seen_l[t] > before_l.get(t, 0):
            return i, t, work[i]
    return None


def _gates_old_work(ctx: _Ctx, c: Change, g: dict, line: int) -> bool:
    """Does the branch of the new version's if / while on ``line`` hold a statement that followed the removed guard
    ``g`` in the base version (``if not ok: raise`` turned into ``if ok: <the work>``)?"""
    old = _branch_span(ctx, c, "old", g["line"])
    new = _branch_span(ctx, c, "new", line)
    if old is None or new is None or new[2] is None or c.old_lines is None:
        return False
    ocode, code = ctx.code(c.file, "old"), ctx.code(c.file)
    follow = {t for i in range(old[1] + 1, min(c.old_lines[1], len(ocode)) + 1)
              for t in [_norm_code(ocode[i - 1])] if "(" in t or "=" in t}
    return any(_norm_code(code[i - 1]) in follow for i in range(new[2], min(new[3], len(code)) + 1)
               if i != line)


def _call_line(ctx: _Ctx, a: Change, b: Change) -> int | None:
    """The first line of the new version of ``a`` that calls ``b``."""
    if a.lines is None or not a.qual or not b.qual:
        return None
    hits = [ln for ln, _nm, tgt, _how, _conf in ctx.callees((a.file, a.qual), set(range(a.lines[0], a.lines[1] + 1)))
            if tgt == (b.file, b.qual)]
    return min(hits) if hits else None


def _helper_return(ctx: _Ctx, c: Change, name: str, changes: list[Change]) -> tuple[list[str], str, str] | None:
    """``(parameters, returned expression, "file:line")`` of a function named ``name`` - in the file of ``c`` or
    among the changed definitions - whose body is one return statement."""
    cands = [(c.file, q) for q in ((ctx.facts(c.file) or {}).get("symbols") or {}) if _last(q) == name]
    cands += [(o.file, o.qual) for o in changes if o.qual and o.name == name and o.lines and (o.file, o.qual)
              not in cands]
    for rel, qual in cands:
        s = ctx.sym(rel, qual)
        if s is None:
            continue
        if _suffix(rel) in (".py", ".pyi"):
            r = rr.py_single_return(rr.py_def_at(ctx.pytree(rel), s["def"]))
            if r:
                params = [p for p in r[0] if p not in ("self", "cls")]
                return params, rr._unparse(r[1]), f"{rel}:{s['def']}"
        elif _suffix(rel) in anchors.TS_LANGS:
            r2 = rr.ts_single_return(ctx.tstree(rel), s["def"])
            if r2:
                return r2[0], r2[1], f"{rel}:{s['def']}"
    return None


def _guard_diff(ctx: _Ctx, changes: list[Change]) -> list[dict]:
    """Exit guards of the base version that the new version of a definition does not have (syntax trees of both
    versions). Before a guard is reported removed, the review rules out: the same check in another changed or new
    definition on its call path (moved: the definition calls it, or it calls the definition), the guard split into
    several (``a or b`` -> ``if a`` + ``if b``), a new guard calling a helper that returns the old condition
    (extracted), a changed guard (the closest new one), and the condition or its negation inside a condition the new
    version adds whose branch holds the work the guard preceded (restructured). Only then is it ``guard-removed``:
    the mechanical fact that neither the check nor its negation is left in a new condition of the definition. A
    guard kept, split, extracted or moved counts as the same check only while it still runs before the work (a sink
    statement, or a call reaching one) that the base guard preceded; otherwise it is ``guard-after-work``."""
    out: list[dict] = []
    added_all: list[tuple[Change, dict]] = []
    per_change = []
    for c in changes:
        if c.test or not c.qual or c.lines is None:
            continue
        if c.kind == "added":
            # a guard can move into a new helper (an extracted function): its guards count as added ones
            added_all += [(c, g) for g in _guard_rows(ctx, c, "new")]
            continue
        if c.kind not in ("body", "signature") or c.old_lines is None:
            continue
        old_g, new_g = _guard_rows(ctx, c, "old"), _guard_rows(ctx, c, "new")
        gone, new = _multiset_minus(old_g, new_g), _multiset_minus(new_g, old_g)
        per_change.append((c, gone, new, _multiset_pairs(old_g, new_g)))
        added_all += [(c, g) for g in new]
    for c, gone, new, kept in per_change:
        old_params = _params_of(ctx, c, "old")
        changed_findings = []
        # a guard kept as it was, now below work that it preceded in the base version
        for og, ng in kept:
            if ng["line"] not in c.new_changed and not any(x < ng["line"] for x in c.new_changed):
                continue
            late = _guard_late(ctx, c, og, ng["line"])
            if late:
                out.append(_late_guard(c, og, late, f"the exit guard `if {og['cond']}: <exit>` of {c.name} (base line "
                                       f"{og['line']}) is now at line {ng['line']}", f"{c.file}:{ng['line']}"))
        for g in gone:
            cond, ln = g["cond"], g["line"]
            moved = []
            for a in added_all:
                if a[1]["raw"] == g["raw"] and a[0] is not c:
                    rel = _move_relation(ctx, c, a[0])
                    if rel:
                        moved.append((a, rel))
            if moved:
                (dest, dg), (how, call_at) = moved[0]
                if how == "callee":
                    late = _guard_late(ctx, c, g, call_at)
                    if late:
                        out.append(_late_guard(c, g, late, f"the exit guard `{cond}` of {c.name} (base line {ln}) "
                                               f"moved into {dest.name} (line {dg['line']}), which {c.name} calls at "
                                               f"line {call_at}", f"{c.file}:{call_at}", [f"{dest.file}:{dg['line']}"]))
                        continue
                    where = f"which {c.name} calls at line {call_at}"
                elif dg["line"] > call_at:   # the caller now checks after it has called the definition
                    out.append(_finding("security", "guard-after-work",
                                        f"the exit guard `{cond}` of {c.name} (base line {ln}) moved into its caller "
                                        f"{dest.name}, at line {dg['line']} - after {dest.name} calls {c.name} at line "
                                        f"{call_at}: the work of {c.name} now runs before the check",
                                        "statically_verified", f"{dest.file}:{dg['line']}",
                                        evidence_at=[f"{dest.file}:{call_at}", f"{c.file}:{ln}"],
                                        basis="syntax trees of both versions: the guard's condition compared as text, "
                                              "the positions of the guard and of the call compared by line",
                                        derived_by="review.guard_diff", for_symbol=c.symbol))
                    continue
                else:
                    where = f"which calls {c.name} at line {call_at}, after the check"
                out.append(_finding("security", "guard-moved",
                                    f"the check `{cond}` left {c.name} (base line {ln}) and appears in "
                                    f"{dest.name} at line {dg['line']}, {where}", "weak_inference", f"{c.file}:{ln}",
                                    evidence_at=[f"{dest.file}:{dg['line']}"], basis="syntax tree diff of both "
                                    "versions; the call between the two definitions read from the new version",
                                    derived_by="review.guard_diff", for_symbol=c.symbol, side="base"))
                continue
            parts = rr.split_top(" ".join(cond.split()), (" or ", "||"))
            if len(parts) > 1:
                pool, match = list(new), []
                for p in parts:
                    k = rr.cond_key(rr.alpha(p, old_params))
                    hit = next((x for x in pool if x["key"] == k), None)
                    if hit is None:
                        break
                    match.append(hit)
                    pool.remove(hit)
                else:
                    for m in match:
                        new.remove(m)
                    late_parts = [(m, w) for m in match for w in [_guard_late(ctx, c, g, m["line"])] if w]
                    if late_parts:
                        m, w = late_parts[0]
                        out.append(_late_guard(c, g, w, f"the exit guard `{cond}` of {c.name} (base line {ln}) was "
                                               f"split into {len(match)} guards and `{m['cond']}` is now at line "
                                               f"{m['line']}", f"{c.file}:{m['line']}"))
                        continue
                    out.append(_finding("security", "guard-split",
                                        f"the exit guard `{cond}` of {c.name} (base line {ln}) was split into "
                                        f"{len(match)} guards: " + ", ".join(f"`{m['cond']}` (line {m['line']})"
                                                                             for m in match)
                                        + " - the same conditions, each still before the work the base guard "
                                          "preceded; their exits were not compared", "weak_inference",
                                        f"{c.file}:{match[0]['line']}", evidence_at=[f"{c.file}:{m['line']}" for m in
                                                                                   match[1:]] + [f"{c.file}:{ln}"],
                                        basis="syntax tree diff of both versions (conditions compared as text)",
                                        derived_by="review.guard_diff", for_symbol=c.symbol))
                    continue
            helper = _guard_helper(ctx, c, g, new, changes, old_params)
            if helper:
                ng, name, where = helper
                new.remove(ng)
                late = _guard_late(ctx, c, g, ng["line"])
                if late:
                    out.append(_late_guard(c, g, late, f"the exit guard `{cond}` of {c.name} (base line {ln}) now "
                                           f"calls {name}(), which returns that condition (defined at {where}), at "
                                           f"line {ng['line']}", f"{c.file}:{ng['line']}", [where]))
                    continue
                out.append(_finding("security", "guard-moved",
                                    f"the exit guard `{cond}` of {c.name} (base line {ln}) now calls {name}(), which "
                                    f"returns that condition (defined at {where}): `{ng['cond']}` at line "
                                    f"{ng['line']} - extracted, the same check by its text", "weak_inference",
                                    f"{c.file}:{ng['line']}", evidence_at=[where, f"{c.file}:{ln}"],
                                    basis="syntax tree diff; the helper's single return expression with the call's "
                                          "arguments put in for its parameters, compared as text",
                                    derived_by="review.guard_diff", for_symbol=c.symbol))
                continue
            partner = max(new, key=lambda x: difflib.SequenceMatcher(None, g["key"], x["key"]).ratio(), default=None)
            ratio = difflib.SequenceMatcher(None, g["key"], partner["key"]).ratio() if partner else 0.0
            if partner is not None and ratio >= 0.5:
                new.remove(partner)
                late = _guard_late(ctx, c, g, partner["line"])
                f = _finding("security", "guard-changed",
                             f"the exit guard of {c.name} changed: `{cond}` (base line {ln}) is now "
                             f"`{partner['cond']}` - weakened or strengthened is not decided"
                             + (f"; it now runs after `{late[1][:90]}` (line {late[0]}), which the base guard "
                                "preceded" if late else ""),
                             "statically_verified", f"{c.file}:{partner['line']}", evidence_at=[f"{c.file}:{ln}"],
                             basis="syntax tree diff of both versions", derived_by="review.guard_diff",
                             for_symbol=c.symbol, old=cond, new=partner["cond"])
                changed_findings.append(f)
                out.append(f)
                continue
            held = _cond_holding(ctx, c, g, old_params)
            exit_lines = {x["line"] for x in _guard_rows(ctx, c, "new")}
            if held and held[2] and held[1] in exit_lines:   # now one operand of a wider exit guard
                text, line, _same = held
                out.append(_finding("security", "guard-changed",
                                    f"the exit guard of {c.name} changed: `{cond}` (base line {ln}) is now one operand "
                                    f"of `{text[:100]}` (line {line}) - weakened or strengthened is not decided",
                                    "statically_verified", f"{c.file}:{line}", evidence_at=[f"{c.file}:{ln}"],
                                    basis="syntax tree diff of both versions", derived_by="review.guard_diff",
                                    for_symbol=c.symbol, old=cond, new=text))
                continue
            if held and held[2]:   # the same test is still made, but its body no longer exits
                text, line, _same = held
                out.append(_finding("security", "guard-removed",
                                    f"the exit guard `if {cond}: <exit>` of {c.name} (base line {ln}) no longer exits: "
                                    f"the condition is still tested at line {line} (`{text[:100]}`) but that branch "
                                    "does not raise, return, break or continue any more", "statically_verified",
                                    f"{c.file}:{ln}", evidence_at=[f"{c.file}:{line}"],
                                    basis="syntax trees of both versions: the base version's if-statement ends in an "
                                          "exit, the new version's if-statement on the same condition does not",
                                    derived_by="review.guard_diff", for_symbol=c.symbol, side="base"))
                continue
            if held:
                text, line, _same = held
                out.append(_finding("security", "guard-restructured",
                                    f"the exit guard `{cond}` of {c.name} (base line {ln}) is gone as a guard, but "
                                    f"`{text[:120]}` at line {line}, a condition the base version did not have, holds "
                                    "its negation and its branch holds work the guard preceded - the check may be "
                                    "kept in another form (not decided)", "weak_inference", f"{c.file}:{line}",
                                    evidence_at=[f"{c.file}:{ln}"], basis="conditions of the new version compared as "
                                    "text with the old condition and its negation; the branch's statements with the "
                                    "base statements after the guard", derived_by="review.guard_diff",
                                    for_symbol=c.symbol))
                continue
            left = [x for x in _guard_rows(ctx, c, "new")][:3]
            out.append(_finding("security", "guard-removed",
                                f"the exit guard `if {cond}: <exit>` of {c.name} (base line {ln}) is gone: no "
                                "condition the new version adds holds the condition or its negation"
                                + ("; the new version's exit guards are " + ", ".join(
                                    f"`{x['cond'][:60]}` (line {x['line']})" for x in left) if left else
                                   "; the new version has no exit guard"),
                                "statically_verified", f"{c.file}:{ln}",
                                evidence_at=[f"{c.file}:{c.lines[0]}"] if c.lines else (),
                                basis="the base version's syntax tree has the guard; the new version's has no "
                                      "condition of its own holding it, its negation gating the guarded work, a split "
                                      "of it or a helper returning it",
                                derived_by="review.guard_diff", for_symbol=c.symbol, side="base"))
        # new guards no old one was paired with: cited on the changed guards of the same definition
        for f in changed_findings:
            if new:
                f["finding"] += "; the new version also adds " + ", ".join(f"`{x['cond'][:60]}` (line {x['line']})"
                                                                           for x in new[:3])
                f["evidence_at"] += [f"{c.file}:{x['line']}" for x in new[:3]]
    return out


def _guard_late(ctx: _Ctx, c: Change, g: dict, new_line: int) -> tuple[int, str, bool] | None:
    """Work the base guard ``g`` preceded that the new version of ``c`` runs before ``new_line``."""
    sp = _branch_span(ctx, c, "old", g["line"])
    return _work_before(ctx, c, g["line"], sp[1] if sp else g["line"], new_line)


def _late_guard(c: Change, g: dict, late: tuple[int, str, bool], what: str, at: str,
                evidence: list[str] | None = None) -> dict:
    """``guard-after-work``: the check is still made, but after work the base version made only when it passed."""
    wl, wt, direct = late
    return _finding("security", "guard-after-work",
                    f"{what}: it now runs after `{wt[:90]}` (line {wl}), which the base version ran only after the "
                    "guard passed" + ("" if direct else " (a call whose callee reaches a sink)"),
                    "statically_verified" if direct else "strong_inference", at,
                    evidence_at=[f"{c.file}:{wl}", *(evidence or []), f"{c.file}:{g['line']}"],
                    basis="syntax trees of both versions: statements compared as text, their order against the guard's "
                          "by line" + ("" if direct else "; the callee's sink found within 2 call hops"),
                    derived_by="review.guard_diff", for_symbol=c.symbol)


def _move_relation(ctx: _Ctx, c: Change, dest: Change) -> tuple[str, int] | None:
    """How a guard that left ``c`` for ``dest`` can still protect ``c``'s work: ``("callee", line in c)`` when the
    new version of ``c`` calls ``dest``, ``("caller", line in dest)`` when ``dest`` calls ``c``; None otherwise (an
    unrelated definition gaining the same check text is no move)."""
    at = _call_line(ctx, c, dest)
    if at is not None:
        return "callee", at
    at = _call_line(ctx, dest, c)
    return ("caller", at) if at is not None else None


def _multiset_minus(a: list[dict], b: list[dict]) -> list[dict]:
    left: dict[str, int] = {}
    for x in b:
        left[x["key"]] = left.get(x["key"], 0) + 1
    out = []
    for x in a:
        if left.get(x["key"], 0) > 0:
            left[x["key"]] -= 1
        else:
            out.append(x)
    return out


def _multiset_pairs(a: list[dict], b: list[dict]) -> list[tuple[dict, dict]]:
    """``(x, y)`` of ``a`` and ``b`` with the same key, paired in order."""
    pool: dict[str, list[dict]] = {}
    for y in b:
        pool.setdefault(y["key"], []).append(y)
    out = []
    for x in a:
        ys = pool.get(x["key"])
        if ys:
            out.append((x, ys.pop(0)))
    return out


def _guard_helper(ctx: _Ctx, c: Change, g: dict, new: list[dict], changes: list[Change],
                  old_params: list[str]) -> tuple[dict, str, str] | None:
    """A new guard that calls a helper whose single return expression, with the call's arguments put in, is the
    old condition (or its negation): ``(new guard, helper name, where)``."""
    want = {g["raw"], g["key"]} | rr.negations(g["cond"]) | rr.negations(rr.alpha(g["cond"], old_params))
    for ng in new:
        for name in dict.fromkeys(re.findall(r"\b([A-Za-z_]\w*)\s*\(", ng["cond"])):
            h = _helper_return(ctx, c, name, changes)
            if h is None:
                continue
            params, expr, where = h
            args = rr.call_args(ng["cond"], name) or []
            got = rr.substitute(expr, params, args)
            pieces = rr.cond_pieces(got)
            if pieces & want or rr.cond_pieces(rr.alpha(got, _params_of(ctx, c, "new"))) & want:
                return ng, name, where
    return None


def _cond_holding(ctx: _Ctx, c: Change, g: dict, old_params: list[str]) -> tuple[str, int, bool] | None:
    """A condition the new version adds (the base version's conditions, the removed guard's aside, do not count)
    that holds the old guard's condition or its negation as one of its ``and`` / ``or`` operands: ``(condition,
    line, same)`` - ``same`` when it is the old condition itself (the test is still made; its branch no longer
    exits); a negation counts only when its branch holds a statement that followed the guard in the base version
    (the work is now gated by it)."""
    new_params = _params_of(ctx, c, "new")
    neg = rr.negations(rr.alpha(g["cond"], old_params))
    budget: dict[str, int] = {}
    for text, _line in _cond_rows(ctx, c, "old"):
        k = rr.cond_key(rr.alpha(text, old_params))
        budget[k] = budget.get(k, 0) + 1
    if budget.get(g["key"], 0) > 0:
        budget[g["key"]] -= 1
    rows = []
    for text, line in _cond_rows(ctx, c):
        k = rr.cond_key(rr.alpha(text, new_params))
        if budget.get(k, 0) > 0:
            budget[k] -= 1
            continue
        rows.append((text, line, k))
    for text, line, k in rows:
        if k == g["key"]:
            return text, line, True
    for text, line, _k in rows:
        pieces = rr.cond_pieces(rr.alpha(text, new_params))
        if g["key"] in pieces:
            return text, line, True
        if pieces & neg and _gates_old_work(ctx, c, g, line):
            return text, line, False
    return None


def _check_calls_removed(ctx: _Ctx, changes: list[Change]) -> list[dict]:
    """Python: a call removed from a changed definition whose callee is a check - it raises on bad input (``if
    ...: raise``, ``assert``) or is named like one (``validate_*``, ``check_*``) - when the new version no longer
    calls it at all (``check-call-removed``), or calls it only after work that the base version ran after the check
    (``check-call-after-work``)."""
    out: list[dict] = []
    for c in changes:
        if c.test or c.kind not in ("body", "signature") or not c.qual or not c.old_changed or \
                _suffix(c.file) not in (".py", ".pyi") or c.old_def_line is None:
            continue
        ofn = rr.py_def_at(ctx.pytree(c.file, "old"), c.old_def_line)
        fn = rr.py_def_at(ctx.pytree(c.file), c.def_line or -1)
        if ofn is None or isinstance(ofn, ast.ClassDef):
            continue
        new_calls = sorted(rr.py_calls_in(fn), key=lambda k: k.lineno) if fn is not None else []
        still = {rr.call_name(k) for k in new_calls}
        seen: set[str] = set()
        for call in rr.py_calls_in(ofn):
            name = rr.call_name(call)
            if not name or name in seen:
                continue
            if name in still:
                # the check is still called: does it still run before the work it preceded? The calls of that name
                # are paired in order (one fewer or one more is a removal or an addition, not a move); calls inside a
                # condition are the guard diff's
                seen.add(name)
                olds = [k for k in sorted(rr.py_calls_in(ofn), key=lambda k: k.lineno) if rr.call_name(k) == name]
                news = [k for k in new_calls if rr.call_name(k) == name]
                if len(olds) != len(news):
                    continue
                late, call, later = None, None, None
                for ok, nk in zip(olds, news):
                    if id(ok) in _cond_calls(ofn) or id(nk) in _cond_calls(fn) or not (
                            set(range(ok.lineno, (ok.end_lineno or ok.lineno) + 1)) & c.old_changed
                            or any(x <= nk.lineno for x in c.new_changed)):
                        continue
                    why = _check_why(ctx, _py_resolve_old(ctx, c, ok), name)
                    late = _work_before(ctx, c, ok.lineno, ok.end_lineno or ok.lineno, nk.lineno) if why else None
                    if late is not None:
                        call, later = ok, nk
                        break
                if late is None or call is None or later is None:
                    continue
                wl, wt, direct = late
                out.append(_finding("security", "check-call-after-work",
                                    f"the call to {name}() in {c.name} (base line {call.lineno}) is now at line "
                                    f"{later.lineno}, after `{wt[:90]}` (line {wl}), which the base version ran only "
                                    f"after the check; {why[0]}", "strong_inference", f"{c.file}:{later.lineno}",
                                    evidence_at=[f"{c.file}:{wl}", *why[1]],
                                    basis="calls of both versions of the definition (syntax trees) compared as text, "
                                          "their order by line; the callee resolved through the file's definitions and "
                                          "imports" + ("" if direct else "; the work's sink found within 2 call hops"),
                                    derived_by="review.check_calls", for_symbol=c.symbol))
                continue
            if not (set(range(call.lineno, (call.end_lineno or call.lineno) + 1)) & c.old_changed):
                continue
            seen.add(name)
            tgt = _py_resolve_old(ctx, c, call)
            raising: list[int] = []
            if tgt is not None:
                s = ctx.sym(*tgt)
                raising = rr.py_raising_guards(rr.py_def_at(ctx.pytree(tgt[0]), s["def"])) if s else []
            if not raising and not (tgt is not None and rr.CHECK_NAME.search(name)):
                continue
            why = (f"it raises on bad input at {', '.join(f'{tgt[0]}:{x}' for x in raising[:3])}" if raising else
                   "named like a check")
            out.append(_finding("security", "check-call-removed",
                                f"the call to {name}() (base line {call.lineno}) was removed from {c.name}; {why}",
                                "strong_inference", f"{c.file}:{call.lineno}",
                                evidence_at=[f"{tgt[0]}:{x}" for x in raising[:3]] if tgt else [],
                                basis="calls of both versions of the definition (syntax trees); the callee resolved "
                                      "through the file's definitions and imports", derived_by="review.check_calls",
                                for_symbol=c.symbol, side="base"))
    return out


def _cond_calls(fn: ast.AST) -> set[int]:
    """``id()`` of the calls inside the conditions of ``fn`` (if / while / assert tests, conditional expressions),
    kept on the node."""
    got = getattr(fn, "_verinoda_cond_calls", None)
    if got is None:
        got = {id(x) for n in rr.own_nodes(fn) if isinstance(n, (ast.If, ast.While, ast.Assert, ast.IfExp))
               for x in ast.walk(n.test) if isinstance(x, ast.Call)}
        try:
            fn._verinoda_cond_calls = got
        except AttributeError:
            pass
    return got


def _check_why(ctx: _Ctx, tgt: tuple[str, str] | None, name: str) -> tuple[str, list[str]] | None:
    """Why the callee ``tgt`` of a call to ``name`` is a check - it raises on bad input, or it is named like one -
    with the evidence lines; None when it is neither."""
    raising: list[int] = []
    if tgt is not None:
        s = ctx.sym(*tgt)
        raising = rr.py_raising_guards(rr.py_def_at(ctx.pytree(tgt[0]), s["def"])) if s else []
    if raising:
        return (f"it raises on bad input at {', '.join(f'{tgt[0]}:{x}' for x in raising[:3])}",
                [f"{tgt[0]}:{x}" for x in raising[:3]])
    if tgt is not None and rr.CHECK_NAME.search(name):
        return "named like a check", []
    return None


def _py_resolve_old(ctx: _Ctx, c: Change, call: ast.Call) -> tuple[str, str] | None:
    """The unit a call of the base version binds to: a definition of the same file, an imported name, a module
    attribute or a ``self`` method (the callee itself read in the current tree)."""
    f = call.func
    syms = (ctx.facts(c.file) or {}).get("symbols") or {}
    osyms = (ctx.facts(c.file, "old") or {}).get("symbols") or {}
    if isinstance(f, ast.Name):
        if f.id in syms or f.id in osyms:
            return (c.file, f.id) if f.id in syms else None
        imp = ctx._py_imports(c.file).get(f.id)
        if imp and imp[0] and imp[1]:
            mrel = ctx.pyix().modules.get(imp[0])
            if mrel and imp[1] in ((ctx.facts(mrel) or {}).get("symbols") or {}):
                return mrel, imp[1]
    elif isinstance(f, ast.Attribute):
        if isinstance(f.value, ast.Name) and f.value.id in ("self", "cls") and "." in c.qual:
            q = f"{c.qual.rpartition('.')[0]}.{f.attr}"
            return (c.file, q) if q in syms else None
        mod = ctx.py_module_of_expr(c.file, f.value)
        mrel = ctx.pyix().modules.get(mod) if mod else None
        if mrel and f.attr in ((ctx.facts(mrel) or {}).get("symbols") or {}):
            return mrel, f.attr
    return None


def _single_constant_return(ctx: _Ctx, c: Change, side: str) -> tuple[str, int] | None:
    span = c.old_lines if side == "old" else c.lines
    if span is None:
        return None
    lines = ctx.code(c.file, side)[span[0] - 1: span[1]]
    body = [ln.strip() for ln in lines]
    stmts = [ln for ln in body if ln and ln not in ("{", "}") and not ln.startswith(("@", "def ", "public ", "private ",
                                                                                    "protected ", "fun ", "override "))]
    if len(stmts) == 1:
        m = re.fullmatch(r"return\s+(true|false|True|False)\s*;?", stmts[0])
        if m:
            idx = body.index(stmts[0])
            return m.group(1), span[0] + idx
    return None


def _performance(ctx: _Ctx, changes: list[Change], hot: dict[str, dict], unknown: list[dict]) -> list[dict]:
    """IO in loops and loops on hot paths. A loop and an IO call inside it that the base version already had
    are not presented as introduced by the change (weak_inference, "was there before"); a loop over a literal
    of N items states that bound; a loop added on a hot path without IO in it is weak_inference unless a
    registration makes the path hot."""
    out: list[dict] = []
    for c in _code_units(changes):
        unit = (c.file, c.qual)
        s = ctx.sym(*unit)
        if s is None:
            continue
        hot_why = hot.get(c.symbol)
        if _suffix(c.file) in (".py", ".pyi"):
            fn = rr.py_def_at(ctx.pytree(c.file), s["def"])
            if fn is None:
                continue
            old_fn = rr.py_def_at(ctx.pytree(c.file, "old"), c.old_def_line) if c.old_def_line else None
            old_params, new_params = rr.py_param_names(old_fn), rr.py_param_names(fn)
            old_loops = {rr.alpha(_loop_head_py(n), old_params): (n, lo_, hi_)
                         for n, lo_, hi_, _it in (rr.py_loops(old_fn) if old_fn is not None else [])}
            ocode = ctx.code(c.file, "old")
            for loop, lo, hi, it in rr.py_loops(fn):
                if not (set(range(lo, hi + 1)) & c.new_changed):
                    continue
                twin = old_loops.get(rr.alpha(_loop_head_py(loop), new_params))
                head_new = twin is None
                # what the same loop of the base version already did inside it
                twin_calls = {rr.call_name(k) for k in rr.py_calls_in(twin[0])} if twin else set()
                twin_kinds = {k for _l, k, _b in rr.sink_hits(ocode, _py_loop_body_start(twin[0], twin[1]),
                                                              twin[2])} if twin else set()
                body_lo = _py_loop_body_start(loop, lo)
                # every call of the loop that reaches IO, and the sinks written in its body: one the base loop did
                # not have is the one to report (a write added next to a read that was there)
                calls_io = []
                for call in sorted(rr.py_calls_in(loop), key=lambda k: (k.lineno, k.col_offset)):
                    if call.lineno < body_lo and not isinstance(loop, (ast.ListComp, ast.SetComp, ast.GeneratorExp,
                                                                       ast.DictComp)):
                        continue   # the iterable of a for-loop is evaluated once
                    tgt, how, conf = ctx.py_resolve(c.file, c.qual, fn, call)
                    hits = ctx.sink_reach(tgt, depth=2) if tgt else []
                    if hits:
                        calls_io.append((call, hits[0], how, conf, head_new or rr.call_name(call) not in twin_calls))
                direct = [(h, head_new or h[1] not in twin_kinds) for h in rr.sink_hits(ctx.code(c.file), body_lo, hi)]
                found = next((f for f in calls_io if f[4]), None)
                pick = None if found else next((d for d in direct if d[1]), None)
                if found is None and pick is None:
                    found = calls_io[0] if calls_io else None
                    pick = direct[0] if direct and found is None else None
                literal = _literal_size(loop)
                if not found and not pick:
                    if hot_why and head_new and lo in c.new_changed:
                        out.append(_hot_loop(c, lo, hot_why, "a loop", literal=literal))
                    continue
                bound = None if literal is not None else (_py_loop_bound(ctx, c, fn, it, lo) if it else None)
                if found:
                    call, sink, how, conf, io_new = found
                    text = (f"{rr.call_name(call)}() runs once per iteration of the loop at line {lo} and reaches a "
                            f"{sink['kind']} at {sink['at']} (N+1)")
                    ev = [f"{c.file}:{call.lineno}", sink["at"]]
                    basis = f"syntax tree loop; call resolved by: {how}" + (f" ({conf})" if conf else "")
                    by = sink["derived_by"]
                else:
                    (line, kind, by), io_new = pick
                    text = f"a {kind} at line {line} runs once per iteration of the loop at line {lo}"
                    ev, basis = [f"{c.file}:{line}"], "syntax tree loop; sink pattern in its body"
                before = sorted({rr.call_name(f[0]) or "?" for f in calls_io if not f[4]})
                if io_new and before:
                    text += f"; the loop already called {', '.join(f'{b}()' for b in before[:3])} before the change"
                if hot_why:
                    text += f"; the function is on a hot path ({hot_why['why']})"
                    ev += [hot_why["at"]] if hot_why.get("at") else []
                status = "strong_inference"
                if literal is not None:
                    text += f"; the loop runs over a literal of {literal} item(s)"
                    status = "weak_inference"
                elif bound:
                    text += f"; the loop is bounded by `{bound['cond']}` at {bound['at']}"
                    ev.append(bound["at"])
                elif it:
                    unknown.append({"kind": "loop_bound", "at": f"{c.file}:{lo}",
                                    "what": f"how many times the loop over `{it}` at {c.file}:{lo} runs",
                                    "why": f"no cap on len({it}) was found in {c.name} or in the checks it calls "
                                           "before the loop",
                                    "next_step": f"look for a limit on {it} in the callers, or measure with the "
                                                 "largest expected input"})
                if not io_new:
                    text += " - the loop and this call were there before the change"
                    status = "weak_inference"
                out.append(_finding("performance", "io-in-loop", text, status, f"{c.file}:{lo}",
                                    evidence_at=ev, basis=basis, derived_by=by, for_symbol=c.symbol,
                                    **({"bound": bound} if bound else {})))
        else:
            tree = ctx.tstree(c.file)
            old_tree = ctx.tstree(c.file, "old") if c.old_lines else None
            old_loops = rr.ts_loops(old_tree, *c.old_lines) if old_tree is not None and c.old_lines else []
            old_heads = [h for _, _, h in old_loops]
            for lo, hi, head in rr.ts_loops(tree, s["start"], s["end"]):
                if not (set(range(lo, hi + 1)) & c.new_changed):
                    continue
                calls_io = _loop_calls_sinks(ctx, c, lo, hi)
                if calls_io:
                    twin = next(((a, b) for a, b, h in old_loops if h == head), None)
                    ocode = ctx.code(c.file, "old")

                    def _was_there(nm: str, tw=twin, oc=ocode) -> bool:
                        return tw is not None and any(re.search(rf"\b{re.escape(nm)}\s*\(", oc[i - 1])
                                                      for i in range(tw[0], min(tw[1], len(oc)) + 1))
                    call_line, name, sink = next((x for x in calls_io if not _was_there(x[1])), calls_io[0])
                    io_new = not _was_there(name)
                    text = (f"{name}() runs once per iteration of the loop at line {lo} and reaches a "
                            f"{sink['kind']} at {sink['at']} (N+1)")
                    ev = [f"{c.file}:{call_line}", sink["at"]]
                    if hot_why:
                        text += f"; the function is on a hot path ({hot_why['why']})"
                        ev += [hot_why["at"]] if hot_why.get("at") else []
                    if not io_new:
                        text += " - the loop and this call were there before the change"
                    out.append(_finding("performance", "io-in-loop", text,
                                        "strong_inference" if io_new else "weak_inference", f"{c.file}:{lo}",
                                        evidence_at=ev, basis="tree-sitter loop; call matched to the graph's edges or "
                                        "to a class of that name", derived_by=sink["derived_by"], for_symbol=c.symbol))
                    continue
                if not hot_why:
                    continue
                if head in old_heads:
                    continue
                if lo in c.new_changed and old_heads and _similar_head(head, old_heads):
                    old_head = _similar_head(head, old_heads)
                    removed = _removed_bound(old_head, head)
                    text = (f"the loop condition at line {lo} changed on a hot path ({hot_why['why']}): "
                            f"`{old_head}` -> `{head}`" + (f"; the bound `{removed}` was removed" if removed else ""))
                    out.append(_finding("performance", "hot-loop-changed", text, hot_why.get("status",
                                                                                          "strong_inference"),
                                        f"{c.file}:{lo}", evidence_at=[hot_why["at"]] if hot_why.get("at") else [],
                                        basis="tree-sitter loop headers of both versions; hot path: "
                                              + hot_why["basis"], derived_by="review.hot_path", for_symbol=c.symbol))
                    continue
                out.append(_hot_loop(c, lo, hot_why, f"a loop (`{head[:60]}`)"))
    return out


def _py_loop_body_start(loop: ast.AST, lo: int) -> int:
    """First line of a loop's body (a multi-line iterable or condition is not part of it)."""
    body = getattr(loop, "body", None)
    if isinstance(body, list) and body:
        return min(getattr(b, "lineno", lo) for b in body)
    return lo


def _literal_size(loop: ast.AST) -> int | None:
    """The number of items when a for-loop (or comprehension) iterates over a literal tuple / list / set."""
    it = getattr(loop, "iter", None)
    if it is None and getattr(loop, "generators", None):
        it = loop.generators[0].iter
    if isinstance(it, (ast.Tuple, ast.List, ast.Set)) and not any(isinstance(e, ast.Starred) for e in it.elts):
        return len(it.elts)
    return None


def _hot_loop(c: Change, lo: int, hot_why: dict, what: str, literal: int | None = None) -> dict:
    status = hot_why.get("status", "strong_inference")
    if literal is not None:
        status = "weak_inference"
    return _finding("performance", "loop-added-hot-path",
                    f"{what} was added at line {lo} in {c.name}, which runs on a hot path ({hot_why['why']})"
                    + (f"; it runs over a literal of {literal} item(s)" if literal is not None else "")
                    + ("; no IO or query was found in it" if status == "weak_inference" else ""),
                    status, f"{c.file}:{lo}", evidence_at=[hot_why["at"]] if hot_why.get("at") else [],
                    basis="syntax tree loop on a changed line; hot path: " + hot_why["basis"],
                    derived_by="review.hot_path", for_symbol=c.symbol)


def _loop_head_py(n: ast.AST) -> str:
    if isinstance(n, (ast.For, ast.AsyncFor)):
        return "for " + rr._unparse(n.target) + " in " + rr._unparse(n.iter)
    if isinstance(n, ast.While):
        return "while " + rr._unparse(n.test)
    return type(n).__name__


def _similar_head(head: str, olds: list[str]) -> str | None:
    best = max(olds, key=lambda o: difflib.SequenceMatcher(None, head, o).ratio(), default=None)
    return best if best is not None and difflib.SequenceMatcher(None, head, best).ratio() >= 0.6 else None


def _removed_bound(old: str, new: str) -> str | None:
    cond = re.sub(r"^\s*(?:while|for|do)\s*\(?", "", old).rstrip(") {")
    for part in re.split(r"&&|\band\b", cond):
        p = part.strip().strip("()").strip()
        if p and p not in new and re.search(r"--|\+\+|<|>|budget|limit|max|count", p):
            return p
    return None


def _loop_calls_sinks(ctx: _Ctx, c: Change, lo: int, hi: int) -> list[tuple[int, str, dict]]:
    """``(line, called name, first sink)`` of every call in the loop's body whose callee reaches IO."""
    unit = (c.file, c.qual)
    out = []
    for line, name, tgt, _how, _conf in sorted(ctx.callees(unit, set(range(lo + 1, hi + 1))), key=lambda x: x[0]):
        if tgt is None:
            continue
        hits = ctx.sink_reach(tgt, depth=2)
        if hits:
            out.append((line, name, hits[0]))
    return out


def _py_loop_bound(ctx: _Ctx, c: Change, fn: ast.AST, it: str, loop_line: int) -> dict | None:
    """A cap on ``len(it)``: a guard in the changed function, or in a function it calls with ``it`` before the
    loop (the callee's parameter at that position)."""
    line = rr.py_len_cap(fn, it)
    if line is not None and line < loop_line:
        return {"at": f"{c.file}:{line}", "cond": _guard_text(ctx, c.file, line)}
    for call in rr.py_calls_in(fn):
        if call.lineno >= loop_line:
            continue
        pos = next((i for i, a in enumerate(call.args) if isinstance(a, ast.Name) and a.id == it), None)
        if pos is None:
            continue
        tgt, _how, _conf = ctx.py_resolve(c.file, c.qual, fn, call)
        if tgt is None:
            continue
        s = ctx.sym(*tgt)
        cfn = rr.py_def_at(ctx.pytree(tgt[0]), s["def"]) if s else None
        if cfn is None or not hasattr(cfn, "args"):
            continue
        params = [a.arg for a in cfn.args.args]
        if params and params[0] in ("self", "cls"):
            params = params[1:]
        if pos < len(params):
            cap = rr.py_len_cap(cfn, params[pos])
            if cap is not None:
                return {"at": f"{tgt[0]}:{cap}", "cond": _guard_text(ctx, tgt[0], cap)}
    return None


def _guard_text(ctx: _Ctx, rel: str, line: int) -> str:
    return ctx.lines(rel)[line - 1].strip()[:100] if line <= len(ctx.lines(rel)) else ""


def _public_api(ctx: _Ctx, changes: list[Change], unknown: list[dict]) -> list[dict]:
    out: list[dict] = []
    for c in changes:
        if not c.qual or c.test or _suffix(c.file) not in CODE_SUFFIXES:
            continue
        if c.kind == "signature":
            if _suffix(c.file) in (".py", ".pyi"):
                out += _py_arity(ctx, c, unknown)
            else:
                out += _jvm_arity(ctx, c)
        elif c.kind == "removed" or (c.kind == "module_statement" and c.old_lines and (
                c.lines is None or (c.old_qual and c.old_qual != c.qual))):
            # a name the module still binds (an import rewritten to import more names, a function replaced by
            # an import of it) is not removed; an import edited in place: the names it no longer binds
            planned = ctx.base_texts.get(c.file) == ctx.text(c.file)   # a planned removal: the code is unchanged
            still = set() if planned else set(((ctx.facts(c.file) or {}).get("bindings") or {}))
            names = [n for n in (c.old_qual or c.qual).split(",") if n] if c.kind == "module_statement" else [c.qual]
            for name in names:
                if "." not in name and name in still:
                    continue
                out += _removed_refs(ctx, c if name == c.qual else replace(c, qual=name), unknown)
    return out


def _py_call_sites(ctx: _Ctx, c: Change, files: list[str] | None = None) -> list[tuple[str, ast.Call, str, str]]:
    """``(file, call, how, status)`` of the calls that bind to ``c`` (Python): names imported from its module or
    defined in its file (not where a local of that name hides it), module attributes (``import pkg.m``, ``from pkg
    import m``, ``import pkg.m as x``), ``self.m()`` in its class, the class's own name, receivers annotated or
    constructed as its class, and calls of a nested function inside its enclosing one. ``files`` limits the
    files read (default: the graph's callers and the files that name it)."""
    from verinoda.guards import _module_of

    mod = _module_of(c.file)
    name = c.name
    owner = c.qual.rpartition(".")[0] if "." in c.qual else ""
    owner_kind = ((ctx.sym(c.file, owner) or ctx.sym(c.file, owner, "old") or {}).get("kind")) if owner else None
    owner_last = owner.rpartition(".")[2]
    # a constructor is called through its class: `Cls(...)` runs `Cls.__init__`
    ctor = owner_kind == "class" and name == "__init__" and "." not in owner
    search = owner_last if ctor else name
    sites = []
    if files is not None:
        cand_files = set(files)
    else:
        cand_files = {c.file}
        if c.node is not None:
            cand_files |= {ctx.g.file(u) for u, _ in _in_edges(ctx, c.node, {"calls", "imports_from", "imports"})
                           if ctx.g.file(u)}
        cand_files |= set(ctx.py_candidates(c.file, search))
    for rel in sorted(f for f in cand_files if f and f.endswith((".py", ".pyi"))):
        if rel != c.file and search not in (ctx.text(rel) or ""):
            continue
        tree = ctx.pytree(rel)
        if tree is None:
            continue
        imports = ctx._py_imports(rel)
        shadow = ctx.shadow(rel)
        local_names = {a for a, (m, orig) in imports.items() if orig == name and m == mod}
        via = {a: r for a, (m, orig) in imports.items() if not owner and orig == name and m and m != mod
               for r in [_py_reexport(ctx, m, orig, mod, name)] if r}
        local_names |= set(via)
        class_names = {a for a, (m, orig) in imports.items() if orig == owner_last and m == mod} if ctor else set()
        for call in ast.walk(tree):
            if not isinstance(call, ast.Call):
                continue
            f = call.func
            how = None
            status = "statically_verified"
            if ctor and isinstance(f, ast.Name) and f.id not in shadow.get(id(f), ()) and \
                    (f.id in class_names or (rel == c.file and f.id == owner_last)):
                how = f"construction of {owner_last} (" + ("the class imported from its module" if f.id in class_names
                                                          else "same module") + ")"
            elif ctor and isinstance(f, ast.Attribute) and f.attr == owner_last and mod and \
                    ctx.py_module_of_expr(rel, f.value) == mod:
                how = f"construction of {owner_last} (module attribute)"
            elif not owner:
                if isinstance(f, ast.Name) and f.id not in shadow.get(id(f), ()) and \
                        (f.id in local_names or (rel == c.file and f.id == name)):
                    how = (f"name imported through the re-export in {via[f.id]}" if f.id in via else
                           "name bound by an import of the changed module" if f.id in local_names else "same module")
                elif isinstance(f, ast.Attribute) and f.attr == name and mod and \
                        ctx.py_module_of_expr(rel, f.value) == mod:
                    how = "module attribute"
            elif owner_kind != "class":   # a function nested in another one: called inside it
                osym = ctx.sym(c.file, owner)
                if rel == c.file and isinstance(f, ast.Name) and f.id == name and osym and \
                        osym["start"] <= call.lineno <= osym["end"]:
                    how = f"inside {owner_last}, where it is defined"
            elif isinstance(f, ast.Attribute) and f.attr == name:
                enc = ctx.label_at(rel, call.lineno)
                if isinstance(f.value, ast.Name) and f.value.id in ("self", "cls") and rel == c.file and enc and \
                        enc.startswith(owner + "."):
                    how = "self/cls in the same class"
                elif isinstance(f.value, ast.Name) and f.value.id == owner_last:
                    how, status = "class attribute", "strong_inference"
                else:
                    efn = rr.py_def_at(tree, (ctx.sym(rel, enc) or {}).get("def", -1)) if enc else None
                    cls = ctx._py_receiver_class(efn, f.value.id) if efn is not None and \
                        isinstance(f.value, ast.Name) else None
                    if cls and _last(cls) == owner_last:
                        how, status = f"receiver annotated or constructed as {cls}", "strong_inference"
            if how:
                sites.append((rel, call, how, status))
    return sites


def _py_reexport(ctx: _Ctx, module: str, name: str, target_mod: str | None, target: str,
                 depth: int = 3) -> str | None:
    """The project file through which ``module``'s ``name`` is ``target_mod``'s ``target`` (``pkg/__init__.py``
    doing ``from .rules import validate``), following re-exports ``depth`` modules deep; None when it is not."""
    mrel = ctx.pyix().modules.get(module) if target_mod else None
    if mrel is None or depth <= 0:
        return None
    for alias, m2, orig in ctx._py_import_rows(mrel):
        if alias != name or not orig or not m2:
            continue
        if m2 == target_mod and orig == target:
            return mrel
        if _py_reexport(ctx, m2, orig, target_mod, target, depth - 1):
            return mrel
    return None


def _py_arity(ctx: _Ctx, c: Change, unknown: list[dict]) -> list[dict]:
    s = ctx.sym(c.file, c.qual)
    fn = rr.py_def_at(ctx.pytree(c.file), s["def"]) if s else None
    if fn is None or isinstance(fn, ast.ClassDef):
        return []
    params = rr.py_params(fn)
    out = []
    bound = "." in c.qual and ctx.sym(c.file, c.qual.rpartition(".")[0]) is not None and \
        (ctx.sym(c.file, c.qual.rpartition(".")[0]) or {}).get("kind") == "class"
    sites = _py_call_sites(ctx, c)
    planned = c.file in ctx.base_texts and ctx.base_texts.get(c.file) == ctx.text(c.file)
    if planned:
        # a planned signature change: there is no new signature to check the calls against - list them
        for rel, call, how, _status in sites[:MAX_PER_CONCERN]:
            out.append(_finding("public_api", "call-site-of-changed-signature",
                                f"{rel}:{call.lineno} calls {c.name}(), whose signature the planned change alters "
                                "(arguments not checked: there is no new signature yet)", "weak_inference",
                                f"{rel}:{call.lineno}", evidence_at=[f"{c.file}:{s['def']}"],
                                basis=f"call bound by: {how}", derived_by="review.call_sites", for_symbol=c.symbol))
    else:
        ofn = rr.py_def_at(ctx.pytree(c.file, "old"), c.old_def_line) if c.old_def_line else None
        old_pos = rr.py_params(ofn)["pos"] if ofn is not None and not isinstance(ofn, ast.ClassDef) else None
        for rel, call, how, status in sites:
            shown = f"{_last(c.qual.rpartition('.')[0])}.__init__" if how.startswith("construction") else c.name
            if params["decorated"] and status == "statically_verified":
                status = "strong_inference"   # a decorator may change the signature
            b = bound and how != "class attribute"
            problem = rr.py_arity_problem(params, call, bound=b)
            if problem:
                out.append(_finding("public_api", "arity-break",
                                    f"the call to {shown}() at {rel}:{call.lineno} no longer fits the new signature: "
                                    f"{problem}", status, f"{rel}:{call.lineno}", evidence_at=[f"{c.file}:{s['def']}"],
                                    basis=f"call bound by: {how}; arguments counted against the new parameters (syntax "
                                          "tree)" + ("; decorated definition" if params["decorated"] else ""),
                                    derived_by="review.arity", for_symbol=c.symbol))
                continue
            moved = _moved_positional(old_pos, params["pos"], call, b)
            if moved:
                out.append(_finding("public_api", "positional-order-changed",
                                    f"the call to {shown}() at {rel}:{call.lineno} passes positional arguments that "
                                    f"now bind to other parameters: {moved}", "strong_inference",
                                    f"{rel}:{call.lineno}", evidence_at=[f"{c.file}:{s['def']}"],
                                    basis=f"call bound by: {how}; the parameter names of both versions compared by "
                                          "position (syntax trees)", derived_by="review.arity", for_symbol=c.symbol))
    if not sites:
        unknown.append({"kind": "dynamic_callers", "at": f"{c.file}:{s['def']}",
                        "what": f"who calls {c.qual} with the old signature",
                        "why": "no call site binds to it by import, module attribute or class in the tree under review",
                        "next_step": f"search for `{c.name}(`; callers through objects are not resolved statically "
                                     "(verinoda resolve-call on a site)"})
    return out


def _moved_positional(old_pos: list[str] | None, new_pos: list[str], call: ast.Call, bound: bool) -> str | None:
    """How the positional arguments of ``call`` bind differently: ``customer -> total, total -> customer`` when a
    parameter the call fills by position has another name at that position now (and the old name still exists,
    so the call is not merely out of date)."""
    if old_pos is None or any(isinstance(a, ast.Starred) for a in call.args):
        return None
    op_, np_ = list(old_pos), list(new_pos)
    if bound:
        op_ = op_[1:] if op_ and op_[0] in ("self", "cls") else op_
        np_ = np_[1:] if np_ and np_[0] in ("self", "cls") else np_
    moved = []
    for i in range(min(len(call.args), len(op_), len(np_))):
        if op_[i] != np_[i] and op_[i] in np_:
            moved.append(f"argument {i + 1} was {op_[i]}, is now {np_[i]}")
    return "; ".join(moved) if moved else None


def _jvm_arity(ctx: _Ctx, c: Change) -> list[dict]:
    s = ctx.sym(c.file, c.qual)
    if s is None:
        return []
    shape = rr.ts_param_count(ctx.tstree(c.file), s["def"])
    owner = _last(c.qual.rpartition(".")[0]) if "." in c.qual else ""
    overloads = [q for q in ((ctx.facts(c.file) or {}).get("symbols") or {}) if q.split("#")[0] == c.qual.split("#")[0]]
    sites = _jvm_call_sites(ctx, c, owner)
    out = []
    for rel, line, how, conf in sites:
        counts = rr.ts_call_arg_counts(ctx.tstree(rel), c.name, line) if shape else []
        problem = None
        if shape and counts and len(overloads) == 1:
            req, total, varargs = shape
            bad = [n for n in counts if n < req or (n > total and not varargs)]
            if bad and len(bad) == len(counts):
                problem = f"{bad[0]} argument(s) given, the new signature takes {req}" + \
                    (f"-{total}" if total != req else "")
        if problem:
            out.append(_finding("public_api", "arity-break",
                                f"the call to {owner + '.' if owner else ''}{c.name}() at {rel}:{line} no longer fits "
                                f"the new signature: {problem}", "strong_inference", f"{rel}:{line}",
                                evidence_at=[f"{c.file}:{s['def']}"],
                                basis=f"call site by: {how}; arguments counted by the syntax tree (overloads in "
                                      "other classes and default arguments through annotations are not checked)",
                                derived_by="review.arity", for_symbol=c.symbol))
        elif not shape or not counts or len(overloads) > 1:
            out.append(_finding("public_api", "call-site-of-changed-signature",
                                f"{rel}:{line} calls {c.name}(), whose signature changed (arguments not checked: "
                                + ("overloads" if len(overloads) > 1 else "no syntax for this call") + ")",
                                "weak_inference", f"{rel}:{line}", basis=f"call site by: {how}",
                                derived_by="review.call_sites", for_symbol=c.symbol))
    return out


def _jvm_call_sites(ctx: _Ctx, c: Change, owner: str) -> list[tuple[str, int, str, str | None]]:
    sites: dict[tuple[str, int], tuple[str, str | None]] = {}
    pkg = _jvm_package(ctx.text(c.file) or "") if _suffix(c.file) in (".java", ".kt", ".kts") else None
    if c.node is not None:
        for u, d in _in_edges(ctx, c.node, {"calls"}):
            at = _relocated_at(ctx, u, c.node, d)
            if at and ":" in at and at.rsplit(":", 1)[1].isdigit():
                rel = at.rsplit(":", 1)[0]
                if owner and _suffix(rel) in (".java", ".kt", ".kts") and \
                        not _jvm_sees(ctx.text(rel) or "", pkg, owner):
                    continue   # the name binds to another class of that name in the caller's file
                sites[(rel, int(at.rsplit(":", 1)[1]))] = ("graph call edge", d.get("confidence"))
    if owner:
        rx = re.compile(rf"\b{re.escape(owner)}\s*\.\s*{re.escape(c.name)}\s*\(")
        for rel in ctx.files():
            if _suffix(rel) not in (".java", ".kt", ".kts", ".groovy", ".scala"):
                continue
            t = ctx.text(rel)
            if not t or c.name not in t or owner not in t or not _jvm_sees(t, pkg, owner):
                continue
            for i, ln in enumerate(ctx.code(rel), 1):
                if rx.search(ln):
                    sites.setdefault((rel, i), (f"text `{owner}.{c.name}(` in a file that imports {owner}", "INFERRED"))
    return [(r, ln, how, conf) for (r, ln), (how, conf) in sorted(sites.items())]


_JVM_PACKAGE = re.compile(r"^\s*package\s+([\w.]+)", re.M)


def _jvm_package(text: str) -> str | None:
    m = _JVM_PACKAGE.search(text)
    return m.group(1) if m else None


def _jvm_sees(text: str, pkg: str | None, owner: str) -> bool:
    """Does a JVM source file bind the simple name ``owner`` to the class of package ``pkg`` (explicit import,
    ``pkg.*`` import or the same package)? Without a package (the default package) any file does."""
    if pkg is None:
        return True
    if _jvm_package(text) == pkg:
        return True
    return bool(re.search(rf"^\s*import\s+(?:static\s+)?{re.escape(pkg)}\.(?:{re.escape(owner)}\b|\*)", text, re.M))


def _removed_refs(ctx: _Ctx, c: Change, unknown: list[dict]) -> list[dict]:
    out = []
    name = c.name
    if not name or name.startswith("<module>"):
        return out
    owner_q = c.qual.split("#")[0].rpartition(".")[0] if "." in c.qual else ""
    owner_kind = (ctx.sym(c.file, owner_q, "old") or ctx.sym(c.file, owner_q) or {}).get("kind") if owner_q else None
    if _suffix(c.file) in (".py", ".pyi"):
        from verinoda.guards import _module_of

        mod = _module_of(c.file)
        if owner_kind == "class":
            out += _removed_method_py(ctx, c, unknown)
        for rel in (ctx.py_candidates(c.file, name) if not owner_q else ()):
            if not rel.endswith((".py", ".pyi")):
                continue
            t = ctx.text(rel)
            if not t or name not in t:
                continue
            tree = ctx.pytree(rel)
            if tree is None:
                continue
            imports = ctx._py_imports(rel)
            shadow = ctx.shadow(rel)
            local = {a for a, (m, orig) in imports.items() if orig == name and (m == mod or (
                m and _py_reexport(ctx, m, orig, mod, name)))}
            for n in ast.walk(tree):
                if isinstance(n, ast.ImportFrom) and any(a.name == name for a in n.names) and \
                        imports.get(next(a.asname or a.name for a in n.names if a.name == name), (None,))[0] == mod:
                    out.append(_finding("public_api", "removed-still-used",
                                        f"{c.qual} was removed but {rel}:{n.lineno} still imports it", "statically_verified",
                                        f"{rel}:{n.lineno}", basis="import of the removed name from its module (syntax "
                                        "tree)", derived_by="review.removed_refs", for_symbol=c.symbol))
                elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id in local and \
                        n.id not in shadow.get(id(n), ()):
                    out.append(_finding("public_api", "removed-still-used",
                                        f"{c.qual} was removed but {rel}:{n.lineno} still uses it", "statically_verified",
                                        f"{rel}:{n.lineno}", basis="name bound by an import of the removed name",
                                        derived_by="review.removed_refs", for_symbol=c.symbol))
                elif isinstance(n, ast.Attribute) and n.attr == name and mod and \
                        ctx.py_module_of_expr(rel, n.value) == mod:
                    out.append(_finding("public_api", "removed-still-used",
                                        f"{c.qual} was removed but {rel}:{n.lineno} still uses it", "statically_verified",
                                        f"{rel}:{n.lineno}", basis="module attribute of the removed name (import m, "
                                        "from pkg import m, import pkg.m)", derived_by="review.removed_refs",
                                        for_symbol=c.symbol))
            if rel == c.file:
                for n in ast.walk(tree):
                    if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id == name and \
                            n.id not in shadow.get(id(n), ()) and \
                            name not in ((ctx.facts(rel) or {}).get("symbols") or {}):
                        out.append(_finding("public_api", "removed-still-used",
                                            f"{c.qual} was removed but its own module still uses it at line {n.lineno}",
                                            "strong_inference", f"{rel}:{n.lineno}", basis="name in the same module",
                                            derived_by="review.removed_refs", for_symbol=c.symbol))
    else:
        out += _removed_refs_jvm(ctx, c, owner_q)
    # the name in strings: possible dynamic use
    strings = []
    for rel in ctx.files():
        if _suffix(rel) not in CODE_SUFFIXES:
            continue
        t = ctx.text(rel)
        if not t or name not in t or len(name) < 4:
            continue
        for i, ln in enumerate(t.split("\n"), 1):
            if re.search(rf"[\"'][^\"'\n]*\b{re.escape(name)}\b[^\"'\n]*[\"']", ln):
                strings.append(f"{rel}:{i}")
    if strings:
        unknown.append({"kind": "string_reference", "at": strings[0],
                        "what": f"whether {name} is used dynamically (its name appears in {len(strings)} string(s))",
                        "why": "names in strings (getattr, reflection, registries, configs) are not resolved",
                        "next_step": "read " + ", ".join(strings[:3])})
    return out


def _removed_method_py(ctx: _Ctx, c: Change, unknown: list[dict]) -> list[dict]:
    """A removed Python method still called: ``self.m()`` in its class, ``Cls.m()``, receivers annotated or
    constructed as the class, and the snapshot graph's call edges into the removed method whose call is still on
    the caller's line. When nothing binds, calls of ``.m(`` through receivers of unknown type are an unknown."""
    out: list[dict] = []
    owner_q = c.qual.rpartition(".")[0]
    provides, bases = _py_bases_provide(ctx, c.file, owner_q, c.name)
    if provides is True:
        return out   # a base class still defines it: calls reach the inherited method
    # a base class outside the project (or one not resolved) may provide it: the callers are only possibly broken
    cap = "weak_inference" if provides is None else None
    seen: set[str] = set()
    for rel, call, how, status in _py_call_sites(ctx, c):
        at = f"{rel}:{call.lineno}"
        if at in seen:
            continue
        seen.add(at)
        out.append(_finding("public_api", "removed-still-used",
                            f"{c.qual} was removed but {at} still calls it ({how})",
                            cap or ("strong_inference" if status == "statically_verified" else status), at,
                            basis=f"call bound by: {how}" + (
                                f"; the class has base classes ({', '.join(bases)}) outside the project or not "
                                "resolved, which may still provide it" if cap else
                                f"; its base classes ({', '.join(bases)}) are project classes that do not define it"
                                if bases else ""),
                            derived_by="review.removed_refs", for_symbol=c.symbol))
    if c.node is not None and ctx.g is not None:
        for u, d in _in_edges(ctx, c.node, {"calls"}):
            at = _relocated_at(ctx, u, c.node, d)
            if not at or not at.rsplit(":", 1)[-1].isdigit() or at in seen:
                continue
            rel, ln = at.rsplit(":", 1)[0], int(at.rsplit(":", 1)[1])
            code = ctx.code(rel)
            if ln > len(code) or not re.search(rf"\.\s*{re.escape(c.name)}\s*\(|\b{re.escape(c.name)}\s*\(",
                                               code[ln - 1]):
                continue   # the caller no longer makes that call
            seen.add(at)
            out.append(_finding("public_api", "removed-still-used",
                                f"{c.qual} was removed but {at} still calls it (the snapshot graph's call edge from "
                                f"{_clean_label(ctx.g.label(u))}, {d.get('confidence') or 'no confidence'})",
                                cap or "strong_inference", at, basis="call edge of the last snapshot's graph; the call "
                                "is still on that line", derived_by="review.removed_refs", for_symbol=c.symbol))
    # calls of `.m(` that no rule above bound (a receiver such as `self.repo`): whether they reach the removed
    # method is unknown - also when other callers were bound
    rx = re.compile(rf"\.\s*{re.escape(c.name)}\s*\(")
    loose = []
    for rel in ctx.files():
        if not rel.endswith((".py", ".pyi")):
            continue
        t = ctx.text(rel)
        if not t or f"{c.name}(" not in t.replace(" ", ""):
            continue
        loose += [at for i, ln in enumerate(ctx.code(rel), 1) for at in [f"{rel}:{i}"] if rx.search(ln)
                  and at not in seen]
    if loose:
        unknown.append({"kind": "unresolved_callers", "at": loose[0],
                        "what": f"whether the {len(loose)} other call(s) of .{c.name}( through objects reach the "
                                f"removed {c.qual}" if out else f"whether the {len(loose)} call(s) of .{c.name}( "
                                f"through objects reach the removed {c.qual}",
                        "why": "their receivers' types are not resolved statically",
                        "next_step": "read " + ", ".join(loose[:3])})
    return out


_NO_BASES = {"object", "Protocol", "ABC", "Generic", "typing.Protocol", "typing.Generic", "abc.ABC", "NamedTuple",
             "TypedDict", "Enum", "enum.Enum"}


def _py_bases_provide(ctx: _Ctx, rel: str, cls_qual: str, meth: str, depth: int = 4) -> tuple[bool | None, list[str]]:
    """Does a base class of the Python class ``rel::cls_qual`` (the tree under review) define ``meth``? True when
    one does (in the project), False when every base is a project class - resolved through the file's definitions
    and imports - and none of them (nor their bases) does, None when a base is outside the project or not
    resolved. Also the base classes' names."""
    s = ctx.sym(rel, cls_qual)
    cls = rr.py_def_at(ctx.pytree(rel), s["def"]) if s else None
    if not isinstance(cls, ast.ClassDef):
        return None, []
    names: list[str] = []
    verdict: bool | None = False
    for b in cls.bases:
        expr = b.value if isinstance(b, ast.Subscript) else b
        d = rr.dotted(expr) or "?"
        if d in _NO_BASES:
            continue
        names.append(d)
        tgt = _py_class_unit(ctx, rel, expr)
        if tgt is None:
            verdict = None
            continue
        brel, bqual = tgt
        if f"{bqual}.{meth}" in ((ctx.facts(brel) or {}).get("symbols") or {}):
            return True, names
        sub = _py_bases_provide(ctx, brel, bqual, meth, depth - 1)[0] if depth > 0 else None
        if sub is True:
            return True, names
        if sub is None:
            verdict = None
    return verdict, names


def _py_class_unit(ctx: _Ctx, rel: str, expr: ast.AST) -> tuple[str, str] | None:
    """``(file, qual)`` of the project class an expression of ``rel`` names: a class of the same file, an imported
    one, or a module attribute."""
    syms = (ctx.facts(rel) or {}).get("symbols") or {}
    if isinstance(expr, ast.Name):
        if (syms.get(expr.id) or {}).get("kind") == "class":
            return rel, expr.id
        imp = ctx._py_imports(rel).get(expr.id)
        if imp and imp[0] and imp[1]:
            mrel = ctx.pyix().modules.get(imp[0])
            if mrel and (((ctx.facts(mrel) or {}).get("symbols") or {}).get(imp[1]) or {}).get("kind") == "class":
                return mrel, imp[1]
    elif isinstance(expr, ast.Attribute):
        mod = ctx.py_module_of_expr(rel, expr.value)
        mrel = ctx.pyix().modules.get(mod) if mod else None
        if mrel and (((ctx.facts(mrel) or {}).get("symbols") or {}).get(expr.attr) or {}).get("kind") == "class":
            return mrel, expr.attr
    return None


def _removed_refs_jvm(ctx: _Ctx, c: Change, owner_q: str) -> list[dict]:
    """A removed Java / Kotlin / other member still named: the graph's call edges into it, ``Owner.name(`` and
    ``Owner::name`` (method references) in files that see the owner's class, ``this::name`` and unqualified calls
    in its own file (strong_inference); ``x.name(`` on a receiver of unknown type is weak_inference."""
    if not owner_q and _suffix(c.file) in _JS_SUFFIXES:
        return _removed_refs_js(ctx, c)
    out: list[dict] = []
    name = c.name
    owner = _last(owner_q) if owner_q else ""
    pkg = _jvm_package(ctx.text(c.file, "old") or "") if _suffix(c.file) in (".java", ".kt", ".kts") else None
    left = {q for q in ((ctx.facts(c.file) or {}).get("symbols") or {}) if _last(q) == name}
    seen: set[str] = set()
    if c.node is not None and ctx.g is not None:
        for u, d in _in_edges(ctx, c.node, {"calls"}):
            at = _relocated_at(ctx, u, c.node, d)
            if not at or not at.rsplit(":", 1)[-1].isdigit():
                continue
            rel, ln = at.rsplit(":", 1)[0], int(at.rsplit(":", 1)[1])
            code = ctx.code(rel)
            if at in seen or ln > len(code) or not re.search(rf"\b{re.escape(name)}\b", code[ln - 1]):
                continue
            seen.add(at)
            out.append(_finding("public_api", "removed-still-used", f"{c.qual} was removed but {at} still calls it "
                                f"(graph call edge, {d.get('confidence') or 'no confidence'})", "strong_inference", at,
                                basis="call edge of the last snapshot's graph; the name is still on that line",
                                derived_by="review.removed_refs", for_symbol=c.symbol))
    if owner:
        strong = re.compile(rf"\b{re.escape(owner)}\s*(?:\.\s*{re.escape(name)}\s*\(|::\s*{re.escape(name)}\b)")
        own = re.compile(rf"\bthis\s*::\s*{re.escape(name)}\b|\bthis\s*\.\s*{re.escape(name)}\s*\(|"
                         rf"(?<![\w.:]){re.escape(name)}\s*\(")
        weak = re.compile(rf"\.\s*{re.escape(name)}\s*\(|::\s*{re.escape(name)}\b")
    else:
        strong, own, weak = re.compile(rf"\b{re.escape(name)}\b"), None, None
    n_weak = 0
    for rel in ctx.files():
        if _suffix(rel) not in CODE_SUFFIXES:
            continue
        t = ctx.text(rel)
        if not t or name not in t:
            continue
        mine = rel == c.file
        if not mine and owner and not _jvm_sees(t, pkg, owner):
            continue
        for i, ln in enumerate(ctx.code(rel), 1):
            at = f"{rel}:{i}"
            if at in seen:
                continue
            if strong.search(ln) and (owner or not mine):
                how, status = f"`{owner + '.' if owner else ''}{name}` named in code (text)", "strong_inference"
            elif mine and own is not None and not left and own.search(ln):
                how, status = "called in its own class (text)", "strong_inference"
            elif not mine and weak is not None and owner in t and weak.search(ln) and n_weak < 5:
                n_weak += 1
                how, status = f"`.{name}(` on a receiver whose type is not resolved (text)", "weak_inference"
            else:
                continue
            seen.add(at)
            out.append(_finding("public_api", "removed-still-used", f"{c.qual} was removed but {at} names it: {how}",
                                status, at, basis="text match in code (comments removed)",
                                derived_by="review.removed_refs", for_symbol=c.symbol))
    return out


_JS_SUFFIXES = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts")
_JS_NAMED = re.compile(r"\bimport\s+(?:type\s+)?(?:\w+\s*,\s*)?\{([^}]*)\}\s*from\s*[\"']([^\"'\n]+)[\"']|"
                       r"\b(?:const|let|var)\s*\{([^}]*)\}\s*=\s*require\s*\(\s*[\"']([^\"'\n]+)[\"']\s*\)")
_JS_STAR = re.compile(r"\bimport\s+\*\s+as\s+(\w+)\s+from\s*[\"']([^\"'\n]+)[\"']|"
                      r"\b(?:const|let|var)\s+(\w+)\s*=\s*require\s*\(\s*[\"']([^\"'\n]+)[\"']\s*\)")


def _js_module_is(importer: str, spec: str, target: str) -> bool:
    """Does the import specifier ``spec`` of the file ``importer`` name the module file ``target`` (a relative
    path, with or without its extension, or its directory's index)?"""
    if not spec.startswith("."):
        return False
    parts: list[str] = []
    for p in (PurePosixPath(importer).parent / spec).parts:
        if p == "..":
            parts = parts[:-1]
        elif p != ".":
            parts.append(p)
    got = "/".join(parts)
    stem = str(PurePosixPath(target).with_suffix(""))
    return got in (target, stem) or re.sub(r"\.(?:m|c)?[jt]sx?$", "", got) == stem or \
        (PurePosixPath(target).stem == "index" and got == str(PurePosixPath(target).parent))


def _removed_refs_js(ctx: _Ctx, c: Change) -> list[dict]:
    """A removed JS / TS top-level function still used: an import of it from its module (``import { f }``,
    ``require``), calls of the imported name or through a namespace import (``m.f(``), and unqualified calls in its
    own module (strong_inference); the word elsewhere - a template string, an object key, a field of that name -
    is weak_inference (at most 5)."""
    out: list[dict] = []
    name = c.name
    word = re.compile(rf"(?<![\w$]){re.escape(name)}(?![\w$])")
    n_weak = 0
    for rel in ctx.files():
        if _suffix(rel) not in CODE_SUFFIXES:
            continue
        t = ctx.text(rel)
        if not t or name not in t:
            continue
        code = ctx.code(rel)
        strong_rx: list[re.Pattern] = []
        how = ""
        if rel == c.file:
            strong_rx.append(re.compile(rf"(?<![\w$.]){re.escape(name)}\s*\("))
            how = "called in its own module"
        elif _suffix(rel) in _JS_SUFFIXES:
            for m in _JS_NAMED.finditer(t):
                names, spec = (m.group(1), m.group(2)) if m.group(2) else (m.group(3), m.group(4))
                if not _js_module_is(rel, spec, c.file):
                    continue
                for part in (names or "").split(","):
                    bits = re.split(r"\s+as\s+|\s*:\s*", part.strip())
                    if bits and bits[0].strip() == name:
                        alias = bits[-1].strip() or name
                        line = t[: m.start()].count("\n") + 1
                        out.append(_finding("public_api", "removed-still-used",
                                            f"{c.qual} was removed but {rel}:{line} still imports it",
                                            "strong_inference", f"{rel}:{line}",
                                            basis="import of the removed name from its module (text)",
                                            derived_by="review.removed_refs", for_symbol=c.symbol))
                        strong_rx.append(re.compile(rf"(?<![\w$.]){re.escape(alias)}\s*\("))
                        how = f"`{alias}(` imported from {c.file}"
            for m in _JS_STAR.finditer(t):
                ns, spec = (m.group(1), m.group(2)) if m.group(2) else (m.group(3), m.group(4))
                if _js_module_is(rel, spec, c.file):
                    strong_rx.append(re.compile(rf"(?<![\w$.]){re.escape(ns)}\s*\.\s*{re.escape(name)}\b"))
                    how = f"`{ns}.{name}` through a namespace import of {c.file}"
        seen_here = {f["at"] for f in out}
        for i, ln in enumerate(code, 1):
            at = f"{rel}:{i}"
            if at in seen_here or name not in ln:
                continue
            if any(rx.search(ln) for rx in strong_rx) and not re.match(r"\s*(?:import|export)\b", ln):
                out.append(_finding("public_api", "removed-still-used", f"{c.qual} was removed but {at} still uses it "
                                    f"({how})", "strong_inference", at, basis="text match in code bound through the "
                                    "file's imports (comments removed)", derived_by="review.removed_refs",
                                    for_symbol=c.symbol))
            elif rel != c.file and word.search(ln) and n_weak < 5:
                n_weak += 1
                out.append(_finding("public_api", "removed-still-used", f"{c.qual} was removed and {at} names `{name}` "
                                    "- not an import of it or a call through one (a string, a key or a member of "
                                    "that name?)", "weak_inference", at, basis="text match in code (comments removed)",
                                    derived_by="review.removed_refs", for_symbol=c.symbol))
    return out


def _dedupe(findings: list[dict]) -> list[dict]:
    """One finding per rule, place and text; of duplicates (a class and its method both cover a changed line)
    the one for the innermost symbol is kept."""
    best: dict[tuple, dict] = {}
    for f in findings:
        k = (f["rule"], f["at"], f["finding"])
        if k not in best or len(f.get("for") or "") > len(best[k].get("for") or ""):
            best[k] = f
    return list(best.values())


# package.json scripts that npm runs by itself (install, publish) or that start the program
_NPM_LIFECYCLE = ("start", "prestart", "poststart", "restart", "serve", "preinstall", "install", "postinstall",
                  "prepare", "prepublish", "prepublishOnly", "prepack", "postpack")
# registration manifests: (path pattern, what it is, key prefixes that register code)
_MANIFESTS = [
    (re.compile(r"(^|/)(fabric|quilt)\.mod\.json$"), "mod manifest (Fabric / Quilt)",
     ("entrypoints", "mixins", "accessWidener", "quilt_loader.entrypoints", "quilt_loader.mixin")),
    (re.compile(r"(^|/)META-INF/(neoforge\.)?mods\.toml$"), "mod manifest (Forge / NeoForge)",
     ("mods", "mixins", "dependencies", "modLoader", "loaderVersion")),
    (re.compile(r"(^|/)(paper-)?plugin\.yml$"), "plugin manifest (Bukkit / Paper)",
     ("main", "commands", "permissions", "depend", "softdepend")),
    (re.compile(r"(^|/)[\w.-]+\.mixins\.json$"), "Mixin configuration", ("",)),
    # npm: what node loads (main / bin / exports / module / type) and the lifecycle scripts npm runs by itself;
    # any other script (lint, test, build) is developer tooling (weak_inference, below)
    (re.compile(r"(^|/)package\.json$"), "npm package manifest", ("main", "bin", "exports", "module", "type",
                                                                   *(f"scripts.{s}" for s in _NPM_LIFECYCLE))),
]
# keys of a manifest whose change is reported at weak_inference only (a package.json script other than a lifecycle one)
_MANIFEST_WEAK = {"npm package manifest": ("scripts",)}


def _manifest_kind(rel: str) -> str | None:
    return next((what for rx, what, _p in _MANIFESTS if rx.search(rel)), None)


def _manifest_findings(ctx: _Ctx, changes: list[Change]) -> list[dict]:
    """Changed keys of a registration manifest that say what the runtime loads (entry points, mixins, commands,
    main classes): an entry-point change, not configuration."""
    out = []
    for c in changes:
        if c.kind != "config_key" or not c.qual:
            continue
        row = next(((what, prefixes) for rx, what, prefixes in _MANIFESTS if rx.search(c.file)), None)
        if row is None:
            continue
        strong = any(c.qual == p or c.qual.startswith(p + ".") or not p for p in row[1])
        if not strong and not any(c.qual == p or c.qual.startswith(p + ".") for p in _MANIFEST_WEAK.get(row[0], ())):
            continue
        if c.lines is None:
            ln, side = c.old_lines[0], "base"
            what = "removed: `" + ctx.lines(c.file, "old")[ln - 1].strip()[:100] + "`"
        else:
            ln, side = c.lines[0], None
            new = ctx.lines(c.file)[ln - 1].strip()[:100]
            what = (f"changed: `{ctx.lines(c.file, 'old')[c.old_lines[0] - 1].strip()[:80]}` -> `{new}`"
                    if c.old_lines else f"added: `{new}`")
        out.append(_finding("entry_points", "registration-manifest-changed",
                            f"{c.file} ({row[0]}): {c.qual} {what} - " + (
                                "what the runtime loads or registers at start changed" if strong else
                                "a script run on request (tooling), not at install or start"),
                            "strong_inference" if strong else "weak_inference", f"{c.file}:{ln}",
                            basis="changed key of a registration manifest (line diff; manifest table)",
                            derived_by="review.MANIFESTS", for_symbol=c.symbol, **({"side": side} if side else {})))
    return out


def _config(ctx: _Ctx, changes: list[Change]) -> list[dict]:
    out: list[dict] = []
    seen: set[tuple] = set()

    def add(f: dict) -> None:
        # one finding per place, rule and key: a renamed key is two findings (the old key and the new one)
        key = (f["at"], f["rule"], f.get("key") or f.get("for"), f.get("side"))
        if key not in seen:
            seen.add(key)
            out.append(f)

    cfg_keys = _repo_config_keys(ctx)
    for c in changes:
        if c.test:
            continue
        rel = c.file
        if c.qual is None and c.kind in ("added", "removed"):   # a whole config file added or removed
            add(_finding("config", "config-file", f"config file {rel} was {c.kind}", "strong_inference",
                         f"{rel}:1", basis="the file is a config file by name (architecture_map.CONFIG_FILE_RE)",
                         derived_by="architecture_map.CONFIG_FILE_RE", for_symbol=c.symbol,
                         **({"side": "base"} if c.kind == "removed" else {})))
            continue
        if c.kind == "config_key":
            if _manifest_kind(rel):
                continue   # a registration manifest: reported under entry_points
            ln = (c.lines or c.old_lines)[0]
            side = "new" if c.lines else "old"
            text = ctx.lines(rel, side)[ln - 1].strip()
            old = ctx.lines(rel, "old")[c.old_lines[0] - 1].strip() if c.old_lines else None
            readers = _key_readers(ctx, c.qual, env=PurePosixPath(rel).name.startswith(".env"))
            if not c.lines:
                what = "removed" + (f"; still read at {', '.join(readers[:3])}" if readers else "")
            else:
                what = f"`{old}` -> `{text}`" if old else "added"
            add(_finding("config", "config-file-key", f"config key {c.qual} changed: {what}",
                         "strong_inference", f"{rel}:{ln}", evidence_at=readers[:5],
                         basis="line diff of the config file; readers by literal key search in code (and "
                               "`process.env.KEY` / `import.meta.env.KEY` for .env files)",
                         derived_by="review.config_keys", for_symbol=c.symbol, readers=readers[:10] or None,
                         key=c.qual, **({"side": "base"} if side == "old" else {})))
            continue
        if _suffix(rel) not in CODE_SUFFIXES:
            continue
        code, ocode = ctx.code(rel), ctx.code(rel, "old")
        old_env: dict[str, int] = {}
        for ln in c.old_changed:
            for rx, _lang in ENV_PATTERNS:
                for m in rx.finditer(ocode[ln - 1] if ln <= len(ocode) else ""):
                    k = _call_text(ocode[ln - 1], m.start(), m.end())
                    old_env[k] = old_env.get(k, 0) + 1
        for ln in sorted(c.new_changed):
            if ln > len(code):
                continue
            line = code[ln - 1]
            for rx, _lang in ENV_PATTERNS:
                m = rx.search(line)
                if m:
                    k = _call_text(line, m.start(), m.end())
                    if old_env.get(k, 0) > 0:   # the same read, unchanged, on a line that changed elsewhere
                        old_env[k] -= 1
                        break
                    add(_finding("config", "env-read-on-changed-line",
                                 f"a changed line reads environment variable {m.group(1)}", "strong_inference",
                                 f"{rel}:{ln}", basis="environment-read pattern on the line (the read itself differs "
                                 "from the base version's changed lines)",
                                 derived_by="architecture_map.ENV_PATTERNS", for_symbol=c.symbol, key=m.group(1)))
                    break
        if _suffix(rel) in (".py", ".pyi"):
            for f in _py_config_names(ctx, c):
                add(f)
        else:
            for f in _jvm_config(ctx, c, cfg_keys):
                add(f)
    return out


def _call_text(text: str, a: int, b: int) -> str:
    """The whole call a match at ``text[a:b]`` belongs to (its arguments included, so a changed default of
    ``os.environ.get("X", "100")`` differs), compared without whitespace."""
    open_at = text.find("(", a, b + 1) if "(" in text[a:b] else (b if text[b:b + 1] == "(" else -1)
    if open_at < 0:
        return rr.cond_key(text[a:b])
    depth, j = 0, open_at
    while j < len(text):
        depth += {"(": 1, ")": -1}.get(text[j], 0)
        j += 1
        if depth == 0:
            break
    return rr.cond_key(text[a:j])


def _enclosing_call(text: str, a: int, b: int) -> str:
    """The call ``text[a:b]`` is an argument of, callee name and all its arguments (``define("k", 15, 0, 100)``);
    the operand around it (:func:`_segment`) when it is no call argument on this line."""
    depth, i = 0, a - 1
    while i >= 0:
        ch = text[i]
        if ch in ")]}":
            depth += 1
        elif ch in "([{":
            if depth == 0:
                break
            depth -= 1
        i -= 1
    if i < 0 or text[i] != "(":
        return _segment(text, a, b)
    start = i
    while start > 0 and (text[start - 1].isalnum() or text[start - 1] in "_."):
        start -= 1
    depth, j = 0, i
    while j < len(text):
        depth += {"(": 1, ")": -1}.get(text[j], 0)
        j += 1
        if depth == 0:
            break
    return rr.cond_key(text[start:j])


def _segment(text: str, a: int, b: int) -> str:
    """The operand of ``text[a:b]`` compared between versions: the text around it up to the enclosing
    bracket or a top-level ``&&`` / ``||`` / ``,`` / ``;`` / ``?`` / assignment - so ``x > LIMIT`` changed to
    ``x >= LIMIT`` differs while ``LIMIT`` beside a changed ``&&`` operand does not."""
    def stop(i: int) -> bool:
        two = text[i:i + 2]
        if two in ("&&", "||"):
            return True
        ch = text[i]
        if ch in ",;?":
            return True
        return ch == "=" and text[i - 1:i] not in ("=", "!", "<", ">") and text[i + 1:i + 2] != "="

    i, depth = a, 0
    while i > 0:
        ch = text[i - 1]
        if ch in ")]}":
            depth += 1
        elif ch in "([{":
            if depth == 0:
                break
            depth -= 1
        elif depth == 0 and (stop(i - 1) or (i >= 2 and text[i - 2:i] in ("&&", "||"))):
            break
        i -= 1
    j, depth = b, 0
    while j < len(text):
        ch = text[j]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            if depth == 0:
                break
            depth -= 1
        elif depth == 0 and stop(j):
            break
        j += 1
    return rr.cond_key(text[i:j])


def _py_config_names(ctx: _Ctx, c: Change) -> list[dict]:
    """Names read on changed lines that are bound to an environment read or live in a config module, where the
    expression reading them changed (a parameter renamed, or another operand changed beside them, is not)."""
    out = []
    tree = ctx.pytree(c.file)
    if tree is None or not c.new_changed or c.kind == "module_statement":
        return out
    imports = ctx._py_imports(c.file)
    code, ocode = ctx.code(c.file), ctx.code(c.file, "old")
    np_, op_ = _params_of(ctx, c, "new"), _params_of(ctx, c, "old")
    old_ctx: dict[str, int] = {}
    otree = ctx.pytree(c.file, "old") if c.old_changed else None
    for n in ast.walk(otree) if otree is not None else ():
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.lineno in c.old_changed and \
                n.lineno <= len(ocode):
            k = n.id + "@" + rr.alpha(_segment(ocode[n.lineno - 1], n.col_offset, n.end_col_offset or n.col_offset),
                                      op_)
            old_ctx[k] = old_ctx.get(k, 0) + 1
    for n in sorted((n for n in ast.walk(tree) if isinstance(n, ast.Name)), key=lambda n: (n.lineno, n.col_offset)):
        if not (isinstance(n.ctx, ast.Load) and n.lineno in c.new_changed) or n.lineno > len(code):
            continue
        where = None
        if n.id in imports and imports[n.id][1]:
            mrel = ctx.pyix().modules.get(imports[n.id][0])
            if mrel:
                where = (mrel, imports[n.id][1])
        else:
            mod = (ctx.facts(c.file) or {}).get("module") or {}
            if f"ASSIGN:{n.id}" in mod:
                where = (c.file, n.id)
        if where is None:
            continue
        mrel, name = where
        m = ((ctx.facts(mrel) or {}).get("module") or {}).get(f"ASSIGN:{name}")
        if m is None:
            continue
        k = n.id + "@" + rr.alpha(_segment(code[n.lineno - 1], n.col_offset, n.end_col_offset or n.col_offset), np_)
        if old_ctx.get(k, 0) > 0:
            old_ctx[k] -= 1
            continue
        def_line = m["start"]
        text = " ".join(ctx.code(mrel)[def_line - 1: m["end"]])
        env = next((mm.group(1) for rx, _ in ENV_PATTERNS for mm in [rx.search(text)] if mm), None)
        if env or CONFIG_FILE_RE.search(mrel):
            out.append(_finding("config", "config-name-on-changed-line",
                                f"a changed line reads {name}" + (f", bound to environment variable {env}" if env else
                                                                  f", a value of the config module {mrel}")
                                + f" ({mrel}:{def_line})", "strong_inference", f"{c.file}:{n.lineno}",
                                evidence_at=[f"{mrel}:{def_line}"], basis="the name's binding followed through the "
                                "file's imports (syntax tree); the binding line read by the environment pattern; the "
                                "expression around the name differs from the base version's",
                                derived_by="architecture_map.ENV_PATTERNS" if env else "architecture_map.CONFIG_FILE_RE",
                                for_symbol=c.symbol, key=env or name))
    return out


def _repo_config_keys(ctx: _Ctx) -> dict[str, list[str]]:
    """Last key segment -> ["file:line"] over the repository's config files (not tests)."""
    keys: dict[str, list[str]] = {}
    for rel in ctx.files():
        if _suffix(rel) not in rr.CONFIG_SUFFIXES or _is_test(rel) or "/lang/" in rel or "/models/" in rel:
            continue
        if _suffix(rel) == ".json" and not CONFIG_FILE_RE.search(rel):
            continue
        for ln, k in rr.config_keys(rel, ctx.lines(rel)):
            keys.setdefault(k.rpartition(".")[2], []).append(f"{rel}:{ln}")
    return keys


def _key_readers(ctx: _Ctx, key: str, *, env: bool = False) -> list[str]:
    """Code lines that read a config key: the key as a quoted literal (``getenv("K")``, ``cfg["k"]``) and, for the
    keys of a .env file, the attribute forms of JS / TS (``process.env.K``, ``import.meta.env.K``)."""
    last = key.rpartition(".")[2]
    if len(last) < 3:
        return []
    attr = rf"|\b(?:process\.env|import\.meta\.env)\s*\.\s*{re.escape(last)}\b" if env else ""
    rx = re.compile(rf"[\"']{re.escape(last)}[\"']" + attr)
    out = []
    for rel in ctx.files():
        if _suffix(rel) not in CODE_SUFFIXES or _is_test(rel):
            continue
        t = ctx.text(rel)
        if not t or last not in t:
            continue
        for i, ln in enumerate(ctx.code(rel), 1):
            if rx.search(ln):
                out.append(f"{rel}:{i}")
    return out


_JVM_FIELD = re.compile(r"^\s*(?:@\w+\s+)*(?:(?:public|private|protected|static|final|const|val|var|internal)\s+)+"
                        r"(?:[\w<>\[\],.?]+\s+)?([A-Za-z_]\w*)\s*(?::\s*[\w<>?.]+\s*)?=(?!=)")
_CONFIG_CLASS = re.compile(r"(Config|Configuration|Settings|Defaults|Options)$")


def _jvm_config(ctx: _Ctx, c: Change, cfg_keys: dict[str, list[str]]) -> list[dict]:
    out = []
    code, ocode = ctx.code(c.file), ctx.code(c.file, "old")
    old_segs: dict[str, int] = {}
    for ln in c.old_changed:
        if ln <= len(ocode):
            for m in rr.QUOTED_KEY.finditer(ocode[ln - 1]):   # a key: the whole call it is an argument of
                k = _enclosing_call(ocode[ln - 1], m.start(), m.end())
                old_segs[k] = old_segs.get(k, 0) + 1
            for m in rr.CONFIG_READ_JVM.finditer(ocode[ln - 1]):
                k = _segment(ocode[ln - 1], m.start(), m.end())
                old_segs[k] = old_segs.get(k, 0) + 1

    def unchanged(line: str, m) -> bool:
        if m.re is rr.QUOTED_KEY:
            k = _enclosing_call(line, m.start(), m.end())
        else:
            k = _segment(line, m.start(), m.end())
        if old_segs.get(k, 0) > 0:
            old_segs[k] -= 1
            return True
        return False

    for side, lines, src in (("new", c.new_changed, code), ("old", c.old_changed, ocode)):
        for ln in sorted(lines):
            if ln > len(src):
                continue
            line = src[ln - 1]
            for m in rr.QUOTED_KEY.finditer(line):
                key = m.group(1) or m.group(2)
                if rr.NBT_RECEIVER.search(line[:m.start()]):
                    continue   # a saved-data (NBT) key: persistence reads it, not the config rules
                in_files = cfg_keys.get(key.rpartition(".")[2], [])
                if not (in_files or rr.CONFIG_CALL.search(line)):
                    continue
                if " " in key or key.count(":") or key.endswith((".png", ".json")):
                    continue
                at = f"{c.file}:{ln}"
                if side == "new" and unchanged(line, m):
                    continue
                if side == "old":
                    # the key left a changed line: report at the new line of the same statement when there is one
                    new_ln = min(c.new_changed) if c.new_changed else None
                    at = f"{c.file}:{new_ln}" if new_ln else at
                    if rr.QUOTED_KEY.search(code[new_ln - 1] if new_ln and new_ln <= len(code) else "") and \
                            key in (code[new_ln - 1] if new_ln else ""):
                        continue
                out.append(_finding("config", "config-key-on-changed-line",
                                    (f"config key `{key}` on a changed line" if side == "new" else
                                     f"config key `{key}` is no longer read on the changed line (base line {ln})")
                                    + (f"; the key is in {', '.join(in_files[:2])}" if in_files else ""),
                                    "strong_inference", at, evidence_at=in_files[:3],
                                    basis="quoted key in a config call or present in a config file of the repository",
                                    derived_by="review.CONFIG_CALL/QUOTED_KEY", for_symbol=c.symbol, key=key))
        if side == "old":
            break
    for ln in sorted(c.new_changed):
        if ln > len(code):
            continue
        for m in rr.CONFIG_READ_JVM.finditer(code[ln - 1]):
            cls, member = m.group(1), m.group(2)
            if member in ("get", "class", "INSTANCE") or member in rr.CONFIG_VERBS or not cls.endswith("Config"):
                continue
            if unchanged(code[ln - 1], m):
                continue
            where = _jvm_member_def(ctx, cls, member)
            out.append(_finding("config", "config-read-on-changed-line",
                                f"a changed line reads the config value {cls}.{member}"
                                + (f" (defined at {where})" if where else ""), "strong_inference", f"{c.file}:{ln}",
                                evidence_at=[where] if where else [], basis="config class read (text rule); the "
                                "expression around it differs from the base version's",
                                derived_by="review.CONFIG_READ_JVM", for_symbol=c.symbol, key=f"{cls}.{member}"))
    # a field of a config class (its default values) initialised on a changed line of the class's own lines
    cls_name = _last(c.qual) if c.qual and (ctx.sym(c.file, c.qual) or {}).get("kind") == "class" else ""
    if cls_name and _CONFIG_CLASS.search(cls_name):
        for ln in sorted(c.new_changed):
            if ln > len(code):
                continue
            fm = _JVM_FIELD.match(code[ln - 1])
            if not fm:
                continue
            field_name = fm.group(1)
            readers = _field_readers(ctx, c.file, cls_name, field_name, ln)
            readers += [r for r in _record_component_readers(ctx, c, cls_name, field_name, ln) if r not in readers]
            out.append(_finding("config", "config-default-changed",
                                f"the value of {cls_name}.{field_name} (a field of a config class) changed: "
                                f"`{ctx.lines(c.file)[ln - 1].strip()[:100]}`"
                                + (f"; read at {', '.join(readers[:3])}" if readers else ""), "strong_inference",
                                f"{c.file}:{ln}", evidence_at=readers[:5], basis="field initialiser of a class named "
                                "like a configuration, on a changed line (text rule); readers by name",
                                derived_by="review.config_class_fields", for_symbol=c.symbol,
                                key=f"{cls_name}.{field_name}"))
    return out


def _record_component_readers(ctx: _Ctx, c: Change, cls: str, field_name: str, line: int) -> list[str]:
    """A config field initialised with its own record's constructor (``DEFAULTS = new Cfg(8, 64)`` in ``record
    Cfg(int a, int maxDistance)``): the reads, in the files that see the class, of the components whose argument
    changed (``.maxDistance()``) - the values the changed default feeds."""
    new_args = rr.call_args(ctx.code(c.file)[line - 1], cls)
    header = re.search(rf"\brecord\s+{re.escape(cls)}\s*(?:<[^>]*>)?\s*\(([^)]*)\)", "\n".join(ctx.code(c.file)))
    if not new_args or header is None:
        return []
    old_args = None
    for t in ctx.code(c.file, "old"):
        m = _JVM_FIELD.match(t)
        if m and m.group(1) == field_name:
            old_args = rr.call_args(t, cls)
            break
    comps = [p.split()[-1] for p in rr.split_top(" ".join(header.group(1).split()), (",",)) if p.split()]
    changed = [comps[i] for i in range(min(len(comps), len(new_args)))
               if old_args is None or i >= len(old_args) or _norm_code(old_args[i]) != _norm_code(new_args[i])]
    pkg = _jvm_package(ctx.text(c.file) or "")
    out: list[str] = []
    for comp in changed:
        rx = re.compile(rf"\.\s*{re.escape(comp)}\s*\(\s*\)")
        for f in ctx.files():
            if _suffix(f) not in (".java", ".kt", ".kts"):
                continue
            t = ctx.text(f)
            if not t or comp not in t or (f != c.file and not _jvm_sees(t, pkg, cls)):
                continue
            out += [f"{f}:{i}" for i, ln in enumerate(ctx.code(f), 1) if rx.search(ln) and f"{f}:{i}" not in out]
    return out


def _field_readers(ctx: _Ctx, rel: str, cls: str, field_name: str, decl_line: int) -> list[str]:
    """Where a static field is read: by its bare name in its own file, as ``Cls.FIELD`` elsewhere."""
    out = []
    bare = re.compile(rf"(?<![\w.]){re.escape(field_name)}\b")
    qual = re.compile(rf"\b{re.escape(cls)}\s*\.\s*{re.escape(field_name)}\b")
    for f in ctx.files():
        if _suffix(f) not in (".java", ".kt", ".kts"):
            continue
        t = ctx.text(f)
        if not t or field_name not in t:
            continue
        for i, ln in enumerate(ctx.code(f), 1):
            if (f == rel and i != decl_line and bare.search(ln)) or (f != rel and qual.search(ln)):
                out.append(f"{f}:{i}")
    return out


def _jvm_member_def(ctx: _Ctx, cls: str, member: str) -> str | None:
    rx = re.compile(rf"\b{re.escape(member)}\b\s*(=|\()")
    for rel in ctx.files():
        if PurePosixPath(rel).stem != cls:
            continue
        for i, ln in enumerate(ctx.code(rel), 1):
            if rx.search(ln):
                return f"{rel}:{i}"
    return None


# -- entries and hot paths ------------------------------------------------------------------------------

def _entry_info(ctx: _Ctx, node: str | None, unit: tuple[str, str] | None) -> dict | None:
    """Why a unit is an entry point: map heuristics (for a definition the snapshot does not have yet, the same
    heuristics read from its syntax tree), registrations by method reference, input overrides."""
    reasons, at, kind = [], None, None
    if node is not None:
        reasons += entry_reasons(ctx.g, node)
    elif unit is not None and _suffix(unit[0]) in (".py", ".pyi") and not _is_test(unit[0]):
        reasons += _py_entry_reasons(ctx, unit)
    if unit is not None:
        for r in ctx.registered(*unit):
            if r["entry"]:
                reasons.append(f"{r['what']} registered at {r['at']} (text, inference)")
                at, kind = r["at"], r["entry"]
        if _suffix(unit[0]) in (".java", ".kt") and _last(unit[1]) in rr.INPUT_OVERRIDES:
            reasons.append("a game callback on player input, by name (inference)")
            kind = kind or "player input"
    if not reasons:
        return None
    strong = any("decorator" in r or "registered" in r for r in reasons) or len(reasons) >= 2
    return {"reasons": reasons, "at": at, "kind": kind or "entry", "status": "strong_inference" if strong
            else "weak_inference"}


def _py_entry_reasons(ctx: _Ctx, unit: tuple[str, str]) -> list[str]:
    """The architecture map's entry heuristics (decorator, name, entry module without callers) applied to a
    Python definition read from the tree under review (it has no graph node yet)."""
    from verinoda.architecture_map import ENTRY_DECORATOR_RE, ENTRY_FILE_RE, ENTRY_NAME_RE

    rel, qual = unit
    s = ctx.sym(rel, qual)
    if s is None or s.get("kind") != "def" or _nested_in_function(ctx, unit):
        return []
    name = _last(qual)
    out = []
    head = "\n".join(ctx.lines(rel)[max(0, s["start"] - 1): s["def"]])
    if ENTRY_DECORATOR_RE.search(head):
        out.append("route/command decorator")
    if ENTRY_NAME_RE.search(name):
        out.append("entry-like name")
    if ENTRY_FILE_RE.search(rel) and not name.startswith("_") and "." not in qual and \
            not [x for x in _py_call_sites(ctx, Change(rel, qual, "added")) if not _is_test(x[0])]:
        out.append("public callable in entry module with no in-project callers")
    return out


_GAME_IMPORT = re.compile(r"^\s*import\s+(?:static\s+)?(?:net\.minecraft|net\.neoforged|net\.minecraftforge|"
                          r"net\.fabricmc|com\.mojang)\.", re.M)


def _hot_info(ctx: _Ctx, node: str | None, unit: tuple[str, str]) -> dict | None:
    """Why a unit runs on a hot path, with the status that evidence supports: a registration found by text
    (a tick / render registration, a tick event handler) is strong_inference; a method name the game calls
    every tick, in a file that imports a game framework, or a request handler, only weak_inference."""
    for r in ctx.registered(*unit):
        if r["hot"]:
            return {"why": f"{r['what']} registered at {r['at']}", "at": r["at"],
                    "basis": "registration by method reference (text search, inference)", "status": "strong_inference"}
    rel, qual = unit
    name = _last(qual)
    s = ctx.sym(rel, qual)
    if s and _suffix(rel) in (".java", ".kt"):
        head = "\n".join(ctx.lines(rel)[max(0, s["start"] - 1): s["def"] + 1])
        m = rr.EVENT_PARAM.search(head)
        if m:
            return {"why": f"@SubscribeEvent for {m.group(1)}", "at": f"{rel}:{s['start']}",
                    "basis": "event type in the handler's signature (text)", "status": "strong_inference"}
    if _suffix(rel) in (".java", ".kt", ".kts") and name in rr.HOT_OVERRIDES and \
            _GAME_IMPORT.search(ctx.text(rel) or ""):
        return {"why": f"{name}() is a method the game calls every tick or frame (by name, in a file that imports "
                       "the game)", "at": None, "basis": "method name table (inference)", "status": "weak_inference"}
    if node is not None and _suffix(rel) in (".py", ".pyi"):
        e = entry_reasons(ctx.g, node)
        if any("decorator" in r or "entry-like name" in r for r in e):
            return {"why": "request/command handler (" + "; ".join(e) + ")", "at": f"{rel}:{s['def']}" if s else None,
                    "basis": "entry-point heuristics of the architecture map", "status": "weak_inference"}
    return None


def _nested_in_function(ctx: _Ctx, unit: tuple[str, str]) -> bool:
    rel, qual = unit
    owner = qual.split("#")[0].rpartition(".")[0]
    return bool(owner) and (ctx.sym(rel, owner) or {}).get("kind") == "def"


def _entries(ctx: _Ctx, changes: list[Change], walk: dict) -> list[dict]:
    out: dict[str, dict] = {}
    g = ctx.g
    for c in _code_units(changes) + [c for c in changes if c.kind in ("removed", "module_statement") and not c.test]:
        unit = (c.file, c.qual) if c.qual and c.kind not in ("removed", "module_statement") else None
        if unit and _nested_in_function(ctx, unit):
            continue   # a function defined inside another one is not called from outside it
        info = _entry_info(ctx, c.node, unit)
        if info and c.lines:
            at = f"{c.file}:{c.def_line or c.lines[0]}"
            out.setdefault(at, _finding("entry_points", "changed-entry",
                                        f"{c.name} is itself an entry point ({info['kind']}): "
                                        + "; ".join(info["reasons"]), info["status"], at,
                                        evidence_at=[info["at"]] if info["at"] else [], basis="entry heuristics",
                                        derived_by="architecture_map.entry_reasons + review.REGISTRATIONS",
                                        for_symbol=c.symbol, entry_kind=info["kind"]))
    if g is None:
        return list(out.values())
    for n, info in walk.items():
        if info["dist"] == 0 or _is_test(g.file(n) or ""):
            continue
        unit = ctx.unit_of(n)
        if unit and _nested_in_function(ctx, unit):
            continue
        e = _entry_info(ctx, n, unit)
        if not e:
            continue
        s = ctx.sym(*unit) if unit else None
        at = f"{g.file(n)}:{s['def'] if s else g.line(n)}"
        chain = _chain(ctx, walk, n)
        status = e["status"]
        seed = info["seed"]
        path = " -> ".join([_clean_label(g.label(n))] + [h["to"] for h in chain])
        out.setdefault(at, _finding("entry_points", "entry-reaches-change",
                                    f"{_clean_label(g.label(n))} ({e['kind']}: {'; '.join(e['reasons'])}) reaches the "
                                    f"changed {seed.name} in {info['dist']} call(s): {path}", status, at,
                                    evidence_at=[e["at"]] + [h["at"] for h in chain[:1]] if e["at"] else
                                    [h["at"] for h in chain[:1]], chain=chain, basis="reverse call graph from the "
                                    "change (depth 3) + entry heuristics", derived_by="architecture_map.entry_reasons",
                                    for_symbol=seed.symbol, entry_kind=e["kind"], distance=info["dist"]))
    return sorted(out.values(), key=lambda f: (rr.rank(f["status"]), f.get("distance", 0), f["at"]))


def _hot_paths(ctx: _Ctx, changes: list[Change], walk: dict) -> dict[str, dict]:
    """changed symbol -> why it runs on a hot path (itself, or a hot caller within 2 hops)."""
    out = {}
    for c in _code_units(changes):
        unit = (c.file, c.qual)
        h = _hot_info(ctx, c.node, unit)
        if h is None and c.node is not None:
            for n, info in walk.items():
                if info["seed"] is c and 0 < info["dist"] <= 2:
                    u = ctx.unit_of(n)
                    h2 = _hot_info(ctx, n, u) if u else None
                    if h2:
                        h = {**h2, "why": f"called from {_last(u[1])}, {h2['why']}"}
                        break
        if h:
            out[c.symbol] = h
    return out


# -- tests -------------------------------------------------------------------------------------------------

def _test_id(ctx: _Ctx, n: str) -> str | None:
    from verinoda.runtime.trace import _is_test_function, pytest_id

    g = ctx.g
    f = g.file(n) or ""
    if f.endswith(".py"):
        return pytest_id(g, n) if _is_test_function(g, n) else None
    if not _is_test(f) or g.G.nodes[n].get("_callable_class"):
        return None
    name = _clean_label(g.label(n))
    src = g.source(n, max_lines=3)
    if name.lower().startswith("test") or (src and re.search(r"@(Test|GameTest|ParameterizedTest)\b", src[2])):
        return f"{f}::{name}"
    return None


def _static_tests(ctx: _Ctx, changes: list[Change]) -> tuple[dict[str, dict], dict[str, list[str]]]:
    """(test id -> {distance, reaches, basis?}, changed symbol -> [test ids]) over calls/uses/references, depth 3,
    through non-test code (the rule of runtime.trace.select_tests). A function nested in another one is reached
    through its enclosing function; a definition the snapshot does not have (added, renamed) through its callers
    in the changed files; test functions in the changed test files that call a changed symbol are read from their
    syntax trees. A removed symbol's tests are the ones that called it."""
    tests: dict[str, dict] = {}
    per: dict[str, list[str]] = {}
    g = ctx.g
    changed_tests = [f for f in ctx.base_texts if _is_test(f) and f.endswith(".py") and ctx.text(f) is not None]

    def add(tid: str, dist: int, c: Change, basis: str | None = None) -> None:
        t = tests.setdefault(tid, {"distance": dist, "reaches": []})
        t["distance"] = min(t["distance"], dist)
        if basis and "basis" not in t:
            t["basis"] = basis
        if c.symbol not in t["reaches"]:
            t["reaches"].append(c.symbol)
        if tid not in per[c.symbol]:
            per[c.symbol].append(tid)

    for c in changes:
        if c.test or not c.qual or c.kind in ("config_key", "file_only", "module_statement"):
            continue
        per.setdefault(c.symbol, [])
        py = _suffix(c.file) in (".py", ".pyi")
        seeds: list[tuple[str, int]] = [(c.node, 0)] if c.node is not None else []
        enc = _enclosing_def(ctx, c)
        if enc is not None and g is not None:
            en = ctx.node_of(c.file, enc)
            if en is not None:
                seeds.append((en, 0))
        if py and c.node is None and c.kind != "removed":
            for _unit, u, _fn, _calls in _py_callers(ctx, (c.file, c.qual), None):
                if u is not None:
                    seeds.append((u, 1))
        if py and changed_tests and c.kind != "removed":
            for srel, call, _how, _st in _py_call_sites(ctx, Change(c.file, c.qual, c.kind), files=changed_tests):
                q = ctx.label_at(srel, call.lineno)
                if q and _last(q).startswith(("test", "Test")):
                    add(f"{srel}::{q.replace('.', '::')}", 1, c, basis="a test in the changed files calls it (syntax "
                                                                       "tree of the tree under review)")
        if g is None:
            continue
        frontier = {n for n, d in seeds if d == 0}
        seen = set(frontier)
        for n, d in seeds:
            if d == 1 and n not in seen:
                seen.add(n)
                tid = _test_id(ctx, n)
                if tid:
                    add(tid, 1, c)
        later = {n for n, d in seeds if d == 1}
        for dist in range(1, DEPTH + 1):
            nxt = set()
            for v in frontier | (later if dist == 2 else set()):
                for u, _d in _in_edges(ctx, v, {"calls", "uses", "references"}):
                    if u in seen:
                        continue
                    seen.add(u)
                    tid = _test_id(ctx, u)
                    if tid:
                        add(tid, dist, c)
                    elif g.file(u) and not _is_test(g.file(u)):
                        nxt.add(u)
            frontier = nxt
    return tests, per


def _enclosing_def(ctx: _Ctx, c: Change) -> str | None:
    """The qualified name of the function a nested function is defined in (None when ``c`` is not nested)."""
    if not c.qual or "." not in c.qual:
        return None
    owner = c.qual.split("#")[0].rpartition(".")[0]
    while owner:
        if (ctx.sym(c.file, owner) or ctx.sym(c.file, owner, "old") or {}).get("kind") == "def":
            outer = owner.rpartition(".")[0]
            if not outer or (ctx.sym(c.file, outer) or {}).get("kind") != "def":
                return owner
            owner = outer
        else:
            return None
    return None


def _observed_tests(ctx: _Ctx, changes: list[Change]) -> dict | None:
    """Tests the runtime tracer's latest complete run saw reaching the changed Python symbols."""
    if ctx.store is None:
        return None
    try:
        from verinoda.runtime import trace

        run = ctx.store.one("SELECT * FROM runtime_runs WHERE complete = 1 ORDER BY created_at DESC, rowid DESC LIMIT 1")
    except Exception:  # noqa: BLE001 - no runtime tables or data
        return None
    if not run:
        return None
    reached: dict[str, list[str]] = {}
    for c in changes:
        if c.test or not c.qual or not c.file.endswith(".py") or c.kind not in ("body", "signature"):
            continue
        hits = trace.tests_reaching(ctx.store, run["id"], c.file, c.qual)
        reached[c.symbol] = sorted(hits)
    header = run.get("header") or {}
    return {"run": run["id"], "commit": run.get("commit_sha"), "reached": reached,
            "tests_run": sorted(((header.get("tests_outcomes") or {}) or {}).keys()),
            "note": "observed in that run, at that commit (before this change when the file changed since): "
                    "run-scoped, never 'always'"}


# -- read_first ----------------------------------------------------------------------------------------

def _read_first(ctx: _Ctx, changes: list[Change], dependents: list[dict], concerns: dict[str, list[dict]],
                max_chars: int) -> tuple[list[dict], dict]:
    items: list[tuple[str, int, int, str, str]] = []   # (file, start, end, side, why)
    renamed = {(c.file, c.renamed_from) for c in changes if c.renamed_from}
    for c in changes:
        if c.renamed_from and c.lines:   # a rename: the new header, not the unchanged body twice
            d = c.def_line or c.lines[0]
            items.append((c.file, max(c.lines[0], d - 1), d + 1, "new", f"renamed from {_last(c.renamed_from)} "
                          "(same body)"))
            continue
        if c.kind == "removed" and (c.file, c.qual) in renamed:
            continue
        if c.kind in ("added", "body", "signature", "module_statement", "config_key", "file_only") and c.lines:
            a, b = c.lines
            if b - a > 60 and c.new_changed:   # a long definition: the changed lines with context
                a, b = max(a, min(c.new_changed) - 3), min(b, max(c.new_changed) + 3)
            why = f"changed ({c.kind})"
            if b - a > 80:
                b, why = a + 80, why + ", first 80 lines"
            items.append((c.file, a, b, "new", why))
        elif c.kind == "removed" and c.old_lines:
            items.append((c.file, c.old_lines[0], c.old_lines[1], "old", "removed (base version)"))
    for d in dependents:
        if d.get("distance") == 1 and d.get("call_at"):
            f, _, ln = d["call_at"].rpartition(":")
            if ln.isdigit():
                items.append((f, max(1, int(ln) - 3), int(ln) + 3, "new", f"call site of {d['to']}"))
    order = sorted((f for fs in concerns.values() for f in fs), key=lambda f: rr.rank(f["status"]))
    for f in order:
        for at in [f["at"], *f.get("evidence_at", [])[:2]]:
            if not at or ":" not in at or not at.rsplit(":", 1)[1].isdigit():
                continue
            rel, ln = at.rsplit(":", 1)[0], int(at.rsplit(":", 1)[1])
            side = "old" if f.get("side") == "base" and at == f["at"] else "new"
            items.append((rel, max(1, ln - 1), ln + 1, side, f"{f['concern']}: {f['rule']}"))
    # merge overlapping ranges of one file and side, keep the first reason
    merged: list[list] = []
    for rel, a, b, side, why in items:
        for m in merged:
            if m[0] == rel and m[3] == side and a <= m[2] + 1 and b >= m[1] - 1:
                m[1], m[2] = min(m[1], a), max(m[2], b)
                break
        else:
            merged.append([rel, a, b, side, why])
    out, more, used = [], [], 0
    for rel, a, b, side, why in merged:
        lines = ctx.lines(rel, side)
        b = min(b, len(lines))
        if a > b:
            continue
        chars = sum(len(x) + 1 for x in lines[a - 1:b])
        at = f"{rel}:{a}-{b}" if b > a else f"{rel}:{a}"
        rec = {"at": at + (" (base)" if side == "old" else ""), "why": why, "chars": chars}
        if used + chars <= max_chars:
            out.append(rec)
            used += chars
        else:
            more.append(rec)
    return out, {"max_chars": max_chars, "used_chars": used, "truncated": bool(more),
                 "more": more[:20], "more_total": len(more)}


# -- the review --------------------------------------------------------------------------------------------

def review(repo: Path, *, store=None, graph=None, base: str | None = None, staged: bool = False,
           targets: list[str] | None = None, change: str | None = None, concerns: list[str] | None = None,
           run_tests: bool = False, observe: bool = False, max_chars: int = DEFAULT_MAX_CHARS,
           record: bool = True) -> dict:
    """Review the working tree against ``base`` (default HEAD), the staged changes, or a planned change
    (``targets`` as ``file`` or ``file::Qual.name`` with ``change`` body | signature | remove)."""
    from verinoda import index, treestate

    t0 = time.perf_counter()
    repo = Path(repo).resolve()
    want = [c for c in (concerns or CONCERNS)]
    bad = [c for c in want if c not in CONCERNS]
    if bad:
        raise ValueError(f"unknown concern(s) {bad}; choose from {', '.join(CONCERNS)}")
    if targets and (base or staged):
        raise ValueError("a planned change (targets) is reviewed on the current code: give no base / staged")
    if change and not targets:
        raise ValueError("change needs targets (file or file::Qual.name)")
    if change and change not in PLANNED_KINDS:
        raise ValueError(f"change must be one of {', '.join(PLANNED_KINDS)}")
    g = graph if graph is not None else index.load(repo)
    skipped: list[dict] = []
    base_info = None
    if targets:
        diffs: list[FileDiff] = []
    else:
        base_sha = treestate.resolve_commit(repo, base or "HEAD")
        base_info = {"ref": treestate.check_ref(base or "HEAD"), "commit": base_sha}
        diffs, skipped = (_diff_staged if staged else _diff_worktree)(repo, base_sha)
    base_texts = {fd.rel: fd.old for fd in diffs}
    ctx = _Ctx(repo, g, store, base_texts)
    if staged and not targets:
        # the staged tree for every file: files outside the diff whose working copy differs from the index are
        # read from the index, and the index lists the files
        ctx.index_files, over = _staged_tree(repo)
        ctx.overrides = {p: t for p, t in over.items() if p not in base_texts}
        ctx.staged = {"base": base_sha, "paths": [fd.rel for fd in diffs if fd.new is not None],
                      "deleted": [fd.rel for fd in diffs if fd.new is None],
                      "skipped": [s["file"] for s in skipped]}
    for fd in diffs:   # the tree under review: the staged contents, or the working tree as read
        ctx._text[fd.rel] = fd.new
    changes: list[Change] = []
    files_info = []
    unknown: list[dict] = []
    if targets:
        changes, planned_unknown = _planned(ctx, targets, change or "body")
        unknown += planned_unknown
    else:
        for fd in diffs:
            cs, info = _classify(ctx, fd)
            changes += cs
            files_info.append(info)
            if info.get("kind") == "file_only":
                unknown.append({"kind": "unsupported_file", "at": fd.rel,
                                "what": f"which definitions of {fd.rel} changed", "why": info.get("why"),
                                "next_step": "read the diff of this file yourself"})
    for c in changes:
        if c.qual and c.kind not in ("config_key", "file_only") and not c.qual.startswith("<module>"):
            c.node = ctx.node_of(c.file, c.qual)
    _mark_renames(ctx, changes)
    # dependents by change kind (a function nested in another one: the callers of the enclosing function)
    walk = {}
    if g is not None:
        seeds_body = [(c.node, c) for c in changes if c.node and c.kind in ("body", "added") and not c.test]
        seeds_body += [(n, c) for c in changes if c.kind in ("body", "added", "signature") and not c.test
                       for n in [ctx.node_of(c.file, _enclosing_def(ctx, c) or "")] if n is not None]
        seeds_sig = [(c.node, c) for c in changes if c.node and c.kind == "signature" and not c.test]
        seeds_rm = [(c.node, c) for c in changes if c.node and c.kind == "removed" and not c.test]
        walk = _walk_back(ctx, seeds_body, _BODY_RELS)
        for n, info in _walk_back(ctx, seeds_sig, _SIG_RELS).items():
            walk.setdefault(n, info)
        for n, info in _walk_back(ctx, seeds_rm, _REMOVE_RELS, depth=1).items():
            walk.setdefault(n, info)
    dependents, dep_total = _dependents(ctx, walk, changes)
    readers = _binding_readers(ctx, changes)
    found: dict[str, list[dict]] = {k: [] for k in CONCERNS}
    hot = _hot_paths(ctx, changes, walk) if g is not None else {}
    if "persistence" in want:
        found["persistence"] = _persistence(ctx, changes, walk)
    if "security" in want and not targets:
        found["security"] = _security(ctx, changes)
    if "performance" in want:
        found["performance"] = _performance(ctx, changes, hot, unknown)
    if "public_api" in want:
        found["public_api"] = _public_api(ctx, changes, unknown)
    if "config" in want:
        found["config"] = _config(ctx, changes)
    if "entry_points" in want:
        found["entry_points"] = _entries(ctx, changes, walk) + _manifest_findings(ctx, changes)
    for k in found:
        found[k] = _dedupe(found[k])
        found[k].sort(key=lambda f: (rr.rank(f["status"]), f["at"] or ""))
    truncated_concerns = {k: len(v) for k, v in found.items() if len(v) > MAX_PER_CONCERN}
    shown = {k: v[:MAX_PER_CONCERN] for k, v in found.items()}
    # tests
    tests = _tests(ctx, changes, run_tests=run_tests, observe=observe, unknown=unknown)
    unknown += _callers_unknown(ctx, changes)
    cited = {c.file for c in changes} | {d["at"].rsplit(":", 1)[0] for d in dependents} | \
        {a.rsplit(":", 1)[0] for fs in shown.values() for f in fs for a in [f["at"], *f["evidence_at"]]
         if a and ":" in a}
    note = _graph_note(ctx, cited, changes)
    unknown += _graph_unknown(note)
    read_first, budget = _read_first(ctx, changes, dependents, shown, max_chars)
    n_strong = sum(1 for v in found.values() for f in v if rr.at_least_strong(f["status"]))
    res = {
        "review_id": None,
        "mode": "planned" if targets else ("staged" if staged else "worktree"),
        "base": base_info,
        "summary": "",
        "graph": note,
        "changes": [c.as_dict() for c in changes],
        "files": files_info,
        "skipped": skipped,
        "dependents": dependents,
        "dependents_total": dep_total,
        "dependents_truncated": dep_total > len(dependents),
        "binding_readers": readers,
        "concerns": {k: shown[k] for k in CONCERNS if k in want},
        "concerns_checked": {k: (f"{len(found[k])} finding(s)" if found[k] else
                                 "no finding from rules: " + "; ".join(RULES[k]))
                             for k in CONCERNS if k in want},
        "concerns_truncated": truncated_concerns,
        "tests": tests,
        "unknown": unknown,
        "read_first": read_first,
        "budget": budget,
        "coverage": {"method": "changed definitions from symbol facts of both versions; dependents over the last "
                               "snapshot's graph (depth 3) by change kind; concern rule tables "
                               "(verinoda/review_rules.py)", "limits": list(LIMITS),
                     "not_checked": ([] if "security" in want and not targets else
                                     ["security (a planned change has no diff to compare)"] if targets else [])},
        "counts": {"changes": len(changes), "findings": sum(len(v) for v in found.values()),
                   "strong_or_verified": n_strong, "unknown": len(unknown)},
    }
    res["summary"] = _summary(res)
    res["seconds"] = round(time.perf_counter() - t0, 3)
    res["exit"] = 3 if (n_strong or unknown) else 0
    if record and store is not None:
        res["review_id"] = _record(store, repo, res)
    return res


def _mark_renames(ctx: _Ctx, changes: list[Change]) -> None:
    """An added and a removed definition of one file with the same body hash: a rename (``renamed_from``)."""
    removed = [c for c in changes if c.kind == "removed" and c.qual and c.old_lines]
    for a in changes:
        if a.kind != "added" or not a.qual or not a.lines:
            continue
        new_t = " ".join(" ".join(ctx.code(a.file)[a.lines[0] - 1: a.lines[1]]).split())
        for r in removed:
            if r.file != a.file:
                continue
            old_t = " ".join(ctx.code(r.file, "old")[r.old_lines[0] - 1: r.old_lines[1]])
            if " ".join(re.sub(rf"\b{re.escape(r.name)}\b", a.name, old_t).split()) == new_t:
                a.renamed_from = r.qual
                break


def _planned(ctx: _Ctx, targets: list[str], kind: str) -> tuple[list[Change], list[dict]]:
    out, unknown = [], []
    k = {"remove": "removed"}.get(kind, kind)
    from verinoda.treestate import safe_path

    for t in targets:
        t = str(t).strip().replace("\\", "/")
        rel, _, qual = t.partition("::")
        rel = rel.strip()
        while rel.startswith("./"):
            rel = rel[2:]
        inside = False
        if safe_path(rel) and not rel.startswith("/") and ":" not in rel:
            try:
                (ctx.repo / rel).resolve().relative_to(ctx.repo)
                inside = True
            except ValueError:
                inside = False
        if not inside:
            raise ValueError(f"target {t!r}: give a path inside the repository, relative to its root")
        facts = ctx.facts(rel)
        if facts is None:
            unknown.append({"kind": "target_not_found", "at": rel, "what": f"the planned change {t}",
                            "why": "no such file with symbol facts in the working tree",
                            "next_step": "give file::Qual.name of an existing definition"})
            continue
        syms = facts.get("symbols") or {}
        quals = [qual] if qual else [q for q in syms if "." not in q.split("#")[0]]
        for q in quals:
            s = syms.get(q) or next((v for kk, v in syms.items() if kk.endswith("." + q)), None)
            if s is None:
                unknown.append({"kind": "target_not_found", "at": rel, "what": f"the planned change {t}",
                                "why": f"{rel} defines no {q}", "next_step": "check the name (file::Class.method)"})
                continue
            q2 = q if q in syms else next(kk for kk, v in syms.items() if v is s)
            span = (s["start"], s["end"])
            ctx.base_texts.setdefault(rel, ctx.text(rel))
            out.append(Change(rel, q2, k, lines=span if k != "removed" else None, old_lines=span,
                              new_changed=set(range(span[0], span[1] + 1)) if k != "removed" else set(),
                              old_changed=set(range(span[0], span[1] + 1)), test=_is_test(rel), def_line=s["def"],
                              old_def_line=s["def"]))
    return out, unknown


def _value_state(ctx: _Ctx, walk: dict, n: str, memo: dict) -> tuple[bool | None, bool | None]:
    """Python body changes: (does ``n`` receive the changed function's value, does it return it on). A node
    receives it when the callee it reaches the change through returned it and ``n`` binds or passes on the call's
    result (def-use in ``n``). (None, None) when a hop is not a Python call or cannot be read."""
    if n in memo:
        return memo[n]
    info = walk[n]
    parent = info["parent"]
    edge = info["edge"] or {}
    if parent is None:
        state: tuple[bool | None, bool | None] = (True, True)   # the changed function returns its own value
    elif info["seed"].kind not in ("body", "signature") or edge.get("relation") != "calls" or \
            edge.get("_construction"):
        state = (None, None)
    else:
        _rec, p_returns = _value_state(ctx, walk, parent, memo)
        unit = ctx.unit_of(n)
        s = ctx.sym(*unit) if unit and unit[0].endswith((".py", ".pyi")) else None
        fn = rr.py_def_at(ctx.pytree(unit[0]), s["def"]) if s else None
        if p_returns is None or fn is None or isinstance(fn, ast.ClassDef):
            state = (None, None)
        elif not p_returns:
            state = (False, False)
        else:
            got = rr.py_carry(fn, _clean_label(ctx.g.label(parent)))
            receives = bool(got.returns or got.passes or got.tainted)
            state = (receives, receives and got.returns)
    memo[n] = state
    return state


def _dependents(ctx: _Ctx, walk: dict, changes: list[Change]) -> tuple[list[dict], int]:
    g = ctx.g
    rows = []
    memo: dict = {}
    for n, info in walk.items():
        if info["dist"] == 0 or _is_test(g.file(n) or ""):
            continue
        chain = _chain(ctx, walk, n)
        unit = ctx.unit_of(n)
        s = ctx.sym(*unit) if unit else None
        row = {"symbol": f"{g.file(n)}::{unit[1] if unit else _clean_label(g.label(n))}",
               "at": f"{g.file(n)}:{s['def'] if s else g.line(n)}", "distance": info["dist"],
               "for": info["seed"].symbol, "change": info["seed"].kind,
               "to": chain[0]["to"] if chain else None, "call_at": chain[0]["at"] if chain else None,
               "via": chain, "status": "possibly affected"}
        receives, _returns = _value_state(ctx, walk, n, memo)
        if receives is not None:
            row["receives_value"] = receives
        rows.append(row)
    rows.sort(key=lambda r: (r["distance"], not r.get("receives_value", False), r["at"]))
    return rows[:MAX_DEPENDENTS], len(rows)


def _binding_readers(ctx: _Ctx, changes: list[Change]) -> list[dict]:
    """Readers of changed module-level bindings (Python names, JVM static members assigned on changed lines)."""
    out = []
    for c in changes:
        if c.test or c.kind not in ("module_statement", "body") or not c.qual:
            continue
        names: list[str] = []
        if c.kind == "module_statement" and _suffix(c.file) in (".py", ".pyi") and not c.qual.startswith("<module>"):
            names = [n for n in dict.fromkeys([*c.qual.split(","), *(c.old_qual or "").split(",")]) if n]
        elif c.kind == "body" and (ctx.sym(c.file, c.qual) or {}).get("kind") == "class" and \
                _suffix(c.file) in (".java", ".kt"):
            code = ctx.code(c.file)
            for ln in sorted(c.new_changed):
                for k in range(ln, max(0, ln - 4), -1):
                    # `NAME = ...` or a declaration `private static final Type NAME = ...`
                    m = re.match(r"\s*(?:(?:public|private|protected|static|final|const|val|var|internal)\s+)*"
                                 r"(?:[\w<>\[\],.?]+\s+)?([A-Z_][A-Z0-9_]*)\s*(?::\s*[\w<>?.]+\s*)?=(?!=)",
                                 code[k - 1]) if k <= len(code) else None
                    if m:
                        names.append(m.group(1))
                        break
        for name in dict.fromkeys(names):
            for at, sym in _readers_of(ctx, c, name):
                out.append({"binding": f"{c.file}::{name}", "at": at, "reader": sym, "for": c.symbol})
    return out[:MAX_DEPENDENTS]


def _readers_of(ctx: _Ctx, c: Change, name: str) -> list[tuple[str, str | None]]:
    out = []
    if _suffix(c.file) in (".py", ".pyi"):
        from verinoda.guards import _module_of

        mod = _module_of(c.file)
        for rel in ctx.py_candidates(c.file, name):
            if not rel.endswith((".py", ".pyi")):
                continue
            t = ctx.text(rel)
            if not t or name not in t:
                continue
            tree = ctx.pytree(rel)
            if tree is None:
                continue
            imports = ctx._py_imports(rel)
            shadow = ctx.shadow(rel)
            local = {a for a, (m, orig) in imports.items() if orig == name and (m == mod or (
                m and _py_reexport(ctx, m, orig, mod, name)))}
            if rel == c.file:
                local.add(name)
            for n in ast.walk(tree):
                hit = (isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id in local
                       and n.id not in shadow.get(id(n), ())) or \
                    (isinstance(n, ast.Attribute) and n.attr == name and mod and isinstance(n.ctx, ast.Load)
                     and ctx.py_module_of_expr(rel, n.value) == mod)
                if hit:
                    q = ctx.label_at(rel, n.lineno)
                    if q:
                        out.append((f"{rel}:{n.lineno}", f"{rel}::{q}"))
    else:
        owner = PurePosixPath(c.file).stem
        rx = re.compile(rf"\b{re.escape(owner)}\s*\.\s*{re.escape(name)}\b")
        for rel in ctx.files():
            if _suffix(rel) not in CODE_SUFFIXES:
                continue
            t = ctx.text(rel)
            if not t or name not in t:
                continue
            for i, ln in enumerate(ctx.code(rel), 1):
                if rx.search(ln):
                    q = ctx.label_at(rel, i)
                    out.append((f"{rel}:{i}", f"{rel}::{q}" if q else None))
    return out


def _tests(ctx: _Ctx, changes: list[Change], *, run_tests: bool, observe: bool, unknown: list[dict]) -> dict:
    static, per = _static_tests(ctx, changes)
    observed = _observed_tests(ctx, changes)
    code_changes = [c for c in changes if not c.test and c.qual and c.kind in ("body", "signature", "added")
                    and _suffix(c.file) in CODE_SUFFIXES]
    out: dict = {"static": [{"test": t, **v} for t, v in sorted(static.items(), key=lambda kv: (kv[1]["distance"],
                                                                                                 kv[0]))],
                 "changed_tests": [c.symbol for c in changes if c.test],
                 "basis": "static: reverse reach over calls/uses/references (depth 3, through non-test code), and "
                          "the tests of the changed test files that call a changed symbol"}
    if observed:
        out["observed"] = observed
    reached_any = {c.symbol for c in code_changes if per.get(c.symbol)} | \
        {s for s, t in ((observed or {}).get("reached") or {}).items() if t}
    # "no test reaches" is said only of a symbol the static graph can reach at all: without any static caller
    # (a callback, a registration, dispatch through a CLI or a subprocess) the tests' reach is unknown
    silent = [c for c in code_changes if c.symbol not in reached_any]
    out["no_test_reaches"] = [c.symbol for c in silent if _has_static_caller(ctx, c)]
    out["reach_unknown"] = [{"symbol": c.symbol, "why": "it has no static caller (a callback, a registration, "
                             "dispatch or a new definition nothing calls yet): tests that reach it that way are not "
                             "seen"} for c in silent if c.symbol not in out["no_test_reaches"]]
    langs = {_lang(c.file) for c in code_changes}
    # test ids become command arguments: one that could be read as an option (a file named "-p.py") is never run
    py_ids = [t for t, _v in sorted(static.items(), key=lambda kv: (kv[1]["distance"], kv[0]))
              if t.split("::")[0].endswith(".py") and not t.startswith("-")]
    if langs - {"python"}:
        others = ", ".join(sorted({"jvm": "Java/Kotlin"}.get(x, x) for x in langs - {"python"}))
        unknown.append({"kind": "runtime_tests", "what": f"what the tests of the {others} code do at run time",
                        "why": "Verinoda runs and traces pytest only; Gradle, Maven and other runners are not "
                               "allowlisted", "next_step": "run the project's tests yourself (e.g. ./gradlew test) "
                                                           "and report the result"})
    staged = ctx.staged
    if observe and py_ids:
        why_not = _staged_run_problem(ctx, staged, observe=True) if staged is not None else None
        if why_not:
            out["observe"] = {"refused": why_not}
        else:
            from verinoda.runtime import trace

            nodes = [c.node for c in code_changes if c.node and c.file.endswith(".py")]
            res = trace.observe(ctx.store, ctx.repo, py_ids, graph=ctx.g, targets=nodes)
            tr = res.get("target_reach") or {}
            by_symbol = {}
            for c in code_changes:
                if c.node in tr:
                    by_symbol[c.symbol] = tr[c.node]
            ran = res.get("tests") or {}
            out["observe"] = {"run": res.get("run_id"), "complete": res.get("complete"), "outcome": res.get("outcome"),
                              "reached": by_symbol,
                              "selected_not_reaching": sorted(t for t in py_ids if t in ran and not any(
                                  t in v for v in by_symbol.values())) if res.get("complete") else [],
                              "limits": res.get("limits"), "error": res.get("error"),
                              "tree": "the working tree (it equals the index for every tracked file)" if staged
                              is not None else "the working tree"}
            reached = {t for v in by_symbol.values() for t in v}
            out["no_test_reaches"] = [s for s in out["no_test_reaches"] if not by_symbol.get(s)]
            out["reach_unknown"] = [r for r in out["reach_unknown"] if not by_symbol.get(r["symbol"])]
            for t in out["static"]:
                if t["test"] in reached:
                    t["observed"] = "reached"
                elif t["test"] in out["observe"]["selected_not_reaching"]:
                    t["observed"] = "not reached"
    if run_tests:
        if py_ids:
            from verinoda import experiments

            chosen = py_ids[:MAX_RUN_TESTS]
            argv = [experiments.python_for(ctx.repo), "-m", "pytest", "-q", "-p", "no:cacheprovider", *chosen]
            src: dict = {}
            why_not = None
            if staged is not None:
                why_not = _staged_run_problem(ctx, staged, observe=False)
                src = {"ref": staged["base"], "overlay": staged["paths"]} if not why_not else {}
            if why_not:
                out["run"] = {"refused": why_not}
            else:
                try:
                    exp = experiments.run(ctx.store, ctx.repo, argv, hypothesis="the tests that reach the reviewed "
                                          "change pass", expect="pass", **src)
                    out["run"] = {"experiment": exp.get("id"), "outcome": exp.get("outcome"),
                                  "summary": exp.get("summary"), "tests": len(chosen), "selected": len(py_ids),
                                  "not_run": len(py_ids) - len(chosen),
                                  "source": ("the staged tree: the base commit with the staged files on top"
                                             if staged is not None else "the working tree"),
                                  "tree": (exp.get("tree") or {}).get("hash") if isinstance(exp.get("tree"), dict)
                                  else exp.get("tree"), "logs": exp.get("logs")}
                except experiments.ExperimentRefused as exc:
                    out["run"] = {"refused": str(exc)}
        else:
            out["run"] = {"skipped": "no pytest test reaches the change statically"}
    return out


MAX_RUN_TESTS = 50


def _has_static_caller(ctx: _Ctx, c: Change) -> bool:
    """Does anything call or reference ``c`` in the static view: the graph's edges into it (or into the function
    it is nested in), or calls in the changed files (a definition the snapshot does not know yet)?"""
    if ctx.g is not None:
        for n in [c.node, ctx.node_of(c.file, _enclosing_def(ctx, c) or "")]:
            if n is not None and _in_edges(ctx, n, {"calls", "references", "uses"}):
                return True
    if _suffix(c.file) in (".py", ".pyi") and c.node is None:
        return bool(_py_callers(ctx, (c.file, c.qual), None)) or bool(
            [s for s in _py_call_sites(ctx, Change(c.file, c.qual, c.kind), files=[
                f for f in ctx.base_texts if f.endswith(".py") and _is_test(f)])])
    return False


def _staged_run_problem(ctx: _Ctx, staged: dict, *, observe: bool) -> str | None:
    """Why the staged tree cannot be run: the tests run on a copy of the base commit with the staged files laid
    on top, read from the working tree - so each staged file must hold its staged content there. ``observe``
    copies the working tree itself: it needs every tracked file - the staged ones included - to hold its staged
    content."""
    if observe:
        differ = sorted({*ctx.overrides, *_staged_unlike_worktree(ctx, staged),
                         *(p for p in staged.get("deleted") or () if (ctx.repo / p).exists())})
        if differ:
            return (f"--observe runs the working tree, and {len(differ)} tracked file(s) there differ from "
                    f"the staged tree (e.g. {differ[0]}): stash or stage them, or review the working tree instead")
        return None
    if staged.get("deleted"):
        return (f"the staged tree deletes {len(staged['deleted'])} file(s) (e.g. {staged['deleted'][0]}) and a copy "
                "of the base commit cannot drop them")
    if staged.get("skipped"):
        return f"staged file(s) that were not read (e.g. {staged['skipped'][0]}): the staged tree cannot be rebuilt"
    bad = _staged_unlike_worktree(ctx, staged)
    if bad:
        return (f"{len(bad)} staged file(s) have unstaged changes in the working tree (e.g. {bad[0]}): the run "
                "copies them from the working tree, so it would not run the staged tree - stash the unstaged changes "
                "or review the working tree instead")
    return None


def _staged_unlike_worktree(ctx: _Ctx, staged: dict) -> list[str]:
    """Staged files whose working-tree copy is not their staged content (unstaged changes on top)."""
    bad = []
    for p in staged["paths"]:
        try:
            now = _decode((ctx.repo / p).read_bytes())
        except OSError:
            now = None
        if now != ctx.text(p) and p not in staged.get("unchanged", ()):
            bad.append(p)
    return bad


def _callers_unknown(ctx: _Ctx, changes: list[Change]) -> list[dict]:
    out = []
    g = ctx.g
    if g is None:
        return out
    for c in changes:
        if c.test or c.kind not in ("body", "signature") or not c.qual or c.node is None:
            continue
        if (ctx.sym(c.file, c.qual) or {}).get("kind") == "class" and _last(c.qual) != _last(
                c.qual.rpartition(".")[0] or c.qual):
            continue
        callers = [u for u, _ in _in_edges(ctx, c.node, {"calls", "references"})]
        regs = ctx.registered(c.file, c.qual)
        if callers:
            continue
        if regs:
            out.append({"kind": "method_reference", "at": regs[0]["at"],
                        "what": f"the callers of {c.qual}",
                        "why": f"it has no call edge in the graph; it is registered by method reference at "
                               f"{regs[0]['at']} ({regs[0]['what']}): found by text search",
                        "next_step": "read the registration; runtime calls of framework callbacks are not traced"})
        elif not c.name.startswith("_"):
            out.append({"kind": "no_callers", "at": f"{c.file}:{c.def_line}",
                        "what": f"who calls {c.qual}",
                        "why": "no call edge in the graph and no registration found: a framework callback, "
                               "reflection, dynamic dispatch, or unused",
                        "next_step": f"search for `{c.name}` (or run `verinoda observe --for {c.name}` for Python)"})
    return out


def _graph_unknown(note: dict) -> list[dict]:
    if note.get("stale_files"):
        return [{"kind": "graph_stale", "at": note["stale_files"][0],
                 "what": "edges of files that changed since the last snapshot (outside this diff)",
                 "why": f"{len(note['stale_files'])} file(s) differ from the snapshot the graph was built from"
                        + (f", {note['newer_naming_changed']} of them added or edited since and naming a changed "
                           "definition: callers there are missing from the dependents (Python call sites, removed "
                           "names and readers were searched in them by text)" if note.get("newer_naming_changed")
                           else ""),
                 "next_step": "run `verinoda update` and review again"}]
    return []


def _graph_note(ctx: _Ctx, cited: set[str], changes: list[Change] | None = None) -> dict:
    """The snapshot the graph comes from, and which of the ``cited`` files (outside the diff) changed since - with
    the code files the snapshot does not have as they are now that name a changed definition (a caller added
    since the last ``update`` has no edge)."""
    snap = None
    stale: list[str] = []
    if ctx.store is not None:
        try:
            snap = ctx.store.latest_snapshot()
        except Exception:  # noqa: BLE001
            snap = None
    if snap is not None:
        recorded = ctx.store.snapshot_files(snap["id"])
        for rel in sorted(cited - set(ctx.base_texts)):
            try:
                if rel in ctx.overrides:   # staged mode: the index's version is the one reviewed
                    t = ctx.overrides[rel]
                    cur = hashlib.sha256(t.encode("utf-8")).hexdigest() if t is not None else None
                else:
                    cur = hashlib.sha256((ctx.repo / rel).read_bytes()).hexdigest()
            except OSError:
                cur = None
            if recorded.get(rel) not in (None, cur):
                stale.append(rel)
    names = {c.name for c in changes or () if c.qual and c.name}
    rx = re.compile(r"\b(?:" + "|".join(sorted(map(re.escape, names))) + r")\b") if names else None
    newer = [f for f in ctx.drifted() if f not in stale and rx is not None and rx.search(ctx.text(f) or "")]
    note = {"snapshot": (snap or {}).get("id"), "commit": (snap or {}).get("commit_sha"),
            "changed_files_reparsed": sorted(ctx.base_texts),
            "python_calls_read_from": sorted(ctx.reparsed)[:20],
            "note": "graph edges come from the last snapshot; the changed files are read from the tree under "
                    "review (Python calls re-parsed from its syntax tree, other languages' calls re-found by name)",
            "stale_files": stale + newer}
    if newer:
        note["newer_naming_changed"] = len(newer)
    return note


def _summary(res: dict) -> str:
    ch = res["changes"]
    where = ("the planned change" if res["mode"] == "planned" else
             f"the {'staged changes' if res['mode'] == 'staged' else 'working tree'} against "
             f"{res['base']['ref']} ({res['base']['commit'][:10]})")
    others = [f["file"] for f in res.get("files") or [] if f.get("kind") in ("data", "doc")]
    named = f" ({', '.join(others[:3])}{', ...' if len(others) > 3 else ''})" if others else ""
    if not ch:
        return (f"Review of {where}: no changed definition (comments, whitespace and docstrings are not changes)."
                + (f" {len(others)} data or documentation file(s) changed{named}, not reviewed by concern."
                   if others else ""))
    kinds: dict[str, int] = {}
    for c in ch:
        kinds[c["kind"]] = kinds.get(c["kind"], 0) + 1
    counts = ", ".join(f"{k}: {len(v)}" for k, v in res["concerns"].items() if v)
    quiet = [k for k, v in res["concerns"].items() if not v]
    parts = [f"Review of {where}: {len(ch)} change(s) (" + ", ".join(f"{n} {k}" for k, n in kinds.items()) + ")."]
    parts.append(("Findings - " + counts + "." if counts else "No finding from the rules.")
                 + (f" No finding from the rules for: {', '.join(quiet)}." if quiet and counts else ""))
    if res["unknown"]:
        parts.append(f"{len(res['unknown'])} unknown(s) to report.")
    if others:
        parts.append(f"{len(others)} data or documentation file(s) changed too{named}, not reviewed by concern.")
    return " ".join(parts)


def _record(store, repo: Path, res: dict) -> str | None:
    from verinoda.store import new_id, now

    rid = new_id("rev")
    try:
        snap = store.latest_snapshot()
        head = (res.get("base") or {}).get("commit") or "planned"
        digest = hashlib.sha256(repr(sorted((c["symbol"], c["kind"]) for c in res["changes"])).encode()).hexdigest()
        store.insert("analyses", {
            "id": rid, "question": f"review {head[:12]}..{res['mode']} {digest[:12]}",
            "snapshot_id": (snap or {}).get("id"),
            "budget": {"max_chars": res["budget"]["max_chars"]},
            "usage": {"seconds": res.get("seconds")},
            "result": {"kind": "review", "summary": res["summary"], "changes": res["changes"],
                       "findings": [{k: f.get(k) for k in ("concern", "rule", "status", "at", "finding")}
                                    for fs in res["concerns"].values() for f in fs],
                       "unknown": res["unknown"], "tests": {"static": [t["test"] for t in res["tests"]["static"]]}},
            "created_at": now()})
    except Exception:  # noqa: BLE001 - a read-only or busy store: the review is still returned
        return None
    return rid


# -- text rendering ----------------------------------------------------------------------------------------

def render_text(res: dict) -> str:
    """Answer first: what changed, the findings by concern, tests, unknowns, what to read first."""
    out = [res["summary"]]
    if res.get("changes"):
        out.append("")
        out.append("Changed:")
        for c in res["changes"][:15]:
            out.append(f"  {c['symbol']}  {c['kind']}" + (f"  lines {c['lines']}" if c.get("lines") else "")
                       + (f"  (base {c['base_lines']})" if c.get("base_lines") else "") + ("  [test]" if c.get("test")
                                                                                          else ""))
        if len(res["changes"]) > 15:
            out.append(f"  ... {len(res['changes']) - 15} more (--json)")
    for k, fs in res["concerns"].items():
        if not fs:
            continue
        out.append("")
        out.append(f"{k.replace('_', ' ')}:")
        for f in fs[:8]:
            out.append(f"  [{f['status']}] {f['finding']}")
            out.append(f"      at {f['at']}" + (f"; see {', '.join(f['evidence_at'][:3])}" if f.get("evidence_at")
                                                else "") + (f"  ({f['derived_by']})" if f.get("derived_by") else ""))
        if len(fs) > 8:
            out.append(f"  ... {len(fs) - 8} more (--json)")
    quiet = [k for k, v in res["concerns"].items() if not v]
    if quiet:
        out.append("")
        out.append("No finding from the rules (not a guarantee) for: " + ", ".join(quiet)
                   + " - see concerns_checked in --json for the rules that ran.")
    t = res.get("tests") or {}
    out.append("")
    st = t.get("static") or []
    out.append(f"Tests: {len(st)} reach the change statically" + (": " + ", ".join(x["test"] for x in st[:6])
                                                                   + (" ..." if len(st) > 6 else "") if st else "."))
    if t.get("observed") and not t.get("observe"):
        ob = t["observed"]
        out.append(f"  observed in run {ob['run']} (commit {str(ob.get('commit') or '?')[:10]}, run-scoped): "
                   + "; ".join(f"{s.split('::')[-1]} reached by {len(v)} test(s)" for s, v in ob["reached"].items()))
    if t.get("observe") and not t["observe"].get("refused"):
        ob = t["observe"]
        out.append(f"  observed (run {ob.get('run')}): " + "; ".join(f"{s.split('::')[-1]} reached by {len(v)}"
                                                                     for s, v in (ob.get("reached") or {}).items())
                   + (f"; selected but not reaching: {', '.join(ob['selected_not_reaching'])}"
                      if ob.get("selected_not_reaching") else ""))
    if t.get("run"):
        r = t["run"]
        summ = r.get("summary")
        if isinstance(summ, dict):
            summ = "; ".join(summ.get("summary_lines") or []) or f"exit code {summ.get('exit_code')}"
        out.append(f"  run: {r.get('outcome') or r.get('refused') or r.get('skipped')}"
                   + (f" ({summ})" if summ else "") + (f" on {r['source']}" if r.get("source") else "")
                   + (f"; {r['tests']} of {r['selected']} selected test(s) run" if r.get("not_run") else "")
                   + (f", experiment {r['experiment']}" if r.get("experiment") else ""))
    if (t.get("observe") or {}).get("refused"):
        out.append(f"  observe: refused - {t['observe']['refused']}")
    if t.get("no_test_reaches"):
        out.append("  no test reaches it in the static graph: " + ", ".join(t["no_test_reaches"][:6]))
    if t.get("reach_unknown"):
        out.append("  tests unknown (no static caller - dispatch, callbacks, a CLI or subprocess run may reach it): "
                   + ", ".join(r["symbol"] for r in t["reach_unknown"][:6]))
    if res.get("unknown"):
        out.append("")
        out.append("Unknown:")
        for u in res["unknown"][:8]:
            out.append(f"  - {u['what']}: {u['why']}. Next: {u['next_step']}")
    if res.get("dependents"):
        out.append("")
        out.append(f"Dependents (possibly affected): {res['dependents_total']}"
                   + (f", {len(res['dependents'])} listed" if res.get("dependents_truncated") else "") + ": "
                   + ", ".join(f"{d['symbol'].split('::')[-1]} ({d['distance']})" for d in res["dependents"][:8]))
    if res.get("read_first"):
        b = res["budget"]
        out.append("")
        out.append(f"Read first ({b['used_chars']} of {b['max_chars']} chars"
                   + (f"; {b['more_total']} more location(s) left out" if b.get("truncated") else "") + "):")
        for r in res["read_first"]:
            out.append(f"  {r['at']}  {r['why']}")
    return "\n".join(out)
