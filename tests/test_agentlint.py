"""Agent instruction files (AGENTS.md, CLAUDE.md, memory) checked against the tree."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from verinoda import agentlint, cli

PYPROJECT = """[project]
name = "demo"
version = "0.1"
dependencies = ["requests>=2"]

[project.optional-dependencies]
dev = ["pytest>=8"]

[project.scripts]
demo = "demo.cli:main"

[dependency-groups]
lint = ["ruff"]
"""

PACKAGE = """{
  "name": "demo-web",
  "scripts": {
    "test": "vitest run",
    "lint": "eslint ."
  },
  "devDependencies": {
    "eslint": "^9"
  }
}
"""

MAKEFILE = """.PHONY: test build
test:
\tpytest -q
build:
\tpython -m build
"""

AGENTS = """# Agents

Read docs/guide.md first, then `demo/cli.py:2` and `demo/cli.py:40`.
The old notes are in docs/old-notes.md.
See [the guide](docs/guide.md) and [setup](docs/setup.md).
Build output lands in `dist/demo.whl`.
Drafts go to docs/drafts/<id>.md, see https://example.com/docs/x.html and origin/main.

```bash
pip install -e .[dev,docs]
npm run lint
npm run e2e
make test
make deploy
python -m demo.cli
python -m demo.server
python -m json.tool
uv sync --extra dev
```

```
demo/
├── cli.py
```

Run tests with `pytest -q`. Format with `black .` and `npx prettier --check .`
Install `pip install requests` or `pip install leftpad`; run `/verinoda` in Claude Code.
"""

CLAUDE = """@AGENTS.md
@docs/missing.md

Run tests: `npm test`
Use `Demo/cli.py` for the entry point.
"""

GEMINI = """Tests: `python -m pytest`.
"""


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "proj"
    files = {"pyproject.toml": PYPROJECT, "package.json": PACKAGE, "Makefile": MAKEFILE, "pnpm-lock.yaml": "",
             ".gitignore": "dist/\n", "docs/guide.md": "# Guide\n", "demo/__init__.py": "",
             "demo/cli.py": "def main():\n    return 0\n", "AGENTS.md": AGENTS, "CLAUDE.md": CLAUDE,
             "GEMINI.md": GEMINI}
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode("utf-8"))
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    return root


@pytest.fixture()
def home(tmp_path, repo):
    h = tmp_path / "home"
    mem = h / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(repo.resolve())) / "memory"
    mem.mkdir(parents=True)
    (mem / "MEMORY.md").write_text("- [Setup](gone.md)\n- [Notes](notes.md), related: [[notes]]\n", encoding="utf-8")
    (mem / "notes.md").write_text("---\nname: notes\n---\nThe entry point is demo/cli.py.\n", encoding="utf-8")
    return h


def _by(res: dict, at_file: str) -> dict[tuple[str, str], dict]:
    return {(c["kind"], c["name"]): c for c in res["checks"] if c["at"].rsplit(":", 1)[0].endswith(at_file)}


def test_every_line_that_names_something_is_checked(repo, home):
    res = agentlint.lint(repo, home=home, include_ok=True)
    assert res["status"] == "wrong"
    assert [f["path"] for f in res["files"]][:3] == ["AGENTS.md", "CLAUDE.md", "GEMINI.md"]
    a = _by(res, "AGENTS.md")
    verdicts = {k: c["verdict"] for k, c in a.items()}
    # paths: prose, code spans with a line, links; a git-ignored one is unknown, a placeholder or URL no check
    assert verdicts[("path", "docs/guide.md")] == "ok"
    assert verdicts[("path", "demo/cli.py:2")] == "ok"
    assert verdicts[("path", "demo/cli.py:40")] == "wrong" and "has 2 lines" in a[("path", "demo/cli.py:40")]["why"]
    assert verdicts[("path", "docs/old-notes.md")] == "wrong"
    assert verdicts[("link", "docs/guide.md")] == "ok" and verdicts[("link", "docs/setup.md")] == "wrong"
    assert verdicts[("path", "dist/demo.whl")] == "unknown" and "git ignores" in a[("path", "dist/demo.whl")]["why"]
    assert not [k for k in a if "drafts" in k[1] or "example.com" in k[1] or "origin" in k[1] or "verinoda" in k[1]]
    # scripts, targets, modules, extras, packages
    assert verdicts[("script", "npm lint")] == "ok" and a[("script", "npm lint")]["evidence"][1] == "package.json:5"
    assert verdicts[("script", "npm e2e")] == "wrong"
    assert verdicts[("target", "make test")] == "ok" and a[("target", "make test")]["evidence"][1] == "Makefile:2"
    assert verdicts[("target", "make deploy")] == "wrong"
    assert verdicts[("module", "demo.cli")] == "ok" and verdicts[("module", "demo.server")] == "wrong"
    assert verdicts[("module", "json.tool")] == "ok"
    assert verdicts[("extra", "dev")] == "ok" and verdicts[("extra", "docs")] == "wrong"
    assert verdicts[("package", "requests")] == "ok" and verdicts[("package", "leftpad")] == "wrong"
    # tools run by name: the package that provides them is declared (strong_inference: a name-to-package table)
    assert verdicts[("tool", "pytest")] == "ok" and a[("tool", "pytest")]["status"] == "strong_inference"
    assert verdicts[("tool", "black")] == "wrong" and verdicts[("tool", "prettier")] == "wrong"
    # a tree diagram in a plain block is not a command
    assert ("path", "demo/") not in a
    c = _by(res, "CLAUDE.md")
    assert c[("import", "AGENTS.md")]["verdict"] == "ok" and c[("import", "docs/missing.md")]["verdict"] == "wrong"
    assert c[("script", "npm test")]["verdict"] == "ok"
    case = c[("path", "Demo/cli.py")]
    assert case["verdict"] == "wrong" and "demo/cli.py" in case["why"]   # the case differs: wrong on Linux


def test_every_wrong_record_carries_its_line_and_a_status(repo, home):
    res = agentlint.lint(repo, home=home)
    assert res["checks"] and all(c["verdict"] != "ok" for c in res["checks"])
    for c in res["checks"]:
        assert c["status"] in ("statically_verified", "strong_inference", "unknown")
        assert c["evidence"][0] == c["at"] and re.search(r":\d+$", c["at"])
        assert c["text"]
    assert res["ok_not_listed"] == res["summary"]["ok"]
    old = next(c for c in res["checks"] if c["name"] == "docs/old-notes.md")
    assert old["at"] == "AGENTS.md:4" and old["text"] == "The old notes are in docs/old-notes.md."


def test_files_agree_on_test_commands_and_the_package_manager(repo, home):
    res = agentlint.lint(repo, home=home, include_ok=True)
    agree = [c for c in res["checks"] if c["kind"] == "agreement"]
    test_pairs = {c["name"]: c for c in agree if c["name"].startswith("test:")}
    # AGENTS runs make test (whose recipe is pytest) and pytest; GEMINI python -m pytest: they agree
    assert test_pairs["test: AGENTS.md / GEMINI.md"]["verdict"] == "ok"
    # CLAUDE runs npm test (vitest): it disagrees with both
    bad = test_pairs["test: AGENTS.md / CLAUDE.md"]
    assert bad["verdict"] == "wrong" and bad["status"] == "strong_inference"
    assert bad["evidence"][1].startswith("CLAUDE.md:")
    assert test_pairs["test: CLAUDE.md / GEMINI.md"]["verdict"] == "wrong"
    # npm against the pnpm lock file
    mgr = [c for c in agree if c["name"] == "package manager npm"]
    assert mgr and all(c["verdict"] == "wrong" and "pnpm-lock.yaml:1" in c["evidence"] for c in mgr)


def test_memory_files_and_their_links(repo, home):
    res = agentlint.lint(repo, home=home, include_ok=True)
    mem = [f for f in res["files"] if f["kind"] == "memory"]
    assert {Path(f["path"]).name for f in mem} == {"MEMORY.md", "notes.md"}
    assert all(f["path"].startswith("~/.claude/projects/") for f in mem)
    m = _by(res, "MEMORY.md")
    assert m[("link", "gone.md")]["verdict"] == "wrong" and m[("link", "notes.md")]["verdict"] == "ok"
    n = _by(res, "notes.md")
    assert n[("path", "demo/cli.py")]["verdict"] == "ok"   # read from the repository, not the memory folder
    none = agentlint.lint(repo, home=home, memory=False)
    assert not [f for f in none["files"] if f["kind"] == "memory"]


def test_a_moved_file_names_its_new_place(repo, home):
    (repo / "src" / "app").mkdir(parents=True)
    (repo / "src" / "app" / "server.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    (repo / "AGENTS.md").write_text("The server is app/server.py; the CLI `demo/cly.py`.\n", encoding="utf-8")
    res = agentlint.lint(repo, home=home, memory=False)
    a = _by(res, "AGENTS.md")
    assert a[("path", "app/server.py")]["verdict"] == "wrong"
    assert a[("path", "app/server.py")]["nearest"] == ["src/app/server.py"]   # the path it names, one folder down
    assert a[("path", "demo/cly.py")]["nearest"] == ["demo/cli.py"]           # else the nearest file name


def test_what_is_not_a_path():
    tree = agentlint.Tree(Path("."), ["docs/a.md"], Path("."))
    lt = agentlint.Linter(tree)
    for v in ("origin/main", "and/or", "https://x.org/a.md", "docs/<id>.md", "--json", "ns:path/fn", "/verinoda",
              "example.com/a.html", "1/2", "$HOME/x.md", "os.path"):
        assert not lt.path_like(v, "prose"), v
    for v in ("docs/a.md", "docs/a.md:3", "./run.sh", "x/y.py", "src\\main.py"):
        assert lt.path_like(v, "prose"), v
    assert lt.path_like("setup.py", "code") and not lt.path_like("setup.py", "prose")


def test_no_instruction_files(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    res = agentlint.lint(tmp_path, home=tmp_path / "home", files=["a.py"])
    assert res["status"] == "no_files" and res["summary"]["checks"] == 0


def test_cli(repo, home, capsys, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    assert cli.main(["agent-lint", "--repo", str(repo)]) == 3
    out = capsys.readouterr().out
    assert out.startswith("agent-lint: ") and "wrong:" in out and "AGENTS.md:4  path docs/old-notes.md" in out
    assert cli.main(["agent-lint", "--repo", str(repo), "--json", "--no-memory"]) == 3
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "wrong" and not [f for f in data["files"] if f["kind"] == "memory"]
    (repo / "extra.md").write_text("See docs/guide.md.\n", encoding="utf-8")
    for f in ("AGENTS.md", "CLAUDE.md", "GEMINI.md"):
        (repo / f).unlink()
    assert cli.main(["agent-lint", "--repo", str(repo), "--no-memory", "--file", "extra.md"]) == 0
    assert cli.main(["agent-lint", "--repo", str(repo), "--no-memory"]) == 2


def _project(tmp_path, files: dict[str, str]) -> Path:
    root = tmp_path / "p"
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode("utf-8"))
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    return root


WEB = {"package.json": '{"scripts": {"lint": "eslint ."}}\n',
       "web/package.json": '{"scripts": {"test": "vitest", "build": "vite build"}}\n'}


def test_cd_carries_through_a_shell_block(tmp_path):
    root = _project(tmp_path, {**WEB, "AGENTS.md": "# A\n\n```bash\ncd web\nnpm run build\nnpm test\n```\n\n"
                                                    "```bash\nnpm run lint\ncd $APP_DIR\nnpm run build\n```\n"})
    res = agentlint.lint(root, home=tmp_path / "h", memory=False, include_ok=True)
    got = {(c["name"], c["at"]): (c["verdict"], c["evidence"]) for c in res["checks"] if c["kind"] == "script"}
    assert got[("npm build", "AGENTS.md:5")] == ("ok", ["AGENTS.md:5", "web/package.json:1"])
    assert got[("npm test", "AGENTS.md:6")][0] == "ok"
    assert got[("npm lint", "AGENTS.md:10")][0] == "ok"   # a new block starts at the file's folder again
    assert got[("npm build", "AGENTS.md:12")][0] == "unknown"   # after a cd that cannot be followed


def test_package_manager_builtins_and_workspace_flags(tmp_path):
    root = _project(tmp_path, {**WEB, "AGENTS.md": "```bash\npnpm -r test\nnpm --workspaces run build\n"
                                                    "bun test\nbun build ./src/index.ts\npnpm -r nope\n```\n"})
    res = agentlint.lint(root, home=tmp_path / "h", memory=False, include_ok=True)
    v = {c["name"]: c["verdict"] for c in res["checks"] if c["kind"] == "script"}
    assert v == {"pnpm test": "ok", "npm build": "ok", "pnpm nope": "wrong"}   # bun test / build: bun's own


def test_a_double_star_glob_matches_no_folder_too(tmp_path):
    root = _project(tmp_path, {"src/a.ts": "", "AGENTS.md": "Sources: `src/**/*.ts`, `src/*/x.ts`.\n"})
    res = agentlint.lint(root, home=tmp_path / "h", memory=False, include_ok=True)
    v = {c["name"]: c["verdict"] for c in res["checks"]}
    assert v["src/**/*.ts"] == "ok" and v["src/*/x.ts"] == "wrong"


def test_a_nested_file_governs_its_own_folder(tmp_path):
    root = _project(tmp_path, {**WEB, "pyproject.toml": PYPROJECT,
                               "AGENTS.md": "Run the Python tests with `pytest`.\n",
                               "CLAUDE.md": "Tests: `python -m pytest`.\n",
                               "web/AGENTS.md": "Run the tests with `npm test` from this folder.\n"})
    res = agentlint.lint(root, home=tmp_path / "h", memory=False, include_ok=True)
    pairs = {c["name"]: c["verdict"] for c in res["checks"] if c["kind"] == "agreement"}
    assert pairs == {"test: AGENTS.md / CLAUDE.md": "ok"}


def test_a_dependency_is_evidenced_by_its_declaration(tmp_path):
    pp = ('[project]\nname = "d"\n# we used to run black here\n[tool.pytest.ini_options]\naddopts = "-q"\n'
          '[dependency-groups]\ndev = ["pytest>=8", "black"]\n')
    root = _project(tmp_path, {"pyproject.toml": pp, "CLAUDE.md": "Run `pytest` and `black .`\n"})
    res = agentlint.lint(root, home=tmp_path / "h", memory=False, include_ok=True)
    ev = {c["name"]: c["evidence"][1] for c in res["checks"] if c["kind"] == "tool"}
    assert ev == {"pytest": "pyproject.toml:7", "black": "pyproject.toml:7"}


def test_paths_outside_the_repository_are_unknown(tmp_path):
    root = _project(tmp_path, {"docs/exists.md": "", "AGENTS.md": (
        "Configure `~/.config/demo/config.toml`.\n"
        "See [guide](/docs/exists.md) and [gone](/docs/gone.md), `/docs/gone2.md` and `/etc/demo/x.conf`.\n")})
    res = agentlint.lint(root, home=tmp_path / "h", memory=False, include_ok=True)
    v = {(c["kind"], c["name"]): c["verdict"] for c in res["checks"]}
    assert v[("path", "~/.config/demo/config.toml")] == "unknown"
    assert v[("link", "/docs/exists.md")] == "ok" and v[("link", "/docs/gone.md")] == "wrong"
    assert v[("path", "/docs/gone2.md")] == "wrong"          # its first folder is the repository's
    assert v[("path", "/etc/demo/x.conf")] == "unknown"


def test_uv_run_options_with_a_value(tmp_path):
    root = _project(tmp_path, {"pyproject.toml": PYPROJECT, "AGENTS.md": "```bash\nuv run --extra dev ruff check .\n"
                                                                         "uv run --extra=nope pytest\n```\n"})
    res = agentlint.lint(root, home=tmp_path / "h", memory=False, include_ok=True)
    v = {(c["kind"], c["name"]): c["verdict"] for c in res["checks"]}
    assert v[("extra", "dev")] == "ok" and v[("extra", "nope")] == "wrong"
    assert v[("tool", "ruff")] == "ok" and v[("tool", "pytest")] == "ok"


def test_a_just_alias_is_a_recipe(tmp_path):
    root = _project(tmp_path, {"justfile": "alias t := test\n\ntest:\n    pytest\n",
                               "AGENTS.md": "```bash\njust t\njust nope\n```\n"})
    res = agentlint.lint(root, home=tmp_path / "h", memory=False, include_ok=True)
    got = {c["name"]: (c["verdict"], c["evidence"]) for c in res["checks"] if c["kind"] == "target"}
    assert got["just t"] == ("ok", ["AGENTS.md:2", "justfile:1"]) and got["just nope"][0] == "wrong"


def test_a_path_with_a_space(tmp_path):
    root = _project(tmp_path, {"docs/My File.md": "", "AGENTS.md": "# A\n\n[b](<docs/My File.md>)\n`docs/My File.md`\n"
                                                                   "[c](docs/My%20File.md)\n"})
    res = agentlint.lint(root, home=tmp_path / "h", memory=False, include_ok=True)
    assert res["status"] == "ok"
    assert {(c["kind"], c["name"]) for c in res["checks"]} == {("link", "docs/My File.md"),
                                                               ("path", "docs/My File.md")}


def test_many_missing_paths_are_settled_quickly_and_the_list_is_capped(tmp_path):
    import time

    body = "".join(f"see src/pkg/missing{i}.py\n" for i in range(1500))
    root = _project(tmp_path, {"src/pkg/a.py": "", "AGENTS.md": body})
    t0 = time.perf_counter()
    res = agentlint.lint(root, home=tmp_path / "h", memory=False)
    assert time.perf_counter() - t0 < 20
    assert res["summary"]["wrong"] == 1500 and len(res["checks"]) == agentlint.MAX_CHECKS
    assert res["truncated"] and res["checks_not_listed"] == 500
