"""Graph ids do not carry the scan root (the machine's path, user name included): verinoda.portable_ids."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

from verinoda import workflow
from verinoda.paths import graph_path
from verinoda.portable_ids import strip_root_from_ids
from verinoda.project_index.ids import make_id
from verinoda.store import open_store

FIXTURES = Path(__file__).resolve().parents[1] / "tests_upstream" / "fixtures"


def test_ids_of_missing_imports_and_dmf_elements_are_repo_relative_after_a_scan(tmp_path):
    root = tmp_path / "home" / "Çınar" / "proj"
    (root / "src").mkdir(parents=True)
    shutil.copy(FIXTURES / "cjs_require.js", root / "src" / "app.js")  # its ./foundation, ./helpers do not exist
    shutil.copy(FIXTURES / "sample.dmf", root / "src" / "ui.dmf")
    workflow.init(root)
    st = open_store(root)
    try:
        workflow.scan(st, root)
        workflow.update(st, root)  # an update merges the graph it finds: nothing comes back
    finally:
        st.close()
    g = json.loads(graph_path(root).read_text(encoding="utf-8"))
    ids = [n["id"] for n in g["nodes"]] + [e[k] for e in g["links"] for k in ("source", "target")]
    slug = make_id(str(root.resolve()))
    assert ids and not [i for i in ids if slug in i]
    assert not [i for i in ids if "ınar" in i or "inar" in i]  # no part of the user's folder either
    labels = [n.get(k) or "" for n in g["nodes"] for k in ("label", "norm_label")]
    assert not [x for x in labels if slug in x or "inar" in x]  # nor in the names shown for them
    targets = {e["target"] for e in g["links"]}
    assert "src_foundation" in targets and "src_foundation_loadfoundation" in targets
    assert any(i.startswith("src_ui_elem_") and i.endswith("_mapwindow_map") for i in ids)


def test_only_ids_minted_from_the_root_are_rewritten():
    root = Path("/home/me/proj")
    slug = make_id(str(root))
    nodes = [{"id": f"{slug}_lib_x"}, {"id": f"a_elem_{slug}_ui_window"}, {"id": f"x{slug}_y"},
             {"id": "home_me_projector"}, {"id": slug}, {"id": f"pkg_{slug}_mod"}]
    edges = [{"source": f"{slug}_lib_x", "target": f"{slug}_lib_y"}]
    hyper = [{"nodes": [f"{slug}_lib_x", "other"]}]
    assert strip_root_from_ids(nodes, edges, root, hyper) == 4  # two nodes, two edge ends
    assert [n["id"] for n in nodes] == ["lib_x", "a_elem_ui_window", f"x{slug}_y", "home_me_projector", slug,
                                        f"pkg_{slug}_mod"]  # the root only where an absolute path mints it
    assert edges == [{"source": "lib_x", "target": "lib_y"}] and hyper[0]["nodes"] == ["lib_x", "other"]


def test_the_label_of_a_rewritten_id_loses_the_root_too():
    root = Path("/home/me/proj")
    slug = make_id(str(root))
    nodes = [{"id": f"{slug}_lib_x", "label": f"{slug}_lib_x", "norm_label": f"{slug}_lib_x"},
             {"id": f"a_elem_{slug}_ui_window", "label": "window"}, {"id": "keep", "label": f"{slug}_keep"}]
    strip_root_from_ids(nodes, [], root)
    assert nodes == [{"id": "lib_x", "label": "lib_x", "norm_label": "lib_x"},
                     {"id": "a_elem_ui_window", "label": "window"},
                     {"id": "keep", "label": f"{slug}_keep"}]  # only the nodes whose ids were rewritten


def test_real_nodes_and_one_folder_roots_are_left_alone_and_collisions_kept_apart():
    root = Path("/home/me/proj")
    slug = make_id(str(root))
    # a real node (it has a source file) whose id happens to start like the root is not touched
    nodes = [{"id": f"{slug}_real", "source_file": "x.py"}, {"id": "secrets"}]
    edges = [{"source": "app", "target": f"{slug}_secrets"}, {"source": "app", "target": "secrets"}]
    assert strip_root_from_ids(nodes, edges, root) == 1
    assert nodes[0]["id"] == f"{slug}_real"
    assert edges[0]["target"] == "secrets_unresolved"  # a missing ./secrets is not the secrets module
    for one in (Path("/web_app"), Path("C:/code") if os.name == "nt" else Path("/code")):
        ids = [{"id": make_id(str(one)) + "_pkg_mod"}]
        assert strip_root_from_ids(ids, [], one) == 0
