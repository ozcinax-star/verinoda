"""Precise call-site resolution, lazily and cached (docs/DESIGN.md D29).

Optional: needs the ``precise`` extra (``pip install 'repoatlas[precise]'`` =
jedi). Without it every function here answers ``None`` ("no precise answer"),
and callers keep their current behaviour.

:func:`resolve_call` answers one question for one call site: *which definition
does the call to ``target_label`` on ``path:line`` bind to?* The answer's
``kind``:

``definitive``
    exactly one in-repository function or class, reached by name
    (``f()``, ``mod.f()``, ``Cls()``, ``Cls.m()``) or through ``self`` /
    ``cls`` / ``super()``. Equal to the graph's target: the edge is statically
    verified. Different: a definitive refutation of the edge.
``dynamic``
    the binding depends on runtime values: a call through a parameter or a
    local variable (``cb()``, ``make()()``), or a method called on a
    parameter/local/expression receiver (``repo.save()`` - a subclass or a
    double may be passed). Inferred candidates are listed; never a refutation.
``ambiguous``
    several definitions (conditional or repeated definitions, unions).
``external``
    library, builtin or stub code outside the repository.
``unresolved``
    jedi found nothing, or no call to that name on the line.

With ``target_path``/``target_line`` the result also carries ``verdict``:
``confirms`` / ``refutes`` (definitive answers only) or ``undetermined``.

Cost control: results are cached in the ``resolutions`` table keyed by the
file's sha256 (so edits invalidate them) and in-process; one ``jedi.Project``
per repository and a few ``jedi.Script`` objects are kept warm; a
:class:`Budget` (default <= 20 sites or <= 1.5 s per analysis, config
``budget.precise_sites`` / ``budget.precise_seconds``) bounds fresh work.
When it is spent :func:`resolve_call` returns ``None`` and
``budget.skipped`` counts the sites left unresolved ("not resolved (budget)").

SCIP: for non-Python files, a fresh ``index.scip`` found by
:mod:`repoatlas.scip_reader` answers instead. It is *not* used for Python:
scip-python symbols are name-based and confirmed all 13 shadowed-duplicate
errors that jedi caught on Graphify's own code (docs/DESIGN.md 4.1).
"""

from __future__ import annotations

import ast
import hashlib
import json
import time
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from pathlib import Path

from repoatlas.store import Store, now

KINDS = ("definitive", "dynamic", "ambiguous", "unresolved", "external")
DEFAULT_SITES = 20
DEFAULT_SECONDS = 1.5
SCRIPT_CACHE = 16
_VENV_PARTS = {".venv", "venv", "site-packages", "node_modules", ".tox", ".nox"}
_CTOR_NAMES = ("__init__", "__new__", "__call__")


@dataclass
class Budget:
    """Fresh (uncached) resolutions allowed per analysis."""

    max_sites: int = DEFAULT_SITES
    max_seconds: float = DEFAULT_SECONDS
    sites: int = 0
    spent_s: float = 0.0
    skipped: int = 0

    @classmethod
    def from_config(cls, repo: Path) -> "Budget":
        from repoatlas.paths import load_config

        b = load_config(repo).get("budget", {})
        return cls(int(b.get("precise_sites", DEFAULT_SITES)), float(b.get("precise_seconds", DEFAULT_SECONDS)))

    def allow(self) -> bool:
        if self.sites >= self.max_sites or self.spent_s >= self.max_seconds:
            self.skipped += 1
            return False
        return True

    def charge(self, seconds: float) -> None:
        self.sites += 1
        self.spent_s += seconds

    def as_dict(self) -> dict:
        return {"max_sites": self.max_sites, "max_seconds": self.max_seconds, "sites": self.sites,
                "spent_s": round(self.spent_s, 3), "skipped": self.skipped}


def available() -> tuple[bool, str]:
    """(True, 'jedi <version>') or (False, why) - never raises."""
    try:
        import jedi
    except ImportError:
        return False, "jedi is not installed (pip install 'repoatlas[precise]')"
    return True, f"jedi {jedi.__version__}"


# -- call sites ------------------------------------------------------------------------------

@dataclass
class CallSite:
    line: int          # line of the callee token
    col: int           # 0-based *character* column of the callee token (jedi's convention)
    token: str
    receiver: str      # "name" (f()), "self" (self./cls./super().), "attribute" (x.f()), "expression"
    base_pos: tuple[int, int] | None = None   # (line, char col) of the receiver's last name token


def _char_col(line_text: str, byte_col: int) -> int:
    """ast columns are UTF-8 byte offsets; jedi wants characters."""
    return len(line_text.encode("utf-8")[:byte_col].decode("utf-8", "replace"))


def _token_pos(node: ast.AST, lines: list[str]) -> tuple[int, int] | None:
    """(line, char col) of the last name token of a Name / Attribute expression."""
    if isinstance(node, ast.Name):
        ln, bcol = node.lineno, node.col_offset
    elif isinstance(node, ast.Attribute) and node.end_lineno is not None and node.end_col_offset is not None:
        ln, bcol = node.end_lineno, node.end_col_offset - len(node.attr.encode("utf-8"))
    else:
        return None
    return (ln, _char_col(lines[ln - 1], bcol)) if 0 < ln <= len(lines) else None


class FileIndex:
    """Call and name nodes of one parsed file, by line (built once per file content)."""

    def __init__(self, tree: ast.AST | None, lines: list[str]):
        self.tree, self.lines = tree, lines
        self.calls: dict[int, list[ast.Call]] = defaultdict(list)
        self.loads: dict[int, list[ast.AST]] = defaultdict(list)
        self.callees: set[int] = set()
        for n in ast.walk(tree) if tree is not None else ():
            if isinstance(n, ast.Call):
                self.callees.add(id(n.func))
                self.calls[n.lineno].append(n)
                end = getattr(n.func, "end_lineno", None)
                if end and end != n.lineno:
                    self.calls[end].append(n)
            elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                self.loads[n.lineno].append(n)
            elif isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Load) and n.end_lineno:
                self.loads[n.end_lineno].append(n)


def call_sites(tree: ast.AST | FileIndex, lines: list[str], line: int) -> list[CallSite]:
    """Every call whose expression starts on ``line`` or whose callee token is on it."""
    nodes = tree.calls.get(line, []) if isinstance(tree, FileIndex) else \
        [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    out: list[CallSite] = []
    for node in nodes:
        f = node.func
        pos = _token_pos(f, lines)
        if pos is None or line not in (node.lineno, pos[0]):
            continue
        if isinstance(f, ast.Name):
            out.append(CallSite(pos[0], pos[1], f.id, "name"))
            continue
        assert isinstance(f, ast.Attribute)
        base = f.value
        if (isinstance(base, ast.Name) and base.id in ("self", "cls")) or \
                (isinstance(base, ast.Call) and isinstance(base.func, ast.Name) and base.func.id == "super"):
            out.append(CallSite(pos[0], pos[1], f.attr, "self"))
        elif isinstance(base, (ast.Name, ast.Attribute)):
            out.append(CallSite(pos[0], pos[1], f.attr, "attribute", _token_pos(base, lines)))
        else:
            out.append(CallSite(pos[0], pos[1], f.attr, "expression"))
    out.sort(key=lambda c: (c.line, c.col))
    return out


def reference_sites(tree: ast.AST | FileIndex, lines: list[str], line: int, token: str) -> list[CallSite]:
    """Loads of ``token`` on ``line`` that are not the callee of a call (callbacks, aliases)."""
    idx = tree if isinstance(tree, FileIndex) else FileIndex(tree, lines)
    out: list[CallSite] = []
    for node in idx.loads.get(line, []):
        if id(node) in idx.callees:
            continue
        if isinstance(node, ast.Name) and node.id == token:
            rec = "name"
            base_pos = None
        elif isinstance(node, ast.Attribute) and node.attr == token:
            base = node.value
            rec = "self" if isinstance(base, ast.Name) and base.id in ("self", "cls") else "attribute"
            base_pos = _token_pos(base, lines) if rec == "attribute" else None
        else:
            continue
        pos = _token_pos(node, lines)
        if pos is not None and pos[0] == line:
            out.append(CallSite(pos[0], pos[1], token, rec, base_pos))
    out.sort(key=lambda c: (c.line, c.col))
    return out


def target_token(target_label: str) -> str:
    """``.save()`` / ``OrderRepository.save`` / ``pkg.mod.f`` -> the callee name at the call site."""
    lab = target_label.strip().lstrip(".").split("(")[0].strip()
    return lab.rpartition(".")[2] or lab


# -- jedi resolver ---------------------------------------------------------------------------

def _def_lines(tree: ast.AST | None) -> dict[int, int]:
    """Map every decorator line and def line of a definition to its ``def``/``class`` line."""
    m: dict[int, int] = {}
    if tree is None:
        return m
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            first = min([n.lineno] + [d.lineno for d in n.decorator_list])
            for ln in range(first, n.lineno + 1):
                m.setdefault(ln, n.lineno)
    return m


class JediResolver:
    """jedi goto/infer per call site; one warm ``jedi.Project`` per repository."""

    def __init__(self, repo: Path):
        import jedi

        self.jedi = jedi
        self.repo = Path(repo).resolve()
        self.tool = f"jedi {jedi.__version__}"
        self.project = jedi.Project(str(self.repo), smart_sys_path=True)
        self._scripts: OrderedDict = OrderedDict()

    def _script(self, abs_path: Path, sha: str, source: str):
        key = (str(abs_path), sha)
        sc = self._scripts.get(key)
        if sc is None:
            sc = self.jedi.Script(source, path=str(abs_path), project=self.project)
            self._scripts[key] = sc
            while len(self._scripts) > SCRIPT_CACHE:
                self._scripts.popitem(last=False)
        else:
            self._scripts.move_to_end(key)
        return sc

    def _in_repo(self, module_path) -> str | None:
        if not module_path:
            return None
        try:
            rel = Path(module_path).resolve().relative_to(self.repo)
        except (ValueError, OSError):
            return None
        return None if set(rel.parts) & _VENV_PARTS else rel.as_posix()

    def _describe(self, d) -> dict:
        rel = self._in_repo(d.module_path)
        return {"path": rel, "line": d.line, "name": d.name, "qualname": d.full_name, "type": d.type,
                "in_repo": rel is not None, "builtin": bool(d.in_builtin_module())}

    def _receiver(self, script, site: CallSite) -> str:
        """name | self | module | class | parameter | local | expression."""
        if site.receiver != "attribute" or site.base_pos is None:
            return site.receiver
        try:
            defs = script.goto(*site.base_pos, follow_imports=True)
        except Exception:  # noqa: BLE001 - jedi internal errors happen
            defs = []
        types = {d.type for d in defs}
        if types and types <= {"module"}:
            return "module"
        if types and types <= {"class"}:
            return "class"
        return "parameter" if "param" in types else "local"

    def resolve_site(self, script, site: CallSite) -> dict:
        try:
            defs = script.goto(site.line, site.col, follow_imports=True)
        except Exception as exc:  # noqa: BLE001
            return {"kind": "unresolved", "targets": [], "reason": f"jedi error: {type(exc).__name__}"}
        receiver = self._receiver(script, site)
        if not defs:
            return {"kind": "unresolved", "targets": [], "receiver": receiver, "reason": "jedi found no definition"}
        # one entry per definition site (a re-export can reach the same def under another full_name)
        uniq = {(d.module_path and str(d.module_path), d.line, d.column): d for d in defs}
        described = [self._describe(d) for d in uniq.values()]
        types = {d["type"] for d in described}
        if types & {"param", "statement", "instance"}:
            try:
                inferred = script.infer(site.line, site.col)
            except Exception:  # noqa: BLE001
                inferred = []
            cands = [self._describe(d) for d in {(d.module_path and str(d.module_path), d.line): d
                                                 for d in inferred}.values()]
            what = "a parameter" if "param" in types else "a local or global variable"
            return {"kind": "dynamic", "targets": cands, "receiver": receiver,
                    "reason": f"the callee is {what}: its value is only known at runtime"}
        in_repo = [d for d in described if d["in_repo"]]
        if len(described) > 1:
            return {"kind": "ambiguous", "targets": described, "receiver": receiver,
                    "reason": f"{len(described)} definitions"}
        d = described[0]
        if not in_repo:
            return {"kind": "external", "targets": described, "receiver": receiver,
                    "reason": "the definition is outside the repository"}
        if d["type"] not in ("function", "class"):
            return {"kind": "dynamic", "targets": described, "receiver": receiver,
                    "reason": f"jedi resolved a {d['type']}, not a function or class"}
        if receiver in ("parameter", "local", "expression"):
            return {"kind": "dynamic", "targets": described, "receiver": receiver,
                    "reason": f"method called on a {receiver} receiver: the runtime class (a subclass or a "
                              "test double) decides which method runs"}
        return {"kind": "definitive", "targets": described, "receiver": receiver, "reason": None}


_RESOLVERS: dict[str, JediResolver] = {}
_MEMO: dict[tuple, dict] = {}
_MEMO_MAX = 20000
_PARSED: OrderedDict = OrderedDict()   # (path, sha256) -> FileIndex; a few big files dominate
_PARSED_MAX = 32
_DEF_LINES: dict[tuple, dict[int, int]] = {}


def _parsed(key: tuple, source: str) -> FileIndex:
    hit = _PARSED.get(key)
    if hit is None:
        try:
            tree: ast.AST | None = ast.parse(source)
        except (SyntaxError, ValueError):
            tree = None
        hit = _PARSED[key] = FileIndex(tree, source.splitlines())
        while len(_PARSED) > _PARSED_MAX:
            _PARSED.popitem(last=False)
    else:
        _PARSED.move_to_end(key)
    return hit


def _def_lines_of(path: Path) -> dict[int, int]:
    """:func:`_def_lines` of a file, cached by its content hash."""
    try:
        data = path.read_bytes()
    except OSError:
        return {}
    key = (str(path), hashlib.sha256(data).hexdigest())
    if key not in _DEF_LINES:
        if len(_DEF_LINES) > 256:
            _DEF_LINES.clear()
        _DEF_LINES[key] = _def_lines(_parsed(key, data.decode("utf-8", "replace")).tree)
    return _DEF_LINES[key]


def jedi_resolver(repo: Path) -> JediResolver | None:
    """The warm resolver for ``repo``, or None when jedi is not installed."""
    key = str(Path(repo).resolve())
    r = _RESOLVERS.get(key)
    if r is None:
        if not available()[0]:
            return None
        r = _RESOLVERS[key] = JediResolver(Path(key))
    return r


def _cache_get(store: Store | None, key: tuple) -> dict | None:
    hit = _MEMO.get(key)
    if hit is not None or store is None:
        return hit
    row = store.one("SELECT result FROM resolutions WHERE path=? AND file_sha256=? AND line=? AND token=? AND tool=?",
                    key)
    if row is None:
        return None
    res = row["result"] if isinstance(row["result"], dict) else json.loads(row["result"])
    _MEMO[key] = res
    return res


def _cache_put(store: Store | None, key: tuple, result: dict) -> None:
    if len(_MEMO) > _MEMO_MAX:
        _MEMO.clear()
    _MEMO[key] = result
    if store is not None:
        with store.tx() as conn:
            conn.execute("INSERT OR IGNORE INTO resolutions (path, file_sha256, line, token, tool, result, created_at)"
                         " VALUES (?,?,?,?,?,?,?)", (*key, json.dumps(result, sort_keys=True), now()))


def _merge(results: list[dict]) -> dict:
    """Several call sites of the same name on one line: one answer."""
    if len(results) == 1:
        return results[0]
    kinds = {r["kind"] for r in results}
    tset = {(t["path"], t["line"]) for r in results for t in r["targets"]}
    if kinds == {"definitive"} and len(tset) == 1:
        return results[0]
    targets = [t for r in results for t in r["targets"]]
    return {"kind": "ambiguous", "targets": targets, "receiver": results[0].get("receiver"),
            "reason": f"{len(results)} calls to this name on the line resolve differently"}


def _verdict(repo: Path, res: dict, target_path: str | None, target_line: int | None) -> str:
    if res["kind"] != "definitive" or not target_path:
        return "undetermined"
    tp = target_path.replace("\\", "/")
    want = None
    if target_line:
        want = _def_lines_of(repo / tp).get(int(target_line), int(target_line))
    for t in res["targets"]:
        if t["path"] == tp and (want is None or want in (t["line"], t.get("init_line"))):
            return "confirms"
    # Class(...) resolved to a class whose own __init__ is not the target: the target may be an
    # inherited __init__ - not a refutation.
    return "undetermined" if res.get("ctor") else "refutes"


def resolve_call(repo: Path, path: str, line: int, target_label: str, *, store: Store | None = None,
                 budget: Budget | None = None, target_path: str | None = None,
                 target_line: int | None = None) -> dict | None:
    """Resolve the call to ``target_label`` at ``path:line`` (see the module docstring).

    None means no precise answer: jedi not installed (Python) and no fresh SCIP
    document (other languages), the file is unreadable, or ``budget`` is
    spent (``budget.skipped`` counts those). Cached answers cost no budget.
    """
    repo = Path(repo).resolve()
    rel = path.replace("\\", "/")
    abs_path = (repo / rel).resolve()
    try:
        abs_path.relative_to(repo)
        data = abs_path.read_bytes()
    except (ValueError, OSError):
        return None
    token = target_token(target_label)
    if not rel.endswith((".py", ".pyi")):
        return _resolve_scip(repo, rel, int(line), token, store=store, target_path=target_path,
                             target_line=target_line)
    resolver = jedi_resolver(repo)
    if resolver is None:
        return None
    out = _lookup(resolver, abs_path, data, hashlib.sha256(data).hexdigest(), rel, int(line), token,
                  store=store, budget=budget)
    if out is not None and target_path:
        out["verdict"] = _verdict(repo, out, target_path, target_line)
    return out


def _lookup(resolver: JediResolver, abs_path: Path, data: bytes, sha: str, rel: str, line: int, token: str, *,
            store: Store | None, budget: Budget | None) -> dict | None:
    key = (rel, sha, line, token, resolver.tool)
    cached = _cache_get(store, key)
    if cached is not None:
        return dict(cached, cached=True)
    if budget is not None and not budget.allow():
        return None
    t0 = time.perf_counter()
    out = _resolve_fresh(resolver, abs_path, data, sha, rel, line, token)
    elapsed = time.perf_counter() - t0
    if budget is not None:
        budget.charge(elapsed)
    _cache_put(store, key, out)
    return dict(out, cached=False, elapsed_ms=round(elapsed * 1000, 1))


def _resolve_fresh(resolver: JediResolver, abs_path: Path, data: bytes, sha: str, rel: str, line: int,
                   token: str) -> dict:
    source = data.decode("utf-8", "replace")
    base = {"tool": resolver.tool, "path": rel, "line": line, "token": token}
    fidx = _parsed((str(abs_path), sha), source)
    if fidx.tree is None:
        return {**base, "kind": "unresolved", "targets": [], "reason": "the file does not parse"}
    tree, lines = fidx, fidx.lines
    sites = call_sites(fidx, lines, line)
    chosen = [s for s in sites if s.token == token]
    ctor = False
    if not chosen and token in _CTOR_NAMES:
        chosen, ctor = sites, True   # Class(...) runs Class.__init__: resolve every call on the line
    if not chosen and not sites and not reference_sites(tree, lines, line, token):
        return {**base, "kind": "unresolved", "targets": [], "reason": f"no call to {token!r} on line {line}"}
    script = resolver._script(abs_path, sha, source)
    if not chosen:
        # An aliased call (from m import helper as h2; h2()) binds to a definition named token.
        alias = [(st, r) for st, r in ((st, resolver.resolve_site(script, st)) for st in sites)
                 if any(t["name"] == token for t in r["targets"])]
        if alias:
            res = _merge([r for _, r in alias])
            return {**base, "col": alias[0][0].col, "alias": alias[0][0].token, **res}
        refs = reference_sites(tree, lines, line, token)
        if not refs:
            return {**base, "kind": "unresolved", "targets": [], "reason": f"no call to {token!r} on line {line}"}
        res = _merge([resolver.resolve_site(script, st) for st in refs])
        if res["kind"] == "definitive":
            res = {**res, "kind": "dynamic",
                   "reason": f"{token!r} is referenced, not called, on this line (e.g. passed as a callback): "
                             "whether and when it runs is decided at runtime"}
        return {**base, "col": refs[0].col, "site": "reference", **res}
    results = [resolver.resolve_site(script, st) for st in chosen]
    if ctor:
        results = [r for r in results if any(t["type"] == "class" for t in r["targets"])] or \
            [{"kind": "unresolved", "targets": [], "reason": f"no class instantiation on line {line}"}]
        for r in results:
            for t in r["targets"]:
                if t["type"] == "class" and t["in_repo"]:
                    t["init_line"] = _method_line(resolver.repo / t["path"], t["line"], token)
    res = _merge(results)
    return {**base, "col": chosen[0].col, **res, **({"ctor": True} if ctor else {})}


def _method_line(path: Path, class_line: int, name: str) -> int | None:
    """Line of ``name`` defined directly in the class at ``class_line`` (None: inherited)."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    tree = _parsed((str(path), hashlib.sha256(data).hexdigest()), data.decode("utf-8", "replace")).tree
    if tree is None:
        return None
    for n in ast.walk(tree):
        if isinstance(n, ast.ClassDef) and n.lineno == class_line:
            for m in n.body:
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and m.name == name:
                    return m.lineno
    return None


def resolution_evidence(repo: Path, res: dict, *, commit: str | None = None) -> dict | None:
    """A ``static_resolution`` evidence dict for a :func:`resolve_call` result (not inserted).

    Anchored on the call line (path, line, content hash) so it goes stale with
    the call site; ``meta`` carries the tool, kind, targets (with the hash of
    each in-repo target's definition line) and, when the caller passed a
    target, the verdict. The ``static_resolution`` source type (rank 2,
    verifying) is registered by :mod:`repoatlas.evidence`; only a
    ``definitive`` answer may verify or refute an edge.
    """
    from repoatlas import evidence as evmod

    repo = Path(repo).resolve()
    targets = []
    for t in res.get("targets", [])[:5]:
        t = dict(t)
        if t.get("in_repo") and t.get("path") and t.get("line"):
            lines = _read_lines(repo / t["path"])
            text = lines[t["line"] - 1] if lines and 0 < t["line"] <= len(lines) else None
            t["def_hash"] = evmod.content_hash(text) if text and text.strip() else None
        targets.append(t)
    meta = {"tool": res.get("tool"), "kind": res.get("kind"), "targets": targets, "token": res.get("token"),
            "receiver": res.get("receiver"), "reason": res.get("reason"),
            **{k: res[k] for k in ("verdict", "alias", "site", "ctor", "level", "name_based", "uncertainty")
               if k in res}}
    ev = evmod.source_evidence(repo, res["path"], int(res["line"]), commit=commit, source_type="static_resolution",
                               meta=meta)
    if ev is not None:
        ev["locator"] = f"{res['path']}:{res['line']} {res.get('token')} -> {res.get('kind')} ({res.get('tool')})"
    return ev


def _read_lines(p: Path) -> list[str] | None:
    try:
        return p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None


def resolve_file(repo: Path, path: str, *, store: Store | None = None) -> list[dict]:
    """Resolve every call site in one Python file (batch use: ``scan --precise``, audits)."""
    repo = Path(repo).resolve()
    rel = path.replace("\\", "/")
    resolver = jedi_resolver(repo)
    if resolver is None:
        return []
    abs_path = (repo / rel).resolve()
    try:
        data = abs_path.read_bytes()
        tree = ast.parse(data.decode("utf-8", "replace"))
    except (OSError, SyntaxError, ValueError):
        return []
    sha = hashlib.sha256(data).hexdigest()
    out = []
    seen = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            tok = f.id if isinstance(f, ast.Name) else getattr(f, "attr", None)
            if tok and (node.lineno, tok) not in seen:
                seen.add((node.lineno, tok))
                r = _lookup(resolver, abs_path, data, sha, rel, node.lineno, tok, store=store, budget=None)
                if r is not None:
                    out.append(r)
    return out


# -- SCIP (non-Python) -----------------------------------------------------------------------

_SCIP: dict[str, object] = {}


def _resolve_scip(repo: Path, rel: str, line: int, token: str, *, store: Store | None,
                  target_path: str | None, target_line: int | None) -> dict | None:
    try:
        from repoatlas import scip_reader
    except ImportError:  # pragma: no cover - same package
        return None
    key = str(repo)
    res = _SCIP.get(key)
    if res is None:
        found = scip_reader.find_index(repo)
        if found is None:
            return None
        try:
            res = scip_reader.ScipResolver.load(repo, found)
        except (OSError, ValueError) as exc:  # corrupt or unreadable index
            _SCIP[key] = False
            return {"kind": "unresolved", "targets": [], "tool": "scip", "path": rel, "line": line,
                    "token": token, "reason": f"index.scip unreadable: {exc}"}
        _SCIP[key] = res
    if res is False:
        return None
    out = res.resolve(rel, line, token)
    if out is not None and target_path:
        tp = target_path.replace("\\", "/")
        if out["kind"] != "definitive":
            out["verdict"] = "undetermined"
        else:
            hit = any(t["path"] == tp and (not target_line or t["line"] == int(target_line)) for t in out["targets"])
            out["verdict"] = "confirms" if hit else "refutes"
    return out


def reset_caches() -> None:
    """Forget warm resolvers and memoised answers (tests; after a jedi upgrade)."""
    _RESOLVERS.clear()
    _MEMO.clear()
    _SCIP.clear()
    _PARSED.clear()
    _DEF_LINES.clear()
