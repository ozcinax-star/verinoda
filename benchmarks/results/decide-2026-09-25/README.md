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
