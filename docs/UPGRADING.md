# Versioning and upgrades

Verinoda follows semantic versioning once it leaves `0.x`. While in `0.x`,
minor versions may change CLI output and MCP tool results. The SQLite schema is
always migrated forward, never silently reset.

## What changes on upgrade

| Component | Upgrade path |
|---|---|
| Package / CLI | From PyPI: `uv tool upgrade verinoda`, `pipx upgrade verinoda` or `pip install -U verinoda` (npm: `npx -y verinoda@latest`). The development version: re-run the install command from the README (`uv tool install --force --reinstall-package verinoda --link-mode copy "verinoda[precise] @ https://github.com/ozcinax-star/verinoda/archive/main.zip"`, or `install.sh`); it fetches the archive again and reinstalls in copy mode. For Codex on Windows, reinstall with `uv tool install --link-mode copy <wheel-or-path>` (see the README). |
| `.verinoda/atlas.db` | `verinoda.store` keeps `meta.schema_version`. Opening an older database applies the migrations in `_MIGRATIONS` in order (v1 → v2 → ... → v6). A database newer than the installed Verinoda is refused with an explicit error instead of being modified (`verinoda doctor` reports it as a failed check). A migration is one-way: copy `.verinoda/atlas.db` before trying a newer Verinoda if you may go back to the older one. Claims, evidence and history are never deleted by a migration. |
| `.verinoda/index/` | Derived, disposable data (see below). After an upgrade that changes extraction, run `verinoda scan <repo> --force`. Claims whose dependencies changed are marked `stale` by the normal snapshot comparison, not by the upgrade itself. |
| `.verinoda/config.json` | Optional. A user config is merged key by key over `paths.DEFAULT_CONFIG`, so new keys (`budget.precise_sites`, `budget.precise_seconds`, `research.network`, `understanding.*`, and since 2026-09-26 `mcp.profile` ("core") and `query.shape_budget` (false), both written by `verinoda init`) take their defaults when absent. `verinoda mcp serve` stops with the fix when the file is not JSON or its `mcp` setting cannot be read (`mcp` not an object, `profile` not a string or unknown); only a missing setting serves the core profile. |
| 0.1.0 -> 0.2.0 (2026-09-26, D41-D46) | Two new dependencies come with the upgrade: `graspologic-native` (Leiden communities in native code, so the graph's communities, the UI's regions and GRAPH_REPORT.md are grouped differently once) and `pypdf` (PDF text). Run `verinoda update` (or `scan`): PDF and Office documents enter the graph and the search index, and on Windows images with text are read once with the built-in OCR (`VERINODA_OCR=0` to skip). `verinoda check` now reads Java and Kotlin, and the imports of TypeScript/JavaScript; `verinoda update --fast` rebuilds the graph in the background; its jar tables are cached under `.verinoda/cache/jvm/` (safe to delete). |
| After 0.2.0 (unreleased, D47-) | `verinoda when SYMBOL` says when a method runs (events, delays, the conditions around each call); JVM lambdas handed to a registration or a scheduler are `registers` edges now; Mixin handlers have `injects` edges (D48) and the impact view and change review list the registered GameTests that reach a change (D49); `verinoda backlog` links backlog items and the comments that cite them (D50, `"backlog": {"files": [...]}` in config.json for another file). The receiver sidecar (version 7) is rebuilt once on the next load (no rescan needed).; the Java check flags members out of reach and constructors no argument list fits, and `verinoda api` reads Java classes (D51; the jar tables under `.verinoda/cache/jvm/` are rebuilt once).; `.mcfunction` files enter the graph on the next `verinoda update` (D52: `verinoda datapack`).; `verinoda trace-log` and `verinoda shader` are new commands (D53, D54).; the search index is rebuilt once (schema 5: a method's javadoc is its own text, D55). |
| Agent skills / MCP registrations | Re-run `verinoda setup` in the project (it also updates the index and installs for the agents found on PATH), or `verinoda install --agent <claude\|codex> --scope <project\|user>`. This brings the skills' "understand the question first" and "references the user gives" sections and the new allowed-tools entries (`verinoda plan *`, `verinoda resolve *`). Install is idempotent, rewrites only files carrying the Verinoda ownership marker, and records what it wrote in the install manifest; `verinoda uninstall` removes exactly those entries. The MCP tool list comes from the server itself, so new tools appear without re-registration. Since 2026-09-26 the server serves the core profile (twelve of the 36 tools) by default and the skills read the CLI's text instead of `--json` (re-running setup brings them); see *Upgrading to the 2026-09-26 code* below for the tools that need `--profile full`. |
| Change review and behaviour probe (D35, D36) | Nothing to migrate: `verinoda review` stores each review as an `analyses` row (`rev_...`), `verinoda probe` runs through `experiments` like any other run. Re-run `verinoda setup` for the skill sections that call them (review before editing and before saying done; probe after editing Python functions). Since 2026-09-26 `probe` no longer refuses a function because a library module set an environment variable while it loaded (numpy sets `OPENBLAS_MAIN_FREE` when imported); project code that sets one is still refused. |
| Schema v5 and v6 (decisions, debug ledger) | Automatic on the first command: v5 adds the append-only `decisions`, `decision_briefs` and `decision_answers` tables (D33), v6 the debug ledger's sessions and attempts (D34). An older Verinoda refuses a v6 `atlas.db` ("newer than this Verinoda"). Decision records live as Markdown in the decisions folder (default `.verinoda/decisions/`); to run `decide check` in CI, commit the folder and name it in `verinoda.toml` (`[decisions] dir = "docs/decisions"`) or `pyproject.toml` (`[tool.verinoda.decisions] dir`); `.verinoda/config.json` (`decisions.dir`) is not committed. `decide check` now exits 3 (not 0) when a guard checked no file, edge or manifest, a file could not be read, a configured decisions folder does not exist, or no record exists while ADR-like files do: a CI job that treated any non-1 exit as a pass should treat 3 as "not checked". |
| Debug ledger (D34) | An agent-reported attempt must name the command it ran (`debug try --observed-output FILE --exit-code N -- <command>`). `debug close --resolved-by N` refuses a pass of a command other than the session's repro, a run where tests that failed before were skipped or not run, a tree Verinoda also saw fail, and (until `--accept-test-edit`) a tree whose tests changed since attempt 0. `debug differential` / `debug rerun` exit 3 when they did not settle it. `experiment run --ref REF --claim C` attaches the run as `qualifies` unless REF is the claim's own commit. Since 2026-09-26 `debug close --resolved-by` also refuses when the baseline passed and every failing run came after an edit (those failures are the edits' own; show the symptom with `debug rerun` on the base tree, or close with `--abandoned`), and a Minecraft GameTest run's failures are read from its summary ("N required tests failed" and the "- ns:test_id: message" lines), mapped to the test method, instead of from an exception the server logs while it starts. |
| Name check (D32) | Nothing to migrate. `verinoda check` / `verinoda api` need the `precise` extra (jedi); answers are cached under `.verinoda/cache/check/` (derived, safe to delete). Re-run `verinoda setup` to get the skill sections "Check the names code uses" and "Confirm your own sentences". MCP `code_check` / `api_members`: `env` accepts only `auto`, `none` or a virtual environment whose base interpreter is a known Python installation outside the project. `check` now exits 4 (it exited 0) for a file in another language, a notebook or Cython file, and a Python file that does not parse (all listed under `not_checked`, not counted under `files`); exit 3 still means an absent name or a lock mismatch. `api` exits 4 for a name of the project's code in another language. An attribute or keyword argument inside `try/except Exception` is `absent`, not `guarded` (cached answers of the old rule set are dropped: CHECK_VERSION 4). A hook or CI job that runs `check --diff` in a mixed repository sees exit 4 when a non-Python file changed: treat 4 as "not checked", 3 as "fix the code". |
| Truth rules (D31) | Claims written as plain text are graded again on their next `challenge`/`verify`: word overlap with the cited lines now gives `strong_inference` at most, text written with `claim add` that no typed check covers `weak_inference` at most, and a quote verifies only the quoted text. A passing run attached to a plain-text claim (`experiment run --claim`) stops at `strong_inference`; runs verify `test_run` claims that name the run. To keep a written claim verified, quote the lines (`path:12 contains: <text>`) or use a typed kind (`--kind relation\|config\|order\|location --symbol X`). `claim add --kind order` without `--symbol` takes the function the sentence places the calls in, or refuses and asks for `--symbol`. Since 2026-09-26, config and relation claims, and flow claims with a call hop, outside Python stop at `strong_inference` from their next `verify`/`challenge` (a SCIP answer still verifies relations). |
| Exact names, one build at a time, fresh index (D37) | Nothing to migrate. `receiver_calls.json` v2 is recomputed on the first load (its per-file facts are reused); `.verinoda/index/fresh_ignored.json` changed format (v2), and an older one is ignored and rewritten. Output and exit-code changes are listed below. |
| Upstream (Graphify) base | Maintainers only: `python tools/port_upstream.py <graphify-checkout-at-new-commit>`, review the diff, run `pytest tests` and `pytest tests_upstream`, update `docs/UPSTREAM.md` (commit, test table, inventory). Check that `index.install_path_identity_memo()` still finds `watch._StoredSourcePaths` (`tests/test_index.py` covers it). |

## Upgrading to the 2026-09-26 code

Nothing to migrate: the schema stays v6, and the derived files rebuild themselves (see *Derived
files are disposable*). Re-run `verinoda setup` (or `verinoda install`) to get the skills that read
the CLI's text instead of JSON. What a script, a CI job or an agent may notice:

### Exit codes

- `check` and `api` exit 4 when nothing is absent but something asked for was not checked (it was
  0); `decide check` exits 3 when something could not be checked (it was 0). See the *Name check*
  and *Schema v5 and v6* rows above.
- `decide check` (and MCP `decision_check`) waits up to 120 s (MCP 30 s) for an index build that is
  already running. If the refresh still fails, its `no_edge` guards are `unknown` (`graph_stale`
  says why) and the exit code is 2, not 0.
- `verinoda trace` lists an endpoint that names several symbols (`status: ambiguous`, `hints`)
  instead of picking the first; exit 2 as for an unresolved endpoint. Pass `path/file.py::Name` or
  a node id.
- `verinoda map --view impact --target X` exits 2 when a target does not name one symbol exactly;
  the JSON has `resolution` (status, note, candidates) next to `unresolved`. Targets taken from git
  are reported the same way but do not change the exit code.
- `scan` / `update` return `mode: busy` (exit 1 after the CLI's 10-minute wait) when another build
  of the project holds the lock (`.verinoda/build.lock`); the hint never suggests `--force`.

### Names and a stale index (D37)

- `trace`, `map --view impact --target` and MCP `node_inspect` resolve names exactly. A detected
  copy of the project or a reference tree gives way to the project's own code; definitions in test
  code, examples, fixtures and vendored folders give way to the product's own; a helper nested in a
  function gives way to a module- or class-level definition. What gave way is listed in
  `set_aside` and in a note ("also defined, set aside: ..."). A name that a top-level function and
  a method define in different files is `ambiguous` (candidates listed). Pass `path/file.py::Name`
  or a node id to pick one.
- `trace` no longer traces a similar node for a code name that is not a symbol of the index (a
  module constant, an attribute: `config.DISCOUNT_THRESHOLD`): it is unresolved with `not_a_symbol`
  (where the name occurs, the nearest symbols as hints). `module.name` where the module imports
  `name` resolves to the imported function. Plain words still resolve by similarity (`fuzzy`).
- `trace` text output has a `resolved:` line (the node each endpoint resolved to); `map --view
  impact` has a `resolution` row for every name target.
- MCP `node_inspect` returns `error: ambiguous` (with `candidates`) for a name several symbols
  carry, and `error: not_indexed` for a name only a changed file spells.
- `query --json`, `trace --json`, `map --json` and the MCP read tools carry `stale_count` /
  `stale_files` when files changed since the index (the keys are absent when it is current). The
  note ("N file(s) changed since the index") never lists files the index does not cover (a nested
  git repository or submodule, untracked build/ or dist/ output), so `verinoda update` always
  clears it.
- `query`: "not in the index yet" means the version of the changed file the index describes does
  not spell the name; "may not be in the index yet" when that could not be read. A name the
  indexed file already had is not reported.
- `analyze` results carry `index_refresh` (`ran`, `seconds`, or `skipped` with `stale_files`); the
  refresh time is no longer part of `usage.elapsed_s`. New option `--refresh auto|inline|skip`;
  `inline` waits for a running build (up to 10 minutes) instead of answering from the previous
  index. When a refresh is skipped, a changed file that spells the question's subject is an
  unknown of its own and caps that sub-question at `met_with_inference`.
- The MCP server starts `verinoda update` in the background when analyze skipped a slow refresh. It
  runs in isolated mode (`python -I`) from the temp folder with the server's own package first on
  `sys.path`, and logs to `.verinoda/index/background_update.log`.

### Model-facing output

- `verinoda query` text: no `# <question>` first line; `expanded:` shows at most three `from->to`
  pairs ("(+N more)"; `--json` keeps all with why); the follow-up hint reads
  `next: verinoda query "…" --max-chars N`; an item's `## path:a-b` header shows only its name
  when the passage below starts at its first line; passage windows are dedented. The `## path:a-b`
  and `  path:x-y` locators are unchanged. With a small `--max-chars` the note on left-out
  candidates is always printed (shortened to fit); with no room for any item the text reads
  `… N candidates not shown (budget too small)` instead of
  `no candidate locations to show for this question`.
- `verinoda analyze` without `--json` prints a new layout (see the README): sub-question blocks,
  claims as `[status] text {evidence the text lacks} (claim id)`, confidence only when below its
  status's cap, a `plan links:` line with where the question's words resolved in the code
  (`word -> path:a-b`, weak links left out), the passages in full. Scripts that parsed the old text
  should read `--json`, which is unchanged in content.
- `--json` output is compact (one line) when stdout is not a terminal.
- `verinoda map` without `--json` prints a summary per view (counts, the largest folders and
  packages, the heaviest dependencies, what was left out) instead of the views' JSON; `--max-lines`
  defaults to 12 lines per view, 40 for one `--view`. The config view also lists `.yml`/`.toml`
  config files that are not graph nodes.
- Benchmark result files are schema 3 (`facts_shown`, `shown_per_1k_tokens`); the
  `verinoda_analyze` approach now scores the default text of analyze.

### MCP server

- MCP `analyze` returns a lean response: `analysis_id`, `snapshot`, `understood_as`,
  `subquestions` (id, intent, text, status, answer_claim_ids, flags, decision_brief when present),
  `plan_check` (status, problems, non-weak links as strings), `claims` (id, status, text;
  `confidence`, `evidence`, `uncertainties`, `not_challenged` only when they add something),
  `claims_in_passages` (verified claims left out because the passages print their lines; only
  lines the response still prints count, so when the response cap cuts passages, their claims are
  listed again), `unknowns`, `critique`, `budget_exhausted` and `passages`. `steps`, `usage`,
  `question`, `intents` and `plan_source` are gone; `plan_audit`, `claim_inspect` and
  `verinoda analyze --json` have the full record.
- The server serves the core profile (twelve tools: project_query, analyze, node_inspect,
  relation_trace, map_view, claim_inspect, claim_list, evidence_inspect, index_update, code_check,
  decision_check, change_review) by default. An agent that called question-plan, reference,
  feedback, decision-record/brief, debug, experiment, runtime or claim re-check tools over MCP needs
  `verinoda mcp serve --profile full` (edit the MCP entry's args) or `"mcp": {"profile": "full"}`
  in the project's `.verinoda/config.json`; the CLI has every command either way. No
  re-registration is needed otherwise. Tool descriptions are shorter; tools carry no output schema.
- `verinoda mcp serve` refuses a config.json whose `mcp` setting is malformed (see the config row
  above).
- Codex on an editable or hardlinked install (`verinoda doctor` warns `sandbox_readable`):
  re-running `verinoda setup` / `verinoda install --agent codex` changes the MCP entry's args to end
  with `--profile full`, since the Codex sandbox may not import such an install and MCP must then
  serve every tool the skill uses (see the README).

### Smaller changes

- `verinoda probe`: an environment variable set while a library module loads (numpy's
  `OPENBLAS_MAIN_FREE`) is no longer a side effect; project code that sets one still is.
- Debug ledger: Minecraft GameTest summaries are parsed as the failures, mapped to the test method;
  `debug close --resolved-by` refuses when the baseline passed and every failing run came after an
  edit (see the *Debug ledger* row above).

## Upgrading to the round-3 code (schema v3 / v4)

### Schema

- **v2** adds triggers so that claim-evidence links, snapshots, experiments,
  analyses and research records can no longer be deleted, and links can no
  longer be changed.
- **v3** adds one migration for all round-3 subsystems:
  - new tables: `question_plans`, `file_facts`, `claim_deps`,
    `evidence_locations`, `runtime_runs`, `runtime_calls`, `resolutions`,
    `reference_resolutions`, `file_stat`;
  - new columns: `feedback.plan_id`, `analyses.plan_id`, `claims.claim_key`,
    `claims.verified_at` and `claim_evidence.grp` (evidence groups).
- **v4** makes `claims.text` and `claims.created_at` immutable (trigger
  `no_update_claim_identity`). A correction must supersede a claim; it can no
  longer rewrite one in place. A script that updates `claims.text` directly
  now fails with "a claim's text and created_at are immutable; supersede it
  instead".

### Claims created before the upgrade

- They have no recorded dependencies (`claim_deps`), so invalidation uses the
  old file-level rule for them: any change to a cited or subject file makes
  them stale. Claims created after the upgrade get facet-level dependencies.
- Their evidence has no anchor. It is re-checked by exact hash, and a moved
  block is relocated only when it occurs once in the file (`ambiguous`
  otherwise). It is never relocated by guess.

### Status changes that are calibration, not regressions

The trust engine checks evidence more strictly than earlier versions. The same
claim can therefore get a *lower* status after an upgrade. This is intended:
the earlier status was not justified by its evidence.

- **A claim with zero evidence is now `unknown`** (it was `weak_inference`).
  For example, `verinoda claim add "..."` without `--source` gives
  `unknown`.
- **A verified status needs relevant evidence.** `*_verified` requires one
  evidence group that is verifying, fresh and graded `full` by `entail`: the
  evidence must mechanically state the claim, not merely exist. The check runs
  on every path that changes a status (`claims.check_status`). A claim that
  was `statically_verified` by term overlap can drop to `strong_inference` or
  lower on its next `verify` or `challenge`.
- **Search results, model summaries and user feedback are not support.** On
  their own they now give `weak_inference` at most; before, they were enough
  for `strong_inference`.
- **Only definitive refutations contradict.** A heuristic refutation now
  lowers a claim one step and adds an uncertainty instead of making it
  `contradicted`.
- **Critique is idempotent and never raises.** Re-running critique on an
  unchanged claim gives the same result. It no longer lifts a `stale` or
  `contradicted` claim back to `strong_inference`.
- **Foreign evidence is rejected.** `feedback resolve --verdict
  confirmed|qualified` accepts only evidence already linked to the claim or
  produced by that feedback's own protocol runs, and it never raises the
  claim.
- **`claim add --status stale` is no longer accepted.** Only `update` and
  `scan` set `stale`.

Status shares in older analyses and older benchmark result files are therefore
not directly comparable with new runs.

### Output and exit-code changes

- `verinoda query` prints the plain-text context by default. Use `--json`
  for the structured result. MCP `project_query` has `format="text"` by
  default.
- Every `analyze` runs through a question plan. The CLI exits with 2 for an
  invalid plan and 3 when the plan needs clarification. `verinoda resolve`,
  `observe` and `resolve-call` also use exit code 3 for "needs more".
- `verinoda index` blocks upstream installer, hook and `~/.graphify` commands
  (exit 2). See [UPSTREAM.md](UPSTREAM.md).

## Derived files are disposable

Everything under `.verinoda/index/` is derived from the repository and can be
deleted at any time. `verinoda scan <repo> --force` rebuilds it, and most of
it is also rebuilt automatically:

| File | Built by | Rebuilt automatically when |
|---|---|---|
| `graph.json`, `GRAPH_REPORT.md`, `cache/` (and `graph.html` only with `GRAPHIFY_VIZ_NODE_LIMIT` set; an old one is removed) | `scan` / `update` (vendored Graphify pipeline) | the working tree changed (`update`, or `analyze` refreshing first) |
| `search.db` | `scan` / `update` (`search_index.update`) | its schema or tokenizer version differs (the next query rebuilds it) |
| `receiver_calls.json` (v3) | `scan` / `update` (receiver-call pass: the class a calling file can see) | it does not match `graph.json`, or is an older version (it is recomputed; a v2 file's per-file facts are reused) |
| `lexicon.json` | `scan` / `update` (`lexicon.build`, incremental by file hash) | the next `scan` / `update` |
| `scip_fresh.json` | the first read of a user-supplied `index.scip` | delete it together with `index.scip`, then `scan --scip FILE` again |
| `build_stats.json` | `scan` / `update` (how long the last graph build took) | the next build |
| `fresh_ignored.json` (v2) | the first read after a snapshot (which new paths git ignores) | a new snapshot; an older version is ignored and rewritten |
| `background_update.log` | the MCP server's background `update` | appended; delete at will |

`index.scip` is the only file here that Verinoda cannot rebuild itself: it is
produced by the user's own SCIP indexer.

`.verinoda/build.lock` stays after a build (the lock is on the open file, not
its existence); deleting it while no build runs is harmless.
`.verinoda/build.owner.json` exists only while a build runs.

The caches inside `atlas.db` (`file_facts`, `resolutions`, `file_stat`) are
derived too. They are keyed by file content or stat, and they are recomputed
when they do not match. Everything else in `atlas.db` is audited history and is
never deleted.

## Optional extra: `verinoda[precise]`

Precise call-site resolution (`precise.py`, jedi) is optional. Without it,
every precise lookup answers "no precise answer" and relation claims that
depend on a method's receiver type stay `strong_inference`.

```bash
pip install "<wheel-or-path>[precise]"                                       # into a venv
uv tool install --link-mode copy --with "jedi>=0.19.2,<0.21" <wheel-or-path>  # uv tool
verinoda doctor          # "precise" check: available or why not
```

## Checking the state after an upgrade

`verinoda doctor` reports:

- the installed version and package layout (hardlinked or editable installs
  warn about agent sandboxes), and the upstream base commit;
- graph and snapshot freshness, the schema version and claim counts;
- the search index (schema/tokenizer version, units, stale files), whether
  `receiver_calls.json` matches `graph.json`, the lexicon and seed-dictionary
  size;
- whether precise resolution is available, whether the project's interpreter
  has `sys.monitoring` (without it the tracer falls back to `setprofile`, which
  is much slower), and the SCIP index and its share of fresh documents;
- the reference network mode, the HTTP cache size, rate-limited hosts and
  whether `packaging` is importable;
- agent skill and MCP installation state, and missing optional dependencies.
