"""Install the tool hooks of :mod:`verinoda.tool_hook` into an agent's hook configuration.

=========  ==========================================  =====================================================
agent      file (project / user scope)                 hook
=========  ==========================================  =====================================================
claude     ``.claude/settings.json`` /                 project: ``mcp_tool`` hooks on Grep, Bash and
           ``~/.claude/settings.json``                 Read/Edit/MultiEdit/Write that call ``run_tool``
                                                       (``grep_context`` / ``read_context``) on the project's
                                                       running server - no process per call; user: the
                                                       command hook below (a project may have no server)
codex      ``.codex/hooks.json`` / ``~/.codex/hooks.json``  a ``PostToolUse`` command hook on Bash
cursor     ``.cursor/hooks.json`` / ``~/.cursor/hooks.json``  a ``postToolUse`` command hook on Grep, Shell,
                                                       Read and Write
=========  ==========================================  =====================================================

The command hook is ``verinoda tool-hook --agent NAME``. Only Verinoda's own entries are written, replaced or
removed (an ``mcp_tool`` hook on the ``verinoda`` server calling ``grep_context`` / ``read_context``, or a command
running ``tool-hook``); every other key and hook of the file stays as it was. A file that does not parse as a JSON
object is never touched.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path

AGENTS = ("claude", "codex", "cursor")
SCOPES = ("project", "user")
TIMEOUT = 15
OURS_TOOLS = {"grep_context", "read_context"}


def _home(home: Path | None) -> Path:
    return Path(home) if home is not None else Path.home()


def config_path(agent: str, scope: str, project_dir: Path, home: Path | None = None) -> Path:
    if agent == "claude":
        return (project_dir if scope == "project" else _home(home)) / ".claude" / "settings.json"
    if agent == "codex":
        if scope == "project":
            return project_dir / ".codex" / "hooks.json"
        codex_home = os.environ.get("CODEX_HOME") if home is None else None
        return (Path(codex_home) if codex_home else _home(home) / ".codex") / "hooks.json"
    if agent == "cursor":
        return (project_dir if scope == "project" else _home(home)) / ".cursor" / "hooks.json"
    raise ValueError(f"unknown agent {agent!r} (one of {', '.join(AGENTS)})")


def hook_command(agent: str, scope: str, launcher: dict | None = None) -> tuple[str, list[str]]:
    """``(command line, warnings)`` of the command hook. A project file is shared: it names ``verinoda`` when the
    one on PATH is this build, else this machine's interpreter (said in the warnings)."""
    from verinoda.agents.installer import resolve_launcher

    launcher = launcher or resolve_launcher()
    argv = list(launcher["argv"]) + ["tool-hook", "--agent", agent]
    warnings = []
    if scope == "project" and launcher.get("how") != "path":
        warnings.append("the hook names this machine's interpreter (" + str(launcher["argv"][0]) + "): the `verinoda` "
                        "on PATH is not this build, so another machine needs `verinoda agent-hooks install` again")
    line = subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
    return line, warnings


def entries(agent: str, scope: str, command: str) -> tuple[str, list[dict]]:
    """``(event key, entries)`` Verinoda adds for this agent and scope."""
    if agent == "claude" and scope == "project":
        from importlib import resources

        tpl = json.loads(resources.files("verinoda.agents").joinpath("templates/claude_hooks.json")
                         .read_text("utf-8"))
        return "PostToolUse", tpl["hooks"]["PostToolUse"]
    hook = {"type": "command", "command": command, "timeout": TIMEOUT}
    if agent == "claude":
        return "PostToolUse", [{"matcher": "Grep|Bash|Read|Edit|MultiEdit|Write", "hooks": [hook]}]
    if agent == "codex":
        return "PostToolUse", [{"matcher": "^Bash$", "hooks": [hook]}]
    return "postToolUse", [{"command": command, "matcher": "Grep|Shell|Read|Write", "timeout": TIMEOUT}]


def _ours_hook(h) -> bool:
    if not isinstance(h, dict):
        return False
    if h.get("type") == "mcp_tool":
        inp = h.get("input") if isinstance(h.get("input"), dict) else {}
        return h.get("server") == "verinoda" and inp.get("name") in OURS_TOOLS
    cmd = str(h.get("command") or "").lower()
    return "tool-hook" in cmd and "verinoda" in cmd


def _strip(items: list) -> tuple[list, int]:
    """The entries with Verinoda's hooks taken out (an entry left with no hook goes), and how many were."""
    out, n = [], 0
    for e in items:
        if _ours_hook(e):   # a flat entry (Cursor)
            n += 1
            continue
        if isinstance(e, dict) and isinstance(e.get("hooks"), list):
            kept = [h for h in e["hooks"] if not _ours_hook(h)]
            if len(kept) != len(e["hooks"]):
                n += len(e["hooks"]) - len(kept)
                if not kept:
                    continue
                e = {**e, "hooks": kept}
        out.append(e)
    return out, n


def _read(path: Path) -> tuple[dict | None, str, str | None]:
    """``(data, original text, problem)``; a missing file is ``({}, "", None)``."""
    if not path.exists():
        return {}, "", None
    try:
        raw = path.read_bytes().decode("utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        return None, "", f"{path} cannot be read ({exc})"
    if not raw.strip():
        return {}, raw, None
    try:
        data = json.loads(raw)
    except ValueError as exc:
        return None, raw, f"{path} is not valid JSON ({exc}); not changed"
    if not isinstance(data, dict):
        return None, raw, f"{path} is not a JSON object; not changed"
    hooks = data.get("hooks")
    if hooks is not None and not isinstance(hooks, dict):
        return None, raw, f"{path}: \"hooks\" is not an object; not changed"
    return data, raw, None


def _dump(data: dict, raw: str) -> str:
    indent = 2
    for line in raw.splitlines()[1:]:
        lead = len(line) - len(line.lstrip(" "))
        if lead and line.strip():
            indent = lead
            break
    text = json.dumps(data, indent=indent, ensure_ascii=False) + "\n"
    return text.replace("\n", "\r\n") if "\r\n" in raw else text


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".verinoda-tmp")
    tmp.write_bytes(text.encode("utf-8"))
    os.replace(tmp, path)


def _empty(data: dict) -> bool:
    return not data or data == {"version": 1}


def _one(op: str, agent: str, scope: str, project_dir: Path, home, dry_run: bool, launcher) -> dict:
    path = config_path(agent, scope, project_dir, home)
    res = {"agent": agent, "scope": scope, "path": str(path), "warnings": []}
    data, raw, problem = _read(path)
    if problem:
        return {**res, "result": "refused", "error": problem}
    hooks = dict(data.get("hooks") or {})
    command, warns = hook_command(agent, scope, launcher)
    key, new = entries(agent, scope, command)
    found = 0
    for k in list(hooks):
        if isinstance(hooks[k], list):
            hooks[k], n = _strip(hooks[k])
            found += n
            if not hooks[k]:
                del hooks[k]
    if op == "status":
        cur = data.get("hooks", {}).get(key) if isinstance(data.get("hooks"), dict) else None
        current = isinstance(cur, list) and all(e in cur for e in new)
        return {**res, "result": "installed" if found and current else "outdated" if found else "absent",
                "hooks": found}
    if op == "install":
        res["warnings"] += warns
        hooks[key] = list(hooks.get(key) or []) + new
        if agent == "cursor":
            data = {"version": data.get("version", 1), **{k: v for k, v in data.items() if k != "version"}}
        if agent == "claude" and scope == "project" and not _claude_server(project_dir):
            res["warnings"].append("no `verinoda` server in .mcp.json: the hooks call it (run `verinoda install "
                                   "--agent claude --scope project`, or `verinoda setup`)")
    out = {**data, "hooks": hooks} if hooks else {k: v for k, v in data.items() if k != "hooks"}
    if op == "uninstall":
        if not found:
            return {**res, "result": "absent"}
        if _empty(out):
            if not dry_run:
                path.unlink()
            return {**res, "result": "removed file", "hooks": found}
    text = _dump(out, raw)
    if raw and json.loads(raw) == out:
        return {**res, "result": "unchanged"}
    result = ("removed" if op == "uninstall" else "updated" if path.exists() else "created")
    if not dry_run:
        _write(path, text)
    return {**res, "result": result, "hooks": found if op == "uninstall" else len(new)}


def _claude_server(project_dir: Path) -> bool:
    try:
        cfg = json.loads((project_dir / ".mcp.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return False
    return isinstance(cfg, dict) and "verinoda" in (cfg.get("mcpServers") or {})


def run(op: str, agents: list[str], scope: str, project_dir, home=None, dry_run: bool = False) -> dict:
    """``op`` (install | uninstall | status) for each agent; ``{"ok", "results", "dry_run"}``."""
    if op not in ("install", "uninstall", "status"):
        raise ValueError(f"unknown action {op!r}")
    if scope not in SCOPES:
        raise ValueError(f"scope must be one of {', '.join(SCOPES)}")
    bad = [a for a in agents if a not in AGENTS]
    if bad:
        raise ValueError(f"unknown agent {', '.join(bad)} (one of {', '.join(AGENTS)})")
    from verinoda.agents.installer import resolve_launcher

    launcher = resolve_launcher()
    project_dir = Path(project_dir).resolve()
    results = [_one(op, a, scope, project_dir, home, dry_run, launcher) for a in agents]
    return {"ok": all(r["result"] != "refused" for r in results), "action": op, "dry_run": dry_run,
            "results": results}


def render(res: dict) -> str:
    out = []
    for r in res["results"]:
        out.append(f"{r['agent']} ({r['scope']}): {r['result']}  {r['path']}" + (f"  {r['error']}" if r.get("error")
                                                                               else ""))
        out += [f"  warning: {w}" for w in r.get("warnings") or []]
    if res.get("dry_run"):
        out.append("dry run: nothing was written")
    return "\n".join(out) + "\n"
