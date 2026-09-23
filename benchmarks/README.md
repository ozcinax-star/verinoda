# benchmarks/

Measured outputs of the benchmark engine in `verinoda/benchmark/`. Methods,
metric definitions, tables, comparisons and discussion:
[docs/BENCHMARKS.md](../docs/BENCHMARKS.md).

```
results/<set>.json                          current measurement (round 3): index cost, per question x approach
                                            x run timings, facts found/missed and how, negative-fact matches,
                                            citation checks, claim statistics, environment, set provenance
results/raw/<set>/<question>/<approach>.txt the exact context each approach delivered (what was scored)
results/sweep/<set>.json                    budget sweep: retrieve text, Graphify (vendored and CLI) and raw
results/sweep/raw/...                       at 750 / 1500 / 3000 tokens (same character cap for all)
results/trust/staleness_graphify_300.json   staleness history replay (300 upstream Graphify commits)
results/trust/mutations.json                staleness mutation suite (examples/orders_app)
results/trust/critique_eval.json            critique evaluation on the labelled claim set (in-sample)
results/before-round3/<set>.json            the round-2 measurement, moved here unchanged
results/before-fixes/<set>.json             the round-1 measurement (05890a1, before the round-2 fixes)
```

Sets (question files in `verinoda/benchmark/questions/`, each with a
`provenance` block saying who wrote it and whether it is in-sample):
`orders_app` (examples/orders_app), `graphify_core` (upstream Graphify at commit
20a20d30), `heldout_repoatlas` (Verinoda's own source pinned at commit 7371990),
and the Turkish paraphrase sets `orders_app_tr` / `graphify_core_tr`.

Paths: result files contain no machine paths. Absolute paths are written as
`<TMP>` (system temp dir), `<CORPUS>` (the analysed repository when it lies
outside the Verinoda checkout), `<REPO>` (the Verinoda checkout) and `<HOME>`
(user home). `run --out` and the harnesses' `--out` do this when they write.
Files from older runs are cleaned with
`python -m verinoda.benchmark sanitize <file>.json`, on the machine that
produced them. Each file lists the placeholders it uses under
`paths_sanitized`.
