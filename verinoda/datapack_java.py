"""Java calls into a datapack's functions (docs/DESIGN.md D69).

A mod's Java runs its datapack functions in three ways, and :func:`scan` finds each call as its own record:

- a **command string**: a string that is a command running ``function ns:path`` - ``"function ns:x"``,
  ``"execute as " + name + " run function ns:x"``, ``"schedule function ns:x 20t"`` - handed to the command
  dispatcher (``performPrefixedCommand``, ``runCommand``, Bukkit's ``dispatchCommand``). The text must read as a
  command: prose that mentions one ("run /function ns:x to clean up") is not a call;
- an **identifier**: the function manager's lookup (``getFunctions().get(...)``, Yarn's
  ``getCommandFunctionManager().getFunction(...)``, directly or through a local holding the manager) of an
  identifier built from constants (``Identifier.fromNamespaceAndPath("ns", "path")``, ``Identifier.parse("ns:path")``,
  ``ResourceLocation``). The same identifier names item models, textures and registry entries, so only one that
  reaches the lookup counts;
- a **helper**: a method whose String parameter reaches that lookup with a constant namespace
  (``fromNamespaceAndPath("ns", name)``, ``"ns:" + name``, ``"prefix_" + name``); every call of it with a
  constant binds to the function the constant names. A helper is recognised by its body, never by its name,
  and is its own declaration (a project may have several same-named helpers, and same-named classes in two
  packages or trees); a call binds to it only when Java would resolve the call to it: a qualifier the caller's
  file can see (same file or package, an import, a fully qualified name), or, unqualified, the innermost
  enclosing class that declares a method of that name, or a static import. A method that passes its own
  parameter to a helper is a helper too, an overload that delegates to it included.
  ``datapack.function_helpers`` in ``.verinoda/config.json`` (``["Class.method:argIndex:namespace"]``) names one
  whose body is beyond reading.

A constant, a local assigned once, a ``String.format`` / ``formatted`` argument and a loop over a constant table
(``for (String[] e : TABLE) ... e[1]``, ``for (String n : List.of(...))``, the table in a local or a final field)
are read; a name built at run time from anything else (``"prefix_" + x``, a command argument) cannot be bound:
it is reported as dynamic, with the part that is known (``ns:prefix_*``). The code is read (tree-sitter), not
run, so a call is a lead, as for the rest of the datapack view.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

VIA = ("command-string", "identifier", "helper")
_MANAGERS = {"getFunctions", "getCommandFunctionManager", "getFunctionManager"}
_MANAGER_TYPES = {"ServerFunctionManager", "CommandFunctionManager", "FunctionManager"}
_LOOKUPS = {"get", "getFunction"}
_ID_TYPES = ("Identifier", "ResourceLocation")
_ID_PAIR = {"fromNamespaceAndPath", "of", "tryBuild"}             # (namespace, path)
_ID_WHOLE = {"parse", "tryParse", "of", "bySeparator"}            # ("ns:path")
_SAME_NAME = {"toLowerCase", "trim", "strip", "intern"}          # a String method that keeps the name
_SEQUENCES = {"List", "Set", "Arrays", "Stream", "ImmutableList", "ImmutableSet"}   # List.of(...), Arrays.asList
_SCOPES = {"method_declaration", "constructor_declaration", "compact_constructor_declaration",
           "static_initializer", "field_declaration"}
_TYPES = {"class_declaration", "interface_declaration", "enum_declaration", "record_declaration"}
# a command string: the text starts as a command, or with a part built at run time followed by an execute step
_HEAD = re.compile(r"\s*/?(?:execute|function|schedule|return)\s")
_DYN_HEAD = re.compile(r"\x00\s*(?:run|as|at|positioned|rotated|facing|align|anchored|in|on|summon|if|unless|"
                       r"store|function|schedule)\s")
_FN = re.compile(r"(?:^|(?<=[\s/]))(schedule\s+)?function\s+(?:(#?)([a-z0-9_.-]+):([a-z0-9_./-]*)|(?=\x00))")
_TAIL = re.compile(r"\s*$|\s*\{|\s+with\s|\s+\x00")
_SCHED_TAIL = re.compile(r"\s+(?:\d+(?:\.\d+)?[tsd]?|\x00\S*)(?:\s+(?:append|replace))?\s*$")
_MANAGER_RX = re.compile(r"getFunctions|getCommandFunctionManager|getFunctionManager")
_STRING_RX = re.compile(r'"[^"\n]*\bfunction\s|"""')   # a file with a string that may be a command
_COND_TAIL = re.compile(r"\s+(?:run|if|unless|as|at|positioned|store)\s")
# a String.format / formatted specifier: %s, %2$s, %-10d; %% and %n are text
_FORMAT_SPEC = re.compile(r"%(?:(\d+)\$)?[-#+ 0,(<]*\d*(?:\.\d+)?([a-zA-Z%])")
_PACKAGE = re.compile(r"^\s*package\s+([\w$.]+)\s*;", re.MULTILINE)
_IMPORT = re.compile(r"^\s*import\s+(static\s+)?([\w$]+(?:\.[\w$]+)*?)(\.\*)?\s*;", re.MULTILINE)


@dataclass(frozen=True)
class JavaCall:
    target: str           # ns:path; for a dynamic call, the known start of the name (ns:prefix_)
    file: str
    line: int
    via: str              # command-string | identifier | helper
    caller: str           # Class.method the call is written in (Class alone in a field initialiser)
    how: str              # command strings: function | execute run | schedule; otherwise "lookup"
    helper: str | None = None     # Class.method of the helper (via == "helper")
    tree: str | None = None       # the reference tree (index.reference) the file is in
    dynamic: bool = False
    text: str = ""

    @property
    def at(self) -> str:
        return f"{self.file}:{self.line}"

    @property
    def shown(self) -> str:
        """The target as printed: ``ns:prefix_*`` for a dynamic call."""
        return self.target + "*" if self.dynamic else self.target

    def matches(self, fid: str) -> bool:
        return fid.startswith(self.target) if self.dynamic else fid == self.target

    def record(self) -> dict:
        out = {"kind": "java", "via": self.via, "target": self.shown, "caller": self.caller, "at": self.at,
               "how": self.how}
        if self.helper:
            out["helper"] = self.helper
        if self.tree:
            out["tree"] = self.tree
        if self.dynamic:
            out["dynamic"] = True
        from verinoda.testcode import is_test_file

        if is_test_file(self.file):
            out["test"] = True
        out["text"] = self.text
        return out


@dataclass
class Helper:
    cls: str
    name: str
    arity: int | None      # None: any (a configured helper)
    arg: int               # the parameter that carries the name
    prefix: str            # what the helper puts before it ("ns:", "ns:prefix_"); "" when it takes a whole id
    as_is: bool = False    # an argument holding ':' is used as the whole id
    file: str = ""
    line: int = 0
    source: str = "body"   # body | config | forwards to Class.method
    tree: str | None = None
    takes_id: bool = False  # the parameter is an Identifier / ResourceLocation, not a String
    pkg: str = ""          # the package of its file
    path: str = ""         # its class with the classes around it (Outer.Inner); "" for a configured helper
    start: int = -1        # the start byte of its declaration in ``file`` (-1: a configured helper)

    @property
    def label(self) -> str:
        return f"{self.cls}.{self.name}"

    def bind(self, name: str) -> str:
        if (self.as_is or not self.prefix) and ":" in name:
            return name
        return (self.prefix or "minecraft:") + name

    def record(self) -> dict:
        out = {"helper": self.label, "at": f"{self.file}:{self.line}" if self.file else None, "arg": self.arg,
               "prefix": self.prefix, "source": self.source}
        if self.arity is not None:
            out["arity"] = self.arity
        if self.tree:
            out["tree"] = self.tree
        return out


@dataclass
class Result:
    calls: list[JavaCall] = field(default_factory=list)
    helpers: list[Helper] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)  # lookups of an id the text does not spell
    files_read: int = 0


# -- values of expressions -------------------------------------------------------------------------------
# ("const", s) | ("consts", (s, ...)) | ("param", index, prefix, as_is) | ("prefix", s) | None (nothing known);
# "consts" is one of several constants: a loop over a constant table (``for (String[] e : TABLE) ... e[1]``)

_MAX_CONSTS = 64


def _strs(v) -> tuple[str, ...]:
    return (v[1],) if v[0] == "const" else v[1]


def _consts(vals) -> tuple | None:
    vals = tuple(dict.fromkeys(vals))
    if not vals or len(vals) > _MAX_CONSTS:
        return None
    return ("const", vals[0]) if len(vals) == 1 else ("consts", vals)


def _concat(a, b):
    if a is None:
        return None
    if a[0] == "consts" or (b is not None and b[0] == "consts"):
        if b is not None and a[0] in ("const", "consts") and b[0] in ("const", "consts"):
            return _consts(x + y for x in _strs(a) for y in _strs(b))
        known = os.path.commonprefix(list(_strs(a))) if a[0] in ("const", "consts") else (
            a[2] if a[0] == "param" else a[1])
        return ("prefix", known) if known else None
    if a[0] == "const":
        if b is None:
            return ("prefix", a[1]) if a[1] else None
        if b[0] == "const":
            return ("const", a[1] + b[1])
        if b[0] == "param":
            return ("param", b[1], a[1] + b[2], False) if not b[3] else (("prefix", a[1]) if a[1] else None)
        return ("prefix", a[1] + b[1])
    known = a[2] if a[0] == "param" else a[1]  # something after a parameter: only the start is known
    return ("prefix", known) if known else None


def _either(a, b):
    """``c ? a : b``: the same value, or the whole-id-or-path form ``fn.contains(":") ? fn : "ns:" + fn``."""
    if a == b:
        return a
    if a and b and a[0] == b[0] == "param" and a[1] == b[1]:
        plain, pre = (a, b) if not a[2] else (b, a)
        if not plain[2] and ":" in pre[2]:
            return ("param", a[1], pre[2], True)
    return None


def _literal(src: bytes, n) -> str:
    raw = src[n.start_byte:n.end_byte].decode("utf-8", "replace")
    body = raw[3:-3] if raw.startswith('"""') else raw[1:-1]
    return re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t"}.get(m.group(1), m.group(1)), body)


class _File:
    """One Java file's syntax tree. Nodes are found from the text (a byte offset the regex finds, then the node
    there), not by walking every node, so a large file costs its parse and little more."""

    def __init__(self, rel: str, text: str, tree_label: str | None, consts: dict[str, str]):
        from verinoda.codecheck_java import _parser

        self.rel, self.tree = rel, tree_label
        self.src = text.encode("utf-8")
        self.lines = text.splitlines()
        self.root = _parser().parse(self.src).root_node
        self.consts = consts
        self._scopes: dict[tuple[int, int], _Scope] = {}
        self._fields: dict | None = None
        m = _PACKAGE.search(text)
        self.pkg = m.group(1) if m else ""
        # (static, name, wildcard): `import a.b.C;` -> (False, "a.b.C", False); `import a.b.*;` -> (False, "a.b", True)
        self.imports = [(bool(m.group(1)), m.group(2), bool(m.group(3))) for m in _IMPORT.finditer(text)]

    def invocations(self, names) -> list:
        """The calls of methods named in ``names`` (code only: a name in a comment or a string is not a node)."""
        if not names:
            return []
        rx = re.compile(rb"(?<![\w$])(" + b"|".join(re.escape(n.encode()) for n in sorted(names)) + rb")\s*\(")
        out = []
        for m in rx.finditer(self.src):
            n = self.root.descendant_for_byte_range(m.start(1), m.end(1))
            p = n.parent if n is not None and n.type == "identifier" else None
            if p is not None and p.type == "method_invocation" and p.child_by_field_name("name") == n:
                out.append(p)
        return out

    def strings(self, word: bytes) -> list:
        """The string literals that hold ``word``."""
        out, seen = [], set()
        for m in re.finditer(re.escape(word), self.src):
            n = self.root.descendant_for_byte_range(m.start(), m.end())
            while n is not None and n.type in ("string_fragment", "multiline_string_fragment", "escape_sequence"):
                n = n.parent
            if n is not None and n.type == "string_literal" and n.start_byte not in seen:
                seen.add(n.start_byte)
                out.append(n)
        return out

    def text(self, n) -> str:
        return self.src[n.start_byte:n.end_byte].decode("utf-8", "replace")

    def line_text(self, line: int) -> str:
        return self.lines[line - 1].strip()[:160] if 0 < line <= len(self.lines) else ""

    def scope(self, n) -> _Scope:
        s = n
        while s is not None and s.type not in _SCOPES:
            s = s.parent
        s = s or self.root
        key = (s.start_byte, s.end_byte)
        if key not in self._scopes:
            self._scopes[key] = _Scope(self, s)
        return self._scopes[key]

    def where(self, n) -> tuple[str, str | None]:
        """(class, method) around ``n``: the innermost named type, the innermost method or constructor."""
        cls = meth = None
        p = n
        while p is not None:
            if meth is None and p.type in ("method_declaration", "constructor_declaration"):
                nm = p.child_by_field_name("name")
                meth = self.text(nm) if nm is not None else None
            if p.type in _TYPES:
                nm = p.child_by_field_name("name")
                if nm is not None:
                    cls = self.text(nm)
                    break
            p = p.parent
        return cls or Path(self.rel).stem, meth

    def caller(self, n) -> str:
        cls, meth = self.where(n)
        return f"{cls}.{meth}" if meth else cls

    def field(self, name: str):
        """The initialiser of the ``final`` field ``name`` declared in this file (None: none, or two of them)."""
        if self._fields is None:
            seen: dict[str, list] = {}
            stack = [self.root]
            while stack:
                n = stack.pop()
                if n.type == "field_declaration":
                    mods = next((c for c in n.children if c.type == "modifiers"), None)
                    if n.parent is not None and n.parent.type == "interface_body" or (  # constants
                            mods is not None and re.search(r"\bfinal\b", self.text(mods))):
                        for d in n.named_children:
                            nm = d.child_by_field_name("name") if d.type == "variable_declarator" else None
                            if nm is not None:
                                seen.setdefault(self.text(nm), []).append(d.child_by_field_name("value"))
                elif n.type in ("program", "class_body", "enum_body", "enum_body_declarations", "interface_body") \
                        or n.type in _TYPES:
                    stack.extend(n.named_children)
            self._fields = {k: v[0] for k, v in seen.items() if len(v) == 1 and v[0] is not None}
        return self._fields.get(name)

    def type_path(self, n) -> str:
        """The named types around ``n``, outermost first (``Outer.Inner``)."""
        names = []
        p = n
        while p is not None:
            if p.type in _TYPES:
                nm = p.child_by_field_name("name")
                if nm is not None:
                    names.append(self.text(nm))
            p = p.parent
        return ".".join(reversed(names))

    def enclosing_types(self, n) -> list[tuple[str | None, object]]:
        """``(name, body)`` of the types around ``n``, innermost first; an anonymous class has no name."""
        out = []
        p = n.parent
        while p is not None:
            if p.type in _TYPES:
                nm, body = p.child_by_field_name("name"), p.child_by_field_name("body")
                if body is not None:
                    out.append((self.text(nm) if nm is not None else None, body))
            elif p.type == "object_creation_expression":
                body = next((c for c in p.named_children if c.type == "class_body"), None)
                if body is not None and body.start_byte <= n.start_byte and n.end_byte <= body.end_byte:
                    out.append((None, body))
            p = p.parent
        return out

    def methods(self, body, name: str) -> list:
        """The methods named ``name`` a type's body declares itself."""
        out = []
        for c in body.named_children:
            for d in (c.named_children if c.type == "enum_body_declarations" else [c]):
                if d.type == "method_declaration":
                    nm = d.child_by_field_name("name")
                    if nm is not None and self.text(nm) == name:
                        out.append(d)
        return out


class _Scope:
    """A method (or initialiser): its String/identifier parameters and its locals. A local is kept as
    ``(kind, scope node, start byte, node)``: kind "var" (node: its initialiser), "loop" (an enhanced-for variable;
    node: what it loops over) or "other" (a catch or lambda parameter)."""

    def __init__(self, f: _File, node):
        self.f, self.node = f, node
        self.params: dict[str, int] = {}
        self.param_types: dict[str, str] = {}
        self.locals: dict[str, list] = {}
        self.local_types: dict[str, str] = {}
        self.assigned: set[str] = set()
        ps = node.child_by_field_name("parameters") if node.type in ("method_declaration",
                                                                      "constructor_declaration") else None
        if ps is not None:
            for k, p in enumerate(c for c in ps.named_children if c.type in ("formal_parameter", "spread_parameter")):
                nm, ty = p.child_by_field_name("name"), p.child_by_field_name("type")
                if nm is None:  # a spread parameter keeps its name in a declarator
                    d = next((c for c in p.named_children if c.type == "variable_declarator"), None)
                    nm = d.child_by_field_name("name") if d is not None else None
                if nm is not None:
                    self.params[f.text(nm)] = k
                    self.param_types[f.text(nm)] = f.text(ty) if ty is not None else ""
        body = None
        if node.type == "static_initializer":
            body = node.named_children[-1] if node.named_children else None
        elif node.type != "field_declaration":
            body = node.child_by_field_name("body")
        stack = [body] if body is not None else []
        while stack:
            n = stack.pop()
            t = n.type
            if t == "local_variable_declaration":
                ty = n.child_by_field_name("type")
                for d in n.named_children:
                    if d.type == "variable_declarator":
                        nm = d.child_by_field_name("name")
                        if nm is not None:
                            name = f.text(nm)
                            self.locals.setdefault(name, []).append(
                                ("var", n.parent or n, d.start_byte, d.child_by_field_name("value")))
                            self.local_types[name] = f.text(ty) if ty is not None else ""
            elif t == "enhanced_for_statement":
                nm = n.child_by_field_name("name")
                if nm is not None:
                    self.locals.setdefault(f.text(nm), []).append(("loop", n, n.start_byte,
                                                                   n.child_by_field_name("value")))
            elif t == "catch_formal_parameter":
                nm = n.child_by_field_name("name")  # a catch variable: nothing known of it
                if nm is not None:
                    self.locals.setdefault(f.text(nm), []).append(("other", n.parent or n, n.start_byte, None))
            elif t == "lambda_expression":
                ps2 = n.child_by_field_name("parameters")
                if ps2 is not None:
                    ids = [ps2] if ps2.type == "identifier" else [
                        c.child_by_field_name("name") or c for c in ps2.named_children]
                    for c in ids:
                        if c is not None and c.type == "identifier":
                            self.locals.setdefault(f.text(c), []).append(("other", n, n.start_byte, None))
            elif t == "assignment_expression":
                left = n.child_by_field_name("left")
                if left is not None and left.type == "identifier":
                    self.assigned.add(f.text(left))
            elif t in ("line_comment", "block_comment", "string_literal"):
                continue
            stack.extend(n.children)

    def _entry(self, name: str, at):
        """The declaration of the local ``name`` that the use ``at`` sees; None when the local is assigned again
        or the declaration is not certain."""
        entries = self.locals.get(name) or []
        if name in self.assigned or not entries:
            return None
        if len(entries) > 1 and at is not None:  # the same name in two blocks: the one around the use
            entries = [e for e in entries if e[1].start_byte <= at.start_byte < e[1].end_byte and e[2] <= at.start_byte]
        return entries[0] if len(entries) == 1 else None

    def _local(self, name: str, at=None):
        """The one initialiser of a local never assigned again; False when the name is not a local."""
        if name not in self.locals:
            return False
        e = self._entry(name, at)
        return e[3] if e is not None and e[0] == "var" else None

    def _loop(self, name: str, at=None):
        """What the enhanced-for variable ``name`` loops over (None when it is not one)."""
        e = self._entry(name, at)
        return e[3] if e is not None and e[0] == "loop" else None

    def elements(self, it, k: int | None, depth: int = 0):
        """The constants a loop over ``it`` gives (``k``: the column of a row, ``e[k]``): a local or final field
        holding an array initialiser, ``new String[]{...}``, ``List.of(...)``, ``Arrays.asList(...)``."""
        while it is not None and it.type == "parenthesized_expression" and it.named_children:
            it = it.named_children[0]
        if it is None or depth > 8:
            return None
        if it.type == "identifier":
            name = self.f.text(it)
            init = self._local(name, it)
            if init is False:  # not a local: a final field of this file (a parameter of the name hides it)
                init = self.f.field(name) if name not in self.params else None
                return self.f.scope(init).elements(init, k, depth + 1) if init is not None else None
            return self.elements(init, k, depth + 1) if init is not None else None
        if it.type == "array_creation_expression":
            it = it.child_by_field_name("value")
        if it is not None and it.type == "array_initializer":
            items = [c for c in it.named_children if c.type not in ("line_comment", "block_comment")]
        elif it is not None and it.type == "method_invocation":
            nm, obj = it.child_by_field_name("name"), it.child_by_field_name("object")
            if nm is None or obj is None or self.f.text(nm) not in ("of", "asList") \
                    or self.f.text(obj).rsplit(".", 1)[-1] not in _SEQUENCES:
                return None
            items = _args(it)
        else:
            return None
        vals: list[str] = []
        for e in items:
            if k is not None:
                if e.type == "array_creation_expression":
                    e = e.child_by_field_name("value")
                row = [c for c in e.named_children if c.type not in ("line_comment", "block_comment")] \
                    if e is not None and e.type == "array_initializer" else []
                if k >= len(row):
                    return None
                e = row[k]
            v = self.value(e, depth + 1)
            if v is None or v[0] not in ("const", "consts"):
                return None
            vals.extend(_strs(v))
        return _consts(vals)

    def const(self, n) -> str | None:
        """A ``static final String`` constant named by ``n`` (``NAME``, ``Owner.NAME``)."""
        if n.type == "identifier":
            name = self.f.text(n)
            if name in self.locals or name in self.params:
                return None
        elif n.type == "field_access":
            fld = n.child_by_field_name("field")
            name = self.f.text(fld) if fld is not None else ""
        else:
            return None
        return self.f.consts.get(name)

    def value(self, n, depth: int = 0):
        if n is None or depth > 8:
            return None
        t = n.type
        if t == "string_literal":
            return ("const", _literal(self.f.src, n))
        if t == "parenthesized_expression":
            return self.value(n.named_children[0] if n.named_children else None, depth + 1)
        if t == "binary_expression":
            op = n.child_by_field_name("operator")
            if op is None or op.type != "+":
                return None
            return _concat(self.value(n.child_by_field_name("left"), depth + 1),
                           self.value(n.child_by_field_name("right"), depth + 1))
        if t == "ternary_expression":
            return _either(self.value(n.child_by_field_name("consequence"), depth + 1),
                           self.value(n.child_by_field_name("alternative"), depth + 1))
        if t == "identifier":
            name = self.f.text(n)
            init = self._local(name, n)
            if init is not False:
                if init is None:
                    it = self._loop(name, n)  # for (String name : TABLE): each constant of the table
                    return self.elements(it, None, depth + 1) if it is not None else None
                return self.value(init, depth + 1)
            if name in self.params and name not in self.assigned:
                return ("param", self.params[name], "", False)
        if t == "array_access":  # for (String[] e : TABLE) ... e[1]: that column of each row
            arr, idx = n.child_by_field_name("array"), n.child_by_field_name("index")
            if arr is None or arr.type != "identifier" or idx is None or idx.type != "decimal_integer_literal":
                return None
            it = self._loop(self.f.text(arr), arr)
            return self.elements(it, int(self.f.text(idx)), depth + 1) if it is not None else None
        if t == "method_invocation":  # name.toLowerCase(Locale.ROOT), name.trim(): still the name
            nm, obj = n.child_by_field_name("name"), n.child_by_field_name("object")
            how = self.f.text(nm) if nm is not None else ""
            if obj is None or how not in _SAME_NAME:
                return None
            v = self.value(obj, depth + 1)
            if v is not None and v[0] in ("const", "consts"):
                return _consts(s.lower() if how == "toLowerCase" else s.strip() for s in _strs(v))
            return v if v is not None and v[0] == "param" else None
        c = self.const(n)
        return ("const", c) if c is not None else None

    def id_value(self, n, depth: int = 0):
        """The function id an identifier expression names, as a value of its whole ``ns:path``."""
        if n is None or depth > 8:
            return None
        t = n.type
        if t == "parenthesized_expression":
            return self.id_value(n.named_children[0] if n.named_children else None, depth + 1)
        if t == "identifier":
            name = self.f.text(n)
            init = self._local(name, n)
            if init is not False:
                return self.id_value(init, depth + 1) if init is not None else None
            if name in self.params:
                if name not in self.assigned and self.param_types.get(name, "").endswith(_ID_TYPES):
                    return ("param", self.params[name], "", True)
                return None
            init = self.f.field(name)  # static final Identifier TICK = Identifier.fromNamespaceAndPath(...)
            return self.f.scope(init).id_value(init, depth + 1) if init is not None else None
        args = n.child_by_field_name("arguments")
        vals = [c for c in args.named_children if c.type not in ("line_comment", "block_comment")] \
            if args is not None else []
        if t == "method_invocation":
            obj, nm = n.child_by_field_name("object"), n.child_by_field_name("name")
            if obj is None or nm is None or not self.f.text(obj).endswith(_ID_TYPES):
                return None
            name = self.f.text(nm)
            if name == "withDefaultNamespace" and len(vals) == 1:
                return _pair(("const", "minecraft"), self.value(vals[0], depth + 1))
            if name in _ID_PAIR and len(vals) == 2:
                return _pair(self.value(vals[0], depth + 1), self.value(vals[1], depth + 1))
            if name in _ID_WHOLE and len(vals) == 1:
                return _whole(self.value(vals[0], depth + 1))
            return None
        if t == "object_creation_expression":
            ty = n.child_by_field_name("type")
            if ty is None or not self.f.text(ty).endswith(_ID_TYPES):
                return None
            if len(vals) == 2:
                return _pair(self.value(vals[0], depth + 1), self.value(vals[1], depth + 1))
            if len(vals) == 1:
                return _whole(self.value(vals[0], depth + 1))
        return None

    def is_manager(self, n) -> bool:
        """``server.getFunctions()``, or a local holding it (``ServerFunctionManager functions = ...``)."""
        if n is None:
            return False
        if n.type == "method_invocation":
            nm = n.child_by_field_name("name")
            return nm is not None and self.f.text(nm) in _MANAGERS
        if n.type == "identifier":
            name = self.f.text(n)
            if self.local_types.get(name, "").rsplit(".", 1)[-1] in _MANAGER_TYPES:
                return True
            init = self._local(name, n)
            return bool(init) and self.is_manager(init)
        return False


def _pair(ns, path):
    if not ns or ns[0] != "const" or not re.fullmatch(r"[a-z0-9_.-]+", ns[1]):
        return None
    head = ns[1] + ":"
    if path is None:
        return ("prefix", head)
    if path[0] in ("const", "consts"):
        return _consts(head + p for p in _strs(path))
    if path[0] == "param":
        return ("param", path[1], head + path[2], False) if not path[3] else ("prefix", head)
    return ("prefix", head + path[1])


def _whole(v):
    if v is None:
        return None
    if v[0] in ("const", "consts"):
        return _consts(s if ":" in s else "minecraft:" + s for s in _strs(v))
    if v[0] == "param":
        return v if (not v[2] or ":" in v[2]) else None
    return v if ":" in v[1] else None


def _args(inv) -> list:
    a = inv.child_by_field_name("arguments")
    return [c for c in a.named_children if c.type not in ("line_comment", "block_comment")] if a is not None else []


# -- the scan ----------------------------------------------------------------------------------------------

def reference_trees(repo: Path) -> list[tuple[str, str, list[str]]]:
    """``(prefix/, name, aliases)`` of the configured reference trees (``index.reference``)."""
    try:
        from verinoda.paths import load_config

        entries = (load_config(Path(repo)).get("index") or {}).get("reference") or []
    except Exception:  # noqa: BLE001 - an unreadable config only means no reference trees
        return []
    out = []
    for e in entries:
        path = e.get("path") if isinstance(e, dict) else e
        if isinstance(path, str) and path.strip("/"):
            aliases = [a for a in (e.get("aliases") or []) if isinstance(a, str)] if isinstance(e, dict) else []
            out.append((path.strip("/") + "/", path.strip("/"), aliases))
    return out


def configured_helpers(repo: Path) -> list[Helper]:
    """``datapack.function_helpers`` (or ``function-helpers``): ``"Class.method:argIndex[:namespace]"``."""
    try:
        from verinoda.paths import load_config

        dp = load_config(Path(repo)).get("datapack") or {}
    except Exception:  # noqa: BLE001 - an unreadable config: no configured helpers
        return []
    rows = dp.get("function_helpers") or dp.get("function-helpers") or [] if isinstance(dp, dict) else []
    out = []
    for r in rows if isinstance(rows, list) else []:
        m = re.fullmatch(r"\s*([\w$.]+)\.([\w$]+)\s*:\s*(\d+)\s*(?::\s*([a-z0-9_.-]+)(?::([a-z0-9_./-]*))?)?\s*",
                         str(r))
        if m:
            cls = m.group(1).rsplit(".", 1)[-1]
            prefix = f"{m.group(4)}:{m.group(5) or ''}" if m.group(4) else ""
            out.append(Helper(cls, m.group(2), None, int(m.group(3)), prefix, as_is=bool(prefix), source="config"))
    return out


def scan(repo: Path, texts: dict[str, str], *, consts: dict[str, str] | None = None) -> Result:
    """Every Java call into a datapack function in ``texts`` (``{relative path: source}``)."""
    trees = reference_trees(repo)

    def tree_of(rel: str) -> str | None:
        return next((name for pre, name, _a in trees if rel.startswith(pre)), None)

    const_map = dict(consts or {})
    files: dict[str, _File] = {}

    def parsed(rel: str) -> _File | None:
        if rel not in files:
            try:
                from verinoda.datapack import _J_STR_CONST

                own = {m.group(1): m.group(2) for m in _J_STR_CONST.finditer(texts[rel])}
                files[rel] = _File(rel, texts[rel], tree_of(rel), {**const_map, **own})
            except Exception:  # noqa: BLE001 - a file tree-sitter cannot read is left out, not fatal
                files[rel] = None  # type: ignore[assignment]
        return files[rel]

    res = Result()
    helpers: list[Helper] = configured_helpers(repo)
    seen_helper: set[tuple] = {_key(h) for h in helpers}

    def add_helper(h: Helper | None) -> bool:
        if h is None or _key(h) in seen_helper:
            return False
        seen_helper.add(_key(h))
        helpers.append(h)
        return True

    # 1. lookups: a constant id is a call; a parameter reaching it makes its method a helper
    for rel in [rel for rel, t in texts.items() if _MANAGER_RX.search(t)]:
        f = parsed(rel)
        if f is None:
            continue
        for inv in f.invocations(_LOOKUPS):
            args = _args(inv)
            sc = f.scope(inv)
            if len(args) != 1 or not sc.is_manager(inv.child_by_field_name("object")):
                continue
            v = sc.id_value(args[0])
            line = args[0].start_point[0] + 1
            if v is None:
                res.unresolved.append(f"{rel}:{line}")
                continue
            if v[0] == "param":
                add_helper(_helper_of(f, sc, inv, v, "body", takes_id=_is_id_param(sc, v[1])))
                continue
            for target in (_strs(v) if v[0] in ("const", "consts") else (v[1],)):
                res.calls.append(JavaCall(target, rel, line, "identifier", f.caller(inv), "lookup", None, f.tree,
                                          v[0] == "prefix", f.line_text(line)))

    # 2. command strings
    for rel in [rel for rel, t in texts.items() if _STRING_RX.search(t)]:
        f = parsed(rel)
        if f is None:
            continue
        done: set[tuple[int, int]] = set()
        for s in f.strings(b"function"):
            top = s
            while top.parent is not None and (top.parent.type == "parenthesized_expression" or (
                    top.parent.type == "binary_expression"
                    and (top.parent.child_by_field_name("operator") or top).type == "+")):
                top = top.parent
            key = (top.start_byte, top.end_byte)
            if key in done or b"function" not in f.src[top.start_byte:top.end_byte]:
                continue
            done.add(key)
            calls, params = _command_calls(f, top)
            res.calls.extend(calls)
            for index, prefix in params:  # the method's parameter completes the name: a helper
                sc = f.scope(top)
                add_helper(_helper_of(f, sc, top, ("param", index, prefix, not prefix), "body"))

    # 3. helper calls; a method handing its own parameter to a helper is a helper too (a few rounds)
    found: list[JavaCall] = []
    for _round in range(5):
        names = {h.name for h in helpers}
        if not names:
            break
        found = []
        new = False
        # a file that calls a helper names its method (as a call, not inside another word) and its class (the
        # qualifier, an import, a declared type, or the class itself): a helper named `get` or `run` does not make
        # every file with `.get(` a candidate
        owners: dict[str, set[str]] = {}
        for h in helpers:
            owners.setdefault(h.name, set()).add(h.cls)
        checks = [(n, sorted(cs), re.compile(rf"(?<![\w$]){re.escape(n)}\s*\("),
                   re.compile(r"(?<![\w$])(?:" + "|".join(re.escape(c) for c in sorted(cs)) + r")(?![\w$])"))
                  for n, cs in sorted(owners.items())]
        for rel, t in texts.items():
            if not any(n in t and any(c in t for c in cs) and call.search(t) and owner.search(t)
                       for n, cs, call, owner in checks):  # the plain tests first: they are the fast ones
                continue
            f = parsed(rel)
            if f is None:
                continue
            for inv in f.invocations(names):
                nm = inv.child_by_field_name("name")
                args = _args(inv)
                sc = f.scope(inv)
                h = _pick(f, sc, inv, f.text(nm), len(args), helpers)
                if h is None or h.arg >= len(args):
                    continue
                v = sc.id_value(args[h.arg]) if h.takes_id else sc.value(args[h.arg])
                line = nm.start_point[0] + 1  # the name's line: a chained `.runFunction(` starts on its own
                if v is not None and v[0] == "param":
                    if _is_self(f, sc, inv, h):
                        continue  # a helper handing the name on to itself (an overload is another method)
                    fw = _helper_of(f, sc, inv, ("param", v[1], h.prefix + v[2], h.as_is and not v[2]),
                                    f"forwards to {h.label}", takes_id=h.takes_id)
                    new = add_helper(fw) or new
                    if fw is not None:
                        continue
                if v is not None and v[0] in ("const", "consts"):  # consts: each row of a constant table
                    targets, dyn = [x if h.takes_id else h.bind(x) for x in _strs(v)], False
                elif v is not None and v[0] == "prefix":
                    targets, dyn = [v[1] if h.takes_id or (h.as_is and ":" in v[1]) else h.prefix + v[1]], True
                else:
                    targets, dyn = [h.prefix], True
                found += [JavaCall(target, rel, line, "helper", f.caller(inv), "lookup", h.label, f.tree, dyn,
                                   f.line_text(line)) for target in targets]
        if not new:
            break
    res.calls.extend(found)
    res.helpers = helpers
    res.files_read = len([f for f in files.values() if f is not None])
    uniq: dict[tuple, JavaCall] = {}
    for c in res.calls:
        uniq.setdefault((c.file, c.line, c.target, c.via, c.dynamic), c)
    res.calls = sorted(uniq.values(), key=lambda c: (c.file, c.line, c.target))
    return res


def _is_id_param(sc: _Scope, index: int) -> bool:
    name = next((n for n, k in sc.params.items() if k == index), "")
    return sc.param_types.get(name, "").endswith(_ID_TYPES)


def _helper_of(f: _File, sc: _Scope, n, v, source: str, *, takes_id: bool = False) -> Helper | None:
    """The method around ``n`` as a helper whose parameter ``v[1]`` carries the name (None outside a method)."""
    if sc.node.type not in ("method_declaration", "constructor_declaration"):
        return None
    cls, meth = f.where(n)
    if meth is None:
        return None
    decl = sc.node.child_by_field_name("name") or sc.node
    return Helper(cls, meth, len(sc.params), v[1], v[2], v[3] or not v[2], f.rel, decl.start_point[0] + 1, source,
                  f.tree, takes_id, f.pkg, f.type_path(sc.node) or cls, sc.node.start_byte)


def _key(h: Helper) -> tuple:
    """One helper: its declaration (two classes of the same name in two packages are two helpers)."""
    return (h.file, h.start) if h.start >= 0 else (h.cls, h.name, h.arity, h.tree)


def _is_self(f: _File, sc: _Scope, inv, h: Helper) -> bool:
    """The call is written in the helper's own body (recursion), not in an overload that delegates to it."""
    if h.start >= 0:
        return h.file == f.rel and h.start == sc.node.start_byte
    cls, meth = f.where(inv)
    return (h.cls, h.name) == (cls, meth) and h.arity in (None, len(sc.params))


def _pick(f: _File, sc: _Scope, inv, name: str, arity: int, helpers: list[Helper]) -> Helper | None:
    """The helper a call names: the method Java resolves the call to, when that method is a helper. A qualified
    call names the class (``Mod.runFunction``, ``pkg.Mod.runFunction``, an instance of a declared type), which the
    caller's file must be able to see (the same file or package, an import, a fully qualified name); an unqualified
    call goes to the innermost enclosing class that declares a method of that name, or to a static import. A helper
    found by its body binds only calls in its own tree (a reference tree shares class and package names)."""
    cands = [h for h in helpers if h.name == name and (h.arity is None or h.arity == arity)
             and (h.source == "config" or h.tree == f.tree)]
    if not cands:
        return None
    obj = inv.child_by_field_name("object")
    if obj is None or obj.type in ("this", "super"):
        for tname, body in f.enclosing_types(inv):
            decls = f.methods(body, name)
            if not decls:
                continue  # Java looks in the class around this one
            conf = [h for h in cands if h.source == "config" and h.cls == tname]
            if conf:
                return conf[0]
            fits = [d for d in decls if _param_count(d) == arity]
            return next((h for h in cands for d in fits if h.file == f.rel and h.start == d.start_byte), None)
        stat = [h for h in cands if _static_import(f, h, name)]
        return stat[0] if len(stat) == 1 else None
    q = re.sub(r"\s+", "", f.text(obj))
    last = q.rsplit(".", 1)[-1]
    if not last[:1].isupper():  # an instance: the class its variable is declared with
        q = re.sub(r"\s+", "", (sc.local_types.get(last) or sc.param_types.get(last) or "").split("<", 1)[0])
        last = q.rsplit(".", 1)[-1]
    named = [h for h in cands if h.cls == last and (h.source == "config" or _sees(f, h, q))]
    if len({(h.file, h.arg, h.prefix) for h in named}) == 1:
        return named[0]
    return None


def _param_count(decl) -> int:
    ps = decl.child_by_field_name("parameters")
    if ps is None:
        return 0
    return len([c for c in ps.named_children if c.type in ("formal_parameter", "spread_parameter")])


def _sees(f: _File, h: Helper, q: str) -> bool:
    """Whether the file ``f`` can name the helper's class as ``q`` (``Mod``, ``Outer.Mod``, ``pkg.Outer.Mod``)."""
    if h.file == f.rel:
        return True
    path = (h.path or h.cls).split(".")
    if q == ".".join(([h.pkg] if h.pkg else []) + path):  # fully qualified
        return True
    segs = q.split(".")
    k = len(path) - len(segs)
    if k < 0 or path[k:] != segs:
        return False
    if k == 0 and f.pkg == h.pkg:  # a top-level class of the same package
        return True
    home = ".".join(([h.pkg] if h.pkg else []) + path[:k])  # where the class the call names first is declared
    return any((wild and name == home) or (not wild and name == f"{home}.{path[k]}".lstrip("."))
               for _static, name, wild in f.imports)


def _static_import(f: _File, h: Helper, name: str) -> bool:
    """``import static pkg.Mod.runFunction;`` / ``import static pkg.Mod.*;`` in the caller's file."""
    if h.source == "config":
        text = f.src.decode("utf-8", "replace")
        return bool(re.search(rf"import\s+static\s+[\w.]*\b{re.escape(h.cls)}\.(?:\*|{re.escape(name)})\s*;", text))
    owner = ".".join(([h.pkg] if h.pkg else []) + (h.path or h.cls).split("."))
    return any(static and ((wild and imp == owner) or (not wild and imp == f"{owner}.{name}"))
               for static, imp, wild in f.imports)


def _format_args(f: _File, n) -> list | None:
    """The arguments a format string ``n`` is filled with: ``String.format(n, ...)`` (after a ``Locale``) or
    ``n.formatted(...)``; None when ``n`` is not one."""
    p = n.parent
    if p is None:
        return None
    if p.type == "method_invocation" and p.child_by_field_name("object") == n:
        nm = p.child_by_field_name("name")
        return _args(p) if nm is not None and f.text(nm) == "formatted" else None
    if p.type != "argument_list" or p.parent is None or p.parent.type != "method_invocation":
        return None
    inv = p.parent
    nm, obj = inv.child_by_field_name("name"), inv.child_by_field_name("object")
    if nm is None or f.text(nm) != "format" or obj is None or f.text(obj).rsplit(".", 1)[-1] != "String":
        return None
    args = _args(inv)
    at = next((i for i, a in enumerate(args) if a == n), -1)
    return args[at + 1:] if at in (0, 1) else None


def _command_calls(f: _File, top) -> tuple[list[JavaCall], list[tuple[int, str]]]:
    """The ``function`` calls of one string expression that reads as a command, and ``(parameter index, prefix)``
    where the enclosing method's own parameter completes the name (``"function ns:" + name``: a helper)."""
    sc = f.scope(top)
    # (text, or None when built at run time; line; param; a text block, whose lines are the source's)
    pieces: list[tuple[str | None, int, int | None, bool]] = []

    def part(n):
        """A part built at run time: a constant, the method's own parameter, or nothing known."""
        c = _literal(f.src, n) if n.type == "string_literal" else sc.const(n)  # a literal: a format argument
        v = sc.value(n) if c is None and n.type == "identifier" else None
        pieces.append((c, n.start_point[0] + 1, v[1] if v and v[0] == "param" and not v[2] else None, False))

    def flat(n):
        if n.type == "parenthesized_expression" and n.named_children:
            flat(n.named_children[0])
        elif n.type == "binary_expression" and (n.child_by_field_name("operator") or n).type == "+":
            flat(n.child_by_field_name("left"))
            flat(n.child_by_field_name("right"))
        elif n.type == "string_literal":
            body, line, block = _literal(f.src, n), n.start_point[0] + 1, f.src[n.start_byte:n.start_byte + 3] == b'"""'
            fargs = _format_args(f, n)
            if fargs is None:
                pieces.append((body, line, None, block))
                return
            # String.format("function ns:%s", x) / "function ns:%s".formatted(x): each %s is its argument
            k, last = 0, 0
            for m in _FORMAT_SPEC.finditer(body):
                pieces.append((body[last:m.start()], line + (body[:last].count("\n") if block else 0), None, block))
                last = m.end()
                if m.group(2) in "%n":
                    pieces.append(("%" if m.group(2) == "%" else "\n", line, None, False))
                    continue
                index = int(m.group(1)) - 1 if m.group(1) else k
                k += 0 if m.group(1) else 1
                if 0 <= index < len(fargs):
                    part(fargs[index])
                else:
                    pieces.append((None, line, None, False))
            pieces.append((body[last:], line + (body[:last].count("\n") if block else 0), None, block))
        else:
            part(n)

    flat(top)
    text, starts, param_at = "", [], {}
    for s, line, param, block in pieces:
        starts.append((len(text), line, block))
        if s is None and param is not None:
            param_at[len(text)] = param
        text += s if s is not None else "\x00"
    out: list[JavaCall] = []
    params: list[tuple[int, str]] = []
    offset = 0
    for cmd in text.split("\n"):
        if _HEAD.match(cmd) or _DYN_HEAD.match(cmd):
            for m in _FN.finditer(cmd):
                rest = cmd[m.end():]
                ns, path = m.group(3), m.group(4)
                dynamic = rest.startswith("\x00")
                # `execute if function ns:check run ...`: the function runs as the condition
                cond = bool(re.search(r"(?:^|\s)(?:if|unless)\s+$", cmd[:m.start()])) and not m.group(1)
                if not dynamic and (not path or not (_SCHED_TAIL.match(rest) if m.group(1) else (
                        _TAIL.match(rest) or (cond and _COND_TAIL.match(rest))))):
                    continue
                pos = offset + (m.start(3) if ns else m.end())
                st, line, block = max(((st, ln, b) for st, ln, b in starts if st <= pos),
                                      default=(0, top.start_point[0] + 1, False))
                if block:  # a text block: the line of this command, not of its opening quotes
                    line += text[st:pos].count("\n")
                if m.group(1):
                    how = "schedule"
                elif re.search(r"(?:^|\s)(?:run|execute)\s", cmd[:m.start()]) or cmd.startswith("\x00"):
                    how = "execute run"
                else:
                    how = "function"
                target = f"{m.group(2)}{ns}:{path}" if ns else ""  # "": the whole name is built at run time
                after = cmd[m.end() + 1:m.end() + 2]
                if dynamic and offset + m.end() in param_at and not after.strip() and not m.group(2):
                    params.append((param_at[offset + m.end()], target))
                    continue
                out.append(JavaCall(target, f.rel, line, "command-string", f.caller(top), how,
                                    None, f.tree, dynamic, f.line_text(line)))
        offset += len(cmd) + 1
    return out, params


# -- the graph (D71) ---------------------------------------------------------------------------------------------

def graph_edges(g, read=None) -> list[tuple[str, str, dict]]:
    """Edges from the Java method that runs a datapack function to the function's node (not applied): ``calls``,
    or ``registers`` with the delay in ticks for ``schedule function`` (the call happens later, as an mcfunction's
    ``schedule`` edge says). Only calls bound to one function: a name built at run time is no edge, and neither is
    a call in a reference tree (not the project's running code). Edges are ``INFERRED`` with
    ``_origin=verinoda.datapack_java``; the method is the innermost callable whose lines hold the call, else the
    class; no edge when neither is in the graph."""
    from verinoda.index import JVM_SUFFIXES

    functions: dict[str, str] = {}
    for n, d in g.G.nodes(data=True):
        f = d.get("source_file") or ""
        if f.endswith(".mcfunction") and d.get("label"):
            functions.setdefault(d["label"], n)
    if not functions:
        return []
    callables: dict[str, list[str]] = {}
    for n, d in g.G.nodes(data=True):
        f = d.get("source_file") or ""
        if f.endswith(".java") and (d.get("_callable") or d.get("_callable_class")):
            callables.setdefault(f, []).append(n)
    spans: dict[str, list[tuple[int, int, str]]] = {}

    def spans_of(f: str) -> list[tuple[int, int, str]]:
        """The lines of ``f``'s methods and classes (asked only for a file with a call: a span parses the file)."""
        if f not in spans:
            spans[f] = [(sp[0], sp[1], n) for n in callables.get(f, []) for sp in [g.span(n)] if sp]
        return spans[f]

    if read is None:
        def read(f: str) -> str | None:
            try:
                return (g.root / f).read_text(encoding="utf-8", errors="replace")
            except OSError:
                return None
    texts = {f: t for f in sorted(callables) if f.endswith(JVM_SUFFIXES) for t in [read(f)] if t is not None}
    if not texts:
        return []
    from verinoda import datapack
    from verinoda.project_index.extractors.mcfunction import _ticks

    res = scan(g.root, texts, consts=datapack._Consts(texts).unique())
    out: list[tuple[str, str, dict]] = []
    seen: set[tuple[str, str, str]] = set()
    # a method that looks the function up and then runs it (`if (get(id).isEmpty()) return; perform(...)`): the edge
    # is the run, not the existence check
    for c in sorted(res.calls, key=lambda c: (c.via == "identifier", c.file, c.line)):
        v = functions.get(c.target)
        if c.dynamic or c.tree or v is None:
            continue
        around = [(b - a, n) for a, b, n in spans_of(c.file) if a <= c.line <= b]
        if not around:
            continue
        u = min(around, key=lambda x: (x[0], x[1]))[1]
        rel = "registers" if c.how == "schedule" else "calls"
        if (u, v, rel) in seen:
            continue
        seen.add((u, v, rel))
        how = {"helper": f"helper {c.helper}", "identifier": "function lookup",
               "command-string": f"command string ({c.how})"}.get(c.via, c.via)
        d = {"relation": rel, "confidence": "INFERRED", "_origin": "verinoda.datapack_java", "weight": 1.0,
             "source_file": c.file, "source_location": f"L{c.line}", "context": f"{how} {c.target}"}
        if rel == "registers":
            m = re.search(r"schedule\s+function\s+\S+\s+(\d+(?:\.\d+)?[tsd]?)", c.text or "")
            d.update(registrar="schedule function", delay=_ticks(m.group(1)) if m else None)
        out.append((u, v, d))
    return out
