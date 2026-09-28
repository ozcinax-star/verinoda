"""detect() takes its file list from git in a git work tree: the same corpus as the walk (Verinoda patch)."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import shutil  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda.project_index import detect as dt  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")

READ_BY_VERINODA = ("files", "total_words", "unclassified")  # what the rebuild reads from detect()


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args],
                   cwd=cwd, check=True, capture_output=True)


def _write(root: Path, rel: str, text: str = "x = 1\n") -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


@pytest.fixture()
def repo(tmp_path) -> Path:
    root = tmp_path / "repo"
    _write(root, "app/main.py", "def main():\n    return 1\n")
    _write(root, "app/logs/keep.log", "tracked although ignored\n")
    _write(root, "docs/guide.md", "# Guide\n\nSome words here.\n")
    _write(root, "build/gen.py")                 # a noise dir, tracked: pruned whatever git says
    _write(root, "vendor/lib.py")                # tracked, excluded by .graphifyignore
    _write(root, "app/secret_notes.py")          # tracked, excluded file by file
    _write(root, "package-lock.json", "{}\n")    # a skipped file name
    _write(root, "Makefile", "all:\n\techo\n")   # unclassified
    _write(root, ".gitignore", "*.log\nscratch/\n")
    _write(root, "app/.gitignore", "local_*.py\n")
    _write(root, ".graphifyignore", "vendor/\nsecret_notes.py\n")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "add", "-f", "app/logs/keep.log")
    _git(root, "commit", "-q", "-m", "init")
    _write(root, "app/new_module.py")            # untracked, not ignored
    _write(root, "app/local_settings.py")        # untracked, nested .gitignore
    _write(root, "app/logs/today.log")           # untracked, root .gitignore
    _write(root, "scratch/tmp.py")               # untracked, ignored directory
    _write(root, "private/draft.py")             # untracked, info/exclude
    (root / ".git" / "info").mkdir(exist_ok=True)
    (root / ".git" / "info" / "exclude").write_text("private/\n", encoding="utf-8")
    _write(root, ".verinoda/index/graph.json", "{}\n")  # the configured output dir
    _write(root, ".verinoda/notes.md", "# notes\n")
    return root


def _both(root: Path, **kw) -> tuple[dict, dict]:
    return dt.detect(root, **kw), dt.detect(root, enumeration="walk", **kw)


def test_git_listing_gives_the_walks_corpus(repo):
    fast, walk = _both(repo)
    assert fast["enumeration"] == "git" and walk["enumeration"] == "walk"
    for key in READ_BY_VERINODA:
        assert fast[key] == walk[key], key
    code = {Path(f).relative_to(repo).as_posix() for f in fast["files"]["code"]}
    assert code == {"app/main.py", "app/new_module.py"}
    # the report of what was left out names the same things
    assert str(repo / "vendor") + os.sep in fast["ignored"] and str(repo / "vendor") + os.sep in walk["ignored"]
    assert str(repo / "app" / "secret_notes.py") in fast["ignored"]
    assert str(repo / "build") + os.sep in fast["pruned_noise_dirs"]


def test_excludes_and_a_deleted_tracked_file(repo):
    (repo / "docs" / "guide.md").unlink()  # still in git's index, gone from the work tree
    fast, walk = _both(repo, extra_excludes=["app/new_module.py"])
    assert fast["enumeration"] == "git"
    for key in READ_BY_VERINODA:
        assert fast[key] == walk[key], key
    assert not any(f.endswith("guide.md") for f in fast["files"]["document"])
    assert fast["skipped_sensitive"] == walk["skipped_sensitive"]


@pytest.mark.parametrize("case", ["negation", "nested_repo", "submodule_file", "no_gitignore"])
def test_the_walk_runs_where_git_would_list_something_else(repo, case):
    if case == "negation":  # .graphifyignore may re-include what .gitignore drops; git never lists it
        _write(repo, ".graphifyignore", "vendor/\n!app/logs/today.log\n")
    elif case == "nested_repo":  # an untracked repository inside: git lists it as `dir/`, the walk descends
        _write(repo, "tools/inner/x.py")
        _git(repo / "tools" / "inner", "init", "-q")
    elif case == "submodule_file":
        _write(repo, ".gitmodules", "")
    else:  # no .gitignore rules at the root: no git process at all
        (repo / ".gitignore").unlink()
        (repo / ".git" / "info" / "exclude").unlink()
    fast, walk = _both(repo)
    assert fast["enumeration"] == "walk"
    for key in READ_BY_VERINODA:
        assert fast[key] == walk[key], key


def test_git_failure_falls_back_to_the_walk(repo, monkeypatch):
    def broken(*a, **k):
        raise OSError("no git")

    walk = dt.detect(repo, enumeration="walk")
    monkeypatch.setattr(dt.subprocess, "run", broken)
    fast = dt.detect(repo)
    assert fast["enumeration"] == "walk"
    # without git the walk cannot see which files are tracked, so only the tracked-but-ignored file differs
    assert set(walk["unclassified"]) - set(fast["unclassified"]) <= {str(repo / "app" / "logs" / "keep.log")}
    assert fast["files"]["code"] == walk["files"]["code"]


def test_symlinks_are_checked_like_the_walk(repo, tmp_path):
    outside = _write(tmp_path, "outside/secret.py")
    try:
        os.symlink(outside, repo / "app" / "link_out.py")
        os.symlink(repo / "app", repo / "app_link", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available here")
    fast, walk = _both(repo)
    assert fast["enumeration"] == "git"
    for key in READ_BY_VERINODA:
        assert fast[key] == walk[key], key
    assert any("link_out.py" in s and "outside scan root" in s for s in fast["skipped_sensitive"])
