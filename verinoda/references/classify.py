"""Table-driven classification of a reference (docs/DESIGN.md D10, research-intent-refs P0).

:func:`classify` turns one reference string - a URL, ``owner/repo[@ref|#N]``,
a package spec (``pkg==1.2``, ``name@^1``, purl, ``module@v1.2.3``, Maven
``g:a:v``), an arXiv id, a DOI, a SWHID, a local path, an scp-style git remote -
or one extracted mention into a *reference spec*: its ``class``, its identity
(host, owner, repo, canonical URL, package, paper) and what the reference
itself asks for (``requested``: the ref named in the URL and its likely
namespace, a compare range, a PR/issue number, the file path and the
``#Lx-Ly`` lines, a version, a docs version slot).

Nothing here touches the network or git; resolution happens in
:mod:`verinoda.references.resolver`. Classes that name a version by
construction (compare, archive, release, PR, MR) set ``requires_ref``: when
the ref cannot be read from the reference they are refused, never pinned to
the default branch.

``ref_namespace_hint`` records which namespace a bare name belongs to when a
branch and a tag share it: ``heads_first`` for ``/tree``, ``/blob`` and
``/compare`` (GitHub: "If a branch and a tag have the same name, the branch is
used"), ``tags`` for ``/releases/tag`` and ``/archive/refs/tags``.

:func:`to_legacy` maps a spec to the dict :func:`verinoda.research.parse_reference`
has always returned (``type``/``url``/``ref``/``ref_candidates``/``subpath``/``slug``/...).
"""

from __future__ import annotations

import hashlib
import re
import urllib.parse
from pathlib import Path

GIT_CLASSES = frozenset({"git_repo", "git_file", "git_commit", "git_compare", "git_archive", "pull_request",
                         "merge_request", "release", "release_latest"})
REF_REQUIRED = frozenset({"git_compare", "git_archive", "pull_request", "merge_request", "release"})
DOC_CLASSES = frozenset({"paper", "doi", "doc_page", "swh_object", "qa_post", "web_page", "issue"})
CLASSES = GIT_CLASSES | DOC_CLASSES | frozenset({"package", "runtime", "application", "local_file",
                                                 "local_symbol"})
DOC_KINDS = ("official_doc", "standard", "paper", "secondary")
KINDS = ("auto", "reference_repo", *DOC_KINDS)

GIT_HOSTS = {"github.com", "gitlab.com", "bitbucket.org", "codeberg.org", "git.sr.ht", "gitea.com",
             "salsa.debian.org", "gitee.com"}
_GITEA_HOSTS = {"codeberg.org", "gitea.com"}
_NON_CODE_PAGES = {"pulls", "discussions", "wiki", "actions", "security", "pulse", "graphs", "projects",
                   "milestones", "labels", "network", "stargazers", "watchers", "forks", "settings", "contributors",
                   "community", "packages", "deployments", "activity"}
_SCP_RE = re.compile(r"^(?P<user>[\w.-]+)@(?P<host>[\w.-]+):(?!//)(?P<path>.+)$")
SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
FULL_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_LINES_RE = re.compile(r"^L(\d+)(?:C\d+)?(?:-L?(\d+)(?:C\d+)?)?$")
_BB_LINES_RE = re.compile(r"^lines-(\d+)(?::(\d+))?$")
_ARCHIVE_EXT = re.compile(r"\.(?:tar\.gz|tar\.bz2|tar\.xz|tgz|zip|tar)$")
SWHID_RE = re.compile(r"^swh:1:(cnt|dir|rev|rel|snp):([0-9a-f]{40})((?:;[^;\s]+)*)$")
ARXIV_RE = re.compile(r"^(?:arxiv:\s?)?(\d{4}\.\d{4,5}|[a-z-]+(?:\.[a-z]{2})?/\d{7})(v\d+)?$", re.I)
DOI_RE = re.compile(r"^(?:doi:\s?|https?://(?:dx\.)?doi\.org/)?(10\.\d{4,9}/[-._;()/:a-z0-9]+)$", re.I)
PURL_RE = re.compile(r"^pkg:([a-z][a-z0-9.+-]*)/([^@?#]+?)(?:@([^?#]+))?(?:\?([^#]*))?(?:#(.*))?$", re.I)
GOMOD_RE = re.compile(r"^((?:[a-z0-9-]+\.)+[a-z]{2,}(?:/[\w.~-]+)+)@(v\d+\.\d+\.\d+[\w.+-]*)$")
GO_PSEUDO_RE = re.compile(r"^v\d+\.\d+\.\d+-(?:[\w.]+\.)?(?:0\.)?(\d{14})-([0-9a-f]{12})$")
MAVEN_RE = re.compile(r"^([a-z][\w-]*(?:\.[\w-]+)+):([A-Za-z][\w.-]*):(\d[\w.-]*)$")
PIP_RE = re.compile(r"^((?:@[a-z0-9][\w.-]*/)?[A-Za-z0-9][A-Za-z0-9._-]*)(\[[\w,.-]*\])?\s*"
                    r"((?:===|==|~=|!=|>=|<=|<|>)\s*v?\d[\w.*+-]*(?:\s*,\s*(?:===|==|~=|!=|>=|<=|<|>)\s*\d[\w.*+-]*)*)$")
AT_RE = re.compile(r"^((?:@[a-z0-9][\w.-]*/)?[a-z0-9][a-z0-9._-]*)@((?:\^|~|>=|<=|=)?v?\d+(?:\.\d+){0,2}[\w.+-]*)$",
                   re.I)
SLUG_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9-]{0,38})/([A-Za-z0-9._-]{1,100}?)(?:\.git)?"
                     r"(?:@(?P<ref>[\w./+-]+)|(?P<sep>[#!])(?P<num>\d{1,7}))?$")
_DOCS_FLOATING = {"latest", "stable", "dev", "devel", "main", "master", "current", "nightly", "trunk", "3", "2", ""}
_VERSION_SLOT = re.compile(r"^v?\d+(?:\.\d+){0,3}(?:[-.]?(?:rc|a|b|beta|alpha)\d*)?(?:\.x)?$", re.I)


def slugify(text: str, n: int = 40) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-.")
    return (s or "ref")[:n]


def h6(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:6]


def git_slug(url: str) -> str:
    """Directory name for a repository under ``.verinoda/research`` (same rule research has always used)."""
    clean = re.sub(r"^[A-Za-z][A-Za-z0-9+.-]*://[^/]+/", "", url)
    clean = re.sub(r"^[\w.-]+@[\w.-]+:", "", clean)
    segs = [x for x in re.sub(r"\.git$", "", clean).split("/") if x]
    host = (urllib.parse.urlparse(url).hostname or "") if "://" in url else ""
    return f"{slugify('-'.join(segs[-2:]) or host)}-{h6(url.lower())}"


def _empty(cls: str, s: str) -> dict:
    return {
        "class": cls, "input": s,
        "identity": {"host": None, "owner": None, "repo": None, "canonical_url": None, "clone_url": None,
                     "host_assumed": False, "package": None, "paper": None, "app": None, "local_path": None,
                     "swhid": None, "docs": None, "source_availability": None},
        "requested": {"url": None, "url_ref": None, "url_ref_kind": None, "ref_namespace_hint": "none",
                      "ref_candidates": [], "compare": None, "path": None, "lines": None, "number": None,
                      "commit": None, "version_text": None, "version_exact": None, "version_spec": None,
                      "docs_version": None, "floating_slot": False, "plain": False},
        "requires_ref": cls in REF_REQUIRED, "immutable": False, "floating": False, "notes": [],
        "source_type": None,
    }


def _repo_identity(spec: dict, host: str, owner: str, repo_path: str, *, scheme: str = "https",
                   clone_url: str | None = None, assumed: bool = False) -> None:
    repo_path = re.sub(r"\.git$", "", repo_path.strip("/"))
    canon = f"https://{host}/{repo_path}"
    ident = spec["identity"]
    ident.update(host=host, owner=owner, repo=repo_path.split("/")[-1], canonical_url=canon,
                 clone_url=clone_url or f"{scheme}://{host}/{repo_path}", host_assumed=assumed)


def _lines(fragment: str) -> list[int] | None:
    m = _LINES_RE.match(fragment or "")
    if m:
        a = int(m.group(1))
        return [a, int(m.group(2) or a)]
    m = _BB_LINES_RE.match(fragment or "")
    if m:
        a = int(m.group(1))
        return [a, int(m.group(2) or a)]
    return None


def _ref_splits(tail: list[str], limit: int = 5) -> list[list]:
    out = []
    for n in range(1, min(len(tail), limit) + 1):
        out.append(["/".join(tail[:n]), "/".join(tail[n:]) or None])
    return out


def _set_ref(spec: dict, candidates: list[list], *, hint: str = "heads_first", kind: str | None = None) -> None:
    req = spec["requested"]
    req["ref_candidates"] = candidates
    if candidates:
        req["url_ref"], req["path"] = candidates[0][0], candidates[0][1]
        ref = candidates[0][0]
        if kind is None:
            kind = "commit" if FULL_SHA_RE.match(ref) or (SHA_RE.match(ref) and not ref.isdigit()
                                                          and re.search(r"\d", ref) and re.search(r"[a-f]", ref, re.I)
                                                          and len(candidates) == 1) else "unknown"
        req["url_ref_kind"] = kind
        if kind == "commit":
            spec["immutable"] = True
            req["commit"] = ref
    req["ref_namespace_hint"] = hint


def _compare(spec: dict, text: str) -> None:
    text = urllib.parse.unquote(text)
    three = "..." in text
    base, _, head = text.partition("..." if three else "..")
    if not (base and head):
        spec["notes"].append("compare URL without a base...head range")
        return

    def side(x: str) -> tuple[str | None, str]:
        if ":" in x:  # owner:branch (cross-fork)
            o, _, b = x.partition(":")
            return o, b
        return None, x

    bo, b = side(base)
    ho, h = side(head)
    spec["requested"]["compare"] = {"base": b, "head": h, "three_dot": three, "base_owner": bo, "head_owner": ho}
    _set_ref(spec, [[h, None]], hint="heads_first")


# -- host handlers ---------------------------------------------------------------------------------------

def _github(spec: dict, host: str, segs: list[str], frag: str, query: str) -> dict:
    owner, repo = segs[0], re.sub(r"\.git$", "", segs[1])
    _repo_identity(spec, "github.com" if host.endswith("github.com") else host, owner, f"{owner}/{repo}")
    rest = segs[2:]
    req = spec["requested"]
    if not rest:
        return spec
    action, tail = rest[0], rest[1:]
    lines = _lines(frag)
    if action in ("tree", "blob", "blame", "raw", "edit"):
        spec["class"] = "git_repo" if action == "tree" else "git_file"
        if tail:
            _set_ref(spec, _ref_splits(tail))
        req["lines"] = lines if spec["class"] == "git_file" else None
        req["plain"] = "plain=1" in (query or "")
    elif action == "commit" and tail:
        spec["class"] = "git_commit"
        _set_ref(spec, [[tail[0], None]], kind="commit")
        spec["immutable"] = True
    elif action == "commits":
        spec["class"] = "git_repo"
        if tail:
            _set_ref(spec, _ref_splits(tail))
    elif action == "compare":
        spec["class"] = "git_compare"
        spec["requires_ref"] = True
        if tail:
            _compare(spec, "/".join(tail))
    elif action == "releases":
        if tail[:1] == ["latest"]:
            spec["class"] = "release_latest"
            spec["floating"] = True
            req["url_ref_kind"] = "release_latest"
        elif tail[:1] in (["tag"], ["download"]) and len(tail) > 1:
            spec["class"] = "release"
            _set_ref(spec, [["/".join(tail[1:2] if tail[0] == "download" else tail[1:]), None]], hint="tags",
                     kind="tag")
        else:
            spec["notes"].append("releases list page: no release named")
    elif action == "archive":
        spec["class"] = "git_archive"
        name = _ARCHIVE_EXT.sub("", "/".join(tail))
        if name.startswith("refs/tags/"):
            _set_ref(spec, [[name[len("refs/tags/"):], None]], hint="tags", kind="tag")
        elif name.startswith("refs/heads/"):
            _set_ref(spec, [[name[len("refs/heads/"):], None]], hint="heads_first", kind="branch")
        elif name:
            _set_ref(spec, [[name, None]], hint="heads_first")
    elif action == "pull" and tail and tail[0].isdigit():
        spec["class"] = "pull_request"
        n = int(tail[0])
        req["number"] = n
        if len(tail) >= 3 and tail[1] == "commits" and SHA_RE.match(tail[2]):
            _set_ref(spec, [[tail[2], None]], kind="commit")
            spec["immutable"] = True
            req["url_ref_kind"] = "commit"
        else:
            _set_ref(spec, [[f"refs/pull/{n}/head", None]], hint="none", kind="pr_head")
    elif action == "issues" and tail and tail[0].isdigit():
        spec["class"] = "issue"
        req["number"] = int(tail[0])
    elif action in ("tags", "branches"):
        pass
    elif action in _NON_CODE_PAGES or action == "issues":
        spec["class"] = "web_page"
        spec["source_type"] = "secondary"
    else:
        spec["notes"].append(f"page '{action}' is not a code view; the repository itself is used")
    return spec


def _gitlab(spec: dict, host: str, segs: list[str], frag: str, query: str) -> dict:
    if "-" in segs:
        i = segs.index("-")
        repo_segs, rest = segs[:i], segs[i + 1:]
    else:
        repo_segs, rest = segs, []
        for i, s in enumerate(segs[2:], 2):  # legacy /group/proj/tree/<ref> without "-"
            if s in ("tree", "blob", "commit", "commits", "merge_requests", "issues", "compare", "tags"):
                repo_segs, rest = segs[:i], segs[i:]
                break
    _repo_identity(spec, host, repo_segs[0], "/".join(repo_segs))
    if not rest:
        return spec
    action, tail = rest[0], rest[1:]
    req = spec["requested"]
    if action in ("tree", "blob", "raw", "blame"):
        spec["class"] = "git_repo" if action == "tree" else "git_file"
        if tail:
            _set_ref(spec, _ref_splits(tail))
        req["lines"] = _lines(frag) if spec["class"] == "git_file" else None
    elif action == "commit" and tail:
        spec["class"] = "git_commit"
        _set_ref(spec, [[tail[0], None]], kind="commit")
        spec["immutable"] = True
    elif action == "commits":
        if tail:
            _set_ref(spec, _ref_splits(tail))
    elif action == "compare":
        spec["class"] = "git_compare"
        if tail:
            _compare(spec, "/".join(tail))
    elif action == "merge_requests" and tail and tail[0].isdigit():
        spec["class"] = "merge_request"
        n = int(tail[0])
        req["number"] = n
        if len(tail) >= 3 and tail[1] == "diffs" and "commit_id=" in (query or ""):
            sha = urllib.parse.parse_qs(query).get("commit_id", [""])[0]
            if SHA_RE.match(sha):
                _set_ref(spec, [[sha, None]], kind="commit")
                spec["immutable"] = True
                return spec
        _set_ref(spec, [[f"refs/merge-requests/{n}/head", None]], hint="none", kind="mr_head")
    elif action == "issues" and tail and tail[0].isdigit():
        spec["class"] = "issue"
        req["number"] = int(tail[0])
    elif action == "releases":
        if tail[:2] == ["permalink", "latest"]:
            spec["class"] = "release_latest"
            spec["floating"] = True
            req["url_ref_kind"] = "release_latest"
        elif tail:
            spec["class"] = "release"
            _set_ref(spec, [["/".join(tail), None]], hint="tags", kind="tag")
    elif action == "tags" and tail:
        spec["class"] = "release"
        _set_ref(spec, [["/".join(tail), None]], hint="tags", kind="tag")
    elif action == "archive" and tail:
        spec["class"] = "git_archive"
        _set_ref(spec, [[tail[0], None]], hint="heads_first")
    elif action in ("wikis", "pipelines", "jobs", "issues", "merge_requests", "boards", "milestones", "labels"):
        spec["class"] = "web_page"
        spec["source_type"] = "secondary"
    return spec


def _gitea(spec: dict, host: str, segs: list[str], frag: str, query: str) -> dict:
    owner, repo = segs[0], re.sub(r"\.git$", "", segs[1])
    _repo_identity(spec, host, owner, f"{owner}/{repo}")
    rest = segs[2:]
    if not rest:
        return spec
    action, tail = rest[0], rest[1:]
    req = spec["requested"]
    kinds = {"branch": ("heads_first", "branch"), "tag": ("tags", "tag"), "commit": ("none", "commit")}
    if action in ("src", "raw", "blame") and len(tail) >= 2 and tail[0] in kinds:
        spec["class"] = "git_file" if action != "src" or len(tail) > 2 else "git_repo"
        hint, kind = kinds[tail[0]]
        _set_ref(spec, _ref_splits(tail[1:]) if kind != "commit" else [[tail[1], "/".join(tail[2:]) or None]],
                 hint=hint, kind=kind if kind != "branch" else None)
        if kind == "branch":
            req["url_ref_kind"] = "branch"
        req["lines"] = _lines(frag)
    elif action == "commit" and tail:
        spec["class"] = "git_commit"
        _set_ref(spec, [[tail[0], None]], kind="commit")
    elif action == "compare" and tail:
        spec["class"] = "git_compare"
        _compare(spec, "/".join(tail))
    elif action == "pulls" and tail and tail[0].isdigit():
        spec["class"] = "pull_request"
        req["number"] = int(tail[0])
        _set_ref(spec, [[f"refs/pull/{tail[0]}/head", None]], kind="pr_head")
    elif action == "issues" and tail and tail[0].isdigit():
        spec["class"] = "issue"
        req["number"] = int(tail[0])
    elif action == "releases" and tail[:1] == ["latest"]:
        spec["class"] = "release_latest"
        spec["floating"] = True
    elif action == "releases" and tail[:1] == ["tag"] and len(tail) > 1:
        spec["class"] = "release"
        _set_ref(spec, [["/".join(tail[1:]), None]], hint="tags", kind="tag")
    elif action == "archive" and tail:
        spec["class"] = "git_archive"
        _set_ref(spec, [[_ARCHIVE_EXT.sub("", "/".join(tail)), None]], hint="heads_first")
    return spec


def _bitbucket(spec: dict, host: str, segs: list[str], frag: str, query: str) -> dict:
    owner, repo = segs[0], re.sub(r"\.git$", "", segs[1])
    _repo_identity(spec, host, owner, f"{owner}/{repo}")
    rest = segs[2:]
    if not rest:
        return spec
    action, tail = rest[0], rest[1:]
    req = spec["requested"]
    if action in ("src", "raw") and tail:
        spec["class"] = "git_file" if len(tail) > 1 else "git_repo"
        _set_ref(spec, _ref_splits(tail))
        req["lines"] = _lines(frag)
    elif action in ("commits", "commit") and tail and SHA_RE.match(tail[0]):
        spec["class"] = "git_commit"
        _set_ref(spec, [[tail[0], None]], kind="commit")
    elif action == "branch" and tail:
        _set_ref(spec, [["/".join(tail), None]], kind="branch")
    elif action == "pull-requests" and tail and tail[0].isdigit():
        spec["class"] = "pull_request"
        req["number"] = int(tail[0])
        spec["notes"].append("Bitbucket Cloud does not publish pull-request refs over git; the head needs its API")
    elif action == "issues" and tail and tail[0].isdigit():
        spec["class"] = "issue"
        req["number"] = int(tail[0])
    return spec


def _docs(spec: dict, host: str, segs: list[str], query: str) -> dict | None:
    """Documentation pages whose URL carries a version slot."""
    docs: dict = {"site": None, "project": None, "lang": None}
    slot = None
    if host == "docs.python.org":
        s = list(segs)
        if s and re.fullmatch(r"[a-z]{2}(?:-[a-z]{2})?", s[0]) and len(s) > 1:
            docs["lang"] = s.pop(0)
        docs.update(site="python", project="python")
        slot = s[0] if s and (s[0] in ("3", "2", "dev") or re.fullmatch(r"\d\.\d+", s[0])) else ""
    elif host.endswith((".readthedocs.io", ".readthedocs.org", ".rtfd.io")):
        docs.update(site="rtd", project=host.split(".")[0])
        if len(segs) >= 2 and re.fullmatch(r"[a-z]{2}(?:[-_][a-z]{2,4})?", segs[0]):
            docs["lang"], slot = segs[0], segs[1]
        elif segs:
            slot = segs[0] if (segs[0] in _DOCS_FLOATING or _VERSION_SLOT.match(segs[0])) else None
    elif host == "docs.djangoproject.com" and len(segs) >= 2:
        docs.update(site="django", project="django", lang=segs[0])
        slot = segs[1]
    elif host == "docs.rs" and segs:
        docs.update(site="docs.rs", project=segs[0])
        slot = segs[1] if len(segs) > 1 else "latest"
        spec["identity"]["package"] = {"ecosystem": "cargo", "name": segs[0], "purl": f"pkg:cargo/{segs[0]}"}
    elif host == "pkg.go.dev" and segs:
        mod = "/".join(segs)
        mod, _, ver = mod.partition("@")
        ver = ver.split("/")[0]
        docs.update(site="pkg.go.dev", project=mod)
        slot = ver or "latest"
        spec["identity"]["package"] = {"ecosystem": "golang", "name": mod, "purl": f"pkg:golang/{mod}"}
    elif host == "nodejs.org" and segs[:1] == ["docs"] and len(segs) > 1:
        docs.update(site="node", project="node")
        m = re.fullmatch(r"(latest)(?:-(v\d+)\.x)?|(v\d+\.\d+\.\d+)", segs[1])
        slot = (m.group(2) or m.group(3) or "latest") if m else None
        docs["floating_line"] = bool(m and m.group(1))
    elif host == "nodejs.org" and "api" in segs:
        docs.update(site="node", project="node")
        slot = "latest"
    elif host == "learn.microsoft.com":
        view = urllib.parse.parse_qs(query or "").get("view", [None])[0]
        if not view:
            return None
        docs.update(site="microsoft", project=None)
        slot = view
    else:
        return None
    spec["class"] = "doc_page"
    spec["source_type"] = "official_doc"
    spec["identity"]["docs"] = docs
    req = spec["requested"]
    req["docs_version"] = slot
    floating = slot is None or slot.lower() in _DOCS_FLOATING or bool(docs.get("floating_line"))
    req["floating_slot"] = floating
    return spec


def _generic_doc_slot(spec: dict, segs: list[str]) -> None:
    """Official-doc hosts without a handler: the first version-like or floating path segment (heuristic)."""
    for s in segs[:3]:
        if _VERSION_SLOT.match(s) or s.lower() in ("latest", "stable", "dev", "current"):
            spec["requested"]["docs_version"] = s
            spec["requested"]["floating_slot"] = s.lower() in _DOCS_FLOATING or not _VERSION_SLOT.match(s)
            spec["notes"].append("docs version slot found by the generic path heuristic")
            return


def _package(spec: dict, ecosystem: str, name: str, version: str | None, *, exact: bool | None,
             spec_text: str | None = None, namespace: str | None = None, guessed: bool = False) -> dict:
    spec["class"] = "package"
    full = f"{namespace}/{name}" if namespace else name
    purl_name = full if ecosystem != "npm" or not full.startswith("@") else "%40" + full[1:]
    spec["identity"]["package"] = {"ecosystem": ecosystem, "name": full, "purl": f"pkg:{ecosystem}/{purl_name}",
                                   "ecosystem_guessed": guessed}
    req = spec["requested"]
    req["version_text"] = version
    req["version_exact"] = exact
    req["version_spec"] = spec_text
    if version and exact:
        spec["immutable"] = True
    return spec


def _docs_python_or_url(host: str) -> bool:
    return host == "docs.python.org"


def classify_url(s: str, kind: str = "auto") -> dict:
    parsed = urllib.parse.urlparse(s)
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    segs = [urllib.parse.unquote(x) for x in (parsed.path or "/").split("/") if x]
    frag, query = parsed.fragment, parsed.query
    spec = _empty("web_page", s)
    spec["requested"]["url"] = s
    spec["identity"]["host"] = host
    is_git_host = host in GIT_HOSTS or host.startswith("gitlab.") or host == "www.github.com"
    if is_git_host and "@" in (parsed.path or "") and len(segs) >= 2:
        # https://github.com/o/r@v1.2 (and .git@ref): the ref follows the repository path
        base, _, ref = (parsed.path or "").rpartition("@")
        if ref:
            bsegs = [urllib.parse.unquote(x) for x in base.split("/") if x]
            spec["class"] = "git_repo"
            _repo_identity(spec, "github.com" if host == "www.github.com" else host, bsegs[0], "/".join(bsegs))
            _set_ref(spec, [[ref, None]], hint="none")
            return spec
    if host in ("github.com", "www.github.com") and len(segs) >= 2:
        spec["class"] = "git_repo"
        return _github(spec, "github.com", segs, frag, query)
    if host == "raw.githubusercontent.com" and len(segs) >= 3:
        spec["class"] = "git_file"
        _repo_identity(spec, "github.com", segs[0], f"{segs[0]}/{segs[1]}")
        tail = segs[2:]
        if tail[:2] in (["refs", "heads"], ["refs", "tags"]) and len(tail) > 3:
            hint = "heads_first" if tail[1] == "heads" else "tags"
            _set_ref(spec, _ref_splits(tail[2:]), hint=hint, kind="branch" if tail[1] == "heads" else "tag")
        else:
            _set_ref(spec, _ref_splits(tail))
        return spec
    if host == "codeload.github.com" and len(segs) >= 4:
        spec["class"] = "git_archive"
        _repo_identity(spec, "github.com", segs[0], f"{segs[0]}/{segs[1]}")
        name = "/".join(segs[3:])
        if name.startswith("refs/tags/"):
            _set_ref(spec, [[name[10:], None]], hint="tags", kind="tag")
        elif name.startswith("refs/heads/"):
            _set_ref(spec, [[name[11:], None]], hint="heads_first", kind="branch")
        else:
            _set_ref(spec, [[name, None]], hint="heads_first")
        return spec
    if (host == "gitlab.com" or host.startswith("gitlab.") or host == "salsa.debian.org") and len(segs) >= 2:
        spec["class"] = "git_repo"
        return _gitlab(spec, host, segs, frag, query)
    if (host in _GITEA_HOSTS or "gitea" in host or "forgejo" in host) and len(segs) >= 2:
        spec["class"] = "git_repo"
        return _gitea(spec, host, segs, frag, query)
    if host == "bitbucket.org" and len(segs) >= 2:
        spec["class"] = "git_repo"
        return _bitbucket(spec, host, segs, frag, query)
    if host == "gitee.com" and len(segs) >= 2:
        spec["class"] = "git_repo"
        return _github(spec, host, segs, frag, query)
    if host in ("arxiv.org", "www.arxiv.org", "export.arxiv.org") and len(segs) >= 2 \
            and segs[0] in ("abs", "pdf", "html", "format"):
        ident = "/".join(segs[1:])
        ident = re.sub(r"\.pdf$", "", ident)
        m = ARXIV_RE.match(ident)
        if m:
            return _paper(spec, m.group(1), m.group(2))
    if host in ("doi.org", "dx.doi.org", "www.doi.org") and segs:
        m = DOI_RE.match("/".join(segs))
        if m:
            return _doi(spec, m.group(1))
    if host == "pypi.org" and len(segs) >= 2 and segs[0] == "project":
        ver = segs[2] if len(segs) > 2 else None
        return _package(spec, "pypi", segs[1], ver, exact=bool(ver))
    if host in ("www.npmjs.com", "npmjs.com") and len(segs) >= 2 and segs[0] == "package":
        name = "/".join(segs[1:3]) if segs[1].startswith("@") else segs[1]
        rest = segs[3:] if segs[1].startswith("@") else segs[2:]
        ver = rest[1] if len(rest) >= 2 and rest[0] == "v" else None
        return _package(spec, "npm", name, ver, exact=bool(ver))
    if host == "crates.io" and len(segs) >= 2 and segs[0] == "crates":
        ver = segs[2] if len(segs) > 2 else None
        return _package(spec, "cargo", segs[1], ver, exact=bool(ver))
    if host in ("archive.softwareheritage.org", "softwareheritage.org"):
        for sg in segs:
            if sg.startswith("swh:1:"):
                return _swh(spec, sg + (";" + query.replace("&", ";") if query else ""))
    if host == "stackoverflow.com" or host.endswith(".stackexchange.com") or host in (
            "serverfault.com", "superuser.com", "askubuntu.com"):
        if len(segs) >= 2 and segs[0] in ("questions", "q", "a", "answers") and segs[1].isdigit():
            spec["class"] = "qa_post"
            spec["source_type"] = "secondary"
            spec["requested"]["number"] = int(segs[1])
            return spec
    if _docs(spec, host, segs, query):
        return spec
    # generic hosts: a .git path or url@ref is a git remote, the rest are documents
    path = parsed.path or "/"
    if path.endswith(".git") or ".git@" in path or kind == "reference_repo":
        spec["class"] = "git_repo"
        ref = None
        if "@" in path:
            base, _, r = path.rpartition("@")
            if r:
                path, ref = base, r
        repo_path = path.strip("/")
        _repo_identity(spec, host, repo_path.split("/")[0] if repo_path else host, repo_path or host,
                       scheme=scheme, clone_url=f"{scheme}://{parsed.netloc}/{repo_path}")
        if ref:
            _set_ref(spec, [[ref, None]])
        return spec
    from verinoda.research import (
        _doc_source_type,  # heuristic host/path table, kept in one place
    )

    spec["source_type"] = kind if kind in DOC_KINDS else _doc_source_type(host, parsed.path, query)
    if spec["source_type"] == "paper":
        spec["class"] = "paper" if host in ("arxiv.org", "openreview.net") else "web_page"
    if spec["source_type"] == "official_doc":
        spec["class"] = "doc_page"
        spec["identity"]["docs"] = {"site": "generic", "project": host, "lang": None}
        _generic_doc_slot(spec, segs)
    return spec


def _paper(spec: dict, arxiv_id: str, version: str | None) -> dict:
    spec["class"] = "paper"
    spec["source_type"] = "paper"
    spec["identity"]["paper"] = {"arxiv_id": arxiv_id, "doi": None}
    spec["requested"]["version_text"] = version
    spec["requested"]["version_exact"] = bool(version)
    spec["immutable"] = bool(version)
    return spec


def _doi(spec: dict, doi: str) -> dict:
    m = re.match(r"^10\.48550/arxiv\.(.+)$", doi, re.I)
    if m:  # the arXiv DataCite DOI resolves to the latest version: hand it to the arXiv resolver
        am = ARXIV_RE.match(m.group(1))
        if am:
            _paper(spec, am.group(1), am.group(2))
            spec["identity"]["paper"]["doi"] = doi
            spec["notes"].append("arXiv DOI: resolves to the latest arXiv version (floating)")
            return spec
    spec["class"] = "doi"
    spec["source_type"] = "paper"
    spec["identity"]["paper"] = {"arxiv_id": None, "doi": doi}
    spec["immutable"] = True
    if re.match(r"^10\.5281/zenodo\.", doi, re.I):
        spec["notes"].append("Zenodo DOI: may be a concept DOI (all versions); check it is a version DOI")
    return spec


def _swh(spec: dict, s: str) -> dict:
    m = SWHID_RE.match(s)
    if not m:
        raise ValueError(f"malformed SWHID {s!r}")
    spec["class"] = "swh_object"
    spec["source_type"] = "reference_repo"  # an archived repository object (commit, tree, file, release)
    quals = dict(q.split("=", 1) for q in m.group(3).split(";") if "=" in q)
    spec["identity"]["swhid"] = {"type": m.group(1), "hash": m.group(2), "qualifiers": quals,
                                 "core": f"swh:1:{m.group(1)}:{m.group(2)}"}
    spec["immutable"] = True
    if m.group(1) == "rev":
        spec["requested"]["commit"] = m.group(2)
    if quals.get("origin"):
        try:
            sub = classify_url(quals["origin"])
            if sub["identity"].get("canonical_url"):
                for k in ("host", "owner", "repo", "canonical_url", "clone_url"):
                    spec["identity"][k] = sub["identity"][k]
        except ValueError:
            pass
    if quals.get("path"):
        spec["requested"]["path"] = quals["path"].lstrip("/")
    if quals.get("lines"):
        a, _, b = quals["lines"].partition("-")
        if a.isdigit():
            spec["requested"]["lines"] = [int(a), int(b or a)]
    return spec


def _purl(spec: dict, s: str) -> dict:
    m = PURL_RE.match(s)
    if not m:
        raise ValueError(f"malformed purl {s!r}")
    typ = m.group(1).lower()
    path = urllib.parse.unquote(m.group(2))
    ver = urllib.parse.unquote(m.group(3)) if m.group(3) else None
    if typ in ("github", "gitlab", "bitbucket"):
        host = {"github": "github.com", "gitlab": "gitlab.com", "bitbucket": "bitbucket.org"}[typ]
        spec["class"] = "git_repo"
        _repo_identity(spec, host, path.split("/")[0], path)
        if ver:
            _set_ref(spec, [[ver, None]])
        return spec
    ns, _, name = path.rpartition("/")
    return _package(spec, typ, name, ver, exact=bool(ver), namespace=ns or None)


def _slug_spec(spec: dict, m: re.Match) -> dict:
    owner, repo = m.group(1), m.group(2)
    host = "github.com"
    spec["class"] = "git_repo"
    _repo_identity(spec, host, owner, f"{owner}/{repo}", assumed=True)
    spec["notes"].append("host assumed github.com for owner/repo")
    if m.group("ref"):
        ref = m.group("ref")
        _set_ref(spec, [[ref, None]], hint="none")
        if spec["requested"]["url_ref_kind"] == "commit" or (SHA_RE.match(ref) and re.search(r"[a-f]", ref, re.I)
                                                             and re.search(r"\d", ref)):
            spec["class"] = "git_commit"
            spec["immutable"] = True
            spec["requested"]["url_ref_kind"] = "commit"
            spec["requested"]["commit"] = ref
    elif m.group("num"):
        n = int(m.group("num"))
        spec["requested"]["number"] = n
        spec["class"] = "merge_request" if m.group("sep") == "!" else "issue"
        if m.group("sep") == "!":
            spec["identity"]["host"] = "gitlab.com"
            _repo_identity(spec, "gitlab.com", owner, f"{owner}/{repo}", assumed=True)
            _set_ref(spec, [[f"refs/merge-requests/{n}/head", None]], kind="mr_head")
        else:
            spec["notes"].append("owner/repo#N is an issue or a pull request; resolution checks refs/pull/N/head")
            spec["requested"]["url"] = f"https://github.com/{owner}/{repo}/issues/{n}"
    return spec


def classify(value, kind: str = "auto") -> dict:
    """Reference spec for a string or an extracted mention (see the module docstring).

    Raises ValueError for input that is not a usable reference.
    """
    if isinstance(value, dict):
        return classify_mention(value)
    spec = _classify(value, kind)
    spec["requires_ref"] = spec["class"] in REF_REQUIRED
    return spec


def _classify(value, kind: str) -> dict:
    s = (value or "").strip()
    if not s:
        raise ValueError("empty reference")
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}; expected one of {KINDS}")
    s = s.strip("<>")
    has_scheme = bool(re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", s))
    if not has_scheme:
        if SWHID_RE.match(s):
            return _swh(_empty("swh_object", s), s)
        if s.lower().startswith("pkg:"):
            return _purl(_empty("package", s), s)
        m = DOI_RE.match(s)
        if m and (s.lower().startswith("doi:") or s.startswith("10.")):
            return _doi(_empty("doi", s), m.group(1))
        m = ARXIV_RE.match(s)
        if m and (s.lower().startswith("arxiv:") or re.fullmatch(r"\d{4}\.\d{4,5}(v\d+)?", s)):
            return _paper(_empty("paper", s), m.group(1), m.group(2))
        m = GOMOD_RE.match(s)
        if m:
            return _gomod(_empty("package", s), m.group(1), m.group(2))
        m = MAVEN_RE.match(s)
        if m:
            spec = _package(_empty("package", s), "maven", m.group(2), m.group(3), exact=True, namespace=m.group(1))
            return spec
        if not Path(s).expanduser().exists():
            m = PIP_RE.match(s)
            if m:
                ops = m.group(3).replace(" ", "")
                exact = re.fullmatch(r"(?:===|==)v?([\w.+-]+)", ops)
                return _package(_empty("package", s), "pypi" if not m.group(1).startswith("@") else "npm",
                                m.group(1), exact.group(1) if exact else None, exact=bool(exact), spec_text=ops)
            m = AT_RE.match(s)
            if m and not SLUG_RE.match(s):
                ver = m.group(2)
                exact = re.fullmatch(r"=?v?(\d[\w.+-]*)", ver)
                return _package(_empty("package", s), "npm", m.group(1), exact.group(1) if exact else None,
                                exact=bool(exact), spec_text=None if exact else ver, guessed=True)
            m = SLUG_RE.match(s)
            if m and not _SCP_RE.match(s):
                return _slug_spec(_empty("git_repo", s), m)
    scp = _SCP_RE.match(s) if not has_scheme else None
    if not has_scheme and not scp:
        return _local(s, kind)
    if scp:
        return _scp(s, scp)
    parsed = urllib.parse.urlparse(s)
    scheme = parsed.scheme.lower()
    if scheme in ("ssh", "git", "git+ssh", "git+https", "git+http", "file"):
        return _ssh_like(s, scheme)
    if scheme not in ("http", "https"):
        raise ValueError(f"unsupported reference scheme {scheme!r} in {s!r}")
    spec = classify_url(s, kind)
    if kind in DOC_KINDS and spec["class"] in GIT_CLASSES | {"doc_page", "web_page", "paper", "doi"}:
        if spec["class"] in GIT_CLASSES:
            spec = _empty("web_page", s)
            spec["requested"]["url"] = s
            spec["identity"]["host"] = (parsed.hostname or "").lower()
        spec["source_type"] = kind
    return spec


def _gomod(spec: dict, module: str, version: str) -> dict:
    _package(spec, "golang", module, version, exact=True)
    m = GO_PSEUDO_RE.match(version)
    if m:
        spec["requested"]["commit"] = m.group(2)
        spec["notes"].append(f"Go pseudo-version: commit prefix {m.group(2)}")
    parts = module.split("/")
    if parts[0] in ("github.com", "gitlab.com", "bitbucket.org", "codeberg.org") and len(parts) >= 3:
        _repo_identity(spec, parts[0], parts[1], f"{parts[1]}/{parts[2]}")
        spec["class"] = "package"
        if len(parts) > 3 and not re.fullmatch(r"v\d+", parts[3]):
            spec["requested"]["path"] = "/".join(parts[3:])
    return spec


def _local(s: str, kind: str) -> dict:
    from verinoda.research import _git

    p = Path(s).expanduser()
    ref = None
    if not p.exists() and "@" in s:
        base, _, r = s.rpartition("@")
        if base and r and Path(base).expanduser().exists():
            p, ref = Path(base).expanduser(), r
    if not p.exists():
        raise ValueError(f"reference {s!r} is neither an existing path nor an http(s)/git URL")
    p = p.resolve()
    spec = _empty("git_repo", s)
    spec["identity"]["local_path"] = str(p)
    if p.is_file():
        spec["class"] = "web_page"
        spec["source_type"] = kind if kind in DOC_KINDS else ("paper" if p.suffix.lower() == ".pdf" else "secondary")
        spec["legacy_type"] = "document"
        return spec
    rc, top, _ = _git(p, "rev-parse", "--show-toplevel")
    if rc == 0 and top.strip():
        topp = Path(top.strip()).resolve()
        spec["identity"]["local_path"] = str(topp)
        spec["identity"]["canonical_url"] = topp.as_uri()
        spec["identity"]["repo"] = topp.name
        spec["legacy_type"] = "git"
        if topp != p:
            try:
                spec["requested"]["path"] = p.relative_to(topp).as_posix()
            except ValueError:
                pass
    else:
        spec["legacy_type"] = "local_dir"
        spec["identity"]["repo"] = p.name
    if ref:
        _set_ref(spec, [[ref, spec["requested"]["path"]]], hint="none")
    return spec


def _scp(s: str, scp: re.Match) -> dict:
    path = scp.group("path")
    ref = None
    if "@" in path:
        path, _, r = path.rpartition("@")
        ref = r or None
    spec = _empty("git_repo", s)
    host = scp.group("host").lower()
    repo_path = re.sub(r"\.git$", "", path.strip("/"))
    _repo_identity(spec, host, repo_path.split("/")[0], repo_path,
                   clone_url=f"{scp.group('user')}@{scp.group('host')}:{path}")
    if ref:
        _set_ref(spec, [[ref, None]])
    return spec


def _ssh_like(s: str, scheme: str) -> dict:
    url = s[4:] if scheme.startswith("git+") else s
    pu = urllib.parse.urlparse(url)
    path = pu.path
    ref = None
    if "@" in path:  # ssh://git@host/o/r.git@v1 -> ref after the path (user@ lives in netloc)
        path, _, r = path.rpartition("@")
        ref = r or None
        url = urllib.parse.urlunparse(pu._replace(path=path))
    spec = _empty("git_repo", s)
    host = (pu.hostname or "").lower()
    repo_path = re.sub(r"\.git$", "", path.strip("/"))
    _repo_identity(spec, host or "localhost", (repo_path.split("/") or [""])[0], repo_path, clone_url=url)
    if scheme == "file":
        spec["identity"]["canonical_url"] = url
    if ref:
        _set_ref(spec, [[ref, None]])
    return spec


def classify_mention(m: dict) -> dict:
    """Reference spec for an extracted mention (``verinoda.references.mentions``)."""
    k, text, norm = m["kind"], m["text"], m.get("normalized") or {}
    if k == "url":
        return classify(text)
    if k in ("swhid", "purl", "doi", "arxiv", "gomod", "maven_gav", "pkgspec"):
        t = text if k != "arxiv" or text.lower().startswith("arxiv:") else "arXiv:" + text
        if k == "pkgspec":
            m2 = PIP_RE.match(text)
            if m2:
                ops = m2.group(3).replace(" ", "")
                exact = re.fullmatch(r"(?:===|==)v?([\w.+-]+)", ops)
                return _package(_empty("package", text), "pypi", m2.group(1), exact.group(1) if exact else None,
                                exact=bool(exact), spec_text=ops)
            m2 = AT_RE.match(text)
            if m2:
                ver = m2.group(2)
                exact = re.fullmatch(r"=?v?(\d[\w.+-]*)", ver)
                return _package(_empty("package", text), "npm", m2.group(1), exact.group(1) if exact else None,
                                exact=bool(exact), spec_text=None if exact else ver, guessed=True)
        return classify(t)
    if k in ("repo_slug", "repo_at"):
        m2 = SLUG_RE.match(text)
        if m2:
            return _slug_spec(_empty("git_repo", text), m2)
        raise ValueError(f"not an owner/repo reference: {text!r}")
    if k in ("xref_issue", "xref_pr", "xref_mr"):
        cls = {"xref_issue": "issue", "xref_pr": "pull_request", "xref_mr": "merge_request"}[k]
        spec = _empty(cls, text)
        n = norm.get("number")
        spec["requested"]["number"] = n
        if norm.get("owner"):
            host = "gitlab.com" if cls == "merge_request" else "github.com"
            _repo_identity(spec, host, norm["owner"], f"{norm['owner']}/{norm['repo']}", assumed=True)
        if cls == "pull_request":
            _set_ref(spec, [[f"refs/pull/{n}/head", None]], hint="none", kind="pr_head")
        elif cls == "merge_request":
            _set_ref(spec, [[f"refs/merge-requests/{n}/head", None]], hint="none", kind="mr_head")
        spec["requires_ref"] = False  # the number is the ref; the repository comes from binding
        return spec
    if k == "file":
        spec = _empty("local_file", text)
        spec["requested"]["path"] = text.replace("\\", "/")
        return spec
    if k == "symbol":
        spec = _empty("local_symbol", text)
        spec["requested"]["symbol"] = text
        return spec
    if k == "app":
        spec = _empty("application", text)
        spec["identity"]["app"] = {"name": text}
        spec["identity"]["source_availability"] = "unknown"
        return spec
    if k == "name_candidate":
        rt = norm.get("runtime")
        if rt:
            spec = _empty("runtime", text)
            spec["identity"]["package"] = {"ecosystem": "runtime", "name": rt, "purl": f"pkg:generic/{rt}"}
            return spec
        spec = _package(_empty("package", text), "unknown", text, None, exact=None, guessed=True)
        spec["candidate"] = True
        return spec
    raise ValueError(f"mention kind {k!r} is a qualifier, not a reference")


def to_legacy(spec: dict) -> dict:
    """The dict :func:`verinoda.research.parse_reference` returned before references/ existed."""
    cls = spec["class"]
    ident, req = spec["identity"], spec["requested"]
    out = {"input": spec["input"], "type": None, "url": None, "path": None, "ref": None, "ref_candidates": [],
           "subpath": None, "host": ident.get("host"), "slug": None, "source_type": spec.get("source_type"),
           "class": cls, "requested": req, "identity": ident, "requires_ref": spec.get("requires_ref", False),
           "ref_namespace_hint": req.get("ref_namespace_hint", "none"), "lines": req.get("lines"),
           "compare": req.get("compare"), "notes": list(spec.get("notes") or [])}
    lt = spec.get("legacy_type")
    if ident.get("local_path") and lt in ("git", "local_dir", "document"):
        p = Path(ident["local_path"])
        out["path"] = str(p)
        out["type"] = lt
        out["subpath"] = req.get("path") if lt == "git" else None
        out["slug"] = f"{slugify(p.stem if p.is_file() else p.name)}-{h6(str(p).lower())}"
        if lt == "document":
            out["source_type"] = spec.get("source_type")
        cands = [tuple(c) for c in req.get("ref_candidates") or []]
        out["ref_candidates"] = cands
        out["ref"] = cands[0][0] if cands else None
        return out
    if cls in GIT_CLASSES:
        url = ident.get("clone_url") or ident.get("canonical_url")
        out["type"] = "git"
        out["url"] = url
        cands = [tuple(c) for c in req.get("ref_candidates") or []]
        out["ref_candidates"] = cands
        out["ref"] = cands[0][0] if cands else None
        out["subpath"] = cands[0][1] if cands else req.get("path")
        out["slug"] = git_slug(url)
        return out
    if cls == "package":
        out["type"] = "package"
        pkg = ident.get("package") or {}
        out["slug"] = f"pkg-{slugify(pkg.get('ecosystem', '') + '-' + pkg.get('name', ''), 48)}-{h6(spec['input'])}"
        return out
    # documents: papers, DOIs, docs, Q&A, SWH objects, issues, web pages
    url = req.get("url")
    if url is None:
        if cls == "paper":
            p = ident["paper"]
            url = f"https://arxiv.org/abs/{p['arxiv_id']}{req.get('version_text') or ''}"
        elif cls == "doi":
            url = f"https://doi.org/{ident['paper']['doi']}"
        elif cls == "swh_object":
            url = f"https://archive.softwareheritage.org/{ident['swhid']['core']}"
    out["type"] = "document"
    out["url"] = url
    out["source_type"] = spec.get("source_type") or "secondary"
    host = ident.get("host") or (urllib.parse.urlparse(url).hostname if url else "") or ""
    tail = [x for x in urllib.parse.urlparse(url or "").path.split("/") if x]
    last = "-".join(tail[-2:]) if cls == "issue" else (tail[-1] if tail else "index")
    out["slug"] = f"doc-{slugify(host + '-' + last, 48)}-{h6(url or spec['input'])}"
    return out
