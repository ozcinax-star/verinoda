"""Install / uninstall Verinoda for more coding agents: Cursor, Gemini CLI, GitHub Copilot (VS Code), Kiro,
Continue and Aider.

Claude Code and Codex get a full skill (:mod:`verinoda.agents.installer`); these agents get a short
instructions file in the place each one documents, and the MCP server entry where the agent has one. The
same installer does the writing, so the same rules hold: a file Verinoda did not write is never
overwritten, shared configs are edited key-wise (every other byte kept), every change goes into the
install manifest, and uninstall removes exactly what the manifest lists, if unchanged since install.

Locations (paths relative to the project for ``project`` scope, to the home folder for ``user`` scope):

========  ================================================  ==================================================
agent     MCP server entry                                  instructions
========  ================================================  ==================================================
cursor    ``.cursor/mcp.json`` ``mcpServers`` (both scopes)  ``.cursor/rules/verinoda.mdc`` (project; Cursor's
                                                            user rules live in its settings, not a file)
gemini    ``.gemini/settings.json`` ``mcpServers``          a marked block in ``GEMINI.md`` (project) or
                                                            ``.gemini/GEMINI.md`` (user)
copilot   ``.vscode/mcp.json`` ``servers`` (project only)    a marked block in ``.github/copilot-instructions.md``
kiro      ``.kiro/settings/mcp.json`` ``mcpServers``        ``.kiro/steering/verinoda.md``
continue  ``.continue/mcpServers/verinoda.yaml`` (project)  ``.continue/rules/verinoda.md`` (project)
aider     none: Aider has no MCP client                     ``.aider.verinoda.md``, named by ``read:`` in a new
                                                            ``.aider.conf.yml`` (an existing one is not edited)
========  ================================================  ==================================================

"Found" (``verinoda setup --agents all``): the agent's folder (``.cursor``, ``.gemini``, ``.vscode``,
``.kiro``, ``.continue``; Aider's ``.aider.conf.yml``) exists in the project or the home folder, or its
program (``cursor``, ``gemini``, ``code``, ``kiro``, ``aider``) is on PATH. For Copilot that finds VS Code;
whether the Copilot extension is enabled is not checked.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from verinoda.selffiles import MARKER, MARKER_PREFIX, NAME

TEMPLATE = Path(__file__).parent / "templates" / "agent_RULES.md"


@dataclass(frozen=True)
class Spec:
    title: str
    mcp: dict          # scope -> (kind "json" | "yaml_file", relative path)
    outer: str         # the JSON object holding the servers
    typed: bool        # the entry carries "type": "stdio"
    rules: dict        # scope -> (kind "file" | "md_block", relative path)
    front: str         # frontmatter of a rules file ("" for none)
    dirs: tuple        # a folder (or file) whose presence means "found"
    exes: tuple        # a program on PATH that means "found"
    usage: str


SPECS: dict[str, Spec] = {
    "cursor": Spec(
        "Cursor", {"project": ("json", ".cursor/mcp.json"), "user": ("json", ".cursor/mcp.json")},
        "mcpServers", True, {"project": ("file", ".cursor/rules/verinoda.mdc")},
        "---\ndescription: Verinoda - evidence-first answers about this codebase\nalwaysApply: true\n---\n",
        (".cursor",), ("cursor",),
        "Cursor: the `verinoda` server is in .cursor/mcp.json (turn it on under MCP settings if Cursor lists it "
        "as off); the rule .cursor/rules/verinoda.mdc says when to use it"),
    "gemini": Spec(
        "Gemini CLI", {"project": ("json", ".gemini/settings.json"), "user": ("json", ".gemini/settings.json")},
        "mcpServers", False, {"project": ("md_block", "GEMINI.md"), "user": ("md_block", ".gemini/GEMINI.md")},
        "", (".gemini",), ("gemini",),
        "Gemini CLI: start `gemini` in this folder; `/mcp` lists the `verinoda` server, GEMINI.md says when to "
        "use it"),
    "copilot": Spec(
        "GitHub Copilot (VS Code)", {"project": ("json", ".vscode/mcp.json")}, "servers", True,
        {"project": ("md_block", ".github/copilot-instructions.md")}, "", (".vscode",), ("code",),
        "GitHub Copilot: VS Code asks to start the `verinoda` server from .vscode/mcp.json (MCP: List Servers); "
        "use it in agent mode"),
    "kiro": Spec(
        "Kiro", {"project": ("json", ".kiro/settings/mcp.json"), "user": ("json", ".kiro/settings/mcp.json")},
        "mcpServers", False, {"project": ("file", ".kiro/steering/verinoda.md"),
                              "user": ("file", ".kiro/steering/verinoda.md")},
        "---\ninclusion: always\n---\n", (".kiro",), ("kiro",),
        "Kiro: the `verinoda` server is in .kiro/settings/mcp.json; the steering file says when to use it"),
    "continue": Spec(
        "Continue", {"project": ("yaml_file", ".continue/mcpServers/verinoda.yaml")}, "", False,
        {"project": ("file", ".continue/rules/verinoda.md")},
        "---\nname: Verinoda\nalwaysApply: true\n---\n", (".continue",), (),
        "Continue: the `verinoda` server is in .continue/mcpServers/verinoda.yaml (agent mode uses MCP tools)"),
    "aider": Spec(
        "Aider", {}, "", False, {"project": ("file", ".aider.verinoda.md"), "user": ("file", ".aider.verinoda.md")},
        "", (".aider.conf.yml",), ("aider",),
        "Aider: it reads .aider.verinoda.md when .aider.conf.yml names it (`read:`) or with "
        "`aider --read .aider.verinoda.md`; Aider has no MCP, so it runs the `verinoda` CLI"),
}
AGENTS = tuple(SPECS)
AIDER_CONF = ".aider.conf.yml"
YAML_HEAD = f"# {MARKER} written by `verinoda install`; `verinoda uninstall` removes this file\n"


def scopes(agent: str) -> tuple[str, ...]:
    """The scopes ``agent`` can be installed in: the ones its documentation gives a file for."""
    s = SPECS[agent]
    return tuple(sc for sc in ("project", "user") if sc in s.rules or sc in s.mcp)


def found(agent: str, project_dir: Path, home: Path, which) -> str | None:
    """Why ``agent`` counts as found here (its folder in the project or home, or its program on PATH),
    or None."""
    s = SPECS[agent]
    for base, label in ((Path(project_dir), ""), (Path(home), "~/")):
        for d in s.dirs:
            if (base / d).exists():
                return f"{label}{d} exists"
    for exe in s.exes:
        if which(exe):
            return f"`{exe}` is on PATH"
    return None


# -- what gets written -------------------------------------------------------------------------------------

def render_rules(launcher: dict) -> str:
    """The instructions text (no frontmatter, no marker), naming the command that runs this build."""
    text = TEMPLATE.read_text(encoding="utf-8").replace("\r\n", "\n")
    return text.replace("{{VERINODA_CLI}}", launcher["cli"])


def rules_file(agent: str, launcher: dict) -> bytes:
    s = SPECS[agent]
    return (s.front + MARKER + "\n" + render_rules(launcher)).encode("utf-8")


def _yq(s: str) -> str:
    """A YAML double-quoted scalar (a JSON string is one)."""
    return json.dumps(str(s), ensure_ascii=False)


def continue_yaml(cmd: list[str]) -> bytes:
    lines = [YAML_HEAD.rstrip("\n"), "name: Verinoda", "version: 0.0.1", "schema: v1", "mcpServers:",
             f"  - name: {NAME}", f"    command: {_yq(cmd[0])}", "    args:"]
    lines += [f"      - {_yq(a)}" for a in cmd[1:]]
    return ("\n".join(lines) + "\n").encode("utf-8")


def aider_conf(rules_path: Path) -> bytes:
    return (YAML_HEAD + f"read: [{_yq(rules_path)}]\n").encode("utf-8")


# -- a marked block inside a shared Markdown file ------------------------------------------------------------

BLOCK_BEGIN = "<!-- verinoda-managed v1 begin: added by `verinoda install`; `verinoda uninstall` removes it -->"
BLOCK_END = "<!-- verinoda-managed end -->"
BLOCK_RE = re.compile(r"^<!-- verinoda-managed v1 begin\b[^\r\n]*\r?\n.*?^<!-- verinoda-managed end -->[^\r\n]*"
                      r"(?:\r?\n|\Z)", re.S | re.M)


def render_block(body: str, nl: str = "\n") -> str:
    lines = [BLOCK_BEGIN, *body.rstrip("\n").split("\n"), BLOCK_END]
    return nl.join(lines) + nl


# -- planning (called by the installer) -------------------------------------------------------------------------

def plan_rules(plan, t, prev_items: list[dict], launcher: dict) -> None:
    from verinoda.agents import installer as ins

    if t.skill is None:
        plan.notes.append(f"{SPECS[t.agent].title} keeps {t.scope}-level instructions outside a file "
                          "(its settings); no instructions written for this scope.")
        return
    if t.rules_kind == "file":
        ins._plan_file(plan, t.root, t.skill, rules_file(t.agent, launcher), prev_items, "rules")
        if t.agent == "aider":
            _plan_aider_conf(plan, t, prev_items)
        return
    _plan_block(plan, t, render_rules(launcher), prev_items)


def _plan_aider_conf(plan, t, prev_items: list[dict]) -> None:
    from verinoda.agents import installer as ins

    conf = t.root / AIDER_CONF
    data = aider_conf(t.skill)
    cur = conf.read_bytes() if conf.is_file() else None
    if cur is None or MARKER_PREFIX in cur:
        ins._plan_file(plan, t.root, conf, data, prev_items, "config")
        return
    if t.skill.name.encode("utf-8") in cur:
        plan.act("unchanged", "config", conf, f"already names {t.skill.name}")
        return
    plan.act("manual", "config", conf, "an existing Aider config is not edited")
    plan.warnings.append(f"Aider reads {t.skill.name} only when told to: {conf} exists and is not Verinoda's, so "
                         "it was left as it is. Add the line below to it, or start aider with --read.")
    plan.manual.append(f"read: [{_yq(t.skill)}]   # in {conf}")


def _plan_block(plan, t, body: str, prev_items: list[dict]) -> None:
    from verinoda.agents import _jsonedit as je
    from verinoda.agents import _tomledit as te
    from verinoda.agents import installer as ins

    p, rel = t.skill, ins._rel(t.root, t.skill)
    prev = ins._prev(prev_items, "md_block", rel)
    item = {"kind": "md_block", "role": "rules", "path": rel, "sha256": "", "created_file": False,
            "created_dirs": [], "added_eol": False, "added_separator": False}
    if prev:
        for k in ("created_file", "created_dirs", "added_eol", "added_separator"):
            item[k] = prev.get(k, item[k])
    if not p.exists():
        block = render_block(body)
        item.update(sha256=ins._sha(te.block_key(block)), created_file=True,
                    created_dirs=[ins._rel(t.root, d) for d in ins._missing_dirs(p, t.root)])
        plan.act("create", "rules", p, "Verinoda block")
        plan.ops.append((lambda: (ins._write_bytes(p, block.encode("utf-8")), item)[1], item))
        return
    try:
        text, bom = ins._read_text(p)
    except (OSError, UnicodeDecodeError) as exc:
        plan.refuse("rules", p, f"{p} is not readable UTF-8 text ({exc}); not touching it.")
        return
    nl = je.newline_of(text)
    block = render_block(body, nl)
    item["sha256"] = ins._sha(te.block_key(block))
    m = BLOCK_RE.search(text)
    if m:
        if te.block_key(m.group(0)) == te.block_key(block):
            plan.act("unchanged", "rules", p, "Verinoda block")
            plan.keep(item)
            return
        if prev and ins._sha(te.block_key(m.group(0))) != prev.get("sha256"):
            plan.refuse("rules", p, f"the Verinoda block in {p} was edited after install; refusing to overwrite "
                        "it. Remove the block and re-run install.")
            return
        new, op = text[: m.start()] + block + text[m.end():], "update"
    else:
        new, added_eol, added_sep = te.append_block(text, block, nl)
        item.update(added_eol=added_eol, added_separator=added_sep)
        op = "create"
    plan.act(op, "rules", p, "Verinoda block at the end; rest of file untouched")
    plan.ops.append((lambda: (ins._write_bytes(p, ins._encode(new, bom)), item)[1], item))


def plan_yaml(plan, t, cmd: list[str], prev_items: list[dict]) -> None:
    from verinoda.agents import installer as ins

    ins._plan_file(plan, t.root, t.mcp_path, continue_yaml(cmd), prev_items, "mcp")


def un_block(plan, t, item: dict, kept: list[dict]) -> None:
    """Remove the marked block :func:`_plan_block` added, if unchanged; the file's other bytes stay."""
    from verinoda.agents import _jsonedit as je
    from verinoda.agents import _tomledit as te
    from verinoda.agents import installer as ins

    p = ins._abs(t.root, item["path"])
    if not p.exists():
        plan.act("missing", "rules", p, "file already gone")
        return
    try:
        text, bom = ins._read_text(p)
    except (OSError, UnicodeDecodeError) as exc:
        plan.act("keep", "rules", p, "unreadable")
        plan.warnings.append(f"{p} could not be read ({exc}); left untouched - remove the Verinoda block by hand.")
        kept.append(item)
        return
    m = BLOCK_RE.search(text)
    if not m:
        plan.act("missing", "rules", p, "Verinoda block already gone")
        return
    if ins._sha(te.block_key(m.group(0))) != item.get("sha256"):
        plan.act("keep", "rules", p, "Verinoda block modified since install")
        plan.warnings.append(f"the Verinoda block in {p} was modified after install; left in place.")
        kept.append(item)
        return
    new = te.remove_block(text, m, je.newline_of(text), added_eol=bool(item.get("added_eol")),
                          added_sep=bool(item.get("added_separator")))
    if item.get("created_file") and not new.strip():
        plan.act("remove", "rules", p, "file created by install; nothing else in it")
        dirs = ins._un_dirs(plan, t, item.get("created_dirs", []))
        plan.ops.append((lambda: (p.unlink(), ins._rmdirs(dirs))[0], None))
        return
    plan.act("remove", "rules", p, "Verinoda block; rest of file untouched")
    plan.ops.append((lambda: ins._write_bytes(p, ins._encode(new, bom)), None))


def usage_notes(t, with_mcp: bool) -> list[str]:
    s = SPECS[t.agent]
    notes = [s.usage + "."]
    if t.agent == "aider" and with_mcp:
        notes.append("Aider has no MCP client: no server entry was written, only the instructions.")
    elif with_mcp:
        notes.append(f"Restart {s.title} (or reload its MCP servers) to load the change.")
    return notes
