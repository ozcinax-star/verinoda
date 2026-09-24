"""Folders holding a copy of the project's own code are found and ranked lower (verinoda.copies)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

from verinoda import copies, index, workflow
from verinoda.paths import atlas_dir
from verinoda.store import open_store

MODULES = ["billing", "ledger", "invoice", "tax", "refund", "report", "export_csv", "currency", "discount"]


def _module(name: str, extra: str = "") -> str:
    return (f"def {name}_open(x):\n    return x + 1\n\n\ndef {name}_close(x):\n    return x - 1\n\n\n"
            f"def {name}_total(items):\n    return sum(items)\n{extra}")


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    root = tmp_path_factory.mktemp("copies") / "shop"
    for m in MODULES:
        _write(root, f"app/{m}.py", _module(m))
        _write(root, f"tests/test_{m}.py", f"from app.{m} import {m}_open\n\n\ndef test_{m}_open():\n"
                                           f"    assert {m}_open(1) == 2\n\n\ndef test_{m}_again():\n    assert True\n")
        # an older copy of the project kept for benchmarks: the same files, one name renamed
        _write(root, f"bench/corpora/old/app/{m}.py", _module(m).replace(f"{m}_total", f"{m}_sum"))
        _write(root, f"bench/corpora/old/tests/test_{m}.py",
               f"from app.{m} import {m}_open\n\n\ndef test_{m}_open():\n    assert {m}_open(1) == 2\n\n\n"
               f"def test_{m}_again():\n    assert True\n")
    # a vendored library the product really uses: twins elsewhere do not make it a copy
    for m in ["util_a", "util_b", "util_c", "util_d", "util_e", "util_f", "util_g", "util_h"]:
        _write(root, f"vendor/lib/{m}.py", _module(m))
        _write(root, f"app/uses_{m}.py", f"from lib.{m} import {m}_open, {m}_close\n\n\n"
                                            f"def call_{m}():\n    return {m}_open(1) + {m}_close(2)\n\n\n"
                                            f"def more_{m}():\n    return {m}_open(3)\n")
    workflow.init(root)
    st = open_store(root)
    try:
        res = workflow.scan(st, root)
    finally:
        st.close()
    return root, res


def test_a_nested_copy_of_the_project_is_found(repo):
    root, res = repo
    found = copies.load(root)
    assert [c["path"] for c in found] == ["bench/corpora/old/"]  # the narrowest folder holding the copy
    assert set(found[0]["of"]) <= {"app/", "tests/"} and found[0]["outside_use"] <= copies.MAX_OUTSIDE_USE
    assert (res.get("derived") or {}).get("copies", {}).get("copies") == ["bench/corpora/old/"]


def test_the_real_code_and_a_used_library_are_not_copies(repo):
    root, _ = repo
    paths = [c["path"] for c in copies.detect(index.load(root))]
    assert not any(p.startswith(("app", "tests", "vendor")) for p in paths)


def test_search_ranks_the_copy_below_the_real_code_unless_named(repo):
    from verinoda import search_index

    root, _ = repo
    g = index.load(root)
    hits = search_index.rank(g, "billing open").hits
    files = [h.file for h in hits]
    real = files.index("app/billing.py")
    assert "bench/corpora/old/app/billing.py" in files and real < files.index("bench/corpora/old/app/billing.py")
    named = [h.file for h in search_index.rank(g, "billing open in the old copy").hits]
    assert named.index("bench/corpora/old/app/billing.py") < named.index("app/billing.py")  # "old" names it


def test_detection_can_be_turned_off_or_a_folder_kept(repo):
    root, _ = repo
    cfg_p = atlas_dir(root) / "config.json"
    before = cfg_p.read_text(encoding="utf-8") if cfg_p.exists() else None
    try:
        cfg = json.loads(before) if before else {}
        cfg.setdefault("index", {})["not_copies"] = ["bench"]
        cfg_p.write_text(json.dumps(cfg), encoding="utf-8")
        assert copies.load(root) == []
        cfg["index"] = {"detect_copies": False}
        cfg_p.write_text(json.dumps(cfg), encoding="utf-8")
        assert copies.load(root) == []
    finally:
        if before is None:
            cfg_p.unlink()
        else:
            cfg_p.write_text(before, encoding="utf-8")
    assert [c["path"] for c in copies.load(root)] == ["bench/corpora/old/"]


def test_twins_at_the_same_depth_decide_nothing(tmp_path):
    root = tmp_path / "two"
    for m in MODULES:
        _write(root, f"v1/{m}.py", _module(m))
        _write(root, f"v2/{m}.py", _module(m))
    workflow.init(root)
    st = open_store(root)
    try:
        workflow.scan(st, root)
    finally:
        st.close()
    assert copies.detect(index.load(root)) == []


def test_a_port_beside_its_original_with_nothing_else_using_either_is_left_alone(tmp_path):
    # the flattened original has the shorter path; neither side is used from outside: nothing is decided
    root = tmp_path / "port"
    for m in MODULES:
        _write(root, f"src/main/java/com/x/mod/{m}.py", _module(m))
        _write(root, f"reference/original/com/x/mod/{m}.py", _module(m))
    workflow.init(root)
    st = open_store(root)
    try:
        workflow.scan(st, root)
    finally:
        st.close()
    assert copies.detect(index.load(root)) == []


def test_tests_are_never_the_copy_of_a_copy_of_them(repo):
    root, _ = repo
    assert not any(c["path"].startswith("tests") for c in copies.detect(index.load(root)))
