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
    b = risk.score(_res(), graph=False)
    assert b["status"] == "weak_inference"
    assert b["not_measured"] == ["dependents", "reach_unknown", "uncovered_lines", "untested"]
    assert _part(b, "dependents")["value"] is None
    assert "no coverage report was read" in _part(b, "uncovered_lines")["not_measured"]
    assert risk.compact(b)["parts"]["untested"] == "not measured"
    only = risk.score(_res(concerns={"security": []}, mode="planned"))
    assert "a planned change" in _part(only, "uncovered_lines")["not_measured"]
    assert any("persistence" in n and "not checked" in n for n in only["notes"])


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
    assert list(m)[:4] == ["summary", "exit", "counts", "risk"]


@pytestmark_git
def test_no_change_scores_nothing_and_says_so(repo):
    st = open_store(repo)
    try:
        res = rv.review(repo, store=st)
    finally:
        st.close()
    assert res["risk"]["score"] == 0 and "no changed definition" in res["risk"]["finding"]
    assert "Risk score" not in rv.render_text(res)
