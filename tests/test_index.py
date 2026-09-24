"""Index facade over project_index: build location, true edge direction, receiver
augmentation (labelled INFERRED), symbol spans, quiet builds."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import json  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import index  # noqa: E402
from verinoda.paths import graph_path, index_dir  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "orders_app"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args],
                   cwd=cwd, check=True, capture_output=True)


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    repo = tmp_path_factory.mktemp("index") / "orders_app"
    shutil.copytree(EXAMPLE, repo, ignore=shutil.ignore_patterns(".verinoda", "__pycache__", "*.pyc",
                                                                 ".pytest_cache", "*.db"))
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    stats = index.build(repo, force=True)
    return repo, stats


def test_build_writes_the_graph_under_verinoda(built):
    repo, stats = built
    gp = Path(stats["graph_path"])
    assert gp == graph_path(repo) and gp.is_file()
    assert index_dir(repo) == repo.resolve() / ".verinoda" / "index"
    assert not (repo / "graphify-out").exists()  # upstream default dir is not used
    data = json.loads(gp.read_text(encoding="utf-8"))
    assert stats["ok"] and stats["nodes"] == len(data["nodes"]) > 20 and stats["edges"] > 40
    assert len(stats["log"]) <= 2000


def test_the_upstream_graph_html_is_not_written_unless_asked(built, monkeypatch):
    repo, _ = built
    html = index_dir(repo) / "graph.html"
    assert not html.exists() and "GRAPHIFY_VIZ_NODE_LIMIT" not in os.environ  # `verinoda ui` shows the graph
    monkeypatch.setenv("GRAPHIFY_VIZ_NODE_LIMIT", "5000")  # the upstream switch: a positive limit keeps it
    try:
        assert index.build(repo, force=True)["ok"] and html.is_file()
    finally:
        monkeypatch.delenv("GRAPHIFY_VIZ_NODE_LIMIT")
    assert index.build(repo, force=True)["ok"] and not html.exists()  # and the next build removes it


def test_quiet_build_prints_nothing(built, capfd):
    repo, _ = built
    (repo / "orders" / "extra.py").write_text("def extra():\n    return 1\n", encoding="utf-8")
    try:
        index.build(repo, force=True)
        out, err = capfd.readouterr()
        assert out == "" and err == "", (out, err)  # upstream chatter (e.g. `graphify label` hints) is captured
    finally:
        (repo / "orders" / "extra.py").unlink()
        index.build(repo, force=True)


def test_load_restores_true_edge_direction(built):
    repo, _ = built
    g = index.load(repo, augment=False)
    calls = {(g.label(u), g.label(v)) for u, v, _ in g.edges({"calls"})}
    assert ("create_order_handler()", "place_order()") in calls
    assert ("place_order()", "compute_total()") in calls and ("compute_total()", "apply_discount()") in calls
    assert ("place_order()", "create_order_handler()") not in calls
    # Without augmentation the extractor has no edge for `repo.save(...)`.
    assert ("place_order()", ".save()") not in calls


def test_receiver_augmentation_is_labelled_inferred(built):
    repo, _ = built
    g = index.load(repo)
    aug = [(g.label(u), g.label(v), d) for u, v, d in g.edges({"calls"}) if d.get("_origin") == index.RECEIVER_ORIGIN]
    got = {(a, b) for a, b, _ in aug}
    assert got == {("place_order()", ".save()"), ("fetch_order()", ".get()")}
    for _, _, d in aug:
        assert d["confidence"] == "INFERRED" and d["source_file"] == "orders/service.py"
        assert d["source_location"] in ("L22", "L26") and "on OrderRepository" in d["context"]
    # Idempotent: a second pass adds nothing.
    assert index.augment_python_receiver_calls(g) == 0


def test_spans_and_source_excerpts(built):
    repo, _ = built
    g = index.load(repo)
    place = g.resolve("orders/service.py::place_order")[0]
    assert g.span(place) == (19, 22)
    s, e, text = g.source(place)
    assert (s, e) == (19, 22) and text.splitlines()[0].startswith("def place_order(")
    cls = g.resolve("OrderRepository")[0]
    assert g.span(cls) == (8, 26)
    assert g.source(cls, max_lines=3)[:2] == (8, 10)
    assert g.is_symbol(place) and not g.is_file_node(place)
    f = g.resolve("orders/service.py")[0]
    assert g.is_file_node(f) and not g.is_symbol(f)
    assert [g.label(n) for n in g.symbols_in("orders/pricing.py")] == ["compute_total()", "apply_discount()"]


def test_load_without_graph_raises(tmp_path):
    with pytest.raises(FileNotFoundError, match="verinoda scan"):
        index.load(tmp_path)


# -- file nodes, per-file symbol index -------------------------------------------------------------

def _graph(tmp_path, nodes: list[dict]) -> index.Graph:
    import networkx as nx

    G = nx.MultiDiGraph()
    for n in nodes:
        G.add_node(n["id"], **{k: v for k, v in n.items() if k != "id"})
    return index.Graph(G=G, path=tmp_path / "graph.json", root=tmp_path)


def test_path_labelled_module_nodes_are_file_nodes(tmp_path):
    """Colliding basenames make the extractor label module nodes with a path suffix
    (``extractors/__init__.py``) or the whole path; those are file nodes, not symbols."""
    g = _graph(tmp_path, [
        {"id": "a", "label": "__init__.py", "source_file": "pkg/__init__.py", "file_type": "code"},
        {"id": "b", "label": "extractors/__init__.py", "source_file": "pkg/extractors/__init__.py",
         "file_type": "code"},
        {"id": "c", "label": "pkg/other/__init__.py", "source_file": "pkg/other/__init__.py", "file_type": "code"},
        {"id": "d", "label": "copilot/references/query.md", "source_file": "skills/copilot/references/query.md",
         "file_type": "document"},
        {"id": "f", "label": "load()", "source_file": "pkg/extractors/__init__.py", "file_type": "code",
         "source_location": "L3"},
        # a label that merely ends like the basename but is not a suffix of the path
        {"id": "x", "label": "elsewhere/__init__.py", "source_file": "pkg/extractors/__init__.py",
         "file_type": "code"},
    ])
    assert all(g.is_file_node(n) for n in "abcd")
    assert not g.is_symbol("b") and not g.is_symbol("c")
    assert g.is_symbol("f") and not g.is_file_node("f")
    assert not g.is_file_node("x")
    assert set(g.symbols_in("pkg/extractors/__init__.py")) == {"f", "x"}


def test_symbol_at_is_innermost_and_own_lines_exclude_nested(built):
    repo, _ = built
    g = index.load(repo)
    cls = g.resolve("OrderRepository")[0]
    save = g.resolve("OrderRepository.save")[0]
    s, e = g.span(save)
    assert g.symbol_at("orders/repository.py", s) == save and g.symbol_at("orders/repository.py", e) == save
    assert g.symbol_at("orders/repository.py", 8) == cls        # class line, outside every method
    assert g.symbol_at("orders/repository.py", 1) is None       # module level
    assert g.symbol_at("orders/repository.py", 10_000) is None  # past the end
    cs, ce = g.span(cls)
    methods = [v for v, _ in g.out_edges(cls, {"method"})]
    nested = sum(g.span(m)[1] - g.span(m)[0] + 1 for m in methods)
    assert 0 < g.own_line_count(cls) <= (ce - cs + 1) - nested + 1
    assert g.own_line_count(save) == e - s + 1
    # the undirected view used by the Graphify scorers is cached per graph size
    assert g.undirected() is g.undirected()


# -- spans from one pass per file: Markdown sections, tree-sitter end lines ---------------------------

CHANGELOG = """# Changelog

## 1.2.0

### Fixed

- a bug

```md
# not a heading (fenced)
```

## 1.1.0

- older entry
""" + "\n".join(f"- filler {i}" for i in range(100)) + "\n"


def test_markdown_sections_end_before_the_next_heading_of_the_same_level():
    lines = CHANGELOG.splitlines()
    heads = index.md_headings(lines)
    assert [ln for ln, _ in heads] == [1, 3, 5, 13] and [lvl for _, lvl in heads] == [1, 2, 3, 2]
    ends = index.md_section_ends(lines)
    assert ends[3] == 11          # 1.2.0 keeps its ### Fixed sub-section, stops before 1.1.0
    assert ends[5] == 11 and ends[13] == len(lines) and ends[1] == len(lines)
    setext = index.md_headings(["Title", "=====", "", "text", "Sub", "---"])
    assert setext == [(1, 1), (5, 2)]
    assert index.md_headings(["---", "title: x", "---", "# Real"]) == [(4, 1)]  # front matter skipped


def test_changelog_headings_get_section_spans_not_80_lines(tmp_path):
    repo = tmp_path / "docs_repo"
    repo.mkdir()
    (repo / "CHANGELOG.md").write_bytes(CHANGELOG.encode("utf-8"))
    (repo / "app.py").write_bytes(b"def f():\n    return 1\n")
    index.build(repo, force=True)
    g = index.load(repo)
    heads = {g.label(n): n for n in g.headings_in("CHANGELOG.md")}
    assert "1.2.0" in heads, heads
    assert g.span(heads["1.2.0"]) == (3, 11) and g.span_basis(heads["1.2.0"]) == "section"
    page = next(n for n, d in g.G.nodes(data=True) if d.get("source_file") == "CHANGELOG.md" and g.is_file_node(n))
    assert g.span(page) == (1, len(CHANGELOG.splitlines())) and g.span_basis(page) == "file"
    fn = g.resolve("app.py::f")[0]
    assert g.span(fn) == (1, 2) and g.span_basis(fn) == "ast"


JS = """function outer(a) {
  const inner = (b) => {
    return b + 1;
  };
  return inner(a);
}

class Box {
  constructor(v) {
    this.v = v;
  }

  get() {
    return this.v;
  }
}
"""


def test_tree_sitter_gives_non_python_symbols_their_real_end_line(tmp_path):
    ends = index.ts_def_ends(JS.encode("utf-8"), ".js")
    assert ends[1] == 6 and ends[8] == 16 and ends[9] == 11 and ends[13] == 15
    repo = tmp_path / "js_repo"
    repo.mkdir()
    (repo / "box.js").write_bytes(JS.encode("utf-8"))
    index.build(repo, force=True)
    g = index.load(repo)
    spans = {g.label(n): (g.span(n), g.span_basis(n)) for n in g.symbols_in("box.js")}
    assert spans["outer()"] == ((1, 6), "tree-sitter"), spans
    assert spans["Box"] == ((8, 16), "tree-sitter")
    assert index.ts_def_ends(b"x", ".unknown") == {}


# -- receiver-call sidecar ----------------------------------------------------------------------------

def test_receiver_edges_are_stored_at_build_and_applied_without_parsing(built, monkeypatch):
    repo, _ = built
    side = json.loads((index_dir(repo) / "receiver_calls.json").read_text(encoding="utf-8"))
    assert side["version"] == index.RECEIVER_SIDECAR_VERSION and len(side["edges"]) == 2
    assert index.same_graph(graph_path(repo), side["graph"])

    def no_parse(*a, **k):
        raise AssertionError("load() must not parse files when the sidecar matches the graph")

    monkeypatch.setattr(index, "_py_info", no_parse)
    monkeypatch.setattr(index, "py_file_info", no_parse)
    g = index.load(repo)
    got = {(g.label(u), g.label(v)) for u, v, d in g.edges({"calls"}) if d.get("_origin") == index.RECEIVER_ORIGIN}
    assert got == {("place_order()", ".save()"), ("fetch_order()", ".get()")}


def test_stale_sidecar_is_recomputed_and_matches_the_in_memory_pass(built):
    repo, _ = built
    sc = index_dir(repo) / "receiver_calls.json"
    data = json.loads(sc.read_text(encoding="utf-8"))
    data["graph"] = {"size": 1, "mtime_ns": 1, "sha256": "0" * 64}
    data["edges"] = []
    sc.write_text(json.dumps(data), encoding="utf-8")
    g = index.load(repo)
    plain = index.load(repo, augment=False)
    assert index.augment_python_receiver_calls(plain) == 2
    key = lambda gr: sorted((u, v, d["source_location"]) for u, v, d in gr.edges({"calls"})  # noqa: E731
                            if d.get("_origin") == index.RECEIVER_ORIGIN)
    assert key(g) == key(plain)
    assert json.loads(sc.read_text(encoding="utf-8"))["edges"]  # rewritten for the current graph
    stats = index.refresh_receiver_sidecar(repo)
    assert stats == {"edges": 2, "files_parsed": 0, "files_reused": stats["files_reused"]}


# -- vendored rebuild: the path-identity memo keeps the graph byte-identical ---------------------------

def _update_once(tmp_path: Path, name: str, memo: bool) -> bytes:
    repo = tmp_path / name
    shutil.copytree(EXAMPLE, repo, ignore=shutil.ignore_patterns(".verinoda", "__pycache__", "*.pyc",
                                                                 ".pytest_cache", "*.db"))
    index.build(repo, force=True)
    svc = repo / "orders" / "service.py"
    svc.write_bytes(svc.read_bytes() + b"\n\ndef added_later():\n    return fetch_order\n")
    if memo:
        index.install_path_identity_memo()
    else:
        index.uninstall_path_identity_memo()
    try:
        from verinoda.project_index.watch import _rebuild_code

        assert _rebuild_code(repo, changed_paths=[svc], block_on_lock=True)
    finally:
        index.install_path_identity_memo()
    return graph_path(repo).read_bytes()


def test_path_identity_memo_keeps_the_graph_byte_identical(tmp_path):
    from verinoda.project_index import watch

    with_memo = _update_once(tmp_path, "a", memo=True)
    without = _update_once(tmp_path, "b", memo=False)
    assert with_memo == without
    assert watch._StoredSourcePaths._verinoda_memo is True  # build() installs it


# -- deleted files never stay in the graph ---------------------------------------------------------------

def test_prune_missing_files_drops_their_nodes_and_edges(tmp_path):
    repo = tmp_path / "pr"
    shutil.copytree(EXAMPLE, repo, ignore=shutil.ignore_patterns(".verinoda", "__pycache__", "*.pyc",
                                                                 ".pytest_cache", "*.db"))
    index.build(repo, force=True)
    before = json.loads(graph_path(repo).read_text(encoding="utf-8"))
    (repo / "orders" / "pricing.py").unlink()     # the graph still describes it
    assert index.missing_source_files(repo) == ["orders/pricing.py"]
    assert index.prune_missing_files(repo) == ["orders/pricing.py"]
    after = json.loads(graph_path(repo).read_text(encoding="utf-8"))
    assert index.missing_source_files(repo) == []
    assert len(after["nodes"]) < len(before["nodes"])
    ids = {n["id"] for n in after["nodes"]}
    links = after.get("links", after.get("edges", []))
    assert all(e["source"] in ids and e["target"] in ids for e in links)
    assert index.prune_missing_files(repo) == []  # idempotent
    g = index.load(repo)
    assert not any(g.file(n) == "orders/pricing.py" for n in g.G.nodes)
