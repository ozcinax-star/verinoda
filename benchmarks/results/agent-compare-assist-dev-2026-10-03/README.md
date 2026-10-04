# The assist features on the development sets (2026-10-03 and 04)

Pre-registered in `../../agent_compare/DESIGN_ASSIST.md` (read its amendments of 2026-10-04 with it). **These are development
results**: the two sets are the ones the arms were compared on to choose which to confirm, they were used in the earlier
studies, and the signals were built looking at their missed files, so they are the optimistic ones. The claims are in
`../agent-compare-assist-2026-10-03/`.

## What ran

Real headless `claude -p` sessions of `claude-sonnet-5-5` (Claude Code 2.1.288 as recorded in the Home Assistant sessions; the
seL4 sessions did not log the version), started with `--setting-sources project,local --strict-mcp-config`: none of the
user's own settings, plugins, skills or MCP servers, so a session sees the project and what the arm's setup wrote into it. Tools
`Bash Read Glob Grep Skill`, Verinoda's MCP server where the project was set up with it, the mod's own `locate` and `coupled`
where its `assist` is on, 80 turns at most, and the prompt of `DESIGN_BIG.md` (the bug report, then "name the files that must
change", the same words for every arm).
Arms: `none`; `verinoda_setup` (the project set up with Verinoda, no mod); and the verinoda-live 0.5.0 mod with one `assist`
setting each: `inject` (the located files go in with the prompt), `coupled` (the files that change together with a source file
go in with the result of reading it), `gate` (the first search of a task is answered with the located files), `tool` (`locate`
and `coupled` as tools listed in front, and a paragraph in the system prompt), `passive` (inject, coupled), `passive_gate`
(inject, coupled, gate), `full` (coupled, tool, prompt), `strict` (full, gate); and on seL4 `forced` (`tool`, and the prompt
tells the agent to call `locate` first: what the tool's content is worth when it is used).

- **seL4**: the 21 tasks of `DESIGN_SEL4.md`, each at its base commit **with its history up to it**, 11 arms, **3 runs** (693
  sessions).
- **Home Assistant**: the 40 tasks of `DESIGN_BIG.md` at the pinned commit with its whole history, `none` twice and the other
  arms once (440 sessions); three copies of the Verinoda index shared the mod arms' sessions.

## Two things that went wrong, and were corrected

1. **The nudge reached 26 of the 231 first-run seL4 sessions** although no arm asks for it: the mod keeps its stored choices in
   one file shared by every session that loads it, and a stored `auto: nudge` in that file applied to the study's sessions. Those sessions are in `sel4_dev_r1.jsonl.nudged`; they were run again after the mod was changed to read
   no stored choice in a session with nobody at the keyboard. In the data kept here every session's `assist.nudge` is 0.
2. **On Home Assistant the first sessions of the arms that depend on `locate` got no treatment**: the mod asked the daemon
   through the host's own HTTP call, whose time limit ended answers that took 10 to 100 s (`inject` reached the model in 2 of
   its first 19 sessions). The 129 mod-arm sessions are in `ha_dev_r1.jsonl.undelivered`; the mod arms were run again after the
   mod was changed to run the commands, which ask the daemon themselves. The tables below are the rerun.

## seL4 (21 tasks, 3 runs)

21 tasks, 3 runs; recall is summed over tasks (a task's mean over its runs).

| arm | recall | gain over `none` [95 % CI] | W/T/L | solved | hit@1 | sessions where the mod put something in front of the model | sessions that called its tool | sessions that used ToolSearch | median seconds | cost per session |
|---|---|---|---|---|---|---|---|---|---|---|
| none | 17.89/21 | - | - | 15.7 | 19.7 | 0/63 | 0/63 | 0/63 | 15 | $0.048 |
| verinoda_setup | 17.61/21 | -0.28 [-1.11 to +0.33] | 1/18/2 | 15.3 | 19.7 | 0/63 | 0/63 | 0/63 | 21 | $0.049 |
| coupled | 18.00/21 | +0.11 [-0.89 to +1.11] | 3/16/2 | 15.7 | 19.3 | 36/63 | 0/63 | 0/63 | 24 | $0.053 |
| forced | 18.33/21 | +0.44 [+0.00 to +1.17] | 2/19/0 | 15.7 | 19.7 | 0/63 | 63/63 | 0/63 | 24 | $0.061 |
| full | 17.78/21 | -0.11 [-1.17 to +1.00] | 1/18/2 | 15.3 | 19.0 | 35/63 | 44/63 | 0/63 | 23 | $0.053 |
| gate | 18.28/21 | +0.39 [+0.00 to +1.17] | 1/20/0 | 15.7 | 19.7 | 63/63 | 0/63 | 0/63 | 26 | $0.043 |
| inject | 18.44/21 | +0.56 [-0.28 to +1.61] | 3/17/1 | 15.3 | 20.0 | 63/63 | 0/63 | 0/63 | 25 | $0.045 |
| passive | 18.33/21 | +0.44 [-0.22 to +1.22] | 3/17/1 | 16.0 | 20.0 | 63/63 | 0/63 | 0/63 | 26 | $0.042 |
| passive_gate | 18.83/21 | +0.94 [+0.17 to +1.89] | 4/17/0 | 16.3 | 20.0 | 63/63 | 0/63 | 0/63 | 29 | $0.046 |
| strict | 17.89/21 | +0.00 [-0.44 to +0.39] | 2/18/1 | 16.0 | 19.0 | 45/63 | 43/63 | 0/63 | 24 | $0.042 |
| tool | 18.11/21 | +0.22 [-0.83 to +1.33] | 3/16/2 | 16.0 | 18.7 | 0/63 | 51/63 | 0/63 | 24 | $0.056 |

Pairs:

| pair (recall, summed over tasks) | difference [95 % CI] | W/T/L |
|---|---|---|
| coupled - none | +0.11 [-0.89 to +1.11] | 3/16/2 |
| forced - none | +0.44 [+0.00 to +1.17] | 2/19/0 |
| full - none | -0.11 [-1.17 to +1.00] | 1/18/2 |
| gate - none | +0.39 [+0.00 to +1.17] | 1/20/0 |
| inject - none | +0.56 [-0.28 to +1.61] | 3/17/1 |
| passive - none | +0.44 [-0.22 to +1.22] | 3/17/1 |
| passive_gate - none | +0.94 [+0.17 to +1.89] | 4/17/0 |
| strict - none | +0.00 [-0.44 to +0.39] | 2/18/1 |
| tool - none | +0.22 [-0.83 to +1.33] | 3/16/2 |
| coupled - verinoda_setup | +0.39 [-0.78 to +1.56] | 3/16/2 |
| forced - verinoda_setup | +0.72 [+0.00 to +1.83] | 3/18/0 |
| full - verinoda_setup | +0.17 [-0.89 to +1.28] | 3/16/2 |
| gate - verinoda_setup | +0.67 [+0.00 to +1.56] | 3/18/0 |
| inject - verinoda_setup | +0.83 [-0.17 to +2.11] | 4/16/1 |
| passive - verinoda_setup | +0.72 [+0.17 to +1.33] | 5/16/0 |
| passive_gate - verinoda_setup | +1.22 [+0.22 to +2.50] | 5/16/0 |
| strict - verinoda_setup | +0.28 [-0.50 to +1.22] | 2/17/2 |
| tool - verinoda_setup | +0.50 [-0.83 to +2.00] | 4/15/2 |

Mean recall by the number of gold files (what is left for a tool to add is where `none` is low):

| tasks | none | verinoda_setup | coupled | forced | full | gate | inject | passive | passive_gate | strict | tool |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 gold file | 1.00 | 1.00 | 0.98 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 0.98 |
| 2 gold files | 0.46 | 0.38 | 0.58 | 0.50 | 0.33 | 0.46 | 0.50 | 0.50 | 0.62 | 0.42 | 0.54 |
| 3 or more gold files | 0.69 | 0.70 | 0.67 | 0.78 | 0.81 | 0.81 | 0.81 | 0.78 | 0.78 | 0.74 | 0.76 |


**What reached the agent and what it did with it** (every gold file of every session; "shown" is the file being in what the mod
delivered: the located list with the prompt, a refused search, the tool's answer or a note about a file read):

| arm | sessions | gold files (each gold file of each session) | shown to the model | named in the answer | shown and named | shown, not named | named, never shown | neither |
|---|---|---|---|---|---|---|---|---|
| none | 63 | 102 | 0 | 74 | 0 | 0 | 74 | 28 |
| verinoda_setup | 63 | 102 | 0 | 74 | 0 | 0 | 74 | 28 |
| inject | 63 | 102 | 48 | 82 | 42 | 6 | 40 | 14 |
| coupled | 63 | 102 | 9 | 76 | 4 | 5 | 72 | 21 |
| gate | 63 | 102 | 48 | 81 | 41 | 7 | 40 | 14 |
| tool | 63 | 102 | 44 | 79 | 38 | 6 | 41 | 17 |
| passive | 63 | 102 | 50 | 80 | 39 | 11 | 41 | 11 |
| passive_gate | 63 | 102 | 50 | 83 | 41 | 9 | 42 | 10 |
| full | 63 | 102 | 47 | 78 | 40 | 7 | 38 | 17 |
| strict | 63 | 102 | 53 | 75 | 42 | 11 | 33 | 16 |
| forced | 63 | 102 | 55 | 80 | 49 | 6 | 31 | 16 |

## Home Assistant (40 tasks; `none` 2 runs, the others 1)

40 tasks, 2 runs; recall is summed over tasks (a task's mean over its runs).

| arm | recall | gain over `none` [95 % CI] | W/T/L | solved | hit@1 | sessions where the mod put something in front of the model | sessions that called its tool | sessions that used ToolSearch | median seconds | cost per session |
|---|---|---|---|---|---|---|---|---|---|---|
| none | 36.75/40 | - | - | 33.5 | 35.0 | 0/80 | 0/80 | 0/80 | 13 | $0.061 |
| verinoda_setup | 37.83/40 | +1.08 [+0.00 to +2.50] | 3/37/0 | 35.0 | 34.0 | 0/40 | 0/40 | 0/40 | 21 | $0.066 |
| coupled | 36.83/40 | +0.08 [-0.75 to +1.00] | 1/38/1 | 33.0 | 36.0 | 25/40 | 0/40 | 0/40 | 20 | $0.066 |
| full | 36.83/40 | +0.08 [-0.75 to +1.00] | 1/38/1 | 33.0 | 35.0 | 28/40 | 11/40 | 0/40 | 20 | $0.070 |
| gate | 37.00/40 | +0.25 [-0.75 to +1.50] | 1/38/1 | 34.0 | 35.0 | 38/40 | 0/40 | 0/40 | 74 | $0.073 |
| inject | 37.33/40 | +0.58 [-0.50 to +2.00] | 2/37/1 | 34.0 | 36.0 | 40/40 | 0/40 | 0/40 | 69 | $0.066 |
| passive | 36.83/40 | +0.08 [-0.75 to +1.00] | 1/38/1 | 33.0 | 37.0 | 40/40 | 0/40 | 0/40 | 73 | $0.070 |
| passive_gate | 37.33/40 | +0.58 [-0.50 to +2.00] | 2/37/1 | 34.0 | 38.0 | 40/40 | 0/40 | 0/40 | 104 | $0.077 |
| strict | 37.33/40 | +0.58 [-0.50 to +2.00] | 2/37/1 | 34.0 | 35.0 | 38/40 | 15/40 | 0/40 | 51 | $0.076 |
| tool | 36.83/40 | +0.08 [-2.50 to +2.17] | 3/36/1 | 34.0 | 36.0 | 0/40 | 22/40 | 0/40 | 27 | $0.070 |

Pairs:

| pair (recall, summed over tasks) | difference [95 % CI] | W/T/L |
|---|---|---|
| coupled - none | +0.08 [-0.75 to +1.00] | 1/38/1 |
| full - none | +0.08 [-0.75 to +1.00] | 1/38/1 |
| gate - none | +0.25 [-0.75 to +1.50] | 1/38/1 |
| inject - none | +0.58 [-0.50 to +2.00] | 2/37/1 |
| passive - none | +0.08 [-0.75 to +1.00] | 1/38/1 |
| passive_gate - none | +0.58 [-0.50 to +2.00] | 2/37/1 |
| strict - none | +0.58 [-0.50 to +2.00] | 2/37/1 |
| tool - none | +0.08 [-2.50 to +2.17] | 3/36/1 |
| coupled - verinoda_setup | -1.00 [-2.50 to +0.00] | 0/38/2 |
| full - verinoda_setup | -1.00 [-2.50 to +0.00] | 0/38/2 |
| gate - verinoda_setup | -0.83 [-2.17 to +0.00] | 0/38/2 |
| inject - verinoda_setup | -0.50 [-1.50 to +0.00] | 0/39/1 |
| passive - verinoda_setup | -1.00 [-2.50 to +0.00] | 0/38/2 |
| passive_gate - verinoda_setup | -0.50 [-1.50 to +0.00] | 0/39/1 |
| strict - verinoda_setup | -0.50 [-1.50 to +0.00] | 0/39/1 |
| tool - verinoda_setup | -1.00 [-3.00 to +0.00] | 0/39/1 |

Mean recall by the number of gold files (what is left for a tool to add is where `none` is low):

| tasks | none | verinoda_setup | coupled | full | gate | inject | passive | passive_gate | strict | tool |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 gold file | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 0.97 |
| 2 gold files | 0.62 | 0.75 | 0.58 | 0.58 | 0.67 | 0.67 | 0.58 | 0.67 | 0.67 | 0.75 |
| 3 or more gold files | 0.67 | 0.78 | 0.78 | 0.78 | 0.67 | 0.78 | 0.78 | 0.78 | 0.78 | 0.78 |


| arm | sessions | gold files (each gold file of each session) | shown to the model | named in the answer | shown and named | shown, not named | named, never shown | neither |
|---|---|---|---|---|---|---|---|---|
| none | 80 | 104 | 0 | 89 | 0 | 0 | 89 | 15 |
| verinoda_setup | 40 | 52 | 0 | 47 | 0 | 0 | 47 | 5 |
| coupled | 40 | 52 | 11 | 45 | 8 | 3 | 37 | 4 |
| gate | 40 | 52 | 19 | 45 | 18 | 1 | 27 | 6 |
| inject | 40 | 52 | 19 | 46 | 18 | 1 | 28 | 5 |
| tool | 40 | 52 | 23 | 46 | 20 | 3 | 26 | 3 |
| full | 40 | 52 | 18 | 45 | 14 | 4 | 31 | 3 |
| passive | 40 | 52 | 30 | 45 | 27 | 3 | 18 | 4 |
| strict | 40 | 52 | 30 | 46 | 26 | 4 | 20 | 2 |
| passive_gate | 40 | 52 | 30 | 46 | 27 | 3 | 19 | 3 |

The seconds of Home Assistant's sessions are not an idle machine's: other jobs (the build of the next study's copies) ran
meanwhile. `locate` on Home Assistant took 10 to 30 s a call when measured alone (retrieval over its 27,078 tracked files), against 0.2 to
1.3 s on seL4.

## The choice

The rule of the pre-registration (`choose_arm.py`: the largest summed recall gain over `none` on the 61 tasks both
sets share, the two best within 1.0 going to the one with fewer features):

| arm | features | summed gain over `none` |
|---|---|---|
| passive_gate | 3 | +1.53 |
| inject | 1 | +1.14 |
| gate | 1 | +0.64 |
| strict | 3 | +0.58 |
| passive | 2 | +0.53 |
| tool | 1 | +0.31 |
| coupled | 1 | +0.19 |
| full | 2 | -0.03 |

`passive_gate` (+1.53, 3 features) and `inject` (+1.14, 1) are within
1.0 of each other, so `mod_best` is **`inject`** and `mod_second` **`passive_gate`** (`selection.json`). Those,
`none`, `graphify` and `verinoda_setup` are what the confirmatory study runs.

## What it says

- **Delivery works, and the tool is used once it is in front.** The located files reached the model in every `inject` session
  (63 of 63 on seL4, 40 of 40 on Home Assistant) and the gate answered the first search in 63 of 63 and 38 of 40. Listed in
  front of the model instead of behind ToolSearch, `locate` was called in 51 of 63 `tool` sessions on seL4 and 22 of 40 on Home
  Assistant, ToolSearch in none. In the earlier studies Verinoda's tools were called in none of the 286 sessions of the arms
  that had them (126 on seL4, 160 on Home Assistant).
- **Recall barely moves.** seL4, summed recall over 21 tasks: `none` 17.89; `passive_gate` +0.94 [+0.17, +1.89], `inject` +0.56,
  `forced` +0.44, `gate` +0.39, `tool` +0.22, `coupled` +0.11, `strict` 0.00, `full` -0.11, `verinoda_setup` -0.28. Home
  Assistant, 40 tasks: `none` 36.75; `verinoda_setup` +1.08 [0.00, +2.50], `inject`, `passive_gate` and `strict` +0.58, `gate`
  +0.25, the others +0.08. Against `none` the one interval that excludes zero is `passive_gate` on seL4, and nothing here is
  corrected for the number of comparisons made.
- **There is little to gain on these sets.** 45 of the 61 tasks have one gold file (14 of 21 on seL4, 31 of 40 on Home
  Assistant), and every arm gets it (mean recall 0.97 to 1.00); on seL4 only 7 of the 21 tasks differ between the arms at all.
  What is left is in the tasks with more gold files: on seL4's four two-gold tasks `none` 0.46, `passive_gate` 0.62, `coupled`
  0.58; on its three tasks with three or more `none` 0.69, `inject`, `gate` and `full` 0.81.
- **The files `none` misses are not mostly the ones the lists hold.** Gold files `none` missed in every run: 7 in 4 tasks on
  seL4, 7 in 6 tasks on Home Assistant. With `inject` the model was shown 9 of the 21 instances on seL4 (the 7 files, in 3
  sessions each) and named 6 of them, 3 of those shown; on Home Assistant it was shown 1 of 7 and named 2. Over every gold file:
  on seL4 `inject` showed 48 of 102 and the agent named 82 against `none`'s 74; on Home Assistant it showed 19 of 52 and the
  agent named 46 of 52 against `none`'s 89 of 104 (88 % against 86 %).
- **The tool, used, did not beat the passive features.** `forced`, which tells the agent to call `locate` first (63 of 63 did),
  +0.44 [0.00, +1.17] on seL4; `tool`, which the agent called in 51 of 63, +0.22; `coupled` alone +0.11 on seL4 and +0.08 on
  Home Assistant.
- **Answers name more files with the mod, and not more invented ones.** seL4 (63 sessions an arm): `none` named 94 files,
  `verinoda_setup` 96, the mod arms 103 to 112, of which 0 or 1 do not exist at the base commit (`inject` 1, `tool` 1, the others
  0). Home Assistant: `none` 6 of 149 named (4.0 %), the other arms 3 or 4 of 75 to 87 (3.7 to 5.3 %).
- **Cost and time.** seL4: $0.048 a session for `none` against $0.042 to $0.061 for the mod arms (`forced` $0.061, `tool`
  $0.056), median time 15 s against 23 to 29 s. Home Assistant: $0.061 against $0.066 to $0.077, median 13 s against 20 s
  (`coupled`, `full`) to 104 s (`passive_gate`; `inject` 69, `passive` 73, `gate` 74): the arms that wait for `locate` before
  the first answer pay its 10 to 30 s a call, and these sessions shared the machine with other jobs.

## What it does not say

That the features do nothing: these sets cannot show a difference, because the agent alone is near the ceiling on most of
their tasks. The confirmatory set (72 tasks of 33 repositories, 2 to 6 gold files each, 3.5 on average) is built for it;
`none`'s recall there is what its claims are read against.

## Limits

- Both sets are development sets: the arms were compared on them to pick two, and the signals were looked at on their missed
  files. A gain here is an optimistic one and a null here is not a finding.
- Home Assistant has one run of each mod arm, and its intervals are wide (+0.58 has [-0.50, +2.00]).
- `locate`'s history signal reads at most 300 commits per anchor file but pools that limit across the anchors, so one busy
  anchor can use up the commits a quiet one needed (documented in `DESIGN_ASSIST.md`, not changed for the study).
- The query of `inject` is the whole prompt (up to 4,000 characters), so a long report with code in it is a noisy query.
- Time on Home Assistant is for sessions that shared the machine; isolated measurements of `locate` are taken after the
  confirmatory study, on an idle machine, and published with it.
- What an index that is behind the working tree does to `locate`: `../stale-locate-2026-10-04/`. What the signals would
  find with no model in the loop: `../signal-oracle-2026-10-03/`.

## Files

`sel4_dev_r1.jsonl`, `sel4_dev_r2.jsonl`, `sel4_dev_r3.jsonl`, `ha_dev_r1.jsonl`, `ha_dev_r2.jsonl` (one line per session: the
files named, tool calls, turns, cost, the answer, what the mod put in front of the model), `sel4_report.md` and `ha_report.md`
(the tables above), `*_funnel.json`, `*_answer_checks.json`, `selection.json`, the run configs (`sel4_dev_r1.json`, `ha_dev_r1.json`,
`ha_dev_r2.json`; seL4's runs 2 and 3 differ from run 1 in the output file only), `sel4_prep_config.json` (how the seL4 copies
were built), and the sessions set aside (`sel4_dev_r1.jsonl.nudged`, `ha_dev_r1.jsonl.undelivered`).
