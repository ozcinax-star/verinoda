"""The memos of the vendored extraction (local changes, docs/UPSTREAM.md) answer as the functions did.

Each memoised function is compared with a copy of its upstream body on real paths: every file of this
checkout, the upstream fixtures, and odd values. The passes changed in place (the twin-drop counter,
the bare Markdown mention, the final relativisation, the folded label of graph.json) are checked on
real extraction output.
"""

from __future__ import annotations

import json
import os
import random
import re
import shutil
from collections import Counter
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath

import networkx as nx
import pytest

from verinoda.project_index import export, extract as ex, markdown_resolution as mdr, paths as ppaths
from verinoda.project_index.extractors import base, resolution as res

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests_upstream" / "fixtures"


def _real_paths() -> list[str]:
    """Every file of this checkout's code, tests and docs, relative and absolute, as the passes see them."""
    out: list[str] = []
    for top in ("verinoda", "tests", "tests_upstream", "docs", "examples"):
        for p in sorted((ROOT / top).rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts:
                out.append(p.relative_to(ROOT).as_posix())
                out.append(str(p))
    odd = ["a", ".", "..", "./x.py", "x.", "x.PY", "X.Php", "dir.d/file", "a/b.tar.gz", ".hidden", "a\\b\\C.Swift",
           "C:\\a\\b.h", "/abs/posix/file.KT", "tests\\x_test.go", "weird name .java", "ünïcode/ğ.py", "ş.sql"]
    return out + odd


PATHS = _real_paths()


# -- _lang_family / _lang_is_case_insensitive (extract.py) ------------------------------------------------

def _family_upstream(source_file: object) -> str | None:
    if not source_file:
        return None
    return ex._LANG_FAMILY_BY_EXT.get(Path(str(source_file)).suffix.lower())


def _case_insensitive_upstream(source_file: object) -> bool:
    if not source_file:
        return False
    return Path(str(source_file)).suffix.lower() in ex._CASE_INSENSITIVE_EXTS


def test_language_family_memos_answer_as_the_suffix_tables_do():
    values: list[object] = [*PATHS, None, "", 0, 1, PurePosixPath("a/b.PHP"), Path("q.swift"), PureWindowsPath("C:/x.Go")]
    for _ in range(2):  # the second round is answered from the memo
        for v in values:
            assert ex._lang_family(v) == _family_upstream(v), v
            assert ex._lang_is_case_insensitive(v) is _case_insensitive_upstream(v), v
    assert ex._LANG_FAMILY_MEMO and ex._LANG_CASE_INSENSITIVE_MEMO


def test_language_family_memo_is_bounded(monkeypatch):
    monkeypatch.setattr(ex, "_LANG_MEMO_MAX", 5)
    ex._LANG_FAMILY_MEMO.clear()
    for i in range(20):
        assert ex._lang_family(f"f{i}.py") == "python"
        assert len(ex._LANG_FAMILY_MEMO) <= 5


# -- _file_stem (extractors/base.py) ----------------------------------------------------------------------

def _file_stem_upstream(path) -> str:
    if not path.name:
        return ""
    return path.with_suffix("").as_posix()


def test_file_stem_memo_matches_the_lexical_stem():
    values: list[PurePath] = []
    for s in PATHS + ["", "C:/", "/", "a/b/", "a//b.c", "C:rel.py"]:
        values += [Path(s), PurePosixPath(s), PureWindowsPath(s)]
    # the case of a path is kept (Windows paths compare equal across case, the stems do not)
    values += [Path("Pkg/Mod.py"), Path("pkg/mod.py"), Path("PKG/MOD.PY")]
    for _ in range(2):
        for v in values:
            assert base._file_stem(v) == _file_stem_upstream(v), repr(v)
    assert base._file_stem(Path("Pkg/Mod.py")) == "Pkg/Mod"
    assert base._file_stem(Path("pkg/mod.py")) == "pkg/mod"
    for _ in range(2):  # a string is not a path, as before
        with pytest.raises(AttributeError):
            base._file_stem("a/b.py")


# -- _is_test_path (paths.py) -----------------------------------------------------------------------------

def _is_test_path_upstream(path) -> bool:
    if not path:
        return False
    norm = str(path).replace("\\", "/")
    pure = PurePosixPath(norm)
    for segment in pure.parts:
        if segment.lower() in ppaths._TEST_DIR_SEGMENTS:
            return True
    filename = pure.name
    if not filename:
        return False
    return any(pattern.match(filename) for pattern in ppaths._TEST_FILENAME_PATTERNS)


def test_is_test_path_memo_matches_the_classifier():
    values: list[object] = [*PATHS, None, "", "tests", "Tests/", "a/FooTest.java", "a/footest.java",
                            "x.spec.TS", "latest.py", Path("tests/x.py"), PurePosixPath("src/contest.py")]
    for _ in range(2):
        for v in values:
            assert ppaths._is_test_path(v) is _is_test_path_upstream(v), v


# -- _js_source_path (extractors/resolution.py) -----------------------------------------------------------

def _js_source_path_upstream(source_file: str, root: Path):
    if not source_file:
        return None
    path = Path(source_file)
    if not path.is_absolute():
        path = root / path
    try:
        return res._resolve_cached(path)
    except Exception:
        return path


def test_js_source_path_memo_matches_the_resolve(tmp_path, monkeypatch):
    for rel in ("src/a.ts", "src/b/index.ts", "lib/c.js"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("export const x = 1;\n", encoding="utf-8")
    other = tmp_path / "other"
    other.mkdir()
    names = ["src/a.ts", "src/b/index.ts", "src/b/../a.ts", "lib/c.js", "missing.ts", "./src/a.ts",
             str(tmp_path / "src" / "a.ts"), str(tmp_path / "nope" / "x.ts"), "", "SRC/A.TS"]
    res._JS_SOURCE_PATH_MEMO.clear()
    for cwd in (tmp_path, other):
        monkeypatch.chdir(cwd)
        for root in (tmp_path, Path("."), Path("src"), other):
            for _ in range(2):
                for name in names:
                    assert res._js_source_path(name, root) == _js_source_path_upstream(name, root), (cwd, root, name)
    assert res._JS_SOURCE_PATH_MEMO


def test_js_source_path_memo_is_cleared_with_the_resolve_memo(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    res._js_source_path("a.ts", tmp_path)
    assert res._JS_SOURCE_PATH_MEMO
    ex.extract([tmp_path / "a.py"], cache_root=tmp_path, root=tmp_path, parallel=False)
    # extract() clears it at its start, beside _cached_realpath (whatever the run itself added is its own)
    assert ("a.ts", type(tmp_path), str(tmp_path), os.getcwd()) not in res._JS_SOURCE_PATH_MEMO


# -- _go_import_path_for_file (extractors/resolution.py) --------------------------------------------------

def _go_import_path_upstream(source_file, root, module_cache=None):
    cache = module_cache if module_cache is not None else {}
    path = Path(source_file)
    if not path.is_absolute():
        path = root / path
    try:
        directory = res._resolve_cached(path).parent
    except OSError:
        directory = path.absolute().parent
    module_dir = module_path = None
    for candidate in (directory, *directory.parents):
        if candidate in cache:
            cached = cache[candidate]
            if cached:
                module_dir, module_path = candidate, cached
            break
        go_mod = candidate / "go.mod"
        if not go_mod.is_file():
            continue
        try:
            match = re.search(r"(?m)^\s*module\s+([^\s]+)", go_mod.read_text(encoding="utf-8"))
        except (OSError, UnicodeError):
            match = None
        module_dir = candidate
        module_path = match.group(1) if match else None
        cache[candidate] = module_path
        break
    if not module_dir or not module_path:
        return None
    try:
        relative_dir = directory.relative_to(module_dir)
    except ValueError:
        return None
    suffix = relative_dir.as_posix()
    return module_path if suffix == "." else f"{module_path}/{suffix}"


def test_go_module_lookup_with_a_shared_cache_answers_as_upstream(tmp_path):
    files = {
        "mod/go.mod": "module example.com/shop\n\ngo 1.21\n",
        "mod/main.go": "package main\n",
        "mod/store/store.go": "package store\n",
        "mod/store/deep/x.go": "package deep\n",
        "mod/inner/go.mod": "module example.com/inner\n",
        "mod/inner/pkg/y.go": "package pkg\n",
        "nomodline/go.mod": "go 1.21\n",
        "nomodline/a/z.go": "package a\n",
        "free/a/b/w.go": "package b\n",
        "free/v.go": "package free\n",
    }
    for rel, text in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text, encoding="utf-8")
    queries = [rel for rel in files if rel.endswith(".go")]
    queries += [str(tmp_path / q) for q in queries] + ["missing/m.go", "mod/store/none.go", ""]
    rng = random.Random(7)
    for _ in range(6):
        order = queries * 2
        rng.shuffle(order)
        new_cache: dict = {}
        old_cache: dict = {}
        for q in order:
            expected = _go_import_path_upstream(q, tmp_path)
            assert _go_import_path_upstream(q, tmp_path, old_cache) == expected, q
            assert res._go_import_path_for_file(q, tmp_path, new_cache) == expected, q
            assert res._go_import_path_for_file(q, tmp_path) == expected, q
    assert res._go_import_path_for_file("mod/store/deep/x.go", tmp_path, {}) == "example.com/shop/store/deep"
    assert res._go_import_path_for_file("free/a/b/w.go", tmp_path, {}) is None
    # a directory with no go.mod is remembered as such, and only in a cache the caller passed
    cache: dict = {}
    res._go_import_path_for_file("free/a/b/w.go", tmp_path, cache)
    assert cache[(tmp_path / "free" / "a" / "b").resolve()] is res._GO_NO_MODULE_FILE


# -- in-place passes of extract() on real output ----------------------------------------------------------

@pytest.fixture(scope="module")
def extracted(tmp_path_factory):
    work = tmp_path_factory.mktemp("memo_corpus")
    corpus = work / "corpus"
    shutil.copytree(FIXTURES, corpus / "fixtures")
    shutil.copytree(ROOT / "examples" / "orders_app", corpus / "orders_app",
                    ignore=shutil.ignore_patterns(".verinoda", "__pycache__", "*.pyc", "*.db"))
    (corpus / "ts").mkdir()
    (corpus / "ts" / "a.ts").write_text("export function aa(): number { return 1; }\n", encoding="utf-8")
    (corpus / "ts" / "index.ts").write_text("export * from './a';\nexport { aa as bb } from './a';\n", encoding="utf-8")
    (corpus / "ts" / "use.ts").write_text("import { aa, bb } from './index';\nexport const u = aa() + bb();\n",
                                          encoding="utf-8")
    (corpus / "docs").mkdir()
    (corpus / "docs" / "guide.md").write_text(
        "# Guide\n\n`OrderService` and `service.fetch_order`, `aa()` and `orders/service.py::OrderService`.\n",
        encoding="utf-8")
    from verinoda.project_index.detect import detect

    found = detect(corpus)
    files = [Path(f) for f in found["files"]["code"] + found["files"].get("document", [])
             if Path(f).suffix in ex._DISPATCH or Path(f).suffix in mdr.MARKDOWN_MENTION_SUFFIXES]
    assert len(files) > 40
    return corpus, ex.extract(files, cache_root=work / "cache", root=corpus, parallel=False)


def test_the_final_relativisation_leaves_no_absolute_path(extracted):
    corpus, result = extracted
    seen = 0
    for item in result["nodes"] + result["edges"]:
        for key in ("source_file", "definition_file"):
            value = item.get(key)
            if value:
                seen += 1
                assert not Path(value).is_absolute(), (key, value)
                assert str(corpus) not in str(value)
    assert seen > 500


def test_the_twin_counter_counts_every_key_a_twin_can_have(extracted):
    """Only an `imports` edge's key can equal a twin key: counting all edges or only those gives the same."""
    _, result = extracted

    def _edge_key(edge):
        return json.dumps({k: v for k, v in edge.items() if k != "target_file"},
                          sort_keys=True, separators=(",", ":"), default=str)

    edges = result["edges"]
    every = Counter(_edge_key(e) for e in edges)
    imports = Counter(_edge_key(e) for e in edges if e.get("relation") == "imports")
    targets = sorted({str(e.get("target")) for e in edges})[:200]
    probes = 0
    for e in edges:
        if e.get("relation") != "imports":
            continue
        for candidate in [e.get("target"), *targets]:
            twin = _edge_key({**e, "target": candidate})
            assert every[twin] == imports[twin]
            probes += 1
    assert probes > 1000


def test_a_bare_markdown_mention_needs_no_evidence(extracted, monkeypatch):
    """With no qualifier the evidence walk is skipped; the edges are those the walk would allow."""
    corpus, result = extracted
    calls: list[str] = []
    real = mdr._evidence
    monkeypatch.setattr(mdr, "_evidence", lambda nid, *a: calls.append(nid) or real(nid, *a))
    doc = {"raw_calls": [
        {"language": "markdown", "caller_nid": n["id"], "callee": callee, "qualifiers": quals,
         "source_file": "docs/guide.md", "source_location": "L1"}
        for n in result["nodes"][:1]
        for callee, quals in (("OrderService", []), ("fetch_order", ["service"]), ("aa", []), ("fetch_order", []))
    ]}
    nodes = [dict(n) for n in result["nodes"]]
    edges = [dict(e) for e in result["edges"] if e.get("relation") != "references"]
    mdr.resolve_markdown_mentions([doc], nodes, edges)
    assert calls  # only the dotted mention walked
    assert all(c for c in calls)
    bare_candidates = {n["id"] for n in nodes if mdr._symbol_label(n) in ("OrderService", "aa")}
    assert not bare_candidates & set(calls)


# -- norm_label in graph.json (export.py) -----------------------------------------------------------------

def test_graph_json_norm_label_is_the_folded_label(tmp_path):
    labels = ["OrderService", "orderService", "fetch_order()", "Ünïcode", "İstanbul", "ıi", "ﬁle", "Ｆｕｌｌ",
              "e\u0301", "Ünïcode", "", "x.py", "ǅ", "Straße", "ΣΑΣ", "a\u0300b\u0327"]
    G = nx.Graph()
    for i, label in enumerate(labels):
        G.add_node(f"n{i}", label=label)
    G.add_node("int_label", label=12)
    G.add_node("no_label")
    out = tmp_path / "graph.json"
    assert export.to_json(G, {0: list(G.nodes)}, str(out), force=True, built_at_commit="x")
    data = json.loads(out.read_text(encoding="utf-8"))
    for node in data["nodes"]:
        assert node["norm_label"] == export._strip_diacritics(node.get("label", "")).lower(), node
    assert {n["id"]: n["norm_label"] for n in data["nodes"]}["n3"] == "unicode"
