"""``verinoda live`` (verinoda/live.py, D137): settings, the question heuristic, the auto-context, the name check after
an edit, the commit guard, the index state, and the one hook command both Claude Code and Codex call."""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from verinoda import cli, live

ROOT = Path(__file__).resolve().parents[1]
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


@pytest.fixture()
def proj(tmp_path):
    """A project folder with a ``.verinoda`` marker; no index (the commands that need one are patched)."""
    p = tmp_path / "proj"
    (p / ".verinoda").mkdir(parents=True)
    (p / "orders").mkdir()
    (p / "orders" / "service.py").write_text("def place(o):\n    return o\n", encoding="utf-8")
    return p


def payload(proj, event, **kw):
    return {"hook_event_name": event, "cwd": str(proj), **kw}


# -- settings -----------------------------------------------------------------------------------------------

def test_defaults_are_the_mods_and_a_setting_is_kept(proj):
    assert live.settings(proj) == {"refresh": True, "check": True, "guard": False, "auto": "off", "offer": False}
    live.set_setting(proj, "auto", "nudge")
    live.set_setting(proj, "guard", "on")
    live.set_setting(proj, "check", "off")
    assert live.settings(proj) == {"refresh": True, "check": False, "guard": True, "auto": "nudge", "offer": False}
    assert live.set_setting(proj, "auto", "on")["auto"] == "nudge"          # the mod's old spelling
    for key, value in (("auto", "maybe"), ("guard", "sometimes"), ("colour", "on")):
        with pytest.raises(ValueError):
            live.set_setting(proj, key, value)


def test_a_value_of_the_wrong_type_in_the_file_is_ignored(proj):
    (proj / ".verinoda" / "live").mkdir()
    (proj / ".verinoda" / "live" / "live.json").write_text(
        json.dumps({"auto": "loud", "guard": "yes", "check": False}), encoding="utf-8")
    assert live.settings(proj) == {"refresh": True, "check": False, "guard": False, "auto": "off", "offer": False}


def test_find_project_never_picks_the_home_folder(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".verinoda").mkdir(parents=True)
    work = home / "work" / "sub"
    work.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    assert live.find_project(work) is None
    (home / "work" / ".verinoda").mkdir()
    assert live.find_project(work) == (home / "work").resolve()


# -- the question heuristic ------------------------------------------------------------------------------------

@pytest.mark.parametrize("text, ok", [
    ("How does the checkout reach the database?", True),
    ("explain the retry logic in the client", True),
    ("Kor Ocağı her tickte ne yapıyor", True),
    ("bu fonksiyon çağrılıyor mu", True),
    ("/clear", False), ("!ls -la", False), ("# a note to myself", False),
    ("fix it", False),
    ("rename the variable x to count in cart.py everywhere", False),
])
def test_looks_like_a_code_question(text, ok):
    assert live.looks_like_code_question(text) is ok


def test_a_long_prompt_is_a_nudge_but_not_a_search():
    long = "Why does the build fail? " + "log line\n" * 600
    assert not live.looks_like_code_question(long)
    assert live.looks_like_code_question(long, live.NUDGE_MAX_CHARS)


@pytest.mark.parametrize("text, vague", [
    ("make this page better", True), ("Can you make the login screen nicer?", True), ("bunu daha güzel yap", True),
    ("fix the null pointer in cart.py", False), ("improve the speed of the parser", False), ("/verinoda-improve", False),
])
def test_a_vague_request_is_one_that_says_better_and_not_what(text, vague):
    assert live.is_vague_request(text) is vague


# -- the hook command ------------------------------------------------------------------------------------------

def context(out):
    return out["hookSpecificOutput"]["additionalContext"]


def test_a_question_gets_the_nudge_only_when_the_mode_says_so(proj):
    ask = payload(proj, "UserPromptSubmit", prompt="How does the checkout reach the database?")
    assert live.hook(ask) is None                                          # off by default
    live.set_setting(proj, "auto", "nudge")
    out = live.hook(ask)
    assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "[Verinoda auto-context]" in context(out) and str(proj) in context(out) and "analyze" in context(out)
    assert live.hook(payload(proj, "UserPromptSubmit", prompt="rename x to y")) is None   # not a question
    assert live.hook(payload(proj, "UserPromptSubmit", prompt="<system-reminder>How does it work?</system-reminder>")) is None
    assert live.status(proj)["last_context"]["mode"] == "nudge"


def test_search_mode_attaches_the_query_and_says_nothing_when_it_found_none(proj, monkeypatch):
    live.set_setting(proj, "auto", "search")
    calls = []

    def run(argv, repo, timeout, stdin=None):
        calls.append(argv)
        return 0, "orders/service.py:1 def place\n", ""
    monkeypatch.setattr(live, "_run", run)
    out = live.hook(payload(proj, "UserPromptSubmit", prompt="Where is the order placed in the service?"))
    assert "orders/service.py:1 def place" in context(out) and "leads to verify" in context(out)
    assert "query" in calls[0] and "--max-chars" in calls[0]
    monkeypatch.setattr(live, "_run", lambda *a, **k: (0, "", ""))
    assert live.hook(payload(proj, "UserPromptSubmit", prompt="Where is the order placed in the service?")) is None
    assert live.status(proj)["last_context"]["ok"] is False


def test_the_offer_is_on_request_only(proj):
    vague = payload(proj, "UserPromptSubmit", prompt="make the order page nicer")
    assert live.hook(vague) is None
    live.set_setting(proj, "offer", "on")
    out = live.hook(vague)
    assert "/verinoda-improve" in context(out) and "$verinoda-improve" in context(out)
    assert live.hook(payload(proj, "UserPromptSubmit", prompt="fix the crash in cart.py line 40")) is None


REPORT = {"sites": [
    {"path": "orders/service.py", "at": "orders/service.py:3:5", "expr": "pricing.apply_discountt", "verdict": "absent",
     "message": "not found in module pricing", "nearest": [{"name": "apply_discount"}, {"name": "apply_tax"}]},
    {"path": "orders/service.py", "at": "orders/service.py:9:1", "expr": "x.y", "verdict": "unknown", "message": "open"},
    {"path": "orders/other.py", "at": "orders/other.py:1:1", "expr": "q", "verdict": "absent", "message": "elsewhere"}],
    "summary": {"unknown": 1}}


def test_an_edit_that_uses_a_missing_name_tells_the_model_with_the_nearest_names(proj, monkeypatch):
    monkeypatch.setattr(live, "run_check", lambda repo, rel: REPORT)
    edit = payload(proj, "PostToolUse", tool_name="Edit", tool_input={"file_path": str(proj / "orders" / "service.py")})
    out = live.hook(edit)
    text = context(out)
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert "[Verinoda check]" in text and "a name that does not exist" in text
    assert "apply_discountt" in text and "nearest: apply_discount, apply_tax" in text
    assert "orders/other.py" not in text and "x.y" not in text            # another file; an unknown is not reported
    assert live.status(proj)["last_check"] == {"file": "orders/service.py", "checked": True, "bad": 1, "unknown": 1,
                                               "at": live.status(proj)["last_check"]["at"]}


def test_codex_patches_and_every_edit_tool_are_read(proj, monkeypatch):
    seen = []
    monkeypatch.setattr(live, "run_check", lambda repo, rel: seen.append(rel) or {"sites": []})
    patch = "*** Begin Patch\n*** Update File: orders/service.py\n@@\n-a\n+b\n*** Add File: orders/new.py\n+x\n*** End Patch"
    assert live.hook(payload(proj, "PostToolUse", tool_name="apply_patch", tool_input={"input": patch})) is None
    assert seen == ["orders/service.py", "orders/new.py"]
    seen.clear()
    live.hook(payload(proj, "PostToolUse", tool_name="NotebookEdit", tool_input={"notebook_path": "orders/n.py"}))
    live.hook(payload(proj, "PostToolUse", tool_name="Read", tool_input={"file_path": "orders/service.py"}))
    assert seen == ["orders/n.py"]                                          # Read is not an edit


def test_check_is_skipped_for_other_languages_outside_the_project_and_when_off(proj, monkeypatch):
    monkeypatch.setattr(live, "run_check", lambda *a: pytest.fail("the checker must not run"))
    for f in ("README.md", "app.ts", str(proj.parent / "outside.py")):
        res = live.check_edit(proj, f)
        assert res["checked"] is False and res["note"] is None
    live.set_setting(proj, "check", "off")
    assert live.hook(payload(proj, "PostToolUse", tool_name="Edit", tool_input={"file_path": "orders/service.py"})) is None


def test_a_check_that_gives_no_answer_never_blocks_and_never_says_fine(proj, monkeypatch):
    monkeypatch.setattr(live, "run_check", lambda *a: None)
    res = live.check_edit(proj, "orders/service.py")
    assert res["checked"] is False and "no answer" in res["why"]


def test_run_check_takes_exit_3_and_4_as_answers_and_falls_back_to_the_file(proj, monkeypatch):
    answers = iter([(3, json.dumps({"sites": []}), ""), ])
    monkeypatch.setattr(live, "_run", lambda argv, repo, timeout, stdin=None: next(answers))
    assert live.run_check(proj, "orders/service.py") == {"sites": []}
    seen = []

    def run(argv, repo, timeout, stdin=None):
        seen.append(argv[-4:])
        return (2, "", "no git") if "--diff" in argv else (0, '{"sites": []}', "")
    monkeypatch.setattr(live, "_run", run)
    assert live.run_check(proj, "orders/service.py") == {"sites": []} and len(seen) == 2
    monkeypatch.setattr(live, "_run", lambda *a, **k: (None, "", "TimeoutExpired"))
    assert live.run_check(proj, "orders/service.py") is None


def test_a_hook_outside_a_project_or_on_an_unknown_event_answers_nothing(tmp_path, proj):
    assert live.hook({"hook_event_name": "UserPromptSubmit", "cwd": str(tmp_path), "prompt": "How does it work?"}) is None
    assert live.hook(payload(proj, "PreToolUse", tool_name="Edit")) is None
    assert live.hook({}) is None


def test_the_stop_event_refreshes_a_stale_index_in_the_background_and_only_when_on(proj, monkeypatch):
    started = []
    monkeypatch.setattr(live, "index_state", lambda repo: {"state": "stale", "changed": 2, "files": ["a.py"]})
    monkeypatch.setattr(live, "spawn_detached", lambda argv, repo: started.append(argv) or True)
    assert live.hook(payload(proj, "Stop")) is None
    assert len(started) == 1 and started[0][-4:-1] == ["update", "--fast", "--repo"]
    live.set_setting(proj, "refresh", "off")
    live.hook(payload(proj, "Stop"))
    assert len(started) == 1


@pytest.mark.parametrize("state, ran", [("fresh", False), ("building", False), ("none", False)])
def test_refresh_starts_nothing_when_there_is_nothing_to_do(proj, monkeypatch, state, ran):
    monkeypatch.setattr(live, "index_state", lambda repo: {"state": state, "changed": 0, "files": [], "why": "x"})
    monkeypatch.setattr(live, "spawn_detached", lambda *a: pytest.fail("nothing should start"))
    assert live.refresh(proj)["ran"] is ran


# -- the commit guard --------------------------------------------------------------------------------------------

def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
                          cwd=cwd, check=True, capture_output=True, text=True).stdout


@needs_git
def test_a_moved_head_starts_a_review_once_and_the_model_hears_of_it_once(proj, monkeypatch):
    git(proj, "init", "-q")
    git(proj, "add", "-A")
    git(proj, "commit", "-q", "-m", "one")
    live.set_setting(proj, "guard", "on")
    started = []
    monkeypatch.setattr(live, "spawn_detached", lambda argv, repo: started.append(argv) or True)
    shell = payload(proj, "PostToolUse", tool_name="Bash", tool_input={"command": "git status"})
    assert live.hook(shell) is None and started == []                       # the first call only records HEAD
    (proj / "orders" / "service.py").write_text("def place(o):\n    return o + 1\n", encoding="utf-8")
    git(proj, "commit", "-aq", "-m", "two")
    live.hook(shell)
    assert len(started) == 1 and started[0][-3:-1] == ["review", "--repo"]
    live.hook(shell)
    assert len(started) == 1                                                # HEAD did not move again
    monkeypatch.setattr(live, "_run", lambda argv, repo, timeout, stdin=None: (
        (0, json.dumps({"summary": "1 symbol changed", "risk": {"score": 31, "of": 100, "band": "moderate"},
                        "counts": {"findings": 2}}), "") if "review" in argv else (0, "abc1234567\n", "")))
    info = live.review_commit(proj)
    assert info["ok"] and info["score"] == 31 and info["findings"] == 2
    out = live.hook(payload(proj, "UserPromptSubmit", prompt="ok thanks"))
    assert "risk 31/100 (moderate), 2 finding(s)" in context(out)
    assert live.hook(payload(proj, "UserPromptSubmit", prompt="ok thanks")) is None       # told once


@needs_git
def test_the_guard_is_off_by_default(proj, monkeypatch):
    git(proj, "init", "-q")
    git(proj, "add", "-A")
    git(proj, "commit", "-q", "-m", "one")
    monkeypatch.setattr(live, "spawn_detached", lambda *a: pytest.fail("the guard is off"))
    shell = payload(proj, "PostToolUse", tool_name="Bash", tool_input={"command": "git commit"})
    live.hook(shell)
    git(proj, "commit", "-q", "--allow-empty", "-m", "two")
    live.hook(shell)


def test_a_failed_review_is_told_as_failed(proj, monkeypatch):
    monkeypatch.setattr(live, "_run", lambda argv, repo, timeout, stdin=None: (1, "", "boom"))
    info = live.review_commit(proj)
    assert info["ok"] is False
    assert "did not finish" in live.review_line(info)


# -- the CLI ----------------------------------------------------------------------------------------------------------

def run_cli(argv, capsys, stdin=None, monkeypatch=None):
    if stdin is not None:
        monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(stdin.encode("utf-8"))))
    code = cli.main(argv)
    cap = capsys.readouterr()
    return code, cap.out, cap.err


def test_the_hook_command_never_fails_on_garbage_and_prints_the_answer_as_one_json_line(proj, capsys, monkeypatch):
    live.set_setting(proj, "auto", "nudge")
    good = json.dumps(payload(proj, "UserPromptSubmit", prompt="How does the checkout reach the database?"))
    code, out, _ = run_cli(["live", "hook"], capsys, good, monkeypatch)
    assert code == 0 and len(out.strip().splitlines()) == 1
    assert json.loads(out)["hookSpecificOutput"]["additionalContext"].startswith("[Verinoda auto-context]")
    for junk in ("", "not json", "[1, 2]", '{"cwd": 5}'):
        code, out, err = run_cli(["live", "hook"], capsys, junk, monkeypatch)
        assert code == 0 and out == "" and err == ""


def test_auto_guard_and_config_commands(proj, capsys):
    base = ["--repo", str(proj)]
    code, out, _ = run_cli(["live", "auto", "search", *base], capsys)
    assert code == 0 and "auto: search" in out
    code, out, _ = run_cli(["live", "guard", *base], capsys)                  # no value toggles
    assert "guard: on" in out
    code, out, _ = run_cli(["live", "guard", *base], capsys)
    assert "guard: off" in out
    code, out, _ = run_cli(["live", "config", "check", "off", *base], capsys)
    assert "check: off" in out
    code, out, _ = run_cli(["live", "config", "check", *base], capsys)
    assert out.strip() == "check: off"
    code, out, err = run_cli(["live", "auto", "loud", *base], capsys)
    assert code == 2 and "auto is one of" in err
    code, out, err = run_cli(["live", "config", "nope", *base], capsys)
    assert code == 2 and "unknown setting" in err


def test_context_command_shows_what_the_hook_would_attach(proj, capsys):
    base = ["--repo", str(proj)]
    code, out, _ = run_cli(["live", "context", "How", "does", "checkout", "work?", *base], capsys)
    assert "nothing attached" in out
    code, out, _ = run_cli(["live", "context", "How", "does", "checkout", "work?", "--mode", "nudge", *base], capsys)
    assert "[Verinoda auto-context]" in out


def test_check_command_exits_3_when_a_name_is_missing(proj, capsys, monkeypatch):
    monkeypatch.setattr(live, "run_check", lambda repo, rel: REPORT)
    code, out, _ = run_cli(["live", "check", "orders/service.py", "--repo", str(proj)], capsys)
    assert code == 3 and "apply_discountt" in out
    monkeypatch.setattr(live, "run_check", lambda repo, rel: {"sites": [], "summary": {"unknown": 4}})
    code, out, _ = run_cli(["live", "check", "orders/service.py", "--repo", str(proj)], capsys)
    assert code == 0 and "no name that does not exist" in out and "4 site(s) of the change could not be checked" in out


def test_check_without_files_reads_the_whole_change_and_groups_by_file(proj, capsys, monkeypatch):
    report = {"sites": [*REPORT["sites"]], "summary": {"unknown": 2}}
    monkeypatch.setattr(live, "run_check_diff", lambda repo: report)
    res = live.check_all(proj)
    assert res["bad"] == 2 and [r["file"] for r in res["results"]] == ["orders/other.py", "orders/service.py"]
    assert "apply_discountt" in res["results"][1]["note"] and res["results"][0]["bad"] == 1
    monkeypatch.setattr(live, "run_check_diff", lambda repo: {"sites": [], "summary": {"unknown": 3}})
    clean = live.check_all(proj)
    assert clean["bad"] == 0 and clean["results"][0]["unknown"] == 3 and clean["results"][0]["note"] is None
    monkeypatch.setattr(live, "run_check_diff", lambda repo: None)
    assert live.check_all(proj)["results"][0]["checked"] is False
    monkeypatch.setattr(live, "run_check_diff", lambda repo: report)
    code, out, _ = run_cli(["live", "check", "--repo", str(proj)], capsys)
    assert code == 3 and "apply_discountt" in out


def test_a_file_name_that_looks_like_an_option_reaches_check_as_a_path(proj, monkeypatch):
    seen = []
    monkeypatch.setattr(live, "_run", lambda argv, repo, timeout, stdin=None: seen.append(argv) or (2, "", ""))
    live.run_check(proj, "--help.py")
    assert seen[-1][-2:] == ["--", "--help.py"]


# -- a real index -------------------------------------------------------------------------------------------------------

@needs_git
def test_status_follows_a_real_index_fresh_then_stale(tmp_path, capsys):
    p = tmp_path / "app"
    shutil.copytree(ROOT / "examples" / "orders_app", p)
    git(p, "init", "-q")
    git(p, "add", "-A")
    git(p, "commit", "-q", "-m", "init")
    code, _, _ = run_cli(["live", "status", "--repo", str(p)], capsys)
    assert code == 0
    assert live.index_state(p)["state"] == "none"
    assert cli.main(["scan", str(p)]) == 0
    capsys.readouterr()
    assert live.index_state(p)["state"] == "fresh"
    (p / "orders" / "service.py").write_text((p / "orders" / "service.py").read_text("utf-8") + "\n# edit\n", "utf-8")
    ix = live.index_state(p)
    assert ix["state"] == "stale" and ix["files"] == ["orders/service.py"]
    code, out, _ = run_cli(["live", "status", "--repo", str(p), "--json"], capsys)
    assert json.loads(out)["index"]["changed"] == 1
    assert "stale: 1 file(s) changed" in live.render_status(live.status(p))
    assert os.environ.get("GRAPHIFY_OUT")
