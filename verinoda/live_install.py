"""Put ``verinoda live`` into Claude Code and Codex: hooks, and the two small skills (docs/DESIGN.md D137).

=========  =============================================  ==============================================
agent      hooks (project / user scope)                   skills
=========  =============================================  ==============================================
claude     ``.claude/settings.json`` /                    ``.claude/skills/verinoda-live`` and
           ``~/.claude/settings.json``                    ``.../verinoda-improve`` (``/verinoda-live``,
                                                          ``/verinoda-improve``)
codex      ``.codex/hooks.json`` /                        ``.agents/skills/verinoda-live`` and
           ``$CODEX_HOME/hooks.json``                     ``.../verinoda-improve`` (``$verinoda-live``,
                                                          ``$verinoda-improve``)
=========  =============================================  ==============================================

One command serves every hook event of both agents: ``verinoda live hook`` (the event is in the JSON the agent sends
on stdin). Four events are hooked: ``UserPromptSubmit`` (the auto-context, the finished commit review),
``PostToolUse`` of an edit tool or a shell command (the name check, the commit guard), ``Stop`` (the index refresh)
and ``SessionStart`` (the same, once, and HEAD noted).

Only Verinoda's own entries are written, replaced or removed (a command that ends in ``verinoda live hook``); every
other key and hook of the file stays as it was, and a file that does not parse as a JSON object is never touched.
The skills follow the installer's rules (docs/DESIGN.md D5): a file without the ownership marker is never overwritten,
local edits after an install are kept, uninstall removes only what is unchanged since install. What was written is
recorded in ``<root>/.verinoda/live-install.json``.

Codex runs hooks only with ``[features] codex_hooks = true`` in its ``config.toml`` and, for a project's
``.codex/hooks.json``, a trusted project; ``install`` says so and never edits that file.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from pathlib import Path

from verinoda.selffiles import MANIFEST_DIR, MARKER_PREFIX

AGENTS = ("claude", "codex")
SCOPES = ("project", "user")
SKILLS = ("live", "improve")
MANIFEST = "live-install.json"
TIMEOUTS = {"UserPromptSubmit": 30, "PostToolUse": 90, "Stop": 30, "SessionStart": 30}
# a command that ends in `verinoda live hook` (the program may be a path or `python -m verinoda`)
OURS = re.compile(r"\bverinoda\b.*\blive\s+hook\s*$", re.I)


def parse_agents(spec: str) -> list[str]:
    """``auto`` (the agents found on PATH), ``all`` or a comma list."""
    spec = (spec or "auto").strip().lower()
    if spec == "all":
        return list(AGENTS)
    if spec == "auto":
        return [a for a in AGENTS if shutil.which(a)]
    got = [a.strip() for a in spec.split(",") if a.strip()]
    bad = [a for a in got if a not in AGENTS]
    if bad:
        raise SystemExit(f"error: unknown agent {', '.join(bad)} (one of {', '.join(AGENTS)}, all, auto)")
    return got


def _home(home: Path | None) -> Path:
    return Path(home) if home is not None else Path.home()


def hooks_path(agent: str, scope: str, project_dir: Path, home: Path | None = None) -> Path:
    if agent == "claude":
        return (project_dir if scope == "project" else _home(home)) / ".claude" / "settings.json"
    if scope == "project":
        return project_dir / ".codex" / "hooks.json"
    codex_home = os.environ.get("CODEX_HOME") if home is None else None
    return (Path(codex_home) if codex_home else _home(home) / ".codex") / "hooks.json"


def skill_path(agent: str, scope: str, name: str, project_dir: Path, home: Path | None = None) -> Path:
    root = project_dir if scope == "project" else _home(home)
    return root / (".claude" if agent == "claude" else ".agents") / "skills" / f"verinoda-{name}" / "SKILL.md"


def _word(w: str) -> str:
    w = w.replace("\\", "/")
    return w if re.fullmatch(r"[\w./:@+-]+", w) else '"' + w.replace('"', '\\"') + '"'


def hook_command(scope: str, launcher: dict | None = None) -> tuple[str, list[str]]:
    """``(command line, warnings)``: ``verinoda live hook`` when the ``verinoda`` on PATH is this build, else this
    machine's interpreter (said in the warnings for a project file, which is shared)."""
    from verinoda.agents.installer import resolve_launcher

    launcher = launcher or resolve_launcher()
    warnings: list[str] = []
    if launcher.get("how") == "path":
        argv = ["verinoda"]
    else:
        argv = list(launcher["argv"])
        if scope == "project":
            warnings.append(f"the hook names this machine's interpreter ({argv[0]}): the `verinoda` on PATH is not "
                            "this build, so another machine needs `verinoda live install` again")
    return " ".join(_word(w) for w in [*argv, "live", "hook"]), warnings


def entries(agent: str, command: str) -> dict[str, list[dict]]:
    """The hook entries Verinoda adds for ``agent``, by event."""
    def hook(event: str) -> dict:
        return {"type": "command", "command": command, "timeout": TIMEOUTS[event]}

    post = "Edit|Write|MultiEdit|NotebookEdit|Bash|PowerShell" if agent == "claude" else "^(apply_patch|Edit|Write|Bash)$"
    return {"UserPromptSubmit": [{"hooks": [hook("UserPromptSubmit")]}],
            "PostToolUse": [{"matcher": post, "hooks": [hook("PostToolUse")]}],
            "Stop": [{"hooks": [hook("Stop")]}],
            "SessionStart": [{"hooks": [hook("SessionStart")]}]}


def _ours(h) -> bool:
    return isinstance(h, dict) and bool(OURS.search(str(h.get("command") or "")))


def _strip(items: list) -> tuple[list, int]:
    """``items`` without Verinoda's hooks (an entry left with no hook goes) and how many were taken out."""
    out, n = [], 0
    for e in items:
        if isinstance(e, dict) and isinstance(e.get("hooks"), list):
            kept = [h for h in e["hooks"] if not _ours(h)]
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
    if data.get("hooks") is not None and not isinstance(data["hooks"], dict):
        return None, raw, f'{path}: "hooks" is not an object; not changed'
    return data, raw, None


def _indent(raw: str) -> int | str:
    for line in raw.splitlines()[1:]:
        if line.startswith("\t"):
            return "\t"
        lead = len(line) - len(line.lstrip(" "))
        if lead and line.strip():
            return lead
    return 2


def _atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".verinoda-tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _hooks_one(op: str, agent: str, scope: str, project_dir: Path, home, dry_run: bool, launcher) -> dict:
    path = hooks_path(agent, scope, project_dir, home)
    res: dict = {"path": str(path), "warnings": []}
    data, raw, problem = _read(path)
    if problem:
        return {**res, "result": "refused", "error": problem}
    command, warns = hook_command(scope, launcher)
    new = entries(agent, command)
    hooks = dict(data.get("hooks") or {})
    found = 0
    for k in list(hooks):
        if isinstance(hooks[k], list):
            hooks[k], n = _strip(hooks[k])
            found += n
            if not hooks[k]:
                del hooks[k]
    if op == "status":
        cur = (data.get("hooks") or {})
        ok = all(isinstance(cur.get(ev), list) and all(e in cur[ev] for e in es) for ev, es in new.items())
        return {**res, "result": "installed" if found and ok else "outdated" if found else "absent", "hooks": found}
    if op == "install":
        res["warnings"] += warns
        for ev, es in new.items():
            hooks[ev] = list(hooks.get(ev) or []) + es
    out = {**data, "hooks": hooks} if hooks else {k: v for k, v in data.items() if k != "hooks"}
    if op == "uninstall" and not found:
        return {**res, "result": "absent"}
    if raw.strip() and json.loads(raw) == out:
        return {**res, "result": "unchanged"}
    if op == "uninstall" and not out:
        if not dry_run:
            path.unlink()
        return {**res, "result": "removed file", "hooks": found}
    text = json.dumps(out, indent=_indent(raw), ensure_ascii=False) + "\n"
    if "\r\n" in raw:
        text = text.replace("\n", "\r\n")
    if raw.startswith("﻿") or (path.exists() and path.read_bytes().startswith(b"\xef\xbb\xbf")):
        text = "﻿" + text
    result = "removed" if op == "uninstall" else "updated" if path.exists() else "created"
    if not dry_run:
        _atomic(path, text.encode("utf-8"))
    return {**res, "result": result, "hooks": found if op == "uninstall" else len(new)}


# -- the skills -----------------------------------------------------------------------------------------------

def _sha(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


def _manifest_file(root: Path) -> Path:
    return root / MANIFEST_DIR / MANIFEST


def _load_manifest(root: Path) -> dict:
    try:
        data = json.loads(_manifest_file(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _rel(root: Path, p: Path) -> str:
    try:
        return p.relative_to(root).as_posix()
    except ValueError:
        return str(p)


def _skills_one(op: str, agent: str, scope: str, project_dir: Path, home, dry_run: bool, launcher) -> list[dict]:
    from verinoda.agents.installer import render_skill

    root = project_dir if scope == "project" else _home(home)
    manifest = _load_manifest(root)
    recorded = manifest.setdefault("files", {})
    out = []
    for name in SKILLS:
        p = skill_path(agent, scope, name, project_dir, home)
        rel = _rel(root, p)
        item = {"path": str(p), "skill": f"verinoda-{name}"}
        data = render_skill(agent, launcher, template=f"{agent}_{name}")
        cur = p.read_bytes() if p.exists() else None
        if op == "status":
            state = ("absent" if cur is None else "unmanaged" if MARKER_PREFIX not in cur else
                     "installed" if cur == data else "outdated")
            out.append({**item, "result": state})
        elif op == "install":
            if cur is None:
                result = "created"
            elif MARKER_PREFIX not in cur:
                out.append({**item, "result": "refused",
                            "error": f"{p} exists and is not managed by Verinoda (no ownership marker); not overwritten"})
                continue
            elif cur == data:
                result = "unchanged"
            elif rel in recorded and _sha(cur) != recorded[rel]:
                out.append({**item, "result": "kept", "error": f"{p} was edited after it was installed; kept"})
                continue
            else:
                result = "updated"
            if result != "unchanged" and not dry_run:
                _atomic(p, data)
            if not dry_run:
                recorded[rel] = _sha(data if result != "unchanged" else cur)
            out.append({**item, "result": result})
        else:  # uninstall
            if cur is None:
                out.append({**item, "result": "absent"})
            elif MARKER_PREFIX not in cur or (rel in recorded and _sha(cur) != recorded[rel]) \
                    or (rel not in recorded and cur != data):
                out.append({**item, "result": "kept", "error": f"{p} is not as install wrote it; kept"})
            else:
                if not dry_run:
                    p.unlink()
                    recorded.pop(rel, None)
                    d = p.parent
                    while d != root and d.exists() and not any(d.iterdir()):
                        d.rmdir()
                        d = d.parent
                out.append({**item, "result": "removed"})
    if op != "status" and not dry_run:
        mf = _manifest_file(root)
        if recorded:
            _atomic(mf, (json.dumps({"files": recorded}, indent=2) + "\n").encode("utf-8"))
        elif mf.exists():
            mf.unlink()
    return out


# -- the public entry ---------------------------------------------------------------------------------------------

def run(op: str, agents: list[str], scope: str, project_dir, home=None, dry_run: bool = False,
        with_skills: bool = True) -> dict:
    """``op`` (install | uninstall | status) for each agent: ``{"ok", "action", "dry_run", "results"}``."""
    if op not in ("install", "uninstall", "status"):
        raise ValueError(f"unknown action {op!r}")
    if scope not in SCOPES:
        raise ValueError(f"scope must be one of {', '.join(SCOPES)}")
    bad = [a for a in agents if a not in AGENTS]
    if bad:
        raise ValueError(f"unknown agent {', '.join(bad)} (one of {', '.join(AGENTS)})")
    from verinoda.agents.installer import resolve_launcher

    project_dir = Path(project_dir).resolve()
    if not agents:
        return {"ok": False, "action": op, "dry_run": dry_run, "results": [],
                "error": "no agent found on PATH (claude, codex): name them with --agents claude,codex"}
    launcher = resolve_launcher()
    results = []
    for a in agents:
        r = {"agent": a, "scope": scope, "hooks": _hooks_one(op, a, scope, project_dir, home, dry_run, launcher),
             "skills": _skills_one(op, a, scope, project_dir, home, dry_run, launcher) if with_skills else [],
             "notes": []}
        if a == "codex" and op == "install":
            r["notes"].append("Codex runs hooks only with `[features] codex_hooks = true` in its config.toml"
                              + (" and in a trusted project" if scope == "project" else "")
                              + ": add that yourself, this command never edits that file")
        if a == "claude" and scope == "project" and op == "install":
            r["notes"].append(".claude/settings.json is usually committed: the hooks run for everyone who opens "
                              "the project and has `verinoda` installed (they do nothing in a project with no "
                              ".verinoda index); use --scope user to keep them to yourself")
        results.append(r)
    bad_res = [x for r in results for x in [r["hooks"], *r["skills"]] if x["result"] == "refused"]
    return {"ok": not bad_res, "action": op, "dry_run": dry_run, "results": results}


def render(res: dict) -> str:
    if res.get("error"):
        return f"error: {res['error']}\n"
    out = []
    for r in res["results"]:
        h = r["hooks"]
        out.append(f"{r['agent']} ({r['scope']}) hooks: {h['result']}  {h['path']}" + (f"  {h['error']}" if h.get("error") else ""))
        out += [f"  warning: {w}" for w in h.get("warnings") or []]
        for s in r["skills"]:
            out.append(f"{r['agent']} ({r['scope']}) skill {s['skill']}: {s['result']}  {s['path']}"
                       + (f"  {s['error']}" if s.get("error") else ""))
        out += [f"  note: {n}" for n in r["notes"]]
    if res.get("dry_run"):
        out.append("dry run: nothing was written")
    return "\n".join(out) + "\n"
