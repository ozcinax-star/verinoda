# Agents on real repositories (2026-10-02)

Pre-registered in `benchmarks/agent_compare/DESIGN_REALWORLD.md`. The design, the 51 questions (`questions.json`)
and the scorer (`score_rw.py`, with the decision statistic) were committed before any session's result was read.
Ten pinned open-source repositories from `benchmarks/realworld/` (130-974 tracked files, seven languages), 107
hand-written gold facts; the questions list 113 fact slots (six facts are asked for by two questions). One Claude
Code session (Claude Opus 5.5) per question and arm, 153 sessions, none failed:

- `none`: file reading and search only;
- `verinoda_first`: the same plus the Verinoda CLI (5999912 plus this branch's fixes), told to start with `analyze`;
- `graphify_first`: the same plus Graphify 0.9.73's CLI, told to start with `query`.

The prompts are the first study's round-2 prompts with the folder changed. A fact is found when the answer cites a
location in one of its source files within 3 lines of a gold source line.

## Result

| arm | facts found | input tokens | output tokens | tool calls | model calls per session |
|---|---|---|---|---|---|
| none | 107/113 | 13.86 M | 109,290 | 319 | 4.84 |
| verinoda_first | 109/113 | 15.78 M | 91,576 | 299 | 5.37 |
| graphify_first | 111/113 | 15.85 M | 97,199 | 314 | 5.45 |

| pair (paired over 51 questions) | facts difference [95 % CI] | W/T/L | input tokens [95 % CI] | output tokens [95 % CI] | tool calls [95 % CI] |
|---|---|---|---|---|---|
| verinoda_first - none | +2 [-4 to +10] | 2/47/2 | 1.14x [1.06-1.23] | 0.84x [0.78-0.91] | 0.94x [0.86-1.03] |
| graphify_first - none | +4 [-1 to +12] | 3/47/1 | 1.14x [1.08-1.22] | 0.89x [0.81-0.98] | 0.98x [0.91-1.07] |

**Decision (as pre-registered): no measurable difference in facts.** The interval of `verinoda_first - none`
includes zero, so no claim that Verinoda adds facts. The agent alone found 107 of 113 (95 %): as in the first study
there is little room above it. Both index arms used about 14 % more input tokens and 11-16 % fewer output tokens
than the agent alone, with intervals that exclude 1. **The first study's input-token saving (0.84x on corpora of
37-226 files) did not replicate here: on these repositories starting with `analyze` cost more input, not less.**

No session touched the gold, the question files or another arm's folder (`usage.json`: 0 leaks). Every
`verinoda_first` session called Verinoda (51 `analyze`, 4 `query`); every `graphify_first` session called Graphify;
`none` called neither.

## Where the facts differed (5 of 51 questions)

| question | none | verinoda_first | graphify_first | why |
|---|---|---|---|---|
| gson q2 (how `toJson`/`fromJson` find the adapter) | 0/3 | 3/3 | 3/3 | `none` cited every line, but as `G:647-649` after declaring `G = gson/.../Gson.java`: not a path, so not scored |
| fastapi template q2 | 1/2 | 2/2 | 2/2 | `none` did not cite `get_current_user`'s definition |
| axios q5 | 1/2 | 1/2 | 2/2 | the Node adapter's file not cited by `none` and `verinoda_first` |
| guzzle q2 | 2/2 | 1/2 | 1/2 | `verinoda_first` cites `transfer()`'s body (`Client.php:1505`), 6 lines below its definition (1499) |
| guzzle q5 | 2/2 | 1/2 | 2/2 | `verinoda_first` cites `__invoke`'s body (`RedirectMiddleware.php:71-73`), 8 lines below 63 |

Two sensitivity checks that were **not** pre-registered, applied to all arms alike
(`benchmarks/agent_compare/sensitivity_rw.py`, `sensitivity.json`). Resolving aliases an answer declares (only the
gson answer above declares any): none 110, verinoda_first 109 (-1 [-4 to +2]), graphify_first 111 (+1 [-2 to +4]).
A 10-line window: none 107, verinoda_first 111 (+4 [0 to +11]), graphify_first 112 (+5 [0 to +12]). Neither
changes the decision: no interval lies above zero.

## Why input went up

Read from the transcripts. `analyze` output is small (8,395 characters per call on average, one per session), and
it did not replace reading: the agents went on to read the cited code (176 shell calls, mostly line ranges, against
81 for `none`), and all tool output together came to 1.32 M characters against 1.13 M for `none`. Starting with
`analyze` adds a sequential first turn: 5.37 model calls per session against 4.84, and every turn re-reads the whole
conversation, so input grows with the number of turns more than with the size of `analyze`'s answer. In the first
study it went the other way (4.56 model calls per session against 5.39 for `none`): on corpora of 37-226 files
the agent needed fewer turns after `analyze`, here it verified instead. The output saving is not in the answers
(about 45 k tokens and 1.4 k citations per arm, all three alike) but in what the sessions wrote on the way.

## Limits

One session per cell, no repeat of `none` here (the first study measured that noise floor); the questions were
written by a model from the gold; the agent may know these popular projects; the scorer is strict about lines (the
two `verinoda_first` misses describe the right code).

## Files

`questions.json` (fixed before the sessions), `sessions.json` (answers and self-reports), `usage.json` (per session,
from the transcripts), `scores.json` (per question and arm, by repository, the decision statistic), `summary.md`,
`sensitivity.json` (the two checks above, not pre-registered).
