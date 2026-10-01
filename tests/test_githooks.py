"""Git hooks that run ``verinoda update`` (verinoda/githooks.py): marked blocks added to post-commit, post-checkout,
post-merge and post-rewrite without touching what else is there, removed exactly, never written where another
tool owns the hooks, and never running the project's own code."""

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
    (r / ".gitignore").write_text(".verinoda/\n", encoding="utf-8")
    (r / ".verinoda").mkdir()   # a project Verinoda has set up: the hooks run only for one
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


def test_install_creates_the_hooks_and_uninstall_removes_them(repo):
    res = githooks.install(repo)
    assert res["hooks"] == {h: "created" for h in githooks.HOOKS}
    text = (_hooks(repo) / "post-commit").read_text(encoding="utf-8")
    assert text.startswith("#!/bin/sh\n") and githooks.BEGIN in text and "VERINODA_NO_HOOK" in text
    # the work tree is never on sys.path: a project's own verinoda/ folder cannot run
    assert (" -P -m verinoda update " in text) or (" -I -m verinoda update " in text)
    assert "rebase-merge" in text and '"$3" = "1"' in (_hooks(repo) / "post-checkout").read_text(encoding="utf-8")
    assert githooks.install(repo)["hooks"]["post-merge"] == "already installed"
    assert githooks.status(repo)["hooks"] == {h: "installed" for h in githooks.HOOKS}
    assert githooks.uninstall(repo)["hooks"] == {h: "removed (the file was verinoda's)" for h in githooks.HOOKS}
    assert not any((_hooks(repo) / h).exists() for h in githooks.HOOKS)


def test_an_existing_hook_is_kept_and_restored_exactly(repo):
    for own in (b"#!/bin/sh\necho mine\nexit 0\n", b"#!/bin/sh", b"#!/bin/bash\r\necho crlf\r\n"):
        (_hooks(repo) / "post-commit").write_bytes(own)
        githooks.install(repo)
        text = (_hooks(repo) / "post-commit").read_bytes().decode()
        # the block right after the #! line: the hook's own `exit 0` cannot skip it
        assert text.split("\n")[1].startswith(githooks.BEGIN), text
        assert githooks.uninstall(repo)["hooks"]["post-commit"] == "block removed"
        assert (_hooks(repo) / "post-commit").read_bytes() == own


def test_hooks_that_are_not_shell_scripts_or_were_edited_are_left_alone(repo):
    (_hooks(repo) / "post-merge").write_bytes(b"#!/home/shared/venv/bin/python\nprint(1)\n")
    (_hooks(repo) / "post-checkout").write_bytes(b"#!/usr/bin/env -S bash -e\necho ok\n")
    res = githooks.install(repo)
    assert res["hooks"]["post-merge"].startswith("left alone: not a shell script")
    assert res["hooks"]["post-checkout"] == "added to the existing hook"
    # an end marker changed by hand: nothing is removed, so no line of the user's is lost
    p = _hooks(repo) / "post-checkout"
    damaged = p.read_text(encoding="utf-8").replace(githooks.END, githooks.END + " (kept)")
    p.write_text(damaged, encoding="utf-8", newline="\n")
    assert "no end marker" in githooks.uninstall(repo)["hooks"]["post-checkout"]
    assert p.read_text(encoding="utf-8") == damaged
    assert "no end marker" in githooks.install(repo)["hooks"]["post-checkout"]


def test_hooks_run_after_a_commit_a_branch_switch_and_a_rebase_but_not_its_picks(repo, tmp_path):
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
    _git(repo, "commit", "-q", "--amend", "-m", "amended")  # post-rewrite at the end of an amend
    assert _wait(marker)


def test_a_project_without_its_verinoda_folder_is_not_updated(repo, tmp_path):
    marker = tmp_path / "ran.txt"
    githooks.install(repo, command=_marker_command(marker))
    shutil.rmtree(repo / ".verinoda")
    (repo / "a.py").write_text("x = 5\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "after removal")
    time.sleep(2)
    assert not marker.exists()


def test_the_project_s_own_verinoda_package_never_runs(repo, tmp_path):
    (repo / "verinoda").mkdir()
    (repo / "verinoda" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "verinoda" / "__main__.py").write_text(
        f"import pathlib; pathlib.Path({str(tmp_path / 'pwned.txt')!r}).write_text('x')\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "a package named verinoda")
    githooks.install(repo)   # the real command: <python> -P -m verinoda update <project>
    (repo / "a.py").write_text("x = 6\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "trigger")
    time.sleep(4)
    assert not (tmp_path / "pwned.txt").exists()


def test_hooks_managed_elsewhere_are_not_written_nor_removed(repo, tmp_path, capsys):
    shared = tmp_path / "husky"
    shared.mkdir()
    (shared / "post-commit").write_bytes(b"#!/bin/sh\necho husky\n")
    _git(repo, "config", "core.hooksPath", str(shared))
    res = githooks.install(repo)
    assert "core.hooksPath" in res["refused"] and set(res["lines"]) == set(githooks.HOOKS)
    assert githooks.uninstall(repo)["refused"]
    assert (shared / "post-commit").read_bytes() == b"#!/bin/sh\necho husky\n"
    assert cli.main(["hooks", "install", "--repo", str(repo)]) == 2
    assert "not written" in capsys.readouterr().out
    # a hooks path inside the repository's own git folder is git's, not another tool's
    _git(repo, "config", "core.hooksPath", ".git/myhooks")
    assert githooks.install(repo)["hooks"]["post-commit"] == "created"


def test_projects_sharing_hooks_status_and_blocks_of_folders_that_are_gone(repo):
    sub = repo / "pkg"
    (sub / ".verinoda").mkdir(parents=True)
    other = repo / "pkg2"
    (other / ".verinoda").mkdir(parents=True)
    githooks.install(sub)
    githooks.install(other)
    st = githooks.status(sub)
    assert st["hooks"]["post-merge"] == "installed" and st["other_projects"] == [str(other).replace("\\", "/")]
    # a project whose path is a prefix of another's is not reported as installed
    assert githooks.status(repo)["hooks"]["post-merge"].startswith("other hook")
    shutil.rmtree(other)
    st = githooks.status(sub)
    assert st["gone"] == [str(other).replace("\\", "/")]
    res = githooks.uninstall(sub, gone=True)
    assert res["hooks"]["post-merge"] == "1 block(s) removed"
    assert githooks.status(sub)["gone"] == [] and githooks.status(sub)["hooks"]["post-merge"] == "installed"


def test_a_worktree_s_block_runs_only_for_its_own_work_tree(repo, tmp_path):
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", str(wt))
    (wt / ".verinoda").mkdir()
    marker = tmp_path / "wt-ran.txt"
    githooks.install(wt, command=_marker_command(marker))
    (repo / "a.py").write_text("x = 7\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "in the main checkout")   # the shared hook fires, but not for the worktree
    time.sleep(2)
    assert not marker.exists()
    (wt / "a.py").write_text("x = 8\n", encoding="utf-8")
    _git(wt, "commit", "-qam", "in the worktree")
    assert _wait(marker)


def test_cli_status_print_and_dry_run(repo, capsys):
    assert cli.main(["hooks", "install", "--repo", str(repo), "--dry-run"]) == 0
    assert not (_hooks(repo) / "post-commit").exists()
    assert cli.main(["hooks", "install", "--repo", str(repo), "--print"]) == 0
    assert "--- post-rewrite ---" in capsys.readouterr().out
    assert cli.main(["hooks", "install", "--repo", str(repo)]) == 0
    assert cli.main(["hooks", "status", "--repo", str(repo)]) == 0
    assert "post-merge: installed" in capsys.readouterr().out
    assert cli.main(["hooks", "uninstall", "--repo", str(repo)]) == 0


def test_outside_git_is_an_error(tmp_path):
    assert cli.main(["hooks", "status", "--repo", str(tmp_path)]) == 2
