"""D68: Verinoda's own agent-integration files are never part of the project's corpus, and `setup` never
finds its own writes as changed files (a whole graph rebuild when another interpreter ran it)."""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from verinoda import buildlock, freshness, index, selffiles, workflow
from verinoda import setup as setup_mod
from verinoda.agents import installer as _installer
from verinoda.paths import graph_path, search_db_path
from verinoda.snapshot import list_files
from verinoda.store import open_store

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "orders_app"
SKILL = ".claude/skills/verinoda/SKILL.md"
CODEX_SKILL = ".agents/skills/verinoda/SKILL.md"
_REAL_LAUNCHER = _installer.resolve_launcher


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


def _launcher(monkeypatch, python: str | None = None):
    """Agents found on PATH, this build importable; ``python``: the interpreter setup registers (another
    install of the same code), else the running one."""
    from verinoda.agents import installer

    monkeypatch.setattr(installer, "_which", lambda name: "C:/fake/claude.cmd" if name == "claude" else None)
    monkeypatch.setattr(installer, "_import_location", lambda py: str(installer.PKG_DIR))
    monkeypatch.setattr(installer, "resolve_launcher",
                        (lambda: {**_REAL_LAUNCHER(), "argv": [python, "-m", "verinoda"],
                                  "cli": f"{python} -m verinoda"}) if python else _REAL_LAUNCHER)


def _search_files(repo: Path) -> set[str]:
    conn = sqlite3.connect(str(search_db_path(repo)))
    try:
        return {r[0] for r in conn.execute("SELECT file FROM files")}
    finally:
        conn.close()


def _write(repo: Path, rel: str, text: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


# -- the rule ----------------------------------------------------------------------------------------------

def test_the_installer_writes_its_skills_into_the_folders_the_rule_leaves_out(tmp_path):
    from verinoda.agents import installer

    for agent in installer.AGENTS:
        t = installer._target(agent, "project", tmp_path, tmp_path / "home", True)
        rel = t.skill.relative_to(tmp_path).as_posix()
        assert selffiles.is_own(rel) and rel.startswith(selffiles.SKILL_PREFIXES), rel
        assert t.manifest == tmp_path / selffiles.MANIFEST_DIR / selffiles.MANIFEST_NAME
    assert installer.MARKER.encode("utf-8").startswith(selffiles.MARKER_PREFIX)


def test_own_files_are_never_listed_tracked_or_not(project):
    _write(project, SKILL, "<!-- verinoda-managed v1 -->\nskill\n")
    _write(project, CODEX_SKILL, "<!-- verinoda-managed v1 -->\nskill\n")
    _write(project, ".claude/skills/verinoda/notes.md", "anything in the skill folder\n")
    _write(project, ".claude/skills/other/SKILL.md", "another skill: the project's\n")
    _write(project, "tools/verinoda_rules.md", "<!-- verinoda-managed v1 -->\nwritten whole by Verinoda\n")
    _write(project, "tools/taken_over.md", "listed, but the user replaced it (no marker)\n")
    _write(project, ".verinoda/install-manifest.json", json.dumps({"version": 1, "installs": {"claude": {"items": [
        {"kind": "file", "path": SKILL}, {"kind": "file", "path": "tools/verinoda_rules.md"},
        {"kind": "file", "path": "tools/taken_over.md"},
        {"kind": "json_key", "path": ".mcp.json", "key": ["mcpServers", "verinoda"]}]}}}))
    _write(project, ".mcp.json", json.dumps({"mcpServers": {"verinoda": {"command": "verinoda",
                                                                         "args": ["mcp", "serve"]}}}))
    _git(project, "add", "-A", "--", ".claude", ".agents", "tools")  # a team may commit its skill
    files = set(list_files(project))
    assert not {SKILL, CODEX_SKILL, ".claude/skills/verinoda/notes.md", "tools/verinoda_rules.md"} & files
    # the user's own files stay: another skill, a listed file without the marker, a shared config
    assert {".claude/skills/other/SKILL.md", "tools/taken_over.md", ".mcp.json", "orders/service.py"} <= files
    assert selffiles.ignore_patterns(project) == ["/.claude/skills/verinoda/", "/.agents/skills/verinoda/",
                                                  "/tools/verinoda_rules.md"]


def test_a_broken_manifest_never_breaks_the_file_list(project):
    _write(project, ".verinoda/install-manifest.json", "{not json")
    _write(project, SKILL, "<!-- verinoda-managed v1 -->\n")
    files = list_files(project)
    assert "orders/service.py" in files and SKILL not in files
    _write(project, ".verinoda/install-manifest.json", json.dumps({"installs": {"claude": {"items": "x"}}}))
    assert "orders/service.py" in list_files(project)


def test_own_mcp_entry_and_config_digest():
    ours = {"command": "C:/a/python.exe", "args": ["-P", "-m", "verinoda", "mcp", "serve", "--repo-of", ".mcp.json"]}
    assert selffiles.is_own_mcp_entry("verinoda", ours)
    assert selffiles.is_own_mcp_entry("verinoda", {"command": "verinoda", "args": ["mcp", "serve"]})
    assert not selffiles.is_own_mcp_entry("verinoda", {"command": "npx", "args": ["some-server"]})
    assert not selffiles.is_own_mcp_entry("other", ours)


def test_config_digest_leaves_out_only_verinodas_entry(tmp_path):
    p = tmp_path / ".mcp.json"

    def digest(servers: dict, indent=2) -> str:
        p.write_text(json.dumps({"mcpServers": servers}, indent=indent), encoding="utf-8")
        return selffiles.config_digest(p)

    other = {"command": "npx", "args": ["-y", "some-server"]}
    a = digest({"docs": other, "verinoda": {"command": "C:/one/python.exe", "args": ["-m", "verinoda", "mcp", "serve"]}})
    b = digest({"docs": other, "verinoda": {"command": "C:/two/python.exe", "args": ["-m", "verinoda", "mcp", "serve"]}},
               indent=4)
    assert a == b == digest({"docs": other})  # Verinoda's entry, whitespace: not what the graph reads
    assert digest({"docs": {**other, "args": ["-y", "another-server"]}}) != a  # the user's servers are
    p.write_text("{broken", encoding="utf-8")
    assert selffiles.config_digest(p).startswith("b:")
    assert selffiles.config_digest(tmp_path / "package.json") is None


def test_the_graph_never_has_verinodas_own_mcp_entry(tmp_path):
    from verinoda.project_index.mcp_ingest import extract_mcp_config

    p = tmp_path / ".mcp.json"
    p.write_text(json.dumps({"mcpServers": {
        "docs": {"command": "npx", "args": ["-y", "@scope/docs-server"]},
        "verinoda": {"command": "C:/Users/someone/venv/Scripts/python.exe",
                     "args": ["-m", "verinoda", "mcp", "serve", "--repo-of", ".mcp.json"]}}}), encoding="utf-8")
    labels = {n["label"] for n in extract_mcp_config(p)["nodes"]}
    assert "docs" in labels and "npx" in labels
    assert "verinoda" not in labels and not any("python" in lab for lab in labels)


# -- setup and update ------------------------------------------------------------------------------------

def test_setup_from_two_interpreters_in_turn_never_rebuilds_the_graph(project, tmp_path, monkeypatch):
    other = str(tmp_path / "other-venv" / "Scripts" / "python.exe")
    _launcher(monkeypatch)
    first = setup_mod.setup_project(project, agents="claude", home=tmp_path / "home")
    assert first["ok"] and first["index"]["mode"] == "scan"
    assert (project / SKILL).is_file() and (project / ".mcp.json").is_file()
    at = json.loads((project / ".verinoda" / "index" / buildlock.STATS_NAME).read_text(encoding="utf-8"))["at"]

    again = setup_mod.setup_project(project, agents="claude", home=tmp_path / "home")
    assert again["index"]["mode"] == "noop" and again["agents"][0]["result"] == "unchanged"

    for python in (other, None, other):  # another install of the same code, then this one, then the other
        _launcher(monkeypatch, python)
        rep = setup_mod.setup_project(project, agents="claude", home=tmp_path / "home")
        assert rep["agents"][0]["result"] == "updated", rep["agents"]
        # the skill changed (not listed at all); .mcp.json changed only in Verinoda's entry: search only
        assert rep["index"]["graph"] == "none", rep["index"]
        assert rep["index"]["mode"] in ("noop", "incremental")
    stats = json.loads((project / ".verinoda" / "index" / buildlock.STATS_NAME).read_text(encoding="utf-8"))
    assert stats["at"] == at  # no graph build since the first setup
    assert SKILL not in index.graph_source_files(project) and SKILL not in _search_files(project)
    assert freshness.check(project)["count"] == 0


def test_a_change_to_the_users_own_server_still_rebuilds_the_graph(project, tmp_path, monkeypatch):
    _launcher(monkeypatch)
    setup_mod.setup_project(project, agents="claude", home=tmp_path / "home")
    p = project / ".mcp.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    data["mcpServers"]["docs"] = {"command": "npx", "args": ["-y", "@scope/docs-server"]}
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    st = open_store(project)
    try:
        res = workflow.update(st, project)
    finally:
        st.close()
    assert res["index_mode"] == "full" and ".mcp.json" in res["changed"]["modified"]
    g = json.loads(graph_path(project).read_text(encoding="utf-8"))
    labels = {n.get("label") for n in g["nodes"] if n.get("source_file") == ".mcp.json"}
    assert "docs" in labels and "verinoda" not in labels


def test_an_existing_index_drops_verinodas_own_files_on_the_next_update(project, tmp_path, monkeypatch):
    """An index built before D68 has nodes, search units and snapshot rows for the skill: the next update
    drops them all, with no file kept by the build's fail-closed rule and nothing reported as changed after."""
    _launcher(monkeypatch)
    from verinoda.agents import installer

    installer.install("claude", "project", project_dir=project, home=tmp_path / "home")
    with monkeypatch.context() as old:  # the rule as it was: nothing is Verinoda's own
        old.setattr(selffiles, "own_filter", lambda repo: (lambda rel: False))
        old.setattr(selffiles, "ignore_patterns", lambda repo: [])
        st = open_store(project)
        try:
            workflow.scan(st, project)
            assert SKILL in st.snapshot_files(st.latest_snapshot()["id"])
        finally:
            st.close()
        assert SKILL in index.graph_source_files(project) and SKILL in _search_files(project)

    st = open_store(project)
    try:
        res = workflow.update(st, project)
        assert SKILL in res["changed"]["removed"] and res["index_mode"] == "full"
        assert SKILL not in st.snapshot_files(st.latest_snapshot()["id"])
    finally:
        st.close()
    # evicted by the build (a file that leaves the corpus but exists would be kept: "fail-closed: kept")
    assert SKILL not in index.graph_source_files(project)
    assert not any(n.get("source_file") == SKILL
                   for n in json.loads(graph_path(project).read_text(encoding="utf-8"))["nodes"])
    assert SKILL not in _search_files(project)
    assert freshness.check(project)["count"] == 0
    st = open_store(project)
    try:
        assert workflow.update(st, project)["mode"] == "noop"
    finally:
        st.close()


def test_a_copied_index_drops_them_too(project, tmp_path, monkeypatch):
    """An index copied from another folder records that folder as the graph's root: the build's reconcile
    then keeps the nodes of files it no longer reads (they look outside the scanned folder). The graph's
    post-processing drops the nodes of Verinoda's own files whatever the build kept."""
    _launcher(monkeypatch)
    from verinoda.agents import installer

    installer.install("claude", "project", project_dir=project, home=tmp_path / "home")
    with monkeypatch.context() as old:
        old.setattr(selffiles, "own_filter", lambda repo: (lambda rel: False))
        old.setattr(selffiles, "ignore_patterns", lambda repo: [])
        st = open_store(project)
        try:
            workflow.scan(st, project)
        finally:
            st.close()
    assert SKILL in index.graph_source_files(project)
    # the folder the index was copied from, still holding the same files
    shutil.copytree(project, tmp_path / "elsewhere", ignore=shutil.ignore_patterns(".verinoda", ".git"))
    (project / ".verinoda" / "index" / ".graphify_root").write_text(str(tmp_path / "elsewhere"), encoding="utf-8")
    st = open_store(project)
    try:
        res = workflow.update(st, project)
    finally:
        st.close()
    assert res["index_mode"] == "full" and SKILL not in index.graph_source_files(project)
    assert res["own_files_dropped"] == [SKILL]


def test_setup_cli_twice_is_a_noop(project, tmp_path):
    """The real command, one interpreter: the second run finds nothing to do."""
    import os

    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    argv = [sys.executable, "-m", "verinoda", "setup", str(project), "--agents", "claude", "--json"]
    first = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=300)
    assert first.returncode == 0, first.stderr
    second = json.loads(subprocess.run(argv, capture_output=True, text=True, env=env, timeout=300).stdout)
    assert second["index"]["mode"] == "noop" and second["index"]["graph"] == "none"
