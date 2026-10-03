# Putting the sibling files in front of the agent (pre-registration, 2026-10-03)

Written before any session of this study ran (a probe and a few smoke sessions on one seL4 task, below, ran first). It follows
`DESIGN_BIG.md` (Home Assistant) and `DESIGN_SEL4.md` (seL4).

## What the earlier studies left

1. **No difference, because nothing was tested.** In 286 sessions of the Verinoda arms the agent never called Verinoda,
   though the mod's nudge, the MCP server and the skill were in every one; Graphify's hook, which speaks when the agent
   starts to `grep`, was followed in 5 of 80 Home Assistant and 21 of 63 seL4 sessions.
2. **Little room on single-file tasks, all of it on multi-file ones.** The agent alone is at the ceiling on tasks with one
   gold file; 86 % (Home Assistant) and 100 % (seL4) of the recall it loses is on tasks with several. What it misses is the
   sibling of a file it found: the other file of the same integration, the header of a source, the same function in
   another architecture. The agent finishes in a median of 3 tool calls.
3. **Which signal would have listed the missed files** (`benchmarks/results/signal-oracle-2026-10-03/`, no model run):
   the git history of the named files (path-limited), same-stem partners, same-name twins, imports and includes, and
   retrieval on the report's text; five short lists together covered 15 of 28 missed files on seL4 and 12 of 15 on Home
   Assistant. Verinoda's own co-change reading (the last 1,000 commits of HEAD) covered 2 and 0.

So three conditions must hold for a tool to change anything: the tasks leave room (multi-file), the signal is right, and
it reaches the agent. This study tests the second and third together, on tasks that leave room.

## What was built

`verinoda locate` and `verinoda coupled` (core, `verinoda/locate.py`) with a per-repository daemon that keeps the graph
loaded (`verinoda/locate_daemon.py`: 3 s to load on seL4, about 30 s on Home Assistant; a request takes tens of
milliseconds to about a second). The verinoda-live mod 0.5.0 delivers their answer where the agent already looks, as
separate features:

| feature | what the agent gets | how |
|---|---|---|
| `inject` | the located files, with a code question | context attached to the prompt |
| `coupled` | the files that change together with a source file it opened (at most 6 files per session) | context attached to the `Read` result |
| `tool` | `locate` and `coupled` as tools, listed in front (not behind ToolSearch), and a paragraph in the system prompt | `tool.register`, `tool.describe` with `isDeferred: false`, `prompt.compose` |
| `gate` | the first Grep, Glob or grep/rg/find in a shell of a task is answered with the located files instead of running; it may search again | a refusal that carries the answer |

A probe found that a plugin's tool is behind ToolSearch by default (the model called ToolSearch first; with the
`tool.describe` hook it was listed in front and the agent called it directly), and that the engine accepts only a plain
string as a plugin tool's result. Three smoke sessions on seL4 showed each feature's text reaching the model; one of them
found that a choice the mod stored earlier (`/verinoda-auto nudge`) overrides the settings of a scripted session, so a
run now empties the mod's store and puts it back after it (`big_run.isolated_plugin_store`), and every transcript is
checked for the nudge (`assist.nudge`, expected 0); a session of a mod arm whose transcript shows it is a harness fault,
rerun once and reported.

## Arms

Real headless `claude -p` sessions as in `DESIGN_BIG.md`: `claude-sonnet-5-5` (the Haiku sweep below says otherwise), tools
`Bash Read Glob Grep Skill` (plus `mcp__verinoda` and `mcp__verinoda-live` where they exist), no user settings, plugins,
skills, CLAUDE.md or MCP (`--setting-sources project,local --strict-mcp-config`), the user's own `graphify` and `verinoda`
off `PATH`. The same prompt in every arm: it names the project and never an index or a tool, except in `forced`.

- `none`; `graphify` (Graphify 0.9.73's AST graph and `graphify claude install`); `verinoda_setup` (`verinoda setup . --agents
  claude` with this build of Verinoda, no mod: what a user has who installed the CLI only; its MCP server now lists
  `locate` and `coupled` behind `run_tool`).
- The mod 0.5.0 arms (all on the Verinoda setup's copy, the mod's `auto` at `off`, one `assist` setting each):
  `inject`; `coupled`; `gate`; `tool` (tool, prompt); `passive` (`inject,coupled`: what needs no decision of the
  agent); `passive_gate` (`inject,coupled,gate`); `full` (`coupled,tool,prompt`); `strict` (`coupled,tool,prompt,gate`).
- `forced` (a diagnostic, never a candidate): `assist` = `tool` and the prompt ends with "Before you search the code, call
  the tool mcp__verinoda-live__locate with the text of the bug report, then read the files it lists." It bounds what the
  tool's content is worth when it is used.

Arms that share a copy run one session at a time in it; a cap over all arms limits the sessions running at once (a
Verinoda session holds a daemon, 1.8 GB on Home Assistant). Every copy is checked with `git status` after the run. The
`locate` answer takes 0.2 to 1.3 s on seL4 and 10 to 18 s on Home Assistant (retrieval over 27,000 files; the daemon
answers `coupled` in 3 s there); the time of every session is recorded, so the cost of a feature is reported.

**After the development runs, nothing of `verinoda locate`, `coupled` or the mod changes except a bug fix, and a change
that is not one (a threshold, a weight, a text) restarts the development runs of the arms it touches.** (The
retrieval inside `locate` was made faster before any session: the term-proximity candidates are found by walking the
question's tokens instead of testing every passage against every pair, the same passages, 36 s less on Home Assistant.)

## Task sets

**Development sets** (the arms are compared, one is chosen; the results are exploratory because the signals were developed
against seL4's gold files offline, and the same sets were used in the earlier studies):

- seL4: the 21 tasks of `DESIGN_SEL4.md`, each at its own base commit, now **with the history up to that commit** (every
  copy: `git rev-list --all` is exactly the base's ancestors; no later commit is reachable); the earlier studies' copies
  had none, which is what the history signals need. `none`, the Verinoda setup and every mod arm; **3 runs** each.
- Home Assistant: the 40 tasks of `DESIGN_BIG.md`, at the pinned commit with its whole history (114,657 commits). `none`
  (2 runs), `verinoda_setup` (1 run) and the eight mod arms other than `forced` (1 run each). Three copies of the Verinoda
  index share the mod arms' sessions (the daemon of each holds the graph in memory).

**Confirmatory set**, fixed here, never run with any mod arm before, and no task of it inspected beyond what the selection
prints: `benchmarks/agent_compare/contextbench_tasks.py` over ContextBench's 1,136 instances (SWE-bench Verified, Multi,
Poly and Pro: real issues and their fixing pull requests in Python, JavaScript, TypeScript, Go, Rust, C, C++ and Java).
Rules, fixed before the first run and written in that file: the gold is the **source files the gold patch changes** and that
exist at the base commit (the dataset's own gold contexts also list code that only needs reading); 2 to 6 gold files (the
task leaves room); a statement of 150 to 4,000 characters that names no gold file by its path; a repository of at most
200 MB; at most 3 tasks per repository; seeded order (sha256 of `20261002:id`); the base commit fetchable. **73 tasks** pass,
in 33 repositories (python 17, go 10, java 9, javascript 9, rust 9, cpp 7, typescript 6, c 6; gold files: 29 tasks with 2,
10 with 3, 11 with 4, 13 with 5, 10 with 6). Each task has its own copies at its base commit with 3,000 commits of history
before it (`git fetch --depth 3000`; never a commit after the base): `none`; `graphify`; `verinoda_setup`; and `verinoda_mod`
for every mod arm. Statements are shown as the dataset has them; a model may have seen these issues and their fixes, which
lifts every arm alike.

## Scoring and decisions

As in `DESIGN_BIG.md`: the first five distinct files named against the gold; **recall** (primary), solved, hit@1,
precision; the mean of a task's runs in an arm is the unit; tool use, network lookups and what each mod feature put in
front of the model from the transcripts (`session_stats`). Sessions that looked something up on the network are reported
separately. Paired bootstrap over tasks of the summed recall difference (10,000 resamples, seed 20261002); a claim is made
only if the 95 % interval excludes zero (a bound exactly zero does not).

**Choosing the arm on the development sets** (written now, applied once, when every development session has run). Among the
eight mod arms that ran on both sets, `mod_best` is the one with the largest recall difference from `none`, summed over the
61 tasks (a task's mean over its runs; Home Assistant's `none` is the mean of its 2 runs); `mod_second` is the next one.
When the two are within 1.0 recall of each other, the one with fewer features (counted as `inject`, `coupled`, `gate`, `tool`
with its prompt: `inject`, `coupled`, `gate`, `tool` 1; `passive`, `full` 2; `passive_gate`, `strict` 3) is first. The
choice is committed (`selection.json`) before the first confirmatory session.

**Confirmatory runs**: arms `none`, `graphify`, `verinoda_setup`, `mod_best`, `mod_second`, **3 runs** each. The mod and the
CLI are frozen at the commit that holds `selection.json`.

Decisions (the second and third are secondary to the first):

1. `mod_best` - `none`;
2. `mod_best` - `graphify`;
3. `mod_best` - `verinoda_setup`.

Reported, not decided: `mod_second` - `none`; solved and hit@1 for each decision; the same on the tasks `none` does not
solve in every run; by language; the run-to-run difference of each arm (the noise floor); cost, tokens, turns and seconds
(ratios with intervals); how often each feature reached the model and the tool was called; and the share of named paths that
do not exist at the base commit (a model asked for files may invent them).

**A smaller model**: `none` and `mod_best` with `claude-haiku-4-5-20251001` on the confirmatory set, 2 runs each, the same
decision (`mod_best` - `none`), reported next to the first.

## What this does not test

Staleness of the index (the studies' sessions read an index built at the base commit); a review of the change after it is
written; answers whose files are right but whose reasons are not (only the paths' existence is checked); tasks with no
index at all; repositories over 200 MB except Home Assistant, which is a development set. The signals' ranking was fitted
to nothing, but their parameters (300 commits, 2 shared commits, 15 %, 8 files) were set by looking at seL4's missed files,
so seL4 results are the optimistic ones.

## Limits stated in advance

73 confirmatory tasks: a per-task recall difference of about +0.08 (with the 0.25 spread of the earlier studies) is what
80 % power needs, so a smaller difference may not show. One model per run; `claude` CLI version and model id are recorded in
the results. The gold is the files one change touched, so a right file the change did not touch counts as a miss. The mod's
`locate` answer is built from the index at the base commit and the repository's history up to it; no later commit exists in
a copy. Graphify's graph is the AST one. Results are published whatever they are, development sets first.
