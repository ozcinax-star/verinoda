"""Flaky test history: each test's outcome in every run of the debug ledger Verinoda made, kept per test.

Every attempt Verinoda runs itself (``debug try``, ``debug rerun``, ``differential``, ``bisect``, ``observe``) that
passed or failed leaves one ``test_runs`` row per test (schema v7): the outcome the failure-signature plugin
recorded for it (pytest), or, when the run gives no per-test outcomes (another runner), one row for the command
itself (``$ <command>``). Agent-reported runs, timeouts and inconclusive runs leave none. Attempts recorded before
this table existed are added the first time the history is read (:func:`sync`).

:func:`report` (``verinoda debug flaky``) reads the history:

* a test is **flaky** when one tree (the ledger's tree hash) has both a passing and a failing run of it;
* a failing test's change **held** when it passed the last ``N`` runs in a row (``debug.rerun_times``, ``--runs``)
  since its last failure, every one of them on a tree other than the failing one. A pass on the failing tree
  makes the test flaky there, not fixed;
* the **quarantine** list is the user's (``verinoda debug quarantine TEST``): Verinoda keeps it, shows each
  quarantined test's history beside it, and never skips, deselects or reruns a test because of it.

Both judgements are heuristics over the runs recorded on this machine (``strong_inference``): a run of a tree
that passed N times can fail on the next run, and a test never seen failing may still be flaky.
"""
from __future__ import annotations

import re
from pathlib import Path

from verinoda.store import Store, now

LIST_CAP = 20
EVIDENCE_CAP = 3
HISTORY_CAP = 20
# the failure-signature plugin's outcomes; skipped and xfailed tests ran nothing that passed or failed
OUTCOMES = {"passed": "pass", "xpassed": "pass", "failed": "fail", "error": "fail"}
LIMITS = [
    "only runs Verinoda made through the debug ledger are counted; agent-reported, timed-out and inconclusive "
    "runs are not",
    "flaky and held are heuristics over the runs recorded on this machine: N passes never prove a cause is gone",
    "the quarantine list is the user's: Verinoda never skips or deselects a quarantined test",
]


def command_id(argv) -> str:
    """The id of a run that gives no per-test outcomes: the command, without pytest's output-only options."""
    from verinoda import debug

    return "$ " + debug.cmd_text(debug.canonical_command(argv))


def _rows_of(attempt: dict) -> list[tuple[str, str]]:
    if attempt.get("run_by") != "verinoda" or attempt.get("outcome") not in ("pass", "fail"):
        return []
    tests = (attempt.get("signature") or {}).get("tests")
    if isinstance(tests, dict) and tests:
        return sorted((str(t), OUTCOMES[o]) for t, o in tests.items() if o in OUTCOMES)
    return [(command_id(attempt.get("command") or []), attempt["outcome"])]


def record(store: Store, attempt: dict) -> int:
    """Add the per-test rows of one debug attempt (a ``debug_attempts`` row); a second call adds nothing."""
    rows = _rows_of(attempt)
    if not rows:
        return 0
    at = attempt.get("created_at") or now()
    with store.tx() as c:
        cur = c.executemany(
            "INSERT OR IGNORE INTO test_runs (attempt_id, session_id, test, outcome, kind, tree_hash, evidence_id, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(attempt["id"], attempt["session_id"], t, o, attempt["kind"], attempt.get("tree_hash"),
              attempt.get("evidence_id"), at) for t, o in rows])
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0


def sync(store: Store) -> int:
    """Record the attempts Verinoda ran that have no per-test rows yet (made before this table existed)."""
    missing = store.all(
        "SELECT * FROM debug_attempts a WHERE a.run_by = 'verinoda' AND a.outcome IN ('pass', 'fail') AND NOT EXISTS "
        "(SELECT 1 FROM test_runs t WHERE t.attempt_id = a.id) ORDER BY a.created_at, a.rowid")
    return sum(record(store, a) for a in missing)


def _quarantined(store: Store) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for r in store.all("SELECT * FROM test_quarantine ORDER BY seq"):
        if r["action"] == "add":
            out[r["test"]] = {"reason": r.get("reason"), "since": r["created_at"]}
        else:
            out.pop(r["test"], None)
    return out


def quarantine(store: Store, test: str, *, remove: bool = False, reason: str | None = None) -> dict:
    """Add ``test`` to the quarantine list or take it off (the user's decision, logged)."""
    test = (test or "").strip()
    if not test:
        raise ValueError("name the test (its id as `verinoda debug flaky` lists it)")
    current = _quarantined(store)
    if remove and test not in current:
        raise ValueError(f"{test} is not quarantined")
    if not remove and test in current:
        return {"test": test, "quarantined": True, "changed": False, **current[test]}
    store.insert("test_quarantine", {"test": test, "action": "remove" if remove else "add",
                                     "reason": (reason or "").strip() or None, "created_at": now()})
    known = store.one("SELECT COUNT(*) AS n FROM test_runs WHERE test = ?", (test,))["n"]
    out = {"test": test, "quarantined": not remove, "changed": True, "recorded_runs": known}
    if not remove and reason:
        out["reason"] = reason.strip()
    if not known:
        out["note"] = "no recorded run of this test id; check the spelling against `verinoda debug flaky`"
    return out


def _locate(repo: Path, test: str) -> str | None:
    """``path:line`` of a pytest test's definition in the working tree (the file alone when the line is not
    found), or None for a command id or a path outside the repository."""
    if test.startswith("$ ") or "::" not in test:
        return None
    path = test.split("::", 1)[0]
    name = test.rsplit("::", 1)[-1].split("[", 1)[0]
    try:
        f = (repo / path).resolve()
        f.relative_to(repo.resolve())
        text = f.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return None
    m = re.search(rf"^[ \t]*(?:async[ \t]+)?def[ \t]+{re.escape(name)}\b", text, re.M)
    return f"{path}:{text.count(chr(10), 0, m.start()) + 1}" if m else path


def _judge(runs: list[dict], n_verify: int) -> dict:
    """Pass rate, flaky trees and the held-fix verdict of one test's runs (oldest first)."""
    by_tree: dict[str, set[str]] = {}
    for r in runs:
        if r["tree_hash"]:
            by_tree.setdefault(r["tree_hash"], set()).add(r["outcome"])
    both = sorted(t for t, o in by_tree.items() if len(o) == 2)
    passed = sum(1 for r in runs if r["outcome"] == "pass")
    out = {"runs": len(runs), "passed": passed, "failed": len(runs) - passed,
           "pass_rate": round(passed / len(runs), 2), "flaky_trees": both}
    fails = [i for i, r in enumerate(runs) if r["outcome"] == "fail"]
    if fails and fails[-1] + 1 < len(runs):
        last = runs[fails[-1]]
        after = runs[fails[-1] + 1:]
        changed = all(r["tree_hash"] and r["tree_hash"] != last["tree_hash"] for r in after)
        out["since_last_failure"] = {"passes": len(after), "on_changed_tree": changed, "failed_run": last}
        if changed:
            out["held"] = len(after) >= n_verify
    return out


def _evidence(rows: list[dict]) -> list[str]:
    seen: list[str] = []
    for r in rows:
        if r.get("evidence_id") and r["evidence_id"] not in seen:
            seen.append(r["evidence_id"])
    return seen[:EVIDENCE_CAP]


def _entry(repo: Path, test: str, j: dict, quarantined: dict) -> dict:
    return {"test": test, "at": _locate(repo, test), "runs": j["runs"], "passed": j["passed"],
            "failed": j["failed"], "pass_rate": j["pass_rate"], "quarantined": test in quarantined}


def report(store: Store, repo: Path, *, runs: int | None = None, test: str | None = None) -> dict:
    """The flaky tests, the failing tests whose change held for ``runs`` passes, and the quarantine list."""
    from verinoda.paths import load_config

    repo = Path(repo).resolve()
    n_verify = max(1, int(runs or (load_config(repo).get("debug") or {}).get("rerun_times", 5)))
    added = sync(store)
    rows = store.all("SELECT t.*, a.n AS attempt_n FROM test_runs t LEFT JOIN debug_attempts a ON a.id = t.attempt_id "
                     "ORDER BY t.test, t.seq")
    by_test: dict[str, list[dict]] = {}
    for r in rows:
        by_test.setdefault(r["test"], []).append(r)
    q = _quarantined(store)
    flaky, held, pending = [], [], []
    for name, rs in by_test.items():
        j = _judge(rs, n_verify)
        if j.get("held"):
            last = j["since_last_failure"]["failed_run"]
            e = _entry(repo, name, j, q)
            e.update(status="strong_inference", was_flaky=bool(j["flaky_trees"]),
                     evidence=_evidence([last, rs[-1]]),
                     finding=f"passed the last {j['since_last_failure']['passes']} recorded run(s) in a row, each "
                             f"on a tree other than the one it last failed on (session {last['session_id']} attempt "
                             f"{last['attempt_n']}); a measurement on this machine, not proof the cause is gone")
            held.append(e)
        elif j["flaky_trees"]:
            tree = j["flaky_trees"][-1]
            on_tree = [r for r in rs if r["tree_hash"] == tree]
            e = _entry(repo, name, j, q)
            e.update(status="strong_inference",
                     evidence=_evidence([next(r for r in on_tree if r["outcome"] == "fail"),
                                         next(r for r in on_tree if r["outcome"] == "pass")]),
                     finding=f"passed and failed on the same tree ({len(j['flaky_trees'])} tree(s) with both "
                             f"outcomes); {j['passed']} of {j['runs']} recorded runs passed")
            flaky.append(e)
        elif "held" in j:  # passing on a changed tree since its last failure, fewer than N times
            pending.append({"test": name, "passes": j["since_last_failure"]["passes"], "of": n_verify})
    flaky.sort(key=lambda e: (e["pass_rate"], -e["runs"], e["test"]))
    held.sort(key=lambda e: e["test"])
    pending.sort(key=lambda e: (-e["passes"], e["test"]))
    flaky_names = {e["test"] for e in flaky}
    quarantine_out = []
    for name, info in sorted(q.items()):
        rs = by_test.get(name) or []
        item = {"test": name, "since": info["since"], "reason": info.get("reason"), "runs": len(rs)}
        if rs:
            j = _judge(rs, n_verify)
            item.update(pass_rate=j["pass_rate"], now="flaky" if name in flaky_names else
                        "held" if j.get("held") else "passing" if j["failed"] == 0 else "failing")
        quarantine_out.append(item)
    out = {"recorded": {"runs": len({r["attempt_id"] for r in rows}), "tests": len(by_test), "rows": len(rows),
                        "added_now": added},
           "verify_runs": n_verify, "flaky": flaky[:LIST_CAP], "held": held[:LIST_CAP],
           "pending": pending[:LIST_CAP], "quarantine": quarantine_out,
           "quarantine_candidates": [e["test"] for e in flaky if not e["quarantined"]][:LIST_CAP],
           "limits": LIMITS}
    if max(len(flaky), len(held), len(pending)) > LIST_CAP:
        out["truncated"] = True
    if test:
        rs = by_test.get(test)
        if not rs:
            out["test"] = {"test": test, "status": "unknown", "runs": 0,
                           "next_step": "no recorded run of this id; run it through the debug ledger "
                                        "(`verinoda debug rerun`) or check the id against the lists above"}
        else:
            j = _judge(rs, n_verify)
            item = {"test": test, "at": _locate(repo, test), "runs": j["runs"], "passed": j["passed"],
                    "failed": j["failed"], "pass_rate": j["pass_rate"], "flaky_trees": len(j["flaky_trees"]),
                    "quarantined": test in q,
                    "history": [{"session": r["session_id"], "attempt": r["attempt_n"], "kind": r["kind"],
                                 "outcome": r["outcome"], "tree": (r["tree_hash"] or "")[:12]}
                                for r in rs[-HISTORY_CAP:]]}
            if "since_last_failure" in j:
                s = j["since_last_failure"]
                item["since_last_failure"] = {"passes": s["passes"], "on_changed_tree": s["on_changed_tree"],
                                              "held": bool(j.get("held"))}
            out["test"] = item
    return out
