"""The update-path speed-ups (backlog 1.1, stage A, group "watch") change nothing an update writes.

* ``watch._list_counts_differ``: the count check before the canonical graph/topology compares.
* ``index._relativize_once``: ``watch._relativize_source_files`` kept per value for a build.
* ``watch._no_extractor_memo``: the reconcile asks ``_get_extractor`` once per source string.
* ``detect.ignored_predicate``: the git listing of tracked files is made on first use.

Each is checked against the code it replaces on real data, and an update built both ways writes the
same bytes."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import contextlib  # noqa: E402
import copy  # noqa: E402
import json  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import index  # noqa: E402
from verinoda.paths import index_dir  # noqa: E402
from verinoda.project_index import cache, detect, watch  # noqa: E402
from verinoda.project_index.extract import _get_extractor  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "orders_app"
FIXTURES = ROOT / "tests_upstream" / "fixtures"
IGNORE = shutil.ignore_patterns(".verinoda", "__pycache__", "*.pyc", ".pytest_cache", "*.db")

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args],
                   cwd=cwd, check=True, capture_output=True)


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    repo = tmp_path_factory.mktemp("memos") / "orders_app"
    shutil.copytree(EXAMPLE, repo, ignore=IGNORE)
    index.build(repo, force=True)
    return repo


# -- the count check -------------------------------------------------------------------------------

def _full(canon, a, b) -> bool:
    """The gate as upstream wrote it."""
    try:
        return (json.dumps(canon(a), sort_keys=True, ensure_ascii=False)
                == json.dumps(canon(b), sort_keys=True, ensure_ascii=False))
    except Exception:  # noqa: BLE001 - the gate's own fallback
        return False


def _variants(graph: dict) -> list:
    nodes, links = graph["nodes"], graph["links"]
    out = [graph, copy.deepcopy(graph)]
    g = copy.deepcopy(graph)
    g["nodes"].append({"id": "extra_node", "label": "extra()", "source_file": "orders/extra.py"})
    out.append(g)
    g = copy.deepcopy(graph)
    g["links"].append(dict(links[0], relation="references"))
    out.append(g)
    g = copy.deepcopy(graph)
    g.pop("directed", None)
    out.append(g)  # the same graph for the graph compare, which reads a missing key as False
    g = copy.deepcopy(graph)
    g["built_at_commit"] = "0" * 40
    out.append(g)
    g = copy.deepcopy(graph)
    g["nodes"].append("not-a-dict")  # counted by the graph compare, dropped by the topology one
    out.append(g)
    g = copy.deepcopy(graph)
    g["links"].append(7)
    out.append(g)
    g = copy.deepcopy(graph)
    g["nodes"] = list(reversed(nodes))
    g["links"] = list(reversed(links))
    out.append(g)  # the same content in another order
    g = copy.deepcopy(graph)
    for n in g["nodes"]:
        n["community"] = 0
        n.pop("norm_label", None)
    out.append(g)  # the topology compare leaves these fields out
    g = copy.deepcopy(graph)
    g["nodes"][0]["label"] = "renamed"
    out.append(g)  # equal counts, different content
    g = copy.deepcopy(graph)
    g["edges"] = g.pop("links")
    out.append(g)
    g = copy.deepcopy(graph)
    g["hyperedges"] = [{"id": "h", "nodes": [nodes[0]["id"]]}]
    out.append(g)
    g = copy.deepcopy(graph)
    g["hyperedges"] = [{"id": "h", "nodes": [nodes[0]["id"]]}, "odd"]
    out.append(g)
    g = copy.deepcopy(graph)
    g["nodes"] = {"not": "a list"}
    out.append(g)
    g = copy.deepcopy(graph)
    g["links"].append({"source": nodes[0]["id"], "target": nodes[1]["id"], "x": {1, 2}})
    out.append(g)  # json.dumps cannot write a set: the gate falls back to "changed"
    out += [{}, {"nodes": []}, [["nodes", []]], None]
    return out


def test_the_count_check_decides_only_where_the_compare_is_false(built):
    from networkx.readwrite import json_graph

    graph = json.loads((index_dir(built) / "graph.json").read_text(encoding="utf-8"))
    G = json_graph.node_link_graph(graph, edges="links")
    topology = watch._topology_from_graph(G)  # the shape the topology gate's candidate has
    cases = _variants(graph) + _variants(topology)
    decided = 0
    for canon, dicts_only in ((watch._canonical_graph_for_compare, False),
                              (watch._canonical_topology_for_compare, True)):
        for a in cases:
            for b in cases:
                full = _full(canon, a, b)
                differ = watch._list_counts_differ(a, b, dicts_only=dicts_only)
                assert ((not differ) and full) == full
                assert not (differ and full)
                decided += differ
    assert decided > 100  # the check does decide (the pairs with an extra item)
    assert _full(watch._canonical_graph_for_compare, graph, _variants(graph)[4])  # `directed` missing


# -- the relativize memo ---------------------------------------------------------------------------

def _extraction_like(repo: Path) -> list[dict]:
    """The AST cache entries of a build, re-anchored as extraction hands them over (absolute paths)."""
    payloads = []
    for entry in sorted(cache.cache_dir(repo, "ast").glob("*.json")):
        payload = json.loads(entry.read_text(encoding="utf-8"))
        cache._absolutize_source_files_in(payload, repo)
        payloads.append(payload)
    return payloads


def test_extracted_paths_are_made_relative_as_the_upstream_function_does(built, tmp_path):
    repo = built
    real = watch._relativize_source_files
    payloads = _extraction_like(repo)
    assert len(payloads) > 5
    merged = {"nodes": [n for p in payloads for n in p.get("nodes", [])],
              "edges": [e for p in payloads for e in p.get("edges", [])]}
    outside = tmp_path / "elsewhere" / "x.py"
    payloads += [
        merged,
        {"nodes": [{"source_file": "a/b.py"}, {"source_file": str(repo / "abs.py")},
                   {"source_file": str(repo / "orders" / ".." / "orders" / "api.py")},
                   {"source_file": str(outside)}, {"source_file": str(outside), "definition_file": str(outside)},
                   {"source_file": ""}, {"source_file": None}, {"definition_file": str(repo / "d" / "e.cpp")},
                   {"source_file": repo / "orders" / "api.py"}, {"source_file": Path("rel.py")}],
         "edges": [{"source_file": str(repo / "orders" / "api.py"), "definition_file": "rel/x.h"}],
         "hyperedges": [{"source_file": str(repo / "orders" / "api.py")}, {"source_file": "/posix/abs"}],
         "raw_calls": [{"source_file": str(repo / "orders" / "api.py")}]},
        {},
    ]
    for resolve_ctx in (contextlib.nullcontext, index._resolve_once):
        with resolve_ctx(), index._relativize_once():
            memo = watch._relativize_source_files
            assert memo is not real
            for root, scope in ((repo, repo), (repo, None), (repo.parent, repo), (repo, repo / "orders"),
                                (repo, repo)):  # the first pair again, from the memo
                for p in payloads:
                    a, b = copy.deepcopy(p), copy.deepcopy(p)
                    real(a, root, scope=scope)
                    memo(b, root, scope=scope)
                    assert a == b
                    assert [type(i.get("source_file")) for i in a.get("nodes", [])] == \
                           [type(i.get("source_file")) for i in b.get("nodes", [])]
            for fn in (real, memo):  # what raises in the original raises in the memo, at the same item
                bad = {"nodes": [{"source_file": str(repo / "orders" / "api.py")}, {"source_file": ["a", "list"]}]}
                with pytest.raises(TypeError):
                    fn(bad, repo, scope=repo)
                assert bad["nodes"][0]["source_file"] == "orders/api.py"
                with pytest.raises(AttributeError):
                    fn({"nodes": ["not-a-dict"]}, repo)
        assert watch._relativize_source_files is real


# -- the reconcile's extractor memo ----------------------------------------------------------------

def test_the_reconcile_asks_the_extractor_once_per_source_and_gets_its_answer(tmp_path, monkeypatch):
    files = sorted(p for p in FIXTURES.rglob("*") if p.is_file())
    assert len(files) > 100
    odd = tmp_path / "tool"
    odd.write_bytes(b"#!/usr/bin/env bash\necho hi\n")  # extensionless: read for its shebang
    (tmp_path / "plain.h").write_bytes(b"int f(void);\n")
    (tmp_path / "cpp.h").write_bytes(b"namespace n { class C {}; }\n")
    (tmp_path / "matlab.m").write_bytes(b"function y = f(x)\n  y = x;\nend\n")
    extra = [odd, tmp_path / "plain.h", tmp_path / "cpp.h", tmp_path / "matlab.m", tmp_path / "gone.h",
             tmp_path / "notes.txt"]
    monkeypatch.chdir(ROOT)
    values = [str(p) for p in files + extra]
    values += [p.relative_to(ROOT).as_posix() for p in files[:200]]  # relative: against the working dir
    values += [Path(values[0]), PathLikeOnly(values[1])]
    calls: list[str] = []

    def counting(path):
        calls.append(str(path))
        return _get_extractor(path)

    ask = watch._no_extractor_memo(counting)
    for _ in range(3):  # a file's nodes ask again and again
        for v in values:
            assert ask(v) == (_get_extractor(Path(v)) is None)
    strings = {v for v in values if type(v) is str}
    assert len(calls) == len(strings) + 2 * 3  # the two non-str values are asked every time
    assert any(ask(v) for v in strings) and not all(ask(v) for v in strings)
    fresh = watch._no_extractor_memo(counting)  # one reconcile's memo is not another's
    before = len(calls)
    fresh(values[0])
    assert len(calls) == before + 1


class PathLikeOnly:
    def __init__(self, s: str) -> None:
        self.s = s

    def __fspath__(self) -> str:
        return self.s


# -- the lazy git listing ------------------------------------------------------------------------------

@needs_git
def test_the_ignore_predicate_lists_tracked_files_on_first_use_and_answers_as_before(tmp_path, monkeypatch):
    repo = tmp_path / "r"
    (repo / "src" / "gen").mkdir(parents=True)
    (repo / "node_modules").mkdir()
    _git(repo, "init", "-q")
    files = {
        "src/tracked_ignored.py": "a = 1\n",   # a .gitignore rule, but tracked: kept
        "src/untracked_ignored.py": "b = 1\n",
        "src/kept.py": "c = 1\n",
        "src/gen/out.py": "d = 1\n",           # a nested .gitignore
        "src/explicit.py": "e = 1\n",          # .graphifyignore: dropped though tracked
        "node_modules/m.js": "x\n",
    }
    for rel, text in files.items():
        (repo / rel).write_text(text, encoding="utf-8")
    _git(repo, "add", "src/tracked_ignored.py", "src/kept.py", "src/explicit.py")
    (repo / ".gitignore").write_text("*_ignored.py\n", encoding="utf-8")
    (repo / "src" / "gen" / ".gitignore").write_text("out.py\n", encoding="utf-8")
    (repo / ".graphifyignore").write_text("src/explicit.py\n", encoding="utf-8")
    listed: list[Path] = []
    real = detect._git_tracked_path_keys
    monkeypatch.setattr(detect, "_git_tracked_path_keys", lambda root: listed.append(root) or real(root))

    expected = {
        True: {"src/tracked_ignored.py": False, "src/untracked_ignored.py": True, "src/kept.py": False,
               "src/gen/out.py": True, "src/explicit.py": True, "node_modules/m.js": True},
        False: {"src/tracked_ignored.py": False, "src/untracked_ignored.py": False, "src/kept.py": False,
                "src/gen/out.py": False, "src/explicit.py": True, "node_modules/m.js": True},
    }
    for gitignore in (True, False):
        for order in (list(files), list(reversed(files))):
            listed.clear()
            pred = detect.ignored_predicate(repo, extra_excludes=["*.tmp"], gitignore=gitignore)
            assert listed == []  # nothing listed while the predicate is built
            got = {rel: pred(repo / rel) for rel in order}
            assert got == expected[gitignore]
            assert len(listed) == (1 if gitignore else 0)  # once, and only where .gitignore adds rules
            assert pred(tmp_path / "outside.py") is False
            assert len(listed) == (1 if gitignore else 0)


# -- an update both ways ---------------------------------------------------------------------------

def _eager_predicate(real):
    def build(root, **kw):
        pred = real(root, **kw)
        pred(Path(root).resolve() / "__eager_listing__")  # lists the tracked files now, as before
        return pred
    return build


@contextlib.contextmanager
def _as_before(monkeypatch):
    """The four speed-ups turned back into the code they replace."""
    with monkeypatch.context() as m:
        m.setattr(watch, "_list_counts_differ", lambda a, b, *, dicts_only: False)
        m.setattr(watch, "_no_extractor_memo", lambda ge: (lambda sf: ge(Path(sf)) is None))
        m.setattr(index, "_relativize_once", contextlib.nullcontext)
        m.setattr(detect, "ignored_predicate", _eager_predicate(detect.ignored_predicate))
        yield


OUTPUTS = ("graph.json", "GRAPH_REPORT.md", ".graphify_labels.json", ".graphify_labels.json.sig",
           "receiver_calls.json")


def _update(parent: Path, edit, monkeypatch, *, before: bool, decided: list | None = None) -> dict[str, bytes]:
    repo = parent / "orders_app"
    shutil.copytree(EXAMPLE, repo, ignore=IGNORE)
    real = watch._list_counts_differ

    def spy(a, b, *, dicts_only):
        decided.append(real(a, b, dicts_only=dicts_only))
        return decided[-1]

    with _as_before(monkeypatch) if before else monkeypatch.context() as m:
        index.build(repo, force=True)
        if not before:
            m.setattr(watch, "_list_counts_differ", spy)
        edit(repo)
        stats = index.build(repo, prune_missing=True)
    assert stats["ok"]
    out = {name: (index_dir(repo) / name).read_bytes() for name in OUTPUTS if (index_dir(repo) / name).exists()}
    sidecar = json.loads(out["receiver_calls.json"])
    assert sidecar["graph"].pop("mtime_ns")  # the graph file's time: the only value that differs by copy
    out["receiver_calls.json"] = json.dumps(sidecar, sort_keys=True).encode()
    return out


def _add_function(repo: Path) -> None:
    p = repo / "orders" / "pricing.py"
    p.write_bytes(p.read_bytes() + b"\n\ndef loyalty_bonus(points):\n    return points * 2\n")


def _body_only(repo: Path) -> None:
    p = repo / "orders" / "pricing.py"
    p.write_bytes(p.read_bytes() + b"\n# a comment: no node or edge changes\n")


def _delete_file(repo: Path) -> None:
    (repo / "orders" / "config.py").unlink()


def _add_doc_and_header(repo: Path) -> None:
    (repo / "docs" / "notes.txt").write_text("free text\n", encoding="utf-8")
    (repo / "orders" / "native.h").write_text("namespace n { class C { public: int f(); }; }\n",
                                              encoding="utf-8")


@pytest.mark.parametrize("edit, counts_differ", [(_add_function, True), (_body_only, False),
                                                  (_delete_file, True), (_add_doc_and_header, True)])
def test_an_update_writes_the_same_bytes_as_without_the_speed_ups(tmp_path, monkeypatch, edit, counts_differ):
    old = _update(tmp_path / "before", edit, monkeypatch, before=True)
    decided: list[bool] = []
    new = _update(tmp_path / "after", edit, monkeypatch, before=False, decided=decided)
    assert set(old) >= {"graph.json", "GRAPH_REPORT.md", "receiver_calls.json"}
    assert old == new
    assert decided and any(decided) is counts_differ  # the count check decided, or the full compare ran
