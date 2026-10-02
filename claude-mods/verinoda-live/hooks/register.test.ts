import { describe, expect, mock, test } from 'claude-code/testing'
import type { On } from 'claude-code'

const ROOT = 'C:/work/verinoda'
const EDIT = { tool: 'Edit', file_path: 'C:\\work\\verinoda\\src\\a.py', old_string: 'a', new_string: 'b' } as const

// The world beneath the plugin: a repo with an index, a CLI that records its runs, and a graph whose
// state answers the mod's polls from `graph` in order (the last one repeats); 'fails': the check exits 1.
type GraphState = { locked: boolean; behind: number }
function world(on: On, graph: GraphState[] | 'fails' = [{ locked: false, behind: 0 }]) {
  const runs: string[][] = []
  const polls: string[][] = []
  const statuses: (string | undefined)[] = []
  on('session.start', () => ({ cwd: ROOT }))
  on('session.cwd', () => ({ value: ROOT }))
  mock.store(on)
  on('fs.stat', () => ({ value: { isFile: false, isDirectory: true, size: 0, mtimeMs: 0 } }) as never)
  on('command.register', () => ({ value: {} }) as never)
  on('ui.status', ($, e) => {
    statuses.push(e.text)
    return { value: undefined }
  })
  let isStartChecked = false
  on('process.run', ($, e) => {
    // the session-start look at the graph finds it fresh; `graph` answers the watcher's polls after it
    if (e.argv[1] === '-c' && !isStartChecked) {
      isStartChecked = true
      return { value: { exitCode: 0, stdout: '{"locked": false, "behind": 0}', stderr: '' } } as never
    }
    if (e.argv[1] === '-c') {
      polls.push([...e.argv])
      if (graph === 'fails') return { value: { exitCode: 1, stdout: '', stderr: 'boom' } } as never
      const state = graph[Math.min(polls.length - 1, graph.length - 1)]
      return { value: { exitCode: 0, stdout: JSON.stringify(state), stderr: '' } } as never
    }
    if (e.argv[1] === 'check') return { value: { exitCode: 0, stdout: '{"sites": []}', stderr: '' } } as never // check.test.ts
    runs.push([...e.argv])
    return { value: { exitCode: 0, stdout: '{}', stderr: '' } } as never
  })
  on('tool.call', () => ({ result: 'ok' }) as never)
  return { runs, polls, statuses }
}

const START = { cwd: ROOT, surface: 'terminal', isInteractive: true } as never
const UPDATE = { command: 'verinoda-update', args: '' } as never

describe('verinoda-live', () => {
  test('an edit inside the repo is re-indexed by /verinoda-update, the graph then pending', async ($, on) => {
    mock.clock(on)
    const { runs, statuses } = world(on)
    await $.session.start(START)
    await $.tool.call(EDIT)

    expect(statuses).toContain('Verinoda: 1 file pending')
    const result = await $.command.run(UPDATE)

    expect(runs.length).toBe(1)
    expect(runs[0]?.slice(1)).toEqual(['update', '--fast', '--repo', ROOT])
    expect(result).toEqual(expect.objectContaining({
      text: 'text index updated (1 file); the graph is rebuilt in the background',
    }))
    expect(statuses.at(-1)).toBe('Verinoda: text fresh · graph pending…')
  })

  test('the line turns fresh only once the graph has caught up', async ($, on) => {
    const clock = mock.clock(on)
    // the build has not taken the lock yet, then holds it, then is done
    const { polls, statuses } = world(on, [
      { locked: false, behind: 1 },
      { locked: true, behind: 1 },
      { locked: false, behind: 0 },
    ])
    await $.session.start(START)
    await $.tool.call(EDIT)
    await $.command.run(UPDATE)

    await clock.advance(1_000)
    await clock.advance(1_000)
    expect(polls.length).toBe(2)
    expect(statuses.at(-1)).toBe('Verinoda: text fresh · graph pending…')
    expect(await $.command.run(UPDATE)).toEqual(expect.objectContaining({
      text: 'text index fresh; the graph is still being rebuilt',
    }))

    await clock.advance(1_000)
    expect(polls.length).toBe(3)
    expect(polls[0]?.[0]).toBe(`${ROOT}/.venv/Scripts/python.exe`)
    // run in the repo, `-c` would import the repo's own verinoda/ when the repo is Verinoda's source
    expect(polls[0]?.[2]).toMatch(/^import sys; sys\.path\[:\] = \[p for p in sys\.path if p\]; /)
    expect(statuses.at(-1)).toBe('Verinoda: fresh ✓')
    expect(await $.command.run(UPDATE)).toEqual(expect.objectContaining({ text: 'index already fresh' }))
  })

  test('a failing graph check says so instead of claiming fresh', async ($, on) => {
    const clock = mock.clock(on)
    const { statuses } = world(on, 'fails')
    await $.session.start(START)
    await $.tool.call(EDIT)
    await $.command.run(UPDATE)
    await clock.advance(1_000)

    expect(statuses.at(-1)).toBe('Verinoda: graph state unknown')
  })

  test('a graph left behind with no build running is named, and /verinoda-update takes it in', async ($, on) => {
    const clock = mock.clock(on)
    // files changed outside Claude's edit tools: never locked, always behind
    const { runs, statuses } = world(on, [{ locked: false, behind: 2 }])
    await $.session.start(START)
    await $.tool.call(EDIT)
    await $.command.run(UPDATE)

    for (let i = 0; i < 15; i++) await clock.advance(1_000)
    expect(statuses.at(-1)).toBe('Verinoda: text fresh · graph pending…')
    await clock.advance(1_000)
    expect(statuses.at(-1)).toBe('Verinoda: graph behind 2 files · /verinoda-update')

    await $.command.run(UPDATE)
    expect(runs.length).toBe(2)
    expect(statuses).toContain('Verinoda: updating (2 files)…')
  })

  test('edits outside the repo or inside .verinoda are ignored', async ($, on) => {
    mock.clock(on)
    const { runs } = world(on)
    await $.session.start(START)
    await $.tool.call({ tool: 'Write', file_path: 'C:/work/b.py', content: 'x' })
    await $.tool.call({ tool: 'Write', file_path: 'C:/work/verinoda/.verinoda/config.json', content: '{}' })
    const result = await $.command.run(UPDATE)

    expect(runs.length).toBe(0)
    expect(result).toEqual(expect.objectContaining({ text: 'index already fresh' }))
  })
})
