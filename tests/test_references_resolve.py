"""references.resolve end to end + research integration (git fixtures, cassettes; no network)."""

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import importlib.util  # noqa: E402
import socket  # noqa: E402
import sqlite3  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import research as rs  # noqa: E402
from verinoda.references import accounted, compact, render_text, resolve  # noqa: E402
from verinoda.references.gitref import GitRunner  # noqa: E402
from verinoda.references.transport import (  # noqa: E402
    CacheTransport,
    CassetteTransport,
)
from verinoda.store import open_store  # noqa: E402

FX = Path(__file__).parent / "fixtures" / "references"
_spec = importlib.util.spec_from_file_location("references_helpers", FX / "helpers.py")
helpers = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(helpers)
NOW = "2026-09-23T10:00:00Z"
POC = ("requests'in 2.31 sürümünde https://github.com/psf/requests/blob/main/src/requests/sessions.py#L100-L120 "
       "satırlarındaki yönlendirme mantığı bizimkinden farklı mı?")


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("resolve")
    shas = helpers.requests_repo(tmp / "requests")
    project = helpers.analysed_project(tmp / "app")
    return {"shas": shas, "project": project, "requests": tmp / "requests", "tmp": tmp}


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("network access in an offline test")

    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")


def gitrunner(world, network="cache", project=None):
    return GitRunner(network=network, cache_root=(project or world["project"]) / ".verinoda" / "research",
                     remote_map={"https://github.com/psf/requests": world["requests"]})


def run(world, text, *, project=None, network="cache", **kw):
    project = project or world["project"]
    return resolve(open_store(project), project, text, network=network, now=NOW,
                   git=kw.pop("git", None) or gitrunner(world, network, project),
                   transport=kw.pop("transport", None) or CassetteTransport(FX / "cassettes"), **kw)


def test_poc_output_schema_and_append_only_storage(world):
    res = run(world, POC)
    assert res["schema"] == "verinoda.reference_resolution/1" and res["id"].startswith("rrs_")
    assert set(res) >= {"input", "context", "mentions", "references", "unbound_mentions", "questions_for_user",
                        "summary", "mismatches", "unresolved", "status"}
    r = res["references"][0]
    assert r["pin"]["value"] == world["shas"]["v2.31.0"] and r["pin"]["name"] == "v2.31.0"
    assert r["pin"]["immutable"] and r["pin"]["precedence_rank"] == 2
    assert r["coreference"]["via"] == ["name_matches_repo"] and r["requested"]["lines"] == [100, 120]
    alt = next(a for a in r["alternates"] if a["pin"]["basis"] == "url_branch")
    assert alt["pin"]["value"] == world["shas"]["main"] and alt["path_relation"] == "missing_at_primary"
    m1b = next(m for m in r["mismatches"] if m["code"] == "M1b")
    assert m1b["severity"] == "error" and m1b["same_name"] == ["requests/sessions.py"]
    assert res["status"] == "partial" and res["summary"]["errors"] == 1
    assert accounted(res) == (True, [])
    assert res["context"]["local_commit"] and res["input"]["lang"] == "tr"
    st = open_store(world["project"])
    row = st.get("reference_resolutions", res["id"])
    assert row["text"] == POC and row["result"]["references"][0]["pin"]["name"] == "v2.31.0"
    with pytest.raises(sqlite3.DatabaseError):
        st.conn.execute("DELETE FROM reference_resolutions WHERE id = ?", (res["id"],))
    with pytest.raises(sqlite3.DatabaseError):
        st.conn.execute("UPDATE reference_resolutions SET text = 'x' WHERE id = ?", (res["id"],))
    st.conn.rollback()  # the aborted statements leave the implicit transaction (and its write lock) open
    st.close()


def test_compact_and_text_are_small(world):
    res = run(world, POC)
    c = compact(res)
    assert c["references"][0]["pin"].startswith("v2.31.0") and any("M1b" in m for m in c["references"][0]["mismatches"])
    text = render_text(res)
    assert "basis: explicit_text_version" in text and "M1b error" in text and len(text.splitlines()) < 15


def test_a_named_tag_resolves_by_qualified_ref_and_collisions_are_reported(world):
    tree = run(world, "https://github.com/psf/requests/tree/v1")["references"][0]
    tag = run(world, "https://github.com/psf/requests/releases/tag/v1")["references"][0]
    assert tree["pin"]["value"] == world["shas"]["branch_v1"] and tree["pin"]["ref"] == "refs/heads/v1"
    assert tag["pin"]["value"] == world["shas"]["tag_v1"] and tag["pin"]["ref"] == "refs/tags/v1"
    for r in (tree, tag):
        assert any(m["code"] == "M5" for m in r["mismatches"])
        assert any(a["why"].endswith("(M5)") for a in r["alternates"])


def test_refs_required_classes_never_pin_the_default_branch(world):
    for url in ("https://github.com/psf/requests/compare", "https://github.com/psf/requests/pull/99"):
        r = run(world, url)["references"][0]
        assert r["status"] in ("refused", "unresolved") and r["pin"] is None
        assert all(a["pin"]["kind"] != "default_head" for a in r["alternates"]) or r["status"] == "unresolved"


def test_pr_head_merge_request_and_force_push(world):
    pr = run(world, "https://github.com/psf/requests/pull/6963")["references"][0]
    assert pr["pin"]["kind"] == "pr_head" and pr["pin"]["value"] == world["shas"]["pr6963"]
    old = world["shas"]["pr7_old"]
    forced = run(world, f"https://github.com/psf/requests/pull/7/commits/{old}")["references"][0]
    assert forced["pin"]["value"] == old and any(m["code"] == "M6" for m in forced["mismatches"])
    mr = resolve(open_store(world["project"]), world["project"], "https://gitlab.com/psf/requests/-/merge_requests/3",
                 now=NOW, transport=CassetteTransport(FX / "cassettes"),
                 git=GitRunner(network="cache", remote_map={"https://gitlab.com/psf/requests": world["requests"]}))
    assert mr["references"][0]["pin"]["value"] == world["shas"]["pr6963"]


def test_compare_pins_both_ends(world):
    r = run(world, "https://github.com/psf/requests/compare/v2.30.0...v2.31.0")["references"][0]
    c = r["pin"]["compare"]
    assert c["base"]["value"] == world["shas"]["v2.30.0"] and c["head"]["value"] == world["shas"]["v2.31.0"]
    assert c["merge_base"] == world["shas"]["v2.30.0"] and r["pin"]["immutable"]


def test_local_version_ladder_and_demotion(world):
    plain = run(world, "https://github.com/psf/requests/blob/main/requests/api.py")["references"][0]
    assert plain["pin"]["basis"] == "url_branch"  # no local intent: the linked branch wins over the lock
    local = run(world, "https://github.com/psf/requests/blob/main/requests/api.py in the version we use")
    r = local["references"][0]
    assert local["context"]["local_intent"] and r["pin"]["basis"] == "local_project_version"
    assert r["requested"]["local_version"]["source_file"] == "uv.lock"
    assert any(m["code"] == "M2" for m in r["mismatches"])
    caller = run(world, "https://github.com/psf/requests/blob/main/requests/api.py", local_intent=True)
    assert caller["references"][0]["pin"]["basis"] == "local_project_version"


def test_text_date_pins_the_commit_before_it(world, tmp_path):
    bare = tmp_path / "bare"
    bare.mkdir()
    helpers.git(bare, "init", "-q")
    r = run(world, "https://github.com/psf/requests as of 2023-05-10", project=bare)["references"][0]
    assert r["pin"]["basis"] == "text_date" and r["pin"]["value"] == world["shas"]["v2.30.0"]


def test_floating_default_is_always_disclosed(world, tmp_path):
    bare = tmp_path / "bare2"
    bare.mkdir()
    helpers.git(bare, "init", "-q")
    res = run(world, "Look at https://github.com/psf/requests please", project=bare)
    r = res["references"][0]
    assert r["status"] == "pinned_floating" and r["pin"]["kind"] == "default_head"
    assert r["pin"]["value"] == world["shas"]["main"]
    assert any(w.startswith("W_floating") for w in r["warnings"]) and res["summary"]["silent_floating_pins"] == 0


def test_version_binds_to_the_reference_where_it_exists(world):
    # toml is nearer to "2.30" but only requests has that version (toml is unreachable here: no tags at all)
    res = run(world, "https://github.com/psf/requests and https://github.com/uiri/toml, version 2.30 please")
    q = next(m for m in res["mentions"] if m["text"] == "2.30")
    requests_ref = next(r for r in res["references"] if "psf/requests" in (r["identity"]["canonical_url"] or ""))
    assert q["binding"] == "validated_by_existence" and q["id"] in requests_ref["mentions"]
    assert requests_ref["pin"]["name"] == "v2.30.0" and accounted(res)[0]
    # two references where the version exists in neither: nearest, flagged, and a question for the user
    res2 = run(world, "https://github.com/psf/requests and https://github.com/uiri/toml, version 9.9 please")
    q2 = next(m for m in res2["mentions"] if m["text"] == "9.9")
    assert q2["binding"] == "nearest_ambiguous" and any(x["kind"] == "binding" for x in res2["questions_for_user"])


def test_offline_semantics(world, tmp_path):
    project = helpers.analysed_project(tmp_path / "off")
    off_git = GitRunner(network="off", cache_root=project / ".verinoda" / "research")
    res = resolve(open_store(project), project, "https://github.com/psf/requests/tree/v2.31.0. Also numpy==1.26.0.",
                  network="off", now=NOW, git=off_git, transport=CacheTransport(tmp_path / "cache", mode="off"))
    repo_ref = next(r for r in res["references"] if r["class"] == "git_repo")
    assert repo_ref["status"] == "unresolved" and repo_ref["unresolved"][0]["reason"] == "offline"
    assert "--network" in repo_ref["unresolved"][0]["next_step"]
    pkg = next(r for r in res["references"] if r["class"] == "package")
    assert pkg["status"] == "pinned" and "not verified" in pkg["pin"]["display"]  # an exact version needs no lookup
    assert res["summary"]["network_calls"] == 0
    # a research mirror cached earlier answers offline, "as last fetched"
    mirror = project / ".verinoda" / "research" / rs.parse_reference("https://github.com/psf/requests")["slug"]
    helpers.git(world["tmp"], "clone", "--bare", "-q", str(world["requests"]), str(mirror / "mirror.git"))
    res2 = resolve(open_store(project), project, "https://github.com/psf/requests/tree/v2.31.0", network="off",
                   now=NOW, git=GitRunner(network="off", cache_root=project / ".verinoda" / "research"),
                   transport=CacheTransport(tmp_path / "cache", mode="off"))
    assert res2["references"][0]["pin"]["value"] == world["shas"]["v2.31.0"]


def test_local_files_and_commits_of_the_project(world):
    proj = world["project"]
    sha = helpers.git(proj, "rev-parse", "HEAD")
    res = run(world, f"app.py changed in {sha[:10]} and `pyproject.toml` too; missing/nowhere.py is gone")
    by = {r["requested"].get("path") or r["class"]: r for r in res["references"]}
    assert by["app.py"]["status"] == "pinned" and by["app.py"]["pin"]["basis"] == "local_snapshot"
    commit = next(r for r in res["references"] if r["class"] == "git_commit")
    assert commit["pin"]["value"] == sha and commit["coreference"]["via"] == ["local_project_commit"]
    assert by["missing/nowhere.py"]["status"] == "unresolved"
    assert accounted(res)[0]


def test_packages_runtimes_apps_and_unbound(world):
    res = run(world, "Python 3.11 ile requests==2.32.0 ve VS Code'un yaptığı gibi; a bare 2.5 here.")
    by = {r["class"]: r for r in res["references"]}
    assert by["runtime"]["pin"]["value"] == "3.11" and any(m["code"] == "M2" for m in by["runtime"]["mismatches"])
    assert {m["code"] for m in by["package"]["mismatches"]} == {"M9", "M2"}
    assert by["application"]["status"] == "unresolved"
    assert by["application"]["identity"]["source_availability"] == "unknown"
    assert {u["text"] for u in res["unbound_mentions"]} >= {"2.5"}
    assert res["questions_for_user"][0]["question"].startswith("VS Code")  # Turkish template for a TR message


def test_research_uses_the_resolved_pin_without_re_resolving(world, tmp_path, monkeypatch):
    project = tmp_path / "proj"
    project.mkdir()
    st = open_store(project)
    res = run(world, "requests 2.31 https://github.com/psf/requests/tree/main", project=project)
    r = res["references"][0]
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{world['requests'].as_uri()}.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "https://github.com/psf/requests")
    pin = {**r["pin"], "resolution_id": res["id"], "reference_id": r["id"]}
    full = rs.research_full(st, project, "https://github.com/psf/requests/tree/main", pin=pin)
    assert full["status"] == "ok", full.get("error")
    assert full["resolved_commit"] == world["shas"]["v2.31.0"] and full["resolved_tag"] == "v2.31.0"
    assert full["pin_basis"] == "explicit_text_version"
    row = st.get("research", full["id"])
    assert row["notes"]["resolution_id"] == res["id"] and row["notes"]["pin_basis"] == "explicit_text_version"


def test_research_traces_both_ends_of_a_compare_and_refuses_missing_refs(world, tmp_path, monkeypatch):
    project = tmp_path / "proj2"
    project.mkdir()
    st = open_store(project)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{world['requests'].as_uri()}.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "https://github.com/psf/requests")
    full = rs.research_full(st, project, "https://github.com/psf/requests/compare/v2.30.0...v2.31.0")
    assert full["status"] == "ok", full.get("error")
    assert full["resolved_commit"] == world["shas"]["v2.31.0"]
    assert full["compare_pins"]["base"] == world["shas"]["v2.30.0"]
    assert "requests/sessions.py" in full["changed_files"] and full["topic"]  # topic seeded from the change
    assert full.get("_base_mechanism") is not None and Path(full["base_checkout"]).is_dir()
    refused = rs.research(st, project, "https://github.com/psf/requests/compare")
    assert refused["status"] == "error" and refused["ref_status"] == "ref_required"
    assert "default branch" in refused["error"] and refused["resolved_commit"] is None
    pr = rs.research(st, project, "https://github.com/psf/requests/pull/6963")
    assert pr["status"] == "ok" and pr["resolved_commit"] == world["shas"]["pr6963"] and pr["ref_kind"] == "pr_head"
    latest = rs.research(st, project, "https://github.com/psf/requests/releases/latest")
    assert latest["status"] == "ok" and latest["resolved_tag"] == "v2.32.0"
    tree = rs.research(st, project, "https://github.com/psf/requests/tree/v1")
    assert tree["resolved_commit"] == world["shas"]["branch_v1"]
    assert tree.get("mismatches") and tree["mismatches"][0]["code"] == "M5"
    release = rs.research(st, project, "https://github.com/psf/requests/releases/tag/v1")
    assert release["resolved_commit"] == world["shas"]["tag_v1"]


def test_issue_research_uses_the_api_not_scraped_html(world, tmp_path, monkeypatch):
    project = tmp_path / "proj3"
    project.mkdir()
    st = open_store(project)
    monkeypatch.setattr(rs, "_fetch", lambda *a, **k: pytest.fail("an issue must not be scraped as HTML"))
    full = rs.research_full(st, project, "https://github.com/psf/requests/issues/6000",
                            transport=CassetteTransport(FX / "cassettes"))
    assert full["status"] == "ok" and full["source_type"] == "secondary"
    assert "issue #6000" in full["pin"] and full["version"]
    ev = st.evidence(full["evidence_ids"][0])
    assert ev["source_type"] == "secondary" and ev["meta"]["issue"] == 6000


def _serve(d: Path, url: str, body: bytes, ctype: str) -> None:
    """Write one cassette entry (a recorded response) for ``url``."""
    import json as _json

    from verinoda.references.transport import Response, to_entry

    d.mkdir(parents=True, exist_ok=True)
    e = to_entry(Response(200, {"content-type": ctype}, body, url, url, retrieved_at=NOW))
    (d / f"{e['key'][:12]}.json").write_bytes(_json.dumps(e).encode("utf-8"))


def _sdist_bytes(repo: Path, commit: str, extra: dict | None = None) -> bytes:
    import io
    import subprocess
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        files = {n: subprocess.run(["git", "-C", str(repo), "show", f"{commit}:{n}"], capture_output=True,
                                   check=True).stdout
                 for n in helpers.git(repo, "ls-tree", "-r", "--name-only", commit).splitlines()}
        files.update({"PKG-INFO": b"Metadata-Version: 2.1\nName: requests\n", **(extra or {})})
        for n, data in files.items():
            info = tarfile.TarInfo(f"requests-2.31.0/{n}")
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.mark.parametrize("patched", [False, True])
def test_package_research_maps_the_version_to_its_tag_and_compares_the_sdist(world, tmp_path, monkeypatch, patched):
    import hashlib
    import json as _json

    project = helpers.analysed_project(tmp_path / "proj4")
    st = open_store(project)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{world['requests'].as_uri()}.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "https://github.com/psf/requests")
    sdist = _sdist_bytes(world["requests"], world["shas"]["v2.31.0"],
                         {"requests/_patched.py": b"X = 1\n"} if patched else None)
    url = "https://files.pythonhosted.org/packages/xx/requests-2.31.0.tar.gz"
    d = tmp_path / "cassettes"
    _serve(d, "https://pypi.org/pypi/requests/json", _json.dumps({
        "info": {"name": "requests", "version": "2.31.0", "project_urls": {"Source": "https://github.com/psf/requests"}},
        "releases": {"2.31.0": [{"filename": "requests-2.31.0.tar.gz", "packagetype": "sdist", "url": url,
                                 "digests": {"sha256": hashlib.sha256(sdist).hexdigest()}, "yanked": False}]}}
    ).encode("utf-8"), "application/json")
    _serve(d, url, sdist, "application/gzip")
    full = rs.research_full(st, project, "requests==2.31.0", transport=CassetteTransport(d))
    assert full["status"] == "ok", full.get("error")
    assert full["resolved_commit"] == world["shas"]["v2.31.0"] and full["source_type"] == "dependency_source"
    content = full["mapping"]["content"]
    if patched:
        assert full["mapping"]["method"] == "tag_name" and content["misses"] == ["requests/_patched.py"]
        assert full["mismatches"][0]["code"] == "M8"
    else:
        assert full["mapping"]["method"] == "content_match" and full["mapping"]["strength"] == "verified"
        assert content["matched"] == content["total"] > 0
    bad = rs.research_full(st, project, "requests>=2", transport=CassetteTransport(d))
    assert bad["status"] == "error" and "no exact version" in bad["error"]


def test_a_bare_pr_or_issue_number_belongs_to_the_repository_its_sentence_names(world):
    res = run(world, "see PR #123 and issue #456 in psf/requests", network="off")
    xrefs = [r for r in res["references"] if r["class"] in ("issue", "pull_request")]
    assert len(xrefs) == 2
    for r in xrefs:
        assert r["identity"]["canonical_url"] == "https://github.com/psf/requests"
        assert r["coreference"]["via"] == ["repository_in_sentence"]
    tr = run(world, "psf/requests reposundaki #12 sorunu", network="off")
    assert any(r["class"] == "issue" and r["identity"].get("owner") == "psf" for r in tr["references"])


def _registry(d: Path, name: str, pypi: list[str] | None, npm: list[str] | None) -> CassetteTransport:
    """Cassettes for PyPI / npm / crates.io answering ``name`` with these versions (None: 404)."""
    import json as _json

    from verinoda.references.transport import Response, to_entry

    d.mkdir(parents=True, exist_ok=True)

    def entry(url: str, status: int, body: bytes, accept: str | None = None) -> None:
        e = to_entry(Response(status, {"content-type": "application/json"}, body, url, url, retrieved_at=NOW),
                     accept=accept)
        (d / f"{e['key'][:12]}.json").write_bytes(_json.dumps(e).encode("utf-8"))

    pypi_url, npm_url = f"https://pypi.org/pypi/{name}/json", f"https://registry.npmjs.org/{name}"
    if pypi is None:
        entry(pypi_url, 404, b"{}")
    else:
        entry(pypi_url, 200, _json.dumps({"info": {"name": name, "version": pypi[-1]},
                                          "releases": {v: [] for v in pypi}}).encode("utf-8"))
    npm_accept = "application/vnd.npm.install-v1+json"
    if npm is None:
        entry(npm_url, 404, b"{}", npm_accept)
    else:
        entry(npm_url, 200, _json.dumps({"name": name, "versions": {v: {} for v in npm}}).encode("utf-8"), npm_accept)
    entry(f"https://index.crates.io/{name[:2]}/{name[2:4]}/{name}", 404, b"")
    return CassetteTransport(d)


def test_prose_before_a_number_is_not_a_package_and_blocks_nothing(world):
    for text in ("the build took 2.5 seconds", "Why does startup take 1.5 seconds?", "It fails on macOS 14.2",
                 "Tested on Chrome 120.0 and Firefox 121.0", "the ratio drops below 0.75 after warmup",
                 "a bare 2.5 here", "compare fancylib 2.31 with the old one",
                 "Bu fonksiyon log'a ne yazıyor?", "redis'e bağlantı nerede açılıyor?"):
        res = run(world, text, network="off")
        assert not [r for r in res["references"] if r["class"] == "package"], text
        assert res["status"] == "complete" and not res["questions_for_user"], (text, res["status"])


def test_a_name_before_a_version_is_looked_up_only_with_network_on(world, tmp_path):
    # the default mode sends no name to a public registry: the name stays unbound
    res = run(world, "compare fancylib 2.31 with the old one", network="cache",
              transport=_registry(tmp_path / "c0", "fancylib", ["2.31.0"], None))
    assert not [r for r in res["references"] if r["class"] == "package"]
    assert any("network on" in u["why"] for u in res["unbound_mentions"] if u.get("text") == "fancylib")
    # network on: the project's own registry (PyPI here) has the version
    res = run(world, "compare fancylib 2.31 with the old one", network="on",
              transport=_registry(tmp_path / "c1", "fancylib", ["2.30.0", "2.31.0"], None))
    pkg, = [r for r in res["references"] if r["class"] == "package"]
    assert pkg["identity"]["package"]["purl"] == "pkg:pypi/fancylib"
    assert pkg["coreference"]["via"] == ["name_before_version", "registry_pypi"]
    # the project's own registry has the name but not the version, npm has it: asked, not guessed
    res = run(world, "how does fancylib v2.31 parse headers?", network="on",
              transport=_registry(tmp_path / "c2", "fancylib", ["0.1.0"], ["2.31.0"]))
    assert not [r for r in res["references"] if r["class"] == "package"]
    assert any("which one is meant" in u["why"] for u in res["unbound_mentions"] if u.get("text") == "fancylib")
    assert res["status"] != "complete" and res["questions_for_user"]  # v2.31: the user wrote a version


def test_a_bare_number_keeps_the_local_origin_over_a_repository_of_another_sentence(world, tmp_path):
    import subprocess

    proj = helpers.analysed_project(tmp_path / "withorigin")
    if not (proj / ".git").exists():
        subprocess.run(["git", "init", "-q"], cwd=proj, check=True)
    subprocess.run(["git", "remote", "add", "origin", "https://github.com/me/app"], cwd=proj, check=True)
    res = run(world, "Our PR #7 fixes the crash. The same bug was reported in psf/requests.", project=proj,
              network="off")
    pr, = [r for r in res["references"] if r["class"] == "pull_request"]
    assert pr["identity"]["canonical_url"] == "https://github.com/me/app"


def test_a_sentence_with_two_repositories_binds_each_number_to_the_one_written_after_it(world):
    res = run(world, "Compare PR #5 in psf/requests with PR #7 in urllib3/urllib3", network="off")
    urls = sorted(r["identity"]["canonical_url"] for r in res["references"] if r["class"] == "pull_request")
    assert urls == ["https://github.com/psf/requests", "https://github.com/urllib3/urllib3"]
    # the same repository written twice is one repository
    res = run(world, "Is issue #6000 fixed between psf/requests@v2.30.0 and psf/requests@v2.31.0?", network="off")
    issue, = [r for r in res["references"] if r["class"] == "issue"]
    assert issue["identity"]["canonical_url"] == "https://github.com/psf/requests"
