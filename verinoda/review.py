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
import difflib
import hashlib
import re
import time
from collections import deque
from dataclasses import dataclass, field
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
RULES = {
    "persistence": ["sink pattern on a changed line (architecture_map.SINK_PATTERNS + review NBT rows)",
                    "a call on a changed line whose callee reaches a sink (depth <= 2)",
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
    lines: tuple[int, int] | None = None        # span in the new version
    old_lines: tuple[int, int] | None = None    # span in the base version
    new_changed: set[int] = field(default_factory=set)
    old_changed: set[int] = field(default_factory=set)
    test: bool = False
    node: str | None = None
    def_line: int | None = None
    old_def_line: int | None = None

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
        self.reparsed: set[str] = set()

    # texts ------------------------------------------------------------------------------------------
    def text(self, rel: str, side: str = "new") -> str | None:
        if side == "old":
            return self.base_texts.get(rel, self.text(rel)) if rel in self.base_texts else self.text(rel)
        if rel not in self._text:
            try:
                data = (self.repo / rel).read_bytes()
                self._text[rel] = data.decode("utf-8", "replace").replace("\r\n", "\n")
            except OSError:
                self._text[rel] = None
        return self._text[rel]

    def lines(self, rel: str, side: str = "new") -> list[str]:
        t = self.text(rel, side)
        return t.split("\n") if t is not None else []

    def code(self, rel: str, side: str = "new") -> list[str]:
        """Lines with comments blanked (strings kept)."""
        key = (rel, side)
        if key not in self._code:
            from verinoda.guards import code_text

            t = self.text(rel, side)
            self._code[key] = code_text(t, _suffix(rel), keep_strings=True).split("\n") if t is not None else []
        return self._code[key]

    def facts(self, rel: str, side: str = "new") -> dict | None:
        key = (rel, side)
        if key not in self._facts:
            if side == "new" and rel not in self.base_texts and self.store is not None:
                # a file outside the diff: the store's facts cache (keyed by the file's sha256) serves it
                f = anchors.facts_for(self.store, self.repo, rel)
            else:
                t = self.text(rel, side)
                f = anchors.compute_facts(rel, t.encode("utf-8", "surrogatepass")) if t is not None else None
            self._facts[key] = f if anchors.usable(f) else None
        return self._facts[key]

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
            from verinoda.snapshot import list_files

            self._files = list_files(self.repo)
        return self._files

    def pyix(self):
        if self._pyix is None:
            from verinoda.guards import _PyIndex

            self._pyix = _PyIndex(self.repo, self.files())
        return self._pyix

    # symbols and nodes ------------------------------------------------------------------------------
    def span(self, rel: str, qual: str, side: str = "new") -> tuple[int, int] | None:
        s = ((self.facts(rel, side) or {}).get("symbols") or {}).get(qual)
        return (s["start"], s["end"]) if s else None

    def sym(self, rel: str, qual: str, side: str = "new") -> dict | None:
        return ((self.facts(rel, side) or {}).get("symbols") or {}).get(qual)

    def node_of(self, rel: str, qual: str) -> str | None:
        g = self.g
        if g is None or not qual:
            return None
        name = _last(qual)
        cands = [n for n in g.symbols_in(rel) if _clean_label(g.label(n)) == name]
        if len(cands) <= 1:
            return cands[0] if cands else None
        want: set[int] = set()
        for side in ("new", "old"):
            s = self.sym(rel, qual, side)
            if s:
                want |= {s["start"], s["def"]}
        by_line = [n for n in cands if g.line(n) in want]
        if len(by_line) == 1:
            return by_line[0]
        owner = _last(qual.split("#")[0].rpartition(".")[0]) if "." in qual else ""
        if owner:
            own = [n for n in (by_line or cands) if any(_clean_label(g.label(u)) == owner
                                                         for u, _ in g.in_edges(n, {"method"}))]
            if own:
                return own[0]
        return (by_line or cands)[0]

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
            for line, kind, by in rr.sink_hits(self.code(rel), sp[0], sp[1]):
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
        # calls on the given lines that the graph does not know (the change added them): by name, same class/file
        if lines is not None:
            known = {(ln, nm) for ln, nm, *_ in out}
            syms = (self.facts(rel) or {}).get("symbols") or {}
            for ln in sorted(lines):
                if not (s["start"] <= ln <= s["end"]) or ln > len(code):
                    continue
                for m in re.finditer(r"\b([A-Za-z_]\w*)\s*\(", code[ln - 1]):
                    nm = m.group(1)
                    if (ln, nm) in known or nm in ("if", "for", "while", "switch", "catch", "return", "new"):
                        continue
                    local = [q for q in syms if _last(q) == nm]
                    if len(local) == 1:
                        out.append((ln, nm, (rel, local[0]), "same file, by name", "INFERRED"))
        return out

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
            u = resolve_name(f.id)
            if u:
                return u, "Python: name bound in the file or by an import", "EXTRACTED"
        elif isinstance(f, ast.Attribute):
            base = f.value
            if isinstance(base, ast.Name):
                owner = qual.rpartition(".")[0]
                if base.id in ("self", "cls") and owner:
                    u = cls_method(rel, owner, f.attr)
                    if u:
                        return u, "Python: self/cls method", "EXTRACTED"
                if base.id in imports and imports[base.id][1] is None:  # import module as x
                    mrel = self.pyix().modules.get(imports[base.id][0])
                    if mrel and f.attr in ((self.facts(mrel) or {}).get("symbols") or {}):
                        return (mrel, f.attr), "Python: module attribute", "EXTRACTED"
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
        tree = self.pytree(rel)
        out: dict[str, tuple[str | None, str | None]] = {}
        if tree is None:
            return out
        from verinoda.guards import _module_of

        pkg = (_module_of(rel) or "")
        if not rel.endswith("__init__.py"):
            pkg = pkg.rpartition(".")[0]
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                for a in n.names:
                    out[a.asname or a.name.split(".")[0]] = (a.name if a.asname else a.name.split(".")[0], None)
            elif isinstance(n, ast.ImportFrom):
                mod = n.module or ""
                if n.level:
                    parts = pkg.split(".") if pkg else []
                    parts = parts[: len(parts) - (n.level - 1)] if n.level > 1 else parts
                    mod = ".".join([*parts, *([mod] if mod else [])])
                for a in n.names:
                    if a.name != "*":
                        out[a.asname or a.name] = (mod, a.name)
        return out

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
        diffs.append(FileDiff(rel, old_b.decode("utf-8", "replace") if old_b is not None else None,
                              new_b.decode("utf-8", "replace") if new_b is not None else None, in_git))
    return diffs, skipped


def _diff_staged(repo: Path, base_sha: str) -> tuple[list[FileDiff], list[dict]]:
    """The index (staged changes) against ``base_sha``; blob contents read with ``cat-file``."""
    from verinoda import treestate

    out = treestate._git(repo, "diff-index", "--cached", "--name-only", "-z", "--relative", "--no-renames",
                         base_sha, "--")
    if out is None:
        raise treestate.NotAGitTree(f"git diff-index --cached against {base_sha[:12]} failed")
    paths = sorted(p for p in out.split("\0") if p and treestate.safe_path(p))
    staged = {}
    if paths:
        ls = treestate._git(repo, "ls-files", "-s", "-z", "--", *paths) or ""
        for rec in ls.split("\0"):
            if "\t" in rec:
                meta, p = rec.split("\t", 1)
                parts = meta.split(" ")
                if len(parts) >= 3 and parts[2] == "0":
                    staged[p] = parts[1]
    blobs = treestate.read_blobs(repo, [treestate.blob_spec(repo, base_sha, p) for p in paths] + list(staged.values()))
    diffs, skipped = [], []
    for p in paths:
        old_b = blobs.get(treestate.blob_spec(repo, base_sha, p))
        new_b = blobs.get(staged[p]) if p in staged else None
        if (old_b is not None and treestate.is_binary(old_b)) or (new_b is not None and treestate.is_binary(new_b)):
            skipped.append({"file": p, "why": "binary"})
            continue
        norm = (lambda b: None if b is None else treestate.normalise(b).decode("utf-8", "replace"))
        if norm(old_b) == norm(new_b):
            continue
        diffs.append(FileDiff(p, norm(old_b), norm(new_b), True))
    return diffs, skipped


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
        if tag == "delete" and b:   # the new-side place of a pure deletion, for the enclosing symbol
            new_l.discard(0)
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
    if suffix in rr.CONFIG_SUFFIXES or (CONFIG_FILE_RE.search(rel) and suffix not in CODE_SUFFIXES):
        return _config_changes(ctx, fd, new_l, old_l), {**info, "kind": "config"}
    if suffix in anchors.MD_SUFFIXES:
        return [], {**info, "kind": "doc"}
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
    # module-level statements
    mo = (fo or {}).get("module") or {}
    mn = (fn or {}).get("module") or {}
    for key in sorted(set(mo) | set(mn), key=lambda k: ((mn.get(k) or mo.get(k))["start"], k)):
        if key == "DOC" or key.split("#")[0] == "DOC":
            continue
        a, b = mo.get(key), mn.get(key)
        if a is not None and b is not None and a["h"] == b["h"]:
            continue
        if key.startswith("STMT:") and a is not None and b is not None:
            continue
        nc = {ln for ln in new_l if b and b["start"] <= ln <= b["end"]}
        oc = {ln for ln in old_l if a and a["start"] <= ln <= a["end"]}
        if not nc and not oc:
            continue
        name = key.split(":", 1)[1].split("#")[0] if key.startswith(("IMPORT:", "ASSIGN:")) else None
        if name is None:
            ln = (b or a)["start"]
            name = f"<module>@L{ln}"
        out.append(Change(rel, name, "module_statement", lines=(b["start"], b["end"]) if b else None,
                          old_lines=(a["start"], a["end"]) if a else None, new_changed=nc, old_changed=oc,
                          test=test))
    return out, {**info, "kind": "code"}


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
    return list(out.values())


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
            if line not in c.new_changed or f"{c.file}:{line}" in seen_at:
                continue
            seen_at.add(f"{c.file}:{line}")
            out.append(_finding("persistence", "sink-on-changed-line",
                                f"a changed line holds a {kind}: {ctx.lines(c.file)[line - 1].strip()[:100]}",
                                "strong_inference", f"{c.file}:{line}", basis="pattern over the code of the line "
                                "(comments removed)", derived_by=by, for_symbol=c.symbol))
        if c.old_changed and not any(True for _ in rr.sink_hits(code, *(c.lines or (0, -1)))):
            ocode = ctx.code(c.file, "old")
            for line, kind, by in rr.sink_hits(ocode, min(c.old_changed), max(c.old_changed)):
                if line in c.old_changed:
                    out.append(_finding("persistence", "sink-line-removed",
                                        f"a {kind} line was removed or changed (base line {line})", "strong_inference",
                                        f"{c.file}:{line}", basis="pattern over the base version's code",
                                        derived_by=by, for_symbol=c.symbol, side="base"))
    # changed calls that reach a sink
    for c in _code_units(changes):
        unit = (c.file, c.qual)
        for line, name, tgt, how, conf in ctx.callees(unit, c.new_changed):
            if tgt is None or f"{c.file}:{line}" in seen_at:
                continue
            hits = ctx.sink_reach(tgt, depth=2)
            if not hits:
                continue
            s = hits[0]
            seen_at.add(f"{c.file}:{line}")
            path = " -> ".join(_last(u[1]) for u in s["path"])
            out.append(_finding("persistence", "changed-call-reaches-sink",
                                f"the changed call to {name}() at line {line} reaches a {s['kind']} at {s['at']} "
                                f"(via {path})", "strong_inference", f"{c.file}:{line}", evidence_at=[s["at"]],
                                basis=f"call resolved: {how}" + (f", edge {conf}" if conf else ""),
                                derived_by=s["derived_by"], for_symbol=c.symbol))
    # the changed function's value carried by its callers into a sink call (Python, def-use per hop)
    for c in _code_units(changes):
        if _suffix(c.file) not in (".py", ".pyi") or ((ctx.sym(c.file, c.qual) or {}).get("kind") == "class"):
            continue
        out += _value_flow(ctx, c, seen_at)
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
                    hits = [s for s in ctx.unit_sinks(tgt)
                            if params and rr.py_flows_to_line(tfn, params, int(s["at"].rsplit(":", 1)[1]))]
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
    for c in changes:
        if not c.test and _suffix(c.file) in CODE_SUFFIXES:
            by_file.setdefault(c.file, set()).update(c.new_changed)
    # operations on changed lines
    for rel, lines in sorted(by_file.items()):
        if not lines:
            continue
        if _suffix(rel) in (".py", ".pyi"):
            tree = ctx.pytree(rel)
            if tree is None:
                continue
            for line, kind, what, by in rr.py_security_ops(tree, rel, ctx.pyix(), lines):
                flow = _param_flow(ctx, rel, line)
                text = f"a changed line adds {kind}: {what}"
                if flow and flow.get("from_entry"):
                    text += f"; an entry point's parameter reaches it: {flow['from_entry']}"
                elif flow:
                    text += f"; it uses the parameter(s) {', '.join(flow['params'])}"
                out.append(_finding("security", "op-on-changed-line", text,
                                    "statically_verified", f"{rel}:{line}", basis="Python syntax tree of the working "
                                    "tree (calls bound through imports and aliases); parameter flow: def-use per hop, "
                                    "an inference", derived_by=by, for_symbol=_sym_for(ctx, rel, line),
                                    evidence_at=[flow["entry"]] if flow and flow.get("entry") else (),
                                    param_flow=flow))
        else:
            code = ctx.code(rel)
            for line in sorted(lines):
                if line > len(code):
                    continue
                for rx, kind in rr.TEXT_SECURITY_OPS:
                    if rx.search(code[line - 1]):
                        out.append(_finding("security", "op-on-changed-line",
                                            f"a changed line adds {kind}: {ctx.lines(rel)[line - 1].strip()[:100]}",
                                            "strong_inference", f"{rel}:{line}", basis="text rule over the code of the "
                                            "line (comments removed)", derived_by="review.TEXT_SECURITY_OPS",
                                            for_symbol=_sym_for(ctx, rel, line)))
                        break
    # exit guards removed or changed (syntax tree diff), moved guards told apart
    added_all = []
    per_change = []
    for c in changes:
        if c.test or c.kind not in ("body", "signature") or not c.qual or c.old_lines is None or c.lines is None:
            continue
        old_g, new_g = _guards(ctx, c, "old"), _guards(ctx, c, "new")
        per_change.append((c, old_g, new_g))
        o_conds = [g[0] for g in old_g]
        n_conds = [g[0] for g in new_g]
        added_all += [(c, cond, ln) for cond, ln in new_g if n_conds.count(cond) > o_conds.count(cond)]
    for c, old_g, new_g in per_change:
        o_conds = [g[0] for g in old_g]
        n_conds = [g[0] for g in new_g]
        gone = [(cond, ln) for cond, ln in old_g if o_conds.count(cond) > n_conds.count(cond) and ln in c.old_changed]
        new = [(cond, ln) for cond, ln in new_g if n_conds.count(cond) > o_conds.count(cond) and ln in c.new_changed]
        for cond, ln in gone:
            moved = [a for a in added_all if a[1] == cond and a[0] is not c]
            partner = max(new, key=lambda x: difflib.SequenceMatcher(None, cond, x[0]).ratio(), default=None)
            ratio = difflib.SequenceMatcher(None, cond, partner[0]).ratio() if partner else 0.0
            if moved:
                m = moved[0]
                out.append(_finding("security", "guard-moved",
                                    f"the check `{cond}` left {c.name} (base line {ln}) and appears in "
                                    f"{m[0].name} at line {m[2]}", "weak_inference", f"{c.file}:{ln}",
                                    evidence_at=[f"{m[0].file}:{m[2]}"], basis="syntax tree diff of both versions",
                                    derived_by="review.guard_diff", for_symbol=c.symbol, side="base"))
            elif partner is not None and ratio >= 0.5:
                new.remove(partner)
                out.append(_finding("security", "guard-changed",
                                    f"the exit guard of {c.name} changed: `{cond}` (base line {ln}) is now "
                                    f"`{partner[0]}` - weakened or strengthened is not decided",
                                    "statically_verified", f"{c.file}:{partner[1]}", evidence_at=[f"{c.file}:{ln}"],
                                    basis="syntax tree diff of both versions", derived_by="review.guard_diff",
                                    for_symbol=c.symbol, old=cond, new=partner[0]))
            else:
                out.append(_finding("security", "guard-removed",
                                    f"the exit guard `if {cond}: <exit>` of {c.name} (base line {ln}) is gone",
                                    "statically_verified", f"{c.file}:{ln}",
                                    evidence_at=[f"{c.file}:{c.lines[0]}"] if c.lines else (),
                                    basis="the base version's syntax tree has it, the working tree's does not",
                                    derived_by="review.guard_diff", for_symbol=c.symbol, side="base"))
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
                out.append(_finding("security", "security-words", "a changed line names a security-relevant "
                                    f"concept: `{ctx.lines(c.file)[ln - 1].strip()[:90]}`", "weak_inference",
                                    f"{c.file}:{ln}", basis="word match (heuristic)", derived_by="review.AUTH_WORDS",
                                    for_symbol=c.symbol))
                break
    return out


def _param_flow(ctx: _Ctx, rel: str, line: int) -> dict | None:
    """Python: which parameter of the function holding ``line`` reaches its statement, and a caller path (up to
    3 hops, def-use per hop) that feeds it from an entry point's parameter."""
    qual = ctx.label_at(rel, line)
    s = ctx.sym(rel, qual) if qual else None
    fn = rr.py_def_at(ctx.pytree(rel), s["def"]) if s else None
    if fn is None or isinstance(fn, ast.ClassDef) or ctx.g is None:
        return None
    wanted = rr.py_params_on_line(fn, line)
    if not wanted:
        return None
    start = ctx.node_of(rel, qual)
    path = [f"{_last(qual)}({', '.join(sorted(wanted))})"]
    if start is not None and (entry_reasons(ctx.g, start) or _entry_info(ctx, start, (rel, qual))):
        return {"params": sorted(wanted), "from_entry": path[0], "entry": f"{rel}:{s['def']}"}
    frontier = [(start, fn, wanted, path, qual)]
    seen = {start}
    for _ in range(DEPTH):
        nxt = []
        for node, cfn, want, p, cqual in frontier:
            if node is None:
                continue
            for u, _d in _in_edges(ctx, node, {"calls"}):
                if u in seen or _is_test(ctx.g.file(u) or ""):
                    continue
                unit = ctx.unit_of(u)
                if unit is None or not unit[0].endswith((".py", ".pyi")):
                    continue
                us = ctx.sym(*unit)
                ufn = rr.py_def_at(ctx.pytree(unit[0]), us["def"]) if us else None
                if ufn is None or isinstance(ufn, ast.ClassDef):
                    continue
                seen.add(u)
                got: set[str] = set()
                bound = "." in cqual and (ctx.sym(ctx.g.file(node), cqual.rpartition(".")[0]) or {}).get("kind") == \
                    "class"
                for call in rr.py_calls_in(ufn):
                    if rr.call_name(call) == _last(cqual):
                        got |= rr.py_args_from_params(ufn, call, cfn, want,
                                                      bound=bound and isinstance(call.func, ast.Attribute))
                if not got:
                    continue
                p2 = [f"{_last(unit[1])}({', '.join(sorted(got))})"] + p
                if entry_reasons(ctx.g, u) or _entry_info(ctx, u, unit):
                    return {"params": sorted(wanted), "from_entry": " -> ".join(p2),
                            "entry": f"{unit[0]}:{us['def']}"}
                nxt.append((u, ufn, got, p2, unit[1]))
        frontier = nxt
    return {"params": sorted(wanted), "from_entry": None}


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
            old_heads = {_loop_head_py(n) for n, *_ in (rr.py_loops(old_fn) if old_fn is not None else [])}
            for loop, lo, hi, it in rr.py_loops(fn):
                body_lines = set(range(lo, hi + 1))
                if not (body_lines & c.new_changed):
                    continue
                found = None
                for call in rr.py_calls_in(loop):
                    if call.lineno == lo and not isinstance(loop, (ast.ListComp, ast.SetComp, ast.GeneratorExp,
                                                                   ast.DictComp)):
                        continue   # the iterable of a for-loop runs once
                    tgt, how, conf = ctx.py_resolve(c.file, c.qual, fn, call)
                    hits = ctx.sink_reach(tgt, depth=2) if tgt else []
                    if hits:
                        found = (call, hits[0], how, conf)
                        break
                direct = [h for h in rr.sink_hits(ctx.code(c.file), lo + 1, hi)] if not found else []
                if not found and not direct:
                    if hot_why and _loop_head_py(loop) not in old_heads and lo in c.new_changed:
                        out.append(_hot_loop(c, lo, hot_why, "a loop"))
                    continue
                bound = _py_loop_bound(ctx, c, fn, it, lo) if it else None
                if found:
                    call, sink, how, conf = found
                    text = (f"{rr.call_name(call)}() runs once per iteration of the loop at line {lo} and reaches a "
                            f"{sink['kind']} at {sink['at']} (N+1)")
                    ev = [f"{c.file}:{call.lineno}", sink["at"]]
                    basis = f"syntax tree loop; call resolved by: {how}" + (f" ({conf})" if conf else "")
                    by = sink["derived_by"]
                else:
                    line, kind, by = direct[0]
                    text = f"a {kind} at line {line} runs once per iteration of the loop at line {lo}"
                    ev, basis = [f"{c.file}:{line}"], "syntax tree loop; sink pattern in its body"
                if hot_why:
                    text += f"; the function is on a hot path ({hot_why['why']})"
                    ev += [hot_why["at"]] if hot_why.get("at") else []
                if bound:
                    text += f"; the loop is bounded by `{bound['cond']}` at {bound['at']}"
                    ev.append(bound["at"])
                elif it:
                    unknown.append({"kind": "loop_bound", "at": f"{c.file}:{lo}",
                                    "what": f"how many times the loop over `{it}` at {c.file}:{lo} runs",
                                    "why": f"no cap on len({it}) was found in {c.name} or in the checks it calls "
                                           "before the loop",
                                    "next_step": f"look for a limit on {it} in the callers, or measure with the "
                                                 "largest expected input"})
                out.append(_finding("performance", "io-in-loop", text, "strong_inference", f"{c.file}:{lo}",
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
                calls = _loop_calls_sinks(ctx, c, lo, hi)
                if calls:
                    call_line, name, sink = calls
                    text = (f"{name}() runs once per iteration of the loop at line {lo} and reaches a "
                            f"{sink['kind']} at {sink['at']} (N+1)")
                    out.append(_finding("performance", "io-in-loop", text, "strong_inference", f"{c.file}:{lo}",
                                        evidence_at=[f"{c.file}:{call_line}", sink["at"]],
                                        basis="tree-sitter loop; call matched to the graph's edges by name",
                                        derived_by=sink["derived_by"], for_symbol=c.symbol))
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
                    out.append(_finding("performance", "hot-loop-changed", text, "strong_inference", f"{c.file}:{lo}",
                                        evidence_at=[hot_why["at"]] if hot_why.get("at") else [],
                                        basis="tree-sitter loop headers of both versions; hot path: "
                                              + hot_why["basis"], derived_by="review.hot_path", for_symbol=c.symbol))
                    continue
                out.append(_hot_loop(c, lo, hot_why, f"a loop (`{head[:60]}`)"))
    return out


def _hot_loop(c: Change, lo: int, hot_why: dict, what: str) -> dict:
    return _finding("performance", "loop-added-hot-path",
                    f"{what} was added at line {lo} in {c.name}, which runs on a hot path ({hot_why['why']})",
                    "strong_inference", f"{c.file}:{lo}", evidence_at=[hot_why["at"]] if hot_why.get("at") else [],
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


def _loop_calls_sinks(ctx: _Ctx, c: Change, lo: int, hi: int):
    unit = (c.file, c.qual)
    for line, name, tgt, _how, _conf in ctx.callees(unit, set(range(lo + 1, hi + 1))):
        if tgt is None:
            continue
        hits = ctx.sink_reach(tgt, depth=2)
        if hits:
            return line, name, hits[0]
    return None


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
        elif c.kind == "removed" or (c.kind == "module_statement" and c.lines is None and c.old_lines):
            out += _removed_refs(ctx, c, unknown)
    return out


def _py_call_sites(ctx: _Ctx, c: Change) -> list[tuple[str, ast.Call, str, str]]:
    """``(file, call, how, status)`` of the calls that bind to ``c`` (Python): names imported from its module
    or defined in its file, module attributes, ``self.m()`` in its class, and the graph's call edges."""
    mod = None
    from verinoda.guards import _module_of

    mod = _module_of(c.file)
    name = c.name
    owner = c.qual.rpartition(".")[0] if "." in c.qual else ""
    sites = []
    cand_files = {c.file}
    if c.node is not None:
        cand_files |= {ctx.g.file(u) for u, _ in _in_edges(ctx, c.node, {"calls", "imports_from", "imports"})
                       if ctx.g.file(u)}
    if not owner:
        for rel in ctx.files():
            if rel.endswith((".py", ".pyi")) and rel not in cand_files:
                t = ctx.text(rel)
                if t and name in t and (mod or "") .rpartition(".")[2] in t:
                    cand_files.add(rel)
    for rel in sorted(f for f in cand_files if f and f.endswith((".py", ".pyi"))):
        tree = ctx.pytree(rel)
        if tree is None:
            continue
        imports = ctx._py_imports(rel)
        local_names = {a for a, (m, orig) in imports.items() if orig == name and m == mod}
        mod_aliases = {a for a, (m, orig) in imports.items() if orig is None and m == mod}
        for call in ast.walk(tree):
            if not isinstance(call, ast.Call):
                continue
            f = call.func
            how = None
            if not owner:
                if isinstance(f, ast.Name) and (f.id in local_names or (rel == c.file and f.id == name)):
                    how = "name bound by an import of the changed module" if f.id in local_names else "same module"
                elif isinstance(f, ast.Attribute) and f.attr == name and isinstance(f.value, ast.Name) and \
                        f.value.id in mod_aliases:
                    how = "module attribute"
                if how:
                    sites.append((rel, call, how, "statically_verified"))
            elif isinstance(f, ast.Attribute) and f.attr == name:
                enc = ctx.label_at(rel, call.lineno)
                if isinstance(f.value, ast.Name) and f.value.id in ("self", "cls") and rel == c.file and enc and \
                        enc.startswith(owner + "."):
                    sites.append((rel, call, "self/cls in the same class", "statically_verified"))
                elif isinstance(f.value, ast.Name) and f.value.id == owner.rpartition(".")[2]:
                    sites.append((rel, call, "class attribute", "strong_inference"))
                else:
                    efn = rr.py_def_at(tree, (ctx.sym(rel, enc) or {}).get("def", -1)) if enc else None
                    cls = ctx._py_receiver_class(efn, f.value.id) if efn is not None and isinstance(f.value, ast.Name) \
                        else None
                    if cls and _last(cls) == owner.rpartition(".")[2]:
                        sites.append((rel, call, f"receiver annotated or constructed as {cls}", "strong_inference"))
    return sites


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
    for rel, call, how, status in sites:
        if params["decorated"] and status == "statically_verified":
            status = "strong_inference"   # a decorator may change the signature
        problem = rr.py_arity_problem(params, call, bound=bound and how != "class attribute")
        if problem:
            out.append(_finding("public_api", "arity-break",
                                f"the call to {c.name}() at {rel}:{call.lineno} no longer fits the new signature: "
                                f"{problem}", status, f"{rel}:{call.lineno}", evidence_at=[f"{c.file}:{s['def']}"],
                                basis=f"call bound by: {how}; arguments counted against the new parameters (syntax "
                                      "tree)" + ("; decorated definition" if params["decorated"] else ""),
                                derived_by="review.arity", for_symbol=c.symbol))
    if not sites:
        unknown.append({"kind": "dynamic_callers", "at": f"{c.file}:{s['def']}",
                        "what": f"who calls {c.qual} with the old signature",
                        "why": "no call site binds to it by import, module attribute or class in the working tree",
                        "next_step": f"search for `{c.name}(`; callers through objects are not resolved statically "
                                     "(verinoda resolve-call on a site)"})
    return out


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
    if _suffix(c.file) in (".py", ".pyi"):
        from verinoda.guards import _module_of

        mod = _module_of(c.file)
        for rel in ctx.files():
            if not rel.endswith((".py", ".pyi")):
                continue
            t = ctx.text(rel)
            if not t or name not in t:
                continue
            tree = ctx.pytree(rel)
            if tree is None:
                continue
            imports = ctx._py_imports(rel)
            local = {a for a, (m, orig) in imports.items() if orig == name and m == mod}
            aliases = {a for a, (m, orig) in imports.items() if orig is None and m == mod}
            for n in ast.walk(tree):
                if isinstance(n, ast.ImportFrom) and any(a.name == name for a in n.names) and \
                        imports.get(next(a.asname or a.name for a in n.names if a.name == name), (None,))[0] == mod:
                    out.append(_finding("public_api", "removed-still-used",
                                        f"{c.qual} was removed but {rel}:{n.lineno} still imports it", "statically_verified",
                                        f"{rel}:{n.lineno}", basis="import of the removed name from its module (syntax "
                                        "tree)", derived_by="review.removed_refs", for_symbol=c.symbol))
                elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id in local:
                    out.append(_finding("public_api", "removed-still-used",
                                        f"{c.qual} was removed but {rel}:{n.lineno} still uses it", "statically_verified",
                                        f"{rel}:{n.lineno}", basis="name bound by an import of the removed name",
                                        derived_by="review.removed_refs", for_symbol=c.symbol))
                elif isinstance(n, ast.Attribute) and n.attr == name and isinstance(n.value, ast.Name) and \
                        n.value.id in aliases:
                    out.append(_finding("public_api", "removed-still-used",
                                        f"{c.qual} was removed but {rel}:{n.lineno} still uses it", "statically_verified",
                                        f"{rel}:{n.lineno}", basis="module attribute of the removed name",
                                        derived_by="review.removed_refs", for_symbol=c.symbol))
            if rel == c.file:
                for n in ast.walk(tree):
                    if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id == name and \
                            name not in ((ctx.facts(rel) or {}).get("symbols") or {}):
                        out.append(_finding("public_api", "removed-still-used",
                                            f"{c.qual} was removed but its own module still uses it at line {n.lineno}",
                                            "strong_inference", f"{rel}:{n.lineno}", basis="name in the same module",
                                            derived_by="review.removed_refs", for_symbol=c.symbol))
    else:
        owner = _last(c.qual.rpartition(".")[0]) if "." in c.qual else ""
        rx = re.compile(rf"(?:\b{re.escape(owner)}\s*\.\s*|\.\s*)?\b{re.escape(name)}\s*\(" if owner else
                        rf"\b{re.escape(name)}\b")
        pkg = _jvm_package(ctx.text(c.file, "old") or "") if _suffix(c.file) in (".java", ".kt", ".kts") else None
        for rel in ctx.files():
            if _suffix(rel) not in CODE_SUFFIXES or rel == c.file:
                continue
            t = ctx.text(rel)
            if not t or name not in t or (owner and owner not in t) or (owner and not _jvm_sees(t, pkg, owner)):
                continue
            for i, ln in enumerate(ctx.code(rel), 1):
                if rx.search(ln):
                    out.append(_finding("public_api", "removed-still-used",
                                        f"{c.qual} was removed but {rel}:{i} names it", "strong_inference", f"{rel}:{i}",
                                        basis="text match in code (comments removed)", derived_by="review.removed_refs",
                                        for_symbol=c.symbol))
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


def _config(ctx: _Ctx, changes: list[Change]) -> list[dict]:
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def add(f: dict) -> None:
        key = (f["at"], f["rule"])
        if key not in seen:
            seen.add(key)
            out.append(f)

    cfg_keys = _repo_config_keys(ctx)
    for c in changes:
        if c.test:
            continue
        rel = c.file
        if c.kind == "config_key":
            ln = (c.lines or c.old_lines)[0]
            side = "new" if c.lines else "old"
            text = ctx.lines(rel, side)[ln - 1].strip()
            old = ctx.lines(rel, "old")[c.old_lines[0] - 1].strip() if c.old_lines else None
            readers = _key_readers(ctx, c.qual)
            add(_finding("config", "config-file-key", f"config key {c.qual} changed: "
                         + (f"`{old}` -> `{text}`" if old and c.lines else ("removed" if not c.lines else "added")),
                         "strong_inference", f"{rel}:{ln}", evidence_at=readers[:5],
                         basis="line diff of the config file; readers by literal key search in code",
                         derived_by="review.config_keys", for_symbol=c.symbol, readers=readers[:10] or None))
            continue
        if _suffix(rel) not in CODE_SUFFIXES:
            continue
        code = ctx.code(rel)
        for ln in sorted(c.new_changed):
            if ln > len(code):
                continue
            line = code[ln - 1]
            for rx, _lang in ENV_PATTERNS:
                m = rx.search(line)
                if m:
                    add(_finding("config", "env-read-on-changed-line",
                                 f"a changed line reads environment variable {m.group(1)}", "strong_inference",
                                 f"{rel}:{ln}", basis="environment-read pattern on the line",
                                 derived_by="architecture_map.ENV_PATTERNS", for_symbol=c.symbol, key=m.group(1)))
                    break
        if _suffix(rel) in (".py", ".pyi"):
            for f in _py_config_names(ctx, c):
                add(f)
        else:
            for f in _jvm_config(ctx, c, cfg_keys):
                add(f)
    return out


def _py_config_names(ctx: _Ctx, c: Change) -> list[dict]:
    """Names read on changed lines that are bound to an environment read or live in a config module."""
    out = []
    tree = ctx.pytree(c.file)
    if tree is None or not c.new_changed:
        return out
    imports = ctx._py_imports(c.file)
    for n in ast.walk(tree):
        if not (isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.lineno in c.new_changed):
            continue
        if c.kind == "module_statement":
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
        def_line = m["start"]
        text = " ".join(ctx.code(mrel)[def_line - 1: m["end"]])
        env = next((mm.group(1) for rx, _ in ENV_PATTERNS for mm in [rx.search(text)] if mm), None)
        if env or CONFIG_FILE_RE.search(mrel):
            out.append(_finding("config", "config-name-on-changed-line",
                                f"a changed line reads {name}" + (f", bound to environment variable {env}" if env else
                                                                  f", a value of the config module {mrel}")
                                + f" ({mrel}:{def_line})", "strong_inference", f"{c.file}:{n.lineno}",
                                evidence_at=[f"{mrel}:{def_line}"], basis="the name's binding followed through the "
                                "file's imports (syntax tree); the binding line read by the environment pattern",
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


def _key_readers(ctx: _Ctx, key: str) -> list[str]:
    last = key.rpartition(".")[2]
    if len(last) < 3:
        return []
    rx = re.compile(rf"[\"']{re.escape(last)}[\"']")
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


def _jvm_config(ctx: _Ctx, c: Change, cfg_keys: dict[str, list[str]]) -> list[dict]:
    out = []
    code, ocode = ctx.code(c.file), ctx.code(c.file, "old")
    for side, lines, src in (("new", c.new_changed, code), ("old", c.old_changed, ocode)):
        for ln in sorted(lines):
            if ln > len(src):
                continue
            line = src[ln - 1]
            for m in rr.QUOTED_KEY.finditer(line):
                key = m.group(1) or m.group(2)
                in_files = cfg_keys.get(key.rpartition(".")[2], [])
                if not (in_files or rr.CONFIG_CALL.search(line)):
                    continue
                if " " in key or key.count(":") or key.endswith((".png", ".json")):
                    continue
                at = f"{c.file}:{ln}"
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
            if member in ("get", "class", "INSTANCE") or not cls.endswith("Config"):
                continue
            where = _jvm_member_def(ctx, cls, member)
            out.append(_finding("config", "config-read-on-changed-line",
                                f"a changed line reads the config value {cls}.{member}"
                                + (f" (defined at {where})" if where else ""), "strong_inference", f"{c.file}:{ln}",
                                evidence_at=[where] if where else [], basis="config class read (text rule)",
                                derived_by="review.CONFIG_READ_JVM", for_symbol=c.symbol, key=f"{cls}.{member}"))
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
    """Why a unit is an entry point: map heuristics, registrations by method reference, input overrides."""
    reasons, at, kind = [], None, None
    if node is not None:
        reasons += entry_reasons(ctx.g, node)
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


def _hot_info(ctx: _Ctx, node: str | None, unit: tuple[str, str]) -> dict | None:
    for r in ctx.registered(*unit):
        if r["hot"]:
            return {"why": f"{r['what']} registered at {r['at']}", "at": r["at"],
                    "basis": "registration by method reference (text search, inference)"}
    rel, qual = unit
    name = _last(qual)
    if _suffix(rel) in (".java", ".kt", ".kts", ".js", ".ts") and name in rr.HOT_OVERRIDES:
        return {"why": f"{name}() is called by the game every tick or frame (by name)", "at": None,
                "basis": "method name table (inference)"}
    s = ctx.sym(rel, qual)
    if s and _suffix(rel) in (".java", ".kt"):
        head = "\n".join(ctx.lines(rel)[max(0, s["start"] - 1): s["def"] + 1])
        m = rr.EVENT_PARAM.search(head)
        if m:
            return {"why": f"@SubscribeEvent for {m.group(1)}", "at": f"{rel}:{s['start']}",
                    "basis": "event type in the handler's signature (text)"}
    if node is not None and _suffix(rel) in (".py", ".pyi"):
        e = entry_reasons(ctx.g, node)
        if any("decorator" in r or "entry-like name" in r for r in e):
            return {"why": "request/command handler (" + "; ".join(e) + ")", "at": f"{rel}:{s['def']}" if s else None,
                    "basis": "entry-point heuristics of the architecture map"}
    return None


def _entries(ctx: _Ctx, changes: list[Change], walk: dict) -> list[dict]:
    out: dict[str, dict] = {}
    g = ctx.g
    for c in _code_units(changes) + [c for c in changes if c.kind in ("removed", "module_statement") and not c.test]:
        unit = (c.file, c.qual) if c.qual and c.kind not in ("removed", "module_statement") else None
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
    """(test id -> {distance, reaches}, changed symbol -> [test ids]) over calls/uses/references, depth 3,
    through non-test code (the rule of runtime.trace.select_tests)."""
    tests: dict[str, dict] = {}
    per: dict[str, list[str]] = {}
    g = ctx.g
    if g is None:
        return tests, per
    for c in changes:
        if c.test or c.node is None or c.kind in ("removed",):
            continue
        per.setdefault(c.symbol, [])
        frontier, seen = {c.node}, {c.node}
        for dist in range(1, DEPTH + 1):
            nxt = set()
            for v in frontier:
                for u, _d in _in_edges(ctx, v, {"calls", "uses", "references"}):
                    if u in seen:
                        continue
                    seen.add(u)
                    tid = _test_id(ctx, u)
                    if tid:
                        t = tests.setdefault(tid, {"distance": dist, "reaches": []})
                        t["distance"] = min(t["distance"], dist)
                        if c.symbol not in t["reaches"]:
                            t["reaches"].append(c.symbol)
                        if tid not in per[c.symbol]:
                            per[c.symbol].append(tid)
                    elif g.file(u) and not _is_test(g.file(u)):
                        nxt.add(u)
            frontier = nxt
    return tests, per


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
    for c in changes:
        if c.kind in ("added", "body", "signature", "module_statement", "config_key", "file_only") and c.lines:
            a, b = c.lines
            if b - a > 60 and c.new_changed:   # a long definition: the changed lines with context
                a, b = max(a, min(c.new_changed) - 3), min(b, max(c.new_changed) + 3)
            items.append((c.file, a, b, "new", f"changed ({c.kind})"))
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
    # dependents by change kind
    walk = {}
    if g is not None:
        seeds_body = [(c.node, c) for c in changes if c.node and c.kind in ("body", "added") and not c.test]
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
        found["entry_points"] = _entries(ctx, changes, walk)
    for k in found:
        found[k].sort(key=lambda f: (rr.rank(f["status"]), f["at"] or ""))
    truncated_concerns = {k: len(v) for k, v in found.items() if len(v) > MAX_PER_CONCERN}
    shown = {k: v[:MAX_PER_CONCERN] for k, v in found.items()}
    # tests
    tests = _tests(ctx, changes, run_tests=run_tests, observe=observe, unknown=unknown)
    unknown += _callers_unknown(ctx, changes)
    cited = {c.file for c in changes} | {d["at"].rsplit(":", 1)[0] for d in dependents} | \
        {a.rsplit(":", 1)[0] for fs in shown.values() for f in fs for a in [f["at"], *f["evidence_at"]]
         if a and ":" in a}
    note = _graph_note(ctx, cited)
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
            names = [n for n in c.qual.split(",") if n]
        elif c.kind == "body" and (ctx.sym(c.file, c.qual) or {}).get("kind") == "class" and \
                _suffix(c.file) in (".java", ".kt"):
            code = ctx.code(c.file)
            for ln in sorted(c.new_changed):
                for k in range(ln, max(0, ln - 4), -1):
                    m = re.match(r"\s*([A-Z_][A-Z0-9_]*)\s*=", code[k - 1]) if k <= len(code) else None
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
        for rel in ctx.files():
            if not rel.endswith((".py", ".pyi")):
                continue
            t = ctx.text(rel)
            if not t or name not in t:
                continue
            tree = ctx.pytree(rel)
            if tree is None:
                continue
            imports = ctx._py_imports(rel)
            local = {a for a, (m, orig) in imports.items() if orig == name and m == mod}
            if rel == c.file:
                local.add(name)
            for n in ast.walk(tree):
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id in local:
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
                 "basis": "static: reverse reach over calls/uses/references (depth 3, through non-test code)"}
    if observed:
        out["observed"] = observed
    reached_any = {c.symbol for c in code_changes if per.get(c.symbol)} | \
        {s for s, t in ((observed or {}).get("reached") or {}).items() if t}
    out["no_test_reaches"] = [c.symbol for c in code_changes if c.symbol not in reached_any]
    langs = {_lang(c.file) for c in code_changes}
    py_ids = [t for t in static if t.split("::")[0].endswith(".py")]
    if langs - {"python"}:
        others = ", ".join(sorted({"jvm": "Java/Kotlin"}.get(x, x) for x in langs - {"python"}))
        unknown.append({"kind": "runtime_tests", "what": f"what the tests of the {others} code do at run time",
                        "why": "Verinoda runs and traces pytest only; Gradle, Maven and other runners are not "
                               "allowlisted", "next_step": "run the project's tests yourself (e.g. ./gradlew test) "
                                                           "and report the result"})
    if observe and py_ids:
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
                          "limits": res.get("limits"), "error": res.get("error")}
        reached = {t for v in by_symbol.values() for t in v}
        out["no_test_reaches"] = [s for s in out["no_test_reaches"] if not by_symbol.get(s)]
        for t in out["static"]:
            if t["test"] in reached:
                t["observed"] = "reached"
            elif t["test"] in out["observe"]["selected_not_reaching"]:
                t["observed"] = "not reached"
    if run_tests:
        if py_ids:
            from verinoda import experiments

            argv = [experiments.python_for(ctx.repo), "-m", "pytest", "-q", "-p", "no:cacheprovider", *py_ids[:50]]
            try:
                exp = experiments.run(ctx.store, ctx.repo, argv, hypothesis="the tests that reach the reviewed change "
                                      "pass", expect="pass")
                out["run"] = {"experiment": exp.get("id"), "outcome": exp.get("outcome"),
                              "summary": exp.get("summary"), "tests": len(py_ids[:50]),
                              "tree": (exp.get("tree") or {}).get("hash") if isinstance(exp.get("tree"), dict)
                              else exp.get("tree"), "logs": exp.get("logs")}
            except experiments.ExperimentRefused as exc:
                out["run"] = {"refused": str(exc)}
        else:
            out["run"] = {"skipped": "no pytest test reaches the change statically"}
    return out


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
                 "why": f"{len(note['stale_files'])} file(s) differ from the snapshot the graph was built from",
                 "next_step": "run `verinoda update` and review again"}]
    return []


def _graph_note(ctx: _Ctx, cited: set[str]) -> dict:
    """The snapshot the graph comes from, and which of the ``cited`` files (outside the diff) changed since."""
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
                cur = hashlib.sha256((ctx.repo / rel).read_bytes()).hexdigest()
            except OSError:
                cur = None
            if recorded.get(rel) not in (None, cur):
                stale.append(rel)
    note = {"snapshot": (snap or {}).get("id"), "commit": (snap or {}).get("commit_sha"),
            "changed_files_reparsed": sorted(ctx.base_texts),
            "note": "graph edges come from the last snapshot; the changed files are read from the tree under "
                    "review (Python calls re-parsed from its syntax tree, other languages' calls re-found by name)",
            "stale_files": stale}
    return note


def _summary(res: dict) -> str:
    ch = res["changes"]
    where = ("the planned change" if res["mode"] == "planned" else
             f"the {'staged changes' if res['mode'] == 'staged' else 'working tree'} against "
             f"{res['base']['ref']} ({res['base']['commit'][:10]})")
    if not ch:
        return f"Review of {where}: no changed definition (comments, whitespace and docstrings are not changes)."
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
    if t.get("observe"):
        ob = t["observe"]
        out.append(f"  observed (run {ob.get('run')}): " + "; ".join(f"{s.split('::')[-1]} reached by {len(v)}"
                                                                     for s, v in (ob.get("reached") or {}).items())
                   + (f"; selected but not reaching: {', '.join(ob['selected_not_reaching'])}"
                      if ob.get("selected_not_reaching") else ""))
    if t.get("run"):
        r = t["run"]
        out.append(f"  run: {r.get('outcome') or r.get('refused') or r.get('skipped')}"
                   + (f" ({r.get('summary')})" if r.get("summary") else "") + (f", experiment {r['experiment']}"
                                                                                 if r.get("experiment") else ""))
    if t.get("no_test_reaches"):
        out.append("  no test reaches: " + ", ".join(t["no_test_reaches"][:6]))
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
