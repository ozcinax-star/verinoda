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

Sections: [Summary](#summary) · [Results per set](#results-per-set) ·
[Before round 3 vs now](#before-round-3-vs-now) · [Budget sweep](#budget-sweep) ·
[Turkish vs English](#turkish-vs-english) · [Trust harnesses](#trust-harnesses) ·
[Discussion](#discussion) · [Not measured](#not-measured) ·
[Problems found](#problems-found-by-this-round) · [Environment](#environment-and-run-conditions) ·
[Reproduce](#reproduce) · [What is compared](#what-is-compared) ·
[Metrics](#metrics-exact-definitions) · [Question sets](#question-sets) ·
[Per-question results](#per-question-results)

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
| presented as verified that are true | 20/20 | 0.84–1.0 |
| false claims flagged (lowered, contradicted or never verified) | 22/22 | 0.85–1.0 |
| false claims contradicted | 17/22 | 0.57–0.90 |
| true claims lowered (false alarm) | 0/23 | 0.0–0.14 |
| true claims contradicted | 0/23 | 0.0–0.14 |
| benchmark negatives stated as verified / contradicted | 0/10 / 9/10 | |
| false claims verified at creation (gate only, before critique) | 1/22 | 0.01–0.22 |

Critique took 2.4 ms per claim (p50) and 19.7 ms (p95). The set is in-sample.
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
