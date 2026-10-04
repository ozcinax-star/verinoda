"""``verinoda live install`` (verinoda/live_install.py, D137): hooks for Claude Code and Codex written key-wise, the two
small skills written under the installer's rules, and everything removed exactly again."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from verinoda import cli, live, live_install, selffiles
from verinoda.agents import installer

LAUNCHER = {"how": "path", "argv": ["verinoda"], "cli": "verinoda", "note": "", "warnings": [], "path_build": "this"}


@pytest.fixture(autouse=True)
def _launcher(monkeypatch):
    monkeypatch.setattr(installer, "resolve_launcher", lambda: dict(LAUNCHER))


def install(proj, home, agents=("claude", "codex"), scope="project", **kw):
    return live_install.run("install", list(agents), scope, proj, home=home, **kw)


def read(p):
    return json.loads(Path(p).read_text(encoding="utf-8"))


@pytest.fixture()
def dirs(tmp_path):
    proj, home = tmp_path / "proj", tmp_path / "home"
    proj.mkdir()
    home.mkdir()
    return proj, home


def test_the_hooks_land_in_each_agents_own_file_with_one_command(dirs):
    proj, home = dirs
    res = install(proj, home)
    assert res["ok"]
    for path, matcher in ((proj / ".claude" / "settings.json", "Edit|Write|MultiEdit|NotebookEdit|Bash|PowerShell"),
                          (proj / ".codex" / "hooks.json", "^(apply_patch|Edit|Write|Bash)$")):
        hooks = read(path)["hooks"]
        assert set(hooks) == {"UserPromptSubmit", "PostToolUse", "Stop", "SessionStart"}
        assert hooks["PostToolUse"][0]["matcher"] == matcher
        commands = {h["command"] for es in hooks.values() for e in es for h in e["hooks"]}
        assert commands == {"verinoda live hook"}
        assert hooks["PostToolUse"][0]["hooks"][0]["timeout"] > hooks["Stop"][0]["hooks"][0]["timeout"]


def test_other_keys_and_hooks_stay_and_install_twice_changes_nothing(dirs):
    proj, home = dirs
    path = proj / ".claude" / "settings.json"
    path.parent.mkdir()
    mine = {"permissions": {"allow": ["Bash(ls)"]},
            "hooks": {"PostToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo mine"}]}],
                      "PreToolUse": [{"hooks": [{"type": "command", "command": "lint"}]}]}}
    path.write_text(json.dumps(mine, indent=4), encoding="utf-8")
    install(proj, home, agents=("claude",), with_skills=False)
    data = read(path)
    assert data["permissions"] == mine["permissions"] and data["hooks"]["PreToolUse"] == mine["hooks"]["PreToolUse"]
    assert data["hooks"]["PostToolUse"][0] == mine["hooks"]["PostToolUse"][0] and len(data["hooks"]["PostToolUse"]) == 2
    assert path.read_text(encoding="utf-8").splitlines()[1].startswith("    ")      # the file's own indent
    before = path.read_bytes()
    res = install(proj, home, agents=("claude",), with_skills=False)
    assert res["results"][0]["hooks"]["result"] == "unchanged" and path.read_bytes() == before
    live_install.run("uninstall", ["claude"], "project", proj, home=home, with_skills=False)
    assert read(path) == mine


def test_uninstall_removes_exactly_what_install_wrote_and_the_files_it_created(dirs):
    proj, home = dirs
    install(proj, home)
    res = live_install.run("uninstall", ["claude", "codex"], "project", proj, home=home)
    assert res["ok"]
    for gone in (".claude/settings.json", ".codex/hooks.json", ".claude/skills", ".agents/skills",
                 ".verinoda/live-install.json"):
        assert not (proj / gone).exists(), gone
    assert live_install.run("status", ["claude"], "project", proj, home=home)["results"][0]["hooks"]["result"] == "absent"


def test_a_file_that_is_not_json_is_never_touched(dirs):
    proj, home = dirs
    path = proj / ".claude" / "settings.json"
    path.parent.mkdir()
    for text in ("{broken", "[1]", '{"hooks": 5}'):
        path.write_text(text, encoding="utf-8")
        res = install(proj, home, agents=("claude",), with_skills=False)
        assert res["ok"] is False and res["results"][0]["hooks"]["result"] == "refused"
        assert path.read_text(encoding="utf-8") == text


def test_user_scope_writes_under_the_home_folder(dirs):
    proj, home = dirs
    install(proj, home, scope="user")
    assert (home / ".claude" / "settings.json").is_file() and (home / ".codex" / "hooks.json").is_file()
    assert (home / ".claude" / "skills" / "verinoda-improve" / "SKILL.md").is_file()
    assert (home / ".agents" / "skills" / "verinoda-live" / "SKILL.md").is_file()
    assert not (proj / ".claude").exists()


def test_the_skills_name_their_agents_entry_point_and_their_question_tool(dirs):
    proj, home = dirs
    install(proj, home)
    claude = (proj / ".claude" / "skills" / "verinoda-improve" / "SKILL.md").read_text("utf-8")
    codex = (proj / ".agents" / "skills" / "verinoda-improve" / "SKILL.md").read_text("utf-8")
    assert "AskUserQuestion" in claude and "request_user_input" not in claude and "/verinoda-improve" in claude
    assert "request_user_input" in codex and "AskUserQuestion" not in codex and "$verinoda-improve" in codex
    assert "there is no slash command in Codex" in codex
    assert re.findall(r"(?<!\\)\$[A-Za-z0-9_]+", claude) == ["$ARGUMENTS"]    # the only substitution Claude Code makes
    for text in (claude, codex, (proj / ".claude" / "skills" / "verinoda-live" / "SKILL.md").read_text("utf-8")):
        assert "{{" not in text and selffiles.MARKER in text
        assert len(text.splitlines()) < 120
    fm = claude.split("---")[1]
    assert "Bash(verinoda improve *)" in fm and "PowerShell(verinoda improve *)" in fm
    assert "AskUserQuestion" not in fm                                           # it is asked, not allow-listed


def _subcommands(parser) -> dict:
    import argparse

    for a in parser._actions:
        if isinstance(a, argparse._SubParsersAction):
            return a.choices
    return {}


def test_every_command_the_skills_name_exists(dirs):
    proj, home = dirs
    install(proj, home)
    top = _subcommands(cli.build_parser())
    seen = 0
    for skill in ("improve", "live"):
        for agent, folder in (("claude", ".claude"), ("codex", ".agents")):
            text = (proj / folder / "skills" / f"verinoda-{skill}" / "SKILL.md").read_text("utf-8")
            for m in re.finditer(r"`verinoda (live|improve) ([a-z-]+)", text):
                group, sub = m.groups()
                if sub in ("hook",):
                    continue
                assert sub in _subcommands(top[group]), f"`verinoda {group} {sub}` ({agent} {skill}) is not a command"
                seen += 1
    assert seen > 30


def test_a_skill_that_is_not_ours_is_not_overwritten_and_a_local_edit_is_kept(dirs):
    proj, home = dirs
    skill = proj / ".claude" / "skills" / "verinoda-improve" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: mine\n---\nmy own skill\n", encoding="utf-8")
    res = install(proj, home, agents=("claude",))
    by = {s["skill"]: s for s in res["results"][0]["skills"]}
    assert by["verinoda-improve"]["result"] == "refused" and res["ok"] is False
    assert skill.read_text("utf-8").endswith("my own skill\n")
    assert by["verinoda-live"]["result"] == "created"
    skill.unlink()
    install(proj, home, agents=("claude",))
    skill.write_text(skill.read_text("utf-8") + "\nmy note\n", encoding="utf-8")
    again = install(proj, home, agents=("claude",))
    assert {s["skill"]: s["result"] for s in again["results"][0]["skills"]}["verinoda-improve"] == "kept"
    assert "my note" in skill.read_text("utf-8")
    live_install.run("uninstall", ["claude"], "project", proj, home=home)
    assert skill.exists()                                                       # edited after install: left alone


def test_dry_run_writes_nothing(dirs):
    proj, home = dirs
    res = install(proj, home, dry_run=True)
    assert res["dry_run"] and not (proj / ".claude").exists() and not (proj / ".codex").exists()
    assert not (proj / ".verinoda").exists()


def test_codex_gets_the_note_about_its_feature_flag_and_claude_the_one_about_a_shared_file(dirs):
    proj, home = dirs
    res = install(proj, home)
    notes = {r["agent"]: " ".join(r["notes"]) for r in res["results"]}
    assert "codex_hooks = true" in notes["codex"] and "trusted project" in notes["codex"]
    assert "usually committed" in notes["claude"]
    assert "usually committed" not in " ".join(install(proj, home, agents=("claude",), scope="user")["results"][0]["notes"])


def test_a_program_that_is_not_on_path_is_named_and_warned_about_for_a_project_file(dirs, monkeypatch):
    proj, home = dirs
    monkeypatch.setattr(installer, "resolve_launcher", lambda: {**LAUNCHER, "how": "interpreter",
                                                                "argv": ["/opt/py 3/python", "-P", "-m", "verinoda"]})
    res = install(proj, home, agents=("claude",), with_skills=False)
    cmd = read(proj / ".claude" / "settings.json")["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert cmd == '"/opt/py 3/python" -P -m verinoda live hook'
    assert live_install.OURS.search(cmd)
    assert res["results"][0]["hooks"]["warnings"]
    live_install.run("uninstall", ["claude"], "project", proj, home=home, with_skills=False)
    assert not (proj / ".claude" / "settings.json").exists()


def test_the_skills_are_verinodas_own_files_and_stay_out_of_the_projects_corpus(dirs):
    proj, home = dirs
    install(proj, home)
    own = selffiles.own_filter(proj)
    for rel in (".claude/skills/verinoda-live/SKILL.md", ".claude/skills/verinoda-improve/SKILL.md",
                ".agents/skills/verinoda-live/SKILL.md", ".agents/skills/verinoda-improve/SKILL.md"):
        assert own(rel), rel
    assert not own(".claude/skills/other/SKILL.md")


def test_the_cli_installs_reports_and_removes(dirs, capsys):
    proj, home = dirs
    base = ["--agents", "claude", "--project-dir", str(proj)]
    assert cli.main(["live", "install", *base]) == 0
    out = capsys.readouterr().out
    assert "claude (project) hooks: created" in out and "verinoda-improve: created" in out
    assert cli.main(["live", "hooks", *base, "--json"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["results"][0]["hooks"]["result"] == "installed"
    assert cli.main(["live", "uninstall", *base]) == 0
    assert "hooks: removed file" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="unknown agent none-such"):
        cli.main(["live", "install", "--agents", "none-such", "--project-dir", str(proj)])


def test_no_agent_found_says_so(dirs, monkeypatch, capsys):
    proj, _home = dirs
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert cli.main(["live", "install", "--project-dir", str(proj)]) == 1
    assert "no agent found on PATH" in capsys.readouterr().out
    assert live.settings(proj)["refresh"] is True
