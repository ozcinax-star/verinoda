# mod_live on Verinoda's own source, quiet machine (2026-10-02)

`benchmarks/mod_live/run.py --corpus <git archive of 5999912> --runs 2 --steps comment,body,batch:20,add,del`: two
twins of 2,876 files (409 Python files the steps edit), one updated with `update --fast` (the verinoda-live mod's
path), one with a plain `update`. Nothing else ran on the machine (16 CPUs). `mod-live-self-2026-10-02/` is the same
run under heavy load (ContextBench and agent sessions alongside); its ratios agree, its seconds are inflated.

| step | `--fast` (median) | plain `update` | speed-up | graph caught up after | new symbol by text now | by `symbol:` now |
|---|---|---|---|---|---|---|
| comment | 20.8 s | 153.5 s | 7.4x | 182 s | - | - |
| body | 19.5 s | 131.2 s | 6.8x | 155 s | 2/2 | 0/2 (graph pending) |
| batch:20 | 25.1 s | 154.4 s | 6.2x | 174 s | 2/2 | 0/2 (graph pending) |
| add | 19.7 s | 141.9 s | 7.2x | 160 s | **0/2** | 0/2 (graph pending) |
| del | 20.5 s | 147.2 s | 7.2x | 179 s (1 of 2 never) | - | - |

No command failed. Every graph that caught up held the new symbol; every deleted one was gone.

## Two findings

**1. After `--fast`, a function in a newly added file can lose to similar names (reproduced).** The `add` step's
function `modlive_add_r0` is not in the top 5 of a plain-text `query modlive_add_r0`; it is 8th of 13, score 0.959,
under `modlive_body_r0_0` and `modlive_batch_20_r0_*` (1.35: partial name match `modliv`, `r0` plus a graph
bonus) added by the earlier steps. Until the graph build ends the new file is one `data` unit, so its exact
identifier match (`why`: "text: 'add', 'modliv', 'modlive_add_r0', 'r0'") gets no name boost; after a full update
the function is a symbol and ranks first. With a name no other symbol shares (`modlive_probe_fn`, `modlive_add_r7`)
the new file ranks first, which is why earlier manual checks passed. Real-code shape: `parse_config_v2` added next to
`parse_config_*`. A likely fix is in retrieval: treat an exact identifier defined in a not-yet-graphed file's text
like a named symbol. **Fixed** on this branch (`search_index.py`: a definition the question names in a source file
the graph lacks gets the name boost and ranks above units that only share words with it), with a test
(`tests/test_update_fast.py`); the eight sets are unchanged fact by fact (`compare-2026-10-02/after-fix/`).

**2. One background graph build never completed (intermittent).** `background_update.log` holds 9 completions for
10 `--fast` runs; the missing one is run 1's `del`, whose wait hit the harness's 10-minute limit and left one change
for the following plain `update`. The same symptom appeared once in an earlier manual replay under load. The log
records completions only, so whether the spawn failed, the child exited early or it hung is not known from this
run. Found while instrumenting it: on Windows the log handed to the child as its stdout was opened `"ab"`, which
appends only for the process that opened it, so the child wrote from its inherited position over lines added after
it started (shown in a test). The log is now opened with true append access for every writer, and every start or
refusal is logged with its time and pid. A 12-cycle add/del repeat is in `bg_repro` (see the branch notes); the
missing build did not recur in its first cycles.

**Which code these runs measured.** The harness ran `python -m verinoda` with the corpus as its working directory,
and this corpus is Verinoda's own source: Python put the corpus first on `sys.path`, so every command here (and
the 12-cycle `bg_repro`, whose 24 background builds all caught up in 101-152 s) ran the corpus's code, 5999912, not
the build installed for the run. The seconds and ratios above are therefore 5999912's, which is what the table
claims; the two fixes above were not exercised by these runs (the ranking fix was measured separately on the eight
sets, the log fix in a test and an in-process replay where the start line survived a 2-minute build). The harness
now runs `python -P -m verinoda`, which keeps the working directory off `sys.path` (with a test). On
`mod-live-2026-10-02/` (the bundled example corpus, no `verinoda/` package) the installed build ran.
