"""Reference research: a reference repository at a pinned commit, or a document.

Two kinds of reference:

``git`` (GitHub/GitLab/... URLs incl. ``/tree/<ref>``, ``/commit/<sha>``,
``/blob/<ref>/path``, ``url@ref``, scp-style ``git@host:o/r``, local git repos)
    A bare clone is kept (and reused) under ``.verinoda/research/<slug>/mirror.git``.
    The requested ref is resolved to a full SHA and checked out *detached* in a
    per-commit worktree ``.verinoda/research/<slug>/<sha12>``, so evidence that
    cites the checkout (``meta.root``) keeps pointing at the exact version even
    when another version is researched later. A named ref that cannot be
    resolved is an error - the default branch is never used silently. Without a
    ref, default-branch HEAD is pinned and reported as such. Reference strings
    are classified by :mod:`verinoda.references.classify` (compare ranges,
    releases, archives, PR/MR heads, raw/codeload URLs, ``owner/repo``, package
    specs, arXiv, DOI, SWHID): refs are resolved as *qualified* names in the
    namespace the URL implies (a tag/branch name clash is reported as M5),
    classes that name a version by construction are refused without one, and a
    pin chosen by :func:`verinoda.references.resolve` (``pin=``) is checked out
    as is. Compare ranges and PR/MR heads also trace their base.

``local_dir`` (a plain directory, not a git repository)
    Copied to ``.verinoda/research/<slug>/tree-<hash12>`` and pinned by content
    hash (there is no commit to pin).

``document`` (http(s) URLs of docs, standards, papers, articles; local files)
    Fetched with the vendored SSRF-guarded fetcher, HTML reduced to text with
    the stdlib parser, stored with its content hash and ETag/Last-Modified as
    version. Source type is classified (official_doc / standard / paper /
    secondary) unless given. Search-result pages are refused: they are not
    evidence. Network/auth failures give ``status: "unreachable"``.

With a topic, :func:`mechanism` traces how the reference implements it:
retrieval seeds -> call subgraph (depth 2) -> per-symbol facts (data
structures, error handling, concurrency, environment assumptions) with
file:line locators, plus "why" material (``git log -L`` subjects, tests and
docs that mention the symbols). Fact extraction is pattern-based (Python AST;
regex for other languages) and is labelled a heuristic: a missing fact means
"not found in the traced subgraph", never "proven absent".

:func:`compare` runs the same trace on the local project and diffs the fact
sets per category (plus declared dependencies) into shared / only_local /
only_reference / differing, and states explicit unknowns.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.parse
from collections import OrderedDict, defaultdict
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath

from verinoda import evidence as evmod
from verinoda.paths import configure_index_env, ensure_atlas, research_dir
from verinoda.store import Store, new_id, now

configure_index_env()

CATEGORIES = ("data_structures", "error_handling", "concurrency", "environment")
COMPARE_CATEGORIES = CATEGORIES + ("dependencies",)
DOC_KINDS = ("official_doc", "standard", "paper", "secondary")
KINDS = ("auto", "reference_repo", *DOC_KINDS)

GIT_HOSTS = {"github.com", "gitlab.com", "bitbucket.org", "codeberg.org", "git.sr.ht", "gitea.com",
             "salsa.debian.org", "gitee.com"}
_NON_CODE_GITHUB_PAGES = {"issues", "pull", "pulls", "discussions", "wiki", "actions", "security",
                          "pulse", "graphs", "projects", "milestones", "labels"}
_SCP_RE = re.compile(r"^(?P<user>[\w.-]+)@(?P<host>[\w.-]+):(?!//)(?P<path>.+)$")
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
_CODE_SUFFIXES = {".py", ".pyi", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".go", ".rs", ".java", ".kt",
                  ".rb", ".php", ".cs", ".c", ".h", ".cc", ".cpp", ".hpp", ".swift", ".scala", ".lua"}
_DOC_SUFFIXES = {".md", ".rst", ".txt", ".adoc"}

MAX_FACTS_PER_CATEGORY = 250   # hard safety cap in the trace; display caps are much smaller
COMPARE_MAX_SYMBOLS = 48       # compare traces both sides with the same, larger symbol cap
GIT_NET_TIMEOUT = 900
GIT_LOCAL_TIMEOUT = 120
TEXT_MAX_BYTES = 10 * 1024 * 1024
PDF_MAX_BYTES = 50 * 1024 * 1024


# =============================================================================
# small helpers
# =============================================================================

def _tail(text: str, n: int = 300) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else "..." + text[-n:]


def _short(text: str | None, n: int = 120) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= n else text[: n - 3] + "..."


def _slugify(text: str, n: int = 40) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-.")
    return (s or "ref")[:n]


def _h6(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:6]


def _git(cwd: Path | None, *args: str, timeout: float = GIT_LOCAL_TIMEOUT) -> tuple[int, str, str]:
    """Run git without a shell and without interactive prompts."""
    cmd = ["git"]
    if cwd is not None:
        cmd += ["-C", str(cwd)]
    cmd += ["-c", "core.longpaths=true", "-c", "core.autocrlf=false", "-c", "advice.detachedHead=false", *args]
    env = dict(os.environ)
    env.update({"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"})
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


def record_evidence(store: Store, ev: dict | None) -> str | None:
    """Add an evidence row, reusing an identical earlier row (same source,
    locator, commit, hash and checkout root) so repeated research does not
    grow the table. Returns the evidence id (None when ``ev`` is None)."""
    if not ev:
        return None
    root = (ev.get("meta") or {}).get("root")
    rows = store.all(
        "SELECT id, meta FROM evidence WHERE source_type = ? AND locator = ? AND commit_sha IS ?"
        " AND content_hash IS ?",
        (ev["source_type"], ev["locator"], ev.get("commit_sha"), ev.get("content_hash")),
    )
    for r in rows:
        if ((r.get("meta") or {}) if isinstance(r.get("meta"), dict) else {}).get("root") == root:
            return r["id"]
    return evmod.add(store, ev)


def _read_lines(root: Path, rel: str, cache: dict | None = None) -> list[str]:
    key = str(root / rel)
    if cache is not None and key in cache:
        return cache[key]
    try:
        lines = (root / rel).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        lines = []
    if cache is not None:
        cache[key] = lines
    return lines


def _list_files(root: Path) -> list[str]:
    from verinoda.snapshot import list_files

    return list_files(root)


# =============================================================================
# reference parsing
# =============================================================================

def _doc_source_type(host: str, path: str, query: str) -> str:
    """Classify a document URL (heuristic, host/path based)."""
    host = (host or "").lower()
    p = (path or "").lower()
    q = urllib.parse.parse_qs(query or "")
    engine = bool(re.match(r"^(www\.)?(google|bing|duckduckgo|html\.duckduckgo|yandex|baidu|startpage|ecosia)"
                           r"\.[a-z.]+$", host))
    if engine and (p.startswith("/search") or p in ("", "/", "/html", "/html/") or "q" in q or "text" in q):
        return "search_result"
    if host.startswith("search.") or (p.rstrip("/") == "/search" and ("q" in q or "query" in q)):
        return "search_result"
    if host in ("arxiv.org", "export.arxiv.org", "doi.org", "dx.doi.org", "dl.acm.org", "ieeexplore.ieee.org",
                "link.springer.com", "openreview.net", "aclanthology.org", "papers.nips.cc",
                "proceedings.neurips.cc", "proceedings.mlr.press", "www.usenix.org", "usenix.org",
                "sciencedirect.com", "www.sciencedirect.com", "research.google", "semanticscholar.org",
                "www.semanticscholar.org") or p.endswith(".pdf"):
        return "paper"
    if (host in ("www.rfc-editor.org", "rfc-editor.org", "datatracker.ietf.org", "tools.ietf.org",
                 "html.spec.whatwg.org", "spec.whatwg.org", "www.iso.org", "ecma-international.org",
                 "www.ecma-international.org", "tc39.es", "www.unicode.org", "unicode.org")
            or (host in ("www.w3.org", "w3.org") and p.startswith("/tr/"))
            or host.endswith(".spec.whatwg.org")):
        return "standard"
    if (host.startswith(("docs.", "developer.", "developers.", "devdocs.", "api.", "reference."))
            or host.endswith((".readthedocs.io", ".readthedocs.org", ".rtfd.io"))
            or host in ("peps.python.org", "pkg.go.dev", "doc.rust-lang.org", "docs.rs", "learn.microsoft.com",
                        "man7.org", "www.man7.org", "cppreference.com", "en.cppreference.com")
            or (host in ("python.org", "www.python.org") and p.startswith(("/doc", "/dev/peps", "/3/")))
            or (host == "go.dev" and p.startswith(("/doc", "/ref", "/pkg")))
            or (host == "nodejs.org" and "/api" in p)
            or (host == "kubernetes.io" and p.startswith("/docs"))):
        return "official_doc"
    return "secondary"


def parse_reference(ref_str: str, kind: str = "auto") -> dict:
    """Classify a reference string. Raises ValueError when it cannot be used.

    Delegates to :func:`verinoda.references.classify.classify` (the URL/host
    table of docs/DESIGN.md D10) and returns the historical shape ``{"type":
    "git"|"local_dir"|"document"|"package", "url", "path", "ref",
    "ref_candidates": [(ref, subpath), ...], "subpath", "host", "slug",
    "source_type"}`` plus ``class``, ``requested``, ``identity``,
    ``requires_ref``, ``ref_namespace_hint``, ``lines`` and ``compare``. For
    ``/tree/<ref>/...`` URLs whose branch name may contain slashes, every split
    is listed in ``ref_candidates`` (tried in order).
    """
    from verinoda.references.classify import classify, to_legacy

    return to_legacy(classify(ref_str, kind))


# =============================================================================
# git references
# =============================================================================

def _ref_kind(mirror: Path, ref: str, sha: str) -> str:
    if ref.startswith("refs/pull/"):
        return "pr_head"
    if ref.startswith("refs/merge-requests/"):
        return "mr_head"
    if _git(mirror, "show-ref", "--verify", "--quiet", f"refs/tags/{ref}")[0] == 0:
        return "tag"
    if _git(mirror, "show-ref", "--verify", "--quiet", f"refs/heads/{ref}")[0] == 0:
        return "branch"
    if _SHA_RE.match(ref) and sha.lower().startswith(ref.lower()):
        return "commit"
    return "revision"


def _rev(mirror: Path, rev: str) -> str | None:
    rc, out, _ = _git(mirror, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}")
    return out.strip() if rc == 0 and out.strip() else None


def _qualified(mirror: Path, ref: str, hint: str) -> dict | None:
    """Resolve a bare name through ``refs/heads/`` and ``refs/tags/`` explicitly (never git's silent tag-first
    rule): ``hint`` ``heads_first`` follows GitHub's web semantics for tree/blob/compare URLs; otherwise tags win.
    Both existing is reported as a collision (mismatch M5)."""
    br = _rev(mirror, f"refs/heads/{ref}")
    tg = _rev(mirror, f"refs/tags/{ref}")
    if not (br or tg):
        return None
    order = [("branch", br), ("tag", tg)] if hint == "heads_first" else [("tag", tg), ("branch", br)]
    picked = [o for o in order if o[1]]
    out = {"kind": picked[0][0], "sha": picked[0][1], "collision": None}
    if br and tg:
        other = picked[1]
        out["collision"] = {"name": ref, "followed": picked[0][0], "other_kind": other[0], "other_sha": other[1],
                            "branch_sha": br, "tag_sha": tg}
    return out


def _resolve_candidates(mirror: Path, candidates: list[tuple[str, str | None]], *, allow_fetch: bool,
                        notes: list[str], hint: str = "none") -> tuple[str | None, str | None, str | None, str | None]:
    """-> (sha, matched_ref, ref_kind, subpath). A name collision is appended to ``notes`` as ``M5: ...``."""

    def local_try(ref: str) -> tuple[str | None, str | None]:
        if ref.startswith("refs/"):
            sha = _rev(mirror, ref)
            return sha, (_ref_kind(mirror, ref, sha or "") if sha else None)
        q = _qualified(mirror, ref, hint)
        if q:
            if q["collision"]:
                c = q["collision"]
                notes.append(f"M5: '{ref}' is both a branch ({c['branch_sha'][:12]}) and a tag ({c['tag_sha'][:12]}); "
                             f"followed the {c['followed']}")
            return q["sha"], q["kind"]
        if _SHA_RE.match(ref):
            sha = _rev(mirror, ref)
            if sha and sha.lower().startswith(ref.lower()):
                return sha, "commit"
        return None, None

    for ref, sub in candidates:
        sha, kind = local_try(ref)
        if sha:
            return sha, ref, kind, sub
    if not allow_fetch:
        return None, None, None, None
    for ref, sub in candidates:
        if ref.startswith("refs/"):
            rc, _, err = _git(mirror, "fetch", "--quiet", "origin", f"+{ref}:{ref}", timeout=GIT_NET_TIMEOUT)
            if rc != 0:
                notes.append(f"fetch of {ref} failed: {_tail(err, 160)}")
        elif _SHA_RE.match(ref):
            rc, _, err = _git(mirror, "fetch", "--quiet", "origin", ref, timeout=GIT_NET_TIMEOUT)
            if rc == 0:
                sha = _rev(mirror, "FETCH_HEAD")
                if sha and sha.lower().startswith(ref.lower()):
                    return sha, ref, "commit", sub
            notes.append(f"fetch of commit {ref} failed: {_tail(err, 160)}")
            continue
        else:
            _git(mirror, "fetch", "--quiet", "origin", f"+refs/tags/{ref}:refs/tags/{ref}", timeout=GIT_NET_TIMEOUT)
            _git(mirror, "fetch", "--quiet", "origin", f"+refs/heads/{ref}:refs/heads/{ref}", timeout=GIT_NET_TIMEOUT)
        sha, kind = local_try(ref)
        if sha:
            return sha, ref, kind, sub
    return None, None, None, None


def _ensure_worktree(mirror: Path, wt: Path, sha: str) -> str | None:
    """Detached checkout of exactly ``sha`` at ``wt``. Returns an error or None."""
    if (wt / ".git").exists():
        rc, head, _ = _git(wt, "rev-parse", "HEAD")
        if rc == 0 and head.strip() == sha:
            return None
        rc, _, err = _git(wt, "checkout", "--quiet", "--detach", "--force", sha)
        if rc != 0:
            return f"could not check out {sha[:12]} in existing {wt}: {_tail(err)}"
    else:
        _git(mirror, "worktree", "prune")
        rc, _, err = _git(mirror, "worktree", "add", "--quiet", "--detach", "--force", str(wt), sha,
                          timeout=GIT_NET_TIMEOUT)
        if rc != 0:
            return f"git worktree add failed: {_tail(err)}"
    rc, head, _ = _git(wt, "rev-parse", "HEAD")
    if rc != 0 or head.strip() != sha:
        return f"checkout verification failed: HEAD is {head.strip()[:12] or '?'}, expected {sha[:12]}"
    if _git(wt, "symbolic-ref", "-q", "HEAD")[0] == 0:
        return "checkout is on a branch, expected a detached HEAD"
    return None


def _build_index(checkout: Path, version_key: str) -> tuple[dict, bool]:
    """Index a research checkout into its own ``.verinoda/index`` (cached per version)."""
    from verinoda import index
    from verinoda.paths import atlas_dir, graph_path

    if Path(os.environ.get("GRAPHIFY_OUT", "")).is_absolute():
        # One shared absolute output dir would let a reference index overwrite the project's own graph.
        raise RuntimeError("GRAPHIFY_OUT is an absolute path; research checkouts need their own "
                           "per-checkout index - unset GRAPHIFY_OUT (default .verinoda/index)")
    ensure_atlas(checkout)
    marker = atlas_dir(checkout) / "research.json"
    gp = graph_path(checkout)
    try:
        prev = json.loads(marker.read_text(encoding="utf-8")) if marker.exists() else {}
    except (OSError, ValueError):
        prev = {}
    if prev.get("version") == version_key and gp.exists():
        return {"nodes": prev.get("nodes"), "edges": prev.get("edges"), "graph_path": str(gp)}, True
    stats = index.build(checkout, force=True)
    marker.write_text(json.dumps({"version": version_key, "nodes": stats["nodes"], "edges": stats["edges"],
                                  "built_at": now()}), encoding="utf-8")
    return {"nodes": stats["nodes"], "edges": stats["edges"], "graph_path": stats["graph_path"]}, False


def _latest_release_tag(mirror: Path, name: str | None) -> str | None:
    from verinoda.references import versions as vermod

    tags = [t for t in _git(mirror, "tag", "--list")[1].split() if t]
    return vermod.newest(tags, name=name)


def _research_git(store: Store, repo: Path, spec: dict, ref: str | None, res: dict, pin: dict | None = None) -> dict:
    slugdir = research_dir(repo) / spec["slug"]
    mirror = slugdir / "mirror.git"
    source = spec["url"] or spec["path"]
    is_local = spec["url"] is None
    warnings = res["warnings"]
    fetched_at = now()
    cls = spec.get("class")
    if is_local:
        rc, st, _ = _git(Path(spec["path"]), "status", "--porcelain", "--untracked-files=no")
        if rc == 0 and st.strip():
            warnings.append("the reference working tree has uncommitted changes; they are NOT part of the pinned commit")
    if not pin and not ref and spec.get("requires_ref") and not spec.get("ref_candidates"):
        res["status"] = "error"
        res["ref_status"] = "ref_required"
        res["error"] = (f"a {str(cls).replace('_', ' ')} names a version by construction, but no ref could be read "
                        "from the reference; refusing to fall back to the default branch")
        res["next_steps"] = ["give the full URL (compare range, tag, PR/MR number) or pass --ref"]
        return res
    fetch_ok = True
    if not (mirror / "HEAD").exists():
        slugdir.mkdir(parents=True, exist_ok=True)
        rc, _, err = _git(None, "clone", "--bare", "--quiet", str(source), str(mirror), timeout=GIT_NET_TIMEOUT)
        if rc != 0:
            res["status"] = "error" if is_local and rc not in (124,) else "unreachable"
            res["error"] = f"git clone of {source} failed: {_tail(err)}"
            res["next_steps"] = [
                "check the URL, network access and credentials (private repositories need a configured git "
                "credential helper; prompts are disabled)",
                f"test with: git ls-remote {source}",
                "or clone it yourself and pass the local path as the reference",
            ]
            return res
        _git(mirror, "config", "remote.origin.fetch", "+refs/heads/*:refs/heads/*")
        _git(mirror, "config", "core.autocrlf", "false")
        res["clone"] = "cloned"
    else:
        rc, _, err = _git(mirror, "fetch", "--quiet", "--tags", "--force", "origin", timeout=GIT_NET_TIMEOUT)
        fetch_ok = rc == 0
        res["clone"] = "reused+fetched" if fetch_ok else "reused (fetch failed)"
        if not fetch_ok:
            warnings.append(f"could not refresh the cached clone ({_tail(err, 160)}); refs resolved from the "
                            "cache as last fetched")
    notes: list[str] = []
    candidates = [tuple(c) for c in (spec.get("ref_candidates") or [])]
    hint = spec.get("ref_namespace_hint") or "none"
    if ref:
        candidates = [(ref, spec.get("subpath"))]
        hint = "none"
    if pin:
        sha_in = pin.get("sha") or pin.get("value")
        sha = _rev(mirror, sha_in) if sha_in else None
        if not sha and sha_in and (fetch_ok or is_local):
            if str(pin.get("ref") or "").startswith("refs/"):
                _git(mirror, "fetch", "--quiet", "origin", f"+{pin['ref']}:{pin['ref']}", timeout=GIT_NET_TIMEOUT)
            sha = _rev(mirror, sha_in)
            if not sha:
                _git(mirror, "fetch", "--quiet", "origin", sha_in, timeout=GIT_NET_TIMEOUT)
                sha = _rev(mirror, sha_in)
        if not sha:
            res["status"] = "unreachable" if not fetch_ok and not is_local else "error"
            res["error"] = f"the pinned commit {str(sha_in)[:12]} is not available in {source}"
            res["next_steps"] = ["re-resolve the reference: verinoda resolve \"<text>\" --network cache"]
            return res
        kind = pin.get("kind") or "commit"
        name = pin.get("name") or sha[:12]
        res["requested_ref"] = (pin.get("ref") or name) if kind in ("pr_head", "mr_head") else name
        kind = "tag" if kind == "latest_release" else kind
        res["ref_kind"] = "default-branch HEAD" if kind == "default_head" else kind
        res["subpath"] = pin.get("path") if pin.get("path") is not None else spec.get("subpath")
        res["pin_basis"] = pin.get("basis")
        if kind == "default_head":
            res["pin"] = f"pinned default-branch HEAD {sha} ({name}) at {pin.get('retrieved_at') or fetched_at}"
            warnings.append("no ref given: pinned the default branch HEAD; pass --ref to research a specific version")
        else:
            res["pin"] = f"pinned {kind} {name} -> {sha}" + (f" (basis {pin['basis']})" if pin.get("basis") else "")
    elif candidates:
        sha, matched, rkind, sub = _resolve_candidates(mirror, candidates, allow_fetch=fetch_ok or is_local,
                                                       notes=notes, hint=hint)
        for n in notes:
            if n.startswith("M5: "):
                res.setdefault("mismatches", []).append({"code": "M5", "name": "ref_name_collision",
                                                         "severity": "warn", "detail": n[4:]})
        if not sha:
            tried = ", ".join(c for c, _ in candidates)
            offline = not fetch_ok and not is_local
            res["status"] = "unreachable" if offline else "error"
            res["ref_status"] = "unresolved"
            res["error"] = (f"requested ref {tried!r} could not be resolved in {source}"
                            + (" (the cached clone could not be refreshed, so it may exist upstream)" if offline else "")
                            + "; refusing to fall back to the default branch")
            res["next_steps"] = [f"list available refs: git ls-remote --tags --heads {source}",
                                 "pass an existing tag, branch or full commit SHA with --ref"]
            warnings.extend(n for n in notes if not n.startswith("M5: "))
            return res
        warnings.extend(n for n in notes if n.startswith("M5: "))
        res["requested_ref"] = matched
        res["ref_kind"] = rkind
        res["subpath"] = sub
        res["pin"] = f"pinned {rkind} {matched} -> {sha}"
        if rkind == "branch":
            warnings.append(f"'{matched}' is a branch; pinned to its tip {sha[:12]} as fetched at {fetched_at}"
                            + ("" if fetch_ok else " (fetch failed: tip may be outdated)"))
        elif rkind in ("pr_head", "mr_head"):
            warnings.append(f"{matched} is the head of a proposed change as fetched at {fetched_at}; it can move "
                            "(force-push) until merged")
    elif cls == "release_latest":
        tag = _latest_release_tag(mirror, (spec.get("identity") or {}).get("repo"))
        sha = _rev(mirror, f"refs/tags/{tag}") if tag else None
        if not sha:
            res["status"] = "error"
            res["error"] = (f"{source} has no release tags, so '/releases/latest' cannot be pinned "
                            "(no default-branch fallback)")
            return res
        res["requested_ref"] = tag
        res["ref_kind"] = "tag"
        res["subpath"] = spec.get("subpath")
        res["pin"] = f"pinned latest release {tag} -> {sha} as of {fetched_at}"
        warnings.append(f"'/releases/latest' is floating: it resolved to {tag} at {fetched_at}")
    else:
        sha = _rev(mirror, "HEAD")
        if not sha:
            res["status"] = "error"
            res["error"] = f"the reference {source} has no default-branch HEAD (empty repository?)"
            return res
        rc, br, _ = _git(mirror, "symbolic-ref", "--short", "HEAD")
        branch = br.strip() if rc == 0 else "?"
        res["ref_kind"] = "default-branch HEAD"
        res["subpath"] = spec.get("subpath")
        res["pin"] = (f"pinned default-branch HEAD {sha} ({branch}) at {fetched_at}"
                      + ("" if fetch_ok else " from a cached clone (fetch failed; may be outdated)"))
        warnings.append("no ref given: pinned the default branch HEAD; pass --ref to research a specific version")
    tags = [t for t in _git(mirror, "tag", "--points-at", sha)[1].split() if t]
    res["resolved_commit"] = sha
    res["resolved_tag"] = res["requested_ref"] if res.get("ref_kind") == "tag" else (sorted(tags)[0] if tags else None)
    res["tags_at_commit"] = tags[:10]
    wt = slugdir / sha[:12]
    err = _ensure_worktree(mirror, wt, sha)
    if err:
        res["status"] = "error"
        res["error"] = err
        return res
    res["checkout"] = str(wt)
    res["_root"] = wt
    res["_version"] = sha
    res["_mirror"] = mirror
    return res


def _research_local_dir(store: Store, repo: Path, spec: dict, ref: str | None, res: dict) -> dict:
    from verinoda.snapshot import hash_files, tree_hash

    src = Path(spec["path"])
    if ref or spec.get("ref"):
        res["status"] = "error"
        res["error"] = f"{src} is not a git repository, so ref {ref or spec.get('ref')!r} cannot be pinned"
        res["next_steps"] = ["point at the git repository (or its URL) to research a specific version"]
        return res
    files = hash_files(src)
    th = tree_hash(files)
    dst = research_dir(repo) / spec["slug"] / f"tree-{th[:12]}"
    if not dst.exists():
        tmp = dst.with_name(dst.name + ".tmp")
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        for rel in files:
            out = tmp / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(src / rel, out)
            except OSError:
                continue
        tmp.mkdir(parents=True, exist_ok=True)
        # A VCS boundary so ignore rules of the enclosing project do not apply to the copy.
        _git(tmp, "init", "--quiet")
        tmp.rename(dst)
    res["checkout"] = str(dst)
    res["content_hash"] = "sha256:" + th
    res["ref_kind"] = "content hash"
    res["pin"] = f"plain directory (no git): pinned by content hash sha256:{th[:16]} ({len(files)} files) at {now()}"
    res["warnings"].append("not a git repository: no commit, no history; 'why' material is limited to tests and docs")
    res["_root"] = dst
    res["_version"] = "tree:" + th
    return res


# =============================================================================
# documents
# =============================================================================

class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "template", "svg", "canvas", "iframe", "head"}
    BLOCK = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "tr", "table",
             "section", "article", "header", "footer", "blockquote", "dt", "dd", "hr", "main", "nav", "aside",
             "figcaption", "td", "th", "details", "summary"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.main_parts: list[str] = []
        self.skip = 0
        self.in_main = 0
        self.in_title = False
        self.title = ""
        self.in_pre = 0

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self.in_title = True
        if tag in self.SKIP and tag != "head":
            self.skip += 1
        if tag in ("main", "article"):
            self.in_main += 1
        if tag == "pre":
            self.in_pre += 1
        if tag in self.BLOCK:
            self._add("\n")

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        if tag in self.SKIP and tag != "head" and self.skip:
            self.skip -= 1
        if tag in ("main", "article") and self.in_main:
            self.in_main -= 1
        if tag == "pre" and self.in_pre:
            self.in_pre -= 1
        if tag in self.BLOCK:
            self._add("\n")

    def handle_data(self, data):
        if self.in_title:
            self.title += data
            return
        if self.skip:
            return
        self._add(data if self.in_pre else re.sub(r"[ \t\r\f\v]+", " ", data))

    def _add(self, s: str):
        self.parts.append(s)
        if self.in_main:
            self.main_parts.append(s)


def html_to_text(html_text: str) -> tuple[str, str]:
    """HTML -> (title, text). Prefers <main>/<article> content when substantial (heuristic)."""
    p = _TextExtractor()
    try:
        p.feed(html_text)
        p.close()
    except Exception:  # malformed markup: keep what was parsed
        pass
    main = "".join(p.main_parts)
    raw = main if len(main.strip()) > 500 else "".join(p.parts)
    lines, blank = [], False
    for ln in raw.splitlines():
        ln = ln.rstrip()
        if not ln.strip():
            if not blank and lines:
                lines.append("")
            blank = True
            continue
        blank = False
        lines.append(ln.strip() if not ln.startswith("    ") else ln)
    return re.sub(r"\s+", " ", p.title).strip(), "\n".join(lines).strip()


def _fetch(url: str, max_bytes: int, timeout: int = 20) -> dict:
    """SSRF-guarded fetch (vendored project_index.security) that keeps the headers."""
    import urllib.request

    from verinoda.project_index import security as sec

    sec.validate_url(url)
    build = getattr(sec, "_build_opener", None)
    if build is None:  # upstream changed: fall back to the plain safe fetcher (no headers)
        data = sec.safe_fetch(url, max_bytes=max_bytes, timeout=timeout)
        return {"data": data, "content_type": "", "etag": None, "last_modified": None, "final_url": url,
                "http_status": 200}
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; verinoda-research/0.1; evidence fetch)",
        "Accept": "text/html,application/xhtml+xml,text/plain,text/markdown,application/pdf;q=0.9,*/*;q=0.5"})
    with build().open(req, timeout=timeout) as resp:
        status = getattr(resp, "status", None) or getattr(resp, "code", None)
        if status is not None and not (200 <= status < 300):
            raise urllib.error.HTTPError(url, status, f"HTTP {status}", resp.headers, None)
        chunks, total = [], 0
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise OSError(f"response exceeds {max_bytes // 1048576} MB")
            chunks.append(chunk)
        h = resp.headers
        return {"data": b"".join(chunks), "content_type": (h.get("Content-Type") or "").lower(),
                "etag": h.get("ETag"), "last_modified": h.get("Last-Modified"),
                "final_url": resp.geturl() if hasattr(resp, "geturl") else url, "http_status": status}


def _pdf_text(data: bytes) -> str | None:
    try:
        import io

        from pypdf import PdfReader  # optional: pip install verinoda[pdf]
    except ImportError:
        return None
    try:
        reader = PdfReader(io.BytesIO(data))
        return "\n".join((pg.extract_text() or "") for pg in reader.pages)
    except Exception:
        return None


def _topic_terms(topic: str | None) -> list[str]:
    if not topic:
        return []
    try:
        from verinoda.retrieval import terms_for

        terms = terms_for(topic)
    except Exception:
        terms = re.findall(r"\w+", topic.lower())
    return [t for t in terms if len(t) >= 3] or [t for t in re.findall(r"\w+", topic.lower()) if len(t) >= 2]


def _passages(text: str, topic: str | None, limit: int = 5) -> list[dict]:
    terms = _topic_terms(topic)
    if not terms:
        return []
    lines = text.splitlines()
    rx = {t: re.compile(rf"\b{re.escape(t)}", re.I) for t in terms}
    scored = []
    for i, ln in enumerate(lines, 1):
        hit = [t for t, r in rx.items() if r.search(ln)]
        if hit:
            scored.append((len(set(hit)), i, hit))
    need = 2 if len(terms) >= 2 else 1
    best = [x for x in scored if x[0] >= need] or scored
    best.sort(key=lambda x: (-x[0], x[1]))
    out, used = [], set()
    for n, i, hit in best:
        if any(abs(i - u) <= 2 for u in used):
            continue
        used.add(i)
        a, b = max(1, i - 1), min(len(lines), i + 1)
        out.append({"line": i, "lines": [a, b], "terms": sorted(set(hit)),
                    "text": "\n".join(lines[a - 1:b])})
        if len(out) >= limit:
            break
    return out


def _research_document(store: Store, repo: Path, spec: dict, topic: str | None, res: dict) -> dict:
    stype = spec["source_type"]
    res["source_type"] = stype
    if stype == "search_result":
        res["status"] = "error"
        res["refused"] = True
        res["error"] = ("a search-result page is not evidence; open the primary source (official docs, "
                        "standard, paper or repository) and research that URL instead")
        return res
    slugdir = research_dir(repo) / spec["slug"]
    fetched_at = now()
    meta_fetch: dict = {}
    if spec.get("url"):
        url = spec["url"]
        is_pdf_url = urllib.parse.urlparse(url).path.lower().endswith(".pdf")
        try:
            got = _fetch(url, PDF_MAX_BYTES if is_pdf_url or stype == "paper" else TEXT_MAX_BYTES)
        except urllib.error.HTTPError as exc:
            code = exc.code
            why = {401: "authentication required", 403: "forbidden (auth or bot protection)",
                   404: "not found", 407: "proxy authentication required", 410: "gone",
                   429: "rate limited"}.get(code, "server error" if code >= 500 else "HTTP error")
            res["status"] = "unreachable"
            res["error"] = f"HTTP {code} ({why}) for {url}"
            res["next_steps"] = [f"open {url} in a browser to check access",
                                 "for protected pages download the document and research the local file"]
            return res
        except ValueError as exc:  # scheme/SSRF policy or DNS failure (vendored validate_url)
            msg = str(exc)
            res["status"] = "unreachable" if "DNS resolution failed" in msg else "error"
            res["error"] = msg[:300]
            res["next_steps"] = ["check the URL and network access"] if res["status"] == "unreachable" else [
                "only public http(s) URLs are fetched (private/internal addresses are blocked)"]
            return res
        except (urllib.error.URLError, OSError, TimeoutError, ConnectionError) as exc:
            res["status"] = "unreachable"
            res["error"] = f"{type(exc).__name__}: {str(getattr(exc, 'reason', exc))[:300]}"
            res["next_steps"] = ["check network access / proxy settings and retry",
                                 "or download the document and research the local file"]
            return res
        data = got["data"]
        final_url = got["final_url"] or url
        ctype = got["content_type"]
        version = got["etag"] or got["last_modified"]
        meta_fetch = {"requested_url": url, "final_url": final_url, "content_type": ctype, "etag": got["etag"],
                      "last_modified": got["last_modified"], "http_status": got["http_status"]}
    else:
        p = Path(spec["path"])
        data = p.read_bytes()
        final_url = p.as_uri()
        ctype = "application/pdf" if p.suffix.lower() == ".pdf" else (
            "text/html" if p.suffix.lower() in (".html", ".htm") else "text/plain")
        version = None
        meta_fetch = {"path": str(p), "content_type": ctype}
    is_pdf = "pdf" in ctype or data[:5] == b"%PDF-"
    title = ""
    text: str | None
    if is_pdf:
        text = _pdf_text(data)
        if text is None:
            res["unknowns"].append({"question": "document text", "why": "PDF text extraction needs the optional "
                                    "'pypdf' package (pip install verinoda[pdf]) or the PDF has no text layer",
                                    "next_step": "install pypdf, or research an HTML/text version of the paper"})
    else:
        raw = data.decode("utf-8", errors="replace")
        if "html" in ctype or re.search(r"<\s*(html|body|head|div|p)\b", raw[:4000], re.I):
            title, text = html_to_text(raw)
        else:
            text = raw
    raw_hash = "sha256:" + hashlib.sha256(data).hexdigest()
    chash = evmod.content_hash(text) if text is not None else raw_hash
    slugdir.mkdir(parents=True, exist_ok=True)
    text_path = None
    if text is not None:
        text_path = slugdir / f"{chash.split(':')[1][:12]}.txt"
        text_path.write_text(text, encoding="utf-8")
    meta = {**meta_fetch, "title": title or None, "fetched_at": fetched_at, "raw_sha256": raw_hash,
            "text_path": str(text_path) if text_path else None, "chars": len(text or "")}
    if text is not None:
        ev = evmod.url_evidence(final_url, (title + "\n" + text) if title else text, source_type=stype,
                                version=version, meta=meta)
        ev["content_hash"] = chash
    else:
        ev = {"source_type": stype, "locator": final_url, "url": final_url, "version": version,
              "content_hash": raw_hash, "excerpt": None, "meta": meta}
    main_id = record_evidence(store, ev)
    res["evidence_ids"].append(main_id)
    passages = []
    for pas in _passages(text or "", topic):
        pev = {"source_type": stype, "locator": f"{final_url}#text-L{pas['line']}", "url": final_url,
               "version": version, "content_hash": evmod.content_hash(pas["text"]),
               "excerpt": pas["text"][:400],
               "meta": {"document_evidence": main_id, "text_lines": pas["lines"], "terms": pas["terms"],
                        "text_path": str(text_path) if text_path else None, "fetched_at": fetched_at}}
        eid = record_evidence(store, pev)
        res["evidence_ids"].append(eid)
        passages.append({"at": f"text L{pas['lines'][0]}-{pas['lines'][1]}", "terms": pas["terms"],
                         "excerpt": _short(pas["text"], 200), "evidence_id": eid})
    if topic and not passages and text:
        res["unknowns"].append({"question": f"what does the document say about '{topic}'?",
                                "why": "no line of the extracted text mentions the topic terms",
                                "next_step": "rephrase the topic with terms the document uses, or read "
                                             f"{text_path} directly"})
    res.update({
        "url": final_url, "title": title or None, "version": version, "content_hash": chash,
        "version_note": ("ETag" if meta_fetch.get("etag") else "Last-Modified")
        if version else "no ETag/Last-Modified: pinned by content hash only",
        "text_path": str(text_path) if text_path else None, "chars": len(text or ""),
        "passages": passages, "checkout": str(text_path) if text_path else None,
        "pin": f"document {chash[:23]} fetched at {fetched_at}" + (f", version {version}" if version else ""),
    })
    if stype == "secondary":
        res["warnings"].append("classified as a secondary source (non-verifying): it can qualify but never "
                               "verify a claim; prefer official docs, standards, papers or the source code")
    return res


# =============================================================================
# mechanism tracing
# =============================================================================

_CONC_MODULES = ("threading", "_thread", "asyncio", "multiprocessing", "concurrent.futures", "queue", "gevent",
                 "trio", "anyio", "eventlet", "sched")
_NET_MODULES = ("socket", "ssl", "urllib.request", "urllib3", "requests", "httpx", "aiohttp", "http.client",
                "grpc", "websocket", "websockets", "smtplib", "ftplib", "paramiko", "boto3", "botocore",
                "xmlrpc.client")
_DB_MODULES = ("sqlite3", "psycopg2", "psycopg", "pymysql", "MySQLdb", "sqlalchemy", "pymongo", "redis",
               "asyncpg", "aiosqlite", "cx_Oracle", "pyodbc")
_OS_FS = {"os.open", "os.remove", "os.unlink", "os.rename", "os.replace", "os.makedirs", "os.mkdir", "os.rmdir",
          "os.listdir", "os.walk", "os.scandir", "os.stat", "os.chmod", "os.fsync"}
_CONTAINER_CALLS = {
    "dict": "dict", "list": "list", "set": "set", "frozenset": "frozenset", "bytearray": "bytearray",
    "collections.defaultdict": "defaultdict", "collections.OrderedDict": "OrderedDict",
    "collections.deque": "deque", "collections.Counter": "Counter", "collections.namedtuple": "namedtuple",
    "collections.ChainMap": "ChainMap", "heapq.heappush": "heap", "heapq.heapify": "heap",
    "bisect.insort": "sorted list (bisect)", "array.array": "array", "weakref.WeakValueDictionary":
    "WeakValueDictionary", "weakref.WeakKeyDictionary": "WeakKeyDictionary", "weakref.WeakSet": "WeakSet",
    "functools.lru_cache": "lru_cache (memo)", "functools.cache": "cache (memo)", "typing.NamedTuple": "NamedTuple",
    "dataclasses.field": "dataclass field",
}
_PLATFORM_CALLS = {"platform.system", "platform.platform", "platform.machine", "platform.release",
                   "platform.python_implementation", "sys.getwindowsversion"}
_PLATFORM_ATTRS = {"sys.platform", "os.name", "sys.version_info", "os.sep", "sys.byteorder"}
_LOG_ATTRS = {"debug", "info", "warning", "warn", "error", "exception", "critical", "log", "print"}
_RETRY_NAME = re.compile(r"^(Retry\w*|\w*[Rr]etr(y|ies)\w*|[Bb]ackoff\w*|\w*_backoff\w*)$")
_URL_RE = re.compile(r"^(https?|wss?|ftp|grpc)://([^/\s:?#]+)")
_ABS_PATH_RE = re.compile(r"^(/(etc|var|tmp|usr|opt|home|dev|proc|sys|srv|run|mnt|Users)(/|$)|[A-Za-z]:\\\\?|~[/\\])")


def _dotted(node) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else None
    if isinstance(node, ast.Call):
        base = _dotted(node.func)
        return f"{base}()" if base else None
    return None


def _unparse(node, n: int = 80) -> str:
    try:
        return _short(ast.unparse(node), n)
    except Exception:
        return "?"


class _PyModule:
    """Import aliases and module-level facts of one Python file."""

    def __init__(self, tree: ast.AST):
        self.aliases: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.asname:
                        self.aliases[a.asname] = a.name
                    else:
                        self.aliases.setdefault(a.name.split(".")[0], a.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                mod = ("." * (node.level or 0)) + (node.module or "")
                for a in node.names:
                    if a.name != "*":
                        self.aliases[a.asname or a.name] = f"{mod}.{a.name}" if mod else a.name
        self.module_facts: dict[str, list[dict]] = defaultdict(list)
        self.top_stmts: list[ast.stmt] = list(getattr(tree, "body", []))

    def canonical(self, dotted: str | None) -> str | None:
        if not dotted:
            return None
        first, _, rest = dotted.partition(".")
        if first in self.aliases:
            return self.aliases[first] + ("." + rest if rest else "")
        return dotted


def _exc_names(node, mod: _PyModule) -> list[str]:
    if node is None:
        return []
    if isinstance(node, ast.Tuple):
        out = []
        for e in node.elts:
            out += _exc_names(e, mod)
        return out
    d = _dotted(node.func if isinstance(node, ast.Call) else node)
    if not d:
        return [_unparse(node, 40)]
    return [d.rpartition(".")[2] if "." in d and d.split(".")[0] in mod.aliases else d]


def _is_retry_loop(loop) -> bool:
    if isinstance(loop, ast.While):
        return True
    if isinstance(loop, (ast.For, ast.AsyncFor)):
        it = loop.iter
        if isinstance(it, ast.Call) and _dotted(it.func) in ("range", "itertools.count", "count"):
            return True
        tgt = _unparse(loop.target, 40).lower()
        return bool(re.search(r"attempt|retr|tries|try_|backoff", tgt))
    return False


def _handler_behaviour(h: ast.ExceptHandler, loop, mod: _PyModule) -> list[str]:
    beh: list[str] = []
    nodes = [n for s in h.body for n in ast.walk(s)]
    raises = [n for n in nodes if isinstance(n, ast.Raise)]
    if any(r.exc is None for r in raises):
        beh.append("re-raises {t}")
    for r in raises:
        if r.exc is not None:
            for name in _exc_names(r.exc, mod)[:1]:
                beh.append("translates {t} -> " + name)
    has_continue = any(isinstance(n, ast.Continue) for n in nodes)
    returns = [n for n in nodes if isinstance(n, ast.Return)]
    breaks = any(isinstance(n, ast.Break) for n in nodes)
    if loop is not None and not raises and not returns and not breaks:
        beh.append("retries on {t}" if _is_retry_loop(loop) else "skips iteration on {t}")
    elif has_continue and loop is not None:
        beh.append("retries on {t}" if _is_retry_loop(loop) else "skips iteration on {t}")
    if returns:
        beh.append("error-return on {t}")
    logs = any(isinstance(n, ast.Call) and isinstance(n.func, (ast.Attribute, ast.Name))
               and (getattr(n.func, "attr", None) or getattr(n.func, "id", None)) in _LOG_ATTRS for n in nodes)
    if logs:
        beh.append("logs {t}")
    if not raises and not returns and loop is None:
        trivial = all(isinstance(s, ast.Pass) or (isinstance(s, ast.Expr) and isinstance(s.value, (ast.Constant, ast.Call)))
                      for s in h.body)
        if trivial:
            beh.append("swallows {t}")
    return beh


class _PyVisitor(ast.NodeVisitor):
    """Collect fact candidates from a function/class body (pattern based = heuristic)."""

    def __init__(self, emit, mod: _PyModule):
        self.emit = emit
        self.mod = mod
        self.loops: list[ast.AST] = []
        self.in_handler = 0
        self.names_used: set[str] = set()

    def canon(self, node) -> str | None:
        return self.mod.canonical(_dotted(node))

    def _lockish(self, d: str | None) -> bool:
        if not d:
            return False
        last = d.rpartition(".")[2].lower()
        if re.search(r"lock|mutex|semaphore|(^|_)sem$|cond(ition)?$", last):
            return True
        base = d.split(".")[0]
        return any(f["key"].endswith(("Lock", "RLock", "Semaphore", "BoundedSemaphore", "Condition"))
                   for f in self.mod.module_facts.get(base, []))

    # loops -----------------------------------------------------------------
    def _loop(self, node):
        self.loops.append(node)
        self.generic_visit(node)
        self.loops.pop()

    visit_For = visit_While = _loop

    def visit_AsyncFor(self, node):
        self.emit("concurrency", "async for", node.lineno)
        self._loop(node)

    # errors ------------------------------------------------------------------
    def visit_Try(self, node):
        loop = self.loops[-1] if self.loops else None
        for h in node.handlers:
            types = _exc_names(h.type, self.mod) if h.type is not None else ["<bare except>"]
            beh = _handler_behaviour(h, loop, self.mod)
            for t in types:
                self.emit("error_handling", f"catches {t}", h.lineno)
                for b in beh:
                    key = b.format(t=t)
                    if key == f"translates {t} -> {t}":
                        key = f"re-raises {t} (new instance)"
                    self.emit("error_handling", key, h.lineno)
            if loop is not None and any(b.startswith("retries") for b in beh):
                desc = _unparse(loop.iter if isinstance(loop, (ast.For, ast.AsyncFor)) else loop.test, 50)
                self.emit("error_handling", "retry loop", loop.lineno, note=f"loop over {desc} (heuristic)")
        if node.finalbody:
            self.emit("error_handling", "finally cleanup", node.finalbody[0].lineno)
        for s in node.body:
            self.visit(s)
        for h in node.handlers:
            self.in_handler += 1
            for s in h.body:
                self.visit(s)
            self.in_handler -= 1
        for s in node.orelse + node.finalbody:
            self.visit(s)

    visit_TryStar = visit_Try

    def visit_Raise(self, node):
        if not self.in_handler and node.exc is not None:
            for name in _exc_names(node.exc, self.mod)[:1]:
                self.emit("error_handling", f"raises {name}", node.lineno)
        self.generic_visit(node)

    def visit_Assert(self, node):
        self.emit("error_handling", "assert", node.lineno)
        self.generic_visit(node)

    # concurrency --------------------------------------------------------------
    def _with(self, node, is_async: bool):
        if is_async:
            self.emit("concurrency", "async with", node.lineno)
        for item in node.items:
            e = item.context_expr
            d = self.canon(e.func if isinstance(e, ast.Call) else e)
            if d and d.startswith("contextlib.suppress") and isinstance(e, ast.Call):
                for a in e.args:
                    for n in _exc_names(a, self.mod):
                        self.emit("error_handling", f"swallows {n}", node.lineno, note="contextlib.suppress")
            elif self._lockish(d):
                self.emit("concurrency", "lock held (with)", node.lineno, note=f"with {d}")
        self.generic_visit(node)

    def visit_With(self, node):
        self._with(node, False)

    def visit_AsyncWith(self, node):
        self._with(node, True)

    def visit_Await(self, node):
        self.emit("concurrency", "await", node.lineno)
        self.generic_visit(node)

    def visit_Global(self, node):
        self.emit("concurrency", "global mutable state", node.lineno,
                  note=f"global {', '.join(node.names)} (shared state; heuristic)")

    # calls ------------------------------------------------------------------------
    def visit_Call(self, node):
        d = self.canon(node.func)
        ln = node.lineno
        if d:
            last = d.rpartition(".")[2]
            if d == "time.sleep":
                if self.loops:
                    self.emit("error_handling", "sleep in loop (backoff/poll)", ln)
                else:
                    self.emit("concurrency", "blocking sleep (time.sleep)", ln)
            elif any(d == m or d.startswith(m + ".") for m in _CONC_MODULES):
                self.emit("concurrency", d, ln)
            elif last in ("acquire", "release") and self._lockish(d.rpartition(".")[0]):
                self.emit("concurrency", "lock acquire/release", ln)
            if d in ("os.environ.get", "os.getenv", "os.environ.setdefault", "os.environ.pop", "os.putenv"):
                arg = node.args[0] if node.args else None
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    self.emit("environment", f"env {arg.value}", ln)
                else:
                    self.emit("environment", "env (dynamic name)", ln)
            elif d in _PLATFORM_CALLS:
                self.emit("environment", f"platform check {d}()", ln)
            if isinstance(node.func, ast.Attribute) and self.canon(node.func.value) in _PLATFORM_ATTRS:
                args = ",".join(repr(a.value) for a in node.args if isinstance(a, ast.Constant))
                self.emit("environment", f"platform check {self.canon(node.func.value)}.{node.func.attr}({args})", ln)
            segs = [s for s in d.split(".") if _RETRY_NAME.search(s)
                    and not s.endswith(("Error", "Exception", "Warning"))]
            retry_seg = next((s for s in segs if s[:1].isupper()), segs[0] if segs else None)
            if retry_seg and not (last in ("sleep",) or d.startswith(("time.", "asyncio."))):
                self.emit("error_handling", f"retry policy {retry_seg}", ln, note=d)
            else:
                for m in _NET_MODULES:
                    if d == m or d.startswith(m + "."):
                        self.emit("environment", f"network {m}", ln, note=d)
                        break
            top = d.split(".")[0]
            if top in _DB_MODULES:
                self.emit("environment", f"database {top}", ln, note=d)
            if d in ("open", "io.open", "codecs.open") or d in _OS_FS:
                self.emit("environment", "filesystem open/os file ops", ln, note=d)
            elif d.startswith(("os.path.", "shutil.", "tempfile.", "glob.", "pathlib.")):
                self.emit("environment", f"filesystem {d.split('.')[0] if not d.startswith('os.path') else 'os.path'}", ln, note=d)
            if d.startswith("subprocess.") or d in ("os.system", "os.popen", "os.execv", "os.spawnv"):
                self.emit("environment", "subprocess", ln, note=d)
            kind = _CONTAINER_CALLS.get(d)
            if kind:
                self.emit("data_structures", f"container {kind}", ln)
        for kw in node.keywords:
            if kw.arg == "timeout":
                self.emit("environment", "timeout parameter", ln, note=f"timeout={_unparse(kw.value, 40)}")
            elif kw.arg and _RETRY_NAME.search(kw.arg):
                self.emit("error_handling", "retry policy (argument)", ln, note=f"{kw.arg}={_unparse(kw.value, 40)}")
        self.generic_visit(node)

    def visit_Subscript(self, node):
        if self.canon(node.value) == "os.environ":
            sl = node.slice
            if isinstance(sl, ast.Constant) and isinstance(sl.value, str):
                self.emit("environment", f"env {sl.value}", node.lineno)
        self.generic_visit(node)

    def visit_Compare(self, node):
        ops = [node.left, *node.comparators]
        platform = None
        for o in ops:
            d = self.canon(o.func) + "()" if isinstance(o, ast.Call) and self.canon(o.func) else self.canon(o)
            if d in _PLATFORM_ATTRS or (d and d.rstrip("()") in _PLATFORM_CALLS):
                platform = d
        if platform:
            vals = [repr(o.value) for o in ops if isinstance(o, ast.Constant)]
            self.emit("environment", f"platform check {platform}" + (f" vs {','.join(vals)}" if vals else ""),
                      node.lineno)
        self.generic_visit(node)

    def visit_Name(self, node):
        self.names_used.add(node.id)

    # data ---------------------------------------------------------------------------
    def _container(self, node, kind: str):
        self.emit("data_structures", f"container {kind}", node.lineno)
        self.generic_visit(node)

    def visit_Dict(self, node):
        self._container(node, "dict literal")

    def visit_List(self, node):
        if isinstance(getattr(node, "ctx", None), ast.Load):
            self._container(node, "list literal")
        else:
            self.generic_visit(node)

    def visit_Set(self, node):
        self._container(node, "set literal")

    def visit_DictComp(self, node):
        self._container(node, "dict comprehension")

    def visit_ListComp(self, node):
        self._container(node, "list comprehension")

    def visit_SetComp(self, node):
        self._container(node, "set comprehension")

    def visit_Constant(self, node):
        if isinstance(node.value, str) and len(node.value) < 300:
            m = _URL_RE.match(node.value)
            if m:
                self.emit("environment", f"network url {m.group(1)}://{m.group(2)}", node.lineno)
            elif _ABS_PATH_RE.match(node.value):
                self.emit("environment", f"filesystem literal {_short(node.value, 40)}", node.lineno)


def _class_kind(cls: ast.ClassDef, mod: _PyModule) -> tuple[str, bool]:
    decos = [mod.canonical(_dotted(d.func if isinstance(d, ast.Call) else d)) or "" for d in cls.decorator_list]
    bases = [mod.canonical(_dotted(b)) or _unparse(b, 40) for b in cls.bases]
    blast = [b.rpartition(".")[2] for b in bases]
    is_exc = any(b.endswith(("Error", "Exception", "Warning")) or b in ("Exception", "BaseException")
                 for b in blast)
    if any(d.endswith("dataclass") for d in decos):
        return "dataclass", is_exc
    if any(d.startswith(("attr.", "attrs.")) or d in ("attr.s", "define", "frozen") for d in decos):
        return "attrs class", is_exc
    if "NamedTuple" in blast:
        return "NamedTuple", is_exc
    if "TypedDict" in blast:
        return "TypedDict", is_exc
    if any(b in ("Enum", "IntEnum", "StrEnum", "Flag", "IntFlag") for b in blast):
        return "Enum", is_exc
    if "Protocol" in blast:
        return "Protocol", is_exc
    if "BaseModel" in blast:
        return "pydantic model", is_exc
    if is_exc:
        return "exception class", True
    return "class", False


def _py_node_at(tree: ast.AST, line: int):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            first = min([node.lineno] + [d.lineno for d in node.decorator_list])
            if line in (node.lineno, first):
                return node
    return None


def _module_facts(mod: _PyModule, rel: str) -> None:
    """Facts of module-level assignments, keyed by assigned name (e.g. ``_lock = threading.Lock()``)."""
    for st in mod.top_stmts:
        if not isinstance(st, (ast.Assign, ast.AnnAssign)) or st.value is None:
            continue
        targets = st.targets if isinstance(st, ast.Assign) else [st.target]
        names = [t.id for t in targets if isinstance(t, ast.Name)]
        if not names:
            continue
        found: list[dict] = []

        def emit(cat, key, line, note=None, _found=found):
            _found.append({"category": cat, "key": key, "line": line, "note": note})

        _PyVisitor(emit, mod).visit(st.value)
        for n in names:
            mod.module_facts[n] = found


def _py_symbol_facts(root: Path, rel: str, sym_line: int, label: str, cache: dict) -> list[dict] | None:
    from verinoda.index import _py_ast

    key = ("mod", str(root / rel))
    if key not in cache:
        try:
            tree = _py_ast(root / rel)
        except (SyntaxError, ValueError, OSError):
            cache[key] = None
        else:
            mod = _PyModule(tree)
            _module_facts(mod, rel)
            cache[key] = (tree, mod)
    entry = cache[key]
    if entry is None:
        return None
    tree, mod = entry
    node = _py_node_at(tree, sym_line)
    if node is None:
        return None
    facts: list[dict] = []

    def emit(cat, k, line, note=None):
        facts.append({"category": cat, "key": k, "path": rel, "line": line, "symbol": label,
                      "method": "python-ast", "note": note})

    v = _PyVisitor(emit, mod)
    if isinstance(node, ast.ClassDef):
        kind, is_exc = _class_kind(node, mod)
        fields = []
        for st in node.body:
            if isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name):
                fields.append(f"{st.target.id}: {_unparse(st.annotation, 30)}")
            elif isinstance(st, ast.Assign):
                fields += [t.id for t in st.targets if isinstance(t, ast.Name)]
        bases = ", ".join(_unparse(b, 30) for b in node.bases)
        emit("data_structures", f"{kind} {node.name}", node.lineno,
             note=f"{kind} {node.name}" + (f"({bases})" if bases else "")
             + (f"; fields: {', '.join(fields[:8])}" if fields else ""))
        if is_exc:
            emit("error_handling", f"defines exception {node.name}", node.lineno, note=f"class {node.name}({bases})")
        for st in node.body:
            if not isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                v.visit(st)
    else:
        if isinstance(node, ast.AsyncFunctionDef):
            emit("concurrency", "async def", node.lineno)
        a = node.args
        for arg in [*a.posonlyargs, *a.args, *a.kwonlyargs, a.vararg, a.kwarg]:
            if arg is not None and arg.annotation is not None:
                emit("data_structures", f"param type {_unparse(arg.annotation, 50)}", arg.annotation.lineno,
                     note=f"{arg.arg}: {_unparse(arg.annotation, 50)}")
        if node.returns is not None:
            emit("data_structures", f"return type {_unparse(node.returns, 50)}", node.returns.lineno)
        body = list(node.body)
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                and isinstance(body[0].value.value, str):
            body = body[1:]  # docstring: prose, not behaviour
        for dec in node.decorator_list:
            d = mod.canonical(_dotted(dec.func if isinstance(dec, ast.Call) else dec)) or ""
            if d in _CONTAINER_CALLS:
                emit("data_structures", f"container {_CONTAINER_CALLS[d]}", dec.lineno)
            if re.search(r"retry|backoff", d, re.I):
                emit("error_handling", f"retry decorator {d}", dec.lineno)
            if re.search(r"lock|synchronized", d, re.I):
                emit("concurrency", f"lock decorator {d}", dec.lineno)
        for st in body:
            v.visit(st)
    for name in sorted(v.names_used):
        for f in mod.module_facts.get(name, []):
            facts.append({"category": f["category"], "key": f["key"], "path": rel, "line": f["line"],
                          "symbol": label, "method": "python-ast",
                          "note": f"module-level `{name}` used by {label}" + (f"; {f['note']}" if f["note"] else "")})
    return facts


_ENV_RX = [re.compile(r"os\.environ\.get\(\s*['\"]([A-Za-z_]\w*)['\"]"),
           re.compile(r"process\.env\.([A-Za-z_]\w*)"),
           re.compile(r"process\.env\[\s*['\"]([A-Za-z_]\w*)['\"]"),
           re.compile(r"os\.(?:Getenv|LookupEnv)\(\s*\"([A-Za-z_]\w*)\""),
           re.compile(r"env::var\(\s*\"([A-Za-z_]\w*)\""),
           re.compile(r"System\.getenv\(\s*\"([A-Za-z_]\w*)\""),
           re.compile(r"Environment\.GetEnvironmentVariable\(\s*\"([A-Za-z_]\w*)\""),
           re.compile(r"ENV\[\s*['\"]([A-Za-z_]\w*)['\"]")]
_RX_FACTS: list[tuple[str, re.Pattern, str, tuple[str, ...] | None]] = [
    ("concurrency", re.compile(r"\bgo\s+(?:func\b|[\w.]+\()"), "goroutine", (".go",)),
    ("concurrency", re.compile(r"\bmake\(\s*chan\b|\bchan\s+[\w*\[]"), "channel", (".go",)),
    ("concurrency", re.compile(r"\bsync\.(Mutex|RWMutex|WaitGroup|Once|Cond|Map)\b"), "sync.{0}", (".go",)),
    ("concurrency", re.compile(r"\bselect\s*\{"), "select", (".go",)),
    ("concurrency", re.compile(r"\.(R?Lock)\(\)"), "lock acquire/release", None),
    ("concurrency", re.compile(r"\bawait\b"), "await", None),
    ("concurrency", re.compile(r"\basync\s+(?:function|fn)\b|\basync\s*\(|\basync\s+\w+\s*\("), "async def", None),
    ("concurrency", re.compile(r"\bPromise\.(all|race|any|allSettled)\b"), "Promise.{0}", None),
    ("concurrency", re.compile(r"\bsynchronized\b"), "synchronized", None),
    ("concurrency", re.compile(r"\b(ExecutorService|ThreadPoolExecutor|CompletableFuture|new\s+Thread)\b"), "{0}", None),
    ("concurrency", re.compile(r"(tokio::spawn|thread::spawn|Arc<Mutex|Mutex<|RwLock<|mpsc::)"), "{0}", (".rs",)),
    ("concurrency", re.compile(r"\block\s*\("), "lock held (with)", (".cs",)),
    ("error_handling", re.compile(r"\bcatch\s*\(\s*(?:final\s+)?([A-Z][\w.]*)(?:\s*\|\s*[\w.]+)*\s+\w+\s*\)"), "catches {0}", None),
    ("error_handling", re.compile(r"\bcatch\b"), "catches (untyped)", (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx")),
    ("error_handling", re.compile(r"\bthrow\s+new\s+([\w.]+)"), "raises {0}", None),
    ("error_handling", re.compile(r"\bif\s+err\s*!=\s*nil"), "error-return (err != nil)", (".go",)),
    ("error_handling", re.compile(r"\bpanic\("), "panic", (".go", ".rs")),
    ("error_handling", re.compile(r"\brecover\(\)"), "recover", (".go",)),
    ("error_handling", re.compile(r"\?\s*;"), "propagates error (?)", (".rs",)),
    ("error_handling", re.compile(r"\.unwrap\(\)|\.expect\("), "unwrap/expect (panics on error)", (".rs",)),
    ("error_handling", re.compile(r"\bfinally\b|\bdefer\b"), "finally cleanup", None),
    ("error_handling", re.compile(r"\b[Rr]etr(y|ies)\b"), "retry (by name; heuristic)", None),
    ("environment", re.compile(r"\bruntime\.GOOS\b|\bprocess\.platform\b|\bRuntimeInformation\b|cfg!\(\s*target_os|System\.getProperty\(\s*\"os\.name"), "platform check", None),
    ("environment", re.compile(r"\bhttp\.(?:Get|Post|NewRequest|Client)\b|\bfetch\(|\baxios\b|\bnet\.Dial|\breqwest::|\bHttpClient\b|\bXMLHttpRequest\b"), "network", None),
    ("environment", re.compile(r"\bos\.(?:Open|Create|ReadFile|WriteFile|MkdirAll)\b|\bfs\.\w+\(|\bFile::(?:open|create)|\bnew\s+File\w*\(|\bioutil\."), "filesystem open/os file ops", None),
    ("environment", re.compile(r"\b[Tt]imeout\b"), "timeout parameter", None),
    ("environment", re.compile(r"\bexec\.Command\b|\bchild_process\b|\bProcessBuilder\b|\bCommand::new\b"), "subprocess", None),
    ("data_structures", re.compile(r"\btype\s+(\w+)\s+struct\b"), "struct {0}", None),
    ("data_structures", re.compile(r"\btype\s+(\w+)\s+interface\b|\binterface\s+(\w+)"), "interface {0}", None),
    ("data_structures", re.compile(r"\b(?:class|record)\s+([A-Z]\w*)"), "class {0}", None),
    ("data_structures", re.compile(r"\benum\s+(\w+)"), "enum {0}", None),
    ("data_structures", re.compile(r"\bmap\[[^\]]+\][\w*\[\]]+|\bnew\s+Map\b|\bHashMap<|\bDictionary<|\bMap<"), "container map", None),
    ("data_structures", re.compile(r"\bVec<|\bnew\s+ArrayList|\bList<|\[\][A-Za-z*]"), "container list/slice", None),
    ("data_structures", re.compile(r"\bnew\s+Set\b|\bHashSet<"), "container set", None),
]


def _rx_symbol_facts(root: Path, rel: str, span: tuple[int, int], label: str, cache: dict) -> list[dict]:
    lines = _read_lines(root, rel, cache)
    suffix = Path(rel).suffix.lower()
    facts = []
    a, b = span
    for i in range(a, min(b, len(lines)) + 1):
        text = lines[i - 1]
        if re.match(r"\s*(//|#|\*|/\*)", text):
            continue
        for rx in _ENV_RX:
            for m in rx.finditer(text):
                facts.append({"category": "environment", "key": f"env {m.group(1)}", "path": rel, "line": i,
                              "symbol": label, "method": "regex-heuristic", "note": None})
        for cat, rx, tmpl, langs in _RX_FACTS:
            if langs and suffix not in langs:
                continue
            m = rx.search(text)
            if m:
                groups = [g for g in m.groups() if g] or [m.group(0)]
                facts.append({"category": cat, "key": tmpl.format(*groups), "path": rel, "line": i,
                              "symbol": label, "method": "regex-heuristic", "note": None})
    return facts


def _dedupe_facts(facts: list[dict], root: Path, cache: dict) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {c: [] for c in CATEGORIES}
    seen: set = set()
    for f in facts:
        k = (f["category"], f["key"], f["path"], f["line"])
        if k in seen or f["category"] not in out:
            continue
        seen.add(k)
        line_text = (_read_lines(root, f["path"], cache)[f["line"] - 1:f["line"]] or [""])[0]
        f = dict(f)
        f["at"] = f"{f['path']}:{f['line']}"
        f["detail"] = _short(f.pop("note") or line_text, 140)
        f["line_text"] = _short(line_text, 140)
        out[f["category"]].append(f)
    return out


def _is_code(rel: str | None) -> bool:
    return bool(rel) and Path(rel).suffix.lower() in _CODE_SUFFIXES


def mechanism(graph, topic: str, root: Path, commit: str | None, *, depth: int = 2, max_symbols: int = 24,
              focus: str | None = None) -> dict:
    """Trace how ``topic`` is implemented in ``graph`` (rooted at ``root``).

    Pure: reads files and git history under ``root``; writes nothing. Facts
    carry ``at`` = file:line at ``commit``. Extraction is a labelled heuristic.
    """
    from verinoda import retrieval
    from verinoda.architecture_map import is_test_file

    root = Path(root).resolve()
    cache: dict = {}
    r = retrieval.retrieve(graph, topic, retrieval.Budget(max_items=12, max_chars=6000))
    terms = r["terms"]

    def ok(n) -> bool:
        f = graph.file(n)
        if not (n in graph.G and graph.is_symbol(n) and _is_code(f) and not is_test_file(f)):
            return False
        label = graph.label(n)
        if label in (f, Path(f).name) or label.endswith("/" + Path(f).name):  # module node (path-disambiguated label)
            return False
        return (root / f).is_file()

    seeds = [it["id"] for it in r["items"] if it["id"] in graph.G and ok(it["id"])]
    if focus:
        seeds = [s for s in seeds if (graph.file(s) or "").startswith(focus.rstrip("/") + "/")
                 or graph.file(s) == focus] or seeds
    seeds = seeds[:6]
    module_lines = [(it["file"], it["lines"][0]) for it in r["items"]
                    if it["symbol"] == "(module level)" and _is_code(it["file"]) and not is_test_file(it["file"])][:4]
    role: "OrderedDict[str, dict]" = OrderedDict()
    for s in seeds:
        role[s] = {"role": "seed", "depth": 0}
    frontier = list(seeds)
    for d in range(1, depth + 1):
        nxt = []
        for n in frontier:
            outs = [(v, "callee") for v, _ in graph.out_edges(n, {"calls"})]
            if graph.G.nodes[n].get("_callable_class") and d == 1:
                outs += [(v, "method") for v, _ in graph.out_edges(n, {"method"})]
            for v, rl in outs:
                if v not in role and ok(v):
                    role[v] = {"role": rl, "depth": d}
                    nxt.append(v)
            if d == 1:
                for u, _ in graph.in_edges(n, {"calls"}):
                    if u not in role and ok(u):
                        role[u] = {"role": "caller", "depth": 1}
                for u, _ in graph.in_edges(n, {"method"}):
                    if u not in role and ok(u):
                        role[u] = {"role": "owner class", "depth": 1}
        frontier = nxt
    involved = sorted(role, key=lambda n: (role[n]["depth"], list(role).index(n)))[:max_symbols]
    truncated_symbols = max(0, len(role) - len(involved))

    facts: list[dict] = []
    symbols = []
    for n in involved:
        f = graph.file(n)
        label = graph.label(n)
        sp = graph.span(n) or (graph.line(n) or 1, graph.line(n) or 1)
        symbols.append({"symbol": label, "at": f"{f}:{sp[0]}-{sp[1]}", "role": role[n]["role"],
                        "depth": role[n]["depth"], "id": n})
        got = None
        if f.endswith((".py", ".pyi")) and graph.line(n):
            got = _py_symbol_facts(root, f, graph.line(n), label, cache)
        if got is None:
            got = _rx_symbol_facts(root, f, sp, label, cache)
        facts.extend(got)
    for f, ln in module_lines:
        if f.endswith(".py"):
            got = _py_symbol_facts(root, f, ln, "(module level)", cache) or []
            if not got:
                got = _rx_symbol_facts(root, f, (ln, ln), "(module level)", cache)
        else:
            got = _rx_symbol_facts(root, f, (ln, ln), "(module level)", cache)
        facts.extend(got)
    by_cat = _dedupe_facts(facts, root, cache)
    truncated = {}
    for c, lst in by_cat.items():
        if len(lst) > MAX_FACTS_PER_CATEGORY:
            truncated[c] = len(lst) - MAX_FACTS_PER_CATEGORY
            by_cat[c] = lst[:MAX_FACTS_PER_CATEGORY]
    inv = set(involved)
    edges = []
    for u, v, d in graph.edges({"calls"}):
        if u in inv and v in inv:
            loc = d.get("source_location") or ""
            edges.append({"from": graph.label(u), "to": graph.label(v), "confidence": d.get("confidence"),
                          "at": f"{d.get('source_file')}:{loc[1:]}" if loc.startswith("L") else d.get("source_file"),
                          **({"derived_by": d["_origin"]} if str(d.get("_origin", "")).startswith("verinoda") else {})})
    why = _why(graph, root, [n for n in involved if role[n]["role"] == "seed"] or involved[:3], involved, terms, cache)
    unknowns = []
    if not seeds:
        unknowns.append({"question": f"where is '{topic}' implemented?",
                         "why": "no code symbol matched the topic terms " + str(terms),
                         "next_step": "rephrase the topic with identifiers used in the code, or pass a symbol name"})
    return {
        "topic": topic, "terms": terms, "root": str(root), "commit": commit,
        "seeds": [graph.label(s) for s in seeds],
        "symbols": symbols, "truncated_symbols": truncated_symbols, "max_symbols": max_symbols,
        "edges": edges[:40], "facts": by_cat, "truncated_facts": truncated, "why": why,
        "files": sorted({graph.file(n) for n in involved}),
        "coverage": {
            "method": "retrieval seeds -> call subgraph (callees depth %d, callers depth 1, methods of seed "
                      "classes) -> Python AST / regex pattern extraction per symbol (+ module-level names they use)"
                      % depth,
            "limits": ["facts are pattern-based (heuristic); a missing fact means 'not found in the traced "
                       "subgraph', not 'absent'",
                       "dynamic dispatch, callbacks, framework wiring and config files are not followed",
                       "non-Python languages use line regexes (labelled regex-heuristic)"],
        },
        "unknowns": unknowns,
    }


_DOC_RE = re.compile(r"(^|/)(readme|changelog|changes|history|news|contributing|design|architecture|security)[^/]*\.(md|rst|txt|adoc)$"
                     r"|(^|/)(docs?|adr|adrs|decisions?|rfcs?|design)/.*\.(md|rst|txt|adoc)$", re.I)


def _why(graph, root: Path, key_nodes: list[str], involved: list[str], terms: list[str], cache: dict) -> dict:
    from verinoda.architecture_map import DOC_DECISION_RE, is_test_file

    out: dict = {"commits": [], "tests": [], "docs": []}
    names = []
    for n in involved:
        nm = graph.label(n).strip(".()").rpartition(".")[2]
        if len(nm) >= 4 and nm not in names and not nm.startswith("__"):
            names.append(nm)
    key_names = [graph.label(n).strip(".()").rpartition(".")[2] for n in key_nodes]
    # git history of the key symbols' line ranges
    is_git = (root / ".git").exists() and _git(root, "rev-parse", "HEAD")[0] == 0
    if is_git:
        seen = set()
        for n in key_nodes[:3]:
            sp, f = graph.span(n), graph.file(n)
            if not sp or not f:
                continue
            rc, log, _ = _git(root, "log", "-n3", "--no-patch", f"-L{sp[0]},{sp[1]}:{f}",
                              "--format=%H%x1f%aI%x1f%s", timeout=60)
            lines = [l for l in log.splitlines() if "\x1f" in l] if rc == 0 else []
            if not lines:
                rc, log, _ = _git(root, "log", "-n3", "--format=%H%x1f%aI%x1f%s", "--", f, timeout=60)
                lines = [l for l in log.splitlines() if "\x1f" in l] if rc == 0 else []
            for line in lines:
                sha, date, subj = line.split("\x1f", 2)
                if sha in seen:
                    continue
                seen.add(sha)
                out["commits"].append({"sha": sha, "date": date, "subject": _short(subj, 160),
                                       "symbol": graph.label(n), "lines": f"{f}:{sp[0]}-{sp[1]}"})
        out["commits"] = out["commits"][:6]
    files = _list_files(root)
    name_rx = [(nm, re.compile(rf"\b{re.escape(nm)}\b")) for nm in names[:12]]
    # tests that mention the symbols: the key (seed) symbols first, then the rest of the subgraph
    key_rx = [(nm, rx) for nm, rx in name_rx if nm in key_names]
    other_rx = [(nm, rx) for nm, rx in name_rx if nm not in key_names]
    test_files = [f for f in files if is_test_file(f) and _is_code(f)]
    per_file: dict[str, int] = defaultdict(int)
    seen_at: set[str] = set()
    for pass_rx in (key_rx, other_rx):
        for f in test_files:
            if len(out["tests"]) >= 8 or not pass_rx:
                break
            lines = _read_lines(root, f, cache)
            for i, text in enumerate(lines, 1):
                hits = [nm for nm, rx in pass_rx if rx.search(text)]
                if not hits or per_file[f] >= 3 or f"{f}:{i}" in seen_at:
                    continue
                per_file[f] += 1
                seen_at.add(f"{f}:{i}")
                test_fn = None
                for j in range(i, 0, -1):
                    m = re.match(r"\s*(?:async\s+)?(?:def|func|function|it|test)\s*\(?\s*['\"]?([\w .-]+)", lines[j - 1])
                    if m and ("test" in m.group(1).lower() or lines[j - 1].lstrip().startswith(("it(", "test("))):
                        test_fn = m.group(1).strip()
                        break
                out["tests"].append({"at": f"{f}:{i}", "mentions": hits[:3], "test": test_fn,
                                     "line": _short(text, 120), "path": f, "line_no": i})
                if len(out["tests"]) >= 8:
                    break
    # docs / decision records / changelogs that mention the symbols or topic terms
    docs = [f for f in files if Path(f).suffix.lower() in _DOC_SUFFIXES and _DOC_RE.search(f)]
    docs.sort(key=lambda f: (0 if DOC_DECISION_RE.search(f) else 1 if not re.search(r"change|history|news", f, re.I) else 2, f))
    term_rx = [re.compile(rf"\b{re.escape(t)}", re.I) for t in terms if len(t) >= 3]
    need = 2 if len(term_rx) >= 2 else 1
    per_doc: dict[str, int] = defaultdict(int)
    for f in docs[:200]:
        if len(out["docs"]) >= 8:
            break
        try:
            if (root / f).stat().st_size > 1_000_000:
                continue
        except OSError:
            continue
        for i, text in enumerate(_read_lines(root, f, cache), 1):
            sym_hits = [nm for nm, rx in name_rx if nm in key_names and rx.search(text)]
            term_hits = sum(1 for rx in term_rx if rx.search(text))
            if (sym_hits or (term_rx and term_hits >= need)) and per_doc[f] < 3:
                per_doc[f] += 1
                out["docs"].append({"at": f"{f}:{i}", "mentions": sym_hits[:3] or [f"{term_hits} topic term(s)"],
                                    "line": _short(text, 140), "path": f, "line_no": i,
                                    "decision_record": bool(DOC_DECISION_RE.search(f))})
                if len(out["docs"]) >= 8:
                    break
    return out


# =============================================================================
# evidence for a mechanism trace
# =============================================================================

def _record_mechanism(store: Store, mech: dict, *, root: Path, commit: str | None, external: bool,
                      label: str, code_type: str | None = None, meta_extra: dict | None = None) -> list[str]:
    """Record fact/why evidence. ``external`` = checkout outside the analysed repo.

    ``code_type`` overrides the evidence type of external code: ``dependency_source``
    (rank 4) when the checkout is the version the project itself locks.
    """
    ids: list[str] = []
    meta_root = {"root": str(root), **(meta_extra or {})} if external else {}
    code_type = code_type or ("reference_repo" if external else "source_code")
    for cat, lst in mech["facts"].items():
        for f in lst:
            ev = evmod.source_evidence(root, f["path"], f["line"], commit=commit, source_type=code_type,
                                       meta={**meta_root, "category": cat, "fact": f["key"], "topic": mech["topic"],
                                             "method": f["method"]})
            eid = record_evidence(store, ev)
            f["evidence_id"] = eid
            if eid:
                ids.append(eid)
    for t in mech["why"]["tests"]:
        # A test that *mentions* a symbol is source, not a test run (test_result needs an actual run).
        ev = evmod.source_evidence(root, t["path"], t["line_no"], commit=commit, source_type=code_type,
                                   meta={**meta_root, "why": "test mentions symbol", "mentions": t["mentions"]})
        t["evidence_id"] = record_evidence(store, ev)
        if t["evidence_id"]:
            ids.append(t["evidence_id"])
    for d in mech["why"]["docs"]:
        ev = evmod.source_evidence(root, d["path"], d["line_no"], commit=commit, source_type="design_doc",
                                   meta={**meta_root, "why": "doc mentions mechanism", "mentions": d["mentions"]})
        d["evidence_id"] = record_evidence(store, ev)
        if d["evidence_id"]:
            ids.append(d["evidence_id"])
    for c in mech["why"]["commits"]:
        ev = {"source_type": "git_history", "locator": f"{label} commit {c['sha']}", "commit_sha": commit,
              "content_hash": evmod.content_hash(c["subject"]), "excerpt": c["subject"],
              "meta": {**meta_root, "commit": c["sha"], "date": c["date"], "symbol": c["symbol"],
                       "lines": c["lines"], "via": "git log -L"}}
        c["evidence_id"] = record_evidence(store, ev)
        if c["evidence_id"]:
            ids.append(c["evidence_id"])
    return ids


def _compact_facts(lst: list[dict], n: int) -> list[dict]:
    return [{"key": f["key"], "at": f["at"], "detail": _short(f["detail"], 100), "symbol": f["symbol"],
             **({"evidence_id": f["evidence_id"]} if f.get("evidence_id") else {}),
             **({"method": f["method"]} if f.get("method") != "python-ast" else {})} for f in lst[:n]]


def compact_mechanism(mech: dict | None, n: int = 8) -> dict | None:
    if not mech:
        return None
    return {
        "topic": mech["topic"], "terms": mech["terms"], "seeds": mech["seeds"],
        "symbols": [{k: s[k] for k in ("symbol", "at", "role")} for s in mech["symbols"][:12]],
        "symbols_total": len(mech["symbols"]) + mech.get("truncated_symbols", 0),
        "edges": mech["edges"][:12],
        "facts": {c: _compact_facts(mech["facts"][c], n) for c in CATEGORIES},
        "fact_counts": {c: len(mech["facts"][c]) + mech.get("truncated_facts", {}).get(c, 0) for c in CATEGORIES},
        "why": {
            "commits": [{k: c.get(k) for k in ("sha", "date", "subject", "symbol", "evidence_id")}
                        for c in mech["why"]["commits"][:5]],
            "tests": [{k: t.get(k) for k in ("at", "test", "mentions", "evidence_id")} for t in mech["why"]["tests"][:5]],
            "docs": [{k: d.get(k) for k in ("at", "line", "decision_record", "evidence_id")} for d in mech["why"]["docs"][:5]],
        },
        "coverage": mech["coverage"], "unknowns": mech["unknowns"],
    }


# =============================================================================
# public API
# =============================================================================

def _new_result(reference: str, ref: str | None, topic: str | None) -> dict:
    return {"id": new_id("rsh"), "status": "ok", "reference": reference, "kind": None, "source_type": None,
            "requested_ref": ref, "resolved_commit": None, "resolved_tag": None, "ref_kind": None, "pin": None,
            "checkout": None, "index": None, "topic": topic, "evidence_ids": [], "warnings": [], "unknowns": [],
            "next_steps": [], "error": None}


def _transport(repo: Path, transport, network: str | None):
    if transport is not None:
        return transport
    from verinoda.references.transport import for_mode

    return for_mode(repo, network or "cache")


def _research_issue(store: Store, repo: Path, spec: dict, topic: str | None, res: dict, transport) -> str:
    """An issue through the host API (GitHub), pinned by ``updated_at`` + content hash; never scraped HTML.

    Returns ``"done"``, or ``"pull"`` when the number is a pull request (the caller researches its head).
    """
    from verinoda.references import registries as reg

    ident, req = spec.get("identity") or {}, spec.get("requested") or {}
    n = req.get("number")
    if ident.get("host") != "github.com" or not (ident.get("owner") and ident.get("repo") and n):
        res["warnings"].append("issue pages of this host are fetched as HTML (no API pin): the version is the "
                               "content hash at retrieval time")
        _research_document(store, repo, spec, topic, res)
        return "done"
    info = reg.github_issue(transport, ident["owner"], ident["repo"], int(n))
    if not info.get("ok"):
        res["status"] = "unreachable" if info.get("reason") in ("offline", "unreachable", "rate_limited") else "error"
        res["error"] = f"issue #{n} could not be read: {info.get('error')}"
        res["next_steps"] = [f"gh api repos/{ident['owner']}/{ident['repo']}/issues/{n}  (authenticated), or retry "
                             "with network access"]
        return "done"
    if info.get("is_pull_request"):
        return "pull"
    text = f"{info.get('title') or ''}\n\n{info.get('body') or ''}"
    url = info.get("html_url") or req.get("url")
    ev = evmod.url_evidence(url, text, source_type="secondary", version=info.get("updated_at"),
                            meta={"issue": n, "state": info.get("state"), "api": info.get("url"),
                                  "retrieved_at": info.get("retrieved_at"), "from_cache": info.get("from_cache"),
                                  "closed_at": info.get("closed_at")})
    res["evidence_ids"].append(record_evidence(store, ev))
    res.update(source_type="secondary", url=url, title=info.get("title"), version=info.get("updated_at"),
               content_hash=ev["content_hash"],
               pin=f"issue #{n} ({info.get('state')}) as updated at {info.get('updated_at')}, content "
                   f"{ev['content_hash'][:23]}")
    res["warnings"].append("an issue is a secondary source (non-verifying): it can qualify but never verify a claim")
    return "done"


def _package_to_git(repo: Path, spec: dict, res: dict, transport, pin: dict | None) -> dict | None:
    """A package spec -> the git spec of its source repository at the version's tag (mapping by tag name)."""
    from verinoda.references import local as localmod
    from verinoda.references import registries as reg

    ident, req = spec.get("identity") or {}, spec.get("requested") or {}
    pkg = ident.get("package") or {}
    eco, name = pkg.get("ecosystem"), pkg.get("name")
    version = (pin or {}).get("version") or req.get("version_text")
    if not version:
        res["status"] = "error"
        res["error"] = f"package {name} names no exact version; research needs one (no 'latest' fallback)"
        res["next_steps"] = [f"verinoda resolve \"{spec.get('input')}\" to pin it, then research the pinned version"]
        return None
    local = localmod.local_versions(repo)
    loc = localmod.best(local, eco, name) if eco else None
    repo_url = ident.get("canonical_url") or (pin or {}).get("repo") or (loc or {}).get("vcs_url")
    via = "declared in the reference" if ident.get("canonical_url") else ("resolution" if (pin or {}).get("repo")
                                                                           else "local lock VCS source")
    if not repo_url and eco == "pypi":
        info = reg.pypi_project(transport, name)
        repo_url = reg.source_repo_from_urls(info.get("project_urls"), info.get("home_page")) if info.get("ok") else None
        via = "PyPI project_urls (registry metadata: a pointer, not verification)"
    if not repo_url:
        res["status"] = "error"
        res["error"] = f"no source repository is known for {eco} package {name}"
        res["next_steps"] = ["give the repository URL with the tag of that version (e.g. <repo-url>@v<version>)"]
        return None
    git_spec = parse_reference(repo_url)
    v = str(version)
    tags = [f"v{v}", v, f"{name}-{v}", f"{name}@{v}", f"release-{v}"]
    git_spec["ref_candidates"] = [(t, req.get("path")) for t in tags]
    git_spec["ref_namespace_hint"] = "tags"
    git_spec["class"] = "git_repo"
    res["mapping"] = {"method": "tag_name", "strength": "tag_name", "repo": repo_url, "version": v, "via": via,
                      "why": "the version was mapped to the repository tag of the same name; the published artifact "
                             "was not compared (content match is the stronger check)"}
    res["warnings"].append(f"{name} {v} mapped to {repo_url} by tag name ({via}); not content-verified")
    if loc and loc.get("version") and str(loc["version"]) == v:
        res["source_type"] = "dependency_source"
        res["local_version"] = {k: loc.get(k) for k in ("version", "source_file", "line", "kind")}
    return git_spec


def _verify_package_source(repo: Path, res: dict, pkg: dict, transport) -> None:
    """Content match (docs/DESIGN.md D14): compare the published sdist with the pinned commit, blob by blob.

    PyPI only (the registry gives the sdist URL and its sha256). The download is checked against that sha256
    before hashing; the result upgrades the mapping to ``content_match`` (verified only when every comparable
    file matches) or records mismatch M8 with the files that differ.
    """
    from verinoda.references import contentmatch as cm
    from verinoda.references import registries as reg

    mapping = res.setdefault("mapping", {})
    if pkg.get("ecosystem") != "pypi":
        mapping["content_match"] = "not attempted: only PyPI sdists are compared"
        return
    info = reg.pypi_project(transport, pkg["name"])
    rel = (info.get("releases") or {}).get(str(mapping.get("version"))) if info.get("ok") else None
    sdist = next((f for f in (rel or {}).get("files") or [] if f.get("packagetype") == "sdist"), None)
    if not sdist or not sdist.get("url"):
        mapping["content_match"] = f"not attempted: no sdist listed ({info.get('reason') or 'registry'})"
        return
    from verinoda.references.transport import CassetteMiss

    try:
        resp = transport.get(sdist["url"])
    except CassetteMiss:
        raise  # a test forgot to record the download: fail loudly
    except Exception as exc:  # noqa: BLE001 - the tag-name mapping still stands, labelled as such
        mapping["content_match"] = f"not attempted: download failed ({type(exc).__name__}: {_short(str(exc), 120)})"
        return
    got = hashlib.sha256(resp.body).hexdigest()
    if not resp.ok or (sdist.get("sha256") and got != sdist["sha256"]):
        mapping["content_match"] = (f"not attempted: the download does not match the registry sha256 "
                                    f"(HTTP {resp.status})")
        return
    art = research_dir(repo) / "artifacts" / sdist["filename"]
    art.parent.mkdir(parents=True, exist_ok=True)
    art.write_bytes(resp.body)
    m = cm.match(res["_mirror"], res["resolved_commit"], art)
    exact = m["total"] > 0 and m["matched"] == m["total"]
    mapping.update(method="content_match" if exact else "tag_name", strength="verified" if exact else "tag_name",
                   content={k: m[k] for k in ("matched", "total", "ratio", "misses", "skipped")},
                   artifact={"filename": sdist["filename"], "sha256": got})
    if exact:
        mapping["why"] = ("every comparable file of the published sdist is byte-identical (git blob hash) to the "
                          "pinned commit; generated packaging files were skipped")
        res["warnings"] = [w for w in res["warnings"] if "not content-verified" not in w]
        res["warnings"].append(f"{sdist['filename']} matches {res['resolved_tag'] or res['resolved_commit'][:12]} "
                               f"file for file ({m['matched']}/{m['total']} by git blob hash)")
    else:
        res.setdefault("mismatches", []).append(
            {"code": "M8", "name": "artifact_source_mismatch", "severity": "warn",
             "detail": f"{sdist['filename']} matches {m['matched']}/{m['total']} files of the pinned commit; "
                       f"differs: {', '.join(m['misses'][:5])}",
             "resolution": "the mapping stays 'tag_name'; the published artifact is not the pinned source"})


def _trace_base(store: Store, repo: Path, spec: dict, res: dict, topic: str | None) -> None:
    """Compare/PR/MR references: resolve the base, list the changed files and trace the base too."""
    mirror = res.get("_mirror")
    head = res.get("resolved_commit")
    if not mirror or not head:
        return
    cls = spec.get("class")
    base = None
    cmp = spec.get("compare") or (spec.get("requested") or {}).get("compare")
    notes: list[str] = []
    if cls == "git_compare" and cmp:
        base, _, _, _ = _resolve_candidates(mirror, [(cmp["base"], None)], allow_fetch=True, notes=notes,
                                            hint="heads_first")
    elif cls in ("pull_request", "merge_request"):
        tip = _rev(mirror, "HEAD")
        base = _git(mirror, "merge-base", head, tip)[1].strip() if tip else None
    if not base:
        res["warnings"].append("the base of the change could not be resolved; only the head is traced")
        return
    changed = [x for x in _git(mirror, "diff", "--name-only", f"{base}...{head}")[1].splitlines() if x][:200]
    res["changed_files"] = changed[:50]
    res["compare_pins"] = {"base": base, "head": head, "base_label": cmp["base"] if cmp else "merge-base with HEAD",
                           "head_label": res.get("requested_ref"), "changed_files": len(changed)}
    if not topic:
        stems = [PurePosixPath(c).stem for c in changed if _is_code(c)]
        topic = " ".join(dict.fromkeys(s for s in stems if s not in ("__init__",)))[:120] or None
        res["topic_seeded_from_changes"] = topic
    wt = research_dir(repo) / spec["slug"] / base[:12]
    err = _ensure_worktree(mirror, wt, base)
    if err:
        res["warnings"].append(f"base checkout failed: {err}")
        return
    from verinoda import index

    _build_index(wt, base)
    res["base_checkout"] = str(wt)
    if topic:
        g = index.load(wt)
        mech = mechanism(g, topic, wt, base)
        res["evidence_ids"] += _record_mechanism(store, mech, root=wt, commit=base, external=True,
                                                 label=f"{ref_label(res)} (base)",
                                                 code_type=res.get("source_type") if res.get("source_type")
                                                 == "dependency_source" else None)
        res["_base_mechanism"] = mech
    res["_base_topic"] = topic


def research_full(store: Store, repo: Path, reference: str, *, ref: str | None = None, topic: str | None = None,
                  kind: str = "auto", pin: dict | None = None, transport=None, network: str | None = None,
                  verify_source: bool = True) -> dict:
    """Like :func:`research` but also returns internal state (``_graph``,
    ``_mechanism``, ``_root``) for :func:`compare_with` and the feedback protocol.

    ``pin`` is a pin chosen by :func:`verinoda.references.resolve` (``{value|sha, kind, name, basis, ref,
    path, url, source_type, resolution_id, reference_id}``): the reference is checked out at exactly that commit
    (or that document URL is fetched) without resolving its ref again. Compare ranges and PR/MR references also
    trace their base; ``transport`` (tests) and ``network`` serve issue and package lookups. A package spec is
    mapped to its repository's tag and, with ``verify_source``, its published sdist is compared with that commit.
    """
    repo = Path(repo).resolve()
    res = _new_result(reference, ref, topic)
    try:
        ensure_atlas(repo)
        try:
            spec = parse_reference(reference, kind)
        except ValueError as exc:
            res["status"] = "error"
            res["error"] = str(exc)
            return _finish(store, res, None)
        res["kind"] = spec["type"]
        res["class"] = spec.get("class")
        if pin:
            res["resolution_id"], res["reference_id"] = pin.get("resolution_id"), pin.get("reference_id")
            res["pin_basis"] = pin.get("basis")
            if ref:
                res["warnings"].append("--ref is ignored: the resolved pin is used")
                ref = None
        cands = spec.get("ref_candidates") or []
        if ref and cands and spec["type"] not in ("document", "package"):
            match = [c for c in cands if c[0] == ref]
            if not match:
                res["status"] = "error"
                res["error"] = (f"conflicting refs: the reference names {cands[0][0]!r}, --ref says {ref!r}; "
                                "give only one")
                return _finish(store, res, spec)
            spec["subpath"] = match[0][1]
        if ref and ref.startswith("-"):
            res["status"] = "error"
            res["error"] = f"invalid ref {ref!r}"
            return _finish(store, res, spec)
        res["requested_ref"] = ref or spec.get("ref")
        package = None
        if spec["type"] == "package":
            package = (spec.get("identity") or {}).get("package") or {}
            git_spec = _package_to_git(repo, spec, res, _transport(repo, transport, network), pin)
            if git_spec is None:
                return _finish(store, res, spec)
            spec = git_spec
            res["kind"] = "git"
            if pin and not (pin.get("sha") or (pin.get("value") and re.fullmatch(r"[0-9a-f]{40}", str(pin["value"])))):
                pin = None  # a package pin is a version, not a commit: resolve its tag in the mirror
        if spec["type"] == "document" and spec.get("class") == "issue" and not pin:
            outcome = _research_issue(store, repo, spec, topic, res, _transport(repo, transport, network))
            if outcome != "pull":
                return _finish(store, res, spec)
            n = spec["requested"]["number"]
            spec = parse_reference(f"{spec['identity']['canonical_url']}/pull/{n}")
            res["kind"], res["class"] = "git", "pull_request"
            res["warnings"].append(f"#{n} is a pull request: its head is researched")
        if spec["type"] == "document":
            if ref:
                res["warnings"].append("--ref is ignored for documents (they are pinned by content hash/ETag)")
            if pin and pin.get("url"):
                spec = dict(spec, url=pin["url"])
                res["reference_url_used"] = pin["url"]
            _research_document(store, repo, spec, topic, res)
            return _finish(store, res, spec)
        if not res.get("source_type"):
            res["source_type"] = (pin or {}).get("source_type") or "reference_repo"
        if spec["type"] == "git":
            _research_git(store, repo, spec, ref, res, pin=pin)
        else:
            _research_local_dir(store, repo, spec, ref, res)
        if res["status"] != "ok":
            return _finish(store, res, spec)
        if package is not None and verify_source:
            _verify_package_source(repo, res, package, _transport(repo, transport, network))
        from verinoda import index

        root: Path = res["_root"]
        stats, reused = _build_index(root, res["_version"])
        res["index"] = {"nodes": stats["nodes"], "edges": stats["edges"], "reused": reused}
        g = index.load(root)
        res["_graph"] = g
        code_type = "dependency_source" if res.get("source_type") == "dependency_source" else None
        meta_extra = {k: v for k, v in (("resolution_id", res.get("resolution_id")),
                                        ("reference_id", res.get("reference_id")),
                                        ("pin_basis", res.get("pin_basis"))) if v}
        if spec.get("class") in ("git_compare", "pull_request", "merge_request") and spec["type"] == "git":
            _trace_base(store, repo, spec, res, topic)
            topic = topic or res.get("topic_seeded_from_changes")
            res["topic"] = topic
        if topic:
            mech = mechanism(g, topic, root, res["resolved_commit"], focus=res.get("subpath"))
            res["evidence_ids"] += _record_mechanism(store, mech, root=root, commit=res["resolved_commit"],
                                                     external=True, label=ref_label(res), code_type=code_type,
                                                     meta_extra=meta_extra)
            res["_mechanism"] = mech
            res["unknowns"] += mech["unknowns"]
        return _finish(store, res, spec)
    except Exception as exc:  # never let research crash the caller; report instead
        res["status"] = "error"
        res["error"] = f"{type(exc).__name__}: {_short(str(exc), 300)}"
        return _finish(store, res, None)


def ref_label(full: dict) -> str:
    """Short human label: repo name (or URL without scheme) @ pinned version."""
    ref = str(full.get("reference") or "")
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", ref):
        base = ref.split("://", 1)[1]
    elif _SCP_RE.match(ref):
        base = ref.split("@", 1)[1]
    else:
        base = Path(ref).name or ref
    ver = (full.get("resolved_commit") or (full.get("content_hash") or "").replace("sha256:", "") or "")[:10]
    return f"{base}@{ver}" if ver else base


def _finish(store: Store, res: dict, spec: dict | None) -> dict:
    res["evidence_ids"] = list(dict.fromkeys(e for e in res.get("evidence_ids", []) if e))
    notes = {"topic": res.get("topic"), "pin": res.get("pin"), "ref_kind": res.get("ref_kind"),
             "resolution_id": res.get("resolution_id"), "reference_id": res.get("reference_id"),
             "pin_basis": res.get("pin_basis"), "mismatches": res.get("mismatches"),
             "compare_pins": res.get("compare_pins"),
             "source_type": res.get("source_type"), "warnings": res.get("warnings", [])[:10],
             "error": res.get("error"), "index": res.get("index"), "subpath": res.get("subpath"),
             "evidence_ids": res.get("evidence_ids", [])[:200], "evidence_count": len(res.get("evidence_ids", [])),
             "version": res.get("version"), "url": res.get("url"), "title": res.get("title")}
    try:
        store.insert("research", {
            "id": res["id"], "reference": res["reference"], "kind": res.get("kind") or "unknown",
            "requested_ref": res.get("requested_ref"), "resolved_commit": res.get("resolved_commit"),
            "resolved_tag": res.get("resolved_tag"), "local_path": res.get("checkout"),
            "content_hash": res.get("content_hash"), "status": res["status"], "notes": notes,
            "created_at": now(),
        })
    except Exception as exc:  # the result is still useful without its row
        res["warnings"].append(f"could not record research row: {exc}")
    return res


def compact(res: dict) -> dict:
    """Small, agent-friendly view of a research result."""
    out = {k: v for k, v in res.items() if not k.startswith("_")}
    out["evidence_count"] = len(res.get("evidence_ids", []))
    out["evidence_ids"] = res.get("evidence_ids", [])[:20]
    if res.get("_mechanism"):
        out["mechanism"] = compact_mechanism(res["_mechanism"])
    if res.get("_base_mechanism"):
        out["base_mechanism"] = compact_mechanism(res["_base_mechanism"], 5)
    return {k: v for k, v in out.items() if v not in (None, [], {}) or k in ("status", "error", "resolved_commit")}


def research(store: Store, repo: Path, reference: str, *, ref: str | None = None, topic: str | None = None,
             kind: str = "auto") -> dict:
    """Inspect a reference (repository at a pinned commit, or a document).

    Never raises for network/auth failures: ``status`` is ``ok``,
    ``unreachable`` or ``error`` and ``error``/``next_steps`` explain.
    """
    return compact(research_full(store, repo, reference, ref=ref, topic=topic, kind=kind))


# -- dependencies ------------------------------------------------------------------

def _norm_dep(name: str, eco: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower() if eco == "python" else name.lower()


def _line_of(lines: list[str], needle: str) -> int:
    for i, ln in enumerate(lines, 1):
        if needle in ln:
            return i
    return 1


def dependencies(root: Path) -> dict:
    """Declared dependencies (pyproject, requirements*, setup.py/cfg, package.json, go.mod, Cargo.toml)."""
    try:
        import tomllib  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover - py3.10
        import tomli as tomllib  # type: ignore[no-redef]

    root = Path(root)
    items: list[dict] = []
    manifests: list[str] = []
    req_rx = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*(.*)$")

    def add(name, spec, rel, lines, needle, scope, eco):
        items.append({"name": _norm_dep(name, eco), "spec": (spec or "").strip() or "*", "scope": scope,
                      "at": f"{rel}:{_line_of(lines, needle)}", "path": rel, "line": _line_of(lines, needle),
                      "ecosystem": eco})

    def py_req(s, rel, lines, scope):
        s = s.split("#")[0].strip()
        if not s or s.startswith(("-", "git+", "http")):
            return
        m = req_rx.match(s)
        if m:
            add(m.group(1), m.group(3).split(";")[0], rel, lines, s[:40], scope, "python")

    p = root / "pyproject.toml"
    if p.is_file():
        rel = "pyproject.toml"
        manifests.append(rel)
        lines = _read_lines(root, rel)
        try:
            data = tomllib.loads(p.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            data = {}
        proj = data.get("project") or {}
        for s in proj.get("dependencies") or []:
            py_req(s, rel, lines, "runtime")
        for grp, lst in (proj.get("optional-dependencies") or {}).items():
            for s in lst or []:
                py_req(s, rel, lines, f"optional:{grp}")
        poetry = ((data.get("tool") or {}).get("poetry") or {})
        for name, spec in (poetry.get("dependencies") or {}).items():
            if name.lower() != "python":
                add(name, spec if isinstance(spec, str) else json.dumps(spec), rel, lines, name, "runtime", "python")
    for rp in sorted(list(root.glob("requirements*.txt")) + list(root.glob("requirements/*.txt"))):
        rel = rp.relative_to(root).as_posix()
        manifests.append(rel)
        lines = _read_lines(root, rel)
        for s in lines:
            py_req(s, rel, lines, "dev" if re.search(r"dev|test|lint|doc", rel, re.I) else "runtime")
    sp = root / "setup.py"
    if sp.is_file():
        rel = "setup.py"
        manifests.append(rel)
        lines = _read_lines(root, rel)
        text = "\n".join(lines)
        for m in re.finditer(r"(install_requires|requires)\s*=\s*\[(.*?)\]", text, re.S):
            for q in re.findall(r"['\"]([^'\"]+)['\"]", m.group(2)):
                py_req(q, rel, lines, "runtime")
    sc = root / "setup.cfg"
    if sc.is_file():
        import configparser

        rel = "setup.cfg"
        cp = configparser.ConfigParser()
        try:
            cp.read(sc, encoding="utf-8")
            val = cp.get("options", "install_requires", fallback="")
        except configparser.Error:
            val = ""
        if val.strip():
            manifests.append(rel)
            lines = _read_lines(root, rel)
            for s in val.splitlines():
                py_req(s, rel, lines, "runtime")
    pj = root / "package.json"
    if pj.is_file():
        rel = "package.json"
        manifests.append(rel)
        lines = _read_lines(root, rel)
        try:
            data = json.loads(pj.read_text(encoding="utf-8", errors="replace"))
        except ValueError:
            data = {}
        for sect, scope in (("dependencies", "runtime"), ("peerDependencies", "peer"),
                            ("devDependencies", "dev"), ("optionalDependencies", "optional")):
            for name, spec in (data.get(sect) or {}).items():
                add(name, str(spec), rel, lines, f'"{name}"', scope, "npm")
    gm = root / "go.mod"
    if gm.is_file():
        rel = "go.mod"
        manifests.append(rel)
        lines = _read_lines(root, rel)
        block = False
        for ln in lines:
            s = ln.split("//")[0].strip()
            if s.startswith("require ("):
                block = True
                continue
            if block and s == ")":
                block = False
                continue
            m = re.match(r"^(?:require\s+)?([\w./~-]+\.[\w./~-]+)\s+(v[\w.+-]+)", s) if (block or s.startswith("require ")) else None
            if m:
                add(m.group(1), m.group(2), rel, lines, m.group(1), "indirect" if "indirect" in ln else "runtime", "go")
    ct = root / "Cargo.toml"
    if ct.is_file():
        rel = "Cargo.toml"
        manifests.append(rel)
        lines = _read_lines(root, rel)
        try:
            data = tomllib.loads(ct.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            data = {}
        for sect, scope in (("dependencies", "runtime"), ("dev-dependencies", "dev"), ("build-dependencies", "build")):
            for name, spec in (data.get(sect) or {}).items():
                v = spec if isinstance(spec, str) else (spec.get("version") if isinstance(spec, dict) else None)
                add(name, v or json.dumps(spec), rel, lines, name, scope, "cargo")
    return {"items": items, "manifests": manifests}


# -- compare ---------------------------------------------------------------------------

def _group(facts: list[dict]) -> "OrderedDict[str, list[dict]]":
    g: OrderedDict[str, list[dict]] = OrderedDict()
    for f in facts:
        g.setdefault(f["key"], []).append(f)
    return g


def _diff_facts(local: list[dict], ref: list[dict], limit: int = 15) -> dict:
    gl, gr = _group(local), _group(ref)
    shared, only_l, only_r = [], [], []
    for k in sorted(set(gl) & set(gr)):  # shared facts: one locator per side keeps the output small
        shared.append({"key": k, "local_at": [gl[k][0]["at"]], "reference_at": [gr[k][0]["at"]]})
    for k in sorted(set(gl) - set(gr)):
        only_l.append({"key": k, "at": [f["at"] for f in gl[k][:3]], "detail": _short(gl[k][0]["detail"], 100),
                       "evidence": [f["evidence_id"] for f in gl[k][:2] if f.get("evidence_id")]})
    for k in sorted(set(gr) - set(gl)):
        only_r.append({"key": k, "at": [f["at"] for f in gr[k][:3]], "detail": _short(gr[k][0]["detail"], 100),
                       "evidence": [f["evidence_id"] for f in gr[k][:2] if f.get("evidence_id")]})
    out = {"shared": shared[:10], "only_local": only_l[:limit], "only_reference": only_r[:limit], "differing": [],
           "counts": {"shared": len(shared), "only_local": len(only_l), "only_reference": len(only_r),
                      "differing": 0, "local_facts": len(local), "reference_facts": len(ref)}}
    return out


def _diff_deps(dl: dict, dr: dict, local_root: Path, ref_root: Path, store: Store, commit_l: str | None,
               commit_r: str | None, limit: int = 20) -> dict:
    def ev(root, it, commit, external):
        e = evmod.source_evidence(root, it["path"], it["line"], commit=commit,
                                  source_type="reference_repo" if external else "source_code",
                                  meta={**({"root": str(root)} if external else {}), "category": "dependencies",
                                        "fact": f"{it['name']} {it['spec']}"})
        return record_evidence(store, e)

    ml = {i["name"]: i for i in dl["items"]}
    mr = {i["name"]: i for i in dr["items"]}
    shared, differing, only_l, only_r = [], [], [], []
    for k in sorted(set(ml) & set(mr)):
        a, b = ml[k], mr[k]
        if a["spec"] == b["spec"]:
            shared.append({"key": k, "spec": a["spec"], "local_at": [a["at"]], "reference_at": [b["at"]]})
        else:
            differing.append({"key": k, "local": a["spec"], "reference": b["spec"], "local_at": [a["at"]],
                              "reference_at": [b["at"]], "evidence": [x for x in (ev(local_root, a, commit_l, False),
                                                                                  ev(ref_root, b, commit_r, True)) if x]})
    for k in sorted(set(ml) - set(mr)):
        only_l.append({"key": k, "spec": ml[k]["spec"], "scope": ml[k]["scope"], "at": [ml[k]["at"]],
                       "evidence": [x for x in [ev(local_root, ml[k], commit_l, False)] if x]})
    for k in sorted(set(mr) - set(ml)):
        only_r.append({"key": k, "spec": mr[k]["spec"], "scope": mr[k]["scope"], "at": [mr[k]["at"]],
                       "evidence": [x for x in [ev(ref_root, mr[k], commit_r, True)] if x]})
    return {"shared": shared[:limit], "only_local": only_l[:limit], "only_reference": only_r[:limit],
            "differing": differing[:limit], "manifests": {"local": dl["manifests"], "reference": dr["manifests"]},
            "counts": {"shared": len(shared), "only_local": len(only_l), "only_reference": len(only_r),
                       "differing": len(differing), "local_facts": len(dl["items"]), "reference_facts": len(dr["items"])}}


def _local_graph(store: Store, local: Path) -> tuple:
    from verinoda import index, workflow

    res = workflow.update(store, local)
    return index.load(local), res["snapshot"], res.get("mode")


def compare_with(store: Store, local_repo: Path, full: dict, topic: str, *, record_claims: bool = True) -> dict:
    """Diff the mechanism facts of the local project against an already
    researched reference (``full`` from :func:`research_full`)."""
    local = Path(local_repo).resolve()
    out = {"status": full["status"], "topic": topic, "local": {"root": str(local)},
           "reference": {"reference": full["reference"], "kind": full.get("kind"), "pin": full.get("pin"),
                         "commit": full.get("resolved_commit"), "tag": full.get("resolved_tag"),
                         "checkout": full.get("checkout")},
           "research_id": full["id"], "categories": {}, "unknowns": [], "claims": [], "warnings": list(full.get("warnings", [])),
           "error": full.get("error"), "next_steps": list(full.get("next_steps", []))}
    if full["status"] != "ok":
        out["unknowns"] = [{"category": c, "missing_on": "reference", "why": f"reference {full['status']}: {full.get('error')}",
                            "next_step": "fix access to the reference and re-run compare"} for c in COMPARE_CATEGORIES]
        return out
    if full.get("kind") == "document":
        out["status"] = "error"
        out["error"] = "compare needs a code reference (git repository or directory); a document cannot be traced"
        out["next_steps"] = ["use `verinoda research <url> --topic ...` and read the recorded passages"]
        return out
    rm = full.get("_mechanism")
    if not rm or rm.get("max_symbols") != COMPARE_MAX_SYMBOLS or rm.get("topic") != topic:
        # Both sides are traced with the same cap so the diff compares like with like.
        rm = mechanism(full["_graph"], topic, full["_root"], full.get("resolved_commit"),
                       max_symbols=COMPARE_MAX_SYMBOLS, focus=full.get("subpath"))
        ids = _record_mechanism(store, rm, root=Path(full["_root"]), commit=full.get("resolved_commit"),
                                external=True, label=ref_label(full),
                                code_type="dependency_source" if full.get("source_type") == "dependency_source"
                                else None)
        full["evidence_ids"] = list(dict.fromkeys(full.get("evidence_ids", []) + ids))
        full["_mechanism"] = rm
    g_local, snap, mode = _local_graph(store, local)
    lcommit = snap.get("commit_sha")
    lm = mechanism(g_local, topic, local, lcommit, max_symbols=COMPARE_MAX_SYMBOLS)
    _record_mechanism(store, lm, root=local, commit=lcommit, external=False, label=local.name)
    out["local"].update({"commit": lcommit, "dirty": bool(snap.get("dirty")), "snapshot": snap["id"],
                         "index_refresh": mode, "seeds": lm["seeds"],
                         "symbols": [{k: s[k] for k in ("symbol", "at", "role")} for s in lm["symbols"][:8]]})
    out["reference"].update({"seeds": rm["seeds"],
                             "symbols": [{k: s[k] for k in ("symbol", "at", "role")} for s in rm["symbols"][:8]]})
    for c in CATEGORIES:
        out["categories"][c] = _diff_facts(lm["facts"][c], rm["facts"][c])
    ref_root = Path(full["_root"])
    out["categories"]["dependencies"] = _diff_deps(dependencies(local), dependencies(ref_root), local, ref_root,
                                                   store, lcommit, full.get("resolved_commit"))
    for side, m in (("local", lm), ("reference", rm)):
        if not m["seeds"]:
            out["unknowns"].append({"category": "all", "missing_on": side,
                                    "why": f"no code symbol matched '{topic}' in the {side} project",
                                    "next_step": "rephrase the topic with identifiers used on that side"})
        if m.get("truncated_symbols"):
            out["unknowns"].append({
                "category": "all", "missing_on": side,
                "why": f"{m['truncated_symbols']} symbol(s) of the {side} call subgraph beyond the cap of "
                       f"{COMPARE_MAX_SYMBOLS} were not traced; their facts are unknown and may explain "
                       f"'only_{'reference' if side == 'local' else 'local'}' items",
                "next_step": "narrow the topic (name the class or function) to trace a smaller subgraph"})
        for c, n in (m.get("truncated_facts") or {}).items():
            out["unknowns"].append({"category": c, "missing_on": side,
                                    "why": f"{n} {c} fact(s) beyond the safety cap were dropped on {side}",
                                    "next_step": "narrow the topic"})
    for c in COMPARE_CATEGORIES:
        cnt = out["categories"][c]["counts"]
        miss = [s for s, n in (("local", cnt["local_facts"]), ("reference", cnt["reference_facts"])) if n == 0]
        if miss:
            what = "declared dependencies (no manifest found)" if c == "dependencies" else f"{c} facts in the traced subgraph"
            out["unknowns"].append({
                "category": c, "missing_on": "both" if len(miss) == 2 else miss[0],
                "why": f"no {what} on {' and '.join(miss)}; absence of evidence, not evidence of absence",
                "next_step": (f"inspect the {c.replace('_', ' ')} of the traced symbols manually, or widen the topic"
                              if c != "dependencies" else "check lock files / build files that this tool does not parse")})
    out["coverage"] = {
        "method": lm["coverage"]["method"] + "; dependencies from manifests",
        "limits": lm["coverage"]["limits"] + ["keys are compared literally (e.g. 'env FOO', 'catches ValueError');"
                                              " renamed-but-equivalent facts show as only_local/only_reference"],
    }
    if record_claims:
        out["claims"] = _compare_claims(store, local, snap, topic, out, full)
    return out


def _compare_claims(store: Store, local: Path, snap: dict, topic: str, cmp: dict, full: dict) -> list[str]:
    from verinoda.claims import Claims

    cl = Claims(store, local)
    label = ref_label(full)
    ids = []
    for c, d in cmp["categories"].items():
        if not (d["only_local"] or d["only_reference"] or d["differing"]):
            continue
        parts = []
        if d["only_local"]:
            parts.append("only local: " + ", ".join(x["key"] for x in d["only_local"][:4]))
        if d["only_reference"]:
            parts.append("only reference: " + ", ".join(x["key"] for x in d["only_reference"][:4]))
        if d["differing"]:
            parts.append("differing: " + ", ".join(f"{x['key']} {x['local']} vs {x['reference']}" for x in d["differing"][:3]))
        text = _short(f"'{topic}': {c} differs between {local.name}@{(snap.get('commit_sha') or 'worktree')[:10]} "
                      f"and {label}: " + "; ".join(parts), 400)
        evs = []
        for x in (d["only_local"] + d["only_reference"] + d["differing"])[:8]:
            evs += [(e, "supports") for e in x.get("evidence", [])[:1]]
        if not evs:
            continue
        prev = store.one("SELECT id FROM claims WHERE text = ? AND snapshot_id = ? AND superseded_by IS NULL"
                         " AND status NOT IN ('stale','contradicted')", (text, snap["id"]))
        if prev:
            ids.append(prev["id"])
            continue
        local_locs = [a for x in d["only_local"] for a in x["at"]] + [a for x in d["differing"] for a in x["local_at"]]
        files = sorted({a.rpartition(":")[0] for a in local_locs})
        c_ = cl.create(text, project=snap["project"], snapshot=snap, subjects=files[:10], status="strong_inference",
                       evidence=evs, kind="comparison",
                       spec={"topic": topic, "category": c, "reference": full["reference"],
                             "reference_commit": full.get("resolved_commit"), "local_commit": snap.get("commit_sha"),
                             "research_id": full["id"]},
                       uncertainties=["the diff is pattern extraction over the traced call subgraph (depth 2); "
                                      "'only on one side' means not found on the other, not proven absent",
                                      "topic relevance of the traced symbols comes from retrieval ranking (heuristic)"],
                       actor="compare")
        ids.append(c_["id"])
    return ids


def compare(store: Store, local_repo: Path, reference: str, *, ref: str | None = None, topic: str) -> dict:
    """Trace ``topic`` in the local project and in the reference (pinned) and diff the assumptions."""
    local = Path(local_repo).resolve()
    try:
        full = research_full(store, local, reference, ref=ref, topic=topic)
        out = compare_with(store, local, full, topic)
        out["research"] = compact(full) if full["status"] != "ok" else {
            k: v for k, v in compact(full).items() if k != "mechanism"}
        return out
    except Exception as exc:  # report, do not crash the caller
        return {"status": "error", "topic": topic, "local": {"root": str(local)}, "reference": {"reference": reference},
                "error": f"{type(exc).__name__}: {_short(str(exc), 300)}", "categories": {}, "unknowns": [],
                "claims": []}


# -- rendering ---------------------------------------------------------------------------

def render(res: dict) -> None:
    print(f"research {res.get('id', '?')}: {res['status']}  [{res.get('kind') or '?'}"
          f"{'/' + res['source_type'] if res.get('source_type') else ''}]  {res.get('reference')}")
    if res.get("error"):
        print(f"  error: {res['error']}")
    if res.get("pin"):
        print(f"  {res['pin']}" + (f"  (tag {res['resolved_tag']})" if res.get("resolved_tag") else ""))
    if res.get("checkout"):
        print(f"  local copy: {res['checkout']}")
    if res.get("index"):
        i = res["index"]
        print(f"  index: {i['nodes']} nodes, {i['edges']} edges{' (cached)' if i.get('reused') else ''}")
    if res.get("title"):
        print(f"  title: {res['title']}")
    if res.get("kind") == "document" and res.get("content_hash"):
        print(f"  content {res['content_hash'][:23]}  version: {res.get('version') or '-'} ({res.get('version_note')})")
    for p in res.get("passages", [])[:5]:
        print(f"  passage {p['at']} [{', '.join(p['terms'])}]: {p['excerpt']}  ({p['evidence_id']})")
    m = res.get("mechanism")
    if m:
        print(f"  topic '{m['topic']}' terms={m['terms']}  seeds: {', '.join(m['seeds'][:5]) or '-'}")
        for s in m["symbols"][:8]:
            print(f"    {s['role']:<11} {s['symbol']}  {s['at']}")
        for c in CATEGORIES:
            fs = m["facts"][c]
            print(f"  {c} ({m['fact_counts'][c]}):" + ("" if fs else " none found in the traced subgraph"))
            for f in fs[:6]:
                print(f"    - {f['key']}  @{f['at']}")
        w = m["why"]
        for c in w["commits"][:3]:
            print(f"  why/commit {c['sha'][:10]} {c['date'][:10]} {c['subject']}")
        for t in w["tests"][:3]:
            print(f"  why/test {t['at']} {t.get('test') or ''} mentions {', '.join(t['mentions'])}")
        for d in w["docs"][:3]:
            print(f"  why/doc {d['at']}: {d['line']}")
    for wmsg in res.get("warnings", [])[:5]:
        print(f"  warning: {wmsg}")
    for u in res.get("unknowns", [])[:5]:
        print(f"  unknown: {u.get('question')}: {u.get('why')}\n    next: {u.get('next_step')}")
    for s in res.get("next_steps", [])[:4]:
        print(f"  next: {s}")
    if res.get("evidence_count"):
        print(f"  {res['evidence_count']} evidence row(s) recorded (first: {', '.join(res.get('evidence_ids', [])[:3])})")


def render_compare(res: dict) -> None:
    ref = res.get("reference", {})
    loc = res.get("local", {})
    print(f"compare '{res.get('topic')}': {res['status']}")
    print(f"  local:     {loc.get('root')} @ {(loc.get('commit') or 'no-git')[:10]}{' (dirty)' if loc.get('dirty') else ''}"
          f"  seeds: {', '.join(loc.get('seeds', [])[:4]) or '-'}")
    print(f"  reference: {ref.get('reference')}  {ref.get('pin') or ''}"
          f"  seeds: {', '.join(ref.get('seeds', [])[:4]) or '-'}")
    if res.get("error"):
        print(f"  error: {res['error']}")
    for c, d in res.get("categories", {}).items():
        n = d["counts"]
        print(f"  == {c}: shared {n['shared']}, only local {n['only_local']}, only reference {n['only_reference']}"
              + (f", differing {n['differing']}" if n.get("differing") else ""))
        for x in d["only_local"][:5]:
            print(f"     local only: {x['key']}  @{', '.join(x['at'][:2])}")
        for x in d["only_reference"][:5]:
            print(f"     ref only:   {x['key']}  @{', '.join(x['at'][:2])}")
        for x in d.get("differing", [])[:5]:
            print(f"     differs:    {x['key']} local {x['local']} @{x['local_at'][0]} vs ref {x['reference']} @{x['reference_at'][0]}")
    for u in res.get("unknowns", [])[:8]:
        print(f"  unknown [{u['category']}, {u['missing_on']}]: {u['why']}\n    next: {u['next_step']}")
    for w in res.get("warnings", [])[:4]:
        print(f"  warning: {w}")
    if res.get("claims"):
        print(f"  claims: {', '.join(res['claims'])}")
