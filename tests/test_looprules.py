"""Loop rules of the debug ledger, on hand-built attempt records (no runs)."""

from __future__ import annotations

from verinoda import looprules as lr


def _sig(*fails, status="parsed"):
    return {"status": status, "failures": [{"test": t, "exc": e, "path": p, "symbol": s, "at": f"{p}::{s}",
                                            "frames": []} for t, e, p, s in fails],
            "failed_tests": sorted({f[0] for f in fails})}


A = _sig(("t::a", "KeyError", "m.py", "f"))
B = _sig(("t::a", "AssertionError", "tests/t.py", "test_a"))


def _att(n, *, kind="fix", outcome="fail", tree=None, files=None, sig=None, exact=None, coarse=None,
         vs_prev=None, hyp="h", expect="pass", trace=None, progress=None, code=None, run_by="verinoda"):
    sig = sig if sig is not None else ({"status": "none", "failures": []} if outcome == "pass" else A)
    return {"n": n, "kind": kind, "outcome": outcome, "tree_hash": tree or f"t{n}", "tree_files": files or {},
            "signature": sig, "sig_exact": exact, "sig_coarse": coarse, "vs_prev": vs_prev or [],
            "hypothesis": hyp, "hypothesis_terms": lr.terms(hyp), "expect": expect, "trace": trace or {},
            "progress": progress, "run_by": run_by, "experiment_id": f"exp_{n}" if run_by == "verinoda" else None,
            "code_hash": code}


def _rules(res, strength="definitive"):
    return [f["rule"] for f in res["findings"] if f["strength"] == strength]


def _change(path, symbols=("f",), *, test=False, status="modified", removed=(), added=(), kinds=None, old=(1, 1),
            new=(1, 1)):
    return {"path": path, "status": status, "test": test, "symbols": list(symbols),
            "kinds": kinds or {s: "def" for s in symbols},
            "hunks": [{"old": list(old), "new": list(new), "symbols": list(symbols), "removed": list(removed),
                       "added": list(added)}]}


# -- tree_reverted ---------------------------------------------------------------------------------

def test_whole_tree_back_to_an_earlier_state_is_definitive():
    h = [_att(0, kind="baseline", tree="T0", exact="A"), _att(1, tree="T1", exact="A")]
    res = lr.evaluate(h, _att(2, tree="T0", exact="A"))
    assert "tree_reverted" in _rules(res) and res["stop"]
    f = next(f for f in res["findings"] if f["rule"] == "tree_reverted")
    assert [e["attempt"] for e in f["evidence"]] == [0, 1, 2]


def test_a_rerun_of_the_same_tree_is_not_a_revert():
    h = [_att(0, kind="baseline", tree="T0", exact="A")]
    res = lr.evaluate(h, _att(1, kind="probe", tree="T0", exact="A"))
    assert _rules(res) == [] and not res["stop"]


def test_one_file_back_with_new_code_elsewhere_is_only_a_heuristic():
    # review finding: undoing one's own last edit to a file while fixing another file is not a loop (no stop)
    base0 = {"m.py": "c0"}
    h = [_att(0, kind="baseline", tree="T0", files=base0, exact="A", code="K0"),
         _att(1, tree="T1", files={"m.py": "c1"}, exact="A", code="K1"),
         _att(2, tree="T2", files={"m.py": "c2"}, exact="B", sig=B, code="K2")]
    back = _att(3, tree="T3", files={"m.py": "c1", "n.py": "x"}, exact="C", vs_prev=[_change("m.py")], code="K3")
    res = lr.evaluate(h, back)
    assert "tree_reverted" not in _rules(res) and "file_reverted" in _rules(res, "heuristic")
    f = next(f for f in res["findings"] if f["rule"] == "file_reverted")
    assert "the rest of the tree differs from attempt 1's" in f["text"]
    undo = _att(3, tree="T3", files={"m.py": "c0", "n.py": "x"}, exact="C", vs_prev=[_change("m.py")], code="K3")
    assert "file_reverted" not in _rules(lr.evaluate(h, undo), "heuristic")  # back to the starting content
    to_base = _att(3, tree="T3", files={"n.py": "x"}, exact="C", vs_prev=[_change("m.py")], code="K3")
    assert "file_reverted" not in _rules(lr.evaluate(h, to_base), "heuristic")


def test_the_same_code_again_is_a_definitive_revert():
    # the tree differs from attempt 1's only in comments/docstrings: the code was already run
    h = [_att(0, kind="baseline", tree="T0", exact="A", code="K0"), _att(1, tree="T1", exact="A", code="K1"),
         _att(2, tree="T2", exact="B", sig=B, code="K2")]
    res = lr.evaluate(h, _att(3, tree="T3", exact="A", code="K1"))
    f = next(f for f in res["findings"] if f["rule"] == "tree_reverted")
    assert f["strength"] == "definitive" and f.get("same") == "code" and res["stop"]
    assert "comments, docstrings or formatting" in f["text"] and [e["attempt"] for e in f["evidence"]] == [1, 2, 3]
    # the same code as the previous attempt (a docstring edit) is no revert
    assert "tree_reverted" not in _rules(lr.evaluate(h, _att(3, tree="T3", exact="B", sig=B, code="K2")))


def test_a_passing_attempt_is_stopped_only_by_the_test_rules():
    h = [_att(0, kind="baseline", tree="T0", exact="A", code="K0"), _att(1, tree="T1", exact="A", code="K1"),
         _att(2, tree="T2", exact="B", sig=B, code="K2")]
    res = lr.evaluate(h, _att(3, tree="T3", outcome="pass", code="K1"))
    assert "tree_reverted" in _rules(res) and not res["stop"]


# -- signatures ------------------------------------------------------------------------------------

def test_signature_recurred_needs_a_different_known_failure_between():
    h = [_att(0, kind="baseline", exact="A"), _att(1, exact="B", sig=B)]
    res = lr.evaluate(h, _att(2, exact="A"))
    assert _rules(res) == ["signature_recurred"] and res["stop"]
    unknown_between = [_att(0, kind="baseline", exact="A"), _att(1, exact=None, sig=_sig(status="unknown"))]
    assert "signature_recurred" not in _rules(lr.evaluate(unknown_between, _att(2, exact="A")))


def test_an_unknown_signature_never_counts_as_the_same():
    unk = _sig(status="unknown")
    h = [_att(0, kind="baseline", sig=unk), _att(1, sig=unk), _att(2, sig=unk)]
    res = lr.evaluate(h, _att(3, sig=unk))
    assert _rules(res) == [] and lr.progress(h[-1], _att(3, sig=unk)) == "unknown"


def test_no_progress_needs_three_fix_attempts_on_different_trees():
    h = [_att(0, kind="baseline", exact="A"), _att(1, exact="A"), _att(2, exact="A")]
    res = lr.evaluate(h, _att(3, exact="A"))
    assert "no_progress" in _rules(res)
    only_two = lr.evaluate(h[:2], _att(2, exact="A"))
    assert "no_progress" not in _rules(only_two)
    same_trees = [_att(0, kind="baseline", exact="A"), _att(1, tree="X", exact="A"), _att(2, tree="Y", exact="A")]
    assert "no_progress" not in _rules(lr.evaluate(same_trees, _att(3, tree="X", exact="A")))


def test_no_progress_does_not_span_a_passing_attempt():
    # review finding: a flaky test (fail, pass, fail, fail) is not three fix attempts without progress
    h = [_att(0, kind="baseline", exact="A"), _att(1, exact="A"), _att(2, outcome="pass"), _att(3, exact="A")]
    assert "no_progress" not in _rules(lr.evaluate(h, _att(4, exact="A")))


# -- test_edited -------------------------------------------------------------------------------------

def test_editing_only_existing_tests_is_definitive_and_asks_the_user():
    h = [_att(0, kind="baseline", exact="A")]
    cur = _att(1, exact="A", vs_prev=[_change("tests/test_x.py", ("test_a",), test=True, removed=['x = {"qty": 2}'],
                                               added=['x = {"quantity": 2}'])])
    res = lr.evaluate(h, cur)
    assert _rules(res) == ["test_edited"] and res["stop"]


def test_changing_an_assertion_while_also_editing_code_is_definitive():
    h = [_att(0, kind="baseline", exact="A")]
    cur = _att(1, exact="A", vs_prev=[_change("m.py"), _change("tests/test_x.py", ("test_a",), test=True,
                                                                   removed=["    assert total() == 20.0"],
                                                                   added=["    assert total() == 22.0"], old=(7, 7))])
    res = lr.evaluate(h, cur)
    assert _rules(res) == ["test_edited"]
    assert res["assertion_lines"] == [{"path": "tests/test_x.py", "line": 7, "text": "assert total() == 20.0",
                                       "change": "assertion"}]


def test_reformatting_an_assertion_is_not_a_test_edit():
    # review finding: quote style changed on an assertion line, the fix in the code: no stop, no question
    h = [_att(0, kind="baseline", exact="A")]
    quotes = _change("tests/test_x.py", ("test_a",), test=True,
                     removed=['    assert total([{"price": 10.0, "qty": 2}]) == 20.0'],
                     added=["    assert total([{'price': 10.0, 'qty': 2}]) == 20.0"])
    res = lr.evaluate(h, _att(1, outcome="pass", vs_prev=[_change("m.py"), quotes]))
    assert _rules(res) == [] and not res["stop"]
    whole_file_same_code = dict(_change("tests/test_x.py", ("test_a",), test=True, removed=["x"], added=["y"]),
                                no_code_change=True)
    assert _rules(lr.evaluate(h, _att(1, vs_prev=[whole_file_same_code]))) == []


def test_switching_a_failing_test_off_is_a_test_edit():
    # review finding: skip / xfail / an early return added to the failing test made the repro "pass"
    h = [_att(0, kind="baseline", exact="A")]
    for line, sym in (("@pytest.mark.skip(reason='flaky')", "test_a"), ("@pytest.mark.xfail", "test_a"),
                      ("    return", "test_a"), ("    pytest.skip('later')", "test_a")):
        cur = _att(1, outcome="pass", vs_prev=[_change("tests/test_x.py", (sym,), test=True, added=[line])])
        res = lr.evaluate(h, cur)
        assert _rules(res) == ["test_edited"] and res["stop"], line
        assert res["assertion_lines"][0]["change"] == "switched off"
    helper = _att(1, vs_prev=[_change("tests/test_x.py", ("make_items",), test=True, added=["    return"])])
    assert _rules(lr.evaluate(h, helper)) == []  # an early return in a helper is not a test switched off


def test_changing_the_test_selection_in_the_configuration_is_a_test_edit():
    h = [_att(0, kind="baseline", exact="A")]
    cfg = _change("pyproject.toml", ("#tool.pytest.ini_options",), added=["addopts = \"-k 'not compute_total'\""])
    res = lr.evaluate(h, _att(1, outcome="pass", vs_prev=[cfg]))
    assert _rules(res) == ["test_edited"] and res["stop"]
    assert res["assertion_lines"][0]["change"] == "test selection"


def test_the_failing_tests_own_file_is_a_test_file_whatever_its_name():
    # review finding: src/cart.test.js was not a test path; the file a failing test lives in is one
    sig = _sig(("src/cart.check.js::<module>", "AssertionError", "src/cart.check.js", "<module>"))
    h = [_att(0, kind="baseline", exact="A", sig=sig)]
    cur = _att(1, outcome="pass", vs_prev=[_change("src/cart.check.js", ("<module>",), removed=["}]), 7);"],
                                                   added=["}]), 1);"])])
    res = lr.evaluate(h, cur)
    assert _rules(res) == ["test_edited"] and res["stop"]


def test_failing_tests_that_were_skipped_or_not_run_are_no_pass():
    tests0 = {"t::a": "failed", "t::b": "failed", "t::c": "passed"}
    base = _att(0, kind="baseline", exact="A", sig=dict(_sig(("t::a", "E", "m.py", "f"), ("t::b", "E", "m.py", "f")),
                                                         tests=tests0))
    cur = _att(1, outcome="pass", sig={"status": "none", "failures": [], "failed_tests": [],
                                       "tests": {"t::a": "skipped", "t::c": "passed"}})
    res = lr.evaluate([base], cur)
    assert _rules(res) == ["failing_tests_skipped"] and res["stop"] and res["progress"] == "unknown"
    f = res["findings"][0]
    assert f["tests"] == {"t::a": "skipped", "t::b": "not run"}
    ok = _att(1, outcome="pass", sig={"status": "none", "failures": [], "failed_tests": [],
                                      "tests": {"t::a": "passed", "t::b": "passed", "t::c": "passed"}})
    assert _rules(lr.evaluate([base], ok)) == [] and lr.evaluate([base], ok)["progress"] == "improved"
    no_outcomes = _att(1, outcome="pass")  # not pytest: unknown, no finding
    assert _rules(lr.evaluate([base], no_outcomes)) == []


def test_adding_a_new_test_file_is_not_test_edited():
    h = [_att(0, kind="baseline", exact="A")]
    cur = _att(1, exact="A", vs_prev=[_change("tests/test_new.py", ("test_b",), test=True, status="added",
                                               added=["    assert f() == 1"])])
    assert _rules(lr.evaluate(h, cur)) == []
    probe = _att(1, exact="A", vs_prev=[_change("tests/test_x.py", ("test_a",), test=True,
                                                 added=["    print('items', items)"])])
    assert _rules(lr.evaluate(h, probe)) == []  # only lines added to a test: a probe, not an edit


def test_a_passing_attempt_that_edited_the_test_still_stops():
    h = [_att(0, kind="baseline", exact="A")]
    cur = _att(1, outcome="pass", vs_prev=[_change("tests/test_x.py", ("test_a",), test=True,
                                                    removed=["    assert f() == 1"], added=["    assert f() == 2"])])
    res = lr.evaluate(h, cur)
    assert res["stop"] and _rules(res) == ["test_edited"] and res["progress"] == "unknown"


# -- off_path ------------------------------------------------------------------------------------------

def _trace(complete=True, reached=("m.py::f",), anywhere=None, spawns=()):
    return {"run_id": "rtr_1", "complete": complete, "failing": ["t::a"], "called": ["t::a"],
            "reached": {"t::a": list(reached)}, "reached_any": list(anywhere if anywhere is not None else reached),
            "spawns": list(spawns)}


def test_off_path_needs_a_complete_trace_and_unreached_edited_functions():
    h = [_att(0, kind="baseline", exact="A")]
    edit = [_change("cfg.py", ("load_settings",))]
    files = {"cfg.py": "c1"}
    res = lr.evaluate(h, _att(1, exact="A", files=files, vs_prev=edit, trace=_trace()))
    assert _rules(res) == ["off_path"] and res["stop"]
    reached = lr.evaluate(h, _att(1, exact="A", files=files, vs_prev=edit,
                                  trace=_trace(reached=("m.py::f", "cfg.py::load_settings"))))
    assert _rules(reached) == []
    incomplete = lr.evaluate(h, _att(1, exact="A", files=files, vs_prev=edit, trace=_trace(complete=False)))
    assert _rules(incomplete) == []
    # review findings: called at import time (or by another test) is reached; a child process is untraced
    at_import = lr.evaluate(h, _att(1, exact="A", files=files, vs_prev=edit,
                                    trace=_trace(anywhere=("m.py::f", "cfg.py::load_settings"))))
    assert _rules(at_import) == []
    child = lr.evaluate(h, _att(1, exact="A", files=files, vs_prev=edit, trace=_trace(spawns=("subprocess.run",))))
    assert _rules(child) == []
    old_trace = dict(_trace())
    del old_trace["reached_any"]  # no record of what the whole run called: unknown
    assert _rules(lr.evaluate(h, _att(1, exact="A", files=files, vs_prev=edit, trace=old_trace))) == []
    module_level = [_change("cfg.py", ("<module>",), kinds={"<module>": "module"})]
    assert _rules(lr.evaluate(h, _att(1, exact="A", files=files, vs_prev=module_level, trace=_trace()))) == []
    non_python = [_change("cfg.json", ("x",))]
    assert _rules(lr.evaluate(h, _att(1, exact="A", files={"cfg.json": "1"}, vs_prev=non_python,
                                      trace=_trace()))) == []


# -- heuristics -----------------------------------------------------------------------------------------

def test_heuristics_never_stop_on_their_own():
    prev = _att(1, exact="A", coarse="cA", sig=A)
    moved = _att(2, exact="B2", coarse="cB",
                 sig=_sig(("t::a", "AssertionError", "tests/t.py", "test_a")),
                 vs_prev=[_change("m.py", ("f",), added=['    q = i.get("quantity", 0)'])])
    res = lr.evaluate([_att(0, kind="baseline", exact="A0", coarse="c0", tree="T0"), prev], moved)
    assert set(_rules(res, "heuristic")) >= {"error_moved", "masking"} and not res["stop"]


def test_hypothesis_repeated_uses_refuted_attempts_only():
    h = [_att(0, kind="baseline", exact="A"),
         _att(1, exact="A", hyp="the discount rounding is off by one cent")]
    res = lr.evaluate(h, _att(2, exact="B", sig=B, hyp="discount rounding is off by one cent again"))
    assert "hypothesis_repeated" in _rules(res, "heuristic") and not res["stop"]
    confirmed = [_att(0, kind="baseline", exact="A"),
                 _att(1, outcome="pass", hyp="the discount rounding is off by one cent")]
    assert "hypothesis_repeated" not in _rules(lr.evaluate(confirmed, _att(2, exact="B", sig=B,
                                                                            hyp="discount rounding is off by one "
                                                                                "cent again")), "heuristic")
    # review finding: an attempt that improved (4 failing -> 1) is not "refuted"
    improved = [_att(0, kind="baseline", exact="A"),
                _att(1, exact="A", hyp="the discount rounding is off by one cent", progress="improved")]
    assert "hypothesis_repeated" not in _rules(lr.evaluate(improved, _att(2, exact="B", sig=B,
                                                                           hyp="discount rounding is off by one "
                                                                               "cent again")), "heuristic")
    f = next(f for f in res["findings"] if f["rule"] == "hypothesis_repeated")
    assert "refuted" not in f["text"] and "did not make the repro pass" in f["text"]


# -- flaky, progress, budget --------------------------------------------------------------------------------

def test_flaky_suspends_the_loop_rules_until_a_stable_rerun_series():
    h = [_att(0, kind="baseline", tree="T0", exact="A"), _att(1, kind="probe", tree="T0", outcome="pass"),
         _att(2, tree="T1", exact="A")]
    res = lr.evaluate(h, _att(3, tree="T0", exact="A"))  # back to T0: tree_reverted, suspended
    assert res["flaky"] and not res["stop"] and res["findings"] and all(f.get("suspended") for f in res["findings"])
    stable = h + [_att(3, tree="T2", exact="A"), _att(4, kind="rerun", tree="T2", exact="A"),
                  _att(5, kind="rerun", tree="T2", exact="A"), _att(6, kind="rerun", tree="T2", exact="A")]
    res2 = lr.evaluate(stable, _att(7, tree="T4", exact="A"))
    assert res2["flaky"] is None and res2["stop"]


def test_the_no_progress_budget_holds_while_flaky():
    # review finding: flaky suspended the budget too, so a real loop on a flaky-looking session never stopped
    h = [_att(0, kind="baseline", tree="T0", exact="A"), _att(1, kind="probe", tree="T0", outcome="pass"),
         _att(2, tree="T1", exact="A"), _att(3, tree="T2", exact="A")]
    res = lr.evaluate(h, _att(4, tree="T3", exact="A"))
    assert res["flaky"] and res["stop"] and res["stop_reason"].startswith("max_no_progress")
    assert "flaky" in res["stop_reason"] and all(f.get("suspended") for f in res["findings"])


def test_a_message_that_changes_every_run_is_not_flakiness():
    # review finding: a temp path or a time in the message gave a new sig_exact per run -> "flaky"
    h = [_att(0, kind="baseline", tree="T0", exact="A1", coarse="cA")]
    res = lr.evaluate(h, _att(1, kind="probe", tree="T0", exact="A2", coarse="cA"))
    assert res["flaky"] is None and res["progress"] == "same"
    other_tests = _att(1, kind="probe", tree="T0", exact="A1", coarse="cA",
                       sig=_sig(("t::b", "KeyError", "m.py", "f")))
    assert lr.evaluate(h, other_tests)["flaky"]  # other failing tests on the same tree: flaky


def test_an_agent_report_does_not_make_a_tree_verinoda_ran_flaky():
    # review finding: one agent-reported pass outweighed three failing runs by Verinoda
    h = [_att(0, kind="baseline", tree="T0", exact="A", coarse="cA"),
         _att(1, tree="T0", outcome="pass", run_by="agent")]
    res = lr.evaluate(h, _att(2, tree="T0", exact="A", coarse="cA"))
    assert res["flaky"] is None
    only_reports = [_att(0, kind="baseline", tree="T0", exact="A", run_by="agent"),
                    _att(1, kind="probe", tree="T0", outcome="pass", run_by="agent")]
    assert lr.flaky_state(only_reports)  # reports can still disagree among themselves


def test_progress_values():
    two = _sig(("t::a", "E", "m.py", "f"), ("t::b", "E", "m.py", "f"))
    one = _sig(("t::a", "E", "m.py", "f"))
    assert lr.progress(_att(0, sig=two), _att(1, sig=one)) == "improved"
    assert lr.progress(_att(0, sig=one), _att(1, sig=two)) == "regressed"
    assert lr.progress(_att(0, sig=one), _att(1, sig=one)) == "same"
    assert lr.progress(_att(0), _att(1, outcome="pass")) == "improved"
    assert lr.progress(_att(0, outcome="pass"), _att(1)) == "regressed"
    assert lr.progress(_att(0), _att(1, outcome="timeout")) == "unknown"
    # review finding: the same tree (or the same code) with another result is not the edit's doing
    assert lr.progress(_att(0, tree="T"), _att(1, tree="T", outcome="pass")) == "unknown"
    assert lr.progress(_att(0, code="K"), _att(1, code="K", outcome="pass")) == "unknown"
    assert lr.progress(_att(0, tree="T", sig=one), _att(1, tree="T", sig=one)) == "same"


def test_the_same_code_with_another_result_is_possibly_flaky():
    # review finding: docstring-only edits between a fail and a pass were called "improved"
    h = [_att(0, kind="baseline", tree="T0", exact="A", code="K"), _att(1, tree="T1", exact="A", code="K")]
    res = lr.evaluate(h, _att(2, tree="T2", outcome="pass", code="K"))
    assert _rules(res, "heuristic") == ["possibly_flaky"] and res["progress"] == "unknown" and not res["stop"]


def test_max_no_progress_is_a_budget_stop_not_a_finding():
    h = [_att(0, kind="baseline", exact="A"), _att(1, exact="B", sig=B, progress="same"),
         _att(2, exact="C", sig=A, progress="same")]
    res = lr.evaluate(h, _att(3, exact="D", sig=B), max_no_progress=3)
    assert res["stop"] and res["stop_reason"].startswith("max_no_progress") and not _rules(res)
