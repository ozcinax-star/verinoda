"""Safety regressions from the acceptance audit (2026-09-23)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from verinoda import evidence as evmod
from verinoda.paths import find_repo_root
from verinoda.store import NotInitialised, open_store


def _repo(tmp_path: Path) -> Path:
    r = tmp_path / "proj"
    (r / "pkg").mkdir(parents=True)
    (r / "pkg" / "m.py").write_text("\n\ndef f():\n    return 1\n", encoding="utf-8")
    return r


def test_blank_reversed_and_outside_lines_are_not_evidence(tmp_path):
    r = _repo(tmp_path)
    (tmp_path / "outside.py").write_text("SECRET = 1\n", encoding="utf-8")
    assert evmod.source_evidence(r, "pkg/m.py", 1, commit=None) is None          # blank line
    assert evmod.source_evidence(r, "pkg/m.py", 4, 3, commit=None) is None       # reversed range
    assert evmod.source_evidence(r, "../outside.py", 1, commit=None) is None     # escapes the repo
    assert evmod.source_evidence(r, str(tmp_path / "outside.py"), 1, commit=None) is None  # absolute
    ok = evmod.source_evidence(r, "pkg/m.py", 3, 4, commit=None)
    assert ok and ok["locator"] == "pkg/m.py:3-4" and ok["excerpt"].startswith("def f")


def test_reference_checkout_evidence_stays_inside_its_root(tmp_path):
    ref = _repo(tmp_path / "ref")
    ok = evmod.source_evidence(tmp_path, "pkg/m.py", 3, commit="abc", source_type="reference_repo",
                               meta={"root": str(ref)})
    assert ok and ok["meta"]["root"] == str(ref)
    assert evmod.source_evidence(tmp_path, "../../x.py", 1, commit="abc", source_type="reference_repo",
                                 meta={"root": str(ref)}) is None


def test_home_is_never_the_implicit_project_root(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".verinoda").mkdir(parents=True)          # e.g. left by a user-scope install
    (home / ".verinoda" / "atlas.db").write_bytes(b"")  # even a real-looking store
    proj = home / "code" / "app"
    proj.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    assert find_repo_root(proj) == proj.resolve()
    assert find_repo_root(home) == home.resolve()       # only when run from exactly there


def test_nearest_git_or_initialised_project_wins(tmp_path):
    outer = tmp_path / "outer"
    inner = outer / "inner"
    (inner / "src").mkdir(parents=True)
    (outer / ".verinoda").mkdir()
    (outer / ".verinoda" / "atlas.db").write_bytes(b"")
    (inner / ".git").mkdir()
    assert find_repo_root(inner / "src") == inner.resolve()
    bare = tmp_path / "bare"
    (bare / ".verinoda").mkdir(parents=True)            # no atlas.db: not a project
    assert find_repo_root(bare) == bare.resolve()


def test_read_only_commands_never_create_state(tmp_path):
    d = tmp_path / "plain"
    d.mkdir()
    with pytest.raises(NotInitialised):
        open_store(d, create=False)
    assert not (d / ".verinoda").exists()
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    for args in (["claim", "list"], ["memory", "list"], ["feedback", "list"]):
        r = subprocess.run([sys.executable, "-m", "verinoda", *args, "--repo", str(d)],
                           capture_output=True, text=True, env=env, timeout=120)
        assert r.returncode != 0, args
    assert not (d / ".verinoda").exists()


# -- criterion 6: no path verifies a claim through irrelevant evidence (audit 2026-09-23) --------

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "orders_app"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args],
                   cwd=cwd, check=True, capture_output=True, stdin=subprocess.DEVNULL)


@pytest.fixture
def orders(tmp_path):
    import shutil

    from verinoda import workflow

    if shutil.which("git") is None:
        pytest.skip("git not available")
    repo = tmp_path / "orders_app"
    shutil.copytree(EXAMPLE, repo, ignore=shutil.ignore_patterns(".verinoda", "__pycache__", "*.pyc",
                                                                 ".pytest_cache", "*.db"))
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    st = open_store(repo)
    workflow.scan(st, repo)
    yield repo, st
    st.close()


def test_feedback_confirmed_with_an_unrelated_evidence_id_does_not_verify(orders):
    """Audit: "Orders are stored in PostgreSQL" became statically_verified through MCP feedback_resolve."""
    from verinoda.claims import VERIFIED, Claims
    from verinoda.mcp.server import AtlasTools

    repo, st = orders
    snap = st.latest_snapshot()
    c = Claims(st, repo).create("Orders are stored in PostgreSQL", project=snap["project"], snapshot=snap,
                                status="statically_verified", actor="user")  # `claim add` without --source
    assert c["status"] == "unknown"
    unrelated = evmod.add(st, evmod.source_evidence(repo, "orders/config.py", 7, commit=snap["commit_sha"]))
    t = AtlasTools(repo)
    fb = t.feedback_submit("I am sure this is right", claim_id=c["id"], process=False)
    res = t.feedback_resolve(fb["id"], "confirmed", "unrelated line", evidence_ids=[unrelated])
    after = Claims(st, repo).get(c["id"])
    # foreign evidence may never raise a claim: the resolve is rejected and the claim stays unknown
    assert res.get("status") == "rejected" and "foreign evidence" in str(res.get("error", "")), res
    assert after["status"] == "unknown"
    # verify and critique do not lift it either
    assert t.claim_verify(c["id"])["after"]["status"] not in VERIFIED
    assert t.claim_challenge(c["id"])["after"]["status"] not in VERIFIED
    assert Claims(st, repo).get(c["id"])["status"] not in VERIFIED


@pytest.mark.experiment
def test_an_unrelated_passing_test_run_does_not_verify(orders):
    """Audit: "encrypted with AES-256" became experiment_verified 0.95 after an unrelated pricing test run."""
    from verinoda import experiments
    from verinoda.claims import VERIFIED, Claims

    repo, st = orders
    snap = st.latest_snapshot()
    cl = Claims(st, repo)
    c = cl.create("Orders are encrypted with AES-256", project=snap["project"], snapshot=snap,
                  status="experiment_verified", actor="user")
    res = experiments.run(st, repo, [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                                     "tests/test_pricing.py"], hypothesis="pricing ok", claim_id=c["id"])
    assert res["outcome"] == "pass"
    assert [e["relation"] for e in cl.evidence(c["id"])] == ["supports"]  # attached, but it says nothing about AES
    after = cl.reassess(c["id"], reason="ran tests")
    assert after["status"] not in VERIFIED
    from verinoda.claims import ClaimRuleError

    with pytest.raises(ClaimRuleError):
        cl.set_status(c["id"], "experiment_verified", reason="the run passed", downgrade=False)
    # the same run does verify the claim it is about
    ok = cl.create("the pricing tests pass", project=snap["project"], snapshot=snap, status="unknown")
    cl.attach(ok["id"], res["evidence_id"], "supports")
    assert cl.set_status(ok["id"], "experiment_verified", reason="ran", downgrade=False)["status"] == \
        "experiment_verified"


def test_reversed_range_and_blank_lines_cannot_verify_through_the_claims_api(orders):
    from verinoda.claims import VERIFIED, Claims

    repo, st = orders
    snap = st.latest_snapshot()
    empty = {"source_type": "source_code", "locator": "orders/api.py:5-3", "path": "orders/api.py",
             "line_start": 5, "line_end": 3, "content_hash": evmod.content_hash(""), "excerpt": "", "meta": {}}
    c = Claims(st, repo).create("reversed range claim", project=snap["project"], snapshot=snap,
                                status="statically_verified", evidence=[(empty, "supports")])
    assert c["status"] not in VERIFIED


def test_user_claim_text_must_match_the_cited_lines_even_with_a_kind(orders):
    """Audit 2 (2026-09-23): --kind config/location let unrelated text ride on real code lines."""
    import json as _json

    repo, st = orders
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}

    def add(*extra):
        r = subprocess.run([sys.executable, "-m", "verinoda", "claim", "add", *extra, "--repo", str(repo),
                            "--json"], capture_output=True, text=True, env=env, timeout=180, cwd=repo)
        assert r.returncode == 0, r.stderr
        return _json.loads(r.stdout)

    bad = add("Orders are persisted in PostgreSQL", "--status", "statically_verified", "--kind", "config",
              "--symbol", "ORDERS_MAX_ITEMS", "--source", "orders/config.py:6")
    assert bad["status"] not in ("statically_verified", "observed", "experiment_verified",
                                 "primary_source_verified"), bad
    good = add("MAX_ITEMS_PER_ORDER reads ORDERS_MAX_ITEMS", "--status", "statically_verified", "--kind",
               "config", "--symbol", "ORDERS_MAX_ITEMS", "--source", "orders/config.py:6")
    assert good["status"] == "statically_verified", good


def test_verinoda_state_and_git_internals_are_not_evidence(orders):
    repo, _ = orders
    assert evmod.source_evidence(repo, ".verinoda/index/GRAPH_REPORT.md", 1, commit=None) is None
    assert evmod.source_evidence(repo, ".git/HEAD", 1, commit=None) is None
