// The language the person wants Claude to answer in, and the line that tells Claude. The pane's own words stay
// Turkish; this is only what the model is asked, so any language works with no catalogue of translations.

export const LANG_AUTO = 'auto'
export const LANG_TURKISH = 'Turkish'
export const LANG_ENGLISH = 'English'
const LANG_MAX = 40

const KNOWN: Record<string, string> = {
  tr: LANG_TURKISH, turkish: LANG_TURKISH, türkçe: LANG_TURKISH, turkce: LANG_TURKISH,
  en: LANG_ENGLISH, english: LANG_ENGLISH, ingilizce: LANG_ENGLISH, 'i̇ngilizce': LANG_ENGLISH,
}
const AUTO_WORDS = new Set([LANG_AUTO, 'otomatik', 'oto', 'off', 'kapalı', 'default'])

export function asLanguage(value: unknown): string {
  if (typeof value !== 'string') return LANG_AUTO
  const name = value.replace(/[^\p{L}\p{M}\s'’().,-]/gu, '').replace(/\s+/g, ' ').trim().slice(0, LANG_MAX).trim()
  if (name === '' || AUTO_WORDS.has(name.toLowerCase())) return LANG_AUTO
  return KNOWN[name.toLowerCase()] ?? name
}

export function languageCommand(arg: string): string | undefined {
  return arg.trim() === '' ? undefined : asLanguage(arg)
}

export function languageLabel(lang: string): string {
  if (lang === LANG_AUTO) return 'Otomatik'
  return lang === LANG_TURKISH ? 'Türkçe' : lang
}

export function languageNote(wanted: string, sent: string | undefined): string | undefined {
  if (wanted === (sent ?? LANG_AUTO)) return undefined
  if (wanted === LANG_AUTO) {
    return '[Verinoda language] The earlier language request no longer applies: answer in the language the person writes in.'
  }
  return (
    `[Verinoda language] The person wants answers in ${wanted}. Write your whole answer in ${wanted}, and when you offer ` +
    `final options or ask the person to choose, write each option in ${wanted} as well. Keep code, commands, file paths ` +
    'and identifiers as they are.'
  )
}

export function nextLanguage(lang: string): string {
  if (lang === LANG_AUTO) return LANG_TURKISH
  return lang === LANG_TURKISH ? LANG_ENGLISH : LANG_AUTO
}
