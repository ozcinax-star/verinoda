# verinoda-live: a Claude Code mod for Verinoda

A function-hooks plugin ("mod") for Claude Code that keeps a Verinoda index fresh while Claude edits, helps the agent
use it, and reviews commits. It finds the project (the nearest folder at or above the session's with a `.verinoda` index; never a home
folder or a drive root unless the settings name it) and the CLI (the project's `.venv` one, else `verinoda` on
PATH) by itself; `/config` overrides both (`root`, `cli`, `python`) and turns the mascot's motion off (`motion`).

## What it does

| feature | how | default |
|---|---|---|
| Re-index after edits | Edit / Write / NotebookEdit inside `ROOT` mark the file; at the end of the main loop's turn one `verinoda update --fast` runs (subagent turns do not start one). A failed update keeps its files for `/verinoda-update`. | on |
| Graph state | After `--fast` the graph is rebuilt in the background; the status line says `text fresh · graph pending…` until no build holds the lock and no file changed since the latest snapshot, then `fresh ✓`. A graph left behind with no build coming (files changed outside Claude's edit tools) is named, and `/verinoda-update` takes it in. Edits made while the build runs wait for it and are indexed when it ends. | on |
| Auto-context (`/verinoda-auto nudge\|search\|off`) | On a code question the person types in a session started inside `ROOT`: **nudge** attaches an instruction to start with `verinoda analyze` (nothing runs before the prompt); **search** runs `verinoda query` on the prompt (filter syntax taken apart) and attaches its passages. The `auto` setting (`/config`, or `pluginConfigs` in the settings of a scripted session) is the mode a session starts in; a choice made with `/verinoda-auto` is kept and wins. The prompt of a `claude -p` / Agent SDK run counts as the person's. A nudge goes with any code question up to 20,000 characters (a pasted bug report is one); a search is only for prompts up to 2,000. | off |
| Check after edits (`/verinoda-check on\|off`) | After each Edit / Write of a Python, Java or Kotlin file inside `ROOT`, `verinoda check --diff` (falling back to the file itself without git) reads the changed lines; names on them that do not exist in the project or its environment are told to the model with the edit's result (as a PostToolUse note), with the nearest real names. The session start warms the checker (its first run in an environment builds a name index, about 90 s); a cached check takes about 3 s, never more than 60 s, and never fails the edit. The pane shows the last check (`k` toggles). | on |
| Review after commits (`/verinoda-guard on\|off`) | When a Bash or PowerShell command moved the project's `HEAD` (read before and after), `verinoda review --base HEAD~1` runs in the background and a toast gives its risk and findings; a commit made while one runs is reviewed after it. | off |
| `/verinoda-panel` pane | In Turkish (technical nouns kept): the index state as glyph + words + colour with the one action that applies (`u`), the last commit review with its risk band and a 2-line summary (`d` for the rest), auto-context as a fixed three-way control (`1`-`3`) and commit review as a two-way control (`r`), and a purple Claude mascot with glasses in the room left over, animated like Claude Code's own (it blinks and shuffles its feet; while Verinoda works it reads, a glint sweeping its lenses; `hooks/mascot.tsx`, drawn by the surface as a `Client`, so frames repaint only its region; still on surfaces without `Client`) (hidden inline, below 24 columns or when the content needs the rows). | - |

## Nudge, search or off: what was measured

Two pre-registered studies with a model in the loop:

- on 57 questions over corpora of 37-226 files (`benchmarks/results/agent-compare-2026-10-02/`): offered Verinoda,
  the agent called it in 6 of 57 sessions; told to start with `analyze` (what **nudge** says), it found the same
  facts as searching by hand at 16 % fewer input tokens, 22 % fewer output tokens and 29 % fewer tool calls;
  handed `query` results up front (what **search** does), it found 5-6 facts fewer: ranked leads anchor it;
- on 51 questions over the ten real-world repositories (`benchmarks/results/agent-compare-realworld-2026-10-02/`):
  told to start with `analyze`, it found the same facts (109 against 107 of 113, no measurable difference) at
  **1.14x** input tokens and 0.84x output tokens: one more turn per session, the agent still reading the cited code.
  The input saving of the small corpora did not replicate.

And what freshness itself is worth (`benchmarks/results/agent-compare-stale-2026-10-02/`, 50 questions about code
that changed after the index was built): the fresh index the mod keeps and a stale one found the same facts, and no
stale index led the agent into describing removed code (it reads the files on disk). On projects under 300 files
`analyze` refreshes a stale index by itself, so there the mod adds nothing; on the larger ones a stale index cost
about a fifth more input tokens than no index, and the fresh one brought that back (significant in one of two runs).

And the check after edits (`benchmarks/results/agent-compare-guard-2026-10-02/`, 30 coding tasks, 60 real sessions):
with it and without it the agent passed every hidden test and left no name that does not exist; the check never
fired, because the agent read the code before every edit. It works (a deliberate removed name is caught and the model
told), it costs about nothing, and it is on by default as a safety net; it did not change an outcome in this study.

And on a 27,078-file repository (`benchmarks/results/agent-compare-big-2026-10-02/`, 40 real Home Assistant bug reports,
160 real sessions, twice): the agent with the mod and its nudge, with Graphify's own integration and with nothing found
the same files (recall 37.3, 37.0 and 35.8 of 40; no decision reaches a claim, and a second run shows the differences
are chance). The nudge reached every session and the agent never used Verinoda (0 of 80 sessions there, and 0 of 126 on seL4,
the C microkernel, `benchmarks/results/agent-compare-sel4-2026-10-02/`, where an index was most likely to matter: 21 real
bug reports, each session run three times, recall of 21 = 17.67 with nothing, 17.78 with the mod, no claim). The tools work
(a probe asked the agent to use them and they did). An instruction at the moment of the first `grep`, Graphify's hook, was
followed by use of its tool in a third of the seL4 sessions; the nudge, given with the prompt, in none. The mod added nothing
to Verinoda's own setup in either study.

So neither mode is a measured win on real code: **nudge** trades input tokens for output tokens at the same facts,
**search** costs facts. Both stay off until the person turns one on. What the mod adds regardless of the mode is
an index that stays fresh while Claude edits, and commit reviews.

## Developing it

The live copy Claude Code hot-reloads is the session's dev-mods folder; this folder is its versioned source. Load it
from here in a terminal with `claude --plugin-dir <this repository>/claude-mods/verinoda-live`.

- `claude plugin validate <folder>`: what the module hooks and calls, and anything the engine would refuse.
- `claude plugin test <folder>`: `hooks/register.test.ts` (re-index and graph watcher) and `hooks/features.test.ts`
  (question heuristic, prompt origins, the three modes, commit review, robustness, the pane on terminal and desktop).
- Type-check: after the engine has loaded the mod once it writes `.claude-plugin/types/` and a `tsconfig.json`;
  then `tsc -p <folder>`. Both are generated and not kept here.

The 2026-10-02 version was reviewed by three independent lenses (API, state and failure paths, Windows and real-use
edge cases) with two skeptics per finding; the 13 upheld findings are fixed and covered by tests.

Note: the pane's command is `/verinoda-panel`, not `/verinoda`: the Verinoda agent skill (`~/.claude/skills/verinoda`,
installed by `verinoda install`) owns `/verinoda`, and a skill of that name takes the slash before a plugin command.

The pane was redesigned from a user-eyes evaluation (a first-time Turkish speaker, an all-day user, a terminal and
accessibility specialist), two mascot designs and one synthesized spec; the tests mount it on the terminal and
desktop surfaces, check every state's words, the hotkeys shown, and when the mascot appears.
