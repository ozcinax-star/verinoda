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
    # a location inside a longer text (the tests view's keys and items) is cited too
    assert named_maps.cited_files({"apply() (a/b.py:11)": ["c.py:9 .__init__()"], "n": "see d.py."}, known) ==         ["a/b.py", "c.py", "d.py"]
    assert named_maps.cited_files({"x": "no path here: b.py, a/b.pyc"}, known) == []


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


def test_tests_view_cites_what_it_names_and_goes_stale(repo, capsys):
    rc, out = run(capsys, "map", "save", "tv", "--view", "tests", "--repo", str(repo), "--json")
    assert rc == 0
    doc = json.loads((repo / ".verinoda" / "maps" / "tv.json").read_text(encoding="utf-8"))
    text = json.dumps(doc["result"])
    named = {f for f in ("orders/pricing.py", "orders/service.py", "orders/repository.py", "orders/api.py",
                         "orders/config.py") if f in text}
    assert named and named <= set(doc["files"])  # every snapshot file the view names is cited
    assert named_maps.read(repo, "tv")["status"] == "current"
    _edit(repo, sorted(named)[0])
    rc, out = run(capsys, "map", "show", "tv", "--repo", str(repo))
    assert rc == 1 and "STALE" in out


def test_view_saved_beside_an_unrelated_stale_file_cites_nothing_and_reads_unknown(repo, capsys):
    _edit(repo, "orders/pricing.py")  # changed since the index; the cycles view says nothing about it
    rc, out = run(capsys, "map", "save", "cyc", "--view", "cycles", "--repo", str(repo), "--json")
    saved = json.loads(out)
    assert rc == 0 and "stale_at_save" not in saved and saved["cited_files"] == 0 and "unknown" in saved["note"]
    doc = json.loads((repo / saved["path"]).read_text(encoding="utf-8"))
    assert doc["files"] == {} and "stale_files" not in json.dumps(doc["result"])
    res = named_maps.read(repo, "cyc")
    assert res["status"] == "unknown" and res["claims"][0]["status"] == "unknown"
    assert "cites no file" in res["claims"][0]["text"] and "--view cycles" in res["next_step"]
    rc, out = run(capsys, "map", "show", "cyc", "--repo", str(repo))
    assert rc == 1 and "UNKNOWN" in out
    rc, out = run(capsys, "map", "list", "--repo", str(repo), "--json")
    assert json.loads(out)["maps"][0]["status"] == "unknown"


def test_impact_without_target_keeps_the_targets_it_used(repo, capsys):
    _edit(repo, "orders/service.py")
    rc, out = run(capsys, "map", "save", "imp", "--view", "impact", "--repo", str(repo), "--json")
    assert rc == 0
    doc = json.loads((repo / ".verinoda" / "maps" / "imp.json").read_text(encoding="utf-8"))
    assert doc["args"] == {"view": "impact", "target": ["orders/service.py"]}
    assert "--target orders/service.py" in named_maps.read(repo, "imp")["next_step"]


def test_names_differing_in_case_do_not_replace_each_other(repo, capsys):
    assert cli.main(["map", "save", "t1", "--trace", "create_order_handler", "OrderRepository.save",
                     "--repo", str(repo)]) == 0
    assert cli.main(["map", "save", "T1", "--view", "cycles", "--repo", str(repo)]) == 2
    capsys.readouterr()
    assert named_maps.read(repo, "t1")["kind"] == "trace"
    assert named_maps.read(repo, "T1")["status"] == "not_found"  # the stored name, exactly


def test_a_broken_map_file_is_reported_and_never_hides_the_others(repo, capsys):
    from verinoda.mcp.server import AtlasTools

    assert cli.main(["map", "save", "good", "--trace", "create_order_handler", "OrderRepository.save",
                     "--repo", str(repo)]) == 0
    d = repo / ".verinoda" / "maps"
    (d / "bad.json").write_text(json.dumps({"format": named_maps.FORMAT, "name": "bad", "kind": "trace",
                                            "files": ["orders/api.py"], "result": None}), encoding="utf-8")
    (d / "out.json").write_text(json.dumps({"format": named_maps.FORMAT, "name": "out", "kind": "trace",
                                            "files": {"../x.py": "0" * 64}, "result": None}), encoding="utf-8")
    (d / "junk.json").write_text("not json", encoding="utf-8")
    capsys.readouterr()
    for name in ("bad", "out", "junk"):
        rc, out = run(capsys, "map", "show", name, "--repo", str(repo))
        assert rc == 2 and out.startswith("error: " + name + ".json")
    rc, out = run(capsys, "map", "list", "--repo", str(repo), "--json")
    rows = {m["name"]: m["status"] for m in json.loads(out)["maps"]}
    assert rc == 0 and rows == {"bad": "invalid", "good": "current", "junk": "invalid", "out": "invalid"}
    assert len(AtlasTools(repo).map_view("saved")["maps"]) == 4


def test_an_unreadable_index_is_not_read_as_stale(repo, tmp_path, capsys):
    assert cli.main(["map", "save", "t1", "--trace", "create_order_handler", "OrderRepository.save",
                     "--repo", str(repo)]) == 0
    other = tmp_path / "copy"
    shutil.copytree(repo / ".verinoda" / "maps", other / ".verinoda" / "maps")
    (other / ".verinoda" / "atlas.db").write_bytes(b"")
    shutil.copytree(repo / "orders", other / "orders")
    capsys.readouterr()
    res = named_maps.read(other, "t1")
    assert res["status"] == "current"
    with pytest.raises(ValueError, match="cannot be read"):
        named_maps.save(other, "t2", "trace", {}, {"x": "orders/api.py"})


def test_arguments_that_would_be_ignored_are_refused(repo, capsys):
    assert cli.main(["map", "save", "x", "--view", "tests", "--trace", "create_order_handler",
                     "OrderRepository.save", "--repo", str(repo)]) == 2
    assert cli.main(["map", "save", "x", "--view", "tests", "--mode", "any", "--repo", str(repo)]) == 2
    assert cli.main(["map", "show", "x", "--view", "dead", "--repo", str(repo)]) == 2
    assert cli.main(["map", "list", "x", "--repo", str(repo)]) == 2
    assert not (repo / ".verinoda" / "maps" / "x.json").exists()
