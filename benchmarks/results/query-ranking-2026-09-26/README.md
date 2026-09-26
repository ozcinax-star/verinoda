# Query ranking (docs/DESIGN.md D-query-ranking), 2026-09-26

Measurements of the ranking change on branch night/query-ranking against its base, commit 343a00d
(integrate/0925). See docs/BENCHMARKS.md (Update 2026-09-26: query ranking) and docs/DESIGN.md section 10.

Files:

- `dev.json`: the dev set, 37 questions with a gold file (and symbol) each and `asks_tests`. Written for this
  change by its author, with the rules in view: in-sample. Corpora: `big` = a copy of CPython's `Lib` folder
  (the one the senior review used), `gfy` = upstream Graphify, `vn` = Verinoda's repository at 343a00d,
  `orders` / `glow` / `forge` = the examples. Each corpus was copied and indexed with the base code; ranking
  changes need no re-index.
- `score.py`: the scorer. `python score.py dev.json --code DIR --corpus big=PATH ...` runs the query of the
  code in DIR (in-process `retrieval.retrieve` with the budget of `verinoda query`, or the real command with
  `--cli`) and reports, over the ranked list (the JSON items, then `budget.more`): top 1 / 3 / 10 hit rate of the
  gold file, MRR, gold symbol in the top 3, and, for questions not about tests, the test files among the first 5
  and how often the first result is a test. `--compare A.json B.json` diffs two result files.
- `dev-base.json`, `dev-branch.json`: the scorer's results for the base and the branch (per question: ranks and
  the first five results).
- `fast-base.json`, `fast-branch.json`: the fast harness (as in `../fast-2026-09-25/`) on the eight public
  retrieval sets with prepared indexes, base and branch. One private set was measured too and is left out of
  these files (same facts; a test ranked first for 1 of its 16 questions not about tests, 4 before).
- `x1-rejected-flat-test-factor.json`: REJECTED: tests covered by code multiplied by 0.7 instead of moving just
  below the covering code, measured before module-level blocks of test files were left out of the rule
  (orders_app q01 and heldout_repoatlas h02 lose facts). The other rejected variants in docs/DESIGN.md 10.3 were
  measured on intermediate code; their files are not kept.
