"""``review``'s ``risk`` block (verinoda/risk.py): one heuristic roll-up of a change's findings, reach and test
coverage, every part listed with its value, weight, cap and points; a low score never says safe."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import json  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import cli, risk, workflow  # noqa: E402
from verinoda import review as rv  # noqa: E402
from verinoda.store import open_store  # noqa: E402

SHOP = ("import sqlite3\n\n\ndef total(items):\n    s = 0\n    for i in items:\n        s += i\n    return s\n\n\n"
        "def checkout(items):\n    return total(items) + 1\n")


def _res(**over) -> dict:
    """A review result with nothing found: the inputs the score reads."""
    res = {"mode": "worktree", "changes": [{"file": "a.py", "symbol": "a.py::f", "kind": "body", "lines": "3-5"}],
           "concerns": {k: [] for k in rv.CONCERNS}, "api_changes": [], "dependents": [], "dependents_total": 0,
           "tests": {"static": [], "no_test_reaches": [], "reach_unknown": []}, "unknown": [],
           "differential": {"preexisting": 0}, "counts": {"uncovered_changed_lines": 0, "api_breaking": 0},
           "coverage": {"not_checked": []}}
    res.update(over)
    return res


def _part(block: dict, name: str) -> dict:
    return next(p for p in block["parts"] if p["part"] == name)


def test_the_caps_add_up_to_the_maximum():
    assert sum(cap for _n, _w, _p, cap in risk.PARTS) == risk.MAX_SCORE


def test_every_part_is_listed_and_a_zero_score_never_says_safe():
    b = risk.score(_res(tests={"static": [], "no_test_reaches": [], "reach_unknown": [],
                               "coverage": {"uncovered": [], "patch": {}}}))
    assert b["score"] == 0 and b["band"] == "low" and b["status"] == "strong_inference"
    assert [p["part"] for p in b["parts"]] == [n for n, *_ in risk.PARTS] + ["preexisting"]
    assert all(p["points"] == 0 and p["value"] == 0 for p in b["parts"])
    assert "never a verdict that the change is safe" in b["finding"]
    assert "safe" not in b["band"] and "not_measured" not in b
    text = "\n".join(risk.render(b))
    assert "never a verdict that the change is safe" in text and "findings_strong: 0 x 10 = 0" in text


def test_the_parts_add_up_with_weights_and_caps():
    strong = [{"status": "strong_inference", "at": f"a.py:{n}", "delta": "introduced"} for n in (3, 4, 5, 6)]
    weak = [{"status": "weak_inference", "at": "a.py:9", "delta": "introduced"}]
    pre = [{"status": "statically_verified", "at": "a.py:1", "delta": "preexisting"}]
    res = _res(concerns={**{k: [] for k in rv.CONCERNS}, "security": strong + pre, "performance": weak},
               api_changes=[{"verdict": "breaking", "at": "a.py:3"}, {"verdict": "unknown", "base_at": "a.py:7"}],
               counts={"uncovered_changed_lines": 3, "api_breaking": 1},
               dependents=[{"at": "b.py:2"}], dependents_total=12,
               tests={"static": [], "no_test_reaches": ["a.py::f"], "reach_unknown": [],
                      "coverage": {"uncovered": [{"at": "a.py:4"}]}},
               unknown=[{"at": None, "what": "x"}], differential={"preexisting": 1})
    b = risk.score(res)
    got = {p["part"]: (p["value"], p["points"]) for p in b["parts"]}
    assert got == {"findings_strong": (4, 30), "findings_weak": (1, 2), "api_breaking": (1, 10),
                   "api_unknown": (1, 2), "dependents": (12, 10), "untested": (1, 5), "reach_unknown": (0, 0),
                   "uncovered_lines": (3, 3), "unknowns": (1, 1), "preexisting": (1, 0)}
    assert b["score"] == 63 and b["band"] == "high"
    assert _part(b, "findings_strong")["at"] == ["a.py:3", "a.py:4", "a.py:5"]
    assert _part(b, "untested")["at"] == ["a.py:3"] and _part(b, "api_unknown")["at"] == ["a.py:7"]
    assert "findings_strong 4x10=30" in b["finding"] and "a.py:3" in b["evidence_at"]
    assert "(cap 30)" in "\n".join(risk.render(b))
    c = risk.compact(b)
    assert c["score"] == 63 and c["parts"]["dependents"] == "12x1=10" and "never 'safe'" in c["note"]
    assert len(json.dumps(c)) < 600


def test_what_was_not_measured_is_said_never_counted_as_zero():
    b = risk.score(_res())
    assert b["status"] == "strong_inference" and b["not_measured"] == ["uncovered_lines"]
    assert _part(b, "uncovered_lines")["value"] is None
    assert "no coverage report was read" in _part(b, "uncovered_lines")["not_measured"]
    assert risk.compact(b)["parts"]["uncovered_lines"] == "not measured"
    # a report given but not readable: the reason is the unknown, not a missing --coverage
    bad = risk.score(_res(unknown=[{"kind": "coverage_report", "at": None, "why": "bad.xml: not a report"}]))
    assert "could not be read" in _part(bad, "uncovered_lines")["not_measured"]
    # a report read that measured none of the changed files is not a measured zero
    none = risk.score(_res(tests={"static": [], "no_test_reaches": [], "reach_unknown": [], "coverage": {
        "patch": {"measured_changed_lines": 0, "covered": 0, "percent": None}, "uncovered": [],
        "not_in_report": ["a.py"]}}))
    assert "measured none of the changed lines" in _part(none, "uncovered_lines")["not_measured"]
    # a planned change: no changed lines, no base to compare findings with
    only = risk.score(_res(concerns={"security": []}, mode="planned",
                           differential={"skipped": "a planned change has no base version to compare findings with"}))
    assert "a planned change" in _part(only, "uncovered_lines")["not_measured"]
    assert _part(only, "preexisting")["value"] is None and "preexisting" in only["not_measured"]
    assert any("persistence" in n and "not checked" in n for n in only["notes"])
    assert any("current code" in n for n in only["notes"])


def test_a_stale_graph_and_a_stale_report_are_said_in_the_notes():
    b = risk.score(_res(unknown=[{"kind": "graph_stale", "at": "b.py"}], counts={"uncovered_changed_lines": 2},
                        tests={"static": [], "no_test_reaches": [], "reach_unknown": [], "coverage": {
                            "patch": {"measured_changed_lines": 2}, "not_in_report": ["c.py"],
                            "uncovered": [{"at": "a.py:4", "status": "weak_inference"}]}}))
    assert _part(b, "uncovered_lines")["points"] == 2
    notes = " | ".join(b["notes"])
    assert "graph_stale" in notes and "older than the file" in notes and "not in it: c.py" in notes


pytestmark_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def _git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "Me", "GIT_AUTHOR_EMAIL": "me@example.org",
           "GIT_COMMITTER_NAME": "Me", "GIT_COMMITTER_EMAIL": "me@example.org"}
    return subprocess.run(["git", "-c", "core.autocrlf=false", "-c", "init.defaultBranch=main", "-c",
                           "commit.gpgsign=false", *args], cwd=cwd, env=env, check=True, capture_output=True,
                          text=True, stdin=subprocess.DEVNULL).stdout.strip()


@pytest.fixture()
def repo(tmp_path):
    r = tmp_path / "risk ğ repo"
    (r / "shop").mkdir(parents=True)
    (r / "shop/cart.py").write_bytes(SHOP.encode("utf-8"))
    (r / ".gitignore").write_bytes(b".verinoda/\n")
    _git(r, "init", "-q")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "cart")
    workflow.init(r)
    st = open_store(r)
    try:
        workflow.scan(st, r)
    finally:
        st.close()
    return r


@pytestmark_git
def test_a_review_carries_the_score_in_json_text_summary_and_mcp(repo, capsys):
    from verinoda.mcp.server import AtlasTools

    text = (repo / "shop/cart.py").read_text(encoding="utf-8")
    (repo / "shop/cart.py").write_bytes(text.replace("s += i", "s += i * 2").encode("utf-8"))
    cli.main(["review", "--repo", str(repo), "--json"])
    out = json.loads(capsys.readouterr().out)
    b = out["risk"]
    assert b["status"] == "strong_inference" and b["of"] == 100
    assert _part(b, "dependents")["value"] >= 1 and "shop/cart.py:11" in _part(b, "dependents")["at"]
    assert _part(b, "untested")["value"] == 1          # no test reaches total()
    assert b["score"] == sum(p["points"] for p in b["parts"])
    assert f"Risk score {b['score']}/100" in out["summary"]
    cli.main(["review", "--repo", str(repo)])
    assert "Risk score:" in capsys.readouterr().out
    m = AtlasTools(repo).change_review()
    assert m["risk"]["score"] == b["score"] and isinstance(m["risk"]["parts"], dict)
    # the score comes right after the counts and the affected tests (both compact), before the findings
    assert [k for k in m if k != "affected_tests"][:4] == ["summary", "exit", "counts", "risk"]


@pytestmark_git
def test_no_change_scores_nothing_and_says_so(repo):
    st = open_store(repo)
    try:
        res = rv.review(repo, store=st)
    finally:
        st.close()
    assert res["risk"]["score"] == 0 and "no changed definition" in res["risk"]["finding"]
    assert "Risk score" not in rv.render_text(res)


@pytestmark_git
def test_a_review_with_nothing_changed_but_an_unknown_still_scores_zero(repo):
    from verinoda.mcp.server import AtlasTools

    (repo / ".verinoda/config.json").write_text(json.dumps({"decisions": {"dir": "-oops"}}), encoding="utf-8")
    text = (repo / "shop/cart.py").read_text(encoding="utf-8")
    (repo / "shop/cart.py").write_bytes(text.replace("s = 0\n", "s = 0  # start\n").encode("utf-8"))
    st = open_store(repo)
    try:
        res = rv.review(repo, store=st)
    finally:
        st.close()
    assert not res["changes"] and res["unknown"]   # an unknown without a change: still no score
    assert res["risk"]["score"] == 0 and res["risk"]["parts"] == [] and "no changed definition" in \
        res["risk"]["finding"]
    assert AtlasTools(repo).change_review()["risk"]["score"] == 0


def _review_with(repo, **kw) -> dict:
    st = open_store(repo)
    try:
        return rv.review(repo, store=st, **kw)
    finally:
        st.close()


@pytestmark_git
def test_a_coverage_report_counts_uncovered_lines_or_says_it_measured_none(repo):
    text = (repo / "shop/cart.py").read_text(encoding="utf-8")
    (repo / "shop/cart.py").write_bytes(text.replace("s += i", "s += i * 2").encode("utf-8"))
    later = 4102444800   # 2100-01-01: written after every file of the tree
    rp = repo / "coverage.json"
    rp.write_text(json.dumps({"files": {"shop/cart.py": {"executed_lines": [1, 4, 5, 6, 8],
                                                         "missing_lines": [7]}}}), encoding="utf-8")
    os.utime(rp, (later, later))
    b = _review_with(repo, coverage_reports=["coverage.json"])["risk"]
    assert _part(b, "uncovered_lines")["value"] == 1 and _part(b, "uncovered_lines")["at"] == ["shop/cart.py:7"]
    assert "uncovered_lines" not in (b.get("not_measured") or [])
    rp.write_text(json.dumps({"files": {"other/x.py": {"executed_lines": [1], "missing_lines": []}}}),
                  encoding="utf-8")
    os.utime(rp, (later, later))
    res = _review_with(repo, coverage_reports=["coverage.json"])
    assert res["tests"]["coverage"]["not_in_report"] == ["shop/cart.py"]
    b = res["risk"]
    assert "uncovered_lines" in b["not_measured"] and _part(b, "uncovered_lines")["value"] is None
    assert "shop/cart.py" in _part(b, "uncovered_lines")["not_measured"]
    (repo / "bad.json").write_text("{not json", encoding="utf-8")
    b = _review_with(repo, coverage_reports=["bad.json"])["risk"]
    assert "could not be read" in _part(b, "uncovered_lines")["not_measured"]


@pytestmark_git
def test_preexisting_findings_listed_with_findings_all_are_not_scored(repo):
    (repo / "shop/run.py").write_bytes(b"import subprocess\n\n\ndef run(cmd):\n"
                                       b"    return subprocess.run(cmd, shell=True)\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "run")
    st = open_store(repo)
    try:
        workflow.scan(st, repo)
    finally:
        st.close()
    (repo / "shop/run.py").write_bytes(b"import subprocess\n\n\ndef run(cmd):\n"
                                       b"    done = subprocess.run(cmd, shell=True)\n    return done\n")
    res = _review_with(repo, concerns=["security"], findings="all")
    shown = [f for fs in res["concerns"].values() for f in fs]
    assert shown and {f["delta"] for f in shown} == {"preexisting"}
    b = res["risk"]
    assert _part(b, "findings_strong")["value"] == 0 and _part(b, "findings_weak")["value"] == 0
    assert _part(b, "preexisting")["value"] == len(shown)


@pytestmark_git
def test_a_planned_change_names_what_it_cannot_measure(repo):
    b = _review_with(repo, targets=["shop/cart.py::total"], change="signature")["risk"]
    assert _part(b, "preexisting")["value"] is None and "no base version" in _part(b, "preexisting")["not_measured"]
    assert _part(b, "uncovered_lines")["value"] is None
    assert any("current code" in n for n in b["notes"])
    assert risk.compact(b)["parts"]["preexisting"] == "not measured"
