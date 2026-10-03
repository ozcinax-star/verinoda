"""``verinoda locate`` and ``verinoda coupled``: the files a change probably touches besides the one found first.

A coding agent finds the first file of a bug by search and misses the OTHER files of a multi-file fix. Two tiers:

* ``likely``: the files issue-text retrieval ranks first (:func:`verinoda.retrieval.retrieve`, as ``query`` reads it).
* ``coupled``: files that change together with a set of anchor files (the likely files, files the caller names),
  read WITHOUT loading the graph, so it answers in a fraction of a second even on a large history:

  1. ``cochange``: the anchor's own last commits (a path-limited ``git log``, then the whole file lists of exactly
     those commits: a pathspec also limits what ``--name-only`` lists, so one command cannot do both). A busy
     repository's last 1000 commits are days; the anchor's last 300 are its own history. Commits over 30 files are
     left out; a file counts when it shares at least 2 commits and 15 % of the anchor's.
  2. ``neighbour``: Python relative imports and C-family includes, high precision only (an include that matches
     several files is skipped; reverse includes only inside the anchor's folder).
  3. ``pair``: the same stem with a partner extension (``.c``/``.h``, ``.cpp``/``.hpp``, ``.py``/``.pyi``).
  4. ``twin``: the same file name in other folders, when few files carry it (an architecture variant).

Everything degrades: a repository that is not git, a shallow clone, a git call that times out or fails each say so in
the result's ``history`` block and the other signals still answer. A coupled file is a lead (a pattern in the
history or a spelling in the code), never a proven dependency.
"""
from __future__ import annotations

import contextlib
import json
import os
import posixpath
import re
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, TextIO

COMMITS = 300            # the anchor's own commits read
WINDOW = 12000           # the walk of a long history stops this many commits back (about 3 s on 115,000 commits)
BULK = 30                # a commit changing more files than this (a reformat, a mass rename) is left out
MIN_SHARED = 2           # commits a file must share with the anchor
MIN_DEGREE = 0.15        # share of the anchor's commits that also changed the file
PER_ANCHOR_NEIGHBOURS = 4
PER_ANCHOR_PAIRS = 3
PER_ANCHOR_TWINS = 3
TWIN_MAX_CARRIERS = 8    # a file name carried by more files than this is not distinctive
MAX_ANCHORS = 12
SCAN_FILES = 400         # files read to find who includes / imports an anchor
SCAN_BYTES = 300_000
TEXT_CHARS = 3000        # the question text ranked
INVENTORY_CAP = 400_000
PACK_BYTES = 40_000_000  # packs larger than this: the length of the history is asked before the walk
KINDS = ("cochange", "neighbour", "pair", "twin")  # strongest evidence first
_SKIP_DIRS = {".git", ".verinoda", "node_modules", "__pycache__", ".venv", "venv", ".tox", ".mypy_cache",
              ".pytest_cache", "dist", "build"}
_GIT = ("git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-c", "core.quotepath=off")
# variables that point git at another repository (a caller running under `git bisect run` has them set)
_GIT_LOCATION_ENV = {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR",
                     "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_PREFIX", "GIT_NAMESPACE"}

_C_EXT = (".c", ".h", ".S", ".cpp", ".hpp", ".cc", ".hh", ".cxx", ".hxx")
_C_INCLUDE = re.compile(r'^[ \t]*#[ \t]*include[ \t]*[<"]([^>"\n]+)[>"]', re.MULTILINE)
_PY_FROM = re.compile(r"^[ \t]*from[ \t]+(\.+)([\w.]*)[ \t]+import[ \t]+(\([^)]*\)|[^\n]+)", re.MULTILINE)
_PARTNERS = {".c": (".h",), ".h": (".c", ".cpp", ".cc", ".S"), ".S": (".h",), ".cpp": (".hpp", ".h", ".hh"),
             ".cc": (".hh", ".h", ".hpp"), ".hpp": (".cpp", ".cc"), ".hh": (".cc", ".cpp"), ".cxx": (".hxx", ".h"),
             ".hxx": (".cxx",), ".py": (".pyi",), ".pyi": (".py",)}
_GENERIC_NAMES = {"__init__.py", "__main__.py", "setup.py", "conftest.py", "index.js", "index.ts", "main.py", "main.c",
                  "makefile", "cmakelists.txt", "readme.md", "license", "package.json", ".gitignore", "build.gradle",
                  "pom.xml", "mod.rs", "lib.rs", "main.rs", "main.go", "go.mod", "dockerfile", "kconfig"}
_DOC_EXT = (".md", ".rst", ".txt", ".adoc")


# -- paths -------------------------------------------------------------------------------------------------------

def _dir(p: str) -> str:
    return p.rsplit("/", 1)[0] if "/" in p else ""


def _base(p: str) -> str:
    return p.rsplit("/", 1)[-1]


def _stem_ext(p: str) -> tuple[str, str]:
    b = _base(p)
    stem, dot, ext = b.rpartition(".")
    return (stem, "." + ext) if dot and stem else (b, "")


def _common(a: str, b: str) -> int:
    """Leading path components two paths share."""
    n = 0
    for x, y in zip(a.split("/")[:-1], b.split("/")[:-1]):
        if x != y:
            break
        n += 1
    return n


def _is_test(p: str) -> bool:
    parts = p.lower().split("/")
    name = parts[-1]
    return (any(x in ("test", "tests", "testing", "__tests__", "spec", "specs", "testdata") for x in parts[:-1])
            or name.startswith(("test_", "tests_")) or re.search(r"[._-]tests?\.[a-z0-9]+$", name) is not None)


def _is_doc(p: str) -> bool:
    low = p.lower()
    return low.endswith(_DOC_EXT) or low.split("/")[0] in ("doc", "docs", "documentation")


# -- git ---------------------------------------------------------------------------------------------------------

def _env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in _GIT_LOCATION_ENV}


def _run(repo: Path, args: list[str], deadline: float, stdin: str | None = None) -> tuple[str | None, str]:
    """``(stdout, "")`` or ``(None, why)``: a git call that must end before ``deadline`` (a ``time.monotonic()``)."""
    left = deadline - time.monotonic()
    if left <= 0.05:
        return None, "timeout"
    try:
        p = subprocess.run([*_GIT, "-C", str(repo), *args], capture_output=True, timeout=left, env=_env(),
                           input=None if stdin is None else stdin.encode("utf-8"),
                           stdin=subprocess.DEVNULL if stdin is None else None, check=False)
    except subprocess.TimeoutExpired:
        return None, "timeout"
    except OSError:
        return None, "git not found"
    if p.returncode != 0:
        return None, "error"
    return p.stdout.decode("utf-8", errors="replace"), ""


class _Inventory:
    """The repository's tracked files (all files outside git), indexed by name, stem and folder."""

    def __init__(self, files: list[str]) -> None:
        self.files = files
        self.set = set(files)
        self.by_base: dict[str, list[str]] = {}
        self.by_stem: dict[str, list[str]] = {}
        self.by_dir: dict[str, list[str]] = {}
        for f in files:
            self.by_base.setdefault(_base(f), []).append(f)
            self.by_stem.setdefault(_stem_ext(f)[0], []).append(f)
            self.by_dir.setdefault(_dir(f), []).append(f)

    def under(self, prefix: str) -> list[str]:
        pre = prefix + "/" if prefix else ""
        return [f for f in self.files if f.startswith(pre)]


def _walk(repo: Path, deadline: float) -> list[str]:
    out: list[str] = []
    for root, dirs, names in os.walk(repo):
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS)
        rel = os.path.relpath(root, repo).replace("\\", "/")
        rel = "" if rel == "." else rel + "/"
        out.extend(rel + n for n in sorted(names))
        if len(out) > INVENTORY_CAP or time.monotonic() > deadline:
            break
    return out


def _inventory(repo: Path, deadline: float) -> _Inventory:
    out, _ = _run(repo, ["ls-files", "-z", "--cached"], deadline)
    if out is not None:
        files = [f for f in out.split("\0") if f and not f.startswith((".git/", ".verinoda/"))][:INVENTORY_CAP]
    else:
        files = _walk(repo, deadline + 2.0)
    return _Inventory(files)


def _repo_info(repo: Path, deadline: float) -> dict[str, Any]:
    """``{"git": bool, "shallow": bool, "prefix": str, "why": str}`` from one git call."""
    out, why = _run(repo, ["rev-parse", "--is-inside-work-tree", "--is-shallow-repository", "--show-prefix"], deadline)
    if out is None:
        return {"git": False, "shallow": False, "prefix": "", "why": why}
    lines = out.split("\n")
    return {"git": lines[0].strip() == "true", "shallow": len(lines) > 1 and lines[1].strip() == "true",
            "prefix": lines[2].strip() if len(lines) > 2 else "", "why": ""}


def _bound(repo: Path, window: int, deadline: float) -> str | None:
    """The commit ``window`` commits back from HEAD (the walk stops there), None when the history is not longer."""
    out, _ = _run(repo, ["rev-list", "-n1", f"--skip={window}", "HEAD"], deadline)
    return (out or "").strip() or None


def _history_pass(repo: Path, anchors: list[str], relative: bool, bound: str | None, deadline: float) -> dict[str, dict]:
    """``{anchor: row}`` for ``anchors``: the newest 300 commits of each (merges left out) and, for exactly those, the
    files each changed. Two commands, because a pathspec also limits what ``--name-only`` lists: the first walks the
    history limited to the anchors (its file lists name the anchors a commit touched), the second lists the whole
    file lists of the commits found. One walk serves every anchor."""
    rel = ["--relative"] if relative else []
    rng = ["HEAD", f"^{bound}"] if bound else ["HEAD"]
    # --full-history: the default simplification of a log limited to several paths drops commits that the same log
    # limited to one of them lists (a revert on a side branch), so the counts would depend on the other anchors
    out, why = _run(repo, ["log", "--no-merges", "--full-history", f"-n{COMMITS * len(anchors)}",
                           "--format=%x01%H", "--name-only", "--no-renames", "--no-ext-diff", "--no-textconv",
                           *rel, *rng, "--", *[":(literal)" + a for a in anchors]], deadline)
    if out is None:
        return {a: {"anchor": a, "error": why} for a in anchors}
    touched: dict[str, list[str]] = {a: [] for a in anchors}  # anchor -> its commits, newest first
    for blk in out.split("\x01"):
        lines = blk.split("\n")
        names = {x.strip() for x in lines[1:]}
        for a in anchors:
            if a in names and len(touched[a]) < COMMITS:
                touched[a].append(lines[0].strip())
    hashes = list(dict.fromkeys(h for hs in touched.values() for h in hs))
    files: dict[str, set[str]] = {}
    if hashes:
        out, why = _run(repo, ["log", "--no-walk=unsorted", "--no-merges", "--format=%x01%H", "--name-only",
                               "--no-renames", "--no-ext-diff", "--no-textconv", *rel, "--stdin"], deadline,
                        stdin="\n".join(hashes) + "\n")
        if out is None:
            return {a: {"anchor": a, "error": why} for a in anchors}
        for blk in out.split("\x01"):
            lines = blk.split("\n")
            if lines[0].strip():
                files[lines[0].strip()] = {x.strip() for x in lines[1:] if x.strip()}
    rows: dict[str, dict] = {}
    for a in anchors:
        kept = [files[h] for h in touched[a] if h in files and len(files[h]) <= BULK]
        shared: Counter = Counter()
        for c in kept:
            for f in c:
                if f != a:
                    shared[f] += 1
        rows[a] = {"anchor": a, "commits": len(touched[a]), "kept": len(kept), "shared": shared,
                   "windowed": bound is not None}
    return rows


# -- neighbours, pairs, twins ------------------------------------------------------------------------------------

def _read(repo: Path, rel: str) -> str:
    try:
        with open(repo / rel, "rb") as fh:
            return fh.read(SCAN_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _py_targets(path: str, text: str, inv: _Inventory) -> list[str]:
    """Files of the project a Python file imports relatively (``from .x import y``, ``from . import x``)."""
    out: list[str] = []
    for dots, mod, names in _PY_FROM.findall(text):
        base = _dir(path)
        for _ in range(len(dots) - 1):
            base = _dir(base)
        stems = [mod.replace(".", "/")] if mod else [
            re.split(r"\s+as\s+", n.strip())[0].strip() for n in re.sub(r"#[^\n]*", "", names).strip("() \n").split(",")
            if n.strip()]
        for s in stems:
            stem = posixpath.join(base, s) if base else s
            for cand in (stem + ".py", stem + "/__init__.py"):
                if cand in inv.set and cand != path and cand not in out:
                    out.append(cand)
                    break
    return out


def _resolve_include(path: str, inc: str, inv: _Inventory) -> str | None:
    """The one file ``#include inc`` names from ``path``: next to it, or the only file whose path ends with the
    include (an ambiguous match is skipped, not guessed)."""
    near = posixpath.normpath(posixpath.join(_dir(path), inc)) if _dir(path) else posixpath.normpath(inc)
    if near in inv.set:
        return near
    hits = [g for g in inv.by_base.get(_base(inc), ()) if g == inc or g.endswith("/" + inc)]
    return hits[0] if len(hits) == 1 else None


def _c_targets(path: str, text: str, inv: _Inventory) -> list[str]:
    out: list[str] = []
    for inc in _C_INCLUDE.findall(text):
        g = _resolve_include(path, inc.strip(), inv)
        if g and g != path and g not in out:
            out.append(g)
    return out


def _neighbours(repo: Path, anchor: str, inv: _Inventory, deadline: float) -> list[tuple[str, str]]:
    """``[(file, "forward"|"reverse")]``, at most :data:`PER_ANCHOR_NEIGHBOURS`: what the anchor includes or imports
    (nearest first) and what includes or imports it (inside the anchor's own folder subtree for C, its folder for
    Python)."""
    ext = _stem_ext(anchor)[1]
    if ext == ".py":
        targets, siblings = _py_targets, [f for f in inv.by_dir.get(_dir(anchor), ()) if f.endswith(".py")]
    elif ext in _C_EXT:
        targets, siblings = _c_targets, [f for f in inv.under(_dir(anchor)) if f.endswith(_C_EXT)]
    else:
        return []
    forward = sorted(targets(anchor, _read(repo, anchor), inv), key=lambda g: -_common(anchor, g))
    reverse: list[str] = []
    for g in sorted((f for f in siblings if f != anchor), key=lambda f: (-_common(anchor, f), f))[:SCAN_FILES]:
        if time.monotonic() > deadline:
            break
        if anchor in targets(g, _read(repo, g), inv):
            reverse.append(g)
    if ext == ".py" and len(reverse) > 12:  # a module every file of a large folder imports says nothing
        reverse = []
    rev, fwd = reverse[:2], forward[:2]
    rest = [g for g in (*reverse[2:], *forward[2:]) if g not in rev and g not in fwd]
    chosen = [(g, "reverse") for g in rev] + [(g, "forward") for g in fwd]
    chosen += [(g, "reverse" if g in reverse else "forward") for g in rest]
    return chosen[:PER_ANCHOR_NEIGHBOURS]


def _pairs(anchor: str, inv: _Inventory) -> list[str]:
    stem, ext = _stem_ext(anchor)
    want = _PARTNERS.get(ext)
    if not want:
        return []
    test = _is_test(anchor)
    cands = [g for g in inv.by_stem.get(stem, ()) if g != anchor and _stem_ext(g)[1] in want
             and (test or not _is_test(g))]
    near = [g for g in cands if _dir(g) == _dir(anchor)]
    if len(cands) > TWIN_MAX_CARRIERS:  # a stem like "types": only the one in the anchor's own folder is a partner
        cands = near
    return sorted(cands, key=lambda g: (-_common(anchor, g), g))[:PER_ANCHOR_PAIRS]


def _twins(anchor: str, inv: _Inventory) -> list[str]:
    base = _base(anchor)
    carriers = inv.by_base.get(base, ())
    if base.lower() in _GENERIC_NAMES or len(carriers) > TWIN_MAX_CARRIERS:
        return []
    test = _is_test(anchor)
    cands = [g for g in carriers if g != anchor and (test or not _is_test(g))]
    return sorted(cands, key=lambda g: (-_common(anchor, g), g))[:PER_ANCHOR_TWINS]


# -- coupled -----------------------------------------------------------------------------------------------------

def _normalise(repo: Path, anchors) -> list[str]:
    out: list[str] = []
    for a in anchors:
        s = str(a).strip().replace("\\", "/")
        if not s:
            continue
        if os.path.isabs(s):
            try:
                s = Path(s).resolve().relative_to(repo.resolve()).as_posix()
            except (ValueError, OSError):
                pass
        s = posixpath.normpath(s) if not s.startswith("../") else s
        s = s.removeprefix("./")
        if s not in out:
            out.append(s)
    return out


def _inside(a: str) -> bool:
    """A path that stays in the repository: not absolute, not climbing out of it."""
    return not (a.startswith(("/", "../")) or a == ".." or re.match(r"[A-Za-z]:", a) or os.path.isabs(a))


def _phrase(r: dict) -> str:
    a = r["anchor"]
    if r["kind"] == "cochange":
        return f"changed together in {r['shared']} of {r['commits']} commits with {a}"
    if r["kind"] == "neighbour":
        return {"included": f"included by {a}", "includes": f"includes {a}", "imported": f"imported by {a}",
                "imports": f"imports {a}"}[r["how"]]
    return f"{r['kind']} of {a}"


def _gitdir(repo: Path) -> Path | None:
    """The ``.git`` folder of the work tree ``repo`` is in (None: not found, or a worktree's ``.git`` file)."""
    for p in (repo, *list(repo.parents)[:8]):
        if (p / ".git").is_dir():
            return p / ".git"
        if (p / ".git").exists():
            return None
    return None


def _head(gitdir: Path | None) -> str | None:
    """The commit HEAD names, read from the files (no process): None when it cannot be told."""
    if gitdir is None:
        return None
    try:
        ref = (gitdir / "HEAD").read_text(encoding="ascii", errors="replace").strip()
        if not ref.startswith("ref: "):
            return ref or None
        name = ref[5:].strip()
        try:
            return (gitdir / name).read_text(encoding="ascii", errors="replace").strip() or None
        except OSError:
            for line in (gitdir / "packed-refs").read_text(encoding="utf-8", errors="replace").splitlines():
                if line.endswith(" " + name):
                    return line.split()[0]
    except OSError:
        pass
    return None


def _stat(p: Path) -> tuple | None:
    try:
        st = p.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _memo(cache: dict | None, head: str | None) -> dict | None:
    """The part of ``cache`` valid at this commit (a new commit empties it)."""
    if cache is None or not head:
        return None
    if cache.get("head") != head:
        cache.clear()
        cache.update({"head": head, "hist": {}})
    return cache


def _long_history(gitdir: Path | None) -> bool:
    """Could the history be long enough that a full walk is slow? Judged by the size of the packs (a repository
    of a few thousand commits is a few tens of megabytes), so that a small one pays for no extra call."""
    if gitdir is None:
        return True
    try:
        return sum(f.stat().st_size for f in (gitdir / "objects" / "pack").glob("*.pack")) > PACK_BYTES
    except OSError:
        return True


def _history_rows(mem: dict | None, repo: Path, anchors: list[str], relative: bool, window: int,
                  deadline: float, long: bool = True) -> list[dict]:
    """The history row of each anchor: from ``mem`` when the commit is the same, else read in one walk."""
    have = {a: mem["hist"][(a, relative)] for a in anchors if mem is not None and (a, relative) in mem["hist"]}
    todo = [a for a in anchors if a not in have]
    if todo:
        new = _history_pass(repo, todo, relative, _bound(repo, window, deadline) if long else None, deadline)
        for a, row in new.items():
            if mem is not None and "error" not in row:
                mem["hist"][(a, relative)] = row
        have.update(new)
    return [have[a] for a in anchors]


def _why(relations: list[dict]) -> str:
    """The strongest relation in words, the other kinds by name."""
    kinds = list(dict.fromkeys(r["kind"] for r in relations[1:] if r["kind"] != relations[0]["kind"]))
    return _phrase(relations[0]) + (f" (also {', '.join(kinds)})" if kinds else "")


def _code_signals(repo: Path, present: list[str], inv: _Inventory, deadline: float) -> dict[str, dict]:
    """Candidate file -> ``{"relations": [...]}`` from the code alone: neighbours, pairs, twins."""
    rows: dict[str, dict] = {}

    def add(g: str, rel: dict) -> None:
        rows.setdefault(g, {"relations": []})["relations"].append(rel)

    for a in present:
        py = _stem_ext(a)[1] == ".py"
        for g, how in _neighbours(repo, a, inv, deadline):
            fwd = how == "forward"
            kind = ("imported" if fwd else "imports") if py else ("included" if fwd else "includes")
            add(g, {"kind": "neighbour", "anchor": a, "how": kind})
        for g in _pairs(a, inv):
            add(g, {"kind": "pair", "anchor": a})
        for g in _twins(a, inv):
            add(g, {"kind": "twin", "anchor": a})
    return rows


def _git_state(ex: ThreadPoolExecutor, repo: Path, history: bool, gitdir: Path | None, head: str | None,
               mem: dict | None, given: list[str], deadline: float):
    """``(inventory, future of the history rows or None, reason)``. What git is asked (the repository's facts, its
    files, the walk) is asked at once; ``reason`` says why there is no walk."""
    f_info = ex.submit(_repo_info, repo, deadline)
    inv_key = (head, _stat(gitdir / "index")) if gitdir and head else None
    reuse = mem is not None and inv_key is not None and mem.get("inv_key") == inv_key
    f_inv = None if reuse else ex.submit(_inventory, repo, deadline)
    # before the repository is known to be git: a wasted walk is cheaper than waiting for the answer
    f_hist = None
    if history and gitdir is not None:
        f_hist = ex.submit(_history_rows, mem, repo, given, gitdir.parent != repo, WINDOW, deadline,
                           _long_history(gitdir))
    info = f_info.result()
    reason = ""
    if not history:
        reason = "disabled (--no-history)"
    elif not info["git"]:
        reason = "history unavailable: " + ("git not found" if info["why"] == "git not found" else "not a git repository")
    elif info["shallow"]:
        reason = "history unavailable: shallow clone"
    elif f_hist is None:  # a linked worktree's .git is a file: the prefix is known only now
        f_hist = ex.submit(_history_rows, mem, repo, given, bool(info["prefix"]), WINDOW, deadline)
    if f_inv is None:
        inv = mem["inv"]
    else:
        inv = f_inv.result()
        if mem is not None and inv_key:
            mem["inv_key"], mem["inv"] = inv_key, inv
    return inv, (None if reason else f_hist), reason


def _rank(rows: dict[str, dict], anchor_set: set[str], first: set[str]) -> list[tuple]:
    """``(file, row, cochange degree, kinds)``, best first: files related to a ``first`` anchor, then by co-change
    degree, then neighbour, pair, twin."""
    ranked = []
    for f, row in rows.items():
        if f in anchor_set:
            continue
        kinds = {r["kind"] for r in row["relations"]}
        degree = sum(r["degree"] for r in row["relations"] if r["kind"] == "cochange")
        row["relations"].sort(key=lambda r: (KINDS.index(r["kind"]), r["anchor"] not in first, -r.get("degree", 0)))
        ranked.append((f, row, degree, kinds))
    ranked.sort(key=lambda x: (not any(r["anchor"] in first for r in x[1]["relations"]), -x[2],
                               "neighbour" not in x[3], "pair" not in x[3], "twin" not in x[3],
                               -len(x[1]["relations"]), x[0]))
    return ranked


def coupled(repo: Path | str, anchors, *, max_files: int = 8, history: bool = True,
            time_budget_s: float = 6.0, cache: dict | None = None, priority=()) -> dict[str, Any]:
    """Files likely to change together with ``anchors`` (repository-relative paths), without the graph.

    ``{"anchors", "files": [{"path", "tier": "coupled", "why", "score", "relations"}], "history": {"read",
    "commits", "anchors_read", "reason"?, "window"?}, "missing_anchors", "truncated", "seconds"}``. ``score`` is the
    co-change degree (the anchor's commits that also changed the file, summed over anchors), null for a file listed
    by code alone. The ``history`` block says what could not be read (not a git work tree, a shallow clone, a git
    call that ended at ``time_budget_s``) and, for a history longer than :data:`WINDOW` commits, that only the last
    ones were walked; it never raises for that. ``cache``: a dict a long-lived caller (the worker) passes again, so
    what was read at one commit is not read twice. ``priority``: anchors the caller trusts more (named by a person,
    not ranked by a program): files related to them are listed first."""
    t0 = time.monotonic()
    deadline = t0 + max(0.5, float(time_budget_s))
    repo = Path(repo).resolve()
    named = _normalise(repo, anchors)
    outside = [a for a in named if not _inside(a)]  # never read: it is not a file of this repository
    named = [a for a in named if _inside(a)]
    ignored = named[MAX_ANCHORS:] + outside
    given = named[:MAX_ANCHORS]
    hist: dict[str, Any] = {"read": False, "commits": 0, "anchors_read": 0}
    gitdir = _gitdir(repo)
    head = _head(gitdir)
    with ThreadPoolExecutor(max_workers=3) as ex:
        inv, f_hist, reason = _git_state(ex, repo, history, gitdir, head, _memo(cache, head), given, deadline)
        present = [a for a in given if a in inv.set or (repo / a).is_file()]
        rows = _code_signals(repo, present, inv, deadline)  # while the walk runs
        results = f_hist.result() if f_hist is not None else None
    missing = [a for a in given if a not in present] + ignored
    if reason:
        hist["reason"] = reason
    if results is not None:
        results = [r for r in results if r["anchor"] in present]
        done = [r for r in results if "error" not in r]
        hist.update({"read": bool(done), "anchors_read": len(done), "commits": sum(r["commits"] for r in done)})
        if any(r.get("windowed") for r in done):
            hist["window"] = WINDOW
        if len(done) < len(results):
            timed_out = any(r.get("error") == "timeout" for r in results)
            hist["reason"] = ("history not read: a git call ended at the time budget " f"({time_budget_s:g} s)"
                              if timed_out else "history not read: git failed")
        for r in done:
            if r["kept"] < MIN_SHARED:
                continue
            for f, n in r["shared"].items():
                if n >= MIN_SHARED and n / r["kept"] >= MIN_DEGREE and f not in given and (repo / f).is_file():
                    rows.setdefault(f, {"relations": []})["relations"].append(
                        {"kind": "cochange", "anchor": r["anchor"], "shared": n, "commits": r["kept"],
                         "degree": round(n / r["kept"], 3)})
    ranked = _rank(rows, set(given), set(_normalise(repo, priority)))
    cap = max(0, int(max_files))
    files = [{"path": f, "tier": "coupled", "why": _why(row["relations"]),
              "score": round(degree, 3) if degree else None, "relations": row["relations"]}
             for f, row, degree, _k in ranked[:cap]]
    return {"anchors": present, "files": files, "history": hist, "missing_anchors": missing,
            "truncated": len(ranked) > cap, "seconds": round(time.monotonic() - t0, 3)}


# -- locate ------------------------------------------------------------------------------------------------------

class GraphKeeper:
    """The graph loaded once and again only when ``graph.json`` or the receiver-call sidecar changes on disk."""

    def __init__(self, repo: Path | str) -> None:
        self.repo = Path(repo).resolve()
        self._key: tuple | None = None
        self._graph = None

    def _stat(self) -> tuple:
        from verinoda.paths import graph_path, receiver_calls_path

        out = []
        for p in (graph_path(self.repo), receiver_calls_path(self.repo)):
            try:
                st = p.stat()
                out.append((st.st_mtime_ns, st.st_size))
            except OSError:
                out.append(None)
        return tuple(out)

    def __call__(self):
        from verinoda import index

        key = self._stat()
        if self._graph is None or key != self._key:
            self._graph = index.load(self.repo)
            self._key = key
        return self._graph


def _matches(item: dict, words: dict[str, str]) -> list[str]:
    """The question words an item matched (the index's stems are shown as the first word of the question that
    starts with them)."""
    seen: list[str] = []
    for w in item.get("why") or []:
        if w.startswith(("name:", "text:")):
            for t in re.findall(r"'([^']+)'", w.split(" at ")[0]):
                t = words.get(t.lower(), t)
                if t not in seen:
                    seen.append(t)
    return seen[:4]


def _question_words(text: str) -> dict[str, str]:
    """Stem -> a word of the question it starts: the index matches stems ("schedul"), a reader wants a word."""
    out: dict[str, str] = {}
    for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", text):
        for n in range(len(w), 2, -1):
            out.setdefault(w[:n].lower(), w)
    return out


def _symbol(item: dict, path: str) -> str | None:
    sym = (item.get("symbol") or "").removesuffix("()")
    return None if (not sym or "module level" in sym or sym == _base(path)) else sym


def _named(text: str, path: str) -> bool:
    low = text.lower()
    return _base(path).lower() in low or (len(_stem_ext(path)[0]) > 3 and _stem_ext(path)[0].lower() in low)


def _likely(g, repo: Path, text: str, n: int) -> list[dict[str, Any]]:
    from verinoda import query_filters, retrieval

    q = text[:TEXT_CHARS]
    budget = retrieval.Budget(max_items=30, max_chars=24000)
    try:
        res = retrieval.retrieve(g, q, budget, filters=True)
    except query_filters.FilterError:  # prose with an upper-case OR or NOT is not a filter expression
        res = retrieval.retrieve(g, q, retrieval.Budget(max_items=30, max_chars=24000))
    words = _question_words(q)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for it in res.get("items") or []:
        f = it.get("file")
        if not f or f in seen:
            continue
        if (_is_test(f) or _is_doc(f)) and not _named(text, f):
            continue
        seen.add(f)
        m = _matches(it, words)
        out.append({"path": f, "tier": "likely", "why": ("matches: " + ", ".join(m)) if m else "ranked by the question",
                    "score": round(float(it.get("score") or 0.0), 3), "relations": [],
                    "lines": list(it.get("span") or it.get("lines") or []) or None,
                    "symbol": _symbol(it, f)})
        if len(out) >= n:
            break
    return out


def locate(g_or_loader, repo: Path | str, text: str, *, anchors=(), max_files: int = 8, max_chars: int = 1800,
           history: bool = True, time_budget_s: float = 6.0, likely_files: int | None = None,
           fresh: dict | None = None, cache: dict | None = None) -> dict[str, Any]:
    """The files an issue text probably touches: ``likely`` (retrieval) then ``coupled`` (change together with the
    likely files and ``anchors``). ``g_or_loader``: a graph, or a callable returning one (:class:`GraphKeeper`).
    The result renders to at most ``max_chars`` characters (:func:`render`); what was cut sets ``truncated``."""
    from verinoda import freshness

    t0 = time.monotonic()
    repo = Path(repo).resolve()
    text = (text or "").strip()
    if not text:
        raise ValueError("locate needs the issue text")
    cap = max(1, int(max_files))
    pool = ThreadPoolExecutor(max_workers=1)  # the index's freshness is read while the question is ranked
    f_fresh = None if fresh is not None else pool.submit(freshness.check, repo)
    g = g_or_loader() if callable(g_or_loader) else g_or_loader
    n_likely = min(cap, likely_files if likely_files is not None else max(2, min(5, cap // 3 + 1)))
    likely = _likely(g, repo, text, n_likely)
    anchored = list(dict.fromkeys([*[x["path"] for x in likely], *_normalise(repo, anchors)]))
    cp = coupled(repo, anchored, max_files=cap, history=history, time_budget_s=time_budget_s, cache=cache,
                 priority=anchors)
    shown = {x["path"] for x in likely}
    rest = [x for x in cp["files"] if x["path"] not in shown]
    files = likely + rest[: max(0, cap - len(likely))]
    fr = fresh if f_fresh is None else f_fresh.result()
    pool.shutdown(wait=False)
    res: dict[str, Any] = {"question": text, "files": files, "anchors": cp["anchors"], "history": cp["history"],
                           "freshness": freshness.summary(fr) or {"checked": bool(fr.get("checked")), "stale_count": 0},
                           "truncated": cp["truncated"] or len(rest) > len(files) - len(likely)}
    if cp["missing_anchors"]:
        res["missing_anchors"] = cp["missing_anchors"]
    fit(res, max_chars, kind="locate")
    res["seconds"] = round(time.monotonic() - t0, 3)
    return res


# -- text --------------------------------------------------------------------------------------------------------

def _history_note(h: dict) -> str:
    if h.get("read"):
        note = f"history: {h['commits']:,} commits read"
        note += f"; last {h['window']:,} commits only" if h.get("window") else ""
        return note + (f"; {h['reason']}" if h.get("reason") else "")
    return h.get("reason") or "history not read"


def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 3] + "..."


def render(res: dict, max_chars: int = 1800, *, kind: str = "locate") -> str:
    """The plain text a model reads: compact, at most ``max_chars`` (files are dropped from the end to fit; the
    header and the notes stay, cut to fit as well)."""
    files = res["files"]
    head = f"verinoda {kind}: {len(files)} file{'' if len(files) == 1 else 's'} ({_history_note(res['history'])})"
    fr = res.get("freshness") or {}
    notes = []
    if fr.get("stale_count"):
        notes.append(f"{fr['stale_count']} file(s) changed since the index (run `verinoda update`)")
    if res.get("missing_anchors"):
        notes.append("not in the repository: " + ", ".join(res["missing_anchors"][:4]))
    lines = [head, *("note: " + n for n in notes)]
    likely = [f for f in files if f["tier"] == "likely"]
    other = [f for f in files if f["tier"] != "likely"]
    if likely:
        lines.append("likely")
        for f in likely:
            where = f["path"] + (f":{f['lines'][0]}-{f['lines'][1]}" if f.get("lines") and len(f["lines"]) == 2 else "")
            lines.append(_clip(f"  {where}{' ' + f['symbol'] if f.get('symbol') else ''}  - {f['why']}", 200))
    if other:
        lines.append("also check (change together with the above)" if likely else
                     "change together with " + _clip(", ".join(res.get("anchors") or []), 120))
        for f in other:
            lines.append(_clip(f"  {f['path']}  - {f['why']}", 200))
    text = "\n".join(lines)
    return text if len(text) <= max_chars else _clip(text, max_chars)


def fit(res: dict, max_chars: int, *, kind: str = "locate") -> dict:
    """Drop files from the end of ``res["files"]`` (coupled first) until :func:`render` fits ``max_chars``."""
    while res["files"] and len(render(res, 10**9, kind=kind)) > max_chars:
        res["files"].pop()
        res["truncated"] = True
    return res


# -- the worker --------------------------------------------------------------------------------------------------

def handle(request: Any, repo: Path, keeper: Callable, cache: dict | None = None) -> dict[str, Any]:
    """One worker request -> its response line: ``{"id", "ok": true, "result"}`` or ``{"id", "ok": false, "error"}``."""
    rid = request.get("id") if isinstance(request, dict) else None
    try:
        if not isinstance(request, dict):
            raise TypeError("a request is a JSON object")
        op = request.get("op")
        if op == "locate":
            res = locate(keeper, repo, str(request.get("text") or ""), anchors=request.get("anchors") or (),
                         max_files=int(request.get("max_files") or 8), max_chars=int(request.get("max_chars") or 1800),
                         history=bool(request.get("history", True)), cache=cache)
        elif op == "coupled":
            files = request.get("files") or request.get("anchors") or []
            if not isinstance(files, list) or not files:
                raise ValueError("coupled needs a non-empty 'files' list")
            res = coupled(repo, files, max_files=int(request.get("max_files") or 8),
                          history=bool(request.get("history", True)), cache=cache)
        else:
            raise ValueError(f"unknown op {op!r} (locate or coupled)")
        return {"id": rid, "ok": True, "result": res}
    except Exception as exc:  # noqa: BLE001 - a bad request must not end the worker
        return {"id": rid, "ok": False, "error": f"{type(exc).__name__}: {exc}"[:500]}


def serve(repo: Path | str, stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
    """The worker: load the graph once, print ``{"ready": true, "seconds": s}``, then answer one JSON request per
    line until end of input. Every answer is one flushed line; a line that is not a request gets an error answer."""
    repo = Path(repo).resolve()
    stdin, out = stdin or sys.stdin, stdout or sys.stdout

    def send(obj: dict) -> None:
        out.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str) + "\n")
        out.flush()

    t0 = time.monotonic()
    keeper, cache = GraphKeeper(repo), {}
    try:
        keeper()
        warm = True
    except Exception as exc:  # noqa: BLE001 - coupled needs no graph: the worker still serves it
        warm = False
        print(f"verinoda locate --serve: the graph could not be loaded ({exc}); locate will fail", file=sys.stderr)
    send({"ready": True, "seconds": round(time.monotonic() - t0, 3), **({} if warm else {"graph": False})})
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError as exc:
            send({"id": None, "ok": False, "error": f"not JSON: {exc}"[:200]})
            continue
        with contextlib.redirect_stdout(sys.stderr):  # a stray print must not corrupt the protocol
            res = handle(req, repo, keeper, cache)
        send(res)
    return 0
