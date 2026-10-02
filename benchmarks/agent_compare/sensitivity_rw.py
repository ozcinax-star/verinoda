"""Sensitivity checks of the real-world agent comparison that were NOT pre-registered, applied to every arm alike:
aliases an answer declares (``G = path/to/File.java`` then ``G:42``) resolved before scoring, and wider line windows.
The pre-registered result is score_rw.py's; this only shows how much the scoring rule moves it.

    python benchmarks/agent_compare/sensitivity_rw.py RESULTS_DIR
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import score_rw

DECLARED = re.compile(r"`?\b([A-Z][A-Za-z]{0,3})`?\s*(?:=|:=|->|means|is)\s*`?([\w./-]+/[\w.-]+\.\w+)`?")


def resolve_aliases(answer: str) -> str:
    for alias, path in dict(DECLARED.findall(answer)).items():
        answer = re.sub(rf"(?<![\w/]){re.escape(alias)}:(\d)", lambda m, p=path: f"{p}:{m.group(1)}", answer)
    return answer


def run(sessions: list[dict], questions: list[dict], use: dict, window: int) -> dict:
    saved, score_rw.WINDOW = score_rw.WINDOW, window
    try:
        res = score_rw.score(sessions, questions)
    finally:
        score_rw.WINDOW = saved
    arms = [a for a in res["overall"]["arms"] if a != "none"]
    return {"found": {a: v["found"] for a, v in res["overall"]["arms"].items()},
            "diff": {a: {k: score_rw.paired_against_none(res["per_question"], use, a)[k]
                         for k in ("found_diff", "found_diff_ci95")} for a in arms}}


def main() -> int:
    d = Path(sys.argv[1])
    sessions = json.loads((d / "sessions.json").read_text(encoding="utf-8"))
    questions = json.loads((d / "questions.json").read_text(encoding="utf-8"))
    use = json.loads((d / "usage.json").read_text(encoding="utf-8"))
    aliased = [{**s, "answer": resolve_aliases(s.get("answer", ""))} for s in sessions]
    out = {"declared_aliases": sum(a["answer"] != s.get("answer", "") for a, s in zip(aliased, sessions)),
           "registered": run(sessions, questions, use, score_rw.WINDOW),
           "aliases_resolved": run(aliased, questions, use, score_rw.WINDOW),
           **{f"window_{w}": run(sessions, questions, use, w) for w in (10, 20)}}
    (d / "sensitivity.json").write_bytes((json.dumps(out, indent=1) + "\n").encode("utf-8"))
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
