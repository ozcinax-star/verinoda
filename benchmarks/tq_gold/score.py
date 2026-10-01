"""Scores typed-question answers against the frozen gold set (build.py; the files are not changed here).

``python benchmarks/tq_gold/score.py REPOS_DIR [dev|held_out] [--no-verify]``: REPOS_DIR holds scanned copies
named ``qlang`` and ``orders_app``. Prints, per type and status, how many answers were decided and right, and
lists every wrong one. An unknown answer is never wrong (it is counted apart); a count is right when it is a
lower bound of the gold count; ``which`` is right when the files are the gold files.
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load(split: str) -> list[dict]:
    """The gold cases of a split, after checking the frozen hash."""
    manifest = json.loads((HERE / "MANIFEST.json").read_text(encoding="utf-8"))
    name = f"{split}.json"
    data = (HERE / name).read_bytes()
    if hashlib.sha256(data).hexdigest() != manifest["files"][name]:
        raise SystemExit(f"{name} does not match MANIFEST.json: the gold set was changed after it was frozen")
    return json.loads(data)


def _near(cited: str, gold: str) -> bool:
    cf, _, cl = cited.rpartition(":")
    gf, _, gl = gold.rpartition(":")
    return cf == gf and cl.isdigit() and gl.isdigit() and 0 <= int(cl) - int(gl) <= 2


def verdict(ans: dict, gold: dict) -> str:
    """right, wrong or unknown (``?``, invalid)."""
    a, g = ans.get("answer"), gold["answer"]
    if a is None or ans.get("status") == "invalid":
        return "unknown"
    if g is None:
        return "wrong"   # no true answer exists, and one was given
    if isinstance(g, bool):
        return "right" if a is g else "wrong"
    if isinstance(g, int):
        return "right" if isinstance(a, int) and not isinstance(a, bool) and a <= g else "wrong"
    return "right" if sorted(a) == sorted(g) else "wrong"


def located(ans: dict, gold: dict) -> bool | None:
    """Does a right true answer cite every gold line? None when there is nothing to cite."""
    if not gold["at"] or ans.get("answer") in (None, False):
        return None
    cited = ans.get("at") or []
    return all(any(_near(c, g) for c in cited) for g in gold["at"])


def run(repos: Path, split: str, *, verify: bool = True) -> dict:
    from verinoda import tq

    cases = load(split)
    by_repo: dict[str, list[dict]] = defaultdict(list)
    for c in cases:
        by_repo[c["repo"]].append(c)
    rows = []
    for repo, cs in sorted(by_repo.items()):
        for k in range(0, len(cs), tq.MAX_QUESTIONS):
            part = cs[k:k + tq.MAX_QUESTIONS]
            res = tq.ask(repos / repo, [c["question"] for c in part], verify=verify)
            for c, a in zip(part, res["answers"]):
                rows.append({"id": c["id"], "type": c["question"].split()[0], "question": c["question"],
                             "gold": c["answer"], "answer": a.get("answer"), "status": a["status"],
                             "verdict": verdict(a, c), "located": located(a, c), "next": a.get("next")})
    cells: dict[str, dict] = defaultdict(lambda: {"n": 0, "right": 0, "wrong": 0, "unknown": 0})
    for r in rows:
        for key in (f"{r['type']}/{r['status']}", f"all/{r['status']}", "all/all"):
            cells[key]["n"] += 1
            cells[key][r["verdict"]] += 1
    loc = [r["located"] for r in rows if r["located"] is not None and r["verdict"] == "right"]
    unknown = [r for r in rows if r["verdict"] == "unknown"]
    return {"split": split, "verify": verify, "n": len(rows), "cells": dict(sorted(cells.items())),
            "wrong": [r for r in rows if r["verdict"] == "wrong"],
            "wrong_verified": [r for r in rows if r["verdict"] == "wrong"
                               and r["status"] in ("statically_verified", "observed")],
            "unknown_without_next": [r["id"] for r in unknown if not r["next"] and r["status"] != "invalid"],
            "located": f"{sum(loc)}/{len(loc)}", "unknown": len(unknown)}


if __name__ == "__main__":
    out = run(Path(sys.argv[1]), sys.argv[2] if len(sys.argv) > 2 else "dev", verify="--no-verify" not in sys.argv)
    print(json.dumps(out, indent=1))
