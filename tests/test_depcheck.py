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


def _dist(site: Path, name: str, version: str, tops: list[str], requires: list[str] = (),
          record: list[str] = ()) -> None:
    d = site / f"{name}-{version}.dist-info"
    d.mkdir(parents=True)
    meta = ["Metadata-Version: 2.1", f"Name: {name}", f"Version: {version}"]
    meta += [f"Requires-Dist: {r}" for r in requires]
    (d / "METADATA").write_text("\n".join(meta) + "\n\nlong description\n", encoding="utf-8")
    if tops:
        (d / "top_level.txt").write_text("\n".join(tops) + "\n", encoding="utf-8")
    if record:
        rows = [f"{r},sha256=x,1" for r in record] + [f"{d.name}/METADATA,,"]
        (d / "RECORD").write_text("\n".join(rows) + "\n", encoding="utf-8")


def _venv(proj: Path) -> Path:
    (proj / ".venv").mkdir()
    (proj / ".venv" / "pyvenv.cfg").write_text("home = /usr/bin\nversion_info = 3.12.1\n", encoding="utf-8")
    return proj / ".venv" / "Lib" / "site-packages"


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
    # scheduler: in the lock file only; the lock line is cited. A lock line shows it installed, not why
    assert set(trans) == {"scheduler"} and trans["scheduler"]["status"] == "strong_inference"
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


def test_namespace_packages_in_the_environment(tmp_path):
    # google/ is a namespace shared by three distributions: each import goes to the one owning its module
    proj = _write(tmp_path, {
        "pyproject.toml": '[project]\nname = "app"\ndependencies = ["google-cloud-storage", "protobuf"]\n',
        "app/__init__.py": "",
        "app/m.py": "from google.cloud import storage\nfrom google.protobuf import message\n",
    })
    site = _venv(proj)
    _dist(site, "google_api_core", "2.0.0", [], record=["google/api_core/__init__.py"])
    _dist(site, "google_cloud_storage", "2.0.0", [], ["google-api-core"],
          record=["google/cloud/storage/__init__.py", "google/cloud/storage/blob.py"])
    _dist(site, "protobuf", "4.0.0", [], record=["google/protobuf/__init__.py", "google/protobuf/message.py"])
    res = depcheck.check_deps(proj, env="auto")
    assert not res["findings"], res["findings"]
    # an import the environment cannot pin to one distribution (the bare namespace) falls back to the name
    (proj / "app" / "n.py").write_text("import google\n", encoding="utf-8")
    missing = _by(depcheck.check_deps(proj, env="auto"), "missing")
    assert missing["google"]["status"] == "strong_inference"


def test_npm_workspace_sibling_and_nested_lock(tmp_path):
    proj = _write(tmp_path, {
        "package.json": '{"name": "root", "private": true, "workspaces": ["packages/*"]}\n',
        "package-lock.json": '{\n  "packages": {\n    "node_modules/lodash": {},\n'
                             '    "node_modules/a/node_modules/foo": {}\n  }\n}\n',
        "packages/a/package.json": '{"name": "a", "dependencies": {}}\n',
        "packages/b/package.json": '{"name": "b", "dependencies": {"lodash": "^4"}}\n',
        "packages/a/src/index.js": "import lodash from 'lodash';\nconst foo = require('foo');\n",
        "packages/b/src/index.js": "import lodash from 'lodash';\n",
    })
    res = depcheck.check_deps(proj)
    trans, missing = _by(res, "transitive_only"), _by(res, "missing")
    t = trans["lodash"]
    assert t["status"] == "strong_inference"
    assert "packages/b/package.json declares it for its own folder only" in t["claim"]
    assert "packages/b/package.json:1" in [e["locator"] for e in t["evidence"]]
    # foo is only nested under another package in the lock: the root code cannot resolve it
    assert "foo" in missing and "foo" not in trans


def test_spring_boot_starters(tmp_path):
    proj = _write(tmp_path, {
        "build.gradle": "dependencies {\n"
                        "    implementation 'org.springframework.boot:spring-boot-starter-web:3.2.0'\n"
                        "    testImplementation 'org.springframework.boot:spring-boot-starter-test:3.2.0'\n"
                        "}\n",
        "settings.gradle": "rootProject.name = 'app'\n",
        "src/main/java/com/ex/App.java": "package com.ex;\nimport org.springframework.boot.SpringApplication;\n",
        "src/test/java/com/ex/AppTest.java": "package com.ex;\nimport org.junit.jupiter.api.Test;\n",
    })
    res = depcheck.check_deps(proj)
    assert not _by(res, "wrong_group") and not _by(res, "unused"), res["findings"]


def test_runtime_config_module_is_not_dev_code(tmp_path):
    assert not depcheck.is_dev_file("src/config.ts") and not depcheck.is_dev_file("src/app/app.config.ts")
    assert depcheck.is_dev_file("tailwind.config.cjs") and depcheck.is_dev_file(".eslintrc.js")
    proj = _write(tmp_path, {
        "package.json": '{"name": "svc", "dependencies": {"dotenv": "^16", "zod": "^3"}}\n',
        "src/config.ts": "import 'dotenv/config';\nimport { z } from 'zod';\n",
        "src/index.ts": "import { cfg } from './config';\n",
    })
    assert not depcheck.check_deps(proj)["findings"]


def test_plugin_package_does_not_stand_for_its_host(tmp_path):
    proj = _write(tmp_path, {
        "requirements.txt": "flask-sqlalchemy\npsycopg2-binary\n",
        "requirements-dev.txt": "pytest-cov\n",
        "app.py": "import flask\nimport flask_sqlalchemy\nimport psycopg2\n",
        "tests/test_a.py": "import pytest\n",
    })
    res = depcheck.check_deps(proj, env="none")
    assert set(_by(res, "missing")) == {"flask", "pytest"}   # psycopg2 is still psycopg2-binary


def test_not_type_checking_and_parser_limits(tmp_path):
    proj = _write(tmp_path, {
        "requirements.txt": "attrs\n",
        "m.py": "from typing import TYPE_CHECKING\nimport attr\nif not TYPE_CHECKING:\n    import numpy\n"
                "else:\n    import pandas\n",
    })
    (proj / "gen.py").write_text("X = " + "1+" * 100000 + "1\n", encoding="utf-8")
    res = depcheck.check_deps(proj, env="none")
    assert set(_by(res, "missing")) == {"numpy"}   # the runtime branch; pandas is for the checker only
    assert any("gen.py" in lim and "do not parse" in lim for lim in res["limits"])
    assert depcheck.python_imports("x = " + "-" * 100000 + "1", "a.py") is None


def test_js_imports_linear_and_exact():
    import time

    src = ("import a, { b as c } from 'm1';\nexport * from \"m2\";\nimport type { T } from 'm3';\n"
           "import 'm4';\nconst x = require('m5');\nawait import('m6');\nimport\n  d\nfrom 'm7';\n")
    assert depcheck.js_imports(src, "a.ts") == [("m1", 1, False), ("m2", 2, False), ("m3", 3, True),
                                                 ("m4", 4, False), ("m5", 5, False), ("m6", 6, False),
                                                 ("m7", 7, False)]
    for bad in ("import" + " " * 8000, "const s = '" + "import a " * 8000 + "';", "import a\n" * 4000):
        t = time.perf_counter()
        depcheck.js_imports(bad + "\nimport z from 'left-pad';\n", "big.js")
        assert time.perf_counter() - t < 2.0


def test_helper_script_named_like_a_package(tmp_path):
    proj = _write(tmp_path, {
        "pyproject.toml": '[project]\nname = "p"\ndependencies = ["requests"]\n',
        "src/app/__init__.py": "import requests\nimport yaml\n",
        "scripts/requests.py": "print(1)\n",
        "scripts/yaml.py": "print(1)\n",
        "scripts/run.py": "import yaml\n",   # next to scripts/yaml.py: its own module
        # pytest puts tests/ on sys.path for the session: another test folder imports its helper
        "tests/test_a.py": "import helpers\n",
        "tests/helpers.py": "X = 1\n",
        "tests_other/test_b.py": "import helpers\n",
    })
    res = depcheck.check_deps(proj, env="none")
    assert not _by(res, "unused")
    missing = _by(res, "missing")
    assert set(missing) == {"pyyaml"} and missing["pyyaml"]["import_sites"] == 1


def test_oversized_files_are_a_limit(tmp_path, monkeypatch):
    proj = _write(tmp_path, {
        "pyproject.toml": '[project]\nname = "p"\ndependencies = ["requests"]\n',
        "big.py": "import requests\n" + "#" * 500 + "\n",
    })
    monkeypatch.setattr(depcheck, "MAX_FILE_BYTES", 200)
    res = depcheck.check_deps(proj, env="none")
    assert any("over the size cap" in lim and "big.py" in lim for lim in res["limits"])


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
