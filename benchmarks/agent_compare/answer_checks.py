"""Do the files an answer names exist? A model asked for the files of a fix may name one that is not in the repository.

For every session: each file the answer names (the first five, as scored) against the files git tracks in the task's
working copy (the same tree in every arm). Per arm: files named, files that do not exist, and the sessions with at least
one. The paths' existence is all that is checked: not the lines, not the reasons.

    python benchmarks/agent_compare/answer_checks.py CONFIG.json RESULTS.jsonl[,RESULTS2.jsonl ...] OUT.json
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from big_run import copy_of


def tracked(copy: Path) -> set[str]:
    out = subprocess.run(["git", "-C", str(copy), "ls-files", "-z"], capture_output=True, check=False).stdout
    return {p for p in out.decode("utf-8", "replace").split("\0") if p}


def check(cfg: dict, rows: list[dict]) -> dict[str, dict]:
    trees: dict[str, set[str]] = {}
    arms: dict[str, dict] = {}
    for r in rows:
        key = str(copy_of(cfg, {"id": r["id"]}, "none"))
        if key not in trees:
            trees[key] = tracked(Path(key))
        files = r.get("files") or []
        missing = [f for f in files if f not in trees[key]]
        a = arms.setdefault(r["arm"], {"sessions": 0, "named": 0, "missing": 0, "sessions_with_a_missing": 0})
        a["sessions"] += 1
        a["named"] += len(files)
        a["missing"] += len(missing)
        a["sessions_with_a_missing"] += bool(missing)
    for a in arms.values():
        a["share_missing"] = round(a["missing"] / a["named"], 3) if a["named"] else 0.0
    return {k: {f: v[f] for f in ("sessions", "named", "missing", "share_missing", "sessions_with_a_missing")} for k, v in arms.items()}


def main() -> int:
    cfg = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    rows = [json.loads(x) for p in sys.argv[2].split(",") for x in Path(p).read_text(encoding="utf-8").splitlines() if x.strip()]
    out = check(cfg, rows)
    Path(sys.argv[3]).write_bytes((json.dumps(out, indent=1) + "\n").encode("utf-8"))
    for arm, v in out.items():
        print(f"{arm:15s} named {v['named']:5d} missing {v['missing']:4d} ({100 * v['share_missing']:.1f} %) in {v['sessions_with_a_missing']} of {v['sessions']} sessions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
