# An offline oracle of the signals a tool could give an agent (X0)

Date: 2026-10-03. Nothing was run with a model: this reads the sessions already recorded in
`../agent-compare-sel4-2026-10-02/` and `../agent-compare-big-2026-10-02/`.

## Question

The two agent studies found that the agent alone is at the ceiling on single-file tasks and loses nearly all its recall on
multi-file tasks: the files it misses are siblings of the ones it found. Before building anything that tells an agent about
those files, which signal, computed from what the agent already had in hand, would have listed the files it missed?

## Method

For every session of the arm `none` (the agent alone) that did not name every gold file, the *anchors* are the files the agent
did name (the best case for a signal that starts from the files already found). Each signal turns the anchors, and for
`query` the report's text, into a ranked list of candidate files. The oracle counts a missed gold file as covered by a signal
when it is among the first k of the list. `benchmarks/agent_compare/signal_oracle.py` computes it (`python
benchmarks/agent_compare/signal_oracle.py CONFIG.json OUT.json`); the configs here have their folders made generic (`<work>`
is where the study's copies were: each seL4 task at its base commit with its history, Home Assistant at the study's base
commit with its whole history).

Counts are per session: a file missed in all three runs of a task counts three times. The task sets are small (6 seL4 tasks and
8 Home Assistant tasks have a miss), so these are exploratory numbers to choose what to build, not results to claim.

## Results

### seL4: 16 sessions of `none` that missed gold, 28 missed gold files (6 tasks: i1004, i1015, i1091, i1170, i1566, i805)

| signal (what it lists) | mean list size | top 3 | top 5 | top 8 |
|---|---|---|---|---|
| samedir: files in the folders of the files the agent named | 12.6 | 0 (0 %) | 0 (0 %) | 2 (7 %) |
| twins: files of the same name elsewhere in the tree | 11.5 | 3 (11 %) | 4 (14 %) | 4 (14 %) |
| pair: files of the same stem with another extension (`x.c` / `x.h`) | 13.9 | 5 (18 %) | 6 (21 %) | 8 (29 %) |
| neighbors: files the named files include or import, and files that include or import them | 39.7 | 2 (7 %) | 3 (11 %) | 3 (11 %) |
| cochange_path: files changed in the same commits as a named file (that file's own last 300 commits, bulk commits left out) | 6.6 | 4 (14 %) | 4 (14 %) | 6 (21 %) |
| cochange_verinoda: Verinoda's co-change reading as shipped (the last 1000 commits of HEAD) | 0.9 | 2 (7 %) | 2 (7 %) | 2 (7 %) |
| query: `verinoda query` on the report's text | 4.8 | 5 (18 %) | 7 (25 %) | 7 (25 %) |
| union5: the first 5 of each of cochange_path, twins, pair, neighbors and query, together | 13.2 | 15 (54 %) | 15 (54 %) | 15 (54 %) |

### Home Assistant: 14 sessions of `none` that missed gold, 15 missed gold files (8 tasks: pr166748, pr173270, pr177499, pr180357, pr180472, pr182734, pr182949, pr183498)

| signal (what it lists) | mean list size | top 3 | top 5 | top 8 |
|---|---|---|---|---|
| samedir: files in the folders of the files the agent named | 16.9 | 3 (20 %) | 8 (53 %) | 10 (67 %) |
| twins: files of the same name elsewhere in the tree | 2248.9 | 0 (0 %) | 0 (0 %) | 0 (0 %) |
| pair: files of the same stem with another extension (`x.c` / `x.h`) | 1820.9 | 0 (0 %) | 0 (0 %) | 0 (0 %) |
| neighbors: files the named files include or import, and files that include or import them | 3.9 | 6 (40 %) | 9 (60 %) | 9 (60 %) |
| cochange_path: files changed in the same commits as a named file (that file's own last 300 commits, bulk commits left out) | 9.5 | 7 (47 %) | 9 (60 %) | 10 (67 %) |
| cochange_verinoda: Verinoda's co-change reading as shipped (the last 1000 commits of HEAD) | 0.1 | 0 (0 %) | 0 (0 %) | 0 (0 %) |
| query: `verinoda query` on the report's text | 4.1 | 0 (0 %) | 0 (0 %) | 0 (0 %) |
| union5: the first 5 of each of cochange_path, twins, pair, neighbors and query, together | 15.2 | 12 (80 %) | 12 (80 %) | 12 (80 %) |

## What it says

- **Verinoda's co-change reading as shipped covers almost nothing** (2 of 28 on seL4, 0 of 15 on Home Assistant, at any k): it
  reads the last 1000 commits of the whole repository, and on a busy repository the files of one task are in a handful of them.
  The path-limited reading (each anchor's own last 300 commits) covers 6 of 28 (seL4) and 10 of 15 (Home Assistant) in the top 8.
  The window, not the idea, was the fault.
- **Which signal helps depends on the repository.** On seL4 (C: headers and sources in pairs, one file per architecture) the
  same-stem partner and `query` are the largest single signals; on Home Assistant (one folder per integration, a sibling file in the same
  folder) the co-change and import neighbours do, `query` finds none of the 15 and the same-name signals are useless (thousands of
  files are called `__init__.py` or `const.py`).
- **Together, a short list covers a lot**: the union of the first five of five signals (about 13 to 15 files) lists 15 of the 28
  missed files on seL4 and 12 of the 15 on Home Assistant. A list that long is more than an agent should be handed; a build
  has to rank it and keep it to what fits in the prompt.
- `samedir` on Home Assistant is an artefact of the way it is listed (alphabetical, in folders of a median 8 Python files, so any
  list of 8 covers most of a folder); it is not a signal.

## Limits

The anchors are the best case: a session that found nothing has no anchors, and these signals do not help it (`query` and the
report's own words have to). Covering a file is not the agent using it; that is what the next studies measure. The sessions were
chosen by having a miss, so the signals are not scored on the tasks `none` solved (where they can only add noise).

## Files

`sel4_oracle.json`, `ha_oracle.json` (one row per session with a miss: gold, named, anchors, every signal's size and covered
files at each k), `sel4_config.json`, `ha_config.json`.
