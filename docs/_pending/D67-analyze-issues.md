# D67-analyze-issues (pending doc text for the operator to merge)

Section numbers are left as `NN` for the operator (the next free DESIGN.md section).

## DESIGN section

## NN. Issue-shaped questions: never refused for the drafted plan, answered in the question's language, restated briefly (D67, 2026-09-28)

### NN.1 Why

A no-model run of `verinoda analyze` on 80 real issue texts used verbatim as questions (bug reports and pull
request descriptions with their templates, logs and environment dumps; median 405 characters, longest 6,367)
showed three defects that have nothing to do with retrieval:

1. **Refused questions.** 4 of 80 exited 2 with "the plan is invalid; nothing was analysed". A typed question
   runs through a plan drafted by rules (D1-D4), and the draft kept at most 10 references
   (`LIMITS["references"]`) while the plan check requires every version-like token of the message to be
   carried by a reference (`version_dropped`, "never drop a version the user named"). An environment dump
   ("numpy: 1.23.5", "scipy: 1.10.0", ...) or a changelog of commit SHAs and pull-request numbers names 14 to
   56 of them, so the draft dropped every one past the tenth and failed its own check. The user had asked a
   question, not written a plan, and got nothing.
2. **Answers in Turkish for English issues.** 4 of 80 were understood as Turkish (`understood as: Anladığım
   (kurallarla)`, sub-question kinds `[etki]`, `[akış]`). `detect_language` split words at apostrophes, so
   every "I've" / "we've" gave the token `ve`, which is the Turkish "and": two of them made the message "mixed",
   and "mixed" is answered in Turkish. Code in the message (`-o` flags, `var`, locale names such as `en`)
   and a single Turkish letter anywhere (a quoted Turkish word, "Gödel") had the same effect.
3. **The question printed again and again before the evidence.** The drafted `restated_goal_user_lang`
   (shown as "understood as") joined every sub-question's whole text; each sub-question heading printed its
   whole text again; and most unknowns carry the sub-question's text as their `question`, so a sub-question
   with three unknowns printed the issue three more times. On 27 of 80 issues the first code passage came after
   character 6,000 (the median issue's passages began at character 4,759). The echoes are also charged to the
   context budget: every unknown is charged at its serialised size, so a long issue spent budget on copies of
   itself that could have gone to claims.

### NN.2 Decisions

- **The draft carries every version, whatever the limit** (`question_plan._draft_references`,
  `_fold_overflow`). A version named again (the same SHA or pull-request number twice) is one reference. When
  more references remain than a plan may hold, the first ones stay references of their own and the last one
  (`derived_by: ...:version_overflow`) keeps the user's own words as its `text` and lists every remaining
  version in `version.evidence`, where the version check finds them. None is dropped, the plan stays within
  its limits, and the check is unchanged: a host's plan that drops a version is still an error. Relative
  versions ("the previous release") always stay references of their own, because a version clarification is
  about them.
- **A typed question is never refused for its drafted plan** (`analysis.analyze`, `question_plan.fallback`).
  When the drafted plan still fails its checks, it is stored as it was (status `invalid`) and the question is
  answered with a fallback plan: the whole message as one sub-question (intent from the rule cues) about the
  drafted plan's mentions, without references, stored with the drafted plan as its `parent_id`. Without the
  mentions every word of the question would look absent from the repository (the "words occur nowhere" check
  reads the plan's links); when the mentions are what failed, the sub-question has none. A plan without
  references cannot carry the message's versions, so for this plan only (`check(..., refuse_versions=False)`)
  a dropped version is a warning: there is no plan the user could fix. The answer says so: the result has
  `plan_fallback` (the drafted plan's id, why, its first errors), the text view prints a `note:` line after
  "understood as", and the MCP view carries `plan_fallback`. A plan the host passed (`--plan`, MCP
  `plan_json`) that fails its checks is still refused with `invalid_plan` and exit 2, unchanged.
- **The language is the prose's** (`textnorm.detect_language`). Only prose counts: fenced and inline code and
  URLs are left out (all of the text counts when nothing else is left), and the tails of English contractions
  (`'s 't 'd 'm 've 're 'll`) are dropped before the text is split into words; a Turkish suffix after an
  apostrophe (`API'de`, `Order'ı`) is not in that list and still counts. When both languages show, a message
  whose English function words number at least three and at least three times its Turkish signals (Turkish
  function words plus words with Turkish letters) is English. A Turkish question with English identifiers or
  an English phrase in it stays "mixed" and is answered in Turkish as before ("Where is sipariş kaydediliyor?",
  a Turkish question quoting an English error message). The intent cue tables still use their own test
  (`has_turkish`, unchanged: it feeds the lexicon at scan time, and changing it would change the index).
- **The question is restated briefly.**
  - A drafted `restated_goal` / `restated_goal_user_lang` is at most 300 characters (`GOAL_CHARS`): on one
    line, each sub-question's text given an equal share and clipped at a word with an ellipsis. A short
    message is shown whole, as before ("Understood (rules): q1 [locate] Where is compute_total defined?"). A
    host's goal is shown as the host wrote it. The "Understood as" contract of the answer is unchanged.
  - An unknown's `question` is kept on one line and at most 160 characters (`ECHO_CHARS`), where it is made,
    so what the budget is charged for is the clipped text; the verdict check's notes, which are added without
    the analysis's charging function, are clipped before they are charged.
  - The views (the default text of `verinoda analyze` and the MCP `analyze` response) show a sub-question's
    text on one line of at most 160 characters, and under a sub-question an unknown whose question only
    repeats that text is printed as `unknown: <why>; next: ...`. `--json` keeps every text whole, and the plan
    keeps each sub-question's whole text, because retrieval reads it.
  - The note "the question names A (A), B (B); these claims describe the working tree" that every claim of a
    sub-question with named versions carries names the first three versions (each on one line of at most 60
    characters: a URL is one of them) and counts the rest.
  - The draft's tokens for inline code and quotes end on their line (`question_plan._TOKEN_RX`): a fenced
    block (```` ```python ... ``` ````) or a quote left open in a pasted log used to be one "name" hundreds of
    characters long, a required mention that was echoed in the plan links and in every "Which one do you mean
    by '...'?" clarification each claim of the sub-question carried. The words inside such a block are read
    as words, and a name written as code inside it (`compute_total`) is still a code mention.
- Not done: the Turkish-cue test for intents (`has_turkish`) still counts a contraction tail such as `ve`; it
  is also read at scan time by the lexicon, so fixing it needs rebuilt indexes and its own measurement. The
  "plan links" line still links common words of an issue ("First", "time", "missed") to code, and a claim's
  repeated uncertainties are still printed on every claim; both take room before the passages on issue-shaped
  questions. The passages still come after the claims and context claims (the answer first). The
  measurement below is a targeted subset of 17 of the 80, not a re-run of the whole set; the effect on the
  retrieval scores (files and lines found) was not measured.

### NN.3 Measured

Offline, on the 80 issue texts (the rule draft and the plan check without a graph, so only the parts that do
not depend on a repository): drafted plans that fail their own check 4 -> 0; messages detected as Turkish or
mixed 4 -> 0 (all 80 English); the longest drafted "understood as" 296 characters. The tokenizer change alters
the drafted mention list of 6 of the 80 (18 had an inline-code or quote token spanning lines).

`verinoda analyze "<issue text>" --repo <checkout>` (default text output, default budget) on 17 of the 80,
chosen for the defects, not sampled: the 3 refused ones whose checkout is small enough to index quickly (the
fourth, a 75 MB checkout, is covered by the offline check above; one of the three was also answered in
Turkish), the other 3 answered in Turkish, 8 whose code passages began after character 6,000, and 3 short
questions as a regression check. One fresh index per checkout, built once with the base code (the change does
not touch indexing); each arm ran from a copy of that clean `.verinoda` folder, so no arm reused another's
claims. Code
trees were frozen with `git archive` and imported through `PYTHONPATH` in one virtual environment (checked:
`verinoda.__file__` is the frozen tree's). "run" is the original no-model run's output (an older frozen build),
"base" the branch point of this change (D66), "after" this change. Two offsets: where the `passages (...)`
header starts, and where the first printed source line starts (a numbered line under a file header).

| | run | base | after |
|---|---|---|---|
| exit 2, "the plan is invalid" | 3 / 17 | 3 / 17 | 0 / 17 |
| answered in Turkish | 4 / 17 | 4 / 17 | 0 / 17 |
| passages header at or after character 6,000, or none | 14 / 17 | 14 / 17 | 7 / 17 |
| first source line at or after character 6,000, or none | 14 / 17 | 14 / 17 | 9 / 17 |

On the 14 answered by both base and after: the passages header moved from a median character 8,231 to 5,633
and the first source line from 8,564 to 5,987 (earlier by 1,966 characters on average, median 2,151, from 58 on
a 54-character question to 3,939 on a 4,384-character one; never later). The whole output's median went from
13,830 to 11,251 characters. The three questions that were refused are now answered in 4 to 13 seconds
(first source line at characters 4,501, 9,085 and 10,263). "run" and "base" differ by at most 700
characters on any instance (in the answers, not the restatement), so nothing between the older build and the
branch point touched this. A first "after" build without the tokenizer change and the 60-character clip of
the version note had its first source line at median 6,460 over the 17 and late on 10; those two changes
took most from the questions with a pasted code block and a URL (for example 13,659 -> 10,263 and
10,664 -> 9,085).

What still comes before the passages on the late ones: the claims and context claims (the answer first, by
design), one "unknown" line per open point, the plan links of common words, and uncertainties repeated on
each claim; see "Not done". Wall times were measured on a shared, loaded machine and are not compared.

Tests: `tests/test_textnorm.py` (an English issue with contractions, a quoted Turkish word, a name with
Turkish letters and code full of Turkish-looking tokens is English; `API'de`, a Turkish question around a code
block, a Turkish question quoting English stay Turkish / mixed; `clip`), `tests/test_question_plan.py` (an
issue with 15 package versions, repeated PR numbers and SHAs drafts 10 references that carry every version
and passes the check; a long message's goals are at most 300 characters and one line, a short one unchanged;
an English issue with contractions drafts in English; a fenced block is no mention; the fallback plan keeps
the drafted mentions, turns a dropped version into a warning, and drops mentions that are themselves broken),
`tests/test_analysis.py` (a drafted plan made invalid is answered as one sub-question with `plan_fallback`,
the invalid draft stored as its parent, the note in the text and the field in the MCP view; a host's invalid
plan is still refused, unchanged test; a long question's unknowns are one line of at most 160 characters and
its text is printed at most once per heading), `tests/test_analysis_view.py` (the clipped heading, the unknown
without the repeat, the note line, the MCP view's clipped text and `plan_fallback`). The existing analysis,
plan, view, MCP, CLI, verdict-gate, decide, docs and reference tests pass unchanged.

## Decision table row

| D67 | Issue-shaped questions | implemented | Built 2026-09-28 (section NN): a typed question is never refused because the plan drafted for it failed its own checks (the draft carries every version the message names within the reference limit; a drafted plan that still fails is kept for the record and the question is answered as one sub-question about its mentions, saying so; a host's plan is still refused); the answer's language is the prose's (code, URLs and English contraction tails do not count; English function words outnumbering Turkish signals three to one make it English); the drafted "understood as" is at most 300 characters and a sub-question's text is printed once, clipped to 160 characters, so the passages come earlier. |

## UPGRADING note

### D67: Issue-shaped questions

- Nothing to run. `verinoda analyze "<question>"` (and MCP `analyze` without `plan_json`) no longer exits 2
  with "the plan is invalid" because the plan it drafted failed its own checks: a question that names more
  versions than a plan holds is answered, and a drafted plan that still fails is replaced by a one-sub-question
  plan; the result then has `plan_fallback` (JSON and MCP) and the text a `note:` line after "understood as".
  A plan passed with `--plan` / `plan_json` that fails its checks is still refused (exit 2).
- The drafted "understood as" (`understood_as`, the plan's `restated_goal` and `restated_goal_user_lang`) is at
  most 300 characters on one line; an unknown's `question` is at most 160 characters on one line; the text and
  MCP views show a sub-question's `text` the same way and no longer repeat it in the unknowns under it.
  `--json` keeps each sub-question's whole `text`. A script that matched an unknown's question against the
  whole sub-question text to find its sub-question should read the unknown's `sub_question` instead.
- `textnorm.detect_language` (the plan's `language`, the answer language, `decide` briefs, reference
  resolution) reads only the prose: an English message with "I've", code or one Turkish word is `en` where it
  was `mixed`, so it is answered in English.
- The "the question names ..." uncertainty lists at most three versions, each clipped to 60 characters, and
  counts the rest ("and 7 more").
- A plan drafted by `verinoda plan draft` (or inside analyze) no longer turns a fenced code block or a quote
  left open across lines into one mention: inline code and quotes end on their line.
