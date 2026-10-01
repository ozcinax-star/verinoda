"""Does ``verinoda update`` after an edit give the graph a fresh scan of the edited tree gives?

    python -P tools/update_vs_scan.py --corpus DIR --edit REL::APPEND_TEXT [--edit ...] [--work DIR] [--keep]

Copy A: init, scan, apply the edits, update. Copy B: a fresh copy of the edited tree (no .verinoda), init, scan.
Both run this checkout's code (``python -P -m verinoda`` with PYTHONPATH set here, so a corpus that holds its own
``verinoda/`` folder is not imported). Prints the times and the graph comparison (``tools/graph_equal.py``).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "tools"))
import graph_equal  # noqa: E402


def run(cwd: Path, *args: str) -> tuple[float, dict | None]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("VERINODA_")}
    env.update(PYTHONPATH=str(HERE), GRAPHIFY_OUT=".verinoda/index", PYTHONSAFEPATH="1")
    t = time.perf_counter()
    p = subprocess.run([sys.executable, "-P", "-m", "verinoda", *args, "--json"], cwd=cwd, env=env,
                       capture_output=True, text=True, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL)
    dt = time.perf_counter() - t
    if p.returncode not in (0, 1, 3):
        raise SystemExit(f"{' '.join(args)} failed ({p.returncode}): {p.stderr[-1500:]}")
    try:
        return dt, json.loads(p.stdout)
    except ValueError:
        return dt, None


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false",
                    "-c", "core.longpaths=true", *args], cwd=cwd, check=True, capture_output=True,
                   stdin=subprocess.DEVNULL)


def copy_tree(src: Path, dst: Path) -> None:
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns(".verinoda", ".git", "__pycache__", "*.pyc"))
    git(dst, "init", "-q")
    git(dst, "add", "-A")
    git(dst, "commit", "-q", "-m", "corpus")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--edit", action="append", default=[], help="REL::TEXT appended to the file (\\n for newlines)")
    ap.add_argument("--work")
    ap.add_argument("--keep", action="store_true")
    ns = ap.parse_args()
    work = Path(ns.work or tempfile.mkdtemp(prefix="uvs"))
    a, b = work / "a", work / "b"
    copy_tree(Path(ns.corpus), a)
    t_init, _ = run(a, "init", ".")
    t_scan, _ = run(a, "scan", ".")
    for e in ns.edit:
        rel, _, text = e.partition("::")
        p = a / rel
        p.write_bytes(p.read_bytes() + text.replace("\\n", "\n").encode("utf-8"))
    t_upd, upd = run(a, "update", ".")
    shutil.copytree(a, b, ignore=shutil.ignore_patterns(".verinoda", ".git", "__pycache__", "*.pyc"))
    git(b, "init", "-q")
    git(b, "add", "-A")
    git(b, "commit", "-q", "-m", "fresh")
    run(b, "init", ".")
    t_fresh, _ = run(b, "scan", ".")
    res = graph_equal.compare(graph_equal._load(a / ".verinoda/index/graph.json"),
                              graph_equal._load(b / ".verinoda/index/graph.json"))
    out = {"scan_s": round(t_scan, 1), "update_s": round(t_upd, 1), "fresh_scan_s": round(t_fresh, 1),
           "index_mode": (upd or {}).get("index_mode"), "equal": res["equal"], "counts": res["counts"],
           "nodes": res["nodes"], "edges": res["edges"],
           "sample": {k: res[k][:3] for k in ("nodes_only_in_a", "nodes_only_in_b", "edges_only_in_a",
                                               "edges_only_in_b")},
           "changed_sample": res["nodes_changed"][:2], "work": str(work)}
    print(json.dumps(out, indent=1, ensure_ascii=False)[:6000])
    if not ns.keep and res["equal"]:
        shutil.rmtree(work, ignore_errors=True)
    return 0 if res["equal"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
