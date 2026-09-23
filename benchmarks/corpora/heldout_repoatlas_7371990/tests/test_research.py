"""Tests for repoatlas.research (no network: local git fixtures, fake fetchers, refused localhost ports)."""

import os

os.environ.setdefault("GRAPHIFY_OUT", ".repoatlas/index")

import json  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import urllib.error  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from repoatlas import evidence as evmod  # noqa: E402
from repoatlas import research as rs  # noqa: E402
from repoatlas.store import open_store  # noqa: E402

V1 = '''"""Fetch client."""
import os


class FetchError(Exception):
    pass


def fetch(url: str) -> str:
    timeout = float(os.environ.get("FETCH_TIMEOUT", "5"))
    try:
        return _send(url, timeout)
    except ConnectionError as exc:
        raise FetchError(f"cannot reach {url}") from exc


def _send(url: str, timeout: float) -> str:
    return f"{url}:{timeout}"
'''

HEAD = '''"""Fetch client."""
import os
import threading
import time

_lock = threading.Lock()
MAX_ATTEMPTS = 3


class FetchError(Exception):
    pass


def fetch(url: str) -> str | None:
    timeout = float(os.environ.get("FETCH_TIMEOUT_S", "5"))
    for attempt in range(MAX_ATTEMPTS):
        with _lock:
            try:
                return _send(url, timeout)
            except ConnectionError:
                time.sleep(0.1 * attempt)
                continue
    return None


def _send(url: str, timeout: float) -> str:
    return f"{url}:{timeout}"
'''


def _git(root: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t",
                        "-c", "core.autocrlf=false", "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false", *args],
                       capture_output=True, text=True, encoding="utf-8", check=True)
    return r.stdout.strip()


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture
def fx(tmp_path):
    """Two commits; tag v1 on the first. fetch() fails fast at v1, retries under a lock at HEAD."""
    root = tmp_path / "fetchlib"
    _write(root / "fetcher" / "__init__.py", "")
    _write(root / "fetcher" / "client.py", V1)
    _write(root / "tests" / "test_client.py",
           "from fetcher.client import fetch\n\n\ndef test_fetch_returns_body():\n    assert fetch('x')\n")
    _write(root / "docs" / "design.md", "# Design\n\n`fetch` raises FetchError when the server is unreachable.\n")
    _write(root / "pyproject.toml", '[project]\nname = "fx"\nversion = "1"\ndependencies = ["requests>=2.0"]\n')
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "fetch: fail fast with FetchError")
    _git(root, "tag", "v1")
    v1 = _git(root, "rev-parse", "HEAD")
    _write(root / "fetcher" / "client.py", HEAD)
    _write(root / "pyproject.toml", '[project]\nname = "fx"\nversion = "2"\ndependencies = ["requests>=2.31"]\n')
    _write(root / "docs" / "design.md", "# Design\n\n`fetch` retries 3 times under a lock and returns None.\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "fetch: retry with a lock instead of failing")
    head = _git(root, "rev-parse", "HEAD")
    assert v1 != head
    return root, v1, head


@pytest.fixture
def analysed(tmp_path):
    """The project doing the research (its store holds research rows/evidence)."""
    root = tmp_path / "analysed"
    root.mkdir()
    return root, open_store(root)


def _keys(res, cat):
    return [f["key"] for f in res["mechanism"]["facts"][cat]]


# -- parse_reference -------------------------------------------------------------------

@pytest.mark.parametrize("ref_str, expect", [
    ("https://github.com/psf/requests", {"type": "git", "url": "https://github.com/psf/requests", "ref": None}),
    ("https://github.com/psf/requests.git", {"type": "git", "ref": None}),
    ("https://github.com/psf/requests/tree/v2.31.0", {"type": "git", "ref": "v2.31.0",
                                                     "url": "https://github.com/psf/requests"}),
    ("https://github.com/psf/requests/commit/147c8511ddbfa5e8f71bbf5c18ede0c4ceb3bba4",
     {"type": "git", "ref": "147c8511ddbfa5e8f71bbf5c18ede0c4ceb3bba4"}),
    ("https://github.com/psf/requests/blob/main/src/requests/adapters.py",
     {"type": "git", "ref": "main", "subpath": "src/requests/adapters.py"}),
    ("https://github.com/psf/requests@v2.31.0", {"type": "git", "ref": "v2.31.0",
                                                "url": "https://github.com/psf/requests"}),
    ("https://github.com/psf/requests/releases/tag/v2.31.0", {"type": "git", "ref": "v2.31.0"}),
    ("https://gitlab.com/group/sub/proj/-/tree/release/1.0/src", {"type": "git", "ref": "release",
                                                                   "url": "https://gitlab.com/group/sub/proj"}),
    ("git@github.com:psf/requests.git@v2.31.0", {"type": "git", "ref": "v2.31.0",
                                                 "url": "git@github.com:psf/requests.git"}),
    ("ssh://git@example.org/team/lib.git@v3", {"type": "git", "ref": "v3", "url": "ssh://git@example.org/team/lib.git"}),
    ("https://example.org/team/lib.git", {"type": "git", "ref": None}),
    ("https://docs.python.org/3/library/threading.html", {"type": "document", "source_type": "official_doc"}),
    ("https://requests.readthedocs.io/en/latest/user/advanced/", {"type": "document", "source_type": "official_doc"}),
    ("https://arxiv.org/abs/1706.03762", {"type": "document", "source_type": "paper"}),
    ("https://example.com/whitepaper.pdf", {"type": "document", "source_type": "paper"}),
    ("https://www.rfc-editor.org/rfc/rfc9110", {"type": "document", "source_type": "standard"}),
    ("https://someblog.example.com/posts/retries", {"type": "document", "source_type": "secondary"}),
    ("https://www.google.com/search?q=python+lock", {"type": "document", "source_type": "search_result"}),
    ("https://github.com/psf/requests/issues/1234", {"type": "document", "source_type": "secondary"}),
    ("https://medium.com/@someone/an-article", {"type": "document", "source_type": "secondary"}),
])
def test_parse_reference_variants(ref_str, expect):
    got = rs.parse_reference(ref_str)
    for k, v in expect.items():
        assert got[k] == v, (k, got)
    assert got["slug"] and "/" not in got["slug"] and "\\" not in got["slug"]


def test_parse_reference_tree_ref_with_slashes_lists_candidates():
    got = rs.parse_reference("https://github.com/o/r/tree/feature/x/src/a.py")
    assert got["ref_candidates"][:3] == [("feature", "x/src/a.py"), ("feature/x", "src/a.py"),
                                         ("feature/x/src", "a.py")]


def test_parse_reference_kind_override_and_local(fx, tmp_path):
    root, _, _ = fx
    assert rs.parse_reference("https://example.com/spec", "standard")["source_type"] == "standard"
    assert rs.parse_reference("https://example.com/code", "reference_repo")["type"] == "git"
    loc = rs.parse_reference(str(root))
    assert loc["type"] == "git" and Path(loc["path"]) == root.resolve()
    sub = rs.parse_reference(str(root / "fetcher"))
    assert sub["type"] == "git" and sub["subpath"] == "fetcher"
    at = rs.parse_reference(f"{root}@v1")
    assert at["type"] == "git" and at["ref"] == "v1"
    plain = tmp_path / "plain"
    plain.mkdir()
    assert rs.parse_reference(str(plain))["type"] == "local_dir"
    doc = tmp_path / "notes.md"
    doc.write_text("# notes\n", encoding="utf-8")
    assert rs.parse_reference(str(doc))["type"] == "document"
    with pytest.raises(ValueError):
        rs.parse_reference(str(tmp_path / "does-not-exist"))
    with pytest.raises(ValueError):
        rs.parse_reference("ftp://example.com/x")


# -- git research: version pinning ------------------------------------------------------------

def test_research_pins_requested_old_tag_not_head(fx, analysed):
    root, v1, head = fx
    repo, st = analysed
    res = rs.research(st, repo, str(root), ref="v1", topic="fetch retry")
    assert res["status"] == "ok", res
    assert res["resolved_commit"] == v1 and res["resolved_commit"] != head
    assert len(res["resolved_commit"]) == 40
    assert res["resolved_tag"] == "v1" and res["ref_kind"] == "tag"
    assert v1 in res["pin"] and "v1" in res["pin"]
    co = Path(res["checkout"])
    assert co.is_relative_to((repo / ".repoatlas" / "research").resolve())
    assert _git(co, "rev-parse", "HEAD") == v1
    assert subprocess.run(["git", "-C", str(co), "symbolic-ref", "-q", "HEAD"]).returncode != 0  # detached
    # v1-only facts are found; HEAD-only facts are not
    assert "env FETCH_TIMEOUT" in _keys(res, "environment")
    assert "env FETCH_TIMEOUT_S" not in _keys(res, "environment")
    assert "translates ConnectionError -> FetchError" in _keys(res, "error_handling")
    assert not any(k.startswith("retries on") for k in _keys(res, "error_handling"))
    assert res["mechanism"]["facts"]["concurrency"] == []
    assert "fetch()" in res["mechanism"]["seeds"]
    loc = next(f for f in res["mechanism"]["facts"]["environment"] if f["key"] == "env FETCH_TIMEOUT")
    assert loc["at"] == "fetcher/client.py:10"
    subjects = [c["subject"] for c in res["mechanism"]["why"]["commits"]]
    assert "fetch: fail fast with FetchError" in subjects
    assert "fetch: retry with a lock instead of failing" not in subjects
    assert any(d["at"].startswith("docs/design.md") for d in res["mechanism"]["why"]["docs"])
    assert any(t["at"].startswith("tests/test_client.py") for t in res["mechanism"]["why"]["tests"])
    # research row
    row = st.get("research", res["id"])
    assert row["status"] == "ok" and row["resolved_commit"] == v1 and row["resolved_tag"] == "v1"
    assert row["requested_ref"] == "v1" and row["kind"] == "git" and row["local_path"] == str(co)
    # evidence: pinned commit + checkout root, re-checkable
    evs = [st.evidence(e) for e in res["evidence_ids"]]
    assert evs and all(e["commit_sha"] == v1 for e in evs)
    types = {e["source_type"] for e in evs}
    assert {"reference_repo", "git_history", "design_doc"} <= types
    for e in evs:
        if e.get("path"):
            assert e["meta"]["root"] == str(co)
            assert evmod.check_source(repo, e).ok, e["locator"]
    gh = next(e for e in evs if e["source_type"] == "git_history")
    assert gh["meta"]["commit"] == v1 and gh["excerpt"] == "fetch: fail fast with FetchError"


def test_research_default_branch_head_is_reported_as_such(fx, analysed):
    root, v1, head = fx
    repo, st = analysed
    res = rs.research(st, repo, str(root), topic="fetch retry")
    assert res["status"] == "ok"
    assert res["resolved_commit"] == head
    assert res["pin"].startswith(f"pinned default-branch HEAD {head}")
    assert res["ref_kind"] == "default-branch HEAD"
    assert any("default branch" in w for w in res["warnings"])
    assert {"threading.Lock", "lock held (with)"} <= set(_keys(res, "concurrency"))
    assert "retry loop" in _keys(res, "error_handling")
    # the v1 checkout (other worktree) is untouched by researching HEAD afterwards
    res1 = rs.research(st, repo, str(root), ref="v1")
    assert res1["resolved_commit"] == v1
    assert Path(res1["checkout"]) != Path(res["checkout"])
    assert _git(Path(res["checkout"]), "rev-parse", "HEAD") == head
    assert res1["index"]["nodes"] > 0


def test_research_by_sha_and_path_at_ref(fx, analysed):
    root, v1, head = fx
    repo, st = analysed
    res = rs.research(st, repo, str(root), ref=v1[:10])
    assert res["status"] == "ok" and res["resolved_commit"] == v1 and res["ref_kind"] == "commit"
    assert res["resolved_tag"] == "v1"  # the tag that points at the commit
    res2 = rs.research(st, repo, f"{root}@v1")
    assert res2["status"] == "ok" and res2["resolved_commit"] == v1


def test_unresolvable_ref_is_an_error_never_default_branch(fx, analysed):
    root, v1, head = fx
    repo, st = analysed
    res = rs.research(st, repo, str(root), ref="v9.9.9", topic="fetch")
    assert res["status"] == "error"
    assert "could not be resolved" in res["error"] and "default branch" in res["error"]
    assert res["resolved_commit"] is None
    assert "mechanism" not in res
    row = st.get("research", res["id"])
    assert row["status"] == "error" and row["resolved_commit"] is None


def test_conflicting_refs_are_rejected(fx, analysed):
    root, _, _ = fx
    repo, st = analysed
    res = rs.research(st, repo, f"{root}@v1", ref="master")
    assert res["status"] == "error" and "conflicting refs" in res["error"]


def test_cached_clone_is_reused_and_sees_new_commits(fx, analysed):
    root, v1, head = fx
    repo, st = analysed
    first = rs.research(st, repo, str(root))
    _write(root / "NEWS.md", "news\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "news")
    new_head = _git(root, "rev-parse", "HEAD")
    again = rs.research(st, repo, str(root))
    assert again["clone"].startswith("reused")
    assert first["resolved_commit"] == head and again["resolved_commit"] == new_head


def test_offline_refresh_with_unknown_ref_is_unreachable_but_cached_tag_still_pins(fx, analysed, tmp_path):
    root, v1, head = fx
    repo, st = analysed
    moved = tmp_path / "remote_copy"
    shutil.copytree(root, moved)
    url = moved.as_uri()  # file:// URL: a remote as far as research is concerned
    first = rs.research(st, repo, url, ref="v1")
    assert first["status"] == "ok" and first["resolved_commit"] == v1
    moved.rename(tmp_path / "remote_gone")  # the "remote" is now unreachable (rename: git objects are read-only on Windows)
    cached = rs.research(st, repo, url, ref="v1")
    assert cached["status"] == "ok" and cached["resolved_commit"] == v1
    assert any("could not refresh" in w for w in cached["warnings"])
    missing = rs.research(st, repo, url, ref="v2")
    assert missing["status"] == "unreachable" and "may exist upstream" in missing["error"]
    assert missing["resolved_commit"] is None


def test_absolute_graphify_out_is_refused_for_research_checkouts(fx, analysed, monkeypatch, tmp_path):
    root, _, _ = fx
    repo, st = analysed
    monkeypatch.setenv("GRAPHIFY_OUT", str(tmp_path / "shared-index"))
    res = rs.research(st, repo, str(root), ref="v1")
    assert res["status"] == "error" and "GRAPHIFY_OUT" in res["error"]
    assert not (tmp_path / "shared-index").exists()


# -- compare ---------------------------------------------------------------------------------------

def test_compare_reports_assumption_differences_with_locators(fx):
    root, v1, head = fx
    st = open_store(root)
    res = rs.compare(st, root, str(root), ref="v1", topic="fetch retry")
    assert res["status"] == "ok", res.get("error")
    assert res["reference"]["commit"] == v1 and res["local"]["commit"] == head
    cats = res["categories"]
    conc = {x["key"]: x for x in cats["concurrency"]["only_local"]}
    assert "threading.Lock" in conc and conc["threading.Lock"]["at"] == ["fetcher/client.py:6"]
    assert conc["threading.Lock"]["evidence"]
    errs = {x["key"]: x for x in cats["error_handling"]["only_reference"]}
    assert errs["translates ConnectionError -> FetchError"]["at"] == ["fetcher/client.py:13"]
    assert "retries on ConnectionError" in {x["key"] for x in cats["error_handling"]["only_local"]}
    shared = {x["key"]: x for x in cats["error_handling"]["shared"]}
    assert "catches ConnectionError" in shared
    assert shared["catches ConnectionError"]["local_at"] == ["fetcher/client.py:20"]
    assert shared["catches ConnectionError"]["reference_at"] == ["fetcher/client.py:13"]
    env_l = {x["key"] for x in cats["environment"]["only_local"]}
    env_r = {x["key"] for x in cats["environment"]["only_reference"]}
    assert "env FETCH_TIMEOUT_S" in env_l and "env FETCH_TIMEOUT" in env_r
    dep = cats["dependencies"]["differing"][0]
    assert dep["key"] == "requests" and dep["local"] == ">=2.31" and dep["reference"] == ">=2.0"
    assert dep["local_at"] == ["pyproject.toml:4"] and dep["reference_at"] == ["pyproject.toml:4"]
    unk = [u for u in res["unknowns"] if u["category"] == "concurrency"]
    assert unk and unk[0]["missing_on"] == "reference" and "not evidence of absence" in unk[0]["why"]
    # comparison claims: inference with evidence from both sides, local files as subjects
    assert res["claims"]
    from repoatlas.claims import Claims

    cl = Claims(st, root)
    conc_claim = next(cl.show(c) for c in res["claims"] if "concurrency" in cl.get(c)["text"])
    assert conc_claim["status"] == "strong_inference" and conc_claim["kind"] == "comparison"
    assert conc_claim["uncertainties"] and "not proven absent" in conc_claim["uncertainties"][0]
    assert "fetcher/client.py" in conc_claim["subjects"]
    # the local index never includes the research checkouts
    from repoatlas import index

    g = index.load(root)
    assert not any(str(d.get("source_file") or "").startswith(".repoatlas") for _, d in g.G.nodes(data=True))
    # re-running does not duplicate claims or evidence rows
    n_claims = st.one("SELECT COUNT(*) AS n FROM claims")["n"]
    n_ev = st.one("SELECT COUNT(*) AS n FROM evidence")["n"]
    res2 = rs.compare(st, root, str(root), ref="v1", topic="fetch retry")
    assert sorted(res2["claims"]) == sorted(res["claims"])
    assert st.one("SELECT COUNT(*) AS n FROM claims")["n"] == n_claims
    assert st.one("SELECT COUNT(*) AS n FROM evidence")["n"] == n_ev


def test_compare_with_unreachable_reference_reports_unknowns(fx):
    root, _, _ = fx
    st = open_store(root)
    res = rs.compare(st, root, "https://127.0.0.1:9/acme/fetchlib.git", topic="fetch retry")
    assert res["status"] == "unreachable"
    assert {u["category"] for u in res["unknowns"]} == set(rs.COMPARE_CATEGORIES)


# -- unreachable / documents -------------------------------------------------------------------------

def test_unreachable_git_url_does_not_raise(analysed):
    repo, st = analysed
    res = rs.research(st, repo, "https://127.0.0.1:9/acme/lib.git", ref="v1", topic="x")
    assert res["status"] == "unreachable"
    assert "clone" in res["error"] and res["next_steps"]
    assert st.get("research", res["id"])["status"] == "unreachable"


def test_unreachable_document_url_does_not_raise(analysed, monkeypatch):
    import socket

    repo, st = analysed

    def no_dns(*a, **k):  # offline: the vendored validate_url sees a real resolver failure
        raise socket.gaierror(11001, "getaddrinfo failed")

    monkeypatch.setattr(socket, "getaddrinfo", no_dns)
    res = rs.research(st, repo, "https://nonexistent.invalid/docs/page.html", topic="x")
    assert "DNS resolution failed" in res["error"]
    assert res["status"] == "unreachable" and res["kind"] == "document"
    assert "evidence_ids" not in res or not res["evidence_ids"]


@pytest.mark.parametrize("exc, needle", [
    (urllib.error.HTTPError("https://docs.example.org/x", 403, "Forbidden", {}, None), "403"),
    (urllib.error.HTTPError("https://docs.example.org/x", 401, "Unauthorized", {}, None), "authentication"),
    (urllib.error.URLError("network is unreachable"), "unreachable"),
    (TimeoutError("timed out"), "timed out"),
])
def test_document_network_and_auth_failures_are_unreachable(analysed, monkeypatch, exc, needle):
    repo, st = analysed

    def boom(url, max_bytes, timeout=20):
        raise exc

    monkeypatch.setattr(rs, "_fetch", boom)
    res = rs.research(st, repo, "https://docs.example.org/x")
    assert res["status"] == "unreachable" and needle in res["error"]


HTML = b"""<html><head><title>threading - Thread-based parallelism</title>
<style>body { color: red }</style><script>var lockSecret = 1;</script></head>
<body><nav>Home</nav><main><h1>Lock Objects</h1>
<p>A primitive lock is a synchronization primitive that is not owned by a particular thread when locked.</p>
<p>acquire(blocking=True, timeout=-1): Acquire a lock, blocking or non-blocking.</p>
<p>Unrelated paragraph about queues.</p>
</main></body></html>"""


def test_document_research_records_versioned_evidence(analysed, monkeypatch):
    repo, st = analysed
    calls = []

    def fake(url, max_bytes, timeout=20):
        calls.append(url)
        return {"data": HTML, "content_type": "text/html; charset=utf-8", "etag": '"abc123"',
                "last_modified": "Mon, 01 Jan 2024 00:00:00 GMT", "final_url": url, "http_status": 200}

    monkeypatch.setattr(rs, "_fetch", fake)
    url = "https://docs.python.org/3/library/threading.html"
    res = rs.research(st, repo, url, topic="lock acquire")
    assert res["status"] == "ok" and calls == [url]
    assert res["kind"] == "document" and res["source_type"] == "official_doc"
    assert res["version"] == '"abc123"' and res["content_hash"].startswith("sha256:")
    assert res["title"].startswith("threading")
    text = Path(res["text_path"]).read_text(encoding="utf-8")
    assert "lockSecret" not in text and "color: red" not in text and "Acquire a lock" in text
    assert res["passages"] and "acquire" in res["passages"][0]["terms"]
    main = st.evidence(res["evidence_ids"][0])
    assert main["source_type"] == "official_doc" and main["version"] == '"abc123"'
    assert main["url"] == url and main["content_hash"] == res["content_hash"]
    passage = st.evidence(res["passages"][0]["evidence_id"])
    assert passage["locator"].startswith(url + "#text-L")
    row = st.get("research", res["id"])
    assert row["kind"] == "document" and row["content_hash"] == res["content_hash"]
    # same content again -> same evidence rows (no duplicates)
    res2 = rs.research(st, repo, url, topic="lock acquire")
    assert res2["evidence_ids"] == res["evidence_ids"]


def test_search_result_page_is_refused_as_evidence(analysed, monkeypatch):
    repo, st = analysed
    monkeypatch.setattr(rs, "_fetch", lambda *a, **k: pytest.fail("search pages must not be fetched"))
    res = rs.research(st, repo, "https://www.google.com/search?q=python+lock")
    assert res["status"] == "error" and res.get("refused") is True
    assert "not evidence" in res["error"]
    assert st.one("SELECT COUNT(*) AS n FROM evidence")["n"] == 0


def test_local_document_file(analysed, tmp_path):
    repo, st = analysed
    doc = tmp_path / "retry-notes.md"
    doc.write_text("# Retry notes\n\nRetry with exponential backoff.\nOther text.\n", encoding="utf-8")
    res = rs.research(st, repo, str(doc), topic="retry backoff")
    assert res["status"] == "ok" and res["source_type"] == "secondary"
    assert res["passages"][0]["at"] == "text L2-4"


# -- plain directories -------------------------------------------------------------------------------

def test_plain_directory_is_pinned_by_content_hash(fx, analysed, tmp_path):
    root, _, _ = fx
    repo, st = analysed
    plain = tmp_path / "plain_copy"
    shutil.copytree(root, plain, ignore=shutil.ignore_patterns(".git", ".repoatlas"))
    res = rs.research(st, repo, str(plain), topic="fetch retry")
    assert res["status"] == "ok" and res["kind"] == "local_dir"
    assert res["resolved_commit"] is None and res["content_hash"].startswith("sha256:")
    assert "content hash" in res["pin"]
    assert "threading.Lock" in _keys(res, "concurrency")
    evs = [st.evidence(e) for e in res["evidence_ids"]]
    assert all(e["meta"]["root"] == res["checkout"] for e in evs if e.get("path"))
    bad = rs.research(st, repo, str(plain), ref="v1")
    assert bad["status"] == "error" and "not a git repository" in bad["error"]


# -- dependencies ---------------------------------------------------------------------------------------

def test_dependencies_from_manifests(tmp_path):
    _write(tmp_path / "pyproject.toml", '[project]\nname="x"\ndependencies = [\n  "Requests[socks]>=2.31 ; python_version>\'3\'",\n'
                                        '  "urllib3<3",\n]\n[project.optional-dependencies]\ndev = ["pytest>=8"]\n')
    _write(tmp_path / "requirements-dev.txt", "# dev\nruff==0.6.0\n-e .\n")
    _write(tmp_path / "package.json", json.dumps({"dependencies": {"axios": "^1.6.0"},
                                                  "devDependencies": {"vitest": "^1.0.0"}}, indent=2))
    _write(tmp_path / "go.mod", "module x\n\ngo 1.22\n\nrequire (\n\tgithub.com/pkg/errors v0.9.1\n"
                                "\tgolang.org/x/sync v0.7.0 // indirect\n)\n")
    deps = rs.dependencies(tmp_path)
    by = {(d["name"], d["ecosystem"]): d for d in deps["items"]}
    assert by[("requests", "python")]["spec"] == ">=2.31" and by[("requests", "python")]["at"] == "pyproject.toml:4"
    assert by[("urllib3", "python")]["spec"] == "<3"
    assert by[("pytest", "python")]["scope"] == "optional:dev"
    assert by[("ruff", "python")]["scope"] == "dev" and by[("ruff", "python")]["at"] == "requirements-dev.txt:2"
    assert by[("axios", "npm")]["spec"] == "^1.6.0" and by[("vitest", "npm")]["scope"] == "dev"
    assert by[("github.com/pkg/errors", "go")]["spec"] == "v0.9.1"
    assert by[("golang.org/x/sync", "go")]["scope"] == "indirect"
    assert set(deps["manifests"]) == {"pyproject.toml", "requirements-dev.txt", "package.json", "go.mod"}


# -- mechanism extraction details ------------------------------------------------------------------------

def test_mechanism_extracts_categories_from_python(tmp_path):
    from repoatlas import index

    root = tmp_path / "svc"
    _write(root / "svc" / "__init__.py", "")
    _write(root / "svc" / "worker.py", '''"""Worker."""
import asyncio
import os
import sys
from collections import deque
from dataclasses import dataclass

QUEUE = deque()


@dataclass
class Job:
    name: str
    tries: int = 0


async def run_job(job: Job) -> dict:
    if sys.platform == "win32":
        base = "C:\\\\jobs"
    else:
        base = "/var/jobs"
    token = os.environ["JOB_TOKEN"]
    try:
        await asyncio.sleep(0)
        with open(os.path.join(base, job.name)) as fh:
            data = fh.read()
    except FileNotFoundError:
        return {}
    except (ValueError, KeyError):
        pass
    finally:
        QUEUE.append(job)
    return {"job": job.name, "data": data, "token": token}
''')
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "worker")
    index.build(root)
    g = index.load(root)
    m = rs.mechanism(g, "run job worker", root, None)
    keys = {c: {f["key"] for f in m["facts"][c]} for c in rs.CATEGORIES}
    assert {"dataclass Job", "container deque", "container dict literal", "param type Job"} <= keys["data_structures"]
    assert {"catches FileNotFoundError", "error-return on FileNotFoundError", "catches ValueError",
            "swallows KeyError", "finally cleanup"} <= keys["error_handling"]
    assert {"async def", "await", "asyncio.sleep"} <= keys["concurrency"]
    assert {"env JOB_TOKEN", "filesystem open/os file ops", "filesystem os.path"} <= keys["environment"]
    assert any(k.startswith("platform check sys.platform") for k in keys["environment"])
    assert any(k.startswith("filesystem literal /var/jobs") for k in keys["environment"])
    deque_fact = next(f for f in m["facts"]["data_structures"] if f["key"] == "container deque")
    assert deque_fact["at"] == "svc/worker.py:8" and "module-level `QUEUE`" in deque_fact["detail"]
    assert m["coverage"]["limits"] and "heuristic" in m["coverage"]["limits"][0]


def test_mechanism_retry_policy_and_module_nodes(tmp_path):
    from repoatlas import index

    root = tmp_path / "httpish"
    _write(root / "client" / "__init__.py", "")
    _write(root / "client" / "util.py", "def retry_helper(n: int) -> int:\n    return n\n")
    _write(root / "server" / "util.py", "def retry_other(n: int) -> int:\n    return n + 1\n")
    _write(root / "client" / "adapter.py", '''from urllib3.util.retry import Retry


class RetryError(Exception):
    pass


class RetryAdapter:
    def __init__(self, max_retries: int = 0):
        self.max_retries = Retry.from_int(max_retries)

    def send(self, pool, url: str):
        try:
            return pool.urlopen("GET", url, retries=self.max_retries)
        except OSError as e:
            raise RetryError(e)
''')
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "adapter")
    index.build(root)
    g = index.load(root)
    m = rs.mechanism(g, "retry adapter util", root, None)
    errs = {f["key"]: f for f in m["facts"]["error_handling"]}
    assert errs["retry policy Retry"]["at"] == "client/adapter.py:10"
    assert errs["retry policy (argument)"]["at"] == "client/adapter.py:14"
    assert "translates OSError -> RetryError" in errs
    assert "retry policy RetryError" not in errs  # raising an exception is not a retry policy
    labels = [s["symbol"] for s in m["symbols"]]
    assert not any(lbl.endswith("util.py") for lbl in labels), labels  # module nodes are not symbols


def test_mechanism_regex_fallback_for_go(tmp_path):
    from repoatlas import index

    root = tmp_path / "gosvc"
    _write(root / "go.mod", "module example.com/gosvc\n\ngo 1.22\n")
    _write(root / "pool.go", '''package gosvc

import (
\t"os"
\t"sync"
)

type Pool struct {
\tmu sync.Mutex
\tjobs chan string
}

func (p *Pool) Dispatch(job string) error {
\tif os.Getenv("POOL_DISABLED") != "" {
\t\treturn nil
\t}
\tp.mu.Lock()
\tdefer p.mu.Unlock()
\tgo func() { p.jobs <- job }()
\treturn nil
}
''')
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "pool")
    index.build(root)
    g = index.load(root)
    m = rs.mechanism(g, "pool dispatch", root, None)
    keys = {c: {f["key"] for f in m["facts"][c]} for c in rs.CATEGORIES}
    assert "goroutine" in keys["concurrency"] and "lock acquire/release" in keys["concurrency"]
    assert "env POOL_DISABLED" in keys["environment"]
    assert all(f["method"] == "regex-heuristic" for c in rs.CATEGORIES for f in m["facts"][c])


# -- CLI wiring + rendering --------------------------------------------------------------------------------

def test_cli_research_and_compare_json(fx, capsys):
    from repoatlas import cli

    root, v1, _ = fx
    rc = cli.main(["research", str(root), "--ref", "v1", "--topic", "fetch retry", "--repo", str(root), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["resolved_commit"] == v1 and out["resolved_tag"] == "v1"
    rc = cli.main(["compare", str(root), str(root), "--ref", "v1", "--topic", "fetch retry", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["status"] == "ok" and "concurrency" in out["categories"]
    rc = cli.main(["research", "https://127.0.0.1:9/a/b.git", "--repo", str(root)])
    text = capsys.readouterr().out
    assert rc == 1 and "unreachable" in text


def test_render_outputs_are_compact(fx, capsys):
    root, _, _ = fx
    st = open_store(root)
    res = rs.research(st, root, str(root), ref="v1", topic="fetch retry")
    rs.render(res)
    cmp = rs.compare(st, root, str(root), ref="v1", topic="fetch retry")
    rs.render_compare(cmp)
    rs.render(rs.research(st, root, "https://127.0.0.1:9/x/y.git"))
    out = capsys.readouterr().out
    assert "pinned tag v1" in out and "local only: threading.Lock" in out and "unreachable" in out
    assert len(out.splitlines()) < 120
