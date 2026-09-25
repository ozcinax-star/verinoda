"""Concern rule tables and syntax helpers for ``verinoda review`` (docs/DESIGN.md D35).

Everything here reads source text; nothing imports or runs the analysed project's code. The tables
are data: each hit carries the ``derived_by`` label of the row that produced it, so a heuristic is
always named as one.

* Python: ``ast`` (guard diff, loops, value flow within one function, arity of call sites, SQL text
  built from strings, security calls that are builtins or keyword options). Calls bound through
  imports and aliases are judged by :func:`verinoda.guards._py_only_in` (the decision guards' engine).
* Other languages with a tree-sitter grammar in :data:`verinoda.anchors.TS_LANGS`: ``if`` statements
  whose body exits (guard diff), loops, parameter counts (Java / Kotlin).
* Text rules run on code with comments blanked (:func:`verinoda.guards.code_text`), strings kept
  where the rule reads them (SQL, config keys).
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field

from verinoda.architecture_map import SINK_PATTERNS

# -- tables -----------------------------------------------------------------------------------------

# Persistence sinks beyond architecture_map.SINK_PATTERNS that only the review reads (game saved data; JVM file IO).
EXTRA_SINK_PATTERNS = [
    (re.compile(r"\b(?:tag|nbt|compound|data)\s*\.\s*put(?:Int|Long|String|Boolean|Float|Double|Short|Byte|UUID|"
                r"IntArray|LongArray|ByteArray|Compound)?\s*\(\s*\""), "saved-data-write (NBT)"),
    # NeoForge / Forge mark a block entity for saving with setChanged(); Fabric / Yarn with markDirty()
    (re.compile(r"\bsetDirty\s*\(|\bmarkDirty\s*\(|\bsetChanged\s*\(\s*\)"), "saved-data-write (dirty flag)"),
    # SQL writes the map's table does not spell (SQLite's INSERT OR REPLACE / REPLACE INTO, upserts)
    (re.compile(r"\bINSERT\s+OR\s+(?:REPLACE|IGNORE|ABORT|FAIL|ROLLBACK)\s+INTO\b|\bREPLACE\s+INTO\b|"
                r"\bON\s+CONFLICT\b.*\bDO\s+UPDATE\b", re.I), "sql-write"),
    (re.compile(r"\bFiles\s*\.\s*(?:newBufferedWriter|newOutputStream|write|writeString|copy|move|delete|"
                r"deleteIfExists|createFile)\s*\(|\bnew\s+(?:FileOutputStream|FileWriter)\s*\("), "file-write"),
    (re.compile(r"\bFiles\s*\.\s*(?:newBufferedReader|newInputStream|readAllLines|readAllBytes|readString|lines|"
                r"list|walk)\s*\(|\bnew\s+(?:FileInputStream|FileReader|RandomAccessFile)\s*\("), "file-read"),
]
REVIEW_SINKS = [(rx, kind, "architecture_map.SINK_PATTERNS") for rx, kind in SINK_PATTERNS] + \
    [(rx, kind, "review.EXTRA_SINK_PATTERNS") for rx, kind in EXTRA_SINK_PATTERNS]
# the sink kinds that store something (persistence is about writes; reads and connections count on a changed line
# itself and inside loops)
WRITE_SINKS = {"sql-write", "orm-write", "file-write", "kv/object-store", "saved-data-write (NBT)",
               "saved-data-write (dirty flag)"}
# kinds that only the performance rules use (IO cost), never a persistence finding
IO_ONLY_SINKS = {"file-read"}
# saved-data (NBT) keys: tag.putInt("K", ..) writes K, tag.getInt("K") reads it
NBT_KEY = re.compile(r"\b(?:tag|nbt|compound|data)\s*\.\s*(put|get)[A-Z]?\w*\s*\(\s*\"([^\"\n]+)\"")

# Python calls, resolved through imports and aliases (guards engine): qualified name -> kind
PY_SECURITY_CALLS = {
    **{f"subprocess.{f}": "process-exec" for f in ("run", "call", "check_call", "check_output", "Popen",
                                                    "getoutput", "getstatusoutput")},
    **{f"os.{f}": "process-exec" for f in ("system", "popen", "execv", "execve", "execl", "execvp", "spawnl",
                                            "spawnv", "startfile")},
    "pickle.load": "deserialization", "pickle.loads": "deserialization", "pickle.Unpickler": "deserialization",
    "marshal.load": "deserialization", "marshal.loads": "deserialization", "shelve.open": "deserialization",
    "dill.loads": "deserialization", "yaml.unsafe_load": "deserialization", "yaml.full_load": "deserialization",
    "hashlib.md5": "weak-hash", "hashlib.sha1": "weak-hash",
    "tempfile.mktemp": "insecure-temp-file",
    "ssl._create_unverified_context": "tls-verification-off",
}
PY_SECURITY_BUILTINS = {"eval": "code-exec", "exec": "code-exec", "compile": "code-exec", "__import__": "code-exec"}
SQL_TEXT = re.compile(r"\b(SELECT\s+.+?\s+FROM|INSERT\s+(?:OR\s+\w+\s+)?INTO|REPLACE\s+INTO|UPDATE\s+\w+\s+SET|"
                      r"DELETE\s+FROM|DROP\s+TABLE|CREATE\s+TABLE)\b", re.I | re.S)
# the rest of a statement that makes lower- or mixed-case text SQL and not prose ("Select one item from the list")
_SQL_TAIL = re.compile(r"\b(WHERE|JOIN|ORDER\s+BY|GROUP\s+BY|LIMIT|VALUES|SET|RETURNING|HAVING)\b|[?]|%s|:[A-Za-z_]\w*",
                       re.I)
_SQL_SELECT_LIST = re.compile(r"\bSELECT\s+(?:DISTINCT\s+)?(?:\*|[\w.\"`\[\]()*]+(?:\s*,\s*[\w.\"`\[\]()*]+)+)\s+FROM\b",
                              re.I)
_LITERAL = re.compile(r"\"([^\"\n]*)\"|'([^'\n]*)'")


def sql_shaped(text: str) -> bool:
    """Is ``text`` (the text of a string) SQL rather than prose? SQL keywords written in upper case, or lower /
    mixed case with a select list of ``*`` or several columns, a table name built in (``{table}``), or a WHERE /
    VALUES / SET / JOIN ... clause or a placeholder after the keywords ("Select one item from the list" is not)."""
    for m in SQL_TEXT.finditer(text or ""):
        kw = re.sub(r"[^A-Za-z]", "", m.group(0).split()[0])
        if kw.isupper():
            return True
        rest = text[m.start():]
        after = rest[len(m.group(0)):]
        if _SQL_SELECT_LIST.match(rest) or _SQL_TAIL.search(after) or after.lstrip().startswith("{"):
            return True
    return False


def sql_line(code_line: str) -> bool:
    """A line of code holds SQL-shaped text: its string literals are read when the SQL keywords are in one (else
    the line itself, for a statement split over lines)."""
    lits = [a or b for a, b in _LITERAL.findall(code_line or "")]
    inside = [s for s in lits if SQL_TEXT.search(s)]
    if inside:
        return any(sql_shaped(s) for s in inside)
    return sql_shaped(code_line)
# Other languages: text over code (comments blanked, strings kept)
TEXT_SECURITY_OPS = [
    (re.compile(r"\bRuntime\s*\.\s*getRuntime\s*\(\s*\)\s*\.\s*exec\s*\(|\bnew\s+ProcessBuilder\s*\("), "process-exec"),
    (re.compile(r"\bnew\s+ObjectInputStream\s*\(|\.\s*readObject\s*\(\s*\)"), "deserialization"),
    (re.compile(r"MessageDigest\s*\.\s*getInstance\s*\(\s*\"(?:MD5|SHA-?1)\"", re.I), "weak-hash"),
    (re.compile(r"\bchild_process\b|\bexecSync\s*\(|\bnew\s+Function\s*\("), "process-exec"),
    (re.compile(r"\bexec\.Command\s*\("), "process-exec"),
    (re.compile(r"InsecureSkipVerify\s*:\s*true|rejectUnauthorized\s*:\s*false"), "tls-verification-off"),
    (re.compile(r"\"[^\"\n]*\b(?:SELECT\s+.+\s+FROM|INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM)\b[^\"\n]*\"\s*\+|"
                r"\+\s*\"[^\"\n]*\b(?:WHERE|VALUES|SET)\b", re.I), "sql-built-from-strings"),
]
# permission / authorisation checks written as calls (a removed or changed one is a finding)
PERMISSION_CALL = re.compile(
    r"\b(hasPermission|hasPermissionLevel|checkPermission|hasRole|hasAuthority|hasAnyRole|isOp|isOperator|"
    r"requires|withLevel|withPermission|permission_required|login_required|user_passes_test|has_perm|"
    r"is_authenticated|is_superuser|is_staff|check_password|verify_password|authorize|authenticate)\s*[({]")
# words that make a changed line security-relevant (heuristic: weak_inference at most)
AUTH_WORDS = re.compile(r"(?i)\b(password|passwd|secret|api_?key|token|credential|csrf|permission|is_admin|"
                        r"authori[sz]\w*|authenticat\w*|encrypt\w*|decrypt\w*)\b|verify\s*=\s*False")
# a function whose name says it decides whether something is allowed or valid
CHECK_NAME = re.compile(r"^(?:is|has|can|may|still|check|validate|verify|authori[sz]e|allow)[A-Z_]"
                        r"|^(?:stillValid|mayPlace|mayPickup|canUse|isValid|hasPermission)$|_valid$|_allowed$", re.I)

# Hot paths and entry points registered by name (text search: an inference, labelled as such).
# (regex with groups (owner, method), what it registers, hot?, entry kind)
REGISTRATIONS = [
    (re.compile(r"\bcreateTickerHelper\s*\(.*?\b(\w+)\s*::\s*(\w+)"), "block entity ticker", True, None),
    (re.compile(r"\b(?:ServerTickEvents|ClientTickEvents|ServerWorldTickEvents|WorldTickEvents)\s*\.\s*\w+\s*\.\s*"
                r"register\s*\(\s*(\w+)\s*::\s*(\w+)"), "tick event", True, None),
    (re.compile(r"\b(?:WorldRenderEvents|HudRenderCallback|ScreenEvents)\s*\.\s*\w+\s*\.\s*register\s*\(\s*(\w+)\s*::"
                r"\s*(\w+)"), "render event", True, None),
    (re.compile(r"\bServerChunkEvents\s*\.\s*CHUNK_LOAD\s*\.\s*register\s*\(\s*(\w+)\s*::\s*(\w+)"), "chunk load event",
     True, "world event"),
    (re.compile(r"\.\s*playToServer\s*\(.*?\b(\w+)\s*::\s*(\w+)"), "client->server packet handler", False,
     "client->server packet"),
    (re.compile(r"\bServerPlayNetworking\s*\.\s*registerGlobalReceiver\s*\(.*?\b(\w+)\s*::\s*(\w+)"),
     "client->server packet receiver", False, "client->server packet"),
    (re.compile(r"\.\s*playToClient\s*\(.*?\b(\w+)\s*::\s*(\w+)"), "server->client packet handler (client side)",
     False, "server->client packet"),
    (re.compile(r"\bClientPlayNetworking\s*\.\s*registerGlobalReceiver\s*\(.*?\b(\w+)\s*::\s*(\w+)"),
     "server->client packet receiver (client side)", False, "server->client packet"),
    (re.compile(r"\.\s*executes\s*\(\s*(\w+)\s*::\s*(\w+)"), "command handler", False, "command"),
    (re.compile(r"\b(?:UseBlockCallback|UseItemCallback|UseEntityCallback|AttackBlockCallback|AttackEntityCallback)"
                r"\s*\.\s*EVENT\s*\.\s*register\s*\(\s*(\w+)\s*::\s*(\w+)"), "player interaction callback", False,
     "player input"),
]
# methods the game calls every tick or frame, by name (overrides): hot path by name, an inference
HOT_OVERRIDES = {"tick", "serverTick", "clientTick", "baseTick", "aiStep", "animateTick", "randomTick", "render",
                 "renderBg", "renderWidget", "renderTooltip", "inventoryTick", "onUpdate", "update"}
# methods the game calls on player input (overrides): an entry by name, an inference
INPUT_OVERRIDES = {"useItemOn", "useWithoutItem", "use", "onUse", "useOn", "interactAt", "mobInteract",
                   "onItemUse", "handle"}
# NeoForge @SubscribeEvent methods by event type
EVENT_PARAM = re.compile(r"@SubscribeEvent[\s\S]{0,200}?\(\s*(?:final\s+)?([\w.]+(?:Tick|Render\w*)Event[\w.]*)\s+\w+")

CONFIG_CALL = re.compile(r"\b(define\w*|getInt|getString|getBoolean|getDouble|getLong|getFloat|getProperty|getenv|"
                         r"number|section|getConfig\w*)\s*\(")
QUOTED_KEY = re.compile(r"\"([A-Za-z][A-Za-z0-9_.\-]{2,})\"|'([A-Za-z][A-Za-z0-9_.\-]{2,})'")
CONFIG_READ_JVM = re.compile(r"\b(\w*Config\w*)\s*\.\s*(?:get\s*\(\s*\)\s*\.\s*)?([A-Za-z_]\w*)")
# members of a config class that do something rather than hold a value (GlowConfig.load() reads a file)
CONFIG_VERBS = {"load", "reload", "save", "init", "initialize", "register", "write", "read", "parse", "create", "of",
                "builder", "build", "setup", "bootstrap", "open", "close", "reset", "apply", "validate", "sync"}
# NBT receivers: a key read from saved data is not a configuration key
NBT_RECEIVER = re.compile(r"\b(?:tag|nbt|compound)\s*\.\s*\w+\s*\(\s*$")
CONFIG_SUFFIXES = (".toml", ".yml", ".yaml", ".json", ".properties", ".ini", ".cfg", ".conf", ".env")

STATUS_ORDER = ("observed", "experiment_verified", "statically_verified", "primary_source_verified",
                "strong_inference", "weak_inference", "unknown")


def rank(status: str) -> int:
    return STATUS_ORDER.index(status) if status in STATUS_ORDER else len(STATUS_ORDER)


def at_least_strong(status: str) -> bool:
    return rank(status) <= rank("strong_inference")


# -- sinks --------------------------------------------------------------------------------------------

def sink_hits(code_lines: list[str], lo: int, hi: int) -> list[tuple[int, str, str]]:
    """``(line, kind, derived_by)`` of persistence sinks on lines ``lo..hi`` of comment-free code lines. An SQL row
    counts only when the text is SQL-shaped (:func:`sql_shaped`), not prose such as an error message."""
    out = []
    for i in range(max(1, lo), min(hi, len(code_lines)) + 1):
        text = code_lines[i - 1]
        for rx, kind, by in REVIEW_SINKS:
            if rx.search(text) and (not kind.startswith("sql-") or sql_line(text)):
                out.append((i, kind, by))
                break
    return out


# -- Python helpers -----------------------------------------------------------------------------------

def py_parse(text: str | None) -> ast.AST | None:
    if text is None:
        return None
    from verinoda.anchors import parse_python

    try:
        return parse_python(text)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return None


def py_def_at(tree: ast.AST | None, def_line: int) -> ast.AST | None:
    """The def/class node whose ``def`` line is ``def_line``."""
    if tree is None:
        return None
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.lineno == def_line:
            return n
    return None


def call_name(call: ast.Call) -> str | None:
    f = call.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return None


def dotted(expr: ast.AST) -> str | None:
    parts = []
    while isinstance(expr, ast.Attribute):
        parts.append(expr.attr)
        expr = expr.value
    if isinstance(expr, ast.Name):
        parts.append(expr.id)
        return ".".join(reversed(parts))
    return None


def own_nodes(fn: ast.AST):
    """Nodes of a function body, not descending into nested defs, classes or lambdas."""
    stack = list(ast.iter_child_nodes(fn))
    while stack:
        n = stack.pop()
        yield n
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        stack.extend(ast.iter_child_nodes(n))


_CARRY_BUILTINS = {"round", "int", "float", "str", "abs", "min", "max", "sum", "sorted", "list", "tuple", "dict",
                   "set", "bool", "Decimal", "format", "repr", "reversed", "frozenset"}


def _expr_carries(expr: ast.AST | None, tainted: set[str], callee: str) -> bool:
    """Does the value of ``expr`` depend (by data, within this expression) on ``tainted`` names or on a call to
    ``callee``? A call to another function carries nothing (no inter-procedural flow), except builtins that
    transform their argument (``round``, ``int``, ``str``...)."""
    if expr is None:
        return False
    if isinstance(expr, ast.Name):
        return expr.id in tainted
    if isinstance(expr, ast.Call):
        name = call_name(expr)
        if name == callee:
            return True
        if isinstance(expr.func, ast.Name) and name in _CARRY_BUILTINS:
            return any(_expr_carries(a, tainted, callee) for a in expr.args)
        if isinstance(expr.func, ast.Attribute) and name in ("format", "join", "strip", "lower", "upper", "copy"):
            return _expr_carries(expr.func.value, tainted, callee) or any(
                _expr_carries(a, tainted, callee) for a in expr.args)
        return False
    if isinstance(expr, (ast.Lambda, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return False
    if isinstance(expr, ast.Attribute):
        return False   # a field of an object: values inside containers and objects are not followed
    if isinstance(expr, ast.Subscript):
        return _expr_carries(expr.slice, tainted, callee)   # an item of a container is not the container's value
    return any(_expr_carries(c, tainted, callee) for c in ast.iter_child_nodes(expr))


@dataclass
class Carry:
    returns: bool = False
    returns_at: int | None = None
    passes: list[tuple[int, ast.Call, str]] = field(default_factory=list)   # (line, call, callee name)
    tainted: set[str] = field(default_factory=set)


def py_carry(fn: ast.AST, callee: str, call_lines: set[int] | None = None) -> Carry:
    """Intra-procedural def-use in ``fn``: is the result of calls to ``callee`` returned, or passed as an
    argument to another call (per call site; names bound from it are followed through assignments)?

    Straight-line reading: a later reassignment does not untaint a name, branches are not told apart
    (the result is what *may* carry the value)."""
    tainted: set[str] = set()
    stmts = [n for n in own_nodes(fn) if isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.NamedExpr))]
    stmts.sort(key=lambda n: (n.lineno, n.col_offset))
    for _ in range(3):  # a few passes settle chains of assignments written out of order
        before = len(tainted)
        for st in stmts:
            value = st.value
            if _expr_carries(value, tainted, callee):
                targets = st.targets if isinstance(st, ast.Assign) else [st.target]
                for t in targets:
                    for n in ast.walk(t):
                        if isinstance(n, ast.Name):
                            tainted.add(n.id)
        if len(tainted) == before:
            break
    out = Carry(tainted=tainted)
    for n in own_nodes(fn):
        if isinstance(n, ast.Return) and _expr_carries(n.value, tainted, callee):
            out.returns, out.returns_at = True, n.lineno
        elif isinstance(n, (ast.Yield, ast.YieldFrom)) and _expr_carries(n.value, tainted, callee):
            out.returns, out.returns_at = True, n.lineno
        elif isinstance(n, ast.Call):
            name = call_name(n)
            if name == callee or (isinstance(n.func, ast.Name) and name in _CARRY_BUILTINS):
                continue
            args = list(n.args) + [k.value for k in n.keywords]
            if any(_expr_carries(a, tainted, callee) for a in args):
                out.passes.append((n.lineno, n, name or "?"))
    out.passes.sort(key=lambda p: p[0])
    return out


def _fixed_value(e: ast.AST | None) -> bool:
    """A value that does not compute from the input: a constant, a name, an attribute, a message (f-string),
    or a tuple / list / dict of those (``return 404, {"error": "not found"}``)."""
    if e is None or isinstance(e, (ast.Constant, ast.Name, ast.Attribute, ast.JoinedStr)):
        return True
    if isinstance(e, (ast.Tuple, ast.List, ast.Set)):
        return all(_fixed_value(x) for x in e.elts)
    if isinstance(e, ast.Dict):
        return all(_fixed_value(x) for x in [*e.keys, *e.values] if x is not None)
    return False


def _py_guard_exit(stmt: ast.AST) -> bool:
    """An exit that stops the work (raise / continue / break, or a return of a fixed value), not a branch that
    returns a computed result."""
    if isinstance(stmt, (ast.Raise, ast.Continue, ast.Break)):
        return True
    return isinstance(stmt, ast.Return) and _fixed_value(stmt.value)


def py_carrying_params(call: ast.Call, callee_fn: ast.AST, tainted: set[str], callee: str, *,
                       bound: bool) -> set[str]:
    """The parameters of ``callee_fn`` that receive a carried value at ``call`` (by position or keyword)."""
    args = getattr(callee_fn, "args", None)
    if args is None:
        return set()
    params = [p.arg for p in getattr(args, "posonlyargs", [])] + [p.arg for p in args.args]
    if bound and params and params[0] in ("self", "cls"):
        params = params[1:]
    out = set()
    for i, a in enumerate(call.args):
        if isinstance(a, ast.Starred):
            break
        if i < len(params) and _expr_carries(a, tainted, callee):
            out.add(params[i])
    for kw in call.keywords:
        if kw.arg and _expr_carries(kw.value, tainted, callee):
            out.add(kw.arg)
    return out


def py_taint(fn: ast.AST, seeds: set[str]) -> set[str]:
    """The names of ``fn`` that may hold a value computed from the names ``seeds`` (through assignments;
    straight-line reading, containers and object fields not followed)."""
    tainted = set(seeds)
    stmts = [n for n in own_nodes(fn) if isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign))]
    stmts.sort(key=lambda n: (n.lineno, n.col_offset))
    for _ in range(3):
        before = len(tainted)
        for st in stmts:
            if st.value is not None and _expr_carries(st.value, tainted, "\0"):
                targets = st.targets if isinstance(st, ast.Assign) else [st.target]
                tainted |= {n.id for t in targets for n in ast.walk(t) if isinstance(n, ast.Name)}
        if len(tainted) == before:
            break
    return tainted


def py_flows_to_line(fn: ast.AST, seeds: set[str], line: int) -> bool:
    """Does a value from the names ``seeds`` reach the statement holding ``line`` in ``fn`` (through
    assignments; straight-line reading)?"""
    tainted = py_taint(fn, seeds)
    best = None
    for n in own_nodes(fn):
        if isinstance(n, ast.stmt) and n.lineno <= line <= (n.end_lineno or n.lineno) and \
                not isinstance(n, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try)):
            if best is None or (n.end_lineno or n.lineno) - n.lineno < (best.end_lineno or best.lineno) - best.lineno:
                best = n
    if best is None:
        return False
    return any(isinstance(x, ast.Name) and x.id in tainted for x in ast.walk(best))


def py_param_sources(fn: ast.AST) -> dict[str, set[str]]:
    """name -> the parameters of ``fn`` its value may come from (parameters themselves, then names assigned
    from expressions that use them; straight-line reading, no calls into other functions except builtins)."""
    args = getattr(fn, "args", None)
    params = [p.arg for p in getattr(args, "posonlyargs", [])] + [p.arg for p in args.args] + \
        [p.arg for p in args.kwonlyargs] if args is not None else []
    src: dict[str, set[str]] = {p: {p} for p in params if p not in ("self", "cls")}
    stmts = [n for n in own_nodes(fn) if isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign))]
    stmts.sort(key=lambda n: (n.lineno, n.col_offset))
    for _ in range(3):
        changed = False
        for st in stmts:
            if st.value is None:
                continue
            got = set()
            for name, ps in list(src.items()):
                if _expr_carries(st.value, {name}, "\0"):
                    got |= ps
            if not got:
                continue
            targets = st.targets if isinstance(st, ast.Assign) else [st.target]
            for t in targets:
                for n in ast.walk(t):
                    if isinstance(n, ast.Name) and not got <= src.get(n.id, set()):
                        src.setdefault(n.id, set()).update(got)
                        changed = True
        if not changed:
            break
    return src


def py_params_on_line(fn: ast.AST, line: int) -> set[str]:
    """The parameters of ``fn`` whose values reach the statement holding ``line``."""
    src = py_param_sources(fn)
    best = None
    for n in own_nodes(fn):
        if isinstance(n, ast.stmt) and n.lineno <= line <= (n.end_lineno or n.lineno) and \
                not isinstance(n, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try)):
            if best is None or (n.end_lineno or n.lineno) - n.lineno < (best.end_lineno or best.lineno) - best.lineno:
                best = n
    if best is None:
        return set()
    return {p for x in ast.walk(best) if isinstance(x, ast.Name) for p in src.get(x.id, set())}


def py_args_from_params(caller_fn: ast.AST, call: ast.Call, callee_fn: ast.AST, wanted: set[str], *,
                        bound: bool) -> set[str]:
    """The parameters of ``caller_fn`` that flow into the arguments ``call`` passes to the ``wanted``
    parameters of ``callee_fn``."""
    args = getattr(callee_fn, "args", None)
    if args is None:
        return set()
    params = [p.arg for p in getattr(args, "posonlyargs", [])] + [p.arg for p in args.args]
    if bound and params and params[0] in ("self", "cls"):
        params = params[1:]
    exprs = [a for i, a in enumerate(call.args) if not isinstance(a, ast.Starred) and i < len(params)
             and params[i] in wanted]
    exprs += [kw.value for kw in call.keywords if kw.arg in wanted]
    src = py_param_sources(caller_fn)
    return {p for e in exprs for x in ast.walk(e) if isinstance(x, ast.Name) for p in src.get(x.id, set())}


def py_guards(fn: ast.AST) -> list[tuple[str, int]]:
    """``(condition, line)`` of the exit guards in ``fn``: an ``if`` whose body ends the function or the loop
    iteration (raise / continue / break, or a return of a fixed value), and ``assert`` statements."""
    out = []
    for n in own_nodes(fn):
        if isinstance(n, ast.If) and n.body and _py_guard_exit(n.body[-1]):
            out.append((_unparse(n.test), n.lineno))
        elif isinstance(n, ast.Assert):
            out.append(("assert " + _unparse(n.test), n.lineno))
    return sorted(out, key=lambda g: g[1])


def _unparse(node: ast.AST) -> str:
    try:
        return " ".join(ast.unparse(node).split())
    except Exception:  # noqa: BLE001 - very old or odd nodes
        return ast.dump(node)


def py_loops(fn: ast.AST) -> list[tuple[ast.AST, int, int, str | None]]:
    """``(loop node, first line, last line, iterated name)`` of the loops (and comprehensions) in ``fn``."""
    out = []
    for n in own_nodes(fn):
        if isinstance(n, (ast.For, ast.AsyncFor)):
            it = n.iter.id if isinstance(n.iter, ast.Name) else dotted(n.iter)
            out.append((n, n.lineno, n.end_lineno or n.lineno, it))
        elif isinstance(n, ast.While):
            out.append((n, n.lineno, n.end_lineno or n.lineno, None))
        elif isinstance(n, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            gen = n.generators[0] if n.generators else None
            it = gen.iter.id if gen is not None and isinstance(gen.iter, ast.Name) else None
            out.append((n, n.lineno, n.end_lineno or n.lineno, it))
    return out


def py_calls_in(node: ast.AST) -> list[ast.Call]:
    return [c for c in ast.walk(node) if isinstance(c, ast.Call)]


def py_len_cap(fn: ast.AST, name: str) -> int | None:
    """Line of a guard in ``fn`` that caps ``len(name)`` (``if len(name) > X: raise/return``), else None."""
    for cond, line in py_guards(fn):
        if re.search(rf"\blen\(\s*{re.escape(name)}\s*\)\s*(>|>=)", cond) or \
                re.search(rf"(<|<=)\s*len\(\s*{re.escape(name)}\s*\)", cond):
            return line
    return None


def py_import_map(tree: ast.AST | None) -> dict[str, str]:
    """alias -> qualified name bound by the file's imports (``import yaml as y`` -> ``y: yaml``; ``from yaml
    import load`` -> ``load: yaml.load``; relative imports are left out)."""
    out: dict[str, str] = {}
    for n in ast.walk(tree) if tree is not None else ():
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.asname:
                    out[a.asname] = a.name
                else:
                    out.setdefault(a.name.split(".")[0], a.name.split(".")[0])
        elif isinstance(n, ast.ImportFrom) and n.module and not n.level:
            for a in n.names:
                if a.name != "*":
                    out[a.asname or a.name] = f"{n.module}.{a.name}"
    return out


def py_qualified(expr: ast.AST, imports: dict[str, str]) -> str | None:
    """The qualified name of a called expression through the file's imports (``load`` -> ``yaml.load``)."""
    d = dotted(expr)
    if d is None:
        return None
    head, _, rest = d.partition(".")
    if head in imports:
        return imports[head] + (f".{rest}" if rest else "")
    return None


# calls that send SQL text to a database (the argument that carries the text)
DB_CALLS = {"execute", "executemany", "executescript", "raw", "text", "query", "read_sql", "read_sql_query",
            "mogrify", "exec_driver_sql"}


def _sql_reaches_db(fn: ast.AST | None, node: ast.AST) -> int | None:
    """Line of the database call (``execute`` ...) that the SQL text built at ``node`` reaches: as an argument
    of the call itself, or through a name assigned from it (def-use within ``fn``, straight-line). None when
    that is not seen."""
    if fn is None:
        return None
    parents: dict[int, ast.AST] = {}
    for p in ast.walk(fn):
        for ch in ast.iter_child_nodes(p):
            parents[id(ch)] = p
    cur = node
    while id(cur) in parents:
        par = parents[id(cur)]
        if isinstance(par, ast.Call) and call_name(par) in DB_CALLS and cur is not par.func:
            return par.lineno
        if isinstance(par, (ast.Assign, ast.AnnAssign)) and cur is par.value:
            targets = par.targets if isinstance(par, ast.Assign) else [par.target]
            names = {t.id for t in targets if isinstance(t, ast.Name)}
            if not names:
                return None
            for call in sorted(py_calls_in(fn), key=lambda c: c.lineno):
                if call_name(call) in DB_CALLS and call.lineno > par.lineno and py_flows_to_line(fn, names,
                                                                                                  call.lineno):
                    return call.lineno
            return None
        if isinstance(par, ast.stmt):
            return None
        cur = par
    return None


def py_security_ops(tree: ast.AST, rel: str, ix, lines: set[int] | None = None) -> list[tuple[int, str, str, str,
                                                                                                str]]:
    """``(line, kind, what, derived_by, status)`` of security-sensitive operations in a Python file's text.

    Qualified calls go through the decision guards' import/alias engine (``ix``: a
    :class:`verinoda.guards._PyIndex` of the tree under review) and through the file's own imports (``from yaml
    import load``); builtins, ``shell=True``, ``verify=False``, ``yaml.load`` without a safe loader and SQL text
    built with f-strings, ``%``, ``+`` or ``.format`` are read from the syntax tree. A call bound through the
    imports is ``statically_verified``; SQL text built from strings is ``statically_verified`` when the text is
    seen reaching a database call (``execute`` ...) in the same function, else ``strong_inference``."""
    from verinoda import guards

    out: list[tuple[int, str, str, str, str]] = []
    if ix is not None:
        try:
            scan = guards.Scan()
            for level, line, why in guards._py_only_in(ix, rel, {t: t for t in PY_SECURITY_CALLS}, scan):
                if level == guards.VIOLATED and (lines is None or line in lines):
                    target = next((t for t in PY_SECURITY_CALLS if t in why), None)
                    out.append((line, PY_SECURITY_CALLS.get(target, "security-call"), why,
                                "review.PY_SECURITY_CALLS via the guards import/alias engine", "statically_verified"))
        except Exception:  # noqa: BLE001 - the syntax-tree rules below still run
            pass
    imports = py_import_map(tree)
    bound = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)} | \
        {a.asname or a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names} | \
        {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    funcs = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]

    def enclosing_fn(ln: int):
        best = None
        for f in funcs:
            if f.lineno <= ln <= (f.end_lineno or f.lineno) and (best is None or f.lineno > best.lineno):
                best = f
        return best

    def sql(n: ast.AST, ln: int, how: str, text: str) -> None:
        if not sql_shaped(text):
            return
        db = _sql_reaches_db(enclosing_fn(ln), n)
        if db is not None:
            out.append((ln, "sql-built-from-strings", f"SQL text built with {how}, passed to a database call at "
                        f"line {db}", "review.SQL_TEXT", "statically_verified"))
        else:
            out.append((ln, "sql-built-from-strings", f"SQL text built with {how} (not seen reaching a database "
                        "call in this function)", "review.SQL_TEXT", "strong_inference"))

    for n in ast.walk(tree):
        if not isinstance(n, (ast.Call, ast.JoinedStr, ast.BinOp)):
            continue
        ln = getattr(n, "lineno", None)
        if ln is None or (lines is not None and not (set(range(ln, (n.end_lineno or ln) + 1)) & lines)):
            continue
        if isinstance(n, ast.Call):
            name = call_name(n)
            qual = py_qualified(n.func, imports)
            if isinstance(n.func, ast.Name) and name in PY_SECURITY_BUILTINS and name not in bound:
                out.append((ln, PY_SECURITY_BUILTINS[name], f"{name}() on dynamic input", "review.PY_SECURITY_BUILTINS",
                            "statically_verified"))
            if qual in PY_SECURITY_CALLS:
                out.append((ln, PY_SECURITY_CALLS[qual], f"call to {qual}", "review.PY_SECURITY_CALLS via the file's "
                            "imports", "statically_verified"))
            for kw in n.keywords:
                kl = getattr(kw.value, "lineno", ln)   # cite the line of the keyword itself
                if kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                    out.append((kl, "process-exec", f"{name}(..., shell=True)", "review: shell=True keyword",
                                "statically_verified"))
                if kw.arg == "verify" and isinstance(kw.value, ast.Constant) and kw.value.value is False:
                    out.append((kl, "tls-verification-off", f"{name}(..., verify=False)", "review: verify=False",
                                "statically_verified"))
            if (qual or dotted(n.func)) == "yaml.load":
                loader = [kw.value for kw in n.keywords if kw.arg == "Loader"] + list(n.args[1:2])
                if not any("Safe" in _unparse(v) for v in loader):
                    out.append((ln, "deserialization", "yaml.load without a safe Loader", "review: yaml.load (bound "
                                "through the file's imports)", "statically_verified"))
            if isinstance(n.func, ast.Attribute) and n.func.attr == "format" and isinstance(n.func.value, ast.Constant) \
                    and isinstance(n.func.value.value, str) and SQL_TEXT.search(n.func.value.value):
                sql(n, ln, "str.format", n.func.value.value)
        elif isinstance(n, ast.JoinedStr):
            text = "".join(v.value for v in n.values if isinstance(v, ast.Constant) and isinstance(v.value, str))
            if SQL_TEXT.search(text) and any(isinstance(v, ast.FormattedValue) for v in n.values):
                sql(n, ln, "an f-string", "".join(v.value if isinstance(v, ast.Constant) and isinstance(v.value, str)
                                                  else "{x}" for v in n.values))
        elif isinstance(n, ast.BinOp) and isinstance(n.op, (ast.Mod, ast.Add)):
            consts = [c.value for c in (n.left, n.right) if isinstance(c, ast.Constant) and isinstance(c.value, str)]
            hit = next((c for c in consts if SQL_TEXT.search(c)), None)
            if hit is not None:
                sql(n, ln, "%" if isinstance(n.op, ast.Mod) else "+", hit)
    seen, uniq = set(), []
    for item in sorted(out, key=lambda o: (o[0], o[1], rank(o[4]))):
        if (item[0], item[1]) not in seen:
            seen.add((item[0], item[1]))
            uniq.append(item)
    return uniq


def py_param_names(fn: ast.AST | None) -> list[str]:
    """Positional then keyword-only parameter names of a def, ``self`` / ``cls`` included."""
    a = getattr(fn, "args", None)
    if a is None:
        return []
    return [p.arg for p in getattr(a, "posonlyargs", [])] + [p.arg for p in a.args] + [p.arg for p in a.kwonlyargs]


def py_scope_names(fn: ast.AST) -> set[str]:
    """Names a function (or lambda) binds locally: parameters, assignment and loop targets, nested defs and
    classes; minus names declared ``global`` / ``nonlocal``. Imports inside the function are not counted: they
    bind the imported definition, which the review follows through the file's imports."""
    names = set(py_param_names(fn))
    a = getattr(fn, "args", None)
    if a is not None:
        names |= {x.arg for x in (a.vararg, a.kwarg) if x is not None}
    declared: set[str] = set()
    for n in own_nodes(fn):
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            names.add(n.id)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(n.name)
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            declared |= set(n.names)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            names.add(n.name)
    return names - declared


def py_shadowing(tree: ast.AST | None) -> dict[int, frozenset[str]]:
    """``id(ast.Name)`` -> the names bound locally by the functions that enclose that name (a parameter named
    like a module-level function hides it there)."""
    out: dict[int, frozenset[str]] = {}
    if tree is None:
        return out
    stack: list[tuple[ast.AST, frozenset[str]]] = [(tree, frozenset())]
    while stack:
        node, scope = stack.pop()
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                stack.append((ch, scope | frozenset(py_scope_names(ch))))
                continue
            if isinstance(ch, ast.Name):
                out[id(ch)] = scope
            stack.append((ch, scope))
    return out


def py_single_return(fn: ast.AST | None) -> tuple[list[str], ast.AST] | None:
    """``(parameters, returned expression)`` of a def whose body is one ``return <expr>`` (a docstring
    aside), else None: a helper a guard may have been moved into."""
    body = list(getattr(fn, "body", []) or [])
    if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) and \
            isinstance(body[0].value.value, str):
        body = body[1:]
    if len(body) == 1 and isinstance(body[0], ast.Return) and body[0].value is not None:
        return py_param_names(fn), body[0].value
    return None


def py_conditions(fn: ast.AST | None) -> list[tuple[str, int]]:
    """``(condition, line)`` of every if / while / conditional expression / assert in ``fn``."""
    out = []
    for n in own_nodes(fn) if fn is not None else ():
        if isinstance(n, (ast.If, ast.While, ast.IfExp, ast.Assert)):
            out.append((_unparse(n.test), n.lineno))
    return out


def py_raising_guards(fn: ast.AST | None) -> list[int]:
    """Lines of the checks in ``fn`` that stop the work with an exception (``if ...: raise``, ``assert``)."""
    out = []
    for n in own_nodes(fn) if fn is not None else ():
        if isinstance(n, ast.If) and n.body and isinstance(n.body[-1], ast.Raise):
            out.append(n.lineno)
        elif isinstance(n, ast.Assert):
            out.append(n.lineno)
    return sorted(out)


# -- conditions compared as text (guard diff, any language) ---------------------------------------------

def cond_key(text: str) -> str:
    """A condition for comparison: whitespace collapsed and dropped next to punctuation, outer parentheses
    removed (``(a > b)`` and ``a>b`` are one key; ``not x`` keeps its space)."""
    t = re.sub(r"\s*([^\w\s])\s*", r"\1", " ".join((text or "").split()))
    while t.startswith("(") and t.endswith(")") and _balanced(t[1:-1]):
        t = t[1:-1]
    return t


def _balanced(t: str) -> bool:
    depth = 0
    for ch in t:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def split_top(text: str, seps: tuple[str, ...]) -> list[str]:
    """``text`` split at the separators found outside parentheses, brackets and quotes (word separators such
    as ``" and "`` must be spelled with their spaces)."""
    parts, depth, cur, i, quote = [], 0, [], 0, None
    while i < len(text):
        ch = text[i]
        if quote:
            cur.append(ch)
            if ch == quote and text[i - 1] != "\\":
                quote = None
            i += 1
            continue
        if ch in "\"'":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if depth == 0:
            sep = next((s for s in seps if text.startswith(s, i)), None)
            if sep:
                parts.append("".join(cur))
                cur = []
                i += len(sep)
                continue
        cur.append(ch)
        i += 1
    parts.append("".join(cur))
    return [p.strip() for p in parts if p.strip()]


_FLIP = {">=": "<", "<=": ">", "==": "!=", "!=": "==", ">": "<=", "<": ">="}
BOOL_SEPS = ("&&", "||", " and ", " or ")


def negations(text: str) -> set[str]:
    """Keys (:func:`cond_key`) of the negation of a condition: ``not (c)``, ``!(c)``, and the comparison with its
    operator flipped (``a > b`` -> ``a <= b``); for ``not c`` / ``!c`` the condition ``c`` itself."""
    k = cond_key(text)
    out = {f"not({k})", f"not {k}", f"!({k})", f"!{k}"}
    for pre in ("not ", "not(", "!"):
        if k.startswith(pre):
            out.add(cond_key(k[len(pre) - (1 if pre.endswith("(") else 0):]))
    ops = [(m.start(), m.group(0)) for m in re.finditer(r">=|<=|==|!=|(?<![-=<>])>(?![=>])|(?<![<=])<(?![=<])", k)]
    top = []
    for pos, op in ops:
        if _balanced(k[:pos]) and not re.search(r"[\"']", k[:pos]):
            top.append((pos, op))
    if len(top) == 1:
        pos, op = top[0]
        out.add(cond_key(k[:pos] + _FLIP[op] + k[pos + len(op):]))
    return out


def cond_pieces(text: str) -> set[str]:
    """Keys of a condition and of its top-level ``and`` / ``or`` operands."""
    k = cond_key(text)
    return {k} | {cond_key(p) for p in split_top(" ".join(text.split()), BOOL_SEPS)}


def call_args(text: str, name: str) -> list[str] | None:
    """The argument texts of the first call to ``name`` in ``text`` (None when there is no such call)."""
    m = re.search(rf"(?<![\w.]){re.escape(name)}\s*\(", text) or re.search(rf"\.{re.escape(name)}\s*\(", text)
    if not m:
        return None
    depth, i = 1, m.end()
    while i < len(text) and depth:
        depth += {"(": 1, ")": -1}.get(text[i], 0)
        i += 1
    return split_top(text[m.end(): i - 1], (",",))


def substitute(expr: str, params: list[str], args: list[str]) -> str:
    """``expr`` with each parameter name replaced by the argument text the call passes for it."""
    out = expr
    for p, a in zip(params, args):
        a = a.strip()
        rep = a if re.fullmatch(r"[\w.]+(\([^()]*\))?(\.[\w]+(\([^()]*\))?)*", a) else f"({a})"
        out = re.sub(rf"(?<![\w.]){re.escape(p)}\b", lambda _m, r=rep: r, out)
    return out


def alpha(text: str, params: list[str]) -> str:
    """``text`` with the parameter names replaced by their positions, so a renamed parameter compares equal."""
    out = text
    for i, p in enumerate(params):
        if p in ("self", "cls", "this"):
            continue
        out = re.sub(rf"(?<![\w.]){re.escape(p)}\b", f"$p{i}", out)
    return out


def py_params(fn: ast.AST) -> dict:
    """The parameter shape of a def: positional names, how many are required, keyword-only names, * / **."""
    a = fn.args  # type: ignore[attr-defined]
    pos = [p.arg for p in getattr(a, "posonlyargs", [])] + [p.arg for p in a.args]
    n_def = len(a.defaults)
    kwonly = [p.arg for p in a.kwonlyargs]
    kw_required = [p.arg for p, d in zip(a.kwonlyargs, a.kw_defaults) if d is None]
    return {"pos": pos, "required": len(pos) - n_def, "posonly": len(getattr(a, "posonlyargs", [])),
            "kwonly": kwonly, "kw_required": kw_required, "varargs": a.vararg is not None,
            "varkw": a.kwarg is not None,
            "decorated": bool(getattr(fn, "decorator_list", None))}


def py_arity_problem(params: dict, call: ast.Call, *, bound: bool) -> str | None:
    """Why ``call`` cannot bind to a def with ``params`` (None when it can, or when it cannot be told:
    ``*args`` / ``**kwargs`` at the call site). ``bound``: called on an instance or class (``self`` / ``cls``
    is supplied)."""
    if any(isinstance(x, ast.Starred) for x in call.args) or any(k.arg is None for k in call.keywords):
        return None
    pos = list(params["pos"])
    required = params["required"]
    if bound and pos and pos[0] in ("self", "cls"):
        pos, required = pos[1:], max(0, required - 1)
    n_pos = len(call.args)
    kws = [k.arg for k in call.keywords]
    if n_pos > len(pos) and not params["varargs"]:
        return f"{n_pos} positional argument(s) given, at most {len(pos)} accepted"
    filled = set(pos[:n_pos])
    for k in kws:
        if k in filled:
            return f"argument {k!r} given twice"
        if k not in pos[params["posonly"]:] and k not in params["kwonly"] and not params["varkw"]:
            return f"unexpected keyword argument {k!r}"
        filled.add(k)
    missing = [p for p in pos[:required] if p not in filled]
    missing += [k for k in params["kw_required"] if k not in filled]
    if missing:
        return f"missing required argument(s): {', '.join(missing)}"
    return None


# -- tree-sitter helpers --------------------------------------------------------------------------------

_TS_IF = {"if_statement", "if_expression"}
_TS_EXIT = {"return_statement", "throw_statement", "break_statement", "continue_statement", "return_expression",
            "throw_expression", "jump_expression", "break_expression", "continue_expression"}
TS_LOOPS = {"for_statement", "enhanced_for_statement", "while_statement", "do_statement", "do_while_statement",
            "for_in_statement", "for_of_statement", "for_expression", "while_expression", "loop_expression",
            "for_range_loop"}
_TS_STMT_WRAP = {"block", "statements", "control_structure_body", "compound_statement", "statement_block"}


def ts_tree(text: str, suffix: str):
    from verinoda.anchors import _ts_parser

    parser = _ts_parser(suffix)
    if parser is None:
        return None
    try:
        return parser.parse(text.encode("utf-8", "surrogatepass"))
    except Exception:  # noqa: BLE001 - a grammar that refuses the text: no syntax rules for this file
        return None


def ts_walk(node):
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(reversed(n.children))


def _ts_text(node) -> str:
    return " ".join(node.text.decode("utf-8", "replace").split())


def _ts_if_parts(node):
    cond = node.child_by_field_name("condition")
    cons = node.child_by_field_name("consequence")
    kids = node.children
    if cond is None or cons is None:
        opens = [i for i, c in enumerate(kids) if c.type == "("]
        closes = [i for i, c in enumerate(kids) if c.type == ")"]
        if opens and closes:
            inner = [c for c in kids[opens[0] + 1:closes[0]] if c.is_named]
            after = [c for c in kids[closes[0] + 1:] if c.is_named]
            cond = cond or (inner[0] if inner else None)
            cons = cons or (after[0] if after else None)
    return cond, cons


_TS_FIXED = {"null_literal", "true", "false", "decimal_integer_literal", "integer_literal", "string_literal", "string",
             "field_access", "identifier", "simple_identifier", "scoped_identifier", "character_literal",
             "number_literal", "boolean_literal", "float_literal", "decimal_floating_point_literal",
             "navigation_expression", "member_expression", "null", "nil", "none", "line_string_literal"}


def _ts_exits(node) -> bool:
    """Does ``node`` (a statement or a block) end in an exit that stops the work: throw / break / continue, or
    a return of nothing or of a fixed value (a literal, a name, a constant field) - not a computed result."""
    if node is None:
        return False
    if node.type in _TS_EXIT:
        if "return" not in node.type and not (node.type == "jump_expression" and node.text.startswith(b"return")):
            return True
        vals = [c for c in node.children if c.is_named and "comment" not in c.type]
        return not vals or (len(vals) == 1 and vals[0].type in _TS_FIXED)
    if node.type in _TS_STMT_WRAP:
        named = [c for c in node.children if c.is_named and "comment" not in c.type]
        if named:
            return _ts_exits(named[-1])
    if node.type == "expression_statement":
        named = [c for c in node.children if c.is_named]
        return bool(named) and _ts_exits(named[0])
    return False


def ts_guards(tree, lo: int, hi: int) -> list[tuple[str, int]]:
    """``(condition, line)`` of ``if`` statements on lines ``lo..hi`` whose body exits (return / throw /
    break / continue)."""
    out = []
    if tree is None:
        return out
    for n in ts_walk(tree.root_node):
        line = n.start_point[0] + 1
        if n.type in _TS_IF and lo <= line <= hi:
            cond, cons = _ts_if_parts(n)
            if cond is not None and _ts_exits(cons):
                c = _ts_text(cond)
                if c.startswith("(") and c.endswith(")"):
                    c = c[1:-1].strip()
                out.append((c, line))
    return out


def ts_conditions(tree, lo: int, hi: int) -> list[tuple[str, int]]:
    """``(condition, line)`` of every ``if`` / ``while`` on lines ``lo..hi`` (whatever its body does)."""
    out = []
    if tree is None:
        return out
    for n in ts_walk(tree.root_node):
        line = n.start_point[0] + 1
        if lo <= line <= hi and (n.type in _TS_IF or n.type in ("while_statement", "while_expression",
                                                                  "do_statement", "do_while_statement")):
            cond = _ts_if_parts(n)[0] if n.type in _TS_IF else n.child_by_field_name("condition")
            if cond is not None:
                c = _ts_text(cond)
                out.append((c[1:-1].strip() if c.startswith("(") and c.endswith(")") else c, line))
    return out


def _ts_def_at(tree, def_line: int):
    from verinoda.anchors import TS_DEF_TYPES

    for n in ts_walk(tree.root_node) if tree is not None else ():
        if n.type in TS_DEF_TYPES and n.start_point[0] + 1 == def_line:
            return n
    return None


def ts_param_names(tree, def_line: int) -> list[str]:
    """Parameter names of the Java / Kotlin / JS method declared on ``def_line``."""
    n = _ts_def_at(tree, def_line)
    out: list[str] = []
    if n is None:
        return out
    for c in n.children:
        if c.type in ("formal_parameters", "function_value_parameters", "parameters", "parameter_list"):
            for p in c.children:
                if not p.is_named or "comment" in p.type:
                    continue
                nm = p.child_by_field_name("name") or p.child_by_field_name("pattern") or next(
                    (x for x in p.children if x.type in ("identifier", "simple_identifier")), None)
                if nm is not None:
                    out.append(nm.text.decode("utf-8", "replace"))
            break
    return out


def ts_single_return(tree, def_line: int) -> tuple[list[str], str] | None:
    """``(parameters, returned expression text)`` of a method whose body is one ``return <expr>`` (Java,
    Kotlin block body or expression body, JS / TS), else None."""
    n = _ts_def_at(tree, def_line)
    if n is None:
        return None
    body = n.child_by_field_name("body") or next((c for c in n.children if c.type in _TS_BODY), None)
    if body is None:
        return None
    stmts = [c for c in body.children if c.is_named and "comment" not in c.type]
    if len(stmts) == 1 and stmts[0].type == "statements":
        stmts = [c for c in stmts[0].children if c.is_named and "comment" not in c.type]
    if len(stmts) != 1:
        return None
    s = stmts[0]
    if s.type in ("return_statement", "jump_expression") and s.text.lstrip().startswith(b"return"):
        vals = [c for c in s.children if c.is_named and "comment" not in c.type]
        if len(vals) == 1:
            return ts_param_names(tree, def_line), _ts_text(vals[0])
    return None


def ts_loops(tree, lo: int, hi: int) -> list[tuple[int, int, str]]:
    """``(first line, last line, header text)`` of loops on lines ``lo..hi``."""
    out = []
    if tree is None:
        return out
    for n in ts_walk(tree.root_node):
        line = n.start_point[0] + 1
        if n.type in TS_LOOPS and lo <= line <= hi:
            body = n.child_by_field_name("body")
            head = n.text[: (body.start_byte - n.start_byte) if body is not None else 160].decode("utf-8", "replace")
            out.append((line, n.end_point[0] + 1, " ".join(head.split())[:160]))
    return out


_TS_BODY = {"function_body", "block", "class_body", "constructor_body", "enum_body", "interface_body",
            "declaration_list", "field_declaration_list", "compound_statement", "statement_block", "object_body",
            "body_statement", "enum_class_body"}


def ts_header(tree, def_line: int) -> str | None:
    """The text of the definition starting on ``def_line`` before its body (comments dropped, whitespace
    collapsed): what a caller sees. None when no definition starts there or it has no body."""
    if tree is None:
        return None
    from verinoda.anchors import TS_DEF_TYPES

    for n in ts_walk(tree.root_node):
        if n.type in TS_DEF_TYPES and n.start_point[0] + 1 == def_line:
            body = n.child_by_field_name("body") or next((c for c in n.children if c.type in _TS_BODY), None)
            if body is None:
                return None
            parts = []
            for c in ts_walk(n):
                if c.start_byte >= body.start_byte:
                    continue
                if c.child_count == 0 and "comment" not in c.type:
                    parts.append(c.text.decode("utf-8", "replace"))
            return " ".join(parts)
    return None


def ts_param_count(tree, def_line: int) -> tuple[int, int, bool] | None:
    """``(required, total, varargs)`` parameters of the Java/Kotlin method declared on ``def_line``."""
    if tree is None:
        return None
    for n in ts_walk(tree.root_node):
        if n.type in ("method_declaration", "function_declaration", "constructor_declaration") and \
                n.start_point[0] + 1 == def_line:
            for c in n.children:
                if c.type == "formal_parameters":
                    ps = [p for p in c.children if p.type in ("formal_parameter", "spread_parameter")]
                    return len([p for p in ps if p.type == "formal_parameter"]), len(ps), \
                        any(p.type == "spread_parameter" for p in ps)
                if c.type == "function_value_parameters":
                    kids = c.children
                    total, required = 0, 0
                    for i, p in enumerate(kids):
                        if p.type == "parameter":
                            total += 1
                            if not (i + 1 < len(kids) and kids[i + 1].type == "="):
                                required += 1
                    vararg = any("vararg" in _ts_text(p) for p in kids if p.type == "parameter_modifiers")
                    return required, total, vararg
    return None


def ts_call_arg_counts(tree, name: str, line: int) -> list[int]:
    """Argument counts of the calls to ``name`` on ``line`` (Java ``method_invocation``, Kotlin
    ``call_expression``)."""
    out = []
    if tree is None:
        return out
    for n in ts_walk(tree.root_node):
        if n.start_point[0] + 1 > line or n.end_point[0] + 1 < line:
            continue
        if n.type == "method_invocation":
            nm = n.child_by_field_name("name")
            args = n.child_by_field_name("arguments")
            if nm is not None and nm.text.decode() == name and args is not None and nm.start_point[0] + 1 == line:
                out.append(len([a for a in args.children if a.is_named and "comment" not in a.type]))
        elif n.type == "call_expression":
            kids = n.children
            callee = kids[0] if kids else None
            ident = None
            if callee is not None:
                ident = callee if callee.type in ("identifier", "simple_identifier") else next(
                    (c for c in reversed(callee.children) if c.type in ("identifier", "simple_identifier")), None)
            suffix = next((c for c in kids if c.type in ("call_suffix", "value_arguments")), None)
            if ident is not None and ident.text.decode() == name and suffix is not None and \
                    ident.start_point[0] + 1 == line:
                va = suffix if suffix.type == "value_arguments" else next(
                    (c for c in suffix.children if c.type == "value_arguments"), None)
                if va is not None:
                    out.append(len([a for a in va.children if a.type == "value_argument"]))
    return out


# -- config keys ----------------------------------------------------------------------------------------

_YML_KEY = re.compile(r"^(\s*)([A-Za-z_][\w.\-]*)\s*:(?!:)")
_TOML_KEY = re.compile(r"^\s*([A-Za-z_][\w.\-]*)\s*=")
_TOML_SECTION = re.compile(r"^\s*\[\[?\s*([^\]]+?)\s*\]\]?\s*$")
_JSON_KEY = re.compile(r"^\s*\"([^\"]+)\"\s*:")
_PROP_KEY = re.compile(r"^\s*([A-Za-z_][\w.\-]*)\s*[=:]")


def config_keys(rel: str, lines: list[str]) -> list[tuple[int, str]]:
    """``(line, dotted key path)`` of the keys a config file defines, by line (yml by indentation, toml and ini
    by section, json by nesting depth of quoted keys, properties / .env flat)."""
    suffix = "." + rel.rsplit(".", 1)[-1].lower() if "." in rel.rsplit("/", 1)[-1] else ""
    out: list[tuple[int, str]] = []
    if suffix in (".yml", ".yaml"):
        stack: list[tuple[int, str]] = []
        for i, ln in enumerate(lines, 1):
            if not ln.strip() or ln.lstrip().startswith("#"):
                continue
            m = _YML_KEY.match(ln)
            if not m:
                continue
            ind = len(m.group(1))
            while stack and stack[-1][0] >= ind:
                stack.pop()
            stack.append((ind, m.group(2)))
            out.append((i, ".".join(k for _, k in stack)))
    elif suffix in (".toml", ".ini", ".cfg", ".conf"):
        section = ""
        for i, ln in enumerate(lines, 1):
            s = ln.strip()
            if not s or s.startswith(("#", ";")):
                continue
            m = _TOML_SECTION.match(s)
            if m:
                section = m.group(1).strip().strip("\"'")
                continue
            m = _TOML_KEY.match(ln) or (_PROP_KEY.match(ln) if suffix != ".toml" else None)
            if m:
                out.append((i, f"{section}.{m.group(1)}" if section else m.group(1)))
    elif suffix == ".json":
        depth_keys: list[str] = []
        depth = 0
        for i, ln in enumerate(lines, 1):
            m = _JSON_KEY.match(ln)
            if m:   # a key of the top-level object is at depth 1: its path has one part
                depth_keys = depth_keys[:max(depth - 1, 0)] + [m.group(1)]
                out.append((i, ".".join(depth_keys)))
            depth += ln.count("{") - ln.count("}")
            depth = max(depth, 0)
    else:
        for i, ln in enumerate(lines, 1):
            m = _PROP_KEY.match(ln)
            if m and not ln.lstrip().startswith(("#", "!")):
                out.append((i, m.group(1)))
    return out
