"""Run the big-repository study (DESIGN_BIG.md): one real headless Claude Code session per task and arm, each asked
which files must change to fix a real bug report; the answer is scored against the files the fixing pull request
changed (score_big.py).

    python benchmarks/agent_compare/big_run.py CONFIG.json

CONFIG: {"tasks": tasks.json, "copies": {arm: working copy} or "copy_pattern": ".../{task}/{arm}", "project": "the ... repository at {c} (...)", "out": results.jsonl, "model": "claude-sonnet-5-5",
"plugin_dir": the mod, "mod_options": {"auto": "nudge"}, "path_dirs": {arm: [dirs put on PATH]}, "drop_path": ["...\\.local\\bin"],
"arms": [...], "arm_defs": {arm: {...}}, "arm_workers": {arm: n}, "workers": default n, "timeout_s": s, "max_turns": n}.
Resumable: a task and arm already in `out` is skipped.

The arms `none`, `graphify`, `verinoda_setup` and `verinoda_mod` are built in. `arm_defs` adds arms (or changes one): {"kind": "none" |
"graphify" | "verinoda", "plugin": the mod is loaded, "mod_options": its settings (replace the study's `mod_options`), "copy_arm": the
arm whose working copy it uses (arms sharing a copy run one session at a time there), "prompt_suffix": a paragraph after the shared
task text, "model": the model of this arm}.
"""

from __future__ import annotations

import contextlib
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
# A call of the tool is its command followed by a subcommand, or an MCP tool: not a path that names a working copy (the
# copies are folders called `verinoda`, `verinoda_mod`, `graphify`, so `cd .../verinoda && grep` is no call of Verinoda).
_SUB = (r"(?:query|analyze|trace|map|plan|resolve|verify|challenge|doctor|api|review|probe|decide|check|claim|update|scan|setup|"
        r"ui|tq|explain|impact|search|index|mcp|debug|history|path|affected|god-nodes|cluster-only|label|extract|watch|hook)")
VERINODA_CMD = re.compile(r"""(?:(?<![\w./-])verinoda|/verinoda\.exe)["']?\s+""" + _SUB + r"\b")
GRAPHIFY_CMD = re.compile(r"""(?:(?<![\w./-])graphify|/graphify\.exe)["']?\s+""" + _SUB + r"\b")
NETWORK = re.compile(r"\b(curl|wget|Invoke-WebRequest|iwr|WebFetch|WebSearch)\b|github\.com|(^|[\s;&|])gh\s", re.IGNORECASE)
BUILTIN_ARMS = {"none": {"kind": "none"}, "graphify": {"kind": "graphify"}, "verinoda_setup": {"kind": "verinoda"},
                "verinoda_mod": {"kind": "verinoda", "plugin": True}}
# what the mod's assist features put in front of the model: hook context, or the text of a refused search
ASSIST_MARKS = {"inject": "[Verinoda locate]", "coupled_notes": "[Verinoda coupled]", "gate": "[Verinoda] This search was not run",
                "nudge": "[Verinoda auto-context]"}
_COPY_LOCKS: dict[str, threading.Lock] = {}
_COPY_LOCKS_GUARD = threading.Lock()


def arm_def(cfg: dict, arm: str) -> dict:
    """An arm's definition: the built-in one, changed by the config's ``arm_defs``; KeyError for an arm neither knows."""
    d = {**BUILTIN_ARMS.get(arm, {}), **(cfg.get("arm_defs") or {}).get(arm, {})}
    if "kind" not in d:
        raise KeyError(f"arm {arm!r} is not built in and has no entry in arm_defs")
    return d


@contextlib.contextmanager
def isolated_plugin_store(folder: Path | None = None):
    """Empty the mod's stored choices for a run and put them back after it. A choice stored by `/verinoda-auto`,
    `-guard` or `-assist` wins over the settings a scripted session is given, and the store is shared by every session
    that loads the mod, so an earlier `/verinoda-auto nudge` would put the nudge into arms that never asked for it.
    The copy of each file is kept next to it (`*.study-backup`) while the run lasts; one left by a run that died is
    put back first."""
    folder = folder or Path.home() / ".claude" / "plugins" / "store"
    files = sorted(folder.glob("verinoda-live_*.json")) if folder.is_dir() else []
    for f in files:
        backup = f.with_name(f.name + ".study-backup")
        if backup.exists():
            f.write_bytes(backup.read_bytes())
        else:
            backup.write_bytes(f.read_bytes())
        f.write_bytes(b"{}")
    try:
        yield
    finally:
        for f in files:
            backup = f.with_name(f.name + ".study-backup")
            if backup.exists():
                f.write_bytes(backup.read_bytes())
                backup.unlink()


def copy_lock(copy: str | Path) -> threading.Lock:
    """One lock per working copy: two arms that share a copy never run a session in it at the same time."""
    key = Path(copy).as_posix().lower()
    with _COPY_LOCKS_GUARD:
        return _COPY_LOCKS.setdefault(key, threading.Lock())


DEFAULT_PROJECT = "the Home Assistant Core repository at {c} (a git checkout of its development branch)"


def copy_of(cfg: dict, task: dict, arm: str) -> Path:
    """The working copy of a task and arm: one per arm (``copies``), or one per task and arm (``copy_pattern``, for a
    study whose tasks each sit at their own commit)."""
    which = arm_def(cfg, arm).get("copy_arm", arm)
    if "copy_pattern" in cfg:
        return Path(cfg["copy_pattern"].format(task=task["id"], arm=which))
    return Path(cfg["copies"][which])


def prompt_for(task: dict, copy: Path, project: str | None = None, suffix: str = "") -> str:
    """The same text for every arm; it never mentions an index or a tool. An arm that tests an instruction adds ``suffix``."""
    c = copy.as_posix()
    text = (f"You are working in {(project or DEFAULT_PROJECT).format(c=c)}. A user filed this bug report:\n\n"
            f"Title: {task['title']}\n\n{task['body']}\n\n"
            "Task: find where in the repository's source code this bug should be fixed. This is a read-only task: "
            "do not modify any file, and do not use the network or look the issue up on the web or on GitHub; work "
            f"only in {c} with the tools you have.\n\n"
            "Answer with the files that must change to fix this bug: at most five, the most important first, "
            "non-test source files only, each with its path relative to the repository root, the lines or the "
            "function that must change, and one sentence on why.")
    return f"{text}\n\n{suffix}" if suffix else text


def mod_options(cfg: dict, arm: str) -> dict:
    """The mod's settings in this arm: its own, else the study's."""
    return arm_def(cfg, arm).get("mod_options", cfg.get("mod_options", {}))


def tools_for(arm: str, cfg: dict | None = None) -> str:
    """The tools a session may use without asking: the mod's own (`locate`, `coupled`) only where its assist is on."""
    cfg = cfg or {}
    d = arm_def(cfg, arm)
    has_assist = bool(d.get("plugin")) and str(mod_options(cfg, arm).get("assist", "off")).strip() not in ("", "off")
    return "Bash Read Glob Grep Skill" + (" mcp__verinoda" if d["kind"] == "verinoda" else "") + (" mcp__verinoda-live" if has_assist else "")


def session_argv(prompt: str, arm: str, copy: Path, cfg: dict) -> list[str]:
    d = arm_def(cfg, arm)
    argv = ["claude", "-p", prompt, "--output-format", "json", "--model", d.get("model", cfg["model"]),
            "--setting-sources", "project,local", "--strict-mcp-config", "--allowedTools", tools_for(arm, cfg),
            "--max-turns", str(cfg.get("max_turns", 80)), "--json-schema", json.dumps(SCHEMA)]
    if d["kind"] == "verinoda":
        argv += ["--mcp-config", str(copy / ".mcp.json")]
    if d.get("plugin"):
        argv += ["--plugin-dir", cfg["plugin_dir"], "--settings",
                 json.dumps({"pluginConfigs": {"verinoda-live": {"options": mod_options(cfg, arm)}}})]
    return argv


def session_env(arm: str, cfg: dict, base: dict | None = None) -> dict:
    """The environment of a session: the user's own `graphify` / `verinoda` commands are off PATH, the arm's tool on it."""
    env = dict(base if base is not None else git_env())
    drop = [d.lower().replace("/", "\\") for d in cfg.get("drop_path", [])]
    keep = [p for p in env.get("PATH", "").split(os.pathsep) if p.lower().replace("/", "\\").rstrip("\\") not in drop]
    dirs = cfg.get("path_dirs", {})  # an arm without an entry takes the one of the arm whose copy it uses
    env["PATH"] = os.pathsep.join([*dirs.get(arm, dirs.get(arm_def(cfg, arm).get("copy_arm", arm), [])), *keep])
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
    assist = {"inject": 0, "coupled_notes": 0, "gate": 0, "nudge": 0, "locate_calls": 0, "coupled_calls": 0, "tool_search": 0}
    s = {"tool_calls": 0, "verinoda_calls": 0, "graphify_calls": 0, "network": [], "first_tools": [], "assist": assist}
    if transcript is None or not transcript.is_file():
        return s
    for line in transcript.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        attached = entry.get("attachment") or {}
        if attached.get("type") == "hook_additional_context":
            for note in attached.get("content") or []:
                for key in ("inject", "coupled_notes", "nudge"):
                    assist[key] += str(note).startswith(ASSIST_MARKS[key])
        m = entry.get("message") or {}
        c = m.get("content")
        if m.get("role") == "user" and isinstance(c, list):
            for x in c:
                if x.get("type") == "tool_result" and ASSIST_MARKS["gate"] in json.dumps(x.get("content")):
                    assist["gate"] += 1
        if m.get("role") != "assistant" or not isinstance(c, list):
            continue
        for x in c:
            if x.get("type") != "tool_use":
                continue
            name, inp = x.get("name", ""), x.get("input") or {}
            text = json.dumps(inp).replace("\\\\", "/").lower()
            s["tool_calls"] += 1
            assist["locate_calls"] += name == "mcp__verinoda-live__locate"
            assist["coupled_calls"] += name == "mcp__verinoda-live__coupled"
            assist["tool_search"] += name == "ToolSearch"
            if len(s["first_tools"]) < 5:
                s["first_tools"].append(name)
            command = str(inp.get("command", "")).replace("\\", "/")
            if name.startswith("mcp__verinoda") or (name == "Bash" and VERINODA_CMD.search(command)):
                s["verinoda_calls"] += 1
            # Graphify's own files count as its use too: the graph's report and wiki are what its hook points the agent to
            if (name == "Bash" and GRAPHIFY_CMD.search(command)) or "graphify-out" in text:
                s["graphify_calls"] += 1
            if name in ("WebFetch", "WebSearch") or (name == "Bash" and NETWORK.search(str(inp.get("command", "")))):
                s["network"].append(str(inp.get("command") or name)[:120])
    return s


def one(cfg: dict, task: dict, arm: str) -> dict:
    copy = copy_of(cfg, task, arm)
    d = arm_def(cfg, arm)
    argv = session_argv(prompt_for(task, copy, cfg.get("project"), d.get("prompt_suffix", "")), arm, copy, cfg)
    with copy_lock(copy):
        rc, out, err, secs = run(argv, copy, session_env(arm, cfg), timeout=cfg.get("timeout_s", 1800))
    try:
        rep = json.loads(out)
    except ValueError:
        rep = {"is_error": True, "result": (out or err)[-500:]}
    answer = extract_answer(rep)
    usage = rep.get("usage") or {}
    st = session_stats(find_transcript(rep.get("session_id", "")))
    return {"id": task["id"], "arm": arm, "model": d.get("model", cfg["model"]), "exit": rc, "seconds": secs, "files": files_named(answer, copy),
            "answered": answer is not None, "is_error": rep.get("is_error"), "subtype": rep.get("subtype"),
            "session_id": rep.get("session_id"), "num_turns": rep.get("num_turns"), "cost_usd": rep.get("total_cost_usd"),
            "input_tokens": sum(int(usage.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens",
                                                                 "cache_read_input_tokens")),
            "output_tokens": int(usage.get("output_tokens") or 0), "answer": answer, **st}


def free_mb() -> int | None:
    """Free physical memory in MB (Windows), else None: a Verinoda session on a big index holds gigabytes."""
    if sys.platform != "win32":
        return None
    import ctypes

    class Status(ctypes.Structure):
        _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total", ctypes.c_ulonglong),
                    ("avail", ctypes.c_ulonglong), ("tpage", ctypes.c_ulonglong), ("apage", ctypes.c_ulonglong),
                    ("tvirt", ctypes.c_ulonglong), ("avirt", ctypes.c_ulonglong), ("ext", ctypes.c_ulonglong)]

    st = Status()
    st.length = ctypes.sizeof(Status)
    return int(st.avail // (1024 * 1024)) if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)) else None


def status_paths(copy: str) -> set[str]:
    """What `git status` shows in a working copy: the setup's own files are in it before the run, so what a run
    changed is the difference between the two."""
    out = subprocess.run(["git", "-C", copy, "status", "--porcelain"], capture_output=True, text=True, check=False).stdout
    return set(out.splitlines())


def run_by_arm(todo: list, limits: dict, work, default: int = 2) -> list:
    """Run ``work((task, arm))`` for every item, at most ``limits[arm]`` at once per arm, the arms side by side (a
    Verinoda session holds gigabytes on a big index, a plain one almost nothing). A crashing item does not stop the
    others; the ones that crashed are returned with their errors, and a rerun picks them up."""
    by_arm: dict[str, list] = {}
    for item in todo:
        by_arm.setdefault(item[1], []).append(item)
    pools = {a: ThreadPoolExecutor(limits.get(a, default)) for a in by_arm}
    futures = [(item, pools[a].submit(work, item)) for a, items in by_arm.items() for item in items]
    failed = []
    for item, fut in futures:
        exc = fut.exception()
        if exc is not None:
            failed.append((item, repr(exc)))
    for pool in pools.values():
        pool.shutdown()
    return failed


def main() -> int:
    cfg = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    uses_mod = any(arm_def(cfg, a).get("plugin") for a in cfg["arms"])
    store = Path(cfg["plugin_store_dir"]) if cfg.get("plugin_store_dir") else None
    with isolated_plugin_store(store) if uses_mod else contextlib.nullcontext():
        return run_study(cfg)


def run_study(cfg: dict) -> int:
    tasks = json.loads(Path(cfg["tasks"]).read_text(encoding="utf-8"))["tasks"]
    out = Path(cfg["out"])
    done = set()
    if out.exists():
        done = {(r["id"], r["arm"]) for r in map(json.loads, out.read_text(encoding="utf-8").splitlines()) if r}
    todo = [(t, a) for t in tasks for a in cfg["arms"] if (t["id"], a) not in done]
    lock = threading.Lock()
    copies = {(t["id"], a): str(copy_of(cfg, t, a)) for t in tasks for a in cfg["arms"]}
    before = {k: status_paths(c) for k, c in copies.items()}
    stop = threading.Event()

    def monitor() -> None:
        low = free_mb()
        while not stop.wait(120):
            now = free_mb()
            low = now if low is None or now is None else min(low, now)
            print(f"{time.strftime('%H:%M:%S')} free memory {now} MB (lowest so far {low})", flush=True)

    threading.Thread(target=monitor, daemon=True).start()
    print(f"{len(todo)} sessions to run ({len(done)} done)", flush=True)

    def work(item):
        t, a = item
        r = one(cfg, t, a)
        with lock, out.open("ab") as f:
            f.write((json.dumps(r) + "\n").encode("utf-8"))
        hit = len(set(r["files"]) & set(t["gold"]))
        shown = r["assist"]["inject"] + r["assist"]["coupled_notes"] + r["assist"]["gate"]
        print(f"{time.strftime('%H:%M:%S')} {t['id']}:{a} gold {hit}/{len(t['gold'])} files={len(r['files'])} "
              f"tool={r['verinoda_calls'] or r['graphify_calls']} assist_shown={shown} net={len(r['network'])} "
              f"turns={r['num_turns']} {r['seconds']} s", flush=True)

    for item, err in run_by_arm(todo, cfg.get("arm_workers", {}), work, cfg.get("workers", 2)):
        print(f"FAILED {item[0]['id']}:{item[1]} {err}", flush=True)
    stop.set()
    for arm in cfg["arms"]:
        changed = sorted(f"{k[0]}: {x}" for k, c in copies.items() if k[1] == arm for x in status_paths(c) ^ before[k])
        print(f"{arm}: {len(changed)} paths changed in the working copies by the run {changed[:5]}", flush=True)
    print("all done", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
