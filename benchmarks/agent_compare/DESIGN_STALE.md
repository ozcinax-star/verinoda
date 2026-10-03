# Agents after the code moved on: a fresh index against a stale one (pre-registration, 2026-10-02)

Written before any question of this study was written and before any session ran. The two agent studies so far
(`DESIGN.md`, `DESIGN_REALWORLD.md`) asked about code the index had been built on; the agent alone found 95-98 % of
the facts and an index added none. What the verinoda-live mod claims is different: that it keeps the index fresh
while the code changes. This study asks what a fresh index is worth against the index a project has when nobody
refreshes it.

## Corpora

The ten pinned repositories of the real-world run (`benchmarks/realworld/`). For each, the pinned sha is **NEW**
(the code as it is now) and an earlier first-parent commit is **OLD** (when the index was last built), chosen before
any question was written so that OLD..NEW changes a realistic amount of source: 100 commits back for graphify,
axios, full-stack-fastapi-template, gin, fzf, bat and express; 200 for sqlmodel; 50 for gson and guzzle (a few
weeks to about a year of upstream history; express changed little in that time). History fetched from GitHub
(`git fetch --depth 600 origin <NEW>`). Three working copies per repository, all checked out at NEW:

- `none`: no index;
- `stale`: `verinoda init` + `scan` at OLD, then `git checkout NEW` with no update: the index a project has when
  nobody refreshes it. Verinoda itself does not refresh on a query; it reports the changed files ("never silently
  answer from an older tree"), and the agent sees that report;
- `fresh`: the same, then `verinoda update --fast` (what the mod runs after edits) and a wait until the background
  graph build has caught up (no file behind, no build lock): the mod's settled state.

The build under test is this branch's (5999912 plus its fixes).

## Questions

Per repository one agent reads the OLD..NEW history and diff and writes 2-5 questions a developer working on the
code as it is now would ask, **each about something that changed between OLD and NEW** (a function moved, renamed,
added or removed, a call path or a default changed), so that an answer read off OLD would be wrong. Each question
lists its facts as statements with their NEW source (`path:line`), and its **stale traps**: identifiers or paths that
an answer drawn from OLD would cite but that no longer exist at NEW. Rules: non-test source only; no path, line
number or symbol spelling the question does not need; nothing copied from the facts. A second, independent agent
checks every fact against NEW (`git show NEW:path`), that each fact is not already true at OLD, that each trap is
absent at NEW (`git grep -w` at NEW), and the question for leaks; it drops or corrects what fails. Then a mechanical
check: every fact's file exists at NEW with that line, every trap is absent at NEW. The questions are fixed and
committed before the first session.

## Arms

One Claude Code session (Claude Opus 5.5) per question and arm, the prompts of the earlier studies' round 2 with
the folder changed:

- `none`: file reading and search only, on `none`;
- `verinoda_stale`: told to start with Verinoda's `analyze`, on `stale`;
- `verinoda_fresh`: the same prompt, on `fresh`.

## Scoring

- **Facts found** (primary): a fact is found when the answer cites a location in its NEW source file within 3 lines
  of its NEW line (`score_stale.py`, the rule of `score_rw.py`).
- **Stale citations** (primary for the second decision): citations an answer makes that do not exist at NEW (a file
  absent at NEW, or a line past its end), plus citations of a stale trap's OLD location, counted per answer.
- **Trap mentions** (secondary): answers that name a stale trap's identifier or path (an answer may name one to say
  it was renamed, so this is reported, not decided on).
- Tokens and tool calls from the transcripts (`usage.py`), as before.

## Decisions

1. **Fresh against stale, facts:** a claim that a fresh index lets the agent find more facts is made only if
   `verinoda_fresh - verinoda_stale` summed over questions has a 95 % percentile-bootstrap interval (over questions,
   10,000 resamples, seed 20261002) above zero.
2. **Fresh against stale, stale citations:** a claim that a fresh index prevents stale answers is made only if
   `verinoda_stale - verinoda_fresh` stale citations has its interval above zero.
3. Both arms are also paired against `none` and reported; no claim is made from those pairs unless an interval
   excludes zero.

Otherwise the result is reported as no measurable difference.

## Limits stated in advance

One session per cell; the questions are written by a model from the diff, so they are about what changed, which is
the case the mod is for, not a sample of all questions; the agent reads files that are always at NEW, so the index
can only mislead it as far as it trusts the index over what it reads; the agent may know these projects' later
history from training.
