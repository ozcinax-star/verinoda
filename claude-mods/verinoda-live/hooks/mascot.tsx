import type { ClientModule } from 'claude-code'

// Claude's mascot in purple, reading the code through its glasses: 20 x 8, block glyphs only (single cells in
// Cascadia Mono), every body cell a full block so the background never shows through the face. Animated the way
// Claude Code's mascot is: it blinks every few seconds and shuffles its feet; while Verinoda works (`busy`) it reads,
// a glint sweeping across its lenses, its feet and arms quicker. Drawn by the surface (a `Client`), so a frame
// repaints only this region: no render pass, no hook.

export type Mood = 'idle' | 'busy' | 'warn' | 'err'
export type MascotProps = { mood: Mood; caption: string; captionColor: string; captionDim: boolean }
export type Segment = readonly [string, string | null] // text, colour (null: uncoloured padding)

const HEAD = '#a78bfa'
const BODY = '#8b5cf6'
const SIDE = '#7c3aed'
const FRAME = '#5b21b6'
const LENS = '#1e1b4b'
const GLINT = '#f5f3ff'
const BLUSH = '#f0abfc'
const MOUTH = '#4c1d95'
const BELLY = '#6d28d9'

export const MASCOT_WIDTH = 20
export const MASCOT_HEIGHT = 8
export const TICK_MS = 120

const BLINK_EVERY_MS = 4_200 // a blink at the end of each cycle
const BLINK_MS = 160
const STEP_MS: Record<Mood, number> = { idle: 840, busy: 240, warn: 840, err: 840 }
const SWEEP_MS = 360 // busy: the glint moves one cell along the lens

// One lens on the glasses row: five cells, the glint at `at` (0-4), the rest dark; closed, the lid in head colour.
function lens(at: number, isClosed: boolean): Segment[] {
  if (isClosed) return [['█████', HEAD]]
  return [0, 1, 2, 3, 4].map(i => (i === at ? ['█', GLINT] : ['█', LENS]) as Segment)
}

// The frame `t` ms into the animation for this mood, as rows of coloured segments, every row 20 cells.
export function frameRows(t: number, mood: Mood): Segment[][] {
  const isBlink = t % BLINK_EVERY_MS >= BLINK_EVERY_MS - BLINK_MS
  const step = Math.floor(t / STEP_MS[mood]) % 2
  const glint = mood === 'busy' ? Math.floor(t / SWEEP_MS) % 5 : 0
  const arms: [string, string] = mood === 'busy' && step === 1 ? ['▝█', '█▘'] : ['▐█', '█▌']
  const feet = step === 0 ? '▀▀ ▀▀    ▀▀ ▀▀' : '▀▀  ▀▀  ▀▀  ▀▀'
  return [
    [['   ', null], ['▄▄▄▄▄▄▄▄▄▄▄▄▄▄', HEAD], ['   ', null]],
    [['  ', null], ['████████████████', HEAD], ['  ', null]],
    [[' ', null], ['─', SIDE], ['█', FRAME], ...lens(glint, isBlink), ['████', FRAME], ...lens(glint, isBlink), ['█', FRAME], ['─', SIDE], [' ', null]],
    [['  ', null], ['█', FRAME], ['█████', isBlink ? HEAD : LENS], ['████', HEAD], ['█████', isBlink ? HEAD : LENS], ['█', FRAME], ['  ', null]],
    [['  ', null], ['█', HEAD], ['██', BLUSH], ['████', HEAD], ['██', MOUTH], ['████', HEAD], ['██', BLUSH], ['█', HEAD], ['  ', null]],
    [[arms[0], SIDE], ['████████████████', BODY], [arms[1], SIDE]],
    [['  ', null], ['████████████████', BELLY], ['  ', null]],
    [['   ', null], [feet, SIDE], ['   ', null]],
  ]
}

// While busy the caption's dots cycle ("Kodu okuyorum.", "..", "..."); otherwise it is shown as given.
export function captionAt(t: number, mood: Mood, caption: string): string {
  if (mood !== 'busy') return caption
  const base = caption.replace(/[.…]+$/, '')
  return base + '.'.repeat(1 + (Math.floor(t / 400) % 3))
}

type State = { t: number }

const Mascot: ClientModule<MascotProps, State> = (props, surface) => {
  const { Box, Text } = surface.elements
  if (surface.state === undefined) {
    // one clock for the instance's life: started on the first call, never again
    let t = 0
    surface.every(TICK_MS, () => {
      t += TICK_MS
      surface.setState({ t })
    })
    surface.setState({ t: 0 })
  }
  const t = surface.state?.t ?? 0
  return (
    <Box flexDirection="column" alignItems="center">
      {frameRows(t, props.mood).map((row, i) => (
        <Text key={`r${i}`} wrap="truncate">
          {row.map(([text, color], j) => (color === null ? text : <Text key={`r${i}s${j}`} color={color}>{text}</Text>))}
        </Text>
      ))}
      <Text color={props.captionColor} dimColor={props.captionDim} wrap="truncate">{captionAt(t, props.mood, props.caption)}</Text>
    </Box>
  )
}

export default Mascot
