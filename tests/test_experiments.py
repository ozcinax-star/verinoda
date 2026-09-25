"""Experiments: real isolated runs on a git-committed copy of examples/orders_app.

Every test runs a real child process (no mocks of experiments.run): allowlisted
pytest in a throw-away copy with a scrubbed environment, timeouts that kill the
whole process tree, refusal of non-allowlisted commands, logs on disk with only
a summary returned.
"""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import hashlib  # noqa: E402
import json  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import evidence as evmod  # noqa: E402
from verinoda import experiments  # noqa: E402
from verinoda.claims import Claims  # noqa: E402
from verinoda.paths import DEFAULT_CONFIG, runs_dir  # noqa: E402
from verinoda.store import open_store  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "orders_app"
PYTEST = ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]
FAKE_SECRET = "sk-ant-FAKE-0123456789-not-a-real-key"

pytestmark = [pytest.mark.experiment,
              pytest.mark.skipif(shutil.which("git") is None, reason="git not available")]


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args],
                   cwd=cwd, check=True, capture_output=True)


def _copy_example(dst: Path) -> Path:
    shutil.copytree(EXAMPLE, dst, ignore=shutil.ignore_patterns(".verinoda", "__pycache__", "*.pyc",
                                                                ".pytest_cache", "*.db"))
    _git(dst, "init", "-q")
    _git(dst, "add", "-A")
    _git(dst, "commit", "-q", "-m", "init")
    return dst


def _tree_digest(repo: Path) -> dict[str, str]:
    """Every file outside .verinoda (where run logs legitimately go)."""
    out = {}
    for p in sorted(repo.rglob("*")):
        rel = p.relative_to(repo).as_posix()
        if p.is_file() and not rel.startswith((".verinoda/", ".git/")):
            out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def _alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes

        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        k32.GetExitCodeProcess(h, ctypes.byref(code))
        k32.CloseHandle(h)
        return code.value == 259  # STILL_ACTIVE
    try:  # pragma: no cover - POSIX
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@pytest.fixture
def proj(tmp_path):
    repo = _copy_example(tmp_path / "orders_app")
    st = open_store(repo)
    yield repo, st
    st.close()


def _probe(repo: Path, name: str, body: str) -> str:
    (repo / "tests" / name).write_text(body, encoding="utf-8")
    return f"tests/{name}"


# -- policy ----------------------------------------------------------------------------------

@pytest.mark.parametrize("argv,expected", [
    (["pytest"], "allowlisted"),
    (["pytest", "-q", "tests/test_pricing.py"], "allowlisted"),
    (["python", "-m", "pytest", "-q"], "allowlisted"),
    ([r"C:\Python312\python.exe", "-m", "pytest"], "allowlisted"),
    (["py", "-m", "pytest", "-x"], "allowlisted"),
    (["go", "test", "./..."], "allowlisted"),
    (["python", "-c", "print(1)"], "risky"),
    (["python", "-m", "pip", "install", "x"], "risky"),
    (["pytest-evil"], "risky"),
    (["pytest", "tests;", "rm", "-rf", "/"], "risky"),
    (["pytest", "$(whoami)"], "risky"),
    (["pytest", "a|b"], "risky"),
    (["bash", "-c", "pytest"], "risky"),
    ([], "risky"),
])
def test_classify(argv, expected):
    assert experiments.classify(argv, DEFAULT_CONFIG["experiments"]["process_isolation_allowlist"]) == expected


def test_scrubbed_env_drops_secrets_and_keeps_interpreter(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_SECRET)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "fake")
    env = experiments._scrubbed_env(tmp_path)
    assert not {"ANTHROPIC_API_KEY", "GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY"} & set(env)
    assert FAKE_SECRET not in json.dumps(env)
    assert env["PATH"].split(os.pathsep)[0] == str(Path(sys.executable).parent)
    assert env["HOME"] == env["USERPROFILE"] == env["TEMP"] == str(tmp_path)
    assert env["VERINODA_EXPERIMENT"] == "1"


# -- real runs ---------------------------------------------------------------------------------

def test_allowlisted_pytest_passes_in_a_copy_with_process_isolation(proj):
    repo, st = proj
    probe = _probe(repo, "test_probe_cwd.py", (
        "import os\nfrom pathlib import Path\n\n"
        "def test_runs_in_a_copy():\n"
        f"    assert Path.cwd().resolve() != Path(r'{repo.resolve()}')\n"
        "    Path('WRITTEN_BY_EXPERIMENT.txt').write_text('x')\n"
        "    print('CWD=' + os.getcwd())\n"))
    before = _tree_digest(repo)
    res = experiments.run(st, repo, [*PYTEST, "-s", "tests/test_pricing.py", probe],
                          hypothesis="pricing tests pass", commit="abc")
    assert res["isolation"] == "process" and res["outcome"] == "pass" and res["matches_expectation"]
    g = res["guarantees"]
    assert g["separate_process"] and g["cwd_is_copy"] and g["env_allowlisted"] and g["timeout_kills_tree"]
    assert g["network_isolated"] is False and g["fs_reads_confined"] is False  # stated honestly
    # The original tree is untouched: nothing written, nothing changed.
    assert _tree_digest(repo) == before
    assert not (repo / "WRITTEN_BY_EXPERIMENT.txt").exists() and not (repo / ".pytest_cache").exists()
    log = Path(res["logs"]["stdout"]).read_text(encoding="utf-8")
    assert "CWD=" in log and str(repo.resolve()) not in log.split("CWD=")[1].splitlines()[0]
    assert "4 passed" in log
    # Recorded: experiment row + test_result evidence that can verify a claim.
    row = st.get("experiments", res["id"])
    assert row["status"] == "pass" and row["isolation"] == "process" and row["exit_code"] == 0
    assert row["cwd"].startswith("copy of ") and row["command"][:3] == ["python", "-m", "pytest"]
    ev = st.evidence(res["evidence_id"])
    assert ev["source_type"] == "test_result" and ev["meta"]["outcome"] == "pass"
    assert ev["meta"]["isolation"] == "process" and ev["commit_sha"] == "abc"
    assert any("4 passed" in s for s in res["summary"]["summary_lines"])


def test_parent_secrets_are_not_visible_to_the_child(proj, monkeypatch):
    repo, st = proj
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_SECRET)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-fake")
    monkeypatch.setenv("TZ", "UTC")  # allowlisted variable: proves the env is filtered, not emptied
    absent = _probe(repo, "test_probe_env.py", (
        "import os\n\n"
        "def test_secrets_absent():\n"
        "    assert 'ANTHROPIC_API_KEY' not in os.environ\n"
        "    assert 'OPENAI_API_KEY' not in os.environ\n"
        "    assert os.environ.get('VERINODA_EXPERIMENT') == '1'\n"
        "    assert os.environ.get('TZ') == 'UTC'\n"))
    res = experiments.run(st, repo, [*PYTEST, absent], hypothesis="secrets are scrubbed")
    assert res["outcome"] == "pass", Path(res["logs"]["stdout"]).read_text(encoding="utf-8")[-2000:]
    # Control: a test that *requires* the key fails in the child (expected failure).
    present = _probe(repo, "test_probe_env_present.py", (
        "import os\n\ndef test_secret_present():\n    assert os.environ['ANTHROPIC_API_KEY']\n"))
    ctl = experiments.run(st, repo, [*PYTEST, present], hypothesis="the key is visible", expect="fail")
    assert ctl["outcome"] == "fail" and ctl["matches_expectation"] is True
    for r in (res, ctl):
        logs = Path(r["logs"]["stdout"]).read_text(encoding="utf-8") + Path(r["logs"]["stderr"]).read_text(
            encoding="utf-8")
        assert FAKE_SECRET not in logs and FAKE_SECRET not in json.dumps(r)


def test_timeout_kills_the_process_tree_and_is_recorded(proj, tmp_path):
    repo, st = proj
    pidfile = tmp_path / "grandchild.pid"
    probe = _probe(repo, "test_probe_hang.py", (
        "import subprocess, sys, time\nfrom pathlib import Path\n\n"
        "def test_hangs():\n"
        "    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'])\n"
        f"    Path(r'{pidfile}').write_text(str(child.pid))\n"
        "    time.sleep(300)\n"))
    t0 = time.monotonic()
    res = experiments.run(st, repo, [*PYTEST, probe], hypothesis="it finishes", timeout=10)
    elapsed = time.monotonic() - t0
    assert res["outcome"] == "timeout" and res["matches_expectation"] is False
    assert res["summary"]["timed_out"] is True and res["summary"]["exit_code"] is None
    assert elapsed < 60
    row = st.get("experiments", res["id"])
    assert row["status"] == "timeout" and row["timed_out"] == 1 and row["exit_code"] is None
    ev = st.evidence(res["evidence_id"])
    assert ev["meta"]["raw_outcome"] == "timeout" and ev["meta"]["outcome"] == "fail"
    assert pidfile.exists(), "probe never started its grandchild"
    pid = int(pidfile.read_text())
    deadline = time.monotonic() + 10
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.2)
    assert not _alive(pid), f"grandchild {pid} survived the timeout"


def test_non_allowlisted_command_is_refused_without_container(proj):
    repo, st = proj
    if experiments.container_runtime():
        pytest.skip("a container runtime is available; risky commands would run in a container")
    with pytest.raises(experiments.ExperimentRefused, match="no container runtime"):
        experiments.run(st, repo, ["python", "-c", "open('PWNED', 'w').write('x')"], hypothesis="h")
    with pytest.raises(experiments.ExperimentRefused):
        experiments.run(st, repo, "pytest -q; echo hi", hypothesis="shell metacharacters")
    with pytest.raises(experiments.ExperimentRefused):  # container requested explicitly, none available
        experiments.run(st, repo, PYTEST, hypothesis="h", isolation="container")
    rows = st.all("SELECT * FROM experiments ORDER BY created_at")
    assert [r["status"] for r in rows] == ["refused"] * 3 and {r["isolation"] for r in rows} == {"none"}
    assert not (repo / "PWNED").exists()
    assert not runs_dir(repo).exists() or not any(runs_dir(repo).iterdir())


def test_project_config_controls_the_allowlist(proj):
    repo, st = proj
    if experiments.container_runtime():
        pytest.skip("a container runtime is available; the command would run in a container instead")
    cfg = repo / ".verinoda" / "config.json"
    cfg.write_text(json.dumps({"experiments": {"process_isolation_allowlist": ["go test"]}}), encoding="utf-8")
    with pytest.raises(experiments.ExperimentRefused):
        experiments.run(st, repo, PYTEST, hypothesis="pytest no longer allowlisted")
    cfg.write_text(json.dumps({"experiments": {"default_timeout": 1}}), encoding="utf-8")
    probe = _probe(repo, "test_probe_slow.py", "import time\n\ndef test_slow():\n    time.sleep(30)\n")
    res = experiments.run(st, repo, [*PYTEST, probe], hypothesis="default timeout from config")
    assert res["outcome"] == "timeout" and st.get("experiments", res["id"])["timeout_s"] == 1.0


def test_logs_stay_on_disk_and_only_a_summary_is_returned(proj):
    repo, st = proj
    probe = _probe(repo, "test_probe_noisy.py", (
        "def test_noisy():\n"
        "    for i in range(3000):\n"
        "        print(f'noise line {i:04d} ' + 'x' * 60)\n"
        "    assert 1 + 1 == 3, 'deliberate failure'\n"))
    res = experiments.run(st, repo, [*PYTEST, "-s", probe], hypothesis="noisy test passes")
    assert res["outcome"] == "fail" and not res["matches_expectation"]
    out = Path(res["logs"]["stdout"])
    assert out.is_file() and out.parent == runs_dir(repo) / res["id"]
    full = out.read_text(encoding="utf-8")
    assert "noise line 0000" in full and "noise line 2999" in full
    lines = full.splitlines()
    # Exactly one line per print: no "\r\r\n" doubling from text-mode rewrites on Windows.
    assert sum(1 for ln in lines if ln.startswith("noise line ")) == 3000
    assert lines[lines.index(next(ln for ln in lines if ln.startswith("noise line 0000"))) + 1].startswith(
        "noise line 0001")
    s = res["summary"]
    assert s["total_lines"] > 3000
    assert len(s["tail"]) <= 8 and len(s["relevant_lines"]) <= 15 and len(s["summary_lines"]) <= 3
    assert any("deliberate failure" in ln or "assert" in ln for ln in s["relevant_lines"])
    assert any("1 failed" in ln for ln in s["summary_lines"])
    small = json.dumps(res)
    assert len(small) < 8000 and "noise line 1500" not in small
    ev = st.evidence(res["evidence_id"])
    assert len(ev["excerpt"]) <= 400 and ev["meta"]["stdout"] == str(out)


def test_experiment_attached_to_a_claim_supports_or_refutes_it(proj):
    repo, st = proj
    cl = Claims(st, repo)
    c = cl.create("the pricing tests pass", project="orders_app", snapshot=None, status="unknown",
                  kind="test_run", spec={"command": ["tests/test_pricing.py"]})
    ok = experiments.run(st, repo, [*PYTEST, "tests/test_pricing.py"], hypothesis="pricing ok", claim_id=c["id"])
    assert [e["relation"] for e in cl.evidence(c["id"])] == ["supports"]
    assert cl.set_status(c["id"], "experiment_verified", reason="ran", downgrade=False)["status"] == \
        "experiment_verified"
    # the same run under a plain-text claim: its words are in the command line, which is no verification
    plain = cl.create("the pricing tests pass", project="orders_app", snapshot=None, status="unknown")
    cl.attach(plain["id"], ok["evidence_id"], "supports")
    assert cl.set_status(plain["id"], "experiment_verified", reason="ran", downgrade=True)["status"] == \
        "strong_inference"
    bad = _probe(repo, "test_probe_fail.py", "def test_fails():\n    assert False\n")
    c2 = cl.create("the probe passes", project="orders_app", snapshot=None, status="unknown")
    res = experiments.run(st, repo, [*PYTEST, bad], hypothesis="probe ok", claim_id=c2["id"])
    assert res["outcome"] == "fail"
    assert [e["relation"] for e in cl.evidence(c2["id"])] == ["refutes"]
    assert cl.reassess(c2["id"], reason="run failed")["status"] == "contradicted"
    assert ok["evidence_id"] != res["evidence_id"]
    assert evmod.is_verifying(st.evidence(ok["evidence_id"]))


# -- interpreter choice ------------------------------------------------------------------------

def test_python_for_prefers_the_projects_virtualenv(tmp_path):
    repo = tmp_path / "p"
    repo.mkdir()
    assert experiments.python_for(repo) == sys.executable
    for env, rel in (("venv", ("bin", "python")), ("venv", ("Scripts", "python.exe")),
                     (".venv", ("bin", "python")), (".venv", ("Scripts", "python.exe"))):
        exe = repo.joinpath(env, *rel)
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_bytes(b"")
        assert experiments.python_for(repo) == str(exe)  # each later one outranks the earlier ones
    assert Path(experiments.python_for(repo)).is_absolute()


@pytest.mark.parametrize("argv,expected", [
    ([sys.executable, "-m", "pytest", "-q"], "allowlisted"),
    ([r"C:\work\proj\.venv\Scripts\python.exe", "-m", "pytest", "tests/test_x.py"], "allowlisted"),
    (["/work/proj/.venv/bin/python", "-m", "pytest"], "allowlisted"),
    (["/usr/local/bin/python3.12", "-m", "pytest", "-x"], "allowlisted"),
    (['"C:\\Program Files\\Python312\\python.exe"', "-m", "pytest"], "allowlisted"),
    ([r"C:\work\proj\.venv\Scripts\python.exe", "-m", "pip", "install", "x"], "risky"),
    (["/work/proj/.venv/bin/python", "-c", "print(1)"], "risky"),
    (["/work/proj/.venv/bin/python3.12", "script.py"], "risky"),
    (["/tmp/pythonic-tool", "-m", "pytest"], "risky"),
])
def test_classify_accepts_absolute_interpreter_paths_for_pytest_only(argv, expected):
    assert experiments.classify(argv, DEFAULT_CONFIG["experiments"]["process_isolation_allowlist"]) == expected


def test_container_argv_uses_the_images_python():
    assert experiments._container_argv(["/w/.venv/bin/python", "-m", "pytest"]) == ["python", "-m", "pytest"]
    assert experiments._container_argv(["python", "-m", "pytest"]) == ["python", "-m", "pytest"]
    assert experiments._container_argv(["go", "test"]) == ["go", "test"]


# -- inconclusive runs -------------------------------------------------------------------------

@pytest.mark.parametrize("code,outcome", [(0, "pass"), (1, "fail"), (2, "inconclusive"), (3, "inconclusive"),
                                          (4, "inconclusive"), (5, "inconclusive")])
def test_pytest_exit_codes_are_classified(code, outcome):
    assert experiments._classify_outcome(PYTEST, code, False, "", "")[0] == outcome
    # other runners: any non-zero exit is a failure
    assert experiments._classify_outcome(["go", "test"], code, False, "", "")[0] == ("pass" if code == 0 else "fail")
    assert experiments._classify_outcome(PYTEST, None, True, "", "")[0] == "timeout"


@pytest.mark.parametrize("out", [
    "ℹ tests 0\nℹ suites 0\nℹ pass 0\n",                     # node --test, spec reporter
    "TAP version 13\n1..0\n# tests 0\n# pass 0\n",                          # node --test, TAP
    "No tests found, exiting with code 0\n",                                # jest --passWithNoTests
    "\nRan 0 tests in 0.000s\n\nOK\n",                                      # unittest
    "running 0 tests\n\ntest result: ok. 0 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out\n",  # cargo
    "?   \texample.com/m\t[no test files]\n",                               # go
])
def test_a_run_that_ran_no_test_is_no_pass(out):
    # review finding: a node test file renamed out of the runner's pattern ran 0 tests, exited 0 and "passed"
    got, why = experiments._classify_outcome(["node", "--test"], 0, False, out, "")
    assert got == "inconclusive" and "no pass" in why


def test_a_run_with_tests_still_passes():
    for out in ("ℹ tests 3\nℹ pass 3\n", "Ran 2 tests in 0.1s\n\nOK\n",
                "running 1 test\ntest result: ok. 1 passed; 0 failed; 0 ignored\nrunning 0 tests\n"
                "test result: ok. 0 passed; 0 failed; 0 ignored\n",
                "ok  \texample.com/m\t0.01s\n?   \texample.com/n\t[no test files]\n"):
        assert experiments._classify_outcome(["node", "--test"], 0, False, out, "")[0] == "pass"


def test_processes_a_run_leaves_running_are_stopped(proj, tmp_path):
    # review finding: a server a test started outlived the run, kept the throw-away copy and answered the next run
    repo, st = proj
    pidfile = tmp_path / "helper.pid"
    cwdfile = tmp_path / "helper.cwd"
    probe = _probe(repo, "test_probe_leak.py", (
        "import os, subprocess, sys\nfrom pathlib import Path\n\n"
        "def test_leaves_a_helper():\n"
        "    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'],\n"
        "                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        f"    Path(r'{pidfile}').write_text(str(child.pid))\n"
        f"    Path(r'{cwdfile}').write_text(os.getcwd())\n"))
    res = experiments.run(st, repo, [*PYTEST, probe], hypothesis="the helper is stopped", timeout=60)
    assert res["outcome"] == "pass"
    pid = int(pidfile.read_text())
    deadline = time.monotonic() + 10
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.2)
    assert not _alive(pid), f"helper {pid} outlived the run"
    assert not Path(cwdfile.read_text()).parent.exists()  # the throw-away directory is gone
    assert any("left running were stopped" in x for x in res["limits"])


def test_missing_pytest_is_inconclusive():
    out, why = experiments._classify_outcome(PYTEST, 1, False, "", "C:\\py\\python.exe: No module named pytest\n")
    assert out == "inconclusive" and "not installed" in why
    # a missing *plugin* is not a missing pytest
    assert experiments._classify_outcome(PYTEST, 1, False, "", "No module named 'pytest_cov'")[0] == "fail"


def test_inconclusive_run_only_qualifies_the_claim(proj):
    repo, st = proj
    cl = Claims(st, repo)
    c = cl.create("the helper module is tested", project="orders_app", snapshot=None, status="weak_inference",
                  evidence=[(evmod.source_evidence(repo, "orders/pricing.py", 1, commit=None), "supports")])
    empty = _probe(repo, "test_probe_empty.py", "# no tests here\nX = 1\n")
    for expect in ("pass", "fail"):
        res = experiments.run(st, repo, [*PYTEST, empty], hypothesis="the probe passes", claim_id=c["id"],
                              expect=expect)
        assert res["outcome"] == "inconclusive" and res["matches_expectation"] is False
        assert "exit code 5" in res["inconclusive_reason"] and res["next_step"]
        row = st.get("experiments", res["id"])
        assert row["status"] == "inconclusive" and row["exit_code"] == 5
        ev = st.evidence(res["evidence_id"])
        assert ev["meta"]["outcome"] == "inconclusive" and ev["meta"]["raw_outcome"] == "inconclusive"
    rels = [e["relation"] for e in cl.evidence(c["id"])]
    assert rels.count("qualifies") == 2 and rels.count("refutes") == 0 and rels.count("supports") == 1
    assert cl.reassess(c["id"], reason="after inconclusive runs")["status"] == "weak_inference"
    # a path that does not exist is a pytest usage error (4), not a failing test
    res = experiments.run(st, repo, [*PYTEST, "tests/test_does_not_exist.py"], hypothesis="h")
    assert res["outcome"] == "inconclusive" and st.get("experiments", res["id"])["exit_code"] == 4


def test_project_venv_without_pytest_is_inconclusive_not_failed(proj):
    repo, st = proj
    venv = repo / ".venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True, capture_output=True,
                   stdin=subprocess.DEVNULL, timeout=180)
    py = experiments.python_for(repo)
    assert Path(py).is_file() and Path(py).is_relative_to(venv)
    assert experiments.classify([py, "-m", "pytest"],
                                DEFAULT_CONFIG["experiments"]["process_isolation_allowlist"]) == "allowlisted"
    res = experiments.run(st, repo, [py, "-m", "pytest", "-q", "tests/test_pricing.py"], hypothesis="pricing ok")
    assert res["isolation"] == "process"
    assert res["outcome"] == "inconclusive" and "not installed" in res["inconclusive_reason"], res["summary"]


def test_verify_rerun_that_is_inconclusive_does_not_contradict(proj):
    from verinoda import workflow

    repo, st = proj
    cl = Claims(st, repo)
    _probe(repo, "test_probe_empty.py", "X = 1\n")
    c = cl.create("the probe passes", project="orders_app", snapshot=None, status="weak_inference",
                  evidence=[(evmod.source_evidence(repo, "orders/pricing.py", 1, commit=None), "supports")],
                  kind="test_run", spec={"command": ["tests/test_probe_empty.py"]})
    v = workflow.verify(st, repo, c["id"], run=True)
    assert v["experiment"]["outcome"] == "inconclusive"
    assert v["after"]["status"] == "weak_inference" and "inconclusive" in v["note"]
    assert "qualifies" in [e["relation"] for e in cl.evidence(c["id"])]


# -- containers and stdin ----------------------------------------------------------------------

def test_container_run_is_named_and_killed_by_name_on_timeout(proj, monkeypatch):
    repo, st = proj
    real_popen, real_run = subprocess.Popen, subprocess.run
    popened, ran = [], []

    def fake_popen(cmd, **kw):
        if cmd[0] == "fakedocker":
            popened.append((cmd, kw))
            cmd = [sys.executable, "-c", "import time; time.sleep(120)"]
        return real_popen(cmd, **kw)

    def fake_run(cmd, *a, **kw):
        if cmd[0] == "fakedocker":
            ran.append((cmd, kw))
            return subprocess.CompletedProcess(cmd, 0, b"", b"")
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(experiments, "container_runtime", lambda: "fakedocker")
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(subprocess, "run", fake_run)
    res = experiments.run(st, repo, [experiments.python_for(repo), "-m", "pytest", "-q"], hypothesis="h",
                          isolation="container", timeout=3)
    assert res["isolation"] == "container" and res["outcome"] == "timeout"
    ((cmd, kw),) = popened
    name = f"verinoda-{res['id']}"
    assert cmd[cmd.index("--name") + 1] == name and "--rm" in cmd and cmd[cmd.index("--network") + 1] == "none"
    assert cmd[-4:] == ["python", "-m", "pytest", "-q"]  # the host interpreter path means nothing in the image
    assert kw["stdin"] is subprocess.DEVNULL
    assert [c for c, _ in ran] == [["fakedocker", "kill", name]]
    assert ran[0][1]["stdin"] is subprocess.DEVNULL


def test_no_child_inherits_stdin(monkeypatch, tmp_path):
    from verinoda import snapshot

    seen = []
    real_run = subprocess.run

    def spy(cmd, *a, **kw):
        seen.append((cmd[0], kw.get("stdin", "<inherited>")))
        if cmd[0] == "git":
            return real_run(cmd, *a, **kw)
        return subprocess.CompletedProcess(cmd, 1, b"", b"")

    monkeypatch.setattr(subprocess, "run", spy)
    monkeypatch.setattr(experiments.shutil, "which", lambda name, *a, **k: sys.executable)
    snapshot.git(tmp_path, "rev-parse", "HEAD")
    assert experiments.container_runtime() is None  # "info" failed for both runtimes
    experiments._kill_container("fakedocker", "verinoda-x")
    assert [c for c, _ in seen] == ["git", "docker", "podman", "fakedocker"]
    assert all(stdin is subprocess.DEVNULL for _, stdin in seen), seen


# -- acceptance audit: allowlisted commands may only name paths inside the copy ------------------

ALLOW = DEFAULT_CONFIG["experiments"]["process_isolation_allowlist"]


@pytest.mark.parametrize("argv,why", [
    ([*PYTEST, r"C:\outside\test_evil.py"], "absolute path"),
    ([*PYTEST, "C:/outside/test_evil.py"], "absolute path"),
    ([*PYTEST, "/tmp/outside/test_evil.py"], "absolute path"),
    ([*PYTEST, r"\server\share\test_evil.py"], "absolute path"),
    ([*PYTEST, "C:test_evil.py"], "absolute path"),              # drive-relative: another cwd
    ([*PYTEST, "../outside/test_evil.py"], "'..'"),
    ([*PYTEST, r"tests\..\..\x\test_evil.py"], "'..'"),
    ([*PYTEST, "~/test_evil.py"], "home directory"),
    ([*PYTEST, "--rootdir=/tmp"], "absolute path"),
    ([*PYTEST, "--rootdir", ".."], "'..'"),
    ([*PYTEST, "-c", "/etc/pytest.ini"], "absolute path"),
    ([*PYTEST, "-c/etc/pytest.ini"], "absolute path"),
    ([*PYTEST, "--confcutdir=C:/"], "absolute path"),
    ([*PYTEST, "--basetemp", r"C:\Windows\Temp\x"], "absolute path"),
    ([*PYTEST, "--junitxml=../report.xml"], "'..'"),
    ([*PYTEST, "-o", "cache_dir=/tmp/cache"], "absolute path"),
    ([*PYTEST, "--override-ini=cache_dir=../../cache"], "'..'"),
    ([*PYTEST, "--cov-report=xml:/tmp/cov.xml"], "absolute path"),
    ([*PYTEST, "-p", "/tmp/evil_plugin"], "absolute path"),
    ([*PYTEST, "--pyargs", "somepkg"], "--pyargs"),
    (["pytest", "--log-file=/var/log/x"], "absolute path"),
    (["node", "--test", "/abs/test.js"], "absolute path"),
    (["node", "--test", "-e", "require('fs')"], "inline code"),
    (["node", "--test", "--test-reporter-destination=../out.txt"], "'..'"),
    (["go", "test", "-exec", "sh", "./..."], "arbitrary program"),
    (["go", "test", "-coverprofile=/tmp/c.out", "./..."], "absolute path"),
    (["cargo", "test", "--config", "target.x.runner='sh'"], "arbitrary runner"),
    (["npm", "test", "--script-shell=bash"], "arbitrary shell"),
])
def test_policy_rejects_arguments_that_leave_the_copy(argv, why):
    kind, reason = experiments.policy(argv, ALLOW)
    assert kind == "risky" and why in reason, reason
    assert experiments.classify(argv, ALLOW) == "risky"


@pytest.mark.parametrize("argv", [
    [*PYTEST, "tests/test_pricing.py::test_compute_total"],
    [*PYTEST, r"tests\test_pricing.py", "-k", "not slow and discount", "-x", "--tb=short"],
    [*PYTEST, "-p", "no:cacheprovider", "-pno:randomly", "-W", "ignore::DeprecationWarning"],
    [*PYTEST, "--rootdir=.", "-o", "cache_dir=.cache", "--basetemp=tmp/bt", "tests/sub/../test_pricing.py"],
    [*PYTEST, "--junitxml=reports/junit.xml", "--cov-report=term-missing", "tests/test_x.py::test_a[1/2]"],
    ["node", "--test", "tests/", "--test-reporter-destination=out.txt"],
    ["go", "test", "./...", "-run", "TestX"],
    [r"C:\proj\.venv\Scripts\python.exe", "-m", "pytest", "tests"],  # argv[0] may be absolute
])
def test_policy_keeps_paths_inside_the_copy_allowlisted(argv):
    assert experiments.policy(argv, ALLOW) == ("allowlisted", None)


def test_path_escape_is_lexical_and_os_independent():
    assert experiments.path_escape("a/b/../c") is None
    assert experiments.path_escape("a/../..") and "'..'" in experiments.path_escape("a/../..")
    assert experiments.path_escape("./x/./y") is None
    assert "URL" in experiments.path_escape("file:///etc/passwd")
    assert "absolute" in experiments.path_escape('"C:\\x"')
    assert experiments.path_escape("") is None


def test_audit_absolute_test_path_outside_the_project_is_refused(proj, tmp_path):
    """Acceptance-audit reproduction: `python -m pytest <absolute path outside>` wrote a file outside.

    Before the fix the command was allowlisted (it starts with `python -m pytest`), ran under
    process isolation only and the outside test wrote PWNED.txt next to itself.
    """
    repo, st = proj
    if experiments.container_runtime():
        pytest.skip("a container runtime is available; the command would run in a container (outside path absent)")
    outside = tmp_path / "outside"
    outside.mkdir()
    pwned = outside / "PWNED.txt"
    evil = outside / "test_evil.py"
    evil.write_text("from pathlib import Path\n\n"
                    "def test_writes_outside():\n"
                    f"    Path(r'{pwned}').write_text('written from an experiment')\n", encoding="utf-8")
    for arg in (str(evil), evil.as_posix(), "../outside/test_evil.py"):
        with pytest.raises(experiments.ExperimentRefused, match="inside the repository copy") as exc:
            experiments.run(st, repo, [*PYTEST, arg], hypothesis="outside test passes")
        assert "no container runtime" in str(exc.value)
    assert not pwned.exists(), "an experiment wrote outside the project"
    rows = st.all("SELECT * FROM experiments ORDER BY created_at")
    assert [r["status"] for r in rows] == ["refused"] * 3
    assert all("inside the repository copy" in r["summary"] for r in rows)
    assert rows[0]["environment"]["policy"]["kind"] == "risky"
    assert not runs_dir(repo).exists() or not any(runs_dir(repo).iterdir())


def test_guarantees_say_writes_are_not_confined_under_process_isolation(proj):
    repo, st = proj
    res = experiments.run(st, repo, [*PYTEST, "tests/test_pricing.py"], hypothesis="pricing ok")
    g = res["guarantees"]
    assert res["isolation"] == "process"
    assert g["fs_writes_confined"] is False and g["fs_reads_confined"] is False and g["network_isolated"] is False
    assert g["path_args_confined"] is True
    assert any("outside the copy" in s for s in res["limits"])
    row = st.get("experiments", res["id"])
    assert row["environment"]["guarantees"]["fs_writes_confined"] is False
    assert row["environment"]["limits"] == res["limits"]


def test_plugins_env_extra_and_artifacts(proj):
    repo, st = proj
    plugin = (
        "import os, pathlib\n"
        "out = pathlib.Path(os.environ['VERINODA_ARTIFACTS'])\n"
        "here = pathlib.Path(__file__).resolve()\n"
        "(out / 'probe.txt').write_bytes(('x=' + os.environ.get('VERINODA_PROBE', '') + ';'\n"
        "    + 'inside_copy=' + str(pathlib.Path.cwd().resolve() in here.parents)).encode())\n"
    ).encode()
    res = experiments.run(st, repo, [*PYTEST, "-p", "verinoda_probe", "tests/test_pricing.py"],
                          hypothesis="plugin loads", plugins={"verinoda_probe.py": plugin},
                          env_extra={"VERINODA_PROBE": "42"})
    assert res["outcome"] == "pass", Path(res["logs"]["stderr"]).read_text(encoding="utf-8")[-1500:]
    art = Path(res["artifacts"]["probe.txt"])
    assert art.parent == runs_dir(repo) / res["id"] / "artifacts"
    assert art.read_bytes() == b"x=42;inside_copy=False"  # the plugin lives next to the copy, not in it
    assert st.get("experiments", res["id"])["environment"]["plugins"] == ["verinoda_probe.py"]
    with pytest.raises(ValueError, match="VERINODA_"):
        experiments.run(st, repo, PYTEST, hypothesis="h", env_extra={"PYTHONPATH": "/x"})
    with pytest.raises(ValueError, match="module file name"):
        experiments.run(st, repo, PYTEST, hypothesis="h", plugins={"../evil.py": b""})
