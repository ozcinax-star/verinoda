# Three Claude Codes on a big repository (pre-registration, 2026-10-02)

Written before any task was selected and before any session ran. The earlier agent studies (`DESIGN.md`,
`DESIGN_REALWORLD.md`, `DESIGN_STALE.md`, `DESIGN_GUARD.md`) ran on repositories of 130-974 files, where an agent
alone reads its way to 95-98 % of the answers and no index added any. This study asks the same thing on a repository
that does not fit in an agent's head, with tasks nobody wrote for the study.

## Corpus

`home-assistant/core` at `92c6588` (2026-08-14): 27,078 tracked files, 9,890 Python files under `homeassistant/`
(about 1,300 integrations with parallel structure and similar names) and 8,120 under `tests/`. Every working copy is
a clone at that commit, shallow, so the later fixes are not in the repository. The repository's own agent
instructions (`CLAUDE.md`, `AGENTS.md`, `.claude/skills`) are in every copy.

## Tasks

Real bug reports, not written by a model: an issue filed before the pinned commit, closed by exactly one pull
request merged after it (`Fixes #N` in its body; found with the GitHub search `linked:issue`). The gold is the set of
non-test `homeassistant/**.py` files that pull request modified or removed, as they exist at the pinned commit.
Filters, fixed now: 1-4 gold files; at most 12 files changed in the pull request; issue text of 150-4,000 characters;
the issue text names no gold file by its repository path (so a traceback through the fixed file is excluded);
pull-request titles starting with bump / update translations / revert / merge / release skipped; at most two
tasks per integration. Candidates are ordered by sha256 of `seed:PR number` (seed 20261002) and the first 40 that pass
are the tasks. The selection (`big_tasks.py`) and its counts are committed before the first session.

A session gets the issue's title and text, and is asked which files must change to fix it (read-only):
at most five files, most important first, each with the lines or function and a sentence why, as a JSON answer
(`--json-schema`). The prompt is the same in every arm and never mentions an index or a tool.

## Arms

Real headless Claude Code sessions (`claude -p`), the same model pinned in every arm
(`claude-sonnet-5-5`), the same tools (`Bash`, `Read`, `Glob`, `Grep`, `Skill`; `Edit` and `Write` not allowed). User
settings, user plugins, user skills, the user's CLAUDE.md and MCP servers are not loaded in any arm
(`--setting-sources project,local --strict-mcp-config`; checked from the session's init record: no user skills or
plugins, and a probe asked to quote any graphify / Verinoda instruction answered NONE). The `graphify` and `verinoda`
commands of the user's install are taken off `PATH`; an arm gets only its own tool's.

- `none`: original Claude Code, no index, nothing installed.
- `graphify`: Graphify 0.9.73, `graphify update .` (the AST graph; no LLM extraction) and its own Claude Code
  integration, `graphify claude install` (a section in `CLAUDE.md` and PreToolUse hooks that point the agent at
  the graph); `GRAPHIFY_NO_AUTO_REFRESH=1`.
- `verinoda_mod`: Verinoda 0.4.0 at this branch's build, `verinoda setup . --agents claude` (the index, the project
  skill, the MCP server, loaded with `--mcp-config`), plus the **verinoda-live 0.4.1 mod** (`--plugin-dir`) with its
  `auto` setting at `nudge`: a code question carries the instruction to start with `verinoda analyze`. (Before this
  study the nudge could not fire in `claude -p`; 0.4.1 fixes that, and its first headless check is in the commit.)
- `verinoda_setup` (secondary): the same as `verinoda_mod` without the mod and without the nudge; it separates what
  the mod adds from what Verinoda's own setup does. It has its own copy of the index.

The sessions of an arm share one read-only working copy. Afterwards each copy is checked with `git status` for
changes the sessions made.

## Scoring

The files an answer names, the first five distinct ones, paths normalized (relative to the repository root, `/`),
against the gold:

- **recall** (primary): gold files named / gold files, per task;
- **solved**: every gold file named; **hit@1**: the first file named is a gold file; **precision**: gold files named /
  files named (secondary).

Cost, from each session's own report: input tokens (fresh + cache), output tokens, turns, dollars, seconds. Tool use,
from the transcript: sessions that called the arm's tool, calls to it, and any network lookup (`curl`, `wget`,
`Invoke-WebRequest`, `gh`, `github.com` in a Bash call; flagged, and the results are reported with and without them).

## Decisions

Paired bootstrap over tasks (10,000 resamples, seed 20261002) of the summed difference in recall; a claim is made only
if the 95 % interval excludes zero.

1. `verinoda_mod` - `none` (does the modded Verinoda help the original Claude Code?);
2. `graphify` - `none`;
3. `verinoda_mod` - `graphify`.

Secondary, reported and not decided on: `verinoda_mod` - `verinoda_setup`; solved / hit@1 / precision; the paired
ratios of input tokens, output tokens, turns and seconds against `none`.

## Limits stated in advance

One repository, one model, one session per cell; 40 tasks with one to four gold files each, so recall moves in coarse
steps and only a large difference can show; the gold is the files one pull request changed, and other files could fix
the same bug; the agent may know this project from training, though the pull requests are newer than its training
data; Graphify's graph is the AST one (its LLM-extracted graph would cost thousands of calls on 27,000 files);
the repository's own agent instructions are in every arm; the arms' own setups advertise their tools differently,
which is what each tool's setup does.
