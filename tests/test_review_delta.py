"""Differential findings of `verinoda review` (docs/DESIGN.md D35): each finding introduced, preexisting or fixed
between the base and the head; by default the review lists only what the change introduced."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import json  # noqa: E402
import shutil  # noqa: E402

import pytest  # noqa: E402

from tests.test_review import _edit, _project, _review  # noqa: E402
from verinoda import review as rv  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")

FILES = {
    "app/__init__.py": "",
    "app/main.py": "from app.store import load, run, save\n\n\ndef main(conn, cmd, blob):\n"
                   "    return save(conn, \"x\"), run(cmd), load(blob)\n",
    "app/store.py": "import json\nimport pickle\nimport subprocess\n\n\n"
                    "def save(conn, row):\n    conn.execute(\"INSERT INTO t VALUES (?)\", (row,))\n    return row\n\n\n"
                    "def run(cmd):\n    return subprocess.run(cmd, shell=True)\n\n\n"
                    "def load(blob):\n    return pickle.loads(blob)\n",
}


def _change(repo):
    # an operation's line edited (the call unchanged), an operation removed, a write edited, and a new write
    _edit(repo, "app/store.py", "    return subprocess.run(cmd, shell=True)\n",
          "    done = subprocess.run(cmd, shell=True)\n    return done\n")
    _edit(repo, "app/store.py", "return pickle.loads(blob)", "return json.loads(blob)")
    _edit(repo, "app/store.py", "(row,))", "(row.strip(),))")
    p = repo / "app" / "store.py"
    p.write_text(p.read_text(encoding="utf-8") + "\n\ndef purge(conn):\n    conn.execute(\"DELETE FROM t\")\n",
                 encoding="utf-8", newline="\n")


def _all(res: dict) -> list[dict]:
    return [f for fs in res["concerns"].values() for f in fs]


def test_the_review_lists_only_what_the_change_introduced(tmp_path):
    repo = _project(tmp_path, "delta", FILES)
    _change(repo)
    res = _review(repo)
    shown = _all(res)
    assert shown and {f["delta"] for f in shown} == {"introduced"}
    # a write the change edits or adds is what it touches: introduced, even where the base wrote too
    sinks = {f["for"] for f in shown if f["rule"] == "sink-on-changed-line"}
    assert {"app/store.py::save", "app/store.py::purge"} <= sinks
    dif = res["differential"]
    assert dif["shown"] == "introduced" and dif["status"] == "strong_inference"
    [pre] = [f for f in dif["preexisting_findings"] if f["rule"] == "op-on-changed-line"]
    assert pre["for"] == "app/store.py::run" and pre["at"] == "app/store.py:12" and pre["base_at"] == "app/store.py:12"
    assert not any(f["rule"] == "op-on-changed-line" and f.get("for") == "app/store.py::run" for f in shown)
    [fixed] = [f for f in dif["fixed_findings"] if f["rule"] == "op-on-changed-line"]
    assert fixed["for"] == "app/store.py::load" and fixed["at"] == "app/store.py:16" and fixed["side"] == "base"
    assert "pickle" in fixed["finding"] or "deserial" in fixed["finding"]
    assert dif["preexisting"] == len(dif["preexisting_findings"]) and dif["introduced"] == len(shown)
    assert res["counts"]["preexisting"] == dif["preexisting"] and res["counts"]["fixed"] == dif["fixed"]
    # a hidden preexisting finding is named, never read as "no finding"
    assert "preexisting" in res["concerns_checked"]["security"]
    assert "preexisting" in res["summary"] and "fixed" in res["summary"]
    text = rv.render_text(res)
    assert "Against the base" in text and "--findings all" in text and "fixed [" in text


def test_findings_all_lists_the_preexisting_ones_labelled(tmp_path):
    repo = _project(tmp_path, "delta", FILES)
    _change(repo)
    res = _review(repo, findings="all")
    got = {(f["rule"], f.get("for")): f["delta"] for f in _all(res)}
    assert got[("op-on-changed-line", "app/store.py::run")] == "preexisting"
    assert got[("sink-on-changed-line", "app/store.py::purge")] == "introduced"
    assert "preexisting_findings" not in res["differential"] and res["differential"]["fixed"] >= 1
    assert ", preexisting]" in rv.render_text(res)


def test_only_preexisting_findings_leave_the_concern_empty(tmp_path):
    repo = _project(tmp_path, "delta", FILES)
    _edit(repo, "app/store.py", "    return subprocess.run(cmd, shell=True)\n",
          "    done = subprocess.run(cmd, shell=True)\n    return done\n")
    res = _review(repo, concerns=["security"])
    assert res["concerns"]["security"] == [] and res["differential"]["preexisting"] == 1
    assert res["concerns_checked"]["security"].startswith("no finding the change introduced")
    assert "No finding the change introduced." in res["summary"]
    assert res["counts"]["strong_or_verified"] == 0 and res["unknown"] == [] and res["exit"] == 0


def test_rules_about_the_change_itself_are_always_introduced(tmp_path):
    files = {"app/__init__.py": "", "app/guard.py": "def check(user):\n    if user is None:\n        raise ValueError"
                                                    "(\"no user\")\n    return user\n"}
    repo = _project(tmp_path, "delta", files)
    _edit(repo, "app/guard.py", "    if user is None:\n        raise ValueError(\"no user\")\n", "")
    res = _review(repo)
    [g] = [f for f in res["concerns"]["security"] if f["rule"] == "guard-removed"]
    assert g["delta"] == "introduced" and res["differential"]["fixed"] == 0


def test_a_changed_call_of_an_operation_is_introduced(tmp_path):
    repo = _project(tmp_path, "delta", FILES)
    _edit(repo, "app/store.py", "subprocess.run(cmd, shell=True)", "subprocess.run(cmd + \" -v\", shell=True)")
    [f] = [f for f in _review(repo)["concerns"]["security"] if f["rule"] == "op-on-changed-line"]
    assert f["delta"] == "introduced" and f["base_had"] == "kind"


def test_a_planned_change_has_no_differential(tmp_path):
    repo = _project(tmp_path, "delta", FILES)
    res = _review(repo, targets=["app/store.py::save"], change="body")
    assert "skipped" in res["differential"]


def test_unknown_findings_mode_is_refused(tmp_path):
    repo = _project(tmp_path, "delta", FILES)
    with pytest.raises(ValueError, match="findings must be one of"):
        _review(repo, findings="new")


def test_the_cli_takes_findings(tmp_path, capsys):
    from verinoda import cli

    repo = _project(tmp_path, "delta", FILES)
    _change(repo)
    code = cli.main(["review", str(repo), "--findings", "all", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert code == 3 and out["differential"]["shown"] == "all"
