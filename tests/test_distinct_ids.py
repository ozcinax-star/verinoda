"""Two definitions whose names mint one id (``request`` / ``_request``, ``getX`` / ``getx``) stay two nodes,
calls resolve to the exact name, and a name matched only case-insensitively is never an exact match."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

from verinoda import index, naming, retrieval  # noqa: E402
from verinoda.case_ids import split_case_collisions  # noqa: E402
from verinoda.paths import graph_path  # noqa: E402
from verinoda.project_index.extractors.engine import _distinct_def_id  # noqa: E402

# the shape of axios lib/core/Axios.js: a public method and its private twin
AXIOS = """\
import { mergeConfig } from './mergeConfig';

class Axios {
  request(config) {
    return this._request(config);
  }

  _request(config) {
    const merged = mergeConfig(config);
    return merged;
  }
}

export function sendfile(res) {
  return res;
}

export function sendFileHelper(res) {
  return sendfile(res);
}

export function getX() {
  return 1;
}

export function getx() {
  return 2;
}

export default Axios;
"""
MERGE = """\
export function mergeConfig(config) {
  return config;
}
"""
USE = """\
import { getx } from './Axios';
import { _helper } from './helpers';

export function useIt() {
  return getx() + _helper();
}
"""
HELPERS = """\
export function helper() {
  return 1;
}

export function _helper() {
  return helper();
}
"""
# Python: methods (the upstream rule: the public name keeps the id) and nested functions (the new one)
SVC = """\
def helper(x):
    return x


class Service:
    def fetch(self, x):
        return self._fetch(x)

    def _fetch(self, x):
        return helper(x)


def outer():
    def step():
        return 1

    def _step():
        return step()

    return _step()


def _load():
    return helper(1)


def load():
    return 2
"""
CALLER = """\
from pkg.svc import _load, helper


def run():
    return _load()


def _run():
    return helper(2)
"""


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


def _h(name: str, n: int = 6) -> str:
    return hashlib.sha1(name.encode("utf-8")).hexdigest()[:n]


def _graph(root: Path) -> tuple[dict, set]:
    g = json.loads(graph_path(root).read_text(encoding="utf-8"))
    by_id = {n["id"]: (n["label"], n.get("source_location")) for n in g["nodes"]}
    edges = {(e["source"], e["relation"], e["target"], e.get("source_location")) for e in g["links"]}
    return by_id, edges


def test_javascript_definitions_that_mint_one_id_are_two_nodes_with_their_own_calls(tmp_path):
    _write(tmp_path, "lib/core/Axios.js", AXIOS)
    _write(tmp_path, "lib/core/mergeConfig.js", MERGE)
    _write(tmp_path, "lib/core/use.js", USE)
    _write(tmp_path, "lib/core/helpers.js", HELPERS)
    assert index.build(tmp_path, force=True)["ok"]
    by_id, edges = _graph(tmp_path)
    req = "lib_core_axios_axios_request"
    priv = f"{req}_{_h('_request')}"
    # before: one node `.request()` at line 4, and the call at line 9 read as `request`'s
    assert by_id[req] == (".request()", "L4")   # the definition seen first keeps the id it always had
    assert by_id[priv] == ("._request()", "L8")
    assert (req, "calls", priv, "L5") in edges
    assert (priv, "calls", "lib_core_mergeconfig_mergeconfig", "L9") in edges
    assert not any(s == req and t == "lib_core_mergeconfig_mergeconfig" for s, _r, t, _l in edges)
    assert {("lib_core_axios_axios", "method", req, "L4"), ("lib_core_axios_axios", "method", priv, "L8")} <= edges
    # case: two top-level functions
    getx = f"lib_core_axios_getx_{_h('getx')}"
    assert by_id["lib_core_axios_getx"] == ("getX()", "L22") and by_id[getx] == ("getx()", "L26")
    # an importer binds the name it wrote, never its twin
    assert ("lib_core_use_useit", "calls", getx, "L5") in edges
    assert not any(s == "lib_core_use_useit" and t == "lib_core_axios_getx" for s, _r, t, _l in edges)
    helper_priv = f"lib_core_helpers_helper_{_h('_helper')}"
    assert ("lib_core_use_useit", "calls", helper_priv, "L5") in edges
    assert ("lib_core_use", "imports", helper_priv, "L2") in edges
    assert not any(s == "lib_core_use" and t == "lib_core_helpers_helper" for s, _r, t, _l in edges)
    assert (helper_priv, "calls", "lib_core_helpers_helper", "L6") in edges
    # the names resolve exactly, and a trace runs through the private method
    g = index.load(tmp_path)
    assert naming.resolve(g, "_request").node == priv and naming.resolve(g, "_request").exact
    assert naming.resolve(g, "Axios.request").node == req
    t = retrieval.trace(g, "request", "mergeConfig")
    assert t["status"] == "found" and [h["to_id"] for h in t["paths"][0]] == [priv, "lib_core_mergeconfig_mergeconfig"]


def test_python_methods_and_nested_functions_that_mint_one_id_are_two_nodes(tmp_path):
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/svc.py", SVC)
    _write(tmp_path, "pkg/caller.py", CALLER)
    assert index.build(tmp_path, force=True)["ok"]
    by_id, edges = _graph(tmp_path)
    fetch, priv = "pkg_svc_service_fetch", f"pkg_svc_service_fetch_{_h('_fetch')}"
    assert by_id[fetch] == (".fetch()", "L6") and by_id[priv] == ("._fetch()", "L9")
    assert (fetch, "calls", priv, "L7") in edges and (priv, "calls", "pkg_svc_helper", "L10") in edges
    # nested functions: the upstream pre-scan stops at module and class level; the extractor now keeps them apart
    step, step_priv = "pkg_svc_outer_step", f"pkg_svc_outer_step_{_h('_step')}"
    assert by_id[step] == ("step()", "L14") and by_id[step_priv] == ("_step()", "L17")
    assert (step_priv, "calls", step, "L18") in edges
    assert ("pkg_svc_outer", "contains", step_priv, "L17") in edges
    # module level, across files: the import, the call and the calls made in the private twins' bodies
    load, load_priv = "pkg_svc_load", f"pkg_svc_load_{_h('_load')}"
    assert by_id[load] == ("load()", "L27") and by_id[load_priv] == ("_load()", "L23")   # the public name keeps it
    assert ("pkg_caller", "imports", load_priv, "L1") in edges and ("pkg_caller_run", "calls", load_priv, "L5") in edges
    assert not any(s == "pkg_caller_run" and t == load for s, _r, t, _l in edges)
    assert (load_priv, "calls", "pkg_svc_helper", "L24") in edges
    run_priv = f"pkg_caller_run_{_h('_run')}"
    assert (run_priv, "calls", "pkg_svc_helper", "L9") in edges
    assert not any(s == "pkg_caller_run" and t == "pkg_svc_helper" for s, _r, t, _l in edges)


def test_the_extractor_salts_only_a_different_name():
    held: dict[str, str] = {"c_request": "request"}
    assert _distinct_def_id("c_request", "request", held, False) == "c_request"   # the same name: one symbol
    assert _distinct_def_id("c_request", "_request", held, False) == f"c_request_{_h('_request')}"
    assert _distinct_def_id("c_request", "Request", held, False) == f"c_request_{_h('Request')}"
    assert _distinct_def_id("c_request", "Request", held, True) == "c_request"     # PHP folds case
    assert _distinct_def_id("c_request", "_request", held, True) == f"c_request_{_h('_request')}"
    assert _distinct_def_id("c_new", "_new", held, False) == "c_new"               # an id nobody holds
    # the salted id taken by a third name: a longer one
    held[f"c_request_{_h('_request')}"] = "other"
    assert _distinct_def_id("c_request", "_request", held, False) == f"c_request_{_h('_request', 10)}"
    # a qualified label names its last part (a C++ out-of-class definition of an in-class declaration)
    assert _distinct_def_id("c_bar", "Foo::bar", {"c_bar": "bar"}, False) == "c_bar"


def test_the_build_net_splits_names_that_differ_in_underscores_and_keeps_the_public_id(tmp_path):
    for lines in (("L2", "L9"), ("L9", "L2")):
        nodes = [{"id": "f_request", "label": "._request()", "source_file": "f.js", "source_location": lines[0]},
                 {"id": "f_request", "label": ".request()", "source_file": "f.js", "source_location": lines[1]}]
        assert split_case_collisions(nodes, [], tmp_path) == {"f_request": [f"f_request_{_h('_request')}"]}
        assert [(n["label"], n["id"]) for n in nodes] == [("._request()", f"f_request_{_h('_request')}"),
                                                          (".request()", "f_request")]
    # in PHP case is folded, underscores are not
    php = [{"id": "f_x", "label": "Foo", "source_file": "f.php", "source_location": "L1"},
           {"id": "f_x", "label": "foo", "source_file": "f.php", "source_location": "L5"}]
    assert split_case_collisions(php, [], tmp_path) == {}
    php = [{"id": "f_foo", "label": "foo()", "source_file": "f.php", "source_location": "L1"},
           {"id": "f_foo", "label": "_foo()", "source_file": "f.php", "source_location": "L5"},
           {"id": "f_foo", "label": "FOO()", "source_file": "f.php", "source_location": "L9"}]
    assert split_case_collisions(php, [], tmp_path) == {"f_foo": [f"f_foo_{_h('_foo')}"]}
    assert [n["id"] for n in php] == ["f_foo", f"f_foo_{_h('_foo')}", "f_foo"]
    # names that share an id by another route than minting are left alone
    other = [{"id": "f_a_b", "label": "b", "source_file": "f.js", "source_location": "L1"},
             {"id": "f_a_b", "label": "a_b", "source_file": "f.js", "source_location": "L5"}]
    assert split_case_collisions(other, [], tmp_path) == {}
    other = [{"id": "f_foo_bar", "label": "Foo bar", "source_file": "f.md", "source_location": "L1"},
             {"id": "f_foo_bar", "label": "Foo-bar", "source_file": "f.md", "source_location": "L5"}]
    assert split_case_collisions(other, [], tmp_path) == {}
    # an import edge minted with the plain id moves to the name its line writes
    _write(tmp_path, "g.js", "import { _helper } from './f';\n")
    nodes = [{"id": "f_helper", "label": "helper()", "source_file": "f.js", "source_location": "L1"},
             {"id": f"f_helper_{_h('_helper')}", "label": "_helper()", "source_file": "f.js", "source_location": "L5"}]
    edges = [{"source": "g", "target": "f_helper", "relation": "imports", "source_file": "g.js",
              "source_location": "L1"}]
    split_case_collisions(nodes, edges, tmp_path)
    assert edges[0]["target"] == f"f_helper_{_h('_helper')}"


def test_a_code_name_matched_only_case_insensitively_is_labelled_and_never_exact(tmp_path):
    # the shape of express lib/response.js: `res.sendFile = function sendFile` is no node, `function sendfile` is
    _write(tmp_path, "lib/response.js", AXIOS)
    _write(tmp_path, "lib/core/mergeConfig.js", MERGE)
    assert index.build(tmp_path, force=True)["ok"]
    g = index.load(tmp_path)
    r = naming.resolve(g, "lib/response.js::sendFile")
    assert r.status == naming.SIMILAR and r.node == "lib_response_sendfile" and not r.exact
    assert "only case-insensitively" in r.note and "`sendFile`" in r.note
    assert naming.resolve(g, "lib/response.js::sendfile").exact
    assert naming.resolve(g, "sendfile").exact
    t = retrieval.trace(g, "lib/response.js::sendFile", "lib/response.js::sendfile")
    assert t["status"] == "ambiguous: both endpoints resolved to the same node"
    assert "only case-insensitively" in t["fuzzy"]["source"] and "target" not in t.get("fuzzy", {})
    # both spellings defined: each one names its own node
    assert naming.resolve(g, "getX").node == "lib_response_getx"
    assert naming.resolve(g, "getx").node == f"lib_response_getx_{_h('getx')}"
    assert naming.resolve(g, "getx").exact and naming.resolve(g, "getX").exact
