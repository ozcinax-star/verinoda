"""The cycles view of the architecture map: dependency cycles between files and the fewest dependencies to cut."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import itertools  # noqa: E402
import json  # noqa: E402
import random  # noqa: E402

import networkx as nx  # noqa: E402
import pytest  # noqa: E402

from verinoda import architecture_map as am  # noqa: E402
from verinoda import copies, index, map_text, workflow  # noqa: E402
from verinoda.store import open_store  # noqa: E402


def _acyclic_without(cost: dict, cut) -> bool:
    return nx.is_directed_acyclic_graph(nx.DiGraph([e for e in cost if e not in set(cut)]))


def _brute(cost: dict) -> tuple[int, int]:
    """The cheapest (edges, references) of any edge set whose removal leaves the graph acyclic."""
    edges = sorted(cost)
    best = None
    for k in range(len(edges) + 1):
        for cut in itertools.combinations(edges, k):
            if _acyclic_without(cost, cut):
                c = (k, sum(cost[e][1] for e in cut))
                best = c if best is None or c < best else best
        if best is not None:
            return best
    return best


def _random_component(rng: random.Random, n: int, p: float) -> tuple[list[str], dict]:
    nodes = [f"f{i}.py" for i in range(n)]
    cost = {(a, b): (1, rng.randint(1, 4)) for a in nodes for b in nodes if a != b and rng.random() < p}
    return nodes, cost


def test_exact_break_set_is_the_smallest_by_edges_then_references():
    rng = random.Random(7)
    checked = 0
    for _ in range(60):
        nodes, cost = _random_component(rng, rng.randint(2, 5), 0.45)
        if len(cost) > 11 or nx.is_directed_acyclic_graph(nx.DiGraph(list(cost))):
            continue
        cut = am._exact_break(nodes, cost)
        assert _acyclic_without(cost, cut)
        assert (len(cut), sum(cost[e][1] for e in cut)) == _brute(cost)
        checked += 1
    assert checked >= 15


def test_two_files_that_depend_on_each_other_cut_the_lighter_dependency():
    cost = {("a.py", "b.py"): (1, 5), ("b.py", "a.py"): (1, 1)}
    assert am._exact_break(["a.py", "b.py"], cost) == [("b.py", "a.py")]


def test_greedy_break_set_leaves_no_cycle_and_no_cut_that_could_be_put_back():
    rng = random.Random(11)
    for _ in range(25):
        nodes, cost = _random_component(rng, rng.randint(6, 30), 0.15)
        cut = am._greedy_break(nodes, cost)
        assert _acyclic_without(cost, cut)
        for e in cut:  # every cut is needed: putting it back closes a cycle
            assert not _acyclic_without(cost, [x for x in cut if x != e])
        if len(nodes) <= 8:
            exact = am._exact_break(nodes, cost)
            assert len(cut) >= len(exact)


def _graph(tmp_path, edges: list[tuple[str, str, dict]]) -> index.Graph:
    G = nx.MultiDiGraph()
    for u, v, d in edges:
        for n in (u, v):
            f = n.split("::")[0]
            G.add_node(n, label=n.split("::")[-1], source_file=f, source_location="L1", file_type="code")
        G.add_edge(u, v, **{"confidence": "EXTRACTED", "source_file": u.split("::")[0], **d})
    return index.Graph(G=G, path=tmp_path / "graph.json", root=tmp_path)


def test_type_only_deferred_and_prose_edges_close_no_cycle(tmp_path):
    g = _graph(tmp_path, [
        ("a.ts", "b.ts", {"relation": "imports_from", "source_location": "L1"}),
        ("b.ts", "a.ts", {"relation": "imports_from", "source_location": "L2", "type_only": True}),
        ("c.ts", "d.ts", {"relation": "imports_from", "source_location": "L1"}),
        ("d.ts", "c.ts", {"relation": "imports_from", "source_location": "L3", "deferred": True}),
        ("e.py", "README.md", {"relation": "uses", "source_location": "L1"}),
        ("README.md", "e.py", {"relation": "uses", "source_location": "L4"}),
    ])
    v = am.cycles(g)
    assert v["cycles"] == [] and v["break_set_size"] == 0
    assert "no dependency cycles" in map_text.render({"cycles": v}, 10)


def test_a_cycle_that_only_inferred_edges_close_is_weak_inference(tmp_path):
    g = _graph(tmp_path, [
        ("a.py::f", "b.py::g", {"relation": "calls", "source_location": "L3"}),
        ("b.py::g", "a.py::f", {"relation": "calls", "source_location": "L7", "confidence": "INFERRED"}),
    ])
    (c,) = am.cycles(g)["cycles"]
    assert c["status"] == "weak_inference" and "INFERRED" in c["note"]
    (b,) = c["break_set"]
    assert b["closes_status"] == "weak_inference" and b["closes"][0] == b["closes"][-1]


def test_a_copy_of_the_project_is_kept_apart_and_listed_last(tmp_path, monkeypatch):
    """An edge between a copy and the project is a name resolved into the wrong tree: it never merges cycles."""
    monkeypatch.setattr(am, "_aside_roots", lambda g: ("copy/",))
    monkeypatch.setattr(copies, "load", lambda repo: [{"path": "copy"}])
    calls = {"relation": "calls", "source_location": "L2"}
    g = _graph(tmp_path, [
        ("a.py::f", "b.py::g", calls), ("b.py::g", "a.py::f", calls),
        ("copy/a.py::f", "copy/b.py::g", calls), ("copy/b.py::g", "copy/c.py::h", calls),
        ("copy/c.py::h", "copy/a.py::f", calls),
        ("copy/a.py::f", "b.py::g", calls), ("b.py::g", "copy/c.py::h", calls),
    ])
    v = am.cycles(g)
    assert [c["files"] for c in v["cycles"]] == [["a.py", "b.py"], ["copy/a.py", "copy/b.py", "copy/c.py"]]
    assert "in" not in v["cycles"][0] and v["cycles"][1]["in"] == am.ASIDE
    assert any("2 dependencies between them and the project are left out" in x for x in v["coverage"]["limits"])
    assert v["left_out_total"] == 2 and all(x["why"].startswith("between a ") for x in v["left_out"])


def test_a_configured_reference_tree_keeps_its_dependencies_on_the_project(tmp_path, monkeypatch):
    """Vendored code set aside by hand may really be loaded: a cycle through it and the project is reported."""
    monkeypatch.setattr(am, "_aside_roots", lambda g: ("vendor/",))
    monkeypatch.setattr(copies, "load", lambda repo: [])
    calls = {"relation": "calls", "source_location": "L2"}
    g = _graph(tmp_path, [
        ("a.py::f", "vendor/lib.py::g", calls), ("vendor/lib.py::g", "a.py::f", calls),
        ("vendor/x.py::f", "vendor/y.py::g", calls), ("vendor/y.py::g", "vendor/x.py::f", calls),
    ])
    v = am.cycles(g)
    assert [c["files"] for c in v["cycles"]] == [["a.py", "vendor/lib.py"], ["vendor/x.py", "vendor/y.py"]]
    assert "in" not in v["cycles"][0] and v["cycles"][1]["in"] == am.ASIDE
    assert "left_out" not in v
    assert any("configured reference trees (vendor/)" in x for x in v["coverage"]["limits"])


def test_a_standard_library_import_resolved_to_a_project_module_closes_no_cycle(tmp_path):
    """``import html`` in pkg/security.py loads the standard library, not pkg/exporters/html.py."""
    (tmp_path / "pkg" / "exporters").mkdir(parents=True)
    (tmp_path / "pkg" / "security.py").write_text("import os\nimport html\n", encoding="utf-8")
    (tmp_path / "pkg" / "exporters" / "html.py").write_text("from pkg import security\n", encoding="utf-8")
    (tmp_path / "pkg" / "a.py").write_text("import json, pkg.b\n", encoding="utf-8")
    (tmp_path / "pkg" / "b.py").write_text("from pkg import a\n", encoding="utf-8")
    g = _graph(tmp_path, [
        ("pkg/security.py", "pkg/exporters/html.py", {"relation": "imports", "source_location": "L2"}),
        ("pkg/exporters/html.py", "pkg/security.py", {"relation": "imports_from", "source_location": "L1"}),
        ("pkg/a.py", "pkg/b.py", {"relation": "imports", "source_location": "L1"}),
        ("pkg/b.py", "pkg/a.py", {"relation": "imports_from", "source_location": "L1"}),
    ])
    v = am.cycles(g)
    assert [c["files"] for c in v["cycles"]] == [["pkg/a.py", "pkg/b.py"]]   # a project import beside json stays
    (x,) = v["left_out"]
    assert (x["from"], x["to"], x["at"]) == ("pkg/security.py", "pkg/exporters/html.py", ["pkg/security.py:2"])
    assert "standard library" in x["why"] and v["files_with_dependencies"] == 4
    assert am._imports_stdlib("import html", {"pkg"}) and am._imports_stdlib("from os.path import join", {"pkg"})
    assert not am._imports_stdlib("import html", {"html"})   # a top-level html.py of the project shadows it
    assert not am._imports_stdlib("from . import html", {"pkg"})


def _ring_graph(tmp_path, pairs: list[tuple[str, str]], inferred=()) -> index.Graph:
    return _graph(tmp_path, [(f"{a}::f", f"{b}::f", {"relation": "calls", "source_location": "L1",
                                                     **({"confidence": "INFERRED"} if (a, b) in inferred else {})})
                             for a, b in pairs])


def test_large_inputs_are_capped_and_marked_truncated(tmp_path, monkeypatch):
    rng = random.Random(3)
    n = 150
    big = [(f"m{i}.py", f"m{(i + 1) % n}.py") for i in range(n)]   # one ring through every file ...
    big += [(f"m{rng.randrange(n)}.py", f"m{rng.randrange(n)}.py") for _ in range(600)]   # ... and chords
    pairs = [p for p in dict.fromkeys(big) if p[0] != p[1]]
    pairs += [(f"p{i}a.py", f"p{i}b.py") for i in range(am.CYCLES_SHOWN + 5)]
    pairs += [(f"p{i}b.py", f"p{i}a.py") for i in range(am.CYCLES_SHOWN + 5)]
    monkeypatch.setattr(am, "GREEDY_REDUCE_STEPS", 500)
    v = am.cycles(_ring_graph(tmp_path, pairs))
    json.dumps(v)
    assert v["truncated"] is True and v["cycles_total"] == am.CYCLES_SHOWN + 6
    assert len(v["cycles"]) == am.CYCLES_SHOWN and v["files_in_cycles"] == n + 2 * (am.CYCLES_SHOWN + 5)
    big_row = v["cycles"][0]
    assert big_row["size"] == n and len(big_row["files"]) == am.CYCLE_FILES_SHOWN
    assert big_row["break_method"] == "greedy, not reduced"
    assert any("work limit" in x for x in v["coverage"]["limits"])
    assert len(big_row["break_set"]) <= am.CYCLE_CUTS_SHOWN <= big_row["break_set_total"]
    assert len(big_row["dependencies"]) == am.CYCLE_EDGES_SHOWN
    assert all(len(b["steps"]) <= am.CYCLE_RING_SHOWN for b in big_row["break_set"])
    comp = [p for p in pairs if p[0].startswith("m")]
    cost = {p: (1, 1) for p in comp}   # the unreduced cut is still a break set
    assert _acyclic_without(cost, am._greedy_break([f"m{i}.py" for i in range(n)], cost, [500]))


def test_cycles_text_stays_in_its_lines_and_says_what_it_left_out(tmp_path):
    pairs = [(f"f{i}.py", f"f{j}.py") for i in range(5) for j in range(5) if i != j]   # 20 cuts' worth of knot
    pairs += [(f"p{i}a.py", f"p{i}b.py") for i in range(12)] + [(f"p{i}b.py", f"p{i}a.py") for i in range(12)]
    v = am.cycles(_ring_graph(tmp_path, pairs))
    for cap in (3, 5, 8, 12, 20):
        body = map_text._cycles(v, cap)
        assert len(body) <= cap + 1
        text = map_text.render({"cycles": v}, cap)
        assert "more cycles" in text
        if "cycle 1:" in text and len(v["cycles"][0]["break_set"]) * 2 > cap:
            assert "cuts in --json" in text
    assert "13 cycles" in map_text.render({"cycles": v}, 12)


def test_cycles_text_marks_a_cut_on_inferred_edges_only(tmp_path):
    v = am.cycles(_ring_graph(tmp_path, [("a.py", "b.py"), ("b.py", "a.py")], inferred={("b.py", "a.py")}))
    text = map_text.render({"cycles": v}, 20)
    assert "[INFERRED edges only]" in text and "(weak_inference)" in text


@pytest.fixture(scope="module")
def scanned(tmp_path_factory):
    repo = tmp_path_factory.mktemp("cycles") / "proj"
    (repo / "pkg").mkdir(parents=True)
    files = {
        "pkg/__init__.py": "",
        "pkg/a.py": "from pkg import b\n\n\ndef fa():\n    return b.fb()\n",
        "pkg/b.py": "from pkg import c\n\n\ndef fb():\n    return c.fc()\n",
        "pkg/c.py": "from pkg.a import fa\n\n\ndef fc():\n    return 1\n\n\ndef late():\n    return fa()\n",
        "pkg/x.py": "from pkg import y\n\n\ndef fx():\n    return y.fy()\n",
        "pkg/y.py": "from pkg import x\n\n\ndef fy():\n    return 2\n\n\ndef back():\n    return x.fx()\n",
        "solo.py": "from pkg import a\n\n\ndef run():\n    return a.fa()\n",
    }
    for rel, text in files.items():
        (repo / rel).write_text(text, encoding="utf-8")
    workflow.init(repo)
    st = open_store(repo)
    try:
        workflow.scan(st, repo)
    finally:
        st.close()
    return index.load(repo)


def test_scanned_cycles_with_their_break_set_and_evidence(scanned):
    v = am.cycles(scanned)
    json.dumps(v)
    assert v["view"] == "cycles" and v["coverage"]["method"] and v["coverage"]["limits"]
    assert [c["files"] for c in v["cycles"]] == [["pkg/a.py", "pkg/b.py", "pkg/c.py"], ["pkg/x.py", "pkg/y.py"]]
    assert v["files_in_cycles"] == 5 and v["break_set_size"] == 2
    tri, pair = v["cycles"]
    for c in (tri, pair):
        assert c["status"] == "strong_inference" and c["break_method"] == "exact"
        (b,) = c["break_set"]
        assert b["closes"][0] == b["closes"][-1] == b["from"] and b["closes"][1] == b["to"]
        assert b["closes_status"] == "strong_inference"
        for s in b["steps"]:  # every step of the cycle it closes is shown in the code
            assert s["at"] and s["at"][0].startswith(s["from"] + ":")
        assert all(":" in at for at in b["at"])
    assert tri["dependencies_total"] == 3 and len(tri["break_set"][0]["closes"]) == 4
    assert "solo.py" not in {f for c in v["cycles"] for f in c["files"]}


def test_cycles_text_names_the_cut_and_the_cycle_it_closes(scanned):
    text = map_text.render({"cycles": am.cycles(scanned)}, 40)
    assert "== cycles ==" in text and "2 cycles over 5 files" in text
    assert "cut pkg/" in text and "closes pkg/" in text and "limit:" in text
