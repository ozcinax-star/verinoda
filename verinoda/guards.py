"""Guards: the mechanical checks of recorded decisions against the code (docs/DESIGN.md D33).

One rule engine, used by ``verinoda decide check`` (and MCP ``decision_check``), by critique's
exclusivity check and by feedback's exclusive corrections. Each finding has a level:

* ``VIOLATED`` - statically verified: the cited line re-checks now and binds to what the guard forbids;
* ``POSSIBLE`` - a heuristic hit (a textual match in a language without a binding check, an INFERRED
  graph edge, a receiver whose type is not resolved, ``getattr(module, "name")``); never a violation;
* ``REVIEW`` - code a decision governs changed since the decision was recorded (never a violation);
* ``TRIGGER`` - a revisit condition of a decision holds now: the human should look at it again;
* ``ok`` - the guard holds, always with its scope and limits (a narrow check must not look like a
  broad guarantee); ``unknown`` - the guard could not be checked (why, and the next step).

Guard kinds (spec grammar in :mod:`verinoda.decisions`):

``only_in`` - a call may appear only in the allowed files. The scope is the product code by default:
tests, configured reference trees, detected copies of the project and the decision folder are left
out (``scope=all`` keeps them). ``calls=mod.func``:

* Python: a syntax-tree walk that binds names through imports (``import m as a``, ``from m import f as
  g``, relative imports), simple assignments (``g = m.f``) and one or more re-exports through the
  project's own modules (``from pkg.util import g`` where ``pkg/util.py`` imports it). A name rebound
  locally (a parameter, an assignment in the function) or at module level gives POSSIBLE, so does
  ``getattr(m, "f")`` and a star import. Comments and strings are never read as code.
* Java / Kotlin: import-bound calls through the syntax tree. ``Cls.method(...)`` with ``Cls`` imported
  (explicitly, by a static import, with ``pkg.*`` or from the same package), or ``r.method(...)`` where
  ``r`` is declared ``Cls r`` / ``r: Cls`` / ``val r = Cls(...)`` in the same file, is VIOLATED; any
  other receiver (a call chain, an undeclared name, a field of another class) is POSSIBLE.
* Other languages: a regex over the code with comments and strings removed: POSSIBLE at most.

``sink=db-connection`` checks the known connection calls (``sqlite3.connect``, ``psycopg.connect``,
``create_engine``, ``MongoClient``, JDBC ``DriverManager.getConnection`` ...) the same way; the other
sink kinds and ``pattern=REGEX`` are text matches: POSSIBLE at most.

``no_edge`` - no graph edge from files matching ``from`` to files matching ``to``. An EXTRACTED edge
whose cited line still names the target in code is VIOLATED; an INFERRED edge, or one whose line
changed since the index was built, is POSSIBLE.

``dependency`` - ``absent=NAME``: declared in a manifest is VIOLATED (the manifest line is cited);
``present=NAME``: missing from every manifest read is VIOLATED.

Nothing here imports, runs or evaluates code of the analysed repository: files are read as text and
parsed (``ast``, tree-sitter). ``git`` gets validated refs only (``rev-parse --verify
--end-of-options``) and ``--`` before paths.
"""

from __future__ import annotations

import ast
import fnmatch
import io
import re
import time
import tokenize
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

VIOLATED, POSSIBLE, REVIEW, TRIGGER = "VIOLATED", "POSSIBLE", "REVIEW", "TRIGGER"
PY_SUFFIXES = (".py", ".pyi")
JVM_SUFFIXES = (".java", ".kt", ".kts")
CODE_SUFFIXES = (".py", ".pyi", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".go", ".rs", ".java", ".kt", ".kts",
                 ".scala", ".rb", ".php", ".cs", ".c", ".h", ".cc", ".cpp", ".hpp", ".swift", ".lua", ".groovy",
                 ".dart", ".sh", ".ps1")
HASH_COMMENT = (".py", ".pyi", ".rb", ".sh", ".ps1")
# known connection calls per sink kind (the call-based part of architecture_map.SINK_PATTERNS, plus JDBC)
SINK_CALLS = {
    "db-connection": ("sqlite3.connect", "psycopg2.connect", "psycopg.connect", "sqlalchemy.create_engine",
                      "pymongo.MongoClient", "pymysql.connect", "mysql.connector.connect", "asyncpg.connect",
                      "aiosqlite.connect", "java.sql.DriverManager.getConnection"),
}
PY_LIMITS = ["getattr with a computed name, exec / eval, sys.modules and calls through objects (a method of an "
             "instance a call returned) are not followed; getattr(module, \"name\") with a literal name, "
             "importlib / __import__ of the module and star imports are reported as POSSIBLE",
             "re-exports are followed through the project's own modules (4 levels), not through installed packages",
             "files that do not parse are reported as unknown, not checked"]
JVM_LIMITS = ["a receiver's type is read from its declaration in the same file only; calls through inherited "
              "methods, reflection, method references and fields of other classes are POSSIBLE or not seen"]
TEXT_LIMITS = ["languages other than Python, Java and Kotlin are matched as text with comments and strings "
               "removed: POSSIBLE at most"]
EDGE_LIMITS = ["edges are what the index extracted: reflection, string class loading, service loaders, DI "
               "containers and build-time wiring are not seen"]
DEP_LIMITS = ["transitive dependencies, vendored code and manifests other than those listed are not read"]


@dataclass
class Finding:
    decision: str
    guard: str
    kind: str
    level: str
    at: str | None
    why: str
    line: str | None = None
    status: str | None = None
    since: str | None = None

    def as_dict(self) -> dict:
        d = {"decision": self.decision, "guard": self.guard, "kind": self.kind, "level": self.level, "at": self.at,
             "why": self.why}
        for k in ("line", "status", "since"):
            if getattr(self, k):
                d[k] = getattr(self, k)
        return d


@dataclass
class Scan:
    """What one guard looked at: counts per engine, limits, and anything it could not check."""
    files: dict[str, int] = field(default_factory=dict)
    limits: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)

    def count(self, engine: str) -> None:
        self.files[engine] = self.files.get(engine, 0) + 1

    def limit(self, *items: str) -> None:
        for s in items:
            if s not in self.limits:
                self.limits.append(s)


# -- comments and strings -------------------------------------------------------------------------

def _blank(s: str) -> str:
    return "".join("\n" if ch == "\n" else " " for ch in s)


def code_text(text: str, suffix: str) -> str:
    """``text`` with comments and string literals blanked out (line and column positions kept)."""
    suffix = suffix.lower()
    if suffix in PY_SUFFIXES:
        masked = _py_code(text)
        if masked is not None:
            return masked
    return _clike_code(text, hash_comments=suffix in HASH_COMMENT,
                       slash_comments=suffix not in (".py", ".pyi", ".rb", ".sh", ".ps1"))


def _py_code(text: str) -> str | None:
    lines = text.splitlines(keepends=True)
    out = [list(ln) for ln in lines]
    blank_types = {tokenize.COMMENT, tokenize.STRING}
    fmid = getattr(tokenize, "FSTRING_MIDDLE", None)
    if fmid is not None:
        blank_types.add(fmid)
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type not in blank_types:
                continue
            (r1, c1), (r2, c2) = tok.start, tok.end
            for r in range(r1, r2 + 1):
                row = out[r - 1] if r - 1 < len(out) else None
                if row is None:
                    continue
                a = c1 if r == r1 else 0
                b = c2 if r == r2 else len(row)
                for i in range(a, min(b, len(row))):
                    if row[i] not in "\r\n":
                        row[i] = " "
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return None
    return "".join("".join(r) for r in out)


def _clike_code(text: str, *, hash_comments: bool = False, slash_comments: bool = True) -> str:
    out = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        two = text[i:i + 2]
        if slash_comments and two == "//":
            j = text.find("\n", i)
            j = n if j == -1 else j
            out.append(_blank(text[i:j]))
            i = j
        elif slash_comments and two == "/*":
            j = text.find("*/", i + 2)
            j = n if j == -1 else j + 2
            out.append(_blank(text[i:j]))
            i = j
        elif hash_comments and ch == "#":
            j = text.find("\n", i)
            j = n if j == -1 else j
            out.append(_blank(text[i:j]))
            i = j
        elif text.startswith('"""', i):
            j = text.find('"""', i + 3)
            j = n if j == -1 else j + 3
            out.append(_blank(text[i:j]))
            i = j
        elif ch in "\"'`":
            j = i + 1
            while j < n and text[j] != ch and not (text[j] == "\n" and ch != "`"):
                j += 2 if text[j] == "\\" else 1
            j = min(n, j + 1)
            out.append(_blank(text[i:j]))
            i = j
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def pattern_hits(repo: Path, rx: re.Pattern, rels: list[str]) -> list[tuple[str, int, str]]:
    """``(rel, line, text)`` of the lines where ``rx`` matches the code (comments and strings removed)."""
    hits = []
    for rel in rels:
        text = _read(Path(repo) / rel)
        if text is None or not rx.search(text):
            continue
        code = code_text(text, PurePosixPath(rel).suffix).split("\n")
        raw = text.split("\n")
        for i, ln in enumerate(code, 1):
            if rx.search(ln):
                hits.append((rel, i, raw[i - 1].rstrip("\r") if i - 1 < len(raw) else ""))
    return hits


def code_line_matches(repo: Path, rel: str, line: int, rx: re.Pattern) -> bool:
    """Does ``rx`` match line ``line`` of ``rel`` outside its comments and strings?"""
    text = _read(Path(repo) / rel)
    if text is None:
        return False
    code = code_text(text, PurePosixPath(rel).suffix).split("\n")
    return 0 < line <= len(code) and bool(rx.search(code[line - 1]))


EXCLUSIVE_SUFFIXES = (".py", ".js", ".ts", ".go", ".rs", ".java", ".rb", ".php", ".cs")
_DOTTED_RX = re.compile(r"^(?:\\b|\^)?([A-Za-z_]\w*(?:\\\.[A-Za-z_]\w*)+)(?:\\b|\\s\*|\\\(|\$)*$")


def dotted_call(pattern: str) -> str | None:
    """``sqlite3\\.connect\\(`` -> ``sqlite3.connect``: the call a simple exclusivity regex names, else None."""
    m = _DOTTED_RX.match(str(pattern or "").strip())
    return m.group(1).replace("\\.", ".") if m else None


def exclusive_hits(repo: Path, spec: dict, *, include_allowed: bool = False) -> tuple[list[tuple[str, int, str]], str]:
    """Counterexamples to an ``exclusive`` claim (``spec``: ``pattern`` and/or ``calls``, ``allowed_files``).

    The drop-in replacement of the raw per-line regex scan (critique's exclusivity check, feedback's
    exclusive corrections): the pattern must match the *code* of a line (a comment or a string does not
    count), and when the pattern names a dotted call (``sqlite3\\.connect``) or the spec gives ``calls``,
    the Python syntax-tree engine adds the calls made through import aliases (``import sqlite3 as sq``,
    ``from sqlite3 import connect as open_db``). Returns ``([(rel, line, text)], method)``.
    """
    from verinoda.snapshot import list_files

    repo = Path(repo)
    allowed = set(spec.get("allowed_files") or [])
    files = [r for r in list_files(repo) if r.endswith(EXCLUSIVE_SUFFIXES) and (include_allowed or r not in allowed)]
    hits: dict[tuple[str, int], str] = {}
    method = []
    if spec.get("pattern"):
        try:
            rx = re.compile(spec["pattern"])
        except re.error:
            rx = None
        if rx is not None:
            for rel, i, text in pattern_hits(repo, rx, files):
                hits[(rel, i)] = text
            method.append("pattern in code (comments and strings removed)")
    calls = list(spec.get("calls") or []) or ([dotted_call(spec.get("pattern"))] if dotted_call(spec.get("pattern"))
                                              else [])
    if calls:
        ctx = _Ctx(repo, list_files(repo))
        scan = Scan()
        targets = {c: c for c in calls}
        for rel in files:
            if not rel.endswith(PY_SUFFIXES):
                continue
            text = _read(repo / rel)
            if text is None or not any(c.rpartition(".")[2] in text for c in calls):
                continue
            for level, line, _why in _py_only_in(ctx.py, rel, targets, scan):
                if level == VIOLATED:
                    hits.setdefault((rel, line), _line(repo, rel, line))
        method.append(f"Python calls to {', '.join(calls)} through imports and aliases")
    return [(r, i, t) for (r, i), t in sorted(hits.items())], "; ".join(method)


def _read(p: Path) -> str | None:
    try:
        if p.stat().st_size > 2_000_000:
            return None
        return p.read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return None


# -- scope ---------------------------------------------------------------------------------------

def glob_match(path: str, pattern: str) -> bool:
    """A path under a directory (``src/main``), an exact file, or an fnmatch glob (``*`` crosses ``/``)."""
    pattern = pattern.strip().rstrip("/") if not any(c in pattern for c in "*?[") else pattern.strip()
    if not any(c in pattern for c in "*?["):
        return path == pattern or path.startswith(pattern + "/")
    if pattern.endswith("/**") and (path == pattern[:-3] or path.startswith(pattern[:-2])):
        return True
    return fnmatch.fnmatchcase(path, pattern)


def _excluded_roots(repo: Path) -> list[str]:
    roots: list[str] = []
    try:
        from verinoda.paths import load_config

        for e in (load_config(repo).get("index") or {}).get("reference") or []:
            p = e.get("path") if isinstance(e, dict) else e
            if isinstance(p, str) and p.strip():
                roots.append(p.strip().strip("/"))
    except Exception:  # noqa: BLE001 - an unreadable config: no reference trees
        pass
    try:
        from verinoda import copies

        roots += [c["path"].strip("/") for c in copies.load(repo)]
    except Exception:  # noqa: BLE001
        pass
    return [r for r in roots if r]


def scope_files(repo: Path, guard: dict, all_files: list[str]) -> tuple[list[str], list[str]]:
    """(files the guard checks, what the scope leaves out - for the limits)."""
    from verinoda.architecture_map import is_test_file
    from verinoda.decisions import decisions_dir

    repo = Path(repo)
    left_out: list[str] = []
    try:
        ddir = decisions_dir(repo).relative_to(repo.resolve()).as_posix()
    except Exception:  # noqa: BLE001
        ddir = ".verinoda/decisions"
    roots = [] if guard.get("scope") == "all" else _excluded_roots(repo)
    out = []
    n_tests = n_ref = 0
    for rel in all_files:
        if not rel.endswith(CODE_SUFFIXES) or rel.startswith(ddir + "/") or rel.startswith(".verinoda/"):
            continue
        if any(glob_match(rel, a) for a in guard.get("allowed") or []):
            continue
        if any(glob_match(rel, x) for x in guard.get("exclude") or []):
            continue
        if guard.get("scope") != "all":
            if is_test_file(rel):
                n_tests += 1
                continue
            if any(rel == r or rel.startswith(r + "/") for r in roots):
                n_ref += 1
                continue
        out.append(rel)
    if n_tests:
        left_out.append(f"{n_tests} test file(s) are out of scope (scope=all includes them)")
    if n_ref:
        left_out.append(f"{n_ref} file(s) in reference trees or detected copies are out of scope")
    if guard.get("exclude"):
        left_out.append(f"excluded: {', '.join(guard['exclude'])}")
    return out, left_out


# -- Python: calls bound through imports ---------------------------------------------------------

def _module_of(rel: str) -> str | None:
    if not rel.endswith(PY_SUFFIXES):
        return None
    parts = list(PurePosixPath(rel).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) or None


class _PyIndex:
    """Parsed Python files of one repository, parsed on demand, with their import bindings."""

    def __init__(self, repo: Path, all_files: list[str]):
        self.repo = Path(repo)
        self.modules: dict[str, str] = {}
        for rel in all_files:
            if rel.endswith(PY_SUFFIXES):
                m = _module_of(rel)
                if m:
                    self.modules.setdefault(m, rel)
                    # a src layout: src/pkg/mod.py is imported as pkg.mod
                    if m.startswith("src."):
                        self.modules.setdefault(m[4:], rel)
        self._trees: dict[str, tuple[ast.AST | None, str | None]] = {}
        self._binds: dict[str, dict] = {}

    def tree(self, rel: str) -> tuple[ast.AST | None, str | None]:
        if rel not in self._trees:
            text = _read(self.repo / rel)
            tree = None
            if text is not None:
                try:
                    tree = ast.parse(text)
                except (SyntaxError, ValueError, RecursionError):
                    tree = None
            self._trees[rel] = (tree, text)
        return self._trees[rel]

    def bindings(self, rel: str) -> dict:
        """Module-level and nested imports and assignments: ``{name: [(qualified, kind, node)]}`` and stars."""
        if rel in self._binds:
            return self._binds[rel]
        tree, _ = self.tree(rel)
        out: dict = {"names": {}, "stars": [], "assigned": {}}
        if tree is None:
            self._binds[rel] = out
            return out
        pkg = _module_of(rel) or ""
        if not rel.endswith("__init__.py"):
            pkg = pkg.rpartition(".")[0]

        def put(name: str, qual: str, kind: str, node: ast.AST) -> None:
            out["names"].setdefault(name, []).append((qual, kind, node))

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.asname:
                        put(a.asname, a.name, "import", node)
                    else:
                        root = a.name.split(".")[0]
                        put(root, root, "import", node)
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    parts = pkg.split(".") if pkg else []
                    parts = parts[: len(parts) - (node.level - 1)] if node.level > 1 else parts
                    base = ".".join([*parts, *([base] if base else [])])
                for a in node.names:
                    if a.name == "*":
                        out["stars"].append((base, node))
                    else:
                        put(a.asname or a.name, f"{base}.{a.name}" if base else a.name, "from", node)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    if isinstance(t, ast.Name):
                        out["assigned"].setdefault(t.id, []).append(node)
        self._binds[rel] = out
        return out

    def qualify(self, rel: str, expr: ast.AST, depth: int = 0) -> tuple[str | None, str]:
        """Dotted name ``expr`` refers to through the file's bindings: ``(qualified, how)``."""
        attrs = []
        e = expr
        while isinstance(e, ast.Attribute):
            attrs.append(e.attr)
            e = e.value
        if not isinstance(e, ast.Name):
            return None, "not a name"
        attrs.reverse()
        b = self.bindings(rel)
        binds = b["names"].get(e.id) or []
        assigned = b["assigned"].get(e.id) or []
        if binds and not assigned:
            quals = {q for q, _, _ in binds}
            if len(quals) == 1:
                return ".".join([next(iter(quals)), *attrs]), "import"
            return None, f"`{e.id}` is imported from several places"
        if assigned and not binds and len(assigned) == 1 and depth < 3:
            v = assigned[0].value
            if isinstance(v, (ast.Attribute, ast.Name)):
                q, _ = self.qualify(rel, v, depth + 1)
                if q:
                    return ".".join([q, *attrs]), "assignment"
        return None, "not bound by an import"

    def resolve(self, qual: str, depth: int = 0) -> str:
        """Follow a name through the project's own modules (``pkg.util.open_db`` -> ``sqlite3.connect``)."""
        if depth > 4 or "." not in qual:
            return qual
        parts = qual.split(".")
        for i in range(len(parts) - 1, 0, -1):
            mod, rest = ".".join(parts[:i]), parts[i:]
            rel = self.modules.get(mod)
            if rel is None:
                continue
            b = self.bindings(rel)
            name = rest[0]
            binds = b["names"].get(name) or []
            if binds and not b["assigned"].get(name):
                q = binds[0][0]
                if len({x for x, _, _ in binds}) == 1:
                    return self.resolve(".".join([q, *rest[1:]]), depth + 1)
            tree, _ = self.tree(rel)
            if tree is not None and len(b["assigned"].get(name) or []) == 1 and not binds:
                v = b["assigned"][name][0].value
                if isinstance(v, (ast.Attribute, ast.Name)):
                    q, _ = self.qualify(rel, v)
                    if q:
                        return self.resolve(".".join([q, *rest[1:]]), depth + 1)
            return qual
        return qual


def _rebound_locally(tree: ast.AST, call: ast.AST, name: str) -> str | None:
    """Why ``name`` at ``call`` may not be the imported one (a parameter or an assignment in its function)."""
    line = getattr(call, "lineno", 0)
    best = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)) and \
                node.lineno <= line <= (getattr(node, "end_lineno", None) or node.lineno):
            best = node if best is None or node.lineno >= best.lineno else best
    if best is None:
        return None
    args = best.args
    for a in [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]:
        if a is not None and a.arg == name:
            return f"`{name}` is a parameter of the enclosing function"
    for node in ast.walk(best):
        if isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, ast.Store):
            return f"`{name}` is assigned in the enclosing function (line {node.lineno})"
        if isinstance(node, (ast.Import, ast.ImportFrom)) and node is not best:
            for a in node.names:
                if (a.asname or a.name.split(".")[0]) == name and node.lineno <= line:
                    return None  # a local import binds it the usual way
    return None


def _py_only_in(ix: _PyIndex, rel: str, targets: dict[str, str], scan: Scan) -> list[tuple[str, int, str]]:
    """``(level, line, why)`` for calls in ``rel`` that bind to one of ``targets`` (qualified -> shown)."""
    tree, text = ix.tree(rel)
    if text is None:
        return []
    if tree is None:
        scan.unknown.append(f"{rel} does not parse as Python: not checked")
        return []
    scan.count("python")
    out = []
    funcs = {t.rpartition(".")[2] for t in targets}
    mods = {t.rpartition(".")[0] for t in targets}
    b = ix.bindings(rel)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Name) and f.id == "getattr" and len(node.args) >= 2 and \
                isinstance(node.args[1], ast.Constant) and node.args[1].value in funcs:
            q, _ = ix.qualify(rel, node.args[0])
            q = ix.resolve(q) if q else q
            if q in mods:
                full = f"{q}.{node.args[1].value}"
                if full in targets:
                    out.append((POSSIBLE, node.lineno, f"getattr({q}, \"{node.args[1].value}\") reaches "
                                                        f"{targets[full]} dynamically"))
            continue
        if not isinstance(f, (ast.Name, ast.Attribute)):
            continue
        last = f.attr if isinstance(f, ast.Attribute) else f.id
        q, how = ix.qualify(rel, f)
        if q is None:
            if isinstance(f, ast.Name) and last in funcs and b["stars"]:
                for base, _ in b["stars"]:
                    full = f"{ix.resolve(base)}.{last}" if base else last
                    if full in targets:
                        out.append((POSSIBLE, node.lineno, f"`{last}()` may come from `from {base} import *`"))
            continue
        full = ix.resolve(q)
        if full not in targets:
            continue
        root = f
        while isinstance(root, ast.Attribute):
            root = root.value
        why_not = _rebound_locally(tree, node, root.id) if isinstance(root, ast.Name) else None
        mod_assigned = isinstance(root, ast.Name) and b["assigned"].get(root.id) and how == "import"
        shown = targets[full]
        if why_not or mod_assigned:
            out.append((POSSIBLE, node.lineno, f"call to {shown}, but {why_not or f'`{root.id}` is also assigned'}"))
        else:
            written = ast.unparse(f)
            parts = ([f"as `{written}`"] if written != full else []) + ([f"through {q}"] if q != full else []) + \
                ([f"bound by an {how}"] if how != "import" else [])
            out.append((VIOLATED, node.lineno, f"call to {shown}" + (f" ({', '.join(parts)})" if parts else "")))
    return out


def _dynamic_import_hint(rel: str, text: str, code: str, targets: dict[str, str]) -> list[tuple[str, int, str]]:
    out = []
    for t in targets:
        mod, _, func = t.rpartition(".")
        if not re.search(rf"(?:import_module|__import__)\(\s*['\"]{re.escape(mod)}['\"]", text):
            continue
        for i, ln in enumerate(code.split("\n"), 1):
            if re.search(rf"\.{re.escape(func)}\s*\(", ln):
                out.append((POSSIBLE, i, f"{mod} is imported dynamically in this file and `.{func}(` is called"))
    return out


# -- Java / Kotlin: calls bound through imports and declared types -------------------------------

def _jvm_target(t: str) -> tuple[str, str, str]:
    """``pkg.Cls.method`` -> (pkg, Cls, method); ``Cls.method`` -> ("", Cls, method)."""
    qual, _, method = t.rpartition(".")
    parts = qual.split(".")
    cls_i = next((i for i in range(len(parts) - 1, -1, -1) if parts[i][:1].isupper()), None)
    if cls_i is None:
        return qual, "", method
    return ".".join(parts[:cls_i]), ".".join(parts[cls_i:]), method


def _jvm_calls(text: str, suffix: str) -> list[tuple[int, str, str | None, str]]:
    """``(line, method, receiver text or None, receiver node type)`` of every call in a Java/Kotlin file."""
    from verinoda import anchors

    parser = anchors._ts_parser(suffix)
    if parser is None:
        return []
    src = text.encode("utf-8", "surrogatepass")
    tree = parser.parse(src)
    out = []
    stack = [tree.root_node]
    while stack:
        n = stack.pop()
        stack.extend(n.children)
        if n.type == "method_invocation":
            name = n.child_by_field_name("name")
            obj = n.child_by_field_name("object")
            if name is not None:
                out.append((name.start_point[0] + 1, name.text.decode("utf-8", "replace"),
                            obj.text.decode("utf-8", "replace") if obj is not None else None,
                            obj.type if obj is not None else ""))
        elif n.type == "call_expression" and n.children:
            head = n.children[0]
            if head.type == "navigation_expression" and len(head.children) >= 2:
                last = head.children[-1]
                if last.type == "navigation_suffix":  # older grammars: (navigation_suffix . simple_identifier)
                    last = last.children[-1] if last.children else last
                recv = head.children[0]
                out.append((last.start_point[0] + 1, last.text.decode("utf-8", "replace"),
                            recv.text.decode("utf-8", "replace"), recv.type))
            elif head.type in ("identifier", "simple_identifier"):
                out.append((head.start_point[0] + 1, head.text.decode("utf-8", "replace"), None, ""))
    return out


def _jvm_imports(code: str) -> tuple[str | None, dict[str, str], list[str], dict[str, str]]:
    """(package, explicit imports cls->pkg, wildcard packages, static imports method->pkg.Cls)."""
    pkg_m = re.search(r"^\s*package\s+([\w.]+)", code, re.M)
    explicit, wild, static = {}, [], {}
    for m in re.finditer(r"^\s*import\s+(static\s+)?([\w.]+?)(\.\*)?(?:\s+as\s+(\w+))?\s*;?\s*$", code, re.M):
        path = m.group(2)
        if m.group(3):
            wild.append(path)
            continue
        head, _, last = path.rpartition(".")
        if m.group(1):
            static[m.group(4) or last] = head
        else:
            explicit[m.group(4) or last] = path
    return (pkg_m.group(1) if pkg_m else None), explicit, wild, static


def _declared_type(code_lines: list[str], name: str, before: int) -> set[str]:
    """Types ``name`` is declared with in the file before line ``before`` (Java and Kotlin forms)."""
    types = set()
    n = re.escape(name)
    for i, ln in enumerate(code_lines[:before], 1):
        for m in re.finditer(rf"\b([A-Z][\w.]*)(?:<[^;(){{}}=]*>)?(?:\[\])?\s+{n}\s*[=;,):]", ln):
            types.add(m.group(1))
        for m in re.finditer(rf"\b{n}\s*:\s*([A-Z][\w.]*)", ln):
            types.add(m.group(1))
        for m in re.finditer(rf"\b(?:val|var)\s+{n}\s*=\s*([A-Z][\w.]*)\s*[(<]", ln):
            types.add(m.group(1))
        if re.search(rf"\b(?:val|var)\s+{n}\s*=", ln) and not re.search(rf"\b(?:val|var)\s+{n}\s*=\s*[A-Z]", ln):
            types.add("?")
        if re.search(rf"\bvar\s+{n}\s*=", ln) and ln.lstrip().startswith("var "):
            types.add("?")
    return types


def _jvm_only_in(repo: Path, rel: str, targets: dict[str, str], scan: Scan) -> list[tuple[str, int, str]]:
    text = _read(Path(repo) / rel)
    if text is None:
        return []
    suffix = PurePosixPath(rel).suffix.lower()
    code = code_text(text, suffix)
    methods = {_jvm_target(t)[2] for t in targets}
    if not any(re.search(rf"\b{re.escape(m)}\b", code) for m in methods):
        scan.count("jvm")
        return []
    calls = _jvm_calls(text, suffix)
    if not calls and not re.search(r"\S", code):
        return []
    scan.count("jvm")
    pkg, explicit, wild, static = _jvm_imports(code)
    lines = code.split("\n")
    out = []
    for t, shown in targets.items():
        tpkg, cls, method = _jvm_target(t)
        if not cls:
            continue
        simple = cls.rpartition(".")[2]

        def bound(c: str) -> tuple[bool, str]:
            """Is class name ``c`` (as written) the target class in this file?"""
            if "." in c and c[:1].islower():  # fully qualified in the code
                p, _, k = c.rpartition(".")
                return (k == simple and (not tpkg or p == tpkg)), f"fully qualified {c}"
            if c != simple:
                return False, ""
            if c in explicit:
                p = explicit[c].rpartition(".")[0]
                return (not tpkg or p == tpkg), f"imported from {p}"
            if tpkg and (pkg == tpkg or tpkg in wild):
                return True, "same package" if pkg == tpkg else f"imported with {tpkg}.*"
            return False, "not imported in this file"

        for line, name, recv, rtype in calls:
            if name != method:
                continue
            if recv is None:
                if name in static and (static[name].rpartition(".")[2] == simple) and \
                        (not tpkg or static[name].rpartition(".")[0] == tpkg):
                    out.append((VIOLATED, line, f"call to {shown} (static import of {static[name]}.{name})"))
                else:
                    out.append((POSSIBLE, line, f"`{name}(...)` without a receiver: an inherited or local "
                                                f"method of that name is not resolved"))
                continue
            if re.fullmatch(r"[A-Za-z_$][\w$]*", recv):
                types = _declared_type(lines, recv, line)
                named = {x.rpartition(".")[2] for x in types}
                if types and named == {simple}:  # a variable, parameter or field declared with the class
                    t_as = next(iter(types))
                    ok, why = bound(t_as)
                    if ok:
                        out.append((VIOLATED, line, f"call to {shown} (`{recv}` is declared {t_as}, {why})"))
                    else:
                        out.append((POSSIBLE, line, f"`{recv}.{name}(...)`: `{recv}` is a {t_as}, but {why}"))
                    continue
                if simple in named:
                    out.append((POSSIBLE, line, f"`{recv}.{name}(...)`: `{recv}` is declared with several types "
                                                f"({', '.join(sorted(types))})"))
                    continue
                if types and "?" not in types:
                    continue  # declared with another class: not the target
                if not types and recv[:1].isupper():  # a class name: a static call
                    ok, why = bound(recv)
                    if ok:
                        out.append((VIOLATED, line, f"call to {shown} ({recv}.{name}, {why})"))
                    elif recv == simple:
                        out.append((POSSIBLE, line, f"`{recv}.{name}(...)`: {why or 'another class of that name?'}"))
                    continue
                out.append((POSSIBLE, line, f"`{recv}.{name}(...)`: the type of `{recv}` is not resolved"))
                continue
            if re.fullmatch(r"[a-z_][\w]*(?:\.[a-z_][\w]*)+\.[A-Z][\w$]*", recv):  # a fully qualified class
                ok, why = bound(recv)
                if ok:
                    out.append((VIOLATED, line, f"call to {shown} ({recv}.{name}, {why})"))
                continue
            out.append((POSSIBLE, line, f"`...{name}(...)` on `{recv[:40]}`: the receiver's type is not resolved"))
    return out


# -- text fallback ---------------------------------------------------------------------------------

def _text_only_in(repo: Path, rel: str, targets: dict[str, str], scan: Scan) -> list[tuple[str, int, str]]:
    text = _read(Path(repo) / rel)
    if text is None:
        return []
    scan.count("text")
    code = code_text(text, PurePosixPath(rel).suffix).split("\n")
    out = []
    for t, shown in targets.items():
        head, _, func = t.rpartition(".")
        last = head.rpartition(".")[2]
        rx = re.compile(rf"(?<![\w$]){re.escape(last)}\s*(?:\.|::|->)\s*{re.escape(func)}\s*\(")
        for i, ln in enumerate(code, 1):
            if rx.search(ln):
                out.append((POSSIBLE, i, f"`{last}.{func}(` in code (text match; no binding check for this "
                                         "language)"))
    return out


# -- the guards ------------------------------------------------------------------------------------

def _line(repo: Path, rel: str, line: int) -> str:
    text = _read(Path(repo) / rel) or ""
    ls = text.split("\n")
    return ls[line - 1].strip()[:160] if 0 < line <= len(ls) else ""


def _targets(g: dict) -> tuple[dict[str, str], str]:
    if g.get("calls"):
        return {c: c for c in g["calls"]}, "calls " + ", ".join(g["calls"])
    if g.get("sink") in SINK_CALLS:
        return {c: f"{c} ({g['sink']})" for c in SINK_CALLS[g["sink"]]}, f"sink {g['sink']}"
    return {}, f"sink {g.get('sink')}" if g.get("sink") else f"pattern /{g.get('pattern')}/"


class _Ctx:
    def __init__(self, repo: Path, all_files: list[str], graph=None):
        self.repo = Path(repo)
        self.all_files = all_files
        self.graph = graph
        self._py: _PyIndex | None = None
        self._deps: dict | None = None

    @property
    def py(self) -> _PyIndex:
        if self._py is None:
            self._py = _PyIndex(self.repo, self.all_files)
        return self._py

    def deps(self) -> dict:
        if self._deps is None:
            self._deps = declared_dependencies(self.repo)
        return self._deps


def check_only_in(ctx: _Ctx, g: dict) -> tuple[list[tuple[str, str, int, str]], Scan, str]:
    """``[(level, rel, line, why)]``, the scan, and a short description of what is guarded."""
    scan = Scan()
    files, left_out = scope_files(ctx.repo, g, ctx.all_files)
    targets, what = _targets(g)
    what += f" (allowed: {', '.join(g.get('allowed') or [])})"
    out: list[tuple[str, str, int, str]] = []
    if targets:
        funcs = {t.rpartition(".")[2] for t in targets}
        py_files = [f for f in files if f.endswith(PY_SUFFIXES)]
        # a re-export: a module that binds the target at module level makes its own name a target too
        names = set(funcs)
        for rel in ctx.all_files:
            if not rel.endswith(PY_SUFFIXES):
                continue
            text = _read(ctx.repo / rel)
            if text is None or not any(f in text for f in funcs):
                continue
            b = ctx.py.bindings(rel)
            for name, binds in b["names"].items():
                if any(ctx.py.resolve(q) in targets for q, _, _ in binds):
                    names.add(name)
        for rel in py_files:
            text = _read(ctx.repo / rel)
            if text is None or not any(n in text for n in names):
                scan.count("python")
                continue
            for level, line, why in _py_only_in(ctx.py, rel, targets, scan) + \
                    _dynamic_import_hint(rel, text, code_text(text, ".py"), targets):
                out.append((level, rel, line, why))
        for rel in files:
            if rel.endswith(JVM_SUFFIXES):
                for level, line, why in _jvm_only_in(ctx.repo, rel, targets, scan):
                    out.append((level, rel, line, why))
            elif not rel.endswith(PY_SUFFIXES):
                for level, line, why in _text_only_in(ctx.repo, rel, targets, scan):
                    out.append((level, rel, line, why))
        scan.limit(*(PY_LIMITS if scan.files.get("python") else []), *(JVM_LIMITS if scan.files.get("jvm") else []),
                   *(TEXT_LIMITS if scan.files.get("text") else []))
    else:  # a sink kind without call targets, or a pattern: text matches over the code
        from verinoda.architecture_map import SINK_PATTERNS

        rxs = [rx for rx, kind in SINK_PATTERNS if kind == g.get("sink")] if g.get("sink") else \
            [re.compile(g["pattern"])]
        for rel in files:
            text = _read(ctx.repo / rel)
            if text is None:
                continue
            scan.count("text")
            code = code_text(text, PurePosixPath(rel).suffix).split("\n") if not g.get("sink") else text.split("\n")
            for i, ln in enumerate(code, 1):
                if any(rx.search(ln) for rx in rxs):
                    out.append((POSSIBLE, rel, i, f"{what} matches (text; a pattern is never a verified call)"))
        scan.limit("a pattern or a text sink is a text match: POSSIBLE at most"
                   + ("; comments and strings are removed first" if not g.get("sink") else ""))
    scan.limit(*left_out)
    return _strongest(out), scan, what


def _strongest(hits: list[tuple[str, str, int, str]]) -> list[tuple[str, str, int, str]]:
    """One finding per site: a VIOLATED one wins over POSSIBLE ones on the same line."""
    best: dict[tuple[str, int], tuple[str, str, int, str]] = {}
    for h in hits:
        k = (h[1], h[2])
        if k not in best or (h[0] == VIOLATED and best[k][0] != VIOLATED):
            best[k] = h
    return sorted(best.values(), key=lambda x: (x[1], x[2]))


def _named_in_code(repo: Path, rel: str, line: int, name: str) -> bool:
    text = _read(Path(repo) / rel)
    if text is None:
        return False
    code = code_text(text, PurePosixPath(rel).suffix).split("\n")
    return 0 < line <= len(code) and bool(re.search(rf"(?<![\w$]){re.escape(name)}(?![\w$])", code[line - 1]))


def check_no_edge(ctx: _Ctx, g: dict) -> tuple[list[tuple[str, str, int, str]], Scan, str]:
    scan = Scan()
    what = f"{g['from']} -> {g['to']} ({', '.join(g.get('relations') or [])})"
    if ctx.graph is None:
        scan.unknown.append("no index: run `verinoda scan` (no_edge reads the graph's edges)")
        return [], scan, what
    rels = set(g.get("relations") or [])
    out = []
    n = 0
    for u, v, d in ctx.graph.edges(rels or None):
        fu, fv = ctx.graph.file(u), ctx.graph.file(v)
        if not fu or not fv or not glob_match(fu, g["from"]) or not glob_match(fv, g["to"]):
            continue
        n += 1
        loc = str(d.get("source_location") or "")
        src = d.get("source_file") or fu
        line = int(loc[1:]) if loc.startswith("L") and loc[1:].isdigit() else None
        # a file node is labelled with its file name (EmberForgeScreen.kt): the code names its stem
        target = PurePosixPath(fv).stem if ctx.graph.is_file_node(v) else \
            (ctx.graph.label(v).strip(".()").rpartition(".")[2] or PurePosixPath(fv).stem)
        conf = d.get("confidence") or "?"
        rel_name = d.get("relation")
        if line is None:
            out.append((POSSIBLE, src, 0, f"{rel_name} edge to {fv} ({conf}) without a cited line"))
        elif conf == "EXTRACTED" and _named_in_code(ctx.repo, src, line, target):
            out.append((VIOLATED, src, line, f"{rel_name} {target} in {fv} (EXTRACTED edge; the line names it)"))
        elif conf == "EXTRACTED":
            out.append((POSSIBLE, src, line, f"{rel_name} {target} in {fv}: EXTRACTED edge, but the line no "
                                             "longer names it (the index may be older than the file)"))
        else:
            out.append((POSSIBLE, src, line, f"{rel_name} {target} in {fv} ({conf} edge: a resolver's guess)"))
    scan.files["edges"] = n
    scan.limit(*EDGE_LIMITS)
    return _strongest(out), scan, what


# -- dependencies (manifests, with Gradle and Maven) -------------------------------------------------

def _gradle_maven(root: Path) -> list[dict]:
    items = []
    for name in ("build.gradle", "build.gradle.kts"):
        for p in sorted(root.rglob(name)):
            rel = p.relative_to(root).as_posix()
            if any(part in (".verinoda", "build", ".gradle", "node_modules") for part in PurePosixPath(rel).parts):
                continue
            text = _read(p) or ""
            for i, ln in enumerate(text.split("\n"), 1):
                m = re.search(r"\b(\w*(?:[Ii]mplementation|[Aa]pi|[Cc]ompileOnly|[Rr]untimeOnly|[Aa]nnotationProcessor"
                              r"|[Cc]ompile))\s*\(?\s*['\"]([\w.\-]+):([\w.\-]+)(?::([^'\"]+))?['\"]", ln)
                if m:
                    items.append({"name": f"{m.group(2)}:{m.group(3)}".lower(), "short": m.group(3).lower(),
                                  "spec": m.group(4) or "*", "scope": m.group(1), "at": f"{rel}:{i}", "path": rel,
                                  "line": i, "ecosystem": "maven"})
    for p in sorted(root.rglob("pom.xml")):
        rel = p.relative_to(root).as_posix()
        if ".verinoda" in rel:
            continue
        text = _read(p) or ""
        for m in re.finditer(r"<dependency>\s*<groupId>([^<]+)</groupId>\s*<artifactId>([^<]+)</artifactId>"
                             r"(?:\s*<version>([^<]+)</version>)?", text):
            line = text.count("\n", 0, m.start()) + 1
            items.append({"name": f"{m.group(1).strip()}:{m.group(2).strip()}".lower(),
                          "short": m.group(2).strip().lower(), "spec": (m.group(3) or "*").strip(),
                          "scope": "compile", "at": f"{rel}:{line}", "path": rel, "line": line, "ecosystem": "maven"})
    return items


def declared_dependencies(root: Path) -> dict:
    """``research.dependencies`` (Python, npm, Go, Cargo manifests) plus Gradle and Maven files."""
    from verinoda import research

    root = Path(root)
    try:
        base = research.dependencies(root)
    except Exception as exc:  # noqa: BLE001 - an unreadable manifest must not stop a check
        base = {"items": [], "manifests": [], "error": f"{type(exc).__name__}: {exc}"[:200]}
    extra = _gradle_maven(root)
    manifests = list(base.get("manifests") or []) + sorted({i["path"] for i in extra})
    return {"items": list(base.get("items") or []) + extra, "manifests": manifests,
            **({"error": base["error"]} if base.get("error") else {})}


def _dep_key(name: str) -> str:
    return re.sub(r"[-_.]+", "-", str(name or "")).lower()


def find_dependency(deps: dict, name: str) -> list[dict]:
    key = _dep_key(name)
    out = []
    for it in deps.get("items") or []:
        names = {_dep_key(it.get("name")), _dep_key(it.get("short") or "")}
        if key in names or (":" in name and _dep_key(name) == _dep_key(it.get("name"))):
            out.append(it)
    return out


def check_dependency(ctx: _Ctx, g: dict) -> tuple[list[tuple[str, str, int, str]], Scan, str]:
    scan = Scan()
    deps = ctx.deps()
    scan.files["manifests"] = len(deps.get("manifests") or [])
    scan.limit(f"manifests read: {', '.join(deps.get('manifests') or []) or 'none found'}", *DEP_LIMITS)
    out = []
    if g.get("absent"):
        what = f"absent {g['absent']}"
        for it in find_dependency(deps, g["absent"]):
            shown = _dep_key(_line(ctx.repo, it["path"], it["line"]))
            if _dep_key(it.get("short") or it["name"]) in shown or _dep_key(g["absent"]) in shown:
                out.append((VIOLATED, it["path"], it["line"], f"{it['name']} {it.get('spec') or ''} is declared "
                                                              f"({it.get('scope')})".replace("  ", " ")))
            else:
                out.append((POSSIBLE, it["path"], it["line"], f"{it['name']} is listed, but the cited line does "
                                                              "not name it"))
    else:
        what = f"present {g['present']}"
        if not find_dependency(deps, g["present"]):
            manifests = deps.get("manifests") or []
            if manifests:
                out.append((VIOLATED, manifests[0], 0, f"{g['present']} is declared in none of the manifests read"))
            else:
                scan.unknown.append("no manifest was found, so no dependency can be shown present or absent")
    return out, scan, what


# -- governs and revisit ------------------------------------------------------------------------------

def check_governs(repo: Path, v: dict) -> tuple[str, str]:
    """(``ok`` | ``REVIEW``, why) for a governed symbol."""
    from verinoda import anchors

    facts, _ = anchors.facts_for_path(Path(repo) / v["file"], v["file"])
    hits = anchors.symbols_named(facts, v["qual"]) if facts else []
    if not facts:
        return REVIEW, f"{v['file']} cannot be read as code now (deleted or unparsable)"
    if len(hits) != 1:
        return REVIEW, f"{v['qual']} is {'gone from' if not hits else 'ambiguous in'} {v['file']}"
    q, sym = hits[0]
    if v.get("fp") and sym.get("full") != v["fp"]:
        return REVIEW, f"the code of {v['file']}::{q} changed since the decision was recorded " \
                       f"(now lines {sym['start']}-{sym['end']})"
    return "ok", f"{v['file']}::{q} unchanged since the decision (lines {sym['start']}-{sym['end']})"


def revisit_holds(repo: Path, r: dict, *, all_files: list[str] | None = None, deps: dict | None = None
                  ) -> list[str]:
    """Where a revisit condition holds now (``path:line`` or paths); empty when it does not."""
    from verinoda.snapshot import list_files

    if r["kind"] == "dependency_added":
        return [it["at"] for it in find_dependency(deps if deps is not None else declared_dependencies(repo),
                                                  r["value"])]
    files = all_files if all_files is not None else list_files(repo)
    return [f for f in files if glob_match(f, r["value"]) or fnmatch.fnmatchcase(PurePosixPath(f).name, r["value"])]


# -- changed files ---------------------------------------------------------------------------------

def validate_ref(repo: Path, ref: str) -> str:
    """The commit ``ref`` names (never an option: refused when it starts with '-', and passed after
    ``--end-of-options``)."""
    from verinoda.snapshot import git

    ref = str(ref or "").strip()
    if not ref or ref.startswith("-") or any(c in ref for c in "\0\n\r"):
        raise ValueError(f"--base {ref!r} is not a git revision")
    out = git(Path(repo), "rev-parse", "--verify", "--quiet", "--end-of-options", f"{ref}^{{commit}}")
    sha = (out or "").strip()
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha):
        raise ValueError(f"--base {ref!r} does not name a commit in {repo}")
    return sha


def changed_since(repo: Path, sha: str) -> set[str]:
    """Files changed since commit ``sha`` in the working tree, plus untracked ones."""
    from verinoda.snapshot import git

    diff = git(Path(repo), "diff", "--name-only", "--no-renames", sha, "--") or ""
    untracked = git(Path(repo), "ls-files", "--others", "--exclude-standard") or ""
    return {ln.strip() for ln in (diff + "\n" + untracked).splitlines() if ln.strip()}


# -- the check ------------------------------------------------------------------------------------

def _guard_desc(g: dict) -> str:
    return g.get("spec") or g.get("kind", "?")


def check(repo: Path, *, graph=None, base: str | None = None, changed_only: bool = False,
          records=None) -> dict:
    """Run every accepted guard of every enforced decision on the working tree.

    ``base`` (a git revision) or ``changed_only`` (= base HEAD) labels each finding ``new/touched since
    <base>`` or ``pre-existing`` by the files changed since it; then only new/touched violations make
    ``exit`` 1. Without them any violation does.
    """
    from verinoda import decisions as dm
    from verinoda.snapshot import list_files

    t0 = time.monotonic()
    repo = Path(repo).resolve()
    recs = records if records is not None else dm.load_all(repo)
    res: dict = {"violations": [], "possible": [], "reviews": [], "triggers": [], "waived": [], "ok": [],
                 "unknown": [], "not_enforced": [], "pre_existing": [], "decisions": len(recs)}
    base_sha = base_label = None
    changed: set[str] | None = None
    if base or changed_only:
        base_label = base or "HEAD"
        base_sha = validate_ref(repo, base_label)
        changed = changed_since(repo, base_sha)
        res["base"] = {"ref": base_label, "commit": base_sha, "changed_files": len(changed)}
    ctx = _Ctx(repo, list_files(repo), graph)
    today = dm._today()
    for d in recs:
        if not d.enforced:
            res["not_enforced"].append({"decision": d.id, "status": d.status,
                                        "why": "; ".join(d.problems) or f"status {d.status}"})
            continue
        for g in d.guards:
            if g.get("status") != "accepted":
                res["not_enforced"].append({"decision": d.id, "guard": g["id"], "status": g.get("status"),
                                            "why": "proposed guard: inactive until the user accepts it"})
                continue
            fn = {"only_in": check_only_in, "no_edge": check_no_edge, "dependency": check_dependency}[g["kind"]]
            try:
                hits, scan, what = fn(ctx, g)
            except Exception as exc:  # noqa: BLE001 - one broken guard must not hide the others
                res["unknown"].append({"decision": d.id, "guard": g["id"], "kind": g["kind"],
                                       "why": f"the check failed: {type(exc).__name__}: {exc}"[:300]})
                continue
            for u in scan.unknown:
                res["unknown"].append({"decision": d.id, "guard": g["id"], "kind": g["kind"], "why": u})
            counted = False
            for level, rel, line, why in hits:
                f = Finding(d.id, g["id"], g["kind"], level, f"{rel}:{line}" if line else rel, why,
                            line=_line(repo, rel, line) if line else None,
                            status="statically_verified" if level == VIOLATED else "weak_inference")
                if changed is not None:
                    f.since = f"new/touched since {base_label}" if rel in changed else "pre-existing"
                item = {**f.as_dict(), "what": what}
                w = dm.waived(d, g["id"], rel, line or None, today)
                if w is not None:
                    res["waived"].append({**item, "waiver": w})
                    continue
                counted = True
                if level == VIOLATED and f.since == "pre-existing":
                    res["pre_existing"].append(item)
                else:
                    res["violations" if level == VIOLATED else "possible"].append(item)
            if not counted and not scan.unknown:
                res["ok"].append({"decision": d.id, "guard": g["id"], "kind": g["kind"], "what": what,
                                  "scope": scan.files, "limits": scan.limits})
            elif scan.limits:
                for key in ("violations", "possible", "pre_existing"):
                    for f in res[key]:
                        if f["decision"] == d.id and f["guard"] == g["id"] and "limits" not in f:
                            f["limits"] = scan.limits
        for v in d.governs:
            level, why = check_governs(repo, v)
            if level == REVIEW:
                item = {"decision": d.id, "guard": v["id"], "kind": "governs", "level": REVIEW, "at": v["symbol"],
                        "why": why}
                if changed is not None:
                    item["since"] = f"new/touched since {base_label}" if v["file"] in changed else "pre-existing"
                res["reviews"].append(item)
            else:
                res["ok"].append({"decision": d.id, "guard": v["id"], "kind": "governs", "what": v["symbol"],
                                  "scope": {"symbols": 1}, "limits": [why]})
        for r in d.revisit_when:
            where = revisit_holds(repo, r, all_files=ctx.all_files, deps=ctx.deps() if r["kind"] ==
                                  "dependency_added" else None)
            if where and not r.get("baseline"):
                res["triggers"].append({"decision": d.id, "guard": r["id"], "kind": "revisit_when", "level": TRIGGER,
                                        "at": where[0], "why": f"{r['kind']}={r['value']} holds now ("
                                        f"{', '.join(where[:3])}): the user should review {d.id}"})
            else:
                note = "held already when the decision was recorded" if where else "does not hold"
                res["ok"].append({"decision": d.id, "guard": r["id"], "kind": "revisit_when",
                                  "what": f"{r['kind']}={r['value']}", "scope": {}, "limits": [note]})
    res["elapsed_s"] = round(time.monotonic() - t0, 3)
    res["exit"] = 1 if res["violations"] else 0
    res["status"] = ("violated" if res["violations"] else "possible" if res["possible"] else
                     "review" if res["reviews"] or res["triggers"] else "ok")
    if res["violations"]:
        ids = sorted({v["decision"] for v in res["violations"]})
        res["next_step"] = (f"change the code, or ask the user whether {', '.join(ids)} should be superseded or "
                            "these sites waived (`verinoda decide waive`); never edit or supersede a decision "
                            "on your own")
    elif res["possible"] or res["reviews"] or res["triggers"]:
        res["next_step"] = "read the POSSIBLE sites; ask the user about REVIEW / TRIGGER items"
    return res
