"""Targeted experiments with real, stated isolation.

Two isolation levels exist, and the one used is recorded on every run:

``process`` (always available)
    * separate process in its own process group/session;
    * wall-clock timeout that kills the whole process tree;
    * working directory is a throw-away *copy* of the repository
      (tracked + untracked-not-ignored files), so the user's tree is never
      written to;
    * environment rebuilt from an allowlist - API keys, tokens and other
      secrets from the caller's environment are not passed;
    * POSIX only: CPU-time, address-space and file-size rlimits.
    It does NOT isolate the network, and it does NOT confine the child's file
    system access: the command's own path arguments are checked (see Policy),
    but the code it runs - the project's tests - can still read and write
    anywhere the user can. ``guarantees`` in every result says exactly this
    (``fs_reads_confined`` / ``fs_writes_confined`` are false).

``container`` (docker or podman on PATH)
    ``--network none``, memory/CPU/pid limits, read-write mount of the copy only;
    the container is named ``repoatlas-<experiment id>`` and killed by name on
    timeout (killing the CLI client alone would leave it running).

Policy (:func:`policy`): only commands matching the configured test-runner
allowlist and free of shell metacharacters may run under ``process``
isolation. An absolute path to a Python interpreter (e.g. the project's
``.venv``, see :func:`python_for`) counts as ``python``. Every *argument* of an
allowlisted command must stay inside the repository copy: absolute paths,
home-relative paths, URLs and ``..`` escapes are rejected wherever they appear
(positional test paths, ``--opt=value``, ``-o key=value``, ``type:path``
values such as ``--cov-report=xml:/x``, attached short options such as
``-c/x``), and runner options that execute arbitrary code or turn arguments
into outside imports (``pytest --pyargs``, ``node -e``, ``go test -exec``,
``cargo --config``, ``npm --script-shell``) are rejected. Anything else needs
``container`` isolation; without it the experiment is *refused*, not run, and
the refusal names the offending argument. Commands are never passed through a
shell, and no child inherits RepoAtlas's stdin (an MCP server's stdin is its
protocol pipe; a child waiting on it hangs).

Plugins and artifacts: internal callers (the runtime tracer,
:mod:`repoatlas.runtime.trace`) may add Python modules that are written
*next to* the copy and put on ``PYTHONPATH`` (``plugins``), ``REPOATLAS_*``
environment variables (``env_extra``), and collect files the child writes to
``$REPOATLAS_ARTIFACTS``; those files are moved to ``runs/<id>/artifacts/``
before the throw-away copy is deleted.

Outcomes: ``pass`` (exit 0), ``fail`` (exit 1, or any non-zero exit of a
non-pytest runner), ``timeout``, and ``inconclusive`` - pytest could not tell
(exit 2 interrupted/collection error, 3 internal error, 4 usage error, 5 no
tests collected, or pytest is not installed for that interpreter). An
inconclusive run is recorded, but its evidence only *qualifies* a claim: it
never supports or refutes it.
"""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path, PurePosixPath

from repoatlas import evidence as evmod
from repoatlas.paths import load_config, runs_dir
from repoatlas.snapshot import list_files
from repoatlas.store import Store, new_id, now

SHELL_META = re.compile(r"[;&|<>`$\n]|\$\(")
PYTHON_EXE_RE = re.compile(r"^python(\d+(\.\d+)*)?$")
NO_PYTEST_RE = re.compile(r"No module named '?pytest'?(?![\w.])")
PYTEST_INCONCLUSIVE = {2: "pytest was interrupted (e.g. an error during collection)", 3: "pytest internal error",
                       4: "pytest usage error (bad arguments or paths)", 5: "pytest collected no tests"}
ENV_ALLOW = {"PATH", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "LANG", "LC_ALL",
             "TZ", "TERM", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS"}
SUMMARY_RE = re.compile(
    r"(=+ .*(passed|failed|error).* =+"
    # pytest -q prints the bare summary: "3 passed in 0.17s", "1 failed, 2 passed in ..."
    r"|^\d+ (passed|failed|errors?|skipped|xfailed|xpassed|deselected)\b.*\bin [\d.]+s.*$"
    r"|^(ok|FAIL|PASS)\b.*|test result:.*|Tests?:\s+\d+.*)", re.I | re.M)
KILL_DRAIN_TIMEOUT = 15  # seconds to collect output after killing a timed-out tree
RELEVANT_RE = re.compile(r"(FAILED|ERROR|Error|Traceback|assert|panic|exception)", re.I)
MAX_LOG_BYTES = 5 * 1024 * 1024
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024  # per collected artifact file
PLUGIN_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\.py$")
ENV_EXTRA_RE = re.compile(r"^REPOATLAS_[A-Z0-9_]+$")
ARGS_CONFINED = "allowlisted commands may only name paths inside the repository copy"
# Runner options that run arbitrary code or import from outside the copy, whatever their value.
FORBIDDEN_OPTIONS: dict[str, dict[str, str]] = {
    "pytest": {"--pyargs": "--pyargs turns arguments into importable package names that can live outside "
                           "the repository copy"},
    "node": {opt: f"node {opt} evaluates inline code" for opt in ("-e", "--eval", "-p", "--print", "-i",
                                                                 "--interactive")},
    "go": {"-exec": "go test -exec runs an arbitrary program", "-toolexec": "go -toolexec runs an arbitrary program"},
    "cargo": {"--config": "cargo --config can set an arbitrary runner program", "-Z": "cargo -Z enables unstable "
                                                                                     "behaviour"},
    "npm": {"--script-shell": "npm --script-shell runs an arbitrary shell"},
}
_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_URL_RE = re.compile(r"^[A-Za-z][\w+.-]*://")
_TYPED_VALUE_RE = re.compile(r"^[\w.-]{2,}:(.*)$", re.S)  # xml:path, no:plugin (2+ chars: not a drive)


class ExperimentRefused(RuntimeError):
    pass


def python_for(repo: Path) -> str:
    """The interpreter to run a project's tests with.

    The project's own virtualenv when it has one (``.venv`` then ``venv``;
    Windows ``Scripts/python.exe`` or POSIX ``bin/python``), else the
    interpreter running RepoAtlas. Returned as an absolute path, which the
    process-isolation allowlist accepts as ``python``.
    """
    repo = Path(repo).resolve()
    for env in (".venv", "venv"):
        for rel in (("Scripts", "python.exe"), ("bin", "python")):
            p = repo.joinpath(env, *rel)
            if p.is_file():
                return str(p)
    return sys.executable


def _strip_quotes(tok: str) -> str:
    return tok[1:-1] if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in "\"'" else tok


def _arg0_path(arg0: str) -> PurePosixPath:
    """argv[0] as a path, the same on every OS (quotes stripped, backslashes as separators)."""
    return PurePosixPath(_strip_quotes(arg0).replace("\\", "/"))


def _exe_name(arg0: str) -> str:
    """Normalised runner name: basename, lower-case, no .exe; interpreters -> 'python'."""
    name = _arg0_path(arg0).name.lower().removesuffix(".exe")
    return "python" if PYTHON_EXE_RE.match(name) else name


def _is_pytest(argv: list[str]) -> bool:
    if not argv:
        return False
    exe = _exe_name(argv[0])
    return exe == "pytest" or (exe in ("python", "py") and argv[1:3] == ["-m", "pytest"])


def container_runtime() -> str | None:
    for rt in ("docker", "podman"):
        if shutil.which(rt):
            try:
                r = subprocess.run([rt, "info"], capture_output=True, timeout=15, stdin=subprocess.DEVNULL)
                if r.returncode == 0:
                    return rt
            except (OSError, subprocess.TimeoutExpired):
                continue
    return None


def path_escape(value: str) -> str | None:
    """Why ``value`` names a location outside the repository copy, else None.

    Purely lexical, so it is the same on every OS and needs no file system:
    the copy never contains symlinks (files are copied by content), so a
    path that stays inside lexically stays inside for real.
    """
    v = _strip_quotes(value).strip()
    if not v:
        return None
    s = v.replace("\\", "/")
    if s.startswith("~"):
        return f"{value!r} is relative to the home directory"
    if _DRIVE_RE.match(s) or s.startswith("/"):
        return f"{value!r} is an absolute path"
    if _URL_RE.match(s):
        return f"{value!r} is a URL"
    depth = 0
    for part in s.split("/"):
        if part == "..":
            depth -= 1
            if depth < 0:
                return f"{value!r} leaves the repository copy through '..'"
        elif part not in ("", "."):
            depth += 1
    return None


def _path_candidates(tok: str) -> list[str]:
    """Every sub-string of an argument that a runner may treat as a path."""
    out = [tok]
    if tok.startswith("-") and "=" in tok:            # --opt=value
        out.append(tok.split("=", 1)[1])
    if re.match(r"^-[A-Za-z].", tok) and not tok.startswith("--"):
        out.append(tok[2:])                          # attached short option value: -c/x, -oa=b
    for c in list(out):
        if "=" in c and not c.startswith("-"):      # ini override key=value (-o cache_dir=/x)
            out.append(c.split("=", 1)[1])
    for c in list(out):
        m = _TYPED_VALUE_RE.match(c)                 # type:path (--cov-report=xml:/x)
        if m and not _URL_RE.match(c):
            out.append(m.group(1))
    return out


def _runner(argv: list[str]) -> str:
    exe = _exe_name(argv[0])
    return "pytest" if _is_pytest(argv) else exe


def policy(argv: list[str], allowlist: list[str]) -> tuple[str, str | None]:
    """``('allowlisted', None)`` or ``('risky', why)``.

    Allowlisted means: argv starts with an allowlisted test runner, has no
    shell metacharacters, no forbidden runner option, and no argument that
    names a path outside the repository copy (see the module docstring).
    """
    if not argv:
        return "risky", "empty command"
    joined = " ".join(argv)
    if SHELL_META.search(joined):
        return "risky", "the command contains shell metacharacters"
    raw = _arg0_path(argv[0]).name.lower().removesuffix(".exe")
    matched = False
    for exe in dict.fromkeys((raw, _exe_name(argv[0]))):
        norm = " ".join([exe, *argv[1:]])
        if any(norm == p or norm.startswith(p + " ") for p in allowlist):
            matched = True
            break
    if not matched:
        return "risky", f"{' '.join(argv[:3])!r} does not start with an allowlisted test runner"
    forbidden = FORBIDDEN_OPTIONS.get(_runner(argv), {})
    for tok in argv[1:]:
        opt = _strip_quotes(tok).split("=", 1)[0]
        if opt in forbidden:
            return "risky", forbidden[opt]
        for cand in _path_candidates(_strip_quotes(tok)):
            why = path_escape(cand)
            if why:
                return "risky", f"argument {tok!r}: {why}; {ARGS_CONFINED}"
    return "allowlisted", None


def classify(argv: list[str], allowlist: list[str]) -> str:
    """'allowlisted' or 'risky' - see :func:`policy` for the reason."""
    return policy(argv, allowlist)[0]


def _scrubbed_env(home: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k.upper() in ENV_ALLOW}
    env.update({"HOME": str(home), "USERPROFILE": str(home), "TMP": str(home), "TEMP": str(home),
                "PYTHONDONTWRITEBYTECODE": "1", "PYTHONHASHSEED": "0", "CI": "1",
                "NO_COLOR": "1", "REPOATLAS_EXPERIMENT": "1"})
    # Keep the interpreter that runs RepoAtlas reachable (tests need pytest etc.).
    env["PATH"] = os.pathsep.join([str(Path(sys.executable).parent), env.get("PATH", "")])
    return env


def _copy_repo(repo: Path, dst: Path) -> int:
    n = 0
    for rel in list_files(repo):
        src = repo / rel
        out = dst / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(src, out)
            n += 1
        except OSError:
            continue
    return n


def _posix_limits(cpu_s: int, mem_mb: int):  # pragma: no cover - POSIX only
    def apply():
        import resource

        os.setsid()
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_s, cpu_s))
        resource.setrlimit(resource.RLIMIT_AS, (mem_mb << 20, mem_mb << 20))
        resource.setrlimit(resource.RLIMIT_FSIZE, (256 << 20, 256 << 20))
    return apply


def _kill_container(runtime: str, name: str) -> None:
    """Stop a timed-out container by name; killing the docker/podman client does not."""
    try:
        subprocess.run([runtime, "kill", name], capture_output=True, timeout=30, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _container_argv(argv: list[str]) -> list[str]:
    """A host interpreter path means nothing inside the image: use its ``python``."""
    if argv and _exe_name(argv[0]) == "python" and len(_arg0_path(argv[0]).parts) > 1:
        return ["python", *argv[1:]]
    return argv


def _classify_outcome(argv: list[str], code: int | None, timed_out: bool, stdout: str,
                      stderr: str) -> tuple[str, str | None]:
    """(outcome, why) - see the module docstring for the outcome rules."""
    if timed_out:
        return "timeout", None
    if code == 0:
        return "pass", None
    if _is_pytest(argv):
        if NO_PYTEST_RE.search(stderr) or NO_PYTEST_RE.search(stdout[-2000:]):
            return "inconclusive", "pytest is not installed for this interpreter (No module named pytest)"
        if code in PYTEST_INCONCLUSIVE:
            return "inconclusive", f"pytest exit code {code}: {PYTEST_INCONCLUSIVE[code]}"
    return "fail", None


def _kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True,
                       stdin=subprocess.DEVNULL)
    else:  # pragma: no cover
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def _collect_artifacts(src: Path, dst: Path) -> dict[str, str]:
    """Move the regular files the child left in ``src`` to ``dst`` (flat, size-capped)."""
    out: dict[str, str] = {}
    try:
        entries = sorted(src.iterdir())
    except OSError:
        return out
    for p in entries:
        try:
            if not p.is_file() or p.is_symlink() or p.stat().st_size > MAX_ARTIFACT_BYTES:
                continue
            dst.mkdir(parents=True, exist_ok=True)
            target = dst / p.name
            shutil.copyfile(p, target)
            out[p.name] = str(target)
        except OSError:
            continue
    return out


def _summarize(stdout: str, stderr: str, exit_code: int | None, timed_out: bool) -> dict:
    text = stdout + "\n" + stderr
    lines = text.splitlines()
    summary = [m.group(0).strip() for m in SUMMARY_RE.finditer(text)][-3:]
    relevant = [l for l in lines if RELEVANT_RE.search(l)][:15]
    return {"exit_code": exit_code, "timed_out": timed_out, "summary_lines": summary,
            "relevant_lines": [l[:200] for l in relevant], "tail": [l[:200] for l in lines[-8:]],
            "total_lines": len(lines)}


def run(
    store: Store, repo: Path, argv: list[str] | str, *, hypothesis: str, expect: str = "pass",
    timeout: float | None = None, claim_id: str | None = None, isolation: str = "auto",
    commit: str | None = None, plugins: dict[str, bytes] | None = None,
    env_extra: dict[str, str] | None = None,
) -> dict:
    """Run one experiment and record it (and its evidence) in the store.

    ``plugins`` ({module file name: source bytes}) are written next to the
    copy, never into it, and that directory is put on ``PYTHONPATH`` (load
    them with ``-p <module>``). ``env_extra`` adds ``REPOATLAS_*`` variables
    only. Files the child writes to ``$REPOATLAS_ARTIFACTS`` are returned in
    ``result["artifacts"]`` ({name: path under runs/<id>/artifacts}).
    """
    repo = Path(repo).resolve()
    cfg = load_config(repo)["experiments"]
    if isinstance(argv, str):
        argv = shlex.split(argv, posix=(os.name != "nt"))
        if os.name == "nt":
            argv = [_strip_quotes(a) for a in argv]
    for name in plugins or {}:
        if not PLUGIN_NAME_RE.match(name):
            raise ValueError(f"plugin file name must be a plain module file name, not {name!r}")
    for key in env_extra or {}:
        if not ENV_EXTRA_RE.match(key):
            raise ValueError(f"env_extra may only set REPOATLAS_* variables, not {key!r}")
    timeout = float(timeout or cfg["default_timeout"])
    kind, why_risky = policy(argv, cfg["process_isolation_allowlist"])
    runtime = container_runtime() if isolation in ("auto", "container") else None
    if isolation == "container" and not runtime:
        level = None
    elif kind == "allowlisted" and isolation in ("auto", "process"):
        level = "process"
    elif runtime:
        level = "container"
    else:
        level = None
    eid = new_id("exp")
    base = {
        "id": eid, "hypothesis": hypothesis, "command": argv, "timeout_s": timeout,
        "claim_id": claim_id, "created_at": now(),
    }
    if level is None:
        if isolation == "container" and not runtime:
            reason = "container isolation was requested and no container runtime (docker/podman) is available"
        else:
            reason = (f"command classified '{kind}' and no container runtime (docker/podman) is available; "
                      "RepoAtlas does not run non-allowlisted commands with process isolation only")
        if why_risky:
            reason = f"{reason} (why '{kind}': {why_risky})"
        store.insert("experiments", {**base, "cwd": str(repo), "isolation": "none",
                                     "status": "refused", "summary": reason,
                                     "environment": {"policy": {"kind": kind, "reason": why_risky}}})
        raise ExperimentRefused(reason)

    out_dir = runs_dir(repo) / eid
    out_dir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="repoatlas-exp-"))
    home = work / "_home"
    home.mkdir()
    copy = work / "repo"
    copied = _copy_repo(repo, copy)
    env = _scrubbed_env(home)
    artifacts_dir = work / "_artifacts"
    artifacts_dir.mkdir()
    plugins_dir = work / "_plugins"
    if plugins:
        plugins_dir.mkdir()
        for name, data in plugins.items():
            (plugins_dir / name).write_bytes(data)
    guarantees = {
        "separate_process": True, "timeout_kills_tree": True, "cwd_is_copy": True,
        "env_allowlisted": True, "resource_limits": os.name != "nt" or level == "container",
        "network_isolated": level == "container", "fs_reads_confined": level == "container",
        # Process isolation checks the command's own path arguments (policy), but the tests it
        # runs are arbitrary code and can still write anywhere the user can.
        "fs_writes_confined": level == "container",
        "path_args_confined": kind == "allowlisted" or level == "container",
    }
    limits = (["the network is not isolated",
               "the tests run as the user: they can read and write files outside the copy"]
              if level == "process" else [])
    container_name = f"repoatlas-{eid}"
    if level == "container":
        extra: list[str] = []
        for k, v in (env_extra or {}).items():
            extra += ["-e", f"{k}={v}"]
        extra += ["-v", f"{artifacts_dir}:/artifacts", "-e", "REPOATLAS_ARTIFACTS=/artifacts"]
        if plugins:
            extra += ["-v", f"{plugins_dir}:/plugins:ro", "-e", "PYTHONPATH=/plugins"]
        cmd = [runtime, "run", "--rm", "--name", container_name, "--network", "none", "--memory", "1g",
               "--cpus", "1", "--pids-limit", "256", "-v", f"{copy}:/work", "-w", "/work", *extra,
               cfg.get("container_image", "python:3.12-slim"), *_container_argv(argv)]
    else:
        env.update(env_extra or {})
        env["REPOATLAS_ARTIFACTS"] = str(artifacts_dir)
        if plugins:
            env["PYTHONPATH"] = str(plugins_dir)
        cmd = argv
        exe = shutil.which(cmd[0], path=env["PATH"])
        if exe:
            cmd = [exe, *cmd[1:]]
    t0 = time.monotonic()
    kwargs: dict = {"cwd": str(copy), "env": env, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
                    "stdin": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:  # pragma: no cover
        kwargs["preexec_fn"] = _posix_limits(int(timeout) + 5, 2048)
    timed_out = False
    try:
        proc = subprocess.Popen(cmd, **kwargs)
    except OSError as exc:
        shutil.rmtree(work, ignore_errors=True)
        store.insert("experiments", {**base, "cwd": str(copy), "isolation": level, "status": "error",
                                     "summary": f"could not start: {exc}", "environment": guarantees})
        raise
    try:
        so, se = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        if level == "container":
            _kill_container(runtime, container_name)
        _kill_tree(proc)
        try:
            so, se = proc.communicate(timeout=KILL_DRAIN_TIMEOUT)
        except subprocess.TimeoutExpired:
            # A descendant escaped the tree kill and still holds the pipes;
            # do not let it turn the timeout into a hang.
            so, se = b"", b"[repoatlas] output unavailable: a descendant process kept the pipes open after kill"
    duration = time.monotonic() - t0
    stdout = so[:MAX_LOG_BYTES].decode("utf-8", "replace")
    stderr = se[:MAX_LOG_BYTES].decode("utf-8", "replace")
    # Bytes, not write_text: on Windows the child's "\r\n" would become "\r\r\n".
    (out_dir / "stdout.txt").write_bytes(stdout.encode("utf-8"))
    (out_dir / "stderr.txt").write_bytes(stderr.encode("utf-8"))
    artifacts = _collect_artifacts(artifacts_dir, out_dir / "artifacts")
    shutil.rmtree(work, ignore_errors=True)
    code = None if timed_out else proc.returncode
    summ = _summarize(stdout, stderr, code, timed_out)
    outcome, why = _classify_outcome(argv, code, timed_out, stdout, stderr)
    inconclusive = outcome == "inconclusive"
    matches = (expect == "pass" and outcome == "pass") or (expect == "fail" and outcome == "fail")
    is_test = kind == "allowlisted"
    ev = {
        "source_type": "test_result" if is_test else "experiment",
        "locator": f"run {eid}: {' '.join(argv)}",
        "path": None, "commit_sha": commit,
        "content_hash": "sha256:" + hashlib.sha256((stdout + stderr).encode()).hexdigest(),
        "excerpt": " | ".join(([why] if why else []) + (summ["summary_lines"] or summ["tail"][-2:]))[:400],
        "meta": {"experiment_id": eid,
                 "outcome": "inconclusive" if inconclusive else ("pass" if matches else "fail"),
                 "raw_outcome": outcome, "expect": expect, "exit_code": code, "isolation": level,
                 **({"inconclusive_reason": why} if why else {}),
                 "stdout": str(out_dir / "stdout.txt"), "stderr": str(out_dir / "stderr.txt")},
    }
    ev_id = evmod.add(store, ev)
    store.insert("experiments", {
        **base, "cwd": f"copy of {repo} ({copied} files)", "isolation": level,
        "environment": {"guarantees": guarantees, "limits": limits, "python": sys.version.split()[0],
                        "platform": sys.platform, **({"plugins": sorted(plugins)} if plugins else {})},
        "exit_code": code, "duration_s": round(duration, 3), "timed_out": int(timed_out),
        "status": outcome, "summary": "; ".join(([why] if why else []) + summ["summary_lines"]) or None,
        "stdout_path": str(out_dir / "stdout.txt"), "stderr_path": str(out_dir / "stderr.txt"),
        "evidence_id": ev_id,
    })
    if claim_id:
        from repoatlas.claims import Claims

        # An inconclusive run says nothing about the hypothesis: traceable, never support or refutation.
        relation = "qualifies" if inconclusive else ("supports" if matches else "refutes")
        Claims(store, repo).attach(claim_id, ev_id, relation,
                                   note=f"experiment {eid}: expected {expect}, got {outcome}"
                                        + (f" ({why})" if why else ""))
    res = {"id": eid, "isolation": level, "guarantees": guarantees, "outcome": outcome,
           "matches_expectation": matches, "duration_s": round(duration, 3), "evidence_id": ev_id,
           "summary": summ, "logs": {"stdout": str(out_dir / "stdout.txt"), "stderr": str(out_dir / "stderr.txt")}}
    if limits:
        res["limits"] = limits
    if artifacts:
        res["artifacts"] = artifacts
    if why:
        res["inconclusive_reason"] = why
        res["next_step"] = ("install pytest in the project's environment (or create .venv) and re-run"
                            if "not installed" in why else "check the test ids/arguments and re-run")
    return res
