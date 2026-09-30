"""`verinoda export`: the graph as GraphML, Neo4j Cypher, an Obsidian vault and SVG, on a scanned copy of
examples/orders_app."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import json  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import xml.etree.ElementTree as ET  # noqa: E402
from pathlib import Path  # noqa: E402

import networkx as nx  # noqa: E402
import pytest  # noqa: E402

from verinoda import graph_export, index, workflow  # noqa: E402
from verinoda.store import open_store  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "orders_app"
FRESH = {"checked": True, "count": 0, "files": []}
STATUSES = {"strong_inference", "weak_inference", "unknown"}

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args],
                          cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    dst = tmp_path_factory.mktemp("export") / "orders_app"
    shutil.copytree(EXAMPLE, dst, ignore=shutil.ignore_patterns(".verinoda", "__pycache__", "*.pyc",
                                                                ".pytest_cache", "*.db"))
    _git(dst, "init", "-q")
    _git(dst, "add", "-A")
    _git(dst, "commit", "-q", "-m", "init")
    workflow.init(dst)
    st = open_store(dst)
    try:
        workflow.scan(st, dst)
    finally:
        st.close()
    return dst


@pytest.fixture(scope="module")
def g(repo):
    return index.load(repo)


@pytest.fixture(scope="module")
def model(repo, g):
    return graph_export.build(repo, g, fresh=FRESH)


def _call_edge(g) -> tuple[str, str, dict]:
    """A cross-file call edge whose direction matters (caller in one file, callee in another)."""
    return next((u, v, d) for u, v, d in sorted(g.edges({"calls"}), key=lambda x: (x[0], x[1]))
                if g.file(u) and g.file(v) and g.file(u) != g.file(v) and d.get("confidence") == "EXTRACTED")


def test_graphml_is_the_directed_graph_with_every_parallel_edge(repo, g, tmp_path):
    res = graph_export.write(repo, "graphml", tmp_path / "g.graphml", g=g, fresh=FRESH)
    H = nx.read_graphml(res["path"], force_multigraph=True)
    assert H.is_directed() and H.is_multigraph()
    assert H.number_of_nodes() == g.G.number_of_nodes() == res["nodes"]
    assert H.number_of_edges() == g.G.number_of_edges() == res["edges"]  # parallel edges are kept, not merged
    assert H.graph["commit"] == _git(repo, "rev-parse", "HEAD") == res["commit"]
    assert "not verified" in H.graph["note"]
    u, v, d = _call_edge(g)
    got = [a for _, _, a in H.edges(u, data=True) if a["relation"] == "calls"]
    assert H.has_edge(u, v) and H.has_edge(v, u) == g.G.has_edge(v, u)  # caller -> callee, not reversed
    assert any(a["at"] == f"{d['source_file']}:{d['source_location'][1:]}" for a in got)
    node = H.nodes[u]
    assert node["file"] == g.file(u) and node["line"] == g.line(u) and node["kind"] == "callable"


def test_every_edge_carries_its_evidence_and_an_unchecked_status(model):
    assert model["edges"]
    for e in model["edges"]:
        assert e["status"] in STATUSES  # never verified: nothing was checked
        if e.get("at"):
            assert re.fullmatch(r"[^:]+(:\d+)?", e["at"]), e
            assert e["status"] == ("strong_inference" if e["confidence"] == "EXTRACTED" else "weak_inference")
        else:
            assert e["status"] == "unknown"
    assert graph_export.edge_status("EXTRACTED", None) == "unknown"
    assert graph_export.edge_status("INFERRED", "a.py:3") == "weak_inference"
    assert graph_export.edge_status("AMBIGUOUS", "a.py:3") == "weak_inference"


def test_files_changed_since_the_index_are_marked_stale(repo, g):
    m = graph_export.build(repo, g, fresh={"checked": True, "count": 1, "files": ["orders/pricing.py"]})
    stale_nodes = {n["id"] for n in m["nodes"] if n.get("stale")}
    assert stale_nodes and all(n["file"] == "orders/pricing.py" for n in m["nodes"] if n.get("stale"))
    assert all(e["at"].startswith("orders/pricing.py") for e in m["edges"] if e.get("stale"))
    assert m["stale_files"] == ["orders/pricing.py"]
    unchecked = graph_export.build(repo, g, fresh={"checked": False, "why": "no snapshot yet", "files": []})
    assert unchecked["index_freshness"] == "not checked: no snapshot yet"  # never claimed current


def test_no_path_of_this_machine_is_in_the_export(repo, g, tmp_path):
    for fmt in ("graphml", "cypher"):
        text = Path(graph_export.write(repo, fmt, tmp_path / f"x.{fmt}", g=g, fresh=FRESH)["path"]).read_text(
            encoding="utf-8")
        for form in {str(repo), repo.as_posix()}:
            assert form.lower() not in text.lower(), fmt


def test_cypher_merges_so_a_second_import_adds_nothing(model):
    text = graph_export.to_cypher(model)
    stmts = [ln for ln in text.splitlines() if ln and not ln.startswith("//")]
    assert all(s.endswith(";") for s in stmts)
    merges = [s for s in stmts if s.startswith("MERGE (n:Verinoda")]
    rels = [s for s in stmts if s.startswith("MATCH (a:Verinoda")]
    assert len(merges) == len(model["nodes"])
    assert len(rels) == len(model["edges"]) and all(" MERGE (a)-[r:" in s and "{at: '" in s for s in rels)
    assert not any("CREATE (" in s for s in stmts)


def test_cypher_and_graphml_escape_hostile_text():
    model = {"project": "p", "commit": None, "note": "n", "stale_files": [],
             "nodes": [{"id": "a", "label": "x'}) DETACH DELETE n //\nMATCH", "kind": "callable"},
                       {"id": "b<&>", "label": "bad\x01char \"q\"", "kind": "!!"}],
             "edges": [{"source": "a", "target": "b<&>", "relation": "calls; DROP", "confidence": "EXTRACTED",
                        "status": "strong_inference", "at": "a.py:1"}]}
    cy = graph_export.to_cypher(model)
    assert "\\'}) DETACH DELETE n //MATCH" in cy and "[r:CALLSDROP " in cy and "SET n:Node " in cy
    assert len([ln for ln in cy.splitlines() if ln.startswith("MERGE")]) == 2  # nothing broke out of its line
    root = ET.fromstring(graph_export.to_graphml(model).encode("utf-8"))
    ns = {"g": "http://graphml.graphdrawing.org/xmlns"}
    ids = [n.get("id") for n in root.iterfind(".//g:node", ns)]
    assert ids == ["a", "b<&>"]
    labels = [d.text for d in root.iterfind(".//g:node/g:data[@key='n_label']", ns)]
    assert labels[1] == 'badchar "q"'


def _links(text: str) -> list[str]:
    return re.findall(r"\[\[([^|\]]+)(?:\|[^\]]*)?\]\]", text)


def test_the_vault_has_a_note_per_file_and_every_link_opens_one(repo, g, tmp_path):
    vault = tmp_path / "vault"
    res = graph_export.write(repo, "obsidian", vault, g=g, fresh=FRESH)
    files = {n["file"] for n in graph_export.build(repo, g, fresh=FRESH)["nodes"] if n.get("file")}
    notes = {p.relative_to(vault).as_posix() for p in vault.rglob("*.md")}
    assert {f + ".md" for f in files} | {graph_export.INDEX_NOTE} == notes and res["notes"] == len(notes)
    links = [ln for p in vault.rglob("*.md") for ln in _links(p.read_text(encoding="utf-8"))]
    assert links and all((vault / ln).is_file() for ln in links)
    svc = (vault / "orders/service.py.md").read_text(encoding="utf-8")
    assert svc.startswith("---\nfile: \"orders/service.py\"") and "## Symbols" in svc
    assert re.search(r"^- calls \[\[orders/\w+\.py\.md\|orders/\w+\.py\]\]: `.+` -> `.+` at `orders/service\.py:\d+` "
                     r"\(EXTRACTED, strong_inference\)$", svc, re.M)


def test_the_vault_is_written_only_into_a_folder_it_owns(repo, g, tmp_path):
    foreign = tmp_path / "mine"
    foreign.mkdir()
    (foreign / "diary.md").write_text("mine", encoding="utf-8")
    with pytest.raises(ValueError, match="not empty"):
        graph_export.write(repo, "obsidian", foreign, g=g, fresh=FRESH)
    assert [p.name for p in foreign.iterdir()] == ["diary.md"]  # nothing was written

    vault = tmp_path / "vault"
    graph_export.write(repo, "obsidian", vault, g=g, fresh=FRESH)
    (vault / "own.md").write_text("a note of the user's", encoding="utf-8")
    man = json.loads((vault / graph_export.MANIFEST).read_text(encoding="utf-8"))
    man["notes"].append("gone/old.py.md")  # a note an earlier export wrote for a file since deleted
    (vault / "gone").mkdir()
    (vault / "gone/old.py.md").write_text("old", encoding="utf-8")
    (vault / graph_export.MANIFEST).write_text(json.dumps(man), encoding="utf-8")
    graph_export.write(repo, "obsidian", vault, g=g, fresh=FRESH)
    assert not (vault / "gone/old.py.md").exists() and (vault / "own.md").read_text(encoding="utf-8")


def test_note_names_stay_inside_the_vault_and_apart():
    names = graph_export._note_names(["../../etc/passwd", "a/b:c?.py", "A/B_C_.py", "x/[y]#z.md"])
    assert names["../../etc/passwd"] == "etc/passwd"
    assert names["a/b:c?.py"] != names["A/B_C_.py"]  # alike once cleaned (and on a case-folding disk): numbered
    assert names["x/[y]#z.md"] == "x/_y__z.md"


def test_svg_draws_the_most_linked_files_and_says_when_it_left_some_out(repo, g, tmp_path, monkeypatch):
    res = graph_export.write(repo, "svg", tmp_path / "g.svg", g=g, fresh=FRESH)
    root = ET.parse(res["path"]).getroot()
    assert root.tag.endswith("svg") and res["files"] == res["files_total"] and "truncated" not in res
    assert len(root.findall(".//{http://www.w3.org/2000/svg}circle")) == res["files"]
    monkeypatch.setattr(graph_export, "SVG_MAX_FILES", 3)
    res = graph_export.write(repo, "svg", tmp_path / "small.svg", g=g, fresh=FRESH)
    assert res["files"] == 3 and res["truncated"] is True and res["files_total"] > 3


def test_cli_writes_beside_the_index_and_reports_json(repo, capsys):
    from verinoda import cli

    assert cli.main(["export", "--repo", str(repo), "--json"]) == 0
    res = json.loads(capsys.readouterr().out)
    assert Path(res["path"]) == (repo / ".verinoda/index/export/graph.graphml").resolve()
    assert res["format"] == "graphml" and set(res["by_status"]) <= STATUSES and "not verified" in res["note"]
    assert cli.main(["export", "--repo", str(repo), "--format", "cypher"]) == 0
    out = capsys.readouterr().out
    assert "graph.cypher (cypher:" in out and "cypher-shell <" in out
    busy = repo / "busy"
    busy.mkdir()
    (busy / "x.txt").write_text("x", encoding="utf-8")
    assert cli.main(["export", "--repo", str(repo), "--format", "obsidian", "--out", str(busy)]) == 2
    assert "not empty" in capsys.readouterr().err
