import { describe, expect, mock, test } from 'claude-code/testing'
import type { On } from 'claude-code'

import { captionAt, frameRows, MASCOT_HEIGHT, MASCOT_WIDTH, TICK_MS } from './mascot'

const ROOT = 'C:/work/verinoda'
const text = (row: ReadonlyArray<readonly [string, string | null]>) => row.map(([t]) => t).join('')
const colours = (rows: ReturnType<typeof frameRows>) => rows.map(r => r.map(([t, c]) => `${t}:${c}`).join('|')).join('\n')

describe('frames', () => {
  test('every frame of every mood is 8 rows of exactly 20 cells', () => {
    for (const mood of ['idle', 'busy', 'warn', 'err'] as const) {
      for (let t = 0; t < 10_000; t += TICK_MS) {
        const rows = frameRows(t, mood)
        expect(rows.length).toBe(MASCOT_HEIGHT)
        for (const row of rows) expect([...text(row)].length).toBe(MASCOT_WIDTH)
      }
    }
  })

  test('it blinks: once a cycle the lenses close, and they open again', () => {
    const eyes = (t: number) => colours(frameRows(t, 'idle').slice(2, 4)) // the glasses rows only
    expect(eyes(4_100)).not.toBe(eyes(1_000))
    expect(eyes(4_100)).not.toContain('#1e1b4b') // no dark lens cell while the eyes are shut
    expect(eyes(4_200 + 1_000)).toBe(eyes(1_000)) // the next cycle, same moment: open again
  })

  test('busy: the glint sweeps across the lenses, feet and arms quicker than idle', () => {
    const glintAt = (t: number) => frameRows(t, 'busy')[2]!.findIndex(([, c]) => c === '#f5f3ff')
    const spots = new Set([0, 360, 720, 1_080, 1_440].map(glintAt))
    expect(spots.size).toBe(5)
    const feet = (t: number, mood: 'idle' | 'busy') => text(frameRows(t, mood)[7]!)
    expect(feet(0, 'busy')).not.toBe(feet(240, 'busy'))
    expect(feet(0, 'idle')).toBe(feet(240, 'idle')) // idle shuffles slowly
    expect(feet(0, 'idle')).not.toBe(feet(840, 'idle'))
    expect(text(frameRows(240, 'busy')[5]!)).toContain('▝█') // arms up mid-step
  })

  test('the busy caption counts its dots; other captions stay as given', () => {
    expect(captionAt(0, 'busy', 'Kodu okuyorum…')).toBe('Kodu okuyorum.')
    expect(captionAt(400, 'busy', 'Kodu okuyorum…')).toBe('Kodu okuyorum..')
    expect(captionAt(800, 'busy', 'Kodu okuyorum…')).toBe('Kodu okuyorum...')
    expect(captionAt(800, 'idle', 'Her şey güncel')).toBe('Her şey güncel')
  })
})

function world(on: On) {
  on('session.start', () => ({ cwd: ROOT }))
  on('session.cwd', () => ({ value: ROOT }))
  on('fs.stat', () => ({ value: { isFile: false, isDirectory: true, size: 0, mtimeMs: 0 } }) as never)
  on('command.register', ($, e) => ({ value: { command: e.name } }) as never)
  on('ui.status', () => ({ value: undefined }))
  on('process.run', () => ({ value: { exitCode: 0, stdout: '{"locked": false, "behind": 0}', stderr: '' } }) as never)
}

const flat = (n: unknown): string => (typeof n === 'string' ? n
  : ((n as { children?: unknown[] }).children ?? []).map(flat).join(''))

describe('in the pane', () => {
  test('on the terminal the surface animates it: frames change as its clock runs, the pane is not redrawn', async ($, on) => {
    const clock = mock.clock(on)
    mock.store(on)
    world(on)
    await $.session.start({ cwd: ROOT, surface: 'terminal', isInteractive: true } as never)
    await clock.settle()
    const pane = await $.ui.mount({
      plugin: 'verinoda-live', surface: 'terminal', component: 'Pane', requestId: 'verinoda',
      props: { title: 'Verinoda', isFocused: false, bodyColumns: 48, placement: 'dock', scroll: { offset: 0, bodyRows: 40 } } as never,
    })
    expect(await pane.find({ key: 'mascot-client' })).toBeDefined()
    const at0 = JSON.stringify(await pane.drawn({ in: 'mascot-client' }))
    expect(flat(await pane.drawn({ in: 'mascot-client' }))).toContain('Her şey güncel')
    await pane.advance(840) // a step of the feet
    const at840 = JSON.stringify(await pane.drawn({ in: 'mascot-client' }))
    expect(at840).not.toBe(at0)
    await pane.advance(4_100 - 840) // into the blink
    expect(JSON.stringify(await pane.drawn({ in: 'mascot-client' }))).not.toContain('#1e1b4b')
  })

  test('where the surface has no Client the mascot is drawn still', async ($, on) => {
    const clock = mock.clock(on)
    mock.store(on)
    world(on)
    await $.session.start({ cwd: ROOT, surface: 'vscode', isInteractive: true } as never)
    await clock.settle()
    const pane = await $.ui.mount({
      plugin: 'verinoda-live', surface: 'vscode', component: 'Pane', requestId: 'verinoda',
      props: { title: 'Verinoda', isFocused: false, bodyColumns: 48, placement: 'dock', scroll: { offset: 0, bodyRows: 40 } } as never,
    })
    expect(await pane.find({ key: 'mascot' })).toBeDefined()
    expect(await pane.find({ key: 'mascot-client' })).toBeUndefined()
    expect(await pane.find({ text: /Her şey güncel/ })).toBeDefined()
  })
})
