"""Ownership and knowledge: who knows a file, a folder, a line range or a symbol, from ``git blame`` and
CODEOWNERS.

``verinoda owners src/billing`` answers "who knows this code?" two ways, and keeps them apart:

- **Declared owners.** The repository's CODEOWNERS file (the first of ``.github/CODEOWNERS``, ``CODEOWNERS``,
  ``docs/CODEOWNERS``, ``.gitlab/CODEOWNERS`` at the top of the git work tree, GitHub's order) is read and each
  file of the target resolved as GitHub does: the last rule whose pattern matches the path wins, and a rule with
  no owners leaves the path unowned. One claim per rule that owns files of the target, citing the rule's line
  (``statically_verified``: the rule is read, and the matching is the documented syntax; ``strong_inference``
  when the file has GitLab sections, which this reading does not combine).
- **Knowledge from blame.** ``git blame -w --porcelain`` over the file as it is on disk (so a line range or a
  symbol's span is the one the user sees) credits each line to the author of the last commit that changed it;
  lines not committed yet are counted apart and credited to nobody. From the tally: the main author, the bus
  factor (the fewest authors who together hold more than half of the credited lines) and the knowledge loss
  (the lines credited to authors with no commit in the ``days`` before HEAD's commit date, the reference time,
  so the answer does not change with the clock). These are readings of blame, a heuristic of knowledge (a
  reformat or a moved block credits whoever made it), so each is a ``strong_inference`` claim at most, its
  evidence the blame (``git_history``, a ``git blame`` command that gives the lines again). The claims say "at
  HEAD" only when no blamed file differs from HEAD; otherwise they say the work tree was blamed and list the
  changed files, and the locator blames the work tree.

A folder is blamed file by file (text files git tracks, binary ones left out), at most :data:`MAX_FILES` files,
in path order; more and the result says ``truncated``. When the project is a folder of its git repository,
paths are relative to the project and CODEOWNERS patterns are matched against the repository path. Git is only
read; every value reaches it as a path after ``--`` or one ``-L<a>,<b>`` word.
"""
from __future__ import annotations

import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from verinoda import evidence as evmod
from verinoda.snapshot import git
from verinoda.treestate import _GIT_SAFE, project_prefix

MAX_FILES = 200        # files blamed for one folder at most
MAX_LISTED = 50        # files listed per folder (largest first)
MAX_AUTHORS = 20       # authors listed
DEFAULT_DAYS = 365     # an author with no commit in this many days before HEAD is inactive
MAX_DAYS = 100_000     # a larger --days is this one (the cutoff is never before 1970 anyway)
BUS_SHARE = 0.5        # the bus factor's authors hold more than this share of the credited lines
CODEOWNERS_PLACES = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS", ".gitlab/CODEOWNERS")
_GIT_TIMEOUT = 120
_ZERO = "0" * 40
_BLAME_HDR = re.compile(r"^([0-9a-f]{40}) \d+ (\d+)(?: \d+)?$")


def _git(repo: Path, *args: str) -> str | None:
    return git(repo, *_GIT_SAFE, *args, timeout=_GIT_TIMEOUT)


def _not_git(repo: Path, target: str) -> dict:
    return {"kind": "ownership", "target": target, "status": "not_git",
            "note": f"{repo} is not a git work tree with a commit: no blame to read"}


# -- CODEOWNERS ----------------------------------------------------------------------------------------------

def _split(line: str) -> list[str]:
    """Words of a CODEOWNERS line; a backslash keeps the next character (``\\ `` a space, ``\\#`` a hash), and an
    unescaped ``#`` that starts a word starts a comment: it and everything after it are dropped."""
    words, cur, esc, have = [], [], False, False
    for ch in line:
        if esc:
            cur.append(ch)
            esc, have = False, True
        elif ch == "\\":
            esc = True
        elif ch.isspace():
            if have:
                words.append("".join(cur))
            cur, have = [], False
        elif ch == "#" and not have:
            break
        else:
            cur.append(ch)
            have = True
    if have:
        words.append("".join(cur))
    return words


def pattern_matcher(pattern: str):
    """A CODEOWNERS pattern (gitignore-like, as GitHub documents it) as a function that tells whether a path
    relative to the top of the repository matches: ``/x`` and a pattern with an inner ``/`` are anchored at the
    top, others match at any depth; ``*`` and ``?`` stay inside one folder, a ``**`` folder crosses folders (any
    other ``**`` is a ``*``); a trailing ``/`` or ``/**`` matches only what is under that folder; ``docs/*``
    matches the files of ``docs`` but not those of its subfolders; any other pattern also matches everything
    under a folder it names. Matched folder by folder, as git's wildmatch does, never by a backtracking regex:
    the pattern comes from the repository being read, and its cost stays bounded by the pattern's and the
    path's lengths."""
    dir_only = pattern.endswith("/") and len(pattern) > 1
    p = pattern.rstrip("/") if dir_only else pattern
    anchored = p.startswith("/") or "/" in p
    segs = p.lstrip("/").split("/")
    if dir_only or (len(segs) > 1 and segs[-1] == "**"):
        if segs[-1] == "**":
            segs.pop()
        segs.append("**+")                        # one folder level or more under it
    elif not (len(segs) > 1 and segs[-1] == "*"):
        segs.append("**")                         # itself, or anything under a folder it names
    if not anchored:
        segs.insert(0, "**")
    segs = tuple(segs)

    def match(path: str) -> bool:
        return _match_segs(segs, tuple(path.split("/")))

    return match


def _match_segs(pat: tuple[str, ...], parts: tuple[str, ...]) -> bool:
    """Pattern folders against path folders; ``**`` is zero or more folders, ``**+`` one or more. Dynamic
    programming over (pattern folder, path folder): at most len(pat) * len(parts) steps."""
    n, m = len(pat), len(parts)
    # ok[j] is True when pat[i:] matches parts[j:]; built from the last pattern folder back
    ok = [False] * m + [True]
    for i in range(n - 1, -1, -1):
        seg, new = pat[i], [False] * (m + 1)
        if seg in ("**", "**+"):
            acc = False  # any ok[k] for k > j
            for j in range(m, -1, -1):
                new[j] = acc or (seg == "**" and ok[j])
                acc = acc or ok[j]
        else:
            for j in range(m):
                new[j] = ok[j + 1] and _match_word(seg, parts[j])
        ok = new
    return ok[0]


def _match_word(pat: str, s: str) -> bool:
    """One folder name against one pattern folder with ``*`` and ``?``: the greedy wildcard match (on a mismatch,
    only the last ``*`` takes one more character), at most len(pat) * len(s) steps whatever the pattern."""
    p = s_i = 0
    star, mark = -1, 0
    while s_i < len(s):
        if p < len(pat) and (pat[p] == "?" or (pat[p] != "*" and pat[p] == s[s_i])):
            p += 1
            s_i += 1
        elif p < len(pat) and pat[p] == "*":
            star, mark = p, s_i
            p += 1
        elif star >= 0:
            p, mark = star + 1, mark + 1
            s_i = mark
        else:
            return False
    while p < len(pat) and pat[p] == "*":
        p += 1
    return p == len(pat)


def read_codeowners(top: Path) -> dict | None:
    """The CODEOWNERS file GitHub would read at the top of the work tree ``top``: its rules (pattern, owners,
    line) in file order, the lines skipped with the reason, whether it has GitLab sections; None without one."""
    found = [p for p in CODEOWNERS_PLACES if (top / p).is_file()]
    if not found:
        return None
    rel = found[0]
    try:
        text = (top / rel).read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return None
    rules, skipped, sections = [], [], False
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if re.match(r"^\^?\[[^\]]*\]", line):  # a GitLab section header, with optional default owners
            sections = True
            continue
        words = _split(line)
        if not words:
            continue
        pat, owners = words[0], words[1:]
        if pat.startswith("!") or "[" in pat:
            skipped.append({"line": n, "text": line, "why": "GitHub does not support `!` or `[ ]` in patterns"})
            continue
        rules.append({"line": n, "pattern": pat, "owners": owners, "text": line, "match": pattern_matcher(pat)})
    return {"file": rel, "rules": rules, "skipped": skipped, "sections": sections, "others": found[1:]}


def owner_rule(co: dict, repo_path: str) -> dict | None:
    """The CODEOWNERS rule that decides ``repo_path`` (the last one that matches), or None."""
    for r in reversed(co["rules"]):
        if r["match"](repo_path):
            return r
    return None


# -- blame ---------------------------------------------------------------------------------------------------

def _blame(repo: Path, rel: str, span: tuple[int, int] | None, rev: str | None = None) -> dict | None:
    """Lines per commit of one file as it is on disk (or at commit ``rev``, a sha), and each commit's author
    (mailmap applied by git); None when git cannot blame it (a file deleted from the work tree, a range past its
    end)."""
    args = ["blame", "--porcelain", "-w", "--no-textconv"]
    if span:
        args.append(f"-L{span[0]},{span[1]}")
    if rev:
        args.append(rev)
    out = _git(repo, *args, "--", rel)
    if out is None:
        return None
    lines: dict[str, int] = {}
    info: dict[str, dict] = {}
    cur = None
    for line in out.split("\n"):
        m = _BLAME_HDR.match(line)
        if m:
            cur = m.group(1)
            lines[cur] = lines.get(cur, 0) + 1
            info.setdefault(cur, {})
            continue
        if cur is None or line.startswith("\t"):
            continue
        key, _, val = line.partition(" ")
        if key in ("author", "author-mail", "author-time", "summary"):
            info[cur][key] = val
        elif key == "boundary":
            info[cur]["boundary"] = True
    return {"lines": lines, "info": info}


def _author_key(info: dict) -> tuple[str, str]:
    mail = (info.get("author-mail") or "").strip("<>").strip().lower()
    name = info.get("author") or mail or "?"
    return (mail or name.lower()), name


def _last_active(repo: Path) -> tuple[dict[str, int], int]:
    """Each author's newest commit (author time, seconds) in the whole history of HEAD, keyed as blame's are, and
    HEAD's commit time (the first line git prints)."""
    out = _git(repo, "log", "--format=%aE%x1f%aN%x1f%at%x1f%ct", "HEAD") or ""
    seen: dict[str, int] = {}
    head_time = 0
    for line in out.splitlines():
        parts = line.split("\x1f")
        if len(parts) != 4 or not parts[2].isdigit():
            continue
        if not head_time and parts[3].isdigit():
            head_time = int(parts[3])
        key = parts[0].strip().lower() or parts[1].strip().lower()
        seen[key] = max(seen.get(key, 0), int(parts[2]))
    return seen, head_time


def _files(repo: Path, rel: str) -> list[str]:
    """Text files git tracks under ``rel`` (a file or folder of the project), relative to the project."""
    out = _git(repo, "ls-files", "--eol", "-z", "--", rel or ".") or ""
    files = []
    for rec in out.split("\x00"):
        meta, _, path = rec.partition("\t")
        if path and "i/-text" not in meta.split():
            files.append(path)
    return sorted(files)


# -- the answer ----------------------------------------------------------------------------------------------

def _target(repo: Path, target: str | None) -> dict:
    """The target as a path (file or folder, relative to the project) and an optional line span. ``path:A-B`` is
    a line range, ``path#Symbol`` (or ``path::Symbol``) the span of that definition as the file is on disk."""
    t = (target or "").strip().strip("\"'").replace("\\", "/")
    if "\n" in t or "\x00" in t:
        raise ValueError("target must be one line")
    if not t or t in (".", "./"):
        return {"path": "", "span": None, "shown": "."}
    from verinoda import extract

    loc = extract.parse_target(t) if not (repo / t).exists() else None
    if loc and "symbol" in loc:
        got = extract.extract_one(repo, loc)
        if got["status"] != "found":
            raise ValueError(f"{t}: {got.get('note') or got['status']}")
        d = got["definition"]
        return {"path": got["file"], "span": (d["start"], d["end"]), "shown": t, "file": True,
                "symbol": d.get("name") or loc["symbol"]}
    if loc:
        p = _inside(repo, loc["path"], t)
        if not (repo / p).is_file():
            raise ValueError(f"{p} is not a file of this project")
        a, b = min(loc["line"], loc["end"]), max(loc["line"], loc["end"])
        n = _line_count(repo / p)
        if a < 1 or b > n:
            raise ValueError(f"{t}: {p} has {n} line(s); a range is 1-{n} at most")
        return {"path": p, "span": (a, b), "shown": t, "file": True}
    p = _inside(repo, t, t)
    if not (repo / p).exists():
        raise ValueError(f"{t} is not a file or folder of this project")
    return {"path": "" if p == "." else p, "span": None, "shown": t, "file": (repo / p).is_file()}


def _line_count(path: Path) -> int:
    """Lines of a file as git counts them (a last line without a newline counts)."""
    try:
        data = path.read_bytes()
    except OSError:
        return 0
    return data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)


def _modified(repo: Path, rel: str, prefix: str) -> set[str]:
    """Files under ``rel`` (relative to the project) whose content in the work tree or the index differs from
    HEAD's, relative to the project."""
    out = _git(repo, "diff", "--name-only", "-z", "--no-renames", "HEAD", "--", rel or ".") or ""
    return {f[len(prefix):] for f in out.split("\x00") if f and f.startswith(prefix)}


def _inside(repo: Path, path: str, shown: str) -> str:
    """``path`` relative to the project (posix), or ValueError when it is outside it."""
    try:
        return (Path(repo) / path).resolve().relative_to(Path(repo).resolve()).as_posix()
    except ValueError:
        raise ValueError(f"{shown} is outside the project") from None


def owners(repo: Path, target: str | None = None, *, days: int = DEFAULT_DAYS, max_files: int = MAX_FILES) -> dict:
    """Who knows ``target`` (a file, a folder, ``path:A-B`` or ``path#Symbol``; default the whole project): the
    CODEOWNERS rules that own it, and from ``git blame`` its authors with their share of the lines, the main
    author, the bus factor and the knowledge loss, as claims."""
    repo = Path(repo).resolve()
    days = max(1, min(int(days), MAX_DAYS))
    max_files = max(1, min(int(max_files), 5000))
    # one call: the work tree's top, whether the clone is shallow, HEAD's commit (fails without a commit)
    info = (_git(repo, "rev-parse", "--show-toplevel", "--is-shallow-repository", "HEAD^{commit}") or "").split("\n")
    if len(info) < 3 or not re.fullmatch(r"[0-9a-f]{40,64}", info[2].strip()):
        return _not_git(repo, target or ".")
    top, shallow, head = Path(info[0].strip() or repo), info[1].strip() == "true", info[2].strip()
    tgt = _target(repo, target)
    prefix = project_prefix(repo)
    files = [tgt["path"]] if tgt["span"] else _files(repo, tgt["path"])
    all_files = len(files)
    truncated = len(files) > max_files
    files = files[:max_files]
    res: dict = {"kind": "ownership", "target": tgt["shown"], "path": tgt["path"] or ".", "head": head,
                 **({"lines": list(tgt["span"])} if tgt["span"] else {}),
                 **({"symbol": tgt["symbol"]} if tgt.get("symbol") else {}),
                 "files": len(files), "truncated": truncated, "claims": [], "unknowns": []}
    t0 = time.perf_counter()
    workers = max(1, min(8, os.cpu_count() or 1, len(files)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        blamed = list(pool.map(lambda f: (f, _blame(repo, f, tgt["span"])), files))
    active, head_time = _last_active(repo)
    authors: dict[str, dict] = {}
    per_file = []
    uncommitted = unread = boundary_lines = 0
    for f, b in blamed:
        if b is None:
            unread += 1
            continue
        tally: dict[str, int] = {}
        for sha, n in b["lines"].items():
            if sha == _ZERO:
                uncommitted += n
                continue
            inf = b["info"].get(sha, {})
            if inf.get("boundary") and shallow:
                boundary_lines += n
            key, name = _author_key(inf)
            a = authors.setdefault(key, {"name": name, "email": key if "@" in key else None, "lines": 0,
                                         "files": set(), "newest_blamed": 0, "commit": sha})
            a["lines"] += n
            a["files"].add(f)
            t = int(inf.get("author-time") or 0)
            if t >= a["newest_blamed"]:
                a["newest_blamed"], a["commit"] = t, sha
            tally[key] = tally.get(key, 0) + n
        total_f = sum(tally.values())
        if total_f:
            k = min(tally, key=lambda x: (-tally[x], x))  # the order of the main-author claim
            per_file.append({"path": f, "lines": total_f, "main_author": authors[k]["name"],
                             "share": round(tally[k] / total_f, 3), "authors": len(tally)})
    modified = sorted(_modified(repo, tgt["path"], prefix).intersection(f for f, b in blamed if b is not None))
    res["modified_files"] = modified[:MAX_LISTED]
    res["seconds"] = round(time.perf_counter() - t0, 2)
    total = sum(a["lines"] for a in authors.values())
    cutoff = max(0, head_time - days * 86400)
    ranked = sorted(authors.items(), key=lambda kv: (-kv[1]["lines"], kv[0]))
    res["authors"] = [{"name": a["name"], "email": a["email"], "lines": a["lines"],
                       "share": round(a["lines"] / total, 3) if total else 0.0, "files": len(a["files"]),
                       "last_commit": _day(active.get(k)), "active": active.get(k, 0) >= cutoff}
                      for k, a in ranked[:MAX_AUTHORS]]
    res["authors_truncated"] = len(ranked) > MAX_AUTHORS
    res["credited_lines"], res["uncommitted_lines"], res["unreadable_files"] = total, uncommitted, unread
    per_file.sort(key=lambda x: (-x["lines"], x["path"]))
    if not tgt["span"] and len(files) > 1:
        res["per_file"] = per_file[:MAX_LISTED]
        res["per_file_truncated"] = len(per_file) > MAX_LISTED
    limits = ["git blame credits a line to the last commit that changed it (whitespace-only changes ignored): a "
              "reformat, a move or a copy credits whoever made it; it measures who last wrote the lines, not who "
              "understands them",
              "the files as they are in the work tree; lines not committed yet are credited to nobody",
              f"inactive: no commit in the {days} days before HEAD's commit date, anywhere in the history of HEAD",
              "authors are told apart by e-mail (the repository's .mailmap applied by git)"]
    if truncated:
        limits.append(f"only the first {max_files} files in path order were blamed")
    if prefix:
        limits.append(f"the project is the folder {prefix!r} of its git repository; CODEOWNERS is read at the "
                      "repository's top")
    res["coverage"] = {"method": "git blame -w --porcelain per text file git tracks; CODEOWNERS at the top of "
                                 "the work tree", "limits": limits}
    res["knowledge_loss"] = {"days": days, "since": _day(cutoff), "lines": 0, "share": 0.0, "authors": []}
    if modified:
        limits.append(f"{len(modified)} blamed file(s) differ from HEAD: the lines are the work tree's, so a line "
                      "range or a symbol's span may not be HEAD's lines")
    what = _what(tgt, len(files), all_files)
    if total:
        _knowledge_claims(res, ranked, total, active, cutoff, head, what, tgt, shallow, boundary_lines, modified)
    else:
        res["unknowns"].append({"question": f"who wrote {what}?",
                                "why": ("git blame credited no committed line" +
                                        (f" ({uncommitted} line(s) not committed yet)" if uncommitted else "") +
                                        (f"; {unread} file(s) git could not blame" if unread else "")
                                        if files else "git tracks no text file there"),
                                "next_step": "commit the file, or name a file or folder git tracks"})
    _codeowners(res, repo, top, prefix, files, all_files, tgt, head, what)
    res["status"] = "found" if (total or (res["codeowners"] or {}).get("owned")) else "not_found"
    return res


def _day(t: int | None) -> str | None:
    return time.strftime("%Y-%m-%d", time.gmtime(max(0, t))) if t is not None else None


def _what(tgt: dict, n: int, all_n: int) -> str:
    if tgt.get("symbol"):
        return f"`{tgt['symbol']}` ({tgt['path']}:{tgt['span'][0]}-{tgt['span'][1]})"
    if tgt["span"]:
        return f"{tgt['path']}:{tgt['span'][0]}-{tgt['span'][1]}"
    if all_n == 1 and tgt["path"]:
        return f"`{tgt['path']}`"
    if n < all_n:
        return f"the first {n} of the {all_n} file(s) of `{tgt['path'] or '.'}` (in path order)"
    return f"`{tgt['path'] or '.'}` ({n} file(s))"


def _blame_ev(head: str, tgt: dict, excerpt: str, meta: dict, modified: list[str]) -> dict:
    """The blame as evidence; its locator is a git command that gives these lines again: at HEAD when no blamed
    file differs from HEAD, else on the work tree (no revision), which is what was blamed."""
    rng = f" -L{tgt['span'][0]},{tgt['span'][1]}" if tgt["span"] else ""
    rev = "" if modified else f" {head}"
    where = tgt["path"] if tgt.get("file") else f"<each text file git tracks under {tgt['path'] or '.'}>"
    meta = {**meta, **({"work_tree": True, "modified_files": modified[:MAX_LISTED]} if modified else {})}
    return {"source_type": "git_history", "locator": f"git blame -w{rng}{rev} -- {where}", "commit_sha": head,
            "content_hash": evmod.content_hash(excerpt), "excerpt": excerpt[:300], "meta": meta}


def _knowledge_claims(res: dict, ranked: list, total: int, active: dict, cutoff: int, head: str, what: str,
                      tgt: dict, shallow: bool, boundary_lines: int, modified: list[str]) -> None:
    h = head[:10]
    # "at HEAD" only when the blamed files are HEAD's; else the work tree was blamed, on top of HEAD
    at = (f"in the work tree on HEAD {h} ({len(modified)} file(s) with changes not committed)" if modified
          else f"at HEAD {h}")
    unc = ["git blame credits the last commit that changed a line; a reformat, a move or a copy credits whoever "
           "made it"]
    if modified:
        unc.append("blamed as the files are in the work tree, which differs from HEAD: a line range or a symbol's "
                   "span there may not be the same lines at HEAD")
    if shallow and boundary_lines:
        unc.append(f"a shallow clone: {boundary_lines} line(s) are credited to the oldest commit it has, which may "
                   "not have written them")
    tally = "; ".join(f"{a['name']} {a['lines']}" for _, a in ranked[:5]) + f" of {total} lines"
    k, top = ranked[0]
    share = top["lines"] / total
    res["main_author"] = {"name": top["name"], "email": top["email"], "lines": top["lines"],
                          "share": round(share, 3)}
    res["claims"].append({
        "kind": "history", "status": "strong_inference",
        "text": f"{top['name']} is the main author of {what} {at}: git blame credits them with "
                f"{top['lines']} of {total} lines ({share:.0%})"
                + (f"; their newest of those lines is from commit {top['commit'][:10]}" if top.get("commit") else ""),
        "evidence": [_blame_ev(head, tgt, tally, {"author": top["name"], "email": top["email"],
                                                  "lines": top["lines"], "total": total}, modified)],
        "subjects": [tgt["path"] or "."], "uncertainties": list(unc)})
    acc, bus = 0, []
    for _, a in ranked:
        acc += a["lines"]
        bus.append(a["name"])
        if acc > BUS_SHARE * total:
            break
    res["bus_factor"] = {"value": len(bus), "authors": bus, "share": round(acc / total, 3),
                         "threshold": BUS_SHARE}
    names = bus[0] + " alone holds" if len(bus) == 1 else ", ".join(bus) + " together hold"
    res["claims"].append({
        "kind": "history", "status": "strong_inference",
        "text": f"The bus factor of {what} {at} is {len(bus)}: {names} {acc / total:.0%} of the {total} "
                "lines git blame credits (more than half)",
        "evidence": [_blame_ev(head, tgt, tally, {"bus_factor": len(bus), "authors": bus}, modified)],
        "subjects": [tgt["path"] or "."], "uncertainties": list(unc)})
    gone = [(kk, a) for kk, a in ranked if active.get(kk, 0) < cutoff]
    lost = sum(a["lines"] for _, a in gone)
    loss = res["knowledge_loss"]
    loss.update({"lines": lost, "share": round(lost / total, 3),
                 "authors": [{"name": a["name"], "lines": a["lines"], "last_commit": _day(active.get(kk))}
                             for kk, a in gone[:MAX_AUTHORS]]})
    if gone:
        who = ", ".join(f"{a['name']} ({a['lines']}, last commit {_day(active.get(kk)) or 'unknown'})"
                        for kk, a in gone[:5])
        res["claims"].append({
            "kind": "history", "status": "strong_inference",
            "text": f"{lost} of the {total} lines of {what} ({lost / total:.0%}) {at} are credited by git "
                    f"blame to authors with no commit since {loss['since']} ({loss['days']} days before the date "
                    f"of HEAD): {who}",
            "evidence": [_blame_ev(head, tgt, tally, {"inactive_since": loss["since"],
                                                      "authors": [a["name"] for _, a in gone]}, modified)],
            "subjects": [tgt["path"] or "."], "uncertainties": unc + [
                "an author may still be reachable, or work under another e-mail the .mailmap does not join"]})


def _codeowners(res: dict, repo: Path, top: Path, prefix: str, files: list[str], all_files: int, tgt: dict,
                head: str, what: str) -> None:
    co = read_codeowners(top)
    if co is None:
        res["codeowners"] = None
        res["unknowns"].append({"question": f"who owns {what} by CODEOWNERS?",
                                "why": "no CODEOWNERS file (" + ", ".join(CODEOWNERS_PLACES) + ")",
                                "next_step": "the blame authors above are the only reading of ownership"})
        return
    by_rule: dict[int, list[str]] = {}
    unowned: list[str] = []
    for f in files:
        r = owner_rule(co, prefix + f)
        if r is None or not r["owners"]:
            unowned.append(f)
        else:
            by_rule.setdefault(r["line"], []).append(f)
    rules = {r["line"]: r for r in co["rules"]}
    if tgt["span"]:  # CODEOWNERS owns files: a line range or a symbol is owned with its file
        what = f"`{tgt['path']}`"
    owned = sorted(by_rule.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    res["codeowners"] = {"file": co["file"], "rules": len(co["rules"]), "sections": co["sections"],
                         "skipped": co["skipped"], "unowned": len(unowned), "unowned_files": unowned[:MAX_LISTED],
                         "owned": [{"line": ln, "pattern": rules[ln]["pattern"], "owners": rules[ln]["owners"],
                                    "files": len(fs), "examples": fs[:5]} for ln, fs in owned]}
    if co["others"]:
        res["coverage"]["limits"].append("CODEOWNERS also at " + ", ".join(co["others"]) + "; GitHub reads only "
                                         + co["file"])
    if co["sections"]:
        res["coverage"]["limits"].append("the CODEOWNERS file has GitLab sections: each section's last match "
                                         "applies there, which this reading does not combine (the last match in "
                                         "the file wins here)")
    # read from the work tree, so cited at HEAD only when it is HEAD's file (not changed, not uncommitted)
    changed = bool((_git(top, "status", "--porcelain", "--untracked-files=all", "--", co["file"]) or "").strip())
    res["codeowners"]["committed"] = not changed
    if changed:
        res["coverage"]["limits"].append(f"{co['file']} differs from HEAD: its rules are read as they are in the "
                                         "work tree")
    n = len(files)
    of = (f"the {n} file(s) of `{tgt['path'] or '.'}`" if n == all_files
          else f"the first {n} of the {all_files} file(s) of `{tgt['path'] or '.'}` (in path order)")
    for ln, fs in owned[:10]:
        r = rules[ln]
        commit = None if changed else head
        # in a folder of its repository the file is outside the project: cited from the repository's top
        ev = (evmod.source_evidence(top, co["file"], ln, commit=commit, meta={"root": str(top)}, anchor=False)
              if prefix else evmod.source_evidence(repo, co["file"], ln, commit=commit, anchor=False))
        if ev is None:
            continue
        who = " ".join(r["owners"])
        scope = f"{what} is" if tgt.get("file") else f"{len(fs)} of {of} are"
        claim = {"kind": "general",
                 "status": "strong_inference" if co["sections"] else "statically_verified",
                 "text": f"{scope} owned by {who}: the last CODEOWNERS rule that matches, {co['file']}:{ln} "
                         f"contains: {r['text']}",
                 "evidence": [ev], "subjects": [tgt["path"] or "."]}
        if co["sections"]:
            claim["uncertainties"] = ["GitLab sections: another section's rule may add owners"]
        res["claims"].append(claim)


# -- rendering -----------------------------------------------------------------------------------------------

def render(res: dict) -> str:
    if res.get("status") == "not_git":
        return res["note"]
    head = res["target"] + (f" ({res['files']} file(s)" + (", cut" if res.get("truncated") else "") + ")"
                            if res["files"] != 1 else "")
    out = [f"{head} at HEAD {res['head'][:10]}:"]
    for c in res["claims"]:
        out.append(f"  [{c['status']}] {c['text']}")
    for u in res["unknowns"]:
        out.append(f"  unknown: {u['question']} {u['why']}\n    next: {u['next_step']}")
    if res.get("authors"):
        out.append(f"  authors by blamed lines ({res['credited_lines']} lines"
                   + (f", {res['uncommitted_lines']} not committed" if res.get("uncommitted_lines") else "") + "):")
        for a in res["authors"][:10]:
            out.append(f"    {a['share']:>5.0%}  {a['lines']:>6}  {a['name']}"
                       + (f" <{a['email']}>" if a.get("email") else "")
                       + f"  last commit {a['last_commit'] or '?'}" + ("" if a["active"] else "  (inactive)"))
        if len(res["authors"]) > 10 or res.get("authors_truncated"):
            out.append("    ... (--json lists more)")
    co = res.get("codeowners")
    if co:
        out.append(f"  CODEOWNERS ({co['file']}, {co['rules']} rule(s)): {co['unowned']} file(s) unowned")
        for o in co["owned"][:10]:
            out.append(f"    line {o['line']}: {o['pattern']} -> {' '.join(o['owners'])} ({o['files']} file(s))")
    for f in (res.get("per_file") or [])[:15]:
        out.append(f"    {f['path']}: {f['lines']} lines, {f['main_author']} {f['share']:.0%}, "
                   f"{f['authors']} author(s)")
    return "\n".join(out)
