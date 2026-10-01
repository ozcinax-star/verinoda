"""``review --since-last`` (verinoda/review.py): a second review leaves out the findings the last one of the same
base and mode already listed, says how many, lists the ones gone and names the changes that are new."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from verinoda import cli, workflow
from verinoda import review as rv
from verinoda.store import open_store

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")

ORDERS = Path(__file__).resolve().parent.parent / "examples" / "orders_app"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false",
                    "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main", *args], cwd=cwd, check=True,
                   capture_output=True, stdin=subprocess.DEVNULL)


@pytest.fixture()
def orders(tmp_path):
    dst = tmp_path / "since ğ last" / "orders"
    shutil.copytree(ORDERS, dst, ignore=shutil.ignore_patterns(".verinoda", "__pycache__", "*.pyc", "*.db"))
    (dst / ".gitignore").write_text("__pycache__/\n*.pyc\n.verinoda/\n", encoding="utf-8")
    _git(dst, "init", "-q")
    _git(dst, "add", "-A")
    _git(dst, "commit", "-q", "-m", "init")
    workflow.init(dst)
    st = open_store(dst)
    try:
        workflow.scan(st, dst)
    finally:
        st.close()
    return dst


def _edit(repo: Path, rel: str, find: str, replace: str) -> None:
    p = repo / rel
    text = p.read_text(encoding="utf-8")
    assert text.count(find) == 1, (rel, find)
    p.write_text(text.replace(find, replace, 1), encoding="utf-8", newline="\n")


def _review(repo: Path, **kw) -> dict:
    st = open_store(repo)
    try:
        return rv.review(repo, store=st, **kw)
    finally:
        st.close()


def _n(res: dict) -> int:
    return sum(len(v) for v in res["concerns"].values())


def test_a_second_review_shows_only_new_findings(orders):
    _edit(orders, "orders/pricing.py", "def apply_discount(subtotal: float) -> float:",
          "def apply_discount(subtotal: float, rate: float) -> float:")
    first = _review(orders, since_last=True)
    assert first["since_last"]["review_id"] is None and "every finding is new" in first["since_last"]["note"]
    n_first = _n(first)
    assert n_first > 0
    # the same change again, with lines moved by an edit above: nothing new
    _edit(orders, "orders/pricing.py", '"""Pricing rules."""', '"""Pricing rules."""\n\n# moved down')
    again = _review(orders, since_last=True)
    sl = again["since_last"]
    assert sl["review_id"] == first["review_id"] and sl["repeated"] == n_first and _n(again) == 0
    assert again["counts"]["findings_repeated"] == n_first and again["counts"]["findings_new"] == 0
    assert "repeated and left out" in again["summary"]
    # a new change: only its findings are listed, and it is named as new
    p = orders / "orders/config.py"
    p.write_text(p.read_text(encoding="utf-8") + "\n\ndef run_report(cmd):\n    return os.system(cmd)\n",
                 encoding="utf-8", newline="\n")
    third = _review(orders, since_last=True)
    assert third["since_last"]["repeated"] >= n_first and _n(third) >= 1
    assert any(c.startswith("orders/config.py") for c in third["since_last"]["new_changes"])
    # without --since-last the review is unchanged: every finding
    assert _n(_review(orders)) >= n_first + _n(third)


def test_findings_that_went_away_are_listed_as_gone(orders):
    _edit(orders, "orders/pricing.py", "def apply_discount(subtotal: float) -> float:",
          "def apply_discount(subtotal: float, rate: float) -> float:")
    first = _review(orders, since_last=True)
    _edit(orders, "orders/pricing.py", "def apply_discount(subtotal: float, rate: float) -> float:",
          "def apply_discount(subtotal: float) -> float:")
    res = _review(orders, since_last=True)
    assert res["since_last"]["gone_total"] == _n(first) > 0
    assert "gone:" in rv.render_text(res)


def test_cli_and_mcp(orders, capsys):
    from verinoda.mcp.server import AtlasTools

    _edit(orders, "orders/pricing.py", "def apply_discount(subtotal: float) -> float:",
          "def apply_discount(subtotal: float, rate: float) -> float:")
    cli.main(["review", "--repo", str(orders), "--json"])
    capsys.readouterr()
    cli.main(["review", "--repo", str(orders), "--since-last", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert out["since_last"]["repeated"] > 0 and _n(out) == 0
    res = AtlasTools(orders).change_review(since_last=True)
    assert res["since_last"]["repeated"] > 0
