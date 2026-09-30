# Versioning and upgrades

Verinoda follows semantic versioning once it leaves `0.x`. While in `0.x`,
minor versions may change CLI output and MCP tool results. The SQLite schema is
always migrated forward, never silently reset.

## What changes on upgrade

| Component | Upgrade path |
|---|---|
| Package / CLI | From PyPI: `uv tool upgrade verinoda`, `pipx upgrade verinoda` or `pip install -U verinoda` (npm: `npx -y verinoda@latest`). The development version: re-run the install command from the README (`uv tool install --force --reinstall-package verinoda --link-mode copy "verinoda[precise] @ https://github.com/ozcinax-star/verinoda/archive/main.zip"`, or `install.sh`); it fetches the archive again and reinstalls in copy mode. For Codex on Windows, reinstall with `uv tool install --link-mode copy <wheel-or-path>` (see the README). |
| `.verinoda/atlas.db` | `verinoda.store` keeps `meta.schema_version`. Opening an older database applies the migrations in `_MIGRATIONS` in order (v1 → v2 → ... → v6). A database newer than the installed Verinoda is refused with an explicit error instead of being modified (`verinoda doctor` reports it as a failed check). A migration is one-way: copy `.verinoda/atlas.db` before trying a newer Verinoda if you may go back to the older one. Claims, evidence and history are never deleted by a migration. |
| `.verinoda/index/` | Derived, disposable data (see below). Since 0.3.3 the graph records which extraction built it (`build_stats.json`): after an upgrade the next `verinoda update` rebuilds the graph by itself, unchanged files included, and `verinoda doctor` says so until then (`scan <repo> --force` still works). Claims whose dependencies changed are marked `stale` by the normal snapshot comparison, not by the upgrade itself. |
| `.verinoda/config.json` | Optional. A user config is merged key by key over `paths.DEFAULT_CONFIG`, so new keys (`budget.precise_sites`, `budget.precise_seconds`, `research.network`, `understanding.*`, and since 2026-09-26 `mcp.profile` ("core") and `query.shape_budget` (false), both written by `verinoda init`) take their defaults when absent. `verinoda mcp serve` stops with the fix when the file is not JSON or its `mcp` setting cannot be read (`mcp` not an object, `profile` not a string or unknown); only a missing setting serves the core profile. |
| 0.1.0 -> 0.2.0 (2026-09-26, D41-D46) | Two new dependencies come with the upgrade: `graspologic-native` (Leiden communities in native code, so the graph's communities, the UI's regions and GRAPH_REPORT.md are grouped differently once) and `pypdf` (PDF text). Run `verinoda update` (or `scan`): PDF and Office documents enter the graph and the search index, and on Windows images with text are read once with the built-in OCR (`VERINODA_OCR=0` to skip). `verinoda check` now reads Java and Kotlin, and the imports of TypeScript/JavaScript; `verinoda update --fast` rebuilds the graph in the background; its jar tables are cached under `.verinoda/cache/jvm/` (safe to delete). |
| 0.2.0 -> 0.3.0 (2026-09-27, D47-D57) | `verinoda when SYMBOL` says when a method runs (events, delays, the conditions around each call); JVM lambdas handed to a registration or a scheduler are `registers` edges now; Mixin handlers have `injects` edges (D48) and the impact view and change review list the registered GameTests that reach a change (D49); `verinoda backlog` links backlog items and the comments that cite them (D50, `"backlog": {"files": [...]}` in config.json for another file). The receiver sidecar (version 7) is rebuilt once on the next load (no rescan needed).; the Java check flags members out of reach and constructors no argument list fits, and `verinoda api` reads Java classes (D51; the jar tables under `.verinoda/cache/jvm/` are rebuilt once).; `.mcfunction` files enter the graph on the next `verinoda update` (D52: `verinoda datapack`).; `verinoda trace-log` and `verinoda shader` are new commands (D53, D54).; the search index is rebuilt once (schema 5: a method's javadoc is its own text, D55).; Java overloads are separate symbols and calls bind by argument count (D57): run `verinoda scan .` once (an `update` re-reads only changed files); the receiver sidecar (version 8) is rebuilt on the next load. |
| 0.3.0 -> 0.3.2 (2026-09-27, D58-D59; the tag v0.3.1 published nothing) | Nothing to run: the answers change (less noise, string-keyed settings for config questions), the history view is faster, and a PyPI / npm install's `verinoda --version` names the commit it was built from. |
| Agent skills / MCP registrations | Re-run `verinoda setup` in the project (it also updates the index and installs for the agents found on PATH), or `verinoda install --agent <claude\|codex> --scope <project\|user>`. This brings the skills' "understand the question first" and "references the user gives" sections and the new allowed-tools entries (`verinoda plan *`, `verinoda resolve *`). Install is idempotent, rewrites only files carrying the Verinoda ownership marker, and records what it wrote in the install manifest; `verinoda uninstall` removes exactly those entries. The MCP tool list comes from the server itself, so new tools appear without re-registration. Since 2026-09-26 the server serves the core profile (twelve of the tools; the menu changed again with D61) by default and the skills read the CLI's text instead of `--json` (re-running setup brings them); see *Upgrading to the 2026-09-26 code* below for the tools that need `--profile full`. |
| Change review and behaviour probe (D35, D36) | Nothing to migrate: `verinoda review` stores each review as an `analyses` row (`rev_...`), `verinoda probe` runs through `experiments` like any other run. Re-run `verinoda setup` for the skill sections that call them (review before editing and before saying done; probe after editing Python functions). Since 2026-09-26 `probe` no longer refuses a function because a library module set an environment variable while it loaded (numpy sets `OPENBLAS_MAIN_FREE` when imported); project code that sets one is still refused. |
| Schema v5 and v6 (decisions, debug ledger) | Automatic on the first command: v5 adds the append-only `decisions`, `decision_briefs` and `decision_answers` tables (D33), v6 the debug ledger's sessions and attempts (D34). An older Verinoda refuses a v6 `atlas.db` ("newer than this Verinoda"). Decision records live as Markdown in the decisions folder (default `.verinoda/decisions/`); to run `decide check` in CI, commit the folder and name it in `verinoda.toml` (`[decisions] dir = "docs/decisions"`) or `pyproject.toml` (`[tool.verinoda.decisions] dir`); `.verinoda/config.json` (`decisions.dir`) is not committed. `decide check` now exits 3 (not 0) when a guard checked no file, edge or manifest, a file could not be read, a configured decisions folder does not exist, or no record exists while ADR-like files do: a CI job that treated any non-1 exit as a pass should treat 3 as "not checked". |
| Debug ledger (D34) | An agent-reported attempt must name the command it ran (`debug try --observed-output FILE --exit-code N -- <command>`). `debug close --resolved-by N` refuses a pass of a command other than the session's repro, a run where tests that failed before were skipped or not run, a tree Verinoda also saw fail, and (until `--accept-test-edit`) a tree whose tests changed since attempt 0. `debug differential` / `debug rerun` exit 3 when they did not settle it. `experiment run --ref REF --claim C` attaches the run as `qualifies` unless REF is the claim's own commit. Since 2026-09-26 `debug close --resolved-by` also refuses when the baseline passed and every failing run came after an edit (those failures are the edits' own; show the symptom with `debug rerun` on the base tree, or close with `--abandoned`), and a Minecraft GameTest run's failures are read from its summary ("N required tests failed" and the "- ns:test_id: message" lines), mapped to the test method, instead of from an exception the server logs while it starts. |
| Name check (D32) | Nothing to migrate. `verinoda check` / `verinoda api` need the `precise` extra (jedi); answers are cached under `.verinoda/cache/check/` (derived, safe to delete). Re-run `verinoda setup` to get the skill sections "Check the names code uses" and "Confirm your own sentences". MCP `code_check` / `api_members`: `env` accepts only `auto`, `none` or a virtual environment whose base interpreter is a known Python installation outside the project. `check` now exits 4 (it exited 0) for a file in another language, a notebook or Cython file, and a Python file that does not parse (all listed under `not_checked`, not counted under `files`); exit 3 still means an absent name or a lock mismatch. `api` exits 4 for a name of the project's code in another language. An attribute or keyword argument inside `try/except Exception` is `absent`, not `guarded` (cached answers of the old rule set are dropped: CHECK_VERSION 4). A hook or CI job that runs `check --diff` in a mixed repository sees exit 4 when a non-Python file changed: treat 4 as "not checked", 3 as "fix the code". |
| Truth rules (D31) | Claims written as plain text are graded again on their next `challenge`/`verify`: word overlap with the cited lines now gives `strong_inference` at most, text written with `claim add` that no typed check covers `weak_inference` at most, and a quote verifies only the quoted text. A passing run attached to a plain-text claim (`experiment run --claim`) stops at `strong_inference`; runs verify `test_run` claims that name the run. To keep a written claim verified, quote the lines (`path:12 contains: <text>`) or use a typed kind (`--kind relation\|config\|order\|location --symbol X`). `claim add --kind order` without `--symbol` takes the function the sentence places the calls in, or refuses and asks for `--symbol`. Since 2026-09-26, config and relation claims, and flow claims with a call hop, outside Python stop at `strong_inference` from their next `verify`/`challenge` (a SCIP answer still verifies relations). |
| Exact names, one build at a time, fresh index (D37) | Nothing to migrate. `receiver_calls.json` v2 is recomputed on the first load (its per-file facts are reused); `.verinoda/index/fresh_ignored.json` changed format (v2), and an older one is ignored and rewritten. Output and exit-code changes are listed below. |
| Upstream (Graphify) base | Maintainers only: `python tools/port_upstream.py <graphify-checkout-at-new-commit>`, review the diff, run `pytest tests` and `pytest tests_upstream`, update `docs/UPSTREAM.md` (commit, test table, inventory). Check that `index.install_path_identity_memo()` still finds `watch._StoredSourcePaths` (`tests/test_index.py` covers it). |

## Upgrading from 0.3.2 (D60-D76)

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

Add to "Upgrading from 0.3.2 (D60-D69)":

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
- The MCP server has 38 tools: `history_search` is new, served by the core profile behind `run_tool` and by
  the full profile. Reinstalled skills allow `verinoda history` and mention it.

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
