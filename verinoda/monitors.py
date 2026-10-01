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
checked out). A search that could not finish (a timeout, a cut list, a file too large or unreadable, a path
that matches no file, a git error) makes the monitor ``unknown`` (exit 3): a gate never passes on a search it did
not complete. The monitors file itself is never searched (it holds the patterns and the baselines' text).

``--path`` values are project-relative POSIX paths, read literally (no globs, no pathspec magic) by both kinds of
search; ``load`` refuses a file whose fields have the wrong shape, since CI reads it.
"""
from __future__ import annotations

import json
import os
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
    seen = set()
    for m in data["monitors"]:
        problem = _shape_problem(m)
        if problem:
            raise MonitorError(f"{FILE}: {problem}: {m!r}"[:300])
        if m["id"] in seen:
            raise MonitorError(f"{FILE}: two monitors are called {m['id']!r}")
        seen.add(m["id"])
    return data


def _strs(v) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


def _shape_problem(m) -> str | None:
    if not isinstance(m, dict):
        return "a monitor is not an object"
    if not isinstance(m.get("id"), str) or not _ID.match(m["id"]):
        return "a monitor's id is missing or not letters, digits, '.', '_' or '-'"
    if ("regex" in m) == ("ast" in m) or not isinstance(m.get("regex", m.get("ast")), str):
        return f"monitor {m['id']} needs exactly one of regex or ast, as a string"
    for f in ("langs", "paths", "baseline"):
        if f in m and not _strs(m[f]):
            return f"monitor {m['id']}: {f} is not a list of strings"
    if any("\t" not in b for b in m.get("baseline") or []):
        return f"monitor {m['id']}: a baseline entry is not 'file<TAB>text'"
    for x in m.get("paths") or []:
        bad = _path_problem(x)
        if bad:
            return f"monitor {m['id']}: {bad}"
    if "message" in m and not isinstance(m["message"], str):
        return f"monitor {m['id']}: message is not a string"
    return None


def _path_problem(x: str) -> str | None:
    if not x or x != x.strip() or x.startswith((":", "/")) or "\\" in x or re.match(r"^[A-Za-z]:", x):
        return f"path {x!r} is not a project-relative path with '/' (no pathspec magic)"
    if ".." in x.split("/") or x.startswith("./") or x.endswith("/") or "//" in x:
        return f"path {x!r} is not a normalised project-relative path"
    return None


def norm_path(repo: Path, x: str, cwd: Path | None = None) -> str:
    """A ``--path`` as given (relative to ``cwd``, default the project's top) -> a project-relative POSIX path;
    a path outside the project is an error."""
    repo = Path(repo).resolve()
    q = Path(x)
    full = (q if q.is_absolute() else Path(cwd or repo) / q).resolve()
    try:
        rel = full.relative_to(repo).as_posix()
    except ValueError:
        raise MonitorError(f"--path {x!r} is outside the project") from None
    return "" if rel == "." else rel


def _save(repo: Path, data: dict) -> None:
    """Write the file whole or not at all (a temporary file replaced in one step)."""
    target = path(repo)
    tmp = target.with_name(f".{FILE}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
        os.replace(tmp, target)
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise MonitorError(f"{FILE} cannot be written: {exc}") from None


def _key(rel: str, text: str) -> str:
    return f"{rel}\t{' '.join(text.split())}"


def _pathspecs(m: dict) -> list[str]:
    """The monitor's paths, literal (a directory covers what is under it), never the monitors file."""
    paths = [p for p in m.get("paths") or [] if p]
    return [*([f":(literal){p}" for p in paths] or ["."]), f":(exclude,literal){FILE}"]


def _grep_args(m: dict) -> list[str]:
    return ["-E", "-e", m["regex"], "--", *_pathspecs(m)]


def _missing_paths(repo: Path, m: dict) -> list[str]:
    return [p for p in m.get("paths") or [] if p and not (Path(repo) / p).exists()]


def matches(repo: Path, m: dict) -> dict:
    """The monitor's matches in the working tree: ``{"matches": [{at, text}], "complete": bool, "why"}``."""
    repo = Path(repo)
    missing = _missing_paths(repo, m)
    if missing:
        return {"matches": [], "complete": False, "why": f"no such path: {', '.join(missing)[:200]}"}
    if m.get("regex") is not None:
        rc, rows, err = _git_grep_rows(repo, ["-n", "-z", "-I", "--no-color", "--no-column", "--untracked",
                                              *_grep_args(m)])
        if rc == 1:            # git grep: 0 matches found, 1 none, anything else an error
            return {"matches": [], "complete": True}
        if rc == -1:
            return {"matches": rows, "complete": False, "why": f"more than {MAX_MATCHES} matches"}
        if rc != 0:
            return {"matches": [], "complete": False, "why": f"git grep failed: {err.strip()[:200]}"}
        return {"matches": rows, "complete": True}
    from verinoda import grep_ast

    try:
        from verinoda.snapshot import list_files

        # the files as they are now (not the few-seconds listing cache: a gate reads the tree it is run on)
        res = grep_ast.run(repo, m["ast"], langs=m.get("langs") or None, paths=m.get("paths") or None,
                           max_results=MAX_MATCHES, files=list_files(repo))
    except grep_ast.PatternError as exc:
        return {"matches": [], "complete": False, "why": f"the pattern cannot be searched: {exc}"}
    rows, cache = [], {}
    for x in res["matches"]:
        rel, _, n = x["at"].rpartition(":")
        if rel == FILE:
            continue
        if rel not in cache:
            cache[rel] = _file_lines(repo, rel)
        lines = cache[rel]
        k = int(n) if n.isdigit() else 0
        rows.append({"at": x["at"], "text": (lines[k - 1].strip() if 0 < k <= len(lines) else x["text"])[:200]})
    ns = res.get("not_searched") or {}
    why = [w for w, bad in (
        ("a cut list", res.get("truncated")),
        (f"{len(ns.get('timed_out') or [])} file(s) timed out", ns.get("timed_out")),
        (f"{ns.get('budget_spent')} file(s) not searched in the time budget", ns.get("budget_spent")),
        (f"{ns.get('too_large')} file(s) too large", ns.get("too_large")),
        (f"{len(ns.get('unreadable') or [])} file(s) unreadable", ns.get("unreadable")),
        (f"the pattern does not parse as {', '.join(sorted(ns.get('pattern_not_parsed') or {}))}",
         m.get("langs") and ns.get("pattern_not_parsed"))) if bad]
    return {"matches": rows, "complete": not why, **({"why": "; ".join(why)} if why else {})}


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


def _git_grep_rows(repo: Path, args: list[str], timeout: float = 120) -> tuple[int, list[dict], str]:
    """``git grep -n -z`` read line by line: (exit code, rows, stderr). Stops at :data:`MAX_MATCHES` + 1 rows
    (exit code -1, git killed) so a pattern matching everything never fills memory; 2 on a timeout."""
    import subprocess
    import threading

    from verinoda.treestate import _GIT_SAFE

    try:
        proc = subprocess.Popen(["git", "-C", str(repo), *_GIT_SAFE, "grep", *args], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
    except OSError as exc:
        return 2, [], str(exc)
    timer = threading.Timer(timeout, proc.kill)
    timer.start()
    err_chunks: list[bytes] = []
    reader = threading.Thread(target=lambda: err_chunks.append(proc.stderr.read()), daemon=True)
    reader.start()
    rows, cut = [], False
    try:
        for raw in proc.stdout:
            rel, n, text = (raw.rstrip(b"\r\n").split(b"\0", 2) + [b"", b""])[:3]
            if not n.isdigit():
                continue
            if len(rows) >= MAX_MATCHES:
                cut = True
                proc.kill()
                break
            rows.append({"at": f"{rel.decode('utf-8', 'replace')}:{int(n)}",
                         "text": text.decode("utf-8", "replace").strip()[:200]})
    finally:
        proc.stdout.close()
        rc = proc.wait()
        timed_out = not timer.is_alive() and not cut
        timer.cancel()
        reader.join(5)
    err = b"".join(err_chunks).decode("utf-8", "replace")
    if cut:
        return -1, rows, err
    if timed_out and rc != 0:
        return 2, [], f"timed out after {timeout:.0f} s"
    return rc, rows, err


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
    paths = [p for p in (paths or []) if p]
    for x in paths:
        bad = _path_problem(x)
        if bad:
            raise MonitorError(bad)
        if not (Path(repo) / x).exists():
            raise MonitorError(f"no such path: {x}")
    if regex is not None and not regex:
        raise MonitorError("the regex is empty")
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
        raise MonitorError("git log gave no history (no commit yet, or git failed)")
    commits = [ln.split("\x1f") for ln in log.splitlines() if "\x1f" in ln][::-1]   # oldest first
    points = max(2, min(int(points), 50))
    step = max(1, (len(commits) - 1) / (points - 1)) if len(commits) > 1 else 1
    picks = sorted({min(len(commits) - 1, round(i * step)) for i in range(points)})
    rows = []
    for i in picks:
        sha, when = commits[i]
        rc, out, err = _git_grep(Path(repo), ["-c", "-I", "-E", "-e", m["regex"], sha, "--", *_pathspecs(m)])
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
        for g in r["gone"][:10]:
            out.append(f"    gone {g['file']}: {g['text']}")
        if len(r["gone"]) > 10:
            out.append(f"    ... {len(r['gone']) - 10} more gone (--json)")
    return "\n".join(out)
