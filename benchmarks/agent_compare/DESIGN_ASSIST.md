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

## Amendments of 2026-10-04 (before any session of the confirmatory set; none rests on a development result)

The text above stays as it was written. Eleven things in it were wrong or too loose; an independent review of the code and of
this text (five lenses, each finding checked by a second reader) found most of them, and the development runs found two. The
seL4 development runs had finished and Home Assistant's were running when these were written.

1. **A choice stored by the mod reached sessions that had not asked for it.** Emptying the mod's store before a run was not
   enough: the store is shared by every session that loads the mod and each session writes back what it loaded, so the
   terminal session the study is run from restored `auto: nudge` and sessions that had loaded it carried it on (26 of the 231
   first-run seL4 sessions show the nudge). Those 26 were set aside (`dev_r1.jsonl.nudged`, kept) and run again; the mod now
   takes the settings as given in a session with nobody at the keyboard (0.5.0). A session of a mod arm whose transcript
   shows the nudge is set aside for a rerun by `drop_faults.py`, and `assist_report.py` says how many there are.
2. **The confirmatory copies hold the whole history up to the base commit, not 3,000 commits.** A repository with more
   ancestors than the depth ended as a shallow clone, and `locate` then reports "history unavailable: shallow clone" and drops
   the signal the study is about (6 of the first 26 copies; they were rebuilt). The check that no commit after the base is
   reachable is unchanged.
3. **The confirmatory set is 72 tasks, not 73.** A statement that names a gold file with backslashes (a Windows path) is a
   leak as much as one with slashes, and the rule read only slashes: `sympy__sympy-17318` named both its gold files.
   Everything else of the selection is as written: 1,136 candidates, 33 repositories (python 16, go 10, java 9, javascript 9,
   rust 9, cpp 7, typescript 6, c 6), gold files 2: 28 tasks, 3: 10, 4: 11, 5: 13, 6: 10. Ten tasks have six gold files and an
   answer names at most five, so recall cannot reach 1 on them and `solved` is zero by construction.
4. **The confirmatory prompt** is the one of `DESIGN_BIG.md` with the project sentence "the {repo} repository at {c} (a git
   checkout at the commit before the fix)", written in the run configs; the harness's default sentence names Home Assistant
   and is not used there.
5. **What `locate` is given.** `inject` and the gate pass the whole prompt (its first 4,000 characters) to `locate`, not the bug
   report alone, so the harness's own words ("repository", "files", "fix") are part of the query in every mod arm. The `tool` arms
   pass what the model chooses to give it.
6. **Decisions.** The decisions are the three named above and the Haiku comparison, each with the rule "a claim only if the 95 %
   interval excludes zero"; `score_big.py` prints every pair under `decisions`, and a claim on any other pair (for example
   `mod_second` - `none`) is exploratory. No correction is made for the several pairs.
7. **Reporting.** `assist_report.py` gives the pairs without the sessions that looked something up on the network, the recall by
   language and by number of gold files, and how often the mod reached the model; `answer_checks.py`, `delivery_funnel.py`
   and `score_big.py`'s noise floor give the rest. "The same on the tasks `none` does not solve in every run" is dropped: that
   subset is chosen on `none`'s own noise, so the contrast would be biased upward.
8. **The `claude` CLI version** is recorded in every session's row (2.1.288 at the time of writing).
9. **Corrections to what is written above.** The nudge was in the mod arms' sessions; the MCP server and the skill were in all 286.
   The neighbour and pair signals exist for Python (relative imports) and the C family (includes, `.c`/`.h`) only; the other
   signals are language-free. The answer takes 0.2 to 1.3 s on seL4 and 10 to 18 s on Home Assistant (measured with other jobs running
   on the machine; the two sentences above that say otherwise are wrong; Home Assistant's own sessions ran with other jobs
   too, so their seconds are not an idle machine's). The Home Assistant development copies contain the objects of commits after the pinned one, copied
   from a clone that had them, but no ref reaches them (`git rev-list --all` equals the 114,657 ancestors of HEAD); an agent
   would have to run `git fsck` to see one. The signals' parameters were set looking at Home Assistant's missed files as well
   as seL4's, so both development sets are optimistic ones.
10. **`choose_arm.py`** counts only the tasks that `none` and every candidate arm have a session for, so an arm with a failed
    session is not helped by having fewer tasks; the tie rule is as written (the best two).
11. **The development sets are weak ground to choose on.** 45 of their 61 tasks have one gold file, where nothing can differ
    (the agent alone is at the ceiling), and in seL4 only 7 of the 21 tasks differ between any two arms: the choice rests on a
    handful of tasks. The confirmatory set, where every task has two to six gold files, is where a claim can be made.

12. **Home Assistant's first development sessions did not get the treatment, and were rerun.** The mod asked the daemon through
    the host's own HTTP call, whose time limit ended Home Assistant's answers (10 to 100 s; the daemon's log is full of reset
    connections): `inject` reached the model in 2 of 19 sessions and the gate in 1 of 13, where on seL4 (answers of 0.2 to 1.3 s)
    they reached 63 of 63. The 129 mod-arm sessions of that run are kept apart (`ha/dev_r1.jsonl.undelivered`) and the mod arms
    were run again with the fix below; `none` and `verinoda_setup` are unaffected and were kept. The arm choice reads the
    rerun only. The seL4 development runs are not affected.

13. **One confirmatory task's Verinoda copies were not finished when run 1 started, and its sessions were run after them
    (2026-10-04, during the confirmatory runs).** `fasterxml__jackson-databind-3666`, whose Verinoda setup takes over half an
    hour, was still being built when run 1 started (the build had been ended once by a 28-minute limit I had put around the
    command, and was started again at 04:30 without it). Run 1 reached the task, the fourth of every arm's queue, before the
    build ended: its `none` and `graphify` sessions ran on their finished copies; its `inject` and `passive_gate` sessions ran
    on the unfinished `verinoda_mod` copy and got no treatment (`assist.inject` 0), and its `verinoda_setup` session did not
    run (no copy). The two sessions are kept apart (`conf_r1.jsonl.unfinished-copy`); runs 2, 3 and the Haiku runs were held
    until the copies were done (the later run configs were put aside, and `run_confirm2.ps1` runs them), and run 1's three
    missing sessions were run after the build with the same config. Every other task's build was finished (a `done.json`) before
    run 1 started; no other session is affected.

14. **Sessions the network took from the model were run again (2026-10-04, after run 3).** Four sessions of run 3 (two `inject`,
    two `passive_gate`, in four different tasks, all within a few minutes of each other) ended on their first turn with "API
    Error: Can't reach the API server - check your internet or DNS (ENOTFOUND)": no answer, no cost, no turn of the agent. The
    machine's connection was down; the arms had nothing to do with it. Left in, each counts as recall 0 and pulls its arm's
    mean down (about 0.2 of a task each, against effects of about one), so they are set aside (`conf_r3.jsonl.api-error`, kept)
    and were run again, once, with the same config; `drop_faults.py` now does this for any arm (an error result with no answer,
    no cost and at most one turn) as it did for the nudge, and the rule is applied to every results file of the study, the
    Haiku runs included. The rule was written before the rerun's outcome was known; the decisions below are reported with
    the rerun, and the numbers before it are in the results folder's README.

    The Haiku runs had 18 of the same (6 in run 1, 12 in run 2, in both arms, in two clusters of a few minutes): set aside
    (`haiku_r1.jsonl.api-error`, `haiku_r2.jsonl.api-error`) and run again by `run_confirm3.ps1`. Haiku also did what the prompt
    forbids, writing a test file into the repository in three sessions (`test_issue.py`, `test_mod_bug.py`, `test_bug.py`; no
    Sonnet session changed a file), and two of the files were still in a `none` copy when run 2's session of the same task ran
    (pylint-4604, sympy-22080: the transcripts mention them), so those two sessions are set aside as well
    (`haiku_r2.jsonl.saw-earlier-files`) and run again on copies with the files removed. What stays as a result is a session
    that ended without an answer for another reason: three `none_haiku` sessions of run 2 ended after 15 to 35 turns with no
    structured answer.

Bug fixes made after the development runs began and before `selection.json` (none changes what an answer lists): the
daemon's `status` checks that the server that answers is this repository's daemon (the `repo` and `pid` it reports) and
never takes a host from the state file, `stop` checks the answer to its request, two `start`s at once leave one daemon, a
slow answer does not delete the state file of a live daemon, and a request in flight is not ended by the idle timeout; the
git deadline of `locate` ends the whole process tree on Windows; the mod applies its project test to the tools, the
system-prompt line and the gate as to the prompt, forgets the files read when a new task starts, spends its note budget on
source files only once each, and makes no HTTP call at all: `locate` and `coupled` ask a running daemon of the repository
themselves (a command's limit is ten minutes; `--no-daemon` computes in the command) and the mod only runs them. One known fault is left as it is: `locate` reads the history of
all anchors in one walk limited to 300 commits per anchor in all, so a quiet anchor next to a busy one may get fewer
commits than its own 300; the answer says "history read" all the same. It is the same in every arm and every set.
