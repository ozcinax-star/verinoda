# Upstream origin: Graphify

Verinoda (formerly developed under the working name RepoAtlas) is a derivative of **Graphify**
(<https://github.com/Graphify-Labs/graphify>). It is an independent project and
is **not** an official Graphify release or endorsed by its authors.

| | |
|---|---|
| Upstream repository | https://github.com/Graphify-Labs/graphify |
| Pinned commit | `20a20d30d8e7eef77675651f0199d87f913bd3e7` (2026-09-20, "test(ts): de-flake the normalizer scaling test…") |
| Nearest tag | `v0.9.65` (the commit is after it on `main`) |
| Upstream package | `graphifyy` 0.9.65 (not a runtime dependency of Verinoda) |
| Licence | Apache-2.0, with earlier contributions under MIT. `LICENSE`, `LICENSE-MIT` and the upstream `NOTICE` text are kept unchanged. `NOTICE` adds the derivation statement. |
| Recorded in code | `verinoda/project_index/UPSTREAM_COMMIT` |

## How the code was brought in

`tools/port_upstream.py <graphify-checkout>` is the only way upstream code
enters this repository. It:

1. copies upstream `graphify/` → `verinoda/project_index/` and `tests/` → `tests_upstream/`;
2. rewrites module paths only: `graphify.<module>` → `verinoda.project_index.<module>`,
   `from graphify import` / `import graphify` / `-m graphify`, and the
   distribution name used in `importlib.metadata.version("graphifyy")` → `"verinoda"`;
3. in tests only, rewrites repo-relative paths (package dir, skill bodies,
   `ARCHITECTURE.md`/`how-it-works.md` now under `docs/upstream/`);
4. appends a clearly delimited "Verinoda port adjustments" block to
   `tests_upstream/conftest.py` (see below);
5. writes the upstream commit SHA to `verinoda/project_index/UPSTREAM_COMMIT`.

No file under `verinoda/project_index/` is hand-edited. Everything Verinoda
adds lives outside that directory, including the one runtime patch described
under [Runtime patch applied from outside the vendored tree](#runtime-patch-applied-from-outside-the-vendored-tree).
Re-syncing is therefore: check out a newer upstream commit, re-run the script,
review `git diff`, run both test suites.

The import rewrite was necessary rather than cosmetic: keeping a top-level
`graphify` package would collide with an installed `graphifyy` in the same
environment. There was no other bulk renaming (functions, CLI strings, the
`graphify-out` default name, messages all stay as upstream wrote them).

## Upstream test-suite after the port

Measured once, at port time, on the development machine (Windows 11,
Python 3.12.0, `.[mcp]` extras only). It was not re-run after the round-3
changes. Verinoda's round-3 work does not edit `project_index/`, but the
path-identity patch below changes how it runs inside Verinoda. That patch has
its own test in the product suite.

| Run | Passed | Failed | Skipped | Notes |
|---|---|---|---|---|
| Upstream checkout, unmodified (`pytest` in `upstream-graphify/`) | 5503 | 51 | 297 | baseline |
| Ported (`pytest tests_upstream`) | 5436 | 50 | 299 (+1 xfail) | |

Every failure in the ported run also fails in the unmodified upstream checkout
on the same machine (checked test-by-test). They come from optional grammars
that are dev-only upstream (`tree-sitter-hcl` → all terraform tests), Windows
lacking `os.mkfifo`/`AF_UNIX`/unprivileged symlinks, and Windows' default
`cp1254` console encoding in a few tests. The difference in the passed count is
`test_skillgen.py` (67 tests), which is excluded — see below.

Port adjustments (in `tests_upstream/conftest.py`, appended by the script):

- `test_skillgen.py` is not collected: it regenerates upstream's own skill
  bodies and audits them against blobs from upstream's git history
  (`git show <sha>:graphify/skill.md`). That history is not part of this
  repository, and those skill bodies are not Verinoda's skills.
- `test_the_warning_names_the_stale_destination_and_the_exact_command` is
  `xfail(strict=True)`: the upstream installer compares a skill stamp against
  the `graphifyy` version line (0.8.x/0.9.x); Verinoda is 0.1.x, so the
  fixture's "stale" skill looks newer and takes the downgrade branch.

## Runtime patch applied from outside the vendored tree

**Path-identity memo.** The vendored incremental rebuild
(`project_index/watch.py`, class `_StoredSourcePaths`) recomputes a pathlib
identity for every node and edge on every update. `identity()`,
`in_watch_root()` and `rebase_preserved()` are pure functions of the
`source_file` string and of fields set once in `__init__`. Verinoda
therefore memoises them per instance.

- **Where:** `verinoda.index.install_path_identity_memo()`, called from
  `verinoda.index.build()`. It is a monkeypatch. `watch.py` itself is
  unchanged and stays byte-identical to upstream. The patch is idempotent,
  and it is skipped silently if a future upstream renames the class.
- **What it changes:** only speed. The memo returns exactly what the original
  methods return. `tests/test_index.py::test_path_identity_memo_keeps_the_graph_byte_identical`
  builds two copies of `examples/orders_app`, changes one file in each and
  runs the vendored incremental rebuild, once with the memo and once
  without. It requires the two `graph.json` files to be byte-identical.
- **Measured effect** (round-3 search track, one run on the development
  machine, not a benchmark-harness result): a one-file update of
  `graphify_core` (226 files, `querylog.py` touched). The index step fell
  from 4.45 s without the memo to 2.69 s with it, and the whole
  `verinoda update` from 6.25 s to 4.24 s. The two `graph.json` files had
  identical nodes, links, attributes and order. They differed only in
  `built_at_commit`, because each measured copy was its own git repository.
  The memoised methods took about 60% of such an update before the patch.

## Post-processing of graph.json from outside the vendored tree

**Portable ids.** An id with no file of its own keeps what the upstream
pipeline minted it from: the target of an import whose file does not exist
(`require('./missing')`, resolved against the importer's absolute path) and the
element ids of a `.dmf` interface file (they embed the window's id) carry the
scan root folded into `_` parts, user name included.

- **Where:** `verinoda.portable_ids.make_graph_portable()`, called from
  `verinoda.index.build()` after the pipeline wrote graph.json and before the
  receiver-call sidecar is refreshed. No vendored file changes.
- **What it changes:** only ids that start with the root's folded form and
  belong to no node with a source file, and the `_elem_<root>_` part of `.dmf`
  ids; an id that would take one another thing already has gets `_unresolved`;
  nothing under a root of one folder. graph.json is written back in the
  pipeline's own format (indent 2, key order kept). On the eight benchmark
  corpora no id carries the root, so they are unchanged (fresh-index A/B,
  2026-09-24: identical results).

## Notes on vendored modules Verinoda does not use

- **`project_index/scip_ingest.py`** (left untouched) is upstream's skeleton
  for SCIP ingestion. It reads a *simplified* JSON shape (occurrences under
  each symbol), not the SCIP protobuf, and it treats the first element of
  SCIP's 0-based `range` as a 1-based line. It is not wired to any command.
  Verinoda reads real `index.scip` files with its own dependency-free
  decoder, `verinoda/scip_reader.py`, which converts lines to 1-based.

## Feature inventory

"Carried" = the code is present and unmodified apart from the import rewrite.
"Used" = a Verinoda command depends on it. "Exposed" = reachable by a user.

### Carried and used by Verinoda

| Upstream feature | Upstream location | How Verinoda uses it |
|---|---|---|
| File detection / corpus scan | `detect.py` | via the rebuild pipeline in `scan`/`update` |
| Tree-sitter AST extraction (Python, JS/TS, Go, Rust, Java, C/C++, C#, Ruby, Kotlin, Scala, PHP, Swift, Lua, Zig, PowerShell, Elixir, ObjC, Julia, Verilog, Fortran, Bash, JSON, Groovy + optional grammars) | `extract.py`, `extractors/` | the knowledge graph behind every Verinoda view. Upstream already resolves method calls by receiver type in about ten languages (Swift, TS/JS, C++, C#, Java, ObjC, Kotlin, Ruby, Rust); for Python it resolves only `ClassName.method()`. Verinoda adds a narrow Python pass for annotated parameters and `x = Cls()` locals, marked `INFERRED` + `derived_by=verinoda.receiver`. |
| Graph build, dedup, symbol resolution | `build.py`, `dedup.py`, `symbol_resolution.py`, … | same |
| Code-only rebuild (no LLM), incremental rebuild with changed paths, per-repo lock | `watch._rebuild_code` | `verinoda scan` (full) and `verinoda update` (changed files only), with the path-identity memo above |
| Community detection, report, HTML graph | `cluster.py`, `report.py`, `export.py` | produced in `.verinoda/index/` on every build (GRAPH_REPORT.md, graph.html) |
| Query term extraction and node scoring | `serve._query_terms`, `_score_query`, `_score_nodes`, `_pick_scored_endpoint` | fuzzy name resolution in `index.Graph.resolve` (trace endpoints, node lookups) after Verinoda's exact id/path/`Class.method` rules, and `retrieval.terms_for` (the term list of the benchmark's raw baseline). Ranking for `query`/`analyze` now comes from Verinoda's own `search_index.py`. |
| Graphify query renderer | `serve._query_graph_text` | the "Graphify baseline" in `verinoda benchmark` |
| Confidence labels on edges (EXTRACTED / INFERRED / AMBIGUOUS) | extraction schema | kept on every edge; Verinoda never treats them as verification |
| URL validation and safe fetching | `security.py` | document research (`verinoda research <url>`) and the reference resolver's live HTTP transport |

### Carried and exposed through `verinoda index -- …` (pass-through, unsupported)

The upstream CLI is available as `verinoda index -- <graphify arguments>`,
for example `verinoda index -- query "…"`, `-- path A B`, `-- explain X`,
`-- export …`, `-- tree`, `-- affected`, `-- diagnose`, `-- watch`,
`-- benchmark`, `-- prs`, `-- merge-graphs`, and `-- extract` (LLM semantic
extraction with the upstream provider backends). Output goes to
`.verinoda/index/`. Verinoda does not use or test these paths.

**Blocked pass-through commands** (exit 2 with an explanation;
`cli.UPSTREAM_BLOCKED`, `cli.UPSTREAM_HOME_WRITERS`):

| Blocked | Why | Use instead |
|---|---|---|
| `install`, `uninstall`, `hook`, `merge-driver`, and every platform installer: `agents`, `aider`, `amp`, `antigravity`, `claude`, `claw`, `codebuddy`, `codex`, `copilot`, `cursor`, `devin`, `droid`, `gemini`, `hermes`, `kilo`, `kiro`, `opencode`, `pi`, `skills`, `trae`, `trae-cn`, `vscode`. At run time the list is also unioned with upstream's own `install._CLI_INSTALL_COMMANDS`, so a platform added upstream is blocked without editing Verinoda. | They write Graphify-branded skills, hooks, merge drivers or agent configs, and would overwrite or remove a *real* Graphify install on the same machine. | `verinoda install` / `uninstall --agent claude\|codex` |
| `clone`, `provider`, `global`, and the `--global` flag on any command | They write under `~/.graphify` (clone cache, provider registry, global graph), outside the project and outside `.verinoda/`. Verinoda never writes to the home directory implicitly. | `verinoda research <url\|owner/repo> [--ref R]` (pinned checkout under `.verinoda/research`), after `verinoda resolve "<text>"` |

Tests: `tests/test_cli.py` checks every blocked name, that upstream's
installer list is a subset of the static list, and that ordinary commands
(`query`, `path`, `explain`, `tree`, `--help`, …) are not blocked.

### Carried but not exposed

| Upstream feature | Why not exposed |
|---|---|
| Upstream multi-platform installer (`install.py`; the upstream README lists 22 assistants) and its skill bodies (`skill*.md`, `skills/*/references`, `always_on/`) | They install *Graphify*-named skills that call a `graphify` executable Verinoda does not ship, and are blocked in the pass-through (above). Verinoda has its own installer (`verinoda.agents`, Claude Code + Codex only). Kept in the package so the upstream tests keep passing. |
| Git hooks, merge driver (`hooks.py`) | Would install `graphify`-named hooks; blocked in the pass-through. |
| Upstream MCP server (`serve.py`: `query_graph`, `get_node`, `get_neighbors`, `get_community`, `god_nodes`, `graph_stats`, `shortest_path`, `list_prs`, `triage_prs`, `get_pr_impact`) | `verinoda mcp serve` serves Verinoda's own tools (claims, evidence, plans, references, research, critique…). The upstream server still runs with `python -m verinoda.project_index.serve <graph.json>`. |
| SCIP JSON ingestion skeleton (`scip_ingest.py`) | Not wired upstream either; see the note above. |

### Removed from the distribution

| Upstream item | Reason |
|---|---|
| Console scripts `graphify`, `graphify-mcp` | Would clash with an installed `graphifyy`. |
| `Dockerfile`, `uv.lock`, `scripts/`, `worked/` examples, `docs/translations/`, logos and other visual assets | Not needed; Verinoda must not reuse Graphify's visual identity. |
| Upstream README/ARCHITECTURE/BENCHMARKS/CHANGELOG/SECURITY/how-it-works | Kept verbatim under `docs/upstream/` for reference and for the upstream tests that read them. They describe Graphify, not Verinoda. In particular Graphify's published accuracy, cost and token figures are **not** Verinoda claims; Verinoda's own measurements are in `docs/BENCHMARKS.md`. |

### Modified

| Item | Change |
|---|---|
| All upstream Python and skill files | module paths only (see above) |
| Version lookup | reports the `verinoda` distribution version |
| Output directory | unchanged in the module (`GRAPHIFY_OUT`, default `graphify-out`); Verinoda entry points set `GRAPHIFY_OUT=.verinoda/index` before importing it |
| `watch._StoredSourcePaths` (at run time only) | memoised by the path-identity monkeypatch; source file unchanged |
| `tests_upstream/conftest.py` | appended port-adjustment block (above) |

### New in Verinoda (not in Graphify)

Everything under `verinoda/` except `project_index/`, described per module in
[ARCHITECTURE.md](ARCHITECTURE.md):

- **Graph and indexes:** `index.py`, `search_index.py`, `textnorm.py`,
  `lexicon.py`, `snapshot.py`, `architecture_map.py`, `retrieval.py`.
- **Question and references:** `question_plan.py`, `references/`.
- **Claims and trust:** `store.py`, `evidence.py`, `anchors.py`, `entail.py`,
  `claims.py`, `callsite.py`, `critique.py`, `precise.py`, `scip_reader.py`,
  `runtime/`, `memory.py`.
- **Loops and entry points:** `workflow.py`, `analysis.py`, `experiments.py`,
  `research.py`, `feedback.py`, `cli.py`, `mcp/`, `agents/`, `benchmark/`,
  `doctor.py`, `paths.py`.

Also new: `tests/`, `examples/orders_app`, `tools/port_upstream.py`,
`tools/record_reference_cassettes.py`, and the docs in `docs/` except
`docs/upstream/`.
