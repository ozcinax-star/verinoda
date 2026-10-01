"""The dsm view (dependency structure matrix) and the model view (a C4 model against the code)."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import itertools  # noqa: E402
import json  # noqa: E402
import random  # noqa: E402

import networkx as nx  # noqa: E402
import pytest  # noqa: E402

from verinoda import cli, dsm, index, map_text  # noqa: E402


def _graph(tmp_path, edges: list[tuple[str, str, int]], files: tuple[str, ...] = ()) -> index.Graph:
    """Files ``u`` -> ``v`` with ``n`` EXTRACTED import edges each, cited at lines 1..n of ``u``."""
    G = nx.MultiDiGraph()
    for f in files:
        G.add_node(f, label=f, source_file=f, source_location="L1", file_type="code")
    for u, v, n in edges:
        for f in (u, v):
            G.add_node(f, label=f, source_file=f, source_location="L1", file_type="code")
        for i in range(1, n + 1):
            G.add_edge(u, v, relation="calls", confidence="EXTRACTED", source_file=u, source_location=f"L{i}")
    return index.Graph(G=G, path=tmp_path / "graph.json", root=tmp_path)


# -- the matrix ------------------------------------------------------------------------------------

def test_a_group_comes_before_what_it_uses_and_nothing_points_back_without_a_cycle(tmp_path):
    g = _graph(tmp_path, [("app/ui/v.py", "app/core/c.py", 3), ("app/core/c.py", "app/db/d.py", 2),
                          ("app/ui/v.py", "app/db/d.py", 1)])
    v = dsm.dsm(g, depth=2)
    assert [x["name"] for x in v["groups"]] == ["app/ui", "app/core", "app/db"]
    assert v["cycles"] == [] and v["against_order_references"] == 0
    assert not any(c["against_order"] for c in v["cells"])
    cell = next(c for c in v["cells"] if c["from"] == "app/ui" and c["to"] == "app/core")
    assert cell["references"] == 3 and cell["status"] == "strong_inference"
    assert cell["sites"] == ["app/ui/v.py:1", "app/ui/v.py:2", "app/ui/v.py:3"]


def test_inside_a_cycle_the_lighter_direction_points_back(tmp_path):
    g = _graph(tmp_path, [("a/x.py", "b/y.py", 9), ("b/y.py", "a/x.py", 2), ("c/z.py", "a/x.py", 1)])
    v = dsm.dsm(g, depth=1)
    assert [x["name"] for x in v["groups"]] == ["c", "a", "b"]
    assert v["cycles"] == [["a", "b"]]
    back = [c for c in v["cells"] if c["against_order"]]
    assert [(c["from"], c["to"], c["references"]) for c in back] == [("b", "a", 2)]
    assert v["against_order_references"] == 2
    text = map_text.render({"dsm": v}, 40)
    assert "cycle of 2 groups: a, b" in text and "2*" in text


def test_the_exact_order_leaves_the_fewest_references_pointing_back():
    rng = random.Random(7)
    for _ in range(40):
        nodes = [f"g{i}" for i in range(rng.randint(2, 6))]
        w = {(a, b): rng.randint(1, 9) for a in nodes for b in nodes if a != b and rng.random() < 0.5}

        def back(order):
            pos = {x: i for i, x in enumerate(order)}
            return sum(c for (a, b), c in w.items() if pos[a] > pos[b])

        best = min(back(p) for p in itertools.permutations(nodes))
        assert back(dsm._exact_order(nodes, w)) == best
        assert sorted(dsm._greedy_order(nodes, w)) == sorted(nodes)


def test_folder_depth_is_chosen_or_given(tmp_path):
    files = [f"src/p{i}/m{j}/f.py" for i in range(3) for j in range(20)]
    assert dsm.auto_depth(files, max_groups=30) == 2   # depth 3 would make 60 groups
    assert dsm.auto_depth(["a.py", "b/c.py"]) == 1
    g = _graph(tmp_path, [("top.py", "pkg/a.py", 1)])
    v = dsm.dsm(g, depth=3)
    assert {x["name"] for x in v["groups"]} == {"(root)", "pkg"}
    with pytest.raises(ValueError):
        dsm.dsm(g, depth=0)
    with pytest.raises(ValueError):
        dsm.dsm(g, by="layer")


def test_groups_by_tag_put_a_file_in_its_first_tag_and_the_rest_apart(tmp_path):
    (tmp_path / "verinoda.toml").write_text('[architecture.tags]\nui = ["app/ui/**", "app/shared/**"]\n'
                                            'core = "app/**"\nempty = "nothing/**"\n', encoding="utf-8")
    g = _graph(tmp_path, [("app/ui/v.py", "app/core/c.py", 2), ("app/shared/s.py", "app/core/c.py", 1),
                          ("tools/t.py", "app/core/c.py", 1)])
    v = dsm.dsm(g, by="tag")
    names = [x["name"] for x in v["groups"]]
    assert set(names) == {"ui", "core", "(other)"}
    cell = next(c for c in v["cells"] if c["from"] == "ui")
    assert cell["references"] == 3   # app/shared counts in ui, its first tag
    assert any("match two tags" in lim for lim in v["coverage"]["limits"])
    assert v["problems"] == ["tag empty matches no indexed code file"]
    (tmp_path / "verinoda.toml").unlink()
    v = dsm.dsm(g, by="tag")
    assert v["groups"] == [] and "no [architecture.tags]" in v["problems"][0]


# -- the model -------------------------------------------------------------------------------------

DSL = """workspace "Shop" {
    !identifiers hierarchical
    model {
        buyer = person "Buyer"   // no code
        shop = softwareSystem "Shop" {
            web = container "Web" {
                properties {
                    "verinoda.code" "app/web/**"
                }
                -> shop.api "calls"
            }
            api = container "API" {
                properties {
                    "verinoda.code" "app/api/**"
                }
                orders = component "Orders" {
                    properties {
                        "verinoda.code" "app/api/orders/**"
                    }
                }
            }
            db = container "Database" {
                properties {
                    "verinoda.code" "app/db/**, app/migrations/**"
                }
            }
            ghost = container "Ghost" {
                properties {
                    "verinoda.code" "app/ghost/**"
                }
            }
        }
        /* a block comment
           shop.web -> shop.db */
        buyer -> shop.web "uses"
        shop.api -> shop.db "reads"
        shop.web -> shop.db "should not"
        shop.api -> shop.ghost
        shop.api -> nowhere
    }
    views {
        systemContext shop {
            include *
            shop.web -> shop.db
        }
    }
}
"""


def test_the_dsl_is_read_with_hierarchical_ids_properties_and_implied_sources():
    els, rels, problems = dsm.parse_dsl(DSL, "workspace.dsl")
    ids = {e["id"]: e for e in els}
    assert set(ids) == {"buyer", "shop", "shop.web", "shop.api", "shop.api.orders", "shop.db", "shop.ghost"}
    assert ids["shop.api.orders"]["parent"] == "shop.api"
    assert ids["shop.db"]["globs"] == ["app/db/**", "app/migrations/**"]
    pairs = [(r["from"], r["to"]) for r in rels]
    assert pairs == [("shop.web", "shop.api"), ("buyer", "shop.web"), ("shop.api", "shop.db"),
                     ("shop.web", "shop.db"), ("shop.api", "shop.ghost")]   # views and comments are no relations
    assert rels[0]["at"] == "workspace.dsl:10"
    assert problems == ["workspace.dsl:39: nowhere is not an element of the model"]


def test_the_model_is_checked_against_the_code(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "workspace.dsl").write_text(DSL, encoding="utf-8")
    g = _graph(tmp_path, [
        ("app/web/page.py", "app/api/orders/svc.py", 2),   # web -> api, through a component of api
        ("app/api/orders/svc.py", "app/migrations/m1.py", 1),   # api -> db, from the component
        ("app/api/orders/svc.py", "app/api/util.py", 4),   # inside api
        ("app/db/store.py", "app/web/page.py", 3),   # undeclared
        ("tools/x.py", "app/db/store.py", 1),   # outside the model
    ])
    v = dsm.model_check(g, model="docs/workspace.dsl")
    by = {(r["from"], r["to"]): r for r in v["relations"]}
    assert by[("shop.web", "shop.api")]["result"] == "matched" and by[("shop.web", "shop.api")]["references"] == 2
    assert by[("shop.api", "shop.db")]["result"] == "matched"
    assert by[("shop.api", "shop.db")]["sites"] == ["app/api/orders/svc.py:1"]
    assert by[("buyer", "shop.web")]["result"] == "not_checked"
    assert by[("shop.web", "shop.db")]["result"] == "model_only"
    assert by[("shop.web", "shop.db")]["status"] == "strong_inference"
    assert "3 reference(s) go the other way" in by[("shop.web", "shop.db")]["why"]
    assert by[("shop.api", "shop.ghost")]["result"] == "unknown"
    assert v["status"] == "unknown"   # a relation could not be judged: never "consistent"
    assert [(u["from"], u["to"], u["references"]) for u in v["undeclared"]] == [("shop.db", "shop.web", 3)]
    assert v["undeclared"][0]["sites"] == ["app/db/store.py:1", "app/db/store.py:2", "app/db/store.py:3"]
    assert v["summary"]["dependencies_with_an_end_outside"] == 1
    assert v["summary"]["unmapped_elements"] == ["buyer"]
    text = map_text.render({"model": v}, 40)
    assert "model_only: shop.web -> shop.db" in text and "undeclared: shop.db -> shop.web" in text


def test_relations_between_tags_in_verinoda_toml(tmp_path):
    (tmp_path / "verinoda.toml").write_text(
        '[architecture.tags]\nui = "src/ui/**"\ncore = "src/core/**"\n\n'
        '[architecture.model]\nrelations = ["ui -> core", "core -> ui", "bad line"]\n', encoding="utf-8")
    g = _graph(tmp_path, [("src/ui/a.py", "src/core/b.py", 1)])
    v = dsm.model_check(g)
    res = {(r["from"], r["to"]): r["result"] for r in v["relations"]}
    assert res == {("ui", "core"): "matched", ("core", "ui"): "model_only"}
    assert v["status"] == "differs" and v["undeclared"] == []
    assert any("bad line" in p for p in v["problems"])
    (tmp_path / "verinoda.toml").write_text(
        '[architecture.tags]\nui = "src/ui/**"\ncore = "src/core/**"\n\n'
        '[architecture.model]\nrelations = ["ui -> core"]\n', encoding="utf-8")
    assert dsm.model_check(g)["status"] == "consistent"


def test_no_model_says_how_to_give_one(tmp_path):
    g = _graph(tmp_path, [("a.py", "b.py", 1)])
    v = dsm.model_check(g)
    assert v["status"] == "no_model" and "--model" in v["next_step"]
    assert "no model" in map_text.render({"model": v}, 10)
    v = dsm.model_check(g, model="missing.dsl")
    assert v["status"] == "no_model" and "cannot be read" in v["problems"][0]
    v = dsm.model_check(g, model="../outside.dsl")
    assert "outside the repository" in v["problems"][0]


def test_flags_of_another_view_are_refused(capsys):
    assert cli.main(["map", ".", "--depth", "2"]) == 2
    assert "--depth" in capsys.readouterr().err
    assert cli.main(["map", ".", "--view", "dsm", "--model", "w.dsl"]) == 2


def test_the_dsm_and_model_views_through_the_cli(tmp_path, capsys, monkeypatch):
    from verinoda import architecture_map  # noqa: F401 - the view's helpers

    g = _graph(tmp_path, [("a/x.py", "b/y.py", 2)])
    monkeypatch.setattr(cli, "_need_graph", lambda repo: None)
    monkeypatch.setattr(index, "load", lambda repo: g)
    assert cli.main(["map", str(tmp_path), "--view", "dsm", "--depth", "1", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["dsm"]["groups"][0]["name"] == "a" and out["dsm"]["cells"][0]["references"] == 2
    assert cli.main(["map", str(tmp_path), "--view", "model"]) == 0
    assert "no model" in capsys.readouterr().out
