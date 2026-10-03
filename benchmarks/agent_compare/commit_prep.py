"""Working copies for a study whose tasks each sit at their own commit (DESIGN_SEL4.md): per task a one-commit repository at
the task's base commit (the fix is not reachable from it), and from it the four arms' copies: `none`; `graphify`
(graphify update + graphify claude install); `verinoda_mod` (verinoda setup . --agents claude); `verinoda_setup`, a copy of
the finished Verinoda one (so the two share no analysis records). Resumable per task.

    python benchmarks/agent_compare/commit_prep.py CONFIG.json

CONFIG: {"tasks": tasks.json, "clone": the full local clone the base commits are fetched from, "work": folder,
"graphify": graphify.exe, "verinoda_dir": folder with verinoda.exe, "workers": n}
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def env(cfg: dict) -> dict:
    e = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    e["PATH"] = cfg["verinoda_dir"] + os.pathsep + e.get("PATH", "")
    e["GRAPHIFY_NO_AUTO_REFRESH"] = "1"
    e["PYTHONIOENCODING"] = "utf-8"
    return e


def run(argv: list[str], cwd: Path | None, cfg: dict, timeout: int = 3600) -> tuple[int, str, float]:
    t0 = time.perf_counter()
    p = subprocess.run(argv, cwd=cwd, env=env(cfg), capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=timeout, check=False)
    return p.returncode, p.stdout + p.stderr, round(time.perf_counter() - t0, 1)


def one_commit_repo(cfg: dict, sha: str, dest: Path) -> None:
    """The repository at one commit and nothing else: no history, so no later commit to find."""
    if dest.exists():
        shutil.rmtree(dest, onerror=lambda f, p, _e: (os.chmod(p, 0o700), f(p)))
    dest.mkdir(parents=True)
    for argv in (["git", "init", "-q"], ["git", "fetch", "-q", "--depth", "1", cfg["clone"], sha],
                 ["git", "checkout", "-q", "--detach", "FETCH_HEAD"]):
        rc, out, _ = run(argv, dest, cfg)
        assert rc == 0, (argv, out[-300:])


def copy_tree(src: Path, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest, onerror=lambda f, p, _e: (os.chmod(p, 0o700), f(p)))
    shutil.copytree(src, dest)


def prep_task(cfg: dict, task: dict) -> dict:
    root = Path(cfg["work"]) / task["id"]
    if (root / "done.json").exists():
        return json.loads((root / "done.json").read_text(encoding="utf-8"))
    base = root / "base"
    one_commit_repo(cfg, task["base_sha"], base)
    info: dict = {"id": task["id"], "base_sha": task["base_sha"]}
    copy_tree(base, root / "none")
    copy_tree(base, root / "graphify")
    rc, out, secs = run([cfg["graphify"], "update", "."], root / "graphify", cfg)
    m = re.search(r"Rebuilt: (\d+) nodes, (\d+) edges", out)
    info["graphify"] = {"rc": rc, "seconds": secs, "nodes": int(m.group(1)) if m else None, "edges": int(m.group(2)) if m else None}
    rc2, _, _ = run([cfg["graphify"], "claude", "install"], root / "graphify", cfg)
    info["graphify"]["install_rc"] = rc2
    copy_tree(base, root / "verinoda_mod")
    rc, out, secs = run(["verinoda", "setup", ".", "--agents", "claude"], root / "verinoda_mod", cfg)
    m = re.search(r"scan - (\d+) files, (\d+) nodes, (\d+) edges", out)
    info["verinoda"] = {"rc": rc, "seconds": secs, "files": int(m.group(1)) if m else None, "nodes": int(m.group(2)) if m else None,
                        "edges": int(m.group(3)) if m else None}
    copy_tree(root / "verinoda_mod", root / "verinoda_setup")
    for arm in ("none", "graphify", "verinoda_mod", "verinoda_setup"):
        head = run(["git", "rev-parse", "HEAD"], root / arm, cfg)[1].strip()
        assert head == task["base_sha"], (task["id"], arm, head)
    info["ok"] = info["graphify"]["rc"] == 0 and info["verinoda"]["rc"] == 0 and info["graphify"]["nodes"] and info["verinoda"]["nodes"]
    (root / "done.json").write_bytes((json.dumps(info, indent=1) + "\n").encode("utf-8"))
    return info


def main() -> int:
    cfg = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    tasks = json.loads(Path(cfg["tasks"]).read_text(encoding="utf-8"))["tasks"]
    Path(cfg["work"]).mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(cfg.get("workers", 4)) as ex:
        futures = [(t, ex.submit(prep_task, cfg, t)) for t in tasks]
        report = []
        for t, f in futures:
            try:
                r = f.result()
            except Exception as exc:  # noqa: BLE001 - one task failing must not stop the others
                r = {"id": t["id"], "ok": False, "error": repr(exc)[:300]}
            report.append(r)
            print(time.strftime("%H:%M:%S"), t["id"], "ok" if r.get("ok") else f"FAILED {r}", flush=True)
    (Path(cfg["work"]) / "prep.json").write_bytes((json.dumps(report, indent=1) + "\n").encode("utf-8"))
    print(sum(bool(r.get("ok")) for r in report), "of", len(report), "tasks prepared")
    return 0


if __name__ == "__main__":
    sys.exit(main())
