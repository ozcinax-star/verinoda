"""Code monitors and pattern trends (``verinoda monitor``).

A monitor is a saved search that must not gain matches: "no new ``print(``", "no new call to the old API". It lives
in ``verinoda-monitors.json`` at the project's top (committed, so CI reads the same list) with the matches it had
when it was added (its baseline). ``verinoda monitor`` runs every monitor on the working tree and exits 1 when one
has a match its baseline does not have; matches that went away are listed (a migration's progress), and
``monitor accept ID`` takes the current matches as the new baseline (the user's call, recorded with the date).

Two kinds of pattern:

- ``--regex RE``: git's extended regular expressions, run by ``git grep -E`` on the files git tracks (and the
  untracked ones it does not ignore), so a pattern is read the same way in the working tree and in history and
  nothing is matched with a backtracking engine in this process;
- ``--ast PATTERN``: the structural patterns of ``grep-ast`` (code shapes with ``$A`` / ``$$$REST``).

A match is keyed by its file and its line's text (white space collapsed), counted per key, so a line moved by an
edit above it is the same match and a changed or added line is new (as decision baselines do). ``monitor trend
ID`` counts a regex monitor's matches at commits spread over the history (``git grep -c`` at each, nothing
checked out). A search that could not finish (a timeout, a cut list) makes the monitor ``unknown``: a gate never
passes on a search it did not complete.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from datetime import date
from pathlib import Path

FILE = "verinoda-monitors.json"
FORMAT = 1
MAX_MATCHES = 5000
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class MonitorError(ValueError):
    pass


def path(repo: Path) -> Path:
    return Path(repo) / FILE


def load(repo: Path) -> dict:
    p = path(repo)
    if not p.is_file():
        return {"verinoda-monitors": FORMAT, "monitors": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MonitorError(f"{FILE} cannot be read: {exc}") from None
    if not isinstance(data, dict) or data.get("verinoda-monitors") != FORMAT or not isinstance(data.get("monitors"),
                                                                                                 list):
        raise MonitorError(f"{FILE} is not a monitors file (format {FORMAT})")
    for m in data["monitors"]:
        if not isinstance(m, dict) or not isinstance(m.get("id"), str) or \
                not (isinstance(m.get("regex"), str) or isinstance(m.get("ast"), str)):
            raise MonitorError(f"{FILE} has a monitor without an id and a regex or ast pattern: {m!r}"[:300])
    return data


def _save(repo: Path, data: dict) -> None:
    path(repo).write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")


def _key(rel: str, text: str) -> str:
    return f"{rel}\t{' '.join(text.split())}"


def _grep_args(m: dict) -> list[str]:
    paths = [p for p in m.get("paths") or [] if p]
    return ["-E", "-e", m["regex"], "--", *(paths or ["."])]


def matches(repo: Path, m: dict) -> dict:
    """The monitor's matches in the working tree: ``{"matches": [{at, text}], "complete": bool, "why"}``."""
    repo = Path(repo)
    if m.get("regex") is not None:
        rc, out, err = _git_grep(repo, ["-n", "-I", "--no-color", "--untracked", *_grep_args(m)])
        if rc == 1:            # git grep: 0 matches found, 1 none, anything else an error
            return {"matches": [], "complete": True}
        if rc != 0:
            return {"matches": [], "complete": False, "why": f"git grep failed: {err.strip()[:200]}"}
        rows = []
        for ln in out.splitlines():
            rel, _, rest = ln.partition(":")
            n, _, text = rest.partition(":")
            if n.isdigit():
                rows.append({"at": f"{rel}:{n}", "text": text.strip()[:200]})
        return {"matches": rows[:MAX_MATCHES], "complete": len(rows) <= MAX_MATCHES,
                **({"why": f"more than {MAX_MATCHES} matches"} if len(rows) > MAX_MATCHES else {})}
    from verinoda import grep_ast

    try:
        from verinoda.snapshot import list_files

        # the files as they are now (not the few-seconds listing cache: a gate reads the tree it is run on)
        res = grep_ast.run(repo, m["ast"], langs=m.get("langs") or None, paths=m.get("paths") or None,
                           max_results=MAX_MATCHES, files=list_files(repo))
    except grep_ast.PatternError as exc:
        return {"matches": [], "complete": False, "why": f"the pattern cannot be searched: {exc}"}
    rows = []
    for x in res["matches"]:
        rel, _, n = x["at"].rpartition(":")
        lines = _file_lines(repo, rel)
        k = int(n) if n.isdigit() else 0
        rows.append({"at": x["at"], "text": (lines[k - 1].strip() if 0 < k <= len(lines) else x["text"])[:200]})
    timed = (res.get("not_searched") or {}).get("timed_out") or []
    complete = not res.get("truncated") and not timed
    why = "a cut list" if res.get("truncated") else (f"{len(timed)} file(s) timed out" if timed else None)
    return {"matches": rows, "complete": complete, **({"why": why} if why else {})}


def _git_grep(repo: Path, args: list[str]) -> tuple[int, str, str]:
    """(exit code, stdout, stderr) of ``git grep`` in ``repo``: its exit code tells no match from an error."""
    import subprocess

    from verinoda.treestate import _GIT_SAFE

    try:
        r = subprocess.run(["git", "-C", str(repo), *_GIT_SAFE, "grep", *args], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=120, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 2, "", str(exc)
    return r.returncode, r.stdout, r.stderr


def _file_lines(repo: Path, rel: str) -> list[str]:
    try:
        return (Path(repo) / rel).read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n").split("\n")
    except OSError:
        return []


def _counts(rows: list[dict]) -> Counter:
    return Counter(_key(r["at"].rpartition(":")[0], r["text"]) for r in rows)


def add(repo: Path, mid: str, *, regex: str | None = None, ast: str | None = None, langs: list[str] | None = None,
        paths: list[str] | None = None, message: str = "") -> dict:
    """Add a monitor with its current matches as the baseline."""
    if not _ID.match(mid or ""):
        raise MonitorError("an id is letters, digits, '.', '_' or '-' (at most 64), starting with a letter or digit")
    if (regex is None) == (ast is None):
        raise MonitorError("give one of --regex or --ast")
    data = load(repo)
    if any(m["id"] == mid for m in data["monitors"]):
        raise MonitorError(f"a monitor {mid!r} exists (`monitor remove {mid}` first)")
    m = {"id": mid, **({"regex": regex} if regex is not None else {"ast": ast}),
         **({"langs": langs} if langs else {}), **({"paths": paths} if paths else {}),
         **({"message": message} if message else {})}
    got = matches(repo, m)
    if not got["complete"]:
        raise MonitorError(f"the search did not finish ({got.get('why')}): no baseline recorded")
    m["baseline"] = sorted(_counts(got["matches"]).elements())
    m["since"] = date.today().isoformat()
    data["monitors"].append(m)
    _save(repo, data)
    return {"id": mid, "matches": len(got["matches"]), "file": FILE}


def accept(repo: Path, mid: str) -> dict:
    """Take a monitor's current matches as its baseline (the user's call)."""
    data = load(repo)
    m = next((x for x in data["monitors"] if x["id"] == mid), None)
    if m is None:
        raise MonitorError(f"no monitor {mid!r}")
    got = matches(repo, m)
    if not got["complete"]:
        raise MonitorError(f"the search did not finish ({got.get('why')}): baseline unchanged")
    before = len(m.get("baseline") or [])
    m["baseline"] = sorted(_counts(got["matches"]).elements())
    m["accepted"] = date.today().isoformat()
    _save(repo, data)
    return {"id": mid, "before": before, "now": len(m["baseline"])}


def remove(repo: Path, mid: str) -> dict:
    data = load(repo)
    keep = [x for x in data["monitors"] if x["id"] != mid]
    if len(keep) == len(data["monitors"]):
        raise MonitorError(f"no monitor {mid!r}")
    data["monitors"] = keep
    _save(repo, data)
    return {"id": mid, "removed": True}


def check(repo: Path, only: str | None = None) -> dict:
    """Run every monitor (or ``only``): its new matches (not in the baseline), the ones gone, and whether the search
    finished. ``exit`` 1 on any new match, 3 when a search did not finish, else 0."""
    data = load(repo)
    mons = [m for m in data["monitors"] if only is None or m["id"] == only]
    if only is not None and not mons:
        raise MonitorError(f"no monitor {only!r}")
    out = []
    for m in mons:
        got = matches(repo, m)
        base = Counter(m.get("baseline") or [])
        new, left = [], Counter(base)
        for r in sorted(got["matches"], key=lambda r: (r["at"].rpartition(":")[0],
                                                     int(r["at"].rpartition(":")[2] or 0))):
            k = _key(r["at"].rpartition(":")[0], r["text"])
            if left[k] > 0:
                left[k] -= 1
            else:
                new.append({**r, "status": "statically_verified"})
        gone = sorted(left.elements()) if got["complete"] else []
        out.append({"id": m["id"], "kind": "regex" if "regex" in m else "ast", "pattern": m.get("regex", m.get("ast")),
                    "message": m.get("message", ""), "matches": len(got["matches"]), "baseline": sum(base.values()),
                    "new": new, "gone": [{"file": g.split("\t", 1)[0], "text": g.split("\t", 1)[1]} for g in gone],
                    "complete": got["complete"], **({"why": got["why"]} if got.get("why") else {})})
    exit_code = 1 if any(r["new"] for r in out) else 3 if any(not r["complete"] for r in out) else 0
    return {"file": FILE, "monitors": out, "exit": exit_code}


def trend(repo: Path, mid: str, *, points: int = 10) -> dict:
    """A regex monitor's match count at up to ``points`` commits spread over the first-parent history of HEAD."""
    from verinoda.snapshot import git

    data = load(repo)
    m = next((x for x in data["monitors"] if x["id"] == mid), None)
    if m is None:
        raise MonitorError(f"no monitor {mid!r}")
    if "regex" not in m:
        raise MonitorError("a trend is counted for regex monitors only (an ast pattern needs every file parsed at "
                           "every commit)")
    log = git(Path(repo), "log", "--first-parent", "--format=%H%x1f%cI", "HEAD", timeout=60)
    if not log:
        raise MonitorError("no git history")
    commits = [ln.split("\x1f") for ln in log.splitlines() if "\x1f" in ln][::-1]   # oldest first
    points = max(2, min(int(points), 50))
    step = max(1, (len(commits) - 1) / (points - 1)) if len(commits) > 1 else 1
    picks = sorted({min(len(commits) - 1, round(i * step)) for i in range(points)})
    rows = []
    for i in picks:
        sha, when = commits[i]
        rc, out, err = _git_grep(Path(repo), ["-c", "-I", *_grep_args(m)[:3], sha, "--", *(m.get("paths") or ["."])])
        if rc not in (0, 1):
            raise MonitorError(f"git grep failed at {sha[:12]}: {err.strip()[:200]}")
        n = sum(int(ln.rpartition(":")[2]) for ln in (out or "").splitlines() if ln.rpartition(":")[2].isdigit())
        rows.append({"commit": sha[:12], "date": when[:10], "count": n})
    return {"id": mid, "pattern": m["regex"], "points": rows,
            "method": "git grep -c -E at each commit (first-parent history of HEAD, spread evenly)"}


def render(res: dict) -> str:
    out = [f"monitors ({res['file']}): {len(res['monitors'])}"]
    for r in res["monitors"]:
        head = (f"  {r['id']} [{r['kind']}] {r['matches']} match(es), baseline {r['baseline']}: "
                + (f"{len(r['new'])} NEW" if r["new"] else "no new match")
                + (f", {len(r['gone'])} gone" if r["gone"] else "")
                + ("" if r["complete"] else f" - search not finished ({r.get('why')}): unknown"))
        out.append(head)
        for n in r["new"][:20]:
            out.append(f"    NEW {n['at']}: {n['text']}" + (f"  ({r['message']})" if r["message"] else ""))
        if len(r["new"]) > 20:
            out.append(f"    ... {len(r['new']) - 20} more (--json)")
    return "\n".join(out)
