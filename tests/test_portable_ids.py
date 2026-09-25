"""Graph ids do not carry the scan root (the machine's path, user name included): verinoda.portable_ids."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

from verinoda import index, workflow
from verinoda.paths import graph_path, receiver_calls_path
from verinoda.portable_ids import make_graph_portable, strip_root_from_ids
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


def test_one_rewrite_after_the_build_leaves_the_bytes_of_the_two_it_replaces(tmp_path):
    """index._post_process is make_graph_portable followed by prune_missing_files, with one read
    and at most one write: same bytes and same pruned files, whichever of the two had work."""
    root = tmp_path / "home" / "me" / "proj"
    (root / ".verinoda" / "index").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "src" / "a.py").write_bytes(b"x = 1\n")
    slug = make_id(str(root.resolve()))
    gp = graph_path(root)
    for strip in (False, True):
        for prune in (False, True):
            nodes = [{"id": "src_a", "label": "a.py", "source_file": "src/a.py"},
                     {"id": "src_a_f", "label": "fé()", "source_file": "src/a.py"}]
            links = [{"source": "src_a", "target": "src_a_f", "relation": "contains",
                      "source_file": "src/a.py"}]
            if strip:
                nodes.append({"id": f"{slug}_missing_mod", "label": f"{slug}_missing_mod"})
                links.append({"source": "src_a", "target": f"{slug}_missing_mod",
                              "relation": "imports", "source_file": "src/a.py"})
            if prune:
                nodes.append({"id": "src_gone", "label": "gone.py", "source_file": "src/gone.py"})
                links.append({"source": "src_a", "target": "src_gone", "relation": "imports",
                              "source_file": "src/a.py"})
            hyper = [{"id": "h", "nodes": ["src_a", "src_gone"],
                      "source_file": "src/gone.py"}] * prune
            text = json.dumps({"directed": False, "multigraph": False, "graph": {}, "nodes": nodes,
                               "links": links, "hyperedges": hyper, "built_at_commit": "abc"},
                              indent=2)
            gp.write_text(text, encoding="utf-8")
            make_graph_portable(gp, root)
            want_pruned = index.prune_missing_files(root)
            want = gp.read_bytes()
            gp.write_text(text, encoding="utf-8")
            changed, pruned, data = index._post_process(gp, root, prune=True)
            assert gp.read_bytes() == want and pruned == want_pruned == ["src/gone.py"] * prune
            assert changed == {"changed": 2 if strip else 0}
            assert data == json.loads(want)
            gp.write_text(text, encoding="utf-8")  # without the prune: make_graph_portable alone
            make_graph_portable(gp, root)
            want = gp.read_bytes()
            gp.write_text(text, encoding="utf-8")
            assert index._post_process(gp, root, prune=False)[1] is None and gp.read_bytes() == want


def _dangling_project(root: Path) -> Path:
    (root / "src" / "App").mkdir(parents=True)
    shutil.copy(FIXTURES / "cjs_require.js", root / "src" / "app.js")  # ids of missing imports
    shutil.copy(FIXTURES / "sample.dmf", root / "src" / "ui.dmf")
    # it names ../Domain/Domain.csproj and ../Infrastructure/Infrastructure.csproj (not here)
    shutil.copy(FIXTURES / "sample.csproj", root / "src" / "App" / "App.csproj")
    return root


def test_scan_and_update_leave_what_the_two_separate_rewrites_left(tmp_path, monkeypatch):
    """The build prunes the nodes of missing files in the rewrite that makes the ids portable;
    graph.json, the counts, the reported files and the receiver sidecar are what the build
    followed by workflow's prune gave."""
    real_build = index.build
    out = {}
    for name in ("one_rewrite", "two_rewrites"):
        root = _dangling_project(tmp_path / name / "proj")
        if name == "two_rewrites":  # the build only makes the ids portable, the prune follows
            monkeypatch.setattr(index, "build",
                                lambda r, **kw: real_build(r, **{**kw, "prune_missing": False}))
        workflow.init(root)
        st = open_store(root)
        try:
            scan = workflow.scan(st, root)
            app = root / "src" / "app.js"
            app.write_bytes(app.read_bytes() + b"\n// edited\n")
            upd = workflow.update(st, root)
        finally:
            st.close()
        side = json.loads(receiver_calls_path(root).read_text(encoding="utf-8"))
        side["graph"].pop("mtime_ns")
        out[name] = (graph_path(root).read_bytes(), scan["graph"]["nodes"], scan["graph"]["edges"],
                     scan.get("dropped_dangling_references"), upd["snapshot"]["graph_nodes"],
                     upd["snapshot"]["graph_edges"], upd.get("dropped_dangling_references"), side)
    assert out["one_rewrite"] == out["two_rewrites"]
    assert out["one_rewrite"][3]["files"] == ["src/Domain/Domain.csproj",
                                               "src/Infrastructure/Infrastructure.csproj"]
    data = json.loads(out["one_rewrite"][0])
    assert out["one_rewrite"][4:6] == (len(data["nodes"]), len(data["links"]))
