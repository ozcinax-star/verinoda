"""`verinoda setup` and the one-line installer scripts."""

from __future__ import annotations

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
    rep = setup_mod.setup_project(project, agents="auto", home=tmp_path / "home")
    assert [a["agent"] for a in rep["agents"]] == ["claude"] and rep["agents"][0]["ok"]
    assert (project / ".claude" / "skills" / "verinoda" / "SKILL.md").is_file()
    assert not (project / ".agents").exists()
    assert any("/verinoda" in s for s in rep["next_steps"])
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
