"""Path-scoped review rules (verinoda/path_rules.py): rule files cover their folder, checked rules match only the
lines a change added, nearer files override farther ones, prose rule files are listed."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from verinoda import cli, path_rules

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")

ROOT_RULES = """# Agents

Prefer small functions.

```verinoda-rules
warning no-print: regex print\\( -- use the logger
error no-eval: ast eval($$$A) -- eval runs any string
```
"""

API_RULES = """Rules for the API.

```verinoda-rules
error no-print: regex print\\( -- the API never prints
error no-sql-format: regex execute\\(f" -- parameters, not f-strings
this line is not a rule
```

```python
error fake: regex x
```
"""


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false",
                    "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main", *args], cwd=cwd, check=True,
                   capture_output=True, stdin=subprocess.DEVNULL)


def _write(repo: Path, rel: str, text: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture()
def repo(tmp_path):
    r = tmp_path / "rules ğ repo"
    _write(r, "AGENTS.md", ROOT_RULES)
    _write(r, "src/api/.cursor/BUGBOT.md", API_RULES)
    _write(r, "src/api/h.py", "def h():\n    print('old')\n    return 1\n")
    _write(r, "src/core/c.py", "def c():\n    return 2\n")
    _git(r, "init", "-q")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "one")
    return r


def test_parse_and_scope():
    rules, bad = path_rules.parse(API_RULES, "src/api/.cursor/BUGBOT.md")
    assert [(r["mode"], r["id"], r["kind"], r["pattern"]) for r in rules] == [
        ("error", "no-print", "regex", "print\\("), ("error", "no-sql-format", "regex", 'execute\\(f"')]
    assert rules[1]["message"] == "parameters, not f-strings" and rules[0]["source"] == "src/api/.cursor/BUGBOT.md:4"
    assert [b["at"] for b in bad] == ["src/api/.cursor/BUGBOT.md:6"]
    assert path_rules.rule_files(["AGENTS.md", "src/api/.cursor/BUGBOT.md", "docs/x.md", "a/CLAUDE.md"]) == [
        ("AGENTS.md", ""), ("a/CLAUDE.md", "a"), ("src/api/.cursor/BUGBOT.md", "src/api")]


def test_rules_apply_to_added_lines_in_their_folder_only(repo):
    assert path_rules.check(repo)["changed_files"] == 0
    _write(repo, "src/api/h.py", "def h():\n    print('old')\n    print('new')\n    return 1\n")
    _write(repo, "src/core/c.py", "def c():\n    print('core')\n    return 2\n")
    res = path_rules.check(repo)
    got = [(f["mode"], f["rule"], f["at"]) for f in res["findings"]]
    # the API's nearer rule makes print an error there; elsewhere the root's warning applies; the old print is not
    # an added line
    assert got == [("error", "no-print", "src/api/h.py:3"), ("warning", "no-print", "src/core/c.py:2")]
    assert res["exit"] == 1 and res["findings"][0]["source"] == "src/api/.cursor/BUGBOT.md:4"
    assert res["findings"][0]["status"] == "statically_verified"
    assert {p["file"]: p["read"] for p in res["prose"]}["src/api/h.py"] == ["src/api/.cursor/BUGBOT.md", "AGENTS.md"]
    assert [b["at"] for b in res["malformed"]] == ["src/api/.cursor/BUGBOT.md:6"]


def test_off_turns_a_rule_off_below_and_untracked_files_count(repo):
    _write(repo, "src/core/CLAUDE.md", "```verinoda-rules\noff no-print: regex print\\(\n```\n")
    _write(repo, "src/core/new.py", "print('x')\n")
    _write(repo, "src/other/n.py", "print('y')\n")
    res = path_rules.check(repo)
    assert [(f["rule"], f["at"]) for f in res["findings"]] == [("no-print", "src/other/n.py:1")]
    assert res["exit"] == 0                                   # a warning only


def test_ast_rule_and_staged(repo):
    _write(repo, "src/core/c.py", "def c():\n    return eval(\n        'x'\n    )\n")
    res = path_rules.check(repo)
    assert [(f["rule"], f["at"]) for f in res["findings"]] == [("no-eval", "src/core/c.py:2")] and res["exit"] == 1
    assert path_rules.check(repo, staged=True)["changed_files"] == 0
    _git(repo, "add", "src/core/c.py")
    assert path_rules.check(repo, staged=True)["exit"] == 1
    _write(repo, "src/core/c.py", "def c():\n    return 2\n")       # the working copy differs from the index
    res = path_rules.check(repo, staged=True)
    assert res["exit"] == 3 and "differ from their working copy" in res["incomplete"][0]["why"]


def test_staged_reads_rule_files_from_the_index(repo):
    _write(repo, "src/core/c.py", "def c():\n    print('c')\n    return 2\n")
    _git(repo, "add", "src/core/c.py")
    _write(repo, "AGENTS.md", "no rules any more\n")                 # not staged: the staged rules still apply
    assert [f["rule"] for f in path_rules.check(repo, staged=True)["findings"]] == ["no-print"]


def test_a_bad_regex_is_unknown_and_a_bad_base_an_error(repo):
    _write(repo, "REVIEW.md", "```verinoda-rules\nerror broken: regex (\n```\n")
    assert path_rules.check(repo)["changed_files"] == 0     # a rule block is not a change the rules check
    _write(repo, "src/core/c.py", "def c():\n    return 3\n")
    res = path_rules.check(repo)
    assert res["exit"] == 3 and res["incomplete"][0]["rule"] == "broken"
    with pytest.raises(path_rules.RulesError):
        path_rules.check(repo, base="nope")


def test_cli(repo, capsys):
    r = str(repo)
    assert cli.main(["rules", "--repo", r]) == 0
    _write(repo, "src/api/h.py", "print('x')\n")
    capsys.readouterr()
    assert cli.main(["rules", "--repo", r, "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["findings"][0]["at"] == "src/api/h.py:1"
    assert cli.main(["rules", "--repo", r]) == 1 and "ERROR [no-print] src/api/h.py:1" in capsys.readouterr().out
    assert cli.main(["rules", "--repo", r, "--base", "nope"]) == 2
    assert cli.main(["rules", "--repo", r, "--base", "HEAD", "--staged"]) == 2


def test_review_carries_the_rules(repo):
    from verinoda import review as rv
    from verinoda import workflow
    from verinoda.store import open_store

    _write(repo, ".gitignore", ".verinoda/\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "ignore")
    workflow.init(repo)
    st = open_store(repo)
    try:
        workflow.scan(st, repo)
    finally:
        st.close()
    _write(repo, "src/api/h.py", "def h():\n    print('old')\n    print('new')\n    return 1\n")
    res = rv.review(repo, store=None, record=False, concerns=["config"])
    pr = res["path_rules"]
    assert pr["errors"] == 1 and pr["findings"][0]["at"] == "src/api/h.py:3"
    assert pr["read"] == ["AGENTS.md", "src/api/.cursor/BUGBOT.md"]
    assert "ERROR [no-print] src/api/h.py:3: the API never prints" in rv.render_text(res)
    (repo / "AGENTS.md").unlink()
    (repo / "src/api/.cursor/BUGBOT.md").unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "no rules")
    _write(repo, "src/core/c.py", "def c():\n    return 3\n")
    assert "path_rules" not in rv.review(repo, store=None, record=False, concerns=["config"])


def test_a_new_rule_does_not_match_itself(repo):
    _write(repo, "AGENTS.md", ROOT_RULES.replace("```\n", "error no-todo: regex TODO -- no TODO left\n```\n", 1)
           + "\nA TODO in the prose of the file.\n")
    res = path_rules.check(repo)
    assert [(f["rule"], f["at"]) for f in res["findings"]] == [("no-todo", "AGENTS.md:11")]


def test_names_and_hunks_git_prints_oddly(repo):
    _write(repo, "src/my dir/a b.py", "x = 1\n")
    _write(repo, "src/n.md", "one\ntwo\nthree\n# TODO old\nfive\nsix\nseven\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "more")
    _git(repo, "config", "diff.interHunkContext", "5")
    _write(repo, "AGENTS.md", "```verinoda-rules\nerror no-todo: regex TODO\n```\n")
    _git(repo, "add", "AGENTS.md")
    _git(repo, "commit", "-q", "-m", "rule")
    _write(repo, "src/my dir/a b.py", "x = 1\n# TODO new\n")
    _write(repo, "src/n.md", "new first\none\n++ counter\nthree\n# TODO old\nfive\nsix\nseven\n# TODO late\n")
    got = sorted(f["at"] for f in path_rules.check(repo)["findings"])
    assert got == ["src/my dir/a b.py:2", "src/n.md:9"]


def test_staged_in_a_project_below_the_git_top(tmp_path):
    top = tmp_path / "top"
    proj = top / "proj"
    _write(proj, "x.py", "x = 1\n")
    _git(top, "init", "-q")
    _git(top, "add", "-A")
    _git(top, "commit", "-q", "-m", "one")
    _write(proj, "AGENTS.md", "```verinoda-rules\nerror no-todo: regex TODO\n```\n")
    _write(proj, "x.py", "x = 1\n# TODO\n")
    _git(top, "add", "-A")
    res = path_rules.check(proj, staged=True)
    assert res["exit"] == 1 and [f["at"] for f in res["findings"]] == ["x.py:2"]


def test_same_folder_order_and_no_rule_files(repo):
    _write(repo, "src/api/REVIEW.md", "```verinoda-rules\noff no-print: regex print\\(\n```\n")
    _write(repo, "src/api/h.py", "print('x')\n")
    assert path_rules.check(repo)["findings"] == []          # REVIEW.md comes before .cursor/BUGBOT.md
    for f in ("AGENTS.md", "src/api/.cursor/BUGBOT.md", "src/api/REVIEW.md"):
        (repo / f).unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "no rules")
    res = path_rules.check(repo)
    assert res["rule_files"] == [] and res["exit"] == 0
