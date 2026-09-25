"""Build identity: which Verinoda build runs (``verinoda --version``, doctor, MCP serverInfo, schema errors).

Every source is exercised on a throw-away tree: a git checkout of a Verinoda-shaped source folder
(loose refs, packed refs, a linked worktree, local changes), a ``git archive`` export filled in
through the repository's own ``.gitattributes`` line, and PEP 610 install records. A package that
sits in another project's checkout, or install metadata that belongs to another copy, never names
a commit.
"""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import io  # noqa: E402
import json  # noqa: E402
import shutil  # noqa: E402
import sqlite3  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import tarfile  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import buildinfo  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SHA = "0123456789abcdef0123456789abcdef01234567"
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false",
                           "-c", "init.defaultBranch=main", *args], cwd=cwd, check=True, capture_output=True,
                          text=True, stdin=subprocess.DEVNULL).stdout.strip()


def _source_tree(root: Path, name: str = "verinoda") -> Path:
    """A folder shaped like Verinoda's source: pyproject.toml naming ``name`` and a ``verinoda/`` package."""
    pkg = root / "verinoda"
    (pkg / "data").mkdir(parents=True)
    (root / "pyproject.toml").write_text(f'[project]\nname = "{name}"\nversion = "0.1.0.dev0"\n', encoding="utf-8")
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    shutil.copy(ROOT / "verinoda" / buildinfo.ARCHIVAL, pkg / buildinfo.ARCHIVAL)
    return pkg


def _no_record(pkg_dir):
    return None, "no installed distribution"


@needs_git
def test_checkout_commit_branch_and_local_changes(tmp_path):
    pkg = _source_tree(tmp_path / "src")
    root = pkg.parent
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "one")
    head = _git(root, "rev-parse", "HEAD")
    info = buildinfo.collect(pkg, record=_no_record)
    assert (info["source"], info["commit"], info["branch"], info["local_changes"]) == ("git checkout", head, "main",
                                                                                       False)
    assert info["build"] == head[:12] and "read HEAD" in info["evidence"][0]
    assert buildinfo.describe(info) == f"commit {head[:12]}, git checkout, branch main, no local changes"
    assert buildinfo.server_version(info) == f"{info['version']}+{head[:12]}"

    (pkg / "extra.py").write_text("x = 1\n", encoding="utf-8")  # untracked module: a different build
    info = buildinfo.collect(pkg, record=_no_record)
    assert info["local_changes"] is True and info["build"] == head[:12] + "+local-changes"
    assert "with local changes" in buildinfo.describe(info)
    assert buildinfo.server_version(info).endswith(f"+{head[:12]}.local.changes")
    (pkg / "extra.py").unlink()
    (root / "notes.txt").write_text("outside the package\n", encoding="utf-8")  # not part of the build
    assert buildinfo.collect(pkg, record=_no_record)["local_changes"] is False

    no_check = buildinfo.collect(pkg, record=_no_record, check_changes=False)
    assert no_check["local_changes"] is None and "local changes not checked" in buildinfo.describe(no_check)


@needs_git
def test_packed_refs_detached_head_and_linked_worktree(tmp_path):
    pkg = _source_tree(tmp_path / "src")
    root = pkg.parent
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "one")
    head = _git(root, "rev-parse", "HEAD")
    _git(root, "pack-refs", "--all")
    assert not (root / ".git" / "refs" / "heads" / "main").exists()
    assert buildinfo.read_head(root) == (head, "main")
    _git(root, "checkout", "-q", "--detach")
    assert buildinfo.read_head(root) == (head, None)
    _git(root, "checkout", "-q", "main")
    _git(root, "worktree", "add", "-q", "-b", "side", str(tmp_path / "wt"))
    assert (tmp_path / "wt" / ".git").is_file()  # a linked worktree: .git is a file
    info = buildinfo.collect(tmp_path / "wt" / "verinoda", record=_no_record)
    assert (info["commit"], info["branch"], info["source"]) == (head, "side", "git checkout")


@needs_git
def test_a_package_inside_another_projects_checkout_names_no_commit(tmp_path):
    pkg = _source_tree(tmp_path / "app", name="someone-elses-app")  # e.g. a vendored copy
    _git(pkg.parent, "init", "-q")
    _git(pkg.parent, "add", "-A")
    _git(pkg.parent, "commit", "-q", "-m", "one")
    info = buildinfo.collect(pkg, record=_no_record)
    assert info["commit"] is None and info["source"] == "unknown" and info["build"] == "unknown"
    assert buildinfo.describe(info) == "build unknown"
    assert buildinfo.server_version(info) == f"{info['version']}+unknown"


def test_unreadable_git_files_are_not_guessed(tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    (root / ".git").write_text("not a gitdir line\n", encoding="utf-8")
    assert buildinfo.read_head(root) == (None, None)
    gd = tmp_path / "gd"
    gd.mkdir()
    (root / ".git").write_text(f"gitdir: {gd}\n", encoding="utf-8")
    (gd / "HEAD").write_text("ref: refs/heads/../../escape\n", encoding="utf-8")
    assert buildinfo.read_head(root) == (None, None)
    (gd / "HEAD").write_text("ref: refs/heads/unborn\n", encoding="utf-8")
    assert buildinfo.read_head(root) == (None, "unborn")


@needs_git
def test_git_archive_fills_in_the_stamp_through_the_repositorys_attributes(tmp_path):
    """The real .gitattributes line and stamp file, exported the way GitHub builds /archive/<ref>.zip."""
    repo = tmp_path / "repo"
    pkg = _source_tree(repo)
    shutil.copy(ROOT / ".gitattributes", repo / ".gitattributes")
    assert buildinfo.read_archival(pkg) is None  # a checkout: placeholders only
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "one")
    head = _git(repo, "rev-parse", "HEAD")
    tar = subprocess.run(["git", "archive", "--format=tar", "HEAD"], cwd=repo, check=True, capture_output=True,
                         stdin=subprocess.DEVNULL).stdout
    out = tmp_path / "export"
    with tarfile.open(fileobj=io.BytesIO(tar)) as tf:
        tf.extractall(out, **({"filter": "data"} if hasattr(tarfile, "data_filter") else {}))
    stamp = buildinfo.read_archival(out / "verinoda")
    assert stamp and stamp["commit"] == head and stamp["date"]
    info = buildinfo.collect(out / "verinoda", record=_no_record)  # no .git: the stamp is the only source
    assert (info["commit"], info["source"]) == (head, "source archive")
    assert buildinfo.describe(info) == f"commit {head[:12]}, source archive"


def test_install_records(tmp_path):
    pkg = tmp_path / "site-packages" / "verinoda"
    pkg.mkdir(parents=True)

    def rec(data):
        return lambda p: (data, "")

    vcs = buildinfo.collect(pkg, record=rec({"url": "https://github.com/o/verinoda", "vcs_info": {
        "vcs": "git", "commit_id": SHA, "requested_revision": "main"}}))
    assert (vcs["commit"], vcs["source"], vcs["ref"]) == (SHA, "git URL", "main")
    assert buildinfo.describe(vcs) == f"commit {SHA[:12]}, installed from git, ref main"

    by_sha = buildinfo.collect(pkg, record=rec({"url": f"https://github.com/o/verinoda/archive/{SHA}.zip",
                                                "archive_info": {}}))
    assert by_sha["commit"] == SHA and buildinfo.describe(by_sha) == f"commit {SHA[:12]}, installed from an archive"

    # what install.sh records today (uv writes no hash): the ref, but no commit - said, not guessed
    main = buildinfo.collect(pkg, record=rec({"url": "https://github.com/o/verinoda/archive/main.zip",
                                              "archive_info": {}}))
    assert main["commit"] is None and main["ref"] == "main" and main["build"] == "unknown"
    assert buildinfo.describe(main) == "build unknown, installed from an archive, ref main, the commit was not recorded"
    tag = buildinfo.collect(pkg, record=rec({"url": "https://github.com/o/verinoda/archive/refs/tags/v0.2.0.tar.gz",
                                             "archive_info": {"hashes": {"sha256": "ab" * 32}}}))
    assert tag["ref"] == "v0.2.0" and tag["archive_sha256"] == "ab" * 32 and tag["commit"] is None

    # the archive's own stamp names the commit that direct_url.json does not
    (pkg / "data").mkdir()
    (pkg / buildinfo.ARCHIVAL).write_text(f"commit: {SHA}\ndate: 2026-09-26T00:00:00+00:00\n", encoding="utf-8")
    stamped = buildinfo.collect(pkg, record=rec({"url": "https://github.com/o/verinoda/archive/main.zip",
                                                 "archive_info": {}}))
    assert (stamped["commit"], stamped["source"], stamped["ref"]) == (SHA, "archive URL", "main")
    assert buildinfo.describe(stamped) == f"commit {SHA[:12]}, installed from an archive, ref main"

    (pkg / buildinfo.ARCHIVAL).unlink()
    local = buildinfo.collect(pkg, record=rec({"url": "file:///C:/src/verinoda", "dir_info": {"editable": True}}))
    assert local["source"] == "editable folder" and local["commit"] is None
    assert buildinfo.describe(local) == "build unknown, editable install, the commit was not recorded"


def test_install_metadata_of_another_copy_is_not_used(tmp_path, monkeypatch):
    import importlib.metadata as md

    here = tmp_path / "running" / "verinoda"
    there = tmp_path / "installed" / "verinoda"
    for d in (here, there):
        d.mkdir(parents=True)
        (d / "__init__.py").write_text("", encoding="utf-8")

    class Dist:
        def __init__(self, loc: Path, record: dict):
            self.loc, self.record = loc, record

        def read_text(self, name):
            return json.dumps(self.record) if name == "direct_url.json" else None

        def locate_file(self, rel):
            return self.loc / rel

    rec = {"url": "https://github.com/o/verinoda", "vcs_info": {"vcs": "git", "commit_id": SHA}}
    monkeypatch.setattr(md, "distribution", lambda name: Dist(there.parent, rec))
    data, note = buildinfo.installed_record(here)
    assert data is None and "another copy" in note
    assert buildinfo.collect(here)["commit"] is None  # never another install's commit
    monkeypatch.setattr(md, "distribution", lambda name: Dist(here.parent, rec))
    assert buildinfo.installed_record(here) == (rec, "")
    assert buildinfo.collect(here)["commit"] == SHA
    # an editable install: the metadata sits in site-packages, the record names the source folder
    editable = {"url": here.parent.as_uri(), "dir_info": {"editable": True}}
    monkeypatch.setattr(md, "distribution", lambda name: Dist(tmp_path / "site-packages", editable))
    assert buildinfo.installed_record(here) == (editable, "")


@needs_git
def test_version_flag_names_the_running_build(tmp_path):
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    r = subprocess.run([sys.executable, "-m", "verinoda", "--version"], cwd=tmp_path, capture_output=True,
                       text=True, env=env, timeout=120, stdin=subprocess.DEVNULL)
    assert r.returncode == 0, r.stderr
    line = r.stdout.strip()
    assert line.startswith("verinoda ") and "(" in line and line.endswith(")")
    if (ROOT / ".git").exists():  # the tests run from a checkout: its HEAD is the build
        head = _git(ROOT, "rev-parse", "HEAD")
        assert f"commit {head[:12]}, git checkout" in line, line
        assert any(s in line for s in ("no local changes", "with local changes", "local changes not checked"))


def test_newer_schema_error_names_both_builds(tmp_path):
    from verinoda.store import SCHEMA_VERSION, SchemaTooNew, Store

    st = Store(tmp_path / "new.db")
    written = st.one("SELECT value FROM meta WHERE key = 'schema_written_by'")["value"]
    assert written == buildinfo.server_version()
    st.close()
    conn = sqlite3.connect(tmp_path / "new.db")
    conn.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'", (str(SCHEMA_VERSION + 1),))
    conn.execute("UPDATE meta SET value = '9.9.9+abcdef012345' WHERE key = 'schema_written_by'")
    conn.commit()
    conn.close()
    with pytest.raises(SchemaTooNew) as exc:
        Store(tmp_path / "new.db")
    msg = str(exc.value)
    assert f"schema v{SCHEMA_VERSION + 1}, migrated by verinoda 9.9.9+abcdef012345" in msg
    assert buildinfo.server_version() in msg and sys.executable in msg and "verinoda doctor" in msg
    assert exc.value.written_by == "9.9.9+abcdef012345"
