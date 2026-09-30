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
    # the same change with every finding listed: the preexisting one counts
    assert _review(repo, concerns=["security"], findings="all")["exit"] == 3


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


def test_an_operation_added_next_to_an_old_one_is_introduced(tmp_path):
    # two operations of one kind in one function: the new one is never paired with the old one's base finding
    repo = _project(tmp_path, "delta", FILES)
    _edit(repo, "app/store.py", "    return subprocess.run(cmd, shell=True)\n",
          "    done = subprocess.run(cmd, shell=True)\n    subprocess.run(\"rm -rf /tmp/x\" + cmd, shell=True)\n"
          "    return done\n")
    res = _review(repo, concerns=["security"])
    [new] = [f for f in res["concerns"]["security"] if f["rule"] == "op-on-changed-line"]
    assert new["at"] == "app/store.py:13" and new["delta"] == "introduced" and "adds" in new["finding"]
    [old] = res["differential"]["preexisting_findings"]
    assert old["at"] == "app/store.py:12" and old["base_at"] == "app/store.py:12" and old["base_had"] == "call"
    assert res["differential"]["fixed"] == 0


def test_an_added_eval_next_to_an_old_one_is_introduced(tmp_path):
    repo = _project(tmp_path, "delta", {"app/__init__.py": "",
                                        "app/calc.py": "def calc(a, b):\n    x = eval(a)\n    return x\n"})
    _edit(repo, "app/calc.py", "    x = eval(a)\n", "    x = eval(a)  \n    y = eval(b)\n")
    res = _review(repo, concerns=["security"], findings="all")
    got = {f["at"]: f["delta"] for f in res["concerns"]["security"] if f["rule"] == "op-on-changed-line"}
    assert got == {"app/calc.py:2": "preexisting", "app/calc.py:3": "introduced"}


@pytest.mark.parametrize("rel, old, new", [
    ("app/s.py", "def load(blob):\n    return pickle.loads(blob)\n",
     "def load(blob):\n    return subprocess.run(blob, shell=True)\n"),
    ("app/r.js", "function r(c) {\n  return new ProcessBuilder(c);\n}\n",
     "function r(c) {\n  return new ObjectInputStream(c).readObject();\n}\n"),
])
def test_an_operation_swapped_for_another_kind_is_introduced_and_the_old_one_fixed(tmp_path, rel, old, new):
    # one pattern covers every kind of operation: the kind keeps a swapped operation from pairing with the old one
    head = "import pickle\nimport subprocess\n\n\n" if rel.endswith(".py") else ""
    repo = _project(tmp_path, "delta", {"app/__init__.py": "", rel: head + old})
    _edit(repo, rel, old, new)
    res = _review(repo, concerns=["security"])
    ops = [f for f in res["concerns"]["security"] if f["rule"] == "op-on-changed-line"]
    assert ops and {f["delta"] for f in ops} == {"introduced"} and all("adds" in f["finding"] for f in ops)
    dif = res["differential"]
    assert dif["preexisting"] == 0 and dif["fixed"] >= 1
    gone = {f["op_kind"] for f in dif["fixed_findings"]}
    assert gone & {"deserialization", "process-exec"} and not gone & {f["op_kind"] for f in ops}


@pytest.mark.parametrize("src, old, new", [
    ("def login(user, password):\n    ok = verify(password)\n    return ok\n",
     "verify(password)", "verify(password, strict=False)"),
    ("import os\n\n\ndef conf():\n    api_key = os.environ['KEY']\n    return api_key\n",
     "os.environ['KEY']", "'sk-live-hardcoded'"),
])
def test_security_words_on_an_edited_line_are_introduced(tmp_path, src, old, new):
    # the word rule says what a changed line names: the change's own, even where the base line named it too
    repo = _project(tmp_path, "delta", {"app/__init__.py": "", "app/auth.py": src})
    _edit(repo, "app/auth.py", old, new)
    res = _review(repo, concerns=["security"])
    [w] = [f for f in res["concerns"]["security"] if f["rule"] == "security-words"]
    assert w["delta"] == "introduced" and res["differential"]["preexisting"] == 0
    assert "No finding the change introduced." not in res["summary"]


WRITE = "        conn.execute(\"INSERT INTO t VALUES (?)\", (r,))\n"
LOOP = "import requests\n\n\ndef sync(conn, rows):\n    for r in rows:\n" + WRITE


def test_io_in_a_loop_that_did_it_before_is_preexisting(tmp_path):
    # the rule's own text says the loop and the IO were there before: preexisting, even with no base-side finding
    repo = _project(tmp_path, "delta", {"app/__init__.py": "", "app/sync.py": LOOP})
    _edit(repo, "app/sync.py", WRITE, WRITE + "        requests.post(\"http://x/hook\", json=r)\n")
    res = _review(repo, concerns=["performance"], findings="all")
    [f] = [f for f in res["concerns"]["performance"] if f["rule"] == "io-in-loop"]
    assert "were there before the change" in f["finding"] and f["base_had"] == "loop"
    assert f["delta"] == "preexisting" and "base_at" not in f
    shown = _review(repo, concerns=["performance"])
    assert not [g for g in shown["concerns"]["performance"] if g["rule"] == "io-in-loop"]
    assert shown["differential"]["preexisting_by_concern"] == {"performance": 1}


def test_io_added_to_a_loop_is_introduced(tmp_path):
    repo = _project(tmp_path, "delta", {"app/__init__.py": "", "app/sync.py": LOOP.replace(WRITE, "        print(r)\n")})
    _edit(repo, "app/sync.py", "        print(r)\n", WRITE)
    res = _review(repo, concerns=["performance"])
    [f] = [f for f in res["concerns"]["performance"] if f["rule"] == "io-in-loop"]
    assert f["delta"] == "introduced" and "base_had" not in f


def test_io_taken_out_of_a_loop_is_fixed(tmp_path):
    repo = _project(tmp_path, "delta", {"app/__init__.py": "", "app/sync.py": LOOP})
    _edit(repo, "app/sync.py", WRITE, "        print(r)\n")
    dif = _review(repo, concerns=["performance"])["differential"]
    [f] = [f for f in dif["fixed_findings"] if f["rule"] == "io-in-loop"]
    assert f["side"] == "base" and f["at"] == "app/sync.py:5"


def test_a_renamed_function_keeps_its_preexisting_operation(tmp_path):
    # the operation rule compares with the base's changed lines of the file: a rename pairs by file, and the
    # finding's text ("had ... too") and its delta agree
    repo = _project(tmp_path, "delta", {"app/__init__.py": "", "app/run.py": "import subprocess\n\n\n"
                                        "def launch(cmd):\n    return subprocess.run(cmd, shell=True)\n"})
    _edit(repo, "app/run.py", "def launch(cmd):\n    return subprocess.run(cmd, shell=True)\n",
          "def start(cmd):\n    out = subprocess.run(cmd, shell=True)\n    return out\n")
    res = _review(repo, concerns=["security"], findings="all")
    [f] = [f for f in res["concerns"]["security"] if f["rule"] == "op-on-changed-line"]
    assert "too" in f["finding"] and f["delta"] == "preexisting" and f["base_at"] == "app/run.py:5"
    assert not [g for g in res["differential"]["fixed_findings"] if g["rule"] == "op-on-changed-line"]
