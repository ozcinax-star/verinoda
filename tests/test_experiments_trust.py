"""Running a project's own tests safely (docs/DESIGN.md D63).

Trust is a user decision recorded outside the repository (`verinoda trust`): only a trusted project's tests
run with process isolation, and only a trusted project's own config sets the protected settings. For every
project, argv[0] must be a program the user installed, looked up without the current directory; pytest's
ways to take arguments the policy never sees (@files, -p, -o addopts, the config files' addopts and path
settings) are checked; the copy never follows symbolic links or junctions; a container run is hardened.
"""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import io  # noqa: E402
import json  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import experiments, paths  # noqa: E402
from verinoda.paths import DEFAULT_CONFIG, runs_dir  # noqa: E402
from verinoda.store import open_store  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "orders_app"
PYTEST = ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]
ALLOW = DEFAULT_CONFIG["experiments"]["process_isolation_allowlist"]

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args],
                   cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def proj(tmp_path):
    repo = tmp_path / "orders_app"
    shutil.copytree(EXAMPLE, repo, ignore=shutil.ignore_patterns(".verinoda", "__pycache__", "*.pyc",
                                                                 ".pytest_cache", "*.db"))
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    st = open_store(repo)
    yield repo, st
    st.close()


@pytest.fixture
def untrusted(tmp_path, monkeypatch):
    """A fresh per-user directory: no project is trusted and there is no user config."""
    d = tmp_path / "fresh-user-config"
    monkeypatch.setenv(paths.CONFIG_DIR_ENV, str(d))
    return d


@pytest.fixture
def no_container(monkeypatch):
    monkeypatch.setattr(experiments, "container_runtime", lambda: None)


def _write_cfg(repo: Path, data: dict) -> None:
    (repo / ".verinoda").mkdir(exist_ok=True)
    (repo / ".verinoda" / "config.json").write_text(json.dumps(data), encoding="utf-8")


# -- R1: the protected settings come from the user, or from a project the user trusts ---------------------------

def test_an_untrusted_projects_config_cannot_widen_the_protected_settings(tmp_path, untrusted):
    repo = tmp_path / "clone"
    repo.mkdir()
    _write_cfg(repo, {"experiments": {"process_isolation_allowlist": ["python"], "container_image": "evil:1"},
                      "mcp": {"profile": "full"}, "research": {"network": "on"},
                      "budget": {"seconds": 7}})
    cfg = paths.load_config(repo)
    assert cfg["experiments"] == DEFAULT_CONFIG["experiments"]
    assert cfg["mcp"]["profile"] == "core" and cfg["research"]["network"] == "cache"
    assert cfg["budget"]["seconds"] == 7  # an unprotected setting still applies
    assert paths.network_mode(repo) == "cache"
    assert experiments.policy(["python", "-c", "pass"], cfg["experiments"]["process_isolation_allowlist"])[0] == \
        "risky"
    ignored = paths.ignored_repo_settings(repo)
    assert set(ignored) == {"experiments.process_isolation_allowlist", "experiments.container_image", "mcp.profile",
                            "research.network"}
    note = paths.ignored_settings_note(repo)
    assert "not trusted" in note and "verinoda trust" in note and str(paths.user_config_path()) in note
    from verinoda.mcp.server import resolve_profile

    assert resolve_profile(repo) == "core"
    # the user decides: trusted, the project's own file applies
    paths.set_trust(repo)
    assert paths.load_config(repo)["experiments"]["process_isolation_allowlist"] == ["python"]
    assert resolve_profile(repo) == "full" and paths.network_mode(repo) == "on"
    assert paths.ignored_repo_settings(repo) == [] and paths.ignored_settings_note(repo) is None


def test_the_defaults_verinoda_init_writes_are_not_reported_as_ignored(tmp_path, untrusted):
    from verinoda import workflow

    repo = tmp_path / "p"
    repo.mkdir()
    workflow.init(repo)
    assert paths.ignored_repo_settings(repo) == []


def test_the_user_config_sets_the_protected_settings_for_every_project(tmp_path, untrusted):
    untrusted.mkdir()
    (untrusted / "config.json").write_text(json.dumps({"mcp": {"profile": "full"},
                                                      "experiments": {"default_timeout": 9}}), encoding="utf-8")
    repo = tmp_path / "p"
    repo.mkdir()
    from verinoda.mcp.server import resolve_profile

    assert resolve_profile(repo) == "full"
    assert paths.load_config(repo)["experiments"]["default_timeout"] == 9
    assert resolve_profile(repo, "core") == "core"  # --profile still wins


def test_trust_covers_subfolders_only_when_asked_and_never_under_dot_verinoda(tmp_path, untrusted):
    top = tmp_path / "work"
    (top / "a" / ".verinoda" / "research" / "ref").mkdir(parents=True)
    paths.set_trust(top)
    assert paths.is_trusted(top) and not paths.is_trusted(top / "a")
    paths.set_trust(top, subfolders=True)
    assert paths.is_trusted(top / "a")
    assert not paths.is_trusted(top / "a" / ".verinoda" / "research" / "ref")  # a reference checkout
    assert not paths.is_trusted(tmp_path / "work-other")  # a prefix of the name is not a parent
    assert len(paths.trust_entries()) == 1  # re-trusting replaces the entry
    paths.set_trust(top, remove=True)
    assert not paths.is_trusted(top) and paths.trust_entries() == []
    assert paths.trust_path().parent == untrusted  # never inside a repository


def test_trust_command(tmp_path, untrusted, capsys):
    from verinoda import cli

    repo = tmp_path / "p"
    repo.mkdir()
    assert cli.main(["trust", str(repo), "--json", "--yes"]) == 0  # no terminal here: --yes
    out = json.loads(capsys.readouterr().out)
    assert out["trusted"] is True and Path(out["trust_file"]).parent == untrusted and "process isolation" in \
        out["means"]
    assert paths.is_trusted(repo)
    assert cli.main(["trust", "--list", "--json"]) == 0
    assert [Path(e["path"]) for e in json.loads(capsys.readouterr().out)["trusted"]] == [repo.resolve()]
    assert cli.main(["trust", str(repo), "--remove"]) == 0
    assert "no longer trusted" in capsys.readouterr().out and not paths.is_trusted(repo)
    with pytest.raises(SystemExit, match="every project below it"):
        cli.main(["trust", str(Path.home()), "--subfolders"])


class _Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_trusting_is_the_users_decision(tmp_path, untrusted, capsys, monkeypatch):
    """Review D63 M3: an agent with a shell that is handed the command gets a refusal without a terminal;
    on a terminal the user confirms. The refusal's next_step is addressed to the user."""
    from verinoda import cli

    repo = tmp_path / "p"
    repo.mkdir()
    monkeypatch.setattr(sys, "stdin", io.StringIO("y\n"))  # not a terminal, like an agent's shell tool
    with pytest.raises(SystemExit, match="the user's decision"):
        cli.main(["trust", str(repo)])
    assert not paths.is_trusted(repo)
    monkeypatch.setattr(sys, "stdin", _Terminal("n\n"))
    assert cli.main(["trust", str(repo)]) == 1 and not paths.is_trusted(repo)
    assert "Trust " in capsys.readouterr().err
    monkeypatch.setattr(sys, "stdin", _Terminal("y\n"))
    assert cli.main(["trust", str(repo), "--subfolders"]) == 0 and paths.is_trusted(repo / "sub")
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))  # --remove and --list ask nothing
    assert cli.main(["trust", str(repo), "--remove"]) == 0 and not paths.is_trusted(repo)
    step = experiments.untrusted_next_step(repo)
    assert step.startswith("ask the user") and "an agent must never run it" in step and \
        f"verinoda trust {repo.resolve()}" in step


def test_a_deleted_trusted_folder_can_be_removed(tmp_path, untrusted, capsys):
    """Review D63 L2: else the entry stays, and whatever is created at that path later is trusted."""
    from verinoda import cli

    repo = tmp_path / "gone"
    repo.mkdir()
    paths.set_trust(repo)
    shutil.rmtree(repo)
    assert cli.main(["trust", "--list", "--json"]) == 0
    assert [e.get("missing") for e in json.loads(capsys.readouterr().out)["trusted"]] == [True]
    assert cli.main(["trust", str(repo), "--remove"]) == 0
    assert "no longer trusted" in capsys.readouterr().out and paths.trust_entries() == []
    with pytest.raises(SystemExit, match="is not a folder"):  # trusting still needs the folder
        cli.main(["trust", str(repo), "--yes"])


def test_an_untrusted_projects_broken_config_cannot_stop_the_mcp_server(tmp_path, untrusted):
    """Review D63 L4: the file is ignored for an untrusted project, so it is not read either."""
    from verinoda.mcp.server import resolve_profile

    repo = tmp_path / "clone"
    (repo / ".verinoda").mkdir(parents=True)
    (repo / ".verinoda" / "config.json").write_text("{not json", encoding="utf-8")
    assert resolve_profile(repo) == "core"
    paths.set_trust(repo)
    with pytest.raises(ValueError, match="cannot read the MCP tool profile"):
        resolve_profile(repo)


def test_a_relative_config_dir_is_ignored(tmp_path, monkeypatch):
    """Review D63 L5: a relative VERINODA_CONFIG_DIR would be read from the current directory - a clone could
    ship a trust.json that trusts itself."""
    clone = tmp_path / "clone"
    (clone / "evil-user").mkdir(parents=True)
    (clone / "evil-user" / "trust.json").write_text(json.dumps(
        {"version": 1, "trusted": [{"path": str(clone), "subfolders": True}]}), encoding="utf-8")
    default_base = tmp_path / "appdata"
    monkeypatch.setenv("APPDATA", str(default_base))  # the default per-user folder: a temporary one here
    monkeypatch.setenv("XDG_CONFIG_HOME", str(default_base))
    monkeypatch.chdir(clone)
    monkeypatch.setenv(paths.CONFIG_DIR_ENV, "evil-user")
    assert paths.user_config_dir() == default_base / "verinoda"
    assert not paths.is_trusted(clone)
    monkeypatch.setenv(paths.CONFIG_DIR_ENV, "~/vn-config")  # home-relative is absolute once expanded
    assert paths.user_config_dir() == Path("~/vn-config").expanduser()


# -- R2: an untrusted project's tests run only in a container -----------------------------------------------------

def test_an_untrusted_projects_tests_are_refused_without_a_container(proj, untrusted, no_container):
    repo, st = proj
    with pytest.raises(experiments.ExperimentRefused, match="not trusted") as exc:
        experiments.run(st, repo, [*PYTEST, "tests/test_pricing.py"], hypothesis="h")
    assert exc.value.untrusted and f"verinoda trust {repo}" in exc.value.next_step
    ref = experiments.refusal(repo, exc.value)
    assert ref["trusted"] is False and "verinoda trust" in ref["next_step"]
    row = st.all("SELECT * FROM experiments")[0]
    assert row["status"] == "refused" and row["environment"]["trusted"] is False
    assert not runs_dir(repo).exists() or not any(runs_dir(repo).iterdir())
    with pytest.raises(experiments.ExperimentRefused, match="process isolation was requested"):
        experiments.run(st, repo, [*PYTEST, "tests/test_pricing.py"], hypothesis="h", isolation="process")
    paths.set_trust(repo)  # the user's decision
    res = experiments.run(st, repo, [*PYTEST, "tests/test_pricing.py"], hypothesis="h")
    assert res["outcome"] == "pass" and res["isolation"] == "process" and res["trusted"] is True


def test_every_entry_point_says_how_to_trust(proj, untrusted, no_container):
    from verinoda import debug
    from verinoda.runtime import trace

    repo, st = proj
    res = trace.observe(st, repo, ["tests/test_pricing.py"], graph=False)
    assert "not trusted" in res["error"] and "verinoda trust" in res["next_step"]
    ok, why = debug._runnable(repo, PYTEST)
    assert not ok and "not trusted" in why
    with pytest.raises(experiments.ExperimentRefused) as exc:
        debug.start(st, repo, "pricing fails", PYTEST)
    assert exc.value.untrusted and "verinoda trust" in exc.value.next_step
    from verinoda.mcp.server import _error_hint

    exc = experiments.ExperimentRefused("r", experiments.untrusted_next_step(repo), untrusted=True)
    assert "verinoda trust" in _error_hint(exc, repo)


def test_verify_run_in_an_untrusted_project_says_it_was_refused(proj, untrusted, no_container):
    from verinoda import cli, evidence as evmod, workflow
    from verinoda.claims import Claims

    repo, st = proj
    cl = Claims(st, repo)
    c = cl.create("pricing passes", project="orders_app", snapshot=None, status="weak_inference",
                  evidence=[(evmod.source_evidence(repo, "orders/pricing.py", 1, commit=None), "supports")],
                  kind="test_run", spec={"command": ["tests/test_pricing.py"]})
    v = workflow.verify(st, repo, c["id"], run=True)
    assert v["experiment"] is None and "not trusted" in v["run"]["refused"]
    assert "verinoda trust" in v["run"]["next_step"]
    assert v["after"]["status"] != "contradicted"  # a run that did not happen refutes nothing
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        cli._r_verify(v)
    assert "re-run refused" in buf.getvalue() and "verinoda trust" in buf.getvalue()


def test_an_untrusted_project_runs_in_a_container_when_one_is_available(proj, untrusted, monkeypatch):
    repo, st = proj
    real_popen = subprocess.Popen
    seen = []

    def fake_popen(cmd, **kw):
        if cmd[0] == "fakedocker":
            seen.append((cmd, kw))
            cmd = [sys.executable, "-c", "print('1 passed in 0.01s')"]
        return real_popen(cmd, **kw)

    monkeypatch.setattr(experiments, "container_runtime", lambda: "fakedocker")
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-FAKE-not-a-real-key")
    monkeypatch.setenv("DOCKER_CONTEXT", "colima")
    res = experiments.run(st, repo, [experiments.python_for(repo), "-m", "pytest", "-q"], hypothesis="h")
    assert res["isolation"] == "container" and res["trusted"] is False and res["outcome"] == "pass"
    ((cmd, kw),) = seen
    # hardened: read-only root with a /tmp tmpfs, no capabilities, no privilege gain, the host user
    assert "--read-only" in cmd and cmd[cmd.index("--tmpfs") + 1] == "/tmp"
    assert cmd[cmd.index("--cap-drop") + 1] == "ALL"
    assert cmd[cmd.index("--security-opt") + 1] == "no-new-privileges"
    assert "--user" in cmd or "--userns=keep-id" in cmd
    assert "HOME=/tmp" in cmd and cmd[cmd.index("--network") + 1] == "none"
    assert cmd[-3:] == ["python", "-m", "pytest"] or cmd[-4:] == ["python", "-m", "pytest", "-q"]
    # the client keeps what it needs to reach its daemon; no secret reaches it (the container gets only -e)
    assert kw["env"].get("DOCKER_CONTEXT") == "colima" and "ANTHROPIC_API_KEY" not in kw["env"]
    assert not any("ANTHROPIC" in c for c in cmd)


def test_container_users():
    assert experiments._container_user("/usr/bin/podman") == ["--userns=keep-id"]
    user = experiments._container_user("/usr/bin/docker")
    assert user[0] == "--user" and (user[1] == f"{os.getuid()}:{os.getgid()}" if hasattr(os, "getuid")
                                    else user[1] == "1000:1000")


def test_an_image_without_pytest_is_inconclusive_and_names_the_setting(proj, untrusted, monkeypatch):
    repo, st = proj
    real_popen = subprocess.Popen

    def fake_popen(cmd, **kw):
        if cmd[0] == "fakedocker":
            cmd = [sys.executable, "-c", "import sys; sys.stderr.write(\"No module named pytest\\n\"); sys.exit(1)"]
        return real_popen(cmd, **kw)

    monkeypatch.setattr(experiments, "container_runtime", lambda: "fakedocker")
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    res = experiments.run(st, repo, ["python", "-m", "pytest", "-q"], hypothesis="h", isolation="container")
    assert res["outcome"] == "inconclusive" and "container_image" in res["next_step"]
    assert str(paths.user_config_path()) in res["next_step"]


# -- R3: the interpreter is one the user installed ------------------------------------------------------------------

def test_python_for_uses_a_projects_venv_only_when_trusted(tmp_path, untrusted):
    repo = tmp_path / "p"
    exe = repo / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    assert experiments.python_for(repo) == sys.executable  # a cloned repository's .venv can be any program
    assert experiments.policy([str(exe), "-m", "pytest"], ALLOW, repo=repo)[0] == "risky"
    paths.set_trust(repo)
    assert experiments.python_for(repo) == str(exe)
    assert experiments.policy([str(exe), "-m", "pytest"], ALLOW, repo=repo) == ("allowlisted", None)
    assert experiments.policy([str(exe), "-m", "pytest"], ALLOW)[0] == "risky"  # no project: not known


def test_a_venv_outside_the_project_made_from_a_known_interpreter_counts(tmp_path, untrusted):
    base = Path(getattr(sys, "_base_executable", sys.executable))
    venv = tmp_path / "envs" / "tools"
    exe = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    (venv / "pyvenv.cfg").write_text(f"home = {base.parent}\nversion_info = {sys.version_info[0]}."
                                     f"{sys.version_info[1]}.{sys.version_info[2]}\n", encoding="utf-8")
    repo = tmp_path / "p"
    repo.mkdir()
    assert experiments.policy([str(exe), "-m", "pytest"], ALLOW, repo=repo) == ("allowlisted", None)
    inside = repo / ".venv"
    shutil.copytree(venv, inside)
    exe_in = inside / exe.relative_to(venv)
    kind, why = experiments.policy([str(exe_in), "-m", "pytest"], ALLOW, repo=repo)
    assert kind == "risky" and "not a Python installation" in why  # the untrusted project's own venv


# -- R5: argv[0] is never looked up in the current directory ----------------------------------------------------

def test_which_never_looks_in_the_current_directory(tmp_path, monkeypatch):
    here = tmp_path / "here"
    elsewhere = tmp_path / "bin"
    here.mkdir()
    elsewhere.mkdir()
    name = "zzvnprobe"
    for d in (here, elsewhere):
        p = d / (name + (".cmd" if os.name == "nt" else ""))
        p.write_text("@echo off\n" if os.name == "nt" else "#!/bin/sh\n", encoding="utf-8")
        p.chmod(0o755)
    monkeypatch.chdir(here)
    monkeypatch.delenv("NoDefaultCurrentDirectoryInExePath", raising=False)
    assert experiments._which(name, str(tmp_path / "empty")) is None
    assert experiments._which(name, ".") is None  # a relative PATH entry is the current directory too
    found = experiments._which(name, str(elsewhere))
    assert found and Path(found).parent == elsewhere


def test_a_bare_runner_not_on_path_is_an_error_not_a_lookup_in_the_cwd(proj, monkeypatch, tmp_path):
    repo, st = proj
    _write_cfg(repo, {"experiments": {"process_isolation_allowlist": ["zzvnrunner"]}})  # trusted project
    here = tmp_path / "cwd"
    here.mkdir()
    marker = here / "RAN"
    script = here / ("zzvnrunner.cmd" if os.name == "nt" else "zzvnrunner")
    script.write_text(f"@echo x> \"{marker}\"\n" if os.name == "nt" else f"#!/bin/sh\ntouch '{marker}'\n",
                      encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.chdir(here)
    monkeypatch.delenv("NoDefaultCurrentDirectoryInExePath", raising=False)
    with pytest.raises(FileNotFoundError, match="not found on PATH"):
        experiments.run(st, repo, ["zzvnrunner"], hypothesis="h")
    assert not marker.exists()
    row = st.all("SELECT * FROM experiments")[0]
    assert row["status"] == "error" and "current directory is not searched" in row["summary"]


# -- R4: pytest's other ways to take arguments ---------------------------------------------------------------------

@pytest.mark.parametrize("argv,why", [
    ([*PYTEST, "@args.txt"], "reads more arguments from the file"),
    ([*PYTEST, "-k", "@x"], "reads more arguments from the file"),
    ([*PYTEST, "-p", "someplugin"], "loads a plugin module by name"),
    ([*PYTEST, "-psomeplugin"], "loads a plugin module by name"),
    ([*PYTEST, "-o", "addopts=-q"], "addopts override"),
    ([*PYTEST, "--override-ini=addopts=-x"], "addopts override"),
    ([*PYTEST, "-oaddopts=-v"], "addopts override"),
    ([*PYTEST, "-o", "addopts=--junitxml=C:/x.xml"], "absolute path"),
    ([*PYTEST, "-oaddopts=--junitxml=/x.xml"], "absolute path"),
    ([*PYTEST, "--override-ini=addopts=--basetemp=C:/x"], "absolute path"),
    ([*PYTEST, "-o", "addopts=-q --basetemp=../../x"], "'..'"),
    # environment variables: pytest expands them in --junitxml, --rootdir and cache_dir (review D63 H1)
    ([*PYTEST, "--junitxml=%SYSTEMDRIVE%/Users/Public/x.xml"], "environment variable (%SYSTEMDRIVE%)"),
    ([*PYTEST, "--junitxml=%WINDIR%/../Users/Public/x.xml"], "environment variable"),
    ([*PYTEST, "--junit-xml=reports/%LANG%/x.xml"], "environment variable"),
    ([*PYTEST, "--rootdir=%SYSTEMDRIVE%/"], "environment variable"),
    ([*PYTEST, "-o", "cache_dir=%TEMP%/c", "--lf"], "environment variable"),
    ([*PYTEST, "--junit-xml=$SYSTEMROOT/x"], "shell metacharacters"),  # $ in argv: refused as before
    # -p after --: pytest's consider_preparse reads every argument (review D63 M1)
    ([*PYTEST, "tests", "--", "-pzzd63_after_dashdash"], "loads a plugin module by name"),
    ([*PYTEST, "tests", "--", "-p", "zzd63_after_dashdash"], "loads a plugin module by name"),
    # combined short flags: argparse reads -qoX as -q -oX and -qcX as -q -cX
    ([*PYTEST, "-qoaddopts=-pzzd63_via_addopts"], "addopts override"),
    ([*PYTEST, "-qo", "addopts=-pzzd63"], "addopts override"),
    ([*PYTEST, "-qoaddopts=@args.txt"], "addopts override"),
    ([*PYTEST, "-q=oaddopts=-pzzd63"], "addopts override"),
    ([*PYTEST, "-vqo", "addopts=-x"], "addopts override"),
    ([*PYTEST, "-qc/abs/evil.ini"], "absolute path"),
    ([*PYTEST, "-qc../evil.ini"], "'..'"),
])
def test_pytest_argument_rules(argv, why):
    kind, reason = experiments.policy(argv, ALLOW)
    assert kind == "risky" and why in reason, reason


@pytest.mark.parametrize("argv,plugins", [
    ([*PYTEST, "-p", "no:randomly", "-pno:xdist"], ()),
    ([*PYTEST, "-p", "verinoda_probe"], ("verinoda_probe.py",)),
    ([*PYTEST, "-o", "addopts=", "--noconftest"], ()),  # switching the config files' addopts off
    ([*PYTEST, "-o", "cache_dir=.cache"], ()),
    ([*PYTEST, "--", "-p"], ()),  # a trailing -p with no name: pytest loads nothing (its preparse stops there)
    ([*PYTEST, "-qpzzd63"], ()),  # -p inside a cluster: pytest's preparse reads only -p X / -pX, loads nothing
    ([*PYTEST, "-kfoo_pc", "-rfE", "-Wignore::DeprecationWarning", "-vv"], ()),  # values of value-taking options
    ([*PYTEST, "tests/test_fmt.py::test_percent[5%]"], ()),  # one % is no variable
])
def test_pytest_argument_rules_keep_safe_uses(argv, plugins):
    assert experiments.policy(argv, ALLOW, plugins=plugins) == ("allowlisted", None)


@pytest.mark.parametrize("name,text,why", [
    ("pytest.ini", "[pytest]\naddopts = -q --junitxml=/tmp/x.xml\n", "pytest.ini, setting addopts"),
    (".pytest.ini", "[pytest]\naddopts = --junitxml=$SYSTEMDRIVE/x.xml\n", "environment variable"),
    ("pytest.ini", "[pytest]\ncache_dir = ${HOME}/c\n", "setting cache_dir"),
    ("pytest.ini", "[pytest]\ncache_dir = ..\\..\\cache\n", "'..'"),  # one string to pytest: backslashes kept
    # a TOML list item is taken as it is (pytest does not shlex-split it), backslashes included (review D63 L1)
    ("pyproject.toml", '[tool.pytest.ini_options]\naddopts = ["--junitxml=..\\\\..\\\\out\\\\x.xml"]\n', "'..'"),
    ("pyproject.toml", '[tool.pytest.ini_options]\ntestpaths = ["..\\\\..\\\\elsewhere"]\n', "setting testpaths"),
    ("tox.ini", "[pytest]\ncache_dir = ../../cache\n", "tox.ini, setting cache_dir"),
    ("setup.cfg", "[tool:pytest]\npythonpath = src ../outside\n", "setup.cfg, setting pythonpath"),
    ("setup.cfg", "[tool:pytest]\naddopts =\n    -q\n    @more_args\n", "reads more arguments"),
    ("pyproject.toml", "[tool.pytest.ini_options]\nlog_file = \"C:/logs/x.log\"\n", "setting log_file"),
    ("pyproject.toml", "[tool.pytest]\naddopts = [\"-q\", \"--basetemp=/tmp/x\"]\n", "setting addopts"),
    ("pyproject.toml", "[tool.pytest.ini_options]\ntestpaths = [\"tests\", \"~/elsewhere\"]\n", "home directory"),
    ("pytest.toml", "[pytest]\naddopts = [\"-o\", \"addopts=-x\"]\n", "addopts override"),
    ("pytest.ini", "addopts = -q\n[pytest]\naddopts = -x\n", "cannot be read"),  # a key before any section
])
def test_pytest_config_files_are_checked(tmp_path, name, text, why):
    copy = tmp_path / "copy"
    copy.mkdir()
    (copy / name).write_text(text, encoding="utf-8")
    problem = experiments.pytest_config_problem(copy, PYTEST)
    assert problem and why in problem, problem


def test_pytest_config_files_that_stay_inside_pass(tmp_path):
    copy = tmp_path / "copy"
    (copy / "sub" / "tests").mkdir(parents=True)
    (copy / "pyproject.toml").write_text("[tool.black]\nline-length = 100\n[tool.pytest.ini_options]\n"
                                         "addopts = \"-q -p no:randomly\"\ntestpaths = [\"tests\"]\n"
                                         "cache_dir = \".cache\"\n", encoding="utf-8")
    (copy / "sub" / "pytest.ini").write_text("[pytest]\npythonpath = ../src\n", encoding="utf-8")  # still inside
    (copy / "tox.ini").write_text("[tox]\nenvlist = py312\n", encoding="utf-8")  # no pytest section
    assert experiments.pytest_config_problem(copy, [*PYTEST, "sub/tests"]) is None


def test_the_config_files_pytest_reads_are_the_ones_checked(tmp_path):
    copy = tmp_path / "copy"
    (copy / "a" / "b").mkdir(parents=True)
    (copy / "other").mkdir()
    (copy / "a" / "pytest.ini").write_text("[pytest]\naddopts = --basetemp=/x\n", encoding="utf-8")
    (copy / "other" / "pytest.ini").write_text("[pytest]\naddopts = --basetemp=/x\n", encoding="utf-8")
    (copy / "custom.cfg").write_text("[tool:pytest]\naddopts = --basetemp=/y\n", encoding="utf-8")
    assert experiments.pytest_config_problem(copy, PYTEST) is None  # neither is on the way to an argument
    assert "a/pytest.ini" in experiments.pytest_config_problem(copy, [*PYTEST, "a/b"])
    assert "custom.cfg" in experiments.pytest_config_problem(copy, [*PYTEST, "-c", "custom.cfg"])
    # `-o addopts=` switches the files' addopts off: nothing of it is used
    assert experiments.pytest_config_problem(copy, [*PYTEST, "-o", "addopts=", "a/b"]) is None


def test_toml_without_a_toml_parser_is_read_conservatively(tmp_path, monkeypatch):
    """Python 3.10 without tomli: every assignment of a key pytest reads, as the strings in its value."""
    monkeypatch.setattr(experiments, "_load_toml", lambda text: None)
    copy = tmp_path / "copy"
    copy.mkdir()
    (copy / "pyproject.toml").write_text('[tool.pytest.ini_options]\naddopts = [\n  "-q",\n  # comment\n'
                                         '  "--junitxml=/tmp/x.xml",\n]\n', encoding="utf-8")
    assert "absolute path" in experiments.pytest_config_problem(copy, PYTEST)
    (copy / "pyproject.toml").write_text('[tool.pytest.ini_options]\naddopts = """\n-q\n-x\n"""\n'
                                         'testpaths = ["tests"]\n', encoding="utf-8")
    assert experiments.pytest_config_problem(copy, PYTEST) is None
    (copy / "pyproject.toml").write_text("[tool.pytest]\ncache_dir = '../../c'\n", encoding="utf-8")
    assert "'..'" in experiments.pytest_config_problem(copy, PYTEST)


def test_a_config_file_that_leaves_the_copy_refuses_the_run(proj, tmp_path, no_container):
    repo, st = proj
    outside = tmp_path / "outside"
    outside.mkdir()
    (repo / "pytest.ini").write_text(f"[pytest]\naddopts = --junitxml={(outside / 'x.xml').as_posix()}\n",
                                     encoding="utf-8")
    with pytest.raises(experiments.ExperimentRefused, match="pytest.ini, setting addopts") as exc:
        experiments.run(st, repo, [*PYTEST, "tests/test_pricing.py"], hypothesis="h")
    assert "that setting" in exc.value.next_step
    assert not (outside / "x.xml").exists()
    rows = st.all("SELECT * FROM experiments")
    assert [r["status"] for r in rows] == ["refused"] and rows[0]["environment"]["policy"]["kind"] == "risky"
    assert not runs_dir(repo).exists() or not any(runs_dir(repo).iterdir())


def test_a_plugin_in_the_projects_own_config_is_its_choice_one_in_a_named_file_is_checked(tmp_path):
    """Review D63 M2: `addopts = -p pytester` in the config pytest finds itself is the trusted project's own
    choice (like pytest_plugins in conftest.py); a file the command names with -c (any file of the copy, a test
    fixture too) still gets the -p rule, in every spelling of -c."""
    copy = tmp_path / "copy"
    (copy / "sub").mkdir(parents=True)
    (copy / "pytest.ini").write_text("[pytest]\naddopts = -p pytester\n", encoding="utf-8")
    (copy / "sub" / "evil.ini").write_text("[pytest]\naddopts = -pzzd63_via_qc\n", encoding="utf-8")
    assert experiments.pytest_config_problem(copy, PYTEST) is None
    assert experiments.pytest_config_problem(copy, [*PYTEST, "-c", "pytest.ini"]) is None  # the one it finds
    for spelling in (["-c", "sub/evil.ini"], ["-csub/evil.ini"], ["--config-file=sub/evil.ini"],
                     ["-qcsub/evil.ini"], ["-qc", "sub/evil.ini"], ["-q=csub/evil.ini"]):
        problem = experiments.pytest_config_problem(copy, [*PYTEST, *spelling])
        assert problem and "sub/evil.ini, setting addopts" in problem and "loads a plugin module" in problem, \
            (spelling, problem)
        assert "-c" in experiments._config_next_step(problem)
    # the other rules still apply to the project's own file
    (copy / "pytest.ini").write_text("[pytest]\naddopts = -p pytester --junitxml=/x.xml\n", encoding="utf-8")
    assert "absolute path" in experiments.pytest_config_problem(copy, PYTEST)


def test_a_trusted_project_whose_addopts_loads_a_plugin_runs(proj, no_container):
    repo, st = proj
    (repo / "pytest.ini").write_text("[pytest]\naddopts = -p pytester\n", encoding="utf-8")
    res = experiments.run(st, repo, [*PYTEST, "tests/test_pricing.py"], hypothesis="h")
    assert res["outcome"] == "pass" and res["isolation"] == "process", res


def test_an_environment_variable_in_an_argument_writes_nothing_outside(proj, tmp_path, monkeypatch, no_container):
    """Review D63 H1, end to end: LANG is passed to the child, and pytest expands it in --junitxml."""
    repo, st = proj
    outside = tmp_path / "OUTSIDE"
    outside.mkdir()
    monkeypatch.setenv("LANG", str(outside))
    with pytest.raises(experiments.ExperimentRefused, match="environment variable"):
        experiments.run(st, repo, [*PYTEST, "--junitxml=%LANG%/escaped_by_argv.xml", "tests/test_pricing.py"],
                        hypothesis="h")
    (repo / "pytest.ini").write_text("[pytest]\naddopts = --junitxml=$LANG/escaped_by_ini.xml\n", encoding="utf-8")
    with pytest.raises(experiments.ExperimentRefused, match="pytest.ini, setting addopts") as exc:
        experiments.run(st, repo, [*PYTEST, "tests/test_pricing.py"], hypothesis="h")
    assert "environment variable" in str(exc.value) and "that setting" in exc.value.next_step
    assert list(outside.iterdir()) == []


def test_a_config_file_named_in_a_cluster_is_checked_before_the_run(proj, no_container):
    """Review D63 M1.3: `-qcsub/evil.ini` makes pytest read that file; it was never checked."""
    repo, st = proj
    (repo / "sub").mkdir()
    (repo / "sub" / "evil.ini").write_text("[pytest]\naddopts = -pzzd63_via_qc\n", encoding="utf-8")
    with pytest.raises(experiments.ExperimentRefused, match="loads a plugin module") as exc:
        experiments.run(st, repo, [*PYTEST, "-qcsub/evil.ini", "tests/test_pricing.py"], hypothesis="h")
    assert "-c" in exc.value.next_step and "that setting to a path" not in exc.value.next_step


@pytest.mark.parametrize("extra,loads", [
    (["-qoaddopts=-pzzd63_probe_plugin"], True),
    (["--", "-pzzd63_probe_plugin"], True),
    (["-qcsub/evil.ini"], True),
    (["-qpzzd63_probe_plugin"], False),
])
def test_pytest_itself_reads_these_spellings(tmp_path, extra, loads):
    """The rules above follow how pytest reads its arguments; if a pytest release changes that, this says so."""
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "evil.ini").write_text("[pytest]\naddopts = -pzzd63_probe_plugin\n", encoding="utf-8")
    (tmp_path / "test_one.py").write_text("def test_one():\n    pass\n", encoding="utf-8")
    r = subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "--co", "test_one.py", *extra],
                       cwd=tmp_path, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
    assert ("zzd63_probe_plugin" in r.stdout + r.stderr) is loads, r.stdout + r.stderr


def test_the_toml_fallback_splits_strings_and_keeps_list_items(tmp_path, monkeypatch):
    monkeypatch.setattr(experiments, "_load_toml", lambda text: None)
    copy = tmp_path / "copy"
    copy.mkdir()
    (copy / "custom.toml").write_text('[tool.pytest.ini_options]\naddopts = "-q -p evil_plugin"\n', encoding="utf-8")
    assert "loads a plugin module" in experiments.pytest_config_problem(copy, [*PYTEST, "-c", "custom.toml"])
    (copy / "pyproject.toml").write_text('[tool.pytest.ini_options]\naddopts = ["--junitxml=..\\\\..\\\\x.xml"]\n',
                                         encoding="utf-8")
    assert "'..'" in experiments.pytest_config_problem(copy, PYTEST)


def test_pytest_extra_arguments():
    scratch = Path("/w/_pytest")
    out = experiments._pytest_extra(["python", "-m", "pytest", "-q", "--", "t.py"], scratch)
    assert out[:4] == ["python", "-m", "pytest", "-q"] and out[-2:] == ["--", "t.py"]
    assert out[4:6] == ["-p", "no:cacheprovider"] and out[6] == f"--basetemp={scratch / 'basetemp'}"
    out = experiments._pytest_extra(["pytest", "-p", "no:cacheprovider"], scratch)
    assert out.count("no:cacheprovider") == 1
    out = experiments._pytest_extra(["pytest", "--lf"], scratch)  # the cache plugin is needed: moved, not off
    assert "no:cacheprovider" not in out and f"cache_dir={scratch / 'cache'}" in out


def test_pytest_runs_with_its_temporary_folders_in_the_throw_away_directory(proj):
    repo, st = proj
    (repo / "tests" / "test_where.py").write_text(
        "import os, pathlib\n\n"
        "def test_tmp_path_is_in_the_throw_away_dir(tmp_path):\n"
        "    work = pathlib.Path(os.environ['VERINODA_ARTIFACTS']).parent.resolve()\n"
        "    assert work in tmp_path.resolve().parents, (work, tmp_path)\n", encoding="utf-8")  # macOS: /var link
    res = experiments.run(st, repo, ["python", "-m", "pytest", "-q", "tests/test_where.py"], hypothesis="h")
    assert res["outcome"] == "pass", res["summary"]
    env = st.get("experiments", res["id"])["environment"]
    assert "-p" in env["argv"] and any(a.startswith("--basetemp=") for a in env["argv"])
    assert st.get("experiments", res["id"])["command"] == ["python", "-m", "pytest", "-q", "tests/test_where.py"]


# -- R10: the copy never follows a symbolic link or a junction ------------------------------------------------------

def _link_dir(link: Path, target: Path) -> bool:
    try:
        if os.name == "nt":
            import _winapi

            _winapi.CreateJunction(str(target), str(link))
        else:
            os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, AttributeError, NotImplementedError):
        return False


def test_the_copy_skips_symlinks_and_junctions(proj, tmp_path):
    repo, st = proj
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("not the project's", encoding="utf-8")
    if not _link_dir(repo / "linked", outside):
        pytest.skip("this system cannot create a directory link here")
    dst = tmp_path / "dst"
    ids, skipped = {}, []
    n = experiments._copy_repo(repo, dst, ids, skipped)
    assert not (dst / "linked").exists() and "linked/secret.txt" not in ids
    # a junction's files are listed one by one; git lists a POSIX symlink to a folder as one entry
    # what matters: nothing behind the link enters the copy. How it is left out depends on the listing: a
    # junction's files are listed one by one and skipped; git lists a POSIX symlink to a folder as one entry,
    # which the working-tree listing drops as not a file (then nothing is listed as skipped)
    got = [s["path"] for s in skipped]
    assert got in (["linked/secret.txt"], ["linked"], []) and n == len(ids) > 0
    assert not any(k == "linked" or k.startswith("linked/") for k in ids)
    assert not (dst / "linked").exists()
    res = experiments.run(st, repo, [*PYTEST, "tests/test_pricing.py"], hypothesis="h")
    assert res["source"].get("skipped_total", 0) == len(got)  # the key is there only when something was left out
    assert not got or any("not followed" in lim for lim in res["limits"])


def test_through_links_finds_nothing_in_an_ordinary_tree(proj):
    repo, _ = proj
    from verinoda.snapshot import list_files

    assert experiments._through_links(repo, list_files(repo)) == set()
