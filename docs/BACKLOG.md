# Backlog: features competitors have that Verinoda lacks

Source: a survey of about 150 open-source and commercial tools (code graphs and MCP servers, context engines,
program analysis, review and test tools, agent memory and docs tools, Minecraft/JVM tooling), 2026-09-29.
112 features were found; 106 fit Verinoda and are listed here, in build order. The 6 that do not fit are at the end.

How this file is used:

- **An item is deleted when it ships.** The shipped work gets its own decision in `docs/DESIGN.md` (D72, D73, ...)
  and a note in `docs/UPGRADING.md`; the design section, not this file, is the record. Code comments cite the
  D-number, never a backlog id, because the id disappears with the row.
- `verinoda backlog 1.1` shows an item; `verinoda backlog` reads this file (sections `## N - Title`, rows
  `| N.M | **Title** | ... |`).
- **(opt-in)** marks an item that needs the network, an external tool or a heavy dependency: off by default,
  turned on explicitly, like `resolve --network on`.
- Rules every item keeps: output is claims with a status and `file:line` evidence; a heuristic result (a score, a
  coupling, an embedding hit) is `strong_inference` at most; the core MCP profile stays at five tools (new tools go
  behind `run_tool` or the full profile); nothing leaves the machine unless the user turned it on.
- The "Seen in" column names tools that have the feature, for reference; it is not a claim about their quality.

## 1 - Incremental index core

Everything that runs on every edit (hooks, `ui --watch`, the test map) waits on this block.

Status of 1.1 (2026-09-30): stage A (the same outputs, less work) is implemented on six `stagea-*` branches, not yet
verified or merged; see `benchmarks/results/stage-a-2026-09-30/README.md` for the measurements, the branches and the
next steps. Stage B (incremental cross-file passes) is not started.

| id | feature | what | seen in | done when |
|---|---|---|---|---|
| 1.1 | **Update proportional to the change** | Per-file hashes, re-extract only changed files and patch their nodes and edges into the graph, no whole-corpus rebuild; branch switch detected | claude-context, narsil-mcp, codebase-memory-mcp, CodeGraph, GitHub Stack Graphs | an update of one edited file on Verinoda's own repository takes seconds, and its graph equals a full scan's node for node and edge for edge |
| 1.2 | **Native file watcher** | OS file events drive the incremental update instead of polling | narsil-mcp, codebase-memory-mcp | `ui --watch` and the MCP server pick up an edit without a manual `update` |
| 1.4 | **Broader language coverage** | Pull the grammars upstream Graphify added after the fork (COBOL, R, Solidity, Erlang, OCaml, Terraform attributes, Razor and others) | Graphify, codebase-memory-mcp | each new language has an extraction fixture test |
| 1.5 | **Daemon and multi-project server** | One MCP server over several indexed projects; HTTP transport with a token; `list_projects`, `index_status` | Graphify, codebase-memory-mcp, Codanna, Kodit | two projects answered from one server process |
| 1.6 | **Multi-repository index** | Index several local repositories as one group with cross-repo symbol links | Sourcegraph, Graphify merge-graphs, GitNexus | a call from repo A to a function in repo B is an edge with its call site |
| 1.7 | **Trigram regex index** | A local trigram index for exact and regex search faster than a file walk | Zoekt, Cursor, Moderne | regex search on a 2,000-file repository answers in under a second |

## 2 - Precise resolution layer

| id | feature | what | seen in | done when |
|---|---|---|---|---|
| 2.1 | **LSP-backed navigation (opt-in)** | Definition, references, hover, diagnostics, call and type hierarchy, implementations from an installed language server; the answer is verification, not extraction | Serena, mcp-language-server, Octocode, JetBrains IDE MCP | a Java or TypeScript call edge confirmed by the language server is `statically_verified` |
| 2.2 | **SCIP indexers run by Verinoda (opt-in)** | Detect the build and run scip-java, scip-typescript, scip-python and others instead of asking the user for a SCIP file | Sourcegraph auto-indexing | `scan --precise` on a Gradle project produces and adopts a SCIP index |
| 2.3 | **Compiler frontends (opt-in)** | javac/JDT, Roslyn, go/types, libclang as resolvers where no language server is installed | code-graph-rag, Eclipse JDT | one non-Python language resolved without an LSP |
| 2.4 | **Full type and name check through tsc, pyright or mypy** | Run the project's own checker and turn its diagnostics into `check` sites | narsil-mcp, common practice | `check` on TypeScript reports members and calls, not only imports |
| 2.6 | **Import JVM checker findings** | Error Prone and NullAway results, and jdeps' JDK-internal API use, read as evidence | Error Prone, NullAway, jdeps | their findings appear as claims with the tool named |

## 3 - Structural search and query language

| id | feature | what | seen in | done when |
|---|---|---|---|---|
| 3.2 | **Code query language** | A small declarative query language over the graph (nodes, edges, paths) that answers with evidence | CodeQL, Glean Angle, jQAssistant Cypher, NDepend CQLinq, Joern | a query such as "functions that write storage and are reachable from an HTTP handler" returns its sites |
| 3.3 | **Derived facts** | A verified claim or query result stored as a fact other queries use, recomputed when its evidence goes stale | Glean derived predicates, jQAssistant concepts | a derived fact goes stale with the code it rests on |
| 3.5 | **Scripted aggregation over results** | A sandboxed script counts and cross-references search hits (inventories computed, not estimated) | Sourcegraph MCP evaluator | an inventory question answered by a count with its sites |

## 4 - Git history mining

| id | feature | what | seen in | done when |
|---|---|---|---|---|

## 5 - Retrieval and token economy

| id | feature | what | seen in | done when |
|---|---|---|---|---|
| 5.3 | **Hybrid lexical and embedding search (opt-in)** | A local embedding model adds leads fused with BM25 (reciprocal-rank fusion); leads only, never evidence | GitNexus, claude-context, Codanna, cocoindex-code, Augment | benchmark sets gain facts with no fact lost |

## 6 - Graph metrics and dependency health

| id | feature | what | seen in | done when |
|---|---|---|---|---|
| 6.5 | **Reachable vulnerable dependencies and SBOM (opt-in)** | Advisories filtered to library functions the code actually calls; CycloneDX output; licenses | Semgrep Supply Chain, narsil-mcp | an advisory is reported only with a call path to the vulnerable function |

## 7 - Architecture rules engine

| id | feature | what | seen in | done when |
|---|---|---|---|---|
| 7.5 | **DSM view and C4 model check** | Dependency structure matrix; a C4 model compared with the real graph | Lattix, NDepend, IntelliJ, Structurizr | the matrix in `ui`; model edges without code edges listed |
| 7.6 | **Guards written as programs** | A decision record's guard as a small script with graph access; a pre-commit hook | Archgate | a script guard runs in `decide check` |

## 8 - Change review extensions

| id | feature | what | seen in | done when |
|---|---|---|---|---|
| 8.8 | **Pull request triage (opt-in)** | Open PRs with CI and review state, PR impact, a warning when two PRs touch the same code | Graphify MCP | needs authenticated GitHub access |

## 9 - Test map and coverage

| id | feature | what | seen in | done when |
|---|---|---|---|---|
| 9.3 | **Mutation testing scoped to the diff** | Surviving mutants on changed lines: do the reaching tests actually check the change? | mutmut, cosmic-ray, PIT, cargo-mutants, Stryker | surviving mutants reported with their line |

## 10 - New graph edges

| id | feature | what | seen in | done when |
|---|---|---|---|---|
| 10.1 | **Cross-service edges** | HTTP, gRPC, GraphQL and tRPC client calls linked to their route handlers; event emit and listen edges; framework route tables | codebase-memory-mcp, CodeGraph, GitNexus, Bito, CodeSee | `trace` crosses from a frontend fetch to its backend handler |
| 10.2 | **ORM, DI and database schema (live database opt-in)** | ORM models, dependency-injection bindings, migrations and schema as nodes | agentforge-graph, Graphify, Kodit | the dataflow view reaches a table |

## 11 - Claim history and memory

| id | feature | what | seen in | done when |
|---|---|---|---|---|

## 12 - Docs, decisions and specs

| id | feature | what | seen in | done when |
|---|---|---|---|---|
| 12.8 | **Issue and chat sources (opt-in)** | PR, issue, Jira and Slack threads as "why" evidence, with contradictions between sources shown | Unblocked, Glean, Tabnine | a why-answer cites a PR discussion |

## 13 - Agent integration

| id | feature | what | seen in | done when |
|---|---|---|---|---|
| 13.1 | **Hooks on the agent's own tool calls** | PreToolUse and PostToolUse hooks add graph context to the agent's Grep and Read in Claude Code, Codex and Cursor | GitNexus, Codanna, CodeGraph | a Grep in Claude Code returns the matching symbols' callers without a Verinoda call |
| 13.3 | **Installers for more agents** | Cursor, Gemini CLI, GitHub Copilot, Kiro, Aider, Continue and others | Graphify | `setup --agents all` registers each one found |
| 13.4 | **Package existence and slopsquatting check (opt-in)** | A new dependency checked against its registry: exists, age, downloads, malware signals | Socket MCP, Endor Labs | `check` flags a dependency name the registry does not have |

## 14 - Outputs and views

| id | feature | what | seen in | done when |
|---|---|---|---|---|

## 15 - Runtime evidence import

| id | feature | what | seen in | done when |
|---|---|---|---|---|
| 15.1 | **Runtime flaws from traces** | N+1 queries, repeated SQL and slow paths found in recorded test runs | AppMap, Digma | `observe` reports an N+1 with its call path |

## 16 - Minecraft: vanilla source and mappings

| id | feature | what | seen in | done when |
|---|---|---|---|---|
| 16.1 | **Decompiled vanilla source (opt-in)** | The game version the build uses, decompiled and cached; classes and methods readable as evidence; its call graph and hierarchy | minecraft-dev-mcp, mcdev-mcp, minecraft-modding-mcp, ModLens, Loom genSources | `trace` and `when` continue into vanilla code |
| 16.2 | **Mapping namespace translation (opt-in)** | Names between obfuscated, Mojmap, Intermediary, Yarn, SRG and Parchment; stack traces remapped before `trace-log` | Linkie, Enigma, StackDeobfuscator | an intermediary crash log maps onto named code |
| 16.3 | **Symbol exists in a version and namespace** | "Does this method exist in 1.21.4 Mojmap?" | minecraft-modding-mcp | `api` answers per version |
| 16.4 | **Version diff for porting (opt-in)** | Class, member and registry changes between two game versions; breaking changes that touch the project | minecraft-dev-mcp, ModLens primers | `verinoda port 1.21.4 1.21.5` lists the project's sites that break |
| 16.5 | **Third-party mod jars (opt-in)** | A dependency mod's metadata, entry points, Mixin configs and decompiled code | minecraft-dev-mcp, ModLens | `api` reads a dependency mod's class |
| 16.7 | **Vanilla registry and data lookup (opt-in)** | Blocks, items, entities, vanilla tags, recipes and commands of the version | ModLens, misode/mcmeta | a datapack id is told apart from a typo |

## 17 - Minecraft validators

| id | feature | what | seen in | done when |
|---|---|---|---|---|
| 17.1 | **Mixin injection points** | `@At` targets, method selectors, descriptors and `@Shadow` signatures checked against the target's bytecode | MinecraftDev, minecraft-modding-mcp | a wrong `@At` target is `absent` with the nearest real one |
| 17.3 | **Mixin conflicts across mods** | Several mods injecting into the same method; the mod behind a failed injection | ModLens, MixinConflictHelper | conflicts listed with both Mixins |
| 17.4 | **Mixin debug export as evidence** | `.mixin.out` classes and audit reports read as what a Mixin really changed | SpongePowered Mixin | a Mixin claim cites the exported class |
| 17.5 | **Command syntax and JSON schemas per version** | Commands, resource locations, NBT paths, loot tables and predicates checked against vanilla-mcdoc | Spyglass, Datapack Helper Plus | a malformed command is reported with its line |

## 18 - Flow analysis

| id | feature | what | seen in | done when |
|---|---|---|---|---|
| 18.1 | **Control and data dependence** | Control-flow and program-dependence graphs, reaching definitions, backward and forward slices | Joern, GitNexus pdg_query, narsil-mcp, CodePrism, Understand | a slice answers "where does this argument's value come from" with its lines |
| 18.2 | **Taint analysis** | User-declared sources, sinks and sanitizers; library behaviour as data; each result with its full path; SARIF | CodeQL, Semgrep Pro, Pysa, Joern, Code Pathfinder | a source-to-sink path reported with every hop |

## 19 - Behaviour probes and test generation

| id | feature | what | seen in | done when |
|---|---|---|---|---|
| 19.1 | **Symbolic differential behaviour (opt-in)** | Solver-found inputs where two versions differ; inputs that reach every branch | CrossHair diffbehavior and cover | `probe --symbolic` finds a planted difference random inputs miss |
| 19.2 | **Java differential tests (opt-in)** | Tests that pass on the old version and fail on the new, for Java | EvoSuiteR, Randoop, Diffblue Cover | `probe` works on a Java method |
| 19.4 | **Regression test generation (opt-in)** | Tests that pin current behaviour before a refactor | Pynguin | a generated test passes on the current tree |
| 19.5 | **Runtime diff between base and head** | Call paths, SQL queries, routes and exceptions added or removed at runtime between two revisions | AppMap compare | `review --observe` lists runtime changes |

## 20 - Evaluation on a Docker machine

| id | feature | what | seen in | done when |
|---|---|---|---|---|
| 20.1 | **Official SWE-bench Lite evaluation of the Claude Code study (needs Docker or a cloud runner)** | The study kept in the Verinoda Symbiosis repository (github.com/Verinoda-Labs/verinoda-symbiosis: design `benchmarks/agent_compare/DESIGN_SWEBENCH.md`, harness `swebench_prepare.py` and `swebench_run.py`), Claude Code alone against Claude Code with Verinoda Symbiosis on 100 SWE-bench Lite instances, writes `predictions-none.jsonl` and `predictions-verinoda_guided.jsonl`, but its main measure, the share of instances resolved, is **not measured**: the official harness runs each instance in a Docker image (x86_64 Linux, about 120 GB of disk, 16 GB of RAM) and the Windows machine the study ran on cannot. To do on a machine with Docker, or with `sb-cli` or Modal (`benchmarks/agent_compare/SWEBENCH_EVAL.md` in that repository has the commands): (1) take the two predictions files (committed in that repository under `benchmarks/results/swebench-lite-100-2026-10-10/` when the run ends); (2) evaluate each with the same dataset and instance list; (3) keep the reports and the per-instance resolved lists beside the predictions; (4) run the paired analysis of the design (instances resolved in one arm only, bootstrap 95 % interval); (5) add the result to that repository's `docs/DESIGN.md`, the proxies of the pilot beside it. Until then no resolved share is reported, and a result is not compared with the leaderboard (the agents ran without the projects' dependencies installed) | none: our own idea (2026-10-10), not from the survey | both arms' resolved shares and their paired difference with its interval, from the official harness, are in that folder |

## Out of scope

Not built, with the reason; listed so they are not proposed again without a new reason.

- Time-travel debugging (Undo, rr, Replay.io): a native debugger is a separate product.
- Live snapshots in running production services (Lightrun, Dynatrace): needs an agent in production.
- Branching and merging a shared knowledge store (ByteRover): needs a sync server.
- A single static binary: Verinoda is a Python package (Homebrew, Scoop and Winget wrappers stay possible).
- A bridge into a running game client (mcdev-mcp DebugBridge, ModLens): needs a mod shipped into the game.
- Reading and writing NBT: off the analysis mission.
