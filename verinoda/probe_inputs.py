"""Inputs for ``verinoda probe``: strategies from the syntax tree only (docs/DESIGN.md D36).

Nothing here imports or runs the analysed project's code. Everything is read
from source with :mod:`ast`:

* **Types** from annotations (``int``, ``float``, ``str``, ``bytes``, ``bool``,
  ``Decimal``, ``Optional``/``X | None``, ``Union``, ``list``/``tuple``/``dict``/
  ``set`` and their ``typing`` spellings, ``Literal``, enum members read from the
  class body, project classes through *recipes*), refined by the literal
  arguments seen at call sites (``compute_total([{"price": 10.0, "qty": 2}])``
  gives lists of dicts with a float ``price`` and an int ``qty``). A literal
  argument of a caller's parameter that is passed on (``place_order(repo, c,
  items)`` -> ``validate_items(items)``) is followed two levels up.
* **Recipes**: a project class is built only from a constructor call with
  literal arguments found in the code or tests (``OrderRepository(":memory:")``),
  inside the isolated run - never a guessed constructor.
* **Boundaries** mined from the function (base and working-tree versions) and
  the project functions it calls: ``x OP C`` gives C, the next float either
  side, C +- 1 (+0.05, +0.005 for floats); ``len(p) OP C`` and slice bounds give
  sizes C-1, C, C+1; C may be a literal, a module constant or a constant imported
  from a project module (``int(os.environ.get("K", "50"))`` reads its default),
  with the defining line kept as the source; ``round``/``int``/``floor`` add
  values on rounding edges; compared strings are kept as they are.
* **Standard edges**: 0, +-1, 2^31/2^63 bounds, -0.0, nan, +-inf, 1e308, the
  smallest subnormal, '', whitespace, Turkish letters (İstanbul, ı, Çiçek), ß, a
  combining mark, right-to-left text, emoji, a 100,000-character string, empty
  and 10,000-element collections, None where Optional.
* **Generated** values fill the remaining budget: with hypothesis when it is
  installed (``derandomize`` off, ``@seed``, generate phase only, no example
  database), else a fixed pseudo-random list from the same seed.

The corpus is JSON (:func:`encode`; special floats, bytes, tuples, sets and
repeated collections are tagged) and is identical for the base and the
working-tree run.
"""

from __future__ import annotations

import ast
import base64
import decimal
import hashlib
import json
import math
import random
from dataclasses import dataclass, field
from pathlib import Path

INT_EDGES = [0, 1, -1, 2, 10, 100, 255, 256, 1000, -1000, 2**31 - 1, -2**31, 2**31, 2**63 - 1, -2**63, 10**18]
FLOAT_EDGES = [0.0, -0.0, 1.0, -1.0, 0.1, 0.3, 0.5, 2.5, 1e-9, 5e-324, 1e16, 1234.5678, 1e308, -1e308,
               float("inf"), float("-inf"), float("nan")]
ROUNDING_EDGES = [0.5, 1.5, -0.5, 0.125, 2.675, 1.005, 1.015, 0.045, 12.345, 100.05, 100.005, 99.995]
STR_EDGES = ["", " ", "a", "A", "abc", "Hello World", "  padded  ", "tab\tsep", "line\nbreak", "0", "-1", "3.14",
             "İstanbul", "ıi", "Çiçek", "ğüşöç", "ISTANBUL", "ß", "ǅ", "e\u0301", "Å", "שלום", "مرحبا", "😀",
             "👍🏽", "\x00", "O'Brien", "ＡＢＣ", "\u200b", "a-b_c.d"]
LARGE_STR = 100_000
LARGE_LIST = 10_000
BYTES_EDGES = [b"", b"\x00", b"abc", b"\xff\xfe", bytes(range(32))]
DECIMAL_EDGES = ["0", "1", "-1", "0.01", "1.005", "2.675", "100", "1E+10"]
ANY_MIX = [None, 0, 1, -1, 1.5, "", "a", "İstanbul", [], [1], True]
MAX_EDGES_PER_PARAM = 60
MAX_SHAPE_KEY_EDGES = 16
_ENUM_BASES = {"Enum", "IntEnum", "StrEnum", "Flag", "IntFlag"}
_LIST_NAMES = {"list", "List", "Sequence", "MutableSequence", "Iterable", "Collection", "Reversible"}
_DICT_NAMES = {"dict", "Dict", "Mapping", "MutableMapping", "OrderedDict", "DefaultDict", "defaultdict"}
_SET_NAMES = {"set", "Set", "frozenset", "FrozenSet", "AbstractSet", "MutableSet"}
_TUPLE_NAMES = {"tuple", "Tuple"}
_SCALARS = {"int": "int", "float": "float", "str": "str", "bytes": "bytes", "bytearray": "bytes", "bool": "bool",
            "Decimal": "decimal", "None": "none", "NoneType": "none"}
_ANY = {"Any", "object"}


class Raw:
    """An already encoded value (a recipe or an enum member) inside generated data."""

    def __init__(self, enc: dict):
        self.enc = enc


# -- encoding ------------------------------------------------------------------------------

def encode(v):
    """JSON-safe encoding of a value (the plugin's ``decode`` is the inverse)."""
    if isinstance(v, Raw):
        return v.enc
    if v is None or isinstance(v, (bool, str)):
        return v
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            return {"$f": repr(v)}
        return v
    if isinstance(v, decimal.Decimal):
        return {"$dec": str(v)}
    if isinstance(v, (bytes, bytearray)):
        return {"$b": base64.b64encode(bytes(v)).decode("ascii")}
    if isinstance(v, list):
        return [encode(x) for x in v]
    if isinstance(v, tuple):
        return {"$t": [encode(x) for x in v]}
    if isinstance(v, frozenset):
        return {"$fs": [encode(x) for x in sorted(v, key=repr)]}
    if isinstance(v, set):
        return {"$set": [encode(x) for x in sorted(v, key=repr)]}
    if isinstance(v, dict):
        return {"$d": [[encode(k), encode(x)] for k, x in v.items()]}
    raise TypeError(f"cannot encode {type(v).__name__}")


def canon(enc) -> str:
    return json.dumps(enc, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def _vary_src(v, k: int):
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, (int, float)):
        return v + k
    if isinstance(v, str):
        return f"{v}{k}"
    if isinstance(v, list):
        return [_vary_src(x, k) for x in v]
    if isinstance(v, tuple):
        return tuple(_vary_src(x, k) for x in v)
    if isinstance(v, dict):
        return {key: _vary_src(x, k) for key, x in v.items()}
    return v


def plain(enc):
    """The Python value an encoding stands for, when it is a plain literal (no recipe/enum), else raise."""
    if isinstance(enc, list):
        return [plain(x) for x in enc]
    if not isinstance(enc, dict):
        return enc
    if "$f" in enc:
        return float(enc["$f"])
    if "$d" in enc:
        return {plain(k): plain(x) for k, x in enc["$d"]}
    if "$t" in enc:
        return tuple(plain(x) for x in enc["$t"])
    if "$set" in enc:
        return {plain(x) for x in enc["$set"]}
    if "$fs" in enc:
        return frozenset(plain(x) for x in enc["$fs"])
    if "$b" in enc:
        return base64.b64decode(enc["$b"])
    if "$dec" in enc:
        return decimal.Decimal(enc["$dec"])
    if "$rep" in enc:
        el, n = enc["$rep"]
        return [plain(el) for _ in range(int(n))]
    if "$seq" in enc:
        el, n = enc["$seq"]
        return [_vary_src(plain(el), k) for k in range(int(n))]
    if "$srep" in enc:
        s, n = enc["$srep"]
        return str(s) * int(n)
    raise ValueError("not a plain literal")


def to_source(enc, *, full: bool = False, limit: int = 160) -> str:
    """Python source for an encoded value: compact for reports, complete (``full``) for emitted tests."""
    def src(e) -> str:
        if isinstance(e, list):
            return "[" + ", ".join(src(x) for x in e) + "]"
        if not isinstance(e, dict):
            return repr(e)
        if "$f" in e:
            return f"float({e['$f']!r})"
        if "$d" in e:
            return "{" + ", ".join(f"{src(k)}: {src(x)}" for k, x in e["$d"]) + "}"
        if "$t" in e:
            items = [src(x) for x in e["$t"]]
            return "(" + ", ".join(items) + ("," if len(items) == 1 else "") + ")"
        if "$set" in e or "$fs" in e:
            items = [src(x) for x in e.get("$set", e.get("$fs", []))]
            body = "{" + ", ".join(items) + "}" if items else "set()"
            return f"frozenset({body})" if "$fs" in e else body
        if "$b" in e:
            return repr(base64.b64decode(e["$b"]))
        if "$dec" in e:
            return f"Decimal({e['$dec']!r})"
        if "$rep" in e:
            el, n = e["$rep"]
            return f"[{src(el)} for _ in range({int(n)})]"
        if "$seq" in e:
            el, n = e["$seq"]
            if full and int(n) <= 60:
                return repr(plain(e))
            return f"<{int(n)} items like {src(el)}>" if not full else repr(plain(e))
        if "$srep" in e:
            s, n = e["$srep"]
            if len(str(s)) * int(n) <= 40:
                return repr(str(s) * int(n))
            return f"{str(s)!r} * {int(n)}"
        if "$new" in e:
            spec = e["$new"]
            parts = [src(a) for a in spec.get("a", [])] + [f"{k}={src(x)}" for k, x in spec.get("k", [])]
            return f"{spec['n']}({', '.join(parts)})"
        if "$enum" in e:
            return f"{e['$enum']['n']}.{e['$enum']['v']}"
        return repr(e)
    text = src(enc)
    return text if full or len(text) <= limit else text[: limit - 3] + "..."


def call_source(case: dict, *, full: bool = False, limit: int = 200) -> str:
    parts = [to_source(a, full=full, limit=limit) for a in case.get("a", [])]
    parts += [f"{k}={to_source(v, full=full, limit=limit)}" for k, v in case.get("k", [])]
    text = ", ".join(parts)
    return text if full or len(text) <= limit else text[: limit - 3] + "..."


def imports_needed(enc, out: dict[str, set[str]] | None = None) -> dict[str, set[str]]:
    """``{module: {names}}`` an emitted test needs to rebuild ``enc`` (recipes, enums, Decimal)."""
    out = out if out is not None else {}
    if isinstance(enc, list):
        for x in enc:
            imports_needed(x, out)
    elif isinstance(enc, dict):
        if "$new" in enc:
            out.setdefault(enc["$new"]["m"], set()).add(enc["$new"]["n"])
            for a in enc["$new"].get("a", []):
                imports_needed(a, out)
            for _, x in enc["$new"].get("k", []):
                imports_needed(x, out)
        elif "$enum" in enc:
            out.setdefault(enc["$enum"]["m"], set()).add(enc["$enum"]["n"])
        elif "$dec" in enc:
            out.setdefault("decimal", set()).add("Decimal")
        else:
            for v in enc.values():
                imports_needed(v, out)
    return out


def complexity(enc) -> tuple:
    """Order for picking the simplest counterexample: no special floats, ASCII, short, small."""
    special = nonascii = length = 0
    mag = 0.0

    def walk(e) -> None:
        nonlocal special, nonascii, length, mag
        if isinstance(e, bool) or e is None:
            return
        if isinstance(e, (int, float)):
            mag += min(abs(float(e)), 1e18)
            return
        if isinstance(e, str):
            length += len(e)
            nonascii += sum(1 for c in e if ord(c) > 127)
            return
        if isinstance(e, list):
            length += len(e)
            for x in e:
                walk(x)
            return
        if isinstance(e, dict):
            if "$f" in e:
                special += 1
            elif "$rep" in e or "$seq" in e:
                length += int(e.get("$rep", e.get("$seq"))[1])
                walk(e.get("$rep", e.get("$seq"))[0])
            elif "$srep" in e:
                length += len(e["$srep"][0]) * int(e["$srep"][1])
            elif "$new" in e:
                walk(e["$new"].get("a", []))
            else:
                for v in e.values():
                    walk(v)
    walk(enc)
    return special, nonascii, length, mag


# -- type descriptors ----------------------------------------------------------------------

def _ann_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Constant) and node.value is None:
        return "None"
    return None


def annotation_td(node: ast.AST | None, resolve_class=None, depth: int = 0) -> dict:
    """A type descriptor for an annotation node; ``resolve_class(name)`` -> td for project classes (or None)."""
    if node is None:
        return {"k": "any", "why": "no annotation"}
    if depth > 6:
        return {"k": "any", "why": "annotation nested too deeply"}
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        try:
            return annotation_td(ast.parse(node.value, mode="eval").body, resolve_class, depth + 1)
        except SyntaxError:
            return {"k": "any", "why": f"unparsable annotation {node.value!r}"}
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        parts = [annotation_td(node.left, resolve_class, depth + 1), annotation_td(node.right, resolve_class,
                                                                                   depth + 1)]
        return _union(parts)
    name = _ann_name(node)
    if name is not None:
        if name in _SCALARS:
            return {"k": _SCALARS[name]}
        if name in _ANY:
            return {"k": "any", "why": f"annotated {name}"}
        if name in _LIST_NAMES:
            return {"k": "list", "of": {"k": "any", "why": "unparameterised"}}
        if name in _DICT_NAMES:
            return {"k": "dict", "key": {"k": "str"}, "val": {"k": "any", "why": "unparameterised"}}
        if name in _SET_NAMES:
            return {"k": "set", "of": {"k": "int"}}
        if name in _TUPLE_NAMES:
            return {"k": "tuple", "of": {"k": "any", "why": "unparameterised"}}
        if name in ("Callable", "Iterator", "Generator", "AsyncIterator", "Awaitable", "Coroutine", "IO",
                    "TextIO", "BinaryIO", "Type", "type"):
            return {"k": "unsupported", "why": f"values of type {name} are not generated"}
        if resolve_class is not None:
            td = resolve_class(name)
            if td is not None:
                return td
        return {"k": "unsupported", "why": f"no way to build a {name} (not a known type, no recipe)"}
    if isinstance(node, ast.Subscript):
        head = _ann_name(node.value)
        sl = node.slice
        args = list(sl.elts) if isinstance(sl, ast.Tuple) else [sl]
        if head == "Optional":
            return _union([annotation_td(args[0], resolve_class, depth + 1), {"k": "none"}])
        if head == "Union":
            return _union([annotation_td(a, resolve_class, depth + 1) for a in args])
        if head == "Annotated":
            return annotation_td(args[0], resolve_class, depth + 1)
        if head == "Literal":
            vals = []
            for a in args:
                try:
                    vals.append(ast.literal_eval(a))
                except (ValueError, SyntaxError, TypeError):
                    continue
            return {"k": "literal", "values": [encode(v) for v in vals if _encodable(v)]}
        if head in _LIST_NAMES:
            return {"k": "list", "of": annotation_td(args[0], resolve_class, depth + 1)}
        if head in _SET_NAMES:
            return {"k": "set", "of": annotation_td(args[0], resolve_class, depth + 1)}
        if head in _DICT_NAMES and len(args) == 2:
            return {"k": "dict", "key": annotation_td(args[0], resolve_class, depth + 1),
                    "val": annotation_td(args[1], resolve_class, depth + 1)}
        if head in _TUPLE_NAMES:
            if len(args) == 2 and isinstance(args[1], ast.Constant) and args[1].value is Ellipsis:
                return {"k": "tuple", "of": annotation_td(args[0], resolve_class, depth + 1)}
            return {"k": "tuple", "items": [annotation_td(a, resolve_class, depth + 1) for a in args]}
        return annotation_td(node.value, resolve_class, depth + 1)
    return {"k": "any", "why": f"annotation {ast.unparse(node)[:40]!r} not understood"}


def _encodable(v) -> bool:
    try:
        encode(v)
        return True
    except TypeError:
        return False


def _union(parts: list[dict]) -> dict:
    flat: list[dict] = []
    for p in parts:
        flat.extend(p["of"] if p.get("k") == "union" else [p])
    if len(flat) == 2 and {"k": "none"} in flat:
        other = next(p for p in flat if p != {"k": "none"})
        return {"k": "optional", "of": other}
    return {"k": "union", "of": flat}


def observed_td(v) -> dict:
    """The type descriptor of one literal value seen at a call site."""
    if v is None:
        return {"k": "none"}
    if isinstance(v, bool):
        return {"k": "bool"}
    if isinstance(v, int):
        return {"k": "int"}
    if isinstance(v, float):
        return {"k": "float"}
    if isinstance(v, str):
        return {"k": "str"}
    if isinstance(v, (bytes, bytearray)):
        return {"k": "bytes"}
    if isinstance(v, list):
        return {"k": "list", "of": merge_observed([observed_td(x) for x in v]) if v else {"k": "any", "why": "empty"}}
    if isinstance(v, tuple):
        return {"k": "tuple", "items": [observed_td(x) for x in v]}
    if isinstance(v, (set, frozenset)):
        return {"k": "set", "of": merge_observed([observed_td(x) for x in v]) if v else {"k": "int"}}
    if isinstance(v, dict):
        if v and all(isinstance(k, str) for k in v) and len(v) <= 20:
            return {"k": "shape", "keys": {k: observed_td(x) for k, x in v.items()}, "example": encode(v)}
        return {"k": "dict", "key": {"k": "str"}, "val": {"k": "any", "why": "mixed"}}
    return {"k": "any", "why": type(v).__name__}


def merge_observed(tds: list[dict]) -> dict:
    """One descriptor for several observed values (the first informative one wins per shape key)."""
    tds = [t for t in tds if t.get("k") != "any"] or tds
    if not tds:
        return {"k": "any", "why": "nothing observed"}
    first = tds[0]
    if first.get("k") == "shape":
        keys = dict(first["keys"])
        for t in tds[1:]:
            if t.get("k") == "shape":
                for k, sub in t["keys"].items():
                    keys.setdefault(k, sub)
        return {**first, "keys": keys}
    if first.get("k") == "list":
        subs = [t["of"] for t in tds if t.get("k") == "list" and t["of"].get("k") != "any"]
        return {"k": "list", "of": merge_observed(subs) if subs else first["of"]}
    kinds = {t.get("k") for t in tds}
    if kinds == {"int", "float"}:
        return {"k": "float"}
    return first


def refine(ann: dict, obs: dict | None) -> dict:
    """The annotation's descriptor, with unknown parts filled in from what call sites pass."""
    if obs is None:
        return ann
    k = ann.get("k")
    if k == "any":
        return obs if obs.get("k") not in ("any", None) else ann
    if k == "list" and obs.get("k") == "list":
        return {"k": "list", "of": refine(ann["of"], obs["of"])}
    if k == "dict" and obs.get("k") == "shape":
        return obs if ann.get("val", {}).get("k") == "any" else ann
    if k == "optional":
        return {"k": "optional", "of": refine(ann["of"], obs if obs.get("k") != "none" else None)}
    return ann


def in_domain(td: dict) -> bool:
    k = td.get("k")
    if k in ("any", "unsupported"):
        return False
    if k in ("list", "set", "optional"):
        return in_domain(td["of"])
    if k == "tuple":
        return all(in_domain(t) for t in td.get("items", [])) if "items" in td else in_domain(td["of"])
    if k == "dict":
        return in_domain(td["key"]) and in_domain(td["val"])
    if k == "union":
        return all(in_domain(t) for t in td["of"])
    if k == "shape":
        return all(in_domain(t) for t in td["keys"].values())
    return True


def unsupported_reason(td: dict) -> str | None:
    k = td.get("k")
    if k == "unsupported":
        return td.get("why") or "unsupported type"
    for sub in ([td["of"]] if isinstance(td.get("of"), dict) else td.get("of") or []) + \
            list(td.get("items") or []) + [td[x] for x in ("key", "val") if isinstance(td.get(x), dict)] + \
            list((td.get("keys") or {}).values()):
        why = unsupported_reason(sub) if isinstance(sub, dict) else None
        if why:
            return why
    return None


# -- boundaries ----------------------------------------------------------------------------

@dataclass
class Bounds:
    numbers: dict[str, str] = field(default_factory=dict)     # canon(value) -> source text
    lengths: dict[int, str] = field(default_factory=dict)     # size -> source text
    strings: dict[str, str] = field(default_factory=dict)     # literal -> source text
    rounding: bool = False

    def add_number(self, v, src: str) -> None:
        if isinstance(v, bool) or not isinstance(v, (int, float)) or (isinstance(v, float) and not math.isfinite(v)):
            return
        if abs(v) > 1e18:
            return
        self.numbers.setdefault(canon(v), src)

    def add_length(self, n, src: str) -> None:
        if isinstance(n, int) and not isinstance(n, bool) and 0 <= abs(n) <= 100_000:
            self.lengths.setdefault(abs(n), src)

    def add_string(self, s, src: str) -> None:
        if isinstance(s, str) and len(s) <= 200:
            self.strings.setdefault(s, src)

    def summary(self) -> list[dict]:
        out = [{"kind": "number", "value": json.loads(k), "source": s} for k, s in self.numbers.items()]
        out += [{"kind": "length", "value": n, "source": s} for n, s in self.lengths.items()]
        out += [{"kind": "string", "value": v, "source": s} for v, s in self.strings.items()]
        if self.rounding:
            out.append({"kind": "rounding", "value": None, "source": "round/int/floor/ceil in the code"})
        return out


_SAFE_BINOPS = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow)
_ENV_GETS = {"os.environ.get", "os.getenv", "environ.get", "getenv"}


def const_eval(node: ast.AST, lookup=None, depth: int = 0):
    """The value of a constant expression, or raise ValueError. ``lookup(name)`` resolves names.

    Reads the default of ``os.environ.get("K", "50")`` / ``os.getenv`` (the process environment is
    not read: the default is what the code falls back to)."""
    if depth > 8:
        raise ValueError("too deep")
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float, str, bool)) or \
            isinstance(node, ast.Constant) and node.value is None:
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        v = const_eval(node.operand, lookup, depth + 1)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return -v if isinstance(node.op, ast.USub) else v
        raise ValueError("unary on non-number")
    if isinstance(node, ast.BinOp) and isinstance(node.op, _SAFE_BINOPS):
        a = const_eval(node.left, lookup, depth + 1)
        b = const_eval(node.right, lookup, depth + 1)
        if not all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in (a, b)):
            raise ValueError("binop on non-numbers")
        if isinstance(node.op, ast.Pow) and (abs(b) > 64 or abs(a) > 2**16):
            raise ValueError("power too large")
        try:
            return {ast.Add: lambda: a + b, ast.Sub: lambda: a - b, ast.Mult: lambda: a * b,
                    ast.Div: lambda: a / b, ast.FloorDiv: lambda: a // b, ast.Mod: lambda: a % b,
                    ast.Pow: lambda: a ** b}[type(node.op)]()
        except (ZeroDivisionError, OverflowError) as exc:
            raise ValueError(str(exc)) from None
    if isinstance(node, ast.Call) and not node.keywords:
        fname = ast.unparse(node.func)
        if fname in ("int", "float", "str") and len(node.args) == 1:
            v = const_eval(node.args[0], lookup, depth + 1)
            try:
                return {"int": int, "float": float, "str": str}[fname](v)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError(str(exc)) from None
        if fname in _ENV_GETS and len(node.args) == 2:
            return const_eval(node.args[1], lookup, depth + 1)
    if isinstance(node, (ast.Name, ast.Attribute)) and lookup is not None:
        return lookup(node, depth + 1)
    raise ValueError(f"not a constant: {ast.unparse(node)[:40]}")


def _len_target(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "len" \
        and len(node.args) == 1


def mine(fn: ast.AST, rel: str, lookup, bounds: Bounds, define=None) -> Bounds:
    """Add the boundaries of one function body to ``bounds`` (see the module docstring).

    ``define(name)`` gives the ``path:line`` that sets a named constant (shown with the bound)."""
    def cval(node):
        try:
            return const_eval(node, lookup)
        except ValueError:
            return None

    def where(node, value, name: str | None = None) -> str:
        at = define(name) if (define is not None and name) else None
        return f"{rel}:{getattr(node, 'lineno', '?')}" + (f" ({name}" + (f", set at {at}" if at else "") + ")"
                                                          if name else "")

    def named(node) -> str | None:
        if isinstance(node, (ast.Name, ast.Attribute)):
            return ast.unparse(node)
        return None

    for node in ast.walk(fn):
        if isinstance(node, ast.Compare):
            sides = [node.left, *node.comparators]
            for a, op, b in zip(sides, node.ops, sides[1:]):
                for this, other in ((a, b), (b, a)):
                    v = cval(this)
                    if v is None:
                        if isinstance(op, (ast.In, ast.NotIn)) and this is b and isinstance(b, (ast.Tuple, ast.List,
                                                                                               ast.Set)):
                            for elt in b.elts:
                                ev = cval(elt)
                                if isinstance(ev, str):
                                    bounds.add_string(ev, where(elt, ev))
                                elif isinstance(ev, (int, float)):
                                    bounds.add_number(ev, where(elt, ev))
                        continue
                    src = where(node, v, named(this))
                    if isinstance(v, str):
                        bounds.add_string(v, src)
                    elif _len_target(other):
                        bounds.add_length(v, src)
                    else:
                        bounds.add_number(v, src)
        elif isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Slice):
            for part in (node.slice.lower, node.slice.upper):
                if part is not None:
                    v = cval(part)
                    if isinstance(v, int) and not isinstance(v, bool):
                        bounds.add_length(v, where(node, v, named(part)))
        elif isinstance(node, ast.Call):
            fname = ast.unparse(node.func)
            last = fname.rpartition(".")[2]
            if last in ("round", "floor", "ceil", "trunc") or fname in ("int", "math.floor", "math.ceil",
                                                                       "math.trunc", "round"):
                bounds.rounding = True
            if last in ("min", "max", "clamp", "clip"):
                for a in node.args:
                    v = cval(a)
                    if isinstance(v, (int, float)):
                        bounds.add_number(v, where(node, v, named(a)))
            if last in ("startswith", "endswith", "split", "replace", "find", "index", "count") and node.args:
                v = cval(node.args[0])
                if isinstance(v, str):
                    bounds.add_string(v, where(node, v))
    return bounds


# -- value generation ----------------------------------------------------------------------

def _num_bounds(bounds: Bounds | None, as_int: bool) -> list[tuple]:
    out: list[tuple] = []
    for key, src in (bounds.numbers.items() if bounds else []):
        c = json.loads(key)
        tag = f"boundary {c!r} from {src}"
        if as_int:
            base = [math.floor(c), math.ceil(c)] if isinstance(c, float) else [c]
            for b in base:
                for v in (b - 1, b, b + 1):
                    out.append((int(v), tag))
        else:
            c = float(c)
            for v in (c, math.nextafter(c, math.inf), math.nextafter(c, -math.inf), c + 1, c - 1, c + 0.05,
                      c + 0.005, c - 0.005):
                out.append((v, tag))
    return out


def edges(td: dict, bounds: Bounds | None, depth: int = 0) -> list[tuple]:
    """``[(python value or Raw, tag or None)]``: boundary values first, then the standard edges."""
    k = td.get("k")
    if depth > 3:
        return [(typical(td), None)]
    if k == "int":
        vals = _num_bounds(bounds, True) + [(v, None) for v in INT_EDGES]
    elif k == "float":
        vals = _num_bounds(bounds, False)
        if bounds and bounds.rounding:
            vals += [(v, "rounding edge") for v in ROUNDING_EDGES]
        vals += [(v, None) for v in FLOAT_EDGES]
    elif k == "decimal":
        vals = [(decimal.Decimal(repr(json.loads(key))), f"boundary from {src}")
                for key, src in (bounds.numbers.items() if bounds else [])]
        vals += [(decimal.Decimal(s), None) for s in DECIMAL_EDGES]
    elif k == "bool":
        vals = [(True, None), (False, None)]
    elif k == "none":
        vals = [(None, None)]
    elif k == "str":
        vals = []
        for s, src in (bounds.strings.items() if bounds else []):
            tag = f"string {s!r} from {src}"
            vals += [(s, tag), (s.upper(), tag), (s.lower(), tag), (f" {s}", tag), (f"{s} ", tag)]
        for n, src in (bounds.lengths.items() if bounds else []):
            tag = f"length {n} from {src}"
            vals += [(Raw({"$srep": ["a", m]}), tag) for m in (n - 1, n, n + 1) if m >= 0]
        vals += [(s, None) for s in STR_EDGES]
        vals.append((Raw({"$srep": ["a", LARGE_STR]}), "large"))
    elif k == "bytes":
        vals = [(b, None) for b in BYTES_EDGES]
    elif k == "literal":
        vals = [(Raw(v), None) for v in td["values"]]
    elif k == "enum":
        vals = [(Raw({"$enum": {"m": td["m"], "n": td["n"], "v": m}}), None) for m in td["members"]]
    elif k == "recipe":
        vals = [(Raw(r), None) for r in td["recipes"]]
    elif k == "optional":
        vals = [(None, None)] + edges(td["of"], bounds, depth + 1)
    elif k == "union":
        vals = [v for sub in td["of"] for v in edges(sub, bounds, depth + 1)]
    elif k in ("list", "tuple", "set"):
        vals = _collection_edges(td, bounds, depth)
    elif k == "dict":
        kt, vt = typical(td["key"]), typical(td["val"])
        vals = [({}, None), (_pair(kt, vt), None)]
        vals += [(_pair(kt, v), t) for v, t in edges(td["val"], bounds, depth + 1)[:MAX_SHAPE_KEY_EDGES]]
    elif k == "shape":
        vals = _shape_edges(td, bounds, depth)
    else:  # any / unsupported
        vals = [(v, None) for v in ANY_MIX]
    seen: set[str] = set()
    out = []
    for v, t in vals:
        try:
            key = canon(encode(v))
        except TypeError:
            continue
        if key not in seen:
            seen.add(key)
            out.append((v, t))
    return out[:MAX_EDGES_PER_PARAM] if depth == 0 else out


def _pair(k, v) -> dict | Raw:
    if isinstance(k, Raw) or isinstance(v, Raw):
        return Raw({"$d": [[encode(k), encode(v)]]})
    try:
        return {k: v}
    except TypeError:
        return Raw({"$d": [[encode(k), encode(v)]]})


def _unit(td: dict):
    """The neutral value of a descriptor (1, 1.0, 'a'): multiplying or summing keeps boundaries reachable."""
    k = td.get("k")
    return {"int": 1, "float": 1.0, "decimal": decimal.Decimal("1")}.get(k, typical(td))


def typical(td: dict):
    k = td.get("k")
    if k == "int":
        return 1
    if k == "float":
        return 1.0
    if k == "decimal":
        return decimal.Decimal("1.00")
    if k == "str":
        return "abc"
    if k == "bytes":
        return b"abc"
    if k == "bool":
        return True
    if k == "none":
        return None
    if k == "literal":
        return Raw(td["values"][0]) if td["values"] else None
    if k == "enum":
        return Raw({"$enum": {"m": td["m"], "n": td["n"], "v": td["members"][0]}}) if td["members"] else None
    if k == "recipe":
        return Raw(td["recipes"][0])
    if k == "optional":
        return typical(td["of"])
    if k == "union":
        return typical(td["of"][0])
    if k == "list":
        return [typical(td["of"])]
    if k == "set":
        v = typical(td["of"])
        return Raw({"$set": [encode(v)]})
    if k == "tuple":
        if "items" in td:
            return tuple(typical(t) for t in td["items"])
        return (typical(td["of"]),)
    if k == "dict":
        return _pair(typical(td["key"]), typical(td["val"]))
    if k == "shape":
        return Raw(td["example"])
    return 1


def _wrap(k: str, items: list):
    enc = [encode(x) for x in items]
    if k == "tuple":
        return Raw({"$t": enc})
    if k == "set":
        return Raw({"$set": enc})
    return Raw(enc)


def _collection_edges(td: dict, bounds: Bounds | None, depth: int) -> list[tuple]:
    k = td["k"]
    if k == "tuple" and "items" in td:
        base = [typical(t) for t in td["items"]]
        vals = [(tuple(base), None)]
        for i, sub in enumerate(td["items"]):
            for v, t in edges(sub, bounds, depth + 1)[:MAX_SHAPE_KEY_EDGES]:
                row = list(base)
                row[i] = v
                vals.append((_wrap("tuple", row), t))
        return vals
    el = td["of"]
    t0 = typical(el)
    unit = _unit(el) if el.get("k") != "shape" else _unit_shape(el)
    vals = [(_wrap(k, []), None), (_wrap(k, [t0]), None), (_wrap(k, [unit]), None)]
    el_edges = edges(el, bounds, depth + 1)
    if k != "set":
        vals.append((_wrap(k, [t0, t0]), None))
    others = [v for v, _ in el_edges if canon(encode(v)) != canon(encode(t0))][:2]
    if others:
        vals.append((_wrap(k, [t0, *others]), None))
    for n, src in (bounds.lengths.items() if bounds else []):
        tag = f"length {n} from {src}"
        for m in (n - 1, n, n + 1):
            if m >= 0 and (k == "list" or m <= 300):
                vals.append((_sized(k, unit, m), tag))
    if k == "list":
        vals.append((_sized(k, unit, LARGE_LIST), "large"))
    for v, t in el_edges[: max(10, MAX_EDGES_PER_PARAM - len(vals))]:
        vals.append((_wrap(k, [v]), t))
    return vals


def _sized(k: str, el, n: int) -> Raw:
    """A collection of ``n`` distinct variants of ``el`` (numbers + index, strings + index)."""
    if k == "list":
        return Raw({"$seq": [encode(el), int(n)]})
    return _wrap(k, [_vary_src(el if not isinstance(el, Raw) else plain(el.enc), i) for i in range(n)])


def _unit_shape(td: dict):
    """A shape example with every numeric key at its unit value (qty 1, price 1.0)."""
    try:
        ex = plain(td["example"])
    except ValueError:
        return Raw(td["example"])
    if not isinstance(ex, dict):
        return Raw(td["example"])
    out = dict(ex)
    for key, sub in td["keys"].items():
        if sub.get("k") in ("int", "float") and key in out:
            out[key] = _unit(sub)
    return out


def _shape_edges(td: dict, bounds: Bounds | None, depth: int) -> list[tuple]:
    vals: list[tuple] = [(Raw(td["example"]), None)]
    unit = _unit_shape(td)
    vals.append((unit, None))
    if not isinstance(unit, dict):
        return vals
    for key, sub in td["keys"].items():
        for v, t in edges(sub, bounds, depth + 1)[:MAX_SHAPE_KEY_EDGES]:
            row = dict(unit)
            row[key] = v
            if any(isinstance(x, Raw) for x in row.values()):
                vals.append((Raw({"$d": [[encode(a), encode(b)] for a, b in row.items()]}), t))
            else:
                vals.append((row, t))
    return vals


# -- generated values ------------------------------------------------------------------------

def rand_value(td: dict, rng: random.Random, depth: int = 0):
    """A pseudo-random value of a descriptor (the fallback when hypothesis is not installed)."""
    k = td.get("k")
    if depth > 4:
        return typical(td)
    if k == "int":
        return rng.choice([rng.randint(-10, 10), rng.randint(-1000, 1000), rng.randint(-2**40, 2**40)])
    if k == "float":
        r = rng.random()
        if r < 0.05:
            return rng.choice(FLOAT_EDGES)
        return rng.choice([rng.uniform(-10, 10), rng.uniform(-1e6, 1e6), round(rng.uniform(0, 1000), 2),
                           rng.uniform(-1, 1)])
    if k == "decimal":
        return decimal.Decimal(f"{rng.uniform(-1000, 1000):.2f}")
    if k == "str":
        pool = "abcXYZ019 _-.İıÇçŞşĞğÜüÖößé\u0301שم😀\t"
        return "".join(rng.choice(pool) for _ in range(rng.randint(0, 12)))
    if k == "bytes":
        return bytes(rng.randint(0, 255) for _ in range(rng.randint(0, 12)))
    if k == "bool":
        return rng.random() < 0.5
    if k == "none":
        return None
    if k in ("literal", "enum", "recipe"):
        choices = edges(td, None, depth + 1)
        return rng.choice(choices)[0] if choices else None
    if k == "optional":
        return None if rng.random() < 0.2 else rand_value(td["of"], rng, depth + 1)
    if k == "union":
        return rand_value(rng.choice(td["of"]), rng, depth + 1)
    if k in ("list", "set", "tuple"):
        if k == "tuple" and "items" in td:
            return _wrap("tuple", [rand_value(t, rng, depth + 1) for t in td["items"]])
        n = rng.choice([0, 1, 2, 3, 5, 8, rng.randint(0, 20)])
        return _wrap(k, [rand_value(td["of"], rng, depth + 1) for _ in range(n)])
    if k == "dict":
        n = rng.randint(0, 4)
        return Raw({"$d": [[encode(rand_value(td["key"], rng, depth + 1)), encode(rand_value(td["val"], rng,
                                                                                               depth + 1))]
                           for _ in range(n)]})
    if k == "shape":
        return Raw({"$d": [[key, encode(rand_value(sub, rng, depth + 1))] for key, sub in td["keys"].items()]})
    return rng.choice(ANY_MIX)


def hypothesis_strategy(td: dict, st, depth: int = 0):
    """A hypothesis strategy for a descriptor (values or :class:`Raw` encodings)."""
    k = td.get("k")
    if depth > 4:
        return st.just(typical(td))
    if k == "int":
        return st.one_of(st.integers(-1000, 1000), st.integers())
    if k == "float":
        return st.floats(allow_nan=True, allow_infinity=True)
    if k == "decimal":
        return st.decimals(allow_nan=False, allow_infinity=False, places=3, min_value=-10**9, max_value=10**9)
    if k == "str":
        return st.text(max_size=30)
    if k == "bytes":
        return st.binary(max_size=30)
    if k == "bool":
        return st.booleans()
    if k == "none":
        return st.none()
    if k in ("literal", "enum", "recipe"):
        choices = [v for v, _ in edges(td, None, depth + 1)]
        return st.sampled_from(choices) if choices else st.none()
    if k == "optional":
        return st.one_of(st.none(), hypothesis_strategy(td["of"], st, depth + 1))
    if k == "union":
        return st.one_of(*[hypothesis_strategy(t, st, depth + 1) for t in td["of"]])
    if k == "list":
        return st.lists(hypothesis_strategy(td["of"], st, depth + 1), max_size=20).map(lambda xs: _wrap("list", xs))
    if k == "set":
        return st.lists(hypothesis_strategy(td["of"], st, depth + 1), max_size=8).map(lambda xs: _set_or_list(xs))
    if k == "tuple":
        if "items" in td:
            return st.tuples(*[hypothesis_strategy(t, st, depth + 1) for t in td["items"]]).map(
                lambda xs: _wrap("tuple", list(xs)))
        return st.lists(hypothesis_strategy(td["of"], st, depth + 1), max_size=8).map(lambda xs: _wrap("tuple", xs))
    if k == "dict":
        return st.lists(st.tuples(hypothesis_strategy(td["key"], st, depth + 1),
                                  hypothesis_strategy(td["val"], st, depth + 1)), max_size=5).map(
            lambda kv: Raw({"$d": [[encode(a), encode(b)] for a, b in kv]}))
    if k == "shape":
        keys = list(td["keys"])
        return st.tuples(*[hypothesis_strategy(td["keys"][key], st, depth + 1) for key in keys]).map(
            lambda vals: Raw({"$d": [[key, encode(v)] for key, v in zip(keys, vals)]}))
    return st.sampled_from(ANY_MIX)


def _set_or_list(xs: list):
    try:
        encs = {canon(encode(x)): encode(x) for x in xs}
    except TypeError:
        return _wrap("list", xs)
    return Raw({"$set": list(encs.values())})


def generated(tds: list[dict], n: int, seed: int) -> tuple[list[list], str]:
    """``n`` generated argument tuples (encoded) and which generator made them."""
    if n <= 0 or not tds:
        return [], "none"
    try:
        from hypothesis import HealthCheck, Phase, given, settings
        from hypothesis import seed as hseed
        from hypothesis import strategies as st
        import hypothesis as _h
    except ImportError:
        rng = random.Random(seed)
        out = []
        for _ in range(n):
            try:
                out.append([encode(rand_value(td, rng)) for td in tds])
            except TypeError:
                continue
        return out, "fixed pseudo-random list (hypothesis is not installed)"
    strat = st.tuples(*[hypothesis_strategy(td, st) for td in tds])
    got: list[list] = []

    @hseed(seed)
    @settings(max_examples=n, database=None, phases=[Phase.generate], deadline=None, derandomize=False,
              suppress_health_check=list(HealthCheck))
    @given(strat)
    def collect(args):
        try:
            got.append([encode(a) for a in args])
        except TypeError:
            pass

    collect()
    return got, f"hypothesis {_h.__version__} (seed {seed}, generate phase, no example database)"


def corpus_sha256(cases: list[dict]) -> str:
    return hashlib.sha256(canon(cases).encode("ascii")).hexdigest()


def module_name(rel: str, exists) -> tuple[str, str]:
    """(dotted module name, import root) for a project-relative ``.py`` path: packages are walked up while
    their ``__init__.py`` exists (``exists(rel)``)."""
    parts = list(Path(rel).with_suffix("").as_posix().split("/"))
    if parts[-1] == "__init__":
        parts = parts[:-1]
    i = len(parts) - 1
    while i > 0 and exists("/".join(parts[:i]) + "/__init__.py"):
        i -= 1
    root = "/".join(parts[:i]) or "."
    return ".".join(parts[i:]), root

