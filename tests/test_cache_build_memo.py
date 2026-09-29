"""The AST cache's per-build memos (local changes in ``project_index/cache.py``, entered by
``index._cache_build_memo`` for the length of ``index.build``) return what upstream's code returns.

Each replaced function is compared with a verbatim copy of the upstream code it replaces (the
``_upstream_*`` functions below, from cc71f33) on real cache entries: the upstream test fixtures
extracted into a temporary cache, re-anchored under several spellings of the path and the root.
"""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import copy  # noqa: E402
import hashlib  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import pickle  # noqa: E402
import shutil  # noqa: E402
import time  # noqa: E402
from contextlib import redirect_stderr, redirect_stdout  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import index  # noqa: E402
from verinoda.paths import graph_path, index_dir  # noqa: E402
from verinoda.project_index import cache  # noqa: E402
from verinoda.project_index.ids import normalize_id  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests_upstream" / "fixtures"
EXAMPLE = ROOT / "examples" / "orders_app"

# An ObjC pair whose extraction carries an objc_field_types table (keyed by class node id).
OBJC = {
    "objc/Greeter.h": "#import <Foundation/Foundation.h>\n@interface Greeter : NSObject\n- (void)greet;\n@end\n",
    "objc/Greeter.m": '#import "Greeter.h"\n@implementation Greeter\n- (void)greet { NSLog(@"hi"); }\n@end\n',
    "objc/Direct.h": ("#import <Foundation/Foundation.h>\n#import \"Greeter.h\"\n@interface Direct : NSObject\n"
                      "@property (nonatomic, strong) Greeter *greeter;\n- (void)run;\n@end\n"),
    "objc/Direct.m": '#import "Direct.h"\n@implementation Direct\n- (void)run { [self.greeter greet]; }\n@end\n',
}


# -- upstream code (cc71f33), kept verbatim as the reference ---------------------------------------------

def _upstream_portability_anchors(path, root):
    from verinoda.project_index.ids import normalize_id

    try:
        root_resolved = Path(root).resolve()
    except OSError:
        return [], "", [], ""
    try:
        path_resolved = Path(path).resolve()
    except (OSError, RuntimeError):
        path_resolved = Path(path)
    try:
        rel = os.path.relpath(path_resolved, root_resolved)
    except (ValueError, OSError):
        rel = ""
    from_given = cache._id_anchor(str(path), rel)
    from_resolved = cache._id_anchor(str(path_resolved), rel)
    id_restore = next(
        (a for a in (from_given, from_resolved, normalize_id(str(root_resolved))) if a), ""
    )
    root_id_forms = (normalize_id(str(root_resolved)),)
    if Path(root).is_absolute():
        root_id_forms += (normalize_id(str(root)),)
    id_anchors = sorted(
        {a for a in (from_given, from_resolved, *root_id_forms) if a},
        key=len, reverse=True,
    )
    path_anchors = sorted(
        {s for s in (str(root_resolved), str(root)) if Path(s).is_absolute()},
        key=len, reverse=True,
    )
    return id_anchors, id_restore, path_anchors, str(root_resolved)


def _upstream_absolutize_ids_in(payload, path, root):
    _, id_restore, _, path_restore = _upstream_portability_anchors(path, root)

    def restore(value: str) -> str:
        if not value.startswith(cache._ROOT_MARKER):
            return value
        rest = value[len(cache._ROOT_MARKER):]
        if not rest:
            return path_restore
        if rest[0] == "/":
            tail = rest[1:]
            return str(Path(path_restore) / tail) if tail else path_restore
        if rest[0] == "_":
            return (id_restore + rest) if id_restore else rest[1:]
        return value

    cache._rewrite_strings(payload, restore)
    cache._rewrite_id_keyed_table_keys(payload, restore)


def _upstream_relativize_ids_in(payload, path, root):
    id_anchors, _, path_anchors, _ = _upstream_portability_anchors(path, root)
    if not id_anchors and not path_anchors:
        return

    def anchor(value: str) -> str:
        for a in path_anchors:
            if value == a:
                return cache._ROOT_MARKER
            for sep in ("/", "\\"):
                if value.startswith(a + sep):
                    return cache._ROOT_MARKER + "/" + value[len(a) + 1:].replace("\\", "/")
        for a in id_anchors:
            if value.startswith(a + "_"):
                return cache._ROOT_MARKER + "_" + value[len(a) + 1:]
        return value

    cache._rewrite_strings(payload, anchor)
    cache._rewrite_id_keyed_table_keys(payload, anchor)


def _upstream_file_hash(path, root=Path("."), cache_root=None):
    p = cache._normalize_path(Path(path))
    root = cache._normalize_path(Path(root))
    if not p.is_file():
        raise IsADirectoryError(f"file_hash requires a file, got: {p}")
    cache._ensure_stat_index(root, cache_root=cache_root)
    resolved = p.resolve()
    abs_key = str(resolved)
    resolved_root = root.resolve()
    try:
        resolved_rel = resolved.relative_to(resolved_root)
    except ValueError:
        salt = resolved.as_posix().lower()
    else:
        walked = Path(os.path.abspath(p))
        walked_root = Path(os.path.abspath(root))
        try:
            walked_rel = walked.relative_to(walked_root)
        except ValueError:
            walked_rel = None
            for parent in walked.parents:
                try:
                    if parent.resolve() == resolved_root:
                        walked_rel = walked.relative_to(parent)
                        break
                except OSError:
                    continue
            if walked_rel is None:
                walked_rel = resolved_rel
        salt = walked_rel.as_posix().lower()
    if p.suffix.lower() in (".pdf", ".docx", ".xlsx", ".pptx"):
        from verinoda import doctext

        salt += f"\x00doctext-v{doctext.VERSION}"

    st = None
    try:
        st = p.stat()
        if cache._stat_sig_fresh(cache._stat_index.get(abs_key), st):
            hashes = cache._stat_index[abs_key].get("hashes")
            if isinstance(hashes, dict):
                cached = hashes.get(salt)
                if isinstance(cached, str):
                    return cached
    except OSError:
        pass

    observed_at_ns = time.time_ns()
    raw = p.read_bytes()
    content = cache._body_content(raw) if p.suffix.lower() == ".md" else raw
    h = hashlib.sha256()
    h.update(content)
    h.update(b"\x00")
    h.update(salt.encode())
    digest = h.hexdigest()

    if st is not None:
        entry = cache._stat_entry_for(abs_key, st, observed_at_ns)
        hashes = entry.get("hashes")
        if not isinstance(hashes, dict):
            hashes = {}
            entry["hashes"] = hashes
        hashes[salt] = digest
        entry.pop("hash", None)
        cache._stat_index_dirty = True
    return digest


def _upstream_load_ast(path, root, cache_root):
    """Upstream load_cached for kind "ast", with the upstream re-anchoring."""
    h = cache.file_hash(path, root, cache_root=cache_root)
    entry = cache.cache_dir(cache_root, "ast") / f"{h}.json"
    if not entry.exists():
        return None
    result = json.loads(entry.read_text(encoding="utf-8"))
    if isinstance(result, dict) and result.get("partial"):
        return None
    if isinstance(result, dict):
        cache._absolutize_source_files_in(result, root)
        _upstream_absolutize_ids_in(result, path, root)
    return result


# -- fixtures -------------------------------------------------------------------------------------------

def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)  # order-sensitive: key order must match too


@pytest.fixture
def isolated_stat_index(tmp_path, monkeypatch):
    """A stat index of the test's own, never flushed (its root is set, so no atexit flush registers)."""
    monkeypatch.setattr(cache, "_stat_index", {})
    monkeypatch.setattr(cache, "_stat_index_root", tmp_path / "stat-root")
    monkeypatch.setattr(cache, "_stat_index_anchor", tmp_path)
    monkeypatch.setattr(cache, "_stat_index_dirty", False)
    monkeypatch.setattr(cache, "_build_memo", None)
    monkeypatch.setattr(cache, "_build_depth", 0)
    monkeypatch.setattr(cache, "_restored_entries", {})


@pytest.fixture
def corpus(tmp_path, monkeypatch, isolated_stat_index):
    """The upstream fixtures (and an ObjC pair) extracted into a cache of their own; returns
    (corpus root, the files that have an AST cache entry)."""
    from verinoda.project_index import extract as X

    root = tmp_path / "corpus"
    shutil.copytree(FIXTURES, root / "fixtures", ignore=shutil.ignore_patterns("__pycache__"))
    for rel, text in OBJC.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    files = sorted(p for p in root.rglob("*") if p.is_file() and X._get_extractor(p) is not None
                   and p.suffix not in X._JS_CACHE_BYPASS_SUFFIXES)
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        X.extract(files, cache_root=root, root=root, parallel=False)
    monkeypatch.chdir(tmp_path)
    with_entry = [p for p in files if (cache.cache_dir(root, "ast") / f"{cache.file_hash(p, root, cache_root=root)}.json").exists()]
    assert len(with_entry) > 40
    return root, with_entry


def _spellings(p: Path, root: Path):
    """The same file and root as the pipeline or a caller may spell them."""
    rel_root = Path(os.path.relpath(root))
    paths = [p, str(p), Path(os.path.relpath(p)), str(p).replace("\\", "/")]
    roots = [root, str(root), rel_root, str(root) + os.sep, str(root).replace("\\", "/")]
    return paths, roots


# -- _portability_anchors / _relativize_ids_in / _absolutize_ids_in --------------------------------------

def test_anchors_are_upstream_s_in_and_out_of_a_build(corpus):
    root, files = corpus
    cases = []
    for p in files[::5] + [root / "missing.py", root.parent / "outside.py"]:
        paths, roots = _spellings(p, root)
        cases += [(a, r) for a in paths for r in roots]
    cases += [("rel/only.py", "rel"), ("a.py", "."), (Path("a.py"), Path(""))]
    for a, r in cases:
        ref = _upstream_portability_anchors(a, r)
        assert cache._portability_anchors(a, r) == ref, (a, r)
        assert cache._restore_anchors(a, r) == (ref[1], ref[3]), (a, r)
    with cache.build_memo():
        for _ in range(2):  # the second round is served from the build's memo
            for a, r in cases:
                ref = _upstream_portability_anchors(a, r)
                assert cache._portability_anchors(a, r) == ref, (a, r)
                assert cache._restore_anchors(a, r) == (ref[1], ref[3]), (a, r)


def _entries(root: Path, files: list[Path]):
    for p in files:
        e = cache.cache_dir(root, "ast") / f"{cache.file_hash(p, root, cache_root=root)}.json"
        yield p, json.loads(e.read_text(encoding="utf-8"))


def test_absolutize_ids_in_is_upstream_s_on_every_real_entry(corpus):
    root, files = corpus
    entries = list(_entries(root, files))
    marked = sum(cache._ROOT_MARKER in _dump(e) for _, e in entries)
    assert marked > 30  # the entries do carry the marker
    assert any((e.get("objc_field_types") or {}).get("tables") for _, e in entries)  # an id-keyed table
    for scoped in (False, True):
        ctx = cache.build_memo() if scoped else None
        if ctx:
            ctx.__enter__()
        try:
            for p, raw in entries:
                paths, roots = _spellings(p, root)
                for a in paths:
                    for r in roots:
                        ref, new = copy.deepcopy(raw), copy.deepcopy(raw)
                        _upstream_absolutize_ids_in(ref, a, r)
                        cache._absolutize_ids_in(new, a, r)
                        assert _dump(new) == _dump(ref), (p, a, r)
        finally:
            if ctx:
                ctx.__exit__(None, None, None)


def test_absolutize_ids_in_odd_values_as_upstream(corpus):
    root, _ = corpus
    m = cache._ROOT_MARKER

    class Sub(str):
        pass

    payload = {
        "nodes": [{"id": m + "_a_b", "label": m, "path": m + "/x\\y", "n": 1, "f": 1.5, "t": True, "z": None},
                  {"id": m + "x_not_restored", "k": [m + "_a", [m + "_a", {"deep": m + "/"}], Sub(m + "_sub")]}],
        m + "_key_is_left": m + "_value",
        "edges": [{"source": m + "_a", "target": m + "_a"}] * 3,
        "objc_field_types": {"tables": {m + "_cls": {"f": m + "_t"}, "plain": {}}},
        "list_of_str": [m + "_a", "plain", m, m + "/", m + "/q"],
    }
    for a in (root / "fixtures" / "sample.py", "fixtures/sample.py"):
        for r in (root, str(root), Path(os.path.relpath(root))):
            ref, new = copy.deepcopy(payload), copy.deepcopy(payload)
            _upstream_absolutize_ids_in(ref, a, r)
            cache._absolutize_ids_in(new, a, r)
            assert _dump(new) == _dump(ref)
            assert type(new["nodes"][1]["k"][2]) is type(ref["nodes"][1]["k"][2])


def test_relativize_ids_in_is_upstream_s(corpus):
    """_relativize_ids_in reads the whole anchor tuple: the same stored entry either way."""
    from verinoda.project_index import extract as X

    root, files = corpus
    for p in files[::3]:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            res = X._safe_extract(X._get_extractor(p), p)
        for r in (root, str(root)):
            ref, new = copy.deepcopy(res), copy.deepcopy(res)
            cache._relativize_source_files_in(ref, r)
            cache._relativize_source_files_in(new, r)
            _upstream_relativize_ids_in(ref, p, r)
            with cache.build_memo():
                cache._relativize_ids_in(new, p, r)
            assert _dump(new) == _dump(ref), p


# -- file_hash -------------------------------------------------------------------------------------------

def _stat_index_without_times():
    return {k: {kk: vv for kk, vv in v.items() if kk != "indexed_at_ns"} for k, v in cache._stat_index.items()}


def test_file_hash_is_upstream_s_with_its_stat_index(corpus, monkeypatch, tmp_path):
    root, files = corpus
    own = sorted((ROOT / "verinoda").glob("*.py"))[:40] + [ROOT / "README.md"]
    cases = []
    for p in files[::2]:
        paths, roots = _spellings(p, root)
        cases += [(a, r) for a in paths[:3] for r in roots[:3]]
    cases += [(p, ROOT) for p in own] + [(p, root) for p in own[:5]]  # the last: outside the root
    monkeypatch.setenv("GRAPHIFY_MTIME_GRANULARITY_MS", "0")  # the second call takes the fast path
    runs = {}
    for name, fn, scoped in (("upstream", _upstream_file_hash, False), ("new", cache.file_hash, False),
                             ("new in a build", cache.file_hash, True)):
        monkeypatch.setattr(cache, "_stat_index", {})
        ctx = cache.build_memo() if scoped else None
        if ctx:
            ctx.__enter__()
        try:
            got = [fn(a, r, cache_root=tmp_path) for _ in range(2) for a, r in cases]
        finally:
            if ctx:
                ctx.__exit__(None, None, None)
        runs[name] = (got, _stat_index_without_times())
    assert runs["new"] == runs["upstream"]
    assert runs["new in a build"] == runs["upstream"]


def test_file_hash_refuses_what_is_not_a_file_as_upstream(corpus):
    root, _ = corpus
    for bad in (root / "fixtures", root / "missing.py"):
        for fn in (_upstream_file_hash, cache.file_hash):
            with pytest.raises(IsADirectoryError):
                fn(bad, root)
        with cache.build_memo(), pytest.raises(IsADirectoryError):
            cache.file_hash(bad, root)


# -- load_cached during a build: the cache directory, one stat, the kept entries --------------------------

class _CountingPickle:
    def __init__(self):
        self.loads_calls = self.dumps_calls = 0

    def loads(self, b):
        self.loads_calls += 1
        return pickle.loads(b)

    def dumps(self, o, protocol=None):
        self.dumps_calls += 1
        return pickle.dumps(o, protocol=protocol)

    HIGHEST_PROTOCOL = pickle.HIGHEST_PROTOCOL


def _build_round(root, files, spellings=True):
    got = []
    with cache.build_memo():
        for p in files:
            paths, roots = _spellings(p, root) if spellings else ([p], [root])
            for a in paths[:3]:
                for r in roots[:3]:
                    got.append((a, r, cache.load_cached(a, r, cache_root=root)))
    return got


def _mutate(results):
    for *_, g in results:  # extract mutates what it gets
        if isinstance(g, dict):
            for n in g.get("nodes", []):
                n["id"] = "mutated"
            g.setdefault("edges", []).append({"added": True})


def test_load_cached_in_builds_is_upstream_s_and_kept_entries_are_fresh_objects(corpus, monkeypatch):
    root, files = corpus
    monkeypatch.setenv("GRAPHIFY_MTIME_GRANULARITY_MS", "0")
    spy = _CountingPickle()
    monkeypatch.setattr(cache, "pickle", spy)
    monkeypatch.setattr(cache, "_builds_done", 0)
    for rnd in range(3):
        got = _build_round(root, files)
        for a, r, g in got:
            assert g is not None
            assert _dump(g) == _dump(_upstream_load_ast(a, r, root)), (rnd, a, r)
        _mutate(got)
        if rnd == 0:  # the process's first build keeps nothing
            assert spy.dumps_calls == 0 and not cache._restored_entries
        else:
            assert cache._restored_entries
    assert spy.loads_calls >= len(files)  # the third build was served from memory
    # outside a build nothing is kept or served
    before = dict(cache._restored_entries)
    calls = spy.loads_calls
    for p in files[:5]:
        assert _dump(cache.load_cached(p, root, cache_root=root)) == _dump(_upstream_load_ast(p, root, root))
    assert spy.loads_calls == calls and cache._restored_entries == before


def test_a_rewritten_or_deleted_entry_is_not_served_from_memory(corpus, monkeypatch):
    root, files = corpus
    monkeypatch.setenv("GRAPHIFY_MTIME_GRANULARITY_MS", "0")
    monkeypatch.setattr(cache, "_builds_done", 1)
    _build_round(root, files, spellings=False)  # kept
    target, gone = files[0], files[1]
    d = cache.cache_dir(root, "ast")
    e_target = d / f"{cache.file_hash(target, root, cache_root=root)}.json"
    e_gone = d / f"{cache.file_hash(gone, root, cache_root=root)}.json"
    data = json.loads(e_target.read_text(encoding="utf-8"))
    data["nodes"].append({"id": cache._ROOT_MARKER + "_rewritten", "label": "rewritten"})
    tmp = e_target.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, e_target)  # a new file: new inode, new mtime
    e_gone.unlink()
    got = dict((a, g) for a, _, g in _build_round(root, files, spellings=False))
    assert got[gone] is None
    assert any(n.get("label") == "rewritten" for n in got[target]["nodes"])
    assert _dump(got[target]) == _dump(_upstream_load_ast(target, root, root))


def test_an_entry_written_within_the_mtime_granularity_is_not_kept(corpus, monkeypatch):
    root, files = corpus
    monkeypatch.setenv("GRAPHIFY_MTIME_GRANULARITY_MS", str(10 * 365 * 24 * 3600 * 1000))  # every entry is "new"
    monkeypatch.setattr(cache, "_builds_done", 1)
    _build_round(root, files, spellings=False)
    assert not cache._restored_entries


def test_the_cache_directory_is_made_again_on_a_miss_as_upstream_makes_it(corpus, monkeypatch):
    root, files = corpus
    d = cache.cache_dir(root, "ast")
    with cache.build_memo():
        assert cache.load_cached(files[0], root, cache_root=root) is not None  # memoises the directory
        shutil.rmtree(d)
        assert cache.load_cached(files[1], root, cache_root=root) is None
        assert d.is_dir()  # upstream's cache_dir() makes it on every lookup


# -- a whole build, twice in one process -----------------------------------------------------------------

def _copy_example(dst: Path) -> Path:
    shutil.copytree(EXAMPLE, dst, ignore=shutil.ignore_patterns(".verinoda", "__pycache__", "*.pyc",
                                                                ".pytest_cache", "*.db"))
    return dst


def _outputs(repo: Path) -> dict[str, str]:
    """The index files a build writes (not the caches), with what differs between two copies of one
    project taken out: the copy's absolute path and its id form (in the Python sidecars), the detect manifest
    (each file's mtime and when it was seen) and the graph file's mtime in the receiver sidecar."""
    d = index_dir(repo)
    out = {}
    for p in sorted(d.rglob("*")):
        rel = p.relative_to(d)
        if not p.is_file() or "cache" in rel.parts or p.suffix not in (".json", ".md") or p.name == "manifest.json":
            continue
        text = p.read_text(encoding="utf-8")
        for form in (json.dumps(str(repo))[1:-1], str(repo), repo.as_posix()):
            text = text.replace(form, "<ROOT>")
        text = text.replace(normalize_id(str(repo)), "<ROOT-ID>")  # ids the Python sidecar mints
        if p.name == "receiver_calls.json":
            data = json.loads(text)
            data.get("graph", {}).pop("mtime_ns", None)
            text = json.dumps(data)
        out[rel.as_posix()] = text
    return out


class _no_memo:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_builds_served_from_the_kept_entries_write_the_same_files(tmp_path, monkeypatch):
    """Three builds in one process (the later ones served from memory, with a file changed before the
    last) write what builds without the cache memos write."""
    monkeypatch.setenv("GRAPHIFY_MTIME_GRANULARITY_MS", "0")

    def edit(repo):
        svc = repo / "orders" / "service.py"
        svc.write_bytes(svc.read_bytes() + b"\n\ndef added_later():\n    return fetch_order\n")

    monkeypatch.setattr(cache, "_restored_entries", {})
    spy = _CountingPickle()
    monkeypatch.setattr(cache, "pickle", spy)
    a = _copy_example(tmp_path / "a" / "orders_app")  # one name: the report names the folder
    for i in range(3):
        if i == 2:
            edit(a)
        index.build(a)
    with_memo = _outputs(a)
    assert cache._restored_entries and spy.loads_calls > 0  # a build was served from memory

    b = _copy_example(tmp_path / "b" / "orders_app")
    monkeypatch.setattr(index, "_cache_build_memo", _no_memo)
    for i in range(3):
        if i == 2:
            edit(b)
        index.build(b)
    without = _outputs(b)
    assert with_memo.keys() == without.keys() and "graph.json" in with_memo
    assert [k for k in with_memo if with_memo[k] != without[k]] == []
    assert graph_path(a).read_bytes() == graph_path(b).read_bytes()
