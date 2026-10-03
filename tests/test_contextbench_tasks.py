"""The ContextBench task builder and the history-aware copy builder of the follow-up agent study
(benchmarks/agent_compare/contextbench_tasks.py and commit_prep.py)."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import ClassVar

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmarks" / "agent_compare"))


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"agent_compare_{name}_cb", ROOT / "benchmarks" / "agent_compare" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


cbt = _load("contextbench_tasks")
prep = _load("commit_prep")

STATEMENT = ("Crash when the cache is flushed twice\n"
             "Calling flush() two times in a row raises an IndexError on the second call. " * 3)


def diff(files) -> str:
    """A patch that modifies each of ``files``."""
    return "".join(f"diff --git a/{f} b/{f}\nindex 1..2 100644\n--- a/{f}\n+++ b/{f}\n@@ -1 +1 @@\n-x\n+y\n" for f in files)


def row(iid: str, repo: str = "own/repo", lang: str = "python", files=("pkg/a.py", "pkg/b.py"), statement: str = STATEMENT,
        sha: str | None = None, context=None) -> dict:
    """``files`` are the ones the gold patch changes; ``context`` (default: the same) are the dataset's gold contexts."""
    ctx = [{"file": f, "start_line": 1, "end_line": 9, "content": "x"} for f in (files if context is None else context)]
    return {"original_inst_id": iid, "instance_id": f"Set__{iid}", "repo": repo, "repo_url": f"https://github.com/{repo}.git",
            "language": lang, "base_commit": sha or iid.ljust(40, "0"), "gold_context": json.dumps(ctx), "patch": diff(files),
            "problem_statement": statement}


SIZES = {"own/repo": 1000}


# ---- the gold path rule -------------------------------------------------------------------------------------------

def test_gold_paths_are_source_files_in_any_language_and_not_tests_docs_or_ci():
    for good in ("astropy/coordinates/attributes.py", "src/lib.rs", "pkg/server/handler.go", "src/index.ts", "src/App.tsx",
                 "lib/util.js", "include/zstd.h", "src/ponyc/pass/expr.c", "src/main/java/org/x/Foo.java", "CMakeLists.txt",
                 "sub/CMakeLists.txt", "examples/demo.py", "setup.py", "libsel4/arch_include/arm/sel4/arch/types.bf", "Foo.cpp"):
        assert cbt.is_gold_path(good), good
    for bad in ("tests/test_a.py", "pkg/tests/helpers.py", "test/x.c", "spec/models/user_spec.rb", "src/__tests__/a.ts",
                "docs/conf.py", "doc/gen.py", ".github/scripts/x.py", "README.md", "docs/guide.rst", "notes.txt", "LICENSE",
                "package.json", "go.mod", "config.yml", "src/style.css", "pkg/test_util.py", "pkg/conftest.py", "util_test.go",
                "src/app.test.ts", "src/app.spec.js", "src/FooTest.java", "src/FooTests.java", "pkg/tests.py", "Makefile",
                "pkg/Tests/x.py", "pkg/testdata/a.go"):
        assert not cbt.is_gold_path(bad), bad
    assert cbt.normalize_path("/testbed/pkg/a.py") == "pkg/a.py"
    assert cbt.normalize_path("/workspace/repo/pkg/a.py") == "pkg/a.py"
    assert cbt.normalize_path("./.github/a.py") == ".github/a.py"
    assert cbt.normalize_path("pkg\\a.py") == "pkg/a.py"


def test_gold_files_are_the_distinct_sorted_source_files_the_patch_changes():
    r = row("x", files=("pkg/b.py", "pkg/a.py", "pkg/a.py", "tests/test_a.py", "docs/x.md"))
    assert cbt.gold_files(r) == ["pkg/a.py", "pkg/b.py"]


def test_gold_files_follow_the_patch_not_the_contexts_the_dataset_lists_to_read():
    r = row("x", files=("pkg/a.py", "pkg/b.py"), context=("pkg/a.py", "pkg/c.py", "pkg/d.py"))
    assert cbt.gold_files(r) == ["pkg/a.py", "pkg/b.py"]
    assert cbt.gold_files({**r, "patch": ""}) == [] and cbt.gold_files({k: v for k, v in r.items() if k != "patch"}) == []


def test_a_patch_names_the_files_that_exist_before_it_modified_deleted_and_renamed_ones():
    patch = (
        "diff --git a/pkg/mod.py b/pkg/mod.py\nindex 1..2 100644\n--- a/pkg/mod.py\n+++ b/pkg/mod.py\n@@ -1 +1 @@\n-a\n+b\n"
        "diff --git a/pkg/added.py b/pkg/added.py\nnew file mode 100644\nindex 0..2\n--- /dev/null\n+++ b/pkg/added.py\n@@ -0,0 +1 @@\n+a\n"
        "diff --git a/pkg/gone.py b/pkg/gone.py\ndeleted file mode 100644\nindex 1..0\n--- a/pkg/gone.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-a\n"
        "diff --git a/pkg/old.py b/pkg/new_name.py\nsimilarity index 90%\nrename from pkg/old.py\nrename to pkg/new_name.py\n"
        "diff --git a/my dir/with space.py b/my dir/with space.py\nindex 1..2 100644\n--- a/my dir/with space.py\n"
        "+++ b/my dir/with space.py\n@@ -1 +1 @@\n-a\n+b\n"
        "diff --git a/img.png b/img.png\nindex 1..2 100644\nBinary files a/img.png and b/img.png differ\n")
    assert cbt.patch_files(patch) == ["pkg/mod.py", "pkg/gone.py", "pkg/old.py", "my dir/with space.py", "img.png"]
    assert cbt.patch_files("") == [] and cbt.patch_files(None) == []


def test_the_statement_splits_into_title_and_body():
    assert cbt.split_statement("Title here\nbody line\nmore") == ("Title here", "body line\nmore")
    t, b = cbt.split_statement("x" * 300 + "\nrest")
    assert len(t) <= 120 and t.endswith("...") and b.startswith("x" * 300)


# ---- the selection ------------------------------------------------------------------------------------------------

def test_selection_applies_every_rule_and_counts_each_drop():
    long_ = "word " * 1000  # 5000 characters
    rows = [row("keep1"),
            row("one_gold", files=("pkg/a.py",)),
            row("seven_gold", files=tuple(f"pkg/f{i}.py" for i in range(7))),
            row("tests_only", files=("tests/a.py", "tests/b.py", "README.md")),
            row("short", statement="Crash\nshort"),
            row("long", statement="Title\n" + long_),
            row("leak", statement=STATEMENT + " see pkg/a.py"),
            row("big", repo="big/repo"),
            row("gone", repo="gone/repo"),
            row("crlf", statement=STATEMENT.replace("\n", "\r\n"))]
    sizes = {"own/repo": 1000, "big/repo": 300 * 1024, "gone/repo": None}
    tasks, info = cbt.select({"want": 20}, rows, sizes)
    assert sorted(t["id"] for t in tasks) == ["crlf", "keep1"]
    d = info["dropped"]
    assert d["gold source files not 2-6"] == 3 and d["statement length outside 150-4000"] == 2
    assert d["statement names a gold file's path"] == 1 and d["repository larger than 200 MB"] == 1
    assert d["repository missing or private (GitHub API 404)"] == 1
    assert info["candidates"] == 10 and info["selected"] == 2 and info["kept_per_repository"] == {"own/repo": 2}
    assert info["kept_per_language"] == {"python": 2}
    t = {t["id"]: t for t in tasks}["keep1"]
    assert t["gold"] == ["pkg/a.py", "pkg/b.py"] and t["title"] == "Crash when the cache is flushed twice"
    assert t["remote"] == "https://github.com/own/repo.git" and t["repo"] == "own/repo" and t["lang"] == "python"
    assert t["base_sha"] == "keep1".ljust(40, "0") and "\r" not in t["body"]
    for key in ("id", "title", "body", "gold", "base_sha", "issue", "via", "ref", "labels", "issue_created", "fixed_at"):
        assert key in t  # the commit_tasks schema


def test_the_size_of_a_repository_is_asked_only_when_an_instance_reaches_that_filter():
    asked = []
    rows = [row("a", repo="x/one"), row("b", repo="y/two", files=("pkg/a.py",))]  # y/two fails the gold rule first

    def size(repo):
        asked.append(repo)
        return 10
    tasks, _ = cbt.select({"want": 5}, rows, size)
    assert [t["id"] for t in tasks] == ["a"] and asked == ["x/one"]


def test_at_most_three_per_repository_and_the_walk_follows_the_seeded_hash_order():
    rows = [row(f"r{i}", repo="a/big") for i in range(6)] + [row(f"s{i}", repo="b/other") for i in range(2)]
    tasks, info = cbt.select({"want": 20}, rows, lambda r: 1)
    assert [t["repo"] for t in tasks].count("a/big") == 3 and [t["repo"] for t in tasks].count("b/other") == 2
    assert info["dropped"]["more than 3 per repository"] == 3 and info["available_after_cap_and_before_fetch_check"] == 5
    import hashlib
    key = lambda t: hashlib.sha256(f"20261002:{t['id']}".encode()).hexdigest()
    assert [t["id"] for t in tasks] == sorted((t["id"] for t in tasks), key=lambda i: key({"id": i}))
    assert len(cbt.select({"want": 2}, rows, lambda r: 1)[0]) == 2


def test_selection_is_deterministic_and_independent_of_the_row_order_but_not_of_the_seed():
    rows = [row(f"i{i}", repo=f"o/r{i % 7}") for i in range(40)]
    a = [t["id"] for t in cbt.select({"want": 10}, rows, lambda r: 1)[0]]
    b = [t["id"] for t in cbt.select({"want": 10}, list(reversed(rows)), lambda r: 1)[0]]
    c = [t["id"] for t in cbt.select({"want": 10, "seed": 7}, rows, lambda r: 1)[0]]
    assert a == b and len(set(a)) == 10 and a != c


def test_an_unfetchable_base_commit_is_replaced_by_the_next_survivor_and_checked_only_when_walked():
    rows = [row(f"i{i}", repo=f"o/r{i}") for i in range(6)]
    walked = []
    bad = {"i0", "i1"}

    def fetchable(r, gold):
        walked.append(r["original_inst_id"])
        return None if r["original_inst_id"] in bad else gold

    tasks, info = cbt.select({"want": 2}, rows, lambda r: 1, fetchable)
    assert len(tasks) == 2 and not bad & {t["id"] for t in tasks}
    assert len(walked) <= 4 and set(info["unfetchable"]) == bad & set(walked)
    assert info["dropped"].get("base commit not fetchable", 0) == len(info["unfetchable"])


def test_a_failed_fetch_frees_the_slot_in_the_repository_cap():
    rows = [row(f"i{i}", repo="a/one") for i in range(5)]
    first = cbt.select({"want": 5}, rows, lambda r: 1)[0]
    bad = first[0]["id"]
    tasks, _ = cbt.select({"want": 5}, rows, lambda r: 1, lambda r, g: None if r["original_inst_id"] == bad else g)
    assert len(tasks) == 3 and bad not in {t["id"] for t in tasks}


def test_language_filter_and_cached_lookups(tmp_path):
    rows = [row("a", lang="python"), row("b", lang="go", repo="g/o")]
    tasks, info = cbt.select({"want": 5, "languages": ["go"]}, rows, lambda r: 1)
    assert [t["id"] for t in tasks] == ["b"] and info["dropped"]["language not selected"] == 1

    class FakeApi:
        calls: ClassVar[list[str]] = []

        def get(self, path):
            self.calls.append(path)
            return None if "missing" in path else {"size": 2048}
    api = FakeApi()
    cache = tmp_path / "repo_sizes.json"
    sizes = cbt.cached_sizes(cache, api)
    assert sizes("o/r") == 2048 and sizes("o/r") == 2048 and sizes("o/missing") is None
    assert len(api.calls) == 2
    again = cbt.cached_sizes(cache, FakeApi())  # a rerun reads the file, not the API
    assert again("o/r") == 2048 and again("o/missing") is None and FakeApi.calls == api.calls

    checks = []
    f = cbt.cached_at_base(tmp_path / "fetchable.json", lambda url, sha, paths: checks.append(sha) or (paths[:1] if sha == "good" else None))
    good, bad = {"repo_url": "u", "base_commit": "good"}, {"repo_url": "u", "base_commit": "bad"}
    assert f(good, ["a.py", "b.py"]) == ["a.py"] and f(bad, ["a.py"]) is None
    assert f(good, ["a.py", "b.py"]) == ["a.py"] and checks == ["good", "bad"]
    assert cbt.cached_at_base(tmp_path / "fetchable.json", None)(bad, ["a.py"]) is None  # a rerun reads the file


def test_gold_files_missing_at_the_base_commit_leave_the_gold_and_can_drop_the_instance():
    rows = [row("two_of_three", files=("pkg/a.py", "pkg/b.py", "pkg/new.py")), row("one_left", files=("pkg/a.py", "pkg/new.py"))]
    present = lambda r, g: [p for p in g if p != "pkg/new.py"]
    tasks, info = cbt.select({"want": 5}, rows, lambda r: 1, present)
    assert [t["id"] for t in tasks] == ["two_of_three"] and tasks[0]["gold"] == ["pkg/a.py", "pkg/b.py"]
    assert info["dropped"]["gold files existing at the base commit not 2-6"] == 1
    assert info["gold_files_missing_at_base_in_selected"] == {"two_of_three": ["pkg/new.py"]}


def test_the_parquet_reader_gives_rows_the_selection_accepts(tmp_path):
    pq = pytest.importorskip("pyarrow.parquet")
    import pyarrow as pa
    rows = [row("a"), row("b", repo="x/y")]
    path = tmp_path / "d.parquet"
    pq.write_table(pa.Table.from_pylist(rows), path)
    got = cbt.read_parquet(str(path))
    assert [r["original_inst_id"] for r in got] == ["a", "b"]
    assert len(cbt.select({"want": 5}, got, lambda r: 1)[0]) == 2


# ---- the history-aware copies -------------------------------------------------------------------------------------

def git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args], check=True,
                       capture_output=True, text=True)
    return r.stdout.strip()


@pytest.fixture()
def source(tmp_path):
    """A repository with five commits c0..c4 on main and a side branch commit after c2; returns it and the shas."""
    src = tmp_path / "source"
    src.mkdir()
    git(src, "init", "-q", "-b", "main")
    shas = []
    for i in range(5):
        (src / "f.txt").write_bytes(f"v{i}\n".encode())
        (src / f"n{i}.txt").write_bytes(b"x\n")
        git(src, "add", "-A")
        git(src, "commit", "-qm", f"c{i}")
        shas.append(git(src, "rev-parse", "HEAD"))
        if i == 2:
            git(src, "branch", "side")
    git(src, "checkout", "-q", "side")
    (src / "side.txt").write_bytes(b"s\n")
    git(src, "add", "-A")
    git(src, "commit", "-qm", "side")
    git(src, "checkout", "-q", "main")
    return src, shas


def commits(repo: Path, *args: str) -> list[str]:
    return git(repo, "rev-list", *args).split()


def cfg_for(tmp_path, source_path, **extra):
    return {"work": str(tmp_path / "work"), "clone": str(source_path), "retries": 1, "retry_pause": 0,
            "arms": {"none": {"kind": "none"}}, **extra}


@pytest.mark.parametrize("depth,expected", [(1, 1), (2, 2), (3, 3), (50, 3)])
def test_a_copy_has_history_ending_at_the_base_commit_and_no_later_commit(tmp_path, source, depth, expected):
    src, shas = source
    base = shas[2]
    task = {"id": "t1", "base_sha": base}
    info = prep.prep_task(cfg_for(tmp_path, src, history_depth=depth), task)
    assert info["ok"], info
    copy = Path(tmp_path) / "work" / "t1" / "none"
    assert git(copy, "rev-parse", "HEAD") == base
    assert len(commits(copy, "HEAD")) == expected == info["history"]["history_commits"]
    assert commits(copy, "--all") == commits(copy, "HEAD")  # nothing but the base's ancestors is reachable
    assert info["history"]["reachable_commits"] == expected and info["history"]["nothing_after_base"]
    for later in (shas[3], shas[4], git(src, "rev-parse", "side")):
        assert subprocess.run(["git", "-C", str(copy), "cat-file", "-e", later], capture_output=True, check=False).returncode != 0
    assert (copy / "f.txt").read_bytes().strip() == b"v2" and not (copy / "n3.txt").exists()
    if depth >= 3:
        assert shas[0] in commits(copy, "HEAD")


def test_the_history_check_catches_a_reachable_later_commit(tmp_path, source):
    src, shas = source
    c = cfg_for(tmp_path, src)
    side = prep.check_history(c, src, shas[4])  # HEAD is the base, but the branch `side` reaches a commit outside its ancestry
    assert side["head_is_base"] and side["history_commits"] == 5 and side["reachable_commits"] == 6
    assert not side["nothing_after_base"]
    later = prep.check_history(c, src, shas[2])  # HEAD is two commits after this "base"
    assert not later["head_is_base"] and not later["nothing_after_base"]


def test_a_tasks_remote_overrides_the_configs_clone_and_the_default_depth_is_one(tmp_path, source):
    src, shas = source
    cfg = cfg_for(tmp_path, tmp_path / "does-not-exist")
    info = prep.prep_task(cfg, {"id": "t1", "base_sha": shas[3], "remote": str(src)})
    assert info["ok"] and info["history"]["history_commits"] == 1 and info["history_depth"] == 1
    with pytest.raises(ValueError):
        prep.source_of({}, {"id": "t"})


def test_each_kind_is_built_once_and_the_other_arms_of_that_kind_are_copies_of_it(tmp_path, source, monkeypatch):
    src, shas = source
    built = []

    def fake(kind):
        def build(cfg, arm):
            built.append((kind, arm.name))
            (arm / f"{kind}.built").write_bytes(b"1")
            return {"ok": True, "seconds": 0.1, "nodes": 7}
        return build
    monkeypatch.setitem(prep.BUILDERS, "graphify", fake("graphify"))
    monkeypatch.setitem(prep.BUILDERS, "verinoda", fake("verinoda"))
    arms = {"plain": {"kind": "none"}, "plain2": {"kind": "none"}, "g1": {"kind": "graphify"}, "v1": {"kind": "verinoda"},
            "v2": {"kind": "verinoda"}, "v3": {"kind": "verinoda"}}
    cfg = cfg_for(tmp_path, src, arms=arms, history_depth=3)
    info = prep.prep_task(cfg, {"id": "t1", "base_sha": shas[2]})
    assert info["ok"] and sorted(built) == [("graphify", "g1"), ("verinoda", "v1")]
    root = tmp_path / "work" / "t1"
    for arm in ("v1", "v2", "v3"):
        assert (root / arm / "verinoda.built").exists() and not (root / arm / "graphify.built").exists()
    assert (root / "g1" / "graphify.built").exists() and not (root / "plain2" / "graphify.built").exists()
    assert info["verinoda"]["nodes"] == 7 and info["graphify"]["nodes"] == 7 and set(info["arm_checks"]) == set(arms)
    assert all(c["history_commits"] == 3 and c["nothing_after_base"] for c in info["arm_checks"].values())
    # done: a second run neither fetches nor builds
    assert prep.prep_task(cfg, {"id": "t1", "base_sha": "0" * 40}) == info and len(built) == 2


def test_an_interrupted_run_resumes_at_the_first_unfinished_stage(tmp_path, source, monkeypatch):
    src, shas = source
    calls = {"n": 0}

    def flaky(cfg, arm):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"ok": False, "rc": 1}
        return {"ok": True, "seconds": 0.0, "nodes": 3}
    monkeypatch.setitem(prep.BUILDERS, "graphify", flaky)
    arms = {"plain": {"kind": "none"}, "g1": {"kind": "graphify"}, "g2": {"kind": "graphify"}}
    cfg = cfg_for(tmp_path, src, arms=arms)
    task = {"id": "t1", "base_sha": shas[1]}
    first = prep.prep_task(cfg, task)
    assert not first["ok"] and "failed" in first
    root = tmp_path / "work" / "t1"
    assert (root / "stage-base.json").exists() and (root / "stage-arm-plain.json").exists()
    assert not (root / "stage-arm-g1.json").exists()
    (root / "plain" / "keep.me").write_bytes(b"1")  # a finished stage is not redone
    second = prep.prep_task(cfg, task)
    assert second["ok"] and calls["n"] == 2 and (root / "plain" / "keep.me").exists() and (root / "g2").exists()


def test_the_old_four_arm_names_work_without_an_arms_mapping_and_an_old_done_file_counts_as_done(tmp_path):
    assert prep.arms_of({}) == {"none": "none", "graphify": "graphify", "verinoda_mod": "verinoda", "verinoda_setup": "verinoda"}
    with pytest.raises(ValueError):
        prep.arms_of({"arms": {"x": {"kind": "bogus"}}})
    root = tmp_path / "work" / "t1"
    root.mkdir(parents=True)
    old = {"id": "t1", "base_sha": "abc", "ok": True, "graphify": {"nodes": 5}}  # as written before "arms" existed
    (root / "done.json").write_bytes(json.dumps(old).encode())
    assert prep.prep_task({"work": str(tmp_path / "work"), "clone": "nowhere"}, {"id": "t1", "base_sha": "abc"}) == old


def test_a_task_that_fails_is_reported_and_does_not_stop_the_others(tmp_path, source, capsys):
    src, shas = source
    tasks = {"tasks": [{"id": "bad", "base_sha": "f" * 40}, {"id": "good", "base_sha": shas[2]},
                       {"id": "skipped", "base_sha": shas[1]}]}
    (tmp_path / "tasks.json").write_bytes(json.dumps(tasks).encode())
    cfg = cfg_for(tmp_path, src, tasks=str(tmp_path / "tasks.json"), workers=2, history_depth=2, only=["bad", "good", "nope"])
    (tmp_path / "cfg.json").write_bytes(json.dumps(cfg).encode())
    old = sys.argv
    sys.argv = ["commit_prep.py", str(tmp_path / "cfg.json")]
    try:
        assert prep.main() == 0
    finally:
        sys.argv = old
    report = {r["id"]: r for r in json.loads((tmp_path / "work" / "prep.json").read_text(encoding="utf-8"))}
    assert set(report) == {"bad", "good"} and report["good"]["ok"] and not report["bad"]["ok"] and "error" in report["bad"]
    assert not (tmp_path / "work" / "skipped").exists()
    assert "nope" in capsys.readouterr().err


def test_network_fetches_are_retried_and_a_step_has_a_timeout(tmp_path, monkeypatch):
    tries = []

    def step():
        tries.append(1)
        return (0, "ok", 0.0) if len(tries) == 3 else (1, "net", 0.0)
    assert prep.with_retries(step, 3, 0)[0] == 0 and len(tries) == 3
    tries.clear()
    assert prep.with_retries(lambda: (tries.append(1) or (1, "x", 0.0)), 3, 0)[0] == 1 and len(tries) == 3
    rc, out, _ = prep.run([sys.executable, "-c", "import time; time.sleep(30)"], None, {}, timeout=1)
    assert rc == 124 and "timeout" in out
    assert prep.run(["definitely-not-a-program-xyz"], None, {})[0] == 127
    assert prep.timeout_of({"timeouts": {"fetch": 5}}, "fetch") == 5 and prep.timeout_of({}, "graphify") == 3600


def test_a_long_instance_id_becomes_a_short_stable_task_id_so_a_working_copy_path_stays_short():
    short = "sveltejs__svelte-464"
    long_ = "instance_NodeBB__NodeBB-f48ed3658aab7be0f1165d4c1f89af48d7865189-v0495b863a912fbff5749c67e860612b91825407c"
    assert cbt.task_id(short, "sveltejs/svelte") == short
    a = cbt.task_id(long_, "NodeBB/NodeBB")
    assert a == cbt.task_id(long_, "NodeBB/NodeBB") and a.startswith("NodeBB-") and len(a) <= 32
    assert a != cbt.task_id(long_ + "x", "NodeBB/NodeBB")
    tasks, _ = cbt.select({"want": 5}, [row(long_, repo="NodeBB/NodeBB")], {"NodeBB/NodeBB": 1000})
    assert tasks[0]["id"] == a and tasks[0]["orig_id"] == long_ and tasks[0]["ref"] == f"Set__{long_}"
    tasks, _ = cbt.select({"want": 5}, [row(short, repo="sveltejs/svelte")], {"sveltejs/svelte": 1000})
    assert tasks[0]["id"] == short and tasks[0]["orig_id"] == short
