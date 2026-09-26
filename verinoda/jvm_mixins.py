"""SpongePowered Mixin edges (docs/DESIGN.md D48).

A mixin handler is code that runs inside another class's method: ``@Inject(method = "checkSpawnRules(...)Z",
at = @At("HEAD"), cancellable = true)`` on ``MobMixin`` runs at the start of ``Mob.checkSpawnRules`` and may
cancel it. The graph had only a ``references`` edge to ``Mob``. Here each injector becomes an ``injects`` edge
from the handler method to the target class, carrying the target method, the injection point and whether it can
cancel; ``@Accessor`` / ``@Invoker`` methods become ``accesses`` edges naming the target member.

Read from the annotations as written (comments removed, strings kept): nothing is resolved against the target's
bytecode, so a descriptor or an intermediary name (``method_5979``) is reported as written. A target class with no
node in the graph (a string ``targets`` the file does not import) gives no edge.
"""
from __future__ import annotations

import bisect
import re

MIXIN_RELATION = "injects"
ACCESS_RELATION = "accesses"
MIXIN_ORIGIN = "verinoda.mixins"

INJECTORS = ("Inject", "Redirect", "ModifyVariable", "ModifyArg", "ModifyArgs", "ModifyConstant",
             "ModifyExpressionValue", "ModifyReturnValue", "WrapOperation", "WrapWithCondition", "Overwrite")
ACCESSORS = ("Accessor", "Invoker")
_ANN = re.compile(r"@(?:[\w.]*\.)?(" + "|".join(INJECTORS + ACCESSORS) + r")\b")
_MIXIN = re.compile(r"@(?:[\w.]*\.)?Mixin\s*\(")
_IMPORT = re.compile(r"^\s*import\s+(?!static\b)([\w.]+)\s*;", re.M)
_PACKAGE = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.M)
_STR = re.compile(r'"((?:[^"\\]|\\.)*)"')
_POINTS = {"HEAD": "at its start", "RETURN": "before each return", "TAIL": "before its last return",
           "INVOKE": "around a call", "INVOKE_ASSIGN": "after a call's result is stored", "FIELD": "at a field access",
           "NEW": "at an object creation", "CONSTANT": "at a constant", "LOAD": "at a local variable read",
           "STORE": "at a local variable write", "JUMP": "at a jump", "INVOKE_STRING": "around a call"}


def strip_comments(text: str) -> str:
    """``text`` with Java comments replaced by spaces (newlines kept, so lines and columns stay), strings kept."""
    out = list(text)
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == '"' and text.startswith('"""', i):  # a text block
            j = text.find('"""', i + 3)
            i = n if j < 0 else j + 3
        elif c in "\"'":
            j = i + 1
            while j < n and text[j] != c and text[j] != "\n":
                j += 2 if text[j] == "\\" else 1
            i = j + 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
            out[i:j] = " " * (j - i)
            i = j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out[i:j] = [ch if ch == "\n" else " " for ch in text[i:j]]
            i = j
        else:
            i += 1
    return "".join(out)


def _balanced(text: str, open_i: int) -> tuple[str, int]:
    """The text inside the parentheses opening at ``open_i`` (strings respected) and the index after them."""
    depth, i, n = 0, open_i, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            m = _STR.match(text, i)
            i = m.end() if m else i + 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return text[open_i + 1:i], i + 1
        i += 1
    return text[open_i + 1:], n


def _arg(args: str, name: str) -> str | None:
    """The raw value of ``name = ...`` in an annotation's arguments (up to the next top-level comma)."""
    m = re.search(rf"(?<![\w.]){name}\s*=\s*", args)
    if not m:
        return None
    depth, i = 0, m.end()
    while i < len(args):
        c = args[i]
        if c == '"':
            s = _STR.match(args, i)
            i = s.end() if s else i + 1
            continue
        if c in "({[":
            depth += 1
        elif c in ")}]":
            if depth == 0:
                break
            depth -= 1
        elif c == "," and depth == 0:
            break
        i += 1
    return args[m.end():i].strip()


_CONCAT = re.compile(r'"(?:[^"\\]|\\.)*"(?:\s*\+\s*"(?:[^"\\]|\\.)*")*')
_CONST = re.compile(r'\b(?:static\s+final|final\s+static)\s+String\s+([A-Za-z_$][\w$]*)\s*=\s*('
                    + _CONCAT.pattern + r')\s*;')


def _strings(value: str | None, consts: dict[str, str] | None = None) -> list[str]:
    """The strings of an annotation value: literals (``"a" + "b"`` joined) and the file's own ``static final
    String`` constants named there (``method = TARGET``)."""
    out: list[str] = []
    v = value or ""
    for m in re.finditer(_CONCAT.pattern + r"|([A-Za-z_$][\w$.]*)", v):
        if m.group(0).startswith('"'):
            out.append("".join(s.group(1) for s in _STR.finditer(m.group(0))))
        elif consts and m.group(1) and m.group(1).rsplit(".", 1)[-1] in consts:
            out.append(consts[m.group(1).rsplit(".", 1)[-1]])
    return out


def string_constants(code: str) -> dict[str, str]:
    """``static final String NAME = "..." (+ "...")*;`` of a file (comments removed)."""
    return {m.group(1): "".join(s.group(1) for s in _STR.finditer(m.group(2))) for m in _CONST.finditer(code)}


def _plain_value(args: str) -> str | None:
    """``@Accessor("x")`` / ``@At("HEAD")``: the unnamed first string."""
    s = args.strip()
    m = _STR.match(s)
    return m.group(1) if m else None


def readable_member(ref: str) -> tuple[str | None, str]:
    """``Lnet/minecraft/world/entity/Mob;setTarget(L...;)V`` -> ("net.minecraft.world.entity.Mob", "setTarget");
    ``checkSpawnRules(L...;)Z`` -> (None, "checkSpawnRules"); a field ``...;xo:D`` -> (owner, "xo")."""
    owner = None
    m = re.match(r"^L([\w/$]+);(.*)$", ref.strip())
    rest = ref.strip()
    if m:
        owner, rest = m.group(1).replace("/", ".").replace("$", "."), m.group(2)
    name = re.split(r"[(:]", rest, maxsplit=1)[0].strip() or rest
    return owner, name


def _classes(args: str, imports: dict[str, str], package: str | None) -> list[str]:
    """The target classes of an ``@Mixin(...)``: ``X.class`` values (through the imports) and ``targets``
    strings."""
    out: list[str] = []
    for m in re.finditer(r"([A-Za-z_][\w.]*)\s*\.\s*class\b", args):
        name = m.group(1)
        head = name.split(".", 1)[0]
        if head in imports:
            out.append(imports[head] + name[len(head):])
        elif "." in name and name[:1].islower():
            out.append(name)
        else:
            out.append(f"{package}.{name}" if package else name)
    for s in _strings(_arg(args, "targets")):
        out.append(s.replace("/", ".").replace("$", "."))
    return list(dict.fromkeys(out))


def point_words(at: str | None) -> str:
    return _POINTS.get((at or "").upper(), f"at {at}" if at else "")


def describe(d: dict) -> str:
    """``@Inject into Mob.checkSpawnRules at HEAD (at its start; can cancel it)`` for an edge's attributes."""
    cls = (d.get("target_class") or "?").rsplit(".", 1)[-1]
    methods = d.get("target_methods") or []
    if d.get("relation") == ACCESS_RELATION:
        return f"@{d.get('kind')} for {cls}.{d.get('target_member')}"
    where = ", ".join(f"{cls}.{m}" for m in methods) or cls
    s = f"@{d.get('kind')} into {where}"
    if d.get("at"):
        s += f" at {d['at']}"
        if d.get("at_target"):
            s += f" ({d['at_target']})"
    notes = [w for w in (point_words(d.get("at")), "can cancel it" if d.get("cancellable") else "") if w]
    if d.get("kind") == "Overwrite":
        notes = ["replaces the method"]
    return s + (f" - {'; '.join(notes)}" if notes else "")


def mixin_edges(g, read=None) -> list[tuple[str, str, dict]]:
    """``injects`` / ``accesses`` edges from mixin handler methods to their target classes (not applied)."""
    from verinoda.index import JVM_SUFFIXES

    files: dict[str, list[str]] = {}
    for n, d in g.G.nodes(data=True):
        f = d.get("source_file") or ""
        if f.endswith(".java") and d.get("_callable") and not d.get("_callable_class"):
            files.setdefault(f, []).append(n)
    if not files:
        return []
    by_label: dict[str, list[str]] = {}
    for n, d in g.G.nodes(data=True):
        lab = d.get("label")
        if lab and d.get("file_type") == "code" and "." in lab:
            by_label.setdefault(lab, []).append(n)
    classes: dict[tuple[str, str], str] = {}  # (package, simple name) -> class node of the project
    for n, d in g.G.nodes(data=True):
        f = d.get("source_file") or ""
        if f.endswith(JVM_SUFFIXES) and d.get("_callable_class"):
            classes.setdefault((f.rsplit("/", 1)[0].replace("/", "."), d.get("label") or ""), n)

    def class_node(fqn: str) -> str | None:
        hit = by_label.get(fqn)
        if hit:
            return hit[0]
        pkg, _, simple = fqn.rpartition(".")
        return next((n for (p, s), n in classes.items() if s == simple and p.endswith(pkg)), None)

    if read is None:
        def read(f: str) -> str | None:
            try:
                return (g.root / f).read_text(encoding="utf-8", errors="replace")
            except OSError:
                return None
    out: list[tuple[str, str, dict]] = []
    for f in sorted(files):
        text = read(f)
        if not text or "Mixin" not in text:
            continue
        code = strip_comments(text)
        consts = string_constants(code)
        mm = _MIXIN.search(code)
        if not mm:
            continue
        imports = {i.rsplit(".", 1)[-1]: i for i in _IMPORT.findall(code)}
        pk = _PACKAGE.search(code)
        margs, _ = _balanced(code, mm.end() - 1)
        targets = _classes(margs, imports, pk.group(1) if pk else None)
        nodes = [(t, class_node(t)) for t in targets]
        nodes = [(t, n) for t, n in nodes if n]
        if not nodes:
            continue
        starts = [0]
        for k, ch in enumerate(code):
            if ch == "\n":
                starts.append(k + 1)
        spans = sorted((sp[0], sp[1], m) for m in files[f] if (sp := g.span(m)))
        for am in _ANN.finditer(code, mm.end()):
            kind = am.group(1)
            line = bisect.bisect_right(starts, am.start())
            args, after = ("", am.end())
            rest = code[am.end():]
            if rest.lstrip().startswith("("):
                args, after = _balanced(code, am.end() + (len(rest) - len(rest.lstrip())))
            decl = re.search(r"([A-Za-z_$][\w$]*)\s*\(", code[after:after + 600])
            handler = next((m for a, b, m in spans if a <= line <= b), None)
            if handler is None and decl:  # the method's span may start at its name, below the annotation
                hl = line + code[after:after + decl.start()].count("\n")
                handler = next((m for a, b, m in spans if a <= hl <= b), None)
            if handler is None:
                continue
            d: dict = {"relation": MIXIN_RELATION, "confidence": "EXTRACTED", "confidence_score": 1.0,
                       "_origin": MIXIN_ORIGIN, "source_file": f, "source_location": f"L{line}", "kind": kind}
            if kind in ACCESSORS:
                member = _plain_value(args) or _arg_string(args, "value", consts)
                if not member and decl:
                    nm = decl.group(1)
                    member = re.sub(r"^(get|set|is|call|invoke)(?=[A-Z])", "", nm)
                    member = member[:1].lower() + member[1:]
                d.update(relation=ACCESS_RELATION, target_member=member or "?")
            else:
                methods = _strings(_arg(args, "method"), consts) or (_strings(_arg(args, "targets"), consts) if kind == "Overwrite"
                                                             else [])
                if kind == "Overwrite" and not methods and decl:
                    methods = [decl.group(1)]
                d["target_methods"] = [readable_member(x)[1] for x in methods]
                d["target_refs"] = methods
                at_raw = _arg(args, "at")
                if at_raw:
                    am2 = re.search(r"@(?:[\w.]*\.)?At\s*\(", at_raw)
                    at_args = _balanced(at_raw, am2.end() - 1)[0] if am2 else at_raw
                    point = _plain_value(at_args) or _arg_string(at_args, "value", consts)
                    tgt = _arg_string(at_args, "target", consts)
                    if point:
                        d["at"] = point
                    if tgt:
                        owner, member = readable_member(tgt)
                        d["at_target"] = f"{owner.rsplit('.', 1)[-1]}.{member}" if owner else member
                if re.search(r"(?<![\w.])cancellable\s*=\s*true\b", args):
                    d["cancellable"] = True
            for t, n in nodes:
                e = dict(d, target_class=t)
                e["context"] = describe(e)
                out.append((handler, n, e))
    return out


def _arg_string(args: str, name: str, consts: dict[str, str] | None = None) -> str | None:
    v = _strings(_arg(args, name), consts)
    return v[0] if v else None
