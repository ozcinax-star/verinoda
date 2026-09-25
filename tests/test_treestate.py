"""Tree-state identity, changes vs a base commit, commit copies for experiments, and the v6 debug tables."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import hashlib  # noqa: E402
import shutil  # noqa: E402
import sqlite3  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import experiments, treestate  # noqa: E402
from verinoda.store import SCHEMA_VERSION, Store, now, open_store  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "orders_app"
PYTEST = ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def _git(cwd: Path, *args: str) -> str:
    r = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args],
                       cwd=cwd, check=True, capture_output=True, text=True)
    return r.stdout


def _repo(tmp_path: Path) -> Path:
    dst = tmp_path / "oa"
    shutil.copytree(EXAMPLE, dst, ignore=shutil.ignore_patterns(".verinoda", "__pycache__", "*.pyc",
                                                                ".pytest_cache", "*.db"))
    _git(dst, "init", "-q")
    _git(dst, "add", "-A")
    _git(dst, "commit", "-q", "-m", "init")
    return dst


def _digest(repo: Path) -> dict[str, str]:
    out = {}
    for p in sorted(repo.rglob("*")):
        rel = p.relative_to(repo).as_posix()
        if p.is_file() and not rel.startswith(".verinoda/"):
            out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


# -- identity --------------------------------------------------------------------------------

def test_content_id_ignores_crlf_and_tree_id_is_order_free():
    assert treestate.content_id(b"a\r\nb\r\n") == treestate.content_id(b"a\nb\n")
    assert treestate.content_id(b"a\nb\n") != treestate.content_id(b"a\nc\n")
    assert treestate.tree_id({"x": "1", "y": "2"}) == treestate.tree_id({"y": "2", "x": "1"})
    assert treestate.tree_id({"x": "1"}) != treestate.tree_id({"x": "2"})


def test_copy_file_hash_handles_a_crlf_split_across_chunks(tmp_path):
    data = b"x" * ((1 << 20) - 1) + b"\r\n" + b"tail\r\n"
    src, dst = tmp_path / "big.txt", tmp_path / "copy.txt"
    src.write_bytes(data)
    cid = treestate.copy_file(src, dst)
    assert dst.read_bytes() == data
    assert cid == treestate.content_id(data)


def test_current_state_uses_the_memo_and_matches_the_copy(tmp_path):
    repo = _repo(tmp_path)
    st = open_store(repo)
    a = treestate.current(repo, store=st)
    b = treestate.current(repo, store=st)
    assert a["hash"] == b["hash"] and a["count"] == len(a["files"]) > 3
    memo = st.all("SELECT * FROM file_facts WHERE scheme = 'lf1'")
    assert memo, "the raw -> content id memo is kept"
    ids: dict[str, str] = {}
    res = experiments.run(st, repo, PYTEST, hypothesis="identity", file_ids=ids)
    assert res["tree"]["hash"] == a["hash"] and ids == a["files"]
    assert res["source"] == {"kind": "worktree"}
    ev = st.evidence(res["evidence_id"])
    assert ev["meta"]["tree_hash"] == a["hash"]
    (repo / "orders" / "pricing.py").write_text("# changed\n", encoding="utf-8")
    assert treestate.current(repo, store=st)["hash"] != a["hash"]


# -- refs --------------------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["-x", "--output=/tmp/pwned", "--exec=calc", "HEAD\nx", "a b", ""])
def test_refs_that_could_be_options_are_refused(tmp_path, bad):
    repo = _repo(tmp_path)
    with pytest.raises(ValueError):
        treestate.resolve_commit(repo, bad)


def test_resolve_commit_gives_the_full_id(tmp_path):
    repo = _repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD").strip()
    assert treestate.resolve_commit(repo, "HEAD") == head
    assert treestate.resolve_commit(repo, head[:10]) == head
    with pytest.raises(ValueError):
        treestate.resolve_commit(repo, "no-such-branch")


# -- commit copies -----------------------------------------------------------------------------

@pytest.mark.experiment
def test_experiment_on_a_commit_copy_leaves_the_tree_and_git_untouched(tmp_path):
    repo = _repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD").strip()
    p = repo / "orders" / "pricing.py"
    p.write_text(p.read_text(encoding="utf-8").replace('i["qty"]', 'i["quantity"]'), encoding="utf-8")
    before = _digest(repo)  # .git included
    status_before = _git(repo, "status", "--porcelain")
    st = open_store(repo)
    now_run = experiments.run(st, repo, PYTEST, hypothesis="the working tree fails")
    at_head = experiments.run(st, repo, PYTEST, hypothesis="HEAD passes", ref="HEAD")
    assert now_run["outcome"] == "fail" and at_head["outcome"] == "pass"
    assert at_head["source"] == {"kind": "commit", "commit": head, "ref": "HEAD"}
    assert at_head["tree"]["hash"] == treestate.tree_id(treestate.commit_files(repo, head))
    assert at_head["tree"]["hash"] != now_run["tree"]["hash"]
    ev = st.evidence(at_head["evidence_id"])
    assert ev["commit_sha"] == head and ev["meta"]["source"]["kind"] == "commit"
    row = st.get("experiments", at_head["id"])
    assert row["cwd"].startswith(f"copy of commit {head[:12]}")
    assert _digest(repo) == before and _git(repo, "status", "--porcelain") == status_before


@pytest.mark.experiment
def test_overlay_puts_working_tree_files_over_the_commit_copy(tmp_path):
    repo = _repo(tmp_path)
    t = repo / "tests" / "test_pricing.py"
    t.write_text(t.read_text(encoding="utf-8") + "\n\ndef test_new():\n    assert False\n", encoding="utf-8")
    st = open_store(repo)
    plain = experiments.run(st, repo, PYTEST, hypothesis="old tests", ref="HEAD")
    over = experiments.run(st, repo, PYTEST, hypothesis="new test on old code", ref="HEAD",
                           overlay=["tests/test_pricing.py"])
    assert plain["outcome"] == "pass" and over["outcome"] == "fail"
    assert over["source"]["overlay"] == ["tests/test_pricing.py"]
    for bad in (["../x.py"], ["/etc/passwd"], [".git/config"], ["tests/missing.py"]):
        with pytest.raises(ValueError):
            experiments.run(st, repo, PYTEST, hypothesis="bad overlay", ref="HEAD", overlay=bad)
    with pytest.raises(ValueError):
        experiments.run(st, repo, PYTEST, hypothesis="overlay without ref", overlay=["tests/test_pricing.py"])


def test_experiment_refuses_an_option_like_ref(tmp_path):
    repo = _repo(tmp_path)
    with pytest.raises(ValueError):
        experiments.run(open_store(repo), repo, PYTEST, hypothesis="x", ref="--output=x")


# -- changes vs base, patches, symbols -----------------------------------------------------------

def test_changes_vs_base_lists_exactly_the_differing_paths(tmp_path):
    repo = _repo(tmp_path)
    head = treestate.head_commit(repo)
    p = repo / "orders" / "pricing.py"
    p.write_text(p.read_text(encoding="utf-8").replace('i["qty"]', 'i["quantity"]'), encoding="utf-8")
    (repo / "orders" / "extra.py").write_text("X = 1\n", encoding="utf-8")
    (repo / "orders" / "api.py").unlink()
    cfg = repo / "orders" / "config.py"
    cfg.write_bytes(cfg.read_bytes().replace(b"\n", b"\r\n"))  # line endings only: not a change
    ch = treestate.changes_vs_base(repo, head)
    assert set(ch["tree_files"]) == {"orders/pricing.py", "orders/extra.py", "orders/api.py"}
    assert ch["tree_files"]["orders/api.py"] is None
    assert ch["tree_files"]["orders/extra.py"] == treestate.content_id(b"X = 1\n")
    assert "orders/api.py" in ch["base"] and ch["drift"] == []
    patch = treestate.unified_patch({q: (ch["base"].get(q), ch["contents"].get(q)) for q in ch["tree_files"]})
    assert '-    subtotal = sum(i["price"] * i["qty"] for i in items)' in patch
    assert "+++ /dev/null" in patch and "--- /dev/null" in patch


def test_diff_file_maps_hunks_to_the_definitions_they_change():
    old = b'def a():\n    return 1\n\n\ndef b():\n    return 2\n'
    new = b'def a():\n    return 1\n\n\ndef b():\n    return 3\n'
    rec = treestate.diff_file("pkg/mod.py", old, new)
    assert rec["status"] == "modified" and rec["symbols"] == ["b"]
    assert rec["hunks"][0]["old"] == [6, 6] and rec["hunks"][0]["added"] == ["    return 3"]
    assert treestate.diff_file("tests/test_x.py", None, b"x = 1\n")["test"] is True


def test_diff_trees_uses_the_blob_store(tmp_path):
    repo = _repo(tmp_path)
    head = treestate.head_commit(repo)
    v1 = treestate.put_blob(repo, b'"""Pricing rules."""\nA = 1\n')
    v2 = treestate.put_blob(repo, b'"""Pricing rules."""\nA = 2\n')
    d = treestate.diff_trees(repo, head, {"orders/pricing.py": v1}, {"orders/pricing.py": v2})
    assert len(d) == 1 and d[0]["hunks"][0]["removed"] == ["A = 1"] and d[0]["hunks"][0]["added"] == ["A = 2"]
    back = treestate.diff_trees(repo, head, {"orders/pricing.py": v1}, {})
    assert back[0]["hunks"] and "content_unknown" not in back[0]
    unknown = treestate.diff_trees(repo, head, {"orders/pricing.py": "0" * 64}, {})
    assert unknown[0]["content_unknown"] is True


def test_read_blobs_reports_missing_objects(tmp_path):
    repo = _repo(tmp_path)
    head = treestate.head_commit(repo)
    got = treestate.read_blobs(repo, [f"{head}:orders/pricing.py", f"{head}:nope.py"])
    assert got[f"{head}:nope.py"] is None and b"def compute_total" in got[f"{head}:orders/pricing.py"]


# -- schema v6 -----------------------------------------------------------------------------------

def _session(st: Store, sid: str = "dbg_1") -> None:
    st.insert("debug_sessions", {"id": sid, "symptom": "s", "command": ["pytest"], "status": "open",
                                 "created_at": now()})


def test_debug_tables_are_append_only(tmp_path):
    st = Store(tmp_path / "a.db")
    assert SCHEMA_VERSION == 6
    _session(st)
    st.insert("debug_attempts", {"id": "dba_1", "session_id": "dbg_1", "n": 0, "kind": "baseline",
                                 "hypothesis": "h", "command": ["pytest"], "run_by": "verinoda", "outcome": "fail",
                                 "created_at": now()})
    with pytest.raises(sqlite3.IntegrityError):
        st.conn.execute("UPDATE debug_attempts SET outcome = 'pass' WHERE id = 'dba_1'")
    with pytest.raises(sqlite3.IntegrityError):
        st.conn.execute("DELETE FROM debug_attempts")
    with pytest.raises(sqlite3.IntegrityError):
        st.conn.execute("DELETE FROM debug_sessions")
    with pytest.raises(sqlite3.IntegrityError):
        st.conn.execute("UPDATE debug_sessions SET symptom = 'other' WHERE id = 'dbg_1'")
    st.update("debug_sessions", "dbg_1", {"status": "abandoned", "closed_at": now()})
    with pytest.raises(sqlite3.IntegrityError):
        st.conn.execute("UPDATE debug_sessions SET status = 'open' WHERE id = 'dbg_1'")
    with pytest.raises(sqlite3.IntegrityError):
        st.insert("debug_attempts", {"id": "dba_2", "session_id": "dbg_1", "n": 0, "kind": "fix", "hypothesis": "h",
                                     "command": ["pytest"], "run_by": "robot", "outcome": "fail",
                                     "created_at": now()})


def test_a_v4_database_migrates_to_v6(tmp_path):
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    from verinoda import store as storemod

    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    for v in range(1, 5):
        conn.executescript(storemod._MIGRATIONS[v])
    conn.execute("INSERT INTO meta VALUES ('schema_version', '4')")
    conn.commit()
    conn.close()
    st = Store(db)
    assert st.one("SELECT value FROM meta WHERE key='schema_version'")["value"] == "6"
    _session(st, "dbg_x")
    assert st.get("debug_sessions", "dbg_x")["command"] == ["pytest"]


# -- review findings ---------------------------------------------------------------------------

def test_paths_that_a_file_system_may_fold_to_the_git_directory_are_reserved():
    for part in (".git", ".GIT", ".Git", ".git.", ".git ", "git~1", "GIT~1", ".git::$INDEX_ALLOCATION",
                 ".g‌it", "﻿.git", ".verinoda", ".VERINODA"):
        assert treestate.reserved_part(part), repr(part)
    for part in (".github", "git", ".gitignore", "digit", "gitx"):
        assert not treestate.reserved_part(part), part
    assert not treestate.safe_path(".GIT/config") and not treestate.safe_path("a/../b")
    assert treestate.safe_path("src/.github/x.yml")


@pytest.mark.skipif(os.name != "nt", reason="Windows file names")
def test_names_windows_cannot_hold_are_named():
    for rel in ("docs/what?.md", "a:b", "x/NUL", "com1.txt", "trailing./x", "q\"x"):
        assert treestate.unwritable_here(rel), rel
    assert treestate.unwritable_here("docs/normal.md") is None


def test_a_symlink_checked_out_as_a_plain_file_is_no_change(tmp_path):
    # review finding: with core.symlinks=false (the Git for Windows default) a tracked symlink is a plain file
    repo = _repo(tmp_path)
    blob = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=repo, input=b"docs/adr", capture_output=True,
                          check=True).stdout.decode().strip()
    _git(repo, "update-index", "--add", "--cacheinfo", f"120000,{blob},LINK.md")
    _git(repo, "commit", "-q", "-m", "a symlink")
    (repo / "LINK.md").write_bytes(b"docs/adr")   # what a core.symlinks=false checkout writes
    base = treestate.head_commit(repo)
    ids = treestate.current(repo)["files"]
    assert "LINK.md" in ids and "LINK.md" in treestate.index_symlinks(repo)
    ch = treestate.changes_from_ids(repo, base, ids, treestate.base_ids(repo, base))
    assert ch["tree_files"] == {}


def test_python_edits_of_comments_docstrings_and_formatting_keep_the_code_identity():
    old = b'"""Doc."""\n\n\ndef f(x):\n    """Say."""\n    return x["a"]  # key\n'
    new = b'"""Other doc."""\n\n\ndef f(x):\n    return x[\'a\']\n'
    changed = b'"""Doc."""\n\n\ndef f(x):\n    return x["b"]\n'
    assert treestate.code_fingerprint("m.py", old) == treestate.code_fingerprint("m.py", new)
    assert treestate.code_fingerprint("m.py", old) != treestate.code_fingerprint("m.py", changed)
    assert treestate.code_fingerprint("m.js", old) is None and treestate.code_fingerprint("m.py", b"def (") is None
    assert treestate.diff_file("m.py", old, new).get("no_code_change") is True
    assert "no_code_change" not in treestate.diff_file("m.py", old, changed)
    ch = {"tree_files": {"m.py": treestate.content_id(new)}, "contents": {"m.py": new}, "base": {"m.py": old}}
    assert treestate.code_tree_id(ch) == treestate.tree_id({})  # the same code as the base
    ch2 = {"tree_files": {"m.py": treestate.content_id(changed)}, "contents": {"m.py": changed}, "base": {"m.py": old}}
    assert treestate.code_tree_id(ch2) != treestate.tree_id({})


def test_the_ledgers_test_files_cover_the_parsed_languages():
    for p in ("src/cart.test.js", "src/cart.spec.ts", "src/components/Cart.test.tsx", "pricing_test.go",
              "src/test/java/a/WispTest.java", "tests/test_x.py", "pkg/x_test.py", "conftest.py"):
        assert treestate.is_test_file(p), p
    for p in ("src/cart.js", "orders/pricing.py", "latest.go", "src/main/java/a/Contest.java"):
        assert not treestate.is_test_file(p), p


def test_a_big_changed_file_is_diffed_fast_and_once_per_session(tmp_path, monkeypatch):
    # review finding: a 17k-line lockfile changed vs the base cost 4.8 s per `debug try`
    import time

    from verinoda import debug

    lines = [f'    "node_modules/pkg{i}": {{ "version": "1.0.{i % 10}" }},' for i in range(17_000)]
    old = ("\n".join(lines) + "\n").encode()
    lines[500] = lines[500].replace("1.0.", "2.0.")
    lines[9000] = lines[9000].replace("1.0.", "2.0.")
    new = ("\n".join(lines) + "\n").encode()
    t = time.perf_counter()
    d = treestate.diff_file("package-lock.json", old, new)
    assert time.perf_counter() - t < 2.0 and len(d["hunks"]) == 2
    repo = _repo(tmp_path)
    (repo / "package-lock.json").write_bytes(old)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "lock")
    (repo / "package-lock.json").write_bytes(new)
    base = treestate.head_commit(repo)
    ids = treestate.current(repo)["files"]
    first = debug._tree_record(repo, base, ids, {"kind": "worktree"})
    calls = []
    real = treestate.diff_file
    monkeypatch.setattr(treestate, "diff_file", lambda *a, **k: calls.append(a[0]) or real(*a, **k))
    prior = [{"tree_files": first["tree_files"], "touched": {"vs_base": first["vs_base"]}}]
    again = debug._tree_record(repo, base, ids, {"kind": "worktree"}, prior)
    assert calls == [] and again["vs_base"] == first["vs_base"]


def test_a_project_below_the_git_top_level_lists_its_own_files(tmp_path):
    root = tmp_path / "mono"
    shutil.copytree(EXAMPLE, root / "pkg", ignore=shutil.ignore_patterns(".verinoda", "__pycache__", "*.pyc", "*.db"))
    (root / "other.py").write_text("X = 1\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    pkg = root / "pkg"
    base = treestate.head_commit(pkg)
    assert treestate.project_prefix(pkg) == "pkg/"
    files = treestate.commit_files(pkg, base)
    assert "orders/pricing.py" in files and "other.py" not in files and not any(p.startswith("pkg/") for p in files)
    (pkg / "orders" / "pricing.py").write_text("X = 2\n", encoding="utf-8")
    ch = treestate.changes_vs_base(pkg, base)
    assert list(ch["tree_files"]) == ["orders/pricing.py"] and ch["base"]["orders/pricing.py"].startswith(b'"""')
