"""Commit, diff and revision search (verinoda/history.py): when a text appeared or disappeared, with the commit
as evidence; commits by message, author, path, date and diff content; two revisions compared."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import json  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import cli, history  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def _git(cwd: Path, *args: str, author: str = "Bob", date: str | None = None) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": author, "GIT_AUTHOR_EMAIL": f"{author.lower()}@example.org",
           "GIT_COMMITTER_NAME": author, "GIT_COMMITTER_EMAIL": f"{author.lower()}@example.org"}
    if date:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = date
    return subprocess.run(["git", "-c", "core.autocrlf=false", "-c", "init.defaultBranch=main",
                           "-c", "commit.gpgsign=false", *args], cwd=cwd, env=env, check=True,
                          capture_output=True, text=True, stdin=subprocess.DEVNULL).stdout.strip()


def _commit(repo: Path, files: dict[str, str | None], msg: str, *, author: str = "Bob", date: str) -> str:
    for rel, text in files.items():
        p = repo / rel
        if text is None:
            p.unlink()
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(text.encode("utf-8"))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg, author=author, date=date)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture(scope="module")
def repo(tmp_path_factory) -> tuple[Path, list[str]]:
    """Five commits: main() first; Alice adds RETRY_BUDGET to svc.py; it is used in job.py; it is dropped from
    both; a SQL comment line (`-- note`, which a zero-context diff shows as `--- note`) is removed."""
    r = tmp_path_factory.mktemp("history") / "proj"
    r.mkdir()
    _git(r, "init", "-q")
    shas = [
        _commit(r, {"svc.py": "def main():\n    return 1\n", "q.sql": "-- note\nselect 1;\n"}, "Initial import",
                date="2026-01-05T10:00:00+00:00"),
        _commit(r, {"svc.py": "def main():\n    RETRY_BUDGET = 3\n    return RETRY_BUDGET\n"},
                "Add Retry budget to the service", author="Alice", date="2026-02-10T10:00:00+00:00"),
        _commit(r, {"lib/job.py": "from svc import main\n\nLIMIT = 1  # RETRY_BUDGET is the service's\n"},
                "Use the retry budget in the job", date="2026-03-15T10:00:00+00:00"),
        _commit(r, {"svc.py": "def main():\n    return 1\n", "lib/job.py": "from svc import main\n\nLIMIT = 1\n"},
                "Drop the retry budget", date="2026-04-20T10:00:00+00:00"),
        _commit(r, {"q.sql": "select 1;\n"}, "Remove the SQL note", date="2026-05-25T10:00:00+00:00"),
    ]
    return r, shas


def test_a_text_that_came_and_went_is_answered_with_both_commits(repo):
    r, shas = repo
    res = history.text_history(r, "RETRY_BUDGET")
    assert res["status"] == "found" and not res["at_head"]["present"]
    assert [e["commit"] for e in res["events"]] == [shas[1], shas[2], shas[3]]  # oldest first
    appeared, gone = res["claims"]
    assert appeared["status"] == "primary_source_verified" and appeared["kind"] == "history"
    assert shas[1][:10] in appeared["text"] and "svc.py:2" in appeared["text"] and "Alice" in appeared["text"]
    ev = appeared["evidence"][0]
    assert ev["source_type"] == "git_history" and ev["commit_sha"] == shas[1]
    assert ev["locator"] == f"commit {shas[1]} svc.py:2" and ev["excerpt"] == "+RETRY_BUDGET = 3"
    assert ev["meta"]["change"] == "added" and ev["meta"]["date"].startswith("2026-02-10")
    assert gone["status"] == "primary_source_verified" and shas[3][:10] in gone["text"]
    assert "HEAD has no occurrence" in gone["text"]
    assert gone["evidence"][0]["meta"]["change"] == "removed" and gone["evidence"][0]["commit_sha"] == shas[3]
    # a removed line is a line of the parent's file, and the locator names the parent
    assert gone["evidence"][0]["locator"] == f"commit {shas[3]} removed from {shas[3]}^:lib/job.py:3"
    assert "RETRY_BUDGET" in _git(r, "show", f"{shas[3]}^:lib/job.py").split("\n")[2]
    assert res["appeared"]["commit"] == shas[1] and res["disappeared"]["commit"] == shas[3]
    counts = {f["path"]: (f["added"], f["removed"]) for f in res["events"][2]["files"]}
    assert counts == {"svc.py": (0, 2), "lib/job.py": (0, 1)}


def test_a_text_still_at_head_has_no_disappearance(repo):
    r, shas = repo
    res = history.text_history(r, "def main")
    assert [c["text"].split(" in commit ")[0] for c in res["claims"]] == ["`def main` first appeared"]
    assert res["appeared"] == {"commit": shas[0], "at": "svc.py:1"}
    assert res["at_head"]["present"] and res["at_head"]["sites"][0] == {"path": "svc.py", "line": 1,
                                                                   "text": "def main():"}


def test_a_path_narrows_the_history(repo):
    r, shas = repo
    res = history.text_history(r, "RETRY_BUDGET", path="lib")
    assert res["appeared"] == {"commit": shas[2], "at": "lib/job.py:3"}
    assert res["disappeared"]["commit"] == shas[3] and "under lib" in res["claims"][1]["text"]


def test_a_regex_matches_changed_lines(repo):
    r, shas = repo
    res = history.text_history(r, r"RETRY_[A-Z]+ = [0-9]", regex=True)  # git's regex: POSIX, no \d
    assert res["regex"] and res["appeared"]["commit"] == shas[1]
    assert res["disappeared"]["commit"] == shas[3]
    with pytest.raises(ValueError, match="not a regular expression"):
        history.text_history(r, "RETRY_(", regex=True)


def test_a_regex_is_git_s_both_to_select_commits_and_to_pick_lines(repo):
    """A POSIX bracket class selects the commits in git; the lines are matched by git too, so they are found."""
    r, shas = repo
    res = history.text_history(r, "RETRY_[[:upper:]]+ = [[:digit:]]", regex=True)
    assert res["appeared"]["commit"] == shas[1] and res["disappeared"]["commit"] == shas[3]
    # \d is not POSIX: git selects nothing, and the answer does not blame a shallow clone
    res = history.text_history(r, r"RETRY_BUDGET = \d", regex=True)
    assert res["status"] == "not_found" and "shallow" not in res["unknowns"][0]["next_step"]


def test_a_catastrophic_python_pattern_is_matched_by_git(tmp_path):
    """No pattern runs in Python's backtracking engine: one that takes exponential time there returns at once."""
    r = tmp_path / "redos"
    r.mkdir()
    _git(r, "init", "-q")
    sha = _commit(r, {"f.txt": "a" * 34 + "!\n"}, "a line of a's", date="2026-01-05T10:00:00+00:00")
    res = history.text_history(r, "(a|a)+c|a", regex=True)
    assert res["appeared"]["commit"] == sha and res["at_head"]["count"] == 1


def test_a_removed_sql_comment_is_a_line_not_a_file_header(repo):
    r, shas = repo
    res = history.text_history(r, "note")
    assert res["disappeared"] == {"commit": shas[4], "at": "q.sql:1"}
    assert res["claims"][1]["evidence"][0]["excerpt"] == "--- note"


def test_nothing_found_is_an_unknown_with_a_next_step(repo):
    r, _ = repo
    res = history.text_history(r, "never_written_anywhere")
    assert res["status"] == "not_found" and not res["claims"]
    assert res["unknowns"] and "next_step" in res["unknowns"][0]
    with pytest.raises(ValueError):
        history.text_history(r, "  ")
    with pytest.raises(ValueError, match="one line"):
        history.text_history(r, "a\nb")


def test_a_shallow_clone_does_not_prove_the_first_appearance(repo, tmp_path):
    r, shas = repo
    clone = tmp_path / "shallow"
    _git(tmp_path, "clone", "-q", "--depth", "1", r.resolve().as_uri(), str(clone))
    res = history.text_history(clone, "def main")
    assert res["shallow"] and res["claims"][0]["status"] == "strong_inference"
    assert "shallow" in res["claims"][0]["uncertainties"][0]


def test_a_cut_history_does_not_prove_the_first_appearance(repo, monkeypatch):
    r, shas = repo
    monkeypatch.setattr(history, "MAX_EVENTS", 2)  # three commits changed it: the oldest is cut
    res = history.text_history(r, "RETRY_BUDGET")
    assert res["truncated"] and [e["commit"] for e in res["events"]] == [shas[2], shas[3]]
    appeared, gone = res["claims"]
    assert appeared["status"] == "strong_inference" and "cut at 2" in appeared["uncertainties"][0]
    assert res["appeared"]["commit"] == shas[2]
    assert gone["status"] == "primary_source_verified"  # the newest removal is never the one cut


def test_a_text_a_merge_removed_has_an_unknown_disappearance(tmp_path):
    r = tmp_path / "merge"
    r.mkdir()
    _git(r, "init", "-q")
    added = _commit(r, {"a.py": "x = 1\nFLAG_Z = 1\ny = 2\n"}, "add flag", date="2026-01-05T10:00:00+00:00")
    _git(r, "checkout", "-q", "-b", "side")
    _commit(r, {"a.py": "x = 1\nFLAG_Z = 2\ny = 2\n"}, "side flag", date="2026-01-06T10:00:00+00:00")
    _git(r, "checkout", "-q", "main")
    _commit(r, {"a.py": "x = 1\nFLAG_Z = 3\ny = 2\n"}, "main flag", date="2026-01-07T10:00:00+00:00")
    with pytest.raises(subprocess.CalledProcessError):  # a conflict
        _git(r, "merge", "-q", "side")
    (r / "a.py").write_bytes(b"x = 1\ny = 2\n")  # the conflict resolved by deleting the line
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "--no-edit", date="2026-01-08T10:00:00+00:00")
    res = history.text_history(r, "FLAG_Z")
    assert res["status"] == "found" and not res["at_head"]["present"]
    assert res["appeared"]["commit"] == added and "disappeared" not in res
    gone = [u for u in res["unknowns"] if "disappear" in u["question"]]
    assert gone and "merge" in gone[0]["why"] and "--first-parent" in gone[0]["next_step"]


def test_a_text_only_in_binary_files_is_not_said_to_be_unchanged(tmp_path):
    r = tmp_path / "bin"
    r.mkdir()
    _git(r, "init", "-q")
    _commit(r, {"b.bin": "png\x00FLAG_BIN\n"}, "binary", date="2026-01-05T10:00:00+00:00")
    res = history.text_history(r, "FLAG_BIN")
    assert res["status"] == "not_found"
    assert "binary" in res["unknowns"][0]["why"] and "no commit" not in res["unknowns"][0]["why"]


def test_a_project_in_a_folder_of_its_repository_searches_that_folder_only(tmp_path):
    top = tmp_path / "top"
    top.mkdir()
    _git(top, "init", "-q")
    _commit(top, {"other.py": "alpha_token outside\n", "sub proj/a.py": "alpha_token inside\n",
                  "sub proj/lat.txt": "beta_tok\n"}, "both", date="2026-01-05T10:00:00+00:00")
    gone = _commit(top, {"sub proj/a.py": "nothing\n"}, "drop inside", date="2026-01-06T10:00:00+00:00")
    proj = top / "sub proj"
    res = history.text_history(proj, "alpha_token")
    assert not res["at_head"]["present"] and res["appeared"]["at"] == "a.py:1"
    assert res["disappeared"] == {"commit": gone, "at": "a.py:1"}
    res = history.text_history(proj, "beta_tok")
    assert res["appeared"]["at"] == "lat.txt:1" and res["at_head"]["sites"][0]["path"] == "lat.txt"
    assert [c["commit"] for c in history.commits(proj, message="drop")["commits"]] == [gone]
    assert history.commits(proj, message="drop")["commits"][0]["files"] == ["a.py"]
    assert history.commits(tmp_path / "top", message="drop")["commits"][0]["files"] == ["sub proj/a.py"]
    cmp = history.compare(proj, "HEAD~1", "HEAD")
    assert [f["path"] for f in cmp["files"]] == ["a.py"]


def test_head_sites_are_capped_but_counted(tmp_path):
    r = tmp_path / "many"
    r.mkdir()
    _git(r, "init", "-q")
    _commit(r, {f"f{i:02}.txt": "hit\nhit\n" for i in range(12)}, "many", date="2026-01-05T10:00:00+00:00")
    now = history.text_history(r, "hit")["at_head"]
    assert now["count"] == 24 and len(now["sites"]) == history.MAX_SITES and now["truncated"]


def test_commits_by_message_author_path_date_and_diff(repo):
    r, shas = repo
    assert [c["commit"] for c in history.commits(r, message="retry budget")["commits"]] == \
        [shas[3], shas[2], shas[1]]  # newest first, case ignored
    by_alice = history.commits(r, author="alice")["commits"]
    assert [c["commit"] for c in by_alice] == [shas[1]] and by_alice[0]["files"] == ["svc.py"]
    assert [c["commit"] for c in history.commits(r, path="lib")["commits"]] == [shas[3], shas[2]]
    dated = history.commits(r, since="2026-03-01", until="2026-04-30")["commits"]
    assert [c["commit"] for c in dated] == [shas[3], shas[2]]
    diff = history.commits(r, diff=r"retry_budget = 3")
    assert [c["commit"] for c in diff["commits"]] == [shas[3], shas[1]]
    assert diff["commits"][0]["files"] == ["svc.py"]  # the file whose lines matched
    # message and author are extended regexes, as diff is: alternation works in all three
    assert [c["commit"] for c in history.commits(r, message="initial|remove the sql")["commits"]] == \
        [shas[4], shas[0]]
    assert [c["commit"] for c in history.commits(r, author="alice|nobody")["commits"]] == [shas[1]]
    assert by_alice[0]["evidence"]["commit_sha"] == shas[1]
    assert by_alice[0]["evidence"]["source_type"] == "git_history"
    cut = history.commits(r, limit=2)
    assert len(cut["commits"]) == 2 and cut["truncated"]
    assert history.commits(r, message="no such words")["status"] == "not_found"


def test_compare_two_revisions(repo):
    r, shas = repo
    res = history.compare(r, shas[1], "HEAD")
    assert res["status"] == "found" and res["merge_base"] == shas[1]
    assert [c["commit"] for c in res["commits"]] == [shas[4], shas[3], shas[2]]
    assert res["commits_only_in_base"] == 0
    files = {f["path"]: f for f in res["files"]}
    assert set(files) == {"svc.py", "q.sql", "lib/job.py"}
    assert files["svc.py"]["change"] == "modified" and files["svc.py"]["removed"] == 2
    assert files["lib/job.py"]["change"] == "added" and files["lib/job.py"]["added"] == 3
    res = history.compare(r, shas[0], shas[2], path="lib")
    assert [f["path"] for f in res["files"]] == ["lib/job.py"] and res["files"][0]["change"] == "added"
    assert history.compare(r, "HEAD", shas[4])["status"] == "same"
    with pytest.raises(ValueError, match="names no commit"):
        history.compare(r, "no-such-branch", "HEAD")
    with pytest.raises(ValueError, match="is not a revision"):
        history.compare(r, "--output=x", "HEAD")


def test_not_a_git_tree(tmp_path):
    for res in (history.text_history(tmp_path, "x"), history.commits(tmp_path), history.compare(tmp_path, "a", "b")):
        assert res["status"] == "not_git"


def test_a_claim_is_one_the_store_rules_allow(repo):
    """The status a claim states is one :func:`verinoda.claims.check_status` allows for its evidence."""
    from verinoda.claims import check_status

    r, _ = repo
    for c in history.text_history(r, "RETRY_BUDGET")["claims"]:
        evs = [{**e, "relation": "supports", "id": f"evd_{i}"} for i, e in enumerate(c["evidence"])]
        assert check_status(c["status"], evs, claim={"kind": c["kind"], "text": c["text"]}, repo=r) is None, c


def test_cli(repo, capsys):
    r, shas = repo
    assert cli.main(["history", "text", "RETRY_BUDGET", "--repo", str(r), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["disappeared"]["commit"] == shas[3]
    assert cli.main(["history", "text", "RETRY_BUDGET", "--repo", str(r)]) == 0
    text = capsys.readouterr().out
    assert "first appeared in commit " + shas[1][:10] in text and "at HEAD: none" in text
    assert cli.main(["history", "commits", "--author", "alice", "--repo", str(r)]) == 0
    assert "Add Retry budget" in capsys.readouterr().out
    assert cli.main(["history", "compare", shas[0], "--repo", str(r)]) == 0
    assert "lib/job.py" in capsys.readouterr().out
    assert cli.main(["history", "text", "never_written_anywhere", "--repo", str(r)]) == 2
    assert cli.main(["history", "compare", "no-such-branch", "--repo", str(r)]) == 1


def test_mcp_history_search(repo):
    from verinoda.mcp.server import AtlasTools

    r, shas = repo
    (r / ".verinoda").mkdir(exist_ok=True)  # the tools answer in a project Verinoda has set up
    try:
        t = AtlasTools(r)
        res = t.history_search(text="RETRY_BUDGET")
        assert res["appeared"]["commit"] == shas[1] and res["claims"][0]["evidence"][0]["commit_sha"] == shas[1]
        assert [c["commit"] for c in t.history_search(author="alice")["commits"]] == [shas[1]]
        assert t.history_search(base=shas[3])["commits"][0]["commit"] == shas[4]
        assert t.history_search(base="no-such-branch")["error"] == "invalid_argument"
        assert "next_step" in t.history_search(text="never_written_anywhere")
        # a parameter of another mode is an error, not dropped
        for bad in ({"text": "x", "base": "HEAD~1"}, {"text": "x", "message": "fix"}, {"base": "HEAD", "diff": "x"},
                    {"author": "alice", "head": "HEAD"}, {"message": "x", "regex": True}):
            err = t.history_search(**bad)
            assert err["error"] == "invalid_argument", bad
        assert t.history_search(text="RETRY_BUDGET", path="lib")["appeared"]["commit"] == shas[2]
    finally:
        shutil.rmtree(r / ".verinoda")
