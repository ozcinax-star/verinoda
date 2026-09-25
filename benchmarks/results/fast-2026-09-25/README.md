# Fast-harness results, 2026-09-25

Verinoda-only runs of the benchmark question sets on prepared, indexed copies (the harness scores
exactly like `verinoda bench run`: `runner.score` on the text each approach delivers; raw search and
Graphify are not re-run). One private set is measured too and left out of these files. Files named
`*a-*` / `*b-*` are fresh-index A/B pairs (prepared indexes rebuilt by the code under test); the others
reuse prepared indexes, which is valid for question-time changes only. `x*` files are experiments
that were measured and not kept. See docs/BENCHMARKS.md (Update 2026-09-25).

Each file: `{set: {verinoda_analyze, verinoda_retrieve, verinoda_retrieve_text: facts found,
per_q: {question: {approach: facts}}, negatives: {approach: matched}, facts: total, seconds}}`;
audit files: `{set: {questions, met, met_wrong, met_partial, met_full, unmet_but_full, rows}}`.

- `1-before.json`: code at 964b9d8, before this round (prepared indexes of that code)
- `2-analyze-passages.json`: analyze carries the passages query gives (list of lines)
- `3-grounded-verdicts.json`: met only on claims about the question; data path write sites as locations
- `4-turkish-stems.json`: 82 more Turkish stems in the seed glossary (yapi later removed)
- `5-multi-part.json`: multi-part questions: splits and dropped subjects
- `6a-stemmer-head.json`: fresh indexes, code at 5a097cb (items 1-4 committed)
- `6b-stemmer-new.json`: fresh indexes, y-final words meet their -ies forms
- `7a-resolve-once-head.json`: fresh indexes, code at 1c40297
- `7b-resolve-once-new.json`: fresh indexes, each path resolved once per build
- `8a-user-set-head.json`: verinoda_user_tr with the code at 3bd1b94 (before this round)
- `8b-user-set-new.json`: verinoda_user_tr with items 1-4
- `9-before-tr-question-words.json`: the eight public sets with the code at d3165b8 (prepared indexes of that code)
- `9-tr-question-words.json`: the same indexes; a code word in a Turkish question takes no co-occurrence pairs, an unconfirmed Turkish stem expands only to its inflections, a term keeps its highest weight
- `x1-rejected-passive-stems.json`: REJECTED: 29 passive verb stems; 4 results up, 6 down
- `x2-rejected-named-translation-weight.json`: REJECTED: Turkish words no code name contains weighed below their named translation; mixed
- `audit-1-before.json`: verdict audit: analyze's sub-question verdicts against the gold facts its claims carry
- `audit-3-grounded-verdicts.json`: verdict audit: analyze's sub-question verdicts against the gold facts its claims carry
- `audit-4-turkish-stems.json`: verdict audit: analyze's sub-question verdicts against the gold facts its claims carry
- `audit-5-multi-part.json`: verdict audit: analyze's sub-question verdicts against the gold facts its claims carry
- `audit-8b-user-set.json`: verdict audit: analyze's sub-question verdicts against the gold facts its claims carry
