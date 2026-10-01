"""``verinoda check --checker``: the project's own tsc, pyright or mypy, run and read.

Every checker here is a fake written by the test: a small Python script behind a launcher (a ``.cmd`` file on
Windows, the script itself elsewhere) placed in ``node_modules/.bin`` or on PATH, printing output recorded in
the real tools' formats. Nothing is installed or downloaded.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from verinoda import cli, codecheck, precise
from verinoda import codecheck_external as cx

@pytest.fixture(autouse=True)
def trusted(monkeypatch):
    """The projects of these tests are trusted unless a test says otherwise (``untrust``)."""
    from verinoda import paths

    monkeypatch.setattr(paths, "is_trusted", lambda repo: True)


def untrust(monkeypatch):
    from verinoda import paths

    monkeypatch.setattr(paths, "is_trusted", lambda repo: False)


def fake(bin_dir: Path, name: str, body: str, monkeypatch) -> Path:
    """A fake checker ``name`` in ``bin_dir``: ``body`` is Python run with ``argv`` and ``root`` (the cwd)."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / f"_{name}_fake.py"
    script.write_text("import sys, os, json, time\nargv = sys.argv[1:]\nroot = os.getcwd()\n" + body + "\n",
                      encoding="utf-8")
    if os.name == "nt":
        monkeypatch.setenv("VERINODA_FAKE_PY", sys.executable)
        exe = bin_dir / f"{name}.cmd"
        exe.write_text(f'@"%VERINODA_FAKE_PY%" "%~dp0_{name}_fake.py" %*\r\n', encoding="ascii")
    else:
        exe = bin_dir / name
        exe.write_text(f"#!{sys.executable}\n" + script.read_text("utf-8"), encoding="utf-8")
        exe.chmod(0o755)
    return exe


def isolate_path(tmp_path: Path, monkeypatch) -> Path:
    """PATH holds only an empty folder (and git's, for diffs): no real checker can be found."""
    d = tmp_path / "pathbin"
    d.mkdir(exist_ok=True)
    git = subprocess.run(["where" if os.name == "nt" else "which", "git"], capture_output=True, text=True)
    dirs = [str(d)] + ([str(Path(git.stdout.splitlines()[0]).parent)] if git.returncode == 0 and git.stdout else [])
    monkeypatch.setenv("PATH", os.pathsep.join(dirs))
    return d


TS_SRC = """import { helper } from "./lib";

export function run(svc: Service) {
  svc.sendd("x");
  helper(1, 2);
  return parseInt(42);
}

interface Service { send(msg: string): void }
"""
LIB_SRC = "export function helper(n: number): number { return n; }\n"

TSC_BODY = r"""
if argv == ["--version"]:
    print("Version 5.4.5"); sys.exit(0)
assert "--noEmit" in argv and "--pretty" in argv and "-p" in argv, argv
print("src/app.ts(4,7): error TS2339: Property 'sendd' does not exist on type 'Service'.")
print("src/app.ts(5,13): error TS2554: Expected 1 arguments, but got 2.")
print("src/app.ts(6,19): error TS2345: Argument of type 'number' is not assignable to parameter of type 'string'.")
print("src/other.ts(1,1): error TS2304: Cannot find name 'nowhere'.")
print("src/app.ts(1,1): warning TS6133: 'x' is declared but its value is never read.")
for f in ("node_modules/typescript/lib/lib.d.ts", "src/lib.ts", "src/app.ts", "src/other.ts"):
    print(os.path.join(root, f).replace(os.sep, "/"))
sys.exit(2)
"""


def ts_project(tmp_path: Path) -> Path:
    proj = tmp_path / "web"
    (proj / "src").mkdir(parents=True)
    (proj / "tsconfig.json").write_text('{"compilerOptions": {"strict": true}, "include": ["src"]}\n', "utf-8")
    (proj / "src" / "app.ts").write_text(TS_SRC, "utf-8")
    (proj / "src" / "lib.ts").write_text(LIB_SRC, "utf-8")
    (proj / "src" / "other.ts").write_text("nowhere;\n", "utf-8")
    return proj


def test_tsc_reports_members_and_calls_and_the_file_counts_as_type_checked(tmp_path, monkeypatch):
    isolate_path(tmp_path, monkeypatch)
    proj = ts_project(tmp_path)
    fake(proj / "node_modules" / ".bin", "tsc", TSC_BODY, monkeypatch)
    res = codecheck.check(proj, ["src/app.ts", "src/lib.ts"], env="none", use_cache=False, checker="tsc")
    by = {s["at"]: s for s in res["sites"] if s.get("source") == "checker"}
    assert set(by) == {"src/app.ts:4:7", "src/app.ts:5:13", "src/app.ts:6:19"}   # other.ts is not in scope
    m = by["src/app.ts:4:7"]
    assert (m["kind"], m["verdict"], m["name"], m["status"]) == ("member", "absent", "sendd", "observed")
    assert m["checker"] == {"tool": "tsc", "version": "5.4.5", "config": "tsconfig.json", "code": "TS2339"}
    assert "TS2339" in m["message"] and "tsc 5.4.5" in m["message"]
    assert (by["src/app.ts:5:13"]["kind"], by["src/app.ts:5:13"]["verdict"]) == ("call", "mismatch")
    assert (by["src/app.ts:6:19"]["kind"], by["src/app.ts:6:19"]["verdict"]) == ("call", "mismatch")
    assert res["summary"]["mismatch"] == 2 and res["summary"]["absent"] >= 1
    assert res["exit"] == 3 and res["status"] == "absent"
    # the compiler read both files: neither stays under not_checked as "imports only"
    assert not [u for u in res.get("not_checked") or [] if u["path"].startswith("src/")]
    run = res["checker"][0]
    assert run["tool"] == "tsc" and run["status"] == "ran" and run["errors"] == 4 and run["warnings"] == 1
    assert run["exe"].startswith("node_modules/.bin/tsc") and run["config"] == "tsconfig.json"


def test_tsc_cli_text_and_sarif(tmp_path, monkeypatch, capsys):
    isolate_path(tmp_path, monkeypatch)
    proj = ts_project(tmp_path)
    fake(proj / "node_modules" / ".bin", "tsc", TSC_BODY, monkeypatch)
    rc = cli.main(["check", "src/app.ts", "--repo", str(proj), "--env", "none", "--no-cache", "--checker", "tsc"])
    out = capsys.readouterr().out
    assert rc == 3
    assert "2 mismatches" in out and "checker: tsc 5.4.5 - ran" in out
    assert "src/app.ts:4:7  ABSENT  member sendd" in out and "MISMATCH  call" in out
    rc = cli.main(["check", "src/app.ts", "--repo", str(proj), "--env", "none", "--no-cache", "--checker", "tsc",
                   "--sarif"])
    sarif = json.loads(capsys.readouterr().out)
    rules = {r["ruleId"] for r in sarif["runs"][0]["results"]}
    assert "check/mismatch/call" in rules and "check/absent/member" in rules


def test_diff_keeps_only_the_changed_lines(tmp_path, monkeypatch):
    isolate_path(tmp_path, monkeypatch)
    proj = ts_project(tmp_path)

    def git(*a):
        subprocess.run(["git", "-C", str(proj), "-c", "user.name=t", "-c", "user.email=t@t", *a], check=True,
                       capture_output=True)
    (proj / ".gitignore").write_text("node_modules/\n", "utf-8")
    git("init", "-q")
    git("add", "-A")
    git("commit", "-qm", "base")
    (proj / "src" / "app.ts").write_text(TS_SRC.replace("helper(1, 2);", "helper(1, 2); "), "utf-8")   # line 5
    fake(proj / "node_modules" / ".bin", "tsc", TSC_BODY, monkeypatch)
    res = codecheck.check(proj, None, diff="HEAD", env="none", use_cache=False, checker="tsc")
    got = [s["at"] for s in res["sites"] if s.get("source") == "checker"]
    assert got == ["src/app.ts:5:13"]


def test_tsc_not_installed_is_never_a_pass(tmp_path, monkeypatch):
    isolate_path(tmp_path, monkeypatch)
    proj = ts_project(tmp_path)
    res = codecheck.check(proj, ["src/lib.ts"], env="none", use_cache=False, checker="auto")
    run = res["checker"][0]
    assert run["status"] == "not_found" and "npm install --save-dev typescript" in run["next_step"]
    assert res["exit"] == 4 and any("tsc" in n and "not found" in n for n in res["incomplete"])
    assert [u["path"] for u in res["not_checked"]] == ["src/lib.ts"]   # still imports only


def test_no_tsconfig_is_said_not_guessed(tmp_path, monkeypatch):
    isolate_path(tmp_path, monkeypatch)
    proj = tmp_path / "bare"
    proj.mkdir()
    (proj / "a.ts").write_text(LIB_SRC, "utf-8")
    res = codecheck.check(proj, ["a.ts"], env="none", use_cache=False, checker="tsc")
    assert res["exit"] == 4 and any("no tsconfig.json" in n for n in res["incomplete"])
    assert res["checker"] == []


def test_timeout_stops_the_checker(tmp_path, monkeypatch):
    isolate_path(tmp_path, monkeypatch)
    proj = ts_project(tmp_path)
    fake(proj / "node_modules" / ".bin", "tsc",
         'if argv == ["--version"]:\n    print("Version 5.4.5"); sys.exit(0)\ntime.sleep(60)', monkeypatch)
    t0 = time.perf_counter()
    res = codecheck.check(proj, ["src/app.ts"], env="none", use_cache=False, checker="tsc", checker_timeout=1.5)
    assert time.perf_counter() - t0 < 30
    run = res["checker"][0]
    assert run["status"] == "timeout" and "1.5 s" in run["note"] and res["exit"] == 4
    assert not [s for s in res["sites"] if s.get("source") == "checker"]


def test_malformed_tsc_output_fails_loudly(tmp_path, monkeypatch):
    isolate_path(tmp_path, monkeypatch)
    proj = ts_project(tmp_path)
    fake(proj / "node_modules" / ".bin", "tsc",
         'if argv == ["--version"]:\n    print("Version 5.4.5"); sys.exit(0)\n'
         'print("Segmentation fault?")\nsys.exit(1)',
         monkeypatch)
    res = codecheck.check(proj, ["src/app.ts"], env="none", use_cache=False, checker="tsc")
    run = res["checker"][0]
    assert run["status"] == "failed" and "not understood" in run["note"] and res["exit"] == 4
    assert [u["path"] for u in res["not_checked"]] == ["src/app.ts"]


def test_tsc_config_error_is_a_failure(tmp_path, monkeypatch):
    isolate_path(tmp_path, monkeypatch)
    proj = ts_project(tmp_path)
    fake(proj / "node_modules" / ".bin", "tsc",
         'if argv == ["--version"]:\n    print("Version 5.4.5"); sys.exit(0)\n'
         'print("error TS18003: No inputs were found in config file \'tsconfig.json\'.")\nsys.exit(2)', monkeypatch)
    res = codecheck.check(proj, ["src/app.ts"], env="none", use_cache=False, checker="tsc")
    assert res["checker"][0]["status"] == "failed" and "TS18003" in res["checker"][0]["note"]


PY_SRC = "import json\n\n\ndef go(svc):\n    svc.sendd('x')\n    return json.loads('1', 2)\n"

PYRIGHT_BODY = r"""
if argv == ["--version"]:
    print("pyright 1.1.380"); sys.exit(0)
assert argv[0] == "--outputjson", argv
f = os.path.join(root, "pkg", "mod.py")
diags = [
  {"file": f, "severity": "error", "message": 'Cannot access attribute "sendd" for class "Service"',
   "range": {"start": {"line": 4, "character": 8}, "end": {"line": 4, "character": 13}},
   "rule": "reportAttributeAccessIssue"},
  {"file": f, "severity": "error", "message": 'Expected 1 positional argument',
   "range": {"start": {"line": 5, "character": 27}, "end": {"line": 5, "character": 28}}, "rule": "reportCallIssue"},
  {"file": f, "severity": "warning", "message": 'Import "x" is not accessed',
   "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}}},
  {"file": os.path.join(root, "elsewhere.py"), "severity": "error", "message": '"z" is not defined',
   "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}},
   "rule": "reportUndefinedVariable"},
]
print(json.dumps({"version": "1.1.380", "time": "0", "generalDiagnostics": diags,
                  "summary": {"filesAnalyzed": 1, "errorCount": 3, "warningCount": 1}}))
sys.exit(1)
"""


def py_project(tmp_path: Path) -> Path:
    proj = tmp_path / "py"
    (proj / "pkg").mkdir(parents=True)
    (proj / "pkg" / "__init__.py").write_text("", "utf-8")
    (proj / "pkg" / "mod.py").write_text(PY_SRC, "utf-8")
    return proj


def test_pyright_json(tmp_path, monkeypatch):
    isolate_path(tmp_path, monkeypatch)
    proj = py_project(tmp_path)
    (proj / "pyrightconfig.json").write_text("{}\n", "utf-8")
    fake(proj / "node_modules" / ".bin", "pyright", PYRIGHT_BODY, monkeypatch)
    res = codecheck.check(proj, ["pkg/mod.py"], env="none", use_cache=False, checker="auto")
    by = {s["at"]: s for s in res["sites"] if s.get("source") == "checker"}
    assert set(by) == {"pkg/mod.py:5:9", "pkg/mod.py:6:28"}
    assert (by["pkg/mod.py:5:9"]["kind"], by["pkg/mod.py:5:9"]["verdict"], by["pkg/mod.py:5:9"]["name"]) == \
        ("member", "absent", "sendd")
    assert by["pkg/mod.py:6:28"]["kind"] == "call" and by["pkg/mod.py:6:28"]["verdict"] == "mismatch"
    run = res["checker"][0]
    assert (run["tool"], run["version"], run["config"], run["errors"], run["warnings"]) == \
        ("pyright", "1.1.380", "pyrightconfig.json", 3, 1)
    assert res["exit"] == 3


def test_pyright_malformed_json(tmp_path, monkeypatch):
    isolate_path(tmp_path, monkeypatch)
    proj = py_project(tmp_path)
    fake(proj / "node_modules" / ".bin", "pyright",
         'if argv == ["--version"]:\n    print("pyright 1.1.380"); sys.exit(0)\nprint("{not json")\nsys.exit(1)',
         monkeypatch)
    res = codecheck.check(proj, ["pkg/mod.py"], env="none", use_cache=False, checker="pyright")
    assert res["checker"][0]["status"] == "failed" and res["exit"] == 4
    assert any("not checked by pyright" in n for n in res["incomplete"])


MYPY_BODY = r"""
ver = os.environ.get("FAKE_MYPY_VERSION", "1.11.2")
if argv == ["--version"]:
    print(f"mypy {ver} (compiled: yes)"); sys.exit(0)
rows = [("pkg/mod.py", 5, 4, "error", '"Service" has no attribute "sendd"', "attr-defined"),
        ("pkg/mod.py", 6, 11, "error", 'Too many arguments for "loads"', "call-arg"),
        ("pkg/mod.py", 6, 11, "note", '"loads" defined here', None),
        ("pkg/mod.py", 1, 0, "error", 'Library stubs not installed for "yaml"', "import-untyped")]
if "-O" in argv:
    assert argv[argv.index("-O") + 1] == "json"
    for f, l, c, sev, msg, code in rows:
        print(json.dumps({"file": f, "line": l, "column": c, "message": msg, "hint": None, "code": code,
                          "severity": sev}))
else:
    assert "--show-column-numbers" in argv
    for f, l, c, sev, msg, code in rows:
        print(f"{f}:{l}:{c + 1}: {sev}: {msg}" + (f"  [{code}]" if code else ""))
sys.exit(1)
"""


@pytest.mark.parametrize("ver", ["1.11.2", "1.5.1"])
def test_mypy_json_and_text(tmp_path, monkeypatch, ver):
    pathbin = isolate_path(tmp_path, monkeypatch)
    proj = py_project(tmp_path)
    (proj / "mypy.ini").write_text("[mypy]\n", "utf-8")
    monkeypatch.setenv("FAKE_MYPY_VERSION", ver)
    fake(pathbin, "mypy", MYPY_BODY, monkeypatch)
    fake(proj / "node_modules" / ".bin", "pyright", PYRIGHT_BODY, monkeypatch)   # installed, not configured
    res = codecheck.check(proj, ["pkg/mod.py"], env="none", use_cache=False, checker="auto")
    run = res["checker"][0]
    assert (run["tool"], run["version"], run["config"]) == ("mypy", ver, "mypy.ini")
    by = {s["at"]: s for s in res["sites"] if s.get("source") == "checker"}
    assert set(by) == {"pkg/mod.py:5:5", "pkg/mod.py:6:12", "pkg/mod.py:1:1"}
    assert (by["pkg/mod.py:5:5"]["kind"], by["pkg/mod.py:5:5"]["name"]) == ("member", "sendd")
    call = by["pkg/mod.py:6:12"]
    assert (call["kind"], call["verdict"], call["name"]) == ("call", "mismatch", "loads")
    assert "defined here" in call["message"] and call["checker"]["code"] == "call-arg"
    assert by["pkg/mod.py:1:1"]["verdict"] == "unknown"   # stubs missing: the module is there


def test_mypy_not_found(tmp_path, monkeypatch):
    isolate_path(tmp_path, monkeypatch)
    proj = py_project(tmp_path)
    res = codecheck.check(proj, ["pkg/mod.py"], env="none", use_cache=False, checker="mypy")
    run = res["checker"][0]
    assert run["status"] == "not_found" and "pip install mypy" in run["next_step"]
    assert res["exit"] == 4 and "not checked by mypy" in res["exit_because"]


def test_mypy_confirms_an_absent_name(tmp_path, monkeypatch):
    pathbin = isolate_path(tmp_path, monkeypatch)
    proj = tmp_path / "c"
    proj.mkdir()
    (proj / "use.py").write_text("import json\n\njson.lodas('1')\n", "utf-8")
    fake(pathbin, "mypy", 'if argv == ["--version"]:\n    print("mypy 1.11.2"); sys.exit(0)\n'
                          'print(json.dumps({"file": "use.py", "line": 3, "column": 0, "message": '
                          '\'Module has no attribute "lodas"\', "hint": None, "code": "attr-defined", '
                          '"severity": "error"}))\nsys.exit(1)', monkeypatch)
    res = codecheck.check(proj, ["use.py"], env="none", use_cache=False, checker="mypy", include_exists=True)
    on3 = [s for s in res["sites"] if s["line"] == 3 and s["verdict"] == "absent"]
    assert len(on3) == 1
    if precise.available()[0]:   # Verinoda's own absent site, confirmed instead of listed twice
        assert on3[0].get("source") != "checker" and on3[0]["confirmed_by"] == "mypy 1.11.2 attr-defined"
    else:   # without jedi the checker's site stands alone
        assert on3[0]["kind"] == "member" and on3[0]["source"] == "checker"


def test_snippet_is_not_given_to_a_checker(tmp_path, monkeypatch):
    isolate_path(tmp_path, monkeypatch)
    proj = py_project(tmp_path)
    res = codecheck.check(proj, snippet="import json\n", as_path="pkg/new.py", env="none", use_cache=False,
                          checker="auto")
    assert "checker" not in res and res["exit"] == 4
    assert any("snippet is not given" in n for n in res["incomplete"])


def test_pip_pyright_wrapper_is_not_started(tmp_path, monkeypatch):
    isolate_path(tmp_path, monkeypatch)
    proj = py_project(tmp_path)
    sub = "Scripts" if os.name == "nt" else "bin"
    w = proj / ".venv" / sub / ("pyright.exe" if os.name == "nt" else "pyright")
    w.parent.mkdir(parents=True)
    w.write_bytes(b"#!python\nimport re, sys\nfrom pyright.cli import entrypoint\nsys.exit(entrypoint())\n")
    w.chmod(0o755)
    exe, why = cx.find("pyright", proj, proj, "auto")
    assert exe is None and "PyPI wrapper" in why


def test_parsers_and_classification(tmp_path):
    out = ("src/a.ts(3,5): error TS2322: Type 'string' is not assignable to type 'number'.\n"
           "  The expected type comes from property 'n'.\n"
           "src/a.ts(4,1): error TS2305: Module '\"./b\"' has no exported member 'gone'.\n"
           "something unexpected\n"
           "C:/x/node_modules/typescript/lib/lib.es5.d.ts\n")
    diags, glob, program, unparsed = cx.parse_tsc(out, tmp_path)
    assert len(diags) == 2 and "expected type comes from" in diags[0].message and unparsed == 1 and len(program) == 1
    assert cx.classify("tsc", "TS2322", diags[0].message) == "type"
    assert cx.classify("tsc", "TS2305", diags[1].message) == "import"
    assert cx._name_of("import", diags[1].message) == "gone"
    typo = "Property 'lenght' does not exist on type 'string'. Did you mean 'length'?"
    assert cx.classify("tsc", "TS2551", typo) == "member" and cx._name_of("member", typo) == "lenght"
    assert cx.classify("mypy", "name-defined", 'Name "x" is not defined') == "name"
    assert cx.classify("mypy", "attr-defined", 'Module "os" has no attribute "x"') == "import"
    assert cx.classify("pyright", "", '"foo" is not defined') == "name"
    assert cx.verdict_of("tsc", "import", "TS2307", "Cannot find module 'left-pad' or its corresponding type "
                       "declarations.")[0] == "not_installed"
    assert cx.verdict_of("tsc", "import", "TS2307", "Cannot find module './nope'.")[0] == "absent"
    with pytest.raises(ValueError):
        cx.parse_pyright("no json here", tmp_path)
    d, n = cx.parse_mypy_text("a.py:1: error: Bad  [misc]\nSuccess: no issues found in 1 source file\ngarbage\n",
                              tmp_path)
    assert len(d) == 1 and d[0].col == 1 and n == 1


def test_cli_argument_errors(tmp_path):
    proj = tmp_path / "p"
    proj.mkdir()
    with pytest.raises(SystemExit):
        cli.main(["check", "--repo", str(proj), "--checker", "bogus"])
    with pytest.raises(SystemExit, match="--checker-timeout goes with --checker"):
        cli.main(["check", ".", "--repo", str(proj), "--checker-timeout", "5"])
    with pytest.raises(SystemExit, match="--deps"):
        cli.main(["check", "--deps", "--repo", str(proj), "--checker", "mypy"])


def test_output_lines_not_understood_are_never_a_pass(tmp_path, monkeypatch):
    pathbin = isolate_path(tmp_path, monkeypatch)
    proj = py_project(tmp_path)
    fake(pathbin, "mypy", 'if argv == ["--version"]:\n    print("mypy 1.5.1"); sys.exit(0)\n'
                          'print("pkg/mod.py(5) something in a new format")\nsys.exit(0)', monkeypatch)
    res = codecheck.check(proj, ["pkg/mod.py"], env="none", use_cache=False, checker="mypy")
    assert res["checker"][0]["status"] == "ran" and res["checker"][0]["unparsed_lines"] == 1
    assert res["exit"] == 4 and any("not understood" in n for n in res["incomplete"])


# -- review round: regression tests ------------------------------------------------------------------------

MARK_BODY = 'open(os.path.join(os.environ["FAKE_MARK_DIR"], "ran-" + os.path.basename(sys.argv[0])), "w").close()\n'


def marks(tmp_path: Path, monkeypatch) -> Path:
    d = tmp_path / "marks"
    d.mkdir(exist_ok=True)
    monkeypatch.setenv("FAKE_MARK_DIR", str(d))
    return d


def test_untrusted_project_programs_are_not_started(tmp_path, monkeypatch):
    untrust(monkeypatch)
    isolate_path(tmp_path, monkeypatch)
    m = marks(tmp_path, monkeypatch)
    proj = ts_project(tmp_path)
    fake(proj / "node_modules" / ".bin", "tsc", MARK_BODY + TSC_BODY, monkeypatch)
    sub = "Scripts" if os.name == "nt" else "bin"
    (proj / "pkg").mkdir()
    (proj / "pkg" / "mod.py").write_text(PY_SRC, "utf-8")
    fake(proj / ".venv" / sub, "mypy", MARK_BODY + MYPY_BODY, monkeypatch)
    res = codecheck.check(proj, ["src/app.ts", "pkg/mod.py"], env="none", use_cache=False, checker="auto")
    assert not list(m.iterdir())   # nothing the repository supplies ran
    runs = {r["tool"]: r for r in res["checker"]}
    assert runs["tsc"]["status"] == "not_run" and "not trusted" in runs["tsc"]["note"]
    assert "verinoda trust" in runs["tsc"]["next_step"] and "never run it for them" in runs["tsc"]["next_step"]
    py = [r for r in res["checker"] if r["tool"] != "tsc"][0]
    assert py["status"] == "not_run" and "verinoda trust" in py["next_step"]
    assert res["exit"] == 4 and not [s for s in res["sites"] if s.get("source") == "checker"]


def test_untrusted_mypy_with_plugins_and_pyright_are_not_run(tmp_path, monkeypatch):
    untrust(monkeypatch)
    pathbin = isolate_path(tmp_path, monkeypatch)
    m = marks(tmp_path, monkeypatch)
    proj = py_project(tmp_path)
    fake(pathbin, "mypy", MARK_BODY + MYPY_BODY, monkeypatch)
    fake(pathbin, "pyright", MARK_BODY + PYRIGHT_BODY, monkeypatch)
    (proj / "mypy.ini").write_text("[mypy]\nplugins = evil_plugin\n", "utf-8")
    res = codecheck.check(proj, ["pkg/mod.py"], env="none", use_cache=False, checker="mypy")
    assert res["checker"][0]["status"] == "not_run" and "mypy.ini sets plugins" in res["checker"][0]["note"]
    res = codecheck.check(proj, ["pkg/mod.py"], env="none", use_cache=False, checker="pyright")
    assert res["checker"][0]["status"] == "not_run" and "not trusted" in res["checker"][0]["note"]
    assert not list(m.iterdir())
    # without code-running settings, a mypy on PATH (the user's own) runs in an untrusted project
    (proj / "mypy.ini").write_text("[mypy]\nstrict = True\n", "utf-8")
    res = codecheck.check(proj, ["pkg/mod.py"], env="none", use_cache=False, checker="auto")
    assert res["checker"][0]["tool"] == "mypy" and res["checker"][0]["status"] == "ran"


def test_a_file_named_like_an_option_stays_a_file(tmp_path, monkeypatch):
    pathbin = isolate_path(tmp_path, monkeypatch)
    proj = tmp_path / "opt"
    proj.mkdir()
    (proj / "--python-executable=evil.py").write_text("x = 1\n", "utf-8")
    log = tmp_path / "argv.json"
    monkeypatch.setenv("FAKE_ARGV_LOG", str(log))
    fake(pathbin, "mypy", 'if argv == ["--version"]:\n    print("mypy 1.11.2"); sys.exit(0)\n'
                          'json.dump(argv, open(os.environ["FAKE_ARGV_LOG"], "w"))\nsys.exit(0)', monkeypatch)
    res = codecheck.check(proj, [str(proj / "--python-executable=evil.py")], env="none", use_cache=False,
                          checker="mypy")
    argv = json.loads(log.read_text("utf-8"))
    assert argv[-2:] == ["--", "./--python-executable=evil.py"]
    assert not any(a.startswith("--python-executable") for a in argv)
    assert res["checker"][0]["status"] == "ran"


def test_mypy_blocking_error_is_not_a_complete_run(tmp_path, monkeypatch):
    pathbin = isolate_path(tmp_path, monkeypatch)
    proj = py_project(tmp_path)
    fake(pathbin, "mypy", 'if argv == ["--version"]:\n    print("mypy 1.5.1"); sys.exit(0)\n'
                          'print("pkg/other.py:1: error: invalid syntax  [syntax]")\nsys.exit(2)', monkeypatch)
    res = codecheck.check(proj, ["pkg/mod.py"], env="none", use_cache=False, checker="mypy")
    run = res["checker"][0]
    assert run["status"] == "failed" and "blocking error" in run["note"] and "pkg/other.py:1" in run["note"]
    assert res["exit"] == 4 and res["status"] != "checked"


def test_tsc_syntax_error_leaves_the_file_unchecked(tmp_path, monkeypatch):
    isolate_path(tmp_path, monkeypatch)
    proj = ts_project(tmp_path)
    fake(proj / "node_modules" / ".bin", "tsc",
         'if argv == ["--version"]:\n    print("Version 5.4.5"); sys.exit(0)\n'
         'print("src/lib.ts(9,1): error TS1005: \'}\' expected.")\n'
         'print(os.path.join(root, "src/lib.ts").replace(os.sep, "/"))\nsys.exit(2)', monkeypatch)
    res = codecheck.check(proj, ["src/lib.ts"], env="none", use_cache=False, checker="tsc")
    assert not [s for s in res["sites"] if s.get("source") == "checker"]
    assert [u["path"] for u in res["not_checked"]] == ["src/lib.ts"]
    assert res["exit"] == 4 and any("TS1005" in n and "syntax error" in n for n in res["incomplete"])


def test_type_checked_reads_tsconfig_as_jsonc_and_ts_nocheck(tmp_path):
    cfg = tmp_path / "tsconfig.json"
    js = tmp_path / "a.js"
    js.write_text("export const a = 1;\n", "utf-8")
    ts = tmp_path / "b.ts"
    ts.write_text("// @ts-nocheck\nexport const b: number = 'x';\n", "utf-8")
    cfg.write_text('{\n  "compilerOptions": {\n    // "checkJs": true,\n    "allowJs": true,\n  }\n}\n', "utf-8")
    assert not cx._type_checked(js, cfg)
    cfg.write_text('{"compilerOptions": {/* "checkJs": true */ "checkJs": false}}', "utf-8")
    assert not cx._type_checked(js, cfg)
    cfg.write_text('{"compilerOptions": {"checkJs": true, "outDir": "http://x//y"}}', "utf-8")
    assert cx._type_checked(js, cfg)
    assert not cx._type_checked(ts, cfg)   # @ts-nocheck: a TypeScript file is not checked either


def test_ts_nocheck_file_stays_imports_only(tmp_path, monkeypatch):
    isolate_path(tmp_path, monkeypatch)
    proj = ts_project(tmp_path)
    (proj / "src" / "lib.ts").write_text("// @ts-nocheck\n" + LIB_SRC, "utf-8")
    fake(proj / "node_modules" / ".bin", "tsc", TSC_BODY, monkeypatch)
    res = codecheck.check(proj, ["src/lib.ts"], env="none", use_cache=False, checker="tsc")
    assert [u["path"] for u in res["not_checked"]] == ["src/lib.ts"]


def test_one_site_per_import_finding(tmp_path, monkeypatch):
    isolate_path(tmp_path, monkeypatch)
    proj = ts_project(tmp_path)
    (proj / "src" / "app.ts").write_text('import { x } from "./nope";\nexport const y = x;\n', "utf-8")
    fake(proj / "node_modules" / ".bin", "tsc",
         'if argv == ["--version"]:\n    print("Version 5.4.5"); sys.exit(0)\n'
         'print("src/app.ts(1,19): error TS2307: Cannot find module \'./nope\' or its corresponding type '
         'declarations.")\nprint(os.path.join(root, "src/app.ts").replace(os.sep, "/"))\nsys.exit(2)', monkeypatch)
    res = codecheck.check(proj, ["src/app.ts"], env="none", use_cache=False, checker="tsc", include_exists=True)
    on1 = [s for s in res["sites"] if s["line"] == 1 and s["kind"] == "import" and s["name"] == "./nope"]
    assert len(on1) == 1 and on1[0]["confirmed_by"] == "tsc 5.4.5 TS2307" and on1[0].get("source") != "checker"
    assert res["summary"]["absent"] == sum(1 for s in res["sites"] if s["verdict"] == "absent")


def test_one_site_per_python_import_finding(tmp_path, monkeypatch):
    pathbin = isolate_path(tmp_path, monkeypatch)
    proj = tmp_path / "y"
    proj.mkdir()
    (proj / "use.py").write_text("import yaml\n", "utf-8")
    fake(pathbin, "mypy", 'if argv == ["--version"]:\n    print("mypy 1.11.2"); sys.exit(0)\n'
                          'print(json.dumps({"file": "use.py", "line": 1, "column": 0, "message": '
                          '\'Library stubs not installed for "yaml"\', "hint": None, "code": "import-untyped", '
                          '"severity": "error"}))\nsys.exit(1)', monkeypatch)
    res = codecheck.check(proj, ["use.py"], env="none", use_cache=False, checker="mypy", include_exists=True)
    on1 = [s for s in res["sites"] if s["line"] == 1]
    assert len(on1) == 1
    if precise.available()[0]:
        assert on1[0]["verdict"] == "not_installed" and "unknown (mypy 1.11.2 import-untyped)" in on1[0]["checker_says"]


def test_verdicts_of_review_cases(tmp_path):
    cfg = tmp_path / "tsconfig.json"
    cfg.write_text('{"compilerOptions": {"baseUrl": ".", "paths": {"@/*": ["src/*"]}}}', "utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "here.ts").write_text("export {}\n", "utf-8")
    v = cx.verdict_of
    assert cx.classify("tsc", "TS7016", "Could not find a declaration file for module 'x'.") == "import"
    assert v("tsc", "import", "TS7016", "Could not find a declaration file for module 'x'.")[0] == "unknown"
    assert v("tsc", "import", "TS2307", "Cannot find module '@/missing'.", cfg=cfg)[0] == "absent"
    assert v("tsc", "import", "TS2307", "Cannot find module '@/here'.", cfg=cfg)[0] == "unknown"
    assert v("tsc", "import", "TS2307", "Cannot find module 'lodash'.", cfg=cfg)[0] == "unknown"   # baseUrl
    msg = "Cannot find name 'require'. Do you need to install type definitions for node? Try `npm i --save-dev " \
          "@types/node`."
    assert cx.classify("tsc", "TS2580", msg) == "name"
    verdict, why = v("tsc", "name", "TS2580", msg)
    assert verdict == "not_installed" and "@types/node" in why
    (tmp_path / "proj").mkdir()
    (tmp_path / "proj" / "app").mkdir()
    (tmp_path / "proj" / "app" / "__init__.py").write_text("", "utf-8")
    (tmp_path / "proj" / "app" / "real.py").write_text("", "utf-8")
    m = 'Cannot find implementation or library stub for module named "app.gone"'
    assert v("mypy", "import", "import-not-found", m, repo=tmp_path / "proj")[0] == "absent"
    m = 'Cannot find implementation or library stub for module named "app.real"'
    assert v("mypy", "import", "import-not-found", m, repo=tmp_path / "proj")[0] == "unknown"
    m = 'Cannot find implementation or library stub for module named "requests"'
    assert v("mypy", "import", "import-not-found", m, repo=tmp_path / "proj")[0] == "not_installed"
    assert cx.classify("mypy", "union-attr", 'Item "None" of "A | None" has no attribute "x"') == "type"
    assert cx.classify("pyright", "reportOptionalMemberAccess", '"x" is not a known attribute of "None"') == "type"
    assert cx.classify("pyright", "reportAttributeAccessIssue",
                       'Cannot assign to attribute "x" for class "A*"\n  Attribute "x" has no defined setter') == "type"


def test_checker_not_installed_import_is_not_an_exit_3(tmp_path, monkeypatch):
    pathbin = isolate_path(tmp_path, monkeypatch)
    proj = tmp_path / "ni"
    proj.mkdir()
    (proj / "use.py").write_text("x = 1\n\n\ny = 2\n", "utf-8")
    fake(pathbin, "mypy", 'if argv == ["--version"]:\n    print("mypy 1.11.2"); sys.exit(0)\n'
                          'for l in (1, 4):\n'
                          '    print(json.dumps({"file": "use.py", "line": l, "column": 0, "message": '
                          '\'Cannot find implementation or library stub for module named "requests"\', '
                          '"hint": None, "code": "import-not-found", "severity": "error"}))\nsys.exit(1)', monkeypatch)
    res = codecheck.check(proj, ["use.py"], env="none", use_cache=False, checker="mypy")
    assert res["summary"]["not_installed"] == 2 and res["exit"] == 0   # like Verinoda's own not_installed


def test_sarif_levels_of_checker_sites():
    from verinoda import sarif

    def site(v):
        return {"at": "a.py:1:1", "path": "a.py", "line": 1, "col": 1, "kind": "import", "expr": "x", "verdict": v,
                "source": "checker", "message": "m"}
    log = sarif.from_check({"sites": [site("not_installed"), site("absent"), site("mismatch")], "exit": 3})
    levels = {r["ruleId"].split("/")[1]: r["level"] for r in log["runs"][0]["results"]}
    assert levels == {"not_installed": "warning", "absent": "error", "mismatch": "error"}


@pytest.mark.skipif(os.name != "nt", reason="cmd.exe metacharacters matter for .cmd launchers")
def test_cmd_metacharacters_in_the_launcher_path_are_refused(tmp_path, monkeypatch):
    isolate_path(tmp_path, monkeypatch)
    m = marks(tmp_path, monkeypatch)
    base = tmp_path / "a&b"
    base.mkdir()
    proj = ts_project(base)
    fake(proj / "node_modules" / ".bin", "tsc", MARK_BODY + TSC_BODY, monkeypatch)
    res = codecheck.check(proj, ["src/app.ts"], env="none", use_cache=False, checker="tsc")
    run = res["checker"][0]
    assert run["status"] == "not_run" and "cmd.exe" in run["note"] and res["exit"] == 4
    assert not list(m.iterdir())


@pytest.mark.parametrize("value", ["inf", "nan", "0", "-1"])
def test_checker_timeout_must_be_finite_and_positive(tmp_path, value):
    proj = tmp_path / "p"
    proj.mkdir()
    with pytest.raises(SystemExit, match="--checker-timeout"):
        cli.main(["check", ".", "--repo", str(proj), "--checker", "mypy", "--checker-timeout", value])
    with pytest.raises(ValueError):
        cx.run_checkers(proj, "mypy", [], [], timeout=float(value))


def test_minor_review_fixes(tmp_path, monkeypatch):
    from verinoda import codecheck_rank

    a = {"verdict": "mismatch", "path": "b", "line": 1, "col": 1}
    b = {"verdict": "not_installed", "path": "a", "line": 1, "col": 1}
    assert codecheck_rank.order_key(a) < codecheck_rank.order_key(b)
    # mypy: context lines are not "not understood"; a note on another line is not joined to the error
    d, n = cx.parse_mypy_text('pkg/a.py: note: In function "go":\npkg/a.py:3:1: error: Bad  [misc]\n'
                              'pkg/a.py:9:1: note: unrelated\npkg/a.py:3:1: note: about line 3\n', tmp_path)
    assert n == 0 and len(d) == 1 and "unrelated" not in d[0].message
    d, n = cx.parse_mypy_text('pkg/a.py:3:1: error: Bad  [misc]\npkg/a.py:3:1: note: about line 3\n', tmp_path)
    assert "about line 3" in d[0].message
    # pip wrapper detection reads a bounded head and tail
    big = tmp_path / "pyright.exe"
    big.write_bytes(b"MZ" + b"\0" * (9 * 1024 * 1024) + b"from pyright.cli import entrypoint")
    assert cx._pip_pyright(big)


def test_tsc_global_error_with_a_program_is_incomplete(tmp_path, monkeypatch):
    isolate_path(tmp_path, monkeypatch)
    proj = ts_project(tmp_path)
    fake(proj / "node_modules" / ".bin", "tsc",
         'if argv == ["--version"]:\n    print("Version 5.4.5"); sys.exit(0)\n'
         'print("error TS5083: Cannot read file \'base.json\'.")\n'
         'print(os.path.join(root, "src/lib.ts").replace(os.sep, "/"))\nsys.exit(1)', monkeypatch)
    res = codecheck.check(proj, ["src/lib.ts"], env="none", use_cache=False, checker="tsc")
    assert res["checker"][0]["status"] == "ran" and res["exit"] == 4
    assert any("TS5083" in n for n in res["incomplete"])


def test_runaway_output_stops_the_checker(tmp_path, monkeypatch):
    isolate_path(tmp_path, monkeypatch)
    proj = ts_project(tmp_path)
    monkeypatch.setattr(cx, "MAX_OUTPUT", 200_000)
    fake(proj / "node_modules" / ".bin", "tsc",
         'if argv == ["--version"]:\n    print("Version 5.4.5"); sys.exit(0)\n'
         'while True:\n    sys.stdout.write("x" * 10000 + "\\n"); sys.stdout.flush()', monkeypatch)
    t0 = time.perf_counter()
    res = codecheck.check(proj, ["src/app.ts"], env="none", use_cache=False, checker="tsc", checker_timeout=60)
    assert time.perf_counter() - t0 < 40
    assert res["exit"] == 4 and any("passed" in n and "stopped" in n for n in res["incomplete"])
