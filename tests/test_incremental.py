"""The update proportional to the change (verinoda/incremental.py): its graph equals a fresh copy's scan.

The acceptance gate is the oracle fuzz (tools/incremental_fuzz.py): random edits on fixture corpora in Python,
TypeScript/JavaScript, Go and Java, each followed by ``verinoda update`` with the switch on, and the graph
compared with a scan of a fresh copy of the edited tree (tools/graph_equal.py). A fast subset with fixed seeds
runs here; the long run is the tool's.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import graph_equal  # noqa: E402
import incremental_fuzz as fz  # noqa: E402

from verinoda import incremental  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")

CORPORA = {
    "python": ROOT / "examples" / "orders_app",
    "typescript": ROOT / "tests" / "fixtures" / "incremental" / "ts_app",
    "go": ROOT / "tests" / "fixtures" / "incremental" / "go_app",
    "java": ROOT / "examples" / "glow_mod",
}


@pytest.fixture(autouse=True)
def _graphify_out(monkeypatch):
    monkeypatch.setenv("GRAPHIFY_OUT", ".verinoda/index")
    monkeypatch.delenv(incremental.ENV, raising=False)


@pytest.mark.parametrize("lang,seed", [("python", 11), ("typescript", 12), ("go", 13), ("java", 14)])
def test_fuzzed_updates_equal_a_fresh_scan(tmp_path, lang, seed):
    rows = fz.run(CORPORA[lang], seed, 3, tmp_path, switch="verify", keep_failed=False)
    bad = [r for r in rows if not r["equal"]]
    assert not bad, json.dumps(bad, indent=1)[:3000]
    assert any(r["used"] for r in rows), rows  # the patch ran, not only the fallback


def test_the_switch_is_off_by_default(tmp_path, monkeypatch):
    repo = tmp_path / "r"
    fz.copy_tree(CORPORA["python"], repo)
    assert not incremental.enabled(repo)
    fz._scan(repo, None)
    assert not (incremental.ledger_dir(repo) / incremental.LEDGER_FILE).exists()
    p = repo / "orders" / "pricing.py"
    p.write_text(p.read_text(encoding="utf-8") + "\n\ndef added(x):\n    return x\n", encoding="utf-8")
    res = fz._update(repo, None)
    assert res["index_mode"] == "full" and "incremental" not in res
    monkeypatch.setenv(incremental.ENV, "0")
    assert not incremental.enabled(repo)
    monkeypatch.setenv(incremental.ENV, "1")
    assert incremental.enabled(repo)


def test_an_update_says_why_it_fell_back(tmp_path):
    repo = tmp_path / "r"
    fz.copy_tree(CORPORA["python"], repo)
    fz._scan(repo, "1")
    assert (incremental.ledger_dir(repo) / incremental.LEDGER_FILE).exists()
    # a new file: the corpus changed
    (repo / "orders" / "extra.py").write_text("def extra():\n    return 1\n", encoding="utf-8")
    res = fz._update(repo, "1")
    assert res["index_mode"] == "full"
    assert res["incremental"]["used"] is False and "added" in res["incremental"]["reason"]
    # the full build wrote the ledger again: an edit is patched now
    p = repo / "orders" / "pricing.py"
    p.write_text(p.read_text(encoding="utf-8") + "\n\ndef added(x):\n    return extra()\n", encoding="utf-8")
    res = fz._update(repo, "1")
    assert res["index_mode"] == "incremental" and res["incremental"]["used"] is True
    assert "orders/pricing.py" in res["incremental"]["affected"]
    fresh = fz.fresh_graph(repo, tmp_path)
    assert graph_equal.compare(fz._graph(repo), fresh)["equal"]
    # a graph.json the ledger does not describe (rewritten by something else): the full build
    gp = repo / ".verinoda" / "index" / "graph.json"
    gp.write_text(gp.read_text(encoding="utf-8").rstrip("\n") + " \n", encoding="utf-8")
    p.write_text(p.read_text(encoding="utf-8") + "\n# note\n", encoding="utf-8")
    res = fz._update(repo, "1")
    assert res["incremental"]["used"] is False and "graph.json" in res["incremental"]["reason"]


def test_a_language_the_closure_was_not_checked_for_falls_back(tmp_path):
    repo = tmp_path / "r"
    fz.copy_tree(ROOT / "examples" / "forge_mod", repo)
    fz._scan(repo, "1")
    kt = sorted(repo.rglob("*.kt"))[0]
    kt.write_text(kt.read_text(encoding="utf-8") + "\n// note\n", encoding="utf-8")
    res = fz._update(repo, "1")
    assert res["incremental"]["used"] is False and "not checked" in res["incremental"]["reason"]
    assert graph_equal.compare(fz._graph(repo), fz.fresh_graph(repo, tmp_path))["equal"]


def test_build_rows_is_the_vendored_builder(tmp_path):
    """build_rows on a full build's extraction gives the graph build_from_json + to_json give (community aside)."""
    import io
    from contextlib import redirect_stderr, redirect_stdout

    from verinoda.project_index.build import build_from_json
    from verinoda.project_index.export import to_json

    for corpus in CORPORA.values():
        repo = tmp_path / corpus.name
        fz.copy_tree(corpus, repo)
        fz._scan(repo, "1")
        x = json.loads((incremental.ledger_dir(repo) / incremental.EXTRACTION_FILE).read_text(encoding="utf-8"))
        nodes, links = incremental.build_rows(json.loads(json.dumps(x)))
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            G = build_from_json(json.loads(json.dumps(x)))
            to_json(G, {}, str(tmp_path / "g.json"), force=True, built_at_commit="x")
        ref = json.loads((tmp_path / "g.json").read_text(encoding="utf-8"))
        res = graph_equal.compare(ref, {"nodes": list(nodes.values()), "links": links})
        assert res["equal"], (corpus.name, res["counts"], res["edges_only_in_a"][:2], res["edges_only_in_b"][:2])
        # and the scan's graph.json is that graph (the ledger holds what the build read)
        assert graph_equal.compare(fz._graph(repo), {"nodes": list(nodes.values()), "links": links})["equal"]


def test_the_vendored_builder_is_the_pinned_one():
    """build_rows reproduces build_from_json/to_json: when build.py or export.py change, check build_rows against
    them again (test_build_rows_is_the_vendored_builder, the fuzz) and update PINNED."""
    assert incremental.pinned_now() == incremental.PINNED


def test_a_config_key_turns_it_on(tmp_path):
    repo = tmp_path / "r"
    fz.copy_tree(CORPORA["python"], repo)
    fz._scan(repo, None)
    cfg = repo / ".verinoda" / "config.json"
    data = json.loads(cfg.read_text(encoding="utf-8"))
    data.setdefault("index", {})["incremental"] = True
    cfg.write_text(json.dumps(data), encoding="utf-8")
    old = os.environ.pop(incremental.ENV, None)
    try:
        assert incremental.enabled(repo)
    finally:
        if old is not None:
            os.environ[incremental.ENV] = old
