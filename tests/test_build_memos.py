"""The build's memos return what the code they stand in for returns (docs/UPSTREAM.md, Modified).

* ``ids.normalize_id`` answers an exact ``str`` from a bounded memo; the recipe itself is
  ``ids._normalize_id_recipe``.
* ``build.build_from_json`` works out a source_file's alias forms (the pre-migration alias
  index) and an edge endpoint's suffix once per distinct string of one call.

Inputs are real: the extraction of ``tests_upstream/fixtures`` (every language the upstream
tests cover), the upstream alias cases, and, when it exists, Verinoda's own graph.
"""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import inspect  # noqa: E402
import json  # noqa: E402
import shutil  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda.project_index import build as upstream_build  # noqa: E402
from verinoda.project_index import ids  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests_upstream" / "fixtures"
# Verinoda's own graph (read only); another checkout's with VERINODA_TEST_OWN_GRAPH=<graph.json>
OWN_GRAPH = Path(os.environ.get("VERINODA_TEST_OWN_GRAPH") or ROOT / ".verinoda" / "index" / "graph.json")

ODD_STRINGS = [
    "", "_", "___", ".", "a..b", "__init__", "Foo.Bar", "foo-bar baz", "İslemYap", "ISLEM_yap",
    "\u0345\u0300", "\u0345\u0301", "a\u0345\u0300b", "\u01f0\u0f35\u0345", "Ångström", "Ⅳ", "ﬁle",
    "straße", "ΣΊΣΥΦΟΣ", "日本語_名前", "emoji_😀_id", "C:/abs/path/x.py", "src\\win\\y.cs", "  spaced  ",
]


@pytest.fixture(scope="module")
def fixture_extraction(tmp_path_factory):
    from verinoda.project_index.extract import collect_files, extract

    corpus = tmp_path_factory.mktemp("memos") / "fixtures"
    shutil.copytree(FIXTURES, corpus, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    files = collect_files(corpus, root=corpus)
    assert len(files) > 50
    return corpus, extract(files, cache_root=corpus.parent / "cache", root=corpus, parallel=False)


def _own_graph() -> dict | None:
    if not OWN_GRAPH.is_file():
        return None
    try:
        return json.loads(OWN_GRAPH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _strings(extraction: dict) -> list[str]:
    out: set[str] = set(ODD_STRINGS)
    for bucket in ("nodes", "edges", "links"):
        for item in extraction.get(bucket) or ():
            for key in ("id", "label", "source_file", "source", "target", "norm_label"):
                v = item.get(key)
                if isinstance(v, str):
                    out.add(v)
    return sorted(out)


# -- ids.normalize_id ----------------------------------------------------------------------------------------

def test_normalize_id_memo_returns_the_recipe(fixture_extraction):
    _, extraction = fixture_extraction
    strings = _strings(extraction)
    own = _own_graph()
    if own is not None:
        strings += _strings(own)
    assert len(strings) > 500
    ids._normalize_id_memo.cache_clear()
    for s in strings:
        want = ids._normalize_id_recipe(s)
        assert ids.normalize_id(s) == want  # a miss
        got = ids.normalize_id(s)  # a hit
        assert got == want and type(got) is str
    info = ids._normalize_id_memo.cache_info()
    assert info.hits >= len(set(strings)) and info.currsize == len(set(strings))
    # make_id goes through it too; every part split and joined as a caller would
    for s in strings[:2000]:
        parts = s.split(".")
        joined = "_".join(p.strip("_.") for p in parts if p)
        assert ids.make_id(*parts) == ids._normalize_id_recipe(joined)


def test_normalize_id_outside_the_memo_behaves_as_the_recipe():
    class Sub(str):
        pass

    before = ids._normalize_id_memo.cache_info()
    for s in ("Foo.Bar", "İslemYap", ""):
        got, want = ids.normalize_id(Sub(s)), ids._normalize_id_recipe(Sub(s))
        assert got == want == ids.normalize_id(s) and type(got) is type(want)
    assert ids._normalize_id_memo.cache_info().currsize == before.currsize  # a subclass is not kept
    for bad in (None, 3, b"bytes", ["un", "hashable"], {"a": 1}):  # raises where the recipe raises
        with pytest.raises(Exception) as recipe_exc:
            ids._normalize_id_recipe(bad)
        with pytest.raises(type(recipe_exc.value)):
            ids.normalize_id(bad)


def test_the_callers_still_share_one_normalize_id():
    from verinoda.project_index import dedup

    assert upstream_build._normalize_id is ids.normalize_id is dedup.normalize_id


# -- build_from_json: the alias forms and the endpoint suffixes ------------------------------------------------

def _upstream_alias_candidates(nodes: dict[str, dict]) -> dict[str, set[str]]:
    """The alias loop of build_from_json as upstream wrote it (build.py, #1504), with ``G.nodes[nid]``
    read from *nodes* and every id normalised by the recipe (no memo)."""
    from verinoda.project_index.extractors.base import _file_stem as _fs
    from verinoda.project_index.paths import is_absolute_any_platform as _is_abs

    _normalize_id = ids._normalize_id_recipe
    make_id = ids.make_id  # made memo-free by the caller (ids.normalize_id is the recipe then)
    _alias_candidates: dict[str, set[str]] = {}
    for nid in nodes:
        attrs = nodes[nid]
        sf = attrs.get("source_file")
        if not sf:
            continue
        rel = Path(str(sf))
        if _is_abs(str(sf)):
            continue
        new_stem = make_id(_fs(rel))
        if str(attrs.get("label", "")) == rel.name:
            suffix = ""
        else:
            suffix = ""
            if _normalize_id(nid).startswith(new_stem):
                suffix = _normalize_id(nid)[len(new_stem):]
        for old_stem in upstream_build._old_file_stems(rel):
            if old_stem == new_stem:
                continue
            alias = old_stem + suffix
            _alias_candidates.setdefault(_normalize_id(alias), set()).add(nid)
            _alias_candidates.setdefault(alias, set()).add(nid)
    return _alias_candidates


def _traced_build(extraction: dict, root: Path):
    """build_from_json, with the nodes as the alias loop starts and the function's locals as it returns."""
    fn = upstream_build.build_from_json
    code = fn.__code__
    src, first = inspect.getsourcelines(fn)
    alias_line = first + next(i for i, line in enumerate(src) if "_alias_candidates: dict" in line)
    seen: dict = {}

    def local(frame, event, arg):
        if event == "line" and frame.f_lineno == alias_line and "nodes" not in seen:
            g, node_set = frame.f_locals["G"], frame.f_locals["node_set"]
            seen["nodes"] = {nid: dict(g.nodes[nid]) for nid in node_set}
        elif event == "return":
            seen["locals"] = dict(frame.f_locals)
        return local

    def calls(frame, event, arg):
        return local if frame.f_code is code else None

    previous = sys.gettrace()
    sys.settrace(calls)
    try:
        G = fn(json.loads(json.dumps(extraction)), root=root)
    finally:
        sys.settrace(previous)
    assert "nodes" in seen and "locals" in seen
    return G, seen


ALIAS_CASES = {  # tests_upstream/test_build.py's old-stem alias cases, plus absolute and odd paths
    "nodes": [
        {"id": "dev_monitoring_ping", "label": "ping.h", "file_type": "code", "source_file": "Dev/monitoring/ping.h"},
        {"id": "www_pages_api_ping", "label": "ping.php", "file_type": "code", "source_file": "www/pages/api/ping.php"},
        {"id": "dev_poker_server", "label": "server.cpp", "file_type": "code", "source_file": "Dev/poker/server.cpp"},
        {"id": "tools_aolserver_utility_h_tools_aolserver_utility", "label": "utility.h", "file_type": "code",
         "source_file": "Tools/aolserver/utility.h"},
        {"id": "tools_aolserver_utility_cpp_tools_aolserver_utility", "label": "utility.cpp", "file_type": "code",
         "source_file": "Tools/aolserver/utility.cpp"},
        {"id": "wwwapi_masque_com_pages_utility", "label": "utility.php", "file_type": "code",
         "source_file": "wwwapi.masque.com/pages/utility.php"},
        {"id": "dev_monitoring_utility_x", "label": "x", "file_type": "code", "source_file": "Dev/monitoring/utility.h"},
        {"id": "setup", "label": "setup.py", "file_type": "code", "source_file": "setup.py"},
        {"id": "setup_main", "label": "main", "file_type": "code", "source_file": "setup.py"},
        {"id": "claude_claude", "label": "CLAUDE.md", "file_type": "document", "source_file": ".claude/CLAUDE.md"},
        {"id": "abs_posix", "label": "x", "file_type": "code", "source_file": "/elsewhere/abs.py"},
        {"id": "abs_win", "label": "y", "file_type": "code", "source_file": "D:/elsewhere/abs.py"},
        {"id": "no_suffix", "label": "Makefile", "file_type": "code", "source_file": "build/Makefile"},
        {"id": "dotted", "label": "a.b.c", "file_type": "code", "source_file": "pkg/a.b.c.ts"},
    ],
    "edges": [
        {"source": "dev_poker_server", "target": "ping", "relation": "imports", "confidence": "EXTRACTED",
         "source_file": "Dev/poker/server.cpp"},
        {"source": "dev_poker_server", "target": "utility", "relation": "imports", "confidence": "EXTRACTED",
         "source_file": "Dev/poker/server.cpp"},
        {"source": "setup_main", "target": "dotted", "relation": "calls", "confidence": "INFERRED",
         "source_file": "setup.py"},
        {"source": "abs_posix", "target": "no_suffix", "relation": "references", "confidence": "EXTRACTED",
         "source_file": "/elsewhere/abs.py"},
    ],
}


def _check_build(extraction: dict, root: Path, monkeypatch) -> None:
    from verinoda.project_index.extractors.base import _file_stem
    from verinoda.project_index.paths import is_absolute_any_platform

    _, seen = _traced_build(extraction, root)
    got = seen["locals"]
    with monkeypatch.context() as m:  # the reference runs without any memo
        m.setattr(ids, "normalize_id", ids._normalize_id_recipe)
        want = _upstream_alias_candidates(seen["nodes"])
        for s, forms in got["_sf_forms"].items():
            rel = Path(s)
            if is_absolute_any_platform(s):
                assert forms[0] is True
            else:
                assert forms == (False, rel.name, ids.make_id(_file_stem(rel)),
                                 tuple(upstream_build._old_file_stems(rel)))
    assert got["_alias_candidates"] == want
    assert list(got["_alias_candidates"]) == list(want)  # also in the same order
    for value, ext in got["_suffixes"].items():
        assert ext == Path(value).suffix.lower()


def test_build_from_json_alias_forms_match_upstream_on_the_fixtures(fixture_extraction, monkeypatch):
    corpus, extraction = fixture_extraction
    _check_build(extraction, corpus, monkeypatch)


def test_build_from_json_alias_forms_match_upstream_on_the_alias_cases(tmp_path, monkeypatch):
    _check_build(ALIAS_CASES, tmp_path, monkeypatch)
    G = upstream_build.build_from_json(json.loads(json.dumps(ALIAS_CASES)), root=tmp_path)
    assert not G.has_edge("dev_poker_server", "dev_monitoring_ping")  # ambiguous aliases stay dangling
    assert not G.has_edge("dev_poker_server", "wwwapi_masque_com_pages_utility")


def test_build_from_json_alias_forms_match_upstream_on_the_own_graph(monkeypatch):
    own = _own_graph()
    if own is None:
        pytest.skip("no .verinoda/index/graph.json in this checkout")
    extraction = {"nodes": own.get("nodes", []), "edges": own.get("links", own.get("edges", [])),
                  "hyperedges": own.get("hyperedges", [])}
    _check_build(extraction, ROOT, monkeypatch)


def test_the_old_stems_stay_a_fresh_list():
    """_semantic_id_remap inserts into what _old_file_stems returns, so it is never a shared object."""
    rel = Path("docs/dataflow.md")
    a, b = upstream_build._old_file_stems(rel), upstream_build._old_file_stems(rel)
    assert isinstance(a, list) and a == b and a is not b
    remap = upstream_build._semantic_id_remap(
        [{"id": "d_projects_myrepo_docs_dataflow_x", "source_file": "D:/projects/myrepo/docs/dataflow.md",
          "label": "x"}], "D:/projects/myrepo")
    assert remap == {"d_projects_myrepo_docs_dataflow_x": "docs_dataflow_x"}
    assert upstream_build._old_file_stems(rel) == b
