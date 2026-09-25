"""The debug ledger end to end: real isolated runs on git copies of examples/orders_app."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import hashlib  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import cli, debug, experiments, treestate  # noqa: E402
from verinoda.claims import Claims  # noqa: E402
from verinoda.store import open_store  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "orders_app"
PT = ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]

pytestmark = [pytest.mark.experiment, pytest.mark.skipif(shutil.which("git") is None, reason="git not available")]


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args],
                          cwd=cwd, check=True, capture_output=True, text=True).stdout


def _repo(tmp_path: Path, name: str = "oa") -> Path:
    dst = tmp_path / name
    shutil.copytree(EXAMPLE, dst, ignore=shutil.ignore_patterns(".verinoda", "__pycache__", "*.pyc", "*.db"))
    _git(dst, "init", "-q")
    _git(dst, "add", "-A")
    _git(dst, "commit", "-q", "-m", "init")
    return dst


def _sub(repo: Path, rel: str, old: str, new: str) -> None:
    p = repo / rel
    t = p.read_text(encoding="utf-8")
    assert old in t
    p.write_bytes(t.replace(old, new, 1).encode("utf-8"))


def _digest(repo: Path) -> dict[str, str]:
    return {p.relative_to(repo).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(repo.rglob("*")) if p.is_file() and ".verinoda" not in p.relative_to(repo).parts}


def _rules(res: dict, strength: str = "definitive") -> list[str]:
    return [f["rule"] for f in res["loop"] if f["strength"] == strength]


def test_a_looping_session_is_stopped_and_the_differential_finds_the_cause(tmp_path):
    repo = _repo(tmp_path)
    _sub(repo, "orders/pricing.py", 'i["qty"]', 'i["quantity"]')
    st = open_store(repo)
    s0 = debug.start(st, repo, "order totals fail after the rename", PT)
    assert s0["outcome"] == "fail" and s0["signature"]["status"] == "parsed"
    assert {f["at_symbol"] for f in s0["signature"]["failures"]} == {"orders/pricing.py::compute_total"}
    assert s0["tree"]["changed_files"] == [{"path": "orders/pricing.py", "status": "modified",
                                            "symbols": ["compute_total"]}]
    assert Path(s0["tree"]["patch"]).read_text(encoding="utf-8").count('i["quantity"]') == 1
    before = _digest(repo)
    _sub(repo, "tests/test_pricing.py", '"qty": 2', '"quantity": 2')
    a1 = debug.attempt(st, repo, hypothesis="the pricing test data still uses the old key")
    # fewer failures after a test was changed say nothing about the code: progress unknown
    assert _rules(a1) == ["test_edited"] and a1["stop"] and a1["progress"] == "unknown"
    assert a1["questions_for_human"] and a1["strategies"][0]["id"] == "differential"
    q = a1["questions_for_human"][0]
    assert q["test_side"] == ['tests/test_pricing.py:13: assert compute_total([{"price": 10.0, "qty": 2}]) == 20.0']
    assert q["code_side"][0].startswith("orders/pricing.py:7 (compute_total; KeyError raised at orders/pricing.py:7")
    _sub(repo, "orders/pricing.py", 'i["quantity"]', 'i.get("quantity", 0)')
    a2 = debug.attempt(st, repo, hypothesis="a missing quantity should default to zero")
    assert set(_rules(a2, "heuristic")) >= {"error_moved", "masking"} and not a2["stop"]
    _sub(repo, "orders/pricing.py", 'i.get("quantity", 0)', 'i["quantity"]')
    a3 = debug.attempt(st, repo, hypothesis="index quantity directly again")
    assert "tree_reverted" in _rules(a3) and a3["stop"] and a3["tree"]["hash"] == a1["tree"]["hash"]
    d = debug.differential(st, repo)
    assert d["outcome_at_base"] == "pass" and d["hunks"][0]["at"] == "orders/pricing.py:7"
    assert d["hunks"][0]["session_edit"] is False
    status = debug.status(st, repo)
    assert [a["n"] for a in status["attempts"]] == [0, 1, 2, 3, 4] and status["latest"]["stop"]
    after = {k: v for k, v in _digest(repo).items() if k in before}
    changed_by_test = {"tests/test_pricing.py", "orders/pricing.py"}
    assert {k for k in before if before[k] != after.get(k)} <= changed_by_test  # only the test's own edits
    assert "fixed" not in json.dumps(status).lower()
    obs = debug.observe(st, repo)  # instrumented run: are the edits reached, and how does the test get there
    assert obs["trace"]["complete"] and obs["edits_reached"]["complete_trace"]
    assert obs["edits_reached"]["by_failing_tests"]["orders/pricing.py::compute_total"] == [
        "tests/test_service.py::test_place_and_fetch_roundtrip"]
    assert obs["chain"] == ["tests/test_service.py::test_place_and_fetch_roundtrip", "orders/service.py::place_order",
                            "orders/pricing.py::compute_total"]
    d2 = debug.differential(st, repo, trace=True)  # the failing tree is traced (observe); the base run is traced now
    calls = d2["trace_diff"]["tests"]["tests/test_service.py::test_place_and_fetch_roundtrip"]
    assert "orders/pricing.py::compute_total -> orders/pricing.py::apply_discount" in calls["only_when_passing"]
    assert calls["only_when_failing"] == []


def test_closing_needs_a_passing_attempt_on_the_current_tree(tmp_path):
    repo = _repo(tmp_path)
    _sub(repo, "orders/pricing.py", 'i["qty"]', 'i["quantity"]')
    st = open_store(repo)
    debug.start(st, repo, "rename", PT)
    with pytest.raises(debug.DebugError):
        debug.close(st, repo, resolved_by=0)  # attempt 0 failed
    _sub(repo, "orders/pricing.py", 'i["quantity"]', 'i["qty"]')
    ok = debug.attempt(st, repo, hypothesis="revert the key")
    assert ok["outcome"] == "pass" and ok["result"].startswith("the repro command passed at tree")
    (repo / "orders" / "extra.py").write_text("X = 1\n", encoding="utf-8")
    with pytest.raises(debug.DebugError, match="changed since attempt 1"):
        debug.close(st, repo, resolved_by=1)
    (repo / "orders" / "extra.py").unlink()
    closed = debug.close(st, repo, resolved_by=1, note="key reverted")
    assert closed["status"] == "resolved" and "passed at tree" in closed["result"]
    with pytest.raises(debug.DebugError):
        debug.attempt(st, repo, closed["session"], hypothesis="more")


def test_agent_reported_runs_are_labelled_and_never_verify(tmp_path):
    repo = _repo(tmp_path)
    st = open_store(repo)
    log = ("WispTests > wispTicks() FAILED\n    java.lang.IllegalStateException: boom\n"
           "        at com.example.Foo.run(Foo.java:3)\n")
    with pytest.raises(experiments.ExperimentRefused):
        debug.start(st, repo, "gradle fails", ["gradlew", "test"])  # not allowlisted: Verinoda will not run it
    s = debug.start(st, repo, "gradle fails", ["gradlew", "test"], observed_output=log, exit_code=1)
    assert s["run_by"] == "agent" and s["experiment_id"] is None and s["outcome"] == "fail"
    row = st.one("SELECT * FROM debug_attempts WHERE session_id = ?", (s["session"],))
    assert row["output_sha256"] == hashlib.sha256(log.encode()).hexdigest()
    ev = st.evidence(s["evidence_id"])
    assert ev["source_type"] == "agent_report" and ev["meta"]["run_by"] == "agent"
    with pytest.raises(debug.DebugError, match="needs the command you ran"):
        debug.attempt(st, repo, hypothesis="fixed the null", observed_output="BUILD SUCCESSFUL\n", exit_code=0)
    ok = debug.attempt(st, repo, hypothesis="fixed the null", observed_output="BUILD SUCCESSFUL\n", exit_code=0,
                       command=["gradlew", "test"])
    assert ok["outcome"] == "pass" and any("did not run" in x for x in ok["not_run"])
    assert ok["result"].startswith("the repro command passed at tree")
    from verinoda.claims import ClaimRuleError

    cl = Claims(st, repo)
    c = cl.create("the gradle tests pass", project="oa", snapshot=None, status="unknown")
    cl.attach(c["id"], ok["evidence_id"], "supports")
    with pytest.raises(ClaimRuleError):
        cl.set_status(c["id"], "experiment_verified", reason="the agent reported a passing run")
    assert st.claim(c["id"])["status"] != "experiment_verified"


def test_bisect_finds_the_first_failing_commit(tmp_path):
    repo = _repo(tmp_path)
    _sub(repo, "tests/test_pricing.py", "def test_compute_total():",
         "def test_threshold_is_not_discounted():\n    assert apply_discount(100.0) == 100.0\n\n\ndef test_compute_total():")
    _git(repo, "commit", "-qam", "c1 threshold test")
    good = _git(repo, "rev-parse", "HEAD").strip()
    _sub(repo, "README.md", "# ", "# Orders: ")
    _git(repo, "commit", "-qam", "c2 readme")
    _sub(repo, "orders/pricing.py", "if subtotal > DISCOUNT_THRESHOLD:", "if subtotal >= DISCOUNT_THRESHOLD:")
    _git(repo, "commit", "-qam", "c3 inclusive threshold")
    bad_commit = _git(repo, "rev-parse", "HEAD").strip()
    _sub(repo, "orders/config.py", "Runtime configuration", "Runtime configuration (ORDERS_*)")
    _git(repo, "commit", "-qam", "c4 config")
    st = open_store(repo)
    s = debug.start(st, repo, "threshold test fails", PT + ["tests/test_pricing.py"])
    assert s["outcome"] == "fail" and not s["tree"]["changed_files"]
    b = debug.bisect(st, repo, good=good)
    assert b["status"] == "found" and b["first_bad_commit"]["commit"] == bad_commit
    # the bad end is the clean baseline (recorded); the good end and the midpoints are run
    assert b["hunks"][0]["at"] == "orders/pricing.py:13" and b["runs_total"] <= 4
    assert b["runs"][0] == {"commit": s["base"]["commit"], "outcome": "fail", "attempt": 0, "recorded": True}
    auto = debug.bisect(st, repo)  # no --good: steps back through history
    assert auto["status"] == "found" and auto["first_bad_commit"]["commit"] == bad_commit
    with pytest.raises(ValueError):
        debug.bisect(st, repo, good="--output=x")


def test_bisect_can_lay_the_current_test_over_old_commits(tmp_path):
    repo = _repo(tmp_path)
    good = _git(repo, "rev-parse", "HEAD").strip()
    _sub(repo, "orders/pricing.py", "if subtotal > DISCOUNT_THRESHOLD:", "if subtotal >= DISCOUNT_THRESHOLD:")
    _git(repo, "commit", "-qam", "inclusive threshold")
    bad_commit = _git(repo, "rev-parse", "HEAD").strip()
    _sub(repo, "README.md", "# ", "# Orders: ")
    _git(repo, "commit", "-qam", "readme")
    # the regression test exists only in the working tree: without the overlay, old commits lack it
    t = repo / "tests" / "test_threshold.py"
    t.write_text("from orders.pricing import apply_discount\n\n\ndef test_threshold():\n"
                 "    assert apply_discount(100.0) == 100.0\n", encoding="utf-8")
    st = open_store(repo)
    debug.start(st, repo, "threshold", PT + ["tests/test_threshold.py"])
    b = debug.bisect(st, repo, good=good, overlay=["tests/test_threshold.py"])
    assert b["status"] == "found" and b["first_bad_commit"]["commit"] == bad_commit
    assert "tests/test_threshold.py laid over it" in b["limits"][0]
    rows = st.all("SELECT copy_source FROM debug_attempts WHERE kind = 'bisect'")
    assert rows and all(r["copy_source"]["overlay"] == ["tests/test_threshold.py"] for r in rows)


def test_the_minimal_repro_drops_positional_test_paths():
    assert debug._without_selectors(["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_x.py",
                                     "-k", "abc", "tests/"]) == ["python", "-m", "pytest", "-q", "-p",
                                                                 "no:cacheprovider", "-k", "abc"]
    assert debug._without_selectors(["pytest", "tests/test_x.py", "-x"]) == ["pytest", "-x"]


def test_trace_enables_off_path(tmp_path):
    repo = _repo(tmp_path)
    _sub(repo, "orders/pricing.py", 'i["price"] * i["qty"]', 'i["price"] + i["qty"]')
    st = open_store(repo)
    debug.start(st, repo, "wrong totals", PT + ["tests/test_pricing.py"], trace=True)
    _sub(repo, "orders/config.py", '        "database_url": DATABASE_URL,',
         '        "currency": "EUR",\n        "database_url": DATABASE_URL,')
    a1 = debug.attempt(st, repo, hypothesis="the settings lack a currency")
    assert _rules(a1) == ["off_path"] and a1["stop"] and a1["trace"]["complete"]
    assert "orders/config.py::load_settings" in a1["loop"][0]["text"]
    narrowing = next(s for s in a1["strategies"] if s["id"] == "narrowing")
    sus = {s["at"]: s for s in narrowing["suspects"]}
    assert "orders/pricing.py::compute_total" in sus and "ruled_out" not in sus["orders/pricing.py::compute_total"]
    assert any(e.startswith("reached by the failing tests") for e in sus["orders/pricing.py::compute_total"]["evidence"])
    assert sus["orders/config.py::load_settings"]["ruled_out"].startswith("not called at all")
    assert narrowing["suspects"][-1]["at"] == "orders/config.py::load_settings"  # ruled-out suspects last
    mini = next(s for s in a1["strategies"] if s["id"] == "minimal_repro")["command"]
    assert mini.endswith("-p no:cacheprovider tests/test_pricing.py::test_compute_total")  # the file selector goes


def test_a_test_that_passes_alone_but_fails_in_the_suite_is_flagged(tmp_path):
    repo = _repo(tmp_path)
    (repo / "tests" / "test_order.py").write_text(
        "STATE = []\n\n\ndef test_a():\n    STATE.append(1)\n\n\ndef test_b():\n    assert STATE == []\n",
        encoding="utf-8")
    st = open_store(repo)
    s = debug.start(st, repo, "test_b fails in the suite", PT + ["tests/test_order.py"])
    assert s["signature"]["failures"][0]["test"] == "tests/test_order.py::test_b"
    alone = debug.attempt(st, repo, hypothesis="test_b alone", kind="probe",
                          command=PT + ["tests/test_order.py::test_b"])
    assert alone["outcome"] == "pass"
    od = [f for f in alone["loop"] if f["rule"] == "order_dependent"]
    assert od and od[0]["strength"] == "heuristic" and "passed alone" in od[0]["text"]


def test_flaky_sessions_are_flagged_and_rerun_reports_a_pass_rate(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    st = open_store(repo)
    cmd = PT + ["tests/test_pricing.py"]
    debug.start(st, repo, "sometimes fails", cmd)  # passes: the example is green
    _sub(repo, "orders/pricing.py", 'i["qty"]', 'i["quantity"]')
    # the same tree, reported twice with different outcomes: flaky
    out1 = "FAILED tests/test_pricing.py::test_compute_total - KeyError: 'quantity'\n"
    debug.attempt(st, repo, hypothesis="run it again", kind="probe", observed_output=out1, exit_code=1, command=cmd)
    f = debug.attempt(st, repo, hypothesis="and again", kind="probe", observed_output="3 passed in 0.1s\n",
                      exit_code=0, command=cmd)
    assert f.get("flaky") and f["strategies"][0]["id"] == "rerun" and not f["stop"]
    r = debug.rerun(st, repo, times=3)
    # Verinoda's own runs of the tree agree; the agent's reports are listed, not counted (review finding)
    assert r["runs"] == 3 and r["passed"] == 0 and r["tree_runs"] == 3 and r["tree_passed"] == 0
    assert r["conclusion"] == "stable: all 3 run(s) Verinoda made of this tree gave the same result"
    assert not r["flaky"] and "disagree with Verinoda's runs" in r["agent_reports"]
    _sub(repo, "orders/pricing.py", 'i["quantity"]', 'i["qty"]')  # another tree: a stable series
    cleared = debug.rerun(st, repo, times=3)
    assert cleared["conclusion"].startswith("stable") and not cleared["flaky"]


def test_refs_that_look_like_options_are_refused(tmp_path):
    repo = _repo(tmp_path)
    st = open_store(repo)
    with pytest.raises(ValueError):
        debug.start(st, repo, "x", PT, base="--upload-pack=touch /tmp/p")
    with pytest.raises(ValueError):
        debug.start(st, repo, "x", PT, base="-x")


def test_not_a_git_repository(tmp_path):
    plain = tmp_path / "plain"
    shutil.copytree(EXAMPLE, plain, ignore=shutil.ignore_patterns(".verinoda", "__pycache__"))
    with pytest.raises(treestate.NotAGitTree):
        debug.start(open_store(plain), plain, "x", PT)


# -- review findings (Phase 2 review of the debug ledger) --------------------------------------------------

def test_only_the_repro_command_can_pass_the_repro(tmp_path):
    repo = _repo(tmp_path)
    _sub(repo, "orders/pricing.py", 'i["qty"]', 'i["quantity"]')
    st = open_store(repo)
    debug.start(st, repo, "order totals raise KeyError", PT)
    one = debug.attempt(st, repo, hypothesis="the discount tests are unaffected", kind="probe",
                        command=PT + ["tests/test_pricing.py::test_no_discount_below_threshold"])
    assert one["outcome"] == "pass" and not one["result"].startswith("the repro command passed")
    assert "not the session's repro command" in one["result"]
    k = debug.attempt(st, repo, hypothesis="run what matters",
                      command=PT + ["-k", "not compute_total and not roundtrip"])
    assert any("tests not matching -k" in x for x in k["not_run"])
    s = debug.status(st, repo)
    assert "result" not in s and len(s["other_commands_passed"]) == 2
    for n in (1, 2):
        with pytest.raises(debug.DebugError, match="not the session's repro command"):
            debug.close(st, repo, resolved_by=n)


def test_skipping_or_deselecting_the_failing_tests_is_no_pass(tmp_path):
    repo = _repo(tmp_path)
    _sub(repo, "orders/pricing.py", 'i["qty"]', 'i["quantity"]')
    st = open_store(repo)
    debug.start(st, repo, "order totals raise KeyError", PT)
    _sub(repo, "tests/test_pricing.py", "from orders", "import pytest\n\nfrom orders")
    _sub(repo, "tests/test_pricing.py", "def test_compute_total():", "@pytest.mark.skip(reason='flaky')\n"
                                                                    "def test_compute_total():")
    _sub(repo, "tests/test_service.py", "def test_place_and_fetch_roundtrip():\n",
         "def test_place_and_fetch_roundtrip():\n    return\n")
    a = debug.attempt(st, repo, hypothesis="these tests are unreliable")
    assert a["outcome"] == "pass" and a["stop"] and a["progress"] == "unknown"
    assert set(_rules(a)) == {"test_edited", "failing_tests_skipped"}
    assert "that is not a passing repro" in a["result"] and a["questions_for_human"]
    assert any("skipped or xfailed" in x for x in a["not_run"])
    with pytest.raises(debug.DebugError, match="did not pass in it"):
        debug.close(st, repo, resolved_by=1)
    # the same through the configuration: the failing tests deselected in pytest.ini
    _git(repo, "checkout", "--", "tests")
    (repo / "pytest.ini").write_text("[pytest]\naddopts = -k \"not compute_total and not roundtrip\"\n",
                                     encoding="utf-8")
    b = debug.attempt(st, repo, hypothesis="run the stable tests only")
    assert b["outcome"] == "pass" and b["stop"] and "failing_tests_skipped" in _rules(b)
    assert b["loop"][0]["rule"] == "test_edited" and "the tests the configuration selects" in b["loop"][0]["text"]


def test_bisect_runs_both_ends_before_it_names_a_commit(tmp_path):
    repo = _repo(tmp_path)
    init = _git(repo, "rev-parse", "HEAD").strip()
    _sub(repo, "orders/api.py", '"""', '"""(c1) ')
    _git(repo, "commit", "-qam", "c1 docstring")
    _sub(repo, "orders/pricing.py", 'i["price"] * i["qty"]', 'i["price"] + i["qty"]')  # the bug, uncommitted
    st = open_store(repo)
    s = debug.start(st, repo, "totals wrong", PT + ["tests/test_pricing.py"])
    b = debug.bisect(st, repo, good=init)
    assert b["status"] == "unknown" and "passes at the bad end" in b["conclusion"]
    assert b["runs"][0]["outcome"] == "pass" and b["runs_total"] == 1 and "first_bad_commit" not in b
    debug.close(st, repo, s["session"], abandoned=True)
    _git(repo, "commit", "-qam", "c2 plus instead of times")
    bug = _git(repo, "rev-parse", "HEAD").strip()
    _sub(repo, "orders/config.py", "Runtime configuration", "Runtime configuration (c3)")
    _git(repo, "commit", "-qam", "c3 docstring")
    debug.start(st, repo, "totals wrong", PT + ["tests/test_pricing.py"])
    wrong_good = debug.bisect(st, repo, good=bug)
    assert wrong_good["status"] == "unknown" and "does not pass at the good end" in wrong_good["conclusion"]
    found = debug.bisect(st, repo, good=init)
    assert found["status"] == "found" and found["first_bad_commit"]["commit"] == bug
    fb = found["first_bad_commit"]
    assert fb["fail_attempt"] is not None and fb["parent_pass_attempt"] is not None
    assert f"(attempt {fb['parent_pass_attempt']})" in found["conclusion"]
    assert any(r.get("recorded") for r in found["runs"])  # the bad end was run by the earlier bisect: reused


def test_a_failed_baseline_leaves_no_open_session(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    st = open_store(repo)
    with pytest.raises(debug.DebugError, match="needs --exit-code"):
        debug.start(st, repo, "gradle fails", ["gradle", "test"], observed_output="BUILD FAILED")
    assert st.all("SELECT id FROM debug_sessions") == []

    def boom(*a, **k):
        raise OSError("[WinError 2] The system cannot find the file specified")
    monkeypatch.setattr(experiments, "run", boom)
    with pytest.raises(OSError):
        debug.start(st, repo, "go thing", PT)
    rows = st.all("SELECT status, close_note FROM debug_sessions")
    assert [r["status"] for r in rows] == ["abandoned"] and "baseline could not be recorded" in rows[0]["close_note"]
    with pytest.raises(KeyError):
        debug.status(st, repo)


def test_mcp_commands_keep_repeated_and_empty_arguments(tmp_path):
    from verinoda.mcp.server import AtlasTools, _argv_list

    assert _argv_list(["-p", "a", "-p", "b", "-k", ""], "command") == ["-p", "a", "-p", "b", "-k", ""]
    repo = _repo(tmp_path)
    open_store(repo).close()
    t = AtlasTools(repo)
    cmd = PT + ["-p", "no:warnings", "tests/test_pricing.py"]
    e = t.experiment_run(cmd, "the pricing tests pass")
    assert e["outcome"] == "pass"
    st = open_store(repo)
    assert st.one("SELECT command FROM experiments WHERE id = ?", (e["id"],))["command"] == cmd
    bad = t.experiment_run(["python", 1], "x")
    assert bad["error"] == "invalid_argument"


def test_a_project_in_a_subdirectory_of_its_repository(tmp_path):
    root = tmp_path / "mono"
    shutil.copytree(EXAMPLE, root / "pkg", ignore=shutil.ignore_patterns(".verinoda", "__pycache__", "*.pyc", "*.db"))
    (root / "other").mkdir()
    (root / "other" / "o.py").write_text("X = 1\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    pkg = root / "pkg"
    _sub(pkg, "orders/pricing.py", 'i["qty"]', 'i["quantity"]')
    st = open_store(pkg)
    s = debug.start(st, pkg, "totals", PT)
    assert s["outcome"] == "fail"
    assert s["tree"]["changed_files"] == [{"path": "orders/pricing.py", "status": "modified",
                                           "symbols": ["compute_total"]}]
    d = debug.diff(st, pkg)
    assert [(f["path"], f["status"]) for f in d["files"]] == [("orders/pricing.py", "modified")]
    diffr = debug.differential(st, pkg)
    assert diffr["outcome_at_base"] == "pass" and diffr["hunks"][0]["at"] == "orders/pricing.py:7"
    assert not any(p.startswith(("other/", "pkg/")) for p in treestate.commit_files(pkg, s["base"]["commit"]))


def _commit_with_entries(repo: Path, extra: dict[str, bytes]) -> str:
    """A commit on top of HEAD whose tree also has ``extra`` (made with plumbing, which does not check names)."""
    def run(*args, inp=None):
        return subprocess.run(["git", *args], cwd=repo, input=inp, capture_output=True, check=True).stdout

    root = run("ls-tree", "HEAD").decode()
    subtrees: dict[str, list[str]] = {}
    top = [ln for ln in root.splitlines() if ln]
    for path, data in extra.items():
        blob = run("hash-object", "-w", "--stdin", inp=data).decode().strip()
        d, _, name = path.rpartition("/")
        if d:
            subtrees.setdefault(d, []).append(f"100644 blob {blob}\t{name}")
        else:
            top.append(f"100644 blob {blob}\t{name}")
    for d, ents in subtrees.items():
        tid = run("mktree", inp=("\n".join(ents) + "\n").encode()).decode().strip()
        top.append(f"040000 tree {tid}\t{d}")
    tree = run("mktree", inp=("\n".join(top) + "\n").encode()).decode().strip()
    return run("-c", "user.name=t", "-c", "user.email=t@t", "commit-tree", tree, "-p", "HEAD", "-m",
               "crafted").decode().strip()


def test_commit_copies_never_write_a_git_directory(tmp_path):
    repo = _repo(tmp_path)
    cfg = b"[core]\n\tfsmonitor = \"echo PWNED >&2; false\"\n"
    commit = _commit_with_entries(repo, {".GIT/config": cfg, "git~1/config": cfg, ".Verinoda/x": b"y"})
    paths = [p for _, _, p in treestate.commit_entries(repo, commit)]
    assert paths and not any(p.split("/")[0].lower() in (".git", "git~1", ".verinoda") for p in paths)
    dst = tmp_path / "copy"
    dst.mkdir()
    experiments._copy_commit(repo, commit, dst)
    assert sorted(p.name for p in dst.iterdir()) == sorted({p.split("/")[0] for p in paths})
    st = open_store(repo)
    with pytest.raises(ValueError, match="repository-relative file path"):
        experiments.run(st, repo, PT, hypothesis="x", ref="HEAD", overlay=[".GIT/config"])


def test_commit_copies_never_write_a_git_directory_through_backslash_names(tmp_path):
    # review finding: the tree entry ".\.git\config" is one name for git, but Windows writes it as .git/config
    # (and git then runs the fsmonitor it names); the prepared differential copy said "no .git is written"
    repo = _repo(tmp_path)
    cfg = b"[core]\n\tfsmonitor = \"echo PWNED >&2; false\"\n"
    commit = _commit_with_entries(repo, {".\\.git\\config": cfg, ".\\.git\\HEAD": b"ref: refs/heads/main\n",
                                         "a\\GIT~1\\config": cfg, "sub\\.verinoda\\x.txt": b"y",
                                         "venvtrick\\.venv\\y.txt": b"v"})
    reserved = {".git", "git~1", ".verinoda"}
    skipped: list[dict] = []
    paths = [p for _, _, p in treestate.commit_entries(repo, commit, skipped)]
    assert paths and not {q.lower() for p in paths for q in re.split(r"[/\\]", p)} & reserved
    if os.name == "nt":
        assert not any("\\" in p for p in paths)
        assert [s["path"] for s in skipped] == ["venvtrick\\.venv\\y.txt"] and "backslash" in skipped[0]["why"]
    dst = tmp_path / "copy"
    dst.mkdir()
    experiments._copy_commit(repo, commit, dst)
    st = open_store(repo)
    debug.start(st, repo, "x", ["gradlew", "test"], observed_output="FAILED\n", exit_code=1)
    prep = Path(debug.differential(st, repo, base=commit, prepare=True)["prepared_copy"])
    for copy in (dst, prep):
        written = {q.lower() for f in copy.rglob("*") for q in f.relative_to(copy).parts}
        assert written and not written & reserved, copy


def test_debug_diff_does_not_rewrite_the_index(tmp_path):
    repo = _repo(tmp_path)
    st = open_store(repo)
    debug.start(st, repo, "x", ["gradlew", "test"], observed_output="FAILED\n", exit_code=1)
    index = repo / ".git" / "index"
    before = index.read_bytes()
    f = repo / "orders" / "pricing.py"
    os.utime(f, (f.stat().st_atime + 100, f.stat().st_mtime + 100))  # stat-dirty, content unchanged
    debug.diff(st, repo)
    treestate.changes_vs_base(repo, treestate.head_commit(repo))
    assert index.read_bytes() == before


@pytest.mark.skipif(os.name != "nt", reason="names Windows cannot hold")
def test_a_commit_file_this_os_cannot_hold_is_left_out_and_said(tmp_path):
    repo = _repo(tmp_path)
    commit = _commit_with_entries(repo, {"notes/what?.md": b"q\n", "notes/NUL": b"n\n"})
    st = open_store(repo)
    r = experiments.run(st, repo, PT + ["tests/test_pricing.py"], hypothesis="the tests pass", ref=commit)
    assert r["outcome"] == "pass"
    assert {s["path"] for s in r["source"]["skipped"]} == {"notes/what?.md", "notes/NUL"}
    assert any("missing from the copy" in x for x in r["limits"])


def test_a_run_of_another_commit_only_qualifies_a_claim(tmp_path):
    repo = _repo(tmp_path)
    st = open_store(repo)
    cl = Claims(st, repo)
    c = cl.create("the pricing tests pass", project="oa", snapshot=None, status="unknown")
    r = experiments.run(st, repo, PT + ["tests/test_pricing.py"], hypothesis="they pass", ref="HEAD", claim_id=c["id"])
    assert r["outcome"] == "pass"
    link = st.one("SELECT relation, note FROM claim_evidence WHERE claim_id = ?", (c["id"],))
    assert link["relation"] == "qualifies" and "other code than the claim's commit" in link["note"]
    assert "on commit" in st.evidence(r["evidence_id"])["locator"]


def test_cli_differential_that_cannot_judge_exits_3(tmp_path, capsys):
    repo = _repo(tmp_path)
    (repo / "tests" / "conftest.py").write_text("raise ImportError('broken at the base')\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "a broken conftest")
    (repo / "tests" / "conftest.py").write_text("", encoding="utf-8")
    _sub(repo, "orders/pricing.py", 'i["qty"]', 'i["quantity"]')
    r = str(repo)
    assert cli.main(["debug", "start", "totals", "--repo", r, "--", *PT]) == 0
    capsys.readouterr()
    assert cli.main(["debug", "differential", "--repo", r]) == 3
    assert "inconclusive at the base" in capsys.readouterr().out


def test_the_differential_holds_the_symptoms_new_test_fixed(tmp_path):
    repo = _repo(tmp_path)
    _sub(repo, "orders/pricing.py", "if subtotal > DISCOUNT_THRESHOLD:", "if subtotal >= DISCOUNT_THRESHOLD:")
    _git(repo, "commit", "-qam", "discount: simplify the comparison")  # the real cause, committed
    (repo / "tests" / "test_boundary.py").write_text(
        "from orders.pricing import apply_discount\n\n\ndef test_no_discount_at_the_threshold():\n"
        "    assert apply_discount(100.0) == 100.0\n", encoding="utf-8")
    st = open_store(repo)
    debug.start(st, repo, "an order of exactly 100 gets a discount", PT)
    d = debug.differential(st, repo)
    assert d["overlay"] == ["tests/test_boundary.py"] and d["outcome_at_base"] == "fail"
    assert d["status"] == "fails_at_base" and "--overlay tests/test_boundary.py" in d["conclusion"]
    assert "hunks" not in d


def test_a_failing_test_that_does_not_exist_at_the_base_makes_the_differential_inconclusive(tmp_path):
    repo = _repo(tmp_path)
    _sub(repo, "orders/pricing.py", 'i["qty"]', 'i["quantity"]')
    st = open_store(repo)
    debug.start(st, repo, "totals", PT)
    _sub(repo, "orders/pricing.py", 'i["quantity"]', 'i["qty"]')
    (repo / "tests" / "test_extra.py").write_text(
        "from orders.pricing import apply_discount\n\n\ndef test_new_rule():\n"
        "    assert apply_discount(100.0) == 90.0\n", encoding="utf-8")
    debug.attempt(st, repo, hypothesis="the key was right; a threshold test was missing")
    d = debug.differential(st, repo)
    assert d["outcome_at_base"] == "pass" and d["status"] == "inconclusive" and "hunks" not in d
    assert "tests/test_extra.py::test_new_rule" in d["conclusion"] and "not run" in d["conclusion"]


def test_undoing_the_last_edit_while_fixing_another_file_does_not_stop(tmp_path):
    repo = _repo(tmp_path)
    _sub(repo, "orders/pricing.py", "if subtotal > DISCOUNT_THRESHOLD:", "if subtotal < DISCOUNT_THRESHOLD:")
    _sub(repo, "orders/service.py", "total = compute_total(items)", "total = compute_total(items) * 2")
    st = open_store(repo)
    debug.start(st, repo, "discounts and order totals are wrong", PT)
    _sub(repo, "orders/pricing.py", "if subtotal < DISCOUNT_THRESHOLD:", "if subtotal >= DISCOUNT_THRESHOLD:")
    debug.attempt(st, repo, hypothesis="the discount comparison is inverted")
    _sub(repo, "orders/pricing.py", "return round(subtotal * 0.9, 2)", "return subtotal * 0.9")
    debug.attempt(st, repo, hypothesis="rounding makes the roundtrip total differ")
    _sub(repo, "orders/pricing.py", "return subtotal * 0.9", "return round(subtotal * 0.9, 2)")
    _sub(repo, "orders/service.py", "total = compute_total(items) * 2", "total = compute_total(items)")
    a3 = debug.attempt(st, repo, hypothesis="place_order doubles the total")
    assert a3["outcome"] == "pass" and not a3["stop"] and _rules(a3) == []
    assert "file_reverted" in _rules(a3, "heuristic")
    assert debug.close(st, repo, resolved_by=3)["status"] == "resolved"


def test_the_same_code_with_a_docstring_edit_is_a_revert(tmp_path):
    repo = _repo(tmp_path)
    _sub(repo, "orders/pricing.py", 'i["qty"]', 'i["quantity"]')
    st = open_store(repo)
    a0 = debug.start(st, repo, "totals", PT + ["tests/test_pricing.py"])
    _sub(repo, "orders/pricing.py", 'i["quantity"]', 'i.get("quantity", 0)')
    debug.attempt(st, repo, hypothesis="a missing quantity defaults to zero")
    _sub(repo, "orders/pricing.py", 'i.get("quantity", 0)', 'i["quantity"]')
    _sub(repo, "orders/pricing.py", '"""Pricing rules."""', '"""Pricing rules (quantities)."""')
    a2 = debug.attempt(st, repo, hypothesis="index quantity directly, and document it")
    assert a2["tree"]["hash"] != a0["tree"]["hash"] and "tree_reverted" in _rules(a2) and a2["stop"]
    assert "comments, docstrings or formatting" in a2["loop"][0]["text"]


def test_off_path_keeps_an_edit_that_runs_at_import_time(tmp_path):
    repo = _repo(tmp_path)
    _sub(repo, "orders/config.py", 'DISCOUNT_THRESHOLD = float(os.environ.get("ORDERS_DISCOUNT_THRESHOLD", "100.0"))',
         'def discount_threshold() -> float:\n    return float(os.environ.get("ORDERS_DISCOUNT_THRESHOLD", "100.0"))\n'
         '\n\nDISCOUNT_THRESHOLD = discount_threshold()')
    _git(repo, "commit", "-qam", "threshold function")
    _sub(repo, "orders/config.py", '"100.0"))\n', '"10.0"))\n')
    st = open_store(repo)
    debug.start(st, repo, "small orders get a discount", PT + ["tests/test_pricing.py"], trace=True)
    _sub(repo, "orders/config.py", '"10.0"))\n', '"20.0"))\n')
    a1 = debug.attempt(st, repo, hypothesis="the threshold default is too low")
    assert a1["trace"]["complete"] and "off_path" not in _rules(a1)
    narrowing = next((s for s in a1.get("strategies") or [] if s["id"] == "narrowing"), None)
    for sus in (narrowing or {}).get("suspects") or []:
        assert "ruled_out" not in sus, sus


def test_an_agent_report_cannot_resolve_a_tree_verinoda_saw_fail(tmp_path):
    repo = _repo(tmp_path)
    _sub(repo, "orders/pricing.py", 'i["qty"]', 'i["quantity"]')
    st = open_store(repo)
    debug.start(st, repo, "totals", PT)
    rep = debug.attempt(st, repo, hypothesis="I ran the tests myself", observed_output="5 passed in 0.1s\n",
                        exit_code=0, command=PT)
    assert rep["run_by"] == "agent" and not rep.get("flaky")
    with pytest.raises(debug.DebugError, match="did not pass in every run Verinoda made"):
        debug.close(st, repo, resolved_by=1)
    s = debug.status(st, repo)
    assert "not passed in attempt(s) 0: fail" in s["result"]


def test_status_and_close_count_every_run_of_the_tree(tmp_path):
    repo = _repo(tmp_path)
    counter = tmp_path / "counter.txt"
    counter.write_text("0", encoding="utf-8")
    (repo / "tests" / "test_feed.py").write_text(
        "from pathlib import Path\n\nC = Path(%r)\n\n\ndef test_remote_price_feed():\n"
        "    n = int(C.read_text()) + 1\n    C.write_text(str(n))\n    assert n %% 2 == 0\n" % str(counter),
        encoding="utf-8")
    st = open_store(repo)
    cmd = PT + ["tests/test_feed.py"]
    debug.start(st, repo, "the price feed test fails sometimes", cmd)            # run 1: fail
    ok = debug.attempt(st, repo, hypothesis="is it stable?", kind="rerun")         # run 2: pass
    bad = debug.attempt(st, repo, hypothesis="and again?", kind="rerun")           # run 3: fail
    assert ok["outcome"] == "pass" and bad["outcome"] == "fail" and bad.get("flaky")
    assert ok["progress"] == "unknown" and bad["progress"] == "unknown"
    s = debug.status(st, repo)
    assert s["tree_runs"]["disagree"] and "not passed in attempt(s) 0: fail, 2: fail" in s["result"]
    assert s.get("flaky")
    with pytest.raises(debug.DebugError, match="flaky"):
        debug.close(st, repo, resolved_by=1)


def test_the_differential_ranks_by_the_current_failure_when_it_changed(tmp_path):
    repo = _repo(tmp_path)
    _sub(repo, "orders/pricing.py", 'i["qty"]', 'i["quantity"]')
    _sub(repo, "orders/api.py", '"""', '"""(an unrelated docstring edit) ')
    st = open_store(repo)
    debug.start(st, repo, "totals raise KeyError", PT)
    _sub(repo, "orders/pricing.py", 'i["quantity"]', 'i["qty"]')
    _sub(repo, "orders/service.py", "total = compute_total(items)", "total = compute_total(items) + 0.5  # shipping")
    debug.attempt(st, repo, hypothesis="the key was right; add shipping")
    d = debug.differential(st, repo)
    assert d["status"] == "cause_in_diff" and d["hunks"][0]["at"].startswith("orders/service.py:")
    assert d["hunks"][0]["why"].startswith("on the failure's traceback")


# -- CLI ----------------------------------------------------------------------------------------------

def test_cli_debug_commands(tmp_path, capsys):
    repo = _repo(tmp_path)
    _sub(repo, "orders/pricing.py", 'i["qty"]', 'i["quantity"]')
    r = str(repo)
    assert cli.main(["debug", "start", "totals break", "--repo", r, "--json", "--", *PT]) == 0
    s = json.loads(capsys.readouterr().out)
    assert s["attempt"] == 0 and s["session"].startswith("dbg_")
    _sub(repo, "tests/test_pricing.py", '"qty": 2', '"quantity": 2')
    assert cli.main(["debug", "try", "--repo", r, "--hypothesis", "the test data is old"]) == 3  # stop
    out = capsys.readouterr().out
    assert out.splitlines()[0].startswith("attempt 1 (fix): fail  - STOP: definitive: test_edited")
    assert "ask the user:" in out
    assert cli.main(["debug", "status", "--repo", r]) == 0
    assert "#1 fix" in capsys.readouterr().out
    assert cli.main(["debug", "diff", "--repo", r, "--attempt", "0", "--json"]) == 0
    d = json.loads(capsys.readouterr().out)
    assert [f["path"] for f in d["files"]] == ["tests/test_pricing.py"]
    assert cli.main(["debug", "close", "--repo", r, "--abandoned"]) == 0
    assert "abandoned" in capsys.readouterr().out
    assert cli.main(["debug", "start", "gradle", "--repo", r, "--", "gradlew", "test"]) == 3
    assert "refused" in capsys.readouterr().out
    logf = tmp_path / "out.txt"
    logf.write_text("Exception in thread \"main\" java.lang.IllegalStateException: x\n", encoding="utf-8")
    assert cli.main(["debug", "start", "gradle", "--repo", r, "--observed-output", str(logf), "--exit-code", "1",
                     "--json", "--", "gradlew", "test"]) == 0
    assert json.loads(capsys.readouterr().out)["run_by"] == "agent"


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_skills_carry_the_debug_protocol_and_do_not_preapprove_runs(agent):
    from verinoda import agents

    text = agents.render_skill(agent).decode("utf-8")
    for needle in ("verinoda debug start", "debug try --hypothesis", "stop: true", "strategies[0]",
                   "questions_for_human", "Never say \"fixed\"", "--observed-output", "debug_attempt"):
        assert needle in text, needle
    if agent == "claude":
        head = text.split("\n---", 1)[0]  # the front matter (allowed-tools)
        assert "Bash(verinoda debug status *)" in head and "PowerShell(verinoda debug diff *)" in head
        # commands that run the project's tests keep the user's permission prompt
        for cmd in ("start", "try", "differential", "bisect", "rerun", "observe"):
            assert f"verinoda debug {cmd}" not in head, cmd


def test_mcp_debug_tools(tmp_path):
    from verinoda.mcp.server import AtlasTools

    repo = _repo(tmp_path)
    open_store(repo).close()  # an initialised project (.verinoda exists)
    _sub(repo, "orders/pricing.py", 'i["qty"]', 'i["quantity"]')
    t = AtlasTools(repo)
    s = t.debug_start("totals break", PT)
    assert s["attempt"] == 0 and s["outcome"] == "fail"
    a = t.debug_attempt("try again", kind="probe")
    assert a["attempt"] == 1 and a["progress"] == "same"
    st = t.debug_status()
    assert len(st["attempts"]) == 2
    e = t.experiment_run(PT, "the suite fails")
    assert e["outcome"] == "fail" and e["tree"]["hash"] == a["tree"]["hash"]
    refused = t.experiment_run(["gradlew", "test"], "gradle")
    assert refused["status"] == "refused"
    bad = t.debug_strategy("nope")
    assert bad["error"] == "invalid_argument"
