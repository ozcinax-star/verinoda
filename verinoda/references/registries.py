"""Small, cassette-testable lookups against registries and hosts (docs/DESIGN.md D14/D15).

Every function takes a :class:`~verinoda.references.transport.Transport`,
never raises for HTTP or network failures and returns ``{"ok": bool, ...}``
with ``reason`` (``not_found``, ``moved``, ``rate_limited``, ``offline``,
``unreachable``, ``bad_response``) when ``ok`` is false. Registry metadata is a
*pointer* (which versions exist, where the source claims to live); it never
verifies a claim by itself.
"""

from __future__ import annotations

import re
import urllib.parse
import xml.etree.ElementTree as ET

from verinoda.references.transport import (
    CassetteMiss,
    Offline,
    RateLimited,
    TransportError,
)

ATOM = "{http://www.w3.org/2005/Atom}"


def _fail(exc: Exception) -> dict:
    if isinstance(exc, CassetteMiss):
        raise exc  # tests must fail loudly
    if isinstance(exc, Offline):
        return {"ok": False, "reason": "offline", "error": str(exc)}
    if isinstance(exc, RateLimited):
        return {"ok": False, "reason": "rate_limited", "error": str(exc), "reset": exc.reset}
    return {"ok": False, "reason": "unreachable", "error": str(exc)[:300]}


def _get(transport, url: str, accept: str | None = None) -> tuple[object | None, dict | None]:
    try:
        return transport.get(url, accept=accept), None
    except TransportError as exc:
        return None, _fail(exc)


def _status_fail(resp, what: str) -> dict | None:
    if resp.ok:
        return None
    if resp.status in (404, 410):
        return {"ok": False, "reason": "not_found", "error": f"{what}: HTTP {resp.status}", "status": resp.status}
    if resp.status in (301, 302, 307, 308):
        return {"ok": False, "reason": "moved", "error": f"{what}: HTTP {resp.status}",
                "location": resp.headers.get("location"), "status": resp.status}
    if resp.status in (403, 429) and (resp.headers.get("x-ratelimit-remaining") == "0" or resp.status == 429):
        return {"ok": False, "reason": "rate_limited", "error": f"{what}: HTTP {resp.status}",
                "reset": resp.headers.get("x-ratelimit-reset"), "status": resp.status}
    if resp.status in (401, 403):
        return {"ok": False, "reason": "auth_required", "error": f"{what}: HTTP {resp.status}", "status": resp.status}
    return {"ok": False, "reason": "unreachable", "error": f"{what}: HTTP {resp.status}", "status": resp.status}


def _meta(resp) -> dict:
    return {"from_cache": resp.from_cache, "retrieved_at": resp.retrieved_at, "url": resp.url}


# -- PyPI -----------------------------------------------------------------------------------------------------

def pypi_project(transport, name: str) -> dict:
    """Versions (with yank flags and upload times) and project URLs of a PyPI project."""
    url = f"https://pypi.org/pypi/{urllib.parse.quote(name)}/json"
    resp, err = _get(transport, url)
    if err:
        return err
    bad = _status_fail(resp, f"PyPI {name}")
    if bad:
        return bad
    try:
        data = resp.json()
    except ValueError:
        return {"ok": False, "reason": "bad_response", "error": "PyPI answered with invalid JSON"}
    releases = {}
    for ver, files in (data.get("releases") or {}).items():
        files = files or []
        releases[ver] = {"yanked": bool(files) and all(f.get("yanked") for f in files),
                         "yanked_reason": next((f.get("yanked_reason") for f in files if f.get("yanked_reason")), None),
                         "upload_time": min((f.get("upload_time_iso_8601") or f.get("upload_time") or ""
                                             for f in files), default=None) or None,
                         "files": [{"filename": f.get("filename"), "packagetype": f.get("packagetype"),
                                    "sha256": (f.get("digests") or {}).get("sha256"), "url": f.get("url")}
                                   for f in files[:6]]}
    info = data.get("info") or {}
    return {"ok": True, "name": info.get("name") or name, "latest": info.get("version"),
            "versions": list(releases), "releases": releases, "project_urls": info.get("project_urls") or {},
            "home_page": info.get("home_page"), "requires_python": info.get("requires_python"), **_meta(resp)}


def source_repo_from_urls(urls: dict | None, home: str | None = None) -> str | None:
    """The first repository-looking project URL (Source, Repository, Code, then a GitHub/GitLab homepage)."""
    urls = urls or {}
    order = ["source", "source code", "repository", "code", "github", "gitlab", "homepage", "home"]
    by = {k.lower(): v for k, v in urls.items() if isinstance(v, str)}
    cands = [by[k] for k in order if k in by] + ([home] if home else [])
    for u in cands:
        m = re.match(r"^https?://(?:www\.)?(github\.com|gitlab\.com|codeberg\.org|bitbucket\.org)/([^/#?]+)/([^/#?]+)",
                     u or "")
        if m:
            return f"https://{m.group(1)}/{m.group(2)}/{re.sub(r'.git$', '', m.group(3))}"
    return None


# -- npm / crates / Go ---------------------------------------------------------------------------------------

def npm_package(transport, name: str) -> dict:
    url = "https://registry.npmjs.org/" + name.replace("/", "%2F")
    resp, err = _get(transport, url, accept="application/vnd.npm.install-v1+json")
    if err:
        return err
    bad = _status_fail(resp, f"npm {name}")
    if bad:
        return bad
    try:
        data = resp.json()
    except ValueError:
        return {"ok": False, "reason": "bad_response", "error": "npm answered with invalid JSON"}
    versions = data.get("versions") or {}
    return {"ok": True, "name": name, "latest": (data.get("dist-tags") or {}).get("latest"),
            "versions": list(versions),
            "releases": {v: {"yanked": False, "deprecated": (info or {}).get("deprecated")}
                         for v, info in versions.items()}, **_meta(resp)}


def crates_index(transport, name: str) -> dict:
    n = name.lower()
    path = {1: f"1/{n}", 2: f"2/{n}", 3: f"3/{n[0]}/{n}"}.get(len(n), f"{n[0:2]}/{n[2:4]}/{n}")
    resp, err = _get(transport, f"https://index.crates.io/{path}")
    if err:
        return err
    bad = _status_fail(resp, f"crates.io {name}")
    if bad:
        return bad
    import json

    rel = {}
    for line in resp.text().splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        rel[d.get("vers")] = {"yanked": bool(d.get("yanked"))}
    return {"ok": True, "name": name, "versions": list(rel), "releases": rel, **_meta(resp)}


def _go_escape(module: str) -> str:
    return re.sub(r"[A-Z]", lambda m: "!" + m.group(0).lower(), module)


def go_versions(transport, module: str) -> dict:
    resp, err = _get(transport, f"https://proxy.golang.org/{_go_escape(module)}/@v/list")
    if err:
        return err
    bad = _status_fail(resp, f"Go proxy {module}")
    if bad:
        return bad
    vs = [v.strip() for v in resp.text().splitlines() if v.strip()]
    return {"ok": True, "name": module, "versions": vs, "releases": {v: {"yanked": False} for v in vs}, **_meta(resp)}


def go_info(transport, module: str, version: str) -> dict:
    """``.info`` of a module version: time and ``Origin {VCS, URL, Ref, Hash}`` (publisher-declared VCS info)."""
    resp, err = _get(transport, f"https://proxy.golang.org/{_go_escape(module)}/@v/{version}.info")
    if err:
        return err
    bad = _status_fail(resp, f"Go proxy {module}@{version}")
    if bad:
        return bad
    try:
        d = resp.json()
    except ValueError:
        return {"ok": False, "reason": "bad_response", "error": "invalid .info JSON"}
    return {"ok": True, "version": d.get("Version"), "time": d.get("Time"), "origin": d.get("Origin"), **_meta(resp)}


def package_versions(transport, ecosystem: str, name: str) -> dict:
    if ecosystem == "pypi":
        return pypi_project(transport, name)
    if ecosystem == "npm":
        return npm_package(transport, name)
    if ecosystem == "cargo":
        return crates_index(transport, name)
    if ecosystem == "golang":
        return go_versions(transport, name)
    return {"ok": False, "reason": "unsupported", "error": f"no registry lookup for ecosystem {ecosystem!r}"}


# -- arXiv ---------------------------------------------------------------------------------------------------

def arxiv_latest(transport, arxiv_id: str) -> dict:
    """Latest version of an arXiv id from the export API (one request; 3-second host interval)."""
    url = f"http://export.arxiv.org/api/query?id_list={urllib.parse.quote(arxiv_id)}"
    resp, err = _get(transport, url)
    if err:
        return err
    bad = _status_fail(resp, f"arXiv {arxiv_id}")
    if bad:
        return bad
    try:
        root = ET.fromstring(resp.body)
    except ET.ParseError:
        return {"ok": False, "reason": "bad_response", "error": "arXiv answered with invalid XML"}
    entry = root.find(f"{ATOM}entry")
    if entry is None:
        return {"ok": False, "reason": "not_found", "error": f"arXiv has no entry {arxiv_id}"}
    eid = (entry.findtext(f"{ATOM}id") or "").strip()
    m = re.search(r"v(\d+)$", eid)
    if not m:
        return {"ok": False, "reason": "not_found", "error": f"arXiv entry for {arxiv_id} has no version"}
    return {"ok": True, "latest": int(m.group(1)), "title": " ".join((entry.findtext(f"{ATOM}title") or "").split()),
            "updated": entry.findtext(f"{ATOM}updated"), "published": entry.findtext(f"{ATOM}published"),
            **_meta(resp)}


def arxiv_versions(transport, arxiv_id: str) -> dict:
    """All versions with their dates (OAI-PMH ``arXivRaw``) for date-based pins."""
    url = ("http://export.arxiv.org/oai2?verb=GetRecord&identifier=oai:arXiv.org:"
           f"{urllib.parse.quote(arxiv_id)}&metadataPrefix=arXivRaw")
    resp, err = _get(transport, url)
    if err:
        return err
    bad = _status_fail(resp, f"arXiv OAI {arxiv_id}")
    if bad:
        return bad
    from email.utils import parsedate_to_datetime

    out = []
    for m in re.finditer(r'<version version="v(\d+)">\s*<date>([^<]+)</date>', resp.text()):
        try:
            d = parsedate_to_datetime(m.group(2).strip()).date().isoformat()
        except (TypeError, ValueError):
            d = None
        out.append({"version": int(m.group(1)), "date": d})
    if not out:
        return {"ok": False, "reason": "not_found", "error": f"no arXivRaw versions for {arxiv_id}"}
    return {"ok": True, "versions": out, **_meta(resp)}


# -- documentation -------------------------------------------------------------------------------------------

def python_docs_release(transport, url: str) -> dict:
    """The exact Python release a docs.python.org page documents (from its ``<title>``)."""
    resp, err = _get(transport, url)
    if err:
        return err
    bad = _status_fail(resp, "docs.python.org")
    if bad:
        return bad
    m = re.search(r"<title>[^<]*?Python\s+(\d+\.\d+(?:\.\d+)?(?:[abrc]+\d+)?)\s+documentation", resp.text(), re.I)
    if not m:
        return {"ok": False, "reason": "bad_response", "error": "no 'Python X.Y.Z documentation' title found"}
    return {"ok": True, "release": m.group(1), **_meta(resp)}


def rtd_version(transport, project: str, slug: str) -> dict:
    """Read the Docs version -> ``{built, active, type, identifier (commit for tags), ref}``."""
    url = f"https://app.readthedocs.org/api/v3/projects/{project}/versions/{slug}/"
    resp, err = _get(transport, url)
    if err:
        return err
    bad = _status_fail(resp, f"Read the Docs {project}/{slug}")
    if bad:
        return bad
    try:
        d = resp.json()
    except ValueError:
        return {"ok": False, "reason": "bad_response", "error": "invalid RTD JSON"}
    if "results" in d:
        d = (d.get("results") or [{}])[0]
    return {"ok": True, "built": d.get("built"), "active": d.get("active"), "type": d.get("type"),
            "identifier": d.get("identifier"), "ref": d.get("ref"), "vcs": (d.get("urls") or {}).get("vcs"),
            **_meta(resp)}


# -- GitHub REST (optional; git answers most questions without it) -------------------------------------------

def github_issue(transport, owner: str, repo: str, number: int) -> dict:
    resp, err = _get(transport, f"https://api.github.com/repos/{owner}/{repo}/issues/{number}",
                     accept="application/vnd.github+json")
    if err:
        return err
    bad = _status_fail(resp, f"GitHub issue {owner}/{repo}#{number}")
    if bad:
        return bad
    try:
        d = resp.json()
    except ValueError:
        return {"ok": False, "reason": "bad_response", "error": "invalid GitHub JSON"}
    return {"ok": True, "number": d.get("number"), "title": d.get("title"), "state": d.get("state"),
            "is_pull_request": bool(d.get("pull_request")), "updated_at": d.get("updated_at"),
            "closed_at": d.get("closed_at"), "html_url": d.get("html_url"), "body": (d.get("body") or "")[:4000],
            **_meta(resp)}


def github_pull(transport, owner: str, repo: str, number: int) -> dict:
    resp, err = _get(transport, f"https://api.github.com/repos/{owner}/{repo}/pulls/{number}",
                     accept="application/vnd.github+json")
    if err:
        return err
    bad = _status_fail(resp, f"GitHub PR {owner}/{repo}#{number}")
    if bad:
        return bad
    try:
        d = resp.json()
    except ValueError:
        return {"ok": False, "reason": "bad_response", "error": "invalid GitHub JSON"}
    return {"ok": True, "state": d.get("state"), "merged": d.get("merged"),
            "merge_commit_sha": d.get("merge_commit_sha"), "base_sha": (d.get("base") or {}).get("sha"),
            "base_ref": (d.get("base") or {}).get("ref"), "head_sha": (d.get("head") or {}).get("sha"),
            "head_ref": (d.get("head") or {}).get("ref"), "title": d.get("title"), **_meta(resp)}
