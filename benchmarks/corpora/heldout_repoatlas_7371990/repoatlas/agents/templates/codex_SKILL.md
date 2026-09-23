---
name: repoatlas
description: Evidence-first answers about this codebase with RepoAtlas - how a feature works, where something is implemented, how calls or data flow between components (handler -> service -> storage), why a design decision was made, what a change would affect, or whether an earlier conclusion still holds. Every answer is a set of claims with file:line evidence and an explicit status (verified, inference, unknown). Use it before explaining unfamiliar code and when the user disputes an earlier answer; do not use it for pure code-writing tasks.
---
<!-- repoatlas-managed v1 -->
<!-- Managed by `repoatlas install`. After a local install, edits are kept: install will not overwrite them and uninstall leaves the file. A copy with no local install record is refreshed by install. Delete the marker line above to take ownership. -->

# RepoAtlas: evidence-first codebase analysis

## How this skill is invoked in Codex

- The user mentions `$repoatlas` in a prompt (or picks it from `/skills`); Codex may also choose
  it by itself when a task matches the description above.
- **There is no `/repoatlas` slash command in Codex.** Do not tell the user to type one.
- The question to work on is the rest of the user's message. If there is no question about this
  repository, ask what they want to know.

RepoAtlas indexes the repository (AST, no LLM), answers questions as **claims with evidence**,
critiques its own claims, and says **unknown** instead of guessing. Your job is to drive it,
read its structured output, and report exactly what the evidence supports.

## When to use

- "How does X work / where is X / what calls X / how does data get from A to B?"
- "Why is it built this way?" (git history and design docs are searched)
- "What breaks if I change X?" (impact view)
- Verifying, re-checking or challenging an earlier conclusion, yours or the user's.
- Comparing a mechanism with a reference repository or an official document.

## Setup (once per session)

1. `repoatlas doctor --json` - check the `graph` and `snapshot` entries.
2. No index yet: `repoatlas scan .` (local, no network, no LLM).
3. Snapshot does not match the working tree: `repoatlas update .`

Commands find the project root from the working directory (nearest `.repoatlas` or `.git`);
pass `--repo <dir>` otherwise. If `repoatlas` is not on PATH, the CLI on this machine at
install time was: {{REPOATLAS_CLI}}
On Windows PowerShell, put regular expressions and globs in single quotes.

## MCP tools

If the `repoatlas` MCP server is configured (`[mcp_servers.repoatlas]` in `~/.codex/config.toml`,
or in the project's `.codex/config.toml` when the project is trusted), prefer its tools where they
cover the task: they call the same core functions as the CLI. For anything else (for example
experiments), or without the server, run the CLI below, always with `--json`, and read the fields
instead of scraping text.

If the CLI fails inside the Codex sandbox with `PermissionError` or `ModuleNotFoundError` while
importing `repoatlas` (observed on Windows when RepoAtlas was installed with uv's default hardlinks,
or as an editable dev install), do not guess: use the MCP tools above, which run outside the
sandbox, and tell the user that `repoatlas doctor` explains the fix (reinstall with
`uv tool install --link-mode copy ...`).

## Evidence discipline (non-negotiable)

Every important conclusion is a claim with one status:

| status | meaning | how you may phrase it |
|---|---|---|
| `experiment_verified` | a recorded test/experiment run supports it | fact, cite the run |
| `statically_verified` | cited source lines re-checked against the current tree | fact, cite file:line |
| `primary_source_verified` | pinned docs, standard, design doc, git history | fact, cite the source |
| `observed` | directly observed in verifying evidence | fact, cite it |
| `strong_inference` | supported, but not verified (e.g. graph edges only) | "likely", say why |
| `weak_inference` | little support | "possibly", never as fact |
| `unknown` | not established | say unknown + the next verification step |
| `contradicted` | evidence refutes it | say it is false, cite the refutation |
| `stale` | code changed since the claim was made | re-verify before using |

Rules:

- Cite `path:line` (or `path:start-end`) for every statement, taken from the evidence locators.
- Never present `weak_inference` or `unknown` as fact. Never invent a relation, call path,
  file, line, test result or benchmark number.
- A graph edge alone is never verification: `trace` and `query` give leads; confirm them with
  source lines (`analyze`, `claim add --source`, `verify`).
- Do not upgrade a status by wording. Change status only through RepoAtlas (`verify`,
  `experiment run`, `claim add --source`, which downgrades a requested status the evidence
  does not allow).
- Heuristics are labelled: map views (impact included) carry `coverage.method` and
  `coverage.limits`, and `analyze` lists the `intents` it guessed. Repeat those limits when you
  rely on them.
- If RepoAtlas returns `unknown`, report it with its `next_step`; do not fill the gap yourself.

## Commands (examples; always add --json when you read the result)

```bash
# orient
repoatlas map . --json
repoatlas map . --view dependencies --json
repoatlas map . --view impact --target src/module.py --json
repoatlas query "where is the order total computed" --max-items 8 --json

# answer a question: claims + evidence + critique + unknowns, within a budget
repoatlas analyze "how does an order get persisted?" --json
repoatlas analyze "why is pricing separate from the service?" --run-tests --json

# follow a flow between two symbols or files (edge locations included)
repoatlas trace create_order save_order --json
repoatlas trace create_order save_order --mode any --json

# inspect, re-check and attack claims
repoatlas claim show <claim-id> --json
repoatlas claim list --status unknown --json
repoatlas claim add "Totals are computed in pricing.total" --source src/pricing.py:10-24 --json
repoatlas verify <claim-id> --json
repoatlas verify <claim-id> --run --json
repoatlas challenge <claim-id> --json

# targeted experiment (runs in a throw-away copy; non-test commands need docker/podman)
repoatlas experiment run --hypothesis "discount is applied before tax" --expect pass --json -- python -m pytest -q tests/test_pricing.py

# external references: pinned repo commit or document URL
repoatlas research https://github.com/org/reference --topic "retry policy" --json
repoatlas research https://example.org/spec.html --kind official_doc --json
repoatlas compare . https://github.com/org/reference --topic "retry policy" --json

# user critique (see below)
repoatlas feedback add --text "the total is rounded in the repository" --claim <claim-id> --process --json
repoatlas feedback add --text "prices are cached" --expect-pattern "lru_cache" --expect-in "src/*.py" --process --json
repoatlas feedback process <feedback-id> --json
repoatlas feedback list --json

# after code changes
repoatlas update .
```

Workflow: orient (`map`, `query`) -> `analyze` the question -> drill down (`trace`, `claim show`)
-> `challenge` the claims you will rely on -> `verify` / `experiment run` where static evidence is
not enough -> answer.

## User critique is a hypothesis

When the user says an answer is wrong, do not simply agree and do not defend the old answer.
Record the critique and let the protocol check it:

`repoatlas feedback add --text "<their point>" --claim <claim-id> [--correction "<their version>"] [--expect-pattern REGEX --expect-in GLOB] [--reference URL] --process --json`

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

## After editing code

Run `repoatlas update .` after you or the user change code. It re-indexes the changed files
and marks claims whose evidence changed as `stale`; `verify` them again before relying on them.

## Answer format

1. Direct answer in one or two sentences, limited to what is verified.
2. Claims, strongest first: `[status confidence] statement - path:line (claim id)`.
3. Inferences, clearly labelled as such.
4. Unknowns with the next verification step.
5. Budget notes: exhausted budgets, unchallenged claims, heuristic views used.
