"""Guards: the mechanical checks of recorded decisions against the code (docs/DESIGN.md D33).

One rule engine, used by ``verinoda decide check`` (and MCP ``decision_check``), by critique's
exclusivity check and by feedback's exclusive corrections. Each finding has a level:

* ``VIOLATED`` - statically verified: the cited line re-checks now and binds to what the guard forbids;
* ``POSSIBLE`` - a heuristic hit (a textual match in a language without a binding check, an INFERRED
  graph edge, a receiver whose type is not resolved, ``getattr(module, "name")``); never a violation;
* ``REVIEW`` - code a decision governs changed since the decision was recorded (never a violation);
* ``TRIGGER`` - a revisit condition of a decision holds now: the human should look at it again;
* ``ok`` - the guard holds, always with its scope (what it counted) and limits (a narrow check must not look
  like a broad guarantee); ``unknown`` - the guard could not be checked (why, and the next step). A guard
  that checked no file, edge or manifest is ``unknown``, never ``ok``; ``check`` then exits 3.

Guard kinds (spec grammar in :mod:`verinoda.decisions`):

``only_in`` - a call may appear only in the allowed files. The scope is the product code by default:
tests, configured reference trees, detected copies of the project, sample/fixture/vendor folders above a
source root (:func:`not_product_dirs`) and the decision folder are left out (``scope=all`` keeps them).
``calls=mod.func``:

* Python: a syntax-tree walk that resolves names as Python does (module, function, lambda, class and
  comprehension scopes, ``global`` / ``nonlocal``) through imports (``import m as a``, ``from m import f
  as g``, relative imports), simple assignments (``g = m.f``) and one or more re-exports through the
  project's own modules (``from pkg.util import g`` where ``pkg/util.py`` imports it). A binding under
  if / for / while / try / except / match may not run, so the bindings that can reach a call are the last
  unconditional one and every conditional one after it. VIOLATED needs every reaching binding to be the
  target; when only some are (``try: import psycopg2`` / ``except ImportError: psycopg2 = None``, a
  module-level ``for`` / ``with`` / ``except`` target, two drivers imported in two branches) the call is
  POSSIBLE; so is a name that shadows the import with a value the engine does not know (a parameter of
  this or an enclosing function, a loop or comprehension variable), the target used as a value
  (``functools.partial(sqlite3.connect, ...)``, a class attribute), ``getattr(m, "f")`` and a star
  import. A def, a class or a literal that replaces the import is not the target. Comments and strings
  are never read as code.
* Java / Kotlin: import-bound calls through the syntax tree. ``Cls.method(...)`` with ``Cls`` imported
  (explicitly - an import alias too -, by a static import, with ``pkg.*`` or from the same package), or
  ``r.method(...)`` where every declaration of ``r`` in the file is ``Cls r`` / ``r: Cls`` / ``val r =
  Cls(...)``, is VIOLATED; any other receiver (a call chain, an undeclared name, a field of another class,
  a name also bound without a written type: a lambda parameter, ``var``, a Kotlin loop variable) is
  POSSIBLE.
* Other languages: a regex over the code with comments and strings removed: POSSIBLE at most.

``sink=db-connection`` checks the known connection calls (``sqlite3.connect``, ``psycopg.connect``,
``create_engine``, ``MongoClient``, JDBC ``DriverManager.getConnection`` ...) the same way; the other
sink kinds and ``pattern=REGEX`` are text matches: POSSIBLE at most.

``no_edge`` - no graph edge from files matching ``from`` to files matching ``to``. An EXTRACTED edge
whose cited line still names the target in code is VIOLATED; an INFERRED edge, or one whose line
changed since the index was built, is POSSIBLE. A side that matches no indexed file (an external
package is no node) is ``unknown``.

``dependency`` - ``absent=NAME``: declared in a manifest is VIOLATED (the manifest line that names it is
cited); ``present=NAME``: missing from every manifest read is VIOLATED. Manifests are the root ones, the
package.json of the workspace packages the root declares, plus the root Gradle/Maven build and the
subprojects it includes (with ``gradle/*.versions.toml`` catalogs); build files under test, sample,
fixture or vendor folders are not read, and a build the root does not include only gives POSSIBLE.

Nothing here imports, runs or evaluates code of the analysed repository: files are read as text and
parsed (``ast``, tree-sitter). ``git`` gets validated refs only (``rev-parse --verify
--end-of-options``) and ``--`` before paths.
"""

from __future__ import annotations

import ast
import fnmatch
import io
import json
import re
import time
import tokenize
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from verinoda.testcode import is_test_file  # test code, out of an only_in guard's default scope

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
             "instance a call returned, self.attribute) are not followed; getattr(module, \"name\") with a "
             "literal name, importlib / __import__ of the module, star imports and the function used as a value "
             "(functools.partial, a callback, a class attribute) are reported as POSSIBLE",
             "re-exports are followed through the project's own modules (4 levels), not through installed packages",
             "files that do not parse are reported as unknown, not checked"]
JVM_LIMITS = ["a receiver's type is read from its declarations anywhere in the same file (scopes are not resolved: "
              "a name also bound without a written type - a lambda parameter, `var`, a Kotlin loop variable - is "
              "POSSIBLE); calls through inherited methods, reflection, method references and fields of other "
              "classes are POSSIBLE or not seen"]
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
    # (file, line) of a finding -> the other files its binding passes through (a re-export module, the
    # target of a graph edge): --changed / --base count the finding as new when any of them changed
    via: dict[tuple[str, int], set[str]] = field(default_factory=dict)

    def count(self, engine: str) -> None:
        self.files[engine] = self.files.get(engine, 0) + 1

    def limit(self, *items: str) -> None:
        for s in items:
            if s not in self.limits:
                self.limits.append(s)


# -- comments and strings -------------------------------------------------------------------------

def _blank(s: str) -> str:
    return "".join("\n" if ch == "\n" else " " for ch in s)


def code_text(text: str, suffix: str, *, keep_strings: bool = False) -> str:
    """``text`` with comments and string literals blanked out (line and column positions kept).

    ``keep_strings``: only comments are blanked, and in Python the strings that are a statement on their
    own (docstrings); the other string literals stay (an SQL statement, a file mode, ``":memory:"``)."""
    suffix = suffix.lower()
    if suffix in PY_SUFFIXES:
        masked = _py_code(text, keep_strings=keep_strings)
        if masked is not None:
            return masked
    return _clike_code(text, hash_comments=suffix in HASH_COMMENT,
                       slash_comments=suffix not in (".py", ".pyi", ".rb", ".sh", ".ps1"), keep_strings=keep_strings)


def _statement_strings(toks: list) -> set[int]:
    """Indexes of STRING tokens that form a whole statement (a docstring, a bare string)."""
    out: set[int] = set()
    skip = (tokenize.NL, tokenize.COMMENT, tokenize.INDENT, tokenize.DEDENT)
    start, i = True, 0
    while i < len(toks):
        tok = toks[i]
        if start and tok.type == tokenize.STRING:
            j = i
            while j + 1 < len(toks) and toks[j + 1].type in (tokenize.STRING, tokenize.NL):
                j += 1
            k = j + 1
            while k < len(toks) and toks[k].type in skip:
                k += 1
            if k >= len(toks) or toks[k].type in (tokenize.NEWLINE, tokenize.ENDMARKER):
                out.update(x for x in range(i, j + 1) if toks[x].type == tokenize.STRING)
            start, i = False, j + 1
            continue
        start = tok.type in (tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT) or \
            (start and tok.type in (tokenize.NL, tokenize.COMMENT))
        i += 1
    return out


def code_texts(text: str, suffix: str) -> tuple[str, str]:
    """``(code_text(text, suffix), code_text(text, suffix, keep_strings=True))`` from one tokenize pass."""
    suffix = suffix.lower()
    if suffix in PY_SUFFIXES:
        masked = _py_masks(text, (False, True))
        if masked is not None:
            return masked[0], masked[1]
    kw = {"hash_comments": suffix in HASH_COMMENT,
          "slash_comments": suffix not in (".py", ".pyi", ".rb", ".sh", ".ps1")}
    return _clike_code(text, **kw), _clike_code(text, keep_strings=True, **kw)


def _py_code(text: str, *, keep_strings: bool = False) -> str | None:
    masked = _py_masks(text, (keep_strings,))
    return masked[0] if masked is not None else None


def _py_masks(text: str, modes: tuple[bool, ...]) -> tuple[str, ...] | None:
    """The text masked once per mode (``keep_strings`` False / True), from one tokenize pass."""
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return None
    # rows as tokenize counts them: split at a newline only (str.splitlines also splits at form feeds and
    # other separators, which would shift every later row by one)
    lines = re.split(r"(?<=\n)", text)
    fmid = getattr(tokenize, "FSTRING_MIDDLE", None)
    res = []
    for keep_strings in modes:
        out = [list(ln) for ln in lines]
        blank_types = {tokenize.COMMENT} if keep_strings else {tokenize.COMMENT, tokenize.STRING}
        if fmid is not None and not keep_strings:
            blank_types.add(fmid)
        prose = _statement_strings(toks) if keep_strings else set()
        for n, tok in enumerate(toks):
            if tok.type not in blank_types and n not in prose:
                continue
            (r1, c1), (r2, c2) = tok.start, tok.end
            for r in range(r1, r2 + 1):
                if r - 1 >= len(out):
                    continue
                row = out[r - 1]
                a = c1 if r == r1 else 0
                b = min(c2 if r == r2 else len(row), len(row))
                row[a:b] = [ch if ch in "\r\n" else " " for ch in row[a:b]]
        res.append("".join("".join(r) for r in out))
    return tuple(res)


def _clike_code(text: str, *, hash_comments: bool = False, slash_comments: bool = True,
                keep_strings: bool = False) -> str:
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
            out.append(text[i:j] if keep_strings else _blank(text[i:j]))
            i = j
        elif ch in "\"'`":
            j = i + 1
            while j < n and text[j] != ch and not (text[j] == "\n" and ch != "`"):
                j += 2 if text[j] == "\\" else 1
            j = min(n, j + 1)
            out.append(text[i:j] if keep_strings else _blank(text[i:j]))
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


_UNREAD = "{rel} could not be read (over 2 MB, or unreadable): not checked (exclude=GLOB leaves it out of the guard)"


def _read(p: Path) -> str | None:
    try:
        if p.stat().st_size > 2_000_000:
            return None
        text = p.read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return None
    # a byte-order mark is not code (Python runs such a file; ast.parse of the str would refuse it)
    return text[1:] if text.startswith("\ufeff") else text


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


# folders of code that is not the product's own (samples, fixtures, vendored copies): out of an only_in guard's
# default scope, as their build files are out of a dependency guard's (_NOT_PROJECT_BUILD). Only folders above a
# source root count: below src/main/java/ a folder is a package (com/example/... is the Fabric template's package)
_NOT_PRODUCT_DIRS = {"examples", "example", "samples", "sample", "demo", "demos", "fixtures", "fixture",
                     "__fixtures__", "testdata", "test-data", "vendor", "third_party", "third-party", "node_modules"}
# a JVM source root: src/<source set>/<language>/ (Maven, Gradle, Android, Kotlin multiplatform); below it every
# folder is a package (not resources/: its folders are no packages)
_SOURCE_ROOT = re.compile(r"(?:^|/)src/[^/]+/(?:java|kotlin|scala|groovy|clojure|aidl)/")
_PACKAGE_DECL = re.compile(r"^[ \t]*package[ \t]+([A-Za-z_][\w.]*)", re.M)
_PACKAGE_SUFFIXES = (".java", ".kt", ".kts", ".scala", ".groovy")
_GENERATED_NAME = re.compile(r"_pb2(?:_grpc)?\.pyi?$|\.pb\.go$")


def not_product_dirs(repo: Path, rel: str) -> set[str]:
    """The folders of ``rel`` that mark sample, fixture or vendored code (:data:`_NOT_PRODUCT_DIRS`), counting
    only folders that are not package directories: those above a JVM source root (``src/main/java/``), and
    in a Java / Kotlin / Scala / Groovy file outside such a root those its ``package`` line does not name."""
    parts = list(PurePosixPath(rel).parts[:-1])
    if not set(parts) & _NOT_PRODUCT_DIRS:
        return set()
    m = _SOURCE_ROOT.search(rel)
    if m is not None:
        return set(PurePosixPath(rel[:m.end()]).parts) & _NOT_PRODUCT_DIRS
    if rel.endswith(_PACKAGE_SUFFIXES):
        try:
            with open(Path(repo) / rel, "rb") as fh:
                head = code_text(fh.read(4000).decode("utf-8", errors="replace"), PurePosixPath(rel).suffix)
        except OSError:
            head = ""
        pm = _PACKAGE_DECL.search(head)
        pkg = pm.group(1).split(".") if pm else []
        if pkg and parts[len(parts) - len(pkg):] == pkg:
            parts = parts[:len(parts) - len(pkg)]
    return set(parts) & _NOT_PRODUCT_DIRS


def is_generated(repo: Path, rel: str) -> bool:
    """Generated code: a protobuf module name, or a head that says it is generated and not to be edited
    (``# Generated by ... DO NOT EDIT!``, ``// Code generated ... DO NOT EDIT.``, ``@generated``)."""
    if _GENERATED_NAME.search(rel):
        return True
    try:
        with open(Path(repo) / rel, "rb") as fh:
            head = fh.read(1200).decode("utf-8", errors="replace").lower()
    except OSError:
        return False
    return "@generated" in head or ("generated" in head and re.search(r"do not edit|don't edit", head) is not None)


def scope_files(repo: Path, guard: dict, all_files: list[str],
                decisions: Path | None = None) -> tuple[list[str], list[str]]:
    """(files the guard checks, what the scope leaves out - for the limits). ``decisions``: the decisions
    folder in use (default :func:`verinoda.decisions.decisions_dir`)."""
    from verinoda.decisions import decisions_dir

    repo = Path(repo)
    left_out: list[str] = []
    try:
        ddir = (decisions or decisions_dir(repo)).relative_to(repo.resolve()).as_posix()
    except Exception:  # noqa: BLE001
        ddir = ".verinoda/decisions"
    roots = [] if guard.get("scope") == "all" else _excluded_roots(repo)
    out = []
    n_tests = n_ref = 0
    samples: list[str] = []
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
            if not_product_dirs(repo, rel):
                samples.append(rel)
                continue
        out.append(rel)
    if n_tests:
        left_out.append(f"{n_tests} test file(s) are out of scope (scope=all includes them)")
    if n_ref:
        left_out.append(f"{n_ref} file(s) in reference trees or detected copies are out of scope")
    if samples:
        left_out.append(f"{len(samples)} file(s) under example, sample, demo, fixture or vendor folders are out of "
                        f"scope (scope=all includes them): {', '.join(samples[:3])}{' ...' if len(samples) > 3 else ''}")
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


# the kinds of binding whose value the engine does not know (a parameter may be the imported function passed in)
_UNKNOWN_VALUE = {"param", "for", "with", "except", "comp", "walrus", "assign", "augassign", "del", "match"}
_LITERALS = (ast.Constant, ast.JoinedStr, ast.List, ast.Tuple, ast.Dict, ast.Set)
_TRY_TYPES = tuple(t for t in (getattr(ast, "Try", None), getattr(ast, "TryStar", None)) if t is not None)
_MATCH = getattr(ast, "Match", None)


@dataclass
class _Bind:
    """One binding of a name: ``import``/``from`` (``qual``), ``alias`` (``g = m.f``: ``value``), ``def``,
    ``class``, ``literal`` or one whose value is not known (``_UNKNOWN_VALUE``)."""
    name: str
    kind: str
    pos: tuple[int, int]
    cond: bool                       # under if / for / while / try / except / match: it may not run
    qual: str | None = None
    value: ast.AST | None = None
    scope: "_Scope | None" = None    # where the statement is written (an alias' value is evaluated there)

    def text(self) -> str:
        where = self.scope.name if self.scope is not None and self.scope.kind == "function" else ""
        return {"param": f"a parameter of {where or 'the enclosing function'}",
                "for": "a for-loop variable", "with": "bound by a with statement",
                "except": "an exception name (except ... as)", "comp": "a comprehension variable",
                "walrus": "assigned with :=", "assign": "assigned", "augassign": "reassigned (augmented assignment)",
                "del": "deleted", "match": "a match-pattern capture", "def": "a def", "class": "a class",
                "literal": "assigned a literal", "alias": "an alias", "import": f"imported as {self.qual}",
                "from": f"imported as {self.qual}"}.get(self.kind, self.kind) + f" (line {self.pos[0]})"


class _Scope:
    __slots__ = ("kind", "name", "parent", "binds", "globals", "nonlocals", "stars")

    def __init__(self, kind: str, name: str, parent: "_Scope | None"):
        self.kind, self.name, self.parent = kind, name, parent
        self.binds: dict[str, list[_Bind]] = {}
        self.globals: set[str] = set()
        self.nonlocals: set[str] = set()
        self.stars: list[str] = []


class _Scopes:
    """Python name scopes of one file (module, function, lambda, class, comprehension), as Python resolves
    names: every binding of a name in each scope, ``global`` / ``nonlocal``, and the scope each call and
    each value reference runs in. Branches are kept: a binding under if/for/while/try/except/match is
    conditional, so the bindings that can reach a use are the last unconditional one and every conditional
    one after it."""

    def __init__(self, tree: ast.AST, pkg: str):
        self.pkg = pkg
        self.module = _Scope("module", "<module>", None)
        self.uses: list[tuple[ast.Call, _Scope]] = []
        self.refs: list[tuple[ast.AST, _Scope, bool]] = []   # (value reference, scope, followed as an alias)
        self.by_name: dict[str, list[_Bind]] = {}            # every binding of a name, any scope
        self.module_calls: list[tuple[int, int]] = []        # calls made while the module runs (import time)
        self._stmts(tree.body, self.module, False)

    # -- building ----------------------------------------------------------------------------------
    def _bind(self, scope: _Scope, name: str, kind: str, node: ast.AST, cond: bool, *, qual: str | None = None,
              value: ast.AST | None = None) -> None:
        tgt = scope
        if name in scope.globals:
            tgt, cond = self.module, True           # set when that function runs
        elif name in scope.nonlocals:
            tgt = scope.parent
            while tgt is not None and tgt.kind != "function":
                tgt = tgt.parent
            tgt, cond = (tgt or self.module), True
        b = _Bind(name, kind, (getattr(node, "lineno", 0), getattr(node, "col_offset", 0)), cond, qual, value, scope)
        tgt.binds.setdefault(name, []).append(b)
        self.by_name.setdefault(name, []).append(b)

    def _from_base(self, node: ast.ImportFrom) -> str:
        base = node.module or ""
        if node.level:
            parts = self.pkg.split(".") if self.pkg else []
            parts = parts[: len(parts) - (node.level - 1)] if node.level > 1 else parts
            base = ".".join([*parts, *([base] if base else [])])
        return base

    def _declare(self, body: list, scope: _Scope) -> None:
        """``global`` / ``nonlocal`` anywhere in a function body (not in nested scopes) apply to all of it."""
        stack = list(body)
        while stack:
            st = stack.pop()
            if isinstance(st, ast.Global):
                scope.globals.update(st.names)
            elif isinstance(st, ast.Nonlocal):
                scope.nonlocals.update(st.names)
            elif not isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                for child in ast.iter_child_nodes(st):
                    if isinstance(child, (ast.stmt, ast.excepthandler)) or type(child).__name__ == "match_case":
                        stack.append(child)

    def _params(self, args: ast.arguments, scope: _Scope, node: ast.AST) -> None:
        for a in [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]:
            if a is not None:
                self._bind(scope, a.arg, "param", node, False)

    def _targets(self, t: ast.AST, scope: _Scope, kind: str, cond: bool) -> None:
        if isinstance(t, ast.Name):
            self._bind(scope, t.id, kind, t, cond)
        elif isinstance(t, (ast.Tuple, ast.List)):
            for e in t.elts:
                self._targets(e, scope, kind, cond)
        elif isinstance(t, ast.Starred):
            self._targets(t.value, scope, kind, cond)
        elif isinstance(t, ast.Attribute):
            self._expr(t.value, scope, cond)
        elif isinstance(t, ast.Subscript):
            self._expr(t.value, scope, cond)
            self._expr(t.slice, scope, cond)

    def _pattern(self, p: ast.AST, scope: _Scope) -> None:
        for n in ast.walk(p):
            name = getattr(n, "name", None) if type(n).__name__ in ("MatchAs", "MatchStar") else \
                getattr(n, "rest", None) if type(n).__name__ == "MatchMapping" else None
            if name:
                self._bind(scope, name, "match", n, True)
            if type(n).__name__ == "MatchValue":
                self._expr(n.value, scope, True)

    def _stmts(self, body: list, scope: _Scope, cond: bool) -> None:
        for st in body:
            self._stmt(st, scope, cond)

    def _stmt(self, st: ast.AST, scope: _Scope, cond: bool) -> None:
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for d in [*st.decorator_list, *st.args.defaults, *[d for d in st.args.kw_defaults if d is not None]]:
                self._expr(d, scope, cond)
            self._bind(scope, st.name, "def", st, cond)
            s = _Scope("function", st.name, scope)
            self._declare(st.body, s)
            self._params(st.args, s, st)
            self._stmts(st.body, s, False)
        elif isinstance(st, ast.ClassDef):
            for d in [*st.decorator_list, *st.bases, *[k.value for k in st.keywords]]:
                self._expr(d, scope, cond)
            self._bind(scope, st.name, "class", st, cond)
            self._stmts(st.body, _Scope("class", st.name, scope), False)
        elif isinstance(st, ast.Import):
            for a in st.names:
                if a.asname:
                    self._bind(scope, a.asname, "import", st, cond, qual=a.name)
                else:
                    root = a.name.split(".")[0]
                    self._bind(scope, root, "import", st, cond, qual=root)
        elif isinstance(st, ast.ImportFrom):
            base = self._from_base(st)
            for a in st.names:
                if a.name == "*":
                    scope.stars.append(base)
                else:
                    self._bind(scope, a.asname or a.name, "from", st, cond,
                               qual=f"{base}.{a.name}" if base else a.name)
        elif isinstance(st, (ast.Assign, ast.AnnAssign)):
            value = st.value
            targets = st.targets if isinstance(st, ast.Assign) else [st.target]
            if value is None:  # a bare annotation binds nothing
                return
            chain = value
            while isinstance(chain, ast.Attribute):
                chain = chain.value
            if isinstance(value, (ast.Name, ast.Attribute)) and isinstance(chain, ast.Name):
                # `g = m.f`: calls through g are followed where g is a module or function name; a class
                # attribute is reached through self/cls, which is not followed
                self.refs.append((value, scope, scope.kind != "class" and all(isinstance(t, ast.Name)
                                                                              for t in targets)))
            else:
                self._expr(value, scope, cond)
            for t in targets:
                if isinstance(t, ast.Name):
                    kind = "alias" if isinstance(value, (ast.Name, ast.Attribute)) else \
                        "literal" if isinstance(value, _LITERALS) else "assign"
                    self._bind(scope, t.id, kind, t, cond, value=value if kind == "alias" else None)
                else:
                    self._targets(t, scope, "assign", cond)
        elif isinstance(st, ast.AugAssign):
            self._expr(st.value, scope, cond)
            if isinstance(st.target, ast.Name):
                self._bind(scope, st.target.id, "augassign", st.target, cond)
            else:
                self._targets(st.target, scope, "augassign", cond)
        elif isinstance(st, (ast.For, ast.AsyncFor)):
            self._expr(st.iter, scope, cond)
            self._targets(st.target, scope, "for", True)
            self._stmts(st.body, scope, True)
            self._stmts(st.orelse, scope, True)
        elif isinstance(st, (ast.While, ast.If)):
            self._expr(st.test, scope, cond)
            self._stmts(st.body, scope, True)
            self._stmts(st.orelse, scope, True)
        elif isinstance(st, (ast.With, ast.AsyncWith)):
            for it in st.items:
                self._expr(it.context_expr, scope, cond)
                if it.optional_vars is not None:
                    self._targets(it.optional_vars, scope, "with", cond)
            self._stmts(st.body, scope, cond)
        elif _TRY_TYPES and isinstance(st, _TRY_TYPES):
            self._stmts(st.body, scope, True)
            for h in st.handlers:
                if h.type is not None:
                    self._expr(h.type, scope, True)
                if h.name:
                    self._bind(scope, h.name, "except", h, True)
                self._stmts(h.body, scope, True)
            self._stmts(st.orelse, scope, True)
            self._stmts(st.finalbody, scope, cond)
        elif _MATCH is not None and isinstance(st, _MATCH):
            self._expr(st.subject, scope, cond)
            for c in st.cases:
                self._pattern(c.pattern, scope)
                if c.guard is not None:
                    self._expr(c.guard, scope, True)
                self._stmts(c.body, scope, True)
        elif isinstance(st, ast.Delete):
            for t in st.targets:
                self._targets(t, scope, "del", cond)
        elif isinstance(st, (ast.Global, ast.Nonlocal)):
            return
        else:
            for child in ast.iter_child_nodes(st):
                if isinstance(child, ast.stmt):
                    self._stmt(child, scope, cond)
                elif isinstance(child, ast.expr):
                    self._expr(child, scope, cond)

    def _expr(self, e: ast.AST | None, scope: _Scope, cond: bool) -> None:
        if e is None:
            return
        if isinstance(e, ast.Call):
            self.uses.append((e, scope))
            s = scope
            while s is not None and s.kind != "function":
                s = s.parent
            if s is None:  # not inside a def or lambda: it runs while the module runs
                self.module_calls.append((getattr(e, "lineno", 0), getattr(e, "col_offset", 0)))
            base = e.func
            while isinstance(base, ast.Attribute):
                base = base.value
            if not isinstance(base, ast.Name):
                self._expr(base, scope, cond)
            for a in e.args:
                self._expr(a, scope, cond)
            for k in e.keywords:
                self._expr(k.value, scope, cond)
            return
        if isinstance(e, (ast.Attribute, ast.Name)) and isinstance(getattr(e, "ctx", None), ast.Load):
            base = e
            while isinstance(base, ast.Attribute):
                base = base.value
            if isinstance(base, ast.Name):
                self.refs.append((e, scope, False))
            else:
                self._expr(base, scope, cond)
            return
        if isinstance(e, ast.Lambda):
            for d in [*e.args.defaults, *[d for d in e.args.kw_defaults if d is not None]]:
                self._expr(d, scope, cond)
            s = _Scope("function", "<lambda>", scope)
            self._params(e.args, s, e)
            self._expr(e.body, s, False)
            return
        if isinstance(e, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            s = _Scope("comp", "<comprehension>", scope)
            for i, gen in enumerate(e.generators):
                self._expr(gen.iter, scope if i == 0 else s, cond)
                self._targets(gen.target, s, "comp", False)
                for c in gen.ifs:
                    self._expr(c, s, False)
            if isinstance(e, ast.DictComp):
                self._expr(e.key, s, False)
                self._expr(e.value, s, False)
            else:
                self._expr(e.elt, s, False)
            return
        if isinstance(e, ast.NamedExpr):
            self._expr(e.value, scope, cond)
            tgt = scope
            while tgt.kind == "comp" and tgt.parent is not None:
                tgt = tgt.parent
            self._bind(tgt, e.target.id, "walrus", e.target, cond or tgt is not scope)
            return
        for child in ast.iter_child_nodes(e):
            self._expr(child, scope, cond)

    # -- resolving -----------------------------------------------------------------------------------
    def lookup(self, scope: _Scope, name: str) -> _Scope | None:
        """The scope ``name`` resolves to from ``scope`` (a class body is seen only by its own statements)."""
        s, first = scope, True
        while s is not None:
            if s.kind == "class" and not first:
                s = s.parent
                continue
            if name in s.globals:
                return self.module if name in self.module.binds else None
            if name in s.nonlocals:
                s, first = s.parent, False
                continue
            if name in s.binds:
                return s
            s, first = s.parent, False
        return None

    @staticmethod
    def deferred(use: _Scope, bound: _Scope) -> bool:
        """Does the use run later than the binding scope's own statements (inside a def or lambda)?"""
        s = use
        while s is not None and s is not bound:
            if s.kind == "function":
                return True
            s = s.parent
        return False

    def reaching(self, scope: _Scope, name: str, pos: tuple[int, int], deferred: bool, *,
                 import_time: bool = False) -> list[_Bind]:
        """The bindings of ``name`` in ``scope`` that can reach a use at ``pos``. ``import_time``: the use is
        in a function that a module-level call may run before a later module binding replaces an earlier
        one (``CONN = open_all()`` above ``sqlite3 = None``): that earlier binding reaches it too."""
        binds = sorted(scope.binds.get(name) or [], key=lambda b: b.pos)
        if scope.kind == "comp":
            return binds
        cand = binds if deferred else ([b for b in binds if b.pos < pos] or binds)
        last = None
        for b in cand:
            if not b.cond:
                last = b
        out = [b for b in cand if b is last or (b.cond and (last is None or b.pos > last.pos))]
        if import_time and deferred and scope is self.module and self.module_calls:
            # only an earlier import or alias (it may be the target): an earlier literal or def that a later
            # import replaces only fails at import time, and every later call reaches the import
            uncond = [b for b in cand if not b.cond]
            for b, nxt in zip(uncond, uncond[1:]):
                if b.kind in ("import", "from", "alias") and all(b is not o for o in out) and \
                        any(b.pos < p < nxt.pos for p in self.module_calls):
                    out.append(b)
        return out

    def qualify(self, expr: ast.AST, scope: _Scope, depth: int = 0) -> list[tuple[str | None, _Bind]] | None:
        """``[(qualified name or None, binding)]`` for each binding of ``expr``'s root name that can reach it;
        ``[]`` when the name is bound nowhere in the file; None when ``expr`` is not a dotted name."""
        attrs = []
        e = expr
        while isinstance(e, ast.Attribute):
            attrs.append(e.attr)
            e = e.value
        if not isinstance(e, ast.Name):
            return None
        attrs.reverse()
        s = self.lookup(scope, e.id)
        if s is None:
            return []
        out: list[tuple[str | None, _Bind]] = []
        for b in self.reaching(s, e.id, (getattr(expr, "lineno", 0), getattr(expr, "col_offset", 0)),
                               self.deferred(scope, s), import_time=True):
            if b.kind in ("import", "from"):
                out.append((".".join([b.qual, *attrs]), b))
            elif b.kind == "alias" and depth < 3 and b.value is not None and b.scope is not None:
                sub = self.qualify(b.value, b.scope, depth + 1) or []
                out += [(".".join([q, *attrs]) if q else None, b) for q, _ in sub] or [(None, b)]
            else:
                out.append((None, b))
        return out

    def exports(self, name: str) -> list[tuple[str | None, _Bind]]:
        """What a module-level name holds once the module has run (its final bindings)."""
        out: list[tuple[str | None, _Bind]] = []
        for b in self.reaching(self.module, name, (1 << 30, 0), True):
            if b.kind in ("import", "from"):
                out.append((b.qual, b))
            elif b.kind == "alias" and b.value is not None and b.scope is not None:
                sub = self.qualify(b.value, b.scope, 1) or []
                out += [(q, b) for q, _ in sub] or [(None, b)]
            else:
                out.append((None, b))
        return out


class _PyIndex:
    """Parsed Python files of one repository, parsed on demand, with their name scopes."""

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
        self._scopes: dict[str, _Scopes | None] = {}

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

    def scopes(self, rel: str) -> _Scopes | None:
        if rel not in self._scopes:
            tree, _ = self.tree(rel)
            sc = None
            if tree is not None:
                pkg = _module_of(rel) or ""
                if not rel.endswith("__init__.py"):
                    pkg = pkg.rpartition(".")[0]
                try:
                    sc = _Scopes(tree, pkg)
                except RecursionError:
                    sc = None
            self._scopes[rel] = sc
        return self._scopes[rel]

    def resolve(self, qual: str, depth: int = 0) -> list[tuple[str, tuple[str, ...]]]:
        """Follow a name through the project's own modules: ``[(final name, files passed through)]``, one
        per binding that can hold it (``pkg.util.open_db`` -> ``[("sqlite3.connect", ("pkg/util.py",))]``).
        A module-level name that is not an import or alias (a def, a literal, a parameter-like binding)
        stops the chain: its own qualified name is returned."""
        if depth > 4 or "." not in qual:
            return [(qual, ())]
        parts = qual.split(".")
        for i in range(len(parts) - 1, 0, -1):
            mod, rest = ".".join(parts[:i]), parts[i:]
            rel = self.modules.get(mod)
            if rel is None:
                continue
            sc = self.scopes(rel)
            if sc is None or rest[0] not in sc.module.binds:
                return [(qual, ())]
            out: list[tuple[str, tuple[str, ...]]] = []
            for q, _b in sc.exports(rest[0]):
                if q is None:
                    out.append((qual, (rel,)))
                    continue
                for full, via in self.resolve(".".join([q, *rest[1:]]), depth + 1):
                    out.append((full, (rel, *via)))
            return list(dict.fromkeys(out)) or [(qual, ())]
        return [(qual, ())]


def _py_only_in(ix: _PyIndex, rel: str, targets: dict[str, str], scan: Scan,
                names: set[str] | None = None) -> list[tuple[str, int, str]]:
    """``(level, line, why)`` for calls in ``rel`` that bind to one of ``targets`` (qualified -> shown).

    ``names``: the last parts a call's qualified name can have to reach a target (the target functions
    and the names they are re-exported under); any other call is not followed through the project.
    The files a finding's binding passes through (re-exports) are kept in ``scan.via``."""
    tree, text = ix.tree(rel)
    if text is None:
        return []
    if tree is None:
        scan.unknown.append(f"{rel} does not parse as Python: not checked")
        return []
    scan.count("python")
    sc = ix.scopes(rel)
    if sc is None:
        scan.unknown.append(f"{rel} is nested too deeply to resolve its names: not checked")
        return []
    out: list[tuple[str, int, str]] = []
    funcs = {t.rpartition(".")[2] for t in targets}
    mods = {t.rpartition(".")[0] for t in targets}
    names = set(names or ()) | funcs

    def targets_of(q: str) -> list[tuple[str, tuple[str, ...]]]:
        return ix.resolve(q) if q.rpartition(".")[2] in names else [(q, ())]

    def shadowed(root: str, attrs: list[str]) -> str | None:
        """The target an import of ``root`` elsewhere in this file would give (the use is shadowed here)."""
        for b in sc.by_name.get(root) or []:
            if b.kind in ("import", "from") and b.qual:
                for full, _via in targets_of(".".join([b.qual, *attrs])):
                    if full in targets:
                        return full
        return None

    def judge(node: ast.AST, scope: _Scope, *, value_use: bool) -> None:
        expr = node.func if isinstance(node, ast.Call) else node
        reach = sc.qualify(expr, scope)
        if reach is None:
            return
        root = expr
        attrs = []
        while isinstance(root, ast.Attribute):
            attrs.append(root.attr)
            root = root.value
        attrs.reverse()
        if not reach:  # bound nowhere in the file: a builtin, or a name from a star import
            if not value_use and isinstance(expr, ast.Name) and expr.id in funcs:
                for base in sc.module.stars:
                    for full, _ in ix.resolve(f"{base}.{expr.id}" if base else expr.id):
                        if full in targets:
                            out.append((POSSIBLE, node.lineno, f"`{expr.id}()` may come from `from {base} import *`"))
            return
        hits, others, via = [], [], set()
        for q, b in reach:
            found = False
            if q is not None:
                for full, chain in targets_of(q):
                    if full in targets:
                        hits.append((full, q, b))
                        via.update(chain)
                        found = True
                    else:
                        others.append((q, b, full))
            if not found and q is None:
                others.append((None, b, None))
        written = ast.unparse(expr)
        if not hits:
            unknown = [b for q, b, _ in others if q is None and b.kind in _UNKNOWN_VALUE]
            full = shadowed(root.id, attrs) if unknown and not value_use else None
            if full is not None:
                out.append((POSSIBLE, node.lineno, f"call to {targets[full]}?"
                                                   f" `{root.id}` is {unknown[0].text()} here, so the imported "
                                                   "one may not be what is called"))
            return
        full, q, b = hits[0]
        shown = ", ".join(dict.fromkeys(targets[h[0]] for h in hits))
        if via:
            scan.via.setdefault((rel, node.lineno), set()).update(via)
        if value_use:
            out.append((POSSIBLE, node.lineno, f"{shown} is used as a value here (passed on or stored: "
                                               "functools.partial, a callback, a class attribute); calls through "
                                               "it are not followed"))
            return
        if others:
            oq, ob, _ = others[0]
            what = ob.text() if oq is None or ob.kind in ("import", "from") else f"{ob.text()} of {oq}"
            out.append((POSSIBLE, node.lineno, f"call to {shown}, but `{root.id}` can also be {what}"))
            return
        n_binds = len({h[2].pos for h in hits})
        parts = ([f"as `{written}`"] if written != full else []) + ([f"through {q}"] if q != full else []) + \
            (["bound by an assignment"] if any(h[2].kind == "alias" for h in hits) else []) + \
            ([f"imported in {n_binds} branches"] if n_binds > 1 else [])
        out.append((VIOLATED, node.lineno, f"call to {shown}" + (f" ({', '.join(parts)})" if parts else "")))

    for call, scope in sc.uses:
        f = call.func
        if isinstance(f, ast.Name) and f.id == "getattr" and len(call.args) >= 2 and \
                isinstance(call.args[1], ast.Constant) and call.args[1].value in funcs:
            for q, _b in sc.qualify(call.args[0], scope) or []:
                for m, _ in (ix.resolve(q) if q else []):
                    full = f"{m}.{call.args[1].value}"
                    if m in mods and full in targets:
                        out.append((POSSIBLE, call.lineno, f"getattr({m}, \"{call.args[1].value}\") reaches "
                                                           f"{targets[full]} dynamically"))
            continue
        if isinstance(f, (ast.Name, ast.Attribute)):
            judge(call, scope, value_use=False)
    for ref, scope, followed in sc.refs:
        last = ref.attr if isinstance(ref, ast.Attribute) else ref.id
        if not followed and last in names:
            judge(ref, scope, value_use=True)
    return sorted(set(out), key=lambda h: (h[1], h[0] != VIOLATED, h[2]))


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


# a name bound without a written type: a lambda parameter (`x ->`, `(x, y) ->`, `{ x ->`), Java `for (var x :`,
# a Kotlin loop variable (`for (x in`, `for ((k, x) in`), Kotlin destructuring (`val (a, x) =`)
_UNTYPED_BINDERS = (r"(?<![\w$.]){n}\s*(?:,\s*[\w$]+\s*)*\)?\s*->",
                    r"\bfor\s*\(\s*(?:final\s+)?var\s+{n}\s*:",
                    r"\bfor\s*\(\s*\(?\s*(?:[\w$]+\s*,\s*)*{n}\s*(?:,\s*[\w$]+\s*)*\)?\s+in\b",
                    r"\b(?:val|var)\s*\(\s*(?:[\w$]+\s*,\s*)*{n}\s*[,)]")


def _declared_type(code_lines: list[str], name: str) -> tuple[set[str], list[int]]:
    """(types ``name`` is declared with, lines where it is bound without a written type), anywhere in the
    file (Java and Kotlin forms). Scopes are not resolved, so every declaration of the name counts: a
    lambda parameter or loop variable of the same name elsewhere in the file keeps a call POSSIBLE."""
    types: set[str] = set()
    untyped: list[int] = []
    n = re.escape(name)
    typed_rx = rf"\b([A-Z][\w.]*)(?:<[^;(){{}}=]*>)?(?:\[\])?\s+{n}\s*[=;,):]"
    for i, ln in enumerate(code_lines, 1):
        if name not in ln:
            continue
        typed = False
        for m in re.finditer(typed_rx, ln):
            types.add(m.group(1))
            typed = True
        for m in re.finditer(rf"\b{n}\s*:\s*([A-Z][\w.]*)", ln):
            types.add(m.group(1))
            typed = True
        for m in re.finditer(rf"\b(?:val|var)\s+{n}\s*=\s*([A-Z][\w.]*)\s*[(<]", ln):
            types.add(m.group(1))
        if re.search(rf"\b(?:val|var)\s+{n}\s*=", ln) and not re.search(rf"\b(?:val|var)\s+{n}\s*=\s*[A-Z]", ln):
            untyped.append(i)
        elif re.search(rf"\bvar\s+{n}\s*=", ln) and ln.lstrip().startswith("var "):
            untyped.append(i)
        elif not typed and any(re.search(rx.replace("{n}", n), ln) for rx in _UNTYPED_BINDERS):
            untyped.append(i)
    return types, untyped


def _jvm_only_in(repo: Path, rel: str, targets: dict[str, str], scan: Scan) -> list[tuple[str, int, str]]:
    text = _read(Path(repo) / rel)
    if text is None:
        scan.unknown.append(_UNREAD.format(rel=rel))
        return []
    suffix = PurePosixPath(rel).suffix.lower()
    methods = {_jvm_target(t)[2] for t in targets}
    if not any(m in text for m in methods):  # cheap first: most files name none of the methods
        scan.count("jvm")
        return []
    code = code_text(text, suffix)
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
            if c in explicit:  # imported, perhaps under another name (Kotlin `import a.B as C`)
                p, _, k = explicit[c].rpartition(".")
                return (k == simple and (not tpkg or p == tpkg)), \
                    f"imported from {p}" + (f" as {c}" if c != k else "")
            if c != simple:
                return False, ""
            if tpkg and (pkg == tpkg or tpkg in wild):
                return True, "same package" if pkg == tpkg else f"imported with {tpkg}.*"
            return False, "not imported in this file"

        def real(written: str) -> str:
            """The simple class name a written type denotes (through an import alias)."""
            s = written.rpartition(".")[2]
            return explicit[s].rpartition(".")[2] if s in explicit and "." not in written else s

        decl: dict[str, tuple[set[str], list[int]]] = {}
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
                types, untyped = decl.setdefault(recv, _declared_type(lines, recv))
                named = {real(x) for x in types}
                if untyped and (simple in named or not types):
                    out.append((POSSIBLE, line, f"`{recv}.{name}(...)`: `{recv}` is bound without a written type "
                                                f"at line {untyped[0]} (a lambda parameter, `var` or a loop "
                                                "variable): its type is not resolved"))
                    continue
                if untyped:  # declared with other classes here, but also bound without a type elsewhere
                    out.append((POSSIBLE, line, f"`{recv}.{name}(...)`: the type of `{recv}` is not resolved "
                                                f"(declared {', '.join(sorted(types))}, and without a written type "
                                                f"at line {untyped[0]})"))
                    continue
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
                if types:
                    continue  # declared with another class only: not the target
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
        scan.unknown.append(_UNREAD.format(rel=rel))
        return []
    scan.count("text")
    if not any(t.rpartition(".")[2] in text for t in targets):  # cheap first
        return []
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
    def __init__(self, repo: Path, all_files: list[str], graph=None, decisions: Path | None = None,
                 graph_stale: str | None = None):
        self.repo = Path(repo)
        self.all_files = all_files
        self.graph = graph
        self.decisions = decisions  # the decisions folder in use (None: decisions.decisions_dir)
        self.graph_stale = graph_stale  # why ``graph`` may not describe the working tree (a failed refresh)
        self._py: _PyIndex | None = None
        self._deps: dict | None = None
        self._code: dict[str, list[str]] = {}

    def code_lines(self, rel: str) -> list[str]:
        """The file's lines with comments and strings removed, masked once per check."""
        if rel not in self._code:
            text = _read(self.repo / rel)
            self._code[rel] = [] if text is None else code_text(text, PurePosixPath(rel).suffix).split("\n")
        return self._code[rel]

    @property
    def py(self) -> _PyIndex:
        if self._py is None:
            self._py = _PyIndex(self.repo, self.all_files)
        return self._py

    def deps(self) -> dict:
        if self._deps is None:
            self._deps = declared_dependencies(self.repo, self.all_files)
        return self._deps


def check_only_in(ctx: _Ctx, g: dict) -> tuple[list[tuple[str, str, int, str]], Scan, str]:
    """``[(level, rel, line, why)]``, the scan, and a short description of what is guarded."""
    scan = Scan()
    files, left_out = scope_files(ctx.repo, g, ctx.all_files, ctx.decisions)
    targets, what = _targets(g)
    what += f" (allowed: {', '.join(g.get('allowed') or [])})"
    out: list[tuple[str, str, int, str]] = []
    if targets:
        py_files = [f for f in files if f.endswith(PY_SUFFIXES)]
        # A re-export - a module that binds the target (by import or assignment) - makes its own name a
        # target too; a chain of them is followed a few rounds deep. A file can reach a target only by
        # naming its module (import, getattr, importlib) or a module of the project that re-exports it:
        # every other file is skipped without parsing. JVM classes (java.sql.DriverManager) are no
        # Python targets.
        py_targets = {t: s for t, s in targets.items() if not _jvm_target(t)[1]}
        roots = {t.split(".")[0] for t in py_targets}
        names = {t.rpartition(".")[2] for t in py_targets}
        reexporters: set[str] = set()

        def candidate(text: str | None) -> bool:
            return text is not None and (any(r in text for r in roots) or any(m in text for m in reexporters))

        all_py = [r for r in ctx.all_files if r.endswith(PY_SUFFIXES)]
        texts = {r: _read(ctx.repo / r) for r in all_py}
        for _round in range(4):
            grew = False
            for rel in all_py:
                if not candidate(texts.get(rel)):
                    continue
                sc = ctx.py.scopes(rel)
                if sc is None:
                    continue
                found = False
                for name in list(sc.module.binds):  # what the module's names hold once it has run
                    for q, _b in sc.exports(name):
                        if q and q.rpartition(".")[2] in names and \
                                any(full in py_targets for full, _ in ctx.py.resolve(q)):
                            found = True
                            if name not in names:
                                names.add(name)
                                grew = True
                mod = (_module_of(rel) or "").rpartition(".")[2]
                if found and mod and mod not in reexporters:
                    reexporters.add(mod)
                    grew = True
            if not grew:
                break
        for rel in py_files:
            text = texts.get(rel)
            if text is None:
                scan.unknown.append(_UNREAD.format(rel=rel))
                continue
            if not candidate(text):
                scan.count("python")
                continue
            hits = _py_only_in(ctx.py, rel, py_targets, scan, names)
            if "import_module" in text or "__import__" in text:
                hits += _dynamic_import_hint(rel, text, code_text(text, ".py"), py_targets)
            for level, line, why in hits:
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
                scan.unknown.append(_UNREAD.format(rel=rel))
                continue
            scan.count("text")
            code = code_text(text, PurePosixPath(rel).suffix).split("\n") if not g.get("sink") else text.split("\n")
            for i, ln in enumerate(code, 1):
                if any(rx.search(ln) for rx in rxs):
                    out.append((POSSIBLE, rel, i, f"{what} matches (text; a pattern is never a verified call)"))
        scan.limit("a pattern or a text sink is a text match: POSSIBLE at most"
                   + ("; comments and strings are removed first" if not g.get("sink") else ""))
    if not sum(scan.files.values()) and not scan.unknown:
        scan.unknown.append("no file was checked: the guard's scope holds no code file outside the allowed ones"
                            + (f" ({'; '.join(left_out)})" if left_out else "")
                            + ", so nothing shows that the decision holds")
    if g.get("scope") != "all":  # generated code (only the files with a hit are read for the marker)
        gen = sorted({rel for _lvl, rel, _i, _w in out if is_generated(ctx.repo, rel)})
        if gen:
            out = [h for h in out if h[1] not in gen]
            left_out.append(f"{len(gen)} generated file(s) (marked generated / do not edit) with a match are out of "
                            f"scope (scope=all includes them; a call there comes from its generator): "
                            f"{', '.join(gen[:3])}{' ...' if len(gen) > 3 else ''}")
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


def _named_in_code(ctx: _Ctx, rel: str, line: int, name: str) -> bool:
    code = ctx.code_lines(rel)
    return 0 < line <= len(code) and bool(re.search(rf"(?<![\w$]){re.escape(name)}(?![\w$])", code[line - 1]))


def check_no_edge(ctx: _Ctx, g: dict) -> tuple[list[tuple[str, str, int, str]], Scan, str]:
    scan = Scan()
    what = f"{g['from']} -> {g['to']} ({', '.join(g.get('relations') or [])})"
    if ctx.graph is None:
        scan.unknown.append("no index: run `verinoda scan` (no_edge reads the graph's edges)")
        return [], scan, what
    if ctx.graph_stale:  # the edges read below describe an older tree: nothing found there is not "ok"
        scan.unknown.append(f"{ctx.graph_stale}; an edge a recent edit added is not seen (run `verinoda update`, "
                            "then check again)")
    rels = set(g.get("relations") or [])
    # what the guard looks at: the indexed files each side matches, and the edges out of the `from` files
    indexed = {f for _n, f in ctx.graph.G.nodes(data="source_file") if f}
    from_files = {f for f in indexed if glob_match(f, g["from"])}
    to_files = {f for f in indexed if glob_match(f, g["to"])}
    scan.files.update({"from_files": len(from_files), "to_files": len(to_files), "edges_checked": 0})
    scan.limit(*EDGE_LIMITS)
    for side, files in (("from", from_files), ("to", to_files)):
        if not files:
            dotted = re.fullmatch(r"[A-Za-z_][\w]*(?:\.[\w*]+)+", g[side]) is not None
            scan.unknown.append(f"{side}={g[side]} matches no file in the index, so no edge was checked"
                                + (": no_edge compares the project's own files (a path glob such as src/client/**); "
                                   "a package outside the project is not a node - for calls into it use `only_in "
                                   "calls=Cls.method allowed=...`" if dotted else
                                   " (a path or glob relative to the project root, such as src/client/**)"))
    if scan.unknown:
        return [], scan, what
    out = []
    n = 0
    for u, v, d in ctx.graph.edges(rels or None):
        fu, fv = ctx.graph.file(u), ctx.graph.file(v)
        if not fu or fu not in from_files:
            continue
        scan.files["edges_checked"] += 1
        if not fv or fv not in to_files:
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
        scan.via.setdefault((src, line or 0), set()).add(fv)  # a changed target file makes the edge new
        if line is None:
            out.append((POSSIBLE, src, 0, f"{rel_name} edge to {fv} ({conf}) without a cited line"))
        elif conf == "EXTRACTED" and _named_in_code(ctx, src, line, target):
            out.append((VIOLATED, src, line, f"{rel_name} {target} in {fv} (EXTRACTED edge; the line names it)"))
        elif conf == "EXTRACTED":
            out.append((POSSIBLE, src, line, f"{rel_name} {target} in {fv}: EXTRACTED edge, but the line no "
                                             "longer names it (the index may be older than the file)"))
        else:
            out.append((POSSIBLE, src, line, f"{rel_name} {target} in {fv} ({conf} edge: a resolver's guess)"))
    scan.files["edges_matched"] = n
    if not scan.files["edges_checked"]:
        # A from file with no such edge was looked at only when its language's extractor emits these relations:
        # shown by such an edge out of another file of the same language (a leaf module: constants.py). A
        # language with no such edge anywhere in the index may not be extracted at all: unknown.
        kinds = '/'.join(sorted(rels)) or 'graph'
        langs = {PurePosixPath(f).suffix.lower() for u, _v, _d in ctx.graph.edges(rels or None)
                 if (f := ctx.graph.file(u))}
        seen = sorted(f for f in from_files if PurePosixPath(f).suffix.lower() in langs)
        if not seen:
            scan.unknown.append(f"the index has no {kinds} edge out of the {len(from_files)} file(s) "
                                f"from={g['from']} matches, and none out of any other file of their language, so "
                                "no edge was checked (the index may not extract these relations for it)")
        else:
            scan.files["from_files_looked_at"] = len(seen)
            scan.limit(f"no {kinds} edge leaves the {len(from_files)} from file(s): the index extracts these "
                       f"relations for their language (it has such edges out of other files), so {len(seen)} "
                       f"{'was' if len(seen) == 1 else 'were'} looked at and hold{'s' if len(seen) == 1 else ''} none")
            rest = sorted(from_files - set(seen))
            if rest:
                scan.limit(f"{len(rest)} from file(s) are in a language with no {kinds} edge anywhere in the index, "
                           f"so nothing was checked for them: {', '.join(rest[:5])}{' ...' if len(rest) > 5 else ''}")
    return _strongest(out), scan, what


# -- dependencies (manifests, with Gradle and Maven) -------------------------------------------------

GRADLE_FILES = ("build.gradle", "build.gradle.kts")
# folders whose build files are not the project's own dependencies (samples, fixtures, vendored or generated
# code); test folders are recognised by the test-file rule
_NOT_PROJECT_BUILD = {"node_modules", "build", ".gradle", "target", "vendor", "third_party", "third-party", "examples",
                      "example", "samples", "sample", "fixtures", "fixture", "__fixtures__", "testdata", "test-data",
                      "demo", "demos", ".verinoda"}


def build_code(text: str, rel: str) -> str:
    """A Gradle or Maven build file with its comments blanked (line and column positions kept): a
    commented-out dependency is not declared. Other files are returned as they are."""
    name = PurePosixPath(rel).name
    if name == "pom.xml" or name.endswith(".xml"):
        return re.sub(r"<!--.*?(?:-->|\Z)", lambda m: _blank(m.group(0)), text, flags=re.S)
    if name.endswith((".gradle", ".gradle.kts")):
        return _clike_code(text, slash_comments=True, keep_strings=True)
    return text


def _included_builds(root: Path, files: set[str]) -> set[str]:
    """The root build files and the subproject build files the root build includes (settings.gradle(.kts)
    ``include``; the root pom's ``<modules>``, recursively)."""
    out = {f for f in (*GRADLE_FILES, "pom.xml") if f in files}
    for settings in ("settings.gradle", "settings.gradle.kts"):
        if settings not in files:
            continue
        for ln in build_code(_read(Path(root) / settings) or "", settings).split("\n"):
            if not re.match(r"\s*include\b", ln):
                continue
            for proj in re.findall(r"['\"]:?([\w.\-:/]+)['\"]", ln):
                d = proj.strip(":").replace(":", "/")
                out |= {f"{d}/{g}" for g in GRADLE_FILES if f"{d}/{g}" in files}
    todo = ["pom.xml"] if "pom.xml" in files else []
    while todo:
        pom = todo.pop()
        base = pom.rpartition("/")[0]
        for mod in re.findall(r"<module>\s*([^<]+?)\s*</module>", build_code(_read(Path(root) / pom) or "", pom)):
            sub = PurePosixPath(base, mod.strip().strip("/"), "pom.xml").as_posix()
            if sub in files and sub not in out and ".." not in sub.split("/"):
                out.add(sub)
                todo.append(sub)
    return out


def _catalogs(root: Path, files: list[str]) -> tuple[dict[str, dict], list[str]]:
    """Gradle version catalogs (``gradle/libs.versions.toml``; ``gradle/NAME.versions.toml`` is ``NAME``):
    ``{"libs.snakeyaml": {"name", "short", "spec", "via"}, "libs.plugins.loom": ..., "libs.bundles.x": [...]}``
    keyed by the accessor a build file writes, and the catalog files read."""
    try:
        import tomllib  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover - py3.10
        import tomli as tomllib  # type: ignore[no-redef]

    out: dict = {}
    read: list[str] = []
    for rel in sorted(f for f in files if re.fullmatch(r"gradle/[\w-]+\.versions\.toml", f)):
        raw = _read(Path(root) / rel)
        try:
            data = tomllib.loads(raw or "")
        except Exception:  # noqa: BLE001 - an unreadable catalog: nothing from it
            continue
        read.append(rel)
        acc = PurePosixPath(rel).name.split(".")[0]
        lines = (raw or "").split("\n")
        versions = data.get("versions") or {}

        def line_of(section: str, alias: str) -> int:
            sec = next((i for i, ln in enumerate(lines) if re.match(rf"\s*\[{section}\]", ln)), None)
            for i in range(sec + 1 if sec is not None else 0, len(lines)):
                if lines[i].lstrip().startswith("[") and sec is not None:
                    break
                if re.match(rf"\s*['\"]?{re.escape(alias)}['\"]?\s*=", lines[i]):
                    return i + 1
            return 1

        def version(v) -> str:
            ver = v.get("version") if isinstance(v, dict) else None
            if isinstance(ver, dict):  # `version.ref = "x"` is the table {"ref": "x"}; or strictly / require
                ver = versions.get(ver["ref"]) if ver.get("ref") else ver
                if isinstance(ver, dict):
                    ver = ver.get("strictly") or ver.get("require") or ver.get("prefer")
            return str(ver) if ver else "*"

        def key(alias: str) -> str:
            return re.sub(r"[-_.]+", ".", alias).lower()

        for alias, v in (data.get("libraries") or {}).items():
            if isinstance(v, str):
                g_, _, rest = v.partition(":")
                n_, _, spec = rest.partition(":")
            elif isinstance(v, dict):
                if v.get("module"):
                    g_, _, n_ = str(v["module"]).partition(":")
                else:
                    g_, n_ = str(v.get("group") or ""), str(v.get("name") or "")
                spec = version(v)
            else:
                continue
            if g_ and n_:
                out[f"{acc}.{key(alias)}"] = {"name": f"{g_}:{n_}".lower(), "short": n_.lower(), "spec": spec or "*",
                                              "via": f"{rel}:{line_of('libraries', alias)}"}
        for alias, v in (data.get("plugins") or {}).items():
            pid = v.get("id") if isinstance(v, dict) else str(v).partition(":")[0]
            if pid:
                out[f"{acc}.plugins.{key(alias)}"] = {"name": str(pid).lower(),
                                                      "short": str(pid).rpartition(".")[2].lower(),
                                                      "spec": version(v) if isinstance(v, dict) else
                                                      str(v).partition(":")[2] or "*",
                                                      "via": f"{rel}:{line_of('plugins', alias)}", "plugin": True}
        for alias, members in (data.get("bundles") or {}).items():
            if isinstance(members, list):
                out[f"{acc}.bundles.{key(alias)}"] = [f"{acc}.{key(str(m))}" for m in members]
    return out, read


_CATALOG_REF = re.compile(r"\b(\w+)\s*\(?\s*(?:(?:platform|enforcedPlatform)\s*\(\s*)?([a-z]\w*)\.([A-Za-z][\w.]*)")


def _catalog_items(text: str, rel: str, build: str, catalog: dict) -> list[dict]:
    """Dependencies a build file declares through a version catalog (``implementation(libs.snakeyaml)``,
    ``include(libs.snakeyaml)``, ``alias(libs.plugins.loom)``, ``libs.bundles.x``), cited at the build line."""
    accessors = {k.split(".")[0] for k in catalog}
    items = []
    for i, ln in enumerate(text.split("\n"), 1):
        for m in _CATALOG_REF.finditer(ln):
            if m.group(2) not in accessors:
                continue
            ref = re.sub(r"\.(?:get|asProvider)(?:\(\))?$", "", f"{m.group(2)}.{m.group(3)}".rstrip("."))
            entry = catalog.get(ref.lower())
            refs = [catalog.get(r) for r in entry] if isinstance(entry, list) else [entry]
            for e in refs:
                if not isinstance(e, dict):
                    continue
                items.append({"name": e["name"], "short": e["short"], "spec": e["spec"],
                              "scope": "plugin" if e.get("plugin") else m.group(1), "at": f"{rel}:{i}", "path": rel,
                              "line": i, "ecosystem": "gradle-plugin" if e.get("plugin") else "maven", "build": build,
                              "ref": ref, "via": e["via"]})
    return items


def _gradle_maven(root: Path, all_files: list[str] | None = None) -> tuple[list[dict], list[str], list[str]]:
    """(declarations in Gradle and Maven build files, build files not read, build files and catalogs read).

    Build files come from the project's file list (git-ignored ones are not read). Those under test,
    sample, fixture, vendor or build-output folders, reference trees and detected copies are not read;
    a build file the root build does not include is read, and its items say so (``build: other``).
    A dependency written through a version catalog (``gradle/libs.versions.toml``) is cited at the build
    line that uses it, with the catalog line (``via``)."""
    from verinoda.snapshot import list_files

    root = Path(root)
    files = all_files if all_files is not None else list_files(root)
    builds = [f for f in files if PurePosixPath(f).name in (*GRADLE_FILES, "pom.xml")]
    if not builds:
        return [], [], []
    included = _included_builds(root, set(files))
    roots = _excluded_roots(root)
    catalog, read = _catalogs(root, files)
    read += sorted(s for s in ("settings.gradle", "settings.gradle.kts") if s in files)
    items: list[dict] = []
    skipped: list[str] = []
    for rel in sorted(builds):
        parts = set(PurePosixPath(rel).parts[:-1])
        if rel not in included and (parts & _NOT_PROJECT_BUILD or is_test_file(rel) or
                                    any(rel.startswith(r + "/") for r in roots)):
            skipped.append(rel)
            continue
        read.append(rel)
        build = "project" if rel in included else "other"
        text = build_code(_read(root / rel) or "", rel)  # a commented-out dependency is not declared
        if catalog and not rel.endswith("pom.xml"):
            items += _catalog_items(text, rel, build, catalog)
        if rel.endswith("pom.xml"):
            managed = [(m.start(), m.end()) for m in re.finditer(r"<dependencyManagement>.*?</dependencyManagement>",
                                                                  text, re.S)]
            for m in re.finditer(r"<dependency>(.*?)</dependency>", text, re.S):
                if any(a <= m.start() < b for a, b in managed):
                    continue  # a version pin, not a dependency
                block = m.group(1).split("<exclusions>")[0]
                gm = re.search(r"<groupId>\s*([^<]+?)\s*</groupId>", block)
                am = re.search(r"<artifactId>\s*([^<]+?)\s*</artifactId>", block)
                if not gm or not am:
                    continue
                vm = re.search(r"<version>\s*([^<]+?)\s*</version>", block)
                sm = re.search(r"<scope>\s*([^<]+?)\s*</scope>", block)
                line = text.count("\n", 0, m.start(1) + am.start()) + 1  # the <artifactId> line names it
                items.append({"name": f"{gm.group(1)}:{am.group(1)}".lower(), "short": am.group(1).lower(),
                              "spec": vm.group(1) if vm else "*", "scope": sm.group(1) if sm else "compile",
                              "at": f"{rel}:{line}", "path": rel, "line": line, "ecosystem": "maven", "build": build})
            continue
        for i, ln in enumerate(text.split("\n"), 1):
            pm = re.match(r"\s*id\s*\(?\s*['\"]([\w.\-]+)['\"]\s*\)?(?:\s*version\s*\(?\s*['\"]([^'\"]+)['\"])?", ln)
            if pm:  # a Gradle plugin (net.neoforged.moddev, fabric-loom, org.jetbrains.kotlin.jvm)
                items.append({"name": pm.group(1).lower(), "short": pm.group(1).rpartition(".")[2].lower(),
                              "spec": pm.group(2) or "*", "scope": "plugin", "at": f"{rel}:{i}", "path": rel,
                              "line": i, "ecosystem": "gradle-plugin", "build": build})
                continue
            # include / jarJar / shadow bundle the library into the jar: cited at their own line
            m = re.search(r"\b(\w*(?:[Ii]mplementation|[Aa]pi|[Cc]ompileOnly|[Rr]untimeOnly|[Aa]nnotationProcessor"
                          r"|[Cc]ompile|[Ii]nclude|jarJar|[Ss]hadow))\s*\(?\s*['\"]([\w.\-]+):([\w.\-]+)"
                          r"(?::([^'\"]+))?['\"]", ln)
            if m:
                items.append({"name": f"{m.group(2)}:{m.group(3)}".lower(), "short": m.group(3).lower(),
                              "spec": m.group(4) or "*", "scope": m.group(1), "at": f"{rel}:{i}", "path": rel,
                              "line": i, "ecosystem": "maven", "build": build})
    return items, skipped, read


# folders npm, yarn and pnpm never take a workspace package from (whatever the root's globs say)
_WS_NEVER = {"node_modules", "bower_components", ".verinoda", ".git"}


def ws_patterns(glob: str) -> list[str]:
    """A workspace glob as npm, yarn and pnpm read it: ``./`` and a trailing ``/`` dropped, ``//`` collapsed,
    and ``{a,b}`` alternatives expanded (``"./packages/*"`` -> ``["packages/*"]``)."""
    g = re.sub(r"/{2,}", "/", glob.strip().replace("\\", "/"))
    while g.startswith("./"):
        g = g[2:]
    g = g.rstrip("/")
    m = re.search(r"\{([^{}]*)\}", g)
    if m is None:
        return [g] if g and g != "." else []
    return [x for alt in m.group(1).split(",") for x in ws_patterns(g[:m.start()] + alt + g[m.end():])]


def ws_match(path: str, glob: str) -> bool:
    """Does the directory ``path`` match the workspace glob, one path segment at a time? ``*`` stays inside a
    segment (``packages/*`` is a direct child of packages/, never packages/a/template), ``**`` crosses any
    number of them, and a plain path names that directory only."""
    def seg(parts: tuple[str, ...], pats: tuple[str, ...]) -> bool:
        if not pats:
            return not parts
        if pats[0] == "**":
            return any(seg(parts[i:], pats[1:]) for i in range(len(parts) + 1))
        return bool(parts) and fnmatch.fnmatchcase(parts[0], pats[0]) and seg(parts[1:], pats[1:])

    parts = PurePosixPath(path).parts
    return any(seg(parts, PurePosixPath(p).parts) for p in ws_patterns(glob))


def _workspace_packages(root: Path, files: list[str]) -> tuple[list[dict], list[str], list[str]]:
    """Dependencies of the workspace packages a JavaScript monorepo declares (``workspaces`` in the root
    package.json, ``packages`` in pnpm-workspace.yaml), the package.json files read, and the declared globs
    that match no package.json of the project's files. The globs are matched as the package managers match
    them (:func:`ws_match`); a declared package is read whatever its folder is called (``packages/build``,
    ``apps/demo``): only node_modules and the like are never one."""
    root = Path(root)
    globs: list[str] = []
    try:
        data = json.loads(_read(root / "package.json") or "{}") if "package.json" in files else {}
    except ValueError:
        data = {}
    ws = data.get("workspaces") if isinstance(data, dict) else None
    if isinstance(ws, dict):
        ws = ws.get("packages")
    globs += [w for w in ws or [] if isinstance(w, str)]
    if "pnpm-workspace.yaml" in files:  # packages:\n  - "apps/*"
        block = False
        for ln in (_read(root / "pnpm-workspace.yaml") or "").split("\n"):
            s = ln.split("#")[0].rstrip()
            if re.match(r"^packages\s*:", s):
                block = True
                inline = re.search(r"\[(.*)\]", s)
                if inline:
                    globs += [x.strip().strip("'\"") for x in inline.group(1).split(",") if x.strip()]
                    block = False
                continue
            if block and re.match(r"^\S", s):
                block = False
            m = re.match(r"^\s*-\s*['\"]?([^'\"]+?)['\"]?\s*$", s) if block else None
            if m:
                globs.append(m.group(1))
    globs = [g.strip() for g in globs if g.strip()]
    pos = [g for g in globs if not g.startswith("!")]
    neg = [g[1:] for g in globs if g.startswith("!")]
    items: list[dict] = []
    read: list[str] = []
    unmatched = dict.fromkeys(pos)
    for rel in sorted(f for f in files if PurePosixPath(f).name == "package.json" and f != "package.json"):
        d = PurePosixPath(rel).parent.as_posix()
        hit = [g for g in pos if ws_match(d, g)]
        if set(PurePosixPath(d).parts) & _WS_NEVER or not hit:
            continue
        for g in hit:
            unmatched.pop(g, None)
        if any(ws_match(d, g) for g in neg):
            continue
        raw = _read(root / rel) or ""
        try:
            pkg = json.loads(raw)
        except ValueError:
            continue
        read.append(rel)
        lines = raw.split("\n")
        for sect, scope in (("dependencies", "runtime"), ("peerDependencies", "peer"),
                            ("devDependencies", "dev"), ("optionalDependencies", "optional")):
            deps = pkg.get(sect) if isinstance(pkg, dict) else None
            for name, spec in (deps or {}).items() if isinstance(deps, dict) else ():
                line = next((i for i, ln in enumerate(lines, 1) if f'"{name}"' in ln), 1)
                items.append({"name": str(name).lower(), "spec": str(spec), "scope": scope, "at": f"{rel}:{line}",
                              "path": rel, "line": line, "ecosystem": "npm", "build": "project"})
    return items, read, list(unmatched)


def _optional_lines(root: Path, items: list[dict]) -> None:
    """Cite each ``[project.optional-dependencies]`` item at the line of its own group (``research``
    cites the first line that names the package)."""
    opt = [it for it in items if it.get("path") == "pyproject.toml" and str(it.get("scope")).startswith("optional:")]
    if not opt:
        return
    lines = (_read(Path(root) / "pyproject.toml") or "").split("\n")
    sec = next((i for i, ln in enumerate(lines) if re.match(r"\s*\[project\.optional-dependencies\]", ln)), None)
    if sec is None:
        return
    for it in opt:
        grp = it["scope"].split(":", 1)[1]
        start = next((i for i in range(sec + 1, len(lines)) if re.match(
            rf"\s*['\"]?{re.escape(grp)}['\"]?\s*=\s*\[", lines[i]) or lines[i].lstrip().startswith("[")), None)
        if start is None or lines[start].lstrip().startswith("["):
            continue
        for i in range(start, len(lines)):
            seg = (lines[i].split("=", 1)[1] if i == start else lines[i]).split("#")[0]
            names = re.findall(r"['\"]\s*([A-Za-z0-9][A-Za-z0-9._-]*)", seg)
            if any(_dep_key(n) == _dep_key(it["name"]) for n in names):
                it["line"], it["at"] = i + 1, f"pyproject.toml:{i + 1}"
                break
            if "]" in re.sub(r"(['\"]).*?\1", "", seg):  # the group's list ends (brackets in strings are extras)
                break


_MANIFEST_NAMES = ("package.json", "pyproject.toml", "go.mod", "Cargo.toml", "requirements.txt", *GRADLE_FILES,
                   "pom.xml")


def declared_dependencies(root: Path, all_files: list[str] | None = None) -> dict:
    """``research.dependencies`` (Python, npm, Go, Cargo manifests at the root), the package.json files of
    the workspace packages the root declares, plus Gradle and Maven build files (the root build and what it
    includes, through version catalogs too; others are marked ``build: other``). ``manifests`` lists every
    file read, whether or not it declares anything; ``unread`` the other manifests of the project."""
    from verinoda import research
    from verinoda.snapshot import list_files

    root = Path(root)
    files = all_files if all_files is not None else list_files(root)
    try:
        base = research.dependencies(root)
    except Exception as exc:  # noqa: BLE001 - an unreadable manifest must not stop a check
        base = {"items": [], "manifests": [], "error": f"{type(exc).__name__}: {exc}"[:200]}
    items = [dict(it) for it in base.get("items") or []]
    _optional_lines(root, items)
    ws, ws_read, ws_unmatched = _workspace_packages(root, files)
    extra, skipped, builds_read = _gradle_maven(root, files)
    manifests = list(dict.fromkeys([*(base.get("manifests") or []), *ws_read, *builds_read]))
    read = set(manifests) | set(skipped)
    roots = _excluded_roots(root)
    unread: list[str] = []
    for f in sorted(files):
        parts = set(PurePosixPath(f).parts[:-1])
        if PurePosixPath(f).name not in _MANIFEST_NAMES or f in read or parts & _WS_NEVER:
            continue  # installed packages (node_modules) are never the project's own manifests
        if parts & _NOT_PROJECT_BUILD or is_test_file(f) or any(f.startswith(r + "/") for r in roots):
            skipped.append(f)  # a sample's, a fixture's or a vendored copy's: left out, and the limits say so
        else:
            unread.append(f)
    return {"items": items + ws + extra, "manifests": manifests, "skipped": skipped, "unread": unread,
            **({"workspaces_unmatched": ws_unmatched} if ws_unmatched else {}),
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
    if deps.get("skipped"):
        sk = deps["skipped"]
        scan.limit(f"{len(sk)} build file(s) or manifest(s) under test, sample, fixture, vendor or output folders "
                   f"(or reference trees) are not read: {', '.join(sk[:5])}{' ...' if len(sk) > 5 else ''}")
    if deps.get("workspaces_unmatched"):  # a declared workspace whose packages are not among the project's files
        wu = deps["workspaces_unmatched"]
        scan.limit(f"{len(wu)} declared workspace glob(s) match no package.json among the project's files (git-"
                   f"ignored or build-output folders are not listed), so nothing there was read: {', '.join(wu[:5])}"
                   f"{' ...' if len(wu) > 5 else ''}")
    if deps.get("unread"):
        un = deps["unread"]
        scan.limit(f"{len(un)} other manifest(s) are not read (not the root's, a workspace package or an included "
                   f"build): {', '.join(un[:5])}{' ...' if len(un) > 5 else ''}")
    out = []
    if g.get("absent"):
        what = f"absent {g['absent']}"
        if not deps.get("manifests"):
            scan.unknown.append("no manifest or build file was read, so nothing shows the dependency absent")
            return out, scan, what
        sites: dict[tuple[str, int], list[dict]] = {}
        for it in find_dependency(deps, g["absent"]):
            sites.setdefault((it["path"], it["line"]), []).append(it)
        for (path, line), its in sites.items():  # one finding per cited line
            it = its[0]
            scopes = ", ".join(dict.fromkeys(str(x.get("scope")) for x in its))
            via = f" through the version catalog ({it['via']})" if it.get("via") else ""
            # the cited line must still name it (or its catalog accessor) outside a comment of the build file
            code = build_code(_read(ctx.repo / path) or "", path).split("\n")
            raw_line = code[line - 1] if 0 < line <= len(code) else ""
            shown = _dep_key(raw_line)
            if it.get("build") == "other":
                out.append((POSSIBLE, path, line, f"{it['name']} is declared ({scopes}){via} in {path}, a build file "
                                                  "the root build does not include: whether it is part of this "
                                                  "project is not resolved"))
            elif _dep_key(it.get("short") or it["name"]) in shown or _dep_key(g["absent"]) in shown or \
                    (it.get("ref") and re.search(rf"\b{re.escape(it['ref'])}\b", raw_line, re.I)):
                out.append((VIOLATED, path, line, f"{it['name']} {it.get('spec') or ''} is declared "
                                                  f"({scopes}){via}".replace("  ", " ")))
            else:
                out.append((POSSIBLE, path, line, f"{it['name']} is listed, but the cited line does not name it"))
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
    """Files changed since commit ``sha`` in the working tree, plus untracked ones, as paths relative to
    ``repo`` (the project may be a subdirectory of its git repository: ``--relative``), read NUL-separated
    (``-z``: a path with non-ASCII characters is not quoted)."""
    from verinoda.snapshot import git

    diff = git(Path(repo), "diff", "--name-only", "--relative", "--no-renames", "-z", sha, "--")
    untracked = git(Path(repo), "ls-files", "-z", "--others", "--exclude-standard")
    if diff is None or untracked is None:
        raise ValueError(f"git could not list the files changed since {sha[:12]} in {repo}")
    return {p for p in (diff + "\0" + untracked).split("\0") if p}


# -- the check ------------------------------------------------------------------------------------

def _guard_desc(g: dict) -> str:
    return g.get("spec") or g.get("kind", "?")


def _rel_or_abs(p: Path, root: Path) -> str:
    try:
        return Path(p).resolve().relative_to(Path(root).resolve()).as_posix()
    except ValueError:
        return Path(p).as_posix()


def stale_graph_note(update_result: dict) -> str:
    """Why the graph a gate is about to read may not describe the working tree, from a failed refresh
    (:func:`verinoda.workflow.update`): another build still running after the wait, or an error."""
    if update_result.get("mode") == "busy":
        return "the index was not refreshed: another index build was still running after the wait"
    return f"the index could not be refreshed ({str(update_result.get('error') or 'unknown error')[:160]})"


def check(repo: Path, *, graph=None, base: str | None = None, changed_only: bool = False,
          records=None, decisions_dir: str | None = None, graph_stale: str | None = None) -> dict:
    """Run every accepted guard of every enforced decision on the working tree.

    ``base`` (a git revision) or ``changed_only`` (= base HEAD) labels each finding ``new/touched since
    <base>`` when its file, or a file its binding passes through (a re-export module, an edge's target),
    changed since then, else ``pre-existing: ... unchanged since <base>`` (what was compared: the base tree
    itself is not checked); then only new/touched violations make ``exit`` 1. Without them any violation
    does. ``exit`` is 3 when nothing is violated but something was not checked (``status`` unknown: a guard
    that checked no file, edge or manifest, a record that cannot be read, or no record found while the
    repository holds ADR-like files); ``ok`` never stands for a check that looked at nothing.
    ``decisions_dir``: the records' folder (``--decisions-dir``) instead of the configured one.

    ``graph_stale``: why ``graph`` may be older than the working tree (its refresh failed, or another build
    was still running): every no_edge guard is then ``unknown`` (a violation it still finds stands, as its
    line is re-read), and without a violation ``exit`` is 2, as for an error: a gate never passes on edges
    it could not read.
    """
    from verinoda import decisions as dm
    from verinoda.snapshot import list_files

    t0 = time.monotonic()
    repo = Path(repo).resolve()
    ddir, ddir_from = dm.decisions_dir_source(repo, decisions_dir)
    recs = records if records is not None else dm.load_all(repo, decisions_dir)
    res: dict = {"violations": [], "possible": [], "reviews": [], "triggers": [], "waived": [], "ok": [],
                 "unknown": [], "not_enforced": [], "pre_existing": [], "decisions": len(recs),
                 "decisions_dir": {"path": _rel_or_abs(ddir, repo), "from": ddir_from}}
    base_sha = base_label = None
    changed: set[str] | None = None
    if base or changed_only:
        base_label = base or "HEAD"
        base_sha = validate_ref(repo, base_label)
        changed = changed_since(repo, base_sha)
        res["base"] = {"ref": base_label, "commit": base_sha, "changed_files": len(changed)}
    ctx = _Ctx(repo, list_files(repo), graph, ddir, graph_stale)
    if graph_stale:
        res["graph_stale"] = graph_stale
    today = dm._today()
    if not recs and ddir_from != dm.DEFAULT_SOURCE and not ddir.is_dir():
        # a folder the user named (flag, config, verinoda.toml, pyproject) that is not there: a typo or a folder
        # not created yet, never "0 records, nothing to check"
        res["unknown"].append({"decision": None, "guard": None, "kind": "records",
                               "why": f"the decisions folder {_rel_or_abs(ddir, repo)} ({ddir_from}) does not exist, "
                                      "so no decision record was read: fix the setting, or create the folder "
                                      "(`verinoda decide record` writes it)"})
    if not recs:
        adrs = dm.adr_like_files(repo, ctx.all_files, skip=ddir)
        if adrs:
            res["unknown"].append({"decision": None, "guard": None, "kind": "records",
                                   "why": f"no decision record in {_rel_or_abs(ddir, repo)} ({ddir_from}), but "
                                          f"{len(adrs)} ADR-like file(s) exist: {', '.join(adrs[:4])}"
                                          f"{' ...' if len(adrs) > 4 else ''}. Nothing was checked: set the folder "
                                          "in a committed file (verinoda.toml `[decisions] dir = \"docs/decisions\"`"
                                          ", or `[tool.verinoda.decisions]` in pyproject.toml) or pass "
                                          "--decisions-dir; a hand-written ADR needs `verinoda decide import`",
                                   "adr_like": adrs[:20]})
    for d in recs:
        if not d.enforced:
            res["not_enforced"].append({"decision": d.id, "status": d.status,
                                        "why": "; ".join([*d.problems, *d.inactive]) or f"status {d.status}",
                                        **({"problem": True} if d.problems else {})})
            continue
        for w in d.warnings:  # a waiver that is not applied: its sites are checked like any other
            res["not_enforced"].append({"decision": d.id, "status": "waiver", "why": w})
        for g in d.guards:
            if g.get("status") != "accepted":
                res["not_enforced"].append({"decision": d.id, "guard": g["id"], "status": g.get("status"),
                                            "why": "proposed guard: inactive until the user accepts it"})
                continue
            fn = {"only_in": check_only_in, "no_edge": check_no_edge, "dependency": check_dependency}.get(
                g.get("kind"))
            try:
                if fn is None:
                    raise ValueError(f"guard kind {g.get('kind')!r} is not only_in, no_edge or dependency")
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
                    via = scan.via.get((rel, line)) or set()
                    touched = sorted(p for p in {rel, *via} if p in changed)
                    if touched:
                        f.since = f"new/touched since {base_label}" + \
                            (f" (through {', '.join(touched)})" if rel not in touched else "")
                    else:
                        f.since = f"pre-existing: {rel}" + \
                            (f" and the {len(via)} file(s) its binding passes through" if via else "") + \
                            f" unchanged since {base_label}"
                item = {**f.as_dict(), "what": what}
                w = dm.waived(d, g["id"], rel, line or None, today)
                if w is not None:
                    res["waived"].append({**item, "waiver": w})
                    continue
                counted = True
                if level == VIOLATED and f.since and f.since.startswith("pre-existing"):
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
            try:
                level, why = check_governs(repo, v)
            except Exception as exc:  # noqa: BLE001 - one broken entry must not hide the others
                res["unknown"].append({"decision": d.id, "guard": v.get("id"), "kind": "governs",
                                       "why": f"the check failed: {type(exc).__name__}: {exc}"[:300]})
                continue
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
            try:
                where = revisit_holds(repo, r, all_files=ctx.all_files, deps=ctx.deps() if r["kind"] ==
                                      "dependency_added" else None)
            except Exception as exc:  # noqa: BLE001
                res["unknown"].append({"decision": d.id, "guard": r.get("id"), "kind": "revisit_when",
                                       "why": f"the check failed: {type(exc).__name__}: {exc}"[:300]})
                continue
            # what the condition was checked against: the manifests read, or the project's files
            scope = {"manifests": len(ctx.deps().get("manifests") or [])} if r["kind"] == "dependency_added" \
                else {"files": len(ctx.all_files)}
            if where and not r.get("baseline"):
                res["triggers"].append({"decision": d.id, "guard": r["id"], "kind": "revisit_when", "level": TRIGGER,
                                        "at": where[0], "why": f"{r['kind']}={r['value']} holds now ("
                                        f"{', '.join(where[:3])}): the user should review {d.id}"})
            elif not where and not sum(scope.values()):
                res["unknown"].append({"decision": d.id, "guard": r["id"], "kind": "revisit_when",
                                       "why": f"{r['kind']}={r['value']}: no "
                                              f"{'manifest was' if 'manifests' in scope else 'file was'} read, so "
                                              "nothing shows that it does not hold"})
            else:
                note = "held already when the decision was recorded" if where else "does not hold"
                res["ok"].append({"decision": d.id, "guard": r["id"], "kind": "revisit_when",
                                  "what": f"{r['kind']}={r['value']}", "scope": scope, "limits": [note]})
    res["elapsed_s"] = round(time.monotonic() - t0, 3)
    broken = [n for n in res["not_enforced"] if n.get("problem")]
    stale_unchecked = bool(graph_stale) and any(u["kind"] == "no_edge" for u in res["unknown"])
    # 1: something is violated; 2: edges could not be read (a stale graph); 3: nothing violated, but something was
    # not checked (unknown); 0 otherwise
    res["exit"] = 1 if res["violations"] else 2 if stale_unchecked else 3 if res["unknown"] or broken else 0
    # "ok" only when every guard was checked and nothing was found: a guard or file that could not be checked,
    # or a record that cannot be read, is "unknown"; violations in files unchanged since the base are
    # "pre_existing" (not "ok")
    res["status"] = ("violated" if res["violations"] else "possible" if res["possible"] else
                     "review" if res["reviews"] or res["triggers"] else "unknown" if res["unknown"] or broken else
                     "pre_existing" if res["pre_existing"] else "ok")
    if res["exit"] == 3:
        res["exit_because"] = ", ".join(x for x in (f"{len(res['unknown'])} check(s) not completed (unknown)"
                                                    if res["unknown"] else "",
                                                    f"{len(broken)} record(s) cannot be read" if broken else "") if x)
    steps = []
    if res["violations"]:
        ids = sorted({v["decision"] for v in res["violations"]})
        steps.append(f"change the code, or ask the user whether {', '.join(ids)} should be superseded or these "
                     "sites waived (`verinoda decide waive`); never edit or supersede a decision on your own")
    elif res["possible"] or res["reviews"] or res["triggers"]:
        steps.append("read the POSSIBLE sites; ask the user about REVIEW / TRIGGER items")
    if res["unknown"]:
        steps.append(f"{len(res['unknown'])} check(s) could not be completed (see unknown): nothing says the "
                     "code there keeps the decision")
    if broken:
        steps.append(f"{len(broken)} decision record(s) cannot be read and are not checked (see not_enforced): "
                     "ask the user to fix their front matter; never edit a decision on your own")
    if res["pre_existing"] and not res["violations"]:
        steps.append(f"{len(res['pre_existing'])} violation(s) in files unchanged since {base_label} (see "
                     "pre_existing): not new, but the code still breaks the decision there")
    if steps:
        res["next_step"] = "; ".join(steps)
    return res
