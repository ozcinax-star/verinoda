"""DESIGN.md section 4: delivered text -> SHOWN and CITED {file: set(lines)} views -> ContextBench predictions."""
from __future__ import annotations

import re

PATH = r"[\w.@+$-]+(?:/[\w.@+$-]+)*\.[A-Za-z0-9_+]+"
LOC = re.compile(r"(?<![\w/.@+$-])(" + PATH + r"):L?(\d+)(?:\s?[-–]\s?L?(\d+))?")
MAX_RANGE = 5000
V_HEADER = re.compile(r"^## (" + PATH + r"):L?(\d+)")
V_SUB = re.compile(r"^\s+(" + PATH + r"):L?\d+(?:[-–]L?\d+)?\s*$")
V_NUM = re.compile(r"^(\d+)(?: |$)")
G_NODE = re.compile(r"^NODE .*?\[src=(.*?) loc=(L?(\d+)(?:-L?(\d+))?)?(?: |\])")
G_EDGE_AT = re.compile(r" at=(.+?):L(\d+)(?:-L?(\d+))?\s*$")


def _norm(p: str) -> str:
    p = p.strip().strip("'\"`").replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def _rng(a: str, b: str | None) -> range:
    a_ = int(a)
    b_ = int(b) if b else a_
    if b_ < a_ or b_ - a_ > MAX_RANGE:
        b_ = a_
    return range(a_, b_ + 1)


def _add(v: dict, f: str, lines=()) -> None:
    f = _norm(f)
    if f:
        v.setdefault(f, set()).update(lines)


def locations(text: str) -> dict:
    v: dict = {}
    for m in LOC.finditer(text):
        _add(v, m.group(1), _rng(m.group(2), m.group(3)))
    return v


def verinoda_shown(text: str) -> dict:
    v: dict = {}
    cur = None
    for line in text.splitlines():
        h = V_HEADER.match(line)
        if h:
            cur = _norm(h.group(1))
            continue
        s = V_SUB.match(line)
        if s:
            cur = _norm(s.group(1))
            continue
        n = V_NUM.match(line)
        if n:
            if cur:
                _add(v, cur, [int(n.group(1))])
            continue
        if line and not line[0].isspace():
            cur = None
    return v


def graphify_shown(text: str) -> dict:
    v: dict = {}
    for line in text.splitlines():
        m = G_NODE.match(line)
        if not m or not m.group(1).strip():
            continue
        if m.group(3):
            _add(v, m.group(1), _rng(m.group(3), m.group(4)))
        else:
            _add(v, m.group(1))
    return v


def graphify_edge_locs(text: str) -> dict:
    v: dict = {}
    for line in text.splitlines():
        if line.startswith("EDGE "):
            m = G_EDGE_AT.search(line)
            if m:
                _add(v, m.group(1), _rng(m.group(2), m.group(3)))
    return v


def merge(*vs: dict) -> dict:
    out: dict = {}
    for v in vs:
        for f, ls in v.items():
            out.setdefault(f, set()).update(ls)
    return out


def views(arm: str, text: str) -> dict:
    if arm in ("vq", "va"):
        shown = verinoda_shown(text)
        return {"shown": shown, "cited": merge(shown, locations(text))}
    if arm == "gq":
        shown = graphify_shown(text)
        return {"shown": shown, "cited": merge(shown, graphify_edge_locs(text))}
    if arm == "bm25":
        shown = verinoda_shown(text)  # same '## PATH:A-B' + numbered-line format
        return {"shown": shown, "cited": shown}
    raise ValueError(arm)


def runs(lines: set) -> list[dict]:
    out = []
    for n in sorted(lines):
        if out and n == out[-1]["end"] + 1:
            out[-1]["end"] = n
        else:
            out.append({"start": n, "end": n})
    return out


def to_traj(view: dict) -> dict:
    files = sorted(view)
    spans = {f: runs(ls) for f, ls in sorted(view.items()) if ls}
    return {"pred_files": files, "pred_spans": spans}
