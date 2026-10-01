"""The project brief (``verinoda brief``): facts read from the files now, each line with file:line, under a budget."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from verinoda import cli, project_brief

PYPROJECT = """[build-system]
requires = ["setuptools>=77"]
build-backend = "setuptools.build_meta"

[project]
name = "demo"
version = "0.1"
requires-python = ">=3.10"

[project.scripts]
demo = "demo.cli:main"

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-q"

[tool.ruff]
line-length = 110

[tool.mypy]
strict = true
"""

PACKAGE = """{
  "name": "demo-web",
  "version": "2.0.0",
  "engines": {
    "node": ">=20"
  },
  "scripts": {
    "test": "vitest run",
    "lint": "eslint .",
    "build": "vite build",
    "start": "vite"
  }
}
"""

MAKEFILE = """.PHONY: test
test:
\tpytest -q
deploy:
\t./deploy.sh
"""

JUSTFILE = """fmt:
    ruff format .
"""

WORKFLOW = """name: ci
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: pip install -e . pytest
      - name: tests
        run: |
          echo start
          python -m pytest tests -q
          npm test
"""

GITLAB = """test:
  script:
    - make test
"""

EDITORCONFIG = """root = true

[*]
indent_style = space
indent_size = 4
end_of_line = lf
"""

PRE_COMMIT = """repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    hooks:
      - id: ruff
      - id: ruff-format
"""

TSCONFIG = """{
  "compilerOptions": {
    "strict": true
  }
}
"""


def _put(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8"))


@pytest.fixture()
def repo(tmp_path):
    r = tmp_path / "proj"
    for rel, text in {"pyproject.toml": PYPROJECT, "package.json": PACKAGE, "Makefile": MAKEFILE,
                      "justfile": JUSTFILE, ".github/workflows/ci.yml": WORKFLOW, ".gitlab-ci.yml": GITLAB,
                      ".editorconfig": EDITORCONFIG, ".gitattributes": "# endings\n* text=auto eol=lf\n",
                      ".pre-commit-config.yaml": PRE_COMMIT, "tsconfig.json": TSCONFIG, "AGENTS.md": "# Agents\n",
                      "demo/__init__.py": "", "demo/cli.py": "def main():\n    pass\n",
                      "tests/test_x.py": "def test_x():\n    pass\n", "data/a.json": "{}\n", "data/b.json": "{}\n",
                      "data/c.json": "{}\n"}.items():
        _put(r, rel, text)
    return r


def _files(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


def _by_text(res: dict) -> dict[str, dict]:
    return {r["text"]: r for r in res["lines"]}


def test_every_fact_is_read_from_a_file_and_cites_its_line(repo):
    res = project_brief.brief(repo, max_chars=20000, files=_files(repo))
    got = _by_text(res)
    expected = {
        "Python project demo, 0.1, Python >=3.10": ("project", "pyproject.toml:6"),
        "console script `demo` -> demo.cli:main": ("project", "pyproject.toml:11"),
        "JavaScript package demo-web, 2.0.0, Node >=20": ("project", "package.json:2"),
        "build backend setuptools.build_meta": ("build", "pyproject.toml:3"),
        "`npm run test`: vitest run": ("test", "package.json:8"),
        "`npm run lint`: eslint .": ("check", "package.json:9"),
        "`npm run build`: vite build": ("build", "package.json:10"),
        "`make test`: pytest -q": ("test", "Makefile:2"),
        "`just fmt`": ("check", "justfile:1"),
        "pytest settings: testpaths = [\"tests\"]; addopts = \"-q\"": ("test", "pyproject.toml:13"),
        "`python -m pytest tests -q`": ("ci", ".github/workflows/ci.yml:12"),
        "`npm test`": ("ci", ".github/workflows/ci.yml:13"),
        "`pip install -e . pytest`": ("ci", ".github/workflows/ci.yml:8"),
        "`make test`": ("ci", ".gitlab-ci.yml:3"),
        ".editorconfig [*]: indent_style=space, indent_size=4, end_of_line=lf": ("conventions", ".editorconfig:3"),
        ".gitattributes `* text=auto eol=lf`": ("conventions", ".gitattributes:2"),
        "ruff line-length 110": ("conventions", "pyproject.toml:18"),
        "mypy strict": ("conventions", "pyproject.toml:21"),
        "TypeScript strict: true": ("conventions", "tsconfig.json:3"),
        "pre-commit hooks: ruff, ruff-format": ("conventions", ".pre-commit-config.yaml:4",
                                                ".pre-commit-config.yaml:5"),
        "agent instructions in AGENTS.md (checked by `verinoda agent-lint`)": ("conventions", "AGENTS.md:1"),
    }
    for text, (section, *at) in expected.items():
        assert text in got, (text, sorted(got))
        assert (got[text]["section"], got[text]["evidence"]) == (section, at), text
        assert got[text]["status"] == "statically_verified"
    # a name that gives no role is not listed; shell plumbing in CI is not a command
    assert not [t for t in got if "start" in t or "deploy" in t or "echo" in t]
    assert "`python -m build`" not in res["text"]   # nothing the cited line does not say
    _assert_cited_lines_exist(repo, res)
    assert res["status"] == "ok" and "truncated" not in res and res["chars"] == len(res["text"])


def _assert_cited_lines_exist(repo: Path, res: dict) -> None:
    """Every ``file:line`` cited is a line of that file; a bare path (a folder, an empty file) exists."""
    for r in res["lines"]:
        for at in r["evidence"]:
            rel, _, ln = at.rpartition(":")
            if ln.isdigit():
                assert 1 <= int(ln) <= len((repo / rel).read_text(encoding="utf-8").splitlines()), r
            else:
                assert (repo / at).exists(), r


def test_ci_lists_test_commands_first_and_not_the_install_of_a_test_tool(repo):
    res = project_brief.brief(repo, max_chars=20000, files=_files(repo))
    ci = [r["text"] for r in res["lines"] if r["section"] == "ci"]
    assert ci[:3] == ["`python -m pytest tests -q`", "`npm test`", "`make test`"]
    assert ci[-1] == "`pip install -e . pytest`"


def test_layout_names_what_a_manifest_says_a_folder_is_first(repo):
    res = project_brief.brief(repo, max_chars=20000, files=_files(repo))
    layout = [(r["text"], r["evidence"][0]) for r in res["lines"] if r["section"] == "layout"]
    assert layout[0] == ("demo/: package of console script `demo`, 2 files (.py 2)", "pyproject.toml:11")
    assert layout[1] == ("tests/: pytest testpaths, 1 files (.py 1)", "pyproject.toml:14")
    # more files, but nothing says what it is; the count comes from the file list, so the folder is its evidence
    assert layout[2] == ("data/: 3 files (.json 3)", "data/")
    assert "- data/: 3 files (.json 3) (data/)" in res["text"]
    assert "(pyproject.toml:11, demo/)" in res["text"]


def test_the_text_stays_under_the_budget_and_counts_what_was_left_out(repo):
    full = project_brief.brief(repo, max_chars=20000, files=_files(repo))
    res = project_brief.brief(repo, max_chars=600, files=_files(repo))
    assert res["chars"] <= 600 and len(res["text"]) == res["chars"]
    assert res["truncated"] and res["omitted"] == full["total_lines"] - len(res["lines"]) > 0
    assert f"{res['omitted']} more line(s) over the 600-character budget" in res["text"]
    # the project lines come first; CI is the first section the budget gives up
    kept = {r["section"] for r in res["lines"]}
    assert "project" in kept
    assert "ci" not in kept or {"build", "test", "check", "layout", "conventions"} <= kept
    # the budget is clamped
    assert project_brief.brief(repo, max_chars=5, files=_files(repo))["budget"] == project_brief.MIN_CHARS


def test_rebuilt_from_the_files_on_every_call(repo):
    before = _by_text(project_brief.brief(repo, max_chars=20000, files=_files(repo)))
    assert "`npm run test`: vitest run" in before
    (repo / "package.json").write_text(PACKAGE.replace("vitest run", "jest"), encoding="utf-8")
    after = _by_text(project_brief.brief(repo, max_chars=20000, files=_files(repo)))
    assert "`npm run test`: jest" in after and "`npm run test`: vitest run" not in after


def test_empty_project(tmp_path):
    res = project_brief.brief(tmp_path, files=[])
    assert res["status"] == "empty" and res["lines"] == []
    assert "nothing to say" in project_brief.render(res)


def test_cli(repo, tmp_path, capsys):
    assert cli.main(["brief", "--repo", str(repo)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("# Project brief") and "(pyproject.toml:6)" in out
    assert cli.main(["brief", "--repo", str(repo), "--json", "--max-chars", "400"]) == 0
    res = json.loads(capsys.readouterr().out)
    assert res["budget"] == 400 and res["chars"] <= 400
    empty = tmp_path / "empty"
    empty.mkdir()
    assert cli.main(["brief", "--repo", str(empty)]) == 2


def test_help_tells_it_apart_from_the_decision_brief():
    parser = cli.build_parser()
    sub = next(a for a in parser._actions if a.__class__.__name__ == "_SubParsersAction")
    assert "decide brief" in sub.choices["brief"].description
    assert "decide" in sub.choices   # the decision brief is unchanged


def _brief_of(tmp_path: Path, files: dict[str, str], **kw) -> dict:
    r = tmp_path / "p"
    r.mkdir(exist_ok=True)
    for rel, text in files.items():
        _put(r, rel, text)
    return project_brief.brief(r, max_chars=kw.pop("max_chars", 20000), files=_files(r), **kw)


def _ci_rows(res: dict) -> list[tuple[str, str]]:
    return [(r["text"], r["evidence"][0]) for r in res["lines"] if r["section"] == "ci"]


def test_ci_commands_past_the_cap_are_counted_not_dropped(tmp_path):
    steps = "".join(f"      - run: tool{i} --go\n" for i in range(15))
    res = _brief_of(tmp_path, {".github/workflows/ci.yml": "jobs:\n  a:\n    steps:\n" + steps})
    assert len(_ci_rows(res)) == project_brief.MAX_CI
    assert res["ci_omitted"] == 5 and res["omitted"] == 5 and res["truncated"] is True
    assert res["total_lines"] == len(res["lines"]) + 5
    assert "(5 more CI command(s) past the first 10)" in res["text"]
    assert any("at most 10" in lim for lim in res["limits"])


def test_gitlab_script_items_at_the_key_column_and_flow_lists(tmp_path):
    gitlab = ("test:\n  script:\n  - pip install -e .\n  - pytest -q\n  after_script:\n  - make lint\n"
              "other:\n  script: [\"pytest -x\", \"echo done\"]\n"
              "long:\n  script:\n    - |\n      ruff check .\n      mypy src\n")
    got = _ci_rows(_brief_of(tmp_path, {".gitlab-ci.yml": gitlab}))
    assert ("`pytest -q`", ".gitlab-ci.yml:4") in got
    assert ("`pip install -e .`", ".gitlab-ci.yml:3") in got and ("`make lint`", ".gitlab-ci.yml:6") in got
    assert ("`pytest -x`", ".gitlab-ci.yml:8") in got and not [t for t, _ in got if "echo" in t or "[" in t]
    assert ("`ruff check .`", ".gitlab-ci.yml:12") in got and ("`mypy src`", ".gitlab-ci.yml:13") in got
    assert "`|`" not in [t for t, _ in got]


def test_continued_and_folded_run_blocks_are_one_command(tmp_path):
    wf = ("jobs:\n  a:\n    steps:\n"
          "      - run: |\n          python -m pytest \\\n            --cov=foo tests\n          npm ci\n"
          "      - run: >\n          npm run build\n          --if-present\n"
          "      - run: ${{ matrix.cmd }}\n")
    got = _ci_rows(_brief_of(tmp_path, {".github/workflows/ci.yml": wf}))
    assert got[0] == ("`python -m pytest --cov=foo tests`", ".github/workflows/ci.yml:5")
    assert ("`npm run build --if-present`", ".github/workflows/ci.yml:9") in got
    assert ("`npm ci`", ".github/workflows/ci.yml:7") in got
    assert len(got) == 3   # no fragment and no expression-only step


def test_package_json_cites_the_top_level_name_and_the_bin_entry(tmp_path):
    pj = ('{\n  "author": {"name": "someone"},\n  "name": "tool",\n  "bin": {\n    "tool": "cli.js"\n  }\n}\n')
    got = _by_text(_brief_of(tmp_path, {"package.json": pj}))
    assert got["JavaScript package tool"]["evidence"] == ["package.json:3"]
    assert got["bin `tool` -> cli.js"]["evidence"] == ["package.json:5"]


def test_toml_headers_with_a_comment_and_array_tables(tmp_path):
    pp = ('[project]  # metadata\nname = "demo"\nversion = "1.0"\n\n[[tool.x]]\nline-length = 1\n\n'
          '[tool.black]\nline-length = 90\n[tool.ruff]   # lint\nline-length = 100\n')
    got = _by_text(_brief_of(tmp_path, {"pyproject.toml": pp}))
    assert got["Python project demo, 1.0"]["evidence"] == ["pyproject.toml:2"]
    assert got["ruff line-length 100"]["evidence"] == ["pyproject.toml:11"]
    assert got["black line-length 90"]["evidence"] == ["pyproject.toml:9"]


@pytest.mark.parametrize("pp", ['[project]\nname="demo"\nscripts = "not-a-table"\n', 'project = "x"\n',
                                'tool = 1\nbuild-system = []\n',
                                '[tool.pytest]\nini_options = "x"\n[tool.mypy]\nstrict = true\n[tool]\nruff = 3\n'])
def test_a_manifest_of_an_unexpected_shape_does_not_crash(tmp_path, pp):
    res = _brief_of(tmp_path, {"pyproject.toml": pp, "pkg/__init__.py": ""})
    assert res["status"] == "ok"


def test_empty_files_are_cited_by_path_not_by_a_missing_line(tmp_path):
    res = _brief_of(tmp_path, {"pkg/__init__.py": "", "empty/.gitkeep": "", "AGENTS.md": ""})
    rows = {r["text"]: r["evidence"] for r in res["lines"]}
    assert rows["pkg/: Python package, 1 files (.py 1)"] == ["pkg/__init__.py", "pkg/"]
    assert rows["empty/: 1 files (.gitkeep 1)"] == ["empty/"]
    assert rows["agent instructions in AGENTS.md (checked by `verinoda agent-lint`)"] == ["AGENTS.md"]
    _assert_cited_lines_exist(tmp_path / "p", res)


def test_pre_commit_cites_every_hook_and_counts_the_rest(tmp_path):
    hooks = "repos:\n  - repo: x\n    hooks:\n" + "".join(f"      - id: h{i}\n" for i in range(10))
    got = _by_text(_brief_of(tmp_path, {".pre-commit-config.yaml": hooks}))
    row = got["pre-commit hooks: h0, h1, h2, h3, h4, h5, h6, h7 (+2 more)"]
    assert row["evidence"] == [f".pre-commit-config.yaml:{i}" for i in range(4, 12)]


def test_tsconfig_with_comments_and_trailing_commas(tmp_path):
    ts = '{\n  // comment\n  "compilerOptions": {\n    /* x, */ "strict": true,\n  },\n}\n'
    got = _by_text(_brief_of(tmp_path, {"tsconfig.json": ts}))
    assert got["TypeScript strict: true"]["evidence"] == ["tsconfig.json:4"]


def test_many_scripts_stay_fast(tmp_path):
    import time
    n = 3000
    pj = '{\n  "name": "x",\n  "scripts": {\n' + ",\n".join(f'    "test:{i}": "t"' for i in range(n)) + "\n  }\n}\n"
    pp = '[project]\nname = "x"\n[project.scripts]\n' + "".join(f'c{i} = "x.m:f"\n' for i in range(n))
    start = time.perf_counter()
    res = _brief_of(tmp_path, {"package.json": pj, "pyproject.toml": pp, "x/__init__.py": "x\n"})
    assert res["truncated"] and time.perf_counter() - start < 8
