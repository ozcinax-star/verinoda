"""The token multiplier ("N x fewer tokens than reading every file"), with the facts each approach found.

Reads committed result files only (standard library, no index, no network) and writes
benchmarks/results/token-multiplier-2026-09-28/multiplier.json; prints the markdown tables of
docs/BENCHMARKS.md, "Update 2026-09-28: the token multiplier, with its accuracy".

    python benchmarks/token_multiplier.py            # write the JSON, print the tables
    python benchmarks/token_multiplier.py --check    # compare with the committed JSON, write nothing

Definitions:
- corpus tokens: ceil(corpus.bytes / 4), bytes and file count as the set's result file records them
  (every file the set's include/exclude selects; all seven corpora are text only);
- tokens per question: chars/4 of what the approach returned, mean over the set's questions;
- multiplier: corpus tokens / tokens per question; over several sets
  sum(corpus tokens x questions) / sum(answer tokens);
- facts per 1k tokens: facts found / answer tokens x 1000.

The script exits 1 if the Graphify and raw-search rows it reads do not add up (with the eighth set's
rows from the rounded baseline table) to the totals of the page's merged-night table.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RES = ROOT / "benchmarks" / "results"
OUT = RES / "token-multiplier-2026-09-28" / "multiplier.json"
VERINODA_FILE = "token-wins-2026-09-26/7-review-fixes.json"
BASELINE_TXT = "token-wins-2026-09-26/graphify-baseline-c8da753.txt"

# set -> the result file its corpus size, Graphify rows and raw-search row come from
SETS = {
    "orders_app": "orders_app.json",
    "orders_app_tr": "orders_app_tr.json",
    "glow_mod": "mods-2026-09-24/final/glow_mod.json",
    "forge_mod": "mods-2026-09-24/final/forge_mod.json",
    "graphify_core": "graphify_core.json",
    "graphify_core_tr": "graphify_core_tr.json",
    "heldout_repoatlas": "heldout_repoatlas.json",
}
# the eighth set of the merged-night table: no committed result file records its corpus, so it is used
# only to check that the Graphify and raw-search totals reproduce that table
CHECK_ONLY = ("verinoda_user_tr",)
MERGED_NIGHT = {  # docs/BENCHMARKS.md, "Update 2026-09-26: the merged night": found, total, tokens/question
    "graphify_vendored": (69, 319, 1391),
    "graphify_cli": (68, 319, 1480),
    "raw": (129, 319, 3991),
}
APPROACHES = {  # key -> (label, where the row comes from)
    "verinoda_query": ("Verinoda query (text)", "q_text"),
    "verinoda_analyze": ("Verinoda analyze (text)", "an_bench"),
    "graphify_vendored": ("Graphify, vendored renderer", "graphify_vendored"),
    "graphify_cli": ("Graphify, upstream CLI", "graphify_cli"),
    "raw": ("raw text search", "raw"),
}


def _load(rel: str) -> dict:
    return json.loads((RES / rel).read_text(encoding="utf-8"))


def baseline_rows() -> dict:
    """{(set, approach): (found, total, tokens per question)} from the rounded text table."""
    rows = {}
    pat = re.compile(r"^(\S+)\s+(\S+)\s+(\d+)/(\d+)\s+\d+\s+\d+\s+\d+\s+(\d+)\s")
    for line in (RES / BASELINE_TXT).read_text(encoding="utf-8").splitlines():
        m = pat.match(line)
        if m and m.group(1) != "all":
            rows[(m.group(1), m.group(2))] = (int(m.group(3)), int(m.group(4)), int(m.group(5)))
    return rows


def _row(found: int, total: int, answer_tokens: float, n_q: int, source: str, corpus_tokens: int) -> dict:
    tpq = answer_tokens / n_q
    return {"found": found, "total": total, "tokens_per_question": round(tpq, 1),
            "answer_tokens": round(answer_tokens), "multiplier": round(corpus_tokens / tpq, 2),
            "facts_per_1k_tokens": round(found / answer_tokens * 1000, 2), "source": source}


def compute() -> tuple[dict, list[str]]:
    errors: list[str] = []
    base = baseline_rows()
    ver = _load(VERINODA_FILE)
    sets = {}
    for name, rel in SETS.items():
        res = _load(rel)
        corpus, summary = res["corpus"], res["summary"]
        ctoks = math.ceil(corpus["bytes"] / 4)
        n_q = summary["graphify_vendored"]["questions"]
        rows = {}
        for key, (_label, src) in APPROACHES.items():
            if key.startswith("verinoda"):
                v = ver[name][src]
                if v["n_q"] != n_q:
                    errors.append(f"{name}: {VERINODA_FILE} has {v['n_q']} questions, {rel} {n_q}")
                rows[key] = _row(v["found"], v["total"], v["tokens"], v["n_q"], f"{VERINODA_FILE} {src}", ctoks)
            elif src in summary:
                s = summary[src]
                rows[key] = _row(s["facts_found"], s["facts_total"], s["tokens_total"], s["questions"],
                                 f"{rel} summary.{src}", ctoks)
                if abs(rows[key]["facts_per_1k_tokens"] - s["facts_per_1k_tokens"]) > 0.01:
                    errors.append(f"{name} {src}: facts per 1k {rows[key]['facts_per_1k_tokens']}, "
                                  f"{rel} {s['facts_per_1k_tokens']}")
                b = base.get((name, src))
                if b and (b[0] != s["facts_found"] or abs(b[2] - s["tokens_mean"]) > 0.5):
                    errors.append(f"{name} {src}: {rel} gives {s['facts_found']} at {s['tokens_mean']}, "
                                  f"{BASELINE_TXT} {b[0]} at {b[2]}")
            else:  # Graphify's CLI was not in the mod sets' runs: the rounded baseline table
                found, total, tpq = base[(name, src)]
                rows[key] = _row(found, total, tpq * n_q, n_q, f"{BASELINE_TXT} (rounded tokens/question)", ctoks)
        totals = {r["total"] for r in rows.values()}
        if len(totals) != 1:
            errors.append(f"{name}: gold totals differ between sources: {sorted(totals)}")
        sets[name] = {"corpus": {"files": corpus["file_count"], "bytes": corpus["bytes"], "tokens": ctoks,
                                 "source": f"{rel} corpus"},
                      "questions": n_q, "approaches": rows}

    totals = {}
    nq = sum(s["questions"] for s in sets.values())
    corpus_q = sum(s["corpus"]["tokens"] * s["questions"] for s in sets.values())
    for key, (label, _src) in APPROACHES.items():
        found = sum(s["approaches"][key]["found"] for s in sets.values())
        total = sum(s["approaches"][key]["total"] for s in sets.values())
        ans = sum(s["approaches"][key]["answer_tokens"] for s in sets.values())
        mults = [s["approaches"][key]["multiplier"] for s in sets.values()]
        totals[key] = {"label": label, "found": found, "total": total, "tokens_per_question": round(ans / nq, 1),
                       "multiplier": round(corpus_q / ans, 1), "multiplier_min": min(mults),
                       "multiplier_max": max(mults), "facts_per_1k_tokens": round(found / ans * 1000, 2)}

    # the merged-night check: the seven sets' Graphify and raw rows plus the eighth set's from the text table
    check = {}
    for src, (want_found, want_total, want_tpq) in MERGED_NIGHT.items():
        found = sum(s["approaches"][src]["found"] for s in sets.values())
        total = sum(s["approaches"][src]["total"] for s in sets.values())
        toks = sum(s["approaches"][src]["answer_tokens"] for s in sets.values())
        n = nq
        for extra in CHECK_ONLY:
            f, t, tpq = base[(extra, src)]
            q = ver[extra]["q_text"]["n_q"]
            found, total, toks, n = found + f, total + t, toks + tpq * q, n + q
        got = (found, total, round(toks / n))
        ok = got[:2] == (want_found, want_total) and abs(got[2] - want_tpq) <= 1
        check[src] = {"found": found, "total": total, "tokens_per_question": round(toks / n, 1),
                      "merged_night": f"{want_found}/{want_total} at {want_tpq}", "ok": ok}
        if not ok:
            errors.append(f"merged-night check {src}: {got} != {(want_found, want_total, want_tpq)}")

    out = {
        "generated_by": "python benchmarks/token_multiplier.py",
        "method": {
            "corpus_tokens": "ceil(corpus.bytes / 4); bytes and file_count as the set's result file records them "
                             "(every file the set's include/exclude selects, tracked and untracked-not-ignored; "
                             "all seven corpora are text only)",
            "tokens_per_question": "chars/4 of what the approach returned, mean over the set's questions",
            "multiplier": "corpus tokens / tokens per question; all sets: sum(corpus tokens x questions) / "
                          "sum(answer tokens)",
            "facts_per_1k_tokens": "facts found / answer tokens x 1000",
            "baseline": "reading every file of the corpus for every question (the competitors' baseline)",
        },
        "sources": {"root": "benchmarks/results", "verinoda": VERINODA_FILE, "graphify_and_raw": SETS,
                    "graphify_cli_on_mod_sets": BASELINE_TXT},
        "sets": sets,
        "totals": {"sets": list(sets), "questions": nq, "corpus_tokens_per_question": round(corpus_q / nq, 1),
                   "approaches": totals},
        "merged_night_check": {"extra_sets": list(CHECK_ONLY), "approaches": check},
    }
    return out, errors


def _mult(x: float) -> str:
    return f"{x:,.1f}x" if x < 10 else f"{x:,.0f}x"


def markdown(out: dict) -> str:
    keys = list(APPROACHES)
    head = ["set", "files", "corpus tokens"] + [APPROACHES[k][0] for k in keys]
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for name, s in out["sets"].items():
        cells = [f"`{name}`", str(s["corpus"]["files"]), f"{s['corpus']['tokens']:,}"]
        for k in keys:
            r = s["approaches"][k]
            cells.append(f"{_mult(r['multiplier'])} · {r['tokens_per_question']:,.0f} · {r['found']}/{r['total']}")
        lines.append("| " + " | ".join(cells) + " |")
    t = out["totals"]
    cells = ["**all seven**", str(sum(s["corpus"]["files"] for s in out["sets"].values())),
             f"{t['corpus_tokens_per_question']:,.0f} per question"]
    for k in keys:
        r = t["approaches"][k]
        cells.append(f"**{_mult(r['multiplier'])} · {r['tokens_per_question']:,.0f} · {r['found']}/{r['total']}**")
    lines.append("| " + " | ".join(cells) + " |")
    lines += ["", "| approach | facts found | tokens per question | facts per 1k tokens | multiplier, all seven "
              "| multiplier per set |", "|---|---|---|---|---|---|"]
    for k in keys:
        r = t["approaches"][k]
        lines.append(f"| {r['label']} | {r['found']}/{r['total']} | {r['tokens_per_question']:,.0f} | "
                     f"{r['facts_per_1k_tokens']:.2f} | {_mult(r['multiplier'])} | "
                     f"{_mult(r['multiplier_min'])} - {_mult(r['multiplier_max'])} |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="compare with the committed JSON, write nothing")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # the tables use a middle dot
    out, errors = compute()
    text = json.dumps(out, indent=1, ensure_ascii=False) + "\n"
    if args.check:
        same = OUT.is_file() and OUT.read_text(encoding="utf-8") == text
        print("multiplier.json is up to date" if same else "multiplier.json differs from a fresh computation")
        errors += [] if same else ["stale multiplier.json"]
    else:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        with open(OUT, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        print(markdown(out))
        print()
        for src, c in out["merged_night_check"]["approaches"].items():
            print(f"merged-night check {src}: {c['found']}/{c['total']} at {c['tokens_per_question']:,} "
                  f"(page: {c['merged_night']}) {'ok' if c['ok'] else 'MISMATCH'}")
    for e in errors:
        print("error:", e, file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
