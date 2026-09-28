"""ContextBench no-model comparison runner (DESIGN.md). Usage:
  python scripts/run.py --ids ID[,ID...]      run given instances (smoke)
  python scripts/run.py --sample              run the pre-registered sample (resumable)
Run with cb/.venv python (3.11) from anywhere."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pyarrow.parquet as pq

CB = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CB / "scripts"))
sys.path.insert(0, str(CB / "ContextBench"))
import bm25  # noqa: E402
import views  # noqa: E402
from contextbench.parsers.gold import _normalize_rel_path  # noqa: E402

ENVS = CB.parent / "agentbench" / "envs"
V = ENVS / "verinoda" / "Scripts" / "verinoda.exe"
G = ENVS / "graphify" / "Scripts" / "graphify.exe"
PARQUET = CB / "data" / "contextbench_verified.parquet"
REPOS = CB / "repos"
OUT = CB / "out"
WORK = CB / "work"
CAP = 6000
ARMS = ("vq", "va", "gq", "bm25")
T_INDEX, T_QUERY, T_FETCH, T_EVAL = 3600, 600, 1200, 1800
HEAVY_BYTES = 60_000_000  # plumbing: at most one instance with a checkout above this in flight (memory)

ROWS = {r["original_inst_id"]: r for r in pq.read_table(PARQUET).to_pylist()}
LOCK = threading.Lock()


def base_env() -> dict:
    env = dict(os.environ)
    env.pop("GRAPHIFY_OUT", None)
    # the runner's own venv launcher leaks these into children; the tools' venvs must not see them
    for k in ("__PYVENV_LAUNCHER__", "PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "PYTHONEXECUTABLE"):
        env.pop(k, None)
    for k in list(env):
        if k.endswith(("_API_KEY", "_AUTH_TOKEN")):
            env.pop(k)
    env.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1", GRAPHIFY_QUERY_LOG_DISABLE="1",
               GIT_CONFIG_COUNT="2", GIT_CONFIG_KEY_0="core.autocrlf", GIT_CONFIG_VALUE_0="false",
               GIT_CONFIG_KEY_1="core.longpaths", GIT_CONFIG_VALUE_1="true", GIT_TERMINAL_PROMPT="0")
    return env


ENV = base_env()


def run(argv, cwd, timeout, env=None):
    t0 = time.perf_counter()
    try:
        r = subprocess.run([str(a) for a in argv], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=env or ENV, timeout=timeout)
        return {"rc": r.returncode, "out": r.stdout or "", "err": r.stderr or "", "s": time.perf_counter() - t0}
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        return {"rc": "timeout", "out": out, "err": "timeout", "s": time.perf_counter() - t0}
    except OSError as e:
        return {"rc": "oserror", "out": "", "err": str(e), "s": time.perf_counter() - t0}


def git(*args, cwd, timeout=T_FETCH):
    return run(["git", *args], cwd, timeout)


def rmtree(p: Path) -> None:
    def onerr(func, path, exc):
        try:
            os.chmod(path, stat.S_IWRITE)
            func(path)
        except OSError:
            pass
    if p.exists():
        shutil.rmtree(p, onerror=onerr)


def log(msg: str) -> None:
    with LOCK:
        print(time.strftime("%H:%M:%S"), msg, flush=True)


def fetch(iid: str, r: dict, root: Path) -> dict:
    src = root / "src"
    info = {"tries": []}
    for attempt in (1, 2):
        rmtree(root)
        src.mkdir(parents=True)
        steps = [git("init", "-q", cwd=src), git("remote", "add", "origin", r["repo_url"], cwd=src),
                 git("fetch", "-q", "--depth", "1", "origin", r["base_commit"], cwd=src)]
        ok = all(s["rc"] == 0 for s in steps)
        co = None
        if ok:
            co = git("-c", "advice.detachedHead=false", "checkout", "-q", "--detach", "FETCH_HEAD", cwd=src)
            ok = co["rc"] == 0
        head = git("rev-parse", "HEAD", cwd=src)["out"].strip() if ok else ""
        info["tries"].append({"attempt": attempt, "fetch_s": round(sum(s["s"] for s in steps), 2),
                              "errors": [s["err"][-400:] for s in steps + ([co] if co else []) if s["rc"] != 0],
                              "head": head})
        if ok and head == r["base_commit"]:
            info["ok"] = True
            return info
    info["ok"] = False
    return info


def worktree(src: Path, dest: Path, commit: str) -> dict:
    return git("worktree", "add", "-q", "--detach", str(dest), commit, cwd=src)


def arm_record(res: dict) -> dict:
    ok = res["rc"] == 0
    return {"text": res["out"] if ok else "", "raw_stdout": res["out"], "rc": res["rc"], "s": round(res["s"], 3),
            "stderr_tail": "\n".join(l for l in res["err"].splitlines() if "skill at" not in l)[-1500:]}


def gold_view(r: dict) -> dict:
    v: dict = {}
    for x in json.loads(r["gold_context"]):
        f = _normalize_rel_path(x.get("file", ""))
        if f:
            v.setdefault(f, set()).update(range(int(x["start_line"]), int(x["end_line"]) + 1))
    return v


def process(iid: str) -> dict:
    r = ROWS[iid]
    root = REPOS / iid
    src, vdir, gdir = root / "src", root / "v", root / "g"
    rec = {"id": iid, "lang": r["language"], "repo_url": r["repo_url"], "commit": r["base_commit"],
           "started": time.strftime("%Y-%m-%dT%H:%M:%S")}
    t_all = time.perf_counter()
    f = fetch(iid, r, root)
    rec["fetch"] = f
    if not f["ok"]:
        rec["status"] = "fetch_failed"
        rmtree(root)
        return rec
    rec["checkout_mb"] = round(sum(p.stat().st_size for p in src.rglob("*") if p.is_file() and ".git" not in p.parts) / 1e6, 1)
    for d in (vdir, gdir):
        w = worktree(src, d, r["base_commit"])
        rec.setdefault("worktree_rc", []).append(w["rc"])
    q = r["problem_statement"]
    arms = {}
    # Verinoda
    sc = run([V, "scan", vdir], vdir, T_INDEX)
    rec["v_index"] = {"rc": sc["rc"], "s": round(sc["s"], 2), "out_tail": sc["out"][-300:], "err_tail": sc["err"][-600:]}
    log(f"{iid}: verinoda scan rc={sc['rc']} {sc['s']:.0f}s")
    arms["vq"] = arm_record(run([V, "query", q, "--repo", vdir, "--max-chars", "6000"], vdir, T_QUERY))
    arms["va"] = arm_record(run([V, "analyze", q, "--repo", vdir], vdir, T_QUERY))
    # Graphify
    gu = run([G, "update", "."], gdir, T_INDEX)
    rec["g_index"] = {"rc": gu["rc"], "s": round(gu["s"], 2),
                      "out_tail": "\n".join(l for l in gu["out"].splitlines() if "Rebuilt" in l or "error" in l.lower())[-400:],
                      "err_tail": "\n".join(l for l in gu["err"].splitlines() if "skill at" not in l)[-600:]}
    log(f"{iid}: graphify update rc={gu['rc']} {gu['s']:.0f}s")
    arms["gq"] = arm_record(run([G, "query", q, "--budget", "2000"], gdir, T_QUERY))
    # BM25
    try:
        ix = bm25.build(src)
        text, meta = bm25.query(ix, q)
        arms["bm25"] = {"text": text, "raw_stdout": text, "rc": 0, "s": round(meta["query_s"], 3), "stderr_tail": "",
                        "meta": meta}
        rec["bm25_index"] = {"s": round(ix["index_s"], 2), "files": ix["n"]}
        del ix
    except Exception:
        arms["bm25"] = {"text": "", "raw_stdout": "", "rc": "exception", "s": 0.0,
                        "stderr_tail": traceback.format_exc()[-1500:]}
    # predictions
    preds, labels = [], []
    rel_src = f"../repos/{iid}/src"  # relative to the evaluator cwd: keeps its worktree path short (git: $GIT_DIR too big)
    for arm in ARMS:
        full = arms[arm]["text"]
        for cap, text in (("capped", full[:CAP]), ("uncapped", full)):
            vv = views.views(arm, text)
            for view in ("shown", "cited"):
                preds.append({"instance_id": iid, "repo_url": rel_src, "commit": r["base_commit"],
                              "traj_data": views.to_traj(vv[view])})
                labels.append({"arm": arm, "view": view, "cap": cap, "chars": len(text)})
    preds.append({"instance_id": iid, "repo_url": rel_src, "commit": r["base_commit"],
                  "traj_data": views.to_traj(gold_view(r))})
    labels.append({"arm": "gold", "view": "gold", "cap": "-", "chars": 0})
    for d in ("raw", "preds", "eval"):
        (OUT / d).mkdir(parents=True, exist_ok=True)
    (OUT / "raw" / f"{iid}.json").write_text(json.dumps({"id": iid, "question": q, "arms": arms, "record": rec},
                                                        ensure_ascii=False, indent=1), encoding="utf-8")
    pfile = OUT / "preds" / f"{iid}.jsonl"
    pfile.write_text("".join(json.dumps({**p, "_label": l}) + "\n" for p, l in zip(preds, labels)), encoding="utf-8")
    # official evaluator
    tmp_root = Path(os.environ["TEMP"]) / "cbe" / hashlib.sha1(iid.encode()).hexdigest()[:8]
    env = dict(ENV, CONTEXTBENCH_TMP_ROOT=str(tmp_root))
    efile = OUT / "eval" / f"{iid}.jsonl"
    ev = run([sys.executable, "-m", "contextbench.evaluate", "--gold", PARQUET, "--pred", pfile,
              "--cache", WORK / "evalcache", "--out", efile], CB / "ContextBench", T_EVAL, env=env)
    (OUT / "eval" / f"{iid}.stderr.txt").write_text(ev["err"], encoding="utf-8")
    rec["eval"] = {"rc": ev["rc"], "s": round(ev["s"], 1)}
    rows = [json.loads(l) for l in efile.read_text(encoding="utf-8").splitlines() if l.strip()] if efile.exists() else []
    rec["eval"]["rows"] = len(rows)
    if len(rows) != len(labels):
        rec["status"] = "eval_mismatch"
    else:
        rec["status"] = "ok"
        (OUT / "eval" / f"{iid}.labelled.jsonl").write_text(
            "".join(json.dumps({**l, **row}) + "\n" for l, row in zip(labels, rows)), encoding="utf-8")
    rec["total_s"] = round(time.perf_counter() - t_all, 1)
    rec["arms"] = {a: {"rc": arms[a]["rc"], "s": arms[a]["s"], "chars": len(arms[a]["text"])} for a in ARMS}
    # cleanup: evaluator worktree, then the instance's checkout
    git("worktree", "prune", cwd=src)
    rmtree(tmp_root)
    rmtree(root)
    return rec


class Sched:
    """Hands out instances: at most one heavy (checkout above HEAVY_BYTES) in flight; failed fetches get replaced."""

    def __init__(self, todo, queue, sizes, max_heavy=1):
        self.todo, self.queue, self.sizes = list(todo), queue, sizes
        self.heavy_in_flight, self.max_heavy = 0, max_heavy
        self.cv = threading.Condition()

    def heavy(self, iid):
        return (self.sizes.get(iid) or 0) > HEAVY_BYTES

    def next(self):
        with self.cv:
            while True:
                if not self.todo:
                    return None
                for i, iid in enumerate(self.todo):
                    if not self.heavy(iid) or self.heavy_in_flight < self.max_heavy:
                        self.todo.pop(i)
                        if self.heavy(iid):
                            self.heavy_in_flight += 1
                        return iid
                self.cv.wait(10)

    def done(self, iid, rec):
        with self.cv:
            if self.heavy(iid):
                self.heavy_in_flight -= 1
            with (OUT / "runs.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec) + "\n")
            if rec.get("status") == "fetch_failed":
                lang = rec["lang"]
                nxt = self.queue[lang].pop(0) if self.queue.get(lang) else None
                with (OUT / "replacements.jsonl").open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"failed": iid, "replacement": nxt, "lang": lang}) + "\n")
                if nxt:
                    self.todo.insert(0, nxt)
            self.cv.notify_all()


def worker(sched: Sched) -> None:
    while True:
        iid = sched.next()
        if iid is None:
            return
        lang = ROWS[iid]["language"]
        log(f"{iid}: start ({lang})")
        try:
            rec = process(iid)
        except Exception:
            rec = {"id": iid, "lang": lang, "status": "crash", "trace": traceback.format_exc()[-3000:]}
            rmtree(REPOS / iid)
        log(f"{iid}: {rec.get('status')} in {rec.get('total_s')}s")
        sched.done(iid, rec)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids")
    ap.add_argument("--sample", action="store_true")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--heavy", type=int, default=1)
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    s = json.loads((CB / "sample.json").read_text(encoding="utf-8"))
    sizes = {k: v.get("bytes") for k, v in s["sizes"].items()}
    queue = {k: list(v) for k, v in s["queue"].items()}
    if a.ids:
        ids = a.ids.split(",")
    else:
        ids = [x["id"] for x in s["sample"]]
    done = set()
    if (OUT / "runs.jsonl").exists():
        for l in (OUT / "runs.jsonl").read_text(encoding="utf-8").splitlines():
            d = json.loads(l)
            if d.get("status") in ("ok", "fetch_failed"):
                done.add(d["id"])
    todo = [i for i in ids if i not in done]
    # replacements already made for failed fetches are resumed too
    if (OUT / "replacements.jsonl").exists():
        for l in (OUT / "replacements.jsonl").read_text(encoding="utf-8").splitlines():
            d = json.loads(l)
            if d.get("replacement"):
                q = queue.get(d["lang"], [])
                if d["replacement"] in q:
                    q.remove(d["replacement"])
                if d["replacement"] not in done and d["replacement"] not in todo:
                    todo.append(d["replacement"])
    todo.sort(key=lambda i: -(sizes.get(i) or 0))  # large first, so the tail is short
    log(f"{len(todo)} to run ({len(done)} done)")
    sched = Sched(todo, queue, sizes, a.heavy)
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        for _ in range(a.workers):
            ex.submit(worker, sched)
    log("all done")


if __name__ == "__main__":
    main()
