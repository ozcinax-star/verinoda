"""scan/update keep the derived index data (search index, receiver edges, lexicon, symbol facts)
in step with the graph, and a deleted file never stays in the graph."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".repoatlas/index")

import json  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import types  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from repoatlas import index, retrieval, search_index, workflow  # noqa: E402
from repoatlas.paths import graph_path, index_dir  # noqa: E402
from repoatlas.store import open_store  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "orders_app"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args],
                   cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def proj(tmp_path):
    repo = tmp_path / "orders_app"
    shutil.copytree(EXAMPLE, repo, ignore=shutil.ignore_patterns(".repoatlas", "__pycache__", "*.pyc",
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
    assert res["index_mode"] == "incremental" and si["mode"] == "incremental" and si["files_indexed"] >= 1
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

    lex = types.ModuleType("repoatlas.lexicon")
    lex.build = lex_build
    anc = types.ModuleType("repoatlas.anchors")
    anc.update_facts = update_facts
    monkeypatch.setitem(sys.modules, "repoatlas.lexicon", lex)
    monkeypatch.setitem(sys.modules, "repoatlas.anchors", anc)
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
