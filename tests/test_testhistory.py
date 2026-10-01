"""Flaky test history: per-test rows of the debug ledger's runs, pass rates, flaky trees, held fixes, quarantine."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import json  # noqa: E402
import shutil  # noqa: E402
import sqlite3  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import cli, debug, testhistory  # noqa: E402
from verinoda import store as storemod  # noqa: E402
from verinoda.store import Store, open_store  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "orders_app"
PT = ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]
T = "tests/test_x.py::test_a"


def _store(tmp_path: Path) -> Store:
    st = Store(tmp_path / "atlas.db")
    st.insert("debug_sessions", {"id": "dbg_1", "symptom": "s", "command": json.dumps(PT), "status": "open",
                                 "created_at": "2026-10-01T00:00:00+00:00"})
    return st


def _attempt(st: Store, n: int, tree: str, tests: dict | None, *, outcome: str | None = None,
             run_by: str = "verinoda", command=None, record: bool = True, kind: str = "rerun") -> dict:
    if outcome is None:
        outcome = "fail" if any(o in ("failed", "error") for o in (tests or {}).values()) else "pass"
    sig = {"status": "none"} | ({"tests": tests} if tests is not None else {})
    row = {"id": f"dba_{n}", "session_id": "dbg_1", "n": n, "kind": kind, "hypothesis": "h",
           "command": command or PT, "run_by": run_by, "tree_hash": tree, "outcome": outcome, "signature": sig,
           "created_at": f"2026-10-01T00:00:{n:02d}+00:00"}
    st.insert("debug_attempts", row)
    if record:
        testhistory.record(st, st.get("debug_attempts", row["id"]))
    return row


def test_a_test_with_both_outcomes_on_one_tree_is_flaky_and_other_tests_are_not(tmp_path):
    st = _store(tmp_path)
    _attempt(st, 0, "A", {T: "passed", "tests/test_x.py::test_b": "passed"})
    _attempt(st, 1, "A", {T: "failed", "tests/test_x.py::test_b": "passed"})
    _attempt(st, 2, "A", {T: "passed", "tests/test_x.py::test_b": "skipped"})
    r = testhistory.report(st, tmp_path)
    assert r["recorded"] == {"runs": 3, "tests": 2, "rows": 5, "added_now": 0}
    assert [e["test"] for e in r["flaky"]] == [T]
    e = r["flaky"][0]
    assert (e["runs"], e["passed"], e["failed"], e["pass_rate"]) == (3, 2, 1, 0.67)
    assert e["status"] == "strong_inference" and "same tree" in e["finding"] and not e["quarantined"]
    assert r["quarantine_candidates"] == [T] and not r["held"] and not r["pending"]


def test_failures_on_one_tree_and_passes_on_another_are_a_change_not_flakiness(tmp_path):
    st = _store(tmp_path)
    _attempt(st, 0, "A", {T: "failed"})
    _attempt(st, 1, "A", {T: "failed"})
    for n in range(2, 5):
        _attempt(st, n, "B", {T: "passed"})
    r = testhistory.report(st, tmp_path, runs=5)
    assert not r["flaky"] and not r["held"] and r["pending"] == [{"test": T, "passes": 3, "of": 5}]
    r = testhistory.report(st, tmp_path, runs=3)
    assert [e["test"] for e in r["held"]] == [T] and not r["pending"]
    held = r["held"][0]
    assert held["status"] == "strong_inference" and not held["was_flaky"]
    assert "passed the last 3 recorded run(s) in a row" in held["finding"] and "attempt 1" in held["finding"]
    assert "not proof" in held["finding"]
    _attempt(st, 5, "B", {T: "failed"})  # fails again: the change did not hold, and tree B is now flaky
    r = testhistory.report(st, tmp_path, runs=3)
    assert not r["held"] and [e["test"] for e in r["flaky"]] == [T]


def test_a_pass_on_the_failing_tree_is_flakiness_not_a_held_fix(tmp_path):
    st = _store(tmp_path)
    _attempt(st, 0, "A", {T: "failed"})
    for n in range(1, 4):
        _attempt(st, n, "A" if n == 1 else "B", {T: "passed"})
    r = testhistory.report(st, tmp_path, runs=2)
    assert not r["held"] and not r["pending"] and [e["test"] for e in r["flaky"]] == [T]
    one = testhistory.report(st, tmp_path, runs=2, test=T)["test"]
    assert one["since_last_failure"] == {"passes": 3, "on_changed_tree": False, "held": False}
    assert [h["outcome"] for h in one["history"]] == ["fail", "pass", "pass", "pass"]


def test_only_verinodas_pass_and_fail_runs_are_recorded_and_a_runner_without_test_ids_counts_as_the_command(
        tmp_path):
    st = _store(tmp_path)
    _attempt(st, 0, "A", {T: "failed"}, run_by="agent")
    _attempt(st, 1, "A", None, outcome="timeout")
    _attempt(st, 2, "A", None, outcome="inconclusive")
    go = ["go", "test", "./..."]
    _attempt(st, 3, "A", None, outcome="fail", command=go)
    _attempt(st, 4, "A", None, outcome="pass", command=go)
    rows = st.all("SELECT test, outcome FROM test_runs ORDER BY seq")
    assert rows == [{"test": "$ go test ./...", "outcome": "fail"}, {"test": "$ go test ./...", "outcome": "pass"}]
    assert [e["test"] for e in testhistory.report(st, tmp_path)["flaky"]] == ["$ go test ./..."]
    # pytest's output-only options do not make another command id
    assert testhistory.command_id(PT + ["-vv", "tests"]) == testhistory.command_id(PT + ["tests"])


def test_recording_twice_adds_nothing_and_older_attempts_are_added_when_read(tmp_path):
    st = _store(tmp_path)
    a = _attempt(st, 0, "A", {T: "failed"}, record=False)
    _attempt(st, 1, "A", {T: "passed"}, record=False)
    assert st.one("SELECT COUNT(*) AS n FROM test_runs")["n"] == 0
    r = testhistory.report(st, tmp_path)
    assert r["recorded"]["added_now"] == 2 and r["flaky"][0]["test"] == T
    assert testhistory.record(st, st.get("debug_attempts", a["id"])) == 0
    assert testhistory.report(st, tmp_path)["recorded"]["added_now"] == 0
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        st.conn.execute("DELETE FROM test_runs")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        st.conn.execute("UPDATE test_runs SET outcome = 'pass'")


def test_the_quarantine_list_is_the_users_and_logged(tmp_path):
    st = _store(tmp_path)
    _attempt(st, 0, "A", {T: "failed"})
    _attempt(st, 1, "A", {T: "passed"})
    q = testhistory.quarantine(st, T, reason="times out on CI")
    assert q.pop("since") and q == {"test": T, "quarantined": True, "changed": True, "recorded_runs": 2,
                                    "reason": "times out on CI"}
    again = testhistory.quarantine(st, T, reason="another reason")  # the same keys; the logged reason stays
    assert again.pop("note").startswith("already quarantined") and again.pop("since")
    assert again == {"test": T, "quarantined": True, "changed": False, "recorded_runs": 2, "reason": "times out on CI"}
    assert "check the spelling" in testhistory.quarantine(st, "tests/test_x.py::typo")["note"]
    r = testhistory.report(st, tmp_path)
    assert r["flaky"][0]["quarantined"] and r["quarantine_candidates"] == []
    by = {x["test"]: x for x in r["quarantine"]}
    assert by[T]["now"] == "flaky" and by[T]["reason"] == "times out on CI" and by[T]["pass_rate"] == 0.5
    assert by["tests/test_x.py::typo"]["runs"] == 0
    testhistory.quarantine(st, T, remove=True)
    assert [x["test"] for x in testhistory.report(st, tmp_path)["quarantine"]] == ["tests/test_x.py::typo"]
    with pytest.raises(ValueError, match="not quarantined"):
        testhistory.quarantine(st, T, remove=True)
    with pytest.raises(ValueError):
        testhistory.quarantine(st, "  ")
    assert [x["action"] for x in st.all("SELECT action FROM test_quarantine ORDER BY seq")] == ["add", "add",
                                                                                                "remove"]
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        st.conn.execute("DELETE FROM test_quarantine")


def test_passes_of_older_commits_from_bisect_or_differential_never_make_a_held_fix(tmp_path):
    st = _store(tmp_path)
    _attempt(st, 0, "HEAD", {T: "failed"}, kind="baseline")
    for n in range(1, 6):  # bisect's good end and midpoints pass on older commits
        _attempt(st, n, f"C{n}", {T: "passed"}, kind="bisect")
    _attempt(st, 6, "C0", {T: "passed"}, kind="differential")
    r = testhistory.report(st, tmp_path, runs=5)
    assert not r["held"] and not r["pending"] and not r["flaky"]
    for n in range(7, 9):  # the working tree changed and now passes: that counts
        _attempt(st, n, "FIX", {T: "passed"})
    assert testhistory.report(st, tmp_path, runs=5)["pending"] == [{"test": T, "passes": 2, "of": 5}]
    one = testhistory.report(st, tmp_path, runs=2)
    assert [e["test"] for e in one["held"]] == [T] and "attempt 0" in one["held"][0]["finding"]


def test_a_pass_on_a_tree_that_failed_before_is_not_a_held_fix(tmp_path):
    st = _store(tmp_path)
    _attempt(st, 0, "A", {T: "failed"})
    _attempt(st, 1, "B", {T: "failed"})
    for n in range(2, 5):  # back on tree A, which failed before
        _attempt(st, n, "A", {T: "passed"})
    r = testhistory.report(st, tmp_path, runs=3)
    assert not r["held"] and [e["test"] for e in r["flaky"]] == [T]


def test_attempts_added_on_read_keep_their_run_order(tmp_path):
    st = _store(tmp_path)
    for n in range(5):  # recorded before the table existed
        _attempt(st, n, "A", {T: "passed"}, record=False)
    _attempt(st, 5, "B", {T: "failed"})  # recorded live after the upgrade, before the first read
    r = testhistory.report(st, tmp_path, runs=5, test=T)
    assert r["recorded"]["added_now"] == 5 and not r["held"] and not r["flaky"]
    assert [h["attempt"] for h in r["test"]["history"]] == [0, 1, 2, 3, 4, 5]
    assert "since_last_failure" not in r["test"]


def test_lists_are_capped_the_quarantine_too_and_files_are_read_only_for_shown_entries(tmp_path, monkeypatch):
    st = _store(tmp_path)
    for i in range(LIST_N := testhistory.LIST_CAP + 5):
        testhistory.quarantine(st, f"tests/test_x.py::test_q{i}")
    tests = {f"tests/test_x.py::test_{i}": "passed" for i in range(LIST_N)}
    _attempt(st, 0, "A", tests)
    _attempt(st, 1, "A", {t: "failed" for t in tests})
    calls = []
    real = testhistory._locate
    monkeypatch.setattr(testhistory, "_locate", lambda *a: calls.append(a[1]) or real(*a))
    r = testhistory.report(st, tmp_path)
    assert len(r["quarantine"]) == testhistory.LIST_CAP and len(r["flaky"]) == testhistory.LIST_CAP
    assert r["truncated"] is True and len(calls) == testhistory.LIST_CAP


def test_runs_below_one_is_an_error(tmp_path):
    st = _store(tmp_path)
    for bad in (0, -3):
        with pytest.raises(ValueError, match="1 or more"):
            testhistory.report(st, tmp_path, runs=bad)
    assert testhistory.report(st, tmp_path, runs=1)["verify_runs"] == 1


def test_a_test_is_located_at_its_definition(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("import x\n\nclass TestK:\n    async def test_a(self):\n"
                                                   "        pass\n", encoding="utf-8")
    assert testhistory._locate(tmp_path, "tests/test_x.py::TestK::test_a[1-2]") == "tests/test_x.py:4"
    assert testhistory._locate(tmp_path, "tests/test_x.py::test_gone") == "tests/test_x.py"
    assert testhistory._locate(tmp_path, "../outside.py::test_a") is None
    assert testhistory._locate(tmp_path, "$ go test ./...") is None
    st = _store(tmp_path)
    assert testhistory.report(st, tmp_path, test="nope")["test"]["status"] == "unknown"


def test_a_v6_database_gains_the_tables(tmp_path):
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    for v in range(1, 7):
        conn.executescript(storemod._MIGRATIONS[v])
    conn.execute("INSERT INTO meta VALUES ('schema_version', '6')")
    conn.commit()
    conn.close()
    st = Store(db)
    assert st.one("SELECT value FROM meta WHERE key = 'schema_version'")["value"] == str(storemod.SCHEMA_VERSION)
    assert st.all("SELECT * FROM test_runs") == [] and st.all("SELECT * FROM test_quarantine") == []


# -- end to end: a real rerun series on a copy of examples/orders_app ------------------------------------------

def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args],
                   cwd=cwd, check=True, capture_output=True, text=True)


@pytest.mark.experiment
@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_a_rerun_series_persists_each_tests_outcome_and_the_cli_reports_it(tmp_path, capsys):
    repo = tmp_path / "oa"
    shutil.copytree(EXAMPLE, repo, ignore=shutil.ignore_patterns(".verinoda", "__pycache__", "*.pyc", "*.db"))
    counter = (tmp_path / "count.txt").as_posix()
    (repo / "tests" / "test_flip.py").write_text(
        "from pathlib import Path\n\n\ndef test_flip():\n"
        f"    p = Path({counter!r})\n"
        "    n = int(p.read_text()) if p.exists() else 0\n"
        "    p.write_text(str(n + 1))\n"
        "    assert n % 2 == 0\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    st = open_store(repo)
    cmd = PT + ["tests/test_flip.py", "tests/test_pricing.py"]
    debug.start(st, repo, "test_flip fails now and then", cmd)   # run 1: passes
    r = debug.rerun(st, repo, times=2)                             # runs 2 and 3: fail, pass
    assert r["passed"] == 1 and r["tests_both_outcomes"] == ["tests/test_flip.py::test_flip"]
    rep = testhistory.report(st, repo)
    assert rep["recorded"]["runs"] == 3 and rep["recorded"]["added_now"] == 0
    flaky = {e["test"]: e for e in rep["flaky"]}
    assert list(flaky) == ["tests/test_flip.py::test_flip"]
    assert flaky["tests/test_flip.py::test_flip"]["at"] == "tests/test_flip.py:4"
    assert flaky["tests/test_flip.py::test_flip"]["pass_rate"] == 0.67
    assert len(flaky["tests/test_flip.py::test_flip"]["evidence"]) == 2
    st.close()
    rr = str(repo)
    assert cli.main(["debug", "quarantine", "tests/test_flip.py::test_flip", "--repo", rr,
                     "--reason", "shared file"]) == 0
    capsys.readouterr()
    assert cli.main(["debug", "flaky", "--repo", rr]) == 0
    out = capsys.readouterr().out
    assert "tests/test_flip.py::test_flip [quarantined]  pass rate 0.67 (2/3)" in out and "quarantine (yours):" in out
    assert cli.main(["debug", "flaky", "--repo", rr, "--json", "--test", "tests/test_flip.py::test_flip"]) == 0
    got = json.loads(capsys.readouterr().out)
    assert [h["outcome"] for h in got["test"]["history"]] == ["pass", "fail", "pass"]
