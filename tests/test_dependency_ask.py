"""Ask before writing a dependency (verinoda/dependency_ask.py): a proposed dependency judged against the accepted
decision guards - forbidden / restricted / allowed / unknown, each rule that applies cited at its record line."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import json  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import cli, workflow  # noqa: E402
from verinoda import decisions as dm  # noqa: E402
from verinoda import dependency_ask as da  # noqa: E402
from verinoda.store import open_store  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")

FILES = {
    "app/__init__.py": "",
    "app/ui/views.py": "def show():\n    return 1\n",
    "app/core/service.py": "def place(x):\n    return x\n",
    "app/db/store.py": "def save(x):\n    return x\n",
    "orders/api.py": "def submit(x):\n    return x\n",
    "orders/_impl.py": "def run(x):\n    return x\n",
    "old/thing.py": "X = 1\n",
    "verinoda.toml": "[architecture.tags]\nlegacy = [\"old/**\"]\n",
}
GUARDS = ["layers order=app/ui/**,app/core/**,app/db/**",
          "public module=orders/** api=orders/api.py",
          "no_edge from=app/** to=tag:legacy",
          "only_in calls=sqlite3.connect allowed=app/db/store.py",
          "dependency absent=psycopg",
          "allow_edges from=app/core/** allowed=app/db/**,orders/api.py"]


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false",
                    "-c", "commit.gpgsign=false", *args], cwd=cwd, check=True, capture_output=True)


def _write(repo: Path, rel: str, text: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8"))


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    r = tmp_path_factory.mktemp("ask") / "proj ğ"
    for rel, text in FILES.items():
        _write(r, rel, text)
    _git(r, "init", "-q")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "init")
    workflow.init(r)
    st = open_store(r)
    try:
        dm.record(st, r, chosen="layers and modules", rationale="the ui sits on top", guards=GUARDS)
    finally:
        st.close()
    return r


def _by(res: dict) -> dict:
    return {r["kind"]: r for r in res["rules"]}


def test_a_lower_layer_may_not_reach_up_and_the_rule_is_cited(repo):
    res = da.ask(repo, "app/db/new_cache.py", "app/ui/views.py")
    assert res["verdict"] == "forbidden" and da.exit_code(res) == 1
    rule = _by(res)["layers"]
    assert rule["verdict"] == "forbidden" and "layer 3 (app/db/**) may not depend on layer 1" in rule["why"]
    assert rule["status"] == "statically_verified" and rule["spec"] == GUARDS[0]
    path, _, line = rule["evidence"].rpartition(":")
    assert path.endswith(".md") and "layers order=app/ui/**" in (repo / path).read_text("utf-8").split("\n")[
        int(line) - 1]
    assert res["scope"]["guards"] == len(GUARDS) and res["source"] == "app/db/new_cache.py"


def test_a_module_name_resolves_to_its_file_and_a_downward_use_is_allowed(repo):
    res = da.ask(repo, "app/ui/views.py", "app.core.service")
    assert res["target_kind"] == "file" and res["target_path"] == "app/core/service.py"
    assert res["verdict"] == "allowed" and da.exit_code(res) == 0
    assert _by(res)["layers"]["verdict"] == "allows"


def test_public_interface_and_its_test_scope(repo):
    assert _by(da.ask(repo, "app/ui/views.py", "orders/_impl.py"))["public"]["verdict"] == "forbidden"
    assert _by(da.ask(repo, "app/ui/views.py", "orders/api.py"))["public"]["verdict"] == "allows"
    assert "public" not in _by(da.ask(repo, "orders/other.py", "orders/_impl.py"))  # inside the module
    assert "public" not in _by(da.ask(repo, "tests/test_orders.py", "orders/_impl.py"))  # test code: out of scope


def test_tags_no_edge_and_allow_edges(repo):
    res = da.ask(repo, "app/core/service.py", "old/thing.py")
    by = _by(res)
    assert res["verdict"] == "forbidden"
    assert by["no_edge"]["verdict"] == "forbidden" and by["allow_edges"]["verdict"] == "forbidden"
    assert _by(da.ask(repo, "app/core/service.py", "app/db/store.py"))["allow_edges"]["verdict"] == "allows"


def test_restricted_calls_and_absent_packages(repo):
    res = da.ask(repo, "app/core/service.py", "sqlite3")
    assert res["target_kind"] == "package" and res["verdict"] == "restricted" and da.exit_code(res) == 0
    assert "sqlite3.connect may be called only in app/db/store.py" in _by(res)["only_in"]["why"]
    assert _by(da.ask(repo, "app/db/store.py", "sqlite3"))["only_in"]["verdict"] == "allows"
    res = da.ask(repo, "app/ui/views.py", "psycopg")
    assert res["verdict"] == "forbidden" and _by(res)["dependency"]["verdict"] == "forbidden"
    res = da.ask(repo, "app/ui/views.py", "requests")
    assert res["verdict"] == "allowed" and res["rules"] == []
    assert any("not a project file" in x for x in res["limits"])


def _rec(repo: Path, *specs: str, waivers: list | None = None) -> dm.Decision:
    return dm.Decision(id="ADR-0009", number=9, title="t", waivers=waivers or [],
                       guards=[dm.parse_guard(s, repo, f"g{i}") for i, s in enumerate(specs, 1)])


def test_unknown_waived_and_no_rules(repo):
    res = da.ask(repo, "app/x.py", "old/thing.py", records=[_rec(repo, "no_edge from=app/** to=tag:nowhere")])
    assert res["verdict"] == "unknown" and da.exit_code(res) == 3
    assert res["rules"][0]["status"] == "unknown" and "no tag 'nowhere'" in res["rules"][0]["why"]
    res = da.ask(repo, "app/x.py", "old/thing.py", records=[_rec(
        repo, "no_edge from=app/** to=old/**", waivers=[{"guard": "g1", "at": "app/x.py", "reason": "migration"}])])
    assert res["verdict"] == "allowed" and res["rules"][0]["verdict"] == "waived"
    res = da.ask(repo, "app/x.py", "old/thing.py", records=[])
    assert res["verdict"] == "unknown" and "no accepted guard" in res["unknown"][0]
    proposed = _rec(repo, "no_edge from=app/** to=old/**")
    proposed.guards[0]["status"] = "proposed"
    assert da.ask(repo, "app/x.py", "old/thing.py", records=[proposed])["verdict"] == "unknown"


def test_bad_arguments_are_refused(repo):
    with pytest.raises(da.AskError, match="inside the repository"):
        da.ask(repo, "../elsewhere.py", "sqlite3")
    with pytest.raises(da.AskError, match="glob"):
        da.ask(repo, "app/**", "sqlite3")
    with pytest.raises(da.AskError, match="empty"):
        da.ask(repo, "app/x.py", " ")


def test_cli_and_mcp(repo, capsys):
    from verinoda.mcp.server import AtlasTools

    assert cli.main(["decide", "ask", "app/db/x.py", "app/ui/views.py", "--repo", str(repo)]) == 1
    out = capsys.readouterr().out
    assert out.startswith("forbidden: app/db/x.py -> app/ui/views.py") and "[forbidden]" in out
    assert cli.main(["decide", "ask", "app/ui/views.py", "app/core/service.py", "--repo", str(repo), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["verdict"] == "allowed"
    assert cli.main(["decide", "ask", "../x.py", "sqlite3", "--repo", str(repo), "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "error"
    t = AtlasTools(repo)
    m = t.dependency_ask("app/ui/views.py", "orders/_impl.py")
    assert m["verdict"] == "forbidden" and m["rules"][0]["kind"] == "public"
    assert t.dependency_ask("/etc/passwd", "x")["error"] == "invalid_argument"


# -- review round: how a target is named, scopes and shapes ----------------------------------------------------

MORE = {
    "app/api/logging.py": "X = 1\n",
    "scripts/psycopg.py": "X = 1\n",
    "web/ui/utils.ts": "export const u = 1;\n",
    "web/db/q.ts": "export const q = 1;\n",
    "src/main/java/com/acme/db/Store.java": "package com.acme.db;\nclass Store {}\n",
    "samples/demo.py": "X = 1\n",
    "README.md": "# x\n",
}


@pytest.fixture(scope="module")
def repo2(tmp_path_factory):
    r = tmp_path_factory.mktemp("ask2") / "proj"
    for rel, text in {**FILES, **MORE}.items():
        if rel != "app/__init__.py":  # app/ui stays a namespace package
            _write(r, rel, text)
    _git(r, "init", "-q")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "init")
    return r


def _one(repo: Path, *specs: str) -> list:
    return [_rec(repo, *specs)]


def test_a_bare_name_is_not_a_same_named_file_deeper_in_the_project(repo2):
    recs = _one(repo2, "layers order=app/api/**,app/core/**", "dependency absent=psycopg")
    res = da.ask(repo2, "app/core/x.py", "logging", records=recs)
    assert res["target_kind"] == "package" and res["verdict"] == "allowed"
    res = da.ask(repo2, "app/db/new.py", "psycopg", records=recs)
    assert res["target_kind"] == "package" and res["verdict"] == "forbidden"


def test_package_paths_are_packages(repo2):
    recs = _one(repo2, "dependency absent=lodash")
    res = da.ask(repo2, "web/ui/a.ts", "lodash/fp", records=recs)
    assert res["target_kind"] == "package" and res["verdict"] == "forbidden"
    for target in ("@angular/core", "github.com/pkg/errors"):
        assert da.ask(repo2, "web/ui/a.ts", target, records=recs)["target_kind"] == "package", target


def test_namespace_packages_symbols_and_jvm_classes_resolve_to_project_paths(repo2):
    recs = _one(repo2, "layers order=app/ui/**,app/core/**,app/db/**", "no_edge from=app/** to=src/main/java/**")
    for target in ("app.ui", "app.ui.views.show", "app/ui"):
        res = da.ask(repo2, "app/db/new.py", target, records=recs)
        assert res["target_kind"] == "file" and res["verdict"] == "forbidden", target
    res = da.ask(repo2, "app/db/new.py", "app.ui.later", records=recs)
    assert res["target_path"] == "app/ui/later" and res["verdict"] == "forbidden"
    assert _by(res)["layers"]["status"] == "strong_inference"  # a new module read from a dotted name
    res = da.ask(repo2, "app/db/new.py", "com.acme.db.Store", records=recs)
    assert res["target_path"] == "src/main/java/com/acme/db/Store.java" and res["verdict"] == "forbidden"


def test_relative_targets_are_read_from_the_source_folder_and_case_is_the_files(repo2):
    recs = _one(repo2, "layers order=web/ui/**,web/db/**")
    assert da.ask(repo2, "web/db/q.ts", "../ui/utils", records=recs)["target_path"] == "web/ui/utils.ts"
    assert da.ask(repo2, "web/db/q.ts", "../ui/utils", records=recs)["verdict"] == "forbidden"
    assert da.ask(repo2, "web/db/q.ts", "./x", records=recs)["target_path"] == "web/db/x"
    with pytest.raises(da.AskError, match="outside"):
        da.ask(repo2, "web/db/q.ts", "../../../x", records=recs)
    recs = _one(repo2, "layers order=app/ui/**,app/core/**,app/db/**")
    assert da.ask(repo2, "APP/db/new.py", "app/ui/views.py", records=recs)["verdict"] == "forbidden"
    res = da.ask(repo2, "app/db/store.py", "App/UI/views.py", records=recs)
    assert res["verdict"] == "forbidden" and res["target_path"] == "app/ui/views.py"


def test_a_folder_is_no_source(repo2):
    for source in ("./", ".", "app/core/", "app/db"):
        with pytest.raises(da.AskError, match="folder"):
            da.ask(repo2, source, "psycopg", records=[])


def test_only_in_patterns_and_scope(repo2):
    res = da.ask(repo2, "app/a.py", "sqlite3", records=_one(repo2, r"only_in pattern=sqlite3\.connect\( "
                                                                 "allowed=app/db/store.py"))
    assert res["verdict"] == "restricted"
    res = da.ask(repo2, "app/a.py", "sqlite3", records=_one(repo2, r"only_in pattern=sqlite3\.(connect|open) "
                                                                 "allowed=app/db/store.py"))
    assert res["verdict"] == "unknown" and _by(res)["only_in"]["verdict"] == "unknown"
    guard = _one(repo2, "only_in calls=sqlite3.connect allowed=app/db/store.py")
    assert da.ask(repo2, "README.md", "sqlite3", records=guard)["verdict"] == "allowed"  # no code: out of scope
    assert da.ask(repo2, "samples/demo.py", "sqlite3", records=guard)["verdict"] == "allowed"


def test_import_names_of_other_distributions(repo2):
    recs = _one(repo2, "dependency absent=PyYAML", "dependency absent=beautifulsoup4")
    assert da.ask(repo2, "app/a.py", "yaml", records=recs)["verdict"] == "forbidden"
    assert da.ask(repo2, "app/a.py", "bs4.element", records=recs)["verdict"] == "forbidden"
    res = da.ask(repo2, "app/a.py", "ruamel.yaml", records=recs)
    assert res["verdict"] == "allowed" and any("manifest name" in x for x in res["limits"])


def test_overlapping_layers_are_unknown_as_in_check(repo2):
    res = da.ask(repo2, "app/core/x.py", "app/ui/views.py", records=_one(repo2, "layers order=app/**,app/core/**"))
    assert res["verdict"] == "unknown" and "has no file of its own" in _by(res)["layers"]["why"]


def test_a_missing_decisions_folder_is_named_and_json_carries_the_exit(repo2, capsys):
    res = da.ask(repo2, "app/a.py", "psycopg", decisions_dir="nothere")
    assert res["verdict"] == "unknown" and "does not exist" in res["unknown"][0] and res["exit"] == 3
    assert cli.main(["decide", "ask", "app/a.py", "psycopg", "--repo", str(repo2), "--json"]) == 3
    assert json.loads(capsys.readouterr().out)["exit"] == 3
