"""Real-world end-user benchmark: Verinoda run on pinned open-source repositories.

Usage::

    python benchmarks/realworld/run.py --repos gin-gonic/gin [--work C:/vbench] [--out DIR]
    python benchmarks/realworld/run.py --smallest 2
    python benchmarks/realworld/run.py --all

For each repository of ``manifest.json``: a shallow clone at the pinned tag (an existing clone at the
pinned sha is reused), the sha checked, then the end-user scenarios run as subprocesses
(``python -P -m verinoda ...`` with ``PYTHONPATH`` set to the Verinoda checkout under test), each timed,
with its exit code, the tail of its stderr and the size of its output. The gold facts of
``gold/<owner>__<name>.json`` are checked against Verinoda's answers.

The repository's code is never executed: no ``trust``, ``observe``, ``experiment``, ``probe`` or
``mutate``, no ``--run-tests``, ``--observe``, ``--checker`` or ``--env``; :func:`guard_argv` refuses
them before any process starts. The clone is read by Verinoda and by ``git`` (clone, rev-parse,
ls-files); one scripted one-line edit is made and reverted.

Standard library only (Verinoda itself is run as a subprocess).
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
DEFAULT_WORK = Path(os.environ.get("VERINODA_BENCH_WORK", "C:/vbench" if os.name == "nt" else "/tmp/vbench"))

# Commands and flags that would run the repository's code (its tests, its interpreter, its tools).
FORBIDDEN_COMMANDS = frozenset({"trust", "observe", "experiment", "probe", "mutate", "setup", "hooks",
                                "install", "agent-hooks", "ui", "research", "compare", "index"})
FORBIDDEN_FLAGS = frozenset({"--run-tests", "--observe", "--checker", "--env", "--registry", "--deps"})

# Exit codes each command documents as a normal answer; anything else is recorded as a crash.
OK_CODES = {
    "init": {0}, "scan": {0}, "update": {0}, "query": {0}, "analyze": {0, 3},
    "trace": {0, 2},            # 2: no directed path
    "q": {0, 1, 3},             # 1: no row, 3: cut by a budget
    "check": {0, 3, 4},         # 3: something absent, 4: a file not checked (another language)
    "map": {0}, "routes": {0}, "schema": {0, 3}, "taint": {0, 3}, "review": {0, 3},
    "doctor": {0, 1}, "mcp": {0},
}

TIMEOUTS = {"scan": 1800, "update": 900, "analyze": 600, "review": 600, "init": 120, "doctor": 300,
            "query": 300, "trace": 300, "q": 300, "check": 300, "map": 600, "routes": 300, "schema": 300,
            "taint": 600, "mcp": 600}

STDERR_TAIL = 1200
TRACEBACK = "Traceback (most recent call last)"

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


class Forbidden(ValueError):
    """An argv that would run the repository's code."""


def guard_argv(argv: list[str]) -> None:
    """Refuse a Verinoda argv that could execute the analysed repository's code."""
    if argv and argv[0] in FORBIDDEN_COMMANDS:
        raise Forbidden(f"never run on a benchmark repository: verinoda {argv[0]}")
    for a in argv:
        flag = a.split("=", 1)[0]
        if flag in FORBIDDEN_FLAGS:
            raise Forbidden(f"never run on a benchmark repository: {flag}")


def slug(name: str) -> str:
    return name.replace("/", "__")


def load_manifest(path: Path | None = None) -> dict:
    return json.loads((path or HERE / "manifest.json").read_text(encoding="utf-8"))


def load_gold(name: str, gold_dir: Path | None = None) -> dict | None:
    p = (gold_dir or HERE / "gold") / f"{slug(name)}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None


# -- clone ---------------------------------------------------------------------------------------------

def _git(args: list[str], cwd: Path | None = None, timeout: int = 600) -> subprocess.CompletedProcess:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, env=env)


def head_sha(clone: Path) -> str | None:
    r = _git(["-C", str(clone), "rev-parse", "HEAD"])
    return r.stdout.strip() if r.returncode == 0 else None


def git_clone(repo: dict, dest: Path) -> None:
    """Shallow clone at the pinned tag; no submodules, no hooks of ours, nothing checked out but the tree."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    r = _git(["-c", "advice.detachedHead=false", "-c", "core.autocrlf=false", "clone", "--quiet",
              "--depth", "1", "--branch", repo["tag"], "--no-recurse-submodules", repo["url"], str(dest)],
             timeout=1800)
    if r.returncode != 0:
        raise RuntimeError(f"clone of {repo['name']} failed: {r.stderr.strip()[-500:]}")


def ensure_clone(repo: dict, work: Path, clone_fn=git_clone, sha_fn=head_sha) -> Path:
    """The clone of ``repo`` under ``work`` at the pinned sha: reused when it is there, else cloned.

    A folder at another sha is never deleted (it may be someone's work): that is an error."""
    dest = work / slug(repo["name"])
    if not dest.exists():
        clone_fn(repo, dest)
    sha = sha_fn(dest)
    if sha != repo["sha"]:
        raise RuntimeError(f"{dest} is at {sha}, not the pinned {repo['sha']} ({repo['tag']})")
    return dest


def tracked_files(clone: Path) -> list[str]:
    """The repository's files (git ls-files; a folder walk when it is not a git checkout)."""
    r = _git(["-C", str(clone), "ls-files"])
    if r.returncode == 0:
        return [x for x in r.stdout.splitlines() if x]
    return sorted(p.relative_to(clone).as_posix() for p in clone.rglob("*")
                  if p.is_file() and not {".git", ".verinoda"} & set(p.relative_to(clone).parts))


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


class Runner:
    """Runs the scenarios of one repository and records each step."""

    def __init__(self, clone: Path, *, python: str = sys.executable, verinoda_root: Path = ROOT,
                 module: str = "verinoda", timeout_scale: float = 1.0, timeouts: dict | None = None,
                 sanitize: Sanitizer | None = None, log=None):
        self.clone = Path(clone)
        self.python = python
        self.verinoda_root = Path(verinoda_root)
        self.module = module
        self.scale = timeout_scale
        self.timeouts = dict(TIMEOUTS, **(timeouts or {}))
        self.steps: list[dict] = []
        self.sanitize = sanitize or Sanitizer({str(self.clone): "<CORPUS>", str(self.verinoda_root): "<REPO>",
                                               str(Path.home()): "<HOME>"})
        self.log = log or (lambda s: None)

    def env(self) -> dict:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(self.verinoda_root)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        env["GIT_TERMINAL_PROMPT"] = "0"
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
        tb = TRACEBACK in stderr
        reason = None
        if timed_out:
            reason = f"timeout after {timeout:.0f}s"
        elif tb:
            reason = "traceback in stderr"
        elif code not in ok:
            reason = f"exit {code} not in {sorted(ok)}"
        json_ok = None
        if "--json" in args or raw_argv is not None:
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

def _strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v)


def _norm(s: str) -> str:
    return s.replace("\\", "/")


def cites(obj, loc: str) -> bool:
    """Some string of ``obj`` names ``loc`` (path:line) exactly: not path:2360 for path:236."""
    pat = re.compile(r"(?:^|[\s(\"'/])" + re.escape(_norm(loc)) + r"(?![0-9])")
    for s in _strings(obj):
        s = _norm(s)
        if s == _norm(loc) or pat.search(s):
            return True
    return False


def _at_files(obj) -> set[str]:
    out = set()
    for s in _strings(obj):
        m = re.match(r"^(.+?):\d+(?:-\d+)?$", _norm(s))
        if m:
            out.add(m.group(1))
    return out


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


def judge(check: dict, exit_code: int | None, stdout: str) -> tuple[bool, str]:
    """Whether Verinoda's answer holds the gold fact, and why not."""
    try:
        data = json.loads(stdout)
    except ValueError:
        return False, f"no JSON (exit {exit_code})"
    kind = check["kind"]
    if kind == "q":
        rows = data.get("rows") or []
        if not rows:
            return False, "no row"
        missing = [c for c in check.get("cites", []) if not cites(rows, c)]
        return (not missing), ("" if not missing else f"rows do not cite {', '.join(missing)}")
    if kind == "trace":
        paths = data.get("paths") or []
        if not paths:
            return False, f"no path ({data.get('status')})"
        files = _at_files(paths) | _at_files(data.get("resolved") or {})
        missing = [f for f in check.get("via_files", []) if _norm(f) not in files]
        return (not missing), ("" if not missing else f"path does not reach {', '.join(missing)}")
    if kind == "query":
        k = int(check.get("top_k", 8))
        files = [_norm(i.get("file", "")) for i in (data.get("items") or [])[:k]]
        want = _norm(check["expect_file"])
        if want in files:
            return True, f"rank {files.index(want) + 1}"
        return False, f"not in top {k}: {files}"
    if kind == "routes":
        table = data.get("route_table") or []
        for r in table:
            if (r.get("path") == check["path"] and (not check.get("method") or
                                                    str(r.get("method", "")).upper() == check["method"].upper())
                    and check.get("handler", "") in json.dumps(r)):
                return True, ""
        return False, "route not in the table"
    if kind == "schema":
        names = {t.get("name") for t in data.get("tables") or []}
        return (check["table"] in names), ("" if check["table"] in names else f"tables: {sorted(names)[:20]}")
    return False, f"unknown kind {kind}"


def check_gold(runner: Runner, gold: dict) -> list[dict]:
    out = []
    for fact in gold.get("facts", []):
        chk = fact["check"]
        rec, stdout = runner.run(f"gold:{fact['id']}", gold_argv(chk), ok={0, 1, 2, 3})
        hit, why = (False, rec["reason"] or "") if rec["timeout"] or rec["crash"] else judge(chk, rec["exit"], stdout)
        out.append({"id": fact["id"], "kind": chk["kind"], "hit": hit, "why": why, "seconds": rec["seconds"]})
    return out


# -- the scripted edit ----------------------------------------------------------------------------------

def pick_edit(clone: Path, files: list[str], scenario: dict) -> dict:
    """The one-line edit: the scenario's, else a comment line appended to the largest source file."""
    if scenario.get("edit"):
        return scenario["edit"]
    best = None
    for f in files:
        ext = Path(f).suffix
        if ext not in SOURCE_EXT or "test" in f.lower() or "vendor" in f.lower():
            continue
        try:
            size = (clone / f).stat().st_size
        except OSError:
            continue
        if best is None or size > best[0]:
            best = (size, f, ext)
    if best is None:
        return {}
    return {"file": best[1], "after_line": None, "insert": f"{SOURCE_EXT[best[2]]} verinoda-bench edit"}


class Edit:
    """Insert one line (after ``after_line``, or at the end) and restore the exact bytes afterwards."""

    def __init__(self, clone: Path, spec: dict):
        self.path = clone / spec["file"]
        self.spec = spec
        self.original: bytes | None = None

    def apply(self) -> None:
        self.original = self.path.read_bytes()
        text = self.original.decode("utf-8")
        nl = "\r\n" if "\r\n" in text else "\n"
        lines = text.split(nl)
        at = self.spec.get("after_line")
        idx = len(lines) if at is None else int(at)
        if at is None and lines and lines[-1] == "":
            idx = len(lines) - 1
        lines.insert(idx, self.spec["insert"])
        self.path.write_bytes(nl.join(lines).encode("utf-8"))

    def revert(self) -> None:
        if self.original is not None:
            self.path.write_bytes(self.original)
            self.original = None


# -- one repository ---------------------------------------------------------------------------------------

MCP_PROBE = HERE / "mcp_probe.py"


def run_repo(repo: dict, clone: Path, *, python: str = sys.executable, verinoda_root: Path = ROOT,
             module: str = "verinoda", gold: dict | None = None, timeout_scale: float = 1.0,
             steps: set[str] | None = None, log=None) -> dict:
    """Every scenario on one clone; ``steps`` limits them (by group name) for tests."""
    want = (lambda g: True) if steps is None else (lambda g: g in steps)
    sc = dict(DEFAULT_SCENARIO, **(repo.get("scenario") or {}))
    r = Runner(clone, python=python, verinoda_root=verinoda_root, module=module, timeout_scale=timeout_scale,
               log=log)
    files = tracked_files(clone)
    started = _dt.datetime.now().isoformat(timespec="seconds")
    vn = clone / ".verinoda"
    if want("init"):
        if vn.is_dir() and vn.parent == clone:
            shutil.rmtree(vn)
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
            r.run("trace", ["trace", pair[0], pair[1], "--json"])
    if want("q"):
        for i, qq in enumerate(sc["q"], 1):
            r.run(f"q{i}", ["q", qq, "--json"])
    edit_spec = pick_edit(clone, files, sc)
    if want("check") and (sc.get("check_file") or edit_spec.get("file")):
        r.run("check", ["check", sc.get("check_file") or edit_spec["file"], "--json"])
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
    return summarize(repo, files, r.steps, gold_results, edit_rec, started)


def _median(xs):
    return round(statistics.median(xs), 3) if xs else None


def summarize(repo: dict, files: list[str], steps: list[dict], gold_results: list[dict], edit: dict | None,
              started: str) -> dict:
    by = {s["step"]: s for s in steps}

    def secs(name):
        return by[name]["seconds"] if name in by else None

    return {
        "name": repo["name"], "tag": repo["tag"], "sha": repo["sha"], "language": repo.get("language"),
        "started": started, "files": len(files),
        "scan_s": secs("scan"), "update_s": secs("update"),
        "query_median_s": _median([s["seconds"] for s in steps if re.fullmatch(r"query\d+", s["step"])]),
        "analyze_median_s": _median([s["seconds"] for s in steps if re.fullmatch(r"analyze\d+", s["step"])]),
        "crashes": [s["step"] for s in steps if s["crash"]],
        "timeouts": [s["step"] for s in steps if s["timeout"]],
        "max_stdout_bytes": max((s["stdout_bytes"] for s in steps), default=0),
        "gold": {"hits": sum(g["hit"] for g in gold_results), "total": len(gold_results),
                 "facts": gold_results},
        "edit": edit, "steps": steps,
    }


# -- the run -----------------------------------------------------------------------------------------------

def environment(python: str, verinoda_root: Path) -> dict:
    v = subprocess.run([python, "-P", "-c", "import verinoda,sys;print(getattr(verinoda,'__version__','?'));"
                        "print(sys.version.split()[0])"], capture_output=True, text=True,
                       env=dict(os.environ, PYTHONPATH=str(verinoda_root)))
    lines = v.stdout.split()
    commit = _git(["-C", str(verinoda_root), "rev-parse", "--short", "HEAD"]).stdout.strip()
    import platform
    return {"verinoda_version": lines[0] if lines else None, "python": lines[1] if len(lines) > 1 else None,
            "verinoda_commit": commit or None, "platform": platform.platform(), "cpus": os.cpu_count()}


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


def merge_results(path: Path, run: dict) -> dict:
    """Add this run's repositories to the results file of the day (a repository run again replaces its entry)."""
    if path.is_file():
        old = json.loads(path.read_text(encoding="utf-8"))
        keep = {r["name"]: r for r in old.get("repos", [])}
        for r in run["repos"]:
            keep[r["name"]] = r
        run = dict(run, repos=list(keep.values()), runs=old.get("runs", []) + run["runs"])
    path.write_text(json.dumps(run, indent=1, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    return run


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
    manifest = load_manifest(a.manifest)
    repos = select(manifest, a.repos.split(",") if a.repos else None, a.smallest, a.all)
    work = a.work.resolve()
    if "onedrive" in str(work).lower():
        print(f"warning: {work} is under OneDrive; sync slows the index and locks files", file=sys.stderr)
    day = _dt.date.today().isoformat()
    out = a.out or ROOT / "benchmarks" / "results" / f"realworld-{day}"
    out.mkdir(parents=True, exist_ok=True)
    env = environment(a.python, a.verinoda_root)
    results = []
    for repo in repos:
        print(f"== {repo['name']} {repo['tag']}", flush=True)
        try:
            clone = ensure_clone(repo, work)
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            results.append({"name": repo["name"], "tag": repo["tag"], "sha": repo["sha"], "error": str(exc)})
            print(f"  skipped: {exc}", flush=True)
            continue
        res = run_repo(repo, clone, python=a.python, verinoda_root=a.verinoda_root,
                       gold=load_gold(repo["name"]), timeout_scale=a.timeout_scale,
                       log=lambda s: print(s, flush=True))
        results.append(res)
    run = {"kind": "verinoda-realworld", "date": day, "environment": env,
           "runs": [{"at": _dt.datetime.now().isoformat(timespec="seconds"), "repos": [r["name"] for r in results]}],
           "paths_sanitized": ["<CORPUS>", "<REPO>", "<HOME>"], "repos": results}
    merged = merge_results(out / "results.json", run)
    _report().write_summary(merged, out / "summary.md")
    print(f"results: {out / 'results.json'}\nsummary: {out / 'summary.md'}")
    return 1 if any(r.get("crashes") or r.get("timeouts") or r.get("error") for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
