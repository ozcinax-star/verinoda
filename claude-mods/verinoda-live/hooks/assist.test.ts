import { describe, expect, mock, test } from 'claude-code/testing'
import type { On } from 'claude-code'

import { assistFeatures, assistPrompt, coupledNote, isBlockedSearch, isSourceFile, parseAssist, renderLocate } from './assist'

const ROOT = 'C:/work/proj'
const START = { cwd: ROOT, surface: 'terminal', isInteractive: true } as never
const LOCATE = 'mcp__verinoda-live__locate'
const COUPLED = 'mcp__verinoda-live__coupled'
const REPORT = `Where should this be fixed? ${'The scheduler is preempted twice in a row on SMP. '.repeat(10)}`.trim()

// What `verinoda locate --json` / `coupled --json` print: the rendered text and the files behind it.
const LOCATE_JSON = {
  text: 'verinoda locate: 2 files\nlikely\n  src/kernel/thread.c:351-412 schedule\nalso check\n  src/arch/arm/kernel/thread.c - twin',
  files: [
    { path: 'src/kernel/thread.c', tier: 'likely', why: 'matches: scheduler' },
    { path: 'src/arch/arm/kernel/thread.c', tier: 'coupled', why: 'twin of src/arch/x86/kernel/thread.c' },
  ],
}
const COUPLED_JSON = {
  text: 'coupled\n  include/thread.h - changed together in 6 of 14 commits',
  files: [{ path: 'include/thread.h', tier: 'coupled', why: 'changed together in 6 of 14 commits' }],
}

type Opts = {
  daemon?: { url: string; token: string } | 'unsupported'
  locate?: { exitCode: number; stdout: string }
  fetchOk?: boolean
}

// The world beneath the plugin: an indexed project, a CLI whose locate/coupled answer from here, a daemon that may or
// may not exist, and a record of what ran, what was registered and what went over HTTP.
function world(on: On, opts: Opts = {}) {
  const runs: string[][] = []
  const registered: string[] = []
  const fetched: { url: string; init: { headers?: Record<string, string>; body?: string } }[] = []
  on('session.start', () => ({ cwd: ROOT }))
  on('session.cwd', () => ({ value: ROOT }))
  on('fs.stat', () => ({ value: { isFile: false, isDirectory: true, size: 0, mtimeMs: 0 } }) as never)
  on('command.register', ($, e) => ({ value: { command: e.name } }) as never)
  on('ui.status', () => ({ value: undefined }))
  on('tool.register', ($, e) => {
    registered.push(e.name)
    return { value: { tool: `mcp__verinoda-live__${e.name}` } } as never
  })
  on('process.run', ($, e) => {
    if (e.argv[1] === 'check') return { value: { exitCode: 0, stdout: '{"sites": []}', stderr: '' } } as never
    runs.push([...e.argv])
    if (e.argv[1] === '-c') return { value: { exitCode: 0, stdout: '{"locked": false, "behind": 0}', stderr: '' } } as never
    if (e.argv[1] === 'locate' && e.argv.includes('--daemon')) {
      if (opts.daemon === undefined || opts.daemon === 'unsupported') return { value: { exitCode: 2, stdout: '', stderr: 'unknown option' } } as never
      return { value: { exitCode: 0, stdout: JSON.stringify({ running: true, ...opts.daemon, pid: 1 }), stderr: '' } } as never
    }
    if (e.argv[1] === 'locate') return { value: { stderr: '', ...(opts.locate ?? { exitCode: 0, stdout: JSON.stringify(LOCATE_JSON) }) } } as never
    if (e.argv[1] === 'coupled') return { value: { exitCode: 0, stdout: JSON.stringify(COUPLED_JSON), stderr: '' } } as never
    return { value: { exitCode: 0, stdout: '{}', stderr: '' } } as never
  })
  on('http.fetch', ($, e) => {
    fetched.push({ url: e.url, init: (e.init ?? {}) as never })
    const body = e.url.endsWith('/coupled') ? COUPLED_JSON : LOCATE_JSON
    return { value: { status: opts.fetchOk === false ? 500 : 200, ok: opts.fetchOk !== false, headers: {}, text: JSON.stringify(body) } } as never
  })
  on('tool.call', () => ({ result: 'ok' }) as never)
  on('prompt.submit', ($, e) => ({ text: e.text, context: e.context }))
  on('prompt.compose', () => ({ sections: [{ id: 'intro', text: 'You are an agent.', scope: 'shared' }] }) as never)
  const cli = (op: string) => runs.filter(r => r[1] === op && !r.includes('--daemon'))
  return { runs, registered, fetched, cli }
}

const COMPOSE = { model: 'm', promptModel: 'm', surfaces: ['terminal'], tools: [], outputStyle: null, traits: [] } as never
const ask = (text: string, origin: Record<string, unknown> = { kind: 'sdk' }) => ({ text, wait: false, origin }) as never
const READ = (rel: string) => ({ tool: 'Read', file_path: `${ROOT}/${rel}` }) as never
const run = (args = '') => ({ command: 'verinoda-assist', args }) as never
const contextOf = (r: unknown) => ((r as { context?: readonly string[] }).context ?? [])

describe('assist: pure parts', () => {
  test('presets, lists and nonsense', () => {
    expect(assistFeatures(undefined)).toEqual({ inject: false, coupled: false, tool: false, prompt: false, gate: false })
    expect(assistFeatures('off')).toEqual(assistFeatures(''))
    expect(assistFeatures('tool')).toEqual({ inject: false, coupled: false, tool: true, prompt: true, gate: false })
    expect(assistFeatures('full')).toEqual({ inject: false, coupled: true, tool: true, prompt: true, gate: false })
    expect(assistFeatures('strict')).toEqual({ inject: false, coupled: true, tool: true, prompt: true, gate: true })
    expect(assistFeatures('inject')).toEqual({ inject: true, coupled: false, tool: false, prompt: false, gate: false })
    expect(assistFeatures('inject, coupled')).toEqual({ inject: true, coupled: true, tool: false, prompt: false, gate: false })
    expect(assistFeatures('coupled,loud,gate')).toEqual({ inject: false, coupled: true, tool: false, prompt: false, gate: true })
    expect(assistFeatures('loud')).toEqual(assistFeatures('off'))
    expect(assistFeatures('constructor')).toEqual(assistFeatures('off'))
    expect(assistFeatures(true)).toEqual(assistFeatures('off'))
  })

  test('what /verinoda-assist accepts', () => {
    expect(parseAssist('FULL')).toBe('full')
    expect(parseAssist(' inject , coupled ')).toBe('inject,coupled')
    expect(parseAssist('')).toBeUndefined()
    expect(parseAssist('sometimes')).toBeUndefined()
    expect(parseAssist('inject,sometimes')).toBeUndefined()
    expect(parseAssist('constructor')).toBeUndefined()
  })

  test("the rendered text is the CLI's when it has one, else built from the files, and always cut", () => {
    expect(renderLocate(LOCATE_JSON, 5_000)).toBe(LOCATE_JSON.text)
    const built = renderLocate({ files: LOCATE_JSON.files }, 5_000) ?? ''
    expect(built).toContain('src/kernel/thread.c')
    expect(built).toContain('twin of src/arch/x86/kernel/thread.c')
    expect(renderLocate({ text: 'x'.repeat(5_000) }, 100)?.length).toBeLessThanOrEqual(100)
    expect(renderLocate({ files: [] }, 100)).toBeUndefined()
    expect(renderLocate({ text: 'nothing found', files: [] }, 100)).toBeUndefined()
    expect(renderLocate(undefined, 100)).toBeUndefined()
  })

  test('the coupled note names the file and its partners, or nothing', () => {
    const note = coupledNote('src/kernel/thread.c', COUPLED_JSON) ?? ''
    expect(note.startsWith('[Verinoda coupled]')).toBe(true)
    expect(note).toContain('src/kernel/thread.c')
    expect(note).toContain('include/thread.h')
    expect(coupledNote('a.c', { files: [] })).toBeUndefined()
    expect(coupledNote('a.c', { files: [{ path: 'a.c' }] })).toBeUndefined()
    expect(coupledNote('a.c', undefined)).toBeUndefined()
  })

  test('a search that opens a code hunt is told from other calls', () => {
    expect(isBlockedSearch({ tool: 'Grep' })).toBe(true)
    expect(isBlockedSearch({ tool: 'Glob' })).toBe(true)
    for (const c of ['grep -rn foo .', 'rg foo src', 'git grep -n foo', 'find . -name "*.c"', 'cd src && grep -n x a.c', 'ls | grep c', 'LC_ALL=C grep -r x .']) {
      expect(isBlockedSearch({ tool: 'Bash', command: c })).toBe(true)
    }
    expect(isBlockedSearch({ tool: 'PowerShell', command: 'Select-String -Path src -Pattern x' })).toBe(true)
    for (const c of ['ls src', 'cat a.c', 'git status', 'python -m pytest', 'git log --grep=x', 'echo grep']) {
      expect(isBlockedSearch({ tool: 'Bash', command: c })).toBe(false)
    }
    expect(isBlockedSearch({ tool: 'Read' })).toBe(false)
  })

  test('source files are code that is not a test or a document', () => {
    for (const f of ['src/a.c', 'include/a.h', 'pkg/m.py', 'lib/x.ts', 'CMakeLists.txt', 'src/a.rs', 'src/arch/x86/Makefile']) expect(isSourceFile(f)).toBe(true)
    for (const f of ['README.md', 'docs/a.c', 'tests/test_a.py', 'src/a.test.ts', 'pkg/test_m.py', 'a.png', 'LICENSE', 'pkg/data.json']) expect(isSourceFile(f)).toBe(false)
  })

  test('the system prompt line is written for what is on', () => {
    const tool = assistPrompt(assistFeatures('tool'))
    expect(tool).toContain(LOCATE)
    expect(tool.toLowerCase()).toContain('every file')
    expect(tool).not.toContain(COUPLED)
    expect(assistPrompt(assistFeatures('full'))).toContain(COUPLED)
    expect(assistPrompt(assistFeatures('prompt'))).not.toContain(LOCATE)
  })
})

describe('assist: off by default', () => {
  test('nothing is registered, attached or run', async ($, on) => {
    mock.clock(on)
    mock.store(on)
    const w = world(on)
    await $.session.start(START)
    expect(contextOf(await $.tool.call(READ('src/kernel/thread.c')))).toEqual([])
    expect(contextOf(await $.prompt.submit(ask(REPORT)))).toEqual([])
    const out = await $.prompt.compose(COMPOSE)
    expect(out.sections.some(s => s.id === 'verinoda-live:assist')).toBe(false)
    expect(w.registered).toEqual([])
    expect(w.runs.filter(r => r[1] === 'locate' || r[1] === 'coupled').length).toBe(0)
  })
})

describe('assist: the tools the model can call', () => {
  test('the tool preset registers locate and coupled at session start', { options: { assist: 'tool' } }, async ($, on) => {
    mock.clock(on)
    mock.store(on)
    const w = world(on)
    await $.session.start(START)
    expect(w.registered).toEqual(['locate', 'coupled'])
  })

  test('both are listed in front, not behind ToolSearch', { options: { assist: 'tool' } }, async ($, on) => {
    mock.clock(on)
    mock.store(on)
    world(on)
    await $.session.start(START)
    for (const tool of [LOCATE, COUPLED]) {
      const d = await $.tool.describe({ tool, description: 'engine text', isDeferred: true } as never) as { isDeferred?: boolean; description: string }
      expect(d.isDeferred).toBe(false)
      expect(d.description.length).toBeGreaterThan(40)
    }
  })

  test('locate runs the CLI on the text, with the files the model names as anchors, and returns its text', { options: { assist: 'tool' } }, async ($, on) => {
    mock.clock(on)
    mock.store(on)
    const w = world(on)
    await $.session.start(START)
    const r = await $.tool.call({ tool: LOCATE, text: REPORT, files: ['src/a.c', 'src/b.c'] } as never) as { result?: unknown }
    const run1 = w.cli('locate')[0] ?? []
    expect(run1.slice(1, 5)).toEqual(['locate', '--repo', ROOT, '--json'])
    expect(run1).toEqual(expect.arrayContaining(['--anchor', 'src/a.c', 'src/b.c']))
    expect(run1.at(-2)).toBe('--')
    expect(run1.at(-1)).toBe(REPORT)
    expect(r.result).toBe(LOCATE_JSON.text) // a plain string: the engine takes no other shape for a plugin's tool
  })

  test('without files, the files the model has read are the anchors', { options: { assist: 'tool' } }, async ($, on) => {
    mock.clock(on)
    mock.store(on)
    const w = world(on)
    await $.session.start(START)
    await $.tool.call(READ('src/kernel/thread.c'))
    await $.tool.call({ tool: LOCATE, text: REPORT } as never)
    expect(w.cli('locate')[0]).toEqual(expect.arrayContaining(['--anchor', 'src/kernel/thread.c']))
  })

  test('coupled runs the CLI on the files', { options: { assist: 'tool' } }, async ($, on) => {
    mock.clock(on)
    mock.store(on)
    const w = world(on)
    await $.session.start(START)
    const r = await $.tool.call({ tool: COUPLED, files: ['src/kernel/thread.c'] } as never) as { result?: unknown }
    expect(w.cli('coupled')[0]?.slice(1)).toEqual(['coupled', '--repo', ROOT, '--json', 'src/kernel/thread.c'])
    expect(String(r.result)).toContain('include/thread.h')
  })

  test('a failing CLI, an empty answer or no text is a result the model can read, never a crash', { options: { assist: 'tool' } }, async ($, on) => {
    mock.clock(on)
    mock.store(on)
    world(on, { locate: { exitCode: 1, stdout: '' } })
    await $.session.start(START)
    expect(String(((await $.tool.call({ tool: LOCATE, text: REPORT } as never)) as { result?: unknown }).result)).toContain('failed')
    expect(String(((await $.tool.call({ tool: LOCATE, text: '' } as never)) as { result?: unknown }).result)).toContain('text')
  })

  test('the system prompt gets one section, last and per session, with the prompt feature', { options: { assist: 'tool' } }, async ($, on) => {
    mock.clock(on)
    mock.store(on)
    world(on)
    await $.session.start(START)
    const out = await $.prompt.compose(COMPOSE)
    expect(out.sections.map(s => s.id)).toEqual(['intro', 'verinoda-live:assist'])
    const s = out.sections[1]
    expect(s?.text).toContain(LOCATE)
    expect(s?.scope).toBe('session')
  })
})

describe('assist: the report goes in with the prompt (inject)', () => {
  test('a code question of any length up to 20,000 characters gets the locate result as context', { options: { assist: 'inject' } }, async ($, on) => {
    mock.clock(on)
    mock.store(on)
    const w = world(on)
    await $.session.start(START)
    const r = contextOf(await $.prompt.submit(ask(REPORT.repeat(10))))
    expect(r[0]?.startsWith('[Verinoda locate]')).toBe(true)
    expect(r[0]).toContain('src/kernel/thread.c:351-412 schedule')
    expect(w.cli('locate')[0]?.slice(1, 5)).toEqual(['locate', '--repo', ROOT, '--json'])
  })

  test('other prompts and a failing CLI get nothing', { options: { assist: 'inject' } }, async ($, on) => {
    mock.clock(on)
    mock.store(on)
    const w = world(on, { locate: { exitCode: 1, stdout: '' } })
    await $.session.start(START)
    expect(contextOf(await $.prompt.submit(ask('Refactor the scanner to use a pool')))).toEqual([])
    expect(contextOf(await $.prompt.submit(ask(REPORT)))).toEqual([])
    expect(w.cli('locate').length).toBe(1) // the question ran, the refactor did not
  })

  test('an answer of no files attaches nothing', { options: { assist: 'inject' } }, async ($, on) => {
    mock.clock(on)
    mock.store(on)
    world(on, { locate: { exitCode: 0, stdout: JSON.stringify({ text: 'nothing found', files: [] }) } })
    await $.session.start(START)
    expect(contextOf(await $.prompt.submit(ask(REPORT)))).toEqual([])
  })

  test('a notification is not answered', { options: { assist: 'inject' } }, async ($, on) => {
    mock.clock(on)
    mock.store(on)
    const w = world(on)
    await $.session.start(START)
    expect(contextOf(await $.prompt.submit(ask(REPORT, { kind: 'task-notification' })))).toEqual([])
    expect(w.cli('locate').length).toBe(0)
  })
})

describe('assist: what changes together with a file the model reads (coupled)', () => {
  test('a source file inside the project gets its partners as a note, once, and only a few times', { options: { assist: 'coupled' } }, async ($, on) => {
    mock.clock(on)
    mock.store(on)
    const w = world(on)
    await $.session.start(START)
    const first = contextOf(await $.tool.call(READ('src/kernel/thread.c')))
    expect(first.join('\n')).toContain('[Verinoda coupled]')
    expect(first.join('\n')).toContain('include/thread.h')
    expect(w.cli('coupled')[0]?.slice(1)).toEqual(['coupled', '--repo', ROOT, '--json', 'src/kernel/thread.c'])
    expect(contextOf(await $.tool.call(READ('src/kernel/thread.c')))).toEqual([])
    expect(w.cli('coupled').length).toBe(1)
    for (const f of ['a', 'b', 'c', 'd', 'e', 'f']) await $.tool.call(READ(`src/${f}.c`))
    expect(w.cli('coupled').length).toBe(6) // the first file and five more: the budget of notes per session
  })

  test('a document, a test and a file outside the project get nothing and cost nothing', { options: { assist: 'coupled' } }, async ($, on) => {
    mock.clock(on)
    mock.store(on)
    const w = world(on)
    await $.session.start(START)
    for (const f of ['README.md', 'tests/test_a.py', 'docs/a.c']) expect(contextOf(await $.tool.call(READ(f)))).toEqual([])
    expect(contextOf(await $.tool.call({ tool: 'Read', file_path: 'C:/other/src/a.c' } as never))).toEqual([])
    expect(w.cli('coupled').length).toBe(0)
  })
})

describe('assist: the first search is answered with the located files (gate)', () => {
  const grep = { tool: 'Grep', pattern: 'schedule' } as never

  test('the first Grep is refused once, with the answer, and the next goes through', { options: { assist: 'strict' } }, async ($, on) => {
    mock.clock(on)
    mock.store(on)
    const w = world(on)
    await $.session.start(START)
    await $.prompt.submit(ask(REPORT))
    const first = await $.tool.call(grep) as { deny?: string }
    expect(first.deny).toContain('src/kernel/thread.c:351-412 schedule')
    expect(first.deny).toContain('was not run')
    expect(w.cli('locate').length).toBe(1)
    const second = await $.tool.call(grep) as { deny?: string; result?: unknown }
    expect(second.deny).toBeUndefined()
    expect(second.result).toBe('ok')
    expect(w.cli('locate').length).toBe(1)
  })

  test('a Bash search is the same hunt; other Bash commands are not', { options: { assist: 'strict' } }, async ($, on) => {
    mock.clock(on)
    mock.store(on)
    world(on)
    await $.session.start(START)
    await $.prompt.submit(ask(REPORT))
    expect(((await $.tool.call({ tool: 'Bash', command: 'ls src' } as never)) as { deny?: string }).deny).toBeUndefined()
    expect(((await $.tool.call({ tool: 'Bash', command: 'grep -rn schedule .' } as never)) as { deny?: string }).deny).toContain('was not run')
    expect(((await $.tool.call({ tool: 'Bash', command: 'grep -rn schedule .' } as never)) as { deny?: string }).deny).toBeUndefined()
  })

  test('with no task yet, or the tool already called, nothing is refused', { options: { assist: 'strict' } }, async ($, on) => {
    mock.clock(on)
    mock.store(on)
    world(on)
    await $.session.start(START)
    expect(((await $.tool.call(grep)) as { deny?: string }).deny).toBeUndefined() // no prompt of the person's yet
    await $.prompt.submit(ask(REPORT))
    await $.tool.call({ tool: LOCATE, text: REPORT } as never)
    expect(((await $.tool.call(grep)) as { deny?: string }).deny).toBeUndefined()
  })

  test('a CLI that finds nothing lets the search through', { options: { assist: 'strict' } }, async ($, on) => {
    mock.clock(on)
    mock.store(on)
    world(on, { locate: { exitCode: 0, stdout: JSON.stringify({ files: [] }) } })
    await $.session.start(START)
    await $.prompt.submit(ask(REPORT))
    expect(((await $.tool.call(grep)) as { deny?: string }).deny).toBeUndefined()
  })

  test('without the gate feature a search is never refused', { options: { assist: 'full' } }, async ($, on) => {
    mock.clock(on)
    mock.store(on)
    world(on)
    await $.session.start(START)
    await $.prompt.submit(ask(REPORT))
    expect(((await $.tool.call(grep)) as { deny?: string }).deny).toBeUndefined()
  })
})

describe('assist: a daemon keeps the graph loaded', () => {
  test('it is started at session start and answers over HTTP with its token; the CLI is not run', { options: { assist: 'tool' } }, async ($, on) => {
    const clock = mock.clock(on)
    mock.store(on)
    const w = world(on, { daemon: { url: 'http://127.0.0.1:51234', token: 'tok' } })
    await $.session.start(START)
    await clock.settle()
    expect(w.runs.some(r => r[1] === 'locate' && r.includes('--daemon') && r.includes('start'))).toBe(true)
    await $.tool.call({ tool: LOCATE, text: REPORT, files: ['src/a.c'] } as never)
    expect(w.fetched.length).toBe(1)
    expect(w.fetched[0]?.url).toBe('http://127.0.0.1:51234/locate')
    expect(w.fetched[0]?.init.headers?.['X-Verinoda-Token']).toBe('tok')
    expect(JSON.parse(w.fetched[0]?.init.body ?? '{}')).toEqual(expect.objectContaining({ text: REPORT, anchors: ['src/a.c'] }))
    expect(w.cli('locate').length).toBe(0)
  })

  test('a daemon that does not answer falls back to the CLI, and is not asked again', { options: { assist: 'tool' } }, async ($, on) => {
    const clock = mock.clock(on)
    mock.store(on)
    const w = world(on, { daemon: { url: 'http://127.0.0.1:51234', token: 'tok' }, fetchOk: false })
    await $.session.start(START)
    await clock.settle()
    await $.tool.call({ tool: LOCATE, text: REPORT } as never)
    await $.tool.call({ tool: LOCATE, text: REPORT } as never)
    expect(w.fetched.length).toBe(1)
    expect(w.cli('locate').length).toBe(2)
  })

  test('an old CLI that knows no daemon is used as it is', { options: { assist: 'tool' } }, async ($, on) => {
    const clock = mock.clock(on)
    mock.store(on)
    const w = world(on, { daemon: 'unsupported' })
    await $.session.start(START)
    await clock.settle()
    await $.tool.call({ tool: LOCATE, text: REPORT } as never)
    expect(w.fetched.length).toBe(0)
    expect(w.cli('locate').length).toBe(1)
  })

  test('with nothing that asks for it, no daemon is started', async ($, on) => {
    const clock = mock.clock(on)
    mock.store(on)
    const w = world(on, { daemon: { url: 'http://127.0.0.1:51234', token: 'tok' } })
    await $.session.start(START)
    await clock.settle()
    expect(w.runs.some(r => r.includes('--daemon'))).toBe(false)
  })
})

describe('assist: /verinoda-assist', () => {
  test('says what is set, sets a preset or a list, and refuses anything else', async ($, on) => {
    mock.clock(on)
    mock.store(on)
    world(on)
    await $.session.start(START)
    expect(await $.command.run(run(''))).toEqual(expect.objectContaining({ text: expect.stringContaining('assist: off') }))
    expect(await $.command.run(run('full'))).toEqual(expect.objectContaining({ text: expect.stringContaining('assist: full') }))
    expect(await $.command.run(run(''))).toEqual(expect.objectContaining({ text: expect.stringContaining('assist: full') }))
    expect(await $.command.run(run('inject, coupled'))).toEqual(expect.objectContaining({ text: expect.stringContaining('assist: inject,coupled') }))
    expect(await $.command.run(run('sometimes'))).toEqual(expect.objectContaining({ text: expect.stringContaining('usage') }))
  })

  test('turning it on in the session registers the tools and keeps the choice for the next one', async ($, on) => {
    mock.clock(on)
    mock.store(on)
    const w = world(on)
    await $.session.start(START)
    expect(w.registered).toEqual([])
    await $.command.run(run('tool'))
    expect(w.registered).toEqual(['locate', 'coupled'])
    await $.session.start(START) // a later session reads what was kept
    expect(await $.command.run(run(''))).toEqual(expect.objectContaining({ text: expect.stringContaining('assist: tool') }))
  })

  test('a stored choice wins over the setting', { options: { assist: 'tool' } }, async ($, on) => {
    mock.clock(on)
    mock.store(on, { assist: 'off' })
    const w = world(on)
    await $.session.start(START)
    expect(w.registered).toEqual([])
  })

  test('the setting is where a session starts that has never been asked', { options: { assist: 'full' } }, async ($, on) => {
    mock.clock(on)
    mock.store(on)
    const w = world(on)
    await $.session.start(START)
    expect(w.registered).toEqual(['locate', 'coupled'])
  })
})
