"""The side-effect gate of ``verinoda probe`` (docs/DESIGN.md D36): which functions it will not call.

Before any input runs, the gate reads - never imports - the target function, the
project functions it calls (its static closure, 4 levels, through imports,
re-exports, ``self.method()``, constructors and parameters annotated with a
project class), the constructors the input recipes will run, and the
module-level statements of every project module those live in and import (they
run when the target is imported). It refuses the probe when any of them:

* writes files (``open(..., "w"/"a"/"x"/"+")``, ``os.remove``, ``shutil.rmtree``,
  ``tempfile``, ``Path.write_text`` ...), opens network connections (``socket``,
  ``requests``, ``urllib.request``, ``http.client`` ...), starts processes or
  threads (``subprocess``, ``os.system``, ``multiprocessing``,
  ``threading.Thread`` ...) or connects to a database (``sqlite3.connect`` ...);
* passes an SQL write statement (``INSERT INTO`` ..., a literal, a name
  assigned one, a parameter default, an instance attribute, a project class's or
  module's constant, a loop variable over statements, or the return value of a
  project function that returns one) to a call that does not only build or log
  text (``cur.execute("INSERT ...")``, ``cur.execute(_sql())``; a statement that
  is only returned or assigned runs nothing); read queries are allowed;
* matches another sink line of :data:`verinoda.architecture_map.SINK_PATTERNS`
  (``.commit()``, ``.save()``, ``json.dump``): a text pattern, marked
  ``heuristic``; ``.commit()`` / ``.save()`` on ``self``, a parameter annotated
  with a project class or a local built by a project constructor is read as that
  class's method instead;
* changes state the probe cannot isolate between calls: a ``global`` name it
  assigns, a module-level container it mutates (``CACHE[k] = v``,
  ``_seen.append(x)``), an attribute of an imported module or a class
  (``config.X = 1``, ``Cls.count += 1``, ``setattr(Cls, "n", x)``), a
  class-level container changed through ``self`` (``items: list = []``, then
  ``self.items.append(x)``; dataclass fields are the instance's), the
  environment, ``sys.path``, the working directory, the global random seed,
  signal or exit handlers.

A constructor call reads ``__init__``, ``__post_init__``, ``__new__`` and the
``__del__`` that runs when the object is released.

Each reason names the line and the call chain that reaches it (``place_order ->
OrderRepository.save``). The gate is static and therefore heuristic: calls
through objects it cannot type (``self.conn.execute``), ``getattr`` with a
computed name, ``exec`` and code in installed packages are not followed (they
are counted in ``unresolved_calls``). The probe run itself carries an audit hook
that blocks file writes, network, processes and environment changes while the
function runs (:mod:`verinoda.runtime.probe_plugin`), so what the gate misses is
still stopped and reported. ``--allow-side-effects`` is the user's decision to
run anyway; the reasons are then kept in the result.
"""

from __future__ import annotations

import ast
from pathlib import Path

from verinoda import guards
from verinoda.architecture_map import SINK_PATTERNS

MAX_DEPTH = 4
MAX_FUNCTIONS = 300
MAX_MODULES = 200

EXACT_SINKS: dict[str, str] = {
    **{f"os.{n}": "file-write" for n in ("remove", "unlink", "rmdir", "removedirs", "rename", "renames", "replace",
                                         "mkdir", "makedirs", "chmod", "chown", "lchown", "link", "symlink",
                                         "truncate", "ftruncate", "utime", "write", "mkfifo", "mknod")},
    **{f"shutil.{n}": "file-write" for n in ("rmtree", "move", "copy", "copy2", "copyfile", "copytree", "copymode",
                                             "copystat", "make_archive", "unpack_archive", "chown")},
    **{f"tempfile.{n}": "file-write" for n in ("mkstemp", "mkdtemp", "NamedTemporaryFile", "TemporaryFile",
                                               "TemporaryDirectory", "SpooledTemporaryFile")},
    "shelve.open": "file-write", "dbm.open": "file-write",
    **{f"os.{n}": "process" for n in ("system", "popen", "fork", "forkpty", "kill", "killpg", "startfile",
                                      "posix_spawn", "posix_spawnp")},
    "pty.spawn": "process", "webbrowser.open": "process", "webbrowser.open_new": "process",
    "webbrowser.open_new_tab": "process",
    "threading.Thread": "thread", "threading.Timer": "thread", "_thread.start_new_thread": "thread",
    "concurrent.futures.ThreadPoolExecutor": "thread",
    **{f"os.{n}": "global-state" for n in ("putenv", "unsetenv", "chdir", "fchdir", "chroot", "setuid", "setgid",
                                           "umask")},
    **{f"sys.{n}": "global-state" for n in ("setrecursionlimit", "settrace", "setprofile", "setswitchinterval",
                                            "set_int_max_str_digits")},
    "random.seed": "global-state", "signal.signal": "global-state", "signal.alarm": "global-state",
    "atexit.register": "global-state", "logging.basicConfig": "global-state", "logging.disable": "global-state",
    "warnings.simplefilter": "global-state", "warnings.filterwarnings": "global-state",
    "locale.setlocale": "global-state", "gc.disable": "global-state", "importlib.reload": "global-state",
    "faulthandler.enable": "global-state",
}
PREFIX_SINKS: tuple[tuple[str, str], ...] = (
    ("subprocess.", "process"), ("multiprocessing.", "process"), ("os.exec", "process"), ("os.spawn", "process"),
    ("concurrent.futures.ProcessPoolExecutor", "process"), ("asyncio.create_subprocess", "process"),
    ("socket.", "network"), ("requests.", "network"), ("urllib.request.", "network"), ("urllib3.", "network"),
    ("http.client.", "network"), ("httpx.", "network"), ("aiohttp.", "network"), ("smtplib.", "network"),
    ("ftplib.", "network"), ("poplib.", "network"), ("imaplib.", "network"), ("telnetlib.", "network"),
    ("xmlrpc.client.", "network"), ("paramiko.", "network"), ("boto3.", "network"), ("botocore.", "network"),
    ("grpc.", "network"), ("websocket.", "network"), ("websockets.", "network"),
    ("redis.", "kv/object-store"),
    *((name, "db-connection") for name in guards.SINK_CALLS["db-connection"] if not name.startswith("java.")),
    ("pymongo.", "db-connection"), ("psycopg2.", "db-connection"), ("psycopg.", "db-connection"),
    ("pymysql.", "db-connection"), ("mysql.connector.", "db-connection"), ("asyncpg.", "db-connection"),
    ("aiosqlite.", "db-connection"),
)
STATE_ROOTS = {"os.environ": "the environment", "sys.path": "sys.path", "sys.modules": "sys.modules",
               "sys.stdout": "sys.stdout", "sys.stderr": "sys.stderr"}
PATH_METHODS = {"write_text", "write_bytes", "unlink", "rmdir", "touch", "symlink_to", "hardlink_to", "mkdir"}
MUTATORS = {"append", "extend", "insert", "update", "add", "pop", "popitem", "clear", "setdefault", "remove",
            "discard", "sort", "reverse", "appendleft", "extendleft", "rotate", "__setitem__", "__delitem__"}
OPEN_CALLS = {"open", "io.open", "codecs.open", "builtins.open"}
# text patterns checked line by line; SQL writes are checked on the syntax tree (Gate._sql_writes), reads allowed
LINE_SINKS = [(rx, kind) for rx, kind in SINK_PATTERNS if kind not in ("sql-read", "sql-write")]
SQL_WRITE_RX = next(rx for rx, kind in SINK_PATTERNS if kind == "sql-write")
# calls that only build, inspect or log text: an SQL statement passed to them runs nothing
TEXT_ONLY_CALLS = frozenset({
    "format", "format_map", "join", "replace", "strip", "lstrip", "rstrip", "lower", "upper", "casefold", "title",
    "capitalize", "startswith", "endswith", "split", "rsplit", "splitlines", "partition", "rpartition", "encode",
    "decode", "count", "find", "rfind", "index", "rindex", "ljust", "rjust", "center", "zfill", "translate",
    "removeprefix", "removesuffix", "len", "str", "repr", "bool", "isinstance", "print", "debug", "info", "warning",
    "warn", "error", "exception", "critical", "log", "match", "search", "fullmatch", "sub", "subn", "compile",
    "findall", "finditer", "escape", "dedent", "indent"})
LIMITS = [
    "the gate reads code, it does not run it: calls through objects it cannot type (self.conn.execute), getattr "
    "with a computed name, exec/eval and code inside installed packages are not followed; the probe's audit hook "
    "blocks file writes, network, processes and environment changes at run time",
    "state a function keeps in its own instance (self.x = ...) is not a refusal: each call gets a fresh instance",
    "text patterns (`.commit()`, `.save()`, `json.dump(` ...) are heuristic: a reason marked heuristic may be a "
    "method of an object that touches nothing",
]


def sink_of(qual: str) -> str | None:
    if qual in EXACT_SINKS:
        return EXACT_SINKS[qual]
    for prefix, kind in PREFIX_SINKS:
        if qual == prefix.rstrip(".") or qual.startswith(prefix):
            return kind
    return None


def _open_mode(call: ast.Call) -> tuple[bool, str | None]:
    """(writes?, why) for an ``open``-like call: a literal mode with w/a/x/+, or a mode that is not a literal."""
    mode = call.args[1] if len(call.args) > 1 else next((k.value for k in call.keywords if k.arg == "mode"), None)
    if mode is None:
        return False, None
    if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
        return (any(c in mode.value for c in "wax+"), f"mode {mode.value!r}")
    return True, "a mode that is not a literal"


def _find_def(tree: ast.AST, qual: str) -> ast.AST | None:
    node: ast.AST | None = tree
    for part in qual.split("."):
        found = None
        for child in getattr(node, "body", []):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and child.name == part:
                found = child
        if found is None:
            for child in ast.walk(node) if node is tree else []:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and child.name == part \
                        and child.col_offset == 0:
                    found = child
        node = found
        if node is None:
            return None
    return node


def _main_guard(node: ast.AST) -> bool:
    return isinstance(node, ast.If) and "__name__" in ast.unparse(node.test) and "__main__" in ast.unparse(node.test)


def _scope_walk(root: ast.AST):
    """``ast.walk`` of ``root`` without the bodies of nested functions, classes and lambdas."""
    yield root
    stack = list(ast.iter_child_nodes(root))
    while stack:
        n = stack.pop()
        yield n
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            stack.extend(ast.iter_child_nodes(n))


def _sql_literal_line(expr: ast.AST) -> int | None:
    """The line of a string literal in ``expr`` that holds an SQL write statement, else None."""
    for n in ast.walk(expr):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and SQL_WRITE_RX.search(n.value):
            return n.lineno
    return None


def _call_name(f: ast.AST) -> str:
    return f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else "")


class Gate:
    """One gate run over a repository (a :class:`guards._PyIndex` of its Python files)."""

    def __init__(self, repo: Path, files: list[str]):
        self.repo = Path(repo)
        self.ix = guards._PyIndex(self.repo, [f for f in files if f.endswith(".py")])
        self.reasons: list[dict] = []
        self.seen_fn: set[tuple[str, str]] = set()
        self.seen_mod: set[str] = set()
        self.unresolved: list[str] = []
        self.checked: list[str] = []
        self._uses: dict[str, dict[int, object]] = {}
        self._sqlc: dict[str, dict[str, int]] = {}
        self._sqlr: dict[str, tuple[str, int] | None] = {}

    # -- helpers ---------------------------------------------------------------------------
    def _uses_map(self, rel: str) -> dict[int, object]:
        if rel not in self._uses:
            sc = self.ix.scopes(rel)
            self._uses[rel] = {id(c): s for c, s in (sc.uses if sc else [])}
        return self._uses[rel]

    def _locate(self, full: str) -> tuple[str, str] | None:
        """(file, qualified name inside it) of a project definition named by a dotted name."""
        parts = full.split(".")
        for i in range(len(parts) - 1, 0, -1):
            rel = self.ix.modules.get(".".join(parts[:i]))
            if rel is not None:
                return rel, ".".join(parts[i:])
        return None

    def _module_qual(self, rel: str, name: str) -> str | None:
        """What a module-level name of ``rel`` holds (a dotted name), following imports and re-exports."""
        sc = self.ix.scopes(rel)
        if sc is None or name not in sc.module.binds:
            return None
        for q, b in sc.exports(name):
            if q:
                return self.ix.resolve(q)[0][0]
            if b.kind in ("def", "class"):
                mod = guards._module_of(rel)
                return f"{mod}.{name}" if mod else name
        return None

    def _line(self, rel: str, line: int) -> str:
        _, text = self.ix.tree(rel)
        lines = (text or "").splitlines()
        return lines[line - 1].strip()[:160] if 0 < line <= len(lines) else ""

    def _add(self, kind: str, rel: str, line: int, why: str, via: list[str], phase: str,
             heuristic: bool = False) -> None:
        at = f"{rel}:{line}"
        rec = {"kind": kind, "at": at, "line": self._line(rel, line), "why": why, "via": list(via), "phase": phase,
               **({"heuristic": True} if heuristic else {})}
        for k, r in enumerate(self.reasons):
            if r["at"] == at and r["kind"] == kind:
                if r.get("heuristic") and not heuristic:  # a resolved call outranks a text pattern on its line
                    self.reasons[k] = rec
                return
        self.reasons.append(rec)

    # -- walking -------------------------------------------------------------------------------
    def run(self, roots: list[tuple[str, str]]) -> dict:
        queue: list[tuple[str, str, int, list[str]]] = [(rel, qual, 0, [qual]) for rel, qual in roots]
        for rel, _ in roots:
            self._module(rel)
        while queue and len(self.seen_fn) < MAX_FUNCTIONS:
            rel, qual, depth, via = queue.pop(0)
            if (rel, qual) in self.seen_fn:
                continue
            self.seen_fn.add((rel, qual))
            tree, _ = self.ix.tree(rel)
            node = _find_def(tree, qual) if tree is not None else None
            if node is None:
                continue
            if isinstance(node, ast.ClassDef):  # a constructor call: __init__ / __post_init__ / __new__, and the
                for m in ("__init__", "__post_init__", "__new__", "__del__"):  # finalizer when it is released
                    if _find_def(tree, f"{qual}.{m}") is not None:
                        queue.append((rel, f"{qual}.{m}", depth, via))
                continue
            self.checked.append(f"{rel}::{qual}")
            self._module(rel)
            for nxt in self._visit(rel, qual, node, via, "call"):
                if depth + 1 <= MAX_DEPTH:
                    queue.append((*nxt, depth + 1, [*via, nxt[1]]))
        return {"verdict": "refused" if self.reasons else "passed", "reasons": self.reasons,
                "functions_checked": len(self.checked), "modules_checked": sorted(self.seen_mod),
                "unresolved_calls": len(self.unresolved), "unresolved_examples": self.unresolved[:5],
                "limits": list(LIMITS)}

    def _module(self, rel: str) -> None:
        """Module-level statements of ``rel`` and of the project modules it imports (they run at import)."""
        if rel in self.seen_mod or len(self.seen_mod) >= MAX_MODULES:
            return
        self.seen_mod.add(rel)
        tree, _ = self.ix.tree(rel)
        sc = self.ix.scopes(rel)
        if tree is None or sc is None:
            return
        skip: set[int] = set()
        for st in tree.body:
            if _main_guard(st):
                skip.update(id(n) for n in ast.walk(st))
        calls = {(getattr(n, "lineno", 0), getattr(n, "col_offset", 0)): n for n in ast.walk(tree)
                 if isinstance(n, ast.Call)}
        mod_calls = [calls[pos] for pos in sc.module_calls if pos in calls and id(calls[pos]) not in skip]
        uses = self._uses_map(rel)
        for call in mod_calls:
            for nxt in self._call(rel, call, uses.get(id(call)), [f"import of {rel}"], "import", None):
                fr, fq = nxt
                self.run_extra(fr, fq, [f"import of {rel}", fq])
        # text sinks on module-level statement lines (not inside defs / classes' methods)
        for st in tree.body:
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Import, ast.ImportFrom)) \
                    or _main_guard(st):
                continue
            self._sql_writes(rel, st, [f"import of {rel}"], "import")
            self._text(rel, st.lineno, st.end_lineno or st.lineno, [f"import of {rel}"], "import")
        # project modules imported at module level
        for st in tree.body:
            if isinstance(st, (ast.Import, ast.ImportFrom)):
                for a in st.names:
                    qual = self._import_qual(rel, st, a)
                    loc = self._module_rel(qual) if qual else None
                    if loc:
                        self._module(loc)

    def _import_qual(self, rel: str, st: ast.AST, alias: ast.alias) -> str | None:
        if isinstance(st, ast.Import):
            return alias.name
        sc = self.ix.scopes(rel)
        base = sc._from_base(st) if sc is not None else (st.module or "")
        return f"{base}.{alias.name}" if base else alias.name

    def _module_rel(self, qual: str) -> str | None:
        parts = qual.split(".")
        for i in range(len(parts), 0, -1):
            rel = self.ix.modules.get(".".join(parts[:i]))
            if rel is not None:
                return rel
        return None

    def run_extra(self, rel: str, qual: str, via: list[str]) -> None:
        if (rel, qual) in self.seen_fn:
            return
        tree, _ = self.ix.tree(rel)
        node = _find_def(tree, qual) if tree is not None else None
        if node is None or isinstance(node, ast.ClassDef):
            return
        self.seen_fn.add((rel, qual))
        self.checked.append(f"{rel}::{qual}")
        for nxt in self._visit(rel, qual, node, via, "import"):
            self.run_extra(nxt[0], nxt[1], [*via, nxt[1]])

    def _text(self, rel: str, a: int, b: int, via: list[str], phase: str,
              typed: dict[int, int] | None = None) -> None:
        """Sink text patterns on lines ``a``..``b`` (strings kept, comments and docstrings blanked). SQL write
        statements are checked on the syntax tree instead (:meth:`_sql_writes`); ``typed`` counts, per line, the
        ``.commit()`` / ``.save()`` calls on an object of a project class that defines that method (the gate reads
        that method instead)."""
        _, text = self.ix.tree(rel)
        if text is None:
            return
        code = guards.code_text(text, ".py", keep_strings=True).splitlines()
        for ln in range(a, min(b, len(code)) + 1):
            line = code[ln - 1]
            for rx, kind in LINE_SINKS:
                if kind == "orm-write":
                    if len(rx.findall(line)) > (typed or {}).get(ln, 0):
                        self._add(kind, rel, ln, "the line matches the orm-write text pattern (`.commit()`, "
                                                 "`.save()` ...) on an object the gate cannot type", via, phase,
                                  heuristic=True)
                elif rx.search(line):
                    self._add(kind, rel, ln, f"the line matches the {kind} sink pattern", via, phase,
                              heuristic=True)

    # -- SQL write statements ------------------------------------------------------------------------
    def _sql_consts(self, rel: str) -> dict[str, int]:
        """Module-level and class-level names of ``rel`` assigned an SQL write statement: name -> line."""
        if rel in self._sqlc:
            return self._sqlc[rel]
        out: dict[str, int] = {}
        self._sqlc[rel] = out
        tree, _ = self.ix.tree(rel)
        body = list(getattr(tree, "body", [])) if tree is not None else []
        for st in list(body):
            if isinstance(st, ast.ClassDef):
                body += st.body
        for st in body:
            if isinstance(st, (ast.Assign, ast.AnnAssign)) and st.value is not None:
                ln = _sql_literal_line(st.value)
                if ln:
                    for t in (st.targets if isinstance(st, ast.Assign) else [st.target]):
                        if isinstance(t, ast.Name):
                            out[t.id] = ln
        return out

    def _sql_attrs(self, rel: str) -> dict[str, int]:
        """Instance attributes of ``rel`` assigned an SQL write statement in a method (``self.insert = "INSERT
        ..."`` in ``__init__``): attribute -> line."""
        key = f"{rel}\0attrs"
        if key in self._sqlc:
            return self._sqlc[key]
        out: dict[str, int] = {}
        self._sqlc[key] = out
        tree, _ = self.ix.tree(rel)
        for n in ast.walk(tree) if tree is not None else []:
            if isinstance(n, (ast.Assign, ast.AnnAssign)) and n.value is not None:
                ln = _sql_literal_line(n.value)
                if ln:
                    for t in (n.targets if isinstance(n, ast.Assign) else [n.target]):
                        if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and \
                                t.value.id in ("self", "cls"):
                            out.setdefault(t.attr, ln)
        return out

    def _attr_sql(self, rel: str, n: ast.Attribute) -> tuple[str, int] | None:
        """``self.X`` / ``cls.X`` (a class constant or an instance attribute of this file), ``Q.X`` (a class of the
        project) or ``module.X`` (a project module) that holds an SQL write statement."""
        if not isinstance(n.value, ast.Name):
            return None
        if n.value.id in ("self", "cls"):
            consts, attrs = self._sql_consts(rel), self._sql_attrs(rel)
            if n.attr in consts:
                return rel, consts[n.attr]
            return (rel, attrs[n.attr]) if n.attr in attrs else None
        full = self._module_qual(rel, n.value.id)
        if not full:
            return None
        where = self.ix.modules.get(full)  # `import orders.q as q; q.INSERT`
        if where is None:
            loc = self._locate(full)  # `from orders.q import Q; Q.INSERT`
            where = loc[0] if loc is not None and "." not in loc[1] else None
        if where is None:
            return None
        consts = self._sql_consts(where)
        return (where, consts[n.attr]) if n.attr in consts else None

    def _returns_sql(self, rel: str, qual: str, depth: int) -> tuple[str, int] | None:
        """Where the SQL write statement is that project function ``qual`` of ``rel`` returns, else None
        (``def _sql(): return "INSERT ..."``; followed 3 calls deep)."""
        key = f"{rel}\0{qual}"
        if key in self._sqlr:
            return self._sqlr[key]
        if depth > 3:
            return None
        self._sqlr[key] = None  # a recursive helper: no answer while it is being read
        tree, _ = self.ix.tree(rel)
        fn = _find_def(tree, qual) if tree is not None else None
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return None
        cls = qual.rpartition(".")[0] or None
        local = self._sql_locals(rel, fn, list(_scope_walk(fn)), cls, depth + 1)
        for n in _scope_walk(fn):
            if isinstance(n, ast.Return) and n.value is not None:
                src = self._sql_source(rel, n.value, local, cls, depth + 1)
                if src:
                    self._sqlr[key] = (src[0], src[1])
                    return self._sqlr[key]
        return None

    def _call_sql(self, rel: str, call: ast.Call, cls: str | None, depth: int) -> tuple[str, int] | None:
        """The SQL write statement a call to a project function returns (``execute(_sql())``)."""
        f = call.func
        target = None
        if isinstance(f, ast.Name):
            full = self._module_qual(rel, f.id)
            target = self._locate(full) if full else None
        elif isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id in ("self", "cls") \
                and cls:
            target = (rel, f"{cls}.{f.attr}")
        return self._returns_sql(*target, depth) if target is not None else None

    def _sql_source(self, rel: str, expr: ast.AST, local: dict[str, tuple[str, int]], cls: str | None = None,
                    depth: int = 0) -> tuple[str, int, str | None] | None:
        """``(file, line, name)`` of an SQL write statement that ``expr`` contains (``name`` None) or names through a
        variable, a constant, an attribute or a project function's return value (``name``), else None."""
        ln = _sql_literal_line(expr)
        if ln:
            return rel, ln, None
        consts = self._sql_consts(rel)
        for n in ast.walk(expr):
            if isinstance(n, ast.Name):
                if n.id in local:
                    return (*local[n.id], n.id)
                if n.id in consts:
                    return rel, consts[n.id], n.id
                full = self._module_qual(rel, n.id)
                loc = self._locate(full) if full else None
                if loc is not None and "." not in loc[1] and loc[1] in self._sql_consts(loc[0]):
                    return loc[0], self._sql_consts(loc[0])[loc[1]], n.id
            elif isinstance(n, ast.Attribute):
                src = self._attr_sql(rel, n)
                if src:
                    return (*src, ast.unparse(n)[:60])
            elif isinstance(n, ast.Call) and depth <= 3:
                src = self._call_sql(rel, n, cls, depth)
                if src:
                    return (*src, f"{ast.unparse(n.func)[:50]}()")
        return None

    def _sql_locals(self, rel: str, root: ast.AST, nodes: list, cls: str | None, depth: int = 0
                    ) -> dict[str, tuple[str, int]]:
        """Local names bound to an SQL write statement: assignments, parameter defaults (``sql="INSERT ..."``, used
        when the caller leaves it out) and loop variables over a collection of statements."""
        local: dict[str, tuple[str, int]] = {}
        args = getattr(root, "args", None)
        if args is not None:
            pos = [*args.posonlyargs, *args.args]
            pairs = list(zip(pos[len(pos) - len(args.defaults):], args.defaults))
            pairs += [(p, d) for p, d in zip(args.kwonlyargs, args.kw_defaults) if d is not None]
            for p, d in pairs:
                src = self._sql_source(rel, d, local, cls, depth)
                if src:
                    local[p.arg] = (src[0], src[1])
        for n in nodes:
            value, targets = None, []
            if isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign)) and n.value is not None:
                value, targets = n.value, (n.targets if isinstance(n, ast.Assign) else [n.target])
            elif isinstance(n, (ast.For, ast.AsyncFor, ast.comprehension)):
                value, targets = n.iter, [n.target]
            if value is None:
                continue
            src = self._sql_source(rel, value, local, cls, depth)
            if src:
                for t in targets:
                    for x in ast.walk(t):
                        if isinstance(x, ast.Name):
                            local[x.id] = (src[0], src[1])
        return local

    def _sql_writes(self, rel: str, root: ast.AST, via: list[str], phase: str, cls: str | None = None) -> None:
        """An SQL write statement (``INSERT INTO`` ...) is a sink where it reaches a call's arguments -
        ``cur.execute("INSERT ...")``, ``db.run(SQL)`` with ``SQL`` assigned one, a parameter default, an instance
        attribute, another class's constant, a loop variable over statements or a project function's return value
        - and not a call that only builds or logs text (``.format``, ``.join``, ``log.info``, an exception). A
        statement that is only returned, assigned or compared runs nothing."""
        # a function with its nested functions (as the call walk); a module-level statement without the bodies of
        # the functions it defines (they do not run at import)
        nodes = list(ast.walk(root) if isinstance(root, (ast.FunctionDef, ast.AsyncFunctionDef)) else
                     _scope_walk(root))
        local = self._sql_locals(rel, root, nodes, cls)
        for n in nodes:
            if not isinstance(n, ast.Call):
                continue
            name = _call_name(n.func)
            if name in TEXT_ONLY_CALLS or name.endswith(("Error", "Exception", "Warning")):
                continue
            for arg in [*n.args, *(k.value for k in n.keywords)]:
                src = self._sql_source(rel, arg, local, cls)
                if not src:
                    continue
                if src[2] is None:  # the statement is written in the call: its line
                    self._add("sql-write", rel, src[1], f"an SQL write statement reaches `{name or 'a call'}()`",
                              via, phase)
                else:  # passed by name: the call's line, and where the statement is
                    self._add("sql-write", rel, n.lineno, f"`{name or 'a call'}()` gets the SQL write statement "
                                                          f"`{src[2]}` (set at {src[0]}:{src[1]})", via, phase)
                break

    # -- objects of project classes ----------------------------------------------------------------------
    def _local_class(self, rel: str, fn: ast.AST, name: str) -> tuple[str, str] | None:
        """``(file, class)`` when every assignment of the local ``name`` in ``fn`` is a constructor call of one
        project class (``t = Txn()``), else None."""
        found: set[tuple[str, str]] = set()
        for n in _scope_walk(fn):
            targets = n.targets if isinstance(n, ast.Assign) else ([n.target] if isinstance(
                n, (ast.AnnAssign, ast.AugAssign, ast.For, ast.AsyncFor)) else [])
            for t in targets:
                if not any(isinstance(x, ast.Name) and x.id == name for x in ast.walk(t)):
                    continue
                value = getattr(n, "value", None)
                if not isinstance(n, ast.Assign) or not isinstance(t, ast.Name) or not isinstance(value, ast.Call):
                    return None
                written = value.func
                attrs = []
                while isinstance(written, ast.Attribute):
                    attrs.append(written.attr)
                    written = written.value
                if not isinstance(written, ast.Name):
                    return None
                base = self._module_qual(rel, written.id)
                loc = self._locate(".".join([base, *reversed(attrs)])) if base else None
                tree, _ = self.ix.tree(loc[0]) if loc else (None, None)
                if loc is None or not isinstance(_find_def(tree, loc[1]) if tree is not None else None,
                                                 ast.ClassDef):
                    return None
                found.add(loc)
        return found.pop() if len(found) == 1 else None

    def _typed_method(self, rel: str, ctx: tuple[ast.AST, str | None], call: ast.Call) -> tuple[str, str] | None:
        """``(file, Class.method)`` for ``obj.method()`` when ``obj`` is ``self``/``cls``, a parameter annotated
        with a project class or a local built by a project class's constructor, and that class defines
        ``method``; else None."""
        f = call.func
        if not (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name)):
            return None
        target = self._param_method(rel, ctx, f.value.id, f.attr)
        if target is None:
            loc = self._local_class(rel, ctx[0], f.value.id)
            target = (loc[0], f"{loc[1]}.{f.attr}") if loc else None
        if target is None:
            return None
        tree, _ = self.ix.tree(target[0])
        return target if tree is not None and _find_def(tree, target[1]) is not None else None

    def _visit(self, rel: str, qual: str, node: ast.AST, via: list[str], phase: str) -> list[tuple[str, str]]:
        """Check one definition; return the project definitions it calls."""
        cls = qual.rpartition(".")[0] if "." in qual else None
        typed: dict[int, int] = {}
        for call in ast.walk(node):
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and \
                    call.func.attr in ("commit", "save") and not call.args and not call.keywords and \
                    self._typed_method(rel, (node, cls), call) is not None:
                ln = call.end_lineno or call.lineno
                typed[ln] = typed.get(ln, 0) + 1
        self._sql_writes(rel, node, via, phase, cls)
        self._text(rel, node.lineno, node.end_lineno or node.lineno, via, phase, typed)
        self._state_writes(rel, node, via, phase, cls)
        uses = self._uses_map(rel)
        out: list[tuple[str, str]] = []
        for call in ast.walk(node):
            if isinstance(call, ast.Call):
                out += self._call(rel, call, uses.get(id(call)), via, phase, (node, cls))
        return out

    def _call(self, rel: str, call: ast.Call, scope, via: list[str], phase: str,
              ctx: tuple[ast.AST, str | None] | None) -> list[tuple[str, str]]:
        sc = self.ix.scopes(rel)
        f = call.func
        out: list[tuple[str, str]] = []
        written = ast.unparse(f)[:60]
        if isinstance(f, ast.Attribute) and f.attr in PATH_METHODS:
            self._add("file-write", rel, call.lineno, f"`{written}()` writes to the file system (a Path method)",
                      via, phase)
        if isinstance(f, ast.Attribute) and f.attr == "open":
            writes, why = _open_mode(ast.Call(func=f, args=[ast.Constant(None), *call.args], keywords=call.keywords))
            if writes:
                self._add("file-write", rel, call.lineno, f"`{written}()` opens a file with {why}", via, phase)
        reach = sc.qualify(f, scope) if (sc is not None and scope is not None) else None
        if reach is None:  # not a dotted name (a call on a call's result, a subscript ...)
            self.unresolved.append(f"{rel}:{call.lineno} {written}")
            return out
        if not reach:  # bound nowhere in this file: a builtin
            name = f.id if isinstance(f, ast.Name) else None
            if name == "open":
                writes, why = _open_mode(call)
                if writes:
                    self._add("file-write", rel, call.lineno, f"open() with {why}", via, phase)
            elif name is None:
                self.unresolved.append(f"{rel}:{call.lineno} {written}")
            return out
        for q, b in reach:
            if q is not None:
                for full, _chain in self.ix.resolve(q):
                    if full in OPEN_CALLS:
                        writes, why = _open_mode(call)
                        if writes:
                            self._add("file-write", rel, call.lineno, f"{full}() with {why}", via, phase)
                        continue
                    kind = sink_of(full)
                    if kind:
                        self._add(kind, rel, call.lineno, f"calls {full}", via, phase)
                        continue
                    loc = self._locate(full)
                    if loc is not None:
                        out.append(loc)
                continue
            if b.kind in ("def", "class"):
                attrs = []
                e = f
                while isinstance(e, ast.Attribute):
                    attrs.append(e.attr)
                    e = e.value
                base = ""
                if b.scope is not None and b.scope.kind == "class":
                    base = b.scope.name + "."
                out.append((rel, base + ".".join([e.id, *reversed(attrs)]) if isinstance(e, ast.Name) else b.name))
            elif b.kind == "param" and isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and ctx:
                target = self._param_method(rel, ctx, f.value.id, f.attr)
                if target is not None:
                    out.append(target)
                else:
                    self.unresolved.append(f"{rel}:{call.lineno} {written}")
            elif b.kind == "assign" and isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and ctx:
                target = self._typed_method(rel, ctx, call)  # `t = Txn(); t.commit()` -> Txn.commit
                if target is not None:
                    out.append(target)
                else:
                    self.unresolved.append(f"{rel}:{call.lineno} {written}")
            else:
                self.unresolved.append(f"{rel}:{call.lineno} {written}")
        return out

    def _param_method(self, rel: str, ctx: tuple[ast.AST, str | None], param: str, method: str
                      ) -> tuple[str, str] | None:
        """``self.m()`` in a method of C -> C.m; ``p.m()`` with ``p: Cls`` (a project class) -> Cls.m."""
        node, cls = ctx
        args = getattr(node, "args", None)
        if args is None:
            return None
        params = [*args.posonlyargs, *args.args]
        if cls and params and params[0].arg == param and param in ("self", "cls"):
            return rel, f"{cls}.{method}"
        for p in [*params, *args.kwonlyargs]:
            if p.arg == param and p.annotation is not None:
                ann = p.annotation
                if isinstance(ann, ast.Constant) and isinstance(ann.value, str):
                    try:
                        ann = ast.parse(ann.value, mode="eval").body
                    except SyntaxError:
                        return None
                name = ann.id if isinstance(ann, ast.Name) else None
                if isinstance(ann, ast.Subscript) and ast.unparse(ann.value) in ("Optional", "typing.Optional"):
                    name = ann.slice.id if isinstance(ann.slice, ast.Name) else None
                if not name:
                    return None
                full = self._module_qual(rel, name)
                loc = self._locate(full) if full else None
                if loc:
                    return loc[0], f"{loc[1]}.{method}"
        return None

    def _class_level(self, rel: str, cls: str | None) -> set[str]:
        """Names assigned in the body of class ``cls`` of ``rel`` and not set on the instance in its ``__init__``
        (``items: list = []``): ``self.items.append(x)`` changes state every instance shares."""
        tree, _ = self.ix.tree(rel)
        node = _find_def(tree, cls) if cls and tree is not None else None
        if not isinstance(node, ast.ClassDef):
            return set()
        # annotated names of a dataclass, attrs or pydantic class, a NamedTuple ... are per-instance fields
        plain = not node.decorator_list and not node.bases and not node.keywords
        names: set[str] = set()
        for st in node.body:
            if isinstance(st, ast.Assign):
                names |= {t.id for t in st.targets if isinstance(t, ast.Name)}
            elif isinstance(st, ast.AnnAssign) and st.value is not None and isinstance(st.target, ast.Name) and \
                    (plain or "ClassVar" in ast.unparse(st.annotation)):
                names.add(st.target.id)
        init = _find_def(tree, f"{cls}.__init__")
        for n in ast.walk(init) if init is not None else []:
            if isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Store) and isinstance(n.value, ast.Name) \
                    and n.value.id == "self":
                names.discard(n.attr)
        return names

    def _state_writes(self, rel: str, fn: ast.AST, via: list[str], phase: str, cls: str | None = None) -> None:
        """Writes to state the probe cannot isolate between calls (see the module docstring)."""
        sc = self.ix.scopes(rel)
        module_names = set(sc.module.binds) if sc is not None else set()
        shared = self._class_level(rel, cls)
        declared = {n for st in ast.walk(fn) if isinstance(st, ast.Global) for n in st.names}
        local: set[str] = set()
        args = getattr(fn, "args", None)
        if args is not None:
            local |= {a.arg for a in [*args.posonlyargs, *args.args, *args.kwonlyargs]}
            local |= {a.arg for a in (args.vararg, args.kwarg) if a is not None}
        for n in ast.walk(fn):
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store) and n.id not in declared:
                local.add(n.id)

        def module_kind(name: str) -> str | None:
            if name in local or name not in module_names or sc is None:
                return None
            kinds = {b.kind for b in sc.module.binds.get(name, [])}
            if kinds & {"import", "from"}:
                return "module"
            if "class" in kinds:
                return "class"
            if kinds & {"def"}:
                return None
            return "value"

        def root(e: ast.AST) -> ast.AST:
            while isinstance(e, (ast.Attribute, ast.Subscript)):
                e = e.value
            return e

        def class_object(e: ast.AST) -> bool:
            """Is ``e`` a class: ``cls``, ``type(self)``, ``self.__class__`` or a project class's name?"""
            if isinstance(e, ast.Name):
                return e.id == "cls" or module_kind(e.id) == "class"
            if isinstance(e, ast.Call):
                return ast.unparse(e.func) == "type"
            return isinstance(e, ast.Attribute) and e.attr == "__class__"

        def on_class(e: ast.AST) -> bool:
            """Does ``e`` (an object changed in place) live on a class: ``cls.X``, ``type(self).X``,
            ``self.__class__.X``, ``Counter.X`` or ``self.X`` with X a class-level name of this class?"""
            chain = []
            while isinstance(e, (ast.Attribute, ast.Subscript)):
                chain.append(e)
                e = e.value
            if not chain:
                return False
            first = chain[-1]
            if class_object(e) or class_object(first):
                return True
            return isinstance(e, ast.Name) and e.id == "self" and isinstance(first, ast.Attribute) and \
                first.attr in shared

        def target(t: ast.AST, line: int) -> None:
            if isinstance(t, (ast.Tuple, ast.List)):
                for x in t.elts:
                    target(x, line)
                return
            if isinstance(t, ast.Starred):
                target(t.value, line)
                return
            if isinstance(t, ast.Name):
                if t.id in declared:
                    self._add("global-state", rel, line, f"assigns the module global `{t.id}`", via, phase)
                return
            r = root(t)
            written = ast.unparse(t)[:60]
            for name, what in STATE_ROOTS.items():
                if written == name or written.startswith(name + ".") or written.startswith(name + "["):
                    self._add("global-state", rel, line, f"changes {what} (`{written}`)", via, phase)
                    return
            if isinstance(r, ast.Name):
                k = module_kind(r.id)
                if k == "module" and isinstance(t, ast.Attribute):
                    self._add("global-state", rel, line, f"sets an attribute of an imported module (`{written}`)",
                              via, phase)
                elif k == "class":
                    self._add("global-state", rel, line, f"changes class-level state (`{written}`)", via, phase)
                elif k == "value" or (r.id in declared):
                    self._add("global-state", rel, line, f"mutates the module-level `{r.id}` (`{written}`)",
                              via, phase)
                elif r.id in ("self", "cls") and isinstance(t, ast.Attribute) and \
                        ast.unparse(t).startswith(("self.__class__.", "cls.")):
                    self._add("global-state", rel, line, f"changes class-level state (`{written}`)", via, phase)
                elif r.id == "self" and isinstance(t, (ast.Attribute, ast.Subscript)) and on_class(t.value):
                    # `self.items[k] = v` with `items` a class attribute: every instance shares it
                    self._add("global-state", rel, line, f"changes class-level state (`{written}`)", via, phase)
            elif isinstance(r, ast.Call) and ast.unparse(r.func) == "type":
                self._add("global-state", rel, line, f"changes class-level state (`{written}`)", via, phase)

        for n in ast.walk(fn):
            if isinstance(n, ast.Assign):
                for t in n.targets:
                    target(t, n.lineno)
            elif isinstance(n, (ast.AugAssign, ast.AnnAssign)) and n.target is not None:
                if isinstance(n, ast.AnnAssign) and n.value is None:
                    continue
                target(n.target, n.lineno)
            elif isinstance(n, ast.Delete):
                for t in n.targets:
                    if not isinstance(t, ast.Name):
                        target(t, n.lineno)
            elif isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in MUTATORS:
                r = root(n.func.value)
                written = ast.unparse(n.func)[:60]
                for name, what in STATE_ROOTS.items():
                    if written.startswith(name + "."):
                        self._add("global-state", rel, n.lineno, f"changes {what} (`{written}()`)", via, phase)
                        break
                else:
                    if isinstance(r, ast.Name) and module_kind(r.id) == "value":
                        self._add("global-state", rel, n.lineno, f"mutates the module-level `{r.id}` "
                                                                 f"(`{written}()`)", via, phase)
                    elif on_class(n.func.value):
                        self._add("global-state", rel, n.lineno, f"changes class-level state (`{written}()`)",
                                  via, phase)
            elif isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in ("setattr", "delattr") \
                    and n.args:
                obj = n.args[0]
                written = ast.unparse(n)[:60]
                if isinstance(obj, ast.Name) and module_kind(obj.id) == "module":
                    self._add("global-state", rel, n.lineno, f"sets an attribute of an imported module "
                                                             f"(`{written}`)", via, phase)
                elif class_object(obj) or on_class(obj):
                    self._add("global-state", rel, n.lineno, f"changes class-level state (`{written}`)", via, phase)


def check(repo: Path, files: list[str], roots: list[tuple[str, str]]) -> dict:
    """Run the gate from ``roots`` (``(file, qualified name)``: the target and the recipe constructors)."""
    return Gate(repo, files).run(roots)
