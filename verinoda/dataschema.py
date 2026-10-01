"""Database tables, ORM models, migrations and dependency injection as graph nodes and edges.

Read from the code, never from a running service:

- **tables** - SQLAlchemy declarative classes (``__tablename__``), ``Table("name", ...)``, SQLModel classes with
  ``table=True``, Django models (``<app>_<model>`` or ``Meta.db_table``), JPA ``@Entity`` classes (``@Table(name=)``,
  else the class name in snake_case, Spring Boot's default naming), ``CREATE TABLE`` in ``.sql`` files and in SQL
  strings, Alembic ``op.create_table`` / ``op.add_column`` and Django ``migrations.CreateModel``;
- **reads and writes** - SQL strings in a function (``INSERT INTO``, ``UPDATE``, ``DELETE FROM`` write; ``SELECT
  ... FROM`` / ``JOIN`` read), ORM calls on a model class or an instance of one (``Order.objects.filter``,
  ``session.query(Order)``, ``session.add(order)``, ``order.save()``) and Spring Data repositories
  (``orderRepository.save(...)`` on a ``JpaRepository<Order, ...>``);
- **injection** - FastAPI ``Depends(provider)`` parameters and Spring constructor / ``@Autowired`` dependencies on a
  project class.

Each table becomes a node (``file_type: schema``, ``kind: table``, id ``table:<name>``) and each use an edge:
``maps_to`` (model class -> table), ``writes_table`` / ``reads_table`` (function -> table), ``migrates`` (migration
function -> table), ``injects`` (consumer -> provider). Edges are INFERRED (``derived_by: verinoda.dataschema``)
except a model's mapping declared by name (``__tablename__``, ``@Table(name=)``, ``db_table``), which is EXTRACTED.
A table name the code does not spell (a Django or JPA default) says how it was derived.

The live database is opt-in and read only: :func:`live_sqlite` opens a local SQLite file the user names
(``mode=ro``) and lists its tables and columns for :func:`report` to compare with the code.
"""

from __future__ import annotations

import ast
import hashlib
import re
from pathlib import Path

ORIGIN = "verinoda.dataschema"
FACTS_VERSION = 1
NODE_PREFIX = "table:"
RELATIONS = ("maps_to", "writes_table", "reads_table", "migrates", "injects")
CODE_SUFFIXES = (".py", ".java", ".kt", ".sql")

_SQL_START = re.compile(r"^\s*(SELECT|INSERT|UPDATE|DELETE|WITH|CREATE|REPLACE|MERGE|ALTER|DROP)\b", re.I)
# the shape of a statement, not only its first word ("Merge a class ..." and "Update the index" are prose)
_SQL_SHAPE = re.compile(
    r"^\s*(?:SELECT\b[\s\S]*?\bFROM\b|INSERT\s+(?:OR\s+\w+\s+)?INTO\b|UPDATE\s+(?:OR\s+\w+\s+)?\S+\s+SET\b|"
    r"DELETE\s+FROM\b|WITH\s+(?:RECURSIVE\s+)?\w+\s*(?:\([^)]*\))?\s+AS\s*\(|"
    r"CREATE\s+(?:TEMP\w*\s+|UNIQUE\s+|VIRTUAL\s+)*(?:TABLE|INDEX|VIEW|TRIGGER)\b|REPLACE\s+INTO\b|MERGE\s+INTO\b|"
    r"ALTER\s+TABLE\b|DROP\s+(?:TABLE|INDEX|VIEW)\b)", re.I)
_NAME = r"[`\"\[]?(?P<n>[A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)?)[`\"\]]?"
_SQL_PATTERNS = [
    (re.compile(r"\bCREATE\s+(?:TEMP(?:ORARY)?\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?" + _NAME, re.I), "create"),
    (re.compile(r"\bALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?" + _NAME, re.I), "migrate"),
    (re.compile(r"\bDROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?" + _NAME, re.I), "migrate"),
    (re.compile(r"\bINSERT\s+(?:OR\s+\w+\s+)?INTO\s+" + _NAME, re.I), "write"),
    (re.compile(r"\bREPLACE\s+INTO\s+" + _NAME, re.I), "write"),
    (re.compile(r"\bMERGE\s+INTO\s+" + _NAME, re.I), "write"),
    (re.compile(r"\bUPDATE\s+(?:OR\s+\w+\s+)?" + _NAME + r"\s+SET\b", re.I), "write"),
    (re.compile(r"\bDELETE\s+FROM\s+" + _NAME, re.I), "write"),
    (re.compile(r"\b(?:FROM|JOIN)\s+" + _NAME, re.I), "read"),
]
_SQL_NOT_TABLES = {"select", "where", "lateral", "unnest", "values", "dual", "set", "only", "if", "not",
                   "exists", "on", "as", "table", "index", "into", "or", "and"}
_COLUMN_SKIP = {"primary", "foreign", "unique", "constraint", "check", "key", "index", "exclude"}

WRITE_METHODS = {"add", "add_all", "create", "bulk_create", "update", "delete", "save", "merge", "insert",
                 "bulk_update", "update_or_create", "get_or_create", "remove", "bulk_insert_mappings",
                 "bulk_save_objects", "saveAll", "saveAndFlush", "deleteAll", "deleteById", "persist"}
READ_METHODS = {"query", "filter", "filter_by", "get", "all", "first", "one", "select", "exclude", "values",
                "values_list", "scalars", "count", "exists", "get_object_or_404", "aggregate", "annotate",
                "order_by", "scalar", "one_or_none", "find", "find_one", "find_all", "findAll", "findById",
                "existsById", "getById", "getReferenceById", "in_bulk", "iterator", "latest", "earliest"}
DJANGO_FIELD_FK = ("ForeignKey", "OneToOneField")
_SPRING_STEREOTYPES = ("Service", "Component", "Repository", "Controller", "RestController", "Configuration")


def snake(name: str) -> str:
    """Spring Boot's physical naming (``CamelCaseToUnderscoresNamingStrategy``): ``OrderLine`` -> ``order_line``."""
    s = re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", name)
    return s.lower()


def _norm(name: str) -> str:
    """A table name as compared: unquoted, without a schema prefix, lower case."""
    return name.strip('`"[]').rsplit(".", 1)[-1].lower()


# -- SQL text ---------------------------------------------------------------------------------------

def sql_tables(text: str) -> list[tuple[str, str]]:
    """``(table, op)`` for the tables a SQL text names: create, migrate, write or read (a table both read and
    written in one statement is listed for each)."""
    out: list[tuple[str, str]] = []
    claimed: list[tuple[int, int]] = []
    for rx, op in _SQL_PATTERNS:
        for m in rx.finditer(text):
            s, e = m.span("n")
            if any(a <= s < b for a, b in claimed):   # `DELETE FROM t` is a write, not also a read
                continue
            name = _norm(m.group("n"))
            if name in _SQL_NOT_TABLES or not name:
                continue
            claimed.append((s, e))
            out.append((name, op))
    return out


def create_columns(text: str, table: str) -> list[str]:
    """The column names of ``CREATE TABLE table (...)`` in ``text`` (constraints left out)."""
    m = re.search(r"\bCREATE\s+(?:TEMP(?:ORARY)?\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[`\"\[]?(?:\w+\.)?"
                  + re.escape(table) + r"[`\"\]]?\s*\(", text, re.I)
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

def _const_str(node) -> str | None:
    """A string a node spells: a constant, an f-string's literal parts (values as ``?``), a ``+`` of those."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else "?"
                       for v in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        a, b = _const_str(node.left), _const_str(node.right)
        if a is not None or b is not None:
            return (a if a is not None else "?") + (b if b is not None else "?")
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
    """The app label of a Django models module: the folder holding ``models.py`` or the ``models`` package."""
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


def looks_like_sql(text: str) -> bool:
    """A string that reads as a SQL statement: it starts with a statement keyword written in capitals, or in any case
    with a SQL marker in it (a placeholder, ``=``, ``*``, ``WHERE``, ``VALUES``) - so "Select a file" is prose."""
    m = _SQL_START.match(text)
    if not m or not _SQL_SHAPE.match(text):
        return False
    if m.group(1).isupper():
        return True
    return bool(re.search(r"\?|%s|%\(\w+\)s|(?<!:):\w|=|\*|\bwhere\b|\bvalues\b", text, re.I))


def _py_facts(text: str, rel: str) -> dict:
    out: dict = {"tables": [], "models": [], "uses": [], "injects": []}
    _PyFacts(out, rel).run(ast.parse(text))
    return out


_FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)
_POP = object()   # a marker on the stack: leaving a function


class _PyFacts:
    """One pass over a module (an explicit stack, no visitor dispatch): models, ``Table()``, SQL strings and ORM /
    injection calls per function."""

    def __init__(self, out: dict, rel: str):
        self.out, self.rel, self.app = out, rel, _django_app(rel)
        self.funcs: list[tuple[ast.AST, dict[str, str]]] = []   # (function, name -> class it was built from)

    @staticmethod
    def _docstring(node):
        body = getattr(node, "body", None)
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                and isinstance(body[0].value.value, str):
            return body[0].value
        return None

    def run(self, tree: ast.Module) -> None:
        skip: set[int] = set()   # docstrings and the string parts of a concatenation already read
        doc = self._docstring(tree)
        if doc is not None:
            skip.add(id(doc))
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
                self.funcs.append((n, {}))
                stack.append(_POP)
            elif t is ast.ClassDef:
                doc = self._docstring(n)
                if doc is not None:
                    skip.add(id(doc))
                _py_model(n, self.rel, self.app, self.out)
            elif t is ast.Assign:
                self._assign(n)
            elif t is ast.Call:
                _py_call(n, self.funcs[-1] if self.funcs else (None, {}), self.out)
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

    def _assign(self, node: ast.Assign) -> None:
        v = node.value
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

    def _string(self, node) -> None:
        s = _const_str(node)
        if not s or not looks_like_sql(s):
            return
        for table, op in sql_tables(s):
            if op == "create":
                self.out["tables"].append({"name": table, "line": node.lineno, "via": "CREATE TABLE in a string",
                                           "columns": create_columns(s, table), "declared": True})
            self.out["uses"].append({"line": node.lineno, "table": table, "op": op, "via": "SQL string"})


def _py_model(cls: ast.ClassDef, rel: str, app: str | None, out: dict) -> None:
    body = cls.body
    tablename = next((_const_str(s.value) for s in body if isinstance(s, ast.Assign)
                      and any(isinstance(t, ast.Name) and t.id == "__tablename__" for t in s.targets)), None)
    bases = [_dotted(b) for b in cls.bases]
    sqlmodel = any(b[-1:] == ("SQLModel",) for b in bases) and any(
        k.arg == "table" and isinstance(k.value, ast.Constant) and k.value.value is True for k in cls.keywords)
    django = any(b[-2:] == ("models", "Model") for b in bases) or (
        app is not None and any(b == ("Model",) for b in bases))
    meta = next((s for s in body if isinstance(s, ast.ClassDef) and s.name == "Meta"), None)
    db_table, abstract = None, False
    if meta is not None:
        for s in meta.body:
            if isinstance(s, ast.Assign) and isinstance(s.targets[0], ast.Name):
                if s.targets[0].id == "db_table":
                    db_table = _const_str(s.value)
                if s.targets[0].id == "abstract" and isinstance(s.value, ast.Constant) and s.value.value is True:
                    abstract = True
    name = declared = via = None
    if tablename:
        name, declared, via = tablename, True, "sqlalchemy __tablename__"
    elif sqlmodel:
        name, declared, via = cls.name.lower(), False, "SQLModel default: the class name in lower case"
    elif django and not abstract:
        if db_table:
            name, declared, via = db_table, True, "django Meta.db_table"
        elif app:
            name, declared, via = f"{app}_{cls.name.lower()}", False, "django default: <app>_<model>"
    if not name:
        return
    cols = []
    for s in body:
        target = s.target if isinstance(s, ast.AnnAssign) else (s.targets[0] if isinstance(s, ast.Assign) else None)
        value = s.value if isinstance(s, (ast.Assign, ast.AnnAssign)) else None
        if not isinstance(target, ast.Name) or target.id.startswith("__") or not isinstance(value, ast.Call):
            continue
        fn = _dotted(value.func)[-1:] or ("",)
        if fn[0] in ("Column", "mapped_column", "Field") or fn[0].endswith("Field") or fn[0] in DJANGO_FIELD_FK:
            explicit = _const_str(value.args[0]) if value.args and fn[0] in ("Column", "mapped_column") else None
            col = explicit or (target.id + "_id" if fn[0] in DJANGO_FIELD_FK else target.id)
            if fn[0] == "Field" and not sqlmodel:
                continue
            cols.append(col.lower())
    out["tables"].append({"name": _norm(name), "line": cls.lineno, "via": via, "columns": cols,
                          "declared": declared})
    out["models"].append({"class": cls.name, "line": cls.lineno, "table": _norm(name), "declared": declared})


def _py_call(n: ast.Call, ctx: tuple, out: dict) -> None:
    """One call in a function: an Alembic or Django migration step, an injection, an ORM read or write."""
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
        if mname:
            out["uses"].append({"line": n.lineno, "model": mname, "op": "migrate", "via": "django CreateModel",
                                **({"table": _norm(db_table)} if db_table else {})})
        return
    if method in ("Depends", "Security"):
        target = _dotted(n.args[0]) if n.args else ()
        if target:
            out["injects"].append({"line": n.lineno, "target": target[-1], "via": f"FastAPI {method}()"})
        return
    if func is None:
        return
    op = "write" if method in WRITE_METHODS else "read" if method in READ_METHODS else None
    if op is None:
        return
    refs: set[str] = set()
    base = n.func.value if isinstance(n.func, ast.Attribute) else None
    while isinstance(base, (ast.Attribute, ast.Call, ast.Subscript)):   # Order.objects.filter(...).first()
        base = base.func if isinstance(base, ast.Call) else base.value
    if isinstance(base, ast.Name):
        refs.add(instances.get(base.id, base.id))
    for a in [*n.args, *(k.value for k in n.keywords)]:
        if isinstance(a, ast.Name):
            refs.add(instances.get(a.id, a.id))
        elif isinstance(a, ast.Call):
            f = _dotted(a.func)
            if f and f[-1] in ("select", "insert", "update", "delete") and a.args and isinstance(a.args[0], ast.Name):
                refs.add(a.args[0].id)   # session.execute(select(Order))
                op = "write" if f[-1] in ("insert", "update", "delete") else op
            elif f and f[-1][:1].isupper():
                refs.add(f[-1])         # session.add(Order(...))
    for r in sorted(refs):
        if r[:1].isupper():
            out["uses"].append({"line": n.lineno, "model": r, "op": op, "via": f"ORM .{method}()"})


_JAVA_ENTITY = re.compile(r"@Entity\b(?P<between>(?:\s*@[\w.]+(?:\([^)]*\))?)*)\s*(?:public\s+|data\s+|open\s+|"
                          r"abstract\s+|final\s+)*class\s+(?P<cls>\w+)")
_JAVA_TABLE = re.compile(r"@Table\s*\([^)]*?\bname\s*=\s*\"(?P<t>[^\"]+)\"")
_JAVA_REPO = re.compile(r"interface\s+(?P<repo>\w+)\s*(?:<[^>]*>)?\s*(?:extends|:)\s*[^{]*?\b(?:Jpa|Crud|"
                        r"PagingAndSorting|ListCrud|Mongo|Reactive)\w*Repository\s*<\s*(?P<entity>\w+)")
_JAVA_FIELD = re.compile(r"\b(?P<type>[A-Z]\w*)(?:<[^>]*>)?\s+(?P<var>[a-z]\w*)\s*[;,)=]")
_KOTLIN_FIELD = re.compile(r"\b(?:val|var)\s+(?P<var>[a-z]\w*)\s*:\s*(?P<type>[A-Z]\w*)")
_JAVA_CALL = re.compile(r"\b(?P<var>[a-z]\w*)\s*\.\s*(?P<m>\w+)\s*\(")
_JAVA_SQL = re.compile(r"\"((?:SELECT|INSERT|UPDATE|DELETE|WITH|CREATE|MERGE)\b[^\"]*)\"", re.I)
_JAVA_CLASS = re.compile(r"(?P<ann>(?:@\w+(?:\([^)]*\))?\s*)*)(?:public\s+|open\s+|final\s+|abstract\s+)*class\s+(?P<cls>\w+)")


def _jvm_facts(text: str, rel: str) -> dict:
    from verinoda.index import _java_code_lines

    out: dict = {"tables": [], "models": [], "uses": [], "injects": [], "repos": []}
    kotlin = rel.endswith(".kt")
    code = "\n".join(_java_code_lines(text, kotlin=kotlin))   # comments and strings blanked, lines kept

    def line_of(pos: int) -> int:
        return code.count("\n", 0, pos) + 1

    for m in _JAVA_ENTITY.finditer(code):
        cls = m.group("cls")
        head = text[max(0, m.start() - 400):m.end()]   # @Table(name="...") holds a string: read the raw text
        t = _JAVA_TABLE.search(head)
        name, declared, via = (t.group("t"), True, "JPA @Table(name)") if t else (
            snake(cls), False, "JPA default: the entity name in snake_case (Spring Boot naming)")
        out["tables"].append({"name": _norm(name), "line": line_of(m.start("cls")), "via": via, "columns": [],
                              "declared": declared})
        out["models"].append({"class": cls, "line": line_of(m.start("cls")), "table": _norm(name),
                              "declared": declared})
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
    for m in _JAVA_SQL.finditer(text):   # @Query("...") and JDBC strings
        if not looks_like_sql(m.group(1)):
            continue
        for table, op in sql_tables(m.group(1)):
            out["uses"].append({"line": text.count("\n", 0, m.start()) + 1, "table": table, "op": op,
                                "via": "SQL string"})
    for m in _JAVA_CLASS.finditer(code):
        if not any(f"@{s}" in m.group("ann") for s in _SPRING_STEREOTYPES):
            continue
        start = m.end()
        body_end = code.find("\nclass ", start)
        body = code[start:body_end if body_end > 0 else len(code)]
        for f in re.finditer(r"@Autowired\s+(?:private\s+|protected\s+|public\s+|lateinit\s+var\s+\w+\s*:\s*)?"
                             r"(?:final\s+)?(?P<t>[A-Z]\w*)", body):
            out["injects"].append({"line": line_of(start + f.start("t")), "target": f.group("t"),
                                   "via": "Spring @Autowired", "owner_class": m.group("cls")})
        ctor = re.search(r"\b" + m.group("cls") + r"\s*\((?P<params>[^)]*)\)\s*\{", body) or \
            (re.search(r"^\s*\((?P<params>[^)]*)\)", body) if kotlin else None)
        if ctor:
            for p in re.finditer(r"(?:(?:val|var)\s+\w+\s*:\s*|(?:final\s+)?)(?P<t>[A-Z]\w*)\b(?:<[^>]*>)?\s*\w*",
                                 ctor.group("params")):
                out["injects"].append({"line": line_of(start + ctor.start("params") + p.start("t")),
                                       "target": p.group("t"), "via": "Spring constructor injection",
                                       "owner_class": m.group("cls")})
    return out


def _sql_facts(text: str, rel: str) -> dict:
    out: dict = {"tables": [], "models": [], "uses": [], "injects": []}
    migration = bool(re.search(r"(^|/)(migrations?|db/migrate|flyway|liquibase|schema)(/|$)", rel, re.I)
                     or re.match(r"V\d+(_\d+)*__", rel.rsplit("/", 1)[-1]))
    for i, line in enumerate(text.splitlines(), 1):
        for table, op in sql_tables(line):
            if op == "create":
                rest = "\n".join(text.splitlines()[i - 1:])
                out["tables"].append({"name": table, "line": i, "via": "CREATE TABLE in " + (
                    "a migration" if migration else "a SQL file"), "columns": create_columns(rest, table),
                    "declared": True})
            elif op == "migrate":
                added = re.search(r"\bADD\s+(?:COLUMN\s+)?(?:IF\s+NOT\s+EXISTS\s+)?[`\"\[]?(\w+)", line, re.I)
                out["uses"].append({"line": i, "table": table, "op": "migrate", "via": "ALTER/DROP TABLE",
                                    **({"columns": [added.group(1).lower()]} if added and
                                       added.group(1).lower() not in _COLUMN_SKIP else {})})
    return out


def file_facts(text: str, rel: str) -> dict:
    """The tables, models, uses and injections one file holds."""
    try:
        if rel.endswith(".py"):
            return _py_facts(text, rel)
        if rel.endswith((".java", ".kt")):
            return _jvm_facts(text, rel)
        if rel.endswith(".sql"):
            return _sql_facts(text, rel)
    except (SyntaxError, ValueError, RecursionError):
        return {"unreadable": True}
    return {}


# -- linking into the graph -------------------------------------------------------------------------

# a Python file is parsed only when it holds one of these: a model or table declaration, an injection, a migration
# step, an ORM session / manager / query, or a SQL statement's shape
_PY_HINT = re.compile(
    r"__tablename__|\bTable\(|models\.Model|SQLModel|Depends\(|Security\(|\bop\.\w+\(|migrations\.|\.objects\b|"
    r"\bsession\b|\.query\(|\bselect\(|\.save\(|"
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
    """Facts of every candidate file (reused from ``old`` by sha256), then ``(files, nodes, edges, report)``."""
    old = old or {}
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
        if prev and prev.get("sha256") == sha:
            files[f] = prev
            continue
        text = data.decode("utf-8", errors="replace").lstrip("﻿")
        if f.endswith(".py") and not _PY_HINT.search(text):   # nothing a table could come from: not parsed
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


def link(g, facts: dict[str, dict]):
    """Table nodes, edges and a report from the files' facts."""
    tables: dict[str, dict] = {}
    models: dict[str, list[dict]] = {}
    for f, fx in sorted(facts.items()):
        for t in fx.get("tables") or []:
            e = tables.setdefault(t["name"], {"defs": [], "columns": [], "uses": []})
            e["defs"].append({"at": f"{f}:{t['line']}", "via": t["via"], "declared": t["declared"]})
            for c in t.get("columns") or []:
                if c not in e["columns"]:
                    e["columns"].append(c)
        for m in fx.get("models") or []:
            models.setdefault(m["class"], []).append({**m, "file": f})
    repos = {r["repo"]: r["entity"] for fx in facts.values() for r in fx.get("repos") or []}
    edges: list[tuple[str, str, dict]] = []
    unresolved: list[dict] = []

    def edge(u, table, relation, f, line, conf, why, extra=None):
        edges.append((u, NODE_PREFIX + table, {"relation": relation, "confidence": conf, "_origin": ORIGIN,
                                               "source_file": f, "source_location": f"L{line}", "context": why,
                                               **(extra or {})}))

    for cls, ms in models.items():
        for m in ms:
            n = _class_node(g, m["file"], cls, m["line"])
            if n:
                edge(n, m["table"], "maps_to", m["file"], m["line"], "EXTRACTED" if m["declared"] else "INFERRED",
                     f"{cls} maps to {m['table']}")
    for f, fx in sorted(facts.items()):
        for u in fx.get("uses") or []:
            table = u.get("table")
            why = u["via"]
            if not table and u.get("model"):
                ms = models.get(u["model"]) or []
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
            relation = {"write": "writes_table", "read": "reads_table", "migrate": "migrates", "create": "migrates"}[
                u["op"]]
            e["uses"].append({"at": f"{f}:{u['line']}", "op": u["op"], "via": why,
                              "by": g.label(owner) if owner else None})
            if owner:
                edge(owner, table, relation, f, u["line"], "INFERRED", why)
    by_name: dict[str, list[str]] = {}
    for n, d in g.G.nodes(data=True):
        if d.get("file_type") == "code" and d.get("source_file"):
            by_name.setdefault(str(d.get("label") or "").strip(".()").rpartition(".")[2], []).append(n)
    injections = []
    for f, fx in sorted(facts.items()):
        for i in fx.get("injects") or []:
            targets = [t for t in by_name.get(i["target"], []) if not g.is_file_node(t)]
            owner = (_class_node(g, f, i["owner_class"], i["line"]) if i.get("owner_class")
                     else g.symbol_at(f, i["line"]))
            if len(targets) != 1 or not owner or owner == targets[0]:
                continue
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

def live_sqlite(path: Path) -> dict[str, list[str]]:
    """{table: columns} of a local SQLite database, opened read only."""
    import sqlite3

    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"no database file at {p}")
    uri = p.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' "
                                            "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {n.lower(): [r[1].lower() for r in conn.execute(f'PRAGMA table_info("{n}")')] for n in names}
    finally:
        conn.close()


def report(g, *, db: Path | None = None, table: str | None = None) -> dict:
    """The tables the code defines and uses, who reads and writes them, injections, and with ``db`` the
    differences from a live SQLite database."""
    from verinoda import index

    side = index._read_sidecar(g.root) or {}
    block = side.get("data_schema") if isinstance(side.get("data_schema"), dict) else None
    old = block.get("files") if block and block.get("facts_version") == FACTS_VERSION else None
    _files, _nodes, _edges, rep = collect(g, old=old)
    tables = rep["tables"]
    if table:
        tables = [t for t in tables if t["name"] == _norm(table)]
    out = {"tables": tables, "injections": rep["injections"], "unresolved": rep["unresolved"],
           "counts": rep["counts"], "status": "strong_inference" if tables else "none_found",
           "limits": [
               "read from the code: table names a framework derives (Django <app>_<model>, JPA snake_case, SQLModel) "
               "follow the framework's default and say so; a custom naming strategy or db_table set elsewhere is "
               "not seen",
               "reads and writes are calls on a model class, an instance built in the same function, a repository "
               "field or SQL strings; a model passed in as a parameter or a query built in pieces is not seen",
               "a SQL string is parsed by patterns (INSERT INTO, UPDATE ... SET, DELETE FROM, FROM, JOIN), not by a "
               "SQL parser",
           ]}
    if db is not None:
        live = live_sqlite(db)
        code = {t["name"]: t for t in rep["tables"]}
        diff = {"database": str(db), "only_in_database": sorted(set(live) - set(code)),
                "only_in_code": sorted(n for n, t in code.items() if t["defs"] and n not in live),
                "columns": []}
        for name in sorted(set(live) & set(code)):
            want = set(code[name]["columns"])
            have = set(live[name])
            if want and (want - have or have - want):
                diff["columns"].append({"table": name, "only_in_code": sorted(want - have),
                                        "only_in_database": sorted(have - want)})
        diff["status"] = "observed"
        diff["basis"] = "the database file as read now (read only); the code side as above"
        out["live"] = diff
    return out


def render(res: dict) -> str:
    c = res["counts"]
    out = [f"{c['tables']} table(s) ({c['defined']} defined in the code), {c['models']} model(s), "
           f"{c['edges']} edge(s), {c['injections']} injection(s)"]
    for t in res["tables"]:
        reads = [u for u in t["uses"] if u["op"] == "read"]
        writes = [u for u in t["uses"] if u["op"] == "write"]
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
        for d in live["columns"]:
            out.append(f"      {d['table']}: columns only in the code {d['only_in_code'] or '-'}, only in the "
                       f"database {d['only_in_database'] or '-'}")
    return "\n".join(out) + "\n"
