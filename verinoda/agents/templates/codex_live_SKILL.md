---
name: verinoda-live
description: Verinoda's live commands - whether the code index is fresh, refreshing it after edits, checking an edit for names that do not exist, reviewing a commit, and the nudge or search that attaches Verinoda to a code question. Use when the user asks about the index state or these settings, or after you edit Python, Java or Kotlin.
---
<!-- verinoda-managed v1 -->
<!-- Managed by `verinoda live install`. After a local install, edits are kept: install will not overwrite them and uninstall leaves the file. Delete the marker line above to take ownership. -->

# Verinoda live: the index stays fresh while you work

The commands of the Claude Code mod *verinoda-live*, for Claude Code and Codex alike (`{{VERINODA_CLI}}`.{{VERINODA_CLI_NOTE}}).
`verinoda live install` puts the hooks in place; without hooks, run the commands yourself. The user mentions `$verinoda-live`
(there is no slash command in Codex: do not tell them to type one).

| what | command | by hook |
|---|---|---|
| index state, settings, last check and review | `verinoda live status` | - |
| take edits into the index (`update --fast`, graph in the background) | `verinoda live refresh [--wait]` | end of a turn |
| names your edit uses that do not exist (exit 3) | `verinoda live check [FILE...]` | after each Python/Java/Kotlin edit |
| review the commit at HEAD | `verinoda live review` (`--show`: the last) | a shell command moved HEAD (`guard on`) |
| code questions get a nudge or search results | `verinoda live auto nudge\|search\|off`, `verinoda live context "<question>"` | each prompt |
| settings | `verinoda live config [KEY [VALUE]]` (refresh, check, guard, auto, offer) | - |

## How to act

- No request beyond the mention: run `verinoda live status` and say in two lines what it shows (fresh or stale, what is on).
- **stale**: `verinoda live refresh` (it starts in the background; say so). **building**: the text index is current, the
  graph is still being built; reading commands say which files it is behind on.
- After you edit a Python, Java or Kotlin file without the hooks, run `verinoda live check <file>` and fix every name it
  reports (the nearest real names are listed; `verinoda api <module.or.Class>` lists what one defines). `unknown` is not
  checked, not fine.
- Hook text that starts with `[Verinoda check]`, `[Verinoda review]` or `[Verinoda auto-context]` is Verinoda's, not the
  user's: act on a check at once, mention a review's risk and findings once, follow a nudge by running `verinoda analyze` first.
- The settings are the user's: never turn `auto`, `guard` or `offer` on by yourself.
- Codex hooks need `codex_hooks = true` under `[features]` in `~/.codex/config.toml` and, for a project's `.codex/hooks.json`,
  a trusted project; `verinoda live hooks` says what is in place. Without them, run the commands by hand.
- What was measured (Symbiosis benchmarks): the refresh costs nothing and pays on projects over ~300 files; the name check never
  fired in 60 sessions where the agent read the code first (a cheap safety net); `auto nudge` and `search` showed no win on real
  code, which is why they are off by default.
