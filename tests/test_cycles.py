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
from verinoda import index, map_text, workflow  # noqa: E402
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
