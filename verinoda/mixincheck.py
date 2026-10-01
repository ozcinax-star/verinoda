"""Mixin injection points checked against the target class's bytecode.

A Mixin names places in a game class by strings the compiler never checks: the method an injector goes into
(``@Inject(method = "tick(Z)V")``), the instruction an ``@At`` stops at (``@At(value = "INVOKE", target =
"Lnet/minecraft/world/entity/Mob;setTarget(Lnet/minecraft/world/entity/LivingEntity;)V")``) and the members a
``@Shadow`` declaration stands for. A wrong one compiles, and the game stops at start (or the injection is
skipped). Here each is looked up in the class file of the ``@Mixin`` target, read from the jars of the build's
classpath (:func:`verinoda.jvmclass.discover`, the same as ``access-check``), and an ``@At`` target in what the
bytecode of the selected methods actually references (:func:`verinoda.jvmclass.class_code`: the ``Code``
attribute's calls, field accesses and ``new``).

Verdicts per row: ``exists`` (the class file holds it; ``statically_verified``, the evidence ``jar!class``),
``absent`` (the class file read holds no such method, descriptor, referenced member or shadowed member, with the
same evidence; ``strong_inference`` as in ``access-check``: the classpath is what the last build resolved and may
be older than the build file, so a name missing from it is not proven wrong for the version the build names),
``unknown`` (no class file to compare with - the target class is on no jar read, a JDK or project class, an
incomplete classpath - with the next step; a member that is, or may be, inherited; a selector, target or name
this reader does not compare). The nearest real names of an ``absent`` row are found by edit distance: a
suggestion, ``strong_inference``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from verinoda import jvmclass
from verinoda.accesscheck import _JDK_PREFIXES, _SKIP, ClassFiles, _configured, _project_class
from verinoda.codecheck_java import MIXIN_INJECTORS, _file_ctx, _param_text, _parser, _t, _type_text, _typevars
from verinoda.jvm_mixins import string_constants, strip_comments

INVOKE_POINTS = ("INVOKE", "INVOKE_ASSIGN", "INVOKE_STRING")
_MIXIN = re.compile(r"@(?:[\w.]*\.)?Mixin\b")
# names of a namespace the named classpath does not carry (Fabric intermediary, Forge SRG)
_FOREIGN = re.compile(r"^(?:method|field|comp)_\d+$|^[fm]_\d+_$|^(?:func|field)_\d+_\w*$")
_DECLS = ("class_declaration", "interface_declaration")
_COMMENTS = ("line_comment", "block_comment", "comment")
NEXT_CLASSPATH = ("run the Gradle (Loom) build once, or list the jars in code_check.classpath "
                  "(.verinoda/config.json), then run mixin-check again")
SUGGESTION = "strong_inference"
ABSENT_STATUS = "strong_inference"
SHADOW_PREFIX = "shadow$"           # @Shadow's prefix() when none is written
# java.lang.Object's methods: a name among them is inherited when Object itself is on no jar read
_OBJECT_METHODS = {"<init>", "clone", "equals", "finalize", "getClass", "hashCode", "notify", "notifyAll",
                   "toString", "wait"}
_NEW_DESC = re.compile(r"^\((?:\[*(?:[BCDFIJSZ]|L[^;()]+;))*\)L[^;()\[]+;$")


# -- edit distance ---------------------------------------------------------------------------------------

def _distance(a: str, b: str) -> int:
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def nearest(written: str, candidates, limit: int = 3) -> list[str]:
    """The real names closest to ``written`` by edit distance (at most half its length, or 3, away)."""
    cap = max(3, len(written) // 2)
    scored = sorted((d, c) for c in set(candidates) if (d := _distance(written, c)) <= cap)
    return [c for _d, c in scored[:limit]]


# -- reading the Mixin sources ---------------------------------------------------------------------------

@dataclass
class Item:
    kind: str                    # method | at | shadow
    line: int
    annotation: str
    written: str
    point: str | None = None     # @At value
    selectors: list[str] = field(default_factory=list)   # an @At's injector's method selectors
    member: str | None = None    # shadow: field | method
    name: str | None = None
    types: list[str] = field(default_factory=list)       # shadow: field type, or parameter types then return
    typevars: set[str] = field(default_factory=set)
    unread: bool = False         # at: a method selector is not a constant string this reader resolves


@dataclass
class MixinClass:
    path: str
    line: int
    name: str
    targets: list[tuple[str, list[str]]] = field(default_factory=list)   # (as written, binary candidates)
    items: list[Item] = field(default_factory=list)
    priority: int | None = None  # @Mixin(priority = N) as written
    target_names: list[list[str]] = field(default_factory=list)   # per target: the binary name(s) it may be
    injections: list[dict] = field(default_factory=list)   # one per injector or @Overwrite: what it names


def _line(node) -> int:
    return node.start_point[0] + 1


def _type(node, src: bytes) -> str:
    """A type as written with every array dimension (``int[][]`` -> ``int[][]``; ``_type_text`` keeps one)."""
    if node is not None and node.type == "array_type":
        dims = node.child_by_field_name("dimensions")
        return _type(node.child_by_field_name("element"), src) + "[]" * max(1, _t(dims, src).count("[")
                                                                             if dims is not None else 1)
    return _type_text(node, src)


def _str(n, src: bytes, consts: dict[str, str]) -> str | None:
    """A constant string: a literal, literals joined with ``+``, or a ``static final String`` of the file."""
    if n.type == "string_literal":
        return "".join(_t(c, src) for c in n.named_children if c.type in ("string_fragment", "escape_sequence"))
    if n.type == "binary_expression" and any(c.type == "+" for c in n.children):
        parts = [_str(c, src, consts) for c in n.named_children]
        return None if any(p is None for p in parts) else "".join(parts)
    if n.type == "parenthesized_expression" and n.named_children:
        return _str(n.named_children[0], src, consts)
    if n.type in ("identifier", "field_access"):
        return consts.get(_t(n, src).rsplit(".", 1)[-1])
    return None


def _element(a, src: bytes, key: str):
    """The value node of ``key = ...`` in an annotation (``value`` also for a lone argument), or None."""
    args = a.child_by_field_name("arguments")
    if args is None:
        return None
    for ch in args.named_children:
        if ch.type == "element_value_pair":
            k = ch.child_by_field_name("key")
            if k is not None and _t(k, src) == key:
                return ch.child_by_field_name("value")
        elif key == "value" and ch.type not in _COMMENTS:
            return ch
    return None


def _flat(v) -> list:
    if v is None:
        return []
    if v.type in ("element_value_array_initializer", "array_initializer"):
        return [x for c in v.named_children for x in _flat(c)]
    return [v]


def _strings(a, src: bytes, key: str, consts: dict[str, str]) -> list[tuple[object, str]]:
    out = []
    for v in _flat(_element(a, src, key)):
        s = _str(v, src, consts)
        if s is not None:
            out.append((v, s))
    return out


def _annotations(mods, src: bytes) -> list[tuple[str, object]]:
    if mods is None:
        return []
    return [(_t(a.child_by_field_name("name"), src).rsplit(".", 1)[-1], a) for a in mods.named_children
            if a.type in ("annotation", "marker_annotation")]


def _candidates(written: str, ctx) -> list[str]:
    """Binary names a type written in the file may be (imports, its package, on-demand imports, java.lang;
    the outer parts of a dotted name as enclosing classes)."""
    head, _, rest = written.partition(".")
    dotted: list[str] = []
    if head in ctx.single:
        dotted.append(ctx.single[head] + ("." + rest if rest else ""))
    if "." in written and written[:1].islower():
        dotted.append(written)
    pkg = ctx.package.replace("/", ".")
    dotted.append(f"{pkg}.{written}" if pkg else written)
    dotted += [f"{p}.{written}" for p in ctx.ondemand] + [f"java.lang.{written}"]
    out = []
    for d in dotted:
        parts = d.split(".")
        for k in range(len(parts) - 1, 0, -1):   # a.b.Outer.Inner: the longest package first
            out.append("/".join(parts[:k]) + "/" + "$".join(parts[k:]))
    return list(dict.fromkeys(out))


def read_mixins(path: str, src: bytes) -> list[MixinClass]:
    """The ``@Mixin`` classes of a Java file with what they name in their targets."""
    tree = _parser().parse(src)
    ctx = _file_ctx(path, tree.root_node, src)
    consts = string_constants(strip_comments(src.decode("utf-8", "replace")))
    out: list[MixinClass] = []
    stack = [(tree.root_node, "")]
    while stack:
        node, outer = stack.pop()
        for ch in node.named_children:
            if ch.type not in _DECLS:
                continue
            nm = ch.child_by_field_name("name")
            simple = _t(nm, src) if nm is not None else "?"
            cname = f"{outer}.{simple}" if outer else (f"{ctx.package.replace('/', '.')}.{simple}" if ctx.package
                                                         else simple)
            body = ch.child_by_field_name("body")
            if body is not None:
                stack.append((body, cname))
            mods = next((c for c in ch.named_children if c.type == "modifiers"), None)
            mixin = next((a for n, a in _annotations(mods, src) if n == "Mixin"), None)
            if mixin is None:
                continue
            mc = MixinClass(path, _line(ch), cname)
            for v in _flat(_element(mixin, src, "value")):
                if v.type == "class_literal" and v.named_children:
                    w = _type_text(v.named_children[0], src)
                    cands = _candidates(w, ctx)
                    mc.targets.append((w, cands))
                    # an imported or fully qualified name is the first candidate; any other may be any of them
                    sure = w.partition(".")[0] in ctx.single or ("." in w and w[:1].islower())
                    mc.target_names.append(cands[:1] if sure else cands)
            for _v, s in _strings(mixin, src, "targets", consts):
                b = s.strip().replace(".", "/") if "/" not in s else s.strip()
                mc.targets.append((s, [b]))
                mc.target_names.append([b])
            mc.priority = _int(_element(mixin, src, "priority"), src)
            ctv = _typevars(ch, src)
            for m in (body.named_children if body is not None else []):
                if m.type in ("method_declaration", "field_declaration"):
                    _member_items(mc, m, src, consts, ctv)
            out.append(mc)
    return out


def _int(n, src: bytes) -> int | None:
    """An int literal (``5``, ``-1``, ``0x10``), else None."""
    if n is None:
        return None
    try:
        return int(re.sub(r"[\s_lL]", "", _t(n, src)), 0)
    except ValueError:
        return None


def _value(n, src: bytes, consts: dict[str, str]):
    """An annotation value as compared with a class file's: a string, a number, true/false, an enum constant's
    name, a nested annotation as ``{"type": simple name, "values": {...}}``, a list; the text when unread."""
    if n is None:
        return None
    if n.type in ("element_value_array_initializer", "array_initializer"):
        return [_value(c, src, consts) for c in n.named_children if c.type not in _COMMENTS]
    if n.type in ("annotation", "marker_annotation"):
        args = n.child_by_field_name("arguments")
        vals: dict = {}
        for ch in (args.named_children if args is not None else []):
            if ch.type == "element_value_pair":
                vals[_t(ch.child_by_field_name("key"), src)] = _value(ch.child_by_field_name("value"), src, consts)
            elif ch.type not in _COMMENTS:
                vals["value"] = _value(ch, src, consts)
        return {"type": _t(n.child_by_field_name("name"), src).rsplit(".", 1)[-1], "values": vals}
    s = _str(n, src, consts)
    if s is not None:
        return s
    text = _t(n, src).strip()
    if text in ("true", "false"):
        return text == "true"
    num = _int(n, src)
    if num is not None:
        return num
    try:
        return float(re.sub(r"[fFdD]$", "", text))
    except ValueError:
        return text.rsplit(".", 1)[-1]   # an enum constant (Opcodes.GETFIELD) or a name this reader does not resolve


def _injection(aname: str, a, m, src: bytes, consts: dict[str, str]) -> dict:
    """What an injector (or ``@Overwrite``) names: its target method selectors, its ``@At`` points and the
    values that pick a slot (``ordinal``, ``index``, ``name``, ``constant``, ``cancellable``)."""
    nm = m.child_by_field_name("name")
    member = _t(nm, src) if nm is not None else "?"
    args = a.child_by_field_name("arguments") if a.type == "annotation" else None
    vals: dict = {}
    for ch in (args.named_children if args is not None else []):
        if ch.type == "element_value_pair":
            vals[_t(ch.child_by_field_name("key"), src)] = _value(ch.child_by_field_name("value"), src, consts)
    sels = vals.get("method")
    sels = sels if isinstance(sels, list) else [sels] if sels is not None else []
    if aname == "Overwrite":
        sels = [member]
    return {"kind": aname, "line": _line(a), "member": member, "values": vals,
            "selectors": [s for s in sels if isinstance(s, str)],
            "unread": any(not isinstance(s, str) for s in sels)}


def _member_items(mc: MixinClass, m, src: bytes, consts: dict[str, str], ctv: set[str]) -> None:
    mods = next((c for c in m.named_children if c.type == "modifiers"), None)
    for aname, a in _annotations(mods, src):
        if m.type == "method_declaration" and (aname in MIXIN_INJECTORS or aname == "Overwrite"):
            mc.injections.append(_injection(aname, a, m, src, consts))
        if aname in MIXIN_INJECTORS:
            sels = _strings(a, src, "method", consts)
            for node, s in sels:
                mc.items.append(Item("method", _line(node), f"@{aname}", s))
            for at in _flat(_element(a, src, "at")):
                if at.type not in ("annotation", "marker_annotation"):
                    continue
                pv = _strings(at, src, "value", consts)
                tv = _strings(at, src, "target", consts)
                if not pv or not tv:
                    continue        # HEAD, RETURN, TAIL...: no member is named
                point = pv[0][1].strip().upper()
                if point not in INVOKE_POINTS + ("FIELD", "NEW"):
                    continue
                mc.items.append(Item("at", _line(tv[0][0]), f"@{aname} @At({point})", tv[0][1], point=point,
                                     selectors=[s for _n, s in sels],
                                     unread=len(_flat(_element(a, src, "method"))) > len(sels)))
        elif aname == "Shadow":
            prefix = next((s for _n, s in _strings(a, src, "prefix", consts)), SHADOW_PREFIX)
            aliases = [s for _n, s in _strings(a, src, "aliases", consts)]
            ty = _type(m.child_by_field_name("type"), src)
            if m.type == "field_declaration":
                for d in m.named_children:
                    if d.type == "variable_declarator":
                        nm = d.child_by_field_name("name")
                        name = _t(nm, src)
                        dn = next((c for c in d.named_children if c.type == "dimensions"), None)
                        dims = "[]" * _t(dn, src).count("[") if dn is not None else ""
                        mc.items.append(Item("shadow", _line(nm), "@Shadow", f"{name}: {ty}{dims}", member="field",
                                             name=name, types=[ty + dims], typevars=set(ctv),
                                             selectors=aliases))
            else:
                nm = m.child_by_field_name("name")
                if nm is None:
                    continue
                name = _t(nm, src)
                if prefix and name.startswith(prefix):
                    name = name[len(prefix):]
                params = []
                ps = m.child_by_field_name("parameters")
                for p in (ps.named_children if ps is not None else []):
                    if p.type == "formal_parameter":
                        dims = next((c for c in p.named_children if c.type == "dimensions"), None)
                        params.append(_type(p.child_by_field_name("type"), src)
                                      + ("[]" * _t(dims, src).count("[") if dims is not None else ""))
                    elif p.type == "spread_parameter":
                        t = next((c for c in p.named_children if c.type not in ("modifiers", "variable_declarator")),
                                 None)
                        params.append(_type(t, src) + "[]")
                mc.items.append(Item("shadow", _line(nm), "@Shadow", f"{name}({', '.join(params)}) -> {ty}",
                                     member="method", name=name, types=params + [ty],
                                     typevars=ctv | _typevars(m, src), selectors=aliases))


# -- comparing with the class file -----------------------------------------------------------------------

def parse_selector(s: str) -> tuple[str | None, str | None] | None:
    """(name, descriptor) of a method selector (``name``, ``name*``, ``name(desc)ret``, ``Lowner;name(desc)``,
    ``owner.name(desc)``); a name of None matches every method; None for a selector not compared here (a
    regular expression)."""
    s = re.sub(r"\s+", "", s)
    if s.startswith("/"):
        return None
    s = re.sub(r"\{[\d,]*\}$", "", s)               # a quantifier
    owner, name, desc = parse_member(s)
    name = (name or "").rstrip("*")
    return (name or None), desc


def parse_member(s: str) -> tuple[str | None, str | None, str | None]:
    """(owner, name, descriptor) of a member reference as Mixin reads it: ``Lowner;name(desc)ret``,
    ``Lowner;name:desc``, ``owner.name(desc)`` or a part of one."""
    s = re.sub(r"\s+", "", s)
    owner = None
    m = re.match(r"^L([^;(]+);(.*)$", s)
    if m:
        owner, rest = m.group(1), m.group(2)
    else:
        head = re.split(r"[(:]", s, maxsplit=1)[0]
        rest = s
        if "." in head:
            o, _, nm = head.rpartition(".")
            owner, rest = o.replace(".", "/"), nm + s[len(head):]
    if "(" in rest:
        i = rest.index("(")
        name, desc = rest[:i], rest[i:]
    elif ":" in rest:
        name, _, desc = rest.partition(":")
    else:
        name, desc = rest, None
    return owner, (name or None), (desc or None)


def _simple(written: str) -> str:
    """A type as compared: its last name and its array dimensions (``a.B.C[]`` -> ``C[]``)."""
    dims = written.count("[]")
    return written.replace("[]", "").rsplit(".", 1)[-1] + "[]" * dims


def _same_type(written: str, real: str, typevars: set[str]) -> bool:
    """Whether a type written in the source is the descriptor type ``real`` (``[Lnet/a/B$C``, ``I``), by simple
    name; a type variable is erased to its bound and not compared."""
    if written.replace("[]", "").rsplit(".", 1)[-1] in typevars:
        return True
    return _simple(written) == _simple(_param_text(real))


def _sig(name: str, desc: str) -> str:
    close = desc.index(")")
    params = jvmclass._descriptor_types(desc[1:close])
    ret = jvmclass._descriptor_types(desc[close + 1:]) or ["V"]
    return f"{name}({', '.join(_param_text(p) for p in params)}) -> {_param_text(ret[0])}"


def _render(r: list[str], with_owner: bool) -> str:
    kind, owner, name, desc = r
    if kind == "N":
        return f"L{owner};"
    o = f"L{owner};" if with_owner else ""
    return f"{o}{name}{desc}" if kind == "M" else f"{o}{name}:{desc}"


class _Target:
    """One ``@Mixin`` target class: its class file (or why there is none) and the rows checked against it."""

    def __init__(self, written: str, cands: list[str], cf: ClassFiles, complete: bool, source: str,
                 java_paths: set[str]):
        self.cf = cf
        self.binary = next((c for c in cands if c in cf.where), cands[0] if cands else written)
        self.shown = self.binary.replace("/", ".").replace("$", ".")
        self.data = None
        self.why = self.next = None
        if self.binary in cf.where:
            self.evidence = cf.evidence(self.binary)
            self.data = cf.code(self.binary)
            if self.data is None:
                self.why, self.next = f"{self.evidence} could not be read", "check that the jar is not damaged"
            return
        self.evidence = None
        if self.binary.startswith(_JDK_PREFIXES):
            self.why = f"{self.shown} is a JDK class: the JDK's class files are not read here"
            self.next = f"verinoda api {self.shown} lists its members from the JDK's API"
        elif any(_project_class(c, java_paths) for c in cands):
            self.why = f"{self.shown} is a class of the project's own sources: its compiled class is not read here"
            self.next = "verinoda check on the Mixin file compares the member names with the sources"
        elif not complete:
            self.why = f"{self.shown} is on no jar read; the classpath is not complete ({source})"
            self.next = NEXT_CLASSPATH
        else:
            self.why = f"{self.shown} is on no jar of the classpath ({source})"
            self.next = ("verinoda check on the Mixin file reports a @Mixin target that does not resolve; if the game "
                         "version changed, rebuild so the classpath is the one the build names")

    def inherited(self, what: str, name: str) -> str | None:
        """Why ``name`` is, or may be, a super class's member rather than absent: the super class (read on the
        classpath) that declares it, or the first super class this reader could not read; None when every super
        class was read and none declares it."""
        sup, hops = (self.data or {}).get("super"), 0
        while sup and hops < 30:
            shown = sup.replace("/", ".").replace("$", ".")
            d = self.cf.code(sup) if sup in self.cf.where else None
            if d is None:
                if sup == "java/lang/Object":
                    return (f"{name} is declared in java.lang.Object, a super class, not in {self.shown}"
                            if what == "methods" and name in _OBJECT_METHODS else None)
                return (f"{name} may be declared in {shown}, a super class whose class file is not read here "
                        "(on no jar read, or unreadable)")
            if any(r[0] == name for r in d[what]):
                return f"{name} is declared in {shown}, a super class, not in {self.shown}"
            sup, hops = d.get("super"), hops + 1
        return None


def _row(mc: MixinClass, it: Item, t: _Target, verdict: str, why: str, **extra) -> dict:
    row = {"at": f"{mc.path}:{it.line}", "mixin": mc.name, "target_class": t.shown, "kind": it.kind,
           "annotation": it.annotation, "written": it.written, "verdict": verdict,
           "status": {"unknown": "unknown", "absent": ABSENT_STATUS}.get(verdict, "statically_verified"),
           "why": why}
    if t.evidence:
        row["evidence"] = t.evidence
    if extra.get("nearest"):
        row["nearest"] = extra.pop("nearest")
        row["nearest_status"] = SUGGESTION
    extra.pop("nearest", None)
    row.update({k: v for k, v in extra.items() if v})
    return row


def _select(t: _Target, sel: str) -> tuple[list | None, str, str, list[str]]:
    """The methods of the class file a selector matches, with a verdict, why and the nearest real ones."""
    parsed = parse_selector(sel)
    if parsed is None:
        return None, "unknown", "a regular-expression selector is not compared here", []
    name, desc = parsed
    owner = parse_member(re.sub(r"\{[\d,]*\}$", "", re.sub(r"\s+", "", sel)))[0]
    if owner and owner != t.binary:
        return [], "absent", (f"the selector names the owner {owner.replace('/', '.')}, and Mixin matches it only "
                              f"against the target {t.shown}"), []
    methods = t.data["methods"]
    if name is None:
        got = [m for m in methods if desc is None or m[1] == desc]
        return got, ("exists" if got else "absent"), (f"{len(got)} method(s) of {t.evidence} match" if got else
                                                       f"{t.evidence} has no method with the descriptor {desc}"), []
    same = [m for m in methods if m[0] == name]
    got = [m for m in same if desc is None or m[1] == desc]
    if got:
        shown = ", ".join(f"{m[0]}{m[1]}" for m in got[:3])
        return got, "exists", f"{shown} is in {t.evidence}", []
    if same:
        return [], "absent", (f"{t.evidence} has no method {name}{desc}; its {name} has the descriptor(s) "
                              + ", ".join(m[1] for m in same[:5])), nearest(f"{name}{desc}",
                                                                           [f"{m[0]}{m[1]}" for m in same])
    sup = t.inherited("methods", name)
    if sup:
        return [], "unknown", f"{sup}: a Mixin injects only into the target's own methods", []
    if _FOREIGN.match(name):
        return [], "unknown", (f"{name} is an intermediary or SRG name; the classpath read here carries other "
                               "names"), []
    cands = [m[0] + (m[1] if desc else "") for m in methods if m[0] != "<clinit>" or name == "<clinit>"]
    return [], "absent", f"{t.evidence} declares no method named {name}", nearest(name + (desc or ""), cands)


def _near_refs(written: str, name: str | None, pairs: list[tuple[str, str]], keep: int = 10) -> list[str]:
    """The nearest of rendered references ``(name, rendered)``: their names are ranked first and only the
    references of the ``keep`` closest names are compared in full, so a method with thousands of calls stays
    cheap."""
    if name and len({n for n, _r in pairs}) > keep:
        close = set(sorted({n for n, _r in pairs}, key=lambda n: (_distance(name, n), n))[:keep])
        pairs = [(n, r) for n, r in pairs if n in close]
    return nearest(written, [r for _n, r in pairs])


def _check_at(it: Item, t: _Target, matched: list, uncompared: str | None = None) -> tuple[str, str, list[str]]:
    """An @At target against the bytecode of the methods its injector's selectors matched; ``uncompared`` says
    why some (or all) of the selectors were not compared, which keeps a miss ``unknown``."""
    if not matched:
        return "unknown", (uncompared or "its injector's method selector matches no method of the class file") + \
            ", so the point is not looked for", []
    written = it.written.strip()
    owner, name, desc = parse_member(written)
    codes = [m[3] for m in matched]
    refs = [r for c in codes if c for r in c]
    if it.point == "NEW":
        if written.startswith("("):
            if not desc or not _NEW_DESC.match(desc):
                return "unknown", (f"{written} is neither a class name nor a constructor descriptor "
                                   "((params)Lpkg/Cls;) this reader reads, so it is not looked for"), []
            close = desc.index(")")
            cls, cdesc = desc[close + 2:-1], desc[:close + 1] + "V"
            hit = any(r[0] == "N" and r[1] == cls for r in refs) and any(
                r[0] == "M" and r[1] == cls and r[2] == "<init>" and r[3] == cdesc for r in refs)
            pairs = [(r[1].rsplit("/", 1)[-1], f"({r[3][1:r[3].index(')')]})L{r[1]};")
                     for r in refs if r[0] == "M" and r[2] == "<init>"]
            name = cls.rsplit("/", 1)[-1]
        else:
            cls = written[1:-1] if written.startswith("L") and written.endswith(";") else written.replace(".", "/")
            hit = any(r[0] == "N" and r[1] == cls for r in refs)
            pairs = [(r[1].rsplit("/", 1)[-1], _render(r, True) if written.startswith("L") else r[1])
                     for r in refs if r[0] == "N"]
            name = cls.rsplit("/", 1)[-1]
    else:
        kind = "F" if it.point == "FIELD" else "M"
        pool = [r for r in refs if r[0] == kind]
        hit = any((owner is None or r[1] == owner) and (name is None or r[2] == name)
                  and (desc is None or r[3] == desc) for r in pool)
        pairs = [(r[2], _render(r, owner is not None)) for r in pool]
    where = ", ".join(f"{t.shown.rsplit('.', 1)[-1]}.{m[0]}{m[1]}" for m in matched[:3])
    if hit:
        return "exists", f"the bytecode of {where} ({t.evidence}) references {it.written}", []
    if any(c is None or c is False for c in codes):
        return "unknown", (f"{where} has no bytecode this reader walked (abstract, native, or an instruction it "
                           "does not know)"), []
    what = {"FIELD": "field access", "NEW": "object creation"}.get(it.point or "", "call")
    if uncompared:
        return "unknown", (f"the bytecode of {where} ({t.evidence}) has no {what} {it.written}, but {uncompared}, "
                           "so the methods it selects are not all looked in"), []
    return "absent", f"the bytecode of {where} ({t.evidence}) has no {what} {it.written}", _near_refs(
        written, name, pairs)


def _check_shadow(it: Item, t: _Target) -> tuple[str, str, list[str]]:
    what = "fields" if it.member == "field" else "methods"
    rows = t.data[what]
    names = [it.name] + [a for a in it.selectors if a]
    same = [r for r in rows if r[0] in names]
    if it.member == "field":
        good = [r for r in same if _same_type(it.types[0], jvmclass._descriptor_types(r[1])[0], it.typevars)]
        real = [f"{r[0]}: {_param_text(jvmclass._descriptor_types(r[1])[0])}" for r in same]
    else:
        good = []
        for r in same:
            close = r[1].index(")")
            params = jvmclass._descriptor_types(r[1][1:close])
            ret = jvmclass._descriptor_types(r[1][close + 1:]) or ["V"]
            want = it.types
            if len(params) + 1 == len(want) and all(_same_type(w, p, it.typevars)
                                                    for w, p in zip(want, params + ret)):
                good.append(r)
        real = [_sig(r[0], r[1]) for r in same]
    if good:
        r = good[0]
        shown = f"{r[0]}: {_param_text(jvmclass._descriptor_types(r[1])[0])}" if it.member == "field" else \
            _sig(r[0], r[1])
        return "exists", f"{shown} is in {t.evidence}", []
    if same:
        return "absent", (f"{t.evidence} has {it.name} as " + "; ".join(real[:5]) + f", not {it.written}"
                          if it.member == "method" else
                          f"{t.evidence} has {real[0]}, not {it.types[0]}"), real[:3]
    sup = t.inherited(what, it.name or "")
    if sup:
        return "unknown", f"{sup}: a @Shadow reaches only {t.shown}'s own members", []
    if _FOREIGN.match(it.name or ""):
        return "unknown", f"{it.name} is an intermediary or SRG name; the classpath read here carries other names", []
    return "absent", f"{t.evidence} declares no {it.member} named {it.name}", nearest(
        it.name or "", [r[0] for r in rows if not r[0].startswith("<")])


def check_mixin(mc: MixinClass, cf: ClassFiles, complete: bool, source: str, java_paths: set[str]) -> list[dict]:
    rows: list[dict] = []
    for written, cands in mc.targets:
        t = _Target(written, cands, cf, complete, source, java_paths)
        matched: dict[str, list | None] = {}
        for it in mc.items:
            if t.data is None:
                rows.append(_row(mc, it, t, "unknown", t.why, next=t.next))
                continue
            if it.kind == "method":
                got, verdict, why, near = _select(t, it.written)
                matched[it.written] = got
            elif it.kind == "at":
                sel: list = []
                for s in it.selectors:
                    if s not in matched:
                        matched[s] = _select(t, s)[0]
                    sel += matched[s] or []
                regex = [s for s in it.selectors if matched[s] is None]
                uncompared = ("a method selector of its injector is not a constant string this reader resolves"
                              if not it.selectors or it.unread else
                              f"its injector's selector {regex[0]} is a regular expression, not compared here"
                              if regex else None)
                verdict, why, near = _check_at(it, t, list({id(m): m for m in sel}.values()), uncompared)
            else:
                verdict, why, near = _check_shadow(it, t)
            rows.append(_row(mc, it, t, verdict, why, nearest=near))
    return rows


# -- the command -----------------------------------------------------------------------------------------

def mixin_files(repo: Path) -> list[str]:
    from verinoda.snapshot import listed_files

    out = []
    for rel in listed_files(repo):
        rel = Path(rel).as_posix()
        if not rel.endswith(".java") or any(x in _SKIP for x in rel.split("/")[:-1]):
            continue
        try:
            text = (repo / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if _MIXIN.search(text):
            out.append(rel)
    return sorted(out)


def check(repo: Path, paths: list[str] | None = None, config: dict | None = None) -> dict:
    """Every Mixin class of the project's Java sources (or of ``paths``) against the class files of its
    build's classpath."""
    import time

    from verinoda.snapshot import listed_files

    t0 = time.perf_counter()
    repo = Path(repo).resolve()
    if paths:
        targets = []
        for p in paths:
            ap = Path(p) if Path(p).is_absolute() else repo / p
            try:
                targets.append(ap.resolve().relative_to(repo).as_posix())
            except ValueError:
                targets.append(ap.as_posix())
    else:
        targets = mixin_files(repo)
    java_paths = {Path(r).as_posix() for r in listed_files(repo) if r.endswith((".java", ".kt"))}
    by_root: dict[Path, list[MixinClass]] = {}
    notes: list[str] = []
    files = []
    for rel in targets:
        try:
            src = (repo / rel).read_bytes()
        except OSError as exc:
            notes.append(f"{rel} is not readable: {exc.__class__.__name__}")
            continue
        found = read_mixins(rel, src)
        files.append({"path": rel, "mixins": len(found)})
        if found:
            by_root.setdefault(jvmclass.build_root(repo, repo / rel), []).extend(found)
    entries, builds = [], []
    for root, group in sorted(by_root.items()):
        try:
            where = root.relative_to(repo).as_posix() or "."
        except ValueError:
            where = root.as_posix()
        cp = jvmclass.discover(root, config if root == repo else None)
        if cp.source == "none" and root != repo and _configured(config):
            cp = jvmclass.discover(repo, config)
            cp.notes.append(f"{where} has no classpath of its own: the configured code_check.classpath is read")
        cf = ClassFiles(cp.jars)
        complete = cp.complete and not cf.unreadable and bool(cf.where)
        notes += cp.notes + [f"unreadable jar: {n}" for n in cf.unreadable]
        builds.append({"build": where, "classpath": cp.source, "complete": complete, "jars": len(cp.jars),
                       "classes": len(cf.where)})
        try:
            for mc in group:
                entries += check_mixin(mc, cf, complete, cp.source, java_paths)
        finally:
            cf.close()
    counts = {v: sum(1 for r in entries if r["verdict"] == v) for v in ("exists", "absent", "unknown")}
    return {"files": files, "builds": builds, "entries": entries, "counts": counts,
            "notes": list(dict.fromkeys(notes)), "seconds": round(time.perf_counter() - t0, 3)}


def lookup(repo: Path, paths: list[str] | None = None) -> dict:
    """``verinoda mixin-check``: the check; ``no_mixins`` when no Java file of the project holds a ``@Mixin``."""
    from verinoda.paths import load_config

    try:
        config = load_config(repo)
    except Exception:  # noqa: BLE001 - no readable config: the build's own classpath is looked for
        config = None
    res = check(repo, paths, config)
    if not any(f["mixins"] for f in res["files"]):
        where = "the file(s) named" if paths else "the project's Java sources"
        return {"status": "no_mixins", **res, "note": f"no @Mixin class in {where}"}
    return {"status": "found", **res}


def render(res: dict) -> str:
    if res["status"] == "no_mixins":
        return res["note"]
    c = res["counts"]
    n = sum(f["mixins"] for f in res["files"])
    out = [f"{n} Mixin class(es), {len(res['entries'])} names: {c['exists']} found in the bytecode, "
           f"{c['absent']} absent, {c['unknown']} unknown"]
    for b in res["builds"]:
        out.append(f"  classpath of {b['build']}: {b['classpath']}, "
                   f"{'complete' if b['complete'] else 'not complete'}, {b['jars']} jar(s)")
    for note in res["notes"]:
        out.append(f"  note: {note}")
    for r in sorted((r for r in res["entries"] if r["verdict"] != "exists"), key=lambda r: r["verdict"] != "absent"):
        out.append(f"  {r['verdict']} [{r['status']}] {r['at']}  {r['annotation']} {r['written']}  "
                   f"(in {r['target_class']})")
        out.append(f"    {r['why']}")
        if r.get("nearest"):
            out.append(f"    nearest (a suggestion, {r['nearest_status']}): {', '.join(r['nearest'])}")
        if r.get("next"):
            out.append(f"    next: {r['next']}")
    return "\n".join(out)
