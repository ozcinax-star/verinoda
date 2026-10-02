# ContextBench rerun, 2026-10-02 (partial: 40 of 80 instances)

The pre-registered no-model comparison of 2026-09-28 (`benchmarks/contextbench/DESIGN.md`) run again with the
current tools: Verinoda 5999912 (was 3f0e72c) and Graphify 0.9.73 (was 0.9.69); same dataset revision (sha256
verified), same evaluator commit (1436c28), same sample (rebuilt offline from the seed and checked identical to the
pre-registered table; `gh` was not available, so `sample.json`'s sizes come from the 2026-09-28 run's `checkout_mb`
and are used only for scheduling). BM25 and the arms' commands are unchanged.

**Stopped after 40 of 80 instances**, twice by the runner's two-hour limit: 34 in the first run, 6 more in a resumed
run (the harness skips instances marked `ok` in `runs.jsonl`), whose next instances were Material-UI and VS Code
checkouts that took 25-33 minutes each to index under load. The runner takes the largest checkouts first, so the 40
are the heavy end of the sample, not a stratified subset; every language is present (c 7, cpp 6, go 1, java 7,
javascript 5, python 9, rust 1, typescript 4). Indexing ran beside other benchmarks, so its times are under load.

## Same 40 instances, old run vs new (`compare.py` -> `compare.md`, `compare.json`)

Cited view, first 6,000 characters, as in 2026-09-28's primary table:

| approach | file recall per instance | file recall micro | cited span F1 | paired (file recall): better / same / worse |
|---|---|---|---|---|
| Verinoda query | 0.561 -> 0.553 | 0.420 -> 0.414 | 0.066 -> 0.064 | 0 / 39 / 1 |
| Verinoda analyze | **0.405 -> 0.465** | **0.293 -> 0.338** | 0.103 -> 0.111 | **8 / 31 / 1** |
| Graphify query | 0.355 -> 0.356 | 0.242 -> 0.248 | 0.004 -> 0.004 | 1 / 39 / 0 |
| BM25 (control) | 0.321 -> 0.321 | 0.217 -> 0.217 | 0.033 -> 0.032 | 0 / 40 / 0 |

- The control is unchanged on all 40 instances: the rerun reproduces the harness.
- Verinoda's analyze names the gold files more often on 8 instances (all 8 among the first 34) and less often on
  one of the 6 added: `mui__material-ui-39196` (TypeScript), 1 of 3 gold files before, 0 now. The fixes made after
  this benchmark found analyze's issue-shaped-question defects (D67) show here.
- Verinoda's query and Graphify are where they were; query still leads at the same budget (0.553 against 0.356).

Files: `summary.json`, `scores.json`, `aggregate.txt` (the harness's own aggregation of the 40), `runs.jsonl`
(per-instance timings and statuses), `sample.json`. To finish the 80, rerun the harness from its workspace: it skips
the instances already marked `ok`.
