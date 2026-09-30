"""The work ``verinoda update`` no longer repeats gives what repeating it gave.

Each memo or reuse is compared with the code it replaced (kept here verbatim as the reference) on real or
generated inputs: case_ids' file keys, python_facts' import resolution, the deleted-file check, the snapshot's
file listing handed to the search index and the lexicon, and the receiver sidecar built from the graph data
the build already holds, reusing the snapshot's stat-cached hashes.
"""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import copy  # noqa: E402
import json  # noqa: E402
import shutil  # noqa: E402
import sqlite3  # noqa: E402
import subprocess  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import case_ids, index, lexicon, search_index, snapshot, workflow  # noqa: E402
from verinoda.paths import graph_path, index_dir  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "orders_app"
OWN_GRAPH = ROOT / ".verinoda" / "index" / "graph.json"   # a checkout's own index, when it has one


# -- case_ids: one _file_key per spelling -------------------------------------------------------------

def _plans_before(nodes: list, root: Path | None):
    """``case_ids._plans`` as it was before the file keys were memoised (verbatim)."""
    _file_key, _bare, _line, _new_id, _digest = (case_ids._file_key, case_ids._bare, case_ids._line,
                                                 case_ids._new_id, case_ids._digest)
    _Plan, _Member, _SPLIT_ID = case_ids._Plan, case_ids._Member, case_ids._SPLIT_ID
    groups: dict[str, list[dict]] = {}
    owner: dict[str, tuple[str, str]] = {}
    for n in nodes:
        if isinstance(n, dict) and isinstance(n.get("id"), str) and n.get("source_file"):
            groups.setdefault(n["id"], []).append(n)
            owner.setdefault(n["id"], (_file_key(n["source_file"], root), _bare(n.get("label"))))
    plans: dict = {}
    split_now: set[str] = set()
    for nid, group in groups.items():
        if len(group) < 2:
            continue
        files = {_file_key(n["source_file"], root) for n in group}
        if len(files) != 1 or Path(str(group[0]["source_file"])).suffix.lower() not in case_ids.CASE_SENSITIVE_SUFFIXES:
            continue
        first_line: dict = {}
        for n in group:
            name = _bare(n.get("label"))
            ln = _line(n)
            if name not in first_line or (ln is not None and (first_line[name] is None or ln < first_line[name])):
                first_line[name] = ln
        if len(first_line) < 2 or len({k.casefold() for k in first_line}) != 1:
            continue
        file = files.pop()
        names = sorted(first_line)
        owner[nid] = (file, names[0])
        members = [_Member(names[0], first_line[names[0]], nid)]
        for name in names[1:]:
            new = _new_id(nid, name, file, owner)
            owner[new] = (file, name)
            members.append(_Member(name, first_line[name], new))
        plans[nid] = _Plan(file, members)
        split_now.add(nid)
        by_name = {m.name: m.nid for m in members}
        for n in group:
            n["id"] = by_name[_bare(n.get("label"))]
    for nid, group in groups.items():
        m = _SPLIT_ID.match(nid)
        base = groups.get(m.group(1)) if m else None
        if not base or m.group(1) in plans and nid in {x.nid for x in plans[m.group(1)].members}:
            continue
        file, name = owner[nid]
        bfile, bname = owner[m.group(1)]
        if (file != bfile or name == bname or name.casefold() != bname.casefold()
                or not _digest(name).startswith(m.group(2))):
            continue
        plan = plans.setdefault(m.group(1), _Plan(file, [_Member(bname, _line(base[0]), m.group(1))]))
        plan.members.append(_Member(name, _line(group[0]), nid))
    return plans, split_now


def _generated_nodes(root: Path) -> list[dict]:
    """Collisions within a file (spelled relative, absolute, with ``..`` and backslashes), across files,
    in a case-insensitive language, splits an earlier build left, and nodes without a file."""
    nodes = []
    for i in range(40):
        f = f"src/mod{i % 7}.ts"
        spellings = [f, str(root / f), f"src/../{f}", f.replace("/", "\\")]
        nodes.append({"id": f"mod{i}_orderservice", "label": "OrderService", "source_file": spellings[i % 4],
                      "source_location": f"L{i + 1}"})
        nodes.append({"id": f"mod{i}_orderservice", "label": "orderService", "source_file": spellings[(i + 1) % 4],
                      "source_location": f"L{i + 3}"})
        nodes.append({"id": f"mod{i}_orderservice", "label": ".orderService()", "source_file": f,
                      "source_location": f"L{i + 9}"})
    nodes.append({"id": "cross_x", "label": "X", "source_file": "a.py", "source_location": "L1"})
    nodes.append({"id": "cross_x", "label": "x", "source_file": "b.py", "source_location": "L1"})
    nodes.append({"id": "sql_t", "label": "T", "source_file": "q.sql", "source_location": "L1"})
    nodes.append({"id": "sql_t", "label": "t", "source_file": "q.sql", "source_location": "L2"})
    kept = "old_orderapi"
    nodes.append({"id": kept, "label": "OrderApi", "source_file": "api.ts", "source_location": "L1"})
    nodes.append({"id": f"{kept}_{case_ids._digest('orderApi')[:6]}", "label": "orderApi", "source_file": "api.ts",
                  "source_location": "L9"})
    nodes.append({"id": "nofile", "label": "N"})
    nodes.append({"id": "nofile2", "label": "N", "source_file": ""})
    return nodes


def _same_plans(nodes: list, root: Path | None) -> None:
    a, b = copy.deepcopy(nodes), copy.deepcopy(nodes)
    assert case_ids._plans(a, root) == _plans_before(b, root)
    assert a == b  # the ids the split rewrote in place


@pytest.mark.parametrize("with_root", [True, False])
def test_memoised_file_keys_give_the_same_plans(tmp_path, with_root):
    nodes = _generated_nodes(tmp_path)
    plans, split_now = case_ids._plans(copy.deepcopy(nodes), tmp_path if with_root else None)
    assert split_now and any(len(p.members) == 2 for p in plans.values())  # both kinds of split occur
    _same_plans(nodes, tmp_path if with_root else None)


def test_memoised_file_keys_give_the_same_plans_on_real_graphs():
    fixture = json.loads((ROOT / "tests_upstream" / "fixtures" / "extraction.json").read_text(encoding="utf-8"))
    _same_plans(fixture["nodes"], ROOT)
    if not OWN_GRAPH.is_file():
        pytest.skip("this checkout has no index of its own")
    nodes = json.loads(OWN_GRAPH.read_text(encoding="utf-8"))["nodes"]
    _same_plans(nodes, ROOT)


# -- python_facts: one resolution per (module, level, folder) ----------------------------------------

PY_FILES = {
    "pkg/__init__.py": "from .a import f as g\nfrom . import b\nfrom pkg import c\n",
    "pkg/a.py": "from .c import helper\nfrom . import b, c, missing\nfrom pkg.c import helper as h2\n",
    "pkg/b.py": "from .c import helper\nfrom . import c\nfrom pkg import a\nfrom ..top import T\n",
    "pkg/c.py": "from pkg.a import f\nfrom .sub import deep\nfrom .sub.deep import D\n\ndef helper():\n    f()\n",
    "pkg/sub/__init__.py": "from .. import a\nfrom ..c import helper\n",
    "pkg/sub/deep.py": "from ...pkg import c\nfrom .. import b\nfrom pkg.sub import deep\nD = 1\n",
    "ns/brain.py": "from ns import other\nfrom . import other\n\ndef think():\n    return 2\n",
    "ns/other.py": "from ns import brain\nfrom ns.brain import think\n",
    "src/lib/__init__.py": "",
    "src/lib/core.py": "from lib import util\nfrom lib.util import u\nfrom lib import core\n",
    "src/lib/util.py": "from lib.core import x\nfrom lib import core\nfrom lib import util\n\ndef u():\n    pass\n",
    "top.py": "from pkg import a, b\nfrom ns import brain\nfrom pkg.sub import deep\nfrom nowhere import z\nT = 1\n",
    "main.py": "from pkg import a, b\nfrom top import T\nfrom ns import brain\nfrom . import top\n",
}


def _py_project(root: Path) -> list[Path]:
    for rel, text in PY_FILES.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return sorted(p for p in root.rglob("*.py") if p.is_file())


def _collected(fn, paths, root):
    from verinoda.project_index.extractors.models import _SymbolResolutionFacts

    facts = _SymbolResolutionFacts()
    fn(paths, root, facts)
    return facts


def test_memoised_import_resolution_gives_the_upstream_facts(tmp_path):
    from verinoda.project_index.extractors import resolution as res
    from verinoda.python_facts import python_facts_cache

    root = tmp_path / "proj"
    paths = _py_project(root)
    want = _collected(res._collect_python_symbol_resolution_facts, paths, root)
    assert want.imports and want.module_imports and want.exports  # every kind occurs
    for _ in range(2):  # cold, then from the kept facts
        with python_facts_cache(tmp_path / "index"):
            assert _collected(res._collect_python_symbol_resolution_facts, paths, root) == want


def test_memoised_import_resolution_gives_the_upstream_facts_on_this_repository(tmp_path):
    from verinoda.project_index.extractors import resolution as res
    from verinoda.python_facts import python_facts_cache

    paths = sorted(p for p in (ROOT / "verinoda").rglob("*.py") if "__pycache__" not in p.parts)
    want = _collected(res._collect_python_symbol_resolution_facts, paths, ROOT)
    assert len(want.imports) > 100
    with python_facts_cache(tmp_path / "index"):
        assert _collected(res._collect_python_symbol_resolution_facts, paths, ROOT) == want


# -- the deleted-file check: stat before resolve -------------------------------------------------------

def _missing_before(repo: Path, nodes) -> list[str]:
    """``index._missing_in_nodes`` as it was (verbatim)."""
    seen: dict[str, bool] = {}
    for n in nodes:
        sf = n.get("source_file")
        if not isinstance(sf, str) or sf in seen:
            continue
        p = index._local_source(repo, sf)
        seen[sf] = p is not None and not p.exists()
    return sorted(sf for sf, gone in seen.items() if gone)


def _link_dir(link: Path, target: Path) -> bool:
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        pass
    if os.name == "nt":
        try:
            import _winapi

            _winapi.CreateJunction(str(target), str(link))
            return True
        except (ImportError, OSError):
            pass
    return False


def test_stat_first_gives_the_same_missing_files(tmp_path):
    repo, outside = (tmp_path / "repo").resolve(), (tmp_path / "outside").resolve()
    (repo / "src").mkdir(parents=True)
    outside.mkdir()
    (repo / "src" / "here.py").write_text("x = 1\n", encoding="utf-8")
    (outside / "there.py").write_text("y = 2\n", encoding="utf-8")
    linked = _link_dir(repo / "linked", outside)
    sfs = ["src/here.py", "src/gone.py", str(repo / "src" / "here.py"), str(repo / "src" / "gone.py"),
           str(outside / "there.py"), str(outside / "never.py"), "../outside/there.py", "../outside/never.py",
           "src/../src/here.py", "src\\here.py", "https://example.com/a.py", "file:///c/x.py", "virtual:thing",
           "", ".", "src", "linked/there.py", "linked/never.py", "bad\0name.py", "C:relative.py"]
    nodes = [{"source_file": sf} for sf in sfs] + [{"source_file": None}, {"source_file": 3}, {}]
    got = index._missing_in_nodes(repo, nodes)
    assert got == _missing_before(repo, nodes)
    assert "src/gone.py" in got and "src/here.py" not in got
    if linked:
        assert "linked/never.py" not in got  # resolves outside the repository: never "missing"


# -- the snapshot's listing, reused by the search index and the lexicon --------------------------------

def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args],
                   cwd=cwd, check=True, capture_output=True, stdin=subprocess.DEVNULL)


@pytest.fixture
def orders(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git not available")
    from verinoda.store import open_store

    repo = tmp_path / "orders_app"
    shutil.copytree(EXAMPLE, repo, ignore=shutil.ignore_patterns(".verinoda", "__pycache__", "*.pyc",
                                                                 ".pytest_cache", "*.db"))
    (repo / "config").mkdir()
    (repo / "config" / "settings.yaml").write_text("orders:\n  limit: 5\n", encoding="utf-8")
    (repo / "locales").mkdir()
    (repo / "locales" / "en.json").write_text('{"order": {"place_order": "Place the order"}}', encoding="utf-8")
    (repo / "locales" / "tr.json").write_text('{"order": {"place_order": "Siparisi ver"}}', encoding="utf-8")
    old = time.time() - 3600  # older than the racy margin: the snapshot's stat-cached hashes are trusted
    for p in repo.rglob("*"):
        if p.is_file():
            os.utime(p, (old, old))
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    workflow.init(repo)
    st = open_store(repo)
    yield repo.resolve(), st
    st.close()


def _db_rows(db: Path) -> dict:
    conn = sqlite3.connect(str(db))
    try:
        out = {}
        for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
            rows = conn.execute(f'SELECT * FROM "{name}"').fetchall()
            if name == "meta":
                rows = [r for r in rows if r[0] != "built_at"]
            out[name] = rows
        return out
    finally:
        conn.close()


def _lexicon_without_time(repo: Path) -> dict:
    raw = json.loads(lexicon.lexicon_path(repo).read_text(encoding="utf-8"))
    raw.pop("built_at", None)
    return raw


def test_the_snapshot_listing_is_list_files_and_derives_the_same_data(orders):
    repo, st = orders
    workflow.scan(st, repo)
    listed: list[str] = []
    snap = snapshot.take_snapshot(st, repo, graph_stats={"nodes": 1}, listing=listed)
    assert listed == snapshot.list_files(repo) and "config/settings.yaml" in listed
    assert snap["file_count"] == len(listed)

    g = index.load(repo)
    a, b = tmp = (repo.parent / "a.db", repo.parent / "b.db")
    search_index.update(repo, g, None, db=a)
    search_index.update(repo, g, None, db=b, listed=list(listed))
    assert _db_rows(a) == _db_rows(b)
    assert any(r[0] == "config/settings.yaml" for r in _db_rows(b)["files"])
    for p in tmp:
        p.unlink()

    for graph in (g, None):
        lexicon.build(repo, graph)
        want = _lexicon_without_time(repo)
        lexicon.build(repo, graph, listed=list(listed))
        assert _lexicon_without_time(repo) == want
        lexicon.lexicon_path(repo).unlink()  # the next build starts from nothing again
    assert want["pairs"]  # the lexicon learned something (translations included)


def test_update_derives_what_listing_again_derived(orders, monkeypatch):
    repo, st = orders
    workflow.scan(st, repo)
    svc = repo / "orders" / "service.py"
    svc.write_bytes(svc.read_bytes() + b"\n\ndef audit_marker():\n    return 'audit'\n")
    (repo / "config" / "more.yaml").write_text("more: 1\n", encoding="utf-8")
    res = workflow.update(st, repo)
    assert "error" not in res["derived"]["search_index"] and "error" not in res["derived"].get("lexicon", {})
    db = index_dir(repo) / "search.db"
    got_db, got_lex = _db_rows(db), _lexicon_without_time(repo)
    # the same derivation, each step listing the tree itself (the code before the listing was handed over)
    g = index.load(repo)
    files = st.snapshot_files(res["snapshot"]["id"])
    shutil.copy2(db, repo.parent / "copy.db")
    real_update = search_index.update
    monkeypatch.setattr(search_index, "update", lambda *a, **k: real_update(*a, **{**k, "listed": None}))
    workflow._derive(st, repo, changed=["orders/service.py", "config/more.yaml"], all_files=sorted(files),
                     file_hashes=files, tree=res["snapshot"]["tree_hash"])
    assert _db_rows(db) == got_db and _lexicon_without_time(repo) == got_lex


# -- the receiver sidecar: the build's own graph data, the snapshot's stat-cached hashes ---------------

def test_the_graph_from_data_is_the_graph_load_builds(orders):
    repo, st = orders
    workflow.scan(st, repo)
    gp = graph_path(repo)
    loaded = index.load(repo, augment=False)
    made = index._graph_from_data(json.loads(gp.read_text(encoding="utf-8")), gp, repo)
    assert (made.path, made.root) == (loaded.path, loaded.root)
    assert list(made.G.nodes(data=True)) == list(loaded.G.nodes(data=True))
    assert list(made.G.edges(keys=True, data=True)) == list(loaded.G.edges(keys=True, data=True))


def test_the_sidecar_the_build_writes_is_the_one_a_reload_writes(orders):
    repo, st = orders
    workflow.scan(st, repo)
    sc = index_dir(repo) / "receiver_calls.json"
    built = sc.read_bytes()
    sc.unlink()
    index._PYINFO_CACHE.clear()
    index.refresh_receiver_sidecar(repo)  # the graph read from graph.json, every file parsed
    assert sc.read_bytes() == built


def test_stat_cached_hashes_give_the_same_sidecar(orders, monkeypatch):
    repo, st = orders
    workflow.scan(st, repo)
    sc = index_dir(repo) / "receiver_calls.json"
    trusted = []
    real_fresh = snapshot._StatCache.fresh

    def fresh(self, rel, stat):
        hit = real_fresh(self, rel, stat)
        trusted.append(hit is not None)
        return hit

    monkeypatch.setattr(snapshot._StatCache, "fresh", fresh)
    stats = index.refresh_receiver_sidecar(repo)
    with_cache = sc.read_bytes()
    assert any(trusted) and stats["files_parsed"] == 0
    monkeypatch.setattr(snapshot._StatCache, "for_repo", classmethod(lambda cls, repo, store: None))
    index.refresh_receiver_sidecar(repo)
    assert sc.read_bytes() == with_cache

    # a changed file: its cached hash is not trusted (or differs), so it is read and parsed again
    svc = repo / "orders" / "service.py"
    svc.write_bytes(svc.read_bytes() + b"\n\ndef extra(repo):\n    return repo.save(1)\n")
    workflow.update(st, repo)
    after = sc.read_bytes()
    monkeypatch.undo()
    sc.unlink()
    index._PYINFO_CACHE.clear()
    index.refresh_receiver_sidecar(repo)
    assert sc.read_bytes() == after
