"""Claims as of a moment or a commit (verinoda/asof.py): recorded time read back from the append-only history,
code time from the snapshots the transitions were made at."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import json  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import asof, cli, workflow  # noqa: E402
from verinoda import evidence as evmod  # noqa: E402
from verinoda.claims import Claims  # noqa: E402
from verinoda.store import open_store  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false",
                           "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main", *args], cwd=cwd, check=True,
                          capture_output=True, text=True, stdin=subprocess.DEVNULL).stdout.strip()


def _commit(repo: Path, files: dict[str, str], msg: str) -> str:
    for rel, text in files.items():
        (repo / rel).write_text(text, encoding="utf-8", newline="\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture()
def story(tmp_path):
    """c0 (before the claim), c1 (claim made), c2 (an unrelated file changed: nothing recorded), c3 (its file
    changed: stale)."""
    repo = tmp_path / "as of ğ"
    repo.mkdir()
    _git(repo, "init", "-q")
    c0 = _commit(repo, {".gitignore": ".verinoda/\n", "b.py": "def b():\n    return 0\n"}, "zero")
    c1 = _commit(repo, {"a.py": "def a():\n    return 1\n"}, "one")
    workflow.init(repo)
    st = open_store(repo)
    workflow.scan(st, repo)
    snap = st.latest_snapshot()
    ev = evmod.source_evidence(repo, "a.py", 1, 2, commit=snap["commit_sha"])
    c = Claims(st, repo).create("a() returns 1", project="p", snapshot=snap, status="statically_verified",
                                evidence=[(ev, "supports")], subjects=["a.py"])
    c2 = _commit(repo, {"b.py": "def b():\n    return 2\n"}, "two")
    workflow.update(st, repo)
    c3 = _commit(repo, {"a.py": "def a():\n    return 3\n"}, "three")
    workflow.update(st, repo)
    yield repo, st, c["id"], (c0, c1, c2, c3)
    st.close()


def test_code_time(story):
    repo, st, cid, (c0, c1, c2, c3) = story
    assert st.claim(cid)["status"] == "stale"

    def one(rev):
        (r,) = [x for x in asof.at_commit(st, repo, rev)["claims"] if x["id"] == cid]
        return r["status_then"], r["verdict"], r["held"]

    first = st.history(cid)[1]["to_status"]          # the initial assessment
    assert first in asof.HOLDING
    assert one(c1) == (first, "observed", True)
    assert one(c2) == (first, "strong_inference", True)              # nothing recorded at c2: carried over from c1
    assert one(c3) == ("stale", "observed", False)                  # the update at c3 saw its file change
    assert one(c0) == (None, "unknown", False)                         # before the claim was made
    _git(repo, "checkout", "-q", "-b", "side", c2)
    side = _commit(repo, {"c.py": "x = 1\n"}, "side")
    assert one(side) == (first, "strong_inference", True)           # c3 is not in its history
    with pytest.raises(asof.AsOfError):
        asof.at_commit(st, repo, "no-such-rev")


def test_recorded_time(story):
    repo, st, cid, _ = story
    hist = st.history(cid)
    made = hist[-2]["created_at"]                     # the last transition before it went stale
    then = asof.at_time(st, made)
    (r,) = [x for x in then["claims"] if x["id"] == cid]
    assert r["status_then"] == hist[-2]["to_status"] != "stale" and r["status_now"] == "stale"
    assert then["status"] == "observed"
    assert [x for x in asof.at_time(st, "2000-01-01")["claims"] if x["id"] == cid] == []
    assert any(x["id"] == cid for x in asof.at_time(st, "2999-12-31", status="stale")["claims"])
    with pytest.raises(asof.AsOfError):
        asof.at_time(st, "last tuesday")


def test_every_transition_records_its_snapshot(story):
    repo, st, cid, _ = story
    events = asof.events(st, st.claim(cid))
    assert all(e["snapshot"] and e["recorded"] for e in events)
    # an older history without the snapshot in the payload is rebuilt from the rebind and stale records
    old = [dict(h, payload={k: v for k, v in h["payload"].items() if k != "snapshot"}) for h in st.history(cid)]

    class Old:
        def history(self, _):
            return old

    rebuilt = asof.events(Old(), st.claim(cid))
    assert [e["snapshot"] for e in rebuilt] == [e["snapshot"] for e in events]
    assert not any(e["recorded"] for e in rebuilt)


def test_cli(story, capsys):
    repo, st, cid, (c0, c1, c2, c3) = story
    r = str(repo)
    assert cli.main(["claim", "asof", "--commit", c2, "--repo", r, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["counts"] == {st.history(cid)[1]["to_status"]: 1}
    assert cli.main(["claim", "asof", "--time", "2999-01-01", "--repo", r]) == 0
    assert "stale 1" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(["claim", "asof", "--repo", r])
    with pytest.raises(SystemExit):
        cli.main(["claim", "asof", "--commit", "nope", "--repo", r])


def test_an_invalidation_in_the_working_tree_is_at_no_commit(story):
    repo, st, cid, (c0, c1, c2, c3) = story
    e = asof.events(st, st.claim(cid))[-1]
    assert e["status"] == "stale" and st.snapshot(e["snapshot"])["commit_sha"] == c3
    st.record_transition(cid, from_status="stale", to_status="stale", from_conf=0.1, to_conf=0.1, reason="wt",
                         actor="t", payload={"new_snapshot": None})
    assert asof.events(st, st.claim(cid))[-1]["snapshot"] is None


def test_an_older_commit_checked_later_does_not_hide_the_record_at_rev(story):
    """After `git checkout c0` and an update, the claim goes stale at c0: c1 still answers from c1's own record."""
    repo, st, cid, (c0, c1, c2, c3) = story
    _git(repo, "checkout", "-q", c1)
    workflow.update(st, repo)
    (r,) = [x for x in asof.at_commit(st, repo, c1)["claims"] if x["id"] == cid]
    assert (r["status_then"], r["verdict"]) == (st.history(cid)[1]["to_status"], "observed")


def test_a_transition_on_the_working_tree_is_at_no_commit(story):
    repo, st, cid, (c0, c1, c2, c3) = story
    first = st.history(cid)[1]["to_status"]
    cl = Claims(st, repo)
    cl.attach(cid, evmod.source_evidence(repo, "a.py", 2, 2, commit=None), "refutes")
    cl.set_status(cid, "contradicted", reason="a counterexample on the working tree", actor="t", downgrade=False)
    e = asof.events(st, st.claim(cid))[-1]
    assert e["status"] == "contradicted" and e["snapshot"] is None
    (r,) = [x for x in asof.at_commit(st, repo, c1)["claims"] if x["id"] == cid]
    assert (r["status_then"], r["verdict"]) == (first, "observed")


def test_a_dirty_snapshot_is_at_no_commit(tmp_path):
    repo = tmp_path / "dirty"
    repo.mkdir()
    _git(repo, "init", "-q")
    c1 = _commit(repo, {".gitignore": ".verinoda/\n", "a.py": "def a():\n    return 1\n"}, "one")
    (repo / "a.py").write_text("def a():\n    return 99\n", encoding="utf-8", newline="\n")
    workflow.init(repo)
    st = open_store(repo)
    try:
        workflow.scan(st, repo)
        snap = st.latest_snapshot()
        assert snap["dirty"]
        ev = evmod.source_evidence(repo, "a.py", 1, 2, commit=snap["commit_sha"])
        c = Claims(st, repo).create("a() returns 99", project="p", snapshot=snap, status="statically_verified",
                                    evidence=[(ev, "supports")], subjects=["a.py"])
        (r,) = [x for x in asof.at_commit(st, repo, c1)["claims"] if x["id"] == c["id"]]
        assert r["verdict"] == "unknown" and r["status_then"] is None
        res = asof.at_commit(st, repo, c1)
        assert res["counts"].get("no_record") == 1 and asof.at_commit(st, repo, c1, status="unknown")["claims"] == []
    finally:
        st.close()


def test_weak_inference_is_not_held_and_times_parse():
    assert "weak_inference" not in asof.HOLDING
    assert asof._parse_time(" 2026-05-01 ").isoformat() == "2026-05-01T23:59:59+00:00"
    assert asof._parse_time("2026-05-01T12:00:00Z").isoformat() == "2026-05-01T12:00:00+00:00"


def test_older_rows_rebuilt_with_a_verify_rebind():
    class St:
        def history(self, _):
            return [
                {"seq": 1, "to_status": "unknown", "reason": "created", "payload": {}, "created_at": "t1"},
                {"seq": 2, "to_status": "strong_inference", "reason": "initial assessment", "payload": {},
                 "created_at": "t2"},
                {"seq": 3, "to_status": "stale", "reason": "x", "payload": {"new_snapshot": "s2"}, "created_at": "t3"},
                {"seq": 4, "to_status": "strong_inference", "reason": "verify: evidence re-checked", "payload": {},
                 "created_at": "t4"},
                {"seq": 5, "to_status": "strong_inference", "reason": "verify: rebound to current snapshot",
                 "payload": {"snapshot_id": "s3", "commit_sha": "c3"}, "created_at": "t5"},
            ]

    ev = asof.events(St(), {"id": "c", "snapshot_id": "s1"})
    assert [(e["snapshot"], e["sure"]) for e in ev] == [("s1", True), ("s1", True), ("s2", True), (None, False),
                                                         ("s3", True)]
