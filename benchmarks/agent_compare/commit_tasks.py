"""Mine localization tasks for a repository with few issues (DESIGN_SEL4.md): every task is a real bug report with its
own base commit, the repository as it was just before the change that closed it (a pull request's base, or a commit's
parent), so no index can be shared between tasks. Nothing is written by a model.

A task: an issue that a merged pull request closes by keyword (`Fixes #N`), or that a commit message on any branch of the
clone closes the same way; the gold is the code files that change modified or removed, as they are at the base commit.

    python benchmarks/agent_compare/commit_tasks.py mine   CONFIG.json CANDIDATES.json
    python benchmarks/agent_compare/commit_tasks.py select CONFIG.json CANDIDATES.json TASKS.json

CONFIG: {"repo": "owner/name", "clone": a full local clone, "seed": int, "want": n, "gold_max": 4, "changed_max": 12,
"issue_min": 150, "issue_max": 4000}. Needs a GitHub token from `git credential fill`, never printed.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from big_tasks import Api, leaks

# `Fixes #N`, `closes https://github.com/o/r/issues/N`, and the cross-repository spelling `Resolves o/r#N`
CLOSES = re.compile(r"(?:fix(?:e[sd])?|close[sd]?|resolve[sd]?)\s*:?\s*(?:https?://github\.com/[\w.-]+/[\w.-]+/issues/|(?:[\w.-]+/[\w.-]+)?#)(\d+)",
                    re.IGNORECASE)

CODE_SUFFIXES = {"c", "h", "s", "py", "bf", "xml", "xsd", "cmake", "dts", "pbf", "lds", "in"}
NOT_CODE = ("tests/", "test/", "manual/", "docs/", "doc/", ".github/")


def is_gold_path(path: str) -> bool:
    """A code or build file (not a test, a document or a CI file)."""
    low = path.lower()
    if low.startswith(NOT_CODE) or "/tests/" in low or low.endswith(".md"):
        return False
    return path.rsplit("/", 1)[-1] == "CMakeLists.txt" or low.rsplit(".", 1)[-1] in CODE_SUFFIXES


def git(clone: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", clone, *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                          check=False)


def closed_issues(api: Api, repo: str) -> list[dict]:
    out = []
    for page in range(1, 30):
        items = api.get(f"repos/{repo}/issues?state=closed&per_page=100&page={page}") or []
        out += [i for i in items if "pull_request" not in i]
        if len(items) < 100:
            break
    return out


def by_pull_request(api: Api, repo: str, issue: dict) -> dict | None:
    n = issue["number"]
    seen = {}
    for e in api.get(f"repos/{repo}/issues/{n}/timeline?per_page=100") or []:
        src = (e.get("source") or {}).get("issue") or {}
        if e.get("event") == "cross-referenced" and src.get("pull_request") and (src.get("repository_url") or "").endswith("/" + repo):
            seen[src["number"]] = 1
    for pn in seen:
        pr = api.get(f"repos/{repo}/pulls/{pn}")
        if not pr or not pr.get("merged_at") or n not in {int(x) for x in CLOSES.findall((pr.get("body") or "") + "\n" + pr["title"])}:
            continue
        files: list[dict] = []
        for page in range(1, 4):
            chunk = api.get(f"repos/{repo}/pulls/{pn}/files?per_page=100&page={page}") or []
            files += chunk
            if len(chunk) < 100:
                break
        return {"via": "pull request", "ref": pn, "base_sha": pr["base"]["sha"], "fix_at": pr["merged_at"],
                "files": [(f["filename"], f["status"]) for f in files]}
    return None


def by_commit(clone: str, number: int) -> dict | None:
    """A commit whose message closes the issue by keyword; its first parent is the base."""
    out = git(clone, "log", "--all", "--format=%H%x1f%P%x1f%cI%x1f%B%x1e").stdout
    for rec in out.split("\x1e"):
        parts = rec.strip("\n").split("\x1f")
        if len(parts) < 4 or number not in {int(x) for x in CLOSES.findall(parts[3])}:
            continue
        parents = parts[1].split()
        if len(parents) != 1:  # a merge: the pull request route covers it
            continue
        stat = git(clone, "show", "--name-status", "--format=", parts[0]).stdout.splitlines()
        files = [(ln.split("\t")[-1], {"M": "modified", "D": "removed", "A": "added"}.get(ln[0], "renamed")) for ln in stat if "\t" in ln]
        return {"via": "commit", "ref": parts[0], "base_sha": parents[0], "fix_at": parts[2], "files": files}
    return None


def mine(cfg: dict, out: Path) -> None:
    api = Api()
    cands = []
    for it in closed_issues(api, cfg["repo"]):
        found = by_pull_request(api, cfg["repo"], it) or by_commit(cfg["clone"], it["number"])
        if found:
            cands.append({"issue": it["number"], "title": it["title"], "body": it.get("body") or "", "created": it["created_at"],
                          "labels": [x["name"] for x in it.get("labels", [])], **found})
    out.write_bytes((json.dumps(cands, indent=1, ensure_ascii=False) + "\n").encode("utf-8"))
    print(len(cands), "candidates:", {v: sum(c["via"] == v for c in cands) for v in ("pull request", "commit")})


def at_base(clone: str, sha: str, path: str) -> bool:
    return git(clone, "cat-file", "-e", f"{sha}:{path}").returncode == 0


def select(cfg: dict, cands: list[dict]) -> tuple[list[dict], dict]:
    """The pre-registered filters, then the seeded order; every task that passes is kept up to ``want``."""
    why: dict[str, list[int]] = {}

    def drop(reason: str, c: dict) -> None:
        why.setdefault(reason, []).append(c["issue"])

    ok = []
    for c in cands:
        text = f"{c['title']}\n{c['body']}"
        gold = [p for p, s in c["files"] if s in ("modified", "removed") and is_gold_path(p) and at_base(cfg["clone"], c["base_sha"], p)]
        if c["created"] >= c["fix_at"]:
            drop("the issue is newer than the fix", c)
        elif not 1 <= len(gold) <= cfg.get("gold_max", 4):
            drop(f"gold code files not 1-{cfg.get('gold_max', 4)} at the base commit", c)
        elif len(c["files"]) > cfg.get("changed_max", 12):
            drop("more than 12 files changed", c)
        elif not cfg.get("issue_min", 150) <= len(c["body"]) <= cfg.get("issue_max", 4000):
            drop("issue body length outside 150-4000", c)
        elif leaks(text, gold):
            drop("the issue names a gold file's path", c)
        else:
            ok.append({**c, "gold": gold})
    ok.sort(key=lambda c: hashlib.sha256(f"{cfg['seed']}:{c['issue']}".encode()).hexdigest())
    tasks = [{"id": f"i{c['issue']}", "issue": c["issue"], "via": c["via"], "ref": c["ref"], "title": c["title"], "body": c["body"],
              "gold": c["gold"], "base_sha": c["base_sha"], "labels": c["labels"], "issue_created": c["created"], "fixed_at": c["fix_at"]}
             for c in ok[: cfg["want"]]]
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
