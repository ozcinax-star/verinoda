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
    c = Claims(st, repo).create("local/settings.py:1 contains: CACHE_SECONDS = 30", project=snap["project"],
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
