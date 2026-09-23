"""Architecture map views on a scanned copy of examples/orders_app."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import json  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import architecture_map as am  # noqa: E402
from verinoda import index, workflow  # noqa: E402
from verinoda.store import open_store  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "orders_app"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args],
                   cwd=cwd, check=True, capture_output=True)


def _copy_example(dst: Path) -> Path:
    shutil.copytree(EXAMPLE, dst, ignore=shutil.ignore_patterns(".verinoda", "__pycache__", "*.pyc",
                                                                ".pytest_cache", "*.db"))
    _git(dst, "init", "-q")
    _git(dst, "add", "-A")
    _git(dst, "commit", "-q", "-m", "init")
    return dst


@pytest.fixture(scope="module")
def g(tmp_path_factory):
    repo = _copy_example(tmp_path_factory.mktemp("map") / "orders_app")
    workflow.init(repo)
    st = open_store(repo)
    workflow.scan(st, repo)
    st.close()
    return index.load(repo)


@pytest.fixture(scope="module")
def full(g):
    return am.build_map(g)


def test_every_view_states_method_and_limits(g, full):
    assert set(full) == set(am.VIEWS) == {"hierarchy", "dependencies", "dataflow", "config", "tests", "history"}
    views = dict(full, impact=am.impact(g, ["orders/pricing.py"]))
    for name, v in views.items():
        assert v["view"] == name
        assert isinstance(v["coverage"]["method"], str) and v["coverage"]["method"]
        assert isinstance(v["coverage"]["limits"], list) and v["coverage"]["limits"], name
    json.dumps(views)  # structured and serialisable


def test_hierarchy(full):
    h = full["hierarchy"]
    assert {"orders", "tests", "docs", "(root)"} <= set(h["subsystems"])
    pricing = h["subsystems"]["orders"]["packages"]["orders"]["orders/pricing.py"]
    assert pricing["kind"] == "code" and any(t.startswith("compute_total()@L6") for t in pricing["top"])
    assert h["subsystems"]["tests"]["packages"]["tests"]["tests/test_pricing.py"]["kind"] == "test"


def test_dependencies(full):
    d = full["dependencies"]
    pairs = {(r["from"], r["to"]) for r in d["internal"]}
    assert ("orders/api.py", "orders/service.py") in pairs and ("orders/service.py", "orders/pricing.py") in pairs
    assert "sqlite3" in d["external_imports"]
    uses = next(r for r in d["internal"] if (r["from"], r["to"], r["relation"]) ==
                ("orders/service.py", "orders/repository.py", "uses"))
    assert set(uses["confidence"]) == {"INFERRED"}  # confidence carried, not upgraded


def test_dataflow_handler_to_save_with_labelled_inferred_hop(g, full):
    df = full["dataflow"]
    entries = {e["symbol"]: e for e in df["entries"]}
    assert "create_order_handler()" in entries and entries["create_order_handler()"]["at"] == "orders/api.py:16"
    assert all(e["why"] for e in df["entries"])
    path = next(p for p in df["paths"] if [h["to"] for h in p["hops"]] == ["place_order()", ".save()"])
    assert path["entry"] == "orders/api.py:16" and path["sink"] == "orders/repository.py:15"
    first, save = path["hops"]
    assert (first["from"], first["relation"], first["confidence"], first["at"]) == \
        ("create_order_handler()", "calls", "EXTRACTED", "orders/api.py:18")
    assert (save["relation"], save["confidence"], save["derived_by"], save["at"]) == \
        ("calls", "INFERRED", index.RECEIVER_ORIGIN, "orders/service.py:22")
    assert "sql-write" in path["sink_kinds"] and "orders/repository.py:17" in path["sink_lines"]
    assert any("heuristic" in lim for lim in df["coverage"]["limits"])


def test_dataflow_never_steps_through_containment(full):
    for p in full["dataflow"]["paths"]:
        for h in p["hops"]:
            assert h["relation"] in ("calls", "method"), h
            if h["relation"] == "method":  # the only allowed class->method step: construction runs __init__
                assert h["to"] == ".__init__()", h
            assert h["relation"] != "contains"
            assert h["at"], h  # every hop has a locator


def test_config_finds_the_three_env_vars(full):
    c = full["config"]
    env = c["env_vars"]
    assert {"ORDERS_DATABASE_URL", "ORDERS_MAX_ITEMS", "ORDERS_DISCOUNT_THRESHOLD"} <= set(env)
    assert env["ORDERS_DATABASE_URL"][0]["at"] == "orders/config.py:5"
    assert env["ORDERS_MAX_ITEMS"][0]["at"] == "orders/config.py:6"
    assert env["ORDERS_DISCOUNT_THRESHOLD"][0]["at"] == "orders/config.py:7"
    assert "orders/config.py" in c["config_files"] and "pyproject.toml" in c["config_files"]
    assert {"orders/pricing.py", "orders/service.py", "orders/repository.py"} <= set(
        c["config_importers"]["orders/config.py"])


def test_tests_view_maps_pricing_functions_to_their_tests(full):
    t = full["tests"]
    assert t["tests"] == 5
    cov = t["covered"]
    ct = cov["compute_total() (orders/pricing.py:6)"]
    ad = cov["apply_discount() (orders/pricing.py:11)"]
    assert "test_compute_total()" in ct
    assert {"test_discount_applies_above_threshold()", "test_no_discount_below_threshold()"} <= set(ad)
    assert any("load_settings()" in s for s in t["not_reached_by_tests"])
    assert any("runtime coverage" in lim for lim in t["coverage"]["limits"])


def test_history_finds_the_adr_and_its_status(full):
    h = full["history"]
    assert h["is_git"] is True and h["recent_commits"][0]["subject"] == "init"
    adr = next(d for d in h["decisions"] if d["doc"] == "docs/adr/0001-sqlite-persistence.md")
    assert adr["status"] == "accepted" and "OrderRepository" in adr["mentions_symbols"]
    assert any(c["subject"] == "init" for c in h["decision_commits"])  # the commit touched docs/adr/


def test_impact_of_pricing_lists_service_api_and_both_test_files(g):
    imp = am.impact(g, ["orders/pricing.py"])
    assert {"orders/service.py", "orders/api.py", "tests/test_pricing.py", "tests/test_service.py"} <= set(
        imp["affected_files"])
    assert "orders/pricing.py" not in imp["affected_files"]
    assert imp["tests_to_run"] == ["tests/test_pricing.py", "tests/test_service.py"]
    assert imp["unresolved"] == []
    dist = {s["symbol"]: s["distance"] for s in imp["affected_symbols"]}
    assert dist["place_order()"] == 1 and dist["create_order_handler()"] == 2
    assert "possibly affected" in " ".join(imp["coverage"]["limits"])


def test_impact_of_a_symbol_and_of_unknown_targets(g):
    imp = am.impact(g, ["orders/service.py::place_order", "definitely_not_a_symbol_xyz"])
    assert "orders/api.py" in imp["affected_files"] and "tests/test_service.py" in imp["tests_to_run"]
    assert imp["unresolved"] == ["definitely_not_a_symbol_xyz"]


def test_history_without_git(tmp_path):
    repo = tmp_path / "nogit"
    (repo / "docs" / "decisions").mkdir(parents=True)
    (repo / "app.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    (repo / "docs" / "decisions" / "d1.md").write_text("# Use run\n\nStatus: proposed\n\nWe call `run`.\n",
                                                       encoding="utf-8")
    workflow.init(repo)
    st = open_store(repo)
    workflow.scan(st, repo)
    st.close()
    h = am.history(index.load(repo))
    assert h["is_git"] is False and h["recent_commits"] == []
    assert any("no git repository" in lim for lim in h["coverage"]["limits"])
    assert h["decisions"][0]["status"] == "proposed"


def test_symbol_at_matches_brute_force_innermost_span(g):
    """_symbol_at now uses the graph's per-file index; it must agree with a scan of every span."""
    for f in am._files(g):
        n_lines = len(am._read(g.root, f))
        spans = [(g.span(n), n) for n in g.symbols_in(f) if g.span(n)]
        for line in range(1, n_lines + 2):
            containing = [(sp, n) for sp, n in spans if sp[0] <= line <= sp[1]]
            want = max(containing, key=lambda x: (x[0][0], -x[0][1]))[1] if containing else None
            assert am._symbol_at(g, f, line) == want, (f, line)


def test_read_uses_the_line_cache_but_sees_edits(tmp_path):
    f = tmp_path / "m.py"
    f.write_bytes(b"a = 1\nb = 2\n")
    first = am._read(tmp_path, "m.py")
    assert first == ["a = 1", "b = 2"] and am._read(tmp_path, "m.py") == first
    first.append("mutating the returned list does not touch the cache")
    assert am._read(tmp_path, "m.py") == ["a = 1", "b = 2"]
    f.write_bytes(b"a = 1\nb = 2\nc = 3\n")  # size changes: a new cache key
    assert am._read(tmp_path, "m.py")[-1] == "c = 3"
    assert am._read(tmp_path, "missing.py") == []
