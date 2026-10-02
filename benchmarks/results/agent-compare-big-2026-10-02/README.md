# Three Claude Codes on a big repository (2026-10-02)

Pre-registered in `benchmarks/agent_compare/DESIGN_BIG.md`. The design, the miner (`big_tasks.py`), the 40 tasks
(`tasks.json`), the session harness (`big_run.py`), the scorer (`score_big.py`), the configuration (`config.json`)
and the index builds (`builds.json`) were committed before the first study session. No model wrote any task.

**Repository:** `home-assistant/core` at `92c6588` (2026-08-14), 27,078 tracked files (9,890 Python files under
`homeassistant/`, about 1,460 integrations). **Tasks:** 40 real bug reports, each closed by one pull request merged
after that commit; the answer is the non-test source files the pull request changed (31 tasks have one gold file,
6 two, 3 three; 40 different integrations; the issue never names a gold file's path). A session is asked which files
must change (at most five, read-only, no network).

**Arms**, all real headless `claude -p` sessions with the model pinned (`claude-sonnet-5-5`), the same tools, and no
user settings, plugins, skills, CLAUDE.md or MCP servers (`--setting-sources project,local --strict-mcp-config`;
the user's own `graphify` and `verinoda` commands taken off `PATH`):

| arm | what it has |
|---|---|
| `none` | original Claude Code: no index, nothing installed |
| `graphify` | Graphify 0.9.73's AST graph (`graphify update .`) and its own Claude Code integration, `graphify claude install` |
| `verinoda_mod` | Verinoda's `verinoda setup . --agents claude` (index, project skill, MCP server) and the verinoda-live 0.4.2 mod with its `auto` setting at `nudge` |
| `verinoda_setup` | the same Verinoda setup without the mod (secondary: what the mod adds) |

Both indexes were built on the full checkout (`builds.json`): Graphify 333,673 nodes, 914,148 edges, 8,418
communities in 70 min; Verinoda 334,253 nodes, 909,222 edges in 54 min (both at the same time, so under mutual
load). A warm query takes about 50 s (Graphify) and 29 s (Verinoda) and Verinoda's holds about 1.8 GB.

## Result (pre-registered, the first run)

| arm | recall (of 40) | solved (all gold files) | hit@1 | precision | sessions that used the arm's tool | input tokens |
|---|---|---|---|---|---|---|
| none | 35.83 | 32 | 35 | 0.657 | - | 3.53 M |
| graphify | 37.00 | 34 | 36 | 0.677 | 7 of 40 | 3.30 M |
| verinoda_mod | 37.33 | 34 | 38 | 0.675 | 6 of 40 | 3.51 M |
| verinoda_setup | 37.83 | 35 | 37 | 0.700 | 2 of 40 | 3.58 M |

| decision (recall, summed over 40 tasks) | difference [95 % CI] | W/T/L | input tokens | turns |
|---|---|---|---|---|
| 1. verinoda_mod - none | +1.50 [+0.00 to +4.00] | 2/38/0 | 0.99x [0.91-1.09] | 1.01x [0.94-1.07] |
| 2. graphify - none | +1.17 [-0.67 to +3.67] | 2/37/1 | 0.93x [0.84-1.03] | 0.96x [0.88-1.04] |
| 3. verinoda_mod - graphify | +0.33 [-1.17 to +2.00] | 2/37/1 | 1.06x [0.97-1.16] | 1.06x [0.98-1.13] |

**No claim on any of the three:** the rule was an interval that excludes zero, and decision 1's lower bound is
exactly zero (two tasks better, none worse, 38 unchanged). The agent alone found 89.6 % of the gold files and
solved 32 of 40 tasks completely: the ceiling of the earlier studies is here too, on a repository roughly 30 to 200
times larger. Seconds are not compared: the arms ran with different concurrency (plain 4 sessions at once, the others 2).

## The tools were shown and rarely used

Every session was told about its arm's tool, as that tool's own setup does it (checked in the transcripts):
Graphify's `CLAUDE.md` rule in 40 of 40 sessions and its PreToolUse hook ("MANDATORY ... you MUST run `graphify query`
before grepping raw files") in 39; the mod's nudge ("start by running Verinoda's analyze") in 40 of 40, with the MCP
server and the skill; the Verinoda setup's MCP server and skill in 40 of 40. The agent used the tool in 7, 6 and 2
sessions of 40 (graphify, verinoda_mod, verinoda_setup). Where it did, its recall was exactly that of `none` on the
same tasks (7.00 against 7.00, 5.50 against 5.50, 1.50 against 1.50). Every task where the arms differ is one where the
differing arms made **no tool call at all** (`pr180472`: `none` 0.00, the other three 1.00, no tool call in any): the
difference is not the tool.

## Not pre-registered: a second run, the noise floor, the two runs pooled

After the first run showed that the differences came from sessions that used no tool, every session was run a
second time (same prompts, same copies, `results_r2.jsonl`; `extra.py` -> `extra.json`). One caveat: both runs share
the Verinoda indexes, so the few analyses of the first run are in the second run's atlas.

| arm | recall run 1 | recall run 2 | pooled (mean of the two) | sessions using the tool, run 2 |
|---|---|---|---|---|
| none | 35.83 | 37.00 | 36.42 | - |
| graphify | 37.00 | 36.83 | 36.92 | 8 |
| verinoda_mod | 37.33 | 36.83 | 37.08 | 7 |
| verinoda_setup | 37.83 | 37.83 | 37.83 | 3 |

- **The noise floor:** the same arm run twice moves by chance as much as the arms differ: `none` run 1 - run 2 =
  -1.17 [-3.67 to +0.67] (one task better, two worse, 37 unchanged). In the second run `none` is above `graphify`
  and `verinoda_mod`.
- **Pooled decisions:** verinoda_mod - none +0.67 [-0.50 to +2.00]; graphify - none +0.50 [+0.00 to +1.50];
  verinoda_mod - graphify +0.17 [-0.58 to +1.00]; verinoda_mod - verinoda_setup -0.75 [-2.00 to +0.00].
  Nothing reaches a claim, and the mod does not add to Verinoda's own setup here.
- **Tool users again:** in the second run too, the sessions that used the tool scored what `none` scored on their
  tasks (7.50 against 7.50, 6.00 against 6.00, 2.17 against 2.17).

## Why the big repository did not make these tasks hard

Home Assistant's bug-report template asks for the integration, so all 40 issues name the integration the bug is in
(40 of 40). That removes the hard part of a 1,460-integration repository: what is left is choosing among the files
of one integration folder, which holds a median of 8 Python files (2 to 51; 31 of the 40 have 15 or fewer), and
a `grep` of the issue's words finds the right one. An index has nothing to add to that, and the agent, which knows
it, mostly skipped the tool even when told to use it first.

## What this says about the mod

- On a 27,078-file repository, with real bug reports and the mod's own recommended setting, the agent solved the
  tasks as well without Verinoda, Graphify or the mod as with them; the decision rule gave no claim, and the
  second run and the noise floor say the small positive differences are chance.
- The nudge reached every session and the agent used Verinoda in 6 to 7 of 40 (whether before its first search was
  not measured). Graphify's hook, worded as MANDATORY, was followed by use of Graphify in 7 to 8 of 40. An agent that
  can answer from a `grep` of the issue's words mostly does, and these tasks let it.
- The mod is not what was missing: `verinoda_mod` and `verinoda_setup` do not differ.
- This does not show the tools are useless on big repositories. It shows that when the issue names the component,
  the repository's size does not matter. The test that would is a report that does not name it (a user who does not
  know which integration is at fault); that was not run.

## Fixed along the way (before the study sessions)

The smoke test (on a task outside the study) found two faults in the mod that would have voided the arm, both fixed with
tests before the study: the nudge never reached `claude -p` sessions (their prompts carry origin `sdk`; 0.4.1) and
it skipped prompts over 2,000 characters, which a pasted bug report exceeds (0.4.2).

## Limits

One repository, one model, one session per cell in the pre-registered run; 40 tasks with one to three gold files
each, so recall moves in coarse steps and only a large difference could show; the gold is the files one pull
request changed; Graphify's graph is the AST one (its LLM-extracted graph would cost thousands of calls on 27,000
files); every arm has the repository's own agent instructions; the two Verinoda copies and the runs share index
state (a few analysis records); the second run, the noise floor and the size and naming analysis were not
pre-registered.

## Files

`tasks.json` (the 40 tasks), `candidates.json` (the 323 candidates and `tasks.json`'s filter counts), `tasks_config.json`,
`config.json`, `builds.json`, `results.jsonl` (the pre-registered run, one line per session: the files named, tool
calls, turns, cost, the answer), `results_r2.jsonl` (the second run), `scores.json` and `summary.md` (`score_big.py`),
`extra.py` and `extra.json` (the analyses not pre-registered).
