import type { ToolSpec } from 'claude-code'
import type { ImproveDecision, ImproveGroup, ImproveItem, ImproveState, ImproveStatus } from '../types'

export const IMPROVE_PANE = 'verinoda-improve'
export const PROPOSE_TOOL = 'mcp__verinoda-live__improve_propose'
export const REPORT_TOOL = 'mcp__verinoda-live__improve_report'
export const GROUPS: ImproveGroup[] = ['problem', 'improvement', 'taste', 'own']
export const GROUP_LABEL: Record<ImproveGroup, string> = {
  problem: 'Olası sorunlar', improvement: 'İşlevsel iyileştirmeler', taste: 'Estetik seçenekler', own: 'Sizin eklediğiniz',
}
export const DECISION_LABEL = { apply: 'Uygula', keep: 'Kalsın', check: 'Önce kontrol et' }
const MARK: Record<ImproveDecision, string> = { none: '[ ]', apply: '[✓]', keep: '[–]', check: '[?]' }
export const RESULT_LABEL: Record<Exclude<ImproveStatus, 'waiting'>, string> = {
  applied: '✓ Uygulandı', partial: '◐ Kısmen uygulandı', failed: '✗ Uygulanamadı',
  not_confirmed: '○ İncelendi: sorun doğrulanmadı', unreported: '? Sonuç bildirilmedi',
}
export const WORDS = {
  idle: 'Liste yok. /verinoda-improve [neye bakılsın] ile başlatın.',
  reviewing: 'İnceleniyor… Claude listeyi hazırlıyor; hiçbir şey değiştirilmiyor.',
  noList: 'Liste henüz gelmedi. Claude bir soru sormuş olabilir: yanıtlayın ya da /verinoda-improve ile yeniden başlatın.',
  notSent: 'İstek gönderilemedi.',
  sent: 'Seçimleriniz gönderildi; sonuçlar burada görünecek.',
  select: 'Göndermek için en az bir madde seçin.',
  mobile: 'Kendi maddenizi eklemek için terminali ya da masaüstünü kullanın.',
}
export const NO_PERSON = 'verinoda improve: nobody is at the keyboard to choose; ask in text instead'

export const PROPOSE_DESCRIPTION = 'Show the user a list of what could be changed, in the Verinoda pane, so that they choose. Call it only when the user asked for improvement options or a review list, in words or with /verinoda-improve; never on your own initiative. Before calling, look at the code or the screen in question and change nothing. Rank the items, the most worth doing first, and do not pad the list. Each item is one change the user can say yes or no to. `group`: `problem` is a possible bug, inconsistency or unexpected behaviour (give `evidence`, the file:line you read); `improvement` is usability, readability, performance or upkeep; `taste` is look, tone, density, colour or layout, a preference and not a defect (set `seen` to true only if you judged it from a rendered view or a screenshot). `title` says the change in one concrete line (not "improve the hierarchy" but "make Save the only primary button"); `observation` is what is there now; `why` is why it may matter; `change` is exactly what you would do and what stays as it is; `cost` is what it costs or risks, if anything. After the call, stop: change nothing and wait for the user\'s selection, which arrives as a message.'
export const REPORT_DESCRIPTION = 'Report what happened to each item the user sent from the Verinoda pane, once you are done or cannot go on. One outcome per item id you were given. For an item to apply: `applied` (done as described), `partial` (say what is missing) or `failed` (say why). For an item to check first: `confirmed` (the problem is real; put the fix you propose in `change` and do not apply it) or `not_confirmed` (you looked and found nothing that needs a change). `note` is one or two sentences the user reads; `evidence` is what shows it: the check you ran and its result, a file:line. List under `extra_changes` every change you made that was not on the list.'
export const OFFER_PROMPT = 'When the user asks for something to be better, nicer, cleaner or of higher quality without saying what should change, do not guess and do not start editing. Offer in one line to list what could be changed so that they choose: `/verinoda-improve` opens the list, and they may add what to look at. If they ask for the list in words, look first, then call `mcp__verinoda-live__improve_propose`. A request that says what to change is not vague: do it.'

const STRING = { type: 'string' }
const STRINGS = { type: 'array', items: STRING }
export const IMPROVE_TOOLS: ToolSpec[] = [
  {
    name: 'improve_propose', description: PROPOSE_DESCRIPTION,
    inputSchema: {
      type: 'object', required: ['subject', 'items'], properties: {
        subject: { ...STRING, description: 'What was looked at, in a few words: a file, a screen, a module' },
        items: {
          type: 'array', minItems: 1, maxItems: 30, items: {
            type: 'object', required: ['id', 'group', 'title', 'observation', 'why', 'change'], properties: {
              id: { ...STRING, description: 'Short and unique in this list: i1, i2, ...' },
              group: { enum: ['problem', 'improvement', 'taste'] }, title: STRING,
              observation: STRING, why: STRING, change: STRING, cost: STRING, evidence: STRINGS, seen: { type: 'boolean' },
            },
          },
        },
      },
    },
  },
  {
    name: 'improve_report', description: REPORT_DESCRIPTION,
    inputSchema: {
      type: 'object', required: ['outcomes'], properties: {
        outcomes: {
          type: 'array', minItems: 1, items: {
            type: 'object', required: ['id', 'status', 'note'], properties: {
              id: STRING, status: { enum: ['applied', 'partial', 'failed', 'confirmed', 'not_confirmed'] },
              note: STRING, change: STRING, evidence: STRINGS,
            },
          },
        },
        extra_changes: STRINGS,
      },
    },
  },
]

export function initialImprove(): ImproveState {
  return { phase: 'idle', subject: '', items: [], open: null, unfolded: [], extra: [], draft: '', ownSeq: 0,
    expect: '', turn: null, note: '', requestSeq: 0 }
}

function object(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function cut(value: unknown, max: number): string {
  const text = typeof value === 'string' ? value.trim() : ''
  return text.length > max ? `${text.slice(0, max - 1)}…` : text
}

function strings(value: unknown, count: number, width: number): string[] {
  return Array.isArray(value) ? value.map(v => cut(v, width)).filter(Boolean).slice(0, count) : []
}

export const listItems = (s: ImproveState) => s.items.filter(i => i.outcome === null || i.outcome.status === 'waiting')
export const resultItems = (s: ImproveState) => s.items.filter(i => i.outcome !== null && i.outcome.status !== 'waiting')
export const sendable = (i: ImproveItem) => i.outcome === null && (i.decision === 'apply' || i.decision === 'check')
const unsentOwn = (s: ImproveState) => s.items.filter(i => i.group === 'own' && i.outcome === null && i.sentAs === null)
const waiting = (s: ImproveState) => s.items.filter(i => i.outcome?.status === 'waiting')

export function replacedList(s: ImproveState): { count: number; decisions: number } {
  const items = listItems(s).filter(i => i.group !== 'own')
  return { count: items.length, decisions: items.filter(i => i.decision !== 'none').length }
}

export function startReview(s: ImproveState, what: string): ImproveState {
  return { ...s, phase: 'reviewing', subject: what, items: unsentOwn(s), extra: [], open: null, unfolded: [],
    expect: 'review', turn: null, note: '', requestSeq: s.requestSeq + 1 }
}

export function propose(s: ImproveState, input: unknown, interactive = true): { state: ImproveState; error: string | null } {
  const refuse = (error: string) => ({ state: s, error })
  if (!interactive) return refuse(NO_PERSON)
  if (s.phase === 'sent') return refuse('verinoda improve: a selection is being applied; report its outcomes with improve_report before proposing a new list')
  const bad = (what: string) => refuse(`verinoda improve: ${what}; nothing was shown`)
  if (!object(input) || !Array.isArray(input.items) || input.items.length < 1 || input.items.length > 30) {
    return bad('items must list 1 to 30 objects')
  }
  const items: ImproveItem[] = []
  const ids = new Set<string>()
  for (const [n, raw] of input.items.entries()) {
    if (!object(raw)) return bad(`item ${n + 1} must be an object`)
    const id = typeof raw.id === 'string' ? raw.id.trim() : ''
    const name = id || `at position ${n + 1}`
    if (!/^[A-Za-z0-9_-]{1,16}$/.test(id) || /^u\d+$/.test(id)) return bad(`item ${name} has an invalid or reserved id`)
    if (ids.has(id)) return bad(`item ${id} has a repeated id`)
    ids.add(id)
    if (raw.group !== 'problem' && raw.group !== 'improvement' && raw.group !== 'taste') return bad(`item ${id} has an unknown group`)
    for (const field of ['title', 'observation', 'why', 'change']) {
      if (typeof raw[field] !== 'string' || raw[field].trim() === '') return bad(`item ${id} needs a non-empty ${field}`)
    }
    items.push({ id, group: raw.group, title: cut(raw.title, 120), observation: cut(raw.observation, 600),
      why: cut(raw.why, 600), change: cut(raw.change, 600), cost: cut(raw.cost, 600), evidence: strings(raw.evidence, 6, 160),
      seen: raw.seen === true, verified: false, checked: '', decision: 'none', sentAs: null, outcome: null })
  }
  return { error: null, state: { ...s, phase: 'choosing', subject: cut(input.subject, 80) || cut(s.subject, 80) || 'bu konuşma',
    items: [...items, ...unsentOwn(s)], extra: [], open: null, unfolded: [], note: '', requestSeq: s.requestSeq + 1 } }
}

export function proposalReply(s: ImproveState, old: ImproveState, placed: boolean): string {
  const items = listItems(s).filter(i => i.group !== 'own')
  const count = (group: ImproveGroup) => items.filter(i => i.group === group).length
  const replaced = replacedList(old)
  const tail = replaced.decisions ? ` The earlier list (${replaced.count} items, ${replaced.decisions} decisions) was replaced.` : ''
  return (placed
    ? `Shown to the user: ${items.length} items (${count('problem')} possible problems, ${count('improvement')} improvements, ${count('taste')} matters of taste). Stop here: change nothing and wait for their selection, which arrives as a message.`
    : `The list is stored (${items.length} items) but the pane is not on screen. Tell the user to run /verinoda-improve to see it. Change nothing meanwhile.`) + tail
}

export function decide(s: ImproveState, id: string, decision: Exclude<ImproveDecision, 'none'>): ImproveState {
  if (s.phase !== 'choosing') return s
  return { ...s, items: s.items.map(i => {
    if (i.id !== id || i.outcome !== null || i.group === 'own') return i
    if (decision === 'check' && (i.group !== 'problem' || i.verified)) return i
    return { ...i, decision: i.decision === decision ? 'none' : decision }
  }) }
}

export function toggleOpen(s: ImproveState, id: string): ImproveState {
  return s.phase === 'choosing' && s.items.some(i => i.id === id && i.outcome === null)
    ? { ...s, open: s.open === id ? null : id } : s
}

export function addOwn(s: ImproveState, value: string): ImproveState {
  const title = cut(value, 300)
  if (s.phase !== 'choosing' || !title || unsentOwn(s).length >= 10) return s
  const ownSeq = s.ownSeq + 1
  const item: ImproveItem = { id: `u${ownSeq}`, group: 'own', title, observation: '', why: '', change: title, cost: '',
    evidence: [], seen: false, verified: false, checked: '', decision: 'apply', sentAs: null, outcome: null }
  return { ...s, ownSeq, draft: '', items: [...s.items, item] }
}

export function removeOwn(s: ImproveState, id: string): ImproveState {
  if (s.phase !== 'choosing' || !unsentOwn(s).some(i => i.id === id)) return s
  return { ...s, open: s.open === id ? null : s.open, items: s.items.filter(i => i.id !== id) }
}

export function firstSight(s: ImproveState): { visible: ImproveItem[]; hidden: Record<ImproveGroup, number> } {
  const items = listItems(s)
  const first = new Set(items.filter(i => i.group !== 'own').slice(0, 7).map(i => i.id))
  const hidden = { problem: 0, improvement: 0, taste: 0, own: 0 }
  const visible = items.filter(i => {
    if (i.group === 'own' || first.has(i.id) || i.decision !== 'none' || i.verified || s.open === i.id || s.unfolded.includes(i.group)) return true
    hidden[i.group]++
    return false
  })
  return { visible, hidden }
}

export function countsLine(s: ImproveState): string {
  const items = listItems(s)
  if (!items.length) return ''
  const parts = [`${items.length} madde`]
  for (const [decision, label] of [['apply', 'uygula'], ['check', 'kontrol'], ['keep', 'kalsın']] as const) {
    const n = items.filter(i => i.decision === decision && (decision === 'keep' || sendable(i))).length
    if (n) parts.push(`${n} ${label}`)
  }
  return parts.join(' · ')
}

export function rowLabel(i: ImproveItem): string {
  const suffix = i.verified ? ' (doğrulandı)' : i.group === 'problem' && !i.evidence.length ? ' (kanıtsız)'
    : i.group === 'taste' && !i.seen ? ' (tahmin)' : ''
  return `${MARK[i.decision]} ${i.title}${suffix}`
}

export const REVIEW_FIRST = 'Make a list of what could be changed here so that I can choose. Do not change anything yet.'
export function reviewPrompt(what: string): string {
  return `${REVIEW_FIRST}\nWhat to look at: ${what.trim() || 'what we have been working on in this conversation. If that is not clear, ask me one question first.'}\n\nLook at it first: read the code, and if it has a rendered view you can look at, look at it. Then call\n${PROPOSE_TOOL} with the items, ranked, the most worth doing first. Do not pad the list. Then stop and\nwait for my selection.`
}

export function selectionPrompt(s: ImproveState): string {
  const sections = [`My decisions on the improvement list (${s.subject}):`]
  for (const [decision, heading] of [
    ['apply', 'Apply these:'], ['check', 'Check these first (investigate only; change nothing for them):'],
    ['keep', 'Keep these as they are (do not change them, also not as a side effect):'],
  ] as const) {
    const items = s.items.filter(i => i.decision === decision && i.outcome === null)
    if (!items.length) continue
    sections.push(`${heading}\n${items.map(i => `- [${i.id}] ${i.title}${i.group === 'own' ? ' (my own item)' : decision === 'apply' ? `: ${i.change}` : ''}`).join('\n')}`)
  }
  sections.push(`Everything else stays as it is. You may make the small auxiliary changes an item needs. If one would add behaviour, change\nbehaviour I did not choose, or cost something, tell me before you do it. When you are done or cannot go on, call\n${REPORT_TOOL} with one outcome for each item to apply or to check, and list under extra_changes any\nchange that was not on the list.`)
  return sections.join('\n\n')
}

export function reportPrompt(ids: string[]): string {
  return `Report the outcome of the items I sent that you have not reported yet (${ids.join(', ')}) with ${REPORT_TOOL}.\nChange nothing else.`
}

const pendingOutcome = (): ImproveItem['outcome'] => ({ status: 'waiting', note: '', evidence: [] })
export function sendSelection(s: ImproveState): ImproveState {
  if (s.phase !== 'choosing' || !s.items.some(sendable)) return s
  return { ...s, phase: 'sent', expect: 'selection', turn: null, note: '', requestSeq: s.requestSeq + 1,
    items: s.items.map(i => sendable(i) ? { ...i, sentAs: i.decision as 'apply' | 'check', outcome: pendingOutcome() } : i) }
}

export function askReport(s: ImproveState): ImproveState {
  if (s.phase !== 'choosing' || !s.items.some(i => i.outcome?.status === 'unreported')) return s
  return { ...s, phase: 'sent', expect: 'report', turn: null, note: '', requestSeq: s.requestSeq + 1,
    items: s.items.map(i => i.outcome?.status === 'unreported' ? { ...i, outcome: pendingOutcome() } : i) }
}

export function stopWaiting(s: ImproveState): ImproveState {
  return { ...s, phase: 'choosing', expect: '', turn: null, note: '', requestSeq: s.requestSeq + 1,
    items: s.items.map(i => i.outcome?.status === 'waiting'
      ? { ...i, outcome: { ...i.outcome, status: 'unreported' } } : i) }
}

export function submissionFailed(s: ImproveState, sent: ImproveState): ImproveState {
  if (s.requestSeq !== sent.requestSeq || s.expect !== sent.expect || !s.expect) return s
  const ids = new Set(waiting(sent).map(i => i.id))
  return { ...s, phase: sent.expect === 'review' ? 'idle' : 'choosing', expect: '', turn: null, note: 'not-sent',
    items: s.items.map(i => !ids.has(i.id) || i.outcome?.status !== 'waiting' ? i : sent.expect === 'selection'
      ? { ...i, sentAs: null, outcome: null } : { ...i, outcome: { ...i.outcome, status: 'unreported' } }) }
}

export function turnStarted(s: ImproveState, text: string, turnId: string): ImproveState {
  if (!s.expect) return s
  const prompt = s.expect === 'review' ? reviewPrompt(s.subject) : s.expect === 'selection'
    ? selectionPrompt(s) : reportPrompt(waiting(s).map(i => i.id))
  return text.trim().startsWith(prompt.split('\n')[0] ?? prompt) ? { ...s, turn: turnId, expect: '' } : s
}

export function turnEnded(s: ImproveState, turnId: string, agentId?: string): ImproveState {
  if (agentId !== undefined || s.turn !== turnId) return s
  if (s.phase === 'sent') return stopWaiting(s)
  return { ...s, turn: null, note: s.phase === 'reviewing' ? 'no-list' : s.note }
}

export function report(s: ImproveState, input: unknown): { state: ImproveState; message: string } {
  const refuse = (message: string) => ({ state: s, message: `verinoda improve: ${message}` })
  if (!object(input) || !Array.isArray(input.outcomes) || !input.outcomes.length || !input.outcomes.every(object)) {
    return refuse('outcomes must list at least one item; nothing was recorded')
  }
  const ids = new Set<string>()
  for (const raw of input.outcomes) {
    const id = cut(raw.id, 160)
    if (ids.has(id)) return refuse(`item ${id} is reported twice`)
    ids.add(id)
  }
  const pending = new Map<string, ImproveItem>()
  for (const raw of input.outcomes) {
    const id = cut(raw.id, 160)
    const item = s.items.find(i => i.id === id)
    if (!item || (item.outcome?.status !== 'waiting' && item.outcome?.status !== 'unreported')) return refuse(`no item ${id} is waiting for an outcome`)
    pending.set(id, item)
  }
  for (const raw of input.outcomes) {
    const id = cut(raw.id, 160)
    const item = pending.get(id)!
    const allowed = item.sentAs === 'apply' ? ['applied', 'partial', 'failed'] : ['confirmed', 'not_confirmed']
    if (typeof raw.status !== 'string' || !allowed.includes(raw.status)) {
      return refuse(`item ${id} was sent to ${item.sentAs}; its outcome is one of ${allowed.join(', ')}`)
    }
  }
  for (const raw of input.outcomes) {
    if (!cut(raw.note, 600)) return refuse(`item ${cut(raw.id, 160)} needs a note the user can read`)
  }
  const changes = new Map<string, ImproveItem>()
  for (const raw of input.outcomes) {
    const id = cut(raw.id, 160)
    const item = pending.get(id)!
    const note = cut(raw.note, 600)
    const evidence = strings(raw.evidence, 6, 160)
    changes.set(id, raw.status === 'confirmed'
      ? { ...item, verified: true, checked: note, outcome: null, decision: 'none', sentAs: null,
        change: cut(raw.change, 600) || item.change, evidence: [...item.evidence, ...evidence].slice(0, 6) }
      : { ...item, outcome: { status: raw.status as ImproveStatus, note, evidence } })
  }
  const state: ImproveState = { ...s, items: s.items.map(i => changes.get(i.id) ?? i),
    extra: [...s.extra, ...strings(input.extra_changes, 20, 300)].slice(0, 20) }
  const left = waiting(state).map(i => i.id)
  if (!left.length) state.phase = 'choosing'
  return { state, message: `Recorded: ${changes.size} outcomes.${left.length ? ` Still waiting for: ${left.join(', ')}.` : ''}` }
}
