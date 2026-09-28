"""Aggregate the evaluator rows (DESIGN.md section 6) -> out/summary.json, out/scores.json and markdown tables."""
from __future__ import annotations

import json
import statistics as st
import sys
from pathlib import Path

CB = Path(__file__).resolve().parent.parent
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else CB / "out"
ARMS = ("vq", "va", "gq", "bm25")
NAMES = {"vq": "Verinoda query", "va": "Verinoda analyze", "gq": "Graphify query", "bm25": "BM25 baseline"}
GRANS = ("file", "span", "line", "symbol")
LANGS = ("c", "cpp", "go", "java", "javascript", "python", "rust", "typescript")


def load():
    runs = {}
    for l in (OUT / "runs.jsonl").read_text(encoding="utf-8").splitlines():
        d = json.loads(l)
        runs[d["id"]] = d  # last record wins
    inst = []
    for iid, rec in runs.items():
        if rec.get("status") != "ok":
            continue
        rows = [json.loads(l) for l in (OUT / "eval" / f"{iid}.labelled.jsonl").read_text(encoding="utf-8").splitlines()]
        gold = next(r for r in rows if r["arm"] == "gold")
        if "error" in gold:
            print("gold entry error", iid, gold["error"], file=sys.stderr)
            continue
        gs = {g: gold["final"][g]["gold_size"] for g in GRANS}
        ceil = {g: gold["final"][g]["coverage"] for g in GRANS}
        cells = {}
        for r in rows:
            if r["arm"] == "gold":
                continue
            key = f'{r["arm"]}|{r["view"]}|{r["cap"]}'
            if "error" in r:
                c = {g: {"inter": 0, "gold": gs[g], "pred": 0} for g in GRANS}
                c["error"] = r["error"]
            else:
                c = {g: {"inter": r["final"][g]["intersection"], "gold": r["final"][g]["gold_size"],
                         "pred": r["final"][g]["pred_size"]} for g in GRANS}
            c["chars"] = r["chars"]
            cells[key] = c
        inst.append({"id": iid, "lang": rec["lang"], "gold_sizes": gs, "ceiling": ceil, "cells": cells,
                     "v_index_s": rec["v_index"]["s"], "v_index_rc": rec["v_index"]["rc"],
                     "g_index_s": rec["g_index"]["s"], "g_index_rc": rec["g_index"]["rc"],
                     "bm25_index_s": (rec.get("bm25_index") or {}).get("s"),
                     "arms": rec["arms"], "checkout_mb": rec.get("checkout_mb"), "total_s": rec.get("total_s")})
    return runs, inst


def prf(inter, gold, pred):
    r = inter / gold if gold else None
    p = inter / pred if pred else None
    f = 2 * p * r / (p + r) if (p is not None and r is not None and p + r > 0) else (0.0 if r is not None else None)
    return r, p, f


def agg(items, key):
    out = {}
    for g in GRANS:
        I = sum(x["cells"][key][g]["inter"] for x in items)
        Gs = sum(x["cells"][key][g]["gold"] for x in items)
        P = sum(x["cells"][key][g]["pred"] for x in items)
        r, p, f = prf(I, Gs, P)
        rs = [x["cells"][key][g]["inter"] / x["cells"][key][g]["gold"] for x in items if x["cells"][key][g]["gold"]]
        ps = [x["cells"][key][g]["inter"] / x["cells"][key][g]["pred"] for x in items if x["cells"][key][g]["pred"]]
        fs = []
        for x in items:
            c = x["cells"][key][g]
            rr, pp, ff = prf(c["inter"], c["gold"], c["pred"])
            if rr is not None:
                fs.append(ff or 0.0)
        out[g] = {"micro_recall": r, "micro_precision": p, "micro_f1": f,
                  "macro_recall": st.mean(rs) if rs else None, "macro_precision": st.mean(ps) if ps else None,
                  "macro_f1": st.mean(fs) if fs else None,
                  "empty_preds": sum(1 for x in items if x["cells"][key][g]["pred"] == 0), "n": len(items)}
    toks = [x["cells"][key]["chars"] / 4 for x in items]
    out["tokens_mean"] = st.mean(toks) if toks else None
    out["tokens_median"] = st.median(toks) if toks else None
    out["evaluator_errors"] = sum(1 for x in items if "error" in x["cells"][key])
    for g in ("file", "span", "line"):
        r = out[g]["micro_recall"]
        out[g]["recall_per_1k_tokens"] = (r / (out["tokens_mean"] / 1000)) if (r is not None and out["tokens_mean"]) else None
    return out


def pct(xs, q):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    k = min(len(xs) - 1, max(0, int(round(q * (len(xs) - 1)))))
    return xs[k]


def main():
    runs, inst = load()
    keys = [f"{a}|{v}|{c}" for a in ARMS for v in ("cited", "shown") for c in ("capped", "uncapped")]
    summary = {"n_instances": len(inst), "by_lang_n": {l: sum(1 for x in inst if x["lang"] == l) for l in LANGS},
               "not_ok": {k: v.get("status") for k, v in runs.items() if v.get("status") != "ok"},
               "overall": {}, "by_lang": {l: {} for l in LANGS}}
    for k in keys:
        summary["overall"][k] = agg(inst, k)
        for l in LANGS:
            items = [x for x in inst if x["lang"] == l]
            if items:
                summary["by_lang"][l][k] = agg(items, k)
    # ceiling (gold entry)
    summary["ceiling"] = {g: prf(sum(x["ceiling"][g] * x["gold_sizes"][g] for x in inst),
                                 sum(x["gold_sizes"][g] for x in inst), 1)[0] for g in GRANS}
    # timings and failures
    tm = {}
    for name, xs in (("verinoda_scan", [x["v_index_s"] for x in inst]), ("graphify_update", [x["g_index_s"] for x in inst]),
                     ("bm25_index", [x["bm25_index_s"] for x in inst])):
        tm[name] = {"median_s": pct(xs, .5), "p90_s": pct(xs, .9), "max_s": pct(xs, 1.0),
                    "sum_s": sum(x for x in xs if x is not None)}
    for a in ARMS:
        xs = [x["arms"][a]["s"] for x in inst]
        tm[a + "_query"] = {"median_s": pct(xs, .5), "p90_s": pct(xs, .9), "max_s": pct(xs, 1.0)}
    summary["timings"] = tm
    summary["failures"] = {
        "verinoda_scan_nonzero": [x["id"] for x in inst if x["v_index_rc"] != 0],
        "graphify_update_nonzero": [x["id"] for x in inst if x["g_index_rc"] != 0],
        **{f"{a}_nonzero": [x["id"] for x in inst if x["arms"][a]["rc"] != 0] for a in ARMS},
        **{f"{a}_empty_text": [x["id"] for x in inst if x["arms"][a]["chars"] == 0] for a in ARMS}}
    (OUT / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    (OUT / "scores.json").write_text(json.dumps(inst, indent=1), encoding="utf-8")
    print(tables(summary))


def f3(x):
    return "-" if x is None else f"{x:.3f}"


def tables(s):
    L = []
    for view in ("cited", "shown"):
        for cap in ("capped", "uncapped"):
            L.append(f"\n### Overall, {view.upper()}, {cap} (N = {s['n_instances']})\n")
            L.append("| arm | file R | file P | file F1 | span R | span P | span F1 | line R | symbol R | tokens mean | "
                     "file R /1k tok | span R /1k tok | empty |")
            L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
            for a in ARMS:
                o = s["overall"][f"{a}|{view}|{cap}"]
                L.append(f"| {NAMES[a]} | {f3(o['file']['micro_recall'])} | {f3(o['file']['micro_precision'])} | "
                         f"{f3(o['file']['micro_f1'])} | {f3(o['span']['micro_recall'])} | "
                         f"{f3(o['span']['micro_precision'])} | {f3(o['span']['micro_f1'])} | "
                         f"{f3(o['line']['micro_recall'])} | {f3(o['symbol']['micro_recall'])} | "
                         f"{o['tokens_mean']:.0f} | {f3(o['file']['recall_per_1k_tokens'])} | "
                         f"{f3(o['span']['recall_per_1k_tokens'])} | {o['file']['empty_preds']} |")
    # per language, primary views (capped): CITED file R / P, CITED span F1, SHOWN span F1
    for view, metric, label in (("cited", ("file", "micro_recall"), "CITED file recall"),
                                ("cited", ("file", "micro_precision"), "CITED file precision"),
                                ("cited", ("span", "micro_f1"), "CITED span F1"),
                                ("shown", ("file", "micro_recall"), "SHOWN file recall"),
                                ("shown", ("span", "micro_recall"), "SHOWN span recall"),
                                ("shown", ("span", "micro_f1"), "SHOWN span F1")):
        L.append(f"\n### Per language, {label} (capped, micro)\n")
        L.append("| language | n | " + " | ".join(NAMES[a] for a in ARMS) + " |")
        L.append("|---|---|" + "---|" * len(ARMS))
        for lang in LANGS + ("overall",):
            src = s["overall"] if lang == "overall" else s["by_lang"].get(lang) or {}
            if not src:
                continue
            n = s["n_instances"] if lang == "overall" else s["by_lang_n"][lang]
            vals = [f3(src[f"{a}|{view}|capped"][metric[0]][metric[1]]) for a in ARMS]
            L.append(f"| {lang} | {n} | " + " | ".join(vals) + " |")
    # macro (secondary)
    for view in ("cited", "shown"):
        L.append(f"\n### Macro (mean over instances), {view.upper()}, capped\n")
        L.append("| arm | file R | file P (non-empty) | span R | span P (non-empty) | span F1 | empty preds |")
        L.append("|---|---|---|---|---|---|---|")
        for a in ARMS:
            o = s["overall"][f"{a}|{view}|capped"]
            L.append(f"| {NAMES[a]} | {f3(o['file']['macro_recall'])} | {f3(o['file']['macro_precision'])} | "
                     f"{f3(o['span']['macro_recall'])} | {f3(o['span']['macro_precision'])} | "
                     f"{f3(o['span']['macro_f1'])} | {o['file']['empty_preds']} |")
    tm = s["timings"]
    L.append("\n### Timings (wall seconds, measured with 3 instances in flight on a shared machine)\n")
    L.append("| step | median | p90 | max | sum |")
    L.append("|---|---|---|---|---|")
    for k, v in tm.items():
        L.append(f"| {k} | {f3(v['median_s'])} | {f3(v['p90_s'])} | {f3(v['max_s'])} | "
                 f"{f3(v.get('sum_s'))} |")
    L.append(f"\nceiling (gold entry, micro recall): " + ", ".join(f"{g} {f3(v)}" for g, v in s["ceiling"].items()))
    L.append("failures: " + json.dumps({k: v for k, v in s["failures"].items() if v}))
    L.append("not ok: " + json.dumps(s["not_ok"]))
    return "\n".join(L)


if __name__ == "__main__":
    main()
