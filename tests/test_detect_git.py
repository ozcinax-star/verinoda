"""detect() takes its file list from git in a git work tree: the same corpus as the walk (Verinoda patch)."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import json  # noqa: E402
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


@pytest.mark.parametrize("case", ["negation", "nested_repo", "submodule_file", "no_gitignore", "embedded_repo",
                                  "utf16_gitignore", "ansi_gitignore"])
def test_the_walk_runs_where_git_would_list_something_else(repo, case):
    if case == "negation":  # .graphifyignore may re-include what .gitignore drops; git never lists it
        _write(repo, ".graphifyignore", "vendor/\n!app/logs/today.log\n")
    elif case == "nested_repo":  # an untracked repository inside: git lists it as `dir/`, the walk descends
        _write(repo, "tools/inner/x.py")
        _git(repo / "tools" / "inner", "init", "-q")
    elif case == "submodule_file":
        _write(repo, ".gitmodules", "")
    elif case == "embedded_repo":
        # `git add` of a nested clone records a gitlink and no .gitmodules: git lists `inner` and none of
        # its files, the walk descends (review of D66, finding 1)
        _write(repo, "inner/lib/core.py", "def core():\n    return 1\n")
        _git(repo / "inner", "init", "-q")
        _git(repo / "inner", "add", "-A")
        _git(repo / "inner", "commit", "-q", "-m", "inner")
        _git(repo, "add", "inner")
        _git(repo, "commit", "-q", "-m", "embed")
    elif case == "utf16_gitignore":
        # what Windows PowerShell 5.1 `"..." > .gitignore` writes: the walk decodes it, git reads bytes
        # (review of D66, finding 4)
        _write(repo, "private2/salaries.md", "# salaries\n")
        (repo / ".gitignore").write_bytes("*.log\r\nscratch/\r\nprivate2/\r\n".encode("utf-16"))
    elif case == "ansi_gitignore":  # an ANSI code page file: the walk decodes it, git matches the raw bytes
        _write(repo, "Orçamento/plan.md", "# plan\n")
        (repo / ".gitignore").write_bytes("*.log\nscratch/\nOrçamento/\n".encode("cp1254"))
    else:  # no .gitignore rules at the root: no git process at all
        (repo / ".gitignore").unlink()
        (repo / ".git" / "info" / "exclude").unlink()
    fast, walk = _both(repo)
    assert fast["enumeration"] == "walk"
    for key in READ_BY_VERINODA:
        assert fast[key] == walk[key], key
    if case == "embedded_repo":
        assert str(repo / "inner" / "lib" / "core.py") in fast["files"]["code"]
        assert not any("not a regular file" in s for s in fast["skipped_sensitive"])
    elif case in ("utf16_gitignore", "ansi_gitignore"):
        assert not any("salaries" in f or "plan.md" in f for f in fast["files"]["document"])


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


@pytest.mark.skipif(os.name != "nt", reason="junctions are a Windows directory link")
def test_a_junction_out_of_the_root_stays_out(repo, tmp_path):
    """git lists files through a junction as through a directory; the walk's realpath check kept them out."""
    _write(tmp_path, "outside/secret.py")
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(repo / "linked"), str(tmp_path / "outside")],
                          capture_output=True)
    if made.returncode != 0:
        pytest.skip("mklink /J is not available here")
    fast, walk = _both(repo)
    assert fast["enumeration"] == "git"
    for key in READ_BY_VERINODA:
        assert fast[key] == walk[key], key
    assert not any("secret.py" in f for f in fast["files"]["code"])
    assert any("linked" in s and "outside scan root" in s for s in fast["skipped_sensitive"])


def test_inherited_git_variables_do_not_redirect_the_listing(repo, tmp_path, monkeypatch):
    """A git hook exports GIT_DIR / GIT_INDEX_FILE for its own repository (review of D66, finding 9)."""
    walk = dt.detect(repo, enumeration="walk")
    other = tmp_path / "other"
    _write(other, "elsewhere.py")
    _git(other, "init", "-q")
    _git(other, "add", "-A")
    _git(other, "commit", "-q", "-m", "other")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_INDEX_FILE", str(other / ".git" / "index"))
    fast = dt.detect(repo)
    assert fast["enumeration"] == "git"
    for key in READ_BY_VERINODA:
        assert fast[key] == walk[key], key


def test_an_unignored_noise_directory_is_left_to_the_walks_rule(repo):
    """git is told to skip the directories the walk prunes by name, so an un-ignored node_modules costs
    no enumeration and a repository inside it does not force the walk (review of D66, finding 11)."""
    _write(repo, "node_modules/dep/index.js", "module.exports = 1\n")
    _git(repo / "node_modules" / "dep", "init", "-q")  # --others would list it as `node_modules/dep/`
    fast, walk = _both(repo)
    assert fast["enumeration"] == "git"
    for key in READ_BY_VERINODA:
        assert fast[key] == walk[key], key
    assert str(repo / "node_modules") + os.sep in fast["pruned_noise_dirs"]


def test_a_very_deep_path_does_not_exhaust_the_recursion_limit(repo, monkeypatch):
    """One step per directory level was a recursive call (review of D66, finding 6)."""
    deep = "a/" * 1100 + "f.py"
    monkeypatch.setattr(dt, "_git_listed_files", lambda root: (["app/main.py", deep], []))
    got = dt._git_enumerate(repo, [], set(), repo / ".verinoda" / "index")
    assert got is not None
    assert repo / "app" / "main.py" in got[0]


def _deny_listing(d: Path):
    """Make directory ``d`` unlistable for this user; return the undo, or None when that is not possible."""
    if os.name == "nt":
        user = subprocess.run(["whoami"], capture_output=True, text=True).stdout.strip()
        if not user or subprocess.run(["icacls", str(d), "/deny", f"{user}:(RD)"],
                                      capture_output=True).returncode != 0:
            return None
        return lambda: subprocess.run(["icacls", str(d), "/remove:d", user], capture_output=True)
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return None  # root lists any directory
    os.chmod(d, 0)
    return lambda: os.chmod(d, 0o755)


def test_an_unreadable_directory_is_reported_by_the_walk(repo):
    """git only warns on stderr about a directory it cannot open; the walk names it in walk_errors
    (review of D66, finding 8)."""
    locked = _write(repo, "locked/a.py").parent
    undo = _deny_listing(locked)
    if undo is None:
        pytest.skip("cannot make a directory unreadable here")
    try:
        try:
            os.listdir(locked)
            pytest.skip("the directory is still readable (elevated rights)")
        except OSError:
            pass
        fast = dt.detect(repo)
    finally:
        undo()
    assert fast["enumeration"] == "walk"
    assert any("locked" in e for e in fast["walk_errors"])


@pytest.fixture()
def star_repo(tmp_path) -> Path:
    """`.gitignore` = `test*.py`: git drops a file whose name matches, the Python matcher also
    `tests/foo.py` (its `*` crosses `/`)."""
    root = tmp_path / "star"
    _write(root, "main.py", "def main():\n    return 1\n")
    _write(root, ".gitignore", "test*.py\n")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    _write(root, "tests/foo.py", "def foo():\n    return 2\n")  # untracked, kept by git
    _write(root, "test_x.py")  # untracked, ignored by both
    return root


def test_the_ignore_predicate_keeps_what_git_keeps(star_repo):
    """The reconcile step asks ignored_predicate whether a corpus-absent file is ignored; a file git lists
    is not (review of D66, finding 2)."""
    fast, walk = _both(star_repo)
    foo = star_repo / "tests" / "foo.py"
    assert fast["enumeration"] == "git" and str(foo) in fast["files"]["code"]
    assert str(foo) not in walk["files"]["code"]  # the two matchers differ here
    ignored = dt.ignored_predicate(star_repo, gitignore=True)
    assert ignored(foo) is False
    assert ignored(star_repo / "test_x.py") is True
    assert dt.ignored_predicate(star_repo, gitignore=False)(foo) is False
    _write(star_repo, ".graphifyignore", "tests/foo.py\n")  # an explicit rule still decides
    assert dt.ignored_predicate(star_repo, gitignore=True)(foo) is True


def test_a_rebuild_that_walks_does_not_evict_what_git_listed(star_repo):
    """Scan through git, then a full rebuild that has to walk (.gitmodules appeared), then git again: the
    file git keeps stays in the graph throughout (review of D66, finding 2)."""
    from verinoda.project_index import paths
    from verinoda.project_index.watch import _rebuild_code

    graph = star_repo / paths.GRAPHIFY_OUT / "graph.json"

    def sources() -> set:
        return {n.get("source_file") for n in json.loads(graph.read_text(encoding="utf-8"))["nodes"]}

    assert _rebuild_code(star_repo, no_cluster=True, acquire_lock=False) is True
    assert "tests/foo.py" in sources()
    (star_repo / ".gitmodules").write_text("", encoding="utf-8")  # detect() walks from here on
    assert dt.detect(star_repo)["enumeration"] == "walk"
    assert _rebuild_code(star_repo, no_cluster=True, acquire_lock=False) is True
    assert "tests/foo.py" in sources()
    (star_repo / ".gitmodules").unlink()
    assert _rebuild_code(star_repo, no_cluster=True, acquire_lock=False) is True
    assert "tests/foo.py" in sources()
