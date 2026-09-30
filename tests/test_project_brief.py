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
        "build backend setuptools.build_meta (`python -m build`)": ("build", "pyproject.toml:3"),
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
        "pre-commit hooks: ruff, ruff-format": ("conventions", ".pre-commit-config.yaml:4"),
        "agent instructions in AGENTS.md (checked by `verinoda agent-lint`)": ("conventions", "AGENTS.md:1"),
    }
    for text, (section, at) in expected.items():
        assert text in got, (text, sorted(got))
        assert (got[text]["section"], got[text]["evidence"]) == (section, [at]), text
        assert got[text]["status"] == "statically_verified"
    # a name that gives no role is not listed; shell plumbing in CI is not a command
    assert not [t for t in got if "start" in t or "deploy" in t or "echo" in t]
    # every cited line exists in its file
    for r in res["lines"]:
        rel, ln = r["evidence"][0].rsplit(":", 1)
        assert 1 <= int(ln) <= max(1, len((repo / rel).read_text(encoding="utf-8").splitlines())), r
    assert res["status"] == "ok" and "truncated" not in res and res["chars"] == len(res["text"])


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
    assert layout[2][0].startswith("data/: 3 files (.json 3)")   # more files, but nothing says what it is


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
