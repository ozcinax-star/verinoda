# Verinoda

> **⚠️ Durum / Status: TAMAMLANMADI — work in progress, no guarantees.**
> Bu depo aktif geliştirme altındadır ve henüz bitmemiştir. Özellikler eksik,
> değişken veya hatalı olabilir; hiçbir doğruluk, güvenlik ya da uygunluk
> güvencesi verilmez. Üretimde kullanmayın.
> This repository is unfinished. Features may be missing, change without notice
> or be wrong. No warranty or guarantee of correctness, security or fitness for
> any purpose is given (see also the Apache-2.0 "AS IS" terms in `LICENSE`).
> Not published to PyPI yet. Formerly developed under the working name "RepoAtlas".

### What is in this snapshot (2026-09-23)

| Part | State |
|---|---|
| Graphify port (`verinoda/project_index`, `tests_upstream/`) | done. Upstream suite at port time: 5436 passed / 50 failed, and every failure also fails on unmodified upstream on the same Windows machine; not re-run since (`docs/UPSTREAM.md`) |
| Core: claims, evidence, critique, experiments, research/compare, feedback, memory, installers, MCP server (23 tools) | implemented |
| Round 3: search engine, question plans with Turkish support, reference resolver, trust engine (anchors, entailment, facet-level staleness), runtime observation, precise call resolution | implemented and wired into the CLI, MCP and `analyze`; gaps per decision in `docs/DESIGN.md` ("Implementation status") |
| Product test suite | 1,134 passed, 1 skipped, 2 deselected (slow packaging and installer checks), Windows 11 / Python 3.12, 2026-09-23 |
| Agent integration | Claude Code (`/verinoda`) and Codex (`$verinoda`) verified in real headless sessions with the earlier skill text (`docs/AGENT-VERIFICATION.md`); the round-3 skill text (understand-first and references protocols) has no such record yet |
| Benchmarks (measured, `docs/BENCHMARKS.md`, chars/4 token estimates, gold facts found in the delivered context; no model in the loop) | Graphify's own code (226 files, 37 facts, in-sample): Verinoda text retrieval 36/37 at 1,424 tokens/question, ~0.14 s; Graphify 7/37. Set on Verinoda's own earlier code (33 facts): 25/33 vs Graphify 8/33 and raw reading 9/33; it was held out until the 2026-09-23 ranking change, which was chosen with it in view (22/33 before). Turkish paraphrases of the example app: 32/32. Regressions: `analyze` on the example app 32 -> 31/32, the one-off scan of the large corpus got slower (6.7 s -> 14.7 s cold), and the 2026-09-23 change adds about 0.03 s per retrieval on the large sets |
| Cross-platform | tested on Windows 11 only; the CI workflow (Linux/macOS/Windows) exists but is manual and has not been run |

**Known limitations** (see also `docs/DESIGN.md` for per-decision gaps):

Open findings of the second acceptance audit (2026-09-23 05:00), not yet fixed:

- Under process isolation a test run can still write outside the throw-away copy through pytest `@argsfile` or `--junitxml` indirection; use `--isolation container` for untrusted code.
- Running `scan`/`init`/`observe` with the home directory itself as the project is not refused.
- Questions that are only partly about the code can come back `met` with verified but irrelevant claims.
- The reference resolver can merge or drop references in some multi-reference sentences.
- Fixed after that audit: user `claim add --kind` with unrelated text no longer verifies; `.verinoda/` and `.git/` files are no longer accepted as evidence; `packaging` is now a declared dependency.

Found by running Verinoda on its own repository (2026-09-23), not yet fixed:

- Command names in a question ("the `scan` command", "`scan` komutu") are not mapped to their handler functions (`cmd_scan`) unless the names match, so "do `init` and `scan` refuse the home directory?" does not reach the guard in `paths.py`.
- On a repository of about 2,000 files an incremental `update` after a two-file edit took about 33 s, almost all of it in the graph rebuild. `analyze` refreshes first and can spend its default 60 s budget on that; run `verinoda update .` before asking.
- In this repository the frozen benchmark snapshot (`benchmarks/corpora/heldout_repoatlas_7371990/`) duplicates many hits of self-queries.

- Windows only so far. The POSIX resource limits and the container isolation
  path (docker/podman) are coded but have never been run.
- `experiment`, `observe` and `analyze --run-tests/--observe` run the
  project's own tests. Under process isolation (the default) the command's
  path arguments are confined to a throw-away copy, but the tests themselves
  can read and write outside it and use the network. Only run them on code
  you trust (`experiment run --isolation container` needs docker/podman and
  has not been tried against a real one).
- Test runs and observation are Python/pytest only. They use the project's
  own `.venv`/`venv` when it has one, otherwise Verinoda's interpreter;
  pytest must be installed there, or the run is `inconclusive`. The fast
  tracer needs Python 3.12+ (`sys.monitoring`). The fallback is much slower,
  and child processes are not traced.
- Precise call resolution is optional (`verinoda[precise]`, jedi) and covers
  Python only. Without it, method calls on typed parameters stay
  `strong_inference`. Other languages need a SCIP index that you produce
  yourself (`scan --scip FILE`).
- Turkish questions work but trail English. The Turkish question sets and
  the intent gold table were written by the rule author, so those scores are
  in-sample.
- Staleness of relation claims is tracked at the level of the whole calling
  function, so edits elsewhere in that function can mark a still-true claim
  stale.
- Some reference mismatch codes fire only in narrow cases, PEP 740
  provenance is not used for pinning, and there is no authenticated GitHub
  access.
- No model-in-the-loop measurement exists. Token counts are chars/4
  estimates, and the harness scores whether gold facts are *present* in the
  delivered context, not answer accuracy.

The sections below describe the product; where they are ahead of the code, the
lists above say so.

Evidence-first codebase analysis for people and coding agents.

Verinoda looks at a repository as a running system: code, call and data
flow, tests, configuration, git history, decision records and, when you let
it, targeted test runs. It answers questions as **claims with evidence**.
Every claim carries a status (`statically_verified`, `experiment_verified`,
`strong_inference`, `unknown`, `stale`, …), the exact `file:line` or commit it
rests on, and what is still uncertain. When a claim can't be supported, it says
`unknown` and names the next check to run. Questions may be in English or
Turkish. References in them (repositories, versions, packages, PRs, papers)
are pinned to the exact version the user meant before anything is compared.

> Verinoda is derived from [Graphify](https://github.com/Graphify-Labs/graphify)
> (commit `20a20d30`, Apache-2.0). It is an independent project, **not** an
> official Graphify release. See [docs/UPSTREAM.md](docs/UPSTREAM.md).
> The name "Verinoda" was checked as free on PyPI, npm and GitHub on 2026-09-23 (see
> [docs/NAMING.md](docs/NAMING.md)). Nothing has been published to a package index.

## Install

No git and no Python needed beforehand; [uv](https://docs.astral.sh/uv/) installs
Verinoda as an isolated tool and downloads a suitable Python if there is none.

**Windows (PowerShell or cmd)**

```powershell
winget install --id astral-sh.uv -e   # only if `uv --version` does not work yet; then open a new terminal
uv tool install --force --reinstall-package verinoda --link-mode copy "verinoda[precise] @ https://github.com/ozcinax-star/verinoda/archive/main.zip"
uv tool update-shell                  # once: puts verinoda on PATH for new terminals
```

**macOS / Linux (or Git Bash on Windows)**

```bash
curl -LsSf https://raw.githubusercontent.com/ozcinax-star/verinoda/main/install.sh | sh
```

Both install from the GitHub archive with the optional precise resolver and copy files
instead of hardlinking them (so sandboxed agents such as Codex can import the package).
Run the same `uv tool install ...` line, or the script, again to upgrade. The script
([install.sh](install.sh), read it first) installs uv if it is missing, runs that
command and `uv tool update-shell`; options are environment variables: `VERINODA_REF`
(branch, tag or commit; default `main`), `VERINODA_EXTRAS` (`none` to skip the precise
extra), `VERINODA_NO_MODIFY_PATH=1`.

Why there is no `irm ... | iex` one-liner for Windows: Microsoft Defender blocked
`powershell -ExecutionPolicy ByPass -c "irm <script url> | iex"` for this project's
script as `Trojan:Win32/Commando.A!ml`, a machine-learning verdict on that
download-and-run command line (the script file itself was not flagged). The plain `uv`
commands above avoid the pattern.

Then, once per project:

```bash
cd my-project
verinoda setup        # index the code + connect Claude Code / Codex if they are installed
```

`verinoda setup` is safe to re-run (it updates the index and leaves unchanged skills
alone). `--agents claude,codex|all|none`, `--scope user` for all projects, `--no-mcp`.

### Other ways to install

Python 3.10+ (3.12+ recommended: the runtime tracer uses `sys.monitoring`).
Graphify (`graphifyy`) is **not** required; the extractor is part of this
package.

```bash
# from a checkout or a built wheel (not on PyPI)
uv tool install --link-mode copy .                                   # or a wheel path
pipx install ./dist/verinoda-0.1.0.dev0-py3-none-any.whl
pip install ./dist/verinoda-0.1.0.dev0-py3-none-any.whl             # into an existing venv

# optional: precise call-site resolution (jedi)
uv tool install --link-mode copy --with "jedi>=0.19.2,<0.21" .
pip install ".[precise]"

verinoda --version
verinoda doctor
```

Build the wheel with `uv build --wheel` (or `python -m build --wheel`).

**Codex on Windows: use `--link-mode copy`.** With uv's default link mode
the installed package files are hardlinks into uv's cache. Codex's
`workspace-write` sandbox on Windows could not read them (`PermissionError`),
so the `verinoda` CLI failed inside Codex while the MCP tools still worked
(`docs/AGENT-VERIFICATION.md`). `uv tool install --link-mode copy …` (or
`UV_LINK_MODE=copy`) avoids this. `verinoda doctor` warns when the install
is hardlinked or editable.

## Quick start

```bash
cd my-project
verinoda setup                           # once: index + agent skills (or `verinoda scan .` for the index only)
verinoda map . --view dataflow           # entry points -> persistence, with limits stated
verinoda query "where is the discount threshold configured?"     # plain-text context
verinoda trace create_order_handler OrderRepository.save
verinoda plan draft "Sipariş API'den veritabanına nasıl ulaşıyor?"   # -> .verinoda/plans/plan-001.json
verinoda plan check plan-001.json        # grounds every mention; 0 ready, 2 invalid, 3 needs clarification
verinoda analyze --plan plan-001.json    # or: verinoda analyze "How does an order reach the database?"
verinoda resolve "compare with requests 2.31 sessions.py"        # pin the references first
verinoda observe --for apply_discount    # which tests reach it at runtime (isolated copy)
verinoda resolve-call orders/service.py:22 save                  # precise extra: which definition?
verinoda claim show clm_…                # evidence (with grades), uncertainties, full history
verinoda challenge clm_…                 # adversarial re-check; can only lower confidence
verinoda update .                        # after edits: re-index changed files, mark affected claims stale
verinoda verify clm_…                    # re-check evidence (moved lines are relocated)
```

Try it on the bundled example: copy `examples/orders_app` somewhere, `git init`
and commit it, then run the commands above inside the copy. For `observe`,
give the copy a `.venv` with pytest installed.

## Commands

| Command | What it does |
|---|---|
| `doctor` | Python, package layout, upstream base, graph/snapshot freshness, schema, claim counts, search index, lexicon, precise/SCIP availability, `sys.monitoring`, reference network mode, agent skills, MCP config, optional deps; secrets shown only as set/unset |
| `setup [path] [--agents auto\|all\|none\|claude,codex] [--scope project\|user] [--no-mcp]` | One step per project, safe to re-run: `init`, `scan` on the first run and `update` afterwards, then skills + MCP for the agents found on PATH (default `auto`); prints what is left to do by hand. Refuses the home directory unless `--allow-home` |
| `init [path]` | Create `.verinoda/` (database, config) |
| `scan <repo> [--force] [--precise] [--scip FILE]` / `update <repo>` | Full / incremental index + snapshot, then the derived search index, lexicon and symbol facts; `update` marks claims whose dependencies changed `stale`. `--precise` resolves the call sites of changed `.py` files; `--scip` adopts a SCIP index you produced |
| `map <repo> [--view …]` | hierarchy, dependencies, dataflow, config, tests, history, impact (`--target`, default: git changes) |
| `query "<q>" [--max-items N] [--max-chars N]` | Bounded retrieval from the passage index; plain text for a model by default (skeleton first, each item with why it was chosen), `--json` for programs |
| `trace <a> <b> [--mode flow\|any]` | Directed paths, each hop with relation, confidence and call-site location; hints when an endpoint does not resolve |
| `plan draft\|check\|schema\|audit` | Question plans: draft from the message (TR/EN rules), check and ground a plan file, print the schema, re-judge an analysis' sub-questions later |
| `analyze ["<q>"] [--plan FILE] [--run-tests] [--observe]` | Budgeted loop per sub-question → claims + evidence + critique + unknowns, each sub-question judged against its `done_when` |
| `claim show\|list\|add` | Inspect claims, or record one with source evidence (`--kind location\|relation\|config --symbol X` for a mechanical grade) |
| `verify <id> [--run]` | Re-check evidence against the current tree (anchored relocation; optionally re-run its test) |
| `challenge <id>` | Critique and counter-hypothesis probes; lowers status/confidence when support is weak |
| `resolve "<text>" [--reference URL[@ref]] [--network off\|cache\|on] [--local-intent]` | Pin every reference in a message to the exact version meant; reports mismatches and questions for the user |
| `research <url-or-repo> [--ref] [--topic] [--resolution ID --reference-id rN]` | Reference repo pinned to an exact commit (or the pin from `resolve`), or a document; mechanism trace |
| `compare <local> <reference> --topic …` | Assumption diff: data structures, errors, concurrency, environment, dependencies |
| `feedback add\|process\|resolve\|show\|list` | User critique handled as a hypothesis → confirmed / qualified / corrected / unresolved (references resolved first) |
| `experiment run --hypothesis … -- <cmd>` | Isolated targeted experiment; logs on disk, summary + evidence recorded; refused when policy does not allow it |
| `observe [TEST_ID …] [--for SYMBOL …] [--terms W …] [--mode …]` | Run tests under the call tracer in an isolated copy; reach per test, boundary calls, limits |
| `resolve-call PATH:LINE TARGET [--target PATH:LINE]` | Precise resolution of one call site (needs the `precise` extra; exit 3 = no precise answer) |
| `memory list\|learn\|history` | Versioned learnings, invalidated with their source claim |
| `install/uninstall --agent claude\|codex --scope project\|user` | Skill (+ MCP) for Claude Code (`/verinoda`) and Codex (`$verinoda`) |
| `mcp serve` | MCP server (stdio) over the same core, 23 tools |
| `index -- <args>` | The Graphify-derived CLI (advanced, unsupported); installer, hook and `~/.graphify` commands are blocked |
| `benchmark run\|sanitize` | Raw search vs Graphify baseline vs Verinoda on question sets with gold facts |
| `benchmark staleness replay\|mutations`, `benchmark critique-eval` | Staleness harness (history replay, mutation suite) and critique precision/recall |

Exit codes: 0 done, 1 error, 2 usage error / invalid plan / blocked command,
3 "needs more" (clarification, partial resolution, refused experiment,
incomplete observation, no precise answer). Every command except `memory`,
`mcp serve` and `index` accepts `--json` (`plan schema` always prints JSON).

## Coding agents

```bash
verinoda install --agent claude --scope project   # .claude/skills/verinoda/SKILL.md + .mcp.json entry
verinoda install --agent codex  --scope project   # .agents/skills/verinoda/SKILL.md (+ MCP config)
verinoda uninstall --agent claude --scope project # removes only what install recorded
```

- **Claude Code**: `/verinoda how does checkout reach the database?`
- **Codex**: mention `$verinoda` in the prompt. (Codex has no `/verinoda` command.)

The skills describe the working method; all logic lives in the CLI/MCP core.
Two protocols come first:

1. **References the user gives.** When the message has links, repository or
   package names, versions, commits, PR/issue numbers, papers or docs, the
   agent runs `verinoda resolve "<message>" --json` (MCP `reference_resolve`)
   before researching or answering. It reports each reference as
   `<name> @ <pin> (basis: …)` with each mismatch on its own line. It never
   substitutes the default branch for a version the user named, asks only
   the returned `questions_for_user`, and states every unresolved part with
   its next step.
2. **Understand the question first.** `verinoda plan draft "<message>"`
   (MCP `question_plan_draft`). Then the agent edits the plan: it splits
   compound questions, glosses domain words, copies versions exactly as
   written, and never invents candidates. Then `verinoda plan check`: exit
   0 ready, 2 invalid, 3 needs clarification. The agent asks only the
   returned clarifications (`AskUserQuestion` in Claude Code;
   `request_user_input` or plain text in Codex) and records the answers.
   Then `verinoda analyze --plan <file>`. The answer starts with
   "Understood as / Anladığım: …", followed by one block per sub-question
   with its verdict, claims and unknowns.

Then the evidence discipline: report claims with their status, never upgrade
a status by wording, `challenge` what you rely on, report `unknown` with its
next step, and treat user critique as a hypothesis (`feedback add --process`).

## How claims stay honest

- **Relevant evidence, checked in one place.** A `*_verified` status needs one
  evidence group that is verifying, fresh and *mechanically entails* the claim
  (for example: an AST call to the target at the cited line inside the claimed
  caller; a definition spanning exactly the cited lines). Every stored status
  change passes through this check, so unrelated evidence cannot verify a
  claim on any path (API, verify, feedback, experiments, runtime runs, MCP).
- A graph edge (`EXTRACTED`/`INFERRED`) is never enough on its own. Search
  results, model summaries and user feedback are not even support for an
  inference. A claim with no evidence is `unknown`.
- **Definitive vs heuristic refutation.** Only an exhaustive check within a
  stated scope (no call to the target on the cited line, a precise resolver's
  definitive different target, …) makes a claim `contradicted`. A heuristic
  doubt lowers it one step and adds an uncertainty.
- **Facet-level staleness.** Claims depend on symbol facets (signature, body,
  name bindings, doc sections, the test set). An edit makes a claim `stale` on
  the next `update`/`analyze` only if something it depends on changed. Code
  that only moved is relocated through anchors, and a duplicated line is
  reported `ambiguous` rather than guessed.
- Critique and re-verification never raise a claim above its assessed ceiling.
  Critique is idempotent and never restores a stale or contradicted claim.
- Runtime observations are run-scoped ("observed in run R at commit C"). They
  never support an "always" claim, and calls seen through test doubles never
  support production edges.
- Nothing is deleted: user corrections supersede (the old claim is kept as
  `contradicted` with `superseded_by`), claim text is immutable, and history,
  plans, reference resolutions and runtime runs are append-only.
- Heuristics state their method and limits (`coverage.limits`,
  `uncertainties`, `derived_by`). Budget exhaustion or irrelevant retrieval
  yields `unknown` with the next verification step.

## Documentation

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — modules, state on disk, invariants
- [docs/DESIGN.md](docs/DESIGN.md) — design decisions D1-D30 and their implementation status
- [docs/BENCHMARKS.md](docs/BENCHMARKS.md) — measured comparison (no unmeasured savings claims)
- [docs/UPSTREAM.md](docs/UPSTREAM.md) — Graphify base commit, feature inventory, port method, runtime patch
- [docs/UPGRADING.md](docs/UPGRADING.md) — versioning, schema migrations, calibration changes, derived files
- [docs/AGENT-VERIFICATION.md](docs/AGENT-VERIFICATION.md) — what was verified with the real agents
- [docs/GENEL-BAKIS.md](docs/GENEL-BAKIS.md) — Türkçe genel bakış (ürün sahibi için)
- [docs/NAMING.md](docs/NAMING.md) — name availability

## License

Apache-2.0 (see `LICENSE`); portions originally under MIT (`LICENSE-MIT`).
`NOTICE` records the Graphify origin and the modifications.
