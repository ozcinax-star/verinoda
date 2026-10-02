"""The update oracle: ``verinoda update`` after an edit gives the graph a fresh copy's scan gives
(``tools/graph_equal.py``, ``tools/update_vs_scan.py``)."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import graph_equal  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def _graph(nodes, links):
    return {"nodes": nodes, "links": links}


def test_graph_equal_ignores_history_and_order():
    a = _graph([{"id": "x", "label": "x", "community": 1}, {"id": "y", "label": "y"}],
               [{"source": "x", "target": "y", "relation": "calls"}])
    b = _graph([{"id": "y", "label": "y"}, {"id": "x", "label": "x", "community": 7}],
               [{"source": "x", "target": "y", "relation": "calls"}])
    assert graph_equal.compare(a, b)["equal"]


def test_graph_equal_names_what_differs():
    a = _graph([{"id": "x", "label": "x"}, {"id": "y", "label": "y"}],
               [{"source": "x", "target": "y", "relation": "calls"}])
    b = _graph([{"id": "x", "label": "x2"}, {"id": "z", "label": "z"}],
               [{"source": "x", "target": "y", "relation": "calls"}, {"source": "x", "target": "y", "relation": "calls"}])
    res = graph_equal.compare(a, b)
    assert not res["equal"]
    assert res["nodes_only_in_a"] == ["y"] and res["nodes_only_in_b"] == ["z"]
    assert res["nodes_changed"][0]["id"] == "x" and res["counts"]["edges_only_in_b"] == 1


def test_an_update_equals_a_fresh_scan_on_the_example(tmp_path):
    edit = "orders/pricing.py::\\n\\ndef added_helper(x):\\n    return compute_total(x)\\n"
    p = subprocess.run([sys.executable, "-P", str(ROOT / "tools" / "update_vs_scan.py"), "--corpus",
                        str(ROOT / "examples" / "orders_app"), "--edit", edit, "--work", str(tmp_path / "w")],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=ROOT)
    out = json.loads(p.stdout)
    assert p.returncode == 0 and out["equal"], out
    assert out["counts"] == {"nodes_only_in_a": 0, "nodes_only_in_b": 0, "nodes_changed": 0,
                             "edges_only_in_a": 0, "edges_only_in_b": 0}
