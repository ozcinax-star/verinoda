"""Real-world end-user benchmark: Verinoda run on pinned open-source repositories.

Usage::

    python benchmarks/realworld/run.py --repos gin-gonic/gin [--work C:/vbench] [--out DIR]
    python benchmarks/realworld/run.py --smallest 2
    python benchmarks/realworld/run.py --all

For each repository of ``manifest.json``: a shallow clone at the pinned tag (an existing clone at the
pinned sha is reused, a dirty one is refused), the sha checked, then the end-user scenarios run as
subprocesses (``python -P -m verinoda ...`` with ``PYTHONPATH`` set to the Verinoda checkout under test and
a fresh, empty Verinoda config folder), each timed, with its exit code, the tail of its stderr and the size
of its output. The gold facts of ``gold/<owner>__<name>.json`` are checked against Verinoda's answers.

The repository's code is never executed: :func:`guard_argv` lets through only the read-only commands of
:data:`ALLOWED` with their listed options, and refuses anything else before any process starts. The clone
is read by Verinoda and by ``git`` (clone, rev-parse, ls-files, status); one scripted one-line edit is made
and reverted, and the clone must be clean afterwards.

Standard library only (Verinoda itself is run as a subprocess).
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import shutil
import stat
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
DEFAULT_WORK = Path(os.environ.get("VERINODA_BENCH_WORK", "C:/vbench" if os.name == "nt" else "/tmp/vbench"))

# The only commands the runner may start, each with the only options it may pass. Everything else, and any
# other token starting with "-" (an abbreviation such as --run-test or --obs included: argparse would
# expand it), is refused: the allow list cannot miss a command that runs code (debug try, run, spec,
# mutations, trust, observe, ...) the way a deny list can.
ALLOWED: dict[str, frozenset[str]] = {
    "init": frozenset({"--json"}), "scan": frozenset({"--json"}), "update": frozenset({"--json"}),
    "query": frozenset({"--json", "--max-items"}), "analyze": frozenset({"--json"}),
    "trace": frozenset({"--json", "--mode"}), "q": frozenset({"--json"}), "check": frozenset({"--json"}),
    "map": frozenset({"--json", "--view"}), "routes": frozenset({"--json"}), "schema": frozenset({"--json"}),
    "taint": frozenset({"--json"}), "review": frozenset({"--json"}), "doctor": frozenset({"--json"}),
}

# Exit codes each command documents as a normal answer; anything else is recorded as a crash. A non-zero
# allowed code with --json must still come with a JSON object on stdout (with the key of JSON_KEY): an
# internal error (KeyError, ValueError, FileNotFoundError, no index) also exits 1, with "error: ..." on
# stderr and no JSON.
OK_CODES = {
    "init": {0}, "scan": {0}, "update": {0}, "query": {0}, "analyze": {0, 3},
    "trace": {0, 2},            # 2: no directed path
    "q": {0, 1, 3},             # 1: no row, 3: cut by a budget
    "check": {0, 3, 4},         # 3: something absent, 4: a file not checked
    "map": {0}, "routes": {0}, "schema": {0, 3}, "taint": {0, 3}, "review": {0, 3},
    "doctor": {0, 1}, "mcp": {0},
}
JSON_KEY = {"q": "rows"}

TIMEOUTS = {"scan": 1800, "update": 900, "analyze": 600, "review": 600, "init": 120, "doctor": 300,
            "query": 300, "trace": 300, "q": 300, "check": 300, "map": 600, "routes": 300, "schema": 300,
            "taint": 600, "mcp": 600}

STDERR_TAIL = 1200
TRACEBACK = "Traceback (most recent call last)"
MIN_PYTHON = (3, 11)        # python -P

DEFAULT_SCENARIO = {
    "queries": ["where is the main entry point", "how are errors handled and reported",
                "how is configuration loaded"],
    "analyze": ["what happens when the program starts", "how are errors handled"],
    "q": ["match (f:function) return count(f)",
          "match (a:function)-[calls]->(b:function) return count(*)"],
    "map_views": ["dependencies", "dataflow", "dead", "hotspots"],
}

SOURCE_EXT = {".py": "#", ".go": "//", ".js": "//", ".ts": "//", ".tsx": "//", ".java": "//", ".rs": "//",
              ".php": "//", ".kt": "//", ".rb": "#", ".cs": "//"}
CHECK_EXT = (".py", ".java", ".kt")      # the languages `check` reads; another file only says "not checked"


class Forbidden(ValueError):
    """An argv outside the allow list (it could run the repository's code)."""


class DirtyClone(RuntimeError):
    """The clone has changes to tracked files."""


def guard_argv(argv: list[str]) -> None:
    """Refuse a Verinoda argv that is not on the allow list :data:`ALLOWED`."""
    if not argv or argv[0] not in ALLOWED:
        raise Forbidden(f"not on the allow list: verinoda {argv[0] if argv else '(nothing)'}")
    allowed = ALLOWED[argv[0]]
    for a in argv[1:]:
        if a.startswith("-") and a.split("=", 1)[0] not in allowed:
            raise Forbidden(f"option not on the allow list of verinoda {argv[0]}: {a}")


def slug(name: str) -> str:
    return name.replace("/", "__")


def load_manifest(path: Path | None = None) -> dict:
    return json.loads((path or HERE / "manifest.json").read_text(encoding="utf-8"))


def load_gold(name: str, gold_dir: Path | None = None) -> dict | None:
    p = (gold_dir or HERE / "gold") / f"{slug(name)}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None


# -- clone ---------------------------------------------------------------------------------------------

def _git(args: list[str], cwd: Path | None = None, timeout: int = 600) -> subprocess.CompletedProcess:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_LFS_SKIP_SMUDGE="1")
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, env=env)


def head_sha(clone: Path) -> str | None:
    r = _git(["-C", str(clone), "rev-parse", "HEAD"])
    return r.stdout.strip() if r.returncode == 0 else None


def dirty_files(clone: Path) -> list[str] | None:
    """Tracked files with changes (``git status --porcelain --untracked-files=no``); None: not a git checkout."""
    if not (Path(clone) / ".git").exists():
        return None
    r = _git(["-C", str(clone), "status", "--porcelain", "--untracked-files=no"])
    if r.returncode != 0:
        return None
    return [x for x in r.stdout.splitlines() if x.strip()]


def _rmtree(p: Path) -> None:
    """Remove a folder of ours, read-only files (git packs on Windows) included."""
    def fix(func, path, _exc):
        os.chmod(path, stat.S_IWRITE)
        func(path)
    if sys.version_info >= (3, 12):
        shutil.rmtree(p, onexc=fix)
    else:
        shutil.rmtree(p, onerror=fix)


def git_clone(repo: dict, dest: Path) -> None:
    """Shallow clone at the pinned tag: no submodules, no symlinks (a tracked link is a plain file), long
    paths allowed, no Git LFS download (GIT_LFS_SKIP_SMUDGE in :func:`_git`)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    r = _git(["-c", "advice.detachedHead=false", "-c", "core.autocrlf=false", "-c", "core.symlinks=false",
              "-c", "core.longpaths=true", "clone", "--quiet", "--depth", "1", "--branch", repo["tag"],
              "--no-recurse-submodules", "--config", "core.symlinks=false", "--config", "core.longpaths=true",
              repo["url"], str(dest)], timeout=1800)
    if r.returncode != 0:
        raise RuntimeError(f"clone of {repo['name']} failed: {r.stderr.strip()[-500:]}")


def ensure_clone(repo: dict, work: Path, clone_fn=git_clone, sha_fn=head_sha) -> Path:
    """The clone of ``repo`` under ``work`` at the pinned sha: reused when it is there, else cloned.

    The clone is made in ``<slug>.partial`` and renamed when it is complete, so a clone killed half-way
    never looks like a finished one; a ``.partial`` folder left by such a run is ours and is removed. A
    finished folder at another sha is never deleted (it may be someone's work): that is an error."""
    dest = work / slug(repo["name"])
    if not dest.exists():
        partial = work / (slug(repo["name"]) + ".partial")
        if partial.exists():
            _rmtree(partial)
        try:
            clone_fn(repo, partial)
        except BaseException:
            if partial.exists():
                shutil.rmtree(partial, ignore_errors=True)
            raise
        partial.rename(dest)
    sha = sha_fn(dest)
    if sha != repo["sha"]:
        raise RuntimeError(f"{dest} is at {sha}, not the pinned {repo['sha']} ({repo['tag']})"
                           + ("; it is not a git checkout: delete it to clone again" if sha is None else ""))
    return dest


def assert_clean(clone: Path) -> None:
    """Refuse a clone with changes to tracked files: the gold facts were read on the pristine sources."""
    dirty = dirty_files(clone)
    if dirty:
        raise DirtyClone(f"{clone} has changes to tracked files ({len(dirty)}: {', '.join(dirty[:5])}); "
                         f"restore them (git checkout -- .) before running")


def tracked_files(clone: Path) -> list[str]:
    """The repository's files (git ls-files; a folder walk when it is not a git checkout)."""
    r = _git(["-C", str(clone), "ls-files"])
    if r.returncode == 0:
        return [x for x in r.stdout.splitlines() if x]
    return sorted(p.relative_to(clone).as_posix() for p in clone.rglob("*")
                  if p.is_file() and not {".git", ".verinoda"} & set(p.relative_to(clone).parts))


def inside(clone: Path, rel: str) -> bool:
    """``clone/rel`` is a regular file that is not a symlink and resolves inside the clone."""
    p = Path(clone) / rel
    try:
        if p.is_symlink() or not p.is_file():
            return False
        p.resolve().relative_to(Path(clone).resolve())
        return True
    except (OSError, ValueError):
        return False


# -- one command -----------------------------------------------------------------------------------------

def _kill_tree(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    else:
        try:
            os.killpg(proc.pid, 9)
        except OSError:
            proc.kill()


class Sanitizer:
    def __init__(self, mapping: dict[str, str]):
        pairs = []
        for real, ph in mapping.items():
            if not real:
                continue
            for v in {real, real.replace("\\", "/"), real.replace("/", "\\"), real.replace("\\", "\\\\")}:
                pairs.append((v, ph))
        self.pairs = sorted(pairs, key=lambda p: -len(p[0]))

    def __call__(self, text: str) -> str:
        flags = re.IGNORECASE if os.name == "nt" else 0
        for real, ph in self.pairs:
            text = re.sub(re.escape(real), lambda _m, ph=ph: ph, text, flags=flags)
        return text


def _json_object(stdout: str):
    try:
        data = json.loads(stdout)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


class Runner:
    """Runs the scenarios of one repository and records each step.

    ``config_dir`` is the Verinoda user config folder the steps see (``VERINODA_CONFIG_DIR``): the runner
    passes a fresh empty one, so the user's ``trust.json`` and ``config.json`` never apply to the clone (a
    trusted clone could run its own Python in ``doctor``)."""

    def __init__(self, clone: Path, *, python: str = sys.executable, verinoda_root: Path = ROOT,
                 module: str = "verinoda", timeout_scale: float = 1.0, timeouts: dict | None = None,
                 sanitize: Sanitizer | None = None, log=None, config_dir: Path | None = None):
        self.clone = Path(clone)
        self.python = python
        self.verinoda_root = Path(verinoda_root)
        self.module = module
        self.scale = timeout_scale
        self.timeouts = dict(TIMEOUTS, **(timeouts or {}))
        self.steps: list[dict] = []
        self.config_dir = Path(config_dir) if config_dir else None
        self.sanitize = sanitize or Sanitizer({str(self.clone): "<CORPUS>", str(self.verinoda_root): "<REPO>",
                                               str(Path.home()): "<HOME>"})
        self.log = log or (lambda s: None)

    def env(self) -> dict:
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith("VERINODA_")}
        env["PYTHONPATH"] = str(self.verinoda_root)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_LFS_SKIP_SMUDGE"] = "1"
        if self.config_dir is not None:
            env["VERINODA_CONFIG_DIR"] = str(self.config_dir.resolve())
        for k in ("VIRTUAL_ENV", "PYTHONHOME", "PYTHONSTARTUP"):
            env.pop(k, None)
        return env

    def _exec(self, argv: list[str], timeout: float) -> tuple[int | None, bytes, bytes, float, bool]:
        kw = {}
        if os.name == "nt":
            kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kw["start_new_session"] = True
        t0 = time.perf_counter()
        proc = subprocess.Popen(argv, cwd=self.clone, env=self.env(), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kw)
        try:
            out, err = proc.communicate(timeout=timeout)
            return proc.returncode, out, err, time.perf_counter() - t0, False
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            try:
                out, err = proc.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                out, err = b"", b""
            return None, out or b"", err or b"", time.perf_counter() - t0, True

    def run(self, step: str, args: list[str], *, command: str | None = None, ok: set[int] | None = None,
            raw_argv: list[str] | None = None) -> tuple[dict, str]:
        """One subprocess step; returns (record, stdout text). ``raw_argv`` replaces ``-m MODULE args``."""
        command = command or args[0]
        if raw_argv is None:
            guard_argv(args)
            argv = [self.python, "-P", "-m", self.module, *args]
        else:
            argv = raw_argv
        timeout = self.timeouts.get(command, 300) * self.scale
        self.log(f"  {step} ...")
        code, out, err, secs, timed_out = self._exec(argv, timeout)
        stdout = out.decode("utf-8", "replace")
        stderr = err.decode("utf-8", "replace")
        ok = OK_CODES.get(command, {0}) if ok is None else ok
        wants_json = "--json" in args or raw_argv is not None
        obj = _json_object(stdout) if wants_json else None
        tb = TRACEBACK in stderr
        reason = None
        if timed_out:
            reason = f"timeout after {timeout:.0f}s"
        elif tb:
            reason = "traceback in stderr"
        elif code not in ok:
            reason = f"exit {code} not in {sorted(ok)}"
        elif code != 0 and wants_json and (obj is None or (JSON_KEY.get(command) and
                                                           JSON_KEY[command] not in obj)):
            first = next((ln for ln in stderr.splitlines() if ln.strip()), "")
            reason = f"exit {code} without a JSON answer" + (f": {first[:200]}" if first else "")
        json_ok = None
        if wants_json:
            try:
                json.loads(stdout)
                json_ok = True
            except ValueError:
                json_ok = False
        rec = {
            "step": step, "command": command,
            "argv": [self.sanitize(a) for a in (args if raw_argv is None else raw_argv[1:])],
            "seconds": round(secs, 3), "exit": code, "ok_codes": sorted(ok),
            "timeout": timed_out, "timeout_s": timeout,
            "crash": bool(reason) and not timed_out, "reason": reason,
            "stdout_bytes": len(out), "stderr_bytes": len(err), "json_ok": json_ok,
            "stderr_tail": self.sanitize(stderr[-STDERR_TAIL:]) if (reason or stderr.strip()) else "",
        }
        self.steps.append(rec)
        self.log(f"  {step}: exit {code} {secs:.1f}s {len(out)}B" + (f" ** {reason}" if reason else ""))
        return rec, stdout


# -- gold ------------------------------------------------------------------------------------------------

def _ats(obj):
    """Every string value of an ``at`` key in ``obj`` (Verinoda's file:line citations)."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "at" and isinstance(v, str):
                yield v
            else:
                yield from _ats(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _ats(v)


def _norm(s: str) -> str:
    return s.replace("\\", "/")


def cites(obj, loc: str) -> bool:
    """Some ``at`` of ``obj`` names ``loc`` (path:line) exactly: not path:2360 for path:236, not
    binding/gin.go:236 for gin.go:236 (only the start, a space or a quote may come before the path)."""
    pat = re.compile(r"(?:^|[\s\"'])" + re.escape(_norm(loc)) + r"(?![\w./:-])")
    return any(pat.search(_norm(s)) for s in _ats(obj))


def _at_file(at: str) -> str | None:
    m = re.match(r"^(.+?):\d+(?:-\d+)?$", _norm(at).strip())
    return m.group(1) if m else None


def gold_argv(check: dict) -> list[str]:
    kind = check["kind"]
    if kind == "q":
        return ["q", check["query"], "--json"]
    if kind == "trace":
        return ["trace", check["source"], check["target"], "--json"] + (["--mode", check["mode"]]
                                                                        if check.get("mode") else [])
    if kind == "query":
        return ["query", check["question"], "--json", "--max-items", str(check.get("top_k", 8))]
    if kind == "routes":
        return ["routes", "--json"]
    if kind == "schema":
        return ["schema", "--json"]
    raise ValueError(f"unknown gold check kind: {kind}")


def _judge_trace(check: dict, data: dict) -> tuple[bool, str]:
    """Both ends resolved (at ``source_at`` / ``target_at`` when given) and one path whose edges are located
    in every ``via_files`` file and at every ``via_at`` file:line. The resolved endpoints never stand in for
    the path: a via file must be where an edge of the path is."""
    paths = [p for p in data.get("paths") or [] if isinstance(p, list)]
    resolved = data.get("resolved") if isinstance(data.get("resolved"), dict) else {}
    for end in ("source", "target"):
        r = resolved.get(end)
        if not isinstance(r, dict) or not r.get("at"):
            return False, f"{end} not resolved ({data.get('status')})"
        want = check.get(f"{end}_at")
        if want and _norm(r["at"]) != _norm(want):
            return False, f"{end} resolved at {r['at']}, not {want}"
    if not paths:
        return False, f"no path ({data.get('status')})"
    best = None
    for p in paths:
        ats = [_norm(e["at"]) for e in p if isinstance(e, dict) and isinstance(e.get("at"), str)]
        files = {_at_file(a) for a in ats}
        missing = [f for f in check.get("via_files", []) if _norm(f) not in files]
        missing += [a for a in check.get("via_at", []) if _norm(a) not in ats]
        if not missing:
            return True, ""
        if best is None or len(missing) < len(best):
            best = missing
    return False, f"no path edge at {', '.join(best)}"


def judge(check: dict, exit_code: int | None, stdout: str) -> tuple[bool, str]:
    """Whether Verinoda's answer holds the gold fact, and why not."""
    try:
        data = json.loads(stdout)
    except ValueError:
        return False, f"no JSON (exit {exit_code})"
    if not isinstance(data, dict):
        return False, f"JSON is a {type(data).__name__}, not an object (exit {exit_code})"
    kind = check["kind"]
    if kind == "q":
        rows = data.get("rows") or []
        if not rows:
            return False, "no row"
        missing = [c for c in check.get("cites", []) if not cites(rows, c)]
        return (not missing), ("" if not missing else f"rows do not cite {', '.join(missing)}")
    if kind == "trace":
        return _judge_trace(check, data)
    if kind == "query":
        k = int(check.get("top_k", 8))
        files = [_norm(str(i.get("file", ""))) for i in (data.get("items") or [])[:k] if isinstance(i, dict)]
        want = _norm(check["expect_file"])
        if want in files:
            return True, f"rank {files.index(want) + 1}"
        return False, f"not in top {k}: {files}"
    if kind == "routes":
        table = data.get("route_table") or []
        for r in table:
            if not isinstance(r, dict):
                continue
            if (r.get("path") == check["path"] and (not check.get("method") or
                                                    str(r.get("method", "")).upper() == check["method"].upper())
                    and check.get("handler", "") in json.dumps(r)):
                return True, ""
        return False, "route not in the table"
    if kind == "schema":
        names = {t.get("name") for t in data.get("tables") or [] if isinstance(t, dict)}
        return (check["table"] in names), ("" if check["table"] in names else f"tables: {sorted(map(str, names))[:20]}")
    return False, f"unknown kind {kind}"


def check_gold(runner: Runner, gold: dict) -> list[dict]:
    """Each fact checked with its command's own documented exit codes (query 0, trace 0/2, q 0/1/3, ...):
    an internal error is a crash and a miss, never just a miss."""
    out = []
    for fact in gold.get("facts", []):
        chk = fact["check"]
        rec, stdout = runner.run(f"gold:{fact['id']}", gold_argv(chk))
        if rec["timeout"] or rec["crash"]:
            hit, why = False, rec["reason"] or ""
        else:
            try:
                hit, why = judge(chk, rec["exit"], stdout)
            except Exception as exc:  # a malformed answer is a miss, not the end of the run
                hit, why = False, f"judge error: {type(exc).__name__}: {exc}"
        out.append({"id": fact["id"], "v": int(fact.get("v", 1)), "kind": chk["kind"], "hit": hit, "why": why,
                    "seconds": rec["seconds"]})
    return out


# -- the scripted edit ----------------------------------------------------------------------------------

def _candidates(clone: Path, files: list[str], exts) -> list[tuple[int, str]]:
    out = []
    for f in files:
        if Path(f).suffix not in exts or "test" in f.lower() or "vendor" in f.lower():
            continue
        if not inside(clone, f):                 # a symlink, or a path resolving outside the clone
            continue
        try:
            out.append(((Path(clone) / f).stat().st_size, f))
        except OSError:
            continue
    return out


def pick_edit(clone: Path, files: list[str], scenario: dict) -> dict:
    """The one-line edit: the scenario's, else a comment line appended to the largest source file."""
    if scenario.get("edit"):
        return scenario["edit"]
    cands = _candidates(clone, files, SOURCE_EXT)
    if not cands:
        return {}
    _size, f = max(cands)
    return {"file": f, "after_line": None, "insert": f"{SOURCE_EXT[Path(f).suffix]} verinoda-bench edit"}


def pick_check_file(clone: Path, files: list[str], scenario: dict, edit_spec: dict) -> str | None:
    """A file `check` actually reads (Python, Java or Kotlin): the scenario's, else the edited file, else the
    largest such source file; None when the repository has none (the step is then skipped, not run for an
    exit 4 'not checked' that measures nothing)."""
    for f in (scenario.get("check_file"), edit_spec.get("file")):
        if f and Path(f).suffix in CHECK_EXT and inside(clone, f):
            return f
    cands = _candidates(clone, files, CHECK_EXT)
    return max(cands)[1] if cands else None


class Edit:
    """Insert one line (after ``after_line``, or at the end) and restore the exact bytes afterwards.

    Works on bytes: a file in Latin-1 or cp1252 is edited as well as a UTF-8 one. A symlink or a path that
    resolves outside the clone is refused (the edit would write somewhere else)."""

    def __init__(self, clone: Path, spec: dict):
        self.clone = Path(clone)
        self.path = self.clone / spec["file"]
        self.spec = spec
        self.original: bytes | None = None

    def apply(self) -> None:
        if not inside(self.clone, self.spec["file"]):
            raise ValueError(f"edit refused: {self.spec['file']} is a symlink, missing, or outside the clone")
        self.original = self.path.read_bytes()
        nl = b"\r\n" if b"\r\n" in self.original else b"\n"
        lines = self.original.split(nl)
        at = self.spec.get("after_line")
        idx = len(lines) if at is None else int(at)
        if at is None and lines and lines[-1] == b"":
            idx = len(lines) - 1
        lines.insert(idx, self.spec["insert"].encode("utf-8"))
        self.path.write_bytes(nl.join(lines))

    def revert(self) -> None:
        if self.original is not None:
            self.path.write_bytes(self.original)
            self.original = None


# -- one repository ---------------------------------------------------------------------------------------

MCP_PROBE = HERE / "mcp_probe.py"


def _answer_status(stdout: str):
    obj = _json_object(stdout)
    return obj.get("status") if obj else None


def run_repo(repo: dict, clone: Path, *, python: str = sys.executable, verinoda_root: Path = ROOT,
             module: str = "verinoda", gold: dict | None = None, timeout_scale: float = 1.0,
             steps: set[str] | None = None, log=None, env: dict | None = None) -> dict:
    """Every scenario on one clone; ``steps`` limits them (by group name) for tests."""
    with tempfile.TemporaryDirectory(prefix="vbench-config-") as cfg:
        return _run_repo(repo, clone, python=python, verinoda_root=verinoda_root, module=module, gold=gold,
                         timeout_scale=timeout_scale, steps=steps, log=log, env=env, config_dir=Path(cfg))


def _run_repo(repo, clone, *, python, verinoda_root, module, gold, timeout_scale, steps, log, env,
              config_dir) -> dict:
    want = (lambda g: True) if steps is None else (lambda g: g in steps)
    sc = dict(DEFAULT_SCENARIO, **(repo.get("scenario") or {}))
    r = Runner(clone, python=python, verinoda_root=verinoda_root, module=module, timeout_scale=timeout_scale,
               log=log, config_dir=config_dir)
    files = tracked_files(clone)
    started = _dt.datetime.now().isoformat(timespec="seconds")
    notes: list[str] = []
    skipped: list[str] = []
    vn = clone / ".verinoda"
    if want("init"):
        if any(f == ".verinoda" or f.startswith(".verinoda/") for f in files):
            notes.append("the repository tracks a .verinoda/ folder: kept, so init is not cold")
        elif vn.is_dir() and not vn.is_symlink() and vn.parent == clone:
            _rmtree(vn)
        r.run("init", ["init", "."])
    if want("scan"):
        r.run("scan", ["scan", ".", "--json"])
    gold_results = check_gold(r, gold) if gold and want("gold") else []
    if want("query"):
        for i, qn in enumerate(sc["queries"], 1):
            r.run(f"query{i}", ["query", qn, "--json"])
    if want("analyze"):
        for i, qn in enumerate(sc["analyze"], 1):
            r.run(f"analyze{i}", ["analyze", qn, "--json"])
    if want("trace"):
        pair = sc.get("trace")
        if not pair and gold:
            pair = next(([f["check"]["source"], f["check"]["target"]] for f in gold.get("facts", [])
                         if f["check"]["kind"] == "trace"), None)
        if pair:
            rec, out = r.run("trace", ["trace", pair[0], pair[1], "--json"])
            rec["answer_status"] = _answer_status(out)
        else:
            skipped.append("trace: no pair in the scenario or the gold")
    if want("q"):
        for i, qq in enumerate(sc["q"], 1):
            r.run(f"q{i}", ["q", qq, "--json"])
    edit_spec = pick_edit(clone, files, sc)
    if want("check"):
        cf = pick_check_file(clone, files, sc, edit_spec)
        if cf:
            rec, out = r.run("check", ["check", cf, "--json"])
            rec["answer_status"] = _answer_status(out)
        else:
            skipped.append("check: no Python, Java or Kotlin file (check reads only those)")
    if want("map"):
        for v in sc["map_views"]:
            r.run(f"map:{v}", ["map", ".", "--view", v, "--json"])
    if want("routes"):
        r.run("routes", ["routes", "--json"])
    if want("schema"):
        r.run("schema", ["schema", "--json"])
    if want("taint") and "python" in repo.get("language", "").lower():
        r.run("taint", ["taint", "--json"])
    if want("doctor"):
        r.run("doctor", ["doctor", "--json"])
    if want("mcp") and module == "verinoda":
        for tool, qn in (("project_query", sc["queries"][0]), ("analyze", sc["analyze"][0])):
            r.run(f"mcp:{tool}", [], command="mcp",
                  raw_argv=[python, "-P", str(MCP_PROBE), str(clone), tool, qn])
    edit_rec = None
    if want("edit") and edit_spec:
        ed = Edit(clone, edit_spec)
        ed.apply()
        try:
            r.run("update", ["update", ".", "--json"])
            r.run("review", ["review", ".", "--json"])
        finally:
            ed.revert()
        r.run("update:revert", ["update", ".", "--json"])
        edit_rec = {k: edit_spec.get(k) for k in ("file", "after_line", "insert")}
    dirty_after = dirty_files(clone)
    res = summarize(repo, files, r.steps, gold_results, edit_rec, started)
    res.update(environment=env, skipped=skipped, notes=notes, config_dir="isolated (empty, per repository)",
               clean_after=None if dirty_after is None else not dirty_after,
               dirty_after=dirty_after or [])
    return res


def _median(xs):
    return round(statistics.median(xs), 3) if xs else None


def _gold_counts(facts: list[dict], v: int) -> dict:
    sel = [g for g in facts if g.get("v", 1) == v]
    return {"hits": sum(g["hit"] for g in sel), "total": len(sel)}


def summarize(repo: dict, files: list[str], steps: list[dict], gold_results: list[dict], edit: dict | None,
              started: str) -> dict:
    by = {s["step"]: s for s in steps}

    def secs(name):
        return by[name]["seconds"] if name in by else None

    v1 = _gold_counts(gold_results, 1)
    return {
        "name": repo["name"], "tag": repo["tag"], "sha": repo["sha"], "language": repo.get("language"),
        "started": started, "files": len(files),
        "scan_s": secs("scan"), "update_s": secs("update"),
        "query_median_s": _median([s["seconds"] for s in steps if re.fullmatch(r"query\d+", s["step"])]),
        "analyze_median_s": _median([s["seconds"] for s in steps if re.fullmatch(r"analyze\d+", s["step"])]),
        "crashes": [s["step"] for s in steps if s["crash"]],
        "timeouts": [s["step"] for s in steps if s["timeout"]],
        "max_stdout_bytes": max((s["stdout_bytes"] for s in steps), default=0),
        # hits/total: the frozen v1 facts (comparable across runs); v2: the corrected checks added later
        "gold": {"hits": v1["hits"], "total": v1["total"], "v2": _gold_counts(gold_results, 2),
                 "facts": gold_results},
        "edit": edit, "steps": steps,
    }


# -- the run -----------------------------------------------------------------------------------------------

def python_version(python: str) -> tuple[int, int] | None:
    try:
        r = subprocess.run([python, "-c", "import sys;print(sys.version_info[0], sys.version_info[1])"],
                           capture_output=True, text=True, timeout=60)
        major, minor = r.stdout.split()
        return int(major), int(minor)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def environment(python: str, verinoda_root: Path) -> dict:
    v = subprocess.run([python, "-P", "-c", "import verinoda,sys;print(getattr(verinoda,'__version__','?'));"
                        "print(sys.version.split()[0])"], capture_output=True, text=True,
                       env=dict(os.environ, PYTHONPATH=str(verinoda_root)))
    lines = v.stdout.split()
    commit = _git(["-C", str(verinoda_root), "rev-parse", "--short", "HEAD"]).stdout.strip()
    dirty = _git(["-C", str(verinoda_root), "status", "--porcelain", "--untracked-files=no"])
    import platform
    return {"verinoda_version": lines[0] if lines else None, "python": lines[1] if len(lines) > 1 else None,
            "verinoda_commit": commit or None,
            "verinoda_dirty": bool(dirty.stdout.strip()) if dirty.returncode == 0 else None,
            "platform": platform.platform(), "cpus": os.cpu_count()}


def select(manifest: dict, names: list[str] | None, smallest: int | None, all_: bool) -> list[dict]:
    repos = manifest["repos"]
    if names:
        idx = {r["name"]: r for r in repos}
        unknown = [n for n in names if n not in idx]
        if unknown:
            raise SystemExit(f"not in the manifest: {', '.join(unknown)}")
        return [idx[n] for n in names]
    if smallest:
        return sorted(repos, key=lambda r: r.get("files_approx", 0))[:smallest]
    if all_:
        return list(repos)
    raise SystemExit("name --repos, --smallest N or --all")


def _report():
    """report.py of this folder, loaded by path (its name would clash on sys.path)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("realworld_report", HERE / "report.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def merge(old: dict | None, run: dict) -> dict:
    """``run``'s repositories added to ``old`` (a repository run again replaces its entry); runs appended."""
    if not old:
        return run
    keep = {r["name"]: r for r in old.get("repos", [])}
    for r in run["repos"]:
        keep[r["name"]] = r
    return dict(run, repos=list(keep.values()), runs=old.get("runs", []) + run["runs"])


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def merge_results(path: Path, run: dict) -> dict:
    """Add this run's repositories to the results file of the day (a repository run again replaces its entry)."""
    old = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    merged = merge(old, run)
    _write_json(path, merged)
    return merged


def _failed(r: dict) -> bool:
    return bool(r.get("crashes") or r.get("timeouts") or r.get("error") or r.get("clean_after") is False)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repos", help="comma-separated manifest names (owner/name)")
    ap.add_argument("--smallest", type=int, help="the N smallest repositories of the manifest")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--work", type=Path, default=DEFAULT_WORK, help=f"clone folder (default {DEFAULT_WORK})")
    ap.add_argument("--out", type=Path, help="results folder (default benchmarks/results/realworld-<date>)")
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--verinoda-root", type=Path, default=ROOT, help="the Verinoda checkout under test")
    ap.add_argument("--timeout-scale", type=float, default=1.0)
    ap.add_argument("--manifest", type=Path)
    a = ap.parse_args(argv)
    pv = python_version(a.python)
    if pv is None or pv < MIN_PYTHON:
        raise SystemExit(f"{a.python} is Python {'.'.join(map(str, pv)) if pv else '?'}: the runner needs "
                         f"{'.'.join(map(str, MIN_PYTHON))} or later (python -P)")
    manifest = load_manifest(a.manifest)
    repos = select(manifest, a.repos.split(",") if a.repos else None, a.smallest, a.all)
    work = a.work.resolve()
    if "onedrive" in str(work).lower():
        print(f"warning: {work} is under OneDrive; sync slows the index and locks files", file=sys.stderr)
    day = _dt.date.today().isoformat()
    out = a.out or ROOT / "benchmarks" / "results" / f"realworld-{day}"
    out.mkdir(parents=True, exist_ok=True)
    env = environment(a.python, a.verinoda_root)
    results_path = out / "results.json"
    old = json.loads(results_path.read_text(encoding="utf-8")) if results_path.is_file() else None
    at = _dt.datetime.now().isoformat(timespec="seconds")
    results: list[dict] = []
    report = _report()
    for repo in repos:
        print(f"== {repo['name']} {repo['tag']}", flush=True)
        try:
            clone = ensure_clone(repo, work)
            assert_clean(clone)
            res = run_repo(repo, clone, python=a.python, verinoda_root=a.verinoda_root,
                           gold=load_gold(repo["name"]), timeout_scale=a.timeout_scale,
                           log=lambda s: print(s, flush=True), env=env)
        except Exception as exc:  # one repository's failure is recorded; the others still run
            res = {"name": repo["name"], "tag": repo["tag"], "sha": repo["sha"], "environment": env,
                   "error": f"{type(exc).__name__}: {exc}"}
            print(f"  error: {res['error']}", flush=True)
        results.append(res)
        run = {"kind": "verinoda-realworld", "date": day, "environment": env,
               "runs": [{"at": at, "environment": env, "repos": [r["name"] for r in results]}],
               "paths_sanitized": ["<CORPUS>", "<REPO>", "<HOME>"], "repos": results}
        merged = merge(old, run)                 # written after each repository: a later failure loses nothing
        _write_json(results_path, merged)
        report.write_summary(merged, out / "summary.md")
    print(f"results: {results_path}\nsummary: {out / 'summary.md'}")
    return 1 if any(_failed(r) for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
