# Working on verinoda-live (for coding agents)

verinoda-live is a Claude Code mod: a plugin of function hooks that runs inside the session beside the agent. This note is
what you need before changing it. The feature to build next is specified in `IMPROVE-PANE.md` beside this file.

## What a mod is

- `.claude-plugin/plugin.json` is the manifest (name, version, `userConfig` settings, `"types": "./types/index.d.ts"`).
- `hooks/hooks.json` names one module, `hooks/register.tsx`. It exports `register(on, options)`; `options` holds the
  `userConfig` values. `on(event, matcher?, hook)` adds a hook, and every hook is `($, e, next)`: `$` is the engine interface, `e` the
  event's input (plain, frozen), `next(e)` runs the plugins beneath and then the engine. A hook that returns without calling
  it answers alone; `next({ ...e, x })` rewrites what the rest sees.
- The module has **no DOM and no Node**: files, processes, the clock, the store, the prompt and the screen are all reached
  through `$`. A module that holds `import()` does not load. Files of the mod are imported with `import` declarations.
- JSX compiles against the global `h`. Elements are not globals: take them from the surface's table in the hook,
  `const { Box, Text, Button, Input } = $.ui.resolve(e)`.

## The API reference: read it, do not guess

- **`.claude-plugin/types/claude-code/index.d.ts`** (about 15,000 lines) declares every event's input and result, every call
  on `$` with a doc comment and an example, and every element's props. It is the authority. Grep it for the name at hand:
  `export type ButtonProps`, `export type InputProps`, `export type PaneOpenArgs`, `export type PromptSubmitArgs`,
  `export type ToolSpec`, `export type CommandRunResult`, `Pane: {`, `'tool.call'`.
- Claude Code writes that folder each time it loads the mod, and git ignores it. If it is missing, start
  `claude --plugin-dir claude-mods/verinoda-live` once in a terminal (or ask the person to). Written against Claude Code
  2.1.289; the API is early access and moves between releases, so where this note and the declaration file disagree, the
  file wins.
- `claude-mods/.local-reference/plugin-authoring/`, when it exists, is a local copy of Claude Code's own authoring guide
  (its reference) and of three small complete examples (a pane, a band, a tool-call hook). It is not in the repository.

## Layout

| file | holds |
|---|---|
| `hooks/register.tsx` | the wiring: every `on(...)`, the session state (atoms), the module state (`live`, `cfg`), the pane's drawing |
| `hooks/assist.ts` | the pure part of the assist features (parsing, rendering, tool specs); `hooks/assist.test.ts` tests it and its wiring |
| `hooks/mascot.tsx` | a `Client` module: the animated mascot |
| `types/index.d.ts` | the contract: `PluginState['verinoda-live']` (every `$.state` key) and `McpToolInputs` (the tools' arguments) |
| `hooks/*.test.ts` | the tests, run by `claude plugin test` |

A new feature follows `assist`: its logic that needs no `$` goes in a file of its own with its own tests, and `register.tsx`
gets as little as wires it in. `register.tsx` is 1,240 lines; do not grow it by a feature's whole body.

## Checks: all three pass before you say it is done

Run from the repository root.

```
claude plugin validate claude-mods/verinoda-live        # ends with "Validation passed"
claude plugin test claude-mods/verinoda-live            # 88 pass, 0 fail in 5 files before your change
npx --yes -p typescript tsc -p claude-mods/verinoda-live   # exit 0 and no output
```

`validate` also lists what the module hooks, calls, reads and writes: read that list after a change, it is the quickest
sign that the engine sees what you meant. It holds every `$.state` key the module names to the contract in
`types/index.d.ts`.

## How the tests work

A test gets the engine's `$` and an `on` whose hooks sit **beneath** the plugin and stand for the engine and the machine:
what they answer is what the plugin's `$` calls get. The existing files build that world in one function (`world(on)`), then
drive the plugin through `$`:

```ts
import { describe, expect, mock, test } from 'claude-code/testing'

test('the pane draws the settings and a press changes one', async ($, on) => {
  const clock = mock.clock(on)          // $.clock under the test's control
  mock.store(on, { auto: 'nudge' })     // $.store with what an earlier session chose (mock.store(on): empty)
  world(on)                             // fs, process.run, prompt.submit, tool.call ... answered here
  await $.session.start({ cwd: 'C:/work/proj', surface: 'terminal', isInteractive: true } as never)
  await clock.settle()                  // the background work session.start began has answered
  const pane = await $.ui.mount({
    plugin: 'verinoda-live', surface: 'terminal', component: 'Pane', requestId: 'verinoda',
    props: { title: 'Verinoda', isFocused: true, bodyColumns: 48, placement: 'dock', scroll: { offset: 0, bodyRows: 40 } } as never,
  })
  expect(await pane.find({ text: /✓ Index güncel/ })).toBeDefined()
  expect((await pane.find({ key: 'auto-off' }))?.props.label).toBe('Kapalı')
  await $.ui.press({ plugin: 'verinoda-live', key: 'auto-off' })            // a Button, by its key
  expect((await pane.find({ key: 'auto-off' }))?.props.label).toBe('● Kapalı')
})
```

An `Input` a feature draws is typed into with `await $.ui.input({ plugin: 'verinoda-live', key: '<its key>', text: 'hello' })`
(Enter with that text; `kind: 'change'` for an edit without Enter).

`$.command.run({ command: 'verinoda-panel', args: '' } as never)` runs a command;
`$.tool.call({ tool: 'mcp__verinoda-live__locate', text: '...' } as never)` calls a tool as the model would;
`$.prompt.submit(...)` raises a prompt; a hook the test registers on `prompt.submit` sees what the plugin submitted. Read
`hooks/features.test.ts` (the pane, on terminal and desktop) and `hooks/assist.test.ts` (tools) before writing a test.

## Conventions of this mod

- **The pane's text is Turkish; a command's output and everything the model reads is English.**
- Every state has a glyph and words; colour only reinforces them. A control that would do nothing is not drawn.
- What a drawing reads lives in `$.state`: `const x = atom({ plugin: 'verinoda-live', key: 'x' } as const, initial)`,
  `read($, x)` while drawing, `update($, x, fn)` from a handler; the write redraws the readers. Declare the key in
  `types/index.d.ts`. A module variable is lost on a reload; `$.state` (the session's) and `$.store` (across sessions) stay.
  A reload (a saved file, a changed setting) runs `register` and the `session.start` hook again.
- A setting starts from `userConfig` (`options`), and a choice the person made with a command is kept in `$.store` and wins,
  **in interactive sessions only** (`loadSettings($, isInteractive)`).
- Work that must not hold up a hook is started with `background(promise)`.
- Every element that can be pressed, typed into or found by a test has a `key`.
- Comments say why (and what was measured), not what the next line does.

## Facts about the host that cost time to find

1. **A plugin's tool is deferred behind ToolSearch unless a hook says otherwise**, and a tool the model must look up first is
   not called. `tool.describe` answers `{ description, isDeferred: false }` for the mod's tools (see the hook in
   `register.tsx`); add a new tool to its matcher.
2. **A plugin tool's result must be a plain string** (`{ result: 'text' }`). Any other shape fails the engine's check.
3. **`$.store` is one file per plugin, shared by every session that loads the mod and rewritten by each at its end.** A
   choice stored in one terminal once reached scripted sessions that never asked for it. A session with nobody at the
   keyboard (`session.start`'s `e.isInteractive === false`: `claude -p`, the SDK) reads no stored choice.
4. `$.http.fetch` has a short time limit of its own; an answer that takes tens of seconds is dropped. Slow work goes through
   `$.process.run(argv, { cwd, timeoutMs })` (30 s by default, ten minutes at most).
5. A hook has 10 s of its own time per dispatch; time spent inside a `$` call or `next(e)` does not count, except
   `$.clock.sleep`, which does.
6. **A pane opened because the person asked (the hook of a command they typed or a prompt they entered, a Button, Input or
   Select they worked) is placed at any width; one opened unasked (a timer, `session.start`, a queued prompt, `focus`) is
   placed only from 144 terminal columns** and otherwise waits undrawn (`$.ui.open` resolves `{ isPlaced: false }`). The
   floor is 110 columns for a pane id the person opened before and did not close by hand, and opening an id that is already
   open only retitles it and answers `isPlaced: true`. A model's tool call is in neither list; treat it as unasked and handle
   both answers. Open a pane from the command the person typed.
7. `$.prompt.submit({ text })` queues a prompt that starts a turn of its own once the session is idle; it is never folded
   into a running turn. The call resolves when that turn starts or when the prompt was queued behind a running one, so its
   resolution does not say the turn began: `turn.start` (`{ text, turnId }`) does, and `turn.complete` carries the same
   `turnId`. **Do not await it inside a `command.run` hook or a press handler** (the turn may not be able to start until the
   hook returns; this was not tried): start it with `background(...)`.
8. A plugin's prompt is shown to the model as "the plugin sent a message" unless it is submitted with `asUser: true`, which
   the model reads bare, as the person's own words (the transcript still names the plugin, and an `@file` mention in a
   plugin's prompt is not expanded). This mod's own `prompt.submit` hook treats an `asUser` plugin prompt as the person's
   (`isPersonsPrompt`): it starts a task for the gate and may attach auto-context. A prompt the mod itself submits must be
   let through that hook untouched.
9. `/verinoda` belongs to the Verinoda agent skill, which takes the slash before a plugin command: the mod's commands are
   `verinoda-<word>`.
10. Surfaces differ: `mobile` has no `Input` and no `Select`; narrow on `e.surface` and draw without them there. A tree that
    does not validate is not drawn at all, and only the debug log says why (`claude --debug`).
11. On the terminal a focused pane gives Tab to walk its elements, Enter to press, the arrows to scroll (the engine owns the
    scroll), and a Button's `hotkey` (one digit or one lowercase letter). The person gives it the keyboard with ctrl+x tab or a
    click; `$.ui.open({ focus: true })` is a request the surface may refuse.

## What you cannot check, and must say so

Nothing here can click a pane in a real terminal, and a scripted session (`claude -p`) has no person. The tests mount the
pane and press its keys, which shows the logic; how it feels in a terminal (focus, scrolling a long list, wrapping at 40
columns) only a person sees. List what you could not check in your final message instead of calling it done.
