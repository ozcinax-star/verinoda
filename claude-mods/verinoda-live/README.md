# verinoda-live: a Claude Code mod for Verinoda

A function-hooks plugin ("mod") for Claude Code that keeps a Verinoda index fresh while Claude edits, helps the agent
use it, and reviews commits. It finds the project (the nearest folder at or above the session's with a `.verinoda` index; never a home
folder or a drive root unless the settings name it) and the CLI (the project's `.venv` one, else `verinoda` on
PATH) by itself; `/config` overrides both (`root`, `cli`, `python`) and turns the mascot's motion off (`motion`).

## What it does

| feature | how | default |
|---|---|---|
| Choose improvements (`/verinoda-improve [what to look at]`) | A separate Turkish pane with ranked possible problems, functional improvements and matters of taste. Seven items first, the rest folded; no model item selected. Open a row, choose apply, keep or check first, add your own items and send. The pane tracks outcomes and can ask for missing reports. No index needed; interactive sessions only. `improveOffer` in `/config` adds a one-line offer on vague requests. | command only; `improveOffer` off |
| Re-index after edits | Edit / Write / NotebookEdit inside `ROOT` mark the file; at the end of the main loop's turn one `verinoda update --fast` runs (subagent turns do not start one). A failed update keeps its files for `/verinoda-update`. | on |
| Graph state | After `--fast` the graph is rebuilt in the background; the status line says `text fresh · graph pending…` until no build holds the lock and no file changed since the latest snapshot, then `fresh ✓`. A graph left behind with no build coming (files changed outside Claude's edit tools) is named, and `/verinoda-update` takes it in. Edits made while the build runs wait for it and are indexed when it ends. | on |
| Auto-context (`/verinoda-auto nudge\|search\|off`) | On a code question the person types in a session started inside `ROOT`: **nudge** attaches an instruction to start with `verinoda analyze` (nothing runs before the prompt); **search** runs `verinoda query` on the prompt (filter syntax taken apart) and attaches its passages. The `auto` setting (`/config`, or `pluginConfigs` in the settings of a scripted session) is the mode a session starts in; a choice made with `/verinoda-auto` is kept and wins, except in a scripted session (`claude -p`, the Agent SDK), which does not read what was chosen in a terminal (the store is shared by every session that loads the mod and rewritten by each at its end) and takes the settings as given. The prompt of a `claude -p` / Agent SDK run counts as the person's. A nudge goes with any code question up to 20,000 characters (a pasted bug report is one); a search is only for prompts up to 2,000. | off |
| Check after edits (`/verinoda-check on\|off`) | After each Edit / Write of a Python, Java or Kotlin file inside `ROOT`, `verinoda check --diff` (falling back to the file itself without git) reads the changed lines; names on them that do not exist in the project or its environment are told to the model with the edit's result (as a PostToolUse note), with the nearest real names. The session start warms the checker (its first run in an environment builds a name index, about 90 s); a cached check takes about 3 s, never more than 60 s, and never fails the edit. The pane shows the last check (`k` toggles). | on |
| Review after commits (`/verinoda-guard on\|off`) | When a Bash or PowerShell command moved the project's `HEAD` (read before and after), `verinoda review --base HEAD~1` runs in the background and a toast gives its risk and findings; a commit made while one runs is reviewed after it. | off |
| Answer language (`/verinoda-lang auto\|tr\|en\|<a language>`, or the pane's **Cevap dili**) | Claude answers in the selected language and offers its final options in that language; code, commands and paths stay unchanged. **auto** follows the language the person writes in. Turkish and English have pane buttons (`l` cycles auto, Turkish and English); any other language can be typed into the pane or given to the command. It applies to the person's prompts inside or outside a project. The pane's labels remain Turkish. | auto |
| Where a change belongs (`/verinoda-assist off\|inject\|tool\|full\|strict`, or a list of `inject, coupled, tool, prompt, gate`) | Ways to put what `verinoda locate` and `verinoda coupled` know (the files a change touches besides the first one an agent finds: what changes together with them in the git history, same-stem partners, same-name twins, imports) in front of the agent, each its own feature. **inject** adds the located files to a code question (the agent is not asked to call anything). **coupled** adds to the result of each source file the agent opens (at most 6 files per session) the files that change together with it. **tool** registers `locate` and `coupled` as tools, listed in front of the model (`tool.describe` with `isDeferred: false`: a plugin's tool is otherwise behind ToolSearch, which the agent studies never saw it use) and **prompt** adds a paragraph to the system prompt. **gate** answers the first search of a task (Grep, Glob, or grep, rg, find in a shell) with the located files instead of running it; the agent searches again if it still wants to. Presets: `inject`; `tool` (tool, prompt); `full` (coupled, tool, prompt); `strict` (full, gate). A daemon keeps the graph loaded (`verinoda locate --daemon`, started at session start; the commands the mod runs ask it by themselves); without one, or with a Verinoda that has none, every lookup is a command that loads the graph itself. The `assist` setting is where a session starts; a choice made with `/verinoda-assist` is kept and wins. Whether any of this raises the files an agent finds is measured in `benchmarks/agent_compare/DESIGN_ASSIST.md` (the pre-registration) and the results next to it. | off |
| `/verinoda-panel` pane | In Turkish (technical nouns kept): the index state as glyph + words + colour with the one action that applies (`u`), the last commit review with its risk band and a 2-line summary (`d` for the rest), auto-context as a fixed three-way control (`1`-`3`) and commit review as a two-way control (`r`), and a purple Claude mascot with glasses in the room left over, animated like Claude Code's own (it blinks and shuffles its feet; while Verinoda works it reads, a glint sweeping its lenses; `hooks/mascot.tsx`, drawn by the surface as a `Client`, so frames repaint only its region; still on surfaces without `Client`) (hidden inline, below 24 columns or when the content needs the rows). | - |

## Choosing improvements

Run `/verinoda-improve src/parser.ts` (or give a screen or module to look at). With no argument it looks at what the
conversation has been about. Claude is asked to inspect first, make no changes, and return a ranked list through
`improve_propose`. The command opens the pane immediately; its review prompt starts from a zero-delay `$.clock.after`
callback because this host refuses `prompt.submit` inside `command.run`, even when it is not awaited.

Open a row to see the observation, reason, proposed change, cost and evidence. `Uygula` selects it, `Kalsın` protects it
in the selection prompt, and `Önce kontrol et` requests investigation only. Pressing the same choice again clears it.
Unmarked items have no decision. A confirmed problem comes back unselected with its check note and proposed fix; it
needs another explicit choice before applying. Own items are selected when added (up to ten); mobile points to a terminal
or desktop for text entry. Tab and Enter navigate a focused pane; `u`, `k`, `c` choose an open item's action, `g` sends.

`improve_report` fills in each result and any extra changes. `Sonucu iste` asks for outcomes not reported by the end of the
selection's own turn. `Beklemeyi bırak` stops waiting for reports; it does not cancel the agent or revoke an already sent
request. A failed submission restores the choices. A bare command reopens an unfinished list; an argument starts a new
review, keeping unsent own items and the draft. `/clear` resets this session's checklist. Nothing is remembered in the
mod's store, and the new tools stay unregistered until the command is used or `improveOffer` is enabled.

The model's inspection, adherence to the selected scope and report accuracy are **not measured in a real session**.
The tests drive mocked prompts, tools and panes; they do not establish that an agent obeys the instructions. Please try
the following in a terminal loaded with `claude --plugin-dir claude-mods/verinoda-live`, and report what you see:

1. `/verinoda-improve <a file>` opens the pane, starts the review and produces a list without edits.
2. ctrl+x tab focuses it; Tab reaches the rows, Enter opens details, `u` / `k` / `c` select, arrows scroll, and `g` sends.
3. Adding an own item clears the field; typing feels responsive. Twenty items at 40 columns remain readable.
4. The model applies only the selected outcomes, reports the results and all auxiliary changes, and a selection sent
   while it is still writing is not declared unreported when that earlier turn ends.
5. With `improveOffer` on in `/config`, a vague "make this nicer" gets the one-line offer before any editing.

Still unverified: a desktop-hosted session's `isInteractive` flag; real narrow-terminal placement from a model's tool;
prompt first-line matching when other plugins rewrite prompts; native Input redraw behaviour and keystroke latency;
whether the host itself preserves state through `/clear` (the mod resets it explicitly either way). Previews, preference
memory, mutually exclusive alternatives, nested items, redirection and dependency/conflict warnings are later steps.

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

## Installing it so it loads in every session

`--plugin-dir` loads the mod for one run only. To have it load without a flag, install it from a local marketplace.

**From a clone of this repository.** The repository root holds `.claude-plugin/marketplace.json`, which points at this folder:

```
claude plugin marketplace add <path to this repository>
claude plugin install verinoda-live@verinoda --scope project   # run inside a project that has a .verinoda index
claude plugin install verinoda-live@verinoda --scope user      # or: every project, indexed or not
```

**From a copy of this folder on its own** (no repository around it). Add `.claude-plugin/marketplace.json` beside
`plugin.json` and make the plugin its own marketplace:

```json
{
  "name": "verinoda",
  "owner": { "name": "Verinoda" },
  "metadata": { "description": "Verinoda Claude Code mod." },
  "plugins": [{ "name": "verinoda-live", "source": "./", "description": "Verinoda in Claude Code." }]
}
```

then `claude plugin marketplace add <path to the folder>` and the same `install` line. Do not keep both marketplaces
registered: they share the name `verinoda`.

Then run `/plugin configure verinoda-live@verinoda` in Claude Code to set the eight `userConfig` options if needed.
`claude plugin validate <folder>` checks a manifest first.

- Prefer `--scope project` in projects that have an index. The mod looks for `.verinoda` from the folder the session starts
  in, and `$.store` is one file shared by every session that loads the mod (see `AGENTS.md`), so the fewer sessions load it
  the fewer can leak a choice into one that did not ask.
- Measured: both manifests pass `claude plugin validate`, and `marketplace add`, `install` and `plugin list` succeeded on
  Linux for both layouts (Claude Code 2.1.289). Not measured: Windows, and that the installed copy hot-reloads after an edit
  of `register.tsx`.
- Not measured: `--plugin-dir` and an installed mod of the same name in one session. It may load twice. While developing
  with `--plugin-dir`, run `claude plugin disable verinoda-live@verinoda`.
- A copy of this folder is not updated when the repository changes; copy it again.

## Developing it

The live copy Claude Code hot-reloads is the session's dev-mods folder; this folder is its versioned source. Load it
from here in a terminal with `claude --plugin-dir <this repository>/claude-mods/verinoda-live`.

- `claude plugin validate <folder>`: what the module hooks and calls, and anything the engine would refuse.
- `claude plugin test <folder>`: `hooks/register.test.ts` (re-index and graph watcher) and `hooks/features.test.ts`
  (question heuristic, prompt origins, the three modes, commit review, robustness, the pane on terminal and desktop).
  `hooks/assist.test.ts` holds the assist features (presets, the tools and their placement, inject, the note on a file
  read, the gate, the daemon and its fallback, `/verinoda-assist`); `hooks/assist.ts` is their pure part.
  `hooks/improve.test.ts` adds 38 tests of list validation, decisions, prompt provenance, deferred review submission,
  queued turns, outcomes, failed submissions, clear/reload, racing sends and panes on terminal/desktop/mobile at 30 columns.
  Together: 126 pass, 0 fail in 6 files (the original 88 still pass).
- Type-check: after the engine has loaded the mod once it writes `.claude-plugin/types/` and a `tsconfig.json`;
  then `tsc -p <folder>`. Both are generated and not kept here.

The 2026-10-02 version was reviewed by three independent lenses (API, state and failure paths, Windows and real-use
edge cases) with two skeptics per finding; the 13 upheld findings are fixed and covered by tests.

Note: the pane's command is `/verinoda-panel`, not `/verinoda`: the Verinoda agent skill (`~/.claude/skills/verinoda`,
installed by `verinoda install`) owns `/verinoda`, and a skill of that name takes the slash before a plugin command.

The pane was redesigned from a user-eyes evaluation (a first-time Turkish speaker, an all-day user, a terminal and
accessibility specialist), two mascot designs and one synthesized spec; the tests mount it on the terminal and
desktop surfaces, check every state's words, the hotkeys shown, and when the mascot appears.
