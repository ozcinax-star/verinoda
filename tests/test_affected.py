"""The workspace packages a change affects (verinoda/affected.py): ``review``'s ``affected`` key, the MCP
``change_review`` response's compact form and ``verinoda affected``."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import json  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import affected as aff  # noqa: E402
from verinoda import cli, workflow  # noqa: E402
from verinoda import review as rv  # noqa: E402
from verinoda.store import open_store  # noqa: E402


def _run(tree: dict[str, str], changed: list[str]) -> dict:
    return aff.affected(sorted(tree), tree.get, changed)


def _rows(res: dict) -> dict[str, dict]:
    return {r["name"]: r for r in res["affected"]}


NPM = {
    "package.json": '{\n  "private": true,\n  "workspaces": ["packages/*", "apps/*"]\n}\n',
    "packages/core/package.json": '{\n  "name": "@acme/core",\n  "scripts": {"build": "tsc", "test": "vitest"}\n}\n',
    "packages/core/src/index.ts": "export const x = 1;\n",
    "packages/ui/package.json": ('{\n  "name": "@acme/ui",\n  "dependencies": {\n    "@acme/core": "workspace:*",\n'
                                 '    "react": "^18"\n  }\n}\n'),
    "apps/web/package.json": '{\n  "name": "web",\n  "dependencies": {\n    "@acme/ui": "^1.0.0"\n  }\n}\n',
    "apps/docs/package.json": '{\n  "name": "docs",\n  "dependencies": {\n    "react": "^18"\n  }\n}\n',
    "README.md": "# acme\n",
}


def test_npm_workspace_changed_package_and_its_dependents_with_the_manifest_lines():
    res = _run(NPM, ["packages/core/src/index.ts", "README.md"])
    rows = _rows(res)
    assert res["packages_total"] == 4 and res["ecosystems"] == {"npm": 4}
    assert set(rows) == {"@acme/core", "@acme/ui", "web"}          # docs depends on react only
    core = rows["@acme/core"]
    assert core["reason"] == "changed" and core["files"] == 1 and core["at"] == "packages/core/package.json:2"
    assert core["targets"] == ["build", "test"] and core["claim"]["status"] == "statically_verified"
    ui = rows["@acme/ui"]
    assert ui["reason"] == "depends" and ui["at"] == "packages/ui/package.json:4" and ui["distance"] == 1
    assert ui["claim"]["status"] == "statically_verified"          # workspace: protocol
    assert ui["claim"]["evidence"][0]["excerpt"] == '"@acme/core": "workspace:*",'
    web = rows["web"]
    assert web["via"] == ["web", "@acme/ui", "@acme/core"] and web["distance"] == 2
    assert web["claim"]["status"] == "strong_inference"            # a version range: maybe the published one
    assert web["claim"]["uncertainties"]
    assert res["outside"] == ["README.md"]
    text = "\n".join(aff.render(res))
    assert "[strong_inference] web (npm, apps/web/): via web -> @acme/ui -> @acme/core" in text
    assert "outside every package (1, not followed): README.md" in text


def test_a_change_in_a_leaf_affects_only_it_and_a_single_package_repo_is_not_a_monorepo():
    res = _run(NPM, ["apps/docs/package.json"])
    assert [r["name"] for r in res["affected"]] == ["docs"]
    single = _run({"package.json": '{"name": "x"}', "src/a.js": ""}, ["src/a.js"])
    assert single["packages_total"] == 0 and "affected" not in single and single["not_checked"]
    assert aff.compact(single) == {"packages_total": 0} and aff.render(single) == []


def test_pnpm_workspace_yaml_and_negated_globs():
    tree = {"pnpm-workspace.yaml": "packages:\n  - 'libs/*'\n  - '!libs/skip'\n",
            "libs/a/package.json": '{"name": "a"}', "libs/b/package.json": '{"name": "b",\n"dependencies": '
                                                                           '{"a": "workspace:^"}}',
            "libs/skip/package.json": '{"name": "skip",\n"dependencies": {"a": "workspace:^"}}'}
    res = _run(tree, ["libs/a/index.js"])
    assert set(_rows(res)) == {"a", "b"} and res["packages_total"] == 2
    assert _rows(res)["b"]["at"] == "libs/b/package.json:2"


def test_cargo_workspace_members_paths_and_renames():
    tree = {"Cargo.toml": '[workspace]\nmembers = [\n  "crates/*",\n]\n\n[workspace.dependencies]\n'
                          'core = { path = "crates/core" }\n',
            "crates/core/Cargo.toml": '[package]\nname = "acme_core"\nversion = "0.1.0"\n',
            "crates/cli/Cargo.toml": ('[package]\nname = "acme-cli"\n\n[dependencies]\n'
                                      'core = { path = "../core", package = "acme-core" }\nserde = "1"\n'),
            "crates/srv/Cargo.toml": '[package]\nname = "srv"\n\n[dev-dependencies.acme-cli]\nversion = "0.1"\n',
            "crates/core/src/lib.rs": ""}
    res = _run(tree, ["crates/core/src/lib.rs"])
    rows = _rows(res)
    assert set(rows) == {"acme_core", "acme-cli", "srv"}
    assert rows["acme-cli"]["at"] == "crates/cli/Cargo.toml:5"
    assert rows["acme-cli"]["claim"]["status"] == "statically_verified"
    assert rows["srv"]["at"] == "crates/srv/Cargo.toml:4" and rows["srv"]["claim"]["status"] == "strong_inference"


def test_gradle_settings_include_and_project_dependencies():
    tree = {"settings.gradle.kts": 'rootProject.name = "acme"\ninclude(":core", ":app")\ninclude(":tools:gen")\n',
            "core/build.gradle.kts": "plugins { java }\n",
            "app/build.gradle.kts": 'dependencies {\n    implementation(project(":core"))\n'
                                    '    // implementation(project(":tools:gen"))\n}\n',
            "tools/gen/build.gradle.kts": "dependencies {\n    implementation(projects.app)\n}\n",
            "core/src/main/java/A.java": "class A {}\n"}
    res = _run(tree, ["core/src/main/java/A.java"])
    rows = _rows(res)
    assert rows[":core"]["at"] == "settings.gradle.kts:2"
    assert rows[":app"]["at"] == "app/build.gradle.kts:2" and rows[":app"]["claim"]["status"] == "statically_verified"
    assert rows[":tools:gen"]["via"] == [":tools:gen", ":app", ":core"]
    assert rows[":tools:gen"]["claim"]["status"] == "strong_inference"   # a type-safe accessor


def test_maven_modules_and_dependencies():
    parent = "<parent><groupId>com.acme</groupId><artifactId>root</artifactId></parent>"
    tree = {"pom.xml": "<project>\n<groupId>com.acme</groupId>\n<artifactId>root</artifactId>\n<modules>\n"
                       "  <module>core</module>\n  <module>web</module>\n</modules>\n</project>\n",
            "core/pom.xml": f"<project>\n{parent}\n<artifactId>core</artifactId>\n</project>\n",
            "web/pom.xml": (f"<project>\n{parent}\n<artifactId>web</artifactId>\n<dependencies>\n<dependency>\n"
                            "  <groupId>com.acme</groupId>\n  <artifactId>core</artifactId>\n</dependency>\n"
                            "</dependencies>\n</project>\n"),
            "core/src/main/java/C.java": ""}
    res = _run(tree, ["core/src/main/java/C.java"])
    rows = _rows(res)
    assert rows["core"]["at"] == "core/pom.xml:3"
    assert rows["web"]["at"] == "web/pom.xml:7" and rows["web"]["claim"]["status"] == "statically_verified"


def test_python_and_go_modules_in_subfolders():
    py = {"pyproject.toml": '[project]\nname = "root"\n',
          "libs/core/pyproject.toml": '[project]\nname = "acme.core"\n',
          "libs/api/pyproject.toml": ('[project]\nname = "acme-api"\ndependencies = [\n  "Acme_Core>=1",\n'
                                      '  "httpx",\n]\n\n[tool.uv.sources]\nacme-core = { workspace = true }\n'),
          "tests/fixture/pyproject.toml": '[project]\nname = "fx"\ndependencies = ["acme-core"]\n',
          "examples/demo/pyproject.toml": '[project]\nname = "demo"\ndependencies = ["acme-core"]\n'}
    res = _run(py, ["libs/core/acme/core.py"])
    rows = _rows(res)
    assert set(rows) == {"acme.core", "acme-api"} and res["packages_total"] == 3   # fixtures, samples left out
    lone = {"pyproject.toml": '[project]\nname = "app"\n', "examples/demo/pyproject.toml": '[project]\nname = "d"\n'}
    assert _run(lone, ["app.py"])["packages_total"] == 0
    assert rows["acme-api"]["at"] == "libs/api/pyproject.toml:4"
    assert rows["acme-api"]["claim"]["status"] == "statically_verified"
    go = {"go.work": "go 1.22\n\nuse (\n\t./svc\n\t./lib\n)\n",
          "lib/go.mod": "module example.com/lib\n\ngo 1.22\n",
          "svc/go.mod": "module example.com/svc\n\nrequire (\n\texample.com/lib v0.0.0\n)\n",
          "lib/x.go": ""}
    rows = _rows(_run(go, ["lib/x.go"]))
    assert rows["example.com/svc"]["at"] == "svc/go.mod:4"
    assert rows["example.com/svc"]["claim"]["status"] == "statically_verified"


def test_compact_form_is_small():
    res = _run(NPM, ["packages/core/src/index.ts", "README.md"])
    c = aff.compact(res)
    assert c["affected_total"] == 3 and c["outside"] == 1
    assert c["affected"][2] == {"name": "web", "reason": "depends", "at": "apps/web/package.json:4",
                                "via": ["web", "@acme/ui", "@acme/core"], "status": "strong_inference"}
    assert len(json.dumps(c)) < 600


# -- through review, MCP and the CLI ------------------------------------------------------------------------
def _git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "Me", "GIT_AUTHOR_EMAIL": "me@example.org",
           "GIT_COMMITTER_NAME": "Me", "GIT_COMMITTER_EMAIL": "me@example.org"}
    return subprocess.run(["git", "-c", "core.autocrlf=false", "-c", "init.defaultBranch=main", "-c",
                           "commit.gpgsign=false", *args], cwd=cwd, env=env, check=True, capture_output=True,
                          text=True, stdin=subprocess.DEVNULL).stdout.strip()


@pytest.fixture()
def mono(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git not available")
    r = tmp_path / "mono ğ"
    for rel, text in {**NPM, ".gitignore": ".verinoda/\n",
                      "packages/core/src/index.ts": "export function x(): number {\n  return 1;\n}\n"}.items():
        (r / rel).parent.mkdir(parents=True, exist_ok=True)
        (r / rel).write_bytes(text.encode("utf-8"))
    _git(r, "init", "-q")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "init")
    workflow.init(r)
    st = open_store(r)
    try:
        workflow.scan(st, r)
    finally:
        st.close()
    (r / "packages/core/src/index.ts").write_bytes(b"export function x(): number {\n  return 2;\n}\n")
    return r


def test_review_names_the_affected_packages_with_checkable_claims(mono):
    st = open_store(mono)
    try:
        res = rv.review(mono, store=st)
    finally:
        st.close()
    rows = _rows(res["affected"])
    assert set(rows) == {"@acme/core", "@acme/ui", "web"}
    assert "Workspace packages affected: 3 of 4 (1 changed, 2 depending on them)." in res["summary"]
    assert "Affected workspace packages (3 of 4" in rv.render_text(res)
    from verinoda import evidence as evmod

    for r in rows.values():   # every cited line is the manifest's line, hashed as the evidence store hashes it
        for e in r["claim"]["evidence"]:
            rel, line = e["locator"].rsplit(":", 1)
            text = (mono / rel).read_text(encoding="utf-8").split("\n")[int(line) - 1].strip()
            assert e["excerpt"] == text and e["content_hash"] == evmod.content_hash(text)


def test_mcp_and_cli_carry_the_affected_packages(mono, capsys):
    from verinoda.mcp.server import AtlasTools

    got = AtlasTools(mono).change_review()["affected"]
    assert got["affected_total"] == 3 and "claim" not in got["affected"][0]
    cli.main(["affected", "--repo", str(mono), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert out["mode"] == "worktree" and out["changed_files"] == 1 and len(out["affected"]) == 3
    assert cli.main(["affected", "--repo", str(mono), "--file", "apps/docs/package.json"]) == 0
    text = capsys.readouterr().out
    assert "[statically_verified] docs (npm, apps/docs/): 1 changed file(s)" in text
    assert cli.main(["affected", "--repo", str(mono), "--file", "x", "--staged"]) == 2
