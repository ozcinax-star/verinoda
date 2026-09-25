"""Install / uninstall the Verinoda skill and MCP server for Claude Code and Codex.

Locations (checked against the agents' current docs and CLIs, see the module
tests and docs):

=====================  =========================================  ==========================================
agent / scope          skill                                      MCP server entry
=====================  =========================================  ==========================================
claude / project       <project>/.claude/skills/verinoda/SKILL.md  <project>/.mcp.json ``mcpServers.verinoda``
claude / user          ~/.claude/skills/verinoda/SKILL.md          ``claude mcp add --scope user`` (~/.claude.json)
codex  / project       <project>/.agents/skills/verinoda/SKILL.md  <project>/.codex/config.toml (trusted projects)
codex  / user          ~/.agents/skills/verinoda/SKILL.md          ~/.codex/config.toml (or $CODEX_HOME)
=====================  =========================================  ==========================================

Safety rules:

* a SKILL.md without the ``<!-- verinoda-managed`` marker is never overwritten;
* shared configs are edited key-wise: only the ``verinoda`` server entry is
  added, replaced or removed, everything else keeps its bytes;
* every change is recorded in ``<root>/.verinoda/install-manifest.json``
  (path, sha256 of what was written, config keys, external commands);
* uninstall removes only manifest-listed items that are unchanged since
  install (hash match), and only directories the install created;
* ``dry_run`` plans the same changes and writes nothing.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from verinoda.agents import _jsonedit as je
from verinoda.agents import _tomledit as te

AGENTS = ("claude", "codex")
SCOPES = ("project", "user")
NAME = "verinoda"
MARKER = "<!-- verinoda-managed v1 -->"
MARKER_PREFIX = b"<!-- verinoda-managed"
MANIFEST_NAME = "install-manifest.json"
MANIFEST_VERSION = 1
TEMPLATES = Path(__file__).parent / "templates"
CLI_PLACEHOLDER = "{{VERINODA_CLI}}"
CLI_NOTE_PLACEHOLDER = "{{VERINODA_CLI_NOTE}}"
# Codex's default MCP start-up timeout is short; a Python server can be slower
# to start on Windows (cold imports), so give it more room.
CODEX_STARTUP_TIMEOUT = 30
# Characters cmd.exe would reinterpret when a .cmd/.bat shim is launched.
_CMD_META = set('%^&|<>"!\r\n')


# -- seams patched by tests -------------------------------------------------------

def _which(name: str) -> str | None:
    # On Windows an npm-installed CLI ships both an extension-less POSIX shim and
    # a .cmd launcher; older Pythons (3.12.0) may return the shim first, which
    # CreateProcess cannot run (WinError 193). Prefer real Windows launchers.
    if os.name == "nt" and not Path(name).suffix:
        for ext in (".exe", ".cmd", ".bat"):
            hit = shutil.which(name + ext)
            if hit:
                return hit
    return shutil.which(name)


def _run(argv: list[str], env: dict | None = None, timeout: float = 120) -> dict:
    """Run an external command (never through a shell)."""
    try:
        p = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           env=env, timeout=timeout)
        return {"returncode": p.returncode, "stdout": (p.stdout or "")[-600:], "stderr": (p.stderr or "")[-600:]}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"returncode": None, "stdout": "", "stderr": str(exc)}


# -- small helpers ---------------------------------------------------------------

def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _sha(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _entry_hash(entry: dict) -> str:
    return _sha(json.dumps(entry, sort_keys=True, separators=(",", ":"), ensure_ascii=False))


def _rel(root: Path, p: Path) -> str:
    try:
        return p.relative_to(root).as_posix()
    except ValueError:
        return str(p)


def _abs(root: Path, s: str) -> Path:
    p = Path(s)
    return p if p.is_absolute() else root / p


def _missing_dirs(path: Path, stop: Path) -> list[Path]:
    """Ancestors of ``path`` that do not exist yet (shallowest first), not above ``stop``."""
    out: list[Path] = []
    d = path.parent
    while not d.exists() and d != stop and d != d.parent:
        out.append(d)
        d = d.parent
    return list(reversed(out))


def _read_text(path: Path) -> tuple[str, bool]:
    raw = path.read_bytes()
    bom = raw.startswith(b"\xef\xbb\xbf")
    return raw[3 if bom else 0:].decode("utf-8"), bom


def _encode(text: str, bom: bool) -> bytes:
    return (b"\xef\xbb\xbf" if bom else b"") + text.encode("utf-8")


def _write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.verinoda-tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _display(argv: list[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(argv)
    import shlex

    return shlex.join(argv)


def _batch_unsafe(exe: str, args: list[str]) -> bool:
    return Path(exe).suffix.lower() in (".cmd", ".bat") and any(set(a) & _CMD_META for a in args)


def _version() -> str:
    from verinoda import __version__

    return __version__


# -- targets -----------------------------------------------------------------------

@dataclass
class Target:
    agent: str
    scope: str
    root: Path          # project dir or home; manifest paths are relative to it
    home: Path
    explicit_home: bool
    project_dir: Path
    skill: Path
    mcp_kind: str       # "json" | "toml" | "claude_cli"
    mcp_path: Path      # file holding the server entry (claude_cli: ~/.claude.json, read only)
    manifest: Path


def _claude_user_config(home: Path, explicit_home: bool) -> Path:
    if not explicit_home and os.environ.get("CLAUDE_CONFIG_DIR"):
        return Path(os.environ["CLAUDE_CONFIG_DIR"]) / ".claude.json"
    return home / ".claude.json"


def _codex_home(home: Path, explicit_home: bool) -> Path:
    if not explicit_home and os.environ.get("CODEX_HOME"):
        return Path(os.environ["CODEX_HOME"])
    return home / ".codex"


def _target(agent: str, scope: str, project_dir: Path, home: Path, explicit_home: bool) -> Target:
    root = project_dir if scope == "project" else home
    if agent == "claude":
        skill = root / ".claude" / "skills" / NAME / "SKILL.md"
        if scope == "project":
            kind, mcp = "json", project_dir / ".mcp.json"
        else:
            kind, mcp = "claude_cli", _claude_user_config(home, explicit_home)
    else:
        skill = root / ".agents" / "skills" / NAME / "SKILL.md"
        kind = "toml"
        mcp = (project_dir / ".codex" / "config.toml" if scope == "project"
               else _codex_home(home, explicit_home) / "config.toml")
    return Target(agent, scope, root, home, explicit_home, project_dir, skill, kind, mcp,
                  root / ".verinoda" / MANIFEST_NAME)


# -- which program the agents start ------------------------------------------------------

PKG_DIR = Path(__file__).resolve().parents[1]
# The config file a project-scope Claude Code server finds its project by (`mcp serve --repo-of`):
# Claude Code reads .mcp.json from the session folder and every folder above it and starts stdio
# servers in the session folder, so no absolute project path has to be stored.
CLAUDE_PROJECT_CONFIG = ".mcp.json"
# `#!C:\...\python.exe` inside a Windows console-script launcher (pip/distlib and uv write one).
_EXE_SHEBANG = re.compile(rb'#!\s*"?([A-Za-z]:[\\/][^\r\n"]*?pythonw?[0-9.]*\.exe)"?[ \t]*\r?\n', re.I)
_IMPORT_CACHE: dict[str, str | None] = {}


def _norm(p) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(str(p))))


def launcher_python(path) -> str | None:
    """The Python interpreter a ``verinoda`` console-script launcher starts, read from the file (never run).

    POSIX scripts: the shebang line, including pip's ``#!/bin/sh`` + ``'''exec' <python>`` form for
    long paths. Windows ``.exe`` launchers (pip/distlib, uv): the ``#!<python.exe>`` line they embed.
    ``#!/usr/bin/env python3`` depends on PATH when it starts, and an unreadable file is not guessed:
    both give None.
    """
    try:
        p = Path(path)
        st = p.stat()
        # a path from a config file: only a regular file of launcher size is read (never a device or pipe)
        if not stat.S_ISREG(st.st_mode) or st.st_size > 16 * 1024 * 1024:
            return None
        data = p.read_bytes()
    except OSError:
        return None
    if data.startswith(b"#!"):
        first, _, rest = data.partition(b"\n")
        interp = first[2:].strip().decode("utf-8", "replace")
        if interp in ("/bin/sh", "/usr/bin/sh"):
            m = re.match(rb"'''exec' (?:\"([^\"]+)\"|'([^']+)'|(\S+))", rest)
            return (m.group(1) or m.group(2) or m.group(3)).decode("utf-8", "replace") if m else None
        if interp.startswith('"'):
            end = interp.find('"', 1)
            return interp[1:end] if end > 1 else None
        tok = interp.split()[0] if interp.split() else ""
        return None if not tok or os.path.basename(tok) == "env" else tok
    m = _EXE_SHEBANG.search(data)
    return m.group(1).decode("utf-8", "replace") if m else None


def _import_location(python: str) -> str | None:
    """Folder of the ``verinoda`` package ``python`` imports when an agent starts it: from a neutral
    folder and without this process's PYTHONPATH (``-E``). None when it cannot import it. Only the
    running interpreter (``sys.executable``) is ever passed here."""
    if python in _IMPORT_CACHE:
        return _IMPORT_CACHE[python]
    code = "import os, verinoda; print(os.path.dirname(os.path.abspath(verinoda.__file__)))"
    try:
        p = subprocess.run([python, "-E", "-c", code], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", cwd=tempfile.gettempdir(), timeout=60, stdin=subprocess.DEVNULL)
        lines = (p.stdout or "").strip().splitlines()
        out = lines[-1].strip() if p.returncode == 0 and lines else None
    except (OSError, subprocess.SubprocessError):
        out = None
    _IMPORT_CACHE[python] = out
    return out


def _module_argv(python: str) -> list[str]:
    # -P (Python 3.11+): the folder the agent starts the server in - any project - is not put on
    # sys.path, so a project's own `verinoda/` folder can never stand in for the installed package
    return [python, *(["-P"] if sys.version_info >= (3, 11) else []), "-m", NAME]


def resolve_launcher() -> dict:
    """The program agents start for this build, and why.

    The ``verinoda`` on PATH is registered only when it is this build: its launcher starts this
    interpreter, and that interpreter imports this package when an agent starts it. Otherwise the
    running interpreter is registered (``<python> -m verinoda``) and the result says what the PATH
    one is. Returns ``{"argv", "how" ("path" | "interpreter"), "cli", "note", "warnings", "python",
    "package", "imports", "path_exe", "path_python"}``; nothing is run apart from this interpreter's
    import check.
    """
    py = sys.executable or ""
    loc = _import_location(py) if py else None
    runs_ours = loc is not None and _norm(loc) == _norm(PKG_DIR)
    found = _which(NAME)
    path_exe = os.path.abspath(found) if found else None
    path_py = launcher_python(path_exe) if path_exe else None
    out = {"python": py or None, "package": str(PKG_DIR), "imports": loc, "path_exe": path_exe,
           "path_python": path_py, "warnings": []}
    same_env = bool(path_exe and py) and (
        _norm(path_py) == _norm(py) if path_py else _norm(Path(path_exe).parent) == _norm(Path(py).parent))
    if path_exe and same_env and runs_ours:
        out.update(argv=[path_exe], how="path", cli=NAME,
                   note=f"registered `verinoda` on PATH ({path_exe}): it runs this build ({py})")
        return out
    if not py:  # an embedded interpreter without sys.executable: nothing better than the PATH program
        argv = [path_exe] if path_exe else [NAME]
        out.update(argv=argv, how="path", cli=NAME,
                   note=f"registered {argv[0]}: the running interpreter's path is not known")
        out["warnings"].append("the running Python interpreter's path is not known (sys.executable is empty); "
                               f"registered {argv[0]}, which may be another Verinoda build")
        return out
    argv = _module_argv(py)
    if not path_exe:
        why = "`verinoda` is not on PATH"
    elif not same_env:
        why = (f"`verinoda` on PATH ({path_exe}) starts {path_py}, another installation" if path_py else
               f"`verinoda` on PATH ({path_exe}) is a launcher whose interpreter could not be read")
    else:
        why = f"`verinoda` on PATH ({path_exe}) imports {loc or 'no verinoda package'}, not this build"
    out.update(argv=argv, how="interpreter", cli=_display(argv),
               note=f"registered the running interpreter ({_display(argv)}): {why}")
    if path_exe and not (same_env and runs_ours):
        out["warnings"].append(
            f"`verinoda` on PATH ({path_exe}) is not the build running this command ({PKG_DIR}); the agent "
            f"configs name the running interpreter instead ({_display(argv)}), and so does the skill. Upgrade "
            "or remove the PATH install to use one build everywhere.")
    if not runs_ours:
        from verinoda.buildinfo import is_source_root

        how = (f" (from its source folder: `{_display([py, '-m', 'pip', 'install', '-e', str(PKG_DIR.parent)])}`)"
               if is_source_root(PKG_DIR.parent) else "")
        out["warnings"].append(
            f"`{_display(argv)}` started by an agent imports {loc or 'no verinoda package'}, not this build at "
            f"{PKG_DIR} (imported here through PYTHONPATH or the current folder), so the server will run that "
            f"one. Install this build into that interpreter{how} and re-run.")
    parts = {s.lower() for s in Path(py).parts}
    if "archive-v0" in parts or ("uv" in parts and "cache" in parts):
        out["warnings"].append(
            f"{py} lives in uv's cache (a `uvx` run): `uv cache clean` deletes it and the registered server stops "
            "starting. Install with `uv tool install` and re-run setup.")
    return out


def entry_python(entry: dict | None) -> str | None:
    """The interpreter a registered server entry starts, read statically: the command itself for
    ``<python> [-P] -m verinoda``, the launcher's shebang otherwise (a bare name is looked up on PATH
    now; the agent may see another PATH)."""
    if not isinstance(entry, dict) or not entry.get("command"):
        return None
    cmd = str(entry["command"])
    args = [str(a) for a in entry.get("args") or []]
    exe = cmd if os.path.dirname(cmd) else (_which(cmd) or cmd)
    if "-m" in args and args[args.index("-m") + 1: args.index("-m") + 2] == [NAME]:
        return exe
    return launcher_python(exe)


# -- what gets written -----------------------------------------------------------------

def server_command(scope: str, project_dir: Path, agent: str | None = None,
                   launcher: dict | None = None) -> list[str]:
    """Command line that starts the Verinoda MCP server for this scope.

    Project scope for Claude Code stores no project path (``--repo-of .mcp.json``, see
    :data:`CLAUDE_PROJECT_CONFIG`); Codex project configs keep ``--repo <project>`` because where
    Codex starts its servers is not verified."""
    base = list((launcher or resolve_launcher())["argv"])
    if scope != "project":
        return base + ["mcp", "serve"]
    if agent == "claude":
        return base + ["mcp", "serve", "--repo-of", CLAUDE_PROJECT_CONFIG]
    return base + ["mcp", "serve", "--repo", str(project_dir)]


def cli_hint(launcher: dict | None = None) -> str:
    """How the skill tells the agent to run the CLI: ``verinoda`` when the PATH one is this build."""
    return (launcher or resolve_launcher())["cli"]


def render_skill(agent: str, launcher: dict | None = None) -> bytes:
    tpl = (TEMPLATES / f"{agent}_SKILL.md").read_text(encoding="utf-8")
    cli = cli_hint(launcher)
    note = "" if cli == NAME else (" It is not the `verinoda` on PATH (another build): run it wherever this "
                                   "skill says `verinoda`.")
    text = tpl.replace("\r\n", "\n").replace(CLI_PLACEHOLDER, cli).replace(CLI_NOTE_PLACEHOLDER, note)
    if MARKER not in text:  # pragma: no cover - template invariant, tested
        raise RuntimeError(f"template for {agent} lacks the ownership marker")
    return text.encode("utf-8")


# -- manifest ---------------------------------------------------------------------------

def _empty_manifest(t: Target) -> dict:
    return {"version": MANIFEST_VERSION, "tool": NAME, "scope": t.scope, "root": str(t.root),
            "created": {"dirs": [], "files": []}, "installs": {}}


def _load_manifest(t: Target) -> dict:
    if not t.manifest.exists():
        return _empty_manifest(t)
    data = json.loads(t.manifest.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("installs"), dict):
        raise ValueError("unrecognised manifest format")
    data.setdefault("created", {"dirs": [], "files": []})
    return data


def _save_manifest(t: Target, manifest: dict) -> None:
    d = t.manifest.parent
    if not d.exists():
        created = manifest.setdefault("created", {"dirs": [], "files": []})
        created["dirs"].append(_rel(t.root, d))
        if t.scope == "project":
            from verinoda.paths import ensure_atlas

            ensure_atlas(t.root)  # also keeps .verinoda/ out of the project's git
            gi = d / ".gitignore"
            if gi.exists():
                created["files"].append({"path": _rel(t.root, gi), "sha256": _sha(gi.read_bytes())})
        else:
            d.mkdir(parents=True)
    _write_bytes(t.manifest, (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))


def _drop_manifest(t: Target, manifest: dict) -> list[str]:
    """Delete the manifest and the support dirs/files the install created, if nothing else is there."""
    removed = []
    t.manifest.unlink()
    removed.append(str(t.manifest))
    created = manifest.get("created") or {}
    owned = {}
    for f in created.get("files", []):
        p = _abs(t.root, f["path"])
        if p.exists() and _sha(p.read_bytes()) == f["sha256"]:
            owned[p] = f
    for rel in reversed(created.get("dirs", [])):
        d = _abs(t.root, rel)
        if not d.is_dir():
            continue
        rest = [c for c in d.iterdir() if c not in owned]
        if rest:
            continue
        for c in list(d.iterdir()):
            c.unlink()
            removed.append(str(c))
        try:
            d.rmdir()
            removed.append(str(d))
        except OSError:
            pass
    return removed


def _prev(items: list[dict], kind: str, path: str | None = None) -> dict | None:
    for it in items:
        if it.get("kind") == kind and (path is None or it.get("path") == path):
            return it
    return None


# -- plan ---------------------------------------------------------------------------------

@dataclass
class Plan:
    actions: list[dict] = field(default_factory=list)
    ops: list[tuple[Callable[[], dict | None], dict | None]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    refusals: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    manual: list[str] = field(default_factory=list)

    def act(self, op: str, what: str, path, detail: str = "") -> None:
        self.actions.append({"op": op, "what": what, "path": str(path), "detail": detail})

    def refuse(self, what: str, path, msg: str) -> None:
        self.act("refuse", what, path, msg)
        self.refusals.append(msg)

    def keep(self, item: dict) -> None:
        """Record ``item`` in the manifest without changing anything on disk."""
        self.ops.append((lambda: item, item))


def _plan_skill(plan: Plan, t: Target, prev_items: list[dict], launcher: dict | None = None) -> None:
    data = render_skill(t.agent, launcher)
    rel = _rel(t.root, t.skill)
    prev = _prev(prev_items, "file", rel)
    item = {"kind": "file", "role": "skill", "path": rel, "sha256": _sha(data),
            "created_dirs": list(prev.get("created_dirs", [])) if prev else []}
    if not t.skill.exists():
        item["created_dirs"] = [_rel(t.root, d) for d in _missing_dirs(t.skill, t.root)]
        plan.act("create", "skill", t.skill)
        plan.ops.append((lambda: (_write_bytes(t.skill, data), item)[1], item))
        return
    cur = t.skill.read_bytes()
    if MARKER_PREFIX not in cur:
        plan.refuse("skill", t.skill, f"{t.skill} already exists and is not managed by Verinoda (no "
                    f"'{MARKER}' marker); refusing to overwrite it. Move or rename it, then re-run install.")
        return
    if cur == data:
        plan.act("unchanged", "skill", t.skill)
        plan.keep(item)
        return
    if prev and _sha(cur) != prev.get("sha256"):
        plan.refuse("skill", t.skill, f"{t.skill} was edited after Verinoda installed it; refusing to "
                    "overwrite local edits. Delete it (or undo the edits) and re-run install.")
        return
    plan.act("update", "skill", t.skill, "managed copy, unchanged since install" if prev
             else "carries the Verinoda marker")
    plan.ops.append((lambda: (_write_bytes(t.skill, data), item)[1], item))


def _json_set(text: str, entry: dict, expected: dict) -> tuple[str, bool]:
    try:
        new = je.set_member(text, ("mcpServers", NAME), entry)
        if json.loads(new) == expected:
            return new, True
    except (je.JsonEditError, ValueError, IndexError):
        pass
    nl = je.newline_of(text)
    return json.dumps(expected, indent=je.indent_unit(text), ensure_ascii=False).replace("\n", nl) + nl, False


def _plan_json(plan: Plan, t: Target, cmd: list[str], prev_items: list[dict]) -> None:
    entry = {"type": "stdio", "command": cmd[0], "args": cmd[1:]}
    p, rel = t.mcp_path, _rel(t.root, t.mcp_path)
    prev = _prev(prev_items, "json_key", rel)
    item = {"kind": "json_key", "role": "mcp", "path": rel, "key": ["mcpServers", NAME],
            "sha256": _entry_hash(entry), "created_file": False, "created_parent": False}
    if not p.exists():
        item.update(created_file=True, created_parent=True)
        text = json.dumps({"mcpServers": {NAME: entry}}, indent=2, ensure_ascii=False) + "\n"
        plan.act("create", "mcp", p, f"mcpServers.{NAME}")
        plan.ops.append((lambda: (_write_bytes(p, text.encode("utf-8")), item)[1], item))
        return
    try:
        text, bom = _read_text(p)
        data = json.loads(text)
    except (UnicodeDecodeError, ValueError) as exc:
        plan.refuse("mcp", p, f"{p} is not valid JSON ({exc}); not touching it. Fix it, or install with --no-mcp.")
        return
    if not isinstance(data, dict) or not isinstance(data.get("mcpServers", {}), dict):
        plan.refuse("mcp", p, f"{p} has an unexpected shape (need an object with an 'mcpServers' object); "
                    "not touching it.")
        return
    if prev:
        item.update(created_file=prev.get("created_file", False), created_parent=prev.get("created_parent", False))
    else:
        item["created_parent"] = "mcpServers" not in data
    cur = data.get("mcpServers", {}).get(NAME)
    if cur == entry:
        plan.act("unchanged", "mcp", p, f"mcpServers.{NAME}")
        if prev:
            plan.keep(item)
        else:
            plan.notes.append(f"{p} already had an identical '{NAME}' entry that Verinoda did not add; "
                              "uninstall will leave it.")
        return
    if cur is not None:
        if not prev:
            plan.refuse("mcp", p, f"{p} already has an 'mcpServers.{NAME}' entry that Verinoda did not add; "
                        "refusing to replace it. Remove that entry, or install with --no-mcp.")
            return
        if _entry_hash(cur) != prev.get("sha256"):
            plan.refuse("mcp", p, f"the 'mcpServers.{NAME}' entry in {p} was edited after install; "
                        "refusing to overwrite it. Remove it, or install with --no-mcp.")
            return
    expected = copy.deepcopy(data)
    expected.setdefault("mcpServers", {})[NAME] = entry
    new, spliced = _json_set(text, entry, expected)
    plan.act("update" if cur is not None else "create", "mcp", p,
             f"mcpServers.{NAME}; other keys preserved" + ("" if spliced else " (file re-indented)"))
    if not spliced:
        plan.warnings.append(f"{p}: could not splice the entry in place; the file was re-serialised "
                             "(values and key order kept, whitespace may differ).")
    plan.ops.append((lambda: (_write_bytes(p, _encode(new, bom)), item)[1], item))


def _plan_toml(plan: Plan, t: Target, cmd: list[str], prev_items: list[dict]) -> None:
    entry = {"command": cmd[0], "args": cmd[1:], "startup_timeout_sec": CODEX_STARTUP_TIMEOUT}
    p, rel = t.mcp_path, _rel(t.root, t.mcp_path)
    prev = _prev(prev_items, "toml_block", rel)
    item = {"kind": "toml_block", "role": "mcp", "path": rel, "key": f"mcp_servers.{NAME}",
            "sha256": "", "created_file": False, "created_dirs": [], "added_eol": False, "added_separator": False}
    if prev:
        for k in ("created_file", "created_dirs", "added_eol", "added_separator"):
            item[k] = prev.get(k, item[k])
    if not p.exists():
        block = te.render_block(NAME, entry, "\n")
        item.update(sha256=_sha(te.block_key(block)), created_file=True,
                    created_dirs=[_rel(t.root, d) for d in _missing_dirs(p, t.root)],
                    added_eol=False, added_separator=False)
        plan.act("create", "mcp", p, f"[mcp_servers.{NAME}]")
        plan.ops.append((lambda: (_write_bytes(p, block.encode("utf-8")), item)[1], item))
        return
    try:
        text, bom = _read_text(p)
        data = te.loads(text)
    except (UnicodeDecodeError, te.TOMLDecodeError) as exc:
        plan.refuse("mcp", p, f"{p} is not valid TOML ({exc}); not touching it. Fix it, or install with --no-mcp.")
        return
    nl = je.newline_of(text)
    block = te.render_block(NAME, entry, nl)
    item["sha256"] = _sha(te.block_key(block))
    servers = data.get("mcp_servers") if isinstance(data.get("mcp_servers"), dict) else {}
    m = te.find_block(text)
    if m:
        cur = m.group(0)
        if te.block_key(cur) == te.block_key(block):
            plan.act("unchanged", "mcp", p, f"[mcp_servers.{NAME}]")
            plan.keep(item)
            return
        if prev and _sha(te.block_key(cur)) != prev.get("sha256"):
            plan.refuse("mcp", p, f"the Verinoda block in {p} was edited after install; refusing to overwrite "
                        "it. Remove the block, or install with --no-mcp.")
            return
        new = text[: m.start()] + block + text[m.end():]
        op = "update"
    else:
        if NAME in servers:
            plan.refuse("mcp", p, f"{p} already defines [mcp_servers.{NAME}] outside a Verinoda block; "
                        "refusing to replace it. Remove it, or install with --no-mcp.")
            return
        new, added_eol, added_sep = te.append_block(text, block, nl)
        item.update(added_eol=added_eol, added_separator=added_sep)
        op = "create"
    try:
        nd = te.loads(new)
        ok = te.without(nd, NAME) == te.without(data, NAME) and nd.get("mcp_servers", {}).get(NAME) == entry
    except te.TOMLDecodeError:
        ok = False
    if not ok:
        plan.refuse("mcp", p, f"cannot add [mcp_servers.{NAME}] to {p} without changing other settings "
                    "(e.g. mcp_servers is defined inline); add this block by hand:\n" + block)
        return
    plan.act(op, "mcp", p, f"[mcp_servers.{NAME}] in a delimited block; rest of file untouched")
    plan.ops.append((lambda: (_write_bytes(p, _encode(new, bom)), item)[1], item))


def _read_claude_user_entry(path: Path) -> tuple[dict | None, str | None]:
    if not path.exists():
        return None, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return None, f"could not read {path}: {exc}"
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    cur = servers.get(NAME) if isinstance(servers, dict) else None
    return (cur if isinstance(cur, dict) else None), None


def _claude_local_entries(path: Path, project_dir: Path) -> list[tuple[str, dict]]:
    """``verinoda`` servers registered with ``claude mcp add --scope local`` for ``project_dir`` or a folder above it.

    Claude Code keys local-scope servers by the folder a session was started in (under
    ``projects`` in ~/.claude.json), and such an entry overrides the user-scope one there.
    Read only; the installer never writes local scope.
    """
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return []
    projects = data.get("projects") if isinstance(data, dict) else None
    if not isinstance(projects, dict):
        return []

    def norm(p) -> str:
        return os.path.normcase(os.path.normpath(str(p)))

    want = {norm(p) for p in (project_dir, *project_dir.parents)}
    out = []
    for folder, conf in projects.items():
        servers = conf.get("mcpServers") if isinstance(conf, dict) else None
        cur = servers.get(NAME) if isinstance(servers, dict) else None
        if isinstance(cur, dict) and norm(folder) in want:
            out.append((folder, cur))
    return sorted(out)


def _claude_matches(cur: dict | None, command: str, args: list[str]) -> bool:
    return (isinstance(cur, dict) and cur.get("command") == command and list(cur.get("args") or []) == list(args)
            and cur.get("type", "stdio") == "stdio")


def _cli_env(t: Target) -> dict | None:
    """Point the claude CLI at ``t.home`` when a non-default home was given."""
    if not t.explicit_home:
        return None
    env = dict(os.environ)
    env["HOME"] = env["USERPROFILE"] = str(t.home)
    env.pop("CLAUDE_CONFIG_DIR", None)
    return env


ADD_TAIL = ["mcp", "add", "--scope", "user", NAME, "--"]
REMOVE_TAIL = ["mcp", "remove", "--scope", "user", NAME]


def _plan_claude_cli(plan: Plan, t: Target, cmd: list[str], prev_items: list[dict]) -> None:
    prev = _prev(prev_items, "command")
    cur, err = _read_claude_user_entry(t.mcp_path)
    if err:
        plan.warnings.append(err)
    entry = {"command": cmd[0], "args": cmd[1:]}
    item = {"kind": "command", "role": "mcp", "config": str(t.mcp_path), "key": f"mcpServers.{NAME}",
            "argv": ["claude", *ADD_TAIL, *cmd], "undo_argv": ["claude", *REMOVE_TAIL],
            "entry": entry, "sha256": _entry_hash(entry)}
    if _claude_matches(cur, cmd[0], cmd[1:]):
        plan.act("unchanged", "mcp", t.mcp_path, f"user-scope server '{NAME}'")
        if prev:
            plan.keep(prev)
        else:
            plan.notes.append(f"a user-scope '{NAME}' MCP server identical to ours already existed; "
                              "uninstall will leave it.")
        return
    steps = [ADD_TAIL + cmd]
    op = "create"
    if cur is not None:
        pe = (prev or {}).get("entry") or {}
        if not prev or not _claude_matches(cur, pe.get("command"), pe.get("args") or []):
            plan.refuse("mcp", t.mcp_path, f"a user-scope '{NAME}' MCP server already exists in {t.mcp_path} and "
                        "was not added by Verinoda (or was changed since); refusing to replace it. Remove it "
                        f"with `claude mcp remove --scope user {NAME}`, or install with --no-mcp.")
            return
        steps = [REMOVE_TAIL, ADD_TAIL + cmd]
        op = "update"
    shown = [_display(["claude", *s]) for s in steps]
    claude = _which("claude")
    if not claude or _batch_unsafe(claude, [a for s in steps for a in s]):
        why = ("the `claude` CLI was not found on PATH" if not claude else
               "the arguments contain characters cmd.exe would reinterpret")
        plan.act("manual", "mcp", t.mcp_path, f"{why}; nothing written to {t.mcp_path.name}")
        plan.warnings.append(f"MCP not registered for Claude Code user scope: {why}. Run the command(s) below.")
        plan.manual.extend(shown)
        return
    plan.act("run", "mcp", t.mcp_path, " && ".join(shown))

    def apply() -> dict | None:
        runs = []
        for s in steps:
            r = _run([claude, *s], env=_cli_env(t))
            runs.append({"argv": ["claude", *s], "returncode": r["returncode"], "at": _now()})
            if r["returncode"] != 0:
                plan.errors.append(f"`{_display(['claude', *s])}` failed (exit {r['returncode']}): "
                                   f"{(r['stderr'] or r['stdout']).strip()[-300:]}")
                return None
        item["runs"] = runs
        after, _ = _read_claude_user_entry(t.mcp_path)
        if not _claude_matches(after, cmd[0], cmd[1:]):
            plan.warnings.append(f"`claude mcp add` succeeded but the entry is not visible in {t.mcp_path}; "
                                 f"check with `claude mcp get {NAME}`.")
        return item

    plan.ops.append((apply, item))


# -- results -------------------------------------------------------------------------------

def _base(operation: str, agent: str, scope: str, project_dir, home, dry_run: bool) -> dict:
    return {"ok": True, "operation": operation, "agent": agent, "scope": scope, "dry_run": dry_run,
            "result": "", "project_dir": str(project_dir), "home": str(home) if scope == "user" else None,
            "root": str(project_dir if scope == "project" else home), "skill": None, "mcp_config": None, "manifest": None, "actions": [], "warnings": [], "errors": [],
            "manual": [], "notes": []}


def _fail(res: dict, msg: str) -> dict:
    res.update(ok=False, result="error")
    res["errors"].append(msg)
    return res


def _check_args(res: dict, agent: str, scope: str, project_dir: Path, home: Path) -> bool:
    if agent not in AGENTS:
        _fail(res, f"unknown agent {agent!r} (choose from {', '.join(AGENTS)})")
    elif scope not in SCOPES:
        _fail(res, f"unknown scope {scope!r} (choose from {', '.join(SCOPES)})")
    elif scope == "project" and not project_dir.is_dir():
        _fail(res, f"project directory {project_dir} does not exist")
    elif scope == "user" and not home.is_dir():
        _fail(res, f"home directory {home} does not exist")
    return res["ok"]


def _usage_notes(t: Target, with_mcp: bool) -> list[str]:
    notes = []
    if t.agent == "claude":
        notes.append(f"Claude Code: type /{NAME} <question> (the text after the command is passed to the skill "
                     "as $ARGUMENTS); Claude can also load the skill by itself when a request matches it. "
                     "Start a new session if it does not appear.")
        if with_mcp and t.scope == "project":
            notes.append("Claude Code asks for approval before starting servers from a project .mcp.json "
                         f"(check /mcp). The entry stores no project path (--repo-of {CLAUDE_PROJECT_CONFIG}), so "
                         "moving the project keeps it working, but it names this machine's Verinoda program - "
                         "review before committing.")
    else:
        notes.append(f"Codex: mention ${NAME} in your prompt (or pick it via /skills); Codex may also choose it "
                     f"when a task matches. Codex has no /{NAME} slash command.")
        if with_mcp and t.scope == "project":
            notes.append("Codex reads .codex/config.toml only for trusted projects; if the Verinoda tools are "
                         "missing, trust this project in Codex or install with --scope user.")
        if with_mcp:
            notes.append("Restart Codex to load MCP configuration changes.")
    return notes


def _apply(plan: Plan, prev_items: list[dict]) -> list[dict]:
    """Run the planned ops in order; return the manifest items to record."""
    done: list[dict] = []
    for i, (fn, item) in enumerate(plan.ops):
        try:
            rec = fn()
        except OSError as exc:
            plan.errors.append(f"write failed: {exc}")
            touched = {(it.get("kind"), it.get("path")) for _, it in plan.ops[i:] if it}
            done.extend(p for p in prev_items if (p.get("kind"), p.get("path")) in touched)
            break
        if rec is not None:
            done.append(rec)
    return done


# -- public API ------------------------------------------------------------------------------

def install(agent: str, scope: str, *, project_dir, home=None, with_mcp: bool = True,
            dry_run: bool = False) -> dict:
    """Install the Verinoda skill (and MCP server unless ``with_mcp=False``) for one agent/scope."""
    project_dir = Path(project_dir).resolve()
    explicit_home = home is not None
    home = Path(home).resolve() if home is not None else Path.home()
    res = _base("install", agent, scope, project_dir, home, dry_run)
    if not _check_args(res, agent, scope, project_dir, home):
        return res
    t = _target(agent, scope, project_dir, home, explicit_home)
    res.update(skill=str(t.skill), mcp_config=str(t.mcp_path) if with_mcp else None, manifest=str(t.manifest))
    try:
        manifest = _load_manifest(t)
    except (OSError, ValueError) as exc:
        return _fail(res, f"cannot read {t.manifest} ({exc}); not changing anything.")
    prev = manifest["installs"].get(agent) or {}
    prev_items = prev.get("items") or []

    plan = Plan()
    launcher = resolve_launcher()
    plan.warnings.extend(launcher["warnings"])
    res["server"] = {k: launcher[k] for k in ("how", "note", "python", "package", "imports", "path_exe",
                                              "path_python")}
    _plan_skill(plan, t, prev_items, launcher)
    if with_mcp:
        cmd = server_command(scope, project_dir, agent, launcher)
        res["server"]["command"] = cmd
        {"json": _plan_json, "toml": _plan_toml, "claude_cli": _plan_claude_cli}[t.mcp_kind](plan, t, cmd, prev_items)
    else:
        for it in prev_items:
            if it.get("role") == "mcp":
                plan.keep(it)
                plan.notes.append("MCP registration from an earlier install left in place (use uninstall to remove).")
    res["actions"], res["warnings"], res["manual"] = plan.actions, plan.warnings, plan.manual
    if plan.refusals:
        res["errors"].extend(plan.refusals)
        res.update(ok=False, result="refused")
        res["notes"].append("nothing was written.")
        return res
    res["notes"] = plan.notes + _usage_notes(t, with_mcp)
    ops = {a["op"] for a in plan.actions} - {"manual"}
    res["result"] = ("unchanged" if ops <= {"unchanged"} else "updated" if "update" in ops else "installed")
    if dry_run:
        return res

    items = _apply(plan, prev_items)
    from verinoda.buildinfo import build_info

    entry = {"scope": scope, "root": str(t.root), "installed_at": prev.get("installed_at") or _now(),
             "updated_at": prev.get("updated_at") or _now(), "verinoda_version": _version(),
             "verinoda_build": build_info()["build"],
             "with_mcp": bool(with_mcp) or any(i.get("role") == "mcp" for i in items), "items": items}
    same = (prev and prev.get("items") == items and prev.get("verinoda_version") == entry["verinoda_version"]
            and prev.get("verinoda_build") == entry["verinoda_build"] and prev.get("with_mcp") == entry["with_mcp"])
    if same and t.manifest.exists():
        plan.act("unchanged", "manifest", t.manifest)
    elif items:
        if prev:
            entry["updated_at"] = _now()
        manifest["installs"][agent] = entry
        existed = t.manifest.exists()
        try:
            _save_manifest(t, manifest)
            plan.act("update" if existed else "create", "manifest", t.manifest)
        except OSError as exc:
            plan.errors.append(f"could not write manifest {t.manifest}: {exc}")
    res["errors"].extend(plan.errors)
    if plan.errors:
        res.update(ok=False, result="error")
    return res


def _un_dirs(plan: Plan, t: Target, rels: list[str]) -> list[Path]:
    dirs = [_abs(t.root, r) for r in rels]
    for d in reversed(dirs):
        plan.act("remove", "dir", d, "only if empty (created by install)")
    return dirs


def _rmdirs(dirs: list[Path]) -> None:
    for d in reversed(dirs):
        try:
            d.rmdir()
        except OSError:
            pass


def _un_file(plan: Plan, t: Target, item: dict, kept: list[dict]) -> None:
    p = _abs(t.root, item["path"])
    if not p.exists():
        plan.act("missing", item.get("role", "file"), p, "already gone")
        return
    if _sha(p.read_bytes()) != item.get("sha256"):
        plan.act("keep", item.get("role", "file"), p, "modified since install")
        plan.warnings.append(f"{p} was modified after install; left in place.")
        kept.append(item)
        return
    plan.act("remove", item.get("role", "file"), p)
    dirs = _un_dirs(plan, t, item.get("created_dirs", []))
    plan.ops.append((lambda: (p.unlink(), _rmdirs(dirs))[0], None))


def _un_json(plan: Plan, t: Target, item: dict, kept: list[dict]) -> None:
    p = _abs(t.root, item["path"])
    if not p.exists():
        plan.act("missing", "mcp", p, "file already gone")
        return
    try:
        text, bom = _read_text(p)
        data = json.loads(text)
        cur = data.get("mcpServers", {}).get(NAME)
    except (UnicodeDecodeError, ValueError, AttributeError) as exc:
        plan.act("keep", "mcp", p, "unreadable")
        plan.warnings.append(f"{p} is not valid JSON ({exc}); left untouched - remove 'mcpServers.{NAME}' by hand.")
        kept.append(item)
        return
    if cur is None:
        plan.act("missing", "mcp", p, f"mcpServers.{NAME} already gone")
        return
    if _entry_hash(cur) != item.get("sha256"):
        plan.act("keep", "mcp", p, f"mcpServers.{NAME} modified since install")
        plan.warnings.append(f"the '{NAME}' entry in {p} was modified after install; left in place.")
        kept.append(item)
        return
    expected = copy.deepcopy(data)
    del expected["mcpServers"][NAME]
    drop_outer = bool(item.get("created_parent")) and not expected["mcpServers"]
    if drop_outer:
        del expected["mcpServers"]
    if item.get("created_file") and expected == {}:
        plan.act("remove", "mcp", p, "file created by install; nothing else in it")
        plan.ops.append((lambda: p.unlink(), None))
        return
    try:
        new = je.remove_member(text, ("mcpServers", NAME), drop_empty_outer=drop_outer)
        if json.loads(new) != expected:
            raise ValueError("splice mismatch")
    except (je.JsonEditError, ValueError, IndexError):
        nl = je.newline_of(text)
        new = json.dumps(expected, indent=je.indent_unit(text), ensure_ascii=False).replace("\n", nl) + nl
        plan.warnings.append(f"{p}: re-serialised while removing the entry (whitespace may differ).")
    plan.act("remove", "mcp", p, f"key mcpServers.{NAME}; other keys preserved")
    plan.ops.append((lambda: _write_bytes(p, _encode(new, bom)), None))


def _un_toml(plan: Plan, t: Target, item: dict, kept: list[dict]) -> None:
    p = _abs(t.root, item["path"])
    if not p.exists():
        plan.act("missing", "mcp", p, "file already gone")
        return
    try:
        text, bom = _read_text(p)
        old = te.loads(text)
    except (UnicodeDecodeError, te.TOMLDecodeError) as exc:
        plan.act("keep", "mcp", p, "unreadable")
        plan.warnings.append(f"{p} is not valid TOML ({exc}); left untouched - remove the Verinoda block by hand.")
        kept.append(item)
        return
    m = te.find_block(text)
    if not m:
        plan.act("missing", "mcp", p, "Verinoda block already gone")
        return
    if _sha(te.block_key(m.group(0))) != item.get("sha256"):
        plan.act("keep", "mcp", p, "Verinoda block modified since install")
        plan.warnings.append(f"the Verinoda block in {p} was modified after install; left in place.")
        kept.append(item)
        return
    nl = je.newline_of(text)
    new = te.remove_block(text, m, nl, added_eol=bool(item.get("added_eol")),
                          added_sep=bool(item.get("added_separator")))
    try:
        nd = te.loads(new)
        ok = te.without(nd, NAME) == te.without(old, NAME) and NAME not in (nd.get("mcp_servers") or {})
    except te.TOMLDecodeError:
        ok = False
    if not ok:
        plan.act("keep", "mcp", p, "removal would change other settings")
        plan.warnings.append(f"removing the Verinoda block from {p} would change other settings; left in place.")
        kept.append(item)
        return
    if item.get("created_file") and not new.strip():
        plan.act("remove", "mcp", p, "file created by install; nothing else in it")
        dirs = _un_dirs(plan, t, item.get("created_dirs", []))
        plan.ops.append((lambda: (p.unlink(), _rmdirs(dirs))[0], None))
        return
    plan.act("remove", "mcp", p, f"[mcp_servers.{NAME}] block; rest of file untouched")
    plan.ops.append((lambda: _write_bytes(p, _encode(new, bom)), None))


def _un_command(plan: Plan, t: Target, item: dict, kept: list[dict]) -> None:
    cfg = t.mcp_path
    cur, err = _read_claude_user_entry(cfg)
    if err:
        plan.warnings.append(err)
    e = item.get("entry") or {}
    if cur is None and not err:
        plan.act("missing", "mcp", cfg, f"user-scope server '{NAME}' already gone")
        return
    if not _claude_matches(cur, e.get("command"), e.get("args") or []):
        plan.act("keep", "mcp", cfg, "user-scope server changed since install" if cur else "cannot verify")
        plan.warnings.append(f"the user-scope '{NAME}' MCP server differs from what install added (or could not "
                             f"be read); left in place. Remove it with `claude mcp remove --scope user {NAME}`.")
        kept.append(item)
        return
    shown = _display(["claude", *REMOVE_TAIL])
    claude = _which("claude")
    if not claude:
        plan.act("manual", "mcp", cfg, "the `claude` CLI was not found on PATH")
        plan.manual.append(shown)
        plan.warnings.append("the user-scope MCP server is still registered: run the command below.")
        kept.append(item)
        return
    plan.act("run", "mcp", cfg, shown)

    def apply():
        r = _run([claude, *REMOVE_TAIL], env=_cli_env(t))
        if r["returncode"] != 0:
            plan.errors.append(f"`{shown}` failed (exit {r['returncode']}): "
                               f"{(r['stderr'] or r['stdout']).strip()[-300:]}")
            kept.append(item)

    plan.ops.append((apply, None))


def uninstall(agent: str, scope: str, *, project_dir, home=None, dry_run: bool = False) -> dict:
    """Remove what :func:`install` recorded in the manifest, if unchanged since install."""
    project_dir = Path(project_dir).resolve()
    explicit_home = home is not None
    home = Path(home).resolve() if home is not None else Path.home()
    res = _base("uninstall", agent, scope, project_dir, home, dry_run)
    if not _check_args(res, agent, scope, project_dir, home):
        return res
    t = _target(agent, scope, project_dir, home, explicit_home)
    res.update(skill=str(t.skill), mcp_config=str(t.mcp_path), manifest=str(t.manifest))
    try:
        manifest = _load_manifest(t)
    except (OSError, ValueError) as exc:
        return _fail(res, f"cannot read {t.manifest} ({exc}); not changing anything.")
    entry = manifest["installs"].get(agent)
    if not entry:
        res["result"] = "nothing_to_uninstall"
        res["notes"].append(f"no Verinoda install for {agent} ({scope}) is recorded in {t.manifest}.")
        try:
            if t.skill.exists() and MARKER_PREFIX in t.skill.read_bytes():
                res["warnings"].append(f"{t.skill} carries the Verinoda marker but is not in the manifest; "
                                       "left in place (delete it by hand if you want it gone).")
        except OSError:
            pass
        return res

    plan = Plan()
    kept: list[dict] = []
    handlers = {"file": _un_file, "json_key": _un_json, "toml_block": _un_toml, "command": _un_command}
    for item in entry.get("items", []):
        h = handlers.get(item.get("kind"))
        if h is None:
            plan.warnings.append(f"unknown manifest item {item.get('kind')!r}; left in place.")
            kept.append(item)
            continue
        h(plan, t, item, kept)
    res["actions"], res["warnings"], res["manual"] = plan.actions, plan.warnings, plan.manual
    removed = any(a["op"] in ("remove", "run") for a in plan.actions)
    res["result"] = ("partially_uninstalled" if kept and removed else "kept_modified" if kept
                     else "uninstalled")
    if dry_run:
        return res
    for fn, _ in plan.ops:
        try:
            fn()
        except OSError as exc:
            plan.errors.append(f"removal failed: {exc}")
    try:
        if kept:
            entry["items"] = kept
            entry["updated_at"] = _now()
            _save_manifest(t, manifest)
            plan.act("update", "manifest", t.manifest, f"{len(kept)} item(s) left in place")
        else:
            del manifest["installs"][agent]
            if manifest["installs"]:
                _save_manifest(t, manifest)
                plan.act("update", "manifest", t.manifest)
            else:
                for i, r in enumerate(_drop_manifest(t, manifest)):
                    plan.act("remove", "manifest" if i == 0 else "support", r,
                             "" if i == 0 else "created by install for the manifest")
    except OSError as exc:
        plan.errors.append(f"could not update manifest {t.manifest}: {exc}")
    res["errors"].extend(plan.errors)
    if plan.errors:
        res.update(ok=False, result="error")
    return res


def _loc(t: Target, p: Path) -> str:
    """Short location: relative to the project, or ``~/...`` for the home directory."""
    try:
        rel = p.relative_to(t.root).as_posix()
    except ValueError:
        return str(p)
    return f"~/{rel}" if t.scope == "user" else rel


def _mcp_status(t: Target) -> tuple[bool, str, dict | None]:
    """(registered, one-line description, the registered entry)."""
    p, loc = t.mcp_path, _loc(t, t.mcp_path)
    try:
        if t.mcp_kind == "json":
            cur = (json.loads(_read_text(p)[0]).get("mcpServers") or {}).get(NAME) if p.exists() else None
            return ((True, f"{loc}: {NAME} -> {_short(cur, t)}", cur) if cur
                    else (False, f"not registered ({loc})", None))
        if t.mcp_kind == "claude_cli":
            cur, err = _read_claude_user_entry(p)
            if err:
                return False, err, None
            return ((True, f"user scope ({loc}): {NAME} -> {_short(cur, t)}", cur) if cur
                    else (False, f"not registered ({loc})", None))
        text = _read_text(p)[0] if p.exists() else ""
        cur = (te.loads(text).get("mcp_servers") or {}).get(NAME)
        if not cur:
            return False, f"not registered ({loc})", None
        how = "Verinoda block" if te.find_block(text) else "not managed by Verinoda"
        trust = "; used only if Codex trusts the project" if t.scope == "project" else ""
        return True, f"{loc} ({how}): {NAME} -> {_short(cur, t)}{trust}", cur
    except (OSError, UnicodeDecodeError, ValueError, AttributeError) as exc:
        return False, f"cannot read {loc}: {exc}", None


def _running() -> dict:
    """The interpreter running this command and whether an agent-started one imports this package."""
    py = sys.executable or ""
    loc = _import_location(py) if py else None
    return {"python": py or None, "runs_ours": loc is not None and _norm(loc) == _norm(PKG_DIR), "imports": loc}


def server_check(entry: dict, t: Target, running: dict) -> dict:
    """What a registered server entry starts, compared with the build running this command (static:
    the command is never run). ``build``: ``this`` | ``other`` | ``unknown``; ``problems``: lines for
    doctor (another build, a project path that is not this project)."""
    out: dict = {"python": entry_python(entry), "problems": []}
    args = [str(a) for a in entry.get("args") or []]
    if t.scope == "project" and "--repo" in args:
        i = args.index("--repo")
        target = args[i + 1] if i + 1 < len(args) else ""
        if target and _norm(target) != _norm(t.project_dir):
            out["serves"] = target
            out["problems"].append(f"it serves {target}, not this project (moved or copied?); run `verinoda setup` "
                                   "(or `verinoda install`) here to update the entry")
    py, run_py = out["python"], running.get("python")
    fix = ("`verinoda setup` here registers this one" if t.scope == "project" else
           f"`verinoda install --agent {t.agent} --scope user` registers this one")
    if py is None or not run_py:
        out["build"] = "unknown"
        out["detail"] = "could not tell which Python the registered command starts"
    elif _norm(py) != _norm(run_py):
        out["build"] = "other"
        out["detail"] = f"starts {py}, not the Python running this command ({run_py})"
        out["problems"].append(f"the registered server starts {py}, not the Python running this command "
                               f"({run_py}), so it may be another Verinoda build; `{_display([py, '-m', NAME, '--version'])}` "
                               f"names it, {fix}")
    elif not running.get("runs_ours"):
        out["build"] = "other"
        out["detail"] = f"starts this Python, which imports {running.get('imports') or 'no verinoda'} when an agent starts it"
        out["problems"].append(f"the registered server starts this Python ({py}), but outside this command it imports "
                               f"{running.get('imports') or 'no verinoda package'}, not this build ({PKG_DIR}); install "
                               "this build into it to serve this one")
    else:
        out["build"] = "this"
        out["detail"] = "runs this build"
    return out


def _short(entry: dict, t: Target) -> str:
    """``verinoda.exe mcp serve --repo <project>``; a --repo pointing elsewhere is shown in full."""
    cmd = entry.get("command") or "?"
    args = [str(a) for a in entry.get("args") or []]
    args = ["<project>" if a == str(t.project_dir) else a for a in args]
    s = " ".join([Path(str(cmd)).name, *args])
    return s if len(s) <= 140 else s[:137] + "..."


def status(project_dir, home=None) -> dict:
    """Installed state per ``agent:scope``: skill file, ownership and MCP registration, and for each
    registered server what it starts compared with the running build (``mcp_server``)."""
    project_dir = Path(project_dir).resolve()
    explicit_home = home is not None
    home = Path(home).resolve() if home is not None else Path.home()
    out: dict = {}
    running: dict | None = None

    def check(entry: dict, t: Target) -> dict:
        nonlocal running
        if running is None:
            running = _running()
        return server_check(entry, t, running)

    for agent in AGENTS:
        for scope in SCOPES:
            t = _target(agent, scope, project_dir, home, explicit_home)
            info: dict = {"installed": False, "skill": None, "skill_target": str(t.skill), "managed": False,
                          "modified": None, "mcp": "", "mcp_registered": False}
            try:
                if t.skill.exists():
                    data = t.skill.read_bytes()
                    info["skill"] = str(t.skill)
                    info["installed"] = MARKER_PREFIX in data
                    if not info["installed"]:
                        info["note"] = "SKILL.md present but not managed by Verinoda"
                    try:
                        items = (_load_manifest(t)["installs"].get(agent) or {}).get("items") or []
                    except (OSError, ValueError):
                        items = []
                    it = _prev(items, "file", _rel(t.root, t.skill))
                    info["managed"] = it is not None
                    if it is not None:
                        info["modified"] = _sha(data) != it.get("sha256")
                info["mcp_registered"], info["mcp"], entry = _mcp_status(t)
                if entry:
                    info["mcp_server"] = check(entry, t)
                if t.mcp_kind == "claude_cli":
                    local = _claude_local_entries(t.mcp_path, project_dir)
                    if local:
                        info["mcp_local"] = [{"folder": f, "server": _short(cur, t), "check": check(cur, t)}
                                             for f, cur in local]
                        info["mcp"] += "".join(f"; local scope for {f}: {NAME} -> {_short(cur, t)} (used instead "
                                               "of the user entry by sessions started there)" for f, cur in local)
            except OSError as exc:
                info["error"] = str(exc)
            out[f"{agent}:{scope}"] = info
    return out


def render(res: dict) -> None:
    """Compact human rendering of an install/uninstall result or a status dict."""
    if "actions" not in res:
        for key, v in res.items():
            state = "installed" if v.get("installed") else "not installed"
            if v.get("modified"):
                state += " (modified)"
            print(f"{key:<15} {state:<24} {v.get('skill') or v.get('skill_target')}")
            print(f"{'':<15} mcp: {v.get('mcp')}")
            if (v.get("mcp_server") or {}).get("detail"):
                print(f"{'':<15} server: {v['mcp_server']['detail']}")
        return
    dry = res.get("dry_run")
    root = res.get("root") or ""
    print(f"{res['operation']} {res['agent']} ({res['scope']}, {root}): {res['result']}"
          + ("  [dry run - nothing written]" if dry else ""))
    for a in res["actions"]:
        op = a["op"]
        if dry and op in ("create", "update", "remove", "run"):
            op = "would " + op
        path = a["path"]
        if root and path.startswith(root):
            path = path[len(root):].lstrip("\\/") or "."
        print(f"  {op:<14} {a['what']:<8} {path}" + (f"  ({a['detail']})" if a.get("detail") else ""))
    if (res.get("server") or {}).get("note"):
        print(f"  server: {res['server']['note']}")
    for w in res.get("warnings", []):
        print(f"  warn: {w}")
    for e in res.get("errors", []):
        print(f"  error: {e}")
    for m in res.get("manual", []):
        print(f"  run manually: {m}")
    for n in res.get("notes", []):
        print(f"  note: {n}")
