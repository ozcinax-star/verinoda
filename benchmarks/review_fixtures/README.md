# review_fixtures: labelled changes for `verinoda review` (docs/DESIGN.md D35)

Each fixture is one change to a git copy of an example project, with gold labels written by reading
the code, **before any review rule was written**. `dev.json` (36 fixtures) is used while the rules are
built and tuned; `heldout.json` (11) stays unrun until the rules are frozen. Both files were written by
the same person who writes the rules (the builder), so even the held-out set is not independent: it
only guards against tuning on its answers. `MANIFEST.json` holds the sha256 of both files as committed
before the first rule existed; `git log` shows the order.

Projects: `orders_app`, `glow_mod` and `forge_mod` (copies of `examples/`), and `vcopy` (a git clone of a
380-file copy of Verinoda's own repository, commit `184b0db`).

## A fixture

```
{"id", "project", "title",
 "edits": [{"file", "find", "replace"}],       exact text, applied once each, in order, to the base
 "options": {"observe": true, ...},            review options for this fixture
 "gold": {
   "changes":   [{"symbol": "file::Qual.name", "kind": body|signature|added|removed|module_statement|config_key}],
   "must_find": [{"concern", "at": "file:line" or "file:a-b", "note"}],
   "may_find":  [...],                         acceptable, never counted against precision or recall
   "must_not_flag": [{"concern"[, "at"]}],     any such finding is a false positive (no "at": the whole concern)
   "must_say_unknown": [{"kind"[, "at", "or_bound_at"]}],
   "tests": {"reach": [test ids], "no_test_reaches": [symbols], "observed_reach", "observed_not_reached"},
   "affected": ["file::Qual.name"]}}           dependents a reviewer must see (blast radius)
```

Concerns: `persistence`, `security`, `performance`, `public_api`, `config`, `entry_points`. A line in
`at` is a line of the changed tree, except where the note says "base line": a guard or check that the
change removed is cited at its line in the base commit. Test ids: `file::test_name` (pytest) and
`file::methodName` (JUnit methods). Unknown kinds: `runtime_tests` (the project's tests cannot be run or
observed here: Gradle is not allowlisted), `loop_bound` (a loop's bound is not established;
`or_bound_at` names the line of a cap that, when cited instead, also satisfies the item).

## Scoring (verinoda/benchmark/review_eval.py)

- A reported finding matches a gold item when the concern is the same and the gold `at` (file and line
  or range) contains the finding's `at` line or one of its `evidence_at` lines.
- Precision per concern: over the reported findings with status `strong_inference` or above; a finding
  that matches no `must_find` or `may_find` item is a false positive (so is anything matching
  `must_not_flag`). Recall per concern: `must_find` items matched by any reported finding.
- Unknowns: `must_say_unknown` items reported (by kind, and `at` when given).
- Changed symbols: exact (symbol, kind) matches; a fixture with `"changes": []` must report none.
- Blast radius: `affected` symbols listed among the dependents, and the dependent count against
  `verinoda map --view impact` on the same diff.
