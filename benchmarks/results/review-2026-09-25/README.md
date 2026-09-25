# Change review (`verinoda review`, docs/DESIGN.md D35), 2026-09-25

Measurements of the review on the labelled change fixtures of `benchmarks/review_fixtures/` (36 dev, 11
held-out; the fixtures, their gold and the rules have one author, the builder). The gold was written and
hashed before any rule existed (commit 50d43a5); one amendment, before the first run, is recorded in
`MANIFEST.json` (G05). The harness is `verinoda benchmark review-eval` (`verinoda/benchmark/review_eval.py`):
each fixture is applied to a copy of an indexed git copy of its project (the examples, and a clone of a
380-file copy of Verinoda's repository at commit 184b0db for `vcopy`), the copy's git index refreshed as a
`git status` would, and reviewed; the copies were made on this Windows machine (6 cores, other agents running).

Files:

- `dev-frozen.json`: the dev split with the rules frozen at commit 3b73872 (in-sample: the rules were written
  and tuned with these fixtures in view).
- `heldout-frozen.json`: the held-out split, its first and only run with those frozen rules.
- `heldout-after-fixes.json`, `dev-after-fixes.json`: both splits after the fixes that followed the frozen run:
  the one the held-out run showed (an import statement rewritten to import more names was read as a removed
  name, HO4), and those found by reviewing Verinoda's own branch against main with the tool (JSON data files
  read as config files key by key; persistence through a callee counts writes only, not reads; findings of one
  changed function grouped per sink kind; a guard moved into a new helper function reported as moved, not
  removed; functions nested in functions are not entry points; "signature" dropped from the security words).
  The held-out set is no longer clean for these rules.
- `a8-run-tests.json`: fixture V01 (the design's A8) reviewed with `--run-tests`.

Each result has `summary` (per-concern true and false positives at strong_inference or above, recall of the
must-find items, must-say-unknown, changed symbols, test reach, dependents, gold lines inside `read_first`,
silent truncation, time per project, dependents against `map --view impact`) and `rows` (per fixture: the
score with every false positive and miss, times, and a compact copy of the review).

Scoring (the fixtures' README has the matching rule): a finding matches a gold item when the concern is the
same and the gold line (or range) holds the finding's `at` or one of its `evidence_at` lines. Precision
counts findings at strong_inference or above; a finding matching no must-find or may-find item is a false
positive. Recall counts must-find items matched by any finding.

Runs made while the rules were written (not kept as files; the dev set is in-sample for all of them):

1. First run on the examples: every must-find item found; Kotlin body changes read as signature changes
   (F05, F09: fixed - the text before the body decides); the six entry-point false positives below. The
   `vcopy` clone failed on Windows long paths (the harness now uses short directory names and
   `core.longpaths`).
2. `vcopy`: 9 value-flow false positives per guard fixture (a string reaching a callee that only *calls* a
   sink) and 5-7 s per review. Fixed: the last hop must reach the sink's own statement; per-file caches.
3. Two value-flow false positives left on `vcopy` (a value read out of a returned dict). Fixed: values inside
   containers and object fields are not followed (the design's rule).
4. Dev as frozen: 66 true / 6 false positives, recall 62/62.

Before run 1, on a two-fixture trial (O01, O02): a guard-changed false positive on O01 (`if subtotal >
threshold: return round(...)` is a branch that computes a result, not a guard). Fixed: a guard ends in
raise / throw / break / continue or returns a fixed value.

The six dev false positives are entry points that do reach the change (commands, a packet handler, a chunk
event) on three glow_mod fixtures whose gold lists no entry points (G03, G05, G06); the gold was not changed.
