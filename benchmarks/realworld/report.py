"""The markdown summary of a real-world run (results.json written by run.py).

    python benchmarks/realworld/report.py benchmarks/results/realworld-<date>/results.json [--out summary.md]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _s(v) -> str:
    return "-" if v is None else (f"{v:.1f}" if isinstance(v, float) else str(v))


def table_rows(results: dict) -> list[list[str]]:
    rows = []
    for r in results.get("repos", []):
        if r.get("error"):
            rows.append([r["name"], "-", "-", "-", "-", "-", "error", "-"])
            continue
        g = r.get("gold") or {}
        crashes = len(r.get("crashes") or [])
        timeouts = len(r.get("timeouts") or [])
        rows.append([
            f"{r['name']} ({r.get('language') or '?'})", str(r.get("files", "-")), _s(r.get("scan_s")),
            _s(r.get("update_s")), f"{_s(r.get('query_median_s'))} / {_s(r.get('analyze_median_s'))}",
            str(crashes), str(timeouts), f"{g.get('hits', 0)}/{g.get('total', 0)}" if g.get("total") else "-",
        ])
    return rows


def render(results: dict) -> str:
    env = results.get("environment") or {}
    out = [f"# Real-world run {results.get('date', '')}", "",
           f"Verinoda {env.get('verinoda_version')} at {env.get('verinoda_commit')}, Python {env.get('python')}, "
           f"{env.get('platform')}, {env.get('cpus')} CPUs. Times in seconds, wall clock, one process at a time.",
           "", "| repo | files | scan s | update s | query / analyze median s | crashes | timeouts | gold |",
           "|---|---:|---:|---:|---:|---:|---:|---:|"]
    out += ["| " + " | ".join(row) + " |" for row in table_rows(results)]
    for r in results.get("repos", []):
        if r.get("error"):
            out += ["", f"## {r['name']}", "", f"Not run: {r['error']}"]
            continue
        out += ["", f"## {r['name']} {r.get('tag')}", ""]
        bad = [s for s in r.get("steps", []) if s.get("crash") or s.get("timeout")]
        if bad:
            out.append("Crashes and timeouts:")
            out.append("")
            for s in bad:
                tail = (s.get("stderr_tail") or "").strip().splitlines()[-1:] or [""]
                out.append(f"- `{s['step']}`: {s.get('reason')}; {tail[0][:200]}")
            out.append("")
        g = r.get("gold") or {}
        if g.get("facts"):
            out.append("Gold facts:")
            out.append("")
            for f in g["facts"]:
                out.append(f"- {'hit ' if f['hit'] else 'MISS'} `{f['id']}` ({f['kind']}){': ' + f['why'] if f['why'] else ''}")
            out.append("")
        out.append("| step | exit | s | stdout bytes |")
        out.append("|---|---:|---:|---:|")
        for s in r.get("steps", []):
            flag = " (timeout)" if s.get("timeout") else (" (crash)" if s.get("crash") else "")
            out.append(f"| {s['step']} | {_s(s.get('exit'))}{flag} | {s['seconds']:.2f} | {s['stdout_bytes']} |")
    return "\n".join(out) + "\n"


def write_summary(results: dict, path: Path) -> None:
    path.write_text(render(results), encoding="utf-8", newline="\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="markdown summary of a real-world run")
    ap.add_argument("results", type=Path)
    ap.add_argument("--out", type=Path)
    a = ap.parse_args(argv)
    results = json.loads(a.results.read_text(encoding="utf-8"))
    out = a.out or a.results.with_name("summary.md")
    write_summary(results, out)
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
