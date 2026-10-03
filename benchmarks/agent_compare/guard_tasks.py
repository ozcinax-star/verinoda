"""Assemble and check the tasks of the check-after-edits study (DESIGN_GUARD.md) from the task workflow's output:
the reviewer's final prompts, tasks it dropped left out; then, in a fresh clone per task, the hidden test must fail
and, with the reference patch applied, pass. Writes tasks.json (the kept tasks) and task_checks.json (every verdict).

    python benchmarks/agent_compare/guard_tasks.py WORKFLOW_OUTPUT.json CONFIG.json OUT_DIR
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from guard_run import git_env, run

REPO_KEY = {"Graphify-Labs/graphify": "graphify", "fastapi/sqlmodel": "sqlmodel"}


def assemble(results: list[dict]) -> tuple[list[dict], list[dict]]:
    tasks, checks = [], []
    for r in results:
        verdicts = {v["id"]: v for v in (r.get("review") or {}).get("verdicts", [])}
        for t in (r.get("written") or {}).get("tasks", []):
            v = verdicts.get(t["id"])
            entry = {"id": t["id"], "area": r["key"], "review": v["verdict"] if v else "not reviewed",
                     "reasons": v["reasons"] if v else ""}
            checks.append(entry)
            if v is None or v["verdict"] == "dropped":
                continue
            tasks.append({"id": t["id"], "repo": REPO_KEY[r["repo"]], "area": r["key"], "title": t["title"],
                          "prompt": v["prompt"] or t["prompt"], "test_code": t["test_code"],
                          "reference_patch": t["reference_patch"], "internals_used": t["internals_used"],
                          "why_memory_may_fail": t["why_memory_may_fail"]})
    return tasks, checks


def verify(task: dict, repo: dict, work: Path) -> dict:
    """The hidden test on a fresh clone: failing as it is, passing with the reference patch."""
    copy = work / task["id"]
    if copy.exists():
        shutil.rmtree(copy)
    env = git_env()
    sha = run(["git", "-C", repo["base"], "rev-parse", "HEAD"], None, env)[1].strip()
    run(["git", "clone", "-q", "--shared", repo["base"], str(copy)], None, env)
    run(["git", "-C", str(copy), "checkout", "-q", "--detach", sha], None, env)
    hidden = work / f"{task['id']}-hidden"
    hidden.mkdir(parents=True, exist_ok=True)
    test = hidden / f"test_{task['id'].replace('-', '_')}.py"
    test.write_bytes(task["test_code"].encode("utf-8"))
    tenv = git_env()
    tenv["PYTHONPATH"] = str(copy)
    pytest = [repo["python"], "-m", "pytest", "-q", "-p", "no:cacheprovider", "--rootdir", str(hidden), str(test)]
    before = run(pytest, copy, tenv, timeout=600)
    patch = task["reference_patch"] if task["reference_patch"].endswith("\n") else task["reference_patch"] + "\n"
    applied = run(["git", "-C", str(copy), "apply", "--whitespace=nowarn", "-"], None, env, stdin=patch)
    after = run(pytest, copy, tenv, timeout=600)
    return {"fails_without": before[0] != 0, "patch_applies": applied[0] == 0, "passes_with": after[0] == 0,
            "apply_error": applied[2][-300:], "after_tail": (after[1] + after[2])[-400:]}


def main() -> int:
    raw = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    cfg = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
    out = Path(sys.argv[3])
    tasks, checks = assemble(raw["result"] if isinstance(raw, dict) else raw)
    work = Path(cfg["runs"]) / "verify"
    kept = []
    by_id = {c["id"]: c for c in checks}
    for t in tasks:
        v = verify(t, cfg["repos"][t["repo"]], work)
        by_id[t["id"]]["mechanical"] = v
        ok = v["fails_without"] and v["patch_applies"] and v["passes_with"]
        by_id[t["id"]]["kept"] = ok
        if ok:
            kept.append(t)
        print(t["id"], "kept" if ok else f"dropped {v}", flush=True)
    out.mkdir(parents=True, exist_ok=True)
    (out / "tasks.json").write_bytes((json.dumps(kept, indent=1, ensure_ascii=False) + "\n").encode("utf-8"))
    (out / "task_checks.json").write_bytes((json.dumps(checks, indent=1, ensure_ascii=False) + "\n").encode("utf-8"))
    print(f"{len(kept)} of {len(checks)} tasks kept")
    return 0


if __name__ == "__main__":
    sys.exit(main())
