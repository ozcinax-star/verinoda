"""Tables for the 2026-10-02 rerun of the eight public sets: facts found and tokens per question per approach, for
both Graphify CLI versions, next to the last committed measurements of the same approaches.

Old sources: Verinoda's query text and analyze from token-wins-2026-09-26/7-review-fixes.json (fd30f5b, the figures
docs/BENCHMARKS.md quotes); raw search and Graphify from benchmarks/results/<set>.json (round 3, 2026-09-23;
Graphify CLI 0.9.65) and mods-2026-09-24/final/<set>.json for glow_mod and forge_mod (no CLI there).

    python benchmarks/results/compare-2026-10-02/summarize.py
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
RES = HERE.parent
SETS = ("orders_app", "orders_app_tr", "glow_mod", "forge_mod", "graphify_core", "graphify_core_tr",
        "heldout_repoatlas", "verinoda_user_tr")
APPROACHES = (("verinoda_retrieve_text", "Verinoda query (text)"), ("verinoda_analyze", "Verinoda analyze (text)"),
              ("graphify_vendored", "Graphify, vendored renderer"), ("graphify_cli", "Graphify CLI"),
              ("raw", "raw text search"))
OLD_VERINODA = {"verinoda_retrieve_text": "q_text", "verinoda_analyze": "an_bench"}


def new_rows(version: str) -> dict[str, dict]:
    out = {}
    for s in SETS:
        p = HERE / f"graphify-{version}" / f"{s}.json"
        if not p.exists():
            p = HERE / f"graphify-{version}" / f"{s}.patched.json"
        if not p.exists():
            continue
        d = json.loads(p.read_text(encoding="utf-8"))
        if d.get("status") not in (None, "ok", "complete"):
            out[s] = {"status": d.get("status")}
            continue
        out[s] = {"patched": p.name.endswith(".patched.json"), "cli": (d.get("environment") or {}).get("graphify_cli"),
                  **{a: d["summary"][a] for a, _ in APPROACHES if a in d["summary"]}}
    return out


def old_rows() -> dict[str, dict]:
    tw = json.loads((RES / "token-wins-2026-09-26" / "7-review-fixes.json").read_text(encoding="utf-8"))
    out = {}
    for s in SETS:
        row = {}
        for a, k in OLD_VERINODA.items():
            v = tw.get(s, {}).get(k)
            if v:
                n = len(v["per_q"])
                row[a] = {"facts_found": v["found"], "tokens_mean": v["tokens"] / n}
        for p in (RES / f"{s}.json", RES / "mods-2026-09-24" / "final" / f"{s}.json"):
            if p.exists():
                d = json.loads(p.read_text(encoding="utf-8"))
                for a in ("raw", "graphify_vendored", "graphify_cli"):
                    if a in d.get("summary", {}):
                        row[a] = d["summary"][a]
                break
        out[s] = row
    return out


def cell(r: dict | None) -> str:
    if not r:
        return "-"
    return f"{r['facts_found']} · {round(r['tokens_mean']):,}"


def main() -> None:
    old = old_rows()
    new73, new65 = new_rows("0.9.73"), new_rows("0.9.65")
    facts_total = {}
    for s in SETS:
        for src in (new73, new65):
            r = src.get(s, {})
            for a, _ in APPROACHES:
                if isinstance(r.get(a), dict) and "facts_total" in r[a]:
                    facts_total[s] = r[a]["facts_total"]
    lines = ["Each cell: facts found · tokens per question (chars/4). New = this rerun (Verinoda 5999912); "
             "old = the last committed figures.", ""]
    head = "| set (facts) | " + " | ".join(f"{name} old | new" for _, name in APPROACHES) + " |"
    lines += [head, "|---" * (1 + 2 * len(APPROACHES)) + "|"]
    totals = {a: {"old": [0, 0.0, 0], "new": [0, 0.0, 0]} for a, _ in APPROACHES}
    for s in SETS:
        r_new = new73.get(s, {})
        if "status" in r_new and len(r_new) == 1:
            lines.append(f"| {s} | not run: {r_new['status']} |")
            continue
        cells = []
        for a, _ in APPROACHES:
            o = old.get(s, {}).get(a)
            n = r_new.get(a) if a != "graphify_cli" else r_new.get(a)
            cells += [cell(o), cell(n)]
            for tag, r in (("old", o), ("new", n)):
                if r:
                    nq = r.get("questions") or 1
                    totals[a][tag][0] += r["facts_found"]
                    totals[a][tag][1] += r["tokens_mean"] * nq
                    totals[a][tag][2] += nq
        mark = " (patched set)" if r_new.get("patched") else ""
        lines.append(f"| {s} ({facts_total.get(s, '?')}){mark} | " + " | ".join(cells) + " |")
    lines += ["", "Graphify CLI in the new columns: 0.9.73. The same sets with the CLI 0.9.65 (pinned upstream):", "",
              "| set | Graphify CLI 0.9.65 | Graphify CLI 0.9.73 |", "|---|---|---|"]
    for s in SETS:
        a, b = new65.get(s, {}).get("graphify_cli"), new73.get(s, {}).get("graphify_cli")
        lines.append(f"| {s} | {cell(a)} | {cell(b)} |")
    print("\n".join(lines))
    (HERE / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
