"""Tests for verinoda.agents.more_agents: Cursor, Gemini CLI, GitHub Copilot, Kiro, Continue and Aider.

Every test runs in a temporary project and home; ``Path.home`` points at a guard path that must never be
created, programs on PATH are stubbed, and no external command may run. Nothing here depends on which
agents are installed on the machine.
"""

from __future__ import annotations

import json
import re
import shlex
import sys
from pathlib import Path

import pytest

from verinoda import agents, selffiles
from verinoda.agents import installer as ins
from verinoda.agents import more_agents as ma

OTHERS = ma.AGENTS


def _no_run(argv, env=None, timeout=120):  # pragma: no cover - must never be reached
    raise AssertionError(f"unexpected external command: {argv}")


@pytest.fixture
def env(tmp_path, monkeypatch):
    home, proj, bindir = tmp_path / "home", tmp_path / "proj", tmp_path / "bin"
    for d in (home, proj, bindir):
        d.mkdir()
    exe = bindir / "verinoda.exe"
    exe.write_bytes(f"#!{sys.executable}\n".encode("utf-8"))
    monkeypatch.setattr(ins, "_import_location", lambda python: str(ins.PKG_DIR))
    guard = tmp_path / "REAL-HOME-MUST-NOT-BE-USED"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: guard))
    monkeypatch.setenv("HOME", str(guard))
    monkeypatch.setenv("USERPROFILE", str(guard))
    which = {"verinoda": str(exe)}
    monkeypatch.setattr(ins, "_which", lambda name: which.get(name))
    monkeypatch.setattr(ins, "_run", _no_run)
    # which program gets registered is the launcher tests' business (test_agents.py); here it is fixed, so
    # these tests do not depend on how Verinoda is installed on the machine running them
    launcher = {"argv": [str(exe)], "how": "path", "cli": "verinoda", "note": "registered `verinoda` on PATH",
                "why": "it runs this build", "warnings": [], "python": sys.executable, "package": str(ins.PKG_DIR),
                "imports": str(ins.PKG_DIR), "runs_ours": True, "path_exe": str(exe), "path_python": sys.executable,
                "path_build": "this"}
    monkeypatch.setattr(ins, "resolve_launcher", lambda: dict(launcher, warnings=[]))
    from verinoda import doctor

    monkeypatch.setattr(doctor, "install_layout", lambda: {"path": str(bindir), "hardlinked": False,
                                                           "editable": False})

    class NS:
        pass

    ns = NS()
    ns.tmp, ns.home, ns.proj, ns.exe, ns.which = tmp_path, home, proj, str(exe), which
    yield ns
    assert not guard.exists(), "the real home directory would have been touched"


def tree(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): (p.read_bytes() if p.is_file() else None)
            for p in sorted(root.rglob("*"))}


def manifest(root: Path) -> dict:
    return json.loads((root / ".verinoda" / "install-manifest.json").read_text(encoding="utf-8"))


def install(env, agent, scope="project", **kw):
    return agents.install(agent, scope, project_dir=env.proj, home=env.home, **kw)


def uninstall(env, agent, scope="project", **kw):
    return agents.uninstall(agent, scope, project_dir=env.proj, home=env.home, **kw)


# where each agent's files land in a project (from the docs each one publishes; see more_agents)
PROJECT_FILES = {
    "cursor": {".cursor/rules/verinoda.mdc", ".cursor/mcp.json"},
    "gemini": {"GEMINI.md", ".gemini/settings.json"},
    "copilot": {".github/copilot-instructions.md", ".vscode/mcp.json"},
    "kiro": {".kiro/steering/verinoda.md", ".kiro/settings/mcp.json"},
    "continue": {".continue/rules/verinoda.md", ".continue/mcpServers/verinoda.yaml"},
    "aider": {".aider.verinoda.md", ".aider.conf.yml"},
}


# -- project scope: what is written, idempotence, exact uninstall ------------------------------------------

@pytest.mark.parametrize("agent", OTHERS)
def test_project_install_writes_the_documented_files_and_uninstall_removes_them(env, agent):
    r = install(env, agent)
    assert r["ok"] and r["result"] == "installed", r
    files = {k for k, v in tree(env.proj).items() if v is not None and not k.startswith(".verinoda/")}
    assert files == PROJECT_FILES[agent]
    for rel in (f for f in files if not f.endswith(".json")):  # JSON has no comments: the manifest records it
        text = (env.proj / rel).read_text(encoding="utf-8")
        assert agents.MARKER in text or "verinoda-managed v1" in text, rel
    if agent != "aider":
        assert r["server"]["command"][-3:] == ["serve", "--repo", str(env.proj.resolve())]
    assert r["notes"] and r["notes"][0].startswith(ma.SPECS[agent].title.split(" ")[0])
    before = tree(env.tmp)
    r2 = install(env, agent)
    assert r2["ok"] and r2["result"] == "unchanged" and tree(env.tmp) == before  # byte-identical
    u = uninstall(env, agent)
    assert u["ok"] and u["result"] == "uninstalled", u
    assert tree(env.proj) == {} and tree(env.home) == {}


def test_mcp_entries_follow_each_agents_format(env):
    for agent in ("cursor", "gemini", "copilot", "kiro"):
        install(env, agent)
    cmd = [env.exe, "mcp", "serve", "--repo", str(env.proj.resolve())]
    load = lambda rel: json.loads((env.proj / rel).read_text(encoding="utf-8"))  # noqa: E731
    stdio = {"type": "stdio", "command": cmd[0], "args": cmd[1:]}
    plain = {"command": cmd[0], "args": cmd[1:]}
    assert load(".cursor/mcp.json") == {"mcpServers": {"verinoda": stdio}}
    assert load(".gemini/settings.json") == {"mcpServers": {"verinoda": plain}}
    assert load(".vscode/mcp.json") == {"servers": {"verinoda": stdio}}  # VS Code's key is `servers`
    assert load(".kiro/settings/mcp.json") == {"mcpServers": {"verinoda": plain}}
    item = next(i for i in manifest(env.proj)["installs"]["copilot"]["items"] if i["kind"] == "json_key")
    assert item["key"] == ["servers", "verinoda"] and item["created_dirs"] == [".vscode"]


def test_rules_files_carry_the_frontmatter_each_agent_reads(env):
    for agent in ("cursor", "kiro", "continue"):
        install(env, agent, with_mcp=False)
    mdc = (env.proj / ".cursor/rules/verinoda.mdc").read_text(encoding="utf-8")
    assert mdc.startswith("---\ndescription: ") and "\nalwaysApply: true\n---\n" + agents.MARKER in mdc
    assert (env.proj / ".kiro/steering/verinoda.md").read_text(encoding="utf-8").startswith(
        "---\ninclusion: always\n---\n" + agents.MARKER)
    assert (env.proj / ".continue/rules/verinoda.md").read_text(encoding="utf-8").startswith(
        "---\nname: Verinoda\nalwaysApply: true\n---\n")
    assert not (env.proj / ".cursor/mcp.json").exists()  # --no-mcp: instructions only


def test_continue_server_file_is_yaml_with_the_command(env, tmp_path):
    weird = tmp_path / 'dir "q" \\ x'  # a path with quotes and a backslash stays one YAML string
    install(env, "continue")
    text = (env.proj / ".continue/mcpServers/verinoda.yaml").read_text(encoding="utf-8")
    assert text.startswith("# " + agents.MARKER) and "schema: v1\nmcpServers:\n  - name: verinoda\n" in text
    # each scalar is a double-quoted YAML string, which a JSON string is: read back without a YAML parser
    odd = ma.continue_yaml([str(weird), "a: b", "-x"]).decode("utf-8")
    assert json.loads(re.search(r"command: (.*)", odd).group(1)) == str(weird)
    assert [json.loads(v) for v in re.findall(r"^      - (.*)$", odd, re.M)] == ["a: b", "-x"]
    yaml = pytest.importorskip("yaml")
    doc = yaml.safe_load(text)
    server = doc["mcpServers"][0]
    assert [server["command"], *server["args"]][-3:] == ["serve", "--repo", str(env.proj.resolve())]
    assert yaml.safe_load(ma.continue_yaml([str(weird), "a: b", "-x"]))["mcpServers"][0] == {
        "name": "verinoda", "command": str(weird), "args": ["a: b", "-x"]}


# -- user files are kept byte for byte ------------------------------------------------------------------------

@pytest.mark.parametrize("nl", ["\n", "\r\n"])
def test_shared_json_configs_keep_every_other_byte(env, nl):
    original = ('{' + nl + '    "servers": {' + nl + '        "other": {"command": "x"}' + nl + '    },' + nl
                + '    "inputs": []' + nl + '}' + nl).encode("utf-8")
    (env.proj / ".vscode").mkdir()
    (env.proj / ".vscode/mcp.json").write_bytes(original)
    cursor = ('{"mcpServers": {"a": {"command": "a"}}, "zeta": 1}').encode("utf-8")  # no final newline
    (env.proj / ".cursor").mkdir()
    (env.proj / ".cursor/mcp.json").write_bytes(cursor)
    for agent in ("copilot", "cursor"):
        assert install(env, agent)["ok"]
    after = (env.proj / ".vscode/mcp.json").read_bytes()
    assert after.startswith(original[:20]) and b'"other": {"command": "x"}' in after and b'"inputs": []' in after
    assert json.loads(after)["servers"]["verinoda"]["type"] == "stdio"
    for agent in ("copilot", "cursor"):
        assert uninstall(env, agent)["result"] == "uninstalled"
    assert (env.proj / ".vscode/mcp.json").read_bytes() == original
    assert (env.proj / ".cursor/mcp.json").read_bytes() == cursor
    assert not (env.proj / ".cursor/rules").exists() and not (env.proj / ".github").exists()


@pytest.mark.parametrize("text", ["# Project notes\n\nUse tabs.\n", "# Notes\r\nno final newline", "", "x\n\n"])
def test_markdown_block_is_appended_and_removed_byte_for_byte(env, text):
    original = text.encode("utf-8")
    (env.proj / "GEMINI.md").write_bytes(original)
    assert install(env, "gemini", with_mcp=False)["ok"]
    after = (env.proj / "GEMINI.md").read_bytes()
    assert after.startswith(original) and after.count(ma.BLOCK_BEGIN.encode()) == 1
    if "\r\n" in text:
        assert b"\n" not in after.replace(b"\r\n", b"")  # the file's own line ending is used
    assert install(env, "gemini", with_mcp=False)["result"] == "unchanged"
    assert uninstall(env, "gemini")["result"] == "uninstalled"
    assert (env.proj / "GEMINI.md").read_bytes() == original  # kept: the file was the user's


def test_a_rules_file_verinoda_did_not_write_is_never_overwritten(env):
    (env.proj / ".kiro/steering").mkdir(parents=True)
    (env.proj / ".kiro/steering/verinoda.md").write_bytes(b"my own steering\n")
    before = tree(env.tmp)
    r = install(env, "kiro")
    assert not r["ok"] and r["result"] == "refused" and "not managed by Verinoda" in r["errors"][0]
    assert tree(env.tmp) == before  # nothing written, the MCP entry included


def test_an_edited_block_is_left_in_place_on_uninstall(env):
    install(env, "copilot", with_mcp=False)
    p = env.proj / ".github/copilot-instructions.md"
    p.write_bytes(p.read_bytes().replace(b"Answer rules", b"My answer rules"))
    edited = p.read_bytes()
    u = uninstall(env, "copilot")
    assert u["result"] == "kept_modified" and p.read_bytes() == edited
    assert any("modified after install" in w for w in u["warnings"])
    r = install(env, "copilot", with_mcp=False)
    assert r["result"] == "refused" and p.read_bytes() == edited


def test_aider_has_no_mcp_and_an_existing_config_is_not_edited(env):
    conf = env.proj / ".aider.conf.yml"
    conf.write_bytes(b"model: sonnet\n")
    r = install(env, "aider")
    assert r["ok"] and r["mcp_config"] is None and "command" not in r["server"]
    assert conf.read_bytes() == b"model: sonnet\n"
    assert r["manual"] and r["manual"][0].startswith("read: [") and ".aider.verinoda.md" in r["manual"][0]
    assert any("no MCP client" in n for n in r["notes"])
    assert uninstall(env, "aider")["result"] == "uninstalled"
    assert tree(env.proj) == {".aider.conf.yml": b"model: sonnet\n"}


def test_aider_config_written_by_install_names_the_rules_file(env):
    install(env, "aider")
    text = (env.proj / ".aider.conf.yml").read_text(encoding="utf-8")
    assert text.startswith("# " + ma.BLOCK_BEGIN) and text.rstrip("\n").endswith("# " + ma.BLOCK_END)
    assert f"read: [{json.dumps(str(env.proj.resolve() / '.aider.verinoda.md'))}]" in text


# -- user scope ----------------------------------------------------------------------------------------------

def test_user_scope_writes_under_home_only(env):
    for agent in ("cursor", "gemini", "kiro", "aider"):
        r = install(env, agent, "user")
        assert r["ok"], r
    assert tree(env.proj) == {}
    files = {k for k, v in tree(env.home).items() if v is not None and not k.startswith(".verinoda/")}
    assert files == {".cursor/mcp.json", ".gemini/settings.json", ".gemini/GEMINI.md", ".kiro/settings/mcp.json",
                     ".kiro/steering/verinoda.md", ".aider.verinoda.md", ".aider.conf.yml"}
    assert json.loads((env.home / ".cursor/mcp.json").read_text(encoding="utf-8"))["mcpServers"]["verinoda"][
        "args"] == ["mcp", "serve"]
    r = install(env, "cursor", "user")
    assert any("settings" in n for n in r["notes"])  # Cursor's user rules are not a file
    for agent in ("cursor", "gemini", "kiro", "aider"):
        assert uninstall(env, agent, "user")["result"] == "uninstalled"
    assert tree(env.home) == {}


@pytest.mark.parametrize("agent", ["copilot", "continue"])
def test_user_scope_is_refused_where_no_location_is_documented(env, agent):
    r = install(env, agent, "user")
    assert not r["ok"] and "no user-scope location" in r["errors"][0]
    assert tree(env.home) == {}


# -- found / setup --all -----------------------------------------------------------------------------------------

def test_found_rule_is_a_folder_or_a_program(env):
    from verinoda import setup as setup_mod

    (env.proj / ".cursor").mkdir()
    (env.home / ".gemini").mkdir()
    env.which.update(aider="/bin/aider", claude="/bin/claude")
    found = setup_mod.found_agents(env.proj, env.home)
    assert found == {"claude": "`claude` is on PATH", "cursor": ".cursor exists", "gemini": "~/.gemini exists",
                     "aider": "`aider` is on PATH"}
    assert setup_mod._choose("all", found) == ["claude", "cursor", "gemini", "aider"]
    assert setup_mod._choose("auto") == ["claude"]  # the default is unchanged
    (env.proj / ".continue").mkdir()
    assert "continue" in setup_mod.found_agents(env.proj, env.home)
    assert "continue" not in setup_mod.found_agents(env.proj, env.home, "user")  # project scope only


def test_setup_all_registers_each_agent_found(env):
    from verinoda import setup as setup_mod

    (env.proj / "app.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    (env.proj / ".kiro").mkdir()
    env.which["gemini"] = "/bin/gemini"
    rep = setup_mod.setup_project(env.proj, agents="all", home=env.home)
    assert rep["ok"], rep
    assert [a["agent"] for a in rep["agents"]] == ["gemini", "kiro"]
    assert rep["agents_found"] == {"gemini": "`gemini` is on PATH", "kiro": ".kiro exists"}
    assert (env.proj / "GEMINI.md").is_file() and (env.proj / ".kiro/settings/mcp.json").is_file()
    assert any(s.startswith("Kiro:") for s in rep["next_steps"])
    assert set(manifest(env.proj)["installs"]) == {"gemini", "kiro"}


def test_setup_all_with_nothing_found_says_so(env):
    from verinoda import setup as setup_mod

    (env.proj / "app.py").write_text("x = 1\n", encoding="utf-8")
    rep = setup_mod.setup_project(env.proj, agents="all", home=env.home)
    assert rep["ok"] and rep["agents"] == [] and rep["agents_found"] == {}
    assert any("no coding agent found" in w for w in rep["warnings"])


# -- the rest of the product sees these files as Verinoda's ----------------------------------------------------

def test_rules_files_are_own_files_and_shared_files_are_not(env):
    for agent in OTHERS:
        install(env, agent)
    own = set(selffiles.own_files(env.proj))
    # a shared file install created holds only Verinoda's block: Verinoda's text, so out of the corpus too
    assert own == {".cursor/rules/verinoda.mdc", ".kiro/steering/verinoda.md", ".continue/rules/verinoda.md",
                   ".continue/mcpServers/verinoda.yaml", ".aider.verinoda.md", ".aider.conf.yml", "GEMINI.md",
                   ".github/copilot-instructions.md"}
    vscode = env.proj / ".vscode/mcp.json"
    with_ours = selffiles.config_digest(vscode)
    data = json.loads(vscode.read_text(encoding="utf-8"))
    del data["servers"]["verinoda"]
    vscode.write_text(json.dumps(data), encoding="utf-8")
    assert selffiles.config_digest(vscode) == with_ours  # our entry does not count in the graph's digest


def test_cli_lists_every_agent_and_the_rules_text_is_complete(env):
    from verinoda.cli import build_parser

    choices = next(a for a in build_parser()._subparsers._group_actions[0].choices["install"]._actions
                   if a.dest == "agent").choices
    assert tuple(choices) == ins.ALL_AGENTS
    text = ma.render_rules(ins.resolve_launcher())
    for s in ("statically_verified", "observed", "experiment_verified", "primary_source_verified",
              "strong_inference", "weak_inference", "unknown", "contradicted", "stale"):
        assert f"`{s}`" in text, s
    assert "Never present `weak_inference` or `unknown` as fact" in text and "{{" not in text
    for ln in re.findall(r"^verinoda .*$", text, re.M):
        build_parser().parse_args(shlex.split(ln)[1:])


# -- review round: the block is Verinoda's text, the user's config stays the user's, notes say what was written

def test_a_shared_file_with_the_users_text_stays_in_the_corpus_and_agent_lint_skips_the_block(env):
    from verinoda import agentlint

    (env.proj / "pyproject.toml").write_text('[project]\nname = "shop"\nversion = "1"\n', encoding="utf-8")
    (env.proj / "app.py").write_text("def total():\n    return 1\n", encoding="utf-8")
    for agent in ("gemini", "copilot"):
        install(env, agent)
    assert selffiles.is_own(env.proj, "GEMINI.md") and selffiles.is_own(env.proj, ".github/copilot-instructions.md")
    rep = agentlint.lint(env.proj, memory=False, home=env.home, files=["GEMINI.md", "app.py", "pyproject.toml"])
    assert rep["summary"]["wrong"] == 0, rep  # the block's `-m verinoda` line is not the project's
    p = env.proj / "GEMINI.md"
    p.write_bytes(p.read_bytes() + b"\nRun `python -m nosuchmodule` to test.\n")
    assert not selffiles.is_own(env.proj, "GEMINI.md")  # the user wrote in it: it is the project's now
    rep = agentlint.lint(env.proj, memory=False, home=env.home, files=["GEMINI.md", "app.py", "pyproject.toml"])
    wrong = [c for c in rep["checks"] if c["verdict"] == "wrong"]
    assert [c["at"] for c in wrong] == [f"GEMINI.md:{len(p.read_text(encoding='utf-8').splitlines())}"], wrong


def test_find_block_is_linear_on_many_begin_lines_without_an_end():
    import time

    text = ma.BLOCK_BEGIN + " x\n"
    t0 = time.perf_counter()
    assert selffiles.find_block(text * 20000) is None
    assert time.perf_counter() - t0 < 1.0
    both = "a\n" + ma.render_block("b") + "c\n" + ma.render_block("d", comment="# ")
    m = selffiles.find_block(both)
    assert m.group(0) == ma.render_block("b") and both[m.end():].startswith("c\n")


def test_aider_config_keys_the_user_adds_are_kept_and_reinstall_stays_safe(env):
    assert install(env, "aider")["result"] == "installed"
    conf = env.proj / ".aider.conf.yml"
    conf.write_bytes(conf.read_bytes() + b"model: sonnet\n")
    r = install(env, "aider")
    assert r["ok"] and r["result"] == "unchanged", r
    assert not selffiles.is_own(env.proj, ".aider.conf.yml")  # the user's keys are in it now
    u = uninstall(env, "aider")
    assert u["result"] == "uninstalled", u
    assert tree(env.proj) == {".aider.conf.yml": b"model: sonnet\n"}


@pytest.mark.parametrize("conf, manual", [
    ("# TODO maybe read .aider.verinoda.md later\nmodel: sonnet\n", "read: ["),  # a comment is not a read entry
    ("read: [CONVENTIONS.md]\n", "add "),  # one read: key: add to it, never a second one
    ("read:\n  - CONVENTIONS.md\n  - .aider.verinoda.md  # ours\n", None),
    ("model: x\nread: [CONVENTIONS.md, .aider.verinoda.md]\n", None),
])
def test_aider_existing_config_counts_only_a_read_entry(env, conf, manual):
    (env.proj / ".aider.conf.yml").write_text(conf, encoding="utf-8")
    r = install(env, "aider")
    assert r["ok"], r
    act = next(a for a in r["actions"] if a["what"] == "config")
    if manual:
        assert act["op"] == "manual" and r["manual"] and r["manual"][0].startswith(manual), r
    else:
        assert act["op"] == "unchanged" and "`read:` entry" in act["detail"] and not r["manual"], r
    assert (env.proj / ".aider.conf.yml").read_text(encoding="utf-8") == conf


@pytest.mark.parametrize("agent", ["cursor", "gemini", "copilot", "kiro", "continue"])
def test_notes_name_only_what_was_written(env, agent):
    r = install(env, agent, with_mcp=False)
    assert r["mcp_config"] is None
    mcp_rel = ma.SPECS[agent].mcp["project"][1]
    assert not any(mcp_rel in n or "server" in n for n in r["notes"]), r["notes"]
    assert ma.SPECS[agent].rules["project"][1] in r["notes"][0]


def test_cursor_user_scope_notes_name_no_rules_file(env):
    r = install(env, "cursor", "user")
    assert not any(".cursor/rules" in n for n in r["notes"]), r["notes"]
    assert any("~/.cursor/mcp.json" in n for n in r["notes"])
    assert ma.usage("cursor", "user", False) == "Cursor: nothing is written for it at user scope"


@pytest.mark.parametrize("agent", ["copilot", "continue"])
def test_setup_refuses_an_agent_with_no_place_at_the_scope_before_writing(env, agent):
    from verinoda import setup as setup_mod

    (env.proj / "app.py").write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(setup_mod.SetupRefused, match="no user-scope location"):
        setup_mod.setup_project(env.proj, agents=agent, scope="user", home=env.home)
    assert not (env.proj / ".verinoda").exists() and tree(env.home) == {}
