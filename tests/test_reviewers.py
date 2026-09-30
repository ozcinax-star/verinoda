"""``review``'s ``reviewers`` block (verinoda/reviewers.py): who last changed the code a change modifies, the
CODEOWNERS rule of each changed file, and the earlier commits that changed the same definitions."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import json  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import cli, workflow  # noqa: E402
from verinoda import review as rv  # noqa: E402
from verinoda.store import open_store  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")

PRICING = ("def total(items):\n    s = 0\n    for i in items:\n        s += i\n    return s\n\n\n"
           "def tax(x):\n    return x * 0.2\n")


def _git(cwd: Path, *args: str, who: str = "Me", date: str | None = None) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": who, "GIT_AUTHOR_EMAIL": f"{who.lower()}@example.org",
           "GIT_COMMITTER_NAME": who, "GIT_COMMITTER_EMAIL": f"{who.lower()}@example.org"}
    if date:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = date
    return subprocess.run(["git", "-c", "core.autocrlf=false", "-c", "init.defaultBranch=main", "-c",
                           "commit.gpgsign=false", *args], cwd=cwd, env=env, check=True, capture_output=True,
                          text=True, stdin=subprocess.DEVNULL).stdout.strip()


def _commit(repo: Path, files: dict[str, str], msg: str, who: str, date: str) -> str:
    for rel, text in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode("utf-8"))
    _git(repo, "add", "-A", who=who)
    _git(repo, "commit", "-q", "-m", msg, who=who, date=date)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture()
def repo(tmp_path):
    r = tmp_path / "who ğ reviews"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "me@example.org")
    first = _commit(r, {"shop/pricing.py": PRICING, ".github/CODEOWNERS": "shop/ @pricing-team\n",
                        ".gitignore": ".verinoda/\n"},
                    "Add pricing", "Alice", "2026-01-05T10:00:00+00:00")
    second = _commit(r, {"shop/pricing.py": PRICING.replace("return x * 0.2", "return round(x * 0.2, 2)")},
                     "Round tax to cents\n\nThe ledger stores cents.", "Bob", "2026-02-05T10:00:00+00:00")
    # the user's own earlier line inside total(): left out of the suggestions as the change's author
    third = _commit(r, {"shop/pricing.py": PRICING.replace("return round", "return round").replace(
        "    s = 0\n", "    s = 0  # start\n").replace("return x * 0.2", "return round(x * 0.2, 2)")},
        "Comment the start", "Me", "2026-03-05T10:00:00+00:00")
    workflow.init(r)
    st = open_store(r)
    try:
        workflow.scan(st, r)
    finally:
        st.close()
    return r, (first, second, third)


def _review(repo: Path) -> dict:
    st = open_store(repo)
    try:
        return rv.review(repo, store=st)
    finally:
        st.close()


def test_the_authors_of_the_changed_base_lines_are_suggested_and_the_change_author_left_out(repo):
    r, (first, second, third) = repo
    text = (r / "shop/pricing.py").read_text(encoding="utf-8")
    # the change rewrites lines 2-5 of total(): Alice wrote 3-5, the user wrote line 2
    (r / "shop/pricing.py").write_bytes(text.replace("    s = 0  # start\n    for i in items:\n        s += i\n"
                                                     "    return s\n", "    return sum(items)\n").encode("utf-8"))
    who = _review(r)["reviewers"]
    (alice,) = who["suggested"]
    assert alice["author"] == "Alice" and alice["lines"] == 3 and alice["of"] == 4
    c = alice["claim"]
    assert c["status"] == "strong_inference" and "a likely reviewer" in c["text"]
    assert c["evidence"][0]["source_type"] == "git_history" and c["evidence"][0]["locator"].startswith(
        "git blame -w -L2,5 ")
    assert who["excluded"]["author"] == "Me" and who["excluded"]["lines"] == 1
    assert who["declared"] == [{"owners": ["@pricing-team"], "files": ["shop/pricing.py"], "rule": "shop/ @pricing-team",
                                "at": ".github/CODEOWNERS:1", "status": "statically_verified"}]
    related = {x["commit"]: x for x in who["related_commits"]}
    assert set(related) == {third, first}           # Bob's commit changed tax(), not total()
    assert related[first]["claim"]["status"] == "primary_source_verified"
    assert related[first]["touched"] == ["shop/pricing.py::total"]


def test_a_change_to_bobs_line_names_bob_and_quotes_his_reason(repo):
    r, (first, second, third) = repo
    text = (r / "shop/pricing.py").read_text(encoding="utf-8")
    (r / "shop/pricing.py").write_bytes(text.replace("round(x * 0.2, 2)", "round(x * 0.21, 2)").encode("utf-8"))
    res = _review(r)
    who = res["reviewers"]
    assert [s["author"] for s in who["suggested"]] == ["Bob"]
    bob = next(x for x in who["related_commits"] if x["commit"] == second)
    assert bob["body"] == "The ledger stores cents." and bob["touched"] == ["shop/pricing.py::tax"]
    assert "Reviewers and related changes" in rv.render_text(res)


def test_added_code_and_planned_changes_name_no_one(repo):
    r, _ = repo
    text = (r / "shop/pricing.py").read_text(encoding="utf-8")
    (r / "shop/pricing.py").write_bytes((text + "\n\ndef fee(x):\n    return 1\n").encode("utf-8"))
    who = _review(r)["reviewers"]
    assert who["suggested"] == [] and who["related_commits"] == []
    st = open_store(r)
    try:
        planned = rv.review(r, store=st, targets=["shop/pricing.py::total"], change="body")
    finally:
        st.close()
    assert planned["reviewers"]["not_checked"] == "a planned change has no base lines to blame"


def test_cli_json_carries_the_block(repo, capsys):
    r, _ = repo
    text = (r / "shop/pricing.py").read_text(encoding="utf-8")
    (r / "shop/pricing.py").write_bytes(text.replace("x * 0.2", "x * 0.3").encode("utf-8"))
    cli.main(["review", "--repo", str(r), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert out["reviewers"]["suggested"][0]["author"] == "Bob"


def test_the_mcp_response_carries_a_compact_block(repo):
    from verinoda.mcp.server import AtlasTools

    r, (_, second, _) = repo
    text = (r / "shop/pricing.py").read_text(encoding="utf-8")
    (r / "shop/pricing.py").write_bytes(text.replace("x * 0.2", "x * 0.3").encode("utf-8"))
    who = AtlasTools(r).change_review()["reviewers"]
    assert who["suggested"] == [{"author": "Bob", "lines": 1, "of": 1, "status": "strong_inference"}]
    assert who["related_commits"][0]["commit"] == second[:12] and "claim" not in who["related_commits"][0]
    assert "review --json" in who["note"]
