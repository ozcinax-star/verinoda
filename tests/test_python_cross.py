"""The kept pruned trees give the cross-file import pass exactly what the upstream parse gives it."""

import contextlib
import json
import pickle
import shutil
import sys
from pathlib import Path

import pytest

import verinoda.python_cross as pc
from verinoda import index
from verinoda.paths import graph_path
from verinoda.project_index import extract as ex
from verinoda.project_index.extractors import resolution as res
from verinoda.python_cross import FILE, PINNED, _source_digest, python_cross_cache

PARSE = res._parse_python_tree

FILES = {
    "pkg/__init__.py": "from .models import Model as M\nfrom . import util\n",
    "pkg/models.py": (
        "class Base:\n    pass\n\n\nclass Model(Base):\n    def save(self):\n        return Response()\n\n\n"
        "class Response:\n    pass\n\n\ndef make():\n    return Model()\n"
    ),
    "pkg/util.py": (
        "from .models import Model, Base, make\nfrom .models import (\n    Response as R,\n    Missing,\n)\n\n"
        "class Helper(Base):\n    kind: Model = None\n\n    def run(self) -> R:\n        m = Model()\n        return make()\n\n"
        "def top():\n    def inner():\n        return Model\n    return inner, R\n\n"
        "@decorate(Model)\ndef decorated(x=lambda: make()):\n    return Base\n\n"
        "Model.registry = []\n"
    ),
    "pkg/sub/__init__.py": "",
    "pkg/sub/deep.py": (
        "from ..models import Response\nfrom ...outside import Thing\nfrom . import nothing\n\n"
        "class Handler:\n    def handle(self) -> Response:\n        return Response()\n"
    ),
    "app.py": (
        "from pkg.models import Model\nfrom models import Base\nfrom pkg import util\nfrom unknown import Response\n"
        "from pkg.models import *\nimport pkg.models as pm\n"
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from pkg.util import Helper\n\n"
        "class App(Model, Base):\n    helper: 'Helper'\n\n    def go(self):\n        from pkg.models import make as build\n"
        "        return build(), Response, util.top()\n\n"
        "class Model:\n    pass\n\n"
        "def _hidden():\n    def Model():\n        return Base\n    return Model\n\n"
        "try:\n    def fallback(h: Helper):\n        return h\nexcept Exception:\n    pass\n"
    ),
    "types_user.py": (
        "from pkg.models import Model, Response\n\n\nclass Sub(Model):\n    def f(self, r: Response) -> Response:\n        return r\n"
    ),
    "unicode_names.py": "from pkg.models import Model as Modèl\n\nclass Çalışan(Modèl):\n    def f(self):\n        return Modèl\n",
    "broken.py": "from pkg.models import Model\ndef oops(:\n    Model(\n",
    "notes.txt": "not python\n",
}


def _write(root: Path, files: dict) -> None:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if text is None:
            p.unlink(missing_ok=True)
        else:
            p.write_text(text, encoding="utf-8")


def _capture(root: Path, cache: Path) -> bytes:
    """The arguments the real extractor hands the cross-file pass for this project, pickled."""
    paths = sorted(p for p in root.rglob("*") if p.is_file() and ".verinoda" not in p.parts)
    got = []
    real = ex._resolve_cross_file_imports

    def grab(per_file, paths_, all_nodes=None, all_edges=None):
        got.append(pickle.dumps((per_file, paths_, all_nodes, all_edges)))
        return real(per_file, paths_, all_nodes, all_edges)

    ex._resolve_cross_file_imports = grab
    try:
        ex.extract(paths, cache_root=cache, root=root, parallel=False)
    finally:
        ex._resolve_cross_file_imports = real
    assert len(got) == 1
    return got[0]


def _run(fn, blob: bytes) -> str:
    per_file, paths, all_nodes, all_edges = pickle.loads(blob)
    res._parse_python_tree_cached.cache_clear()  # the upstream parse memo, as a new process would start
    try:
        out = fn(per_file, paths, all_nodes, all_edges)
    except (AttributeError, RecursionError, TypeError) as exc:  # what a pass that fails part-way leaves counts too
        out = type(exc).__name__
    # element order and dict key order both count
    return json.dumps([out, all_nodes, all_edges], default=str)


def _cached(index_dir: Path, blob: bytes, **kw):
    limit = sys.getrecursionlimit()
    with python_cross_cache(index_dir, **kw) as c:
        assert c.active
        got = _run(ex._resolve_cross_file_imports, blob)
        assert res._parse_python_tree is PARSE and sys.getrecursionlimit() == limit  # put back after the pass
    assert ex._resolve_cross_file_imports is res._resolve_cross_file_imports is c.real  # and after the build
    return got, c


def _project(tmp_path: Path, extra: dict | None = None) -> tuple[Path, bytes, str]:
    root = tmp_path / "proj"
    _write(root, {**FILES, **(extra or {})})
    blob = _capture(root, tmp_path / "xcache")
    return root, blob, _run(res._resolve_cross_file_imports, blob)


def test_the_pin_is_the_vendored_function():
    # Fails when the vendored pass changes. Check that its walk still reads only what python_cross
    # keeps (the node types and fields in _prune/_build), then update PINNED.
    assert _source_digest(res._resolve_cross_file_imports) == PINNED


def test_cold_warm_and_edited_files_give_the_upstream_result(tmp_path):
    root, blob, want = _project(tmp_path)
    parsed = json.loads(want)
    assert any(e["relation"] == "uses" for e in parsed[0])  # the project exercises the pass
    index_dir = tmp_path / "index"

    got, cold = _cached(index_dir, blob)
    assert got == want
    assert cold.misses == 7 and cold.hits == 0 and (index_dir / FILE).is_file()
    got, warm = _cached(index_dir, blob)
    assert got == want
    assert warm.misses == 0 and warm.hits == 7  # a file with a syntax error parses too (with error nodes)

    # an edit, a new file that other files' imports now reach, and a removed file
    _write(root, {
        "pkg/util.py": FILES["pkg/util.py"].replace("def top():", "def top2():\n    return Base\n\ndef top():"),
        "pkg/extra.py": "class Extra:\n    pass\n",
        "app.py": FILES["app.py"] + "\nfrom pkg.extra import Extra\n\ndef use_extra():\n    return Extra\n",
        "broken.py": None,
    })
    blob2 = _capture(root, tmp_path / "xcache")
    want2 = _run(res._resolve_cross_file_imports, blob2)
    assert want2 != want
    got2, edited = _cached(index_dir, blob2)
    assert got2 == want2
    assert edited.misses == 3 and edited.hits == 4  # util.py, app.py, extra.py are parsed
    kept = json.loads((index_dir / FILE).read_text(encoding="utf-8"))["files"]
    assert not any(k.endswith("broken.py") for k in kept)  # only this build's files are kept

    # the old inputs against the new files on disk (nodes that no longer match): still as upstream
    assert _cached(index_dir, blob)[0] == _run(res._resolve_cross_file_imports, blob)


def test_a_new_module_changes_where_an_import_points_without_parsing_the_importer(tmp_path):
    late = "from pkg.later import Thing\n\nclass User:\n    def f(self):\n        return Thing()\n"
    root, blob, want = _project(tmp_path, {"late.py": late})
    assert _cached(tmp_path / "index", blob)[0] == want
    assert not any(e["relation"] == "uses" and e["source_file"].endswith("late.py") for e in json.loads(want)[0])

    _write(root, {"pkg/later.py": "class Thing:\n    pass\n"})
    blob2 = _capture(root, tmp_path / "xcache")
    want2 = _run(res._resolve_cross_file_imports, blob2)
    got2, again = _cached(tmp_path / "index", blob2)
    assert got2 == want2
    assert again.misses == 1  # only the new module; late.py's kept tree now meets a target
    assert any(e["relation"] == "uses" and e["source_file"].endswith("late.py") for e in json.loads(got2)[0])


def test_a_changed_vendored_function_runs_as_upstream(tmp_path):
    _, blob, want = _project(tmp_path)
    with python_cross_cache(tmp_path / "index", pinned="not-this-one") as c:
        assert not c.active
        assert ex._resolve_cross_file_imports is res._resolve_cross_file_imports is c.real
        assert _run(ex._resolve_cross_file_imports, blob) == want
    assert not (tmp_path / "index" / FILE).exists()


def test_a_cache_from_other_code_is_not_used(tmp_path):
    _, blob, want = _project(tmp_path)
    _cached(tmp_path / "index", blob)
    kept = tmp_path / "index" / FILE
    kept.write_text(kept.read_text(encoding="utf-8").replace('"stamp":"', '"stamp":"other-'), encoding="utf-8")
    got, again = _cached(tmp_path / "index", blob)
    assert got == want and again.hits == 0


def test_a_damaged_entry_is_parsed_again(tmp_path):
    _, blob, want = _project(tmp_path)
    _cached(tmp_path / "index", blob)
    kept = tmp_path / "index" / FILE
    data = json.loads(kept.read_text(encoding="utf-8"))
    victims = [k for k in data["files"] if k.endswith(("util.py", "deep.py", "types_user.py", "app.py"))]
    assert len(victims) == 4
    data["files"][victims[0]]["t"] = ["R", "module", [["zz"]]]
    data["files"][victims[1]]["t"] = ["R", "module", [["i", 5, 3]]]
    del data["files"][victims[2]]["d"]
    data["files"][victims[3]]["t"] = ["R", "module", [["f", "go", [["i", "Model", "3"]]]]]
    kept.write_text(json.dumps(data), encoding="utf-8")
    got, c = _cached(tmp_path / "index", blob)
    assert got == want and c.misses == 4


@pytest.mark.parametrize("text", [
    lambda stamp: json.dumps({"stamp": stamp, "files": None}),
    lambda stamp: json.dumps({"stamp": stamp, "files": []}),
    lambda stamp: json.dumps({"stamp": stamp, "files": "x"}),
    lambda stamp: json.dumps([stamp]),
    lambda stamp: "[" * 100_000 + "]" * 100_000,  # json.loads raises RecursionError
], ids=["files-null", "files-list", "files-string", "not-an-object", "too-deep"])
def test_a_kept_file_of_another_shape_counts_as_empty(tmp_path, text):
    _, blob, want = _project(tmp_path)
    cold = _cached(tmp_path / "index", blob)[1]
    kept = tmp_path / "index" / FILE
    kept.write_text(text(json.loads(kept.read_text(encoding="utf-8"))["stamp"]), encoding="utf-8")
    got, c = _cached(tmp_path / "index", blob)
    assert got == want and c.misses == cold.misses and c.hits == 0  # every file parsed again
    assert isinstance(json.loads(kept.read_text(encoding="utf-8"))["files"], dict)  # and the file rewritten
    assert _cached(tmp_path / "index", blob)[1].hits == cold.misses


class _FullDisk:
    def __init__(self, f):
        self.f = f

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.f.close()

    def write(self, text):
        raise OSError(28, "No space left on device")


def _refused(src, dst):  # os.replace on Windows while another process has the file open
    raise PermissionError(13, "The process cannot access the file", str(dst))


@pytest.mark.parametrize("failing", ["write", "replace"])
def test_a_save_that_fails_leaves_the_old_file_and_no_temp_file(tmp_path, monkeypatch, failing):
    root, blob, _ = _project(tmp_path)
    _cached(tmp_path / "index", blob)
    before = (tmp_path / "index" / FILE).read_bytes()
    _write(root, {"pkg/extra.py": "class Extra:\n    pass\n"})
    blob2 = _capture(root, tmp_path / "xcache")
    want2 = _run(res._resolve_cross_file_imports, blob2)
    with monkeypatch.context() as m:
        if failing == "write":
            real_fdopen = pc.os.fdopen
            m.setattr(pc.os, "fdopen", lambda fd, *a, **kw: _FullDisk(real_fdopen(fd, *a, **kw)))
        else:
            m.setattr(pc.os, "replace", _refused)
        got, c = _cached(tmp_path / "index", blob2)
    assert got == want2 and c.misses == 1
    assert [p.name for p in (tmp_path / "index").iterdir()] == [FILE]
    assert (tmp_path / "index" / FILE).read_bytes() == before
    assert _cached(tmp_path / "index", blob2)[1].misses == 1  # the next build saves it
    assert _cached(tmp_path / "index", blob2)[1].misses == 0


def test_deep_files_get_their_full_tree_and_the_same_outcome(tmp_path):
    for depth in (pc.DEEP + 50, 1100, 3000):
        sub = tmp_path / str(depth)
        nested = "from pkg.models import Model\n\ndef f():\n    return " + "(" * depth + "Model" + ")" * depth + "\n"
        _, blob, want = _project(sub, {"nested.py": nested})
        for _ in range(2):  # cold, then warm
            got, c = _cached(sub / "index", blob)
            assert got == want and c.deep == 1


def test_the_recursion_limit_is_met_where_upstream_meets_it(tmp_path):
    # the upstream walk is recursive: with the kept trees, a deep file must overflow at exactly the
    # same limit as without them (the wrapper's own frame is made up for)
    nested = "from pkg.models import Model\n\ndef f():\n    return " + "(" * 1100 + "Model" + ")" * 1100 + "\n"
    _, blob, _ = _project(tmp_path, {"nested.py": nested})
    old = sys.getrecursionlimit()

    def at(limit, cached=False):  # the pass is called from the same depth either way
        with python_cross_cache(tmp_path / f"index{limit}") if cached else contextlib.nullcontext():
            sys.setrecursionlimit(limit)
            try:
                return _run(ex._resolve_cross_file_imports, blob)
            finally:
                sys.setrecursionlimit(old)

    lo, hi = 1100, 4000  # the lowest limit at which the upstream pass finishes
    assert json.loads(at(lo))[0] == "RecursionError"
    assert json.loads(at(hi))[0] != "RecursionError"
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if json.loads(at(mid))[0] == "RecursionError":
            lo = mid
        else:
            hi = mid
    for limit in (lo, hi):
        want = at(limit)
        assert at(limit, cached=True) == want  # cold
        assert at(limit, cached=True) == want  # warm


def test_a_low_recursion_limit_gets_full_trees(tmp_path):
    _, blob, want = _project(tmp_path)
    old = sys.getrecursionlimit()
    sys.setrecursionlimit(pc._frames() + pc.DEEP + 60)  # no room for the pruning walk
    try:
        got, c = _cached(tmp_path / "index", blob)
    finally:
        sys.setrecursionlimit(old)
    assert got == want and c.deep == 7 and not (tmp_path / "index" / FILE).exists()


def test_a_pass_that_fails_part_way_leaves_what_upstream_leaves(tmp_path):
    _, blob, _ = _project(tmp_path)
    _cached(tmp_path / "index", blob)
    before = set(json.loads((tmp_path / "index" / FILE).read_text(encoding="utf-8"))["files"])
    per_file, paths, all_nodes, all_edges = pickle.loads(blob)
    # an edge the repoint loop reaches after other files were done (its file matches, its relation is
    # unhashable): upstream stops there with a TypeError, some edges already repointed
    late = next(str(p) for p in paths if p.name == "types_user.py")
    all_edges.insert(len(all_edges) // 2, {"source": "a", "target": "b", "relation": ["unhashable"], "source_file": late})
    odd = pickle.dumps((per_file, paths, all_nodes, all_edges))
    want = _run(res._resolve_cross_file_imports, odd)
    assert json.loads(want)[0] == "TypeError"
    got, c = _cached(tmp_path / "index", odd)
    assert got == want and c.hits > 0
    after = set(json.loads((tmp_path / "index" / FILE).read_text(encoding="utf-8"))["files"])
    assert after == before  # the files the failed pass did not reach are still kept


def test_the_repoint_loop_is_given_only_the_edges_it_can_change(tmp_path, monkeypatch):
    _, blob, want = _project(tmp_path)
    _, _, all_nodes, all_edges = pickle.loads(blob)
    assert len(json.loads(want)[1]) < len(all_nodes)  # the pass repoints edges and drops stubs here
    given = []
    real = pc._typed_edges

    def typed_edges(edges):
        given.append(real(edges))
        return given[-1]

    monkeypatch.setattr(pc, "_typed_edges", typed_edges)
    assert _cached(tmp_path / "index", blob)[0] == want
    assert given[0] is not None and 0 < len(given[0]) < len(all_edges)
    assert all(e["relation"] in pc._TYPE_REPOINT_RELATIONS for e in given[0])


def test_a_repointed_stub_another_edge_still_names_is_kept(tmp_path):
    _, blob, first = _project(tmp_path)
    per_file, paths, all_nodes, all_edges = pickle.loads(blob)
    kept_ids = {n["id"] for n in json.loads(first)[1]}
    dropped = [n["id"] for n in all_nodes if n["id"] not in kept_ids]
    assert dropped  # stubs the pass repointed edges away from and removed
    # an edge the repoint loop never changes (not a type reference) still names one: upstream keeps it
    all_edges.append({"source": all_nodes[0]["id"], "target": dropped[0], "relation": "calls",
                      "confidence": "EXTRACTED", "source_file": "elsewhere.py"})
    blob = pickle.dumps((per_file, paths, all_nodes, all_edges))
    want = _run(res._resolve_cross_file_imports, blob)
    assert dropped[0] in {n["id"] for n in json.loads(want)[1]}
    assert _cached(tmp_path / "index", blob)[0] == want  # cold
    assert _cached(tmp_path / "index", blob)[0] == want  # warm


@pytest.mark.parametrize("stubs_named", [False, True], ids=["stubs-dropped", "stubs-kept"])
@pytest.mark.parametrize("odd", [
    {"relation": "calls", "source_file": ["unhashable"]},  # the repoint loop fails on it
    {"relation": "calls", "target": ["unhashable"]},  # the prune after the loop fails on it
    {"relation": "calls", "source": {"un": "hashable"}},
    ("not", "a", "dict"),
], ids=["source_file", "target", "source", "not-a-dict"])
def test_an_edge_upstream_fails_on_fails_the_same_way(tmp_path, odd, stubs_named):
    _, blob, first = _project(tmp_path)
    per_file, paths, all_nodes, all_edges = pickle.loads(blob)
    if stubs_named:
        # type references no file's imports resolve name every stub the pass repoints edges away from,
        # so none is dropped (the prune still reads every edge)
        kept_ids = {n["id"] for n in json.loads(first)[1]}
        for stub in [n["id"] for n in all_nodes if n["id"] not in kept_ids]:
            all_edges.append({"source": all_nodes[0]["id"], "target": stub, "relation": "references",
                              "source_file": "elsewhere.py"})
    if isinstance(odd, dict):
        late = next(str(p) for p in paths if p.name == "types_user.py")
        odd = {"source": "a", "target": "b", "source_file": late, **odd}
    all_edges.insert(len(all_edges) // 2, odd)
    blob = pickle.dumps((per_file, paths, all_nodes, all_edges))
    want = _run(res._resolve_cross_file_imports, blob)
    assert json.loads(want)[0] in ("TypeError", "AttributeError")
    assert _cached(tmp_path / "index", blob)[0] == want  # cold
    assert _cached(tmp_path / "index", blob)[0] == want  # warm


def test_a_build_gives_the_same_graph_with_and_without_the_kept_trees(tmp_path, monkeypatch):
    # a first build and a rebuild over an existing graph differ upstream, so like is compared with like
    repo = tmp_path / "repo"
    _write(repo, FILES)
    kept = repo / ".verinoda" / "index" / FILE
    edited = FILES["pkg/util.py"] + "\ndef later():\n    return Model\n"

    def builds(keep: bytes | None = None) -> list[bytes]:
        shutil.rmtree(repo / ".verinoda", ignore_errors=True)
        if keep is not None:
            kept.parent.mkdir(parents=True)
            kept.write_bytes(keep)
        _write(repo, {"pkg/util.py": FILES["pkg/util.py"]})
        out = []
        for i in range(3):
            if i == 2:
                _write(repo, {"pkg/util.py": edited})
            assert index.build(repo, force=True)["ok"]
            out.append(graph_path(repo).read_bytes())
        return out

    with monkeypatch.context() as m:
        m.setattr(pc, "PINNED", "not-this-one")
        without = builds()
    assert not kept.exists()
    real_build, served = pc._build, []
    monkeypatch.setattr(pc, "_build", lambda t: served.append(1) or real_build(t))
    assert builds() == without  # cold, warm, after an edit
    assert served  # the builds did walk kept trees
    trees = kept.read_bytes()
    assert b'"t":["R"' in trees
    assert builds(keep=trees) == without  # warm from the first build on
    # a kept file of another shape with the right stamp: parsed again, not a pass that fails
    odd = json.dumps({"stamp": json.loads(trees)["stamp"], "files": None}).encode()
    assert builds(keep=odd) == without
