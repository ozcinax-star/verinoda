"""Edit-to-fresh benchmark for the Claude Code mod ``verinoda-live``.

The mod re-indexes once per turn with ``verinoda update --fast --repo ROOT``. This measures that path
against a plain ``verinoda update`` on a twin copy edited the same way (same bytes), step by step:

- ``seconds``: wall clock of the update command (what the turn's status line waits for)
- ``text_now``: whether a plain-text ``query <new name>`` returns the new definition right after the command
- ``symbol_now``: whether ``query symbol:<new name>`` (answered from the graph) finds it right after;
  ``graph_pending``: the graph was still behind the tree (build running, or not yet started) then
- ``graph_seconds``: for ``--fast``, how long until the graph caught up (no build running, no file changed
  since the latest snapshot)
- ``graph_fresh``: whether ``symbol:`` finds it once the build is done, asked before anything else updates
  the index; ``gone_absent``: a deleted symbol is no longer found; ``leftover_changes``: what a following
  plain ``update`` still reported as changed (must be 0)

Usage::

    python benchmarks/mod_live/run.py                      # examples/orders_app, 3 runs
    python benchmarks/mod_live/run.py --corpus PATH --runs 5 --out benchmarks/results/mod-live-YYYY-MM-DD

The corpus is copied (``.verinoda``, ``.git``, caches left out) into two folders under a work dir and
committed there; the original is never touched. Verinoda runs as ``python -m verinoda`` with the
interpreter given by ``--python`` (default: this one). Standard library only.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
DEFAULT_CORPUS = ROOT / "examples" / "orders_app"
IGNORE = shutil.ignore_patterns(".verinoda", ".git", "__pycache__", "*.pyc", ".pytest_cache", ".venv", "*.db")
GRAPH_WAIT_SECONDS = 600.0


# --- edits: deterministic, so both twins get the same bytes ---------------------------------------


@dataclass(frozen=True)
class Edit:
    kind: str  # comment | body | batch | add | del
    files: tuple[str, ...]  # repository-relative, posix
    symbol: str | None  # a new name the edit introduces (None: nothing to find)
    gone: str | None = None  # a name the edit removes


def python_files(repo: Path) -> list[str]:
    """The corpus's own Python files, sorted, tests and the benchmark's own modules left out."""
    out = []
    for p in sorted(repo.rglob("*.py")):
        rel = p.relative_to(repo).as_posix()
        if "/tests/" in f"/{rel}" or rel.split("/")[-1].startswith(("test_", "bench_mod_")):
            continue
        out.append(rel)
    return out


def symbol_name(step: str, run: int) -> str:
    return f"modlive_{step.replace(':', '_')}_r{run}"


def apply_edit(repo: Path, step: str, run: int) -> Edit:
    """Make the edit ``step`` (``comment``, ``body``, ``batch:K``, ``add``, ``del``) for run ``run``."""
    kind, _, arg = step.partition(":")
    files = python_files(repo)
    if not files:
        raise ValueError(f"no Python files to edit in {repo}")
    name = symbol_name(step, run)
    if kind == "comment":
        rel = files[0]
        _append(repo / rel, f"# {name}\n")
        return Edit(kind, (rel,), None)
    if kind in ("body", "batch"):
        count = 1 if kind == "body" else int(arg or 5)
        chosen = tuple(files[i % len(files)] for i in range(count))
        for i, rel in enumerate(dict.fromkeys(chosen)):
            _append(repo / rel, f"\n\ndef {name}_{i}(value):\n    return value + {i}\n")
        return Edit(kind, tuple(dict.fromkeys(chosen)), f"{name}_0")
    if kind == "add":
        rel = f"bench_mod_{run}.py"
        (repo / rel).write_text(f"def {name}(value):\n    return value * 2\n", encoding="utf-8", newline="\n")
        return Edit(kind, (rel,), name)
    if kind == "del":
        rel = f"bench_mod_{run}.py"
        target = repo / rel
        if not target.exists():
            raise ValueError("'del' needs an earlier 'add' in the same run")
        target.unlink()
        return Edit(kind, (rel,), None, gone=symbol_name("add", run))
    raise ValueError(f"unknown step {step!r}")


def _append(path: Path, text: str) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


# --- running verinoda ---------------------------------------------------------------------------


@dataclass
class Ran:
    argv: list[str]
    exit_code: int
    seconds: float
    stdout: str
    stderr_tail: str

    def json(self) -> dict:
        try:
            value = json.loads(self.stdout)
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}


def run_verinoda(python: str, repo: Path, *args: str, timeout: float = 900.0) -> Ran:
    """``python -m verinoda <args>`` on ``repo``, named explicitly (``init`` by PATH, the rest by --repo)."""
    where = [str(repo)] if args[0] == "init" else ["--repo", str(repo)]
    argv = [python, "-m", "verinoda", *args, *where]
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    t0 = time.perf_counter()
    p = subprocess.run(argv, cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=timeout, env=env, check=False)
    return Ran(argv[2:], p.returncode, time.perf_counter() - t0, p.stdout, p.stderr[-600:])


_GRAPH_STATE = (
    "import json, sys; from pathlib import Path; from verinoda import buildlock, freshness; "
    "r = Path(sys.argv[1]); print(json.dumps({'locked': buildlock.is_locked(r), "
    "'behind': freshness.check(r, with_new=True).get('count', 0)}))"
)


def graph_behind(python: str, repo: Path) -> bool:
    """Whether the graph is not yet built over the working tree.

    ``update --fast`` records no snapshot until its background build ends, so the files changed since
    the latest snapshot are the ones the graph is behind on; a held build lock means a build is running.
    The lock alone is not enough: right after the command the spawned build may not have taken it yet.
    """
    p = subprocess.run([python, "-c", _GRAPH_STATE, str(repo)], capture_output=True, text=True, check=False)
    try:
        state = json.loads(p.stdout)
    except ValueError:
        return True
    return bool(state["locked"]) or state["behind"] > 0


def wait_graph(python: str, repo: Path, limit: float = GRAPH_WAIT_SECONDS) -> float | None:
    """Seconds until the graph has caught up with the tree, or None when it has not after ``limit``."""
    t0 = time.perf_counter()
    while graph_behind(python, repo):
        if time.perf_counter() - t0 > limit:
            return None
        time.sleep(0.25)
    return time.perf_counter() - t0


def hit_symbols(query_json: dict) -> set[str]:
    """The symbols a ``query --json`` answer returned as items (``name()`` read as ``name``).

    Only ``items`` count: the answer echoes the question, so a plain text search of the output would
    find any name that was asked for."""
    items = query_json.get("items")
    return {str(i.get("symbol", "")).removesuffix("()") for i in items if isinstance(i, dict)}         if isinstance(items, list) else set()


def finds_text(python: str, repo: Path, name: str) -> bool:
    """Whether a plain-text query for ``name`` returns an item whose excerpt defines it."""
    out = run_verinoda(python, repo, "query", name, "--json", "--max-items", "5")
    items = out.json().get("items")
    return out.exit_code == 0 and isinstance(items, list) and any(
        f"def {name}(" in str(i.get("excerpt", "")) for i in items if isinstance(i, dict))


def finds(python: str, repo: Path, symbol: str) -> bool:
    """Whether ``query symbol:<symbol>`` returns an item that is that symbol."""
    out = run_verinoda(python, repo, "query", f"symbol:{symbol}", "--json", "--max-items", "5")
    return out.exit_code == 0 and symbol in hit_symbols(out.json())


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=bench", "-c", "user.email=bench@localhost", "-c", "core.autocrlf=false",
                    *args], cwd=repo, check=True, capture_output=True)


def prepare(python: str, corpus: Path, dest: Path) -> dict:
    shutil.copytree(corpus, dest, ignore=IGNORE)
    git(dest, "init", "-q")
    git(dest, "add", "-A")
    git(dest, "commit", "-q", "-m", "corpus")
    init = run_verinoda(python, dest, "init", "--json")
    scan = run_verinoda(python, dest, "scan", "--json")
    if init.exit_code or scan.exit_code:
        raise RuntimeError(f"indexing {dest} failed: {init.stderr_tail or scan.stderr_tail}")
    return {"scan_seconds": round(scan.seconds, 3), "files": len(python_files(dest))}


# --- one step on both twins ---------------------------------------------------------------------


@dataclass
class Measure:
    mode: str  # fast | full
    seconds: float
    exit_code: int
    text_now: bool | None
    symbol_now: bool | None
    graph_pending: bool = False  # the graph was still behind the tree when symbol_now was asked
    graph_seconds: float | None = None
    graph_fresh: bool | None = None
    gone_absent: bool | None = None
    leftover_changes: int | None = None
    background: dict | None = None  # (fast) what `update --fast` said about its background build: started, pid, why
    stderr_tail: str = ""


@dataclass
class StepResult:
    run: int
    step: str
    files: list[str]
    measures: list[Measure] = field(default_factory=list)


def measure(python: str, repo: Path, edit: Edit, *, fast: bool) -> Measure:
    args = ["update", "--json"] + (["--fast"] if fast else [])
    ran = run_verinoda(python, repo, *args)
    ended = time.perf_counter()
    # graph first and right away: the background build may finish while a slower check runs
    pending = graph_behind(python, repo) if fast else False
    symbol_now = finds(python, repo, edit.symbol) if edit.symbol else None
    text_now = finds_text(python, repo, edit.symbol) if edit.symbol else None
    m = Measure("fast" if fast else "full", round(ran.seconds, 3), ran.exit_code, text_now, symbol_now,
                graph_pending=pending, stderr_tail=ran.stderr_tail if ran.exit_code else "")
    if fast:
        m.background = ran.json().get("background")
        # from the command's end, the checks above included: the build runs on while they do
        caught_up = wait_graph(python, repo) is not None
        m.graph_seconds = round(ran.seconds + time.perf_counter() - ended, 3) if caught_up else None
    # what the background build (or the full update) left, before anything else updates the index
    m.graph_fresh = finds(python, repo, edit.symbol) if edit.symbol else None
    m.gone_absent = (not finds(python, repo, edit.gone)) if edit.gone else None
    after = run_verinoda(python, repo, "update", "--json")  # must find nothing left to do
    changed = after.json().get("changed", {})
    m.leftover_changes = sum(len(v) for v in changed.values() if isinstance(v, list))
    return m


def run_benchmark(python: str, corpus: Path, work: Path, steps: list[str], runs: int,
                  log=print) -> dict:
    twins = {"fast": work / "fast", "full": work / "full"}
    setup = {mode: prepare(python, corpus, path) for mode, path in twins.items()}
    results: list[StepResult] = []
    for run in range(runs):
        for step in steps:
            edits = {mode: apply_edit(path, step, run) for mode, path in twins.items()}
            assert edits["fast"] == edits["full"]
            row = StepResult(run, step, list(edits["fast"].files))
            # alternate which twin goes first, so a warm disk cache favours neither
            order = ["fast", "full"] if (run + steps.index(step)) % 2 == 0 else ["full", "fast"]
            for mode in order:
                row.measures.append(measure(python, twins[mode], edits[mode], fast=mode == "fast"))
            results.append(row)
            log(format_row(row))
    return {"setup": setup, "steps": [asdict(r) for r in results], "summary": summarize(results)}


# --- reporting ----------------------------------------------------------------------------------


def format_row(row: StepResult) -> str:
    parts = []
    for m in sorted(row.measures, key=lambda m: m.mode):
        extra = f" graph {m.graph_seconds}s pending={m.graph_pending}" if m.mode == "fast" else ""
        parts.append(f"{m.mode} {m.seconds}s{extra} text_now={m.text_now} symbol_now={m.symbol_now} "
                     f"graph={m.graph_fresh} gone={m.gone_absent}")
    return f"run {row.run} {row.step:<9} | " + " | ".join(parts)


def summarize(rows: list[StepResult]) -> dict:
    out: dict[str, dict] = {}
    for step in dict.fromkeys(r.step for r in rows):
        by_mode: dict[str, dict] = {}
        for mode in ("fast", "full"):
            ms = [m for r in rows if r.step == step for m in r.measures if m.mode == mode]
            secs = [m.seconds for m in ms]
            graph = [m.graph_seconds for m in ms if m.graph_seconds is not None]
            by_mode[mode] = {
                "n": len(ms),
                "median_seconds": round(statistics.median(secs), 3) if secs else None,
                "max_seconds": round(max(secs), 3) if secs else None,
                "median_graph_seconds": round(statistics.median(graph), 3) if graph else None,
                "errors": sum(1 for m in ms if m.exit_code != 0),
                "text_now": _rate(m.text_now for m in ms),
                "symbol_now": _rate(m.symbol_now for m in ms),
                "graph_pending": _rate(m.graph_pending for m in ms),
                "graph_fresh": _rate(m.graph_fresh for m in ms),
                "gone_absent": _rate(m.gone_absent for m in ms),
                "leftover_changes": sum(m.leftover_changes or 0 for m in ms),
            }
        fast, full = by_mode["fast"]["median_seconds"], by_mode["full"]["median_seconds"]
        by_mode["speedup"] = round(full / fast, 2) if fast and full else None
        out[step] = by_mode
    return out


def _rate(values) -> str | None:
    vals = [v for v in values if v is not None]
    return f"{sum(vals)}/{len(vals)}" if vals else None


def sanitize(value, replacements: dict[str, str]):
    """Replace machine paths (both slash styles) with placeholders, recursively."""
    if isinstance(value, str):
        for path, token in replacements.items():
            for spelling in {path, path.replace("\\", "/"), path.replace("/", "\\")}:
                value = value.replace(spelling, token)
        return value
    if isinstance(value, list):
        return [sanitize(v, replacements) for v in value]
    if isinstance(value, dict):
        return {k: sanitize(v, replacements) for k, v in value.items()}
    return value


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    ap.add_argument("--steps", default="comment,body,batch:5,add,del")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--work", type=Path, default=None, help="kept when given; a temp dir otherwise")
    ap.add_argument("--out", type=Path, default=None, help="folder for result.json")
    a = ap.parse_args(argv)

    steps = [s.strip() for s in a.steps.split(",") if s.strip()]
    corpus = a.corpus.resolve()
    work = a.work.resolve() if a.work else Path(tempfile.mkdtemp(prefix="modlive-"))
    if a.work and work.exists() and any(work.iterdir()):
        ap.error(f"--work {work} is not empty")
    try:
        body = run_benchmark(a.python, corpus, work, steps, a.runs)
    finally:
        if a.work is None:
            shutil.rmtree(work, ignore_errors=True)

    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True,
                            check=False)
    result = {
        "benchmark": "mod-live edit-to-fresh",
        "date": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "verinoda_commit": commit.stdout.strip() or None,
        "corpus": str(corpus),
        "steps_spec": steps,
        "runs": a.runs,
        "platform": {"os": os.name, "python": sys.version.split()[0], "cpus": os.cpu_count()},
        **body,
    }
    # most specific first: the work dir and the corpus may lie under the checkout or the home dir
    result = sanitize(result, {str(work): "<TMP>", str(corpus): "<CORPUS>", str(ROOT): "<REPO>",
                               str(Path.home()): "<HOME>"})
    print(json.dumps(result["summary"], indent=2))
    if a.out:
        a.out.mkdir(parents=True, exist_ok=True)
        (a.out / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {a.out / 'result.json'}")
    bad = sum(v[m]["errors"] for v in result["summary"].values() for m in ("fast", "full"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
