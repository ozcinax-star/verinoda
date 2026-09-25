# debugloops_v1 and the failure-signature fixtures, 2026-09-25

Measurements of the debug ledger (docs/DESIGN.md D34). All of it is in-sample in the sense that
the same person wrote the sessions, the gold and the rules; the gold was written first.

## Sessions (`sessions.json`)

12 scripted debugging sessions: 8 that loop, 4 controls. Each builds a fresh git copy of an example
(`examples/orders_app`, or `examples/glow_mod` for L7), optionally makes commits (L2), applies the
setup edits (the bug, uncommitted), starts a session with the repro command and records the listed
attempts in order with `verinoda.debug` (the Python API the CLI and MCP tools call). L7 is reported
by the "agent" (Gradle logs written in the Gradle format; Gradle does not run here). C2's test fails
at random (`random.random() >= 0.5`), so its result varies from run to run.

Gold per session, fixed before the loop rules ran on any session: whether it loops, the attempt at
which the loop is established, which definitive rules are correct at which attempt (any other
definitive finding is a false positive), the correct first strategy and the true cause (file:line in
the tree at the loop). The cause lines of L6, L7 and L8 were corrected once before the first run (they
had been given for the tree before a later attempt moved them).

Scoring: definitive precision over every definitive finding that was not suspended; loop recall =
looping sessions with `stop: true` at or before the established attempt; controls with any
`stop: true`; top strategy = `strategies[0]` at the first stop; "names the cause" = the first strategy
was then run and its top result contains the cause line (the differential's first hunk, bisect's first
failing commit and its first hunk).

- `run1-first.json`: the first run. Precision 10/10, recall 7/8, 0/4 controls stopped, top strategy
  7/8, cause named 6/7. Misses: L7 (a JVM class-loader identity hash in the message made the recurring
  failure look new) and L6 (the differential ranked the agent's own edit first).
- `run2-two-fixes.json` (JVM hashes, attempt-0 hunks first by exact hunk: 11/11, 8/8, 0/4, 8/8,
  cause named 6/8 - L8 broke), `run3-line-match.json` (attempt-0 hunks matched by line: cause named
  7/8), `run4-flaky-found.json` (same scores; C2's rerun series of five passes on the tree that had
  failed was reported "stable", which led to the third fix).
- `run5-final.json`: after three fixes found on these sessions - JVM identity hashes normalised in
  messages; the differential ranks hunks that were already there at attempt 0 first (compared line by
  line; an exact-hunk version, run 2, broke L8); a rerun series on the tree that gave different results
  no longer clears flakiness (C2). Precision 11/11, recall 8/8, 0/4 controls stopped, top strategy 8/8,
  cause named 7/8 (L7: Gradle cannot run here, so the differential only prepared a copy). `debug try`
  overhead in this run, the 27 attempts Verinoda ran: median 0.08 s, max 0.11 s.
- `run6-final-code.json`: the committed code after review changes that followed run 5 (narrowing
  suspects, order-dependence, `test_edited` needing replaced or removed test lines, the human question's
  code side, the minimal-repro command). Same scores as run 5. It ran while the full product test suite
  was running on the same machine: `debug try` overhead median 0.16 s, p90 0.55 s, max 0.77 s; see
  `overhead.json` for a separate measurement (without the test suite running next to it).

- `run7-review-first-ranking.json` and `run8-review-fixes.json`: after a two-reviewer review of the
  ledger (docs/DESIGN.md section 6.5). The harness changed in one place: an agent-reported run must
  now name the command it ran, so L7's reports pass the session's command. Run 7 had a first version
  of the differential's ranking fix (cause named 5/8: L6 and L8 lost theirs); run 8 is the committed
  code: 11/11, 8/8, 0/4, 8/8, cause named 7/8 - the same findings and stops as run 6 on every attempt,
  except progress after a test edit ("unknown" now) and C2's random outcomes.

## Overhead (`overhead.json`, and `overhead-before-review.json` for the code before the review changes)

20 `debug try` attempts in a row (alternating two edits) per series, the overhead being the attempt's
wall time minus the command's own run time: orders_app (11 files) untraced and with `--trace`, and a
`git clone` of this repository (2,341 files; 2,331 before the review changes) with
`tests/test_looprules.py` as the repro. Each record
has the median, p90 and max, and the median per step (`run_s` includes copying the tree). Other agents
were running on the 6-core machine; single attempts can take seconds longer. `overhead-after-review.json`
is the same measurement on the code after the review fixes (2,342 files in the clone by then).

## Signatures (`signatures.json`)

30 log fixtures: 21 from real runs (pytest, unittest, a Python script, Java, Rust, Node on this
machine) and 9 hand-written in the formats of tools not installed here (Gradle, Maven surefire, Go,
Jest). Gold (exception type and `path::symbol` of the crash) was written from each log before the
parser was scored. First score 18/30; after format fixes found on them 30/30. Then 10 held-out real
logs were produced and labelled the same way: 8/10 on their first score, 10/10 after two fixes (so
they are no longer held out). 17 of the 30 fixtures, with machine paths replaced, are in
`tests/fixtures/failsig/` and run in the product tests (sources with a `.fixture` suffix and logs as
`.log`, so that indexing this repository does not take them for its own code). The scripts that produced the logs and the
harness that ran the sessions are not in the repository.
