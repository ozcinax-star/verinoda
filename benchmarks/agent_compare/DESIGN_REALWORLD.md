# Agent comparison on real repositories: pre-registration (2026-10-02)

Written before any question of this study was written and before any session ran. The first study
(`DESIGN.md`, `results/agent-compare-2026-10-02/`) ran on corpora of 37-226 files, where the agent alone found
98 % of the facts: a ceiling that leaves no room for a tool to add facts. This one uses the ten pinned
open-source repositories of the real-world run (`benchmarks/realworld/`, 130-974 tracked files, seven languages),
whose 107 gold facts were written by hand at the pinned sha before Verinoda was run on them.

## Questions

The gold facts are statements with their source (`gin.go:236`), not questions. Per repository one agent writes
4-6 questions a developer new to the code would ask, together covering every fact, each question listing the fact
ids it is meant to bring out. Rules for the writer: no file name, path, line number or symbol spelling that the
question does not need (a function's name may appear when a developer would ask about it by name); no wording
copied from the fact. A second, independent agent checks every question for leaks (an answer location or a detail
only the gold gives) and for whether each listed fact is something a good answer to it would contain; a flagged
question is rewritten once and checked again. The questions are fixed before the first session.

## Corpora and arms

Per repository three fresh clones of the pinned sha (`git clone --shared` of the real-world clone, then
`checkout --detach <sha>`): `none` (no index), `verinoda` (`verinoda init` + `scan` with the build under test,
5999912 plus this branch's fixes), `graphify` (`graphify update .`, 0.9.73, AST only). Arms, one session per
question and arm, the arms of a question started together, Claude Opus 5.5 at the session's effort:

- `none`: file reading and search only;
- `verinoda_first`: the same plus the Verinoda CLI, told to start with `analyze` (the first study's best arm, and
  what the verinoda-live mod's `nudge` says);
- `graphify_first`: the same plus the Graphify CLI, told to start with `query`.

The prompts are the first study's round-2 prompts, unchanged except for the folder.

## Scoring

A fact is **found** when the answer cites a location in one of the fact's source files within 3 lines of one of
its source lines (`path:line` or a `path:a-b` range overlapping that window; the path compared by suffix, case kept).
Nothing else is scored. Tool use, gold access, tokens and tool calls are read from the transcripts
(`usage.py`), as in the first study.

- **Primary:** facts found per arm over the 107, paired per question against `none`.
- **Secondary:** input/output tokens and tool calls (paired ratios with a percentile bootstrap over questions);
  per language; per repository size.
- **Decision this study informs:** whether Verinoda adds facts for an agent on real repositories (not only cost),
  stated as a claim only if `verinoda_first` finds more facts than `none` with the paired difference's 95 %
  bootstrap interval above zero. Otherwise the result is reported as no measurable difference in facts.

## Limits stated in advance

One session per cell; the questions are written by a model from the gold, so they may lean towards what the gold
says; seven languages but ten repositories; the agent may know some of these popular projects from training.
