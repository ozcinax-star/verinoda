import { describe, expect, mock, test } from 'claude-code/testing'
import type { Engine } from 'claude-code/testing'
import type { On } from 'claude-code'
import type { ImproveGroup, ImproveState } from '../types'
import {
  addOwn, askReport, countsLine, decide, firstSight, initialImprove, listItems, OFFER_PROMPT, propose,
  PROPOSE_DESCRIPTION, PROPOSE_TOOL, removeOwn, report, REPORT_DESCRIPTION, reportPrompt, REPORT_TOOL,
  reviewPrompt, rowLabel, sendSelection, selectionPrompt, startReview, stopWaiting, submissionFailed,
  turnEnded, turnStarted,
} from './improve'

const ROOT = 'C:/work/proj'
const START = { cwd: ROOT, surface: 'terminal', isInteractive: true } as never
const COMPOSE = { model: 'm', promptModel: 'm', surfaces: ['terminal'], tools: [], outputStyle: null, traits: [] } as never
const run = (args = '') => ({ command: 'verinoda-improve', args }) as never
const item = (id: string, group: ImproveGroup = 'problem') => ({
  id, group, title: `title ${id}`, observation: `observation ${id}`, why: `why ${id}`, change: `change ${id}`,
})
const three = () => [item('i1'), item('i2', 'improvement'), item('i3', 'taste')]
const proposed = (items = three(), subject = 'the screen') => propose(initialImprove(), { subject, items }).state
const outcome = (id: string, status = 'applied', note = 'Done, checked.') => ({ id, status, note })
const toolResult = (out: unknown) => String((out as { result?: unknown }).result)
const callPropose = ($: Engine, items = three(), subject = 'the screen') => $.tool.call({ tool: PROPOSE_TOOL, subject, items } as never)
const callReport = ($: Engine, outcomes: unknown[], extra_changes?: unknown[]) => $.tool.call({ tool: REPORT_TOOL, outcomes, extra_changes } as never)
const press = ($: Engine, key: string) => $.ui.press({ plugin: 'verinoda-live', key })
const input = ($: Engine, text: string, kind: 'change' | 'submit' = 'submit') => $.ui.input({ plugin: 'verinoda-live', key: 'own', text, kind })
const mount = ($: Engine, surface: 'terminal' | 'desktop' | 'mobile' = 'terminal', bodyColumns = 48, isFocused = true) => $.ui.mount({
  plugin: 'verinoda-live', surface, component: 'Pane', requestId: 'verinoda-improve',
  props: { title: 'İyileştirme', isFocused, bodyColumns, placement: 'dock', scroll: { offset: 0, bodyRows: 40 } } as never,
})
const end = ($: Engine, turnId: string, agentId?: string) => $.turn.complete({ turnId, agentId, answer: '', durationMs: 1, reason: 'answer', isAborted: false } as never)
const observed = new Map<Engine, () => ImproveState>()
const state = async ($: Engine) => observed.get($)!()

function world(on: On, opts: { indexed?: boolean; placed?: boolean } = {}) {
  const w = {
    registered: [] as string[], commands: [] as string[], opened: [] as string[], runs: [] as string[][],
    prompts: [] as { text: string; origin: unknown; context: readonly string[] }[],
    failure: '' as '' | 'drop' | 'reject', hold: undefined as Promise<void> | undefined,
    improve: initialImprove(), writes: 0,
  }
  on('state.set', { plugin: 'verinoda-live', key: 'improve' }, async ($, e, next) => {
    const out = await next(e)
    if (out.value?.isSet) { w.improve = e.value; w.writes++ }
    return out
  })
  on('session.start', () => ({ cwd: ROOT }))
  on('session.cwd', () => ({ value: ROOT }))
  on('fs.stat', () => opts.indexed
    ? { value: { isFile: false, isDirectory: true, size: 0, mtimeMs: 0 } } as never : { deny: 'ENOENT' })
  on('command.register', ($, e) => {
    w.commands.push(e.name)
    return { value: { command: e.name } } as never
  })
  on('tool.register', ($, e) => {
    w.registered.push(e.name)
    return { value: { tool: `mcp__verinoda-live__${e.name}` } } as never
  })
  on('ui.open', ($, e) => {
    w.opened.push(e.id)
    return { value: opts.placed === false ? { isPlaced: false, reason: 'narrow' } : { isPlaced: true } } as never
  })
  on('ui.status', () => ({ value: undefined }))
  on('process.run', ($, e) => {
    w.runs.push([...e.argv])
    const stdout = e.argv[1] === '-c' ? '{"locked":false,"behind":0}' : e.argv[1] === 'check' ? '{"sites":[]}'
      : e.argv[1] === 'locate' ? '{"text":"located src/parser.ts","files":[{"path":"src/parser.ts"}]}' : '{}'
    return { value: { exitCode: 0, stdout, stderr: '' } } as never
  })
  on('tool.call', () => ({ result: 'ok' }) as never)
  on('prompt.submit', async ($, e) => {
    w.prompts.push({ text: e.text, origin: e.origin, context: e.context ?? [] })
    const failure = w.failure
    if (w.hold) await w.hold
    if (failure === 'reject') throw new Error('submit failed')
    return failure === 'drop' ? { drop: 'test dropped this prompt' } : { text: e.text, context: e.context }
  })
  on('prompt.compose', () => ({ sections: [{ id: 'intro', text: 'You are an agent.', scope: 'shared' }] }) as never)
  on('turn.start', ($, e) => ({ turnId: e.turnId }))
  on('turn.complete', () => ({ text: '' }))
  on('session.end', ($, e) => ({ sessionId: e.sessionId }))
  return w
}

async function setup($: Engine, on: On, opts: { indexed?: boolean; placed?: boolean; interactive?: boolean } = {}) {
  const clock = mock.clock(on)
  mock.store(on)
  const w = world(on, opts)
  observed.set($, () => w.improve)
  await $.session.start({ cwd: ROOT, surface: 'terminal', isInteractive: opts.interactive !== false } as never)
  await clock.settle()
  return { w, clock }
}

describe('improve: list and decisions', () => {
  test('a proposal keeps its ranking, bounds every text, and starts undecided', () => {
    const s = propose(initialImprove(), { subject: ' s '.repeat(50), items: [
      { ...item('z', 'taste'), title: ' ' + 't'.repeat(140), observation: 'o'.repeat(700), why: ' w ',
        change: 'c'.repeat(700), cost: 'k'.repeat(700), evidence: Array.from({ length: 8 }, () => 'e'.repeat(200)), seen: true }, item('a'),
    ] }).state
    expect(s.items.map(i => i.id)).toEqual(['z', 'a'])
    expect(s.subject.length).toBe(80)
    expect(s.items[0]?.title.length).toBe(120)
    expect(s.items[0]?.title.endsWith('…')).toBe(true)
    expect(s.items[0]?.observation.length).toBe(600)
    expect(s.items[0]?.why).toBe('w')
    expect(s.items[0]?.change.length).toBe(600)
    expect(s.items[0]?.cost.length).toBe(600)
    expect(s.items[0]?.evidence.length).toBe(6)
    expect(s.items[0]?.evidence[0]?.length).toBe(160)
    expect(s.items.every(i => i.decision === 'none' && i.outcome === null && !i.verified)).toBe(true)
    expect(propose(startReview(initialImprove(), 'a file'), { subject: ' ', items: three() }).state.subject).toBe('a file')
    expect(propose(initialImprove(), { items: three() }).state.subject).toBe('bu konuşma')
  })

  test('invalid proposals are refused whole, naming the offending item', () => {
    const s = decide(proposed(), 'i1', 'keep')
    const cases: [unknown, string][] = [
      [{}, 'items'], [{ items: [] }, 'items'], [{ items: Array.from({ length: 31 }, (_, n) => item(`i${n}`)) }, 'items'],
      [{ items: [null] }, 'item 1'], [{ items: [item('i1'), item('i1')] }, 'i1'],
      [{ items: [item('bad id')] }, 'bad id'], [{ items: [item('u1')] }, 'u1'],
      [{ items: [item('abcdefghijklmnopq')] }, 'abcdefghijklmnopq'], [{ items: [item('')] }, 'position 1'],
      [{ items: [{ ...item('i9'), group: 'other' }] }, 'i9'],
      ...['title', 'observation', 'why', 'change'].map(key => [{ items: [{ ...item('i9'), [key]: ' ' }] }, 'i9'] as [unknown, string]),
    ]
    for (const [input, name] of cases) {
      const out = propose(s, input)
      expect(out.state).toBe(s)
      expect(out.error).toContain(name)
      expect(out.error).toContain('nothing was shown')
    }
  })

  test('malformed optional fields are absent; non-string evidence entries are dropped', () => {
    const s = propose(initialImprove(), { items: [
      { ...item('i1'), cost: 7, evidence: 'file:1', seen: 'true' },
      { ...item('i2'), evidence: [' file:2 ', 7, '', null, ' file:3 '] },
    ] }).state
    expect(s.items[0]?.cost).toBe('')
    expect(s.items[0]?.evidence).toEqual([])
    expect(s.items[0]?.seen).toBe(false)
    expect(s.items[1]?.evidence).toEqual(['file:2', 'file:3'])
  })

  test('seven ranked items show first; decisions, verification, open details and unfolding add to them', () => {
    let s = proposed(Array.from({ length: 12 }, (_, n) => item(`i${n + 1}`, n % 2 ? 'taste' : 'problem')))
    expect(firstSight(s).visible.map(i => i.id)).toEqual(['i1', 'i2', 'i3', 'i4', 'i5', 'i6', 'i7'])
    expect(firstSight(s).hidden).toEqual({ problem: 2, taste: 3, improvement: 0, own: 0 })
    s = decide(s, 'i10', 'keep')
    s = { ...s, items: s.items.map(i => i.id === 'i11' ? { ...i, verified: true } : i), open: 'i12' }
    expect(firstSight(s).visible.map(i => i.id)).toEqual(['i1', 'i2', 'i3', 'i4', 'i5', 'i6', 'i7', 'i10', 'i11', 'i12'])
    expect(firstSight(s).hidden.problem).toBe(1)
    expect(firstSight(s).hidden.taste).toBe(1)
    expect(firstSight({ ...s, unfolded: ['taste'] }).hidden.taste).toBe(0)
  })

  test('decisions toggle; only an unverified problem can be checked; own ids are never reused', () => {
    let s = proposed()
    for (const d of ['apply', 'keep', 'check'] as const) {
      s = decide(s, 'i1', d)
      expect(s.items[0]?.decision).toBe(d)
      s = decide(s, 'i1', d)
      expect(s.items[0]?.decision).toBe('none')
    }
    expect(decide(s, 'i2', 'check').items[1]?.decision).toBe('none')
    const verified = { ...s, items: s.items.map(i => ({ ...i, verified: true })) }
    expect(decide(verified, 'i1', 'check').items[0]?.decision).toBe('none')
    s = addOwn(s, ' mine ')
    expect(s.items.at(-1)?.decision).toBe('apply')
    s = addOwn(removeOwn(s, 'u1'), 'next')
    expect(s.items.at(-1)?.id).toBe('u2')
    expect(countsLine(s)).toBe('4 madde · 1 uygula')
  })

  test('the selection names only explicit choices, with own items and no already sent item', () => {
    let s = proposed([...three(), item('i4')])
    s = addOwn(decide(decide(decide(s, 'i1', 'check'), 'i2', 'apply'), 'i3', 'keep'), 'my addition')
    const text = selectionPrompt(s)
    expect(text).toContain('Apply these:\n- [i2] title i2: change i2')
    expect(text).toContain('[u1] my addition (my own item)')
    expect(text).toContain('Check these first (investigate only; change nothing for them):\n- [i1]')
    expect(text).toContain('Keep these as they are (do not change them, also not as a side effect):\n- [i3]')
    expect(text).not.toContain('[i4]')
    expect(text).toContain('extra_changes')
    const sent = sendSelection(s)
    const next = selectionPrompt(decide({ ...sent, phase: 'choosing' }, 'i4', 'apply'))
    expect(next).not.toContain('[i1]')
    expect(next).not.toContain('[i2]')
    expect(next).not.toContain('[u1]')
    expect(next).not.toContain('Check these first')
    expect(next).toContain('[i4]')
    expect(selectionPrompt(proposed())).not.toContain('Apply these:')
  })
})

describe('improve: reports and queued turns', () => {
  test('all five report outcomes, evidence, confirmed fixes and extra changes', () => {
    let s = proposed(Array.from({ length: 5 }, (_, n) => item(`i${n + 1}`)))
    for (const id of ['i1', 'i2', 'i3']) s = decide(s, id, 'apply')
    for (const id of ['i4', 'i5']) s = decide(s, id, 'check')
    s = sendSelection(s)
    const out = report(s, { outcomes: [outcome('i1'), outcome('i2', 'partial'), outcome('i3', 'failed'),
      { ...outcome('i4', 'confirmed', 'Reproduced.'), change: 'The verified fix', evidence: ['file:7', 3] }, outcome('i5', 'not_confirmed')],
      extra_changes: [' helper renamed ', '', false, ...Array.from({ length: 30 }, () => 'e'.repeat(400))] })
    expect(out.message).toBe('Recorded: 5 outcomes.')
    expect(out.state.phase).toBe('choosing')
    expect(out.state.items.map(i => i.outcome?.status ?? null)).toEqual(['applied', 'partial', 'failed', null, 'not_confirmed'])
    const checked = out.state.items[3]!
    expect(checked.verified).toBe(true)
    expect(checked.decision).toBe('none')
    expect(checked.sentAs).toBe(null)
    expect(checked.checked).toBe('Reproduced.')
    expect(checked.change).toBe('The verified fix')
    expect(checked.evidence).toEqual(['file:7'])
    expect(rowLabel(checked)).toContain('(doğrulandı)')
    expect(listItems(out.state).map(i => i.id)).toEqual(['i4'])
    expect(out.state.extra.length).toBe(20)
    expect(out.state.extra[0]).toBe('helper renamed')
    expect(out.state.extra[1]?.length).toBe(300)
    expect(report(out.state, { outcomes: [outcome('i4', 'confirmed')] }).state).toBe(out.state)
  })

  test('invalid reports record nothing, including a valid outcome before an invalid one', () => {
    const s = sendSelection(decide(decide(proposed(), 'i1', 'apply'), 'i2', 'apply'))
    const invalid = [[], [null], [outcome('missing')], [outcome('i1'), outcome('i1')], [outcome('i1', 'unknown')],
      [outcome('i1', 'confirmed')], [outcome('i1', 'not_confirmed')], [outcome('i1', 'applied', ' ')], [outcome('i3')],
      [outcome('i1'), outcome('missing')]]
    for (const outcomes of invalid) {
      const out = report(s, { outcomes, extra_changes: ['must not be kept'] })
      expect(out.state).toBe(s)
      expect(out.message).toContain('verinoda improve:')
    }
    const final = report(s, { outcomes: [outcome('i1')] }).state
    expect(report(final, { outcomes: [outcome('i1')] }).state).toBe(final)
    const check = sendSelection(decide(proposed(), 'i1', 'check'))
    for (const status of ['applied', 'partial', 'failed']) expect(report(check, { outcomes: [outcome('i1', status)] }).state).toBe(check)
    const optional = report(check, { outcomes: [{ ...outcome('i1', 'confirmed'), change: 3, evidence: 'none' }] }).state
    expect(optional.items[0]?.change).toBe('change i1')
    expect(optional.items[0]?.evidence).toEqual([])
  })

  test('only the newest submitted prompt owns a turn; missing reports remain reportable', () => {
    let s = startReview(initialImprove(), '')
    s = turnStarted(s, reviewPrompt(''), 'review')
    s = propose(s, { items: three() }).state
    const chosen = decide(s, 'i1', 'apply')
    s = sendSelection(chosen)
    expect(s.turn).toBe(null)
    expect(turnEnded(s, 'review')).toBe(s)
    expect(turnEnded(s, 'other')).toBe(s)
    s = turnStarted(s, '  ' + selectionPrompt(chosen), 'selection')
    expect(turnEnded(s, 'selection', 'child')).toBe(s)
    s = turnEnded(s, 'selection')
    expect(s.items[0]?.outcome?.status).toBe('unreported')
    expect(s.phase).toBe('choosing')
    expect(report(s, { outcomes: [outcome('i1')] }).state.items[0]?.outcome?.status).toBe('applied')
  })

  test('failed submissions restore decisions; a late failure cannot overwrite a newer request or a cleared session', () => {
    const review = startReview(initialImprove(), 'first')
    const newer = startReview(review, 'second')
    expect(submissionFailed(newer, review)).toBe(newer)
    expect(submissionFailed(review, review).phase).toBe('idle')
    const s = sendSelection(decide(proposed(), 'i1', 'apply'))
    const restored = submissionFailed(s, s)
    expect(restored.items[0]?.decision).toBe('apply')
    expect(restored.items[0]?.sentAs).toBe(null)
    expect(restored.items[0]?.outcome).toBe(null)
    expect(restored.note).toBe('not-sent')
    const request = askReport(stopWaiting(s))
    expect(submissionFailed(request, request).items[0]?.outcome?.status).toBe('unreported')
    const cleared = { ...initialImprove(), requestSeq: s.requestSeq + 1 }
    expect(submissionFailed(cleared, s)).toBe(cleared)
  })
})

describe('improve: activation and prompts', () => {
  test('ordinary turns leave an inactive improvement state untouched', async ($, on) => {
    const { w } = await setup($, on)
    await $.turn.start({ text: 'a regular task', turnId: 'ordinary' })
    await end($, 'ordinary')
    expect(w.writes).toBe(0)
    expect(w.registered).toEqual([])
    expect(w.prompts).toEqual([])
    expect(w.opened).toEqual([])
  })

  test('a scripted session has no tools, offer, pane or submitted prompt', { options: { improveOffer: true } }, async ($, on) => {
    const { w } = await setup($, on, { interactive: false })
    expect((await $.command.run(run())).text).toContain('needs a person')
    expect(w.registered).toEqual([])
    expect(w.opened).toEqual([])
    expect(w.prompts).toEqual([])
    expect((await $.prompt.compose(COMPOSE)).sections.some(s => s.id === 'verinoda-live:improve')).toBe(false)
    expect(toolResult(await callPropose($))).toContain('nobody is at the keyboard')
    expect(toolResult(await callReport($, [outcome('i1')]))).toContain('nobody is at the keyboard')
    expect((await state($)).phase).toBe('idle')
    expect(w.writes).toBe(0)
  })

  test('the command registers tools and opens a review without an index; a reload keeps it', async ($, on) => {
    const { w, clock } = await setup($, on)
    expect(w.registered).toEqual([])
    expect((await $.prompt.compose(COMPOSE)).sections.map(s => s.id)).toEqual(['intro'])
    expect(w.commands).toContain('verinoda-improve')
    expect((await $.command.run(run(' @src/parser.ts '))).text).toContain('nothing is changed until you choose')
    await clock.settle()
    expect(w.registered).toEqual(['improve_propose', 'improve_report'])
    expect(w.opened).toEqual(['verinoda-improve'])
    expect(w.prompts[0]?.text).toBe(reviewPrompt('@src/parser.ts'))
    expect(w.prompts[0]?.origin).toEqual({ kind: 'plugin', name: 'verinoda-live', asUser: true })
    expect(w.runs).toEqual([])
    const before = await state($)
    await $.session.start(START)
    expect(w.registered).toEqual(['improve_propose', 'improve_report', 'improve_propose', 'improve_report'])
    expect(await state($)).toEqual(before)
    for (const [tool, description] of [[PROPOSE_TOOL, PROPOSE_DESCRIPTION], [REPORT_TOOL, REPORT_DESCRIPTION]]) {
      const got = await $.tool.describe({ tool, description: 'old', isDeferred: true } as never)
      expect(got.isDeferred).toBe(false)
      expect(got.description).toBe(description)
    }
  })

  test('the optional offer registers tools and a session section, even without an index', { options: { improveOffer: true } }, async ($, on) => {
    const { w } = await setup($, on)
    expect(w.registered).toEqual(['improve_propose', 'improve_report'])
    expect((await $.prompt.compose(COMPOSE)).sections).toEqual([
      { id: 'intro', text: 'You are an agent.', scope: 'shared' },
      { id: 'verinoda-live:improve', text: OFFER_PROMPT, scope: 'session' },
    ])
    expect(w.opened).toEqual([])
    expect(w.prompts).toEqual([])
    expect(w.runs).toEqual([])
  })

  test('all three own prompts bypass nudge and the strict assist task gate', { options: { auto: 'nudge', assist: 'strict' } }, async ($, on) => {
    const { w, clock } = await setup($, on, { indexed: true })
    await $.command.run(run('where is the parser slow?'))
    await clock.settle()
    const grep = { tool: 'Grep', pattern: 'parser' } as never
    expect((await $.tool.call(grep) as { deny?: string }).deny).toBeUndefined()
    await callPropose($)
    await mount($)
    await press($, 'item-i1')
    await press($, 'act-apply')
    await press($, 'send')
    await clock.settle()
    expect((await $.tool.call(grep) as { deny?: string }).deny).toBeUndefined()
    await press($, 'stop-waiting')
    await press($, 'ask-report')
    await clock.settle()
    expect((await $.tool.call(grep) as { deny?: string }).deny).toBeUndefined()
    expect(w.prompts.length).toBe(3)
    for (const p of w.prompts) {
      expect(p.context).toEqual([])
      expect(p.origin).toEqual({ kind: 'plugin', name: 'verinoda-live', asUser: true })
    }
    expect(w.runs.filter(r => r[1] === 'locate' && !r.includes('--daemon'))).toEqual([])
    await $.prompt.submit({ text: 'Where is the parser slow? Please find its implementation and all of the callers.', origin: { kind: 'composer' }, wait: false } as never)
    expect(w.prompts.at(-1)?.context.join('')).toContain('[Verinoda auto-context]')
    expect((await $.tool.call(grep) as { deny?: string }).deny).toContain('located src/parser.ts')
  })
})

describe('improve: choosing in the pane', () => {
  test('three groups, evidence and taste labels, no preselection and no send button', async ($, on) => {
    await setup($, on)
    await $.command.run(run())
    expect(toolResult(await callPropose($))).toContain('Shown to the user: 3 items (1 possible problems, 1 improvements, 1 matters of taste)')
    const pane = await mount($)
    const drawn = (await pane.find({ key: 'improve-pane' }))!.text
    expect(drawn.indexOf('Olası sorunlar') < drawn.indexOf('İşlevsel iyileştirmeler')).toBe(true)
    expect(drawn.indexOf('İşlevsel iyileştirmeler') < drawn.indexOf('Estetik seçenekler')).toBe(true)
    expect((await pane.find({ key: 'item-i1' }))?.props.label).toBe('[ ] title i1 (kanıtsız)')
    expect((await pane.find({ key: 'item-i3' }))?.props.label).toBe('[ ] title i3 (tahmin)')
    expect(await pane.find({ key: 'send' })).toBeUndefined()
    expect(await pane.find({ text: 'Göndermek için en az bir madde seçin.' })).toBeDefined()
    expect(drawn).not.toContain('g gönder')
  })

  test('a stored list says when the host did not put the pane on screen', async ($, on) => {
    await setup($, on, { placed: false })
    await $.command.run(run())
    const text = toolResult(await callPropose($))
    expect(text).toContain('stored (3 items) but the pane is not on screen')
    expect(text).toContain('/verinoda-improve')
    expect((await state($)).phase).toBe('choosing')
  })

  test('details explain locally; marks toggle and keep alone cannot be sent', async ($, on) => {
    const { w, clock } = await setup($, on)
    await $.command.run(run())
    await clock.settle()
    await callPropose($)
    const pane = await mount($)
    await press($, 'item-i1')
    expect(await pane.find({ text: 'Kanıt verilmedi' })).toBeDefined()
    for (const key of ['act-apply', 'act-keep', 'act-check']) expect(await pane.find({ key })).toBeDefined()
    await press($, 'act-apply')
    expect((await pane.find({ key: 'send' }))?.props.label).toBe('Gönder (1)')
    expect((await pane.find({ key: 'act-apply' }))?.props.label).toBe('● Uygula')
    await press($, 'act-apply')
    expect(await pane.find({ key: 'send' })).toBeUndefined()
    await press($, 'act-keep')
    expect(await pane.find({ key: 'send' })).toBeUndefined()
    expect(await pane.find({ text: '3 madde · 1 kalsın' })).toBeDefined()
    await press($, 'item-i2')
    expect(await pane.find({ key: 'act-check' })).toBeUndefined()
    expect(await pane.find({ key: 'details-i1' })).toBeUndefined()
    await press($, 'item-i2')
    expect(await pane.find({ key: 'details-i2' })).toBeUndefined()
    expect(w.prompts.length).toBe(1)
  })

  test('each more button counts only folded items and reveals its group', async ($, on) => {
    await setup($, on)
    await $.command.run(run())
    await callPropose($, Array.from({ length: 12 }, (_, n) => item(`i${n + 1}`)))
    const pane = await mount($)
    expect((await pane.find({ key: 'more-problem' }))?.props.label).toBe('5 madde daha')
    expect(await pane.find({ key: 'item-i8' })).toBeUndefined()
    await press($, 'more-problem')
    expect(await pane.find({ key: 'item-i12' })).toBeDefined()
    expect(await pane.find({ key: 'more-problem' })).toBeUndefined()
  })

  test('own items retain the draft, empty the field on submit, can be removed, and stop at ten', async ($, on) => {
    await setup($, on)
    await $.command.run(run())
    await callPropose($)
    const pane = await mount($)
    await input($, 'My own request', 'change')
    expect((await pane.find({ key: 'own' }))?.props.value).toBe('My own request')
    await input($, 'My own request')
    expect((await pane.find({ key: 'own' }))?.props.value).toBe('')
    expect((await pane.find({ key: 'item-u1' }))?.props.label).toBe('[✓] My own request')
    await press($, 'item-u1')
    expect(await pane.find({ key: 'act-apply' })).toBeUndefined()
    await press($, 'act-remove')
    expect(await pane.find({ key: 'item-u1' })).toBeUndefined()
    await input($, 'next')
    expect(await pane.find({ key: 'item-u2' })).toBeDefined()
    for (let n = 0; n < 9; n++) await input($, `own ${n}`)
    expect(await pane.find({ key: 'own' })).toBeUndefined()
    const s = await state($)
    expect(s.items.filter(i => i.group === 'own').length).toBe(10)
    expect(addOwn(s, 'eleventh')).toBe(s)
  })

  test('a replacement keeps unsent own items and their draft, clears decisions and unfolds; sent refuses it', async ($, on) => {
    await setup($, on)
    await $.command.run(run())
    await callPropose($)
    const pane = await mount($)
    await input($, 'keep my addition')
    await input($, 'unfinished', 'change')
    await press($, 'item-i1')
    await press($, 'act-keep')
    expect(toolResult(await callPropose($, [item('new')]))).toContain('The earlier list (3 items, 1 decisions) was replaced.')
    expect(await pane.find({ key: 'item-u1' })).toBeDefined()
    expect(await pane.find({ key: 'item-i1' })).toBeUndefined()
    expect((await pane.find({ key: 'own' }))?.props.value).toBe('unfinished')
    expect((await pane.find({ key: 'item-new' }))?.props.label).toContain('[ ]')
    await press($, 'send')
    const sent = await state($)
    expect(toolResult(await callPropose($))).toContain('a selection is being applied')
    expect(await state($)).toEqual(sent)
  })
})

describe('improve: sending and receiving outcomes', () => {
  test('two send presses racing for the same choices queue one prompt', async ($, on) => {
    const { w, clock } = await setup($, on)
    await $.command.run(run())
    await clock.settle()
    await callPropose($)
    await mount($)
    await press($, 'item-i1')
    await press($, 'act-apply')
    await Promise.all([press($, 'send'), press($, 'send')])
    await clock.settle()
    expect(w.prompts.length).toBe(2)
    expect((await state($)).phase).toBe('sent')
  })

  test('sending freezes the list; stopping exposes missing reports, including while a later selection runs', async ($, on) => {
    const { w, clock } = await setup($, on)
    await $.command.run(run())
    await callPropose($, Array.from({ length: 10 }, (_, n) => item(`i${n + 1}`)))
    const pane = await mount($)
    await press($, 'item-i1')
    await press($, 'act-apply')
    const chosen = await state($)
    await press($, 'send')
    await clock.settle()
    expect(w.prompts.at(-1)?.text).toBe(selectionPrompt(chosen))
    expect(w.prompts.at(-1)?.origin).toEqual({ kind: 'plugin', name: 'verinoda-live', asUser: true })
    expect((await state($)).phase).toBe('sent')
    for (const key of ['item-i1', 'own', 'send', 'ask-report', 'more-problem', 'act-apply', 'act-keep', 'act-check']) {
      expect(await pane.find({ key })).toBeUndefined()
    }
    expect(await pane.find({ key: 'details-i1' })).toBeDefined()
    expect(await pane.find({ text: /title i1.*bekleniyor/ })).toBeDefined()
    await press($, 'stop-waiting')
    expect(await pane.find({ text: /Sonuç bildirilmedi/ })).toBeDefined()
    expect(await pane.find({ key: 'ask-report' })).toBeDefined()
    await press($, 'item-i2')
    await press($, 'act-apply')
    await press($, 'send')
    expect(await pane.find({ key: 'ask-report' })).toBeUndefined()
    expect(await pane.find({ text: /Sonuç bildirilmedi/ })).toBeDefined()
  })

  test('partial reports keep waiting rows; final results are immutable and show extra changes', async ($, on) => {
    await setup($, on)
    await $.command.run(run())
    await callPropose($)
    const pane = await mount($)
    for (const id of ['i1', 'i2']) {
      await press($, `item-${id}`)
      await press($, 'act-apply')
    }
    await press($, 'send')
    expect(toolResult(await callReport($, [outcome('i1')]))).toBe('Recorded: 1 outcomes. Still waiting for: i2.')
    expect((await state($)).phase).toBe('sent')
    expect(await pane.find({ text: /title i2.*bekleniyor/ })).toBeDefined()
    expect(toolResult(await callReport($, [outcome('i2', 'partial', 'One case remains.')], ['helper updated']))).toBe('Recorded: 1 outcomes.')
    expect((await state($)).phase).toBe('choosing')
    expect(await pane.find({ text: /✓ Uygulandı · title i1/ })).toBeDefined()
    expect(await pane.find({ text: /◐ Kısmen uygulandı · title i2/ })).toBeDefined()
    expect(await pane.find({ text: 'Listede olmayan değişiklikler' })).toBeDefined()
    expect(await pane.find({ text: /helper updated/ })).toBeDefined()
    expect(await pane.find({ key: 'item-i1' })).toBeUndefined()
    expect(toolResult(await callReport($, [outcome('i1')]))).toContain('no item i1 is waiting')
  })

  test('unrelated, previous review and subagent ends do not finish the queued selection; its own end does', async ($, on) => {
    const { w, clock } = await setup($, on)
    await $.command.run(run())
    await clock.settle()
    await $.turn.start({ text: w.prompts[0]!.text, turnId: 'review' })
    await callPropose($)
    const pane = await mount($)
    await press($, 'item-i1')
    await press($, 'act-apply')
    await press($, 'send')
    await clock.settle()
    for (const id of ['already-running', 'review']) await end($, id)
    expect((await state($)).phase).toBe('sent')
    await $.turn.start({ text: w.prompts.at(-1)!.text, turnId: 'selected' })
    await end($, 'selected', 'child')
    expect((await state($)).phase).toBe('sent')
    await end($, 'selected')
    expect(await pane.find({ text: /Sonuç bildirilmedi/ })).toBeDefined()
    await press($, 'ask-report')
    await clock.settle()
    expect(w.prompts.at(-1)?.text).toBe(reportPrompt(['i1']))
    await $.turn.start({ text: w.prompts.at(-1)!.text, turnId: 'report' })
    expect(toolResult(await callReport($, [outcome('i1')]))).toBe('Recorded: 1 outcomes.')
    await end($, 'report')
    expect((await state($)).phase).toBe('choosing')
    expect(await pane.find({ key: 'ask-report' })).toBeUndefined()
  })

  test('a review without a list shows its note only on its own end, and accepts a late list', async ($, on) => {
    const { w, clock } = await setup($, on)
    await $.command.run(run())
    await clock.settle()
    const pane = await mount($)
    await end($, 'other')
    expect(await pane.find({ text: /İnceleniyor/ })).toBeDefined()
    await $.turn.start({ text: w.prompts[0]!.text, turnId: 'review' })
    await end($, 'review')
    expect(await pane.find({ text: /Liste henüz gelmedi/ })).toBeDefined()
    await callPropose($)
    expect(await pane.find({ text: /Liste henüz gelmedi/ })).toBeUndefined()
    expect(await pane.find({ key: 'item-i1' })).toBeDefined()
  })

  test('a confirmed check returns undecided, with evidence and without a check button, until explicitly applied', async ($, on) => {
    await setup($, on)
    await $.command.run(run())
    await callPropose($)
    const pane = await mount($)
    await press($, 'item-i1')
    await press($, 'act-check')
    await press($, 'send')
    await callReport($, [{ ...outcome('i1', 'confirmed', 'Reproduced with an empty input.'), change: 'Guard the empty input', evidence: ['parser.ts:9'] }])
    expect((await pane.find({ key: 'item-i1' }))?.props.label).toBe('[ ] title i1 (doğrulandı)')
    expect(await pane.find({ text: /İnceleme: Reproduced/ })).toBeDefined()
    expect(await pane.find({ text: 'Kanıt: parser.ts:9' })).toBeDefined()
    expect(await pane.find({ text: 'Kanıt verilmedi' })).toBeUndefined()
    expect(await pane.find({ key: 'act-check' })).toBeUndefined()
    expect(await pane.find({ key: 'send' })).toBeUndefined()
    await press($, 'act-apply')
    await press($, 'send')
    expect(toolResult(await callReport($, [outcome('i1')]))).toBe('Recorded: 1 outcomes.')
  })

  test('the command reopens an unfinished list, refuses a new review while sent, and restarts a finished one', async ($, on) => {
    const { w, clock } = await setup($, on)
    await $.command.run(run('one'))
    await clock.settle()
    await callPropose($, [item('i1')])
    const pane = await mount($)
    expect((await $.command.run(run())).text).toBe('improvement list opened (1 items)')
    await clock.settle()
    expect(w.prompts.length).toBe(1)
    await press($, 'item-i1')
    await press($, 'act-keep')
    expect((await $.command.run(run('two'))).text).toContain('the earlier list of 1 items was replaced')
    await callPropose($, [item('i1')])
    await press($, 'item-i1')
    await press($, 'act-apply')
    await press($, 'send')
    expect((await $.command.run(run('three'))).text).toContain('a selection is being applied')
    await callReport($, [outcome('i1')])
    expect((await $.command.run(run())).text).toContain('looking at it')
    expect(await pane.find({ text: /İnceleniyor/ })).toBeDefined()
  })
})

describe('improve: failures and session boundaries', () => {
  test('clearing before the deferred review runs prevents it from starting in the new conversation', async ($, on) => {
    const { w, clock } = await setup($, on)
    await $.command.run(run('old conversation'))
    await $.session.end({ reason: 'clear', sessionId: 's' } as never)
    await clock.settle()
    expect(w.prompts).toEqual([])
    expect((await state($)).phase).toBe('idle')
  })

  for (const failure of ['drop', 'reject'] as const) {
    test(`a ${failure} restores the review, selection and report request with a visible note`, async ($, on) => {
      const { w, clock } = await setup($, on)
      w.failure = failure
      await $.command.run(run())
      await clock.settle()
      const pane = await mount($)
      expect((await state($)).phase).toBe('idle')
      expect(await pane.find({ text: 'İstek gönderilemedi.' })).toBeDefined()
      w.failure = ''
      await $.command.run(run())
      await clock.settle()
      expect(await pane.find({ text: 'İstek gönderilemedi.' })).toBeUndefined()
      expect(toolResult(await callPropose($))).toContain('Shown to the user')
      expect((await state($)).phase).toBe('choosing')
      expect(await pane.find({ key: 'item-i1' })).toBeDefined()
      await press($, 'item-i1')
      await press($, 'act-apply')
      w.failure = failure
      await press($, 'send')
      await clock.settle()
      expect((await state($)).phase).toBe('choosing')
      expect((await state($)).items[0]?.sentAs).toBe(null)
      expect((await pane.find({ key: 'item-i1' }))?.props.label).toContain('[✓]')
      expect((await pane.find({ key: 'send' }))?.props.label).toBe('Gönder (1)')
      expect(await pane.find({ text: 'İstek gönderilemedi.' })).toBeDefined()
      w.failure = ''
      await press($, 'send')
      await clock.settle()
      expect(await pane.find({ text: 'İstek gönderilemedi.' })).toBeUndefined()
      await press($, 'stop-waiting')
      w.failure = failure
      await press($, 'ask-report')
      await clock.settle()
      expect((await state($)).phase).toBe('choosing')
      expect((await state($)).items[0]?.outcome?.status).toBe('unreported')
      expect(await pane.find({ text: 'İstek gönderilemedi.' })).toBeDefined()
      w.failure = ''
      await press($, 'ask-report')
      await clock.settle()
      expect(await pane.find({ text: 'İstek gönderilemedi.' })).toBeUndefined()
      await press($, 'stop-waiting')
      expect((await state($)).note).toBe('')
      expect(toolResult(await callReport($, [outcome('i1')]))).toBe('Recorded: 1 outcomes.')
    })
  }

  test('a delayed dropped review cannot undo a newer review', async ($, on) => {
    const { w, clock } = await setup($, on)
    let release!: () => void
    w.hold = new Promise<void>(resolve => { release = resolve })
    w.failure = 'drop'
    await $.command.run(run('old review'))
    await clock.settle()
    w.hold = undefined
    w.failure = ''
    await $.command.run(run('new review'))
    release()
    await clock.settle()
    expect((await state($)).subject).toBe('new review')
    expect((await state($)).phase).toBe('reviewing')
    expect((await state($)).expect).toBe('review')
    expect((await state($)).note).toBe('')
  })

  test('clear resets an in-flight list and expected turn; other session ends preserve it', async ($, on) => {
    await setup($, on)
    await $.command.run(run())
    await callPropose($)
    await mount($)
    await input($, 'my request')
    await press($, 'send')
    const before = await state($)
    expect(before.expect).toBe('selection')
    await $.session.end({ reason: 'other', sessionId: 's' } as never)
    expect(await state($)).toEqual(before)
    await $.session.end({ reason: 'clear', sessionId: 's' } as never)
    const cleared = await state($)
    expect(cleared.phase).toBe('idle')
    expect(cleared.items).toEqual([])
    expect(cleared.expect).toBe('')
    expect(cleared.turn).toBe(null)
    expect((await $.command.run(run())).text).toContain('looking at it')
  })
})

describe('improve: surfaces', () => {
  test('a mobile pane with only final results advertises no nonexistent controls', async ($, on) => {
    await setup($, on)
    await $.command.run(run())
    await callPropose($, [item('i1')])
    const pane = await mount($, 'mobile')
    await press($, 'item-i1')
    await press($, 'act-apply')
    await press($, 'send')
    await callReport($, [outcome('i1')])
    expect(await pane.find({ text: /tab gezin/ })).toBeUndefined()
    expect(await pane.find({ text: /enter seç/ })).toBeUndefined()
  })

  for (const surface of ['terminal', 'desktop', 'mobile'] as const) {
    test(`the tree validates at 30 columns on ${surface}, with its supported input controls`, async ($, on) => {
      await setup($, on)
      await $.command.run(run())
      await callPropose($)
      const pane = await mount($, surface, 30)
      expect(await pane.find({ text: 'Olası sorunlar' })).toBeDefined()
      expect((await pane.find({ key: 'improve-pane' }))?.props.width).toBe(30)
      if (surface === 'mobile') {
        expect(await pane.find({ key: 'own' })).toBeUndefined()
        expect(await pane.find({ text: /Kendi maddenizi eklemek için terminali/ })).toBeDefined()
      } else expect(await pane.find({ key: 'own' })).toBeDefined()
      await press($, 'item-i1')
      await press($, 'act-check')
      await press($, 'item-i2')
      await press($, 'act-apply')
      await press($, 'send')
      await callReport($, [outcome('i1', 'not_confirmed', 'Could not reproduce.'), outcome('i2', 'failed', 'Missing dependency.')])
      expect(await pane.find({ text: /○ İncelendi: sorun doğrulanmadı/ })).toBeDefined()
      expect(await pane.find({ text: /✗ Uygulanamadı/ })).toBeDefined()
    })
  }

  test('an unfocused pane explains how to give it the keyboard', async ($, on) => {
    await setup($, on)
    await $.command.run(run())
    await callPropose($)
    const pane = await mount($, 'terminal', 40, false)
    expect(await pane.find({ text: 'Seçmek için ctrl+x tab' })).toBeDefined()
  })
})
