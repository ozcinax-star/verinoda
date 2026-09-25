# Behaviour probe measurements (docs/DESIGN.md D36, 2026-09-25)

Everything here is **in-sample**: the fixtures, their gold labels, the mutant labels and the probe have one
author. The gold labels were fixed before the probe ran on any fixture (`fixtures.py`, sha256
`4a2265ec1e0c891b5c29d5fc022a46cd08bdbe2aa25101a8158db32748ed6db3`, recorded before run 1 and stored in every
result file as `gold_sha256`); the mutant labels were written after the mutants were generated and filtered by
the tests, and before the probe ran on them (`mutant_labels.json`, sha256 in `mutants_run1.json`). The design's
B1 case (D03) had been run once during development before `fixtures.py` was written.

## Files

| file | what |
|---|---|
| `fixtures.py` | 49 hand fixtures on git copies of `examples/orders_app` plus small modules (`EXTRA`, committed as the base; the edit is the working tree): 21 behaviour changes the existing tests are meant to miss (`change`), 1 quadratic loop (`scaling`), 12 behaviour-preserving edits (`equivalent`), 9 functions the side-effect gate must refuse (`refuse`), 4 it must let run (`run`), 2 `unsupported` |
| `run_fixtures.py` | the harness: builds each fixture, runs the fixture's own tests on the working tree, probes (equivalents with seeds 0-4; the scaling fixture with and without `--scaling`), scores against the gold. `--no-hypothesis` blocks hypothesis (the fixed pseudo-random list is used); `--no-mining` is an ablation without boundary mining |
| `run1-first.json` | the first run, gold fixed, code before the review of its own results |
| `run2-fixes.json` | after two fixes found in run 1 (scaling did not skip sizes it had measured over budget: 23 s per probe; timeouts counted as differences) - scores unchanged |
| `run3-no-hypothesis.json` | the same with hypothesis blocked |
| `ablation-no-mining.json` | the 22 change fixtures without boundary mining (one seed) |
| `run4-780328a.json` | an interim run of commit 780328a (hypothesis still mixed in constants of the modules imported in the harness process) |
| `run5-final.json` | the final code, commit c0dbd1f |
| `run6-fixer.json` | after the review fixes (fixer round: threads and child processes blocked at run time, a module name taken by another module, process exits, plugin errors, float drift only on floats and as its own status `numeric_drift_only`, the gate's SQL rule on the syntax tree, `--changed` no pass when a function was not compared) |
| `mutants.py`, `mutants_survivors.json`, `mutant_labels.json`, `mutants_run1.json`, `mutants_run3-final.json` | 45 automated single-point mutants (comparison flips, and/or swap, `not` removal, `+`/`-`, `*`/`/`, integer constants +-1, float constants +1%) of 12 functions of the fixture project; 27 survive the tests; hand labels; the probe's results (first run, and the final code) |
| `mutants_run4-fixer.json` | the same 27 surviving mutants after the review fixes |
| `run7-fixer2.json`, `mutants_run5-fixer2.json` | after the second review round (finalizers, exit handlers and asyncio's socket pair at run time; SQL statements through helpers, defaults, attributes and loops in the gate; float drift only between float literals; no pass when fewer than half of the inputs returned or raised; an unparseable changed file) |

Reproduce (from the repository root, with the project's venv): `python
benchmarks/results/probe-2026-09-25/run_fixtures.py WORKDIR OUT.json` and `python mutants.py generate WORKDIR
survivors.json`, `python mutants.py probe WORKDIR survivors.json mutant_labels.json OUT.json` (from this folder).
WORKDIR receives the git copies (a scratch directory).

## Results

| run | detected (change + scaling) | of those the tests miss | differences on equivalent edits | gate right / wrong refusals | unsupported | reproduced | s per probe (median / p90 / max) |
|---|---|---|---|---|---|---|---|
| run1-first | 22/22 | 20/20 | 5/60 (E04) | 9/9 / 0/4 | 2/2 | 37/37 | 1.9 / 3.7 / 23.0 |
| run2-fixes | 22/22 | 20/20 | 5/60 (E04) | 9/9 / 0/4 | 2/2 | 37/37 | 2.4 / 3.8 / 10.6 |
| run3-no-hypothesis | 22/22 | 20/20 | 5/60 (E04) | 9/9 / 0/4 | 2/2 | 36/36 | 1.7 / 3.0 / 10.5 |
| run4-780328a | 22/22 | 20/20 | 5/60 (E04) | 9/9 / 0/4 | 2/2 | 37/37 | 2.6 / 4.0 / 10.8 |
| run5-final | 22/22 | 20/20 | 5/60 (E04) | 9/9 / 0/4 | 2/2 | 37/37 | 2.1 / 4.2 / 11.1 |
| run6-fixer | 22/22 | 20/20 | 5/60 (E04, now `numeric_drift_only`) | 9/9 / 0/4 | 2/2 | 37/37 | 2.0 / 3.8 / 11.2 |
| run7-fixer2 | 22/22 | 20/20 | 5/60 (E04, `numeric_drift_only`) | 9/9 / 0/4 | 2/2 | 37/37 | 2.1 / 3.3 / 10.9 |
| ablation-no-mining (22 change fixtures) | 19/22 (D01, D04, D11 missed) | 17/20 | - | - | - | 22/22 | 3.3 / 3.7 / 13.1 |

- E04's gold is wrong: `sum()` of floats is compensated on Python 3.12 (`sum([0.1] * 10) == 1.0`, a loop gives
  `0.9999999999999999`), so turning the generator `sum` into a loop changes results; the probe reported
  `numeric_drift` (the low-priority class) on all 5 seeds. The other 11 equivalent edits: 0 differences in 55
  runs. `fixtures.py` is left as it was when the runs were made (its sha256 is in every result file).
- D05 and D15 are caught by the fixture project's own tests (an empty-order test, a currency assertion).
- Automated mutants: 45 generated, 27 survive the tests, labelled 25 `change` and 2 `change_message_only`, none
  equivalent; the probe finds a difference in 25/25 and none in the 2 message-only ones (types are compared, not
  messages), in the first run, with the final code and after the review fixes (`mutants_run4-fixer.json`,
  median 3.1 s per probe), and after the second review round (`mutants_run5-fixer2.json`, 25/25, median 2.75 s).
