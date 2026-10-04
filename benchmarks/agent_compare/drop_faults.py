"""Set aside the sessions of a results file that a harness fault touched, so that rerunning the study (it is resumable: a
task and arm already in the file is skipped) runs them again (DESIGN_ASSIST.md: a session of a mod arm whose transcript
shows the nudge is a fault, rerun once and reported; so is a session the network took from the model, amendment 14).

Two faults:

* the mod's nudge ("[Verinoda auto-context]") reached the model although no arm of the study asks for it (`assist.nudge` > 0):
  rows are moved to `<file>.nudged`;
* the API could not be reached (the machine's connection or DNS was down for minutes): the session ended on its first turn with an
  error, no answer and no cost, whatever its arm: rows are moved to `<file>.api-error`.

Both asides are kept for the record. With `--count` nothing is moved.

    python benchmarks/agent_compare/drop_faults.py [--count] RESULTS.jsonl [RESULTS2.jsonl ...]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def nudged(row: dict) -> bool:
    return bool((row.get("assist") or {}).get("nudge"))


def api_error(row: dict) -> bool:
    """A session that never reached the model: an error result, no answer, no cost, at most one turn."""
    return bool(row.get("is_error")) and not row.get("answered") and not row.get("cost_usd") and (row.get("num_turns") or 0) <= 1


FAULTS = (("nudged", nudged), ("api-error", api_error))


def faulty(row: dict) -> bool:
    return any(test(row) for _, test in FAULTS)


def split(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """(rows to keep, faulty rows)."""
    return [r for r in rows if not faulty(r)], [r for r in rows if faulty(r)]


def lines(rows: list[dict]) -> bytes:
    return "".join(json.dumps(r) + "\n" for r in rows).encode("utf-8")


def read(p: Path) -> list[dict]:
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()] if p.exists() else []


def main(argv: list[str]) -> int:
    count_only = "--count" in argv
    total = 0
    for name in (a for a in argv if a != "--count"):
        p = Path(name)
        rows = read(p)
        keep, bad = split(rows)
        total += len(bad)
        moved = []
        if not count_only and bad:
            for kind, test in FAULTS:
                these = [r for r in bad if test(r)]
                if these:
                    aside = p.with_suffix(p.suffix + "." + kind)
                    aside.write_bytes(lines(read(aside) + these))
                    moved.append(f"{len(these)} to {aside.name}")
            p.write_bytes(lines(keep))
        print(f"{name}: {len(bad)} faulty of {len(rows)}" + (f"; moved {', '.join(moved)}" if moved else ""))
    return 0 if total == 0 or not count_only else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
