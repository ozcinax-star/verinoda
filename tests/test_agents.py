"""Tests for verinoda.agents: Claude Code / Codex skill + MCP installer.

Every test uses a temporary home and project directory. ``Path.home`` is
redirected to a non-existent guard path so an accidental use of the real home
directory fails loudly, and the external ``claude`` CLI is always stubbed.
"""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import contextlib  # noqa: E402
import difflib  # noqa: E402
import hashlib  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import shlex  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402
from types import SimpleNamespace  # noqa: E402

import pytest  # noqa: E402

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

from verinoda import agents  # noqa: E402
from verinoda.agents import installer as ins  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "orders_app"
ALL = [(a, s) for a in ("claude", "codex") for s in ("project", "user")]


# -- fixtures / helpers --------------------------------------------------------------

def _no_run(argv, env=None, timeout=120):  # pragma: no cover - must never be reached
    raise AssertionError(f"unexpected external command: {argv}")


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    proj = tmp_path / "proj"
    home.mkdir()
    proj.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    exe = bindir / "verinoda.exe"
    # a console-script launcher for the running interpreter, which imports this package: the PATH
    # `verinoda` is this build, so it is what gets registered (see the build-skew tests below)
    exe.write_bytes(f"#!{sys.executable}\n".encode("utf-8"))
    monkeypatch.setattr(ins, "_import_location", lambda python: str(ins.PKG_DIR))
    guard = tmp_path / "REAL-HOME-MUST-NOT-BE-USED"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: guard))
    monkeypatch.setenv("HOME", str(guard))
    monkeypatch.setenv("USERPROFILE", str(guard))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    which = {"verinoda": str(exe), "claude": None}
    monkeypatch.setattr(ins, "_which", lambda name: which.get(name))
    monkeypatch.setattr(ins, "_run", _no_run)
    from verinoda import doctor

    layout = {"path": str(bindir), "hardlinked": False, "editable": False}  # a copy-mode install by default
    monkeypatch.setattr(doctor, "install_layout", lambda: dict(layout))
    ns = SimpleNamespace(tmp=tmp_path, home=home, proj=proj, exe=str(exe), which=which, bindir=bindir,
                         layout=layout)
    yield ns
    assert not guard.exists(), "the real home directory would have been touched"


def tree(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): (p.read_bytes() if p.is_file() else None)
            for p in sorted(root.rglob("*"))}


def sha(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


def manifest(root: Path) -> dict:
    return json.loads((root / ".verinoda" / "install-manifest.json").read_text(encoding="utf-8"))


def only_insertions(before: str, after: str) -> bool:
    ops = difflib.SequenceMatcher(a=before, b=after, autojunk=False).get_opcodes()
    return all(op in ("equal", "insert") for op, *_ in ops)


class FakeClaude:
    """Stub of the `claude mcp add/remove --scope user` CLI writing <home>/.claude.json."""

    def __init__(self, home: Path):
        self.home = home
        self.calls: list[tuple[list[str], dict | None]] = []

    def __call__(self, argv, env=None, timeout=120):
        self.calls.append((list(argv), env))
        cfg = self.home / ".claude.json"
        data = json.loads(cfg.read_text(encoding="utf-8")) if cfg.exists() else {}
        tail = argv[1:]
        if tail[:5] == ["mcp", "add", "--scope", "user", "verinoda"]:
            assert tail[5] == "--"
            cmd = tail[6:]
            data.setdefault("mcpServers", {})["verinoda"] = {"type": "stdio", "command": cmd[0],
                                                               "args": cmd[1:], "env": {}}
        elif tail[:5] == ["mcp", "remove", "--scope", "user", "verinoda"]:
            data.get("mcpServers", {}).pop("verinoda", None)
        else:  # pragma: no cover
            raise AssertionError(argv)
        cfg.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return {"returncode": 0, "stdout": "", "stderr": ""}


def frontmatter(text: str) -> tuple[dict, str]:
    assert text.startswith("---\n"), "frontmatter must start on line 1"
    head, _, body = text[4:].partition("\n---\n")
    fm: dict = {}
    key = None
    for line in head.splitlines():
        if line.startswith("  - ") and key:
            fm.setdefault(key, []).append(line[4:])
        elif ":" in line and not line.startswith(" "):
            key, _, val = line.partition(":")
            fm[key] = val.strip() or []
    return fm, body


# -- templates ---------------------------------------------------------------------------

STATUSES = ("observed", "experiment_verified", "statically_verified", "primary_source_verified",
            "strong_inference", "weak_inference", "unknown", "contradicted", "stale")


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_skill_templates_are_complete(env, agent):
    text = agents.render_skill(agent).decode("utf-8")
    fm, body = frontmatter(text)
    assert fm["name"] == "verinoda"
    assert 100 < len(fm["description"]) <= 1536
    assert body.lstrip().startswith(agents.MARKER)
    assert "{{" not in text and "recorded at install time is `verinoda`" in text  # on PATH -> plain name
    # audit: the fallback must lead somewhere even when the recorded command is the bare name
    assert "use the MCP tools" in body and "absolute path" in body
    env.which["verinoda"] = None  # off PATH: the skill names the concrete executable/interpreter
    off_path = agents.render_skill(agent).decode("utf-8")
    assert ins.cli_hint() in off_path and ins.cli_hint() != "verinoda"
    for s in STATUSES:
        assert f"`{s}`" in body, s
    for needle in ("path:line", "feedback add", "--process", "verinoda update .", "next_step",
                   "usage.exhausted", "Never present `weak_inference` or `unknown` as fact"):
        assert needle in body, needle
    # audit: a verified status is "verified at snapshot X with evidence", never unconditional fact
    assert "fact, cite" not in body and "verified at snapshot X" in body and "snapshot.commit" in body
    assert body.count("| fact") == 0
    assert len(body.splitlines()) < 240  # concise: the business logic lives in the core
    if agent == "claude":
        # exactly one argument placeholder: any other $word would be substituted by Claude Code
        assert re.findall(r"(?<!\\)\$[A-Za-z0-9_]+", body) == ["$ARGUMENTS"]
        assert fm["argument-hint"]
        assert "Bash(verinoda analyze *)" in fm["allowed-tools"]
        for tool in ("Bash", "PowerShell"):
            for cmd in ("plan", "resolve"):
                assert f"{tool}(verinoda {cmd} *)" in fm["allowed-tools"], (tool, cmd)
        assert not any("experiment" in t or "research" in t for t in fm["allowed-tools"])
        assert "AskUserQuestion" in body
    else:
        assert "$verinoda" in body
        assert "There is no `/verinoda` slash command in Codex" in body
        assert body.count("/verinoda") == 1  # only in the statement that it does not exist
        assert "$ARGUMENTS" not in body
        assert "request_user_input" in body and "plain-text question" in body


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_skills_understand_the_question_and_resolve_references_first(agent):
    """D9 and the reference protocol: the skills drive the core; they do not re-implement it."""
    _, body = frontmatter(agents.render_skill(agent).decode("utf-8"))
    understand = body.split("## Understand the question first", 1)[1].split("\n## ", 1)[0]
    for needle in ("verinoda plan draft", "question_plan_draft", "split compound questions", "gloss_en",
                   "copy versions exactly as the user wrote them", "never add a candidate",
                   "verinoda plan check", "question_plan_check", "plan_json", "answers", "clarification_id",
                   "verinoda analyze --plan", "Understood as / Anladığım", "one block per",
                   "sub-question with its verdict", "blocked_by_clarification"):
        assert needle in understand, needle
    # the steps come in order: draft -> edit -> check -> ask -> analyze -> answer
    order = [understand.index(k) for k in ("plan draft", "split compound", "plan check", "Ask only",
                                            "analyze --plan", "Understood as")]
    assert order == sorted(order)
    refs = body.split("## References the user gives", 1)[1].split("\n## ", 1)[0]
    for needle in ("links", "package names", "versions", "commits", "PR/issue", "papers", "docs",
                   'verinoda resolve "<message>"', "reference_resolve", "<name> @ <pin> (basis: <basis>)",
                   "mismatch", "Never substitute the default branch", "questions_for_user", "next_step",
                   "verinoda research --resolution <id> --reference-id <rN>"):
        assert needle in refs, needle
    # deeper verification is optional and named
    assert "--observe" in body and "verinoda observe" in body and "resolve-call" in body
    assert "Optional deeper verification" in body
    # the answer format starts with the reading of the question
    fmt = body.split("## Answer format", 1)[1]
    assert fmt.lstrip().startswith('1. "Understood as / Anladığım: ..."')


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_skill_examples_parse_with_the_real_cli(agent):
    from verinoda.cli import build_parser

    text = agents.render_skill(agent).decode("utf-8")
    blocks = re.findall(r"```bash\n(.*?)```", text, re.S)
    lines = [ln for b in blocks for ln in b.splitlines() if ln.startswith("verinoda ")]
    seen = set()
    for ln in lines:
        argv = shlex.split(ln)[1:]
        build_parser().parse_args(argv)  # SystemExit (argparse error) fails the test
        seen.add(" ".join(argv[:2]) if argv[0] in ("feedback", "experiment", "claim") else argv[0])
        # text by default (2026-09-26): the CLI's text is written for the model; JSON cost 2-5x the tokens
        # (analyze 3,824 vs 717, decide brief 13,206 vs 2,985) and the skill reads no field the text leaves out
        assert "--json" not in argv, ln
    for cmd in ("analyze", "trace", "verify", "research", "challenge", "query", "map", "update",
                "feedback add", "feedback process", "experiment run", "compare", "claim show",
                "plan", "resolve", "observe", "resolve-call"):
        assert cmd in seen, cmd
    assert "Add `--json` only for a field the text" in " ".join(text.split())
    assert "doctor --brief" in text and "doctor --json" not in text
    # the plan flow is shown end to end: draft, check, then analyze with the plan file
    assert any(ln.startswith("verinoda plan draft ") for ln in lines)
    assert any(ln.startswith("verinoda plan check ") for ln in lines)
    assert any(ln.startswith("verinoda analyze --plan ") for ln in lines)


# -- project scope ---------------------------------------------------------------------------

def test_claude_project_install_is_idempotent_and_uninstalls_cleanly(env):
    r = agents.install("claude", "project", project_dir=env.proj, home=env.home)
    assert r["ok"] and r["result"] == "installed", r
    skill = env.proj / ".claude" / "skills" / "verinoda" / "SKILL.md"
    data = skill.read_bytes()
    assert agents.MARKER.encode() in data
    mcp = json.loads((env.proj / ".mcp.json").read_text(encoding="utf-8"))
    # no project path in the file: the server finds the folder holding .mcp.json (moving the project is fine)
    assert mcp == {"mcpServers": {"verinoda": {
        "type": "stdio", "command": env.exe, "args": ["mcp", "serve", "--repo-of", ".mcp.json"]}}}
    assert str(env.proj) not in (env.proj / ".mcp.json").read_text(encoding="utf-8")
    m = manifest(env.proj)
    items = m["installs"]["claude"]["items"]
    f = next(i for i in items if i["kind"] == "file")
    assert f["path"] == ".claude/skills/verinoda/SKILL.md" and f["sha256"] == sha(data)
    assert f["created_dirs"] == [".claude", ".claude/skills", ".claude/skills/verinoda"]
    j = next(i for i in items if i["kind"] == "json_key")
    assert j["path"] == ".mcp.json" and j["key"] == ["mcpServers", "verinoda"] and j["created_file"]
    assert m["created"]["dirs"] == [".verinoda"]
    assert (env.proj / ".verinoda" / ".gitignore").read_text(encoding="utf-8") == "*\n"
    assert any("/verinoda <question>" in n for n in r["notes"])

    before = tree(env.tmp)
    r2 = agents.install("claude", "project", project_dir=env.proj, home=env.home)
    assert r2["ok"] and r2["result"] == "unchanged"
    assert {a["op"] for a in r2["actions"]} == {"unchanged"}
    assert tree(env.tmp) == before  # byte-identical, manifest included

    u = agents.uninstall("claude", "project", project_dir=env.proj, home=env.home)
    assert u["ok"] and u["result"] == "uninstalled", u
    assert tree(env.proj) == {}  # everything the install created is gone
    assert tree(env.home) == {}


def test_codex_project_install_and_uninstall(env):
    r = agents.install("codex", "project", project_dir=env.proj, home=env.home)
    assert r["ok"] and r["result"] == "installed"
    skill = env.proj / ".agents" / "skills" / "verinoda" / "SKILL.md"
    assert skill.exists()
    cfg = tomllib.loads((env.proj / ".codex" / "config.toml").read_text(encoding="utf-8"))
    assert cfg == {"mcp_servers": {"verinoda": {
        "command": env.exe, "args": ["mcp", "serve", "--repo", str(env.proj.resolve())],
        "startup_timeout_sec": ins.CODEX_STARTUP_TIMEOUT}}}
    assert any("trusted" in n for n in r["notes"])
    assert any("no /verinoda slash command" in n for n in r["notes"])
    toml_item = next(i for i in manifest(env.proj)["installs"]["codex"]["items"] if i["kind"] == "toml_block")
    assert toml_item["key"] == "mcp_servers.verinoda" and toml_item["created_file"]
    assert toml_item["created_dirs"] == [".codex"]
    assert agents.install("codex", "project", project_dir=env.proj, home=env.home)["result"] == "unchanged"
    u = agents.uninstall("codex", "project", project_dir=env.proj, home=env.home)
    assert u["ok"] and u["result"] == "uninstalled"
    assert tree(env.proj) == {}


def test_both_agents_share_one_project_manifest(env):
    agents.install("claude", "project", project_dir=env.proj, home=env.home)
    agents.install("codex", "project", project_dir=env.proj, home=env.home)
    assert set(manifest(env.proj)["installs"]) == {"claude", "codex"}
    agents.uninstall("claude", "project", project_dir=env.proj, home=env.home)
    m = manifest(env.proj)
    assert set(m["installs"]) == {"codex"}
    assert not (env.proj / ".claude").exists() and (env.proj / ".agents").exists()
    agents.uninstall("codex", "project", project_dir=env.proj, home=env.home)
    assert tree(env.proj) == {}


def test_install_keeps_preexisting_dirs_and_verinoda_state(env):
    (env.proj / ".claude").mkdir()
    (env.proj / ".claude" / "settings.json").write_bytes(b"{}\n")
    (env.proj / ".verinoda").mkdir()
    (env.proj / ".verinoda" / "atlas.db").write_bytes(b"db")
    agents.install("claude", "project", project_dir=env.proj, home=env.home, with_mcp=False)
    m = manifest(env.proj)
    assert m["created"] == {"dirs": [], "files": []}
    f = m["installs"]["claude"]["items"][0]
    assert f["created_dirs"] == [".claude/skills", ".claude/skills/verinoda"]
    assert len(m["installs"]["claude"]["items"]) == 1  # --no-mcp: skill only
    assert not (env.proj / ".mcp.json").exists()
    agents.uninstall("claude", "project", project_dir=env.proj, home=env.home)
    assert tree(env.proj) == {".claude": None, ".claude/settings.json": b"{}\n",
                              ".verinoda": None, ".verinoda/atlas.db": b"db"}


# -- user scope ----------------------------------------------------------------------------------

def test_claude_user_scope_uses_claude_cli(env, monkeypatch):
    fake = FakeClaude(env.home)
    monkeypatch.setattr(ins, "_run", fake)
    claude_exe = str(env.bindir / "claude.exe")
    env.which["claude"] = claude_exe
    cfg = env.home / ".claude.json"
    cfg.write_text(json.dumps({"numStartups": 7, "mcpServers": {"other": {"command": "x", "args": []}}}),
                   encoding="utf-8")

    r = agents.install("claude", "user", project_dir=env.proj, home=env.home)
    assert r["ok"] and r["result"] == "installed", r
    assert (env.home / ".claude" / "skills" / "verinoda" / "SKILL.md").exists()
    argv, cenv = fake.calls[0]
    assert argv == [claude_exe, "mcp", "add", "--scope", "user", "verinoda", "--", env.exe, "mcp", "serve"]
    assert cenv["USERPROFILE"] == str(env.home.resolve()) and "CLAUDE_CONFIG_DIR" not in cenv
    assert not (env.proj / ".mcp.json").exists() and tree(env.proj) == {}
    item = next(i for i in manifest(env.home)["installs"]["claude"]["items"] if i["kind"] == "command")
    assert item["argv"][:7] == ["claude", "mcp", "add", "--scope", "user", "verinoda", "--"]
    assert item["undo_argv"] == ["claude", "mcp", "remove", "--scope", "user", "verinoda"]
    assert item["runs"][0]["returncode"] == 0 and item["entry"]["args"] == ["mcp", "serve"]

    r2 = agents.install("claude", "user", project_dir=env.proj, home=env.home)
    assert r2["result"] == "unchanged" and len(fake.calls) == 1  # no second `claude mcp add`

    u = agents.uninstall("claude", "user", project_dir=env.proj, home=env.home)
    assert u["ok"] and u["result"] == "uninstalled", u
    assert fake.calls[-1][0] == [claude_exe, "mcp", "remove", "--scope", "user", "verinoda"]
    left = json.loads(cfg.read_text(encoding="utf-8"))
    assert left == {"numStartups": 7, "mcpServers": {"other": {"command": "x", "args": []}}}
    assert set(tree(env.home)) == {".claude.json"}


def test_claude_user_scope_without_cli_writes_nothing_to_claude_json(env):
    r = agents.install("claude", "user", project_dir=env.proj, home=env.home)
    assert r["ok"], r
    assert not (env.home / ".claude.json").exists()
    assert r["manual"] and r["manual"][0].startswith("claude mcp add --scope user verinoda --")
    assert any(a["op"] == "manual" for a in r["actions"])
    kinds = {i["kind"] for i in manifest(env.home)["installs"]["claude"]["items"]}
    assert kinds == {"file"}
    assert agents.uninstall("claude", "user", project_dir=env.proj, home=env.home)["ok"]
    assert tree(env.home) == {}


def test_claude_user_scope_refuses_foreign_server(env, monkeypatch):
    monkeypatch.setattr(ins, "_run", FakeClaude(env.home))
    env.which["claude"] = str(env.bindir / "claude.exe")
    (env.home / ".claude.json").write_text(json.dumps(
        {"mcpServers": {"verinoda": {"type": "stdio", "command": "somebody-else", "args": []}}}), encoding="utf-8")
    before = tree(env.tmp)
    r = agents.install("claude", "user", project_dir=env.proj, home=env.home)
    assert not r["ok"] and r["result"] == "refused"
    assert "not added by Verinoda" in " ".join(r["errors"])
    assert tree(env.tmp) == before


def test_cmd_shim_with_unsafe_arguments_is_not_run(env, tmp_path):
    weird = tmp_path / "a&b"
    weird.mkdir()
    (weird / "verinoda.exe").write_bytes(f"#!{sys.executable}\n".encode("utf-8"))  # this build, registered
    env.which["verinoda"] = str(weird / "verinoda.exe")
    env.which["claude"] = "C:/tools/claude.cmd"
    r = agents.install("claude", "user", project_dir=env.proj, home=env.home)  # _run would raise
    assert r["ok"] and r["manual"] and "cmd.exe" in " ".join(r["warnings"])


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("final_eol", [True, False])
def test_codex_user_config_is_preserved_byte_for_byte(env, newline, final_eol):
    lines = ["# my codex settings", 'model = "o3"  # keep this comment', "",
             "[mcp_servers.other]", 'command = "npx"', 'args = ["-y", "other-mcp"]', "",
             "[mcp_servers.other.env]", 'TOKEN_NAME = "X"', "",
             "# trailing comment about projects", "[projects.'C:\\work\\x']", 'trust_level = "trusted"']
    original = newline.join(lines) + (newline if final_eol else "")
    cfg = env.home / ".codex" / "config.toml"
    cfg.parent.mkdir()
    cfg.write_bytes(original.encode("utf-8"))

    r = agents.install("codex", "user", project_dir=env.proj, home=env.home)
    assert r["ok"] and r["result"] == "installed", r
    new = cfg.read_bytes().decode("utf-8")
    assert new.startswith(original)  # appended; nothing before it rewritten
    parsed = tomllib.loads(new)
    assert parsed["mcp_servers"]["other"] == {"command": "npx", "args": ["-y", "other-mcp"], "env": {"TOKEN_NAME": "X"}}
    assert parsed["mcp_servers"]["verinoda"]["args"] == ["mcp", "serve"]  # user scope: no --repo
    assert parsed["model"] == "o3" and parsed["projects"]["C:\\work\\x"]["trust_level"] == "trusted"
    block = new[len(original):]
    if newline == "\r\n":
        assert "\r\n" in block and "\n" not in block.replace("\r\n", "")
    assert (env.home / ".agents" / "skills" / "verinoda" / "SKILL.md").exists()

    snap = tree(env.home)
    assert agents.install("codex", "user", project_dir=env.proj, home=env.home)["result"] == "unchanged"
    assert tree(env.home) == snap

    u = agents.uninstall("codex", "user", project_dir=env.proj, home=env.home)
    assert u["ok"] and u["result"] == "uninstalled", u
    assert cfg.read_bytes() == original.encode("utf-8")  # exact restore
    assert set(tree(env.home)) == {".codex", ".codex/config.toml"}


def test_codex_home_env_is_respected_only_without_explicit_home(env, monkeypatch, tmp_path):
    ch = tmp_path / "codexhome"
    monkeypatch.setenv("CODEX_HOME", str(ch))
    t = ins._target("codex", "user", env.proj, env.home, explicit_home=True)
    assert t.mcp_path == env.home / ".codex" / "config.toml"
    t = ins._target("codex", "user", env.proj, env.home, explicit_home=False)
    assert t.mcp_path == ch / "config.toml"
    assert t.skill == env.home / ".agents" / "skills" / "verinoda" / "SKILL.md"



def test_claude_user_uninstall_without_cli_keeps_record(env, monkeypatch):
    fake = FakeClaude(env.home)
    monkeypatch.setattr(ins, "_run", fake)
    env.which["claude"] = str(env.bindir / "claude.exe")
    assert agents.install("claude", "user", project_dir=env.proj, home=env.home)["ok"]
    env.which["claude"] = None
    u = agents.uninstall("claude", "user", project_dir=env.proj, home=env.home)
    assert u["ok"] and u["manual"] == ["claude mcp remove --scope user verinoda"]
    assert len(fake.calls) == 1  # nothing run
    kept = manifest(env.home)["installs"]["claude"]["items"]
    assert [i["kind"] for i in kept] == ["command"]  # retried by a later uninstall
    env.which["claude"] = str(env.bindir / "claude.exe")
    assert agents.uninstall("claude", "user", project_dir=env.proj, home=env.home)["result"] == "uninstalled"
    assert fake.calls[-1][0][1:3] == ["mcp", "remove"]


def test_claude_user_uninstall_leaves_changed_server(env, monkeypatch):
    fake = FakeClaude(env.home)
    monkeypatch.setattr(ins, "_run", fake)
    env.which["claude"] = str(env.bindir / "claude.exe")
    agents.install("claude", "user", project_dir=env.proj, home=env.home)
    cfg = env.home / ".claude.json"
    d = json.loads(cfg.read_text(encoding="utf-8"))
    d["mcpServers"]["verinoda"]["args"].append("--verbose")
    cfg.write_text(json.dumps(d), encoding="utf-8")
    u = agents.uninstall("claude", "user", project_dir=env.proj, home=env.home)
    assert u["result"] == "partially_uninstalled" and len(fake.calls) == 1
    assert "--verbose" in cfg.read_text(encoding="utf-8")

# -- shared JSON config -----------------------------------------------------------------------------

MCP_JSON_VARIANTS = {
    "four_space": ('{\n    "$schema": "x",\n    "mcpServers": {\n        "db": {\n            "command": "db-mcp",\n'
                   '            "args": ["--ro"],\n            "env": {"TOKEN": "${TOKEN}"}\n        }\n    },\n'
                   '    "other": [1, 2]\n}\n'),
    "one_line": '{"mcpServers":{"db":{"command":"db-mcp"}},"other":1}',
    "no_servers_crlf": '{\r\n  "other": true\r\n}\r\n',
    "empty_servers": '{\n  "mcpServers": {}\n}\n',
    "tricky_strings": '{"x": "}{\\"[", "mcpServers": {"db": {"command": "a\\"b}", "args": ["{,}"]}}}\n',
    "tabs": '{\n\t"mcpServers": {\n\t\t"db": {\n\t\t\t"command": "x"\n\t\t}\n\t}\n}\n',
}


@pytest.mark.parametrize("variant", sorted(MCP_JSON_VARIANTS))
def test_mcp_json_keeps_unrelated_keys(env, variant):
    original = MCP_JSON_VARIANTS[variant]
    p = env.proj / ".mcp.json"
    p.write_bytes(original.encode("utf-8"))
    orig_data = json.loads(original)

    r = agents.install("claude", "project", project_dir=env.proj, home=env.home)
    assert r["ok"], r
    assert not r["warnings"], r["warnings"]  # spliced in place, no re-serialisation
    new = p.read_bytes().decode("utf-8")
    assert only_insertions(original, new), new
    data = json.loads(new)
    assert data["mcpServers"]["verinoda"]["command"] == env.exe
    for k, v in orig_data.items():
        if k != "mcpServers":
            assert data[k] == v
    for k, v in (orig_data.get("mcpServers") or {}).items():
        assert data["mcpServers"][k] == v

    assert agents.install("claude", "project", project_dir=env.proj, home=env.home)["result"] == "unchanged"
    u = agents.uninstall("claude", "project", project_dir=env.proj, home=env.home)
    assert u["ok"], u
    assert p.read_bytes() == original.encode("utf-8")  # exact restore


def test_mcp_json_entry_update_when_command_changes(env):
    agents.install("claude", "project", project_dir=env.proj, home=env.home)
    newexe = env.bindir / "verinoda2.exe"
    newexe.write_bytes(f"#!{sys.executable}\n".encode("utf-8"))
    env.which["verinoda"] = str(newexe)
    r = agents.install("claude", "project", project_dir=env.proj, home=env.home)
    assert r["ok"] and r["result"] == "updated", r
    data = json.loads((env.proj / ".mcp.json").read_text(encoding="utf-8"))
    assert data["mcpServers"]["verinoda"]["command"] == str(newexe)
    assert agents.uninstall("claude", "project", project_dir=env.proj, home=env.home)["result"] == "uninstalled"
    assert tree(env.proj) == {}


# -- which build the agents start (senior review gap 14) ----------------------------------------------

OTHER_PY = r"C:\other\venv\Scripts\python.exe" if os.name == "nt" else "/other/venv/bin/python"


def test_launcher_python_reads_the_interpreter_without_running_it(tmp_path):
    def f(name: str, data: bytes) -> Path:
        p = tmp_path / name
        p.write_bytes(data)
        return p

    assert ins.launcher_python(f("posix", b"#!/opt/venv/bin/python\nimport sys\n")) == "/opt/venv/bin/python"
    assert ins.launcher_python(f("pip_long", b"#!/bin/sh\n'''exec' '/very long/venv/bin/python' \"$0\" \"$@\"\n' '''\n")
                               ) == "/very long/venv/bin/python"
    assert ins.launcher_python(f("env", b"#!/usr/bin/env python3\n")) is None  # decided by PATH at start-up
    # Windows launchers (pip/distlib: shebang + zip appended; uv: the same lines in a resource)
    exe = b"MZ\x90\x00" + b"\x00" * 64 + b"#!C:\\Users\\x\\venv\\Scripts\\python.exe\n# -*- coding: utf-8 -*-\nimport sys\n"
    assert ins.launcher_python(f("a.exe", exe + b"PK\x03\x04")) == r"C:\Users\x\venv\Scripts\python.exe"
    quoted = b"MZ" + b"\x00" * 64 + b'#!"C:\\Program Files\\Python312\\python.exe"\r\n' + b"PK\x03\x04"
    assert ins.launcher_python(f("b.exe", quoted)) == r"C:\Program Files\Python312\python.exe"
    assert ins.launcher_python(f("empty.exe", b"")) is None
    assert ins.launcher_python(tmp_path / "missing.exe") is None
    assert ins.launcher_python(tmp_path) is None  # only regular files are read (a config may name anything)
    if os.path.exists("/dev/zero"):
        assert ins.launcher_python("/dev/zero") is None
    real = shutil.which("verinoda")
    if real and os.name == "nt":  # the machine's own launcher (uv or pip) names a python.exe
        lp = ins.launcher_python(real)
        assert lp is None or lp.lower().endswith(("python.exe", "pythonw.exe"))


def test_the_import_check_never_imports_a_package_from_a_shared_or_start_folder(tmp_path, monkeypatch):
    """A `verinoda/` left (or planted) in the temp folder, or in the folder the check starts in, is never
    imported: its code would run as whoever runs setup/install/doctor, and it would be reported as the
    build the agents start. The check starts in a new private folder with the start folder off sys.path."""
    marker = tmp_path / "PLANTED_RAN"

    def plant(folder: Path) -> Path:
        (folder / "verinoda").mkdir(parents=True)
        (folder / "verinoda" / "__init__.py").write_text(f"open({str(marker)!r}, 'w').write('ran')\n",
                                                         encoding="utf-8")
        return folder

    shared = plant(tmp_path / "shared-tmp")
    monkeypatch.setattr(tempfile, "tempdir", str(shared))  # TMPDIR / TEMP: /tmp on Linux
    monkeypatch.setattr(ins, "_IMPORT_CACHE", {})
    loc = ins._import_location(sys.executable)
    assert not marker.exists() and (loc is None or not ins._inside(loc, shared)), loc
    assert [p.name for p in shared.iterdir()] == ["verinoda"]  # the private start folder is removed again

    made: list[Path] = []

    def mkdtemp(*a, **k):  # the start folder itself holding a package: -P / the probe's first statement
        made.append(plant(tmp_path / f"start{len(made)}"))
        return str(made[-1])

    monkeypatch.setattr(tempfile, "mkdtemp", mkdtemp)
    monkeypatch.setattr(ins, "_IMPORT_CACHE", {})
    loc = ins._import_location(sys.executable)
    assert made and not marker.exists() and (loc is None or not ins._inside(loc, made[0])), loc
    assert not made[0].exists()
    # Python 3.10 has no -P: the probe's first statement alone keeps the start folder off sys.path
    start = plant(tmp_path / "start-310")
    r = subprocess.run([sys.executable, "-E", "-c", ins._IMPORT_PROBE], cwd=start, capture_output=True, text=True,
                       timeout=120, stdin=subprocess.DEVNULL)
    assert not marker.exists() and str(start) not in r.stdout, r.stdout + r.stderr


def test_same_python_means_one_interpreter_of_one_environment(tmp_path):
    (tmp_path / "venv" / "bin").mkdir(parents=True)
    (tmp_path / "other" / "bin").mkdir(parents=True)
    py = tmp_path / "venv" / "bin" / "python3.12"
    py.write_bytes(b"interpreter")
    os.link(py, tmp_path / "venv" / "bin" / "python3")  # a venv's second name for it
    os.link(py, tmp_path / "other" / "bin" / "python3")  # the same base file, another environment
    assert ins.same_python(py, py) and ins.same_python(py, tmp_path / "venv" / "bin" / "python3")
    assert not ins.same_python(py, tmp_path / "other" / "bin" / "python3")
    assert not ins.same_python(py, tmp_path / "venv" / "bin" / "missing") and not ins.same_python(None, py)


def test_a_path_verinoda_of_another_build_is_not_registered(env, monkeypatch):
    """The lead's repro: setup from one build wrote .mcp.json for the older PATH build."""
    fake = FakeClaude(env.home)
    monkeypatch.setattr(ins, "_run", fake)
    env.which["claude"] = str(env.bindir / "claude.exe")
    other = env.bindir / "verinoda-old.exe"
    other.write_bytes(f"#!{OTHER_PY}\n".encode("utf-8"))
    env.which["verinoda"] = str(other)
    want = [sys.executable, *(["-P"] if sys.version_info >= (3, 11) else []), "-m", "verinoda"]

    r = agents.install("claude", "project", project_dir=env.proj, home=env.home)
    assert r["ok"], r
    entry = json.loads((env.proj / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["verinoda"]
    assert [entry["command"], *entry["args"]] == want + ["mcp", "serve", "--repo-of", ".mcp.json"]
    srv = r["server"]
    assert srv["how"] == "interpreter" and srv["path_exe"] == str(other) and srv["path_python"] == OTHER_PY
    assert "registered the running interpreter" in srv["note"] and OTHER_PY in srv["note"]
    assert any(str(other) in w and "is not the build running this command" in w for w in r["warnings"])
    skill = (env.proj / ".claude" / "skills" / "verinoda" / "SKILL.md").read_text(encoding="utf-8")
    assert f"recorded at install time is `{ins._display(want)}`." in skill
    assert "It is not the `verinoda` on PATH (another build)" in skill and "{{" not in skill
    env.which["verinoda"] = None
    off = agents.render_skill("codex").decode("utf-8")
    assert "`verinoda` is not on PATH here: run this command wherever" in off and "another build" not in off

    r = agents.install("claude", "user", project_dir=env.proj, home=env.home)
    assert r["ok"] and fake.calls[0][0][-(len(want) + 2):] == want + ["mcp", "serve"]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        agents.render(r)
    assert "server: registered the running interpreter" in buf.getvalue()


def test_the_same_interpreter_importing_another_copy_is_not_this_build(env, monkeypatch):
    # e.g. a dev run through PYTHONPATH while the venv has an editable install of another checkout
    monkeypatch.setattr(ins, "_import_location", lambda python: "/elsewhere/verinoda")
    r = agents.install("claude", "project", project_dir=env.proj, home=env.home, dry_run=True)
    assert r["server"]["how"] == "interpreter" and r["server"]["imports"] == "/elsewhere/verinoda"
    assert "imports /elsewhere/verinoda, not this build" in r["server"]["note"]
    assert any("/elsewhere/verinoda, not this build at" in w for w in r["warnings"])
    assert r["server"]["runs_ours"] is False and r["server"]["path_build"] == "other"


@pytest.mark.parametrize("imports", ["/elsewhere/verinoda", None])
def test_a_program_not_shown_to_be_this_build_gets_the_repo_every_build_knows(env, monkeypatch, imports):
    """--repo-of is new: an older build started by the entry exits with 'unrecognized arguments', so an
    entry whose program imports another copy (or none) keeps `--repo <project>` and says why."""
    monkeypatch.setattr(ins, "_import_location", lambda python: imports)
    r = agents.install("claude", "project", project_dir=env.proj, home=env.home)
    assert r["ok"], r
    entry = json.loads((env.proj / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["verinoda"]
    assert entry["command"] == sys.executable and "--repo-of" not in entry["args"]
    assert entry["args"][-4:] == ["mcp", "serve", "--repo", str(env.proj.resolve())]
    note = next(n for n in r["notes"] if "project .mcp.json" in n)
    assert "stores this project's path (--repo)" in note and "moving the project keeps it working" not in note
    if imports:
        assert any("stores the project path (--repo)" in w for w in r["warnings"])
    else:  # nothing imports: said as it is, not as another build
        assert any("could not import verinoda" in w and "may not start" in w for w in r["warnings"])
        assert r["server"]["path_build"] == "unknown"
    # once the interpreter imports this build, a re-install moves the entry to --repo-of
    monkeypatch.setattr(ins, "_import_location", lambda python: str(ins.PKG_DIR))
    r = agents.install("claude", "project", project_dir=env.proj, home=env.home)
    entry = json.loads((env.proj / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["verinoda"]
    assert r["result"] == "updated" and entry["args"][-2:] == ["--repo-of", ".mcp.json"]


def test_the_move_note_holds_only_when_the_program_is_outside_the_project(env):
    r = agents.install("claude", "project", project_dir=env.proj, home=env.home, dry_run=True)
    assert any("moving the project keeps it working" in n for n in r["notes"])
    inner = env.proj / ".venv" / "Scripts" / "verinoda.exe"  # a project-local venv's launcher on PATH
    inner.parent.mkdir(parents=True)
    inner.write_bytes(f"#!{sys.executable}\n".encode("utf-8"))
    env.which["verinoda"] = str(inner)
    r = agents.install("claude", "project", project_dir=env.proj, home=env.home)
    entry = json.loads((env.proj / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["verinoda"]
    assert entry["command"] == str(inner) and entry["args"][-2:] == ["--repo-of", ".mcp.json"]
    note = next(n for n in r["notes"] if "project .mcp.json" in n)
    assert "moving the project keeps it working" not in note
    assert f"its program is inside the project ({inner})" in note and "re-run setup after moving" in note


def test_a_path_launcher_whose_interpreter_cannot_be_read_is_unknown_not_another_build(env):
    """`#!/usr/bin/env python3` (or a shim of an unknown format) first on PATH, outside this Python's folder."""
    shim = env.bindir / "verinoda-env.exe"
    shim.write_bytes(b"#!/usr/bin/env python3\nimport sys\nfrom verinoda.cli import main\n")
    env.which["verinoda"] = str(shim)
    r = agents.install("claude", "project", project_dir=env.proj, home=env.home)
    srv = r["server"]
    assert srv["how"] == "interpreter" and srv["path_python"] is None and srv["path_build"] == "unknown"
    assert "is this build is unknown" in srv["note"] and "could not be read" in srv["note"]
    assert any("could not tell whether `verinoda` on PATH" in w for w in r["warnings"])
    assert not any("is not the build running this command" in w for w in r["warnings"])
    skill = (env.proj / ".claude" / "skills" / "verinoda" / "SKILL.md").read_text(encoding="utf-8")
    assert "Whether the `verinoda` on PATH is this build is not known" in skill and "another build" not in skill


def test_without_verinoda_on_path_the_running_interpreter_is_registered(env):
    env.which["verinoda"] = None
    r = agents.install("codex", "project", project_dir=env.proj, home=env.home)
    cfg = tomllib.loads((env.proj / ".codex" / "config.toml").read_text(encoding="utf-8"))["mcp_servers"]["verinoda"]
    assert cfg["command"] == sys.executable and cfg["args"][-4:] == ["mcp", "serve", "--repo", str(env.proj.resolve())]
    assert "`verinoda` is not on PATH" in r["server"]["note"] and not r["warnings"]


def test_an_absolute_repo_entry_from_an_older_install_is_migrated(env):
    """Older installs wrote `--repo <absolute project>`; the manifest lets a re-install replace it."""
    old = {"type": "stdio", "command": env.exe, "args": ["mcp", "serve", "--repo", str(env.proj.resolve())]}
    (env.proj / ".mcp.json").write_text(json.dumps({"mcpServers": {"verinoda": old}}, indent=2) + "\n",
                                        encoding="utf-8")
    (env.proj / ".verinoda").mkdir()
    (env.proj / ".verinoda" / "install-manifest.json").write_text(json.dumps({
        "version": 1, "tool": "verinoda", "scope": "project", "root": str(env.proj),
        "created": {"dirs": [], "files": []},
        "installs": {"claude": {"scope": "project", "root": str(env.proj), "verinoda_version": "0.1.0.dev0",
                                "with_mcp": True, "items": [
                                    {"kind": "json_key", "role": "mcp", "path": ".mcp.json",
                                     "key": ["mcpServers", "verinoda"], "sha256": ins._entry_hash(old),
                                     "created_file": True, "created_parent": True}]}}}), encoding="utf-8")
    st = agents.status(env.proj, home=env.home)["claude:project"]
    assert st["mcp_server"]["problems"] == []  # still this project: nothing to flag yet
    r = agents.install("claude", "project", project_dir=env.proj, home=env.home)
    assert r["ok"] and any(a["op"] == "update" and a["what"] == "mcp" for a in r["actions"]), r
    entry = json.loads((env.proj / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["verinoda"]
    assert entry["args"] == ["mcp", "serve", "--repo-of", ".mcp.json"]
    assert manifest(env.proj)["installs"]["claude"]["verinoda_build"]
    assert agents.uninstall("claude", "project", project_dir=env.proj, home=env.home)["result"] == "uninstalled"
    assert not (env.proj / ".mcp.json").exists()


def test_status_flags_a_moved_project_and_a_server_of_another_python(env):
    moved_from = env.tmp / "old-place"
    (env.proj / ".mcp.json").write_text(json.dumps({"mcpServers": {"verinoda": {
        "command": env.exe, "args": ["mcp", "serve", "--repo", str(moved_from)]}}}), encoding="utf-8")
    srv = agents.status(env.proj, home=env.home)["claude:project"]["mcp_server"]
    assert srv["build"] == "this" and srv["serves"] == str(moved_from)
    assert "not this project (moved or copied?)" in srv["problems"][0]
    (env.proj / ".mcp.json").write_text(json.dumps({"mcpServers": {"verinoda": {
        "command": env.exe, "args": ["mcp", "serve", "--repo", "."]}}}), encoding="utf-8")
    assert agents.status(env.proj, home=env.home)["claude:project"]["mcp_server"]["problems"] == []

    (env.proj / ".mcp.json").write_text(json.dumps({"mcpServers": {"verinoda": {
        "command": OTHER_PY, "args": ["-m", "verinoda", "mcp", "serve", "--repo-of", ".mcp.json"]}}}),
        encoding="utf-8")
    srv = agents.status(env.proj, home=env.home)["claude:project"]["mcp_server"]
    assert srv["build"] == "other" and srv["python"] == OTHER_PY
    # a project file names the program: doctor does not tell the user to run it
    assert "may be another Verinoda build" in srv["problems"][0] and "--version" not in srv["problems"][0]
    assert "check what it is before running it" in srv["problems"][0]
    older = env.bindir / "verinoda-old.exe"  # ... unless it is what the PATH `verinoda` starts
    older.write_bytes(f"#!{OTHER_PY}\n".encode("utf-8"))
    env.which["verinoda"] = str(older)
    srv = agents.status(env.proj, home=env.home)["claude:project"]["mcp_server"]
    assert "--version" in srv["problems"][0] and OTHER_PY in srv["problems"][0]
    env.which["verinoda"] = env.exe

    (env.proj / ".mcp.json").write_text(json.dumps({"mcpServers": {"verinoda": {
        "command": "verinoda", "args": ["mcp", "serve"]}}}), encoding="utf-8")  # a bare name: PATH now
    assert agents.status(env.proj, home=env.home)["claude:project"]["mcp_server"]["build"] == "this"
    env.which["verinoda"] = None
    assert agents.status(env.proj, home=env.home)["claude:project"]["mcp_server"]["build"] == "unknown"


def test_network_paths_named_by_a_project_config_are_never_touched(env, monkeypatch):
    """A clone's .mcp.json names `\\\\host\\share\\...`: on Windows a stat connects to that host with the
    user's credentials, before the user approved anything. Compared as text only; the result is unknown."""
    assert ins.network_path(r"\\host\share\v.exe") and ins.network_path("//host/share/v")
    assert ins.network_path(r"\\?\UNC\host\share\v.exe") and ins.network_path("\\/host/share")
    assert not ins.network_path(r"C:\venv\python.exe") and not ins.network_path("/opt/venv/bin/python")
    assert not ins.network_path("") and not ins.network_path(None)
    touched = []
    real_stat, real_read, real_os_stat = Path.stat, Path.read_bytes, os.stat

    def spy(real, what):
        def f(p, *a, **k):
            s = os.fspath(p) if not isinstance(p, int) else ""
            if ins.network_path(s):
                touched.append((what, s))
                raise OSError("refused by the test")
            return real(p, *a, **k)
        return f

    monkeypatch.setattr(Path, "stat", spy(real_stat, "Path.stat"))
    monkeypatch.setattr(Path, "read_bytes", spy(real_read, "Path.read_bytes"))
    monkeypatch.setattr(os, "stat", spy(real_os_stat, "os.stat"))
    assert ins.launcher_python(r"\\attacker.invalid\share\verinoda.exe") is None
    for command, args in ((r"\\attacker.invalid\share\verinoda.exe", ["mcp", "serve"]),
                          (r"\\attacker.invalid\share\python.exe", ["-m", "verinoda", "mcp", "serve"]),
                          ("//attacker.invalid/share/verinoda", ["mcp", "serve"])):
        (env.proj / ".mcp.json").write_text(json.dumps({"mcpServers": {"verinoda": {
            "command": command, "args": args}}}), encoding="utf-8")
        srv = agents.status(env.proj, home=env.home)["claude:project"]["mcp_server"]
        assert srv["build"] == "unknown" and srv["python"] is None
        assert srv["detail"] == f"unknown (network path not read: {command})"
        assert "network path" in srv["problems"][0] and "--version" not in srv["problems"][0]
        assert ins.same_python(command, sys.executable) is False
    assert touched == []


def test_bom_files_round_trip(env):
    bom = b"\xef\xbb\xbf"
    mcp = env.proj / ".mcp.json"
    mcp_orig = bom + b'{\n  "mcpServers": {\n    "db": {"command": "x"}\n  }\n}\n'
    mcp.write_bytes(mcp_orig)
    cfg = env.home / ".codex" / "config.toml"
    cfg.parent.mkdir()
    cfg_orig = bom + b'model = "o3"\n'
    cfg.write_bytes(cfg_orig)
    assert agents.install("claude", "project", project_dir=env.proj, home=env.home)["ok"]
    assert agents.install("codex", "user", project_dir=env.proj, home=env.home)["ok"]
    assert mcp.read_bytes().startswith(bom + b"{") and cfg.read_bytes().startswith(cfg_orig)
    assert "verinoda" in json.loads(mcp.read_bytes()[3:])["mcpServers"]
    agents.uninstall("claude", "project", project_dir=env.proj, home=env.home)
    agents.uninstall("codex", "user", project_dir=env.proj, home=env.home)
    assert mcp.read_bytes() == mcp_orig and cfg.read_bytes() == cfg_orig


@pytest.mark.parametrize("value", [r"C:\Users\o'brien\x.exe", r"C:\Program Files\x.exe", "plain", 'q"uote',
                                   "tab\there", "\u00fcn\u00ef"])
def test_toml_strings_round_trip(value):
    from verinoda.agents import _tomledit as te

    block = te.render_block("verinoda", {"command": value, "args": [value, "--repo"]})
    assert tomllib.loads(block)["mcp_servers"]["verinoda"] == {"command": value, "args": [value, "--repo"]}


# -- refusals ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("agent,scope", ALL)
def test_refuses_to_overwrite_foreign_skill(env, agent, scope):
    t = ins._target(agent, scope, env.proj.resolve(), env.home.resolve(), True)
    t.skill.parent.mkdir(parents=True)
    t.skill.write_text("---\nname: verinoda\ndescription: mine\n---\nmy own skill\n", encoding="utf-8")
    before = tree(env.tmp)
    r = agents.install(agent, scope, project_dir=env.proj, home=env.home)
    assert not r["ok"] and r["result"] == "refused"
    assert "not managed by Verinoda" in r["errors"][0]
    assert tree(env.tmp) == before  # nothing written at all (no MCP entry, no manifest)
    st = agents.status(env.proj, home=env.home)[f"{agent}:{scope}"]
    assert st["installed"] is False and st["skill"] == str(t.skill) and "not managed" in st["note"]


def test_refuses_foreign_mcp_entries(env):
    (env.proj / ".mcp.json").write_text('{"mcpServers": {"verinoda": {"command": "other"}}}', encoding="utf-8")
    cfg = env.home / ".codex" / "config.toml"
    cfg.parent.mkdir()
    cfg.write_text('[mcp_servers.verinoda]\ncommand = "other"\n', encoding="utf-8")
    before = tree(env.tmp)
    r1 = agents.install("claude", "project", project_dir=env.proj, home=env.home)
    r2 = agents.install("codex", "user", project_dir=env.proj, home=env.home)
    assert r1["result"] == r2["result"] == "refused"
    assert tree(env.tmp) == before


@pytest.mark.parametrize("content", ["{not json", "[1, 2]"])
def test_refuses_unparseable_mcp_json(env, content):
    (env.proj / ".mcp.json").write_text(content, encoding="utf-8")
    before = tree(env.tmp)
    r = agents.install("claude", "project", project_dir=env.proj, home=env.home)
    assert r["result"] == "refused" and tree(env.tmp) == before


def test_refuses_invalid_or_inline_toml(env):
    cfg = env.home / ".codex" / "config.toml"
    cfg.parent.mkdir()
    for content in ("model = \n", 'mcp_servers = { other = { command = "x" } }\n'):
        cfg.write_text(content, encoding="utf-8")
        before = tree(env.tmp)
        r = agents.install("codex", "user", project_dir=env.proj, home=env.home)
        assert r["result"] == "refused", (content, r)
        assert tree(env.tmp) == before


def test_corrupt_manifest_blocks_changes(env):
    (env.proj / ".verinoda").mkdir()
    (env.proj / ".verinoda" / "install-manifest.json").write_text("garbage", encoding="utf-8")
    before = tree(env.tmp)
    r = agents.install("claude", "project", project_dir=env.proj, home=env.home)
    assert not r["ok"] and tree(env.tmp) == before


# -- user modifications are respected ----------------------------------------------------------------------

def test_uninstall_leaves_user_modified_files(env):
    agents.install("claude", "project", project_dir=env.proj, home=env.home)
    skill = env.proj / ".claude" / "skills" / "verinoda" / "SKILL.md"
    skill.write_bytes(skill.read_bytes() + b"\nmy local note\n")

    r = agents.install("claude", "project", project_dir=env.proj, home=env.home)
    assert r["result"] == "refused" and "edited after Verinoda installed it" in r["errors"][0]

    u = agents.uninstall("claude", "project", project_dir=env.proj, home=env.home)
    assert u["ok"] and u["result"] == "partially_uninstalled", u
    assert skill.read_bytes().endswith(b"my local note\n")
    assert any("modified" in w for w in u["warnings"])
    assert not (env.proj / ".mcp.json").exists()  # unchanged entry in a file we created: removed
    kept = manifest(env.proj)["installs"]["claude"]["items"]
    assert [i["kind"] for i in kept] == ["file"]  # still tracked, so a later install will not clobber it


def test_uninstall_leaves_modified_toml_block_and_json_entry(env):
    agents.install("codex", "user", project_dir=env.proj, home=env.home)
    cfg = env.home / ".codex" / "config.toml"
    cfg.write_text(cfg.read_text(encoding="utf-8").replace("startup_timeout_sec = 30", "startup_timeout_sec = 99"),
                   encoding="utf-8")
    u = agents.uninstall("codex", "user", project_dir=env.proj, home=env.home)
    assert u["ok"] and u["result"] == "partially_uninstalled"
    assert "startup_timeout_sec = 99" in cfg.read_text(encoding="utf-8")
    assert not (env.home / ".agents").exists()

    agents.install("claude", "project", project_dir=env.proj, home=env.home)
    p = env.proj / ".mcp.json"
    d = json.loads(p.read_text(encoding="utf-8"))
    d["mcpServers"]["verinoda"]["env"] = {"X": "1"}
    p.write_text(json.dumps(d), encoding="utf-8")
    u = agents.uninstall("claude", "project", project_dir=env.proj, home=env.home)
    assert u["result"] == "partially_uninstalled"
    assert json.loads(p.read_text(encoding="utf-8"))["mcpServers"]["verinoda"]["env"] == {"X": "1"}


def test_uninstall_without_manifest_touches_nothing(env):
    t = ins._target("claude", "project", env.proj.resolve(), env.home.resolve(), True)
    t.skill.parent.mkdir(parents=True)
    t.skill.write_bytes(agents.render_skill("claude"))
    before = tree(env.tmp)
    u = agents.uninstall("claude", "project", project_dir=env.proj, home=env.home)
    assert u["ok"] and u["result"] == "nothing_to_uninstall" and u["warnings"]
    assert tree(env.tmp) == before


def test_marked_skill_without_manifest_is_adopted(env):
    """A managed SKILL.md committed by a teammate (marker, no local manifest) may be refreshed."""
    t = ins._target("codex", "project", env.proj.resolve(), env.home.resolve(), True)
    t.skill.parent.mkdir(parents=True)
    t.skill.write_bytes(agents.render_skill("codex").replace(b"# Verinoda", b"# Verinoda (old)"))
    r = agents.install("codex", "project", project_dir=env.proj, home=env.home, with_mcp=False)
    assert r["ok"] and r["result"] == "updated"
    assert t.skill.read_bytes() == agents.render_skill("codex")


# -- dry run / status ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("agent,scope", ALL)
def test_dry_run_writes_nothing(env, monkeypatch, agent, scope):
    fake = FakeClaude(env.home)
    monkeypatch.setattr(ins, "_run", fake)
    env.which["claude"] = str(env.bindir / "claude.exe")
    before = tree(env.tmp)
    r = agents.install(agent, scope, project_dir=env.proj, home=env.home, dry_run=True)
    assert r["ok"] and r["dry_run"] and r["result"] == "installed"
    assert {a["op"] for a in r["actions"]} <= {"create", "run"} and r["actions"]
    assert tree(env.tmp) == before and fake.calls == []

    assert agents.install(agent, scope, project_dir=env.proj, home=env.home)["ok"]
    n_calls = len(fake.calls)
    before = tree(env.tmp)
    u = agents.uninstall(agent, scope, project_dir=env.proj, home=env.home, dry_run=True)
    assert u["ok"] and u["dry_run"] and any(a["op"] in ("remove", "run") for a in u["actions"])
    assert tree(env.tmp) == before and len(fake.calls) == n_calls


def test_status_reports_each_agent_and_scope(env):
    st = agents.status(env.proj, home=env.home)
    assert set(st) == {"claude:project", "claude:user", "codex:project", "codex:user"}
    assert not any(v["installed"] or v["mcp_registered"] for v in st.values())
    for v in st.values():
        assert {"installed", "skill", "mcp"} <= set(v)

    agents.install("claude", "project", project_dir=env.proj, home=env.home)
    agents.install("codex", "user", project_dir=env.proj, home=env.home)
    st = agents.status(env.proj, home=env.home)
    cp, cu, xp, xu = (st[k] for k in ("claude:project", "claude:user", "codex:project", "codex:user"))
    assert cp["installed"] and cp["managed"] and cp["modified"] is False and cp["mcp_registered"]
    assert cp["skill"].endswith(os.path.join(".claude", "skills", "verinoda", "SKILL.md"))
    assert "--repo-of .mcp.json" in cp["mcp"] and cp["mcp"].startswith(".mcp.json")
    assert cp["mcp_server"]["build"] == "this" and cp["mcp_server"]["problems"] == []
    assert xu["installed"] and xu["mcp_registered"] and "Verinoda block" in xu["mcp"]
    assert not cu["installed"] and not cu["mcp_registered"] and cu["skill"] is None
    assert not xp["installed"] and not xp["mcp_registered"]

    skill = Path(cp["skill"])
    skill.write_bytes(skill.read_bytes() + b"x")
    assert agents.status(env.proj, home=env.home)["claude:project"]["modified"] is True


def test_render_is_compact(env, capsys):
    r = agents.install("codex", "project", project_dir=env.proj, home=env.home, dry_run=True)
    agents.render(r)
    out = capsys.readouterr().out
    assert "would create" in out and "$verinoda" in out
    assert str(env.proj.resolve()) + os.sep not in out.split("\n", 1)[1]  # paths shown relative to root
    agents.render(agents.status(env.proj, home=env.home))
    assert "codex:user" in capsys.readouterr().out


def test_cli_install_and_uninstall_project_scope(env, capsys):
    from verinoda.cli import main

    assert main(["install", "--agent", "codex", "--scope", "project", "--project-dir", str(env.proj),
                 "--json"]) == 0
    res = json.loads(capsys.readouterr().out)
    assert res["ok"] and res["result"] == "installed"
    assert main(["uninstall", "--agent", "codex", "--scope", "project", "--project-dir", str(env.proj)]) == 0
    assert "uninstalled" in capsys.readouterr().out
    assert tree(env.proj) == {}


def test_server_command_uses_real_installation(tmp_path):
    """No stubs: the command must point at an existing executable."""
    cmd = agents.server_command("project", tmp_path)
    assert Path(cmd[0]).is_file()
    assert cmd[-4:] == ["mcp", "serve", "--repo", str(tmp_path)]
    if len(cmd) > 5:  # the running interpreter: `<python> [-P] -m verinoda`
        assert cmd[0] == sys.executable and cmd[-6:-4] == ["-m", "verinoda"]
    launcher = ins.resolve_launcher()
    want = ["--repo-of", ".mcp.json"] if launcher["runs_ours"] else ["--repo", str(tmp_path)]
    assert agents.server_command("project", tmp_path, "claude", launcher)[-4:] == ["mcp", "serve", *want]
    user = agents.server_command("user", tmp_path)
    assert user[-2:] == ["mcp", "serve"] and "--repo" not in user
    assert agents.server_command("user", tmp_path, profile="full")[-4:] == ["mcp", "serve", "--profile", "full"]


@pytest.mark.parametrize("layout", ["editable", "hardlinked"])
def test_codex_gets_the_full_mcp_profile_when_its_sandbox_may_not_import_verinoda(env, layout):
    """Where the CLI can fail inside the Codex sandbox, MCP is the way in: it must serve every tool the
    skill makes mandatory (debug ledger, plans, references, decisions), so install registers --profile full.
    Claude, and Codex on a copy-mode install, keep the default (core)."""
    r = agents.install("codex", "user", project_dir=env.proj, home=env.home)
    args = tomllib.loads((env.home / ".codex" / "config.toml").read_text(encoding="utf-8"))
    assert r["ok"] and args["mcp_servers"]["verinoda"]["args"] == ["mcp", "serve"]
    assert not any("--profile" in n for n in r["notes"])
    env.layout[layout] = True
    r = agents.install("codex", "user", project_dir=env.proj, home=env.home)
    args = tomllib.loads((env.home / ".codex" / "config.toml").read_text(encoding="utf-8"))
    assert r["ok"] and r["result"] == "updated", r
    assert args["mcp_servers"]["verinoda"]["args"] == ["mcp", "serve", "--profile", "full"]
    assert any("--profile full" in n and "sandbox" in n for n in r["notes"])
    claude = agents.install("claude", "project", project_dir=env.proj, home=env.home)
    entry = json.loads((env.proj / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["verinoda"]
    assert claude["ok"] and "--profile" not in entry["args"]
    # the skills say what to do when a mandated tool is not served (the core profile)
    for agent in ("claude", "codex"):
        assert '`"mcp": {"profile": "full"}`' in agents.render_skill(agent).decode("utf-8"), agent


# -- end to end on the example project -------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_example_project_roundtrip_leaves_git_clean(env, tmp_path):
    repo = tmp_path / "orders_app"
    shutil.copytree(EXAMPLE, repo, ignore=shutil.ignore_patterns(".verinoda", "__pycache__", ".pytest_cache"))

    def git(*a):
        return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=repo,
                              capture_output=True, text=True, encoding="utf-8", check=True).stdout

    git("init", "-q")
    git("add", "-A")
    git("commit", "-q", "-m", "init")
    for agent in ("claude", "codex"):
        assert agents.install(agent, "project", project_dir=repo, home=env.home)["ok"]
    status = sorted(ln[3:] for ln in git("status", "--porcelain", "--untracked-files=all").splitlines())
    assert status == [".agents/skills/verinoda/SKILL.md", ".claude/skills/verinoda/SKILL.md",
                      ".codex/config.toml", ".mcp.json"]  # .verinoda/ is git-ignored by itself
    for agent in ("claude", "codex"):
        assert agents.uninstall(agent, "project", project_dir=repo, home=env.home)["result"] == "uninstalled"
    assert git("status", "--porcelain", "--untracked-files=all", "--ignored") == ""


def test_status_reports_claude_local_scope_servers_for_the_project_or_a_parent(env):
    cfg = env.home / ".claude.json"
    ws = env.tmp / "workspace"
    proj = ws / "tool"
    proj.mkdir(parents=True)
    entry = {"type": "stdio", "command": env.exe, "args": ["mcp", "serve", "--repo", str(proj)]}
    cfg.write_text(json.dumps({"projects": {
        ws.as_posix(): {"mcpServers": {"verinoda": entry}},          # a session opened one level above
        str(env.tmp / "unrelated"): {"mcpServers": {"verinoda": entry}},
        str(proj): {"mcpServers": {"other": entry}},
    }}), encoding="utf-8")
    st = agents.status(proj, home=env.home)["claude:user"]
    assert [{k: v for k, v in e.items() if k != "check"} for e in st["mcp_local"]] == [
        {"folder": ws.as_posix(), "server": "verinoda.exe mcp serve --repo <project>"}]
    assert st["mcp_local"][0]["check"]["build"] == "this"
    assert f"local scope for {ws.as_posix()}" in st["mcp"] and not st["mcp_registered"]
    assert "mcp_local" not in agents.status(env.proj, home=env.home)["claude:user"]
