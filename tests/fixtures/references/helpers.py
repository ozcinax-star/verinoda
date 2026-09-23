"""Git and project fixtures for the reference-resolver tests (no network).

``requests_repo`` imitates the layout history of psf/requests that the
research PoC relied on: ``requests/sessions.py`` at v2.30.0 and v2.31.0 (both
annotated tags), a src-layout move (``src/requests/sessions.py``) at v2.32.0 and
on ``main`` - so a ``/blob/main/src/...`` link combined with "2.31" reproduces
mismatch M1b. It also carries a branch/tag name collision (``v1``), pull-request
heads under ``refs/pull/N/head`` (created with ``git update-ref``), a
merge-request head under ``refs/merge-requests/N/head`` and a release branch
with a slash in its name.

``analysed_project`` is the local project: ``uv.lock`` locking requests 2.31.0
and a git-sourced dependency, ``.python-version`` 3.12, and a fake ``.venv``
whose ``requests`` dist-info says 2.31.0 plus a PEP 610 VCS install.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

ENV = ["-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", "-c", "commit.gpgsign=false",
       "-c", "tag.gpgsign=false", "-c", "init.defaultBranch=main"]


def git(root: Path, *args: str, date: str | None = None) -> str:
    import os

    env = dict(os.environ)
    if date:
        env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
    r = subprocess.run(["git", "-C", str(root), *ENV, *args], capture_output=True, text=True, encoding="utf-8",
                       check=True, env=env)
    return r.stdout.strip()


def write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8"))


SESSIONS_V230 = '''"""Session objects."""


class Session:
    def resolve_redirects(self, resp):
        return resp.headers.get("location")
'''

SESSIONS_V231 = '''"""Session objects."""


class Session:
    def resolve_redirects(self, resp, max_redirects=30):
        hops = 0
        while resp.is_redirect and hops < max_redirects:
            hops += 1
        return resp.headers.get("location")
'''


def requests_repo(root: Path) -> dict:
    """Build the fixture; returns the SHAs of every named point."""
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q", "-b", "main")
    write(root / "requests" / "__init__.py", '__version__ = "2.30.0"\n')
    write(root / "requests" / "sessions.py", SESSIONS_V230)
    write(root / "docs" / "index.rst", "Requests\n========\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "release 2.30.0", date="2023-05-03T12:00:00Z")
    git(root, "tag", "-a", "v2.30.0", "-m", "v2.30.0")
    s230 = git(root, "rev-parse", "HEAD")
    # a branch named like a tag: branch v1 and tag v1 point at different commits
    git(root, "branch", "v1")
    write(root / "requests" / "__init__.py", '__version__ = "2.31.0"\n')
    write(root / "requests" / "sessions.py", SESSIONS_V231)
    git(root, "add", "-A")
    git(root, "commit", "-qm", "release 2.31.0", date="2023-05-22T12:00:00Z")
    git(root, "tag", "-a", "v2.31.0", "-m", "v2.31.0")
    s231 = git(root, "rev-parse", "HEAD")
    git(root, "tag", "v1")  # lightweight tag v1 at 2.31.0; branch v1 stays at 2.30.0
    git(root, "branch", "release/2.x")
    # pull request 6963: a side commit reachable only through refs/pull/6963/head
    tree = git(root, "rev-parse", "HEAD^{tree}")
    pr = git(root, "commit-tree", tree, "-p", s231, "-m", "PR 6963: tweak redirects", date="2023-06-01T12:00:00Z")
    git(root, "update-ref", "refs/pull/6963/head", pr)
    pr7_old = git(root, "commit-tree", tree, "-p", s231, "-m", "PR 7 first push", date="2023-06-02T12:00:00Z")
    pr7 = git(root, "commit-tree", tree, "-p", s231, "-m", "PR 7 force-pushed", date="2023-06-03T12:00:00Z")
    git(root, "update-ref", "refs/pull/7/head", pr7)
    git(root, "update-ref", "refs/merge-requests/3/head", pr)
    # src-layout move, released as 2.32.0, then more work on main
    git(root, "mv", "requests", "src_tmp")
    (root / "src").mkdir(exist_ok=True)
    git(root, "mv", "src_tmp", "src/requests")
    write(root / "src" / "requests" / "__init__.py", '__version__ = "2.32.0"\n')
    git(root, "add", "-A")
    git(root, "commit", "-qm", "move to src layout; release 2.32.0", date="2024-05-20T12:00:00Z")
    git(root, "tag", "v2.32.0")
    s232 = git(root, "rev-parse", "HEAD")
    write(root / "src" / "requests" / "sessions.py", SESSIONS_V231 + "\n\ndef merge_setting(a, b):\n    return b or a\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "main: merge_setting", date="2024-09-01T12:00:00Z")
    main = git(root, "rev-parse", "HEAD")
    git(root, "tag", "v2.33.0rc1")
    return {"v2.30.0": s230, "v2.31.0": s231, "v2.32.0": s232, "main": main, "branch_v1": s230, "tag_v1": s231,
            "pr6963": pr, "pr7": pr7, "pr7_old": pr7_old, "release/2.x": s231}


def analysed_project(root: Path, *, python: str = "3.12", requests_version: str = "2.31.0") -> Path:
    """A local project that locks and installs requests (no code is ever run)."""
    root.mkdir(parents=True, exist_ok=True)
    write(root / "pyproject.toml", '[project]\nname = "app"\nversion = "0.1"\nrequires-python = ">=3.11"\n'
                                   'dependencies = ["requests>=2.30", "toml"]\n')
    write(root / "uv.lock", f'''version = 1
requires-python = ">=3.11"

[[package]]
name = "app"
version = "0.1"
source = {{ virtual = "." }}

[[package]]
name = "requests"
version = "{requests_version}"
source = {{ registry = "https://pypi.org/simple" }}
sdist = {{ url = "https://files.pythonhosted.org/requests-{requests_version}.tar.gz", hash = "sha256:00", size = 1, upload-time = "2023-05-22T15:12:44Z" }}

[[package]]
name = "toml"
version = "0.10.2"
source = {{ git = "https://github.com/uiri/toml?rev=0.10.2#3f637dba5f68db63d4b30967fedda51c82459471" }}
''')
    write(root / ".python-version", f"{python}\n")
    venv = root / ".venv"
    write(venv / "pyvenv.cfg", f"home = C:\\Python\nversion = {python}.1\n")
    site = venv / "Lib" / "site-packages"
    write(site / f"requests-{requests_version}.dist-info" / "METADATA",
          f"Metadata-Version: 2.1\nName: requests\nVersion: {requests_version}\nSummary: HTTP\n")
    write(site / f"requests-{requests_version}.dist-info" / "RECORD", "")
    write(site / "toml-0.10.2.dist-info" / "METADATA", "Metadata-Version: 2.1\nName: toml\nVersion: 0.10.2\n")
    write(site / "toml-0.10.2.dist-info" / "RECORD", "")
    write(site / "toml-0.10.2.dist-info" / "direct_url.json",
          '{"url": "https://github.com/uiri/toml", "vcs_info": {"vcs": "git", "requested_revision": "0.10.2", '
          '"commit_id": "3f637dba5f68db63d4b30967fedda51c82459471"}}')
    git(root, "init", "-q", "-b", "main")
    write(root / "app.py", "import requests\n\n\ndef fetch(url):\n    return requests.get(url)\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "app")
    return root
