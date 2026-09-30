"""Hotspots: change frequency times complexity, per file and per function (the map's ``hotspots`` view).

A file's **changes** are the non-merge commits among the last ``n_commits`` that touched it (``git log
--no-merges --name-only``); its **complexity** is the sum of its functions' cyclomatic complexity as
:mod:`verinoda.health` counts it on the current text. A function's changes are the non-merge commits whose
changed lines fall inside it: each commit's ``-U0`` hunks are carried to the current text through the diffs
between the file's versions (keyed by blob id, so a side branch reached through a merge is carried through the
merge's diff to that parent, and the working-tree edits through a line diff against ``HEAD``). A line a later
commit replaced takes the place of its replacement (the i-th line of a hunk the i-th, or the last, new line); a
line a later commit deleted takes the place of the line before the deletion when the lines on both sides of it
are one function's; else it no longer maps. A pure deletion counts for the function that holds the lines on
both sides of it. The **score** is changes x complexity.

A file's change count and every complexity are read from git and the syntax tree (``statically_verified``); a
function's change count rests on that line carrying, and ranking by the score is a heuristic: both
``strong_inference``. A file that changes often is not wrong for that. Nothing here runs the project's code or
reaches the network.
"""

from __future__ import annotations

import difflib
import heapq
import re
from array import array
from collections import Counter
from pathlib import Path, PurePosixPath

from verinoda import health
from verinoda.snapshot import git

DEFAULT_COMMITS = 1000   # the window: the last N non-merge commits
DEFAULT_LIMIT = 20       # files and functions listed
FUNCTION_FILES = 20      # the top files whose functions are measured
REVIEW_COMMITS = 500     # per-function window of `review`: the last N commits touching the reviewed files
ZERO_BLOB = "0" * 40
_EXACT_DIFF_CELLS = 4_000_000   # a working-tree diff larger than this (lines x lines) uses difflib's autojunk
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_INDEX_RE = re.compile(r"^index ([0-9a-f]+)\.\.([0-9a-f]+)")
_GIT = ("-c", "core.quotepath=off")
# every unit of a Python function's cyclomatic complexity (verinoda/health.py) is one of these words in its text:
# the function (def), a branch, loop, handler or case arm, a boolean operator, a comprehension's for / if; so
# their count in a file (strings and comments too) bounds the file's complexity from above
_PY_BOUND_RE = re.compile(r"\b(?:def|if|elif|for|while|except|case|and|or)\b")
LIMITS = [
    "a change is a commit, whatever its size; a merge is not counted (its diffs only carry lines)",
    "complexity is the sum of the functions' cyclomatic complexity (verinoda/health.py): module-level code "
    "and languages health has no grammar for are not weighed",
    "renames are not followed: a renamed file's history starts at the rename",
    "a function's changes are the commits whose changed lines map into its current span: a replaced line takes "
    "its replacement's place, so a hunk that spans two functions can credit the wrong one; a line deleted "
    "outside one function no longer maps",
    "the score ranks where to look first; it is a heuristic, not a verdict on the code",
]


def _code(rel: str) -> bool:
    from verinoda import anchors

    return PurePosixPath(rel).suffix.lower() in (".py", ".pyi", *anchors.TS_LANGS)


def _scope(repo) -> tuple[list[str], list[str]]:
    """The diff option and the pathspec that keep a log inside the project: in a folder of its git repository,
    that folder, its paths relative to it (as the graph's are)."""
    from verinoda.treestate import project_prefix

    return (["--relative"], ["--", "."]) if project_prefix(repo) else ([], [])


def _grafted(repo) -> set[str]:
    """The boundary commits of a shallow clone (their diff shows every file as added); empty when not shallow."""
    if (git(repo, "rev-parse", "--is-shallow-repository") or "").strip() != "true":
        return set()
    path = (git(repo, "rev-parse", "--git-path", "shallow") or "").strip()
    if not path:
        return set()
    p = Path(path) if Path(path).is_absolute() else Path(repo) / path
    try:
        return {ln.strip() for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()}
    except OSError:
        return set()


def file_changes(repo, n_commits: int = DEFAULT_COMMITS) -> dict | None:
    """Per path, the non-merge commits among the last ``n_commits`` that touched it; None without git history.
    ``{"changes": Counter, "last": {path: (sha, date)}, "commits", "oldest", "whole_history", "shallow"}``. A
    shallow clone's boundary commits are not counted (their diff is the whole tree)."""
    rel, spec = _scope(repo)
    out = git(repo, *_GIT, "log", "--no-merges", "--no-renames", *rel, f"-n{n_commits}", "--format=@@%H%x1f%cI",
              "--name-only", *spec)
    if out is None:
        return None
    grafted = _grafted(repo)
    changes: Counter = Counter()
    last: dict[str, tuple[str, str]] = {}
    commits, oldest, cur, skip = 0, None, ("", ""), False
    for line in out.splitlines():
        if line.startswith("@@"):
            sha, _, date = line[2:].partition("\x1f")
            skip = sha in grafted
            if not skip:
                cur, oldest = (sha, date), date
                commits += 1
        elif line.strip() and not skip:
            f = line.strip()
            changes[f] += 1
            last.setdefault(f, cur)   # newest first
    return {"changes": changes, "last": last, "commits": commits, "oldest": oldest,
            "whole_history": commits < n_commits and not grafted, "shallow": bool(grafted)}


def _identity(n: int) -> array:
    return array("i", range(1, n + 1))


def _line_map(old: str, new: str, own: list | None = None) -> array:
    """For each line of ``old``, its line in ``new`` (0: gone), carried as :func:`_parent_map` carries a diff:
    an unchanged line as its copy, an edited line as the line in its place. The lines both texts start and end
    with are matched first; a large middle is diffed with difflib's popular-line heuristic (``autojunk``) so a
    file of many repeated lines does not take quadratic time."""
    a, b = old.split("\n"), new.split("\n")
    if a == b:
        return _identity(len(a))
    pre = 0
    while pre < len(a) and pre < len(b) and a[pre] == b[pre]:
        pre += 1
    suf = 0
    while suf < len(a) - pre and suf < len(b) - pre and a[-1 - suf] == b[-1 - suf]:
        suf += 1
    ma, mb = a[pre:len(a) - suf], b[pre:len(b) - suf]
    sm = difflib.SequenceMatcher(None, ma, mb, autojunk=len(ma) * len(mb) > _EXACT_DIFF_CELLS)
    hunks = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag != "equal":
            ol, nl = i2 - i1, j2 - j1
            hunks.append((pre + i1 + (1 if ol else 0), ol, pre + j1 + (1 if nl else 0), nl))
    return _parent_map(_identity(len(b)), hunks, own)


def _parent_map(m: array, hunks: list[tuple[int, int, int, int]], own: list | None = None) -> array:
    """The map of the version before a diff, from the map ``m`` of the version after it and the diff's hunks: an
    unchanged line maps as its copy does, a replaced line as the new line in its place (the i-th, or the last),
    a deleted line as the line before the deletion when ``own`` (current line -> function) puts the lines on
    both sides of it in one function, else to 0."""
    def cur(i: int) -> int:   # 0-based line of the new version -> current line (0: none)
        return m[i] if 0 <= i < len(m) else 0

    out = array("i")
    p = c = 0   # lines of the old and the new version passed
    for os_, ol, _ns, nl in hunks:
        k = (os_ - 1 if ol else os_) - p   # unchanged lines before the hunk
        run = m[c:c + k]
        out.extend(run)
        if len(run) < k:
            out.extend(array("i", bytes(4 * (k - len(run)))))
        c += k
        if nl:
            out.extend(array("i", (cur(c + min(i, nl - 1)) for i in range(ol))))
        else:
            before, after = cur(c - 1), cur(c)
            inside = bool(own is not None and before and after and own[before] is not None
                          and own[before] == own[after])
            out.extend(array("i", [before if inside else 0] * ol))
        p, c = p + k + ol, c + nl
    out.extend(m[c:])
    return out


def _owners(fns: dict[str, health.FnMetrics], n_lines: int) -> list[str | None]:
    """For each current line (index 0 unused), the innermost function holding it."""
    own: list[str | None] = [None] * (n_lines + 2)
    for m in sorted(fns.values(), key=lambda m: (m.start, -m.end)):
        for ln in range(max(1, m.start), min(m.end, n_lines) + 1):
            own[ln] = m.qual   # an inner definition starts later and overwrites its parent's lines
    return own


def function_changes(repo, texts: dict[str, str], *, n_commits: int | None = REVIEW_COMMITS,
                     since: str | None = None, metrics: dict | None = None) -> dict | None:
    """Per file of ``texts`` (path -> current text) and function: ``{"changes", "last": (sha, date)}`` over the
    commits of the window (the last ``n_commits`` touching these files, or those since ``since``); None without
    git history. ``{"files": {path: {qual: {...}}}, "metrics": {path: {qual: FnMetrics}}, "commits",
    "unmapped"}``; ``metrics``: the files' :func:`verinoda.health.file_metrics` when already counted."""
    rels = sorted(r for r in texts if _code(r))
    known = metrics or {}
    metrics = {r: (known[r] if r in known else health.file_metrics(r, texts[r])) or {} for r in rels}
    rels = [r for r in rels if metrics[r]]
    if not rels:
        return {"files": {}, "metrics": metrics, "commits": 0, "unmapped": 0}
    owners = {r: _owners(metrics[r], texts[r].count("\n") + 1) for r in rels}
    tree = git(repo, *_GIT, "ls-tree", "HEAD", "--", *rels)   # paths relative to the project's folder
    if tree is None:
        return None
    maps: dict[tuple[str, str], array] = {}
    for line in tree.splitlines():
        info, _, path = line.partition("\t")
        parts = info.split()
        if len(parts) == 3 and parts[1] == "blob" and path in texts:
            head = git(repo, "cat-file", "blob", parts[2])
            if head is not None:
                maps[(path, parts[2])] = _line_map(head.replace("\r\n", "\n"), texts[path].replace("\r\n", "\n"),
                                                   owners.get(path))
    # no external diff or text conversion: a repository's config could name a program, and a converted text's
    # line numbers are not the file's
    args = ["log", "--topo-order", "-m", "--no-renames", "--full-index", "-U0", "--no-color", "--no-ext-diff",
            "--no-textconv", *_scope(repo)[0], "--format=@@%H%x1f%P%x1f%cI"]
    args += [f"-n{n_commits}"] if n_commits else []
    args += [f"--since={since}"] if since else []
    log = git(repo, *_GIT, *args, "--", *rels, timeout=120)
    if log is None:
        return None
    grafted = _grafted(repo)
    counts: dict[str, dict[str, dict]] = {r: {} for r in rels}
    shas: set[str] = set()
    unmapped = 0
    commit = ("", "", False)   # sha, date, is a merge
    sec: dict | None = None

    def close() -> None:
        nonlocal unmapped
        if sec is None or sec["rel"] not in owners or sec["new"] is None:
            return
        rel, old, new, hunks = sec["rel"], sec["old"], sec["new"], sec["hunks"]
        m = array("i") if new == ZERO_BLOB else maps.get((rel, new))
        if m is None:
            unmapped += 0 if commit[2] else 1
            return
        if not commit[2]:
            own, hit = owners[rel], set()

            def at(ln: int) -> str | None:
                cur = m[ln - 1] if 0 < ln <= len(m) else 0
                return own[cur] if cur else None

            for _os, _ol, ns, nl in hunks:
                if nl:
                    hit.update(q for ln in range(ns, ns + nl) if (q := at(ln)))
                elif (q := at(ns)) and q == at(ns + 1):
                    hit.add(q)
            for q in hit:
                row = counts[rel].setdefault(q, {"changes": 0, "last": (commit[0], commit[1])})
                row["changes"] += 1
        if old != ZERO_BLOB and (rel, old) not in maps:
            maps[(rel, old)] = _parent_map(m, hunks, owners[rel])

    skip = 0   # hunk body lines still to pass
    for line in log.splitlines():
        if skip:
            if not line.startswith("\\"):
                skip -= 1
            continue
        if line.startswith("@@") and not line.startswith("@@ -"):
            close()
            sec = None
            sha, parents, date = (line[2:].split("\x1f") + ["", ""])[:3]
            # a merge, or a shallow clone's boundary (its diff adds every file): its diffs only carry lines
            commit = (sha, date, len(parents.split()) > 1 or sha in grafted)
            if sha not in grafted:
                shas.add(sha)
        elif line.startswith("diff --git "):
            close()
            sec = {"rel": None, "old": None, "new": None, "hunks": []}
        elif sec is None:
            continue
        elif (mi := _INDEX_RE.match(line)):
            sec["old"], sec["new"] = mi.group(1), mi.group(2)
        elif line.startswith("--- a/") and sec["rel"] is None:
            sec["rel"] = line[6:].rstrip("\t")   # git ends a name that has a space with a tab
        elif line.startswith("+++ b/"):
            sec["rel"] = line[6:].rstrip("\t")
        elif (mh := _HUNK_RE.match(line)):
            ol = int(mh.group(2)) if mh.group(2) is not None else 1
            nl = int(mh.group(4)) if mh.group(4) is not None else 1
            sec["hunks"].append((int(mh.group(1)), ol, int(mh.group(3)), nl))
            skip = ol + nl
    close()
    return {"files": counts, "metrics": metrics, "commits": len(shas), "unmapped": unmapped}


def _last(pair: tuple[str, str] | None) -> dict | None:
    return {"sha": pair[0][:12], "date": pair[1]} if pair and pair[0] else None


def function_rows(fc: dict, rels: list[str]) -> list[dict]:
    """Function hotspot rows of ``rels`` from :func:`function_changes`, highest score first."""
    rows = []
    for rel in rels:
        for q, c in (fc["files"].get(rel) or {}).items():
            m = fc["metrics"][rel].get(q)
            if m is None:
                continue
            rows.append({"symbol": m.symbol, "at": f"{rel}:{m.start}-{m.end}", "changes": c["changes"],
                         "cyclomatic": m.cyclomatic, "score": c["changes"] * m.cyclomatic,
                         "last_change": _last(c["last"]), "cyclomatic_status": "statically_verified",
                         "status": "strong_inference"})
    rows.sort(key=lambda r: (-r["score"], -r["changes"], r["symbol"]))
    return rows


def hotspots(g, n_commits: int = DEFAULT_COMMITS, limit: int = DEFAULT_LIMIT) -> dict:
    """The ``hotspots`` view: code files and functions ranked by changes x complexity (module docstring)."""
    from verinoda import architecture_map as am
    from verinoda.testcode import is_test_or_support_file

    root = g.root
    method = (f"git log of the last {n_commits} non-merge commits (changes per file; per function, each "
              "commit's changed lines carried to the current text) x cyclomatic complexity counted on the syntax "
              "tree (verinoda/health.py); score = changes x complexity")
    res: dict = {"view": "hotspots", "coverage": {"method": method, "limits": list(LIMITS)}}
    fc = file_changes(root, n_commits)
    if fc is None:
        res["coverage"]["limits"].append("no git repository -> no change history")
        return res | {"is_git": False, "files": [], "files_total": 0, "functions": [], "functions_total": 0}
    aside = am._aside_roots(g)
    in_graph = set(am._files(g))
    cands = []
    for f, n in fc["changes"].items():
        if f not in in_graph or not _code(f) or is_test_or_support_file(f) or (aside and f.startswith(aside)):
            continue
        lines = am._read(root, f)
        if lines:
            text = "\n".join(lines)
            bound = len(_PY_BOUND_RE.findall(text)) if f.endswith((".py", ".pyi")) else None
            cands.append((f, n, lines, text, bound))
    # the files without a bound first, then by the bound on their score, highest first: once that bound is below
    # the lowest of the best `keep` scores, no file left can enter them
    cands.sort(key=lambda c: (c[4] is not None, -(c[1] * (c[4] or 0)), c[0]))
    keep = max(limit, FUNCTION_FILES)
    best: list[int] = []   # a min-heap of the highest `keep` scores so far
    rows, texts, fmetrics, not_measured, below = [], {}, {}, [], 0   # below: files the bound skipped
    for i, (f, n, lines, text, bound) in enumerate(cands):
        if bound is not None and len(best) >= keep and n * bound < best[0]:
            below = len(cands) - i
            break
        fns = health.file_metrics(f, text)
        if fns is None:
            not_measured.append(f)
            continue
        cx = sum(m.cyclomatic for m in fns.values())
        if not cx:
            continue
        texts[f], fmetrics[f] = text, fns
        rows.append({"file": f, "at": f"{f}:1-{len(lines)}", "changes": n, "complexity": cx,
                     "functions": len(fns), "score": n * cx, "last_change": _last(fc["last"].get(f)),
                     "counts_status": "statically_verified", "status": "strong_inference"})
        (heapq.heappush if len(best) < keep else heapq.heappushpop)(best, n * cx)
    rows.sort(key=lambda r: (-r["score"], -r["changes"], r["file"]))
    top = [r["file"] for r in rows[:FUNCTION_FILES]]
    since = None if fc["whole_history"] else fc["oldest"]
    fn = function_changes(root, {f: texts[f] for f in top}, n_commits=None, since=since,
                          metrics=fmetrics) if top else None
    frows = function_rows(fn, top) if fn else []
    window = {"commits": fc["commits"], "since": fc["oldest"], "whole_history": fc["whole_history"],
              "functions_of_top_files": len(top)}
    if fn and fn["unmapped"]:
        window["function_changes_not_mapped"] = fn["unmapped"]
    if fc["shallow"]:
        window["shallow"] = True
        res["coverage"]["limits"].append("a shallow clone: the commits before its boundary are missing and the "
                                         "boundary commits are not counted (`git fetch --unshallow` for the rest)")
    if top and fn is None:
        window["functions_not_measured"] = True
        res["coverage"]["limits"].append("the per-function git log failed or timed out: no function is ranked")
    if _scope(root)[0]:
        res["coverage"]["limits"].append("the project is a folder of its git repository: only commits and paths "
                                         "inside that folder are read")
    return res | {
        "is_git": True, "window": window, "files_changed": len(cands),
        "files": rows[:limit], "files_total": len(rows),
        "functions": frows[:limit], "functions_total": len(frows),
        **({"truncated": True} if len(rows) > limit or len(frows) > limit or below else {}),
        **({"not_measured": not_measured[:20], "not_measured_total": len(not_measured)} if not_measured else {}),
        **({"below_top_not_measured": below} if below else {}),
    }


def rank(repo, ranges: list[tuple[str, int]], texts: dict[str, str], n_commits: int = REVIEW_COMMITS
         ) -> list[dict | None] | None:
    """For each ``(path, line)`` of the current text: the hotspot of the function holding it
    (``{"symbol", "changes", "cyclomatic", "score", "status"}``), or None (no function); None instead of the list
    when the history could not be read (no git history, or git failed)."""
    try:
        fc = function_changes(repo, texts, n_commits=n_commits)
    except (OSError, ValueError, RecursionError):
        fc = None
    if not fc:
        return None
    out: list[dict | None] = []
    for rel, ln in ranges:
        ms = fc["metrics"].get(rel) or {}
        inner = [m for m in ms.values() if m.start <= ln <= m.end]
        if not inner:
            out.append(None)
            continue
        m = max(inner, key=lambda m: (m.start, -m.end))
        c = (fc["files"].get(rel) or {}).get(m.qual, {}).get("changes", 0)
        out.append({"symbol": m.symbol, "changes": c, "cyclomatic": m.cyclomatic, "score": c * m.cyclomatic,
                    "status": "strong_inference"})
    return out
