"""Human renderings of a benchmark result: terminal summary and Markdown tables."""

from __future__ import annotations

LABELS = {
    "raw": "raw grep+read",
    "graphify_vendored": "Graphify query (vendored, depth 3)",
    "graphify_cli": "Graphify CLI (upstream, depth 2)",
    "repoatlas_analyze": "RepoAtlas analyze",
    "repoatlas_retrieve": "RepoAtlas retrieve",
}


def _f(x, nd: int = 2) -> str:
    if x is None:
        return "-"
    if isinstance(x, float):
        return f"{x:.{nd}f}"
    return str(x)


def render(res: dict) -> None:
    """Compact terminal rendering (``repoatlas benchmark run`` without --json)."""
    print(f"benchmark set {res['set']['name']} ({res['set']['questions']} questions)  status={res.get('status')}")
    gv = res.get("gold_validation") or {}
    print(f"gold facts re-verified against the corpus: {gv.get('facts_checked')} checked, "
          f"{len(gv.get('failures', []))} failure(s)")
    for f in gv.get("failures", [])[:10]:
        print(f"  GOLD FAIL {f}")
    if res.get("status") != "ok":
        return
    print(f"tokens: {res['token_count_method']};  model: {res['llm']['status']}")
    print(f"graphify CLI: {res.get('graphify_cli_status')}")
    print("\nindex (one-off):")
    for line in index_lines(res):
        print("  " + line)
    print()
    for line in summary_table(res).splitlines():
        print(line)
    print("\nfacts found per question (found/total):")
    for line in per_question_table(res).splitlines():
        print(line)
    s = res["summary"].get("repoatlas_analyze", {})
    if s.get("claims"):
        c = s["claims"]
        print(f"\nRepoAtlas claims: {c['total']} ({', '.join(f'{k}={v}' for k, v in sorted(c['by_status'].items()))}); "
              f"verified share {_f(c['verified_share'])}; weak/unknown {c['weak_or_unknown']} "
              f"(unlabelled {c['weak_or_unknown_unlabelled']}); negative facts stated as findings "
              f"{_negatives_as_findings(res)} (in {c['negatives_presented_as_findings']} claim(s)); "
              f"unknowns reported {c['unknowns']}; relation claims labelled "
              f"contradicted {c['contradicted_relations']}, of which the cited line does name the target "
              f"{c['contradicted_but_cited_line_names_target']} (via import alias "
              f"{c['contradicted_named_via_import_alias']})")
        k = s["cache"]
        print(f"claim reuse: warm {k['warm_reused']}/{k['warm_claims']}, cold {k['cold_reused']}/{k['cold_claims']} "
              f"({k['method']})")
    print("\nnot measured:")
    for n in res.get("not_measured", []):
        print(f"  - {n}")


FINDING = {"observed", "experiment_verified", "statically_verified", "primary_source_verified", "strong_inference"}


def _negatives_as_findings(res: dict) -> int:
    """Distinct negative facts that some RepoAtlas analyze claim with a *finding* status states.

    ``summary.repoatlas_analyze.claims.negatives_presented_as_findings`` counts
    matching claims; one negative fact can be matched by several claims.
    """
    n = 0
    for q in res.get("questions", []):
        sc = (q.get("approaches", {}).get("repoatlas_analyze") or {}).get("score") or {}
        for neg in (sc.get("negatives") or {}).get("matched", []):
            if any(m.get("status") in FINDING for m in neg.get("matched", [])):
                n += 1
    return n


def index_lines(res: dict) -> list[str]:
    idx = res.get("index", {})
    out = []
    r = idx.get("repoatlas")
    if r:
        out.append(f"RepoAtlas scan: cold {_f(r['cold_seconds'])} s, warm {_f(r['warm_seconds'])} s; "
                   f"{r['nodes']} nodes / {r['edges']} edges; {r['cache_files']} cache files")
    g = idx.get("graphify_cli")
    if g:
        c, w = g["cold"], g["warm"]
        out.append(f"graphify update .: cold {_f(c['seconds'])} s, warm {_f(w['seconds'])} s; "
                   f"{c.get('nodes')} nodes / {c.get('edges')} edges; cache files {c.get('cache_files')} -> "
                   f"{w.get('cache_files')}")
    return out


def summary_table(res: dict) -> str:
    rows = [("approach", "facts found", "via locator", "pinpointed", "all-facts Qs", "tokens mean", "tokens max",
             "locators/Q", "tool calls/Q", "cold s (median)", "warm s (median)", "neg. matched",
             "cited line lacks target")]
    for a in res["approaches"]:
        s = res["summary"].get(a, {})
        if "facts_total" not in s:
            rows.append((LABELS.get(a, a), s.get("status", "-"), *["-"] * 11))
            continue
        rows.append((
            LABELS.get(a, a), f"{s['facts_found']}/{s['facts_total']}", str(s["facts_found_via_locator"]),
            str(s["facts_pinpointed"]),
            f"{s['questions_all_facts']}/{s['questions']}", _f(s["tokens_mean"], 0), str(s["tokens_max"]),
            _f(s["locators_distinct_mean"], 1), _f(s["agent_tool_calls_mean"], 1),
            _f(s["seconds_cold_median"], 3), _f(s["seconds_warm_median"], 3),
            f"{s['negatives_matched']}/{s['negatives_checked']}" if s.get("assertions_scored", True) else "n/a",
            f"{s['relations_cited_line_missing_target']}/{s['relations_checked']}"
            if s.get("assertions_scored", True) else "n/a",
        ))
    return md_table(rows)


def per_question_table(res: dict) -> str:
    rows = [("question", "category", *[LABELS.get(a, a) for a in res["approaches"]])]
    for q in res["questions"]:
        cells = []
        for a in res["approaches"]:
            slot = q["approaches"].get(a, {})
            sc = slot.get("score")
            if not sc:
                cells.append("error")
                continue
            f = sc["facts"]
            cells.append(f"{f['found_n']}/{f['total']} ({sc['tokens']} tok)")
        rows.append((f"{q['id']} {q['question']}", q.get("category") or "", *cells))
    return md_table(rows)


def timing_table(res: dict) -> str:
    rows = [("approach", "cold total s", "warm total s", "cold median s", "warm median s")]
    for a in res["approaches"]:
        s = res["summary"].get(a, {})
        if "facts_total" not in s:
            continue
        rows.append((LABELS.get(a, a), _f(s["seconds_cold_total"], 3), _f(s["seconds_warm_total"], 3),
                     _f(s["seconds_cold_median"], 3), _f(s["seconds_warm_median"], 3)))
    return md_table(rows)


def md_table(rows: list[tuple]) -> str:
    head, *body = rows
    lines = ["| " + " | ".join(head) + " |", "|" + "|".join("---" for _ in head) + "|"]
    lines += ["| " + " | ".join(str(c).replace("|", "\\|") for c in r) + " |" for r in body]
    return "\n".join(lines)


def markdown(res: dict) -> str:
    """All measured tables for docs/BENCHMARKS.md."""
    parts = [f"### Set `{res['set']['name']}` ({res['set']['questions']} questions)", ""]
    env = res.get("environment", {})
    parts.append(f"Measured {env.get('date_utc')} on {env.get('os')} ({env.get('machine')}, {env.get('cpu_count')} "
                 f"logical CPUs), Python {env.get('python')}, repoatlas {env.get('repoatlas_version')}, vendored "
                 f"Graphify {str(env.get('vendored_graphify_commit'))[:12]}"
                 + (f", upstream CLI `{env['graphify_cli']['version']}`" if env.get("graphify_cli") else "")
                 + f". Corpus: {res['corpus']['file_count']} files, {res['corpus']['bytes']} bytes. "
                 f"Repeat = {res['params']['repeat']}. Token counts: {res['token_count_method']}.")
    parts += ["", "Index (one-off, not included in per-query times):", ""]
    parts += [f"- {l}" for l in index_lines(res)]
    parts += ["", summary_table(res), "", "Per-query wall time:", "", timing_table(res), "",
              "Facts found per question (tokens delivered):", "", per_question_table(res), ""]
    s = res["summary"].get("repoatlas_analyze", {})
    if s.get("claims"):
        c, k = s["claims"], s["cache"]
        parts.append(f"RepoAtlas claims: {c['total']} total "
                     f"({', '.join(f'{kk}={v}' for kk, v in sorted(c['by_status'].items()))}); verified share "
                     f"{_f(c['verified_share'])}, inference share {_f(c['inference_share'])}; weak/unknown "
                     f"{c['weak_or_unknown']} (shown without their status label: {c['weak_or_unknown_unlabelled']}); "
                     f"negative facts stated as findings: {_negatives_as_findings(res)} "
                     f"(in {c['negatives_presented_as_findings']} claim(s)); unknowns "
                     f"reported: {c['unknowns']}; relation claims labelled contradicted: {c['contradicted_relations']}, "
                     f"of which the cited line does name the target: {c['contradicted_but_cited_line_names_target']} "
                     f"(via an import alias: {c['contradicted_named_via_import_alias']}). "
                     f"Claim reuse: warm {k['warm_reused']}/{k['warm_claims']}, "
                     f"cold {k['cold_reused']}/{k['cold_claims']}.")
    return "\n".join(parts)
