import { describe, expect, mock, test } from 'claude-code/testing'
import type { On } from 'claude-code'

import { asMode, contextBlock, isPersonsPrompt, looksLikeCodeQuestion, modeCommand, nudgeBlock, plainQuery } from './register'

const ROOT = 'C:/work/verinoda'
const START = { cwd: ROOT, surface: 'terminal', isInteractive: true } as never
const QUESTION = 'Where is the graph rebuilt after update --fast?'
const REVIEW_JSON = JSON.stringify({ summary: 'Review of the working tree against HEAD~1: 3 change(s).', risk: { score: 41, of: 100, band: 'medium' }, counts: { findings: 2 } })

// The world beneath the plugin: an indexed project, a CLI whose query and review answer from here, and a record of
// what the plugin ran and showed.
function world(on: On, opts: { cwd?: string; query?: { exitCode: number; stdout: string }; heads?: string[];
  update?: { exitCode: number }; graph?: { locked: boolean; behind: number }[]; reviewGate?: Promise<void>;
  existing?: string[] } = {}) {
  const runs: string[][] = []
  let headCalls = 0
  let isStartChecked = false
  let graphCalls = 0
  const toasts: string[] = []
  on('session.start', () => ({ cwd: opts.cwd ?? ROOT }))
  on('session.cwd', () => ({ value: opts.cwd ?? ROOT }))
  // every path exists, or only those `existing` lists (a .verinoda index, a .venv CLI) when it is given
  on('fs.stat', ($, e) => (opts.existing === undefined || opts.existing.includes(e.path.replace(/\\/g, '/'))
    ? { value: { isFile: false, isDirectory: true, size: 0, mtimeMs: 0 } } : { deny: 'ENOENT' }) as never)
  const registered: string[] = []
  const opened: string[] = []
  on('command.register', ($, e) => {
    registered.push(e.name)
    return { value: { command: e.name } } as never
  })
  on('ui.open', ($, e) => {
    opened.push(e.id)
    return { value: { id: e.id } } as never
  })
  on('ui.status', () => ({ value: undefined }))
  on('ui.toast', ($, e) => {
    toasts.push(e.text)
    return { value: undefined }
  })
  on('process.run', ($, e) => {
    if (e.argv[1] === 'check') return { value: { exitCode: 0, stdout: '{"sites": []}', stderr: '' } } as never // check.test.ts
    runs.push([...e.argv])
    if (e.argv[1] === 'query') return { value: { stderr: '', ...(opts.query ?? { exitCode: 0, stdout: '## a.py:1-3 f\n1 def f(): ...' }) } } as never
    if (e.argv[0] === 'git') {
      const heads = opts.heads ?? ['aaa']
      return { value: { exitCode: 0, stdout: `${heads[Math.min(headCalls++, heads.length - 1)]}\n`, stderr: '' } } as never
    }
    if (e.argv[1] === 'review') {
      const answer = { value: { exitCode: 3, stdout: REVIEW_JSON, stderr: '' } } as never
      return opts.reviewGate ? opts.reviewGate.then(() => answer) : answer
    }
    if (e.argv[1] === 'update') return { value: { exitCode: opts.update?.exitCode ?? 0, stdout: '{}', stderr: 'locked' } } as never
    if (e.argv[1] === '-c' && !isStartChecked) {
      isStartChecked = true // the session-start look at the graph finds it fresh; `graph` answers the watcher
      return { value: { exitCode: 0, stdout: '{"locked": false, "behind": 0}', stderr: '' } } as never
    }
    if (e.argv[1] === '-c') {
      const g = opts.graph ?? [{ locked: false, behind: 0 }]
      return { value: { exitCode: 0, stdout: JSON.stringify(g[Math.min(graphCalls++, g.length - 1)]), stderr: '' } } as never
    }
    return { value: { exitCode: 0, stdout: '{}', stderr: '' } } as never
  })
  on('tool.call', () => ({ result: 'ok' }) as never)
  on('prompt.submit', ($, e) => ({ text: e.text, context: e.context }))
  on('turn.complete', () => ({ text: '' }) as never)
  return { runs, toasts, registered, opened }
}

// A prompt as the person's Enter raises it, and as other senders do.
const ask = (text: string, origin: Record<string, unknown> = { kind: 'composer' }) =>
  ({ text, wait: false, origin }) as never

// The text of a drawing, its elements' strings joined (a Client's drawing is read with `drawn({ in: key })`).
const flat = (n: unknown): string => (typeof n === 'string' ? n
  : ((n as { children?: unknown[] }).children ?? []).map(flat).join(''))

const run = (args = '') => ({ command: '', args } as { command: string; args: string })

describe('pure parts', () => {
  test('code questions are told from other prompts, in English and Turkish', () => {
    for (const q of ['Where is the graph rebuilt?', 'how does update --fast defer the graph build',
      'Wisp.spawn kimler tarafından çağrılıyor?', 'nasıl çalışıyor bu indeks', 'bu fonksiyon test ediliyor mu',
      'Kor Ocağı her tickte ne yapıyor']) {
      expect(looksLikeCodeQuestion(q)).toBe(true)
    }
    for (const p of ['/verinoda-update', '!git status', 'ok', 'fix the typo in README and commit it',
      'Refactor the scanner to use a pool', 'Use the new name everywhere', 'x'.repeat(2_001) + '?']) {
      expect(looksLikeCodeQuestion(p)).toBe(false)
    }
  })

  test('only the person speaks for the person', () => {
    expect(isPersonsPrompt({ kind: 'composer' })).toBe(true)
    expect(isPersonsPrompt({ kind: 'bridge' })).toBe(true)
    expect(isPersonsPrompt({ kind: 'plugin', asUser: true })).toBe(true)
    expect(isPersonsPrompt({ kind: 'plugin' })).toBe(false)
    expect(isPersonsPrompt({ kind: "task-notification" })).toBe(false)
    expect(isPersonsPrompt(undefined)).toBe(false)
  })

  test('modes: arguments, the toggle and stored values', () => {
    expect(modeCommand('', 'off')).toBe('nudge')
    expect(modeCommand('', 'search')).toBe('off')
    expect(modeCommand('on', 'off')).toBe('nudge')
    expect(modeCommand('search', 'off')).toBe('search')
    expect(modeCommand('kapat', 'nudge')).toBe('off')
    expect(modeCommand('sometimes', 'off')).toBeUndefined()
    expect(asMode(true)).toBe('nudge')
    expect(asMode('search')).toBe('search')
    expect(asMode(undefined)).toBe('off')
    expect(asMode('loud')).toBe('off')
  })

  test('filter spellings in a prompt are taken apart before the query', () => {
    expect(plainQuery('How is the file:line evidence computed?')).toBe('How is the file line evidence computed?')
    expect(plainQuery('Is NOT the /cache/ path:x used?')).toBe('Is not the cache path x used?')
    expect(plainQuery('what (calls) symbol:Foo')).toBe('what  calls  symbol Foo')
    expect(plainQuery('plain question')).toBe('plain question')
  })

  test('the blocks name the index and say what they are', () => {
    expect(nudgeBlock(ROOT, 'V.exe')).toContain(`V.exe analyze "<the question>" --repo ${ROOT}`)
    expect(nudgeBlock(ROOT, 'V.exe')).not.toContain('"V.exe"') // unquoted: PowerShell cannot run "path" args
    const block = contextBlock(ROOT, 'x'.repeat(20_000))
    expect(block.startsWith('[Verinoda auto-context]')).toBe(true)
    expect(block.endsWith('[cut]')).toBe(true)
    expect(block.length).toBeLessThan(13_000)
  })
})

describe('auto-context', () => {
  test('off by default: a code question goes through untouched and nothing runs', async ($, on) => {
    mock.clock(on)
    mock.store(on)
    const { runs } = world(on)
    await $.session.start(START)
    const res = await $.prompt.submit(ask(QUESTION))
    expect(res.context ?? []).toEqual([])
    expect(runs.filter(r => r[1] === 'query').length).toBe(0)
  })

  test('nudge: the instruction is attached, nothing runs before the prompt', async ($, on) => {
    mock.clock(on)
    mock.store(on)
    const { runs } = world(on)
    await $.session.start(START)
    await $.command.run({ ...run('nudge'), command: 'verinoda-auto' } as never)
    const res = await $.prompt.submit(ask(QUESTION))
    expect(res.context?.length).toBe(1)
    expect(res.context?.[0]).toContain('start by running')
    expect(runs.filter(r => r[1] === 'query').length).toBe(0)

    const plain = await $.prompt.submit(ask('Refactor the scanner to use a pool'))
    expect(plain.context ?? []).toEqual([])
  })

  test('search: the query results are attached; a failing query attaches nothing', async ($, on) => {
    mock.clock(on)
    mock.store(on)
    const { runs } = world(on)
    await $.session.start(START)
    await $.command.run({ ...run('search'), command: 'verinoda-auto' } as never)
    const res = await $.prompt.submit(ask(QUESTION))
    const query = runs.find(r => r[1] === 'query')
    expect(query?.slice(1)).toEqual(['query', QUESTION, '--repo', ROOT, '--max-chars', '6000'])
    expect(res.context?.[0]).toContain('def f()')
  })

  test('search with a failing query sends the prompt without context', async ($, on) => {
    mock.clock(on)
    mock.store(on)
    world(on, { query: { exitCode: 1, stdout: '' } })
    await $.session.start(START)
    await $.command.run({ ...run('search'), command: 'verinoda-auto' } as never)
    const res = await $.prompt.submit(ask(QUESTION))
    expect(res.context ?? []).toEqual([])
    expect(res.text).toBe(QUESTION)
  })

  test('a session started outside the configured project gets nothing', { options: { root: ROOT } }, async ($, on) => {
    mock.clock(on)
    mock.store(on, { auto: 'nudge' })
    world(on, { cwd: 'C:/work/other-project', existing: [`${ROOT}/.verinoda`] })
    await $.session.start(START)
    expect((await $.prompt.submit(ask(QUESTION))).context ?? []).toEqual([])
  })

  test('a home folder with an index is not taken for the project unless the settings name it', async ($, on) => {
    mock.clock(on)
    mock.store(on, { auto: 'nudge' })
    mock.env(on, { USERPROFILE: String.raw`C:\Users\dev` })
    const { runs } = world(on, { cwd: 'C:/Users/dev', existing: ['C:/Users/dev/.verinoda'] })
    await $.session.start(START)
    await $.tool.call({ tool: 'Edit', file_path: 'C:/Users/dev/notes.txt', old_string: 'a', new_string: 'b' } as never)
    expect(await $.command.run({ ...run(), command: 'verinoda-update' } as never))
      .toEqual(expect.objectContaining({ text: expect.stringContaining('no .verinoda index') }))
    expect(runs.length).toBe(0)
    expect((await $.prompt.submit(ask(QUESTION))).context ?? []).toEqual([])
  })

  test('the project is found from a subfolder; without a .venv the CLI is the one on PATH', async ($, on) => {
    mock.clock(on)
    mock.store(on, { auto: 'search' })
    const { runs } = world(on, { cwd: `${ROOT}/verinoda/benchmark`, existing: [`${ROOT}/.verinoda`] })
    await $.session.start(START)
    await $.prompt.submit(ask(QUESTION))
    const query = runs.find(r => r[1] === 'query')
    expect(query?.[0]).toBe('verinoda')
    expect(query?.slice(-3)).toEqual([ROOT, '--max-chars', '6000'])
  })

  test('only the person is answered: a plugin speaking for itself or a notification gets nothing', async ($, on) => {
    mock.clock(on)
    mock.store(on, { auto: 'nudge' })
    world(on)
    await $.session.start(START)
    expect((await $.prompt.submit(ask(QUESTION, { kind: 'plugin', name: 'other' }))).context ?? []).toEqual([])
    expect((await $.prompt.submit(ask(QUESTION, { kind: 'task-notification' }))).context ?? []).toEqual([])
    expect((await $.prompt.submit(ask(QUESTION, { kind: 'plugin', name: 'other', asUser: true }))).context?.length).toBe(1)
    expect((await $.prompt.submit(ask(QUESTION, { kind: 'bridge' }))).context?.length).toBe(1)
  })

  test('the mode chosen is kept for the next session', async ($, on) => {
    mock.clock(on)
    mock.store(on, { auto: 'search' })
    const { runs } = world(on)
    await $.session.start(START)
    await $.prompt.submit(ask(QUESTION))
    expect(runs.some(r => r[1] === 'query')).toBe(true)
  })
})

describe('review after commits', () => {
  test('only a command that moved HEAD is reviewed, from either shell, and only with the guard on', async ($, on) => {
    const clock = mock.clock(on)
    mock.store(on)
    // HEAD as read before and after each guarded command: unchanged for `git log`, moved by the commit and the amend
    const { runs, toasts } = world(on, { heads: ['aaa', 'aaa', 'aaa', 'bbb', 'bbb', 'ccc'] })
    await $.session.start(START)
    await $.tool.call({ tool: 'Bash', command: 'git commit -m "x"' } as never)
    await clock.settle()
    expect(runs.some(r => r[0] === 'git' || r[1] === 'review')).toBe(false) // guard off: nothing read, nothing run

    await $.command.run({ ...run('on'), command: 'verinoda-guard' } as never)
    await $.tool.call({ tool: 'Bash', command: 'git log --grep=commit --oneline' } as never)
    await clock.settle()
    expect(runs.filter(r => r[1] === 'review').length).toBe(0)

    await $.tool.call({ tool: 'Bash', command: 'git -C . commit -am "fix" 2>&1 | tail -3' } as never)
    await clock.settle()
    const reviews = runs.filter(r => r[1] === 'review')
    expect(reviews.length).toBe(1)
    expect(reviews[0]?.slice(1)).toEqual(['review', '--json', '--repo', ROOT, '--base', 'HEAD~1', '--max-chars', '4000'])
    expect(toasts.at(-1)).toBe('Verinoda review of the commit: risk 41/100 (medium), 2 finding(s) · /verinoda-panel')

    await $.tool.call({ tool: 'PowerShell', command: 'git commit --amend --no-edit' } as never)
    await clock.settle()
    expect(runs.filter(r => r[1] === 'review').length).toBe(2)
  })

  test('a commit that lands while a review runs gets its own review afterwards', async ($, on) => {
    const clock = mock.clock(on)
    mock.store(on, { guard: true })
    let open: () => void = () => undefined
    const gate = new Promise<void>(resolve => { open = resolve })
    const { runs } = world(on, { heads: ['a1', 'b1', 'b1', 'c1'], reviewGate: gate })
    await $.session.start(START)
    await $.tool.call({ tool: 'Bash', command: 'git commit -m a' } as never)
    await $.tool.call({ tool: 'Bash', command: 'git commit -m b' } as never)
    expect(runs.filter(r => r[1] === 'review').length).toBe(1) // the second waits for the first
    open()
    await clock.settle()
    expect(runs.filter(r => r[1] === 'review').length).toBe(2)
  })
})

describe('re-index robustness', () => {
  test('a failed update keeps the files, and /verinoda-update tries again', async ($, on) => {
    mock.clock(on)
    mock.store(on)
    const { runs } = world(on, { update: { exitCode: 1 } })
    await $.session.start(START)
    await $.tool.call({ tool: 'Edit', file_path: `${ROOT}/a.py`, old_string: 'a', new_string: 'b' } as never)
    const first = await $.command.run({ ...run(), command: 'verinoda-update' } as never)
    expect(first).toEqual(expect.objectContaining({ text: expect.stringContaining('update failed (exit 1)') }))
    const second = await $.command.run({ ...run(), command: 'verinoda-update' } as never)
    expect(second).toEqual(expect.objectContaining({ text: expect.stringContaining('update failed (exit 1)') }))
    expect(runs.filter(r => r[1] === 'update').length).toBe(2)
  })

  test('edits made while the graph build runs wait for it, then are indexed without being asked', async ($, on) => {
    const clock = mock.clock(on)
    mock.store(on)
    // the build holds the lock for two polls, then the graph has caught up
    const { runs } = world(on, { graph: [{ locked: true, behind: 1 }, { locked: true, behind: 1 }, { locked: false, behind: 0 }] })
    await $.session.start(START)
    await $.tool.call({ tool: 'Edit', file_path: `${ROOT}/a.py`, old_string: 'a', new_string: 'b' } as never)
    await $.command.run({ ...run(), command: 'verinoda-update' } as never)
    await $.tool.call({ tool: 'Edit', file_path: `${ROOT}/b.py`, old_string: 'a', new_string: 'b' } as never)
    const waiting = await $.command.run({ ...run(), command: 'verinoda-update' } as never)
    expect(waiting).toEqual(expect.objectContaining({ text: 'waiting for the graph build; 1 file will be indexed when it ends' }))
    expect(runs.filter(r => r[1] === 'update').length).toBe(1)
    for (let i = 0; i < 3; i++) await clock.advance(1_000)
    await clock.settle()
    expect(runs.filter(r => r[1] === 'update').length).toBe(2)
  })

  test('a subagent turn does not start a re-index; the main loop turn does', async ($, on) => {
    mock.clock(on)
    mock.store(on)
    const { runs } = world(on)
    await $.session.start(START)
    await $.tool.call({ tool: 'Edit', file_path: `${ROOT}/a.py`, old_string: 'a', new_string: 'b' } as never)
    await $.turn.complete({ turnId: 't1', agentId: 'sub-1', answer: '', durationMs: 1, isAborted: false, reason: 'end_turn' } as never)
    expect(runs.filter(r => r[1] === 'update').length).toBe(0)
    await $.turn.complete({ turnId: 't2', answer: '', durationMs: 1, isAborted: false, reason: 'end_turn' } as never)
    expect(runs.filter(r => r[1] === 'update').length).toBe(1)
  })
})

describe('pane', () => {
  test('/verinoda-panel opens it, and no command is named plain verinoda (a skill of that name wins the slash)', async ($, on) => {
    mock.clock(on)
    mock.store(on)
    const { registered, opened } = world(on)
    await $.session.start(START)
    expect(registered).toContain('verinoda-panel')
    expect(registered).not.toContain('verinoda')
    const res = await $.command.run({ ...run(), command: 'verinoda-panel' } as never)
    expect(res).toEqual(expect.objectContaining({ text: 'Verinoda pane opened.' }))
    expect(opened).toEqual(['verinoda'])
  })

  for (const surface of ['terminal', 'desktop'] as const) {
    test(`draws the state, the settings and the last context on ${surface}, in Turkish`, async ($, on) => {
      const clock = mock.clock(on)
      mock.store(on, { auto: 'nudge' })
      world(on)
      await $.session.start(START)
      await clock.settle()
      await $.prompt.submit(ask(QUESTION))
      const pane = await $.ui.mount({
        plugin: 'verinoda-live', surface, component: 'Pane', requestId: 'verinoda',
        props: { title: 'Verinoda', isFocused: true, bodyColumns: 48, placement: 'dock', scroll: { offset: 0, bodyRows: 40 } } as never,
      })
      expect(await pane.find({ text: /✓ Index güncel/ })).toBeDefined()
      expect((await pane.find({ key: 'auto-nudge' }))?.props.label).toBe('● Yönlendir')
      expect((await pane.find({ key: 'auto-off' }))?.props.label).toBe('Kapalı')
      expect(await pane.find({ text: /Önerilen · Claude önce analyze/ })).toBeDefined()
      expect(await pane.find({ text: /yönlendirildi · "Where is the graph rebuilt/ })).toBeDefined()
      expect(await pane.find({ key: 'update' })).toBeUndefined() // nothing to update: no button that does nothing
      await $.ui.press({ plugin: 'verinoda-live', key: 'auto-off' })
      expect((await pane.find({ key: 'auto-off' }))?.props.label).toBe('● Kapalı')
      expect((await $.prompt.submit(ask(QUESTION))).context ?? []).toEqual([])
    })
  }

  test('the mascot sits in the room left over, and only there', async ($, on) => {
    const clock = mock.clock(on)
    mock.store(on)
    world(on)
    await $.session.start(START)
    await clock.settle() // the session-start look at the graph answers
    // one drawing at a time: mount at a size, read it, unmount
    const hasMascot = async (bodyRows: number, bodyColumns = 48, placement = 'dock') => {
      const pane = await $.ui.mount({
        plugin: 'verinoda-live', surface: 'terminal', component: 'Pane', requestId: 'verinoda',
        props: { title: 'Verinoda', isFocused: false, bodyColumns, placement, scroll: { offset: 0, bodyRows } } as never,
      })
      const isDrawn = (await pane.find({ key: 'mascot-client' })) !== undefined
      const found = {
        mascot: (await pane.find({ key: 'mascot' })) !== undefined,
        // the caption is the animated Client's: read from its own drawing
        caption: isDrawn && flat(await pane.drawn({ in: 'mascot-client' })).includes('Her şey güncel'),
        hint: (await pane.find({ text: /ctrl\+x tab/ })) !== undefined,
      }
      await pane.unmount()
      return found
    }
    expect(await hasMascot(40)).toEqual({ mascot: true, caption: true, hint: true }) // not focused: how to get the keys
    expect((await hasMascot(14)).mascot).toBe(false) // too short: hidden, never cropped
    expect((await hasMascot(40, 22)).mascot).toBe(false) // too narrow
    expect((await hasMascot(40, 96, 'inline')).mascot).toBe(false) // inline: never
  })

  test('a failed update shows what failed and a retry, with the mascot saying so', async ($, on) => {
    const clock = mock.clock(on)
    mock.store(on)
    world(on, { update: { exitCode: 1 } })
    await $.session.start(START)
    await clock.settle()
    await $.tool.call({ tool: 'Edit', file_path: `${ROOT}/a.py`, old_string: 'a', new_string: 'b' } as never)
    await $.command.run({ ...run(), command: 'verinoda-update' } as never)
    const pane = await $.ui.mount({
      plugin: 'verinoda-live', surface: 'terminal', component: 'Pane', requestId: 'verinoda',
      props: { title: 'Verinoda', isFocused: true, bodyColumns: 48, placement: 'dock', scroll: { offset: 0, bodyRows: 40 } } as never,
    })
    expect(await pane.find({ text: /✗ Güncelleme başarısız · 1 dosya bekletiliyor/ })).toBeDefined()
    expect(await pane.find({ text: /çıkış 1: locked/ })).toBeDefined()
    expect((await pane.find({ key: 'update' }))?.props.label).toBe('Tekrar dene')
    expect(flat(await pane.drawn({ in: 'mascot-client' }))).toContain('Bir şey ters gitti')
    expect(await pane.find({ text: /^u güncelle · 1-3 bağlam · r inceleme · k kontrol$/ })).toBeDefined()
  })
})
