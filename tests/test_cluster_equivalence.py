"""The faster community-detection helpers give exactly what the code they replace gave.

Local changes (Verinoda) in verinoda/project_index/cluster.py (docs/UPSTREAM.md, Modified):
_partition sorts an iterator over the edge view, cohesion_score and _split_community count a plain
graph's edges from its adjacency, remap_communities_to_previous counts overlaps in one pass. Each
test compares against the original code, kept here verbatim (or, for the remap, kept in the module
as _overlaps_by_intersection), on generated graphs shaped like code graphs and on the upstream
fixture.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import networkx as nx
import pytest

from verinoda.project_index import cluster as cl
from verinoda.project_index.build import build_from_json

FIXTURES = Path(__file__).resolve().parent.parent / "tests_upstream" / "fixtures"


# -- the original code -------------------------------------------------------------------------

def old_cohesion_score(G, community_nodes):
    n = len(community_nodes)
    if n <= 1:
        return 1.0
    subgraph = G.subgraph(community_nodes)
    actual = subgraph.number_of_edges() - nx.number_of_selfloops(subgraph)
    possible = n * (n - 1) / 2
    return actual / possible if possible > 0 else 0.0


def old_remap(communities, previous_node_community):
    if not communities:
        return {}

    new_sets = {cid: set(nodes) for cid, nodes in communities.items()}
    old_sets = {}
    for node, old_cid in previous_node_community.items():
        old_sets.setdefault(old_cid, set()).add(node)

    overlaps = []
    for old_cid, old_nodes in old_sets.items():
        for new_cid, new_nodes in new_sets.items():
            overlap = len(old_nodes & new_nodes)
            if overlap > 0:
                overlaps.append((overlap, old_cid, new_cid))
    overlaps.sort(key=lambda x: (-x[0], x[1], x[2]))

    new_to_final = {}
    used_old_ids = set()
    matched_new_ids = set()
    for _overlap, old_cid, new_cid in overlaps:
        if old_cid in used_old_ids or new_cid in matched_new_ids:
            continue
        new_to_final[new_cid] = old_cid
        used_old_ids.add(old_cid)
        matched_new_ids.add(new_cid)

    unmatched = [cid for cid in communities if cid not in matched_new_ids]
    unmatched.sort(key=lambda cid: (-len(communities[cid]), tuple(sorted(communities[cid]))))
    next_id = 0
    for new_cid in unmatched:
        while next_id in used_old_ids:
            next_id += 1
        new_to_final[new_cid] = next_id
        used_old_ids.add(next_id)
        next_id += 1

    remapped = {}
    for new_cid, nodes in communities.items():
        remapped[new_to_final[new_cid]] = sorted(nodes)
    return dict(sorted(remapped.items(), key=lambda kv: kv[0]))


# -- graphs shaped like code graphs --------------------------------------------------------------

def code_like_graph(seed: int, modules: int = 12, per_module: int = 40, directed: bool = False,
                    star: int = 0):
    """Modules of densely linked functions, sparse calls between modules, recursive self-loops, a
    few isolates and a doc hub linked to a slice of every module; with ``star``, a config module of
    one hub and ``star`` leaves (a community of 50+ nodes whose cohesion is under the split
    threshold)."""
    rng = random.Random(seed)
    G = nx.DiGraph() if directed else nx.Graph()
    ids = [[f"pkg{m}_mod_fn{i}" for i in range(per_module)] for m in range(modules)]
    order = [n for mod in ids for n in mod]
    rng.shuffle(order)
    G.add_nodes_from(order)
    for mod in ids:
        for i, u in enumerate(mod):
            for v in rng.sample(mod, 3):
                if v != u:
                    G.add_edge(u, v, relation="calls", weight=1.0)
            if rng.random() < 0.05:
                G.add_edge(u, u, relation="calls", weight=1.0)  # recursion
    for _ in range(modules * per_module // 6):
        a, b = rng.sample(range(modules), 2)
        G.add_edge(rng.choice(ids[a]), rng.choice(ids[b]), relation="imports", weight=0.5)
    G.add_node("README_md")
    for mod in ids:
        for u in rng.sample(mod, per_module // 2):
            G.add_edge("README_md", u, relation="references", weight=0.3)
    G.add_nodes_from(f"lonely_{i}" for i in range(5))
    for i in range(star):
        G.add_edge("settings_py", f"settings_key_{i}", relation="contains", weight=1.0)
    return G


def fixture_graph():
    return build_from_json(json.loads((FIXTURES / "extraction.json").read_text(encoding="utf-8")))


GRAPHS = [code_like_graph(s) for s in range(4)] + [
    code_like_graph(7, modules=30, per_module=25),
    code_like_graph(8, modules=3, per_module=120),
    code_like_graph(9, modules=10, per_module=40, star=90),
    fixture_graph(),
    nx.relabel_nodes(nx.karate_club_graph(), str),
]


def subsets(G, rng, count=60):
    nodes = list(G.nodes)
    out = [nodes, [], nodes[:1], nodes[:2]]
    for _ in range(count):
        k = rng.randint(0, len(nodes))
        s = rng.sample(nodes, k)
        if s and rng.random() < 0.3:
            s = s + s[: rng.randint(1, len(s))]  # duplicates in the list
        if rng.random() < 0.3:
            s = s + ["not_in_graph_a", "not_in_graph_b"]  # members absent from G
        out.append(s)
    return out


# -- cohesion_score and _split_community's edge count --------------------------------------------

@pytest.mark.parametrize("index", range(len(GRAPHS)))
def test_cohesion_score_equals_the_subgraph_count(index):
    G = GRAPHS[index]
    rng = random.Random(index)
    variants = [G, G.to_directed(), nx.MultiGraph(G), G.subgraph(list(G.nodes)[: len(G) // 2 + 1])]
    for H in variants:
        for s in subsets(H, rng):
            assert repr(cl.cohesion_score(H, s)) == repr(old_cohesion_score(H, s))
    assert cl._plain_graph(G) and not cl._plain_graph(variants[1]) and not cl._plain_graph(variants[3])


@pytest.mark.parametrize("index", range(len(GRAPHS)))
def test_subgraph_edge_count_equals_number_of_edges(index):
    G = GRAPHS[index]
    rng = random.Random(100 + index)
    for s in subsets(G, rng):
        view = G.subgraph(s)
        assert cl._subgraph_edge_count(G, s) == view.number_of_edges()
        assert cl._intra_pair_edges(G, s) == view.number_of_edges() - nx.number_of_selfloops(view)


def test_counts_with_self_loops_only_and_odd_nodes():
    G = nx.Graph()
    G.add_edge("a", "a")
    G.add_edge("b", "b")
    G.add_node("c")
    G.add_edge(("t", 1), ("t", 2))  # tuple nodes
    G.add_edge(("t", 1), ("t", 1))
    for s in (["a", "b", "c"], ["a"], ["a", "a"], [("t", 1), ("t", 2)], [("t", 1), "a", "zz"]):
        view = G.subgraph(s)
        assert cl._subgraph_edge_count(G, s) == view.number_of_edges()
        assert repr(cl.cohesion_score(G, s)) == repr(old_cohesion_score(G, s))
    # a tuple that is itself a node: G.subgraph takes it as that one node, and so do the counts
    assert cl._subgraph_edge_count(G, ("t", 1)) == G.subgraph(("t", 1)).number_of_edges() == 1
    with pytest.raises(nx.NetworkXError):
        cl.cohesion_score(G, ["a", ["unhashable"]])
    with pytest.raises(nx.NetworkXError):
        old_cohesion_score(G, ["a", ["unhashable"]])


# -- cluster() end to end ------------------------------------------------------------------------

def _original_counts(monkeypatch):
    monkeypatch.setattr(cl, "_plain_graph", lambda G: False)


@pytest.mark.parametrize("index", range(len(GRAPHS)))
def test_cluster_is_the_same_with_the_original_counts(index, monkeypatch):
    G = GRAPHS[index]
    inputs = [G, G.to_directed(), nx.MultiGraph(G)]
    fast = [cl.cluster(H) for H in inputs] + [cl.score_all(H, cl.cluster(H)) for H in inputs]
    fast_hubs = cl.cluster(G, exclude_hubs_percentile=95)
    _original_counts(monkeypatch)
    slow = [cl.cluster(H) for H in inputs] + [cl.score_all(H, cl.cluster(H)) for H in inputs]
    assert list(map(list, (d.items() for d in fast))) == list(map(list, (d.items() for d in slow)))
    assert list(fast_hubs.items()) == list(cl.cluster(G, exclude_hubs_percentile=95).items())


def test_the_cohesion_split_pass_runs_on_the_generated_graphs(monkeypatch):
    """The graphs above do reach both rewritten counts inside cluster()."""
    seen = {"split": 0, "cohesion": 0}
    real_split, real_pairs = cl._subgraph_edge_count, cl._intra_pair_edges

    def split(*a):
        seen["split"] += 1
        return real_split(*a)

    def pairs(*a):
        seen["cohesion"] += 1
        return real_pairs(*a)

    monkeypatch.setattr(cl, "_subgraph_edge_count", split)
    monkeypatch.setattr(cl, "_intra_pair_edges", pairs)
    for G in GRAPHS:
        cl.cluster(G)
    assert seen["split"] > 0 and seen["cohesion"] > 0


@pytest.mark.parametrize("index", range(len(GRAPHS)))
def test_partition_builds_the_same_stable_graph(index, monkeypatch):
    """sorted(iter(view)) gives the list sorted(view) gave: the graph handed to Leiden is the same,
    node order, edge order and attributes."""
    G = GRAPHS[index]
    view = G.subgraph([n for n in G.nodes if G.degree(n) > 0])
    handed = []
    monkeypatch.setattr(cl, "_native_leiden", lambda stable, resolution: handed.append(stable) or {})
    for H in (view, nx.MultiGraph(G).subgraph(list(view.nodes))):
        cl._partition(H)
        stable = handed.pop()
        ref = nx.Graph()
        ref.add_nodes_from(sorted(H.nodes(), key=str))
        if H.is_multigraph():
            rows = sorted(H.edges(data=True), key=lambda row: (
                *sorted((str(row[0]), str(row[1]))),
                json.dumps(row[2], sort_keys=True, ensure_ascii=False, default=str)))
        else:
            rows = sorted(H.edges(data=True), key=lambda row: tuple(sorted((str(row[0]), str(row[1])))))
        for u, v, attrs in rows:
            ref.add_edge(u, v, **attrs)
        assert list(stable.nodes(data=True)) == list(ref.nodes(data=True))
        assert list(stable.edges(data=True)) == list(ref.edges(data=True))
        assert [list(nbrs) for nbrs in stable.adj.values()] == [list(nbrs) for nbrs in ref.adj.values()]


# -- remap_communities_to_previous ----------------------------------------------------------------

def random_partition(nodes, rng, parts):
    comms: dict[int, list[str]] = {}
    for n in nodes:
        comms.setdefault(rng.randrange(parts), []).append(n)
    cids = list(comms)
    rng.shuffle(cids)
    return {cid: comms[cid] for cid in cids}


def test_remap_matches_the_original_on_random_partitions():
    rng = random.Random(2024)
    for trial in range(400):
        universe = [f"n{i}" for i in range(rng.randint(1, 120))]
        new_nodes = [n for n in universe if rng.random() < 0.9] or universe[:1]
        communities = random_partition(new_nodes, rng, rng.randint(1, 15))
        if rng.random() < 0.3:  # cluster() numbers from 0 by size; remap sees any ids
            communities = {cid * 7 + 3: m for cid, m in communities.items()}
        old_nodes = [n for n in universe if rng.random() < 0.85]
        previous = {}
        for n in rng.sample(old_nodes, len(old_nodes)):
            previous[n] = rng.randrange(rng.randint(1, 20)) * rng.choice((1, 1, 1, 5))
        assert cl._overlaps_by_count(communities, previous) == cl._overlaps_by_intersection(communities, previous)
        got = cl.remap_communities_to_previous(communities, previous)
        assert list(got.items()) == list(old_remap(communities, previous).items()), trial


def test_remap_with_a_node_in_two_communities_takes_the_original_count():
    communities = {0: ["a", "b", "c"], 1: ["c", "d"], 2: ["e", "e"]}
    previous = {"a": 4, "c": 4, "d": 9, "e": 1, "zz": 3}
    assert cl._overlaps_by_count(communities, previous) is None
    got = cl.remap_communities_to_previous(communities, previous)
    assert list(got.items()) == list(old_remap(communities, previous).items())
    # a node repeated inside one community is still one node
    communities = {0: ["a", "a", "b"], 1: ["c"]}
    assert cl._overlaps_by_count(communities, previous) == cl._overlaps_by_intersection(communities, previous)
    assert list(cl.remap_communities_to_previous(communities, previous).items()) == list(
        old_remap(communities, previous).items())


@pytest.mark.parametrize("index", range(len(GRAPHS)))
def test_remap_matches_the_original_on_clustered_graphs(index):
    """A clustering of the graph remapped to the clustering of a changed graph, both ways."""
    G = GRAPHS[index]
    rng = random.Random(index)
    H = G.copy()
    H.remove_nodes_from(rng.sample(list(H.nodes), len(H) // 10))
    for u in rng.sample(list(H.nodes), min(20, len(H))):
        H.add_edge(u, f"new_{u}")
    a, b = cl.cluster(G), cl.cluster(H)
    for new, prev in ((a, b), (b, a), (a, a)):
        previous = {n: cid for cid, members in prev.items() for n in members}
        got = cl.remap_communities_to_previous(new, previous)
        assert list(got.items()) == list(old_remap(new, previous).items())
