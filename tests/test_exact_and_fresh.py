"""Names resolve exactly and a stale index is never silent (review 2026-09-26, gaps 2 and 6).

The repros are the evaluators': a frozen copy of the project chosen by trace/node_inspect/plan check,
a made-up impact target that "affected" 80 symbols, a receiver call linked from the copy into the
project, two index builds colliding (and a hint to run `scan --force`), query and trace answering
silently from an older tree, and analyze spending its whole budget on the refresh.
"""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import json  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import (  # noqa: E402
    analysis,
    architecture_map,
    buildlock,
    copies,
    freshness,
    index,
    naming,
    question_plan,
    retrieval,
    workflow,
)
from verinoda.store import open_store  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")

ROOT = Path(__file__).resolve().parents[1]
MODULES = ["billing", "ledger", "invoice", "tax", "refund", "report", "export_csv", "currency", "discount"]


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args],
                   cwd=cwd, check=True, capture_output=True)


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


def _module(name: str) -> str:
    return (f"def {name}_open(x):\n    return x + 1\n\n\ndef {name}_close(x):\n    return x - 1\n\n\n"
            f"def {name}_total(items):\n    return sum(items)\n")


CLAIMS = ('"""Claims."""\n\n\nclass Claims:\n    def set_status(self, s):\n        return s\n\n'
          '    def get(self, cid):\n        return cid\n\n\ndef assess_change(c):\n    return c\n\n\n'
          'STATUS_LIMIT = 5\n')


def _cli(pkg: str) -> str:
    return (f"from {pkg}.billing import billing_open\nfrom {pkg}.claims import Claims, assess_change\n\n\n"
            "def cmd_update(cl: Claims):\n    cl.set_status(1)\n    return assess_change(billing_open(1))\n")


def _make_project(root: Path) -> Path:
    """app/ (the product), tests/ using it, and bench/corpora/old/oldapp/: a frozen copy of app/ under
    another package name, which nothing outside it uses (scan detects it as a copy)."""
    for m in MODULES:
        _write(root, f"app/{m}.py", _module(m))
        _write(root, f"tests/test_{m}.py", f"from app.{m} import {m}_open\n\n\ndef test_{m}_open():\n"
                                           f"    assert {m}_open(1) == 2\n")
        _write(root, f"bench/corpora/old/oldapp/{m}.py", _module(m))
    for pkg, base in (("app", "app"), ("oldapp", "bench/corpora/old/oldapp")):
        _write(root, f"{base}/__init__.py", "")
        _write(root, f"{base}/claims.py", CLAIMS)
        _write(root, f"{base}/cli.py", _cli(pkg))
    # one name the product itself defines twice
    _write(root, "app/extra_a.py", "def shared_name():\n    return 1\n")
    _write(root, "app/extra_b.py", "def shared_name():\n    return 2\n")
    _write(root, "tests/test_cli.py", "from app.claims import Claims\nfrom app.cli import cmd_update\n\n\n"
                                      "def test_cmd_update():\n    assert cmd_update(Claims()) == 2\n")
    _write(root, ".gitignore", "*.log\n.verinoda/\n")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    workflow.init(root)
    st = open_store(root)
    try:
        res = workflow.scan(st, root)
    finally:
        st.close()
    assert not res.get("error"), res.get("error")
    return root


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """A scanned project, copied per test that edits it (the scan runs once)."""
    return _make_project(tmp_path_factory.mktemp("exact") / "proj")


@pytest.fixture
def proj(built, tmp_path):
    dst = tmp_path / "proj"
    shutil.copytree(built, dst)  # copy2 keeps modification times: the stat cache stays valid
    return dst


@pytest.fixture(scope="module")
def g(built):
    return index.load(built)


# -- one exact resolver; copies give way ---------------------------------------------------------

def test_the_copy_is_detected(built):
    assert [c["path"] for c in copies.load(built)] == ["bench/corpora/old/oldapp/"]


@pytest.mark.parametrize("target", ["claims.assess_change", "assess_change", "app/claims.py::assess_change"])
def test_trace_resolves_to_the_project_not_its_copy(g, target):
    # review: `trace cmd_query search_index.rank` answered with hops inside the frozen copy
    res = retrieval.trace(g, "cmd_update", target)
    assert res["status"] == "found", res
    hops = res["paths"][0]
    assert all(not (h["at"] or "").startswith("bench/") for h in hops)
    assert res["resolved"]["source"]["at"].startswith("app/cli.py")
    assert res["resolved"]["target"]["at"].startswith("app/claims.py")
    assert "fuzzy" not in res and "ambiguous" not in res


def test_naming_the_copy_uses_it_and_says_so(g):
    res = retrieval.trace(g, "oldapp.cli.cmd_update", "oldapp.claims.assess_change")
    assert res["status"] == "found"
    assert res["resolved"]["target"]["at"].startswith("bench/corpora/old/oldapp/claims.py")
    r = naming.resolve(g, "bench/corpora/old/oldapp/claims.py::assess_change")
    assert r.exact and g.file(r.node) == "bench/corpora/old/oldapp/claims.py"


def test_a_name_the_project_defines_twice_is_ambiguous_and_nothing_is_picked(g):
    res = retrieval.trace(g, "cmd_update", "shared_name")
    assert res["status"] == "ambiguous" and res["paths"] == [] and res["resolved"]["target"] is None
    assert {h["at"].split(":")[0] for h in res["hints"]["target"]} == {"app/extra_a.py", "app/extra_b.py"}
    assert "names 2 symbols" in res["ambiguous"]["target"]
    exact = retrieval.trace(g, "cmd_update", "app/extra_a.py::shared_name")
    assert exact["status"] == "no directed path" and exact["resolved"]["target"]["at"].startswith("app/extra_a.py")


def test_a_constant_is_said_where_it_is_and_never_traced_through_a_similar_name(g, built):
    # review: trace "resolved by similarity" a code name that is no symbol of the index
    res = retrieval.trace(g, "cmd_update", "claims.STATUS_LIMIT", mode="any")
    assert res["status"] == "unresolved" and res["paths"] == [] and "fuzzy" not in res
    assert "`STATUS_LIMIT` occurs at app/claims.py:16" in res["not_a_symbol"]["target"]
    from verinoda.mcp.server import AtlasTools

    ni = AtlasTools(built).node_inspect("claims.STATUS_LIMIT")
    assert ni["error"] == "not_a_symbol" and "no similar name is used instead" in ni["message"]
    words = retrieval.trace(g, "cmd update", "assess change")  # plain words may still go by similarity
    assert words["status"] == "found" and set(words["fuzzy"]) == {"source", "target"}


def test_node_inspect_uses_the_exact_resolver(built):
    from verinoda.mcp.server import AtlasTools

    t = AtlasTools(built)
    res = t.node_inspect("assess_change")  # review: node_inspect rank resolved to the copy's evidence.py
    assert res["node"]["file"] == "app/claims.py" and res["node"]["resolution"] == "label"
    tied = t.node_inspect("shared_name")
    assert tied["error"] == "ambiguous" and len(tied["candidates"]) == 2 and "path/file.py::symbol" in tied["hint"]
    missing = t.node_inspect("app/claims.py::no_such_thing")
    assert missing["error"] == "not_found" and "no symbol named `no_such_thing` in app/claims.py" in missing["message"]


def test_plan_check_never_asks_between_a_copy_and_its_original(g, built):
    # review: plan check for "What calls assess_change?" asked the user to choose the copy's or the original
    plan = question_plan.draft("What calls assess_change?", g)
    res = question_plan.check(plan, g, built)
    assert res["status"] == "ready" and not res["clarifications"], res["clarifications"]
    lk = next(lk for lk in res["links"] if lk["text"] == "assess_change")
    assert lk["status"] == "linked" and g.file(lk["nodes"][0]) == "app/claims.py"
    assert any(g.file(n).startswith("bench/") for n in lk.get("set_aside") or [])


def test_impact_of_a_made_up_target_is_unresolved_not_eighty_symbols(g, built):
    # review: impact target `...::NoSuchThing` gave 80 affected, `path::Class.method` gave 0
    res = architecture_map.impact(g, ["app/claims.py::NoSuchThing"])
    assert res["unresolved"] == ["app/claims.py::NoSuchThing"] and res["affected_symbols"] == []
    assert res["resolution"][0]["status"] == "not_found"
    real = architecture_map.impact(g, ["app/claims.py::Claims.set_status"])
    assert real["unresolved"] == [] and "app/cli.py" in real["affected_files"]
    assert not any(f.startswith("bench/") for f in real["affected_files"])
    tied = architecture_map.impact(g, ["shared_name"])
    assert tied["unresolved"] == ["shared_name"] and tied["resolution"][0]["status"] == "ambiguous"
    assert len(tied["resolution"][0]["candidates"]) == 2


def _cli_run(repo: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": str(ROOT), "VERINODA_NO_AUTO_INDEX": "1"}
    return subprocess.run([sys.executable, "-m", "verinoda", *args, "--repo", str(repo)], cwd=str(repo.parent),
                          capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=300)


@pytest.mark.e2e
def test_cli_impact_and_trace_exit_non_zero_with_candidates(built):
    r = _cli_run(built, "map", "--view", "impact", "--target", "app/claims.py::NoSuchThing")
    assert r.returncode == 2 and "not used (not_found)" in r.stdout and "did not name one symbol" in r.stderr
    r = _cli_run(built, "trace", "cmd_update", "shared_name")
    assert r.returncode == 2 and "names several symbols (none was picked)" in r.stdout
    assert "app/extra_a.py" in r.stdout and "app/extra_b.py" in r.stdout


def test_the_receiver_pass_stays_inside_the_package_it_imports_from(g):
    # review: the copy's `cl.set_status` was linked to the project's Claims.set_status
    recv = [(u, v) for u, v, d in g.G.edges(data=True) if d.get("_origin") == index.RECEIVER_ORIGIN]
    assert recv, "no receiver edges at all"
    for u, v in recv:
        assert g.file(u).startswith("bench/") == g.file(v).startswith("bench/"), (g.file(u), g.file(v))
    by_caller = {g.file(u): g.file(v) for u, v in recv if g.label(v).strip(".()") == "set_status"}
    assert by_caller == {"app/cli.py": "app/claims.py",
                         "bench/corpora/old/oldapp/cli.py": "bench/corpora/old/oldapp/claims.py"}


def test_visible_class_prefers_own_file_then_imports_and_never_crosses_a_copy(g):
    imports = index._file_imports(g)
    roots = index._copy_roots(g)
    cands = [n for n, d in g.G.nodes(data=True) if d.get("label") == "Claims" and d.get("_callable_class")]
    assert len(cands) == 2
    pick = index._visible_class(g, "app/cli.py", cands, imports, roots)
    assert g.file(pick) == "app/claims.py"
    pick = index._visible_class(g, "bench/corpora/old/oldapp/cli.py", cands, imports, roots)
    assert g.file(pick) == "bench/corpora/old/oldapp/claims.py"
    lone = [c for c in cands if g.file(c).startswith("bench/")]
    assert index._visible_class(g, "app/cli.py", lone, imports, roots) is None


# -- one build at a time -------------------------------------------------------------------------

def _hold(repo: Path, seconds: float, purpose: str = "test build") -> tuple[threading.Thread, threading.Event]:
    started = threading.Event()

    def run():
        with buildlock.build_lock(repo, wait=0, purpose=purpose):
            started.set()
            time.sleep(seconds)

    th = threading.Thread(target=run, daemon=True)
    th.start()
    assert started.wait(5)
    return th, started


def test_a_second_update_that_may_not_wait_does_nothing_and_says_who_builds(proj):
    th, _ = _hold(proj, 1.5)
    try:
        (proj / "app" / "billing.py").write_text(_module("billing") + "\n# edited\n", encoding="utf-8")
        st = open_store(proj)
        try:
            before = st.latest_snapshot()["id"]
            res = workflow.update(st, proj, wait=0)
            assert res["mode"] == "busy" and res["snapshot"]["id"] == before
        finally:
            st.close()
        assert "another index build of this project is running (test build" in res["error"]
        assert "--force" not in json.dumps(res)
        assert buildlock.is_locked(proj)
    finally:
        th.join(5)
    assert not buildlock.is_locked(proj)


def test_a_second_update_waits_for_the_first_then_runs(proj):
    th, _ = _hold(proj, 0.6)
    seen = []
    st = open_store(proj)
    try:
        (proj / "app" / "tax.py").write_text(_module("tax") + "\n# edited\n", encoding="utf-8")
        res = workflow.update(st, proj, wait=30, on_wait=seen.append)
    finally:
        st.close()
        th.join(5)
    assert res["mode"] == "incremental" and "app/tax.py" in res["changed"]["modified"]
    assert seen and seen[0]["purpose"] == "test build"


def test_the_lock_is_reentrant_in_one_thread(proj):
    with buildlock.build_lock(proj, wait=0, purpose="outer"):
        with buildlock.build_lock(proj, wait=0, purpose="inner"):
            assert buildlock.holder(proj)["purpose"] == "outer"
    assert buildlock.holder(proj) is None


@pytest.mark.e2e
def test_two_cli_updates_do_not_collide_and_never_suggest_force(proj):
    # review: two `update` runs a second apart: [WinError 2] and "hint: verinoda scan ... --force"
    holder = subprocess.Popen(
        [sys.executable, "-c", "import sys, time; from verinoda import buildlock\n"
                               "with buildlock.build_lock(sys.argv[1], wait=0, purpose='update'):\n"
                               "    print('locked', flush=True); time.sleep(3)", str(proj)],
        cwd=str(proj.parent), env={**os.environ, "PYTHONPATH": str(ROOT)}, stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "locked"
        assert buildlock.is_locked(proj)
        r = _cli_run(proj, "update")
    finally:
        holder.wait(30)
    assert r.returncode == 0, r.stderr
    assert "waiting for another index build of this project (update" in r.stderr
    assert "--force" not in r.stdout + r.stderr


def test_the_ui_watcher_skips_a_busy_round_and_tries_again(tmp_path):
    from verinoda.ui import server as uiserver

    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    answers = [{"mode": "busy", "error": "another index build of this project is running"}, {"mode": "noop"}]
    calls = []

    def run_update():
        calls.append(time.perf_counter())
        return answers[min(len(calls), len(answers)) - 1]

    w = uiserver.Watcher(tmp_path, interval=0.05, run_update=run_update)
    w.start()
    try:
        time.sleep(0.2)
        (tmp_path / "b.py").write_text("y = 1\n", encoding="utf-8")
        deadline = time.perf_counter() + 60  # a slow listing slows the looks down (10x its time)
        while w.updates < 1 and time.perf_counter() < deadline:
            time.sleep(0.05)
        assert len(calls) >= 2 and w.updates == 1 and w.error is None
    finally:
        w.stop.set()
        w.join(2)


# -- staleness is never silent -------------------------------------------------------------------

def test_a_fresh_index_is_reported_fresh_and_a_touch_is_not_a_change(proj):
    assert freshness.check(proj)["count"] == 0
    p = proj / "app" / "ledger.py"
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))  # same bytes, new time
    assert freshness.check(proj)["count"] == 0


def test_edits_new_files_and_deletions_are_listed_and_ignored_files_are_not(proj):
    (proj / "app" / "billing.py").write_text(_module("billing") + "\n# edited\n", encoding="utf-8")
    _write(proj, "app/newmod.py", "def brand_new():\n    return 1\n")
    _write(proj, "app/run.log", "noise\n")          # git-ignored
    _write(proj, "app/sub/deep.py", "X = 1\n")      # a new folder
    (proj / "app" / "refund.py").unlink()
    res = freshness.check(proj)
    assert res["files"] == ["app/billing.py", "app/newmod.py", "app/sub/deep.py", "app/refund.py"]
    assert (res["modified"], res["added"], res["removed"]) == (1, 2, 1)
    assert "app/run.log" not in res["files"]


def test_query_says_which_files_changed_and_names_the_index_does_not_have(proj):
    # review: after an edit `query "where is frobnicate_widget defined?"` returned an unrelated test, silently
    cli = proj / "app" / "cli.py"
    cli.write_text(cli.read_text(encoding="utf-8") + "\n\ndef frobnicate_widget():\n    return 42\n",
                   encoding="utf-8")
    g = index.load(proj)
    res = retrieval.retrieve(g, "where is frobnicate_widget defined?", retrieval.Budget(max_items=8))
    retrieval.attach_freshness(res, g, freshness.check(proj))
    assert res["stale_count"] == 1 and res["stale_files"] == ["app/cli.py"]
    assert res["not_in_index"] == [{"name": "frobnicate_widget", "at": "app/cli.py:10"}]
    text = retrieval.render_text(res)
    head = text.splitlines()[:4]
    assert any(ln.startswith("not in the index yet") and "`frobnicate_widget` at app/cli.py:10" in ln for ln in head)
    assert any(ln.startswith("1 file(s) changed since the index") and "app/cli.py" in ln for ln in head)


def test_trace_never_substitutes_a_similar_name_for_one_in_a_changed_file(proj):
    # review: trace said "resolved by similarity to Widget" for a function added to an edited file
    cli = proj / "app" / "cli.py"
    cli.write_text(cli.read_text(encoding="utf-8") + "\n\ndef frobnicate_widget():\n    return 42\n",
                   encoding="utf-8")
    g = index.load(proj)
    fresh = freshness.check(proj)
    res = retrieval.trace(g, "cmd_update", "frobnicate_widget", stale=fresh["files"])
    assert res["status"] == "unresolved" and res["resolved"]["target"] is None and "fuzzy" not in res
    assert res["not_indexed"]["target"].startswith("`frobnicate_widget` is not in the index yet: it occurs at "
                                                   "app/cli.py:10")
    assert "verinoda update" in res["next_step"] and "target" not in res["hints"]  # no similar names offered


@pytest.mark.e2e
def test_cli_query_and_trace_print_the_stale_files(proj):
    (proj / "app" / "invoice.py").write_text(_module("invoice") + "\n# edited\n", encoding="utf-8")
    r = _cli_run(proj, "query", "invoice total")
    assert r.returncode == 0 and "1 file(s) changed since the index" in r.stdout and "app/invoice.py" in r.stdout
    r = _cli_run(proj, "query", "invoice total", "--json")
    data = json.loads(r.stdout)
    assert data["stale_files"] == ["app/invoice.py"] and data["stale_count"] == 1
    r = _cli_run(proj, "trace", "cmd_update", "assess_change")
    assert r.returncode == 0 and "note: 1 file(s) changed since the index" in r.stdout


def test_mcp_read_tools_carry_the_stale_files(proj):
    from verinoda.mcp.server import AtlasTools

    t = AtlasTools(proj)
    assert "stale_files" not in t.node_inspect("assess_change")
    (proj / "app" / "currency.py").write_text(_module("currency") + "\n# edited\n", encoding="utf-8")
    q = t.project_query("currency open")
    assert "1 file(s) changed since the index" in q["text"] and "app/currency.py" in q["text"]
    for res in (t.node_inspect("assess_change"), t.relation_trace("cmd_update", "assess_change"),
                t.map_view("hierarchy"), t.map_view("impact", ["app/claims.py"])):
        assert res.get("stale_files") == ["app/currency.py"] and res["stale_count"] == 1, res.keys()


# -- analyze: the refresh does not eat the answer ------------------------------------------------

def test_analyze_gives_the_refresh_time_back_to_the_budget(proj, monkeypatch):
    # review: analyze after a one-line edit spent its whole 60 s budget refreshing and answered unmet
    real = workflow.update

    def slow_update(*a, **kw):
        time.sleep(6.0)
        return real(*a, **kw)

    monkeypatch.setattr(workflow, "update", slow_update)
    (proj / "app" / "claims.py").write_text(CLAIMS + "\n# edited\n", encoding="utf-8")
    st = open_store(proj)
    try:
        t0 = time.perf_counter()
        res = analysis.analyze(st, proj, "Where is assess_change defined?", budget=analysis.Budget(seconds=5.0))
        wall = time.perf_counter() - t0
    finally:
        st.close()
    ref = res["index_refresh"]
    assert ref["ran"] is True and ref["seconds"] >= 6.0
    assert res["usage"]["exhausted"] is None or "time" not in res["usage"]["exhausted"], res["usage"]
    assert res["usage"]["elapsed_s"] <= wall - 6.0 + 0.5  # the refresh is not in the answer's time


def test_analyze_answers_from_the_previous_index_when_a_refresh_would_be_slow(proj, monkeypatch):
    monkeypatch.setattr(analysis, "REFRESH_SMALL_PROJECT", 0)  # the fixture is small; judge it as a big one
    buildlock.record_build(proj, graph_seconds=120.0, files=40)
    (proj / "app" / "claims.py").write_text(CLAIMS.replace("return c\n", "return c  # edited\n"),
                                            encoding="utf-8")
    st = open_store(proj)
    try:
        before = st.latest_snapshot()["id"]
        res = analysis.analyze(st, proj, "Where is assess_change defined?")
        assert st.latest_snapshot()["id"] == before  # nothing was rebuilt
    finally:
        st.close()
    ref = res["index_refresh"]
    assert ref["ran"] is False and "last graph build took 120 s" in ref["skipped"]
    assert ref["stale_files"] == ["app/claims.py"] and ref["stale_count"] == 1
    u = next(u for u in res["unknowns"] if u["question"].startswith("does the index describe"))
    assert "app/claims.py" in u["why"] and "previous index" in u["why"]
    cited = [c for c in res["claims"] if any(":app/claims.py:" in e for e in c["evidence"])]
    others = [c for c in res["claims"] if c not in cited]
    assert cited and all(any("changed since the index" in x for x in c["uncertainties"]) for c in cited)
    assert not any(any("changed since the index" in x for x in c["uncertainties"]) for c in others)


def test_analyze_does_not_wait_for_another_build(proj, monkeypatch):
    monkeypatch.setattr(analysis, "REFRESH_SMALL_PROJECT", 0)  # a big project: no wait at all
    (proj / "app" / "claims.py").write_text(CLAIMS + "\n# edited\n", encoding="utf-8")
    th, _ = _hold(proj, 3.0, purpose="update")
    try:
        st = open_store(proj)
        try:
            t0 = time.perf_counter()
            res = analysis.analyze(st, proj, "Where is assess_change defined?")
            took = time.perf_counter() - t0
        finally:
            st.close()
    finally:
        th.join(10)
    ref = res["index_refresh"]
    assert ref["skipped"] == "another index build is running" and ref["busy"]["holder"]["purpose"] == "update"
    assert ref["seconds"] < 1.0 and took > 0 and "--force" not in json.dumps(res)
    assert ref["stale_files"] == ["app/claims.py"]


def test_analyze_of_a_small_project_waits_briefly_for_another_build(proj):
    (proj / "app" / "claims.py").write_text(CLAIMS + "\n# edited\n", encoding="utf-8")
    th, _ = _hold(proj, 1.0, purpose="update")
    try:
        st = open_store(proj)
        try:
            res = analysis.analyze(st, proj, "Where is assess_change defined?")
        finally:
            st.close()
    finally:
        th.join(10)
    ref = res["index_refresh"]
    assert ref["ran"] is True and ref["mode"] == "incremental" and "skipped" not in ref


def test_mcp_analyze_starts_a_background_update_when_the_refresh_would_be_slow(proj, monkeypatch):
    from verinoda.mcp import server as mcp_server

    started = []

    class FakeProc:
        pid = 4242

        def poll(self):
            return None

    def fake_popen(argv, **kw):
        started.append(argv)
        return FakeProc()

    monkeypatch.setattr(mcp_server, "_spawn", fake_popen)
    monkeypatch.setattr(analysis, "REFRESH_SMALL_PROJECT", 0)
    buildlock.record_build(proj, graph_seconds=120.0, files=40)
    (proj / "app" / "claims.py").write_text(CLAIMS + "\n# edited\n", encoding="utf-8")
    t = mcp_server.AtlasTools(proj)
    res = t.analyze("Where is assess_change defined?")
    ref = res["index_refresh"]
    assert ref["ran"] is False and ref["background_update"].startswith("started (pid 4242)")
    argv = started[0]
    assert argv[1:3] == ["-I", "-c"] and argv[-3:] == ["update", "--repo", str(proj.resolve())]
    assert t._update_in_background() == "running (pid 4242)"  # one at a time


_EVIL = "import pathlib\npathlib.Path(r'{marker}').write_text('project code ran')\n"


@pytest.mark.e2e
def test_the_background_update_never_imports_a_verinoda_the_project_ships(proj):
    # review: the child ran `python -m verinoda` with its working directory in .verinoda/index, so a
    # project shipping .verinoda/index/verinoda/__main__.py had its own code executed
    from verinoda.mcp import server as mcp_server

    marker = proj.parent / "EVIL_RAN.txt"
    for base in (proj / ".verinoda" / "index" / "verinoda", proj / "verinoda"):
        for mod in ("__init__.py", "__main__.py", "cli.py"):
            _write(base, mod, _EVIL.format(marker=marker))
    (proj / "app" / "claims.py").write_text(CLAIMS + "\n# edited\n", encoding="utf-8")
    t = mcp_server.AtlasTools(proj)
    assert t._update_in_background().startswith("started (pid ")
    assert t._background.wait(240) == 0
    assert not marker.exists(), marker.read_text(encoding="utf-8")
    assert "app/claims.py" not in freshness.check(proj)["files"]  # the real update ran
