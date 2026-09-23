"""Minimal-diff edits of one member inside a JSON config file.

Only the ``"repoatlas"`` member is added, replaced or removed; every other byte
of the file (formatting, key order, other servers) is left as it was. The
spans are found with the stdlib's own scanners (``json.decoder.scanstring`` and
``JSONDecoder.raw_decode``), so there is no hand-written value parser. Every
edit is checked by parsing the result and comparing it with the expected
document; callers fall back to a full re-serialisation when that check fails.
"""

from __future__ import annotations

import json
import re
from json.decoder import scanstring

_WS = " \t\r\n"
_DEC = json.JSONDecoder()


class JsonEditError(ValueError):
    pass


def _skip_ws(s: str, i: int) -> int:
    while i < len(s) and s[i] in _WS:
        i += 1
    return i


def members(s: str, open_i: int) -> tuple[list[tuple[str, int, int, int, str]], int]:
    """Members of the object whose ``{`` is at ``open_i``.

    Returns ``([(key, key_start, value_start, value_end, separator)], close_index)``.
    """
    if open_i >= len(s) or s[open_i] != "{":
        raise JsonEditError("expected an object")
    out: list[tuple[str, int, int, int, str]] = []
    i = _skip_ws(s, open_i + 1)
    if i < len(s) and s[i] == "}":
        return out, i
    while True:
        if i >= len(s) or s[i] != '"':
            raise JsonEditError(f"expected a key at offset {i}")
        key, j = scanstring(s, i + 1)
        k = _skip_ws(s, j)
        if k >= len(s) or s[k] != ":":
            raise JsonEditError(f"expected ':' at offset {k}")
        vs = _skip_ws(s, k + 1)
        _, ve = _DEC.raw_decode(s, vs)
        out.append((key, i, vs, ve, s[j:vs]))
        k = _skip_ws(s, ve)
        if k < len(s) and s[k] == ",":
            i = _skip_ws(s, k + 1)
            continue
        if k < len(s) and s[k] == "}":
            return out, k
        raise JsonEditError(f"expected ',' or '}}' at offset {k}")


def top_object(s: str) -> int:
    i = _skip_ws(s, 0)
    if i >= len(s) or s[i] != "{":
        raise JsonEditError("top level is not a JSON object")
    return i


def _find(ms, key):
    hit = None
    for idx, m in enumerate(ms):
        if m[0] == key:
            hit = idx  # json.loads keeps the last duplicate; so do we
    return hit


def _line_lead(s: str, pos: int) -> str:
    ls = s.rfind("\n", 0, pos) + 1
    return re.match(r"[ \t]*", s[ls:]).group(0)


def newline_of(s: str) -> str:
    return "\r\n" if "\r\n" in s else "\n"


def indent_unit(s: str) -> str:
    """Indentation step used by the file (default two spaces)."""
    try:
        ms, _ = members(s, top_object(s))
    except (JsonEditError, ValueError):
        return "  "
    if ms and "\n" in s[top_object(s):ms[0][1]]:
        lead = _line_lead(s, ms[0][1])
        if lead:
            return lead
    return "  "


def _dump(value, unit: str, lead: str, nl: str) -> str:
    txt = json.dumps(value, indent=unit, ensure_ascii=False)
    return (nl + lead).join(txt.split("\n"))


def _insert(s: str, open_i: int, key: str, value, unit: str, nl: str) -> str:
    ms, close_i = members(s, open_i)
    k = json.dumps(key, ensure_ascii=False)
    if ms:
        sep = ms[0][4] if ms[0][4].strip() == ":" else ": "
        if "\n" in s[open_i:ms[0][1]]:
            lead = _line_lead(s, ms[0][1])
            text = "," + nl + lead + k + sep + _dump(value, unit, lead, nl)
        else:  # single-line object: stay single-line
            text = ", " + k + sep + json.dumps(value, ensure_ascii=False)
        pos = ms[-1][3]
        return s[:pos] + text + s[pos:]
    base = _line_lead(s, open_i)
    lead = base + unit
    text = nl + lead + k + ": " + _dump(value, unit, lead, nl) + nl + base
    return s[: open_i + 1] + text + s[close_i:]


def _remove(s: str, open_i: int, idx: int) -> str:
    ms, close_i = members(s, open_i)
    _, ks, _, ve, _ = ms[idx]
    if len(ms) == 1:
        return s[: open_i + 1] + s[close_i:]
    if idx == len(ms) - 1:
        return s[: ms[idx - 1][3]] + s[ve:]
    return s[:ks] + s[ms[idx + 1][1]:]


def set_member(s: str, path: tuple[str, str], value) -> str:
    """Set ``obj[path[0]][path[1]] = value`` touching only that span."""
    outer, name = path
    unit, nl = indent_unit(s), newline_of(s)
    top = top_object(s)
    ms, _ = members(s, top)
    oi = _find(ms, outer)
    if oi is None:
        return _insert(s, top, outer, {name: value}, unit, nl)
    vs = ms[oi][2]
    if s[vs] != "{":
        raise JsonEditError(f"{outer!r} is not an object")
    inner, _ = members(s, vs)
    ii = _find(inner, name)
    if ii is None:
        return _insert(s, vs, name, value, unit, nl)
    _, _, ivs, ive, _ = inner[ii]
    lead = _line_lead(s, inner[ii][1])
    if "\n" in s[ivs:ive] or "\n" in s[vs:inner[0][1]]:
        new = _dump(value, unit, lead, nl)
    else:
        new = json.dumps(value, ensure_ascii=False)
    return s[:ivs] + new + s[ive:]


def remove_member(s: str, path: tuple[str, str], *, drop_empty_outer: bool) -> str:
    outer, name = path
    top = top_object(s)
    ms, _ = members(s, top)
    oi = _find(ms, outer)
    if oi is None:
        return s
    vs = ms[oi][2]
    if s[vs] != "{":
        return s
    inner, _ = members(s, vs)
    ii = _find(inner, name)
    if ii is None:
        return s
    s = _remove(s, vs, ii)
    if drop_empty_outer:
        ms, _ = members(s, top)
        oi = _find(ms, outer)
        inner, _ = members(s, ms[oi][2])
        if not inner:
            s = _remove(s, top, oi)
    return s
