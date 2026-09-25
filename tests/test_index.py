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


def test_cached_paths_are_re_anchored_as_the_upstream_function_does(built):
    """_absolutize_once keeps the answer per path string for a build; every AST cache entry and
    the odd cases come out as the upstream function leaves them."""
    import copy

    from verinoda.project_index import cache

    repo, _ = built
    real = cache._absolutize_source_files_in
    entries = sorted(cache.cache_dir(repo, "ast").glob("*.json"))
    assert len(entries) > 5
    payloads = [json.loads(e.read_text(encoding="utf-8")) for e in entries]
    payloads += [
        {"nodes": [{"source_file": "a/b.py"}, {"source_file": str(repo / "abs.py")},
                   {"source_file": ""}, {"source_file": None}, {"definition_file": "d/e.ts"}, "not-a-dict"],
         "edges": [{"source_file": "a/b.py", "definition_file": "a/b.py"}],
         "raw_calls": [{"source_file": "./x/../y.py"}]},
        {"hyperedges": [{"source_file": "/posix/abs"}]},
        {},
    ]
    with index._absolutize_once():
        memo = cache._absolutize_source_files_in
        assert memo is not real
        for root in (repo, repo.parent, repo):  # a second root is kept apart
            for p in payloads:
                a, b = copy.deepcopy(p), copy.deepcopy(p)
                real(a, root)
                memo(b, root)
                assert a == b
        for fn in (real, memo):  # a value that is not a string raises in both
            with pytest.raises(TypeError):
                fn({"nodes": [{"source_file": ["a", "list"]}]}, repo)
    assert cache._absolutize_source_files_in is real


def test_the_files_of_the_graph_are_read_from_the_json_as_load_sees_them(built, tmp_path):
    def by_load(gp=None):
        g = index.load(repo, gp)
        return {d["source_file"] for _, d in g.G.nodes(data=True) if d.get("source_file")}

    repo, _ = built
    assert index.graph_source_files(repo) == by_load()
    # a repeated id: load() keeps the last value given for it
    gp = tmp_path / "graph.json"
    nodes = [{"id": "a", "source_file": "x.py"}, {"id": "a", "source_file": "y.py"},
             {"id": "b", "source_file": "z.py"}, {"id": "b"},
             {"id": "c", "source_file": "w.py"}, {"id": "c", "source_file": ""}]
    gp.write_text(json.dumps({"nodes": nodes, "links": []}), encoding="utf-8")
    assert index.graph_source_files(repo, gp) == by_load(gp) == {"y.py", "z.py"}


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


# -- data-shaped JSON and small batches (index._known_empty_json) --------------------------------------

def _json_project(root: Path) -> Path:
    shutil.copytree(EXAMPLE, root, ignore=shutil.ignore_patterns(".verinoda", "__pycache__", "*.pyc",
                                                                 ".pytest_cache", "*.db"))
    data = root / "data"
    data.mkdir()
    for i in range(3):  # data-shaped: the JSON extractor skips them
        (data / f"rows_{i}.json").write_text(json.dumps({"rows": [{"id": i, "name": f"row {i}"}]}),
                                             encoding="utf-8")
    (root / "package.json").write_text(json.dumps({"name": "shop", "dependencies": {"left-pad": "1"}}),
                                       encoding="utf-8")
    web = root / "web"
    web.mkdir()
    for i in range(22):  # JavaScript is never cached upstream: 22 files to extract on every build
        (web / f"m{i}.js").write_text(f"export function f{i}(x) {{ return x + {i}; }}\n", encoding="utf-8")
    return root


def test_skipped_data_json_is_remembered_and_small_batches_stay_in_this_process(tmp_path, monkeypatch):
    from verinoda.project_index import extract

    pool_calls = []
    real_parallel = extract._extract_parallel

    def counted(*a, **kw):
        pool_calls.append(len(a[0]))
        return real_parallel(*a, **kw)

    monkeypatch.setattr(extract, "_extract_parallel", counted)
    monkeypatch.setattr(index, "IN_PROCESS_OK", True)  # as on Windows
    graphs, pools = {}, {}
    for name, limit in (("in_process", index.IN_PROCESS_BYTES), ("pool", 0)):
        monkeypatch.setattr(index, "IN_PROCESS_BYTES", limit)
        pool_calls.clear()
        repo = _json_project(tmp_path / name / "proj")
        first = index.build(repo, force=True)
        second = index.build(repo)
        rows = repo / "data" / "rows_0.json"
        rows.write_text(json.dumps({"rows": [], "note": "changed"}), encoding="utf-8")
        third = index.build(repo)
        rows.write_text(json.dumps({"dependencies": {"left-pad": "1"}}), encoding="utf-8")  # now config
        fourth = index.build(repo)
        graphs[name] = [first, second, third, fourth], graph_path(repo).read_bytes()
        pools[name] = list(pool_calls)
        assert first["empty_json"] == {"replayed": 0, "recorded": 3}
        assert second["empty_json"] == {"replayed": 3, "recorded": 0}
        assert third["empty_json"] == {"replayed": 2, "recorded": 1}
        assert fourth["empty_json"] == {"replayed": 2, "recorded": 0}
        assert "data/rows_0.json" in {n.get("source_file") for n in json.loads(graphs[name][1])["nodes"]}
    # 22 or 23 files to extract after the first build: past the upstream threshold of 20 for a
    # process pool, extracted in this process instead; the same graph either way
    assert pools["in_process"] == [] and pools["pool"][1:] == [22, 23, 23]
    assert graphs["in_process"][1] == graphs["pool"][1]
    assert [s["nodes"] for s in graphs["in_process"][0]] == [s["nodes"] for s in graphs["pool"][0]]


def test_remembered_empty_json_is_dropped_when_the_extractor_changes(tmp_path, monkeypatch):
    repo = _json_project(tmp_path / "proj")
    index.build(repo, force=True)
    monkeypatch.setattr(index._known_empty_json, "_stamp", staticmethod(lambda X, jc: "another extractor"))
    assert index.build(repo)["empty_json"] == {"replayed": 0, "recorded": 3}


CONFIG_JSON = b'{\n  "dependencies": {"left-pad": "1.0.0", "right-pad": "2.0.0"}\n}\n'
DATA_JSON = b'{\n  "runs": [1, 2, 3],\n  "name": "bench"\n}\n'


def _cfg_nodes(repo: Path) -> list[str]:
    g = json.loads(graph_path(repo).read_text(encoding="utf-8"))
    return sorted(n["id"] for n in g["nodes"] if n.get("source_file") == "data/cfg.json")


def _remembered(repo: Path) -> dict:
    data = json.loads((index_dir(repo) / index.EMPTY_JSON_FILE).read_text(encoding="utf-8"))
    return {Path(p).name: v for p, v in data["files"].items()}


def test_a_json_file_rewritten_while_it_is_extracted_is_not_remembered(tmp_path, monkeypatch):
    from verinoda.project_index import extract

    monkeypatch.setattr(index, "IN_PROCESS_OK", True)  # the injected write runs in this process
    repo = _json_project(tmp_path / "proj")
    cfg = repo / "data" / "cfg.json"
    cfg.write_bytes(DATA_JSON)
    index.build(repo, force=True)
    assert "cfg.json" in _remembered(repo) and _cfg_nodes(repo) == []

    real = extract._extract_sequential

    def racing(work, *a, **kw):  # someone writes data JSON back once the build has read the config
        if any(Path(p).name == "cfg.json" for _, p in work):
            cfg.write_bytes(DATA_JSON)
        return real(work, *a, **kw)

    cfg.write_bytes(CONFIG_JSON)
    with monkeypatch.context() as m:
        m.setattr(extract, "_extract_sequential", racing)
        stats = index.build(repo)
    assert stats["ok"] and _cfg_nodes(repo) == []  # the build saw the data JSON
    assert "cfg.json" not in _remembered(repo)  # read as config, extracted as data: not kept
    cfg.write_bytes(CONFIG_JSON)
    index.build(repo)
    assert "data_cfg_dependencies_left_pad" in _cfg_nodes(repo)


def test_a_forced_build_replays_no_remembered_json(tmp_path):
    import hashlib

    repo = _json_project(tmp_path / "proj")
    index.build(repo, force=True)
    # a wrong entry under the current extractor: the config bytes remembered as skipped
    p = index_dir(repo) / index.EMPTY_JSON_FILE
    data = json.loads(p.read_text(encoding="utf-8"))
    cfg = repo / "data" / "cfg.json"
    skipped = {"nodes": [], "edges": [], "skipped": "data json (not a config/manifest)"}
    data["files"][str(cfg)] = {"h": hashlib.blake2b(CONFIG_JSON, digest_size=16).hexdigest(),
                               "r": skipped}
    p.write_text(json.dumps(data), encoding="utf-8")
    cfg.write_bytes(CONFIG_JSON)
    assert index.build(repo)["empty_json"]["replayed"] == 4 and _cfg_nodes(repo) == []  # trusted
    stats = index.build(repo, force=True)
    assert stats["empty_json"] == {"replayed": 0, "recorded": 3}
    assert "data_cfg_dependencies_left_pad" in _cfg_nodes(repo)
    assert "cfg.json" not in _remembered(repo)


def test_json_past_the_extractor_limit_is_not_read_whole(tmp_path, monkeypatch):
    import builtins
    import hashlib

    small = tmp_path / "small.json"
    small.write_bytes(DATA_JSON)
    assert index._json_digest(small) == hashlib.blake2b(DATA_JSON, digest_size=16).hexdigest()
    assert index._json_digest(tmp_path / "missing.json") is None
    big = tmp_path / "big.json"
    big.write_bytes(b'{"rows": [' + b"1, " * (index.JSON_READ_LIMIT // 3) + b"0]}")
    reads = []
    real_open = builtins.open

    class Counted:
        def __init__(self, f):
            self.f = f

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return self.f.__exit__(*a)

        def read(self, n=-1):
            reads.append(n)
            return self.f.read(n)

    monkeypatch.setattr(builtins, "open", lambda *a, **kw: Counted(real_open(*a, **kw)))
    assert index._json_digest(big) is None  # extract_json gives an error for it: nothing to keep
    monkeypatch.undo()
    assert reads == [index.JSON_READ_LIMIT + 1]


def test_what_is_left_is_extracted_here_only_when_it_is_small(tmp_path, monkeypatch):
    files = []
    for i in range(3):
        p = tmp_path / f"m{i}.ts"
        p.write_bytes(b"x" * 1000)
        files.append((i, p))
    monkeypatch.setattr(index, "IN_PROCESS_OK", True)
    monkeypatch.setattr(index, "IN_PROCESS_BYTES", 3000)
    assert index._small_batch(files)
    assert index._small_batch(files + [(3, tmp_path / "gone.ts")])
    big = tmp_path / "big.json"
    big.write_bytes(b"[" + b"0," * 2000 + b"0]")
    assert not index._small_batch(files + [(4, big)])
    assert index._small_batch(files + [(4, big)], {str(big)})  # turned down unparsed: no work
    monkeypatch.setattr(index, "IN_PROCESS_BYTES", 2999)  # the bytes decide, not only the count
    assert not index._small_batch(files)
    monkeypatch.setattr(index, "IN_PROCESS_BYTES", 3000)
    monkeypatch.setattr(index, "IN_PROCESS_BELOW", 3)
    assert not index._small_batch(files)
    monkeypatch.setattr(index, "IN_PROCESS_BELOW", 64)
    monkeypatch.setattr(index, "IN_PROCESS_OK", False)  # not measured there: the pool as before
    assert not index._small_batch(files)


def test_no_code_stamp_once_the_code_changed_on_disk(tmp_path, monkeypatch):
    pkg = tmp_path / "pkg"
    (pkg / "project_index" / "extractors").mkdir(parents=True)
    for rel in ("index.py", "portable_ids.py", "project_index/watch.py",
                "project_index/extractors/json_config.py"):
        (pkg / rel).write_text(f"# {rel}\n", encoding="utf-8")
    monkeypatch.setattr(index, "_HERE", pkg)
    monkeypatch.setattr(index, "_STAMP", [])
    monkeypatch.setattr(index, "_LOADED_CODE", index._code_files())
    assert [f[0] for f in index._LOADED_CODE] == ["index.py", "portable_ids.py",
                                                  "project_index/extractors/json_config.py",
                                                  "project_index/watch.py"]
    stamp = index._code_stamp()
    assert stamp and index._code_stamp() == stamp
    # this process loaded the code above; then the files on disk change (an edit, an upgrade)
    (pkg / "project_index" / "extractors" / "json_config.py").write_bytes(b"# changed\n")
    assert index._code_stamp() is None
    monkeypatch.setattr(index, "_LOADED_CODE", index._code_files())  # a process started now
    monkeypatch.setattr(index, "_STAMP", [])
    assert index._code_stamp() not in (None, stamp)
