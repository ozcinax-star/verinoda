import { atom, read, update } from 'claude-code'
import type { EngineInterface, On } from 'claude-code'
import type { ImproveDecision, ImproveState } from '../types'
import {
  addOwn, askReport, countsLine, DECISION_LABEL, decide, firstSight, GROUP_LABEL, GROUPS, IMPROVE_PANE,
  IMPROVE_TOOLS, initialImprove, listItems, NO_PERSON, proposalReply, propose, PROPOSE_DESCRIPTION,
  PROPOSE_TOOL, removeOwn, replacedList, report, REPORT_DESCRIPTION, reportPrompt, REPORT_TOOL, RESULT_LABEL,
  resultItems, reviewPrompt, rowLabel, sendable, sendSelection, selectionPrompt, startReview, stopWaiting,
  submissionFailed, toggleOpen, turnStarted, WORDS,
} from './improve'

const improve = atom({ plugin: 'verinoda-live', key: 'improve' } as const, initialImprove())
type Background = (work: Promise<unknown>) => void

async function registerTools($: EngineInterface): Promise<void> {
  for (const spec of IMPROVE_TOOLS) await $.tool.register(spec)
}

function submit($: EngineInterface, sent: ImproveState, text: string, background: Background): void {
  // D175: returning from the handler lets the queued turn start. A failed older submission owns no newer marks.
  background((async () => {
    try {
      const out = await $.prompt.submit({ text, asUser: true })
      if (out.drop === undefined) return
    } catch { /* restore the person's choices below */ }
    await update($, improve, s => submissionFailed(s, sent))
  })())
}

async function send($: EngineInterface, kind: 'selection' | 'report', background: Background): Promise<void> {
  const job: { state?: ImproveState; text: string } = { text: '' }
  await update($, improve, s => {
    job.state = undefined // update may retry after another press has already sent these items
    const state = kind === 'selection' ? sendSelection(s) : askReport(s)
    if (state === s) return s
    job.text = kind === 'selection' ? selectionPrompt(s)
      : reportPrompt(s.items.filter(i => i.outcome?.status === 'unreported').map(i => i.id))
    job.state = state
    return state
  })
  if (job.state) submit($, job.state, job.text, background)
}

export function registerImprove(on: On, live: { interactive: boolean }, background: Background): void {
  on('session.end', async ($, e, next) => {
    if (e.reason === 'clear') await update($, improve, s => ({ ...initialImprove(), requestSeq: s.requestSeq + 1 }))
    return next(e)
  })

  on('tool.describe', { tool: [PROPOSE_TOOL, REPORT_TOOL] }, ($, e) => ({
    description: e.tool === PROPOSE_TOOL ? PROPOSE_DESCRIPTION : REPORT_DESCRIPTION, isDeferred: false,
  }))

  on('command.run', { command: IMPROVE_PANE }, async ($, e) => {
    if (!live.interactive) return { text: 'verinoda improve needs a person to choose: it does nothing in a scripted session' }
    const old = await read($, improve)
    const what = e.args.trim()
    const open = () => $.ui.open({ id: IMPROVE_PANE, title: 'İyileştirme', focus: true, rows: 16 })
    if (old.phase === 'sent') {
      await open()
      return { text: 'a selection is being applied; its outcomes will show in the pane' }
    }
    if (old.phase === 'choosing' && !what && (listItems(old).length || old.items.some(i => i.outcome?.status === 'unreported'))) {
      await open()
      return { text: `improvement list opened (${listItems(old).length} items)` }
    }
    await registerTools($)
    const sent = await update($, improve, s => startReview(s, what))
    await open()
    // D175: the host rejects prompt.submit anywhere in command.run, even without awaiting it.
    $.clock.after(0, () => background((async () => {
      const current = await read($, improve)
      if (current.requestSeq === sent.requestSeq && current.expect === 'review') submit($, sent, reviewPrompt(what), background)
    })()))
    const replaced = replacedList(old)
    return { text: 'looking at it; the list will appear in the pane, and nothing is changed until you choose'
      + (replaced.decisions ? ` (the earlier list of ${replaced.count} items was replaced)` : '') }
  })

  on('tool.call', { tool: PROPOSE_TOOL }, async ($, e) => {
    if (!live.interactive) return { result: NO_PERSON }
    let old = initialImprove()
    let change: ReturnType<typeof propose> = { state: old, error: null }
    await update($, improve, s => {
      old = s
      change = propose(s, e, live.interactive)
      return change.state
    })
    if (change.error !== null) return { result: change.error }
    const opened = await $.ui.open({ id: IMPROVE_PANE, title: 'İyileştirme', focus: true, rows: 16 })
    return { result: proposalReply(change.state, old, opened.isPlaced) }
  })

  on('tool.call', { tool: REPORT_TOOL }, async ($, e) => {
    if (!live.interactive) return { result: NO_PERSON }
    let message = ''
    await update($, improve, s => {
      const out = report(s, e)
      message = out.message
      return out.state
    })
    return { result: message }
  })

  on('turn.start', async ($, e, next) => {
    if (live.interactive && (await read($, improve)).expect) {
      await update($, improve, s => turnStarted(s, e.text, e.turnId))
    }
    return next(e)
  })

  on('ui.render', { component: 'Pane', requestId: IMPROVE_PANE }, async ($, e) => {
    const { Box, Text, Button } = $.ui.resolve(e)
    const s = await read($, improve)
    const columns = Math.max(1, e.props.bodyColumns ?? 48)
    if (!live.interactive) return <Box flexDirection="column" width={columns} />
    if (s.phase === 'idle' || s.phase === 'reviewing') {
      return <Box flexDirection="column" width={columns}>
        <Text wrap="wrap">{s.phase === 'idle' ? WORDS.idle : s.note === 'no-list' ? WORDS.noList : WORDS.reviewing}</Text>
        {s.note === 'not-sent' && <Text wrap="wrap">{WORDS.notSent}</Text>}
      </Box>
    }
    const choosing = s.phase === 'choosing'
    const sight = firstSight(s)
    const results = resultItems(s)
    const n = s.items.filter(sendable).length
    const hasUnreported = results.some(i => i.outcome?.status === 'unreported')
    const openItem = sight.visible.find(i => i.id === s.open)
    const hasOwnInput = choosing && e.surface !== 'mobile' && listItems(s).filter(i => i.group === 'own').length < 10
    const counts = countsLine(s)
    const act = (decision: Exclude<ImproveDecision, 'none'>) => update($, improve, x => decide(x, s.open ?? '', decision))
    const actions = (openItem?.group === 'problem' && !openItem.verified ? ['apply', 'keep', 'check'] : ['apply', 'keep']) as ('apply' | 'keep' | 'check')[]
    const hotkey = { apply: 'u', keep: 'k', check: 'c' }
    const footer: string[] = []
    if (choosing && n) footer.push('g gönder')
    if (choosing && openItem && openItem.group !== 'own') {
      for (const a of actions) footer.push(`${hotkey[a]} ${a === 'apply' ? 'uygula' : a === 'keep' ? 'kalsın' : 'kontrol'}`)
    }
    if (!choosing || sight.visible.length || hasOwnInput || hasUnreported) {
      footer.push('tab gezin', choosing && sight.visible.length ? 'enter aç' : 'enter seç')
    }

    let input = null
    if (choosing) {
      if (e.surface === 'mobile') input = <Text key="own-mobile" wrap="wrap" dimColor>{WORDS.mobile}</Text>
      else if (hasOwnInput) {
        const { Input } = $.ui.resolve(e)
        input = <Input key="own" label="Kendi maddeniz" submitLabel="ekle" value={s.draft}
          onInput={value => update($, improve, x => x.phase === 'choosing' ? { ...x, draft: value } : x)}
          onSubmit={value => update($, improve, x => addOwn(x, value))} />
      }
    }

    return <Box key="improve-pane" flexDirection="column" width={columns}>
      <Text key="subject" bold wrap="wrap">{`İyileştirme: ${s.subject}`}</Text>
      {s.note === 'not-sent' && <Text key="not-sent" wrap="wrap">{WORDS.notSent}</Text>}
      {counts && <Text key="counts" wrap="wrap">{counts}</Text>}
      {results.length > 0 && <Box key="results" flexDirection="column" marginTop={1}>
        <Text bold>Sonuçlar</Text>
        {results.map(i => {
          const outcome = i.outcome!
          return <Box key={`result-${i.id}`} flexDirection="column">
            <Text wrap="wrap">{`${RESULT_LABEL[outcome.status as Exclude<typeof outcome.status, 'waiting'>]} · ${i.title}`}</Text>
            {outcome.note && <Text wrap="wrap">{`  ${outcome.note}`}</Text>}
            {outcome.evidence.length > 0 && <Text wrap="wrap" dimColor>{`  ${outcome.evidence.join(', ')}`}</Text>}
          </Box>
        })}
        {choosing && hasUnreported && <Button key="ask-report" label="Sonucu iste"
          onPress={() => send($, 'report', background)} />}
      </Box>}
      {s.extra.length > 0 && <Box key="extra" flexDirection="column" marginTop={1}>
        <Text bold>Listede olmayan değişiklikler</Text>
        {s.extra.map((text, i) => <Text key={`extra-${i}`} wrap="wrap">{`· ${text}`}</Text>)}
      </Box>}
      {GROUPS.filter(group => listItems(s).some(i => i.group === group)).map(group => (
        <Box key={`group-${group}`} flexDirection="column" marginTop={1}>
          <Text bold>{GROUP_LABEL[group]}</Text>
          {sight.visible.filter(i => i.group === group).map(i => <Box key={`row-${i.id}`} flexDirection="column">
            {choosing
              ? <Button key={`item-${i.id}`} plain label={rowLabel(i)} onPress={() => update($, improve, x => toggleOpen(x, i.id))} />
              : <Text key={`waiting-${i.id}`} wrap="wrap">{rowLabel(i) + (i.outcome?.status === 'waiting' ? ' … bekleniyor' : '')}</Text>}
            {s.open === i.id && <Box key={`details-${i.id}`} flexDirection="column" paddingLeft={2}>
              {i.group === 'own' ? <Text wrap="wrap">{i.title}</Text> : <Box flexDirection="column">
                <Text wrap="wrap">{`Gözlem: ${i.observation}`}</Text>
                <Text wrap="wrap">{`Neden: ${i.why}`}</Text>
                <Text wrap="wrap">{`Öneri: ${i.change}`}</Text>
                {i.cost && <Text wrap="wrap">{`Bedel: ${i.cost}`}</Text>}
                {i.evidence.length > 0 ? <Text wrap="wrap">{`Kanıt: ${i.evidence.join(', ')}`}</Text>
                  : i.group === 'problem' && <Text wrap="wrap">Kanıt verilmedi</Text>}
                {i.verified && <Text wrap="wrap">{`İnceleme: ${i.checked}`}</Text>}
              </Box>}
              {choosing && (i.group === 'own'
                ? <Button key="act-remove" label="Kaldır" onPress={() => update($, improve, x => removeOwn(x, i.id))} />
                : actions.map(a => <Button key={`act-${a}`} label={`${i.decision === a ? '● ' : ''}${DECISION_LABEL[a]}`}
                  hotkey={hotkey[a]} variant={i.decision === a ? 'primary' : 'secondary'} onPress={() => act(a)} />))}
            </Box>}
          </Box>)}
          {choosing && sight.hidden[group] > 0 && <Button key={`more-${group}`} plain label={`${sight.hidden[group]} madde daha`}
            onPress={() => update($, improve, x => x.phase !== 'choosing' || x.unfolded.includes(group)
              ? x : { ...x, unfolded: [...x.unfolded, group] })} />}
        </Box>
      ))}
      <Box key="submission" flexDirection="column" marginTop={1}>
        {input}
        {choosing ? n > 0
          ? <Button key="send" hotkey="g" variant="primary" label={`Gönder (${n})`} onPress={() => send($, 'selection', background)} />
          : <Text key="select" wrap="wrap">{WORDS.select}</Text>
          : <Box flexDirection="column">
            <Text wrap="wrap">{WORDS.sent}</Text>
            <Button key="stop-waiting" variant="secondary" label="Beklemeyi bırak"
              onPress={() => update($, improve, x => x.phase === 'sent' ? stopWaiting(x) : x)} />
          </Box>}
      </Box>
      {(!e.props.isFocused || footer.length > 0) && <Text key="footer" wrap="wrap" dimColor>
        {e.props.isFocused ? footer.join(' · ') : 'Seçmek için ctrl+x tab'}
      </Text>}
    </Box>
  })

}
