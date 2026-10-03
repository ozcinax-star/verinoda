# Three Claude Codes on seL4 (pre-registration, 2026-10-02)

Written before any session of this study ran. It repeats `DESIGN_BIG.md`'s comparison (original Claude Code, Graphify's
own Claude Code integration, Verinoda's setup with the verinoda-live mod, and Verinoda's setup alone) on `seL4/seL4`,
the C microkernel. Two things led here. The ContextBench table (no agent) had the index finding the right file far more
often than lexical search in short-named C and C++ code (ponyc 0.12 to 0.68, simdjson 0.14 to 0.50, nlohmann/json 0.38 to
0.62), and the Home Assistant study found nothing because every bug report there names its component. seL4 is that C
profile: terse names, the same function written once per architecture (arm, x86, riscv), generated code (`.bf` bitfield
files, syscall XML), issues written by kernel developers.

**It is not a big repository:** 1,059 tracked files (447 `.h`, 183 `.c`, 112 `.dts`, 112 `.cmake`, 39 `.py`), 5,002
commits. The study tests a kind of code, not a size.

## Tasks

Real bug reports, not written by a model. An issue of `seL4/seL4` closed by a merged pull request that says so by keyword
(`Fixes #N`, `Closes seL4/seL4#N`), or named the same way in the message of a commit; 61 candidates (48 pull requests,
13 commits). Each task has **its own base commit**: the pull request's base, or the commit's parent, so the repository is
as it was just before the change and the fix cannot be in it. The gold is the code and build files the change modified or
removed (`.c .h .S .py .bf .xml .xsd .cmake .dts .pbf .lds .in`, `CMakeLists.txt`; not tests, documents or CI files), as
they exist at the base commit. Filters, fixed now: 1 to 6 gold files; at most 12 files changed; issue text of 150 to
4,000 characters; the issue newer than the fix is dropped; the issue text names no gold file by its path (a developer's
issue often does). Candidates are ordered by sha256 of `seed:issue number` (seed 20261002) and up to 40 that pass are the
tasks: **21 pass** (dropped: 18 without 1 to 6 gold code files, 14 naming a gold path, 8 by length). The selection
(`commit_tasks.py`, `tasks.json`) is committed before the first session. The gold limit is 6, not the 4 of the Home
Assistant study, because a kernel fix often touches one file per architecture; it was set before any session, from the
candidates' file counts alone.

A session gets the issue's title and text and is asked which files must change (at most five, read-only, no network), as in
`DESIGN_BIG.md`; the prompt names the project and never mentions an index or a tool.

## Arms and copies

The four arms of `DESIGN_BIG.md`, unchanged: `none`; `graphify` (Graphify 0.9.73's AST graph and `graphify claude install`);
`verinoda_mod` (`verinoda setup . --agents claude` and the verinoda-live 0.4.2 mod with `auto` at `nudge`); `verinoda_setup`
(the setup without the mod). Real headless `claude -p` sessions, `claude-sonnet-5-5`, the same tools, no user settings,
plugins, skills, CLAUDE.md or MCP (`--setting-sources project,local --strict-mcp-config`), the user's own `graphify` and
`verinoda` commands off `PATH`.

Because the base commits differ, every task has its own four copies (`commit_prep.py`): a one-commit repository fetched at
the base commit (no history, so no later commit to find), and from it `none`; `graphify` (an index built there);
`verinoda_mod` (an index built there); `verinoda_setup` (a copy of the finished Verinoda one, so the two share no analysis
record). Each copy is checked to be at the base commit.

## Runs and scoring

**Every session is run three times** (the same prompts and copies; the Verinoda analysis records of an earlier run are in a
later run's atlas), because the Home Assistant study found one run's differences to be as large as the same arm's
run-to-run difference. The unit of analysis is the mean of a task's three recalls in an arm.

Scoring as in `DESIGN_BIG.md`: the first five distinct files named against the gold; **recall** (primary), solved, hit@1,
precision (secondary); tool use and any network lookup from the transcripts, and the results are also reported without
sessions that looked something up.

## Decisions

Paired bootstrap over tasks (10,000 resamples, seed 20261002) of the summed difference in mean recall; a claim is made only
if the 95 % interval excludes zero (a bound that is exactly zero does not exclude it):

1. `verinoda_mod` - `none`;
2. `graphify` - `none`;
3. `verinoda_mod` - `graphify`.

Secondary, reported and not decided on: `verinoda_mod` - `verinoda_setup`; the run-to-run difference of each arm (the noise
floor); each run on its own; the sessions that used a tool against `none` on the same tasks.

## Limits stated in advance

21 tasks with one to three gold files each, so recall moves in coarse steps and only a large difference can show; a small
repository, so no claim about size; one model; the gold is the files one change touched. **The agent may know these fixes:**
the issues date from 2016 to 2025 and the pull requests were merged before the model's training data ends, so the model may
recall what the fix changed, which would lift every arm alike and could hide a difference (sessions that looked the issue up
on the network are flagged; recall from memory cannot be seen). Graphify's graph is the AST one. The tasks come from the
issues that have a closing change; seL4's other issues (questions, documentation) are not represented.
