// The pure parts of the assist features (0.5): which are on, how a located answer reads to the model, which calls open
// a code hunt. Nothing here runs a process or touches the engine; register.tsx does.
//
// What the features are for: the agent studies (benchmarks/agent_compare) found the agent never calls a tool it is
// only told about, and that what it misses are the sibling files of the ones it found. So the answer is put where the
// agent is already looking: with the prompt (`inject`), with the result of a file it reads (`coupled`), in place of its
// first search (`gate`); and the same lookups are offered as tools (`tool`) with a line in the system prompt (`prompt`).

export type Features = { inject: boolean; coupled: boolean; tool: boolean; prompt: boolean; gate: boolean }

export const FEATURE_NAMES = ['inject', 'coupled', 'tool', 'prompt', 'gate'] as const

const NONE: Features = { inject: false, coupled: false, tool: false, prompt: false, gate: false }

export const PRESETS: Readonly<Record<string, Features>> = {
  off: NONE,
  inject: { ...NONE, inject: true },
  tool: { ...NONE, tool: true, prompt: true },
  full: { ...NONE, coupled: true, tool: true, prompt: true },
  strict: { ...NONE, coupled: true, tool: true, prompt: true, gate: true },
}

const squeeze = (value: string) => value.trim().toLowerCase().replace(/\s+/g, '')
const isPreset = (name: string) => Object.prototype.hasOwnProperty.call(PRESETS, name)

// A setting as the features it turns on: a preset's name, or a comma list of features. Whatever is not a feature (a
// value stored by another version, a typo in the settings) turns nothing on.
export function assistFeatures(value: unknown): Features {
  if (typeof value !== 'string') return NONE
  const v = squeeze(value)
  if (isPreset(v)) return PRESETS[v] as Features
  const on = new Set(v.split(','))
  return {
    inject: on.has('inject'), coupled: on.has('coupled'), tool: on.has('tool'), prompt: on.has('prompt'), gate: on.has('gate'),
  }
}

// What /verinoda-assist accepts: a preset, or a list of features only; undefined for anything else.
export function parseAssist(arg: string): string | undefined {
  const v = squeeze(arg)
  if (v === '') return undefined
  if (isPreset(v)) return v
  const names = v.split(',')
  return names.every(n => (FEATURE_NAMES as readonly string[]).includes(n)) ? names.join(',') : undefined
}

export const ASSIST_USAGE =
  `usage: /verinoda-assist ${Object.keys(PRESETS).join('|')}, or a list of ${FEATURE_NAMES.join(', ')} (for example inject,coupled)`

// What `verinoda locate` and `verinoda coupled` print with --json: the answer as text, and the files behind it.
export type LocatedFile = { path?: string; tier?: string; why?: string }
export type Located = { text?: string; files?: LocatedFile[] }

const named = (files: LocatedFile[] | undefined) =>
  (files ?? []).filter((f): f is LocatedFile & { path: string } => typeof f.path === 'string' && f.path !== '')

const cut = (text: string, max: number) => (text.length > max ? `${text.slice(0, Math.max(0, max - 1))}…` : text)

// The answer the model reads: the CLI's own text when it has one, else built from the files; never longer than `max`;
// undefined when it found no file (an answer of "nothing" is not worth the model's attention).
export function renderLocate(located: Located | undefined, max: number): string | undefined {
  if (located === undefined) return undefined
  const files = named(located.files)
  if (Array.isArray(located.files) && files.length === 0) return undefined
  const given = typeof located.text === 'string' ? located.text.trim() : ''
  if (given !== '') return cut(given, max)
  if (files.length === 0) return undefined
  const lines = files.map(f => `- ${f.path}${f.tier ? ` (${f.tier})` : ''}${f.why ? `: ${f.why}` : ''}`)
  return cut(['Verinoda locate: files this change may belong in', ...lines].join('\n'), max)
}

const COUPLED_LISTED = 8

// What the model reads after a file it opened: the files that change together with it, or undefined when there are none.
export function coupledNote(file: string, located: Located | undefined): string | undefined {
  const files = named(located?.files).filter(f => f.path !== file)
  if (files.length === 0) return undefined
  const lines = files.slice(0, COUPLED_LISTED).map(f => `- ${f.path}${f.why ? `: ${f.why}` : ''}`)
  return (
    `[Verinoda coupled] ${file} is usually changed together with these files (git history, same-named and mirrored ` +
    'files). A fix to it may need the same fix there; they are leads, not rules, so check each:\n' +
    lines.join('\n')
  )
}

// Commands that look for code: grep and its relatives, find, git grep, PowerShell's Select-String. A pipeline or a
// `cd x && grep` is read segment by segment; the first word of a segment is the command (after any NAME=value).
const SEARCH_COMMANDS = new Set(['grep', 'egrep', 'fgrep', 'zgrep', 'rg', 'ag', 'ack', 'find', 'fd', 'fdfind', 'findstr', 'select-string', 'sls'])

function startsSearch(segment: string): boolean {
  const words = segment.trim().split(/\s+/).filter(w => !/^[A-Za-z_][\w]*=/.test(w))
  const first = (words[0] ?? '').toLowerCase().replace(/^.*[\\/]/, '').replace(/\.exe$/, '')
  if (first === 'git') return (words[1] ?? '').toLowerCase() === 'grep'
  return SEARCH_COMMANDS.has(first)
}

export function isBlockedSearch(e: { tool: string; command?: unknown }): boolean {
  if (e.tool === 'Grep' || e.tool === 'Glob') return true
  if (e.tool !== 'Bash' && e.tool !== 'PowerShell') return false
  return typeof e.command === 'string' && e.command.split(/&&|\|\||[;|\n]/).some(startsSearch)
}

// Source code, for the notes about what changes together: a file that is neither a test nor a document nor data.
const CODE_FILE = /\.(c|h|cc|cpp|cxx|hpp|hh|hxx|inl|m|mm|s|asm|py|pyi|pyx|js|jsx|mjs|cjs|ts|tsx|java|kt|kts|scala|go|rs|rb|php|cs|swift|lua|sh|bash|cmake|bf|pml)$/i
const BUILD_FILE = /^(cmakelists\.txt|makefile|kconfig|kbuild|meson\.build|build\.bazel|build|.*\.mk)$/i
const TEST_FILE = /(^test_|_test\.|\.test\.|\.spec\.|^conftest\.py$)/i
const NOT_SOURCE_DIR = new Set(['docs', 'doc', 'documentation', 'test', 'tests', '__tests__', 'spec', 'specs'])

export function isSourceFile(path: string): boolean {
  const parts = path.replace(/\\/g, '/').split('/')
  const base = parts[parts.length - 1] ?? ''
  if (parts.slice(0, -1).some(p => NOT_SOURCE_DIR.has(p.toLowerCase())) || TEST_FILE.test(base)) return false
  return CODE_FILE.test(base) || BUILD_FILE.test(base)
}

export const LOCATE_TOOL = 'mcp__verinoda-live__locate'
export const COUPLED_TOOL = 'mcp__verinoda-live__coupled'

export const TOOL_SPECS = [
  {
    name: 'locate',
    description:
      'Locate where a change belongs in this project. Give the text of the bug report or task (and files you already know ' +
      'are involved). Returns the files most likely to need a change, each with the reason: where the text matches the ' +
      "code index, files that changed together with those files in the project's git history, same-named or mirrored " +
      'files in sibling directories, and header/source partners. A fix often belongs in several of them: call this ' +
      'before searching with grep or find, read what it lists, and name every file that needs the change.',
    inputSchema: {
      type: 'object',
      properties: {
        text: { type: 'string', description: 'The bug report or task as written (title and body).' },
        files: { type: 'array', items: { type: 'string' }, description: 'Files you already know are involved (paths relative to the project); optional.' },
      },
      required: ['text'],
    },
  },
  {
    name: 'coupled',
    description:
      'List the files that usually change together with the given files: the same commits in the git history, same-named ' +
      'or mirrored files in sibling directories, header/source partners. Use it on a file you are about to change, to see ' +
      'where else the same change may belong.',
    inputSchema: {
      type: 'object',
      properties: { files: { type: 'array', items: { type: 'string' }, description: 'Paths relative to the project.' } },
      required: ['files'],
    },
  },
] as const

// The line the system prompt carries (feature `prompt`), written for what is on.
export function assistPrompt(f: Features): string {
  const parts = ['Verinoda, an index of this project\'s code and git history, is connected.']
  if (f.tool) {
    parts.push(
      `When you are given a bug report or a change to make, call ${LOCATE_TOOL} with its text before you search with ` +
        'grep, find or glob: it lists the files most likely to need a change, including files that usually change ' +
        'together with them and same-named files in sibling directories.',
    )
  }
  parts.push(
    'A change often belongs in several files, not only the first one you find. When you say where a fix goes, name ' +
      'every file that needs the change, siblings and mirrored copies included, not only the first match.',
  )
  if (f.tool && f.coupled) parts.push(`${COUPLED_TOOL} gives the same for any file; opening a source file may add that note by itself.`)
  return parts.join(' ')
}
