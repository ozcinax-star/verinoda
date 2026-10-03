"""Offline oracle of the signals a tool could give an agent (the options study, X0): for every file the agent alone
missed in a task, would a signal computed from what the agent had in hand have listed it? No session is run.

The anchors of a session are the files it named. A signal turns them (and the issue text) into a ranked list of
candidate files; the oracle asks whether a missed gold file is among the first k. Signals: files in the anchors'
folders; files of the same name elsewhere (twins); a `.c`/`.h` partner of the same stem; the files an anchor includes
or imports and the files that include or import it; the files changed together with the anchors in the git history
(Verinoda's own reading of the last 1000 commits of HEAD, and a reading limited to the anchors' own commits); Verinoda's
`query` on the issue text.

    python benchmarks/agent_compare/signal_oracle.py CONFIG.json OUT.json

CONFIG: {"tasks": tasks.json, "none_results": [results.jsonl, ...], "history_repo": "<template with {task}> or a path",
"query_copy": "<template with {task}> or a path, an indexed copy to run `verinoda query` on", "limit_query_tasks": [ids] or null,
"k": [3, 5, 8]}
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

C_INCLUDE = re.compile(r'^\s*#\s*include\s*[<"]([^>"]+)[>"]', re.MULTILINE)
PY_IMPORT = re.compile(r"^\s*(?:from\s+(\.*[\w.]*)\s+import\s+([\w, ()*]+)|import\s+([\w., ]+))", re.MULTILINE)
CODE_SUFFIXES = (".c", ".h", ".S", ".py", ".cpp", ".hpp", ".cc", ".cmake", ".bf", ".rs", ".go", ".js", ".ts")


def git(repo: str, *args: str) -> str:
    p = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                       check=False, env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")})
    return p.stdout


def dirname(p: str) -> str:
    return p.rsplit("/", 1)[0] if "/" in p else ""


def basename(p: str) -> str:
    return p.rsplit("/", 1)[-1]


def stem(p: str) -> str:
    return basename(p).rsplit(".", 1)[0]


class Repo:
    """A checkout at the base commit, with what the signals read from it (cached)."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.files = [f for f in git(path, "ls-files").splitlines() if f]
        self.fileset = set(self.files)
        self.by_base: dict[str, list[str]] = defaultdict(list)
        self.by_stem: dict[str, list[str]] = defaultdict(list)
        self.by_dir: dict[str, list[str]] = defaultdict(list)
        for f in self.files:
            self.by_base[basename(f)].append(f)
            self.by_stem[stem(f)].append(f)
            self.by_dir[dirname(f)].append(f)
        self._text: dict[str, str] = {}
        self._includes: dict[str, list[str]] | None = None

    def text(self, f: str) -> str:
        if f not in self._text:
            try:
                self._text[f] = (Path(self.path) / f).read_text(encoding="utf-8", errors="replace")
            except OSError:
                self._text[f] = ""
        return self._text[f]

    def includes(self) -> dict[str, list[str]]:
        """file -> the files it includes (C family, by path suffix: an ambiguous include lists every match)."""
        if self._includes is None:
            self._includes = {}
            for f in self.files:
                if f.endswith((".c", ".h", ".S", ".cpp", ".hpp", ".cc")):
                    got = []
                    for inc in C_INCLUDE.findall(self.text(f)):
                        got += [g for g in self.by_base.get(basename(inc), []) if g == inc or g.endswith("/" + inc)]
                    self._includes[f] = sorted(set(got))
        return self._includes


def py_imports(repo: Repo, f: str) -> list[str]:
    """Files of the same folder a Python file imports relatively (`from .x import`, `from . import x`)."""
    out = []
    d = dirname(f)
    for frm, names, _plain in PY_IMPORT.findall(repo.text(f)):
        if frm.startswith("."):
            mod = frm.lstrip(".")
            if mod:
                out += [g for g in repo.files if g == f"{d}/{mod.replace('.', '/')}.py" or g == f"{d}/{mod.replace('.', '/')}/__init__.py"]
            else:
                for n in re.split(r"[,\s()]+", names):
                    if n and f"{d}/{n}.py" in repo.fileset:
                        out.append(f"{d}/{n}.py")
    return out


def neighbors(repo: Repo, anchors: list[str]) -> list[str]:
    inc = repo.includes()
    forward, backward = Counter(), Counter()
    for a in anchors:
        for g in inc.get(a, []):
            forward[g] += 1
        for g in py_imports(repo, a):
            forward[g] += 1
        # who includes / imports the anchor
        for g, targets in inc.items():
            if a in targets:
                backward[g] += 1
        d = dirname(a)
        if a.endswith(".py"):
            for g in repo.by_dir.get(d, []):
                if g != a and g.endswith(".py") and a in py_imports(repo, g):
                    backward[g] += 1
    ranked = [f for f, _ in (forward + backward).most_common()]
    return [f for f in ranked if f not in anchors]


def cochange_path(repo: Repo, anchors: list[str], commits: int = 300, min_shared: int = 2, min_degree: float = 0.15) -> list[str]:
    """Files changed together with an anchor in the anchor's own last commits (path-limited, so a busy repository's
    thousands of unrelated commits do not crowd the window); bulk commits (over 30 files) left out."""
    score: Counter = Counter()
    for a in anchors:
        # two steps: a pathspec also limits the files `--name-only` lists, so the commits come first and their whole
        # file lists second
        hashes = git(repo.path, "log", "--no-merges", f"-n{commits}", "--format=%H", "--", a).split()
        if not hashes:
            continue
        out = git(repo.path, "log", "--no-walk=unsorted", "--no-merges", "--format=%x01", "--name-only", *hashes)
        commit_files = [{x.strip() for x in blk.splitlines() if x.strip()} for blk in out.split("\x01") if blk.strip()]
        commit_files = [c for c in commit_files if len(c) <= 30]
        if len(commit_files) < min_shared:
            continue
        shared: Counter = Counter()
        for c in commit_files:
            for f in c:
                if f != a:
                    shared[f] += 1
        for f, n in shared.items():
            if n >= min_shared and n / len(commit_files) >= min_degree and f in repo.fileset:
                score[f] += n / len(commit_files)
    return [f for f, _ in score.most_common() if f not in anchors]


def cochange_verinoda(repo: Repo, anchors: list[str]) -> list[str]:
    """Verinoda's own reading (history.co_changes: the last 1000 commits of HEAD, 3 shared commits, 30 % of the target's)."""
    from verinoda import history
    res = history.co_changes(Path(repo.path), anchors)
    return [c.get("file") or c.get("path") for c in res.get("coupled", []) if (c.get("file") or c.get("path"))]


def same_dir(repo: Repo, anchors: list[str]) -> list[str]:
    dirs = {dirname(a) for a in anchors}
    return sorted(f for d in dirs for f in repo.by_dir.get(d, []) if f not in anchors)


def twins(repo: Repo, anchors: list[str]) -> list[str]:
    return sorted({g for a in anchors for g in repo.by_base.get(basename(a), []) if g != a})


def pairs(repo: Repo, anchors: list[str]) -> list[str]:
    return sorted({g for a in anchors for g in repo.by_stem.get(stem(a), []) if g != a and g.endswith(CODE_SUFFIXES)})


def retrieval(query_repo: str, text: str, k: int = 12) -> list[str]:
    p = subprocess.run(["verinoda", "query", text, "--repo", query_repo, "--json", "--max-chars", "8000"], capture_output=True,
                       text=True, encoding="utf-8", errors="replace", check=False)
    try:
        items = json.loads(p.stdout).get("items") or []
    except ValueError:
        return []
    out: list[str] = []
    for it in items:
        f = it.get("file")
        if f and f not in out:
            out.append(f)
    return out[:k]


def main() -> int:
    cfg = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    tasks = {t["id"]: t for t in json.loads(Path(cfg["tasks"]).read_text(encoding="utf-8"))["tasks"]}
    runs = [[json.loads(x) for x in Path(p).read_text(encoding="utf-8").splitlines() if x.strip()] for p in cfg["none_results"]]
    ks = cfg.get("k", [3, 5, 8])
    repos: dict[str, Repo] = {}
    queries: dict[str, list[str]] = {}
    per: list[dict] = []
    for ri, run in enumerate(runs, 1):
        for r in run:
            if r["arm"] != "none" or r["id"] not in tasks:
                continue
            t = tasks[r["id"]]
            gold = t["gold"]
            named = r["files"]
            missed = [g for g in gold if g not in named]
            if not missed:
                continue
            hist = cfg["history_repo"].format(task=t["id"])
            if hist not in repos:
                repos[hist] = Repo(hist)
            repo = repos[hist]
            anchors = [f for f in named if f in repo.fileset] or []
            sig = {"samedir": same_dir(repo, anchors), "twins": twins(repo, anchors), "pair": pairs(repo, anchors),
                   "neighbors": neighbors(repo, anchors), "cochange_path": cochange_path(repo, anchors),
                   "cochange_verinoda": cochange_verinoda(repo, anchors) if anchors else []}
            if t["id"] not in queries and (cfg.get("limit_query_tasks") in (None, []) or t["id"] in cfg["limit_query_tasks"]):
                queries[t["id"]] = retrieval(cfg["query_copy"].format(task=t["id"]), f"{t['title']}\n{t['body']}"[:3000])
            sig["query"] = [f for f in queries.get(t["id"], []) if f not in anchors]
            row = {"task": t["id"], "run": ri, "gold": gold, "named": named, "anchors": anchors, "missed": missed, "signals": {}}
            for name, cand in sig.items():
                row["signals"][name] = {"size": len(cand), **{f"cover@{k}": [m for m in missed if m in cand[:k]] for k in ks}}
            union = []
            for name in ("cochange_path", "twins", "pair", "neighbors", "query"):
                union += [f for f in sig[name][:5] if f not in union]
            row["signals"]["union5"] = {"size": len(union), **{f"cover@{k}": [m for m in missed if m in union] for k in ks}}
            per.append(row)
    names = list(per[0]["signals"]) if per else []
    total = sum(len(r["missed"]) for r in per)
    summary = {"missed_gold_instances": total, "sessions_with_a_miss": len(per), "signals": {}}
    for n in names:
        summary["signals"][n] = {"mean_list_size": round(sum(r["signals"][n]["size"] for r in per) / len(per), 1),
                                 **{f"covers@{k}": sum(len(r["signals"][n][f"cover@{k}"]) for r in per) for k in ks}}
    Path(sys.argv[2]).write_bytes((json.dumps({"summary": summary, "rows": per}, indent=1) + "\n").encode("utf-8"))
    print(f"{len(per)} sessions of `none` with a miss, {total} missed gold files (counted per session)")
    print(f"{'signal':20s} {'mean list':>9s} " + " ".join(f"{'@' + str(k):>6s}" for k in ks))
    for n, v in summary["signals"].items():
        print(f"{n:20s} {v['mean_list_size']:9.1f} " + " ".join(f"{100 * v[f'covers@{k}'] / total:5.0f}%" for k in ks))
    return 0


if __name__ == "__main__":
    sys.exit(main())
