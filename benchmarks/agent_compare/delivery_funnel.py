"""From the answer to the cause: of the gold files, how many did the mod put in front of the model, and how many of those
did the agent then name? A feature can only help through the first step and the agent's use of it is the second.

Per arm, over every session and every gold file of its task (each gold file of each session counts once):
`shown` (it was in what the mod delivered: the `locate` text with the prompt, a refused search, the tool's result, or a
note about a file read), `named` (it is in the answer's first five files), and the four combinations.

    python benchmarks/agent_compare/delivery_funnel.py TASKS.json RESULTS.jsonl[,RESULTS2.jsonl ...] OUT.json
"""

from __future__ import annotations

import glob
import json
import os
import re
import sys
from pathlib import Path

LOCATE_LINE = re.compile(r"^\s{2}(\S+?)(?::\d+-\d+)?(?:\s|$)")
COUPLED_LINE = re.compile(r"^- (\S+?):")


def shown_paths(transcript: Path) -> set[str]:
    """The files the mod's texts in a session's transcript list."""
    out: set[str] = set()
    for line in transcript.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        texts: list[str] = []
        att = d.get("attachment") or {}
        if att.get("type") == "hook_additional_context":
            texts += [str(c) for c in att.get("content") or []]
        content = (d.get("message") or {}).get("content")
        if isinstance(content, list):
            for b in content:
                if b.get("type") == "tool_result":
                    c = b.get("content")
                    texts.append(c if isinstance(c, str) else json.dumps(c))
        for text in texts:
            if "verinoda locate:" in text or "verinoda coupled:" in text:
                for ln in text.replace("\\n", "\n").splitlines():
                    m = LOCATE_LINE.match(ln)
                    if m and ("/" in m.group(1) or "." in m.group(1)):
                        out.add(m.group(1))
            if text.startswith("[Verinoda coupled]"):
                for ln in text.splitlines():
                    m = COUPLED_LINE.match(ln)
                    if m:
                        out.add(m.group(1))
    return out


def funnel(tasks: list[dict], rows: list[dict], transcript_of=None) -> dict[str, dict]:
    gold = {t["id"]: set(t["gold"]) for t in tasks}
    arms: dict[str, dict] = {}
    for r in rows:
        if r["id"] not in gold:
            continue
        path = transcript_of(r) if transcript_of else None
        shown = shown_paths(path) if path else set()
        named = set(r.get("files") or [])
        a = arms.setdefault(r["arm"], {"sessions": 0, "gold": 0, "shown": 0, "named": 0, "shown_and_named": 0,
                                       "shown_not_named": 0, "named_not_shown": 0, "neither": 0})
        a["sessions"] += 1
        for g in gold[r["id"]]:
            s, n = g in shown, g in named
            a["gold"] += 1
            a["shown"] += s
            a["named"] += n
            a["shown_and_named"] += s and n
            a["shown_not_named"] += s and not n
            a["named_not_shown"] += n and not s
            a["neither"] += not s and not n
    return arms


def transcript_by_session(row: dict) -> Path | None:
    sid = row.get("session_id")
    hits = glob.glob(os.path.expanduser(f"~/.claude/projects/*/{sid}.jsonl")) if sid else []
    return Path(hits[0]) if hits else None


def main() -> int:
    tasks = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))["tasks"]
    rows = [json.loads(x) for p in sys.argv[2].split(",") for x in Path(p).read_text(encoding="utf-8").splitlines() if x.strip()]
    out = funnel(tasks, rows, transcript_by_session)
    Path(sys.argv[3]).write_bytes((json.dumps(out, indent=1) + "\n").encode("utf-8"))
    print(f"{'arm':14s} {'gold':>5s} {'shown':>6s} {'named':>6s} {'both':>5s} {'shown only':>10s} {'named only':>10s} {'neither':>7s}")
    for arm, a in out.items():
        print(f"{arm:14s} {a['gold']:5d} {a['shown']:6d} {a['named']:6d} {a['shown_and_named']:5d} {a['shown_not_named']:10d} "
              f"{a['named_not_shown']:10d} {a['neither']:7d}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
