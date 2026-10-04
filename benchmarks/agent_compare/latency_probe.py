"""How long `verinoda locate` and `verinoda coupled` take, per repository, on a machine with nothing else running: the
daemon's start and its graph load, then the command as the mod runs it (wall time of the process, which asks the daemon), the
command computing for itself (`--no-daemon`: what a repository with no daemon pays), and `coupled`.

    python benchmarks/agent_compare/latency_probe.py CONFIG.json OUT.json

CONFIG: {"verinoda": the executable, "sets": [{"name", "tasks": tasks.json, "repo": "<folder with {task}>", "n": tasks to probe,
"repeat": asks per task}]}
"""

from __future__ import annotations

import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def summarize(samples: list[float]) -> dict:
    s = sorted(samples)
    if not s:
        return {"n": 0}
    return {"n": len(s), "median": round(statistics.median(s), 2), "max": round(s[-1], 2), "min": round(s[0], 2)}


def timed(argv: list[str]) -> tuple[float, int]:
    t0 = time.monotonic()
    p = subprocess.run(argv, capture_output=True, check=False)
    return time.monotonic() - t0, p.returncode


def daemon(exe: str, repo: str, what: str) -> dict:
    p = subprocess.run([exe, "locate", "--daemon", what, "--repo", repo, "--json"], capture_output=True, text=True, encoding="utf-8", check=False)
    try:
        return json.loads(p.stdout)
    except ValueError:
        return {"running": False}


def probe_task(exe: str, repo: str, task: dict, repeat: int) -> dict:
    text = f"{task['title']}\n{task['body']}"[:3000]
    daemon(exe, repo, "stop")
    t0 = time.monotonic()
    started = daemon(exe, repo, "start")
    listens = time.monotonic() - t0
    loaded = None
    while started.get("running") and time.monotonic() - t0 < 900:
        if daemon(exe, repo, "status").get("graph_loaded"):
            loaded = time.monotonic() - t0
            break
        time.sleep(1.0)
    first, _ = timed([exe, "locate", "--repo", repo, "--json", "--", text])
    asks = [timed([exe, "locate", "--repo", repo, "--json", "--", text])[0] for _ in range(repeat)]
    coupled = [timed([exe, "coupled", "--repo", repo, "--json", *task["gold"][:2]])[0] for _ in range(repeat)]
    daemon(exe, repo, "stop")
    alone, _ = timed([exe, "locate", "--repo", repo, "--json", "--no-daemon", "--", text])
    return {"id": task["id"], "daemon_listens": round(listens, 2), "graph_loaded_after": None if loaded is None else round(loaded, 2),
            "locate_first": round(first, 2), "locate_asks": [round(x, 2) for x in asks], "coupled_asks": [round(x, 2) for x in coupled],
            "locate_without_daemon": round(alone, 2)}


def main() -> int:
    cfg = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    out = []
    for st in cfg["sets"]:
        tasks = json.loads(Path(st["tasks"]).read_text(encoding="utf-8"))["tasks"][: st.get("n", 5)]
        rows = [probe_task(cfg["verinoda"], st["repo"].format(task=t["id"]), t, st.get("repeat", 3)) for t in tasks]
        out.append({"name": st["name"], "rows": rows, "summary": {
            "daemon_listens": summarize([r["daemon_listens"] for r in rows]),
            "graph_loaded_after": summarize([r["graph_loaded_after"] for r in rows if r["graph_loaded_after"] is not None]),
            "locate_asks": summarize([x for r in rows for x in r["locate_asks"]]),
            "coupled_asks": summarize([x for r in rows for x in r["coupled_asks"]]),
            "locate_without_daemon": summarize([r["locate_without_daemon"] for r in rows])}})
        print(st["name"], json.dumps(out[-1]["summary"]))
    Path(sys.argv[2]).write_bytes((json.dumps(out, indent=1) + "\n").encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
