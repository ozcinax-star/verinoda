"""``verinoda docs check`` (verinoda/docrefs.py): code references in the repository's documents, checked against
the tree; renamed paths and moved lines fixed with --fix, the rest flagged."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from verinoda import cli, docrefs

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")

CODE = "".join(f"line_{i} = {i}\n" for i in range(1, 11))


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false",
                    "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main", *args], cwd=cwd, check=True,
                   capture_output=True, stdin=subprocess.DEVNULL)


def _write(repo: Path, rel: str, text: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8"))


def _commit(repo: Path, msg: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)


GUIDE = """# Guide

The loader is `src/load.py`; its setup is `src/load.py:5-6` and the helper lives in `src/util.py`.
See [the loader](../src/load.py#L5-L6) and the [old module](../src/old_name.py).
Not ours: `orders/api.py`, a template `docs/drafts/<id>.md`, a glob `src/**`, a URL `https://x.org/a.py`.

```sh
cat src/missing_in_a_fence.py   # a fenced example is not a reference
```
"""


@pytest.fixture()
def repo(tmp_path):
    r = tmp_path / "docs ğ check"
    _write(r, "src/load.py", CODE)
    _write(r, "src/util.py", "x = 1\n")
    _write(r, "src/old_name.py", "y = 2\n")
    _write(r, ".gitignore", "build/\n")
    _write(r, "docs/guide.md", GUIDE)
    _git(r, "init", "-q")
    _commit(r, "init")
    return r


def test_a_tree_that_matches_its_docs_is_ok_and_other_projects_paths_are_not_checked(repo):
    res = docrefs.check(repo)
    assert res["exit"] == 0 and res["ok"] == 5 and not res["broken"], res
    assert res["unchecked"] == {"not a path of this repository (its first folder is not here)": 1}


def test_a_missing_path_is_broken_and_an_ignored_one_is_not_checked(repo):
    _write(repo, "docs/more.md", "Uses `src/gone.py` and writes `build/out.bin`.\n")
    res = docrefs.check(repo, ["docs/more.md"])
    (b,) = res["broken"]
    assert b["at"] == "docs/more.md:1" and b["path"] == "src/gone.py" and res["exit"] == 1
    assert res["unchecked"] == {"a path git ignores (a runtime or generated file)": 1}


def test_a_rename_is_found_in_git_and_fixed(repo):
    _git(repo, "mv", "src/old_name.py", "src/new_name.py")
    _commit(repo, "rename")
    res = docrefs.check(repo)
    (rn,) = res["renamed"]
    assert rn["to"] == "src/new_name.py" and rn["fix"] == "../src/new_name.py" and res["exit"] == 1
    assert cli.main(["docs", "check", "--repo", str(repo), "--fix"]) == 0
    text = (repo / "docs/guide.md").read_text(encoding="utf-8")
    assert "[old module](../src/new_name.py)" in text and text.count("\n") == GUIDE.count("\n")


def test_moved_lines_are_renumbered_and_changed_lines_flagged(repo):
    _write(repo, "src/load.py", "import os\nimport sys\n" + CODE)   # lines 5-6 are now 7-8
    res = docrefs.check(repo)
    moved = {m["ref"]: m for m in res["moved"]}
    assert moved["src/load.py:5-6"]["to"] == [7, 8] and moved["src/load.py:5-6"]["fix"] == "src/load.py:7-8"
    assert moved["../src/load.py#L5-L6"]["fix"] == "../src/load.py#L7-L8"
    assert cli.main(["docs", "check", "--repo", str(repo), "--fix", "--json"]) == 0
    text = (repo / "docs/guide.md").read_text(encoding="utf-8")
    assert "`src/load.py:7-8`" in text and "(../src/load.py#L7-L8)" in text
    _commit(repo, "renumbered")
    _write(repo, "src/load.py", "import os\nimport sys\n" + CODE.replace("line_5 = 5", "line_5 = 50"))
    res = docrefs.check(repo)
    assert {c["ref"] for c in res["changed"]} == {"src/load.py:7-8", "../src/load.py#L7-L8"} and res["exit"] == 1
    assert "1 of 2 line(s) gone" in res["changed"][0]["why"] or "edited" in res["changed"][0]["why"]


def test_a_line_past_the_end_is_broken_and_exclude_leaves_documents_out(repo):
    _write(repo, "docs/more.md", "See `src/util.py:3`.\n")
    res = docrefs.check(repo, ["docs"])
    assert [b["why"] for b in res["broken"]] == ["src/util.py has 1 lines; the reference cites 3-3"]
    res = docrefs.check(repo, exclude=["docs/more.md"])
    assert res["exit"] == 0 and res["excluded_docs"] == 1


def test_references_skip_fences_urls_globs_and_placeholders():
    refs = docrefs.references(GUIDE)
    assert [r["raw"] for r in refs] == ["src/load.py", "src/load.py:5-6", "src/util.py", "../src/load.py#L5-L6",
                                        "../src/old_name.py", "orders/api.py"]


def test_cli_text_and_exit_codes(repo, capsys):
    assert cli.main(["docs", "check", "--repo", str(repo)]) == 0
    assert "0 broken" in capsys.readouterr().out
    _write(repo, "docs/more.md", "Uses `src/gone.py`.\n")
    assert cli.main(["docs", "check", "--repo", str(repo), "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["broken"][0]["path"] == "src/gone.py"
    assert cli.main(["docs", "check", "--repo", str(repo)]) == 1
    assert "BROKEN docs/more.md:1 `src/gone.py`" in capsys.readouterr().out
