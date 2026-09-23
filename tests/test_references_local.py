"""The versions the local project uses (verinoda.references.local): lock files, project venv, runtime pins."""

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import importlib.util  # noqa: E402
import json  # noqa: E402
from pathlib import Path  # noqa: E402

from verinoda.references import local as lv  # noqa: E402

_H = Path(__file__).parent / "fixtures" / "references" / "helpers.py"
_spec = importlib.util.spec_from_file_location("references_helpers", _H)
helpers = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(helpers)
write = helpers.write


def _one(res, key):
    items = res["packages"][key]
    return items[0]


def test_uv_lock_registry_git_and_the_project_itself(tmp_path):
    root = helpers.analysed_project(tmp_path / "app")
    res = lv.local_versions(root)
    req = _one(res, "pkg:pypi/requests")
    assert (req["version"], req["kind"], req["source_file"]) == ("2.31.0", "lock", "uv.lock")
    lines = (root / "uv.lock").read_text(encoding="utf-8").splitlines()
    assert lines[req["line"] - 1] == 'name = "requests"' and req["locator"] == f"uv.lock:{req['line']}"
    assert req["artifact_hashes"] == ["sha256:00"]
    toml = _one(res, "pkg:pypi/toml")
    assert toml["vcs_url"] == "https://github.com/uiri/toml" and toml["vcs_requested"] == "0.10.2"
    assert toml["vcs_commit"] == "3f637dba5f68db63d4b30967fedda51c82459471"
    assert "pkg:pypi/app" not in res["packages"]  # the virtual project is not a dependency
    # installed metadata of the project's own venv is read too (after the lock)
    kinds = [i["kind"] for i in res["packages"]["pkg:pypi/requests"]]
    assert kinds[:2] == ["lock", "installed"] and "manifest" in kinds
    inst = next(i for i in res["packages"]["pkg:pypi/toml"] if i["kind"] == "installed")
    assert inst["vcs_commit"] == "3f637dba5f68db63d4b30967fedda51c82459471"
    assert inst["source_file"].endswith("toml-0.10.2.dist-info/METADATA")
    assert lv.best(res, "pypi", "requests")["kind"] == "lock"
    assert lv.find_by_repo(res, "https://github.com/uiri/toml.git")["name"] == "toml"
    assert lv.find_by_repo(res, "git@github.com:uiri/toml.git")["name"] == "toml"


def test_runtime_pins(tmp_path):
    root = helpers.analysed_project(tmp_path / "app", python="3.11")
    write(root / ".nvmrc", "v20.11.1\n")
    write(root / "package.json", json.dumps({"name": "x", "engines": {"node": ">=18"}}, indent=2))
    write(root / "rust-toolchain.toml", '[toolchain]\nchannel = "1.76.0"\n')
    res = lv.local_versions(root)
    py = res["runtime"]["python"]
    assert py[0] == {**py[0], "version": "3.11", "source_file": ".python-version", "line": 1, "kind": "runtime"}
    assert any(i["kind"] == "installed" and i["version"] == "3.11.1" for i in py)  # pyvenv.cfg
    assert any(i["kind"] == "runtime_constraint" and i["version"] == ">=3.11" for i in py)
    assert res["runtime"]["node"][0]["version"] == "20.11.1"
    assert any(i["version"] == ">=18" for i in res["runtime"]["node"])
    assert res["runtime"]["rust"][0]["version"] == "1.76.0"


def test_other_lock_formats(tmp_path):
    root = tmp_path / "multi"
    write(root / "poetry.lock", '[[package]]\nname = "Django"\nversion = "4.2.11"\n\n[[package]]\nname = "lib"\n'
                                'version = "1.0"\n\n[package.source]\ntype = "git"\nurl = "https://github.com/o/lib"\n'
                                'reference = "main"\nresolved_reference = "' + "b" * 40 + '"\n')
    write(root / "Pipfile.lock", json.dumps({"default": {"attrs": {"version": "==23.2.0", "hashes": []}},
                                             "develop": {"pytest": {"version": "==8.1.1"}}}, indent=4))
    write(root / "requirements.txt", "# pins\nurllib3==2.2.1 --hash=sha256:abc\nidna>=3\n"
                                     "six @ git+https://github.com/benjaminp/six@1.16.0#" + "c" * 40 + "\n-e .\n")
    write(root / "package-lock.json", json.dumps({"lockfileVersion": 3, "packages": {
        "": {"name": "root"}, "node_modules/react": {"version": "18.2.0", "resolved": "https://r/react-18.2.0.tgz",
                                                     "integrity": "sha512-x"},
        "node_modules/a/node_modules/react": {"version": "17.0.0"}}}, indent=2))
    write(root / "Cargo.lock", 'version = 3\n\n[[package]]\nname = "serde"\nversion = "1.0.197"\nsource = '
                               '"registry+https://github.com/rust-lang/crates.io-index"\nchecksum = "abc"\n\n'
                               '[[package]]\nname = "mine"\nversion = "0.1.0"\n\n[[package]]\nname = "gitdep"\n'
                               'version = "0.2.0"\nsource = "git+https://github.com/o/gitdep?rev=v0.2#' + "d" * 40 + '"\n')
    write(root / "go.mod", "module example.com/x\n\ngo 1.22\n\ntoolchain go1.22.1\n\nrequire (\n"
                           "\tgolang.org/x/net v0.20.0\n\tgithub.com/pkg/errors v0.0.0-20240101120000-abcdef123456 // indirect\n)\n")
    write(root / "go.sum", "golang.org/x/net v0.20.0 h1:xyz=\ngolang.org/x/net v0.20.0/go.mod h1:abc=\n")
    write(root / "pylock.toml", 'lock-version = "1.0"\n\n[[packages]]\nname = "attrs"\nversion = "23.2.0"\n\n'
                                '[[packages]]\nname = "tool"\nversion = "0.3"\n[packages.vcs]\ntype = "git"\n'
                                'url = "https://github.com/o/tool"\ncommit-id = "' + "e" * 40 + '"\n')
    write(root / "yarn.lock", "# yarn\n")
    res = lv.local_versions(root)
    p = res["packages"]
    assert _one(res, "pkg:pypi/django")["version"] == "4.2.11"
    assert _one(res, "pkg:pypi/lib")["vcs_commit"] == "b" * 40
    assert _one(res, "pkg:pypi/attrs")["version"] == "23.2.0"
    assert any(i["source_file"] == "pylock.toml" for i in p["pkg:pypi/attrs"])
    assert _one(res, "pkg:pypi/pytest")["scope"] == "develop"
    assert _one(res, "pkg:pypi/urllib3") == {**_one(res, "pkg:pypi/urllib3"), "version": "2.2.1", "line": 2,
                                             "hashed": True}
    assert _one(res, "pkg:pypi/idna")["kind"] == "manifest" and _one(res, "pkg:pypi/idna")["spec"] == ">=3"
    assert _one(res, "pkg:pypi/six")["vcs_commit"] == "c" * 40
    assert [i["version"] for i in p["pkg:npm/react"]] == ["18.2.0"]  # nested copies are not what the project imports
    assert _one(res, "pkg:cargo/serde")["checksum"] == "abc" and "pkg:cargo/mine" not in p
    assert _one(res, "pkg:cargo/gitdep")["vcs_commit"] == "d" * 40
    net = _one(res, "pkg:golang/golang.org/x/net")
    assert net["version"] == "v0.20.0" and net["go_sum"] == "go.sum:1"
    assert _one(res, "pkg:golang/github.com/pkg/errors")["vcs_commit"] == "abcdef123456"
    assert res["runtime"]["go"][0]["version"] == "1.22"
    assert [u["file"] for u in res["unsupported"]] == ["yarn.lock"]
    assert _one(res, "pkg:pypi/tool")["vcs_commit"] == "e" * 40


def test_never_imports_project_code_nor_uses_our_interpreter(tmp_path):
    root = tmp_path / "evil"
    site = root / ".venv" / "Lib" / "site-packages"
    write(root / ".venv" / "pyvenv.cfg", "version = 3.12.0\n")
    marker = tmp_path / "executed.txt"
    boom = f"open({str(marker)!r}, 'w').write('ran')\n"
    write(site / "sitecustomize.py", boom)
    write(site / "evil.pth", "import sitecustomize\n")
    write(site / "evil-1.0.dist-info" / "METADATA", "Metadata-Version: 2.1\nName: evil\nVersion: 1.0\n")
    write(site / "evil" / "__init__.py", boom)
    res = lv.local_versions(root)
    assert _one(res, "pkg:pypi/evil")["version"] == "1.0"
    assert not marker.exists()
    # a project without a venv sees none of Verinoda's own installed packages
    bare = tmp_path / "bare"
    bare.mkdir()
    assert not any(i["kind"] == "installed" for items in lv.local_versions(bare)["packages"].values() for i in items)


def test_local_version_is_source_evidence(tmp_path):
    root = helpers.analysed_project(tmp_path / "app")
    item = lv.best(lv.local_versions(root), "pypi", "requests")
    ev = lv.evidence_for(root, item, commit="abc")
    assert ev["source_type"] == "source_code" and ev["locator"] == item["locator"]
    assert ev["content_hash"].startswith("sha256:") and ev["meta"]["version"] == "2.31.0"


def test_broken_lock_files_are_problems_not_crashes(tmp_path):
    root = tmp_path / "broken"
    write(root / "uv.lock", "this is [not toml")
    write(root / "package-lock.json", "{nope")
    res = lv.local_versions(root)
    assert res["packages"] == {} and any("package-lock.json" in p for p in res["problems"])
