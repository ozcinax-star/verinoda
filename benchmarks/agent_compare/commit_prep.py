"""Working copies for a study whose tasks each sit at their own commit (DESIGN_SEL4.md): per task a repository at the
task's base commit with git history ending there (`history_depth` 1: a one-commit repository, the fix is not
reachable from it; N: git's `--depth N`, the commits within N parent steps of the base, so exactly N on a linear
history and MORE than N where merges fork it; never a commit after the base), and from it the arms' copies. Each KIND
of arm is built once per task and the other arms of that kind are copies of the finished one (so two Verinoda arms
share no analysis records but start identical). Resumable per task and per stage; a task that fails is reported and
the others go on.

    python benchmarks/agent_compare/commit_prep.py CONFIG.json

CONFIG: {"tasks": tasks.json, "work": folder, "workers": n,
  "clone": the full local clone (or any git URL) the base commits are fetched from; a task's own "remote" overrides it,
  "history_depth": 1 (default; N runs `git fetch --depth N <source> <base_sha>`; the result records the commit count),
  "arms": {name: {"kind": "none" | "graphify" | "verinoda"}} (default: none, graphify, verinoda_mod and verinoda_setup,
      the last two of kind verinoda; the arms of one kind are built in the order given, the first is the build),
  "only": [task ids] (build just these), "graphify": graphify.exe, "verinoda_dir": folder with verinoda.exe,
  "retries": 3, "retry_pause": 10 (seconds, network fetches only), "keep_base": true,
  "timeouts": {"fetch": 1800, "checkout": 600, "graphify": 3600, "verinoda": 3600, "git": 300} (seconds per step),
  "git_config": {"core.longpaths": "true"} (git -c values for every git step, e.g. "core.autocrlf": "false")}

Per task `<work>/<id>/done.json` is the result; `<work>/<id>/stage-*.json` mark finished stages, so an interrupted run
resumes at the first unfinished stage and a done task is skipped. Every arm copy is checked at the end: HEAD is the
base commit and `git rev-list --all` holds exactly the base commit's ancestors (the counts are compared too), so no
commit after the base is reachable.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

KINDS = ("none", "graphify", "verinoda")
DEFAULT_ARMS = {"none": {"kind": "none"}, "graphify": {"kind": "graphify"}, "verinoda_mod": {"kind": "verinoda"},
                "verinoda_setup": {"kind": "verinoda"}}
DEFAULT_TIMEOUTS = {"fetch": 1800, "checkout": 600, "graphify": 3600, "verinoda": 3600, "git": 300}


def env(cfg: dict) -> dict:
    e = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    if cfg.get("verinoda_dir"):
        e["PATH"] = cfg["verinoda_dir"] + os.pathsep + e.get("PATH", "")
    e["GRAPHIFY_NO_AUTO_REFRESH"] = "1"
    e["PYTHONIOENCODING"] = "utf-8"
    e["GIT_TERMINAL_PROMPT"] = "0"
    conf = {"core.longpaths": "true", **cfg.get("git_config", {})}
    e["GIT_CONFIG_COUNT"] = str(len(conf))
    for i, (k, v) in enumerate(conf.items()):
        e[f"GIT_CONFIG_KEY_{i}"] = k
        e[f"GIT_CONFIG_VALUE_{i}"] = str(v)
    return e


def timeout_of(cfg: dict, step: str) -> int:
    return {**DEFAULT_TIMEOUTS, **cfg.get("timeouts", {})}[step]


def run(argv: list[str], cwd: Path | None, cfg: dict, timeout: int = 3600) -> tuple[int, str, float]:
    """(return code, stdout+stderr, seconds); a step that outlives its timeout is killed and reports code 124."""
    t0 = time.perf_counter()
    try:
        p = subprocess.run(argv, cwd=cwd, env=env(cfg), capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, check=False)
        rc, out = p.returncode, p.stdout + p.stderr
    except subprocess.TimeoutExpired as exc:
        partial = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        rc, out = 124, f"timeout after {timeout}s\n{partial}"
    except OSError as exc:
        rc, out = 127, f"cannot run {argv[0]}: {exc}"
    return rc, out, round(time.perf_counter() - t0, 1)


def with_retries(step: Callable[[], tuple[int, str, float]], tries: int, pause: float) -> tuple[int, str, float]:
    """Run a network step up to ``tries`` times, ``pause`` seconds apart; the last result is returned."""
    rc, out, secs = 1, "", 0.0
    for attempt in range(max(1, tries)):
        rc, out, secs = step()
        if rc == 0:
            break
        if attempt + 1 < tries:
            time.sleep(pause)
    return rc, out, secs


def rmtree(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path, onerror=lambda f, p, _e: (os.chmod(p, 0o700), f(p)))


def source_of(cfg: dict, task: dict) -> str:
    src = task.get("remote") or cfg.get("clone")
    if not src:
        raise ValueError(f"task {task['id']}: no \"remote\" and the config has no \"clone\"")
    return src


def history_repo(cfg: dict, task: dict, dest: Path) -> dict:
    """The repository at the base commit with up to ``history_depth`` commits ending there and nothing else."""
    rmtree(dest)
    dest.mkdir(parents=True)
    depth = int(cfg.get("history_depth", 1))
    if depth < 1:
        raise ValueError("history_depth must be at least 1")
    sha = task["base_sha"]
    rc, out, _ = run(["git", "init", "-q"], dest, cfg, timeout_of(cfg, "git"))
    assert rc == 0, ("git init", out[-300:])
    fetch = ["git", "fetch", "-q", "--depth", str(depth), source_of(cfg, task), sha]
    rc, out, secs = with_retries(lambda: run(fetch, dest, cfg, timeout_of(cfg, "fetch")), cfg.get("retries", 3),
                                 cfg.get("retry_pause", 10))
    assert rc == 0, ("git fetch", out[-300:])
    rc, out, _ = run(["git", "-c", "advice.detachedHead=false", "checkout", "-q", "--detach", "FETCH_HEAD"], dest, cfg,
                     timeout_of(cfg, "checkout"))
    assert rc == 0, ("git checkout", out[-300:])
    return {"fetch_seconds": secs, **check_history(cfg, dest, sha)}


def check_history(cfg: dict, repo: Path, sha: str) -> dict:
    """HEAD is ``sha`` and every commit any ref (or HEAD) reaches is an ancestor of it: nothing after the base."""
    t = timeout_of(cfg, "git")
    head = run(["git", "rev-parse", "HEAD"], repo, cfg, t)[1].strip()
    everything = run(["git", "rev-list", "--all"], repo, cfg, t)[1].split()
    ancestors = run(["git", "rev-list", sha], repo, cfg, t)[1].split()
    count = run(["git", "rev-list", "--count", sha], repo, cfg, t)[1].strip()
    ok = head == sha and len(everything) == int(count or -1) and set(everything) == set(ancestors) and len(ancestors) > 0
    return {"head_is_base": head == sha, "history_commits": len(ancestors), "reachable_commits": len(everything),
            "nothing_after_base": ok}


def copy_tree(src: Path, dest: Path) -> None:
    rmtree(dest)
    shutil.copytree(src, dest)


def build_graphify(cfg: dict, arm: Path) -> dict:
    rc, out, secs = run([cfg["graphify"], "update", "."], arm, cfg, timeout_of(cfg, "graphify"))
    m = re.search(r"Rebuilt: (\d+) nodes, (\d+) edges", out)
    rc2, _, _ = run([cfg["graphify"], "claude", "install"], arm, cfg, timeout_of(cfg, "graphify"))
    return {"rc": rc, "seconds": secs, "nodes": int(m.group(1)) if m else None, "edges": int(m.group(2)) if m else None,
            "install_rc": rc2, "ok": bool(rc == 0 and m and int(m.group(1)) and rc2 == 0)}


def build_verinoda(cfg: dict, arm: Path) -> dict:
    exe = shutil.which("verinoda", path=cfg.get("verinoda_dir") or None) or "verinoda"
    rc, out, secs = run([exe, "setup", ".", "--agents", "claude"], arm, cfg, timeout_of(cfg, "verinoda"))
    m = re.search(r"scan - (\d+) files, (\d+) nodes, (\d+) edges", out)
    return {"rc": rc, "seconds": secs, "files": int(m.group(1)) if m else None, "nodes": int(m.group(2)) if m else None,
            "edges": int(m.group(3)) if m else None, "ok": bool(rc == 0 and m and int(m.group(2)))}


BUILDERS = {"graphify": build_graphify, "verinoda": build_verinoda}


def arms_of(cfg: dict) -> dict[str, str]:
    """arm name -> kind, in the order given."""
    arms = cfg.get("arms") or DEFAULT_ARMS
    out = {}
    for name, spec in arms.items():
        kind = spec["kind"] if isinstance(spec, dict) else spec
        if kind not in KINDS:
            raise ValueError(f"arm {name}: kind {kind!r} is not one of {KINDS}")
        out[name] = kind
    return out


def stage(root: Path, name: str) -> dict | None:
    f = root / f"stage-{name}.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None


def mark(root: Path, name: str, info: dict) -> None:
    (root / f"stage-{name}.json").write_bytes((json.dumps(info, indent=1) + "\n").encode("utf-8"))


def prep_task(cfg: dict, task: dict) -> dict:
    root = Path(cfg["work"]) / task["id"]
    arms = arms_of(cfg)
    done = root / "done.json"
    if done.exists():
        old = json.loads(done.read_text(encoding="utf-8"))
        # a done.json from before "arms" and "history_depth" existed describes the old four arms at depth 1
        old_arms = old.get("arms") or {n: s["kind"] for n, s in DEFAULT_ARMS.items()}
        if old.get("ok") and old_arms == arms and old.get("history_depth", 1) == int(cfg.get("history_depth", 1)):
            return old
    base = root / "base"
    info: dict = {"id": task["id"], "base_sha": task["base_sha"], "history_depth": int(cfg.get("history_depth", 1)),
                  "arms": arms}
    hist = stage(root, "base")
    if hist is None or not base.exists():  # nothing built on an unfinished base survives
        rmtree(root)
        root.mkdir(parents=True)
        hist = history_repo(cfg, task, base)
        mark(root, "base", hist)
    info["history"] = hist
    first_of_kind: dict[str, str] = {}
    for name, kind in arms.items():
        first_of_kind.setdefault(kind, name)
    builds: dict[str, dict] = {}
    for name, kind in arms.items():
        copy_done = stage(root, f"arm-{name}")
        if copy_done is not None and (root / name).exists():
            if first_of_kind[kind] == name and kind != "none":
                builds[kind] = copy_done
            continue
        if first_of_kind[kind] == name:  # the one build of this kind
            copy_tree(base, root / name)
            result = BUILDERS[kind](cfg, root / name) if kind != "none" else {"ok": True}
            if kind != "none":
                builds[kind] = result
            if not result["ok"]:
                info["failed"] = f"{kind} build failed in {name}"
                break  # not marked: a rerun builds it again
            mark(root, f"arm-{name}", result)
        else:
            src = first_of_kind[kind]
            if stage(root, f"arm-{src}") is None:
                break
            copy_tree(root / src, root / name)
            mark(root, f"arm-{name}", {"ok": True, "copy_of": src})
    for kind, result in builds.items():
        info[kind] = {k: v for k, v in result.items() if k != "ok"}
    checks = {}
    for name in arms:
        if (root / name).exists() and stage(root, f"arm-{name}") is not None:
            checks[name] = check_history(cfg, root / name, task["base_sha"])
    info["arm_checks"] = checks
    info["ok"] = bool(len(checks) == len(arms) and "failed" not in info and hist["nothing_after_base"]
                      and all(c["nothing_after_base"] for c in checks.values()))
    if not cfg.get("keep_base", True) and info["ok"]:
        rmtree(base)
    done.write_bytes((json.dumps(info, indent=1) + "\n").encode("utf-8"))
    return info


def main() -> int:
    cfg = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    tasks = json.loads(Path(cfg["tasks"]).read_text(encoding="utf-8"))["tasks"]
    if cfg.get("only"):
        wanted = set(cfg["only"])
        tasks = [t for t in tasks if t["id"] in wanted]
        missing = wanted - {t["id"] for t in tasks}
        if missing:
            print("not in tasks.json:", sorted(missing), file=sys.stderr)
    Path(cfg["work"]).mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(cfg.get("workers", 4)) as ex:
        futures = [(t, ex.submit(prep_task, cfg, t)) for t in tasks]
        report = []
        for t, f in futures:
            try:
                r = f.result()
            except Exception as exc:  # noqa: BLE001 - one task failing must not stop the others
                r = {"id": t["id"], "ok": False, "error": repr(exc)[:300]}
            report.append(r)
            print(time.strftime("%H:%M:%S"), t["id"], "ok" if r.get("ok") else f"FAILED {r}", flush=True)
    (Path(cfg["work"]) / "prep.json").write_bytes((json.dumps(report, indent=1) + "\n").encode("utf-8"))
    print(sum(bool(r.get("ok")) for r in report), "of", len(report), "tasks prepared")
    return 0


if __name__ == "__main__":
    sys.exit(main())
