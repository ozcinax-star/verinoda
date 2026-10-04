---
name: verinoda-improve
description: Turn a vague request (make this better, cleaner, nicer, higher quality) into choices before anything changes - a ranked list of what could be changed, multiple-choice questions, and only the items the user chose are done. Use when the user mentions it or asks for improvement options or a review list; never on your own initiative. Nothing is changed until they choose.
---
<!-- verinoda-managed v1 -->
<!-- Managed by `verinoda live install`. After a local install, edits are kept: install will not overwrite them and uninstall leaves the file. Delete the marker line above to take ownership. -->

# Verinoda improve: choose what to change before anything changes

"Make this better / cleaner / nicer" does not say what should change. Guessing changes things nobody asked for; open
questions tire the person. This turns it into a list they recognise their wish in. **Nothing changes until they choose.**
All logic is in the CLI (`{{VERINODA_CLI}}`.{{VERINODA_CLI_NOTE}}); the list lives in `.verinoda/improve.json`.

## When

- The user mentions `$verinoda-improve` (there is no slash command in Codex: do not tell them to type one), or asks for
  improvement options, a review list, "what could be better".
- Never on your own initiative. A request that says what to change is not vague: just do it.

## The flow

1. `verinoda improve start <what to look at>` prints what to do. What to look at is the rest of the user's message; none
   means what this conversation has been about (unclear: ask one question first).
2. **Look, change nothing.** Read the code; if there is a rendered view you can look at, look at it.
3. Hand over the list, ranked, most worth doing first, no padding: write the JSON (`verinoda improve schema` shows it) and
   run `verinoda improve propose --file <file>` (or pipe it on stdin). One item = one change the user can say yes or no to,
   with a concrete `title` (not "improve the hierarchy" but "make Save the only primary button"). `group`: `problem` = a
   possible bug or inconsistency (give `evidence`, the file:line you read; without it the list says so), `improvement` =
   usability, readability, performance, upkeep, `taste` = look, tone, density (`seen` true only if you judged it from a
   rendered view). A suspicion is not a fact; never present one as such.
4. **Ask.** `verinoda improve questions` prints the undecided items as multiple-choice questions, three to a batch. Ask each
   batch in **one** `request_user_input` call (plain numbered text if it is not available: "1. Apply, 2. Check first,
   3. Keep as is" per item), in the user's language: one question per item, the options exactly as printed. Record the
   answers: `verinoda improve decide i1=apply i2=keep i3=check`. An item they did not answer, or answered "other", stays
   **undecided**: that is not keep and not reject; never record a decision they did not make. Offer once: "anything of your
   own to add?" (`verinoda improve add "..."`). `verinoda improve show` prints the list as it stands, `--all` the folded items.
5. `verinoda improve send` - only now do you work. It prints what to do: apply the chosen items; for "check first" only
   investigate and change nothing; leave everything else, the kept items included, exactly as it is. Small auxiliary changes an
   item needs are fine; if one would add behaviour or cost something, say so before.
6. Report with `verinoda improve report` (`verinoda improve schema --report`): one outcome per item you were sent -
   `applied`, `partial` (say what is missing) or `failed` (say why) for an item to apply; `confirmed` (put the fix you
   propose in `change`, do not apply it) or `not_confirmed` for one to check. Every change that was not on the list goes under
   `extra_changes`. A confirmed problem goes back into the list: the user decides on its fix like any other item.
7. Tell the user what happened to each item in a few lines, from the report, with its evidence.

The report never came: `verinoda improve stop-waiting`, then `verinoda improve ask-report`. A new `improve start` replaces
the list; the user's own unsent items stay.

## Rules

- The list is not consent and neither is opening it. Only the user's choice is.
- Report what you did, not what you meant to do; a `partial` or `failed` item is said as such.
- Questions and results are in the user's language; the CLI's text is English.
- If the CLI fails inside the Codex sandbox, say so and use the MCP tools of the `verinoda` server where they cover it.
