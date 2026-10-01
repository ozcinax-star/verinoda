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


def _env_line(env: dict | None) -> str:
    env = env or {}
    dirty = " (with uncommitted changes)" if env.get("verinoda_dirty") else ""
    return (f"Verinoda {env.get('verinoda_version')} at {env.get('verinoda_commit')}{dirty}, Python "
            f"{env.get('python')}, {env.get('platform')}, {env.get('cpus')} CPUs")


def _gold(g: dict, key: str | None = None) -> str:
    g = (g.get(key) if key else g) or {}
    return f"{g.get('hits', 0)}/{g.get('total', 0)}" if g.get("total") else "-"


def table_rows(results: dict) -> list[list[str]]:
    rows = []
    for r in results.get("repos", []):
        if r.get("error"):
            rows.append([r["name"], "-", "-", "-", "-", "-", "-", "error", "-", "-"])
            continue
        g = r.get("gold") or {}
        crashes = len(r.get("crashes") or [])
        timeouts = len(r.get("timeouts") or [])
        clean = {True: "yes", False: "NO", None: "-"}[r.get("clean_after")]
        rows.append([
            f"{r['name']} ({r.get('language') or '?'})", str(r.get("files", "-")), _s(r.get("scan_s")),
            _s(r.get("update_s")), f"{_s(r.get('query_median_s'))} / {_s(r.get('analyze_median_s'))}",
            str(crashes), str(timeouts), clean, _gold(g), _gold(g, "v2"),
        ])
    return rows


def render(results: dict) -> str:
    envs = []                   # the environments of the repositories listed (not of runs they replaced)
    for r in results.get("repos", []):
        line = _env_line(r.get("environment") or results.get("environment"))
        if line not in envs:
            envs.append(line)
    if not envs:
        envs = [_env_line(results.get("environment"))]
    out = [f"# Real-world run {results.get('date', '')}", ""]
    out += [f"{e}." for e in envs]
    out += ["", "Times in seconds, wall clock, one process at a time. Gold v1: the frozen facts; v2: the "
            "corrected checks added after review.", "",
            "| repo | files | scan s | update s | query / analyze median s | crashes | timeouts | clean after | "
            "gold v1 | gold v2 |",
            "|---|---:|---:|---:|---:|---:|---:|---|---:|---:|"]
    out += ["| " + " | ".join(row) + " |" for row in table_rows(results)]
    for r in results.get("repos", []):
        if r.get("error"):
            out += ["", f"## {r['name']}", "", f"Not run: {r['error']}"]
            continue
        out += ["", f"## {r['name']} {r.get('tag')}", ""]
        if r.get("environment"):
            out += [f"{_env_line(r['environment'])}; started {r.get('started')}.", ""]
        for n in r.get("notes") or []:
            out.append(f"Note: {n}")
        for n in r.get("skipped") or []:
            out.append(f"Skipped: {n}")
        if r.get("clean_after") is False:
            out.append(f"NOT clean after the revert: {', '.join(r.get('dirty_after') or [])}")
        if r.get("notes") or r.get("skipped") or r.get("clean_after") is False:
            out.append("")
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
                v = f" v{f['v']}" if f.get("v", 1) != 1 else ""
                out.append(f"- {'hit ' if f['hit'] else 'MISS'} `{f['id']}`{v} ({f['kind']})"
                           f"{': ' + f['why'] if f['why'] else ''}")
            out.append("")
        out.append("| step | exit | s | stdout bytes |")
        out.append("|---|---:|---:|---:|")
        for s in r.get("steps", []):
            flag = " (timeout)" if s.get("timeout") else (" (crash)" if s.get("crash") else "")
            st = f" {s['answer_status']}" if s.get("answer_status") else ""
            out.append(f"| {s['step']} | {_s(s.get('exit'))}{flag}{st} | {s['seconds']:.2f} | {s['stdout_bytes']} |")
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
