"""Orchestration: copy corpus, build indexes, run every approach, score, summarise.

Order of work (all timings with ``time.perf_counter``):

1. copy the corpus into a temp workdir (the analysed repo is only read) and
   commit it - from the working tree, or from the commit the question set pins
   (``corpus.git_commit``); re-verify every gold fact against that copy;
2. index cost, reported separately: Verinoda ``workflow.scan`` twice (cold,
   then warm with the AST cache present) and, with ``graphify_cmd``, upstream
   ``graphify update .`` twice in a second copy;
3. ``repeat`` full passes over questions x approaches (pass 1 = cold, later
   passes = warm); the delivered text of pass 1 is scored. With ``sweep`` the
   budgeted approaches also run as ``<approach>@<tokens>`` (see
   :mod:`verinoda.benchmark.approaches`);
4. optional model-in-the-loop answers (``llm="anthropic"``).
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import statistics
import sys
import tempfile
import time
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Callable

from verinoda.paths import configure_index_env

configure_index_env()

from verinoda.benchmark import approaches as ap  # noqa: E402
from verinoda.benchmark import llm as llmmod  # noqa: E402
from verinoda.benchmark import metrics as mx  # noqa: E402

# 2: verinoda_retrieve_text, budget sweep (``<approach>@<tokens>``), facts per 1k tokens,
#    set provenance, corpora pinned to a commit.
SCHEMA = 2
VERIFIED = {"observed", "experiment_verified", "statically_verified", "primary_source_verified"}
FINDING = VERIFIED | {"strong_inference"}
INFERENCE = {"strong_inference", "weak_inference"}


# -- question sets ----------------------------------------------------------------

def builtin_sets() -> dict[str, Path]:
    d = resources.files("verinoda.benchmark") / "questions"
    return {Path(str(p)).stem: Path(str(p)) for p in d.iterdir() if str(p).endswith(".json")}


def load_questions(questions, repo: Path) -> tuple[dict, str]:
    """``questions``: path to a JSON set, a set dict, a built-in set name, or None (auto-detect)."""
    if isinstance(questions, dict):
        return questions, "<inline>"
    sets = builtin_sets()
    if questions is None:
        for name, p in sorted(sets.items()):
            data = json.loads(p.read_text(encoding="utf-8"))
            if all((Path(repo) / d).exists() for d in (data.get("corpus") or {}).get("detect", ["\0"])):
                return data, str(p)
        raise FileNotFoundError(f"no built-in question set matches {repo}; pass --questions "
                                f"(built-in: {', '.join(sorted(sets))})")
    if str(questions) in sets:
        p = sets[str(questions)]
    else:
        p = Path(questions)
    data = json.loads(Path(p).read_text(encoding="utf-8"))
    return data, str(p)


def _check_schema(qs: dict) -> None:
    ids = set()
    for q in qs.get("questions", []):
        for key in ("id", "question", "facts"):
            if key not in q:
                raise ValueError(f"question {q.get('id')!r} lacks {key!r}")
        for f in q["facts"] + q.get("negatives", []):
            if f["id"] in ids:
                raise ValueError(f"duplicate id {f['id']}")
            ids.add(f["id"])


# -- environment ----------------------------------------------------------------

def environment(graphify_version: str | None, graphify_path: str | None) -> dict:
    from verinoda import __version__
    from verinoda.snapshot import git

    up = Path(__file__).resolve().parent.parent / "project_index" / "UPSTREAM_COMMIT"
    gv = None
    try:
        import subprocess

        gv = subprocess.run(["git", "--version"], capture_output=True, text=True, timeout=30).stdout.strip()
    except OSError:
        pass
    src = Path(__file__).resolve().parents[2]
    # The code under test is the ``verinoda`` package; record whether it differs from HEAD and how,
    # so a result measured on an uncommitted working tree can still be tied to exact sources.
    pkg_changes = (git(src, "status", "--porcelain", "--", "verinoda") or "").splitlines()
    pkg_diff = git(src, "diff", "HEAD", "--", "verinoda") or ""
    return {
        "date_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "os": platform.platform(), "machine": platform.machine(), "processor": platform.processor(),
        "cpu_count": os.cpu_count(), "python": sys.version.split()[0], "python_impl": platform.python_implementation(),
        "verinoda_version": __version__,
        "vendored_graphify_commit": up.read_text(encoding="utf-8").strip() if up.exists() else None,
        "graphify_cli": {"path": graphify_path, "version": graphify_version} if graphify_path else None,
        "git": gv,
        "verinoda_source_commit": (git(src, "rev-parse", "HEAD") or "").strip() or None,
        "verinoda_package_changes_vs_commit": [l.strip() for l in pkg_changes][:60],
        "verinoda_package_diff_sha256": _sha(pkg_diff) if pkg_diff else None,
    }


# -- scoring ------------------------------------------------------------------------

def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _claim_stats(claims: list[dict], delivered_text: str) -> dict:
    """Claim quality from the store's view, cross-checked against what was delivered."""
    try:
        shown = {c.get("id"): c for c in json.loads(delivered_text).get("claims", [])}
    except ValueError:
        shown = {}
    by: dict[str, int] = {}
    for c in claims:
        by[c["status"]] = by.get(c["status"], 0) + 1
    n = len(claims)
    ev_types: dict[str, int] = {}
    for c in claims:
        for e in c.get("evidence", []):
            parts = e.split(":", 2)
            if len(parts) == 3:
                ev_types[parts[1]] = ev_types.get(parts[1], 0) + 1
    from verinoda.evidence import NON_VERIFYING

    return {
        "total": n, "by_status": by,
        "verified": sum(v for k, v in by.items() if k in VERIFIED),
        "inference": sum(v for k, v in by.items() if k in INFERENCE),
        "verified_share": round(sum(v for k, v in by.items() if k in VERIFIED) / n, 3) if n else None,
        "inference_share": round(sum(v for k, v in by.items() if k in INFERENCE) / n, 3) if n else None,
        "weak_or_unknown": by.get("weak_inference", 0) + by.get("unknown", 0),
        # A weak/unknown claim is "presented as a finding" only if the output hides its status.
        "weak_or_unknown_unlabelled": sum(1 for c in claims if c["status"] in ("weak_inference", "unknown")
                                          and shown.get(c["id"], {}).get("status") != c["status"]),
        "contradicted_shown": by.get("contradicted", 0), "stale_shown": by.get("stale", 0),
        "evidence_by_type": ev_types,
        "evidence_verifying": sum(v for k, v in ev_types.items() if k not in NON_VERIFYING),
        "evidence_non_verifying": sum(v for k, v in ev_types.items() if k in NON_VERIFYING),
    }


def score(approach: str, q: dict, text: str, meta: dict, root: Path) -> dict:
    approach, _tokens = ap.split_approach(approach)
    facts = mx.score_facts(q["facts"], text)
    locs = mx.extract_locators(text)
    out: dict = {
        "chars": len(text), "tokens": mx.count_tokens(text), "sha256": _sha(text),
        "facts": facts,
        "locators": {"distinct": len(locs), "files": len({l.path for l in locs}),
                     "point_or_short": sum(1 for l in locs if l.span <= mx.PINPOINT_MAX_LINES)},
        "agent_tool_calls": meta.get("agent_tool_calls"),
    }
    if approach == "raw":
        assertions: list[dict] = []
        out["claims"] = "n/a (no claims): source text only"
        out["raw"] = {k: meta[k] for k in ("terms", "matching_files", "matching_lines", "files_read", "truncated")}
    elif approach.startswith("graphify"):
        assertions = mx.graphify_assertions(text)
        out["claims"] = "n/a (no claims): graph context; EDGE lines are scored as asserted relations"
        out["graphify"] = {k: meta[k] for k in ("nodes_shown", "edges_shown", "edge_confidence", "truncated",
                                                "nodes_found", "depth", "budget_tokens")}
        if meta.get("stderr"):
            out["graphify"]["stderr"] = meta["stderr"]
    elif approach == "verinoda_retrieve":
        assertions = mx.retrieval_assertions(meta["edges"])
        out["claims"] = "n/a (no claims): retrieval excerpts; edges are scored as asserted relations"
        out["retrieve"] = {"items": meta["items"], "edges": len(meta["edges"]), "edge_confidence": meta["edge_confidence"],
                           "budget": meta["budget"]}
    elif approach == "verinoda_retrieve_text":
        assertions = mx.text_outline_assertions(text)
        out["claims"] = ("n/a (no claims): retrieval text; its calls / called by outlines are scored as "
                         "asserted relations")
        out["retrieve_text"] = {k: meta[k] for k in ("items", "outline_edges", "edge_confidence", "truncated_note",
                                                     "params")}
    else:
        assertions = mx.claim_assertions(meta["claims"])
        out["claims"] = _claim_stats(meta["claims"], text)
        out["unknowns"] = len(meta["unknowns"])
        out["analysis_usage"] = meta["usage"]
    negs = []
    for neg in q.get("negatives", []):
        hits = mx.match_negative(neg, assertions)
        if hits:
            negs.append({"id": neg["id"], "statement": neg["statement"],
                         "matched": [{"text": h["text"][:200], **({"status": h["status"]} if h.get("status") else {})}
                                     for h in hits[:5]]})
    out["negatives"] = {"checked": len(q.get("negatives", [])), "matched_n": len(negs), "matched": negs}
    if approach == "verinoda_analyze":
        wrong = [m for n in negs for m in n["matched"] if m.get("status") in FINDING]
        out["negatives"]["presented_as_findings"] = len(wrong)
        out["negatives"]["labelled_not_findings"] = sum(len(n["matched"]) for n in negs) - len(wrong)
        # Only claims presented as findings are checked; contradicted/stale/weak ones are labelled already.
        cite_input = [a for a in assertions if a.get("status") in FINDING]
        # The reverse error: relation claims Verinoda itself labelled contradicted whose cited line
        # does name the target (e.g. through an import alias) - a true relation marked false.
        contra = [a for a in assertions if a.get("status") == "contradicted" and a.get("rel")]
        cc = mx.check_citations(root, contra)
        out["claims"]["contradicted_relations"] = cc["checked"]
        out["claims"]["contradicted_but_cited_line_names_target"] = cc["checked"] - cc["cited_line_missing_target"]
        out["claims"]["contradicted_named_via_import_alias"] = cc["named_via_import_alias"]
    else:
        cite_input = assertions
    out["relations"] = {"asserted": sum(1 for a in assertions if a.get("rel")), **mx.check_citations(root, cite_input)}
    if meta.get("sweep"):
        out["sweep"] = meta["sweep"]
    return out


# -- summary --------------------------------------------------------------------------

def _med(xs: list[float]) -> float | None:
    return round(statistics.median(xs), 3) if xs else None


def summarize(res: dict) -> dict:
    out = {}
    for a in res["approaches"]:
        rows = [q["approaches"][a] for q in res["questions"] if a in q["approaches"]]
        ok = [r for r in rows if "error" not in r]
        if not ok:
            out[a] = {"status": "no successful runs", "errors": [r.get("error") for r in rows][:3]}
            continue
        toks = [r["score"]["tokens"] for r in ok]
        cold = [r["runs"][0]["seconds"] for r in ok]
        warm = [run["seconds"] for r in ok for run in r["runs"][1:]]
        s = {
            "questions": len(ok), "errors": len(rows) - len(ok),
            "facts_found": sum(r["score"]["facts"]["found_n"] for r in ok),
            "facts_total": sum(r["score"]["facts"]["total"] for r in ok),
            "facts_pinpointed": sum(r["score"]["facts"]["pinpointed_n"] for r in ok),
            "questions_all_facts": sum(1 for r in ok if r["score"]["facts"]["found_n"] == r["score"]["facts"]["total"]),
            "tokens_mean": round(statistics.mean(toks), 1), "tokens_median": _med(toks), "tokens_max": max(toks),
            "tokens_total": sum(toks), "chars_total": sum(r["score"]["chars"] for r in ok),
            "facts_per_1k_tokens": mx.per_1k(sum(r["score"]["facts"]["found_n"] for r in ok), sum(toks)),
            "pinpointed_per_1k_tokens": mx.per_1k(sum(r["score"]["facts"]["pinpointed_n"] for r in ok), sum(toks)),
            "locators_distinct_mean": round(statistics.mean(r["score"]["locators"]["distinct"] for r in ok), 1),
            "agent_tool_calls_mean": round(statistics.mean(r["score"]["agent_tool_calls"] or 0 for r in ok), 2),
            "seconds_cold_median": _med(cold), "seconds_cold_total": round(sum(cold), 3),
            "seconds_warm_median": _med(warm), "seconds_warm_total": round(sum(warm), 3) if warm else None,
            "facts_found_via_locator": sum(1 for r in ok for v in r["score"]["facts"]["via"].values()
                                           if v.startswith("loc ")),
            "assertions_scored": ap.split_approach(a)[0] != "raw",
            "negatives_matched": sum(r["score"]["negatives"]["matched_n"] for r in ok),
            "negatives_checked": sum(r["score"]["negatives"]["checked"] for r in ok),
            "relations_asserted": sum(r["score"]["relations"]["asserted"] for r in ok),
            "relations_checked": sum(r["score"]["relations"]["checked"] for r in ok),
            "relations_cited_line_missing_target": sum(r["score"]["relations"]["cited_line_missing_target"] for r in ok),
            "relations_named_via_import_alias": sum(r["score"]["relations"]["named_via_import_alias"] for r in ok),
        }
        if a == "verinoda_analyze":
            cs = [r["score"]["claims"] for r in ok]
            by: dict[str, int] = {}
            for c in cs:
                for k, v in c["by_status"].items():
                    by[k] = by.get(k, 0) + v
            tot = sum(c["total"] for c in cs)
            s["claims"] = {
                "total": tot, "by_status": by,
                "verified_share": round(sum(by.get(k, 0) for k in VERIFIED) / tot, 3) if tot else None,
                "inference_share": round(sum(by.get(k, 0) for k in INFERENCE) / tot, 3) if tot else None,
                "weak_or_unknown": by.get("weak_inference", 0) + by.get("unknown", 0),
                "weak_or_unknown_unlabelled": sum(c["weak_or_unknown_unlabelled"] for c in cs),
                "negatives_presented_as_findings": sum(r["score"]["negatives"].get("presented_as_findings", 0) for r in ok),
                "contradicted_relations": sum(c["contradicted_relations"] for c in cs),
                "contradicted_but_cited_line_names_target": sum(c["contradicted_but_cited_line_names_target"] for c in cs),
                "contradicted_named_via_import_alias": sum(c["contradicted_named_via_import_alias"] for c in cs),
                "unknowns": sum(r["score"]["unknowns"] for r in ok),
            }
            warm_runs = [run for r in ok for run in r["runs"][1:]]
            s["cache"] = {
                "method": "claims whose id already existed in atlas.db before the run (same text + snapshot)",
                "cold_claims": sum(r["runs"][0]["claims"] for r in ok),
                "cold_reused": sum(r["runs"][0]["claims_reused"] for r in ok),
                "warm_claims": sum(run["claims"] for run in warm_runs),
                "warm_reused": sum(run["claims_reused"] for run in warm_runs),
            }
        if res.get("llm", {}).get("enabled"):
            ms = [r["model"] for r in ok if r.get("model", {}).get("status") == "measured"]
            s["model"] = {
                "answers": len(ms),
                "input_tokens": sum(m["input_tokens"] for m in ms), "output_tokens": sum(m["output_tokens"] for m in ms),
                "cost_usd": round(sum(m["cost_usd"] or 0 for m in ms), 6),
                "answer_facts_found": sum(m["answer_facts"]["found_n"] for m in ms),
            }
        base, tokens = ap.split_approach(a)
        if tokens is not None:
            s["sweep"] = {"base": base, "tokens": tokens, "char_cap": ap.sweep_chars(tokens)}
        out[a] = s
    return out


def sweep_summary(res: dict) -> dict | None:
    """``{base: {tokens: {facts_found, facts_total, facts_pinpointed, tokens_mean, facts_per_1k_tokens, ...}}}``."""
    rows: dict[str, dict] = {}
    for a, s in (res.get("summary") or {}).items():
        base, tokens = ap.split_approach(a)
        if tokens is None or "facts_total" not in s:
            continue
        rows.setdefault(base, {})[str(tokens)] = {
            k: s[k] for k in ("facts_found", "facts_total", "facts_pinpointed", "tokens_mean", "tokens_max",
                              "facts_per_1k_tokens", "pinpointed_per_1k_tokens", "seconds_cold_median",
                              "seconds_warm_median", "negatives_matched", "negatives_checked")}
    return rows or None


# -- main entry ---------------------------------------------------------------------------

def run_benchmark(repo: Path, *, questions=None, out=None, graphify_cmd: str | None = None, llm: str = "none",
                  repeat: int = 2, model: str | None = None, workdir: Path | None = None,
                  keep_workdir: bool = False, progress: Callable[[str], None] | None = None,
                  sweep: list[int] | tuple[int, ...] | None = None, at: str | None = None,
                  sweep_only: bool = False) -> dict:
    """Run the comparison and return the full result dict (see docs/BENCHMARKS.md).

    ``sweep``: token budgets (chars/4) at which the budgeted approaches also run;
    with ``sweep_only`` only those sweep points run (the default configurations
    are skipped, so their timings stay those of a run without a sweep).
    ``at``: take the corpus from this commit of ``repo`` (default: the set's
    ``corpus.git_commit``, else the working tree).
    """
    say = progress or (lambda _m: None)
    repo = Path(repo).resolve()
    qset, qpath = load_questions(questions, repo)
    _check_schema(qset)
    repeat = max(1, int(repeat))
    corpus_spec = qset.get("corpus") or {}
    pin = at or corpus_spec.get("git_commit")
    sweep = sorted({int(t) for t in sweep or [] if int(t) > 0})
    if sweep_only and not sweep:
        raise ValueError("sweep_only needs at least one sweep budget")
    # Always a fresh directory of our own (inside ``workdir`` when given), so the
    # final cleanup can never remove anything the caller put there.
    if workdir:
        Path(workdir).mkdir(parents=True, exist_ok=True)
    base = Path(tempfile.mkdtemp(prefix="verinoda-bench-", dir=str(workdir) if workdir else None))
    ra_root, cli_root = base / "verinoda", base / "graphify_cli"
    model = model or os.environ.get("VERINODA_BENCH_MODEL") or llmmod.DEFAULT_MODEL
    llm_on, llm_reason = llmmod.status(llm)
    gcmd = ap.resolve_cmd(graphify_cmd) if graphify_cmd else None
    res: dict = {
        "benchmark": "verinoda raw-vs-graphify-vs-verinoda", "schema": SCHEMA,
        "set": {"name": qset.get("name"), "file": qpath, "description": qset.get("description"),
                "questions": len(qset["questions"]), "gold_verified_by": qset.get("verified_by"),
                "provenance": qset.get("provenance")},
        "params": {
            "repeat": repeat,
            "raw": {"top_files": ap.RAW_TOP_FILES, "char_cap": ap.RAW_CHAR_CAP, "grep_lines": ap.RAW_GREP_LINES,
                    "min_term_len": ap.RAW_MIN_TERM, "terms": "verinoda.retrieval.terms_for (Graphify _query_terms)"},
            "graphify_vendored": {"renderer": "verinoda.index.graphify_query_text", "mode": "bfs", "depth": 3,
                                  "token_budget": 2000},
            "graphify_cli": {"command": "graphify query <question>", "mode": "bfs", "depth": 2, "token_budget": 2000},
            "verinoda_analyze": {"budget": "config defaults (60 s, 40 tool calls, 6000 tokens)", "challenge": True,
                                  "run_tests": False},
            "verinoda_retrieve": {"max_items": 10, "max_chars": 6000},
            "verinoda_retrieve_text": {"max_items": 10, "max_chars": 6000, "render_chars": 6000,
                                        "call": "retrieval.render_text(retrieve(g, q, Budget(10, 6000)), 6000)"},
            "sweep": {"tokens": sweep, "approaches": [f"{b}@{t}" for b in ap.SWEEP_BASES for t in sweep],
                      "rule": "context cap = 4 x tokens characters for every approach; Graphify is given "
                              "ceil(4 x tokens / 3) of its own 3-chars-per-token budget, raw char_cap = 4 x tokens, "
                              "retrieve_text Budget(10, 4 x tokens) rendered at 4 x tokens chars"} if sweep else None,
            "pinpoint_max_lines": mx.PINPOINT_MAX_LINES,
        },
        "token_count_method": mx.token_method(),
        "llm": {"enabled": llm_on, "status": llm_reason, "model": model if llm_on else None,
                "prices_usd_per_mtok": llmmod.PRICES.get(model) if llm_on else None,
                "price_source": llmmod.PRICE_SOURCE if llm_on else None},
        "workdir": str(base), "workdir_kept": keep_workdir,
    }
    store = None
    try:
        say(f"copying corpus from {repo} to {ra_root}")
        corpus = ap.prepare_workdir(repo, ra_root, include=corpus_spec.get("include"),
                                    exclude=corpus_spec.get("exclude"), commit=pin)
        res["corpus"] = {k: v for k, v in corpus.items() if k != "files"}
        gold = mx.validate_gold(ra_root, qset["questions"], corpus_files=corpus["files"])
        res["gold_validation"] = gold
        if not gold["ok"]:
            res["status"] = "gold_invalid"
            return res
        approaches = ["raw", "graphify_vendored"]
        if graphify_cmd:
            if gcmd is None:
                res["graphify_cli_status"] = f"not run: {graphify_cmd!r} not found"
            else:
                import shutil

                shutil.copytree(ra_root, cli_root)
                approaches.append("graphify_cli")
                res["graphify_cli_status"] = "run"
        else:
            res["graphify_cli_status"] = "not run: --graphify-cmd not given"
        approaches += ["verinoda_analyze", "verinoda_retrieve", "verinoda_retrieve_text"]
        have_cli = "graphify_cli" in approaches
        points = [f"{b}@{t}" for b in ap.SWEEP_BASES for t in sweep if b in approaches]
        approaches[:] = points if sweep_only else approaches + points
        res["approaches"] = approaches
        res["params"]["sweep_only"] = bool(sweep_only)
        res["environment"] = environment(ap.graphify_cli_version(gcmd, base) if have_cli else None,
                                         gcmd if have_cli else None)

        # -- index cost (one-off), separate from query cost -----------------------------
        from verinoda import workflow
        from verinoda.paths import index_dir
        from verinoda.store import open_store

        workflow.init(ra_root)
        store = open_store(ra_root)
        say("verinoda scan (cold)")
        t0 = time.perf_counter()
        s1 = workflow.scan(store, ra_root)
        cold = time.perf_counter() - t0
        say("verinoda scan (warm)")
        t0 = time.perf_counter()
        s2 = workflow.scan(store, ra_root)
        warm = time.perf_counter() - t0
        index = {
            "raw": {"note": "no index; every query greps and reads the working tree"},
            "verinoda": {"cold_seconds": round(cold, 3), "warm_seconds": round(warm, 3),
                          "nodes": s1["graph"]["nodes"], "edges": s1["graph"]["edges"],
                          "warm_nodes": s2["graph"]["nodes"], "warm_edges": s2["graph"]["edges"],
                          "cache_files": ap.count_files(index_dir(ra_root) / "cache"),
                          "what": "workflow.scan = Graphify-derived AST index (in-process) + snapshot (file hashes) "
                                  "+ stale-claim check; warm = same tree again with the AST cache present",
                          "used_by": ["graphify_vendored", "verinoda_analyze", "verinoda_retrieve",
                                      "verinoda_retrieve_text"]},
        }
        if have_cli:
            say("graphify update . (cold)")
            c1 = ap.graphify_cli_update(gcmd, cli_root)
            say("graphify update . (warm)")
            c2 = ap.graphify_cli_update(gcmd, cli_root)
            index["graphify_cli"] = {"cold": c1, "warm": c2,
                                     "what": "`graphify update .` subprocess (AST only, no LLM); includes interpreter "
                                             "start-up; warm = same tree again with graphify-out/cache present"}
            if not c1["ok"]:
                approaches[:] = [a for a in approaches if ap.split_approach(a)[0] != "graphify_cli"]
                res["graphify_cli_status"] = f"not run: graphify update failed: {c1.get('error') or c1.get('stderr')}"
        res["index"] = index

        # -- queries ------------------------------------------------------------------------
        qrows = []
        for q in qset["questions"]:
            qrows.append({"id": q["id"], "category": q.get("category"), "question": q["question"],
                          "facts": len(q["facts"]), "negatives": len(q.get("negatives", [])),
                          "approaches": {a: {"runs": []} for a in approaches}})
        first: dict[tuple[str, str], tuple[str, dict]] = {}
        for rnd in range(repeat):
            kind = "cold" if rnd == 0 else "warm"
            for q, row in zip(qset["questions"], qrows):
                for a in approaches:
                    say(f"pass {rnd + 1}/{repeat} {q['id']} {a}")
                    slot = row["approaches"][a]
                    t0 = time.perf_counter()
                    try:
                        text, meta = _call(a, q["question"], ra_root, cli_root, gcmd, store)
                    except Exception as exc:  # keep going; the failure is part of the result
                        slot.setdefault("error", f"{type(exc).__name__}: {str(exc)[:300]}")
                        slot["runs"].append({"run": rnd + 1, "kind": kind, "seconds": round(time.perf_counter() - t0, 3),
                                             "error": True})
                        continue
                    dt = time.perf_counter() - t0
                    run = {"run": rnd + 1, "kind": kind, "seconds": round(dt, 4), "chars": len(text), "sha256": _sha(text)}
                    if a == "verinoda_analyze":
                        run.update(claims=len(meta["claims"]), claims_reused=meta["claims_reused"],
                                   claims_new=meta["claims_new"], internal_tool_calls=meta["usage"]["tool_calls"],
                                   budget_exhausted=meta["usage"]["exhausted"])
                    slot["runs"].append(run)
                    if (q["id"], a) not in first:
                        first[(q["id"], a)] = (text, meta)

        # -- scoring (pass-1 text) -------------------------------------------------------------
        for q, row in zip(qset["questions"], qrows):
            for a in approaches:
                slot = row["approaches"][a]
                if (q["id"], a) not in first:
                    continue
                text, meta = first[(q["id"], a)]
                slot["score"] = score(a, q, text, meta, ra_root)
                slot["context_changed_on_warm"] = len({r.get("sha256") for r in slot["runs"] if r.get("sha256")}) > 1
                slot["_text"] = text
        # -- optional model in the loop -----------------------------------------------------------
        for q, row in zip(qset["questions"], qrows):
            for a in approaches:
                slot = row["approaches"][a]
                if "_text" not in slot:
                    continue
                if llm_on:
                    say(f"model {q['id']} {a}")
                    m = llmmod.ask(slot["_text"], q["question"], model=model)
                    if m.get("status") == "measured":
                        m["answer_facts"] = mx.score_facts(q["facts"], m["answer"])
                        m["answer"] = m["answer"][:4000]
                    slot["model"] = m
                else:
                    slot["model"] = {"status": llm_reason}
        res["questions"] = qrows
        res["summary"] = summarize(res)
        res["sweep"] = sweep_summary(res)
        res["not_measured"] = _not_measured(res, llm_on, llm_reason)
        res["status"] = "ok"
        if out:
            _write(res, Path(out))
        return res
    finally:
        for row in res.get("questions", []):
            for slot in row["approaches"].values():
                slot.pop("_text", None)
        if store is not None:
            store.close()
        if not keep_workdir:
            ap.remove_tree(base)


def _call(a: str, question: str, ra_root: Path, cli_root: Path, gcmd: str | None, store) -> tuple[str, dict]:
    base, tokens = ap.split_approach(a)
    if tokens is not None:
        chars = ap.sweep_chars(tokens)
        if base == "raw":
            text, meta = ap.raw_context(ra_root, question, char_cap=chars)
        elif base == "graphify_vendored":
            text, meta = ap.graphify_vendored_context(ra_root, question, budget=ap.graphify_budget(tokens))
        elif base == "graphify_cli":
            text, meta = ap.graphify_cli_context(gcmd, cli_root, question, budget=ap.graphify_budget(tokens))
        else:
            text, meta = ap.verinoda_retrieve_text(ra_root, question, max_chars=chars)
        return text, {**meta, "sweep": {"tokens": tokens, "char_cap": chars}}
    if a == "raw":
        return ap.raw_context(ra_root, question)
    if a == "graphify_vendored":
        return ap.graphify_vendored_context(ra_root, question)
    if a == "graphify_cli":
        return ap.graphify_cli_context(gcmd, cli_root, question)
    if a == "verinoda_analyze":
        return ap.verinoda_analyze(store, ra_root, question)
    if a == "verinoda_retrieve":
        return ap.verinoda_retrieve(ra_root, question)
    if a == "verinoda_retrieve_text":
        return ap.verinoda_retrieve_text(ra_root, question)
    raise ValueError(a)


def _not_measured(res: dict, llm_on: bool, llm_reason: str) -> list[str]:
    items = []
    if not llm_on:
        items.append(f"model-in-the-loop answer accuracy, model tokens and model cost - {llm_reason}")
    if res.get("graphify_cli_status", "").startswith("not run"):
        items.append(f"real upstream Graphify CLI baseline - {res['graphify_cli_status']}")
    items += [
        "runtime correctness of answers (only presence of gold facts in the delivered context is scored)",
        "OS file-cache effects are not separated from process warm-up in cold vs warm timings",
        "memory use",
    ]
    return items


def _write(res: dict, out: Path) -> None:
    """Results JSON at ``out``; delivered contexts under ``<out dir>/raw/<out stem>/``.

    Both are written with machine-specific paths (home, temp dir, checkout
    locations) replaced by placeholders (:mod:`verinoda.benchmark.sanitize`),
    so committed results carry no user or machine paths. The returned ``res``
    keeps the real paths. Scores and ``sha256`` are of the text as delivered;
    a context that needed sanitising is flagged ``score.context_sanitized``.
    """
    from verinoda.benchmark.sanitize import for_result, used_placeholders

    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    raw_dir = out.parent / "raw" / out.stem
    san = for_result(res)
    for row in res.get("questions", []):
        for a, slot in row["approaches"].items():
            text = slot.get("_text")
            if text is None:
                continue
            p = raw_dir / row["id"] / f"{a}.txt"
            p.parent.mkdir(parents=True, exist_ok=True)
            clean = san.text(text)
            p.write_text(clean, encoding="utf-8", newline="\n")
            if clean != text:
                slot["score"]["context_sanitized"] = True
            try:
                slot["score"]["context_file"] = p.relative_to(out.parent).as_posix()
            except ValueError:
                slot["score"]["context_file"] = str(p)
    body = {k: v for k, v in res.items()}
    body["questions"] = [{**row, "approaches": {a: {k: v for k, v in s.items() if k != "_text"}
                                                for a, s in row["approaches"].items()}}
                         for row in res.get("questions", [])]
    body = san.obj(body)
    body["paths_sanitized"] = {"placeholders": used_placeholders(body),
                               "rule": "absolute home / temp / checkout paths replaced when written; see "
                                       "verinoda/benchmark/sanitize.py"}
    out.write_text(json.dumps(body, indent=1, ensure_ascii=False, default=str) + "\n", encoding="utf-8", newline="\n")
