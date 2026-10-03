import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { AutoMode, CheckInfo, ContextInfo, IndexKind, IndexState, Notice, ReviewInfo } from '../types'
import {
  ASSIST_USAGE, assistFeatures, assistPrompt, COUPLED_TOOL, coupledNote, isBlockedSearch, isSourceFile, LOCATE_TOOL, parseAssist,
  renderLocate, TOOL_SPECS,
} from './assist'
import type { Features, Located } from './assist'
import { frameRows, MASCOT_HEIGHT, MASCOT_WIDTH } from './mascot'
import type { MascotProps, Mood } from './mascot'

const PANE = 'verinoda'
const GRAPH_POLL_MS = 1_000
const GRAPH_WAIT_MS = 10 * 60_000
// Unlocked yet behind this long: no build is coming (files changed outside Claude's edit tools).
const GRAPH_STALL_MS = 15_000
// Auto-context runs `query` (median 2.2 s, at most 4.5 s on the agent study's corpora), not `analyze` (up to 55 s
// on this repository): it runs before the prompt is sent, so it must be quick.
const CONTEXT_TIMEOUT_MS = 15_000
const CONTEXT_MAX_CHARS = 12_000
const REVIEW_TIMEOUT_MS = 3 * 60_000
// The host's longest wait for a process (10 min). `verinoda update` itself waits up to 600 s for the build lock, so
// the mod never starts one while its own background build holds it (reindex defers to the watcher instead).
const UPDATE_TIMEOUT_MS = 600_000
const NOTICE_MS = 6_000

// `update --fast` takes the text in at once and rebuilds the graph in a background `verinoda update`.
// The graph has caught up when no build holds the lock and no file changed since the latest snapshot
// (the background build records it); the lock alone misses a build that has not taken it yet.
// Run with the repo as cwd, `-c` puts the cwd ('') first on sys.path, which would import the repo's own verinoda/
// when the repo is Verinoda's source; dropped first (`-P` would need Python 3.11, Verinoda supports 3.10).
const GRAPH_STATE =
  'import sys; sys.path[:] = [p for p in sys.path if p]; ' +
  'import json; from pathlib import Path; from verinoda import buildlock, freshness; ' +
  "r = Path(sys.argv[1]); print(json.dumps({'locked': buildlock.is_locked(r), " +
  "'behind': freshness.check(r).get('count', 0)}))"
const EDIT_TOOLS = ['Edit', 'Write', 'NotebookEdit'] as const
const SHELL_TOOLS = ['Bash', 'PowerShell'] as const

// After each edit of a file `verinoda check` reads (Python, Java, Kotlin), the names the edited lines use that do not
// exist in the project or its environment are told to the model with the edit's result. A cached check takes about
// 3 s; the first one in an environment builds a name index (about 90 s on sqlmodel), so the session start warms it
// on a snippet that is not in the project. The edit never waits longer than CHECK_TIMEOUT_MS for it.
const CHECKABLE = /\.(py|pyi|java|kt|kts)$/i
const CHECK_TIMEOUT_MS = 60_000
const WARM_TIMEOUT_MS = 300_000
const CHECK_NOTE_MAX = 8
const WARM_SNIPPET = 'import os\n\n\ndef _verinoda_warm(x):\n    return os.path.join(x.name, "a")\n'

// A prompt worth a code search: a question by its mark, its first word or a Turkish question particle. Boundaries
// are spelled out because \b is ASCII-only and Turkish words end in letters it does not know.
const END = String.raw`(?=[\s?.,!:;]|$)`
const QUESTION_START = new RegExp(
  String.raw`^(how|why|where|what|which|who|when|does|do|is|are|can|could|should|explain|show|find|list|trace|` +
    String.raw`nasıl|neden|niye|niçin|nerede|nereden|nereye|ne|neler|hangi|kim|kimler|açıkla|göster|bul|listele)` + END,
  'i',
)
const QUESTION_PARTICLE = new RegExp(String.raw`\s(mi|mı|mu|mü|misin|mısın|mudur|müdür)` + END, 'i')
// Turkish puts its question word anywhere ("Kor Ocağı her tickte ne yapıyor"); so do English how/why/where/which.
const QUESTION_ANYWHERE = new RegExp(
  String.raw`(?:^|[\s"'(])(nasıl|neden|niye|niçin|nerede|nereden|nereye|ne|neler|hangi|hangisi|kim|kimler|how|why|where|which)` + END,
  'i',
)
// A shell command that may move HEAD; whether it did is read from HEAD itself, before and after.
const GIT_WORD = /\bgit\b/

// The assist features (./assist.ts) ask `verinoda locate` and `verinoda coupled`. A daemon, when the CLI has one, keeps
// the graph loaded (loading it takes 3 s on seL4 and about 30 s on Home Assistant); without one every lookup is a CLI run.
const LOCATE_TIMEOUT_MS = 90_000
const DAEMON_START_TIMEOUT_MS = 120_000
const LOCATE_TEXT_MAX = 4_000 // of a report, what the lookup is given: a command line has a limit
const LOCATE_ANSWER_MAX = 1_800
const ANCHORS_MAX = 5
const COUPLED_BUDGET = 6 // notes about a file's partners, per session: each is a lookup and a few hundred tokens
const TASK_MIN_CHARS = 40 // a prompt shorter than this is not a task to locate
const ASSIST_TOOL = (name: string) => `mcp__verinoda-live__${name}`

const CHECKING: IndexState = { kind: 'checking', n: 0, waiting: false, since: null, at: null, error: '' }

// Session state the drawing reads (declared in ../types); settings are also kept in $.store across sessions.
const status = atom({ plugin: 'verinoda-live', key: 'status' } as const, '')
const idx = atom({ plugin: 'verinoda-live', key: 'idx' } as const, CHECKING)
const notice = atom({ plugin: 'verinoda-live', key: 'notice' } as const, null as Notice | null)
const auto = atom({ plugin: 'verinoda-live', key: 'auto' } as const, 'off' as AutoMode)
const guard = atom({ plugin: 'verinoda-live', key: 'guard' } as const, false)
const check = atom({ plugin: 'verinoda-live', key: 'check' } as const, true)
const lastCheck = atom({ plugin: 'verinoda-live', key: 'lastCheck' } as const, null as CheckInfo | null)
const lastContext = atom({ plugin: 'verinoda-live', key: 'lastContext' } as const, null as ContextInfo | null)
const lastReview = atom({ plugin: 'verinoda-live', key: 'lastReview' } as const, null as ReviewInfo | null)
const reviewing = atom({ plugin: 'verinoda-live', key: 'reviewing' } as const, null as string | null)
const expanded = atom({ plugin: 'verinoda-live', key: 'expanded' } as const, false)
const assist = atom({ plugin: 'verinoda-live', key: 'assist' } as const, 'off')

// Where Verinoda is, settled at session start from the plugin's settings (`userConfig`), else found: the project is
// the nearest folder at or above the session's with a .verinoda index; the CLI is the project's own .venv one if it
// has one, else `verinoda` on PATH; the Python beside that CLI reads the graph's state.
const cfg = { cli: 'verinoda', python: 'python', motion: true, auto: 'off' as AutoMode, assist: 'off' }

// Module state: a reload starts it over, which only forgets edits not yet indexed.
const live = {
  root: undefined as string | undefined,
  cwd: '',
  pending: new Set<string>(),
  isChecking: true, // the session-start look at the graph has not answered yet
  isRunning: false,
  runningCount: 0,
  isGraphBehind: false,
  graphSince: null as number | null,
  graphFiles: 0,
  stalledBehind: 0, // files the graph is behind on with no build coming; /verinoda-update takes them in
  isSlow: false,
  isUnknown: false,
  failed: null as { n: number; error: string } | null,
  freshAt: null as number | null,
  isReviewing: false,
  reviewAgain: false, // a commit landed while a review ran: review once more when it ends
  task: '', // the person's latest prompt long enough to be a task: what the gate and the tools locate from
  reads: [] as string[], // the project's files the model has opened, relative, in order
  coupledAsked: new Set<string>(),
  usedLocate: false, // the model called the locate tool for this task
  gated: false, // the gate has answered the first search of this task
  daemon: undefined as { url: string; token: string } | undefined,
  daemonStart: undefined as Promise<void> | undefined,
}

export function norm(p: string): string {
  return p.replace(/\\/g, '/').replace(/\/+$/, '').toLowerCase()
}

// The person's own words: typed at the composer, sent through the Remote Control bridge, or submitted by a plugin
// as the person's (`asUser`); never a notification, a peer, a schedule or a plugin speaking for itself.
export function isPersonsPrompt(origin: { kind: string; asUser?: true } | undefined): boolean {
  if (origin === undefined) return false
  // `sdk` is `claude -p` and the Agent SDK: the script's own prompt is the person's, there is no one else it could be
  return origin.kind === 'composer' || origin.kind === 'bridge' || origin.kind === 'sdk' ||
    (origin.kind === 'plugin' && origin.asUser === true)
}

// `max` is what a prompt may be to count: a search takes the prompt as its query, so it stays short (2,000); a nudge
// searches nothing, and a bug report pasted into the question is still a question about where the code is.
export const NUDGE_MAX_CHARS = 20_000
export function looksLikeCodeQuestion(text: string, max = 2_000): boolean {
  const t = text.trim()
  if (t.length < 12 || t.length > max || /^[/!#]/.test(t)) return false
  return t.includes('?') || QUESTION_START.test(t) || QUESTION_PARTICLE.test(t) || QUESTION_ANYWHERE.test(t)
}

// The nudge is round 2's `verinoda_first` instruction of the agent comparison (benchmarks/agent_compare): with it the
// agent found the same facts as searching by hand at 16 % fewer input and 29 % fewer tool calls. Nothing runs first.
export function nudgeBlock(root: string, verinoda: string): string {
  // the path has no spaces, so it runs unquoted in bash and PowerShell alike
  return (
    `[Verinoda auto-context] This project has a Verinoda code index (${root}). For this question, start by running ` +
    `Verinoda's analyze on it before any other search: ${verinoda} analyze "<the question>" --repo ${root} ` +
    `(and ${verinoda} query "<names it surfaces>" --repo ${root}); then verify and complete with your own reading ` +
    'as needed. Use this CLI with --repo: a Verinoda MCP server, if one is connected, may index another folder. ' +
    'Its claims carry file:line evidence and a status; never state an inference or unknown as fact.'
  )
}

export function isCheckable(path: string): boolean {
  return CHECKABLE.test(path)
}

type CheckSite = { at?: string; path?: string; expr?: string; name?: string; verdict?: string; message?: string;
  nearest?: { name?: string }[] }

// A file under the project, relative to it, slashes forward and case kept (it is shown and passed on as spelled).
function relPath(root: string, file: string): string {
  const r = root.replace(/\\/g, '/').replace(/\/+$/, '')
  return file.replace(/\\/g, '/').slice(r.length + 1)
}

function badSites(report: { sites?: CheckSite[] }, rel: string): CheckSite[] {
  return (report.sites ?? []).filter(s =>
    (s.verdict === 'absent' || s.verdict === 'mismatch') && norm(s.path ?? '') === norm(rel))
}

// The note the model reads after an edit: the edited file's sites `verinoda check` found absent or mismatched (an
// unknown is not checked, so it is never reported), with the nearest real names; undefined when there are none.
export function checkNote(root: string, file: string, report: { sites?: CheckSite[] }, cli: string): string | undefined {
  const rel = relPath(root, file)
  const bad = badSites(report, rel)
  if (bad.length === 0) return undefined
  const lines = bad.slice(0, CHECK_NOTE_MAX).map(s => {
    const near = (s.nearest ?? []).map(n => n.name).filter(Boolean).slice(0, 3)
    return `- ${s.at ?? rel} ${s.expr ?? s.name ?? ''}: ${s.message ?? s.verdict}${near.length ? `; nearest: ${near.join(', ')}` : ''}`
  })
  const more = bad.length > CHECK_NOTE_MAX ? [`(${bad.length - CHECK_NOTE_MAX} more: ${cli} check --diff --repo ${root})`] : []
  return [
    `[Verinoda check] The lines you just changed in ${rel} use ` +
      `${bad.length === 1 ? 'a name that does' : `${bad.length} names that do`} not exist in this project or its ` +
      'environment (a static check; runtime-made names are never reported):',
    ...lines,
    ...more,
    'Fix them before relying on this code; the nearest real names are listed, and ' +
      `${cli} api <module or class> --repo ${root} lists what one defines.`,
  ].join('\n')
}

// `verinoda query` reads filters in its question (path: file: lang: symbol: is:, /regex/, AND OR NOT); a prompt is
// plain text, so those spellings are taken apart before it is passed on.
export function plainQuery(prompt: string): string {
  return prompt
    .replace(/(^|[\s(])(-?)(path|file|lang|language|symbol|is):(?=\S)/gi, '$1$2$3 ')
    .replace(/(^|\s)\/([^/\s][^/]*)\/(?=\s|$)/g, '$1$2')
    .replace(/(^|\s)(AND|OR|NOT)(?=\s|$)/g, (_, pre: string, op: string) => pre + op.toLowerCase())
    .replace(/[()]/g, ' ')
}

export function contextBlock(root: string, retrieved: string): string {
  const body = retrieved.length > CONTEXT_MAX_CHARS ? `${retrieved.slice(0, CONTEXT_MAX_CHARS)}\n[cut]` : retrieved
  return (
    `[Verinoda auto-context] Ranked code passages Verinoda retrieved for this prompt from its index of ${root} ` +
    '(the index can be behind the working tree). They are leads with file:line locations, not verified answers: ' +
    'use what helps, check what you rely on.\n\n' +
    body
  )
}

// Work left running after a hook returns (a re-index, the graph watcher, a review): a reload or the end of the
// session aborts its $ calls, which is no error of the person's, so the rejection is dropped here.
function background(work: Promise<unknown>): void {
  work.catch(() => undefined)
}

function plural(n: number): string {
  return `${n} file${n === 1 ? '' : 's'}`
}

function seconds(fromMs: number, toMs: number): number {
  return Math.round((toMs - fromMs) / 100) / 10
}

function isInside(path: string, root: string): boolean {
  const p = norm(path)
  const r = norm(root)
  return p === r || p.startsWith(`${r}/`)
}

// The index state from the module's fields, as the pane reads it and as the status line says it (in English, as the
// slash commands do). The first matching rule wins: a running update over a failure, a failure over waiting edits.
export function indexState(): { state: IndexState; line: string } {
  const base = { waiting: false, since: null, at: live.freshAt, error: '' }
  const at = (kind: IndexKind, n: number, line: string, extra: Partial<IndexState> = {}) =>
    ({ state: { ...base, kind, n, ...extra }, line })
  if (live.root === undefined) return at('noindex', 0, `no .verinoda index at or above ${live.cwd}`)
  if (live.isRunning) return at('updating', live.runningCount, `updating (${plural(live.runningCount)})…`)
  if (live.failed !== null) {
    return at('failed', live.failed.n, `update failed · ${plural(live.failed.n)} kept for /verinoda-update`, { error: live.failed.error })
  }
  if (live.pending.size > 0) return at('pending', live.pending.size, `${plural(live.pending.size)} pending`, { waiting: live.isGraphBehind })
  if (live.isGraphBehind) return at('graph', live.graphFiles, 'text fresh · graph pending…', { since: live.graphSince })
  if (live.stalledBehind > 0) return at('behind', live.stalledBehind, `graph behind ${plural(live.stalledBehind)} · /verinoda-update`)
  if (live.isSlow) return at('slow', 0, 'graph build still running (10 min+)')
  if (live.isUnknown) return at('unknown', 0, 'graph state unknown')
  if (live.isChecking) return at('checking', 0, 'checking…')
  return at('fresh', 0, 'fresh ✓')
}

function publish($: EngineInterface): void {
  const { state, line } = indexState()
  $.ui.status(live.root === undefined ? undefined : `Verinoda: ${line}`)
  // the pane's copy is best effort: a write refused after the module unloaded is dropped
  update($, status, () => line).catch(() => undefined)
  update($, idx, () => state).catch(() => undefined)
}

const MODES: readonly AutoMode[] = ['off', 'nudge', 'search']

export function asMode(value: unknown): AutoMode {
  if (value === true) return 'nudge' // 0.2.0-dev stored a boolean
  return MODES.includes(value as AutoMode) ? (value as AutoMode) : 'off'
}

async function setAuto($: EngineInterface, mode: AutoMode): Promise<void> {
  await update($, auto, () => mode)
  await $.store.set('auto', mode)
}

async function setGuard($: EngineInterface, value: boolean): Promise<void> {
  await update($, guard, () => value)
  await $.store.set('guard', value)
}

async function setCheck($: EngineInterface, value: boolean): Promise<void> {
  await update($, check, () => value)
  await $.store.set('check', value)
}

async function setAssist($: EngineInterface, value: string): Promise<void> {
  await update($, assist, () => value)
  await $.store.set('assist', value)
}

async function features($: EngineInterface): Promise<Features> {
  return assistFeatures(await read($, assist))
}

// Auto-context and commit review start off, the check after edits on; what the person chose last is kept in the store.
// A scripted session (`claude -p`, the Agent SDK) has nobody at the keyboard, so what was chosen there is not read: it
// takes the settings (`pluginConfigs`) as they are given. (The store is shared by every session that loads the mod and
// rewritten by each at its end: a choice made once in a terminal would otherwise reach the arms of a study that never
// asked for it, and did.)
async function loadSettings($: EngineInterface, isInteractive: boolean): Promise<void> {
  // the setting (`userConfig.auto`) is where a session starts that has never been asked; a stored choice wins
  const storedChoice = (key: string) => (isInteractive ? $.store.get(key) : Promise.resolve(undefined))
  const stored = await storedChoice('auto')
  const mode = stored === undefined ? cfg.auto : asMode(stored)
  const isGuard = (await storedChoice('guard')) === true
  const isCheck = (await storedChoice('check')) !== false
  const storedAssist = await storedChoice('assist')
  await update($, auto, () => mode)
  await update($, guard, () => isGuard)
  await update($, check, () => isCheck)
  await update($, assist, () => (typeof storedAssist === 'string' ? storedAssist : cfg.assist))
}

// `check --diff` reads the lines changed against HEAD (and new files whole); a project without git, or a diff the
// CLI cannot read, falls back to the edited file whole. Exit 3 (something absent) and 4 (something not checked) are
// answers too. Whatever happens, the edit itself has already gone through.
async function runCheck($: EngineInterface, root: string, file: string): Promise<{ sites?: CheckSite[] } | undefined> {
  for (const argv of [[cfg.cli, 'check', '--diff', '--json', '--repo', root], [cfg.cli, 'check', file, '--json', '--repo', root]]) {
    try {
      const { exitCode, stdout } = await $.process.run(argv, { cwd: root, timeoutMs: CHECK_TIMEOUT_MS })
      if (exitCode !== 0 && exitCode !== 3 && exitCode !== 4) continue
      const report = JSON.parse(stdout) as { sites?: CheckSite[] }
      if (typeof report === 'object' && report !== null) return report
    } catch {
      return undefined // a timeout or a CLI that will not start: the second command would wait as long
    }
  }
  return undefined
}

async function checkEdit($: EngineInterface, root: string, file: string): Promise<string | undefined> {
  const report = await runCheck($, root, file)
  const note = report === undefined ? undefined : checkNote(root, file, report, cfg.cli)
  const at = await $.clock.now()
  const rel = relPath(root, file)
  const n = report === undefined ? 0 : badSites(report, rel).length
  await update($, lastCheck, () => ({ file: rel, n, ok: report !== undefined, at }))
  return note
}

// The first check in an environment builds its name index; done once at session start, on code outside the project.
async function warmCheck($: EngineInterface, root: string): Promise<void> {
  try {
    await $.process.run([cfg.cli, 'check', '--stdin', '--as', '_verinoda_warm.py', '--json', '--repo', root],
      { cwd: root, stdin: WARM_SNIPPET, timeoutMs: WARM_TIMEOUT_MS })
  } catch {
    // a cold first check is slower, nothing more
  }
}

type GraphState = { locked: boolean; behind: number }

async function graphState($: EngineInterface, root: string): Promise<GraphState | undefined> {
  try {
    const { exitCode, stdout } = await $.process.run([cfg.python, '-c', GRAPH_STATE, root], { cwd: root })
    if (exitCode !== 0) return undefined
    const d = JSON.parse(stdout) as Partial<GraphState>
    return typeof d.locked === 'boolean' && typeof d.behind === 'number' ? { locked: d.locked, behind: d.behind } : undefined
  } catch {
    return undefined
  }
}

// One look at the graph (session start, or the pane's "check again"): fresh, a build to watch, or behind.
async function checkGraph($: EngineInterface): Promise<void> {
  const root = live.root
  if (root === undefined || live.isRunning || live.isGraphBehind) return
  live.isChecking = true
  live.isUnknown = false
  live.isSlow = false
  publish($)
  const state = await graphState($, root)
  live.isChecking = false
  if (state === undefined) live.isUnknown = true
  else if (state.locked) background(watchGraph($, root, state.behind))
  else if (state.behind > 0) live.stalledBehind = state.behind
  else live.freshAt = await $.clock.now()
  publish($)
}

// One watcher at a time; a later update while it runs only keeps it going.
async function watchGraph($: EngineInterface, root: string, files: number): Promise<void> {
  if (live.isGraphBehind) return
  live.isGraphBehind = true
  live.graphFiles = files
  live.stalledBehind = 0
  live.isSlow = false
  live.isUnknown = false
  const started = await $.clock.now()
  live.graphSince = started
  publish($)
  let idleSince: number | undefined // first poll that saw no build running while behind
  try {
    while ((await $.clock.now()) - started < GRAPH_WAIT_MS) {
      await $.clock.sleep(GRAPH_POLL_MS)
      if (live.isRunning) {
        idleSince = undefined
        continue
      }
      const state = await graphState($, root)
      if (state === undefined) {
        live.isUnknown = true
        return
      }
      const now = await $.clock.now()
      if (state.locked || state.behind === 0) idleSince = undefined
      else idleSince ??= now
      const isStalled = idleSince !== undefined && now - idleSince >= GRAPH_STALL_MS
      if (state.behind === 0 || isStalled) {
        live.stalledBehind = isStalled ? state.behind : 0
        if (!isStalled) live.freshAt = now
        return
      }
    }
    live.isSlow = true
  } finally {
    live.isGraphBehind = false
    live.graphSince = null
    publish($)
    if (live.pending.size > 0 && !live.isRunning) background(reindex($)) // edits made while the build ran
  }
}

async function exists($: EngineInterface, path: string): Promise<boolean> {
  return (await $.fs.stat(path).catch(() => undefined)) !== undefined
}

// The project: the folder the settings name, else the nearest one at or above the session's with a .verinoda index.
async function findRoot($: EngineInterface, named: string, cwd: string): Promise<string | undefined> {
  // slashes made forward, case kept: a case-sensitive file system finds the folder only as it is spelled
  const slashes = (p: string) => p.replace(/\\/g, '/').replace(/\/+$/, '')
  if (named !== '') return (await exists($, `${slashes(named)}/.verinoda`)) ? slashes(named) : undefined
  // a home folder or a drive root is never taken for a project unless the settings name it (Verinoda's own first
  // scan refuses them too): every edit anywhere under it would re-index it
  const homes = new Set<string>()
  const profile = await $.env.get('USERPROFILE').catch(() => undefined)
  const home = await $.env.get('HOME').catch(() => undefined)
  for (const h of [profile, home]) if (h) homes.add(norm(h))
  let dir = slashes(cwd)
  for (let i = 0; i < 40 && dir !== ''; i++) {
    const isTooWide = homes.has(norm(dir)) || /^[A-Za-z]:$/.test(dir) || dir === ''
    if (!isTooWide && (await exists($, `${dir}/.verinoda`))) return dir
    const up = dir.replace(/\/[^/]*$/, '')
    if (up === dir || /^[A-Za-z]:$/.test(dir)) break
    dir = up
  }
  return undefined
}

// The CLI and its Python: the settings' when given; else the project's .venv; else what PATH finds.
async function resolveTools($: EngineInterface, root: string, options: Record<string, unknown>): Promise<void> {
  const given = (k: string) => (typeof options[k] === 'string' ? (options[k] as string).trim() : '')
  const venv = [`${root}/.venv/Scripts/verinoda.exe`, `${root}/.venv/bin/verinoda`]
  let cli = given('cli')
  if (cli === '') {
    for (const c of venv) if (await exists($, c)) { cli = c; break }
  }
  cfg.cli = cli || 'verinoda'
  let python = given('python')
  if (python === '' && /[/\\]/.test(cfg.cli)) {
    const dir = cfg.cli.replace(/\\/g, '/').replace(/\/[^/]*$/, '')
    for (const c of [`${dir}/python.exe`, `${dir}/python`]) if (await exists($, c)) { python = c; break }
  }
  cfg.python = python || 'python'
  cfg.motion = options.motion !== false
}

// What a re-index came to: `text` is the command's answer (English, as before); `code` lets the pane say it.
type Outcome = {
  code: 'noindex' | 'running' | 'graph-fresh' | 'fresh' | 'waiting' | 'updated' | 'failed'
  n: number
  text: string
}

async function reindex($: EngineInterface): Promise<Outcome> {
  const { root, pending } = live
  if (root === undefined) return { code: 'noindex', n: 0, text: `no .verinoda index at or above ${live.cwd}` }
  if (live.isRunning) return { code: 'running', n: live.runningCount, text: 'an update is already running' }
  if (pending.size === 0 && live.isGraphBehind) {
    return { code: 'graph-fresh', n: 0, text: 'text index fresh; the graph is still being rebuilt' }
  }
  if (pending.size === 0 && live.stalledBehind === 0 && live.failed === null) return { code: 'fresh', n: 0, text: 'index already fresh' }
  if (live.isGraphBehind) {
    // the background build holds the lock; `update` would wait for it, so the files wait instead
    publish($)
    return { code: 'waiting', n: pending.size, text: `waiting for the graph build; ${plural(pending.size)} will be indexed when it ends` }
  }

  live.isRunning = true
  const files = [...pending]
  const stalled = live.stalledBehind
  const count = files.length || stalled || live.failed?.n || 0
  live.runningCount = count
  pending.clear()
  live.stalledBehind = 0
  live.failed = null
  live.isSlow = false
  live.isUnknown = false
  const fail = (error: string) => {
    files.forEach(f => live.pending.add(f))
    live.stalledBehind = Math.max(live.stalledBehind, stalled)
    live.failed = { n: count, error }
  }
  publish($)
  try {
    const { exitCode, stderr } = await $.process.run(
      [cfg.cli, 'update', '--fast', '--repo', root],
      { cwd: root, timeoutMs: UPDATE_TIMEOUT_MS },
    )
    if (exitCode !== 0) {
      fail(`çıkış ${exitCode}: ${stderr.trim().split('\n').pop()?.slice(0, 160) ?? ''}`)
      return { code: 'failed', n: count, text: `update failed (exit ${exitCode}): ${stderr.slice(0, 300)}` }
    }
    live.isRunning = false
    background(watchGraph($, root, count))
    return { code: 'updated', n: count, text: `text index updated (${plural(count)}); the graph is rebuilt in the background` }
  } catch (err) {
    fail(String(err).slice(0, 160))
    return { code: 'failed', n: count, text: `update failed: ${String(err)}` }
  } finally {
    live.isRunning = false
    publish($)
  }
}

const NOTICE_TEXT: Record<Outcome['code'], (n: number) => Notice> = {
  noindex: () => ({ text: 'Bu projede index yok', tone: 'err' }),
  running: n => ({ text: `Zaten güncelleniyor (${n} dosya)`, tone: 'ok' }),
  'graph-fresh': () => ({ text: 'Metin güncel · graph hâlâ kuruluyor', tone: 'ok' }),
  fresh: () => ({ text: 'Index zaten güncel', tone: 'ok' }),
  waiting: n => ({ text: `Graph bitince ${n} dosya eklenecek`, tone: 'ok' }),
  updated: n => ({ text: `Güncellendi (${n} dosya) · graph arka planda kuruluyor`, tone: 'ok' }),
  failed: () => ({ text: 'Güncelleme başarısız · ayrıntı yukarıda', tone: 'err' }),
}

// The pane's update button: run it, and say what happened under the button for a few seconds.
async function updateFromPane($: EngineInterface): Promise<void> {
  const outcome = await reindex($)
  const said = NOTICE_TEXT[outcome.code](outcome.n)
  await update($, notice, () => said)
  await $.clock.sleep(NOTICE_MS)
  await update($, notice, n => (n === said ? null : n))
}

async function recheckFromPane($: EngineInterface): Promise<void> {
  await update($, notice, () => ({ text: 'Kontrol ediliyor…', tone: 'ok' }))
  await checkGraph($)
  await update($, notice, () => null)
}

// Verinoda's query on the prompt, as one context block; undefined (with the reason recorded) when nothing is attached.
async function retrieve($: EngineInterface, prompt: string): Promise<string | undefined> {
  const root = live.root
  if (root === undefined) return undefined
  const t0 = await $.clock.now()
  const record = (info: Pick<ContextInfo, 'ok' | 'chars' | 'note'>, t1: number) =>
    update($, lastContext, () => ({ prompt: prompt.trim().slice(0, 80), seconds: seconds(t0, t1), mode: 'search' as const, at: t1, ...info }))
  try {
    const { exitCode, stdout, stderr } = await $.process.run(
      [cfg.cli, 'query', plainQuery(prompt), '--repo', root, '--max-chars', '6000'],
      { cwd: root, timeoutMs: CONTEXT_TIMEOUT_MS },
    )
    const t1 = await $.clock.now()
    if (exitCode !== 0 || stdout.trim() === '') {
      await record({ ok: false, chars: 0, note: exitCode !== 0 ? `query exit ${exitCode}: ${stderr.slice(0, 120)}` : 'nothing found' }, t1)
      return undefined
    }
    const block = contextBlock(root, stdout.trim())
    await record({ ok: true, chars: block.length, note: '' }, t1) // shown in the pane, not over the index state
    return block
  } catch (err) {
    await record({ ok: false, chars: 0, note: `query failed: ${String(err).slice(0, 120)}` }, await $.clock.now())
    return undefined
  }
}

// ---- the assist features: lookups, tools, notes (./assist.ts holds the pure parts) ------------------------------

// The daemon, started in the background at session start when a feature will ask for it; an old CLI that knows no
// `locate --daemon` exits non-zero, and every lookup is then a CLI run.
async function startDaemon($: EngineInterface, root: string): Promise<void> {
  try {
    const { exitCode, stdout } = await $.process.run(
      [cfg.cli, 'locate', '--daemon', 'start', '--repo', root, '--json'],
      { cwd: root, timeoutMs: DAEMON_START_TIMEOUT_MS },
    )
    if (exitCode !== 0) return
    const d = JSON.parse(stdout) as { running?: boolean; url?: string; token?: string }
    if (d.running === true && typeof d.url === 'string' && typeof d.token === 'string') live.daemon = { url: d.url, token: d.token }
  } catch {
    // no daemon, nothing lost
  }
}

function wantsDaemon(f: Features): boolean {
  return f.inject || f.coupled || f.tool || f.gate
}

function ensureDaemon($: EngineInterface, f: Features): void {
  if (live.root === undefined || live.daemonStart !== undefined || !wantsDaemon(f)) return
  live.daemonStart = startDaemon($, live.root)
  background(live.daemonStart)
}

function parseLocated(text: string): Located | undefined {
  try {
    const d = JSON.parse(text) as unknown
    return typeof d === 'object' && d !== null && !Array.isArray(d) ? (d as Located) : undefined
  } catch {
    return undefined
  }
}

type Lookup = { op: 'locate'; text: string; anchors: string[] } | { op: 'coupled'; files: string[] }

// One lookup: the daemon when there is one and it answers, else the CLI; undefined when neither does.
async function lookup($: EngineInterface, root: string, q: Lookup): Promise<Located | undefined> {
  await live.daemonStart?.catch(() => undefined)
  const daemon = live.daemon
  if (daemon !== undefined) {
    try {
      const body = q.op === 'locate' ? { text: q.text, anchors: q.anchors, max_chars: LOCATE_ANSWER_MAX } : { files: q.files }
      const res = await $.http.fetch(`${daemon.url}/${q.op}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Verinoda-Token': daemon.token }, body: JSON.stringify(body),
      })
      if (res.ok) return parseLocated(res.text)
    } catch {
      // the CLI answers below
    }
    live.daemon = undefined // it does not answer: not asked again this session
  }
  const argv = q.op === 'locate'
    ? [cfg.cli, 'locate', '--repo', root, '--json', '--max-chars', String(LOCATE_ANSWER_MAX), ...q.anchors.flatMap(a => ['--anchor', a]), '--', q.text]
    : [cfg.cli, 'coupled', '--repo', root, '--json', ...q.files]
  try {
    const { exitCode, stdout } = await $.process.run(argv, { cwd: root, timeoutMs: LOCATE_TIMEOUT_MS })
    return exitCode === 0 ? parseLocated(stdout) : undefined
  } catch {
    return undefined
  }
}

async function registerAssistTools($: EngineInterface): Promise<void> {
  for (const spec of TOOL_SPECS) {
    await $.tool.register({ name: spec.name, description: spec.description, inputSchema: spec.inputSchema as unknown as Record<string, unknown> })
  }
}

// A path as the lookups spell it: relative to the project, slashes forward.
function toRel(root: string, file: string): string {
  const p = file.replace(/\\/g, '/')
  return isInside(p, root) ? relPath(root, p) : p.replace(/^\.\//, '')
}

function noteRead(rel: string): void {
  if (!live.reads.includes(rel) && live.reads.length < 50) live.reads.push(rel)
}

// A prompt that could be a task starts a new hunt: the gate may answer its first search, the tool counts as not yet used.
function startTask(text: string): void {
  const t = text.trim()
  if (t.length < TASK_MIN_CHARS || /^[/!#]/.test(t)) return
  live.task = t
  live.gated = false
  live.usedLocate = false
}

export function locateBlock(answer: string): string {
  return (
    "[Verinoda locate] Files Verinoda's index, the project's git history and sibling directories point to for this " +
    'request. They are leads, not verified answers: read them, check what you rely on, and name every file that ' +
    `needs the change.\n\n${answer}`
  )
}

export function gateText(answer: string): string {
  return (
    "[Verinoda] This search was not run. Before searching, Verinoda's index, the project's git history and sibling " +
    `directories point to these files for the task:\n\n${answer}\n\nRead them first and name every file that needs ` +
    'the change; run the search again only if you still need it.'
  )
}

// What the last commit changed, by concern: Verinoda's review of the working tree against the commit's parent.
async function reviewCommit($: EngineInterface, sha: string): Promise<void> {
  const root = live.root
  if (root === undefined) return
  if (live.isReviewing) {
    live.reviewAgain = true
    return
  }
  live.isReviewing = true
  const short = sha.slice(0, 7)
  await update($, reviewing, () => short)
  const t0 = await $.clock.now()
  const failedInfo = (summary: string, t1: number): ReviewInfo =>
    ({ ok: false, risk: '', score: null, of: 100, band: '', findings: 0, summary, seconds: seconds(t0, t1), sha: short, at: t1 })
  try {
    const { exitCode, stdout } = await $.process.run(
      [cfg.cli, 'review', '--json', '--repo', root, '--base', 'HEAD~1', '--max-chars', '4000'],
      { cwd: root, timeoutMs: REVIEW_TIMEOUT_MS },
    )
    const t1 = await $.clock.now()
    if (exitCode !== 0 && exitCode !== 3) {  // 3: findings or unknowns to report, an answer
      await update($, lastReview, () => failedInfo(`çıkış ${exitCode}`, t1))
      return
    }
    const d = JSON.parse(stdout) as {
      summary?: string
      risk?: { score?: number; of?: number; band?: string }
      counts?: { findings?: number }
    }
    const score = d.risk?.score ?? null
    const of = d.risk?.of ?? 100
    const band = d.risk?.band ?? ''
    const risk = score === null ? '' : `${score}/${of} (${band || '?'})`
    const info: ReviewInfo = {
      ok: true, risk, score, of, band, findings: d.counts?.findings ?? 0, summary: (d.summary ?? '').slice(0, 600),
      seconds: seconds(t0, t1), sha: short, at: t1,
    }
    await update($, lastReview, () => info)
    await update($, expanded, () => false)
    $.ui.toast(`Verinoda review of the commit: risk ${risk || 'n/a'}, ${info.findings} finding(s) · /verinoda-panel`)
  } catch (err) {
    await update($, lastReview, () => failedInfo(String(err).slice(0, 200), 0))
  } finally {
    live.isReviewing = false
    await update($, reviewing, () => null).catch(() => undefined)
    if (live.reviewAgain) {
      live.reviewAgain = false
      const head = await gitHead($, root)
      background(reviewCommit($, head ?? sha))
    }
  }
}

async function gitHead($: EngineInterface, root: string): Promise<string | undefined> {
  try {
    const { exitCode, stdout } = await $.process.run(['git', '-C', root, 'rev-parse', 'HEAD'], { cwd: root, timeoutMs: 10_000 })
    return exitCode === 0 ? stdout.trim() : undefined
  } catch {
    return undefined
  }
}

export function modeCommand(arg: string, current: AutoMode): AutoMode | undefined {
  const a = arg.trim().toLowerCase()
  if (a === '') return current === 'off' ? 'nudge' : 'off'
  if (a === 'on' || a === 'açık' || a === 'aç') return 'nudge'
  if (a === 'kapalı' || a === 'kapat') return 'off'
  return MODES.includes(a as AutoMode) ? (a as AutoMode) : undefined
}

export function settingCommand(arg: string, current: boolean): boolean | undefined {
  const a = arg.trim().toLowerCase()
  if (a === '') return !current
  if (a === 'on' || a === 'açık' || a === 'ac' || a === 'aç') return true
  if (a === 'off' || a === 'kapalı' || a === 'kapat') return false
  return undefined
}

// ---- the pane's words, pictures and layout (pure: tested without the engine) ----------------------------------

export function hhmm(ms: number | null): string {
  if (ms === null) return '--:--'
  const d = new Date(ms)
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

export type Tone = 'ok' | 'busy' | 'warn' | 'err' | 'idle'
export const TONE_COLOR: Record<Tone, string> = { ok: 'green', busy: 'cyan', warn: 'yellow', err: 'red', idle: 'gray' }
export type Action = 'update' | 'retry' | 'recheck'

// Every state as a glyph, words and a tone; the glyph and words carry the meaning, the colour only reinforces it.
export function describeIndex(s: IndexState): { head: string; tone: Tone; details: string[]; action: Action | null } {
  switch (s.kind) {
    case 'checking': return { head: '◌ Kontrol ediliyor…', tone: 'idle', details: [], action: null }
    case 'fresh': return { head: '✓ Index güncel', tone: 'ok', details: [s.at === null ? 'bu oturumda değişiklik yok' : `son kontrol ${hhmm(s.at)}`], action: null }
    case 'pending':
      return s.waiting
        ? { head: `● ${s.n} dosya bekliyor`, tone: 'warn', details: ['graph bitince eklenecek'], action: null }
        : { head: `● ${s.n} dosya bekliyor`, tone: 'warn', details: ['tur bitince kendiliğinden güncellenir'], action: 'update' }
    case 'updating': return { head: `◐ Güncelleniyor · ${s.n} dosya`, tone: 'busy', details: [], action: null }
    case 'graph':
      return { head: s.n > 0 ? `◐ Graph kuruluyor · ${s.n} dosya` : '◐ Graph kuruluyor', tone: 'busy',
        details: [`${hhmm(s.since)}'den beri · genelde 2-4 dk`, 'Metin araması şimdiden güncel'], action: null }
    case 'behind': return { head: `▲ Graph ${s.n} dosya geride`, tone: 'warn', details: ["Claude'un dışında değişen dosyalar"], action: 'update' }
    case 'slow': return { head: "▲ Graph kurulumu 10 dk'yı geçti", tone: 'warn', details: [], action: 'recheck' }
    case 'failed': return { head: `✗ Güncelleme başarısız · ${s.n} dosya bekletiliyor`, tone: 'err', details: [s.error], action: 'retry' }
    case 'unknown': return { head: '? Graph durumu okunamadı', tone: 'idle', details: [], action: 'recheck' }
    case 'noindex': return { head: "? Bu projede Verinoda index'i yok", tone: 'idle', details: ['Proje klasöründe çalıştır:', 'verinoda init, sonra verinoda update'], action: null }
  }
}

export const ACTION_LABEL: Record<Action, string> = { update: 'Şimdi güncelle', retry: 'Tekrar dene', recheck: 'Tekrar kontrol et' }

export function describeReview(r: ReviewInfo | null, sha: string | null): { head: string; tone: Tone; meta: string } | null {
  if (sha !== null) return { head: `◐ İnceleniyor · ${sha}…`, tone: 'busy', meta: '' }
  if (r === null) return null
  const meta = `${r.sha} · ${hhmm(r.at)}`
  if (!r.ok) return { head: `✗ İnceleme başarısız (${r.summary})`, tone: 'err', meta }
  const tail = `${r.findings} bulgu`
  if (r.score === null) return { head: `? Risk yok · ${tail}`, tone: 'idle', meta }
  const score = `${r.score}/${r.of}`
  if (r.band === 'high') return { head: `▲ Yüksek risk ${score} · ${tail}`, tone: 'err', meta }
  if (r.band === 'medium') return { head: `● Orta risk ${score} · ${tail}`, tone: 'warn', meta }
  return { head: `✓ Düşük risk ${score} · ${tail}`, tone: 'ok', meta }
}

export const MODE_LABEL: Record<AutoMode, string> = { off: 'Kapalı', nudge: 'Yönlendir', search: 'Arama' }
export const MODE_HINT: Record<AutoMode, string> = {
  off: 'Sorulara bağlam eklenmez',
  nudge: 'Önerilen · Claude önce analyze çalıştırır',
  search: 'Arama sonuçları eklenir · ölçümde daha zayıf',
}
export const OUTSIDE_HINT = '▲ Oturum proje dışında başladı; bağlam eklenmez'
export const GUARD_HINT = 'Commit sonrası risk ve bulgular bildirilir'
export const CHECK_HINT = "Python/Java/Kotlin düzenlemesindeki var olmayan isimler Claude'a söylenir"

export function checkLine(c: CheckInfo | null): { text: string; tone: Tone } | null {
  if (c === null) return null
  if (!c.ok) return { text: `Son kontrol çalışmadı: ${c.file} (düzenleme yapıldı)`, tone: 'warn' }
  if (c.n === 0) return { text: `Son kontrol: ${c.file} · temiz`, tone: 'idle' }
  return { text: `Son kontrol: ${c.file} · ${c.n} isim bulunamadı, Claude'a söylendi`, tone: 'warn' }
}

export function contextLine(c: ContextInfo | null): { text: string; tone: Tone } | null {
  if (c === null) return null
  const head = `"${c.prompt.slice(0, 40)}${c.prompt.length > 40 ? '…' : ''}"`
  if (c.mode === 'nudge') return { text: `Son ${hhmm(c.at)} · yönlendirildi · ${head}`, tone: 'idle' }
  if (c.ok) return { text: `Son ${hhmm(c.at)} · ${c.chars.toLocaleString('tr')} karakter eklendi (${c.seconds.toLocaleString('tr')} sn) · ${head}`, tone: 'idle' }
  return { text: `Son ${hhmm(c.at)} · eklenmedi: ${c.note === 'nothing found' ? 'sonuç yok' : 'arama hatası'} · ${head}`, tone: 'warn' }
}

// The mascot (./mascot.tsx) is drawn by the surface as a `Client`, animated there; a surface without `Client`
// (VS Code, mobile) gets its first frame, still.
const ANIMATED_SURFACES = ['terminal', 'desktop']
const MOOD: Record<Tone, Mood> = { ok: 'idle', idle: 'idle', busy: 'busy', warn: 'warn', err: 'err' }

export function mascotCaption(s: IndexState, r: ReviewInfo | null, sha: string | null): { text: string; tone: Tone } {
  if (s.kind === 'failed' || (r !== null && !r.ok)) return { text: 'Bir şey ters gitti', tone: 'err' }
  if (r !== null && r.ok && r.band === 'high' && sha === null) return { text: "Commit'e bir bak", tone: 'err' }
  if (s.kind === 'pending' || s.kind === 'behind' || s.kind === 'slow') return { text: 'Güncelleme bekliyor', tone: 'warn' }
  if (s.kind === 'updating' || s.kind === 'graph' || s.kind === 'checking' || sha !== null) return { text: 'Kodu okuyorum…', tone: 'busy' }
  if (s.kind === 'noindex') return { text: 'Index yok', tone: 'idle' }
  return { text: 'Her şey güncel', tone: 'idle' }
}

// Rows a line takes at this width (a wrapped line may take several).
export function rowsOf(text: string, columns: number): number {
  return Math.max(1, Math.ceil(text.length / Math.max(1, columns)))
}

export const register: Register = (on, options) => {
  on('session.start', async ($, e, next) => {
    live.cwd = await $.session.cwd()
    cfg.auto = asMode(options.auto)
    cfg.assist = typeof options.assist === 'string' ? options.assist : 'off'
    live.root = await findRoot($, typeof options.root === 'string' ? options.root.trim() : '', live.cwd)
    if (live.root !== undefined) await resolveTools($, live.root, options)
    await loadSettings($, e.isInteractive !== false)
    await $.command.register({ name: 'verinoda-update', description: 'Re-index the files edited this session now.' })
    await $.command.register({ name: 'verinoda-assist', description: 'Where a change belongs: locate files, what changes together, a tool and the first search (off, inject, tool, full, strict).' })
    await $.command.register({ name: 'verinoda-auto', description: 'Code questions: nudge (start with Verinoda analyze), search (attach results) or off.' })
    await $.command.register({ name: 'verinoda-guard', description: 'Review each commit with Verinoda afterwards: on, off, or toggle.' })
    await $.command.register({ name: 'verinoda-check', description: 'Check each edit of Python/Java/Kotlin code for names that do not exist: on, off, or toggle.' })
    // not plain `verinoda`: the Verinoda agent skill of that name takes `/verinoda` first
    await $.command.register({ name: 'verinoda-panel', description: 'Open the Verinoda pane: index state, settings, last context and review.' })
    publish($)
    background(checkGraph($))
    if (live.root !== undefined && (await read($, check))) background(warmCheck($, live.root))
    const f = await features($)
    if (live.root !== undefined && f.tool) await registerAssistTools($) // awaited: listed by the first turn
    ensureDaemon($, f)
    return next(e)
  })

  // The tools are first-class: their schemas in the prompt's list, not behind ToolSearch, where the agent studies found
  // a tool that must first be looked up is not called (in this build a plugin's tool is deferred unless a hook says not).
  on('tool.describe', { tool: [LOCATE_TOOL, COUPLED_TOOL] }, ($, e) => {
    const spec = TOOL_SPECS.find(s => ASSIST_TOOL(s.name) === e.tool)
    return { description: spec?.description ?? e.description, isDeferred: false }
  })

  on('prompt.compose', async ($, e, next) => {
    const out = await next(e)
    const f = await features($)
    if (!f.prompt || live.root === undefined) return out
    return { sections: [...out.sections, { id: 'verinoda-live:assist', text: assistPrompt(f), scope: 'session' as const }] }
  })

  // The tools answer a plain string: any other shape fails the engine's check of a plugin's tool result.
  on('tool.call', { tool: LOCATE_TOOL }, async ($, e) => {
    live.usedLocate = true
    const root = live.root
    if (root === undefined) return { result: 'verinoda locate: this project has no Verinoda index' }
    const text = typeof e.text === 'string' ? e.text.trim() : ''
    if (text === '') return { result: 'verinoda locate needs the text of the bug report or task (the text argument)' }
    const named = Array.isArray(e.files) ? e.files.filter((f): f is string => typeof f === 'string' && f !== '') : []
    const anchors = (named.length > 0 ? named.map(f => toRel(root, f)) : live.reads).slice(0, ANCHORS_MAX)
    const located = await lookup($, root, { op: 'locate', text: text.slice(0, LOCATE_TEXT_MAX), anchors })
    if (located === undefined) return { result: 'verinoda locate failed (the CLI did not answer); search the code yourself' }
    return { result: renderLocate(located, LOCATE_ANSWER_MAX) ?? 'verinoda locate found no file for this text' }
  })

  on('tool.call', { tool: COUPLED_TOOL }, async ($, e) => {
    const root = live.root
    if (root === undefined) return { result: 'verinoda coupled: this project has no Verinoda index' }
    const files = Array.isArray(e.files) ? e.files.filter((f): f is string => typeof f === 'string' && f !== '').map(f => toRel(root, f)).slice(0, ANCHORS_MAX) : []
    if (files.length === 0) return { result: 'verinoda coupled needs the files to look up (the files argument)' }
    const located = await lookup($, root, { op: 'coupled', files })
    if (located === undefined) return { result: 'verinoda coupled failed (the CLI did not answer); search the code yourself' }
    return { result: renderLocate(located, LOCATE_ANSWER_MAX) ?? 'verinoda coupled found no file that changes together with these' }
  })

  // A file the model opens brings the files that change together with it: attached to a call the agent makes anyway.
  on('tool.call', { tool: 'Read' }, async ($, e, next) => {
    const ran = await next(e)
    const root = live.root
    if (root === undefined || ran.deny !== undefined || ran.isError === true || typeof e.file_path !== 'string') return ran
    if (!isInside(e.file_path, root)) return ran
    const rel = toRel(root, e.file_path)
    if (rel.startsWith('.verinoda/')) return ran
    noteRead(rel)
    if (!(await features($)).coupled || !isSourceFile(rel) || live.coupledAsked.has(rel) || live.coupledAsked.size >= COUPLED_BUDGET) return ran
    live.coupledAsked.add(rel)
    const note = coupledNote(rel, await lookup($, root, { op: 'coupled', files: [rel] }))
    return note === undefined ? ran : { ...ran, context: [...(ran.context ?? []), note] }
  })

  // The task's first search is answered with the files that point to it, and run again if still wanted: the answer
  // reaches an agent that did not ask. Once per task, and never once the model has called the tool itself.
  on('tool.call', { tool: ['Grep', 'Glob', ...SHELL_TOOLS] }, async ($, e, next) => {
    const root = live.root
    if (root === undefined || live.gated || live.usedLocate || live.task === '' || !isBlockedSearch(e)) return next(e)
    if (!(await features($)).gate) return next(e)
    live.gated = true
    const located = await lookup($, root, { op: 'locate', text: live.task.slice(0, LOCATE_TEXT_MAX), anchors: live.reads.slice(0, ANCHORS_MAX) })
    const answer = renderLocate(located, LOCATE_ANSWER_MAX)
    return answer === undefined ? next(e) : { deny: gateText(answer) }
  })

  on('tool.call', { tool: EDIT_TOOLS }, async ($, e, next) => {
    const ran = await next(e)
    const root = live.root
    if (root === undefined || ran.deny !== undefined || ran.isError === true) return ran

    const path = e.tool === 'NotebookEdit' ? e.notebook_path : e.tool === 'Edit' || e.tool === 'Write' ? e.file_path : undefined
    const file = path === undefined ? '' : norm(path)
    if (!file.startsWith(`${norm(root)}/`) || file.includes('/.verinoda/')) return ran
    live.pending.add(file)
    publish($)
    if (path === undefined || !isCheckable(path) || !(await read($, check))) return ran
    // the edit has gone through: nothing the check does may change that
    const note = await checkEdit($, root, path.replace(/\\/g, '/')).catch(() => undefined)
    return note === undefined ? ran : { ...ran, context: [...(ran.context ?? []), note] }
  })

  // After a command that moved the project's HEAD (a commit, an amend, an alias of either), review what it changed;
  // HEAD is read before and after, so a git command that made no commit there is never reviewed.
  on('tool.call', { tool: SHELL_TOOLS }, async ($, e, next) => {
    const root = live.root
    if (root === undefined || !GIT_WORD.test(e.command) || !(await read($, guard))) return next(e)
    const before = await gitHead($, root)
    const ran = await next(e)
    const after = await gitHead($, root)
    if (ran.deny === undefined && before !== undefined && after !== undefined && after !== before) background(reviewCommit($, after))
    return ran
  })

  // A code question typed in the project gets the nudge (or, in search mode, Verinoda's results) as context.
  on('prompt.submit', async ($, e, next) => {
    const isUser = isPersonsPrompt(e.origin)
    if (isUser) startTask(e.text)
    if (live.root === undefined || !isUser || !isInside(live.cwd, live.root)) return next(e)
    const blocks: string[] = []
    if ((await features($)).inject && looksLikeCodeQuestion(e.text, NUDGE_MAX_CHARS)) {
      const located = await lookup($, live.root, { op: 'locate', text: e.text.trim().slice(0, LOCATE_TEXT_MAX), anchors: [] })
      const answer = renderLocate(located, LOCATE_ANSWER_MAX)
      if (answer !== undefined) blocks.push(locateBlock(answer))
    }
    const mode = await read($, auto)
    if (mode !== 'off' && looksLikeCodeQuestion(e.text, mode === 'nudge' ? NUDGE_MAX_CHARS : 2_000)) {
      let block: string | undefined
      if (mode === 'nudge') {
        const nudge = nudgeBlock(live.root, cfg.cli)
        block = nudge
        const at = await $.clock.now()
        await update($, lastContext, () => ({ prompt: e.text.trim().slice(0, 80), mode, ok: true, chars: nudge.length, seconds: 0, note: '', at }))
      } else {
        block = await retrieve($, e.text)
      }
      if (block !== undefined) blocks.push(block)
    }
    return next(blocks.length === 0 ? e : { ...e, context: [...(e.context ?? []), ...blocks] })
  })

  // One re-index per turn, not per edit; left running so the turn ends at once.
  on('turn.complete', ($, e, next) => {
    if (e.agentId === undefined && live.pending.size > 0 && !live.isRunning) background(reindex($))
    return next(e)
  })

  on('command.run', { command: 'verinoda-update' }, async $ => ({ text: (await reindex($)).text }))

  on('command.run', { command: 'verinoda-auto' }, async ($, e) => {
    const mode = modeCommand(e.args, await read($, auto))
    if (mode === undefined) return { text: 'usage: /verinoda-auto [nudge|search|off] (no argument toggles nudge/off)' }
    await setAuto($, mode)
    const where = mode !== 'off' && live.root !== undefined && !isInside(live.cwd, live.root) ? ` (only for sessions started in ${live.root})` : ''
    return { text: `auto-context: ${mode}${where}` }
  })

  on('command.run', { command: 'verinoda-assist' }, async ($, e) => {
    if (e.args.trim() === '') return { text: `assist: ${await read($, assist)} (${ASSIST_USAGE})` }
    const value = parseAssist(e.args)
    if (value === undefined) return { text: ASSIST_USAGE }
    await setAssist($, value)
    const f = assistFeatures(value)
    if (live.root !== undefined && f.tool) await registerAssistTools($)
    ensureDaemon($, f)
    const where = value !== 'off' && live.root !== undefined && !isInside(live.cwd, live.root) ? ` (only for sessions started in ${live.root})` : ''
    return { text: `assist: ${value}${where}` }
  })

  on('command.run', { command: 'verinoda-guard' }, async ($, e) => {
    const value = settingCommand(e.args, await read($, guard))
    if (value === undefined) return { text: 'usage: /verinoda-guard [on|off]' }
    await setGuard($, value)
    return { text: `review after commits ${value ? 'on' : 'off'}` }
  })

  on('command.run', { command: 'verinoda-check' }, async ($, e) => {
    const value = settingCommand(e.args, await read($, check))
    if (value === undefined) return { text: 'usage: /verinoda-check [on|off]' }
    await setCheck($, value)
    return { text: `check after edits ${value ? 'on' : 'off'}` }
  })

  on('command.run', { command: 'verinoda-panel' }, async $ => {
    await $.ui.open({ id: PANE, title: 'Verinoda', focus: true })
    return { text: 'Verinoda pane opened.' }
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Button, Text } = $.ui.resolve(e)
    const s = await read($, idx)
    const said = await read($, notice)
    const mode = await read($, auto)
    const isGuard = await read($, guard)
    const ctx = await read($, lastContext)
    const rev = await read($, lastReview)
    const sha = await read($, reviewing)
    const isExpanded = await read($, expanded)
    const isCheck = await read($, check)
    const lastChk = await read($, lastCheck)

    const columns = Math.max(20, e.props.bodyColumns ?? 48)
    const isInline = e.props.placement === 'inline'
    const bodyRows = e.props.scroll?.bodyRows ?? 0
    const tone = (t: Tone) => TONE_COLOR[t]
    let used = 0 // rows the content takes, counted while the tree is built, to know what is left for the mascot
    const count = (text: string) => { used += rowsOf(text, columns) }

    // 1 · the index
    const ix = describeIndex(s)
    const action = ix.action
    const indexBlock = (
      <Box key="index" flexDirection="column">
        <Text bold color={tone(ix.tone)} wrap="wrap">{ix.head}</Text>
        {!isInline && ix.details.map((d, i) => <Text key={`d${i}`} dimColor wrap="truncate">{`  ${d}`}</Text>)}
        {action !== null && (
          <Button key="update" label={ACTION_LABEL[action]} hotkey="u" variant={action === 'recheck' ? 'secondary' : 'primary'}
            onPress={() => background(action === 'recheck' ? recheckFromPane($) : updateFromPane($))} />
        )}
        {said !== null && (
          <Text key="notice" color={said.tone === 'err' ? 'red' : said.tone === 'warn' ? 'yellow' : undefined}
            dimColor={said.tone === 'ok'} wrap="truncate">{`  ${said.text}`}</Text>
        )}
      </Box>
    )
    count(ix.head)
    if (!isInline) used += ix.details.length
    if (action !== null) used += 1
    if (said !== null) used += 1

    if (s.kind === 'noindex') return <Box flexDirection="column">{indexBlock}</Box>

    // 2 · the last commit
    const rv = describeReview(rev, sha)
    const showReview = rv !== null || isGuard
    const summary = rev !== null && rev.ok && rev.findings > 0 && sha === null ? rev.summary : ''
    const clamp = columns * 2 - 3
    const isCut = summary.length > clamp
    const shown = isExpanded || !isCut ? summary : `${summary.slice(0, clamp)}…`
    const emptyReview = 'Henüz commit incelenmedi'
    const reviewBlock = !showReview ? null : (
      <Box key="review" flexDirection="column">
        {!isInline && <Text bold>Son commit</Text>}
        {rv === null
          ? <Text dimColor wrap="truncate">{emptyReview}</Text>
          : <Text bold={rv.tone === 'err'} color={tone(rv.tone)} wrap="wrap">{isInline ? `Son commit: ${rv.head}` : rv.head}</Text>}
        {!isInline && rv !== null && rv.meta !== '' && <Text dimColor wrap="truncate">{`  ${rv.meta}`}</Text>}
        {!isInline && shown !== '' && <Text wrap="wrap">{shown}</Text>}
        {!isInline && isCut && (
          <Button key="review-more" label={isExpanded ? 'Kısalt' : 'Ayrıntı'} hotkey="d" variant="secondary"
            onPress={() => update($, expanded, x => !x)} />
        )}
      </Box>
    )
    if (reviewBlock !== null) {
      if (!isInline) used += 2 // the title and the gap above it
      count(rv === null ? emptyReview : rv.head)
      if (!isInline && rv !== null && rv.meta !== '') used += 1
      if (!isInline && shown !== '') count(shown)
      if (!isInline && isCut) used += 1
    }

    // 3 · auto-context: all three modes in a fixed order, the selected one primary and marked
    const outside = mode !== 'off' && live.root !== undefined && !isInside(live.cwd, live.root)
    const last = mode === 'off' ? null : contextLine(ctx)
    const isNarrow = columns < 36
    const contextEl = (
      <Box key="context" flexDirection="column">
        {!isInline && <Text bold>Otomatik bağlam</Text>}
        <Box flexDirection={isNarrow ? 'column' : 'row'} columnGap={1}>
          {isInline && <Text>Bağlam</Text>}
          {MODES.map((m, i) => (
            <Button key={`auto-${m}`} label={`${m === mode ? '● ' : ''}${MODE_LABEL[m]}`} hotkey={String(i + 1)}
              variant={m === mode ? 'primary' : 'secondary'} onPress={() => (m === mode ? undefined : setAuto($, m))} />
          ))}
        </Box>
        {!isInline && (outside
          ? <Text color="yellow" wrap="wrap">{OUTSIDE_HINT}</Text>
          : <Text dimColor wrap="wrap">{MODE_HINT[mode]}</Text>)}
        {!isInline && last !== null && (
          <Text dimColor={last.tone === 'idle'} color={last.tone === 'warn' ? 'yellow' : undefined} wrap="truncate">{last.text}</Text>
        )}
      </Box>
    )
    if (!isInline) {
      used += 2 // the title and the gap above it
      used += isNarrow ? MODES.length : 1
      count(outside ? OUTSIDE_HINT : MODE_HINT[mode])
      if (last !== null) used += 1
    }

    // 4 · review after commits: the same two-option control; r always flips it
    const guardEl = (
      <Box key="guard" flexDirection="column">
        <Box flexDirection={columns < 40 ? 'column' : 'row'} columnGap={1}>
          <Text bold={!isInline}>{isInline ? 'İnceleme' : 'Commit incelemesi'}</Text>
          <Button key="guard-on" label={isGuard ? '● Açık' : 'Açık'} hotkey={isGuard ? undefined : 'r'}
            variant={isGuard ? 'primary' : 'secondary'} onPress={() => (isGuard ? undefined : setGuard($, true))} />
          <Button key="guard-off" label={isGuard ? 'Kapalı' : '● Kapalı'} hotkey={isGuard ? 'r' : undefined}
            variant={isGuard ? 'secondary' : 'primary'} onPress={() => (isGuard ? setGuard($, false) : undefined)} />
        </Box>
        {!isInline && <Text dimColor wrap="wrap">{GUARD_HINT}</Text>}
      </Box>
    )
    if (!isInline) {
      used += 2 + (columns < 40 ? 2 : 0) // the row (stacked when narrow) and the gap above it
      count(GUARD_HINT)
    }

    // 5 · the check after edits: the same two-option control; k always flips it; the last check under it
    const lastLine = checkLine(lastChk)
    const checkEl = (
      <Box key="check" flexDirection="column">
        <Box flexDirection={columns < 40 ? 'column' : 'row'} columnGap={1}>
          <Text bold={!isInline}>{isInline ? 'Kontrol' : 'Kod kontrolü'}</Text>
          <Button key="check-on" label={isCheck ? '● Açık' : 'Açık'} hotkey={isCheck ? undefined : 'k'}
            variant={isCheck ? 'primary' : 'secondary'} onPress={() => (isCheck ? undefined : setCheck($, true))} />
          <Button key="check-off" label={isCheck ? 'Kapalı' : '● Kapalı'} hotkey={isCheck ? 'k' : undefined}
            variant={isCheck ? 'secondary' : 'primary'} onPress={() => (isCheck ? setCheck($, false) : undefined)} />
        </Box>
        {!isInline && <Text dimColor wrap="wrap">{CHECK_HINT}</Text>}
        {lastLine !== null && (
          <Text key="check-last" dimColor={lastLine.tone === 'idle'} color={lastLine.tone === 'warn' ? 'yellow' : undefined}
            wrap="truncate">{lastLine.text}</Text>
        )}
      </Box>
    )
    if (!isInline) {
      used += 2 + (columns < 40 ? 2 : 0)
      count(CHECK_HINT)
      if (lastLine !== null) used += 1
    }

    if (isInline) {
      return (
        <Box flexDirection="column">
          {indexBlock}
          {reviewBlock}
          <Box flexDirection="row" columnGap={2} flexWrap="wrap">{contextEl}{guardEl}{checkEl}</Box>
        </Box>
      )
    }

    // 6 · footer: only the keys that exist in this drawing
    const keys = [action !== null ? 'u güncelle' : '', '1-3 bağlam', 'r inceleme', 'k kontrol', isCut ? 'd ayrıntı' : ''].filter(Boolean)
    const footer = e.props.isFocused ? keys.join(' · ') : 'Kısayollar için ctrl+x tab'
    used += 2 // the footer and the gap above it

    // 6 · the mascot, only in the room left over: never cropped, never pushing content
    const spare = bodyRows - used
    const caption = mascotCaption(s, rev, sha)
    const showMascot = columns >= 24 && !isExpanded && spare >= MASCOT_HEIGHT + 3
    const mascotProps: MascotProps = {
      mood: MOOD[caption.tone], caption: caption.text, captionColor: tone(caption.tone), captionDim: caption.tone === 'idle',
    }
    const { Client } = $.ui.resolve(e) as { Client?: (props: Record<string, unknown>) => JSX.Element }
    const mascot = !showMascot ? null : (
      <Box key="mascot" flexDirection="column" alignItems="center" marginTop={Math.max(0, spare - MASCOT_HEIGHT - 3)}>
        {Client !== undefined && cfg.motion && ANIMATED_SURFACES.includes(e.surface)
          ? <Client key="mascot-client" module="./mascot.tsx" props={mascotProps} width={MASCOT_WIDTH} height={MASCOT_HEIGHT + 1} />
          : (
            <Box flexDirection="column" width={MASCOT_WIDTH}>
              {frameRows(0, mascotProps.mood).map((row, i) => (
                <Text key={`m${i}`} wrap="truncate">
                  {row.map(([text, c], j) => (c === null ? text : <Text key={`m${i}-${j}`} color={c}>{text}</Text>))}
                </Text>
              ))}
              <Text color={mascotProps.captionColor} dimColor={mascotProps.captionDim} wrap="truncate">{caption.text}</Text>
            </Box>
          )}
      </Box>
    )

    return (
      <Box flexDirection="column" rowGap={1}>
        {indexBlock}
        {reviewBlock}
        {contextEl}
        {guardEl}
        {checkEl}
        {mascot}
        <Text dimColor wrap="truncate">{footer}</Text>
      </Box>
    )
  })
}
