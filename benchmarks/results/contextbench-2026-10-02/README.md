# ContextBench rerun, 2026-10-02 (partial: 34 of 80 instances)

The pre-registered no-model comparison of 2026-09-28 (`benchmarks/contextbench/DESIGN.md`) run again with the
current tools: Verinoda 5999912 (was 3f0e72c) and Graphify 0.9.73 (was 0.9.69); same dataset revision (sha256
verified), same evaluator commit (1436c28), same sample (rebuilt offline from the seed and checked identical to the
pre-registered table; `gh` was not available, so `sample.json`'s sizes come from the 2026-09-28 run's `checkout_mb`
and are used only for scheduling). BM25 and the arms' commands are unchanged.

**Stopped after 34 of 80 instances** by the runner's two-hour limit (the harness is resumable: `runs.jsonl`). The
runner takes the largest checkouts first, so the 34 are the heavy end of the sample, not a stratified subset; every
language is present (c 5, cpp 6, go 1, java 6, javascript 4, python 9, rust 1, typescript 2). Indexing ran beside
other benchmarks, so its times are under load.

## Same 34 instances, old run vs new (`compare.py` -> `compare.md`, `compare.json`)

Cited view, first 6,000 characters, as in 2026-09-28's primary table:

| approach | file recall per instance | file recall micro | cited span F1 | paired (file recall): better / same / worse |
|---|---|---|---|---|
| Verinoda query | 0.578 -> 0.568 | 0.484 -> 0.476 | 0.081 -> 0.083 | 0 / 33 / 1 |
| Verinoda analyze | **0.390 -> 0.470** | **0.315 -> 0.379** | 0.123 -> 0.127 | **8 / 26 / 0** |
| Graphify query | 0.376 -> 0.378 | 0.266 -> 0.274 | 0.005 -> 0.005 | 1 / 33 / 0 |
| BM25 (control) | 0.348 -> 0.348 | 0.266 -> 0.266 | 0.048 -> 0.047 | 0 / 34 / 0 |

- The control is unchanged on all 34 instances: the rerun reproduces the harness.
- Verinoda's analyze names the gold files more often on 8 instances and less often on none: the fixes made after
  this benchmark found analyze's issue-shaped-question defects (D67) show here.
- Verinoda's query and Graphify are where they were; query still leads at the same budget (0.568 against 0.378).

Files: `summary.json`, `scores.json`, `aggregate.txt` (the harness's own aggregation of the 34), `runs.jsonl`
(per-instance timings and statuses), `sample.json`. To finish the 80, rerun the harness from its workspace: it skips
the instances already marked `ok`.
