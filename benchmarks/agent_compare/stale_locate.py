"""What a stale index does to `verinoda locate` (no model is run): the same report text asked of a task's copy as indexed
at the base commit and of a copy of it whose working tree has moved on by K commits while its index has not.

Per task: the files each lists, how much they overlap, how many of the stale copy's files are no longer in its tree, whether
the stale answer says the index is behind (`freshness.stale_count`), and how many of the task's gold files each lists.

    python benchmarks/agent_compare/stale_locate.py CONFIG.json OUT.json

CONFIG: {"tasks": tasks.json, "fresh": "<folder with {task}>" (a copy at the base commit with its index),
"stale": "<folder with {task}>" (made here: a copy of `fresh`), "full": a full clone holding the later commits,
"verinoda": the verinoda executable, "ahead": K (default 100), "only": [task ids] or null}
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from guard_run import git_env


def git(repo: str, *args: str, check: bool = True) -> str:
    p = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                       env=git_env(), check=False)
    if check and p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} in {repo}: {p.stderr.strip()[:200]}")
    return p.stdout.strip()


def descendant(full: str, base: str, k: int) -> str | None:
    """The k-th commit after ``base`` on the way to the clone's HEAD (oldest first), or None when there are fewer."""
    out = git(full, "rev-list", "--reverse", "--ancestry-path", f"{base}..HEAD").split()
    return out[k - 1] if len(out) >= k else None


def locate(verinoda: str, repo: str, text: str) -> dict:
    p = subprocess.run([verinoda, "locate", "--repo", repo, "--json", "--max-files", "8", "--", text], capture_output=True,
                       text=True, encoding="utf-8", errors="replace", check=False, env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")})
    try:
        return json.loads(p.stdout)
    except ValueError:
        return {"files": [], "error": (p.stderr or p.stdout)[-300:]}


def listed(res: dict) -> list[str]:
    return [f["path"] for f in res.get("files") or []]


def one(cfg: dict, task: dict) -> dict:
    fresh, stale = cfg["fresh"].format(task=task["id"]), cfg["stale"].format(task=task["id"])
    target = descendant(cfg["full"], task["base_sha"], cfg.get("ahead", 100))
    if target is None:
        return {"id": task["id"], "skipped": f"fewer than {cfg.get('ahead', 100)} commits after the base"}
    if not (Path(stale) / ".git").exists():
        shutil.copytree(fresh, stale, ignore=shutil.ignore_patterns("locate-daemon.*"), dirs_exist_ok=False)
    if git(stale, "rev-parse", "HEAD") != target:  # a copy whose checkout never ran (a rerun after a crash) is not stale
        git(stale, "fetch", "-q", cfg["full"], target)
        git(stale, "checkout", "-q", "-f", target)
    text = f"{task['title']}\n{task['body']}"[:3000]
    a, b = locate(cfg["verinoda"], fresh, text), locate(cfg["verinoda"], stale, text)
    fa, fb = listed(a), listed(b)
    gold = set(task["gold"])
    union = set(fa) | set(fb)
    return {"id": task["id"], "ahead": cfg.get("ahead", 100), "changed_files": len(git(stale, "diff", "--name-only", task["base_sha"], target).splitlines()),
            "fresh": fa, "stale": fb, "overlap": round(len(set(fa) & set(fb)) / len(union), 3) if union else 1.0,
            "gone_from_the_tree": [p for p in fb if not (Path(stale) / p).exists()],
            "says_behind": bool((b.get("freshness") or {}).get("stale_count")), "stale_count": (b.get("freshness") or {}).get("stale_count", 0),
            "gold_fresh": sorted(gold & set(fa)), "gold_stale": sorted(gold & set(fb)), "gold": len(gold)}


def main() -> int:
    cfg = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    tasks = json.loads(Path(cfg["tasks"]).read_text(encoding="utf-8"))["tasks"]
    rows = [one(cfg, t) for t in tasks if not cfg.get("only") or t["id"] in cfg["only"]]
    done = [r for r in rows if "skipped" not in r]
    summary = {"tasks": len(done), "skipped": len(rows) - len(done), "ahead": cfg.get("ahead", 100),
               "mean_overlap": round(sum(r["overlap"] for r in done) / len(done), 3) if done else None,
               "listed_files": sum(len(r["stale"]) for r in done), "gone_from_the_tree": sum(len(r["gone_from_the_tree"]) for r in done),
               "tasks_saying_behind": sum(r["says_behind"] for r in done),
               "gold": sum(r["gold"] for r in done), "gold_listed_fresh": sum(len(r["gold_fresh"]) for r in done),
               "gold_listed_stale": sum(len(r["gold_stale"]) for r in done)}
    Path(sys.argv[2]).write_bytes((json.dumps({"summary": summary, "rows": rows}, indent=1) + "\n").encode("utf-8"))
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
