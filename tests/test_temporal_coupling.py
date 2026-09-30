"""Temporal coupling (verinoda/history.py co_changes, architecture_map.impact): files that changed together with a
change in git and that no graph edge links to it, listed by the impact view as strong_inference claims with their
commit counts and the shared commits as evidence."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import json  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import architecture_map as am  # noqa: E402
from verinoda import cli, history, index, map_text, workflow  # noqa: E402
from verinoda.store import open_store  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false",
                           "-c", "init.defaultBranch=main", "-c", "commit.gpgsign=false", *args], cwd=cwd,
                          check=True, capture_output=True, text=True, stdin=subprocess.DEVNULL).stdout.strip()


def _commit(repo: Path, files: dict[str, str | None], msg: str) -> str:
    for rel, text in files.items():
        p = repo / rel
        if text is None:
            p.unlink()
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(text.encode("utf-8"))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


def _scan(repo: Path):
    workflow.init(repo)
    st = open_store(repo)
    workflow.scan(st, repo)
    st.close()
    return index.load(repo)


@pytest.fixture(scope="module")
def proj(tmp_path_factory):
    """a.py imports x.py; b.py imports a.py; c.py, cfg.toml and old.py (later deleted) share no edge with a.py.
    a.py changes with b, c, x, cfg and old three times; alone once; three times in a 33-file reformat with d.py;
    twice with e.py (new then)."""
    r = tmp_path_factory.mktemp("coupling") / "proj"
    r.mkdir()
    _git(r, "init", "-q")
    shas = {"init": _commit(r, {"x.py": "def helper():\n    return 1\n",
                                "a.py": "from x import helper\n\n\ndef run(v=0):\n    return helper() + v\n",
                                "b.py": "from a import run\n\n\ndef go():\n    return run()\n",
                                "c.py": "def other(v=0):\n    return v\n", "d.py": "D = 0\n",
                                "cfg.toml": "limit = 0\n", "old.py": "OLD = 0\n"}, "Initial import")}
    for i in range(1, 4):
        shas[f"together{i}"] = _commit(r, {
            "a.py": f"from x import helper\n\n\ndef run(v={i}):\n    return helper() + v\n",
            "b.py": f"from a import run\n\n\ndef go():\n    return run({i})\n",
            "x.py": f"def helper():\n    return {i + 1}\n", "c.py": f"def other(v={i}):\n    return v\n",
            "cfg.toml": f"limit = {i}\n", "old.py": f"OLD = {i}\n"}, f"Change run and its companions {i}")
    _commit(r, {"a.py": "from x import helper\n\n\ndef run(v=9):\n    return helper() + v\n", "old.py": None},
            "Change run alone, drop old")
    for i in range(1, 4):
        bulk = {f"fill/f{k}.txt": f"{i}\n" for k in range(31)}
        _commit(r, {**bulk, "a.py": f"from x import helper\n\n\ndef run(v=9):\n    return helper() + v + {i}\n",
                    "d.py": f"D = {i}\n"}, f"Reformat {i}")
    for i in range(1, 3):
        _commit(r, {"a.py": f"from x import helper\n\n\ndef run(v=7):\n    return helper() + v - {i}\n",
                    "e.py": f"E = {i}\n"}, f"Tune e {i}")
    return r, shas


def test_co_changes_counts_shared_commits_and_leaves_out_bulk_missing_and_rare_files(proj):
    r, _ = proj
    res = history.co_changes(r, ["a.py"])
    assert res["is_git"] and res["commits_read"] == 10 and res["bulk_skipped"] == 3
    got = {c["file"]: (c["commits"], c["target_commits"]) for c in res["coupled"]}
    # a.py: init, three companion commits, alone, two with e = 7 commits read (the reformats left out)
    assert got == {"b.py": (4, 7), "c.py": (4, 7), "x.py": (4, 7), "cfg.toml": (4, 7)}
    assert "d.py" not in got and "e.py" not in got and "old.py" not in got  # bulk only / 2 < 3 / deleted
    assert "no graph edge" not in res["coupled"][0]["text"]  # nothing was said about the graph


def test_impact_lists_history_coupled_files_without_a_static_edge_as_strong_inference(proj):
    r, shas = proj
    g = _scan(r)
    imp = am.impact(g, ["a.py"])
    assert "b.py" in imp["affected_files"]  # a static dependent, listed by the walk
    coupled = {c["file"]: c for c in imp["history_coupled"]}
    assert set(coupled) == {"c.py", "cfg.toml"}  # b.py is reached, x.py is imported by a.py
    c = coupled["c.py"]
    assert c["kind"] == "temporal_coupling" and c["status"] == "strong_inference"
    assert c["coupled_to"] == "a.py" and c["commits"] == 4 and c["target_commits"] == 7 and c["degree"] == 0.57
    assert "c.py changed in 4 of the 7 commits that changed a.py" in c["text"]
    assert "no graph edge links the two files" in c["text"] and c["uncertainties"]
    ev = c["evidence"]
    assert len(ev) == 3 and all(e["source_type"] == "git_history" for e in ev)
    assert ev[0]["commit_sha"] == shas["together3"]
    # the evidence names both files the commit changed, not only the coupled one
    assert ev[0]["locator"] == f"commit {shas['together3']} a.py and c.py"
    assert ev[0]["meta"]["files"] == ["a.py", "c.py"]
    hc = imp["history_coupling"]
    assert hc["is_git"] and hc["commits_read"] == 10 and hc["bulk_skipped"] == 3
    assert "not a dependency" in " ".join(hc["limits"]) and "git log" in hc["method"]
    assert "history_coupled" not in am.impact(g, ["a.py"], co_change=False)
    json.dumps(imp)


def test_a_symbol_target_reads_its_file_and_a_plain_file_target_is_read_too(proj):
    r, _ = proj
    g = index.load(r)
    by_symbol = am.impact(g, ["a.py::run"])
    assert {c["file"] for c in by_symbol["history_coupled"]} == {"c.py", "cfg.toml"}
    by_file = am.impact(g, ["cfg.toml"])
    got = {c["file"]: c["commits"] for c in by_file["history_coupled"]}
    assert got.get("a.py") == 4 and got.get("c.py") == 4


def test_text_summary_names_the_coupled_files(proj):
    r, _ = proj
    imp = am.impact(index.load(r), ["a.py"])
    text = map_text.render({"impact": imp}, 40)
    assert "changed together in git, no graph edge (2; last 10 commits):" in text
    assert "[strong_inference] c.py: 4 of 7 commits of a.py" in text


def test_cli_map_impact_json_carries_the_claims(proj, capsys):
    r, _ = proj
    assert cli.main(["map", str(r), "--view", "impact", "--target", "a.py", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert {c["file"] for c in out["impact"]["history_coupled"]} == {"c.py", "cfg.toml"}


def test_without_git_nothing_is_coupled(tmp_path):
    repo = tmp_path / "nogit"
    repo.mkdir()
    (repo / "app.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    imp = am.impact(_scan(repo), ["app.py"])
    assert imp["history_coupled"] == [] and imp["history_coupling"]["is_git"] is False


def test_a_plain_file_target_is_read_in_git_and_is_not_an_unresolved_name(proj, capsys):
    r, _ = proj
    imp = am.impact(index.load(r), ["cfg.toml"])
    assert imp["unresolved"] == [] and imp["history_only_targets"] == ["cfg.toml"]
    assert "resolution" not in imp
    assert cli.main(["map", str(r), "--view", "impact", "--target", "cfg.toml"]) == 0
    out = capsys.readouterr()
    assert "read in git history only: cfg.toml" in out.out and "a.py: 4 of" in out.out
    assert "not used" not in out.out and "error" not in out.err
    # without the co-change reading, a file the graph does not hold is an unresolved target, as before
    assert am.impact(index.load(r), ["cfg.toml"], co_change=False)["unresolved"] == ["cfg.toml"]


def test_a_file_joined_by_any_graph_edge_is_not_listed(tmp_path):
    """index.ts re-exports helpers.ts (a relation the impact walk does not follow); they always change together."""
    r = tmp_path / "ts"
    r.mkdir()
    _git(r, "init", "-q")
    for i in range(4):
        _commit(r, {"helpers.ts": f"export function fmtA(v: number) {{ return v + {i}; }}\n",
                    "index.ts": f"export * from './helpers';\nexport const VERSION = {i};\n",
                    "notes.md": f"version {i}\n"}, f"Release {i}")
    g = _scan(r)
    assert any(d.get("relation") == "re_exports" and {g.file(u), g.file(v)} == {"index.ts", "helpers.ts"}
               for u, v, d in g.G.edges(data=True))
    imp = am.impact(g, ["helpers.ts"])
    assert "index.ts" not in imp["affected_files"]  # the walk does not follow re_exports ...
    got = {c["file"] for c in imp["history_coupled"]}
    assert "index.ts" not in got and "notes.md" in got  # ... and still the edge keeps it off the list


def test_a_shallow_clone_says_its_history_is_cut(proj, tmp_path):
    r, _ = proj
    clone = tmp_path / "shallow ç"
    subprocess.run(["git", "clone", "-q", "--depth", "2", r.resolve().as_uri(), str(clone)], check=True,
                   capture_output=True, stdin=subprocess.DEVNULL)
    res = history.co_changes(clone, ["a.py"])
    assert res["commits_read"] == 2 and res["coupled"] == [] and res["shallow"] is True
    assert any("shallow clone" in x for x in res["coverage"]["limits"])
    assert "shallow" not in history.co_changes(r, ["a.py"])
    imp = am.impact(index.load(r), ["a.py"])
    imp["history_coupling"] = {"shallow": True, "commits_read": 2}
    text = map_text.render({"impact": imp}, 40)
    assert "a shallow clone, only 2 commits to read" in text


def test_a_failed_git_log_is_an_error_not_an_empty_finding(proj, monkeypatch):
    r, _ = proj
    real = history._git
    monkeypatch.setattr(history, "_git", lambda repo, *a: None if a[0] == "log" else real(repo, *a))
    res = history.co_changes(r, ["a.py"])
    assert res["coupled"] == [] and "git log failed" in res["error"]
    imp = am.impact(index.load(r), ["a.py"])
    assert "git log failed" in imp["history_coupling"]["error"]
    assert "not read (git log failed" in map_text.render({"impact": imp}, 40)


def test_analyze_impact_does_not_read_co_change(proj, monkeypatch):
    from verinoda import analysis

    r, _ = proj
    index.load(r)
    seen = []
    real = am.impact
    monkeypatch.setattr(am, "impact", lambda *a, **k: seen.append(k.get("co_change", True)) or real(*a, **k))
    st = open_store(r)
    try:
        analysis.analyze(st, r, "What breaks if the run function changes?")
    finally:
        st.close()
    assert seen and not any(seen)
