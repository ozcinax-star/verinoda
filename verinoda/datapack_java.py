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
  and is keyed by class, name and argument count (a project may have several same-named helpers); a method
  that passes its own parameter to a helper is a helper too. ``datapack.function_helpers`` in
  ``.verinoda/config.json`` (``["Class.method:argIndex:namespace"]``) names one whose body is beyond reading.

A name built at run time (``"prefix_" + x``, a loop variable, a command argument) cannot be bound: it is
reported as dynamic, with the part that is known (``ns:prefix_*``). The code is read (tree-sitter), not run,
so a call is a lead, as for the rest of the datapack view.
"""
from __future__ import annotations

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
# ("const", s) | ("param", index, prefix, as_is) | ("prefix", s) | None (nothing known)

def _concat(a, b):
    if a is None:
        return None
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


class _Scope:
    """A method (or initialiser): its String/identifier parameters and the locals declared once in it."""

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
                            self.locals.setdefault(name, []).append(d.child_by_field_name("value"))
                            self.local_types[name] = f.text(ty) if ty is not None else ""
            elif t in ("enhanced_for_statement", "catch_formal_parameter"):
                nm = n.child_by_field_name("name")  # loop and catch variables: nothing known of them
                if nm is not None:
                    self.locals.setdefault(f.text(nm), []).append(None)
            elif t == "lambda_expression":
                ps2 = n.child_by_field_name("parameters")
                if ps2 is not None:
                    ids = [ps2] if ps2.type == "identifier" else [
                        c.child_by_field_name("name") or c for c in ps2.named_children]
                    for c in ids:
                        if c is not None and c.type == "identifier":
                            self.locals.setdefault(f.text(c), []).append(None)
            elif t == "assignment_expression":
                left = n.child_by_field_name("left")
                if left is not None and left.type == "identifier":
                    self.assigned.add(f.text(left))
            elif t in ("line_comment", "block_comment", "string_literal"):
                continue
            stack.extend(n.children)

    def _local(self, name: str):
        """The one initialiser of a local never assigned again; False when the name is not a local."""
        if name not in self.locals:
            return False
        inits = self.locals[name]
        return inits[0] if len(inits) == 1 and name not in self.assigned else None

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
            init = self._local(name)
            if init is not False:
                return self.value(init, depth + 1) if init is not None else None
            if name in self.params and name not in self.assigned:
                return ("param", self.params[name], "", False)
        if t == "method_invocation":  # name.toLowerCase(Locale.ROOT), name.trim(): still the name
            nm, obj = n.child_by_field_name("name"), n.child_by_field_name("object")
            how = self.f.text(nm) if nm is not None else ""
            if obj is None or how not in _SAME_NAME:
                return None
            v = self.value(obj, depth + 1)
            if v is not None and v[0] == "const":
                return ("const", v[1].lower() if how == "toLowerCase" else v[1].strip())
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
            init = self._local(name)
            if init is not False:
                return self.id_value(init, depth + 1) if init is not None else None
            if name in self.params and name not in self.assigned \
                    and self.param_types.get(name, "").endswith(_ID_TYPES):
                return ("param", self.params[name], "", True)
            return None
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
            init = self._local(name)
            return bool(init) and self.is_manager(init)
        return False


def _pair(ns, path):
    if not ns or ns[0] != "const" or not re.fullmatch(r"[a-z0-9_.-]+", ns[1]):
        return None
    head = ns[1] + ":"
    if path is None:
        return ("prefix", head)
    if path[0] == "const":
        return ("const", head + path[1])
    if path[0] == "param":
        return ("param", path[1], head + path[2], False) if not path[3] else ("prefix", head)
    return ("prefix", head + path[1])


def _whole(v):
    if v is None:
        return None
    if v[0] == "const":
        return ("const", v[1] if ":" in v[1] else "minecraft:" + v[1])
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
    seen_helper: set[tuple] = {(h.cls, h.name, h.arity, h.tree) for h in helpers}

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
                h = _helper_of(f, sc, inv, v, "body", takes_id=_is_id_param(sc, v[1]))
                if h is not None and (h.cls, h.name, h.arity, h.tree) not in seen_helper:
                    seen_helper.add((h.cls, h.name, h.arity, h.tree))
                    helpers.append(h)
                continue
            res.calls.append(JavaCall(v[1], rel, line, "identifier", f.caller(inv), "lookup", None, f.tree,
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
                h = _helper_of(f, sc, top, ("param", index, prefix, not prefix), "body")
                if h is not None and (h.cls, h.name, h.arity, h.tree) not in seen_helper:
                    seen_helper.add((h.cls, h.name, h.arity, h.tree))
                    helpers.append(h)

    # 3. helper calls; a method handing its own parameter to a helper is a helper too (a few rounds)
    found: list[JavaCall] = []
    for _round in range(5):
        names = {h.name for h in helpers}
        if not names:
            break
        found = []
        new = False
        for rel, t in texts.items():
            if not any(n in t for n in names):
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
                line = inv.start_point[0] + 1
                if v is not None and v[0] == "param":
                    cls, meth = f.where(inv)
                    if (h.cls, h.name) == (cls, meth):
                        continue  # a helper handing the name on to itself
                    fw = _helper_of(f, sc, inv, ("param", v[1], h.prefix + v[2], h.as_is and not v[2]),
                                    f"forwards to {h.label}", takes_id=h.takes_id)
                    if fw is not None and (fw.cls, fw.name, fw.arity, fw.tree) not in seen_helper:
                        seen_helper.add((fw.cls, fw.name, fw.arity, fw.tree))
                        helpers.append(fw)
                        new = True
                    if fw is not None:
                        continue
                if v is not None and v[0] == "const":
                    target, dyn = (v[1] if h.takes_id else h.bind(v[1])), False
                elif v is not None and v[0] == "prefix":
                    target, dyn = (v[1] if h.takes_id or (h.as_is and ":" in v[1]) else h.prefix + v[1]), True
                else:
                    target, dyn = h.prefix, True
                found.append(JavaCall(target, rel, line, "helper", f.caller(inv), "lookup", h.label, f.tree, dyn,
                                      f.line_text(line)))
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
                  f.tree, takes_id)


def _pick(f: _File, sc: _Scope, inv, name: str, arity: int, helpers: list[Helper]) -> Helper | None:
    """The helper a call names: by class (its qualifier, the declared type of the receiver, or the caller's own
    class), name and argument count; one in the caller's tree first."""
    cands = [h for h in helpers if h.name == name and (h.arity is None or h.arity == arity)]
    if not cands:
        return None
    cands = [h for h in cands if h.tree == f.tree] or cands
    obj = inv.child_by_field_name("object")
    if obj is None or obj.type in ("this", "super"):
        cls, _m = f.where(inv)
        own = [h for h in cands if h.cls == cls and (h.file == f.rel or h.source == "config")] or \
              [h for h in cands if h.file == f.rel]
        if own:
            return own[0]
        text = f.src.decode("utf-8", "replace")
        stat = [h for h in cands if re.search(rf"import\s+static\s+[\w.]*\b{re.escape(h.cls)}\."
                                              rf"(?:\*|{re.escape(name)})\s*;", text)]
        return stat[0] if len(stat) == 1 else None
    qual = f.text(obj).rsplit(".", 1)[-1]
    if not qual[:1].isupper():  # an instance: the class its variable is declared with
        declared = sc.local_types.get(qual) or sc.param_types.get(qual) or ""
        qual = declared.rsplit(".", 1)[-1].split("<", 1)[0]
    named = [h for h in cands if h.cls == qual]
    if len({(h.file, h.arg, h.prefix) for h in named}) == 1:
        return named[0]
    return None


def _command_calls(f: _File, top) -> tuple[list[JavaCall], list[tuple[int, str]]]:
    """The ``function`` calls of one string expression that reads as a command, and ``(parameter index, prefix)``
    where the enclosing method's own parameter completes the name (``"function ns:" + name``: a helper)."""
    sc = f.scope(top)
    pieces: list[tuple[str | None, int, int | None]] = []   # (text, or None when built at run time; line; param)

    def flat(n):
        if n.type == "parenthesized_expression" and n.named_children:
            flat(n.named_children[0])
        elif n.type == "binary_expression" and (n.child_by_field_name("operator") or n).type == "+":
            flat(n.child_by_field_name("left"))
            flat(n.child_by_field_name("right"))
        elif n.type == "string_literal":
            pieces.append((_literal(f.src, n), n.start_point[0] + 1, None))
        else:
            c = sc.const(n)
            v = sc.value(n) if c is None and n.type == "identifier" else None
            pieces.append((c, n.start_point[0] + 1, v[1] if v and v[0] == "param" and not v[2] else None))

    flat(top)
    text, starts, param_at = "", [], {}
    for s, line, param in pieces:
        starts.append((len(text), line))
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
                if not dynamic and (not path or not (_SCHED_TAIL.match(rest) if m.group(1) else _TAIL.match(rest))):
                    continue
                pos = offset + (m.start(3) if ns else m.end())
                line = max((ln for st, ln in starts if st <= pos), default=top.start_point[0] + 1)
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
