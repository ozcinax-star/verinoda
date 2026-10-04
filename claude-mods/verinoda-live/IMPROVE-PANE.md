# The improvement checklist pane: step 1 (backlog 13.9)

Status: to build. Decided 2026-10-04. This is the brief for whoever builds it. Read `AGENTS.md` beside this file first: it
holds how the mod is laid out, the three checks, how the tests work and the facts about the host this brief leans on.

## What it is for

People ask "make this better", "higher quality", "nicer" without saying what should change. A model that guesses changes
things nobody asked for; a model that asks open questions tires the person. This feature turns the vague request into a
list the person can recognize their wish in:

1. The person asks for the list (`/verinoda-improve`).
2. The model looks at the thing and **changes nothing**. It hands the mod a ranked list of what could change.
3. The pane shows the list in three groups. Nothing is selected.
4. The person marks items: apply, keep, or (for a suspected problem) check first. They may add items of their own.
5. The person sends. Only then does the model work, on what was chosen.
6. The pane shows what happened to each item.

## The rules that decide every open question

1. **Nothing changes without the person's decision.** Opening the pane is not consent. The list is not consent.
2. **Nothing is selected at the start.** An item without a decision means "not decided". It is never treated as rejected,
   never sent as "keep", never recorded.
3. **A suspicion is not a fact.** A `problem` item shows its evidence, or says on its row that it has none. A `taste` item the
   model did not judge from a rendered view says on its row that it is a guess. Choosing "check first" asks for an
   investigation, not a change.
4. **The first sight is short.** The list may be long, the pane shows the seven best-ranked items and folds the rest. The
   model is told not to pad.
5. **The boundary of the work is what the person chose.** The model may make the small auxiliary changes a chosen item
   needs, and reports every change that was not on the list.
6. **Only on request.** Nothing of this runs, and no tool of it is listed to the model, until the person runs the command
   (or turned on the `improveOffer` setting).
7. **A session with nobody at the keyboard gets none of it.** "Nobody at the keyboard" is `session.start`'s
   `e.isInteractive === false`, as everywhere in this mod. (What that flag is in a session the desktop app hosts is not known
   here: see the unknowns at the end. The pane is drawn for the `desktop` and `mobile` surfaces all the same, since such a
   surface can be attached to a terminal session.)

## What is in step 1, and what is not

In: the command, two tools for the model, the pane (three groups, a short first sight, details per item, three decisions,
the person's own items, send, outcomes), outcomes that never came, and one optional line in the system prompt.

Not in step 1; do not build these, the later steps are decided and come after this one works in a real session:

- step 2: single-choice groups for alternatives that exclude each other, items nested under an outcome, a free-text
  redirection that makes the model redo the list while the selections are kept, warnings about conflicting or dependent items;
- later: previews and a screenshot loop for the `taste` group; a remembered "keep".

Step 1 writes nothing to `$.store`, calls no Verinoda command, needs no Verinoda index and changes no Python.

## Words used below

- An item's **outcome** is `null`, in flight (`waiting`), final (`applied`, `partial`, `failed`, `not_confirmed`) or
  `unreported` (its turn ended and the model said nothing about it). `waiting` and `unreported` still accept a report; a final
  outcome accepts none.
- An item is **sendable** when its decision is `apply` or `check` and its outcome is `null`.
- The **list** is the items whose outcome is `null` or `waiting`. The **results** are the items whose outcome is final or
  `unreported`. An item is in one or the other.
- A check that confirmed a problem is not an outcome: it puts the item back in the list (see `improve_report`).

## The flow

The feature's whole state is one `$.state` value (see "State"). Its `phase`:

| phase | means | leaves it |
|---|---|---|
| `idle` | no list | the command starts a review: `reviewing`. A list arrives (`improve_propose`): `choosing` |
| `reviewing` | the model was asked for a list | a list arrives: `choosing`. The command again: `reviewing` anew |
| `choosing` | the list is shown and can be marked | the person sends: `sent`. A new list arrives: `choosing` with that list |
| `sent` | the model is working on what was sent | no item is `waiting` any more (reports, the turn's end, or the person stops waiting): `choosing` |

After outcomes arrive the phase is `choosing` again: the results are shown above the list, and what is left in the list can
still be marked and sent.

## The command

Registered at `session.start` with the mod's other commands:

- name `verinoda-improve`, argument hint `[what to look at]`,
- description `List what could be changed here and choose what to apply; nothing changes until you choose.`

Its `command.run` hook, with `what = e.args.trim()`; the first row that fits decides:

| when | does | answers (`text`) |
|---|---|---|
| the session is not interactive | nothing | `verinoda improve needs a person to choose: it does nothing in a scripted session` |
| phase `sent` | opens the pane | `a selection is being applied; its outcomes will show in the pane` |
| phase `choosing`, `what` is empty, and the list is not empty or an item is `unreported` | opens the pane | `improvement list opened (N items)`, N the list's size |
| otherwise | starts a review (below) | `looking at it; the list will appear in the pane, and nothing is changed until you choose` and, when the model's items of the old list held a decision, ` (the earlier list of N items was replaced)`, N the number of the model's items it had |

So a finished list (every item has a final outcome) does not hold the command: the next bare `/verinoda-improve` starts over.

Starting a review:

1. Register the two tools (`$.tool.register`, awaited). A name registered again is replaced, so no flag is kept; the
   declaration says a tool is callable "from the next prompt on", which is the review prompt.
2. Set the state to `reviewing` with `subject: what`, no model items, no results, `extra` empty, `open` null, `unfolded` empty,
   `turn` null, `note` ''. The person's own items that were not sent are kept (and the draft in the field).
3. `await $.ui.open({ id: 'verinoda-improve', title: 'İyileştirme', focus: true, rows: 16 })`. The person's command is
   behind it, so it is placed at any width.
4. Submit the review prompt (see "The prompts the mod submits").

`what` is passed on as text: an `@file` mention in it is not expanded for a plugin's prompt.

`live` needs to know whether the session is interactive: keep `e.isInteractive !== false` from `session.start` (it is only
passed on to `loadSettings` today).

## The tools

Both are registered only in an interactive session: by the command, and at `session.start` (which also runs after a reload
of the module) when `improveOffer` is on or the `improve` state's phase is not `idle`. Both are added to the `tool.describe`
hook that answers `isDeferred: false`. Both answer a plain string. Add their arguments to `McpToolInputs` in
`types/index.d.ts`. Validate the input in the hook: do not assume the schema was enforced. An optional field of the wrong
type is treated as absent (`cost` `''`, `evidence` `[]`, `seen` false; a report's `change` and `evidence` ignored), and a
non-string or empty entry of `evidence` is dropped: none of these is a refusal.

### `improve_propose` (the model calls `mcp__verinoda-live__improve_propose`)

Description, verbatim:

> Show the user a list of what could be changed, in the Verinoda pane, so that they choose. Call it only when the user asked
> for improvement options or a review list, in words or with /verinoda-improve; never on your own initiative. Before calling,
> look at the code or the screen in question and change nothing. Rank the items, the most worth doing first, and do not pad
> the list. Each item is one change the user can say yes or no to. `group`: `problem` is a possible bug, inconsistency or
> unexpected behaviour (give `evidence`, the file:line you read); `improvement` is usability, readability, performance or
> upkeep; `taste` is look, tone, density, colour or layout, a preference and not a defect (set `seen` to true only if you
> judged it from a rendered view or a screenshot). `title` says the change in one concrete line (not "improve the hierarchy"
> but "make Save the only primary button"); `observation` is what is there now; `why` is why it may matter; `change` is
> exactly what you would do and what stays as it is; `cost` is what it costs or risks, if anything. After the call, stop:
> change nothing and wait for the user's selection, which arrives as a message.

Input schema:

```json
{
  "type": "object",
  "properties": {
    "subject": { "type": "string", "description": "What was looked at, in a few words: a file, a screen, a module" },
    "items": {
      "type": "array", "minItems": 1, "maxItems": 30,
      "items": {
        "type": "object",
        "properties": {
          "id": { "type": "string", "description": "Short and unique in this list: i1, i2, ..." },
          "group": { "enum": ["problem", "improvement", "taste"] },
          "title": { "type": "string" },
          "observation": { "type": "string" },
          "why": { "type": "string" },
          "change": { "type": "string" },
          "cost": { "type": "string" },
          "evidence": { "type": "array", "items": { "type": "string" } },
          "seen": { "type": "boolean" }
        },
        "required": ["id", "group", "title", "observation", "why", "change"]
      }
    }
  },
  "required": ["subject", "items"]
}
```

Validation, in this order; the first failure is the answer and nothing is stored:

- the session is not interactive: `verinoda improve: nobody is at the keyboard to choose; ask in text instead`
- phase `sent`: `verinoda improve: a selection is being applied; report its outcomes with improve_report before proposing a new list`
- `items` is not an array of 1 to 30 objects; an `id` is empty, repeated, not `[A-Za-z0-9_-]{1,16}`, or of the form `u` and
  digits (kept for the person's own items); a `group` is not one of the three; or `title`, `observation`, `why` or `change` is
  not a non-empty string: `verinoda improve: <what is wrong, naming the item>; nothing was shown`

What is stored:

- the items in the order given (the order is the rank), strings trimmed, `title` cut at 120 characters and the other texts at
  600 (a cut ends in `…`), `evidence` at most 6 strings of at most 160 characters, every decision `none`, every outcome `null`;
- `subject`: the tool's, trimmed and cut at 80 characters; when that is empty, the command's `what`; when that is empty too,
  `bu konuşma`;
- a new list replaces the model's items with their decisions, the results, `extra`, `open` and `unfolded`. The person's own
  items that were not sent are kept.

`note` becomes `''`. Then the pane is opened (`$.ui.open`, the same id). When the command opened the pane and it is still
open, the call only retitles it and answers `isPlaced: true` (the declaration says so). Whether a pane that a model's tool call
opens counts as "asked" is not stated there: this brief infers it does not, so when the person closed the pane by hand, or the
tool was called without the command (`improveOffer`), the call may answer `{ isPlaced: false }` in a narrow terminal. Handle
both answers; the real placement on that path is for a person to check.

Answer when stored; when the model's items of the list it replaced held a decision, the answer ends with
` The earlier list (N items, D decisions) was replaced.`, N the number of the model's items it had and D how many of them
had a decision other than `none` (the person's own items are in neither number):

- placed: `Shown to the user: N items (P possible problems, I improvements, T matters of taste). Stop here: change nothing and wait for their selection, which arrives as a message.`
- not placed: `The list is stored (N items) but the pane is not on screen. Tell the user to run /verinoda-improve to see it. Change nothing meanwhile.`

### `improve_report` (the model calls `mcp__verinoda-live__improve_report`)

Description, verbatim:

> Report what happened to each item the user sent from the Verinoda pane, once you are done or cannot go on. One outcome per
> item id you were given. For an item to apply: `applied` (done as described), `partial` (say what is missing) or `failed`
> (say why). For an item to check first: `confirmed` (the problem is real; put the fix you propose in `change` and do not
> apply it) or `not_confirmed` (you looked and found nothing that needs a change). `note` is one or two sentences the user
> reads; `evidence` is what shows it: the check you ran and its result, a file:line. List under `extra_changes` every change
> you made that was not on the list.

Input schema:

```json
{
  "type": "object",
  "properties": {
    "outcomes": {
      "type": "array", "minItems": 1,
      "items": {
        "type": "object",
        "properties": {
          "id": { "type": "string" },
          "status": { "enum": ["applied", "partial", "failed", "confirmed", "not_confirmed"] },
          "note": { "type": "string" },
          "change": { "type": "string" },
          "evidence": { "type": "array", "items": { "type": "string" } }
        },
        "required": ["id", "status", "note"]
      }
    },
    "extra_changes": { "type": "array", "items": { "type": "string" } }
  },
  "required": ["outcomes"]
}
```

Validation, in this order; a call with any invalid outcome records nothing and says which:

- `outcomes` is not an array of at least one object: `verinoda improve: outcomes must list at least one item; nothing was recorded`
- an `id` given twice in the call: `verinoda improve: item <id> is reported twice`
- an `id` whose item's outcome is not `waiting` or `unreported` (never sent, already final, or no such item):
  `verinoda improve: no item <id> is waiting for an outcome`
- a `status` that is none of the five, or one that does not fit how the item was sent (`confirmed` or `not_confirmed` for an
  item sent to apply; `applied`, `partial` or `failed` for one sent to check):
  `verinoda improve: item <id> was sent to <apply|check>; its outcome is one of <the statuses that fit>`
- an empty `note`: `verinoda improve: item <id> needs a note the user can read`

`extra_changes` entries that are not non-empty strings are dropped without a refusal.

What it does (texts cut as for a proposed list):

- `applied`, `partial`, `failed`, `not_confirmed`: the item's outcome becomes that, with the note and the evidence. It is a
  result now.
- `confirmed`: the item goes back into the list, markable: `verified` true, `checked` the note, outcome `null`, decision
  `none`, `sentAs` null, `change` replaced when one was given, and the report's `evidence` appended to the item's own (cut as
  for a list), so that `Kanıt verilmedi` is drawn only for an item that still has none. It accepts no report until it is
  sent again.
- `extra_changes` are appended to the state's `extra` (at most 20 strings of 300 characters).
- When no item is `waiting` any more, the phase goes back to `choosing`.

Answer: `Recorded: N outcomes.` and, when some are still waiting, ` Still waiting for: i4, i7.`

## The prompts the mod submits

Three prompts, all through `$.prompt.submit({ text, asUser: true })`: each is the direct result of the person's own command
or press and carries their decision. With `asUser` the model reads the text bare, as the person's words; the transcript
still names the plugin.

Rules for all three:

- **Never awaited** inside the command's hook or a press handler: start it with `background(...)`.
- **The mod's own `prompt.submit` hook passes them on untouched** (no task start for the gate, no auto-context, no `locate`):
  test the origin first, `e.origin?.kind === 'plugin' && e.origin.name === 'verinoda-live'`, and `return next(e)`.
- **Knowing which turn is theirs.** A prompt a plugin submits is queued and starts a turn of its own once the session is idle;
  a turn that is running when the person presses send is another turn, and its end must not count. So: **at the moment a
  prompt is submitted** (the review start, send, `Sonucu iste`) the state's `expect` names it (`review`, `selection` or
  `report`) **and `turn` is set to null**, so that only the newest prompt's turn is tracked. A `turn.start` hook compares the
  turn's `text` with the first line of the expected prompt (`startsWith`, after trimming): on a match it stores the turn's
  `turnId` in `turn` and clears `expect`. The `turn.complete` hook acts only when `e.agentId === undefined && e.turnId === turn`,
  and then clears `turn` (see "When the mod's turn ends"). A subagent raises no `turn.start`.
- **A prompt that did not enter.** When the submit's promise resolves to `{ drop }` or rejects: clear `expect` and set `note`
  to `'not-sent'`, and then:
  - a review: back to `idle`;
  - a selection: the items it had marked go back as they were before the press (`sentAs` null, outcome `null`, the decision
    kept), and the phase to `choosing`, so that the same marks can be sent again;
  - a report request: its `waiting` items go back to `unreported` (they were, before the press), and the phase to `choosing`.

The review (`reviewPrompt(what)`); the second line is `What to look at: <what>` or, when `what` is empty, the sentence shown:

```
Make a list of what could be changed here so that I can choose. Do not change anything yet.
What to look at: what we have been working on in this conversation. If that is not clear, ask me one question first.

Look at it first: read the code, and if it has a rendered view you can look at, look at it. Then call
mcp__verinoda-live__improve_propose with the items, ranked, the most worth doing first. Do not pad the list. Then stop and
wait for my selection.
```

The selection (`selectionPrompt(state)`), built from the sendable items and, under "Keep", the list's items whose decision is
`keep`. A section with no item is left out; an item of the person's own reads `- [u1] <their text> (my own item)`; an item
that is not sendable (undecided, or already sent) is never named:

```
My decisions on the improvement list (<subject>):

Apply these:
- [i3] <title>: <change>

Check these first (investigate only; change nothing for them):
- [i5] <title>

Keep these as they are (do not change them, also not as a side effect):
- [i2] <title>

Everything else stays as it is. You may make the small auxiliary changes an item needs. If one would add behaviour, change
behaviour I did not choose, or cost something, tell me before you do it. When you are done or cannot go on, call
mcp__verinoda-live__improve_report with one outcome for each item to apply or to check, and list under extra_changes any
change that was not on the list.
```

Asking for outcomes that did not come (`reportPrompt(ids)`):

```
Report the outcome of the items I sent that you have not reported yet (<ids>) with mcp__verinoda-live__improve_report.
Change nothing else.
```

### When the mod's turn ends

On the `turn.complete` that is the mod's own turn (the rule above), whether it ended or was interrupted:

- phase `sent`: every item still `waiting` becomes `unreported`, and the phase goes back to `choosing`. The pane then shows a
  Button that asks for them. `improve_report` still accepts an `unreported` item's id later.
- phase `reviewing` (the review's turn ended and no list came; the model may have asked a question instead): the phase
  stays, `note` becomes `'no-list'`, and the pane says so.

Any other turn's end changes nothing.

## The optional line in the system prompt

A `userConfig` field `improveOffer` (boolean, default `false`, title `Offer the improvement list`, description `When a request
says "better" or "nicer" without saying what, Claude offers the improvement list instead of guessing. Off: the list comes
only with /verinoda-improve.`). When it is on, in an interactive session started in any folder:

- the two tools are registered at `session.start` (so that a "yes" in words is enough), and
- the `prompt.compose` hook adds one section, id `verinoda-live:improve`, scope `session`, verbatim:

> When the user asks for something to be better, nicer, cleaner or of higher quality without saying what should change, do
> not guess and do not start editing. Offer in one line to list what could be changed so that they choose:
> `/verinoda-improve` opens the list, and they may add what to look at. If they ask for the list in words, look first, then
> call `mcp__verinoda-live__improve_propose`. A request that says what to change is not vague: do it.

It is a plain setting: no command changes it and nothing about it is stored.

## State

One key, `improve`, in `PluginState['verinoda-live']`. The shape below is what the brief needs; names may change, the
contract in `types/index.d.ts` must declare whatever is used.

```ts
export type ImproveGroup = 'problem' | 'improvement' | 'taste' | 'own'
export type ImproveDecision = 'none' | 'apply' | 'keep' | 'check'
export type ImproveStatus = 'waiting' | 'applied' | 'partial' | 'failed' | 'not_confirmed' | 'unreported'

export type ImproveItem = {
  id: string // the model's id, or u1, u2, ... for the person's own
  group: ImproveGroup
  title: string // an own item: the person's text
  observation: string
  why: string
  change: string
  cost: string // '' when none was given
  evidence: string[]
  seen: boolean // taste: judged from a rendered view
  verified: boolean // a check confirmed it
  checked: string // the note of the check that confirmed it, else ''
  decision: ImproveDecision // an own item: always 'apply'
  sentAs: 'apply' | 'check' | null // what it was last sent as
  outcome: { status: ImproveStatus; note: string; evidence: string[] } | null
}

export type ImproveState = {
  phase: 'idle' | 'reviewing' | 'choosing' | 'sent'
  subject: string
  items: ImproveItem[] // the model's in rank order, then the person's own
  open: string | null // the item whose details are shown
  unfolded: ImproveGroup[] // groups whose folded items were opened
  extra: string[] // changes the model reported that were not on the list
  draft: string // what the own-item field holds
  ownSeq: number // the last number given to an own item; never reused in a session
  expect: '' | 'review' | 'selection' | 'report' // the prompt that was submitted and whose turn has not started
  turn: string | null // the id of the mod's own turn in flight
  note: '' | 'no-list' | 'not-sent'
}
```

It is session state: a reload keeps it. `/clear` ends the session without a `session.start` after it, and the declaration does
not say that `$.state` is dropped: add a `session.end` hook that, when `e.reason === 'clear'`, sets the state back to `idle`
with no items and clears `expect` and `turn`. A new session starts at `idle`.

`note` is set only by the three places that say so (a dropped prompt, a review's turn that ended with no list), and set to `''`
by the review start, `improve_propose`, send, `Sonucu iste` and `Beklemeyi bırak`.

Sending: every sendable item gets `sentAs` (its decision) and the outcome `waiting`; its decision stays, so its mark stays.

## The pane

Pane id `verinoda-improve`, title `İyileştirme`, a pane of its own beside the mod's `verinoda` pane (so their hotkeys do not
meet). Its text is Turkish. Size the tree to `e.props.bodyColumns`; the engine scrolls it.

### By phase

- `idle`: `Liste yok. /verinoda-improve [neye bakılsın] ile başlatın.`; with `note: 'not-sent'` a second line:
  `İstek gönderilemedi.`
- `reviewing`: `İnceleniyor… Claude listeyi hazırlıyor; hiçbir şey değiştirilmiyor.`; with `note: 'no-list'` instead:
  `Liste henüz gelmedi. Claude bir soru sormuş olabilir: yanıtlayın ya da /verinoda-improve ile yeniden başlatın.`
- `choosing`: the results and the list, as below; with `note: 'not-sent'` the line `İstek gönderilemedi.` above them.
- `sent`: the same drawing with nothing to mark: a row is a `Text` (the same mark and title, then ` … bekleniyor` on a waiting
  item), the open item's details stay shown without their action Buttons, and `more-<group>`, the `own` Input and `send` are
  not drawn, nor is `Sonucu iste` (results from an earlier send stay shown, without that Button, while another send is in
  flight: two pending prompts would not be told apart). In place of the send Button: the line
  `Seçimleriniz gönderildi; sonuçlar burada görünecek.` and one secondary
  Button, `Beklemeyi bırak` (key `stop-waiting`), which turns every `waiting` item to `unreported`, clears `expect`, `turn` and
  `note`, and goes back to `choosing` (the way out when the model never reports).

### The drawing in `choosing`

```
İyileştirme: <subject>
9 madde · 2 uygula · 1 kontrol · 1 kalsın

Sonuçlar                                      (only when there is a result)
  ✓ Uygulandı · <title>
      <note>
      <evidence, joined with ", ">
  [ Sonucu iste ]                             (only when an item is unreported)
Listede olmayan değişiklikler                 (only when the model reported some)
  · <text>

Olası sorunlar
  [ ] <title>
  [?] <title> (kanıtsız)
  3 madde daha
İşlevsel iyileştirmeler
  [✓] <title>
Estetik seçenekler
  [–] <title> (tahmin)
Sizin eklediğiniz
  [✓] <text>

Kendi maddeniz: ____________ (ekle)
[ Gönder (3) ]
g gönder · tab gezin · enter aç
```

- The counts line counts the list's items (the model's and the person's) and, of them, the sendable ones to apply, the
  sendable ones to check, and the ones marked keep. A count of zero is left out. With an empty list the line is not drawn.
- A group with no item in the list is not drawn. `Sizin eklediğiniz` is drawn only when the list holds an own item.
- **First sight**: of the list's model items, the seven best-ranked are shown, each under its group. Shown **in addition**:
  any item with a decision, any verified item, and the open item. A group's other items sit behind one Button,
  `N madde daha` (key `more-<group>`), where N counts only that group's items that are not drawn; pressing it shows that
  group whole until a new list arrives.
- An item's row is one plain Button, key `item-<id>`, label `<mark> <title>` and at most one suffix: ` (doğrulandı)` on a
  verified item; else ` (kanıtsız)` on a `problem` item with no evidence; else ` (tahmin)` on a `taste` item with `seen` false.
  Marks: `[ ]` not decided, `[✓]` apply, `[–]` keep, `[?]` check first. Pressing a row opens its details, and closes them
  when they are open. One item is open at a time.
- **The open item**, under its row, indented:

  ```
      Gözlem: <observation>
      Neden: <why>
      Öneri: <change>
      Bedel: <cost>                        (only when given)
      Kanıt: <evidence, joined with ", ">   (a problem with none: "Kanıt verilmedi"; another group with none: no line)
      İnceleme: <checked>                  (only on a verified item)
      [ Uygula ] [ Kalsın ] [ Önce kontrol et ]
  ```

  The Buttons' keys are `act-apply`, `act-keep`, `act-check`, their hotkeys `u`, `k`, `c`. `Önce kontrol et` is drawn only for
  a `problem` item that is not verified. The Button of the decision the item has is the primary one and its label starts with
  `● `; pressing it clears the decision (back to not decided). Pressing another sets that one.
- The person's own item, open: its text and one Button, `Kaldır` (key `act-remove`), which deletes it. Its decision is
  `apply` from the start: they wrote it because they want it.
- The Input, key `own`, label `Kendi maddeniz`, `submitLabel` `ekle`, drawn with `value` the state's `draft`. `onInput` keeps
  `draft`; `onSubmit` with a non-empty text adds an own item and sets `draft` to `''`, so that the next drawing draws another
  value and the field is empty. The item's id is `u<k>`, k being `ownSeq + 1` (and `ownSeq` becomes k): a removed id is not
  given again. Its text is cut at 300 characters. At most 10 own items in the list; the Input is not drawn beyond that. On
  the `mobile` surface there is no `Input`: draw the line `Kendi maddenizi eklemek için terminali ya da masaüstünü kullanın.`
- The send Button, key `send`, hotkey `g`, primary, label `Gönder (N)`, N the number of sendable items. With N at zero it is
  not drawn, and the line `Göndermek için en az bir madde seçin.` is (so "keep" alone sends nothing). Pressing it marks the
  sendable items sent (see "State"), sets the phase to `sent`, `expect` to `selection`, `turn` to null and `note` to `''`,
  and submits the selection prompt.
- **Results**. Glyph and words: `✓ Uygulandı`, `◐ Kısmen uygulandı`, `✗ Uygulanamadı`, `○ İncelendi: sorun doğrulanmadı`,
  `? Sonuç bildirilmedi`. A result is not a Button and cannot be marked. When any item is `unreported`, one Button under the
  results: `Sonucu iste` (key `ask-report`), drawn only in phase `choosing`, which puts those items back to `waiting`, sets
  the phase to `sent`, `expect` to `report`, `turn` to null and `note` to `''`, and submits the report prompt for their ids.
- The footer names only the keys that exist in this drawing. When the pane does not hold the keyboard it reads
  `Seçmek için ctrl+x tab` (the mod's other pane swaps its footer the same way, with its own words).
- Colour only reinforces: every mark and every status is readable without it.

## Files

- `hooks/improve.ts`: everything that needs no `$`: validating and storing a proposed list, the decisions, what is sendable,
  which items are in the first sight, the counts, applying a report, the three prompts and their first lines, the tool specs
  and descriptions, the pane's words.
- `hooks/improve.test.ts`: its tests and the wiring's tests, with a `world(on)` of its own in the shape of
  `assist.test.ts`'s. Its `ui.open` hook answers `{ value: { isPlaced: true } }` by default and
  `{ value: { isPlaced: false, reason: 'narrow' } }` on an option (the existing worlds answer no `isPlaced` at all).
- The pane's drawing and the hooks: a module of their own (for instance `hooks/improve-pane.tsx` exporting a function that
  `register` calls with `on` and what it needs from `register.tsx`), or, if that proves awkward with the module state,
  the smallest possible addition to `register.tsx`. Say which you chose and why.
- `types/index.d.ts`: the `improve` state key, the two tools in `McpToolInputs`.
- `.claude-plugin/plugin.json`: `improveOffer`, version `0.6.0`, and the description if it lists features.
- `README.md` of the mod: a row in the features table, the command, the setting, the tests' line.

## Tests to write

Pure:

1. A valid list is stored in the order given, texts trimmed and cut, every decision `none`; `subject` falls back to the
   command's `what`, then to `bu konuşma`.
2. Each invalid input (no items, 31 items, a repeated id, a bad id, the id `u1`, an unknown group, an empty title) is refused
   with a message naming the item, and the state is as it was.
3. First sight: of 12 items the seven best-ranked are visible and each group's Button counts only what is not drawn; a
   low-ranked item with a decision, a low-ranked verified item and a low-ranked open item are visible too; unfolding a group
   shows it whole.
4. Decisions: apply, keep, check; the same one again clears it; `check` is refused outside `problem` and on a verified item.
5. The selection prompt lists apply, check and keep under their headings, leaves out an empty section, never names an
   undecided item, marks the person's own items, and after a first send does not name an item that already has an outcome.
6. Optional fields of the wrong type (`cost` a number, `evidence` a string or with non-strings, `seen` a string) are
   treated as absent without a refusal. A report: each status lands on its item; a `confirmed` item is back in the list, verified, undecided, with the new `change`
   and the note as `checked`; refused whole: an unknown id, an id twice, a final item, a status that is none of the five, a
   status that does not fit how the item was sent, an empty note, no outcomes; `extra_changes` are kept and a non-string
   among them is dropped.

Wired (through `$`, on the terminal surface unless said):

7. In a scripted session the command answers that it needs a person, opens no pane, registers no tool and submits nothing.
8. In an interactive session the tools are not registered before the command and are after it; the command opens the pane
   and submits the review prompt as the person's with the argument in it. After a reload (a second `session.start`) with the
   phase not `idle`, the tools are registered again.
9. `tool.describe` answers `isDeferred: false` for both tools.
10. The mod's own prompts pass its `prompt.submit` hook untouched. Use an argument that is a code question
    (`/verinoda-improve where is the parser slow?`) with auto-context on `nudge`, so that the hook without the bypass would
    attach context: the review prompt gets none. With assist on `strict`, the first Grep after the mod's prompt is not
    answered by the gate (no task was started by it). The same for the selection and the report prompts.
11. `improve_propose` draws the three groups in order, ` (tahmin)` on an unseen taste item, ` (kanıtsız)` on a problem without
    evidence, nothing selected, no send Button and the line that asks for a selection. Its answer says "Shown" when the pane
    is placed and "stored ... not on screen" when it is not.
12. Pressing a row opens its details and the three Buttons; a problem without evidence shows `Kanıt verilmedi`; `act-apply`
    marks it and the send Button appears as `Gönder (1)`; an `improvement` item draws no `Önce kontrol et`; with only
    `act-keep` marked there is no send Button. Pressing `more-problem` shows the folded items.
13. Typing into `own` with `kind: 'change'` leaves the Input's `value` equal to the text; submitting it adds the item,
    selected, and the Input's `value` is `''` afterwards; `act-remove` deletes it; the next one gets a new id (`u2`, not `u1`
    again); the eleventh is not possible.
14. `send` submits the selection prompt as the person's and the phase is `sent`; no `item-<id>`, `own`, `send`,
    `ask-report` or `more-<group>` element is drawn; `stop-waiting` is, and pressing it shows the items as
    `Sonuç bildirilmedi`.
15. `improve_propose` during `sent` is refused and the list is as it was; during `choosing` it replaces the list, keeps the
    unsent own items, and its answer says the earlier list was replaced.
16. `improve_report` for one of two sent items answers with ` Still waiting for: <the other id>`, leaves the phase `sent`
    and the other row ` … bekleniyor`; reporting the other shows both results with their words and returns the pane to
    `choosing`; a second report for the same id is refused; `extra_changes` are drawn under `Listede olmayan değişiklikler`.
17. The mod's turn: a `turn.start` whose text is the selection prompt, then its `turn.complete`, with an item still waiting:
    `Sonuç bildirilmedi` and `Sonucu iste` are drawn; pressing it submits the report prompt; a report that then arrives for
    that item is accepted. Before that: the `turn.complete` of a turn that was running when send was pressed (another
    `turnId`) changes nothing; so does the `turn.complete` of an earlier tracked turn (a review's, whose `turn.start` matched)
    that is still running when send is pressed, and a subagent's `turn.complete` (an `agentId`) changes nothing.
18. The review's own turn ending without a list shows the "list has not come" line; a list that arrives afterwards
    replaces it; another turn's end during `reviewing` changes nothing.
19. A `confirmed` report through the wiring: the item is back in its group with ` (doğrulandı)`, its details show
    `İnceleme:`, and it draws no `Önce kontrol et`.
20. The command during `choosing` with no argument only opens the pane; with an argument it starts over and says the
    earlier list was replaced; during `sent` it starts nothing; with every item final and no argument it starts a new review.
21. A submit that resolves to `{ drop }`: a review goes back to `idle`; a selection's items are marked as before and
    sendable again, the phase `choosing`; a report request's items are `unreported` again; each with the `İstek gönderilemedi.`
    line, which the next review start, list, send, `Sonucu iste` or `Beklemeyi bırak` removes.
22. With `improveOffer` on, an interactive session registers the tools at start and `prompt.compose` carries the section;
    off, neither; a scripted session, neither. A `session.end` with `reason: 'clear'` resets the state (a list, a `sent`
    phase and `expect` are gone; the command then starts a review), and one with another reason does not.
23. The pane draws on `desktop` as on the terminal, and on `mobile` without the Input and with the line that says where to
    add an item.
24. At `bodyColumns: 30` the tree still validates (nothing assumes a wide pane).

## Before you say it is done

- The three checks of `AGENTS.md` pass; the 88 earlier tests still pass.
- Your changes are only under `claude-mods/verinoda-live/` and `docs/`. Leave `.claude/`, `p.out` and anything that was
  already modified as you found it.
- `docs/DESIGN.md` has a new last section, `## 148. The improvement checklist pane (D175, <date>)`, in the form of its
  neighbours: why, decisions, measured (the tests; say that no real session was run), not done (step 2, the later steps, what
  only a person can check), tests. `docs/UPGRADING.md` gets a `### D175: The improvement checklist pane` note after D174's,
  and its section heading becomes `## Upgrading from 0.4.0 (D137-D175)`.
- The row 13.9 of `docs/BACKLOG.md` stays, cut down to what is still to do. Step 1 is a part of the item, not the item: its
  "done when" is a real session, and steps remain. (The root `AGENTS.md` rule covers this: a row whose item shipped only in
  part is cut down to what is left.)
- Your last message lists what you could not check.

## What only a person can check

Ask the person to try these in a terminal with `claude --plugin-dir claude-mods/verinoda-live`, and report what they see:

1. `/verinoda-improve <a file>`: the pane opens beside the transcript (or above the prompt in a narrow terminal), the
   review starts by itself, the list arrives, nothing in the project changed.
2. ctrl+x tab gives the pane the keyboard; Tab reaches every row, Enter opens one, `u` / `k` / `c` mark it, the arrows scroll
   a long list, `g` sends.
3. Typing an own item and pressing Enter adds it and leaves the field empty.
4. After sending, the model changes only what was chosen and calls `improve_report`; the results show. Sending while the
   model is still writing does not show `Sonuç bildirilmedi` early.
5. A list of 20 items at 40 columns is readable.
6. With `improveOffer` on (in `/config`), "make this nicer" gets the one-line offer and no edit.

## Decided; do not reopen

- The trigger is the command. The model never opens the list on its own; with `improveOffer` it offers it in one line.
- "Explain" is the item's details opening in the pane. It costs no model turn.
- "Later" is not a state: an undecided item is already that.
- The share of shown items that get selected is not a measure of success and is not shown as one.
- No memory of decisions in step 1.

## Unknowns to report back, not to guess

- Whether `$.prompt.submit` may be awaited inside `command.run` (this brief says do not).
- What `session.start`'s `isInteractive` is in a session the desktop app hosts (if it is false there, the feature is off for
  that person, and the brief's rule 7 needs another test of "a person is here").
- Whether an `Input` redrawn with the same `value` it was drawn with before is emptied (the brief avoids the question by
  keeping the draft in the state, at the price of a state write per keystroke; say if that is slow).
- Whether the `text` of `turn.start` is exactly what the mod submitted when another plugin or a settings hook rewrites
  prompts (the brief compares the first line only; `stop-waiting` is the way out if it never matches).
- Whether `$.state` survives `/clear` (the brief resets the state on a `session.end` with `reason: 'clear'` either way).
- Whether a pane that a model's tool call opens is placed in a narrow terminal (the brief assumes not).

If the declaration file contradicts this brief, the file is right: do what it allows, and say what you changed and why.
