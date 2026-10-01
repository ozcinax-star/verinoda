"""Paired runs of analyze without a host intent, with a correct one and with a wrong one.

verdict audit (benchmarks/verdict_audit, all 39 cases) and the fastbench sets whose corpora are in the
repository (the graphify sets need an external checkout and are left out). Each condition runs on its own
fresh copy of an indexed base, so no condition reuses another's claims.

usage (from a source checkout, PYTHONPATH at its root): python run.py WORK OUT.json [audit|fast|both]; python summarize.py OUT.json
"""
import json
import shutil
import sys
import time
from pathlib import Path

from verinoda import analysis
from verinoda.benchmark import approaches as ap
from verinoda.benchmark import runner
from verinoda.benchmark import verdict_audit as va
from verinoda.benchmark.review_eval import _rmtree
from verinoda.store import open_store

AUDIT_INTENT = {
    "pyloop-callsoon-callers": "callers", "pyloop-why-pause": "why", "pyloop-copy-env": "config",
    "webui-openpalette": "callers", "webui-useauth-components": "callers", "mixmod-unlisted": "config",
    "glow-wisp-ticked": "callers", "glow-why-heart": "why", "verinoda-stale-decision": "flow",
    "verinoda-lock-why": "why", "mixmod-unlisted-tr": "config", "mixmod-spark-ticked": "callers",
    "verinoda-openpalette": "callers", "orders-not-called-by-handlers": "callers", "orders-why-commit": "why",
    "pyloop-callsoon-callers-tr": "callers", "webui-useauth-components-tr": "callers",
    "pyloop-schedule-callers": "callers", "pyloop-why-adr": "why", "webui-renderlist-callers": "callers",
    "mixmod-lamp-ticked": "callers", "glow-config-load-callers": "callers", "glow-wisp-defined": "locate",
    "glow-plugin-blockspertick": "locate", "forge-addheat-callers": "callers", "forge-blockentity-ticked": "callers",
    "orders-why-sqlite": "why", "orders-place-order-callers": "callers", "verinoda-invalidate-callers": "callers",
    "verinoda-detect-defined": "locate", "pyloop-resolution-defined": "locate", "webui-useauth-defined": "locate",
    "mixmod-mixin-defined": "locate", "mixmod-coolall-callers": "callers", "glow-items-registered": "locate",
    "forge-maxheat-declared": "locate", "orders-env-config": "config", "orders-order-written": "dataflow",
    "verinoda-mcp-size-env": "config",
}
ORDERS = {"q01": "dataflow", "q02": "flow", "q03": "config", "q04": "tests", "q05": "why", "q06": "impact",
          "q07": "locate", "q08": "flow", "q09": "config", "q10": "behaviour"}
FAST_INTENT = {
    "forge_mod": {"q01": "callers", "q02": "callers", "q03": "locate", "q04": "locate", "q05": "locate",
                  "q06": "config", "q07": "config", "q08": "locate", "q09": "locate", "q10": "flow",
                  "q11": "behaviour", "q12": "flow", "q13": "callers", "q14": "locate"},
    "glow_mod": {"q01": "callers", "q02": "callers", "q03": "locate", "q04": "callers", "q05": "callers",
                 "q06": "config", "q07": "config", "q08": "locate", "q09": "locate", "q10": "flow",
                 "q11": "behaviour", "q12": "locate", "q13": "behaviour", "q14": "flow"},
    "heldout_repoatlas": {"h01": "locate", "h02": "config", "h03": "flow", "h04": "impact", "h05": "flow",
                          "h06": "why", "h07": "locate", "h08": "tests"},
    "orders_app": ORDERS, "orders_app_tr": ORDERS,
    "verinoda_user_tr": {"u01": "flow", "u02": "usage", "u03": "flow", "u04": "locate", "u05": "impact",
                         "u06": "flow", "u07": "why", "u08": "behaviour", "u09": "performance", "u10": "flow",
                         "u11": "why", "u12": "flow"},
}
FAST_SRC = {"forge_mod": "examples/forge_mod", "glow_mod": "examples/glow_mod", "orders_app": "examples/orders_app",
            "orders_app_tr": "examples/orders_app", "heldout_repoatlas": "benchmarks/corpora/heldout_repoatlas_7371990",
            "verinoda_user_tr": "."}
# a host that misreads the question: a fixed wrong intent for each right one
WRONG = {"callers": "tests", "tests": "callers", "flow": "locate", "dataflow": "config", "config": "dataflow",
         "locate": "impact", "define": "impact", "why": "flow", "history": "flow", "impact": "callers",
         "behaviour": "why", "usage": "locate", "performance": "locate", "compare_reference": "locate",
         "decide": "locate"}
CONDS = ("none", "right", "wrong")


def intent_for(cond, right):
    return None if cond == "none" else right if cond == "right" else WRONG[right]


def audit(work: Path, out: dict, say):
    data = va.load_cases()
    by_project = {}
    for c in data["cases"]:
        by_project.setdefault(c["project"], []).append(c)
    rows = []
    for name, cases in by_project.items():
        base, why = va.base_copy(name, data["projects"][name], work)
        if base is None:
            say(f"skip {name}: {why}")
            continue
        for cond in CONDS:
            repo = work / f"run-audit-{cond}" / name
            if repo.exists():
                _rmtree(repo)
            shutil.copytree(base, repo)
            st = open_store(repo)
            try:
                for c in cases:
                    it = intent_for(cond, AUDIT_INTENT[c["id"]])
                    t0 = time.perf_counter()
                    res = analysis.analyze(st, repo, c["question"], intent=it)
                    secs = round(time.perf_counter() - t0, 2)
                    subs = res.get("subquestions") or []
                    verdict = va.overall([s.get("status") or "?" for s in subs])
                    row = {"id": c["id"], "split": c["split"], "kind": c["kind"], "cond": cond, "intent": it,
                           "sub_intents": [s.get("intent") for s in subs],
                           "intent_check": res.get("intent_check"), **va.score_case(c, verdict, va.answer_text(res)),
                           "statuses": sorted(cl["status"] for cl in res.get("claims") or []),
                           "unknowns": len(res.get("unknowns") or []), "seconds": secs}
                    rows.append(row)
                    say(f"audit {cond} {c['id']}: {verdict}{' WRONG' if row['wrong_met'] else ''} "
                        f"{(row['intent_check'] or {}).get('applied')} {secs}s")
            finally:
                st.close()
                _rmtree(repo)
    out["audit"] = rows


def fast(work: Path, out: dict, say):
    here = va.HERE
    rows = []
    for set_name, labels in FAST_INTENT.items():
        qset, _p = runner.load_questions(set_name, here)
        spec = qset.get("corpus") or {}
        base = work / "fastbase" / set_name
        if not (base / ".verinoda" / "index" / "graph.json").is_file():
            if base.exists():
                _rmtree(base)
            ap.prepare_workdir(here / FAST_SRC[set_name], base, include=spec.get("include"),
                               exclude=spec.get("exclude"), commit=spec.get("git_commit"))
            va._index(base, spec.get("verinoda_config"))
            say(f"indexed {set_name}")
        for cond in CONDS:
            repo = work / f"run-fast-{cond}" / set_name
            if repo.exists():
                _rmtree(repo)
            shutil.copytree(base, repo)
            st = open_store(repo)
            try:
                for q in qset["questions"]:
                    it = intent_for(cond, labels[q["id"]])
                    t0 = time.perf_counter()
                    res = analysis.analyze(st, repo, q["question"], intent=it)
                    secs = round(time.perf_counter() - t0, 2)
                    meta = {"agent_tool_calls": 1, "claims": res["claims"], "unknowns": res["unknowns"],
                            "verdicts": [s.get("status") for s in res.get("subquestions") or []],
                            "usage": res["usage"]}
                    sc = runner.score("verinoda_analyze", q, ap.analyze_context(res), meta, repo)
                    v = sc["verdict"]
                    row = {"set": set_name, "id": q["id"], "cond": cond, "intent": it,
                           "intent_check": res.get("intent_check"),
                           "sub_intents": [s.get("intent") for s in res.get("subquestions") or []],
                           "facts": sc["facts"]["found_n"], "facts_total": len(q["facts"]),
                           "met": v["met"], "facts_in_claims": v["facts_in_claims"],
                           "met_wrong": v["met"] and v["facts_in_claims"] < len(q["facts"]),
                           "neg_as_findings": sc["negatives"].get("presented_as_findings", 0),
                           "statuses": sorted(cl["status"] for cl in res.get("claims") or []),
                           "unknowns": len(res.get("unknowns") or []), "chars": sc["chars"], "seconds": secs}
                    rows.append(row)
                    say(f"fast {cond} {set_name}/{q['id']}: facts {row['facts']}/{row['facts_total']} "
                        f"met {row['met']} {(row['intent_check'] or {}).get('applied')} {secs}s")
            finally:
                st.close()
                _rmtree(repo)
    out["fast"] = rows


def main():
    work, dest = Path(sys.argv[1]), Path(sys.argv[2])
    which = sys.argv[3] if len(sys.argv) > 3 else "both"
    work.mkdir(parents=True, exist_ok=True)
    out = json.loads(dest.read_text(encoding="utf-8")) if dest.exists() else {}

    def say(m):
        print(m, flush=True)

    if which in ("audit", "both"):
        audit(work, out, say)
        dest.write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
    if which in ("fast", "both"):
        fast(work, out, say)
        dest.write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
