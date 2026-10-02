"""Mine localization tasks for the big-repository study (DESIGN_BIG.md) from GitHub, and select the final set.

A task is a real bug report (an issue filed before the pinned commit) that a pull request merged after it closed; the
gold is the non-test source files that pull request changed (that exist at the pinned commit). Nothing is written by
a model. Needs a GitHub token from `git credential fill`, never printed.

    python benchmarks/agent_compare/big_tasks.py mine   CONFIG.json CANDIDATES.json
    python benchmarks/agent_compare/big_tasks.py select CONFIG.json CANDIDATES.json TASKS.json

CONFIG: {"repo": "owner/name", "base": local clone at the pinned commit, "sha": pinned commit, "pinned_at": ISO time,
"until": ISO date, "source_prefix": "homeassistant/", "seed": int, "want": 30, "per_group_max": 2, ...}
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

CLOSES = re.compile(r"(?:fix(?:e[sd])?|close[sd]?|resolve[sd]?)\s*:?\s*(?:https?://github\.com/[\w.-]+/[\w.-]+/issues/|#)(\d+)",
                    re.IGNORECASE)
SKIP_TITLE = re.compile(r"^(bump|update translations|\[?dependabot|revert|merge|release)", re.IGNORECASE)


def token() -> str:
    out = subprocess.run(["git", "credential", "fill"], input="protocol=https\nhost=github.com\n\n", capture_output=True,
                         text=True, check=True).stdout
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)["password"]


class Api:
    def __init__(self) -> None:
        self.tok = token()

    def get(self, path: str, retries: int = 4):
        url = "https://api.github.com/" + path.lstrip("/")
        for i in range(retries):
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.tok}", "User-Agent": "big-tasks",
                                                       "Accept": "application/vnd.github+json"})
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    return json.loads(r.read())
            except urllib.error.HTTPError as exc:
                if exc.code in (403, 429) and i + 1 < retries:
                    time.sleep(30 * (i + 1))
                    continue
                if exc.code == 404:
                    return None
                raise
            except OSError:
                time.sleep(5 * (i + 1))
        raise RuntimeError(url)


def group_of(path: str, prefix: str) -> str:
    """The integration (or top directory) a path belongs to: homeassistant/components/<name>/... -> <name>."""
    parts = path[len(prefix):].split("/")
    return parts[1] if parts[0] == "components" and len(parts) > 2 else parts[0]


def leaks(issue_text: str, gold: list[str]) -> bool:
    """The issue names a gold file by its repository path, or by its path under the package."""
    low = issue_text.lower()
    return any(g.lower() in low or "/".join(g.split("/")[-3:]).lower() in low for g in gold)


def mine(cfg: dict, out: Path) -> None:
    api = Api()
    seen: dict[int, dict] = {}
    start = cfg["pinned_at"][:10]
    days = []
    d0 = time.strptime(start, "%Y-%m-%d")
    t = time.mktime(d0)
    end = time.mktime(time.strptime(cfg["until"], "%Y-%m-%d"))
    while t <= end:
        days.append(time.strftime("%Y-%m-%d", time.localtime(t)))
        t += 86400 * 3
    for a in days:
        b = time.strftime("%Y-%m-%d", time.localtime(time.mktime(time.strptime(a, "%Y-%m-%d")) + 86400 * 2))
        for page in range(1, 11):
            q = urllib.parse.quote(f"repo:{cfg['repo']} is:pr is:merged merged:{a}..{b} linked:issue")
            res = api.get(f"search/issues?q={q}&per_page=100&page={page}&sort=created&order=asc")
            time.sleep(2.2)
            for it in (res or {}).get("items", []):
                seen[it["number"]] = it
            if len((res or {}).get("items", [])) < 100:
                break
        print(a, len(seen), flush=True)
    cands = []
    for n, it in sorted(seen.items()):
        if SKIP_TITLE.search(it["title"]):
            continue
        pr = api.get(f"repos/{cfg['repo']}/pulls/{n}")
        if not pr or not pr.get("merged_at") or pr["merged_at"] <= cfg["pinned_at"]:
            continue
        refs = {int(x) for x in CLOSES.findall(pr.get("body") or "")}
        if len(refs) != 1:  # one issue per task
            continue
        files = []
        for page in range(1, 4):
            chunk = api.get(f"repos/{cfg['repo']}/pulls/{n}/files?per_page=100&page={page}") or []
            files += chunk
            if len(chunk) < 100:
                break
        src = [f for f in files if f["filename"].startswith(cfg["source_prefix"]) and f["filename"].endswith(".py")
               and f["status"] in ("modified", "removed")]
        issue_no = next(iter(refs))
        issue = api.get(f"repos/{cfg['repo']}/issues/{issue_no}")
        if not issue or "pull_request" in issue or issue["created_at"] >= cfg["pinned_at"]:
            continue
        cands.append({"pr": n, "pr_title": pr["title"], "merged_at": pr["merged_at"], "issue": issue_no,
                      "issue_title": issue["title"], "issue_body": issue.get("body") or "",
                      "issue_created": issue["created_at"], "labels": [x["name"] for x in issue.get("labels", [])],
                      "changed_files": len(files), "gold": [f["filename"] for f in src]})
        if len(cands) % 25 == 0:
            print("candidates", len(cands), flush=True)
    out.write_bytes((json.dumps(cands, indent=1, ensure_ascii=False) + "\n").encode("utf-8"))
    print(len(cands), "candidates")


def at_pinned(base: str, sha: str, path: str) -> bool:
    return subprocess.run(["git", "-C", base, "cat-file", "-e", f"{sha}:{path}"], capture_output=True, check=False).returncode == 0


def select(cfg: dict, cands: list[dict]) -> tuple[list[dict], dict]:
    """The pre-registered filters, then a seeded order with at most ``per_group_max`` tasks per integration."""
    why: dict[str, list[int]] = {}

    def drop(reason: str, c: dict) -> None:
        why.setdefault(reason, []).append(c["pr"])

    ok = []
    for c in cands:
        text = f"{c['issue_title']}\n{c['issue_body']}"
        gold = [g for g in c["gold"] if at_pinned(cfg["base"], cfg["sha"], g)]
        if not 1 <= len(gold) <= cfg.get("gold_max", 4):
            drop("gold files not 1-4 at the pinned commit", c)
        elif c["changed_files"] > cfg.get("changed_max", 12):
            drop("more than 12 files changed", c)
        elif not cfg.get("issue_min", 150) <= len(c["issue_body"]) <= cfg.get("issue_max", 4000):
            drop("issue body length outside 150-4000", c)
        elif leaks(text, gold):
            drop("the issue names a gold file's path", c)
        else:
            ok.append({**c, "gold": gold})
    ok.sort(key=lambda c: hashlib.sha256(f"{cfg['seed']}:{c['pr']}".encode()).hexdigest())
    per: dict[str, int] = {}
    tasks = []
    for c in ok:
        groups = {group_of(g, cfg["source_prefix"]) for g in c["gold"]}
        if any(per.get(g, 0) >= cfg.get("per_group_max", 2) for g in groups):
            drop("integration already has two tasks", c)
            continue
        for g in groups:
            per[g] = per.get(g, 0) + 1
        tasks.append({"id": f"pr{c['pr']}", "pr": c["pr"], "issue": c["issue"], "title": c["issue_title"],
                      "body": c["issue_body"], "gold": c["gold"], "groups": sorted(groups), "labels": c["labels"],
                      "issue_created": c["issue_created"], "merged_at": c["merged_at"]})
        if len(tasks) == cfg["want"]:
            break
    return tasks, {"candidates": len(cands), "passed_filters": len(ok), "dropped": {k: len(v) for k, v in why.items()}}


def main() -> int:
    mode, cfg_p, a = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
    cfg = json.loads(cfg_p.read_text(encoding="utf-8"))
    if mode == "mine":
        mine(cfg, a)
    else:
        tasks, info = select(cfg, json.loads(a.read_text(encoding="utf-8")))
        Path(sys.argv[4]).write_bytes((json.dumps({"filters": info, "tasks": tasks}, indent=1, ensure_ascii=False) + "\n").encode("utf-8"))
        print(info, len(tasks), "tasks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
