"""Which Verinoda build is running: its version, the git commit it was built from when that is known,
and where it lives.

``verinoda --version``, ``doctor`` (text and ``--json``), the MCP ``serverInfo`` and the agent installer
all show this, so two installs that both say ``0.1.0.dev0`` can be told apart. Sources, in order; each
one only reports what it read, and the build is ``unknown`` when none of them applies:

1. **git checkout** - the folder above the package is a Verinoda source tree (``pyproject.toml`` names
   the ``verinoda`` project) with ``.git``: HEAD is read from the git files (``git rev-parse`` only
   when the files use a format this reader does not know, such as reftable). Local changes come from
   ``git status`` limited to ``verinoda/`` and ``pyproject.toml``; without git they are ``None``
   (not checked), never "clean".
2. **source archive** - ``verinoda/data/git_archival.txt`` is filled in by ``git archive`` through
   ``export-subst`` in ``.gitattributes``; GitHub's ``/archive/<ref>.zip`` downloads (what install.sh
   installs) are made that way. An unfilled file (a checkout, a local build) gives nothing.
3. **install record** - the PEP 610 ``direct_url.json`` of the installed distribution, used only when
   that distribution is the package that is running: ``vcs_info.commit_id`` for a git URL install, a
   commit named in an archive URL, otherwise the URL and the requested ref without a commit.

Nothing here runs project code: the only external command is ``git`` with fixed arguments in the
package's own source folder.
"""

from __future__ import annotations

import functools
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

PKG_DIR = Path(__file__).resolve().parent
ARCHIVAL = "data/git_archival.txt"
SHORT = 12
_SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_REF = re.compile(r"refs/[A-Za-z0-9._/+-]+")
_PROJECT_NAME = re.compile(r'^\s*name\s*=\s*["\']verinoda["\']\s*$', re.M)
_ARCHIVE_REF = re.compile(r"/archive/(?:refs/(?:heads|tags)/)?(?P<ref>.+?)\.(?:zip|tar\.gz|tgz)$")
_GIT_TIMEOUT = 10


def _version() -> str:
    from verinoda import __version__

    return __version__


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def is_source_root(root: Path) -> bool:
    """``root`` holds Verinoda's own ``pyproject.toml`` (not some other project that vendors the package)."""
    text = _read(root / "pyproject.toml")
    return bool(text and _PROJECT_NAME.search(text))


def _git_dirs(root: Path) -> tuple[Path, Path] | None:
    """(git dir, common dir) of the work tree at ``root``, read from ``.git`` (a folder, or a file for a
    linked worktree or a submodule)."""
    dot = root / ".git"
    if dot.is_dir():
        gd = dot
    elif dot.is_file():
        text = (_read(dot) or "").strip()
        if not text.startswith("gitdir:"):
            return None
        gd = Path(text[len("gitdir:"):].strip())
        if not gd.is_absolute():
            gd = root / gd
    else:
        return None
    common = gd
    cd = _read(gd / "commondir")
    if cd and cd.strip():
        c = Path(cd.strip())
        common = c if c.is_absolute() else gd / c
    return gd, common


def read_head(root: Path) -> tuple[str | None, str | None]:
    """(commit, branch) of the checkout at ``root`` from its git files; ``(None, None)`` when unreadable.

    A detached HEAD has no branch. An unborn branch, or refs in a format this reader does not parse,
    gives ``(None, branch)``."""
    dirs = _git_dirs(root)
    if dirs is None:
        return None, None
    gd, common = dirs
    head = (_read(gd / "HEAD") or "").strip()
    if _SHA.fullmatch(head):
        return head, None
    if not head.startswith("ref:"):
        return None, None
    ref = head[4:].strip()
    if not _REF.fullmatch(ref) or ".." in ref:
        return None, None
    branch = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref
    for base in (gd, common):
        sha = (_read(base / ref) or "").strip()
        if _SHA.fullmatch(sha):
            return sha, branch
    for line in (_read(common / "packed-refs") or "").splitlines():
        if not line or line[0] in "#^":
            continue
        sha, _, name = line.partition(" ")
        if name.strip() == ref and _SHA.fullmatch(sha):
            return sha, branch
    return None, branch


def _git(root: Path, *args: str) -> subprocess.CompletedProcess | None:
    """``git`` with fixed arguments in the package's own source folder; None when it cannot run."""
    exe = shutil.which("git")
    if not exe:
        return None
    try:
        return subprocess.run([exe, "--no-optional-locks", *args], cwd=root, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=_GIT_TIMEOUT, stdin=subprocess.DEVNULL,
                              check=False)
    except (OSError, subprocess.SubprocessError):
        return None


def local_changes(root: Path) -> bool | None:
    """Uncommitted changes to the package or pyproject.toml in the checkout; None when git could not tell."""
    r = _git(root, "status", "--porcelain", "--", "verinoda", "pyproject.toml")
    if r is None or r.returncode != 0:
        return None
    return bool(r.stdout.strip())


def read_archival(pkg_dir: Path) -> dict | None:
    """``{"commit", "date"}`` from a ``git archive`` export of the package; None when the file was not
    filled in (it still holds the ``$Format`` placeholders) or is missing."""
    text = _read(pkg_dir / ARCHIVAL)
    if not text:
        return None
    vals = {}
    for line in text.splitlines():
        key, sep, val = line.partition(":")
        if sep and not line.startswith("#"):
            vals[key.strip()] = val.strip()
    commit = vals.get("commit", "")
    if not _SHA.fullmatch(commit):
        return None
    return {"commit": commit, "date": vals.get("date") or None}


def _file_url_path(url: str | None) -> Path | None:
    if not url or not url.startswith("file:"):
        return None
    p = unquote(urlparse(url).path)
    if os.name == "nt" and re.match(r"^/[A-Za-z]:", p):
        p = p[1:]
    return Path(p) if p else None


def _same(a: Path, b: Path) -> bool:
    def norm(p: Path) -> str:
        try:
            p = p.resolve()
        except OSError:
            pass
        return os.path.normcase(os.path.normpath(str(p)))

    return norm(a) == norm(b)


def installed_record(pkg_dir: Path) -> tuple[dict | None, str]:
    """(direct_url.json, note) of the installed ``verinoda`` distribution when it is the running package.

    Metadata of *another* install on the same path (for example a dev run through PYTHONPATH while
    the interpreter has an editable install elsewhere) is not used: the note says so."""
    try:
        from importlib.metadata import distribution

        dist = distribution("verinoda")
    except Exception:  # noqa: BLE001 - no metadata at all: a plain source tree
        return None, "no installed distribution"
    try:
        raw = dist.read_text("direct_url.json")
        data = json.loads(raw) if raw else None
    except (OSError, ValueError):
        data = None
    try:
        owns = _same(Path(str(dist.locate_file("verinoda/__init__.py"))), pkg_dir / "__init__.py")
    except Exception:  # noqa: BLE001
        owns = False
    if not owns and isinstance(data, dict) and (data.get("dir_info") or {}).get("editable"):
        src = _file_url_path(data.get("url"))
        owns = src is not None and _same(src / "verinoda", pkg_dir)
    if not owns:
        return None, "the installed distribution's metadata describes another copy of the package"
    if not isinstance(data, dict):
        return None, "installed from an index or a wheel without an install record (direct_url.json)"
    return data, ""


def _from_record(data: dict) -> dict:
    """What a PEP 610 record says about the build: commit (when recorded), source kind, origin, ref."""
    url = str(data.get("url") or "")
    out: dict = {"commit": None, "origin": url or None, "ref": None}
    vcs = data.get("vcs_info")
    if isinstance(vcs, dict):
        cid = str(vcs.get("commit_id") or "")
        out.update(source="git URL", commit=cid if _SHA.fullmatch(cid) else None,
                   ref=vcs.get("requested_revision") or None)
        return out
    arch = data.get("archive_info")
    if isinstance(arch, dict):
        m = _ARCHIVE_REF.search(urlparse(url).path) if url else None
        ref = unquote(m.group("ref")) if m else None
        out.update(source="archive URL", ref=ref)
        if ref and _SHA.fullmatch(ref):
            out["commit"] = ref
        hashes = arch.get("hashes") if isinstance(arch.get("hashes"), dict) else {}
        h = hashes.get("sha256") or (str(arch.get("hash") or "").partition("=")[2] if arch.get("hash") else "")
        if h:
            out["archive_sha256"] = h
        return out
    dir_info = data.get("dir_info")
    if isinstance(dir_info, dict):
        out["source"] = "editable folder" if dir_info.get("editable") else "local folder"
        return out
    out["source"] = "unknown"
    return out


def collect(pkg_dir: Path = PKG_DIR, *, check_changes: bool = True, record=None) -> dict:
    """The build description for the package at ``pkg_dir`` (see the module docstring).

    ``record`` replaces :func:`installed_record` (tests). ``check_changes=False`` skips ``git status``
    (``local_changes`` is then None: not checked)."""
    pkg_dir = Path(pkg_dir)
    info: dict = {"version": _version(), "commit": None, "branch": None, "ref": None, "source": "unknown",
                  "origin": None, "local_changes": None, "date": None, "evidence": [],
                  "package": str(pkg_dir), "python": sys.executable}
    root = pkg_dir.parent
    if (root / ".git").exists() and is_source_root(root):
        commit, branch = read_head(root)
        how = f"read HEAD from {root / '.git'}"
        if commit is None:
            r = _git(root, "rev-parse", "--verify", "HEAD")
            sha = (r.stdout.strip() if r is not None and r.returncode == 0 else "")
            if _SHA.fullmatch(sha):
                commit, how = sha, f"git rev-parse HEAD in {root}"
        info.update(source="git checkout", origin=str(root), commit=commit, branch=branch)
        info["evidence"].append(how if commit else f"{root / '.git'}: HEAD could not be read")
        if commit and check_changes:
            info["local_changes"] = local_changes(root)
            info["evidence"].append("git status on verinoda/ and pyproject.toml" if info["local_changes"] is not None
                                    else "local changes not checked (git could not run)")
        return _finish(info)
    arch = read_archival(pkg_dir)
    data, note = record(pkg_dir) if record is not None else installed_record(pkg_dir)
    if data is not None:
        rec = _from_record(data)
        info.update({k: v for k, v in rec.items() if v is not None})
        info["evidence"].append("install record (direct_url.json)")
    elif note:
        info["evidence"].append(note)
    if arch:
        if info["commit"] and info["commit"] != arch["commit"]:  # pragma: no cover - contradictory records
            info["evidence"].append(f"direct_url.json names {info['commit'][:SHORT]}, the archive stamp "
                                    f"{arch['commit'][:SHORT]}; the archive stamp is the built code's own")
        info.update(commit=arch["commit"], date=arch["date"])
        if info["source"] == "unknown":
            info["source"] = "source archive"
        info["evidence"].append(f"{ARCHIVAL} filled in by git archive")
    return _finish(info)


def _finish(info: dict) -> dict:
    c = info["commit"]
    info["build"] = (c[:SHORT] + ("+local-changes" if info["local_changes"] else "")) if c else "unknown"
    return info


@functools.lru_cache(maxsize=1)
def build_info() -> dict:
    """The running build (computed once per process)."""
    return collect(PKG_DIR)


def describe(info: dict) -> str:
    """One line: ``commit 54186f5c1a2b, git checkout, branch main, no local changes``."""
    parts = []
    if info.get("commit"):
        parts.append(f"commit {info['commit'][:SHORT]}")
    else:
        parts.append("build unknown")
    src = info.get("source") or "unknown"
    where = {"git checkout": "git checkout", "source archive": "source archive", "git URL": "installed from git",
             "archive URL": "installed from an archive", "editable folder": "editable install",
             "local folder": "installed from a local folder"}.get(src)
    if where:
        parts.append(where)
        if info.get("branch"):
            parts.append(f"branch {info['branch']}")
        elif info.get("ref") and info.get("ref") != info.get("commit"):
            parts.append(f"ref {info['ref']}")
    if src == "git checkout" and info.get("commit"):
        lc = info.get("local_changes")
        parts.append("local changes not checked" if lc is None else "with local changes" if lc
                     else "no local changes")
    elif not info.get("commit") and src in ("archive URL", "local folder", "editable folder"):
        parts.append("the commit was not recorded")
    return ", ".join(parts)


def version_line(info: dict | None = None) -> str:
    """What ``verinoda --version`` prints."""
    info = info or build_info()
    return f"verinoda {info['version']} ({describe(info)})"


def server_version(info: dict | None = None) -> str:
    """The MCP ``serverInfo.version``: a PEP 440 local version such as ``0.1.0.dev0+54186f5c1a2b``
    (``.local.changes`` with local changes, ``+unknown`` when the commit is not known)."""
    info = info or build_info()
    c = info.get("commit")
    local = (c[:SHORT] + (".local.changes" if info.get("local_changes") else "")) if c else "unknown"
    return f"{info['version']}+{local}"
