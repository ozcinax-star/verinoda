"""Run the check-after-edits study (DESIGN_GUARD.md): one real headless Claude Code session per task and arm, each in
a fresh clone with a copied Verinoda index; then the hidden test and `verinoda check --diff` on what it left.

    python benchmarks/agent_compare/guard_run.py CONFIG.json

CONFIG: {"tasks": tasks.json, "repos": {key: {"base": clone with .verinoda, "python": env python}}, "runs": dir,
"out": results.jsonl, "plugin_dir": the mod, "verinoda_python": the build under test's python, "arms": [...],
"workers": n, "timeout_s": s, "max_turns": n}. Resumable: a task and arm already in `out` is skipped.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

NOTE_MARK = "[Verinoda check]"
TOOLS = "Bash Read Edit Write Glob Grep"


def prompt_for(task: dict, copy: Path, python: str) -> str:
    """The same text for every arm; it never mentions Verinoda."""
    c = copy.as_posix()
    return (f"You are working in the Python repository at {c} (a git clone).\n\n{task['prompt']}\n\n"
            "Notes:\n- Make the change in the repository's source code; do not commit.\n"
            f"- To run Python: cd {c} && PYTHONPATH={c} {python} ... (the dependencies are installed there; the package "
            "itself is imported from this folder). You may write and run your own quick checks; remove any scratch "
            "files you add.\n"
            f"- Work only inside {c}.\n"
            "When you are done, reply with a short summary of the change.")


def session_argv(prompt: str, arm: str, plugin_dir: str, max_turns: int) -> list[str]:
    argv = ["claude", "-p", prompt, "--output-format", "json", "--permission-mode", "acceptEdits",
            "--allowedTools", TOOLS, "--strict-mcp-config", "--max-turns", str(max_turns)]
    return argv + (["--plugin-dir", plugin_dir] if arm == "guard" else [])


def count_notes(transcript: Path | None) -> int:
    """Check notes the model received: the hook's additional context carrying the mark, one per edit it flagged."""
    if transcript is None or not transcript.is_file():
        return 0
    n = 0
    for line in transcript.read_text(encoding="utf-8", errors="replace").splitlines():
        if NOTE_MARK not in line:
            continue
        try:
            att = json.loads(line).get("attachment") or {}
        except ValueError:
            continue
        if att.get("type") == "hook_additional_context":
            n += sum(NOTE_MARK in c for c in att.get("content") or [] if isinstance(c, str))
    return n


def bad_sites(report: dict) -> list[str]:
    """`verinoda check --diff --json`: the absent or mismatched sites (the lines the session changed)."""
    return [s.get("at", "") for s in report.get("sites") or [] if s.get("verdict") in ("absent", "mismatch")]


def find_transcript(session_id: str) -> Path | None:
    root = Path.home() / ".claude" / "projects"
    hits = list(root.glob(f"*/{session_id}.jsonl")) if session_id else []
    return hits[0] if hits else None


def run(argv, cwd, env=None, timeout=600, stdin=None):
    t0 = time.perf_counter()
    try:
        p = subprocess.run(argv, cwd=cwd, env=env, input=stdin, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, check=False)
        return p.returncode, p.stdout, p.stderr, round(time.perf_counter() - t0, 1)
    except subprocess.TimeoutExpired as exc:
        return -9, exc.stdout or "", f"timeout after {timeout} s", round(time.perf_counter() - t0, 1)


def git_env() -> dict:
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def _force_remove(func, path, _exc) -> None:
    """git's pack files are read-only; on Windows that stops a delete until the flag is cleared."""
    os.chmod(path, 0o700)
    func(path)


def prepare(base: Path, dest: Path) -> None:
    if dest.exists():  # left by an interrupted run: started over
        shutil.rmtree(dest, onerror=_force_remove)
    env = git_env()
    sha = run(["git", "-C", str(base), "rev-parse", "HEAD"], None, env)[1].strip()
    assert run(["git", "clone", "-q", "--shared", str(base), str(dest)], None, env)[0] == 0
    assert run(["git", "-C", str(dest), "checkout", "-q", "--detach", sha], None, env)[0] == 0
    shutil.copytree(base / ".verinoda", dest / ".verinoda")
    with (dest / ".git" / "info" / "exclude").open("a", encoding="utf-8") as f:
        f.write("\n.verinoda/\n")


def one(cfg: dict, task: dict, arm: str) -> dict:
    repo = cfg["repos"][task["repo"]]
    copy = Path(cfg["runs"]) / arm / task["id"]
    prepare(Path(repo["base"]), copy)
    env = git_env()
    env["PATH"] = str(Path(cfg["verinoda_python"]).parent) + os.pathsep + env.get("PATH", "")
    argv = session_argv(prompt_for(task, copy, repo["python"]), arm, cfg["plugin_dir"], cfg.get("max_turns", 100))
    rc, out, err, secs = run(argv, copy, env, timeout=cfg.get("timeout_s", 2700))
    try:
        rep = json.loads(out)
    except ValueError:
        rep = {"is_error": True, "result": (out or err)[-500:]}
    hidden = Path(cfg["runs"]) / f"{arm}-hidden" / task["id"]
    hidden.mkdir(parents=True, exist_ok=True)
    test = hidden / f"test_{task['id'].replace('-', '_')}.py"
    test.write_bytes(task["test_code"].encode("utf-8"))
    tenv = git_env()
    tenv["PYTHONPATH"] = str(copy)
    trc, tout, terr, _ = run([repo["python"], "-m", "pytest", "-q", "-p", "no:cacheprovider", "--rootdir",
                                  str(hidden), str(test)], copy, tenv, timeout=600)
    crc, cout, _, _ = run([cfg["verinoda_python"], "-P", "-m", "verinoda", "check", "--diff", "--json", "--repo",
                           str(copy)], copy, git_env(), timeout=600)
    try:
        bad = bad_sites(json.loads(cout))
    except ValueError:
        bad = None
    diff = run(["git", "-C", str(copy), "diff", "--stat"], None, git_env())[1].strip().splitlines()
    return {"id": task["id"], "repo": task["repo"], "arm": arm, "exit": rc, "seconds": secs,
            "passed": trc == 0, "test_tail": (tout + terr)[-600:], "check_bad_sites": bad, "check_exit": crc,
            "notes": count_notes(find_transcript(rep.get("session_id", ""))), "session_id": rep.get("session_id"),
            "num_turns": rep.get("num_turns"), "cost_usd": rep.get("total_cost_usd"), "usage": rep.get("usage"),
            "is_error": rep.get("is_error"), "subtype": rep.get("subtype"), "result": (rep.get("result") or "")[-1500:],
            "diffstat": diff[-1] if diff else ""}


def main() -> int:
    cfg = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    tasks = json.loads(Path(cfg["tasks"]).read_text(encoding="utf-8"))
    out = Path(cfg["out"])
    done = set()
    if out.exists():
        done = {(r["id"], r["arm"]) for r in map(json.loads, out.read_text(encoding="utf-8").splitlines()) if r}
    todo = [(t, a) for t in tasks for a in cfg["arms"] if (t["id"], a) not in done]
    lock = threading.Lock()
    print(f"{len(todo)} sessions to run ({len(done)} done)", flush=True)

    def work(item):
        t, a = item
        r = one(cfg, t, a)
        with lock, out.open("ab") as f:
            f.write((json.dumps(r) + "\n").encode("utf-8"))
        print(f"{time.strftime('%H:%M:%S')} {t['id']}:{a} passed={r['passed']} notes={r['notes']} "
              f"bad={r['check_bad_sites'] and len(r['check_bad_sites'])} {r['seconds']} s", flush=True)

    with ThreadPoolExecutor(cfg.get("workers", 6)) as ex:
        list(ex.map(work, todo))
    print("all done", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
