"""`verinoda setup` and the one-line installer scripts."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from verinoda import setup as setup_mod

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "orders_app"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args],
                   cwd=cwd, check=True, capture_output=True, stdin=subprocess.DEVNULL)


@pytest.fixture
def project(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git not available")
    repo = tmp_path / "orders_app"
    shutil.copytree(EXAMPLE, repo, ignore=shutil.ignore_patterns(".verinoda", "__pycache__", "*.pyc"))
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def test_setup_next_steps_name_the_command_that_runs_this_build(project, tmp_path, monkeypatch):
    """When the `verinoda` on PATH is another build, setup registers the running interpreter; its Next
    lines must not send the user to the PATH one (an older build on an atlas.db this one just wrote)."""
    from verinoda.agents import installer

    other = r"C:\other\venv\Scripts\python.exe" if os.name == "nt" else "/other/venv/bin/python"
    old = tmp_path / "bin" / "verinoda.exe"
    old.parent.mkdir()
    old.write_bytes(f"#!{other}\n".encode("utf-8"))
    which = {"verinoda": str(old), "claude": "C:/fake/claude.cmd"}
    monkeypatch.setattr(installer, "_which", lambda name: which.get(name))
    monkeypatch.setattr(installer, "_import_location", lambda python: str(installer.PKG_DIR))
    cli = installer._display([sys.executable, *(["-P"] if sys.version_info >= (3, 11) else []), "-m", "verinoda"])

    rep = setup_mod.setup_project(project, agents="claude", home=tmp_path / "home")
    steps = "\n".join(rep["next_steps"])
    assert rep["cli"] == rep["server"]["cli"] == cli
    for cmd in ("query ", "ui`", "analyze ", "update .`", "doctor`"):
        assert f"`{cli} {cmd}" in steps and f"`verinoda {cmd}" not in steps, cmd
    assert "`/verinoda <question>`" in steps  # the agent's slash command keeps its name
    assert not any(w.startswith("the steps below") for w in rep["warnings"])  # the agents line says why
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        setup_mod.render(rep)
    assert f"`{cli} query " in out.getvalue() and "agents start: registered the running interpreter" in out.getvalue()

    rep = setup_mod.setup_project(project, agents="none")  # no agent line: a note says why
    assert f"`{cli} doctor`" in "\n".join(rep["next_steps"])
    assert any(w.startswith(f"the steps below run this build as `{cli}`") and str(old) in w for w in rep["warnings"])

    old.write_bytes(f"#!{sys.executable}\n".encode("utf-8"))  # the PATH one is this build: plain `verinoda`
    rep = setup_mod.setup_project(project, agents="none")
    assert rep["cli"] == "verinoda" and "`verinoda doctor`" in "\n".join(rep["next_steps"])
    assert not any(w.startswith("the steps below") for w in rep["warnings"])


def test_setup_indexes_and_is_safe_to_rerun(project):
    rep = setup_mod.setup_project(project, agents="none")
    assert rep["ok"] and rep["index"]["mode"] == "scan" and rep["index"]["nodes"] > 0
    assert (project / ".verinoda" / "atlas.db").is_file() and rep["agents"] == []
    assert not any("no coding agent found" in w for w in rep["warnings"])  # 'none' was explicit
    again = setup_mod.setup_project(project, agents="none")
    assert again["ok"] and again["index"]["mode"] in ("noop", "incremental", "full")


def test_setup_auto_installs_only_the_agents_found(project, tmp_path, monkeypatch):
    from verinoda.agents import installer

    real = installer._which
    monkeypatch.setattr(installer, "_which", lambda name: "C:/fake/claude.cmd" if name == "claude"
                        else (None if name == "codex" else real(name)))
    # this interpreter imports this build when an agent starts it (how an installed build runs)
    monkeypatch.setattr(installer, "_import_location", lambda python: str(installer.PKG_DIR))
    rep = setup_mod.setup_project(project, agents="auto", home=tmp_path / "home")
    assert [a["agent"] for a in rep["agents"]] == ["claude"] and rep["agents"][0]["ok"]
    assert (project / ".claude" / "skills" / "verinoda" / "SKILL.md").is_file()
    assert not (project / ".agents").exists()
    assert any("/verinoda" in s for s in rep["next_steps"])
    # which build set it up, and which program the agents start (the running build, never a guess)
    from verinoda import buildinfo

    assert rep["build"]["build"] == buildinfo.build_info()["build"] and rep["build"]["python"] == sys.executable
    assert rep["server"]["note"].startswith("registered ") and rep["server"]["how"] in ("path", "interpreter")
    entry = json.loads((project / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["verinoda"]
    assert entry["args"][-4:] == ["mcp", "serve", "--repo-of", ".mcp.json"] and str(project) not in json.dumps(entry)
    assert rep["agents"][0]["server"] == [entry["command"], *entry["args"]]
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        setup_mod.render(rep)
    assert f"build {rep['build']['build']}" in out.getvalue() and "agents start: registered" in out.getvalue()
    # re-running changes nothing
    rep2 = setup_mod.setup_project(project, agents="auto", home=tmp_path / "home")
    assert rep2["agents"][0]["result"] == "unchanged"


def test_setup_warns_when_no_agent_is_found(project, monkeypatch):
    from verinoda.agents import installer

    monkeypatch.setattr(installer, "_which", lambda name: None)
    rep = setup_mod.setup_project(project, agents="auto")
    assert rep["ok"] and rep["agents"] == []
    assert any("no coding agent found" in w for w in rep["warnings"])


def test_setup_refuses_home_and_unknown_agents(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    with pytest.raises(setup_mod.SetupRefused):
        setup_mod.setup_project(home, agents="none")
    assert not (home / ".verinoda").exists()
    with pytest.raises(setup_mod.SetupRefused):
        setup_mod.setup_project(tmp_path, agents="cursor")


def test_setup_cli_json(project):
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    r = subprocess.run([sys.executable, "-m", "verinoda", "setup", str(project), "--agents", "none", "--json"],
                       capture_output=True, text=True, env=env, timeout=300)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["ok"] and out["index"]["nodes"] > 0 and out["next_steps"]


def test_reference_trees_are_recorded_merged_and_validated(tmp_path):
    from verinoda import workflow
    from verinoda.paths import DEFAULT_CONFIG, load_config

    repo = tmp_path / "mod"
    (repo / "original" / "src").mkdir(parents=True)
    (repo / "src").mkdir()
    workflow.init(repo)
    assert setup_mod.add_reference(repo, "original=Plugin,paper") == {
        "path": "original/", "aliases": ["paper", "plugin"], "added": True}
    # the same folder again (absolute this time) merges aliases instead of adding a second entry
    again = setup_mod.add_reference(repo, f"{repo / 'original'}=orijinal")
    assert again == {"path": "original/", "aliases": ["orijinal", "paper", "plugin"], "added": False}
    cfg = load_config(repo)
    assert cfg["index"]["reference"] == [{"path": "original/", "aliases": ["orijinal", "paper", "plugin"]}]
    assert cfg["budget"] == DEFAULT_CONFIG["budget"]  # the rest of the config is untouched
    raw = (repo / ".verinoda" / "config.json").read_bytes()
    assert b"\r\n" not in raw and raw.endswith(b"\n")
    for bad in ["", "=alias", "missing", ".", str(tmp_path), "src/../.."]:
        with pytest.raises(setup_mod.SetupRefused):
            setup_mod.add_reference(repo, bad)
    assert load_config(repo)["index"]["reference"] == cfg["index"]["reference"]


def test_setup_records_reference_trees(project):
    (project / "vendored").mkdir()
    (project / "vendored" / "old.py").write_text("def create_order():\n    return 1\n", encoding="utf-8")
    rep = setup_mod.setup_project(project, agents="none", reference=["vendored=legacy"])
    assert rep["ok"] and rep["reference"] == [{"path": "vendored/", "aliases": ["legacy"], "added": True}]
    assert "reference" in rep["steps"]


# -- installer scripts -------------------------------------------------------------------------------

def test_install_script_parses_and_uses_the_safe_options():
    text = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert "--link-mode copy" in text and "--reinstall-package verinoda" in text
    assert "/archive/" in text and "git+" not in text  # no git needed
    for opt in ("VERINODA_REF", "VERINODA_EXTRAS", "VERINODA_SPEC", "VERINODA_NO_MODIFY_PATH"):
        assert opt in text
    assert b"\r" not in (ROOT / "install.sh").read_bytes()
    if shutil.which("sh"):
        assert subprocess.run(["sh", "-n", str(ROOT / "install.sh")], capture_output=True).returncode == 0


def test_docs_do_not_recommend_a_powershell_download_cradle():
    """Microsoft Defender blocked `powershell ... -c "irm <url> | iex"` for our script
    (Trojan:Win32/Commando.A!ml, 2026-09-23); the Windows instructions use plain uv commands."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    overview = (ROOT / "docs" / "GENEL-BAKIS.md").read_text(encoding="utf-8")
    assert not (ROOT / "install.ps1").exists()
    for text in (readme, overview):
        code = "\n".join(text.split("```")[1::2])  # fenced blocks only: prose may name the pattern
        assert "| iex" not in code and "iex (" not in code and "DownloadString" not in code
        assert ('uv tool install --force --reinstall-package verinoda --link-mode copy '
                '"verinoda[precise] @ https://github.com/ozcinax-star/verinoda/archive/main.zip"') in code


@pytest.mark.slow
def test_install_sh_installs_a_working_cli_from_a_local_source(tmp_path):
    """Real run of install.sh (uv required) with a local source and throw-away tool dirs."""
    if not shutil.which("sh") or not shutil.which("uv"):
        pytest.skip("sh and uv are required")
    env = {**os.environ, "UV_TOOL_DIR": str(tmp_path / "tools"), "UV_TOOL_BIN_DIR": str(tmp_path / "bin"),
           "VERINODA_NO_MODIFY_PATH": "1", "VERINODA_SPEC": f"verinoda @ {ROOT.as_uri()}"}
    r = subprocess.run(["sh", str(ROOT / "install.sh")], capture_output=True, text=True, env=env, timeout=900)
    assert r.returncode == 0, r.stdout + r.stderr
    exe = next((tmp_path / "bin").glob("verinoda*"))
    assert subprocess.run([str(exe), "setup", "--help"], capture_output=True).returncode == 0


def test_folders_that_copy_other_code_are_suggested_as_reference_trees(tmp_path):
    names = [f"mod{i}.py" for i in range(8)]
    files = [f"app/src/{n}" for n in names] + [f"legacy/plugin/src/{n}" for n in names[:6]] + \
        ["legacy/plugin/src/only_here.py", "app/tests/test_mod0.py", "docs/readme.md"]
    sugg = setup_mod.reference_suggestions(tmp_path, files)
    assert sugg == [{"path": "legacy/plugin/src/", "shared": 6, "files": 7, "copy_of": "app/"}]
    assert setup_mod.reference_suggestions(tmp_path, files, ["legacy/"]) == []   # already configured
    assert setup_mod.reference_suggestions(tmp_path, [f"app/{n}" for n in names]) == []


def _tree(root, files: dict) -> list[str]:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return sorted(files)


def test_a_copy_of_root_code_is_suggested_but_sibling_apps_and_api_impl_pairs_are_not(tmp_path):
    body = "".join(f"    def m{i}(self):\n        return {i}\n" for i in range(6))
    copy = {f"src/app/mod{i}.py": f"class Mod{i}:\n{body}" for i in range(8)}
    copy |= {f"legacy/src/app/mod{i}.py": f"class Mod{i}:\n{body}    # old\n" for i in range(6)}
    sugg = setup_mod.reference_suggestions(tmp_path / "a", _tree(tmp_path / "a", copy))
    assert [s["path"] for s in sugg] == ["legacy/src/app/"] and sugg[0]["copy_of"] == "./"
    django = {f"{app}/{m}.py": f"# {app} {m}\nfrom django.db import models\nclass {app.title()}{m.title()}: pass\n"
              for app in ("blog", "shop") for m in ("models", "views", "admin", "apps", "urls", "forms", "signals")}
    assert setup_mod.reference_suggestions(tmp_path / "d", _tree(tmp_path / "d", django)) == []
    api_impl = {f"{mod}/src/main/java/com/x/Svc{i}.java":
                (f"package com.x;\npublic interface Svc{i} {{\n  int run{i}(int a);\n}}\n" if mod == "api" else
                 f"package com.x;\nimport java.util.List;\npublic class Svc{i} implements Api{i} {{\n"
                 f"  public int run{i}(int a) {{\n    return a * {i};\n  }}\n}}\n")
                for mod in ("api", "impl") for i in range(7)}
    api_impl |= {"impl/src/main/java/com/x/Extra.java": "class Extra {}\n"}
    assert setup_mod.reference_suggestions(tmp_path / "j", _tree(tmp_path / "j", api_impl)) == []


def test_a_bad_reference_refuses_before_anything_is_written(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    with pytest.raises(setup_mod.SetupRefused):
        setup_mod.setup_project(proj, agents=[], reference=["no_such_dir"])
    assert not (proj / ".verinoda").exists()
