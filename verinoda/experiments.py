"""Targeted experiments with real, stated isolation.

Two isolation levels exist, and the one used is recorded on every run:

``process`` (always available)
    * separate process in its own process group/session;
    * wall-clock timeout that kills the whole process tree;
    * processes the command leaves running (a server a test started) are
      stopped when it exits (Windows: a job object; POSIX: its session), so
      they cannot answer the next run; the run's limits say so;
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
    the container is named ``verinoda-<experiment id>`` and killed by name on
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
shell, and no child inherits Verinoda's stdin (an MCP server's stdin is its
protocol pipe; a child waiting on it hangs).

Source of the copy: the working tree (default), or - with ``ref`` - the
regular files of one commit, read with ``git cat-file`` like ``git archive``
would give them but without running smudge filters (``overlay`` can put
working-tree files such as the current tests on top; the run says so). Either
way the copy is made in a temp directory and the user's tree, index and
``.git`` are only read. Every run records the *tree identity* of what it ran
on (:mod:`verinoda.treestate`): a tree hash over the CRLF-normalised content
of each copied file, computed while copying.

Plugins and artifacts: internal callers (the runtime tracer,
:mod:`verinoda.runtime.trace`) may add Python modules that are written
*next to* the copy and put on ``PYTHONPATH`` (``plugins``), ``VERINODA_*``
environment variables (``env_extra``), and collect files the child writes to
``$VERINODA_ARTIFACTS``; those files are moved to ``runs/<id>/artifacts/``
before the throw-away copy is deleted.

Outcomes: ``pass`` (exit 0), ``fail`` (exit 1, or any non-zero exit of a
non-pytest runner), ``timeout``, and ``inconclusive`` - pytest could not tell
(exit 2 interrupted/collection error, 3 internal error, 4 usage error, 5 no
tests collected, or pytest is not installed for that interpreter), or a
runner exited 0 having run no test (node ``tests 0``, ``No tests found``,
unittest ``Ran 0 tests``, cargo/go with zero tests). An
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

from verinoda import evidence as evmod
from verinoda import treestate
from verinoda.paths import load_config, runs_dir
from verinoda.snapshot import list_files
from verinoda.store import Store, new_id, now

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
ORPHAN_GRACE_S = 2.0     # the command exited but a process it started holds its output: stopped after this
CREATE_SUSPENDED = 0x00000004  # Windows process creation flag
RELEVANT_RE = re.compile(r"(FAILED|ERROR|Error|Traceback|assert|panic|exception)", re.I)
MAX_LOG_BYTES = 5 * 1024 * 1024
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024  # per collected artifact file
COMMIT_COPY_BATCH = 2000  # blobs read per `git cat-file --batch` when copying a commit
COPY_THREADS = 8          # threads copying a working tree of COPY_PARALLEL_MIN files or more
COPY_PARALLEL_MIN = 200
PLUGIN_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\.py$")
ENV_EXTRA_RE = re.compile(r"^VERINODA_[A-Z0-9_]+$")
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


def refusal(repo: Path, exc: BaseException) -> dict:
    """The structured answer for a refused experiment (the CLI and the MCP tools give the same)."""
    allow = load_config(repo)["experiments"]["process_isolation_allowlist"]
    return {"status": "refused", "reason": str(exc),
            "limits": ["without docker/podman only allowlisted test runners run, and only with process "
                       "isolation (no network or filesystem confinement)",
                       f"allowlist (config experiments.process_isolation_allowlist): {', '.join(allow)}"],
            "next_step": "run the tests through an allowlisted runner with paths inside the repository, or "
                         "install docker/podman for container isolation"}


def python_for(repo: Path) -> str:
    """The interpreter to run a project's tests with.

    The project's own virtualenv when it has one (``.venv`` then ``venv``;
    Windows ``Scripts/python.exe`` or POSIX ``bin/python``), else the
    interpreter running Verinoda. Returned as an absolute path, which the
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
                "NO_COLOR": "1", "VERINODA_EXPERIMENT": "1"})
    # Keep the interpreter that runs Verinoda reachable (tests need pytest etc.).
    env["PATH"] = os.pathsep.join([str(Path(sys.executable).parent), env.get("PATH", "")])
    return env


def _copy_repo(repo: Path, dst: Path, ids: dict[str, str] | None = None) -> int:
    """Copy the working tree's file set; ``ids`` receives each copied file's content id.

    Bigger trees are copied by a few threads: per-file open/close (and on Windows
    the virus scanner) dominates, not bytes, so the files overlap.
    """
    rels = list_files(repo)
    for d in sorted({(dst / r).parent for r in rels}):
        d.mkdir(parents=True, exist_ok=True)

    def one(rel: str) -> tuple[str, str | None]:
        try:
            return rel, treestate.copy_file(repo / rel, dst / rel)
        except OSError:
            return rel, None

    if len(rels) >= COPY_PARALLEL_MIN:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=COPY_THREADS) as pool:
            done = list(pool.map(one, rels))
    else:
        done = [one(r) for r in rels]
    n = 0
    for rel, cid in done:
        if cid is None:
            continue
        n += 1
        if ids is not None:
            ids[rel] = cid
    return n


def _copy_commit(repo: Path, commit: str, dst: Path, ids: dict[str, str] | None = None,
                 skipped: list[dict] | None = None) -> int:
    """Write the regular files of ``commit`` (raw blobs: no filters, no line-ending conversion) into ``dst``.

    The user's work tree, index and ``.git`` are only read. Symlinks and
    submodules are not written, nor is any path naming git's directory in any
    spelling (see :func:`verinoda.treestate.commit_entries`). A file this OS
    cannot hold (``what?.md`` on Windows) is left out and listed in ``skipped``.
    """
    ents = treestate.commit_entries(repo, commit, skipped)
    n = 0
    for i in range(0, len(ents), COMMIT_COPY_BATCH):
        batch = ents[i:i + COMMIT_COPY_BATCH]
        data = treestate.read_blobs(repo, [oid for _, oid, _ in batch])
        for mode, oid, rel in batch:
            blob = data.get(oid)
            if blob is None or path_escape(rel):
                continue
            out = dst / rel
            try:
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(blob)
            except OSError as exc:
                if skipped is not None:
                    skipped.append({"path": rel, "why": f"could not be written here: {exc.strerror or exc}"})
                continue
            if mode == "100755" and os.name != "nt":  # pragma: no cover - POSIX
                os.chmod(out, 0o755)
            n += 1
            if ids is not None:
                ids[rel] = treestate.content_id(blob)
    return n


def _overlay(repo: Path, dst: Path, paths: list[str], ids: dict[str, str] | None) -> list[str]:
    """Copy working-tree files over a commit copy (labelled in the run's ``source``)."""
    done = []
    root = repo.resolve()
    for rel in paths:
        rel = rel.replace("\\", "/").strip()
        if not rel or path_escape(rel) or not treestate.safe_path(rel.strip("/")) or rel.startswith("/"):
            raise ValueError(f"overlay path {rel!r} must be a repository-relative file path (not in .git or "
                             ".verinoda, in any spelling)")
        src = repo / rel
        cur = repo
        for part in rel.split("/"):
            cur = cur / part
            if cur.is_symlink():
                raise ValueError(f"overlay path {rel!r} goes through a symlink ({cur.relative_to(repo).as_posix()})")
        try:
            src.resolve().relative_to(root)
        except ValueError:
            raise ValueError(f"overlay path {rel!r} resolves outside the repository") from None
        if not src.is_file():
            raise ValueError(f"overlay path {rel!r} is not a file in the working tree")
        out = dst / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        cid = treestate.copy_file(src, out)
        if ids is not None:
            ids[rel] = cid
        done.append(rel)
    return done


def _posix_limits(cpu_s: int, mem_mb: int):  # pragma: no cover - POSIX only
    """Limits for the child, each one as far as the system allows it.

    macOS refuses an address-space limit (``setrlimit(RLIMIT_AS)`` raises ValueError there), and a
    raise in ``preexec_fn`` stopped every isolated run from starting at all; a limit the system
    does not take is now skipped instead, the others still apply."""
    limits = [("RLIMIT_CPU", cpu_s), ("RLIMIT_FSIZE", 256 << 20)]
    if sys.platform != "darwin":
        limits.insert(1, ("RLIMIT_AS", mem_mb << 20))

    def apply():
        import resource

        os.setsid()
        for name, value in limits:
            try:
                resource.setrlimit(getattr(resource, name), (value, value))
            except (ValueError, OSError, AttributeError):
                pass  # this system does not take it
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


# Test runners that exit 0 having run no test at all (a test file renamed out of the runner's pattern, a
# filter that matches nothing): that is no pass.
_ZERO_TESTS = (
    (re.compile(r"^\s*(?:ℹ|#)\s*tests 0\s*$", re.M), "node --test ran 0 tests"),
    (re.compile(r"^\s*No tests? (?:files )?found, exiting with code 0", re.M), "the runner found no tests"),
    (re.compile(r"^Ran 0 tests in ", re.M), "unittest ran 0 tests"),
)
_GO_RESULT = re.compile(r"^(ok|\?|FAIL|---)\s", re.M)
_CARGO_RESULT = re.compile(r"^test result: \w+\. (\d+) passed; (\d+) failed", re.M)


def _ran_no_tests(stdout: str, stderr: str) -> str | None:
    """Why a run that exited 0 ran no test, or None."""
    text = (stdout or "")[-200_000:] + "\n" + (stderr or "")[-50_000:]
    for rx, why in _ZERO_TESTS:
        if rx.search(text):
            return why
    cargo = _CARGO_RESULT.findall(text)
    if cargo and sum(int(p) + int(f) for p, f in cargo) == 0:
        return "cargo test ran 0 tests"
    go = [ln for ln in text.splitlines() if _GO_RESULT.match(ln)]
    if go and all(ln.startswith(("ok", "?")) and ("[no test files]" in ln or "[no tests to run]" in ln) for ln in go):
        return "go test ran no tests"
    return None


def _classify_outcome(argv: list[str], code: int | None, timed_out: bool, stdout: str,
                      stderr: str) -> tuple[str, str | None]:
    """(outcome, why) - see the module docstring for the outcome rules."""
    if timed_out:
        return "timeout", None
    if code == 0:
        none_ran = _ran_no_tests(stdout, stderr)
        if none_ran:
            return "inconclusive", f"exit 0, but {none_ran}: a run without tests is no pass"
        return "pass", None
    if _is_pytest(argv):
        if NO_PYTEST_RE.search(stderr) or NO_PYTEST_RE.search(stdout[-2000:]):
            return "inconclusive", "pytest is not installed for this interpreter (No module named pytest)"
        if code in PYTEST_INCONCLUSIVE:
            return "inconclusive", f"pytest exit code {code}: {PYTEST_INCONCLUSIVE[code]}"
    return "fail", None


class _ProcessTree:
    """Every process the command starts, stopped when the run ends - not only on timeout: a server a test
    leaves running would otherwise outlive the run, keep its throw-away directory and answer the next run.

    Windows: a job object (kill-on-close); the child is started suspended (``CREATE_SUSPENDED``), put in the
    job and only then resumed - a Python venv launcher starts the real interpreter within milliseconds, and a
    process started before the child was in the job would escape it. POSIX: the child's own session
    (``os.setsid`` in ``_posix_limits``). Best effort: when the job cannot be made, the run goes on as before."""

    def __init__(self, proc: subprocess.Popen, suspended: bool = False):
        self.proc = proc
        self.job = None
        if os.name == "nt":
            try:
                self.job = self._make_job(proc)
            except (OSError, AttributeError, ValueError):
                self.job = None
            finally:
                if suspended and not self._resume(proc.pid):
                    proc.kill()
                    raise OSError("the command was started suspended and could not be resumed")

    @staticmethod
    def _k32():
        import ctypes
        from ctypes import wintypes

        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateJobObjectW.restype = wintypes.HANDLE
        k.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        k.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        k.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                                ctypes.c_void_p]
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        k.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        k.Thread32First.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
        k.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
        k.OpenThread.restype = wintypes.HANDLE
        k.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.ResumeThread.restype = wintypes.DWORD
        k.ResumeThread.argtypes = [wintypes.HANDLE]
        return k

    def _resume(self, pid: int) -> bool:
        """Resume the threads of a process started with ``CREATE_SUSPENDED`` (Popen keeps no thread handle)."""
        import ctypes
        from ctypes import wintypes

        class ThreadEntry(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ThreadID", wintypes.DWORD),
                        ("th32OwnerProcessID", wintypes.DWORD), ("tpBasePri", ctypes.c_long),
                        ("tpDeltaPri", ctypes.c_long), ("dwFlags", wintypes.DWORD)]

        try:
            k = self._k32()
            snap = k.CreateToolhelp32Snapshot(0x4, 0)  # TH32CS_SNAPTHREAD
            if not snap or snap == ctypes.c_void_p(-1).value:
                return False
            resumed = 0
            try:
                te = ThreadEntry()
                te.dwSize = ctypes.sizeof(te)
                ok = k.Thread32First(snap, ctypes.byref(te))
                while ok:
                    if te.th32OwnerProcessID == pid:
                        h = k.OpenThread(0x0002, False, te.th32ThreadID)  # THREAD_SUSPEND_RESUME
                        if h:
                            if k.ResumeThread(h) != 0xFFFFFFFF:
                                resumed += 1
                            k.CloseHandle(h)
                    ok = k.Thread32Next(snap, ctypes.byref(te))
            finally:
                k.CloseHandle(snap)
            return resumed > 0
        except (OSError, AttributeError, ValueError):
            return False

    def _make_job(self, proc: subprocess.Popen):
        import ctypes
        from ctypes import wintypes

        class Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class Extended(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", ctypes.c_uint64 * 6),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        k = self._k32()
        job = k.CreateJobObjectW(None, None)
        if not job:
            return None
        info = Extended()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = k.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)) and \
            k.AssignProcessToJobObject(job, int(proc._handle))  # type: ignore[attr-defined]
        if not ok:
            k.CloseHandle(job)
            return None
        return job

    def _active(self) -> int | None:
        """Processes of the job still running (Windows), or None when unknown."""
        if self.job is None:
            return None
        import ctypes

        class Accounting(ctypes.Structure):
            _fields_ = [("TotalUserTime", ctypes.c_int64), ("TotalKernelTime", ctypes.c_int64),
                        ("ThisPeriodTotalUserTime", ctypes.c_int64), ("ThisPeriodTotalKernelTime", ctypes.c_int64),
                        ("TotalPageFaultCount", ctypes.c_uint32), ("TotalProcesses", ctypes.c_uint32),
                        ("ActiveProcesses", ctypes.c_uint32), ("TotalTerminatedProcesses", ctypes.c_uint32)]

        acc = Accounting()
        try:
            if self._k32().QueryInformationJobObject(self.job, 1, ctypes.byref(acc), ctypes.sizeof(acc), None):
                return int(acc.ActiveProcesses)
        except OSError:
            pass
        return None

    def stop(self) -> int | None:
        """Stop every process of the tree that is still running: the number stopped, -1 for some (how many is
        not known), None when this cannot be told (no job object)."""
        if os.name == "nt":
            if self.job is None:
                return None
            n = self._active()
            try:
                k = self._k32()
                k.TerminateJobObject(self.job, 1)
                k.CloseHandle(self.job)
            except OSError:
                pass
            self.job = None
            return n
        try:  # pragma: no cover - POSIX
            os.killpg(self.proc.pid, 0)
        except (ProcessLookupError, PermissionError, OSError):
            return 0
        try:  # pragma: no cover - POSIX
            os.killpg(self.proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            return 0
        return -1  # pragma: no cover - some were running; how many is not known here


def _communicate(proc: subprocess.Popen, tree: _ProcessTree, deadline: float) -> tuple[bytes, bytes, bool, int | None]:
    """``proc.communicate`` until ``deadline``: (stdout, stderr, timed out, processes stopped). Waits in slices:
    when the command has exited but a process it started still holds its output pipes, that process is stopped
    after ``ORPHAN_GRACE_S`` instead of the run waiting for the timeout."""
    exited_at = None
    while True:
        left = deadline - time.monotonic()
        try:
            so, se = proc.communicate(timeout=max(0.01, min(left, 1.0)))
            return so, se, False, None
        except subprocess.TimeoutExpired:
            if proc.poll() is not None:
                exited_at = exited_at or time.monotonic()
                if time.monotonic() - exited_at >= ORPHAN_GRACE_S:
                    n = tree.stop()
                    try:
                        so, se = proc.communicate(timeout=KILL_DRAIN_TIMEOUT)
                    except subprocess.TimeoutExpired:
                        so, se = b"", (b"[verinoda] output unavailable: a process the command started kept the pipes "
                                       b"open after the command exited")
                    return so, se, False, n if n is not None else -1
            if time.monotonic() >= deadline:
                return b"", b"", True, None


def _remove_tree(path: Path, tries: int = 5) -> bool:
    """Delete a throw-away directory, retrying while stopped processes release their files; False if it stays."""
    for i in range(tries):
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            return True
        time.sleep(0.2 * (i + 1))
    return not path.exists()


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
    env_extra: dict[str, str] | None = None, ref: str | None = None, overlay: list[str] | None = None,
    file_ids: dict[str, str] | None = None,
) -> dict:
    """Run one experiment and record it (and its evidence) in the store.

    ``plugins`` ({module file name: source bytes}) are written next to the
    copy, never into it, and that directory is put on ``PYTHONPATH`` (load
    them with ``-p <module>``). ``env_extra`` adds ``VERINODA_*`` variables
    only. Files the child writes to ``$VERINODA_ARTIFACTS`` are returned in
    ``result["artifacts"]`` ({name: path under runs/<id>/artifacts}).

    Source of the copy: the working tree by default; with ``ref`` the regular
    files of that commit (``git archive``-like, read with ``git cat-file``; the
    user's tree, index and ``.git`` are untouched), optionally with
    working-tree files ``overlay`` copied on top (labelled in ``source``). The
    same policy and isolation apply either way. ``result["tree"]`` is the
    identity of what ran (:mod:`verinoda.treestate`: tree hash over content
    ids, computed while copying); ``file_ids`` (a dict) receives the per-file
    content ids.
    """
    repo = Path(repo).resolve()
    cfg = load_config(repo)["experiments"]
    if isinstance(argv, str):
        argv = shlex.split(argv, posix=(os.name != "nt"))
        if os.name == "nt":
            argv = [_strip_quotes(a) for a in argv]
    source: dict = {"kind": "worktree"}
    if ref is not None:
        sha = treestate.resolve_commit(repo, ref)
        source = {"kind": "commit", "commit": sha, "ref": treestate.check_ref(ref)}
        commit = commit or sha
    elif overlay:
        raise ValueError("overlay applies to a commit copy only (give ref)")
    for name in plugins or {}:
        if not PLUGIN_NAME_RE.match(name):
            raise ValueError(f"plugin file name must be a plain module file name, not {name!r}")
    for key in env_extra or {}:
        if not ENV_EXTRA_RE.match(key):
            raise ValueError(f"env_extra may only set VERINODA_* variables, not {key!r}")
    timeout = float(timeout or cfg["default_timeout"])
    kind, why_risky = policy(argv, cfg["process_isolation_allowlist"])
    # probed only when it can matter: an allowlisted command under auto/process runs with process isolation
    process_ok = kind == "allowlisted" and isolation in ("auto", "process")
    runtime = container_runtime() if isolation == "container" or (isolation == "auto" and not process_ok) else None
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
        if source["kind"] == "commit":
            reason_src = f" (source: commit {source['commit'][:12]})"
        else:
            reason_src = ""
        if isolation == "container" and not runtime:
            reason = "container isolation was requested and no container runtime (docker/podman) is available"
        else:
            reason = (f"command classified '{kind}' and no container runtime (docker/podman) is available; "
                      "Verinoda does not run non-allowlisted commands with process isolation only")
        if why_risky:
            reason = f"{reason} (why '{kind}': {why_risky})"
        store.insert("experiments", {**base, "cwd": str(repo) + reason_src, "isolation": "none",
                                     "status": "refused", "summary": reason,
                                     "environment": {"policy": {"kind": kind, "reason": why_risky},
                                                     "source": source}})
        raise ExperimentRefused(reason)

    out_dir = runs_dir(repo) / eid
    out_dir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="verinoda-exp-"))
    home = work / "_home"
    home.mkdir()
    copy = work / "repo"
    ids: dict[str, str] = {}
    try:
        if source["kind"] == "commit":
            copy.mkdir()
            not_written: list[dict] = []
            copied = _copy_commit(repo, source["commit"], copy, ids, not_written)
            if not_written:
                source["skipped"] = not_written[:50]
                source["skipped_total"] = len(not_written)
            if overlay:
                source["overlay"] = _overlay(repo, copy, list(overlay), ids)
            where = f"copy of commit {source['commit'][:12]} of {repo}"
        else:
            copied = _copy_repo(repo, copy, ids)
            where = f"copy of {repo}"
    except Exception:
        shutil.rmtree(work, ignore_errors=True)
        raise
    tree = {"hash": treestate.tree_id(ids), "files": len(ids)}
    if file_ids is not None:
        file_ids.update(ids)
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
    if source.get("skipped"):
        limits.append(f"{source['skipped_total']} file(s) of the commit are missing from the copy (this OS cannot "
                      "hold their names): " + ", ".join(s["path"] for s in source["skipped"][:5]))
    container_name = f"verinoda-{eid}"
    if level == "container":
        extra: list[str] = []
        for k, v in (env_extra or {}).items():
            extra += ["-e", f"{k}={v}"]
        extra += ["-v", f"{artifacts_dir}:/artifacts", "-e", "VERINODA_ARTIFACTS=/artifacts"]
        if plugins:
            extra += ["-v", f"{plugins_dir}:/plugins:ro", "-e", "PYTHONPATH=/plugins"]
        cmd = [runtime, "run", "--rm", "--name", container_name, "--network", "none", "--memory", "1g",
               "--cpus", "1", "--pids-limit", "256", "-v", f"{copy}:/work", "-w", "/work", *extra,
               cfg.get("container_image", "python:3.12-slim"), *_container_argv(argv)]
    else:
        env.update(env_extra or {})
        env["VERINODA_ARTIFACTS"] = str(artifacts_dir)
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
        # suspended until it is in the job object (_ProcessTree): nothing it starts can escape the job
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | CREATE_SUSPENDED
    else:  # pragma: no cover
        kwargs["preexec_fn"] = _posix_limits(int(timeout) + 5, 2048)
    timed_out = False
    try:
        proc = subprocess.Popen(cmd, **kwargs)
        tree_procs = _ProcessTree(proc, suspended=os.name == "nt")
    except OSError as exc:
        _remove_tree(work)
        store.insert("experiments", {**base, "cwd": str(copy), "isolation": level, "status": "error",
                                     "summary": f"could not start: {exc}",
                                     "environment": {**guarantees, "source": source, "tree_hash": tree["hash"]}})
        raise
    left_running: int | None = None  # processes the command left running when it exited, stopped by Verinoda
    try:
        so, se, timed_out, left_running = _communicate(proc, tree_procs, t0 + timeout)
        if timed_out:
            if level == "container":
                _kill_container(runtime, container_name)
            _kill_tree(proc)
            tree_procs.stop()
            try:
                so, se = proc.communicate(timeout=KILL_DRAIN_TIMEOUT)
            except subprocess.TimeoutExpired:
                # A descendant escaped the tree kill and still holds the pipes;
                # do not let it turn the timeout into a hang.
                so, se = b"", b"[verinoda] output unavailable: a descendant process kept the pipes open after kill"
    finally:
        stopped_now = tree_procs.stop()   # whatever the command left running (also closes the job)
    duration = time.monotonic() - t0
    if not timed_out and stopped_now:
        left_running = stopped_now if not left_running else left_running + max(0, stopped_now)
    stdout = so[:MAX_LOG_BYTES].decode("utf-8", "replace")
    stderr = se[:MAX_LOG_BYTES].decode("utf-8", "replace")
    # Bytes, not write_text: on Windows the child's "\r\n" would become "\r\r\n".
    (out_dir / "stdout.txt").write_bytes(stdout.encode("utf-8"))
    (out_dir / "stderr.txt").write_bytes(stderr.encode("utf-8"))
    artifacts = _collect_artifacts(artifacts_dir, out_dir / "artifacts")
    removed = _remove_tree(work)
    if left_running and not timed_out:
        limits.append(("some process(es)" if left_running < 0 else f"{left_running} process(es)") + " the command "
                      "left running were stopped when it exited")
    if not removed:
        limits.append(f"the throw-away copy {work} could not be deleted (a file in it is still in use)")
    code = None if timed_out else proc.returncode
    summ = _summarize(stdout, stderr, code, timed_out)
    outcome, why = _classify_outcome(argv, code, timed_out, stdout, stderr)
    inconclusive = outcome == "inconclusive"
    matches = (expect == "pass" and outcome == "pass") or (expect == "fail" and outcome == "fail")
    is_test = kind == "allowlisted"
    on = ""
    if source["kind"] == "commit":
        on = f" (on commit {source['commit'][:12]}" + (f" + working-tree {', '.join(source['overlay'])}"
                                                       if source.get("overlay") else "") + ")"
    ev = {
        "source_type": "test_result" if is_test else "experiment",
        "locator": f"run {eid}{on}: {' '.join(argv)}",
        "path": None, "commit_sha": commit,
        "content_hash": "sha256:" + hashlib.sha256((stdout + stderr).encode()).hexdigest(),
        "excerpt": " | ".join(([why] if why else []) + (summ["summary_lines"] or summ["tail"][-2:]))[:400],
        "meta": {"experiment_id": eid,
                 "outcome": "inconclusive" if inconclusive else ("pass" if matches else "fail"),
                 "raw_outcome": outcome, "expect": expect, "exit_code": code, "isolation": level,
                 **({"inconclusive_reason": why} if why else {}),
                 "tree_hash": tree["hash"], "source": source,
                 "stdout": str(out_dir / "stdout.txt"), "stderr": str(out_dir / "stderr.txt")},
    }
    ev_id = evmod.add(store, ev)
    store.insert("experiments", {
        **base, "cwd": f"{where} ({copied} files)", "isolation": level,
        "environment": {"guarantees": guarantees, "limits": limits, "python": sys.version.split()[0],
                        "platform": sys.platform, **({"plugins": sorted(plugins)} if plugins else {}),
                        "source": source, "tree_hash": tree["hash"]},
        "exit_code": code, "duration_s": round(duration, 3), "timed_out": int(timed_out),
        "status": outcome, "summary": "; ".join(([why] if why else []) + summ["summary_lines"]) or None,
        "stdout_path": str(out_dir / "stdout.txt"), "stderr_path": str(out_dir / "stderr.txt"),
        "evidence_id": ev_id,
    })
    if claim_id:
        from verinoda.claims import Claims

        # An inconclusive run says nothing about the hypothesis: traceable, never support or refutation.
        # A run of another commit (a commit copy) says nothing about the code the claim describes either.
        claim_commit = (store.claim(claim_id) or {}).get("commit_sha")
        other_code = source["kind"] == "commit" and (source.get("overlay") or source["commit"] != claim_commit)
        relation = "qualifies" if (inconclusive or other_code) else ("supports" if matches else "refutes")
        Claims(store, repo).attach(claim_id, ev_id, relation,
                                   note=f"experiment {eid}{on}: expected {expect}, got {outcome}"
                                        + (f" ({why})" if why else "")
                                        + ("; it ran other code than the claim's commit, so it only qualifies the "
                                           "claim" if other_code else ""))
    res = {"id": eid, "isolation": level, "guarantees": guarantees, "outcome": outcome,
           "matches_expectation": matches, "duration_s": round(duration, 3), "evidence_id": ev_id,
           "summary": summ, "logs": {"stdout": str(out_dir / "stdout.txt"), "stderr": str(out_dir / "stderr.txt")},
           "exit_code": code, "tree": tree, "source": source}
    if limits:
        res["limits"] = limits
    if artifacts:
        res["artifacts"] = artifacts
    if why:
        res["inconclusive_reason"] = why
        res["next_step"] = ("install pytest in the project's environment (or create .venv) and re-run"
                            if "not installed" in why else "check the test ids/arguments and re-run")
    return res
