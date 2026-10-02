"""Run the big-repository study (DESIGN_BIG.md): one real headless Claude Code session per task and arm, each asked
which files must change to fix a real bug report; the answer is scored against the files the fixing pull request
changed (score_big.py).

    python benchmarks/agent_compare/big_run.py CONFIG.json

CONFIG: {"tasks": tasks.json, "copies": {arm: working copy}, "out": results.jsonl, "model": "claude-sonnet-5-5",
"plugin_dir": the mod, "mod_options": {"auto": "nudge"}, "path_dirs": {arm: [dirs put on PATH]}, "drop_path": ["...\\.local\\bin"],
"arms": [...], "workers": n, "timeout_s": s, "max_turns": n}. Resumable: a task and arm already in `out` is skipped.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from guard_run import find_transcript, git_env, run

SCHEMA = {
    "type": "object",
    "properties": {
        "files": {
            "type": "array", "maxItems": 5,
            "items": {"type": "object", "properties": {
                "path": {"type": "string", "description": "path relative to the repository root"},
                "lines": {"type": "string", "description": "the lines or the function that must change"},
                "why": {"type": "string", "description": "one sentence"}}, "required": ["path", "why"]},
        },
    },
    "required": ["files"],
}
NETWORK = re.compile(r"\b(curl|wget|Invoke-WebRequest|iwr|WebFetch|WebSearch)\b|github\.com|(^|[\s;&|])gh\s", re.IGNORECASE)
VERINODA_ARMS = ("verinoda_mod", "verinoda_setup")


def prompt_for(task: dict, copy: Path) -> str:
    """The same text for every arm; it never mentions an index or a tool."""
    c = copy.as_posix()
    return (f"You are working in the Home Assistant Core repository at {c} (a git checkout of its development "
            "branch). A user filed this bug report:\n\n"
            f"Title: {task['title']}\n\n{task['body']}\n\n"
            "Task: find where in the repository's source code this bug should be fixed. This is a read-only task: "
            "do not modify any file, and do not use the network or look the issue up on the web or on GitHub; work "
            f"only in {c} with the tools you have.\n\n"
            "Answer with the files that must change to fix this bug: at most five, the most important first, "
            "non-test source files only, each with its path relative to the repository root, the lines or the "
            "function that must change, and one sentence on why.")


def tools_for(arm: str) -> str:
    return "Bash Read Glob Grep Skill" + (" mcp__verinoda" if arm in VERINODA_ARMS else "")


def session_argv(prompt: str, arm: str, copy: Path, cfg: dict) -> list[str]:
    argv = ["claude", "-p", prompt, "--output-format", "json", "--model", cfg["model"],
            "--setting-sources", "project,local", "--strict-mcp-config", "--allowedTools", tools_for(arm),
            "--max-turns", str(cfg.get("max_turns", 80)), "--json-schema", json.dumps(SCHEMA)]
    if arm in VERINODA_ARMS:
        argv += ["--mcp-config", str(copy / ".mcp.json")]
    if arm == "verinoda_mod":
        argv += ["--plugin-dir", cfg["plugin_dir"], "--settings",
                 json.dumps({"pluginConfigs": {"verinoda-live": {"options": cfg["mod_options"]}}})]
    return argv


def session_env(arm: str, cfg: dict, base: dict | None = None) -> dict:
    """The environment of a session: the user's own `graphify` / `verinoda` commands are off PATH, the arm's tool on it."""
    env = dict(base if base is not None else git_env())
    drop = [d.lower().replace("/", "\\") for d in cfg.get("drop_path", [])]
    keep = [p for p in env.get("PATH", "").split(os.pathsep) if p.lower().replace("/", "\\").rstrip("\\") not in drop]
    env["PATH"] = os.pathsep.join([*cfg.get("path_dirs", {}).get(arm, []), *keep])
    env["GRAPHIFY_NO_AUTO_REFRESH"] = "1"
    return env


def normalize_path(path: str, copy: Path) -> str:
    """A path as the answer spells it, relative to the repository root with forward slashes."""
    p = path.strip().replace("\\", "/")
    root = copy.as_posix().rstrip("/") + "/"
    if p.lower().startswith(root.lower()):
        p = p[len(root):]
    while p.startswith("./"):
        p = p[2:]
    return p.lstrip("/")


def files_named(answer: dict | None, copy: Path) -> list[str]:
    """The first five distinct files the answer names, in order."""
    out: list[str] = []
    for f in (answer or {}).get("files") or []:
        p = normalize_path(str(f.get("path", "")), copy) if isinstance(f, dict) else ""
        if p and p not in out:
            out.append(p)
    return out[:5]


def extract_answer(rep: dict) -> dict | None:
    """The structured answer of `claude -p --json-schema`: its own field, else JSON in the result text."""
    for key in ("structured_output", "structuredOutput"):
        if isinstance(rep.get(key), dict):
            return rep[key]
    text = rep.get("result") or ""
    for cand in (text, text[text.find("{"): text.rfind("}") + 1] if "{" in text else ""):
        try:
            v = json.loads(cand)
            if isinstance(v, dict):
                return v
        except ValueError:
            continue
    return None


def session_stats(transcript: Path | None) -> dict:
    """What the transcript shows the session did: tool calls, calls to the arm's tool, and network lookups."""
    s = {"tool_calls": 0, "verinoda_calls": 0, "graphify_calls": 0, "network": [], "first_tools": []}
    if transcript is None or not transcript.is_file():
        return s
    for line in transcript.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            m = json.loads(line).get("message") or {}
        except ValueError:
            continue
        c = m.get("content")
        if m.get("role") != "assistant" or not isinstance(c, list):
            continue
        for x in c:
            if x.get("type") != "tool_use":
                continue
            name, inp = x.get("name", ""), x.get("input") or {}
            text = json.dumps(inp).replace("\\\\", "/").lower()
            s["tool_calls"] += 1
            if len(s["first_tools"]) < 5:
                s["first_tools"].append(name)
            if name.startswith("mcp__verinoda") or (name == "Bash" and "verinoda" in text):
                s["verinoda_calls"] += 1
            if (name == "Bash" and "graphify" in text) or "graphify-out" in text:
                s["graphify_calls"] += 1
            if name in ("WebFetch", "WebSearch") or (name == "Bash" and NETWORK.search(str(inp.get("command", "")))):
                s["network"].append(str(inp.get("command") or name)[:120])
    return s


def one(cfg: dict, task: dict, arm: str) -> dict:
    copy = Path(cfg["copies"][arm])
    argv = session_argv(prompt_for(task, copy), arm, copy, cfg)
    rc, out, err, secs = run(argv, copy, session_env(arm, cfg), timeout=cfg.get("timeout_s", 1800))
    try:
        rep = json.loads(out)
    except ValueError:
        rep = {"is_error": True, "result": (out or err)[-500:]}
    answer = extract_answer(rep)
    usage = rep.get("usage") or {}
    st = session_stats(find_transcript(rep.get("session_id", "")))
    return {"id": task["id"], "arm": arm, "exit": rc, "seconds": secs, "files": files_named(answer, copy),
            "answered": answer is not None, "is_error": rep.get("is_error"), "subtype": rep.get("subtype"),
            "session_id": rep.get("session_id"), "num_turns": rep.get("num_turns"), "cost_usd": rep.get("total_cost_usd"),
            "input_tokens": sum(int(usage.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens",
                                                                 "cache_read_input_tokens")),
            "output_tokens": int(usage.get("output_tokens") or 0), "answer": answer, **st}


def main() -> int:
    cfg = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    tasks = json.loads(Path(cfg["tasks"]).read_text(encoding="utf-8"))["tasks"]
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
        hit = len(set(r["files"]) & set(t["gold"]))
        print(f"{time.strftime('%H:%M:%S')} {t['id']}:{a} gold {hit}/{len(t['gold'])} files={len(r['files'])} "
              f"tool={r['verinoda_calls'] or r['graphify_calls']} net={len(r['network'])} turns={r['num_turns']} {r['seconds']} s",
              flush=True)

    with ThreadPoolExecutor(cfg.get("workers", 8)) as ex:
        list(ex.map(work, todo))
    for arm in cfg["arms"]:
        dirty = subprocess.run(["git", "-C", cfg["copies"][arm], "status", "--porcelain"], capture_output=True, text=True,
                               check=False).stdout.splitlines()
        print(f"{arm}: {len(dirty)} paths changed in the working copy after the run", flush=True)
    print("all done", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
