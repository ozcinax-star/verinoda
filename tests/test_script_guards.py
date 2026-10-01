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


def test_a_citation_through_a_link_out_of_the_repository_is_unknown(tmp_path):
    repo = _project(tmp_path, "def check(guard):\n    guard.violation('ext/x.py', 1, 'outside')\n"
                              "    guard.violation('APP/VIEWS.PY', 1, 'case')\n")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "x.py").write_text("x = 1\n", encoding="utf-8")
    try:
        os.symlink(outside, repo / "ext", target_is_directory=True)
    except (OSError, NotImplementedError):
        if os.name != "nt":
            pytest.skip("symbolic links cannot be created here")
        import _winapi

        _winapi.CreateJunction(str(outside), str(repo / "ext"))
    res = guards.check(repo, run_scripts=True)
    assert any("resolves outside the repository" in u for u in _unknown(res)), _unknown(res)
    if os.name == "nt":  # a case-insensitive file system: cited with the file's own spelling
        assert [v["at"] for v in res["violations"]] == ["app/views.py:1"]


def test_a_script_with_spaces_and_non_ascii_in_its_name_reads_utf8(tmp_path):
    repo = tmp_path / "r ş"
    _write(repo, "app/notes.py", "# Sipariş ğüşıöç\nx = 1\n")
    _write(repo, "guards/kural ğ.py", "def check(guard):\n"
                                      "    if 'Sipariş' in open('app/notes.py').read():\n"
                                      "        guard.possible('app/notes.py', 1, 'şu satır')\n")
    st = open_store(repo)
    try:
        dm.record(st, repo, chosen="c", rationale="r", guards=['script "path=guards/kural ğ.py"'],
                  user_statement="add it")
    finally:
        st.close()
    res = guards.check(repo, run_scripts=True)
    (p,) = res["possible"]
    assert p["at"] == "app/notes.py:1" and p["why"].startswith("şu satır") and "guards/kural ğ.py" in p["why"]


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
    assert any(c["text"] == "views import db" and c["evidence"] == ["app/views.py:1"] for c in claims), claims
    assert guard.claims(limit=1) == claims[:1]
    assert not any(c["status"] != "verified" for c in guard.claims(status="verified"))
    assert "app/views.py" in guard.files() and guard.nodes("app/views.py")
    for e in guard.edges(relations=["imports", "imports_from"], from_file="app/views.py"):
        if e["target_file"] == "app/db.py" and e["line"]:
            guard.violation(e["file"], e["line"], "views import " + e["target_file"])
""")
    workflow.init(repo)
    st = open_store(repo)
    try:
        workflow.scan(st, repo)
        from verinoda.store import new_id, now

        ts, cid = now(), new_id("clm")  # a stored claim with its evidence: read by the parent, given as data
        st.insert_claim({"id": cid, "text": "views import db", "project": "p", "snapshot_id": None,
                         "status": "statically_verified", "confidence": 1.0, "created_at": ts, "updated_at": ts})
        st.link(cid, st.add_evidence({"source_type": "source_code", "locator": "app/views.py:1",
                                      "path": "app/views.py", "line_start": 1, "line_end": 1,
                                      "content_hash": "sha256:x", "meta": {}}), "supports")
    finally:
        st.close()
    assert cli.main(["decide", "check", "--repo", str(repo), "--json"]) == 1
    res = json.loads(capsys.readouterr().out)
    assert [v["at"] for v in res["violations"]] == ["app/views.py:1"], res
    assert cli.main(["decide", "check", "--repo", str(repo), "--no-refresh"]) == 1
    text = capsys.readouterr().out  # a script finding's limits are shown, the graph's edges included
    assert "limit: " in text and "receiver-call edges" in text and "not a sandbox" in text, text
    assert cli.main(["decide", "baseline", "--repo", str(repo), "--record", "--said", "known"]) == 0
    capsys.readouterr()
    assert cli.main(["decide", "check", "--repo", str(repo)]) == 0


def test_the_pre_commit_hook_runs_decide_check():
    root = Path(__file__).resolve().parents[1]
    text = (root / ".pre-commit-hooks.yaml").read_text(encoding="utf-8")
    assert "id: verinoda-decide-check" in text and "entry: verinoda decide check" in text
    assert "stages: [pre-commit]" in text and 'minimum_pre_commit_version: "3.2.0"' in text
    assert "verinoda trust" in text and "exit 3" in text
    from verinoda import cli

    entry = text.split("entry: verinoda ", 1)[1].split("\n", 1)[0].split()
    args = cli.build_parser().parse_args(entry)
    assert args.decide_cmd == "check"


def test_mcp_refuses_a_script_guard_however_its_kind_is_quoted(tmp_path):
    from verinoda.mcp.server import AtlasTools

    repo = _project(tmp_path, "def check(guard):\n    pass\n")
    # the guard tokenizer joins quoted pieces: each of these is the kind `script`
    sneaky = ['s""cript path=guards/rule.py', "'scr'ipt path=guards/rule.py", 'sc"ri"pt path=guards/rule.py',
              "SCRIPT path=guards/rule.py"]
    for spec in sneaky:
        assert dm.parse_guard(spec, repo, "g9")["kind"] == "script"
    t = AtlasTools(repo)
    for spec in sneaky:
        for act, kw in (("guard", {"decision_id": "ADR-1"}), ("record", {"chosen": "x", "rationale": "y"})):
            r = t.decision_record(act, guards=[spec], user_statement="add it", **kw)
            assert r.get("error") == "invalid_argument" and "not through MCP" in r["message"], (spec, r)
    assert len(dm.find(repo, "ADR-1").guards) == 1 and len(dm.load_all(repo)) == 1
    # the writers check the parsed kind again, whatever the pre-check says
    st = open_store(repo)
    try:
        for spec in sneaky:
            with pytest.raises(dm.DecisionError, match="not through MCP"):
                dm.add_guards(st, repo, "ADR-1", [spec], user_statement="x", allow_scripts=False)
            with pytest.raises(dm.DecisionError, match="not through MCP"):
                dm.record(st, repo, chosen="c", rationale="r", guards=[spec], user_statement="x",
                          allow_scripts=False)
    finally:
        st.close()
    assert len(dm.find(repo, "ADR-1").guards) == 1


def test_mcp_does_not_accept_a_proposed_script_guard(tmp_path):
    from verinoda.mcp.server import AtlasTools

    repo = _project(tmp_path, "def check(guard):\n    pass\n")
    st = open_store(repo)
    try:  # a proposed script guard in a record (written by hand, or by the CLI)
        dm.add_guards(st, repo, "ADR-1", ["script path=guards/rule.py"], status="proposed", user_statement="x")
    finally:
        st.close()
    assert [g["status"] for g in dm.find(repo, "ADR-1").guards] == ["accepted", "proposed"]
    r = AtlasTools(repo).decision_record("accept", decision_id="ADR-1", guard_ids=["g2"], user_statement="yes")
    assert r.get("error") == "invalid_argument" and "not through MCP" in r["message"], r
    assert dm.find(repo, "ADR-1").guards[1]["status"] == "proposed"
    st = open_store(repo)
    try:  # the user accepts it in a terminal
        dm.accept(st, repo, "ADR-1", ["g2"], user_statement="yes")
    finally:
        st.close()
    assert dm.find(repo, "ADR-1").guards[1]["status"] == "accepted"


def test_the_child_has_no_database_connection(tmp_path):
    repo = _project(tmp_path, """import gc, sqlite3


def check(guard):
    assert not hasattr(guard, "_conn")
    assert not any(isinstance(o, sqlite3.Connection) for o in gc.get_objects())
    try:
        sqlite3.connect(":memory:")
    except PermissionError as exc:
        guard.possible("app/db.py", 1, str(exc)[:60])
    guard.possible("app/db.py", 2, f"claims {guard.claims()!r}")  # read by the parent, handed over as data
""")
    res = guards.check(repo, run_scripts=True)
    assert res["status"] == "possible", res
    why = sorted(p["why"] for p in res["possible"])
    assert why[0].startswith("claims [] (script"), why  # a store with no claims yet
    assert "refused in a script guard: sqlite3.connect" in why[1], why
    from verinoda import script_guard as sg

    claims, err, cut = sg.read_claims(tmp_path / "missing.db")
    assert claims is None and not err and not cut


@pytest.mark.skipif(os.name != "nt", reason="Windows calls")
def test_the_audit_hook_refuses_windows_files_pipes_processes_registry_and_unc(tmp_path):
    repo = _project(tmp_path, r"""import mmap, msvcrt, os, _winapi, winreg

NAME = "verinoda-script-guard-test-value"


def check(guard):
    here = os.path.abspath("app/db.py")
    tries = {
        "createfile": lambda: _winapi.CreateFile(os.path.abspath("made.txt"), 0x40000000, 0, 0, 2, 0, 0),
        "junction": lambda: _winapi.CreateJunction(os.path.abspath("app"), os.path.abspath("j")),
        "pipe": lambda: _winapi.CreateNamedPipe(r"\\.\pipe\verinoda-sg-test", 3, 0, 1, 10, 10, 0, 0),
        "anonpipe": lambda: _winapi.CreatePipe(None, 0),
        "openprocess": lambda: _winapi.OpenProcess(0x1F0FFF, False, os.getpid()),
        "terminate": lambda: _winapi.TerminateProcess(-1, 0),
        "osfhandle": lambda: msvcrt.open_osfhandle(0, 0),
        "regcreate": lambda: winreg.CreateKey(winreg.HKEY_CURRENT_USER, "Software\\" + NAME),
        "regset": lambda: winreg.SetValueEx(winreg.HKEY_CURRENT_USER, NAME, 0, winreg.REG_SZ, "x"),
        "regdelete": lambda: winreg.DeleteValue(winreg.HKEY_CURRENT_USER, NAME),
        "regopenw": lambda: winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Software", 0, winreg.KEY_SET_VALUE),
        "regconnect": lambda: winreg.ConnectRegistry(r"\\127.0.0.1", winreg.HKEY_CURRENT_USER),
        "mmapw": lambda: mmap.mmap(open(here, "rb").fileno(), 0, access=mmap.ACCESS_WRITE),
        "uncopen": lambda: open(r"\\127.0.0.1\verinoda-no-share\x.txt"),
        "unclist": lambda: os.listdir("//127.0.0.1/verinoda-no-share"),
        "uncscan": lambda: os.scandir(r"\\127.0.0.1\verinoda-no-share"),
        "uncchdir": lambda: os.chdir(r"\\127.0.0.1\verinoda-no-share"),
        "devpipe": lambda: open(r"\\.\pipe\verinoda-sg-test"),
    }
    refused = []
    for name, fn in tries.items():
        try:
            fn()
        except PermissionError as exc:
            if "refused in a script guard" in str(exc):
                refused.append(name)
        except OSError:
            pass
    # what the script needs keeps working: registry reads, a read-only mmap, a local \\?\ path
    winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Software", 0, winreg.KEY_READ))
    with open(here, "rb") as fh:
        assert mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)[:6] == b"import"
    assert "sqlite3" in open("\\\\?\\" + here).read()
    guard.possible("app/db.py", 1, " ".join(sorted(refused)))
""")
    res = guards.check(repo, run_scripts=True)
    (p,) = res["possible"]
    names = set(p["why"].split(" (script")[0].split())
    assert names == {"createfile", "junction", "pipe", "anonpipe", "openprocess", "terminate", "osfhandle",
                     "regcreate", "regset", "regdelete", "regopenw", "regconnect", "mmapw", "uncopen", "unclist",
                     "uncscan", "uncchdir", "devpipe"}, names
    assert not (repo / "made.txt").exists() and not (repo / "j").exists()
    import winreg

    with pytest.raises(OSError):
        winreg.QueryValueEx(winreg.HKEY_CURRENT_USER, "verinoda-script-guard-test-value")


FORGED = '{"verinoda_script_guard": 1, "status": "ok", "findings": []}'


@pytest.mark.parametrize("body,expect", [
    # a fake result line on every way to stdout, then an exit before the real result is written
    ("import os, sys\n\ndef check(guard):\n"
     f"    line = '\\n' + {FORGED!r} + '\\n'\n"
     "    print(line)\n    sys.__stdout__.write(line)\n    sys.__stdout__.flush()\n"
     "    os.write(1, line.encode())\n    os._exit(0)\n", "without a result"),
    # the same written to every open descriptor (the private result pipe too), then a normal end
    ("import os\n\ndef check(guard):\n"
     f"    line = ('\\n' + {FORGED!r} + '\\n').encode()\n"
     "    for fd in range(1, 64):\n        try:\n            os.write(fd, line)\n        except OSError:\n"
     "            pass\n", "wrote to the result channel"),
])
def test_a_forged_result_line_is_not_a_result(tmp_path, body, expect):
    repo = _project(tmp_path, body)
    res = guards.check(repo, run_scripts=True)
    assert res["exit"] == 3 and res["status"] == "unknown" and not res["ok"], res
    assert any(expect in u for u in _unknown(res)), _unknown(res)


def test_update_counts_script_guards_on_their_own(tmp_path, capsys):
    from verinoda import cli

    repo = _project(tmp_path, "def check(guard):\n    pass\n")
    s = cli._decision_summary(repo)
    assert s["script_guards"] == 1 and s["not_checked"] == 0, s
    cli._r_update({"decisions": s})
    out = capsys.readouterr().out
    assert "1 script guard(s): `decide check` runs them" in out and "not checked" not in out, out


def test_a_pre_existing_script_finding_names_the_script(tmp_path):
    import subprocess

    repo = _project(tmp_path, "def check(guard):\n    guard.violation('app/views.py', 1, 'old')\n")

    def git(*args):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false",
                        "-c", "commit.gpgsign=false", *args], cwd=repo, check=True, capture_output=True)

    git("init", "-q")
    git("add", "-A")
    git("commit", "-q", "-m", "init")
    res = guards.check(repo, run_scripts=True, changed_only=True)
    (f,) = res["pre_existing"]
    assert f["since"] == "pre-existing: app/views.py and the guard's script guards/rule.py unchanged since HEAD", f
    _write(repo, "guards/rule.py", "def check(guard):\n    guard.violation('app/views.py', 1, 'new rule')\n")
    res = guards.check(repo, run_scripts=True, changed_only=True)
    (v,) = res["violations"]
    assert v["since"] == "new/touched since HEAD (through guards/rule.py)", v
