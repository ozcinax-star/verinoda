"""Recount what the sessions did from their transcripts, with big_run.session_stats as it is now, and write the result
file again (the old counts kept as ``*_uncorrected``). Needed once: the first counter took the name of a working copy
(a folder called `verinoda`, `verinoda_mod`, `graphify`) in a command for a call of the tool.

    python benchmarks/agent_compare/recount_tools.py RESULTS.jsonl [RESULTS.jsonl ...]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from big_run import find_transcript, session_stats

FIELDS = ("tool_calls", "verinoda_calls", "graphify_calls", "network", "first_tools")


def recount(row: dict) -> dict:
    stats = session_stats(find_transcript(row.get("session_id") or ""))
    out = dict(row)
    for k in ("verinoda_calls", "graphify_calls", "network"):
        out[f"{k}_uncorrected"] = row.get(k)
    out.update({k: stats[k] for k in FIELDS})
    out["recounted"] = True
    return out


def main() -> int:
    for path in sys.argv[1:]:
        p = Path(path)
        rows = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
        new = [recount(r) for r in rows]
        p.write_bytes(("\n".join(json.dumps(r) for r in new) + "\n").encode("utf-8"))
        for arm in ("graphify", "verinoda_mod", "verinoda_setup"):
            a = [(r, n) for r, n in zip(rows, new, strict=True) if r["arm"] == arm]
            was = sum(((r.get("verinoda_calls") or 0) + (r.get("graphify_calls") or 0)) > 0 for r, _ in a)
            now = sum(((n["verinoda_calls"] or 0) + (n["graphify_calls"] or 0)) > 0 for _, n in a)
            print(f"{p.name} {arm}: sessions that used the tool {was} -> {now} of {len(a)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
