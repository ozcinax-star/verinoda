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
aider     none: Aider has no MCP client                     ``.aider.verinoda.md``, named by ``read:`` in a marked
                                                            block of a new ``.aider.conf.yml`` (an existing one
                                                            without the block is not edited)
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

from verinoda.selffiles import MARKER, NAME, find_block

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
    mcp_note: str      # how to use the server entry ({mcp}: where it is)
    rules_note: str    # what the instructions file does ({rules}: where it is, {conf}: Aider's config)


SPECS: dict[str, Spec] = {
    "cursor": Spec(
        "Cursor", {"project": ("json", ".cursor/mcp.json"), "user": ("json", ".cursor/mcp.json")},
        "mcpServers", True, {"project": ("file", ".cursor/rules/verinoda.mdc")},
        "---\ndescription: Verinoda - evidence-first answers about this codebase\nalwaysApply: true\n---\n",
        (".cursor",), ("cursor",),
        "the `verinoda` server is in {mcp} (turn it on under MCP settings if Cursor lists it as off)",
        "the rule {rules} says when to use it"),
    "gemini": Spec(
        "Gemini CLI", {"project": ("json", ".gemini/settings.json"), "user": ("json", ".gemini/settings.json")},
        "mcpServers", False, {"project": ("md_block", "GEMINI.md"), "user": ("md_block", ".gemini/GEMINI.md")},
        "", (".gemini",), ("gemini",),
        "`/mcp` in `gemini` lists the `verinoda` server ({mcp})", "{rules} says when to use it"),
    "copilot": Spec(
        "GitHub Copilot (VS Code)", {"project": ("json", ".vscode/mcp.json")}, "servers", True,
        {"project": ("md_block", ".github/copilot-instructions.md")}, "", (".vscode",), ("code",),
        "VS Code asks to start the `verinoda` server from {mcp} (MCP: List Servers); use it in agent mode",
        "{rules} says when to use it"),
    "kiro": Spec(
        "Kiro", {"project": ("json", ".kiro/settings/mcp.json"), "user": ("json", ".kiro/settings/mcp.json")},
        "mcpServers", False, {"project": ("file", ".kiro/steering/verinoda.md"),
                              "user": ("file", ".kiro/steering/verinoda.md")},
        "---\ninclusion: always\n---\n", (".kiro",), ("kiro",),
        "the `verinoda` server is in {mcp}", "the steering file {rules} says when to use it"),
    "continue": Spec(
        "Continue", {"project": ("yaml_file", ".continue/mcpServers/verinoda.yaml")}, "", False,
        {"project": ("file", ".continue/rules/verinoda.md")},
        "---\nname: Verinoda\nalwaysApply: true\n---\n", (".continue",), (),
        "the `verinoda` server is in {mcp} (agent mode uses MCP tools)", "the rule {rules} says when to use it"),
    "aider": Spec(
        "Aider", {}, "", False, {"project": ("file", ".aider.verinoda.md"), "user": ("file", ".aider.verinoda.md")},
        "", (".aider.conf.yml",), ("aider",),
        "", "it reads {rules} when {conf} names it (`read:`) or with `aider --read {rules}`; Aider has no MCP, "
        "so it runs the `verinoda` CLI"),
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


def aider_read(rules_path: Path) -> str:
    """The ``read:`` line that makes Aider load the instructions file."""
    return f"read: [{_yq(rules_path)}]"


def read_entry(text: str) -> tuple[bool, str]:
    """(does the YAML text have a top-level ``read:`` key, the text of its value with comments cut): the
    value on the key's line and the indented or ``-`` lines under it. A line reading, not a YAML parser."""
    has, vals, inside = False, [], False
    for ln in text.splitlines():
        code = "" if ln.lstrip().startswith("#") else re.split(r"\s#", ln, maxsplit=1)[0]
        if not code.strip():
            continue
        if code[0] not in " \t-":
            inside = re.match(r"""["']?read["']?\s*:""", code) is not None
            if inside:
                has = True
                vals.append(code.split(":", 1)[1])
        elif inside:
            vals.append(code)
    return has, " ".join(vals)


# -- a marked block inside a shared file (Markdown; a YAML comment in Aider's config) --------------------------

BLOCK_BEGIN = "<!-- verinoda-managed v1 begin: added by `verinoda install`; `verinoda uninstall` removes it -->"
BLOCK_END = "<!-- verinoda-managed end -->"
# found by verinoda.selffiles.find_block, which also keeps a file holding only the block out of the corpus


def render_block(body: str, nl: str = "\n", comment: str = "") -> str:
    """The marked block around ``body``; ``comment`` ("# " in YAML) goes before the begin and end lines."""
    lines = [comment + BLOCK_BEGIN, *body.rstrip("\n").split("\n"), comment + BLOCK_END]
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
    _plan_block(plan, t, t.skill, render_rules(launcher), prev_items, "rules")


def _plan_aider_conf(plan, t, prev_items: list[dict]) -> None:
    """Aider's config: created with a marked block holding the ``read:`` line (the user may add keys around
    it; uninstall removes only the block). A config without the block is the user's and is not edited."""
    from verinoda.agents import installer as ins

    conf = t.root / AIDER_CONF
    if not conf.exists():
        _plan_block(plan, t, conf, aider_read(t.skill), prev_items, "config", "# ")
        return
    try:
        text, _bom = ins._read_text(conf)
    except (OSError, UnicodeDecodeError):
        text = None
    if text is not None and find_block(text):
        _plan_block(plan, t, conf, aider_read(t.skill), prev_items, "config", "# ")
        return
    has_read, value = read_entry(text or "")
    if text is not None and t.skill.name in value:
        plan.act("unchanged", "config", conf, f"its `read:` entry names {t.skill.name}")
        return
    plan.act("manual", "config", conf, "an existing Aider config is not edited")
    plan.warnings.append(f"Aider reads {t.skill.name} only when told to: {conf} exists and is not Verinoda's, so "
                         "it was left as it is. Add the file to its `read:` entry, or start aider with --read.")
    plan.manual.append(f"add {_yq(t.skill)} to the `read:` list in {conf}" if has_read
                       else f"{aider_read(t.skill)}   # in {conf}")


def _plan_block(plan, t, p: Path, body: str, prev_items: list[dict], role: str, comment: str = "") -> None:
    from verinoda.agents import _jsonedit as je
    from verinoda.agents import _tomledit as te
    from verinoda.agents import installer as ins

    rel = ins._rel(t.root, p)
    prev = ins._prev(prev_items, "md_block", rel)
    item = {"kind": "md_block", "role": role, "path": rel, "sha256": "", "created_file": False,
            "created_dirs": [], "added_eol": False, "added_separator": False}
    if prev:
        for k in ("created_file", "created_dirs", "added_eol", "added_separator"):
            item[k] = prev.get(k, item[k])
    if not p.exists():
        block = render_block(body, "\n", comment)
        item.update(sha256=ins._sha(te.block_key(block)), created_file=True,
                    created_dirs=[ins._rel(t.root, d) for d in ins._missing_dirs(p, t.root)])
        plan.act("create", role, p, "Verinoda block")
        plan.ops.append((lambda: (ins._write_bytes(p, block.encode("utf-8")), item)[1], item))
        return
    try:
        text, bom = ins._read_text(p)
    except (OSError, UnicodeDecodeError) as exc:
        plan.refuse(role, p, f"{p} is not readable UTF-8 text ({exc}); not touching it.")
        return
    nl = je.newline_of(text)
    block = render_block(body, nl, comment)
    item["sha256"] = ins._sha(te.block_key(block))
    m = find_block(text)
    if m:
        if te.block_key(m.group(0)) == te.block_key(block):
            plan.act("unchanged", role, p, "Verinoda block")
            plan.keep(item)
            return
        if prev and ins._sha(te.block_key(m.group(0))) != prev.get("sha256"):
            plan.refuse(role, p, f"the Verinoda block in {p} was edited after install; refusing to overwrite "
                        "it. Remove the block and re-run install.")
            return
        new, op = text[: m.start()] + block + text[m.end():], "update"
    else:
        new, added_eol, added_sep = te.append_block(text, block, nl)
        item.update(added_eol=added_eol, added_separator=added_sep)
        op = "create"
    plan.act(op, role, p, "Verinoda block at the end; rest of file untouched")
    plan.ops.append((lambda: (ins._write_bytes(p, ins._encode(new, bom)), item)[1], item))


def plan_yaml(plan, t, cmd: list[str], prev_items: list[dict]) -> None:
    from verinoda.agents import installer as ins

    ins._plan_file(plan, t.root, t.mcp_path, continue_yaml(cmd), prev_items, "mcp")


def un_block(plan, t, item: dict, kept: list[dict]) -> None:
    """Remove the marked block :func:`_plan_block` added, if unchanged; the file's other bytes stay."""
    from verinoda.agents import _jsonedit as je
    from verinoda.agents import _tomledit as te
    from verinoda.agents import installer as ins

    p, role = ins._abs(t.root, item["path"]), item.get("role") or "rules"
    if not p.exists():
        plan.act("missing", role, p, "file already gone")
        return
    try:
        text, bom = ins._read_text(p)
    except (OSError, UnicodeDecodeError) as exc:
        plan.act("keep", role, p, "unreadable")
        plan.warnings.append(f"{p} could not be read ({exc}); left untouched - remove the Verinoda block by hand.")
        kept.append(item)
        return
    m = find_block(text)
    if not m:
        plan.act("missing", role, p, "Verinoda block already gone")
        return
    if ins._sha(te.block_key(m.group(0))) != item.get("sha256"):
        plan.act("keep", role, p, "Verinoda block modified since install")
        plan.warnings.append(f"the Verinoda block in {p} was modified after install; left in place.")
        kept.append(item)
        return
    new = te.remove_block(text, m, je.newline_of(text), added_eol=bool(item.get("added_eol")),
                          added_sep=bool(item.get("added_separator")))
    if item.get("created_file") and not new.strip():
        plan.act("remove", role, p, "file created by install; nothing else in it")
        dirs = ins._un_dirs(plan, t, item.get("created_dirs", []))
        plan.ops.append((lambda: (p.unlink(), ins._rmdirs(dirs))[0], None))
        return
    plan.act("remove", role, p, "Verinoda block; rest of file untouched")
    plan.ops.append((lambda: ins._write_bytes(p, ins._encode(new, bom)), None))


def usage(agent: str, scope: str, with_mcp: bool) -> str:
    """How to use what install writes for ``agent`` at ``scope``: the server entry only when one is written
    (``with_mcp`` and a location at this scope), the instructions only when this scope has a file for them."""
    s = SPECS[agent]
    pre = "~/" if scope == "user" else ""
    parts = []
    if with_mcp and scope in s.mcp:
        parts.append(s.mcp_note.format(mcp=pre + s.mcp[scope][1]))
    if scope in s.rules:
        parts.append(s.rules_note.format(rules=pre + s.rules[scope][1], conf=pre + AIDER_CONF))
    return f"{s.title}: " + ("; ".join(parts) if parts else f"nothing is written for it at {scope} scope")


def usage_notes(t, with_mcp: bool) -> list[str]:
    s = SPECS[t.agent]
    notes = [usage(t.agent, t.scope, with_mcp) + "."]
    if t.agent == "aider" and with_mcp:
        notes.append("Aider has no MCP client: no server entry was written, only the instructions.")
    elif with_mcp and t.mcp_kind:
        notes.append(f"Restart {s.title} (or reload its MCP servers) to load the change.")
    return notes
