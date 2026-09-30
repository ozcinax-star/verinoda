The slow AST-cache reads are not caused by OneDrive. The same slowdown happens in a copy outside OneDrive: the first time each freshly written cache entry is opened costs about 12 ms, and later opens cost under 1 ms. An `update` whose cache entries have not been opened since they were written takes about 13–16 s longer. Moving the cache out of OneDrive would save about 0 s.

For the review question: `detect.ignored_predicate` is built twice per `update` and the predicates it returns are called 0 times. Building the `gitignore=True` one alone costs about 0.67 s per `update`.

**Setup.** The copy is at `.../scratchpad/stall/self` (git archive of cc71f33, 2,588 files, the same count as the OneDrive repo's tracked files). All runs used the baseline code via PYTHONPATH, which I checked before each run with `verinoda.__file__`.

**Timings are noisy.** From about 01:47 to about 02:00 another agent's `tools/update_equality.py` was running `update`s on the same 16-core machine. That explains the slow runs marked * below. Use the in-process `load_cached` split, which ran in matched pairs, rather than those wall times.

| What | Where | Result (measured) |
|---|---|---|
| scan, wall | copy | about 235 s by timestamps (01:17:37 to 01:21:32); verinoda itself printed "in 128.35s" |
| scan | OneDrive | 124 s (given; not re-run, and I can't tell whether it is wall time or verinoda's own figure) |
| `update` after the one-function edit / after revert, cache already read | copy | 82.07 s / 82.22 s |
| `update` after the edit / after revert | OneDrive | 78 s / 79 s (given) |
| `update`, CLI, cache rewritten first (never opened) | copy | 99.2 s*, 112.1 s* |
| `update`, CLI, cache already read | copy | 90.0 s*, 104.4 s*, 94.5 s* |
| in-process `update`, cache already read | copy | wall 79.8 / 79.8 / 80.8 s; `load_cached` total 4.45 / 4.43 / 4.50 s (1,492 calls, 1,240 hits) |
| in-process `update`, cache rewritten first (never opened) | copy | wall 97.6 / 95.9 s; `load_cached` total 17.22 / 19.99 s |
| read pass over the copy's cache, 3.4 min after scan, first open | copy | 1,240 entries, 52.5 MB: 14.48 s (median 13.2 ms per entry) |
| same pass again at once | copy | 0.84 s (median 0.45 ms) |
| same pass 18–23 min later | copy | 0.84 s |
| cache copied to new files, half read at once (first open) | copy | 620 entries: 6.37 s (median 12.8 ms), then 0.43 s on the second read |
| other half left unread 19 min, then first open | copy | 620 entries: 7.13 s (median 12.7 ms), then 0.39 s on the second read |
| OneDrive `.verinoda/index/cache/ast/*` read pass, 3 times (entries 41–76 min old) | OneDrive | 1,244 entries: 0.83 s, 0.83 s, 0.90 s |
| small test files, written then read by the same process, 200 per kind, first / second open | copy area | 100 B JSON: 1.2 / 0.19 ms; 40 KB JSON: 11.3 / 0.21 ms; 400 KB JSON: 11.7 / 0.41 ms; 40 KB random `.bin`: 9.9 / 0.20 ms |
| cost to build `ignored_predicate`, 3 rounds | copy | `gitignore=False`: 2–3 ms; `gitignore=True`: 668–678 ms |
| calls to the predicates built in `update` (edit run and revert run) | copy | 2 built per `update` (gitignore False and True); 0 predicate calls in every run |
| Defender (`Get-MpComputerStatus`) | – | real-time and on-access protection on; I can't view exclusions without admin |

**Conclusion.**
- **Measured:** the slow first read happens outside OneDrive at the same size as in your profile (about 12 ms per entry, about 14.5 s for 1,240 entries). It does not wear off with age: entries left unread for 19 minutes were just as slow. It is per file, not per byte (40 KB and 400 KB cost the same), and it also hits a process reading files it has just written. A second open is always fast, and an `update` whose entries are already read behaves the same in both places (about 80 s in the copy, 78–79 s in OneDrive). The OneDrive cache, already read, reads as fast as the copy (0.83–0.90 s against 0.84 s).
- **Inferred:** the cost is real-time antivirus (Defender) checking each newly written file the first time it is opened. I did not prove this, because I didn't disable Defender or add an exclusion. It would explain the 16.5 s in your profile: that `update` was the first to open entries written by the scan before it. It is paid once per entry, so it lands on the first `update` after a scan or cache rewrite (about 13–16 s wall, about 15% here). Later `update`s only pay for the few entries they write themselves.
- **So:** moving the AST cache outside OneDrive gains about 0 s. What could help are fewer freshly written files to open (for example, packing the cache into one file, whose cost I did not measure) or an antivirus exclusion or Dev Drive on the user's side.

**Open question (review): is `detect.ignored_predicate` called?** Measured: `watch.py:1501-1507` builds it twice per `update` (both runs reported `gitignore=True` as the second build). The predicates are called 0 times in both the edit and revert runs. They are only consulted at `watch.py:865-872`, for files still in the graph and on disk but no longer collected, and a typical `update` has none. Building the `gitignore=True` predicate still costs about 0.67 s each time, which is thrown away. Building it only when first needed would save about 0.67 s per `update`; that figure is inferred from the build timing alone.

**Also found:**
- Every `update` looks up 252 files that are never in the cache (1,492 lookups, 1,240 hits), and no new entries appear afterwards. I did not check which files these are or what they cost.
- My first counting script ran under the wrong output directory because it imported `verinoda.project_index` before `verinoda.cli` sets `GRAPHIFY_OUT`. It wrote a stray `self/graphify-out/` inside the copy, which I deleted, and I discarded those numbers. Before that, the script's pool workers re-ran it and hung; I killed them.

Nothing in the repo, its `.verinoda`, or the baseline was modified. The copy's work tree is clean and its index matches the reverted tree.

Files are in `<scratch>/stall`:
- `count_driver.py`
- `readpass.py`
- `rewrite.py`
- `synth.py`
- `build_cost.py`
- `timeit.py`
- `readpass.log`
- `timings.log`
- `d1.out` to `d4.out`