"""Consolidation (verinoda/consolidate.py): stale claims re-verified against the current tree, duplicates found
and folded without deleting anything, and the update that runs it."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import json  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import cli, consolidate, workflow  # noqa: E402
from verinoda import evidence as evmod  # noqa: E402
from verinoda.claims import Claims  # noqa: E402
from verinoda.store import open_store  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false",
                    "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main", *args], cwd=cwd, check=True,
                   capture_output=True, stdin=subprocess.DEVNULL)


def _write(repo: Path, rel: str, text: str) -> None:
    (repo / rel).write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture()
def proj(tmp_path):
    repo = tmp_path / "con ğ"
    repo.mkdir()
    _write(repo, ".gitignore", ".verinoda/\n")
    _write(repo, "a.py", "def a():\n    return 1\n")
    _write(repo, "b.py", "def b():\n    return 2\n")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "one")
    workflow.init(repo)
    st = open_store(repo)
    workflow.scan(st, repo)
    yield repo, st
    st.close()


def _claim(repo, st, path, text, start=1, end=2):
    snap = st.latest_snapshot()
    ev = evmod.source_evidence(repo, path, start, end, commit=snap["commit_sha"])
    return Claims(st, repo).create(text, project="p", snapshot=snap, status="statically_verified",
                                   evidence=[(ev, "supports")], subjects=[path])


def test_a_stale_claim_whose_code_came_back_is_restored(proj):
    repo, st = proj
    c = _claim(repo, st, "a.py", "a() returns 1")
    first = st.claim(c["id"])["status"]
    _write(repo, "a.py", "def a():\n    return 5\n")
    workflow.update(st, repo)
    assert st.claim(c["id"])["status"] == "stale"
    res = consolidate.run(st, repo, dry_run=True)
    assert res["rechecked"][0]["would"] == "verify" and st.claim(c["id"])["status"] == "stale"
    assert consolidate.run(st, repo)["still_stale"] == 1           # the code is still different
    _write(repo, "a.py", "def a():\n    return 1\n")               # put back
    res = consolidate.run(st, repo)
    assert res["restored"] == 1 and st.claim(c["id"])["status"] == first


def test_limit_and_budget(proj):
    repo, st = proj
    ids = [_claim(repo, st, "a.py", f"a() returns 1 ({k})")["id"] for k in range(3)]
    _write(repo, "a.py", "def a():\n    return 5\n")
    workflow.update(st, repo)
    assert all(st.claim(i)["status"] == "stale" for i in ids)
    res = consolidate.run(st, repo, limit=1)
    assert len(res["rechecked"]) == 1 and res["not_reached"] == 2
    res = consolidate.run(st, repo, budget=0)
    assert res["rechecked"] == [] and res["not_reached"] == 3


def test_duplicates_are_folded_never_deleted(proj):
    repo, st = proj
    c1 = _claim(repo, st, "b.py", "b() returns 2")
    c2 = _claim(repo, st, "b.py", "b() returns 2", 2, 2)               # the same statement, other lines
    other = _claim(repo, st, "a.py", "a() returns 1")
    groups = consolidate.duplicate_groups(st)
    assert len(groups) == 1 and set([groups[0]["keep"], *groups[0]["fold"]]) == {c1["id"], c2["id"]}
    res = consolidate.run(st, repo, merge=True)
    (m,) = res["merged"]
    keep, dup = m["kept"], m["duplicate"]
    assert m["evidence_linked"] == 1
    assert {e["id"] for e in st.claim_evidence(dup)} <= {e["id"] for e in st.claim_evidence(keep)}
    d = st.claim(dup)
    assert d["spec"]["duplicate_of"] == keep and d["superseded_by"] is None       # kept, marked, not refuted
    assert st.history(dup)[-1]["payload"]["duplicate_of"] == keep
    assert consolidate.duplicate_groups(st) == [] and st.claim(other["id"])["status"] != "stale"


def test_update_consolidates_with_the_flag_or_the_setting(proj, capsys):
    repo, st = proj
    c = _claim(repo, st, "a.py", "a() returns 1")
    _write(repo, "a.py", "def a():\n    return 5\n")
    workflow.update(st, repo)
    _write(repo, "a.py", "def a():\n    return 1\n")
    capsys.readouterr()
    assert cli.main(["update", str(repo), "--json"]) == 0
    assert "consolidated" not in json.loads(capsys.readouterr().out)
    cfg = repo / ".verinoda" / "config.json"
    data = json.loads(cfg.read_text(encoding="utf-8"))
    data["claims"] = {"consolidate_on_update": True}
    cfg.write_text(json.dumps(data), encoding="utf-8")
    _write(repo, "b.py", "def b():\n    return 3\n")
    assert cli.main(["update", str(repo), "--json"]) == 0
    con = json.loads(capsys.readouterr().out)["consolidated"]
    assert con["restored"] >= 0 and "still_stale" in con
    st2 = open_store(repo)
    try:
        assert st2.claim(c["id"])["status"] != "stale"
    finally:
        st2.close()


def test_cli(proj, capsys):
    repo, st = proj
    _claim(repo, st, "b.py", "b() returns 2")
    _claim(repo, st, "b.py", "b() returns 2", 2, 2)
    capsys.readouterr()
    assert cli.main(["consolidate", "--repo", str(repo), "--dry-run"]) == 0
    assert "1 group(s) of duplicate claims" in capsys.readouterr().out
    assert cli.main(["consolidate", "--repo", str(repo), "--merge", "--json"]) == 0
    assert len(json.loads(capsys.readouterr().out)["merged"]) == 1
    assert cli.main(["consolidate", "--repo", str(repo), "--limit", "-1"]) == 2
