# Benchmarks: raw search vs Graphify vs Verinoda

> **Name change (2026-09-23):** the product was renamed from its working name RepoAtlas to Verinoda after these measurements. Result files under `benchmarks/results/` keep the ids they were written with (`repoatlas_*` approach ids = today's `verinoda_*`); see `docs/NAMING.md`.

This page reports a reproducible comparison of ways to give a coding agent
context for a question about a codebase, plus the two trust harnesses
(claim staleness and critique). It gives the exact metric definitions, the
measured numbers, and what was **not** measured.

Every number on this page comes from a result file under `benchmarks/results/`
(current round: `<set>.json`, `sweep/<set>.json`, `trust/*.json`; previous
round: `before-round3/<set>.json`), produced by the commands in
[Reproduce](#reproduce). Ratios quoted in the text are computed from those
numbers. No number is carried over from Graphify's published benchmarks or
from the research and track reports, and no savings factor is claimed beyond
the measured ratios.

Sections: [Update 2026-09-25: change review](#update-2026-09-25-change-review-verinoda-review-d35) · [Update 2026-09-25: debug ledger](#update-2026-09-25-debug-ledger-debugloops_v1) · [Update 2026-09-25: decisions](#update-2026-09-25-decisions-stay-human-d33) · [Name check, third review round](#update-2026-09-25-name-check-third-review-round) · [Name check, second review round](#update-2026-09-25-name-check-second-review-round) · [Name check after review](#update-2026-09-25-name-check-after-review) · [Name check 2026-09-25](#update-2026-09-25-name-existence-check-verinoda-check-d32) · [Update 2026-09-25 (truth rules)](#update-2026-09-25-truth-rules-word-overlap-never-verifies-roles-are-bound-code-names-are-not-substituted) · [Update 2026-09-25](#update-2026-09-25-analyze-keeps-what-query-found-grounded-verdicts-turkish-update-time) · [Update 2026-09-24](#update-2026-09-24-data-files-game-mods-java-calls) · [Update 2026-09-23](#update-2026-09-23-dogfooding-fixes) · [Summary](#summary) · [Results per set](#results-per-set) ·
[Before round 3 vs now](#before-round-3-vs-now) · [Budget sweep](#budget-sweep) ·
[Turkish vs English](#turkish-vs-english) · [Trust harnesses](#trust-harnesses) ·
[Discussion](#discussion) · [Not measured](#not-measured) ·
[Problems found](#problems-found-by-this-round) · [Environment](#environment-and-run-conditions) ·
[Reproduce](#reproduce) · [What is compared](#what-is-compared) ·
[Metrics](#metrics-exact-definitions) · [Question sets](#question-sets) ·
[Per-question results](#per-question-results)

## Update 2026-09-25: change review (`verinoda review`, D35)

Result files: `benchmarks/results/review-2026-09-25/` (its README lists the runs made while the rules were
written and what each changed). Fixtures: `benchmarks/review_fixtures/` - 36 dev and 11 held-out changes on git
copies of `orders_app`, `glow_mod`, `forge_mod` and a clone of a 380-file copy of Verinoda's repository, each with
must-find, may-find and must-not-flag concerns (file:line), must-say-unknown items, test reach and the
dependents a reviewer must see. The fixtures, the gold and the rules have one author (the builder); the gold was
hashed before any rule existed. The dev numbers are in-sample; the held-out set was run once, with the rules
frozen (commit 3b73872), and only guards against tuning on its answers.

| split | precision (>= strong_inference) | recall (must-find) | must-say-unknown | changed symbols exact | gold dependents listed | gold lines in `read_first` |
|---|---|---|---|---|---|---|
| dev, frozen rules (in-sample) | 66/72 = 0.92 | 62/62 | 9/9 | 36/36 | 19/19 | 62/62 |
| held-out, frozen rules (only run) | 23/29 = 0.79 | 18/18 | 2/2 | 10/11 | 3/3 | 18/18 |
| held-out after the later fixes (no longer clean) | 22/27 = 0.81 | 18/18 | 2/2 | 10/11 | 3/3 | 18/18 |
| dev after the later fixes (in-sample) | 65/71 = 0.92 | 62/62 | 9/9 | 36/36 | 19/19 | 62/62 |

The later fixes: the HO4 bug below, and six found by running the review on Verinoda's own branch against
main (a 321-definition diff): JSON data files were read as config files key by key (1,395 "changes"),
persistence through a callee now counts writes only, one changed function's findings are grouped per sink
kind, a guard moved into a new helper is `guard-moved` (weak) instead of removed, functions nested in
functions are no entry points, and "signature" is no longer a security word. That branch review took 85 s
before and 48 s after these fixes (with the test suite running on the same machine).

Per concern (dev, frozen): persistence 10/10 precision, 8/8 recall; security 12/12, 12/12; performance 3/3, 3/3;
public API 21/21, 21/21; config 5/5, 5/5; entry points 15/21, 13/13. Held-out (frozen): persistence 6/10, 3/3;
security 6/6, 6/6; performance 2/2, 2/2; config 3/3, 2/2; entry points 6/7, 5/5; public API 0/1 (no must-find).
Unknowns are 22% of the reported items on both splits (22 of 99 on dev, 11 of 49 on held-out); 5 dev and 9
held-out findings are below strong_inference (word hits, moved guards, name-only entries).

The false positives: on dev, six entry points that do reach the change (commands, a packet handler, a chunk
event) on three glow_mod fixtures whose gold lists no entry points; on held-out, the output of `snapshot.git`
carried into four stored records and one entry point on HV1 (its gold says "no persistence"), and an import
statement rewritten to import more names read as a removed name on HO4 - a bug, fixed after that run. HO4's
changed symbols also list two import statements the gold left out.

**Time per review** (without `--run-tests` and without `update`; the copy's git index refreshed as `git
status` would; median / max):

| project | graph loaded (the CLI's case) | graph kept in memory (MCP) | cold CLI process |
|---|---|---|---|
| orders_app | 0.19 / 0.19 s | 0.13 / 0.15 s | 0.66-0.75 s |
| forge_mod | 0.21 / 0.24 s | 0.16 / 0.18 s | 0.74-0.77 s |
| glow_mod | 0.20 / 0.27 s | 0.14 / 0.19 s | 0.95 s |
| 380-file Verinoda copy | 1.85 / 5.4 s | 0.78 / 1.3 s | 2.2-2.4 s |

(dev, frozen rules; the cold CLI column from separate runs on the same copies.) The design's bars, 2 s on the
examples and 6 s on the copy, hold. With a stale git index (a freshly copied checkout) git reports every file
as changed and the review reads each one: 11 s on the copy; the review does not refresh the index itself, since
that writes `.git/index`.

**Blast radius** against `verinoda map --view impact` on the same diff: fewer items on every fixture with
dependents - A1 (O01) 3 vs 11, A7 (F02) 0 vs 28 (the tick handler's caller is a method-reference registration,
reported as an unknown), A8 (V01) 7 vs the view's capped 80 - with every gold dependent listed (19/19, 3/3).
The review counts non-test dependents; the view also counts tests and files. One held-out fixture keeps more:
HV1 (`snapshot.git`) has 61 dependents against the view's capped 80.

**What to read**: every gold location lies inside the review's `read_first` (62/62 dev, 18/18 held-out) at a
median of 642 characters per dev fixture (max 3,400; the budget is 6,000). The design's baseline, `map --view
impact` plus reading the changed and affected files, also covers every gold file (62/62) but at a median of
11,530 characters; 18 of the 36 dev fixtures exceed 6,000 characters that way, and on the Verinoda copy it is
1.4-1.5 million characters (scratch measurement on the dev copies, not in the result files).

**A8 with `--run-tests`** (`a8-run-tests.json`): 19 tests selected statically, among them
`tests/test_experiments.py::test_policy_rejects_arguments_that_leave_the_copy`; the run failed (8 failed, 66
passed, 30 s); the failures in the log are the `..` cases that the removed guard handled. A1 with `--observe`:
the four tests that reach `apply_discount` at run time were reported as reached, and
`test_empty_order_rejected` (statically selected) as selected but not reaching it.

Not measured: an agent session with and without the review step; repositories of 2,000+ files; the precision of
the word heuristics on their own; Java/Kotlin test runs (Gradle is not allowlisted).

## Update 2026-09-25: debug ledger (debugloops_v1)

Result files: `benchmarks/results/debugloops-2026-09-25/` (its README says how they were produced).
Everything here is in-sample: the sessions, their gold and the loop rules have one author; the gold
was fixed before the rules ran on any session, and three fixes followed from the first runs.

**Loop detection, 12 scripted sessions** (8 looping, 4 controls, on git copies of `orders_app` and
`glow_mod`; L7 is a Gradle session reported by the "agent" because Gradle does not run here):

| run | definitive precision | loop recall (stop at or before the gold attempt) | controls stopped | top strategy correct | first strategy names the cause |
|---|---|---|---|---|---|
| 1, first | 10/10 | 7/8 | 0/4 | 7/8 | 6/7 |
| 5, after three fixes | 11/11 | 8/8 | 0/4 | 8/8 | 7/8 |
| 6, committed code (after review changes) | 11/11 | 8/8 | 0/4 | 8/8 | 7/8 |
| 7, review fixes, first ranking change | 11/11 | 8/8 | 0/4 | 8/8 | 5/8 |
| 8, review fixes as committed | 11/11 | 8/8 | 0/4 | 8/8 | 7/8 |

Runs 7 and 8 follow a review that found 25 distinct problems (docs/DESIGN.md section 7.5): false
stops, false "passed", unverified bisect ends, git-safety gaps. The fixes changed which findings
fire; on these 12 sessions every attempt's definitive and heuristic findings and stops came out the
same as in run 6 (L6's revert is now found as a code-identical revert: the tree differed from
attempt 1's only in a docstring). Two intended differences: progress after a test edit (L1 attempt 1,
L8 attempt 2) is now "unknown" instead of "improved", and C2, random by design, happened to pass all
four attempts in run 8, so flakiness first showed in its rerun series (1 of 5 passed; 5 of 9 runs of
the tree). Run 7 used the first fix of the ranking (tiers before "already
there at attempt 0" when the failure's coarse signature changed): L6 and L8 lost their cause, the
agent's own edits ranking first. Run 8 is the committed rule (code before test files, comment-only
files last, "same symptom" also when the failing tests are the same), which ranks the reviewers'
counter-example right as well. In-sample: the counter-example and the fix are known to the author.

The misses of run 1: a JVM class-loader identity hash (`'knot' @1a2b3c4d`) in the message made a
recurring Java failure look new (now normalised); the differential ranked the agent's own edit above
the cause that was already there when the symptom was recorded (hunks present at attempt 0 now come
first, matched line by line). Run 4 then showed a rerun series of five passes on a tree that had failed
reported as "stable"; a series on a tree that disagreed no longer clears flakiness, and the pass rate
counts every recorded run of the tree. Run 6 is the committed code after later review changes
(narrowing suspects, order-dependence, `test_edited` needing replaced or removed test lines); it
scored the same. The 7/8: L7's differential can only prepare a copy of the base
(Gradle is not runnable here). The heuristic rules have no gold; they fired 15 times in the looping
sessions and never on the controls.

**Failure signatures**: 30 log fixtures (21 real runs of pytest, unittest, Python, Java, Rust and
Node; 9 hand-written in the Gradle, Maven, Go and Jest formats), exact exception and `path::symbol`:
18/30 on the first score, 30/30 after format fixes found on them. 10 held-out real logs produced
after that: 8/10 on their first score (a `--tb=native` pytest section, a bare `Error:` in Node),
10/10 after two fixes. No crash on any log, and a property test feeds arbitrary text.

**`debug try` overhead** beyond the command's own run (`overhead.json`, the committed code, 20
attempts per series; `overhead-before-review.json` is the same measurement before the review changes):

| tree | median | p90 | max | command median |
|---|---|---|---|---|
| orders_app (11 files) | 0.14 s | 0.25 s | 0.49 s | 0.59 s |
| orders_app, `--trace` | 0.15 s | 0.18 s | 0.23 s | 0.63 s |
| clone of this repository (2,341 files) | 5.0 s | 6.4 s | 9.1 s | 0.66 s |

(Before the review changes: 0.12 / 0.15 / 0.16 s, 0.12 / 0.17 / 0.17 s and 4.5 / 5.9 / 7.2 s.) The
design's bar (0.3 s on the examples) holds at the median and p90; one of the 20 untraced attempts took
0.49 s in the last series (the machine was shared with other agents' benchmark runs). On the big tree
it does not hold: every run copies the
whole tree (per-file open/close dominates; copying with 8 threads took the copy alone from 3.0 s to
1.8 s, median of 6 alternating runs each). Reusing one copy per session, synced by content id, would
remove most of it and is not built. Other agents were running on the machine; single attempts took up
to seconds longer in other runs.

After the review fixes (`overhead-after-review.json`, same script, 20 attempts per series):

| tree | median | p90 | max | command median |
|---|---|---|---|---|
| orders_app (11 files) | 0.08 s | 0.11 s | 0.18 s | 0.67 s |
| orders_app, `--trace` | 0.08 s | 0.09 s | 0.10 s | 0.63 s |
| clone of this repository (2,342 files) | 2.4 s | 2.8 s | 3.3 s | 0.75 s |

The big-tree number is lower than before because the machine was less loaded, not because of the
fixes: the step that dominates, copying the tree for each run (`run_s` 3.1 s median here), was not
changed. A reviewer's case the fixes did change: one 17,504-line lockfile changed vs the base made
every attempt re-diff it (difflib without its junk heuristic): 4.7 s overhead median of 3 probes
before, 0.13 s after (the diff is now reused while the file's content is unchanged, and files above
2,000 lines use difflib's junk heuristic; measured with the reviewer's generator on orders_app).

Not measured: a real agent with and without the protocol; container isolation; precision of the
heuristic rules; Gradle or Maven runs.

## Update 2026-09-25: decisions stay human (D33)

Branch p2/decide (docs/DESIGN.md section 6). Result files and the harnesses that wrote them:
`benchmarks/results/decide-2026-09-25/` (its README says how to run them; nothing needs the network).
Everything here is in-sample unless it says held out: the author of the rules also wrote the cases,
the gold and the questions.

**Intent routing.** Before: both decision questions of the design came back `met` with flow or
dataflow claims. Now a should/which/scale question is intent `decide`, verdict
`human_decision_required`. 24 written questions: 24/24 (in-sample). Two held-out sets of 10, each
written and hashed before the rules it measured, each run once with frozen rules: precision 1.00 and
recall 0.40 on both. The bar was precision ≥ 0.90 and recall ≥ 0.85: **recall is not met**. The first
set's three misses were fixed afterwards (so it is in-sample now), the second's were not. None of the
616 benchmark questions is read as a decision. A host agent that writes the plan can set the intent
itself; the cue tables are the fallback.

**Guards** (`guard_mutations.json`). 54 mutations on git copies of orders_app, glow_mod and
forge_mod: 25 violating, 20 benign, 9 out of reach (bars: at least 30 / 12 / 12 / 6). VIOLATED
precision 1.00 (25 of 25) and recall 1.00 on the in-reach violations (bars 1.00 and 0.90); no benign
or out-of-reach case gave a VIOLATED finding, and all 9 out-of-reach forms (getattr with a literal or
a computed name, importlib, `__import__`, exec, `sys.modules`, a star import, reflection by class
name, a call chain in Java) are named in the limits or reported POSSIBLE. On the 33 orders_app cases
of the `sqlite3.connect` guard the raw per-line regex the old exclusivity check used had tp 7, fp 8,
fn 6; the engine tp 13, fp 0, fn 0. Two cases were added after the first run: V25 (a Java import of a
Kotlin class, a file-node label the first engine mismatched) and B20 (a `def` that shadows an
imported name: the first engine reported it VIOLATED; found by reading the code, not by the set).
`decide check` per case: median 42 ms, max 89 ms (bar 1 s; one earlier run had a single 969 ms
outlier). On Verinoda's own code (`check-time-verinoda*.json`): a prepared 78-file copy with the
index, 4 guards incl. no_edge, 0.48-0.64 s; the full tree, 631 .py of 2,309 files, 3 guards without
the index, 1.6-2.7 s (bar 5 s; before two pre-filters were added it was 6.6-11.3 s).

**Decision brief** (`brief_orders_app.json`). orders_app, the design's EN and TR question: 8 of 8
gold forces each, 18 of 18 cited evidence items re-read equal to what the brief recorded (precision
1.00), the 5 gold question kinds asked, no question the code answers, no recommendation field, 0.11-0.17
s. In-sample: the gold came with the design and the probes were written after reading it. On the
full Verinoda tree a brief takes 5.6-6.2 s (44 forces).

**No regression.** Fast harness, the eight public sets: no difference in any question and approach
between integrate/0925 (`fast-base.json`) and this branch after each of the four steps
(`fast-step4.json` is the last); against `er_new` the only differences are the five Turkish results
integrate/0925 already improved.

**After the two reviews (same day).** Two reviewers found 37 problems (some the same); the fixes and
what they changed, measured on this machine against an export of the branch before the fixes (35d2987):

- *Guards.* Python names are now resolved by scope (a closure's parameter, a module-level `for`/`with`/
  `except`/comprehension variable, `except ImportError: psycopg2 = None`, two drivers in two branches, a
  method named like the import); Java/Kotlin receivers bound without a written type stay POSSIBLE and
  Kotlin import aliases bind; Maven items are cited at their `<artifactId>` line; build files are taken
  from the project's file list (git-ignored ones are not read), test/sample/fixture/vendor ones are
  skipped and a build the root does not include is POSSIBLE; `conftest.py` is test code;
  `--changed`/`--base` read project-relative, NUL-separated paths and count a finding as new when a file
  its binding passes through changed. Every one of the reviewers' forms was reproduced first. The
  mutation set is now 76 cases (22 added from the reviews: in-sample for the fix): VIOLATED precision
  1.00 (32 of 32), recall 1.00 in reach, 13 of 13 out-of-reach forms named or POSSIBLE; the 54 original
  cases give exactly the results they gave before. Raw regex on the 45 orders_app `sqlite3.connect`
  cases: tp 9, fp 15, fn 8; the engine tp 17, fp 0, fn 0. Per case: median 42 ms, max 61 ms.
- *Time on the full tree* (631 .py of 2,314 files, 3 guards without the index, records in memory, 3 runs
  each, same session, other agents loading the machine; `check-time-review-fixes.json`): `decide check`
  1.97-2.12 s against 1.84-2.16 s before - no change (bar: 5 s). The brief got slower: 7.7-8.0 s
  against 5.0-5.5 s, because its probes now read code without comments and docstrings (one tokenize
  pass per file that holds a sink word). A no-op `verinoda update` no longer runs every guard again (it
  says so); `no_edge` re-checks mask each file once per check.
- *Brief* (`brief_orders_app.json`): 8 of 8 gold forces, 19 of 19 cited evidence items re-read equal
  (one more than before: the `if _repo is None` line that makes the shared instance a strong inference),
  5 of 5 gold question kinds, for both questions. The reviewers' adversarial copies (a comment naming a
  connection, a TODO in a test, a global assigned on every call, `./records` in a compose file, a Java
  project with a JDBC driver, boto3 for Bedrock, 61 commits of churn, an ADR whose first `because` is
  context) now give no false force, absence or skipped question (tests in `tests/test_decide.py`).
- *Intent routing.* The reviewers measured the builder's rules on their own questions: precision 0.42,
  recall 0.50 on 44, and 7 of 8 how-to questions read as decisions. The cues now separate strong ones
  from ones a veto cancels (past tense, a question about what the code does, a usage verb); a growth
  condition alone is no decision. In-sample (the rules were changed against them): the reviewers' 52
  questions 52/52, the written 24 and held-out 1 and 2 all correct, and held-out 3 (24 questions the
  fixer wrote before changing the cues but after seeing their results under the old rules) 24/24. The
  one set left out of the tuning, **held-out 4** (20 questions, hashed before the new cues were written,
  run once): **precision 0.83, recall 0.50** - the recall bar (0.85) is still not met. Of its 5 misses,
  4 carry a word ("pick", "fits ... best", "smarter", "doğru zaman") that now adds a note that the
  question may ask for a choice (a note, never another verdict; that word list was written after seeing
  those misses, so it is in-sample). The 86 benchmark questions (119 texts with their English versions)
  are read as no decision and get no note. A sub-question another intent answers but whose words ask
  for a choice in so many words is `human_decision_required` whatever plan gave its intent.
- *No regression* (fast harness, the eight public sets, prepared indexes, baseline and fixed code on the
  same copies): no difference in any of the 86 questions for any of the three Verinoda approaches
  (facts found: analyze 284, retrieve 231, retrieve_text 283 of 319, before and after; the negatives
  matched did not change either): `fast-35d2987.json` and `fast-review-fixes.json`.
- *Review round 3 (same day, by the fixer; not re-recorded in the result files).* The reviewer's 40 new
  questions (20 choices, 20 look-alikes, written before running the router): precision 1.00, recall
  0.55; the cues added for its misses make it 20/20 and in-sample. Held-out 4 with those cues: precision
  0.86, recall 0.60, but its one new hit ("what would you pick") was a phrasing DESIGN section 6 named
  from its misses, so it is no longer clean. A question whose words may ask for a choice is now at most
  `met_with_inference` (it was only a note). The 119 benchmark texts: still no decision and no note, and
  their rule plans are identical before and after. Guard mutations: all 76 cases unchanged (commented-out
  Gradle/Maven dependencies, generated files and sample folders were added as tests in
  `tests/test_decide_review3.py`). Brief on orders_app: 8/8 gold forces, 20/20 evidence re-checks (one
  more item: the connection line now also backs SQLite's presence).

**Not measured.** A real agent session with the new skill text (Claude Code or Codex); the brief on
any repository other than orders_app against a gold (forge_mod and a message-broker question were
only read); held-out guard cases; `decide check` in a real CI job; a quote pin against a live page
(the tests use a cached page, the network was off).

## Update 2026-09-25 (truth rules): word overlap never verifies, roles are bound, code names are not substituted

The rules of docs/DESIGN.md D31, measured at commit `7da834b` against `d3165b8`.

**False sentences recorded as `statically_verified`.** A copy of `examples/orders_app` with two
AI-written files added (`orders/ai/export.py` imports `place_orders`, which exists nowhere;
`orders/ai/remote.py`), made during the design pass; not in the repository. Each sentence was
recorded with `claim add ... --status statically_verified`, then challenged. The first nine are
the design pass's own false sentences, written after reading the code:

| sentence (kind) | before: created -> challenged | after |
|---|---|---|
| apply_discount returns the subtotal above the threshold (general) | verified -> verified | weak -> weak |
| The discount threshold is read from ORDERS_MAX_ITEMS (config) | verified -> verified | contradicted at creation |
| place_order saves the order before it validates the items (general) | likely -> likely | weak -> weak |
| `OrderRepository.save` calls `place_order` (general) | likely -> likely | weak -> weak |
| The discount is 20% above the threshold (general) | likely -> weak | weak -> unknown |
| create_order_handler returns 404 when the payload is invalid (general) | likely -> weak | weak -> unknown |
| Orders are stored in PostgreSQL (general) | weak -> weak | weak -> weak |
| create_order_handler calls save (relation) | weak -> contradicted | contradicted at creation |
| OrderRepository.save calls place_order (relation) | weak -> contradicted | contradicted at creation |

"Likely" is `strong_inference`. Before: 2 of 9 stayed verified and 2 "likely" after critique. After:
none verified or "likely", 3 contradicted when created, with the scope in the reason ("no direct
call to save in create_order_handler (orders/api.py:16-21); calls through other names are not
followed"; "orders/config.py:6 binds ORDERS_MAX_ITEMS to MAX_ITEMS_PER_ORDER; DISCOUNT_THRESHOLD
reads ORDERS_DISCOUNT_THRESHOLD at orders/config.py:7"). Nine more false sentences of the same
kinds (three relations, a config text about another setting, `--kind order` with the order
reversed or a call missing, `--kind location` for a name the file does not define, two texts):
before, of the 7 that could be recorded (`--kind order` is new), 1 verified, 1 "likely", 2 weak
and 3 contradicted after challenge; after, of all 9, none verified or "likely", 7 contradicted at
creation, 2 weak.

Seventeen true sentences of the same kinds: none is contradicted (before: one, a relation cited one
line above its call, was contradicted by critique; it is now a warning and `unknown`). The typed
ones end where they did before (config 4, relation 7, location 1, a quote 1; the order one could
not be recorded before and is verified now). The two plain-text ones drop as intended:
"compute_total applies the discount" `statically_verified` -> `weak_inference`, "apply_discount
returns round(subtotal * 0.9, 2) above the threshold" `strong_inference` -> `weak_inference`.

`analyze "Where is place_orders defined?"` (and the Turkish "place_orders fonksiyonu nerede
tanımlı?"): before `met` on `place_order()` (the mention was linked by identifier parts at 0.75);
after `unmet`, first unknown "no symbol named `place_orders` in this repository; nearest:
place_order (orders/service.py:19)". `trace create_order_handler place_orders`: before a path to
`place_order()`; after unresolved with the same line. `trace export_order save_order`: before the
target silently became `OrderRepository` and the answer was "no directed path"; after "no symbol
named `save_order` in this repository (the name occurs at orders/ai/export.py:19)".

Not reached by these rules (they need the answer checker's typed atoms): the design's case 1 as
written ("returns the subtotal unchanged above the threshold") and case 3 as plain text are
`weak_inference`, not `contradicted`; case 5 (`place_order` calls `OrderRepository.save`, true but
through a parameter) stays `strong_inference` at creation.

**Benchmarks.** Fast harness on the prepared indexes (query-time change; the indexes are
unchanged by it), the seven public sets plus `verinoda_user_tr`: gold facts per approach
identical to the baseline `d3165b8` on every question (analyze 66, 48, 32, 32, 36, 31, 26, 11;
query text and JSON unchanged), verdicts identical, analyze negatives matched 2 (`q04.n.empty_order`
in both orders sets, stated as `weak_inference`) and **0 stated as verified or as findings**, before
and after. Claim texts and
statuses of every analysis are identical except the order of the file list in one impact claim,
which varies between runs of the same code. Code-shaped mentions of the benchmark questions: none
is `not_found`; `max_heat` (forge_mod) and `graph.json` (graphify_core) are now `weak` instead of
linked/ambiguous by identifier parts, which changes no fact or verdict.

**Critique evaluation**: see [Trust harnesses](#trust-harnesses) (two true plain-text claims
`statically_verified` -> `strong_inference`, everything else unchanged).

**Cost.** Checking that a code-shaped name exists reads the repository's files only when the
graph has no such name: 43 ms on `orders_app`, 131 ms on `heldout_repoatlas` (106 files) and
251 ms on `graphify_core` (226 files) for a name found nowhere, cached per index; a scan that runs
over 2 s stops and claims nothing. All of this is in-sample: the rules were written after seeing
these cases, and there is no held-out set of false sentences yet.

**After the review (fixes to D31).** Two reviewers wrote an adversarial set on copies of
`examples/orders_app` (plus `orders/extras.py` with aliases, decorators, nested functions and
self calls, and `orders/flow.py` with calls deferred into a nested function and a lambda) and
`examples/glow_mod`: 101 sentences, 41 false and 60 true, each recorded with `claim add ...
--status statically_verified` and then challenged (the script and both runs are in the fixer's
scratch directory, not in the repository). Before is the branch at `772953c`:

| | before | after |
|---|---|---|
| false sentences verified at creation | 17 | 0 |
| false sentences verified after `challenge` | 17 | 0 |
| false sentences contradicted | 17 | 16 |
| true sentences contradicted | 19 | 0 |
| true sentences verified after `challenge` | 21 | 39 |
| sentences `claim add` refuses (asks for a clearer one) | 2 | 5 |

The 17 false ones that were verified: negations ("place_order does not call validate_items",
"`place_order` is not defined in ...", TR "çağırmaz"), extra clauses ("... before
validate_items", "twice", "Only ...", "defaults to 50"), a quote next to false prose, calls
deferred into a nested function, an owner the line is not in. They are now `strong_inference` (8)
or lower. The 19 true ones that were contradicted: module constants and variables as
definitions, `--symbol path::name`, compound and Turkish sentences whose roles were guessed
(`validate_items and compute_total are called in place_order`, "place_order, validate_items ve
compute_total'ı çağırır", "validate_items'ı place_order çağırır"), "after that", nested calls in
an order claim. The false one no longer contradicted: "`place_order` is defined in
orders/api.py" citing the import line; an import binds the name there, so it is `weak_inference`
now. Refused: two negated order sentences, "... and then, after computing the total, `save`" (two
order words), and two relation sentences without `--symbol` whose callee is not in a clear form.
True sentences that stay unverified (`strong_inference` at creation, never contradicted): clefts
and relative clauses ("place_order is what create_order_handler calls", "... çağıran fonksiyon
...", "...'ın çağırdığı fonksiyon ..."), "both A and B call C", a sentence that adds "first" or
"before anything else", a Turkish config subject ("İndirim eşiği ...", its words are not the
English name the read is bound to), a Java order claim.

`trace` on the same copies: `orders\api.py orders\repository.py`, `create_order_handler
orders/repository.py::OrderRepository.save`, `create_order_handler() OrderRepository.save()`,
`orders.api orders.repository`, `orders/api orders/repository`, `com.example.glowmod.GlowMod
com.example.glowmod.entity.Wisp`, `GlowMod#onInitialize Wisp` and a backslash `.java` path all
resolve (before, each was "no symbol/file named ..."); `Foo.save`, `OrderRepository.place_order`,
`Cart.check` and `orders/service.py::place_orders` are still not found ("no symbol named
`place_orders` in orders/service.py").

Fast harness, the seven public sets plus `verinoda_user_tr`, on the same prepared indexes
(query-time change): facts per question and approach, and negatives, identical to `er_new`
(`d3165b8`), in two runs. The machine was shared with other jobs, so wall times are compared
only between interleaved runs of `772953c` and the fix (best of 3): `orders_app` 6.4 / 7.1 s,
`glow_mod` 7.5 / 8.1 s, `heldout_repoatlas` 14.8 / 14.8 s, within the spread of repeated runs.
Critique evaluation at the fix: every row identical to the committed `critique_eval.json`;
interleaved with `772953c` (3 runs each) the per-claim time is p50 2.6-6.5 ms / p95 51-53 ms
after against p50 2.4-9.4 ms / p95 51-100 ms before on the loaded machine (the committed file's
2.4 / 21.8 ms was measured on a quieter one).

`name_site` on a 2,305-file repository (CPython's standard library, 79,532 nodes) with a current
`search.db`: a name found nowhere is answered in 340-440 ms (before: the 2 s scan ran out and the
answer was "not checked"). Soundness of the index shortcut on that repository: 2,399 words (and
their lower/upper case forms) taken from 150 random files, outside import lines: none was
answered "absent" while a file spells it. With a stale `search.db` the old scan runs.

**Second review round (findings the first fix did not see).** Checked again on fresh copies of
the same apps, against the branch at `098003f`:

- written text that names more code than its check binds verified: "create_order_handler calls
  place_order and fetch_order", "place_order calls validate_items and fetch_order", "place_order
  calls validate_items with MAX_ITEMS_PER_ORDER", "`place_order` calls `validate_items` before
  `save` and `fetch_order`" (order) and "`place_order` is defined in orders/service.py and calls
  `fetch_order`" (location) were `statically_verified`; they are `strong_inference` now
  (`entail.unchecked_names`). The true "A calls B and C", its Turkish form and "A calls B, and C
  calls D" stay verified (each name is a direct call in the caller's body);
- `analyze "What does OrderRepository.place_order do?"` was `met` from a weak text hit (the name
  `place_order` exists in orders/service.py); it is `unmet`, with "no symbol named
  `OrderRepository.place_order` in this repository; nearest: place_order (orders/service.py:19)".
  `LanternEvents.activate` (glow_mod) went from unlinked to `not_found`, `Cart.check` now offers
  `_check`. A dotted name whose owner the graph does not define (`Repository.save`, `wisp.count`)
  stays `weak`, and its uncertainty now says that only the last part occurs ("`save` occurs at
  orders/repository.py:15, not the whole name");
- past the 2 s scan cap a code-shaped name was linked to a similar one again (`place_orders` to
  `place_order`); it is `weak` at most now, saying the existence check did not run;
- "Where is get_repo defined?" (two definitions) no longer gets the unknown "the question's words
  'get_repo', 'defined' occur nowhere".

The 101-sentence adversarial set gives the same outcome as at `098003f` (0 false verified, 0 true
contradicted, 39 true verified, 5 refused). Fast harness, the seven public sets plus
`verinoda_user_tr` on the prepared indexes (query-time change): facts per question and approach,
and negatives, identical to `er_new` (analyze 66, 48, 32, 32, 36, 31, 26, 11). Critique
evaluation: every row identical to the committed `critique_eval.json` (true claims contradicted
0/23). `name_site` on the 2,305-file repository: names found nowhere 340-460 ms as before; a dotted
name whose last part occurs but not the whole name (`repo.save`, `wisp.count`) 335-360 ms against
about 120 ms at `098003f`, since the whole name is still looked for 0.25 s after the part is found.
In-sample, like the rest of this section.

**Third review round (a second reviewer's findings).** The reviewer's probe batches (104 written
sentences, 37 analyze questions, 25 trace endpoints on copies of `examples/orders_app` with extra
files - a Go package in two files, `settings.yaml`, classes with dynamic attributes -,
`glow_mod` and `forge_mod`) were run again on fresh copies with the fix, against the branch at
`f353d95`:

| | before | after |
|---|---|---|
| false sentences verified (at creation or after `challenge`) | 24 of 63 | 0 |
| true sentences contradicted | 8 of 41 | 0 |
| true sentences verified after `challenge` | 21 | 26 |
| false sentences contradicted | 10 | 8 |

The 24 false ones that were verified: a second callee after "instead of", "rather than", "via",
"with the result of", "from inside" or in a relative clause ("..., which uses `get_repo`"); a
file in the text other than the evidence's ("`save` is defined in orders/service.py", "orders/
pricing.py reads ORDERS_MAX_ITEMS"); a kind the definition does not have ("`OrderRepository` is
a function", "an async function", "a module constant" for a class attribute, TR "bir
fonksiyondur", "a method of the Settings class" for `Other.save`); numbers as words ("ten
seconds"), arguments ("with two arguments", TR "müşteri adıyla"), conditions and bounds
("provided that", "for subtotals below the threshold", TR "altındaysa"), adjacency in an order
("immediately before"). They are `strong_inference` or lower now. The 8 true ones that were
contradicted: order sentences without `--symbol` that name the function last ("`validate_items`
runs before `save` in `place_order`", "... by `place_order`", TR "`place_order` içinde"), which
were checked in `validate_items`' body; and relation sentences with a plain-word caller cited
one line above the call ("checkout calls submit", "guarded calls total_of"), whose caller's
body was never read. The two false ones no longer contradicted ("guarded calls total_of when
items is empty", TR "items boşsa") were contradicted for that wrong reason (the cited line one
above the call); they are `unknown` now. "guarded calls total_of" citing the call line (an
import alias: `from orders.pricing import compute_total as total_of`) is verified now; it was
graded "star-imported".

`analyze`: `store.Open` (Go, defined in the package's other file), `pricing.discount_rate` and
`cart.max_lines` (keys of `settings.yaml`; `pricing` is also a module, `cart` a class in another
letter case), `Options.timeout` (set through `self.__dict__`) and `settings.database_url` were
"no symbol named ... in this repository"; they are `weak`/`unlinked` with the site ("`discount_rate`
occurs at settings.yaml:2"). `OrderRepository.execute` and `Cart.append` (only calls on another
object inside the class) were `met` from a text hit; they are `not_found` now. `trace` resolves
`config.DISCOUNT_THRESHOLD`, `OrderRepository.conn`, `Cart.items`, `Settings.MAX_RETRIES`,
`Options.timeout`, `service.compute_total`, `orders.service.compute_total` and `api.place_order`
by similarity with a note ("`DISCOUNT_THRESHOLD` occurs at orders/config.py:7, not the whole
name") instead of "no symbol named ..."; `Foo.save`, `Settings.save`, `Cart.check` and
`OrderRepository.place_order` are still not found. A claim reused by a later analysis no longer
carries the earlier question's weak-link uncertainty.

The 101-sentence set of the first round: the same outcome as at `f353d95` (0 false verified, 0
true contradicted, 16 false contradicted, 5 refused) except one more true sentence verified (the
alias call above), 40 of 60. Fast harness, the seven public sets plus `verinoda_user_tr` on the
prepared indexes (query-time change): facts per question and approach, and negatives, identical
to `er_new` (282 of 282 set x question x approach cells). Critique evaluation: every row
identical to the committed `critique_eval.json` (true claims contradicted 0/23). `name_site` on
the 2,305-file repository, while the test suite ran: names found nowhere 290-560 ms; a member of
the `test` package (611 modules) reads the package under the 2 s limit and answers in 2.0-2.1 s,
"not checked" when the limit runs out first (before: "absent" after 1.7-7.4 s, over the limit).
In-sample: the rules were written after seeing these sentences.

## Update 2026-09-25: name check, third review round

A second reviewer reported nine findings (one high, six medium, two low). Each was reproduced on the
branch and fixed (docs/DESIGN.md D32):

- **High: removed names reported as existing.** `collections.Mapping`, `from collections import
  Iterable` and 11 more collections ABCs removed in Python 3.10 were `exists` (exit 0). jedi followed
  the typeshed stub's own `from collections.abc import ...` to `typing.py`. For a closed
  standard-library module the interpreter's names are now complete: of 1,732 names that stubs import
  for their annotations and the running module lacks, 0 are `exists` (13 before) and 0 are `absent`.
- **MCP `env` started a program from the checked repository** (a `pyvenv.cfg` home inside the project,
  or an interpreter path). It is now refused, and nothing is started. The note for a `.venv` that was
  not used names the program `--env` would start. Before, the note said "pass --env .venv to trust it".
- **False `absent` for attributes that a descriptor or property sets on the instance by computed name**
  (the lazy_property recipe; 3 probes). They are `unknown` now. Standard `property`,
  `functools.cached_property` and a wrapper that only calls the method keep the instance closed.
- **False `absent` for `sys.path += [...]`, `sys.path[:0] = ...`, `import sys as _s` and `from sys import
  path`** in a `conftest.py` or in the checked file (4 forms). They are `unknown` now.
- **Stale cache after a `conftest.py` or pytest's `pythonpath` changed** (4 cases). Each is now a miss
  with the fresh answer, also in a long-lived process.
- **`--diff` silently checked nothing** under `diff.mnemonicPrefix` or `diff.dstPrefix`, or with
  `--repo` in a subdirectory. It now checks these files.
- **`api` said "not found" (exit 3) for names it did not decide.** Examples:
  `concurrent.futures.ProcessPoolExecutor`, `os.environ.copy`. These are now `found: null`,
  `decided: unknown`, exit 0.

Also fixed while checking: a local bound to a literal (`d = {}`) or an instance without a `__dict__`
(`collections.deque`) is closed. Constructor keywords are judged against the class object, so
`threading.Thread(deamon=True)` is `absent`. A standard-library name that the module's own source binds
only on another platform is `unknown` (`subprocess.select` on Windows was `absent`). A class whose
base is a call (`class P(namedtuple(...))`) crashed the check; it is `unknown` now. One failing site no
longer stops the check; it is listed under `incomplete`. The one open low finding is not fixed:
verdicts that change between runs. jedi raises inside its own inference for some names, depending on
set order. The site is then `unknown` instead of `exists`, never `absent`, and the reason now says so.

Result files are in `benchmarks/results/codecheck-review3-2026-09-25/`. Windows 11, Python 3.12.0,
jedi 0.20.0.

| set | environment | sites | absent (false) |
|---|---|---|---|
| reviewer probes of real idioms, plus 26 invented names | none | 352 | 23 (0) |
| reviewer sweeps: stdlib, third-party, compiled stubs | none / Verinoda's `.venv` | 19,666 | 0 |
| second reviewer's probes (removed names, descriptors, conftest, locals), plus 44 invented | none / `.venv` | 703 | 29 (0) |
| fixer's descriptor and decorator probes (5 real, 3 invented) | none | 28 | 3 (0) |
| first round's smaller probe projects | none / a project `.venv` | 104 | 0 |
| click, pluggy, h11, starlette (chosen by a reviewer) | Verinoda's `.venv` | 8,525 | 2 (0) |

- The 2 absents in the four packages are right: `itsdangerous` is not installed. Their `unknown` count
  went from 813 to 737.
- Invented names decided `absent` on the second reviewer's probes: 11/31 missed before, 10/31 now;
  on the local-instance probes 6/7 missed before, 3/7 now.

**Fixture** (144 probes, runtime oracle, in-sample): every probe gets the same verdict and the same
nearest names as in the second round.

| measure | result | bar |
|---|---|---|
| precision of `absent` | 74/74 | |
| recall on closed containers | 74/74 | ≥ 0.9 |
| invented names decided | 74/79 | |
| intended name in the nearest three | 24/26 | |
| warm check | 0.31 s per 100 sites | |
| `check --diff`, 30 changed lines, fresh process | median 1.07 s (1.05-1.11 s, 5 runs) | 1.5 s |

The two versions also took turns on the same 30 changed lines, 7 runs each, on an idle machine. The
median was 1.17 s for the code this round started from and 1.19 s for this code. Reading all 145 files
from the disk cache took 0.59 s (0.37 s in the second round): each answer now also depends on the
`conftest.py` files above its file.

**Clean sets** (every site real; the times are noisy because other runs shared the machine):

| body | environment | sites | absent | not_installed | guarded | unknown | time |
|---|---|---|---|---|---|---|---|
| Verinoda's own package | Verinoda's `.venv` | 48,071 | 0 | 1 | 58 | 13,398 (28%) | 321 s |
| Graphify (a copy) | Verinoda's `.venv` | 51,943 | 0 | 2 | 58 | 19,106 (37%) | 657 s |
| the same | none | 51,841 | 0 | 276 | 121 | 22,743 (44%) | 588 s |
| upstream-graphify itself | Verinoda's `.venv` | 51,941 | 0 | 2 | 57 | 19,063 (37%) | 665 s |
| stdlib `json`, `email`, `http`, `pathlib` as a project | none | 5,210 | 0 | 0 | 0 | 1,289 (25%) | 42 s |

## Update 2026-09-25: name check, second review round

The first round's fixes were made from a shortened copy of the review findings. This round took the
full list (28 findings from two reviews) and reproduced each one on the branch. 23 were already fully
fixed. Four were still open, in full or in part, and are fixed now (docs/DESIGN.md D32: Guards,
Constructors, Cache):

- **The cache kept a stale answer after a re-export in the middle changed.** Example:
  `pkg/__init__` -> `pkg/api` -> `pkg/old`. It kept a stale `absent`, and in the other direction a
  stale `exists`. Now every file jedi loaded for an answer is a dependency. A long-lived process (the
  MCP server) also gave a stale `absent` for a base class reached through a re-export. It now starts
  other files' jedi scripts afresh on each call.
- **A standard-library base with no constructor of its own answered for the class.** Example:
  `class Plugin(abc.ABC, Base)`. `Plugin(name=)` was absent against `ABC()`; now `Base.__init__`
  answers.
- **Guards that guard nothing.** A broad `except Exception: raise`, a bare `except: raise`, and
  `IS_PROD = True` ... `if IS_PROD:` made invented names `guarded` (exit 0). Now they are `absent`.
  Fallback handlers, specific handlers and flags set by an import test still guard.
- **"Not found in this project" for a module the project has.** `lib/helpers.py`, not on the assumed
  search path, is now `unknown`.

The fifth finding (low severity) is half fixed. The reason given for a module-level receiver is now
correct. The other half is deferred: the project-wide attribute-store rule still hides `json.timeout`
when some `self.app.timeout = 3` exists. Narrowing it needs the receiver's type, and a wrong narrowing
would give a false absent. Also new: a file that does not parse is listed under `incomplete`.

Result files are in `benchmarks/results/codecheck-review2-2026-09-25/`. Windows 11, Python 3.12.0,
jedi 0.20.0. Other runs shared the machine, so the times are noisy.

| set | environment | sites | absent (false) before | after |
|---|---|---|---|---|
| reviewer probes of real idioms, plus 26 invented names | none | 352 | 19 (0) | 23 (0) |
| reviewer sweeps: stdlib, third-party, compiled stubs | none / Verinoda's `.venv` | 19,666 | 0 | 0 |
| click, pluggy, h11, starlette (chosen by a reviewer) | Verinoda's `.venv` | 8,525 | 2 (0) | 2 (0) |

- Invented names decided `absent` went from 19/26 to 23/26. The 3 left are `unknown`:
  - a module-level receiver;
  - a name the project stores elsewhere (the deferred finding);
  - a C method without a signature.
- The 2 absents in the four packages are right: `itsdangerous` is not installed.

**Fixture** (144 probes, runtime oracle, in-sample): every probe gets the same verdict and the same
nearest names as before this round.

| measure | result | bar |
|---|---|---|
| precision of `absent` | 74/74 | |
| recall on closed containers | 74/74 | ≥ 0.9 |
| invented names decided | 74/79 | |
| intended name in the nearest three | 24/26 | |
| warm check | 0.32 s per 100 sites | |
| `check --diff`, 30 changed lines, fresh process | median 1.33 s (1.30-1.52 s, 5 runs) | 1.5 s |

In a second `--diff` run the two versions took turns under the same load, 7 runs each: the median was
1.23 s for the code this round started from and 1.23 s for this code.

**Clean sets** (every site real):

| body | environment | sites | absent | not_installed | guarded | unknown | time |
|---|---|---|---|---|---|---|---|
| Verinoda's own package | Verinoda's `.venv` | 47,741 | 0 | 1 | 58 | 13,954 (29%) | 369 s |
| Graphify | Verinoda's `.venv` | 51,943 | 0 | 2 | 58 | 19,150 (37%) | 748 s |
| the same | none | 51,841 | 0 | 278 | 121 | 22,828 (44%) | 652 s |
| stdlib `json`, `email`, `http`, `pathlib` as a project | none | 5,210 | 0 | 0 | 0 | 1,359 (26%) | 125 s |

A few `unknown`/`exists` answers in the `email` package change from run to run (12-14 of 5,210
sites). This is not new: two runs of the code this round started from differ on 12 such sites. Checked
alone, `_header_value_parser.py` gives the same answers under three hash seeds, so jedi's inference
seems to depend on the files read before it. None of these sites is ever `absent`.

## Update 2026-09-25: name check after review

Two reviews of `verinoda check` found code execution and false absents. Each finding was reproduced
first; the fixes are in docs/DESIGN.md D32 (Safety, Still `unknown`, Not closed at all). Result files:
`benchmarks/results/codecheck-review-2026-09-25/`. Windows 11, Python 3.12.0, jedi 0.20.0; other runs
shared the machine, so times are noisy.

**Safety (the reviewers' repros, re-run on the fixed code).** Planted code writes a marker file when it
runs. Before: a `whoami.exe` copied to `.venv/Scripts/python.exe` was started (jedi's `safe=True` is
true for any file on Windows); a `.pth` import line in the project's `.venv` ran twice per check; a
package `__init__` ran when jedi read a compiled submodule, in site-packages, in an editable project,
and inside Verinoda's own process with no `.venv`; `--diff=--output=FILE` emptied FILE (CLI and MCP).
After: no marker in any of the five cases, the fake interpreter is never started, and the `--diff`
value is refused. `tests/test_codecheck.py` has these cases (all new tests fail on commit 0bc9616).

**False absents on real code.** Before = the reviewers' result files (commit 0bc9616), after = this
code on copies of the same files.

| set (written by) | environment | files | sites | false absents before | after |
|---|---|---|---|---|---|
| probes of real Python idioms (reviewer 2) | none | 87 | 352 | 25 | 0 |
| a sweep over standard-library objects (reviewer 2) | none | 253 | 11,367 | 5 | 0 |
| a sweep over third-party objects (reviewer 2) | Verinoda's `.venv` | 106 | 6,922 | 4 | 0 |
| compiled modules with stubs: numpy, pydantic_core (reviewer 2) | Verinoda's `.venv` | 14 | 1,377 | 24 | 0 |
| click, pluggy, h11, starlette, not used before (reviewer 1) | Verinoda's `.venv` | 71 | 8,525 | 5 | 0 |

The 87-file set also has 26 invented names: 19 absent before and after (the other 7 are `guarded` or
`unknown`, as designed). In the last set 2 absents remain and are right: `itsdangerous` is not
installed. These sets are in-sample for this round: rules were changed while looking at them.

**The first measurement again.** The fixture set (144 probes, runtime oracle) gives the same verdict
and the same nearest names on every probe: precision of `absent` 74/74, recall on closed containers
74/74, 74/79 invented names decided, intended name in the nearest three 24/26, 0 of 65 real sites
absent. Warm 0.34 s per 100 sites; `check --diff` of 30 changed lines in a fresh process, median
1.25 s (1.15-1.34 s, 5 runs; bar 1.5 s); the whole probe directory 3.6 s; again from the disk cache
0.43 s. Clean sets, every site real:

| body | environment checked | files | sites | absent | not_installed | guarded | unknown | time |
|---|---|---|---|---|---|---|---|---|
| Verinoda's own package (this branch) | Verinoda's `.venv` (`--env`) | 161 | 47,596 | 0 | 1 | 58 | 13,922 (29%) | 378 s |
| Graphify | Verinoda's `.venv` | 388 | 51,943 | 0 | 2 | 58 | 19,150 (37%) | 777 s |
| the same | none (standard library only) | 388 | 51,841 | 0 | 278 | 121 | 22,828 (44%) | 667 s |
| `json`, `email`, `http`, `pathlib` copied as a project | none | 40 | 5,210 | 0 | 0 | 0 | 1,370 (26%) | 80 s |

The times are 9-45% above the first measurement's on Graphify and the standard-library copy with the
same number of jedi calls, and 10% below on Verinoda's package: the machine was busy, so no speed
change is claimed either way. The retrieval benchmark (`fastbench`, the seven public sets and
`verinoda_user_tr`) gives the same facts as the current main code on all 258 question×approach rows
(the check changes no index or query code; the private set was not run).

**Not measured.** Linux and macOS (the new tests run in CI there); a `.venv` whose base interpreter is
managed by uv, pyenv or conda on a real machine (the rule is unit-tested only through Verinoda's own
interpreter); how often the new `unknown` reasons (stores through parameters, compiled-module stubs)
hide a real invented name outside the fixture set.

## Update 2026-09-25: name-existence check (`verinoda check`, D32)

`verinoda check` (docs/DESIGN.md D32) was measured on a generated fixture set with a runtime oracle
and on clean code where every site is real. Windows 11, Python 3.12.0, jedi 0.20.0. The harness
(probe generator, oracle, scorer, clean-set runner) and the full outputs are outside the repository
(the builder's scratch directory); the numbers below are copied into
`benchmarks/results/codecheck-2026-09-25/` (`summary.json`, and `fixture_rows.json` with every probe).

**Fixture set (in-sample: written by the rule author, and the rules were changed while looking at
it).** A copy of `examples/orders_app` with a project `.venv` (`python -m venv --without-pip`;
packaging 26.3 and idna 3.20 copied from Verinoda's own venv; a local-only package `fancylib`
2.1.0), and 144 probe modules with one site each: 79 invented names (near-name renames,
synonyms, extra keyword arguments, a name imported from the wrong module, missing modules and
submodules, wrong dict keys; three of them are packages installed next to Verinoda but not in the
project's `.venv`) and 65 real counterparts (two of them guarded imports). Ground truth: each probe
module is imported and called with the project's own interpreter in a separate copy
(`ImportError`, `AttributeError` on the name, `TypeError` about the keyword, `KeyError` on the key =
absent; anything else = exists). The oracle and the labels agree on every probe.

| metric | design bar | measured |
|---|---|---|
| precision of `absent` | ≥ 0.99 | 74/74 |
| recall of `absent` on invented names in closed containers | ≥ 0.9 | 74/74 |
| invented names decided `absent` (the rest `unknown` with a reason) | ≥ 0.6 | 74/79 = 0.94 |
| intended name in the nearest three, near-name renames | ≥ 0.8 | 24/26 = 0.92 |
| intended name in the nearest three, every mutation with an intended name | - | 48/62 = 0.77 |
| wrong-module mutations with the defining module under `elsewhere` | - | 3/3 |
| real sites called `absent` | 0 | 0 of 65 (62 exist, 1 unknown, 2 guarded) |
| sites judged against Verinoda's own venv while the project has a `.venv` | 0 | 0 |
| warm, per 100 sites (second in-process run, disk cache off; 364 sites in 145 files) | ≤ 1 s | 0.33 s |
| `check --diff`, 30 changed lines, fresh process including environment start-up (5 runs) | ≤ 1.5 s | median 1.22 s (1.14-1.33) |
| whole probe directory, fresh process (3 runs) | - | 3.5-3.6 s |
| whole probe directory again with the disk cache | - | 0.57 s (145/145 files from the cache) |

The 5 invented names left `unknown` are the designed open cases: `sqlite3.connect_async`
(Python 3.12's `sqlite3` defines a module `__getattr__`), a local module with `__getattr__`,
`json.dumps(sorted=True)` (`**kw`), and two parameter receivers (one annotated). The near-name
misses: `re.matchall` (intended `findall`: no shared part, too many edits) and `p.read_txt` on a
parameter (unknown, so no nearest names). By kind of mutation: near 25/26, synonym 19/22, kwarg 15/16, wrong module 3/3, missing module 9/9, dict key 3/3 decided absent.

**Clean sets (working code; every site is real, so every `absent` would be false).**

| body | environment checked | files | sites | absent | not_installed | guarded | unknown |
|---|---|---|---|---|---|---|---|
| Verinoda's own package (this branch) | Verinoda's `.venv` (`--env`) | 161 | 46,732 | 0 | 1 | 57 | 13,234 (28%) |
| Graphify (`upstream-graphify`: `graphify/`, `tests/`, `scripts/`, `tools/`) | Verinoda's `.venv` (not its own: lock mismatches are listed) | 388 | 51,943 | 0 | 2 | 58 | 19,197 (37%) |
| the same | none (standard library only) | 388 | 51,841 | 0 | 279 | 121 | 22,823 (44%) |
| `json`, `email`, `http`, `pathlib` of the standard library, copied as a project | none (standard library only) | 40 | 5,210 | 0 | 0 | 0 | 1,360 (26%) |

Graphify is only partly unseen: Verinoda's vendored `project_index` derives from it. Every guarded
site of the Verinoda run whose unguarded verdict was `absent` (8: `yaml`, `botocore`,
`mcp.server.fastmcp.FastMCP` and `mcp.types.AnyUrl` in mcp 2.2.0) was imported in that venv: all are
absent at runtime too, and the code handles it. `not_installed` there is `anthropic`, a declared
extra that is not installed.

Found and fixed by these runs before the numbers above: 5 false absents on Verinoda's package (a
from-import judged without looking at the module's own names; a `PYTHONPATH` entry taken for the
standard library - these two are covered by the clean runs only); `robot.py` shadowing the `robot`
package (the file's own directory was on the search path inside a package); `import jedi`
"existing" in any environment (jedi's own process has it imported); `os.fork` absent on Windows in
Unix-only code (`http/server.py`); `os.path` read as module `os`. By reading, not by a run:
constructor keywords of an `Enum` (`Color(value=1)` goes through the metaclass). The last five have
unit tests in `tests/test_codecheck.py`.

Why `unknown` is 28-45% of the sites: parameters (`self`, `node`, pytest fixtures), subscripts and
binary operations as receivers (`tmp_path / "x"`), locals assigned from calls, `**kwargs` callees.
That is by design: those receivers can hold subclasses or other types, and an agent must read them.

**Other results.** The retrieval sets (`fastbench`, seven public sets and `verinoda_user_tr`) give
the same facts as the current main code on all 282 set×question×approach rows (`check` changes no
index or query code; the private set was not run). A whole-package run takes 7 minutes for about
46,700 sites (jedi is 70% of it; one process, cache off) and grows to about 1 GB of memory.

**Not measured.** Linux and macOS (the unit tests run in CI there); a project environment on
Python 3.10 or 3.13; large dynamic libraries in user code (numpy, pandas, Django, SQLAlchemy,
pydantic); agents following the new skill section (no model run); the design's mod and JVM parts
(config keys, resource ids, jar index - not built).

## Update 2026-09-25: analyze keeps what query found, grounded verdicts, Turkish, update time

Measured with the fast harness (`benchmarks/results/fast-2026-09-25/`, its README says how): the
Verinoda approaches only, scored exactly like `verinoda bench run`, on the seven public sets and
one private set (left out of the files; it moved the same way). Fresh-index A/B where the change
touches the index.

**analyze no longer loses what query finds.** An analysis carried only its claims; the passages its
own search had found were dropped. On the seven public sets it delivered fewer gold facts than
`verinoda query` on 33 of 74 questions (62 facts). It now carries the passages `query` gives for
the same question, as a list of lines (one JSON string would escape every line break, and the
locators in it could no longer be read). Negatives unchanged. The output grows by up to 6,000
characters; over MCP (12,000 characters) the passages are cut before any claim.

| set | analyze before | analyze after | query text |
|---|---|---|---|
| forge_mod (68) | 42 | 66 | 66 |
| glow_mod (50) | 43 | 48 | 48 |
| orders_app (32) | 31 | 32 | 32 |
| orders_app_tr (32) | 30 | 32 | 32 |
| graphify_core (37) | 29 | 36 | 36 |
| graphify_core_tr (37) | 16 | 27 | 26 |
| heldout_repoatlas (33) | 13 | 26 | 25 |
| **total (289)** | **204** | **267** | **265** |

**"met" means the claims are about the question.** A verdict audit (`audit-*.json`) compares each
question's verdict with the gold facts its claims carry (the passages left out). "Met on the wrong
sources": every sub-question met, none of the gold facts in the claims. Before, context claims (a
verified definition of whatever ranked near the question) could make a sub-question met: "Where is
an order written to the database?" was met on `load_settings()` and a config line. Now a context
claim counts only when it is about the question's subject (a named or linked symbol, a member or
owner of one, or an item carrying every group of the question's words), and the data-path handler
states the write sites it found as verified locations. Public sets, 74 questions:

| | before | after |
|---|---|---|
| met with none of the gold facts in the claims | 2 | 0 |
| met, claims carry some of them | 18 | 20 |
| met, claims carry all of them | 27 | 27 |

The claims that answer are then listed first (`answer_claim_ids` per sub-question: what the
sub-question's own handler found, the product's code before tests, the strongest first), and a
question that names a symbol has its callers or tests looked up for that symbol only. Fact results:
0 of 333 change; one more met fully backed (`graphify_core_tr` 2 -> 3).

**Turkish.** 82 more generic stems in the seed glossary (budget, cut, truncate, fit, score, weight,
evidence, claim, decision, design, architecture, option, flag ...; `yapı` left out, `yapıyor`
would read as structure): `graphify_core_tr` query text 26 → 30, analyze 27 → 31, retrieve 15 →
18; the other sets unchanged. Multi-part questions: ", sonra / ardından / ek olarak" split a
question, and a later part that names no code and at most one word of its own asks about the
earlier subject (Turkish drops it); no fact moved on the sets, one more backed verdict.

**Stemmer.** `query`/`queries`, `copy`/`copies`, `entry`/`entries`, `key`/`keys` were different
tokens (quer vs query); they are one now. Fresh-index A/B: 2 of 333 results change, both up
(retrieve JSON on `orders_app_tr` and `graphify_core_tr`). The same kind of split for words
ending in -er: `cluster` was `clust` but `clustered`/`clustering` stayed `cluster` (likewise filter,
register, render, answer: on `graphify_core` 77 units carried one form and 193 the other); an
inflected -er word now meets its base (tokenizer version 4). Fresh-index A/B: 0 of 333 results
change; kept as a correctness fix.

**Update time.** Each path is resolved once per build (the pipeline called `Path.resolve` about
30,000 times per update of Verinoda's own repository) and compact JSON goes through the C encoder.
Then what each Python file imports and calls (two walks over every node of every `.py` file on each
build) is kept between builds, keyed by a hash of the file (`verinoda/python_facts.py`); where an
import points is still worked out on every build. `verinoda update` after a one-line edit of a copy
of Verinoda's own repository (about 1,200 files), the command itself, wall clock, three alternating
runs per version: 44.1-44.8 s → 36.6-38.1 s (resolve once) → 34.3-34.9 s (kept Python facts).
graph.json is the same (byte for byte on the examples, on graphify_core and, with the kept facts, on
Verinoda's own repository). Fresh-index A/B of the first change: 0 of 333 results differ.

**All of it together, on a quiet machine (2026-09-25, evening).** The code before these changes
(`d3165b8`) against the integration of the per-file caches (Python facts and the cross-file pass), the
kept graph and the other update changes: each version on its own copy of Verinoda's repository (the
new code migrates `atlas.db`), the command itself, wall clock, three alternating runs per edit kind.
An edit that shifts lines (the graph changes): 34.0-34.3 s -> 27.1-27.2 s. A comment appended (the
graph stays the same, so the last build's graph.json is kept): 34.0-34.1 s -> 21.1-21.2 s (27.3 s on
the first such update, which records the build). Harness: `cli_ab2.sh` in the session scratchpad.

*Correction:* this section first said 35.1 s → 27.9 s. Those figures came from a timing script that
imported the indexer before Verinoda had pointed it at `.verinoda/index`, so part of the pipeline
used another directory and did less work. The figures above are the command's own.

**Cross-file import pass.** The pass that turns `from ... import` statements into `uses` edges and
repoints type references (`_resolve_cross_file_imports`) parsed every `.py` file with symbols and
walked every node of it on each build (about 3.2 million calls). What that walk can use depends only
on the file's bytes, so a pruned tree per file (its `from ... import` statements, its definitions and
the identifiers inside a definition that an import binds) is now kept between builds, keyed by a
hash of the file (`verinoda/python_cross.py`); the upstream function runs unchanged over it, and only
while its source matches the version this was checked against. The pass alone, in-process, on the
inputs captured from a real build of a copy of Verinoda (622 `.py` paths, 24.6k nodes, 52.5k edges):
upstream 5.8-7.2 s, first build with the cache 5.0-5.7 s, later builds 0.68-0.78 s, after 6 edited
files and a new module 0.96 s (7 files parsed). Python's standard library as a project (1,739 paths,
80k nodes, 136k edges): upstream 16.7-21.1 s, first build 14.9-17.3 s, later builds 5.5 s. The output
(new edges, nodes and edges after the pass) is identical element by element and in order in every
case, first build, later builds and after edits (20 files and a new module on the standard library).
`verinoda update` after a one-line edit of a copy of Verinoda's own repository, the command itself,
wall clock, 8 rounds alternating which version went first, on a loaded machine (other jobs running):
38.3-42.6 s (median 39.9) → 32.1-39.3 s (median 35.0); the kept trees were faster in 8 of 8 rounds,
by 2.7-7.2 s (median 4.9). graph.json: the same in all 8 rounds between the two copies (root path
set aside), and byte for byte on one copy updated from the same saved state with the old code, with
the new code and kept trees, and with the new code and none. The kept file is 1.1 MB for Verinoda,
1.7 MB for the standard library. Answers were not re-benchmarked: nothing downstream of graph.json
changes.

Then the loop in that pass that repoints type references: once per importing file it scanned every
edge (52k on Verinoda), though only an edge whose relation is a type
reference (references, inherits, implements, extends) can change. It is now given just those; the
one step after it that reads every edge, dropping the stubs it repointed edges away from unless some
edge still names them, runs on a scratch copy of the nodes and is redone over every edge. The pass
alone, later builds, alternating in one process: Verinoda 0.82-0.95 s → 0.44-0.50 s, the standard
library 9.2-9.7 s → 2.7-2.8 s (first build there 12.1-13.5 s against upstream 16.9-22.1 s). Output
identical as above (first build, later builds, after 8 edited files and a new module on Verinoda,
20 and a new module on the standard library), and the tests cover edges upstream fails on.
`verinoda update`, 6 more rounds against the code before both changes: 35.4-37.4 s (median 36.3)
→ 31.1-37.8 s (median 31.2), faster in 5 of 6 (the first run after the code changed was 1.7 s
slower); graph.json the same in every round, and byte for byte on one copy updated from the same
saved state with the old code, with kept trees and without.

**Update time, second round.** Four changes to what an update does around the extraction; the
graph, the search index and the answers stay the same. (1) The AST cache's source paths are made
absolute once per path string for a build, not once per item (about 150,000 pathlib joins per
update of Verinoda's own repository). (2) After the build graph.json is read once and written
once: ids made portable and the nodes of missing files pruned in one rewrite (before: five
reads and two rewrites of the 34 MB file, and the receiver sidecar computed twice), and whether
an edit concerns the graph is read from the JSON without building the graph. (3) On a
repository whose graph.json Verinoda rewrites after every build (ids that carry the root, nodes
of files the code names but the repository does not have; Verinoda's own repository is one), the
upstream "topology unchanged" path never fired. There, when the pipeline builds exactly the
graph of the last full rebuild, it now runs: graph.json is kept, clustering and two rewrites are
skipped, and GRAPH_REPORT.md and the dated backup are written with the upstream functions as the
full path writes them on such a repository (`.verinoda/index/rebuild_record.json`). Where
graph.json is the pipeline's own output, the upstream comparison already worked and still
decides alone. On a repository of the first kind, that is the case for an edit that leaves every
node and edge as it was (a comment or a sentence changed in place, lines appended at the end of a
file); not when lines move or symbols change, and not on the first update after a file was added
or removed or after a scan (the upstream merge carries the previous graph's community numbers
into the next graph, so it comes out different once more). (4) The 112 data-shaped JSON files the
upstream extractor skips are remembered by path and bytes, and fewer than 64 files left to
extract are extracted in the process instead of by a process pool (after the second review:
on Windows only, and only up to 256 KiB of source; see below).

`verinoda update` on two identical copies of Verinoda's own repository (about 1,200 files), the
command itself (`python -m verinoda update`, wall clock), run one after the other at the same
path in alternating order, other agents busy on the same 6-core machine, so only the
differences within a pair mean much (all four changes;
`benchmarks/results/update-time-2026-09-25/4-all-four.json`, its README says how):

| update after | before | after | pairs |
|---|---|---|---|
| an edit that leaves the graph as it was | 35.2-38.0 s | 26.7-28.2 s | 3 at the start of the run |
| the same, later in the run (machine busier) | 38.2-43.3 s | 28.8-30.3 s | 3 |
| an edit the graph changes with (a moved line, a file added or deleted; the first update of new code) | 35.3-38.0 s | 32.7-34.6 s | 4 |
| `scan`, `scan --force` of an unchanged tree | 34.8-36.4 s | 32.5-32.8 s | 2 |
| a scan with no index at all | 74.3 s | 71.3 s | 1 |

One more pair, a Markdown line inserted at the busiest moment of the run, came out 7.3 s slower
(39.5 -> 46.8 s).

Measured one change at a time before that (same kind of pairs): (1) 1.3 s less on the median of
7 updates that change the graph (-3.5 to +0.9 s); (1)+(2) 2.3 s less (9 updates); (1)-(3) 8.0 s
less on 7 updates that keep the graph (35.2-36.3 s -> 27.5-28.1 s) and 2.3 s less on 7 that do
not (files 1-3 in the same folder). (4) was not measured on its own; with it the updates that keep
the graph took 26.7-28.2 s, within the noise of the run before. After every step of these runs (54 steps: in-place comments, a moved line, an added and a
deleted file, a deleted module other files import, a data JSON edit, Markdown edits in place and
with a line inserted, `scan`, `scan --force`, a scan from nothing) the outputs of the two copies
were compared: graph.json byte for byte, GRAPH_REPORT.md, the labels and their signatures, the
dated backup, the receiver sidecar (the graph's mtime aside), search.db (every table, sorted;
the graph's mtime and the build time aside), lexicon.json (`built_at` aside), manifest.json (the
`seen` times aside) and the snapshot's counts, then `verinoda query` for three questions.
Identical in every step. No benchmark set was rerun: what the answers are built from did not
change.

A review then found two differences from main, both fixed. On a repository with nothing to
rewrite, (3) took over the upstream fast path and rewrote GRAPH_REPORT.md and the dated backup
where main leaves them as they are (orders_app with a data JSON set to `{}`: the report's word
count, and the day's backup replaced). And the MCP server kept its loaded graph as long as
graph.json's stat did not change, so after an update that kept graph.json but changed the
receiver sidecar (`repo.get(order_id)` -> `repo.save(...)` on the same line) the running server
still answered with the old call edge. Main does the same on a repository where the upstream
fast path keeps graph.json; the server's key now includes the sidecar. After the fixes the
review's A/B runs (main against this code, the command itself at one path, 9 sequences with 230
steps on orders_app with and without files that name missing ones, outside a git repository, and
with TypeScript path aliases: comments, string literals, docstrings, a receiver call, renames,
moved lines, a file that does not parse, data and config JSON edits, deleted and restored files,
ignore files, a hand-edited label, a removed sidecar and report, a date rollover, backups off,
`scan`, `scan --force`) gave identical outputs in every step, and `verinoda query`, `map` and
`notes` the same text. The timings above were taken on Verinoda's own repository, where
graph.json is rewritten after every build; the fixes do not change what an update does there.

A second review found four more things, all fixed. (a) A data JSON file rewritten while a build
ran was remembered under the digest of the bytes read before extraction with the result of the
bytes the extractor read later (`cfg.json` holding config JSON when the build started and data
JSON when it was extracted), so its config nodes stayed out of the graph in every later update
and even after `scan --force`. A skipped result is now kept only when the file holds the same
bytes after extraction as before, a `force` build replays nothing, and the store's version went
up, so a store written by the earlier code is dropped (the review's steps now give main's graph
byte for byte). (b) The 64-file rule slowed updates of projects with 20 to 63 JavaScript or
TypeScript files of ordinary size (upstream never caches them, so every update extracts them
all): 40 TypeScript files of about 1 MB updated about 1.5 s slower than main. What is left is now
extracted in the process only when it is at most 256 KiB of source (here about 2 ms per KB of
TypeScript in the process, while a pool costs 0.6-0.8 s before it starts), and only on Windows,
where that was measured; otherwise it goes to the pool as on main. `verinoda update` after a
comment edit in a Python file, the command itself, main against this code at one path in
alternating order, graph.json byte-identical in every pair (median difference, this code minus
main): 24 TypeScript files of 224 KB in total, extracted in the process, -0.1 s (6 pairs); 60
files, 178 KB, in the process, -0.1 s (6); 60 files, 599 KB, pool, +0.06 s (4); 40 files, 993
KB, pool, -0.2 s (4). One scan of the 40-file project took 25.1 s against main's 10.9 s in that
run; 7 more scans of fresh copies in both orders took 8.6-9.5 s with either code. In an update of
Verinoda's own repository what is left after the replay is 23 files, 172 KB, extracted in the
process as before, so the figures above stand. (c) Each JSON file to extract was read whole to
hash it, also one past the extractor's 1 MiB limit (an update with a 400 MB data file peaked at
446 MB, main at 50 MB). Now at most 1 MiB + 1 bytes are read, as the extractor reads, and such a
file counts no bytes towards the rule in (b); with a 100 MB data file each of three updates
peaked at 50 MB (main: 50 MB twice, and once 1,376 MB, as high as the scan; not investigated). (d) The code stamp of `rebuild_record.json` was read from disk when first needed, so
a process that kept running (the MCP server) while Verinoda's files changed on disk wrote a
record of the old code's output under the new code's stamp, and the next `verinoda update` kept
a graph.json the new code would have written differently. A process now neither writes nor uses
a record once a stamped file changed on disk after it was loaded; in the review's steps the next
update writes the new code's graph.json, as main does.

Found on the way, not changed (the outputs had to stay the same): an id made portable whose
plain form is taken gets `_unresolved` appended without checking that name too, and the upstream
merge keeps the previous graph's portable ids as nodes of their own, so graph.json of Verinoda's
own repository holds 6 duplicate node ids (`tests_upstream_fixtures_foundation_unresolved` and
five more), and each missing import of a JavaScript fixture appears twice.

**A set of Turkish user questions about Verinoda (`verinoda_user_tr`, in-sample).** 12 questions (one
a user's own words, the rest written in that style), 30 gold facts at commit 3bd1b94: query text
11/30, analyze 8 → 11 with this round; of 5 questions judged met, 3 have none of their gold facts
in the claims. It shows a weak spot the other sets did not: in a repository whose code is English
but whose UI strings and docs are Turkish, the Turkish words of a question find the Turkish text
first. Three attempts were measured and not kept: 29 passive verb stems (4 results up, 6 down),
weighing a Turkish word no code name contains below its translation (user set +3, but `forge_mod`
retrieve 50 → 45: in a mod, those words are how the data files are found), and a seed entry
`degis` → change (değişince, değişirse; 3 results down, all on one user question whose answer
is not about changes, none up).

**Turkish questions: code words, short stems.** An English word typed into a Turkish question took
up to three of the lexicon's co-occurrence pairs (`update` -> issue, cli, audit; `graphify` ->
install, uninstall, hook): names it occurs with, not what it means. An English question never gets
them, and the question plan already skipped them for words the repository uses. Now a question word
that is a name part of some indexed unit (a symbol, a test, a heading or a data key) keeps only the
pairs that carry it: the repository's own translation pairs, a name built on the word (`duplicate`
-> `deduplicate`) and a name the seed dictionary translates to it (`start` -> `baslat`). Two
cleanups came with it. A Turkish word the index does not know expands through its stem as narrowly
as the stem is short: a stem of 5+ letters to the three most frequent terms it begins, as before (a
Turkish spelling of an English word: `projenin` -> `project`, `algoritması` -> `algorithm`,
`protokolü` -> `protocol`); a 4-letter stem only to itself inflected, for a name its first part in
snake_case or camelCase (`para_birimi`, `WispSpawner`; not `paragraph`, not `artifact` for
`artırıyor`); a 3-letter stem only to itself, when the code names something with it (`sil` for
`siliniyor`; `sayıyor` no longer reaches `sayfa`, `sığmak` `signal`, `sınır` `singleton`). And a term
reached by several routes keeps its highest weight. Same prepared indexes (question-time change;
`9-before-tr-question-words.json` -> `9-tr-question-words.json`), eight public sets: analyze 282 ->
284, JSON 229 -> 231, text 280 -> 283, negatives unchanged. Five results change, all up:
`verinoda_user_tr` u09 0 -> 2 / 1 / 2 (analyze / JSON / text; in-sample; the set is now 13 / 9 / 13
of 30) and `graphify_core_tr` g04 text 2 -> 3, g08 JSON 0 -> 1 (the change was not tuned on that
set). Measured one at a time on prototypes of the first version below, all of it came from the
first change; the stem and weight changes moved rankings only.

The first version of this change kept only translation pairs and expanded every stem as the 4-letter
rule above; it scored the same on the eight sets but lost elsewhere. A review wrote 44 Turkish
questions against `glow_mod`, `graphify_core_tr` and `orders_app_tr` (`9-tr-review-ranks.json`: the
rank of the answer in `verinoda query`; not a benchmark set, and the final rules were made with them
in view). Against d3165b8 the first version ranked the answer higher on 7 and lower on 11, where it
dropped Turkish spellings of English words (proje, algoritma), camelCase names (`WispSpawner`) and
pairs that carry the word (`deduplicate`, `baslat`). The final version: 9 higher, 1 lower
(`duplicate node'lar ...` 2 -> 3: neighbours of `node` had helped there). Still lost against
d3165b8: the co-occurrence pairs of a code word that do not carry it, even where they help. Noise
that stays: a 3-letter stem still reaches an English name part spelled like it (`sığmak` -> `sig`
in graphify), and a 5+ letter stem the English words that begin like it (`indirirken` ->
`indirect`). The private set was not run for this change. The larger cause is still open: each
dictionary gloss of a Turkish word scores as its own term (bul -> find, lookup, search), so correct
additions hurt: Turkish verb forms (yeniliyor -> refresh) cost the user set 5 facts, the UI's own
en/tr strings as glosses 1; neither was kept.

**Final answers.** `verinoda bench run --answer-cmd CMD` hands each approach's context and the
question to any command (a local model, a command-line client) and scores the answer it writes:
gold facts, known-wrong statements, `answers_correct` (every fact, no wrong statement); analyze's
summary reports whether "met" is backed by gold facts in its claims. No model was run for this
update: final-answer accuracy is still **not measured**.

## Update 2026-09-24: data files, game mods, Java calls

The goal of this round: questions about a Minecraft mod, whose behaviour lives
partly in its data pack (`.mcfunction`), its JSON resources and a yml config,
which Graphify and Verinoda did not index. What changed (see the README
section "Game mods, data packs and other data files"):

* data files (data packs, JSON, yml/toml/ini configs, SQL, shaders, ...) are
  indexed as `data` units; files left out are listed with the reason;
* resource ids link code and data in pack/mod repositories, with the
  registry taken from the context; inferred links are marked as such;
* byte-identical data files rank once; reference trees (`setup --reference`)
  rank at 0.6x unless the question names them; generic data files that are
  neither configuration nor a pack resource rank at 0.6x unless named;
* Java calls the extractor drops are added when imports or the package bind
  the class (the original plugin kept next to a port makes `Wisp.spawn`
  ambiguous by name, and the extractor then records no call at all);
* `analyze` follows a line that names a data resource to the call on that
  line and to the callers of its function, and quotes the config file line
  for a settings question;
* the `called by` outline lists product code before tests and each caller
  once (a mod's `src/gametest/` sorts before `src/main/`);
* Turkish: passive caller questions ("kimler tarafından çağrılıyor"), the
  command-name mapping (`scan` komutu -> `cmd_scan`), translation pairs from
  parallel locale files, and game/mod words in the seed dictionary.

Four independent reviewer agents read the change before any of it was
measured and reproduced about twenty defects, all fixed. The serious ones:
the data pass indexed files the graph skips as secrets (`credentials.json`)
and printed them in query output; any `data/<a>/<b>/` folder of an ordinary
repository produced resource links; an id resolved to every registry, and a
link to the wrong kind of file was stated as `statically_verified`; a Java
call through a same-named class of another package was graded as a verified
call; the copy rule hid live code behind a frozen snapshot of it.

**The new set, `glow_mod`** (`examples/glow_mod`, 14 questions, 50 facts, 7
questions in Turkish; see [Question sets](#question-sets)). It was written by
an agent that never ran Verinoda on it. It was held out for the first
measurement only: the Java call pass, the translation pairs, the game words of
the seed dictionary and the link chain in `analyze` were added after looking at
the misses of q01, q03, q04 and q13, so the last column is in-sample for those
changes. `repeat = 1`, no upstream CLI; `32a5bd4` is the last pushed commit
and knows nothing of the set's reference tree (`corpus.verinoda_config`).
Facts found (pinpointed), tokens per question:

| approach | `32a5bd4` | first measurement (held out) | now |
|---|---|---|---|
| raw grep+read | 33 (29), 2,695 | 33 (29), 2,695 | 33 (29), 2,695 |
| Graphify (vendored renderer) | 13 (13), 1,200 | 13 (13), 1,200 | 13 (13), 1,200 |
| Verinoda analyze | 18 (17), 672 | 25 (24), 774 | **41 (39)**, 1,025 |
| Verinoda retrieve (JSON) | 25 (25), 1,327 | 32 (32), 1,396 | **43 (43)**, 1,430 |
| Verinoda retrieve (text) | 34 (32), 1,381 | 46 (44), 1,431 | **48 (45)**, 1,484 |

No approach stated any of the 12 negative facts. Raw grep+read is strong here
because the example is small (37 files) and its identifiers are in the
questions; it still missed 17 facts at almost twice the context. Result files:
`benchmarks/results/mods-2026-09-24/` (`glow_mod_at_32a5bd4.json`,
`glow_mod_first_measurement.json`, `glow_mod.json`).

**A second held-out set, `forge_mod`** (`examples/forge_mod`: a fictional
NeoForge mod in Java and Kotlin with recipes of a custom type, tags, worldgen
and a biome modifier, a config spec, a network payload, lang files in both
languages and JUnit tests; 14 questions, 7 of them Turkish, 68 facts, 19
negatives). Another agent wrote it without running Verinoda on it, after all
of the changes above, and it was measured once, with nothing tuned on it
(`forge_mod.json`, `forge_mod_at_32a5bd4.json`; `repeat = 1`):

| approach | `32a5bd4` | now |
|---|---|---|
| raw grep+read | 36 (30), 4,471 | 36 (30), 4,471 |
| Graphify (vendored renderer) | 12 (12), 1,344 | 12 (12), 1,344 |
| Verinoda analyze | 26 (19), 672 | **38 (30)**, 816 |
| Verinoda retrieve (JSON) | 28 (28), 1,334 | **45 (44)**, 1,429 |
| Verinoda retrieve (text) | 43 (36), 1,375 | **63 (57)**, 1,466 |

No approach stated any of the 19 negatives. The text format answered 10 of
the 14 questions completely (raw 3, Graphify 0). The weakest spots: a data-only
worldgen question in Turkish (q05, text 3/4, JSON and analyze 0/4) and the
recipe-type question q03 (text 4/6, analyze 1/6). Kotlin had no extra call
pass then: Kotlin -> Java calls came only from the extractor. The pass now
covers Kotlin callers too (added after this measurement, so `forge_mod` is
in-sample for it: analyze 38 -> 39, the other cells and sets unchanged).

**Second review round (later the same night).** Four more review agents went
through every change after `ea93f92` and reproduced each finding. Fixed, with
tests: short "stems" of Turkish words (`çalışıyor` -> `cal`, `veri` -> `ver`:
a stem is now at least 4 letters, 5 for an English word typed into a Turkish
question such as `login`); the Turkish derivation guard had also blocked
English abbreviations (`application` -> `app`, `deduplicate` -> `dedup`); `öl`
(die) also matched `ölçü` (measure); an inferred link now passes only half of
a code unit's graph prior to a data unit; the alignment check flagged
unchanged Java/Kotlin files with long annotation blocks for good; a Java
import with a trailing comment was ignored; Kotlin raw strings were read as
code and Kotlin properties were not followed; `update` rebuilt the graph for
edits to documents and data files; `doctor` did not know the mismatch marker.
In the reference resolver, ordinary prose before a number ("took 2.5
seconds", "macOS 14.2") became a package reference and made the result
`unresolved`, and every "word N.N" was sent to three public registries: a
name before a version is now looked up only with `--network on`, common words
and units are never package names, and only a clearly written project name
("the X package", a name before `v0.3`) is asked about. Fractions, protocols,
word pairs and folders (`1/3`, `HTTP/2`, `read/write`, `services/billing`) are
no longer repositories, a bare PR number keeps the local origin over a
repository named in another sentence, and a sentence with two repositories
binds each number to the one written after it. The reference-tree suggestion
of `setup` matched bare file names (every pair of Django apps, monorepo
packages) and took 259 s on 48,000 files: it now compares relative paths and
file contents, in 0.3 s.

Added in the same round: Turkish labels from the repository's own locale files
("Kor Ocağı" under `block.x.ember_forge`) expand to the identifier of their
key at full weight, instead of every label their words occur in; the seed
dictionary has world-generation words (`biyom`, `dünya`, `cevher`, `oluş`,
`üret`, ...); a loaded lexicon is reused until its file changes (0.16 s per
load on a large project, up to three loads per Turkish question before). After
looking at the failures of `forge_mod` q05, q09 and q11, that set is
in-sample for these changes: text 63 -> **64**, JSON 45 -> **50**, analyze
38 -> **40** (`forge_mod_review2.json`); `glow_mod` is unchanged
(`glow_mod_review2.json`), and on the five earlier sets only
`graphify_core_tr` changed (text 25 -> 26). Tried and not kept: counting all
expansions of one question word as one term (`graphify_core_tr` -1 text, -1
analyze) and splitting a registry class's prior among the items it names
(`forge_mod` JSON -4).

Then a question that spells a resource id (`emberforge:forging`,
`#emberforge:forge_fuels`) reaches every passage that writes that id, and the
data file defined as it, as surely as a spelled identifier reaches its code;
only namespaces the index knows as resource namespaces count, so `key:value`
text in other repositories is untouched. `forge_mod` q03 (the recipe type
whose two recipes were missed): text 4 -> 6, analyze 1 -> 2; the set: text
64 -> **66**, analyze 40 -> **41**, JSON 50 (q03 +1, q04 -1). Every other set
unchanged.

Two more fixes from the private set: a question that writes `Owner.name`
("who calls Wisp.spawn?") gave every method called `spawn` the score of a
spelled identifier; now only the one that `Owner` owns (its qualified name, or
its file for a module function) gets it, unless no such method exists. And the
text format skipped every unit inside a unit it had already shown, although a
long class section prints only two passages: a method of that class, ranked
second with its callers, was never shown. It now skips only lines already
printed. The private set's text format went from 22 to 31 of 39 facts (three
"who calls ..." questions from 0/4, 0/4 and 2/4 to 4/4 each, one other -1;
in-sample: found while looking at those questions); `forge_mod` text 66 -> 65
(q08: more sections share the budget); every other set unchanged.

Last of the night: two adjacent question words that the code writes as one
name ("needle marker" -> `needle_marker`, "order service" -> `OrderService`,
a Turkish word through its confirmed stem) count as that name, spelled; a
dotted token such as `graph.json` is not two words. `forge_mod` analyze
41 -> 42 and text 65 -> 66, `glow_mod` analyze 41 -> 43 and JSON 43 -> 44,
`heldout_repoatlas` JSON 20 -> 21, the private set text 31 -> 32 and JSON
23 -> 24; `graphify_core` analyze 30 -> 29 ("query terms" now also names
`_query_terms`). The other sets unchanged.

**Third review, and the end of the night.** Three more review agents went
through the commits after the second round. Their findings, each reproduced
and fixed with a test: the Java/Kotlin method-reference edges (`Cls::m` as a
call, commit `d70b801`) were not neutral as its message said. Those edges are
stored when a project is scanned, and the per-change measurement had reused
indexes built earlier, so it never saw them; built with them, `forge_mod` JSON
was 46 (q11) and `glow_mod` analyze 42. They also let a reference hide a later
direct call to the same method, could be lifted to a verified "calls" with a
SCIP index, resolved `this::m` inside anonymous classes to the outer class,
and read Java text blocks as code. The commit was reverted. The Turkish stem
rule of `30847a1` (the stem most units use) picked short English words
("eventleri" -> `even`): the longest indexed stem wins again, and the shorter
one only when the longer is the English stem of another inflection (the word
goes on with the `e` it dropped: "melekten" -> `melek`, not `melekt`). A
spelled resource id is matched as a whole id (`minecraft:item` no longer
matches `minecraft:item/generated`), each file is read once, data files
first, capped by files. A lowercase owner (`store.save`) is a module or
package, a capitalized one (`Store.save`) a class. Every measurement from here
on builds each index from scratch.

One official run of every set on the final code
(`benchmarks/results/mods-2026-09-24/final/`, `repeat = 1`, each corpus
indexed from scratch). Facts found; the first number is this update's earlier
result file for the set (`forge_mod`: its held-out measurement), bold where
the final run found more:

| set | facts | raw | Graphify | analyze | JSON | text |
|---|---|---|---|---|---|---|
| `forge_mod` | 68 | 36 | 12 | 38 -> **42** | 45 -> **50** | 63 -> **66** |
| `glow_mod` | 50 | 33 | 13 | 41 -> **43** | 43 -> **44** | 48 |
| `orders_app` | 32 | 31 | 16 | 31 | 31 | 32 |
| `orders_app_tr` | 32 | 10 | 6 | 30 | 28 | 32 |
| `graphify_core` | 37 | 4 | 7 | 30 -> 29 | 27 | 36 |
| `graphify_core_tr` | 37 | 5 | 2 | 16 | 15 | 25 -> **26** |
| `heldout_repoatlas` | 33 | 9 | 8 | 13 | 20 -> **21** | 25 |

Against the pre-mod baseline (`dogfood-2026-09-23`) the five earlier sets
differ in four cells: `graphify_core` analyze 30 -> 29, `graphify_core_tr`
JSON 16 -> 15 and text 25 -> 26, `heldout_repoatlas` JSON 20 -> 21. No
approach stated a negative on the mod sets; the one analyze negative on
`orders_app` and `orders_app_tr` is the one they had before. The private set,
run the same way: text 32, JSON 24, analyze 24 of 39 facts (22 / 23 / 24
before this round).

**The five earlier sets** (regression check, same harness, `repeat = 1`, no
upstream CLI, against `dogfood-2026-09-23/`): 24 of the 25 Verinoda cells and
every raw and Graphify cell found exactly the same number of facts. One cell
lost one fact: `graphify_core_tr` retrieve JSON 16 -> 15. On that question
(g08) the ranking now folds the identical `update.md` copies of the skill
folders into one item; the next items then include two call edges whose
characters pushed the `more` list, which held the `dispatch_command` locator,
out of the JSON budget. The text format of the same question is unchanged.
Before the generic-data factor, `heldout_repoatlas` text lost a fact to the
1,290-line seed dictionary JSON of that corpus, which outranked the code; that
is what the factor is for.

**Later the same night** (in the "now" column above): the `update`
correctness fix below, a Turkish stem rule (an inflected word also searches
its longest indexed stem: "bıçağının" -> `bicagi`), `öl` (die) matched as
written so it no longer collides with `ol` (be), and a PageRank prior for data
units through their links. Measured on all seven sets against the run before
them: `glow_mod` JSON 41 -> 43 and analyze 39 -> 41 (the exact-spelling
entries now also reach the question plan's glosses), the private set JSON +1,
every other cell unchanged. A 0.8 factor for test files was tried in the same round and
dropped: +3 facts on `heldout_repoatlas`, but -2 on `glow_mod` and -1 on
`orders_app_tr`, whose behaviour questions cite tests.

**`update` lost cross-file edges (fixed).** The upstream incremental rebuild
extracts only the changed files and resolves imports and calls within that
batch, so after editing `orders/service.py` its 4 edges into `pricing.py` and
`repository.py` were missing (and dangling references appeared) until the next
full scan. `update` now re-extracts the whole corpus from the AST cache: about
33 s instead of 20 s on Verinoda's own ~2,100 files; skipping the upstream
report's "suggested questions", which Verinoda never reads, took a full
rebuild there from 42 s to 33 s. `tests/test_workflow_index.py` checks the
edges after an edit.

**A private mod.** The work was driven by the owner's own Fabric mod (Java,
a data pack, a yml config and the original Paper plugin kept as a reference
tree). Its question set and results are not published.

**Costs.** Scan time did not grow measurably (`graphify_core` cold 14.7 s,
round 3 14.7 s; the data pass reads the non-graph files once more, about 0.3 s
on Verinoda's own repository). Retrieval medians stayed within noise of the
previous round (`graphify_core` text 0.141 s, `heldout_repoatlas` 0.135 s).
Analyze contexts grew on `glow_mod` (774 -> 1,025 tokens) with the link-chain
claims. The search index schema is now 4; an older `search.db` is rebuilt on
first use.

**Not done / known limits.** Resource kinds are inferred from a fixed table
of command words and JSON keys; an id in plain Java is "kind not stated"
unless one file has it. Bare names count only as an argument of an id
constructor or of a helper whose name says what it loads. Folding merges
Turkish stems such as `öl` (die) and `ol` (be): `öl` is now matched as written
(an `exact` section of the seed dictionary), but the question plan itself
still reads folded words. The extra call pass covers Java and Kotlin callers,
not Scala or Groovy.

## Update 2026-09-23: dogfooding fixes

Running Verinoda on its own repository exposed two problems, fixed in commit
`8eb47ff`:

1. **Turkish question understanding.** The seed dictionary missed common
   software words (`komut`, `kök`, `izin`, `tara`, `kaldır`, `ev dizini`,
   `zaman aşımı`, ...), did not match softened stems (`reddet` ->
   `reddediyor`, `istek` -> `isteği`) or u-harmony suffixes (`grubu`), and a
   two-word key such as `ev dizin` never matched because the word `ev` was
   dropped as too short before the lookup.
2. **Ranking.** The question "home directory" ranked `find_repo_root`, whose
   docstring says "The home directory ...", 73rd: functions that repeat a
   `home` parameter scored higher. A passage of code (not prose) or a unit
   name in which two adjacent words of the question are also adjacent now
   counts 1.3 times (`search_index.PROX_BOOST`).

**How the ranking variant was chosen.** Four variants (factor 1.5 or 1.3;
at most two tokens apart or strictly adjacent; with or without prose
passages) were run once on all five sets. The kept variant found the most
facts over all five sets (392 against 376 before and 378, 385 and 386 for the
others) and lost no fact in any set/approach cell. The held-out set took part
in that choice, so **for this change `heldout_repoatlas` is in-sample**.
Documentation passages were excluded: the 1.5 / two-token variant lost 6
facts on `graphify_core_tr` (retrieve text 25 -> 19, 5 of them on the question
about `graphify update`) when prose passages got the factor and 1 when they
did not (25 -> 24). Documentation often puts command names side by side.

**Measured.** Result files: `benchmarks/results/dogfood-2026-09-23/` (same
machine and harness as round 3, `repeat = 2`, the real upstream CLI). The
five main runs and the budget sweep ran on the clean commit `8eb47ff`
(17:35-17:47 UTC), then the five main runs on the previous commit `923bb8c`
(17:47-17:52 UTC, `previous-commit/`), one after another with nothing else
running. Facts found (pinpointed), `923bb8c` -> `8eb47ff`:

| set | Verinoda analyze | retrieve (JSON) | retrieve (text) |
|---|---|---|---|
| `orders_app` | 31 (31) -> 31 (31) | 31 (31) -> 31 (31) | 32 (32) -> 32 (32) |
| `graphify_core` | 26 (16) -> **30** (15) | 27 (18) -> 27 (18) | 35 (21) -> **36** (22) |
| `heldout_repoatlas` | 11 (7) -> **13** (7) | 17 (12) -> **20** (15) | 22 (16) -> **25** (18) |
| `orders_app_tr` | 30 (30) -> 30 (30) | 28 (28) -> 28 (28) | 32 (32) -> 32 (32) |
| `graphify_core_tr` | 13 (12) -> **16** (13) | 16 (12) -> 16 (12) | 25 (15) -> 25 (15) |

Raw grep+read and both Graphify renderers found the same facts with the same
context sizes in both runs. The `923bb8c` numbers equal the round-3 numbers
below (that commit did not change retrieval).

Costs, from the same two runs:

* **Context size** (tokens per question, chars/4): analyze 1,154 -> 1,181
  on `orders_app`, 587 -> 652 on `heldout_repoatlas`, 863 -> 971 on
  `graphify_core_tr`, and 1,021 -> 985 on `graphify_core`. Retrieve sizes
  moved by at most 9.
* **Latency** (cold median per question): retrieve text 0.110 -> 0.140 s on
  `graphify_core`, 0.109 -> 0.135 s on `heldout_repoatlas`, 0.161 -> 0.198 s
  on `graphify_core_tr`, unchanged on `orders_app` (0.010 s). The approaches
  whose code did not change moved by at most 7% between the two runs. The
  adjacency check re-reads up to 300 candidate passages from disk per
  question. Analyze cold medians rose (`graphify_core` 1.316 -> 2.061 s) while
  its warm medians fell (1.038 -> 0.821 s); two repeats cannot separate that
  from noise.
* **Pinpointing:** analyze on `graphify_core` pinpointed 15 facts instead of
  16 while finding 30 instead of 26.
* **Timing drift:** unchanged code (the upstream CLI) measured 10-16% slower
  in this session than in round 3. Compare times only within this section.

Budget sweep, retrieve text, facts at 750 / 1,500 / 3,000 tokens (round 3 ->
now): `graphify_core` 32 -> 33 / 35 -> 36 / 37 -> 37; `heldout_repoatlas`
18 -> 22 / 22 -> 25 / 32 -> 32; `graphify_core_tr` 19 -> 19 / 25 -> 25 /
26 -> 26 (`dogfood-2026-09-23/sweep/`).

**Still open.** The question that exposed these problems ("init ve scan
komutları ev dizininde çalıştırılınca reddediyor mu?") is still not
answered. The plan now reads it correctly (`komutları=command`,
`ev dizininde=home`, `reddediyor=reject`), but retrieval does not reach
`paths._is_home_or_above` or `find_repo_root`, and the command names `init`
and `scan` are not mapped to their handlers `cmd_init` and `cmd_scan`.

The sections below describe the round-3 measurement unless they say
otherwise.

## Summary

Measured on one Windows 11 machine (AMD64, 6 logical CPUs, CPython 3.12.0),
`repeat = 2`, with the real upstream Graphify CLI (`graphify 0.9.65`) and no
model in the loop. Tokens are **estimated** as chars/4, not counted with a
tokenizer. Five question sets: two English sets used since round 1
(`orders_app`, `graphify_core`), a held-out set on Verinoda's own source
(`heldout_repoatlas`), and Turkish paraphrases of the two English sets. The
provenance of each set, and whether it is in-sample, is in
[Question sets](#question-sets).

* **The new model-facing format, `verinoda query` text
  (`verinoda_retrieve_text`), found the most gold facts on every set.** Its
  facts per 1,000 tokens were the highest on three of the five sets. On
  `graphify_core` and `heldout_repoatlas`, Verinoda analyze had a higher
  ratio (2.83 vs 2.73 and 2.34 vs 1.97) with fewer facts (26 vs 35 and 11 vs
  22).
  * `orders_app`: 32/32 facts at 887 tokens per question (3.61 facts per 1k
    tokens). Raw grep+read: 31/32 at 1,224 (2.53). Graphify: 16/32 at 2,234
    (vendored renderer, 0.72) and 15/32 at 1,651 (CLI, 0.91).
  * `graphify_core` (in-sample for Verinoda retrieval): 35/37 at 1,422
    tokens (2.73), 21 of them pinpointed. Graphify: 7/37 (0.47 and 0.40). Raw:
    4/37 at 5,989 tokens (0.07).
  * `heldout_repoatlas`: 22/33 at 1,398 tokens (1.97), 16 pinpointed.
    Graphify: 8/33 at about 1,645 tokens (0.61 for both renderers; only 2
    pinpointed). Raw: 9/33 at 5,992 (0.19).
* **The same retrieval as JSON (`verinoda_retrieve`) found fewer facts at
  the same size**: 31/32, 27/37 and 17/33. Verinoda analyze found 31/32,
  26/37 and 11/33, with the smallest contexts on the two larger sets (1,021
  and 587 tokens per question).
* **Before round 3 → now** (question sets, gold, corpora, Graphify and raw
  unchanged; their fact counts and context sizes reproduce exactly):
  * `graphify_core`: analyze 18 → 26 of 37, retrieve JSON 18 → 27 of 37.
    Retrieve (JSON) cold median went from 1.373 s to 0.106 s per question, and
    analyze's from 1.889 s to 1.215 s (warm 1.685 s → 0.679 s).
  * `orders_app`: analyze **regressed**, from 32/32 at 821 tokens to 31/32 at
    1,154 tokens (3.90 → 2.69 facts per 1k tokens).
  * One-off index cost rose on the large corpus: Verinoda scan on
    `graphify_core` took 14.66 s cold (6.72 s before), because scan now also
    builds the search index, the lexicon and symbol facts. Warm: 6.01 s
    (5.81 s before). `graphify update .` took 7.53 s cold.
* **Budget sweep** (same character cap for every approach): at 750 tokens,
  retrieve text kept 32/37 on `graphify_core` at 644 tokens per question
  (5.52 facts per 1k) and 18/33 on `heldout_repoatlas`. At 3,000 tokens it
  reached 37/37 and 32/33. Graphify went from 5 to 8–9 of 37 and from 5 to 10–11
  of 33 across the same range. Raw stayed at 4/37 on `graphify_core`.
  Graphify's contexts exceeded the character cap on most questions: its
  truncation notices sit outside its budget, and a graph whose nodes all fit
  is never cut.
* **Turkish (in-sample, same gold).** On `orders_app_tr`, retrieve text
  found 32/32 (no gap to English), analyze 30/32 (−1) and retrieve JSON 28/32
  (−3), while Graphify lost 10 facts and raw 21. On `graphify_core_tr`, retrieve
  text found 25/37 (−10), analyze 13/37 (−13), Graphify 2/37 (−5) and raw
  5/37 (+1).
* **Wrong statements.** No approach stated any of the 7 `graphify_core`
  negative facts. On `orders_app` the negative "`test_empty_order_rejected`
  exercises the discount calculation" still matches 2 analyze claims. Both
  are now labelled `weak_inference`, so 0 negatives are presented as findings
  (round 2: the corresponding 2 claims were `strong_inference` findings). Every
  `calls`/`uses` relation that any approach asserted with a line cites a line
  that names its target. For retrieve text that is 44/44, 145/145 and 187/187
  outline entries on the three English-language sets.
* **Trust harnesses.**
  * History replay of 300 upstream Graphify commits (26,097 claims): stale
    recall 1.0, 0 claims silently wrong, and false-stale rate 1.0 under the
    old file rule vs 0.1217 under facet dependencies.
  * 10,259 of 10,259 moved citations were relocated exactly.
  * Mutation suite: 38/38 verdicts.
  * Critique evaluation (in-sample labelled set): 20 of 20 claims presented
    as verified are true, 22 of 22 false claims are flagged, 0 of 23 true
    claims are lowered, and 0 of 10 benchmark negatives are stated as
    verified.
* **Not measured:** answer accuracy with a model in the loop (no API key),
  model cost, other operating systems and machines, memory, a long-running MCP
  server, history-based answers. See [Not measured](#not-measured).

## Results per set

Columns: *facts found* = gold facts present in the delivered context;
*pinpointed* = found through a locator spanning ≤ 30 lines; *all-facts Qs* =
questions with every gold fact present; *negative facts stated* = distinct
negative facts matched by an approach's assertions / negative facts checked
(for analyze, how many of the matching claims had a *finding* status);
*tokens/Q* = mean delivered tokens per question (chars/4); *facts/1k tok* =
facts found per 1,000 delivered tokens; *cold / warm* = median seconds per
question in pass 1 / pass 2. Index cost is one-off and not included in the
per-question times.

### `orders_app` (10 questions, 32 gold facts, 15 negative facts; in-sample)

| approach | facts found | pinpointed | all-facts Qs | negative facts stated | tokens/Q | facts/1k tok | cold s/Q | warm s/Q |
|---|---|---|---|---|---|---|---|---|
| Verinoda retrieve (text) | 32/32 | 32 | 10/10 | 0/15 | 887 | 3.61 | 0.009 | 0.009 |
| Verinoda retrieve (JSON) | 31/32 | 31 | 9/10 | 0/15 | 1396 | 2.22 | 0.009 | 0.009 |
| Verinoda analyze | 31/32 | 31 | 9/10 | 1/15 (0 as findings) | 1154 | 2.69 | 0.164 | 0.098 |
| Graphify query (vendored, depth 3) | 16/32 | 16 | 2/10 | 0/15 | 2234 | 0.72 | 0.004 | 0.004 |
| Graphify CLI (upstream, depth 2) | 15/32 | 15 | 2/10 | 0/15 | 1651 | 0.91 | 0.353 | 0.348 |
| raw grep+read | 31/32 | 31 | 9/10 | n/a | 1224 | 2.53 | 0.016 | 0.016 |

Index cost: Verinoda scan 0.30 s cold / 0.17 s warm (41 nodes, 74 edges);
`graphify update .` 0.51 s / 0.47 s. Verinoda analyze made 121 claims
(statically_verified 85, primary_source_verified 9, strong_inference 23,
weak_inference 4; verified share 0.777); every weak claim was delivered with
its label, 3 unknowns were reported, and the warm pass reused 121/121 claims.

### `graphify_core` (9 questions, 37 gold facts, 7 negative facts; in-sample)

Upstream Graphify at 20a20d30: `graphify/` plus two test modules and
`pyproject.toml` (226 files, 4.7 MB).

| approach | facts found | pinpointed | all-facts Qs | negative facts stated | tokens/Q | facts/1k tok | cold s/Q | warm s/Q |
|---|---|---|---|---|---|---|---|---|
| Verinoda retrieve (text) | 35/37 | 21 | 7/9 | 0/7 | 1422 | 2.73 | 0.105 | 0.109 |
| Verinoda retrieve (JSON) | 27/37 | 18 | 3/9 | 0/7 | 1433 | 2.09 | 0.106 | 0.105 |
| Verinoda analyze | 26/37 | 16 | 3/9 | 0/7 (0 as findings) | 1021 | 2.83 | 1.215 | 0.679 |
| Graphify query (vendored, depth 3) | 7/37 | 7 | 1/9 | 0/7 | 1667 | 0.47 | 0.260 | 0.259 |
| Graphify CLI (upstream, depth 2) | 7/37 | 7 | 1/9 | 0/7 | 1940 | 0.40 | 0.574 | 0.576 |
| raw grep+read | 4/37 | 4 | 1/9 | n/a | 5989 | 0.07 | 0.289 | 0.284 |

Index cost: Verinoda scan 14.66 s cold / 6.01 s warm (4,092 nodes, 8,597
edges); `graphify update .` 7.53 s / 6.11 s (same node and edge counts).
Analyze made 92 claims (statically_verified 74, observed 12,
primary_source_verified 4, strong_inference 1, weak_inference 1; verified
share 0.978), 3 unknowns, warm reuse 92/92. The 12 `observed` claims are call
relations with a definitive jedi resolution as evidence (`static_resolution`).

### `heldout_repoatlas` (8 questions, 33 gold facts, no negative facts; held out)

Verinoda's own source at commit 7371990 (106 files, 2.2 MB), written before
the retrieval prototype ran on it; see [Question sets](#question-sets).

| approach | facts found | pinpointed | all-facts Qs | negative facts stated | tokens/Q | facts/1k tok | cold s/Q | warm s/Q |
|---|---|---|---|---|---|---|---|---|
| Verinoda retrieve (text) | 22/33 | 16 | 2/8 | n/a (none in set) | 1398 | 1.97 | 0.101 | 0.109 |
| Verinoda retrieve (JSON) | 17/33 | 12 | 2/8 | n/a | 1441 | 1.47 | 0.101 | 0.106 |
| Verinoda analyze | 11/33 | 7 | 1/8 | n/a | 587 | 2.34 | 0.490 | 0.353 |
| Graphify query (vendored, depth 3) | 8/33 | 2 | 0/8 | n/a | 1647 | 0.61 | 0.210 | 0.226 |
| Graphify CLI (upstream, depth 2) | 8/33 | 2 | 0/8 | n/a | 1644 | 0.61 | 0.528 | 0.526 |
| raw grep+read | 9/33 | 3 | 1/8 | n/a | 5992 | 0.19 | 0.133 | 0.130 |

Index cost: Verinoda scan 11.35 s cold / 4.11 s warm (3,613 nodes, 9,728
edges); `graphify update .` 6.22 s / 5.07 s. Analyze made 41 claims
(statically_verified 28, primary_source_verified 2, strong_inference 4,
weak_inference 3, contradicted 4; verified share 0.732), 2 unknowns. The 4
`contradicted` claims are 2 distinct claims, each delivered in two answers,
that a test "reads environment variable VERINODA_EXPERIMENT / VERINODA_PROBE".
The cited lines are inside string literals that the test writes to a probe
file, so the test itself reads nothing. Analysis generated these claims and
critique refuted them; they reach the model labelled `contradicted`.

### `orders_app_tr` (Turkish paraphrases of `orders_app`; in-sample)

| approach | facts found | pinpointed | all-facts Qs | negative facts stated | tokens/Q | facts/1k tok | cold s/Q | warm s/Q |
|---|---|---|---|---|---|---|---|---|
| Verinoda retrieve (text) | 32/32 | 32 | 10/10 | 0/15 | 873 | 3.67 | 0.011 | 0.011 |
| Verinoda retrieve (JSON) | 28/32 | 28 | 7/10 | 0/15 | 1415 | 1.98 | 0.011 | 0.011 |
| Verinoda analyze | 30/32 | 30 | 9/10 | 1/15 (0 as findings) | 1228 | 2.44 | 0.155 | 0.108 |
| Graphify query (vendored, depth 3) | 6/32 | 6 | 0/10 | 0/15 | 1218 | 0.49 | 0.003 | 0.003 |
| Graphify CLI (upstream, depth 2) | 5/32 | 5 | 0/10 | 0/15 | 926 | 0.54 | 0.354 | 0.349 |
| raw grep+read | 10/32 | 10 | 2/10 | n/a | 318 | 3.15 | 0.016 | 0.016 |

Raw's 3.15 facts per 1k tokens comes from very small contexts: for 6 of the
10 Turkish questions it delivered 22–129 tokens and 0 facts, because the
Turkish words match (almost) nothing in the English code (see
[Per-question results](#per-question-results)). Graphify answered "No
matching nodes found." to 5 of the 10 questions.

### `graphify_core_tr` (Turkish paraphrases of `graphify_core`; in-sample)

| approach | facts found | pinpointed | all-facts Qs | negative facts stated | tokens/Q | facts/1k tok | cold s/Q | warm s/Q |
|---|---|---|---|---|---|---|---|---|
| Verinoda retrieve (text) | 25/37 | 15 | 5/9 | 0/7 | 1453 | 1.91 | 0.153 | 0.155 |
| Verinoda retrieve (JSON) | 16/37 | 12 | 1/9 | 0/7 | 1436 | 1.24 | 0.149 | 0.156 |
| Verinoda analyze | 13/37 | 12 | 2/9 | 0/7 (0 as findings) | 863 | 1.67 | 1.512 | 0.666 |
| Graphify query (vendored, depth 3) | 2/37 | 2 | 0/9 | 0/7 | 1288 | 0.17 | 0.257 | 0.253 |
| Graphify CLI (upstream, depth 2) | 2/37 | 2 | 0/9 | 0/7 | 1526 | 0.15 | 0.570 | 0.574 |
| raw grep+read | 5/37 | 4 | 1/9 | n/a | 5991 | 0.09 | 0.386 | 0.384 |

Index cost: Verinoda scan 14.82 s cold / 5.56 s warm; `graphify update .`
7.38 s / 6.13 s (same corpus as `graphify_core`).

## Before round 3 vs now

`before-round3/` is the round-2 measurement: Verinoda at `05890a1` plus the
uncommitted round-2 fix round, package diff hash `f8d30bb9…` (measured
2026-09-22 21:13 UTC; not identical to commit `a42a57c`, which also carries
early round-3 files). *Now* is `7371990` plus the uncommitted integration and
measurement changes, diff hash `1a8dd47e…`. The question wording, gold facts
and negatives of both sets are the same JSON content in both runs (a test
pins the sha256 of their canonical dump); only a `provenance` block was added
to the set files. The
corpora, the vendored Graphify, the upstream CLI and the raw baseline did not
change, and their fact counts and context sizes are identical in both runs;
their per-question medians moved by up to 12% (raw warm on `graphify_core`:
0.322 s → 0.284 s) and the CLI's one-off `update` on the small corpus by 34%
(0.77 s → 0.51 s), which is this machine's run-to-run noise.
`retrieve (text)` did not exist before. Arrows read *before → now*.

### `orders_app`

| approach | facts found | pinpointed | neg. matched | tokens mean | facts/1k tok | cold s (median) | warm s (median) |
|---|---|---|---|---|---|---|---|
| raw grep+read | 31/32 -> 31/32 | 31 -> 31 | n/a -> n/a | 1224 -> 1224 | 2.53 -> 2.53 | 0.016 -> 0.016 | 0.016 -> 0.016 |
| Graphify query (vendored, depth 3) | 16/32 -> 16/32 | 16 -> 16 | 0/15 -> 0/15 | 2234 -> 2234 | 0.72 -> 0.72 | 0.004 -> 0.004 | 0.004 -> 0.004 |
| Graphify CLI (upstream, depth 2) | 15/32 -> 15/32 | 15 -> 15 | 0/15 -> 0/15 | 1651 -> 1651 | 0.91 -> 0.91 | 0.374 -> 0.353 | 0.371 -> 0.348 |
| Verinoda analyze | 32/32 -> 31/32 | 32 -> 31 | 1/15 -> 1/15 | 821 -> 1154 | 3.90 -> 2.69 | 0.240 -> 0.164 | 0.096 -> 0.098 |
| Verinoda retrieve (JSON) | 30/32 -> 31/32 | 30 -> 31 | 0/15 -> 0/15 | 1287 -> 1396 | 2.33 -> 2.22 | 0.007 -> 0.009 | 0.007 -> 0.009 |
| Verinoda retrieve (text) | n/a -> 32/32 | n/a -> 32 | n/a -> 0/15 | n/a -> 887 | n/a -> 3.61 | n/a -> 0.009 | n/a -> 0.009 |

* Analyze lost `q10.status` ("create_order_handler turns it into HTTP 400",
  `orders/api.py:19-20`) and delivers 41% more tokens (821 → 1,154). It made 121 claims instead of
  95, and its strong_inference claims went from 14 to 23.
* The matching negative (q04) is now stated only by `weak_inference`
  claims: 0 claims presented as findings, against 2 before.
* Index: Verinoda scan 0.56 s → 0.30 s cold, 0.18 s → 0.17 s warm;
  `graphify update .` 0.77 s → 0.51 s cold (unchanged code, so noise).

### `graphify_core`

| approach | facts found | pinpointed | neg. matched | tokens mean | facts/1k tok | cold s (median) | warm s (median) |
|---|---|---|---|---|---|---|---|
| raw grep+read | 4/37 -> 4/37 | 4 -> 4 | n/a -> n/a | 5989 -> 5989 | 0.07 -> 0.07 | 0.294 -> 0.289 | 0.322 -> 0.284 |
| Graphify query (vendored, depth 3) | 7/37 -> 7/37 | 7 -> 7 | 0/7 -> 0/7 | 1667 -> 1667 | 0.47 -> 0.47 | 0.260 -> 0.260 | 0.280 -> 0.259 |
| Graphify CLI (upstream, depth 2) | 7/37 -> 7/37 | 7 -> 7 | 0/7 -> 0/7 | 1940 -> 1940 | 0.40 -> 0.40 | 0.595 -> 0.574 | 0.587 -> 0.576 |
| Verinoda analyze | 18/37 -> 26/37 | 13 -> 16 | 0/7 -> 0/7 | 767 -> 1021 | 2.61 -> 2.83 | 1.889 -> 1.215 | 1.685 -> 0.679 |
| Verinoda retrieve (JSON) | 18/37 -> 27/37 | 15 -> 18 | 0/7 -> 0/7 | 1433 -> 1433 | 1.40 -> 2.09 | 1.373 -> 0.106 | 1.371 -> 0.105 |
| Verinoda retrieve (text) | n/a -> 35/37 | n/a -> 21 | n/a -> 0/7 | n/a -> 1422 | n/a -> 2.73 | n/a -> 0.105 | n/a -> 0.109 |

* g08 ("How does graphify update rebuild the code graph without an LLM?"),
  0/6 for every approach before, is now 6/6 for analyze and retrieve text
  (retrieve JSON 1/6; Graphify and raw 0/6). g02 (the query command's call
  chain) is 6/6 for both retrieve formats and 1/6 for analyze.
* Analyze: 81 → 92 claims; 0 contradicted in both runs; warm reuse 81/81 →
  92/92.
* Index: Verinoda scan 6.72 s → 14.66 s cold and 5.81 s → 6.01 s warm;
  `graphify update .` 7.69 s → 7.53 s cold (unchanged code).

## Budget sweep

Separate runs (`benchmarks/results/sweep/<set>.json`, `--sweep 750,1500,3000
--sweep-only`, repeat 2, real CLI). At a sweep point of N tokens every
approach gets the same cap of 4N characters (N tokens in the chars/4
estimate; for Graphify, `ceil(4N/3)` of its own tokens). Each cell is *facts
found / mean delivered tokens per question / facts per 1k tokens*.

**`orders_app`**

| approach | 750 tok | 1500 tok | 3000 tok |
|---|---|---|---|
| Verinoda retrieve (text) | 32/32 / 727 / 4.40 | 32/32 / 887 / 3.61 | 32/32 / 887 / 3.61 |
| Graphify query (vendored, depth 3) | 16/32 / 2234 / 0.72 | 16/32 / 2234 / 0.72 | 16/32 / 2157 / 0.74 |
| Graphify CLI (upstream, depth 2) | 15/32 / 1660 / 0.90 | 15/32 / 1651 / 0.91 | 15/32 / 1600 / 0.94 |
| raw grep+read | 27/32 / 715 / 3.77 | 30/32 / 1134 / 2.64 | 31/32 / 1224 / 2.53 |

**`graphify_core`**

| approach | 750 tok | 1500 tok | 3000 tok |
|---|---|---|---|
| Verinoda retrieve (text) | 32/37 / 644 / 5.52 | 35/37 / 1422 / 2.73 | 37/37 / 2259 / 1.82 |
| Graphify query (vendored, depth 3) | 5/37 / 925 / 0.60 | 7/37 / 1667 / 0.47 | 9/37 / 3172 / 0.32 |
| Graphify CLI (upstream, depth 2) | 5/37 / 924 / 0.60 | 7/37 / 1940 / 0.40 | 8/37 / 3277 / 0.27 |
| raw grep+read | 4/37 / 737 / 0.60 | 4/37 / 1488 / 0.30 | 4/37 / 2989 / 0.15 |

**`heldout_repoatlas`**

| approach | 750 tok | 1500 tok | 3000 tok |
|---|---|---|---|
| Verinoda retrieve (text) | 18/33 / 684 / 3.29 | 22/33 / 1398 / 1.97 | 32/33 / 2956 / 1.35 |
| Graphify query (vendored, depth 3) | 5/33 / 900 / 0.69 | 8/33 / 1647 / 0.61 | 10/33 / 3148 / 0.40 |
| Graphify CLI (upstream, depth 2) | 5/33 / 899 / 0.69 | 8/33 / 1644 / 0.61 | 11/33 / 3148 / 0.44 |
| raw grep+read | 2/33 / 728 / 0.34 | 5/33 / 1488 / 0.42 | 6/33 / 2991 / 0.25 |

**`orders_app_tr`**

| approach | 750 tok | 1500 tok | 3000 tok |
|---|---|---|---|
| Verinoda retrieve (text) | 31/32 / 735 / 4.22 | 32/32 / 873 / 3.67 | 32/32 / 873 / 3.67 |
| Graphify query (vendored, depth 3) | 6/32 / 1218 / 0.49 | 6/32 / 1218 / 0.49 | 6/32 / 1175 / 0.51 |
| Graphify CLI (upstream, depth 2) | 5/32 / 934 / 0.54 | 5/32 / 926 / 0.54 | 5/32 / 891 / 0.56 |
| raw grep+read | 9/32 / 253 / 3.56 | 10/32 / 318 / 3.15 | 10/32 / 318 / 3.15 |

**`graphify_core_tr`**

| approach | 750 tok | 1500 tok | 3000 tok |
|---|---|---|---|
| Verinoda retrieve (text) | 19/37 / 685 / 3.08 | 25/37 / 1453 / 1.91 | 26/37 / 2289 / 1.26 |
| Graphify query (vendored, depth 3) | 1/37 / 699 / 0.16 | 2/37 / 1288 / 0.17 | 2/37 / 3257 / 0.07 |
| Graphify CLI (upstream, depth 2) | 1/37 / 814 / 0.14 | 2/37 / 1526 / 0.15 | 2/37 / 2794 / 0.08 |
| raw grep+read | 4/37 / 734 / 0.61 | 4/37 / 1487 / 0.30 | 4/37 / 2970 / 0.15 |

What the sweep shows:

* **Retrieve text degrades gradually.** Going from 1,500 to 750 tokens it
  lost 3 of 37 facts on `graphify_core`, 4 of 33 on `heldout_repoatlas` and
  none on `orders_app`. Doubling to 3,000 tokens added 2 (37/37) and 10
  (32/33). On the small corpus it never needs its full budget: at 1,500 and
  3,000 tokens it delivered the same 887 tokens per question. Among the
  swept approaches it has the highest facts per 1k tokens in all 15 set ×
  budget cells; the closest runner-up is raw on the small corpus
  (`orders_app` at 750 tokens: 3.77 against 4.40).
* **The facts-per-1k ratio against Graphify at equal cap**, computed from the
  cells above, on the three English-language sets: at 750 tokens 4.40/0.72 =
  6.1x (`orders_app`, vendored), 5.52/0.60 = 9.2x (`graphify_core`) and
  3.29/0.69 = 4.8x (`heldout_repoatlas`); at 3,000 tokens 4.9x, 5.7x and 3.4x
  (against the vendored renderer; the CLI's cells are in the same tables).
  These are ratios of context contents, not of answer quality or cost.
* **Graphify does not stay inside the cap.** Its delivered context was
  longer than the character cap in 100 of 138 question × budget cells for the
  vendored renderer and 93 of 138 for the CLI (`score.chars` >
  `sweep.char_cap` in the sweep files). Retrieve text and raw never were. Two
  mechanisms, both by Graphify's design: the truncation notice and end marker
  sit outside its budget, and when every node it found fits, it returns the
  complete answer with all edges ("Complete answer over budget"). On
  `orders_app` the vendored renderer therefore delivered about 2,234 tokens
  per question at every budget.
* **Raw** reads its top-ranked files from their first line, so a larger cap
  only helps when the answer lies within the next lines of those files: 27 →
  31 of 32 on the small corpus, flat at 4/37 on `graphify_core`, where its
  top-ranked files (`serve.py`, `build.py`, `llm.py`, `cli.py`) have 2,400–4,800
  lines and even 3,000 tokens cover only their first ~170.

## Turkish vs English

Same gold facts, negatives and corpus; only the question text differs
(`question_en` in the set records the English original). The Turkish
paraphrases were written by the author of the Turkish rules
(`textnorm`, `lexicon`, `question_plan`) while tuning them, so these numbers
are **in-sample**: an upper bound on the Turkish behaviour, not an
independent measurement.

| set | approach | EN facts | TR facts | gap | EN tok/Q | TR tok/Q |
|---|---|---|---|---|---|---|
| orders_app | Verinoda retrieve (text) | 32/32 | 32/32 | 0 | 887 | 873 |
| orders_app | Verinoda retrieve (JSON) | 31/32 | 28/32 | −3 | 1396 | 1415 |
| orders_app | Verinoda analyze | 31/32 | 30/32 | −1 | 1154 | 1228 |
| orders_app | Graphify query (vendored, depth 3) | 16/32 | 6/32 | −10 | 2234 | 1218 |
| orders_app | Graphify CLI (upstream, depth 2) | 15/32 | 5/32 | −10 | 1651 | 926 |
| orders_app | raw grep+read | 31/32 | 10/32 | −21 | 1224 | 318 |
| graphify_core | Verinoda retrieve (text) | 35/37 | 25/37 | −10 | 1422 | 1453 |
| graphify_core | Verinoda retrieve (JSON) | 27/37 | 16/37 | −11 | 1433 | 1436 |
| graphify_core | Verinoda analyze | 26/37 | 13/37 | −13 | 1021 | 863 |
| graphify_core | Graphify query (vendored, depth 3) | 7/37 | 2/37 | −5 | 1667 | 1288 |
| graphify_core | Graphify CLI (upstream, depth 2) | 7/37 | 2/37 | −5 | 1940 | 1526 |
| graphify_core | raw grep+read | 4/37 | 5/37 | +1 | 5989 | 5991 |

* On the small domain corpus the gap is closed for retrieve text and nearly
  closed for analyze. The approaches without Turkish handling lose most of
  their facts (Graphify 10, raw 21).
* On `graphify_core_tr` a 10-fact gap remains for retrieve text. Three
  questions account for it: g01 (0/4 in Turkish vs 4/4 in English), g02 (1/6
  vs 6/6) and g04 (2/4 vs 3/4). g01 ("Graphify sorgu çıktısı token bütçesine
  sığmak için nerede kesiliyor?") was answered 0/4 by every approach.
* Analyze loses more than retrieval on this set (−13). g08 goes from 6/6 in
  English to 0/6 in Turkish for analyze, while retrieve text keeps 6/6.
* DESIGN.md §1.3 targets a Turkish–English gap "close to 0". That holds on
  `orders_app` (in-sample) and not on `graphify_core`.

## Trust harnesses

Question-independent harnesses (docs/DESIGN.md D30), results in
`benchmarks/results/trust/`.

**History replay** (`staleness_graphify_300.json`). The last 300 non-merge
commits of upstream Graphify touching `graphify/*.py` (HEAD 20a20d30) were
replayed: 388 file versions and 26,097 claims generated mechanically at each
parent, at most 40 per kind per file. A claim's truth "changed" when an
independent from-scratch oracle at the child disagrees; that happened for 449
of them. The *file rule* is Verinoda ≤ 0.1 (any cited file changed); the
*symbol mode* is the round-3 facet dependencies.

| kind | claims | truth changed | recall (file / symbol) | false-stale rate (file / symbol) | precision (file / symbol) |
|---|---|---|---|---|---|
| location | 12,094 | 380 | 1.0 / 1.0 | 1.0 / 0.0 | 0.0314 / 1.0 |
| relation | 12,770 | 69 | 1.0 / 1.0 | 1.0 / 0.2105 | 0.0054 / 0.0252 |
| config | 1,233 | 0 | 1.0 / 1.0 | 1.0 / 0.3633 | 0.0 / 0.0 |
| **all** | **26,097** | **449** | **1.0 / 1.0** | **1.0 / 0.1217** | **0.0172 / 0.1257** |

* Silently wrong (truth changed, not marked stale, status verified): **0**.
* Symbol mode marked 3,571 claims stale, against 26,097 under the file
  rule. 22,526 claims whose files changed but whose facets did not were
  re-bound to the new snapshot without a status change.
* Relocation: 10,259 of 10,259 citations that only moved were relocated to
  exactly the new lines (Wilson 95% 0.9996–1.0).
* Invalidation per commit: p50 109.9 ms, p95 355.8 ms, max 803.3 ms. Claim
  recording: 2.724 s per commit.
* Relation claims still have a 21% false-stale rate and 2.5% precision,
  because their dependency is the caller's whole body, not the call
  statement. Config claims had no truth changes in this history, so their
  precision is 0 by construction and their false-stale rate (36%) is the only
  informative number.
* The population only contains claims whose evidence lies in the modified
  files. Incoming relations from unchanged files were not sampled.

**Mutation suite** (`mutations.json`). There are 12 edit categories on a git
copy of `examples/orders_app`, among them whitespace, shift, a docstring edit,
a local rename, deleting or renaming the target, changing an import alias,
moving a definition, deleting a file, adding a same-name symbol and adding a
test. Each runs end to end through `workflow.update`. Result: 38 of 38
expected verdicts, stale recall 1.0, and `workflow.update` took 196–758 ms per
mutation.

**Critique evaluation** (`critique_eval.json`; in-sample). The set has 45
labelled claims about `examples/orders_app`: 23 true and 22 false. The false
ones include the 10 `orders_app` benchmark negatives and mutated true claims.
Each claim is created with the status a careless producer would request, then
challenged.

| measure | result | Wilson 95% |
|---|---|---|
| presented as verified that are true | 18/18 (was 20/20) | 0.82–1.0 |
| false claims flagged (lowered, contradicted or never verified) | 22/22 | 0.85–1.0 |
| false claims contradicted | 17/22 | 0.57–0.90 |
| true claims lowered (false alarm) | 0/23 | 0.0–0.14 |
| true claims contradicted | 0/23 | 0.0–0.14 |
| benchmark negatives stated as verified / contradicted | 0/10 / 9/10 | |
| false claims verified at creation (gate only, before critique) | 1/22 | 0.01–0.22 |
| true claims verified at creation | 18/23 (was 20/23) | 0.58–0.90 |

Re-run on 2026-09-25 at commit `7da834b` (docs/DESIGN.md D31): the two true
plain-text claims ("compute_total applies the discount", "the discount
threshold is read from ORDERS_DISCOUNT_THRESHOLD") are now `strong_inference`
instead of `statically_verified`, because word overlap no longer verifies;
every other row is unchanged. The false claim verified at creation is still
the "only X" claim with a planted second occurrence, which critique
contradicts.

Critique took 2.4 ms per claim (p50) and 21.8 ms (p95). The set is in-sample.
The trust track added probes (config read, wrong start line, text conflicts)
after seeing misses on it, there is no held-out claim set, and the confidence
caps were not recalibrated.

## Discussion

### What the text format changes, and what "found" means for it

Retrieve text and retrieve JSON rank candidates the same way (one
`retrieve()` call). They differ in how the result is packed:

* The text format spends its budget on three full items: header, signature,
  first doc line, `calls:` / `called by:` outlines and matching passages.
* Items 4–7 get one passage each.
* Every further candidate becomes one skeleton line (`path:a-b name`).
* Call outlines name each caller with its call site (`name (path:line)`).

Headers, skeleton lines and outline entries are all locators. A gold fact
therefore counts as found when the text *points at* its lines, even when the
code itself is not quoted. That is how the text format found g08's six
pipeline steps on `graphify_core` (6/6, against 1/6 for JSON at the same
size).

A model that receives a pointer still has to open the file to read the body.
*Pinpointed* separates the two cases: it requires a locator of at most 30
lines. It is 21 of the 35 facts on `graphify_core` and 16 of the 22 on
`heldout_repoatlas`. Each fact's `score.facts.via` in the result files names
the exact locator or token that matched, so every credit can be checked.

### Verinoda analyze

* **`graphify_core`**: analyze delivered the smallest context among the
  approaches with at least 20 facts (1,021 tokens per question, 26/37). It
  improved from 18/37 in round 2.
* **`orders_app`**: it regressed (31/32, +41% tokens).
* **`heldout_repoatlas`**: it found 11/33. The per-question contexts show why:
  * h05 ("How is a claim marked stale when its cited files change?") was
    read as an *impact* question and answered with a `feedback.py` helper
    (0/4).
  * h08 ("Which tests cover stale claim invalidation?") cited the right
    test file's module docstring but no test names (0/5 in 169 tokens).
  * h04 ("What is affected if evidence.content_hash changes?") listed a test
    of `content_hash` but none of the five production callers (0/5).
* In the same answers analysis also generated two false claims (a test
  "reads" environment variables that only occur in string literals). Critique
  labelled both `contradicted`, which is the evidence discipline working, but
  they still cost tokens.
* On `graphify_core` 12 claims are `observed`: call relations with a
  definitive jedi resolution as evidence.

### In-sample and held-out, honestly

* `orders_app` and `graphify_core` are in-sample for Verinoda. The round-3
  retrieval design was made after seeing Verinoda's misses on
  `graphify_core` (docs/DESIGN.md §3.1), so the 35/37 there is not evidence
  of generalisation.
* `heldout_repoatlas` is the closest thing to a held-out measurement here,
  with three limits:
  1. The search track ran it during development. It reports that it did not
     tune on it, but it did see the scores.
  2. The gold was written against an earlier snapshot of the Verinoda
     source. For this run it was reviewed and re-anchored to commit 7371990
     by the measurement step (this page's author). Three facts were restated
     where the code had been restructured, with extra accepted locations.
     Every change is recorded next to the fact (`review.original`,
     `fact_original`).
  3. It has 8 questions, all Python, all on one code base.
* Scores that the research and track reports give for this set were measured
  on that earlier snapshot. They are not comparable and are not repeated
  here.
* The Turkish sets and the critique-eval set are in-sample by construction
  (written or extended by the author of the rules they test).

### Time and index cost

* **Per-question time.** On `graphify_core`, Verinoda retrieve took about
  0.105 s per question (JSON or text), less than Graphify's in-process
  renderer (0.260 s) and CLI (0.574 s), and about 13x less than in round 2
  (1.373 s cold median).
  * Every benchmark call reloads the graph from disk (`index.load`), and so
    does Graphify's renderer. This is not the "warm query under 50 ms" of
    DESIGN.md D22, which assumes a loaded graph; that setting was not
    measured.
  * Analyze took 1.215 s cold and 0.679 s warm.
  * On the 11-file corpus everything but the Graphify CLI (0.35 s,
    interpreter start-up) and analyze (0.16 s cold) takes under 20 ms.
* **Index cost** went the other way. Verinoda scan now also builds the
  passage index, the lexicon and per-definition symbol facts. On
  `graphify_core` that doubled its cold time (6.72 s → 14.66 s), to about
  twice `graphify update .` (7.53 s). The warm scan is on par (6.01 s vs
  6.11 s). No question count in these sets makes the index pay for itself in
  wall time against raw (0.29 s per question, no index).
  Whether the delivered context pays for itself in model tokens or answer
  quality needs a model in the loop, which was not run.
* **Noise.** The unchanged approaches moved by up to 12% per question between
  rounds. Differences below that are not meaningful.

### Against the design targets (docs/DESIGN.md)

* **§0 goal 3**: more gold facts per token than Graphify, fewer wrong
  statements, latency in the same range.
  * Facts per token: every Verinoda approach had a higher facts-per-1k ratio
    than both Graphify renderers on all five sets.
  * Wrong statements: neither Verinoda nor Graphify presented any negative
    fact as a finding (analyze states one, `orders_app` q04, only in
    `weak_inference` claims).
  * Latency: on `graphify_core`, retrieve (0.105 s) was faster than
    Graphify's renderer (0.260 s) and CLI (0.574 s), while analyze (1.215 s
    cold, 0.679 s warm) was slower than both.
* **D22, facts per 1k tokens at least Graphify's on held-out sets**: met on
  the one held-out set. Retrieve text had 1.97 against 0.61 for either
  renderer, and retrieve JSON 1.47.
* **D22, query latency under 50 ms warm and 1 s cold**: not measured in the
  setting the target names (a loaded graph; a new process per query). The
  benchmark's per-call time includes loading the graph (about 0.1 s).
* **§1.3, Turkish–English fact gap close to 0**: met for retrieve text on
  `orders_app_tr` (in-sample), not met on `graphify_core_tr` (−10).
* **D30, staleness recall 1.0 and no silently wrong verified claim**: met in
  the replay (26,097 claims) and the mutation suite (38 verdicts).

### What is and is not comparable

* *Facts found* is presence in the delivered context: an upper bound on what
  a model could answer, not answer accuracy.
* The approaches deliver different forms:
  * source text (raw, retrieve JSON excerpts);
  * graph structure (Graphify);
  * pointers plus passages (retrieve text);
  * claims with statuses and evidence locators (analyze).

  Which form a model answers best from was not measured.
* Tokens are `ceil(chars / 4)` for every approach (tiktoken is not
  installed). Graphify's own header estimate uses 3 chars per token. Neither
  is a Claude tokenizer.
* Raw is a naive grep-and-read baseline. A smarter raw agent (several
  targeted greps, windowed reads) was not simulated.

## Not measured

* **Model in the loop.** `ANTHROPIC_API_KEY` was not set, so there is no
  answer accuracy, no model tokens and no model cost. The `--llm anthropic`
  path exists and would record usage-based cost. Nothing was estimated in its
  place.
* **Other operating systems and machines.** Every number comes from one
  Windows 11 machine. Linux, macOS, other hardware and variance across
  machines were not measured.
* Cost of any kind other than wall time on this machine; memory use.
* A long-running MCP server that keeps the graph loaded between questions,
  and Verinoda in a new process per question (`verinoda query` cold CLI).
* Where the per-question time goes (no profiling in this round).
* Analyze with runtime tracing (`observe`) or test runs, and with a
  host-written question plan. Every analyze run here used the rule-drafted
  plan.
* The question-understanding metrics of DESIGN.md §1.3: intent macro-F1,
  segmentation, mention linking, clarification precision and recall. The
  36-question gold-plan table lives in `tests/test_question_plan.py` and is
  in-sample. Benchmark schema 2 with gold plans was not built.
* Reference resolution (DESIGN.md §2.3). Its golden corpus is a test fixture
  (`tests/test_references_golden.py`), not a benchmark result.
* Git-history-based answers: every benchmark copy has one commit.
* Graphify's LLM-based extraction (`graphify extract`). Only the AST-only
  `update` was used, as Verinoda uses only AST extraction.
* Variance: one machine, two passes, no confidence intervals for the
  question sets. OS file-cache effects are not separated from process
  warm-up.

## Problems found by this round

For the module owners. The benchmark tuned nothing, and no set was changed
after its results were seen.

1. **Analyze regressed on `orders_app`**: 32/32 → 31/32 (`q10.status` lost)
   and 821 → 1,154 tokens per question; strong_inference claims 14 → 23.
2. **Analyze on the held-out set** (11/33):
   * intent misread (h05 read as impact);
   * a tests answer without test names (h08);
   * impact answers without production callers (h04);
   * environment-read claims generated from string literals, then
     contradicted.
3. **Turkish on the large corpus**: retrieve text −10 and analyze −13 facts
   against English. g01 is 0/4 for every approach; analyze g08 is 0/6 in
   Turkish and 6/6 in English.
4. **JSON trails text** at the same size on every set (for example 27 vs 35
   of 37 on `graphify_core`). Clients that ask for JSON (`--json`, MCP
   `format=json`) get fewer facts.
5. **Scan cost** on the large corpus doubled (6.72 s → 14.66 s cold).
6. **Product CLI wiring** (cli.py, not part of the benchmark):
   * `verinoda benchmark run` does not yet expose `--sweep`, `--sweep-only`
     or `--at`;
   * `verinoda benchmark staleness|critique-eval --out` writes through
     `cli._bench_out`, which does not sanitise paths.

   `python -m verinoda.benchmark …` and the harness modules do both.
7. **Harness limit**: history-based answers cannot be measured on
   single-commit copies.

## Environment and run conditions

From the result files (`environment`):

* Windows-11-10.0.26200-SP0, AMD64 (AMD Family 25 Model 97), 6 logical CPUs.
* CPython 3.12.0, git 2.53.0.windows.1; `tiktoken` not installed (tokens =
  chars/4).
* verinoda 0.1.0.dev0 at source commit `7371990` with uncommitted changes to
  23 package paths: the integration step's analysis, cli, doctor, feedback,
  mcp, paths, workflow and skill templates, plus this measurement's benchmark
  modules and question sets. The paths are listed in
  `environment.verinoda_package_changes_vs_commit`, with diff sha256
  `1a8dd47e680d…`. It is the same in all 13 result files.
* Vendored Graphify `20a20d30`; upstream CLI `graphify 0.9.65` from the same
  commit, in its own venv.
* The `orders_app` corpus is the working tree of `examples/orders_app`,
  which had no changes. `source_dirty: true` in its result refers to the
  Verinoda checkout as a whole. `graphify_core` reads the upstream checkout
  at `20a20d30` (clean). `heldout_repoatlas` is `git archive 7371990` of the pre-rename history, shipped as `benchmarks/corpora/heldout_repoatlas_7371990/`.
* Timeline (UTC, 2026-09-23):
  * staleness replay about 01:06–01:22;
  * mutation suite and critique evaluation 01:22;
  * main and sweep runs 01:42–01:58, strictly one after another.

Run conditions:

* A process logger polled the process table about every 3 s from 01:22 UTC.
* Another agent's full test suite was running during at least part of the
  staleness replay (seen at about 01:17 UTC) and during the mutation suite
  and the critique evaluation (01:22 UTC); it ended at 01:30 UTC. The
  harnesses' timing numbers (invalidation ms, update ms, ms per claim) were
  therefore measured under load. Their counts do not depend on timing.
* Before each benchmark run the script waited for 30 s without any other
  Python or Graphify process. For the first run the check timed out (it
  counted its own logger, a bug fixed for the later checks). The log shows
  no foreign process after 01:30:08 UTC.
* From 01:42 to 01:58 UTC the logger saw no process other than the runs
  themselves and their children (indexer workers, the jedi subprocess of
  analyze, the Graphify CLI).

## Reproduce

From the `verinoda` repository root on Windows, with the project venv. Use
`python` instead of `.venv/Scripts/python` elsewhere. Placeholders:

* `<UPSTREAM>`: a checkout of <https://github.com/Graphify-Labs/graphify> at
  `20a20d30d8e7eef77675651f0199d87f913bd3e7`.
* `<UPSTREAM_VENV>`: a separate venv with that checkout installed
  (`pip install -e <UPSTREAM>`). It provides the CLI `graphify 0.9.65`, at
  `<UPSTREAM_VENV>/Scripts/graphify.exe` on Windows and
  `<UPSTREAM_VENV>/bin/graphify` elsewhere.

In the measured runs both were directories next to the Verinoda checkout
(`../upstream-graphify`, `../.venv-upstream`).

```
G=<UPSTREAM_VENV>/Scripts/graphify.exe

# main runs: default configurations, repeat 2, real upstream CLI
.venv/Scripts/python -m verinoda.benchmark run --repo examples/orders_app --questions orders_app \
    --repeat 2 --graphify-cmd $G --out benchmarks/results/orders_app.json
.venv/Scripts/python -m verinoda.benchmark run --repo <UPSTREAM> --questions graphify_core \
    --repeat 2 --graphify-cmd $G --out benchmarks/results/graphify_core.json
.venv/Scripts/python -m verinoda.benchmark run --repo benchmarks/corpora/heldout_repoatlas_7371990 --questions heldout_repoatlas \
    --repeat 2 --graphify-cmd $G --out benchmarks/results/heldout_repoatlas.json
.venv/Scripts/python -m verinoda.benchmark run --repo examples/orders_app --questions orders_app_tr \
    --repeat 2 --graphify-cmd $G --out benchmarks/results/orders_app_tr.json
.venv/Scripts/python -m verinoda.benchmark run --repo <UPSTREAM> --questions graphify_core_tr \
    --repeat 2 --graphify-cmd $G --out benchmarks/results/graphify_core_tr.json

# budget sweep: the same five commands with these options and another output file
    --sweep 750,1500,3000 --sweep-only --out benchmarks/results/sweep/<set>.json

# trust harnesses
.venv/Scripts/python -m verinoda.benchmark.staleness replay --repo <UPSTREAM> --commits 300 \
    --pathspec "graphify/*.py" --cap 40 --out benchmarks/results/trust/staleness_graphify_300.json
.venv/Scripts/python -m verinoda.benchmark.staleness mutations --out benchmarks/results/trust/mutations.json
.venv/Scripts/python -m verinoda.benchmark.critique_eval --out benchmarks/results/trust/critique_eval.json

# tables for this page
.venv/Scripts/python -m verinoda.benchmark markdown benchmarks/results/<set>.json
.venv/Scripts/python -m verinoda.benchmark markdown benchmarks/results/sweep/<set>.json
.venv/Scripts/python -m verinoda.benchmark compare benchmarks/results/before-round3/<set>.json \
    benchmarks/results/<set>.json

# replace machine paths in result files written by an older harness (run on the machine that wrote them)
.venv/Scripts/python -m verinoda.benchmark sanitize <file>.json

# optional, measures model tokens/cost (needs the key and `pip install anthropic`)
#   add: --llm anthropic        (model: claude-sonnet-5, or set VERINODA_BENCH_MODEL)

# unit + end-to-end tests of the benchmark itself (no network, no LLM)
.venv/Scripts/python -m pytest tests/test_benchmark.py -q -p no:cacheprovider
```

`heldout_repoatlas` was measured on commit `7371990` of the pre-rename history
(the product was then called RepoAtlas). That history is not part of this
repository, so the corpus ships as a snapshot in
`benchmarks/corpora/heldout_repoatlas_7371990/` (the set records
`corpus.snapshot_dir` and `corpus.source_commit`); pass it as `--repo`. The
snapshot does not move when the code does. `--at <commit>` does the same for any
set.

`verinoda benchmark run` (the product CLI) uses the same engine but does not
yet expose `--sweep`, `--sweep-only` or `--at`; `python -m verinoda.benchmark`
does, prints progress on stderr and accepts `--keep-workdir`. Both only *read*
the given repository:

* They copy the files (tracked, plus untracked files that are not ignored,
  filtered by the question set's `corpus.include` / `corpus.exclude`; or the
  pinned commit's files) into a temporary directory, and commit them there.
* They build every index and database in that copy. A second copy is made
  for the upstream CLI.
* They delete the temporary directory afterwards.

Run from a normal shell, or anything with a `__main__` guard, because the
indexer starts worker processes on large corpora.

**Outputs.**

* `benchmarks/results/<set>.json`: all measurements, per question, approach
  and run.
* `benchmarks/results/raw/<set>/<question>/<approach>.txt`: the exact
  delivered contexts that were scored (sweep runs: under
  `benchmarks/results/sweep/raw/`).
* `benchmarks/results/trust/*.json`: the three harness results.
* `benchmarks/results/before-round3/`: the round-2 run, moved here unchanged
  (files and `raw/` tree).
* `benchmarks/results/before-fixes/`: the round-1 run (05890a1).

**Paths in result files.** When it writes results, the harness replaces
absolute paths with placeholders: `<TMP>` (the system temp dir), `<CORPUS>`
(the analysed repository, when it is outside the Verinoda checkout; for the
Graphify sets that is `<UPSTREAM>`), `<REPO>` (the Verinoda checkout) and
`<HOME>` (the user's home directory). Each file lists the placeholders it uses
under `paths_sanitized`. A delivered context that needed rewriting is flagged
`score.context_sanitized`, because its `sha256` is of the text as scored. The
staleness and critique harnesses write through the same sanitiser.

## What is compared

All approaches answer the **same questions** on the **same copy** of the
corpus. Each one produces the text that would be handed to a model (the
"delivered context"). That text is saved verbatim under
`benchmarks/results/raw/<set>/<question>/<approach>.txt` and is what gets
scored.

| Approach | What it is | Index |
|---|---|---|
| `raw` | A deterministic simulation of an agent with no tools beyond grep and read. It extracts the question terms with the same function Verinoda uses (`verinoda.retrieval.terms_for`, built on Graphify's `_query_terms`; duplicates removed), drops terms shorter than 3 chars and greps the rest case-insensitively as substrings. It ranks files by (distinct terms matched, matching lines, path), shows the first 40 grep hits (`path:line: text`), then reads the top 5 files in full with line numbers (`cat -n` style) until 24,000 characters. The grep listing counts toward the cap. | none |
| `graphify_vendored` | Graphify's own query renderer (`_query_graph_text` at the pinned commit 20a20d30, called through `verinoda.index.graphify_query_text`) with the defaults of Graphify's MCP `query_graph` tool: BFS, depth 3, 2,000-token budget. It reads the graph built by Verinoda's scan (the vendored Graphify pipeline, same commit). | Verinoda scan |
| `graphify_cli` | The real upstream CLI, `graphify query "<question>"`, with the CLI's defaults (BFS, depth 2, 2,000-token budget). It runs as a subprocess in a *separate* copy of the corpus indexed with `graphify update .`, installed from the upstream checkout at the same commit (`graphify 0.9.65`). Its environment has `GRAPHIFY_OUT` removed (so it uses its own `graphify-out/`), every `*_API_KEY` / `*_AUTH_TOKEN` removed, and `GRAPHIFY_QUERY_LOG_DISABLE=1`. | `graphify update .` |
| `verinoda_analyze` | `verinoda.analysis.analyze` with the default budget (60 s, 40 internal tool calls, ~6,000 tokens), critique on, no test runs, no host plan (the rule-drafted question plan is used). The delivered context is compact JSON of `question`, `intents`, `claims` (id, text, status, confidence, evidence locators, uncertainties) and `unknowns`, the same fields as in the earlier runs. The round-3 plan fields of `verinoda analyze --json` (`understood_as`, `subquestions`, `plan_check`) are **not** included, so they cost no tokens here. | Verinoda scan |
| `verinoda_retrieve` | `verinoda.retrieval.retrieve` with the CLI defaults of `verinoda query` (10 items, 6,000 chars). The delivered context is its JSON: items with reasons, an excerpt window, the full definition `span`, and the relations among the items. | Verinoda scan |
| `verinoda_retrieve_text` | **New in round 3.** The model-facing text of the same retrieval: `retrieval.render_text(retrieve(g, q, Budget(10, 6000)), 6000)`, the default output of `verinoda query`. Skeleton first: the top 3 items with `path:a-b` header, signature, first doc line, `calls:` / `called by:` outlines and matching passages; items 4-7 with one passage; the rest as one `path:a-b name` line each; truncation stated with the follow-up command. | Verinoda scan |

**Budget sweep** (`--sweep 750,1500,3000`). `<approach>@<tokens>` runs a
budgeted approach with a context cap of `4 x tokens` **characters**, i.e.
`tokens` in this benchmark's chars/4 estimate, so every approach at one sweep
point gets the same number of characters:

* `verinoda_retrieve_text@N`: `render_text(retrieve(g, q, Budget(10, 4N)), 4N)`;
* `graphify_vendored@N` and `graphify_cli@N`: Graphify's budget counts 3
  chars per token, so it is given `ceil(4N / 3)` Graphify tokens (1000 / 2000 /
  4000; the CLI gets `--budget`);
* `raw@N`: `char_cap = 4N`.

The 1,500-token point is the default configuration of retrieve text (6,000
chars) and of both Graphify renderers (budget 2,000); raw's default is 24,000
chars. The sweep runs are separate runs (`--sweep-only`), so the timings of the
default configurations in the main runs are not disturbed by them.

These differences are part of what is being compared, not accidents:

* The vendored renderer and the upstream CLI differ in traversal depth (3 vs
  2), because those are the defaults of Graphify's MCP tool and CLI
  respectively. Both are reported.
* The Verinoda approaches load the graph through `verinoda.index.load`,
  which adds `INFERRED` receiver-call edges (`param.method()` on annotated
  parameters). The Graphify renderers read `graph.json` as Graphify wrote it.
* Graphify never cuts a context in which every node it found fits; it then
  returns all edges too, with the notice "Complete answer over budget". Its
  delivered size can therefore exceed the budget it was given (see the sweep).
* Graphify and raw produce *context*, not claims. Verinoda analyze produces
  *claims with statuses*. Where a metric only makes sense for one kind, the
  table says `n/a` rather than inventing a comparable number.

## Metrics (exact definitions)

Implemented in `verinoda/benchmark/metrics.py` and `runner.py`. The rules
are mechanical, so every score can be re-checked by hand from the saved
contexts.

**Locator.** A repo-relative `path` plus a line range, recognised in any of
these spellings: `path:N`, `path:N-M`, `path:LN` (Graphify `at=`), Graphify
node attributes `src=path loc=LN`, retrieval JSON `"file": "path", "lines": [a, b]`,
and the raw reader's file headers `==> path:1-K <==`. Retrieve-text headers
(`## path:a-b ...`) and skeleton lines (`path:a-b name`) are `path:N-M`
locators. Absolute paths are not locators.

**Key fact found.** Each question has hand-verified gold facts. A fact has a
`source` (`path:line` plus a substring that must be on that line) and a list
of `match_any` alternatives. The fact counts as *found* in a delivered context
if at least one alternative matches:

* `{"loc": "path:a-b"}`: some locator in the context is on the same path and
  its line range overlaps `a-b`.
* `{"text": "tok"}`: the literal token occurs, case-sensitive, with word
  boundaries where the token starts or ends with a letter, digit or `_`.
* `{"all": [...]}`: every listed token occurs.

"Found" means *present in what the model would read*. It is an upper bound on
what a model could answer from that context, not a measurement of answer
accuracy (see [Not measured](#not-measured)).

**Pinpointed.** Found through a `loc` alternative whose matching locator
spans at most 30 lines: a pointer to the spot rather than a whole file.

**Facts per 1k tokens.** Gold facts found in a set divided by the tokens
delivered for that set, times 1,000 (`summary.<approach>.facts_per_1k_tokens`).
It is a ratio of what was delivered, not a cost saving: it says nothing about
whether a model answers correctly from it.

**Gold verification.** Before anything is scored, every fact is re-checked
against the benchmark copy (`validate_gold`): `source.at` must exist and
contain `source.contains`; every `loc` alternative must point at existing
lines; every `text`/`all` token must occur somewhere in the corpus. A set that
fails is not run (`status: gold_invalid`).

**Negative facts (known-wrong statements).** Each question may list
statements that are false in the source, either as a regex or as a relation
triple `[source, relation, target]` (labels normalised: backticks, `()` and a
leading `.` removed, qualified names reduced to their last component, `|`
separates alternatives). Negatives are matched only against **assertions**:

* Graphify: `EDGE a --rel [...]--> b` lines.
* Verinoda analyze: claim texts (relation claims and data-path chains).
* Verinoda retrieve (JSON): its `edges` list.
* Verinoda retrieve (text): its `calls:` / `called by:` outline entries,
  one `calls` assertion each (`metrics.text_outline_assertions`); quoted source
  lines assert nothing.
* Raw: nothing (`n/a`).

For analyze, a matched claim counts as *presented as a finding* when its status
is `observed`, `*_verified` or `strong_inference`; matches in claims already
labelled `weak_inference`, `unknown`, `contradicted` or `stale` are reported
separately.

**Citation check** (heuristic, every approach that states relations with a
line): for every asserted `calls`/`uses` relation that cites `path:line`, does
that line contain the target's name, or an import alias of it bound in the
same file, as a word? A failure means the cited line does not show the
relation; it does not prove the relation false. For analyze only claims
presented as findings are checked.

**Context size.** Characters and tokens of the delivered context. Tokens are
counted with `tiktoken` `cl100k_base` when it can be imported, otherwise
estimated as `ceil(chars / 4)`. Every result file states which
(`token_count_method`); every run on this page used the chars/4 estimate.
Neither is the tokenizer of any Claude model.

**Tool calls.** raw: 1 grep plus one read per file read; Graphify: 1 query;
Verinoda: 1 call (analyze's internal steps are recorded separately).

**Model cost** is measured **only** with `--llm anthropic`, `ANTHROPIC_API_KEY`
set and the `anthropic` package installed: each context plus the question is
sent to the Messages API (`claude-sonnet-5` unless `VERINODA_BENCH_MODEL` is
set), tokens come from the response `usage`, cost from this table, and the
same gold-fact rule is applied to the answer. Without the key every model
field says `not measured`; nothing is estimated.

| model | input USD / 1M tokens | output USD / 1M tokens |
|---|---|---|
| claude-sonnet-5 | 2.00 | 10.00 |
| claude-opus-5 | 5.00 | 25.00 |
| claude-haiku-4-5 | 1.00 | 5.00 |

Source: Anthropic pricing page
(<https://platform.claude.com/docs/en/about-claude/pricing>) as cached in the
Claude API reference on 2026-06-24, recorded on 2026-09-22; standard
first-party rates.

**Time.** All with `time.perf_counter`.

* *Index cost (one-off)*, reported separately from query cost: Verinoda
  `workflow.scan` in process, run twice (cold, then warm with the caches
  present); round 3's scan also builds the search index, lexicon and symbol
  facts. Upstream `graphify update .` run twice as a subprocess in its own
  copy (includes interpreter start-up).
* *Per-query time*: `repeat` full passes over all questions and approaches.
  Pass 1 is **cold**, later passes are **warm**. Cold includes first-use work
  in the benchmark process (imports, per-process caches filled by earlier
  questions). The CLI pays interpreter start-up and graph load on every query.
  OS file-cache effects are not separated.

**Cache hits.** Verinoda: a claim is *reused* if a claim with the same id
already existed in `atlas.db` before the run. Graphify CLI: files in
`graphify-out/cache` after the cold and warm `graphify update .`. Raw: none.

## Question sets

Stored in `verinoda/benchmark/questions/`. Every set has a `provenance`
block (who wrote the questions and the gold, whether it is in-sample), which
the runner copies into `set.provenance` of each result. The question wording,
gold facts and negatives of `orders_app` and `graphify_core` are unchanged
since `05890a1`; `tests/test_benchmark.py` pins the sha256 of both arrays.

| set | corpus | questions | gold facts | negatives | written by | in-sample |
|---|---|---|---|---|---|---|
| `orders_app` | `examples/orders_app` (11 files) | 10: where ×3, flow ×2, config, tests, why, impact, behaviour | 32 | 15 | benchmark harness author (round 1), before any approach ran, by reading every file | yes: Verinoda has been developed against this example since |
| `graphify_core` | upstream Graphify at `20a20d30`: `graphify/`, `tests/test_security.py`, `tests/test_querylog.py`, `pyproject.toml` (226 files) | 9: where ×2, flow ×2, config ×2, why, impact, tests | 37 | 7 | benchmark harness author (round 1), before any approach ran | yes: the round-3 retrieval design followed its misses |
| `heldout_repoatlas` | the product (then named RepoAtlas) at commit `7371990` of the pre-rename history, shipped as a snapshot: `repoatlas/` without `project_index/` and `benchmark/questions/`, plus `tests/` without `fixtures/` (106 files) | 8: where ×2, config, flow, impact, behaviour, why, tests | 33 | 0 | the retrieval research agent, after its prototype was built and before it ran on this corpus; gold reviewed and re-anchored to `7371990` by the measurement step | no (held out from the design; seen by the search track, which reports no tuning on it) |
| `orders_app_tr` | as `orders_app` | the 10 `orders_app` questions in Turkish | 32 (same) | 15 (same) | the question-understanding rule author, while tuning the Turkish rules | yes |
| `graphify_core_tr` | as `graphify_core` | the 9 `graphify_core` questions in Turkish | 37 (same) | 7 (same) | the question-understanding rule author, while tuning the Turkish rules | yes |
| `forge_mod` | `examples/forge_mod` (55 files: a fictional NeoForge mod in Java and Kotlin with a data pack, worldgen, tags, lang files in two languages and JUnit tests) | 14, 7 of them Turkish: callers, cross-layer, config, resources, flow, behaviour, where | 68 | 19 | an agent that never ran Verinoda on it, after all the 2026-09-24 changes | held out for its first measurement; in-sample since: the Kotlin call pass, q05, q09, q11 for the second review round, q03 for the spelled resource id |
| `glow_mod` | `examples/glow_mod` (37 files: a fictional Fabric mod - Java, a data pack, assets, a yml config, a reference tree `reference/` configured through `corpus.verinoda_config`, and a copied data pack) | 14, 7 of them Turkish: callers, cross-layer (code <-> data pack), config, resources, flow, behaviour, where | 50 | 12 (in 10 questions) | an agent that never ran Verinoda on it, after the Minecraft support was written | held out for its first measurement only: the Java call pass, the translation pairs, the game words of the seed dictionary and the link chain were added after looking at the misses of q01, q03, q04 and q13 |

The held-out review, fact by fact, is stored in the set:

* 24 facts are `reanchored`: the same statement, with the locators
  re-pointed to the same code at `7371990`.
* 6 facts are `unchanged`: the cited lines did not move.
* 3 facts are `restated`, because the code had been restructured since the
  gold was written:
  * `h04.check`: the moved-block search now lives in `_legacy_search`;
  * `h05.changed`: `invalidate_stale` now goes through `assess_change`;
  * `h07.classify`: the allowlist decision now lives in `policy()`, which
    `classify()` wraps.

  Each restated fact keeps its original wording (`fact_original`) and
  accepts both the old and the new location.

No fact was added or removed, and the question wording is the research
agent's. Every fact passes `validate_gold` on the pinned snapshot; a test
re-checks this whenever the commit is present.

Question wording was fixed before any approach ran on a set. Gold facts are
changed only when the gold check finds them wrong, or, for the held-out set,
by the recorded re-anchoring.

## Per-question results

Facts found / gold facts per question, with the tokens delivered, for the default configurations.
`python -m verinoda.benchmark markdown benchmarks/results/<set>.json` prints these and the other
tables of a result file.

### `orders_app`

| question | category | raw grep+read | Graphify query (vendored, depth 3) | Graphify CLI (upstream, depth 2) | Verinoda analyze | Verinoda retrieve (JSON) | Verinoda retrieve (text) |
|---|---|---|---|---|---|---|---|
| q01 Where is an order written to the database? | where | 3/3 (1759 tok) | 1/3 (2517 tok) | 1/3 (2120 tok) | 3/3 (1256 tok) | 3/3 (1413 tok) | 3/3 (941 tok) |
| q02 What is the call path from the create-order HTTP handler to the database write? | flow | 3/3 (1736 tok) | 1/3 (717 tok) | 1/3 (581 tok) | 3/3 (833 tok) | 2/3 (1379 tok) | 3/3 (879 tok) |
| q03 Which environment variables configure the orders service and where are they read? | config | 3/3 (1275 tok) | 0/3 (2681 tok) | 0/3 (2450 tok) | 3/3 (1132 tok) | 3/3 (1384 tok) | 3/3 (788 tok) |
| q04 Which tests exercise the discount calculation? | tests | 3/4 (671 tok) | 4/4 (2641 tok) | 4/4 (2399 tok) | 4/4 (1144 tok) | 4/4 (1322 tok) | 4/4 (722 tok) |
| q05 Why does the project use SQLite for persistence? | why | 3/3 (690 tok) | 1/3 (2149 tok) | 0/3 (534 tok) | 3/3 (1414 tok) | 3/3 (1414 tok) | 3/3 (822 tok) |
| q06 What needs retesting if OrderRepository.save changes? | impact | 3/3 (1249 tok) | 2/3 (2450 tok) | 2/3 (2053 tok) | 3/3 (1377 tok) | 3/3 (1427 tok) | 3/3 (1067 tok) |
| q07 Where is the 10% discount applied and what threshold controls it? | where | 3/3 (620 tok) | 2/3 (2006 tok) | 2/3 (704 tok) | 3/3 (962 tok) | 3/3 (1410 tok) | 3/3 (725 tok) |
| q08 How does get_order_handler load an order from storage? | flow | 4/4 (1782 tok) | 1/4 (2467 tok) | 1/4 (2230 tok) | 4/4 (1021 tok) | 4/4 (1409 tok) | 4/4 (1085 tok) |
| q09 Where is the SQLite connection opened and what decides the database file? | where | 3/3 (853 tok) | 1/3 (2194 tok) | 1/3 (1284 tok) | 3/3 (1492 tok) | 3/3 (1404 tok) | 3/3 (840 tok) |
| q10 What happens when an order is submitted with no items? | behaviour | 3/3 (1607 tok) | 3/3 (2522 tok) | 3/3 (2158 tok) | 2/3 (914 tok) | 3/3 (1403 tok) | 3/3 (998 tok) |

### `graphify_core`

| question | category | raw grep+read | Graphify query (vendored, depth 3) | Graphify CLI (upstream, depth 2) | Verinoda analyze | Verinoda retrieve (JSON) | Verinoda retrieve (text) |
|---|---|---|---|---|---|---|---|
| g01 Where is graphify query output cut to fit the token budget? | where | 0/4 (5990 tok) | 0/4 (1657 tok) | 0/4 (1656 tok) | 3/4 (643 tok) | 3/4 (1425 tok) | 4/4 (1477 tok) |
| g02 Which functions does the graphify query command call to turn a question into graph context? | flow | 0/6 (5982 tok) | 0/6 (1671 tok) | 0/6 (4130 tok) | 1/6 (872 tok) | 6/6 (1425 tok) | 6/6 (1460 tok) |
| g03 Which environment variables control graphify's query log? | config | 0/4 (5996 tok) | 0/4 (1674 tok) | 0/4 (1673 tok) | 4/4 (1140 tok) | 4/4 (1430 tok) | 4/4 (1486 tok) |
| g04 Where does graphify store its AST extraction cache and how is a cache entry keyed? | where | 0/4 (5992 tok) | 4/4 (1641 tok) | 4/4 (1640 tok) | 3/4 (1254 tok) | 3/4 (1431 tok) | 3/4 (1457 tok) |
| g05 Which environment variable raises the maximum graph.json size, and what is the default limit? | config | 0/3 (5988 tok) | 1/3 (1692 tok) | 1/3 (1691 tok) | 2/3 (1521 tok) | 2/3 (1448 tok) | 3/3 (1276 tok) |
| g06 Why does graphify drop stopwords from query terms? | why | 3/3 (5997 tok) | 1/3 (1666 tok) | 1/3 (1664 tok) | 3/3 (626 tok) | 2/3 (1439 tok) | 3/3 (1326 tok) |
| g07 What is affected if _query_terms changes? | impact | 1/4 (5990 tok) | 1/4 (1646 tok) | 1/4 (1646 tok) | 3/4 (923 tok) | 4/4 (1429 tok) | 4/4 (1349 tok) |
| g08 How does graphify update rebuild the code graph without an LLM? | flow | 0/6 (5981 tok) | 0/6 (1660 tok) | 0/6 (1658 tok) | 6/6 (818 tok) | 1/6 (1427 tok) | 6/6 (1486 tok) |
| g09 Which tests cover the graph file size cap and its environment override? | tests | 0/3 (5989 tok) | 0/3 (1700 tok) | 0/3 (1699 tok) | 1/3 (1391 tok) | 2/3 (1441 tok) | 2/3 (1484 tok) |

### `heldout_repoatlas`

| question | category | raw grep+read | Graphify query (vendored, depth 3) | Graphify CLI (upstream, depth 2) | Verinoda analyze | Verinoda retrieve (JSON) | Verinoda retrieve (text) |
|---|---|---|---|---|---|---|---|
| h01 Where is an experiment's process tree killed when it times out? | where | 0/4 (5983 tok) | 0/4 (1654 tok) | 0/4 (1644 tok) | 4/4 (765 tok) | 4/4 (1435 tok) | 4/4 (1385 tok) |
| h02 Which environment variables are passed to experiment commands, and where is that decided? | config | 4/4 (5992 tok) | 0/4 (1679 tok) | 0/4 (1669 tok) | 1/4 (849 tok) | 1/4 (1439 tok) | 2/4 (1497 tok) |
| h03 What does verinoda scan call to build the index and invalidate stale claims? | flow | 1/5 (5998 tok) | 1/5 (1621 tok) | 1/5 (1620 tok) | 3/5 (940 tok) | 3/5 (1438 tok) | 4/5 (1370 tok) |
| h04 What is affected if evidence.content_hash changes? | impact | 0/5 (5998 tok) | 4/5 (1620 tok) | 4/5 (1618 tok) | 0/5 (720 tok) | 5/5 (1455 tok) | 5/5 (1419 tok) |
| h05 How is a claim marked stale when its cited files change? | behaviour | 0/4 (5991 tok) | 2/4 (1648 tok) | 2/4 (1646 tok) | 0/4 (310 tok) | 1/4 (1448 tok) | 1/4 (1426 tok) |
| h06 Why can a claim not be deleted from the store? | why | 0/2 (5994 tok) | 0/2 (1649 tok) | 0/2 (1647 tok) | 1/2 (227 tok) | 1/2 (1437 tok) | 1/2 (1375 tok) |
| h07 Where does Verinoda decide whether an experiment command needs a container? | where | 0/4 (5986 tok) | 1/4 (1667 tok) | 1/4 (1666 tok) | 2/4 (713 tok) | 2/4 (1442 tok) | 3/4 (1229 tok) |
| h08 Which tests cover stale claim invalidation? | tests | 4/5 (5997 tok) | 0/5 (1641 tok) | 0/5 (1642 tok) | 0/5 (169 tok) | 0/5 (1435 tok) | 2/5 (1481 tok) |

### `orders_app_tr`

| question | category | raw grep+read | Graphify query (vendored, depth 3) | Graphify CLI (upstream, depth 2) | Verinoda analyze | Verinoda retrieve (JSON) | Verinoda retrieve (text) |
|---|---|---|---|---|---|---|---|
| q01 Sipariş veritabanına nerede yazılıyor? | where | 0/3 (22 tok) | 0/3 (6 tok) | 0/3 (7 tok) | 3/3 (1319 tok) | 3/3 (1422 tok) | 3/3 (898 tok) |
| q02 Sipariş oluşturma API'sinden veritabanı yazımına kadar çağrı yolu nedir? | flow | 0/3 (129 tok) | 1/3 (2454 tok) | 1/3 (1993 tok) | 3/3 (1008 tok) | 3/3 (1425 tok) | 3/3 (942 tok) |
| q03 Sipariş servisini hangi ortam değişkenleri yapılandırıyor ve bunlar nerede okunuyor? | config | 0/3 (33 tok) | 0/3 (6 tok) | 0/3 (7 tok) | 3/3 (1030 tok) | 3/3 (1415 tok) | 3/3 (857 tok) |
| q04 İndirim hesaplamasını hangi testler çalıştırıyor? | tests | 0/4 (24 tok) | 0/4 (6 tok) | 0/4 (7 tok) | 4/4 (1247 tok) | 4/4 (1408 tok) | 4/4 (737 tok) |
| q05 Proje kalıcılık için neden SQLite kullanıyor? | why | 3/3 (644 tok) | 1/3 (2581 tok) | 0/3 (1757 tok) | 3/3 (1519 tok) | 3/3 (1410 tok) | 3/3 (786 tok) |
| q06 OrderRepository.save değişirse neleri yeniden test etmek gerekir? | impact | 3/3 (1395 tok) | 2/3 (2455 tok) | 2/3 (2091 tok) | 3/3 (1467 tok) | 2/3 (1415 tok) | 3/3 (906 tok) |
| q07 %10 indirim nerede uygulanıyor ve bunu hangi eşik kontrol ediyor? | where | 0/3 (27 tok) | 0/3 (6 tok) | 0/3 (7 tok) | 3/3 (1267 tok) | 3/3 (1406 tok) | 3/3 (763 tok) |
| q08 get_order_handler bir siparişi depodan nasıl yüklüyor? | flow | 2/4 (293 tok) | 1/4 (2462 tok) | 1/4 (2098 tok) | 4/4 (954 tok) | 4/4 (1413 tok) | 4/4 (1010 tok) |
| q09 SQLite bağlantısı nerede açılıyor ve veritabanı dosyasını ne belirliyor? | where | 2/3 (586 tok) | 1/3 (2194 tok) | 1/3 (1284 tok) | 3/3 (1517 tok) | 2/3 (1420 tok) | 3/3 (907 tok) |
| q10 Kalemsiz bir sipariş gönderilince ne oluyor? | behaviour | 0/3 (23 tok) | 0/3 (6 tok) | 0/3 (7 tok) | 1/3 (957 tok) | 1/3 (1412 tok) | 3/3 (924 tok) |

### `graphify_core_tr`

| question | category | raw grep+read | Graphify query (vendored, depth 3) | Graphify CLI (upstream, depth 2) | Verinoda analyze | Verinoda retrieve (JSON) | Verinoda retrieve (text) |
|---|---|---|---|---|---|---|---|
| g01 Graphify sorgu çıktısı token bütçesine sığmak için nerede kesiliyor? | where | 0/4 (5982 tok) | 0/4 (1653 tok) | 0/4 (1940 tok) | 0/4 (734 tok) | 0/4 (1437 tok) | 0/4 (1430 tok) |
| g02 graphify query komutu bir soruyu graf bağlamına çevirmek için hangi fonksiyonları çağırıyor? | flow | 0/6 (5992 tok) | 0/6 (1649 tok) | 0/6 (1648 tok) | 0/6 (594 tok) | 0/6 (1435 tok) | 1/6 (1456 tok) |
| g03 Graphify'ın query log'unu hangi ortam değişkenleri kontrol ediyor? | config | 0/4 (5992 tok) | 0/4 (1635 tok) | 0/4 (1634 tok) | 4/4 (1068 tok) | 3/4 (1438 tok) | 4/4 (1489 tok) |
| g04 Graphify AST çıkarım önbelleğini nerede saklıyor ve bir önbellek kaydının anahtarı nasıl oluşturuluyor? | where | 0/4 (6000 tok) | 0/4 (1635 tok) | 0/4 (3511 tok) | 1/4 (723 tok) | 2/4 (1441 tok) | 2/4 (1350 tok) |
| g05 Hangi ortam değişkeni en büyük graph.json boyutunu artırıyor ve varsayılan sınır nedir? | config | 0/3 (5996 tok) | 1/3 (1635 tok) | 1/3 (1625 tok) | 2/3 (1341 tok) | 2/3 (1438 tok) | 3/3 (1452 tok) |
| g06 Graphify sorgu terimlerinden neden stopword'leri atıyor? | why | 3/3 (5988 tok) | 0/3 (93 tok) | 0/3 (92 tok) | 3/3 (681 tok) | 2/3 (1429 tok) | 3/3 (1443 tok) |
| g07 _query_terms değişirse ne etkilenir? | impact | 2/4 (5992 tok) | 1/4 (1634 tok) | 1/4 (1633 tok) | 2/4 (658 tok) | 4/4 (1433 tok) | 4/4 (1490 tok) |
| g08 graphify update kod grafiğini LLM olmadan nasıl yeniden oluşturuyor? | flow | 0/6 (5982 tok) | 0/6 (1649 tok) | 0/6 (1648 tok) | 0/6 (700 tok) | 1/6 (1438 tok) | 6/6 (1473 tok) |
| g09 Grafik dosyası boyut sınırını ve ortam değişkeniyle aşılmasını hangi testler kapsıyor? | tests | 0/3 (5998 tok) | 0/3 (6 tok) | 0/3 (7 tok) | 1/3 (1271 tok) | 2/3 (1439 tok) | 2/3 (1496 tok) |
