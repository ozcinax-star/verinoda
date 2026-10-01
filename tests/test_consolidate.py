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


def _claim(repo, st, path, text, start=1, end=2, *, project="p", valid_env=None, subjects=None, grp=None,
           relation="supports", status="statically_verified"):
    snap = st.latest_snapshot()
    ev = evmod.source_evidence(repo, path, start, end, commit=snap["commit_sha"])
    item = (ev, relation, grp) if grp else (ev, relation)
    return Claims(st, repo).create(text, project=project, snapshot=snap, status=status, valid_env=valid_env,
                                   evidence=[item], subjects=subjects or [path])


def _set_config(repo: Path, on: bool) -> None:
    cfg = repo / ".verinoda" / "config.json"
    data = json.loads(cfg.read_text(encoding="utf-8"))
    data["claims"] = {"consolidate_on_update": on}
    cfg.write_text(json.dumps(data), encoding="utf-8")


def test_a_stale_claim_whose_code_came_back_is_restored(proj):
    repo, st = proj
    c = _claim(repo, st, "a.py", "a() returns 1")
    first = st.claim(c["id"])["status"]
    _write(repo, "a.py", "def a():\n    return 5\n")
    workflow.update(st, repo)
    assert st.claim(c["id"])["status"] == "stale"
    res = consolidate.run(st, repo, dry_run=True)
    assert res["rechecked"][0]["would"] == "verify" and st.claim(c["id"])["status"] == "stale"
    res = consolidate.run(st, repo)
    assert res["still_stale"] == 1 and res["restored"] == 0           # the code is still different
    _write(repo, "a.py", "def a():\n    return 1\n")               # put back (the run refreshes the index)
    res = consolidate.run(st, repo)
    assert res["restored"] == 1 and res["changed_status"] == 0 and res["not_run"] is None
    assert st.claim(c["id"])["status"] == first and res["rechecked"][0]["was"] == first


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


def test_the_stale_queue_rotates_across_runs(proj):
    repo, st = proj
    ids = [_claim(repo, st, "a.py", f"a() returns 1 ({k})")["id"] for k in range(3)]
    _write(repo, "a.py", "def a():\n    return 5\n")
    workflow.update(st, repo)
    seen = [consolidate.run(st, repo, limit=1)["rechecked"][0]["id"] for _ in range(4)]
    assert sorted(seen[:3]) == sorted(ids)                          # every one reached, none twice
    assert seen[3] == seen[0]                                       # then the one tried longest ago
    assert all(st.claim(i)["spec"].get("consolidate_tried") for i in ids)


def test_duplicates_are_folded_never_deleted(proj):
    repo, st = proj
    c1 = _claim(repo, st, "b.py", "b() returns 2", grp="g")
    c2 = _claim(repo, st, "b.py", "b() returns 2", 2, 2, grp="g")     # the same statement, other lines
    other = _claim(repo, st, "a.py", "a() returns 1")
    groups = consolidate.duplicate_groups(st)
    assert len(groups) == 1 and set([groups[0]["keep"], *groups[0]["fold"]]) == {c1["id"], c2["id"]}
    kept_before = st.claim(groups[0]["keep"])
    res = consolidate.run(st, repo, merge=True)
    (m,) = res["merged"]
    keep, dup = m["kept"], m["duplicate"]
    assert m["evidence_linked"] == 1
    assert {e["id"] for e in st.claim_evidence(dup)} <= {e["id"] for e in st.claim_evidence(keep)}
    merged = [e for e in st.claim_evidence(keep) if e["link_note"] == f"merged from {dup}"]
    assert [e["grp"] for e in merged] == [f"{dup}:g"]                 # the groups of two claims never merge
    k = st.claim(keep)                                                # a fold never changes the kept status
    assert (k["status"], k["confidence"]) == (kept_before["status"], kept_before["confidence"])
    d = st.claim(dup)
    assert d["spec"]["duplicate_of"] == keep and d["superseded_by"] is None       # kept, marked, not refuted
    assert st.history(dup)[-1]["payload"]["duplicate_of"] == keep
    assert consolidate.duplicate_groups(st) == [] and st.claim(other["id"])["status"] != "stale"


def test_a_fold_leaves_a_stale_kept_claim_stale_and_stale_claims_are_not_grouped(proj):
    repo, st = proj
    stale = _claim(repo, st, "a.py", "x is set", subjects=["x"])
    live = _claim(repo, st, "b.py", "x is set", subjects=["x"])
    assert len(consolidate.duplicate_groups(st)) == 1
    _write(repo, "a.py", "def a():\n    return 5\n")
    workflow.update(st, repo)
    assert st.claim(stale["id"])["status"] == "stale" and st.claim(live["id"])["status"] != "stale"
    assert consolidate.duplicate_groups(st) == []                    # only one live claim left
    assert consolidate.run(st, repo, merge=True, limit=0)["merged"] == []
    consolidate._fold(st, repo, stale["id"], live["id"])             # even folded into, it is not lifted
    assert st.claim(stale["id"])["status"] == "stale"


def test_contradicted_and_supported_are_a_conflict_never_folded(proj):
    repo, st = proj
    good = _claim(repo, st, "b.py", "b() returns 2")
    bad = _claim(repo, st, "b.py", "b() returns 2", 2, 2, relation="refutes", status="contradicted")
    assert st.claim(bad["id"])["status"] == "contradicted"
    res = consolidate.run(st, repo, merge=True)
    assert res["duplicates"] == [] and res["merged"] == []
    (x,) = res["conflicts"]
    assert x["contradicted"] == [bad["id"]] and x["not_contradicted"] == [good["id"]]
    assert "both contradicted and not" in consolidate.render(res)
    assert "duplicate_of" not in st.claim(bad["id"])["spec"]


def test_refuting_evidence_is_not_carried(proj):
    repo, st = proj
    _claim(repo, st, "b.py", "b() returns 2")
    _claim(repo, st, "b.py", "b() returns 2", 2, 2)
    rid = evmod.add(st, evmod.source_evidence(repo, "a.py", 2, 2, commit=st.latest_snapshot()["commit_sha"]))
    (g,) = consolidate.duplicate_groups(st)
    st.link(g["fold"][0], rid, "refutes", note="a counter-example")      # linking alone changes no status
    (m,) = consolidate.run(st, repo, merge=True)["merged"]
    assert m["refutes_not_carried"] == 1
    assert rid not in {e["id"] for e in st.claim_evidence(m["kept"])}


def test_other_project_or_environment_is_not_a_duplicate(proj):
    repo, st = proj
    _claim(repo, st, "b.py", "b() returns 2")
    _claim(repo, st, "b.py", "b() returns 2", 2, 2, project="q")
    _claim(repo, st, "b.py", "b() returns 2", 1, 1, valid_env="py3.12")
    assert consolidate.duplicate_groups(st) == []
    _claim(repo, st, "b.py", "b() returns 2", 1, 1, valid_env="py3.12")
    (g,) = consolidate.duplicate_groups(st)
    assert g["valid_env"] == "py3.12" and g["project"] == "p"


def test_the_later_verification_is_kept(proj):
    repo, st = proj
    old = st.latest_snapshot()["id"]
    earlier, later = sorted([_claim(repo, st, "b.py", "b() returns 2")["id"],
                             _claim(repo, st, "b.py", "b() returns 2", 2, 2)["id"]])   # the id tiebreak: earlier
    _write(repo, "c.py", "def c():\n    return 3\n")
    workflow.update(st, repo)
    new = st.latest_snapshot()["id"]
    assert new != old and st.claim(earlier)["status"] == st.claim(later)["status"]
    st.update_claim(earlier, {"verified_at": old})                   # verified_at names a snapshot
    st.update_claim(later, {"verified_at": new})
    assert consolidate.duplicate_groups(st)[0]["keep"] == later


def test_a_failed_fold_leaves_nothing_behind(proj, monkeypatch):
    repo, st = proj
    _claim(repo, st, "b.py", "b() returns 2")
    _claim(repo, st, "b.py", "b() returns 2", 2, 2)
    (g,) = consolidate.duplicate_groups(st)
    before = [e["id"] for e in st.claim_evidence(g["keep"])]

    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(st, "record_transition", boom)
    res = consolidate.run(st, repo, merge=True)
    assert res["merged"] == [] and res["errors"][0]["id"] == g["fold"][0]
    assert [e["id"] for e in st.claim_evidence(g["keep"])] == before       # the links rolled back too


def test_update_consolidates_with_the_flag_or_the_setting(proj, capsys):
    repo, st = proj
    c = _claim(repo, st, "a.py", "a() returns 1")
    _write(repo, "a.py", "def a():\n    return 5\n")
    workflow.update(st, repo)
    _write(repo, "a.py", "def a():\n    return 1\n")
    capsys.readouterr()
    assert cli.main(["update", str(repo), "--json"]) == 0
    assert "consolidated" not in json.loads(capsys.readouterr().out)
    assert st.claim(c["id"])["status"] == "stale"
    _set_config(repo, True)
    _write(repo, "b.py", "def b():\n    return 3\n")
    assert cli.main(["update", str(repo), "--json"]) == 0
    con = json.loads(capsys.readouterr().out)["consolidated"]
    assert con["restored"] == 1 and con["still_stale"] == 0 and con["errors"] == 0
    assert st.claim(c["id"])["status"] != "stale"


def test_update_with_the_flag(proj, capsys):
    repo, st = proj
    c = _claim(repo, st, "a.py", "a() returns 1")
    _write(repo, "a.py", "def a():\n    return 5\n")
    workflow.update(st, repo)
    _write(repo, "a.py", "def a():\n    return 1\n")
    capsys.readouterr()
    assert cli.main(["update", str(repo), "--consolidate"]) == 0
    assert "consolidate: 1 stale claim(s) restored" in capsys.readouterr().out
    assert st.claim(c["id"])["status"] != "stale"


def test_update_reports_a_consolidation_error_without_failing(proj, capsys, monkeypatch):
    repo, _st = proj

    def boom(*a, **k):
        raise RuntimeError("locked")

    monkeypatch.setattr(consolidate, "run", boom)
    _write(repo, "b.py", "def b():\n    return 3\n")
    capsys.readouterr()
    assert cli.main(["update", str(repo), "--consolidate", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["consolidated"] == {"error": "RuntimeError: locked"}


def test_update_skips_consolidation_after_a_deferred_update(proj, capsys, monkeypatch):
    repo, _st = proj
    real = workflow.update

    def deferred(*a, **k):
        return {**real(*a, **k), "index_mode": "deferred"}

    monkeypatch.setattr(workflow, "update", deferred)
    monkeypatch.setattr(consolidate, "run", lambda *a, **k: pytest.fail("consolidation ran"))
    _write(repo, "b.py", "def b():\n    return 3\n")
    capsys.readouterr()
    assert cli.main(["update", str(repo), "--consolidate", "--json"]) == 0
    assert "skipped" in json.loads(capsys.readouterr().out)["consolidated"]


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
    assert cli.main(["consolidate", "--repo", str(repo), "--budget", "-1"]) == 2
