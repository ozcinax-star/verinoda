"""Script guards: a decision's guard written as a small Python program, run by `decide check` in a child process
with a read-only view of the graph and the claims (verinoda/script_guard.py)."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import json  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import decisions as dm  # noqa: E402
from verinoda import guards  # noqa: E402
from verinoda.store import open_store  # noqa: E402

VIEWS = "from app import db\n\n\ndef page():\n    return db.rows()\n"
DB = "import sqlite3\n\n\ndef rows():\n    return sqlite3.connect('x').execute('select 1').fetchall()\n"


def _write(repo: Path, rel: str, text: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8"))


def _project(tmp_path: Path, script: str, *, spec_extra: str = "") -> Path:
    # a folder name with a space and non-ASCII letters, as a Windows user folder often has
    repo = tmp_path / "proje ğüş dir"
    _write(repo, "app/__init__.py", "")
    _write(repo, "app/views.py", VIEWS)
    _write(repo, "app/db.py", DB)
    _write(repo, "guards/rule.py", script)
    st = open_store(repo)
    try:
        dm.record(st, repo, chosen="views never touch the database", rationale="r",
                  guards=[f"script path=guards/rule.py{spec_extra}"], user_statement="keep views away from the db")
    finally:
        st.close()
    return repo


def _unknown(res: dict) -> list[str]:
    return [u["why"] for u in res["unknown"] if u.get("kind") == "script"]


def test_a_script_guard_reports_a_violation_at_a_cited_line(tmp_path):
    repo = _project(tmp_path, "def check(guard):\n"
                              "    for i, line in enumerate(guard.read('app/views.py').splitlines(), 1):\n"
                              "        if 'import db' in line:\n"
                              "            guard.violation('app/views.py', i, 'views import the db module')\n")
    res = guards.check(repo, run_scripts=True)
    assert res["exit"] == 1 and res["status"] == "violated", res
    (v,) = res["violations"]
    assert v["at"] == "app/views.py:1" and v["kind"] == "script" and v["line"] == "from app import db"
    # the script's logic is not verified here: never statically_verified
    assert v["status"] == "strong_inference" and "script guards/rule.py" in v["why"]
    assert any("not a sandbox" in lim for lim in v["limits"])


def test_a_clean_script_is_ok_with_what_it_ran_and_its_limits(tmp_path):
    repo = _project(tmp_path, "def check(guard):\n    print('looked at', len(guard.read('app/db.py')))\n")
    res = guards.check(repo, run_scripts=True)
    assert res["exit"] == 0 and res["status"] == "ok", res
    (ok,) = res["ok"]
    assert ok["kind"] == "script" and ok["scope"] == {"script_runs": 1} and ok["what"] == "script guards/rule.py"
    assert any("printed: looked at" in lim for lim in ok["limits"])
    assert any("strong_inference" in lim for lim in ok["limits"])


@pytest.mark.parametrize("body,expect", [
    ("def check(guard):\n    x = 1\n    raise ValueError('boom')\n", "ValueError: boom (line 3 of guards/rule.py)"),
    ("import sys\n\ndef check(guard):\n    sys.exit(0)\n", "SystemExit"),
    ("def chekc(guard):\n    pass\n", "defines no check(guard) function"),
    ("def check(:\n", "SyntaxError"),
    ("import os\n\ndef check(guard):\n    os._exit(0)\n", "without a result"),
])
def test_a_script_that_fails_is_unknown_never_ok(tmp_path, body, expect):
    repo = _project(tmp_path, body)
    res = guards.check(repo, run_scripts=True)
    assert res["exit"] == 3 and res["status"] == "unknown" and not res["ok"], res
    assert any(expect in u for u in _unknown(res)), _unknown(res)


def test_a_script_that_runs_too_long_is_stopped_and_unknown(tmp_path):
    repo = _project(tmp_path, "def check(guard):\n    while True:\n        pass\n", spec_extra=" timeout=1")
    res = guards.check(repo, run_scripts=True)
    assert res["exit"] == 3 and not res["ok"]
    assert any("did not finish within 1 s" in u for u in _unknown(res)), _unknown(res)


def test_the_audit_hook_refuses_network_processes_native_code_and_writes(tmp_path):
    repo = _project(tmp_path, """import os, socket, subprocess, sys


def check(guard):
    tries = {
        "connect": lambda: socket.create_connection(("127.0.0.1", 9), timeout=1),
        "lookup": lambda: socket.getaddrinfo("example.com", 80),
        "process": lambda: subprocess.run([sys.executable, "-c", "pass"]),
        "system": lambda: os.system("echo hi"),
        "write": lambda: open("written.txt", "w"),
        "append": lambda: open("app/db.py", "a"),
        "remove": lambda: os.remove("app/db.py"),
        "mkdir": lambda: os.mkdir("newdir"),
        "ctypes": lambda: __import__("ctypes").CDLL("kernel32" if os.name == "nt" else None),
    }
    refused = []
    for name, fn in tries.items():
        try:
            fn()
        except Exception as exc:  # importing ctypes on Windows fails with AttributeError after the refusal
            if any("refused in a script guard" in str(e) for e in (exc, exc.__context__)):
                refused.append(name)
    seen = []
    sys.addaudithook(lambda *a: seen.append(a))  # not added (CPython drops a hook another hook refuses)
    open("app/db.py").close()
    if not seen:
        refused.append("hook")
    guard.possible("app/db.py", 1, " ".join(sorted(refused)))
    # reading stays allowed
    assert "sqlite3" in open("app/db.py").read()
""")
    res = guards.check(repo, run_scripts=True)
    (p,) = res["possible"]
    names = set(p["why"].split(" (script")[0].split())
    assert names == {"append", "connect", "ctypes", "hook", "lookup", "mkdir", "process", "remove", "system",
                     "write"}, names
    assert not (repo / "written.txt").exists() and not (repo / "newdir").exists()
    assert (repo / "app/db.py").read_text(encoding="utf-8") == DB
    assert p["status"] == "weak_inference"


def test_cited_lines_must_be_lines_of_repository_files(tmp_path):
    repo = _project(tmp_path, "def check(guard):\n"
                              "    guard.violation('../outside.py', 1, 'a')\n"
                              "    guard.violation('C:/Windows/win.ini', 1, 'b')\n"
                              "    guard.violation('app/views.py', 999, 'c')\n"
                              "    guard.violation('app/views.py', None, 'd')\n"
                              "    guard.violation('app/missing.py', 1, 'e')\n"
                              "    guard.violation('.\\\\app\\\\db.py', 5, 'f')\n")
    res = guards.check(repo, run_scripts=True)
    assert [v["at"] for v in res["violations"]] == ["app/db.py:5"]
    why = _unknown(res)
    assert len(why) == 5 and res["exit"] == 1
    assert any("not a path inside the repository" in w for w in why)
    assert any("the file has 5 line(s)" in w for w in why)
    assert any("without a line number" in w for w in why)
    assert any("not a readable file" in w for w in why)


def test_the_spec_and_the_script_path(tmp_path):
    repo = tmp_path / "r"
    _write(repo, "guards/rule.py", "def check(guard):\n    pass\n")
    g = dm.parse_guard("script path=guards/rule.py timeout=5", repo, "g1")
    assert g["path"] == "guards/rule.py" and g["timeout"] == 5 and g["kind"] == "script"
    assert dm.parse_guard("script path=guards\\rule.py", repo, "g1")["timeout"] == 60
    for bad in ("script path=../rule.py", "script path=C:/x/rule.py", "script path=guards/rule.txt",
                "script path=guards/none.py", "script path=guards/rule.py timeout=0",
                "script path=guards/rule.py timeout=601", "script path=guards/rule.py timeout=x", "script",
                "script path=guards/rule.py extra=1"):
        with pytest.raises(dm.DecisionError):
            dm.parse_guard(bad, repo, "g1")
    # a hand-edited record: the same rules when it is read
    with pytest.raises(dm.DecisionError):
        dm.validate_guard({"id": "g1", "kind": "script", "path": "/etc/rule.py"})
    with pytest.raises(dm.DecisionError):
        dm.validate_guard({"id": "g1", "kind": "script", "path": "guards/rule.py", "timeout": True})
    from verinoda import script_guard as sg

    assert sg.script_file(repo, "guards/rule.py")[0] == (repo / "guards/rule.py").resolve()
    assert sg.script_file(repo, "../r/guards/rule.py")[0] is None
    assert sg.script_file(repo, "guards/none.py") == (None, "script guards/none.py does not exist")


def test_a_linked_script_outside_the_repository_is_not_run(tmp_path):
    from verinoda import script_guard as sg

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "rule.py").write_text("def check(guard):\n    pass\n", encoding="utf-8")
    repo = tmp_path / "r"
    repo.mkdir()
    try:
        os.symlink(outside, repo / "guards", target_is_directory=True)
    except (OSError, NotImplementedError):
        if os.name != "nt":
            pytest.skip("symbolic links cannot be created here")
        import _winapi  # a junction needs no privilege on Windows

        _winapi.CreateJunction(str(outside), str(repo / "guards"))
    p, why = sg.script_file(repo, "guards/rule.py")
    assert p is None and "outside the repository" in why
    assert sg.run(repo, "guards/rule.py")["status"] == "error"


def test_not_run_without_the_cli_or_in_an_untrusted_project(tmp_path, monkeypatch):
    from verinoda.mcp.server import AtlasTools

    repo = _project(tmp_path, "def check(guard):\n    raise SystemExit('ran')\n")
    res = guards.check(repo)  # update's summary line, what-if, MCP: never run
    assert res["exit"] == 3 and any("only from the CLI" in u for u in _unknown(res))
    m = AtlasTools(repo).decision_check()
    assert m["exit"] == 3 and any("only from the CLI" in u["why"] for u in m["unknown"])
    monkeypatch.setenv("VERINODA_CONFIG_DIR", str(tmp_path / "fresh-user-dir"))
    res = guards.check(repo, run_scripts=True)
    assert res["exit"] == 3 and any("not trusted" in u and "verinoda trust" in u for u in _unknown(res))


def test_mcp_never_records_a_script_guard(tmp_path):
    from verinoda.mcp.server import AtlasTools

    repo = _project(tmp_path, "def check(guard):\n    pass\n")
    t = AtlasTools(repo)
    for act, kw in (("guard", {"decision_id": "ADR-1"}), ("record", {"chosen": "x", "rationale": "y"})):
        r = t.decision_record(act, guards=["script path=guards/rule.py"], user_statement="add it", **kw)
        assert r.get("error") == "invalid_argument" and "not through MCP" in r["message"], r
    assert len(dm.find(repo, "ADR-1").guards) == 1


def test_decide_check_runs_it_on_the_graph_and_claims(tmp_path, capsys):
    from verinoda import cli, workflow

    repo = _project(tmp_path, """def check(guard):
    claims = guard.claims()
    assert isinstance(claims, list)
    assert "app/views.py" in guard.files() and guard.nodes("app/views.py")
    for e in guard.edges(relations=["imports", "imports_from"], from_file="app/views.py"):
        if e["target_file"] == "app/db.py" and e["line"]:
            guard.violation(e["file"], e["line"], "views import " + e["target_file"])
""")
    workflow.init(repo)
    st = open_store(repo)
    try:
        workflow.scan(st, repo)
    finally:
        st.close()
    assert cli.main(["decide", "check", "--repo", str(repo), "--json"]) == 1
    res = json.loads(capsys.readouterr().out)
    assert [v["at"] for v in res["violations"]] == ["app/views.py:1"], res
    assert cli.main(["decide", "baseline", "--repo", str(repo), "--record", "--said", "known"]) == 0
    capsys.readouterr()
    assert cli.main(["decide", "check", "--repo", str(repo)]) == 0


def test_the_pre_commit_hook_runs_decide_check():
    root = Path(__file__).resolve().parents[1]
    text = (root / ".pre-commit-hooks.yaml").read_text(encoding="utf-8")
    assert "id: verinoda-decide-check" in text and "entry: verinoda decide check" in text
    from verinoda import cli

    entry = text.split("entry: verinoda ", 1)[1].split("\n", 1)[0].split()
    args = cli.build_parser().parse_args(entry)
    assert args.decide_cmd == "check"
