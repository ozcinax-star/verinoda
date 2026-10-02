# Does checking each edit help an agent write working code? (2026-10-02)

Pre-registered in `benchmarks/agent_compare/DESIGN_GUARD.md`. The design, the 30 tasks (`tasks.json`, with every
review and mechanical verdict in `task_checks.json`), the session harness (`guard_run.py`) and the scorer
(`score_guard.py`) were committed before any session ran.

The feature under test is verinoda-live 0.4.0's check after edits: after every Edit or Write of a Python, Java or
Kotlin file, `verinoda check --diff` reads the changed lines and the names on them that do not exist in the project
or its environment are told to the model with the edit's result, with the nearest real names.

30 coding tasks, 20 on Graphify internals changed after mid-2026 (after the model's training data) and 10 on
SQLModel, each asking for a new function with an exact signature and behaviour, a hidden pytest file and a reference
patch (in a fresh clone every hidden test fails as the code is and passes with its patch). Two real headless Claude
Code sessions per task (`claude -p`, Claude Opus 5.5), the same prompt, a fresh clone with a copied Verinoda index,
MCP servers off in both; they differ only in `--plugin-dir`:

- `plain`: no plugin;
- `guard`: verinoda-live 0.4.0 loaded, its check after edits on.

Before the run, the harness was checked on a deliberate case: writing a name SQLModel removed, the `guard` session
received the note and quoted it, the `plain` session received none.

## Result (pre-registered)

| arm | hidden test passed | absent names left in the code | check notes received | turns | cost |
|---|---|---|---|---|---|
| plain | 30/30 | 0 | - | 332 | $15.23 |
| guard | 30/30 | 0 | 0 (65 edits, all of Python files) | 373 | $15.20 |

| decision (paired over 30 tasks) | difference [95 % CI] | W/T/L |
|---|---|---|
| 1. passes, guard - plain | +0 [+0 to +0] | 0/30/0 |
| 2. absent names left, plain - guard | +0 [+0 to +0] | 0/30/0 |
| turns, guard - plain (reported) | +41 [-10 to +93] | 17/2/11 |

**Both decisions: no measurable difference.** Every session of both arms passed its hidden test, no session left a
name that does not exist, and the check never reported anything: the 65 edits of the `guard` sessions were all of
Python files, so each was in its scope, and no note reached the model (a clean check is silent, so its runs are not
recorded one by one; the harness's own `verinoda check --diff` after every session agrees: nothing absent).
By repository and by whether the prompt names a helper (`summary.md`) the picture is the same: all passed, no notes.

## Why the check had nothing to catch

Read from the transcripts: no session in either arm edited before reading. Before its first edit a session made a
median of 4 reads or searches (at least 1), then a median of 2 edits. The agent looked up the functions it was about
to call, so it called them by their real names, even in Graphify code written after its training data. The failure
the check is built for, code written from a memory of an API, did not happen in these tasks.

## What this says about the mod

- The check works (shown on the deliberate case) and cost about nothing here: the same cost, and a median session of
  227 s against 216 s.
- It did not change the outcome on these tasks, because the agent already reads before it writes. It can only
  matter where an agent writes without reading: very large edits, many files at once, or a model that reads less.
  None of that was measured here.
- Together with the earlier studies: in reading and in writing, on these tasks, an agent that reads the files is at
  the ceiling, and Verinoda has not made it more correct.

## Run notes

The run was stopped once from outside after 51 of 60 sessions; the harness resumes, and the 9 sessions in flight were
run again from the start. On that resume, a clone left by an interrupted session could not be deleted (git's
read-only pack files on Windows); the harness now clears the flag first, and the remaining 8 sessions ran. Every one
of the 60 cells has exactly one completed session.

## Limits

Two repositories, one language, 30 tasks; one session per cell; the tasks were written by a model and specify an
exact signature, which invites reading the code; eight prompts name a helper their solution uses (marked before the
run, reported apart). With every session passing, this study cannot show a difference in either direction.

## Files

`tasks.json`, `task_checks.json`, `config.json` (paths sanitized), `results.jsonl` (one line per session: passed, the
absent sites `verinoda check --diff` found, notes, turns, cost, the session's final message), `scores.json` and
`summary.md` (`score_guard.py`).
