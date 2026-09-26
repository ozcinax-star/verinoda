"""Two symbols of one file whose names differ only in case keep distinct graph ids: verinoda.case_ids."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

from verinoda import index  # noqa: E402
from verinoda.case_ids import split_case_collisions  # noqa: E402
from verinoda.mcp.server import AtlasTools  # noqa: E402
from verinoda.paths import graph_path  # noqa: E402
from verinoda.ui import data as uidata  # noqa: E402

# the senior evaluation's repro (web persona, mono): a class and its camelCase singleton in one file
SERVICE = """\
import { OrderRepository } from './orderRepository';

export class OrderService {
  constructor(private readonly repo: OrderRepository) {}

  async placeOrder(total: number): Promise<number> {
    return this.repo.save(total);
  }
}

export const orderService = new OrderService(new OrderRepository());

export function localUse(): OrderService {
  const svc = new OrderService(new OrderRepository());
  return svc;
}
"""
REPOSITORY = """\
export class OrderRepository {
  async save(total: number): Promise<number> {
    return total;
  }
}
"""
ROUTES = """\
import { orderService } from '../services/orderService';

export async function createOrderHandler(total: number): Promise<number> {
  return orderService.placeOrder(total);
}
"""
SPEC = """\
import { OrderService } from './orderService';
import { OrderRepository } from './orderRepository';

export function makeService(): OrderService {
  return new OrderService(new OrderRepository());
}
"""


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


def _h(name: str) -> str:
    return hashlib.sha1(name.encode("utf-8")).hexdigest()[:6]


def test_a_class_and_a_camelcase_const_of_one_file_are_two_nodes_after_a_build(tmp_path):
    _write(tmp_path, "src/services/orderService.ts", SERVICE)
    _write(tmp_path, "src/services/orderRepository.ts", REPOSITORY)
    _write(tmp_path, "src/routes/orders.ts", ROUTES)
    _write(tmp_path, "src/services/orderServiceSpec.ts", SPEC)
    assert index.build(tmp_path, force=True)["ok"]
    g = json.loads(graph_path(tmp_path).read_text(encoding="utf-8"))
    base = "src_services_orderservice_orderservice"
    by_id = {n["id"]: n for n in g["nodes"]}
    # before: one node `orderService` at line 11 marked as a class, and no OrderService node
    assert (by_id[base]["label"], by_id[base]["source_location"]) == ("OrderService", "L3")
    const = by_id[f"{base}_{_h('orderService')}"]
    assert (const["label"], const["source_location"]) == ("orderService", "L11")
    assert f"{base}_placeorder" in by_id   # the method ids do not change

    def edges(to: str) -> set:
        return {(e["source"], e["relation"]) for e in g["links"] if e["target"] == to}

    assert ("src_routes_orders", "imports") in edges(const["id"])   # `import { orderService }`
    assert ("src_routes_orders", "imports") not in edges(base)
    assert ("src_services_orderservicespec", "imports") in edges(base)   # `import { OrderService }`
    assert ("src_services_orderservice_localuse", "calls") in edges(base)   # `new OrderService(`
    assert ("src_services_orderservice_localuse", "calls") not in edges(const["id"])
    assert {("src_services_orderservice", "contains")} <= edges(base) & edges(const["id"])
    methods = {e["target"] for e in g["links"] if e["source"] == base and e["relation"] == "method"}
    assert f"{base}_placeorder" in methods
    # the name picks the node, case and all
    gr = index.load(tmp_path)
    assert gr.resolve("OrderService")[0] == base and gr.resolve("orderService")[0] == const["id"]
    tools = AtlasTools(tmp_path)
    got = tools.node_inspect("orderService")["node"]
    assert (got["id"], got["line"]) == (const["id"], 11)
    assert tools.node_inspect("OrderService")["node"]["kind"] == "class"
    atlas = uidata.Atlas(tmp_path)   # the UI's name search puts the name written with its case first
    for q, first, second in (("orderService", const["id"], base), ("OrderService", base, const["id"])):
        ids = [r["id"] for r in atlas.search(q)["results"]]
        assert ids.index(first) < ids.index(second), (q, ids)
    # an update after an edit elsewhere keeps the split and the importer's edge
    _write(tmp_path, "src/routes/orders.ts", ROUTES + "\nexport const unused = 1;\n")
    assert index.build(tmp_path, changed=[tmp_path / "src/routes/orders.ts"])["ok"]
    g2 = json.loads(graph_path(tmp_path).read_text(encoding="utf-8"))
    by2 = {n["id"]: n for n in g2["nodes"]}
    assert by2[base]["label"] == "OrderService" and by2[const["id"]]["label"] == "orderService"
    assert any(e["source"] == "src_routes_orders" and e["target"] == const["id"] for e in g2["links"])
    # ... and so does an update of the file itself
    _write(tmp_path, "src/services/orderService.ts", SERVICE.replace("return svc;", "return svc; // kept"))
    assert index.build(tmp_path, changed=[tmp_path / "src/services/orderService.ts"])["ok"]
    g3 = json.loads(graph_path(tmp_path).read_text(encoding="utf-8"))
    by3 = {n["id"]: n for n in g3["nodes"]}
    assert by3[base]["label"] == "OrderService" and by3[const["id"]]["label"] == "orderService"
    assert any(e["source"] == "src_routes_orders" and e["target"] == const["id"] for e in g3["links"])


def _svc_nodes(src: str) -> list[dict]:
    return [{"id": "f_x", "label": "X", "source_file": src, "source_location": "L3"},
            {"id": "f_x", "label": "x", "source_file": src, "source_location": "L9"}]


def test_edges_follow_the_line_they_were_read_from(tmp_path):
    _write(tmp_path, "f.ts", "import a from 'a';\n\nclass X {\n  m() {}\n}\n\nfunction use() { return new X(); }\n"
                             "\nexport const x = new X();\nfoo(x);\n")
    nodes = _svc_nodes("f.ts")
    edges = [{"source": "f", "target": "f_x", "relation": "contains", "source_file": "f.ts", "source_location": "L3"},
             {"source": "f", "target": "f_x", "relation": "contains", "source_file": "f.ts", "source_location": "L9"},
             {"source": "f_x", "target": "f_x_m", "relation": "method", "source_file": "f.ts", "source_location": "L4"},
             {"source": "f_use", "target": "f_x", "relation": "calls", "source_file": "f.ts", "source_location": "L7"},
             {"source": "f_x", "target": "f_foo", "relation": "calls", "source_file": "f.ts", "source_location": "L10"},
             {"source": "g", "target": "f_x", "relation": "imports", "source_file": "g.ts", "source_location": "L1"}]
    _write(tmp_path, "g.ts", "import { x } from './f';\n")
    split = split_case_collisions(nodes, edges, tmp_path)
    new = f"f_x_{_h('x')}"
    assert split == {"f_x": [new]} and [n["id"] for n in nodes] == ["f_x", new]
    assert [(e["source"], e["target"]) for e in edges] == [
        ("f", "f_x"), ("f", new),      # contains: the member defined on that line
        ("f_x", "f_x_m"),              # a method edge inside the class: the class
        ("f_use", "f_x"),              # `new X()`: the class
        (new, "f_foo"),                # from line 10, after the const's definition: the const
        ("g", new)]                    # `import { x }`: the const
    before = [dict(e) for e in edges]
    assert split_case_collisions(nodes, edges, tmp_path) == {} and edges == before   # a second pass: no change
    # an update keeps the split nodes of f.ts and re-reads g.ts, whose new edge ends at the kept id
    again = [{"source": "g", "target": "f_x", "relation": "imports", "source_file": "g.ts", "source_location": "L1"}]
    assert split_case_collisions(nodes, again, tmp_path) == {} and again[0]["target"] == new


def test_only_case_variants_in_one_file_of_a_case_sensitive_language_are_split(tmp_path):
    same = [{"id": "f_x", "label": "X", "source_file": "f.ts", "source_location": "L1"},
            {"id": "f_x", "label": "X", "source_file": "f.ts", "source_location": "L1"}]
    assert split_case_collisions(same, [], tmp_path) == {}   # the same symbol twice
    two_files = [{"id": "f_x", "label": "X", "source_file": "a.ts", "source_location": "L1"},
                 {"id": "f_x", "label": "x", "source_file": "b.ts", "source_location": "L1"}]
    assert split_case_collisions(two_files, [], tmp_path) == {}   # the upstream pass keeps files apart
    for suffix in (".sql", ".pas", ".php", ".f90", ".cls"):   # one symbol in these languages
        nodes = _svc_nodes("f" + suffix)
        assert split_case_collisions(nodes, [], tmp_path) == {} and {n["id"] for n in nodes} == {"f_x"}
    member = [{"id": "c_m", "label": ".placeOrder()", "source_file": "c.py", "source_location": "L2"},
              {"id": "c_m", "label": ".placeorder()", "source_file": "c.py", "source_location": "L5"}]
    assert split_case_collisions(member, [], tmp_path) == {"c_m": [f"c_m_{_h('placeorder')}"]}


def test_the_name_that_sorts_first_keeps_the_id_wherever_it_is_defined(tmp_path):
    for lines in (("L9", "L2"), ("L2", "L9")):
        nodes = [{"id": "f_x", "label": "x", "source_file": "f.ts", "source_location": lines[0]},
                 {"id": "f_x", "label": "X", "source_file": "f.ts", "source_location": lines[1]}]
        split_case_collisions(nodes, [], tmp_path)
        assert [(n["label"], n["id"]) for n in nodes] == [("x", f"f_x_{_h('x')}"), ("X", "f_x")]
    # the same symbol kept from the last graph under its split id: the new node joins it
    kept = [{"id": "f_x", "label": "X", "source_file": "f.ts", "source_location": "L1"},
            {"id": "f_x", "label": "x", "source_file": "f.ts", "source_location": "L2"},
            {"id": f"f_x_{_h('x')}", "label": "x", "source_file": "f.ts", "source_location": "L2"}]
    split_case_collisions(kept, [], tmp_path)
    assert [n["id"] for n in kept] == ["f_x", f"f_x_{_h('x')}", f"f_x_{_h('x')}"]
    taken = [{"id": "f_x", "label": "X", "source_file": "f.ts", "source_location": "L1"},
             {"id": "f_x", "label": "x", "source_file": "f.ts", "source_location": "L2"},
             {"id": f"f_x_{_h('x')}", "label": "other", "source_file": "f.ts", "source_location": "L3"}]
    split_case_collisions(taken, [], tmp_path)
    assert taken[1]["id"] == "f_x_" + hashlib.sha1(b"x").hexdigest()[:10]   # never an id another node has
