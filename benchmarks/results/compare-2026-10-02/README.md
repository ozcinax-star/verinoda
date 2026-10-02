# The eight public sets on the current build, against Graphify, 2026-10-02

Verinoda 0.4.0 at 5999912 (`competitor-backlog`), `python -m verinoda.benchmark run --repeat 2` per set, once with
the Graphify CLI 0.9.73 (latest on PyPI, `graphify-0.9.73/`) and once with 0.9.65 (upstream at 20a20d30, the
version the earlier figures used, `graphify-0.9.65/`). Vendored Graphify renderer at 20a20d30 in both. Windows 11,
16 CPUs, other benchmarks running at the same time: the timings in the result files are under load; facts and
tokens are not affected. `summary.md` (from `summarize.py`) has every set; old figures are the last committed ones
(Verinoda: `token-wins-2026-09-26/7-review-fixes.json`, fd30f5b; Graphify and raw: the round-3 files of 2026-09-23
and `mods-2026-09-24/final/`).

## Totals

| approach | eight sets: facts found | tokens per question | facts per 1k tokens |
|---|---|---|---|
| Verinoda query (text) | **284/319** | 1,313 | **2.52** |
| Verinoda analyze (text) | **285/319** | 1,890 | 1.75 |
| Graphify, vendored renderer | 71/319 | 1,393 | 0.59 |
| Graphify CLI 0.9.73 | 67/319 | 1,480 | 0.53 |
| Graphify CLI 0.9.65 | 68/319 | 1,480 | 0.53 |
| raw text search | 129/319 | 3,991 | 0.38 |

At the same budget Verinoda's query finds 4.2 times the facts of Graphify's CLI 0.9.73, as on 2026-09-26 (4.1).

## Against the last committed figures

Fact by fact (the found ids per question, not only the totals), Verinoda's query and analyze lost three facts since
fd30f5b and gained none, all on the held-out set:

| approach | question | lost |
|---|---|---|
| query (text) | heldout h08 | `h08.cli` |
| analyze (text) | heldout h08 | `h08.cli` |
| analyze (text) | heldout h02 | `h02.set` |

The same three are lost with either Graphify version (the Verinoda side does not depend on it). A first-parent
bisect of `h08.cli` (query text) over fd30f5b..5999912, each step a fresh run of the set at that commit with HEAD's
gold (`bisect/` notes below), names 8b69778 "What an agent carries and cites (D60)" as the first commit without it;
that commit's own message records the loss ("heldout h08: the last passage no longer fits the 6,000-character
budget"): passages number their lines since D60, and the numbers take budget. A known trade-off, not a new defect.
The same bisect of `h02.set` (analyze text) names 918e6e2 "Less noise in an answer (D58)", whose message records
"analyze 318 -> 317 (a token inside a refuted claim ...)": since D58 the context the critique refuted is counted, not
printed, and h02's text alternative `REPOATLAS_EXPERIMENT` was in such a claim. Both losses are recorded trade-offs.
Bisect method: `git rev-list --first-parent`, a detached worktree per step, the harness run with every `GIT_*`
variable removed (a first attempt under `git bisect run` let the harness's own `git init`/`commit` write into the
repository being bisected; repaired, see the session notes). Graphify's vendored
renderer gained two facts on forge_mod (12 -> 14) with the same pinned code: Verinoda-independent, so most likely
the corpus copy or the scorer moved; not investigated. Graphify's CLI 0.9.73 finds one fact fewer than 0.9.65
(heldout 8 -> 7) and is otherwise identical on these sets.

## The `verinoda_user_tr` set fails the harness's gold check

At HEAD the harness refuses `verinoda_user_tr` as `gold_invalid`: u01.detect's text alternative `copies.detect`
occurs nowhere in the set's pinned corpus (3bd1b94). Its earlier figures came from other harnesses (token-wins,
the token economist's table), which do not run the check. The rerun used `verinoda_user_tr.patched-questions.json`,
the set with only that alternative removed (its location alternative is valid), labelled as patched in every file
(`*.patched.json`). The set itself is fixed on this branch (the alternative removed from
`verinoda/benchmark/questions/verinoda_user_tr.json`, with a test that its gold verifies against its pinned corpus).

## After the ranking fix (`after-fix/`)

The same eight sets rerun (Verinoda approaches only, `--repeat 1`) after the change that lets a definition the
question names in a source file the graph has no node for yet rank as the symbol would (`verinoda/search_index.py`,
`named_new`; found by `benchmarks/mod_live` on the large corpus, see `mod-live-self-quiet-2026-10-02/README.md`).
Fact by fact, every question of every set, for query text, query JSON and analyze: **no fact gained or lost**
(284, 237 and 285 of 319, as before); token counts within a few tokens (analyze's own variation). The change only
reaches files the graph lacks, which a freshly indexed benchmark corpus never has.
