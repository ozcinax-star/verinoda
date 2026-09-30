"""Declared against used dependencies (``verinoda check --deps``, MCP ``code_check(deps=true)``): missing,
transitive only, unused and wrong-group packages for pyproject, package.json and Gradle, with their
status and ``file:line`` evidence; the exit codes, the CLI and the MCP wiring.

The projects are written into tmp_path without git (the file list walks the folder). The Python
environment is a hand-written ``.venv`` (pyvenv.cfg plus dist-info folders): nothing is installed or run.
"""

from __future__ import annotations

import json
import os
import textwrap
from pathlib import Path

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import pytest  # noqa: E402

from verinoda import depcheck  # noqa: E402


def _write(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(text), encoding="utf-8")
    return root


def _by(res: dict, finding: str) -> dict[str, dict]:
    return {f["package"]: f for f in res["findings"] if f["finding"] == finding}


PYPROJECT = '''\
[project]
name = "shop"
dependencies = [
    "requests>=2",
    "PyYAML>=6",
    "rich>=13",
    "click>=8",
]

[project.optional-dependencies]
fast = ["orjson>=3"]

[dependency-groups]
dev = ["pytest>=8", "hypothesis>=6"]
'''

PY_FILES = {
    "pyproject.toml": PYPROJECT,
    "shop/__init__.py": "",
    "shop/core.py": '''\
        import os
        import requests
        import yaml
        from shop import helpers
        import hypothesis
        import numpy

        try:
            import ujson
        except ImportError:
            ujson = None
        ''',
    "shop/helpers.py": "import json\n",
    "tests/test_core.py": '''\
        import click
        import pytest
        from shop.core import requests
        ''',
    "examples/demo.py": "import flask\n",
}


@pytest.fixture()
def pyproj(tmp_path):
    return _write(tmp_path, PY_FILES)


def test_python_by_name(pyproj):
    res = depcheck.check_deps(pyproj, env="none")
    missing, unused, wrong = _by(res, "missing"), _by(res, "unused"), _by(res, "wrong_group")
    # numpy: imported by runtime code, declared nowhere; cited at its import line
    assert set(missing) == {"numpy"}
    assert missing["numpy"]["status"] == "strong_inference"   # no environment: tied by name only
    assert missing["numpy"]["evidence"][0] == {"locator": "shop/core.py:6", "role": "import",
                                               "excerpt": "import numpy"}
    # ujson is optional (try/except ImportError); flask is in examples/; os, json and shop are not packages
    assert not {"ujson", "flask", "os", "json", "shop"} & {f["package"] for f in res["findings"]}
    # rich is declared for runtime and imported nowhere; orjson is an extra, not judged
    assert set(unused) == {"rich"} and unused["rich"]["evidence"][0]["locator"] == "pyproject.toml:6"
    # hypothesis: a dev group, imported by runtime code; click: runtime, imported only by tests
    assert set(wrong) == {"hypothesis", "click"}
    assert "dev dependency" in wrong["hypothesis"]["claim"]
    assert wrong["hypothesis"]["evidence"][0]["role"] == "declaration"
    assert wrong["hypothesis"]["evidence"][0]["locator"] == "pyproject.toml:14"
    assert "only test" in wrong["click"]["claim"]
    # yaml is tied to PyYAML by the alias table, requests by name: neither is reported
    assert "pyyaml" not in {f["package"].lower() for f in res["findings"]}
    assert res["exit"] == 3 and res["summary"] == {"missing": 1, "transitive_only": 0, "unused": 1,
                                                   "wrong_group": 2}
    assert res["manifests"] == ["pyproject.toml"]
    assert any("tied to packages by name only" in lim for lim in res["limits"])


def _dist(site: Path, name: str, version: str, tops: list[str], requires: list[str] = ()) -> None:
    d = site / f"{name}-{version}.dist-info"
    d.mkdir(parents=True)
    meta = ["Metadata-Version: 2.1", f"Name: {name}", f"Version: {version}"]
    meta += [f"Requires-Dist: {r}" for r in requires]
    (d / "METADATA").write_text("\n".join(meta) + "\n\nlong description\n", encoding="utf-8")
    (d / "top_level.txt").write_text("\n".join(tops) + "\n", encoding="utf-8")


def test_python_with_environment_finds_transitive(tmp_path):
    proj = _write(tmp_path, {
        "pyproject.toml": '[project]\nname = "app"\ndependencies = ["httpx>=0.27"]\n',
        "app.py": "import httpx\nimport anyio\nimport attr\n",
    })
    (proj / ".venv").mkdir()
    (proj / ".venv" / "pyvenv.cfg").write_text("home = /usr/bin\nversion_info = 3.12.1\n", encoding="utf-8")
    site = proj / ".venv" / "Lib" / "site-packages"
    _dist(site, "httpx", "0.27.0", ["httpx"], ["anyio", "sniffio", "rich ; extra == 'cli'"])
    _dist(site, "anyio", "4.0.0", ["anyio"], ["sniffio"])
    _dist(site, "sniffio", "1.3.0", ["sniffio"])
    _dist(site, "attrs", "23.1.0", ["attr", "attrs"])
    res = depcheck.check_deps(proj, env="auto")
    assert res["ecosystems"]["python"]["declared"] == 1   # httpx is a name, not a URL to skip
    trans, missing = _by(res, "transitive_only"), _by(res, "missing")
    assert set(trans) == {"anyio"}
    t = trans["anyio"]
    assert t["status"] == "statically_verified" and "because httpx requires it" in t["claim"]
    assert [e["role"] for e in t["evidence"]] == ["declaration", "import"]
    assert t["evidence"][1]["locator"] == "app.py:2"
    # attr is tied to the installed attrs by its top_level.txt; installed, but nothing declares or requires it
    assert set(missing) == {"attrs"} and missing["attrs"]["status"] == "statically_verified"
    assert "imported and installed, but declared by no manifest" in missing["attrs"]["claim"]
    assert ".venv (4 installed distributions)" in res["ecosystems"]["python"]["environment"]
    # --env none: the same imports tied by name only, so nothing is statically verified
    plain = depcheck.check_deps(proj, env="none")
    assert {f["status"] for f in plain["findings"]} == {"strong_inference"}
    with pytest.raises(ValueError):
        depcheck.check_deps(proj, env="nowhere")


PACKAGE_JSON = '''\
{
  "name": "web",
  "dependencies": {
    "react": "^18.0.0",
    "lodash": "^4.17.0",
    "jest-dom": "^1.0.0"
  },
  "devDependencies": {
    "vitest": "^1.0.0",
    "axios": "^1.6.0",
    "@types/node": "^20.0.0"
  }
}
'''


def test_npm(tmp_path):
    proj = _write(tmp_path, {
        "package.json": PACKAGE_JSON,
        "package-lock.json": '{\n  "packages": {\n    "node_modules/scheduler": {\n      "version": "0.23.0"\n'
                             '    }\n  }\n}\n',
        "src/app.tsx": '''\
            import React from "react";
            import axios from "axios";
            import { unstable_now } from "scheduler";
            import left from "left-pad";
            import type { Theme } from "styled-types";
            import fs from "node:fs";
            import path from "path";
            import { util } from "./util";
            ''',
        "src/util.ts": "export const util = 1;\n",
        "src/app.test.ts": 'import { describe } from "vitest";\nimport "jest-dom";\n',
    })
    res = depcheck.check_deps(proj)
    missing, trans, unused, wrong = (_by(res, k) for k in depcheck.FINDINGS)
    assert set(missing) == {"left-pad"} and missing["left-pad"]["evidence"][0]["locator"] == "src/app.tsx:4"
    # scheduler: in the lock file only; the lock line is cited
    assert set(trans) == {"scheduler"} and trans["scheduler"]["status"] == "statically_verified"
    assert {"locator": "package-lock.json:3", "role": "lock", "excerpt": '"node_modules/scheduler": {'} \
        in trans["scheduler"]["evidence"]
    assert set(unused) == {"lodash"}
    # axios: a devDependency imported by the app; jest-dom: a dependency only the test file imports
    assert set(wrong) == {"axios", "jest-dom"}
    assert wrong["axios"]["evidence"][0]["locator"] == "package.json:10"
    # the type-only import, node builtins, relative files and @types packages are never reported
    assert not {"styled-types", "fs", "path", "node:fs", "@types/node", "vitest"} & {f["package"] for f in
                                                                                     res["findings"]}
    assert res["ecosystems"]["npm"]["declared"] == 6


BUILD_GRADLE = '''\
plugins {
    id 'java'
}

dependencies {
    implementation 'com.google.guava:guava:33.0.0-jre'
    implementation 'org.yaml:snakeyaml:2.2'
    testImplementation 'junit:junit:4.13.2'
    runtimeOnly 'org.postgresql:postgresql:42.7.0'
}
'''


def test_gradle(tmp_path):
    proj = _write(tmp_path, {
        "build.gradle": BUILD_GRADLE,
        "settings.gradle": "rootProject.name = 'app'\n",
        "src/main/java/com/acme/App.java": '''\
            package com.acme;

            import java.util.List;
            import com.google.common.collect.ImmutableList;
            import org.junit.Assert;
            import com.acme.util.Strings;
            import org.apache.commons.lang3.StringUtils;
            ''',
        "src/test/java/com/acme/AppTest.java": "package com.acme;\n\nimport org.junit.Test;\n",
    })
    res = depcheck.check_deps(proj)
    unused, wrong = _by(res, "unused"), _by(res, "wrong_group")
    # snakeyaml: compiled against and imported nowhere; postgresql is runtimeOnly: no import is expected
    assert set(unused) == {"org.yaml:snakeyaml"}
    assert unused["org.yaml:snakeyaml"]["evidence"][0]["locator"] == "build.gradle:7"
    assert set(wrong) == {"junit:junit"}
    assert wrong["junit:junit"]["evidence"][1]["locator"] == "src/main/java/com/acme/App.java:5"
    # guava is tied by the table of well-known packages; JVM imports are never missing: the unmatched is a limit
    assert not _by(res, "missing") and not _by(res, "transitive_only")
    assert any("org.apache.commons" in lim for lim in res["limits"] if lim.startswith("jvm:"))
    assert res["ecosystems"]["gradle_maven"]["declared"] == 4 - 1


def test_nothing_to_check_and_clean(tmp_path):
    empty = _write(tmp_path / "empty", {"main.py": "import os\n"})
    res = depcheck.check_deps(empty)
    assert res["exit"] == 4 and "nothing was checked" in res["exit_because"] and not res["findings"]
    clean = _write(tmp_path / "clean", {
        "pyproject.toml": '[project]\nname = "c"\ndependencies = ["requests"]\n',
        "c.py": "import requests\n",
    })
    res = depcheck.check_deps(clean, env="none")
    assert res["exit"] == 0 and not res["findings"] and "exit_because" not in res


def test_dev_files_and_dynamic_imports(tmp_path):
    assert depcheck.is_dev_file("tests/test_x.py") and depcheck.is_dev_file("docs/conf.py")
    assert depcheck.is_dev_file("web/vite.config.ts") and depcheck.is_dev_file("setup.py")
    assert not depcheck.is_dev_file("pkg/config.py") and not depcheck.is_dev_file("src/app.ts")
    proj = _write(tmp_path, {
        "pyproject.toml": '[project]\nname = "p"\ndependencies = ["tree-sitter-lua", "wheel"]\n',
        "p.py": 'import importlib\nmod = importlib.import_module("tree_sitter_lua")\n',
        "typing_only.py": "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import pandas\n",
    })
    res = depcheck.check_deps(proj, env="none")
    # named in a string (a dynamic import): a limit, not unused; wheel is a build tool; pandas is TYPE_CHECKING only
    assert not res["findings"]
    assert any("tree-sitter-lua" in lim for lim in res["limits"])


# -- CLI and MCP ------------------------------------------------------------------------------------------------

def test_cli(pyproj, capsys):
    from verinoda import cli

    rc = cli.main(["check", "--deps", "--repo", str(pyproj), "--env", "none"])
    out = capsys.readouterr().out
    assert rc == 3
    assert "1 missing, 0 transitive only, 1 unused, 2 in the wrong group" in out
    assert "MISSING          numpy  (python, strong_inference)" in out
    assert "    import      shop/core.py:6  import numpy" in out
    rc = cli.main(["check", "--deps", "--repo", str(pyproj), "--env", "none", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert rc == 3 and data["kind"] == "deps" and data["summary"]["wrong_group"] == 2
    with pytest.raises(SystemExit):
        cli.main(["check", "--deps", "shop/core.py", "--repo", str(pyproj)])
    with pytest.raises(SystemExit):
        cli.main(["check", "--deps", "--repo", str(pyproj), "--env", "nowhere"])


def test_mcp_code_check_deps(pyproj):
    from verinoda.mcp.server import AtlasTools

    (pyproj / ".verinoda").mkdir(exist_ok=True)
    t = AtlasTools(pyproj)
    res = t.code_check(deps=True, env="none")
    core = depcheck.check_deps(pyproj, env="none")
    assert res["summary"] == core["summary"] and res["exit"] == 3
    assert {f["package"] for f in res["findings"]} == {f["package"] for f in core["findings"]}
    assert t.code_check(deps=True, paths=["shop/core.py"])["error"] == "invalid_argument"


def test_mcp_core_profile_offers_deps(pyproj):
    anyio = pytest.importorskip("anyio")
    from verinoda.mcp.server import build_server

    (pyproj / ".verinoda").mkdir(exist_ok=True)
    tools = {t.name: t for t in anyio.run(build_server(pyproj).list_tools)}
    assert len(tools) == 5   # a parameter of the core code_check, not a new tool
    schema = getattr(tools["code_check"], "input_schema", None) or tools["code_check"].inputSchema
    assert "deps" in schema["properties"] and "env" not in schema["properties"]
