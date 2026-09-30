"""Git hooks that run ``verinoda update`` (verinoda/githooks.py): marked blocks added to post-commit, post-checkout
and post-merge without touching what else is there, removed exactly, and never written where another tool owns
the hooks."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from verinoda import cli, githooks

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def _git(cwd: Path, *args: str, env: dict | None = None) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false",
                           "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main", *args], cwd=cwd,
                          check=True, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                          env={**os.environ, **(env or {})}).stdout


@pytest.fixture()
def repo(tmp_path):
    r = tmp_path / "hooks ğ repo"
    r.mkdir()
    _git(r, "init", "-q")
    (r / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "init")
    return r


def _hooks(r: Path) -> Path:
    return r / ".git" / "hooks"


def _marker_command(marker: Path) -> list[str]:
    return [sys.executable, "-c", "import sys,pathlib;pathlib.Path(sys.argv[1]).write_text(chr(120))", str(marker)]


def _wait(p: Path, seconds: float = 15) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if p.exists():
            return True
        time.sleep(0.2)
    return False


def test_install_creates_three_hooks_and_uninstall_removes_them(repo):
    res = githooks.install(repo)
    assert res["hooks"] == {h: "created" for h in githooks.HOOKS}
    text = (_hooks(repo) / "post-commit").read_text(encoding="utf-8")
    assert text.startswith("#!/bin/sh\n") and githooks.BEGIN in text and "-m verinoda update" in text
    assert "VERINODA_NO_HOOK" in text
    assert '"$3" = "1"' in (_hooks(repo) / "post-checkout").read_text(encoding="utf-8")
    assert githooks.install(repo)["hooks"]["post-merge"] == "already installed"
    assert githooks.status(repo)["hooks"] == {h: "installed" for h in githooks.HOOKS}
    assert githooks.uninstall(repo)["hooks"] == {h: "removed (the file was verinoda's)" for h in githooks.HOOKS}
    assert not any((_hooks(repo) / h).exists() for h in githooks.HOOKS)


def test_an_existing_hook_is_kept_and_restored_exactly(repo):
    own = b"#!/bin/sh\necho mine\nexit 0\n"
    (_hooks(repo) / "post-commit").write_bytes(own)
    githooks.install(repo)
    text = (_hooks(repo) / "post-commit").read_text(encoding="utf-8")
    # the block right after the #! line: the hook's own `exit 0` cannot skip it
    assert text.split("\n")[1].startswith(githooks.BEGIN) and text.endswith("echo mine\nexit 0\n")
    assert githooks.uninstall(repo)["hooks"]["post-commit"] == "block removed"
    assert (_hooks(repo) / "post-commit").read_bytes() == own
    (_hooks(repo) / "post-merge").write_bytes(b"#!/usr/bin/env python3\nprint(1)\n")
    assert githooks.install(repo)["hooks"]["post-merge"].startswith("left alone: not a shell script")


def test_hooks_run_after_a_commit_and_a_branch_switch_only(repo, tmp_path):
    marker = tmp_path / "ran.txt"
    githooks.install(repo, command=_marker_command(marker))
    (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "two")
    assert _wait(marker)
    marker.unlink()
    _git(repo, "checkout", "-q", "-b", "other")            # a branch checkout: $3 = 1
    assert _wait(marker)
    marker.unlink()
    (repo / "a.py").write_text("x = 3\n", encoding="utf-8")
    _git(repo, "checkout", "--", "a.py")                    # files checked out: $3 = 0, nothing to re-index
    (repo / "a.py").write_text("x = 4\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "quiet", env={"VERINODA_NO_HOOK": "1"})
    time.sleep(2)
    assert not marker.exists()


def test_hooks_managed_elsewhere_are_not_written(repo, tmp_path, capsys):
    shared = tmp_path / "husky"
    shared.mkdir()
    _git(repo, "config", "core.hooksPath", str(shared))
    res = githooks.install(repo)
    assert "core.hooksPath" in res["refused"] and not any(shared.iterdir())
    assert set(res["lines"]) == set(githooks.HOOKS)
    assert cli.main(["hooks", "install", "--repo", str(repo)]) == 2
    assert "not written" in capsys.readouterr().out
    # a hooks path inside the repository's own git folder is git's, not another tool's
    _git(repo, "config", "core.hooksPath", ".git/myhooks")
    assert githooks.install(repo)["hooks"]["post-commit"] == "created"


def test_two_projects_of_one_repository_each_have_their_block(repo):
    sub = repo / "pkg"
    sub.mkdir()
    githooks.install(repo)
    githooks.install(sub)
    text = (_hooks(repo) / "post-merge").read_text(encoding="utf-8")
    assert text.count(githooks.BEGIN) == 2
    githooks.uninstall(sub)
    text = (_hooks(repo) / "post-merge").read_text(encoding="utf-8")
    assert text.count(githooks.BEGIN) == 1 and str(sub).replace("\\", "/") not in text


def test_cli_status_print_and_dry_run(repo, capsys):
    assert cli.main(["hooks", "install", "--repo", str(repo), "--dry-run"]) == 0
    assert not (_hooks(repo) / "post-commit").exists()
    assert cli.main(["hooks", "install", "--repo", str(repo), "--print"]) == 0
    assert "--- post-checkout ---" in capsys.readouterr().out
    assert cli.main(["hooks", "install", "--repo", str(repo)]) == 0
    assert cli.main(["hooks", "status", "--repo", str(repo)]) == 0
    assert "post-merge: installed" in capsys.readouterr().out
    assert cli.main(["hooks", "uninstall", "--repo", str(repo)]) == 0


def test_outside_git_is_an_error(tmp_path, capsys):
    assert cli.main(["hooks", "status", "--repo", str(tmp_path)]) == 2
