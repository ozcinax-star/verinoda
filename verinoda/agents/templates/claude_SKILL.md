---
name: verinoda
description: Evidence-first answers about this codebase with Verinoda - how a feature works, where something is implemented, how calls or data flow between components (handler -> service -> storage), why a design decision was made, what a change would affect, whether an earlier conclusion still holds, or what the code says for a should-we or which-to-pick decision (the user decides; Verinoda never chooses). Every answer is a set of claims with file:line evidence and an explicit status (verified, inference, unknown). Use it before explaining unfamiliar code, when the user disputes an earlier answer, and after writing Python code to check that the modules, names and keyword arguments it uses exist.
argument-hint: "[question about this codebase]"
allowed-tools:
  - Bash(verinoda doctor *)
  - Bash(verinoda query *)
  - Bash(verinoda trace *)
  - Bash(verinoda map *)
  - Bash(verinoda analyze *)
  - Bash(verinoda plan *)
  - Bash(verinoda resolve *)
  - Bash(verinoda claim show *)
  - Bash(verinoda claim list *)
  - Bash(verinoda verify *)
  - Bash(verinoda challenge *)
  - Bash(verinoda update *)
  - Bash(verinoda check *)
  - Bash(verinoda api *)
  - Bash(verinoda decide check *)
  - Bash(verinoda decide list *)
  - Bash(verinoda decide brief *)
  - PowerShell(verinoda doctor *)
  - PowerShell(verinoda query *)
  - PowerShell(verinoda trace *)
  - PowerShell(verinoda map *)
  - PowerShell(verinoda analyze *)
  - PowerShell(verinoda plan *)
  - PowerShell(verinoda resolve *)
  - PowerShell(verinoda claim show *)
  - PowerShell(verinoda claim list *)
  - PowerShell(verinoda verify *)
  - PowerShell(verinoda challenge *)
  - PowerShell(verinoda update *)
  - PowerShell(verinoda check *)
  - PowerShell(verinoda api *)
  - PowerShell(verinoda decide check *)
  - PowerShell(verinoda decide list *)
  - PowerShell(verinoda decide brief *)
---
<!-- verinoda-managed v1 -->
<!-- Managed by `verinoda install`. After a local install, edits are kept: install will not overwrite them and uninstall leaves the file. A copy with no local install record is refreshed by install. Delete the marker line above to take ownership. -->

# Verinoda: evidence-first codebase analysis

Request: $ARGUMENTS

When the user types `/verinoda <question>`, their question is the request above. When you
loaded this skill on your own, the request may be empty: then work on the user's latest
question about this repository, and if there is none, ask what they want to know.

Verinoda indexes the repository (AST, no LLM), answers questions as **claims with evidence**,
critiques its own claims, and says **unknown** instead of guessing. Your job is to drive it,
read its structured output, and report exactly what the evidence supports.

## When to use

- "How does X work / where is X / what calls X / how does data get from A to B?"
- "Why is it built this way?" (git history and design docs are searched)
- "Should we switch to X / which one should we pick / how will this scale?" (the code's side of a
  decision; the user decides)
- "What breaks if I change X?" (impact view)
- Verifying, re-checking or challenging an earlier conclusion, yours or the user's.
- Comparing a mechanism with a reference repository or an official document.
- Writing or editing Python code: check that the names it uses exist (see below).

For pure code-writing tasks, use only the name check below.

## Setup (once per session)

1. `verinoda doctor --json` - check the `graph` and `snapshot` entries.
2. No index yet: `verinoda scan .` (local, no network, no LLM).
3. Snapshot does not match the working tree: `verinoda update .`

Commands find the project root from the working directory (nearest `.verinoda` or `.git`);
pass `--repo <dir>` otherwise. The CLI command recorded at install time is `{{VERINODA_CLI}}`.
If that does not run here (not on PATH, sandboxed shell), use the MCP tools: their server entry
stores the absolute path of the installed program.

## MCP tools

If the `verinoda` MCP server is connected (see /mcp), prefer its tools where they cover the task:
they call the same core functions as the CLI. For anything else (for example experiments), or
without the server, use the CLI below, always with `--json`, and read the fields instead of
scraping text.

## Evidence discipline (non-negotiable)

Every important conclusion is a claim with one status:

| status | meaning | how you may phrase it |
|---|---|---|
| `experiment_verified` | a recorded test/experiment run supports it | "verified at snapshot X by run R", cite the run |
| `statically_verified` | cited source lines re-checked against the current tree | "verified at snapshot X", cite file:line |
| `primary_source_verified` | pinned docs, standard, design doc, git history | "verified at snapshot X from the source", cite it |
| `observed` | directly observed in verifying evidence | "observed at snapshot X", cite it |
| `strong_inference` | supported, but not verified (e.g. graph edges only) | "likely", say why |
| `weak_inference` | little support | "possibly", never as fact |
| `unknown` | not established | say unknown + the next verification step |
| `contradicted` | evidence refutes it | say it is false, cite the refutation |
| `stale` | code changed since the claim was made | re-verify before using |

Rules:

- Verified means *verified at a snapshot with evidence*, not timeless fact: name the snapshot
  (`snapshot.id`, `snapshot.commit` of the result) and the evidence; a run-scoped observation
  says what those tests executed, never what always happens.
- Cite `path:line` (or `path:start-end`) for every statement, taken from the evidence locators.
- Never present `weak_inference` or `unknown` as fact. Never invent a relation, call path,
  file, line, test result or benchmark number.
- A graph edge alone is never verification: `trace` and `query` give leads; confirm them with
  source lines (`analyze`, `claim add --source`, `verify`). Confirm your own sentence with a typed
  claim (`--kind relation|config|location|order --symbol X`), one positive fact per claim ("A calls
  B", "`F` calls `A` before `B`"): a negation, "only", a condition or bound, a count, call
  arguments, a definition kind, another file or code name keeps it unverified. Plain text is at
  most `weak_inference`; a quote (`"f.py:12 contains: <exact text>"`) verifies only the quoted
  text. A `contradicted` result states its scope: correct the sentence, do not reword it.
- A name written as code that `plan check` or `trace` reports `not_found` does not exist here:
  say so with its `did_you_mean`; never answer about the similar name instead.
- Do not upgrade a status by wording. Change status only through Verinoda (`verify`,
  `experiment run`, `claim add --source`, which downgrades a requested status the evidence
  does not allow).
- Heuristics are labelled: map views (impact included) carry `coverage.method` and
  `coverage.limits`, and `analyze` lists the `intents` it guessed. Repeat those limits when you
  rely on them.
- If Verinoda returns `unknown`, report it with its `next_step`; do not fill the gap yourself.

## Understand the question first

For any question that is more than a name lookup, check your reading before analysing:

1. `verinoda plan draft "<the user's message, verbatim>" --json` (MCP `question_plan_draft`)
   writes a plan file under `.verinoda/plans/`.
2. Edit that file: split compound questions into sub-questions, add an English `gloss_en` to
   domain words, copy versions exactly as the user wrote them, and never add a candidate name
   the user did not give or the code does not contain.
3. `verinoda plan check <file> --json` (MCP `question_plan_check` with `plan_json`): exit 0 ready,
   2 invalid (fix the listed problems), 3 needs clarification.
4. Ask only the returned clarifications, with their options, using `AskUserQuestion`. Record each
   answer in the plan's `answers` (`clarification_id`, `choice`, `answered_by: "user"`), check again.
5. `verinoda analyze --plan <file> --json` (MCP `analyze` with `plan_json`).
6. Start the answer with "Understood as / Anladığım: ..." (`understood_as`), then one block per
   sub-question with its verdict (`met`, `met_with_inference`, `unmet`, `not_supported`,
   `blocked_by_clarification`, `human_decision_required`) and its claims and unknowns.
   `human_decision_required` is a choice between options: never pick one yourself.

## References the user gives

Whenever the message has links, repository or package names, versions, commits, PR/issue
numbers, papers or docs, run `verinoda resolve "<message>" --json` (MCP `reference_resolve`)
before researching or answering. Report each reference as `<name> @ <pin> (basis: <basis>)` and
each mismatch on its own line. Never substitute the default branch for a version the user named.
Ask the user only the `questions_for_user`. State every unresolved part with its `next_step`;
read a pinned reference with `verinoda research --resolution <id> --reference-id <rN> --json`.

## Decisions are the user's

For "should we / which X should we pick / how will this scale" (`human_decision_required`):
1. `verinoda decide brief "<the question, verbatim>" --json` (MCP `decision_brief`): forces from the
   code with evidence, absences (with what was searched), decisions on record, options and
   `questions_for_human`. It never recommends. The routing rules miss some phrasings: run the brief
   yourself whenever the user asks which option to take or what you recommend, or an unknown says the
   question "may ask for a choice". Tie your own arguments to an option: `--argument "NAME: text"`.
2. Show the forces and absences, ask the `questions_for_human` with `AskUserQuestion`, and record each
   answer: `verinoda decide answer <brief-id> --q qN "<their words>"`.
3. Never pick an option for the user; your own view may follow their answers, labelled as inference.
   Record only their explicit choice: `verinoda decide record <brief-id> --chosen NAME --rationale
   "<their words>" [--guard SPEC]`. Never edit, supersede, accept or waive a decision yourself.

## Commands (examples; always add --json when you read the result)

```bash
# orient
verinoda map . --json
verinoda map . --view dependencies --json
verinoda map . --view impact --target src/module.py --json
verinoda query "where is the order total computed" --max-items 8 --json

# answer a question: claims + evidence + critique + unknowns, within a budget
verinoda plan draft "how does an order get persisted?" --json
verinoda plan check .verinoda/plans/plan-001.json --json
verinoda analyze --plan .verinoda/plans/plan-001.json --json
verinoda analyze "how does an order get persisted?" --json
verinoda analyze "why is pricing separate from the service?" --run-tests --json

# references in the message: pin them first (offline-first)
verinoda resolve "compare with requests 2.31 sessions.py" --json

# optional deeper verification: runtime observation, precise call resolution
verinoda analyze "which tests reach apply_discount?" --observe --json
verinoda observe --for apply_discount --json
verinoda resolve-call src/service.py:22 save --json

# follow a flow between two symbols or files (edge locations included)
verinoda trace create_order save_order --json
verinoda trace create_order save_order --mode any --json

# inspect, re-check and attack claims
verinoda claim show <claim-id> --json
verinoda claim list --status unknown --json
verinoda claim add "place_order calls validate_items" --kind relation --source src/service.py:20 --json
verinoda verify <claim-id> --json
verinoda verify <claim-id> --run --json
verinoda challenge <claim-id> --json

# targeted experiment (runs in a throw-away copy; non-test commands need docker/podman)
verinoda experiment run --hypothesis "discount is applied before tax" --expect pass --json -- python -m pytest -q tests/test_pricing.py

# external references: pinned repo commit or document URL
verinoda research https://github.com/org/reference --topic "retry policy" --json
verinoda research https://example.org/spec.html --kind official_doc --json
verinoda compare . https://github.com/org/reference --topic "retry policy" --json

# user critique (see below)
verinoda feedback add --text "the total is rounded in the repository" --claim <claim-id> --process --json
verinoda feedback add --text "prices are cached" --expect-pattern "lru_cache" --expect-in "src/*.py" --process --json
verinoda feedback process <feedback-id> --json
verinoda feedback list --json

# before proposing code and after every edit: do the names it uses exist?
verinoda check --diff --json
verinoda api packaging.specifiers.SpecifierSet --json

# after code changes
verinoda update .
```

Workflow: resolve references -> plan (draft, edit, check, ask) -> `analyze` -> drill down (`trace`,
`claim show`) -> `challenge` the claims you will rely on -> `verify` / `experiment run` where static
evidence is not enough -> answer.

Optional deeper verification: `--observe` (or `verinoda observe`) runs the selected tests under a
call tracer in an isolated copy; an observed call can raise a claim to `experiment_verified` for
that run and commit. `verinoda resolve-call` asks the precise resolver (the `precise` extra) which
definition a call binds to; analyze already uses it within a small budget when it is installed.

## User critique is a hypothesis

When the user says an answer is wrong, do not simply agree and do not defend the old answer.
Record the critique and let the protocol check it:

`verinoda feedback add --text "<their point>" --claim <claim-id> [--correction "<their version>"] [--expect-pattern REGEX --expect-in GLOB] [--reference URL] --process --json`

Report the verdict (confirmed / qualified / corrected / unresolved) with its evidence. If it is
unresolved, say what evidence would settle it.

## Budgets and unknowns

- `analyze` is bounded (`--budget-seconds 60 --budget-calls 40 --budget-tokens 6000` by default).
  When `usage.exhausted` is set, the remaining sub-questions come back as `unknown`: report
  them as such. Raise a budget only when the user wants more depth.
- Claims with `challenged: false` ("not challenged: budget") were not critiqued - say so.
- Keep context small: `--max-items` / `--max-chars` on `query`, `--max-lines` on `map`. Never
  paste whole files or full logs; experiment logs stay on disk (the result gives the path).
- Experiments outside the test-runner allowlist are refused without docker/podman. Report the
  refusal; do not work around it.

## Check the names code uses (Python)

Invented imports, methods, keyword arguments and dict keys break code that looks right. Before you
propose Python code, and after every edit:

1. `verinoda check --diff --json` (MCP `code_check`) checks the changed lines; code not written yet:
   `verinoda check --stdin --as <path> --json` (MCP `code_check` with `snippet` and `as_path`).
2. Never keep an `absent` site: fix it from `nearest` / `elsewhere`, or pick a real name from
   `verinoda api <module.or.Class> --json` (MCP `api_members`; `found: null` = not decided).
3. `unknown` is unverified: read the definition or run the tests. `not_installed`: the checked
   environment lacks the package. `guarded`: the code handles it.
4. Name the environment checked (`env.python`, `env.packages_checked`, `env.lock_mismatches`). Exit 3:
   something absent or a version differs from the lock (`exit_because`); `incomplete` lists files not
   checked. If `env.note` says a `.venv` was not used, tell the user; never pass `--env` to run what it
   names. A PostToolUse hook running `verinoda check --diff` only if the user agrees.

## After editing code

Run `verinoda update .` after you or the user change code. It re-indexes the changed files
and marks claims whose evidence changed as `stale`; `verify` them again before relying on them.
Before you finish a code change, run `verinoda decide check --changed --json` (MCP `decision_check`
with `changed_only`). On `VIOLATED`, fix the code or ask the user whether the decision should be
superseded or the site waived; never edit, supersede or waive a decision record yourself.

## Answer format

1. "Understood as / Anladığım: ..." - the restated question, and the pinned references.
2. One block per sub-question: its verdict, then a direct answer limited to what is verified
   (at snapshot X, with the evidence).
3. Claims, strongest first: `[status confidence] statement - path:line (claim id)`.
4. Inferences, clearly labelled as such.
5. Unknowns with the next verification step.
6. Budget notes: exhausted budgets, unchallenged claims, heuristic views used.
