# Agents after the code moved on: a fresh index against a stale one (2026-10-02)

Pre-registered in `benchmarks/agent_compare/DESIGN_STALE.md`. The design and the OLD/NEW commits (`plan.json`), the
scorer (`score_stale.py`) and the 50 questions (`questions.json`, with every verifier verdict in
`question_checks.json`) were committed before any session ran.

Ten real-world repositories at their pinned commit (NEW); the index built at an earlier upstream commit (OLD, 50-200
first-parent commits back). 50 questions, each about something that changed between OLD and NEW (153 facts at NEW,
30 stale traps: names and paths that exist only at OLD). Three arms, one Claude Code session (Claude Opus 5.5) per
question and arm, all on copies checked out at NEW:

- `none`: no index;
- `verinoda_stale`: told to start with `analyze`, index built at OLD and never updated (57-217 files behind);
- `verinoda_fresh`: the same prompt, index refreshed with `update --fast` and the graph caught up (what the
  verinoda-live mod does; 4-39 s for the update and 11-76 s more for the graph).

## Result (pre-registered)

| arm | facts found | stale citations (answers with one) | citations | answers naming a removed name |
|---|---|---|---|---|
| none | 147/153 | 14 (5) | 910 | 3 |
| verinoda_stale | 147/153 | 18 (6) | 981 | 3 |
| verinoda_fresh | 144/153 | 17 (7) | 1014 | 1 |

| comparison (paired over 50 questions) | difference [95 % CI] | W/T/L | input tokens | output tokens | tool calls |
|---|---|---|---|---|---|
| facts, fresh - stale (decision 1) | -3 [-9 to +1] | 1/46/3 | 0.92x [0.85-1.00] | 1.04x [0.98-1.12] | 0.94x [0.87-1.02] |
| stale citations, stale - fresh (decision 2) | +1 [-2 to +5] | 2/47/1 | | | |
| facts, fresh - none | -3 [-9 to +2] | 2/44/4 | 0.98x [0.89-1.07] | 0.81x [0.76-0.88] | 0.85x [0.74-0.96] |
| facts, stale - none | +0 [-4 to +4] | 2/46/2 | 1.06x [0.97-1.15] | 0.78x [0.73-0.85] | 0.90x [0.79-1.02] |

**Decision 1: no measurable difference in facts** between a fresh and a stale index; **decision 2: no measurable
difference in stale citations.** The agent alone found 147 of 153 facts (96 %): the ceiling of the earlier studies is
here too.

**The stale-citation measure did not measure staleness, and no claim is made from it.** Of its 49 counts, 44 are
citations of current code that happens to sit within 3 lines of a trap's OLD position in a file that still exists
(the three arms cite these alike, `none` included), and 4 are a `127.0.0.1:<port>` address read as a location. One
citation in 150 answers points at code that is gone: `scripts/mkdocs_hooks.py:16` (sqlmodel q5, `verinoda_stale`),
in a sentence saying the hook *was* removed. The pre-registered pair `stale - none` (+4 [+1 to +8]) is this
artifact. Every answer that names a removed identifier or path (7) names it as history ("did exist", "a commit
removed it", "there is no such field"), never as current code. **No answer in any arm described code that no longer
exists as if it were there:** the agents read the files on disk, which are always at NEW.

## A deviation found after the sessions: `analyze` refreshes small projects itself

The design said Verinoda does not refresh a stale index when asked a question. That holds for `query`, not for
`analyze`: before answering it refreshes the index when the project has fewer than 300 files, or when at most 200
files changed and the rebuild is estimated at 15 s or less (`analysis.REFRESH_*`); otherwise it answers from the old
index and says so ("index: NOT refreshed (202 files changed ...); answered from the previous index"). After the
sessions, the `stale` copies of express, full-stack-fastapi-template, gin, guzzle and fzf (124-237 files) were
fresh; those of axios, sqlmodel, gson, graphify and bat (305-950 files) were as behind as before. So the `stale`
arm measured what an agent gets **without the mod**: on a small project Verinoda refreshes itself on the first
`analyze`, on a large one it does not.

## Not pre-registered: by project size, and a repeat

`extra.py` -> `extra.json`. The split is the index state measured after the sessions (Verinoda's own threshold),
not the results. Run 1 is an earlier run of the same 150 sessions, of which 24 (axios, express and the fastapi
template, all three arms) were lost to a network outage (`ENOTFOUND`); run 2 was rerun in full and is the primary
result above. In 118 of the 126 cells both runs have, the same number of facts was found.

On the five large projects, where the index really stayed stale (25 questions):

| pair | facts | input tokens, run 2 | input tokens, run 1 | tool calls, run 2 |
|---|---|---|---|---|
| stale - none | 72 vs 73 of 75 | **1.23x [1.13-1.34]** | **1.18x [1.07-1.31]** | 1.07x [0.97-1.18] |
| fresh - stale | 72 vs 72 | **0.81x [0.72-0.91]** | 0.94x [0.80-1.11] | **0.85x [0.75-0.96]** |
| fresh - none | 72 vs 73 | 1.00x [0.88-1.13] | 1.12x [0.97-1.29] | 0.91x [0.80-1.02] |

- A stale index did not cost facts, but it cost work: the agent used about a fifth more input tokens than with no
  index at all, in both runs. On these projects `analyze` answers from the old index and says it was not refreshed
  (202 or 217 files changed, or the rebuild was estimated as too slow); why that costs more input than reading the
  code with no index was not measured.
- A fresh index removed that overhead in run 2 (0.81x input and 0.85x tool calls against the stale index) and came
  back to the cost of no index; in run 1 the saving pointed the same way but its interval includes 1. Stated as
  observed in one run, not as replicated.
- On the five small projects, where `analyze` had refreshed the stale index itself, fresh and stale cost the same
  (input 1.08x [1.00-1.17], run 2), as expected.

## What this says about the mod

- On these questions an index, fresh or stale, did not change what the agent found: it reads the current files and
  the answer is at the ceiling either way. A stale index did **not** lead the agent into describing removed code.
- Freshness matters for cost, and only on projects large enough that Verinoda will not refresh itself inside
  `analyze` (here 305-950 files): there a stale index made the agent spend more than having no index (both runs),
  and the fresh index the mod keeps brought it back (one run).
- On small projects the mod's freshness adds nothing that `analyze` does not already do on its own.

## Limits

One session per cell per run; the size split and the repeat are not pre-registered; the questions were written by a
model from the diffs and are about what changed, not a sample of all questions; five repositories per size group.

## Files

`plan.json` (OLD/NEW per repository), `questions.json`, `question_checks.json`, `tree_new.json` (files and line
counts at NEW), `sessions.json` and `usage.json` (run 2, primary), `sessions_run1.json` and `usage_run1.json` (run 1,
126 sessions), `scores.json` and `summary.md` (`score_stale.py`), `extra.py` and `extra.json` (the analyses above).
