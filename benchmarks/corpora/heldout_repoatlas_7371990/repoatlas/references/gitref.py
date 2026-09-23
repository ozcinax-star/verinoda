"""Git access for reference resolution: refs, qualified names, trees (docs/DESIGN.md D10/D11).

:class:`GitRunner` answers the questions pinning needs, with as little network
as possible:

* :meth:`refs` - every branch, tag (peeled to its commit), pull/merge-request
  head and the default branch of a repository: one ``git ls-remote --symref``
  per repository and resolution. Local repositories (paths, ``file://`` URLs,
  ``remote_map`` entries used by tests) never touch the network. With network
  mode ``off`` a cached mirror under ``.repoatlas/research/<slug>/`` answers
  "as last fetched" or the call reports ``offline``.
* :meth:`resolve_name` - a *qualified* lookup (``refs/heads/X`` and
  ``refs/tags/X``) in the order the reference implies; when both exist the
  result says so (mismatch M5) instead of letting git silently prefer the tag.
* :meth:`objects` - a git directory that holds the commits and trees
  (the local repository, an existing research mirror, or a blobless bare
  mirror ``meta.git`` created on demand: ``git clone --bare --filter=blob:none``)
  for path checks (M1b), same-name suggestions, dates (``rev-list --before``),
  ancestry (M6) and merge bases.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from repoatlas.references.classify import git_slug

GIT_NET_TIMEOUT = 300
GIT_LOCAL_TIMEOUT = 60


def run_git(args: list[str], *, cwd: Path | None = None, git_dir: Path | None = None,
            timeout: float = GIT_LOCAL_TIMEOUT) -> tuple[int, str, str]:
    cmd = ["git"]
    if git_dir is not None:
        cmd += ["--git-dir", str(git_dir)]
    elif cwd is not None:
        cmd += ["-C", str(cwd)]
    cmd += ["-c", "core.longpaths=true", "-c", "core.autocrlf=false", *args]
    env = dict(os.environ)
    env.update({"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never", "GIT_ASKPASS": "",
                "SSH_ASKPASS": ""})
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, env=env, stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        return 127, "", "git executable not found on PATH"
    except subprocess.TimeoutExpired:
        return 124, "", f"git {args[0] if args else ''} timed out after {timeout:.0f}s"
    except OSError as exc:
        return 126, "", f"could not run git: {exc}"
    return r.returncode, r.stdout, r.stderr


def parse_ls_remote(out: str) -> dict:
    """``git ls-remote --symref`` output -> ``{heads, tags, pulls, mrs, head_ref, head_sha, other}``."""
    heads: dict[str, str] = {}
    tags: dict[str, str] = {}
    tag_objects: dict[str, str] = {}
    pulls: dict[int, str] = {}
    mrs: dict[int, str] = {}
    other: dict[str, str] = {}
    head_ref = head_sha = None
    for line in out.splitlines():
        if line.startswith("ref: "):
            target, _, name = line[5:].partition("\t")
            if name.strip() == "HEAD":
                head_ref = target.strip()
            continue
        sha, _, name = line.partition("\t")
        name = name.strip()
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            continue
        if name == "HEAD":
            head_sha = sha
        elif name.startswith("refs/heads/"):
            heads[name[11:]] = sha
        elif name.startswith("refs/tags/"):
            t = name[10:]
            if t.endswith("^{}"):
                tags[t[:-3]] = sha  # peeled commit of an annotated tag
            else:
                tag_objects[t] = sha
                tags.setdefault(t, sha)
        elif m := re.fullmatch(r"refs/pull/(\d+)/head", name):
            pulls[int(m.group(1))] = sha
        elif m := re.fullmatch(r"refs/merge-requests/(\d+)/head", name):
            mrs[int(m.group(1))] = sha
        else:
            other[name] = sha
    return {"heads": heads, "tags": tags, "tag_objects": tag_objects, "pulls": pulls, "mrs": mrs,
            "head_ref": head_ref, "head_sha": head_sha, "other": other}


class GitRunner:
    """Refs and objects of reference repositories.

    ``remote_map`` maps canonical URLs (``https://github.com/o/r``) to local
    repositories - tests use it to stand in for hosted repositories without a
    network. ``cache_root`` is ``.repoatlas/research`` of the analysed project.
    """

    def __init__(self, *, network: str = "cache", cache_root: Path | None = None,
                 remote_map: dict | None = None):
        self.network = network
        self.cache_root = Path(cache_root) if cache_root else None
        self.remote_map = {self._canon(k): Path(v) for k, v in (remote_map or {}).items()}
        self._refs: dict[str, dict] = {}
        self._objects: dict[str, Path | None] = {}
        self.network_calls = 0
        self.log: list[str] = []

    # -- locations -------------------------------------------------------------------------------------
    @staticmethod
    def _canon(url: str) -> str:
        return re.sub(r"\.git$", "", str(url).rstrip("/")).lower()

    def local_path(self, url: str) -> Path | None:
        """A local repository for ``url`` (mapped, a plain path or a ``file://`` URL), else None."""
        mapped = self.remote_map.get(self._canon(url))
        if mapped is not None:
            return mapped
        if url.startswith("file://"):
            from urllib.parse import unquote, urlparse

            p = unquote(urlparse(url).path)
            if re.match(r"^/[A-Za-z]:/", p):
                p = p[1:]
            return Path(p) if Path(p).exists() else None
        if "://" not in url and not re.match(r"^[\w.-]+@[\w.-]+:", url):
            p = Path(url)
            return p if p.exists() else None
        return None

    def cached_mirrors(self, url: str) -> list[Path]:
        if self.cache_root is None:
            return []
        d = self.cache_root / git_slug(url)
        return [m for m in (d / "mirror.git", d / "meta.git") if (m / "HEAD").is_file()]

    # -- refs --------------------------------------------------------------------------------------------
    def refs(self, url: str) -> dict:
        """``{ok, heads, tags, pulls, mrs, default_branch, head_sha, source, as_of, error}`` for a repository."""
        k = self._canon(url)
        if k in self._refs:
            return self._refs[k]
        local = self.local_path(url)
        if local is not None:
            rc, out, err = run_git(["ls-remote", "--symref", str(local)], timeout=GIT_LOCAL_TIMEOUT)
            res = self._refset(rc, out, err, source="local")
        elif self.network == "off":
            mirrors = self.cached_mirrors(url)
            if mirrors:
                rc, out, err = run_git(["ls-remote", "--symref", str(mirrors[0])])
                res = self._refset(rc, out, err, source="cached_mirror")
                res["as_of"] = "last fetch of the cached mirror (network off)"
            else:
                res = {"ok": False, "error": "network is off and no cached mirror of this repository exists",
                       "reason": "offline", "source": None}
        else:
            self.network_calls += 1
            self.log.append(f"ls-remote {url}")
            rc, out, err = run_git(["ls-remote", "--symref", url, "HEAD", "refs/heads/*", "refs/tags/*"],
                                   timeout=GIT_NET_TIMEOUT)
            res = self._refset(rc, out, err, source="ls-remote")
            if not res["ok"]:
                mirrors = self.cached_mirrors(url)
                if mirrors:
                    rc2, out2, err2 = run_git(["ls-remote", "--symref", str(mirrors[0])])
                    cached = self._refset(rc2, out2, err2, source="cached_mirror")
                    if cached["ok"]:
                        cached["as_of"] = "last fetch of the cached mirror (ls-remote failed)"
                        cached["warning"] = res.get("error")
                        res = cached
        self._refs[k] = res
        return res

    @staticmethod
    def _refset(rc: int, out: str, err: str, *, source: str) -> dict:
        if rc != 0:
            e = (err or "").strip().splitlines()
            msg = e[-1][:300] if e else f"git ls-remote failed ({rc})"
            reason = "not_found" if re.search(r"not found|does not exist|Repository not found", err or "", re.I) \
                else ("auth_required" if re.search(r"Authentication|could not read Username|403", err or "", re.I)
                      else "unreachable")
            return {"ok": False, "error": msg, "reason": reason, "source": source}
        p = parse_ls_remote(out)
        default = p["head_ref"][11:] if p["head_ref"] and p["head_ref"].startswith("refs/heads/") else None
        head_sha = p["head_sha"] or (p["heads"].get(default) if default else None)
        return {"ok": True, **p, "default_branch": default, "head_sha": head_sha, "source": source, "as_of": "now"}

    def pull_head(self, url: str, number: int, *, gitlab: bool = False) -> str | None:
        """The head commit of PR/MR ``number`` (``refs/pull/N/head`` / ``refs/merge-requests/N/head``)."""
        rs = self.refs(url)
        table = rs.get("mrs" if gitlab else "pulls") or {}
        if number in table:
            return table[number]
        ref = f"refs/merge-requests/{number}/head" if gitlab else f"refs/pull/{number}/head"
        local = self.local_path(url)
        if local is not None:
            rc, out, _ = run_git(["ls-remote", str(local), ref])
        elif self.network != "off":
            self.network_calls += 1
            self.log.append(f"ls-remote {url} {ref}")
            rc, out, _ = run_git(["ls-remote", url, ref], timeout=GIT_NET_TIMEOUT)
        else:
            mirrors = self.cached_mirrors(url)
            if not mirrors:
                return None
            rc, out, _ = run_git(["ls-remote", str(mirrors[0]), ref])
        if rc == 0 and out.strip():
            sha = out.split()[0]
            table[number] = sha
            return sha
        return None

    def resolve_name(self, refset: dict, name: str, hint: str = "none") -> dict:
        """Qualified lookup of a branch/tag name.

        -> ``{found, sha, kind, qualified, collision, other_sha}``. ``hint``
        ``heads_first`` prefers the branch (GitHub web semantics for tree/blob/
        compare URLs), ``tags`` and ``none`` prefer the tag (git's own order).
        """
        heads, tags = refset.get("heads") or {}, refset.get("tags") or {}
        br, tg = heads.get(name), tags.get(name)
        out = {"found": bool(br or tg), "sha": None, "kind": None, "qualified": None, "collision": bool(br and tg),
               "other_sha": None, "other_kind": None}
        order = [("branch", br, f"refs/heads/{name}"), ("tag", tg, f"refs/tags/{name}")]
        if hint != "heads_first":
            order.reverse()
        picked = [o for o in order if o[1]]
        if picked:
            out.update(kind=picked[0][0], sha=picked[0][1], qualified=picked[0][2])
            if len(picked) > 1:
                out.update(other_kind=picked[1][0], other_sha=picked[1][1])
        return out

    # -- objects -------------------------------------------------------------------------------------------
    def objects(self, url: str, *, need: list[str] | None = None) -> Path | None:
        """A git dir (or work tree) holding commits and trees of ``url``; None when unavailable."""
        k = self._canon(url)
        if k not in self._objects:
            local = self.local_path(url)
            if local is not None:
                self._objects[k] = local
            else:
                mirrors = self.cached_mirrors(url)
                if mirrors:
                    self._objects[k] = mirrors[0]
                elif self.network != "off" and self.cache_root is not None:
                    dest = self.cache_root / git_slug(url) / "meta.git"
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    self.network_calls += 1
                    self.log.append(f"clone --bare --filter=blob:none {url}")
                    rc, _, err = run_git(["clone", "--bare", "--quiet", "--filter=blob:none", url, str(dest)],
                                         timeout=GIT_NET_TIMEOUT)
                    self._objects[k] = dest if rc == 0 else None
                    if rc != 0:
                        self.log.append(f"blobless clone failed: {err.strip()[-200:]}")
                else:
                    self._objects[k] = None
        gd = self._objects[k]
        if gd is not None and need:
            missing = [s for s in need if s and not self.has_commit(gd, s)]
            if missing and self.network != "off" and self.local_path(url) is None:
                self.network_calls += 1
                run_git(["fetch", "--quiet", "--filter=blob:none", "origin", *missing], git_dir=self._gd(gd),
                        timeout=GIT_NET_TIMEOUT)
        return gd

    @staticmethod
    def _gd(p: Path) -> Path:
        return p / ".git" if (p / ".git").exists() else p

    def has_commit(self, gd: Path, sha: str) -> bool:
        return run_git(["cat-file", "-e", f"{sha}^{{commit}}"], git_dir=self._gd(gd))[0] == 0

    def commit_of(self, gd: Path, rev: str) -> str | None:
        rc, out, _ = run_git(["rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"], git_dir=self._gd(gd))
        return out.strip() if rc == 0 and out.strip() else None

    def path_exists(self, gd: Path, sha: str, path: str) -> bool | None:
        """True/False when the commit is available; None when it cannot be checked."""
        if not self.has_commit(gd, sha):
            return None
        return run_git(["cat-file", "-e", f"{sha}:{path}"], git_dir=self._gd(gd))[0] == 0

    def blob_id(self, gd: Path, sha: str, path: str) -> str | None:
        rc, out, _ = run_git(["rev-parse", "--verify", "--quiet", f"{sha}:{path}"], git_dir=self._gd(gd))
        return out.strip() if rc == 0 and out.strip() else None

    def same_basename(self, gd: Path, sha: str, path: str, limit: int = 5) -> list[str]:
        base = path.rsplit("/", 1)[-1]
        rc, out, _ = run_git(["ls-tree", "-r", "--name-only", sha], git_dir=self._gd(gd))
        if rc != 0:
            return []
        hits = [p for p in out.splitlines() if p.rsplit("/", 1)[-1] == base]
        tail = path.split("/")
        hits.sort(key=lambda p: -sum(1 for a, b in zip(reversed(p.split("/")), reversed(tail)) if a == b))
        return hits[:limit]

    def rev_before(self, gd: Path, date: str, rev: str) -> str | None:
        rc, out, _ = run_git(["rev-list", "-1", "--first-parent", f"--before={date}", rev], git_dir=self._gd(gd))
        return out.strip() if rc == 0 and out.strip() else None

    def commit_date(self, gd: Path, sha: str) -> str | None:
        rc, out, _ = run_git(["show", "-s", "--format=%cI", sha], git_dir=self._gd(gd))
        return out.strip() if rc == 0 and out.strip() else None

    def is_ancestor(self, gd: Path, a: str, b: str) -> bool | None:
        rc, _, _ = run_git(["merge-base", "--is-ancestor", a, b], git_dir=self._gd(gd))
        return True if rc == 0 else (False if rc == 1 else None)

    def merge_base(self, gd: Path, a: str, b: str) -> str | None:
        rc, out, _ = run_git(["merge-base", a, b], git_dir=self._gd(gd))
        return out.strip() if rc == 0 and out.strip() else None

    def changed_files(self, gd: Path, a: str, b: str, limit: int = 200) -> list[str]:
        rc, out, _ = run_git(["diff", "--name-only", f"{a}...{b}"], git_dir=self._gd(gd))
        return out.splitlines()[:limit] if rc == 0 else []

    def renamed_to(self, gd: Path, a: str, b: str, path: str) -> str | None:
        rc, out, _ = run_git(["diff", "--find-renames", "--name-status", a, b], git_dir=self._gd(gd))
        if rc != 0:
            return None
        for line in out.splitlines():
            parts = line.split("\t")
            if parts[0].startswith("R") and len(parts) == 3 and (parts[1] == path or parts[2] == path):
                return parts[2] if parts[1] == path else parts[1]
        return None
