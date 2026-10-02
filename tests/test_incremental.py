"""The update proportional to the change (verinoda/incremental.py): its graph equals a fresh copy's scan.

The acceptance gate is the oracle fuzz (tools/incremental_fuzz.py): random edits on fixture corpora in Python,
TypeScript/JavaScript, Go and Java, each followed by ``verinoda update`` with the switch on, and the graph
compared with a scan of a fresh copy of the edited tree (tools/graph_equal.py). A fast subset with fixed seeds
runs here; the long run is the tool's.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import graph_equal  # noqa: E402
import incremental_fuzz as fz  # noqa: E402

from verinoda import incremental  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")

CORPORA = {
    "python": ROOT / "examples" / "orders_app",
    "typescript": ROOT / "tests" / "fixtures" / "incremental" / "ts_app",
    "go": ROOT / "tests" / "fixtures" / "incremental" / "go_app",
    "java": ROOT / "examples" / "glow_mod",
}


@pytest.fixture(autouse=True)
def _graphify_out(monkeypatch):
    monkeypatch.setenv("GRAPHIFY_OUT", ".verinoda/index")
    monkeypatch.delenv(incremental.ENV, raising=False)


@pytest.mark.parametrize("lang,seed", [("python", 11), ("typescript", 12), ("go", 13), ("java", 14)])
def test_fuzzed_updates_equal_a_fresh_scan(tmp_path, lang, seed):
    rows = fz.run(CORPORA[lang], seed, 3, tmp_path, switch="verify", keep_failed=False)
    bad = [r for r in rows if not r["equal"]]
    assert not bad, json.dumps(bad, indent=1)[:3000]
    assert any(r["used"] for r in rows), rows  # the patch ran, not only the fallback


def test_the_switch_is_off_by_default(tmp_path, monkeypatch):
    repo = tmp_path / "r"
    fz.copy_tree(CORPORA["python"], repo)
    assert not incremental.enabled(repo)
    fz._scan(repo, None)
    assert not (incremental.ledger_dir(repo) / incremental.LEDGER_FILE).exists()
    p = repo / "orders" / "pricing.py"
    p.write_text(p.read_text(encoding="utf-8") + "\n\ndef added(x):\n    return x\n", encoding="utf-8")
    res = fz._update(repo, None)
    assert res["index_mode"] == "full" and "incremental" not in res
    monkeypatch.setenv(incremental.ENV, "0")
    assert not incremental.enabled(repo)
    monkeypatch.setenv(incremental.ENV, "1")
    assert incremental.enabled(repo)


def test_an_update_says_why_it_fell_back(tmp_path):
    repo = tmp_path / "r"
    fz.copy_tree(CORPORA["python"], repo)
    fz._scan(repo, "1")
    assert (incremental.ledger_dir(repo) / incremental.LEDGER_FILE).exists()
    # a new file: the corpus changed
    (repo / "orders" / "extra.py").write_text("def extra():\n    return 1\n", encoding="utf-8")
    res = fz._update(repo, "1")
    assert res["index_mode"] == "full"
    assert res["incremental"]["used"] is False and "added" in res["incremental"]["reason"]
    # the full build wrote the ledger again: an edit is patched now
    p = repo / "orders" / "pricing.py"
    p.write_text(p.read_text(encoding="utf-8") + "\n\ndef added(x):\n    return extra()\n", encoding="utf-8")
    res = fz._update(repo, "1")
    assert res["index_mode"] == "incremental" and res["incremental"]["used"] is True
    assert "orders/pricing.py" in res["incremental"]["affected"]
    fresh = fz.fresh_graph(repo, tmp_path)
    assert graph_equal.compare(fz._graph(repo), fresh)["equal"]
    # a graph.json the ledger does not describe (rewritten by something else): the full build
    gp = repo / ".verinoda" / "index" / "graph.json"
    gp.write_text(gp.read_text(encoding="utf-8").rstrip("\n") + " \n", encoding="utf-8")
    p.write_text(p.read_text(encoding="utf-8") + "\n# note\n", encoding="utf-8")
    res = fz._update(repo, "1")
    assert res["incremental"]["used"] is False and "graph.json" in res["incremental"]["reason"]


def test_a_language_the_closure_was_not_checked_for_falls_back(tmp_path):
    repo = tmp_path / "r"
    fz.copy_tree(ROOT / "examples" / "forge_mod", repo)
    fz._scan(repo, "1")
    kt = sorted(repo.rglob("*.kt"))[0]
    kt.write_text(kt.read_text(encoding="utf-8") + "\n// note\n", encoding="utf-8")
    res = fz._update(repo, "1")
    assert res["incremental"]["used"] is False and "not checked" in res["incremental"]["reason"]
    assert graph_equal.compare(fz._graph(repo), fz.fresh_graph(repo, tmp_path))["equal"]


def test_build_rows_is_the_vendored_builder(tmp_path):
    """build_rows on a full build's extraction gives the graph build_from_json + to_json give (community aside)."""
    import io
    from contextlib import redirect_stderr, redirect_stdout

    from verinoda.project_index.build import build_from_json
    from verinoda.project_index.export import to_json

    for corpus in CORPORA.values():
        repo = tmp_path / corpus.name
        fz.copy_tree(corpus, repo)
        fz._scan(repo, "1")
        x = json.loads((incremental.ledger_dir(repo) / incremental.EXTRACTION_FILE).read_text(encoding="utf-8"))
        nodes, links = incremental.build_rows(json.loads(json.dumps(x)))
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            G = build_from_json(json.loads(json.dumps(x)))
            to_json(G, {}, str(tmp_path / "g.json"), force=True, built_at_commit="x")
        ref = json.loads((tmp_path / "g.json").read_text(encoding="utf-8"))
        res = graph_equal.compare(ref, {"nodes": list(nodes.values()), "links": links})
        assert res["equal"], (corpus.name, res["counts"], res["edges_only_in_a"][:2], res["edges_only_in_b"][:2])
        # and the scan's graph.json is that graph (the ledger holds what the build read)
        assert graph_equal.compare(fz._graph(repo), {"nodes": list(nodes.values()), "links": links})["equal"]


def test_the_vendored_builder_is_the_pinned_one():
    """build_rows reproduces build_from_json/to_json: when build.py or export.py change, check build_rows against
    them again (test_build_rows_is_the_vendored_builder, the fuzz) and update PINNED."""
    assert incremental.pinned_now() == incremental.PINNED


def test_a_config_key_turns_it_on(tmp_path):
    repo = tmp_path / "r"
    fz.copy_tree(CORPORA["python"], repo)
    fz._scan(repo, None)
    cfg = repo / ".verinoda" / "config.json"
    data = json.loads(cfg.read_text(encoding="utf-8"))
    data.setdefault("index", {})["incremental"] = True
    cfg.write_text(json.dumps(data), encoding="utf-8")
    old = os.environ.pop(incremental.ENV, None)
    try:
        assert incremental.enabled(repo)
    finally:
        if old is not None:
            os.environ[incremental.ENV] = old


# --------------------------------------------------------------------------------------------- review cases
# Each one: a small corpus scanned with the switch on, edits, an update, and its graph compared with a fresh copy's
# scan. They are edits whose reach the names in the files do not show.

_PKG = '{"name": "t", "version": "1.0.0", "private": true}\n'


def _corpus(tmp_path: Path, files: dict[str, str]) -> Path:
    src = tmp_path / "src_tree"
    for rel, text in files.items():
        p = src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")
    repo = tmp_path / "r"
    fz.copy_tree(src, repo)
    fz._scan(repo, "1")
    return repo


def _edit_and_compare(repo: Path, tmp_path: Path, edits: dict[str, str]) -> tuple[dict, dict]:
    for rel, text in edits.items():
        (repo / rel).write_text(text, encoding="utf-8", newline="\n")
    res = fz._update(repo, "1")
    return res, graph_equal.compare(fz._graph(repo), fz.fresh_graph(repo, tmp_path))


def _assert_equal(res: dict, cmp: dict) -> None:
    assert cmp["equal"], json.dumps({"incremental": res.get("incremental"), "counts": cmp["counts"],
                                     **{k: cmp[k][:3] for k in ("nodes_only_in_a", "nodes_only_in_b",
                                                                "edges_only_in_a", "edges_only_in_b")}})[:3000]


def test_a_renamed_default_export_reaches_its_importer(tmp_path):
    repo = _corpus(tmp_path, {
        "package.json": _PKG,
        "src/def.ts": "export default function makeThing() {\n  return 1;\n}\n",
        "src/usedef.ts": 'import mk from "./def";\n\nexport function go() {\n  return mk();\n}\n',
    })
    res, cmp = _edit_and_compare(repo, tmp_path, {
        "src/def.ts": "export default function buildThing() {\n  return 1;\n}\n"})
    assert res["incremental"]["used"] is True and "src/usedef.ts" in res["incremental"]["affected"]
    _assert_equal(res, cmp)
    # and the next patched update starts from the right rows
    res, cmp = _edit_and_compare(repo, tmp_path, {
        "src/usedef.ts": 'import mk from "./def";\n\nexport function go() {\n  return mk() + 1;\n}\n'})
    assert res["incremental"]["used"] is True
    _assert_equal(res, cmp)


def test_a_renamed_export_alias_reaches_its_importer(tmp_path):
    repo = _corpus(tmp_path, {
        "package.json": _PKG,
        "src/a.ts": "function inner() {\n  return 1;\n}\n\nexport { inner as outer };\n",
        "src/b.ts": 'import { outer } from "./a";\n\nexport function useIt() {\n  return outer();\n}\n',
    })
    res, cmp = _edit_and_compare(repo, tmp_path, {
        "src/a.ts": "function inner() {\n  return 1;\n}\n\nexport { inner as other };\n"})
    assert res["incremental"]["used"] is True
    _assert_equal(res, cmp)


_JAVA = "src/main/java/com/"


def test_a_java_package_moved_drops_the_imports_of_it(tmp_path):
    foo = "package com.{p};\n\npublic class Foo {{\n    public void go() {{\n    }}\n}}\n"
    repo = _corpus(tmp_path, {
        _JAVA + "a/Foo.java": foo.format(p="a"),
        _JAVA + "c/Foo.java": foo.format(p="c"),
        _JAVA + "b/Bar.java": "package com.b;\n\nimport com.a.Foo;\n\npublic class Bar {\n    void run() {\n"
                              "        new Foo().go();\n    }\n}\n",
    })
    res, cmp = _edit_and_compare(repo, tmp_path, {_JAVA + "a/Foo.java": foo.format(p="z")})
    assert res["incremental"]["used"] is True
    _assert_equal(res, cmp)


def test_a_method_moved_to_another_class_of_its_file(tmp_path):
    repo = _corpus(tmp_path, {
        "shapes.py": "class Widget:\n    def render(self):\n        return 1\n\n\nclass Gadget:\n"
                     "    def other(self):\n        return 2\n",
        "use.py": "from shapes import Gadget, Widget\n\n\ndef b():\n    Widget.render(None)\n"
                  "    Gadget.render(None)\n",
        "use2.py": "from shapes import Gadget\n\n\ndef c():\n    return Gadget.render(None)\n",
        "README.md": "# Shapes\n\nSee `Gadget.render`.\n",
    })
    res, cmp = _edit_and_compare(repo, tmp_path, {
        "shapes.py": "class Widget:\n    pass\n\n\nclass Gadget:\n    def other(self):\n        return 2\n\n"
                     "    def render(self):\n        return 1\n"})
    assert res["incremental"]["used"] is True
    assert {"use.py", "use2.py", "README.md"} <= set(res["incremental"]["affected"])
    _assert_equal(res, cmp)


def test_a_java_field_retyped_under_a_subclass(tmp_path):
    cls = "package com.x;\n\npublic class {n} {{\n    public void go() {{\n    }}\n}}\n"
    bar = "package com.x;\n\npublic class Bar {{\n    protected {t} f = new {t}();\n}}\n"
    repo = _corpus(tmp_path, {
        _JAVA + "x/Foo.java": cls.format(n="Foo"),
        _JAVA + "x/Other.java": cls.format(n="Other"),
        _JAVA + "x/Bar.java": bar.format(t="Foo"),
        _JAVA + "x/Baz.java": "package com.x;\n\npublic class Baz extends Bar {\n    void run() {\n"
                              "        f.go();\n        this.f.go();\n    }\n}\n",
    })
    res, cmp = _edit_and_compare(repo, tmp_path, {_JAVA + "x/Bar.java": bar.format(t="Other")})
    assert res["incremental"]["used"] is True
    _assert_equal(res, cmp)


def test_a_go_type_whose_id_is_its_file_stem(tmp_path):
    """store/store.go's stem is ``store_store``, the id of its ``type Store`` too: the ledger records it as an id the
    package makes, so a file that switches from one such type to another brings both definers in."""
    typ = "package {p}\n\ntype {T} struct{{}}\n\nfunc (s *{T}) Find() int {{\n\treturn 1\n}}\n"
    types = 'package svc\n\nimport "example.com/g/{p}"\n\ntype S struct {{\n\tst *{p}.{T}\n}}\n'
    repo = _corpus(tmp_path, {
        "go.mod": "module example.com/g\n\ngo 1.22\n",
        "store/store.go": typ.format(p="store", T="Store"),
        "cache/cache.go": typ.format(p="cache", T="Cache"),
        "svc/types.go": types.format(p="store", T="Store"),
        "svc/do.go": "package svc\n\nfunc (s *S) Do() int {\n\treturn s.st.Find()\n}\n",
    })
    ledger = json.loads((incremental.ledger_dir(repo) / incremental.LEDGER_FILE).read_text(encoding="utf-8"))
    assert "n:store_store" in ledger["pre"]["store/store.go"]
    assert "n:cache_cache" in ledger["pre"]["cache/cache.go"]
    res, cmp = _edit_and_compare(repo, tmp_path, {"svc/types.go": types.format(p="cache", T="Cache")})
    if res["incremental"]["used"]:
        assert {"store/store.go", "cache/cache.go"} <= set(res["incremental"]["affected"])
        _assert_equal(res, cmp)
    else:  # a fallback says why; the full update's own difference (an orphan external stub) is not the patch's
        assert res["incremental"]["reason"]


def test_a_file_changed_without_a_graph_build_is_extracted_by_the_next_patch(tmp_path):
    repo = _corpus(tmp_path, {
        "app.py": "def a():\n    return 1\n",
        "README.md": "# App\n\nText.\n",
        "data.json": "{}\n",
    })
    (repo / "data.json").write_text('{"dependencies": {"left-pad": "1.0.0"}, "extends": "./base.json"}\n',
                                    encoding="utf-8")
    res = fz._update(repo, "1")
    assert res["index_mode"] == "none"
    ledger = json.loads((incremental.ledger_dir(repo) / incremental.LEDGER_FILE).read_text(encoding="utf-8"))
    assert ledger.get("pending") == ["data.json"]
    res, cmp = _edit_and_compare(repo, tmp_path, {"app.py": "def a():\n    return 1\n\n\ndef b():\n    return a()\n"})
    _assert_equal(res, cmp)
    res, cmp = _edit_and_compare(repo, tmp_path, {"README.md": "# App\n\nMore text.\n"})
    _assert_equal(res, cmp)
