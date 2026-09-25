"""The kept Python facts give the symbol resolution pass exactly what the upstream walks give it."""

import json
from pathlib import Path

import pytest

import verinoda.python_facts as pf
from verinoda.project_index.extractors import resolution as res
from verinoda.project_index.extractors.models import _SymbolResolutionFacts
from verinoda.python_facts import FILE, python_facts_cache

FILES = {
    "pkg/__init__.py": "from .a import f as g\nfrom . import b\n",
    "pkg/a.py": "from .c import helper\n\ndef f():\n    return helper()\n",
    "pkg/b.py": "def b_fn():\n    pass\n",
    "pkg/c.py": "def helper():\n    return 1\n",
    "ns/brain.py": "def think():\n    return 2\n",
    "app.py": "from ns import brain\nfrom pkg.a import f\n\ndef run():\n    brain.think()\n    return f()\n",
    "main.py": "from pkg import a, b\nfrom pkg import g as alias\n\ndef main():\n    alias()\n    a.f()\n\nclass K:\n    def m(self):\n        main()\n",
    "broken.py": "def oops(:\n    from pkg import (\n",
    "notes.txt": "not python\n",
}


def _project(tmp_path: Path) -> tuple[Path, list[Path]]:
    root = tmp_path / "proj"
    for rel, text in FILES.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return root, sorted(p for p in root.rglob("*") if p.is_file())


def _facts(fn, paths, root) -> _SymbolResolutionFacts:
    facts = _SymbolResolutionFacts()
    fn(paths, root, facts)
    return facts


def test_cold_warm_and_edited_files_give_the_upstream_facts(tmp_path):
    root, paths = _project(tmp_path)
    real = res._collect_python_symbol_resolution_facts
    want = _facts(real, paths, root)
    assert want.imports and want.uses and want.module_imports  # the project exercises every kind

    with python_facts_cache(tmp_path / "index") as cold:
        assert _facts(res._collect_python_symbol_resolution_facts, paths, root) == want
    assert res._collect_python_symbol_resolution_facts is real  # put back after the build
    assert cold.misses == 8 and cold.hits == 0
    assert (tmp_path / "index" / FILE).is_file()

    with python_facts_cache(tmp_path / "index") as warm:
        assert _facts(res._collect_python_symbol_resolution_facts, paths, root) == want
    assert warm.hits == 8 and warm.misses == 0  # a file with a syntax error parses too (with error nodes)

    (root / "pkg" / "a.py").write_text("from .c import helper\nfrom .b import b_fn\n\ndef f():\n    b_fn()\n    return helper()\n",
                                       encoding="utf-8")
    res._parse_python_tree_cached.cache_clear()  # the upstream parse memo, as a new process would start
    want = _facts(real, paths, root)
    with python_facts_cache(tmp_path / "index") as edited:
        assert _facts(res._collect_python_symbol_resolution_facts, paths, root) == want
    assert edited.misses == 1 and edited.hits == 7


def test_a_new_file_changes_where_an_import_points_without_a_new_walk(tmp_path):
    root, _ = _project(tmp_path)
    (root / "late.py").write_text("from pkg import d\n", encoding="utf-8")  # pkg/d.py does not exist yet
    paths = sorted(p for p in root.rglob("*") if p.is_file())
    with python_facts_cache(tmp_path / "index"):
        before = _facts(res._collect_python_symbol_resolution_facts, paths, root)
    assert not any(sub.name == "d.py" for _, sub, _, _ in before.module_imports)
    (root / "pkg" / "d.py").write_text("X = 1\n", encoding="utf-8")
    paths = sorted(p for p in root.rglob("*") if p.is_file())
    want = _facts(res._collect_python_symbol_resolution_facts, paths, root)
    with python_facts_cache(tmp_path / "index") as again:
        got = _facts(res._collect_python_symbol_resolution_facts, paths, root)
    assert got == want
    assert again.misses == 1  # only the new file is walked; late.py's import now points at it
    assert any(src.name == "late.py" and sub.name == "d.py" for src, sub, _, _ in got.module_imports)


def test_a_cache_from_other_code_is_not_used(tmp_path):
    root, paths = _project(tmp_path)
    with python_facts_cache(tmp_path / "index"):
        _facts(res._collect_python_symbol_resolution_facts, paths, root)
    kept = tmp_path / "index" / FILE
    kept.write_text(kept.read_text(encoding="utf-8").replace('"stamp":"', '"stamp":"other-'), encoding="utf-8")
    with python_facts_cache(tmp_path / "index") as again:
        _facts(res._collect_python_symbol_resolution_facts, paths, root)
    assert again.hits == 0


@pytest.mark.parametrize("text", [
    lambda stamp: json.dumps({"stamp": stamp, "files": None}),
    lambda stamp: json.dumps({"stamp": stamp, "files": []}),
    lambda stamp: json.dumps({"stamp": stamp, "files": "x"}),
    lambda stamp: json.dumps([stamp]),
    lambda stamp: "[" * 100_000 + "]" * 100_000,  # json.loads raises RecursionError
], ids=["files-null", "files-list", "files-string", "not-an-object", "too-deep"])
def test_a_kept_file_of_another_shape_counts_as_empty(tmp_path, text):
    root, paths = _project(tmp_path)
    want = _facts(res._collect_python_symbol_resolution_facts, paths, root)
    with python_facts_cache(tmp_path / "index"):
        _facts(res._collect_python_symbol_resolution_facts, paths, root)
    kept = tmp_path / "index" / FILE
    kept.write_text(text(json.loads(kept.read_text(encoding="utf-8"))["stamp"]), encoding="utf-8")
    with python_facts_cache(tmp_path / "index") as again:
        assert _facts(res._collect_python_symbol_resolution_facts, paths, root) == want
    assert again.misses == 8 and again.hits == 0  # every file walked again
    assert isinstance(json.loads(kept.read_text(encoding="utf-8"))["files"], dict)  # and the file rewritten


def _refused(src, dst):  # os.replace on Windows while another process has the file open
    raise PermissionError(13, "The process cannot access the file", str(dst))


def test_a_save_that_fails_leaves_the_old_file_and_no_temp_file(tmp_path, monkeypatch):
    root, paths = _project(tmp_path)
    with python_facts_cache(tmp_path / "index"):
        _facts(res._collect_python_symbol_resolution_facts, paths, root)
    before = (tmp_path / "index" / FILE).read_bytes()
    (root / "late.py").write_text("def later():\n    pass\n", encoding="utf-8")
    paths = sorted(p for p in root.rglob("*") if p.is_file())
    want = _facts(res._collect_python_symbol_resolution_facts, paths, root)
    with monkeypatch.context() as m:
        m.setattr(pf.os, "replace", _refused)
        with python_facts_cache(tmp_path / "index") as c:
            assert _facts(res._collect_python_symbol_resolution_facts, paths, root) == want
    assert c.misses == 1
    assert [p.name for p in (tmp_path / "index").iterdir()] == [FILE]
    assert (tmp_path / "index" / FILE).read_bytes() == before
