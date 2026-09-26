"""Folders that are a copy of the project's own code.

A benchmark corpus holding an older version of the project, a vendored snapshot kept for
comparison: the same files, defining the same names, under another folder. Left alone they compete
with the real code in every search and crowd the graph.

A folder is taken for a copy when

1. most of its code files have a *twin* outside it: a file of the same name that defines mostly
   the same names;
2. the rest of the project hardly uses it: the real code is imported and called from outside its
   folder, a copy only from inside;
3. most of those twins sit in a part of the project that *is* used from outside its own top
   folder (the real code: ``verinoda/``, used by ``tests/``). This decides which side is the copy.
   Two sides that nothing else uses (``tests/`` and a copy of it, a port and its original with
   no code around them) decide nothing: they are left alone, and ``verinoda setup --reference``
   marks such a tree by hand.

Detected copies are written to ``copies.json`` beside the index at every scan and update, and
ranked like the trees of ``verinoda setup --reference``: lower, unless the question names them. The
narrowest folder that holds the copy is reported, so naming it (``heldout``) lifts it.
``index.detect_copies: false`` in ``.verinoda/config.json`` turns this off; a path listed in
``index.not_copies`` is never taken for one.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path, PurePosixPath

MIN_FILES = 8           # fewer code files than this are not judged
MIN_COPY_SHARE = 0.6    # of a folder's code files that must have a twin outside it
MAX_OUTSIDE_USE = 0.1   # of the links into a folder that may come from outside it
MIN_USED_TWINS = 0.5    # of the twins that must sit in a part of the project used from outside
MIN_NAMES = 2           # a file must define at least this many names to be compared
MIN_SIMILARITY = 0.5    # Jaccard of the names two files define
MAX_TWIN_GROUP = 64     # a file name more common than this (index.ts in a monorepo) is not compared
NARROWER = 0.95         # a sub-folder holding this share of a folder's copies is reported instead
USE_RELATIONS = frozenset({"imports", "imports_from", "calls", "references", "uses", "inherits", "implements"})
FILE_NAME = "copies.json"
VERSION = 2


def _names(g, f: str) -> frozenset[str]:
    out = set()
    for n in g.symbols_in(f):
        label = str(g.label(n) or "").strip().lstrip(".").removesuffix("()")
        if label:
            out.add(label.rsplit(".", 1)[-1].lower())
    return frozenset(out)


def _ancestors(f: str) -> list[str]:
    parts = f.split("/")[:-1]
    return ["/".join(parts[:i]) + "/" for i in range(1, len(parts) + 1)]


def _top(f: str) -> str:
    return f.split("/", 1)[0] + "/" if "/" in f else ""


def _config(repo: Path) -> dict:
    from verinoda.paths import load_config

    try:
        return load_config(Path(repo)).get("index") or {}
    except Exception:  # noqa: BLE001 - an unreadable config: detection on, nothing excluded
        return {}


def detect(g, configured: tuple[str, ...] = ()) -> list[dict]:
    """The copy folders of graph ``g``: ``[{"path": "a/b/", "files", "copies", "outside_use", "of"}]``.
    ``configured``: reference trees already set by hand (never reported again)."""
    configured = tuple(c.strip("/").lower() + "/" for c in configured if c and c.strip("/"))
    files = sorted({f for _n, d in g.G.nodes(data=True) if (f := d.get("source_file"))})
    names = {}
    for f in files:
        s = _names(g, f)
        if len(s) >= MIN_NAMES:
            names[f] = s
    by_base: dict[str, list[str]] = defaultdict(list)
    for f in names:
        by_base[PurePosixPath(f).name.lower()].append(f)

    twins: dict[str, list[str]] = defaultdict(list)  # a file -> the same-named files defining the same names
    for group in by_base.values():
        if len(group) < 2 or len(group) > MAX_TWIN_GROUP:
            continue
        for i, f in enumerate(group):
            for other in group[i + 1:]:
                if f.rsplit("/", 1)[0] == other.rsplit("/", 1)[0]:
                    continue
                a, b = names[f], names[other]
                if len(a & b) / len(a | b) >= MIN_SIMILARITY:
                    twins[f].append(other)
                    twins[other].append(f)
    if not twins:
        return []

    # use: links into a folder, and how many come from outside it (for every folder that matters)
    folders = {d for f in twins for d in _ancestors(f)} | {_top(t) for ts in twins.values() for t in ts}
    into: dict[str, int] = defaultdict(int)
    outside: dict[str, int] = defaultdict(int)
    for u, v, data in g.G.edges(data=True):
        if data.get("relation") not in USE_RELATIONS:
            continue
        fu, fv = g.file(u), g.file(v)
        if not fu or not fv or fu == fv:
            continue
        for d in _ancestors(fv):
            if d in folders:
                into[d] += 1
                if not fu.startswith(d):
                    outside[d] += 1

    def used(top: str) -> bool:  # the project root's own files are the project
        return top == "" or outside[top] > MAX_OUTSIDE_USE * into[top]

    total: dict[str, int] = defaultdict(int)
    copies: dict[str, int] = defaultdict(int)
    used_twins: dict[str, int] = defaultdict(int)
    for f in names:
        for d in _ancestors(f):
            total[d] += 1
            away = [t for t in twins.get(f, ()) if not t.startswith(d) and not t.lower().startswith(configured)]
            if away:
                copies[d] += 1
                used_twins[d] += any(used(_top(t)) for t in away)
    passing = {d for d in copies
               if total[d] >= MIN_FILES and copies[d] >= MIN_COPY_SHARE * total[d]
               and outside[d] <= MAX_OUTSIDE_USE * into[d]
               and used_twins[d] >= MIN_USED_TWINS * copies[d]
               and not d.lower().startswith(configured) and not any(c.startswith(d.lower()) for c in configured)}
    # the narrowest folder that holds the copy (benchmarks/corpora/x/, not benchmarks/), unless a
    # wider one adds copies of its own (reference/ over reference/a/ when reference/b/ is one too)
    narrow = {d for d in passing
              if not any(c != d and c.startswith(d) and copies[c] >= NARROWER * copies[d] for c in passing)}
    chosen = sorted(d for d in narrow if not any(a in narrow for a in _ancestors(d.rstrip("/") + "/x")[:-1]))
    out = []
    for d in chosen:
        of: dict[str, int] = defaultdict(int)
        for f in names:
            if f.startswith(d):
                for t in twins.get(f, ()):
                    if not t.startswith(d):
                        of[_top(t) or "./"] += 1
                        break
        out.append({"path": d, "files": total[d], "copies": copies[d],
                    "outside_use": round(outside[d] / into[d], 3) if into[d] else 0.0,
                    "of": sorted(of, key=lambda k: (-of[k], k))[:3]})
    return out


def _path(repo: Path) -> Path:
    from verinoda.paths import graph_path

    return graph_path(Path(repo)).parent / FILE_NAME


def update(repo: Path, g) -> dict:
    """Detect the copies of ``g`` and write them beside the index (a scan / update step)."""
    cfg = _config(repo)
    configured = tuple((e.get("path") if isinstance(e, dict) else e) or "" for e in cfg.get("reference") or [])
    found = [] if cfg.get("detect_copies") is False else detect(g, tuple(c for c in configured if isinstance(c, str)))
    p = _path(repo)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps({"version": VERSION, "copies": found}, indent=2) + "\n", encoding="utf-8")
    tmp.replace(p)
    return {"copies": [c["path"] for c in load(repo)]}  # what is ranked lower, after index.not_copies


def roots_not_named(repo: Path, words: set[str], text: str = "") -> tuple[str, ...]:
    """Path prefixes of the configured reference trees (``index.reference``) and of the detected
    copies that neither ``words`` (folded question words) nor ``text`` name: they count less in a
    search and give way to the project's own code when a name is resolved.

    An alias counts as a word or a word's stem ("orijinalde" names "orijinal"); a folder name only
    as the whole word (so "super" does not name "mymod-original")."""
    import re

    from verinoda import textnorm
    from verinoda.paths import load_config

    try:
        entries = list((load_config(Path(repo)).get("index") or {}).get("reference") or [])
    except Exception:  # noqa: BLE001 - an unreadable config only means no reference trees
        return ()
    try:
        entries += [c["path"] for c in load(Path(repo))]
    except Exception:  # noqa: BLE001 - no detection result: the configured trees only
        pass
    text = textnorm.fold_tr(text or "")
    roots = []
    for e in entries:
        path = e.get("path") if isinstance(e, dict) else e
        if not isinstance(path, str) or not path.strip("/"):
            continue
        aliases = [textnorm.fold_tr(s) for s in (e.get("aliases") or [])] if isinstance(e, dict) else []
        segments = [textnorm.fold_tr(seg) for seg in path.strip("/").split("/") if len(seg) >= 3]
        named = any(a in words or (len(a) >= 5 and any(w.startswith(a) for w in words)) for a in aliases) \
            or any(s in words or re.search(r"(?<![\w-])" + re.escape(s) + r"(?![\w-])", text) for s in segments)
        if not named:
            roots.append(path.strip("/") + "/")
    return tuple(roots)


def load(repo: Path) -> list[dict]:
    """The detected copies to rank lower: none when detection is off or a path is excluded."""
    cfg = _config(repo)
    if cfg.get("detect_copies") is False:
        return []
    keep_out = {str(p).strip("/").lower() + "/" for p in cfg.get("not_copies") or [] if isinstance(p, str)}
    try:
        raw = json.loads(_path(repo).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [c for c in raw.get("copies") or []
            if isinstance(c, dict) and isinstance(c.get("path"), str)
            and not any(c["path"].lower().startswith(k) for k in keep_out)]
