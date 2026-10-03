# Three Claude Codes on seL4 (2026-10-02)

Pre-registered in `benchmarks/agent_compare/DESIGN_SEL4.md`; the design, the miner (`commit_tasks.py`), the 21 tasks
(`tasks.json`), the copy builder (`commit_prep.py`), the harness (`big_run.py`), the scorer (`score_big.py`) and the
configuration (`config.json`) were committed before the first session. No model wrote any task.

`seL4/seL4` is the C microkernel: **1,059 tracked files** (not a big repository), terse names, one function per
architecture, generated code. The earlier no-agent ContextBench table had the index far ahead of lexical search in this
kind of code, so this was where a difference was most likely.

**Tasks:** 21 real issues (17 closed by a merged pull request, 4 by a commit, all by keyword), each at **its own base commit**
as a one-commit repository, so the fix is not in it (61 candidates; dropped: 18 without 1 to 6 gold code files, 14 naming a gold
path, 8 by length). Gold: the code and build files the change modified or removed (14 tasks have one, 4 two, 2 three, 1 six;
13 `.c`, 11 `.h`, 4 `.cmake`). Issues date from 2016 to 2025, the fixes from 2016 to 2026. **Arms** as in the Home Assistant
study (`../agent-compare-big-2026-10-02/`): `none`, `graphify` (AST graph and `graphify claude install`), `verinoda_mod`
(`verinoda setup` and the verinoda-live 0.4.2 mod with the nudge on) and `verinoda_setup`; real headless `claude -p`
sessions, `claude-sonnet-5-5`, the same tools, no user settings. Each index took about 40 s (Graphify, median 5,627 nodes) and
53 s (Verinoda, 5,543 nodes) to build. **Every session was run three times** and scored on the mean.

## Result (pre-registered: the mean of the three runs)

| arm | recall (of 21) | solved | hit@1 | precision | tasks where the tool was used in some run | input tokens |
|---|---|---|---|---|---|---|
| none | 17.67 | 15.7 | 19.3 | 0.853 | - | 1.85 M |
| graphify | 18.28 | 16.0 | 19.3 | 0.844 | 12 | 1.91 M |
| verinoda_mod | 17.78 | 16.0 | 19.7 | 0.843 | **0** | 1.86 M |
| verinoda_setup | 17.56 | 15.3 | 19.7 | 0.860 | **0** | 2.03 M |

| decision (recall, summed over 21 tasks, mean of the runs) | difference [95 % CI] | W/T/L |
|---|---|---|
| 1. verinoda_mod - none | +0.11 [-0.50 to +0.67] | 2/18/1 |
| 2. graphify - none | +0.61 [-0.28 to +1.72] | 3/16/2 |
| 3. verinoda_mod - graphify | -0.50 [-1.33 to +0.11] | 1/17/3 |

**No claim on any of the three.** The agent alone found 84 % of the gold files (solved 15.7 of 21 tasks) and the arms are
within 0.7 of 21. Run to run the same arm moves by as much: `none` -0.83 to +0.17 between runs, `verinoda_mod` up to -1.17
(each pair's interval includes or touches zero; `summary.md`).

## The Verinoda arms never used Verinoda

In **none of the 126 sessions of `verinoda_mod` and `verinoda_setup`** did the agent call Verinoda: no `mcp__verinoda` tool,
no `verinoda` command (checked twice: by the harness's counter and by a separate raw scan of every tool input). Every one of
those sessions had been told: the mod's nudge ("start by running Verinoda's analyze on it before any other search", with the
exact command) in 63 of 63 `verinoda_mod` sessions, and the MCP server's instructions and the project skill in all 126. Every
session's first call was `Grep`, `Read`, `Glob` or `Bash`.

It was not that the tools could not be used: a probe session in the same harness, asked to, ran `verinoda query` through
`Bash` and an MCP tool (`project_query`, found with `ToolSearch`), and both worked. The agent chose not to. The same holds
in the Home Assistant study, where the earlier version of this finding ("6 to 7 of 40 sessions used Verinoda") was wrong:
the counter took a working copy's folder name (`verinoda`, `verinoda_mod`) in a command for a call. The counts of both
studies were recounted from the transcripts (`recount_tools.py`; the old counts are kept in `*_uncorrected`) and the
Home Assistant result is 0 of 160 Verinoda-arm sessions.

Graphify's tool was used, more often here than in Home Assistant: in 21 of 63 sessions (8, 9 and 4 of 21 in the three runs;
Home Assistant 2 and 3 of 40). Its hook fires at the moment the agent calls `Grep` ("MANDATORY: ... you MUST run
`graphify query` before grepping raw files", 63 of 63 sessions) where the nudge was given with the prompt. Not
pre-registered: in the 21 sessions that used Graphify the recall was 17.33 where `none` scored 16.17 on the same tasks and
runs; in the other 42, 37.50 against 36.83. The agent chooses when to use the tool, so this is not a controlled comparison.

## What this says

- On seL4's real bug reports an agent with grep reads its way to the right file as well as one with an index: no decision
  reaches a claim, and Verinoda cannot be credited or blamed for the small differences because it was never called.
- The mod's nudge, given with the prompt, did nothing to the agent's behaviour in 126 sessions across two repositories.
  An instruction given at the moment of the first search (Graphify's way) was followed by use of the tool in about a third
  of these sessions.
  If the mod is to change what the agent does, that is where it has to speak.
- The difference the ContextBench table showed (an index finds the file where lexical search does not) does not reach the
  agent, which searches iteratively and reads. It would take an agent that cannot do that, or a task without words to grep.

## Limits

21 tasks, so recall moves in coarse steps and only a large difference could show; a small repository; one model; one
repository. **The agent may know these fixes**: most of the issues and changes are from before the model's training data
ends, which would lift every arm alike (nothing in the transcripts shows a lookup: no session used the network). The gold is the
files one change touched. Graphify's graph is the AST one. The runs share each task's Verinoda atlas, which was never used.
The analyses of Graphify's users and the nudge-versus-hook reading were not pre-registered.

## Files

`tasks.json`, `candidates.json`, `tasks_config.json`, `config.json`, `results_r1.jsonl`, `results_r2.jsonl`, `results_r3.jsonl`
(one line per session; the tool counts are the recounted ones), `scores.json` and `summary.md` (`score_big.py`, mean of the
runs, each run and the noise floor).
