"""Commit, diff and revision search: when a text appeared or disappeared, which commits match, what two
revisions differ in.

"When did ``retry_budget`` appear, and when did it go?" is a question about the repository's history, and git
answers it exactly: ``git log -S<text>`` lists the commits that changed how often the text occurs, ``-G<regex>``
the ones whose added or removed lines match. :func:`text_history` reads those commits with their zero-context
patches (oldest first), counts the text in each file's added and removed lines, and says:

- the commit that first added it (the oldest commit that added an occurrence), with the line it was added at;
- when HEAD has no occurrence, the commit that last removed it, with the line it was removed from;
- where it is at HEAD otherwise (``git grep``).

Each of these is a ``history`` claim whose evidence is the commit (``git_history``, the diff line quoted). The
reading is exact for the history reachable from HEAD; a shallow clone or a cut list (more than :data:`MAX_EVENTS`
commits) leaves the first appearance unproven, so that claim is ``strong_inference`` then, with the reason.

:func:`commits` searches commit messages, authors, paths, dates and diff content (``git log --grep``,
``--author``, ``-G``); :func:`compare` lists what one revision has that another has not (the commits of
``A..B``, their merge base, the files changed with their line counts). Git is only read, never written; every
argument that reaches it is one ``--option=value`` word or a path after ``--``, and a revision is checked to name
a commit first.
"""
from __future__ import annotations

import re
from pathlib import Path

from verinoda import evidence as evmod
from verinoda.snapshot import git
from verinoda.treestate import _GIT_SAFE

MAX_EVENTS = 200     # commits read for one text; more and the oldest (the first appearance) are cut
MAX_COMMITS = 100    # commits a search or a comparison lists at most
MAX_FILES = 200      # changed files a comparison lists at most
MAX_SITES = 10       # HEAD occurrences listed
_GIT_TIMEOUT = 120
_HDR = "\x1e@@"      # start of one commit in the log output (never in a diff line)
_FMT = f"--format={_HDR}%H%x1f%an%x1f%aI%x1f%s"
_HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
# no external diff or text conversion (a repository's config could name a program), no colour, no renames
# (a moved file's text is neither added nor removed)
_DIFF_SAFE = ("--no-ext-diff", "--no-textconv", "--no-color", "--no-renames")


def _git(repo: Path, *args: str) -> str | None:
    return git(repo, *_GIT_SAFE, *args, timeout=_GIT_TIMEOUT)


def _is_git(repo: Path) -> bool:
    return _git(repo, "rev-parse", "--verify", "--quiet", "HEAD^{commit}") is not None


def _shallow(repo: Path) -> bool:
    return (_git(repo, "rev-parse", "--is-shallow-repository") or "").strip() == "true"


def _word(value: str | None, what: str) -> str | None:
    """A filter value as given (stripped), or None; one line only."""
    if value is None:
        return None
    v = str(value).strip()
    if not v:
        return None
    if "\n" in v or "\r" in v or "\x00" in v:
        raise ValueError(f"{what} must be one line")
    return v


def _pathspec(path: str | None) -> list[str]:
    p = _word(path, "path")
    return ["--", p.replace("\\", "/")] if p else []


def _rev(repo: Path, rev: str, what: str) -> str:
    """The commit ``rev`` names (a branch, tag, sha, ``HEAD~3``); ValueError when it names none."""
    r = _word(rev, what)
    if not r:
        raise ValueError(f"{what}: a revision is needed")
    if r.startswith("-"):
        raise ValueError(f"{what}: {r!r} is not a revision")
    sha = (_git(repo, "rev-parse", "--verify", "--quiet", f"{r}^{{commit}}") or "").strip()
    if not sha:
        raise ValueError(f"{what}: {r!r} names no commit in this repository")
    return sha


def _not_git(kind: str, repo: Path) -> dict:
    return {"status": "not_git", "kind": kind, "note": f"{repo} is not a git work tree with a commit: no history "
                                                       "to search"}


def _header(line: str) -> dict:
    sha, author, date, subject = line[len(_HDR):].split("\x1f", 3)
    return {"commit": sha, "date": date, "author": author, "subject": subject}


def _ev(c: dict, *, path: str | None = None, line: int | None = None, excerpt: str | None = None,
        change: str | None = None) -> dict:
    """``git_history`` evidence for commit ``c``: the diff line when there is one, else the subject."""
    where = f" {path}:{line}" if path and line else (f" {path}" if path else "")
    text = excerpt if excerpt is not None else c["subject"]
    meta = {"date": c["date"], "author": c["author"], "subject": c["subject"]}
    if path:
        meta.update({"file": path, "line": line, "change": change})
    return {"source_type": "git_history", "locator": f"commit {c['commit']}{where}", "commit_sha": c["commit"],
            "content_hash": evmod.content_hash(text), "excerpt": text[:300], "meta": meta}


# -- when a text appeared or disappeared ---------------------------------------------------------------------

def _count(line: str, text: str, rx: re.Pattern | None) -> int:
    if rx is not None:
        return 1 if rx.search(line) else 0  # a regex counts matching lines, as git -G reads them
    return line.count(text)


def _parse_patches(out: str, text: str, rx: re.Pattern | None) -> list[dict]:
    """Commits (as git listed them) with, per file, the occurrences its added and removed lines hold and the
    first line of each (new-file line for an addition, parent-file line for a removal)."""
    commits: list[dict] = []
    cur: dict | None = None
    f: dict | None = None
    old_ln = new_ln = 0
    for line in out.split("\n"):
        if line.startswith(_HDR):
            cur = {**_header(line), "files": []}
            commits.append(cur)
            f = None
            continue
        if cur is None:
            continue
        if line.startswith("diff --git "):
            f = {"path": None, "added": 0, "removed": 0, "added_at": None, "removed_at": None,
                 "added_line": None, "removed_line": None}
            cur["files"].append(f)
            in_header = True  # until the first hunk: a removed "-- x" line is "--- x" too
            continue
        if f is None:
            continue
        if in_header and line.startswith("--- "):
            if line != "--- /dev/null":
                f["path"] = f["path"] or _diff_path(line[4:], "a/")
            continue
        if in_header and line.startswith("+++ "):
            if line != "+++ /dev/null":
                f["path"] = _diff_path(line[4:], "b/")
            continue
        m = _HUNK.match(line)
        if m:
            old_ln, new_ln = int(m.group(1)), int(m.group(2))
            in_header = False
            continue
        if line.startswith("+"):
            n = _count(line[1:], text, rx)
            if n:
                f["added"] += n
                if f["added_at"] is None:
                    f["added_at"], f["added_line"] = new_ln, line[1:].strip()
            new_ln += 1
        elif line.startswith("-"):
            n = _count(line[1:], text, rx)
            if n:
                f["removed"] += n
                if f["removed_at"] is None:
                    f["removed_at"], f["removed_line"] = old_ln, line[1:].strip()
            old_ln += 1
    for c in commits:
        c["files"] = [x for x in c["files"] if x["path"] and (x["added"] or x["removed"])]
    return commits


_ESC = {"\\": "\\", '"': '"', "t": "\t", "n": "\n", "r": "\r", "a": "\a", "b": "\b", "f": "\f", "v": "\v"}


def _diff_path(p: str, prefix: str) -> str:
    """A path of a ``---``/``+++`` line; git quotes one with a control character, a quote or a backslash (other
    characters stay as they are with core.quotepath off)."""
    p = p.rstrip("\t")
    if len(p) > 1 and p.startswith('"') and p.endswith('"'):
        raw, out, i = p[1:-1], bytearray(), 0
        while i < len(raw):
            ch = raw[i]
            if ch == "\\" and i + 1 < len(raw):
                if raw[i + 1:i + 4].isdigit() and len(raw[i + 1:i + 4]) == 3:
                    out.append(int(raw[i + 1:i + 4], 8) & 0xFF)
                    i += 4
                    continue
                out += _ESC.get(raw[i + 1], raw[i + 1]).encode("utf-8")
                i += 2
                continue
            out += ch.encode("utf-8")
            i += 1
        p = out.decode("utf-8", "replace")
    return p[len(prefix):] if p.startswith(prefix) else p


def _at_head(repo: Path, text: str, regex: bool, spec: list[str]) -> dict:
    """The lines at HEAD that hold the text (``git grep``, binary files left out)."""
    out = _git(repo, "grep", "-n", "-I", "--null", "-E" if regex else "-F", "-e", text, "HEAD", *spec)
    sites = []
    for line in (out or "").splitlines():
        parts = line.split("\x00", 2)
        if len(parts) < 3:
            continue
        where, ln, body = parts
        sites.append({"path": where[len("HEAD:"):] if where.startswith("HEAD:") else where, "line": int(ln),
                      "text": body.strip()[:200]})
    return {"present": bool(sites), "count": len(sites), "sites": sites[:MAX_SITES],
            "truncated": len(sites) > MAX_SITES}


def text_history(repo: Path, text: str, *, regex: bool = False, path: str | None = None) -> dict:
    """When ``text`` (a literal; with ``regex``, an extended regular expression) appeared and, when HEAD no
    longer has it, disappeared: the commits that added and removed it, oldest first, and claims citing them."""
    repo = Path(repo)
    t = _word(text, "text")
    if not t:
        raise ValueError("text: give the text (or with regex, the pattern) to look for")
    rx = None
    if regex:
        try:
            rx = re.compile(t)
        except re.error as exc:
            raise ValueError(f"text: not a regular expression: {exc}") from None
    base = {"kind": "text", "text": t, "regex": bool(regex), "path": _word(path, "path")}
    if not _is_git(repo):
        return {**base, **_not_git("text", repo)}
    spec = _pathspec(path)
    pick = f"-G{t}" if regex else f"-S{t}"
    out = _git(repo, "log", pick, f"-n{MAX_EVENTS + 1}", _FMT, "-p", "-U0", *_DIFF_SAFE, *spec)
    if out is None:
        raise ValueError(f"git log {pick[:2]} failed (a pattern git cannot read, or it took over "
                         f"{_GIT_TIMEOUT} s)")
    listed = _parse_patches(out, t, rx)
    truncated = len(listed) > MAX_EVENTS
    events = [c for c in reversed(listed[:MAX_EVENTS]) if c["files"]]  # oldest first
    shallow = _shallow(repo)
    now = _at_head(repo, t, bool(regex), spec)
    limits = ["history reachable from HEAD only (other branches are not searched)",
              "merge commits are not diffed: a text a merge alone introduced is not seen",
              "a moved file's text is neither added nor removed (no rename detection)"]
    if regex:
        limits.append("a regex is git's (POSIX extended) to select commits and Python's to count lines")
    else:
        limits.append("a text spanning lines is not counted in the diff lines (git -S still selects the commit)")
    res = {**base, "status": "found" if events else "not_found", "events": [_event(c) for c in events],
           "head": now, "truncated": truncated, "shallow": shallow,
           "coverage": {"method": f"git log {pick[:2]} over the history of HEAD, zero-context patches; "
                                  "git grep at HEAD", "limits": limits},
           "claims": [], "unknowns": []}
    if not events:
        res["unknowns"].append({
            "question": f"when did {t!r} appear?",
            "why": "no commit reachable from HEAD added or removed it" + (" (a shallow clone)" if shallow else ""),
            "next_step": ("check the spelling or search without --path; `verinoda history commits --diff PATTERN` "
                          "matches changed lines by regex") if not now["present"] else
                         "it is at HEAD but no commit added it in the history searched: the clone may be shallow"})
        return res
    complete = not truncated and not shallow
    why_not = ("the history is cut at %d commits: an older commit may have added it first" % MAX_EVENTS
               if truncated else "a shallow clone: the commits before its boundary are missing")
    first = next((c for c in events if any(f["added"] for f in c["files"])), None)
    if first is not None:
        f = next(x for x in first["files"] if x["added"])
        claim = {"kind": "history", "status": "primary_source_verified" if complete else "strong_inference",
                 "text": f"`{t}` first appeared in commit {first['commit'][:10]} ({first['date'][:10]}, "
                         f"{first['author']}): {first['subject']} - added at {f['path']}:{f['added_at']}"
                         + (f" (history under {base['path']})" if base["path"] else ""),
                 "evidence": [_ev(first, path=f["path"], line=f["added_at"], excerpt="+" + (f["added_line"] or ""),
                                  change="added")],
                 "subjects": [f["path"]]}
        if not complete:
            claim["uncertainties"] = [why_not]
        res["claims"].append(claim)
        res["appeared"] = {"commit": first["commit"], "at": f"{f['path']}:{f['added_at']}"}
    else:
        res["unknowns"].append({"question": f"when did {t!r} appear?",
                                "why": "the commits found only removed it: " + (why_not if not complete else
                                                                                "it was added before them"),
                                "next_step": "search without --path (it may have come from another file)"})
    if not now["present"]:
        last = next((c for c in reversed(events) if any(f["removed"] for f in c["files"])), None)
        if last is not None:
            f = next(x for x in last["files"] if x["removed"])
            res["claims"].append({
                "kind": "history", "status": "primary_source_verified",
                "text": f"`{t}` disappeared in commit {last['commit'][:10]} ({last['date'][:10]}, {last['author']}): "
                        f"{last['subject']} - removed from {f['path']}:{f['removed_at']}; HEAD has no occurrence"
                        + (f" under {base['path']}" if base["path"] else ""),
                "evidence": [_ev(last, path=f["path"], line=f["removed_at"],
                                 excerpt="-" + (f["removed_line"] or ""), change="removed")],
                "subjects": [f["path"]]})
            res["disappeared"] = {"commit": last["commit"], "at": f"{f['path']}:{f['removed_at']}"}
    return res


def _event(c: dict) -> dict:
    return {"commit": c["commit"], "date": c["date"], "author": c["author"], "subject": c["subject"],
            "files": [{"path": f["path"], "added": f["added"], "removed": f["removed"],
                       **({"added_at": f["added_at"]} if f["added"] else {}),
                       **({"removed_at": f["removed_at"]} if f["removed"] else {})} for f in c["files"]]}


# -- commit search -------------------------------------------------------------------------------------------

def commits(repo: Path, *, message: str | None = None, author: str | None = None, path: str | None = None,
            since: str | None = None, until: str | None = None, diff: str | None = None,
            limit: int = 20) -> dict:
    """Commits reachable from HEAD, newest first, whose message matches ``message``, whose author (name or
    e-mail) matches ``author``, that touch ``path``, fall between ``since`` and ``until`` (any date git reads:
    ``2026-01-31``, ``2 weeks ago``) and, with ``diff``, whose added or removed lines match that regular
    expression. Message, author and diff patterns ignore case. Each commit lists the files it changed (with
    ``diff``: the files whose lines matched)."""
    repo = Path(repo)
    limit = max(1, min(int(limit), MAX_COMMITS))
    filters = {k: v for k, v in (("message", _word(message, "message")), ("author", _word(author, "author")),
                                 ("path", _word(path, "path")), ("since", _word(since, "since")),
                                 ("until", _word(until, "until")), ("diff", _word(diff, "diff"))) if v}
    base = {"kind": "commits", "filters": filters}
    if not _is_git(repo):
        return {**base, **_not_git("commits", repo)}
    args = ["log", f"-n{limit + 1}", _FMT, "--name-only", *_DIFF_SAFE, "--regexp-ignore-case"]
    if "message" in filters:
        args.append(f"--grep={filters['message']}")
    if "author" in filters:
        args.append(f"--author={filters['author']}")
    if "since" in filters:
        args.append(f"--since={filters['since']}")
    if "until" in filters:
        args.append(f"--until={filters['until']}")
    if "diff" in filters:
        args.append(f"-G{filters['diff']}")
    out = _git(repo, *args, *_pathspec(path))
    if out is None:
        raise ValueError("git log failed (a pattern or date git cannot read, or it took over "
                         f"{_GIT_TIMEOUT} s)")
    found: list[dict] = []
    for line in out.split("\n"):
        if line.startswith(_HDR):
            found.append({**_header(line), "files": []})
        elif line.strip() and found:
            found[-1]["files"].append(line.strip())
    for c in found:
        c["evidence"] = f"commit {c['commit']}"
    return {**base, "status": "found" if found else "not_found", "commits": found[:limit],
            "truncated": len(found) > limit,
            "coverage": {"method": "git log over the history of HEAD, newest first",
                         "limits": ["history reachable from HEAD only",
                                    "message, author and diff patterns are git's regular expressions, case "
                                    "ignored; a merge commit's diff is not searched"]}}


# -- two revisions -------------------------------------------------------------------------------------------

def compare(repo: Path, base_rev: str, head_rev: str, *, path: str | None = None) -> dict:
    """What ``head_rev`` has that ``base_rev`` has not: the commits of ``base..head`` (newest first), the merge
    base, and the files that differ between the two trees with their added and removed line counts."""
    repo = Path(repo)
    out_base = {"kind": "compare", "base": {"rev": base_rev}, "head": {"rev": head_rev}, "path": _word(path, "path")}
    if not _is_git(repo):
        return {**out_base, **_not_git("compare", repo)}
    a = _rev(repo, base_rev, "base")
    b = _rev(repo, head_rev, "head")
    out_base["base"]["commit"], out_base["head"]["commit"] = a, b
    spec = _pathspec(path)
    mb = (_git(repo, "merge-base", a, b) or "").strip() or None
    log = _git(repo, "log", f"-n{MAX_COMMITS + 1}", _FMT, f"{a}..{b}", *spec) or ""
    only_head = [_header(x) for x in log.split("\n") if x.startswith(_HDR)]
    back = _git(repo, "rev-list", "--count", f"{b}..{a}", *spec)
    status = _git(repo, "diff", "--name-status", "-z", *_DIFF_SAFE, a, b, *spec)
    numstat = _git(repo, "diff", "--numstat", "-z", *_DIFF_SAFE, a, b, *spec)
    if status is None or numstat is None:
        raise ValueError("git diff failed between the two revisions")
    kinds = {}
    parts = status.split("\x00")
    for i in range(0, len(parts) - 1, 2):
        if parts[i]:
            kinds[parts[i + 1]] = {"A": "added", "D": "deleted", "M": "modified", "T": "type changed"}.get(
                parts[i][:1], parts[i])
    files = []
    for rec in numstat.split("\x00"):
        m = re.match(r"^(-|\d+)\t(-|\d+)\t(.*)$", rec, re.S)
        if not m:
            continue
        p = m.group(3)
        files.append({"path": p, "change": kinds.get(p, "modified"),
                      "added": None if m.group(1) == "-" else int(m.group(1)),
                      "removed": None if m.group(2) == "-" else int(m.group(2))})
    files.sort(key=lambda f: f["path"])
    same = a == b
    return {**out_base, "status": "same" if same else "found", "merge_base": mb,
            "commits": only_head[:MAX_COMMITS], "commits_truncated": len(only_head) > MAX_COMMITS,
            "commits_only_in_base": int(back.strip()) if back and back.strip().isdigit() else None,
            "files": files[:MAX_FILES], "files_truncated": len(files) > MAX_FILES,
            "totals": {"files": len(files), "added": sum(f["added"] or 0 for f in files),
                       "removed": sum(f["removed"] or 0 for f in files)},
            "coverage": {"method": "git log base..head, git diff base head (the two trees, not the working tree)",
                         "limits": ["a renamed file is listed as deleted and added",
                                    "binary files have no line counts (null)"]}}


# -- rendering -----------------------------------------------------------------------------------------------

def _short(c: dict) -> str:
    return f"{c['commit'][:10]} {c['date'][:10]} {c['author']}: {c['subject']}"


def render(res: dict) -> str:
    if res.get("status") == "not_git":
        return res["note"]
    kind = res.get("kind")
    if kind == "text":
        what = f"/{res['text']}/" if res.get("regex") else repr(res["text"])
        out = [f"{what}" + (f" under {res['path']}" if res.get("path") else "") + ":"]
        for c in res.get("claims") or []:
            out.append(f"  [{c['status']}] {c['text']}")
            out += [f"    uncertain: {u}" for u in c.get("uncertainties") or []]
        for u in res.get("unknowns") or []:
            out.append(f"  unknown: {u['why']}\n    next: {u['next_step']}")
        if res.get("events"):
            out.append(f"  commits that added or removed it ({len(res['events'])}, oldest first):")
            for e in res["events"]:
                ch = ", ".join(f"{f['path']} +{f['added']}/-{f['removed']}" for f in e["files"][:4])
                out.append(f"    {_short(e)}  [{ch}]")
        head = res.get("head") or {}
        if head.get("present"):
            out.append(f"  at HEAD: {head['count']} line(s): "
                       + ", ".join(f"{s['path']}:{s['line']}" for s in head["sites"])
                       + (" ..." if head.get("truncated") else ""))
        else:
            out.append("  at HEAD: none")
        if res.get("truncated"):
            out.append(f"  (cut at {MAX_EVENTS} commits: narrow with --path)")
        return "\n".join(out)
    if kind == "commits":
        f = res.get("filters") or {}
        out = ["commits" + (" where " + ", ".join(f"{k}={v!r}" for k, v in f.items()) if f else "") + ":"]
        if not res.get("commits"):
            out.append("  none")
        for c in res.get("commits") or []:
            out.append(f"  {_short(c)}")
            if c["files"]:
                out.append("    " + ", ".join(c["files"][:6]) + (" ..." if len(c["files"]) > 6 else ""))
        if res.get("truncated"):
            out.append("  (more: raise --limit or narrow the filters)")
        return "\n".join(out)
    b, h = res["base"], res["head"]
    out = [f"{b['rev']} ({b['commit'][:10]}) .. {h['rev']} ({h['commit'][:10]})"
           + (f" under {res['path']}" if res.get("path") else "")
           + (f"; merge base {res['merge_base'][:10]}" if res.get("merge_base") else "; no merge base")]
    if res["status"] == "same":
        out.append("  the same commit")
        return "\n".join(out)
    out.append(f"  {len(res['commits'])}{'+' if res.get('commits_truncated') else ''} commit(s) only in "
               f"{h['rev']}; {res.get('commits_only_in_base')} only in {b['rev']}")
    for c in res["commits"][:20]:
        out.append(f"    {_short(c)}")
    t = res["totals"]
    out.append(f"  {t['files']} file(s) differ, +{t['added']}/-{t['removed']} lines:")
    for f in res["files"][:40]:
        counts = "binary" if f["added"] is None else f"+{f['added']}/-{f['removed']}"
        out.append(f"    {f['change']:<9} {f['path']} {counts}")
    if len(res["files"]) > 40 or res.get("files_truncated"):
        out.append("    ... (--json lists them)")
    return "\n".join(out)
