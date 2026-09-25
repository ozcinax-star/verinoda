"""scan/update keep the derived index data (search index, receiver edges, lexicon, symbol facts)
in step with the graph, and a deleted file never stays in the graph."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import json  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import types  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import index, retrieval, search_index, workflow  # noqa: E402
from verinoda.paths import graph_path, index_dir  # noqa: E402
from verinoda.store import open_store  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "orders_app"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args],
                   cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def proj(tmp_path):
    repo = tmp_path / "orders_app"
    shutil.copytree(EXAMPLE, repo, ignore=shutil.ignore_patterns(".verinoda", "__pycache__", "*.pyc",
                                                                 ".pytest_cache", "*.db"))
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    workflow.init(repo)
    st = open_store(repo)
    yield repo, st
    st.close()


def _graph_files(repo: Path) -> set[str]:
    data = json.loads(graph_path(repo).read_text(encoding="utf-8"))
    return {n["source_file"] for n in data["nodes"] if n.get("source_file")}


def test_scan_builds_the_search_index_and_receiver_sidecar(proj):
    repo, st = proj
    res = workflow.scan(st, repo)
    d = res["derived"]
    assert d["search_index"]["mode"] == "full" and d["search_index"]["units"] > 20
    assert (index_dir(repo) / "search.db").is_file() and (index_dir(repo) / "receiver_calls.json").is_file()
    for name in ("lexicon", "anchors"):  # other tracks: reported when installed, never fatal
        assert name not in d or "error" not in d[name] or isinstance(d[name]["error"], str)
    g = index.load(repo)
    assert search_index.open_for(g).notes == []  # nothing to sync at query time


def test_update_reindexes_only_what_changed(proj):
    repo, st = proj
    workflow.scan(st, repo)
    svc = repo / "orders" / "service.py"
    svc.write_bytes(svc.read_bytes() + b"\n\ndef audit_trail_marker():\n    return 'audit'\n")
    res = workflow.update(st, repo)
    si = res["derived"]["search_index"]
    # the graph is rebuilt over the corpus (cross-file edges stay right); the search index only
    # re-reads the changed file
    assert res["index_mode"] == "full" and si["mode"] == "incremental" and si["files_indexed"] >= 1
    g = index.load(repo)
    assert retrieval.retrieve(g, "audit trail marker")["items"][0]["symbol"] == "audit_trail_marker()"
    again = workflow.update(st, repo)
    assert again["mode"] == "noop" and "derived" not in again


def test_deleted_file_leaves_the_graph_and_the_search_index(proj):
    repo, st = proj
    workflow.scan(st, repo)
    (repo / "orders" / "pricing.py").unlink()
    res = workflow.update(st, repo)
    assert res["changed"]["removed"] == ["orders/pricing.py"]
    assert "orders/pricing.py" not in _graph_files(repo)
    assert index.missing_source_files(repo) == []
    assert res["derived"]["search_index"]["files_removed"] == 1
    g = index.load(repo)
    hits = search_index.rank(g, "compute_total apply_discount").hits
    assert not any(h.file == "orders/pricing.py" for h in hits)


def test_nodes_the_indexer_keeps_for_a_deleted_file_are_pruned(proj, monkeypatch):
    """Acceptance-audit defect: an update that kept a deleted file's nodes."""
    repo, st = proj
    workflow.scan(st, repo)
    stale_graph = graph_path(repo).read_bytes()
    (repo / "orders" / "config.py").unlink()
    real_build = index.build

    def keeps_everything(r, **kw):  # an indexer that leaves the old graph in place
        graph_path(r).write_bytes(stale_graph)
        return {"ok": True, "graph_path": str(graph_path(r)), "nodes": 0, "edges": 0, "log": ""}

    monkeypatch.setattr(index, "build", keeps_everything)
    res = workflow.update(st, repo)
    assert res["pruned_missing_files"] == ["orders/config.py"]
    assert index.missing_source_files(repo) == [] and "orders/config.py" not in _graph_files(repo)
    data = json.loads(graph_path(repo).read_text(encoding="utf-8"))
    assert res["snapshot"]["graph_nodes"] == len(data["nodes"])
    monkeypatch.setattr(index, "build", real_build)


def test_scan_forces_a_rebuild_when_the_graph_describes_deleted_files(proj, monkeypatch):
    repo, st = proj
    workflow.scan(st, repo)
    (repo / "orders" / "api.py").unlink()
    forced = []
    real_build = index.build

    def spy(r, **kw):
        forced.append(kw.get("force"))
        return real_build(r, **kw)

    monkeypatch.setattr(index, "build", spy)
    res = workflow.scan(st, repo)
    assert forced == [True] and res["forced_for_deleted_files"] == ["orders/api.py"]
    assert "orders/api.py" not in _graph_files(repo)


def test_a_refused_incremental_update_after_deletions_is_retried_in_full(proj, monkeypatch):
    repo, st = proj
    workflow.scan(st, repo)
    (repo / "orders" / "api.py").unlink()
    calls = []
    real_build = index.build

    def refuse_incremental(r, **kw):
        calls.append("force" if kw.get("force") else "changed")
        if not kw.get("force"):
            return {"ok": False, "graph_path": str(graph_path(r)), "nodes": 0, "edges": 0, "log": "refused"}
        return real_build(r, **kw)

    monkeypatch.setattr(index, "build", refuse_incremental)
    res = workflow.update(st, repo)
    assert calls == ["changed", "force"] and res["index_mode"] == "full" and "error" not in res
    assert "orders/api.py" not in _graph_files(repo)


def test_lexicon_and_anchor_hooks_are_called_and_their_errors_reported(proj, monkeypatch):
    repo, st = proj
    got: dict = {}

    def lex_build(r, graph=None, changed=None, *, file_hashes=None, tree_hash=None):
        got["lexicon"] = (Path(r), graph is not None, changed, bool(file_hashes), tree_hash)
        return {"units": 1}

    def update_facts(store, r, changed_paths):
        got["anchors"] = sorted(changed_paths)
        raise RuntimeError("facts exploded")

    lex = types.ModuleType("verinoda.lexicon")
    lex.build = lex_build
    anc = types.ModuleType("verinoda.anchors")
    anc.update_facts = update_facts
    monkeypatch.setitem(sys.modules, "verinoda.lexicon", lex)
    monkeypatch.setitem(sys.modules, "verinoda.anchors", anc)
    res = workflow.scan(st, repo)
    assert got["lexicon"][0] == repo.resolve() and got["lexicon"][1] and got["lexicon"][2] is None
    assert got["lexicon"][3] and got["lexicon"][4] == res["snapshot"]["tree_hash"]
    assert "orders/service.py" in got["anchors"]  # a scan passes every file
    assert res["derived"]["lexicon"]["units"] == 1
    assert res["derived"]["anchors"]["error"] == "RuntimeError: facts exploded"
    svc = repo / "orders" / "service.py"
    svc.write_bytes(svc.read_bytes() + b"\n# touched\n")
    res = workflow.update(st, repo)
    assert got["lexicon"][2] == ["orders/service.py"] and got["anchors"] == ["orders/service.py"]


# -- verify and noop updates on the facet-level rules (claims.assess_change) --------------------------

def _relation_claim(st, repo):
    from verinoda import evidence as evmod
    from verinoda.claims import Claims

    snap = st.latest_snapshot()
    ev = evmod.source_evidence(repo, "orders/api.py", 18, commit=snap["commit_sha"])
    return Claims(st, repo).create(
        "`create_order_handler()` calls `place_order()` (orders/api.py:18)", project=snap["project"], snapshot=snap,
        subjects=["orders/api.py::create_order_handler()", "orders/service.py::place_order()"],
        status="statically_verified", evidence=[(ev, "supports")], kind="relation",
        spec={"source": "orders_api_create_order_handler", "target": "orders_service_place_order",
              "target_label": "place_order()", "relation": "calls", "at": "orders/api.py:18",
              "confidence": "EXTRACTED"}, actor="test")


def _refuse_updates(monkeypatch, st):
    prev = st.latest_snapshot()
    monkeypatch.setattr(workflow, "update", lambda store, repo_: {
        "snapshot": prev, "error": "the indexer did not rewrite the graph", "hint": "verinoda scan --force",
        "stale": [], "changed_count": 1, "mode": "index_refused"})


def test_verify_judges_changes_by_the_facets_the_claim_depends_on(proj, monkeypatch):
    """Without a fresh snapshot, verify compares with the working tree: an unrelated edit in the target's
    file keeps the claim, a changed signature of the target does not."""
    repo, st = proj
    workflow.scan(st, repo)
    c = _relation_claim(st, repo)
    assert c["status"] == "statically_verified", c
    svc = repo / "orders" / "service.py"
    original = svc.read_bytes()
    svc.write_bytes(original + b"\n\ndef unrelated_helper():\n    return 1\n")
    _refuse_updates(monkeypatch, st)
    v = workflow.verify(st, repo, c["id"])
    assert v["index_refresh"]["error"]
    assert v["after"]["status"] == "statically_verified" and "unconfirmed_files" not in v, v
    assert v["source_checks"] and all({"status", "candidate", "ok"} <= set(ch) for ch in v["source_checks"])
    assert v["source_checks"][0]["status"] == "same"
    # the callee's signature changes: its facet changed and no evidence of the claim re-checks it
    svc.write_bytes(original.replace(b"def place_order(repo: OrderRepository, customer: str, items: list[dict])",
                                     b"def place_order(repo: OrderRepository, customer: str, items: list[dict], "
                                     b"note: str = '')"))
    v = workflow.verify(st, repo, c["id"])
    assert v["after"]["status"] == "stale" and v["unconfirmed_files"] == ["orders/service.py"], v
    assert any(f.get("facet") == "sig" and "place_order" in f.get("dep", "") for f in v["changed_facets"]), v


def test_noop_update_still_rechecks_citations_of_ignored_files(proj):
    from verinoda import evidence as evmod
    from verinoda.claims import Claims

    repo, st = proj
    (repo / ".gitignore").write_bytes(b"local/\n")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-q", "-m", "ignore local")
    local = repo / "local" / "settings.py"
    local.parent.mkdir()
    local.write_bytes(b"CACHE_SECONDS = 30\n")
    workflow.scan(st, repo)
    snap = st.latest_snapshot()
    assert "local/settings.py" not in st.snapshot_files(snap["id"])  # gitignored: never hashed
    ev = evmod.source_evidence(repo, "local/settings.py", 1, commit=snap["commit_sha"])
    c = Claims(st, repo).create("CACHE_SECONDS is 30 in local/settings.py:1", project=snap["project"],
                                snapshot=snap, subjects=["local/settings.py"], status="statically_verified",
                                evidence=[(ev, "supports")], actor="test")
    assert c["status"] == "statically_verified", c
    quiet = workflow.update(st, repo)
    assert quiet["mode"] == "noop" and quiet["stale"] == [] and quiet["untracked_citations"] == ["local/settings.py"]
    local.write_bytes(b"CACHE_SECONDS = 300\n")
    res = workflow.update(st, repo)
    assert res["mode"] == "noop" and [s["id"] for s in res["stale"]] == [c["id"]], res
    assert Claims(st, repo).get(c["id"])["status"] == "stale"


def test_nodes_for_files_that_never_existed_are_dropped_and_reported_apart(proj, monkeypatch, capsys):
    """A project file naming another project (sample.csproj -> ../Domain/Domain.csproj) made the
    indexer add nodes for a file that is not in the repository; they were reported as deleted."""
    from verinoda import cli

    repo, st = proj
    workflow.scan(st, repo)
    real_build = index.build

    def adds_a_phantom(r, **kw):
        res = real_build(r, **kw)
        data = json.loads(graph_path(r).read_text(encoding="utf-8"))
        data["nodes"].append({"id": "phantom", "label": "Domain.csproj", "source_file": "Domain/Domain.csproj"})
        graph_path(r).write_text(json.dumps(data), encoding="utf-8")
        return res

    monkeypatch.setattr(index, "build", adds_a_phantom)
    (repo / "orders" / "config.py").write_text("X = 1\n", encoding="utf-8")
    res = workflow.update(st, repo)
    assert "pruned_missing_files" not in res
    assert res["dropped_dangling_references"] == {"count": 1, "files": ["Domain/Domain.csproj"]}
    assert "Domain/Domain.csproj" not in _graph_files(repo)
    cli._r_derived(res)
    out = capsys.readouterr().out
    assert "no longer exist" not in out and "1 file(s) named by other files are not in the repository" in out


def test_an_update_keeps_the_cross_file_edges_of_the_files_it_re_extracts(proj):
    """The upstream incremental pass extracts only the changed files and resolves cross-file
    imports and calls within that batch, so editing service.py dropped its edges into
    pricing.py and repository.py until the next full scan."""
    repo, st = proj
    workflow.scan(st, repo)

    def edges():
        g = index.load(repo, augment=False)
        return {(g.file(u), g.label(u), d.get("relation"), g.file(v), g.label(v)) for u, v, d in g.G.edges(data=True)}

    before = edges()
    assert any(e[0] == "orders/service.py" and e[3] == "orders/pricing.py" for e in before)
    svc = repo / "orders" / "service.py"
    svc.write_bytes(svc.read_bytes() + b"\n# a comment only\n")
    workflow.update(st, repo)
    assert edges() == before


def test_a_file_edited_while_the_index_was_built_is_caught_and_rebuilt(proj):
    """The graph read the old text and the search index the new one: spans no longer fit.
    The file is stored as changed since indexing, and the next update rebuilds it."""
    repo, st = proj
    workflow.scan(st, repo)
    svc = repo / "orders" / "service.py"
    index.build(repo)                                   # the graph sees the old text ...
    svc.write_bytes(b"\n\n\n\n\n\n\n\n\n\n\n\n" + svc.read_bytes())  # ... then the file shifts
    g = index.load(repo)
    search_index.update(repo, g)
    db = search_index.db_path_for(g)
    assert search_index.misaligned_files(db) == ["orders/service.py"]
    assert "orders/service.py" in search_index.stale_files(search_index.open_for(g), repo, ["orders/service.py"])
    res = workflow.update(st, repo)
    assert "orders/service.py" in res["changed"]["modified"]
    assert search_index.misaligned_files(db) == []


def test_an_update_that_touches_no_file_of_the_graph_keeps_the_graph(proj):
    repo, st = proj
    data = repo / "deploy" / "settings.yml"
    data.parent.mkdir()
    data.write_text("orders:\n  max_items: 50\n", encoding="utf-8")
    workflow.scan(st, repo)
    assert "deploy/settings.yml" not in _graph_files(repo)  # a data file: indexed for search only
    gp = graph_path(repo)
    before = gp.read_bytes()
    data.write_text("orders:\n  max_items: 50\n  archive_after_days: 30\n", encoding="utf-8")
    res = workflow.update(st, repo)
    assert res["mode"] == "incremental" and res["index_mode"] == "none"
    assert "deploy/settings.yml" in res["changed"]["modified"] and gp.read_bytes() == before
    # the search index still sees the edit
    items = retrieval.retrieve(index.load(repo), "archive_after_days")["items"]
    assert any(i["file"] == "deploy/settings.yml" for i in items)
    # a new code file does rebuild it
    (repo / "orders" / "audit.py").write_text("def audit_order(order):\n    return order\n", encoding="utf-8")
    assert workflow.update(st, repo)["index_mode"] == "full" and "orders/audit.py" in _graph_files(repo)


# -- the vendored "topology unchanged" fast path (index._keep_unchanged_graph) --------------------

@pytest.fixture
def dangling(tmp_path):
    """orders_app plus files that name files the project does not have (a csproj's project
    references, a require of a missing module): graph.json is rewritten after every build."""
    repo = tmp_path / "orders_app"
    shutil.copytree(EXAMPLE, repo, ignore=shutil.ignore_patterns(".verinoda", "__pycache__", "*.pyc",
                                                                 ".pytest_cache", "*.db"))
    fx = ROOT / "tests_upstream" / "fixtures"
    (repo / "web" / "App").mkdir(parents=True)
    shutil.copy(fx / "cjs_require.js", repo / "web" / "app.js")
    shutil.copy(fx / "sample.csproj", repo / "web" / "App" / "App.csproj")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    workflow.init(repo)
    return repo


def _outputs(repo: Path, res: dict) -> dict:
    ix = index_dir(repo)
    side = json.loads((ix / "receiver_calls.json").read_text(encoding="utf-8"))
    side["graph"].pop("mtime_ns")
    backups = {str(p.relative_to(ix)): p.read_bytes() for d in ix.iterdir()
               if d.is_dir() and d.name[:2] == "20" for p in sorted(d.iterdir())}
    files = ("GRAPH_REPORT.md", ".graphify_labels.json", ".graphify_labels.json.sig",
             ".graphify_root")
    return {"graph": graph_path(repo).read_bytes(), "sidecar": side, "backups": backups,
            **{n: (ix / n).read_bytes() for n in files},
            "counts": (res["snapshot"]["graph_nodes"], res["snapshot"]["graph_edges"]),
            "dangling": res.get("dropped_dangling_references"), "stale": res["stale"]}


def _update_once(repo: Path, monkeypatch, edit, *, fast: bool) -> tuple[dict, dict]:
    """``edit``, then an update, with the fast path allowed or not; (outputs, the build's stats)."""
    stats: list[dict] = []
    real_build = index.build

    def spy(r, **kw):
        stats.append(real_build(r, **kw))
        return stats[-1]

    with monkeypatch.context() as m:
        m.setattr(index, "build", spy)
        if not fast:
            m.setattr(index._keep_unchanged_graph, "_unchanged", lambda self, rec: False)
        edit()
        st = open_store(repo)
        try:
            res = workflow.update(st, repo)
        finally:
            st.close()
    return _outputs(repo, res), (stats[-1] if stats else {})


def _recorded(repo: Path, tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    """Scan, then updates appending a comment until one keeps the graph (the first ones change
    it: the upstream reconcile keeps nodes of the previous graph); the state is saved."""
    st = open_store(repo)
    workflow.scan(st, repo)
    st.close()
    svc = repo / "orders" / "service.py"
    for i in range(6):
        def append(i=i):
            svc.write_bytes(svc.read_bytes() + f"\n# edit {i}\n".encode())

        if _update_once(repo, monkeypatch, append, fast=True)[1].get("graph_kept"):
            break
    else:
        pytest.fail("the graph never came out the same twice")
    assert (index_dir(repo) / index.REBUILD_RECORD).is_file()
    saved = tmp_path / "saved"
    shutil.copytree(repo, saved)
    return svc, saved


def _restore(saved: Path, repo: Path) -> None:
    def writable(fn, path, _exc):  # git's object files are read-only on Windows
        os.chmod(path, 0o700)
        fn(path)

    handler = {"onexc": writable} if sys.version_info >= (3, 12) else {"onerror": writable}
    shutil.rmtree(repo, **handler)
    shutil.copytree(saved, repo)


def test_an_update_that_rebuilds_the_same_graph_keeps_it_and_writes_what_the_full_path_writes(
        dangling, tmp_path, monkeypatch):
    repo = dangling
    svc, saved = _recorded(repo, tmp_path, monkeypatch)

    def comment():  # in place: no node or edge changes; the corpus's word count does (the report)
        svc.write_bytes(svc.read_bytes().replace(b"# edit ", b"# an edit, longer: "))

    fast, fast_stats = _update_once(repo, monkeypatch, comment, fast=True)
    assert fast_stats.get("graph_kept") is True
    assert fast["dangling"]["files"] == ["web/Domain/Domain.csproj",
                                         "web/Infrastructure/Infrastructure.csproj"]
    _restore(saved, repo)
    full, full_stats = _update_once(repo, monkeypatch, comment, fast=False)
    assert "graph_kept" not in full_stats
    assert fast == full

    def again():  # the record still describes what is on disk
        svc.write_bytes(svc.read_bytes().replace(b"an edit, longer", b"another edit"))

    assert _update_once(repo, monkeypatch, again, fast=True)[1].get("graph_kept") is True


@pytest.mark.parametrize("change", ["body", "labels", "new_file", "deleted_file"])
def test_the_full_path_runs_when_the_graph_or_a_file_the_record_fingerprints_changed(
        dangling, tmp_path, monkeypatch, change):
    repo = dangling
    svc, saved = _recorded(repo, tmp_path, monkeypatch)

    def edit():
        if change == "body":  # every later line moves
            svc.write_bytes(b"# a line on top\n" + svc.read_bytes())
        elif change == "labels":  # the labels changed by someone else
            lf = index_dir(repo) / ".graphify_labels.json"
            labels = json.loads(lf.read_text(encoding="utf-8"))
            labels[next(iter(labels))] = "a name given by hand"
            lf.write_text(json.dumps(labels, indent=2) + "\n", encoding="utf-8")
            svc.write_bytes(svc.read_bytes().replace(b"# edit ", b"# edited again "))
        elif change == "new_file":
            (repo / "orders" / "audit.py").write_bytes(b"def audit(o):\n    return o\n")
        else:
            (repo / "orders" / "config.py").unlink()

    fast, fast_stats = _update_once(repo, monkeypatch, edit, fast=True)
    assert "graph_kept" not in fast_stats
    _restore(saved, repo)
    full, _ = _update_once(repo, monkeypatch, edit, fast=False)
    assert fast == full


@pytest.fixture
def clean(tmp_path):
    """orders_app as it is: nothing in graph.json needs a rewrite after a build."""
    repo = tmp_path / "orders_app"
    shutil.copytree(EXAMPLE, repo, ignore=shutil.ignore_patterns(".verinoda", "__pycache__", "*.pyc",
                                                                 ".pytest_cache", "*.db"))
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    workflow.init(repo)
    return repo


def test_a_graph_json_the_pipeline_wrote_itself_is_left_to_the_vendored_comparison(
        clean, tmp_path, monkeypatch):
    # The vendored fast path keeps such a graph by itself and leaves GRAPH_REPORT.md and the
    # dated backup as they are; Verinoda's kept path (which rewrites them) is not for this case.
    repo = clean
    st = open_store(repo)
    workflow.scan(st, repo)
    st.close()
    ix, gp = index_dir(repo), graph_path(repo)
    svc = repo / "orders" / "service.py"
    code, extra = svc.read_bytes(), b"\n\ndef audit_order(order):\n    return order\n"

    # a function added, then removed: each time the graph changes and the full path runs (the
    # code before this fix left a record after the second that the next update matched)
    for step in (lambda: svc.write_bytes(code + extra), lambda: svc.write_bytes(code)):
        before = gp.stat().st_mtime_ns
        stats = _update_once(repo, monkeypatch, step, fast=True)[1]
        assert gp.stat().st_mtime_ns != before
        assert not stats.get("pruned_files") and not stats["portable_ids"]["changed"]
        assert not (ix / index.REBUILD_RECORD).exists()  # nothing was rewritten: no record
    saved = tmp_path / "saved"
    shutil.copytree(repo, saved)
    before, report = gp.stat().st_mtime_ns, (ix / "GRAPH_REPORT.md").read_bytes()

    def comment():  # no node or edge changes; the corpus's word count does
        svc.write_bytes(code + b"\n# a comment of a few more words\n")

    fast, fast_stats = _update_once(repo, monkeypatch, comment, fast=True)
    assert gp.stat().st_mtime_ns == before  # the vendored fast path kept the graph
    assert "graph_kept" not in fast_stats and fast["GRAPH_REPORT.md"] == report
    _restore(saved, repo)
    full, _ = _update_once(repo, monkeypatch, comment, fast=False)
    assert fast == full


def test_a_record_of_a_graph_json_nothing_rewrote_is_not_used(dangling, tmp_path, monkeypatch):
    repo = dangling
    svc, saved = _recorded(repo, tmp_path, monkeypatch)
    rp = index_dir(repo) / index.REBUILD_RECORD
    rec = json.loads(rp.read_text(encoding="utf-8"))
    assert rec["rewrote"] is True
    rp.write_text(json.dumps({**rec, "rewrote": False}), encoding="utf-8")
    shutil.copy(rp, saved / rp.relative_to(repo))

    def comment():
        svc.write_bytes(svc.read_bytes().replace(b"# edit ", b"# an edit, longer: "))

    fast, fast_stats = _update_once(repo, monkeypatch, comment, fast=True)
    assert "graph_kept" not in fast_stats  # the vendored comparison decided: graph.json differs
    _restore(saved, repo)
    full, _ = _update_once(repo, monkeypatch, comment, fast=False)
    assert fast == full
