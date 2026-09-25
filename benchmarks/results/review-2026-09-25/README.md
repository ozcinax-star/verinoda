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

## First review round (`dev-review-round1.json`, `heldout-review-round1.json`)

Two reviewers reported 41 findings (11 high, 20 medium, 10 low) from their own changes: scripted repros on
small projects, and 47 changes they labelled on the example copies and the Verinoda clone before running the
review. Each finding was reproduced on commit 8e2cc3b and fixed with a regression test in
`tests/test_review.py` (39 new tests; every one fails on 8e2cc3b), docs/DESIGN.md section 8.5 lists them. Both
splits were then run again on fresh indexed base copies (the fixtures and their gold unchanged; the held-out
split is no longer clean for these rules):

| split | precision (>= strong_inference) | recall | must-say-unknown | changed symbols exact | static test reach | `no_test_reaches` gold | gold dependents | gold lines in `read_first` |
|---|---|---|---|---|---|---|---|---|
| dev (in-sample) | 67/73 = 0.92 | 62/62 | 9/9 | 36/36 | 22/22 | 1/4 | 19/19 | 62/62 |
| held-out | 22/27 = 0.81 | 18/18 | 2/2 | 10/11 | - | - | 3/3 | 18/18 |

The false positives are the same as after the builder's fixes (the six glow_mod entry points on dev; HV1's four
value flows and one entry point on held-out). `no_test_reaches` 1/4: the gold says "no test reaches" for three
symbols with no static caller (O14's unused new function, F01's packet handler, F02's ticker-registered method);
the review now puts such symbols under `tests.reach_unknown` - their tests' reach is unknown, not zero (the
reviewers' finding: `verinoda/cli.py::cmd_map` is run by tests through a subprocess and was listed as reached by
no test). The gold was not changed. Time with the graph loaded (median / max; other agents' test suites were
running on the machine): orders_app 0.19 / 0.33 s, forge_mod 0.24 / 0.41 s, glow_mod 0.24 / 0.27 s, the
380-file copy 1.71 / 1.87 s on dev (V03 5.0 s before this round's caches) and 3.7 / 6.0 s on held-out (HV1, 61
dependents); with the graph in memory 0.63 s median on the copy.

## Second review round (`dev-review-round2.json`, `heldout-review-round2.json`)

A second reviewer reported 20 findings (7 high, 8 medium, 5 low) from scripted repros on small projects and the
example copies, run against 42d9b41 (and against 8e2cc3b / ed1c891 to tell regressions of the first round apart).
Each was reproduced on 42d9b41 and fixed with a regression test in `tests/test_review.py` (section "review round
2": 17 new tests and 2 extended ones, all failing on 42d9b41); docs/DESIGN.md section 8.6 lists them. The first
held-out run after the fixes showed one duplicate the fixes introduced (HV1: a changed `subprocess.run(...)` call
and the `shell=True` added to it reported twice); after that fix both splits were run again on base copies
indexed in the same session (fixtures and gold unchanged; the held-out split is not clean for these rules):

| split | precision (>= strong_inference) | recall | must-say-unknown | changed symbols exact | static test reach | `no_test_reaches` gold | gold dependents | gold lines in `read_first` |
|---|---|---|---|---|---|---|---|---|
| dev (in-sample) | 67/73 = 0.92 | 62/62 | 9/9 | 36/36 | 22/22 | 1/4 | 19/19 | 62/62 |
| held-out | 22/27 = 0.81 | 18/18 | 2/2 | 10/11 | - | - | 3/3 | 18/18 |

Unchanged from the first round: the fixtures hold none of the reviewer's cases. Time: this run shared the
machine with other agents' test suites (dev: orders_app 0.39 s, forge_mod 0.62 s, glow_mod 0.40 s median; the copy
7.8 s median on dev and 6.7 s on held-out, mostly graph loading under load; two earlier runs of the same code gave
0.23-0.27 s and 4.8-5.2 s on dev). An interleaved A/B on the same base copies (graph loaded each
time, three rounds, the median of three reviews after a warm-up) gives V01-V03 1.19-1.25 s at 42d9b41 and
1.32-1.41 s after the round (+9 to +13 %); the examples' fixtures O03, F02, G05 0.159 / 0.154 / 0.165 s and 0.160 /
0.169 / 0.182 s.
