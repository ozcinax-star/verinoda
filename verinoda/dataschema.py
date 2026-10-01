"""Database tables, ORM models, migrations and dependency injection as graph nodes and edges.

Read from the code, never from a running service:

- **tables** - SQLAlchemy declarative classes (``__tablename__``), Flask-SQLAlchemy ``db.Model`` classes (the class
  name in snake_case), ``Table("name", ...)``, SQLModel classes with ``table=True``, Django models (``<app>_<model>``
  or ``Meta.db_table``; a model extending a project's abstract or concrete model included), JPA ``@Entity`` classes
  (``@Table(name=)``, else the entity name in snake_case, Spring Boot's default naming), ``CREATE TABLE`` in ``.sql``
  files and in SQL strings, Alembic ``op.create_table`` / ``op.add_column`` and Django ``migrations.CreateModel``;
- **reads and writes** - SQL strings in a function (``INSERT INTO``, ``UPDATE``, ``DELETE FROM`` write; ``SELECT
  ... FROM`` / ``JOIN`` read), a SQL constant a function names, JPQL in ``@Query`` (entity names mapped to their
  tables), ORM calls on a model class or an instance of one (``Order.objects.filter``, ``order.save()``), a model
  handed to a session (``session.query(Order)``, ``session.add(order)``, ``select(Order)``) and Spring Data
  repositories (``orderRepository.save(...)`` on a ``JpaRepository<Order, ...>``);
- **injection** - FastAPI ``Depends(provider)`` (a parameter, a route decorator's ``dependencies=[...]``, an
  ``Annotated[..., Depends(provider)]`` alias used as a parameter type) and Spring constructor / ``@Autowired`` /
  Lombok ``final`` field dependencies on a project class.

Each table becomes a node (``file_type: schema``, ``kind: table``, id ``table:<name>``) and each use an edge:
``maps_to`` (model class -> table), ``writes_table`` / ``reads_table`` (function -> table), ``migrates`` (migration
function -> table), ``injects`` (consumer -> provider). Edges are INFERRED (``derived_by: verinoda.dataschema``)
except a model's mapping declared by name (``__tablename__``, ``@Table(name=)``, ``db_table``), which is EXTRACTED
and cited at the line that names the table. A table name the code does not spell (a framework default) says how
it was derived.

The live database is opt-in and read only: :func:`live_sqlite` reads a local SQLite file the user names (opened
``immutable``, or a temporary copy when it has a write-ahead log, so nothing is written beside it) and lists its
tables and columns for :func:`report` to compare with the code.
"""

from __future__ import annotations

import ast
import hashlib
import re
from pathlib import Path

ORIGIN = "verinoda.dataschema"
FACTS_VERSION = 2
NODE_PREFIX = "table:"
RELATIONS = ("maps_to", "writes_table", "reads_table", "migrates", "injects")
CODE_SUFFIXES = (".py", ".java", ".kt", ".sql")

_SQL_START = re.compile(r"^\s*(SELECT|INSERT|UPDATE|DELETE|WITH|CREATE|REPLACE|MERGE|ALTER|DROP)\b", re.I)
# the shape of a statement, not only its first word ("Merge a class ..." and "Update the index" are prose)
_SQL_SHAPE = re.compile(
    r"^\s*(?:SELECT\b[\s\S]*?\bFROM\b|INSERT\s+(?:OR\s+\w+\s+)?INTO\b|"
    r"UPDATE\s+(?:OR\s+\w+\s+)?\S+(?:\s+(?:AS\s+)?\w+)?\s+SET\b|"
    r"DELETE\s+FROM\b|WITH\s+(?:RECURSIVE\s+)?\w+\s*(?:\([^)]*\))?\s+AS\s*\(|"
    r"CREATE\s+(?:TEMP\w*\s+|UNIQUE\s+|VIRTUAL\s+)*(?:TABLE|INDEX|VIEW|TRIGGER)\b|REPLACE\s+INTO\b|MERGE\s+INTO\b|"
    r"ALTER\s+TABLE\b|DROP\s+(?:TABLE|INDEX|VIEW)\b)", re.I)
_OPEN, _CLOSE = r"[`\"\[]?", r"[`\"\]]?"
# an optional schema (and database) prefix, each part quoted or not: `"public"."invoices"`, `[dbo].[Customers]`
_NAME = (r"(?:" + _OPEN + r"(?P<s>[A-Za-z_][\w$]*)" + _CLOSE + r"\s*\.\s*){0,2}"
         + _OPEN + r"(?P<n>[A-Za-z_][\w$]*)" + _CLOSE + r"(?![\w$])")
_SQL_PATTERNS = [
    (re.compile(r"\bCREATE\s+(?:TEMP(?:ORARY)?\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?" + _NAME, re.I), "create"),
    (re.compile(r"\bALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?" + _NAME, re.I), "migrate"),
    (re.compile(r"\bDROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?" + _NAME, re.I), "migrate"),
    (re.compile(r"\bINSERT\s+(?:OR\s+\w+\s+)?INTO\s+" + _NAME, re.I), "write"),
    (re.compile(r"\bREPLACE\s+INTO\s+" + _NAME, re.I), "write"),
    (re.compile(r"\bMERGE\s+INTO\s+" + _NAME, re.I), "write"),
    (re.compile(r"\bUPDATE\s+(?:OR\s+\w+\s+)?" + _NAME + r"(?:\s+(?:AS\s+)?(?!SET\b)\w+)?\s+SET\b", re.I),
     "write"),   # `UPDATE orders o SET`, JPQL `UPDATE Order o SET`
    (re.compile(r"\bDELETE\s+FROM\s+" + _NAME, re.I), "write"),
    (re.compile(r"\b(?P<kw>FROM|JOIN)\s+" + _NAME, re.I), "read"),
]
_SQL_NOT_TABLES = {"select", "where", "lateral", "unnest", "values", "dual", "set", "only", "if", "not",
                   "exists", "on", "as", "table", "index", "into", "or", "and"}
_SYSTEM_SCHEMAS = {"information_schema", "pg_catalog", "sys", "performance_schema", "mysql"}
_COLUMN_SKIP = {"primary", "foreign", "unique", "constraint", "check", "key", "index", "exclude", "fulltext",
                "spatial"}
# SQL text with its comments blanked and its string literals emptied (positions and line breaks kept)
# (a backslash escapes inside a literal, as MySQL and Postgres E'' strings allow: 'O\'Brien')
_SQL_LEXEME = re.compile(r"'(?:[^'\\]|\\.|'')*'?|--[^\n]*|/\*[\s\S]*?(?:\*/|\Z)")
_SQL_LEXEME_HASH = re.compile(r"'(?:[^'\\]|\\.|'')*'?|--[^\n]*|/\*[\s\S]*?(?:\*/|\Z)|(?m:^[ \t]*#[^\n]*)")
# a literal that runs over a line opening a statement has lost its closing quote: the text is lexed line by line
_RUNAWAY = re.compile(r"\n[ \t]*(?:CREATE|ALTER|DROP|INSERT)\b", re.I)
_CTE = re.compile(r"(?:\bWITH\s+(?:RECURSIVE\s+)?|,\s*)" + _OPEN + r"(\w+)" + _CLOSE
                  + r"\s*(?:\([^)]*\))?\s+AS\s*(?:NOT\s+)?(?:MATERIALIZED\s+)?\(", re.I)
_SELECT = re.compile(r"\bSELECT\b", re.I)
_LEADING_COMMENTS = re.compile(r"^(?:\s*(?:--[^\n]*|/\*[\s\S]*?\*/))+")

WRITE_METHODS = {"add", "add_all", "create", "bulk_create", "update", "delete", "save", "merge", "insert",
                 "bulk_update", "update_or_create", "get_or_create", "remove", "bulk_insert_mappings",
                 "bulk_save_objects", "saveAll", "saveAndFlush", "deleteAll", "deleteById", "persist"}
READ_METHODS = {"query", "filter", "filter_by", "get", "all", "first", "one", "select", "exclude", "values",
                "values_list", "scalars", "count", "exists", "get_object_or_404", "aggregate", "annotate",
                "order_by", "scalar", "one_or_none", "find", "find_one", "find_all", "findAll", "findById",
                "existsById", "getById", "getReferenceById", "in_bulk", "iterator", "latest", "earliest"}
# a model in the arguments counts only on a session-like receiver, or in these calls of their own
# (SQLAlchemy's select(Order), Django's get_object_or_404(Order, ...))
_BARE_MODEL_CALLS = {"select", "insert", "update", "delete", "get_object_or_404", "get_list_or_404"}
DJANGO_FIELD_FK = ("ForeignKey", "OneToOneField")
_DJANGO_NOT_COLUMNS = ("ManyToManyField", "GenericRelation", "GenericForeignKey")
_SPRING_STEREOTYPES = ("Service", "Component", "Repository", "Controller", "RestController", "Configuration")


def snake(name: str) -> str:
    """Spring Boot's physical naming (``CamelCaseToUnderscoresNamingStrategy``): ``OrderLine`` -> ``order_line``."""
    s = re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", name)
    return s.lower()


def flask_snake(name: str) -> str:
    """Flask-SQLAlchemy's default ``__tablename__``: ``OrderLine`` -> ``order_line``, ``HTTPLog`` -> ``http_log``."""
    return re.sub(r"((?<=[a-z0-9])[A-Z]|(?!^)[A-Z](?=[a-z]))", r"_\1", name).lower()


def _norm(name: str) -> str:
    """A table name as compared: unquoted, without a schema prefix, lower case."""
    return name.strip('`"[]').rsplit(".", 1)[-1].strip('`"[]').lower()


# -- SQL text ---------------------------------------------------------------------------------------

def _blank_sql(text: str, hash_comments: bool = False) -> str:
    """``text`` with its comments blanked and its string literals emptied, same length and line breaks
    (``hash_comments``: a line starting with ``#`` is a comment too, as in MySQL files)."""
    rx = _SQL_LEXEME_HASH if hash_comments else _SQL_LEXEME
    runaway = False

    def blank(m):
        nonlocal runaway
        tok = m.group()
        if tok.startswith("'"):
            if "\n" in tok and _RUNAWAY.search(tok):
                runaway = True
            return "'" + re.sub(r"[^\n]", " ", tok[1:-1]) + "'" if len(tok) > 1 else tok
        return re.sub(r"[^\n]", " ", tok)

    out = rx.sub(blank, text)
    if runaway and "\n" in text:   # a quote the patterns misread must not hide the rest of the file
        return "\n".join(rx.sub(lambda m: re.sub(r"[^']", " ", m.group()) if m.group().startswith("'")
                                else re.sub(r"[^\n]", " ", m.group()), line) for line in text.split("\n"))
    return out


def _in_call(text: str, positions: list[int]) -> set[int]:
    """The positions inside a function's parentheses that hold no ``SELECT`` before them (``EXTRACT(year FROM
    x)``, ``trim(both ' ' FROM name)``), as opposed to a subquery (``IN (SELECT id FROM t)``)."""
    want = sorted(set(positions))
    if not want:
        return set()
    out: set[int] = set()
    stack: list[int] = []
    k = 0
    for m in re.finditer(r"[()]", text):
        while k < len(want) and want[k] < m.start():
            p = want[k]
            if stack:
                o = stack[-1]
                if re.search(r"\w\s*$", text[max(0, o - 40):o]) and not _SELECT.search(text, o + 1, p):
                    out.add(p)
            k += 1
        if m.group() == "(":
            stack.append(m.start())
        elif stack:
            stack.pop()
    for p in want[k:]:
        if stack and re.search(r"\w\s*$", text[max(0, stack[-1] - 40):stack[-1]]) \
                and not _SELECT.search(text, stack[-1] + 1, p):
            out.add(p)
    return out


def sql_matches(text: str, hash_comments: bool = False) -> list[dict]:
    """Each table a SQL text names: ``{"name", "raw", "schema", "op", "pos"}`` (``op`` create, migrate, write or
    read; ``pos`` where the name starts). Comments and string literals are not read, a ``FROM`` inside a function
    call (``EXTRACT(year FROM d)``) is not a table, a CTE's name (``WITH recent AS (...)``) is not one, and a
    system catalog (``information_schema.tables``) is left out."""
    text = _blank_sql(text, hash_comments)
    ctes = {m.group(1).lower() for m in _CTE.finditer(text)} if re.search(r"\bWITH\b", text, re.I) else set()
    out: list[dict] = []
    claimed: list[tuple[int, int]] = []
    froms: list[int] = []
    for rx, op in _SQL_PATTERNS:
        for m in rx.finditer(text):
            s, e = m.span("n")
            if any(a <= s < b for a, b in claimed):   # `DELETE FROM t` is a write, not also a read
                continue
            name, schema = m.group("n").lower(), (m.group("s") or "").lower()
            if not name or name in _SQL_NOT_TABLES or schema in _SYSTEM_SCHEMAS or name.startswith("sqlite_"):
                continue
            if op == "read" and (name in ctes or re.search(r"\bDISTINCT\s+$", text[max(0, m.start() - 20):m.start()],
                                                             re.I)):   # a CTE, or `x IS DISTINCT FROM b`
                continue
            claimed.append((s, e))
            out.append({"name": name, "raw": m.group("n"), "schema": schema, "op": op, "pos": s,
                        "kw": m.start() if op == "read" and m.group("kw").upper() == "FROM" else None})
            if out[-1]["kw"] is not None:
                froms.append(m.start())
    inside = _in_call(text, froms)
    return [{k: v for k, v in x.items() if k != "kw"} for x in out if x["kw"] is None or x["kw"] not in inside]


def sql_tables(text: str) -> list[tuple[str, str]]:
    """``(table, op)`` for the tables a SQL text names: create, migrate, write or read (a table both read and
    written in one statement is listed for each)."""
    return [(x["name"], x["op"]) for x in sql_matches(text)]


def create_columns(text: str, table: str, hash_comments: bool = False) -> list[str]:
    """The column names of ``CREATE TABLE table (...)`` in ``text`` (constraints and comments left out)."""
    text = _blank_sql(text, hash_comments)
    m = re.search(r"\bCREATE\s+(?:TEMP(?:ORARY)?\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:" + _OPEN + r"\w+" + _CLOSE
                  + r"\s*\.\s*){0,2}" + _OPEN + re.escape(table) + _CLOSE + r"\s*\(", text, re.I)
    if not m:
        return []
    depth, i, start = 1, m.end(), m.end()
    while i < len(text) and depth:
        depth += {"(": 1, ")": -1}.get(text[i], 0)
        i += 1
    body = text[start:i - 1]
    cols, part, depth = [], [], 0
    for ch in body + ",":
        if ch == "," and depth == 0:
            words = "".join(part).strip().split()
            if words and words[0].strip('`"[]').lower() not in _COLUMN_SKIP:
                cols.append(words[0].strip('`"[]').lower())
            part = []
            continue
        depth += {"(": 1, ")": -1}.get(ch, 0)
        part.append(ch)
    return cols


# -- per-file facts ---------------------------------------------------------------------------------

_VALUE = "{}"   # what an f-string's value or a non-literal operand reads as: no SQL marker, never a table name


def _const_str(node) -> str | None:
    """A string a node spells: a constant, an f-string's literal parts (values as ``{}``), a ``+`` of those."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else _VALUE
                       for v in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        a, b = _const_str(node.left), _const_str(node.right)
        if a is not None or b is not None:
            return (a if a is not None else _VALUE) + (b if b is not None else _VALUE)
    return None


def _dotted(e) -> tuple[str, ...]:
    parts = []
    while isinstance(e, ast.Attribute):
        parts.append(e.attr)
        e = e.value
    if isinstance(e, ast.Name):
        parts.append(e.id)
    return tuple(reversed(parts))


def _kw(call: ast.Call, name: str):
    return next((k.value for k in call.keywords if k.arg == name), None)


def _django_app(rel: str) -> str | None:
    """The app label of a Django models module: the folder holding ``models.py`` or the ``models`` package (or
    the ``migrations`` package)."""
    parts = rel.split("/")
    if parts[-1] == "models.py" and len(parts) >= 2:
        return parts[-2]
    if "models" in parts[:-1]:
        i = parts.index("models")
        return parts[i - 1] if i >= 1 else None
    if "migrations" in parts[:-1]:
        i = parts.index("migrations")
        return parts[i - 1] if i >= 1 else None
    return None


def _module_of(rel: str) -> str:
    mod = rel[:-3] if rel.endswith(".py") else rel
    if mod.endswith("/__init__"):
        mod = mod[: -len("/__init__")]
    return mod.replace("/", ".")


def looks_like_sql(text: str) -> bool:
    """A string that reads as a SQL statement: it starts with a statement keyword written in capitals, or in any case
    with a SQL marker in it (a placeholder, ``=``, ``*``, ``WHERE``, ``VALUES``) - so "Select a file" is prose.
    Comments before the statement (``-- the schema``) do not count."""
    text = _LEADING_COMMENTS.sub("", text, count=1)
    m = _SQL_START.match(text)
    if not m or not _SQL_SHAPE.match(text):
        return False
    if m.group(1).isupper():
        return True
    if not re.search(r"\?|%s|%\(\w+\)s|(?<!:):\w|=|\*|\bwhere\b|\bvalues\b", text, re.I):
        return False
    # written in lower case, a statement has to go on as SQL after its table name: "delete from cache failed
    # for %s" and "Insert into queue failed key=%s" are messages
    return bool(_LOWER_SQL.match(text))


_TNAME = r"[`\"\[]?[A-Za-z_][\w$.]*[`\"\]]?"
_LOWER_SQL = re.compile(
    r"\s*(?:delete\s+from\s+" + _TNAME + r"(?:\s+(?:as\s+)?\w+)?\s*(?:\bwhere\b|\busing\b|\breturning\b|;|$)"
    r"|insert\s+(?:or\s+\w+\s+)?into\s+" + _TNAME + r"\s*(?:\(|\bvalues\b|\bselect\b|\bdefault\b)"
    r"|update\s+(?:or\s+\w+\s+)?" + _TNAME + r"(?:\s+(?:as\s+)?\w+)?\s+set\s+[\w.`\"\[\]]+\s*="
    r"|select\s[\s\S]*?\bfrom\s+" + _TNAME + r"(?:\s+(?:as\s+)?(?!(?:where|join|inner|left|right|full|cross|"
    r"natural|group|order|limit|offset|union|having|on)\b)\w+)?\s*(?:\b(?:where|join|inner|left|right|full|cross|"
    r"natural|group|order|limit|offset|union|having)\b|[;,)]|$)"
    r"|with\b|create\b|alter\b|drop\b|replace\b|merge\b)", re.I)

_DJANGO_IMPORT = re.compile(r"^\s*(?:from|import)\s+django\b", re.M)


def _py_facts(text: str, rel: str) -> dict:
    out: dict = {"tables": [], "models": [], "uses": [], "injects": []}
    _PyFacts(out, rel, django=bool(_DJANGO_IMPORT.search(text))).run(ast.parse(text))
    return {k: v for k, v in out.items() if v or k in ("tables", "models", "uses", "injects")}


_FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)
_LOG_METHODS = {"debug", "info", "warning", "warn", "error", "exception", "critical", "fatal", "log"}


def _message_call(n: ast.Call) -> bool:
    """A call whose strings are messages: logging (``log.info``, ``logger.warning``, ``logging.error``,
    ``self.log.debug``), ``warnings.warn``, ``print``."""
    chain = _dotted(n.func)
    if chain in (("print",), ("warn",)) or chain[-2:] == ("warnings", "warn"):
        return True
    return len(chain) >= 2 and chain[-1] in _LOG_METHODS and any(
        x.lower().strip("_") in ("log", "logger", "logging") or x.lower().endswith("logger") for x in chain[:-1])
_POP = object()   # a marker on the stack: leaving a function


def _session_like(chain: tuple[str, ...], instances: dict[str, str]) -> bool:
    """A receiver that is a database session: ``session``, ``db``, ``self.session``, ``db.session``, a name
    built from or typed as a ``...Session`` class."""
    if not chain:
        return False
    if any("session" in p.lower() for p in chain) or chain[0] in ("db", "database", "uow"):
        return True
    return instances.get(chain[0], "").endswith("Session")


class _PyFacts:
    """One pass over a module (an explicit stack, no visitor dispatch): models, ``Table()``, SQL strings and ORM /
    injection calls per function."""

    def __init__(self, out: dict, rel: str, django: bool = False):
        self.out, self.rel, self.app, self.django = out, rel, _django_app(rel), django
        self.funcs: list[tuple[ast.AST, dict[str, str]]] = []   # (function, name -> class it was built from)
        self.consts: dict[str, str] = {}   # a module constant holding a SQL statement -> its text
        self.class_consts: dict[str, dict[str, str]] = {}   # class -> its constants holding a SQL statement
        self._locals: dict[int, set[str]] = {}

    @staticmethod
    def _docstring(node):
        body = getattr(node, "body", None)
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                and isinstance(body[0].value.value, str):
            return body[0].value
        return None

    def _sql_constants(self, tree: ast.Module, skip: set[int]) -> None:
        """``SQL = "SELECT ..."`` at module or class level: its tables are defined there, its reads and writes
        belong to the functions that name it."""
        scopes = [(None, tree.body)] + [(n.name, n.body) for n in tree.body if isinstance(n, ast.ClassDef)]
        for owner, body in scopes:
            for s in body:
                if not (isinstance(s, (ast.Assign, ast.AnnAssign)) and s.value is not None):
                    continue
                targets = s.targets if isinstance(s, ast.Assign) else [s.target]
                if len(targets) != 1 or not isinstance(targets[0], ast.Name):
                    continue
                text = _const_str(s.value)
                if not text or not looks_like_sql(text):
                    continue
                if owner is None:
                    self.consts[targets[0].id] = text
                else:
                    self.class_consts.setdefault(owner, {})[targets[0].id] = text
                self._string(s.value, uses=False)
                for c in ast.walk(s.value):
                    skip.add(id(c))

    def run(self, tree: ast.Module) -> None:
        skip: set[int] = set()   # docstrings and the string parts of a concatenation already read
        doc = self._docstring(tree)
        if doc is not None:
            skip.add(id(doc))
        self._sql_constants(tree, skip)
        stack: list = [tree]
        while stack:
            n = stack.pop()
            if n is _POP:
                self.funcs.pop()
                continue
            t = type(n)
            if t in _FUNCS:
                doc = self._docstring(n)
                if doc is not None:
                    skip.add(id(doc))
                self.funcs.append((n, self._typed_params(n)))
                stack.append(_POP)
            elif t is ast.ClassDef:
                doc = self._docstring(n)
                if doc is not None:
                    skip.add(id(doc))
                _py_model(n, self.rel, self.app, self.out, self.django)
            elif t is ast.Assign:
                self._assign(n)
            elif t in (ast.With, ast.AsyncWith):
                if self.funcs:
                    for item in n.items:
                        if isinstance(item.optional_vars, ast.Name) and isinstance(item.context_expr, ast.Call):
                            f = _dotted(item.context_expr.func)
                            if f and f[-1][:1].isupper():
                                self.funcs[-1][1][item.optional_vars.id] = f[-1]
            elif t is ast.ImportFrom:
                self._import(n)
            elif t is ast.Call:
                if _message_call(n):   # a log line, a warning, a print: its text is a message, not SQL
                    self._skip_strings([*n.args, *(k.value for k in n.keywords)], skip)
                _py_call(n, self.funcs[-1] if self.funcs else (None, {}), self.out, self.app)
            elif t is ast.Raise:
                if isinstance(n.exc, ast.Call):   # raise ValueError("Insert into outbox failed ...")
                    self._skip_strings([*n.exc.args, *(k.value for k in n.exc.keywords)], skip)
            elif t is ast.Name:
                # a module constant named in a function - not a local of the same name
                if self.funcs and n.id in self.consts and isinstance(n.ctx, ast.Load) \
                        and n.id not in self._local_names(self.funcs[-1][0]):
                    self._sql(self.consts[n.id], n.lineno, defs=False, via=f"SQL constant {n.id}")
                continue
            elif t is ast.Attribute:
                # a class constant through self, cls or the class's own name - not any object's attribute
                if self.funcs and isinstance(n.ctx, ast.Load) and isinstance(n.value, ast.Name):
                    owner = n.value.id
                    pools = list(self.class_consts.values()) if owner in ("self", "cls") else \
                        [self.class_consts.get(owner, {})]
                    text = next((c[n.attr] for c in pools if n.attr in c), None)
                    if text:
                        self._sql(text, n.lineno, defs=False, via=f"SQL constant {n.attr}")
            elif t is ast.Constant:
                if type(n.value) is str and id(n) not in skip:
                    self._string(n)
                continue
            elif t is ast.JoinedStr or (t is ast.BinOp and type(n.op) is ast.Add):
                if id(n) not in skip and (t is ast.JoinedStr or _const_str(n) is not None):
                    self._string(n)
                    for c in ast.walk(n):   # its parts are this one string
                        if type(c) in (ast.Constant, ast.JoinedStr, ast.BinOp):
                            skip.add(id(c))
            stack.extend(reversed(list(ast.iter_child_nodes(n))))

    @staticmethod
    def _skip_strings(nodes, skip: set[int]) -> None:
        for a in nodes:
            for c in ast.walk(a):
                if type(c) in (ast.Constant, ast.JoinedStr, ast.BinOp):
                    skip.add(id(c))

    def _local_names(self, fn) -> set[str]:
        """The names a function binds itself (its parameters and assignments): they hide a module constant."""
        got = self._locals.get(id(fn))
        if got is None:
            a = fn.args
            got = {x.arg for x in [*a.posonlyargs, *a.args, *a.kwonlyargs, *([a.vararg] if a.vararg else []),
                                   *([a.kwarg] if a.kwarg else [])]}
            got |= {c.id for c in ast.walk(fn) if isinstance(c, ast.Name) and isinstance(c.ctx, ast.Store)}
            self._locals[id(fn)] = got
        return got

    def _typed_params(self, fn) -> dict[str, str]:
        """Parameters typed with a class name (``session: Session``), and those types recorded for the
        ``Annotated`` aliases a dependency may hide behind (``session: SessionDep``)."""
        typed: dict[str, str] = {}
        a = fn.args
        for p in [*a.posonlyargs, *a.args, *a.kwonlyargs]:
            ann = _dotted(p.annotation) if p.annotation is not None else ()
            if ann and ann[-1][:1].isupper():
                typed[p.arg] = ann[-1]
                self.out.setdefault("annots", []).append({"line": fn.lineno, "name": ann[-1]})
        return typed

    def _import(self, node: ast.ImportFrom) -> None:
        mod = node.module or ""
        if node.level:
            pkg = _module_of(self.rel).split(".")
            if not self.rel.endswith("/__init__.py"):
                pkg = pkg[:-1]
            pkg = pkg[: len(pkg) - (node.level - 1)] if node.level > 1 else pkg
            mod = ".".join([*pkg, *([mod] if mod else [])])
        if not mod:
            return
        imports = self.out.setdefault("imports", {})
        for a in node.names:
            if a.name != "*" and a.name[:1].isupper():
                imports[a.asname or a.name] = f"{mod}.{a.name}"

    def _assign(self, node: ast.Assign) -> None:
        v = node.value
        if isinstance(v, ast.Subscript) and _dotted(v.value)[-1:] == ("Annotated",) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):   # SessionDep = Annotated[Session, Depends(get_session)]
            parts = v.slice.elts if isinstance(v.slice, ast.Tuple) else [v.slice]
            for p in parts[1:]:
                if isinstance(p, ast.Call) and _dotted(p.func)[-1:] in (("Depends",), ("Security",)) and p.args:
                    target = _dotted(p.args[0])
                    if target:
                        self.out.setdefault("dep_aliases", []).append(
                            {"name": node.targets[0].id, "target": target[-1], "line": node.lineno,
                             "via": f"FastAPI {_dotted(p.func)[-1]}() through {node.targets[0].id}"})
            return
        if not isinstance(v, ast.Call):
            return
        f = _dotted(v.func)
        if f[-1:] == ("Table",) and v.args:
            name = _const_str(v.args[0])
            target = node.targets[0]
            if name and isinstance(target, ast.Name):
                cols = [c for c in (_const_str(a.args[0]) if isinstance(a, ast.Call) and a.args else None
                                    for a in v.args[1:]) if c]
                self.out["tables"].append({"name": _norm(name), "line": node.lineno, "via": "sqlalchemy Table()",
                                           "columns": [c.lower() for c in cols], "declared": True})
                self.out["models"].append({"class": target.id, "line": node.lineno, "table": _norm(name),
                                           "declared": True})
        elif f and f[-1][:1].isupper() and self.funcs:
            for t in node.targets:
                if isinstance(t, ast.Name):
                    self.funcs[-1][1][t.id] = f[-1]

    def _string(self, node, uses: bool = True) -> None:
        s = _const_str(node)
        if not s or not looks_like_sql(s):
            return
        # a plain literal spans as many lines as its value: each name is cited at its own line
        exact = isinstance(node, ast.Constant) and getattr(node, "end_lineno", None) is not None \
            and node.end_lineno - node.lineno == s.count("\n")
        self._sql(s, node.lineno, uses=uses, exact=exact)

    def _sql(self, s: str, line: int, *, defs: bool = True, uses: bool = True, via: str = "SQL string",
             exact: bool = False) -> None:
        for x in sql_matches(s):
            table, op = x["name"], x["op"]
            at = line + s.count("\n", 0, x["pos"]) if exact else line
            if op == "create" and defs:
                self.out["tables"].append({"name": table, "line": at, "via": "CREATE TABLE in a string",
                                           "columns": create_columns(s, table), "declared": True})
            if uses:
                self.out["uses"].append({"line": at, "table": table, "op": op, "via": via})


def _field_columns(body: list, sqlmodel: bool, django: bool) -> tuple[list[str], bool, list[dict]]:
    """The columns a model's fields make, whether one of them is the primary key, and the Django many-to-many
    fields that make a join table of their own (no ``through=`` model)."""
    cols, pk, m2m = [], False, []
    for s in body:
        target = s.target if isinstance(s, ast.AnnAssign) else (s.targets[0] if isinstance(s, ast.Assign) else None)
        value = s.value if isinstance(s, (ast.Assign, ast.AnnAssign)) else None
        if not isinstance(target, ast.Name) or target.id.startswith("__") or not isinstance(value, ast.Call):
            continue
        fn = _dotted(value.func)[-1:] or ("",)
        if fn[0] in _DJANGO_NOT_COLUMNS:   # a many-to-many field is a join table, not a column
            if django and fn[0] == "ManyToManyField" and value.args and _kw(value, "through") is None \
                    and _kw(value, "db_table") is None:
                a = value.args[0]
                other = _const_str(a) or (_dotted(a)[-1] if _dotted(a) else None)
                if other:
                    m2m.append({"field": target.id, "target": other.rpartition(".")[2]})
            continue
        if fn[0] in ("Column", "mapped_column", "Field") or fn[0].endswith("Field") or fn[0] in DJANGO_FIELD_FK:
            if fn[0] == "Field" and not sqlmodel:
                continue
            explicit = _const_str(value.args[0]) if value.args and fn[0] in ("Column", "mapped_column") else None
            explicit = explicit or _const_str(_kw(value, "db_column"))
            col = explicit or (target.id + "_id" if fn[0] in DJANGO_FIELD_FK else target.id)
            cols.append(col.lower())
            pk_kw = _kw(value, "primary_key")
            if isinstance(pk_kw, ast.Constant) and pk_kw.value is True:
                pk = True
    return cols, pk, m2m


def _py_model(cls: ast.ClassDef, rel: str, app: str | None, out: dict, django_file: bool = False) -> None:
    body = cls.body
    tn = next((s for s in body if isinstance(s, ast.Assign)
               and any(isinstance(t, ast.Name) and t.id == "__tablename__" for t in s.targets)), None)
    tablename = _const_str(tn.value) if tn is not None else None
    bases = [_dotted(b) for b in cls.bases]
    sqlmodel = any(b[-1:] == ("SQLModel",) for b in bases) and any(
        k.arg == "table" and isinstance(k.value, ast.Constant) and k.value.value is True for k in cls.keywords)
    flask = any(b[-2:] == ("db", "Model") for b in bases)
    sa_abstract = any(isinstance(s, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__abstract__"
                                                        for t in s.targets)
                      and isinstance(s.value, ast.Constant) and s.value.value is True for s in body)
    meta = next((s for s in body if isinstance(s, ast.ClassDef) and s.name == "Meta"), None)
    db_table, db_line, abstract, proxy = None, None, False, False
    if meta is not None:
        for s in meta.body:
            if isinstance(s, ast.Assign) and isinstance(s.targets[0], ast.Name):
                if s.targets[0].id == "db_table":
                    db_table, db_line = _const_str(s.value), s.lineno
                true = isinstance(s.value, ast.Constant) and s.value.value is True
                if s.targets[0].id == "abstract" and true:
                    abstract = True
                if s.targets[0].id == "proxy" and true:   # the parent's table, none of its own
                    proxy = True
    name = declared = via = None
    line = cls.lineno
    if tablename:
        name, declared, via, line = tablename, True, "sqlalchemy __tablename__", tn.lineno
    elif sqlmodel:
        name, declared, via = cls.name.lower(), False, "SQLModel default: the class name in lower case"
    elif flask and not sa_abstract:
        name, declared, via = flask_snake(cls.name), False, "Flask-SQLAlchemy default: the class name in snake_case"
    if name:
        cols, _pk, _m2m = _field_columns(body, sqlmodel, False)
        out["tables"].append({"name": _norm(name), "line": line, "via": via, "columns": cols, "declared": declared})
        out["models"].append({"class": cls.name, "line": cls.lineno, "table": _norm(name), "declared": declared,
                              "decl_line": line})
        return
    if not bases or not (django_file or app):
        return
    # a Django model is told in linking: its base is `models.Model` in a file importing django, or a project
    # class that is one (an abstract base in another module)
    direct = django_file and any(b[-2:] == ("models", "Model") or b == ("Model",) for b in bases)
    cols, pk, m2m = _field_columns(body, False, True)
    out.setdefault("django", []).append({
        "class": cls.name, "line": cls.lineno, "bases": [b[-1] for b in bases if b], "direct": direct,
        "abstract": abstract, "app": app or "", "columns": cols, "pk": pk, **({"m2m": m2m} if m2m else {}),
        **({"proxy": True} if proxy else {}),
        **({"db_table": _norm(db_table), "decl_line": db_line} if db_table else {})})


def _py_call(n: ast.Call, ctx: tuple, out: dict, app: str | None = None) -> None:
    """One call in a function: an Alembic or Django migration step, an injection, an ORM read or write
    (``app``: the Django app of the file, for a migration's ``CreateModel``)."""
    func, instances = ctx
    chain = _dotted(n.func)
    method = chain[-1] if chain else (n.func.attr if isinstance(n.func, ast.Attribute) else None)
    fname = getattr(func, "name", None)
    if fname in ("upgrade", "downgrade") and chain[-2:] in (
            ("op", "create_table"), ("op", "add_column"), ("op", "drop_table"), ("op", "alter_column"),
            ("op", "drop_column"), ("op", "rename_table")) and n.args:   # Alembic
        table = _const_str(n.args[0])
        if table:
            cols = [c for c in (_const_str(a.args[0]) if isinstance(a, ast.Call) and a.args
                                and _dotted(a.func)[-1:] == ("Column",) else None for a in n.args[1:]) if c]
            if chain[-1] == "create_table":
                out["tables"].append({"name": _norm(table), "line": n.lineno, "via": "alembic op.create_table",
                                      "columns": [c.lower() for c in cols], "declared": True})
            out["uses"].append({"line": n.lineno, "table": _norm(table), "op": "migrate",
                                "via": f"alembic op.{chain[-1]}",
                                **({"columns": [c.lower() for c in cols]} if cols else {})})
        return
    if chain[-2:] == ("migrations", "CreateModel"):
        mname = _const_str(_kw(n, "name"))
        opts = _kw(n, "options")
        db_table = None
        if isinstance(opts, ast.Dict):
            for k, v in zip(opts.keys, opts.values):
                if _const_str(k) == "db_table":
                    db_table = _const_str(v)
        proxy = isinstance(opts, ast.Dict) and any(
            _const_str(k) == "proxy" and isinstance(v, ast.Constant) and v.value is True
            for k, v in zip(opts.keys, opts.values))
        if mname and not proxy:   # the table of the migration's own app (two apps may each have an `Item`)
            table = _norm(db_table) if db_table else (f"{app}_{mname.lower()}" if app else None)
            out["uses"].append({"line": n.lineno, "model": mname, "op": "migrate", "via": "django CreateModel",
                                **({"table": table} if table else {})})
        return
    if method in ("Depends", "Security"):
        target = _dotted(n.args[0]) if n.args else ()
        if target and func is not None:   # owned by the function: a parameter, its decorator or its body
            out["injects"].append({"line": n.lineno, "target": target[-1], "via": f"FastAPI {method}()",
                                   "owner_line": func.lineno})
        return
    if func is None:
        return
    op = "write" if method in WRITE_METHODS else "read" if method in READ_METHODS else None
    if op is None:
        return
    refs: set[str] = set()
    receiver = n.func.value if isinstance(n.func, ast.Attribute) else None
    base = receiver
    while isinstance(base, (ast.Attribute, ast.Call, ast.Subscript)):   # Order.objects.filter(...).first()
        base = base.func if isinstance(base, ast.Call) else base.value
    if isinstance(base, ast.Name):
        refs.add(instances.get(base.id, base.id))
    # a model in the arguments: only handed to a session (session.add(o), db.session.query(Order)) or to
    # select(Order) / get_object_or_404(Order) - a set's .add(o) or a dict's .get(Order) is no table use
    session = _session_like(_dotted(receiver), instances) if receiver is not None else (
        isinstance(n.func, ast.Name) and n.func.id in _BARE_MODEL_CALLS)
    if session:
        for a in [*n.args, *(k.value for k in n.keywords)]:
            if isinstance(a, ast.Name):
                refs.add(instances.get(a.id, a.id))
            elif isinstance(a, ast.Call):
                f = _dotted(a.func)
                if f and f[-1] in ("select", "insert", "update", "delete") and a.args \
                        and isinstance(a.args[0], ast.Name):
                    refs.add(a.args[0].id)   # session.execute(select(Order))
                    op = "write" if f[-1] in ("insert", "update", "delete") else op
                elif f and f[-1][:1].isupper():
                    refs.add(f[-1])         # session.add(Order(...))
    for r in sorted(refs):
        if r[:1].isupper() and not r.endswith("Session"):
            out["uses"].append({"line": n.lineno, "model": r, "op": op, "via": f"ORM .{method}()"})


# -- Java and Kotlin --------------------------------------------------------------------------------

# comments, text blocks / raw strings, strings and char literals, in the order they start
_JVM_LEXEME = re.compile(r'//[^\n]*|/\*[\s\S]*?(?:\*/|\Z)|"""[\s\S]*?(?:"""|\Z)|"(?:\\.|[^"\\\n])*"?|'
                         r"'(?:\\.|[^'\\\n])+'")
_JAVA_ENTITY = re.compile(r"@(?:[\w.]*\.)?Entity\b")
_JAVA_CLASS_HEAD = re.compile(r"(?:(?:public|protected|private|internal|data|open|abstract|final|sealed)\s+)*"
                              r"class\s+(\w+)")
_JAVA_REPO = re.compile(r"interface\s+(?P<repo>\w+)\s*(?:<[^>]*>)?\s*(?:extends|:)\s*[^{]*?\b(?:Jpa|Crud|"
                        r"PagingAndSorting|ListCrud|Mongo|Reactive)\w*Repository\s*<\s*(?P<entity>\w+)")
_JAVA_FIELD = re.compile(r"\b(?P<type>[A-Z]\w*)(?:<[^>]*>)?\s+(?P<var>[a-z]\w*)\s*[;,)=]")
_KOTLIN_FIELD = re.compile(r"\b(?:val|var)\s+(?P<var>[a-z]\w*)\s*:\s*(?P<type>[A-Z]\w*)")
_JAVA_CALL = re.compile(r"\b(?P<var>[a-z]\w*)\s*\.\s*(?P<m>\w+)\s*\(")
_JAVA_CLASS = re.compile(r"(?P<ann>(?:@\w+(?:\([^)]*\))?\s*)*)(?:public\s+|open\s+|final\s+|abstract\s+)*"
                         r"class\s+(?P<cls>\w+)")
_JVM_CONST = re.compile(r"(?:\bstatic\s+final|\bfinal\s+static|\bconst\s+val)\s+(?:String\s+)?(?P<name>\w+)\s*"
                        r"(?::\s*String\s*)?=\s*$")


def _jvm_scan(text: str) -> tuple[str, list[tuple[int, str]]]:
    """The code with comments and string contents blanked (same length, same line breaks), and each string
    literal outside a comment as ``(position of its content, content)`` (text blocks included)."""
    parts, strings, last = [], [], 0
    for m in _JVM_LEXEME.finditer(text):
        tok = m.group()
        parts.append(text[last:m.start()])
        last = m.end()
        if tok.startswith(("//", "/*")):
            parts.append(re.sub(r"[^\n]", " ", tok))
            continue
        if tok.startswith("'"):
            parts.append(" " * len(tok))
            continue
        q = 3 if tok.startswith('"""') else 1
        end = len(tok) - q if len(tok) >= 2 * q and tok.endswith('"' * q) else len(tok)
        strings.append((m.start() + q, tok[q:end]))
        parts.append(tok[:q] + re.sub(r"[^\n]", " ", tok[q:end]) + tok[end:])
    parts.append(text[last:])
    return "".join(parts), strings


def _closing(code: str, open_at: int) -> int:
    """The position of the parenthesis closing the one at ``open_at`` (the end of the text when none)."""
    depth = 0
    for i in range(open_at, len(code)):
        c = code[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
    return len(code)


def _entities(code: str):
    """``(start, (args start, args end) or None, class name, class name position)`` of each ``@Entity`` class:
    the annotations between ``@Entity`` and ``class`` are skipped by balanced parentheses (``@Table(name = "x",
    indexes = {@Index(...)})``)."""
    for m in _JAVA_ENTITY.finditer(code):
        i, eargs = m.end(), None
        j = len(code) - len(code[i:].lstrip())
        if code[j:j + 1] == "(":
            k = _closing(code, j)
            eargs, i = (j, k + 1), k + 1
        while True:
            j = len(code) - len(code[i:].lstrip())
            a = re.match(r"@[\w.]+", code[j:])
            if not a:
                break
            i = j + a.end()
            j = len(code) - len(code[i:].lstrip())
            if code[j:j + 1] == "(":
                i = _closing(code, j) + 1
        j = len(code) - len(code[i:].lstrip())
        c = _JAVA_CLASS_HEAD.match(code, j)
        if c:
            yield m.start(), eargs, c.group(1), c.start(1)


def _top_level_name(code: str, text: str, a: int, b: int) -> str | None:
    """The ``name = "..."`` argument of the annotation whose parentheses are ``a``..``b`` - not a nested
    ``@Index(name = ...)``."""
    depth = 0
    for i in range(a + 1, b):
        ch = code[i]
        if ch in "({":
            depth += 1
        elif ch in ")}":
            depth -= 1
        elif depth == 0 and ch == "n":
            m = re.compile(r"\bname\s*=\s*\"([^\"]*)\"").match(text, i)
            if m and (i == 0 or not (text[i - 1].isalnum() or text[i - 1] == "_")):
                return m.group(1)
    return None


def _jpql_context(code: str, pos: int) -> bool | None:
    """True when the string at ``pos`` is JPQL (an ``@Query`` without ``nativeQuery = true``, a ``createQuery``
    argument), False when it is SQL by declaration (``nativeQuery = true``, ``createNativeQuery``), None when
    nothing says. The enclosing call is found by parenthesis depth, however long its arguments."""
    depth = 0
    i = pos - 1
    while i >= 0:
        ch = code[i]
        if ch == ")":
            depth += 1
        elif ch == "(":
            if depth == 0:
                m = re.search(r"(?:@(?:[\w.]*\.)?Query|\bcreate(?P<native>Native)?Query)\s*$", code[max(0, i - 80):i])
                if m:
                    if m.group("native"):
                        return False
                    if m.group().startswith("create"):
                        return True
                    return not re.search(r"\bnativeQuery\s*=\s*true\b", code[i:_closing(code, i)])
            else:
                depth -= 1
        elif ch in ";{}" and depth == 0:   # a statement or declaration boundary: no enclosing query call
            return None
        i -= 1
    return None


def _jvm_facts(text: str, rel: str) -> dict:
    out: dict = {"tables": [], "models": [], "uses": [], "injects": [], "repos": []}
    kotlin = rel.lower().endswith(".kt")
    code, strings = _jvm_scan(text)   # comments and strings blanked, positions kept

    def line_of(pos: int) -> int:
        return text.count("\n", 0, pos) + 1

    class_ends = [m.end() for m in re.finditer(r"\bclass\s+\w+", code)]
    for start, eargs, cls, cls_at in _entities(code):
        entity = cls
        if eargs:   # @Entity(name = "Purchase"): the JPQL name, and the default table's
            entity = _top_level_name(code, text, eargs[0], eargs[1] - 1) or cls
        prev = max((e for e in class_ends if e <= start), default=0)
        head_start = max(prev, start - 400)   # an annotation of this class, not of the one before
        name, declared, via, decl = snake(entity), False, \
            "JPA default: the entity name in snake_case (Spring Boot naming)", line_of(cls_at)
        for t in re.finditer(r"@(?:[\w.]*\.)?Table\s*\(", code[head_start:cls_at]):
            a = head_start + t.start()
            tn = _top_level_name(code, text, head_start + t.end() - 1, _closing(code, head_start + t.end() - 1))
            if tn:
                name, declared, via, decl = tn, True, "JPA @Table(name)", line_of(a)
        out["tables"].append({"name": _norm(name), "line": decl, "via": via, "columns": [], "declared": declared})
        out["models"].append({"class": cls, "line": line_of(cls_at), "table": _norm(name),
                              "declared": declared, "decl_line": decl,
                              **({"entity": entity} if entity != cls else {})})
    for m in _JAVA_REPO.finditer(code):
        out["repos"].append({"repo": m.group("repo"), "entity": m.group("entity"), "line": line_of(m.start("repo"))})
    vars_: dict[str, str] = {}
    for rx in (_JAVA_FIELD, _KOTLIN_FIELD):
        for m in rx.finditer(code):
            vars_.setdefault(m.group("var"), m.group("type"))
    for m in _JAVA_CALL.finditer(code):
        t = vars_.get(m.group("var"))
        meth = m.group("m")
        if not t:
            continue
        op = ("write" if meth in WRITE_METHODS or meth.startswith(("save", "delete", "update", "insert"))
              else "read" if meth in READ_METHODS or meth.startswith(("find", "get", "count", "exists", "read",
                                                                     "query", "stream"))
              else None)
        if op:
            out["uses"].append({"line": line_of(m.start()), "repo_type": t, "op": op, "via": f"repository .{meth}()"})
    consts: dict[str, list[dict]] = {}
    for pos, s in strings:   # @Query("...") (JPQL unless nativeQuery = true), JDBC strings, text blocks
        jpql = _jpql_context(code, pos)
        if not jpql and not looks_like_sql(s):
            continue
        found = sql_matches(s)
        if jpql:   # entity names, not tables; `JOIN o.lines l` is a path, not an entity
            uses = [{"model": x["raw"], "op": x["op"], "via": "JPQL in @Query"} for x in found if not x["schema"]
                    and x["op"] in ("read", "write")]
        else:
            uses = [{"table": x["name"], "op": x["op"], "via": "SQL string"} for x in found]
        const = _JVM_CONST.search(code[max(0, pos - 200):pos].rstrip('"'))
        if const and not jpql:   # a constant: its reads and writes belong to the methods that name it
            consts[const.group("name")] = [{**u, "via": f"SQL constant {const.group('name')}"} for u in uses]
            for x in found:
                if x["op"] == "create":
                    out["tables"].append({"name": x["name"], "line": line_of(pos + x["pos"]),
                                          "via": "CREATE TABLE in a string",
                                          "columns": create_columns(s, x["name"]), "declared": True})
            continue
        named = [x for x in found if not x["schema"] and x["op"] in ("read", "write")] if jpql else found
        for u, x in zip(uses, named):   # each at the line naming it (a text block spans lines)
            out["uses"].append({"line": line_of(pos + x["pos"]), **u})
    for name, uses in consts.items():
        for m in re.finditer(r"(?<![\w.$])" + re.escape(name) + r"\b(?!\s*(?::\s*String\s*)?=[^=])", code):
            for u in uses:
                out["uses"].append({"line": line_of(m.start()), **u})
    for m in _JAVA_CLASS.finditer(code):
        ann = m.group("ann")
        if not any(f"@{s}" in ann for s in _SPRING_STEREOTYPES):
            continue
        start = m.end()
        body_end = code.find("\nclass ", start)
        body = code[start:body_end if body_end > 0 else len(code)]
        for f in re.finditer(r"@Autowired\s+(?:@\w+(?:\([^)]*\))?\s+)*(?:private\s+|protected\s+|public\s+|"
                             r"lateinit\s+var\s+\w+\s*:\s*)?(?:final\s+)?(?P<t>[A-Z]\w*)", body):
            out["injects"].append({"line": line_of(start + f.start("t")), "target": f.group("t"),
                                   "via": "Spring @Autowired", "owner_class": m.group("cls")})
        if "@RequiredArgsConstructor" in ann or "@AllArgsConstructor" in ann:   # Lombok: the final fields
            for f in re.finditer(r"(?:private\s+|protected\s+)?final\s+(?P<t>[A-Z]\w*)(?:<[^>]*>)?\s+\w+\s*;", body):
                out["injects"].append({"line": line_of(start + f.start("t")), "target": f.group("t"),
                                       "via": "Spring constructor injection (Lombok)",
                                       "owner_class": m.group("cls")})
        ctor = re.search(r"\b" + m.group("cls") + r"\s*\((?P<params>[^)]*)\)\s*\{", body) or \
            (re.search(r"^\s*(?:(?:@\w+(?:\([^)]*\))?\s*)*(?:private\s+|internal\s+|public\s+|protected\s+)?"
                       r"constructor\s*)?\((?P<params>[^)]*)\)", body) if kotlin else None)
        if ctor:
            for p in re.finditer(r"(?:(?:val|var)\s+\w+\s*:\s*|(?:final\s+)?)(?P<t>[A-Z]\w*)\b(?:<[^>]*>)?\s*\w*",
                                 ctor.group("params")):
                out["injects"].append({"line": line_of(start + ctor.start("params") + p.start("t")),
                                       "target": p.group("t"), "via": "Spring constructor injection",
                                       "owner_class": m.group("cls")})
    return out


# -- SQL files --------------------------------------------------------------------------------------

def _sql_facts(text: str, rel: str) -> dict:
    out: dict = {"tables": [], "models": [], "uses": [], "injects": []}
    migration = bool(re.search(r"(^|/)(migrations?|db/migrate|flyway|liquibase|schema)(/|$)", rel, re.I)
                     or re.match(r"V\d+(_\d+)*__", rel.rsplit("/", 1)[-1]))
    blank = _blank_sql(text, hash_comments=True)
    upper = blank.upper()
    for x in sql_matches(text, hash_comments=True):   # over the whole text: a statement may span lines
        table, op, pos = x["name"], x["op"], x["pos"]
        line = text.count("\n", 0, pos) + 1
        if op == "create":
            start = max(0, upper.rfind("CREATE", 0, pos))
            out["tables"].append({"name": table, "line": line, "via": "CREATE TABLE in " + (
                "a migration" if migration else "a SQL file"), "columns": create_columns(text[start:], table, True),
                "declared": True})
        elif op == "migrate":
            end = blank.find(";", pos)
            stmt = blank[pos:end if end >= 0 else len(blank)]
            added = [a.group(1).lower() for a in re.finditer(
                r"\bADD\s+(?:COLUMN\s+)?(?:IF\s+NOT\s+EXISTS\s+)?[`\"\[]?(\w+)", stmt, re.I)
                if a.group(1).lower() not in _COLUMN_SKIP]
            out["uses"].append({"line": line, "table": table, "op": "migrate", "via": "ALTER/DROP TABLE",
                                **({"columns": added} if added else {})})
    return out


def file_facts(text: str, rel: str) -> dict:
    """The tables, models, uses and injections one file holds."""
    low = rel.lower()   # `V1__init.SQL` is a SQL file too
    try:
        if low.endswith(".py"):
            return _py_facts(text, rel)
        if low.endswith((".java", ".kt")):
            return _jvm_facts(text, rel)
        if low.endswith(".sql"):
            return _sql_facts(text, rel)
    except (SyntaxError, ValueError, RecursionError):
        return {"unreadable": True}
    return {}


# the keys an entry of each list must have, with their types (a sidecar entry that does not is read again)
_SHAPES = {
    "tables": {"name": str, "line": int, "via": str},
    "models": {"class": str, "line": int, "table": str},
    "uses": {"line": int, "op": str, "via": str},
    "injects": {"line": int, "target": str, "via": str},
    "repos": {"repo": str, "entity": str, "line": int},
    "django": {"class": str, "line": int, "bases": list, "app": str, "columns": list},
    "dep_aliases": {"name": str, "target": str, "line": int, "via": str},
    "annots": {"name": str, "line": int},
}
# the optional keys linking reads, with their types
_OPTIONAL = {"table": str, "model": str, "repo_type": str, "owner_line": int, "decl_line": int, "owner_class": str,
             "db_table": str, "entity": str, "columns": list, "target": str, "app": str, "m2m": list}
_OPS = {"write": "writes_table", "read": "reads_table", "migrate": "migrates", "create": "migrates"}


_DB_CALL = re.compile(
    r"\.\s*(?:" + "|".join(sorted(WRITE_METHODS | READ_METHODS)) + r"|execute\w*|query\w*|batchUpdate|save\w*|"
    r"find\w*|delete\w*|update\w*|insert\w*)\s*\(|\bsession\b|"
    r"\b(?:select|insert|update|delete|get_object_or_404|get_list_or_404)\s*\(")


def line_uses_table(line: str) -> bool:
    """Does a cited line hold an ORM or database call (``session.add(o)``, ``Order.objects.filter(...)``,
    ``conn.execute(SQL)``, ``repo.save(o)``) - the kind of line the pass reads a table use from?"""
    return bool(_DB_CALL.search(line or ""))


def _typed(v, t) -> bool:
    return isinstance(v, t) and not (t is int and isinstance(v, bool))


def valid_facts(fx) -> bool:
    """Facts of the shape :func:`file_facts` makes (a damaged sidecar entry is not)."""
    if not isinstance(fx, dict):
        return False
    for key, shape in _SHAPES.items():
        v = fx.get(key)
        if v is None:
            continue
        if not isinstance(v, list):
            return False
        for item in v:
            if not isinstance(item, dict) or not all(_typed(item.get(k), t) for k, t in shape.items()):
                return False
            if any(t is list and not all(isinstance(x, str) for x in item[k]) for k, t in shape.items()):
                return False
            if any(k in item and not _typed(item[k], tp) for k, tp in _OPTIONAL.items()):
                return False
            cols = item.get("columns")
            if cols is not None and not all(isinstance(c, str) for c in cols):
                return False
            if key == "uses" and (item["op"] not in _OPS or not any(
                    isinstance(item.get(k), str) for k in ("table", "model", "repo_type"))):
                return False
    imports = fx.get("imports")
    return imports is None or (isinstance(imports, dict) and all(isinstance(v, str) for v in imports.values()))


# -- linking into the graph -------------------------------------------------------------------------

# a Python file is parsed only when it holds one of these: a model or table declaration, an injection, a migration
# step, an ORM session / manager / query, a Django import, or a SQL statement's shape
_PY_HINT = re.compile(
    r"__tablename__|\bTable\(|models\.Model|db\.Model|SQLModel|\bdjango\b|Depends\(|Security\(|\bop\.\w+\(|"
    r"migrations\.|\.objects\b|\bsession\b|\.query\(|\bselect\(|\.save\(|"
    r"(?i:\bSELECT\b[^\n]{0,300}?\bFROM\b|\bINSERT\s+(?:OR\s+\w+\s+)?INTO\b|\bDELETE\s+FROM\b|\bUPDATE\s+\S+\s+SET\b|"
    r"\bCREATE\s+(?:\w+\s+){0,3}TABLE\b|\bALTER\s+TABLE\b|\bDROP\s+TABLE\b)")


def candidate_files(g) -> list[str]:
    files = set()
    for _n, d in g.G.nodes(data=True):
        f = d.get("source_file")
        if f and f.lower().endswith(CODE_SUFFIXES) and d.get("file_type") in ("code", "document", None):
            files.add(f)
    try:
        from verinoda.snapshot import list_files

        files |= {f for f in list_files(g.root) if f.lower().endswith(".sql")}
    except Exception:  # noqa: BLE001 - no listing: the graph's files only
        pass
    from verinoda.testcode import is_test_or_support_file

    return sorted(f for f in files if not is_test_or_support_file(f))


def collect(g, read=None, old: dict | None = None) -> tuple[dict, list, list, dict]:
    """Facts of every candidate file (reused from ``old`` by sha256 when well formed), then ``(files, nodes,
    edges, report)``."""
    old = old if isinstance(old, dict) else {}
    files: dict[str, dict] = {}
    for f in candidate_files(g):
        try:
            data = read(f) if read else (g.root / f).read_bytes()
        except OSError:
            continue
        if isinstance(data, str):
            data = data.encode("utf-8")
        sha = hashlib.sha256(data).hexdigest()
        prev = old.get(f)
        if isinstance(prev, dict) and prev.get("sha256") == sha and valid_facts(prev.get("facts")):
            files[f] = prev
            continue
        text = data.decode("utf-8", errors="replace").lstrip("\ufeff")
        if f.lower().endswith(".py") and not _PY_HINT.search(text):   # nothing a table could come from: not parsed
            files[f] = {"sha256": sha, "facts": {}}
            continue
        files[f] = {"sha256": sha, "facts": file_facts(text, f)}
    nodes, edges, report = link(g, {f: v["facts"] for f, v in files.items()})
    return files, nodes, edges, report


def _class_node(g, f: str, cls: str, line: int) -> str | None:
    for n in g.symbols_in(f):
        if g.label(n).strip(".()").rpartition(".")[2] == cls:
            return n
    return g.symbol_at(f, line)


def _django_models(facts: dict[str, dict]) -> list[dict]:
    """The Django models among the classes the files hold: a class extending ``models.Model`` in a file that
    imports django, or a project class that is one (through any number of bases); with the table each makes
    and the columns it has (its abstract bases' fields, the implicit ``id`` unless a field is the primary key,
    a ``<parent>_ptr_id`` under a concrete parent; many-to-many fields are join tables)."""
    by_name: dict[str, list[dict]] = {}
    for f, fx in sorted(facts.items()):
        for c in fx.get("django") or []:
            by_name.setdefault(c["class"], []).append({**c, "file": f})
    known = {name for name, cs in by_name.items() if any(c.get("direct") for c in cs)}
    grew = True
    while grew:
        grew = False
        for name, cs in by_name.items():
            if name not in known and any(b in known for c in cs for b in c["bases"]):
                known.add(name)
                grew = True

    def base_of(c: dict, b: str) -> dict:
        """The class ``b`` that ``c`` extends: one in the same file, else the one its file imports (two apps
        may each have an ``Item``), else the first."""
        cands = by_name[b]
        same = [x for x in cands if x["file"] == c["file"]]
        if same:
            return same[0]
        target = ((facts.get(c["file"]) or {}).get("imports") or {}).get(b)
        if target:
            mod = target.rpartition(".")[0]
            hit = [x for x in cands if _module_of(x["file"]) == mod or _module_of(x["file"]).startswith(mod + ".")]
            if hit:
                return hit[0]
        return cands[0]

    def chain(c: dict, seen: set[str]) -> tuple[list[str], str | None]:
        """(inherited columns, the concrete parent) of ``c``."""
        cols: list[str] = []
        concrete = None
        for b in c["bases"]:
            if b not in known or b in seen:
                continue
            parent = base_of(c, b)
            if parent.get("abstract"):
                pc, pconc = chain(parent, seen | {b})
                cols += pc + list(parent["columns"])
                concrete = concrete or pconc
            else:
                concrete = concrete or b
        return cols, concrete

    def own_table(name: str, c: dict) -> tuple | None:
        if c.get("db_table"):
            return c["db_table"], True, "django Meta.db_table", c.get("decl_line") or c["line"]
        if c["app"]:
            return f"{c['app']}_{name.lower()}", False, "django default: <app>_<model>", c["line"]
        return None

    def proxied(c: dict, seen: set[str]) -> tuple[str, str] | None:
        """(concrete model, its table) behind a proxy model."""
        for b in c["bases"]:
            if b not in known or b in seen:
                continue
            parent = base_of(c, b)
            if parent.get("proxy") or parent.get("abstract"):
                hit = proxied(parent, seen | {b})
            else:
                hit = (b, own_table(b, parent)[0]) if own_table(b, parent) else None
            if hit:
                return hit
        return None

    out = []
    for name in sorted(known):
        for c in by_name[name]:
            if c.get("abstract"):
                continue
            if c.get("proxy"):   # Meta.proxy: the concrete parent's table, no table of its own
                hit = proxied(c, {name})
                if hit:
                    out.append({"file": c["file"], "class": name, "line": c["line"], "table": hit[1],
                                "declared": False, "via": f"django proxy of {hit[0]}", "decl_line": c["line"],
                                "columns": [], "proxy": True})
                continue
            inherited, concrete = chain(c, {name})
            cols = list(dict.fromkeys(inherited + list(c["columns"])))
            if concrete:
                cols = [f"{concrete.lower()}_ptr_id"] + cols
            elif not c.get("pk") and "id" not in cols:
                cols = ["id"] + cols
            own = own_table(name, c)
            if own is None:
                continue
            table, declared, via, line = own
            out.append({"file": c["file"], "class": name, "line": c["line"], "table": table, "declared": declared,
                        "via": via, "decl_line": line, "columns": cols})
            for j in c.get("m2m") or []:   # the join table Django makes: <table>_<field>
                if not isinstance(j, dict) or not isinstance(j.get("field"), str) \
                        or not isinstance(j.get("target"), str):
                    continue
                me, other = name.lower(), ("self" if j["target"] == "self" else j["target"].lower())
                jcols = ["id", f"from_{me}_id", f"to_{me}_id"] if other in ("self", me) else \
                    ["id", f"{me}_id", f"{other}_id"]
                out.append({"file": c["file"], "class": None, "line": c["line"], "table": f"{table}_{j['field']}",
                            "declared": False, "via": f"django ManyToManyField join table ({name}.{j['field']})",
                            "decl_line": c["line"], "columns": jcols})
    return out


def _pick(ms: list[dict], f: str, name: str, imports: dict) -> list[dict]:
    """Of several models named ``name``, the one the file ``f`` imports (``from shop.models import Item``) or
    defines itself."""
    if len(ms) < 2:
        return ms
    same = [m for m in ms if m["file"] == f]
    if len(same) == 1:
        return same
    target = (imports or {}).get(name)
    if target:
        mod = target.rpartition(".")[0]
        hit = [m for m in ms if _module_of(m["file"]) == mod or _module_of(m["file"]).startswith(mod + ".")]
        if len(hit) == 1:
            return hit
    return ms


def link(g, facts: dict[str, dict]):
    """Table nodes, edges and a report from the files' facts."""
    tables: dict[str, dict] = {}
    models: dict[str, list[dict]] = {}

    def define(name, f, line, via, declared, columns):
        e = tables.setdefault(name, {"defs": [], "columns": [], "uses": []})
        e["defs"].append({"at": f"{f}:{line}", "via": via, "declared": declared})
        for c in columns or []:
            if c not in e["columns"]:
                e["columns"].append(c)

    for f, fx in sorted(facts.items()):
        for t in fx.get("tables") or []:
            define(t["name"], f, t["line"], t["via"], t.get("declared", False), t.get("columns"))
        for m in fx.get("models") or []:
            models.setdefault(m["class"], []).append({**m, "file": f})
    for m in _django_models(facts):
        if not m.get("proxy"):   # a proxy model maps to its parent's table and defines none
            define(m["table"], m["file"], m["decl_line"], m["via"], m["declared"], m["columns"])
        if m["class"]:   # a join table has no model class
            models.setdefault(m["class"], []).append(m)
    entities: dict[str, list[dict]] = {}
    for cls, ms in models.items():
        for m in ms:
            entities.setdefault(m.get("entity") or cls, []).append(m)
    repos = {r["repo"]: r["entity"] for fx in facts.values() for r in fx.get("repos") or []}
    edges: list[tuple[str, str, dict]] = []
    seen_edges: set[tuple] = set()
    unresolved: list[dict] = []

    def edge(u, table, relation, f, line, conf, why, extra=None):
        key = (u, table, relation, f, line)
        if key in seen_edges:
            return
        seen_edges.add(key)
        edges.append((u, NODE_PREFIX + table, {"relation": relation, "confidence": conf, "_origin": ORIGIN,
                                               "source_file": f, "source_location": f"L{line}", "context": why,
                                               **(extra or {})}))

    for cls, ms in models.items():
        for m in ms:
            n = _class_node(g, m["file"], cls, m["line"])
            if n:
                edge(n, m["table"], "maps_to", m["file"], m.get("decl_line") or m["line"],
                     "EXTRACTED" if m["declared"] else "INFERRED", f"{cls} maps to {m['table']}")
    for f, fx in sorted(facts.items()):
        imports = fx.get("imports") or {}
        for u in fx.get("uses") or []:
            table = u.get("table")
            why = u["via"]
            if not table and u.get("model"):
                pool = entities if u["via"].startswith("JPQL") else models
                ms = _pick(pool.get(u["model"]) or [], f, u["model"], imports)
                if len(ms) != 1:
                    if ms:
                        unresolved.append({"at": f"{f}:{u['line']}", "model": u["model"],
                                           "why": f"{len(ms)} models of that name"})
                    continue
                table = ms[0]["table"]
                why += f" on {u['model']}"
            if not table and u.get("repo_type"):
                entity = repos.get(u["repo_type"])
                ms = models.get(entity or "") or []
                if len(ms) != 1:
                    continue
                table = ms[0]["table"]
                why += f" on {u['repo_type']} ({entity})"
            if not table:
                continue
            e = tables.setdefault(table, {"defs": [], "columns": [], "uses": []})
            for c in u.get("columns") or []:
                if c not in e["columns"]:
                    e["columns"].append(c)
            owner = g.symbol_at(f, u["line"])
            at = f"{f}:{u['line']}"
            if not any(x["at"] == at and x["op"] == u["op"] for x in e["uses"]):
                e["uses"].append({"at": at, "op": u["op"], "via": why, "by": g.label(owner) if owner else None})
            if owner:
                edge(owner, table, _OPS[u["op"]], f, u["line"], "INFERRED", why)
    by_name: dict[str, list[str]] = {}
    for n, d in g.G.nodes(data=True):
        if d.get("file_type") == "code" and d.get("source_file"):
            by_name.setdefault(str(d.get("label") or "").strip(".()").rpartition(".")[2], []).append(n)
    aliases: dict[str, list[dict]] = {}
    for f, fx in sorted(facts.items()):
        for a in fx.get("dep_aliases") or []:
            aliases.setdefault(a["name"], []).append(a)
    injections = []
    seen_inj: set[tuple] = set()
    for f, fx in sorted(facts.items()):
        wanted = list(fx.get("injects") or [])
        for a in fx.get("annots") or []:   # a parameter typed with an Annotated[..., Depends(f)] alias
            hits = aliases.get(a["name"]) or []
            if len({h["target"] for h in hits}) == 1:
                wanted.append({"line": a["line"], "target": hits[0]["target"], "via": hits[0]["via"],
                               "owner_line": a["line"]})
        for i in wanted:
            targets = [t for t in by_name.get(i["target"], []) if not g.is_file_node(t)]
            if i.get("owner_class") and len(targets) > 1:   # a Spring dependency is a type, not its constructor
                targets = [t for t in targets if not g.label(t).endswith(")")]
            if i.get("owner_class"):
                owner = _class_node(g, f, i["owner_class"], i["line"])
            else:
                owner = g.symbol_at(f, i.get("owner_line") or i["line"])
            if len(targets) != 1 or not owner or owner == targets[0] or (owner, targets[0]) in seen_inj:
                continue
            seen_inj.add((owner, targets[0]))
            edges.append((owner, targets[0], {"relation": "injects", "confidence": "INFERRED", "_origin": ORIGIN,
                                             "source_file": f, "source_location": f"L{i['line']}",
                                             "context": i["via"]}))
            injections.append({"at": f"{f}:{i['line']}", "from": g.label(owner), "to": g.label(targets[0]),
                               "via": i["via"]})
    nodes = []
    for name, e in sorted(tables.items()):
        where = (e["defs"] or e["uses"] or [{"at": ""}])[0]["at"]
        f, _, line = where.rpartition(":")
        nodes.append((NODE_PREFIX + name, {"label": name, "file_type": "schema", "kind": "table",
                                           "source_file": f or None, "source_location": f"L{line}" if line else None,
                                           "columns": e["columns"], "defined": bool(e["defs"]), "_origin": ORIGIN}))
    report = {"tables": [{"name": n, **e} for n, e in sorted(tables.items())], "injections": injections,
              "unresolved": unresolved[:50],
              "counts": {"tables": len(tables), "defined": sum(1 for e in tables.values() if e["defs"]),
                         "models": sum(len(v) for v in models.values()), "edges": len(edges),
                         "injections": len(injections)}}
    return nodes, edges, report


def apply(g, block: dict | None) -> None:
    """Add a stored block's table nodes and edges to the graph (malformed entries skipped)."""
    block = block if isinstance(block, dict) else {}
    for n in block.get("nodes") or []:
        if isinstance(n, (list, tuple)) and len(n) == 2 and isinstance(n[0], str) and isinstance(n[1], dict) \
                and n[0].startswith(NODE_PREFIX) and n[0] not in g.G:
            g.G.add_node(n[0], **n[1])
    from verinoda.index import _apply_edges

    raw = block.get("edges") if isinstance(block.get("edges"), list) else []
    _apply_edges(g, [(e[0], e[1], e[2]) for e in raw if isinstance(e, (list, tuple)) and len(e) == 3
                     and isinstance(e[0], str) and isinstance(e[1], str) and isinstance(e[2], dict)])


def is_table(g, n: str) -> bool:
    return n in g.G and g.G.nodes[n].get("kind") == "table" and g.G.nodes[n].get("file_type") == "schema"


# -- the schema report and the live database (opt-in) --------------------------------------------------

def _read_tables(conn) -> dict[str, list[str]]:
    names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' "
                                        "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    return {n.lower(): [r[0].lower() for r in conn.execute("SELECT name FROM pragma_table_info(?)", (n,))]
            for n in names}


# tables a framework keeps for itself in the project's database (not defined by the project's code)
_FRAMEWORK_TABLE = re.compile(r"(?:django_migrations|django_session|django_content_type|django_admin_log|"
                              r"django_site|auth_\w+|alembic_version|flyway_schema_history|databasechangelog\w*|"
                              r"schema_migrations|ar_internal_metadata)$")


def live_sqlite(path: Path) -> dict[str, list[str]]:
    """{table: columns} of a local SQLite database, read without writing anything beside it: opened
    ``immutable`` (no lock, no ``-shm`` / ``-wal`` file made), or, when it has a write-ahead log, a temporary
    copy of the file and its log is read (the log holds committed rows the file does not)."""
    import shutil
    import sqlite3
    import tempfile

    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"no database file at {p}")
    wal = p.with_name(p.name + "-wal")
    if wal.is_file() and wal.stat().st_size > 0:
        with tempfile.TemporaryDirectory(prefix="verinoda-db-") as tmp:
            copy = Path(tmp) / "copy.db"
            shutil.copyfile(p, copy)
            shutil.copyfile(wal, Path(tmp) / "copy.db-wal")
            conn = sqlite3.connect(copy)
            try:
                return _read_tables(conn)
            finally:
                conn.close()
    conn = sqlite3.connect(p.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        return _read_tables(conn)
    finally:
        conn.close()


def report(g, *, db: Path | None = None, table: str | None = None) -> dict:
    """The tables the code defines and uses, who reads and writes them, injections, and with ``db`` the
    differences from a live SQLite database (only ``table`` when given)."""
    from verinoda import index

    side = index._read_sidecar(g.root) or {}
    block = side.get("data_schema") if isinstance(side.get("data_schema"), dict) else None
    old = block.get("files") if block and block.get("facts_version") == FACTS_VERSION else None
    _files, _nodes, _edges, rep = collect(g, old=old if isinstance(old, dict) else None)
    tables = rep["tables"]
    if table:
        tables = [t for t in tables if t["name"] == _norm(table)]
    out = {"tables": tables, "injections": rep["injections"], "unresolved": rep["unresolved"],
           "counts": rep["counts"], "status": "strong_inference" if tables else "none_found",
           "limits": [
               "read from the code: table names a framework derives (Django <app>_<model>, JPA snake_case, SQLModel, "
               "Flask-SQLAlchemy) follow the framework's default and say so; a custom naming strategy or db_table "
               "set elsewhere is not seen",
               "reads and writes are calls on a model class, an instance built in the same function, a model handed "
               "to a session, a repository field or SQL strings; a model passed in as a parameter or a query built "
               "in pieces is not seen",
               "a SQL string is parsed by patterns (INSERT INTO, UPDATE ... SET, DELETE FROM, FROM, JOIN), not by a "
               "SQL parser",
           ]}
    if db is not None:
        live = live_sqlite(db)
        code = {t["name"]: t for t in rep["tables"]}
        if table:
            want_name = _norm(table)
            live = {n: c for n, c in live.items() if n == want_name}
            code = {n: t for n, t in code.items() if n == want_name}
        framework = sorted(n for n in set(live) - set(code) if _FRAMEWORK_TABLE.match(n))
        diff = {"database": str(db), "only_in_database": sorted(set(live) - set(code) - set(framework)),
                "framework_tables": framework,
                "only_in_code": sorted(n for n, t in code.items() if t["defs"] and n not in live),
                "columns": []}
        for name in sorted(set(live) & set(code)):
            want = set(code[name]["columns"])
            have = set(live[name])
            if want and (want - have or have - want):
                diff["columns"].append({"table": name, "only_in_code": sorted(want - have),
                                        "only_in_database": sorted(have - want)})
        diff["status"] = "observed"
        diff["basis"] = ("the database file as read now (read only: opened immutable, or a temporary copy with its "
                         "write-ahead log; opened immutable, a hot rollback journal of an unfinished write is not "
                         "applied); framework bookkeeping tables (migrations, sessions, auth) are listed apart and "
                         "are no difference; the code side as above")
        out["live"] = diff
    return out


def render(res: dict) -> str:
    c = res["counts"]
    out = [f"{c['tables']} table(s) ({c['defined']} defined in the code), {c['models']} model(s), "
           f"{c['edges']} edge(s), {c['injections']} injection(s)"]
    for t in res["tables"]:
        reads = list({(u["by"], u["at"]): u for u in t["uses"] if u["op"] == "read"}.values())
        writes = list({(u["by"], u["at"]): u for u in t["uses"] if u["op"] == "write"}.values())
        defs = ", ".join(f"{d['at']} ({d['via']})" for d in t["defs"][:3]) or "not defined in the code"
        out.append(f"  {t['name']}: {defs}")
        if t["columns"]:
            out.append(f"      columns: {', '.join(t['columns'][:20])}")
        for label, us in (("written by", writes), ("read by", reads)):
            if us:
                out.append(f"      {label}: " + "; ".join(f"{u['by'] or '?'} @{u['at']}" for u in us[:6])
                           + (f" (+{len(us) - 6})" if len(us) > 6 else ""))
    for i in res["injections"][:20]:
        out.append(f"  inject: {i['from']} <- {i['to']} ({i['via']}) @{i['at']}")
    live = res.get("live")
    if live:
        out.append(f"  live database {live['database']}: only there {live['only_in_database'] or '-'}; "
                   f"only in the code {live['only_in_code'] or '-'}")
        if live.get("framework_tables"):
            out.append(f"      framework tables (no difference): {', '.join(live['framework_tables'])}")
        for d in live["columns"]:
            out.append(f"      {d['table']}: columns only in the code {d['only_in_code'] or '-'}, only in the "
                       f"database {d['only_in_database'] or '-'}")
    return "\n".join(out) + "\n"
