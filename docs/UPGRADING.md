# Versioning and upgrades

Verinoda follows semantic versioning once it leaves `0.x`. While in `0.x`,
minor versions may change CLI output and MCP tool results. The SQLite schema is
always migrated forward, never silently reset.

## What changes on upgrade

| Component | Upgrade path |
|---|---|
| Package / CLI | From PyPI: `uv tool upgrade verinoda`, `pipx upgrade verinoda` or `pip install -U verinoda` (npm: `npx -y verinoda@latest`). The development version: re-run the install command from the README (`uv tool install --force --reinstall-package verinoda --link-mode copy "verinoda[precise] @ https://github.com/ozcinax-star/verinoda/archive/main.zip"`, or `install.sh`); it fetches the archive again and reinstalls in copy mode. For Codex on Windows, reinstall with `uv tool install --link-mode copy <wheel-or-path>` (see the README). |
| `.verinoda/atlas.db` | `verinoda.store` keeps `meta.schema_version`. Opening an older database applies the migrations in `_MIGRATIONS` in order (v1 → v2 → ... → v6). A database newer than the installed Verinoda is refused with an explicit error instead of being modified (`verinoda doctor` reports it as a failed check). A migration is one-way: copy `.verinoda/atlas.db` before trying a newer Verinoda if you may go back to the older one. Claims, evidence and history are never deleted by a migration. |
| `.verinoda/index/` | Derived, disposable data (see below). Since 0.4.0 the graph records which extraction built it (`build_stats.json`): after an upgrade the next `verinoda update` rebuilds the graph by itself, unchanged files included, and `verinoda doctor` says so until then (`scan <repo> --force` still works). Claims whose dependencies changed are marked `stale` by the normal snapshot comparison, not by the upgrade itself. |
| `.verinoda/config.json` | Optional. A user config is merged key by key over `paths.DEFAULT_CONFIG`, so new keys (`budget.precise_sites`, `budget.precise_seconds`, `research.network`, `understanding.*`, and since 2026-09-26 `mcp.profile` ("core") and `query.shape_budget` (false), both written by `verinoda init`) take their defaults when absent. `verinoda mcp serve` stops with the fix when the file is not JSON or its `mcp` setting cannot be read (`mcp` not an object, `profile` not a string or unknown); only a missing setting serves the core profile. |
| 0.1.0 -> 0.2.0 (2026-09-26, D41-D46) | Two new dependencies come with the upgrade: `graspologic-native` (Leiden communities in native code, so the graph's communities, the UI's regions and GRAPH_REPORT.md are grouped differently once) and `pypdf` (PDF text). Run `verinoda update` (or `scan`): PDF and Office documents enter the graph and the search index, and on Windows images with text are read once with the built-in OCR (`VERINODA_OCR=0` to skip). `verinoda check` now reads Java and Kotlin, and the imports of TypeScript/JavaScript; `verinoda update --fast` rebuilds the graph in the background; its jar tables are cached under `.verinoda/cache/jvm/` (safe to delete). |
| 0.2.0 -> 0.3.0 (2026-09-27, D47-D57) | `verinoda when SYMBOL` says when a method runs (events, delays, the conditions around each call); JVM lambdas handed to a registration or a scheduler are `registers` edges now; Mixin handlers have `injects` edges (D48) and the impact view and change review list the registered GameTests that reach a change (D49); `verinoda backlog` links backlog items and the comments that cite them (D50, `"backlog": {"files": [...]}` in config.json for another file). The receiver sidecar (version 7) is rebuilt once on the next load (no rescan needed).; the Java check flags members out of reach and constructors no argument list fits, and `verinoda api` reads Java classes (D51; the jar tables under `.verinoda/cache/jvm/` are rebuilt once).; `.mcfunction` files enter the graph on the next `verinoda update` (D52: `verinoda datapack`).; `verinoda trace-log` and `verinoda shader` are new commands (D53, D54).; the search index is rebuilt once (schema 5: a method's javadoc is its own text, D55).; Java overloads are separate symbols and calls bind by argument count (D57): run `verinoda scan .` once (an `update` re-reads only changed files); the receiver sidecar (version 8) is rebuilt on the next load. |
| 0.3.0 -> 0.3.2 (2026-09-27, D58-D59; the tag v0.3.1 published nothing) | Nothing to run: the answers change (less noise, string-keyed settings for config questions), the history view is faster, and a PyPI / npm install's `verinoda --version` names the commit it was built from. |
| 0.3.2 -> 0.4.0 (2026-10-01, D60-D136) | Automatic on the first command: `atlas.db` goes from schema v6 to v9 (v7 per-test outcomes of the debug ledger's runs and the quarantine list, v8 a memory's expiry, v9 the persistent test-to-code map); an older Verinoda then refuses it, so copy `.verinoda/atlas.db` first if you may go back. The next `verinoda update` rebuilds the graph by itself (the extraction changed). Re-run `verinoda setup` for the skills and the MCP registrations (the core menu changed with D61). Every new command and every changed output is listed per decision under *Upgrading from 0.3.2 (D60-D136)* below. |
| Agent skills / MCP registrations | Re-run `verinoda setup` in the project (it also updates the index and installs for the agents found on PATH), or `verinoda install --agent <claude\|codex> --scope <project\|user>`. This brings the skills' "understand the question first" and "references the user gives" sections and the new allowed-tools entries (`verinoda plan *`, `verinoda resolve *`). Install is idempotent, rewrites only files carrying the Verinoda ownership marker, and records what it wrote in the install manifest; `verinoda uninstall` removes exactly those entries. The MCP tool list comes from the server itself, so new tools appear without re-registration. Since 2026-09-26 the server serves the core profile (twelve of the tools; the menu changed again with D61) by default and the skills read the CLI's text instead of `--json` (re-running setup brings them); see *Upgrading to the 2026-09-26 code* below for the tools that need `--profile full`. |
| Change review and behaviour probe (D35, D36) | Nothing to migrate: `verinoda review` stores each review as an `analyses` row (`rev_...`), `verinoda probe` runs through `experiments` like any other run. Re-run `verinoda setup` for the skill sections that call them (review before editing and before saying done; probe after editing Python functions). Since 2026-09-26 `probe` no longer refuses a function because a library module set an environment variable while it loaded (numpy sets `OPENBLAS_MAIN_FREE` when imported); project code that sets one is still refused. |
| Schema v5 and v6 (decisions, debug ledger) | Automatic on the first command: v5 adds the append-only `decisions`, `decision_briefs` and `decision_answers` tables (D33), v6 the debug ledger's sessions and attempts (D34). An older Verinoda refuses a v6 `atlas.db` ("newer than this Verinoda"). Decision records live as Markdown in the decisions folder (default `.verinoda/decisions/`); to run `decide check` in CI, commit the folder and name it in `verinoda.toml` (`[decisions] dir = "docs/decisions"`) or `pyproject.toml` (`[tool.verinoda.decisions] dir`); `.verinoda/config.json` (`decisions.dir`) is not committed. `decide check` now exits 3 (not 0) when a guard checked no file, edge or manifest, a file could not be read, a configured decisions folder does not exist, or no record exists while ADR-like files do: a CI job that treated any non-1 exit as a pass should treat 3 as "not checked". |
| Debug ledger (D34) | An agent-reported attempt must name the command it ran (`debug try --observed-output FILE --exit-code N -- <command>`). `debug close --resolved-by N` refuses a pass of a command other than the session's repro, a run where tests that failed before were skipped or not run, a tree Verinoda also saw fail, and (until `--accept-test-edit`) a tree whose tests changed since attempt 0. `debug differential` / `debug rerun` exit 3 when they did not settle it. `experiment run --ref REF --claim C` attaches the run as `qualifies` unless REF is the claim's own commit. Since 2026-09-26 `debug close --resolved-by` also refuses when the baseline passed and every failing run came after an edit (those failures are the edits' own; show the symptom with `debug rerun` on the base tree, or close with `--abandoned`), and a Minecraft GameTest run's failures are read from its summary ("N required tests failed" and the "- ns:test_id: message" lines), mapped to the test method, instead of from an exception the server logs while it starts. |
| Name check (D32) | Nothing to migrate. `verinoda check` / `verinoda api` need the `precise` extra (jedi); answers are cached under `.verinoda/cache/check/` (derived, safe to delete). Re-run `verinoda setup` to get the skill sections "Check the names code uses" and "Confirm your own sentences". MCP `code_check` / `api_members`: `env` accepts only `auto`, `none` or a virtual environment whose base interpreter is a known Python installation outside the project. `check` now exits 4 (it exited 0) for a file in another language, a notebook or Cython file, and a Python file that does not parse (all listed under `not_checked`, not counted under `files`); exit 3 still means an absent name or a lock mismatch. `api` exits 4 for a name of the project's code in another language. An attribute or keyword argument inside `try/except Exception` is `absent`, not `guarded` (cached answers of the old rule set are dropped: CHECK_VERSION 4). A hook or CI job that runs `check --diff` in a mixed repository sees exit 4 when a non-Python file changed: treat 4 as "not checked", 3 as "fix the code". |
| Truth rules (D31) | Claims written as plain text are graded again on their next `challenge`/`verify`: word overlap with the cited lines now gives `strong_inference` at most, text written with `claim add` that no typed check covers `weak_inference` at most, and a quote verifies only the quoted text. A passing run attached to a plain-text claim (`experiment run --claim`) stops at `strong_inference`; runs verify `test_run` claims that name the run. To keep a written claim verified, quote the lines (`path:12 contains: <text>`) or use a typed kind (`--kind relation\|config\|order\|location --symbol X`). `claim add --kind order` without `--symbol` takes the function the sentence places the calls in, or refuses and asks for `--symbol`. Since 2026-09-26, config and relation claims, and flow claims with a call hop, outside Python stop at `strong_inference` from their next `verify`/`challenge` (a SCIP answer still verifies relations). |
| Exact names, one build at a time, fresh index (D37) | Nothing to migrate. `receiver_calls.json` v2 is recomputed on the first load (its per-file facts are reused); `.verinoda/index/fresh_ignored.json` changed format (v2), and an older one is ignored and rewritten. Output and exit-code changes are listed below. |
| Upstream (Graphify) base | Maintainers only: `python tools/port_upstream.py <graphify-checkout-at-new-commit>`, review the diff, run `pytest tests` and `pytest tests_upstream`, update `docs/UPSTREAM.md` (commit, test table, inventory). Check that `index.install_path_identity_memo()` still finds `watch._StoredSourcePaths` (`tests/test_index.py` covers it). |

## Upgrading from 0.4.0 (D137-D165)

### D137: Trigram regex index

New command `verinoda search`: exact and regular-expression search narrowed by a trigram index that it builds on
first use in `.verinoda/index/trigram.db` (disposable; delete it or pass `--rebuild` to start over). No MCP tool
changes.

### D138: Mutation testing scoped to the diff

New command `verinoda mutate`: mutation testing scoped to the diff (Python). It runs the selected tests once per
mutant through `experiments.run`, so each mutant is a recorded experiment under `.verinoda/runs/`. Nothing else
changes; no MCP tool changes.

### D139: Guards written as programs

A decision record may now carry a `script path=FILE.py [timeout=SECONDS]` guard: a Python file in the repository
that defines `check(guard)` and reports `guard.violation(path, line, why)` or `guard.possible(...)`, reading the
graph and the stored claims through `guard`. It runs the project's own code with your privileges, like the
project's tests: `verinoda decide check` and `decide baseline` run it only in a project you trust (`verinoda
trust`), in a child process with a scrubbed environment, a timeout and an audit hook that is a tripwire against
accidental network, processes and file writes, not a sandbox. Through MCP, in `update` and in `what-if` a script
guard is `unknown`, and MCP neither records nor accepts one. A script that raises, exits or times out makes the
check exit 3. `guards.check` has a new keyword `run_scripts` (default False); `decisions.record`, `add_guards`
and `accept` have `allow_scripts` (default True). `.pre-commit-hooks.yaml` offers the `verinoda-decide-check`
hook for the pre-commit framework (3.2.0 or later).

### D140: Installers for more agents

`verinoda install --agent` and `verinoda setup --agents` accept `cursor`, `gemini`, `copilot`, `kiro`,
`continue` and `aider`. `setup --agents all` now means every supported agent found (its folder in the
project or home folder, or its program on PATH); it no longer installs Claude Code and Codex when their
CLI is missing. The report lists why each agent was found (`agents_found`). Install and uninstall keep the
existing rules: files Verinoda did not write are never overwritten, and uninstall removes only what the
manifest lists. A Claude Code `.mcp.json` manifest item now also records `created_dirs`, so the first
install after upgrading rewrites the manifest once (no agent file changes).

### D141: Mixin injection points

New command `verinoda mixin-check [FILE ...] [--json]`: every Mixin method selector, `@At` member target and
`@Shadow` field or method checked against the target class's bytecode on the build's classpath, `exists` /
`absent` (with `jar!class` evidence and the nearest real names as a suggestion) or `unknown` with the next step.
Exit 3: absent; 4: something unknown; 2: no `@Mixin`. `jvmclass.class_code()` and `accesscheck.ClassFiles.code()`
are new; nothing else changes.

### D142: Dependency structure matrix and C4 model check

New map views `dsm` (`verinoda map --view dsm [--group-by folder|tag] [--depth N]`: a dependency structure matrix
between folders or `[architecture.tags]`) and `model` (`verinoda map --view model [--model workspace.dsl]`: a C4
model - Structurizr DSL with the element property `"verinoda.code"`, or `[architecture.model] relations` between
tags in `verinoda.toml` - against the code's dependencies). MCP `map_view` accepts both. `verinoda ui` has a
Dependency matrix page. Nothing to migrate.

### D143: Hooks on the agent's own tool calls

New `verinoda agent-hooks install|uninstall|status` (Claude Code, Codex, Cursor; project or user scope) and the
hook command `verinoda tool-hook`. MCP `grep_context` also takes `command` (a shell line); `pattern` is optional.
The Claude Code hooks template has a Bash entry. A project that copied the old template keeps working; running
`agent-hooks install` replaces its entries with the current ones.

### D144: Scripted aggregation over search results

New command `verinoda inventory`: named searches counted by file, line or symbol, a condition over their counts
(`--where "a and not b"`), grouped with their lines; exact or marked as lower bounds. Nothing to migrate.

### D145: Mixin conflicts across mods

`verinoda mixin-check` gains `--conflicts`, `--with PATH` (repeatable) and `--log PATH`: Mixins of several mods
on the same target method, the project's and those of the mod jars found locally (never downloaded), each pair
`conflict`, `order_dependent` or a shared target with compatible kinds (`strong_inference`), and each Mixin
failure of a log (a `.gz` one too) named with its mod. Exit 3: a clash or a failure named with its mod; 4: no
other mod's Mixins read, a failure's mod not found, or a log that is not text (3 wins over 4); 2: no Mixin. Without these options `mixin-check` is unchanged. `jvmclass.class_annotations()` is
new; `mixincheck.MixinClass` gains `priority`, `target_names` and `injections`. No MCP tool changes.

### D146: Cross-service edges

New command `verinoda routes [--no-table] [--json]`: the route table and every client call with a URL, each
linked to the one handler its path and method fit, ambiguous (every candidate listed, no edge), unmatched or a
method mismatch; tRPC, Python gRPC, GraphQL and named events too. `trace` now crosses these edges when there is
no call path: a hop with `kind: "cross_service"`, `relation` `requests` / `rpc_calls` / `emits`, INFERRED,
`derived_by=verinoda.cross_service`, the call in `at` and the handler's declaration in `route_at`; paths found
before are unchanged, and `--mode any` can report `reachability: "cross_service"`. A no-path result can carry
`cross_service_ambiguous`. `receiver_calls.json` is version 10 (a `cross_service` block): an older one is
recomputed on the first load, nothing to migrate. The graph gains edges with three new relations; tools that
list every relation (`export`, the UI) show them, and `map --view dead` counts a handler reached through one as
reached. `analyze` flow claims still state calls only: they do not cross these edges.

### D147: Runtime flaws from traces

- `verinoda observe` also records SQL statements (sqlite3, and other drivers through SQLAlchemy) and samples the
  stack, and reports `runtime_flaws`: N+1 queries with the call path from the test to the statement and the loop,
  repeated identical SQL and slow paths, each `observed` for that run with its `file:line`, the "this is an N+1"
  reading `strong_inference` at most. Thresholds: `--n-plus-one N` (5), `--repeated N` (3), `--slow-ms MS` (100),
  `--slow-share F` (0.2); `--no-flaws` leaves the recording out. It adds a second plugin to the run
  (`verinoda_flaws`) and its file `artifacts/flaws.jsonl`; on the measured runs it cost 2-8% of the observe
  time. With it on, sqlite3 connections and cursors in the tests are subclasses of the ones asked for (exact-type
  checks on them fail; `isinstance` holds). The MCP tools and their output are unchanged; `trace.observe` records
  flaws only with `flaws=True`.

### D148: Derived facts

Schema v10 (after v9, the test-to-code map): `atlas.db` gains `facts` and the append-only `fact_history`
(migrated on first open; an older Verinoda then refuses the database as written by a newer one, so copy
`.verinoda/atlas.db` first if you may go back). New command `verinoda fact add|list|show|refresh|retire`. `scan`
and `update` results gain a `facts` key (and a `facts:` line) only in a project that has facts; `update` then
re-runs stale search facts within 20 facts and 10 s (an `update --fast` that defers the graph only lowers
them), which `facts.refresh_on_update: false` in `.verinoda/config.json` turns off. `query --json`, MCP
`project_query` and `analyze` gain a `facts` list when the question names a fact (a `facts_error` when they
could not be read). No MCP tool added; the tool count is unchanged.

### D149: Native file watcher

`verinoda ui --watch` is woken by the operating system's file events (Windows `ReadDirectoryChangesW`, Linux
`inotify`, `watchdog` elsewhere when installed) and falls back to polling; nothing to configure. `/api/version`'s
`watch` block says which (`backend`, `note`). New: `verinoda mcp serve --watch` runs a fast update when the project's files
change; add `--watch` to the server's arguments in the agent's MCP configuration to use it. `ui.server.Watcher`
moved to `verinoda.fswatch.Watcher` (still importable from the old place).

### D150: Control and data dependence

New command `verinoda slice PATH:LINE` (`--var`, `--arg`, `--forward`, `--depth`): a backward slice of a Python
line inside its function, across callers' arguments via the index, or a forward slice. Nothing to migrate.

### D151: Package existence and slopsquatting check

`verinoda check --deps --registry [new|all] --network on|cache` asks PyPI, npm, crates.io, Maven Central and the
Go proxy whether the dependencies a change adds (and the undeclared imports) exist, are young, little used,
yanked, deprecated or taken down, and flags look-alike names of popular packages; findings `not_in_registry`,
`registry_signal` and `lookalike_name`, exit 3; a bad `--diff` revision is exit 2. `decide ask ... --registry`
does the same for a proposed package. Off by default; only public package names are sent (local, VCS and URL
sources are skipped, names behind a private registry the project configures are never sent); answers cached in
`.verinoda/research/package-check.json`. Dependency items of `guards.declared_dependencies` gain `declared`
(the name as written). Nothing to migrate.

### D152: Full type and name check through tsc, pyright or mypy

`verinoda check` gains `--checker tsc|pyright|mypy|auto` and `--checker-timeout SECONDS`: the project's own type
checker is run (never installed or downloaded; in a project not trusted with `verinoda trust`, only a checker
outside the repository, never pyright, and mypy only without `plugins`/`python_executable`) and its errors on the
lines in scope are sites with `status` `observed` and a `checker` field {tool, version, config, code}; a
TypeScript file it compiled is no longer listed under `not_checked`. A new verdict `mismatch` (a call or type that
does not fit; exit 3) appears in `summary` of every `check` result (0 without `--checker`) and sorts with
`absent`; the result gains `checker` (one entry per run) when the option is given, and a site Verinoda and the
checker both report may carry `confirmed_by`, `checker_says` or `own_verdict`. A checker not found, not run,
timed out or failing is exit 4 with the next step. Without the option `check` is unchanged. No MCP tool changes;
MCP `code_check` never runs a checker.

### D153: Runtime diff between base and head

`verinoda observe` gains `--compare REF`: the same tests are also run at commit REF and the result gains
`runtime_diff` (calls, library calls, SQL, routes, exceptions raised, test outcomes added, removed or changed).
`review --observe` runs the selected tests at the review's base too (runs of another commit are never taken as the
latest run) and adds `tests.observe.runtime_diff`; it also
records SQL now (the flaws recorder), so it takes about twice as long. Runs gain `raise` records (Python 3.12+) and
per-test `exc`; `runtime.trace.observe()` takes `ref=`. Nothing to migrate; the MCP tools are unchanged.

### D154: Taint analysis

New command `verinoda taint` (exit 3 when a path is found) and the library data `verinoda/data/taint_python.json`.
A project may add a `[taint]` table to `verinoda.toml` (`sources`, `sinks = [{match, arg, keyword, kind,
when_keyword, safe_keywords}]`, `sanitizers` as calls or `{match, kinds}`, `builtin`); unknown keys are errors. `sarif.export(res, "taint")` is new. No MCP change; nothing to migrate.

### D155: Code query language

New command `verinoda q "QUERY"`: a declarative query over the graph (node patterns with kinds, edges with a
relation, a direction and a bounded length, joins on shared variables, `WHERE` with `not` and `exists`, `RETURN`
with `count`, `LIMIT`) answered with rows that cite their evidence; `--verify` re-reads the cited call sites;
`--max-rows`, `--max-expansions` and `--timeout` bound it. Nothing to migrate; the MCP tools are unchanged (40).

### D156: Import JVM checker findings

New command `verinoda import-findings FILE ...` reads Error Prone and NullAway diagnostics from javac, Gradle,
Maven and Ant logs, `jdeps -jdkinternals` output and SARIF files as claims with the tool named (`--tool
auto|errorprone|nullaway|jdeps`, `--path`, `--limit`, `--json`; exit 4 when no file was read). New module
`verinoda/jvm_findings.py`. Nothing is run and nothing is written. Nothing to migrate; the MCP tools are unchanged
(40).

### D157: Mixin debug export as evidence

`verinoda mixin-check` gains an `export` section in its JSON and text output and `--export PATH` (repeatable):
where Mixin's debug export is found (`.mixin.out`, `run/.mixin.out`, `runs/*/.mixin.out`, or given), each of the
project's injectors is `applied` / `merged` (`observed`, the exported class file as evidence), `not_applied` or
`unknown`, and each target class lists what the export holds beyond the original. Without an export the section
is one `unknown` with how to turn it on (`-Dmixin.debug.export=true`); the existing rows, counts and exit codes
are unchanged. `jvmclass.class_code()` returns the class's interfaces (`ifaces`). A Mixin claim of `analyze` may
carry a second, `experiment`-type evidence citing the exported class, and its uncertainties now name the export.
No MCP tool is added.

### D158: ORM, DI and database schema

New command `verinoda schema` (exit 3 with `--db` when the database and the code differ). The graph gains `table:<name>`
nodes (`file_type: schema`) and `maps_to`, `writes_table`, `reads_table`, `migrates` and `injects` edges; the receiver
sidecar is version 11 (an older one is recomputed on the first load). `map --view dataflow` paths may end one hop
later, at a table, with `table` and `table_at`; the `sink` is still the function that writes or reads it, whose
`sink_kinds` may now be `table-write` / `table-read` (a function with only ORM table uses is a sink it was not
before). `q` accepts the five relations; the `ui` local graph, impact and path follow them; `trace`, `butterfly` and
`node_inspect` set a table aside for any symbol of the same name (test code and nested functions included). With
`--db`, a framework's bookkeeping tables are listed under `framework_tables` and no longer make exit 3. Tools that
list every node or relation (export, the UI) show them. No MCP change.

### Typed questions, batched

New command `verinoda tq` (exit 0 when every answer is decided, 1 when one is `?` or a question is invalid, 2 when the
batch cannot be read, 3 when a budget cut it) and a new MCP tool `tq` (the 41st). In the core profile it is reached
through `run_tool` (not listed in the menu, not named in the server instructions); `--profile full` lists it. The
run_tool catalog line of `history_search` is shorter (`{text|symbol|message|base, ...}`; its full argument list still
comes back in an `invalid_arguments` hint) and the gateway's `arguments` description lost its example, so the core
menu is 23 characters shorter than before. Re-run `verinoda setup` only if a client pins the menu text. Library
callers: `graphquery.run` takes an object query (`query=`, from `graphquery.build`), a shared context (`ctx=`) and
the asked route (`route=`); its text form and results are unchanged. `testmap.mapping_current` is the rule
`testmap.affected` used inside; nothing else changed there. Nothing to migrate: tq writes nothing.

### D159: Typed questions, batched

New command `verinoda tq` and MCP tool `tq` (the 41st tool; through `run_tool` in the core profile). The core menu's
`history_search` catalog line is shorter and run_tool's `arguments` description lost its example. Library callers:
`graphquery.run` gains `query=`, `ctx=` and `route=` (text calls unchanged), and `testmap.mapping_current` is new.
Nothing to migrate; tq writes nothing. (Also in docs/UPGRADING.md.)

### D160: Host intent for analyze

Nothing to migrate. `verinoda analyze "<question>" --intent INTENT` and MCP `analyze`'s `intent` (full profile
only; `--profile full` or `mcp.profile` "full") take one of the plan intents (`locate`, `define`, `flow`,
`callers`, `dataflow`, `config`, `tests`, `why`, `history`, `impact`, `behaviour`, `compare_reference`,
`performance`, `architecture`, `usage`, `decide`). It is used only when the rule reading of the question agrees;
the result then has `intent_check` with both readings. Not with `--plan` / `plan_json`. The core profile's
`analyze` is unchanged, and the MCP tool count stays 40.

### D161: Real-world benchmark on pinned popular repositories

This adds no change to Verinoda itself. There are new files under `benchmarks/realworld/`, new
results under `benchmarks/results/realworld-<date>/` and a new test module. Three things to know
before running:

- **Clone folder.** By default, running it clones into `C:/vbench`, which took 30 MB for the two
  small repositories (clones and their `.verinoda/`) and a few hundred MB for all ten.
- **Network.** Cloning needs the network. The tests do not.
- **Python.** The runner needs Python 3.11 or later (`python -P`) and refuses an older one.

### D162: Measured frequencies for typed answers

`verinoda tq` answers may carry `measured: k/n held-out @<gold sha8>` (JSON key `measured`, text part after
`via`) when the answer's cell of (type, answer, status) was measured on the frozen held-out gold sets with at least
30 answers, the question was asked with options such a held-out answer used (never `scope=lib`), verify is on,
and the installed code is the code that was measured (`tq.py`, `index.py`, every package module they import and
`project_index/`). It is a frequency on that set, not a probability,
and it changes no answer or status; any edit to the engine files hides it until `verinoda benchmark tq-audit` is
run again. New: `verinoda benchmark tq-audit [--work DIR] [--table PATH] [--report-dir DIR] [--no-write]
[--json]` (source checkout only), the packaged table `verinoda/data/tq_calibration.json`, the held-out set
`benchmarks/tq_gold2/` and the report `benchmarks/results/tq-audit-2026-10-02/`. No MCP tool, menu or
instructions change (the tool count stays 41).

### D163: Broader language coverage from upstream Graphify

Nothing to migrate. The extractor files changed, so the extraction stamp changed and the first `verinoda update`
after upgrading rebuilds the graph by itself. COBOL files (`.cbl .cob .cobol .cpy`) then enter the graph with no
extra. For VB.NET, R, Erlang and Solidity, install the grammar: `pip install "verinoda[languages]"` (or
`[vbnet]`, `[r]`, `[erlang]`, `[solidity]`; with uv: `uv tool install --with "tree-sitter-solidity==1.2.13" ...`),
then run `verinoda update`. Installing a grammar changes the stamp, so that update reads the skipped files.

`scan` and `update` results have a new key, `not_extracted`: a list of groups, each
`{language, grammar, count, files (first 5), reason, install}`. The CLI prints each group as a `warning:` line.
Projects with `.sql`, `.tf`, `.ml`, `.lisp`, `.dm` or `.robot` files and without those grammars now see this
warning; the files were left out before as well. OCaml classes, `.cshtml` `@functions` methods and the
redaction of more Terraform secret values change those languages' graphs on the rebuild. The query filter
accepts `lang:cobol`, `lang:erlang`, `lang:r`, `lang:solidity` and `lang:vbnet`.

### Several projects from one server (backlog 1.5)

Nothing to migrate; a single-project server (`mcp serve`, `--repo`, `--repo-of`) lists the same core menu as
before. Two new MCP tools, `list_projects` and `index_status`: 43 tools. A single-project server lists them only
in the full profile; a core server over several projects reaches them through `run_tool`. New: `mcp serve
--projects A,B` / `--all-projects`, `--max-loaded N`, `--transport http` with `--host` / `--port`, `verinoda mcp
daemon start|status|stop`, `verinoda mcp token [--rotate]` and `verinoda projects add|list|remove`. The user config
folder gains `projects.json`, `mcp-token`, `mcp-daemon.json` and `mcp-daemon.log`. In a server over several
projects every tool takes `project`, and a client config that registered one server per project can be replaced
by one entry with `--projects`. The HTTP transport binds 127.0.0.1:8765 by default and every request needs
`Authorization: Bearer <token>` from `verinoda mcp token`. `AtlasTools` takes an optional `lock=` and gains
`drop_caches()`, `graph_loaded`, `list_projects()` and `index_status()`. `build_server` takes `hub=`.

### D164: Several projects from one MCP server, HTTP transport and daemon

Nothing to migrate. A single-project server (`mcp serve`, `--repo`, `--repo-of`) lists the same core menu as before.

- **New tools.** Two new MCP tools, `list_projects` and `index_status`, bring the count to 43. A single-project
  server lists them only in the full profile; a core server over several projects reaches them through
  `run_tool`.
- **New options.** `mcp serve --projects A,B`, `--all-projects`, `--max-loaded N`, and `--transport http` with
  `--host` / `--port`.
- **New commands.** `verinoda mcp daemon start|status|stop`, `verinoda mcp token [--rotate]` and
  `verinoda projects add|list|remove`.
- **New files.** The user config folder gains `projects.json`, `mcp-token`, `mcp-daemon.json` and
  `mcp-daemon.log`.
- **Client configs.** A config that registered one server per project can be replaced by one entry with
  `--projects`. In that server, pass `project` in tool calls.
- **Library API.** `AtlasTools(..., lock=)`, plus `drop_caches()`, `graph_loaded`, `list_projects()`,
  `index_status()` and `project_entry()`; `build_server(None, hub=ProjectHub(...))`; `listed_of()` and
  `index_state()` in `verinoda.mcp.server`. `serve()` takes `projects=`, `max_loaded=`, `transport=`, `host=`,
  `port=` and `state_file=`.

### D165: Definition lines: overload implementations and annotated declarations

- The first `update` after upgrading rebuilds the graph: the extraction stamp and
  `_AST_CACHE_SCHEMA` 9 both change.
- Java, Kotlin and C# methods and classes with an annotation or attribute above their name are then
  cited one or more lines lower, at the name. Their spans do not change.
- Python functions with `@overload` stubs are cited at the implementation, with the stubs' lines
  in `metadata.overloads`.
- Claims citing the old line still resolve: the span covers the annotation, and review and anchor
  lookups accept the first line too.
- Tree-sitter anchor facts are computed again once (new cache key `ts1.def2`). Anchors already
  stored keep their scheme and stay valid.

## Upgrading from 0.3.2 (D60-D136)

### D63: Running a project's own tests safely

Add under a new heading "Upgrading to the 2026-09-28 code (D63)":

- **Your projects are not trusted until you say so.** `analyze --run-tests`, `review --run-tests`,
  `observe`, `experiment run`, `verify --run`, `probe` and the debug ledger - and the MCP tools behind them,
  including core `change_review(run_tests/observe)` - now refuse to run an untrusted project's tests with
  process isolation (the result says `refused ... the project is not trusted` with the next step). Run
  `verinoda trust <path>` once per project whose code you trust (or `verinoda trust <folder> --subfolders` for a
  folder of your own projects); it asks you to confirm, and in a script without a terminal needs `--yes` (so
  does Git Bash/mintty on Windows, where Python sees no terminal). Do not let an agent run it for you. With docker or podman installed, an untrusted project's tests run in a
  container instead. `verinoda trust --list` shows the list (a folder that no longer exists is marked
  `missing`), `--remove` takes one off. The record lives in `%APPDATA%\verinoda\trust.json` (Windows) or
  `~/.config/verinoda/trust.json`; `VERINODA_CONFIG_DIR` (an absolute path; a relative one is ignored) moves it.
- **Settings that moved.** `experiments.*` (allowlist, container image, default timeout), `mcp.profile` and
  `research.network` in a project's `.verinoda/config.json` apply only when the project is trusted. Otherwise
  they are ignored and the experiment result (and `verinoda mcp serve` on stderr) says so. To keep one for an
  untrusted project, put it in the user-level `config.json` next to `trust.json` (it applies to every project),
  or trust the project. `"mcp": {"profile": "full"}` in the project's file (README, `mcp serve` row) therefore
  needs a trusted project, `--profile full`, or the user-level config. In a trusted project the project's file
  still wins over the user-level one, and `verinoda init` writes every default into it (`"mcp": {"profile":
  "core"}`, `"research": {"network": "cache"}`, the experiment settings): delete a key from the project's file
  to use the user-level value.
- **An interpreter given by path** (`experiment run -- C:/.../python.exe -m pytest`) must be one this system
  knows (Verinoda's own, the registry, PATH, a Python manager's folder), a virtual environment made from one
  outside the project, or the trusted project's own `.venv`. Other runners (`pytest`, `npm`, `go`, `cargo`,
  `node`) must be given as bare names; they are looked up on PATH but never in the current directory, and a name
  not on PATH is an error.
- **pytest runs** get `-p no:cacheprovider` and a `--basetemp` in the throw-away folder added (`--lf` and friends
  keep working, with an empty cache). Refused under process isolation: `@file` arguments, `-p NAME` (except
  `-p no:NAME`; after a `--` too), `-o addopts=...` (also as `-qoaddopts=...`), a path that names an environment
  variable (`--junitxml=%TEMP%/x.xml`), and an `addopts` or path setting (`cache_dir`, `log_file`, `pythonpath`,
  `testpaths`) in the project's pytest config files that points outside the project: the refusal names the file
  and the setting. A `-p` in the `addopts` of the project's own config (`addopts = -p pytester`) runs; one in a
  file named with `-c` that pytest would not find itself is refused.
- **Symbolic links and junctions** in the working tree are not copied into the throw-away copy (listed under
  `source.skipped`); a test that reads a fixture through a link sees it missing.
- **Containers** run with `--read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges` as your
  user (`--userns=keep-id` for podman). A test that writes outside `/work` and `/tmp` fails there. Set
  `experiments.container_image` in the user-level config to an image with pytest and the project's
  dependencies; the default `python:3.12-slim` has no pytest and gives `inconclusive`.
- Scripts that call `experiments.policy()` directly: an absolute interpreter path that does not exist or is not
  known is now `risky` (pass `repo=` for a trusted project's `.venv`, `plugins=` for your own `-p` modules).

### D64: Ranked unknowns

- `verinoda check --json` and MCP `code_check` list fewer unknown sites by default. Each unknown Python site
  has `rank` (`high` / `medium` / `low`) and `rank_why`; LOW sites are no longer in `sites` unless `--all`
  (`include_exists=true`) is given - `summary.unknown` still counts them, and the new `unknown_summary` gives the
  counts per rank and the LOW sites per cause. Sites are ordered absent, HIGH unknown, not installed, MEDIUM
  unknown, guarded, LOW unknown (then path and line), no longer by verdict alone. A script that read every unknown
  from `sites` should pass `--all`.
- MCP `code_check`: `sites` is a list of one-line strings (`VERDICT path:line:col kind expr | ...`), no longer of
  objects, and `files` lists only the files that could not be read (`summary.files` counts all). The CLI's `--json`
  keeps objects.
- Text output labels unknowns `unknown HIGH` / `unknown MEDIUM` / `unknown LOW` and ends with a line of rank
  counts and the largest LOW causes.
- More sites are `exists` than before (a receiver's declared type is read where jedi's goto found nothing), an
  import inside `with pytest.raises(ImportError)` is `guarded`, and a module listed by its exact name in a
  requirements file anywhere in the project is `not_installed` with `optional: true` instead of `absent`. An
  absent name that is the only statement of a `with raises(AttributeError / TypeError / KeyError)` block is
  `unknown` (MEDIUM, `expected_error`).
- MCP `code_check` lines also carry `guard: ...`, `swallowed by ...`, `optional dependency` and `elsewhere: ...`.
- Cached check answers are recomputed once (`CHECK_VERSION` 6). The first check in an environment builds a word
  index of its sources (about 10-40 s for 16,000 files) and keeps it in the user cache
  (`%LOCALAPPDATA%/verinoda/Cache/names/`, `~/Library/Caches/verinoda/names/`, `~/.cache/verinoda/names/`, or
  `$VERINODA_CACHE_DIR/names/`; derived, safe to delete); `VERINODA_NAME_INDEX_BUDGET_S` (default 120) bounds the
  time one check spends on it, and the next check continues a build that was cut short. A
  `.verinoda/cache/check/names-*.json` left by an earlier build of this change is no longer read and can be
  deleted.
- `docs/ARCHITECTURE.md` gained one row for `codecheck_rank.py` (tests/test_docs.py requires every module there).

### D65: Fewer false calls, tests of more ecosystems

- The graph changes on the next `verinoda update` (the extraction stamp and the AST cache schema changed, so the
  whole graph is rebuilt once, unchanged files included): fewer calls edges in Go, Rust, PHP, Ruby, JS/TS and
  Python (`super()`) code - a callers or trace answer that listed a same-named method of another object no
  longer does, and some true same-file calls through an untyped local are no longer in the graph (analyze says
  `unknown` for them instead of a wrong caller). No call from vendored (`vendor/` at the root or beside its manifest, `third_party/`, `_vendor/` ...) or
  generated files is in the graph any more (their definitions stay, marked `vendored`, so calls into them
  still bind); minified files keep only their file node. Search still reads their text. To keep everything, set
  `"index": {"vendored": true}` in `.verinoda/config.json`; the next `verinoda update` rebuilds the graph (use
  `verinoda scan . --force` if the update refuses a graph that shrinks).
- Test files: .NET test projects (`UnitTests/`, `Foo.Tests/`, `Tests/`), Xcode test targets at the root
  (`MyAppTests/`), XCTest `FooTests.swift`, GoogleTest `x_test.cc`, Dart and Elixir `x_test.*` now count as
  tests: they rank lower in search, and the tests view, impact's `tests_to_run` and the change review list them
  as tests.
- `.hh .hxx .ipp .inl .tpp` files enter the graph as C++ on the next `verinoda update`.

### D66: Faster scans

- Nothing to run. `verinoda scan` / `update` list files through `git ls-files` in a git repository whose root
  has a `.gitignore`, which applies git's own .gitignore semantics. On most trees the files indexed are the
  same; where Verinoda's matcher read a rule differently from git, git's reading now counts: a `*` does
  not cross `/` (`test*.py` no longer drops `tests/foo.py`), case follows `core.ignorecase`, and a file
  renamed only in case without `git mv` keeps git's spelling on a case-folding file system. Such a file
  is added by the next scan and never evicted by a later rebuild that has to walk. A file ignored only by
  the global git excludes file (`core.excludesFile`) is indexed as before (detect never read that file).
  An ignore file that is not UTF-8 (UTF-16 from Windows PowerShell 5.1, an ANSI code page) keeps the walk
  and its decoding. The detect() result has a new key
  `enumeration` (`"git"` or `"walk"`), and on the git path its `ignored` list is git's report of what
  .gitignore dropped.
- A full search-index build writes `.verinoda/index/search.db.build-<pid>` and renames it to `search.db`
  when complete; a build that fails keeps the previous index (it used to be deleted first). A leftover
  build file from a killed build is removed by the next full build after an hour, or can be deleted by hand.

### D67: Issue-shaped questions

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

### D68: Verinoda's own files are not the project's

### D68: Verinoda's own files are not indexed

- Nothing to run. The first `verinoda update` (or `setup`) after upgrading rebuilds the graph once: the
  AST cache schema is 8 (an MCP config's extraction no longer includes Verinoda's own server entry), and the
  skill files leave the index. After it, a file carrying the `verinoda-managed` marker under
  `.claude/skills/verinoda/` or `.agents/skills/verinoda/` (at any depth, so a nested project's skill too)
  or listed by the install manifest as Verinoda's is in no snapshot, graph, search index, lexicon, freshness
  report or debug-ledger tree. A file without the marker there (your own `SKILL.md`, a `reference.md` next
  to Verinoda's) stays indexed. `.mcp.json` stays indexed without Verinoda's `verinoda` entry.
- `verinoda setup` installs the agent files before it indexes. Running it again, from the same or another
  Verinoda install, no longer rebuilds the graph when only those files changed; the setup report's `index`
  has `graph`: `"full"` or `"none"`.
- A scan/update result may carry `own_files_dropped` (graph.json had nodes from Verinoda's own files and they
  were dropped), and `build_stats.json` has `configs` (the MCP-config digests of the last graph build).
- An index copied from another folder may keep the pre-D68 `verinoda` server node of `.mcp.json`; delete
  `.verinoda/index` and run `verinoda scan` to clear it (derived files are disposable).

### D69: Datapack functions called from Java

Nothing to run: `verinoda datapack` reads the Java at query time, the index is unchanged. `datapack function`
now lists Java callers after the mcfunction ones, and its JSON changed: each `called_by` row has `kind`
(`mcfunction` or `java`; a Java row has `via`, `helper`, `caller`, `at`, `how`, `target`, `text` and, when they
apply, `tree` and `test`), a new `dynamic` list holds the names Java builds at run time that may be the
function (also in a `not_found` answer), and `dynamic_any` the sites that build all of the name past the
namespace. The summary JSON has `java_calls` and `java`, and a `missing_functions` row from Java has `how: "java
<via>"`, `caller` and `helper` / `tree` when they apply. A helper whose body Verinoda cannot read can be named in
`.verinoda/config.json`: `{"datapack": {"function_helpers": ["Class.method:argIndex:namespace"]}}`.

### D70: Entity tags added through a constant, a conditional, the live set or a built name

Nothing to run: `verinoda datapack` reads the Java at query time. "Tags checked but never added" gets shorter where
Java adds a tag through its class's own constant (a constant name several classes declare is no longer dropped),
`c ? A : B`, or `entityTags().add(...)`; `datapack tag NAME` lists those adds. A row whose tag a name built at run
time may add stays, with `(maybe added by File.java:N: *_at)`; JSON: the row has `maybe_added_by` (`[{at,
pattern}]`), the summary's `problems` has `tags_added_dynamically`, a `datapack tag` answer (found or not) has
`maybe_added_by`, and the text summary has a line "tag names Java builds at run time (N)". The summary's `note`
text changed. A script that counted the rows of the list sees fewer.

### D71: Java calls into datapack functions in the graph

Nothing to run: the receiver sidecar (`.verinoda/index/receiver_calls.json`, version 9) is rebuilt once on the next
load and then holds the `datapack` edges; `when`, `trace`, impact, `analyze` and `node_inspect` follow a Java
method's calls into a datapack function. A function id with a folder (`ns:dir/name`) now resolves to the function
instead of `not_found`. `verinoda datapack` itself is unchanged.

### D72: Translation keys in Minecraft lang files

New command `verinoda lang`: translation keys missing from a locale or only in it, written twice, placeholders
that differ from the default locale, keys the code asks for that no lang file defines, keys nothing names. Each
finding cites both files. Exit 3 when something is found, 2 when nothing could be compared (no lang file, or no
file of the `--default` locale). Nothing else changes.

### D73: Extract the definition around a location

- New command `verinoda extract <path:LINE|path#Symbol ...> [--from FILE|-]`: the whole function or class around a
  location, or around each location of a compiler's, linter's or test run's output. Reads the file as it is now;
  needs no index; nothing changes on disk. `--json` gives each definition as a claim with its lines as evidence.
  Exit 2 when a location is not found. No change to the store, the index or the MCP tools.

### D74: Dependency cycles and a minimal break set

`verinoda map` (all views) and `map --json` now include a `cycles` view; programs that iterate the views of the
all-views map see one more key. `map --view cycles` and the MCP `map_view {view: "cycles"}` are new. Nothing else
changes; the index is not rebuilt.

### D75: Graph exports: GraphML, Cypher, Obsidian and SVG

`verinoda export [--format graphml|cypher|obsidian|svg] [--out PATH] [--json]` is new. It writes
`.verinoda/index/export/` by default, which is derived and disposable like the rest of `index/`.
`verinoda index export ...` is unchanged. Use the new command when direction, parallel edges and each
edge's location matter.

### D76: Commit, diff and revision search

- New command `verinoda history`: `history text "<text>" [--regex] [--path P]` (the commit that first added a
  text and, when HEAD has none, the one that last removed it, each a claim with the commit as evidence),
  `history commits [--message RE] [--author RE] [--path P] [--since D] [--until D] [--diff RE] [--limit N]`,
  `history compare BASE [HEAD] [--path P]`. Read only; exit 2 when nothing is found.
- `history_search` is new (one more MCP tool; `dependency_ask` of D105 is the latest), served by the core profile
  behind `run_tool` and by the full profile. Reinstalled skills allow `verinoda history` and mention it.

### D77: Mermaid diagrams and a wiki outline

- New commands `verinoda diagram` and `verinoda wiki`; nothing existing changed behaviour.
- MCP `map_view` accepts the view `outline` (`targets` = page ids for their diagrams).
- `verinoda ui --export` files now carry a `wiki` field and are larger by the size of the diagrams; the
  export's summary line names the pages and diagrams.
- An optional `.verinoda-wiki.json` at the repository root steers the outline; commit it with the code.

### D78: Agent instruction file lint

- New command `verinoda agent-lint`: AGENTS.md, CLAUDE.md, GEMINI.md, Copilot/Cursor/Windsurf/Cline rules and
  Claude Code's memory checked against the tree (paths, scripts, targets, modules, packages, extras, tools, and
  whether the files agree). Read-only, no index needed, no schema change. Exit 3 when something is wrong: a CI
  step that runs it fails on a stale instruction file.
- If the MCP tool below is added: the server has one more tool (full profile only); update the tool counts.

### D79: Filter syntax in query

`verinoda query` and MCP `project_query` read filters in the question: `path:GLOB`, `lang:NAME`,
`symbol:NAME`, `is:vendored|generated|minified|test`, `/regex/`, and `AND` / `OR` / `NOT` / `-` / parentheses.
Filters narrow the ranked results and add no score. A question of filters only lists every matching unit. A
question with no `key:value` filter and no `/regex/` token ranks exactly as before (`/word/`, one path
segment between slashes, and an `is:` value that is not a filter are words). JSON results with filters
carry a new `filters` block. An unreadable filter is an error: exit code 1 on the CLI, `invalid_argument` on
MCP. No re-index is needed.

### D80: Secret scrubbing

- Experiment logs, agent-reported outputs and logs copied by `trace-log` are now stored with secrets and e-mail
  addresses replaced by `<redacted:RULE>` markers (line numbers unchanged); the evidence excerpts and summaries made
  from them are redacted too. An experiment's evidence `content_hash` (and an agent-reported run's
  `output_sha256` when something was redacted) is now the hash of the kept, redacted text.
- `verinoda ui --export` also replaces secrets and e-mail addresses in the embedded text.
- New command `verinoda secret-scan [FILE ...] [--fix] [--json]`. Logs stored by an earlier version may still hold
  secrets: run `verinoda secret-scan` and, if it reports findings, `verinoda secret-scan --fix`.

### D81: Rename preview

New command `verinoda rename-preview <symbol> <new_name> [--max-sites N] [--json]`: every line a rename
would touch, each with its status, why and the line, plus mentions, other symbols of the same name and
conflicts. It edits and records nothing. Exit 2 when the symbol does not resolve or the new name is
invalid, 3 on a conflict. No schema or index change. No MCP tool yet (see above).

### D82: Declared vs used dependencies

`verinoda check --deps` (and MCP `code_check` with `deps: true`) compares the declared dependencies of
pyproject/requirements, package.json and Gradle/Maven builds with the imports of the project's files and
reports packages that are missing, only transitively installed, unused, or in the wrong group (dev vs
runtime), each with `file:line` evidence and a status. Exit 3 when something is found, 4 when no manifest
was read. The Python manifest reader now reads requirements whose names start with `http` (httpx,
httpcore); before, they were skipped, so a dependency guard asking for httpx absent or present may change
its verdict.

### D83: Complexity, code health and clones

`verinoda review` has a seventh concern, `health`, run by default: reviews of changes that make a function
more complex, or that add a near-duplicate, now carry `strong_inference` findings under
`concerns.health`. `--concerns` without `health` restores the previous output. New command `verinoda health`.
No schema change; no new MCP tool.

### D84: Dead code

`verinoda map --view dead` and MCP `map_view {view: "dead"}` are new; no existing view, output field or
default changes. Nothing is stored; the index format is unchanged.

### D85: MCP prompts

`verinoda mcp serve` now also answers `prompts/list` and `prompts/get` with four ready workflows (review,
onboarding, debug, pre_merge); `verinoda mcp prompts` prints them. The tool count (38) and the core menu
are unchanged.

### D86: Breaking vs compatible API change

`review --json` and MCP `change_review` have a new key `api_changes` (a list of verdicts) and `counts` a new key
`api_breaking` (and `api_changes_total` when the list was cut); the review summary gains a "Public API" sentence when there are public changes. Exit codes are
unchanged. No tool was added.

### D87: Decisions a diff touches

`verinoda review` (and MCP `change_review`) now returns `decisions`: the decision records the change
reaches, each with `reached_by` entries (`kind`, `entry`, `at`, `why`, `status`, `evidence_at`), and prints
them under "Decisions to read". A record the change deletes is listed with `status: deleted`; a record that
could not be read adds `error` and an `unknown` entry of kind `decision_records`. Exit codes and the MCP
tool count are unchanged.

### D88: Missing mod dependencies and pack collisions

`verinoda datapack` prints two new sections, "resource collisions across packs and mods" and "mod
dependencies not met", plus one line for dependencies the repository alone cannot check. `--json` has
`problems.pack_collisions`, `problems.mod_dependencies` and, when there are any,
`problems.dependencies_not_checked`, `problems.dependencies_unchecked` and `problems.pack_copies`, along with
`packs` (the number of sources), `with` and `unreadable`. New:
`datapack packs` and `--with PATH`. A repository with a collision but no datapack functions now gets the summary
and not `no_datapack` (exit 0 and not 2). `--with` is refused on `datapack tag|score|function`. No new MCP tool. No
rescan is needed.

### D89: Coverage import

- New command `verinoda coverage`; `review` has `--coverage REPORT` and, when a report lies at a usual path
  (`coverage.xml`, `lcov.info`, `coverage/lcov.info`, `build/reports/jacoco/test/jacocoTestReport.xml`,
  `target/site/jacoco/jacoco.xml`, ...), a `tests.coverage` section. A review that exited 0 can now exit 3 when
  a fresh report shows changed lines no test ran. `counts.uncovered_changed_lines` is a new key.
- The MCP tool count does not change (no new tool; `change_review` reads the reports it finds).

### D90: Ownership and knowledge map

New command `verinoda owners [TARGET]`: who knows a file, folder, line range or symbol, from CODEOWNERS and git
blame (main author, bus factor, knowledge loss), as claims. Read only; nothing to migrate. The MCP tool count is
unchanged (no new tool). Re-run `verinoda install` (or `setup`) to get
the skills that name it.

### D91: Hotspots

`verinoda map --view hotspots` (and MCP `map_view` with `view: hotspots`) ranks code files and functions by
changes x cyclomatic complexity. `verinoda review` orders `read_first` within each kind by the same score and adds
`hotspot` to each range it could score; the kinds keep their order. No MCP tool was added.

### D92: Temporal coupling

`map --view impact` (and the MCP `map_view` impact) now also lists `history_coupled`: files that changed together
with the change in the last 1,000 commits and that no graph edge links to it, each a `strong_inference` claim with
its commit count and the shared commits as evidence; `history_coupling` says how many commits were read. No new
command or MCP tool.

### D93: Installed-version library docs

`verinoda api NAME --docs` (MCP `api_members` with `docs=true`) also quotes the definition's docstring and the
section of the installed distribution's README that names it, each with `path:start-end` and the installed
version, read from the installed files. Answers without the option are unchanged. The MCP tool count is
unchanged.

### D94: Commit rationale per symbol

New: `verinoda history symbol NAME|path:A-B` (MCP `history_search` with `symbol`) lists the commits that changed
a symbol's lines, each quoting its message. `analyze`'s why-answers now quote the commit body in the evidence and
map working-tree lines to HEAD's before asking git; their claim text is unchanged. The MCP tool count is unchanged.

### D95: Architecture rules as code

`decide record --guard` and `decide guard` accept three new guard kinds for architecture rules (D95):
`layers order=src/ui/**,src/core/**,src/db/**` (top first; no edge from a lower layer up), `allow_edges
from=GLOB allowed=GLOB,...` (those files use only each other and the allowed ones) and `public module=GLOB
api=GLOB,...` (outside code reaches the module only through its api files); the last two leave test code
out unless `scope=all`. `decide check` reports each
violating edge at its cited line and fails with exit 1, as for `no_edge`. Any glob of an edge guard may be
`tag:NAME` for a set of globs defined under `[architecture.tags]` in `verinoda.toml` (or
`[tool.verinoda.architecture.tags]` in `pyproject.toml`). Records written before are unchanged; a record
using the new kinds is not enforced by an older Verinoda (the kind is reported as unknown). The MCP tool
count does not change (no new tool).

### D96: Differential findings

`verinoda review` / MCP `change_review` now list under `concerns` only the findings the change introduced; findings
the base version's changed code already had are counted and listed under `differential.preexisting_findings`, and
findings the change removed under `differential.fixed_findings`. Every finding has a `delta` field. Pass
`--findings all` (MCP `findings: "all"`) for the previous list, with labels. The exit code follows what is listed:
a change whose findings were all there before now exits 0. `counts` has two new keys, `preexisting` and `fixed`; `op-on-changed-line` findings carry `op_kind` and
`io-in-loop` findings `base_had: loop` when the loop did that IO before.
The MCP tool count does not change (no new tool).

### D97: What a merged change made stale

`verinoda review` (and `change_review`) now has a `made_stale` key: the stored claims, notes and decision
records the change makes stale, each with a status, the changed line and what it cites, plus a "Made stale by
the change" section in the text. Nothing to do; no new command, no MCP tool count change, no store schema
change.

### D98: Decision record lifecycle

`verinoda decide supersede OLD --by NEW` supersedes one existing record by another and updates both;
`verinoda decide link ADR-N amends ADR-M` links two records and writes the reverse link on the other one;
`verinoda decide toc` prints a table of contents with a Mermaid graph of the records (`--write
docs/decisions/README.md` keeps it in a file). `verinoda ui` shows the records on a timeline (`#/d`). Records
written from now on carry a `links:` line in their front matter; an older Verinoda reads such a record but
drops the line when it rewrites the record. A record whose `supersedes` or `superseded-by` is not answered by
the other record now shows a warning in `decide list` (nothing changes in what is enforced). MCP
`decision_record` has two more actions (`supersede`, `link`) and one more argument (`link`); the tool count is
unchanged.

### D99: Butterfly view

`verinoda butterfly NAME` prints a symbol's callers and callees, or a class's inheritance tree, with the
line of each link; in `verinoda ui` a *Butterfly* button on a function, method or class note shows the
same around the note. No re-scan needed. The MCP tool count is unchanged.

### D100: Access Widener and Access Transformer

New command `verinoda access-check [FILE ...] [--json]`: every access widener (`.accesswidener`,
`.classtweaker`) and access transformer (`accesstransformer.cfg`, `*_at.cfg`, the files the mod manifests name)
entry checked against the class files of the build's classpath (`code_check.classpath` or the Loom build's),
each with its `path:line`: `exists`, `absent` with the nearest real names, `malformed` or `unknown`. Exit 3 when
an entry is absent or malformed, 4 when one is unknown, 2 when there is no such file. No MCP change; the tool
count stays the same.

### D101: Undeclared symbols and naming rules

`verinoda datapack` lists objectives (and, when there are any, teams and boss bars) used but never declared, and
takes `team NAME` / `bossbar ID` lookups. Set `datapack.naming` in `.verinoda/config.json` to check names against
your conventions (off by default). In `--json`, an `objectives remove` site now has kind `remove` (it was
`define`). On a macro line, a name a macro fills in part (`a_$(x)`) is no longer read as its spelled part:
`$tag @s add a_$(x)` is a tag a macro fills in (it was the tag `a_`), and `$function ns:do_$(x)` is no longer a
call to the missing function `ns:do_`. No MCP tool was added.

### D102: Client and server separation

`verinoda map --view sides` (and MCP `map_view` with `view: sides`, through `run_tool`) lists client-only code
(the `src/client` source set, `@Environment(EnvType.CLIENT)` / `@OnlyIn(Dist.CLIENT)` classes and methods,
`net.minecraft.client` classes) that the mod's server-side entry points reach, each path a `strong_inference`
claim at most (`weak_inference` through an inferred edge) with every hop at `file:line`. Plain `verinoda map` is unchanged. No new MCP tool.

### D103: Violation baseline and ratchet

New: `verinoda decide baseline [--record --said "..." [--replace] | --shrink]` keeps a committed list of known
violations (`baseline.json` in the decisions folder). With a baseline, `decide check` fails only on violations it
does not list; listed ones are `baselined`, fixed entries `baseline_fixed`. Without a `baseline.json` nothing
changes. The MCP tool count is unchanged.

### D104: Suggested reviewers and related changes

`review` (and MCP `change_review`) has a new `reviewers` key: suggested reviewers from `git blame` of the base
lines the change modifies (the change's own author left out), the CODEOWNERS owners of the changed files, and the
earlier commits that changed the same definitions with their messages. Nothing else in the output changes. The
MCP tool count is unchanged.

### D105: Ask before writing a dependency

- New: `verinoda decide ask SOURCE TARGET` and the MCP tool `dependency_ask` (core, behind `run_tool` in a
  project with decision records; listed in the full profile). The MCP server now has a 39th tool.
- The core instructions of a project with decision records now name `dependency_ask`.

### D106: What-if refactoring

New: `verinoda what-if --move OLD=NEW` simulates moving or renaming files and folders (nothing is edited) and
reports the edge-guard findings and the dependency cycles the move would add or remove; exit 3 when it adds some.
The cycles view's output is unchanged. The MCP tool count is unchanged.

### D107: Project brief

New command `verinoda brief [--max-chars N] [--json]`: a project brief (name, runtime, build/test/check
commands, CI commands, layout, conventions) read from the files on every call, each line with `file:line`, under
a character budget (default 2,000). It is not the decision brief: `verinoda decide brief` is unchanged. No MCP change.

### D108: Docs coupled to code, drift check and trivial auto-fix

New: `verinoda docs check [PATHS] [--fix] [--exclude GLOB]` checks the paths and line references in the
repository's documents against the working tree (exit 1 when one is broken, renamed, moved or changed); `--fix`
repairs renamed paths and moved line numbers. The MCP tool count is unchanged.

### D109: Git hooks for re-indexing

New: `verinoda hooks install` (and `setup --hooks`) adds post-commit, post-checkout, post-merge and post-rewrite
hooks that run `verinoda update` in the background (only in the project's own work tree); `verinoda hooks uninstall` removes exactly what it added. Nothing
changes unless you run it. The MCP tool count is unchanged.

### D110: Ranked repo map under a token budget

`verinoda map --view repo [--target FILE ...] [--max-tokens N]` prints a repo map: the files to read first,
ranked by PageRank over the file dependency graph toward the files in play (the targets, else the git
changes), and each class, function and method definition line at `file:line`, until the estimated tokens
reach N (default 1024). `--json` has the ranks (`strong_inference`) and the counts. The MCP `map_view`
takes `view: "repo"`, with `targets` as the files in play. The MCP tool count is unchanged.

### D111: Undocumented decisions

`verinoda decide undocumented` lists structural choices no decision record covers - storage that goes through
one file, a Python library imported by one product file, the environment read in one file - each a
`weak_inference` candidate over a verified fact with `path:line` evidence, with the guard that would keep it and
the `decide record` command. `verinoda decide dismiss CANDIDATE --reason "..."` records the user's dismissal in
`.verinoda/dismissed_decisions.json` (local; `--undo` lists it again). No MCP tool was added.

### D112: Change risk score

`review` (CLI and `--json`) and MCP `change_review` gain a `risk` key: a heuristic score out of 100 with every
part listed (`value`, `weight`, `cap`, `points`, locations) and the inputs not measured named. The summary ends
with one sentence giving the score. No tool, argument or exit code changes.

### D113: Session dedup

`project_query` (MCP, text format) no longer repeats passages it already printed in the same server session; it
lists them by location instead. Set `VERINODA_QUERY_DEDUP=0` to turn this off. JSON answers and the CLI are
unchanged. The MCP tool count is unchanged.

### D114: Incremental re-review

New: `verinoda review --since-last` (MCP `change_review` with `since_last=true`) leaves out the findings the last
review of the same base and mode already listed, counts them and lists the ones gone. Without it, `review` is
unchanged. The MCP tool count is unchanged.

### D115: Glob-scoped context

New: `verinoda context FILE` lists what the project says about a file (decision records whose guards name it,
notes on it or on a matching glob, Cursor and Kiro rules); MCP `read_context` returns the same as a PostToolUse
hook output, and the hooks template has a Read/Edit entry for it. The MCP server has one more tool (40 then).

### D116: SARIF in and out, CI check status

New: `verinoda sarif FILE...` reads a linter's or CodeQL's SARIF as evidence (the tool's statements, at most
`strong_inference`), and `review`, `check` and `decide check` take `--sarif` to print their findings as a SARIF
2.1.0 log for GitHub code scanning (`github/codeql-action/upload-sarif`); exit codes are unchanged. No MCP change:
the tool count stays the same.

### D117: Flaky test history

Schema v7: `atlas.db` gains `test_runs` and `test_quarantine` (migrated on first open; an older Verinoda refuses
the database as written by a newer one). New `verinoda debug flaky` and `verinoda debug quarantine`; every
debug-ledger run Verinoda makes now keeps each test's outcome, and runs recorded before the upgrade are added
the first time `debug flaky` reads the history. `debug rerun --json` gains `tests_both_outcomes` when a series
saw a test both pass and fail. No MCP tool added; the tool count is unchanged.

### D118: Named flow maps

New: `verinoda map save NAME --trace A B` (or `--view V`) keeps a trace or map views in
`.verinoda/maps/NAME.json`; `verinoda map show NAME` reads it back as `current`, `stale` (exit 1, the changed
files named) or `unknown` (exit 1, it cites no file) and `map list` lists them; `.verinoda` is git-ignored, so
share a map with `git add -f .verinoda/maps/NAME.json`. MCP: `map_view` takes `view: "saved"` with `targets: [NAME]`. The MCP
tool count does not change (no new tool).

### D119: Crash diagnosis rules and suspect scoring

`verinoda trace-log` now names a known crash pattern (out of memory, the watchdog, a missing dependency or class, a
Mixin that failed to apply, the wrong Java) with its log line, and ranks the mods the stack frames point at by a
score (`strong_inference`, its inputs shown). Two new keys in `--json`: `diagnosis` and `suspects` (plus `score`,
the formula), and each rule hit a `status`. A log with only a crash pattern now exits 0. No index change; the MCP
tool count is unchanged.

### D120: Rationale nodes

New command `verinoda rationale [symbol|path]`: the WHY/NOTE/NB/HACK/IMPORTANT/RATIONALE comments and the
comments citing a decision record, each with its `file:line`, quoted, and attached to the definition it is above
or in. MCP `node_inspect` lists the ones of the node under `rationale` (at most 5). Nothing to rebuild: they are
read from the files when asked. The MCP tool count is unchanged.

### D121: Structural (AST pattern) search

New command `verinoda grep-ast PATTERN [PATH ...] [--lang L] [--rule FILE] [--max-results N] [--json]`:
structural search with `$A` / `$$$REST` metavariables over Python, Java, TypeScript and the other tree-sitter
languages the index reads; exit 0 with matches, 1 without, 2 on a bad pattern or rule file. No MCP tool was
added.

### D122: Evidence-backed code tours

New: `verinoda tour SOURCE TARGET` writes a CodeTour `.tour` file from a trace path, pinned to the commit; `verinoda
tour --check FILE [--fix]` re-anchors its steps after the code moves. The MCP tool count is unchanged.

### D123: Pattern trends and code monitors

New: `verinoda monitor` keeps saved searches in a committed `verinoda-monitors.json` and exits 1 when one gains a
match its baseline does not have; `monitor trend ID` counts a regex monitor's matches over the history. The MCP
tool count is unchanged.

### D124: Memory event history and expiry

Schema v8 adds `memory.expires_at`; an older Verinoda refuses a database this one opened. `memory history KEY`
now prints an object, `{"key", "events", "versions"}`, where it printed the list of versions (now under
`versions`). New: `memory learn --ttl` and `memory forget`. The MCP tool count is unchanged.

### D125: Path-scoped review rules

New: `verinoda rules` checks `verinoda-rules` blocks in AGENTS.md, CLAUDE.md, BUGBOT.md and REVIEW.md (any
folder) against the lines a change added in the folders they cover, and `review` carries a `path_rules` block
when the project has such files. The MCP tool count is unchanged.

### D126: Bi-temporal claims

New: `verinoda claim asof --time WHEN | --commit REV` lists each claim's status as recorded at a moment or at a
commit. Claim history rows now carry `payload.snapshot` (the snapshot the transition was made at). The MCP tool
count is unchanged.

### D127: Typed notes and wikilinks

New: `verinoda notes --facts [--category C] [--tag T]` lists the `- [category] fact #tag` lines of your notes;
`verinoda notes --links` resolves every `[[Name]]` and exits 1 when one leads nowhere. The MCP tool count is
unchanged.

### D128: Shaderpack lint and include graph

`verinoda shader --check` now also lints the shader text (includes, brackets, `#if`/`#endif`, `#version`,
undeclared Iris/OptiFine uniforms, undefined macros), so a project whose check passed may now exit 3; the issues
carry a `status`. New `shader --includes` lists the include edges; `shader --check --glslang` also compiles each
pack stage with `glslangValidator` when installed (off by default). The MCP tool count does not change.

### D129: Bisect over the debug ledger's attempts

`verinoda debug bisect --attempts` finds the first attempt of a debug session (agent-reported steps
included) whose tree fails the repro: each recorded tree is rebuilt from the session base and the blob
store in a throw-away copy and run by Verinoda; the answer names the attempt, its hypothesis, the files it
changed and the run ids on both sides. `experiments.run` takes `replay=` for that. No tool count change.

### D130: Persistent test-to-code map and affected tests

Schema v9 (after v8, a memory's `expires_at`): `atlas.db` gains `test_map` and `test_map_tests`
(migrated on first open; an older Verinoda refuses the database as written by a newer one). Every traced run (`observe`, `analyze --observe`, `review --observe`,
the debug ledger's traced attempts) now updates the test-to-code map; `review` lists the affected tests (observed
first, static reach as the fallback, each labelled) and prints one pytest command that runs them (`review --json`:
`tests.affected`); `observe --json` gains `test_map` (tests and functions recorded); MCP `change_review` gains
the compact `affected_tests` key. No MCP tool added; the tool count is unchanged.

### D131: Property test templates

`verinoda probe` gains `--template roundtrip|idempotent|equivalence`, `--inverse` and `--test-file`; with
`--template`, `--emit-test` writes a property test file (never over an existing one) instead of printing pinning
tests, and the result has a `property_test` entry (`status`, `observed`, `evaluated`, `counterexamples`,
`claim_status`, `path`, `written`, and `hash_order_dropped` when inputs were left out). The probe plugin's spec
accepts `property_refs`; `experiments.run` accepts `PYTHONHASHSEED` in `env_extra`. No MCP tool or argument changes.

### D132: Infrastructure-as-code nodes

New command `verinoda infra [--file FILE] [--json]`: Dockerfiles, compose services, Kubernetes containers and
Terraform resources, each linked to the project file its command runs (statically_verified commands;
strong_inference links through `COPY` lines, weak_inference by name). Read-only; nothing to migrate. No MCP tool
changes.

### D133: Affected projects in a monorepo

`review --json` and the MCP `change_review` response have a new key, `affected`: in a monorepo the
workspace packages the change affects (the packages holding changed files, then those declaring a
dependency on them, each with its manifest line and status; compact in MCP), else
`{"packages_total": N, "not_checked": [...]}` (MCP: `{"packages_total": N}`). The review summary adds a
"Workspace packages affected" sentence when there are some. New command `verinoda affected`. No MCP tool
was added.

### D134: Background consolidation

New: `verinoda consolidate` re-verifies stale claims against the current tree and finds duplicate claims
(`--merge` folds them, nothing deleted); `update --consolidate` or `claims.consolidate_on_update` in
`.verinoda/config.json` runs it after each update (never with `--merge`). No MCP tool changes.

### D135: Error and trace import

`verinoda trace-log FILE` also reads a Sentry event export or an OpenTelemetry trace in OTLP JSON from a local
file: each frame is mapped onto the repository or reported as stale, ambiguous or not in the repository, and a
mapped frame is recorded as a claim scoped to its event. A file named `.json` / `.jsonl` / `.ndjson` is read as
JSON and refused if it does not parse; any other file is read as an export only when it parses and holds an event
or span, and as a log otherwise, as before.

### D136: Specs traced to code and tests

New command `verinoda spec check [--specs-dir DIR] [--json]`: requirement criteria (`- [ID] WHEN ... THE
SYSTEM SHALL ...`) in Markdown files under `.verinoda/specs/` (or `[specs] dir` in `verinoda.toml`) with
`evidence:` lines naming claim ids, test ids, `path::Name` or `path:lines`; each criterion is verified,
tested, tested_inferred, broken, unchecked or unevidenced. Exit 1 while one is broken or unevidenced, 3 when
something was not checked, 2 when the folder setting cannot be read. Nothing changes for projects without
specs.

### D60-D62

- D62: `grep_context`, the server's 37th tool then, is what an optional Claude Code Grep hook calls
  (through run_tool in the core profile). Nothing calls it unless that hook is configured
  (`verinoda/agents/templates/claude_hooks.json`); no migration.

- MCP core profile (D61): the menu lists project_query, analyze, code_check, index_update and `run_tool`.
  node_inspect, relation_trace, map_view, claim_inspect, claim_list, evidence_inspect, change_review and
  decision_check are called as `run_tool {"name": "node_inspect", "arguments": {"name": "..."}}`. A prompt or
  script that calls `mcp__verinoda__node_inspect` (or another of them) directly needs `--profile full` or
  `"mcp": {"profile": "full"}` in `.verinoda/config.json`; so do analyze's plan_json, run_tests, observe and
  budget_calls and code_check's env and include_exists, which the core menu no longer lists.

Nothing to migrate. Re-run `verinoda setup` (or `verinoda install`) for the skills' citation and code-check lines.

- Query and analyze text print passage lines with their line numbers (`19     def request_key(...)`), and
  leave blank lines out. A program that read the passage lines as source text strips the number and the
  space after it; the `## path:a-b` and `  path:x-y` locators are unchanged.
- MCP: the core profile lists `decision_check` only in a project with decision records (restart the server
  after adding the first one); `index_update` on a folder never scanned runs the first scan (not in a home
  folder, a drive root or a workspace of several projects), and the other tools' not_initialised hint names it. Tool and parameter
  descriptions and the server instructions are shorter.

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
