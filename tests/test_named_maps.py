"""Named maps: a trace or map result saved under a name, read back current or stale (examples/orders_app copy)."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import json  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import cli, named_maps, workflow  # noqa: E402
from verinoda.store import open_store  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "orders_app"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args],
                   cwd=cwd, check=True, capture_output=True)


@pytest.fixture()
def repo(tmp_path):
    dst = tmp_path / "orders_app"
    shutil.copytree(EXAMPLE, dst, ignore=shutil.ignore_patterns(".verinoda", "__pycache__", "*.pyc",
                                                                ".pytest_cache", "*.db"))
    _git(dst, "init", "-q")
    _git(dst, "add", "-A")
    _git(dst, "commit", "-q", "-m", "init")
    workflow.init(dst)
    st = open_store(dst)
    workflow.scan(st, dst)
    st.close()
    return dst


def run(capsys, *argv: str) -> tuple[int, str]:
    rc = cli.main(list(argv))
    return rc, capsys.readouterr().out


def _edit(repo: Path, rel: str) -> None:
    p = repo / rel
    p.write_text(p.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8", newline="\n")


def test_cited_files_reads_paths_lines_and_symbols():
    known = {"a/b.py", "c.py", "d.py", "e.py"}
    res = {"at": "a/b.py:12", "x": ["c.py::Foo.bar", {"d.py": 1}], "y": "e.py:3-9", "z": "not/known.py:1",
           "w": "a/b.py is mentioned in prose"}
    assert named_maps.cited_files(res, known) == ["a/b.py", "c.py", "d.py", "e.py"]


def test_names_are_checked():
    for bad in ("", "../x", "a/b", ".hidden", "-x", "a" * 65, "x y"):
        with pytest.raises(ValueError):
            named_maps.check_name(bad)
    assert named_maps.check_name(" flow.v2_x-1 ") == "flow.v2_x-1"


def test_trace_saved_reads_current_then_stale(repo, capsys):
    rc, out = run(capsys, "map", "save", "order-flow", "--trace", "create_order_handler", "OrderRepository.save",
                  "--repo", str(repo), "--json")
    saved = json.loads(out)
    assert rc == 0 and saved["status"] == "saved" and saved["kind"] == "trace" and saved["cited_files"] >= 2
    assert saved["path"] == ".verinoda/maps/order-flow.json" and "stale_at_save" not in saved
    doc = json.loads((repo / saved["path"]).read_text(encoding="utf-8"))
    assert doc["snapshot"] == saved["snapshot"] and doc["commit"] and doc["result"]["status"] == "found"
    assert doc["args"] == {"source": "create_order_handler", "target": "OrderRepository.save", "mode": "flow"}
    assert all(len(sha) == 64 for sha in doc["files"].values())

    res = named_maps.read(repo, "order-flow")
    assert res["status"] == "current" and res["as_of"]["snapshot"] == saved["snapshot"]
    assert res["claims"][0]["status"] == "primary_source_verified" and "have the content" in res["claims"][0]["text"]
    assert res["result"]["paths"] and res["limits"]

    # a file the map does not cite changes nothing; one it cites makes it stale
    other = next(f for f in ("orders/pricing.py", "README.md") if f not in doc["files"] and (repo / f).is_file())
    _edit(repo, other)
    assert named_maps.read(repo, "order-flow")["status"] == "current"
    cited = sorted(doc["files"])[0]
    _edit(repo, cited)
    res = named_maps.read(repo, "order-flow")
    assert res["status"] == "stale" and res["changed_files"] == [cited] and res["changed_count"] == 1
    assert res["claims"][0]["evidence"][0] == {"path": cited, "sha256_saved": doc["files"][cited][:12],
                                               "change": "modified"}
    assert "as it was then" in res["claims"][0]["text"]
    assert "--trace create_order_handler OrderRepository.save" in res["next_step"]
    assert res["result"]["paths"]  # what it was, marked stale - never dropped, never current

    rc, out = run(capsys, "map", "show", "order-flow", "--repo", str(repo))
    assert rc == 1 and "STALE: 1 cited file(s) changed since: " + cited in out
    assert "not the code now" in out and "path 1:" in out
    rc, out = run(capsys, "map", "list", "--repo", str(repo), "--json")
    m = json.loads(out)["maps"][0]
    assert rc == 0 and (m["name"], m["kind"], m["status"], m["changed_count"]) == ("order-flow", "trace", "stale", 1)

    (repo / cited).unlink()
    res = named_maps.read(repo, "order-flow")
    assert res["status"] == "stale" and res["claims"][0]["evidence"][0]["change"] == "removed"


def test_view_saved_and_listed(repo, capsys):
    rc, out = run(capsys, "map", "save", "deps", "--view", "dependencies", "--repo", str(repo), "--json")
    saved = json.loads(out)
    assert rc == 0 and saved["kind"] == "map" and saved["cited_files"] > 0
    res = named_maps.read(repo, "deps")
    assert res["status"] == "current" and list(res["result"]) == ["dependencies"]
    assert res["args"] == {"view": "dependencies"}
    rc, out = run(capsys, "map", "show", "deps", "--repo", str(repo))
    assert rc == 0 and "current: the" in out and "dependencies" in out
    rc, out = run(capsys, "map", "list", "--repo", str(repo))
    assert rc == 0 and "deps" in out and "current" in out
    # saving again under the name replaces it
    rc, _ = run(capsys, "map", "save", "deps", "--view", "tests", "--repo", str(repo), "--json")
    assert rc == 0 and named_maps.read(repo, "deps")["args"] == {"view": "tests"}


def test_saved_from_a_changed_file_reads_stale(repo, capsys):
    _edit(repo, "orders/repository.py")  # the index still describes the committed version
    rc, out = run(capsys, "map", "save", "f", "--trace", "create_order_handler", "OrderRepository.save",
                  "--repo", str(repo), "--json")
    saved = json.loads(out)
    assert rc == 0 and saved["stale_at_save"] == ["orders/repository.py"] and "reads back stale" in saved["note"]
    assert named_maps.read(repo, "f")["status"] == "stale"


def test_refusals(repo, capsys):
    rc, out = run(capsys, "map", "save", "x", "--trace", "zzqq_no_such_symbol", "OrderRepository.save",
                  "--repo", str(repo))
    assert rc == 2 and not (repo / ".verinoda" / "maps" / "x.json").exists()
    assert cli.main(["map", "save", "../x", "--view", "tests", "--repo", str(repo)]) == 2
    assert cli.main(["map", "save", "--repo", str(repo)]) == 2
    assert cli.main(["map", str(repo), "extra"]) == 2
    rc, out = run(capsys, "map", "show", "nope", "--repo", str(repo), "--json")
    assert rc == 2 and json.loads(out)["status"] == "not_found"
    rc, out = run(capsys, "map", "list", "--repo", str(repo), "--json")
    assert rc == 0 and json.loads(out)["status"] == "none"


def test_mcp_map_view_saved(repo, capsys):
    from verinoda.mcp.server import AtlasTools

    assert cli.main(["map", "save", "order-flow", "--trace", "create_order_handler", "OrderRepository.save",
                     "--repo", str(repo)]) == 0
    capsys.readouterr()
    t = AtlasTools(repo)
    res = t.map_view("saved", targets=["order-flow"])
    assert res["status"] == "current" and res["result"]["status"] == "found" and res["claims"]
    assert list(res)[:3] == ["status", "name", "as_of"]  # never cut before the saved result
    assert t.map_view("saved")["maps"][0]["name"] == "order-flow"
    assert t.map_view("saved", targets=["a", "b"])["error"] == "invalid_argument"
    assert t.map_view("saved", targets=["../x"])["error"] == "invalid_argument"
    assert t.map_view("saved", targets=["nope"])["status"] == "not_found"
    small = AtlasTools(repo, max_chars=900).map_view("saved", targets=["order-flow"])
    assert small["status"] == "current" and small["as_of"] and small.get("truncated")
