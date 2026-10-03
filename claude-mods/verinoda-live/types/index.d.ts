// What the verinoda-live pane draws from, held by the host for the session (the two settings are also kept in the
// mod's store across sessions).

// off: nothing; nudge: ask the agent to start with Verinoda's analyze (measured: same facts, lower cost);
// search: attach Verinoda's query results to the prompt (measured: lower cost but a few facts fewer).
export type AutoMode = 'off' | 'nudge' | 'search'

// The index as the pane shows it; every kind has a glyph and words, colour only reinforces them.
export type IndexKind =
  | 'checking' // the session-start check is running
  | 'fresh' // nothing waiting, no build running
  | 'pending' // edits waiting for the end of the turn (or for a running graph build: `waiting`)
  | 'updating' // `verinoda update --fast` is running
  | 'graph' // the text is in; the graph is rebuilt in the background (2-4 min on this repository)
  | 'behind' // the graph is behind with no build coming (files changed outside Claude's edit tools)
  | 'slow' // the watcher gave up after 10 minutes
  | 'failed' // the last update failed; its files are kept
  | 'unknown' // the graph state could not be read
  | 'noindex' // no .verinoda in the project

export type IndexState = {
  kind: IndexKind
  n: number // files the state is about (pending, updating, graph, behind, failed)
  waiting: boolean // pending while a graph build runs: they go in when it ends
  since: number | null // ms: when the graph build started (graph)
  at: number | null // ms: when the index was last seen fresh in this session
  error: string // failed: the first line of the CLI's error
}

export type Notice = { text: string; tone: 'ok' | 'warn' | 'err' }

export type ContextInfo = {
  prompt: string // the prompt's head, for the pane
  mode: AutoMode
  ok: boolean
  chars: number // attached characters (0 when nothing was attached)
  seconds: number
  note: string // why nothing was attached, or the empty string
  at: number // ms
}

export type ReviewInfo = {
  ok: boolean
  risk: string // "52/100 (high)" or the empty string, for the toast
  score: number | null
  of: number
  band: string // low | medium | high, as the review names it, or the empty string
  findings: number
  summary: string // the review's own one-paragraph summary, cut at 600 characters
  seconds: number
  sha: string // the reviewed commit, short
  at: number // ms
}

// The last check of an edit (`verinoda check`): what the model was told, for the pane.
export type CheckInfo = {
  file: string // the edited file, relative to the project
  n: number // names reported absent or mismatched on the edited lines
  ok: boolean // false: the check could not run (the edit went through all the same)
  at: number // ms
}

declare module 'claude-code' {
  // The tools the mod registers (feature `tool`), so a `tool.call` hook can name them and read their arguments.
  interface McpToolInputs {
    'mcp__verinoda-live__locate': { text: string; files?: string[] }
    'mcp__verinoda-live__coupled': { files: string[] }
  }

  interface PluginState {
    'verinoda-live': {
      status: string
      idx: IndexState
      notice: Notice | null
      auto: AutoMode
      guard: boolean
      check: boolean // check every edit of a Python, Java or Kotlin file (on unless turned off)
      lastCheck: CheckInfo | null
      lastContext: ContextInfo | null
      lastReview: ReviewInfo | null
      reviewing: string | null // the short sha of the commit under review
      expanded: boolean // the review summary shown whole
      assist: string // the assist setting: a preset (off, inject, tool, full, strict) or a list of features
    }
  }
}
