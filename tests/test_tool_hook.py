"""Graph context after the agent's own searches and reads (tool hooks), and their install into agent hook files."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import io  # noqa: E402
import json  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

import verinoda  # noqa: E402
from verinoda import agent_hooks, cli, index, tool_hook  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "orders_app"


@pytest.fixture(scope="module")
def repo(tmp_path_factory) -> Path:
    from verinoda import workflow
    from verinoda.store import open_store

    r = tmp_path_factory.mktemp("hook ş") / "orders_app"   # a non-ASCII path, as on the user's machine
    shutil.copytree(EXAMPLE, r, ignore=shutil.ignore_patterns(".verinoda", "__pycache__", "*.pyc", "*.db"))
    workflow.init(r)
    st = open_store(r)
    try:
        workflow.scan(st, r)
    finally:
        st.close()
    return r


# -- what a tool call searched for ----------------------------------------------------------------

@pytest.mark.parametrize("command, expected", [
    ('rg -n "def save_order" src | head', ["def save_order"]),
    ("git grep -e foo_bar -- x", ["foo_bar"]),
    ("grep -rn --include=*.py apply_discount .", ["apply_discount"]),
    ("grep -r -A 3 place_order orders", ["place_order"]),
    ("cd orders && grep -E 'a|b' x.py; rg --type py -g '*.py' Thing", ["a|b", "Thing"]),
    ("Select-String -Path *.py -Pattern OrderRepo", ["OrderRepo"]),
    ('findstr /s /c:"total price" *.py', ["total price"]),
    ("LC_ALL=C /usr/bin/grep -F needle hay.txt", ["needle"]),
    ("rg.exe --regexp=alpha --regexp beta", ["alpha", "beta"]),
    ("ls -la; cat README.md", []),
    ("grep 'unclosed", ["'unclosed"]),
])
def test_shell_commands_give_their_search_patterns(command, expected):
    assert tool_hook.shell_patterns(command) == expected


def test_tool_calls_are_told_apart():
    k = tool_hook.event_kind
    assert k({"tool_name": "Grep", "tool_input": {"pattern": "x"}}) == ("search", ["x"])
    assert k({"tool_name": "Bash", "tool_input": {"command": "rg foo"}}) == ("search", ["foo"])
    assert k({"tool_name": "Shell", "tool_input": json.dumps({"command": "grep bar"})}) == ("search", ["bar"])
    assert k({"tool_name": "Read", "tool_input": {"file_path": "a.py"}}) == ("file", ["a.py"])
    assert k({"tool_name": "Read", "tool_input": {"target_file": "b.py"}}) == ("file", ["b.py"])
    assert k({"tool_name": "Bash", "tool_input": {"command": "pytest -q"}}) is None
    assert k({"tool_name": "WebFetch", "tool_input": {"url": "x"}}) is None
    assert k({"tool_name": "Grep", "tool_input": None}) is None


# -- the answer -----------------------------------------------------------------------------------

def test_a_search_names_the_symbols_definition_and_callers_in_each_agents_shape(repo):
    ev = {"tool_name": "Bash", "tool_input": {"command": "rg -n place_order"}, "cwd": str(repo / "orders")}
    out = tool_hook.answer(ev, "codex")
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert "`place_order` is defined at orders/service.py:" in ctx and "called by create_order_handler" in ctx
    assert len(ctx) <= tool_hook.CONTEXT_CHARS
    cur = tool_hook.answer({"tool_name": "Grep", "tool_input": {"pattern": "place_order"},
                            "workspace_roots": [str(repo)]}, "cursor")
    assert cur == {"additional_context": ctx}
    assert tool_hook.answer({"tool_name": "Grep", "tool_input": {"pattern": "no_such_name_here"},
                             "cwd": str(repo)}, "claude") == {}


def test_the_lines_are_remembered_until_the_index_changes(repo, monkeypatch):
    ev = {"tool_name": "Grep", "tool_input": {"pattern": "compute_total"}, "cwd": str(repo)}
    first = tool_hook.answer(ev, "claude")
    assert "`compute_total` is defined at orders/pricing.py:" in first["hookSpecificOutput"]["additionalContext"]

    def no_graph(*_a, **_k):
        raise AssertionError("the graph was loaded for a remembered word")

    monkeypatch.setattr(index, "load", no_graph)
    assert tool_hook.answer(ev, "claude") == first
    from verinoda.paths import graph_path

    gp = graph_path(repo)
    st = gp.stat()
    os.utime(gp, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000))   # a new build: the memo is cleared
    assert tool_hook.answer(ev, "claude") == {}   # the graph is needed again (index.load raises: nothing said)
    monkeypatch.undo()
    assert tool_hook.answer(ev, "claude") == first


def test_a_read_gets_what_the_project_says_about_the_file(repo, monkeypatch):
    from verinoda import scoped

    monkeypatch.setattr(scoped, "for_file", lambda r, p: {"path": p})
    monkeypatch.setattr(scoped, "text", lambda res, *a, **k: f"notes on {res['path']}")
    out = tool_hook.answer({"tool_name": "Read", "tool_input": {"file_path": "orders/api.py"}, "cwd": str(repo)},
                           "claude")
    assert out["hookSpecificOutput"]["additionalContext"] == "notes on orders/api.py"


def test_no_project_or_a_broken_event_says_nothing(tmp_path):
    ev = {"tool_name": "Grep", "tool_input": {"pattern": "place_order"}, "cwd": str(tmp_path)}
    assert tool_hook.answer(ev, "claude") == {}
    assert tool_hook.run("claude", io.BytesIO(b"not json")) == "{}"
    assert tool_hook.run("claude", io.BytesIO(b"[1, 2]")) == "{}"


def test_the_hook_command_reads_utf8_and_answers_in_ascii(repo):
    ev = json.dumps({"tool_name": "Grep", "tool_input": {"pattern": "place_order"}, "cwd": str(repo)},
                    ensure_ascii=False).encode("utf-8")
    env = {**os.environ, "PYTHONPATH": str(Path(verinoda.__file__).resolve().parents[1])}
    p = subprocess.run([sys.executable, "-m", "verinoda", "tool-hook", "--agent", "codex"], input=ev,
                       capture_output=True, env=env, cwd=str(repo), timeout=120)
    assert p.returncode == 0, p.stderr
    out = json.loads(p.stdout.decode("ascii"))
    assert "place_order" in out["hookSpecificOutput"]["additionalContext"]


# -- the install ----------------------------------------------------------------------------------

def _hooks(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_install_writes_each_agents_file_and_keeps_other_hooks(tmp_path):
    proj, home = tmp_path / "p", tmp_path / "home"
    (proj / ".claude").mkdir(parents=True)
    other = {"type": "command", "command": "echo hi"}
    (proj / ".claude" / "settings.json").write_text(json.dumps(
        {"permissions": {"allow": ["Bash(ls)"]}, "hooks": {"PostToolUse": [{"matcher": "Grep", "hooks": [other]}],
                                                            "Stop": [{"hooks": [other]}]}}, indent=4),
        encoding="utf-8")
    res = agent_hooks.run("install", list(agent_hooks.AGENTS), "project", proj, home=home)
    assert res["ok"] and [r["result"] for r in res["results"]] == ["updated", "created", "created"]
    claude = _hooks(proj / ".claude" / "settings.json")
    assert claude["permissions"] == {"allow": ["Bash(ls)"]} and claude["hooks"]["Stop"] == [{"hooks": [other]}]
    post = claude["hooks"]["PostToolUse"]
    assert post[0] == {"matcher": "Grep", "hooks": [other]}
    assert [e["matcher"] for e in post[1:]] == ["Grep", "Bash", "Read|Edit|MultiEdit|Write"]
    assert all(h["type"] == "mcp_tool" and h["server"] == "verinoda" for e in post[1:] for h in e["hooks"])
    assert (proj / ".claude" / "settings.json").read_text(encoding="utf-8").startswith('{\n    "permissions"')
    assert any(".mcp.json" in w for w in res["results"][0]["warnings"])
    codex = _hooks(proj / ".codex" / "hooks.json")["hooks"]["PostToolUse"]
    assert codex[0]["matcher"] == "^Bash$" and "tool-hook --agent codex" in codex[0]["hooks"][0]["command"]
    cursor = _hooks(proj / ".cursor" / "hooks.json")
    assert cursor["version"] == 1 and cursor["hooks"]["postToolUse"][0]["matcher"] == "Grep|Shell|Read|Write"
    assert "tool-hook --agent cursor" in cursor["hooks"]["postToolUse"][0]["command"]
    again = agent_hooks.run("install", list(agent_hooks.AGENTS), "project", proj, home=home)
    assert [r["result"] for r in again["results"]] == ["unchanged"] * 3
    st = agent_hooks.run("status", list(agent_hooks.AGENTS), "project", proj, home=home)
    assert [r["result"] for r in st["results"]] == ["installed"] * 3


def test_uninstall_removes_exactly_verinodas_hooks(tmp_path):
    proj, home = tmp_path / "p", tmp_path / "home"
    proj.mkdir()
    agent_hooks.run("install", ["claude", "cursor"], "project", proj, home=home)
    cur = proj / ".cursor" / "hooks.json"
    data = _hooks(cur)
    data["hooks"]["postToolUse"].append({"command": "./mine.sh"})
    cur.write_text(json.dumps(data), encoding="utf-8")
    res = agent_hooks.run("uninstall", ["claude", "cursor"], "project", proj, home=home)
    assert [r["result"] for r in res["results"]] == ["removed file", "removed"]
    assert not (proj / ".claude" / "settings.json").exists()
    assert _hooks(cur) == {"version": 1, "hooks": {"postToolUse": [{"command": "./mine.sh"}]}}
    again = agent_hooks.run("uninstall", ["claude", "cursor"], "project", proj, home=home)
    assert [r["result"] for r in again["results"]] == ["absent", "absent"]


def test_user_scope_uses_the_command_hook_and_the_home_folder(tmp_path, monkeypatch):
    monkeypatch.delenv("CODEX_HOME", raising=False)
    home = tmp_path / "home"
    res = agent_hooks.run("install", ["claude", "codex"], "user", tmp_path, home=home)
    assert [Path(r["path"]) for r in res["results"]] == [home / ".claude" / "settings.json",
                                                         home / ".codex" / "hooks.json"]
    (entry,) = _hooks(home / ".claude" / "settings.json")["hooks"]["PostToolUse"]
    assert entry["matcher"] == "Grep|Bash|Read|Edit|MultiEdit|Write"
    assert entry["hooks"][0]["type"] == "command" and "tool-hook --agent claude" in entry["hooks"][0]["command"]


def test_a_file_that_is_not_a_json_object_is_never_touched(tmp_path):
    (tmp_path / ".codex").mkdir()
    bad = tmp_path / ".codex" / "hooks.json"
    bad.write_text("{ not json", encoding="utf-8")
    res = agent_hooks.run("install", ["codex"], "project", tmp_path)
    assert not res["ok"] and res["results"][0]["result"] == "refused"
    assert bad.read_text(encoding="utf-8") == "{ not json"
    with pytest.raises(ValueError):
        agent_hooks.run("install", ["vim"], "project", tmp_path)


def test_dry_run_and_the_cli(tmp_path, capsys):
    assert cli.main(["agent-hooks", "install", "--agent", "all", "--dry-run", "--repo", str(tmp_path),
                     "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["dry_run"] and [r["result"] for r in out["results"]] == ["created"] * 3
    assert not (tmp_path / ".codex").exists()
    assert cli.main(["agent-hooks", "status", "--agent", "codex", "--repo", str(tmp_path)]) == 0
    assert "absent" in capsys.readouterr().out
    assert cli.main(["agent-hooks", "install", "--agent", "vim", "--repo", str(tmp_path)]) == 2
