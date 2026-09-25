# Decisions stay human (D33): measurements, 2026-09-25

What `decisions_v1` measured on branch p2/decide (docs/DESIGN.md section 6, docs/BENCHMARKS.md
"Update 2026-09-25: decisions"). Every case, gold item and rule below was written by the same author who
wrote the code it measures: all of it is in-sample except where a file says it was held out.

Run from the repository root with the project's Python (nothing here needs the network):

    python benchmarks/results/decide-2026-09-25/intent_eval.py --out intent_results.json
    python benchmarks/results/decide-2026-09-25/guard_mutations.py
    python benchmarks/results/decide-2026-09-25/brief_orders_app.py

The last two copy `examples/` into a temporary folder, `git init` and scan each copy; they never touch
`examples/` itself.

## Files

- `intent_written.json`: the 24 written tuning questions (12 decisions, 12 questions about code).
- `intent_heldout1.json`: 10 questions written and hashed before any decide cue existed
  (sha256 `6c5a4693a75769886c2bb92fe68704e009fb4ad05cb3b62565bbd901eea903ea`). Measured once with the
  frozen rules: precision 1.00, recall 0.40 (2 of 5). The cue rules were then widened for its three
  misses (a clause that opens with "should", "is it time to", "do we need a", "or keep"), so it is
  in-sample now.
- `intent_heldout2.json`: 10 more, written and hashed after held-out 1 was measured and before those
  rule changes (sha256 `984ba593fcced57ba2a56697cb3fb6d13aadd4290be1e39b87c5ec184d70b22e`). Measured
  once: precision 1.00, recall 0.40 (2 of 5). Its misses ("would Redis be a better fit", "geçsek mi",
  "is SQLite enough for us") were not turned into rules.
- `intent_results.json`: the current rules on all three files (written by `intent_eval.py`).
- `guard_mutations.py` / `guard_mutations.json`: guard mutations on git copies of orders_app,
  glow_mod and forge_mod: violating cases (every line marked `<<` must be VIOLATED, nothing else),
  benign ones (no VIOLATED at all; POSSIBLE allowed) and out-of-reach ones (a real violation the engine
  cannot verify: no VIOLATED, and the form must be named in the limits or a POSSIBLE finding). The
  baseline is the raw per-line regex the old critique exclusivity check ran, on the orders_app cases.
  Two cases were added after the first run and say so in their comments (V25: a bug found outside the
  set; B20: a false VIOLATED found by reading the engine).
- `brief_orders_app.py` / `brief_orders_app.json`: the decision brief on orders_app for the EN and TR
  question of the design, against its 8 gold forces and 5 gold question kinds. Evidence precision is
  checked independently: every cited line is re-read and must equal the excerpt the brief recorded.
- `fast-base.json` / `fast-step4.json`: the fast benchmark harness (Verinoda approaches only, the eight
  public sets) with integrate/0925 and with this branch after step 4; no difference in any question.
- `check-time-verinoda.json`: `decide check` with four guards (no_edge included) and a brief on a
  prepared 78-file copy of Verinoda's own code (index warm, records in memory, the brief not stored).
- `check-time-verinoda-full.json`: `decide check` with three guards (no index: no no_edge) and a brief
  on the full tree of this branch (631 .py of 2,309 files). Run 1 of each is with a cold file cache.
  These two were written by scratch scripts, not by a harness in this folder.

## After the two reviews (same day, by the fixer)

- `guard_mutations.py` / `.json` now hold 76 cases: the 54 above (their results did not change) plus 22
  forms the reviewers found (V26-V32, B21-B31, O10-O13). The new ones are in-sample for the fix: it was
  written against them.
- `intent_reviewers.json`: the reviewers' 52 questions (in-sample: the cue rules were changed against them).
- `intent_heldout3.json` (sha256 `237d7770e73a6267f1301f959117bc71a212ef3b2cd5d14e1d09d92d7855657e`):
  24 questions the fixer wrote and hashed before changing the cues, but after seeing the reviewers' lists;
  the fixer also saw its results under the builder's rules before writing the new cues, so it is not a
  clean held-out set.
- `intent_heldout4.json` (sha256 `7b3d5a5de5a495ec91ed60a5b5a28d1205dcfec9d24220012c6f64dc849b6463`):
  20 questions written and hashed before the new cue rules were written; cues that echoed its phrasings
  were removed before the run; run once with the rules frozen (`frozen_run_heldout34.json`;
  question_plan.py's sha256 at that moment began `ba8564c2`; later edits to it added only the note-only
  `may_ask_for_choice` and wrapped a line, and `intent_results.json` gives the same numbers):
  precision 0.83, recall 0.50. It is the only set here that measures questions the rules were not tuned on.
- `intent_results.json`: regenerated with the new rules on all intent files.
- `check-time-review-fixes.json`: `decide check` (3 guards) and a brief on the full tree, the code of
  35d2987 and the review fixes on the same files (a scratch script, like the two files above).
- `fast-35d2987.json` / `fast-review-fixes.json`: the fast harness (the builder's script, run with the
  code of 35d2987 and with the review fixes on the same prepared copies; the eight public sets): no
  difference in any question.
- `fast-base.json`, `fast-step4.json` and the two `*_frozen_run.txt` files were converted to LF line
  endings (no recorded hash covers them).
